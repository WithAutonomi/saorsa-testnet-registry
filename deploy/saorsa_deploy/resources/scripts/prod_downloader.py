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
    MEMORY_PROFILING     `true` wraps each ant invocation with heaptrack
                         (default `false`). Profiles → HEAPTRACK_DIR.
    DOWNLOAD_DIAGNOSTICS `true` passes --download-diagnostics to each ant
                         download, writing one JSONL record per chunk-fetch
                         attempt to LOG_DIR/<op>.diag.jsonl (default `false`).
                         Requires an instrumented ant build.
    HEAPTRACK_DIR        Output dir for heaptrack profiles (default
                         /var/log/ant-heaptrack); only used when MEMORY_PROFILING.
    CLIENT_KIND          `native` (default) runs the `ant` binary; `wasm` runs
                         ant-core compiled to WASM over WebRTC-Direct via the
                         `ant-wasm` CLI. Everything else about the service — the
                         list walk, ONE_SHOT, the per-op logs, the unit — is
                         identical; only the spawned binary differs.
    ANT_WASM             /path/to/ant-wasm (default /usr/local/bin/ant-wasm).
                         Only used when CLIENT_KIND=wasm.
    MAX_DOWNLOAD_BYTES   Largest file to attempt, 0 = unbounded. Under
                         CLIENT_KIND=wasm this is ant-core's browser cap: an
                         address line declaring more bytes is SKIPPED without
                         spawning anything.

Address lines are `<address>[ <bytes>]`. The optional size lets an over-cap
entry be skipped up front instead of paying for a data-map resolution to be
refused; a bare line is never treated as over-cap, because unknown is not
"too big".
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

# Influx `status` is an integer field, so a skip cannot be the string the reports
# render: a type conflict makes InfluxDB drop the whole point. 2 is the wire
# value; `record-results` maps it back to the `skipped` row status.
STATUS_SUCCESS = 0
STATUS_FAILURE = 1
STATUS_SKIPPED = 2

# ant-core refuses a browser-path file above MAX_BROWSER_FILE_BYTES once the data
# map resolves. That is a property of the transport, not of the network, so it is
# recorded as a skip — counting it as a download failure would depress the
# success rate for a file that is perfectly healthy.
ERROR_KIND_OVER_CAP = "over_cap"
# The child was killed by a signal (`rc < 0`), which on a client VM is almost
# always the OOM killer reaching the WASM process — by far the largest RSS
# there. Recorded so an OOM is distinguishable from a transfer that failed.
ERROR_KIND_CRASH = "crash"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value in (None, ""):
        print(f"ERROR: required env var {name} is not set", file=sys.stderr)
        sys.exit(2)
    return value


def _parse_address_line(line: str) -> tuple[str, int | None]:
    """Split an address line into ``(address, declared_bytes | None)``.

    The size is what the uploader published, when it published one. A bare line
    yields None rather than 0: unknown is not "too big", so a list written
    before sizes were published keeps working unchanged.
    """
    parts = line.split()
    address = parts[0]
    if len(parts) < 2:
        return address, None
    try:
        return address, int(parts[1])
    except ValueError:
        return address, None


def _read_addresses(addresses_file: str) -> list[tuple[str, int | None]]:
    addrs: list[tuple[str, int | None]] = []
    with open(addresses_file, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            addrs.append(_parse_address_line(line))
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


def _escape_field_string(value: str) -> str:
    """Quote a string field value for line protocol."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
    address: str = "",
    error_kind: str = "",
    client_build: str = "",
) -> str:
    """Build a `download`-measurement Influx line.

    Schema is intentionally identical to what `downloader.sh` emits
    (`status`, `total_ms`, `download_secs`, `file_size`, `start_ns`, tagged
    by `host`/`region`) so the existing `deployments print-downloads` and
    `deployments record-results` paths keep working unchanged.
    """
    tag_set = f"host={_escape_tag(host)},region={_escape_tag(region)}"
    fields = [
        f"status={status}i",
        f"total_ms={total_ms}i",
        f"download_secs={download_secs}",
        f"file_size={file_size}i",
        f"start_ns={start_ns}i",
    ]
    # A string field, and a FIELD rather than a tag: an address is high
    # cardinality (every op has a different one) and tags are indexed, so
    # tagging it would blow up the series count for something nothing groups by.
    # Emitted on both transports — nothing else maps an operation to what it
    # fetched, which is what makes a per-address failure investigable.
    if address:
        fields.append(f"address={_escape_field_string(address)}")
    if error_kind:
        fields.append(f"error_kind={_escape_field_string(error_kind)}")
    # The ant-client commit the transferring binary was built from, as the
    # binary itself reported it. Present on the browser path only, and only
    # where a child actually ran and printed a result: `deployments
    # upgrade-wasm-client` swaps the bundle under a running service, so without
    # this the transfers either side of an upgrade are indistinguishable.
    if client_build:
        fields.append(f"client_build={_escape_field_string(client_build)}")
    field_set = ",".join(fields)
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


def _maybe_wrap_heaptrack(
    cmd: list[str], *, memory_profiling: bool, output_base: str | None
) -> list[str]:
    """Prepend a ``heaptrack -o <base>`` wrapper when profiling is enabled.

    heaptrack writes ``<base>.<pid>.zst`` incrementally, so even an OOM/SIGKILL
    leaves an analysable trace. heaptrack's own diagnostics go to stderr, so the
    ``ant -v`` JSON/log streams this script tees are unaffected. No-op when off.
    """
    if not memory_profiling or not output_base:
        return cmd
    return ["heaptrack", "-o", output_base, *cmd]


def _build_native_argv(
    *,
    ant_path: str,
    bootstrap_args: list[str],
    evm_network: str,
    address: str,
    output_path: Path,
    diagnostics_path: Path | None = None,
) -> list[str]:
    """The native `ant` download argv."""
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
    # One diagnostics file per download: ant opens the path with truncate, so a
    # single shared path would leave only the last address's records.
    if diagnostics_path is not None:
        cmd += ["--download-diagnostics", str(diagnostics_path)]
    return cmd


def _build_wasm_argv(
    *, ant_wasm_path: str, address: str, output_path: Path
) -> list[str]:
    """The `ant-wasm` download argv.

    No `--bootstrap` and no `--evm-network`: the bootstrap multiaddrs come from
    BOOTSTRAP_MULTIADDRS in the service environment, and a download pays for
    nothing, so there is no chain to name. Deliberately the same
    `file download <addr> -o <path>` shape as the native argv — that is what
    makes this a drop-in and leaves the rest of the service untouched.
    """
    return [
        ant_wasm_path,
        "file", "download", address,
        "-o", str(output_path),
    ]


def _parse_wasm_result(stdout: str) -> dict:
    """The `ant-wasm` JSON result line, or {} when there isn't one.

    Walked from the END because progress output could in principle reach stdout
    ahead of it, and the result is always the last line. An unparseable or
    absent line yields {} — the exit code still tells the caller what happened,
    so a missing result degrades the detail rather than losing the operation.
    """
    import json

    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _run_transfer(
    *,
    cmd: list[str],
    stderr_log_path: Path,
    stdout_log_path: Path,
    memory_profiling: bool = False,
    heaptrack_output_base: str | None = None,
) -> tuple[int, str, str]:
    """Run the transfer binary and tee its streams to log files in real time.

    Mirrors prod_uploader._run_ant exactly — split stdout/stderr, two
    pumping threads, files written line-by-line so a slow run is observable
    via ``tail -f`` and so the diagnostic survives a hard kill.
    """
    cmd = _maybe_wrap_heaptrack(
        cmd,
        memory_profiling=memory_profiling,
        output_base=heaptrack_output_base,
    )

    # The systemd EnvironmentFile always defines RUST_LOG (empty when no
    # rust_log was configured). An EMPTY RUST_LOG in ant's environment sets its
    # tracing EnvFilter to an empty filter, which silences ALL output and
    # overrides -v — leaving the .stderr log blank. Only let RUST_LOG reach ant
    # when it actually carries a filter; otherwise -v drives the verbosity.
    env = dict(os.environ)
    if not env.get("RUST_LOG"):
        env.pop("RUST_LOG", None)

    stderr_log_path.parent.mkdir(parents=True, exist_ok=True)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stderr_err: list[BaseException] = []
    stdout_err: list[BaseException] = []

    proc = subprocess.Popen(
        cmd,
        env=env,
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
    memory_profiling = os.environ.get("MEMORY_PROFILING", "false").lower() == "true"
    download_diagnostics = (
        os.environ.get("DOWNLOAD_DIAGNOSTICS", "false").lower() == "true"
    )
    heaptrack_dir = Path(os.environ.get("HEAPTRACK_DIR", "/var/log/ant-heaptrack"))
    client_kind = os.environ.get("CLIENT_KIND", "native").strip().lower()
    is_wasm = client_kind == "wasm"
    ant_wasm_path = os.environ.get("ANT_WASM", "/usr/local/bin/ant-wasm")
    try:
        max_download_bytes = int(os.environ.get("MAX_DOWNLOAD_BYTES", "0"))
    except ValueError:
        max_download_bytes = 0
    # The cap is ant-core's BROWSER cap, so it applies to the WASM transport
    # only. The native client has no such limit, and nothing should be able to
    # make it skip a file it can download perfectly well — so the value is gated
    # on the transport here rather than trusted to be absent from the env file.
    if not is_wasm:
        max_download_bytes = 0

    download_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    if memory_profiling:
        heaptrack_dir.mkdir(parents=True, exist_ok=True)
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
        f"transport={client_kind} addresses={len(addresses)} "
        f"sleep_interval_secs={sleep_interval_secs}",
        flush=True,
    )

    cycle = 0
    op_counter = 0
    while True:
        cycle += 1
        print(f"--- cycle {cycle} ({len(addresses)} addresses) ---", flush=True)
        cycle_failures = 0
        cycle_skips = 0
        for idx, (address, declared_bytes) in enumerate(addresses, start=1):
            op_counter += 1
            log_basename = _safe_log_basename(address, op_counter)
            stderr_log_path = log_dir / f"{log_basename}.stderr"
            stdout_log_path = log_dir / f"{log_basename}.stdout"
            output_path = download_dir / address
            heaptrack_output_base = (
                str(heaptrack_dir / f"download-{op_counter:06d}-{log_basename}")
                if memory_profiling
                else None
            )
            diagnostics_path = (
                log_dir / f"{log_basename}.diag.jsonl"
                if download_diagnostics
                else None
            )

            print(f"[{idx}/{len(addresses)}] cycle={cycle} {address}", flush=True)
            start_ns = time.time_ns()

            # Up-front skip: the uploader published a size larger than this
            # transport can carry, so there is nothing to learn from spawning a
            # child that will resolve a data map and then refuse. A bare line
            # (declared_bytes is None) is never skipped — unknown is not
            # "too big".
            if (
                max_download_bytes
                and declared_bytes is not None
                and declared_bytes > max_download_bytes
            ):
                cycle_skips += 1
                print(
                    f"  skipped: declared {declared_bytes} bytes exceeds the "
                    f"{max_download_bytes}-byte cap for this transport",
                    flush=True,
                )
                if influx_enabled:
                    _post_influx(
                        influx_url, influx_token, influx_org, influx_bucket,
                        _build_line(
                            host=host, region=region,
                            status=STATUS_SKIPPED,
                            total_ms=0, download_secs=0.0,
                            file_size=declared_bytes, start_ns=start_ns,
                            address=address,
                            error_kind=ERROR_KIND_OVER_CAP,
                        ),
                    )
                continue

            if is_wasm:
                cmd = _build_wasm_argv(
                    ant_wasm_path=ant_wasm_path,
                    address=address,
                    output_path=output_path,
                )
            else:
                cmd = _build_native_argv(
                    ant_path=ant_path,
                    bootstrap_args=bootstrap_args,
                    evm_network=evm_network,
                    address=address,
                    output_path=output_path,
                    diagnostics_path=diagnostics_path,
                )

            try:
                exit_code, stdout, stderr_text = _run_transfer(
                    cmd=cmd,
                    stderr_log_path=stderr_log_path,
                    stdout_log_path=stdout_log_path,
                    memory_profiling=memory_profiling,
                    heaptrack_output_base=heaptrack_output_base,
                )
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                end_ns = time.time_ns()
                total_ms = (end_ns - start_ns) // 1_000_000
                cycle_failures += 1
                print(
                    f"  download FAILED to spawn {cmd[0]}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                if influx_enabled:
                    line = _build_line(
                        host=host, region=region,
                        status=STATUS_FAILURE, total_ms=total_ms,
                        download_secs=0.0,
                        file_size=0, start_ns=start_ns,
                        address=address,
                    )
                    _post_influx(
                        influx_url, influx_token, influx_org, influx_bucket,
                        line,
                    )
                continue

            end_ns = time.time_ns()
            total_ms = (end_ns - start_ns) // 1_000_000
            file_size = 0
            if output_path.exists():
                try:
                    file_size = output_path.stat().st_size
                except OSError:
                    file_size = 0

            error_kind = ""
            client_build = ""
            if is_wasm:
                result = _parse_wasm_result(stdout)
                download_secs = float(result.get("elapsed_secs") or 0.0)
                error_kind = str(result.get("error_kind") or "")
                client_build = str(result.get("client_build") or "")
                if not file_size:
                    file_size = int(result.get("file_size") or 0)
            else:
                download_secs = _parse_download_secs(stdout)

            # A negative return code is a signal: on a client VM that is almost
            # always the OOM killer, which reaches the transfer process long
            # before this parent. Recorded as a crash and the walk continues —
            # no checkpoint, no restart-policy change, no inflight journal.
            if exit_code < 0:
                error_kind = ERROR_KIND_CRASH

            if exit_code == 0:
                status = STATUS_SUCCESS
                print(
                    f"  ok: total={total_ms}ms download_time={download_secs}s "
                    f"size={file_size} log={stderr_log_path}",
                    flush=True,
                )
            elif error_kind == ERROR_KIND_OVER_CAP:
                # The child resolved the data map and this transport refused the
                # file. Not a network failure: the content is there and healthy.
                status = STATUS_SKIPPED
                cycle_skips += 1
                print(
                    f"  skipped: this transport refuses the file "
                    f"(log={stderr_log_path})",
                    flush=True,
                )
            else:
                status = STATUS_FAILURE
                cycle_failures += 1
                kind_note = f" kind={error_kind}" if error_kind else ""
                print(
                    f"  FAILED: exit={exit_code}{kind_note} total={total_ms}ms "
                    f"stderr_log={stderr_log_path}",
                    flush=True,
                )
                tail = _tail_lines(stderr_text, STDERR_TAIL_LINES)
                if tail:
                    sys.stderr.write(
                        f"--- last {STDERR_TAIL_LINES} lines of transfer stderr "
                        f"(full log: {stderr_log_path}) ---\n"
                    )
                    sys.stderr.write(tail)
                    if not tail.endswith("\n"):
                        sys.stderr.write("\n")
                    sys.stderr.flush()

            if influx_enabled:
                line = _build_line(
                    host=host, region=region,
                    status=status,
                    total_ms=total_ms,
                    download_secs=download_secs,
                    file_size=file_size,
                    start_ns=start_ns,
                    address=address,
                    error_kind=error_kind,
                    client_build=client_build,
                )
                _post_influx(
                    influx_url, influx_token, influx_org, influx_bucket, line,
                )

            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass

        print(
            f"--- cycle {cycle} done: failures={cycle_failures}/"
            f"{len(addresses)} skipped={cycle_skips} ---",
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
