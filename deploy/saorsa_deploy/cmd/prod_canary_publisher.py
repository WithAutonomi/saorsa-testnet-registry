"""CLI entry points for ``prod canary provision-publisher`` /
``prod canary unprovision-publisher``.

Modelled on the Slack reporter's pair: both look up the canary's paired
monitoring stack, SSH to its VM as root, and hand off to
:mod:`saorsa_deploy.provisioning.prod_canary_publisher`.

The ordering of the preflight checks is deliberate. The env file is read and
validated first, naming every missing key at once, because it is the only
check that costs nothing — the registry lookup needs `DATABASE_URL`, the
monitoring-state lookup needs the operator's AWS credentials, and the wheel
build takes a visible amount of time. An operator who got one key wrong should
not discover it after any of that.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.provisioning.prod_canary_publisher import (
    ENV_PATH,
    PUBLISHER_DIR,
    SERVICE_NAME,
    STATE_FILE,
    ProdCanaryPublisherProvisioner,
    ProdCanaryPublisherRemover,
    build_publisher_wheels,
)
from saorsa_deploy.registry import get_repository
from saorsa_deploy.ssh import clear_known_hosts, wait_for_ssh
from saorsa_deploy.state import load_monitoring_state

#: Keys the operator's env file must define. Prefixed so the file can sit
#: alongside other saorsa-deploy configuration without a `DATABASE_URL` meant
#: for the publisher's own (restricted) role being mistaken for the
#: operator's.
REQUIRED_ENV_KEYS = (
    "CANARY_PUBLISHER_DATABASE_URL",
    "CANARY_PUBLISHER_AWS_ACCESS_KEY_ID",
    "CANARY_PUBLISHER_AWS_SECRET_ACCESS_KEY",
)
OPTIONAL_ENV_KEYS = ("CANARY_PUBLISHER_REGION",)
DEFAULT_REGION = "eu-west-2"
DEFAULT_ENV_FILE = "~/.config/saorsa-deploy/canary-publisher.env"


def parse_env_file(text: str) -> dict[str, str]:
    """Parse a ``KEY=value`` file. Blank lines and ``#`` comments ignored,
    surrounding quotes on the value stripped, no shell expansion."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        key, sep, value = stripped.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_publisher_env(env_file: str) -> dict[str, str]:
    """Read and validate the operator's publisher env file.

    Raises :class:`ValueError` naming *every* problem at once — a file with
    two keys missing should take one correction, not two provisioning runs.
    """
    path = Path(os.path.expanduser(env_file))
    if not path.is_file():
        raise ValueError(
            f"env file not found: {path}\n"
            f"It must define: {', '.join(REQUIRED_ENV_KEYS)} "
            f"(optionally {', '.join(OPTIONAL_ENV_KEYS)})."
        )

    parsed = parse_env_file(path.read_text())
    missing = [k for k in REQUIRED_ENV_KEYS if not parsed.get(k)]
    if missing:
        raise ValueError(f"{path} is missing {len(missing)} required key(s): " + ", ".join(missing))

    return {
        "database_url": parsed["CANARY_PUBLISHER_DATABASE_URL"],
        "aws_access_key_id": parsed["CANARY_PUBLISHER_AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": parsed["CANARY_PUBLISHER_AWS_SECRET_ACCESS_KEY"],
        "region": parsed.get("CANARY_PUBLISHER_REGION") or DEFAULT_REGION,
    }


def _load_monitoring(name: str, console: Console) -> dict:
    try:
        return load_monitoring_state(name)
    except RuntimeError:
        console.print(
            f"[bold red]Error:[/bold red] No monitoring stack found for "
            f"'{name}'. The publisher runs on the canary's monitoring VM — "
            "provision the canary first."
        )
        sys.exit(1)


def _require_canary(name: str, console: Console) -> None:
    """Refuse any deployment that is not a prod canary.

    The publisher's whole job is the canary dataset; pointed at another kind
    it would publish an empty one over a good one.
    """
    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set. "
            "This command needs the registry to confirm the deployment's kind."
        )
        sys.exit(1)

    deployment = repo.get_active_by_name(name) or repo.get_by_name(name)
    if deployment is None:
        console.print(f"[bold red]Error:[/bold red] No deployment named '{name}' in the registry.")
        sys.exit(1)

    deploy_args = deployment.deploy_args if isinstance(deployment.deploy_args, dict) else {}
    kind = deploy_args.get("kind", "")
    if kind != "prod-canary":
        console.print(
            f"[bold red]Error:[/bold red] '{name}' is a '{kind or 'unknown'}' "
            "deployment, not a prod-canary. The publisher only publishes the "
            "canary dataset."
        )
        sys.exit(1)


def cmd_prod_canary_provision_publisher(args):
    console = Console()
    console.print(f"[bold]prod canary provision-publisher: {args.name}[/bold]")

    try:
        operator_env = load_publisher_env(args.env_file)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    _require_canary(args.name, console)

    mon_state = _load_monitoring(args.name, console)
    host_ip = mon_state.get("vm_ip", "")
    influx_token = mon_state.get("influxdb_token", "") or ""
    influx_org = mon_state.get("influxdb_org", "ant") or "ant"
    influx_bucket = mon_state.get("influxdb_bucket", "testnet-metrics") or "testnet-metrics"

    if not host_ip:
        console.print("[bold red]Error:[/bold red] monitoring state has no vm_ip.")
        sys.exit(1)
    if not influx_token:
        console.print(
            "[bold red]Error:[/bold red] monitoring state has no Influx "
            "token — run monitoring provision first."
        )
        sys.exit(1)

    title = args.title or args.name

    console.print(
        f"  Monitoring VM: {host_ip}\n"
        f"  Deployment:    {args.name}\n"
        f"  Dataset:       s3://{args.bucket}/{args.prefix}\n"
        f"  Title:         {title}\n"
        f"  Region:        {operator_env['region']}\n"
        f"  On calendar:   {args.on_calendar}\n"
        f"  Run now:       {args.run_now}"
    )

    console.print("[bold]Building wheels...[/bold]")
    try:
        wheels = build_publisher_wheels(console=console)
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] wheel build failed: {e}")
        sys.exit(1)

    clear_known_hosts([host_ip], console)
    not_ready = wait_for_ssh([host_ip], args.ssh_key_path, console=console)
    if not_ready:
        console.print(f"[bold red]Error:[/bold red] SSH never came up on monitoring VM {host_ip}.")
        sys.exit(1)

    provisioner = ProdCanaryPublisherProvisioner(
        host_ip=host_ip,
        deployment_name=args.name,
        bucket=args.bucket,
        prefix=args.prefix,
        title=title,
        database_url=operator_env["database_url"],
        aws_access_key_id=operator_env["aws_access_key_id"],
        aws_secret_access_key=operator_env["aws_secret_access_key"],
        region=operator_env["region"],
        influx_token=influx_token,
        influx_org=influx_org,
        influx_bucket=influx_bucket,
        on_calendar=args.on_calendar,
        wheels=wheels,
        ssh_key_path=args.ssh_key_path,
        console=console,
    )
    try:
        provisioner.execute()
    except Exception as e:
        console.print(f"[bold red]Publisher provisioning failed:[/bold red] {e}")
        sys.exit(1)

    console.print(f"[bold green]Publisher for '{args.name}' is installed.[/bold green]")
    console.print(
        f"[dim]Venv {PUBLISHER_DIR}/venv, env {ENV_PATH}, "
        f"timer {SERVICE_NAME}.timer, state {STATE_FILE}.[/dim]"
    )

    if not args.run_now:
        return

    console.print("[bold]Running the publisher once...[/bold]")
    try:
        exit_code, last_run = provisioner.run_once()
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    if last_run:
        # markup=False: this is JSON the VM wrote, and a value rich reads as a
        # style tag would be dropped from the one record of what just ran.
        console.print(last_run, markup=False, highlight=False)
    else:
        console.print(f"[yellow]No {STATE_FILE} on the VM.[/yellow]")
    if exit_code != 0:
        console.print(f"[bold red]The publisher run failed (exit {exit_code}).[/bold red]")
        sys.exit(exit_code)
    console.print("[bold green]Publisher run completed.[/bold green]")


def cmd_prod_canary_unprovision_publisher(args):
    console = Console()
    console.print(f"[bold]prod canary unprovision-publisher: {args.name}[/bold]")

    mon_state = _load_monitoring(args.name, console)
    host_ip = mon_state.get("vm_ip", "")
    if not host_ip:
        console.print("[bold red]Error:[/bold red] monitoring state has no vm_ip.")
        sys.exit(1)

    clear_known_hosts([host_ip], console)
    not_ready = wait_for_ssh([host_ip], args.ssh_key_path, console=console)
    if not_ready:
        console.print(f"[bold red]Error:[/bold red] SSH never came up on monitoring VM {host_ip}.")
        sys.exit(1)

    remover = ProdCanaryPublisherRemover(
        host_ip=host_ip,
        purge=args.purge,
        ssh_key_path=args.ssh_key_path,
        console=console,
    )
    try:
        remover.execute()
    except Exception as e:
        console.print(f"[bold red]Publisher removal failed:[/bold red] {e}")
        sys.exit(1)

    console.print(f"[bold green]Publisher for '{args.name}' removed.[/bold green]")
    if not args.purge:
        console.print(
            f"[dim]The venv under {PUBLISHER_DIR} and {STATE_FILE} were kept "
            "— pass --purge to remove them too.[/dim]"
        )
