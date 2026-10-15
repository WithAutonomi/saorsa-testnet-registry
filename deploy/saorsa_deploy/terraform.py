import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

DO_VOLUME_DETACH_RACE_MARKERS = (
    "Error detaching the volumes from the droplet",
    "404",
    "error detaching volume from droplet: not found",
)

DESTROY_RETRY_MARKER = (
    "--- saorsa-deploy: retrying terraform destroy after "
    "the DigitalOcean volume-detach 404 race ---"
)


@dataclass
class TerraformResult:
    success: bool
    provider: str
    region: str
    stdout: str = ""
    stderr: str = ""
    outputs: dict = field(default_factory=dict)
    retried: bool = False


@dataclass
class TerraformRunConfig:
    provider: str
    region: str
    tf_source_dir: Path
    workspace_dir: Path
    state_key: str
    variables: dict[str, str] = field(default_factory=dict)


def _build_terraform_env() -> dict[str, str]:
    """Build an environment dict with provider tokens mapped to TF_VAR_ equivalents."""
    env = os.environ.copy()
    token_mappings = {
        "DO_TOKEN": "TF_VAR_do_token",
        "HCLOUD_TOKEN": "TF_VAR_hcloud_token",
        "VULTR_API_KEY": "TF_VAR_vultr_api_key",
    }
    for env_var, tf_var in token_mappings.items():
        if env_var in env:
            env[tf_var] = env[env_var]
    return env


def prepare_workspace(config: TerraformRunConfig) -> None:
    """Copy Terraform files from source directory to workspace directory."""
    config.workspace_dir.mkdir(parents=True, exist_ok=True)
    for tf_file in config.tf_source_dir.glob("*.tf"):
        shutil.copy2(tf_file, config.workspace_dir / tf_file.name)


def build_init_args(config: TerraformRunConfig) -> list[str]:
    """Build the argument list for terraform init."""
    return [
        "terraform",
        "init",
        "-input=false",
        "-reconfigure",
        f"-backend-config=key={config.state_key}",
    ]


def build_apply_args(config: TerraformRunConfig) -> list[str]:
    """Build the argument list for terraform apply."""
    args = [
        "terraform",
        "apply",
        "-auto-approve",
        "-input=false",
    ]
    for key, value in sorted(config.variables.items()):
        args.append(f"-var={key}={value}")
    return args


def build_output_args() -> list[str]:
    """Build the argument list for terraform output."""
    return ["terraform", "output", "-json"]


def run_terraform(config: TerraformRunConfig) -> TerraformResult:
    """Run terraform init + apply + output for a single provider/region."""
    prepare_workspace(config)

    env = _build_terraform_env()

    init_args = build_init_args(config)
    init_result = subprocess.run(
        init_args,
        cwd=str(config.workspace_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if init_result.returncode != 0:
        return TerraformResult(
            success=False,
            provider=config.provider,
            region=config.region,
            stdout=init_result.stdout,
            stderr=init_result.stderr,
        )

    apply_args = build_apply_args(config)
    apply_result = subprocess.run(
        apply_args,
        cwd=str(config.workspace_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if apply_result.returncode != 0:
        return TerraformResult(
            success=False,
            provider=config.provider,
            region=config.region,
            stdout=apply_result.stdout,
            stderr=apply_result.stderr,
        )

    outputs = {}
    output_args = build_output_args()
    output_result = subprocess.run(
        output_args,
        cwd=str(config.workspace_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if output_result.returncode == 0 and output_result.stdout.strip():
        raw = json.loads(output_result.stdout)
        outputs = {k: v.get("value") for k, v in raw.items()}

    return TerraformResult(
        success=True,
        provider=config.provider,
        region=config.region,
        stdout=apply_result.stdout,
        stderr=apply_result.stderr,
        outputs=outputs,
    )


def build_destroy_args(config: TerraformRunConfig) -> list[str]:
    """Build the argument list for terraform destroy."""
    args = [
        "terraform",
        "destroy",
        "-auto-approve",
        "-input=false",
    ]
    for key, value in sorted(config.variables.items()):
        args.append(f"-var={key}={value}")
    return args


def is_do_volume_detach_race(output: str) -> bool:
    """Return True if Terraform output carries the DigitalOcean volume-detach 404 signature.

    Whitespace is collapsed before matching: Terraform and rich both wrap these lines, so the
    droplet id lands on the line after the word "droplet".
    """
    collapsed = " ".join(ANSI_ESCAPE_RE.sub("", output).split())
    return all(marker in collapsed for marker in DO_VOLUME_DETACH_RACE_MARKERS)


def run_terraform_destroy(
    config: TerraformRunConfig, *, retry_delay_secs: int = 10
) -> TerraformResult:
    """Run terraform init + destroy for a single provider/region, retrying one known race.

    The DigitalOcean provider's droplet delete first POSTs a detach for every id in the
    droplet's ``volume_ids``, read from the state refreshed at plan time — before the
    ``digitalocean_volume_attachment`` was destroyed. Terraform then deletes the volume and the
    droplet concurrently, the volume goes in 0s, and the detach 404s on a volume that is already
    gone. One retry suffices because the second destroy refreshes state first, by which point the
    droplet's ``volume_ids`` is empty. That failed the first ``down`` of all five prod client
    deployments on 2026-10-15, and every retry was clean.
    """
    prepare_workspace(config)

    env = _build_terraform_env()

    init_args = build_init_args(config)
    init_result = subprocess.run(
        init_args,
        cwd=str(config.workspace_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if init_result.returncode != 0:
        return TerraformResult(
            success=False,
            provider=config.provider,
            region=config.region,
            stdout=init_result.stdout,
            stderr=init_result.stderr,
        )

    destroy_args = build_destroy_args(config)
    destroy_result = subprocess.run(
        destroy_args,
        cwd=str(config.workspace_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if (
        destroy_result.returncode != 0
        and config.provider == "digitalocean"
        and is_do_volume_detach_race(destroy_result.stdout + destroy_result.stderr)
    ):
        time.sleep(retry_delay_secs)
        retry_result = subprocess.run(
            destroy_args,
            cwd=str(config.workspace_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        return TerraformResult(
            success=retry_result.returncode == 0,
            provider=config.provider,
            region=config.region,
            stdout=f"{destroy_result.stdout}\n{DESTROY_RETRY_MARKER}\n{retry_result.stdout}",
            stderr=f"{destroy_result.stderr}\n{DESTROY_RETRY_MARKER}\n{retry_result.stderr}",
            retried=True,
        )

    return TerraformResult(
        success=destroy_result.returncode == 0,
        provider=config.provider,
        region=config.region,
        stdout=destroy_result.stdout,
        stderr=destroy_result.stderr,
    )
