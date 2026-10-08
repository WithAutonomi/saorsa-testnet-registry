"""Higher-level orchestration of a prod canary deployment.

A prod canary deployment is one wallet-bearing upload VM plus one or more
wallet-free probe VMs, all paired with a dedicated InfluxDB+Grafana
monitoring stack. The upload VM walks a URL list once (uploading each file
to the prod Autonomi network and recording a verification probe); the probe
VMs run forever, hourly probing every canary they own (per a static blake3
hash partition).

``prod canary destroy`` tears the monitoring stack down by default;
the persisted dataset lives in Postgres (via ``record-results``), so the
on-VM Influx + Grafana are treated as ephemeral. Pass
``--keep-monitoring`` if you want them to survive a teardown.

Pattern mirrors :mod:`saorsa_deploy.cmd.prod_uploaders`. Upload spec is
provisioned as a single ``--uploader`` slot in clients infra; probe specs
are expanded into N ``--downloader`` slots (one per probe VM). The roles
in ``client_state`` are then partitioned back into upload / probe sets.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console
from saorsa_db.models import DeploymentStatus

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.cmd.prod_uploaders import (
    DEFAULT_MONITORING_VOLUME_SIZE,
    MONITORING_REGION,
    _resolve_ant_binary,
    _resolve_secret_key,
)
from saorsa_deploy.prod_bootstraps import (
    PROD_BOOTSTRAPS_URL,
    ProdBootstrapsError,
    fetch_prod_bootstraps,
)
from saorsa_deploy.provisioning.prod_canary import (
    PROBE_SERVICE_NAME,
    UPLOAD_SERVICE_NAME,
    ProdCanaryProbeProvisioner,
    ProdCanaryUploadProvisioner,
)
from saorsa_deploy.registry import (
    record_deployment_created,
    record_event,
    record_status,
)
from saorsa_deploy.ssh import clear_known_hosts, wait_for_ssh
from saorsa_deploy.state import load_client_state, load_monitoring_state

DEFAULT_PROBE_CONCURRENCY = 3
# Upload VM stays small: it uploads sequentially and runs one verification
# download at a time.
DEFAULT_VM_SIZE = "s-1vcpu-2gb"
# Probe VMs need headroom: each `ant file download` of a ~500 MB file peaks
# at ~720 MB RSS (measured), and probes run several concurrently. A
# 1-vCPU/2 GB box OOM-kills itself at concurrency >1 with large files, which
# surfaces as bogus timeout-driven "loss". s-2vcpu-4gb fits 3 concurrent
# 500 MB downloads with headroom.
DEFAULT_PROBE_VM_SIZE = "s-2vcpu-4gb"


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


def parse_upload_spec(spec: str) -> dict:
    """Parse the ``--upload-spec`` value: ``REGION[:VM_SIZE]``."""
    parts = spec.split(":")
    region = parts[0]
    if not region:
        raise ValueError(f"Invalid upload spec '{spec}': region cannot be empty.")
    if len(parts) > 2:
        raise ValueError(f"Invalid upload spec '{spec}'. Expected REGION[:VM_SIZE].")
    vm_size = parts[1] if len(parts) == 2 and parts[1] else DEFAULT_VM_SIZE
    return {"region": region, "vm_size": vm_size}


def parse_probe_spec(spec: str) -> dict:
    """Parse a ``--probe-spec`` value: ``REGION[:VM_SIZE]:count=N``.

    ``count`` is required; ``vm_size`` is optional and defaults to
    ``DEFAULT_PROBE_VM_SIZE``. Returns ``{region, vm_size, count}``.
    """
    parts = spec.split(":")
    if len(parts) < 2:
        raise ValueError(f"Invalid probe spec '{spec}'. Expected REGION[:VM_SIZE]:count=N.")
    region = parts[0]
    if not region:
        raise ValueError(f"Invalid probe spec '{spec}': region cannot be empty.")

    options: dict[str, str] = {}
    vm_size = DEFAULT_PROBE_VM_SIZE
    for token in parts[1:]:
        if "=" in token:
            key, val = token.split("=", 1)
            if key in options:
                raise ValueError(f"Invalid probe spec '{spec}': duplicate key '{key}'.")
            options[key] = val
        else:
            if vm_size != DEFAULT_PROBE_VM_SIZE:
                raise ValueError(f"Invalid probe spec '{spec}': vm_size already set.")
            if not token:
                raise ValueError(f"Invalid probe spec '{spec}': vm_size cannot be empty.")
            vm_size = token

    if "count" not in options:
        raise ValueError(f"Invalid probe spec '{spec}': count=N is required.")
    try:
        count = int(options["count"])
    except ValueError as e:
        raise ValueError(f"Invalid probe spec '{spec}': count must be an integer.") from e
    if count < 1:
        raise ValueError(f"Invalid probe spec '{spec}': count must be >= 1.")

    extras = set(options) - {"count"}
    if extras:
        raise ValueError(f"Invalid probe spec '{spec}': unknown keys {sorted(extras)}.")

    return {"region": region, "vm_size": vm_size, "count": count}


def _read_urls_file(path: str) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Cannot read --urls-file {path}: {e}") from e
    has = any(line.strip() and not line.strip().startswith("#") for line in content.splitlines())
    if not has:
        raise ValueError(f"--urls-file {path} contains no URLs.")
    return content


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _monitoring_stack_exists(name: str) -> bool:
    try:
        load_monitoring_state(name)
        return True
    except RuntimeError:
        return False


def _monitoring_has_influx(name: str) -> bool:
    """True only when monitoring state carries usable InfluxDB creds.

    A monitoring VM whose ``monitoring provision`` never finished leaves state
    with a ``vm_ip`` but no ``influxdb_url``/``influxdb_token``; that must be
    re-provisioned, not skipped.
    """
    try:
        state = load_monitoring_state(name)
    except RuntimeError:
        return False
    return bool(state.get("influxdb_url") and state.get("influxdb_token"))


def _client_stack_exists(name: str) -> bool:
    try:
        state = load_client_state(name)
        return bool(state.get("clients"))
    except RuntimeError:
        return False


def _ensure_monitoring(args, console: Console) -> None:
    from saorsa_deploy.cmd.monitoring_infra import cmd_monitoring_infra
    from saorsa_deploy.cmd.provision_monitoring import cmd_provision_monitoring

    if _monitoring_stack_exists(args.name):
        if _monitoring_has_influx(args.name):
            console.print(
                f"[dim]Monitoring stack '{args.name}' already exists — skipping "
                "monitoring infra / monitoring provision.[/dim]"
            )
            return
        console.print(
            f"[yellow]Monitoring VM for '{args.name}' exists but has no "
            "InfluxDB creds (a prior provision didn't finish) — "
            "re-provisioning.[/yellow]"
        )
        cmd_provision_monitoring(
            SimpleNamespace(
                name=args.name,
                ssh_key_path=args.ssh_key_path,
            )
        )
        return

    console.print("[bold cyan]Step 1/5: creating monitoring VM[/bold cyan]")
    cmd_monitoring_infra(
        SimpleNamespace(
            name=args.name,
            region=MONITORING_REGION,
            volume_size=args.monitoring_volume_size,
            # A single client VM forwards little; keep the InfluxDB box small
            # rather than the larger testnet default.
            droplet_size="s-2vcpu-4gb",
            force=False,
            grafana=getattr(args, "grafana", False),
        )
    )

    console.print("[bold cyan]Step 2/5: provisioning InfluxDB + Grafana[/bold cyan]")
    cmd_provision_monitoring(
        SimpleNamespace(
            name=args.name,
            ssh_key_path=args.ssh_key_path,
        )
    )


def _ensure_client_infra(
    args,
    upload_spec: dict,
    probe_specs: list[dict],
    console: Console,
) -> None:
    """Create one upload VM (--uploader slot) and N probe VMs (--downloader
    slots, one per probe count). The role label is reused as the partitioning
    key when reading client_state back."""
    from saorsa_deploy.cmd.infra_clients import cmd_infra_clients

    if _client_stack_exists(args.name):
        console.print(
            f"[dim]Client stack '{args.name}' already exists — skipping clients infra.[/dim]"
        )
        return

    # prod canary VMs are Digital Ocean only (the upload/probe specs carry no
    # provider field), so synthesise client specs with the digital_ocean
    # provider that parse_uploader_spec / parse_downloader_spec now require.
    # bootstrap_cache is carried through so client_state records the real
    # per-VM cache mode (the print/slack surfaces read it from there).
    cache_token = f"bootstrap_cache={str(getattr(args, 'bootstrap_cache', True)).lower()}"
    synth_uploader = [
        f"digital_ocean:{upload_spec['region']}:0:{upload_spec['vm_size']}:{cache_token}"
    ]
    synth_downloaders: list[str] = []
    for ps in probe_specs:
        for _ in range(ps["count"]):
            synth_downloaders.append(f"digital_ocean:{ps['region']}:{ps['vm_size']}:{cache_token}")

    console.print(
        f"[bold cyan]Step 3/5: creating 1 upload VM + "
        f"{len(synth_downloaders)} probe VM(s)[/bold cyan]"
    )
    cmd_infra_clients(
        SimpleNamespace(
            name=args.name,
            uploader=synth_uploader,
            downloader=synth_downloaders,
            force=False,
        )
    )


def _load_monitoring_creds(args, console: Console) -> dict:
    state = load_monitoring_state(args.name)
    influx_url = state.get("influxdb_url", "")
    influx_token = state.get("influxdb_token", "")
    if not (influx_url and influx_token):
        console.print(
            "[bold red]Error:[/bold red] monitoring state has no Influx "
            "URL/token — run monitoring provision first."
        )
        sys.exit(1)
    return {
        "influx_url": influx_url,
        "influx_token": influx_token,
        "influx_org": state.get("influxdb_org", "ant"),
        "influx_bucket": state.get("influxdb_bucket", "testnet-metrics"),
    }


def _partition_clients(name: str, console: Console) -> tuple[dict, list[dict]]:
    """Pull client_state and split it into (upload_client, probe_clients).

    Upload role is the single ``--uploader`` slot; probes are the
    ``--downloader`` slots in client_index order.
    """
    state = load_client_state(name)
    clients = state.get("clients", [])

    uploads = [c for c in clients if c.get("ip") and c.get("role", "upload") == "upload"]
    probes = [c for c in clients if c.get("ip") and c.get("role") == "download"]

    if len(uploads) != 1:
        console.print(
            f"[bold red]Error:[/bold red] expected exactly 1 upload VM in client "
            f"state, found {len(uploads)}."
        )
        sys.exit(1)
    if not probes:
        console.print("[bold red]Error:[/bold red] no probe VMs found in client state.")
        sys.exit(1)

    probes_sorted = sorted(probes, key=lambda c: c["client_index"])
    return uploads[0], probes_sorted


def _provision_canary(args, urls_content: str, console: Console) -> None:
    upload_client, probe_clients = _partition_clients(args.name, console)
    secret_key = _resolve_secret_key(args, console)
    binary_url, binary_is_archive = _resolve_ant_binary(args, console)

    console.print("[bold cyan]Step 4a/5: fetching production bootstrap peers[/bold cyan]")
    try:
        bootstrap_peers = fetch_prod_bootstraps()
    except ProdBootstrapsError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    console.print(f"  Using {len(bootstrap_peers)} production peers.")

    creds = _load_monitoring_creds(args, console)

    all_ips = [upload_client["ip"]] + [p["ip"] for p in probe_clients]
    clear_known_hosts(all_ips, console)
    not_ready = wait_for_ssh(all_ips, args.ssh_key_path, console=console)
    if not_ready:
        console.print(f"[bold red]Error:[/bold red] SSH never came up on: {', '.join(not_ready)}")
        sys.exit(1)

    console.print("[bold cyan]Step 4b/5: provisioning canary upload VM[/bold cyan]")
    upload_provisioner = ProdCanaryUploadProvisioner(
        host_ip=upload_client["ip"],
        region=upload_client.get("region", "unknown"),
        deployment_name=args.name,
        bootstrap_peers=bootstrap_peers,
        wallet_secret_key=secret_key,
        urls_file_content=urls_content,
        upload_sleep_secs=args.upload_sleep_secs,
        bootstrap_cache=getattr(args, "bootstrap_cache", True),
        rust_log=getattr(args, "rust_log", None),
        ssh_key_path=args.ssh_key_path,
        binary_url=binary_url,
        binary_is_archive=binary_is_archive,
        console=console,
        **creds,
    )
    upload_provisioner.execute()

    console.print(
        f"[bold cyan]Step 5/5: provisioning {len(probe_clients)} canary probe VM(s)[/bold cyan]"
    )
    probes_for_provisioner: list[dict] = []
    for shard_index, client in enumerate(probe_clients):
        probes_for_provisioner.append(
            {
                "ip": client["ip"],
                "region": client.get("region", "unknown"),
                "shard_index": shard_index,
            }
        )
    probe_provisioner = ProdCanaryProbeProvisioner(
        probes=probes_for_provisioner,
        shard_count=len(probe_clients),
        deployment_name=args.name,
        bootstrap_peers=bootstrap_peers,
        probe_concurrency=args.probe_concurrency,
        bootstrap_cache=getattr(args, "bootstrap_cache", True),
        rust_log=getattr(args, "rust_log", None),
        ssh_key_path=args.ssh_key_path,
        binary_url=binary_url,
        binary_is_archive=binary_is_archive,
        console=console,
        **creds,
    )
    probe_provisioner.execute()


# ---------------------------------------------------------------------------
# Service stop helpers
# ---------------------------------------------------------------------------


def _stop_canary_services(args, console: Console) -> None:
    state = load_client_state(args.name)
    host_ips = [c["ip"] for c in state.get("clients", []) if c.get("ip")]
    if not host_ips:
        return
    clear_known_hosts(host_ips, console)
    hosts_data = [(ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in host_ips]
    state_obj = State(inventory=Inventory((hosts_data, {})), config=Config())
    connect_all(state_obj)
    try:
        add_op(
            state_obj,
            server.shell,
            name="Stop and disable canary services",
            commands=[
                f"systemctl disable --now {UPLOAD_SERVICE_NAME} 2>/dev/null || true",
                f"systemctl disable --now {PROBE_SERVICE_NAME} 2>/dev/null || true",
            ],
        )
        run_ops(state_obj)
    finally:
        disconnect_all(state_obj)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def cmd_prod_canary_deploy(args):
    console = Console()

    if not args.upload_spec:
        console.print("[bold red]Error:[/bold red] --upload-spec is required.")
        sys.exit(1)
    if not args.probe_spec:
        console.print("[bold red]Error:[/bold red] At least one --probe-spec is required.")
        sys.exit(1)
    if bool(args.urls_file) == bool(getattr(args, "pool", None)):
        console.print(
            "[bold red]Error:[/bold red] exactly one of --urls-file or --pool "
            "is required. --pool draws from the managed file pool, which "
            "cannot return a file that has already been uploaded."
        )
        sys.exit(1)
    if not getattr(args, "pool", None) and (
        getattr(args, "pool_min_size", None) or getattr(args, "pool_max_size", None)
    ):
        console.print(
            "[bold red]Error:[/bold red] --pool-min-size/--pool-max-size narrow "
            "a --pool selection; they have no effect on --urls-file."
        )
        sys.exit(1)
    if args.upload_sleep_secs < 0:
        console.print("[bold red]Error:[/bold red] --upload-sleep-secs must be >= 0.")
        sys.exit(1)
    if args.probe_concurrency < 1:
        console.print("[bold red]Error:[/bold red] --probe-concurrency must be >= 1.")
        sys.exit(1)

    try:
        upload_spec = parse_upload_spec(args.upload_spec)
        probe_specs = [parse_probe_spec(s) for s in args.probe_spec]
        urls_content = _read_urls_file(args.urls_file) if args.urls_file else None
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # Before any infrastructure and before the wallet check: a canary consumes
    # ~350 distinct files permanently, so a shortfall must surface here rather
    # than after VMs exist.
    pool_selection_result = None
    if getattr(args, "pool", None):
        urls_content, pool_selection_result = _select_canary_pool_files(args, console)

    # Eagerly resolve the wallet so we fail fast before spinning up infra.
    if not getattr(args, "secret_key", None):
        if not os.environ.get("ARBITRUM_ONE_SECRET_KEY"):
            console.print(
                "[bold red]Error:[/bold red] No wallet secret key provided. "
                "Use --secret-key or set ARBITRUM_ONE_SECRET_KEY (a funded "
                "Arbitrum One wallet)."
            )
            sys.exit(1)

    total_probes = sum(p["count"] for p in probe_specs)
    url_count = sum(
        1 for line in urls_content.splitlines() if line.strip() and not line.strip().startswith("#")
    )
    console.print(f"[bold]prod canary deploy: {args.name}[/bold]")
    console.print(f"  Prod bootstraps URL: {PROD_BOOTSTRAPS_URL}")
    console.print(f"  Upload VM: {upload_spec['region']} ({upload_spec['vm_size']})")
    for ps in probe_specs:
        console.print(f"  Probe spec: {ps['region']} ({ps['vm_size']}) x {ps['count']}")
    console.print(f"  URL count: {url_count}")
    console.print(f"  Probe shard count: {total_probes}")
    console.print(f"  Upload sleep secs: {args.upload_sleep_secs}")
    console.print(f"  Probe concurrency: {args.probe_concurrency}")
    console.print()

    deploy_args_payload = {
        "kind": "prod-canary",
        "prod_bootstraps_url": PROD_BOOTSTRAPS_URL,
        "monitoring_name": args.name,
        "upload_spec": upload_spec,
        "probe_specs": probe_specs,
        "urls_file": args.urls_file,
        "url_count": url_count,
        "probe_shard_count": total_probes,
        "probe_concurrency": args.probe_concurrency,
        "upload_sleep_secs": args.upload_sleep_secs,
        "bootstrap_cache": getattr(args, "bootstrap_cache", True),
        "forward_logs": getattr(args, "forward_logs", False),
        "rust_log": getattr(args, "rust_log", None),
        "purpose": getattr(args, "purpose", None),
        "ant_binary": build_binary_descriptor(args.branch_name, args.repo_owner, args.ant_version),
    }
    deployment_id = record_deployment_created(args.name, deploy_args_payload)
    if pool_selection_result:
        from saorsa_deploy.pool_selection import attach_deployment
        from saorsa_deploy.registry import get_file_pool_repository

        pool_repo = get_file_pool_repository()
        if pool_repo is not None and deployment_id:
            attach_deployment(pool_repo, [pool_selection_result], deployment_id, args.name)
    record_event(args.name, "prod_canary_deploy_started", deploy_args_payload)

    try:
        _ensure_monitoring(args, console)
        _ensure_client_infra(args, upload_spec, probe_specs, console)
        _provision_canary(args, urls_content, console)
        from saorsa_deploy.cmd.provision_telegraf_metrics import (
            provision_client_telegraf_best_effort,
        )

        provision_client_telegraf_best_effort(args.name, args.ssh_key_path, console=console)
        if getattr(args, "forward_logs", False):
            from saorsa_deploy.cmd.prod_log_forwarding import (
                PROD_CANARY_GLOBS,
                ensure_client_elastic,
                install_client_forwarder,
            )

            es_creds = ensure_client_elastic(args.name, args.ssh_key_path, console)
            can_state = load_client_state(args.name)
            up_ips = [
                c["ip"]
                for c in can_state.get("clients", [])
                if c.get("ip") and c.get("role", "upload") == "upload"
            ]
            probe_ips = [
                c["ip"]
                for c in can_state.get("clients", [])
                if c.get("ip") and c.get("role") == "download"
            ]
            install_client_forwarder(
                name=args.name,
                role="canary-upload",
                host_ips=up_ips,
                tail_globs=PROD_CANARY_GLOBS,
                es_creds=es_creds,
                ssh_key_path=args.ssh_key_path,
                console=console,
            )
            install_client_forwarder(
                name=args.name,
                role="canary-probe",
                host_ips=probe_ips,
                tail_globs=PROD_CANARY_GLOBS,
                es_creds=es_creds,
                ssh_key_path=args.ssh_key_path,
                console=console,
            )
    except SystemExit:
        record_event(args.name, "prod_canary_deploy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Deploy failed:[/bold red] {e}")
        record_event(args.name, "prod_canary_deploy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_canary_deploy_completed")
    record_status(args.name, DeploymentStatus.RUNNING)
    console.print()
    console.print(f"[bold green]prod canary '{args.name}' is running.[/bold green]")
    console.print(
        f"[dim]Persist canary metrics: saorsa-deploy deployments record-results "
        f"--name {args.name}[/dim]"
    )
    console.print(
        f"[dim]Print canary status:    saorsa-deploy deployments "
        f"print-recorded-results --name {args.name}[/dim]"
    )


def cmd_prod_canary_destroy(args):
    from saorsa_deploy.cmd.destroy_clients import cmd_destroy_clients
    from saorsa_deploy.cmd.destroy_monitoring import cmd_destroy_monitoring

    console = Console()
    console.print(f"[bold]prod canary destroy: {args.name}[/bold]")

    # Only when monitoring is going too. A canary keeps its InfluxDB by default
    # — the dataset IS the product — so the usual teardown leaves the results
    # recoverable and there is nothing to warn about.
    keep_monitoring = getattr(args, "keep_monitoring", True)
    if not keep_monitoring and not getattr(args, "skip_results_check", False):
        from saorsa_deploy.results_guard import check_before_destroy

        check_before_destroy(console, args.name, "prod-canary")

    record_event(args.name, "prod_canary_destroy_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    try:
        if _client_stack_exists(args.name):
            try:
                _stop_canary_services(args, console)
            except (SystemExit, Exception) as e:
                console.print(
                    f"[yellow]canary stop reported a failure ({e!s}); "
                    "continuing with destroy.[/yellow]"
                )

            console.print("[bold cyan]Destroying canary VMs...[/bold cyan]")
            cmd_destroy_clients(SimpleNamespace(name=args.name, force=True))
        else:
            console.print(
                f"[dim]No client stack found for '{args.name}' — skipping canary destroy.[/dim]"
            )

        if args.keep_monitoring:
            console.print(
                "[dim]Leaving monitoring stack intact (--keep-monitoring); "
                "canary dataset survives destroy.[/dim]"
            )
        elif _monitoring_stack_exists(args.name):
            console.print("[bold cyan]Destroying monitoring stack...[/bold cyan]")
            cmd_destroy_monitoring(SimpleNamespace(name=args.name, force=True))
        else:
            console.print(f"[dim]No monitoring stack found for '{args.name}' — skipping.[/dim]")

        from saorsa_deploy.cmd.prod_log_forwarding import destroy_client_elastic

        destroy_client_elastic(args.name, getattr(args, "keep_elastic", False), console)
    except SystemExit:
        record_event(args.name, "prod_canary_destroy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Destroy failed:[/bold red] {e}")
        record_event(args.name, "prod_canary_destroy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_canary_destroy_completed")
    record_status(args.name, DeploymentStatus.DESTROYED)
    console.print()
    console.print(f"[bold green]prod canary '{args.name}' destroyed.[/bold green]")


# Re-exported for external use (kept here so tests can import without depending
# on monitoring_volume_size's default living in two places).
DEFAULT_MONITORING_VOLUME = DEFAULT_MONITORING_VOLUME_SIZE


def _select_canary_pool_files(args, console: Console):
    """Draw this canary's files from the pool.

    The canary is the pool's dominant consumer — measured at 382, 311 and 313
    distinct files across the three runs to date, all of them permanently
    spent. Two consequences shape this function: the selection is summarised
    per size class rather than listed (350 lines would bury everything else),
    and a resumed deploy must reuse the persisted selection rather than drawing
    another ~350 files.

    Returns ``(urls_content, selected)`` where ``urls_content`` is the same flat
    list the rest of the command already distributes, so provisioning and the
    VM-side fetch are untouched.
    """
    from saorsa_db.models.prod_file_pool import ClaimSource, PoolFileKind

    from saorsa_deploy.pool_selection import (
        PoolSelectionError,
        parse_request_token,
        parse_size_bounds,
        render_selection,
        select_for_deployment,
    )
    from saorsa_deploy.registry import get_file_pool_repository

    repo = get_file_pool_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] --pool needs DATABASE_URL — the file "
            "pool lives in the registry database.\n"
            "No infrastructure was created."
        )
        sys.exit(1)

    try:
        request = parse_request_token(args.pool)
        # A canary measures chunk loss, and the chunk floor is 4 — a 14 KB file
        # buys the same minimum observation as a 5 MB one while costing a pool
        # entry either way. --pool-min-size is how an operator keeps the run off
        # the bottom of the tiny class.
        min_size_bytes, max_size_bytes = parse_size_bounds(
            getattr(args, "pool_min_size", None),
            getattr(args, "pool_max_size", None),
        )
    except (PoolSelectionError, ValueError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    try:
        selected = select_for_deployment(
            repo,
            name=args.name,
            slot="canary-upload",
            kind=PoolFileKind.URL,
            request=request,
            source=ClaimSource.PROD_CANARY,
            min_size_bytes=min_size_bytes,
            max_size_bytes=max_size_bytes,
        )
    except PoolSelectionError as exc:
        console.print(f"\n[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    console.print("  Pool selection")
    render_selection(console, "canary upload", request, selected, min_size_bytes, max_size_bytes)
    console.print(
        f"    {len(selected)} file(s) claimed; selection persisted to S3 state "
        "(a resume reuses it)."
    )
    return "\n".join(entry.locator for entry in selected) + "\n", selected
