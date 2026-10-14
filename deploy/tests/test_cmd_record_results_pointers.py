"""Pointer operations from Influx into Postgres, and out again to print-pointers.

`record-results` picks pointer records up in the SAME pass as the file-op
records, because a pointer client rides an ordinary testnet rather than being a
kind of its own. That is the point of these tests: a command an operator has to
remember separately is a command that does not get run, and the Influx points
expire while the rows do not.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.db import Base
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy.cmd.record_results import _record_pointer_ops
from saorsa_deploy.influx import parse_pointer_csv

T0 = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)

_CSV_HEADER = (
    ",result,table,_time,_measurement,host,op,region,address,age_secs,attempts,"
    "counter_expected,counter_observed,duration_ms,error,gas_spend_wei,ok,outcome,"
    "owned,paid_attempts,read_back,start_ns,target_kind,wallet_spend_atto"
)


def _csv(*rows: str) -> str:
    return "\n".join([_CSV_HEADER, *rows]) + "\n"


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyDeploymentRepository(session)


# ---------------------------------------------------------------------------
# Parsing the Influx CSV
# ---------------------------------------------------------------------------


def test_a_pointer_point_parses_with_every_field():
    body = _csv(
        ",_result,0,2026-09-26T10:00:00Z,pointer,PTR-1,update,lon1,"
        "abcd,3600,1,5,4,4200,,1000000000000000,0,stale,1,1,stale,"
        "1759000000000000000,chunk,5000000000000000000"
    )
    point = parse_pointer_csv(body)[0]
    assert point["op"] == "update"
    assert point["outcome"] == "stale"
    assert point["owned"] is True
    assert point["counter_expected"] == 5
    assert point["counter_observed"] == 4
    assert point["age_secs"] == 3600
    assert point["read_back"] == "stale"
    assert point["ant_spend_atto"] == 5_000_000_000_000_000_000
    assert point["gas_spend_wei"] == 1_000_000_000_000_000


def test_a_foreign_pointer_read_keeps_its_owned_flag():
    """The two populations are reported separately, because only for an owned
    pointer is the expected state known. A foreign read defaulting to `owned`
    would move it into a population it does not belong to."""
    body = _csv(
        ",_result,0,2026-09-26T10:00:00Z,pointer,PTR-1,sweep,lon1,"
        "abcd,7200,1,,9,900,,,1,ok,0,0,,1759000000000000000,chunk,"
    )
    assert parse_pointer_csv(body)[0]["owned"] is False


def test_a_missing_owned_field_reads_as_owned():
    """Never as foreign: that would silently stop an own-pointer read being
    checked against its expected state."""
    header = _CSV_HEADER.replace(",owned", "")
    body = (
        header + "\n" + ",_result,0,2026-09-26T10:00:00Z,pointer,PTR-1,sweep,lon1,abcd,60,1,"
        "1,1,900,,,1,ok,1,,1759000000000000000,chunk,\n"
    )
    assert parse_pointer_csv(body)[0]["owned"] is True


def test_a_point_with_no_host_or_op_is_skipped():
    """Both are tags and both are part of the row identity; a point missing
    either cannot be attributed to a client or an operation."""
    body = _csv(
        ",_result,0,2026-09-26T10:00:00Z,pointer,,sweep,lon1,a,1,1,1,1,1,,,1,ok,1,0,,1,chunk,",
        ",_result,0,2026-09-26T10:00:00Z,pointer,PTR-1,,lon1,a,1,1,1,1,1,,,1,ok,1,0,,1,chunk,",
    )
    assert parse_pointer_csv(body) == []


def test_another_measurement_is_ignored():
    body = _csv(
        ",_result,0,2026-09-26T10:00:00Z,upload,PTR-1,sweep,lon1,a,1,1,1,1,1,,,1,ok,1,0,,1,chunk,"
    )
    assert parse_pointer_csv(body) == []


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _record(repo, deployment, points, console=None):
    from rich.console import Console

    args = SimpleNamespace(name=deployment.name)
    with patch("saorsa_deploy.cmd.record_results.fetch_pointer_points", return_value=points):
        return _record_pointer_ops(
            repo,
            deployment,
            args,
            console or Console(),
            "http://influx",
            "token",
            "ant",
            "testnet-metrics",
        )


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
        "ant_spend_atto": 5 * 10**18,
        "gas_spend_wei": 10**15,
        "duration_ms": 4200,
        "start_ns": 1_759_000_000_000_000_000,
        "error": None,
        "_time": T0,
    }
    point.update(extra)
    return point


def test_the_network_profile_is_denormalised_onto_the_row(repo):
    """So a shaped client's pointer ops can be filtered without joining through
    deploy_args JSON, exactly as for the file-op tables."""
    deployment = repo.create(
        name="PTR-01",
        deploy_args={
            "kind": "testnet",
            "network_by_host": {"PTR-1": "home-fttc"},
        },
    )
    _record(repo, deployment, [_point()])
    rows = repo.list_pointer_records_for_deployment(deployment.id)
    assert rows[0].network_profile == "home-fttc"


# ---------------------------------------------------------------------------
# Handover ops (V2-1354)
# ---------------------------------------------------------------------------

_HANDOVER_HEADER = (
    ",result,table,_time,_measurement,host,op,region,address,recipient,"
    "finality_status,answered,group,holders,attempt,counter_expected_u64,"
    "counter_observed_u64,duration_ms,error,gas_spend_wei,ok,outcome,owned,"
    "paid_attempts,start_ns,target_kind,wallet_spend_atto"
)

_FINAL = 18446744073709551615


def _handover_csv(*rows: str) -> str:
    return "\n".join([_HANDOVER_HEADER, *rows]) + "\n"


def test_a_transfer_point_carries_the_handover_fields():
    body = _handover_csv(
        ",_result,0,2026-09-26T10:00:00Z,pointer,PTR-1,transfer,lon1,"
        f"abcd,efgh,final,,,,,{_FINAL},,5200,,1000000000000000,1,ok,1,1,"
        "1759000000000000000,pointer,5000000000000000000"
    )
    point = parse_pointer_csv(body)[0]
    assert point["op"] == "transfer"
    assert point["recipient"] == "efgh"
    assert point["finality_status"] == "final"
    # `u64::MAX` cannot be an Influx integer field, so it arrives as a string
    # under its own name and is folded back onto the one key here.
    assert point["counter_expected"] == _FINAL


def test_a_finality_point_carries_the_close_group_shape():
    body = _handover_csv(
        ",_result,0,2026-09-26T10:00:00Z,pointer,PTR-1,finality,lon1,"
        "abcd,,unconfirmed,4,5,4,,,,800,,,0,unconfirmed,1,0,"
        "1759000000000000000,,"
    )
    point = parse_pointer_csv(body)[0]
    assert point["outcome"] == "unconfirmed"
    assert (point["answered"], point["group_size"], point["holders"]) == (4, 5, 4)


def test_the_handover_ops_and_outcomes_are_recorded_rather_than_counted_unusable(repo):
    """The `unusable` counter is for a value this build does not understand. An
    op it DOES understand must land in the table, or the gate reads NOT ASSESSED
    on a run that measured handovers."""
    deployment = repo.create(name="PTR-01", deploy_args={"kind": "testnet"})
    points = [
        {
            "_time": T0,
            "host": "PTR-1",
            "region": "lon1",
            "op": op,
            "outcome": outcome,
            "owned": True,
            "address": "ab" * 32,
            "start_ns": 100 + i,
            "duration_ms": 900,
            "recipient": "cd" * 32,
            "finality_status": status,
            "answered": 5,
            "group_size": 5,
            "holders": 5,
            "attempt": attempt,
            "counter_expected": _FINAL,
        }
        for i, (op, outcome, status, attempt) in enumerate(
            [
                ("transfer", "ok", "final", None),
                ("finality", "settling", "settling", None),
                ("finality", "unconfirmed", "unconfirmed", None),
                ("finality", "forked", "forked", None),
                ("controller", "ok", None, None),
                ("takeback", "ok", None, "update"),
                ("takeback", "anomaly", None, "transfer"),
            ]
        )
    ]
    console = SimpleNamespace(print=lambda *_, **__: None)
    with patch("saorsa_deploy.cmd.record_results.fetch_pointer_points", return_value=points):
        _record_pointer_ops(
            repo,
            deployment,
            SimpleNamespace(name="PTR-01"),
            console,
            "http://influx",
            "token",
            "ant",
            "testnet-metrics",
        )
    rows = repo.list_pointer_records_for_deployment(deployment.id)
    assert len(rows) == len(points)
    by_op = {}
    for row in rows:
        by_op.setdefault(row.op.value, []).append(row)
    assert set(by_op) == {"transfer", "finality", "controller", "takeback"}
    transfer = by_op["transfer"][0]
    assert transfer.recipient == "cd" * 32
    assert transfer.finality_status == "final"
    assert {r.outcome.value for r in by_op["finality"]} == {
        "settling",
        "unconfirmed",
        "forked",
    }
    assert {r.attempt for r in by_op["takeback"]} == {"update", "transfer"}


def test_the_counter_column_can_hold_a_handover_counter():
    """`u64::MAX` is about twice a signed BIGINT, so the column is NUMERIC(20).

    Asserted on the COLUMN TYPE rather than by writing the value and reading it
    back, because the SQLite fixture has no exact numeric: SQLAlchemy converts
    through a float there, and 18446744073709551615 comes back as
    18446744073709551616. Postgres NUMERIC(20) is exact, so a round-trip test
    here would be testing the fixture.
    """
    from saorsa_db.models.pointer_record import DeploymentPointerRecord
    from sqlalchemy import Numeric

    for name in ("counter_expected", "counter_observed"):
        column = DeploymentPointerRecord.__table__.c[name]
        assert isinstance(column.type, Numeric), name
        assert column.type.precision >= 20, name


def test_the_recorded_counter_is_the_value_the_point_carried():
    """The hop record-results is responsible for, asserted on what it PASSES to
    the repository rather than on what the fixture stores — see above."""
    captured = {}

    class _Repo:
        def upsert_pointer_record(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(**kwargs)

    point = {
        "_time": T0,
        "host": "PTR-1",
        "region": "lon1",
        "op": "get",
        "outcome": "ok",
        "owned": True,
        "address": "ab" * 32,
        "start_ns": 1,
        "duration_ms": 900,
        "recipient": "cd" * 32,
        "counter_expected": _FINAL,
        "counter_observed": _FINAL,
    }
    console = SimpleNamespace(print=lambda *_, **__: None)
    with patch("saorsa_deploy.cmd.record_results.fetch_pointer_points", return_value=[point]):
        _record_pointer_ops(
            _Repo(),
            SimpleNamespace(id=1, deploy_args={}),
            SimpleNamespace(name="PTR-01"),
            console,
            "http://influx",
            "token",
            "ant",
            "testnet-metrics",
        )
    assert captured["counter_expected"] == _FINAL
    assert captured["counter_observed"] == _FINAL
    assert captured["recipient"] == "cd" * 32
