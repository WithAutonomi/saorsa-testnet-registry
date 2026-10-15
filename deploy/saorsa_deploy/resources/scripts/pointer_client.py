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

Four loops, one thread
----------------------

Create, update, sweep and -- when `TRANSFER_INTERVAL_SECS` is positive -- the
handover loop run on one scheduler in one process, strictly serially. They share
a single wallet, and every write is a paid on-chain transaction, so two
concurrent writes would race the same nonce -- the failure mode that cost staging
run #566 six uploaders. Whichever loop is due soonest runs next; a loop that
overruns simply delays itself, it is never run twice concurrently.

The handover loop (V2-1354)
---------------------------

One cycle hands one owned pointer over for good: create a recipient, `transfer`
to it, then check the result from near (`finality`, `get`, `controller`,
`resolve`) and prove the two take-backs -- an `update` and a second `transfer` on
the handed-over key -- are refused BEFORE they pay. That last part is why the
take-back outcome depends on the wallet: a refusal that still cost money is not a
refusal, and only a balance delta either side can tell. A take-back whose balance
could not be read is `error`, never `ok`: an unread balance is unknown, not zero.

It is OFF by default (interval 0), because `ant pointer transfer` does not exist
on a build predating V2-1354 and the loop would then record an error a cycle that
says nothing about the release.

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
#: The handover ops (V2-1354). `transfer` is the only paid one of the four.
OP_TRANSFER = "transfer"
OP_FINALITY = "finality"
OP_CONTROLLER = "controller"
OP_TAKEBACK = "takeback"

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
#: Finality statuses that are neither "held by the whole close group" nor an
#: error, carried as outcomes of their own so a handover that is still settling
#: is never reported as one that failed. `unconfirmed` is final on a majority
#: with a peer that did not answer; `forked` is two rival final states.
OUTCOME_SETTLING = "settling"
OUTCOME_UNCONFIRMED = "unconfirmed"
OUTCOME_FORKED = "forked"

#: The counter a handover's final state carries: `u64::MAX`. A read of a
#: handed-over address returns exactly this, with a `pointer`-kind target that is
#: the recipient, which is what the redirect read is checked against.
FINAL_COUNTER = 18446744073709551615

#: `ant pointer finality` status -> outcome. `open` is `stale`: the handover is
#: simply not held, which is the same claim a stale read makes. An unknown status
#: is an error rather than a pass -- a future ant-client adding a status must not
#: read as a clean handover.
FINALITY_OUTCOMES = {
    "final": OUTCOME_OK,
    "settling": OUTCOME_SETTLING,
    "unconfirmed": OUTCOME_UNCONFIRMED,
    "forked": OUTCOME_FORKED,
    "open": OUTCOME_STALE,
}

#: Largest value an Influx integer field (and a Postgres BIGINT) can hold. A
#: handover's counter is `u64::MAX`, which is twice that, so it cannot travel as
#: one -- see :func:`counter_fields`.
MAX_I64 = (1 << 63) - 1

#: At most this many FOREIGN handed-over pointers get a `finality` check per
#: sweep. Finality asks the whole close group and never stops early, so it is the
#: most expensive read here, and the handover population only grows. A module
#: constant and deliberately not a setting: it bounds a cost, it does not express
#: an intent an operator would want to vary.
FOREIGN_FINALITY_SAMPLE = 20

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
#: `Error::PointerFinal`, whose Display is `pointer is final: {0}`. Both a second
#: `transfer` and an `update` on a handed-over pointer fail with it, and fail
#: BEFORE paying -- which is the whole point of the take-back checks. It also
#: carries the single-rival-state refusal (`rival_final`: "pointer {a} is already
#: final on state {s} held by {n} of {g} peers").
POINTER_FINAL_MARKER = "pointer is final"
#: `Error::PointerForked`, whose Display is `pointer is forked: {0}` -- raised by
#: `rival_final` and `final_already` when the close group holds MORE THAN ONE
#: final state. The body is "pointer {a} holds {n} different final states", so
#: neither "final state" singular nor any phrase about a rival appears in it:
#: matching on the error's own prefix is the only stable option. A take-back on a
#: forked pointer is refused with this and NOT with `pointer is final`, so both
#: markers are a refusal wherever one is.
POINTER_FORKED_MARKER = "pointer is forked"
#: The refusals `pointer_sign_transfer` makes before paying when the close group
#: could not be fully canvassed. THREE emits share this wording -- the transfer's
#: own silent-peer refusal ("only N of the G peers of pointer A's close group
#: answered, and a silent one could hold a final state; a transfer cannot be taken
#: back, so try again when all of them can be asked"), the finality check's own
#: under-quorum shortfall, and the recipient `get`'s. All three happen inside the
#: transfer BEFORE any payment, so `read_shortfall` is the right class for each
#: and there is no need to tell them apart. The address is interpolated in the
#: middle, so the usable part is the tail.
TRANSFER_SHORTFALL_MARKER = "close group answered"
#: The two refusals that mean "this pointer can never take another write".
#: `final_already` answers an update with the first on an unforked pointer and
#: the second on a forked one, so a check that accepted only one would read a
#: forked pointer's refusal as an error.
NO_MORE_WRITES_MARKERS = (POINTER_FINAL_MARKER, POINTER_FORKED_MARKER)
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
#: configuration covers every role. Two things are appended to the two files
#: there: this script's own prints (the per-op summary lines, via
#: `_ForwardingStream`) AND every network `ant` invocation's full stdout and
#: stderr (`AntRunner.run`, the same way `uploader.sh` appends its per-op
#: captures). Without the second half a clean run forwards a few KB of summary
#: lines and `ant`'s own tracing -- the client's view of a refused take-back, a
#: silent peer, a forked read -- reaches Elasticsearch not at all. Balance reads
#: and key generation are NOT appended: no peers are involved and `uploader.sh`
#: discards its balance read's stderr too.
FORWARD_LOG_DIR = "/var/log/ant-client-forward"
#: Coarse disk guard for the append-only forward logs, as in `uploader.sh`:
#: truncate past this rather than fill the VM.
FORWARD_LOG_MAX_BYTES = 1 << 30


def _append_forward(path: str, text: str) -> None:
    """Append to a telegraf-tailed forward log, truncating past the cap.

    The one place the cap is applied, so the stream tee and the per-op captures
    cannot drift. A failure to append is swallowed: log forwarding must never
    take the workload down with it.
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
        _append_forward(self._path, text)
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
        #: Own pointers handed over for good, `{old: {recipient, transferred_at}}`.
        #: Durable for the same reason the keys are: a handover is irreversible,
        #: so a restart that forgot one would go on trying to update a pointer
        #: that can never accept another write, and would record every refusal
        #: as a lost write.
        self.transferred: dict[str, dict] = {}
        #: Handovers published by the OTHER pointer clients,
        #: `{old: {recipient, first_seen}}`. A foreign client reading the plain
        #: address list sees the old address with no expected state; this is what
        #: says the state to expect there is the final one, aimed at `recipient`.
        self.foreign_transfers: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.own = dict(data.get("own") or {})
        self.foreign = dict(data.get("foreign") or {})
        self.transferred = dict(data.get("transferred") or {})
        self.foreign_transfers = dict(data.get("foreign_transfers") or {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "own": self.own,
                    "foreign": self.foreign,
                    "transferred": self.transferred,
                    "foreign_transfers": self.foreign_transfers,
                }
            )
        )
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

    def mark_transferred(self, address: str, recipient: str, state_id: str = "") -> None:
        """Record a completed handover, and move the expected state onto it.

        The expected counter becomes `u64::MAX` and the expected target the
        recipient, so every subsequent read of this address -- the redirect read,
        every sweep, and every other client's sweep -- is checked against the
        final state rather than against the state it last held.

        `state_id` is the final state the transfer signed, as the CLI reports it.
        Kept because it is what makes "holders of OUR state" exact on every later
        finality check, including a forked one, where several states are reported
        and only one of them is ours.
        """
        self.transferred[address] = {
            "recipient": recipient,
            "transferred_at": time.time(),
            "state_id": state_id,
        }
        entry = self.own.get(address) or {}
        entry["counter"] = FINAL_COUNTER
        entry["target"] = recipient
        entry["target_kind"] = "pointer"
        self.own[address] = entry
        self.save()

    def add_foreign_transfer(self, address: str, recipient: str) -> bool:
        """Register a handover published by another client. True when it is new."""
        if address in self.transferred:
            # Our own handover, read back off the shared prefix.
            return False
        existing = self.foreign_transfers.get(address)
        if existing is not None and existing.get("recipient") == recipient:
            return False
        self.foreign_transfers[address] = {
            "recipient": recipient,
            "first_seen": (existing or {}).get("first_seen", time.time()),
        }
        return True

    def established_own(self) -> list[str]:
        """Own pointers that have actually been created (a counter is known)."""
        return [a for a, e in self.own.items() if e.get("counter") is not None]

    def updatable_own(self) -> list[str]:
        """Own pointers that can still take a write.

        A handed-over pointer cannot: `update` on it is refused before paying.
        Keeping it in the update candidate list would spend the cycle on a
        guaranteed refusal and record it as a failed write. It is excluded from
        the chain targets for the same reason -- a chain hop through a final
        pointer is a hop that can never be re-aimed.
        """
        return [a for a in self.established_own() if a not in self.transferred]


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


def classify_failure(stderr: str, *, write: bool, handover: bool = False) -> str:
    """Map `ant`'s stderr to an outcome class.

    The ordering matters. A lost write and a close-group shortfall are both
    reported as a failed command with exit 1, and both are ordinary events on a
    healthy network -- lumping either into `error` would make the error rate a
    measure of contention rather than of faults.

    ``handover`` routes the three handover refusals, and ONLY for the handover
    ops. They are refusals before payment, so on a `transfer` they are routine
    events, while on an ordinary create or update the same words would mean
    something else -- so the plain ops' classification is left exactly as it was.
    """
    if handover:
        if any(marker in stderr for marker in NO_MORE_WRITES_MARKERS):
            return OUTCOME_ANOMALY
        if TRANSFER_SHORTFALL_MARKER in stderr:
            return OUTCOME_READ_SHORTFALL
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


def counter_fields(expected, observed) -> dict:
    """The two counter fields, routed by whether the value fits an i64.

    Influx integer fields are SIGNED 64-bit, and so is a Postgres ``BIGINT``, but
    a handover's counter is `u64::MAX` -- about twice what either holds. So a
    counter that does not fit travels as a STRING field under its own name.

    Two decisions, both load-bearing. A string rather than a float, because the
    exact value is what `classify_read` compares and 1.8446744073709552e19 is not
    it; and a NEW NAME rather than a string in `counter_expected`, because a
    field that is an integer on some points and a string on others is a TYPE
    CONFLICT and Influx drops the whole point. The atto wallet balances in this
    same measurement already travel as strings for exactly the first reason.
    """
    fields: dict = {}
    for key, value in (("counter_expected", expected), ("counter_observed", observed)):
        if value is None or isinstance(value, bool) or not isinstance(value, int):
            continue
        if -1 <= value <= MAX_I64:
            fields[key] = value
        else:
            fields[f"{key}_u64"] = str(value)
    return fields


def _as_int(value) -> int | None:
    """An int field from `ant`'s JSON, or None when it is not one.

    None rather than 0: Influx has no null, and a field that is dropped is
    "not reported", while a 0 would claim the close group has no members.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _finality_status(status) -> str | None:
    """The `status` out of a finality STATUS object (`finality_json`'s inner one).

    Accepting a bare string too costs nothing and means a shape change does not
    silently drop the field. Mind which object this is handed: see
    :func:`finality_object`.
    """
    if isinstance(status, dict):
        value = status.get("status")
        return value if isinstance(value, str) and value else None
    if isinstance(status, str) and status:
        return status
    return None


def finality_object(record: dict, *, nested: bool) -> dict:
    """The `finality_json` object out of a result, or {}.

    The two commands wrap it differently, and getting this wrong is silent.
    `ant pointer finality` prints `finality_json` AS its whole document, so the
    object IS the record: `{address, group, answered, finality: {status, ...}}`.
    `ant pointer transfer` prints `{address, state, finality: <finality_json>,
    finality_error}` -- so there the status sits at `finality.finality.status`,
    and reading `finality.status` on a transfer returned None for every real
    reply. `finality` is null on a transfer whose check failed, with the reason
    in `finality_error`.
    """
    if not nested:
        return record if isinstance(record, dict) else {}
    finality = record.get("finality") if isinstance(record, dict) else None
    return finality if isinstance(finality, dict) else {}


def finality_holders(finality: dict, state_id: str | None = None) -> int | None:
    """Peers holding OUR final state, read out of a `finality_json` object.

    `holders` is NOT a top-level field. `final_state_json` renders it inside the
    state object, so it is at `finality.state.holders` for `settling` / `final` /
    `unconfirmed`, and one per entry of `finality.states` -- plus
    `finality.majority` -- for a fork. Reading it off the top level returned None
    for every real reply, which left the column dead.

    With our own final state's id (recorded at handover time) the answer is
    exact, fork or not. Without one -- every FOREIGN handover, whose state id
    this client never saw -- it falls back to the single state, or to the
    majority side of a fork. A fork with no majority has no state to count, and
    that is what such a fork means.
    """
    status = finality.get("finality")
    if not isinstance(status, dict):
        return None
    candidates = []
    state = status.get("state")
    if isinstance(state, dict):
        candidates.append(state)
    states = status.get("states")
    if isinstance(states, list):
        candidates.extend(s for s in states if isinstance(s, dict))
    if state_id:
        for candidate in candidates:
            if candidate.get("state_id") == state_id:
                return _as_int(candidate.get("holders"))
        return None
    if isinstance(state, dict):
        return _as_int(state.get("holders"))
    majority = status.get("majority")
    if isinstance(majority, dict):
        return _as_int(majority.get("holders"))
    return None


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
        *,
        forward_dir: str | None = None,
    ):
        self.ant_path = ant_path
        self.bootstrap_args = bootstrap_args
        self.evm_network = evm_network
        self.rust_log = rust_log
        self.timeout_secs = timeout_secs
        #: Set only when the client has `forward_logs`. Every network
        #: invocation's capture is appended there; see `FORWARD_LOG_DIR`.
        self.forward_dir = forward_dir

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
        if bootstrap:
            self._forward(out, err)
        return AntResult(rc, out, err, duration_ms)

    def _forward(self, out: str, err: str) -> None:
        """Append a network invocation's captures to the forward logs.

        Whatever the exit code: a successful op's trace is the point -- on a
        clean run the only thing `log_op` forwards is its one summary line, so
        conditioning on the outcome is how `ant`'s output came to reach
        Elasticsearch not at all. `capture_output=True` means nothing is written
        to the service's own streams, so this is the only route.
        """
        if not self.forward_dir:
            return
        for name, text in (("pointer.stdout.log", out), ("pointer.stderr.log", err)):
            if not text:
                continue
            # The forwarder's tail is line-based: the last line of one op must
            # not be glued to the first line of the next.
            if not text.endswith("\n"):
                text += "\n"
            _append_forward(os.path.join(self.forward_dir, name), text)


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


def parse_transfer_lines(text: str) -> list[tuple[str, str]]:
    """Handover pairs from a published transfers list. Lines are `<old> <new>`.

    NOT :func:`parse_address_lines`, which takes the first field of each line and
    would read the old address as a plain pointer and throw the recipient away --
    leaving the reader with an address whose expected state it does not know.
    Both fields must be addresses; a one-column line is skipped rather than
    half-read.
    """
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        old, recipient = fields[0], fields[1]
        if _HEX_ADDRESS.match(old) and _HEX_ADDRESS.match(recipient):
            out.append((old, recipient))
    return out


def is_pointer_transfers_key(key: str) -> bool:
    """Whether a published object holds handover PAIRS rather than addresses.

    Tested before :func:`is_pointer_key`, which also matches it (the name starts
    `pointer-`): read as an address list, each line's first field would be taken
    for an ordinary pointer and the recipient discarded.
    """
    return key.rsplit("/", 1)[-1].startswith("pointer-transfers-")


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
        #: `(old, recipient)` pairs published by the other pointer clients.
        self.foreign_transfers: list[tuple[str, str]] = []

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

    def refresh(self, own_key: str, own_transfers_key: str = "") -> None:
        """Re-read the prefix, skipping this client's own two objects."""
        if not self.enabled:
            return
        now = time.time()
        if now - self._last_refresh < self.refresh_secs:
            return
        self._last_refresh = now
        chunks: list[str] = []
        pointers: list[str] = []
        transfers: list[tuple[str, str]] = []
        try:
            keys = self._list_keys()
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            print(f"WARN: address listing failed: {exc}", file=sys.stderr, flush=True)
            return
        for key in keys:
            name = key.rsplit("/", 1)[-1]
            if name == own_key or (own_transfers_key and name == own_transfers_key):
                continue
            try:
                body = self._fetch(f"{self._base_url()}/{urllib.parse.quote(key)}")
            except (urllib.error.URLError, socket.timeout, OSError):
                continue
            # Transfers first: the key also matches `is_pointer_key`, and read as
            # an address list its recipient column would be thrown away.
            if is_pointer_transfers_key(key):
                transfers.extend(parse_transfer_lines(body))
            elif is_pointer_key(key):
                pointers.extend(parse_address_lines(body))
            else:
                chunks.extend(parse_address_lines(body))
        if chunks:
            self.chunk_targets = chunks
        self.foreign_pointers = pointers
        self.foreign_transfers = transfers

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


def publish(addresses: list[str], path: Path, presigned_url: str, label: str = "address") -> bool:
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
        print(f"WARN: {label} flush failed: {exc}", file=sys.stderr, flush=True)
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
            forward_dir=FORWARD_LOG_DIR if _env_bool("FORWARD_LOGS") else None,
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
        # A second object, for the handovers. Separate from the address list
        # because its lines are two columns: a reader of the address list takes
        # the first field, so a pair written there would publish the old address
        # with its recipient silently dropped.
        self.own_transfers_key_name = f"pointer-transfers-{self.region}-{self.client_index}.txt"
        self.transfers_file = self.data_dir / self.own_transfers_key_name
        self.transfers_presigned_url = _env("TRANSFERS_PRESIGNED_URL")

        self.create_interval = _env_int("CREATE_INTERVAL_SECS", 60)
        self.update_interval = _env_int("UPDATE_INTERVAL_SECS", 30)
        self.sweep_interval = _env_int("SWEEP_INTERVAL_SECS", 600)
        self.chain_fraction = _env_float("CHAIN_FRACTION", 0.10)
        # 0 = no handover loop. See the module docstring.
        self.transfer_interval = _env_int("TRANSFER_INTERVAL_SECS", 0)
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

    def do_create(self) -> str | None:
        """Create one new pointer. Returns its address, or None on any failure.

        The return value exists for the handover loop, which needs a recipient
        and must create it exactly as the create loop does -- same wallet reads,
        same `op=create` point, same read-back, same entry in the owned pool. A
        recipient created by a different code path would be a pointer measured
        differently from every other.
        """
        if self.pointer_max and len(self.ledger.own) >= self.pointer_max:
            return None
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
            return None
        address = keygen.json().get("address", "")
        if not address:
            print("keygen produced no address", file=sys.stderr)
            return None
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
                    **counter_fields(0, record.get("counter")),
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
        return address if result.ok else None

    def do_update(self) -> None:
        # Handed-over pointers excluded: an `update` on one is refused before it
        # pays, so including them would spend the cycle on a certain refusal and
        # record it as a failed write.
        candidates = self.ledger.updatable_own()
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
                    **counter_fields(expected, record.get("counter")),
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

    def do_resolve(self, address: str, expected_target: str | None = None) -> None:
        """Follow a chain to the chunk at its end.

        Run after a chained update -- the only time this client knows a chain
        exists -- and after a handover, where the chain is the redirection the
        final state created. Resolution (the depth cap, the cycle check) is
        entirely client-side; a node never reads a target.

        ``expected_target`` is only asserted when the CLI actually names the
        resolved target: its absence is unknown, not a mismatch.
        """
        start_ns = time.time_ns()
        result = self.runner.run(["pointer", "resolve", address])
        outcome = OUTCOME_OK if result.ok else classify_failure(result.stderr, write=False)
        record = result.json() if result.ok else {}
        if outcome == OUTCOME_OK and expected_target:
            resolved = record.get("target")
            if isinstance(resolved, str) and resolved and resolved != expected_target:
                outcome = OUTCOME_STALE
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

    # -- the handover loop (V2-1354) ---------------------------------------

    def do_transfer(self) -> None:
        """Hand one owned pointer over for good, then check the result.

        Nine steps, each its own Influx point so a failure is attributable to the
        step it happened in rather than to "the handover". Only a completed
        transfer continues the cycle: there is nothing to check about a handover
        that did not happen, and checking anyway would record a handful of
        `not_found`s that say the transfer failed a second time.
        """
        candidates = self.ledger.updatable_own()
        if not candidates:
            # Not an error, and nothing is emitted: the create loop has simply
            # not produced an untransferred pointer yet. A point here would be a
            # handover attempt that never attempted anything.
            print("transfer: no untransferred owned pointer available, skipping", flush=True)
            return
        old = random.choice(candidates)
        old_key = self.ledger.own[old]["key"]

        # The recipient is an ordinary created pointer -- same op, same point,
        # same owned pool -- because a pointer created specially would not be
        # comparable with any other pointer in the run.
        recipient = self.do_create()
        if recipient is None:
            # Nothing is emitted -- the handover was never attempted -- but it
            # has to be said, or a run whose `pointer_max` cap is full reads as
            # one where handovers simply never happened.
            print(
                "transfer: no recipient could be created, skipping the cycle "
                f"(pointer_max={self.pointer_max}, own={len(self.ledger.own)})",
                flush=True,
            )
            return

        start_ns = time.time_ns()
        ant_before, gas_before = self.wallet.ant_atto(), self.wallet.gas_wei()
        result = self.runner.run(["pointer", "transfer", "--key", old_key, recipient])
        ant_after, gas_after = self.wallet.ant_atto(), self.wallet.gas_wei()
        spend = self._spend_fields(ant_before, ant_after, gas_before, gas_after)
        paid = spend.get("wallet_spend_atto", 0) > 0

        if result.ok:
            outcome = OUTCOME_OK
        elif paid:
            # Classified by the PAYMENT first, not by the markers. Every refusal
            # the markers name happens inside `pointer_sign_transfer`, before a
            # quote is bought -- so a transfer that spent and did not land is a
            # lost write whatever its stderr says, and reading a marker first
            # would file a paid failure as a refusal that never cost anything.
            outcome = OUTCOME_LOST_WRITE
        else:
            outcome = classify_failure(result.stderr, write=True, handover=True)
        record = result.json() if result.ok else {}
        # `print_transfer` nests `finality_json` under its own `finality` key, so
        # the status is one level deeper here than on the finality command.
        finality = finality_object(record, nested=True)
        state_id = record.get("state") if isinstance(record.get("state"), str) else ""
        self.emit(
            OP_TRANSFER,
            {
                "outcome": outcome,
                "ok": outcome == OUTCOME_OK,
                "owned": True,
                "address": old,
                "recipient": recipient,
                **counter_fields(FINAL_COUNTER, None),
                "target_kind": "pointer",
                "finality_status": _finality_status(finality.get("finality")),
                "duration_ms": result.duration_ms,
                "start_ns": start_ns,
                "attempts": 1,
                "paid_attempts": 1 if paid else 0,
                "error": "" if outcome == OUTCOME_OK else error_summary(result.stderr),
                **spend,
            },
        )
        self.log_op(OP_TRANSFER, old, outcome, result)
        if outcome != OUTCOME_OK:
            return

        self.ledger.mark_transferred(old, recipient, state_id or "")
        self.flush_transfers()
        transferred_at = self.ledger.transferred[old]["transferred_at"]

        self.check_finality(
            old,
            owned=True,
            age_secs=int(time.time() - transferred_at),
            state_id=state_id or None,
        )
        self.check_redirect(old, recipient, owned=True, age_secs=0)
        self.check_controller(old, recipient)
        # The recipient's own target is where the chain now ends.
        self.do_resolve(old, expected_target=(self.ledger.own.get(recipient) or {}).get("target"))
        # The two take-backs. Both must be refused BEFORE paying -- that is the
        # property, and it is the wallet that decides whether it held.
        target = self.pool.random_chunk_target()
        self.check_takeback(
            old,
            ["pointer", "update", "--key", old_key, "--kind", "chunk", target],
            attempt="update",
        )
        self.check_takeback(
            old,
            ["pointer", "transfer", "--key", old_key, recipient],
            attempt="transfer",
        )

    def check_finality(
        self, address: str, *, owned: bool, age_secs: int, state_id: str | None = None
    ) -> None:
        """`ant pointer finality` -- does the WHOLE close group hold the handover?

        Free, and it never stops early: that is the difference from a plain read,
        which is answered by a quorum. `unconfirmed` is the one worth staring at
        -- final on a majority with a peer that did not answer -- because it is
        the state a silent peer leaves behind, and it is exactly what `transfer`
        refuses to write into.
        """
        start_ns = time.time_ns()
        result = self.runner.run(["pointer", "finality", address])
        record = result.json() if result.ok else {}
        # The finality command prints `finality_json` as its whole document, so
        # the object is the record itself -- unlike a transfer's, which nests it.
        finality = finality_object(record, nested=False)
        status = _finality_status(finality.get("finality"))
        if not result.ok:
            outcome = OUTCOME_ERROR
        else:
            outcome = FINALITY_OUTCOMES.get(status or "", OUTCOME_ERROR)
        self.emit(
            OP_FINALITY,
            {
                "outcome": outcome,
                "ok": outcome == OUTCOME_OK,
                "owned": owned,
                "address": address,
                "finality_status": status,
                "answered": _as_int(finality.get("answered")),
                "group": _as_int(finality.get("group")),
                "holders": finality_holders(finality, state_id),
                "age_secs": age_secs,
                "duration_ms": result.duration_ms,
                "start_ns": start_ns,
                "attempts": 1,
                "paid_attempts": 0,
                "error": "" if outcome == OUTCOME_OK else error_summary(result.stderr),
            },
        )
        self.log_op(OP_FINALITY, address, outcome, result)

    def check_redirect(self, address: str, recipient: str, *, owned: bool, age_secs: int) -> None:
        """Read a handed-over address: it must return the final state.

        Counter `u64::MAX`, target the recipient. Through the same
        `read_pointer`/`classify_read` pair every other read goes through, and
        compared on the same target representation -- the bare hex address, which
        is what the chained-update path has always compared a `pointer`-kind
        target against.
        """
        read_outcome, record, result, start_ns = self.read_pointer(address)
        outcome = (
            read_outcome
            if read_outcome != OUTCOME_OK
            else self.classify_read(FINAL_COUNTER, recipient, record)
        )
        self.emit(
            OP_GET,
            {
                "outcome": outcome,
                "ok": outcome == OUTCOME_OK,
                "owned": owned,
                "address": address,
                "recipient": recipient,
                **counter_fields(FINAL_COUNTER, record.get("counter")),
                "target_kind": "pointer",
                "age_secs": age_secs,
                "duration_ms": result.duration_ms,
                "start_ns": start_ns,
                "attempts": 1,
                "paid_attempts": 0,
                "error": "" if outcome == OUTCOME_OK else error_summary(result.stderr),
            },
        )

    def check_controller(self, address: str, recipient: str) -> None:
        """`ant pointer controller` -- who owns this pointer now?

        It follows the redirections, so the pointer at the end of the chain is
        the recipient. Any OTHER address is `stale`: the walk worked and landed
        somewhere else, which is a different claim from the walk failing.
        """
        start_ns = time.time_ns()
        result = self.runner.run(["pointer", "controller", address])
        record = result.json() if result.ok else {}
        controller = record.get("pointer")
        if result.ok and controller == recipient:
            outcome = OUTCOME_OK
        elif result.ok and isinstance(controller, str) and controller:
            outcome = OUTCOME_STALE
        else:
            outcome = OUTCOME_ERROR
        self.emit(
            OP_CONTROLLER,
            {
                "outcome": outcome,
                "ok": outcome == OUTCOME_OK,
                "owned": True,
                "address": address,
                "recipient": recipient,
                "target_kind": "pointer",
                "duration_ms": result.duration_ms,
                "start_ns": start_ns,
                "attempts": 1,
                "paid_attempts": 0,
                "error": "" if outcome == OUTCOME_OK else error_summary(result.stderr),
            },
        )
        self.log_op(OP_CONTROLLER, address, outcome, result)

    def check_takeback(self, address: str, args: list[str], *, attempt: str) -> None:
        """Try to write to a handed-over pointer. A refusal is the PASS.

        `ok` means refused -- with `pointer is final` OR `pointer is forked` on
        stderr -- and not one atto or one wei spent. Both markers are needed:
        `final_already` answers an update with the first on an unforked pointer
        and the second on a forked one, so accepting only `pointer is final`
        would read a forked pointer's refusal as an error. The spend is the other
        half of the assertion: a refusal after payment is not a refusal, and the
        ADR's whole claim about a handover is that it cannot be taken back. A
        balance that could not be READ gives `error` -- unknown is not zero, and
        reading it as zero would turn an unmeasured take-back into a pass.
        """
        start_ns = time.time_ns()
        ant_before, gas_before = self.wallet.ant_atto(), self.wallet.gas_wei()
        result = self.runner.run(args)
        ant_after, gas_after = self.wallet.ant_atto(), self.wallet.gas_wei()
        spend = self._spend_fields(ant_before, ant_after, gas_before, gas_after)
        ant_spend = spend.get("wallet_spend_atto")
        gas_spend = spend.get("gas_spend_wei")
        measured = ant_spend is not None and gas_spend is not None

        if result.ok or (ant_spend or 0) > 0 or (gas_spend or 0) > 0:
            # It was accepted, or it was refused but still cost money. Either way
            # the handover did not hold the way the ADR says it does.
            outcome = OUTCOME_ANOMALY
        elif measured and any(m in result.stderr for m in NO_MORE_WRITES_MARKERS):
            outcome = OUTCOME_OK
        else:
            outcome = OUTCOME_ERROR
        self.emit(
            OP_TAKEBACK,
            {
                "outcome": outcome,
                "ok": outcome == OUTCOME_OK,
                "owned": True,
                "address": address,
                "attempt": attempt,
                "duration_ms": result.duration_ms,
                "start_ns": start_ns,
                "attempts": 1,
                "paid_attempts": 1 if (ant_spend or 0) > 0 else 0,
                # Recorded even on `ok`, unlike every other op. Here `ok` means
                # the command FAILED as it was supposed to, and its stderr is
                # the only record of WHICH refusal it was -- `pointer is final`
                # or `pointer is forked`, which are different facts about the
                # pointer and are not recoverable from anywhere else.
                "error": error_summary(result.stderr),
                **spend,
            },
        )
        self.log_op(OP_TAKEBACK, address, outcome, result)

    def do_sweep(self) -> None:
        """Read every pointer this client knows, own and foreign.

        Own pointers are checked against the ledger's expected state; foreign
        ones only against their last-seen counter, since a counter going
        backwards is the only thing an outside observer can call wrong. Both
        record the pointer's AGE at read, which is what makes decay with age a
        table rather than a judgement.
        """
        self.pool.refresh(self.own_key_name, self.own_transfers_key_name)
        discovered = 0
        for address in self.pool.foreign_pointers:
            if self.ledger.add_foreign(address):
                discovered += 1
        handovers = 0
        for address, recipient in self.pool.foreign_transfers:
            if self.ledger.add_foreign_transfer(address, recipient):
                handovers += 1
        if discovered or handovers:
            self.ledger.save()
            print(
                f"sweep: discovered {discovered} foreign pointer(s) and "
                f"{handovers} foreign handover(s)",
                flush=True,
            )

        now = time.time()
        # Handed-over pointers are deliberately skipped here and read by
        # `_sweep_handovers` instead, which knows the recipient and runs the
        # finality check beside the read. Sweeping them here as well read every
        # one of them TWICE a sweep and inflated the `sweep` rows on exactly the
        # addresses whose state can no longer change.
        for address in list(self.ledger.updatable_own()):
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
        self._sweep_handovers(now)

    def _sweep_handovers(self, now: float) -> None:
        """Re-check every handover this client knows about, own and foreign.

        The question a handover raises is not answered at the moment it is made:
        a close group that was still settling may converge, or fork, hours later.
        So every own handover is re-checked in full on every sweep, and foreign
        ones -- which is how a handover is observed from the OTHER side of the
        world, the reason two pointer clients exist -- are read back too.

        `finality` on the foreign set is SAMPLED. It asks the whole close group
        and never stops early, so it is the most expensive read here, while the
        handover population only ever grows; an unsampled sweep would eventually
        spend all its time on it.

        A FOREIGN handover's age is measured from when this client first saw the
        published pair, not from the handover itself: the transfers file carries
        no timestamp, so the real moment is unknowable from here. Foreign rows in
        the by-age table therefore UNDER-state the age, and the table says so.
        """
        for address, entry in list(self.ledger.transferred.items()):
            transferred_at = entry.get("transferred_at", now)
            age = int(now - transferred_at)
            self.check_finality(
                address,
                owned=True,
                age_secs=age,
                # Exact even on a fork, where several states are reported and
                # only one of them is ours. Absent on a ledger written before
                # the id was recorded, which falls back to the single state.
                state_id=entry.get("state_id") or None,
            )
            self.check_redirect(address, entry["recipient"], owned=True, age_secs=age)

        foreign = list(self.ledger.foreign_transfers.items())
        for address, entry in foreign:
            self.check_redirect(
                address,
                entry["recipient"],
                owned=False,
                age_secs=int(now - entry.get("first_seen", now)),
            )
        sample = (
            foreign
            if len(foreign) <= FOREIGN_FINALITY_SAMPLE
            else random.sample(foreign, FOREIGN_FINALITY_SAMPLE)
        )
        for address, entry in sample:
            self.check_finality(
                address,
                owned=False,
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
                **counter_fields(expected_counter, record.get("counter")),
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

    def flush_transfers(self) -> None:
        """Publish `<old> <recipient>` for every handover this client has made."""
        lines = [f"{old} {entry['recipient']}" for old, entry in self.ledger.transferred.items()]
        published = publish(lines, self.transfers_file, self.transfers_presigned_url, "handover")
        if published:
            print(
                f"published {len(lines)} handover(s) as {self.own_transfers_key_name}",
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
        handover = (
            f"transfer/{self.transfer_interval}s " if self.transfer_interval > 0 else "no-handover "
        )
        print(
            f"Pointer client starting: host={self.host} region={self.region} "
            f"index={self.client_index} create/{self.create_interval}s "
            f"update/{self.update_interval}s sweep/{self.sweep_interval}s "
            f"{handover}"
            f"chain={self.chain_fraction} known="
            f"{len(self.ledger.own)} own + {len(self.ledger.foreign)} foreign, "
            f"{len(self.ledger.transferred)} handed over",
            flush=True,
        )
        self.pool.refresh(self.own_key_name, self.own_transfers_key_name)
        # Publish whatever a previous incarnation owned before doing anything
        # else, so a restarted client's peers can sweep its pointers again
        # immediately rather than after the first create.
        if self.ledger.established_own():
            self.flush_addresses()
        if self.ledger.transferred:
            self.flush_transfers()

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
        # Registered only when enabled: an interval of 0 would make the scheduler
        # pick it every pass, which on a paid operation is a busy loop that
        # spends money. See the module docstring for why 0 is the default.
        if self.transfer_interval > 0:
            due[OP_TRANSFER] = now + self.transfer_interval
            handlers[OP_TRANSFER] = self.do_transfer
            intervals[OP_TRANSFER] = self.transfer_interval
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
