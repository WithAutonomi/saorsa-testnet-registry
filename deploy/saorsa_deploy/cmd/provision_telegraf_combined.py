"""Combined Telegraf provisioning command (``deployments provision-telegraf``).

Installs both the Elasticsearch log-forwarder and the InfluxDB host-metrics
forwarder in a single SSH pass per host group, replacing the old separate
``elastic provision-telegraf`` and ``monitoring provision-telegraf`` commands.

A backend is provisioned only when its credentials resolve: explicit
``--elasticsearch-*`` / ``--influxdb-*`` flags, or — with ``--name`` — whatever
elastic / monitoring state the deployment has. So a testnet with only monitoring
gets metrics only, one with only Elasticsearch gets logs only, and one with both
gets a single combined pass.
"""

import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_telegraf import (
    _resolve_telegraf_tuning,
    _resolve_version_info,
    group_by_role_and_credentials,
    read_inventory,
    read_ips_from_file,
    set_hostnames,
)
from saorsa_deploy.cmd.provision_telegraf_metrics import _provision_client_telegraf
from saorsa_deploy.provisioning.monitoring import INFLUXDB_CLIENT_BUCKET
from saorsa_deploy.provisioning.telegraf import TelegrafProvisioner
from saorsa_deploy.provisioning.telegraf_combined import provision_combined
from saorsa_deploy.provisioning.telegraf_metrics import TelegrafMetricsProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import (
    load_deployment_state,
    load_elastic_state,
    load_monitoring_state,
)


def _resolve_backends(args, console):
    """Resolve which forwarders to provision.

    Returns ``(es, influx)`` where each is a dict of connection details or
    ``None`` when that backend is unavailable. Exits if neither resolves.
    """
    # --skip-logs is the mirror of --skip-metrics, and exists for the same
    # reason in the opposite direction: retrofitting a *metrics* config change
    # needs --name, because that is what supplies the `testnet_name` global tag
    # every existing series is keyed on. Dropping --name to avoid resolving the
    # ES backend would set that tag to the empty string (`name = args.name or
    # ""` below) and sever every series from its history. So the ES forwarder is
    # excluded by flag instead.
    es_url = args.elasticsearch_url
    es_username = args.elasticsearch_username
    es_password = args.elasticsearch_password
    if getattr(args, "skip_logs", False):
        es_url = es_username = es_password = None
    elif not es_url and args.name:
        try:
            state = load_elastic_state(args.name)
            es_url = state.get("elasticsearch_url", "")
            es_username = state.get("elasticsearch_username", "")
            es_password = state.get("elasticsearch_password", "")
        except RuntimeError:
            pass
    es = None
    if es_url and es_username and es_password:
        node_version, branch_commit = _resolve_version_info(args)
        es = {
            "elasticsearch_url": es_url,
            "elasticsearch_username": es_username,
            "elasticsearch_password": es_password,
            "node_version": node_version,
            "branch_commit": branch_commit,
        }

    # --skip-metrics narrows a run to the Elasticsearch forwarder alone. Needed
    # because --name is what supplies the ES index name, but it *also* resolves
    # monitoring state -- so without this there is no way to retrofit a log
    # forwarding change without simultaneously regenerating (and restarting)
    # the host-metrics forwarder on every targeted host.
    influx = None
    if not getattr(args, "skip_metrics", False):
        influx_url = args.influxdb_url
        influx_token = args.influxdb_token
        influx_org = args.influxdb_org
        influx_bucket = args.influxdb_bucket
        if not influx_url and args.name:
            try:
                monitoring = load_monitoring_state(args.name)
                influx_url = monitoring.get("influxdb_url", "")
                influx_token = monitoring.get("influxdb_token", "")
                influx_org = monitoring.get("influxdb_org", influx_org)
                influx_bucket = monitoring.get("influxdb_bucket", influx_bucket)
            except RuntimeError:
                pass
        if influx_url and influx_token:
            influx = {
                "influxdb_url": influx_url,
                "influxdb_token": influx_token,
                "influxdb_org": influx_org,
                "influxdb_bucket": influx_bucket,
            }

    if es is None and influx is None:
        console.print(
            "[bold red]Error:[/bold red] No Telegraf backend resolved. Provide "
            "Elasticsearch and/or InfluxDB credentials, or pass --name for a "
            "deployment that has elastic and/or monitoring state."
        )
        sys.exit(1)

    enabled = []
    if es is not None:
        enabled.append("Elasticsearch logs")
    if influx is not None:
        enabled.append("InfluxDB metrics")
    console.print(f"[dim]Provisioning: {', '.join(enabled)}[/dim]")
    return es, influx


def _make_es_provisioner(args, es, *, host_ips, host_role, ssh_key, ssh_user, name, console):
    if es is None:
        return None
    return TelegrafProvisioner(
        host_ips=host_ips,
        host_role=host_role,
        ssh_key_path=ssh_key,
        ssh_user=ssh_user,
        testnet_name=name,
        elasticsearch_url=es["elasticsearch_url"],
        elasticsearch_username=es["elasticsearch_username"],
        elasticsearch_password=es["elasticsearch_password"],
        branch_commit=es["branch_commit"],
        node_version=es["node_version"],
        from_beginning=args.from_beginning,
        ant_log_dir=args.ant_log_dir,
        console=console,
        **_resolve_telegraf_tuning(args),
    )


def _make_metrics_provisioner(
    influx, *, host_ips, host_role, ssh_key, ssh_user, name,
    bucket=None, console, collect_node_memory=False,
    node_memory_interval_secs=300,
):
    if influx is None:
        return None
    return TelegrafMetricsProvisioner(
        host_ips=host_ips,
        ssh_key_path=ssh_key,
        ssh_user=ssh_user,
        host_role=host_role,
        influxdb_url=influx["influxdb_url"],
        influxdb_token=influx["influxdb_token"],
        influxdb_org=influx["influxdb_org"],
        influxdb_bucket=bucket or influx["influxdb_bucket"],
        testnet_name=name,
        collect_node_memory=collect_node_memory,
        node_memory_interval_secs=node_memory_interval_secs,
        console=console,
    )


def _provision_group(
    args, es, influx, *, ips, role, ssh_key, ssh_user,
    name, console, bucket=None,
):
    """Run one combined pass over a single host group.

    ``bucket`` overrides the metrics destination bucket (used to route CLIENT
    host metrics to the dedicated client bucket).
    """
    if not ips:
        return
    console.print(
        f"[bold]Provisioning Telegraf on {len(ips)} {role} host(s)...[/bold]"
    )
    clear_known_hosts(ips, console)
    es_prov = _make_es_provisioner(
        args, es, host_ips=ips, host_role=role, ssh_key=ssh_key,
        ssh_user=ssh_user, name=name, console=console,
    )
    metrics_prov = _make_metrics_provisioner(
        influx, host_ips=ips, host_role=role, ssh_key=ssh_key,
        ssh_user=ssh_user, name=name,
        bucket=bucket, console=console,
        collect_node_memory=getattr(args, "node_memory_metrics", False),
        node_memory_interval_secs=getattr(args, "node_memory_interval", 300),
    )
    try:
        provision_combined(
            ips, ssh_key, [es_prov, metrics_prov], ssh_user=ssh_user, console=console,
        )
    except Exception as e:
        console.print(
            f"[bold red]Telegraf provisioning failed on {role}:[/bold red] {e}"
        )
        sys.exit(1)


def cmd_provision_telegraf_combined(args):
    """Provision the ES log-forwarder and InfluxDB metrics forwarder in one pass."""
    console = Console()
    es, influx = _resolve_backends(args, console)
    host_role_override = getattr(args, "host_role", None)
    name = args.name or ""

    if args.inventory_path:
        try:
            entries = read_inventory(args.inventory_path)
        except (OSError, ValueError, KeyError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        if host_role_override:
            for entry in entries:
                entry["role"] = host_role_override
        console.print(
            f"[bold]Using {len(entries)} VM(s) from inventory "
            f"{args.inventory_path}...[/bold]"
        )
        clear_known_hosts([e["ip"] for e in entries], console)
        set_hostnames(entries, console)
        for (role, ssh_user, ssh_key), ips in group_by_role_and_credentials(entries).items():
            bucket = INFLUXDB_CLIENT_BUCKET if role == "CLIENT" else None
            _provision_group(
                args, es, influx, ips=ips, role=role,
                ssh_key=ssh_key, ssh_user=ssh_user, name=name,
                bucket=bucket, console=console,
            )
    elif args.host_ips_file:
        try:
            all_ips = read_ips_from_file(args.host_ips_file)
        except (OSError, ValueError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        role = host_role_override or "WAN_NODE"
        bucket = INFLUXDB_CLIENT_BUCKET if role == "CLIENT" else None
        console.print(
            f"[bold]Using {len(all_ips)} host IP(s) from {args.host_ips_file}...[/bold]"
        )
        _provision_group(
            args, es, influx, ips=all_ips, role=role,
            ssh_key=args.ssh_key_path, ssh_user=args.ssh_user,
            name=name, bucket=bucket, console=console,
        )
    elif args.name:
        _provision_by_name(args, es, influx, console)
    else:
        console.print(
            "[bold red]Error:[/bold red] "
            "Either --name, --host-ips-file, or --inventory-path must be provided."
        )
        sys.exit(1)

    console.print()
    console.print(
        "[bold green]Telegraf provisioned successfully on all hosts.[/bold green]"
    )


def _provision_by_name(args, es, influx, console):
    """Provision node-tier (combined) + client-tier (metrics-only) by deployment name."""
    name = args.name
    console.print(f"[bold]Loading deployment state for '{name}'...[/bold]")
    # Testnet deployment state is optional: client-only deployments (prod
    # uploaders / downloaders / canary) have client state but no testnet state.
    try:
        state = load_deployment_state(name)
    except RuntimeError:
        state = None
        console.print(
            f"[dim]No testnet deployment state for '{name}' — "
            "provisioning client VMs only (if any).[/dim]"
        )

    node_found = False
    if state is not None:
        node_found = _provision_nodes_by_name(args, es, influx, state, console)

    # Client host metrics are metrics-only (the client→ES forwarder is installed
    # by `clients provision`). Provision them only when an InfluxDB backend exists.
    client_found = False
    if influx is not None:
        common_kwargs = {
            "ssh_key_path": args.ssh_key_path,
            "ssh_user": args.ssh_user,
            "influxdb_url": influx["influxdb_url"],
            "influxdb_token": influx["influxdb_token"],
            "influxdb_org": influx["influxdb_org"],
            "influxdb_bucket": influx["influxdb_bucket"],
            "testnet_name": name,
            "console": console,
        }
        client_found, client_ok = _provision_client_telegraf(name, common_kwargs, console)
        if not client_ok:
            console.print(
                "[bold red]Error:[/bold red] Telegraf metrics provisioning failed "
                f"on client host(s) for '{name}'."
            )
            sys.exit(1)

    if not node_found and not client_found:
        console.print(
            "[bold red]Error:[/bold red] No node or client VMs found for "
            f"'{name}'. Nothing to provision."
        )
        sys.exit(1)


def _provision_nodes_by_name(args, es, influx, state, console) -> bool:
    """Run the combined pass over a testnet's node tier. Returns True if any host."""
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

    if not (genesis_ips or secondary_ips or regular_ips):
        return False

    ssh_key = args.ssh_key_path
    ssh_user = args.ssh_user
    name = args.name

    _provision_group(
        args, es, influx, ips=genesis_ips, role="GENESIS",
        ssh_key=ssh_key, ssh_user=ssh_user, name=name, console=console,
    )
    _provision_group(
        args, es, influx, ips=secondary_ips, role="BOOTSTRAP",
        ssh_key=ssh_key, ssh_user=ssh_user, name=name, console=console,
    )
    _provision_group(
        args, es, influx, ips=regular_ips, role="WAN_NODE",
        ssh_key=ssh_key, ssh_user=ssh_user, name=name, console=console,
    )
    return True
