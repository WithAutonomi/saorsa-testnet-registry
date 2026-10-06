"""``prod bootstrap upgrade`` / ``webrtc-addresses`` / ``set-peers``.

The production bootstrap tier is seven VMs, one ``ant-bootstrap`` service each,
that every client on the network enters through. They are upgraded ahead of the
fleet's own 24h staged rollout so a browser client has a WebRTC-Direct peer to
dial the moment a release ships -- and, in the same restart, have their WebRTC
listener pinned to a fixed port, because ant-node otherwise picks a random one
per start and every published multiaddr would go stale on every restart.

Everything here is serial and gated. Each host is upgraded on its own, is only
accepted once its own log says it came up on the new version with the listener
bound, and is only left in that state once a real ICE connectivity check
reaches it from this machine; anything short of that rolls the host back and
stops the run, leaving the hosts after it untouched. Seven bootstraps restarted
in parallel onto a bad binary is an outage for everyone, which is the whole
reason this is not a loop over ``pyinfra``'s host list.

``set-peers`` rewrites the tier's ``--bootstrap`` peer lists to the current
inventory and is gated the same way, for the same reason: it restarts all seven
of the hosts every client enters the network through, and it must do so one at
a time.

Pure logic -- unit parsing and editing, the health-gate parser, version
ordering -- lives in ``saorsa_deploy/prod_bootstrap_upgrade.py``; the ICE probe
lives in ``saorsa_deploy/webrtc_probe.py``.
"""

import ipaddress
import json
import posixpath
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table

from saorsa_deploy import webrtc_probe
from saorsa_deploy.binary_source import RELEASE_ASSET_NAME
from saorsa_deploy.inventory import NodeVm, load_node_inventory
from saorsa_deploy.prod_bootstrap_upgrade import (
    DEFAULT_MULTIADDR_OUT,
    SERVICE_NAME,
    STARTING_MESSAGE,
    SUMS_ASSET_NAME,
    UNIT_FILE_PATH,
    WEBRTC_PORT_FLAG,
    BootstrapUnit,
    add_webrtc_port,
    build_backup_stat_command,
    build_foreign_owner_command,
    build_log_cursor_command,
    build_log_tail_command,
    build_service_start_command,
    build_started_version_command,
    evaluate_health,
    expected_digest,
    file_digest,
    latest_started_version,
    parse_backup_stat,
    parse_foreign_owners,
    parse_log_cursor,
    parse_service_start_date,
    parse_unit,
    parse_version_output,
    peers_for,
    read_unit,
    release_urls,
    remove_webrtc_port,
    require_version,
    set_bootstrap_peers,
    started_after_restart,
    supports_webrtc,
    unit_peers,
)
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.webrtc_addresses import (
    MULTIADDR_FILENAME,
    address_host,
    parse_multiaddr,
    rewrite_host,
)

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
]

#: How long a restarted bootstrap has to report itself healthy. It rejoins the
#: network on start, so this is not instant even when everything is right.
#: ant-node 0.20.0 opens the WebRTC listener only after that join: measured on
#: the production tier at 1m43s and 2m36s (nrt) and over 3m29s (syd), so the
#: original 180s rolled back a healthy host. After the 2026-09-25 set-peers
#: restart it took 14m26s (fsn1) and 13m35s (ash), which 600s would also have
#: failed; fleet nodes measured up to 11m55s.
HEALTH_TIMEOUT_SECS = 1200
_HEALTH_POLL_SECS = 5

#: One ICE check's deadline, and how many are tried before a host is failed.
PROBE_TIMEOUT_SECS = 5.0
PROBE_ATTEMPTS = 3

#: Settle time between hosts. The tier is small enough that two bootstraps
#: bouncing at once is a noticeable fraction of it.
INTER_HOST_WAIT_SECS = 60


@dataclass
class HostFailure:
    """Why one host was refused, for the summary line."""

    name: str
    reason: str


def _privileged(vm: NodeVm, command: str) -> str:
    """Wrap ``command`` for a host whose SSH user is not root.

    Only the AWS bootstrap is in that position (``ubuntu`` with passwordless
    sudo); the other six log in as root, and wrapping their commands anyway
    would fail on images with no ``sudo`` installed.
    """
    if vm.ssh_user == "root":
        return command
    return f"sudo -n sh -c {shlex.quote(command)}"


def _ssh(
    vm: NodeVm,
    ssh_key: str,
    command: str,
    *,
    timeout: int = 120,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", ssh_key, *_SSH_OPTS, f"{vm.ssh_user}@{vm.ip}", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin,
    )


def _run(
    vm: NodeVm,
    ssh_key: str,
    command: str,
    *,
    timeout: int = 120,
    stdin: str | None = None,
) -> str:
    """Run one command on a host, returning stdout; raise on a non-zero exit."""
    result = _ssh(vm, ssh_key, command, timeout=timeout, stdin=stdin)
    if result.returncode != 0:
        raise RuntimeError(
            f"{vm.name}: command failed ({result.returncode}): "
            f"{result.stderr.strip() or command}"
        )
    return result.stdout


def _resolve_ssh_key(vm: NodeVm, override: str | None) -> str:
    return str(Path(override or vm.ssh_key).expanduser())


def _load_bootstraps(
    inventory_path: str, limit_to: str | None, console: Console
) -> list[NodeVm]:
    """The bootstrap VMs to act on, in inventory order (or ``--limit-to`` order).

    ``require_provider_id=False`` because the bootstrap inventory predates that
    field; nothing here needs it, since no cloud-side volume is touched.
    """
    path = Path(inventory_path).expanduser()
    if not path.is_file():
        console.print(f"[bold red]Error:[/bold red] inventory not found: {path}")
        sys.exit(1)
    try:
        vms = load_node_inventory(path, require_provider_id=False)
    except (ValueError, OSError) as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if not limit_to:
        return vms

    selected: list[NodeVm] = []
    for token in [t.strip() for t in limit_to.split(",") if t.strip()]:
        matches = [
            vm
            for vm in vms
            if token in (vm.name, vm.region) or vm.name.endswith(f"-{token}")
        ]
        if not matches:
            console.print(
                f"[bold red]Error:[/bold red] --limit-to {token!r} matches no host "
                f"in {path}"
            )
            sys.exit(1)
        for vm in matches:
            if vm not in selected:
                selected.append(vm)
    return selected


# --------------------------------------------------------------------------
# Local preflight
# --------------------------------------------------------------------------


def _download(url: str, dest: Path, console: Console) -> None:
    console.print(f"  Fetching {url}")
    response = requests.get(url, timeout=300, stream=True)
    response.raise_for_status()
    with dest.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            handle.write(chunk)


def _preflight_binary(version: str, workdir: Path, console: Console) -> Path:
    """Fetch, checksum and run the release archive locally; return its path.

    The archive verified here is the one uploaded to every host, so all seven
    bootstraps demonstrably run identical bytes -- rather than each fetching
    the release itself and being trusted to have got the same thing.
    """
    archive_url, sums_url = release_urls(version)
    archive = workdir / RELEASE_ASSET_NAME
    sums = workdir / SUMS_ASSET_NAME
    try:
        _download(sums_url, sums, console)
        _download(archive_url, archive, console)
    except requests.RequestException as e:
        console.print(f"[bold red]Error:[/bold red] download failed: {e}")
        sys.exit(1)

    try:
        wanted = expected_digest(sums.read_text(), RELEASE_ASSET_NAME)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    found = file_digest(archive)
    if found != wanted:
        console.print(
            f"[bold red]Error:[/bold red] sha256 mismatch for "
            f"{RELEASE_ASSET_NAME}: expected {wanted}, got {found}"
        )
        sys.exit(1)
    console.print(f"  sha256 [green]OK[/green] {found}")

    extracted = workdir / "extracted"
    extracted.mkdir(exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(extracted)
    binary = extracted / "ant-node"
    if not binary.is_file():
        console.print(
            f"[bold red]Error:[/bold red] {RELEASE_ASSET_NAME} has no 'ant-node' "
            "at its root"
        )
        sys.exit(1)
    binary.chmod(0o755)

    try:
        result = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=60
        )
        require_version(result.stdout, version)
    except (OSError, subprocess.SubprocessError) as e:
        console.print(f"[bold red]Error:[/bold red] could not run the binary: {e}")
        sys.exit(1)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)
    console.print(f"  ant-node [green]{version}[/green] verified locally")
    return archive


# --------------------------------------------------------------------------
# Firewall preflight
# --------------------------------------------------------------------------


def _cli(argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _hetzner_open_port(vm: NodeVm, port: int, console: Console) -> bool:
    server = _cli(["hcloud", "server", "describe", vm.name, "-o", "json"])
    if server.returncode != 0:
        console.print(
            f"  [bold red]{vm.name}[/bold red]: hcloud server describe failed: "
            f"{server.stderr.strip()}"
        )
        return False
    try:
        # hcloud nests the attached firewalls under public_net; there is no
        # top-level key, and reading one reported "no firewall attached" for
        # both production Hetzner bootstraps while theirs dropped udp/10001.
        public_net = json.loads(server.stdout).get("public_net") or {}
        firewall_ids = [str(f["id"]) for f in (public_net.get("firewalls") or [])]
    except (ValueError, KeyError, TypeError) as e:
        console.print(f"  [bold red]{vm.name}[/bold red]: unreadable describe: {e}")
        return False
    if not firewall_ids:
        console.print(f"  {vm.name}: no firewall attached")
        return True

    for firewall_id in firewall_ids:
        described = _cli(["hcloud", "firewall", "describe", firewall_id, "-o", "json"])
        if described.returncode != 0:
            console.print(
                f"  [bold red]{vm.name}[/bold red]: hcloud firewall describe "
                f"{firewall_id} failed: {described.stderr.strip()}"
            )
            return False
        firewall = json.loads(described.stdout)
        name = firewall.get("name") or firewall_id
        existing = [
            rule
            for rule in (firewall.get("rules") or [])
            if rule.get("direction") == "in"
            and rule.get("protocol") == "udp"
            and str(rule.get("port")) == str(port)
        ]
        if existing:
            console.print(f"  {vm.name}: {name} already allows udp/{port}")
            continue
        added = _cli(
            [
                "hcloud", "firewall", "add-rule", firewall_id,
                "--direction", "in",
                "--protocol", "udp",
                "--port", str(port),
                "--source-ips", "0.0.0.0/0",
                "--source-ips", "::/0",
            ]
        )
        if added.returncode != 0:
            console.print(
                f"  [bold red]{vm.name}[/bold red]: could not add udp/{port} to "
                f"{name}: {added.stderr.strip()}"
            )
            return False
        console.print(f"  {vm.name}: [green]added[/green] udp/{port} to {name}")
    return True


def _aws_open_port(vm: NodeVm, port: int, console: Console) -> bool:
    described = _cli(
        [
            "aws", "ec2", "describe-instances",
            "--region", vm.region,
            "--filters", f"Name=ip-address,Values={vm.ip}",
            "--query", "Reservations[].Instances[].SecurityGroups[].GroupId",
            "--output", "json",
        ]
    )
    if described.returncode != 0:
        console.print(
            f"  [bold red]{vm.name}[/bold red]: aws ec2 describe-instances failed: "
            f"{described.stderr.strip()}"
        )
        return False
    try:
        group_ids = json.loads(described.stdout)
    except ValueError as e:
        console.print(f"  [bold red]{vm.name}[/bold red]: unreadable describe: {e}")
        return False
    if not group_ids:
        console.print(
            f"  [bold red]{vm.name}[/bold red]: no instance in {vm.region} has "
            f"public IP {vm.ip}"
        )
        return False

    for group_id in group_ids:
        authorized = _cli(
            [
                "aws", "ec2", "authorize-security-group-ingress",
                "--region", vm.region,
                "--group-id", group_id,
                "--protocol", "udp",
                "--port", str(port),
                "--cidr", "0.0.0.0/0",
            ]
        )
        if authorized.returncode == 0:
            console.print(f"  {vm.name}: [green]added[/green] udp/{port} to {group_id}")
            continue
        if "InvalidPermission.Duplicate" in authorized.stderr:
            console.print(f"  {vm.name}: {group_id} already allows udp/{port}")
            continue
        console.print(
            f"  [bold red]{vm.name}[/bold red]: could not add udp/{port} to "
            f"{group_id}: {authorized.stderr.strip()}"
        )
        return False
    return True


def _preflight_firewalls(vms: list[NodeVm], port: int, console: Console) -> None:
    """Open the WebRTC port everywhere before any host is restarted.

    Restarting a bootstrap onto a listener the provider still blocks would fail
    that host's ICE probe and roll it back, so this runs for the whole tier up
    front rather than per host.
    """
    console.print(f"[bold]Opening udp/{port} in provider firewalls...[/bold]")
    failed = []
    for vm in vms:
        if vm.provider == "hetzner":
            ok = _hetzner_open_port(vm, port, console)
        elif vm.provider == "aws":
            ok = _aws_open_port(vm, port, console)
        elif vm.provider == "vultr":
            # The Vultr group exists in Terraform but is attached to nothing
            # live, so these four hosts have no provider-side filtering at all.
            console.print(f"  {vm.name}: no firewall group attached")
            ok = True
        else:
            console.print(
                f"  [yellow]{vm.name}: no firewall handling for provider "
                f"{vm.provider!r}[/yellow]"
            )
            ok = True
        if not ok:
            failed.append(vm.name)

    if failed:
        console.print(
            f"[bold red]Error:[/bold red] firewall preflight failed for: "
            f"{', '.join(failed)}. Fix, or re-run with --skip-firewall if the "
            "port is already open."
        )
        sys.exit(1)


# --------------------------------------------------------------------------
# Per-host upgrade
# --------------------------------------------------------------------------


def _read_unit(vm: NodeVm, ssh_key: str) -> tuple[str, BootstrapUnit]:
    text = _run(vm, ssh_key, _privileged(vm, f"cat {UNIT_FILE_PATH}"))
    return text, parse_unit(text)


def _install_binary(
    vm: NodeVm, ssh_key: str, unit: BootstrapUnit, archive: Path, version: str
) -> None:
    """Upload, verify on the host, back up the old binary and swap it in.

    **Everything under the binary directory is left owned by the service
    user.** ant-node's own auto-upgrader runs as that user and writes
    ``<binary>.backup`` with ``fs::copy``, which cannot overwrite a file it does
    not own -- so a backup this command left as ``root:root`` (six of the seven
    hosts log in as root, and ``cp`` without ``-p`` takes the invoking user)
    defeated the upgrader permanently. ant-bootstrap-nrt and -syd sat retrying
    hourly on 0.20.0 for the whole of the 0.21.0 rollout for exactly that
    reason. ``chown -R`` on the whole directory, not ``cp -p`` or a chown of the
    one file: ``cp -p`` would preserve a root-owned *source*'s owner, and the
    directory also holds the upgrader's own temp dir and the peers file, which
    are supposed to be the service user's anyway.
    """
    remote_archive = f"/tmp/{RELEASE_ASSET_NAME}"
    scp = subprocess.run(
        [
            "scp", "-i", ssh_key, *_SSH_OPTS,
            str(archive), f"{vm.ssh_user}@{vm.ip}:{remote_archive}",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if scp.returncode != 0:
        raise RuntimeError(f"{vm.name}: upload failed: {scp.stderr.strip()}")

    _run(
        vm,
        ssh_key,
        _privileged(
            vm,
            f"rm -rf /tmp/ant-node.new /tmp/ant-node && "
            f"tar -xzf {remote_archive} -C /tmp/ && "
            f"mv /tmp/ant-node /tmp/ant-node.new && "
            f"chown {unit.service_user}:{unit.service_user} /tmp/ant-node.new && "
            f"chmod +x /tmp/ant-node.new && rm -f {remote_archive}",
        ),
        timeout=300,
    )
    require_version(
        _run(vm, ssh_key, _privileged(vm, "/tmp/ant-node.new --version")), version
    )
    binary_dir = posixpath.dirname(unit.binary_path)
    owner = f"{unit.service_user}:{unit.service_user}"
    _run(
        vm,
        ssh_key,
        _privileged(
            vm,
            f"cp {unit.binary_path} {unit.binary_path}.backup && "
            f"mv /tmp/ant-node.new {unit.binary_path} && "
            f"chown -R {owner} {binary_dir}",
        ),
    )
    # Should never fire: its job is to fail loudly if the chown did not take,
    # rather than leave the host silently un-upgradeable until the next train.
    # Raised from here, so _upgrade_host's except rolls the host back, prints
    # RESTORED and stops the run -- the same path a failed health gate takes.
    foreign = parse_foreign_owners(
        _run(
            vm,
            ssh_key,
            _privileged(vm, build_foreign_owner_command(binary_dir, unit.service_user)),
        )
    )
    if foreign:
        raise RuntimeError(
            f"{vm.name}: entries under {binary_dir} not owned by "
            f"{unit.service_user}: {', '.join(foreign)}"
        )


def _write_unit(vm: NodeVm, ssh_key: str, text: str) -> None:
    _run(
        vm,
        ssh_key,
        _privileged(vm, f"cat > {UNIT_FILE_PATH}"),
        stdin=text,
    )
    _run(vm, ssh_key, _privileged(vm, "systemctl daemon-reload"))


def _health_gate(
    vm: NodeVm,
    ssh_key: str,
    unit: BootstrapUnit,
    version: str,
    webrtc_port: int,
    *,
    expect_webrtc: bool,
    cursor: tuple[str | None, int],
    console: Console,
) -> bool:
    if not unit.log_dir:
        console.print(
            f"  [yellow]{vm.name}: unit has no --log-dir; falling back to "
            "systemctl is-active only[/yellow]"
        )
        return _is_active(vm, ssh_key)

    cursor_path, offset = cursor
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECS
    verdict = None
    while time.monotonic() < deadline:
        time.sleep(_HEALTH_POLL_SECS)
        try:
            log_text = _run(
                vm,
                ssh_key,
                _privileged(vm, build_log_tail_command(unit.log_dir, cursor_path, offset)),
            )
        except (RuntimeError, subprocess.SubprocessError):
            continue
        verdict = evaluate_health(
            log_text, version, webrtc_port, expect_webrtc=expect_webrtc
        )
        if verdict.ok:
            break

    if verdict is None or not verdict.ok:
        described = verdict.describe() if verdict else "no log read"
        console.print(f"  [bold red]{vm.name}: health gate failed[/bold red] ({described})")
        return False

    if not _is_active(vm, ssh_key):
        console.print(f"  [bold red]{vm.name}: {SERVICE_NAME} is not active[/bold red]")
        return False

    console.print(f"  {vm.name}: [green]healthy[/green] ({verdict.describe()})")
    return True


def _is_active(vm: NodeVm, ssh_key: str) -> bool:
    result = _ssh(
        vm, ssh_key, _privileged(vm, f"systemctl is-active {SERVICE_NAME}")
    )
    return result.stdout.strip() == "active"


def _rollback(
    vm: NodeVm, ssh_key: str, unit: BootstrapUnit, original_unit: str, console: Console
) -> None:
    """Put the host back on the binary and unit it had, then say so.

    Best-effort by necessity -- the host is already in a bad state, and a
    failure here must not mask the failure that caused it -- but every step is
    reported so the operator knows exactly what was and was not restored.
    """
    console.print(f"  [yellow]{vm.name}: restoring previous binary and unit...[/yellow]")

    def attempt(description: str, action) -> None:
        try:
            action()
        except (RuntimeError, ValueError, subprocess.SubprocessError) as e:
            console.print(f"  [bold red]{vm.name}: {description} failed: {e}[/bold red]")

    # Chowned for the same reason the install is: a restore that put the old
    # binary back under the backup's owner would leave the live binary owned by
    # root, which is the mirror of the defect being fixed.
    binary_dir = posixpath.dirname(unit.binary_path)
    owner = f"{unit.service_user}:{unit.service_user}"
    attempt(
        "restore binary",
        lambda: _run(
            vm,
            ssh_key,
            _privileged(
                vm,
                f"mv {unit.binary_path}.backup {unit.binary_path} && "
                f"chown -R {owner} {binary_dir}",
            ),
        ),
    )
    # The unit is restored from the text read before the edit, not by editing
    # the edited one back: whatever else went wrong, the file that was there
    # when this command arrived is the one that should be there when it leaves.
    attempt(
        "restore unit",
        lambda: _write_unit(vm, ssh_key, remove_webrtc_port(original_unit)),
    )
    attempt(
        "restart service",
        lambda: _run(vm, ssh_key, _privileged(vm, f"systemctl restart {SERVICE_NAME}")),
    )

    deadline = time.monotonic() + HEALTH_TIMEOUT_SECS
    while True:
        if _is_active(vm, ssh_key):
            console.print(f"  [bold]{vm.name}: RESTORED[/bold]")
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(_HEALTH_POLL_SECS)
    console.print(
        f"  [bold red]{vm.name}: RESTORE INCOMPLETE — {SERVICE_NAME} did not come "
        "back active. Investigate this host before doing anything else.[/bold red]"
    )


def _upgrade_host(
    vm: NodeVm,
    ssh_key: str,
    archive: Path,
    version: str,
    webrtc_port: int,
    *,
    expect_webrtc: bool,
    console: Console,
    probe_advisory: bool = False,
    probe_failures: list[str] | None = None,
) -> str | None:
    """Upgrade one host. Returns ``None`` on success, else the failure reason.

    With ``probe_advisory`` a failed ICE probe keeps the host on the new binary
    and is recorded in ``probe_failures`` instead of rolling it back. The health
    gate stays binding either way.
    """
    console.print(f"[bold]{vm.name}[/bold] ({vm.provider}, {vm.ip})")

    try:
        original_unit, unit = _read_unit(vm, ssh_key)
    except (RuntimeError, ValueError, subprocess.SubprocessError) as e:
        return str(e)
    console.print(f"  binary {unit.binary_path}, root-dir {unit.root_dir}")

    try:
        _install_binary(vm, ssh_key, unit, archive, version)
    except (RuntimeError, ValueError, subprocess.SubprocessError) as e:
        # Nothing has been swapped in yet unless the final move ran, and the
        # backup is only written immediately before it, so a restore is safe
        # either way.
        _rollback(vm, ssh_key, unit, original_unit, console)
        return str(e)

    if not expect_webrtc:
        # Verified against the live tier: ant-node 0.19.0 has no
        # --webrtc-direct-port at all, and clap exits on an unknown argument --
        # so appending it for a pre-0.20.0 version would leave a bootstrap that
        # cannot start, which is the opposite of what a same-version rehearsal
        # of this command is for.
        edited = original_unit
        console.print(
            f"  [yellow]ant-node {version} has no {WEBRTC_PORT_FLAG}; leaving "
            "the unit as it is[/yellow]"
        )
    elif unit.webrtc_port is None:
        edited = add_webrtc_port(original_unit, webrtc_port)
        console.print(f"  appended {WEBRTC_PORT_FLAG} {webrtc_port}")
    else:
        edited = original_unit
        console.print(
            f"  [yellow]{WEBRTC_PORT_FLAG} already pinned to "
            f"{unit.webrtc_port}; leaving it[/yellow]"
        )
        webrtc_port = unit.webrtc_port

    try:
        if edited != original_unit:
            _write_unit(vm, ssh_key, edited)
        cursor = parse_log_cursor(
            _run(
                vm,
                ssh_key,
                _privileged(vm, build_log_cursor_command(unit.log_dir or "/dev/null")),
            )
        )
        _run(vm, ssh_key, _privileged(vm, f"systemctl restart {SERVICE_NAME}"))
    except (RuntimeError, subprocess.SubprocessError) as e:
        _rollback(vm, ssh_key, unit, original_unit, console)
        return str(e)

    if not _health_gate(
        vm,
        ssh_key,
        unit,
        version,
        webrtc_port,
        expect_webrtc=expect_webrtc,
        cursor=cursor,
        console=console,
    ):
        _rollback(vm, ssh_key, unit, original_unit, console)
        return "health gate failed"

    if not expect_webrtc:
        console.print(f"  ICE probe: [yellow]not expected[/yellow] (ant-node {version})")
        return None

    try:
        advertised = address_host(_read_multiaddr(vm, ssh_key))
    except (RuntimeError, ValueError, subprocess.SubprocessError):
        advertised = None
    host = _answering_host(vm, advertised, webrtc_port)
    if host is None:
        console.print(
            f"  [bold red]{vm.name}: ICE probe to {vm.ip}:{webrtc_port} got no "
            "reply[/bold red]"
        )
        if probe_advisory:
            console.print(
                f"  [yellow]{vm.name}: --probe-advisory, keeping ant-node "
                f"{version} (health gate passed)[/yellow]"
            )
            if probe_failures is not None:
                probe_failures.append(vm.name)
            return None
        _rollback(vm, ssh_key, unit, original_unit, console)
        return "ICE probe failed"

    console.print(f"  ICE probe {host}:{webrtc_port}: [green]reachable[/green]")
    return None


def _confirm(vms: list[NodeVm], version: str, port: int, console: Console) -> None:
    console.print(
        f"About to upgrade [bold]{len(vms)}[/bold] production bootstrap(s) to "
        f"ant-node [bold]{version}[/bold] and pin WebRTC to udp/{port}:"
    )
    for vm in vms:
        console.print(f"  {vm.name} ({vm.provider}, {vm.ip})")
    if not sys.stdin.isatty():
        console.print(
            "[bold red]Error:[/bold red] not a TTY and -y was not passed. "
            "Re-run with -y to proceed without confirmation."
        )
        sys.exit(1)
    if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        console.print("Aborted.")
        sys.exit(1)


def cmd_prod_bootstrap_upgrade(args) -> None:
    console = Console()
    version = args.node_version
    webrtc_port = args.webrtc_port
    expect_webrtc = supports_webrtc(version)

    vms = _load_bootstraps(args.inventory_path, args.limit_to, console)
    if not args.yes:
        _confirm(vms, version, webrtc_port, console)

    if not expect_webrtc:
        console.print(
            f"[yellow]ant-node {version} predates WebRTC-Direct: the listener "
            "gate and ICE probe are not expected and will be skipped.[/yellow]"
        )

    with tempfile.TemporaryDirectory(prefix="ant-bootstrap-upgrade-") as tmp:
        console.print(f"[bold]Verifying the ant-node {version} release...[/bold]")
        archive = _preflight_binary(version, Path(tmp), console)

        if args.skip_firewall:
            console.print("[yellow]Skipping the firewall preflight.[/yellow]")
        else:
            _preflight_firewalls(vms, webrtc_port, console)

        clear_known_hosts([vm.ip for vm in vms], console)

        upgraded: list[str] = []
        probe_failures: list[str] = []
        for index, vm in enumerate(vms):
            ssh_key = _resolve_ssh_key(vm, args.ssh_key_path)
            reason = _upgrade_host(
                vm,
                ssh_key,
                archive,
                version,
                webrtc_port,
                expect_webrtc=expect_webrtc,
                console=console,
                probe_advisory=args.probe_advisory,
                probe_failures=probe_failures,
            )
            if reason is not None:
                console.print(
                    f"[bold red]Stopping:[/bold red] {vm.name} failed ({reason}). "
                    f"{len(vms) - index - 1} host(s) left untouched."
                )
                console.print(
                    f"Upgraded before the failure: "
                    f"{', '.join(upgraded) if upgraded else 'none'}"
                )
                sys.exit(1)
            upgraded.append(vm.name)
            if index < len(vms) - 1:
                console.print(f"  settling for {INTER_HOST_WAIT_SECS}s...")
                time.sleep(INTER_HOST_WAIT_SECS)

    console.print(
        f"[green]Upgraded {len(upgraded)} bootstrap(s) to ant-node {version}.[/green]"
    )
    if not expect_webrtc:
        console.print(
            f"[yellow]ant-node {version} publishes no WebRTC multiaddr, so the "
            "address list was not collected.[/yellow]"
        )
        return

    if probe_failures:
        # The published list is every browser client's way in, and its own
        # probe would fail the same way; collecting it is left to
        # webrtc-addresses once the probe is understood.
        console.print(
            f"[bold yellow]ICE probe got no reply from: "
            f"{', '.join(probe_failures)}. Those hosts were kept on {version} "
            "(--probe-advisory). The WebRTC address list was NOT collected; run "
            "`prod bootstrap webrtc-addresses` once the probe is resolved."
            "[/bold yellow]"
        )
        return

    console.print()
    # Collection is over the WHOLE inventory, never the --limit-to subset: the
    # file it writes is the complete published bootstrap set, and rewriting it
    # from two hosts would silently narrow every browser client's way into the
    # network to those two. The all-or-nothing guard inside _collect_multiaddrs
    # protects against an unreachable host, not against a short input list.
    everything = _load_bootstraps(args.inventory_path, None, console)
    if len(everything) != len(vms):
        console.print(
            f"--limit-to upgraded {len(vms)} host(s); collecting WebRTC "
            f"addresses from all {len(everything)} bootstraps."
        )
        clear_known_hosts([vm.ip for vm in everything], console)
    _collect_multiaddrs(
        everything,
        args.inventory_path,
        args.ssh_key_path,
        DEFAULT_MULTIADDR_OUT,
        console,
    )


# --------------------------------------------------------------------------
# webrtc-addresses
# --------------------------------------------------------------------------


def _read_multiaddr(vm: NodeVm, ssh_key: str) -> str:
    """The browser address one bootstrap published, exactly as ant-node wrote it.

    Raises ``RuntimeError`` or ``ValueError`` if the host has nothing usable.
    """
    _, unit = _read_unit(vm, ssh_key)
    raw = _run(
        vm, ssh_key, _privileged(vm, f"cat {unit.root_dir}/{MULTIADDR_FILENAME}")
    )
    return parse_multiaddr(raw)


def _is_public_ip(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _answering_host(vm: NodeVm, advertised: str | None, port: int) -> str | None:
    """The address a browser can actually reach this bootstrap's listener on.

    The inventory IP is tried first, since it is the address clients already
    hold. It is not always the right one: the two Hetzner bootstraps' inventory
    IPs are floating IPs, and the WebRTC listener (bound to 0.0.0.0) answers a
    check sent there from the server's primary IP. The probe discards that
    reply, and so does a browser, because ICE requires the answer to come from
    the address it dialled. ant-node advertises the primary IP, which works, so
    that is tried next, provided it is public. Measured on ant-bootstrap-fsn1
    and -ash, 2026-09-24.
    """
    candidates = [vm.ip]
    if advertised and advertised != vm.ip and _is_public_ip(advertised):
        candidates.append(advertised)
    for host in candidates:
        if webrtc_probe.probe(host, port, PROBE_TIMEOUT_SECS, attempts=PROBE_ATTEMPTS):
            return host
    return None


def _collect_multiaddrs(
    vms: list[NodeVm],
    inventory_path: str,
    ssh_key_override: str | None,
    out_path: Path,
    console: Console,
) -> None:
    """Read, probe and write every bootstrap's WebRTC multiaddr.

    All-or-nothing: a partial list published as the production bootstrap set
    silently narrows every browser client's way in, so a single unreachable
    host leaves the previous file in place rather than replacing it with a
    shorter one.
    """
    console.print(
        f"[bold]Collecting WebRTC-Direct addresses from {len(vms)} bootstrap(s)"
        "...[/bold]"
    )
    addresses: list[str] = []
    failures: list[HostFailure] = []

    for vm in vms:
        ssh_key = _resolve_ssh_key(vm, ssh_key_override)
        try:
            multiaddr = _read_multiaddr(vm, ssh_key)
        except (RuntimeError, ValueError, subprocess.SubprocessError) as e:
            failures.append(HostFailure(vm.name, str(e)))
            console.print(f"  [bold red]{vm.name}: {e}[/bold red]")
            continue

        port = int(multiaddr.split("/")[4])
        advertised = address_host(multiaddr)
        host = _answering_host(vm, advertised, port)
        if host is None:
            tried = vm.ip if advertised == vm.ip else f"{vm.ip} and {advertised}"
            failures.append(
                HostFailure(vm.name, f"ICE probe to {tried}:{port} got no reply")
            )
            console.print(
                f"  [bold red]{vm.name}: ICE probe to {tried}:{port} got no "
                "reply[/bold red]"
            )
            continue

        multiaddr = rewrite_host(multiaddr, host)
        if host != vm.ip:
            console.print(
                f"  [yellow]{vm.name}: {vm.ip} does not answer; publishing the "
                f"advertised {host}[/yellow]"
            )
        addresses.append(multiaddr)
        console.print(f"  {vm.name}: [green]{multiaddr}[/green]")

    if failures:
        console.print(
            f"[bold red]Error:[/bold red] {len(failures)} bootstrap(s) failed: "
            f"{', '.join(f.name for f in failures)}. Not writing {out_path}."
        )
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(addresses) + "\n")
    console.print(
        f"[green]Wrote {len(addresses)} address(es) to[/green] {out_path}"
    )


def cmd_prod_bootstrap_webrtc_addresses(args) -> None:
    console = Console()
    vms = _load_bootstraps(args.inventory_path, getattr(args, "limit_to", None), console)
    clear_known_hosts([vm.ip for vm in vms], console)
    _collect_multiaddrs(
        vms,
        args.inventory_path,
        args.ssh_key_path,
        Path(args.out).expanduser(),
        console,
    )


# --------------------------------------------------------------------------
# set-peers
# --------------------------------------------------------------------------


def _restore_unit(
    vm: NodeVm, ssh_key: str, original_unit: str, console: Console
) -> None:
    """Put back the unit this command found, and restart onto it.

    No binary is involved, so unlike the upgrade's rollback this restores one
    file -- but for the same reason: the unit that was on the host when the
    command arrived is the one that should be there when it leaves.
    """
    console.print(f"  [yellow]{vm.name}: restoring the previous unit...[/yellow]")

    def attempt(description: str, action) -> None:
        try:
            action()
        except (RuntimeError, ValueError, subprocess.SubprocessError) as e:
            console.print(f"  [bold red]{vm.name}: {description} failed: {e}[/bold red]")

    attempt("restore unit", lambda: _write_unit(vm, ssh_key, original_unit))
    attempt(
        "restart service",
        lambda: _run(vm, ssh_key, _privileged(vm, f"systemctl restart {SERVICE_NAME}")),
    )

    deadline = time.monotonic() + HEALTH_TIMEOUT_SECS
    while True:
        if _is_active(vm, ssh_key):
            console.print(f"  [bold]{vm.name}: RESTORED[/bold]")
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(_HEALTH_POLL_SECS)
    console.print(
        f"  [bold red]{vm.name}: RESTORE INCOMPLETE — {SERVICE_NAME} did not come "
        "back active. Investigate this host before doing anything else.[/bold red]"
    )


def _restart_gate(
    vm: NodeVm,
    ssh_key: str,
    unit: BootstrapUnit,
    cursor: tuple[str | None, int],
    console: Console,
) -> bool:
    """Wait for the host to log a start line since ``cursor`` and be active."""
    if not unit.log_dir:
        console.print(
            f"  [yellow]{vm.name}: unit has no --log-dir; falling back to "
            "systemctl is-active only[/yellow]"
        )
        return _is_active(vm, ssh_key)

    cursor_path, offset = cursor
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECS
    started = False
    while True:
        try:
            log_text = _run(
                vm,
                ssh_key,
                _privileged(
                    vm, build_log_tail_command(unit.log_dir, cursor_path, offset)
                ),
            )
        except (RuntimeError, subprocess.SubprocessError):
            log_text = ""
        if started_after_restart(log_text):
            started = True
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(_HEALTH_POLL_SECS)

    if not started:
        console.print(
            f"  [bold red]{vm.name}: no '{STARTING_MESSAGE}' logged since the "
            "restart[/bold red]"
        )
        return False
    if not _is_active(vm, ssh_key):
        console.print(f"  [bold red]{vm.name}: {SERVICE_NAME} is not active[/bold red]")
        return False

    console.print(f"  {vm.name}: [green]healthy[/green] (restarted, active)")
    return True


def _set_peers_host(
    vm: NodeVm, ssh_key: str, peers: list[str], console: Console
) -> str | None:
    """Rewrite one host's peers. Returns ``None`` on success, else the reason."""
    console.print(f"[bold]{vm.name}[/bold] ({vm.provider}, {vm.ip})")

    try:
        original_unit, unit = _read_unit(vm, ssh_key)
    except (RuntimeError, ValueError, subprocess.SubprocessError) as e:
        return str(e)

    current = unit_peers(original_unit)
    edited = set_bootstrap_peers(original_unit, peers)
    if edited == original_unit:
        console.print(
            f"  already lists the {len(peers)} expected peer(s); not restarting"
        )
        return None

    console.print(f"  peers: {', '.join(current) or 'none'}")
    console.print(f"      -> {', '.join(peers)}")

    try:
        _write_unit(vm, ssh_key, edited)
        cursor = parse_log_cursor(
            _run(
                vm,
                ssh_key,
                _privileged(vm, build_log_cursor_command(unit.log_dir or "/dev/null")),
            )
        )
        _run(vm, ssh_key, _privileged(vm, f"systemctl restart {SERVICE_NAME}"))
    except (RuntimeError, subprocess.SubprocessError) as e:
        _restore_unit(vm, ssh_key, original_unit, console)
        return str(e)

    if not _restart_gate(vm, ssh_key, unit, cursor, console):
        _restore_unit(vm, ssh_key, original_unit, console)
        return "restart gate failed"
    return None


def _confirm_set_peers(vms: list[NodeVm], port: int, console: Console) -> None:
    console.print(
        f"About to rewrite the --bootstrap peers of [bold]{len(vms)}[/bold] "
        f"production bootstrap(s) and restart each one:"
    )
    for vm in vms:
        console.print(f"  {vm.name} ({vm.provider}, {vm.ip})")
    console.print(
        f"Each will list every other inventory host on udp/{port}, and never itself."
    )
    if not sys.stdin.isatty():
        console.print(
            "[bold red]Error:[/bold red] not a TTY and -y was not passed. "
            "Re-run with -y to proceed without confirmation."
        )
        sys.exit(1)
    if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        console.print("Aborted.")
        sys.exit(1)


def cmd_prod_bootstrap_set_peers(args) -> None:
    console = Console()

    # The peer list is always computed from the *whole* inventory, never from
    # the --limit-to selection: a host's six peers are the other six
    # bootstraps, whether or not this run is touching them.
    everything = _load_bootstraps(args.inventory_path, None, console)
    vms = _load_bootstraps(args.inventory_path, args.limit_to, console)
    all_ips = [vm.ip for vm in everything]

    if not args.yes:
        _confirm_set_peers(vms, args.peer_port, console)

    clear_known_hosts([vm.ip for vm in vms], console)

    done: list[str] = []
    for index, vm in enumerate(vms):
        ssh_key = _resolve_ssh_key(vm, args.ssh_key_path)
        peers = peers_for(vm.ip, all_ips, args.peer_port)
        reason = _set_peers_host(vm, ssh_key, peers, console)
        if reason is not None:
            console.print(
                f"[bold red]Stopping:[/bold red] {vm.name} failed ({reason}). "
                f"{len(vms) - index - 1} host(s) left untouched."
            )
            console.print(
                f"Rewritten before the failure: "
                f"{', '.join(done) if done else 'none'}"
            )
            sys.exit(1)
        done.append(vm.name)
        if index < len(vms) - 1:
            console.print(f"  settling for {INTER_HOST_WAIT_SECS}s...")
            time.sleep(INTER_HOST_WAIT_SECS)

    console.print(
        f"[green]{len(done)} bootstrap(s) now list the other "
        f"{len(all_ips) - 1} inventory host(s) on udp/{args.peer_port}.[/green]"
    )


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def _status_row(vm: NodeVm, ssh_key: str) -> dict:
    """Everything one bootstrap says about itself, read-only.

    No refusals: a host whose unit ``parse_unit`` would reject, or whose
    binary will not run, still produces a row. ``status`` is the thing an
    operator reads *before* deciding whether a mutating verb may be used, so a
    host it cannot describe is the one it most needs to describe.
    """
    row: dict = {
        "name": vm.name,
        "ip": vm.ip,
        "provider": vm.provider,
        "reachable": True,
        "error": None,
        "unit_ok": False,
        "unit_error": None,
        "service_user": None,
        "binary_path": None,
        "root_dir": None,
        "log_dir": None,
        "webrtc_port": None,
        "stop_on_upgrade": False,
        "on_disk_version": None,
        "running_version": None,
        "active": None,
        "backup": {"present": False, "owner": None, "size": None},
        "foreign_owned": [],
    }

    try:
        unit_text = _run(vm, ssh_key, _privileged(vm, f"cat {UNIT_FILE_PATH}"))
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        row["reachable"] = False
        row["error"] = str(e)
        return row

    facts = read_unit(unit_text)
    row.update(
        unit_ok=facts.error is None,
        unit_error=facts.error,
        service_user=facts.service_user,
        binary_path=facts.binary_path,
        root_dir=facts.root_dir,
        log_dir=facts.log_dir,
        webrtc_port=facts.webrtc_port,
        stop_on_upgrade=facts.stop_on_upgrade,
    )

    errors: list[str] = []

    def read(description: str, command: str) -> str | None:
        try:
            return _run(vm, ssh_key, _privileged(vm, command))
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            errors.append(f"{description}: {e}")
            return None

    if facts.binary_path:
        # The on-disk version is documented as null on failure rather than as
        # an error: a binary that will not run is a finding in its own right,
        # and it is the row's other fields that say why.
        try:
            row["on_disk_version"] = parse_version_output(
                _run(vm, ssh_key, _privileged(vm, f"{facts.binary_path} --version"))
            )
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
            row["on_disk_version"] = None

    if facts.log_dir:
        # The start line is looked for only in the log of the day the running
        # process started: see build_started_version_command for why the newest
        # file is the wrong one and why walking back from it is far too much
        # reading. No date, or no log for it, means no running version.
        started_on = parse_service_start_date(
            read("read the service start time", build_service_start_command()) or ""
        )
        if started_on:
            log_line = read(
                "read the start line",
                build_started_version_command(facts.log_dir, started_on),
            )
            if log_line is not None:
                row["running_version"] = latest_started_version(log_line)

    # Via _ssh, not _run: `is-active` exits non-zero for every state but
    # `active`, and `inactive` or `failed` is an answer, not a failed read.
    active = _ssh(vm, ssh_key, _privileged(vm, f"systemctl is-active {SERVICE_NAME}"))
    row["active"] = active.stdout.strip() or None

    if facts.binary_path:
        stat_out = _ssh(vm, ssh_key, _privileged(vm, build_backup_stat_command(facts.binary_path)))
        # A missing backup exits non-zero, which is a fact about the host and
        # not a failed read: a bootstrap whose upgrader has never run has none.
        row["backup"] = parse_backup_stat(stat_out.stdout if stat_out.returncode == 0 else "")

        directory = posixpath.dirname(facts.binary_path)
        foreign = read(
            "find foreign-owned entries",
            build_foreign_owner_command(directory, facts.service_user),
        )
        if foreign is not None:
            row["foreign_owned"] = parse_foreign_owners(foreign)

    if errors:
        row["reachable"] = False
        row["error"] = "; ".join(errors)
    return row


def _status_table(rows: list[dict]) -> Table:
    table = Table(title="Production bootstrap tier")
    for column in (
        "host",
        "on disk",
        "running",
        "active",
        "webrtc",
        "stop-on-upgrade",
        "backup owner",
        "foreign",
        "error",
    ):
        table.add_column(column, overflow="fold")
    for row in rows:
        backup = row["backup"]
        table.add_row(
            row["name"],
            row["on_disk_version"] or "-",
            row["running_version"] or "-",
            row["active"] or "-",
            str(row["webrtc_port"]) if row["webrtc_port"] else "-",
            "yes" if row["stop_on_upgrade"] else "no",
            backup["owner"] or ("-" if backup["present"] else "absent"),
            str(len(row["foreign_owned"])),
            row["error"] or row["unit_error"] or "",
        )
    return table


def cmd_prod_bootstrap_status(args) -> None:
    """Report the production bootstrap tier. Reads only; changes nothing.

    The exit code says whether the *report* is complete, not whether the tier
    is healthy: nothing here is a gate, and a version or an owner the caller
    does not like is for the caller to compare. The one incomplete answer is a
    host that did not respond, which is why that -- and only that -- exits 1.
    """
    # With --json the array is the only thing on stdout, so jq can consume it:
    # progress, warnings and the table's own heading all go to stderr.
    console = Console(stderr=bool(args.json))
    vms = _load_bootstraps(args.inventory_path, args.limit_to, console)
    clear_known_hosts([vm.ip for vm in vms], console)

    rows = []
    for vm in vms:
        console.print(f"Reading {vm.name} ({vm.provider}, {vm.ip})...")
        rows.append(_status_row(vm, _resolve_ssh_key(vm, args.ssh_key_path)))

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        # Rich assumes an 80-column terminal when stdout is not a tty, which
        # squeezes nine columns into unreadable slivers -- and a report tee'd
        # into a maintenance run's log is still meant to be read.
        table_console = console if console.is_terminal else Console(width=160)
        table_console.print(_status_table(rows))

    unreachable = [row["name"] for row in rows if not row["reachable"]]
    if unreachable:
        console.print(
            f"[bold red]{len(unreachable)} host(s) could not be read: "
            f"{', '.join(unreachable)}. This report is incomplete.[/bold red]"
        )
        sys.exit(1)
