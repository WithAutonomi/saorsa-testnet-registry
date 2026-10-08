"""Reclaim the disk held open by the Elasticsearch log forwarder.

``telegraf-ant-elasticsearch`` tails ``{LOG_DIR}/ant-*/ant-node*.log*`` with
``watch_method = "inotify"``. An inotify tail keeps an open file descriptor on
every file it has ever matched, and it never lets one go: when the node's log
prune (``log_retention_days``) deletes a rotated ``ant-node.YYYY-MM-DD.log``,
the directory entry disappears but telegraf's fd does not, so the blocks are
never returned to the filesystem.

The result is a host where ``df`` and ``du`` disagree by many GB and the gap
grows without bound — the prune appears to run, the log directory looks small,
and the volume still fills. Measured across ant-prod-01 on 2026-09-15: **943
GiB** held fleet-wide, a mean of 14.3 GiB per host and 152 GiB on the worst,
with files held continuously back to 2026-07-21 — eight weeks after they were
supposedly pruned.

Restarting the forwarder drops every fd and returns the space. It is not a
destructive operation and needs none of the sequencing the service drain in
``prod services rm`` requires: nothing is deleted, no node is touched, and the
only cost is a seconds-long gap in that host's log shipping. It is safe to
re-read from the tail because the deployed config sets ``from_beginning =
false``, so a restarted forwarder resumes at each live file's end rather than
re-shipping its history into Elasticsearch.

This is the reclaim; it is not the fix. The forwarder will begin accumulating
fds again immediately, so the command is a periodic sweep until the underlying
retention behaviour changes.

Builders here emit ``ANT_TG:``-prefixed markers so the output is parsed rather
than scraped, matching :mod:`saorsa_deploy.provisioning.node_services`.
"""

from saorsa_deploy.provisioning.telegraf import TELEGRAF_SERVICE_NAME
from saorsa_deploy.provisioning.volume import MOUNT_POINT

#: Marker prefix for every line these commands emit.
MARKER = "ANT_TG"

#: The unit that tails node logs. The metrics sidecar (``telegraf-ant-metrics``,
#: procstat) holds no log fds and is deliberately never restarted here.
LOG_FORWARDER_SERVICE = TELEGRAF_SERVICE_NAME


def _probe_block() -> str:
    """Shell that emits the three measurement markers for the current state.

    ``lsof +L1`` selects open files whose link count is below 1 — unlinked, but
    still held open, so their blocks are still allocated. Scoped to the unit's
    own MainPID so an unrelated process's held files are never reported as
    reclaimable by restarting the forwarder.

    **``-p`` and ``+L1`` are OR'd, not AND'd.** lsof combines its selection
    options with a union, so ``-p PID +L1`` lists every file of that process
    *plus* every unlinked file, not the intersection. On ant-prod-01's WAW1-4
    that meant 790 rows where only 685 were unlinked, inflating the figure by
    1.9 GiB with the forwarder's live logs, config and sockets. The awk filter
    is what makes the measurement correct: ``$8`` is the NLINK column that
    ``+L`` adds, and only ``NLINK == 0`` rows are genuinely unlinked. ``$5 ==
    "REG"`` drops sockets and pipes, whose NLINK column holds a protocol name
    rather than a number and whose "size" is an offset, not disk.

    Column positions with ``+L`` are: 1 COMMAND, 2 PID, 3 USER, 4 FD, 5 TYPE,
    6 DEVICE, 7 SIZE/OFF, 8 NLINK, 9 NODE, 10 NAME.
    """
    return f"""\
PID=$(systemctl show -p MainPID --value {LOG_FORWARDER_SERVICE} 2>/dev/null || echo 0)
STATE=$(systemctl is-active {LOG_FORWARDER_SERVICE} 2>/dev/null || true)
[ -n "$STATE" ] || STATE=unknown
echo "{MARKER}:STATE:$STATE:$PID"
HELD=0
FDS=0
if [ "${{PID:-0}}" -gt 0 ] && command -v lsof >/dev/null 2>&1; then
  set -- $(lsof -nP -p "$PID" +L1 2>/dev/null | tail -n +2 \
    | awk '$5 == "REG" && $8 == 0 {{s+=$7; n++}} END {{printf "%.0f %d", s+0, n+0}}')
  HELD=${{1:-0}}
  FDS=${{2:-0}}
fi
echo "{MARKER}:HELD:$HELD:$FDS"
DF=$(df -B1 --output=size,used,avail {MOUNT_POINT} 2>/dev/null | tail -1)
echo "{MARKER}:DF:$(echo $DF | tr ' ' ':')"
"""


def build_probe_command() -> str:
    """Measure only: how much unlinked disk the forwarder is holding, and the
    data volume's current free space. Makes no change to the host."""
    return "set -u\n" + _probe_block()


def build_restart_command() -> str:
    """Measure, restart the log forwarder, then measure again.

    Both measurements come from one SSH round trip so the before/after pair
    describes the same host at the same moment — reading "before" in a separate
    earlier pass would attribute unrelated drift to the restart.

    ``lsof`` is required for the *measurement*, not the reclaim, so a host
    without it still gets restarted and simply reports zero held; the after-df
    is what proves the space came back.

    Deliberately absent: any handling of ``telegraf-ant-metrics`` or the stock
    ``telegraf.service``. The first holds no log fds; the second is the distro
    package unit, is not ours, and is left in whatever state it is in.
    """
    return f"""\
set -u
{_probe_block()}
systemctl restart {LOG_FORWARDER_SERVICE}
echo "{MARKER}:RESTARTED:{LOG_FORWARDER_SERVICE}"
AFTER=$(df -B1 --output=size,used,avail {MOUNT_POINT} 2>/dev/null | tail -1)
echo "{MARKER}:DF_AFTER:$(echo $AFTER | tr ' ' ':')"
"""


def _parse_marker(stdout_lines, marker: str, fields: int) -> list[str] | None:
    prefix = f"{MARKER}:{marker}:"
    for raw_line in stdout_lines:
        line = raw_line.strip()
        if not line.startswith(prefix):
            continue
        parts = line[len(prefix) :].split(":")
        return parts if len(parts) == fields else None
    return None


def parse_state(stdout_lines) -> dict | None:
    """Parse ``ANT_TG:STATE:<active|…>:<pid>`` into ``{"state", "pid"}``."""
    parts = _parse_marker(stdout_lines, "STATE", 2)
    if parts is None:
        return None
    try:
        return {"state": parts[0], "pid": int(parts[1])}
    except ValueError:
        return None


def parse_held(stdout_lines) -> dict | None:
    """Parse ``ANT_TG:HELD:<bytes>:<fds>`` into ``{"held_bytes", "fds"}``."""
    parts = _parse_marker(stdout_lines, "HELD", 2)
    if parts is None:
        return None
    try:
        return {"held_bytes": int(parts[0]), "fds": int(parts[1])}
    except ValueError:
        return None


def parse_df(stdout_lines, *, after: bool = False) -> dict | None:
    """Parse ``ANT_TG:DF:`` (or ``ANT_TG:DF_AFTER:``) into byte counts.

    Returns None when df failed on the host, which the empty-field case makes
    look like a short marker rather than zeroes — zeroes would read as "the
    volume is full", the opposite of "we could not measure it".
    """
    parts = _parse_marker(stdout_lines, "DF_AFTER" if after else "DF", 3)
    if parts is None:
        return None
    try:
        return {
            "total_bytes": int(parts[0]),
            "used_bytes": int(parts[1]),
            "free_bytes": int(parts[2]),
        }
    except ValueError:
        return None


def parse_restarted(stdout_lines) -> bool:
    """True when the restart marker is present — i.e. systemctl returned 0.

    ``set -u`` without ``-e`` means a failed restart still lets the script run
    on, so the marker's absence is the signal that the restart itself failed.
    """
    return any(
        line.strip() == f"{MARKER}:RESTARTED:{LOG_FORWARDER_SERVICE}" for line in stdout_lines
    )
