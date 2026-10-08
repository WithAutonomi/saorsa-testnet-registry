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
                    Autonomi bootstraps. Only required (and only used) when
                    BOOTSTRAP_CACHE=false.
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
    LOG_LEVEL          ant verbosity: `info` (default → -v), `debug` (-vv),
                       `trace` (-vvv). An unrecognised value falls back to info.
    BOOTSTRAP_CACHE    `true` (default) invokes ant WITHOUT --bootstrap so it
                       reads ~/.config/ant/bootstrap_peers.toml (written by the
                       provisioner) and keeps its peer cache enabled. `false`
                       restores the legacy --bootstrap invocation.
    MEMORY_PROFILING   `true` wraps each ant invocation with heaptrack (default
                       `false`). Profiles are written to HEAPTRACK_DIR.
    HEAPTRACK_DIR      Output dir for heaptrack profiles (default
                       /var/log/ant-heaptrack); only used when MEMORY_PROFILING.
    CLIENT_KIND        `native` (default) runs the `ant` binary; `wasm` runs
                       ant-core compiled to WASM over WebRTC-Direct via the
                       `ant-wasm` CLI. Everything else about the service — the
                       URL walk, the per-op logs, the unit, the Influx
                       measurement — is identical; only the spawned binary
                       differs.
    ANT_WASM           /path/to/ant-wasm (default /usr/local/bin/ant-wasm).
                       Only used when CLIENT_KIND=wasm.
    MAX_UPLOAD_BYTES   Largest file to attempt, 0 = unbounded. Under
                       CLIENT_KIND=wasm this is ant-core's browser cap: a fetched
                       file above it is recorded as over_cap and never uploaded.
                       Ignored on the native path, which has no such limit.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MEASUREMENT = "prod_upload"

STATUS_SUCCESS = 0
STATUS_PARTIAL_UPLOAD = 1
STATUS_DOWNLOAD_FAILED = 2
# The file is larger than this transport will carry (ant-core refuses a
# browser-path file above MAX_UPLOAD_BYTES). Nothing is spawned and nothing is
# paid for: a property of the transport, not of the network.
STATUS_OVER_CAP = 3
# The upload failed BEFORE any payment transaction went out, so nothing reached
# the network. Distinct from STATUS_PARTIAL_UPLOAD, where chunks ARE stored: the
# pool claim for this file is released rather than spent. Only the browser path
# can tell the two apart, via `payment_sent` and the payment marker file.
STATUS_FAILED = 4

# The child was killed by a signal (`rc < 0`), which on a client VM is almost
# always the OOM killer reaching the WASM process — by far the largest RSS there.
# Recorded so an OOM is distinguishable from a transfer that failed on its own.
ERROR_KIND_CRASH = "crash"
ERROR_KIND_OVER_CAP = "over_cap"

# Cap the stderr block we forward to journald per failure. ant -v can emit
# tens of thousands of lines per upload; dumping the whole thing trips
# journald's per-unit rate limit (~10 000 msgs / 30 s on Ubuntu defaults)
# and starves earlier entries from the journal. The full stderr always
# goes to a per-file log file on disk regardless.
STDERR_TAIL_LINES = 200

# Per-file log directory on each uploader VM. Holds full ant stdout and
# stderr for every upload — successes too, so slow-but-successful runs
# can be analysed for performance after the fact.
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
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


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
    chunk_attempts_total: int,
    retries_round_0: int,
    retries_round_1: int,
    retries_round_2: int,
    retries_round_3: int,
    store_duration_p50_ms: int,
    store_duration_p95_ms: int,
    store_duration_max_ms: int,
    address: str,
    file_name: str,
    source_url: str,
    reason: str,
    storage_cost_atto: str = "",
    gas_cost_wei: str = "",
    error_kind: str = "",
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
        f"chunk_attempts_total={chunk_attempts_total}i,"
        f"retries_round_0={retries_round_0}i,"
        f"retries_round_1={retries_round_1}i,"
        f"retries_round_2={retries_round_2}i,"
        f"retries_round_3={retries_round_3}i,"
        f"store_duration_p50_ms={store_duration_p50_ms}i,"
        f"store_duration_p95_ms={store_duration_p95_ms}i,"
        f"store_duration_max_ms={store_duration_max_ms}i,"
        f'address="{_escape_field_string(address)}",'
        f'file_name="{_escape_field_string(file_name)}",'
        f'source_url="{_escape_field_string(source_url)}",'
        f'reason="{_escape_field_string(reason)}"'
    )
    # Cost fields are uint256-shaped in ant's JSON (string-encoded). Emit them as
    # Influx string fields so they don't lose precision; downstream
    # record-results parses them back as Decimal. Older `ant` binaries omit them
    # (empty string) — skip so the schema stays stable across versions.
    if storage_cost_atto:
        field_set += f',storage_cost_atto="{_escape_field_string(storage_cost_atto)}"'
    if gas_cost_wei:
        field_set += f',gas_cost_wei="{_escape_field_string(gas_cost_wei)}"'
    # Only the browser path reports one, so the field is absent on every native
    # point and the schema stays exactly as it was.
    if error_kind:
        field_set += f',error_kind="{_escape_field_string(error_kind)}"'
    # No explicit line-protocol timestamp: InfluxDB stamps each point with its
    # own (correct) server-receive time. This is essential for clock-skewed
    # clients — their wall clock can be hours off, and an explicit client
    # timestamp would land the point in the future, hiding it behind the
    # implicit `stop: now()` of every read query. The client's perceived
    # upload-start time is still preserved in the `start_ns` field above.
    return f"{MEASUREMENT},{tag_set} {field_set}"


def _percentile(values: list[int], p: float) -> int:
    """Return the integer percentile of a list. 0 on empty.

    Uses nearest-rank (no interpolation) so the value emitted is always one of
    the observed samples. ``p`` is a fraction in [0, 1].
    """
    if not values:
        return 0
    sorted_values = sorted(values)
    idx = min(len(sorted_values) - 1, max(0, int(p * len(sorted_values))))
    return int(sorted_values[idx])


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


# ant verbosity: -v=info, -vv=debug, -vvv=trace. Kept as a self-contained map
# because this script is intentionally saorsa_deploy-import-free (it is copied
# verbatim onto the uploader VM). Mirrors LOG_LEVEL_VERBOSITY in config/models.py.
_LOG_LEVEL_VERBOSITY = {"info": 1, "debug": 2, "trace": 3}


def _verbosity_flag(log_level: str) -> str:
    """Map a LOG_LEVEL name to ant's repeated-``-v`` flag; unknown → info."""
    return "-" + ("v" * _LOG_LEVEL_VERBOSITY.get(log_level, 1))


def _maybe_wrap_heaptrack(
    cmd: list[str], *, memory_profiling: bool, output_base: str | None
) -> list[str]:
    """Prepend a ``heaptrack -o <base>`` wrapper when profiling is enabled.

    heaptrack writes ``<base>.<pid>.zst`` incrementally, so even an OOM/SIGKILL
    leaves an analysable trace. heaptrack's own diagnostics go to stderr and any
    summary line is skipped by the from-the-end JSON parse, so the ``--json``
    result on stdout is unaffected. A no-op when profiling is off.
    """
    if not memory_profiling or not output_base:
        return cmd
    return ["heaptrack", "-o", output_base, *cmd]


def _build_native_argv(
    *,
    ant_path: str,
    bootstrap_args: list[str],
    evm_network: str,
    file_path: Path,
    payment_type: str | None,
    log_level: str,
) -> list[str]:
    """The native `ant` upload argv."""
    # The verbosity flag is what installs ant's tracing subscriber (no -v = no
    # logs). RUST_LOG (when set on the unit, inherited via `env`) is read by
    # that subscriber's EnvFilter and overrides the -v level — so ALWAYS pass
    # the verbosity flag.
    return [
        ant_path,
        *bootstrap_args,
        "--evm-network",
        evm_network,
        "--json",
        _verbosity_flag(log_level),
        "file",
        "upload",
        "--public",
        *_payment_flag(payment_type),
        str(file_path),
    ]


def _build_wasm_argv(
    *,
    ant_wasm_path: str,
    file_path: Path,
    payment_type: str | None,
    payment_marker_path: Path,
) -> list[str]:
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
    which is what lets the claim for that file be marked partial rather than
    released back into the pool for a canary to draw.
    """
    return [
        ant_wasm_path,
        "file",
        "upload",
        "--public",
        *_payment_flag(payment_type),
        "--payment-marker",
        str(payment_marker_path),
        str(file_path),
    ]


def _run_ant(
    *,
    ant_path: str,
    bootstrap_args: list[str],
    evm_network: str,
    secret_key: str,
    file_path: Path,
    payment_type: str | None,
    log_level: str,
    stderr_log_path: Path,
    stdout_log_path: Path,
    memory_profiling: bool = False,
    heaptrack_output_base: str | None = None,
) -> tuple[int, str, str]:
    """Build the native argv and run it. See :func:`_run_transfer`."""
    cmd = _maybe_wrap_heaptrack(
        _build_native_argv(
            ant_path=ant_path,
            bootstrap_args=bootstrap_args,
            evm_network=evm_network,
            file_path=file_path,
            payment_type=payment_type,
            log_level=log_level,
        ),
        memory_profiling=memory_profiling,
        output_base=heaptrack_output_base,
    )
    return _run_transfer(
        cmd=cmd,
        secret_key=secret_key,
        stderr_log_path=stderr_log_path,
        stdout_log_path=stdout_log_path,
    )


def _run_transfer(
    *,
    cmd: list[str],
    secret_key: str,
    stderr_log_path: Path,
    stdout_log_path: Path,
) -> tuple[int, str, str]:
    """Run a transfer binary and tee its streams to log files in real time.

    Both transports print a single JSON result record as the last line of
    stdout, and both write their diagnostics to stderr — kept separate so the
    logging never breaks the result parse. Both streams are written
    line-by-line to per-upload log files so a slow run is observable on the VM
    via ``tail -f`` and so the diagnostic survives even if systemd kills the
    service mid-upload. Returns ``(exit_code, stdout, stderr)``; the captured
    stderr is also kept in memory so the failure branch can forward a tail to
    the journal without re-reading from disk.

    A negative exit code is a signal — on a client VM almost always the OOM
    killer, which reaches the transfer process long before this parent.
    """
    env = dict(os.environ)
    env["SECRET_KEY"] = secret_key
    # The systemd EnvironmentFile always defines RUST_LOG (empty when no
    # rust_log was configured). An EMPTY RUST_LOG in ant's environment sets its
    # tracing EnvFilter to an empty filter, which silences ALL output and
    # overrides -v — leaving the .stderr log blank. Only let RUST_LOG reach ant
    # when it actually carries a filter; otherwise -v drives the verbosity.
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
            with open(stderr_log_path, "w", encoding="utf-8", errors="replace") as fh:
                assert proc.stderr is not None
                for line in proc.stderr:
                    fh.write(line)
                    stderr_chunks.append(line)
        except BaseException as e:  # noqa: BLE001 — surface in main thread
            stderr_err.append(e)

    def _pump_stdout() -> None:
        try:
            with open(stdout_log_path, "w", encoding="utf-8", errors="replace") as fh:
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
    """Success or failure, from the result JSON alone.

    Keyed on the VALUE of ``error``, not on the key's presence: `ant --json`
    omits the key entirely on success, but `ant-wasm` always emits it and sets it
    to null — so a presence check reported every successful browser-path upload
    as a partial one, and (because a partial spends the file's pool claim) would
    have marked four healthy uploads as only partly stored.
    """
    if parsed is None:
        return STATUS_PARTIAL_UPLOAD
    if parsed.get("error"):
        return STATUS_PARTIAL_UPLOAD
    return STATUS_SUCCESS


def _wasm_failure_status(parsed: dict | None, marker_path: Path) -> int:
    """Which failure a browser-path upload was: over-cap, paid, or unpaid.

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
    if (parsed or {}).get("error_kind") == ERROR_KIND_OVER_CAP:
        return STATUS_OVER_CAP
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
    log_level = os.environ.get("LOG_LEVEL") or "info"
    memory_profiling = os.environ.get("MEMORY_PROFILING", "false").lower() == "true"
    heaptrack_dir = Path(os.environ.get("HEAPTRACK_DIR", "/var/log/ant-heaptrack"))
    client_kind = os.environ.get("CLIENT_KIND", "native").strip().lower()
    is_wasm = client_kind == "wasm"
    ant_wasm_path = os.environ.get("ANT_WASM", "/usr/local/bin/ant-wasm")
    try:
        max_upload_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", "0"))
    except ValueError:
        max_upload_bytes = 0
    # The cap is ant-core's BROWSER cap, so it applies to the WASM transport
    # only. The native client has no such limit, and nothing should be able to
    # make it skip a file it can upload perfectly well — so the value is gated on
    # the transport here rather than trusted to be absent from the env file.
    if not is_wasm:
        max_upload_bytes = 0

    if not secret_key.startswith("0x"):
        secret_key = "0x" + secret_key

    work_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    if memory_profiling:
        heaptrack_dir.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()

    urls = _read_urls(urls_file)
    if not urls:
        print(f"No URLs found in {urls_file}; exiting.", flush=True)
        return 0

    print(
        f"prod uploader starting: host={host} region={region} "
        f"index={client_index} transport={client_kind} urls={len(urls)}",
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
            print(f"  download FAILED: {e}", file=sys.stderr, flush=True)
            failures += 1
            if influx_enabled:
                # No upload happened — total_ms is 0 so it doesn't poison
                # upload-time aggregates. The point still lands on the timeline
                # (InfluxDB server-receive time), and the `start_ns` field marks
                # when this URL was attempted.
                line = _build_line(
                    host=host,
                    region=region,
                    status=STATUS_DOWNLOAD_FAILED,
                    total_ms=0,
                    upload_secs=0.0,
                    file_size=0,
                    start_ns=download_start_ns,
                    chunks_stored=0,
                    chunks_failed=0,
                    total_chunks=0,
                    chunk_attempts_total=0,
                    retries_round_0=0,
                    retries_round_1=0,
                    retries_round_2=0,
                    retries_round_3=0,
                    store_duration_p50_ms=0,
                    store_duration_p95_ms=0,
                    store_duration_max_ms=0,
                    address="",
                    file_name=file_name,
                    source_url=url,
                    reason=str(e),
                )
                _post_influx(influx_url, influx_token, influx_org, influx_bucket, line)
            try:
                local_path.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        download_ms = (time.time_ns() - download_start_ns) // 1_000_000

        # The file is bigger than this transport will carry, so there is nothing
        # to learn from spawning a child that would refuse it before touching the
        # network. Recorded as its own status rather than as a failure: the
        # network was never asked, nothing was paid for, and the pool claim goes
        # back rather than being spent.
        if max_upload_bytes and file_size > max_upload_bytes:
            print(
                f"  over cap: {file_size} bytes exceeds the "
                f"{max_upload_bytes}-byte limit for this transport; not uploaded",
                flush=True,
            )
            if influx_enabled:
                line = _build_line(
                    host=host,
                    region=region,
                    status=STATUS_OVER_CAP,
                    total_ms=0,
                    upload_secs=0.0,
                    file_size=file_size,
                    start_ns=download_start_ns,
                    chunks_stored=0,
                    chunks_failed=0,
                    total_chunks=0,
                    chunk_attempts_total=0,
                    retries_round_0=0,
                    retries_round_1=0,
                    retries_round_2=0,
                    retries_round_3=0,
                    store_duration_p50_ms=0,
                    store_duration_p95_ms=0,
                    store_duration_max_ms=0,
                    address="",
                    file_name=file_name,
                    source_url=url,
                    reason=ERROR_KIND_OVER_CAP,
                    error_kind=ERROR_KIND_OVER_CAP,
                )
                _post_influx(influx_url, influx_token, influx_org, influx_bucket, line)
            try:
                local_path.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        print(
            f"  downloaded {file_size} bytes in {download_ms}ms; uploading...",
            flush=True,
        )

        # `start_ns` and `total_ms` describe the **upload** window only —
        # the source-file fetch happens out-of-band over HTTP and isn't
        # part of what we're measuring against the prod network.
        upload_start_ns = time.time_ns()
        safe_log_name = file_name.replace("/", "_").replace("\x00", "_") or "unnamed"
        log_basename = f"{idx:04d}-{safe_log_name}"
        stderr_log_path = log_dir / f"{log_basename}.stderr"
        stdout_log_path = log_dir / f"{log_basename}.stdout"
        heaptrack_output_base = (
            str(heaptrack_dir / f"upload-{log_basename}") if memory_profiling else None
        )
        payment_marker_path = log_dir / f"{log_basename}.paid"
        try:
            payment_marker_path.unlink(missing_ok=True)
        except OSError:
            pass
        if is_wasm:
            exit_code, stdout, stderr_text = _run_transfer(
                cmd=_build_wasm_argv(
                    ant_wasm_path=ant_wasm_path,
                    file_path=local_path,
                    payment_type=payment_type,
                    payment_marker_path=payment_marker_path,
                ),
                secret_key=secret_key,
                stderr_log_path=stderr_log_path,
                stdout_log_path=stdout_log_path,
            )
        else:
            exit_code, stdout, stderr_text = _run_ant(
                ant_path=ant_path,
                bootstrap_args=bootstrap_args,
                evm_network=evm_network,
                secret_key=secret_key,
                file_path=local_path,
                payment_type=payment_type,
                log_level=log_level,
                stderr_log_path=stderr_log_path,
                stdout_log_path=stdout_log_path,
                memory_profiling=memory_profiling,
                heaptrack_output_base=heaptrack_output_base,
            )
        end_ns = time.time_ns()
        total_ms = (end_ns - upload_start_ns) // 1_000_000

        parsed = _parse_ant_json(stdout)
        status = _classify(parsed)
        # If ant exited non-zero but emitted no parseable JSON, treat as
        # partial_upload too (we have nothing better to call it).
        if exit_code != 0 and status == STATUS_SUCCESS:
            status = STATUS_PARTIAL_UPLOAD

        error_kind = ""
        if is_wasm:
            error_kind = str((parsed or {}).get("error_kind") or "")
            # A negative return code is a signal: on a client VM that is almost
            # always the OOM killer, which reaches the WASM process (by far the
            # largest RSS there) long before this parent. The walk continues.
            if exit_code < 0:
                error_kind = ERROR_KIND_CRASH
            if status != STATUS_SUCCESS:
                status = _wasm_failure_status(parsed, payment_marker_path)

        chunks_stored = int((parsed or {}).get("chunks_stored") or 0)
        chunks_failed = int((parsed or {}).get("chunks_failed") or 0)
        total_chunks = int((parsed or {}).get("total_chunks") or 0)
        upload_secs = float((parsed or {}).get("elapsed_secs") or 0.0)
        address = str((parsed or {}).get("address") or "")
        reason = str((parsed or {}).get("reason") or "")
        # Per-upload on-chain cost (ant emits these on both success and
        # partial-failure JSON). uint256-shaped strings — kept verbatim.
        storage_cost_atto = str((parsed or {}).get("storage_cost_atto") or "")
        gas_cost_wei = str((parsed or {}).get("gas_cost_wei") or "")

        # Fields added by ant-client PR #87 (WithAutonomi). Older `ant` binaries
        # omit them — keep all zero so this script keeps working against them.
        chunk_attempts_total = int((parsed or {}).get("chunk_attempts_total") or 0)
        retries_histogram = (parsed or {}).get("retries_histogram") or [0, 0, 0, 0]
        # Defensive: clamp/pad to length 4.
        retries_histogram = (list(retries_histogram) + [0, 0, 0, 0])[:4]
        store_durations_ms = (parsed or {}).get("store_durations_ms") or []
        # Defensive: filter to ints.
        store_durations_ms = [int(v) for v in store_durations_ms if isinstance(v, (int, float))]
        store_duration_p50_ms = _percentile(store_durations_ms, 0.50)
        store_duration_p95_ms = _percentile(store_durations_ms, 0.95)
        store_duration_max_ms = max(store_durations_ms) if store_durations_ms else 0

        if status == STATUS_SUCCESS:
            print(
                f"  ok: address={address} chunks={chunks_stored}/{total_chunks} "
                f"total={total_ms}ms log={stderr_log_path}",
                flush=True,
            )
        elif status == STATUS_OVER_CAP:
            # The child resolved the file and this transport refused it. Not a
            # network failure: nothing was attempted and nothing was paid for.
            print(
                f"  over cap: this transport refuses the file (log={stderr_log_path})",
                flush=True,
            )
        else:
            failures += 1
            reason_str = f" reason={reason!r}" if reason else ""
            kind_str = f" kind={error_kind}" if error_kind else ""
            paid_str = ""
            if status == STATUS_FAILED:
                paid_str = " (no payment sent — nothing stored)"
            elif is_wasm:
                paid_str = " (payment was sent — chunks may be stored)"
            print(
                f"  FAILED: chunks={chunks_stored}/{total_chunks} "
                f"failed={chunks_failed} exit={exit_code}{kind_str} "
                f"total={total_ms}ms{reason_str}{paid_str} "
                f"stderr_log={stderr_log_path}",
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
                    f"(full log: {stderr_log_path}) ---\n"
                )
                sys.stderr.write(tail)
                if not tail.endswith("\n"):
                    sys.stderr.write("\n")
                sys.stderr.flush()

        if influx_enabled:
            line = _build_line(
                host=host,
                region=region,
                status=status,
                total_ms=total_ms,
                upload_secs=upload_secs,
                file_size=file_size,
                start_ns=upload_start_ns,
                chunks_stored=chunks_stored,
                chunks_failed=chunks_failed,
                total_chunks=total_chunks,
                chunk_attempts_total=chunk_attempts_total,
                retries_round_0=retries_histogram[0],
                retries_round_1=retries_histogram[1],
                retries_round_2=retries_histogram[2],
                retries_round_3=retries_histogram[3],
                store_duration_p50_ms=store_duration_p50_ms,
                store_duration_p95_ms=store_duration_p95_ms,
                store_duration_max_ms=store_duration_max_ms,
                address=address,
                file_name=file_name,
                source_url=url,
                reason=reason,
                storage_cost_atto=storage_cost_atto,
                gas_cost_wei=gas_cost_wei,
                error_kind=error_kind,
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
