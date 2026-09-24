"""Pure logic behind ``prod bootstrap upgrade`` / ``prod bootstrap webrtc-addresses``.

The production bootstrap tier is seven hand-placed VMs that every client reaches
the network through. They are upgraded *ahead* of the fleet's own 24h staged
rollout so that a browser client has somewhere to dial from the moment a release
lands, which means this path runs against production with no canary in front of
it -- hence the decisions encoded here.

**The binary path is read from the live unit, never assumed.** The tier was
provisioned before ``bootstrap_production.py``'s ``BINARY_INSTALL_PATH``
(``/usr/local/bin/ant-node``) was what the deploy wrote, and every live host
actually runs ``/opt/ant-node/ant-node``. Installing over the constant would
leave a second, unused binary on disk while the service kept running the old
one -- an upgrade that reports success and changes nothing.

**The unit edit is append-only.** These seven units carry hand-maintained state
-- stale ``--bootstrap`` peers among it, fixed under their own issue -- so this
code adds one argument line and touches nothing else. Rendering a fresh unit
from ``build_systemd_unit`` would silently discard whatever the live file holds.

The SSH, download and firewall I/O lives in ``cmd/prod_bootstrap_upgrade.py``.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from saorsa_deploy.binary_source import GITHUB_REPO, RELEASE_ASSET_NAME

#: The service every bootstrap VM runs (one per host, unlike the node tier).
SERVICE_NAME = "ant-bootstrap"
UNIT_FILE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"

#: The seven production bootstraps. A separate file from the fleet's
#: ``inventory.json``: different hosts, different service, different SSH users.
PROD_BOOTSTRAP_INVENTORY_PATH = (
    "~/.autonomi/testnets/ant-prod-01/bootstrap-inventory.json"
)

#: Where the published multiaddr list lives. Resolved against the package
#: rather than the cwd so the path means the same thing from anywhere, and
#: committed after a release so consumers (the prod WASM client kinds) read it
#: from the source tree rather than needing SSH to the bootstrap tier.
DEFAULT_MULTIADDR_OUT = (
    Path(__file__).resolve().parent
    / "resources"
    / "prod_bootstrap_webrtc_multiaddrs.txt"
)

#: ant-node's flag for pinning the WebRTC-Direct listener. Unset, the port is
#: random per restart, which would invalidate every published multiaddr on every
#: restart. (The testnet bootstrap provisioner passes the ``--webrtc-port``
#: alias; this path uses the canonical spelling.)
WEBRTC_PORT_FLAG = "--webrtc-direct-port"
DEFAULT_WEBRTC_PORT = 10001

#: ant-node writes ``<root_dir>/webrtc-direct.multiaddr`` and logs the listener
#: from 0.20.0 onward. Below that the WebRTC half of the health gate is not a
#: failure, it is not applicable.
WEBRTC_MIN_VERSION = (0, 20, 0)

SUMS_ASSET_NAME = "SHA256SUMS.txt"

#: Log messages the health gate keys on, as ant-node emits them (flat JSON, one
#: object per line, under the unit's ``--log-dir``).
STARTING_MESSAGE = "ant-node starting"
WEBRTC_LISTENING_MESSAGE = "ADR-0015 WebRTC Direct listening"

_VERSION_OUTPUT_RE = re.compile(r"^ant-node\s+(?P<version>\S+)\s*$", re.MULTILINE)
_SUMS_LINE_RE = re.compile(r"^(?P<digest>[0-9a-fA-F]{64})\s+\*?(?P<name>\S+)\s*$")


@dataclass(frozen=True)
class BootstrapUnit:
    """The parts of a live ``ant-bootstrap.service`` this command needs."""

    exec_start: str
    binary_path: str
    root_dir: str
    service_user: str
    log_dir: str | None
    webrtc_port: int | None


def release_urls(version: str) -> tuple[str, str]:
    """``(archive_url, sums_url)`` for a released ant-node version."""
    base = f"https://github.com/{GITHUB_REPO}/releases/download/v{version}"
    return f"{base}/{RELEASE_ASSET_NAME}", f"{base}/{SUMS_ASSET_NAME}"


def parse_sha256sums(text: str) -> dict[str, str]:
    """``{asset name: digest}`` from a ``SHA256SUMS.txt`` body."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        match = _SUMS_LINE_RE.match(line.strip())
        if match:
            sums[match.group("name")] = match.group("digest").lower()
    return sums


def expected_digest(sums_text: str, asset_name: str) -> str:
    """The recorded digest for ``asset_name``, or ``ValueError``.

    An asset absent from the sums file is refused rather than waved through:
    "nothing to check against" and "checked and matched" must not reach the
    seven production hosts by the same code path.
    """
    sums = parse_sha256sums(sums_text)
    digest = sums.get(asset_name)
    if digest is None:
        raise ValueError(
            f"{asset_name!r} is not listed in {SUMS_ASSET_NAME} "
            f"(listed: {', '.join(sorted(sums)) or 'nothing'})"
        )
    return digest


def file_digest(path) -> str:
    """The sha256 of a file on disk, hex."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_version_output(text: str) -> str:
    """The version from ``ant-node --version`` output.

    Raises ``ValueError`` if the output is not ant-node's -- which is what a
    wrong-architecture or truncated download looks like when it runs at all.
    """
    match = _VERSION_OUTPUT_RE.search(text or "")
    if not match:
        first = (text or "").strip().splitlines()[:1]
        raise ValueError(
            f"not ant-node --version output: {(first[0] if first else '')!r}"
        )
    return match.group("version")


def require_version(text: str, version: str) -> None:
    """Raise unless ``ant-node --version`` reports exactly ``version``."""
    found = parse_version_output(text)
    if found != version:
        raise ValueError(f"expected ant-node {version}, got ant-node {found}")


def version_tuple(version: str) -> tuple[int, ...]:
    """The numeric part of a version, for ordering. ``0.20.0-rc.1`` -> (0, 20, 0)."""
    core = (version or "").split("-", 1)[0]
    parts = []
    for piece in core.split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def supports_webrtc(version: str) -> bool:
    """Whether ``version`` publishes a WebRTC-Direct listener and multiaddr."""
    parsed = version_tuple(version)
    return bool(parsed) and parsed >= WEBRTC_MIN_VERSION


def _exec_start_bounds(lines: list[str]) -> tuple[int, int]:
    """``(first, last)`` line indices of the ``ExecStart=`` block, continuations
    included. Raises ``ValueError`` if the unit has no ``ExecStart``."""
    for index, line in enumerate(lines):
        if line.startswith("ExecStart="):
            last = index
            while lines[last].rstrip().endswith("\\") and last + 1 < len(lines):
                last += 1
            return index, last
    raise ValueError("unit has no ExecStart= line")


def _joined_exec_start(lines: list[str], first: int, last: int) -> str:
    body = " ".join(
        line.rstrip().rstrip("\\").strip() for line in lines[first : last + 1]
    )
    return re.sub(r"\s+", " ", body).strip()


def _flag_value(tokens: list[str], flag: str) -> str | None:
    for index, token in enumerate(tokens):
        if token == flag and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def parse_unit(text: str) -> BootstrapUnit:
    """Read a live ``ant-bootstrap.service`` into the fields the upgrade needs.

    Raises ``ValueError`` when the unit is not one this command may touch. The
    two refusals are deliberate: without ``--root-dir`` there is nowhere to read
    the published multiaddr from, and without ``--stop-on-upgrade`` the node's
    own auto-upgrader is live on the host, so a binary swapped in underneath it
    can be replaced again minutes later by whatever the channel is serving.
    """
    lines = text.splitlines()
    first, last = _exec_start_bounds(lines)
    exec_start = _joined_exec_start(lines, first, last)
    tokens = exec_start[len("ExecStart=") :].split()
    if not tokens:
        raise ValueError("unit has an empty ExecStart=")

    if "--stop-on-upgrade" not in tokens:
        raise ValueError(
            "unit does not pass --stop-on-upgrade: the node's own auto-upgrader "
            "is live on this host and would undo the install"
        )
    root_dir = _flag_value(tokens, "--root-dir")
    if not root_dir:
        raise ValueError("unit does not pass --root-dir")

    service_user = "root"
    for line in lines:
        if line.startswith("User="):
            service_user = line[len("User=") :].strip() or "root"
            break

    raw_port = _flag_value(tokens, WEBRTC_PORT_FLAG)
    webrtc_port = int(raw_port) if raw_port and raw_port.isdigit() else None

    return BootstrapUnit(
        exec_start=exec_start,
        binary_path=tokens[0],
        root_dir=root_dir,
        service_user=service_user,
        log_dir=_flag_value(tokens, "--log-dir"),
        webrtc_port=webrtc_port,
    )


def _rejoin(lines: list[str], trailing_newline: bool) -> str:
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def add_webrtc_port(text: str, port: int) -> str:
    """Append ``--webrtc-direct-port <port>`` to the unit's ExecStart.

    Idempotent, and append-only: every other line of the file is left byte for
    byte as it was. A unit already carrying the flag -- at any value -- is
    returned unchanged, because re-pinning a port that clients have already
    cached a multiaddr for is worse than leaving a deliberate choice alone.
    """
    lines = text.splitlines()
    first, last = _exec_start_bounds(lines)
    if WEBRTC_PORT_FLAG in _joined_exec_start(lines, first, last):
        return text

    lines[last] = f"{lines[last].rstrip()} \\"
    lines.insert(last + 1, f"  {WEBRTC_PORT_FLAG} {port}")
    return _rejoin(lines, text.endswith("\n"))


def remove_webrtc_port(text: str) -> str:
    """The exact inverse of :func:`add_webrtc_port`, for the rollback path.

    Reverting the binary without reverting the unit would hand an older
    ant-node a flag it does not know, and a host that will not start at all is
    a worse failure than the one being rolled back from.
    """
    lines = text.splitlines()
    first, last = _exec_start_bounds(lines)
    if WEBRTC_PORT_FLAG not in _joined_exec_start(lines, first, last):
        return text

    for index in range(first, last + 1):
        if not lines[index].strip().startswith(WEBRTC_PORT_FLAG):
            continue
        # The flag has a line of its own, which is how add_webrtc_port writes
        # it: drop the line, and un-continue the one above when it was last.
        was_last = index == last
        del lines[index]
        if was_last:
            lines[index - 1] = lines[index - 1].rstrip().rstrip("\\").rstrip()
        return _rejoin(lines, text.endswith("\n"))

    # A single-line ExecStart carrying the flag inline: drop the two tokens.
    for index in range(first, last + 1):
        tokens = lines[index].split()
        if WEBRTC_PORT_FLAG not in tokens:
            continue
        flag_at = tokens.index(WEBRTC_PORT_FLAG)
        del tokens[flag_at : flag_at + 2]
        lines[index] = " ".join(tokens)
        return _rejoin(lines, text.endswith("\n"))

    return text


@dataclass(frozen=True)
class HealthVerdict:
    """What the post-restart log said."""

    started: bool
    #: ``None`` when the version predates WebRTC-Direct: not a failure, not
    #: applicable. Conflating the two would make a 0.19.0 rehearsal of this
    #: command look like a broken listener.
    webrtc: bool | None
    bind: str | None

    @property
    def ok(self) -> bool:
        return self.started and self.webrtc is not False

    def describe(self) -> str:
        started = "yes" if self.started else "no"
        if self.webrtc is None:
            webrtc = "not expected"
        elif self.webrtc:
            webrtc = f"yes ({self.bind})"
        else:
            webrtc = "no"
        return f"started={started} webrtc={webrtc}"


def evaluate_health(
    log_text: str, version: str, webrtc_port: int, *, expect_webrtc: bool
) -> HealthVerdict:
    """Read a slice of ant-node's JSON log into a health verdict.

    Only a ``version``-matching start line counts: a node that failed to come up
    on the new binary and was restarted onto the old one by ``Restart=always``
    still writes "ant-node starting", and taking that as success is precisely
    the failure this gate exists to catch.
    """
    started = False
    bind: str | None = None
    for line in log_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        if message == STARTING_MESSAGE and record.get("version") == version:
            started = True
        elif message == WEBRTC_LISTENING_MESSAGE:
            candidate = record.get("bind")
            if isinstance(candidate, str) and candidate.endswith(f":{webrtc_port}"):
                bind = candidate

    return HealthVerdict(
        started=started,
        webrtc=(bind is not None) if expect_webrtc else None,
        bind=bind,
    )


def build_log_cursor_command(log_dir: str) -> str:
    """Print ``<newest log path> <size in bytes>``, or nothing if there is none.

    The gate reads only what the restart wrote. Without a cursor, a log holding
    days of this version's own restarts would pass the gate before the service
    had even been asked to stop.
    """
    return (
        f"f=$(ls -1t {log_dir}/*.log 2>/dev/null | head -1); "
        'if [ -n "$f" ]; then echo "$f $(wc -c < "$f")"; fi'
    )


def parse_log_cursor(stdout: str) -> tuple[str | None, int]:
    """``(path, offset)`` from :func:`build_log_cursor_command`'s output."""
    for line in (stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            return parts[0], int(parts[1])
    return None, 0


def build_log_tail_command(log_dir: str, cursor_path: str | None, offset: int) -> str:
    """Print everything written to the newest log since the cursor was taken.

    A rotation between cursor and tail (the day rolling over mid-restart) moves
    the newest file, and the byte offset into the old one is then meaningless --
    so the whole of the new file is read instead.
    """
    if not cursor_path:
        return f"cat $(ls -1t {log_dir}/*.log 2>/dev/null | head -1) 2>/dev/null"
    return (
        f"f=$(ls -1t {log_dir}/*.log 2>/dev/null | head -1); "
        'if [ -z "$f" ]; then exit 0; fi; '
        f'if [ "$f" = "{cursor_path}" ]; then tail -c +{offset + 1} "$f"; '
        'else cat "$f"; fi'
    )
