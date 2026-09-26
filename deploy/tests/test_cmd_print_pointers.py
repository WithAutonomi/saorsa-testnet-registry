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
