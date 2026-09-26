#!/usr/bin/env python3
"""Exercise pointers -- paid, owner-signed mutable references -- for the life of
a testnet run, driving the native `ant pointer` subcommands over `--json`.

A pointer is not a file, so this is not a file-transfer client with a flag set.
What it measures is whether a reference that was written can be read back, and
whether it goes stale with age. ADR-0016 is explicit that a non-zero stale-read
rate is EXPECTED -- an eclipsed reader can be handed an older, validly signed
state and cannot tell -- so the measurement that matters is the trend, which is
why every read records the pointer's age and own and foreign pointers are kept
apart.

Three loops, one thread
-----------------------

Create, update and sweep run on one scheduler in one process, strictly serially.
They share a single wallet, and every write is a paid on-chain transaction, so
two concurrent writes would race the same nonce -- the failure mode that cost
staging run #566 six uploaders. Whichever loop is due soonest runs next; a loop
that overruns simply delays itself, it is never run twice concurrently.

Cost is measured as PAID, never as quoted
-----------------------------------------

`ant pointer create --json` prints `{"address": ...}` and nothing else: there is
no cost in its output to report. So spend is read from the wallet either side of
each write -- ANT via `ant wallet balance`, native gas via a plain
`eth_getBalance` against the same RPC the payment settles on. That is also the
right answer on its own terms: the ADR warns that a write which times out and is
retried pays twice, and only a balance delta can see that.

Nothing here imports saorsa_deploy: the file is copied verbatim onto the client
VM and run by the `ant-pointer` systemd unit.
"""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MEASUREMENT = "pointer"

OP_CREATE = "create"
OP_UPDATE = "update"
OP_GET = "get"
OP_SWEEP = "sweep"
OP_RESOLVE = "resolve"

# Outcome classes. Every op lands in exactly one, and they are the vocabulary
# `deployments print-pointers` and the `[pointer]` gates are written in.
OUTCOME_OK = "ok"
#: Observed counter below what we wrote, or a foreign counter going backwards.
#: Expected at a non-zero rate -- see the module docstring.
OUTCOME_STALE = "stale"
#: An update refused because the pointer moved while the write was in flight.
OUTCOME_LOST_WRITE = "lost_write"
OUTCOME_NOT_FOUND = "not_found"
#: The close group could not agree, or too few of it answered. Distinct from an
#: error: the network said "I cannot tell you", not "that failed".
OUTCOME_READ_SHORTFALL = "read_shortfall"
#: A counter ABOVE what we wrote. Only a foreign write to a pointer we own could
#: cause it, so it is never routine.
OUTCOME_ANOMALY = "anomaly"
OUTCOME_ERROR = "error"

# Markers matched against `ant`'s stderr, taken from the strings ant-core and
# ant-cli actually emit on the pointer branch. Substrings, never whole lines:
# each interpolates an address or a count.
#
# `Error::CloseGroupShortfall` renders as "close-group PUT shortfall (dial
# churn): ..." for BOTH reads and writes -- the variant predates pointers and
# its Display string still says PUT. Matching the shortfall wording rather than
# the word "PUT" is what keeps a read shortfall out of the error bucket.
SHORTFALL_MARKER = "close-group PUT shortfall"
#: The read-side shortfalls, worded by `pointer_get`.
SHORTFALL_READ_MARKERS = (
    "of the close group answered for pointer",
    "was named by",
    "no close-group peer answered for the pointer",
    "no peer is responsible for pointer",
)
LOST_WRITE_MARKER = "moved while this update was in flight"
#: `ant pointer get` on an absent address exits 1 with this. It is an ANSWER,
#: not a failure: the close group agreed the pointer is not there.
NOT_FOUND_MARKERS = ("No pointer at", "pointer chain breaks at")
ALREADY_EXISTS_MARKER = "already exists at counter"

#: Sent on the JSON-RPC balance read. The public Arbitrum Sepolia endpoint
#: answers **403 Forbidden** to urllib's default `Python-urllib/3.x` UA, so
#: without this every gas measurement silently failed and every write recorded
#: no ETH cost at all. Measured on the VM: 403 with the default UA, 200 with any
#: other.
RPC_USER_AGENT = "saorsa-deploy-pointer-client/1.0"

#: Cap the stderr we echo to journald per failure. `ant -v` is verbose enough to
#: trip journald's per-unit rate limit and starve earlier entries.
STDERR_TAIL_LINES = 40

#: A pointer address / target is 32 bytes, hex-encoded.
ADDRESS_HEX_LEN = 64
_HEX_ADDRESS = re.compile(r"^[0-9a-fA-F]{%d}$" % ADDRESS_HEX_LEN)

#: Where the client->Elasticsearch telegraf forwarder tails, when a client sets
#: `forward_logs`. Matching the native clients' directory so one forwarder
#: configuration covers every role.
FORWARD_LOG_DIR = "/var/log/ant-client-forward"
#: Coarse disk guard for the append-only forward logs, as in `uploader.sh`:
#: truncate past this rather than fill the VM.
FORWARD_LOG_MAX_BYTES = 1 << 30


class _ForwardingStream:
    """Writes through to the real stream and appends to a tailed file.

    The forwarder tails a file rather than the journal, and the native clients
    append to that file from inside the workload script. Doing the same here
    keeps one forwarder configuration for every client role -- and keeps the
    journal intact, which is what an operator reads over SSH.

    A failure to append is swallowed: log forwarding must never take the
    workload down with it.
    """

    def __init__(self, stream, path: str):
        self._stream = stream
        self._path = path

    def write(self, text: str) -> int:
        written = self._stream.write(text)
        try:
            if os.path.exists(self._path) and (os.path.getsize(self._path) > FORWARD_LOG_MAX_BYTES):
                open(self._path, "w").close()
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass
        return written

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return False


def install_log_forwarding() -> None:
    """Tee stdout and stderr into the files the client forwarder tails."""
    os.makedirs(FORWARD_LOG_DIR, exist_ok=True)
    sys.stdout = _ForwardingStream(sys.stdout, os.path.join(FORWARD_LOG_DIR, "pointer.stdout.log"))
    sys.stderr = _ForwardingStream(sys.stderr, os.path.join(FORWARD_LOG_DIR, "pointer.stderr.log"))


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value in (None, "") else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "true" if default else "false").strip().lower() == "true"


# ---------------------------------------------------------------------------
# Influx line protocol
# ---------------------------------------------------------------------------


def _escape_tag(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _escape_field_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_line(host: str, region: str, op: str, fields: dict) -> str:
    """One `pointer` point in line protocol.

    Deliberately carries NO timestamp. Influx stamps the point with its own
    server-receive time, which is the only correct clock here: testnet manifests
    skew client clocks on purpose, and a client-supplied timestamp hours in the
    future is hidden behind the implicit `stop: now()` of every read query. The
    client's own view of when the op began survives in `start_ns`.

    Integers travel as `i` so Influx does not infer a float; balances exceed
    i64 (1000 ANT is 1e21 atto) and travel as strings, with only the delta -- a
    single write's worth -- emitted numerically.
    """
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
    tag_set = f"host={_escape_tag(host)},region={_escape_tag(region)},op={_escape_tag(op)}"
    return f"{MEASUREMENT},{tag_set} {','.join(parts)}"


class InfluxSink:
    """Posts points, and never lets a metrics failure stop the workload."""

    def __init__(self, url: str, token: str, org: str, bucket: str):
        self.url = url.rstrip("/")
        self.token = token
        self.org = org
        self.bucket = bucket

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def write(self, line: str) -> None:
        if not self.enabled:
            return
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
            print(f"WARN: Influx write failed: {exc}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# The durable ledger
# ---------------------------------------------------------------------------


class Ledger:
    """What this client believes about every pointer it knows.

    Durable, because the unit is `Restart=always` and a restart that forgot the
    owner keys would abandon every pointer it had paid for -- ownership is
    immutable, so a lost key is a pointer that can never be updated again. It is
    also what "stale" is measured against: without the expected counter, a read
    has nothing to be wrong about.

    Written atomically (temp file + `os.replace`) so a crash mid-write leaves the
    previous ledger rather than a truncated one.
    """

    def __init__(self, path: Path):
        self.path = path
        self.own: dict[str, dict] = {}
        self.foreign: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.own = dict(data.get("own") or {})
        self.foreign = dict(data.get("foreign") or {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"own": self.own, "foreign": self.foreign}))
        os.replace(tmp, self.path)

    def record_own(self, address: str, key_path: str, counter: int, target: str, kind: str) -> None:
        entry = self.own.get(address) or {"created_at": time.time()}
        entry.update(
            {
                "key": key_path,
                "counter": counter,
                "target": target,
                "target_kind": kind,
            }
        )
        self.own[address] = entry
        self.save()

    def note_key(self, address: str, key_path: str) -> None:
        """Remember an owner key before the create that pays for it.

        A key written but not recorded is a key the service would never look at
        again. Since ownership is immutable and the address is derived from the
        key, a create that crashes after paying is recoverable only if the key is
        already known -- so it is written down first, with no counter, and the
        create fills the rest in.
        """
        self.own.setdefault(address, {"key": key_path, "counter": None, "created_at": time.time()})
        self.save()

    def note_foreign(self, address: str, counter: int) -> None:
        entry = self.foreign.get(address) or {"first_seen": time.time()}
        entry["counter"] = counter
        self.foreign[address] = entry
        self.save()

    def add_foreign(self, address: str) -> bool:
        """Register a foreign pointer. True when it was not already known."""
        if address in self.own or address in self.foreign:
            return False
        self.foreign[address] = {"first_seen": time.time(), "counter": None}
        return True

    def established_own(self) -> list[str]:
        """Own pointers that have actually been created (a counter is known)."""
        return [a for a, e in self.own.items() if e.get("counter") is not None]


# ---------------------------------------------------------------------------
# Running `ant`
# ---------------------------------------------------------------------------


class AntResult:
    def __init__(self, rc: int, stdout: str, stderr: str, duration_ms: int):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.duration_ms = duration_ms

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def json(self) -> dict:
        """The last JSON object on stdout, or {}.

        From the END because `-v` output can interleave: ant writes trace to
        stderr, but heaptrack-style wrappers and shell noise have historically
        reached stdout on other client paths and the from-the-end parse is what
        survives them.
        """
        for line in reversed(self.stdout.strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                return json.loads(line)
            except ValueError:
                continue
        return {}


def classify_failure(stderr: str, *, write: bool) -> str:
    """Map `ant`'s stderr to an outcome class.

    The ordering matters. A lost write and a close-group shortfall are both
    reported as a failed command with exit 1, and both are ordinary events on a
    healthy network -- lumping either into `error` would make the error rate a
    measure of contention rather than of faults.
    """
    if any(marker in stderr for marker in NOT_FOUND_MARKERS):
        return OUTCOME_NOT_FOUND
    if LOST_WRITE_MARKER in stderr:
        return OUTCOME_LOST_WRITE
    if SHORTFALL_MARKER in stderr or any(marker in stderr for marker in SHORTFALL_READ_MARKERS):
        # A write that fell short of its quorum is a lost write from the
        # client's point of view -- it paid and the state did not land -- while a
        # read that fell short simply has no answer to give.
        return OUTCOME_LOST_WRITE if write else OUTCOME_READ_SHORTFALL
    return OUTCOME_ERROR


def error_summary(stderr: str) -> str:
    """The first `Error: ...` line, trimmed. Empty when there is none."""
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("Error:"):
            return line[:240]
    tail = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    return tail[-1][:240] if tail else ""


class AntRunner:
    def __init__(
        self,
        ant_path: str,
        bootstrap_args: list[str],
        evm_network: str,
        rust_log: str,
        timeout_secs: int,
    ):
        self.ant_path = ant_path
        self.bootstrap_args = bootstrap_args
        self.evm_network = evm_network
        self.rust_log = rust_log
        self.timeout_secs = timeout_secs

    def _env(self) -> dict:
        env = dict(os.environ)
        # `ant` installs no tracing subscriber without `-v`, and an EMPTY
        # RUST_LOG sets its EnvFilter to a filter that silences everything --
        # overriding `-v`. systemd always exports the variable, so it has to be
        # REMOVED rather than left empty.
        if self.rust_log:
            env["RUST_LOG"] = self.rust_log
        else:
            env.pop("RUST_LOG", None)
        return env

    def run(self, args: list[str], *, bootstrap: bool = True, evm: bool = True) -> AntResult:
        """Invoke `ant`. The two flag groups are independent, and must be.

        `--evm-network` is needed by every command that touches a chain, which
        includes `wallet balance` -- it is a single EVM read and no peers are
        involved, so it takes `evm=True, bootstrap=False`. Collapsing the two
        into one switch would have left the balance read on ant's DEFAULT
        network, i.e. Arbitrum One: every pointer write's cost would have been
        measured against a mainnet wallet that holds nothing, and every write
        would have recorded a spend of zero.

        `keygen` and `address` need neither.
        """
        argv = [self.ant_path, "-v"]
        if bootstrap:
            argv += self.bootstrap_args
        if evm:
            argv += ["--evm-network", self.evm_network]
        argv += ["--json"] + args
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_secs,
                env=self._env(),
            )
            rc, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc = 124
            out = exc.stdout if isinstance(exc.stdout, str) else ""
            err = (exc.stderr if isinstance(exc.stderr, str) else "") + (
                f"\nError: ant timed out after {self.timeout_secs}s"
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        return AntResult(rc, out, err, duration_ms)


# ---------------------------------------------------------------------------
# Wallet balances -- what a write actually cost
# ---------------------------------------------------------------------------


class Wallet:
    """ANT and native balances, read either side of every paid write.

    ANT comes from `ant wallet balance`, native gas from a plain
    `eth_getBalance` against the RPC the payment settles on. Both return None on
    failure rather than raising: a balance we could not read must not fail the
    write, it must simply leave the cost unrecorded.
    """

    def __init__(self, runner: AntRunner, rpc_url: str):
        self.runner = runner
        self.rpc_url = rpc_url
        self.address = self._resolve_address()

    def _resolve_address(self) -> str:
        if not self.rpc_url:
            return ""
        result = self.runner.run(["wallet", "address"], bootstrap=False)
        for line in reversed(result.stdout.strip().splitlines()):
            match = re.search(r"0x[0-9a-fA-F]{40}", line)
            if match:
                return match.group(0)
        return ""

    def ant_atto(self) -> int | None:
        result = self.runner.run(["wallet", "balance"], bootstrap=False)
        if not result.ok:
            return None
        tail = result.stdout.strip().splitlines()
        if not tail:
            return None
        value = tail[-1].strip()
        return int(value) if value.isdigit() else None

    def gas_wei(self) -> int | None:
        if not (self.rpc_url and self.address):
            return None
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getBalance",
                "params": [self.address, "latest"],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self.rpc_url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": RPC_USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, socket.timeout, OSError, ValueError):
            return None
        raw = body.get("result")
        if not isinstance(raw, str):
            return None
        try:
            return int(raw, 16)
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Targets and published address lists
# ---------------------------------------------------------------------------


def parse_address_lines(text: str) -> list[str]:
    """Addresses from a published list. Lines are `<address>[ <bytes>]`."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        address = line.split()[0]
        if _HEX_ADDRESS.match(address):
            out.append(address)
    return out


def is_pointer_key(key: str) -> bool:
    """Whether a published object holds pointer addresses rather than files."""
    return key.rsplit("/", 1)[-1].startswith("pointer-")


class AddressPool:
    """The deployment's shared published-address prefix, read over public HTTPS.

    Unsigned, exactly as `downloader.sh` reads it: the bucket is public-read and
    a SigV4-signed request from a deliberately clock-skewed VM is rejected at
    about 15 minutes of drift, which would silently starve this client of both
    its chunk targets and its foreign pointers.
    """

    def __init__(self, bucket: str, prefix: str, region: str, refresh_secs: int = 300):
        self.bucket = bucket
        self.prefix = prefix
        self.region = region or "eu-west-2"
        self.refresh_secs = refresh_secs
        self._last_refresh = 0.0
        self.chunk_targets: list[str] = []
        self.foreign_pointers: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.bucket and self.prefix)

    def _base_url(self) -> str:
        return f"https://{self.bucket}.s3.{self.region}.amazonaws.com"

    def _fetch(self, url: str) -> str:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _list_keys(self) -> list[str]:
        url = f"{self._base_url()}/?list-type=2&prefix={urllib.parse.quote(self.prefix + '/')}"
        xml = self._fetch(url)
        return re.findall(r"<Key>([^<]+)</Key>", xml)

    def refresh(self, own_key: str) -> None:
        """Re-read the prefix. `own_key` is this client's own object, skipped."""
        if not self.enabled:
            return
        now = time.time()
        if now - self._last_refresh < self.refresh_secs:
            return
        self._last_refresh = now
        chunks: list[str] = []
        pointers: list[str] = []
        try:
            keys = self._list_keys()
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            print(f"WARN: address listing failed: {exc}", file=sys.stderr, flush=True)
            return
        for key in keys:
            name = key.rsplit("/", 1)[-1]
            if name == own_key:
                continue
            try:
                body = self._fetch(f"{self._base_url()}/{urllib.parse.quote(key)}")
            except (urllib.error.URLError, socket.timeout, OSError):
                continue
            if is_pointer_key(key):
                pointers.extend(parse_address_lines(body))
            else:
                chunks.extend(parse_address_lines(body))
        if chunks:
            self.chunk_targets = chunks
        self.foreign_pointers = pointers

    def random_chunk_target(self) -> str:
        """A real data-map address when the uploaders have published one.

        Falls back to a random 32-byte address so the role never depends on the
        uploader tier: a node stores 33 opaque bytes and never reads the target,
        so a synthetic one exercises the same write path. Only `resolve` can tell
        the difference, and it is only run against chained pointers.
        """
        if self.chunk_targets:
            return random.choice(self.chunk_targets)
        return secrets.token_hex(32)


def publish(addresses: list[str], path: Path, presigned_url: str) -> bool:
    """Write this client's pointer addresses out and PUT them to S3.

    Through a presigned URL, as the uploaders do: the signature is the
    controller's, so the flush keeps working on a VM whose clock is hours out.
    Insists on a literal 200 -- a 307 from a presigned URL minted against the
    wrong endpoint is not an error to `urlopen`, and the flush would silently
    store nothing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{address}\n" for address in sorted(addresses))
    path.write_text(body)
    if not presigned_url:
        return False
    req = urllib.request.Request(presigned_url, data=body.encode("utf-8"), method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status == 200
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        print(f"WARN: address flush failed: {exc}", file=sys.stderr, flush=True)
        return False


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class PointerClient:
    def __init__(self) -> None:
        self.host = socket.gethostname()
        self.region = _env("REGION", "unknown")
        self.client_index = _env("CLIENT_INDEX", "0")
        self.data_dir = Path(_env("POINTER_DATA_DIR", "/var/lib/ant-pointer"))
        self.keys_dir = self.data_dir / "keys"
        self.ledger = Ledger(self.data_dir / "ledger.json")

        bootstrap_cache = _env_bool("BOOTSTRAP_CACHE", True)
        bootstrap_args = _env("BOOTSTRAP_ARGS").split() if not bootstrap_cache else []
        self.runner = AntRunner(
            ant_path=_env("ANT", "/root/.local/bin/ant"),
            bootstrap_args=bootstrap_args,
            evm_network=_env("EVM_NETWORK", "arbitrum-sepolia"),
            rust_log=_env("RUST_LOG"),
            timeout_secs=_env_int("POINTER_OP_TIMEOUT_SECS", 600),
        )
        self.wallet = Wallet(self.runner, _env("RPC_URL"))
        self.influx = InfluxSink(
            _env("INFLUX_URL"),
            _env("INFLUX_TOKEN"),
            _env("INFLUX_ORG", "ant"),
            _env("INFLUX_BUCKET", "testnet-metrics"),
        )
        self.pool = AddressPool(
            _env("ADDRESSES_S3_BUCKET"),
            _env("ADDRESSES_S3_PREFIX"),
            _env("AWS_DEFAULT_REGION", "eu-west-2"),
            refresh_secs=_env_int("POINTER_POOL_REFRESH_SECS", 300),
        )
        self.own_key_name = f"pointer-{self.region}-{self.client_index}.txt"
        self.addresses_file = self.data_dir / self.own_key_name
        self.presigned_url = _env("UPLOAD_PRESIGNED_URL")

        self.create_interval = _env_int("CREATE_INTERVAL_SECS", 60)
        self.update_interval = _env_int("UPDATE_INTERVAL_SECS", 30)
        self.sweep_interval = _env_int("SWEEP_INTERVAL_SECS", 600)
        self.chain_fraction = _env_float("CHAIN_FRACTION", 0.10)
        self.pointer_max = _env_int("POINTER_MAX", 0)
        self.seq = 0

    # -- emitting ----------------------------------------------------------

    def emit(self, op: str, fields: dict) -> None:
        self.influx.write(build_line(self.host, self.region, op, fields))

    def _spend_fields(
        self,
        ant_before: int | None,
        ant_after: int | None,
        gas_before: int | None,
        gas_after: int | None,
    ) -> dict:
        fields: dict = {}
        if ant_before is not None and ant_after is not None:
            fields["wallet_spend_atto"] = ant_before - ant_after
        if ant_before is not None:
            fields["wallet_before_atto"] = str(ant_before)
        if ant_after is not None:
            fields["wallet_after_atto"] = str(ant_after)
        if gas_before is not None and gas_after is not None:
            fields["gas_spend_wei"] = gas_before - gas_after
        return fields

    # -- reads -------------------------------------------------------------

    def read_pointer(self, address: str) -> tuple[str, dict, AntResult, int]:
        """`ant pointer get`, classified.

        Returns ``(outcome, record, result, start_ns)``. The start is captured
        here, BEFORE the read, because it is what `record-results` derives
        `started_at` from: stamping it afterwards recorded every read as having
        begun after it finished, so `started_at` + `duration_ms` disagreed with
        `finished_at` on every row.
        """
        start_ns = time.time_ns()
        result = self.runner.run(["pointer", "get", address])
        if result.ok:
            return OUTCOME_OK, result.json(), result, start_ns
        return classify_failure(result.stderr, write=False), {}, result, start_ns

    def classify_read(
        self, expected_counter: int | None, expected_target: str | None, record: dict
    ) -> str:
        """Compare a read-back record against what this client believes.

        A counter BELOW what we wrote is the stale read the ADR predicts. A
        counter ABOVE it is an anomaly and never routine: only a write by
        something else to a pointer this client owns could produce one.
        """
        if expected_counter is None:
            return OUTCOME_OK
        observed = record.get("counter")
        if not isinstance(observed, int):
            return OUTCOME_ERROR
        if observed < expected_counter:
            return OUTCOME_STALE
        if observed > expected_counter:
            return OUTCOME_ANOMALY
        if expected_target and record.get("target") != expected_target:
            # Same counter, different target: two states at one counter converge
            # on the smaller target, so the one we wrote lost the merge. It is a
            # write that did not stick, which is what `stale` means here.
            return OUTCOME_STALE
        return OUTCOME_OK

    # -- the three loops ---------------------------------------------------

    def do_create(self) -> None:
        if self.pointer_max and len(self.ledger.own) >= self.pointer_max:
            return
        self.seq += 1
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        key_path = self.keys_dir / f"owner-{int(time.time())}-{self.seq}.key"
        keygen = self.runner.run(
            ["pointer", "keygen", "--output", str(key_path)],
            bootstrap=False,
            evm=False,
        )
        if not keygen.ok:
            self.emit(
                OP_CREATE,
                {
                    "outcome": OUTCOME_ERROR,
                    "ok": False,
                    "owned": True,
                    "duration_ms": keygen.duration_ms,
                    "start_ns": time.time_ns(),
                    "attempts": 1,
                    "paid_attempts": 0,
                    "error": error_summary(keygen.stderr),
                },
            )
            print(f"keygen failed: {error_summary(keygen.stderr)}", file=sys.stderr)
            return
        address = keygen.json().get("address", "")
        if not address:
            print("keygen produced no address", file=sys.stderr)
            return
        self.ledger.note_key(address, str(key_path))

        target = self.pool.random_chunk_target()
        start_ns = time.time_ns()
        ant_before, gas_before = self.wallet.ant_atto(), self.wallet.gas_wei()
        result = self.runner.run(
            ["pointer", "create", "--key", str(key_path), "--kind", "chunk", target]
        )
        ant_after, gas_after = self.wallet.ant_atto(), self.wallet.gas_wei()
        spend = self._spend_fields(ant_before, ant_after, gas_before, gas_after)
        paid = spend.get("wallet_spend_atto", 0) > 0

        if result.ok:
            outcome = OUTCOME_OK
            self.ledger.record_own(address, str(key_path), 0, target, "chunk")
            self.flush_addresses()
        elif ALREADY_EXISTS_MARKER in result.stderr:
            # A create that already exists means a previous run paid for this
            # key and we lost the ledger entry, not that anything is wrong.
            outcome = OUTCOME_ANOMALY
        else:
            outcome = classify_failure(result.stderr, write=True)

        read_back = ""
        if result.ok:
            read_outcome, record, read_result, read_start_ns = self.read_pointer(address)
            read_back = (
                read_outcome
                if read_outcome != OUTCOME_OK
                else self.classify_read(0, target, record)
            )
            self.emit(
                OP_GET,
                {
                    "outcome": read_back,
                    "ok": read_back == OUTCOME_OK,
                    "owned": True,
                    "address": address,
                    "counter_expected": 0,
                    "counter_observed": record.get("counter"),
                    "target_kind": "chunk",
                    "age_secs": 0,
                    "duration_ms": read_result.duration_ms,
                    "start_ns": read_start_ns,
                    "attempts": 1,
                    "paid_attempts": 0,
                    "error": "" if read_back == OUTCOME_OK else error_summary(read_result.stderr),
                },
            )

        self.emit(
            OP_CREATE,
            {
                "outcome": outcome,
                "ok": outcome == OUTCOME_OK,
                "owned": True,
                "address": address,
                "counter_expected": 0,
                "target_kind": "chunk",
                "duration_ms": result.duration_ms,
                "start_ns": start_ns,
                "attempts": 1,
                # One invocation, so one payment at most. A double payment can
                # only come from a retry, which this service deliberately never
                # does: the ADR is explicit that there is no recovery journal and
                # a retried write pays a second time.
                "paid_attempts": 1 if paid else 0,
                "error": "" if outcome == OUTCOME_OK else error_summary(result.stderr),
                "read_back": read_back,
                **spend,
            },
        )
        self.log_op(OP_CREATE, address, outcome, result)

    def do_update(self) -> None:
        candidates = self.ledger.established_own()
        if not candidates:
            return
        address = random.choice(candidates)
        entry = self.ledger.own[address]
        expected = entry["counter"] + 1

        chained = (
            len(candidates) > 1
            and self.chain_fraction > 0
            and random.random() < self.chain_fraction
        )
        if chained:
            others = [a for a in candidates if a != address]
            target, kind = random.choice(others), "pointer"
        else:
            target, kind = self.pool.random_chunk_target(), "chunk"

        start_ns = time.time_ns()
        ant_before, gas_before = self.wallet.ant_atto(), self.wallet.gas_wei()
        result = self.runner.run(
            ["pointer", "update", "--key", entry["key"], "--kind", kind, target]
        )
        ant_after, gas_after = self.wallet.ant_atto(), self.wallet.gas_wei()
        spend = self._spend_fields(ant_before, ant_after, gas_before, gas_after)
        paid = spend.get("wallet_spend_atto", 0) > 0

        outcome = OUTCOME_OK if result.ok else classify_failure(result.stderr, write=True)
        read_back = ""
        if result.ok:
            self.ledger.record_own(address, entry["key"], expected, target, kind)
            read_outcome, record, read_result, read_start_ns = self.read_pointer(address)
            read_back = (
                read_outcome
                if read_outcome != OUTCOME_OK
                else self.classify_read(expected, target, record)
            )
            age = int(time.time() - entry.get("created_at", time.time()))
            self.emit(
                OP_GET,
                {
                    "outcome": read_back,
                    "ok": read_back == OUTCOME_OK,
                    "owned": True,
                    "address": address,
                    "counter_expected": expected,
                    "counter_observed": record.get("counter"),
                    "target_kind": kind,
                    "age_secs": age,
                    "duration_ms": read_result.duration_ms,
                    "start_ns": read_start_ns,
                    "attempts": 1,
                    "paid_attempts": 0,
                    "error": "" if read_back == OUTCOME_OK else error_summary(read_result.stderr),
                },
            )
            if chained:
                self.do_resolve(address)

        self.emit(
            OP_UPDATE,
            {
                "outcome": outcome,
                "ok": outcome == OUTCOME_OK,
                "owned": True,
                "address": address,
                "counter_expected": expected,
                "target_kind": kind,
                "duration_ms": result.duration_ms,
                "start_ns": start_ns,
                "attempts": 1,
                "paid_attempts": 1 if paid else 0,
                "error": "" if outcome == OUTCOME_OK else error_summary(result.stderr),
                "read_back": read_back,
                **spend,
            },
        )
        self.log_op(OP_UPDATE, address, outcome, result)

    def do_resolve(self, address: str) -> None:
        """Follow a chain to the chunk at its end.

        Only run after a chained update, because it is the only time this client
        knows a chain exists. Resolution -- the depth cap and the cycle check --
        is entirely client-side; a node never reads a target.
        """
        start_ns = time.time_ns()
        result = self.runner.run(["pointer", "resolve", address])
        outcome = OUTCOME_OK if result.ok else classify_failure(result.stderr, write=False)
        record = result.json() if result.ok else {}
        self.emit(
            OP_RESOLVE,
            {
                "outcome": outcome,
                "ok": outcome == OUTCOME_OK,
                "owned": True,
                "address": address,
                "target_kind": record.get("kind", ""),
                "duration_ms": result.duration_ms,
                "start_ns": start_ns,
                "attempts": 1,
                "paid_attempts": 0,
                "error": "" if outcome == OUTCOME_OK else error_summary(result.stderr),
            },
        )
        self.log_op(OP_RESOLVE, address, outcome, result)

    def do_sweep(self) -> None:
        """Read every pointer this client knows, own and foreign.

        Own pointers are checked against the ledger's expected state; foreign
        ones only against their last-seen counter, since a counter going
        backwards is the only thing an outside observer can call wrong. Both
        record the pointer's AGE at read, which is what makes decay with age a
        table rather than a judgement.
        """
        self.pool.refresh(self.own_key_name)
        discovered = 0
        for address in self.pool.foreign_pointers:
            if self.ledger.add_foreign(address):
                discovered += 1
        if discovered:
            self.ledger.save()
            print(f"sweep: discovered {discovered} foreign pointer(s)", flush=True)

        now = time.time()
        for address in list(self.ledger.established_own()):
            entry = self.ledger.own[address]
            self._sweep_one(
                address,
                owned=True,
                expected_counter=entry["counter"],
                expected_target=entry.get("target"),
                age_secs=int(now - entry.get("created_at", now)),
            )
        for address in list(self.ledger.foreign):
            entry = self.ledger.foreign[address]
            self._sweep_one(
                address,
                owned=False,
                expected_counter=entry.get("counter"),
                expected_target=None,
                age_secs=int(now - entry.get("first_seen", now)),
            )

    def _sweep_one(
        self,
        address: str,
        *,
        owned: bool,
        expected_counter: int | None,
        expected_target: str | None,
        age_secs: int,
    ) -> None:
        outcome, record, result, start_ns = self.read_pointer(address)
        if outcome == OUTCOME_OK:
            if owned:
                outcome = self.classify_read(expected_counter, expected_target, record)
            else:
                observed = record.get("counter")
                if not isinstance(observed, int):
                    outcome = OUTCOME_ERROR
                elif expected_counter is not None and observed < expected_counter:
                    # A foreign counter can legitimately jump forward -- its
                    # owner is writing -- so only a move BACKWARDS is stale.
                    outcome = OUTCOME_STALE
                else:
                    self.ledger.note_foreign(address, observed)
        self.emit(
            OP_SWEEP,
            {
                "outcome": outcome,
                "ok": outcome == OUTCOME_OK,
                "owned": owned,
                "address": address,
                "counter_expected": expected_counter,
                "counter_observed": record.get("counter"),
                "target_kind": record.get("kind", ""),
                "age_secs": age_secs,
                "duration_ms": result.duration_ms,
                "start_ns": start_ns,
                "attempts": 1,
                "paid_attempts": 0,
                "error": "" if outcome == OUTCOME_OK else error_summary(result.stderr),
            },
        )

    # -- helpers -----------------------------------------------------------

    def flush_addresses(self) -> None:
        published = publish(self.ledger.established_own(), self.addresses_file, self.presigned_url)
        if published:
            print(
                f"published {len(self.ledger.established_own())} pointer "
                f"address(es) as {self.own_key_name}",
                flush=True,
            )

    def log_op(self, op: str, address: str, outcome: str, result: AntResult) -> None:
        stamp = time.strftime("%H:%M:%S", time.gmtime())
        short = address[:12] if address else "-"
        print(
            f"[{stamp}] {op} {short} {outcome} ({result.duration_ms}ms)",
            flush=True,
        )
        if outcome != OUTCOME_OK and result.stderr:
            tail = result.stderr.strip().splitlines()[-STDERR_TAIL_LINES:]
            print("\n".join(tail), file=sys.stderr, flush=True)

    def run(self) -> None:
        print(
            f"Pointer client starting: host={self.host} region={self.region} "
            f"index={self.client_index} create/{self.create_interval}s "
            f"update/{self.update_interval}s sweep/{self.sweep_interval}s "
            f"chain={self.chain_fraction} known="
            f"{len(self.ledger.own)} own + {len(self.ledger.foreign)} foreign",
            flush=True,
        )
        self.pool.refresh(self.own_key_name)
        # Publish whatever a previous incarnation owned before doing anything
        # else, so a restarted client's peers can sweep its pointers again
        # immediately rather than after the first create.
        if self.ledger.established_own():
            self.flush_addresses()

        now = time.monotonic()
        due = {
            OP_CREATE: now,
            OP_UPDATE: now + self.update_interval,
            OP_SWEEP: now + self.sweep_interval,
        }
        handlers = {
            OP_CREATE: self.do_create,
            OP_UPDATE: self.do_update,
            OP_SWEEP: self.do_sweep,
        }
        intervals = {
            OP_CREATE: self.create_interval,
            OP_UPDATE: self.update_interval,
            OP_SWEEP: self.sweep_interval,
        }
        while True:
            which = min(due, key=due.get)
            wait = due[which] - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                handlers[which]()
            except Exception as exc:  # noqa: BLE001 -- a loop must not die
                # `Restart=always` would bring the service back, but it would
                # also restart every loop's schedule and lose the run's cadence.
                # One failed op is not a reason to stop measuring.
                print(f"ERROR: {which} failed: {exc}", file=sys.stderr, flush=True)
            # From NOW, not from the previous due time: an op that overran its
            # interval must not then fire back-to-back trying to catch up, which
            # on a paid operation means spending faster than configured.
            due[which] = time.monotonic() + intervals[which]


def main() -> int:
    if _env_bool("FORWARD_LOGS"):
        install_log_forwarding()
    PointerClient().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
