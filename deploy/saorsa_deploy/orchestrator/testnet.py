"""Translate a testnet (or testnet-extension) manifest into an ordered step list.

This is the single source of truth for the command-ordering and flag-coupling
invariants that previously lived as prose in the ``deploy-runbook`` skill:

- bootstraps infra → nodes infra → (elastic/monitoring/clients infra)
- (elastic/monitoring provision) → nodes provision-genesis →
  bootstraps provision (only when bootstrap count > 1) → nodes provision →
  nodes start → (telegraf) → (clients provision/start) → generate-inventory
- ``--testnet`` and ``--upgrade-channel`` go on all three node-provision commands
- destroy runs in reverse: clients → nodes → bootstraps → elastic → monitoring
"""

from __future__ import annotations

from saorsa_deploy.config.models import (
    Clients,
    Elastic,
    ExtensionSpec,
    Manifest,
    Monitoring,
    Nodes,
    TestnetSpec,
)
from saorsa_deploy.orchestrator.steps import (
    Step,
    ant_binary_flags,
    build_steps_for_branches,
    node_binary_flags,
)

# provider key -> (vm_count, regions, regions_list, size) flag names
_PROVIDER_FLAGS = {
    "digital_ocean": ("--do-vm-count", "--do-regions", "--do-regions-list", "--do-droplet-size"),
    "hetzner": (
        "--hetzner-vm-count",
        "--hetzner-regions",
        "--hetzner-regions-list",
        "--hetzner-server-type",
    ),
    "vultr": ("--vultr-vm-count", "--vultr-regions", "--vultr-regions-list", "--vultr-plan"),
}
_PROVIDER_ORDER = ["digital_ocean", "hetzner", "vultr"]


def _uploader_spec(u) -> str:
    parts = [u.provider, u.region, str(u.file_size_mb)]
    if u.vm_size:
        parts.append(u.vm_size)
    if u.payment:
        parts.append(f"payment={u.payment}")
    if u.ant is not None:
        parts.append(f"ant={u.ant}")
    if u.eth is not None:
        parts.append(f"eth={u.eth}")
    if u.clock_skew:
        parts.append("clock_skew=true")
    return ":".join(parts)


def _downloader_spec(d) -> str:
    parts = [d.provider, d.region]
    if d.vm_size:
        parts.append(d.vm_size)
    if d.clock_skew:
        parts.append("clock_skew=true")
    return ":".join(parts)


def _nodes_infra_argv(name: str, nodes: Nodes) -> list[str]:
    argv = ["nodes", "infra", "--name", name]
    for provider in _PROVIDER_ORDER:
        cfg = nodes.providers.get(provider)
        if cfg is None:
            continue
        count_flag, regions_flag, regions_list_flag, size_flag = _PROVIDER_FLAGS[provider]
        argv += [count_flag, str(cfg.vm_count)]
        if cfg.regions_list is not None:
            argv += [regions_list_flag, ",".join(cfg.regions_list)]
        elif cfg.regions is not None:
            argv += [regions_flag, str(cfg.regions)]
        if cfg.size:
            argv += [size_flag, cfg.size]
    if nodes.attached_volume_size is not None:
        argv += ["--attached-volume-size", str(nodes.attached_volume_size)]
    return argv


def _elastic_infra_argv(name: str, elastic: Elastic) -> list[str]:
    argv = ["elastic", "infra", "--name", name]
    if elastic.droplet_size is not None:
        argv += ["--droplet-size", elastic.droplet_size]
    if elastic.volume_size is not None:
        argv += ["--volume-size", str(elastic.volume_size)]
    return argv


def _clients_infra_argv(name: str, clients: Clients) -> list[str]:
    argv = ["clients", "infra", "--name", name]
    for u in clients.uploaders:
        argv += ["--uploader", _uploader_spec(u)]
    for d in clients.downloaders:
        argv += ["--downloader", _downloader_spec(d)]
    return argv


def _clients_destroy_argv(name: str, skip_drain_failures: bool) -> list[str]:
    argv = ["clients", "destroy", "--name", name, "--force"]
    if skip_drain_failures:
        argv.append("--skip-drain-failures")
    return argv


def _node_provision_common(binary, mode: bool, upgrade_channel, ssh_key) -> list[str]:
    """Flags shared by genesis / bootstraps / nodes provision."""
    argv = list(node_binary_flags(binary))
    if mode:
        argv.append("--testnet")
    if upgrade_channel:
        argv += ["--upgrade-channel", upgrade_channel]
    argv += ["--ssh-key-path", ssh_key]
    return argv


def _provision_and_start_steps(
    name: str,
    ssh_key: str,
    *,
    binaries,
    nodes: Nodes,
    mode: bool,
    upgrade_channel,
    bootstrap_count: int,
    bootstrap_port: int,
    elastic: Elastic,
    monitoring: Monitoring,
    clients: Clients | None,
    include_genesis: bool,
) -> list[Step]:
    """Shared provision→start→telegraf→clients→inventory tail (testnet + extension)."""
    steps: list[Step] = []

    if elastic.enabled:
        argv = ["elastic", "provision", "--name", name]
        if elastic.shards is not None:
            argv += ["--shards", str(elastic.shards)]
        if elastic.retention_days is not None:
            argv += ["--retention-days", str(elastic.retention_days)]
        argv += ["--ssh-key-path", ssh_key]
        steps.append(Step(argv, "Provision Elasticsearch"))
    if monitoring.enabled:
        steps.append(
            Step(
                ["monitoring", "provision", "--name", name, "--ssh-key-path", ssh_key],
                "Provision monitoring (InfluxDB + Grafana)",
            )
        )

    common = _node_provision_common(binaries.ant_node, mode, upgrade_channel, ssh_key)

    if include_genesis:
        steps.append(
            Step(
                [
                    "nodes",
                    "provision-genesis",
                    "--name",
                    name,
                    "--port",
                    str(bootstrap_port),
                    *common,
                ],
                "Provision genesis node",
            )
        )
        if bootstrap_count > 1:
            steps.append(
                Step(
                    [
                        "bootstraps",
                        "provision",
                        "--name",
                        name,
                        "--port",
                        str(bootstrap_port),
                        *common,
                    ],
                    "Provision bootstrap nodes",
                )
            )

    nodes_argv = ["nodes", "provision", "--name", name, "--node-count", str(nodes.per_vm), *common]
    if nodes.nat_percent is not None:
        # keep --nat-percent adjacent to other nodes-provision flags, before ssh key
        idx = nodes_argv.index("--ssh-key-path")
        nodes_argv[idx:idx] = ["--nat-percent", str(nodes.nat_percent)]
    if nodes.clock_skew is not None:
        # keep --clock-skew-count adjacent to other nodes-provision flags
        idx = nodes_argv.index("--ssh-key-path")
        nodes_argv[idx:idx] = ["--clock-skew-count", str(nodes.clock_skew)]
    steps.append(Step(nodes_argv, "Provision nodes"))

    steps.append(Step(["nodes", "start", "--name", name, "--ssh-key-path", ssh_key], "Start nodes"))

    if elastic.enabled:
        argv = ["elastic", "provision-telegraf", "--name", name]
        if elastic.drop_log_levels is not None:
            argv += ["--drop-log-levels", elastic.drop_log_levels]
        if elastic.buffer_limit is not None:
            argv += ["--buffer-limit", str(elastic.buffer_limit)]
        argv += ["--ssh-key-path", ssh_key]
        steps.append(Step(argv, "Install Telegraf log forwarding (Elasticsearch)"))
    if monitoring.enabled:
        steps.append(
            Step(
                ["monitoring", "provision-telegraf", "--name", name, "--ssh-key-path", ssh_key],
                "Install Telegraf host metrics (InfluxDB)",
            )
        )

    if clients is not None:
        argv = ["clients", "provision", "--name", name, *ant_binary_flags(binaries.ant)]
        if monitoring.enabled:
            argv += ["--monitoring-name", name]
        argv += ["--ssh-key-path", ssh_key]
        steps.append(Step(argv, "Provision clients"))
        steps.append(
            Step(["clients", "start", "--name", name, "--ssh-key-path", ssh_key], "Start clients")
        )

    steps.append(
        Step(
            ["deployments", "generate-inventory", "--name", name, "--ssh-key-path", ssh_key],
            "Generate inventory snapshot",
        )
    )
    return steps


def build_testnet_deploy_steps(manifest: Manifest) -> list[Step]:
    spec: TestnetSpec = manifest.testnet
    name = manifest.name
    ssh_key = manifest.ssh_key_path

    steps: list[Step] = []
    steps += build_steps_for_branches(name, ssh_key, spec.binaries.ant_node, spec.binaries.ant)

    # --- infra ---
    boot_argv = [
        "bootstraps",
        "infra",
        "--name",
        name,
        "--bootstrap-count",
        str(spec.bootstraps.count),
    ]
    if spec.bootstraps.regions_list is not None:
        boot_argv += ["--do-regions-list", ",".join(spec.bootstraps.regions_list)]
    if spec.bootstraps.attached_volume_size is not None:
        boot_argv += ["--attached-volume-size", str(spec.bootstraps.attached_volume_size)]
    boot_argv += ["--purpose", manifest.purpose]
    steps.append(Step(boot_argv, "Create bootstrap infrastructure"))

    steps.append(Step(_nodes_infra_argv(name, spec.nodes), "Create node infrastructure"))

    if spec.elastic.enabled:
        steps.append(
            Step(_elastic_infra_argv(name, spec.elastic), "Create Elasticsearch infrastructure")
        )
    if spec.monitoring.enabled:
        steps.append(
            Step(["monitoring", "infra", "--name", name], "Create monitoring infrastructure")
        )
    if spec.clients is not None:
        steps.append(Step(_clients_infra_argv(name, spec.clients), "Create client infrastructure"))

    # --- provision + start tail ---
    steps += _provision_and_start_steps(
        name,
        ssh_key,
        binaries=spec.binaries,
        nodes=spec.nodes,
        mode=spec.mode,
        upgrade_channel=spec.upgrade_channel,
        bootstrap_count=spec.bootstraps.count,
        bootstrap_port=spec.bootstraps.port,
        elastic=spec.elastic,
        monitoring=spec.monitoring,
        clients=spec.clients,
        include_genesis=True,
    )
    return steps


def build_testnet_destroy_steps(
    manifest: Manifest, *, skip_drain_failures: bool = False
) -> list[Step]:
    spec: TestnetSpec = manifest.testnet
    name = manifest.name
    steps: list[Step] = []
    if spec.clients is not None:
        steps.append(
            Step(
                _clients_destroy_argv(name, skip_drain_failures),
                "Destroy clients",
            )
        )
    steps.append(Step(["nodes", "destroy", "--name", name, "--force"], "Destroy nodes"))
    steps.append(Step(["bootstraps", "destroy", "--name", name, "--force"], "Destroy bootstraps"))
    if spec.elastic.enabled:
        steps.append(
            Step(["elastic", "destroy", "--name", name, "--force"], "Destroy Elasticsearch")
        )
    if spec.monitoring.enabled:
        steps.append(
            Step(["monitoring", "destroy", "--name", name, "--force"], "Destroy monitoring")
        )
    return steps


# --------------------------------------------------------------------------- #
# extension (reuses another testnet's bootstraps; no bootstrap/genesis tier)
# --------------------------------------------------------------------------- #
def build_extension_deploy_steps(manifest: Manifest) -> list[Step]:
    spec: ExtensionSpec = manifest.extension
    name = manifest.name
    ssh_key = manifest.ssh_key_path

    steps: list[Step] = []
    steps += build_steps_for_branches(name, ssh_key, spec.binaries.ant_node, spec.binaries.ant)

    steps.append(
        Step(
            [
                "deployments",
                "extend",
                "--name",
                name,
                "--source",
                spec.source,
                "--purpose",
                manifest.purpose,
            ],
            f"Seed bootstraps from {spec.source}",
        )
    )
    steps.append(Step(_nodes_infra_argv(name, spec.nodes), "Create node infrastructure"))
    if spec.elastic.enabled:
        steps.append(
            Step(_elastic_infra_argv(name, spec.elastic), "Create Elasticsearch infrastructure")
        )
    if spec.monitoring.enabled:
        steps.append(
            Step(["monitoring", "infra", "--name", name], "Create monitoring infrastructure")
        )
    if spec.clients is not None:
        steps.append(Step(_clients_infra_argv(name, spec.clients), "Create client infrastructure"))

    steps += _provision_and_start_steps(
        name,
        ssh_key,
        binaries=spec.binaries,
        nodes=spec.nodes,
        mode=spec.mode,
        upgrade_channel=spec.upgrade_channel,
        bootstrap_count=1,  # not used (genesis skipped)
        bootstrap_port=10000,
        elastic=spec.elastic,
        monitoring=spec.monitoring,
        clients=spec.clients,
        include_genesis=False,  # extension peers with the source's bootstraps
    )
    return steps


def build_extension_destroy_steps(
    manifest: Manifest, *, skip_drain_failures: bool = False
) -> list[Step]:
    spec: ExtensionSpec = manifest.extension
    name = manifest.name
    steps: list[Step] = []
    if spec.clients is not None:
        steps.append(
            Step(
                _clients_destroy_argv(name, skip_drain_failures),
                "Destroy clients",
            )
        )
    steps.append(Step(["nodes", "destroy", "--name", name, "--force"], "Destroy nodes"))
    if spec.elastic.enabled:
        steps.append(
            Step(["elastic", "destroy", "--name", name, "--force"], "Destroy Elasticsearch")
        )
    if spec.monitoring.enabled:
        steps.append(
            Step(["monitoring", "destroy", "--name", name, "--force"], "Destroy monitoring")
        )
    steps.append(
        Step(["deployments", "unextend", "--name", name, "--force"], "Remove extension state")
    )
    return steps
