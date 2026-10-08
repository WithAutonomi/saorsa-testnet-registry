"""Intentional clock-skew injection for client and node VMs.

When enabled on a VM, provisioning disables the OS time-sync service and jumps
the system clock by a fixed magnitude (``SKEW_HOURS``) in a per-host "random"
direction. This is a *test* affordance: the network previously had bugs in its
quotation / connection paths when a client's clock diverged from UTC, and we
deliberately skew clocks to prove that dependence is gone.

Design notes:

- **Two callers, two ways of choosing the offset.**
  - *Clients* (uploaders/downloaders, testnet and prod) supply an **explicit
    signed duration** (e.g. ``2h``, ``-2h``, ``30m``) per VM via
    :func:`parse_skew_duration`; the sign is the direction and the magnitude is
    exact.
  - *Nodes* keep the original **fixed magnitude, random direction** behaviour:
    every selected node shifts by ``SKEW_HOURS`` in a per-host direction decided
    by :func:`skew_direction_for_ip` (a deterministic hash of the host IP).
  Both paths funnel into :func:`build_clock_skew_command`, which takes a single
  signed-seconds offset.
- **Idempotent.** :func:`build_clock_skew_command` guards on a marker file so a
  re-run never *compounds* another offset onto an already-skewed clock.
- **Persistent.** Time sync is disabled before the clock is moved, so the skew
  survives until the VM is destroyed.

The node-side selection (:func:`select_skew_ips`) mirrors ``nat.py``'s
region-distributed, deterministic strategy so re-runs don't flip which hosts
are skewed.
"""

from __future__ import annotations

import hashlib
import re

# Default magnitude of the *node-side* random skew. "A few hours" — large enough
# to break any naive UTC assumption, small enough to stay sane. Clients set their
# own magnitude explicitly via the duration value, so this only governs nodes.
SKEW_HOURS = 3

# Marker file written once the skew has been applied; its presence makes the
# setup command a no-op on subsequent provision runs (prevents compounding).
SKEW_MARKER = "/etc/ant-clock-skew-applied"

# apt validates a mirror's InRelease against the system clock: a rewound clock
# makes the file look "not valid yet" and an advanced clock makes it look
# expired, either way aborting `apt-get update` (and so every later package
# install) on a skewed VM. Disabling the two date checks lets apt keep working
# regardless of the injected skew. Written by build_clock_skew_command on every
# skewed host so the downstream heaptrack / AWS-CLI / Telegraf apt installs run.
SKEW_APT_CONF = "/etc/apt/apt.conf.d/99-clock-skew-tolerance"

# A Go-style duration body: one or more <int><unit> tokens, unit in h/m/s.
_DURATION_TOKEN_RE = re.compile(r"(\d+)([hms])")
_DURATION_BODY_RE = re.compile(r"(?:\d+[hms])+")
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def parse_skew_duration(value) -> int:
    """Parse a Go-style signed duration into a signed number of seconds.

    Accepts an optional leading ``-`` (clock runs slow) or ``+``, followed by one
    or more ``<int><unit>`` tokens where unit is ``h``, ``m`` or ``s`` — e.g.
    ``2h`` → ``7200``, ``-2h`` → ``-7200``, ``30m`` → ``1800``, ``1h30m`` →
    ``5400``, ``90s`` → ``90``.

    Raises :class:`ValueError` on empty input, a zero-magnitude duration, a
    boolean-ish value (``true``/``false`` — the old form, deliberately rejected),
    or any otherwise malformed string.
    """
    if value is None:
        raise ValueError("clock_skew duration is required (e.g. '2h' or '-30m').")
    text = str(value).strip().lower()
    if not text:
        raise ValueError("clock_skew duration cannot be empty (e.g. '2h' or '-30m').")

    negative = False
    body = text
    if text[0] in "+-":
        negative = text[0] == "-"
        body = text[1:]

    if not _DURATION_BODY_RE.fullmatch(body):
        raise ValueError(
            f"Invalid clock_skew '{value}': expected a Go-style duration such as "
            f"'2h', '-2h', '30m' or '1h30m' (units h/m/s, optional leading '-')."
        )

    total = sum(int(n) * _UNIT_SECONDS[u] for n, u in _DURATION_TOKEN_RE.findall(body))
    if total == 0:
        raise ValueError(f"Invalid clock_skew '{value}': the duration must be non-zero.")
    return -total if negative else total


def format_skew_offset(offset_seconds: int) -> str:
    """Render a signed-seconds offset as a compact label like ``+2h`` / ``-1h30m``."""
    sign = "-" if offset_seconds < 0 else "+"
    remaining = abs(int(offset_seconds))
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")
    if not parts:
        parts.append("0s")
    return sign + "".join(parts)


def skew_direction_for_ip(ip: str) -> int:
    """Return +1 (clock runs fast) or -1 (clock runs slow) for ``ip``.

    Deterministic: a hash of the IP decides the direction, so the choice is
    effectively random across hosts yet stable across re-provisions.
    """
    digest = hashlib.blake2b(ip.encode("utf-8"), digest_size=8).digest()
    return 1 if digest[0] % 2 == 0 else -1


def select_skew_ips(vm_ips_by_region: dict[str, list[str]], count: int) -> list[str]:
    """Select up to ``count`` VM IPs to skew, spread across regions.

    Strategy mirrors :func:`saorsa_deploy.provisioning.nat.select_nat_ips`:
    "one per region first, then round-robin". Regions are sorted by key; IPs
    within each region are sorted; lanes are interleaved slot-by-slot so the
    first ``count`` IPs span as many regions as possible.

    Deterministic for a given input — re-running provisioning yields the same
    selection, so a host doesn't flip between skewed and un-skewed.
    """
    if count <= 0 or not vm_ips_by_region:
        return []
    total = sum(len(ips) for ips in vm_ips_by_region.values())
    if total == 0:
        return []
    target = min(total, count)

    sorted_regions = sorted(vm_ips_by_region.keys())
    lanes = [sorted(vm_ips_by_region[r]) for r in sorted_regions]
    max_len = max(len(lane) for lane in lanes)

    ordered: list[str] = []
    for slot in range(max_len):
        for lane in lanes:
            if slot < len(lane):
                ordered.append(lane[slot])

    return ordered[:target]


def build_clock_skew_command(offset_seconds: int) -> str:
    """Return the shell command that skews a host's clock by ``offset_seconds``.

    ``offset_seconds`` is a signed number of seconds — positive makes the clock
    run fast, negative makes it run slow. The command:

    1. Writes the apt date-tolerance drop-in (``SKEW_APT_CONF``) so later
       ``apt-get update`` calls don't reject the mirror's InRelease against the
       skewed clock. Done unconditionally (before the marker short-circuit) so
       it self-heals on hosts skewed by an earlier build that lacked it.
    2. Short-circuits with ``ANT_SKEW:ALREADY`` if the marker file exists.
    3. Disables NTP and stops/disables every common time-sync service.
    4. Jumps the system clock by the offset using ``date -s``.
    5. Records the applied offset (compact label, e.g. ``+2h``) in the marker
       file and prints ``ANT_SKEW:APPLIED:<offset>``.

    Idempotent via the marker file — re-running never compounds the offset; the
    apt drop-in write is itself idempotent (overwrites the same file).
    """
    sign = "+" if offset_seconds >= 0 else "-"
    magnitude = abs(int(offset_seconds))
    date_offset = f"{sign}{magnitude} seconds"
    label = format_skew_offset(offset_seconds)
    # Runs under /bin/sh (dash on Ubuntu) via pyinfra's server.shell, so no
    # bashisms / pipefail. `date -d 'now <offset>'` is GNU coreutils (present
    # on the Ubuntu images we provision).
    return f"""\
set -eu
MARKER="{SKEW_MARKER}"
mkdir -p /etc/apt/apt.conf.d
cat > "{SKEW_APT_CONF}" <<'EOF'
Acquire::Check-Date "false";
Acquire::Check-Valid-Until "false";
EOF
if [ -f "$MARKER" ]; then
    echo "ANT_SKEW:ALREADY"
    exit 0
fi
timedatectl set-ntp false 2>/dev/null || true
for svc in systemd-timesyncd chrony chronyd ntp ntpsec; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
done
NEW_TIME=$(date -d 'now {date_offset}' '+%Y-%m-%d %H:%M:%S')
date -s "$NEW_TIME" >/dev/null
echo "{label}" > "$MARKER"
echo "ANT_SKEW:APPLIED:{label}"
"""
