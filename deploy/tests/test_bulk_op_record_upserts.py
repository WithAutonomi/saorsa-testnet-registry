"""The four per-operation tables are written in bulk, and still idempotent.

`record-results` writes one row per client operation, and a staging-sized run
has tens of thousands of them. Written one at a time — a SELECT, an INSERT or
UPDATE and a `commit()` each — DEV-01's (registry id 685) second pass spent
38 minutes on 18,691 rows, about 100 ms a row against the managed Postgres and
linear in the row count. Each table is now one `INSERT ... ON CONFLICT` per
batch and one commit.

The cost of that is that the upsert's semantics move out of Python and into a
`SET` clause, so what these tests pin is that the clause still says what the
single-row method said: which columns a second pass overwrites, which ones it
COALESCEs so a re-run that resolved None cannot erase them, and that a batch
carrying the same key twice leaves the last one — a multi-row `ON CONFLICT`
cannot touch one row twice, so the duplicates have to be collapsed before the
statement, keeping exactly what a sequence of single-row upserts would have
left.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.db import Base
from saorsa_db.models import OpRecordStatus
from saorsa_db.models.op_record import DeploymentDownloadRecord, DeploymentUploadRecord
from saorsa_db.models.pointer_record import (
    DeploymentPointerRecord,
    PointerOp,
    PointerOutcome,
)
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

T0 = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


def _deployment(repo, name="DEV-BULK"):
    return repo.create(name=name, deploy_args={"kind": "testnet"})


def _op_row(deployment_id, start_ns=1, **extra):
    row = {
        "deployment_id": deployment_id,
        "client_hostname": "hostA",
        "region": "lon1",
        "file_size_bytes": 1024,
        "status": OpRecordStatus.SUCCESS,
        "start_ns": start_ns,
        "influx_time": T0,
        "started_at": T0,
        "finished_at": T0,
        "duration_ms": 100,
        "network_profile": None,
        "client_kind": None,
    }
    row.update(extra)
    return row


def _pointer_row(deployment_id, start_ns=1, **extra):
    row = {
        "deployment_id": deployment_id,
        "client_hostname": "PTR-1",
        "region": "lon1",
        "op": PointerOp.UPDATE,
        "outcome": PointerOutcome.OK,
        "start_ns": start_ns,
        "influx_time": T0,
        "started_at": T0,
        "finished_at": T0,
        "duration_ms": 900,
        "pointer_address": "ab" * 32,
        "owned": True,
    }
    row.update(extra)
    return row


def _browser_row(deployment_id, round_id=1, instance=0, **extra):
    row = {
        "deployment_id": deployment_id,
        "client_hostname": "DEV-BULK-ant-client-browser-download-lon1-1",
        "round_id": round_id,
        "instance": instance,
        "status": OpRecordStatus.SUCCESS,
        "started_at": T0,
        "region": "lon1",
        "total_ms": 9120,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Insert, update, no duplicates
# ---------------------------------------------------------------------------


def test_upload_records_insert_then_update_in_bulk(repo):
    d = _deployment(repo)
    assert repo.upsert_upload_records([_op_row(d.id, n) for n in (1, 2, 3)]) == 3
    assert len(repo.list_upload_records_for_deployment(d.id)) == 3

    assert (
        repo.upsert_upload_records(
            [_op_row(d.id, n, status=OpRecordStatus.FAILURE, duration_ms=42) for n in (1, 2, 3)]
        )
        == 3
    )
    rows = repo.list_upload_records_for_deployment(d.id)
    assert len(rows) == 3
    assert {r.status for r in rows} == {OpRecordStatus.FAILURE}
    assert {r.duration_ms for r in rows} == {42}


def test_download_records_insert_then_update_in_bulk(repo):
    d = _deployment(repo)
    assert repo.upsert_download_records([_op_row(d.id, n) for n in (1, 2)]) == 2
    repo.upsert_download_records([_op_row(d.id, 1, region="nyc3")])
    rows = repo.list_download_records_for_deployment(d.id)
    assert len(rows) == 2
    assert rows[0].region == "nyc3"


def test_pointer_records_insert_then_update_in_bulk(repo):
    d = _deployment(repo)
    assert repo.upsert_pointer_records([_pointer_row(d.id, n) for n in (1, 2)]) == 2
    repo.upsert_pointer_records(
        [_pointer_row(d.id, 1, outcome=PointerOutcome.STALE, read_back="stale")]
    )
    rows = repo.list_pointer_records_for_deployment(d.id)
    assert len(rows) == 2
    assert rows[0].outcome == PointerOutcome.STALE
    assert rows[0].read_back == "stale"


def test_browser_records_insert_then_update_in_bulk(repo):
    d = _deployment(repo)
    assert repo.upsert_browser_download_records([_browser_row(d.id, 1, i) for i in (0, 1)]) == 2
    repo.upsert_browser_download_records(
        [_browser_row(d.id, 1, 0, status=OpRecordStatus.FAILURE, error_kind="timeout")]
    )
    rows = repo.list_browser_download_records_for_deployment(d.id)
    assert len(rows) == 2
    changed = next(r for r in rows if r.instance == 0)
    assert changed.status == OpRecordStatus.FAILURE
    assert changed.error_kind == "timeout"


def test_an_empty_batch_writes_nothing(repo):
    d = _deployment(repo)
    assert repo.upsert_upload_records([]) == 0
    assert repo.upsert_download_records([]) == 0
    assert repo.upsert_pointer_records([]) == 0
    assert repo.upsert_browser_download_records([]) == 0
    assert repo.list_upload_records_for_deployment(d.id) == []


# ---------------------------------------------------------------------------
# Preserve-on-None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column,first,second",
    [
        ("client_kind", "wasm", "native"),
        ("sha256", "a" * 64, "b" * 64),
    ],
)
def test_an_upload_records_preserved_column_survives_a_none(repo, column, first, second):
    d = _deployment(repo)
    repo.upsert_upload_records([_op_row(d.id, **{column: first})])

    repo.upsert_upload_records([_op_row(d.id, **{column: None})])
    assert getattr(repo.list_upload_records_for_deployment(d.id)[0], column) == first

    repo.upsert_upload_records([_op_row(d.id, **{column: second})])
    assert getattr(repo.list_upload_records_for_deployment(d.id)[0], column) == second


@pytest.mark.parametrize(
    "column,first,second",
    [
        ("client_kind", "wasm", "native"),
        ("address", "ab" * 32, "cd" * 32),
        ("client_build", "deadbee", "f00dcaf"),
        ("verify_status", "ok", "mismatch"),
        ("expected_sha256", "a" * 64, "b" * 64),
        ("actual_sha256", "c" * 64, "d" * 64),
    ],
)
def test_a_download_records_preserved_column_survives_a_none(repo, column, first, second):
    d = _deployment(repo)
    repo.upsert_download_records([_op_row(d.id, **{column: first})])

    repo.upsert_download_records([_op_row(d.id, **{column: None})])
    assert getattr(repo.list_download_records_for_deployment(d.id)[0], column) == first

    repo.upsert_download_records([_op_row(d.id, **{column: second})])
    assert getattr(repo.list_download_records_for_deployment(d.id)[0], column) == second


@pytest.mark.parametrize("column,first,second", [("extension", "browsers", "more-browsers")])
def test_a_browser_records_preserved_column_survives_a_none(repo, column, first, second):
    d = _deployment(repo)
    repo.upsert_browser_download_records([_browser_row(d.id, **{column: first})])

    repo.upsert_browser_download_records([_browser_row(d.id, **{column: None})])
    assert getattr(repo.list_browser_download_records_for_deployment(d.id)[0], column) == first

    repo.upsert_browser_download_records([_browser_row(d.id, **{column: second})])
    assert getattr(repo.list_browser_download_records_for_deployment(d.id)[0], column) == second


def test_extension_is_the_only_browser_column_that_coalesces(repo):
    """`trace_s3_key` COALESCEd here until V2-1650 dropped it with the archive it
    pointed at. Asserted on the tuple, because a stale name in it would have the
    statement COALESCE a column the table no longer has."""
    assert type(repo)._BROWSER_RECORD_COALESCE_COLUMNS == ("extension",)


def test_every_pointer_column_is_overwritten(repo):
    """A pointer row COALESCEs nothing: every column comes off the Influx point,
    so a second pass reading the same point writes the same value."""
    d = _deployment(repo)
    repo.upsert_pointer_records([_pointer_row(d.id, recipient="cd" * 32, attempts=3)])
    repo.upsert_pointer_records([_pointer_row(d.id, recipient=None, attempts=None)])
    row = repo.list_pointer_records_for_deployment(d.id)[0]
    assert row.recipient is None
    assert row.attempts is None


# ---------------------------------------------------------------------------
# A duplicate key within one batch
# ---------------------------------------------------------------------------


def test_a_duplicate_key_within_a_batch_keeps_the_last(repo):
    d = _deployment(repo)
    repo.upsert_upload_records(
        [
            _op_row(d.id, 1, duration_ms=1),
            _op_row(d.id, 1, duration_ms=2),
            _op_row(d.id, 1, duration_ms=3),
        ]
    )
    rows = repo.list_upload_records_for_deployment(d.id)
    assert len(rows) == 1
    assert rows[0].duration_ms == 3


def test_a_duplicate_key_within_a_batch_keeps_the_last_browser_row(repo):
    d = _deployment(repo)
    repo.upsert_browser_download_records(
        [
            _browser_row(d.id, total_ms=1),
            _browser_row(d.id, total_ms=2),
        ]
    )
    rows = repo.list_browser_download_records_for_deployment(d.id)
    assert len(rows) == 1
    assert rows[0].total_ms == 2


def test_one_statement_per_chunk_reaches_the_database(repo):
    """The regression this guards is a 20x one, and it is invisible in a test
    that only checks the rows.

    SQLAlchemy 2.0 turns off its multi-VALUES batching whenever the INSERT
    carries a post-values clause — which ON CONFLICT is (``sql/crud.py``,
    upstream #13130) — so handing the rows to ``Session.execute`` as a
    parameter set falls through to the driver's ``executemany``, which is a
    round trip PER ROW. Measured against the registry that is 19 ms a row and
    an 18,000-row pass takes 6 minutes; rendering the rows INTO the statement
    makes the same pass 17 s. So what has to be pinned is the statement count,
    not the row count.
    """
    from sqlalchemy import event

    executions = []
    engine = repo.session.get_bind()

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        executions.append(executemany)

    d = _deployment(repo)
    executions.clear()
    repo.upsert_upload_records([_op_row(d.id, n) for n in range(1, 301)])
    inserts = [e for e in executions if e is not None]
    assert len(inserts) == 1, f"expected one statement, got {len(inserts)}"
    assert not any(inserts), "the rows must be in the statement, not an executemany"
    event.remove(engine, "before_cursor_execute", _count)


def test_a_wide_table_narrows_the_chunk_to_stay_under_the_bind_limit():
    """The rows are rendered into the statement, so the ceiling is Postgres's
    65,535 bind parameters (and SQLite's 32,766), not a row count — and a
    column added to one of these tables must narrow the chunk rather than
    fail the flush."""
    from saorsa_db.repositories import (
        UPSERT_CHUNK_SIZE,
        UPSERT_MAX_BIND_PARAMS,
        _upsert_chunks,
    )

    narrow = [dict.fromkeys(range(2)) for _ in range(2 * UPSERT_CHUNK_SIZE)]
    assert [len(c) for c in _upsert_chunks(narrow)] == [UPSERT_CHUNK_SIZE, UPSERT_CHUNK_SIZE]

    wide_columns = 200
    wide = [dict.fromkeys(range(wide_columns)) for _ in range(500)]
    per_chunk = UPSERT_MAX_BIND_PARAMS // wide_columns
    assert per_chunk < UPSERT_CHUNK_SIZE
    sizes = [len(c) for c in _upsert_chunks(wide)]
    assert max(sizes) == per_chunk
    assert sum(sizes) == 500
    assert max(sizes) * wide_columns <= UPSERT_MAX_BIND_PARAMS


def test_a_batch_larger_than_one_statement_is_written_whole(repo):
    """`UPSERT_CHUNK_SIZE` rows per statement, so a pass over a staging-sized
    run is a handful of statements rather than one per row."""
    from saorsa_db.repositories import UPSERT_CHUNK_SIZE

    d = _deployment(repo)
    count = UPSERT_CHUNK_SIZE + 7
    assert repo.upsert_upload_records([_op_row(d.id, n) for n in range(1, count + 1)]) == count
    assert len(repo.list_upload_records_for_deployment(d.id)) == count


# ---------------------------------------------------------------------------
# The statement itself, on the dialect that matters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,key_columns,coalesced",
    [
        (
            DeploymentUploadRecord,
            ("deployment_id", "client_hostname", "start_ns"),
            ("client_kind", "sha256"),
        ),
        (
            DeploymentDownloadRecord,
            ("deployment_id", "client_hostname", "start_ns"),
            ("client_kind", "address", "client_build", "verify_status"),
        ),
        (
            DeploymentPointerRecord,
            ("deployment_id", "client_hostname", "start_ns"),
            (),
        ),
    ],
)
def test_the_postgres_statement_is_an_on_conflict_upsert(model, key_columns, coalesced):
    """Production is Postgres and the fixtures are SQLite, so the statement the
    registry actually runs is never exercised by the tests above. Compiling it
    against the Postgres dialect is what pins the conflict target and the
    COALESCEs there too, without a live database."""
    from sqlalchemy.dialects import postgresql

    stmt = postgresql.insert(model.__table__)
    stmt = stmt.on_conflict_do_update(
        index_elements=list(key_columns),
        set_={"region": stmt.excluded.region},
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql
    for column in key_columns:
        assert column in sql

    # And the repository's own column sets are the ones the SET clause is built
    # from, so a column added to a model without a decision about it fails here.
    repository_coalesced = {
        DeploymentUploadRecord: SqlAlchemyDeploymentRepository._UPLOAD_RECORD_COALESCE_COLUMNS,
        DeploymentDownloadRecord: (
            SqlAlchemyDeploymentRepository._DOWNLOAD_RECORD_COALESCE_COLUMNS
        ),
        DeploymentPointerRecord: (),
    }[model]
    for column in coalesced:
        assert column in repository_coalesced


def test_the_postgres_statement_coalesces_the_preserved_columns():
    from sqlalchemy import func
    from sqlalchemy.dialects import postgresql

    stmt = postgresql.insert(DeploymentDownloadRecord.__table__)
    stmt = stmt.on_conflict_do_update(
        index_elements=["deployment_id", "client_hostname", "start_ns"],
        set_={
            "client_kind": func.coalesce(
                stmt.excluded.client_kind, DeploymentDownloadRecord.client_kind
            )
        },
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "coalesce" in sql.lower()
    assert "excluded.client_kind" in sql


def test_an_unsupported_dialect_is_refused_rather_than_silently_slow():
    """A dialect without ON CONFLICT cannot express an upsert as one statement,
    and falling back to a read-then-write per row would reintroduce exactly the
    cost these methods exist to remove."""
    from saorsa_db.repositories import _dialect_insert

    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    )
    with pytest.raises(NotImplementedError, match="ON CONFLICT"):
        _dialect_insert(session)


# ---------------------------------------------------------------------------
# record-results calls each bulk method once per table
# ---------------------------------------------------------------------------


class _CountingRepo:
    """Counts the calls so a regression to a per-row loop is a failed test, not
    a slow run nobody notices until the next staging train."""

    def __init__(self):
        self.calls: dict[str, list[list[dict]]] = {}

    def _record(self, name, rows):
        self.calls.setdefault(name, []).append(list(rows))
        return len(rows)

    def upsert_upload_records(self, rows):
        return self._record("upload", rows)

    def upsert_download_records(self, rows):
        return self._record("download", rows)

    def upsert_pointer_records(self, rows):
        return self._record("pointer", rows)

    def upsert_browser_download_records(self, rows):
        return self._record("browser", rows)


def _silent_console():
    return SimpleNamespace(print=lambda *_, **__: None)


def test_record_file_op_records_calls_each_bulk_method_once():
    from saorsa_deploy.cmd.record_results import _record_file_op_records

    def _point(measurement, start_ns):
        return {
            "_time": T0,
            "host": "hostA",
            "region": "lon1",
            "_measurement": measurement,
            "file_size": 1024,
            "start_ns": start_ns,
            "status": 0,
            "total_ms": 100,
        }

    points = [_point("upload", n) for n in (1, 2, 3)] + [_point("download", n) for n in (4, 5)]
    repo = _CountingRepo()
    with (
        patch("saorsa_deploy.cmd.record_results._fetch_all_op_points", return_value=points),
        patch("saorsa_deploy.cmd.record_results._network_map_from_client_state", return_value={}),
        patch(
            "saorsa_deploy.cmd.record_results._build_client_kind_lookup",
            return_value=lambda host: None,
        ),
    ):
        _record_file_op_records(
            repo,
            SimpleNamespace(id=1, name="DEV-BULK", deploy_args={}),
            SimpleNamespace(name="DEV-BULK"),
            _silent_console(),
            "http://influx",
            "token",
            "ant",
            "testnet-metrics",
        )

    assert len(repo.calls["upload"]) == 1
    assert len(repo.calls["download"]) == 1
    assert len(repo.calls["upload"][0]) == 3
    assert len(repo.calls["download"][0]) == 2


def test_record_pointer_ops_calls_the_bulk_method_once():
    from saorsa_deploy.cmd.record_results import _record_pointer_ops

    points = [
        {
            "_time": T0,
            "host": "PTR-1",
            "region": "lon1",
            "op": "update",
            "outcome": "ok",
            "owned": True,
            "address": "ab" * 32,
            "start_ns": n,
            "duration_ms": 900,
        }
        for n in (1, 2, 3, 4)
    ]
    repo = _CountingRepo()
    with (
        patch("saorsa_deploy.cmd.record_results.fetch_pointer_points", return_value=points),
        patch("saorsa_deploy.cmd.record_results._network_map_from_client_state", return_value={}),
    ):
        _record_pointer_ops(
            repo,
            SimpleNamespace(id=1, name="DEV-BULK", deploy_args={}),
            SimpleNamespace(name="DEV-BULK"),
            _silent_console(),
            "http://influx",
            "token",
            "ant",
            "testnet-metrics",
        )

    assert len(repo.calls["pointer"]) == 1
    assert len(repo.calls["pointer"][0]) == 4


def test_record_browser_downloads_calls_the_bulk_method_once():
    from saorsa_deploy.cmd.record_results import _record_browser_downloads

    points = [
        {
            "_time": T0,
            "host": "DEV-BULK-ant-client-browser-download-lon1-1",
            "region": "lon1",
            "round_id": 1,
            "instance": i,
            "status": "success",
            "start_ns": 1_760_000_000_000_000_000,
            "total_ms": 9120,
        }
        for i in range(4)
    ]
    repo = _CountingRepo()
    with (
        patch(
            "saorsa_deploy.cmd.record_results.fetch_browser_download_points",
            return_value=points,
        ),
        patch("saorsa_deploy.cmd.record_results._client_tiers", return_value=[]),
        patch("saorsa_deploy.hot_files.load_hot_files", return_value=None),
        patch("saorsa_deploy.registry.get_repository", return_value=repo),
    ):
        _record_browser_downloads(
            repo,
            SimpleNamespace(id=1, name="DEV-BULK", deploy_args={}),
            SimpleNamespace(name="DEV-BULK"),
            _silent_console(),
            "http://influx",
            "token",
            "ant",
        )

    assert len(repo.calls["browser"]) == 1
    assert len(repo.calls["browser"][0]) == 4
