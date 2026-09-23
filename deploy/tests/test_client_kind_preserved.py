"""`record-results` must never erase a row's `client_kind` (V2-1286).

The transport of an operation is resolved from the deployment's S3 client
state, and `clients destroy` deletes that state. So a `record-results` re-run
after teardown resolves ``None`` for every host, and an unconditional
assignment would relabel every WASM operation as native -- which pushes WASM
failures into the NATIVE hard success floors, the one place they must never
count.

The upsert therefore coalesces: a NULL never overwrites a known transport, a
known transport still updates.
"""

from datetime import datetime, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.models import OpRecordStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


STARTED = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
FINISHED = datetime(2026, 9, 23, 10, 5, tzinfo=timezone.utc)


def _op(deployment_id, client_kind, **over):
    op = dict(
        deployment_id=deployment_id,
        client_hostname="STG-01-ant-client-wasm-upload-SBG5-1",
        region="SBG5",
        file_size_bytes=1024,
        status=OpRecordStatus.SUCCESS,
        start_ns=1_776_615_435_704_524_798,
        influx_time=FINISHED,
        started_at=STARTED,
        finished_at=FINISHED,
        duration_ms=300_000,
        client_kind=client_kind,
    )
    op.update(over)
    return op


@pytest.mark.parametrize("upsert", ["upsert_upload_record", "upsert_download_record"])
class TestClientKindIsNeverErased:
    def test_a_null_re_upsert_keeps_wasm(self, repo, upsert):
        d = repo.create(name="STG-01", deploy_args={})
        write = getattr(repo, upsert)

        write(**_op(d.id, "wasm"))
        # The teardown re-run: client state is gone, so every host resolves None.
        row = write(**_op(d.id, None, duration_ms=310_000))

        assert row.client_kind == "wasm"
        # The rest of the row still updates -- this is a coalesce on one column,
        # not a refusal to write.
        assert row.duration_ms == 310_000

    def test_a_known_transport_still_updates(self, repo, upsert):
        d = repo.create(name="STG-01", deploy_args={})
        write = getattr(repo, upsert)

        write(**_op(d.id, "wasm"))
        assert write(**_op(d.id, "native")).client_kind == "native"

    def test_a_native_row_is_unaffected(self, repo, upsert):
        # NULL reads as native everywhere downstream, so a native-only run must
        # look exactly as it did before this column existed.
        d = repo.create(name="STG-01", deploy_args={})
        write = getattr(repo, upsert)

        write(**_op(d.id, None))
        assert write(**_op(d.id, None)).client_kind is None


class TestWasmHostLookup:
    def test_lists_only_this_deployment_s_wasm_hosts(self, repo):
        d = repo.create(name="STG-01", deploy_args={})
        other = repo.create(name="STG-02", deploy_args={})

        repo.upsert_upload_record(**_op(d.id, "wasm"))
        repo.upsert_download_record(
            **_op(d.id, "wasm", client_hostname="STG-01-ant-client-wasm-download-tor1-1")
        )
        repo.upsert_upload_record(
            **_op(d.id, "native", client_hostname="STG-01-ant-client-upload-SBG5-1")
        )
        repo.upsert_upload_record(**_op(other.id, "wasm"))

        assert repo.list_wasm_client_hostnames(d.id) == [
            "STG-01-ant-client-wasm-download-tor1-1",
            "STG-01-ant-client-wasm-upload-SBG5-1",
        ]

    def test_empty_for_a_native_only_run(self, repo):
        d = repo.create(name="STG-01", deploy_args={})
        repo.upsert_upload_record(**_op(d.id, None))
        assert repo.list_wasm_client_hostnames(d.id) == []
