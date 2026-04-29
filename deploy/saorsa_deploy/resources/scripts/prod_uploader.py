#!/usr/bin/env python3
"""Download a list of public URLs and upload each to the production Autonomi
network with `ant --json file upload --public`, recording per-upload metrics
in Influx.

The service is configured entirely through environment variables (set via the
EnvironmentFile= directive on the systemd unit). It runs through the URL list
once and exits 0; teardown of the host is the operator's responsibility via
`saorsa-deploy prod uploaders destroy`.

Required env:
    BOOTSTRAP_ARGS  Space-separated `--bootstrap host:port` pairs for the prod
                    Autonomi bootstraps.
    SECRET_KEY      Wallet secret key (with or without the 0x prefix).
    REGION          Tag value for Influx metrics.
    CLIENT_INDEX    Numeric index for the uploader (for log diagnostics).
    URLS_FILE       Path to a newline-separated list of source URLs.

Optional env:
    ANT                /path/to/ant binary (default /root/.local/bin/ant).
    WORK_DIR           Local directory used for downloaded source files
                       (default /var/lib/ant-prod-uploader/files).
    EVM_NETWORK        Payment chain (default arbitrum-one — Autonomi prod runs
                       on Arbitrum One mainnet; uploads cost real ETH).
    INFLUX_URL         InfluxDB v2 URL. If unset, metrics are skipped.
    INFLUX_TOKEN       InfluxDB v2 token. If unset, metrics are skipped.
    INFLUX_ORG         InfluxDB org (default ant).
    INFLUX_BUCKET      InfluxDB bucket (default testnet-metrics).
    DOWNLOAD_TIMEOUT   urllib HTTP timeout in seconds (default 600).
    PAYMENT_TYPE       Force ant payment mode: `merkle` adds --merkle, `no-merkle`
                       adds --no-merkle. Unset → no flag, ant picks its default.
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
from pathlib import Path

MEASUREMENT = "prod_upload"

STATUS_SUCCESS = 0
STATUS_PARTIAL_UPLOAD = 1
STATUS_DOWNLOAD_FAILED = 2

# Cap the stderr block we forward to journald per failure. ant -v can emit
# tens of thousands of lines per upload; dumping the whole thing trips
# journald's per-unit rate limit (~10 000 msgs / 30 s on Ubuntu defaults)
# and starves earlier entries from the journal. The full stderr always
# goes to a per-file log file on disk regardless.
STDERR_TAIL_LINES = 200

# Per-file log directory on each uploader VM. Holds the full ant stderr
# for every failed upload so diagnostics survive journal rotation and
# rate limiting.
LOG_DIR = "/var/log/ant-prod-uploader"


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
    """Escape a value for use as an Influx tag (no commas, equals, spaces)."""
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace("=", "\\=")
        .replace(" ", "\\ ")
    )


def _escape_field_string(value: str) -> str:
    """Escape a string field value for Influx line protocol."""
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


def _build_line(
    *,
    host: str,
    region: str,
    status: int,
    total_ms: int,
    upload_secs: float,
    file_size: int,
    start_ns: int,
    chunks_stored: int,
    chunks_failed: int,
    total_chunks: int,
    address: str,
    file_name: str,
    source_url: str,
    reason: str,
    timestamp_ns: int,
) -> str:
    tag_set = f"host={_escape_tag(host)},region={_escape_tag(region)}"
    field_set = (
        f"status={status}i,"
        f"total_ms={total_ms}i,"
        f"upload_secs={upload_secs:.6f},"
        f"file_size={file_size}i,"
        f"start_ns={start_ns}i,"
        f"chunks_stored={chunks_stored}i,"
        f"chunks_failed={chunks_failed}i,"
        f"total_chunks={total_chunks}i,"
        f'address="{_escape_field_string(address)}",'
        f'file_name="{_escape_field_string(file_name)}",'
        f'source_url="{_escape_field_string(source_url)}",'
        f'reason="{_escape_field_string(reason)}"'
    )
    return f"{MEASUREMENT},{tag_set} {field_set} {timestamp_ns}"


def _download(url: str, dest: Path, timeout: int) -> int:
    """Download `url` to `dest` and return the size in bytes."""
    with urllib.request.urlopen(url, timeout=timeout) as resp, open(dest, "wb") as out:
        size = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            size += len(chunk)
    return size


def _payment_flag(payment_type: str | None) -> list[str]:
    """Translate the env-derived payment-type into the ant flag list.

    Empty string and unrecognised values produce no flag — the script
    silently keeps ant's default behaviour rather than failing the upload.
    """
    if payment_type == "merkle":
        return ["--merkle"]
    if payment_type == "no-merkle":
        return ["--no-merkle"]
    return []


def _run_ant(
    *, ant_path: str, bootstrap_args: list[str], evm_network: str,
    secret_key: str, file_path: Path, payment_type: str | None = None,
) -> tuple[int, str, str]:
    """Run ant with --json and -v; return ``(exit_code, stdout, stderr)``.

    ``--json`` makes the result a single JSON record on stdout.
    ``-v`` turns on info-level tracing on stderr — kept separate from the
    JSON stream so it never breaks parsing. Caller decides whether to
    surface stderr (we only forward it on failure to keep the journal
    compact on the happy path).
    """
    cmd = [
        ant_path,
        *bootstrap_args,
        "--evm-network", evm_network,
        "--json",
        "-v",
        "file", "upload", "--public", *_payment_flag(payment_type),
        str(file_path),
    ]
    env = dict(os.environ)
    env["SECRET_KEY"] = secret_key
    proc = subprocess.run(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_ant_json(stdout: str) -> dict | None:
    """Pick the last non-empty line of stdout and parse it as JSON.

    `ant --json` emits one JSON line on stdout. If anything else is also
    written to stdout (peer-discovery banner etc.), we want only the final
    JSON record, so we walk from the end.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _classify(parsed: dict | None) -> int:
    if parsed is None:
        return STATUS_PARTIAL_UPLOAD
    if "error" in parsed:
        return STATUS_PARTIAL_UPLOAD
    return STATUS_SUCCESS


def _tail_lines(text: str, n: int) -> str:
    """Return the last ``n`` newline-delimited lines of ``text``.

    Empty input yields an empty string. Trailing newline of the input is
    preserved on the output if present, so consumers don't need to worry
    about adding one.
    """
    if not text:
        return ""
    lines = text.splitlines()
    tail = lines[-n:] if n > 0 else lines
    out = "\n".join(tail)
    if text.endswith("\n"):
        out += "\n"
    return out


def _write_failure_log(idx: int, file_name: str, stderr_text: str) -> str | None:
    """Persist the full ``ant`` stderr for a failed upload to disk.

    Returns the absolute path written, or ``None`` if the write failed
    (we never let logging trouble abort the upload loop).
    """
    if not stderr_text:
        return None
    try:
        log_dir = Path(LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        # Strip path separators / nulls just in case file_name slipped
        # past _safe_filename — defence in depth only.
        safe = file_name.replace("/", "_").replace("\x00", "_") or "unnamed"
        path = log_dir / f"{idx:04d}-{safe}.stderr"
        path.write_text(stderr_text, encoding="utf-8", errors="replace")
        return str(path)
    except OSError as e:
        print(
            f"WARN: could not write failure log for #{idx}: {e}",
            file=sys.stderr,
            flush=True,
        )
        return None


def main() -> int:
    bootstrap_arg_str = _require_env("BOOTSTRAP_ARGS")
    secret_key = _require_env("SECRET_KEY")
    region = _require_env("REGION")
    client_index = _require_env("CLIENT_INDEX")
    urls_file = _require_env("URLS_FILE")

    ant_path = os.environ.get("ANT", "/root/.local/bin/ant")
    work_dir = Path(os.environ.get("WORK_DIR", "/var/lib/ant-prod-uploader/files"))
    evm_network = os.environ.get("EVM_NETWORK", "arbitrum-one")
    influx_url = os.environ.get("INFLUX_URL", "")
    influx_token = os.environ.get("INFLUX_TOKEN", "")
    influx_org = os.environ.get("INFLUX_ORG", "ant")
    influx_bucket = os.environ.get("INFLUX_BUCKET", "testnet-metrics")
    download_timeout = int(os.environ.get("DOWNLOAD_TIMEOUT", "600"))
    payment_type = os.environ.get("PAYMENT_TYPE") or None

    if not secret_key.startswith("0x"):
        secret_key = "0x" + secret_key

    bootstrap_args = bootstrap_arg_str.split()
    work_dir.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()

    urls = _read_urls(urls_file)
    if not urls:
        print(f"No URLs found in {urls_file}; exiting.", flush=True)
        return 0

    print(
        f"prod uploader starting: host={host} region={region} "
        f"index={client_index} urls={len(urls)}",
        flush=True,
    )

    influx_enabled = bool(influx_url and influx_token)
    failures = 0
    for idx, url in enumerate(urls, start=1):
        file_name = _safe_filename(url, idx)
        local_path = work_dir / f"{idx:04d}-{file_name}"

        print(f"[{idx}/{len(urls)}] {url}", flush=True)
        download_start_ns = time.time_ns()

        try:
            file_size = _download(url, local_path, timeout=download_timeout)
        except Exception as e:
            download_end_ns = time.time_ns()
            print(f"  download FAILED: {e}", file=sys.stderr, flush=True)
            failures += 1
            if influx_enabled:
                # No upload happened — total_ms is 0 so it doesn't poison
                # upload-time aggregates. start_ns / timestamp still mark
                # when this URL was attempted so it shows up on a timeline.
                line = _build_line(
                    host=host, region=region,
                    status=STATUS_DOWNLOAD_FAILED,
                    total_ms=0, upload_secs=0.0,
                    file_size=0, start_ns=download_start_ns,
                    chunks_stored=0, chunks_failed=0, total_chunks=0,
                    address="", file_name=file_name, source_url=url,
                    reason=str(e),
                    timestamp_ns=download_end_ns,
                )
                _post_influx(influx_url, influx_token, influx_org, influx_bucket, line)
            try:
                local_path.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        download_ms = (time.time_ns() - download_start_ns) // 1_000_000
        print(
            f"  downloaded {file_size} bytes in {download_ms}ms; uploading...",
            flush=True,
        )

        # `start_ns` and `total_ms` describe the **upload** window only —
        # the source-file fetch happens out-of-band over HTTP and isn't
        # part of what we're measuring against the prod network.
        upload_start_ns = time.time_ns()
        exit_code, stdout, stderr_text = _run_ant(
            ant_path=ant_path,
            bootstrap_args=bootstrap_args,
            evm_network=evm_network,
            secret_key=secret_key,
            file_path=local_path,
            payment_type=payment_type,
        )
        end_ns = time.time_ns()
        total_ms = (end_ns - upload_start_ns) // 1_000_000

        parsed = _parse_ant_json(stdout)
        status = _classify(parsed)
        # If ant exited non-zero but emitted no parseable JSON, treat as
        # partial_upload too (we have nothing better to call it).
        if exit_code != 0 and status == STATUS_SUCCESS:
            status = STATUS_PARTIAL_UPLOAD

        chunks_stored = int((parsed or {}).get("chunks_stored") or 0)
        chunks_failed = int((parsed or {}).get("chunks_failed") or 0)
        total_chunks = int((parsed or {}).get("total_chunks") or 0)
        upload_secs = float((parsed or {}).get("elapsed_secs") or 0.0)
        address = str((parsed or {}).get("address") or "")
        reason = str((parsed or {}).get("reason") or "")

        if status == STATUS_SUCCESS:
            print(
                f"  ok: address={address} chunks={chunks_stored}/{total_chunks} "
                f"total={total_ms}ms",
                flush=True,
            )
        else:
            failures += 1
            # Persist the FULL ant stderr to a per-file log file on disk so
            # the diagnostic survives journald rotation and rate limiting.
            log_path = _write_failure_log(idx, file_name, stderr_text)
            log_pointer = f" stderr_log={log_path}" if log_path else ""
            reason_str = f" reason={reason!r}" if reason else ""
            print(
                f"  FAILED: chunks={chunks_stored}/{total_chunks} "
                f"failed={chunks_failed} exit={exit_code} "
                f"total={total_ms}ms{reason_str}{log_pointer}",
                flush=True,
            )
            # Tail the trace to the journal: enough context to diagnose
            # routine partial-upload causes inline, but bounded so we don't
            # swamp the journal's per-unit rate limit and lose other
            # entries (we already learned this the hard way).
            tail = _tail_lines(stderr_text, STDERR_TAIL_LINES)
            if tail:
                sys.stderr.write(
                    f"--- last {STDERR_TAIL_LINES} lines of ant stderr "
                    f"(full log: {log_path or '<not written>'}) ---\n"
                )
                sys.stderr.write(tail)
                if not tail.endswith("\n"):
                    sys.stderr.write("\n")
                sys.stderr.flush()

        if influx_enabled:
            line = _build_line(
                host=host, region=region,
                status=status,
                total_ms=total_ms, upload_secs=upload_secs,
                file_size=file_size, start_ns=upload_start_ns,
                chunks_stored=chunks_stored, chunks_failed=chunks_failed,
                total_chunks=total_chunks,
                address=address, file_name=file_name, source_url=url,
                reason=reason,
                timestamp_ns=end_ns,
            )
            _post_influx(influx_url, influx_token, influx_org, influx_bucket, line)

        try:
            local_path.unlink(missing_ok=True)
        except OSError:
            pass

    print(
        f"prod uploader done: host={host} processed={len(urls)} failures={failures}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
