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
OPS = (
    "create",
    "update",
    "get",
    "sweep",
    "resolve",
    "transfer",
    "finality",
    "controller",
    "takeback",
)

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
    "settling",
    "unconfirmed",
    "forked",
)

#: The outcomes a READ can have -- the original seven, before the handover work.
#: The sweep table is rendered over these rather than over every outcome, because
#: the three finality statuses are structurally impossible on a `sweep` row and
#: three always-zero columns in an already-wide table buy nothing. A run with no
#: handovers therefore renders that table exactly as it did before.
SWEEP_OUTCOMES = (
    "ok",
    "stale",
    "lost_write",
    "not_found",
    "read_shortfall",
    "anomaly",
    "error",
)

#: The paid ops. Reads cost nothing, so every cost figure is taken over these.
#: `transfer` pays -- it buys a quote for the final state like any other write.
WRITE_OPS = ("create", "update", "transfer")
#: The free ops. `get` is the read that confirms a write; `sweep` is the
#: periodic read of the whole known population and the only one with a
#: meaningful age.
#:
#: `finality`, `controller` and `takeback` are in NEITHER list, deliberately. A
#: take-back's success is a REFUSAL, so averaging it with writes that are
#: supposed to land would invert its contribution; and `finality` asks the whole
#: close group rather than reading a state, so its `settling` is not a read that
#: failed. They are reported in the handover section instead.
READ_OPS = ("get", "sweep", "resolve")

#: The counter a handover's final state carries, `u64::MAX`. It is also how a
#: redirect read is identified in the recorded rows: a `get` with this
#: `counter_expected` is a read of a handed-over address and nothing else is.
FINAL_COUNTER = 18446744073709551615

#: The handover ops, in reporting order.
HANDOVER_OPS = ("transfer", "finality", "controller", "takeback")

#: `finality` statuses, in reporting order. The table is written in the CLI's
#: vocabulary rather than in outcome names, because "open" and "final" are what
#: an operator reads in `ant pointer finality` output.
FINALITY_STATUSES = ("final", "settling", "unconfirmed", "forked", "open", "error")

#: outcome -> status, so the tables can be derived from `outcome` alone. The
#: recorded `finality_status` column is the CLI's verbatim word and is kept for
#: a status a future ant-client adds, which this build maps to `error`; the
#: tables use the outcome so a NULL column cannot empty them.
FINALITY_STATUS_BY_OUTCOME = {
    "ok": "final",
    "settling": "settling",
    "unconfirmed": "unconfirmed",
    "forked": "forked",
    "stale": "open",
    "error": "error",
}

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
                # Numeric columns, so SQLAlchemy hands back a Decimal. Reduced
                # to int for the same reason the spend columns are: the Influx
                # reader carries ints, and the two must hand `summarise`
                # identical input.
                "counter_expected": (
                    int(row.counter_expected) if row.counter_expected is not None else None
                ),
                "counter_observed": (
                    int(row.counter_observed) if row.counter_observed is not None else None
                ),
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
                "start_ns": row.start_ns,
                "recipient": row.recipient,
                "finality_status": row.finality_status,
                "answered": row.answered,
                "group_size": row.group_size,
                "holders": row.holders,
                "attempt": row.attempt,
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
                outcomes={o: 0 for o in SWEEP_OUTCOMES},
            )
            buckets[key] = bucket
        bucket.attempts += 1
        outcome = record.get("outcome") or "error"
        bucket.outcomes[outcome] = bucket.outcomes.get(outcome, 0) + 1
    return [buckets[key] for key in sorted(buckets, key=lambda k: (not k[1], k[0]))]


# ---------------------------------------------------------------------------
# Handovers (V2-1354)
# ---------------------------------------------------------------------------


@dataclass
class TakebackCounts:
    """One take-back attempt kind -- `update` or `transfer`."""

    attempt: str
    attempts: int = 0
    #: Refused before paying. This is the PASS: a handover that cannot be taken
    #: back is the property the whole loop exists to demonstrate.
    refused: int = 0
    #: Accepted, or refused but still cost money. A refusal that charged is not
    #: a refusal, which is why the two land in one bucket.
    accepted_or_spent: int = 0
    errors: int = 0

    @property
    def refused_rate(self) -> float | None:
        if not self.attempts:
            return None
        return 100.0 * self.refused / self.attempts


@dataclass
class HandoverSummary:
    """Everything the handovers section of `print-pointers` reports.

    Separate from :class:`PointerSummary` rather than more properties on it,
    because a handover is not an op: it is a nine-step cycle whose steps land in
    four different ops, and the figures that matter are about the cycle.
    """

    attempts: int = 0
    completed: int = 0
    #: Refused before paying because a close-group peer stayed silent. EXCLUDED
    #: from the completion denominator: the client was told "ask again when all
    #: of them can be asked", which is the refusal working as designed, not a
    #: handover that failed. Reported as its own rate over all attempts so it
    #: cannot be lost.
    refused_silent_peer: int = 0
    #: Refused before paying because a rival final state already exists. Nothing
    #: of ours finalized the pointer, so it IS a failed handover -- and an
    #: anomaly, since it means something else ended the pointer's life.
    refused_rival_final: int = 0
    lost_writes: int = 0
    errors: int = 0

    transfer_latencies_ms: list[float] = field(default_factory=list)

    #: ANT and gas per PAID create and per PAID transfer, kept apart because a
    #: handover is one of each and the sum is what one costs.
    create_ant_atto: list[int] = field(default_factory=list)
    create_gas_wei: list[int] = field(default_factory=list)
    transfer_ant_atto: list[int] = field(default_factory=list)
    transfer_gas_wei: list[int] = field(default_factory=list)

    #: The FIRST finality check per handed-over address, by status: how the close
    #: group looked immediately after the transfer.
    first_finality: dict[str, int] = field(default_factory=dict)

    takebacks: dict[str, TakebackCounts] = field(default_factory=dict)

    #: The three post-handover checks. `(ok, attempts)` each.
    redirect_ok: int = 0
    redirect_attempts: int = 0
    controller_ok: int = 0
    controller_attempts: int = 0
    resolve_ok: int = 0
    resolve_attempts: int = 0

    #: Finality checks taken at least :data:`SWEEP_FINALITY_MIN_AGE_SECS` after
    #: the handover, and how many of them read `final`.
    aged_finality_ok: int = 0
    aged_finality_total: int = 0

    @property
    def completion_denominator(self) -> int:
        return self.completed + self.lost_writes + self.errors + self.refused_rival_final

    @property
    def completion_rate(self) -> float | None:
        """Share of handovers that were attempted AND resolved one way or other.

        The silent-peer refusals are not in the denominator: they are the
        pre-payment guard declining to act on incomplete information, and a run
        on a churning network would otherwise show a completion rate that
        measures dial churn rather than handovers.
        """
        if not self.completion_denominator:
            return None
        return 100.0 * self.completed / self.completion_denominator

    @property
    def silent_peer_refusal_rate(self) -> float | None:
        if not self.attempts:
            return None
        return 100.0 * self.refused_silent_peer / self.attempts

    @property
    def first_finality_total(self) -> int:
        return sum(self.first_finality.values())

    def first_finality_rate(self, status: str) -> float | None:
        total = self.first_finality_total
        if not total:
            return None
        return 100.0 * self.first_finality.get(status, 0) / total

    def _mean(self, values: list[int]) -> Decimal | None:
        if not values:
            return None
        return Decimal(sum(values)) / len(values)

    @property
    def mean_ant_atto_per_handover(self) -> Decimal | None:
        """Mean ANT a handover costs: the recipient create plus the transfer.

        The creates are averaged over EVERY paid create, not only the ones a
        handover made: a pointer create is a single-node quote at the same price
        whichever loop asked for it, and the records carry no marker saying which
        did. ``None`` when either half has no paid sample -- a half-measured cost
        reported as a whole one would understate what a handover costs.
        """
        create = self._mean(self.create_ant_atto)
        transfer = self._mean(self.transfer_ant_atto)
        if create is None or transfer is None:
            return None
        return create + transfer

    @property
    def mean_gas_wei_per_handover(self) -> Decimal | None:
        """Mean native gas a handover costs. See the ANT figure above."""
        create = self._mean(self.create_gas_wei)
        transfer = self._mean(self.transfer_gas_wei)
        if create is None or transfer is None:
            return None
        return create + transfer

    def transfer_latency(self, fraction: float) -> float | None:
        return percentile(self.transfer_latencies_ms, fraction)

    def _rate(self, ok: int, attempts: int) -> float | None:
        if not attempts:
            return None
        return 100.0 * ok / attempts

    @property
    def redirect_ok_rate(self) -> float | None:
        return self._rate(self.redirect_ok, self.redirect_attempts)

    @property
    def controller_ok_rate(self) -> float | None:
        return self._rate(self.controller_ok, self.controller_attempts)

    @property
    def resolve_ok_rate(self) -> float | None:
        return self._rate(self.resolve_ok, self.resolve_attempts)

    @property
    def sweep_finality_ok_rate(self) -> float | None:
        """Share of AGED finality checks that found the handover held.

        Aged, because a check taken seconds after the transfer legitimately reads
        `settling` -- see :data:`SWEEP_FINALITY_MIN_AGE_SECS`. This is the figure
        the `handover final on sweep [pointer]` gate reads.
        """
        if not self.aged_finality_total:
            return None
        return 100.0 * self.aged_finality_ok / self.aged_finality_total


#: A finality check is "on sweep" once the handover has had time to converge.
#: 600s, which is the default sweep interval: a check taken seconds after the
#: transfer legitimately reads `settling`, and gating on those would gate on how
#: fast the close group happened to be that minute.
SWEEP_FINALITY_MIN_AGE_SECS = 600


def is_redirect_read(record: dict) -> bool:
    """Whether a `get` row is a read of a handed-over address.

    Identified by the expected counter being `u64::MAX`, which only a handover
    ever writes, or by the `recipient` column, which only a handover-related op
    carries. Inferring it from the presence of a `transfer` record for the same
    address would miss every FOREIGN redirect read -- the handover was made by
    the other client, so this run has no `transfer` row for it.
    """
    if record.get("op") != "get":
        return False
    # Either marker: a foreign redirect read carries the recipient the OTHER
    # client published, and the final counter is what the read is checked
    # against. Both are written on every redirect read; accepting either means a
    # row whose counter could not be stored is still attributable.
    return record.get("counter_expected") == FINAL_COUNTER or record.get("recipient") is not None


def summarise_handovers(records: list[dict]) -> HandoverSummary:
    """Fold the handover ops into the figures the handovers section reports."""
    summary = HandoverSummary(
        first_finality={status: 0 for status in FINALITY_STATUSES},
        takebacks={attempt: TakebackCounts(attempt=attempt) for attempt in ("update", "transfer")},
    )
    # The first finality check per address, by start_ns. "Immediately after the
    # transfer" in practice: the handover cycle runs it as the very next step,
    # and every later one comes from a sweep.
    first_finality: dict[str, tuple[int, str]] = {}

    for record in records:
        op = record.get("op")
        outcome = record.get("outcome") or "error"
        duration = record.get("duration_ms")
        if op == "transfer":
            summary.attempts += 1
            if outcome == "ok":
                summary.completed += 1
                if duration is not None:
                    summary.transfer_latencies_ms.append(float(duration))
            elif outcome == "read_shortfall":
                summary.refused_silent_peer += 1
            elif outcome == "anomaly":
                summary.refused_rival_final += 1
            elif outcome == "lost_write":
                summary.lost_writes += 1
            else:
                summary.errors += 1
            if record.get("paid_attempts"):
                spend = record.get("ant_spend_atto")
                gas = record.get("gas_spend_wei")
                if spend and spend > 0:
                    summary.transfer_ant_atto.append(int(spend))
                if gas is not None and gas > 0:
                    summary.transfer_gas_wei.append(int(gas))
        elif op == "create":
            if record.get("paid_attempts"):
                spend = record.get("ant_spend_atto")
                gas = record.get("gas_spend_wei")
                if spend and spend > 0:
                    summary.create_ant_atto.append(int(spend))
                if gas is not None and gas > 0:
                    summary.create_gas_wei.append(int(gas))
        elif op == "finality":
            status = FINALITY_STATUS_BY_OUTCOME.get(outcome, "error")
            address = record.get("address")
            start_ns = record.get("start_ns")
            if address and start_ns is not None:
                previous = first_finality.get(address)
                if previous is None or start_ns < previous[0]:
                    first_finality[address] = (start_ns, status)
            age = record.get("age_secs")
            if age is not None and int(age) >= SWEEP_FINALITY_MIN_AGE_SECS:
                summary.aged_finality_total += 1
                if outcome == "ok":
                    summary.aged_finality_ok += 1
        elif op == "controller":
            summary.controller_attempts += 1
            if outcome == "ok":
                summary.controller_ok += 1
        elif op == "takeback":
            # An attempt whose kind was not recorded still counts: it happened,
            # and dropping it would understate the number of take-backs tried.
            counts = summary.takebacks.setdefault(
                record.get("attempt") or "unknown",
                TakebackCounts(attempt=record.get("attempt") or "unknown"),
            )
            counts.attempts += 1
            if outcome == "ok":
                counts.refused += 1
            elif outcome == "anomaly":
                counts.accepted_or_spent += 1
            else:
                counts.errors += 1
        elif is_redirect_read(record):
            summary.redirect_attempts += 1
            if outcome == "ok":
                summary.redirect_ok += 1

    # Resolve rows cannot be told apart from a chained update's resolve by a
    # column, so they are matched to the addresses a handover was recorded for.
    handed_over = {
        record.get("address")
        for record in records
        if record.get("op") == "transfer" and (record.get("outcome") or "") == "ok"
    }
    for record in records:
        if record.get("op") == "resolve" and record.get("address") in handed_over:
            summary.resolve_attempts += 1
            if (record.get("outcome") or "") == "ok":
                summary.resolve_ok += 1

    for _, status in first_finality.values():
        summary.first_finality[status] = summary.first_finality.get(status, 0) + 1
    return summary


def finality_age_buckets(
    records: list[dict], bucket_secs: int = DEFAULT_AGE_BUCKET_SECS
) -> list[AgeBucket]:
    """`finality` outcomes bucketed by the handover's age at the check.

    FINALITY ONLY, and own and foreign kept apart for the same reason the sweep
    table keeps them apart: a foreign handover is observed from the other side of
    the world, and whether the close group looks the same from there is the
    question the second pointer client exists to answer.

    The buckets hold statuses rather than outcome names -- `final` and `open`
    rather than `ok` and `stale` -- because that is the vocabulary
    `ant pointer finality` prints.

    A FOREIGN row's age is a LOWER BOUND: the published handover list carries no
    timestamp, so a foreign client can only date a handover from when it first
    saw the pair. Those rows therefore sit in a younger bucket than they belong
    in, which the rendered table states.
    """
    if bucket_secs <= 0:
        raise ValueError("bucket_secs must be > 0")
    buckets: dict[tuple[int, bool], AgeBucket] = {}
    for record in records:
        if record.get("op") != "finality":
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
                outcomes={status: 0 for status in FINALITY_STATUSES},
            )
            buckets[key] = bucket
        bucket.attempts += 1
        status = FINALITY_STATUS_BY_OUTCOME.get(record.get("outcome") or "error", "error")
        bucket.outcomes[status] = bucket.outcomes.get(status, 0) + 1
    return [buckets[key] for key in sorted(buckets, key=lambda k: (not k[1], k[0]))]
