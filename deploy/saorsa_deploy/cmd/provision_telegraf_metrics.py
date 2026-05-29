import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_telegraf import (
    group_by_role_and_credentials,
    read_inventory,
    read_ips_from_file,
    set_hostnames,
)
from saorsa_deploy.provisioning.monitoring import INFLUXDB_CLIENT_BUCKET
from saorsa_deploy.provisioning.telegraf_metrics import TelegrafMetricsProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import (
    load_client_state,
    load_deployment_state,
    load_monitoring_state,
)


def _provision_node_telegraf(
    state: dict, common_kwargs: dict, influxdb_url: str, console: Console
) -> tuple[bool, bool]:
    """Provision Telegraf host metrics on a testnet's node VMs.

    Returns ``(found, ok)``: ``found`` is True if the deployment had any node
    VMs to provision, ``ok`` is True if every node group provisioned without
    error. A failure no longer aborts the process — it is reported and recorded
    in ``ok`` so the caller can still provision client host metrics and decide
    the overall exit status. Provisioning of later groups continues regardless.
    """
    node_count = state.get("node_count", 0)
    bootstrap_nodes = state.get("bootstrap_nodes") or []
    bootstrap_ip = state.get("bootstrap_ip")
    vm_ips = state.get("vm_ips", {})

    if bootstrap_nodes:
        genesis_ips = [
            n["ip"] for n in bootstrap_nodes
            if n.get("bootstrap_index") == 1 and n.get("ip")
        ]
        secondary_ips = [
            n["ip"] for n in bootstrap_nodes
            if n.get("bootstrap_index", 0) >= 2 and n.get("ip")
        ]
    else:
        genesis_ips = [bootstrap_ip] if bootstrap_ip else []
        secondary_ips = []

    regular_ips = []
    for region_key in sorted(vm_ips.keys()):
        regular_ips.extend(vm_ips[region_key])

    all_ips = list(genesis_ips) + list(secondary_ips) + list(regular_ips)
    if not all_ips:
        return (False, True)

    ok = True
    console.print(f"  InfluxDB URL: {influxdb_url}")
    console.print(f"  Target hosts: {len(all_ips)}")
    if node_count:
        console.print(f"  Node count per VM: {node_count}")
    console.print()

    clear_known_hosts(all_ips, console)

    # Provision bootstrap VMs (genesis + any secondary bootstraps)
    for role, is_genesis, ips in [
        ("GENESIS", True, genesis_ips),
        ("BOOTSTRAP", True, secondary_ips),
    ]:
        if not ips:
            continue
        console.print(
            f"[bold]Provisioning Telegraf on {len(ips)} {role} "
            f"node(s): {', '.join(ips)}...[/bold]"
        )
        try:
            TelegrafMetricsProvisioner(
                host_ips=ips,
                node_count=1,
                is_genesis=is_genesis,
                host_role=role,
                **common_kwargs,
            ).execute()
        except Exception as e:
            console.print(
                f"[bold red]Telegraf metrics provisioning failed on {role}:"
                f"[/bold red] {e}"
            )
            ok = False

    # Provision regular VMs (N nodes each)
    if regular_ips:
        console.print(
            f"\n[bold]Provisioning Telegraf on {len(regular_ips)}"
            f" regular VM(s)...[/bold]"
        )
        try:
            TelegrafMetricsProvisioner(
                host_ips=regular_ips,
                node_count=node_count,
                host_role="WAN_NODE",
                **common_kwargs,
            ).execute()
        except Exception as e:
            console.print(
                f"[bold red]Telegraf metrics provisioning failed:[/bold red] {e}"
            )
            ok = False

    return (True, ok)


def _provision_client_telegraf(
    name: str, common_kwargs: dict, console: Console
) -> tuple[bool, bool]:
    """Provision Telegraf host metrics (host_role=CLIENT) on a deployment's
    client VMs (uploaders / downloaders / canary).

    node_count=0 → host stats only, no Prometheus scrape block. Returns
    ``(found, ok)``: ``found`` is True if the deployment had client VMs to
    provision, ``ok`` is True if provisioning did not error. Does not exit the
    process — the caller owns the overall exit status.
    """
    try:
        client_state = load_client_state(name)
    except RuntimeError:
        return (False, True)

    client_ips = [
        c["ip"] for c in client_state.get("clients", []) if c.get("ip")
    ]
    if not client_ips:
        return (False, True)

    console.print(
        f"\n[bold]Provisioning Telegraf on {len(client_ips)}"
        f" client VM(s) (host_role=CLIENT, bucket={INFLUXDB_CLIENT_BUCKET})..."
        f"[/bold]"
    )
    clear_known_hosts(client_ips, console)
    # Client host metrics land in the dedicated client bucket so the Client
    # Host Statistics dashboard stays physically isolated from node data.
    client_kwargs = {**common_kwargs, "influxdb_bucket": INFLUXDB_CLIENT_BUCKET}
    try:
        TelegrafMetricsProvisioner(
            host_ips=client_ips,
            node_count=0,
            host_role="CLIENT",
            **client_kwargs,
        ).execute()
    except Exception as e:
        console.print(
            f"[bold red]Telegraf metrics provisioning failed on "
            f"clients:[/bold red] {e}"
        )
        return (True, False)
    return (True, True)


def provision_client_telegraf_for_deployment(
    name: str,
    ssh_key_path: str,
    ssh_user: str = "root",
    console: Console | None = None,
) -> bool:
    """Provision CLIENT-tagged Telegraf host metrics for a client-only
    deployment, resolving InfluxDB connection details from its monitoring
    state. Intended to be called from the prod client deploy flows so a fresh
    deployment auto-collects host metrics.

    Returns True if any client VM was provisioned. Raises RuntimeError if the
    deployment has no monitoring state; may call sys.exit(1) if the underlying
    provisioning fails (callers that want host metrics to be best-effort
    should catch SystemExit).
    """
    console = console or Console()
    monitoring = load_monitoring_state(name)
    common_kwargs = {
        "ssh_key_path": ssh_key_path,
        "ssh_user": ssh_user,
        "influxdb_url": monitoring.get("influxdb_url", ""),
        "influxdb_token": monitoring.get("influxdb_token", ""),
        "influxdb_org": monitoring.get("influxdb_org", "ant"),
        "influxdb_bucket": monitoring.get("influxdb_bucket", "testnet-metrics"),
        "testnet_name": name,
        "console": console,
    }
    found, ok = _provision_client_telegraf(name, common_kwargs, console)
    if not ok:
        sys.exit(1)
    return found


def provision_client_telegraf_best_effort(
    name: str,
    ssh_key_path: str,
    ssh_user: str = "root",
    console: Console | None = None,
) -> bool:
    """Best-effort wrapper around :func:`provision_client_telegraf_for_deployment`
    for use inside the prod client deploy flows.

    Never raises or exits — the client service is the deploy deliverable, so a
    Telegraf hiccup should warn and leave the deploy intact. Returns True if
    client host metrics were provisioned, and prints the manual re-run command
    on failure.
    """
    console = console or Console()
    console.print(
        "[bold cyan]Provisioning client host metrics (Telegraf)[/bold cyan]"
    )
    try:
        provisioned = provision_client_telegraf_for_deployment(
            name, ssh_key_path, ssh_user, console
        )
        if not provisioned:
            console.print(
                "[yellow]No client VMs found to provision host metrics on."
                "[/yellow]"
            )
        return provisioned
    except SystemExit:
        console.print(
            "[yellow]Client host-metrics provisioning failed; deploy "
            "continues. Re-run later with: saorsa-deploy monitoring "
            f"provision-telegraf --name {name}[/yellow]"
        )
        return False
    except Exception as e:
        console.print(
            f"[yellow]Client host-metrics provisioning error ({e}); deploy "
            "continues.[/yellow]"
        )
        return False


def cmd_provision_telegraf_metrics(args):
    """Provision Telegraf for InfluxDB metrics forwarding on testnet VMs."""
    console = Console()

    # Resolve InfluxDB connection details first
    influxdb_url = args.influxdb_url
    influxdb_token = args.influxdb_token
    influxdb_org = args.influxdb_org
    influxdb_bucket = args.influxdb_bucket

    if not influxdb_url and args.name:
        console.print(
            f"[dim]Loading monitoring state for '{args.name}'...[/dim]"
        )
        try:
            monitoring = load_monitoring_state(args.name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            console.print(
                "[dim]Provide --influxdb-url and --influxdb-token explicitly, "
                "or create a monitoring stack with monitoring infra first.[/dim]"
            )
            sys.exit(1)
        influxdb_url = monitoring.get("influxdb_url", "")
        influxdb_token = monitoring.get("influxdb_token", "")
        influxdb_org = monitoring.get("influxdb_org", influxdb_org)
        influxdb_bucket = monitoring.get("influxdb_bucket", influxdb_bucket)

    if not influxdb_url or not influxdb_token:
        console.print(
            "[bold red]Error:[/bold red] "
            "InfluxDB URL and token are required. Provide --influxdb-url and "
            "--influxdb-token, or ensure a monitoring stack exists for --name."
        )
        sys.exit(1)

    common_kwargs = {
        "ssh_key_path": args.ssh_key_path,
        "ssh_user": args.ssh_user,
        "influxdb_url": influxdb_url,
        "influxdb_token": influxdb_token,
        "influxdb_org": influxdb_org,
        "influxdb_bucket": influxdb_bucket,
        "testnet_name": args.name or "",
        "console": console,
    }

    # Resolve host IPs and node counts
    if args.inventory_path:
        try:
            entries = read_inventory(args.inventory_path)
        except (OSError, ValueError, KeyError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        all_ips = [e["ip"] for e in entries]
        console.print(
            f"[bold]Using {len(entries)} VM(s) from inventory "
            f"{args.inventory_path}...[/bold]"
        )
        console.print(f"  InfluxDB URL: {influxdb_url}")
        console.print(f"  Target hosts: {len(entries)}")
        console.print()

        groups = group_by_role_and_credentials(entries)
        clear_known_hosts(all_ips, console)
        set_hostnames(entries, console)

        for (role, ssh_user, ssh_key), ips in groups.items():
            # Bootstrap VMs run a single service; node VMs run many.
            # node_count=0 makes telegraf_metrics auto-detect based on
            # actual services present, so it's safe for either.
            # CLIENT-role hosts write to the dedicated client bucket so their
            # host metrics stay isolated from node data.
            bucket = (
                INFLUXDB_CLIENT_BUCKET if role == "CLIENT" else influxdb_bucket
            )
            console.print(
                f"[bold]Provisioning {len(ips)} host(s) "
                f"(role={role}, ssh_user={ssh_user}, bucket={bucket})...[/bold]"
            )
            try:
                TelegrafMetricsProvisioner(
                    host_ips=ips,
                    ssh_key_path=ssh_key,
                    ssh_user=ssh_user,
                    node_count=0,
                    host_role=role,
                    influxdb_url=influxdb_url,
                    influxdb_token=influxdb_token,
                    influxdb_org=influxdb_org,
                    influxdb_bucket=bucket,
                    testnet_name=args.name or "",
                    console=console,
                ).execute()
            except Exception as e:
                console.print(
                    f"[bold red]Telegraf metrics provisioning failed:"
                    f"[/bold red] {e}"
                )
                sys.exit(1)
    elif args.host_ips_file:
        try:
            all_ips = read_ips_from_file(args.host_ips_file)
        except (OSError, ValueError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        console.print(
            f"[bold]Using {len(all_ips)} host IP(s) from "
            f"{args.host_ips_file}...[/bold]"
        )
        console.print(f"  InfluxDB URL: {influxdb_url}")
        console.print(f"  Target hosts: {len(all_ips)}")
        console.print(f"  SSH user: {args.ssh_user}")
        console.print()

        clear_known_hosts(all_ips, console)
        try:
            TelegrafMetricsProvisioner(
                host_ips=all_ips,
                node_count=0,
                host_role="WAN_NODE",
                **common_kwargs,
            ).execute()
        except Exception as e:
            console.print(
                f"[bold red]Telegraf metrics provisioning failed:[/bold red] {e}"
            )
            sys.exit(1)
    elif args.host_ips:
        all_ips = [ip.strip() for ip in args.host_ips.split(",") if ip.strip()]
        console.print(f"[bold]Using {len(all_ips)} provided host IP(s)...[/bold]")
        console.print(f"  InfluxDB URL: {influxdb_url}")
        console.print(f"  Target hosts: {len(all_ips)}")
        console.print()

        clear_known_hosts(all_ips, console)
        try:
            TelegrafMetricsProvisioner(
                host_ips=all_ips,
                node_count=0,
                host_role="WAN_NODE",
                **common_kwargs,
            ).execute()
        except Exception as e:
            console.print(
                f"[bold red]Telegraf metrics provisioning failed:[/bold red] {e}"
            )
            sys.exit(1)
    elif args.name:
        console.print(
            f"[bold]Loading deployment state for '{args.name}'...[/bold]"
        )
        # Deployment (testnet) state is optional: client-only deployments
        # (prod uploaders / downloaders / canary) save client state but no
        # testnet deployment state. Tolerate its absence and provision
        # whatever VMs the deployment actually has.
        try:
            state = load_deployment_state(args.name)
        except RuntimeError:
            state = None
            console.print(
                f"[dim]No testnet deployment state for '{args.name}' — "
                "provisioning client VMs only (if any).[/dim]"
            )

        # Provision node and client host metrics independently: a failure on
        # one must not skip the other (a single flaky node used to abort the
        # whole command before client VMs were ever touched). Attempt both,
        # then fail at the end if either had an error.
        node_found, node_ok = (False, True)
        if state is not None:
            node_found, node_ok = _provision_node_telegraf(
                state, common_kwargs, influxdb_url, console
            )

        client_found, client_ok = _provision_client_telegraf(
            args.name, common_kwargs, console
        )

        if not node_found and not client_found:
            console.print(
                "[bold red]Error:[/bold red] No node or client VMs found for "
                f"'{args.name}'. Nothing to provision."
            )
            sys.exit(1)

        if not node_ok or not client_ok:
            failed = [
                label
                for label, ok in (("node", node_ok), ("client", client_ok))
                if not ok
            ]
            console.print(
                f"[bold red]Error:[/bold red] Telegraf provisioning failed on "
                f"{' and '.join(failed)} host(s) for '{args.name}'. "
                "Other host groups were still provisioned; re-run to retry."
            )
            sys.exit(1)
    else:
        console.print(
            "[bold red]Error:[/bold red] "
            "Either --name, --host-ips, --host-ips-file, or --inventory-path "
            "must be provided."
        )
        sys.exit(1)

    console.print()
    console.print(
        "[bold green]Telegraf metrics provisioned successfully on all hosts.[/bold green]"
    )
