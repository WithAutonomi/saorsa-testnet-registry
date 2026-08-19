import re
import socket
import subprocess
import sys
import time

from rich.console import Console
from rich.panel import Panel

from saorsa_deploy.provisioning.elastic import ElasticProvisioner
from saorsa_deploy.provisioning.es_ingest_proxy import dns_preflight_message
from saorsa_deploy.provisioning.es_log_filter import DEFAULT_MIN_LEVEL
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_elastic_state, update_elastic_state


def _wait_for_ssh(ip: str, ssh_key_path: str, console: Console, timeout: int = 120):
    """Wait until SSH is accepting connections on the host."""
    console.print(f"[dim]Waiting for SSH on {ip}...[/dim]")
    start = time.time()
    while time.time() - start < timeout:
        result = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=5",
                "-o", "BatchMode=yes",
                "-i", ssh_key_path,
                f"root@{ip}",
                "true",
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            console.print(f"[dim]SSH ready on {ip}.[/dim]")
            return
        time.sleep(5)
    console.print(
        f"[yellow]Warning: SSH not ready after {timeout}s, proceeding anyway...[/yellow]"
    )


def _resolve_all(hostname: str) -> list[str]:
    """Every A record for ``hostname`` via the system resolver.

    Note this consults the local stub cache (systemd-resolved, nscd), so it can
    lag a record that has already changed — see ``_resolve_for_acme``.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
    except socket.gaierror:
        return []
    return sorted({info[4][0] for info in infos})


def _dig(hostname: str, server: str) -> list[str] | None:
    """A records from ``server``, or ``None`` if ``dig`` could not be used.

    The empty list and ``None`` mean different things and must not be conflated:
    an authoritative "no such record" is a preflight failure, whereas "dig is not
    installed" only means fall back to the system resolver.
    """
    try:
        result = subprocess.run(
            ["dig", "+short", "+time=3", "+tries=2", f"@{server}", "A", hostname],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    # `dig +short` prints intermediate CNAME targets too; keep only addresses.
    return sorted(
        line.strip()
        for line in result.stdout.splitlines()
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", line.strip())
    )


def _resolve_for_acme(hostname: str) -> tuple[list[str], str]:
    """Resolve the way Let's Encrypt will, not the way this machine happens to.

    The system resolver answers from a local cache, so it can report a record
    that has already been changed — which would block a perfectly good deploy,
    and could equally pass a stale-correct answer when the record is in fact
    wrong. ACME resolves from the public DNS hierarchy, so the preflight should
    too. Observed on the first real deploy: every public resolver returned the
    new address while systemd-resolved still held the old one.
    """
    for server in ("1.1.1.1", "8.8.8.8"):
        addresses = _dig(hostname, server)
        if addresses is not None:
            return addresses, f"public DNS ({server})"
    return _resolve_all(hostname), "system resolver (may be a stale local cache)"


def _preflight_dns(hostname: str, expected_ip: str, console: Console) -> None:
    """Refuse to provision until the hostname points at this instance.

    The certificate is obtained during provisioning, so a wrong record fails
    inside Caddy's ACME client where the cause is buried. Checking first turns
    that into one line naming the record and the address it should carry — and
    costs nothing, because the fix is external and the run has to stop either
    way.
    """
    resolved, source = _resolve_for_acme(hostname)
    problem = dns_preflight_message(hostname, resolved, expected_ip)
    if problem is None:
        console.print(f"[dim]DNS OK ({source}): {hostname} -> {expected_ip}[/dim]")
        return
    console.print("[bold red]Error:[/bold red] DNS is not ready for TLS issuance.")
    console.print()
    console.print(f"[dim](resolved via {source})[/dim]")
    console.print(problem)
    console.print()
    if source.startswith("system resolver"):
        console.print(
            "[dim]`dig` was unavailable, so this answer came from the local "
            "resolver cache and may be out of date. Check with "
            f"`dig @1.1.1.1 +short A {hostname}`; if that is correct, flush "
            "with `sudo resolvectl flush-caches` and re-run.[/dim]"
        )
    console.print(
        "[dim]The infrastructure is already created, so nothing is lost: fix the "
        "record and re-run this command (or `deployments up --resume`), which "
        "will skip straight back to this step.[/dim]"
    )
    sys.exit(1)


def print_connection_summary(name: str, details: dict, console: Console):
    """Print a formatted connection summary for the Elasticsearch stack."""
    es_url = details["elasticsearch_url"]
    es_user = details["elasticsearch_username"]
    es_pass = details["elasticsearch_password"]
    es_api_key = details["elasticsearch_api_key"]
    es_index = details["elasticsearch_index_pattern"]

    if details.get("public_ingest"):
        console.print()
        console.print(
            Panel(
                "\n".join(
                    [
                        f"  [bold]Ingest URL:[/bold]    {es_url}",
                        f"  [bold]Index pattern:[/bold] {es_index}",
                        f"  [bold]Ingest role:[/bold]   "
                        f"{details.get('elasticsearch_ingest_role', '')}",
                        f"  [bold]Admin user:[/bold]    {es_user}",
                        f"  [bold]Admin password:[/bold] {es_pass}",
                        "",
                        "  [dim]Elasticsearch listens on loopback only and 9200 is",
                        "  firewalled: this URL is the sole route in.[/dim]",
                        "",
                        "  [dim]Mint a write-only credential for a community user:[/dim]",
                        f"  saorsa-deploy elastic mint-key --name {name} --user <handle>",
                    ]
                ),
                title=f"Elasticsearch (public ingest): {name}",
                border_style="green",
            )
        )
        return

    lines = [
        f"  [bold]URL:[/bold]           {es_url}",
        f"  [bold]Username:[/bold]      {es_user}",
        f"  [bold]Password:[/bold]      {es_pass}",
        f"  [bold]API Key:[/bold]       {es_api_key}",
        f"  [bold]Index Pattern:[/bold] {es_index}",
        "",
        "  [dim]Telegraf command:[/dim]",
        f"  saorsa-deploy deployments provision-telegraf --name {name}",
        "",
        "  [dim]Query with API key:[/dim]",
        f'  curl -H "Authorization: ApiKey {es_api_key}" {es_url}/{es_index}/_search',
    ]

    console.print()
    console.print(
        Panel(
            "\n".join(lines),
            title=f"Elasticsearch: {name}",
            border_style="green",
        )
    )


def cmd_provision_elastic(args):
    """Provision Elasticsearch on the Elasticsearch VM."""
    console = Console()

    console.print(f"[bold]Loading Elasticsearch state for '{args.name}'...[/bold]")
    try:
        state = load_elastic_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    vm_ip = state.get("vm_ip")
    if not vm_ip:
        console.print(
            "[bold red]Error:[/bold red] No VM IP found in Elasticsearch state."
        )
        sys.exit(1)

    console.print(f"  Elasticsearch VM: {vm_ip}")
    console.print()

    _wait_for_ssh(vm_ip, args.ssh_key_path, console)
    clear_known_hosts([vm_ip], console)

    # Contabo has no attachable block storage → Elasticsearch data on root disk.
    root_disk = state.get("provider") == "contabo"

    # The flag may come from this invocation or from what `elastic infra`
    # recorded. Reading state as well means the second half of a two-step deploy
    # cannot silently provision a plaintext, anonymous-access instance onto
    # infrastructure that was firewalled for public ingest — which would leave
    # the endpoint unreachable and 9200 shut.
    public_ingest = bool(
        getattr(args, "public_ingest", False) or state.get("public_ingest", False)
    )
    ingest_hostname = getattr(args, "ingest_hostname", None) or state.get(
        "elasticsearch_ingest_hostname"
    )
    if public_ingest:
        if not ingest_hostname:
            console.print(
                "[bold red]Error:[/bold red] --ingest-hostname is required for a "
                "public-ingest instance (it is the name the TLS certificate is "
                "issued for)."
            )
            sys.exit(1)
        _preflight_dns(ingest_hostname, vm_ip, console)

    try:
        provisioner = ElasticProvisioner(
            host_ip=vm_ip,
            ssh_key_path=args.ssh_key_path,
            testnet_name=args.name,
            retention_days=args.retention_days,
            shards=args.shards,
            console=console,
            root_disk=root_disk,
            public_ingest=public_ingest,
            ingest_hostname=ingest_hostname,
            letsencrypt_staging=bool(
                getattr(args, "letsencrypt_staging", False)
            ),
            min_log_level=getattr(args, "min_log_level", None)
            or DEFAULT_MIN_LEVEL,
        )
        connection_details = provisioner.execute()
    except Exception as e:
        console.print(
            f"[bold red]Elasticsearch provisioning failed:[/bold red] {e}"
        )
        sys.exit(1)

    # Update Elasticsearch state with connection details
    try:
        update_elastic_state(args.name, connection_details)
        console.print("[dim]Elasticsearch state updated in S3.[/dim]")
    except Exception as e:
        console.print(
            f"[yellow]Warning: Failed to update Elasticsearch state: {e}[/yellow]"
        )

    console.print()
    console.print(
        "[bold green]Elasticsearch provisioned successfully.[/bold green]"
    )

    print_connection_summary(args.name, connection_details, console)
