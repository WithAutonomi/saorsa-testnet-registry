#!/usr/bin/env python3
"""Continuously download every public address in a static list from the
production Autonomi network with `ant -v file download`, recording per-op
metrics in Influx and persisting per-op stdout/stderr to disk so failures
and slow successes are diagnosable after the fact.

The service is configured entirely through environment variables (set via
the EnvironmentFile= directive on the systemd unit). Cycle behaviour:

* Each cycle iterates the address list end-to-end.
* When a cycle finishes, the service sleeps ``SLEEP_INTERVAL_SECS`` and
  starts the next cycle. ``SLEEP_INTERVAL_SECS=0`` disables the inter-cycle
  sleep entirely (back-to-back cycling).
* The service runs forever — it's the operator's job to tear it down via
  ``saorsa-deploy prod continuous-downloader destroy``.

Required env:
    BOOTSTRAP_ARGS  Space-separated `--bootstrap host:port` pairs for the prod
                    Autonomi bootstraps. Only required (and only used) when
                    BOOTSTRAP_CACHE=false; with the cache enabled (default)
                    ant runs WITHOUT --bootstrap and reads
                    ~/.config/ant/bootstrap_peers.toml instead.
    REGION          Tag value for Influx metrics.
    ADDRESSES_FILE  Path to a newline-separated list of public file addresses.

Optional env:
    ANT                  /path/to/ant binary (default /root/.local/bin/ant).
    EVM_NETWORK          Payment chain (default arbitrum-one — prod).
    SLEEP_INTERVAL_SECS  Seconds to sleep between full cycles. Default 900
                         (15 minutes). 0 disables.
    DOWNLOAD_DIR         Where ant writes downloaded files (default
                         /var/lib/ant-prod-downloader/downloads). Files are
                         deleted after the per-op metric has been emitted.
    INFLUX_URL           InfluxDB v2 URL. If unset, metrics are skipped.
    INFLUX_TOKEN         InfluxDB v2 token. If unset, metrics are skipped.
    INFLUX_ORG           InfluxDB org (default ant).
    INFLUX_BUCKET        InfluxDB bucket (default testnet-metrics).
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MEASUREMENT = "download"

# Cap the stderr block we forward to journald per failure. ant -v can emit
# tens of thousands of lines per download; dumping the whole thing trips
# journald's per-unit rate limit and starves the journal of useful entries.
# The full stderr always lives in a per-op log file on disk regardless.
STDERR_TAIL_LINES = 200

# Per-op log directory on each downloader VM. Holds full ant stdout and
# stderr for every download — successes too, so slow-but-successful runs
# can be analysed for performance after the fact.
LOG_DIR = "/var/log/ant-prod-downloader"

_TIME_FIELD_RE = re.compile(r"Time:\s+([0-9.]+)")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value in (None, ""):
        print(f"ERROR: required env var {name} is not set", file=sys.stderr)
        sys.exit(2)
    return value


def _read_addresses(addresses_file: str) -> list[str]:
    addrs: list[str] = []
    with open(addresses_file, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            addrs.append(line)
    return addrs


def _safe_log_basename(address: str, fallback_index: int) -> str:
    """Build the per-op log filename stem.

    Mirrors prod_uploader's ``{idx:04d}-{safe_name}`` convention. The address
    is hex (or any opaque token) so we just take the leading 16 chars and
    strip anything path-unsafe to keep filenames manageable.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", address)[:16]
    if not safe:
        safe = "unnamed"
    return f"{fallback_index:04d}-{safe}"


def _escape_tag(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace("=", "\\=")
        .replace(" ", "\\ ")
    )


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
    download_secs: float,
    file_size: int,
    start_ns: int,
) -> str:
    """Build a `download`-measurement Influx line.

    Schema is intentionally identical to what `downloader.sh` emits
    (`status`, `total_ms`, `download_secs`, `file_size`, `start_ns`, tagged
    by `host`/`region`) so the existing `deployments print-downloads` and
    `deployments record-results` paths keep working unchanged.
    """
    tag_set = f"host={_escape_tag(host)},region={_escape_tag(region)}"
    field_set = (
        f"status={status}i,"
        f"total_ms={total_ms}i,"
        f"download_secs={download_secs},"
        f"file_size={file_size}i,"
        f"start_ns={start_ns}i"
    )
    # No explicit line-protocol timestamp: InfluxDB stamps each point with its
    # own (correct) server-receive time. This is essential for clock-skewed
    # clients — their wall clock can be hours off, and an explicit client
    # timestamp would land the point in the future, hiding it behind the
    # implicit `stop: now()` of every read query. The client's perceived
    # download-start time is still preserved in the `start_ns` field above.
    return f"{MEASUREMENT},{tag_set} {field_set}"


def _parse_download_secs(stdout: str) -> float:
    """Pull the ``Time:`` value out of ant's stdout, mirroring downloader.sh.

    Returns 0.0 if the field is absent or unparseable.
    """
    match = _TIME_FIELD_RE.search(stdout)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _tail_lines(text: str, n: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    tail = lines[-n:] if n > 0 else lines
    out = "\n".join(tail)
    if text.endswith("\n"):
        out += "\n"
    return out


def _run_ant(
    *,
    ant_path: str,
    bootstrap_args: list[str],
    evm_network: str,
    address: str,
    output_path: Path,
    stderr_log_path: Path,
    stdout_log_path: Path,
) -> tuple[int, str, str]:
    """Run ant and tee its streams to plain-text log files in real time.

    Mirrors prod_uploader._run_ant exactly — split stdout/stderr, two
    pumping threads, files written line-by-line so a slow run is observable
    via ``tail -f`` and so the diagnostic survives a hard kill.
    """
    # -v installs ant's tracing subscriber (no -v = no logs). RUST_LOG, when
    # set on the unit (inherited by the subprocess), is read by that
    # subscriber's EnvFilter and overrides the -v level — so always pass -v.
    verbosity = ["-v"]
    cmd = [
        ant_path,
        *bootstrap_args,
        "--evm-network", evm_network,
        *verbosity,
        "file", "download", address,
        "-o", str(output_path),
    ]

    stderr_log_path.parent.mkdir(parents=True, exist_ok=True)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stderr_err: list[BaseException] = []
    stdout_err: list[BaseException] = []

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def _pump_stderr() -> None:
        try:
            with open(
                stderr_log_path, "w", encoding="utf-8", errors="replace"
            ) as fh:
                assert proc.stderr is not None
                for line in proc.stderr:
                    fh.write(line)
                    stderr_chunks.append(line)
        except BaseException as e:  # noqa: BLE001
            stderr_err.append(e)

    def _pump_stdout() -> None:
        try:
            with open(
                stdout_log_path, "w", encoding="utf-8", errors="replace"
            ) as fh:
                assert proc.stdout is not None
                for line in proc.stdout:
                    fh.write(line)
                    stdout_chunks.append(line)
        except BaseException as e:  # noqa: BLE001
            stdout_err.append(e)

    t_err = threading.Thread(target=_pump_stderr, daemon=True)
    t_out = threading.Thread(target=_pump_stdout, daemon=True)
    t_err.start()
    t_out.start()
    exit_code = proc.wait()
    t_err.join()
    t_out.join()

    if stderr_err:
        raise stderr_err[0]
    if stdout_err:
        raise stdout_err[0]

    return exit_code, "".join(stdout_chunks), "".join(stderr_chunks)


def _resolve_bootstrap_args() -> list[str]:
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
    region = _require_env("REGION")
    addresses_file = _require_env("ADDRESSES_FILE")

    ant_path = os.environ.get("ANT", "/root/.local/bin/ant")
    evm_network = os.environ.get("EVM_NETWORK", "arbitrum-one")
    sleep_interval_secs = int(os.environ.get("SLEEP_INTERVAL_SECS", "900"))
    one_shot = os.environ.get("ONE_SHOT", "false").strip().lower() == "true"
    download_dir = Path(
        os.environ.get("DOWNLOAD_DIR", "/var/lib/ant-prod-downloader/downloads")
    )
    influx_url = os.environ.get("INFLUX_URL", "")
    influx_token = os.environ.get("INFLUX_TOKEN", "")
    influx_org = os.environ.get("INFLUX_ORG", "ant")
    influx_bucket = os.environ.get("INFLUX_BUCKET", "testnet-metrics")

    download_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()

    addresses = _read_addresses(addresses_file)
    if not addresses:
        print(
            f"No addresses found in {addresses_file}; nothing to do, exiting.",
            flush=True,
        )
        return 0

    influx_enabled = bool(influx_url and influx_token)
    mode = "one-shot" if one_shot else "continuous"
    print(
        f"prod downloader starting: host={host} region={region} mode={mode} "
        f"addresses={len(addresses)} sleep_interval_secs={sleep_interval_secs}",
        flush=True,
    )

    cycle = 0
    op_counter = 0
    while True:
        cycle += 1
        print(f"--- cycle {cycle} ({len(addresses)} addresses) ---", flush=True)
        cycle_failures = 0
        for idx, address in enumerate(addresses, start=1):
            op_counter += 1
            log_basename = _safe_log_basename(address, op_counter)
            stderr_log_path = log_dir / f"{log_basename}.stderr"
            stdout_log_path = log_dir / f"{log_basename}.stdout"
            output_path = download_dir / address

            print(f"[{idx}/{len(addresses)}] cycle={cycle} {address}", flush=True)
            start_ns = time.time_ns()
            try:
                exit_code, stdout, stderr_text = _run_ant(
                    ant_path=ant_path,
                    bootstrap_args=bootstrap_args,
                    evm_network=evm_network,
                    address=address,
                    output_path=output_path,
                    stderr_log_path=stderr_log_path,
                    stdout_log_path=stdout_log_path,
                )
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                end_ns = time.time_ns()
                total_ms = (end_ns - start_ns) // 1_000_000
                cycle_failures += 1
                print(
                    f"  download FAILED to spawn ant: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                if influx_enabled:
                    line = _build_line(
                        host=host, region=region,
                        status=1, total_ms=total_ms, download_secs=0.0,
                        file_size=0, start_ns=start_ns,
                    )
                    _post_influx(
                        influx_url, influx_token, influx_org, influx_bucket,
                        line,
                    )
                continue

            end_ns = time.time_ns()
            total_ms = (end_ns - start_ns) // 1_000_000
            download_secs = _parse_download_secs(stdout)
            file_size = 0
            if output_path.exists():
                try:
                    file_size = output_path.stat().st_size
                except OSError:
                    file_size = 0

            if exit_code == 0:
                print(
                    f"  ok: total={total_ms}ms download_time={download_secs}s "
                    f"size={file_size} log={stderr_log_path}",
                    flush=True,
                )
            else:
                cycle_failures += 1
                print(
                    f"  FAILED: exit={exit_code} total={total_ms}ms "
                    f"stderr_log={stderr_log_path}",
                    flush=True,
                )
                tail = _tail_lines(stderr_text, STDERR_TAIL_LINES)
                if tail:
                    sys.stderr.write(
                        f"--- last {STDERR_TAIL_LINES} lines of ant stderr "
                        f"(full log: {stderr_log_path}) ---\n"
                    )
                    sys.stderr.write(tail)
                    if not tail.endswith("\n"):
                        sys.stderr.write("\n")
                    sys.stderr.flush()

            if influx_enabled:
                line = _build_line(
                    host=host, region=region,
                    status=exit_code,
                    total_ms=total_ms,
                    download_secs=download_secs,
                    file_size=file_size,
                    start_ns=start_ns,
                )
                _post_influx(
                    influx_url, influx_token, influx_org, influx_bucket, line,
                )

            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass

        print(
            f"--- cycle {cycle} done: failures={cycle_failures}/{len(addresses)} ---",
            flush=True,
        )

        if one_shot:
            print("one-shot mode: address list exhausted, exiting", flush=True)
            return 0

        if sleep_interval_secs > 0:
            print(
                f"sleeping {sleep_interval_secs}s before next cycle", flush=True
            )
            time.sleep(sleep_interval_secs)


if __name__ == "__main__":
    sys.exit(main() or 0)
