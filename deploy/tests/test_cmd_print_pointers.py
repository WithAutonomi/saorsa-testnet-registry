"""`deployments print-pointers` — the two sources and the two tables.

The output contract here is what the run issues gate on verbatim, so the tests
assert the FIGURES appear, not just that the command exits. The two sources
matter as much: `--name` reads Influx and is the only view that exists while a
run is going on, `--id` reads Postgres and is the only view that survives
teardown, and they go through the same summary so they cannot disagree.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console
from saorsa_db.db import Base
from saorsa_db.models import PointerOp, PointerOutcome
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.cmd.print_pointers import cmd_print_pointers
from saorsa_deploy.pointer_results import FINAL_COUNTER as FINAL

T0 = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyDeploymentRepository(session)


def _args(**overrides):
    args = SimpleNamespace(
        name=None,
        id=None,
        window_start=None,
        window_end=None,
        by_age=False,
        age_bucket_secs=None,
        monitoring_name=None,
        influx_url="http://influx",
        influx_token="token",
        influx_org="ant",
        influx_bucket="testnet-metrics",
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _capture(args, points=None):
    console = Console(record=True, width=200)
    with patch("saorsa_deploy.cmd.print_pointers.Console", return_value=console):
        if points is not None:
            with patch(
                "saorsa_deploy.cmd.print_pointers.fetch_pointer_points",
                return_value=points,
            ):
                cmd_print_pointers(args)
        else:
            cmd_print_pointers(args)
    return console.export_text()


def _point(**extra):
    point = {
        "host": "PTR-1",
        "region": "lon1",
        "op": "create",
        "outcome": "ok",
        "owned": True,
        "address": "ab" * 32,
        "counter_expected": 0,
        "counter_observed": 0,
        "target_kind": "chunk",
        "age_secs": 0,
        "read_back": "ok",
        "attempts": 1,
        "paid_attempts": 1,
        "ant_spend_atto": 5 * 10**15,
        "gas_spend_wei": 2 * 10**14,
        "duration_ms": 4200,
        "start_ns": 1,
        "error": None,
        # Handover fields (V2-1354): absent on every op that is not part of one.
        "recipient": None,
        "finality_status": None,
        "answered": None,
        "group_size": None,
        "holders": None,
        "attempt": None,
        "_time": T0,
    }
    point.update(extra)
    return point


# ---------------------------------------------------------------------------
# The live (Influx) source
# ---------------------------------------------------------------------------


def test_the_headline_carries_every_figure_the_run_issues_gate_on():
    out = _capture(
        _args(name="PTR-01"),
        [
            _point(),
            _point(start_ns=2, op="update", outcome="lost_write"),
            _point(
                start_ns=3,
                op="sweep",
                outcome="stale",
                age_secs=3600,
                paid_attempts=0,
                read_back=None,
            ),
        ],
    )
    for figure in (
        "Write success",
        "Read success",
        "Stale reads",
        "Lost writes",
        "Read-your-writes matched",
        "RPC-error writes",
        "Double payments",
        "Paid but not stored",
        "Write latency p50 / p95",
        "Mean cost per write",
    ):
        assert figure in out, figure


def test_the_expected_stale_rate_is_stated_where_it_is_read():
    """A reader seeing a non-zero stale rate for the first time must not have to
    find ADR-0016 to know it is normal."""
    out = _capture(_args(name="PTR-01"), [_point(op="sweep", outcome="stale")])
    assert "EXPECTED" in out


def test_the_per_host_table_names_each_op_and_outcome():
    out = _capture(
        _args(name="PTR-01"),
        [_point(host="PTR-1"), _point(host="PTR-2", start_ns=2, op="sweep")],
    )
    assert "PTR-1" in out and "PTR-2" in out
    assert "read_shortfall" in out and "anomaly" in out


def test_an_empty_result_explains_itself_rather_than_printing_an_empty_table():
    out = _capture(_args(name="PTR-01"), [])
    assert "No pointer operations found" in out
    assert "ant-pointer" in out


def test_the_source_is_named_in_the_output():
    """Which source produced a number decides how much to trust it: Influx is
    live but dies at teardown, Postgres survives but lags `record-results`."""
    out = _capture(_args(name="PTR-01"), [_point()])
    assert "InfluxDB" in out and "record-results" in out


# ---------------------------------------------------------------------------
# --by-age
# ---------------------------------------------------------------------------


def test_by_age_buckets_sweeps_and_separates_own_from_foreign():
    out = _capture(
        _args(name="PTR-01", by_age=True),
        [
            _point(op="sweep", outcome="ok", age_secs=60, owned=True, start_ns=1),
            _point(op="sweep", outcome="stale", age_secs=4000, owned=True, start_ns=2),
            _point(op="sweep", outcome="ok", age_secs=4000, owned=False, start_ns=3),
        ],
    )
    assert "Sweep outcomes by pointer age at read" in out
    assert "0-30m" in out and "60-90m" in out
    assert "own" in out and "foreign" in out


def test_the_age_bucket_width_is_configurable():
    out = _capture(
        _args(name="PTR-01", by_age=True, age_bucket_secs=600),
        [_point(op="sweep", outcome="ok", age_secs=700)],
    )
    assert "10-20m" in out


def test_by_age_with_no_aged_sweeps_says_so():
    out = _capture(_args(name="PTR-01", by_age=True), [_point()])
    assert "nothing to" in out


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------


def test_a_window_narrows_on_when_the_operation_finished():
    out = _capture(
        _args(
            name="PTR-01",
            window_start="2026-09-26T09:30:00",
            window_end="2026-09-26T10:30:00",
        ),
        [
            _point(_time=T0),
            _point(start_ns=2, _time=T0 + timedelta(hours=3)),
        ],
    )
    assert "narrowed to 1" in out


def test_a_malformed_window_bound_is_rejected():
    with pytest.raises(SystemExit):
        _capture(_args(name="PTR-01", window_start="yesterday"), [_point()])


# ---------------------------------------------------------------------------
# The recorded (Postgres) source
# ---------------------------------------------------------------------------


def test_the_recorded_source_renders_the_same_figures(repo):
    deployment = repo.create(name="PTR-01", deploy_args={"kind": "testnet"})
    repo.upsert_pointer_record(
        deployment_id=deployment.id,
        client_hostname="PTR-1",
        region="lon1",
        op=PointerOp.CREATE,
        outcome=PointerOutcome.OK,
        start_ns=1,
        influx_time=T0,
        started_at=T0,
        finished_at=T0,
        duration_ms=4200,
        pointer_address="ab" * 32,
        owned=True,
        read_back="ok",
        attempts=1,
        paid_attempts=1,
        ant_spend_atto=5 * 10**15,
        gas_spend_wei=2 * 10**14,
    )
    console = Console(record=True, width=200)
    with patch("saorsa_deploy.cmd.print_pointers.Console", return_value=console):
        with patch("saorsa_deploy.cmd.print_pointers.get_repository", return_value=repo):
            cmd_print_pointers(_args(id=deployment.id))
    out = console.export_text()
    assert "deployment_pointer_records" in out
    assert "Write success" in out
    assert "1/1" in out


def test_an_unknown_deployment_id_is_refused(repo):
    with pytest.raises(SystemExit):
        with patch("saorsa_deploy.cmd.print_pointers.get_repository", return_value=repo):
            cmd_print_pointers(_args(id=999))


def test_no_database_url_refuses_the_id_path():
    with pytest.raises(SystemExit):
        with patch("saorsa_deploy.cmd.print_pointers.get_repository", return_value=None):
            cmd_print_pointers(_args(id=1))


# ---------------------------------------------------------------------------
# The handovers section (V2-1354)
# ---------------------------------------------------------------------------


def test_a_run_with_no_handovers_says_so_rather_than_omitting_the_section():
    """Which is every train whose RC predates `ant pointer transfer`. A section
    that vanishes reads as a measurement nobody took."""
    out = _capture(_args(name="PTR-01"), [_point()])
    assert "no handovers recorded" in out
    assert "Pointer handovers" not in out


def test_the_handovers_section_carries_every_figure_the_run_issue_gates_on():
    out = _capture(
        _args(name="PTR-01"),
        [
            _point(start_ns=1),
            _point(
                start_ns=2,
                op="transfer",
                outcome="ok",
                recipient="cd" * 32,
                finality_status="final",
                counter_expected=FINAL,
                counter_observed=None,
            ),
            _point(
                start_ns=3,
                op="transfer",
                outcome="read_shortfall",
                paid_attempts=0,
                read_back=None,
            ),
            _point(
                start_ns=4,
                op="finality",
                outcome="ok",
                address="ab" * 32,
                finality_status="final",
                answered=5,
                group_size=5,
                holders=5,
                age_secs=1200,
                paid_attempts=0,
                read_back=None,
            ),
            _point(
                start_ns=5,
                op="get",
                outcome="ok",
                counter_expected=FINAL,
                counter_observed=FINAL,
                paid_attempts=0,
                read_back=None,
            ),
            _point(start_ns=6, op="controller", outcome="ok", paid_attempts=0, read_back=None),
            _point(
                start_ns=7,
                op="takeback",
                outcome="ok",
                attempt="update",
                paid_attempts=0,
                ant_spend_atto=0,
                gas_spend_wei=0,
                read_back=None,
            ),
            _point(
                start_ns=8,
                op="takeback",
                outcome="anomaly",
                attempt="transfer",
                paid_attempts=1,
                read_back=None,
            ),
        ],
    )
    for figure in (
        "Pointer handovers",
        "Attempts",
        "Completed",
        "silent peer",
        "rival final",
        "Lost writes",
        "Handover latency p50 / p95",
        "Mean cost per handover",
        "Redirect reads ok",
        "Controller ok",
        "Resolve ok",
        # The title wraps to the (narrow) table width, so match a fragment.
        "Finality at the check",
        "Take-backs",
        "Accepted or spent",
    ):
        assert figure in out, figure
    # The silent-peer refusal is excluded from the completion denominator, so
    # one completed out of two attempts still reads 100%.
    assert "1/1 (100.00%)" in out
    assert "unconfirmed" in out and "forked" in out


def test_the_exclusion_of_the_silent_peer_refusals_is_stated_where_it_is_read():
    """A reader comparing `Attempts` with the completion denominator must not
    have to find the source to see why they differ."""
    out = _capture(
        _args(name="PTR-01"),
        [_point(op="transfer", outcome="read_shortfall", paid_attempts=0, read_back=None)],
    )
    assert "EXCLUDED from the completion denominator" in out


def test_by_age_adds_the_finality_table():
    out = _capture(
        _args(name="PTR-01", by_age=True),
        [
            _point(op="transfer", outcome="ok", start_ns=1, recipient="cd" * 32),
            _point(
                start_ns=2,
                op="finality",
                outcome="ok",
                owned=True,
                age_secs=60,
                paid_attempts=0,
                read_back=None,
            ),
            _point(
                start_ns=3,
                op="finality",
                outcome="stale",
                owned=False,
                age_secs=4000,
                paid_attempts=0,
                read_back=None,
            ),
        ],
    )
    assert "Finality by age since handover" in out
    assert "final %" in out
    # Own and foreign apart, and `open` in the CLI's vocabulary rather than the
    # `stale` outcome name.
    assert "own" in out and "foreign" in out
    assert "open" in out


def test_the_recorded_source_renders_the_handovers_section_too(repo):
    """The two sources go through the same summary, so a handover figure an
    operator reads live must be the same one that survives teardown."""
    deployment = repo.create(name="PTR-01", deploy_args={"kind": "testnet"})
    repo.upsert_pointer_record(
        deployment_id=deployment.id,
        client_hostname="PTR-1",
        region="lon1",
        op=PointerOp.TRANSFER,
        outcome=PointerOutcome.OK,
        start_ns=1,
        influx_time=T0,
        started_at=T0,
        finished_at=T0,
        duration_ms=5000,
        pointer_address="ab" * 32,
        owned=True,
        counter_expected=FINAL,
        attempts=1,
        paid_attempts=1,
        ant_spend_atto=5 * 10**15,
        gas_spend_wei=2 * 10**14,
        recipient="cd" * 32,
        finality_status="final",
    )
    repo.upsert_pointer_record(
        deployment_id=deployment.id,
        client_hostname="PTR-1",
        region="lon1",
        op=PointerOp.FINALITY,
        outcome=PointerOutcome.UNCONFIRMED,
        start_ns=2,
        influx_time=T0,
        started_at=T0,
        finished_at=T0,
        duration_ms=800,
        pointer_address="ab" * 32,
        owned=True,
        age_secs=1200,
        finality_status="unconfirmed",
        answered=4,
        group_size=5,
        holders=4,
    )
    console = Console(record=True, width=200)
    with patch("saorsa_deploy.cmd.print_pointers.Console", return_value=console):
        with patch("saorsa_deploy.cmd.print_pointers.get_repository", return_value=repo):
            cmd_print_pointers(_args(id=deployment.id, by_age=True))
    out = console.export_text()
    assert "Pointer handovers" in out
    assert "Finality by age since handover" in out
    # The unconfirmed reading is what `transfer` refuses to write into, so it
    # must be visible rather than folded into an error.
    assert "unconfirmed" in out
