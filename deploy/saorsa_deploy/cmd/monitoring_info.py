import sys

from rich.console import Console
from rich.panel import Panel

from saorsa_deploy.provisioning.telegraf_metrics import build_telegraf_metrics_config
from saorsa_deploy.state import load_monitoring_state


def cmd_monitoring_info(args):
    """Print monitoring stack connection details."""
    console = Console()

    try:
        state = load_monitoring_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    influxdb_url = state.get("influxdb_url", "")
    influxdb_token = state.get("influxdb_token", "")
    influxdb_org = state.get("influxdb_org", "")
    influxdb_bucket = state.get("influxdb_bucket", "")
    influxdb_client_bucket = state.get("influxdb_client_bucket", "")
    grafana_url = state.get("grafana_url", "")
    grafana_password = state.get("grafana_password", "")

    if not influxdb_url:
        console.print(
            "[bold red]Error:[/bold red] Monitoring stack not yet provisioned. "
            "Run monitoring provision first."
        )
        sys.exit(1)

    if args.telegraf_config:
        # Emit raw Telegraf TOML to stdout — no Rich formatting, pipeable
        config = build_telegraf_metrics_config()
        # Replace env var placeholders with actual values
        config = config.replace("${INFLUXDB_URL}", influxdb_url)
        config = config.replace("${INFLUXDB_TOKEN}", influxdb_token)
        config = config.replace("${INFLUXDB_ORG}", influxdb_org)
        config = config.replace("${INFLUXDB_BUCKET}", influxdb_bucket)
        config = config.replace("${SAORSA_TESTNET_NAME}", args.name)
        print(config)
        return

    grafana_login = f"  (admin / {grafana_password})" if grafana_password else ""
    lines = [
        f"  [bold]InfluxDB:[/bold]  {influxdb_url}",
        f"  [bold]Grafana:[/bold]   {grafana_url}{grafana_login}",
        f"  [bold]Org:[/bold]       {influxdb_org}",
        f"  [bold]Bucket:[/bold]    {influxdb_bucket}  (node host metrics)",
    ]
    if influxdb_client_bucket:
        lines.append(
            f"  [bold]Client Bucket:[/bold] {influxdb_client_bucket}"
            "  (client host metrics)"
        )
    lines.append(f"  [bold]API Token:[/bold] {influxdb_token}")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"Monitoring Stack: {args.name}",
            border_style="green",
        )
    )
