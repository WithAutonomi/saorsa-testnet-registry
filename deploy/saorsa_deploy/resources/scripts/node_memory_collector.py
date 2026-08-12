#!/usr/bin/env python3
"""Emit Influx line protocol for per-process ant-node memory composition.

Run by Telegraf's ``[[inputs.exec]]`` with ``data_format = "influx"``. Two
invocations at different intervals share this one script:

* ``--source status`` reads ``/proc/<pid>/status`` — VmRSS and its
  anonymous / file-backed / shmem split, plus swap. Measured at ~1 ms per
  process, so it runs often.
* ``--source rollup`` reads ``/proc/<pid>/smaps_rollup`` — PSS, the
  private/shared clean/dirty breakdown, LazyFree and AnonHugePages. Measured at
  **~105 ms per process** on a production node, because the kernel walks page
  tables across the whole 39-80 GB virtual address space (the sparse LMDB map),
  not merely the ~65 VMAs. It also holds the target's ``mmap_lock`` read-side
  for that whole walk, so ``mmap``/``munmap`` in the node stalls behind it.
  Hence the slower interval, and hence ``smaps_rollup`` rather than Telegraf
  procstat's full ``smaps`` collector, which would walk and aggregate every VMA.

Why a new measurement rather than extra fields on ``procstat``: the existing
``procstat.memory_rss`` series and tags must be preserved exactly, and a
separate measurement also makes a field-type collision impossible.

Everything is read-only. Nothing here signals, restarts or otherwise alters a
node process.
"""

import argparse
import subprocess
import sys
import time

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
# LazyFree and AnonHugePages are not in the original specification but are kept
# deliberately: LazyFree is the textbook explanation for resident memory
# collapsing with no PID change (MADV_FREE'd pages stay in RSS until reclaimed
# under pressure), and 276 MB of AnonHugePages was observed on the very process
# that prompted this work. Kernels that do not expose a key simply omit it.
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
}

SOURCES = {
    "status": ("status", STATUS_FIELDS),
    "rollup": ("smaps_rollup", ROLLUP_FIELDS),
}


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


def build_line(service: str, pid: int, fields: dict[str, int], timestamp_ns: int) -> str:
    """Render one Influx line.

    ``pid`` is a **field**, never a tag: a tag would mint a fresh series on
    every node restart, and the whole point of keying on ``node_service`` is
    that ``ant-node-3`` stays one series across its restarts.
    """
    rendered = ",".join(f"{name}={value}i" for name, value in sorted(fields.items()))
    return (
        f"{MEASUREMENT},node_service={escape_tag(service)} "
        f"pid={pid}i,{rendered} {timestamp_ns}"
    )


def collect(source: str, deadline_secs: float, now_ns: int | None = None) -> list[str]:
    """Produce the line-protocol lines for one scrape.

    Every process is read independently and a failure on one is skipped rather
    than aborted on, so a single unreadable process cannot cost the scrape its
    other metrics.

    The deadline bounds total wall-clock: ``smaps_rollup`` costs ~105 ms per
    process, so a host running many services with unusually large address
    spaces could otherwise overrun its collection interval and let scrapes
    stack up. Overrunning returns what was gathered rather than nothing.
    """
    filename, wanted = SOURCES[source]
    started = time.monotonic()
    timestamp_ns = now_ns if now_ns is not None else time.time_ns()

    lines: list[str] = []
    for unit, pid in list_units(timeout=max(1.0, deadline_secs / 4)):
        if time.monotonic() - started >= deadline_secs:
            break
        fields = read_proc(pid, filename, wanted)
        if not fields:
            # None (gone/inaccessible) or empty (nothing exposed): in both
            # cases there is no honest point to emit.
            continue
        service = unit[: -len(".service")] if unit.endswith(".service") else unit
        lines.append(build_line(service, pid, fields, timestamp_ns))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=sorted(SOURCES),
        required=True,
        help="Which /proc file to read: 'status' (cheap) or 'rollup' (costly).",
    )
    parser.add_argument(
        "--deadline-secs",
        type=float,
        default=20.0,
        help="Wall-clock bound on the scrape (default: 20).",
    )
    args = parser.parse_args(argv)

    lines = collect(args.source, args.deadline_secs)
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    # No lines is a legitimate outcome — a host with no node services, or every
    # service stopped — so it is not an error and must not fail the scrape.
    return 0


if __name__ == "__main__":
    sys.exit(main())
