#!/usr/bin/env python3
"""Read ONE hot file with N native `ant file download` processes, on a shared
wall-clock schedule, for the life of a testnet run.

ADR-0026 (the peer swarm: readers of a file share its records with each other
over WebRTC data channels, with the nodes introducing them and staying the last
seed) only has anything to measure when several readers read the SAME file at
the SAME time. `downloader.sh` cannot arrange that -- one process per VM, a
random address out of the whole pool, sleep 10 s, repeat -- and the browser role
(`browser_downloaders`) does arrange it, but only inside Chromium. This service
is the native mirror of that role: the one instrument that says what a swarm of
`ant` clients does.

Three things make the cohort simultaneous with no coordinator at all:

* **The round is derived from the wall clock**, not from an interval slept after
  each one: `round_id = floor(epoch / ROUND_PERIOD_SECS)`. A sleep taken after a
  round moves its start by the round's own duration, so two VMs started minutes
  apart would drift apart indefinitely. With the anchor a VM that restarts
  mid-run rejoins the cohort at the next boundary.
* **The reader's start offset is derived from the inventory**:
  `boundary + ((CLIENT_INDEX - 1) * INSTANCES + (instance - 1)) * STAGGER`. A
  cohort of 4 VMs x 4 readers with a 2 s stagger therefore starts one reader
  every 2 s for 30 s, and no VM has to be told what the others are doing. The
  stagger matters because the CLI force-exits the moment a download completes
  (ant-cli `main.rs`), so a reader stops sharing as soon as it finishes --
  overlapping them in time is the whole point.
* **The parity says which rounds are ours.** Two cohorts on one deployment -- a
  branch build on `even`, a released control on `odd` -- then never read the
  same file at the same moment, so neither is inside the other's swarm.

The `swarm` figures come from `ant --json`'s own output and a RELEASED `ant`
does not print them: `swarm` and `swarm_rooms` are absent, which is recorded as
`swarm_present=0` and zeros, never as a failure. That is what lets the control
cohort run a released binary at all.

Nothing here imports saorsa_deploy: the file is copied verbatim onto the client
VM and run by the `ant-hot-file-reader` systemd unit. Python 3.12 standard
library only (Ubuntu 24.04's `python3`).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MEASUREMENT = "hot_file_download"
#: Tag value, not a role string: it names the instrument in a dashboard, beside
#: `host_role=CLIENT` on the telegraf host metrics.
HOST_ROLE = "hot_file_reader"

STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_SKIPPED = "skipped"

# `error_kind` vocabulary. `""` on a success. The five transfer kinds are the
# same words the rest of this system classifies a failed transfer with
# (`transfer_errors.py`'s vocabulary), so a count here groups the same way; the
# three below them are this role's own.
KIND_NOT_FOUND = "not_found"
KIND_PAYMENT = "payment"
KIND_QUORUM = "quorum"
KIND_CONNECTION = "connection"
KIND_TIMEOUT = "timeout"
KIND_OTHER = "other"
#: The bytes came back but their SHA-256 is not the one the uploader published.
#: Its own kind, not `other`: a download that fails is availability, one that
#: returns DIFFERENT bytes is correctness.
KIND_VERIFY = "verify"
#: The round ran before the operator selected a hot file. A `skipped` row, part
#: of the series rather than a failure.
KIND_NO_HOT_FILES = "no_hot_files"
#: This reader's previous download was still running when this round came due.
KIND_ROUND_OVERRUN = "round_overrun"

PARITY_ALL = "all"
PARITY_EVEN = "even"
PARITY_ODD = "odd"

#: `verify_status` codes, identical to `downloader.sh`'s so the two native
#: download paths mean the same thing by them: 0 ok, 1 mismatch, 2 nothing to
#: check against. 2 is NOT a failure -- the content may be perfectly healthy,
#: there is simply no published hash for it.
VERIFY_OK = 0
VERIFY_MISMATCH = 1
VERIFY_UNAVAILABLE = 2

#: Characters of stderr kept as `error_text`. Bounded because it is an Influx
#: string field, and an `ant` backtrace is unbounded.
ERROR_TEXT_MAX = 2000

#: Grace between SIGTERM and SIGKILL when a download passes its deadline.
KILL_GRACE_SECS = 5

#: Where the client log forwarder tails from, and the coarse disk guard on the
#: append-only files it tails. Both the same as every other native client role's.
FORWARD_LOG_DIR = "/var/log/ant-client-forward"
FORWARD_LOG_MAX_BYTES = 1 << 30

#: `swarm` fields, in the order they are recorded. Flat names: Influx has no
#: nested fields, and a `discovery_` prefix is what keeps the discovery
#: counters apart from the record counters they sit beside.
SWARM_FIELDS = (
    "records_from_peers",
    "bytes_from_peers",
    "records_rejected",
    "asks_busy",
    "asks_missing",
    "asks_failed",
    "records_served",
    "bytes_served",
    "links",
    "rooms",
)
SWARM_DISCOVERY_FIELDS = (
    "lookups",
    "announces",
    "announces_failed",
    "announces_timed_out",
    "announces_refused",
    "bytes_sent",
    "bytes_received",
    "offers_made",
    "offers_delivered",
    "offers_received",
    "answers_sent",
    "answers_received",
    "channels_opened",
    "links_proven",
    "links_failed",
)


# ---------------------------------------------------------------------------
# Log forwarding
# ---------------------------------------------------------------------------


def _append_forward(path: str, text: str) -> None:
    """Append to a telegraf-tailed forward log, truncating past the cap.

    A failure to append is swallowed: log forwarding must never take the
    workload down with it.
    """
    try:
        if os.path.exists(path) and (os.path.getsize(path) > FORWARD_LOG_MAX_BYTES):
            open(path, "w").close()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        pass


class _ForwardingStream:
    """Writes through to the real stream and appends to a tailed file.

    The forwarder tails a file rather than the journal, and every other client
    role appends to that file from inside its workload script. Doing the same
    here keeps one forwarder configuration for every role -- and keeps the
    journal intact, which is what an operator reads over SSH.
    """

    def __init__(self, stream, path: str):
        self._stream = stream
        self._path = path
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        # Serialised, unlike the pointer client's: four reader threads emit
        # their point lines concurrently, and two interleaved writes would cut
        # a line in half -- which telegraf drops silently.
        with self._lock:
            written = self._stream.write(text)
            _append_forward(self._path, text)
            return written

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return False


def install_log_forwarding() -> None:
    """Tee stdout and stderr into the files the client forwarder tails."""
    os.makedirs(FORWARD_LOG_DIR, exist_ok=True)
    sys.stdout = _ForwardingStream(
        sys.stdout, os.path.join(FORWARD_LOG_DIR, "hot-file-reader.stdout.log")
    )
    sys.stderr = _ForwardingStream(
        sys.stderr, os.path.join(FORWARD_LOG_DIR, "hot-file-reader.stderr.log")
    )


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value in (None, "") else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "true" if default else "false").strip().lower() == "true"


# ---------------------------------------------------------------------------
# Round arithmetic
# ---------------------------------------------------------------------------


def round_id_at(epoch_secs: float, period_secs: int) -> int:
    """The round a point in time belongs to."""
    return int(epoch_secs // period_secs)


def boundary_of(round_id: int, period_secs: int) -> int:
    """The wall-clock second a round begins on."""
    return round_id * period_secs


def is_our_round(round_id: int, parity: str) -> bool:
    """Whether this cohort owns the round.

    `all` owns every round; `even`/`odd` own half each, so two cohorts on one
    deployment never read the same file at the same moment.
    """
    if parity == PARITY_EVEN:
        return round_id % 2 == 0
    if parity == PARITY_ODD:
        return round_id % 2 == 1
    return True


def hot_file_index_for(round_id: int, file_count: int) -> int | None:
    """Which of the selected hot files this round reads.

    `round_id mod len(files)`, as the browser harness does, so a selection of
    three is cycled and -- because the round id comes from the wall clock --
    every reader in the cohort picks the same one without being told. None when
    there is nothing to choose from, which is the "operator has not selected
    yet" case and is recorded, not failed.
    """
    if file_count <= 0:
        return None
    return round_id % file_count


def reader_offset_secs(client_index: int, instance: int, instances: int, stagger: int) -> int:
    """This reader's start offset from the round boundary.

    Deterministic from the inventory -- the VM's `client_index` and the reader's
    own number -- so the whole cohort spaces itself out with no coordinator.
    `client_index` is 1-based (it is what `clients infra` allocates) and so is
    `instance`.
    """
    if stagger <= 0:
        return 0
    return ((max(client_index, 1) - 1) * instances + (max(instance, 1) - 1)) * stagger


def last_reader_offset_secs(max_client_index: int, instances: int, stagger: int) -> int:
    """The offset of the last reader on the HIGHEST-indexed VM of a cohort.

    What `clients plan` prints, so an operator can see at a glance whether the
    cohort's starts still fit inside a round. Keyed on the highest allocated
    ``client_index``, NOT on how many VMs the cohort has: an extension's indexes
    continue from whatever the deployment already uses, so a second cohort of
    four VMs is indexes 5..8 and its last reader starts far later than a
    first cohort's would. Counting VMs instead printed 150 s for a schedule
    whose last reader actually starts at 310 s.
    """
    if stagger <= 0 or max_client_index <= 0 or instances <= 0:
        return 0
    return reader_offset_secs(max_client_index, instances, instances, stagger)


# ---------------------------------------------------------------------------
# Influx line protocol
# ---------------------------------------------------------------------------


def _escape_tag(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _escape_field_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def build_line(tags: dict, fields: dict) -> str:
    """One `hot_file_download` point in line protocol.

    Deliberately carries NO timestamp. Influx stamps the point with its own
    server-receive time, which is the only correct clock on a testnet whose
    manifests skew client clocks on purpose -- a client-supplied timestamp hours
    in the future hides behind the implicit `stop: now()` of every read query.
    The reader's own view of when its round began survives in `start_ns`.

    Integers travel with the `i` suffix: Influx types a field on first write and
    drops every later point that disagrees, so a field written once as a float
    is a float forever.
    """
    tag_part = ",".join(
        f"{_escape_tag(str(k))}={_escape_tag(str(v))}"
        for k, v in tags.items()
        if v is not None and v != ""
    )
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            parts.append(f"{key}={1 if value else 0}i")
        elif isinstance(value, int):
            parts.append(f"{key}={value}i")
        elif isinstance(value, float):
            parts.append(f"{key}={value:.6f}")
        else:
            parts.append(f'{key}="{_escape_field_string(str(value))}"')
    field_part = ",".join(parts)
    return f"{MEASUREMENT},{tag_part} {field_part}" if tag_part else f"{MEASUREMENT} {field_part}"


#: Missed rounds this service will emit `round_overrun` points for in one
#: catch-up pass. Bounded so the catch-up is O(1) however far behind the loop
#: finds itself: emitting a point for every round since the last one it ran is
#: work proportional to the lateness, which is the thing that made the loop fall
#: further behind each pass. The most recent missed rounds are the informative
#: ones; the rest are counted in a single log line.
OVERRUN_EMIT_LIMIT = 2

#: Points waiting to be posted. Bounded, because an unbounded queue against a
#: slow InfluxDB is a memory leak with extra steps; at eight readers a round
#: this is many rounds' worth of slack.
INFLUX_QUEUE_SIZE = 2048

#: Warn on the first dropped point and every Nth after it.
INFLUX_DROP_WARN_EVERY = 100


class InfluxSink:
    """Posts points from a background thread, and never blocks the workload.

    **The write is asynchronous, and that is a scheduling property rather than
    an optimisation.** Every emit used to POST synchronously, so InfluxDB's
    latency was inside the round loop: a 30 s write with a 120 s period and
    eight readers carried a round past the next boundary, the catch-up then
    wrote eight more points sequentially, and the loop fell further behind on
    every pass -- 40 overrun points and not one download after the first round,
    in a simulated-clock probe through `main`. A reader's job is to read at its
    boundary; publishing what it read must not be able to move that boundary.

    The thread is a daemon and the queue is bounded: a reader that cannot
    publish drops the point with a warning rather than waiting, because a
    missing point is a smaller lie than a round that never ran. The service is
    `Restart=on-failure` and runs forever, so there is no clean-shutdown path
    whose queue would need draining.
    """

    def __init__(self, url: str, token: str, org: str, bucket: str):
        self.url = url.rstrip("/")
        self.token = token
        self.org = org
        self.bucket = bucket
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=INFLUX_QUEUE_SIZE)
        self._worker: "threading.Thread | None" = None
        self._worker_lock = threading.Lock()
        self._dropped = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def write(self, line: str) -> None:
        """Hand a point to the publisher. Never blocks and never raises."""
        if not self.enabled:
            return
        self._ensure_worker()
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            self._dropped += 1
            # Throttled: a backlog drops a point per emit, and a warning per
            # drop would bury the reader's own download lines in its own log.
            if self._dropped == 1 or self._dropped % INFLUX_DROP_WARN_EVERY == 0:
                _warn(
                    f"InfluxDB is not keeping up; dropping points "
                    f"({self._dropped} so far). The rounds still ran."
                )

    def drain(self, timeout: float = 30.0) -> bool:
        """Wait for the queue to empty. For tests; the service never calls it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._publish_forever, name="influx-publisher", daemon=True
            )
            self._worker.start()

    def _publish_forever(self) -> None:
        while True:
            line = self._queue.get()
            try:
                self._post(line)
            except Exception as exc:  # noqa: BLE001 -- the publisher must not die
                _warn(f"Influx write failed: {exc}")
            finally:
                self._queue.task_done()

    def _post(self, line: str) -> None:
        target = (
            f"{self.url}/api/v2/write?org={urllib.parse.quote(self.org)}"
            f"&bucket={urllib.parse.quote(self.bucket)}&precision=ns"
        )
        req = urllib.request.Request(
            target,
            data=line.encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Token {self.token}",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            _warn(f"Influx write failed: {exc}")


# ---------------------------------------------------------------------------
# The hot-file manifest
# ---------------------------------------------------------------------------


def parse_hot_files(body: str) -> list[dict]:
    """The `files` array of a `browser-hot-files.json` body, or [].

    Tolerant on purpose: an unparseable or half-written document is "no hot
    files yet", which is a recorded `skipped` round rather than a crash loop on
    a VM that is billing.
    """
    try:
        document = json.loads(body)
    except ValueError:
        return []
    if not isinstance(document, dict):
        return []
    files = document.get("files")
    if not isinstance(files, list):
        return []
    return [f for f in files if isinstance(f, dict) and f.get("address")]


def fetch_hot_files(url: str, timeout: int = 30) -> list[dict]:
    """GET the selection over unsigned HTTPS.

    Unsigned, exactly as `downloader.sh` fetches the address lists: the bucket
    is public-read and the VM signs nothing, so a deliberately skewed clock
    cannot have its requests rejected (SigV4 tolerates ~15 min).
    """
    if not url:
        return []
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return parse_hot_files(resp.read().decode("utf-8", errors="replace"))
    except (
        urllib.error.URLError,
        # A body that stops short of its advertised `Content-Length` raises
        # `http.client.IncompleteRead`, which is an `HTTPException` and NOT an
        # `OSError` -- so it escaped this tuple, and with the fetch outside the
        # round's own guard the reader thread died without emitting its point.
        # One truncated response then left a permanent hole in that reader's
        # series, which is the one thing "always one point per reader per round"
        # exists to prevent.
        http.client.HTTPException,
        socket.timeout,
        OSError,
        ValueError,
    ) as exc:
        _warn(f"could not read the hot-file selection: {exc}")
        return []


# ---------------------------------------------------------------------------
# Reading `ant --json`
# ---------------------------------------------------------------------------


def parse_result(stdout_text: str) -> dict | None:
    """The result object `ant --json` printed, or None.

    The LAST line that parses as a JSON object, scanned backwards rather than
    taking the final line outright: `ant` is invoked with `-v` so that
    `RUST_LOG` has something to configure, and a tracing line landing on stdout
    after the record would otherwise turn every successful download into
    "no json result".
    """
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _int_or_zero(value) -> int:
    """A swarm counter as an int, 0 for anything that is not one.

    Zero rather than None: these are counters, and a point whose `swarm` was
    absent records zeros with `swarm_present=0` beside them, which is what says
    "this binary does not report a swarm" rather than "it reported nothing".
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def swarm_fields(result: dict | None) -> dict:
    """The 25 swarm counters, `swarm_present`, and `swarm_rooms` as JSON.

    A released `ant` (0.3.13) prints the same result object WITHOUT `swarm` and
    `swarm_rooms`, so both are treated as absent rather than as an error: that
    is what lets the control cohort of a branch-vs-release comparison run a
    released binary at all. `swarm_present` is the field that tells the two
    cases apart -- without it a zero `records_from_peers` from a released
    binary would read as a swarm that formed and shared nothing.
    """
    swarm = result.get("swarm") if isinstance(result, dict) else None
    present = isinstance(swarm, dict)
    source = swarm if present else {}
    discovery = source.get("discovery") if isinstance(source.get("discovery"), dict) else {}
    out: dict = {"swarm_present": 1 if present else 0}
    for name in SWARM_FIELDS:
        out[name] = _int_or_zero(source.get(name))
    for name in SWARM_DISCOVERY_FIELDS:
        out[f"discovery_{name}"] = _int_or_zero(discovery.get(name))
    rooms = result.get("swarm_rooms") if isinstance(result, dict) else None
    out["swarm_rooms"] = json.dumps(rooms) if isinstance(rooms, list) else "[]"
    return out


def error_block(stderr_text: str) -> str:
    """The final ``Error:`` block of a stderr stream, or ``""``.

    **Everything from the LAST ``Error: `` line to the end of stderr**, which is
    the rule `transfer_errors.py` applies for the same reason:
    ``eprintln!("Error: {e:#}")`` does not promise one physical line, and an
    error whose Display carries newlines prints several -- so taking the first
    line alone drops the cause beneath it.

    Separating the block out at all is what this role needs beyond that rule.
    `ant` is invoked with ``-v`` so that the role's `rust_log` option has
    something to configure, so stderr is a whole tracing stream with the error
    at its foot. Classifying the stream matched the startup banner: a run that
    logged ``Connected to autonomi network`` and then failed with
    ``Error: I/O error: No space left on device`` came out as `connection`,
    which attributes a full local disk to the network.

    ``""`` when nothing on stderr announced itself as an error -- the caller
    then classifies nothing and lands on `other`, which is the honest answer
    for a child that failed without saying why.
    """
    marker = "Error: "
    lines = (stderr_text or "").splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].lstrip().startswith(marker):
            return "\n".join(lines[index:])
    return ""


def classify_failure(stderr_text: str) -> str:
    """`error_kind` for a non-zero `ant file download` exit.

    Applied to the final ``Error:`` BLOCK (see :func:`error_block`), never to
    the whole ``-v`` stream. Matched as lower-cased SUBSTRINGS, and the ORDER IS
    LOAD-BEARING in the same way `transfer_errors.py`'s is: a quorum shortfall's
    sentence can carry "connect" inside it, and ant-core's chunk-exhaustion
    sentence contains "not found" while sitting inside a longer error. The
    narrowest claim wins.
    """
    text = error_block(stderr_text).lower()
    if "not found" in text:
        return KIND_NOT_FOUND
    if "payment" in text:
        return KIND_PAYMENT
    if "quorum" in text:
        return KIND_QUORUM
    for marker in ("connect", "dial", "channel"):
        if marker in text:
            return KIND_CONNECTION
    return KIND_OTHER


def clip_error_text(text: str) -> str:
    """The last :data:`ERROR_TEXT_MAX` characters of stderr.

    The TAIL, not the head: `ant`'s last words are the error, while the head of
    a `-v` run is the startup banner.
    """
    return (text or "")[-ERROR_TEXT_MAX:]


def file_sha256(path: str) -> str | None:
    """SHA-256 of a downloaded file, or None when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def verify_download(expected: str | None, path: str) -> tuple[int, str | None]:
    """``(verify_status, actual)`` for a completed download.

    The same three codes `downloader.sh` emits, and for the same reason: a line
    that published no hash leaves nothing to check against, which is its own
    answer and not a mismatch.
    """
    if not expected:
        return VERIFY_UNAVAILABLE, None
    actual = file_sha256(path)
    if actual is None:
        return VERIFY_UNAVAILABLE, None
    return (VERIFY_OK if actual == expected else VERIFY_MISMATCH), actual


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class Config:
    """Everything the service runs on, read from `/etc/ant-client.env`."""

    def __init__(self) -> None:
        self.testnet_name = _env("TESTNET_NAME")
        self.region = _env("REGION", "unknown")
        self.client_index = _env_int("CLIENT_INDEX", 1)
        self.instances = max(1, _env_int("INSTANCES", 4))
        self.round_period_secs = max(1, _env_int("ROUND_PERIOD_SECS", 600))
        self.download_timeout_secs = max(1, _env_int("DOWNLOAD_TIMEOUT_SECS", 300))
        self.round_parity = _env("ROUND_PARITY", PARITY_ALL)
        self.start_stagger_secs = max(0, _env_int("START_STAGGER_SECS", 0))
        self.hot_files_url = _env("HOT_FILES_URL")
        self.download_dir = _env("HOT_FILE_DOWNLOAD_DIR", "/var/lib/ant-client/hot-file")
        self.log_dir = _env("HOT_FILE_LOG_DIR", "/var/log/ant-hot-file-reader")
        self.ant = _env("ANT", "/root/.local/bin/ant")
        self.evm_network = _env("EVM_NETWORK", "arbitrum-sepolia")
        self.rust_log = _env("RUST_LOG")
        self.host = socket.gethostname()
        # With the peer cache enabled (the default) `ant` is invoked WITHOUT
        # --bootstrap: it reads the bootstrap_peers.toml the provisioner wrote
        # and maintains its own cache. `bootstrap_cache=false` restores the
        # explicit --bootstrap invocation, exactly as `downloader.sh` does.
        self.bootstrap_args = (
            [] if _env_bool("BOOTSTRAP_CACHE", True) else _env("BOOTSTRAP_ARGS").split()
        )
        self.sink = InfluxSink(
            _env("INFLUX_URL"),
            _env("INFLUX_TOKEN"),
            _env("INFLUX_ORG", "ant"),
            _env("INFLUX_BUCKET", "testnet-metrics"),
        )

    def offset_for(self, instance: int) -> int:
        return reader_offset_secs(
            self.client_index, instance, self.instances, self.start_stagger_secs
        )

    def command(self, address: str, output_path: str) -> list[str]:
        """The `ant` invocation for one download.

        `--json` sits BEFORE the subcommand because it is a global flag, which
        is where `uploader.sh` puts it and the only position proven to work.
        `-v` is always passed, as in `downloader.sh`: `ant` gates its tracing
        subscriber behind it, so without `-v` the role's `rust_log` option would
        be a setting that nothing applied.
        """
        return [
            self.ant,
            "-v",
            *self.bootstrap_args,
            "--evm-network",
            self.evm_network,
            "--json",
            "file",
            "download",
            address,
            "-o",
            output_path,
        ]

    def child_env(self) -> dict:
        """The environment the `ant` child runs in.

        An EMPTY `RUST_LOG` sets ant's EnvFilter to an empty filter, which
        silences everything and overrides `-v` -- so it must be REMOVED rather
        than left empty (systemd has already exported it).
        """
        env = dict(os.environ)
        if self.rust_log:
            env["RUST_LOG"] = self.rust_log
        else:
            env.pop("RUST_LOG", None)
        return env


# ---------------------------------------------------------------------------
# One reader's round
# ---------------------------------------------------------------------------


#: Serialises a whole log record -- message AND newline -- into one `write`.
#: `print()` calls `write` twice (the text, then the end), so another reader
#: thread could land between them: eight threads over 800 records produced 106
#: merged lines and 121 blank ones, including a line carrying seven download
#: records. One line per download is a contract both the forwarded log and
#: `grep` depend on, so the record is assembled first and written once.
_LOG_LOCK = threading.Lock()


def _write_line(stream, message: str) -> None:
    """Write one complete line to `stream`, atomically with respect to callers."""
    with _LOG_LOCK:
        try:
            stream.write(f"{message}\n")
            stream.flush()
        except (OSError, ValueError):
            # A closed or broken stream must not take a reader down with it.
            pass


def _log(message: str) -> None:
    _write_line(sys.stdout, f"[{time.strftime('%H:%M:%S', time.gmtime())}] {message}")


def _warn(message: str) -> None:
    """A warning, written whole for the same reason :func:`_log` is."""
    _write_line(sys.stderr, f"WARN: {message}")


def _point_log(round_id: int, instance: int, fields: dict) -> None:
    """The one grep-able line per download.

    Deliberately the same keys as the Influx point's, so a question answered off
    the forwarded log and one answered off the bucket cannot disagree.
    """
    _log(
        f"round={round_id} instance={instance} status={fields['status']} "
        f"kind={fields.get('error_kind') or '-'} total_ms={fields.get('total_ms', 0)} "
        f"records_from_peers={fields.get('records_from_peers', 0)} "
        f"bytes_from_peers={fields.get('bytes_from_peers', 0)} "
        f"address={fields.get('address') or '-'}"
    )


def _base_fields(start_ns: int) -> dict:
    """The fields every point carries, whatever happened.

    One point per reader per round of this cohort's parity, ALWAYS -- including
    the rounds that did nothing. A round with no point is itself a finding, and
    it only reads that way if a skip is a point.
    """
    fields = {
        "status": STATUS_SKIPPED,
        "error_kind": "",
        "error_text": "",
        "address": "",
        "file_size": 0,
        "total_ms": 0,
        "client_elapsed_ms": 0,
        "start_ns": start_ns,
        "started_late_ms": 0,
        "verify_status": VERIFY_UNAVAILABLE,
    }
    fields.update(swarm_fields(None))
    return fields


def emit(cfg: Config, round_id: int, instance: int, hot_file_index, fields: dict) -> None:
    """Write one point and its log line."""
    tags = {
        "host": cfg.host,
        "testnet_name": cfg.testnet_name,
        "host_role": HOST_ROLE,
        "region": cfg.region,
        "round_id": round_id,
        "instance": instance,
    }
    if hot_file_index is not None:
        tags["hot_file_index"] = hot_file_index
    _point_log(round_id, instance, fields)
    cfg.sink.write(build_line(tags, fields))


def run_download(cfg: Config, round_id: int, instance: int, entry: dict, start_ns: int) -> dict:
    """Spawn `ant`, wait on the round's deadline, and build the point's fields.

    The deadline is measured from the ROUND BOUNDARY rather than from this
    reader's own staggered start: what it is for is that every reader has
    finished before the next round is due.
    """
    fields = _base_fields(start_ns)
    address = str(entry.get("address") or "")
    expected_bytes = _int_or_zero(entry.get("bytes"))
    expected_sha = entry.get("sha256") or None
    fields["address"] = address
    fields["file_size"] = expected_bytes

    boundary = boundary_of(round_id, cfg.round_period_secs)
    deadline = boundary + cfg.download_timeout_secs
    output_path = os.path.join(cfg.download_dir, f"{round_id}-{instance}")
    stdout_path = os.path.join(cfg.log_dir, f"{round_id}-{instance}.stdout")
    stderr_path = os.path.join(cfg.log_dir, f"{round_id}-{instance}.stderr")
    os.makedirs(cfg.download_dir, exist_ok=True)
    os.makedirs(cfg.log_dir, exist_ok=True)

    # The deadline is checked BEFORE the spawn, and the remaining time is never
    # floored upwards. A reader whose own staggered start lands at or past
    # `boundary + download_timeout_secs` must not run at all: `ant` would still
    # be transferring inside the NEXT round, which on a parity-split deployment
    # is the other cohort's, and its reads would join their swarm. (Spawning and
    # then granting `max(1.0, ...)` did exactly that, and reported success.)
    remaining = deadline - time.time()
    if remaining <= 0:
        fields["status"] = STATUS_FAILURE
        fields["error_kind"] = KIND_TIMEOUT
        fields["error_text"] = "deadline passed before start"
        return fields

    timed_out = False
    started = time.time()
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(
            cfg.command(address, output_path),
            stdout=out,
            stderr=err,
            env=cfg.child_env(),
        )
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                proc.wait(timeout=KILL_GRACE_SECS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    fields["total_ms"] = int((time.time() - started) * 1000)
    exit_code = proc.returncode

    def _read(path: str) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    keep_logs = True
    if timed_out:
        fields["status"] = STATUS_FAILURE
        fields["error_kind"] = KIND_TIMEOUT
        fields["error_text"] = "deadline"
    elif exit_code != 0:
        stderr_text = _read(stderr_path)
        fields["status"] = STATUS_FAILURE
        fields["error_kind"] = classify_failure(stderr_text)
        fields["error_text"] = clip_error_text(stderr_text)
        # The swarm counters of a FAILED download are still worth having: a
        # reader that received records from a peer and then failed says
        # something different from one that never reached the swarm at all.
        fields.update(swarm_fields(parse_result(_read(stdout_path))))
    else:
        result = parse_result(_read(stdout_path))
        if result is None:
            fields["status"] = STATUS_FAILURE
            fields["error_kind"] = KIND_OTHER
            fields["error_text"] = "no json result"
        else:
            fields.update(swarm_fields(result))
            fields["client_elapsed_ms"] = int(float(result.get("elapsed_secs") or 0) * 1000)
            reported = _int_or_zero(result.get("size"))
            verify_status, actual = verify_download(expected_sha, output_path)
            fields["verify_status"] = verify_status
            if expected_bytes and reported != expected_bytes:
                fields["status"] = STATUS_FAILURE
                fields["error_kind"] = KIND_OTHER
                fields["error_text"] = f"size mismatch: got {reported}"
            elif verify_status == VERIFY_MISMATCH:
                fields["status"] = STATUS_FAILURE
                fields["error_kind"] = KIND_VERIFY
                fields["error_text"] = f"sha256 mismatch: expected {expected_sha} got {actual}"
            else:
                fields["status"] = STATUS_SUCCESS
                keep_logs = False

    # The downloaded file always goes: a 300 MiB file per reader per round fills
    # the VM's disk within the hour, and the only thing wanted off it (the hash)
    # has already been taken.
    try:
        os.remove(output_path)
    except OSError:
        pass
    if not keep_logs:
        # Only a FAILED download's `ant` output is kept, exactly as
        # `downloader.sh` does: a run is thousands of downloads and only the
        # failures need diagnosing.
        for path in (stdout_path, stderr_path):
            try:
                os.remove(path)
            except OSError:
                pass
    return fields


def run_reader_round(cfg: Config, round_id: int, instance: int) -> None:
    """One reader's whole round, and the guarantee that it emits a point.

    **Every path out of a round writes exactly one point**, which is what makes
    a missing point a finding rather than an ambiguity. So the whole body is
    guarded, not just the download: the guard inside
    :func:`_reader_round_body` keeps the richer fields (the address, the
    lateness) for the common case, and this one is the backstop for everything
    else -- the manifest fetch above all, where a body truncated short of its
    `Content-Length` used to kill the thread outright and leave a permanent hole
    in that reader's series.

    The arithmetic here cannot raise, so the handler always has a `start_ns` to
    stamp the point with.
    """
    start_ns = (
        boundary_of(round_id, cfg.round_period_secs) + cfg.offset_for(instance)
    ) * 1_000_000_000
    try:
        _reader_round_body(cfg, round_id, instance)
    except Exception as exc:  # noqa: BLE001 -- a reader must keep emitting points
        fields = _base_fields(start_ns)
        fields["status"] = STATUS_FAILURE
        fields["error_kind"] = KIND_OTHER
        fields["error_text"] = clip_error_text(f"{type(exc).__name__}: {exc}")
        try:
            emit(cfg, round_id, instance, None, fields)
        except Exception:  # noqa: BLE001 -- nothing left to report it with
            pass


def _reader_round_body(cfg: Config, round_id: int, instance: int) -> None:
    """Fetch the selection, wait this reader's turn, download, emit."""
    boundary = boundary_of(round_id, cfg.round_period_secs)
    offset = cfg.offset_for(instance)
    scheduled = boundary + offset
    start_ns = scheduled * 1_000_000_000

    # An IMPOSSIBLE start is refused before the sleep, not after it. A stagger
    # long enough to put a reader's start at or past its own round deadline is a
    # start that can never run -- and sleeping to it anyway blocked the whole
    # cohort: `main` joins every reader thread, so one reader waiting until
    # boundary + 720 s of a 600 s round held the loop past the NEXT round's
    # boundary and every reader, including the ones that finished on time, took
    # a `round_overrun` skip for a round they could have run. The arithmetic is
    # deterministic, so this fires on the first round and costs no clock read.
    #
    # A long stagger is deliberately allowed (`clients plan` warns rather than
    # refusing), so the answer is to record THIS reader's timeout and leave the
    # others alone.
    if offset >= cfg.download_timeout_secs:
        fields = _base_fields(start_ns)
        fields["status"] = STATUS_FAILURE
        fields["error_kind"] = KIND_TIMEOUT
        fields["error_text"] = (
            f"start offset {offset}s is at or past the {cfg.download_timeout_secs}s deadline"
        )
        emit(cfg, round_id, instance, None, fields)
        return

    files = fetch_hot_files(cfg.hot_files_url)
    index = hot_file_index_for(round_id, len(files))
    if index is None:
        fields = _base_fields(start_ns)
        fields["error_kind"] = KIND_NO_HOT_FILES
        emit(cfg, round_id, instance, None, fields)
        return

    delay = scheduled - time.time()
    if delay > 0:
        time.sleep(delay)
    # Clamped at 0: a reader cannot start early, and a negative "lateness" from
    # a sleep that returned a hair ahead of the wall clock would be read as one.
    started_late_ms = max(0, int((time.time() - scheduled) * 1000))

    try:
        fields = run_download(cfg, round_id, instance, files[index], start_ns)
    except Exception as exc:  # noqa: BLE001 -- a reader must keep emitting points
        fields = _base_fields(start_ns)
        fields["status"] = STATUS_FAILURE
        fields["error_kind"] = KIND_OTHER
        fields["error_text"] = clip_error_text(f"{type(exc).__name__}: {exc}")
        fields["address"] = str(files[index].get("address") or "")
    fields["started_late_ms"] = started_late_ms
    emit(cfg, round_id, instance, index, fields)


def emit_overrun(cfg: Config, round_id: int) -> None:
    """One `skipped` / `round_overrun` point per reader for a round we missed.

    Cannot happen while `download_timeout_secs < round_period_secs`, which is
    enforced in the schema and in the spec parser -- the guard is cheap and the
    alternative is a round that silently produced nothing.
    """
    boundary = boundary_of(round_id, cfg.round_period_secs)
    for instance in range(1, cfg.instances + 1):
        fields = _base_fields((boundary + cfg.offset_for(instance)) * 1_000_000_000)
        fields["error_kind"] = KIND_ROUND_OVERRUN
        emit(cfg, round_id, instance, None, fields)


def sleep_until(when: float) -> None:
    delay = when - time.time()
    if delay > 0:
        time.sleep(delay)


def _resynchronise(cfg: Config, round_id: int) -> int:
    """The next round to run, having accounted for any the loop has missed.

    Rounds whose boundary has already gone by cannot be run, so they are
    reported as `round_overrun` -- but **at most
    :data:`OVERRUN_EMIT_LIMIT` of them**, and the loop always returns to the
    present. Emitting a point for every missed round is work proportional to
    how late the service is, which is what let a slow InfluxDB push the loop
    further behind on every pass until it stopped reading altogether. The most
    recent missed rounds are the ones worth a point; the rest are a count.

    Returns the first round whose boundary is still ahead, so the caller's next
    `sleep_until` moves forward rather than catching up.
    """
    now = time.time()
    missed = []
    while boundary_of(round_id, cfg.round_period_secs) <= now:
        if is_our_round(round_id, cfg.round_parity):
            missed.append(round_id)
        round_id += 1
    if not missed:
        return round_id
    emitted = missed[-OVERRUN_EMIT_LIMIT:]
    for missed_round in emitted:
        emit_overrun(cfg, missed_round)
    unreported = len(missed) - len(emitted)
    if unreported:
        _log(
            f"rounds={missed[0]}..{emitted[0] - 1} overrun=unreported "
            f"count={unreported} (catch-up is capped at "
            f"{OVERRUN_EMIT_LIMIT} round(s) so it cannot itself fall behind)"
        )
    return round_id


def main() -> int:
    if _env_bool("FORWARD_LOGS"):
        install_log_forwarding()
    cfg = Config()
    _log(
        f"Hot-file reader starting: host={cfg.host} region={cfg.region} "
        f"client_index={cfg.client_index} instances={cfg.instances} "
        f"round={cfg.round_period_secs}s deadline={cfg.download_timeout_secs}s "
        f"parity={cfg.round_parity} stagger={cfg.start_stagger_secs}s "
        f"hot_files={cfg.hot_files_url or '<none>'}"
    )
    # Join at the NEXT boundary: a reader's offset is measured from the
    # boundary, and the current round's has already passed.
    round_id = round_id_at(time.time(), cfg.round_period_secs) + 1
    while True:
        boundary = boundary_of(round_id, cfg.round_period_secs)
        if not is_our_round(round_id, cfg.round_parity):
            # No process, no point, nothing but this line: a round another
            # cohort owns is not a round this one skipped doing work in.
            _log(f"round={round_id} parity=skip")
            sleep_until(boundary)
            round_id += 1
            continue
        sleep_until(boundary)
        threads = [
            threading.Thread(
                target=run_reader_round,
                args=(cfg, round_id, instance),
                name=f"reader-{instance}",
                daemon=True,
            )
            for instance in range(1, cfg.instances + 1)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        round_id = _resynchronise(cfg, round_id + 1)


if __name__ == "__main__":
    sys.exit(main())
