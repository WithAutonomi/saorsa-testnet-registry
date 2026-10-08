from datetime import datetime, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.models import FileOpType, OpRecordStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy.influx import (
    _parse_pivoted_csv,
    aggregate_file_results,
)

CSV_SAMPLE = (
    ",result,table,_start,_stop,_time,host,region,_measurement,file_size,start_ns,status,total_ms\n"
    ",_result,0,2026-03-21T19:14:29Z,2026-04-20T19:14:29Z,2026-04-19T16:19:36.708970856Z,"
    "DEV-03-ant-client-lon1-1,DEV-03-ant-client-lon1-1,upload,52428800,1776615435704524798,0,141004\n"
    ",_result,0,2026-03-21T19:14:29Z,2026-04-20T19:14:29Z,2026-04-19T16:23:30.514719517Z,"
    "DEV-03-ant-client-lon1-1,DEV-03-ant-client-lon1-1,upload,52428800,1776615578106728129,0,232407\n"
    ",_result,0,2026-03-21T19:14:29Z,2026-04-20T19:14:29Z,2026-04-19T16:28:16.369498974Z,"
    "DEV-03-ant-client-lon1-1,DEV-03-ant-client-lon1-1,upload,52428800,1776615972840960725,1,123528\n"
    ",_result,0,2026-03-21T19:14:29Z,2026-04-20T19:14:29Z,2026-04-19T16:22:43.741025023Z,"
    "DEV-03-ant-client-lon1-1,DEV-03-ant-client-lon1-1,download,52428800,1776615585480357833,0,178260\n"
)


def test_parse_pivoted_csv_extracts_operation_points():
    points = _parse_pivoted_csv(CSV_SAMPLE)
    assert len(points) == 4
    p = points[0]
    assert p["_measurement"] == "upload"
    assert p["host"] == "DEV-03-ant-client-lon1-1"
    assert p["file_size"] == 52428800
    assert p["status"] == 0
    assert p["total_ms"] == 141004
    assert p["_time"].tzinfo is not None


def test_parse_pivoted_csv_skips_rows_without_host():
    body = (
        ",result,table,_time,host,_measurement,file_size,start_ns,status,total_ms\n"
        ",_result,0,2026-04-19T16:19:36Z,,upload,52428800,1,0,100\n"
    )
    assert _parse_pivoted_csv(body) == []


def test_parse_pivoted_csv_ignores_non_op_measurements():
    body = (
        ",result,table,_time,host,_measurement,_value,_field\n"
        ",_result,0,2026-04-19T16:19:36Z,hostA,cpu,0.5,usage\n"
    )
    assert _parse_pivoted_csv(body) == []


def _pt(measurement, host, file_size, status, total_ms, ts_iso, start_ns):
    return {
        "_time": datetime.fromisoformat(ts_iso).astimezone(timezone.utc),
        "host": host,
        "_measurement": measurement,
        "file_size": file_size,
        "start_ns": start_ns,
        "status": status,
        "total_ms": total_ms,
    }


def test_aggregate_file_results_groups_and_computes_stats():
    points = [
        _pt("upload", "hostA", 1024, 0, 100, "2026-04-19T10:00:00+00:00", 1_000),
        _pt("upload", "hostA", 1024, 0, 200, "2026-04-19T10:01:00+00:00", 2_000),
        _pt("upload", "hostA", 1024, 1, 999, "2026-04-19T10:02:00+00:00", 3_000),
        _pt("download", "hostA", 1024, 0, 300, "2026-04-19T10:03:00+00:00", 4_000),
        _pt("upload", "hostB", 2048, 1, 500, "2026-04-19T10:04:00+00:00", 5_000),
    ]
    aggs = aggregate_file_results(points)
    assert len(aggs) == 3

    a = next(x for x in aggs if x["client_hostname"] == "hostA" and x["op_type"] == "upload")
    assert a["file_size_bytes"] == 1024
    assert a["success_count"] == 2
    assert a["failure_count"] == 1
    assert a["avg_duration_ms"] == 150  # (100+200)//2, failed one excluded
    assert a["min_duration_ms"] == 100
    assert a["max_duration_ms"] == 200
    assert a["first_started_at"] == datetime.fromtimestamp(1_000 / 1e9, tz=timezone.utc)
    assert a["last_finished_at"] == datetime.fromisoformat("2026-04-19T10:02:00+00:00")

    d = next(x for x in aggs if x["op_type"] == "download")
    assert d["success_count"] == 1 and d["failure_count"] == 0
    assert d["avg_duration_ms"] == 300

    b = next(x for x in aggs if x["client_hostname"] == "hostB")
    assert b["success_count"] == 0
    assert b["failure_count"] == 1
    assert b["avg_duration_ms"] is None
    assert b["min_duration_ms"] is None
    assert b["max_duration_ms"] is None


def test_aggregate_file_results_skips_points_without_file_size():
    points = [
        _pt("upload", "hostA", None, 0, 100, "2026-04-19T10:00:00+00:00", 1_000),
    ]
    assert aggregate_file_results(points) == []


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session), session
    session.close()
    engine.dispose()


def test_upsert_file_result_inserts_then_updates(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})

    r.upsert_file_result(
        deployment_id=deployment.id,
        client_hostname="hostA",
        op_type=FileOpType.UPLOAD,
        file_size_bytes=1024,
        first_started_at=datetime.fromisoformat("2026-04-19T10:00:00+00:00"),
        last_finished_at=datetime.fromisoformat("2026-04-19T10:05:00+00:00"),
        success_count=2,
        failure_count=0,
        avg_duration_ms=150,
        min_duration_ms=100,
        max_duration_ms=200,
    )
    rows = r.list_file_results_for_deployment(deployment.id)
    assert len(rows) == 1
    assert rows[0].success_count == 2

    r.upsert_file_result(
        deployment_id=deployment.id,
        client_hostname="hostA",
        op_type=FileOpType.UPLOAD,
        file_size_bytes=1024,
        first_started_at=datetime.fromisoformat("2026-04-19T10:00:00+00:00"),
        last_finished_at=datetime.fromisoformat("2026-04-19T11:00:00+00:00"),
        success_count=5,
        failure_count=1,
        avg_duration_ms=170,
        min_duration_ms=90,
        max_duration_ms=250,
    )
    rows = r.list_file_results_for_deployment(deployment.id)
    assert len(rows) == 1
    assert rows[0].success_count == 5
    assert rows[0].failure_count == 1
    assert rows[0].max_duration_ms == 250


def test_upsert_file_result_distinct_rows_per_op_and_size(repo):
    r, _ = repo
    deployment = r.create(name="DEV-TEST", deploy_args={})
    common = dict(
        deployment_id=deployment.id,
        client_hostname="hostA",
        first_started_at=datetime.fromisoformat("2026-04-19T10:00:00+00:00"),
        last_finished_at=datetime.fromisoformat("2026-04-19T10:05:00+00:00"),
        success_count=1,
        failure_count=0,
        avg_duration_ms=100,
        min_duration_ms=100,
        max_duration_ms=100,
    )
    r.upsert_file_result(op_type=FileOpType.UPLOAD, file_size_bytes=1024, **common)
    r.upsert_file_result(op_type=FileOpType.DOWNLOAD, file_size_bytes=1024, **common)
    r.upsert_file_result(op_type=FileOpType.UPLOAD, file_size_bytes=2048, **common)

    rows = r.list_file_results_for_deployment(deployment.id)
    assert len(rows) == 3


def _upload_kwargs(deployment_id, **overrides):
    base = dict(
        deployment_id=deployment_id,
        client_hostname="hostA",
        region="lon1",
        file_size_bytes=1024,
        status=OpRecordStatus.SUCCESS,
        start_ns=1_000_000_000_000,
        influx_time=datetime.fromisoformat("2026-04-19T10:00:00+00:00"),
        started_at=datetime.fromisoformat("2026-04-19T10:00:00+00:00"),
        finished_at=datetime.fromisoformat("2026-04-19T10:00:01+00:00"),
        duration_ms=1000,
    )
    base.update(overrides)
    return base


def test_upsert_upload_record_inserts_then_updates(repo):
    r, _ = repo
    d = r.create(name="DEV-TEST", deploy_args={})

    r.upsert_upload_record(**_upload_kwargs(d.id))
    rows = r.list_upload_records_for_deployment(d.id)
    assert len(rows) == 1 and rows[0].status == OpRecordStatus.SUCCESS

    r.upsert_upload_record(
        **_upload_kwargs(
            d.id,
            status=OpRecordStatus.FAILURE,
            duration_ms=42,
            finished_at=None,
        )
    )
    rows = r.list_upload_records_for_deployment(d.id)
    assert len(rows) == 1
    assert rows[0].status == OpRecordStatus.FAILURE
    assert rows[0].duration_ms == 42
    assert rows[0].finished_at is None


def test_upsert_upload_record_distinct_per_start_ns(repo):
    r, _ = repo
    d = r.create(name="DEV-TEST", deploy_args={})
    r.upsert_upload_record(**_upload_kwargs(d.id, start_ns=1))
    r.upsert_upload_record(**_upload_kwargs(d.id, start_ns=2))
    r.upsert_upload_record(**_upload_kwargs(d.id, start_ns=3, client_hostname="hostB"))
    assert len(r.list_upload_records_for_deployment(d.id)) == 3


def test_upload_and_download_records_are_separate_tables(repo):
    r, _ = repo
    d = r.create(name="DEV-TEST", deploy_args={})
    r.upsert_upload_record(**_upload_kwargs(d.id, start_ns=100))
    r.upsert_download_record(**_upload_kwargs(d.id, start_ns=100))
    assert len(r.list_upload_records_for_deployment(d.id)) == 1
    assert len(r.list_download_records_for_deployment(d.id)) == 1


# ---------------------------------------------------------------------------
# Content verification (V2-757)
# ---------------------------------------------------------------------------


def test_a_rerecord_without_the_fields_never_erases_a_recorded_verdict(repo):
    # The same COALESCE rule `client_kind` and `address` follow. A re-run
    # against a source that has lost the fields must not turn a recorded
    # mismatch into an unverified row -- that would silently retract the only
    # evidence the integrity gate has.
    r, _ = repo
    d = r.create(name="DEV-TEST", deploy_args={})
    r.upsert_download_record(
        **_upload_kwargs(d.id, start_ns=7),
        verify_status="mismatch",
        expected_sha256="a" * 64,
        actual_sha256="b" * 64,
    )
    r.upsert_download_record(**_upload_kwargs(d.id, start_ns=7))
    row = r.list_download_records_for_deployment(d.id)[0]
    assert row.verify_status == "mismatch"
    assert row.expected_sha256 == "a" * 64
    assert row.actual_sha256 == "b" * 64


def test_pivoted_csv_parses_the_verification_fields():
    from saorsa_deploy.influx import _parse_pivoted_csv

    csv_body = (
        ",result,table,_time,_measurement,host,region,file_size,start_ns,status,"
        "total_ms,verify_status,expected_sha256,actual_sha256\n"
        ",_result,0,2026-04-19T10:00:01Z,download,hostA,lon1,1024,5,0,900,1,"
        f"{'a' * 64},{'b' * 64}\n"
    )
    point = _parse_pivoted_csv(csv_body)[0]
    assert point["verify_status"] == 1
    assert point["expected_sha256"] == "a" * 64
    assert point["actual_sha256"] == "b" * 64


def test_pivoted_csv_leaves_verification_absent_when_unwritten():
    from saorsa_deploy.influx import _parse_pivoted_csv

    csv_body = (
        ",result,table,_time,_measurement,host,region,file_size,start_ns,status,"
        "total_ms\n"
        ",_result,0,2026-04-19T10:00:01Z,download,hostA,lon1,1024,5,0,900\n"
    )
    point = _parse_pivoted_csv(csv_body)[0]
    assert point["verify_status"] is None
    assert point["sha256"] is None
