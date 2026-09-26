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
from saorsa_db.models import PointerOp, PointerOutcome
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


def test_every_pointer_point_becomes_a_row(repo):
    deployment = repo.create(name="PTR-01", deploy_args={"kind": "testnet"})
    blocks = _record(repo, deployment, [_point(), _point(start_ns=2, op="sweep")])
    rows = repo.list_pointer_records_for_deployment(deployment.id)
    assert len(rows) == 2
    assert {r.op for r in rows} == {PointerOp.CREATE, PointerOp.SWEEP}
    assert rows[0].outcome is PointerOutcome.OK
    # The Slack reply blocks come back so `--update-slack` can append them.
    assert blocks


def test_a_point_with_no_start_ns_is_unusable_not_coerced(repo):
    """`start_ns` is half the upsert key. A row without it could not be revised
    on a re-run, so it would duplicate on every one."""
    deployment = repo.create(name="PTR-01", deploy_args={"kind": "testnet"})
    _record(repo, deployment, [_point(start_ns=None)])
    assert repo.list_pointer_records_for_deployment(deployment.id) == []


def test_an_unrecognised_outcome_is_not_written_as_error(repo):
    """Coercing it would put a value this build does not understand into the
    column the gates read, and `error` is a claim about the network."""
    deployment = repo.create(name="PTR-01", deploy_args={"kind": "testnet"})
    _record(repo, deployment, [_point(outcome="teleported")])
    assert repo.list_pointer_records_for_deployment(deployment.id) == []


def test_a_query_failure_warns_rather_than_failing_the_recording(repo):
    """The file-op records are the gated measurement and are already written by
    the time this runs. A pointer query that fails must not lose them."""
    from rich.console import Console

    deployment = repo.create(name="PTR-01", deploy_args={"kind": "testnet"})
    console = Console()
    args = SimpleNamespace(name="PTR-01")
    with patch(
        "saorsa_deploy.cmd.record_results.fetch_pointer_points",
        side_effect=RuntimeError("influx down"),
    ):
        assert _record_pointer_ops(repo, deployment, args, console, "u", "t", "o", "b") is None


def test_a_run_with_no_pointer_clients_records_nothing_and_says_nothing(repo):
    deployment = repo.create(name="STG-01", deploy_args={"kind": "testnet"})
    assert _record(repo, deployment, []) is None
    assert repo.list_pointer_records_for_deployment(deployment.id) == []


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
