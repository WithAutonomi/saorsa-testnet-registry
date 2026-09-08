"""Translate a testnet (or testnet-extension) manifest into an ordered step list.

This is the single source of truth for the command-ordering and flag-coupling
invariants that previously lived as prose in the ``deploy-runbook`` skill:

- the five infra steps form one parallel group (``group="infra"``): bootstraps
  infra → nodes infra run sequentially in a single lane (they share the
  deployment-state object), while elastic / monitoring / clients infra each run
  as their own concurrent lane. ``iter_units`` collapses the group and
  ``parallel_infra`` executes the lanes at once; on resume the whole idempotent
  group is one journal unit.
- (elastic/monitoring provision) → nodes provision-genesis →
  bootstraps provision (only when bootstrap count > 1) → nodes provision →
  nodes start → (telegraf) → (clients provision/start) → generate-inventory
- ``--testnet`` and ``--upgrade-channel`` go on all three node-provision commands
- destroy runs in reverse: clients → nodes → bootstraps → elastic → monitoring,
  except that an ``elastic``/``monitoring`` block with ``keep_on_destroy: true`` is
  left standing so the run's logs/metrics survive the teardown for a post-mortem
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
from saorsa_deploy.provisioning.network_profile import format_network_token

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
    "ovh": ("--ovh-vm-count", "--ovh-regions", "--ovh-regions-list", "--ovh-flavor"),
    "ovh_3az": (
        "--ovh-3az-vm-count",
        "--ovh-3az-regions",
        "--ovh-3az-regions-list",
        "--ovh-3az-flavor",
    ),
}
_PROVIDER_ORDER = ["digital_ocean", "hetzner", "vultr", "ovh", "ovh_3az"]


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
        parts.append(f"clock_skew={u.clock_skew}")
    if u.bootstrap_cache is not None:
        parts.append(f"bootstrap_cache={str(u.bootstrap_cache).lower()}")
    if u.forward_logs is not None:
        parts.append(f"forward_logs={str(u.forward_logs).lower()}")
    if u.network is not None:
        parts.append(f"network={format_network_token(u.network)}")
    # rust_log MUST be last — its value may contain ':' (RUST_LOG '::' paths).
    if u.rust_log is not None:
        parts.append(f"rust_log={u.rust_log}")
    return ":".join(parts)


def _downloader_spec(d) -> str:
    parts = [d.provider, d.region]
    if d.vm_size:
        parts.append(d.vm_size)
    if d.clock_skew:
        parts.append(f"clock_skew={d.clock_skew}")
    if d.bootstrap_cache is not None:
        parts.append(f"bootstrap_cache={str(d.bootstrap_cache).lower()}")
    if d.forward_logs is not None:
        parts.append(f"forward_logs={str(d.forward_logs).lower()}")
    if d.network is not None:
        parts.append(f"network={format_network_token(d.network)}")
    # rust_log MUST be last — its value may contain ':' (RUST_LOG '::' paths).
    if d.rust_log is not None:
        parts.append(f"rust_log={d.rust_log}")
    return ":".join(parts)


def _wasm_uploader_spec(u) -> str:
    """Token string for a WASM uploader. No bootstrap_cache: see WasmUploader."""
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
        parts.append(f"clock_skew={u.clock_skew}")
    if u.forward_logs is not None:
        parts.append(f"forward_logs={str(u.forward_logs).lower()}")
    if u.network is not None:
        parts.append(f"network={format_network_token(u.network)}")
    # rust_log MUST be last — its value may contain ':' (RUST_LOG '::' paths).
    if u.rust_log is not None:
        parts.append(f"rust_log={u.rust_log}")
    return ":".join(parts)


def _wasm_downloader_spec(d) -> str:
    """Token string for a WASM downloader. See :func:`_wasm_uploader_spec`."""
    parts = [d.provider, d.region]
    if d.vm_size:
        parts.append(d.vm_size)
    if d.clock_skew:
        parts.append(f"clock_skew={d.clock_skew}")
    if d.forward_logs is not None:
        parts.append(f"forward_logs={str(d.forward_logs).lower()}")
    if d.network is not None:
        parts.append(f"network={format_network_token(d.network)}")
    if d.rust_log is not None:
        parts.append(f"rust_log={d.rust_log}")
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
        # Attached-volume size is per-provider; ovh_3az never has one (no block
        # storage — validated in the config model). digital_ocean's CLI prefix
        # is "do".
        if cfg.attached_volume_size is not None:
            vol_prefix = "do" if provider == "digital_ocean" else provider
            argv += [f"--{vol_prefix}-attached-volume-size", str(cfg.attached_volume_size)]
    return argv


def _resolve_elastic_drop_log_levels(elastic: Elastic, log_level) -> str | None:
    """Resolve the ``--drop-log-levels`` value for the node→ES forwarder.

    The node Telegraf forwarder drops any log line whose level is in
    ``--drop-log-levels`` (CLI default ``DEBUG,TRACE``). Left alone that silently
    contradicts a manifest that asked for verbose node logs via
    ``nodes.log_level``: ant-node writes trace/debug to disk but the forwarder
    strips it before Elasticsearch, so only INFO/WARN land in ES.

    So derive the drop set from ``nodes.log_level`` — verbosity ordering is
    TRACE > DEBUG > INFO, and we drop only the levels strictly MORE verbose than
    the configured one (i.e. levels ant-node never even emits at that setting):

    - ``trace``      -> ``""``      (drop nothing; forward everything)
    - ``debug``      -> ``"TRACE"`` (keep DEBUG, drop the noisier TRACE)
    - ``info``/unset -> ``None``    (omit the flag; CLI default ``DEBUG,TRACE``)

    An explicit ``elastic.drop_log_levels`` always wins — the escape hatch for
    "keep verbose logs on disk but out of ES" (set it to ``"DEBUG,TRACE"``) or
    "force-keep everything regardless of node level" (set it to ``""``).

    Returning ``None`` for the info/unset case keeps the generated step argv
    byte-identical to before this change, so existing manifests' ``up`` journal
    and plan hashes still match (no spurious re-runs / resume mismatches).
    """
    if elastic.drop_log_levels is not None:
        return elastic.drop_log_levels
    level = (log_level or "").strip().lower()
    if level == "trace":
        return ""
    if level == "debug":
        return "TRACE"
    return None


def _elastic_infra_argv(name: str, elastic: Elastic) -> list[str]:
    argv = ["elastic", "infra", "--name", name]
    if elastic.droplet_size is not None:
        argv += ["--droplet-size", elastic.droplet_size]
    if elastic.volume_size is not None:
        argv += ["--volume-size", str(elastic.volume_size)]
    # Record the keep-through-teardown intent in the elastic S3 state at create
    # time, so a name-only `down` (which has no manifest to consult) honours it
    # too. Only appended when set, so existing manifests' argv stays identical.
    if elastic.keep_on_destroy:
        argv += ["--keep-on-destroy"]
    return argv


def _monitoring_infra_argv(name: str, monitoring: Monitoring) -> list[str]:
    argv = ["monitoring", "infra", "--name", name]
    # Grafana is opt-in; only append the flag when requested so existing
    # manifests' step argv (and `up` journal hashes) stay byte-identical.
    if monitoring.grafana:
        argv += ["--grafana"]
    # See _elastic_infra_argv — persists the intent for the name-only `down`.
    if monitoring.keep_on_destroy:
        argv += ["--keep-on-destroy"]
    return argv


def _clients_infra_argv(name: str, clients: Clients) -> list[str]:
    argv = ["clients", "infra", "--name", name]
    for u in clients.uploaders:
        argv += ["--uploader", _uploader_spec(u)]
    for d in clients.downloaders:
        argv += ["--downloader", _downloader_spec(d)]
    for u in clients.wasm_uploaders:
        argv += ["--wasm-uploader", _wasm_uploader_spec(u)]
    for d in clients.wasm_downloaders:
        argv += ["--wasm-downloader", _wasm_downloader_spec(d)]
    return argv


def _observability_destroy_steps(
    name: str, elastic: Elastic, monitoring: Monitoring
) -> list[Step]:
    """Elastic / monitoring teardown steps, honouring ``keep_on_destroy``.

    A kept stack is simply never destroyed: the testnet tiers go away and its
    logs/metrics stay queryable for a post-mortem on the run. Cleaning it up
    afterwards is a deliberate ``elastic destroy`` / ``monitoring destroy`` — those
    primitives stay unconditional precisely because they are the cleanup route.

    Shared by the testnet and extension destroy builders so the two kinds cannot
    drift apart on this.
    """
    steps: list[Step] = []
    if elastic.enabled and not elastic.keep_on_destroy:
        steps.append(
            Step(["elastic", "destroy", "--name", name, "--force"], "Destroy Elasticsearch")
        )
    if monitoring.enabled and not monitoring.keep_on_destroy:
        steps.append(
            Step(["monitoring", "destroy", "--name", name, "--force"], "Destroy monitoring")
        )
    return steps


def _clients_destroy_argv(
    name: str, ssh_key: str, skip_drain_failures: bool
) -> list[str]:
    argv = ["clients", "destroy", "--name", name, "--force", "--ssh-key-path", ssh_key]
    if skip_drain_failures:
        argv.append("--skip-drain-failures")
    return argv


def _node_provision_common(
    binary, mode: bool, upgrade_channel, ssh_key, log_level=None
) -> list[str]:
    """Flags shared by genesis / bootstraps / nodes provision."""
    argv = list(node_binary_flags(binary))
    if mode:
        argv.append("--testnet")
    if upgrade_channel:
        argv += ["--upgrade-channel", upgrade_channel]
    # A single ant-node --log-level applies uniformly to every node-tier service
    # (genesis, bootstraps, regular nodes), so it rides the shared common flags.
    if log_level:
        argv += ["--log-level", log_level]
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
    bootstrap_webrtc_port: int | None = None,
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

    common = _node_provision_common(
        binaries.ant_node, mode, upgrade_channel, ssh_key, nodes.log_level
    )

    # Fold Telegraf install into the node-tier provision passes (genesis,
    # bootstraps, WAN nodes) instead of a separate pass: each provision command
    # installs the forwarders the deployment has state for in its own SSH pass.
    # Because the install rides the existing pass and happens before `nodes
    # start`, host metrics still cover the node lifetime from the first second.
    if elastic.enabled or monitoring.enabled:
        common = [*common, "--with-telegraf"]
        if elastic.enabled:
            # Derive the node forwarder's drop set from nodes.log_level so that
            # `log_level: trace`/`debug` actually reaches ES (an explicit
            # elastic.drop_log_levels still overrides). See helper docstring.
            drop = _resolve_elastic_drop_log_levels(elastic, nodes.log_level)
            if drop is not None:
                common += ["--drop-log-levels", drop]
            if elastic.buffer_limit is not None:
                common += ["--buffer-limit", str(elastic.buffer_limit)]

    # A pinned WebRTC port rides both bootstrap-tier provisions and nothing else:
    # only bootstraps need a browser address that never moves. `*common` is
    # splatted last, so the flag is built into the list rather than inserted.
    webrtc_flags: list[str] = []
    if bootstrap_webrtc_port is not None:
        webrtc_flags = ["--webrtc-port", str(bootstrap_webrtc_port)]

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
                    *webrtc_flags,
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
                        *webrtc_flags,
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
    if nodes.full_percent is not None:
        # keep --full-percent adjacent to other nodes-provision flags
        idx = nodes_argv.index("--ssh-key-path")
        nodes_argv[idx:idx] = ["--full-percent", str(nodes.full_percent)]
    if nodes.full_headroom_mb is not None:
        idx = nodes_argv.index("--ssh-key-path")
        nodes_argv[idx:idx] = ["--full-headroom-mb", str(nodes.full_headroom_mb)]
    steps.append(Step(nodes_argv, "Provision nodes"))

    # Telegraf was installed during the provision passes above (via
    # --with-telegraf), so there is no separate telegraf step. Client host
    # metrics are installed by `clients provision` below.
    steps.append(Step(["nodes", "start", "--name", name, "--ssh-key-path", ssh_key], "Start nodes"))

    if clients is not None and clients.has_wasm:
        # Collect the WebRTC-Direct bootstrap addresses before the clients are
        # provisioned: a WASM client bootstraps from them and has no other way into
        # the network. It runs here, after `nodes start`, rather than at
        # bootstrap-provision time, because the address embeds the node's DTLS
        # certificate hash — which exists only once a node has actually run.
        steps.append(
            Step(
                [
                    "deployments",
                    "webrtc-addresses",
                    "--name",
                    name,
                    "--ssh-key-path",
                    ssh_key,
                ],
                "Collect WebRTC bootstrap addresses",
            )
        )

    if clients is not None:
        argv = ["clients", "provision", "--name", name]
        if clients.uploaders or clients.downloaders:
            argv += list(ant_binary_flags(binaries.ant))
        if clients.has_wasm and binaries.ant_wasm is not None:
            argv += [
                "--wasm-branch-name",
                binaries.ant_wasm.branch,
                "--wasm-repo-owner",
                binaries.ant_wasm.repo_owner or "maidsafe",
            ]
        if monitoring.enabled:
            argv += ["--monitoring-name", name]
        # Scale the client→ES log forwarder's buffer with the same knob the node
        # forwarder uses, so a high-volume trace uploader doesn't overflow the
        # client forwarder's small default buffer and silently drop lines.
        if elastic.buffer_limit is not None:
            argv += ["--forward-buffer-limit", str(elastic.buffer_limit)]
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


def build_testnet_deploy_steps(
    manifest: Manifest, *, skip_builds: bool = False
) -> list[Step]:
    spec: TestnetSpec = manifest.testnet
    name = manifest.name
    ssh_key = manifest.ssh_key_path

    steps: list[Step] = []
    if not skip_builds:
        steps += build_steps_for_branches(
            name,
            ssh_key,
            spec.binaries.ant_node,
            spec.binaries.ant,
            spec.binaries.ant_wasm,
        )

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
    # All five infra steps form one parallel group: bootstraps→nodes share the
    # deployment-state object (and nodes infra requires bootstrap state), so they
    # run sequentially in a single lane; elastic/monitoring/clients each own an
    # independent S3 state key and run as their own concurrent lanes.
    steps.append(
        Step(boot_argv, "Create bootstrap infrastructure", group="infra", lane="bootstraps-nodes")
    )
    steps.append(
        Step(
            _nodes_infra_argv(name, spec.nodes),
            "Create node infrastructure",
            group="infra",
            lane="bootstraps-nodes",
        )
    )
    if spec.elastic.enabled:
        steps.append(
            Step(
                _elastic_infra_argv(name, spec.elastic),
                "Create Elasticsearch infrastructure",
                group="infra",
                lane="elastic",
            )
        )
    if spec.monitoring.enabled:
        steps.append(
            Step(
                _monitoring_infra_argv(name, spec.monitoring),
                "Create monitoring infrastructure",
                group="infra",
                lane="monitoring",
            )
        )
    if spec.clients is not None:
        steps.append(
            Step(
                _clients_infra_argv(name, spec.clients),
                "Create client infrastructure",
                group="infra",
                lane="clients",
            )
        )

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
        bootstrap_webrtc_port=spec.bootstraps.webrtc_port,
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
                _clients_destroy_argv(
                    name, manifest.ssh_key_path, skip_drain_failures
                ),
                "Destroy clients",
            )
        )
    steps.append(Step(["nodes", "destroy", "--name", name, "--force"], "Destroy nodes"))
    steps.append(Step(["bootstraps", "destroy", "--name", name, "--force"], "Destroy bootstraps"))
    steps += _observability_destroy_steps(name, spec.elastic, spec.monitoring)
    return steps


# --------------------------------------------------------------------------- #
# extension (reuses another testnet's bootstraps; no bootstrap/genesis tier)
# --------------------------------------------------------------------------- #
def build_extension_deploy_steps(
    manifest: Manifest, *, skip_builds: bool = False
) -> list[Step]:
    spec: ExtensionSpec = manifest.extension
    name = manifest.name
    ssh_key = manifest.ssh_key_path

    steps: list[Step] = []
    if not skip_builds:
        steps += build_steps_for_branches(
            name,
            ssh_key,
            spec.binaries.ant_node,
            spec.binaries.ant,
            spec.binaries.ant_wasm,
        )

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
    # Infra parallel group. The bootstrap tier is the source testnet's (seeded by
    # the sequential `deployments extend` step above), so node infra has no
    # bootstrap-creation lane to wait on — it runs as its own lane alongside the
    # independent elastic/monitoring/clients lanes.
    steps.append(
        Step(
            _nodes_infra_argv(name, spec.nodes),
            "Create node infrastructure",
            group="infra",
            lane="nodes",
        )
    )
    if spec.elastic.enabled:
        steps.append(
            Step(
                _elastic_infra_argv(name, spec.elastic),
                "Create Elasticsearch infrastructure",
                group="infra",
                lane="elastic",
            )
        )
    if spec.monitoring.enabled:
        steps.append(
            Step(
                _monitoring_infra_argv(name, spec.monitoring),
                "Create monitoring infrastructure",
                group="infra",
                lane="monitoring",
            )
        )
    if spec.clients is not None:
        steps.append(
            Step(
                _clients_infra_argv(name, spec.clients),
                "Create client infrastructure",
                group="infra",
                lane="clients",
            )
        )

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
                _clients_destroy_argv(
                    name, manifest.ssh_key_path, skip_drain_failures
                ),
                "Destroy clients",
            )
        )
    steps.append(Step(["nodes", "destroy", "--name", name, "--force"], "Destroy nodes"))
    steps += _observability_destroy_steps(name, spec.elastic, spec.monitoring)
    steps.append(
        Step(["deployments", "unextend", "--name", name, "--force"], "Remove extension state")
    )
    return steps
