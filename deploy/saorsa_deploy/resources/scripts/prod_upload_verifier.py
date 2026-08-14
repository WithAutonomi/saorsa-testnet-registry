#!/usr/bin/env python3
"""Daily production write test — the verifier half (V2-837).

Runs forever on a VM in a region other than the uploader's. Each cycle it asks
the registry which uploaded addresses *this host* has not checked yet,
downloads each one, hashes it, and compares against the hash the uploader took
over the bytes it actually uploaded.

**It keeps no local state.** The canary probe needs a SQLite ledger because it
cannot reach Postgres; this service can, so "have I already checked this?" is
answered by the rows it has written. That means a replaced VM resumes exactly
where its predecessor stopped, and an outage of any length is caught up rather
than silently skipped — which reading the list from InfluxDB could not offer,
since those points expire.

**The verifiers deliberately do not divide the work.** Every verifier checks
every address. A second region confirming the same file is the point, so
sharding would defeat it.
"""

import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MEASUREMENT = "prod_upload_verify"
LOG_DIR = "/var/log/ant-prod-upload-verifier"
HASH_CHUNK_BYTES = 1 << 20
STDERR_TAIL_LINES = 20

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
    except Exception as exc:  # noqa: BLE001
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
    return f"{prefix} {','.join(field_parts)}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail_lines(text: str, count: int) -> str:
    return "\n".join(text.splitlines()[-count:])


def _run_ant(cmd: list, log_base: Path, timeout: int):
    env = dict(os.environ)
    if not env.get("RUST_LOG", "").strip():
        env.pop("RUST_LOG", None)
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
            target=pump,
            args=(process.stdout, Path(f"{log_base}.stdout"), captured["out"]),
            daemon=True,
        ),
        threading.Thread(
            target=pump,
            args=(process.stderr, Path(f"{log_base}.stderr"), captured["err"]),
            daemon=True,
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


def _seconds_until_next_cycle(interval: int) -> float:
    """Align cycles to the epoch so every vantage checks at the same moments.

    Two regions reporting at unrelated times make an availability difference
    impossible to distinguish from a timing difference.
    """
    if interval <= 0:
        return 0.0
    now = time.time()
    return interval - (now % interval)


def main() -> int:
    deployment_id = int(_require_env("DEPLOYMENT_ID"))
    deployment_name = _require_env("DEPLOYMENT_NAME")
    database_url = _require_env("POOL_DATABASE_URL")
    region = _require_env("REGION")

    ant_path = os.environ.get("ANT", "/root/.local/bin/ant")
    provider = os.environ.get("PROVIDER", "").strip() or None
    work_dir = Path(os.environ.get("WORK_DIR", "/var/lib/ant-prod-upload-verifier/downloads"))
    evm_network = os.environ.get("EVM_NETWORK", "arbitrum-one")
    log_level = os.environ.get("LOG_LEVEL", "info").strip() or "info"
    interval = _env_int("VERIFY_INTERVAL_SECS", 3600)
    download_timeout = _env_int("VERIFY_TIMEOUT", 3600)
    # A cap per cycle so a large backlog is worked through steadily instead of
    # one cycle running for hours and starving the next.
    batch_limit = _env_int("VERIFY_BATCH_LIMIT", 50)

    influx_url = os.environ.get("INFLUX_URL", "")
    influx_token = os.environ.get("INFLUX_TOKEN", "")
    influx_org = os.environ.get("INFLUX_ORG", "ant")
    influx_bucket = os.environ.get("INFLUX_BUCKET", "testnet-metrics")

    host = socket.gethostname()
    work_dir.mkdir(parents=True, exist_ok=True)
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

    from saorsa_db.models import ProdUploadVerifyStatus
    from saorsa_db.repositories import SqlAlchemyDeploymentRepository
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    _log(
        f"prod upload verifier starting: deployment={deployment_name} "
        f"({deployment_id}) region={region} host={host} interval={interval}s"
    )

    engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=300)
    session_factory = sessionmaker(bind=engine)

    while True:
        try:
            session = session_factory()
            repo = SqlAlchemyDeploymentRepository(session)
            pending = repo.list_unverified_uploads_for_host(
                deployment_id, host, limit=batch_limit
            )
            if pending:
                _log(f"{len(pending)} address(es) to check")
            for upload in pending:
                _check_one(
                    upload=upload,
                    repo=repo,
                    deployment_id=deployment_id,
                    deployment_name=deployment_name,
                    host=host,
                    region=region,
                    provider=provider,
                    ant_path=ant_path,
                    evm_network=evm_network,
                    log_level=log_level,
                    work_dir=work_dir,
                    timeout=download_timeout,
                    influx=(influx_url, influx_token, influx_org, influx_bucket),
                    ProdUploadVerifyStatus=ProdUploadVerifyStatus,
                )
            session.close()
        except Exception as exc:  # noqa: BLE001
            # A transient database or network failure must not kill the
            # service — the work queue is durable, so the next cycle simply
            # picks up whatever was missed.
            _log(f"ERROR: verification cycle failed: {exc}")

        time.sleep(_seconds_until_next_cycle(interval) or interval)


def _check_one(
    *,
    upload,
    repo,
    deployment_id,
    deployment_name,
    host,
    region,
    provider,
    ant_path,
    evm_network,
    log_level,
    work_dir,
    timeout,
    influx,
    ProdUploadVerifyStatus,
):
    address = upload.address
    output_path = work_dir / f"verify-{address[:16]}"
    started_at = datetime.now(timezone.utc)
    start_ns = time.time_ns()
    # Timestamped so a re-check keeps the failed attempt's log rather than
    # overwriting the very evidence worth reading.
    log_base = Path(LOG_DIR) / (
        f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{address[:16]}"
    )

    cmd = [
        ant_path, "--evm-network", evm_network,
        "-" + ("v" * _LOG_LEVEL_VERBOSITY.get(log_level, 1)),
        "file", "download", address, "-o", str(output_path),
    ]
    rc, _stdout, stderr = _run_ant(cmd, log_base, timeout)
    finished_at = datetime.now(timezone.utc)
    duration_ms = (time.time_ns() - start_ns) // 1_000_000

    actual_sha256 = None
    bytes_downloaded = None
    if rc == 0 and output_path.exists():
        try:
            bytes_downloaded = output_path.stat().st_size
            actual_sha256 = _sha256_file(output_path)
        except Exception as exc:  # noqa: BLE001
            _log(f"WARNING: could not hash {address[:16]}: {exc}")

    expected = upload.sha256
    if not expected:
        # Nothing to compare against. Recorded as inconclusive rather than OK,
        # because "it downloaded" is a weaker claim than this test exists to
        # make, and rather than failed, because nothing is known to be wrong.
        status = ProdUploadVerifyStatus.INCONCLUSIVE
        reason = "upload row has no sha256 to compare against"
    elif rc != 0 or actual_sha256 is None:
        status = ProdUploadVerifyStatus.DOWNLOAD_FAILED
        reason = _tail_lines(stderr, STDERR_TAIL_LINES)[:500] or f"ant exit {rc}"
    elif actual_sha256 != expected:
        status = ProdUploadVerifyStatus.DIGEST_MISMATCH
        reason = "downloaded bytes differ from what was uploaded"
    else:
        status = ProdUploadVerifyStatus.OK
        reason = None

    age = int((started_at - upload.finished_at).total_seconds())
    if status is ProdUploadVerifyStatus.OK:
        _log(f"OK {upload.file_name} ({address[:16]}…) age={age}s {duration_ms}ms")
    else:
        _log(f"{status.value.upper()} {upload.file_name} ({address[:16]}…): {reason}")

    repo.upsert_prod_upload_verification(
        deployment_id=deployment_id,
        client_hostname=host,
        region=region,
        provider=provider,
        address=address,
        file_name=upload.file_name,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        expected_sha256=expected,
        actual_sha256=actual_sha256,
        bytes_downloaded=bytes_downloaded,
        duration_ms=duration_ms,
        reason=reason,
        age_at_check_secs=age,
    )

    influx_url, influx_token, influx_org, influx_bucket = influx
    _post_influx(
        influx_url, influx_token, influx_org, influx_bucket,
        _build_line(
            MEASUREMENT,
            {"host": host, "region": region, "deployment_name": deployment_name},
            {"status": status.value, "ok": status is ProdUploadVerifyStatus.OK,
             "duration_ms": duration_ms, "bytes_downloaded": bytes_downloaded or 0,
             "age_secs": age, "address": address,
             "file_name": upload.file_name, "reason": reason or ""},
        ),
    )
    output_path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
