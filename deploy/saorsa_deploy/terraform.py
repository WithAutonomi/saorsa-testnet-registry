import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TerraformResult:
    success: bool
    provider: str
    region: str
    stdout: str = ""
    stderr: str = ""
    outputs: dict = field(default_factory=dict)


@dataclass
class TerraformRunConfig:
    provider: str
    region: str
    tf_source_dir: Path
    workspace_dir: Path
    state_key: str
    variables: dict[str, str] = field(default_factory=dict)


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

    env = os.environ.copy()
    if "DO_TOKEN" in env:
        env["TF_VAR_do_token"] = env["DO_TOKEN"]

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


def run_terraform_destroy(config: TerraformRunConfig) -> TerraformResult:
    """Run terraform init + destroy for a single provider/region."""
    prepare_workspace(config)

    env = os.environ.copy()
    if "DO_TOKEN" in env:
        env["TF_VAR_do_token"] = env["DO_TOKEN"]

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
    return TerraformResult(
        success=destroy_result.returncode == 0,
        provider=config.provider,
        region=config.region,
        stdout=destroy_result.stdout,
        stderr=destroy_result.stderr,
    )
