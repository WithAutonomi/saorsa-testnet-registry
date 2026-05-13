#!/usr/bin/env python3
"""Upload a list of public URLs to the production Autonomi network as
*canaries* — files that we plan to keep probing forever in order to measure
chunk-loss rate and time-to-loss.

For each URL the service:

1. Downloads the source file to a scratch path.
2. Mints a fresh ``canary_id`` (UUIDv4).
3. Runs ``ant --json file upload --public`` against the prod Autonomi
   bootstraps and parses the resulting public address.
4. Emits one ``prod_canary_upload`` Influx point with the upload metadata
   and one ``prod_canary_chunk`` point recording the data-map address.
5. Immediately verifies the upload by downloading it again and emits a
   ``prod_canary_probe`` point reflecting the initial verification.
6. Persists progress in a position file so a service restart resumes at
   the next URL rather than re-uploading everything.

When the URL list is exhausted the service exits 0; the host idles until
the operator destroys it. A second invocation against the same URL list is
a no-op (position-file plus belt-and-braces Influx existence check).

Required env:
    BOOTSTRAP_ARGS    Space-separated ``--bootstrap host:port`` pairs.
    SECRET_KEY        Wallet secret key (Arbitrum One funded; with or
                      without the ``0x`` prefix).
    REGION            Tag value for Influx metrics.
    DEPLOYMENT_NAME   Tag value pinning every Influx record to this
                      deployment.
    URLS_FILE         Path to a newline-separated list of source URLs.
    INFLUX_URL        InfluxDB v2 URL.
    INFLUX_TOKEN      InfluxDB v2 token.

Optional env:
    ANT                /path/to/ant binary (default /root/.local/bin/ant).
    WORK_DIR           Scratch dir for downloaded source files
                       (default /var/lib/ant-prod-canary/files).
    POSITION_FILE      Resume cursor (default
                       /var/lib/ant-prod-canary/urls.position).
    LOG_DIR            Per-op log dir (default /var/log/ant-prod-canary).
    EVM_NETWORK        Payment chain (default arbitrum-one).
    INFLUX_ORG         InfluxDB org (default ant).
    INFLUX_BUCKET      InfluxDB bucket (default testnet-metrics).
    DOWNLOAD_TIMEOUT   urllib HTTP timeout in seconds (default 600).
    UPLOAD_SLEEP_SECS  Seconds to sleep between uploads (default 0).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

UPLOAD_MEASUREMENT = "prod_canary_upload"
CHUNK_MEASUREMENT = "prod_canary_chunk"
PROBE_MEASUREMENT = "prod_canary_probe"

INITIAL_VERIFY_OK = "ok"
INITIAL_VERIFY_FAILED = "failed"

PROBE_OK = "ok"
PROBE_FAILED = "failed"
PROBE_INCONCLUSIVE = "inconclusive"

CHUNK_ROLE_DATA_MAP = "data_map"

DEFAULT_LOG_DIR = "/var/log/ant-prod-canary"
DEFAULT_WORK_DIR = "/var/lib/ant-prod-canary/files"
DEFAULT_POSITION_FILE = "/var/lib/ant-prod-canary/urls.position"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value in (None, ""):
        print(f"ERROR: required env var {name} is not set", file=sys.stderr)
        sys.exit(2)
    return value


def _read_urls(urls_file: str) -> list[str]:
    urls: list[str] = []
    with open(urls_file, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def _safe_filename(url: str, fallback_index: int) -> str:
    parsed = urllib.parse.urlparse(url)
    name = os.path.basename(parsed.path)
    if not name or name in (".", ".."):
        return f"download-{fallback_index}.bin"
    return name


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


def _post_query_csv(
    url: str, token: str, org: str, flux: str
) -> str:
    """POST a Flux query and return the raw CSV body."""
    target = (
        f"{url.rstrip('/')}/api/v2/query?org={urllib.parse.quote(org)}"
    )
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"WARN: Influx query returned {e.code}: {body}", file=sys.stderr, flush=True)
        return ""
    except (urllib.error.URLError, socket.timeout) as e:
        print(f"WARN: Influx query failed: {e}", file=sys.stderr, flush=True)
        return ""


def _already_uploaded(
    *,
    influx_url: str,
    influx_token: str,
    influx_org: str,
    influx_bucket: str,
    deployment_name: str,
    source_url: str,
) -> bool:
    """Belt-and-braces check: has any earlier run on this deployment already
    uploaded ``source_url``? Position-file is the primary resume mechanism;
    this protects against position-file corruption / hand edits."""
    flux = (
        f'from(bucket: "{influx_bucket}")\n'
        f"  |> range(start: 0)\n"
        f'  |> filter(fn: (r) => r._measurement == "{UPLOAD_MEASUREMENT}")\n'
        f'  |> filter(fn: (r) => r.deployment_name == "{deployment_name}")\n'
        f'  |> filter(fn: (r) => r._field == "source_url")\n'
        f'  |> filter(fn: (r) => r._value == "{source_url}")\n'
        f"  |> limit(n: 1)\n"
    )
    body = _post_query_csv(influx_url, influx_token, influx_org, flux)
    for line in body.splitlines():
        if line.startswith(",") or line.startswith("_result"):
            return True
    return False


def _build_upload_line(
    *,
    host: str,
    region: str,
    deployment_name: str,
    canary_id: str,
    source_url: str,
    file_name: str,
    file_size: int,
    top_address: str,
    num_chunks: int,
    initial_verify_status: str,
    started_ns: int,
    finished_ns: int,
    duration_ms: int,
    timestamp_ns: int,
) -> str:
    tag_set = (
        f"deployment_name={_escape_tag(deployment_name)},"
        f"region={_escape_tag(region)},"
        f"host={_escape_tag(host)}"
    )
    field_set = (
        f'canary_id="{_escape_field_string(canary_id)}",'
        f'source_url="{_escape_field_string(source_url)}",'
        f'file_name="{_escape_field_string(file_name)}",'
        f"file_size_bytes={file_size}i,"
        f'top_address="{_escape_field_string(top_address)}",'
        f"num_chunks={num_chunks}i,"
        f'initial_verify_status="{_escape_field_string(initial_verify_status)}",'
        f"started_at={started_ns}i,"
        f"finished_at={finished_ns}i,"
        f"duration_ms={duration_ms}i"
    )
    return f"{UPLOAD_MEASUREMENT},{tag_set} {field_set} {timestamp_ns}"


def _build_chunk_line(
    *,
    host: str,
    deployment_name: str,
    canary_id: str,
    chunk_address: str,
    chunk_role: str,
    timestamp_ns: int,
) -> str:
    tag_set = (
        f"deployment_name={_escape_tag(deployment_name)},"
        f"host={_escape_tag(host)}"
    )
    field_set = (
        f'canary_id="{_escape_field_string(canary_id)}",'
        f'chunk_address="{_escape_field_string(chunk_address)}",'
        f'chunk_role="{_escape_field_string(chunk_role)}"'
    )
    return f"{CHUNK_MEASUREMENT},{tag_set} {field_set} {timestamp_ns}"


def _build_probe_line(
    *,
    host: str,
    deployment_name: str,
    canary_id: str,
    status: str,
    missing_chunk_address: str,
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
        f'missing_chunk_address="{_escape_field_string(missing_chunk_address)}",'
        f'terminal_peer_outcomes="{_escape_field_string(terminal_peer_outcomes_json)}",'
        f"duration_ms={duration_ms}i"
    )
    return f"{PROBE_MEASUREMENT},{tag_set} {field_set} {timestamp_ns}"


def _download(url: str, dest: Path, timeout: int) -> int:
    with urllib.request.urlopen(url, timeout=timeout) as resp, open(dest, "wb") as out:
        size = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            size += len(chunk)
    return size


def _run_ant(
    *,
    cmd: list[str],
    secret_key: str,
    stdout_log_path: Path,
    stderr_log_path: Path,
) -> tuple[int, str, str]:
    """Run ant capturing stdout and stderr to per-op log files. Returns
    ``(exit_code, stdout, stderr)``."""
    env = dict(os.environ)
    env["SECRET_KEY"] = secret_key
    stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stdout_log_path, "w", encoding="utf-8", errors="replace") as out_fh, \
            open(stderr_log_path, "w", encoding="utf-8", errors="replace") as err_fh:
        proc = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out_fh.write(proc.stdout)
        err_fh.write(proc.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def _parse_ant_json(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


# Re-used from the probe service. Kept inline so the upload-side initial
# verification matches the probe-side classification exactly.
_FAILED_DATAMAP_NEEDLE = "DataMap chunk not found at "
_FAILED_DATA_NEEDLE = "Generic error(Chunk not found: "
_FAILED_DATA_END = ")"
_PEER_SUMMARY_NEEDLE = "chunk_get exhausted close group for "


def _parse_probe_outcome(
    *, exit_code: int, stderr_text: str, downloaded_bytes: int
) -> tuple[str, str, str]:
    """Translate an ant download invocation into ``(status, missing_chunk,
    terminal_peer_outcomes_json)``. ``missing_chunk`` is empty when no chunk
    address could be parsed."""
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

    summary = {}
    for line in stderr_text.splitlines():
        idx = line.find(_PEER_SUMMARY_NEEDLE)
        if idx < 0:
            continue
        # Lines look like:
        # "chunk_get exhausted close group for <addr>: queried=N not_found=N "
        # "timeout=N network_err=N"
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
    summary_json = json.dumps(summary, sort_keys=True)
    return PROBE_FAILED, missing, summary_json


def _read_position(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_position(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(str(value), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    bootstrap_arg_str = _require_env("BOOTSTRAP_ARGS")
    secret_key = _require_env("SECRET_KEY")
    region = _require_env("REGION")
    deployment_name = _require_env("DEPLOYMENT_NAME")
    urls_file = _require_env("URLS_FILE")
    influx_url = _require_env("INFLUX_URL")
    influx_token = _require_env("INFLUX_TOKEN")

    ant_path = os.environ.get("ANT", "/root/.local/bin/ant")
    work_dir = Path(os.environ.get("WORK_DIR", DEFAULT_WORK_DIR))
    position_file = Path(os.environ.get("POSITION_FILE", DEFAULT_POSITION_FILE))
    log_dir = Path(os.environ.get("LOG_DIR", DEFAULT_LOG_DIR))
    evm_network = os.environ.get("EVM_NETWORK", "arbitrum-one")
    influx_org = os.environ.get("INFLUX_ORG", "ant")
    influx_bucket = os.environ.get("INFLUX_BUCKET", "testnet-metrics")
    download_timeout = int(os.environ.get("DOWNLOAD_TIMEOUT", "600"))
    upload_sleep_secs = int(os.environ.get("UPLOAD_SLEEP_SECS", "0"))

    if not secret_key.startswith("0x"):
        secret_key = "0x" + secret_key

    bootstrap_args = bootstrap_arg_str.split()
    work_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()

    urls = _read_urls(urls_file)
    if not urls:
        print(f"No URLs found in {urls_file}; exiting.", flush=True)
        return 0

    position = _read_position(position_file)
    if position >= len(urls):
        print(
            f"Position {position} >= URL count {len(urls)}; nothing to do.",
            flush=True,
        )
        return 0

    print(
        f"prod canary upload starting: host={host} region={region} "
        f"deployment={deployment_name} urls={len(urls)} position={position}",
        flush=True,
    )

    while position < len(urls):
        url = urls[position]
        idx = position + 1

        print(f"[{idx}/{len(urls)}] {url}", flush=True)

        if _already_uploaded(
            influx_url=influx_url,
            influx_token=influx_token,
            influx_org=influx_org,
            influx_bucket=influx_bucket,
            deployment_name=deployment_name,
            source_url=url,
        ):
            print("  already uploaded; skipping", flush=True)
            position += 1
            _write_position(position_file, position)
            continue

        canary_id = str(uuid.uuid4())
        file_name = _safe_filename(url, idx)
        local_path = work_dir / f"{canary_id}-{file_name}"

        try:
            file_size = _download(url, local_path, timeout=download_timeout)
        except Exception as e:
            print(f"  source download FAILED: {e}", file=sys.stderr, flush=True)
            local_path.unlink(missing_ok=True)
            position += 1
            _write_position(position_file, position)
            continue

        upload_started_ns = time.time_ns()
        upload_stdout_log = log_dir / f"upload-{canary_id}.stdout"
        upload_stderr_log = log_dir / f"upload-{canary_id}.stderr"
        upload_cmd = [
            ant_path,
            *bootstrap_args,
            "--evm-network", evm_network,
            "--json",
            "-v",
            "file", "upload", "--public",
            str(local_path),
        ]
        upload_exit, upload_stdout, _upload_stderr = _run_ant(
            cmd=upload_cmd,
            secret_key=secret_key,
            stdout_log_path=upload_stdout_log,
            stderr_log_path=upload_stderr_log,
        )
        upload_finished_ns = time.time_ns()
        upload_duration_ms = (upload_finished_ns - upload_started_ns) // 1_000_000

        parsed = _parse_ant_json(upload_stdout) or {}
        top_address = str(parsed.get("address") or "")
        chunks_stored = int(parsed.get("chunks_stored") or 0)
        total_chunks = int(parsed.get("total_chunks") or 0)
        num_chunks = total_chunks or chunks_stored

        if upload_exit != 0 or not top_address:
            print(
                f"  upload FAILED exit={upload_exit} "
                f"chunks={chunks_stored}/{total_chunks} log={upload_stderr_log}",
                file=sys.stderr,
                flush=True,
            )
            local_path.unlink(missing_ok=True)
            position += 1
            _write_position(position_file, position)
            continue

        # Initial verification: download what we just uploaded.
        verify_path = work_dir / f"{canary_id}-verify-{file_name}"
        verify_cmd = [
            ant_path,
            *bootstrap_args,
            "--json",
            "-v",
            "file", "download", top_address, "-o", str(verify_path),
        ]
        verify_stdout_log = log_dir / f"verify-{canary_id}.stdout"
        verify_stderr_log = log_dir / f"verify-{canary_id}.stderr"
        verify_started_ns = time.time_ns()
        verify_exit, _verify_stdout, verify_stderr = _run_ant(
            cmd=verify_cmd,
            secret_key=secret_key,
            stdout_log_path=verify_stdout_log,
            stderr_log_path=verify_stderr_log,
        )
        verify_finished_ns = time.time_ns()
        verify_duration_ms = (verify_finished_ns - verify_started_ns) // 1_000_000
        verify_size = verify_path.stat().st_size if verify_path.exists() else 0
        verify_status, missing_chunk, peer_outcomes_json = _parse_probe_outcome(
            exit_code=verify_exit,
            stderr_text=verify_stderr,
            downloaded_bytes=verify_size,
        )
        verify_path.unlink(missing_ok=True)

        initial_verify = (
            INITIAL_VERIFY_OK if verify_status == PROBE_OK else INITIAL_VERIFY_FAILED
        )
        print(
            f"  ok: canary_id={canary_id} top={top_address} "
            f"chunks={chunks_stored}/{total_chunks} "
            f"upload_ms={upload_duration_ms} verify={verify_status} "
            f"verify_ms={verify_duration_ms}",
            flush=True,
        )

        upload_line = _build_upload_line(
            host=host,
            region=region,
            deployment_name=deployment_name,
            canary_id=canary_id,
            source_url=url,
            file_name=file_name,
            file_size=file_size,
            top_address=top_address,
            num_chunks=num_chunks,
            initial_verify_status=initial_verify,
            started_ns=upload_started_ns,
            finished_ns=upload_finished_ns,
            duration_ms=upload_duration_ms,
            timestamp_ns=upload_finished_ns,
        )
        _post_influx(
            influx_url, influx_token, influx_org, influx_bucket, upload_line
        )

        # We only know the data-map address from `ant --json` output. The
        # underlying data chunks aren't exposed by the CLI, so we record
        # only that one for now. Probe failures will discover and emit
        # specific data-chunk addresses through the missing_chunk_address
        # field of prod_canary_probe.
        chunk_line = _build_chunk_line(
            host=host,
            deployment_name=deployment_name,
            canary_id=canary_id,
            chunk_address=top_address,
            chunk_role=CHUNK_ROLE_DATA_MAP,
            timestamp_ns=upload_finished_ns,
        )
        _post_influx(
            influx_url, influx_token, influx_org, influx_bucket, chunk_line
        )

        probe_line = _build_probe_line(
            host=host,
            deployment_name=deployment_name,
            canary_id=canary_id,
            status=verify_status,
            missing_chunk_address=missing_chunk,
            terminal_peer_outcomes_json=peer_outcomes_json,
            duration_ms=verify_duration_ms,
            timestamp_ns=verify_finished_ns,
        )
        _post_influx(
            influx_url, influx_token, influx_org, influx_bucket, probe_line
        )

        local_path.unlink(missing_ok=True)
        position += 1
        _write_position(position_file, position)

        if upload_sleep_secs > 0 and position < len(urls):
            time.sleep(upload_sleep_secs)

    print(
        f"prod canary upload done: host={host} processed={len(urls)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
