"""Translate the prod-* manifest kinds into step lists.

The prod kinds are single-command deployments (each ``prod <x> deploy`` wraps its
own infra/monitoring orchestration), so most builders emit exactly one step. The
value here is the same as for testnets: the manifest is the reusable, diffable
record and ``plan`` shows precisely what will run.

A branch-sourced ``binary`` additionally prepends a ``build ant`` step (mirroring
the testnet orchestrator): every prod command — the local-* kinds included —
resolves branch binaries from the name-scoped S3 custom build, which only
``build ant`` populates.

Secrets are never embedded in argv. The underlying prod commands read the wallet
key from the environment (``ARBITRUM_ONE_SECRET_KEY``); see ``deploy_cmds`` for
the env-bridge that honours a custom ``secret_key_env``.
"""

from __future__ import annotations

from saorsa_deploy.config.models import Manifest
from saorsa_deploy.orchestrator.steps import (
    Step,
    ant_binary_flags,
    ant_build_steps,
    build_steps_for_branches,
    node_binary_flags,
)
from saorsa_deploy.provisioning.network_profile import format_network_token


def _format_pool_request(pool: dict) -> str:
    """Render a {class: count} request as the CLI spec token body.

    Order follows the size-class order rather than dict insertion, so the same
    manifest always produces the same argv — which matters because the step
    journal compares argv to decide whether a resumed run matches its plan.
    """
    from saorsa_db.size_class import ordered as size_class_ordered

    known = {c.value: c for c in size_class_ordered()}
    ordered_keys = [c.value for c in size_class_ordered() if c.value in pool]
    # Anything unrecognised is passed through (and rejected downstream with a
    # message naming the valid classes) rather than silently dropped.
    ordered_keys += [k for k in pool if k not in known]
    return ",".join(f"{key}:{pool[key]}" for key in ordered_keys)


def _prod_uploader_spec(u) -> str:
    if u.pool:
        spec = f"{u.region}:{u.vm_size}:pool={_format_pool_request(u.pool)}"
    else:
        spec = f"{u.region}:{u.vm_size}:urls={u.urls}"
    if u.payment:
        spec += f":payment={u.payment}"
    if u.log_level:
        spec += f":log_level={u.log_level}"
    if u.clock_skew:
        spec += f":clock_skew={u.clock_skew}"
    if u.bootstrap_cache is not None:
        spec += f":bootstrap_cache={str(u.bootstrap_cache).lower()}"
    if u.forward_logs is not None:
        spec += f":forward_logs={str(u.forward_logs).lower()}"
    if u.memory_profiling is not None:
        spec += f":memory_profiling={str(u.memory_profiling).lower()}"
    if u.network is not None:
        spec += f":network={format_network_token(u.network)}"
    # rust_log MUST be last — its value may contain ':' (RUST_LOG '::' paths).
    if u.rust_log is not None:
        spec += f":rust_log={u.rust_log}"
    return spec


def _bootstrap_cache_flag(spec) -> list[str]:
    """argparse BooleanOptionalAction flag for spec-level bootstrap_cache."""
    if spec.bootstrap_cache is None:
        return []
    return ["--bootstrap-cache"] if spec.bootstrap_cache else ["--no-bootstrap-cache"]


def _forward_logs_flag(spec) -> list[str]:
    """argparse BooleanOptionalAction flag for spec-level forward_logs."""
    if spec.forward_logs is None:
        return []
    return ["--forward-logs"] if spec.forward_logs else ["--no-forward-logs"]


def _rust_log_flag(spec) -> list[str]:
    """Spec-level --rust-log flag (whole-string value; '::' safe)."""
    return ["--rust-log", spec.rust_log] if spec.rust_log else []


def _memory_profiling_flag(spec) -> list[str]:
    """argparse BooleanOptionalAction flag for spec-level memory_profiling."""
    if getattr(spec, "memory_profiling", None) is None:
        return []
    return ["--memory-profiling"] if spec.memory_profiling else ["--no-memory-profiling"]


def _binary_debug_flag(binary) -> list[str]:
    """Informational ``--debug`` on the deploy step when the binary is a debug build.

    The deploy command itself only fetches the (already-built) binary; this flag
    is recorded into ``deploy_args`` so ``deployments slack-post`` can show that
    the binary is a debug build. The actual no-``--release`` build is driven by
    the separate ``build`` step's own ``--debug`` (see orchestrator/steps.py).
    """
    return ["--debug"] if getattr(binary, "debug", None) else []


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
    argv += _binary_debug_flag(spec.binary)
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
        argv += ["--clock-skew", spec.clock_skew]
    if spec.network is not None:
        argv += ["--network", format_network_token(spec.network)]
    argv += _bootstrap_cache_flag(spec)
    argv += _forward_logs_flag(spec)
    argv += _memory_profiling_flag(spec)
    argv += _rust_log_flag(spec)
    argv += ant_binary_flags(spec.binary)
    argv += _binary_debug_flag(spec.binary)
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
    if spec.provider is not None:
        argv += ["--provider", spec.provider]
    if spec.monitoring_provider is not None:
        argv += ["--monitoring-provider", spec.monitoring_provider]
    if spec.monitoring_region is not None:
        argv += ["--monitoring-region", spec.monitoring_region]
    if spec.monitoring_droplet_size is not None:
        argv += ["--monitoring-droplet-size", spec.monitoring_droplet_size]
    if spec.monitoring_volume_type is not None:
        argv += ["--monitoring-volume-type", spec.monitoring_volume_type]
    if spec.influx_retention_days is not None:
        argv += ["--influx-retention-days", str(spec.influx_retention_days)]
    if spec.log_retention_days is not None:
        argv += ["--log-retention-days", str(spec.log_retention_days)]
    if spec.clock_skew:
        argv += ["--clock-skew", spec.clock_skew]
    if spec.network is not None:
        argv += ["--network", format_network_token(spec.network)]
    argv += _bootstrap_cache_flag(spec)
    argv += _forward_logs_flag(spec)
    argv += _memory_profiling_flag(spec)
    argv += _rust_log_flag(spec)
    argv += ant_binary_flags(spec.binary)
    argv += _binary_debug_flag(spec.binary)
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
    if spec.pool is not None:
        argv += ["--pool", _format_pool_request(spec.pool)]
    else:
        argv += ["--urls-file", spec.urls_file]
    if spec.upload_sleep_secs is not None:
        argv += ["--upload-sleep-secs", str(spec.upload_sleep_secs)]
    if spec.probe_concurrency is not None:
        argv += ["--probe-concurrency", str(spec.probe_concurrency)]
    argv += _bootstrap_cache_flag(spec)
    argv += _forward_logs_flag(spec)
    argv += _rust_log_flag(spec)
    argv += ant_binary_flags(spec.binary)
    if spec.monitoring_volume_size is not None:
        argv += ["--monitoring-volume-size", str(spec.monitoring_volume_size)]
    argv += ["--ssh-key-path", manifest.ssh_key_path]
    return [Step(argv, "Deploy prod canary (upload + probes) + monitoring")]


def _local_uploads(manifest: Manifest) -> list[Step]:
    spec = manifest.prod_local_uploads
    argv = ["prod", "local-uploads", "--name", manifest.name, "--purpose", manifest.purpose]
    if spec.pool is not None:
        argv += ["--from-pool", _format_pool_request(spec.pool)]
    elif spec.urls is not None:
        argv += ["--urls", spec.urls]
    else:
        argv += ["--local-dir-path", spec.local_dir]
    if spec.payment is not None:
        argv += ["--payment", spec.payment]
    # log_level is the documented knob; --verbose is the deprecated alias and is
    # only emitted when log_level is unset (log_level wins if both are given).
    if spec.log_level is not None:
        argv += ["--log-level", spec.log_level]
    else:
        argv += ["--verbose"] * spec.verbose
    argv += _bootstrap_cache_flag(spec)
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
    argv += _bootstrap_cache_flag(spec)
    argv += ant_binary_flags(spec.binary)
    return [Step(argv, "Run prod downloads from the local machine")]


def _nodes(manifest: Manifest) -> list[Step]:
    spec = manifest.prod_nodes
    argv = [
        "prod",
        "nodes",
        "deploy",
        "--name",
        manifest.name,
        "--purpose",
        manifest.purpose,
        "--region",
        spec.region,
    ]
    if spec.provider is not None:
        argv += ["--provider", spec.provider]
    if spec.vm_size is not None:
        argv += ["--vm-size", spec.vm_size]
    if spec.vm_count is not None:
        argv += ["--vm-count", str(spec.vm_count)]
    if spec.node_count is not None:
        argv += ["--node-count", str(spec.node_count)]
    if spec.volume_size is not None:
        argv += ["--volume-size", str(spec.volume_size)]
    if spec.heaptrack is not None:
        argv += ["--heaptrack" if spec.heaptrack else "--no-heaptrack"]
    if spec.monitoring is not None:
        argv += ["--monitoring" if spec.monitoring else "--no-monitoring"]
    if spec.grafana is not None:
        argv += ["--grafana" if spec.grafana else "--no-grafana"]
    if spec.monitoring_volume_size is not None:
        argv += ["--monitoring-volume-size", str(spec.monitoring_volume_size)]
    if spec.log_retention_days is not None:
        argv += ["--log-retention-days", str(spec.log_retention_days)]
    if spec.rewards_address is not None:
        argv += ["--rewards-address", spec.rewards_address]
    if spec.log_level is not None:
        argv += ["--log-level", spec.log_level]
    if spec.port is not None:
        argv += ["--port", str(spec.port)]
    # Dedicated node-log Elasticsearch. Only emit flags when enabled so manifests
    # without an `elastic:` block keep byte-identical step argv (and `up` journal
    # hashes). The deploy command stands up the ES VM and wires the node→ES
    # forwarder; `build_destroy_steps` appends --keep-elastic from spec.keep_elastic.
    if spec.elastic.enabled:
        argv += ["--forward-logs"]
        if spec.elastic.volume_size is not None:
            argv += ["--elastic-volume-size", str(spec.elastic.volume_size)]
        if spec.elastic.retention_days is not None:
            argv += ["--elastic-retention-days", str(spec.elastic.retention_days)]
        if spec.elastic.droplet_size is not None:
            argv += ["--elastic-droplet-size", spec.elastic.droplet_size]
        if spec.elastic.shards is not None:
            argv += ["--elastic-shards", str(spec.elastic.shards)]
        if spec.elastic.drop_log_levels is not None:
            argv += ["--elastic-drop-log-levels", spec.elastic.drop_log_levels]
    argv += node_binary_flags(spec.binary)
    argv += _binary_debug_flag(spec.binary)
    argv += ["--ssh-key-path", manifest.ssh_key_path]
    return [Step(argv, "Deploy prod profiling nodes (+ optional monitoring)")]


def _bool_flag(value, flag: str) -> list[str]:
    """argparse BooleanOptionalAction flag emitted only when set (None omits)."""
    if value is None:
        return []
    return [flag] if value else [f"--no-{flag[2:]}"]


def _debug_nodes(manifest: Manifest) -> list[Step]:
    spec = manifest.prod_debug_nodes
    argv = [
        "prod",
        "debug-nodes",
        "deploy",
        "--name",
        manifest.name,
        "--purpose",
        manifest.purpose,
        "--region",
        spec.region,
    ]
    if spec.provider is not None:
        argv += ["--provider", spec.provider]
    if spec.vm_size is not None:
        argv += ["--vm-size", spec.vm_size]
    if spec.vm_count is not None:
        argv += ["--vm-count", str(spec.vm_count)]
    if spec.node_count is not None:
        argv += ["--node-count", str(spec.node_count)]
    if spec.volume_size is not None:
        argv += ["--volume-size", str(spec.volume_size)]
    if spec.log_level is not None:
        argv += ["--log-level", spec.log_level]
    if spec.rust_backtrace is not None:
        argv += ["--rust-backtrace", spec.rust_backtrace]
    argv += _bool_flag(spec.coredumps, "--coredumps")
    if spec.coredump_retention_days is not None:
        argv += ["--coredump-retention-days", str(spec.coredump_retention_days)]
    if spec.log_retention_days is not None:
        argv += ["--log-retention-days", str(spec.log_retention_days)]
    argv += _bool_flag(spec.heaptrack, "--heaptrack")
    argv += _bool_flag(spec.monitoring, "--monitoring")
    if spec.monitoring_provider is not None:
        argv += ["--monitoring-provider", spec.monitoring_provider]
    if spec.monitoring_region is not None:
        argv += ["--monitoring-region", spec.monitoring_region]
    if spec.monitoring_droplet_size is not None:
        argv += ["--monitoring-droplet-size", spec.monitoring_droplet_size]
    if spec.monitoring_volume_size is not None:
        argv += ["--monitoring-volume-size", str(spec.monitoring_volume_size)]
    argv += _bool_flag(spec.grafana, "--grafana")
    if spec.influx_retention_days is not None:
        argv += ["--influx-retention-days", str(spec.influx_retention_days)]
    argv += _bool_flag(spec.forward_logs, "--forward-logs")
    if spec.elastic_provider is not None:
        argv += ["--elastic-provider", spec.elastic_provider]
    if spec.elastic_region is not None:
        argv += ["--elastic-region", spec.elastic_region]
    if spec.elastic_droplet_size is not None:
        argv += ["--elastic-droplet-size", spec.elastic_droplet_size]
    if spec.elastic_volume_size is not None:
        argv += ["--elastic-volume-size", str(spec.elastic_volume_size)]
    if spec.elastic_retention_days is not None:
        argv += ["--elastic-retention-days", str(spec.elastic_retention_days)]
    if spec.elastic_shards is not None:
        argv += ["--elastic-shards", str(spec.elastic_shards)]
    if spec.elastic_drop_log_levels is not None:
        argv += ["--elastic-drop-log-levels", spec.elastic_drop_log_levels]
    if spec.rewards_address is not None:
        argv += ["--rewards-address", spec.rewards_address]
    if spec.port is not None:
        argv += ["--port", str(spec.port)]
    if spec.contabo_image_id is not None:
        argv += ["--contabo-image-id", spec.contabo_image_id]
    if spec.contabo_ssh_public_key_path is not None:
        argv += ["--contabo-ssh-public-key-path", spec.contabo_ssh_public_key_path]
    argv += node_binary_flags(spec.binary)
    argv += _binary_debug_flag(spec.binary)
    argv += ["--ssh-key-path", manifest.ssh_key_path]
    return [Step(argv, "Deploy prod debug nodes (+ dedicated InfluxDB + ES)")]


_DEPLOY_BUILDERS = {
    "prod-uploaders": _uploaders,
    "prod-downloaders": _downloaders,
    "prod-continuous-downloader": _continuous_downloader,
    "prod-canary": _canary,
    "prod-local-uploads": _local_uploads,
    "prod-local-downloads": _local_downloads,
    "prod-nodes": _nodes,
    "prod-debug-nodes": _debug_nodes,
}

# Destroy is a single command for the VM-backed kinds; local-* have no infra.
_DESTROY = {
    "prod-uploaders": ["prod", "uploaders", "destroy"],
    "prod-downloaders": ["prod", "downloaders", "destroy"],
    "prod-continuous-downloader": ["prod", "continuous-downloader", "destroy"],
    "prod-canary": ["prod", "canary", "destroy"],
    "prod-nodes": ["prod", "nodes", "destroy"],
    "prod-debug-nodes": ["prod", "debug-nodes", "destroy"],
}


def build_deploy_steps(manifest: Manifest, *, skip_builds: bool = False) -> list[Step]:
    try:
        builder = _DEPLOY_BUILDERS[manifest.kind]
    except KeyError:
        raise ValueError(f"no prod orchestrator for kind '{manifest.kind}'")
    # A branch-sourced binary must be built (and uploaded to the name-scoped
    # S3 key) before any prod command tries to resolve it — every kind,
    # including the local-* ones, fetches the custom build from S3. With
    # --skip-builds the operator asserts the build already exists in S3.
    steps: list[Step] = []
    if not skip_builds:
        if manifest.kind in ("prod-nodes", "prod-debug-nodes"):
            # These resolve an ANT-NODE branch build (not the client `ant`), so
            # they need a `build ant-node` step, not `build ant`.
            steps = build_steps_for_branches(
                manifest.name, manifest.ssh_key_path, manifest.spec.binary, None
            )
        else:
            steps = ant_build_steps(
                manifest.name, manifest.ssh_key_path, manifest.spec.binary
            )
    return steps + builder(manifest)


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
    if getattr(manifest.spec, "keep_elastic", None):
        argv.append("--keep-elastic")
    return [Step(argv, f"Destroy {manifest.kind}")]
