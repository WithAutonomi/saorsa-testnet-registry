"""Translate the prod-* manifest kinds into step lists.

The prod kinds are single-command deployments (each ``prod <x> deploy`` wraps its
own infra/monitoring orchestration), so most builders emit exactly one step. The
value here is the same as for testnets: the manifest is the reusable, diffable
record and ``plan`` shows precisely what will run.

Secrets are never embedded in argv. The underlying prod commands read the wallet
key from the environment (``ARBITRUM_ONE_SECRET_KEY``); see ``deploy_cmds`` for
the env-bridge that honours a custom ``secret_key_env``.
"""

from __future__ import annotations

from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator.steps import Step, ant_binary_flags


def _prod_uploader_spec(u) -> str:
    spec = f"{u.region}:{u.vm_size}:urls={u.urls}"
    if u.payment:
        spec += f":payment={u.payment}"
    if u.clock_skew:
        spec += ":clock_skew=true"
    return spec


def _probe_spec(p) -> str:
    parts = [p.region]
    if p.vm_size:
        parts.append(p.vm_size)
    return ":".join(parts) + f":count={p.count}"


def _uploaders(manifest: Manifest) -> list[Step]:
    spec = manifest.prod_uploaders
    argv = ["prod", "uploaders", "deploy", "--name", manifest.name, "--purpose", manifest.purpose]
    for u in spec.uploaders:
        argv += ["--uploader", _prod_uploader_spec(u)]
    if spec.allow_duplicate_files:
        argv += ["--allow-duplicate-files"]
    argv += ant_binary_flags(spec.binary)
    if spec.monitoring_volume_size is not None:
        argv += ["--monitoring-volume-size", str(spec.monitoring_volume_size)]
    argv += ["--ssh-key-path", manifest.ssh_key_path]
    return [Step(argv, "Deploy prod uploaders + monitoring")]


def _downloaders(manifest: Manifest) -> list[Step]:
    spec = manifest.prod_downloaders
    argv = [
        "prod",
        "downloaders",
        "deploy",
        "--name",
        manifest.name,
        "--purpose",
        manifest.purpose,
        "--spec",
        spec.spec,
    ]
    if spec.addresses_path is not None:
        argv += ["--addresses-path", spec.addresses_path]
    if spec.clock_skew:
        argv += ["--clock-skew"]
    argv += ant_binary_flags(spec.binary)
    if spec.monitoring_volume_size is not None:
        argv += ["--monitoring-volume-size", str(spec.monitoring_volume_size)]
    argv += ["--ssh-key-path", manifest.ssh_key_path]
    return [Step(argv, "Deploy prod one-shot downloaders + monitoring")]


def _continuous_downloader(manifest: Manifest) -> list[Step]:
    spec = manifest.prod_continuous_downloader
    argv = [
        "prod",
        "continuous-downloader",
        "deploy",
        "--name",
        manifest.name,
        "--purpose",
        manifest.purpose,
        "--spec",
        spec.spec,
    ]
    if spec.addresses_path is not None:
        argv += ["--addresses-path", spec.addresses_path]
    if spec.sleep_interval_secs is not None:
        argv += ["--sleep-interval-secs", str(spec.sleep_interval_secs)]
    if spec.clock_skew:
        argv += ["--clock-skew"]
    argv += ant_binary_flags(spec.binary)
    if spec.monitoring_volume_size is not None:
        argv += ["--monitoring-volume-size", str(spec.monitoring_volume_size)]
    argv += ["--ssh-key-path", manifest.ssh_key_path]
    return [Step(argv, "Deploy prod continuous downloader + monitoring")]


def _canary(manifest: Manifest) -> list[Step]:
    spec = manifest.prod_canary
    argv = [
        "prod",
        "canary",
        "deploy",
        "--name",
        manifest.name,
        "--purpose",
        manifest.purpose,
        "--upload-spec",
        spec.upload_spec,
    ]
    for p in spec.probes:
        argv += ["--probe-spec", _probe_spec(p)]
    argv += ["--urls-file", spec.urls_file]
    if spec.upload_sleep_secs is not None:
        argv += ["--upload-sleep-secs", str(spec.upload_sleep_secs)]
    if spec.probe_concurrency is not None:
        argv += ["--probe-concurrency", str(spec.probe_concurrency)]
    argv += ant_binary_flags(spec.binary)
    if spec.monitoring_volume_size is not None:
        argv += ["--monitoring-volume-size", str(spec.monitoring_volume_size)]
    argv += ["--ssh-key-path", manifest.ssh_key_path]
    return [Step(argv, "Deploy prod canary (upload + probes) + monitoring")]


def _local_uploads(manifest: Manifest) -> list[Step]:
    spec = manifest.prod_local_uploads
    argv = ["prod", "local-uploads", "--name", manifest.name, "--purpose", manifest.purpose]
    if spec.urls is not None:
        argv += ["--urls", spec.urls]
    else:
        argv += ["--local-dir-path", spec.local_dir]
    if spec.payment is not None:
        argv += ["--payment", spec.payment]
    argv += ["--verbose"] * spec.verbose
    argv += ant_binary_flags(spec.binary)
    return [Step(argv, "Run prod uploads from the local machine")]


def _local_downloads(manifest: Manifest) -> list[Step]:
    spec = manifest.prod_local_downloads
    argv = [
        "prod",
        "local-downloads",
        "--name",
        manifest.name,
        "--purpose",
        manifest.purpose,
        "--addresses-path",
        spec.addresses_path,
    ]
    if spec.keep_output_dir is not None:
        argv += ["--keep-output-dir", spec.keep_output_dir]
    argv += ant_binary_flags(spec.binary)
    return [Step(argv, "Run prod downloads from the local machine")]


_DEPLOY_BUILDERS = {
    "prod-uploaders": _uploaders,
    "prod-downloaders": _downloaders,
    "prod-continuous-downloader": _continuous_downloader,
    "prod-canary": _canary,
    "prod-local-uploads": _local_uploads,
    "prod-local-downloads": _local_downloads,
}

# Destroy is a single command for the VM-backed kinds; local-* have no infra.
_DESTROY = {
    "prod-uploaders": ["prod", "uploaders", "destroy"],
    "prod-downloaders": ["prod", "downloaders", "destroy"],
    "prod-continuous-downloader": ["prod", "continuous-downloader", "destroy"],
    "prod-canary": ["prod", "canary", "destroy"],
}


def build_deploy_steps(manifest: Manifest) -> list[Step]:
    try:
        builder = _DEPLOY_BUILDERS[manifest.kind]
    except KeyError:
        raise ValueError(f"no prod orchestrator for kind '{manifest.kind}'")
    return builder(manifest)


def build_destroy_steps(manifest: Manifest) -> list[Step]:
    base = _DESTROY.get(manifest.kind)
    if base is None:
        # local-* kinds run on the host and create no infrastructure to tear down.
        return []
    # prod destroy commands take no --force (they don't prompt); they accept
    # --name, --ssh-key-path and an optional --keep-monitoring.
    argv = [*base, "--name", manifest.name, "--ssh-key-path", manifest.ssh_key_path]
    if getattr(manifest.spec, "keep_monitoring", None):
        argv.append("--keep-monitoring")
    return [Step(argv, f"Destroy {manifest.kind}")]
