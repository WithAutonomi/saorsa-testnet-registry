"""Higher-level orchestration of a prod uploaders deployment.

A prod uploaders deployment is a multi-VM client deployment that runs
``ant --json file upload --public`` against the **production** Autonomi
network. Each uploader is given a list of public source URLs; the service
downloads every URL in turn, uploads it, records per-file metrics in Influx,
and exits when its list is exhausted. Teardown is explicit via
``prod uploaders destroy``.

Like the prod continuous-downloader command, this is a thin orchestrator:
it shells out to existing primitive commands (``clients infra``,
``monitoring infra``, ``monitoring provision``, ``clients destroy``,
``monitoring destroy``) and uses a custom prod-uploader Pyinfra provisioner
for the per-host service.
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
from saorsa_deploy.prod_bootstraps import (
    PROD_BOOTSTRAPS_URL,
    ProdBootstrapsError,
    fetch_prod_bootstraps,
)
from saorsa_deploy.provisioning.prod_uploader import (
    SERVICE_NAME,
    ProdUploaderProvisioner,
)
from saorsa_deploy.registry import (
    record_deployment_created,
    record_event,
    record_status,
)
from saorsa_deploy.ssh import clear_known_hosts, wait_for_ssh
from saorsa_deploy.state import load_client_state, load_monitoring_state

MONITORING_REGION = "lon1"
DEFAULT_MONITORING_VOLUME_SIZE = 50


_KNOWN_SPEC_KEYS = ("urls", "payment")
_VALID_PAYMENT_VALUES = ("merkle", "no-merkle")


def parse_prod_uploader_spec(spec: str) -> dict:
    """Parse a ``--uploader`` spec.

    Format: ``REGION:VM_SIZE:urls=PATH[:payment=MODE]``. Tail tokens are
    key=value; ``urls`` is required, ``payment`` is optional and must be
    ``merkle`` or ``no-merkle``. Returns a dict with keys ``region``,
    ``vm_size``, ``urls_file`` (str), and ``payment_type`` (str | None).
    """
    parts = spec.split(":")
    if len(parts) < 3:
        raise ValueError(
            f"Invalid uploader spec '{spec}'. "
            f"Expected format: region:vm_size:urls=PATH[:payment=MODE]"
        )

    region = parts[0]
    vm_size = parts[1]
    if not region:
        raise ValueError(f"Invalid uploader spec '{spec}': region cannot be empty.")
    if not vm_size:
        raise ValueError(f"Invalid uploader spec '{spec}': vm_size cannot be empty.")

    # The remaining tail consists of key=value tokens; rejoin with ':' so a
    # path containing colons (rare but legal) is preserved verbatim once we
    # split on the first '=' per token.
    tail_tokens: list[str] = []
    buf = parts[2]
    for next_part in parts[3:]:
        if "=" in next_part and next_part.split("=", 1)[0] in _KNOWN_SPEC_KEYS:
            tail_tokens.append(buf)
            buf = next_part
        else:
            buf = f"{buf}:{next_part}"
    tail_tokens.append(buf)

    options: dict[str, str] = {}
    for token in tail_tokens:
        if "=" not in token:
            raise ValueError(
                f"Invalid uploader spec '{spec}': '{token}' is not key=value."
            )
        key, value = token.split("=", 1)
        if key not in _KNOWN_SPEC_KEYS:
            raise ValueError(
                f"Invalid uploader spec '{spec}': unknown key '{key}'."
            )
        if key in options:
            raise ValueError(
                f"Invalid uploader spec '{spec}': duplicate key '{key}'."
            )
        options[key] = value

    if "urls" not in options:
        raise ValueError(
            f"Invalid uploader spec '{spec}'. urls=PATH is required."
        )

    urls_file = options["urls"]
    if not urls_file:
        raise ValueError(
            f"Invalid uploader spec '{spec}': urls path cannot be empty."
        )

    payment_type: str | None = options.get("payment")
    if payment_type is not None and payment_type not in _VALID_PAYMENT_VALUES:
        raise ValueError(
            f"Invalid uploader spec '{spec}': payment must be one of "
            f"{', '.join(_VALID_PAYMENT_VALUES)}."
        )

    return {
        "region": region,
        "vm_size": vm_size,
        "urls_file": urls_file,
        "payment_type": payment_type,
    }


def _read_urls_file(path: str) -> str:
    """Read and validate the URL list, returning its content for distribution."""
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Cannot read --uploader urls={path}: {e}") from e
    lines = [line.strip() for line in content.splitlines()]
    has_urls = any(line and not line.startswith("#") for line in lines)
    if not has_urls:
        raise ValueError(f"--uploader urls={path} contains no URLs.")
    return content


def _extract_file_names(urls_content: str) -> list[str]:
    """Parse a URL-list file into the bare filename of each upload target.

    Strips comments/blank lines, then takes the last path segment of each URL.
    Falls back to the raw URL if parsing yields an empty name.
    """
    from urllib.parse import urlparse

    names: list[str] = []
    for line in urls_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = urlparse(line)
        candidate = Path(parsed.path).name if parsed.path else ""
        names.append(candidate or line)
    return names


def _resolve_secret_key(args, console: Console) -> str:
    key = getattr(args, "secret_key", None)
    if key:
        console.print("  Wallet key: provided via --secret-key")
        return key
    key = os.environ.get("ARBITRUM_ONE_SECRET_KEY")
    if key:
        console.print("  Wallet key: from ARBITRUM_ONE_SECRET_KEY env var")
        return key
    console.print(
        "[bold red]Error:[/bold red] No wallet secret key provided. "
        "Use --secret-key or set ARBITRUM_ONE_SECRET_KEY (a funded Arbitrum One "
        "wallet — distinct from the testnet ANT_DEPLOY_SECRET_KEY)."
    )
    sys.exit(1)


def _resolve_ant_binary(args, console: Console):
    """Mirror provision_clients's ant binary resolution: returns (url, is_archive)."""
    if args.branch_name:
        repo_owner = getattr(args, "repo_owner", None) or "maidsafe"
        from saorsa_deploy.binary_source import (
            check_ant_custom_build_exists,
            get_ant_custom_build_url,
        )
        if not check_ant_custom_build_exists(args.name, repo_owner, args.branch_name):
            console.print(
                f"[bold red]Error:[/bold red] No custom ant build found for "
                f"{repo_owner}/{args.branch_name}. Run 'build-ant' first."
            )
            sys.exit(1)
        url = get_ant_custom_build_url(args.name, repo_owner, args.branch_name)
        console.print(
            f"  Ant binary: custom build ({repo_owner}/{args.branch_name})"
        )
        return url, False

    if args.ant_version:
        from saorsa_deploy.binary_source import (
            check_ant_release_exists,
            get_ant_release_url,
        )
        if not check_ant_release_exists(args.ant_version):
            console.print(
                f"[bold red]Error:[/bold red] Ant release v{args.ant_version} not found."
            )
            sys.exit(1)
        url = get_ant_release_url(args.ant_version)
        console.print(f"  Ant binary: release v{args.ant_version}")
        return url, True

    console.print("  Ant binary: latest release")
    return None, True


def _monitoring_stack_exists(name: str) -> bool:
    try:
        load_monitoring_state(name)
        return True
    except RuntimeError:
        return False


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
        console.print(
            f"[dim]Monitoring stack '{args.name}' already exists — skipping "
            "monitoring infra / monitoring provision.[/dim]"
        )
        return

    console.print("[bold cyan]Step 1/5: creating monitoring VM[/bold cyan]")
    cmd_monitoring_infra(SimpleNamespace(
        name=args.name,
        region=MONITORING_REGION,
        volume_size=args.monitoring_volume_size,
        force=False,
    ))

    console.print("[bold cyan]Step 2/5: provisioning InfluxDB + Grafana[/bold cyan]")
    cmd_provision_monitoring(SimpleNamespace(
        name=args.name,
        ssh_key_path=args.ssh_key_path,
    ))


def _ensure_client_infra(args, parsed_specs: list[dict], console: Console) -> None:
    """Create the uploader VMs via clients infra.

    The existing clients infra parser expects testnet uploader specs of the
    form ``region:file_size_mb[:vm_size]``. We synthesise those with
    file_size_mb=0 because that field is only consumed by the legacy testnet
    uploader.sh and is unused for prod uploads.
    """
    from saorsa_deploy.cmd.infra_clients import cmd_infra_clients

    if _client_stack_exists(args.name):
        console.print(
            f"[dim]Client stack '{args.name}' already exists — skipping "
            "clients infra.[/dim]"
        )
        return

    synth_uploader_specs = [
        f"{s['region']}:0:{s['vm_size']}"
        for s in parsed_specs
    ]
    console.print(
        f"[bold cyan]Step 3/5: creating {len(synth_uploader_specs)} "
        f"uploader VM(s)[/bold cyan]"
    )
    cmd_infra_clients(SimpleNamespace(
        name=args.name,
        uploader=synth_uploader_specs,
        downloader=[],
        force=False,
    ))


def _provision_uploaders(
    args,
    parsed_specs: list[dict],
    console: Console,
) -> None:
    from saorsa_deploy.state import load_monitoring_state as _load_mon

    state = load_client_state(args.name)
    clients = state.get("clients", [])

    upload_clients = [
        c for c in clients if c.get("role", "upload") == "upload"
    ]
    if len(upload_clients) != len(parsed_specs):
        console.print(
            f"[bold red]Error:[/bold red] expected {len(parsed_specs)} uploader "
            f"VMs in client state but found {len(upload_clients)}."
        )
        sys.exit(1)

    # Pair by ascending client_index — clients infra assigns 1..N in the
    # order specs were supplied.
    upload_clients_sorted = sorted(
        upload_clients, key=lambda c: c["client_index"]
    )
    uploaders_for_provisioner: list[dict] = []
    for spec, client in zip(parsed_specs, upload_clients_sorted):
        if not client.get("ip"):
            console.print(
                f"[bold red]Error:[/bold red] uploader #{client['client_index']} "
                f"({client['region']}) has no IP — VM creation failed."
            )
            sys.exit(1)
        uploaders_for_provisioner.append({
            "ip": client["ip"],
            "region": spec["region"],
            "client_index": client["client_index"],
            "urls_file_content": spec["_urls_file_content"],
            "payment_type": spec.get("payment_type"),
        })

    secret_key = _resolve_secret_key(args, console)
    binary_url, binary_is_archive = _resolve_ant_binary(args, console)

    console.print(
        "[bold cyan]Step 4a/5: fetching production bootstrap peers[/bold cyan]"
    )
    try:
        bootstrap_peers = fetch_prod_bootstraps()
    except ProdBootstrapsError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    console.print(f"  Using {len(bootstrap_peers)} production peers.")

    mon_state = _load_mon(args.name)
    influx_url = mon_state.get("influxdb_url", "")
    influx_token = mon_state.get("influxdb_token", "")
    influx_org = mon_state.get("influxdb_org", "ant")
    influx_bucket = mon_state.get("influxdb_bucket", "testnet-metrics")
    if not (influx_url and influx_token):
        console.print(
            "[bold red]Error:[/bold red] monitoring state has no Influx URL/token "
            "— run monitoring provision first."
        )
        sys.exit(1)

    host_ips = [u["ip"] for u in uploaders_for_provisioner]
    clear_known_hosts(host_ips, console)

    not_ready = wait_for_ssh(host_ips, args.ssh_key_path, console=console)
    if not_ready:
        console.print(
            f"[bold red]Error:[/bold red] SSH never came up on: "
            f"{', '.join(not_ready)}"
        )
        sys.exit(1)

    console.print(
        "[bold cyan]Step 4b/5: provisioning ant-prod-uploader on uploader "
        "VMs[/bold cyan]"
    )
    provisioner = ProdUploaderProvisioner(
        uploaders=uploaders_for_provisioner,
        bootstrap_peers=bootstrap_peers,
        wallet_secret_key=secret_key,
        ssh_key_path=args.ssh_key_path,
        binary_url=binary_url,
        binary_is_archive=binary_is_archive,
        influx_url=influx_url,
        influx_token=influx_token,
        influx_org=influx_org,
        influx_bucket=influx_bucket,
        console=console,
    )
    provisioner.execute()


def _start_uploaders(args, console: Console) -> None:
    state = load_client_state(args.name)
    clients = [
        c for c in state.get("clients", [])
        if c.get("ip") and c.get("role", "upload") == "upload"
    ]
    if not clients:
        console.print("[bold red]Error:[/bold red] No uploader VMs found in state.")
        sys.exit(1)

    host_ips = [c["ip"] for c in clients]
    console.print(
        f"[bold cyan]Step 5/5: starting ant-prod-uploader on "
        f"{len(host_ips)} VM(s)[/bold cyan]"
    )
    clear_known_hosts(host_ips, console)

    hosts_data = [
        (ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in host_ips
    ]
    inventory = Inventory((hosts_data, {}))
    state_obj = State(inventory=inventory, config=Config())
    connect_all(state_obj)
    try:
        add_op(
            state_obj,
            server.shell,
            name=f"Start {SERVICE_NAME}",
            commands=[f"systemctl enable --now {SERVICE_NAME}"],
        )
        run_ops(state_obj)
    finally:
        disconnect_all(state_obj)

    failed = state_obj.failed_hosts
    if failed:
        for host in failed:
            console.print(f"  [red]Failed to start on {host.name}[/red]")
        sys.exit(1)


def cmd_prod_uploaders_deploy(args):
    """Deploy a prod uploaders testbed against the Autonomi production network."""
    console = Console()

    if not args.uploader:
        console.print(
            "[bold red]Error:[/bold red] At least one --uploader spec is required."
        )
        sys.exit(1)

    parsed_specs: list[dict] = []
    for raw in args.uploader:
        try:
            spec = parse_prod_uploader_spec(raw)
            spec["_urls_file_content"] = _read_urls_file(spec["urls_file"])
        except ValueError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        parsed_specs.append(spec)

    console.print(f"[bold]prod uploaders deploy: {args.name}[/bold]")
    console.print(f"  Prod bootstraps URL: {PROD_BOOTSTRAPS_URL}")
    for i, spec in enumerate(parsed_specs, start=1):
        url_count = sum(
            1 for line in spec["_urls_file_content"].splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        console.print(
            f"  Uploader #{i}: {spec['region']} ({spec['vm_size']}, "
            f"urls={url_count})"
        )
    console.print()

    deploy_args_payload = {
        "kind": "prod-uploaders",
        "prod_bootstraps_url": PROD_BOOTSTRAPS_URL,
        "monitoring_name": args.name,
        "uploaders": [
            {
                "region": s["region"],
                "vm_size": s["vm_size"],
                "urls_file": s["urls_file"],
                "file_names": _extract_file_names(s["_urls_file_content"]),
                "payment_type": s.get("payment_type"),
            }
            for s in parsed_specs
        ],
        "purpose": args.purpose,
        "ant_binary": build_binary_descriptor(
            args.branch_name, args.repo_owner, args.ant_version
        ),
    }
    record_deployment_created(args.name, deploy_args_payload)
    record_event(args.name, "prod_uploaders_deploy_started", deploy_args_payload)

    try:
        _ensure_monitoring(args, console)
        _ensure_client_infra(args, parsed_specs, console)
        _provision_uploaders(args, parsed_specs, console)
        _start_uploaders(args, console)
        from saorsa_deploy.cmd.provision_telegraf_metrics import (
            provision_client_telegraf_best_effort,
        )
        provision_client_telegraf_best_effort(
            args.name, args.ssh_key_path, console=console
        )
    except SystemExit:
        record_event(args.name, "prod_uploaders_deploy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Deploy failed:[/bold red] {e}")
        record_event(args.name, "prod_uploaders_deploy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_uploaders_deploy_completed")
    record_status(args.name, DeploymentStatus.RUNNING)
    console.print()
    console.print(
        f"[bold green]prod uploaders '{args.name}' is running.[/bold green]"
    )
    console.print(
        f"[dim]Tail upload progress: saorsa-deploy deployments print-uploads "
        f"--name {args.name}[/dim]"
    )
    console.print(
        f"[dim]Persist uploads to DB:  saorsa-deploy deployments record-results "
        f"--name {args.name}[/dim]"
    )


def cmd_prod_uploaders_destroy(args):
    """Destroy a prod uploaders testbed."""
    from saorsa_deploy.cmd.destroy_clients import cmd_destroy_clients
    from saorsa_deploy.cmd.destroy_monitoring import cmd_destroy_monitoring

    console = Console()
    console.print(f"[bold]prod uploaders destroy: {args.name}[/bold]")

    record_event(args.name, "prod_uploaders_destroy_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    try:
        if _client_stack_exists(args.name):
            try:
                _stop_uploaders(args, console)
            except (SystemExit, Exception) as e:
                # Stopping is best-effort: a half-provisioned VM may have no
                # systemd unit yet, and a dead/unreachable VM will never stop
                # cleanly. Either way we must still proceed to clients destroy
                # so the Terraform-managed VMs get torn down.
                console.print(
                    f"[yellow]stop reported a failure ({e!s}); continuing "
                    "with destroy.[/yellow]"
                )

            console.print("[bold cyan]Destroying uploader VMs...[/bold cyan]")
            cmd_destroy_clients(SimpleNamespace(name=args.name, force=True))
        else:
            console.print(
                f"[dim]No client stack found for '{args.name}' — skipping "
                "uploader destroy.[/dim]"
            )

        if args.keep_monitoring:
            console.print(
                "[dim]Leaving monitoring stack intact (--keep-monitoring); "
                "metrics remain queryable.[/dim]"
            )
        elif _monitoring_stack_exists(args.name):
            console.print("[bold cyan]Destroying monitoring stack...[/bold cyan]")
            cmd_destroy_monitoring(SimpleNamespace(name=args.name, force=True))
        else:
            console.print(
                f"[dim]No monitoring stack found for '{args.name}' — "
                "skipping.[/dim]"
            )
    except SystemExit:
        record_event(args.name, "prod_uploaders_destroy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as e:
        console.print(f"[bold red]Destroy failed:[/bold red] {e}")
        record_event(args.name, "prod_uploaders_destroy_failed", {"error": str(e)})
        record_status(args.name, DeploymentStatus.FAILED)
        sys.exit(1)

    record_event(args.name, "prod_uploaders_destroy_completed")
    record_status(args.name, DeploymentStatus.DESTROYED)
    console.print()
    console.print(
        f"[bold green]prod uploaders '{args.name}' destroyed.[/bold green]"
    )


def _stop_uploaders(args, console: Console) -> None:
    state = load_client_state(args.name)
    clients = [
        c for c in state.get("clients", [])
        if c.get("ip") and c.get("role", "upload") == "upload"
    ]
    if not clients:
        return

    host_ips = [c["ip"] for c in clients]
    console.print(
        f"[bold cyan]Stopping {SERVICE_NAME} on {len(host_ips)} "
        f"VM(s)...[/bold cyan]"
    )
    clear_known_hosts(host_ips, console)

    hosts_data = [
        (ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in host_ips
    ]
    inventory = Inventory((hosts_data, {}))
    state_obj = State(inventory=inventory, config=Config())
    connect_all(state_obj)
    try:
        add_op(
            state_obj,
            server.shell,
            name=f"Stop {SERVICE_NAME}",
            commands=[
                f"systemctl disable --now {SERVICE_NAME} 2>/dev/null || true",
            ],
        )
        run_ops(state_obj)
    finally:
        disconnect_all(state_obj)
