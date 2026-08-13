#!/usr/bin/env python3
"""Emit Influx line protocol for per-process ant-node memory composition.

Run by the `ant-node-memory-collector` systemd timer, which pipes stdout to
InfluxDB via `ant-node-memory-publish`. Deliberately NOT a Telegraf
``inputs.exec``: that route measured ~14.9% of one core on a production node,
of which ~12.5% was telegraf overhead that could not be accounted for.

This supplies only the fields Telegraf's native `procstat` mmap collection
cannot produce — the RSS backing split, the PSS backing split, LazyFree and the
huge-page set. The routine economical signal comes from procstat itself.

* ``/proc/<pid>/status`` — VmRSS and its anonymous / file-backed / shmem split,
  plus swap. ~1 ms per process.
* ``/proc/<pid>/smaps_rollup`` — PSS, the private/shared clean/dirty breakdown,
  LazyFree and the huge-page fields. **~105 ms per process** on a production
  node, because the kernel walks page tables across the whole 39-80 GB virtual
  address space (the sparse LMDB map), not merely the ~65 VMAs. It also holds
  the target's ``mmap_lock`` read-side for that walk, so ``mmap``/``munmap`` in
  the node stalls behind it — hence a coarse timer cadence rather than a fast
  scrape, and hence ``smaps_rollup`` rather than the full ``smaps``.

Because this posts to InfluxDB directly rather than through Telegraf, it must
attach the global tags itself — see :func:`resolve_tags`. Missing ``host`` in
particular merges every machine's ``ant-node-1`` into one series and makes
``record-analysis`` discard the points entirely.

Everything is read-only. Nothing here signals, restarts or otherwise alters a
node process.
"""

import argparse
import os
import socket
import subprocess
import sys
import time
from typing import NamedTuple

MEASUREMENT = "ant_node_memory"

# The same unit set the Telegraf config's four `[[inputs.procstat]]` blocks
# cover, so `node_service` tag values line up exactly with the existing
# per-service RSS series. A pattern matching nothing on a given host is
# harmless — production bootstraps run an un-indexed `ant-bootstrap.service`
# that the hyphenated glob does not match, which is why both forms appear.
UNIT_PATTERNS = (
    "ant-node-*.service",
    "ant-bootstrap-*.service",
    "ant-bootstrap.service",
    "ant-genesis-node.service",
)

# /proc/<pid>/status key -> emitted field name.
STATUS_FIELDS = {
    "VmRSS": "vm_rss",
    "RssAnon": "rss_anon",
    "RssFile": "rss_file",
    "RssShmem": "rss_shmem",
    "VmSwap": "vm_swap",
}

# /proc/<pid>/smaps_rollup key -> emitted field name.
#
# Prefixed `rollup_` because `Rss` here and `VmRSS` from status are sampled at
# different moments and do not agree exactly; giving them one name would invite
# reading a sampling artefact as an accounting discrepancy.
#
# `LazyFree` is the reason this source exists at all: `MADV_FREE`'d pages stay
# resident until the kernel reclaims them under pressure, which produces exactly
# V2-970's shape — RSS grows, then collapses with the PID unchanged. Note it is
# a LOWER BOUND: the kernel documents that implementation optimisations can make
# it under-report, so a non-zero or collapsing value is evidence of MADV_FREE
# while zero does not exclude it.
#
# The three `*PmdMapped` / `AnonHugePages` fields must be collected together.
# `AnonHugePages` covers anonymous THP only, so on its own it can say nothing
# about a file-backed mapping such as the LMDB chunk store; `FilePmdMapped` and
# `ShmemPmdMapped` are the file-backed and shared-memory counterparts.
#
# `Pss_Anon` / `Pss_File` / `Pss_Shmem` give a cleaner backing-type split than
# `Rss - Anonymous`, which silently folds shmem into the non-anonymous side.
# PSS is proportional accounting, so it still does not attribute residency to a
# particular file — that needs a pathname-level `smaps` snapshot.
ROLLUP_FIELDS = {
    "Rss": "rollup_rss",
    "Pss": "rollup_pss",
    "Pss_Anon": "rollup_pss_anon",
    "Pss_File": "rollup_pss_file",
    "Pss_Shmem": "rollup_pss_shmem",
    "Pss_Dirty": "rollup_pss_dirty",
    "Anonymous": "rollup_anonymous",
    "Private_Clean": "rollup_private_clean",
    "Private_Dirty": "rollup_private_dirty",
    "Shared_Clean": "rollup_shared_clean",
    "Shared_Dirty": "rollup_shared_dirty",
    "Referenced": "rollup_referenced",
    "Swap": "rollup_swap",
    "SwapPss": "rollup_swap_pss",
    "LazyFree": "rollup_lazy_free",
    "AnonHugePages": "rollup_anon_huge_pages",
    "FilePmdMapped": "rollup_file_pmd_mapped",
    "ShmemPmdMapped": "rollup_shmem_pmd_mapped",
}

SOURCES = {
    "status": ("status", STATUS_FIELDS),
    "rollup": ("smaps_rollup", ROLLUP_FIELDS),
}

# Read both files in one pass, emitting a single point per service. This is the
# mode the systemd timer uses: the routine economical signal now comes from
# telegraf's native procstat mmap collection, so this collector exists only to
# supply the fields procstat cannot produce, and there is no reason to walk the
# process list twice to get them.
ALL_SOURCES = ("status", "rollup")


def parse_systemctl_show(output: str) -> list[tuple[str, int]]:
    """Parse ``systemctl show`` output into ``(unit_id, main_pid)`` pairs.

    Records are separated by a blank line. Only units that are both active and
    carry a real MainPID are returned: an inactive or not-yet-started unit has
    ``MainPID=0``, and reporting it would either fabricate zeros or, worse,
    attribute another process's memory to it once the PID were reused.
    """
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        key, sep, value = line.partition("=")
        if sep:
            current[key] = value
    if current:
        records.append(current)

    units: list[tuple[str, int]] = []
    for record in records:
        unit = record.get("Id")
        if not unit or record.get("ActiveState") != "active":
            continue
        try:
            pid = int(record.get("MainPID", "0"))
        except ValueError:
            continue
        if pid > 0:
            units.append((unit, pid))
    return units


def list_units(timeout: float) -> list[tuple[str, int]]:
    """Resolve every ant node-tier unit and its current MainPID.

    One ``systemctl show`` call for all patterns (measured at 0.02 s) rather
    than one per unit. Resolving the PID fresh on every scrape is what keeps
    attribution correct across restarts.
    """
    try:
        completed = subprocess.run(
            [
                "systemctl", "show",
                "--property=Id",
                "--property=MainPID",
                "--property=ActiveState",
                *UNIT_PATTERNS,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return parse_systemctl_show(completed.stdout)


def parse_proc_fields(text: str, wanted: dict[str, str]) -> dict[str, int]:
    """Extract ``wanted`` keys from a ``Key:  <n> kB`` /proc file, as bytes.

    Both source files report memory in kB. Converting here — once, at the only
    point that knows the source unit — is what keeps every stored value an
    integer count of bytes. A field key whose type varies is rejected by
    InfluxDB for the life of the shard, so the unit and the type are fixed here
    deliberately rather than left to each caller.

    Keys the kernel does not expose are simply absent from the result; no zero
    is invented for them.
    """
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        field = wanted.get(key)
        if field is None:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            kilobytes = int(parts[0])
        except ValueError:
            continue
        values[field] = kilobytes * 1024
    return values


def read_proc(pid: int, filename: str, wanted: dict[str, str]) -> dict[str, int] | None:
    """Read one process's /proc file, or ``None`` if it cannot be read.

    ``None`` and an empty dict are distinct: the first means the process went
    away or is inaccessible and must produce no point at all, the second means
    the file was readable but held nothing wanted.
    """
    try:
        with open(f"/proc/{pid}/{filename}", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except (OSError, ValueError):
        # Covers the process exiting mid-scrape (ENOENT/ESRCH) and permission
        # failures alike. Either way there is nothing truthful to report.
        return None
    return parse_proc_fields(text, wanted)


def escape_tag(value: str) -> str:
    """Escape a line-protocol tag value.

    Service names are plain (``ant-node-3``), but escaping unconditionally
    means a future unit name cannot silently corrupt the line.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace("=", "\\=")
        .replace(" ", "\\ ")
    )


def resolve_tags(host: str | None = None) -> dict[str, str]:
    """Tags applied to every point, matching what Telegraf attaches natively.

    This collector posts to InfluxDB directly rather than through Telegraf, so
    nothing else supplies the global tags. **`host` is not optional**: without
    it every host's `ant-node-1` collapses into one series, so a fleet-wide
    rollout would silently merge 66 machines' services together — and
    `record-analysis` drops any point lacking a host, so the archival path
    produces nothing at all.

    `host` must match what Telegraf writes for the same machine, or the two
    measurements cannot be joined. Telegraf uses the system hostname, so this
    does too; ``ANT_HOSTNAME`` overrides for the case where they ever diverge.

    ``testnet_name`` and ``host_role`` mirror Telegraf's ``[global_tags]`` and
    come from the same env file the unit already loads, so the tag set is
    identical across both measurements.
    """
    tags = {"host": host or os.environ.get("ANT_HOSTNAME") or socket.gethostname()}
    for env_name, tag_name in (
        ("ANT_TESTNET_NAME", "testnet_name"),
        ("ANT_HOST_ROLE", "host_role"),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            tags[tag_name] = value
    return tags


def build_line(
    service: str,
    pid: int,
    fields: dict[str, int],
    timestamp_ns: int,
    tags: dict[str, str] | None = None,
) -> str:
    """Render one Influx line.

    ``pid`` is a **field**, never a tag: a tag would mint a fresh series on
    every node restart, and the whole point of keying on ``node_service`` is
    that ``ant-node-3`` stays one series across its restarts.

    Tags are emitted in sorted order so a line is deterministic — which makes
    the output diffable and the tests exact.
    """
    all_tags = dict(tags or {})
    all_tags["node_service"] = service
    tag_text = ",".join(
        f"{escape_tag(k)}={escape_tag(v)}" for k, v in sorted(all_tags.items())
    )
    rendered = ",".join(f"{name}={value}i" for name, value in sorted(fields.items()))
    return f"{MEASUREMENT},{tag_text} pid={pid}i,{rendered} {timestamp_ns}"


class CollectionResult(NamedTuple):
    """Lines to emit, plus enough context to tell empty apart from broken.

    A host with no node services and a host where every read was refused both
    produce zero lines, and conflating them is how a total failure hides: the
    `smaps_rollup` source silently returned nothing on every scrape because
    reading it needs ptrace access the unprivileged telegraf user lacks, while
    the world-readable `status` source kept reporting normally.
    """

    lines: list[str]
    units_seen: int
    units_read: int

    @property
    def total_failure(self) -> bool:
        """Units existed and not one of them could be read."""
        return self.units_seen > 0 and self.units_read == 0


def collect(
    source: str,
    deadline_secs: float,
    now_ns: int | None = None,
    tags: dict[str, str] | None = None,
) -> "CollectionResult":
    """Produce the line-protocol lines for one scrape.

    Every process is read independently and a failure on one is skipped rather
    than aborted on, so a single unreadable process cannot cost the scrape its
    other metrics.

    The deadline bounds total wall-clock: ``smaps_rollup`` costs ~105 ms per
    process, so a host running many services with unusually large address
    spaces could otherwise overrun its collection interval and let scrapes
    stack up. Overrunning returns what was gathered rather than nothing.
    """
    sources = ALL_SOURCES if source == "both" else (source,)
    started = time.monotonic()
    timestamp_ns = now_ns if now_ns is not None else time.time_ns()

    lines: list[str] = []
    units_seen = 0
    units_read = 0
    for unit, pid in list_units(timeout=max(1.0, deadline_secs / 4)):
        if time.monotonic() - started >= deadline_secs:
            break
        units_seen += 1
        # Merged across sources so a service produces ONE point per scrape
        # rather than one per file, keeping the series count down and making
        # the reconciliation checks (composition sums to VmRSS, PSS <= RSS)
        # readable from a single row.
        fields: dict[str, int] = {}
        for name in sources:
            filename, wanted = SOURCES[name]
            got = read_proc(pid, filename, wanted)
            if got:
                fields.update(got)
        if not fields:
            # Every source gone/inaccessible/empty: no honest point to emit.
            continue
        units_read += 1
        service = unit[: -len(".service")] if unit.endswith(".service") else unit
        lines.append(build_line(service, pid, fields, timestamp_ns, tags))
    return CollectionResult(lines, units_seen, units_read)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=[*sorted(SOURCES), "both"],
        default="both",
        help="Which /proc file(s) to read: 'status' (cheap), 'rollup' (costly), "
        "or 'both' (default — one merged point per service).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Value for the `host` tag (default: ANT_HOSTNAME, else the system "
        "hostname). Must match what Telegraf writes for this machine, or the "
        "two measurements cannot be joined.",
    )
    parser.add_argument(
        "--deadline-secs",
        type=float,
        default=20.0,
        help="Wall-clock bound on the scrape (default: 20).",
    )
    args = parser.parse_args(argv)

    result = collect(args.source, args.deadline_secs, tags=resolve_tags(args.host))
    if result.lines:
        sys.stdout.write("\n".join(result.lines) + "\n")

    # No lines is a legitimate outcome — a host with no node services, or every
    # service stopped — so it is not an error and must not fail the scrape.
    #
    # But every service being unreadable is NOT legitimate, and reporting it as
    # silence is how a whole source can go missing unnoticed. Telegraf logs a
    # non-zero exec exit, which turns an invisible gap into something the
    # journal shows on the first scrape. Emitted only on total failure so that
    # a service exiting mid-scrape stays quiet.
    if result.total_failure:
        filename = (
            "status+smaps_rollup" if args.source == "both"
            else SOURCES[args.source][0]
        )
        sys.stderr.write(
            f"ant-node-memory-collector: {result.units_seen} node service(s) "
            f"found but /proc/<pid>/{filename} was unreadable for every one. "
            f"Reading smaps_rollup needs ptrace access to the target "
            f"(CAP_SYS_PTRACE); /proc/<pid>/status does not, so a partial "
            f"failure here looks like the cheap source working normally.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
