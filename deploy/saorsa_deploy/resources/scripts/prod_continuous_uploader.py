#!/usr/bin/env python3
"""Daily production write test — the uploader half (V2-837).

Wakes on a cycle, claims a few files from the production file pool, uploads
them to the production Autonomi network, reads each one straight back, and
sleeps. Runs forever.

This is the only VM-side service in this package that talks to the registry
database. It has to: the whole point is that a file is claimed *as it is
uploaded*, so the run never has to reserve a batch in advance and a teardown
leaves nothing stranded. It runs under the venv at /opt/ant-registry-client,
where `saorsa-db` is installed, and connects with a scoped role that can touch
the pool and the two result tables and nothing else.

Three things it is careful about:

* **A claim is resolved by what actually happened.** A successful upload spends
  the file; a partial upload marks it partial (chunks reached the network, so
  it must never be handed to a canary); a failure to fetch the *source* file
  releases it, because nothing reached the network and burning it would be a
  lie.
* **The hash is taken over the bytes handed to ant**, not over the pool's
  catalogued digest — many entries carry only md5/sha1 and some carry none.
  That hash is what every later verification compares against, so it is
  computed from ground truth or not at all.
* **Running out of files is a signal, not a crash.** The service records the
  shortfall and sleeps; topping the pool up heals it with no redeploy.

``CLIENT_KIND=wasm`` swaps the transport for ant-core compiled to WASM over
WebRTC-Direct, spawned through the ``ant-wasm`` CLI (``ANT_WASM``) instead of the
``ant`` binary. Everything around the spawn is identical — the claim, the
SHA-256, the Postgres writes, the replacement logic and the Influx measurements —
because ``ant-wasm`` prints the same result JSON on stdout. Two things do differ:
the argv carries no ``--evm-network``/``--bootstrap`` (the bootstraps come from
``BOOTSTRAP_MULTIADDRS`` and the payment identity from a node's own HELLO), and a
failure is classified by whether a payment went out (see
:func:`_wasm_failure_outcome`).
"""

import hashlib
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Emitted for dashboards. The upload measurement deliberately matches the one
# `prod uploaders` writes so existing panels and `record-results` work
# unchanged; authority for both lives in Postgres.
UPLOAD_MEASUREMENT = "prod_upload"
VERIFY_MEASUREMENT = "prod_upload_verify"
#: Written when a cycle cannot get the files it asked for. Its own measurement
#: so "the pool ran dry" can never be mistaken for "the network rejected an
#: upload" on a dashboard.
EXHAUSTION_MEASUREMENT = "prod_upload_pool_shortfall"
#: One point per cycle, always — the difference between "production is
#: broken" and "we could not test production today". Without it a day when
#: every source fetch 503s looks identical to a day when every upload was
#: rejected, and the second is an incident while the first is weather.
#: Being unconditional, it doubles as a heartbeat: no point in 25h means
#: the service itself is down.
CYCLE_MEASUREMENT = "prod_upload_cycle"

STATUS_SUCCESS = 0
STATUS_PARTIAL_UPLOAD = 1
STATUS_DOWNLOAD_FAILED = 2
#: The upload failed BEFORE any payment transaction went out, so nothing reached
#: the network and the file's claim is RELEASED rather than spent. Only the
#: browser path can tell this from STATUS_PARTIAL_UPLOAD, where chunks ARE
#: stored — via the result JSON's `payment_sent` and the payment marker file.
STATUS_FAILED = 4

#: The child was killed by a signal (``rc < 0``), which on a client VM is almost
#: always the OOM killer reaching the WASM process — by far the largest RSS
#: there. Recorded so an OOM is distinguishable from a transfer that failed on
#: its own.
ERROR_KIND_CRASH = "crash"
# NB 3 is `over_cap` in the sibling one-shot `prod_uploader.py`, and is
# deliberately unreachable here: this kind's pool draw is narrowed to ant-core's
# browser cap at deploy time, so an over-cap file is never claimed. The value is
# skipped rather than reused so the two services' wire values stay aligned.

STDERR_TAIL_LINES = 200
LOG_DIR = "/var/log/ant-prod-continuous-uploader"
HASH_CHUNK_BYTES = 1 << 20

_LOG_LEVEL_VERBOSITY = {"info": 1, "debug": 2, "trace": 3}


def _log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}", flush=True)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        _log(f"FATAL: {name} is not set")
        sys.exit(2)
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _log(f"WARNING: {name}={raw!r} is not an integer; using {default}")
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw == "true"


# --------------------------------------------------------------------------
# InfluxDB (dashboards only — never the authority)
# --------------------------------------------------------------------------


def _escape_tag(value: str) -> str:
    return (
        str(value).replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,")
        .replace("=", "\\=")
    )


def _escape_field(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _post_influx(url: str, token: str, org: str, bucket: str, line: str) -> None:
    if not url or not token:
        return
    endpoint = f"{url.rstrip('/')}/api/v2/write?org={org}&bucket={bucket}&precision=ns"
    request = urllib.request.Request(
        endpoint,
        data=line.encode("utf-8"),
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "text/plain; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except Exception as exc:  # noqa: BLE001 - metrics must never stop the run
        _log(f"WARNING: InfluxDB write failed: {exc}")


def _build_line(measurement: str, tags: dict, fields: dict) -> str:
    tag_part = ",".join(
        f"{_escape_tag(k)}={_escape_tag(v)}" for k, v in tags.items() if v != ""
    )
    field_parts = []
    for key, value in fields.items():
        if isinstance(value, bool):
            field_parts.append(f"{key}={'true' if value else 'false'}")
        elif isinstance(value, int):
            field_parts.append(f"{key}={value}i")
        elif isinstance(value, float):
            field_parts.append(f"{key}={value}")
        else:
            if value is None or value == "":
                continue
            field_parts.append(f'{key}="{_escape_field(value)}"')
    prefix = f"{measurement},{tag_part}" if tag_part else measurement
    # No client timestamp: InfluxDB stamps server-receive time, so a VM with a
    # skewed clock cannot write points into the future where range queries
    # would miss them.
    return f"{prefix} {','.join(field_parts)}"


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def _safe_filename(url: str, index: int) -> str:
    tail = url.rstrip("/").split("/")[-1] or f"file-{index}"
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in tail)[:120]


def _log_safe(name: str) -> str:
    """Basename for a per-op log: no dots, bounded length.

    Dots are stripped rather than kept because the suffix is appended to this,
    and a name carrying its own extension makes the resulting file harder to
    glob and easier to confuse with the artefact it describes.
    """
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:24]


#: HTTP statuses worth trying again. The Internet Archive — which most of the
#: pool is sourced from — routinely answers 500/503 for a few minutes at a
#: time, and 429 is an explicit "come back later". A 404/403 is a fact about
#: the file, not the weather, so retrying only wastes the cycle's time.
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _is_retryable_fetch_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_HTTP_STATUS
    # URLError covers DNS failures, refused connections and TLS resets;
    # TimeoutError/OSError covers a stalled read mid-transfer. All transient.
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def _backoff_delays(attempts: int, base_secs: float, cap_secs: float) -> list:
    """The exponential schedule: one delay per retry after the first attempt.

    Returned without jitter so the schedule itself is a plain, testable
    sequence; the randomisation is applied when sleeping (`_jittered`).
    """
    return [
        min(base_secs * (2**attempt), cap_secs) for attempt in range(attempts - 1)
    ]


def _jittered(delay: float) -> float:
    """Spread a scheduled wait by ±50%.

    Every file in a cycle tends to fail in the same second when the origin is
    having a bad minute, so an unjittered schedule sends them all back at
    exactly the same moments — reproducing the herd against a host that is
    already struggling. This must be genuinely random per call: a jitter
    derived from the attempt number alone is identical for every file and
    every host, which is no jitter at all.
    """
    return round(delay * random.uniform(0.5, 1.5), 1)


def _download_once(url: str, destination: Path, timeout: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(url, timeout=timeout) as response:
        with open(destination, "wb") as handle:
            while True:
                chunk = response.read(HASH_CHUNK_BYTES)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
    return total, digest.hexdigest()


def _download(
    url: str,
    destination: Path,
    timeout: int,
    attempts: int = 1,
    base_secs: float = 10.0,
    cap_secs: float = 300.0,
) -> tuple[int, str]:
    """Fetch the source file with retries, returning (bytes, sha256).

    Hashed while streaming so a multi-gigabyte file is not read twice.

    Retried because the source origin being briefly unavailable says nothing
    about the production network, yet a give-up-on-first-error costs the whole
    day's measurement: on 2026-08-17 all three files 503'd within the same
    second and the cycle produced no signal at all.

    A partial file from a failed attempt is discarded rather than resumed —
    range requests are not universally honoured, and a silently truncated
    source would be uploaded as if it were the real thing, then verified
    against its own truncated hash and look perfectly healthy.
    """
    delays = _backoff_delays(max(attempts, 1), base_secs, cap_secs)
    last_error: Exception | None = None
    for attempt in range(max(attempts, 1)):
        try:
            return _download_once(url, destination, timeout)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            destination.unlink(missing_ok=True)
            if not _is_retryable_fetch_error(exc) or attempt >= len(delays):
                raise
            delay = _jittered(delays[attempt])
            _log(
                f"  source fetch attempt {attempt + 1}/{attempts} failed "
                f"({exc}); retrying in {delay}s"
            )
            time.sleep(delay)
    raise last_error if last_error else RuntimeError("source fetch failed")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail_lines(text: str, count: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-count:])


# --------------------------------------------------------------------------
# ant
# --------------------------------------------------------------------------


def _resolve_bootstrap_args() -> list:
    """Empty when the peer cache is on — ant then reads bootstrap_peers.toml."""
    if _env_bool("BOOTSTRAP_CACHE", True):
        return []
    return os.environ.get("BOOTSTRAP_ARGS", "").split()


def _verbosity_flag(log_level: str) -> str:
    return "-" + ("v" * _LOG_LEVEL_VERBOSITY.get(log_level, 1))


def _run_ant(cmd: list, secret_key: str, log_base: Path, timeout: int):
    """Run ant, streaming both pipes to disk, returning (rc, stdout, stderr)."""
    env = dict(os.environ)
    if secret_key:
        env["SECRET_KEY"] = secret_key
    # An empty RUST_LOG is an EnvFilter that silences everything and would
    # override the -v flag, so it is removed rather than passed through.
    if not env.get("RUST_LOG", "").strip():
        env.pop("RUST_LOG", None)

    # Appended, never `with_suffix`: that replaces an existing extension, so
    # a name like `archive.tar.gz` would land in `archive.tar.stdout`.
    stdout_path = Path(f"{log_base}.stdout")
    stderr_path = Path(f"{log_base}.stderr")
    captured = {"out": [], "err": []}

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True
    )

    def pump(pipe, path, sink):
        with open(path, "w") as handle:
            for line in pipe:
                handle.write(line)
                handle.flush()
                sink.append(line)

    threads = [
        threading.Thread(
            target=pump, args=(process.stdout, stdout_path, captured["out"]), daemon=True
        ),
        threading.Thread(
            target=pump, args=(process.stderr, stderr_path, captured["err"]), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        _log(f"WARNING: ant exceeded {timeout}s and was killed")
    for thread in threads:
        thread.join(timeout=10)
    return process.returncode, "".join(captured["out"]), "".join(captured["err"])


def _parse_ant_json(stdout: str):
    """Last JSON object on stdout wins — ant prints progress before the result."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _payment_flags(payment_type: str) -> list:
    if payment_type == "no-merkle":
        return ["--no-merkle"]
    if payment_type == "merkle":
        return ["--merkle"]
    return []


def _build_native_upload_argv(
    *, ant_path: str, evm_network: str, log_level: str, payment_type: str,
    file_path,
) -> list:
    """The native `ant` upload argv — unchanged from before the browser path."""
    return [
        ant_path, *_resolve_bootstrap_args(), "--evm-network", evm_network,
        "--json", _verbosity_flag(log_level),
        "file", "upload", "--public", *_payment_flags(payment_type),
        str(file_path),
    ]


def _build_wasm_upload_argv(
    *, ant_wasm_path: str, payment_type: str, file_path, payment_marker_path
) -> list:
    """The `ant-wasm` upload argv.

    No `--evm-network` and no `--bootstrap`: the bootstrap multiaddrs come from
    BOOTSTRAP_MULTIADDRS in the service environment, and the payment identity is
    read from a node's own HELLO rather than configured locally. No verbosity
    flag either — the CLI writes progress to stderr unconditionally and takes
    RUST_LOG straight from the environment.

    `--payment-marker` is the one addition. A paid upload killed after the money
    went out but before it printed anything is the failure the exit code cannot
    describe: the ETH is spent and chunks may be stored. The marker lands the
    instant a transaction is submitted, so it survives a SIGKILL of the child —
    which is what lets that file's claim be marked partial rather than released
    back into the pool for a canary to draw.
    """
    return [
        ant_wasm_path,
        "file", "upload", "--public", *_payment_flags(payment_type),
        "--payment-marker", str(payment_marker_path),
        str(file_path),
    ]


def _build_native_download_argv(
    *, ant_path: str, evm_network: str, log_level: str, address: str, output_path
) -> list:
    """The native `ant` read-back argv."""
    return [
        ant_path, "--evm-network", evm_network, _verbosity_flag(log_level),
        "file", "download", address, "-o", str(output_path),
    ]


def _build_wasm_download_argv(*, ant_wasm_path: str, address: str, output_path) -> list:
    """The `ant-wasm` read-back argv — the uploader's own view as a browser user."""
    return [ant_wasm_path, "file", "download", address, "-o", str(output_path)]


def _upload_succeeded(parsed, rc: int) -> bool:
    """Whether the result document and exit code say the upload completed.

    Keyed on the VALUE of ``error``, not on the key's presence: `ant --json`
    omits the key entirely on success, but `ant-wasm` always emits it and sets it
    to null — so a presence check reported every successful browser-path upload
    as a partial one, and a partial SPENDS the file's pool claim as though only
    some of its chunks had been stored.
    """
    if parsed is None or rc != 0:
        return False
    return not parsed.get("error")


def _wasm_failure_outcome(parsed, marker_path) -> int:
    """Which failure a browser-path upload was: paid, or unpaid.

    The distinction decides what happens to the file's pool claim, so it must be
    exact. A payment that went out means chunks may be on the network and the
    file must never reach a canary — so its claim is marked PARTIAL. A failure
    before any payment means nothing reached the network at all and the file is
    untouched, so its claim is RELEASED and the file goes back in the pool.

    Two independent signals say a payment went out, and either is enough. The
    JSON's ``payment_sent`` is the normal one; the marker FILE covers the case
    the JSON cannot — a child killed (typically by the OOM killer) after the
    money went out but before it printed anything. Trusting only the JSON would
    return a paid-for file to the pool.
    """
    if (parsed or {}).get("payment_sent"):
        return STATUS_PARTIAL_UPLOAD
    try:
        if marker_path.exists():
            return STATUS_PARTIAL_UPLOAD
    except OSError:
        # Unreadable marker: assume the money went out. Wrongly marking a file
        # partial costs one pool entry; wrongly releasing a paid-for one puts
        # already-stored chunks in front of a canary.
        return STATUS_PARTIAL_UPLOAD
    return STATUS_FAILED


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def _open_repositories(database_url: str):
    from saorsa_db.repositories import (
        SqlAlchemyDeploymentRepository,
        SqlAlchemyProdFilePoolRepository,
    )
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=300)
    session = sessionmaker(bind=engine)()
    return (
        SqlAlchemyProdFilePoolRepository(session),
        SqlAlchemyDeploymentRepository(session),
        session,
    )


def _parse_pool_request(raw: str) -> dict:
    from saorsa_db.size_class import parse as parse_size_class

    request = {}
    for name, count in json.loads(raw).items():
        request[parse_size_class(name)] = int(count)
    return request


def main() -> int:
    deployment_id = int(_require_env("DEPLOYMENT_ID"))
    deployment_name = _require_env("DEPLOYMENT_NAME")
    database_url = _require_env("POOL_DATABASE_URL")
    pool_request_raw = _require_env("POOL_REQUEST")
    region = _require_env("REGION")
    secret_key = _require_env("SECRET_KEY")

    client_kind = os.environ.get("CLIENT_KIND", "native").strip().lower()
    is_wasm = client_kind == "wasm"
    ant_path = os.environ.get("ANT", "/root/.local/bin/ant")
    ant_wasm_path = os.environ.get("ANT_WASM", "/usr/local/bin/ant-wasm")
    # The transport binary, whichever it is. Named once so the read-back and the
    # per-op log paths do not each have to re-decide.
    transfer_path = ant_wasm_path if is_wasm else ant_path
    work_dir = Path(os.environ.get("WORK_DIR", "/var/lib/ant-prod-continuous-uploader/files"))
    evm_network = os.environ.get("EVM_NETWORK", "arbitrum-one")
    payment_type = os.environ.get("PAYMENT_TYPE", "").strip()
    log_level = os.environ.get("LOG_LEVEL", "info").strip() or "info"
    sleep_interval = _env_int("SLEEP_INTERVAL_SECS", 86400)
    download_timeout = _env_int("DOWNLOAD_TIMEOUT", 1800)
    fetch_attempts = _env_int("SOURCE_FETCH_ATTEMPTS", 5)
    fetch_backoff = _env_int("SOURCE_FETCH_BACKOFF_SECS", 10)
    # Cap on substitutions per cycle. A file whose origin is down is
    # swapped for another from the same size class so the day still yields
    # a real measurement — but capped, because during a full origin outage
    # every replacement fails too and an uncapped loop would just churn
    # claims against the pool.
    max_replacements = _env_int("SOURCE_MAX_REPLACEMENTS", 3)
    upload_timeout = _env_int("UPLOAD_TIMEOUT", 21600)
    verify_timeout = _env_int("VERIFY_TIMEOUT", 3600)
    readback = _env_bool("READBACK_VERIFY", True)

    influx_url = os.environ.get("INFLUX_URL", "")
    influx_token = os.environ.get("INFLUX_TOKEN", "")
    influx_org = os.environ.get("INFLUX_ORG", "ant")
    influx_bucket = os.environ.get("INFLUX_BUCKET", "testnet-metrics")

    pool_min_size = os.environ.get("POOL_MIN_SIZE", "").strip()
    pool_max_size = os.environ.get("POOL_MAX_SIZE", "").strip()

    host = socket.gethostname()
    work_dir.mkdir(parents=True, exist_ok=True)
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

    from saorsa_db.models import (
        ClaimSource,
        PoolFileKind,
        ProdUploadStatus,
        ProdUploadVerifyStatus,
    )
    from saorsa_db.size_class import parse_size

    pool_request = _parse_pool_request(pool_request_raw)
    min_bytes = parse_size(pool_min_size) if pool_min_size else None
    max_bytes = parse_size(pool_max_size) if pool_max_size else None

    _log(
        f"prod continuous uploader starting: deployment={deployment_name} "
        f"({deployment_id}) region={region} host={host} "
        f"transport={client_kind} "
        f"request={ {k.value: v for k, v in pool_request.items()} } "
        f"cycle={sleep_interval}s"
    )

    op_counter = 0
    cycle = 0

    while True:
        cycle += 1
        pool_repo, deployment_repo, session = _open_repositories(database_url)
        try:
            try:
                claims = pool_repo.claim_pool_files(
                    kind=PoolFileKind.URL,
                    request=pool_request,
                    source=ClaimSource.PROD_CONTINUOUS_UPLOADER,
                    deployment_id=deployment_id,
                    deployment_name=deployment_name,
                    min_size_bytes=min_bytes,
                    max_size_bytes=max_bytes,
                )
            except Exception as exc:  # noqa: BLE001
                # Overwhelmingly InsufficientPoolInventory, but any failure to
                # get files is the same operational fact: this cycle uploaded
                # nothing and the service must stay up to try again.
                _log(f"ERROR: could not claim files this cycle: {exc}")
                _post_influx(
                    influx_url,
                    influx_token,
                    influx_org,
                    influx_bucket,
                    _build_line(
                        EXHAUSTION_MEASUREMENT,
                        {"host": host, "region": region,
                         "deployment_name": deployment_name},
                        {"cycle": cycle, "reason": str(exc)[:500],
                         "requested": json.dumps(
                             {k.value: v for k, v in pool_request.items()})},
                    ),
                )
                session.close()
                _log(f"sleeping {sleep_interval}s before retrying")
                time.sleep(sleep_interval)
                continue

            _log(f"cycle {cycle}: claimed {len(claims)} file(s)")
            cycle_stats = {
                "attempted": 0, "succeeded": 0, "partial": 0,
                "source_failed": 0, "replacements": 0,
            }

            queue = list(claims)
            while queue:
                claim = queue.pop(0)
                op_counter += 1
                pool_file = pool_repo.get_pool_file(claim.file_id)
                locator = claim.locator_used or (pool_file.locator if pool_file else "")
                file_name = pool_file.name if pool_file else _safe_filename(locator, op_counter)
                safe = _safe_filename(locator, op_counter)
                local_path = work_dir / f"{op_counter:04d}-{safe}"
                # Timestamp-prefixed, not counter-prefixed: the counter lives
                # in this process, so a service restart would begin again at
                # 0001 and per-op logs would stop sorting chronologically —
                # which matters for something meant to run for years. The
                # counter is kept as a tiebreaker for two ops in one second.
                started_at = datetime.now(timezone.utc)
                start_ns = time.time_ns()
                log_base = Path(LOG_DIR) / (
                    f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-"
                    f"{op_counter:04d}-{_log_safe(safe)}"
                )

                # ---- fetch the source ----
                try:
                    cycle_stats["attempted"] += 1
                    size_bytes, source_sha256 = _download(
                        locator, local_path, download_timeout,
                        attempts=fetch_attempts,
                        base_secs=fetch_backoff,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log(f"source download FAILED for {file_name}: {exc}")
                    # Nothing reached the network, so the file goes back in the
                    # pool rather than being spent on a run that never used it.
                    try:
                        pool_repo.release_pool_claim(
                            claim.id, note=f"source download failed: {exc}"[:500]
                        )
                    except Exception as release_exc:  # noqa: BLE001
                        _log(f"WARNING: could not release claim {claim.id}: {release_exc}")
                    finished_at = datetime.now(timezone.utc)
                    deployment_repo.upsert_prod_upload_result(
                        deployment_id=deployment_id,
                        client_hostname=host,
                        region=region,
                        source_url=locator,
                        file_name=file_name,
                        file_size_bytes=0,
                        status=ProdUploadStatus.DOWNLOAD_FAILED,
                        address=None,
                        chunks_stored=0,
                        chunks_failed=0,
                        total_chunks=0,
                        duration_ms=(time.time_ns() - start_ns) // 1_000_000,
                        upload_secs=None,
                        started_at=started_at,
                        finished_at=finished_at,
                        reason=str(exc)[:500],
                    )
                    _post_influx(
                        influx_url, influx_token, influx_org, influx_bucket,
                        _build_line(
                            UPLOAD_MEASUREMENT,
                            {"host": host, "region": region},
                            {"status": STATUS_DOWNLOAD_FAILED, "total_ms": 0,
                             "file_size": 0, "start_ns": start_ns,
                             "chunks_stored": 0, "chunks_failed": 0,
                             "total_chunks": 0, "file_name": file_name,
                             "source_url": locator, "reason": str(exc)[:200]},
                        ),
                    )
                    local_path.unlink(missing_ok=True)
                    cycle_stats["source_failed"] += 1

                    # Substitute a different file of the same size class so the
                    # cycle still measures production. The origin being down is
                    # not a fact about the network, but giving up here means the
                    # day yields nothing — which is how 2026-08-17 passed with
                    # no signal at all.
                    if cycle_stats["replacements"] < max_replacements and pool_file:
                        replacement = _claim_replacement(
                            pool_repo, pool_file, deployment_id, deployment_name,
                            min_bytes, max_bytes,
                        )
                        if replacement is not None:
                            cycle_stats["replacements"] += 1
                            _log(
                                f"  substituting another file for {file_name} "
                                f"({cycle_stats['replacements']}/{max_replacements})"
                            )
                            queue.append(replacement)
                    continue

                # ---- upload ----
                # The marker is cleared first so a previous op's file can never
                # be read as evidence that this one paid.
                payment_marker_path = Path(f"{log_base}.paid")
                payment_marker_path.unlink(missing_ok=True)
                if is_wasm:
                    cmd = _build_wasm_upload_argv(
                        ant_wasm_path=ant_wasm_path,
                        payment_type=payment_type,
                        file_path=local_path,
                        payment_marker_path=payment_marker_path,
                    )
                else:
                    cmd = _build_native_upload_argv(
                        ant_path=ant_path,
                        evm_network=evm_network,
                        log_level=log_level,
                        payment_type=payment_type,
                        file_path=local_path,
                    )

                upload_start_ns = time.time_ns()
                rc, stdout, stderr = _run_ant(cmd, secret_key, log_base, upload_timeout)
                finished_at = datetime.now(timezone.utc)
                duration_ms = (time.time_ns() - upload_start_ns) // 1_000_000

                parsed = _parse_ant_json(stdout)
                # A transfer emits a JSON result document even when an upload
                # goes wrong part-way. NO document at all, with a non-zero exit,
                # means it died before it could upload anything — a bad config,
                # bad arguments, a broken binary. Nothing reached the network,
                # so the file must go back in the pool rather than be spent.
                #
                # This distinction is load-bearing: `partial` permanently
                # excludes a file from canary selection, on the assumption that
                # some of its chunks are on the network. A malformed
                # bootstrap_peers.toml burned three files that way on the first
                # live run, none of which had been anywhere near production.
                #
                # On the browser path that inference is not safe: a child
                # OOM-killed AFTER paying also exits on a signal and prints
                # nothing, so the payment marker decides instead.
                error_kind = ""
                if _upload_succeeded(parsed, rc):
                    status = ProdUploadStatus.SUCCESS
                    influx_status = STATUS_SUCCESS
                    startup_failure = False
                elif is_wasm:
                    error_kind = str((parsed or {}).get("error_kind") or "")
                    if rc is not None and rc < 0:
                        error_kind = ERROR_KIND_CRASH
                    influx_status = _wasm_failure_outcome(parsed, payment_marker_path)
                    startup_failure = influx_status == STATUS_FAILED
                    status = (
                        ProdUploadStatus.FAILED
                        if startup_failure
                        else ProdUploadStatus.PARTIAL_UPLOAD
                    )
                else:
                    startup_failure = parsed is None and rc != 0
                    status = ProdUploadStatus.PARTIAL_UPLOAD
                    influx_status = STATUS_PARTIAL_UPLOAD

                parsed = parsed or {}
                # The ant-client commit the transferring binary reported. Only
                # `ant-wasm` emits it, and only on a bundle carrying build
                # provenance — so a native row, and a pre-provenance browser
                # row, both stay NULL.
                client_build = str(parsed.get("client_build") or "")
                address = parsed.get("address")
                chunks_stored = int(parsed.get("chunks_stored") or 0)
                chunks_failed = int(parsed.get("chunks_failed") or 0)
                total_chunks = int(parsed.get("total_chunks") or 0)
                upload_secs = parsed.get("elapsed_secs")
                reason = parsed.get("reason") or (
                    None if status is ProdUploadStatus.SUCCESS
                    else f"ant exit {rc}"
                )
                storage_cost = parsed.get("storage_cost_atto")
                gas_cost = parsed.get("gas_cost_wei")

                if status is ProdUploadStatus.SUCCESS:
                    cycle_stats["succeeded"] += 1
                    _log(
                        f"uploaded {file_name} -> {address} "
                        f"({chunks_stored}/{total_chunks} chunks, {duration_ms}ms)"
                    )
                else:
                    cycle_stats["partial"] += 1
                    kind_note = f" kind={error_kind}" if error_kind else ""
                    paid_note = ""
                    if is_wasm:
                        paid_note = (
                            " (no payment sent — nothing stored)"
                            if status is ProdUploadStatus.FAILED
                            else " (payment was sent — chunks may be stored)"
                        )
                    _log(
                        f"upload {status.value.upper()} for {file_name}: "
                        f"{reason}{kind_note}{paid_note}"
                    )
                    _log(_tail_lines(stderr, STDERR_TAIL_LINES))

                deployment_repo.upsert_prod_upload_result(
                    deployment_id=deployment_id,
                    client_hostname=host,
                    region=region,
                    source_url=locator,
                    file_name=file_name,
                    file_size_bytes=size_bytes,
                    status=status,
                    address=address,
                    chunks_stored=chunks_stored,
                    chunks_failed=chunks_failed,
                    total_chunks=total_chunks,
                    duration_ms=duration_ms,
                    upload_secs=float(upload_secs) if upload_secs else None,
                    started_at=started_at,
                    finished_at=finished_at,
                    reason=reason,
                    storage_cost_atto=_decimal_or_none(storage_cost),
                    gas_cost_wei=_decimal_or_none(gas_cost),
                    # Ground truth: the hash of what was handed to the uploader.
                    sha256=source_sha256,
                    # Which transport wrote it. The two are not the same
                    # instrument, so a blended series would describe neither.
                    client_kind=client_kind,
                    # And which build of that transport: the bundle is replaced
                    # in place by `deployments upgrade-wasm-client`, so without
                    # this the rows either side of an upgrade cannot be split.
                    client_build=client_build or None,
                )

                # ---- resolve the claim by what actually happened ----
                try:
                    if status is ProdUploadStatus.SUCCESS:
                        pool_repo.resolve_pool_claim(
                            claim.id,
                            address=address,
                            chunks_stored=chunks_stored,
                            chunks_failed=chunks_failed,
                            total_chunks=total_chunks,
                            storage_cost_atto=_decimal_or_none(storage_cost),
                            gas_cost_wei=_decimal_or_none(gas_cost),
                            note="uploaded by prod continuous-uploader",
                        )
                    elif startup_failure:
                        # Nothing was written, so releasing keeps a configuration
                        # error from quietly consuming the pool — the files are
                        # as unused as before. Native: no result document at all.
                        # Browser path: no payment went out, evidenced by the
                        # result JSON AND the payment marker's absence.
                        note = (
                            f"no payment sent (exit {rc})"
                            if is_wasm
                            else f"ant produced no result document (exit {rc})"
                        )
                        pool_repo.release_pool_claim(claim.id, note=note[:500])
                    else:
                        # Chunks are on the network even though the upload did
                        # not complete, so the file is not available again for
                        # a canary — partial, never released.
                        pool_repo.mark_pool_claim_partial(
                            claim.id,
                            address=address,
                            chunks_stored=chunks_stored,
                            chunks_failed=chunks_failed,
                            total_chunks=total_chunks,
                            note=f"partial upload: {reason}"[:500],
                        )
                except Exception as exc:  # noqa: BLE001
                    _log(f"WARNING: could not resolve claim {claim.id}: {exc}")

                _post_influx(
                    influx_url, influx_token, influx_org, influx_bucket,
                    _build_line(
                        UPLOAD_MEASUREMENT,
                        {"host": host, "region": region},
                        {"status": influx_status, "total_ms": duration_ms,
                         "file_size": size_bytes, "start_ns": start_ns,
                         "chunks_stored": chunks_stored,
                         "chunks_failed": chunks_failed,
                         "total_chunks": total_chunks,
                         "upload_secs": float(upload_secs) if upload_secs else 0.0,
                         "address": address or "", "file_name": file_name,
                         "source_url": locator, "sha256": source_sha256,
                         "reason": reason or "",
                         # Only the browser path reports these, and
                         # _build_line drops empty strings, so a native point is
                         # unchanged.
                         "error_kind": error_kind,
                         "client_build": client_build},
                    ),
                )

                # ---- immediate read-back ----
                if readback and status is ProdUploadStatus.SUCCESS and address:
                    _verify_once(
                        ant_path=transfer_path,
                        is_wasm=is_wasm,
                        address=address,
                        file_name=file_name,
                        expected_sha256=source_sha256,
                        secret_key=secret_key,
                        work_dir=work_dir,
                        log_base=log_base,
                        timeout=verify_timeout,
                        deployment_repo=deployment_repo,
                        deployment_id=deployment_id,
                        host=host,
                        region=region,
                        log_level=log_level,
                        influx=(influx_url, influx_token, influx_org, influx_bucket),
                        deployment_name=deployment_name,
                        uploaded_at=finished_at,
                        ProdUploadVerifyStatus=ProdUploadVerifyStatus,
                    )

                local_path.unlink(missing_ok=True)
        finally:
            session.close()

        # Always emitted, success or not: this is what lets a reader tell
        # "production refused our uploads" from "we never got to try".
        produced_signal = cycle_stats["succeeded"] > 0 or cycle_stats["partial"] > 0
        _post_influx(
            influx_url, influx_token, influx_org, influx_bucket,
            _build_line(
                CYCLE_MEASUREMENT,
                {"host": host, "region": region,
                 "deployment_name": deployment_name},
                {
                    "cycle": cycle,
                    "attempted": cycle_stats["attempted"],
                    "succeeded": cycle_stats["succeeded"],
                    "partial": cycle_stats["partial"],
                    "source_failed": cycle_stats["source_failed"],
                    "replacements": cycle_stats["replacements"],
                    "produced_signal": produced_signal,
                },
            ),
        )
        if not produced_signal:
            _log(
                f"WARNING: cycle {cycle} produced NO production signal — "
                f"{cycle_stats['source_failed']} source fetch(es) failed and "
                f"nothing was uploaded. This says nothing about the network."
            )
        else:
            _log(
                f"cycle {cycle}: {cycle_stats['succeeded']} uploaded, "
                f"{cycle_stats['partial']} partial, "
                f"{cycle_stats['source_failed']} source failure(s), "
                f"{cycle_stats['replacements']} substitution(s)"
            )

        if sleep_interval > 0:
            _log(f"cycle {cycle} complete; sleeping {sleep_interval}s")
            time.sleep(sleep_interval)


def _claim_replacement(
    pool_repo, failed_file, deployment_id, deployment_name, min_bytes, max_bytes
):
    """Claim another file of the same size class as one whose origin is down.

    Same class, so the cycle's shape is preserved: substituting a tiny file for
    an unreachable small one would quietly change what the day measured.

    Returns None rather than raising when nothing is available — a cycle that
    cannot substitute is still worth completing with whatever did work.
    """
    from saorsa_db.models import ClaimSource, PoolFileKind
    from saorsa_db.size_class import classify

    if failed_file is None or not failed_file.size_bytes:
        return None
    try:
        size_class = classify(failed_file.size_bytes)
        claims = pool_repo.claim_pool_files(
            kind=PoolFileKind.URL,
            request={size_class: 1},
            source=ClaimSource.PROD_CONTINUOUS_UPLOADER,
            deployment_id=deployment_id,
            deployment_name=deployment_name,
            min_size_bytes=min_bytes,
            max_size_bytes=max_bytes,
        )
        return claims[0] if claims else None
    except Exception as exc:  # noqa: BLE001
        _log(f"  could not claim a replacement file: {exc}")
        return None


def _decimal_or_none(value):
    if value in (None, ""):
        return None
    try:
        from decimal import Decimal

        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _verify_once(
    *,
    ant_path,
    is_wasm,
    address,
    file_name,
    expected_sha256,
    secret_key,
    work_dir,
    log_base,
    timeout,
    deployment_repo,
    deployment_id,
    host,
    region,
    log_level,
    influx,
    deployment_name,
    uploaded_at,
    ProdUploadVerifyStatus,
):
    """Download an address back and compare it with what was uploaded.

    Shared in spirit with the verifier service, but kept here rather than
    imported: VM runtime scripts are deployed as standalone files, so a shared
    module would have to be a third thing to install and keep in step.

    The read-back runs over the SAME transport as the upload — the uploader VM's
    own view as whatever kind of client it is. The independent verifier VMs stay
    native, so a digest_mismatch there is attributable to the upload rather than
    to the browser read path.
    """
    output_path = work_dir / f"verify-{address[:16]}"
    started_at = datetime.now(timezone.utc)
    start_ns = time.time_ns()
    if is_wasm:
        cmd = _build_wasm_download_argv(
            ant_wasm_path=ant_path, address=address, output_path=output_path
        )
    else:
        cmd = _build_native_download_argv(
            ant_path=ant_path,
            evm_network=os.environ.get("EVM_NETWORK", "arbitrum-one"),
            log_level=log_level,
            address=address,
            output_path=output_path,
        )
    rc, stdout, stderr = _run_ant(
        cmd, secret_key, log_base.with_name(log_base.name + "-verify"), timeout
    )
    # The read-back runs over the SAME transport as the upload, so its row
    # carries the same build stamp — and it is read off the child's own result
    # rather than assumed, for the same reason the upload's is.
    client_build = str((_parse_ant_json(stdout) or {}).get("client_build") or "")
    finished_at = datetime.now(timezone.utc)
    duration_ms = (time.time_ns() - start_ns) // 1_000_000

    actual_sha256 = None
    bytes_downloaded = None
    if rc == 0 and output_path.exists():
        try:
            bytes_downloaded = output_path.stat().st_size
            actual_sha256 = _sha256_file(output_path)
        except Exception as exc:  # noqa: BLE001
            _log(f"WARNING: could not hash the read-back file: {exc}")

    if not expected_sha256:
        status = ProdUploadVerifyStatus.INCONCLUSIVE
        reason = "no expected hash recorded for this upload"
    elif rc != 0 or actual_sha256 is None:
        status = ProdUploadVerifyStatus.DOWNLOAD_FAILED
        reason = _tail_lines(stderr, 20)[:500] or f"ant exit {rc}"
    elif actual_sha256 != expected_sha256:
        status = ProdUploadVerifyStatus.DIGEST_MISMATCH
        reason = "downloaded bytes differ from what was uploaded"
    else:
        status = ProdUploadVerifyStatus.OK
        reason = None

    if status is ProdUploadVerifyStatus.OK:
        _log(f"read-back OK for {file_name} ({address[:16]}…)")
    else:
        _log(f"read-back {status.value.upper()} for {file_name}: {reason}")

    age = int((started_at - uploaded_at).total_seconds()) if uploaded_at else None
    try:
        deployment_repo.upsert_prod_upload_verification(
            deployment_id=deployment_id,
            client_hostname=host,
            region=region,
            address=address,
            file_name=file_name,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            expected_sha256=expected_sha256,
            actual_sha256=actual_sha256,
            bytes_downloaded=bytes_downloaded,
            duration_ms=duration_ms,
            reason=reason,
            age_at_check_secs=age,
            client_build=client_build or None,
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"WARNING: could not record the read-back result: {exc}")

    influx_url, influx_token, influx_org, influx_bucket = influx
    _post_influx(
        influx_url, influx_token, influx_org, influx_bucket,
        _build_line(
            VERIFY_MEASUREMENT,
            {"host": host, "region": region, "deployment_name": deployment_name},
            {"status": status.value, "ok": status is ProdUploadVerifyStatus.OK,
             "duration_ms": duration_ms,
             "bytes_downloaded": bytes_downloaded or 0,
             "age_secs": age or 0, "address": address,
             "file_name": file_name, "reason": reason or "",
             "client_build": client_build},
        ),
    )
    output_path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
