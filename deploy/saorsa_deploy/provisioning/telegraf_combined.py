"""Single-pass Telegraf provisioning.

The Elasticsearch log-forwarder and the InfluxDB host-metrics forwarder used to
be installed in two separate SSH passes over every host (``elastic
provision-telegraf`` then ``monitoring provision-telegraf``). On a large testnet
that doubled the connect/run/disconnect cost and re-installed the same Telegraf
deb twice per host.

:func:`provision_combined` runs both forwarders in **one** pyinfra session per
host group: a single ``connect_all`` → the install op (once) → both provisioners'
operations → a single ``run_ops`` → ``disconnect_all``. Each forwarder keeps its
own systemd unit, config dir and env file, so the two services stay independent —
only the SSH pass and the package install are shared.
"""

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operations import run_ops
from rich.console import Console


def provision_combined(
    host_ips: list[str],
    ssh_key_path: str,
    provisioners: list,
    *,
    ssh_user: str = "root",
    console: Console | None = None,
) -> None:
    """Provision every forwarder in ``provisioners`` on ``host_ips`` in one pass.

    ``provisioners`` is an ordered list of objects exposing
    ``add_ops(state, install=...)`` (``TelegrafProvisioner`` and/or
    ``TelegrafMetricsProvisioner``). The Telegraf install op is added only for the
    first provisioner; the rest pass ``install=False`` so the package is fetched
    once per host. Raises ``RuntimeError`` if any host fails.
    """
    console = console or Console()
    active = [p for p in provisioners if p is not None]
    if not active:
        return

    hosts_data = [(ip, {"ssh_user": ssh_user, "ssh_key": ssh_key_path}) for ip in host_ips]
    inventory = Inventory((hosts_data, {}))
    config = Config()
    if ssh_user != "root":
        config.SUDO = True
        config.USE_SUDO_PASSWORD = False
    state = State(inventory=inventory, config=config)

    console.print(f"Connecting to {len(host_ips)} host(s)...")
    connect_all(state)

    try:
        for index, provisioner in enumerate(active):
            provisioner.add_ops(state, install=index == 0)
        labels = " + ".join(_label(p) for p in active)
        console.print(f"Running Telegraf provisioning operations ({labels})...")
        run_ops(state)
    finally:
        disconnect_all(state)

    failed = state.failed_hosts
    total = len(host_ips)
    succeeded = total - len(failed)
    console.print(f"Telegraf provisioning complete: {succeeded}/{total} hosts succeeded")
    if failed:
        for host in failed:
            console.print(f"  [red]Failed: {host.name}[/red]")
        raise RuntimeError(f"{len(failed)} host(s) failed Telegraf provisioning")


def _label(provisioner) -> str:
    """Short human label for a provisioner, used in the progress line."""
    name = type(provisioner).__name__
    if "Metrics" in name:
        return "InfluxDB metrics"
    return "Elasticsearch logs"
