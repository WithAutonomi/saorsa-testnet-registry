import sys

from rich.console import Console

from saorsa_deploy.cmd.provision_telegraf import set_hostnames
from saorsa_deploy.provisioning.monitoring import INFLUXDB_CLIENT_BUCKET
from saorsa_deploy.provisioning.telegraf_metrics import TelegrafMetricsProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_client_state, load_monitoring_state


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

    clients = [c for c in client_state.get("clients", []) if c.get("ip")]
    client_ips = [c["ip"] for c in clients]
    if not client_ips:
        return (False, True)

    console.print(
        f"\n[bold]Provisioning Telegraf on {len(client_ips)}"
        f" client VM(s) (host_role=CLIENT, bucket={INFLUXDB_CLIENT_BUCKET})..."
        f"[/bold]"
    )
    clear_known_hosts(client_ips, console)
    # Set the OS hostname BEFORE Telegraf starts. Telegraf captures the
    # hostname at service-start time (omit_hostname=false), and uploader.sh /
    # downloader.sh tag their Influx points with `$(hostname)` too. Only the
    # vultr-client terraform module sets the instance hostname from the VM
    # name; DO / OVH / Hetzner client images boot with a provider-assigned
    # machine-id hostname, so without this their metrics land under an opaque
    # tag instead of the inventory name. Build the canonical client hostname
    # (matching vultr-client/main.tf: `<name>-ant-client-<role>-<region>-<idx>`)
    # and apply it before the Telegraf install below.
    ssh_key = common_kwargs.get("ssh_key_path", "~/.ssh/id_rsa")
    ssh_user = common_kwargs.get("ssh_user", "root")
    hostname_entries = [
        {
            "ip": c["ip"],
            "name": (
                f"{name}-ant-client-"
                f"{c.get('role', 'upload')}-{c['region']}-{c['client_index']}"
            ),
            "ssh_user": ssh_user,
            "ssh_key": ssh_key,
        }
        for c in clients
        if c.get("region") is not None and c.get("client_index") is not None
    ]
    set_hostnames(hostname_entries, console)
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
            "continues. Re-run later with: saorsa-deploy deployments "
            f"provision-telegraf --name {name}[/yellow]"
        )
        return False
    except Exception as e:
        console.print(
            f"[yellow]Client host-metrics provisioning error ({e}); deploy "
            "continues.[/yellow]"
        )
        return False