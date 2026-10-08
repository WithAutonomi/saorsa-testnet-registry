"""Shared helpers for prod client log forwarding to Elasticsearch.

Prod client deployments have no node-tier Elasticsearch, so when a prod spec
opts into ``forward_logs`` the deploy command stands up a SMALL dedicated ES
instance (mirroring how it stands up its own monitoring stack), installs the
client telegraf forwarder on the client VMs, and — on destroy — tears the ES
instance down unless ``--keep-elastic`` is given.

Client ``ant`` logs land in the ``telegraf-ant-clients-{name}`` index (raw line
as ``message``, tagged role/stream); see ``provisioning/telegraf_client.py``.
"""

from types import SimpleNamespace

from rich.console import Console

from saorsa_deploy.provisioning.telegraf_client import ClientTelegrafProvisioner
from saorsa_deploy.state import load_elastic_state

#: A single prod client deployment forwards little, so the log ES stays small
#: (mirrors the s-2vcpu-4gb monitoring InfluxDB box).
DEFAULT_ELASTIC_DROPLET = "s-2vcpu-4gb"
DEFAULT_ELASTIC_VOLUME_GB = 100
DEFAULT_ELASTIC_RETENTION_DAYS = 7
DEFAULT_ELASTIC_SHARDS = 2

#: Per-role log file globs for each prod service's persistent stdout/stderr.
PROD_UPLOADER_GLOBS = [
    "/var/log/ant-prod-uploader/*.stdout",
    "/var/log/ant-prod-uploader/*.stderr",
]
PROD_DOWNLOADER_GLOBS = [
    "/var/log/ant-prod-downloader/*.stdout",
    "/var/log/ant-prod-downloader/*.stderr",
]
PROD_CANARY_GLOBS = [
    "/var/log/ant-prod-canary/*.stdout",
    "/var/log/ant-prod-canary/*.stderr",
]


def elastic_state_exists(name: str) -> bool:
    try:
        load_elastic_state(name)
        return True
    except RuntimeError:
        return False


def ensure_client_elastic(
    name: str, ssh_key_path: str, console: Console, volume_size: int | None = None
) -> tuple[str, str, str]:
    """Ensure a dedicated client-log ES instance exists; return its creds.

    Creates the ES VM (infra + provision) when absent, idempotently reusing an
    existing one. Returns ``(url, username, password)``.
    """
    from saorsa_deploy.cmd.elastic_infra import cmd_elastic_infra
    from saorsa_deploy.cmd.provision_elastic import cmd_provision_elastic

    if not elastic_state_exists(name):
        console.print(
            "[bold cyan]Creating dedicated Elasticsearch VM for client log forwarding[/bold cyan]"
        )
        cmd_elastic_infra(
            SimpleNamespace(
                name=name,
                provider="digitalocean",
                region=None,
                droplet_size=DEFAULT_ELASTIC_DROPLET,
                volume_size=volume_size or DEFAULT_ELASTIC_VOLUME_GB,
                volume_type=None,
                force=False,
            )
        )
        console.print("[bold cyan]Provisioning Elasticsearch for client log forwarding[/bold cyan]")
        cmd_provision_elastic(
            SimpleNamespace(
                name=name,
                ssh_key_path=ssh_key_path,
                retention_days=DEFAULT_ELASTIC_RETENTION_DAYS,
                shards=DEFAULT_ELASTIC_SHARDS,
            )
        )
    es = load_elastic_state(name)
    return (
        es.get("elasticsearch_url", ""),
        es.get("elasticsearch_username", ""),
        es.get("elasticsearch_password", ""),
    )


def install_client_forwarder(
    *,
    name: str,
    role: str,
    host_ips: list[str],
    tail_globs: list[str],
    es_creds: tuple[str, str, str],
    ssh_key_path: str,
    console: Console,
) -> None:
    """Install the client→ES telegraf forwarder for one role group."""
    url, username, password = es_creds
    if not (url and username and password):
        raise RuntimeError(
            "forward_logs requested but no Elasticsearch credentials are "
            "available — the dedicated client-log ES failed to provision."
        )
    if not host_ips:
        return
    ClientTelegrafProvisioner(
        host_ips=host_ips,
        ssh_key_path=ssh_key_path,
        testnet_name=name,
        elasticsearch_url=url,
        elasticsearch_username=username,
        elasticsearch_password=password,
        role=role,
        tail_globs=tail_globs,
        console=console,
    ).execute()


def destroy_client_elastic(name: str, keep: bool, console: Console) -> None:
    """Tear down the dedicated client-log ES instance unless ``keep`` is set."""
    if keep:
        if elastic_state_exists(name):
            console.print(
                "[dim]Leaving Elasticsearch stack intact (--keep-elastic); "
                "client logs remain queryable.[/dim]"
            )
        return
    if not elastic_state_exists(name):
        return
    from saorsa_deploy.cmd.destroy_elastic import cmd_destroy_elastic

    console.print("[bold cyan]Destroying Elasticsearch (client log forwarding)...[/bold cyan]")
    cmd_destroy_elastic(SimpleNamespace(name=name, force=True))
