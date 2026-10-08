"""Pointer results: the summary, the age buckets, the gates and the recording.

The arithmetic here is what the two run issues gate on verbatim, and what
`deployments compare` turns into a verdict. Two conventions run through the
whole file and are the reason it is written this way:

* `stale` is a FAILURE for the success rate and an EXPECTED event (ADR-0016).
  Both at once. A test that asserted only one of those would let the other be
  silently changed.
* Reads and writes are never pooled. A pointer client performs far more reads
  than writes, so a merged rate would let the sweeps bury a write failure.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from saorsa_db.db import Base
from saorsa_db.models import PointerOp, PointerOutcome
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.gate_comparison import NOT_ASSESSED, RunMetrics, compare_runs
from saorsa_deploy.gate_config import load_gate_config
from saorsa_deploy.pointer_results import (
    age_buckets,
    filter_to_window,
    normalise_records,
    percentile,
    summarise,
    summarise_by_host,
)

T0 = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


def _op(op, outcome, **extra):
    record = {
        "host": "PTR-1",
        "region": "lon1",
        "op": op,
        "outcome": outcome,
        "owned": True,
        "duration_ms": 1000,
        "finished_at": T0,
    }
    record.update(extra)
    return record


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


def test_reads_and_writes_are_never_pooled():
    """A client performs far more reads than writes, so one rate over both would
    let the sweeps bury a write failure entirely."""
    records = [
        _op("create", "ok", paid_attempts=1),
        _op("update", "lost_write", paid_attempts=1),
        *[_op("sweep", "ok") for _ in range(50)],
    ]
    summary = summarise(records)
    assert summary.write_attempts == 2
    assert summary.write_ok == 1
    assert summary.write_success_rate == pytest.approx(50.0)
    assert summary.read_attempts == 50
    assert summary.read_success_rate == pytest.approx(100.0)


def test_stale_is_a_failure_for_the_rate_and_reported_separately():
    """Both at once, and deliberately. ADR-0016 expects a non-zero stale rate, so
    the success rate alone would read as a network in trouble on a healthy run --
    and the stale rate alone would hide a read that failed outright."""
    records = [_op("sweep", "ok"), _op("sweep", "stale"), _op("sweep", "error")]
    summary = summarise(records)
    assert summary.read_success_rate == pytest.approx(100 / 3)
    assert summary.stale_reads == 1
    assert summary.stale_read_rate == pytest.approx(100 / 3)


def test_lost_writes_count_only_against_writes():
    records = [
        _op("create", "lost_write", paid_attempts=1),
        _op("update", "ok", paid_attempts=1),
        # A read cannot be a lost write, so one here must not move the rate.
        _op("sweep", "stale"),
    ]
    summary = summarise(records)
    assert summary.lost_writes == 1
    assert summary.lost_write_rate == pytest.approx(50.0)


def test_read_your_writes_counts_the_confirming_read():
    records = [
        _op("create", "ok", paid_attempts=1, read_back="ok"),
        _op("update", "ok", paid_attempts=1, read_back="stale"),
        # A write with no confirming read is not counted either way: it is not a
        # match and it is not a miss.
        _op("update", "lost_write", paid_attempts=1),
    ]
    summary = summarise(records)
    assert summary.read_your_writes_total == 2
    assert summary.read_your_writes_ok == 1


def test_rpc_error_writes_are_separated_from_refusals():
    """A refusal and a shortfall are ordinary events on a healthy network; only
    `error` indicates a fault, which is why it is counted apart."""
    records = [
        _op("create", "error", paid_attempts=0),
        _op("update", "lost_write", paid_attempts=1),
        _op("update", "ok", paid_attempts=1),
    ]
    summary = summarise(records)
    assert summary.rpc_error_writes == 1


def test_a_double_payment_is_a_write_that_paid_twice():
    """The ADR's warning: there is no recovery journal, so a retried write pays a
    second time."""
    records = [
        _op("create", "ok", paid_attempts=1),
        _op("update", "ok", paid_attempts=2, attempts=2),
    ]
    assert summarise(records).double_payments == 1


def test_a_paid_write_that_did_not_land_is_counted():
    """The observable form of the same warning, and the one that happens with no
    retry at all: the money went out and the state is not there."""
    records = [
        _op("create", "lost_write", paid_attempts=1),
        _op("update", "error", paid_attempts=0),
        _op("update", "ok", paid_attempts=1),
    ]
    assert summarise(records).paid_but_failed_writes == 1


def test_cost_per_write_is_over_the_writes_that_actually_paid():
    """A write refused before paying consumed nothing. Counting it in the
    denominator would report a cost per write lower than any write ever cost."""
    atto = 10**18
    records = [
        _op("create", "ok", paid_attempts=1, ant_spend_atto=2 * atto, gas_spend_wei=atto),
        _op("update", "ok", paid_attempts=1, ant_spend_atto=4 * atto, gas_spend_wei=3 * atto),
        _op("update", "error", paid_attempts=0),
    ]
    summary = summarise(records)
    assert summary.paid_write_count == 2
    assert summary.mean_ant_atto_per_write == Decimal(3 * atto)
    assert summary.mean_gas_wei_per_write == Decimal(2 * atto)


def test_an_unreadable_gas_balance_is_unknown_not_zero():
    """The balance comes from a JSON-RPC call that can fail on its own. Dividing
    a zero sum by the paid count reported "0 ETH" -- a paid on-chain write shown
    as free. Found on the live verification run, where the public Arbitrum
    Sepolia endpoint 403s urllib's default User-Agent."""
    records = [_op("create", "ok", paid_attempts=1, ant_spend_atto=10**18, gas_spend_wei=None)]
    summary = summarise(records)
    assert summary.paid_write_count == 1
    assert summary.gas_measured_writes == 0
    assert summary.mean_gas_wei_per_write is None
    # The ANT side was measured and must still be reported.
    assert summary.mean_ant_atto_per_write == Decimal(10**18)


def test_gas_is_averaged_over_the_writes_it_was_measured_on():
    records = [
        _op("create", "ok", paid_attempts=1, gas_spend_wei=2 * 10**14),
        _op("update", "ok", paid_attempts=1, gas_spend_wei=None),
    ]
    summary = summarise(records)
    assert summary.gas_measured_writes == 1
    assert summary.mean_gas_wei_per_write == Decimal(2 * 10**14)


def test_cost_is_none_when_nothing_paid():
    """Not zero: a run where every write was refused before paying did not cost
    nothing per write, it has no cost per write."""
    assert summarise([_op("create", "error", paid_attempts=0)]).mean_ant_atto_per_write is None


def test_every_op_and_outcome_column_is_present_even_at_zero():
    """A column that vanishes when it is zero makes two runs' tables
    incomparable by eye, and "no anomalies" is a finding."""
    summary = summarise([_op("sweep", "ok")])
    assert set(summary.by_op) == {"create", "update", "get", "sweep", "resolve"}
    assert summary.by_op["create"].attempts == 0
    assert "anomaly" in summary.by_op["sweep"].outcomes


def test_an_unknown_op_is_counted_nowhere():
    """A future op counted into a neighbouring bucket would give a wrong
    denominator, which is worse than a gap that announces itself."""
    summary = summarise([_op("sweep", "ok"), _op("teleport", "ok")])
    assert summary.by_op["sweep"].attempts == 1
    assert sum(c.attempts for c in summary.by_op.values()) == 1


def test_per_host_summaries_are_separate():
    records = [
        _op("sweep", "ok", host="PTR-1"),
        _op("sweep", "stale", host="PTR-2"),
        _op("sweep", "stale", host="PTR-2"),
    ]
    by_host = summarise_by_host(records)
    assert by_host["PTR-1"].stale_reads == 0
    assert by_host["PTR-2"].stale_reads == 2


def test_percentile_of_nothing_is_none_not_zero():
    assert percentile([], 0.5) is None
    assert percentile([10.0, 20.0, 30.0], 0.5) == 20.0


# ---------------------------------------------------------------------------
# Age buckets
# ---------------------------------------------------------------------------


def test_age_buckets_cover_sweeps_only():
    """A `get` is the read that immediately follows a write, so its age is always
    ~0. Including it would load the youngest bucket with reads that are not aged
    samples at all, and flatten exactly the trend being looked for."""
    records = [
        _op("sweep", "ok", age_secs=100),
        _op("get", "ok", age_secs=0),
    ]
    buckets = age_buckets(records, 1800)
    assert len(buckets) == 1
    assert buckets[0].attempts == 1


def test_own_and_foreign_pointers_are_bucketed_separately():
    """Only for an owned pointer is the expected state known. A foreign `stale`
    means its counter went backwards, which is a different claim."""
    records = [
        _op("sweep", "stale", age_secs=100, owned=True),
        _op("sweep", "ok", age_secs=100, owned=False),
    ]
    buckets = age_buckets(records, 1800)
    assert [b.owned for b in buckets] == [True, False]
    assert buckets[0].rate("stale") == pytest.approx(100.0)
    assert buckets[1].rate("stale") == pytest.approx(0.0)


def test_a_rising_stale_rate_with_age_is_visible():
    records = [
        *[_op("sweep", "ok", age_secs=60) for _ in range(10)],
        *[_op("sweep", "ok", age_secs=4000) for _ in range(8)],
        *[_op("sweep", "stale", age_secs=4000) for _ in range(2)],
    ]
    buckets = age_buckets(records, 1800)
    assert [b.label for b in buckets] == ["0-30m", "60-90m"]
    assert buckets[0].rate("stale") == pytest.approx(0.0)
    assert buckets[1].rate("stale") == pytest.approx(20.0)


def test_a_sweep_with_no_age_is_skipped_rather_than_bucketed_at_zero():
    assert age_buckets([_op("sweep", "ok")], 1800) == []


def test_a_zero_bucket_width_is_refused():
    with pytest.raises(ValueError):
        age_buckets([_op("sweep", "ok", age_secs=1)], 0)


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------


def test_the_window_is_taken_on_when_the_op_finished():
    """A pointer write can take tens of seconds, so an op straddling a phase
    boundary belongs to the phase it completed in -- matching the file-op rule."""
    early = _op("sweep", "ok", finished_at=T0 - timedelta(minutes=5))
    inside = _op("sweep", "ok", finished_at=T0 + timedelta(minutes=5))
    late = _op("sweep", "ok", finished_at=T0 + timedelta(hours=2))
    kept = filter_to_window([early, inside, late], T0, T0 + timedelta(hours=1))
    assert kept == [inside]


def test_a_record_with_no_finish_time_is_dropped_from_a_window():
    assert filter_to_window([_op("sweep", "ok", finished_at=None)], T0, None) == []


# ---------------------------------------------------------------------------
# Postgres round trip
# ---------------------------------------------------------------------------


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyDeploymentRepository(session)


def _deployment(repo):
    return repo.create(name="PTR-01", deploy_args={"kind": "testnet"})


def test_a_pointer_record_round_trips_through_postgres(repo):
    deployment = _deployment(repo)
    repo.upsert_pointer_record(
        deployment_id=deployment.id,
        client_hostname="PTR-1",
        region="lon1",
        op=PointerOp.UPDATE,
        outcome=PointerOutcome.STALE,
        start_ns=1_759_000_000_000_000_000,
        influx_time=T0,
        started_at=T0,
        finished_at=T0,
        duration_ms=4200,
        pointer_address="ab" * 32,
        owned=True,
        counter_expected=5,
        counter_observed=4,
        target_kind="chunk",
        age_secs=3600,
        read_back="stale",
        attempts=1,
        paid_attempts=1,
        # Beyond i64, which is why the column is Numeric(78).
        ant_spend_atto=5 * 10**21,
        gas_spend_wei=10**15,
        error="",
        network_profile=None,
    )
    rows = repo.list_pointer_records_for_deployment(deployment.id)
    assert len(rows) == 1
    normalised = normalise_records(rows)[0]
    assert normalised["op"] == "update"
    assert normalised["outcome"] == "stale"
    assert normalised["counter_expected"] == 5
    assert normalised["ant_spend_atto"] == 5 * 10**21
    assert normalised["age_secs"] == 3600


def test_recording_the_same_operation_twice_revises_it(repo):
    """Keyed on (deployment, host, start_ns), like the file-op upserts, so a
    `record-results` re-run against the same Influx range revises rather than
    duplicates."""
    deployment = _deployment(repo)
    common = dict(
        deployment_id=deployment.id,
        client_hostname="PTR-1",
        region="lon1",
        op=PointerOp.SWEEP,
        start_ns=42,
        influx_time=T0,
        started_at=T0,
        finished_at=T0,
        duration_ms=100,
    )
    repo.upsert_pointer_record(outcome=PointerOutcome.ERROR, **common)
    repo.upsert_pointer_record(outcome=PointerOutcome.OK, **common)
    rows = repo.list_pointer_records_for_deployment(deployment.id)
    assert len(rows) == 1
    assert rows[0].outcome is PointerOutcome.OK


# ---------------------------------------------------------------------------
# The [pointer] gates
# ---------------------------------------------------------------------------


def _run(deployment_id, name, pointer_records):
    return RunMetrics(
        deployment_id=deployment_id,
        name=name,
        window_label="steady-state",
        window_start=T0,
        window_end=T0 + timedelta(hours=3),
        pointer_records=pointer_records,
    )


def _gates(payload, prefix="Pointer"):
    return {g["gate"]: g for g in payload["gates"] if g["gate"].startswith(prefix)}


def test_no_pointer_gates_when_the_candidate_has_none():
    """Every existing comparison must be byte-identical: a run with no pointer
    clients has no pointer gates, not seven NOT ASSESSED rows."""
    payload = compare_runs(_run(1, "c", []), _run(2, "b", []), load_gate_config())
    assert _gates(payload) == {}


def test_pointer_gates_are_emitted_not_assessed_against_a_pointerless_baseline():
    """Emitted, never omitted. Every baseline on the first pointer-carrying train
    has no pointer records, and a gate missing from the report reads as one
    nobody thought about."""
    candidate = _run(
        1,
        "c",
        [_op("create", "ok", paid_attempts=1), _op("sweep", "ok")],
    )
    payload = compare_runs(candidate, _run(2, "b", []), load_gate_config())
    gates = _gates(payload)
    # The two FLOORS are absolute, so they ARE assessed on the candidate alone.
    assert gates["Pointer write success floor [pointer]"]["verdict"] == "PASS"
    assert gates["Pointer read success floor [pointer]"]["verdict"] == "PASS"
    # Every relative gate says why it could not be judged.
    relative = gates["Pointer stale-read rate [pointer]"]
    assert relative["verdict"] == NOT_ASSESSED
    assert "baseline has no pointer records" in relative["detail"]


def test_the_pointer_floors_are_advisory_and_never_hard():
    """Uncalibrated numbers on a tier no run has measured. They are reported in
    full and must not be able to block a ship."""
    candidate = _run(1, "c", [_op("create", "lost_write", paid_attempts=1)])
    payload = compare_runs(candidate, _run(2, "b", []), load_gate_config())
    gate = _gates(payload)["Pointer write success floor [pointer]"]
    assert gate["verdict"] == "FAIL"
    assert gate["kind"] == "soft"
    assert gate["advisory"] is True
    # Advisory, so it stays out of the overall verdict entirely.
    assert payload["overall"] == "PASS"


def test_a_rising_stale_rate_against_a_baseline_fails_the_gate():
    baseline = _run(2, "b", [_op("sweep", "ok") for _ in range(99)] + [_op("sweep", "stale")])
    candidate = _run(1, "c", [_op("sweep", "ok") for _ in range(90)] + [_op("sweep", "stale")] * 10)
    payload = compare_runs(candidate, baseline, load_gate_config())
    gate = _gates(payload)["Pointer stale-read rate [pointer]"]
    assert gate["candidate"] == pytest.approx(10.0)
    assert gate["baseline"] == pytest.approx(1.0)
    assert gate["verdict"] == "FAIL"
    # A relative pointer gate COUNTS, unlike the floors -- it needs no
    # calibration, it only asks whether this release made the tier worse.
    assert gate["advisory"] is False
    assert payload["overall"] == "SOFT FAIL"


def test_a_falling_stale_rate_passes():
    """One-sided, like every other soft gate: an improvement is not a regression."""
    baseline = _run(2, "b", [_op("sweep", "ok") for _ in range(90)] + [_op("sweep", "stale")] * 10)
    candidate = _run(1, "c", [_op("sweep", "ok") for _ in range(99)] + [_op("sweep", "stale")])
    payload = compare_runs(candidate, baseline, load_gate_config())
    assert _gates(payload)["Pointer stale-read rate [pointer]"]["verdict"] == "PASS"


def test_a_zero_stale_rate_on_both_runs_passes_rather_than_reading_not_assessed():
    """0.000% on both runs is the tier behaving on both, not a missing
    measurement. STG-01 #655 vs #627 read NOT ASSESSED ('baseline is zero, so a
    relative change is undefined') for both rates while the count gates beside
    them read PASS for the same 0 vs 0; the two must agree."""
    baseline = _run(
        2, "b", [_op("sweep", "ok") for _ in range(50)] + [_op("update", "ok", paid_attempts=1)]
    )
    candidate = _run(
        1, "c", [_op("sweep", "ok") for _ in range(50)] + [_op("update", "ok", paid_attempts=1)]
    )
    gates = _gates(compare_runs(candidate, baseline, load_gate_config()))
    for name in ("Pointer stale-read rate [pointer]", "Pointer lost-write rate [pointer]"):
        gate = gates[name]
        assert gate["verdict"] == "PASS", name
        assert gate["candidate"] == 0 and gate["baseline"] == 0
        assert gate["detail"].startswith("neither run recorded this at all")


def test_a_stale_rate_appearing_from_a_zero_baseline_fails():
    """Mirrors the count gates: no percentage expresses 0 -> 10%, but it is the
    clearest rise the gate can see, so it is FAIL, never NOT ASSESSED."""
    baseline = _run(2, "b", [_op("sweep", "ok") for _ in range(100)])
    candidate = _run(1, "c", [_op("sweep", "ok") for _ in range(90)] + [_op("sweep", "stale")] * 10)
    gate = _gates(compare_runs(candidate, baseline, load_gate_config()))[
        "Pointer stale-read rate [pointer]"
    ]
    assert gate["verdict"] == "FAIL"
    assert gate["baseline"] == 0 and gate["candidate"] == pytest.approx(10.0)
    assert "appeared on the candidate" in gate["detail"]


def test_a_zero_latency_baseline_is_still_not_assessed():
    """The opt-in is for the two rates only: a zero write latency is an empty
    sample, and PASS would hide a broken capture."""
    from saorsa_deploy.gate_comparison import _compare_scalar

    result = _compare_scalar(
        gate="Pointer write latency p50 [pointer]",
        candidate=100.0,
        baseline=0.0,
        threshold=20.0,
        config=load_gate_config(),
        unit="ms",
    )
    assert result.verdict == NOT_ASSESSED
    assert "relative change is undefined" in result.detail


def test_no_pointer_gate_reaches_the_exit_code():
    """`compare` exits 2 only on a HARD fail. Every pointer gate is soft, so the
    worst a pointer regression can produce is SOFT FAIL."""
    baseline = _run(2, "b", [_op("update", "ok", paid_attempts=1, duration_ms=100)])
    candidate = _run(1, "c", [_op("update", "lost_write", paid_attempts=1, duration_ms=100_000)])
    payload = compare_runs(candidate, baseline, load_gate_config())
    assert all(g["kind"] == "soft" for g in _gates(payload).values())
    assert payload["overall"] != "HARD FAIL"


def test_the_pointer_floors_survive_new_baseline():
    """`--new-baseline` blanks every gate that needs a baseline. The floors do
    not need one, so blanking them would throw away the only pointer measurement
    a first run can make."""
    candidate = _run(1, "c", [_op("create", "ok", paid_attempts=1)])
    payload = compare_runs(candidate, None, load_gate_config(), new_baseline=True)
    gates = _gates(payload)
    assert gates["Pointer write success floor [pointer]"]["verdict"] == "PASS"
    assert gates["Pointer stale-read rate [pointer]"]["verdict"] == NOT_ASSESSED
    assert gates["Pointer stale-read rate [pointer]"]["detail"] == "new baseline"


def test_cost_per_write_is_gated_relative_to_the_baseline():
    atto = 10**18
    baseline = _run(2, "b", [_op("create", "ok", paid_attempts=1, ant_spend_atto=atto)])
    candidate = _run(1, "c", [_op("create", "ok", paid_attempts=1, ant_spend_atto=2 * atto)])
    payload = compare_runs(candidate, baseline, load_gate_config())
    gate = _gates(payload)["Pointer ANT cost per write [pointer]"]
    assert gate["delta_percent"] == pytest.approx(100.0)
    assert gate["verdict"] == "FAIL"
