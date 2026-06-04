#!/usr/bin/env python3
"""Hourly probe service for prod canary uploads.

Each probe VM owns a hash-partition of the canary set, identified by
``PROBE_SHARD_INDEX`` (0-based) of ``PROBE_SHARD_COUNT``. A canary is owned
by shard ``i`` when ``blake3(canary_id)[0] % PROBE_SHARD_COUNT == i``.

Per cycle the service:

1. Pulls every ``prod_canary_upload`` record from Influx that this shard
   owns and ingests new ones into local SQLite at
   ``/var/lib/ant-prod-canary/probe-state.db``.
2. Runs ``ant --json file download <top_address>`` against every
   still-alive canary, with up to ``PROBE_CONCURRENCY`` concurrent
   downloads.
3. Parses each download's outcome and emits a ``prod_canary_probe`` Influx
   point. Local SQLite tracks a *two-strike* rule: a canary is locally
   marked ``lost`` only after two consecutive failed probes from this VM
   that report the **same** ``missing_chunk_address``.
4. Sleeps until the top of the next hour, then loops.

If the SQLite file is missing on startup, it is rebuilt from Influx so a
redeployed shard can pick up exactly where its predecessor left off.

Required env:
    BOOTSTRAP_ARGS       Space-separated ``--bootstrap host:port`` pairs. Only
                         required (and only used) when BOOTSTRAP_CACHE=false.
    DEPLOYMENT_NAME      Tag value pinning every Influx record to this
                         deployment.
    REGION               Tag value for Influx metrics.
    INFLUX_URL           InfluxDB v2 URL.
    INFLUX_TOKEN         InfluxDB v2 token.
    PROBE_SHARD_INDEX    0-based shard index for this VM.
    PROBE_SHARD_COUNT    Total number of probe shards in the deployment.

Optional env:
    ANT                  /path/to/ant binary (default /root/.local/bin/ant).
    WORK_DIR             Scratch dir for download targets
                         (default /var/lib/ant-prod-canary/probe-files).
    STATE_FILE           SQLite path
                         (default /var/lib/ant-prod-canary/probe-state.db).
    INFLUX_ORG           InfluxDB org (default ant).
    INFLUX_BUCKET        InfluxDB bucket (default testnet-metrics).
    PROBE_CONCURRENCY    Concurrent ant download invocations (default 4).
    PROBE_TIMEOUT_SECS   Per-probe ant timeout in seconds (default 300).
    CYCLE_INTERVAL_SECS  Override the hourly cadence (default 3600).
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UPLOAD_MEASUREMENT = "prod_canary_upload"
PROBE_MEASUREMENT = "prod_canary_probe"

PROBE_OK = "ok"
PROBE_FAILED = "failed"
PROBE_INCONCLUSIVE = "inconclusive"

STATUS_ALIVE = "alive"
# A canary that has been confirmed unavailable (two-strike) but which we KEEP
# probing, so that recovery can be observed and the unavailability window
# measured. Replaces the old terminal STATUS_LOST sink, which stopped probing
# and made the canary blind to recovery — chunks were found to go unreachable
# for hours-to-days and then come back, which the old design could never see.
STATUS_UNAVAILABLE = "unavailable"
# Retained only so an upgraded probe can re-activate canaries left in the old
# terminal state by a previous version of this script.
STATUS_LOST = "lost"

DEFAULT_WORK_DIR = "/var/lib/ant-prod-canary/probe-files"
DEFAULT_STATE_FILE = "/var/lib/ant-prod-canary/probe-state.db"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value in (None, ""):
        print(f"ERROR: required env var {name} is not set", file=sys.stderr)
        sys.exit(2)
    return value


def _shard_index(canary_id: str) -> int:
    """blake3 of the canary_id; first byte mod shard count selects the shard.

    Falls back to BLAKE2b if blake3 isn't available — both are cryptographic
    hashes with uniform output, so the partition stays well-balanced. The
    *function used* is part of the contract: changing it after canaries
    have been uploaded would re-shard the deployment.
    """
    try:
        import blake3  # type: ignore
        digest = blake3.blake3(canary_id.encode("utf-8")).digest()
    except ImportError:
        digest = hashlib.blake2b(canary_id.encode("utf-8"), digest_size=32).digest()
    return digest[0]


def _owns(canary_id: str, shard_index: int, shard_count: int) -> bool:
    return _shard_index(canary_id) % shard_count == shard_index


# ---------------------------------------------------------------------------
# SQLite state
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS canary_state (
  canary_id                    TEXT PRIMARY KEY,
  top_address                  TEXT NOT NULL,
  status                       TEXT NOT NULL,
  first_failed_probe_at        TEXT,
  first_failed_missing_chunk   TEXT,
  added_at                     TEXT NOT NULL,
  unavailable_since            TEXT
);
"""


def _open_state(state_path: Path) -> sqlite3.Connection:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_path))
    conn.executescript(_SCHEMA)
    # Migrate state DBs created by an older probe script: add the
    # unavailable_since column and re-activate any canaries stuck in the old
    # terminal STATUS_LOST so they get probed again (recovery observation).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(canary_state)")}
    if "unavailable_since" not in cols:
        conn.execute("ALTER TABLE canary_state ADD COLUMN unavailable_since TEXT")
    conn.execute(
        "UPDATE canary_state SET status = ?, unavailable_since = COALESCE("
        "unavailable_since, first_failed_probe_at, added_at) WHERE status = ?",
        (STATUS_UNAVAILABLE, STATUS_LOST),
    )
    conn.commit()
    return conn


def _state_row(conn: sqlite3.Connection, canary_id: str) -> tuple | None:
    cur = conn.execute(
        "SELECT canary_id, top_address, status, first_failed_probe_at, "
        "first_failed_missing_chunk, added_at, unavailable_since "
        "FROM canary_state WHERE canary_id = ?",
        (canary_id,),
    )
    return cur.fetchone()


def _insert_state(
    conn: sqlite3.Connection, canary_id: str, top_address: str
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO canary_state "
        "(canary_id, top_address, status, added_at) VALUES (?, ?, ?, ?)",
        (canary_id, top_address, STATUS_ALIVE, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _probeable_canaries(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every canary we should probe this cycle — both healthy (alive) and
    currently-unavailable ones. We deliberately keep probing unavailable
    canaries so recovery is observed and the unavailability window measured."""
    cur = conn.execute(
        "SELECT canary_id, top_address FROM canary_state "
        "WHERE status IN (?, ?) ORDER BY added_at",
        (STATUS_ALIVE, STATUS_UNAVAILABLE),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


def _apply_two_strike(
    conn: sqlite3.Connection,
    *,
    canary_id: str,
    status: str,
    missing_chunk: str,
    probed_at_iso: str,
) -> tuple[str, str] | None:
    """Update local state per the two-strike rule and report transitions.

    State machine (local bookkeeping; the authoritative availability timeline
    is the full probe stream in Influx):

    - status=ok:
        * if currently unavailable -> RECOVERED: back to alive, clear markers.
        * else clear any pending first-failure.
    - status=failed:
        * second consecutive failure on the same missing chunk -> UNAVAILABLE
          (but keep probing); stamp unavailable_since.
        * otherwise record this as the new first-failure.
    - status=inconclusive: leave state unchanged.

    Returns ("unavailable", chunk) or ("recovered", "") when a transition
    happens, else None — so the caller can log it.
    """
    if status == PROBE_INCONCLUSIVE:
        return None
    row = _state_row(conn, canary_id)
    if row is None:
        return None
    _, _, current_status, prev_failed_at, prev_failed_chunk, _, _ = row

    if status == PROBE_OK:
        if current_status == STATUS_UNAVAILABLE:
            conn.execute(
                "UPDATE canary_state SET status = ?, first_failed_probe_at = NULL, "
                "first_failed_missing_chunk = NULL, unavailable_since = NULL "
                "WHERE canary_id = ?",
                (STATUS_ALIVE, canary_id),
            )
            conn.commit()
            return ("recovered", "")
        if prev_failed_at is not None or prev_failed_chunk is not None:
            conn.execute(
                "UPDATE canary_state SET first_failed_probe_at = NULL, "
                "first_failed_missing_chunk = NULL WHERE canary_id = ?",
                (canary_id,),
            )
            conn.commit()
        return None

    if status == PROBE_FAILED:
        if current_status == STATUS_UNAVAILABLE:
            # Already known unavailable; keep probing, nothing to transition.
            return None
        if prev_failed_chunk and prev_failed_chunk == missing_chunk:
            conn.execute(
                "UPDATE canary_state SET status = ?, unavailable_since = ? "
                "WHERE canary_id = ?",
                (STATUS_UNAVAILABLE, prev_failed_at or probed_at_iso, canary_id),
            )
            conn.commit()
            return ("unavailable", missing_chunk)
        conn.execute(
            "UPDATE canary_state SET first_failed_probe_at = ?, "
            "first_failed_missing_chunk = ? WHERE canary_id = ?",
            (probed_at_iso, missing_chunk, canary_id),
        )
        conn.commit()
        return None
    return None


# ---------------------------------------------------------------------------
# Influx I/O
# ---------------------------------------------------------------------------


def _escape_tag(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace("=", "\\=")
        .replace(" ", "\\ ")
    )


def _escape_field_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _post_influx(url: str, token: str, org: str, bucket: str, line: str) -> None:
    target = (
        f"{url.rstrip('/')}/api/v2/write?org={urllib.parse.quote(org)}"
        f"&bucket={urllib.parse.quote(bucket)}&precision=ns"
    )
    req = urllib.request.Request(
        target,
        data=line.encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(
            f"WARN: Influx write returned {e.code}: {body}",
            file=sys.stderr,
            flush=True,
        )
    except (urllib.error.URLError, socket.timeout) as e:
        print(f"WARN: Influx write failed: {e}", file=sys.stderr, flush=True)


def _query_csv(url: str, token: str, org: str, flux: str) -> str:
    target = f"{url.rstrip('/')}/api/v2/query?org={urllib.parse.quote(org)}"
    req = urllib.request.Request(
        target,
        data=flux.encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"WARN: Influx query returned {e.code}: {body}", file=sys.stderr, flush=True)
        return ""
    except (urllib.error.URLError, socket.timeout) as e:
        print(f"WARN: Influx query failed: {e}", file=sys.stderr, flush=True)
        return ""


def _fetch_uploads(
    *,
    influx_url: str,
    influx_token: str,
    influx_org: str,
    influx_bucket: str,
    deployment_name: str,
) -> list[dict[str, str]]:
    """Pull canary_id + top_address for every prod_canary_upload record on
    this deployment. We pivot on canary_id because canary_id and
    top_address live as separate _field rows."""
    flux = (
        f'from(bucket: "{influx_bucket}")\n'
        f"  |> range(start: 0)\n"
        f'  |> filter(fn: (r) => r._measurement == "{UPLOAD_MEASUREMENT}")\n'
        f'  |> filter(fn: (r) => r.deployment_name == "{deployment_name}")\n'
        f'  |> filter(fn: (r) => r._field == "canary_id" or r._field == "top_address")\n'
        f'  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        f'  |> keep(columns: ["canary_id", "top_address"])\n'
    )
    body = _query_csv(influx_url, influx_token, influx_org, flux)
    out: list[dict[str, str]] = []
    if not body:
        return out
    reader = csv.reader(io.StringIO(body))
    header: list[str] = []
    for row in reader:
        if not row:
            continue
        if row[0].startswith("#"):
            continue
        if not header:
            header = row
            continue
        record = dict(zip(header, row))
        cid = record.get("canary_id", "").strip()
        addr = record.get("top_address", "").strip()
        if not cid or not addr:
            continue
        out.append({"canary_id": cid, "top_address": addr})
    return out


# ---------------------------------------------------------------------------
# Probe execution
# ---------------------------------------------------------------------------


_FAILED_DATAMAP_NEEDLE = "DataMap chunk not found at "
_FAILED_DATA_NEEDLE = "Generic error(Chunk not found: "
_FAILED_DATA_END = ")"
_PEER_SUMMARY_NEEDLE = "chunk_get exhausted close group for "


def _parse_probe_outcome(
    *, exit_code: int, stderr_text: str, downloaded_bytes: int
) -> tuple[str, str, str]:
    if exit_code == 0 and downloaded_bytes > 0:
        return PROBE_OK, "", ""
    if exit_code == 0 and downloaded_bytes == 0:
        return PROBE_INCONCLUSIVE, "", ""

    missing = ""
    role = ""
    for line in stderr_text.splitlines():
        idx = line.find(_FAILED_DATAMAP_NEEDLE)
        if idx >= 0:
            missing = line[idx + len(_FAILED_DATAMAP_NEEDLE):].strip()
            role = "data_map"
            break
        idx = line.find(_FAILED_DATA_NEEDLE)
        if idx >= 0:
            tail = line[idx + len(_FAILED_DATA_NEEDLE):]
            end = tail.find(_FAILED_DATA_END)
            missing = tail[:end].strip() if end >= 0 else tail.strip()
            role = "data"
            break

    if not missing:
        return PROBE_INCONCLUSIVE, "", ""

    summary: dict[str, object] = {}
    for line in stderr_text.splitlines():
        idx = line.find(_PEER_SUMMARY_NEEDLE)
        if idx < 0:
            continue
        tail = line[idx + len(_PEER_SUMMARY_NEEDLE):]
        for token in tail.split():
            if "=" not in token:
                continue
            key, _, val = token.partition("=")
            try:
                summary[key.rstrip(":")] = int(val.rstrip(","))
            except ValueError:
                continue
    summary["role"] = role

    # Distinguish genuine absence from a reachability problem. A truly missing
    # chunk shows up as NotFound across the close group; if instead most peers
    # timed out we never reached enough holders to determine presence, so the
    # download failure says nothing about loss. Only treat the failure as a
    # loss candidate when a strict majority of the close group actively
    # responded NotFound; otherwise it's inconclusive. The peer summary +
    # missing address are still recorded for diagnostics either way.
    peer_outcomes_json = json.dumps(summary, sort_keys=True)
    if not _is_genuine_absence(summary):
        return PROBE_INCONCLUSIVE, missing, peer_outcomes_json
    return PROBE_FAILED, missing, peer_outcomes_json


def _is_genuine_absence(peer_outcomes: dict) -> bool:
    """True if a failed fetch reflects genuine chunk absence rather than a
    reachability problem from this vantage.

    Requires a strict majority of the close group to have actively responded
    NotFound (``not_found * 2 > queried``). Timeout- / network-error-dominated
    failures don't clear the bar. When the peer summary is missing entirely
    (``queried`` absent — e.g. an ant binary without the chunk_get summary
    line) we fall back to treating the failure as genuine, preserving the
    pre-summary behaviour.
    """
    queried = peer_outcomes.get("queried")
    if not queried or queried <= 0:
        # No usable summary — can't downgrade; keep the old failed semantics.
        return True
    not_found = peer_outcomes.get("not_found") or 0
    return not_found * 2 > queried


def _build_probe_line(
    *,
    host: str,
    deployment_name: str,
    canary_id: str,
    status: str,
    missing_chunk: str,
    terminal_peer_outcomes_json: str,
    duration_ms: int,
    timestamp_ns: int,
) -> str:
    tag_set = (
        f"deployment_name={_escape_tag(deployment_name)},"
        f"host={_escape_tag(host)}"
    )
    field_set = (
        f'canary_id="{_escape_field_string(canary_id)}",'
        f'status="{_escape_field_string(status)}",'
        f'missing_chunk_address="{_escape_field_string(missing_chunk)}",'
        f'terminal_peer_outcomes="{_escape_field_string(terminal_peer_outcomes_json)}",'
        f"duration_ms={duration_ms}i"
    )
    return f"{PROBE_MEASUREMENT},{tag_set} {field_set} {timestamp_ns}"


async def _probe_one(
    *,
    canary_id: str,
    top_address: str,
    ant_path: str,
    bootstrap_args: list[str],
    work_dir: Path,
    timeout_secs: int,
    sem: asyncio.Semaphore,
) -> tuple[str, int, str, int, int]:
    """Run one ant download. Returns
    ``(status, downloaded_bytes, stderr_text, exit_code, duration_ms)``."""
    async with sem:
        target = work_dir / f"probe-{canary_id}.bin"
        target.unlink(missing_ok=True)
        cmd = [
            ant_path,
            *bootstrap_args,
            "--json",
            "-v",
            "file", "download", top_address, "-o", str(target),
        ]
        started_ns = time.time_ns()
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_secs
                )
                exit_code = proc.returncode if proc.returncode is not None else -1
                stderr_text = stderr.decode("utf-8", errors="replace")
            except asyncio.TimeoutError:
                exit_code = -1
                stderr_text = f"TIMEOUT after {timeout_secs}s"
        except FileNotFoundError as e:
            exit_code = -1
            stderr_text = f"ant binary missing: {e}"
        finally:
            # Always reap the subprocess so a cancelled / timed-out / errored
            # probe can never leak a zombie that would prevent the next gather
            # from completing (the original hang we saw on lon1 on 2026-05-15).
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass
        finished_ns = time.time_ns()
        size = target.stat().st_size if target.exists() else 0
        target.unlink(missing_ok=True)
        duration_ms = (finished_ns - started_ns) // 1_000_000
        status, missing, peer_outcomes = _parse_probe_outcome(
            exit_code=exit_code, stderr_text=stderr_text, downloaded_bytes=size
        )
        return status, missing, peer_outcomes, exit_code, duration_ms


async def _run_cycle(
    *,
    conn: sqlite3.Connection,
    ant_path: str,
    bootstrap_args: list[str],
    work_dir: Path,
    host: str,
    deployment_name: str,
    influx_url: str,
    influx_token: str,
    influx_org: str,
    influx_bucket: str,
    timeout_secs: int,
    concurrency: int,
    cycle_budget_secs: int,
) -> None:
    probeable = _probeable_canaries(conn)
    if not probeable:
        print("cycle: no canaries to probe", flush=True)
        return
    print(f"cycle: probing {len(probeable)} canaries", flush=True)
    sem = asyncio.Semaphore(concurrency)

    async def _one(canary_id: str, top_address: str) -> None:
        status, missing, peer_outcomes, exit_code, duration_ms = await _probe_one(
            canary_id=canary_id,
            top_address=top_address,
            ant_path=ant_path,
            bootstrap_args=bootstrap_args,
            work_dir=work_dir,
            timeout_secs=timeout_secs,
            sem=sem,
        )
        finished_ns = time.time_ns()
        line = _build_probe_line(
            host=host,
            deployment_name=deployment_name,
            canary_id=canary_id,
            status=status,
            missing_chunk=missing,
            terminal_peer_outcomes_json=peer_outcomes,
            duration_ms=duration_ms,
            timestamp_ns=finished_ns,
        )
        _post_influx(influx_url, influx_token, influx_org, influx_bucket, line)
        probed_at_iso = datetime.now(timezone.utc).isoformat()
        transition = _apply_two_strike(
            conn,
            canary_id=canary_id,
            status=status,
            missing_chunk=missing,
            probed_at_iso=probed_at_iso,
        )
        if transition is not None:
            kind, chunk = transition
            if kind == "unavailable":
                print(
                    f"  {canary_id} UNAVAILABLE (confirmed) missing={chunk} "
                    "-- will keep probing for recovery",
                    flush=True,
                )
            elif kind == "recovered":
                print(f"  {canary_id} RECOVERED -- available again", flush=True)
        elif status != PROBE_OK:
            print(
                f"  {canary_id} {status} missing={missing or '-'} "
                f"exit={exit_code} duration_ms={duration_ms}",
                flush=True,
            )

    # Wrap the gather in a cycle-wide budget. Without this a single hung
    # subprocess (one whose .kill() doesn't reap cleanly) can block the cycle
    # indefinitely — that's what froze probe-lon1 on 2026-05-15 and left
    # the reporter silent for ~2.5 days.
    try:
        await asyncio.wait_for(
            asyncio.gather(*[_one(cid, addr) for cid, addr in probeable]),
            timeout=cycle_budget_secs,
        )
    except asyncio.TimeoutError:
        print(
            f"WARN: cycle exceeded {cycle_budget_secs}s budget; outstanding "
            "probes were cancelled and will be retried next cycle",
            file=sys.stderr,
            flush=True,
        )


def _ingest_uploads(
    *,
    conn: sqlite3.Connection,
    uploads: list[dict[str, str]],
    shard_index: int,
    shard_count: int,
) -> int:
    added = 0
    for upload in uploads:
        cid = upload["canary_id"]
        if not _owns(cid, shard_index, shard_count):
            continue
        if _state_row(conn, cid) is not None:
            continue
        _insert_state(conn, cid, upload["top_address"])
        added += 1
    return added


def _seconds_until_next_cycle(interval_secs: int) -> int:
    """Sleep until the next interval boundary aligned with the unix epoch.

    For the default 3600s interval that lines probes up at the top of each
    UTC hour, which makes per-host comparison across vantages easier."""
    now = int(time.time())
    next_boundary = ((now // interval_secs) + 1) * interval_secs
    return max(1, next_boundary - now)


def _resolve_bootstrap_args() -> list:
    """Resolve the --bootstrap argv fragment from the environment.

    With the bootstrap cache enabled (default), ant is invoked WITHOUT
    --bootstrap: it reads ~/.config/ant/bootstrap_peers.toml and maintains
    its peer cache. BOOTSTRAP_CACHE=false restores the legacy explicit
    --bootstrap invocation, which disables the cache.
    """
    if os.environ.get("BOOTSTRAP_CACHE", "true") == "true":
        return []
    return _require_env("BOOTSTRAP_ARGS").split()


def main() -> int:
    bootstrap_args = _resolve_bootstrap_args()
    deployment_name = _require_env("DEPLOYMENT_NAME")
    region = _require_env("REGION")  # noqa: F841 - kept for parity with upload service
    influx_url = _require_env("INFLUX_URL")
    influx_token = _require_env("INFLUX_TOKEN")
    shard_index = int(_require_env("PROBE_SHARD_INDEX"))
    shard_count = int(_require_env("PROBE_SHARD_COUNT"))

    if shard_count <= 0 or shard_index < 0 or shard_index >= shard_count:
        print(
            f"ERROR: invalid shard config index={shard_index} count={shard_count}",
            file=sys.stderr,
        )
        return 2

    ant_path = os.environ.get("ANT", "/root/.local/bin/ant")
    work_dir = Path(os.environ.get("WORK_DIR", DEFAULT_WORK_DIR))
    state_file = Path(os.environ.get("STATE_FILE", DEFAULT_STATE_FILE))
    influx_org = os.environ.get("INFLUX_ORG", "ant")
    influx_bucket = os.environ.get("INFLUX_BUCKET", "testnet-metrics")
    concurrency = int(os.environ.get("PROBE_CONCURRENCY", "4"))
    timeout_secs = int(os.environ.get("PROBE_TIMEOUT_SECS", "300"))
    interval_secs = int(os.environ.get("CYCLE_INTERVAL_SECS", "3600"))
    # Hard ceiling on a single cycle's wall clock. Default = interval - 60s so
    # we always free up the loop before the next scheduled cycle starts.
    cycle_budget_secs = int(
        os.environ.get("CYCLE_BUDGET_SECS", str(max(timeout_secs * 2, interval_secs - 60)))
    )

    work_dir.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    conn = _open_state(state_file)

    print(
        f"prod canary probe starting: host={host} shard={shard_index}/{shard_count} "
        f"deployment={deployment_name} concurrency={concurrency} "
        f"interval={interval_secs}s budget={cycle_budget_secs}s",
        flush=True,
    )

    while True:
        try:
            uploads = _fetch_uploads(
                influx_url=influx_url,
                influx_token=influx_token,
                influx_org=influx_org,
                influx_bucket=influx_bucket,
                deployment_name=deployment_name,
            )
            added = _ingest_uploads(
                conn=conn,
                uploads=uploads,
                shard_index=shard_index,
                shard_count=shard_count,
            )
            if added:
                print(f"ingest: +{added} new canaries", flush=True)
            asyncio.run(
                _run_cycle(
                    conn=conn,
                    ant_path=ant_path,
                    bootstrap_args=bootstrap_args,
                    work_dir=work_dir,
                    host=host,
                    deployment_name=deployment_name,
                    influx_url=influx_url,
                    influx_token=influx_token,
                    influx_org=influx_org,
                    influx_bucket=influx_bucket,
                    timeout_secs=timeout_secs,
                    concurrency=concurrency,
                    cycle_budget_secs=cycle_budget_secs,
                )
            )
        except Exception as e:  # noqa: BLE001 - service is meant to keep running
            print(f"WARN: cycle failed: {e}", file=sys.stderr, flush=True)

        sleep_for = _seconds_until_next_cycle(interval_secs)
        print(f"sleeping {sleep_for}s until next cycle", flush=True)
        time.sleep(sleep_for)


if __name__ == "__main__":
    sys.exit(main())
