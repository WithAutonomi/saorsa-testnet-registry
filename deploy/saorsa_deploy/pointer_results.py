"""Aggregate pointer operations into the figures the run issues gate on.

Pure functions over a list of plain dicts, so the same arithmetic serves all
three readers: `deployments print-pointers --name` (live, from Influx),
`--id` (post-run, from Postgres) and `deployments compare`'s `[pointer]` gates.
They must not be able to disagree -- a run whose live table said 0.4% stale and
whose gate said 4% would be worse than having neither.

The record shape is the Influx point's, which is also what
``normalise_records`` turns a ``deployment_pointer_records`` row into:

    op, outcome, owned, address, counter_expected, counter_observed,
    target_kind, age_secs, read_back, attempts, paid_attempts,
    ant_spend_atto, gas_spend_wei, duration_ms, host

Two conventions worth stating, because both look like bugs otherwise:

**`stale` is a failure for the success rate but is EXPECTED.** ADR-0016 is
explicit that an eclipsed reader can be served an older, validly signed state
and cannot tell. So the success rate counts only `ok`, while the stale rate is
reported as its own figure -- the one whose TREND is the signal. Neither number
alone is readable without the other.

**Reads and writes are never pooled.** A write costs money and takes seconds; a
read is free. One latency percentile over both would describe neither, and one
success rate over both would let the far more numerous sweeps bury a write
failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

#: Every op, in the order they are reported. Fixed rather than derived from the
#: data: an op with no records must still appear, as zero, or a client whose
#: create loop never ran reads as a client that was not configured to create.
OPS = ("create", "update", "get", "sweep", "resolve")

#: Every outcome class, in the order they are reported. Same reasoning: a column
#: that vanishes when it is zero makes two runs' tables incomparable by eye.
OUTCOMES = (
    "ok",
    "stale",
    "lost_write",
    "not_found",
    "read_shortfall",
    "anomaly",
    "error",
)

#: The paid ops. Reads cost nothing, so every cost figure is taken over these.
WRITE_OPS = ("create", "update")
#: The free ops. `get` is the read that confirms a write; `sweep` is the
#: periodic read of the whole known population and the only one with a
#: meaningful age.
READ_OPS = ("get", "sweep", "resolve")

#: Default width of an age bucket in the `--by-age` table: 30 minutes, which on
#: a 2-4h run gives enough buckets to see a trend and enough records per bucket
#: for the rate in each to mean something.
DEFAULT_AGE_BUCKET_SECS = 1800


def normalise_records(rows: list) -> list[dict]:
    """Turn ``deployment_pointer_records`` rows into the Influx point shape.

    So that the Postgres reader and the Influx reader hand
    :func:`summarise` identical input and cannot drift. Enum columns are reduced
    to their values; the Numeric spend columns to ints, which is what the Influx
    side carries (one write's spend always fits in an i64).
    """
    out = []
    for row in rows:
        out.append(
            {
                "host": row.client_hostname,
                "region": row.region,
                "op": row.op.value,
                "outcome": row.outcome.value,
                "owned": bool(row.owned),
                "address": row.pointer_address,
                "counter_expected": row.counter_expected,
                "counter_observed": row.counter_observed,
                "target_kind": row.target_kind,
                "age_secs": row.age_secs,
                "read_back": row.read_back,
                "attempts": row.attempts,
                "paid_attempts": row.paid_attempts,
                "ant_spend_atto": (
                    int(row.ant_spend_atto) if row.ant_spend_atto is not None else None
                ),
                "gas_spend_wei": (
                    int(row.gas_spend_wei) if row.gas_spend_wei is not None else None
                ),
                "duration_ms": row.duration_ms,
                "finished_at": row.finished_at,
                "started_at": row.started_at,
            }
        )
    return out


def filter_to_window(records: list[dict], start, end) -> list[dict]:
    """Records whose operation FINISHED inside [start, end).

    On `finished_at`, not on when the op began, for the same reason the file-op
    windowing is: a pointer write can take tens of seconds, and an op that
    straddles a phase boundary belongs to the phase it completed in.
    """
    out = []
    for record in records:
        finished = record.get("finished_at")
        if finished is None:
            continue
        if start is not None and finished < start:
            continue
        if end is not None and finished >= end:
            continue
        out.append(record)
    return out


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile, so the value returned is always an observation.

    ``None`` on an empty list: a percentile of nothing is not zero, and
    reporting zero latency for a client that performed no ops would read as the
    fastest client in the run.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(fraction * len(ordered))))
    return float(ordered[index])


@dataclass
class OpCounts:
    """One op's attempt count and its breakdown by outcome."""

    op: str
    attempts: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> int:
        return self.outcomes.get("ok", 0)

    @property
    def success_rate(self) -> float | None:
        if not self.attempts:
            return None
        return 100.0 * self.ok / self.attempts


@dataclass
class PointerSummary:
    """Everything the run-facing output contract asks for, per host or pooled."""

    host: str
    by_op: dict[str, OpCounts]

    #: Writes whose confirming read returned exactly the state that was written.
    read_your_writes_ok: int = 0
    read_your_writes_total: int = 0
    #: Writes that ended in `error` -- a protocol or RPC failure, as opposed to
    #: a lost write (refused) or a shortfall (unanswered). Reported separately
    #: because they are the only write failures that indicate a fault.
    rpc_error_writes: int = 0
    #: Writes whose service spent ANT more than once. There is no recovery
    #: journal, so the ADR's warning is that a retried write pays twice; this is
    #: the count of that having happened.
    double_payments: int = 0
    #: Writes that spent ANT and did NOT land. The money is gone and the state
    #: is not there -- the observable form of the same warning, and the one that
    #: occurs without any retry.
    paid_but_failed_writes: int = 0

    write_count: int = 0
    paid_write_count: int = 0
    ant_spend_atto: int = 0
    gas_spend_wei: int = 0
    #: Writes whose native-gas spend could actually be READ. Counted separately
    #: from `paid_write_count` because the balance comes from a JSON-RPC call
    #: that can fail on its own -- and when it does, the cost is UNKNOWN, not
    #: zero. Dividing a zero sum by the paid count reported "0 ETH", i.e. that a
    #: paid on-chain write had been free, which is the opposite of the truth.
    gas_measured_writes: int = 0

    write_latencies_ms: list[float] = field(default_factory=list)
    read_latencies_ms: list[float] = field(default_factory=list)

    @property
    def write_attempts(self) -> int:
        return sum(self.by_op[op].attempts for op in WRITE_OPS)

    @property
    def write_ok(self) -> int:
        return sum(self.by_op[op].ok for op in WRITE_OPS)

    @property
    def read_attempts(self) -> int:
        return sum(self.by_op[op].attempts for op in READ_OPS)

    @property
    def read_ok(self) -> int:
        return sum(self.by_op[op].ok for op in READ_OPS)

    @property
    def write_success_rate(self) -> float | None:
        if not self.write_attempts:
            return None
        return 100.0 * self.write_ok / self.write_attempts

    @property
    def read_success_rate(self) -> float | None:
        if not self.read_attempts:
            return None
        return 100.0 * self.read_ok / self.read_attempts

    @property
    def stale_reads(self) -> int:
        return sum(self.by_op[op].outcomes.get("stale", 0) for op in READ_OPS)

    @property
    def stale_read_rate(self) -> float | None:
        """Percentage of reads served a state older than the one written.

        Expected to be non-zero. What matters is whether it moved.
        """
        if not self.read_attempts:
            return None
        return 100.0 * self.stale_reads / self.read_attempts

    @property
    def lost_writes(self) -> int:
        return sum(self.by_op[op].outcomes.get("lost_write", 0) for op in WRITE_OPS)

    @property
    def lost_write_rate(self) -> float | None:
        if not self.write_attempts:
            return None
        return 100.0 * self.lost_writes / self.write_attempts

    @property
    def mean_ant_atto_per_write(self) -> Decimal | None:
        """Mean ANT actually PAID per write that paid, in ATTO.

        In atto rather than whole ANT because every renderer here already takes
        an atto amount, and because the gate compares the figure numerically --
        dividing by 1e18 first would throw away precision for no reader's
        benefit.

        Denominator is the writes that spent, not every write: a write refused
        before it paid consumed nothing, and including it would report a cost
        per write lower than any write ever cost.
        """
        if not self.paid_write_count:
            return None
        return Decimal(self.ant_spend_atto) / self.paid_write_count

    @property
    def mean_gas_wei_per_write(self) -> Decimal | None:
        """Mean native gas actually PAID per write that paid, in WEI.

        ``None`` when no write's gas could be read. The balance comes from a
        JSON-RPC call against the payment chain, and a client that cannot reach
        it has an UNKNOWN gas cost -- which every renderer shows as ``-``. It
        previously divided a zero sum by the paid count and reported ``0 ETH``:
        a paid on-chain write shown as free.
        """
        if not self.gas_measured_writes:
            return None
        return Decimal(self.gas_spend_wei) / self.gas_measured_writes

    def write_latency(self, fraction: float) -> float | None:
        return percentile(self.write_latencies_ms, fraction)

    def read_latency(self, fraction: float) -> float | None:
        return percentile(self.read_latencies_ms, fraction)


def _empty_ops() -> dict[str, OpCounts]:
    return {op: OpCounts(op=op, outcomes={o: 0 for o in OUTCOMES}) for op in OPS}


def summarise(records: list[dict], host: str = "all") -> PointerSummary:
    """Fold a list of pointer operations into one summary."""
    summary = PointerSummary(host=host, by_op=_empty_ops())
    for record in records:
        op = record.get("op")
        if op not in summary.by_op:
            # An op this build does not know. Counted nowhere rather than into
            # a neighbouring bucket: a wrong denominator is worse than a gap.
            continue
        counts = summary.by_op[op]
        counts.attempts += 1
        outcome = record.get("outcome") or "error"
        counts.outcomes[outcome] = counts.outcomes.get(outcome, 0) + 1

        duration = record.get("duration_ms")
        if op in WRITE_OPS:
            summary.write_count += 1
            paid_attempts = record.get("paid_attempts") or 0
            spend = record.get("ant_spend_atto")
            gas = record.get("gas_spend_wei")
            if paid_attempts:
                summary.paid_write_count += 1
                if spend and spend > 0:
                    summary.ant_spend_atto += spend
                # `None` means the balance read failed, not that nothing was
                # spent. Only a write whose gas was actually measured enters the
                # denominator.
                if gas is not None:
                    summary.gas_measured_writes += 1
                    if gas > 0:
                        summary.gas_spend_wei += gas
            if paid_attempts > 1:
                summary.double_payments += 1
            if paid_attempts and outcome != "ok":
                summary.paid_but_failed_writes += 1
            if outcome == "error":
                summary.rpc_error_writes += 1
            read_back = record.get("read_back")
            if read_back:
                summary.read_your_writes_total += 1
                if read_back == "ok":
                    summary.read_your_writes_ok += 1
            if duration is not None:
                summary.write_latencies_ms.append(float(duration))
        elif duration is not None:
            summary.read_latencies_ms.append(float(duration))
    return summary


def summarise_by_host(records: list[dict]) -> dict[str, PointerSummary]:
    by_host: dict[str, list[dict]] = {}
    for record in records:
        by_host.setdefault(record.get("host") or "?", []).append(record)
    return {host: summarise(rows, host) for host, rows in sorted(by_host.items())}


@dataclass
class AgeBucket:
    """Sweep outcomes for pointers of one age band."""

    lower_secs: int
    upper_secs: int
    owned: bool
    attempts: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.lower_secs // 60}-{self.upper_secs // 60}m"

    @property
    def ok_rate(self) -> float | None:
        if not self.attempts:
            return None
        return 100.0 * self.outcomes.get("ok", 0) / self.attempts

    def rate(self, outcome: str) -> float | None:
        if not self.attempts:
            return None
        return 100.0 * self.outcomes.get(outcome, 0) / self.attempts


def age_buckets(records: list[dict], bucket_secs: int = DEFAULT_AGE_BUCKET_SECS) -> list[AgeBucket]:
    """Sweep outcomes bucketed by the pointer's age at read.

    SWEEPS ONLY, and own and foreign pointers kept separate. A `get` is the read
    that immediately follows a write, so its age is always ~0 and including it
    would load the youngest bucket with reads that are not aged samples at all.
    Own and foreign are separate because only for an owned pointer is the
    expected state known: a foreign pointer's `stale` means "its counter went
    backwards", which is a different claim.

    Buckets with no records are omitted here rather than padded, because the age
    range depends on how long the run went on -- unlike the op and outcome
    columns, which are fixed.
    """
    if bucket_secs <= 0:
        raise ValueError("bucket_secs must be > 0")
    buckets: dict[tuple[int, bool], AgeBucket] = {}
    for record in records:
        if record.get("op") != "sweep":
            continue
        age = record.get("age_secs")
        if age is None:
            continue
        owned = bool(record.get("owned", True))
        lower = (int(age) // bucket_secs) * bucket_secs
        key = (lower, owned)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = AgeBucket(
                lower_secs=lower,
                upper_secs=lower + bucket_secs,
                owned=owned,
                outcomes={o: 0 for o in OUTCOMES},
            )
            buckets[key] = bucket
        bucket.attempts += 1
        outcome = record.get("outcome") or "error"
        bucket.outcomes[outcome] = bucket.outcomes.get(outcome, 0) + 1
    return [buckets[key] for key in sorted(buckets, key=lambda k: (not k[1], k[0]))]
