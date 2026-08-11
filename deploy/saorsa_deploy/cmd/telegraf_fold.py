"""Build Telegraf forwarders to fold into a node-tier provisioning pass.

The node-tier provision commands (`nodes provision`, `nodes provision-genesis`,
`bootstraps provision`) can install Telegraf in the SAME pyinfra pass they use to
provision the ant-node services, instead of a second pass via `deployments
provision-telegraf`. This helper resolves the Elasticsearch and InfluxDB
credentials from the deployment's elastic / monitoring state and returns the
matching provisioner objects (each exposing ``add_ops(state, install=...)``) for
the provisioner to fold in.

Resolution is **non-fatal**: a backend whose state is absent is simply skipped,
and if neither resolves the helper warns and returns ``[]`` rather than failing
node provisioning over a monitoring misconfiguration.
"""

from __future__ import annotations

from rich.console import Console

from saorsa_deploy.provisioning.telegraf import TelegrafProvisioner
from saorsa_deploy.provisioning.telegraf_metrics import TelegrafMetricsProvisioner
from saorsa_deploy.state import load_elastic_state, load_monitoring_state


def _parse_drop_log_levels(drop_log_levels: str | None):
    """Return the ``drop_log_levels`` kwarg for ``TelegrafProvisioner``.

    ``None`` -> use the provisioner default (DEBUG,TRACE). An empty string ->
    keep every line. Otherwise a comma-separated, upper-cased list.
    """
    if drop_log_levels is None:
        return None
    return [lvl.strip().upper() for lvl in drop_log_levels.split(",") if lvl.strip()]


def build_node_telegraf_providers(
    name: str,
    *,
    host_ips: list[str],
    host_role: str,
    ssh_key_path: str,
    ssh_user: str = "root",
    drop_log_levels: str | None = None,
    buffer_limit: int | None = None,
    from_beginning: bool = True,
    console: Console | None = None,
) -> list:
    """Resolve ES + InfluxDB telegraf provisioners for a node-tier host group.

    Returns an ordered list (ES log-forwarder first, then InfluxDB metrics) of
    whichever backends have state. Empty when neither does.

    ``from_beginning`` controls whether the ES forwarder's ``tail`` input reads
    each log file from byte 0 (the default, and what every kind has always done)
    or resumes at the end of the existing files. Telegraf keeps no tail offset
    across restarts, so ``True`` means every forwarder restart re-ingests
    whatever logs are still on disk. Pass ``False`` on kinds that restart the
    forwarder as part of normal operation — currently ``prod-debug-nodes``,
    whose daily log prune must restart it to release the descriptors it holds on
    the deleted files.
    """
    console = console or Console()
    providers: list = []

    try:
        elastic = load_elastic_state(name)
    except RuntimeError:
        elastic = None
    if elastic:
        es_url = elastic.get("elasticsearch_url", "")
        es_user = elastic.get("elasticsearch_username", "")
        es_pass = elastic.get("elasticsearch_password", "")
        if es_url and es_user and es_pass:
            metric_buffer = {}
            if buffer_limit is not None:
                metric_buffer["metric_buffer_limit"] = buffer_limit
            providers.append(
                TelegrafProvisioner(
                    host_ips=host_ips,
                    host_role=host_role,
                    ssh_key_path=ssh_key_path,
                    ssh_user=ssh_user,
                    testnet_name=name,
                    elasticsearch_url=es_url,
                    elasticsearch_username=es_user,
                    elasticsearch_password=es_pass,
                    drop_log_levels=_parse_drop_log_levels(drop_log_levels),
                    from_beginning=from_beginning,
                    console=console,
                    **metric_buffer,
                )
            )

    try:
        monitoring = load_monitoring_state(name)
    except RuntimeError:
        monitoring = None
    if monitoring:
        influx_url = monitoring.get("influxdb_url", "")
        influx_token = monitoring.get("influxdb_token", "")
        if influx_url and influx_token:
            providers.append(
                TelegrafMetricsProvisioner(
                    host_ips=host_ips,
                    ssh_key_path=ssh_key_path,
                    ssh_user=ssh_user,
                    host_role=host_role,
                    influxdb_url=influx_url,
                    influxdb_token=influx_token,
                    influxdb_org=monitoring.get("influxdb_org", "ant"),
                    influxdb_bucket=monitoring.get("influxdb_bucket", "testnet-metrics"),
                    testnet_name=name,
                    console=console,
                )
            )

    if not providers:
        console.print(
            "[yellow]--with-telegraf set but no elastic or monitoring state "
            f"found for '{name}'; skipping Telegraf install.[/yellow]"
        )
    return providers
