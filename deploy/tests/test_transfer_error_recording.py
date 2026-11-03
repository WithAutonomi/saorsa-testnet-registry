"""`error_kind` and `reason` reach Postgres, and a null never erases them.

The durable copy of a failed transfer said nothing at all about why: a failed
download was `status=failure`, `file_size_bytes=0` and a duration, and
PROD-WDL-01 had eleven such rows (INFRA-484). The two fields now ride the
Influx point into the op-record and prod-result tables.

The COALESCE is the load-bearing half. Both fields come off the Influx point and
the bucket has a retention window, so a later `deployments archive` over a
window the points have expired out of resolves None for every one of them --
which, written unconditionally, would erase the only durable record of why a
transfer failed. It is the same rule `client_kind` and `client_build` already
carry, for the same reason.

Also here: the per-operation pass must not be skipped when the `--window`
aggregate finds nothing, because that pass queries the full range on its own and
is the one that writes the rows that expire.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from saorsa_db.db import Base
from saorsa_db.models import OpRecordStatus, ProdDownloadStatus, ProdUploadStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

T0 = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)
REASON = "Failed to fetch public DataMap: not found: DataMap chunk not found at d3da"


@pytest.fixture()
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = Session()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


def _op(deployment_id, **over):
    op = dict(
        deployment_id=deployment_id,
        client_hostname="PROD-DL-01-ant-client-download-LON1-1",
        region="LON1",
        file_size_bytes=0,
        status=OpRecordStatus.FAILURE,
        start_ns=1_792_000_000_000_000_000,
        influx_time=T0,
        started_at=T0,
        finished_at=T0,
        duration_ms=1_190_248,
    )
    op.update(over)
    return op


@pytest.mark.parametrize("upsert", ["upsert_upload_record", "upsert_download_record"])
class TestBothFieldsSurviveAReRun:
    def test_they_are_written(self, repo, upsert):
        d = repo.create(name="PROD-DL-01", deploy_args={})
        row = getattr(repo, upsert)(**_op(d.id, error_kind="not_found", reason=REASON))
        assert row.error_kind == "not_found"
        assert row.reason == REASON

    def test_a_null_re_upsert_keeps_them(self, repo, upsert):
        """The re-run over an expired window: every field resolves None."""
        d = repo.create(name="PROD-DL-01", deploy_args={})
        write = getattr(repo, upsert)
        write(**_op(d.id, error_kind="not_found", reason=REASON))
        row = write(**_op(d.id, error_kind=None, reason=None, duration_ms=1_190_999))

        assert row.error_kind == "not_found"
        assert row.reason == REASON
        # A coalesce on two columns, not a refusal to write the row.
        assert row.duration_ms == 1_190_999

    def test_a_known_value_still_updates(self, repo, upsert):
        d = repo.create(name="PROD-DL-01", deploy_args={})
        write = getattr(repo, upsert)
        write(**_op(d.id, error_kind="other", reason="ant exit 1"))
        row = write(**_op(d.id, error_kind="not_found", reason=REASON))
        assert row.error_kind == "not_found"
        assert row.reason == REASON

    def test_a_successful_row_carries_neither(self, repo, upsert):
        """NULL means NOT RECORDED, and a success has nothing to record."""
        d = repo.create(name="PROD-DL-01", deploy_args={})
        row = getattr(repo, upsert)(**_op(d.id, status=OpRecordStatus.SUCCESS))
        assert row.error_kind is None
        assert row.reason is None

    def test_both_fields_are_in_to_dict(self, repo, upsert):
        d = repo.create(name="PROD-DL-01", deploy_args={})
        row = getattr(repo, upsert)(**_op(d.id, error_kind="timeout", reason="timeout: x"))
        as_dict = row.to_dict()
        assert as_dict["error_kind"] == "timeout"
        assert as_dict["reason"] == "timeout: x"


class TestProdResultTables:
    def _upload(self, repo, deployment_id, **over):
        kwargs = dict(
            deployment_id=deployment_id,
            client_hostname="PROD-UL-01-ant-client-upload-LON1-1",
            region="LON1",
            source_url="https://example.invalid/a.bin",
            file_name="a.bin",
            file_size_bytes=1024,
            status=ProdUploadStatus.FAILED,
            address=None,
            chunks_stored=0,
            chunks_failed=0,
            total_chunks=0,
            duration_ms=100,
            upload_secs=0.1,
            started_at=T0,
            finished_at=T0,
            reason="insufficient peers: Got 0 quotes, need 1",
        )
        kwargs.update(over)
        return repo.upsert_prod_upload_result(**kwargs)

    def test_the_upload_kind_is_written_and_never_erased(self, repo):
        d = repo.create(name="PROD-UL-01", deploy_args={})
        self._upload(repo, d.id, error_kind="quorum")
        row = self._upload(repo, d.id, error_kind=None, duration_ms=200)
        assert row.error_kind == "quorum"
        assert row.duration_ms == 200
        assert row.to_dict()["error_kind"] == "quorum"

    def test_the_download_kind_is_written(self, repo):
        d = repo.create(name="PROD-DL-LOCAL", deploy_args={})
        row = repo.upsert_prod_download_result(
            deployment_id=d.id,
            client_hostname="laptop",
            region="local",
            address="ab" * 32,
            status=ProdDownloadStatus.FAILED,
            output_path=None,
            file_size_bytes=None,
            duration_ms=100,
            download_secs=None,
            started_at=T0,
            finished_at=T0,
            reason=REASON,
            error_kind="not_found",
        )
        assert row.error_kind == "not_found"
        assert row.to_dict()["error_kind"] == "not_found"


class _CollectingRepo:
    """Only what `_record_file_op_records` calls."""

    def __init__(self):
        self.upload_rows: list[dict] = []
        self.download_rows: list[dict] = []

    def upsert_upload_records(self, rows):
        self.upload_rows.extend(rows)
        return len(rows)

    def upsert_download_records(self, rows):
        self.download_rows.extend(rows)
        return len(rows)

    def upsert_file_result(self, **_kwargs):  # pragma: no cover - not reached here
        raise AssertionError("the aggregate must not be written when there are no points")


def _silent_console():
    return SimpleNamespace(print=lambda *_, **__: None)


def _point(measurement, start_ns, **over):
    point = {
        "_time": T0,
        "host": "hostA",
        "region": "lon1",
        "_measurement": measurement,
        "file_size": 0,
        "start_ns": start_ns,
        "status": 1,
        "total_ms": 100,
    }
    point.update(over)
    return point


class TestTheInfluxPointCarriesBothFields:
    def test_both_reach_the_row_dicts(self):
        from saorsa_deploy.cmd.record_results import _record_file_op_records

        points = [
            _point("upload", 1, error_kind="quorum", reason="partial upload: 3/4 stored"),
            _point("download", 2, error_kind="not_found", reason=REASON),
        ]
        repo = _CollectingRepo()
        with (
            patch(
                "saorsa_deploy.cmd.record_results._fetch_all_op_points",
                return_value=points,
            ),
            patch(
                "saorsa_deploy.cmd.record_results._network_map_from_client_state",
                return_value={},
            ),
            patch(
                "saorsa_deploy.cmd.record_results._build_client_kind_lookup",
                return_value=lambda host: None,
            ),
        ):
            op_records = _record_file_op_records(
                repo,
                SimpleNamespace(id=1, name="PROD-DL-01", deploy_args={}),
                SimpleNamespace(name="PROD-DL-01"),
                _silent_console(),
                "http://influx",
                "token",
                "ant",
                "testnet-metrics",
            )

        assert repo.upload_rows[0]["error_kind"] == "quorum"
        assert repo.upload_rows[0]["reason"] == "partial upload: 3/4 stored"
        assert repo.download_rows[0]["error_kind"] == "not_found"
        assert repo.download_rows[0]["reason"] == REASON
        # And onto the per-op detail the Slack reply is built from.
        assert {r["error_kind"] for r in op_records} == {"quorum", "not_found"}
        assert REASON in {r["reason"] for r in op_records}

    def test_reason_is_in_the_pivot_allowlist(self):
        """The allowlist is why a new field would otherwise reach no consumer."""
        from saorsa_deploy.influx import PIVOT_FLUX_TEMPLATE

        assert 'r._field == "reason"' in PIVOT_FLUX_TEMPLATE

    def test_the_pivoted_parser_returns_it(self):
        from saorsa_deploy.influx import _parse_pivoted_csv

        body = (
            "#datatype,string,long,dateTime:RFC3339,string,string,string,long,long,string\n"
            ",result,table,_time,_measurement,host,region,status,start_ns,reason\n"
            ",_result,0,2026-11-03T02:00:00Z,download,hostA,LON1,1,100,"
            + REASON.replace(",", " ")
            + "\n"
        )
        points = _parse_pivoted_csv(body)
        assert points[0]["reason"] == REASON.replace(",", " ")

    def test_an_absent_reason_parses_as_none_not_empty_string(self):
        from saorsa_deploy.influx import _parse_pivoted_csv

        body = (
            "#datatype,string,long,dateTime:RFC3339,string,string,string,long,long\n"
            ",result,table,_time,_measurement,host,region,status,start_ns\n"
            ",_result,0,2026-11-03T02:00:00Z,download,hostA,LON1,0,100\n"
        )
        assert _parse_pivoted_csv(body)[0]["reason"] is None


class TestThePerOpPassRunsWhenTheAggregateIsEmpty:
    """Item 9: the two passes are independent and only one takes `--window`.

    `_record_file_ops` used to return before `_record_file_op_records` whenever
    the windowed aggregate query found nothing, so a deployment whose service
    was down across the whole window archived NOTHING -- including the
    operations still sitting in the bucket, which are the copy that expires.
    """

    def test_the_records_are_still_written(self):
        from saorsa_deploy.cmd.record_results import _record_file_ops

        repo = _CollectingRepo()
        # The aggregate query returns nothing in the window; the per-op query
        # (full range, its own call) returns a failed download.
        aggregate_points: list[dict] = []
        op_points = [_point("download", 7, error_kind="not_found", reason=REASON)]
        calls = {"n": 0}

        def _fetch(*_args, **_kwargs):
            calls["n"] += 1
            return aggregate_points if calls["n"] == 1 else op_points

        with (
            patch("saorsa_deploy.cmd.record_results._fetch_all_op_points", _fetch),
            patch(
                "saorsa_deploy.cmd.record_results._network_map_from_client_state",
                return_value={},
            ),
            patch(
                "saorsa_deploy.cmd.record_results._build_client_kind_lookup",
                return_value=lambda host: None,
            ),
        ):
            _record_file_ops(
                repo,
                SimpleNamespace(id=1, name="PROD-DL-01", deploy_args={}),
                SimpleNamespace(name="PROD-DL-01", window="-7d"),
                _silent_console(),
                "http://influx",
                "token",
                "ant",
                "testnet-metrics",
            )

        assert len(repo.download_rows) == 1
        assert repo.download_rows[0]["error_kind"] == "not_found"
        assert repo.download_rows[0]["reason"] == REASON

    def test_the_aggregate_absence_is_still_reported(self):
        from saorsa_deploy.cmd.record_results import _record_file_ops

        printed: list[str] = []
        console = SimpleNamespace(print=lambda *a, **_k: printed.append(" ".join(map(str, a))))
        with (
            patch("saorsa_deploy.cmd.record_results._fetch_all_op_points", return_value=[]),
            patch(
                "saorsa_deploy.cmd.record_results._network_map_from_client_state",
                return_value={},
            ),
            patch(
                "saorsa_deploy.cmd.record_results._build_client_kind_lookup",
                return_value=lambda host: None,
            ),
        ):
            result = _record_file_ops(
                repo=_CollectingRepo(),
                deployment=SimpleNamespace(id=1, name="PROD-DL-01", deploy_args={}),
                args=SimpleNamespace(name="PROD-DL-01", window="-7d"),
                console=console,
                influx_url="http://influx",
                influx_token="token",
                influx_org="ant",
                influx_bucket="testnet-metrics",
            )

        assert any("No upload/download points found" in line for line in printed)
        # Nothing was recorded at all, so there is no Slack block to build.
        assert result is None


class TestTheRenderersShowTheKind:
    """Item 10: counts to group by, and only when there is something to count.

    Rendered with a real Rich console into a buffer, so what is asserted is what
    an operator sees rather than the shape of a dict.
    """

    def _render(self, fn, *args):
        from io import StringIO

        from rich.console import Console

        buffer = StringIO()
        console = Console(file=buffer, width=120, no_color=True, legacy_windows=False)
        fn(*args, console)
        return buffer.getvalue()

    def _row(self, status, error_kind, reason=None):
        return SimpleNamespace(
            client_hostname="hostA",
            region="LON1",
            status=status,
            error_kind=error_kind,
            reason=reason,
            file_size_bytes=1024,
            duration_ms=100,
            started_at=T0,
            finished_at=T0,
            start_ns=1,
            address=None,
            verify_status=None,
            client_kind="native",
            client_build=None,
        )

    def test_the_footer_counts_each_kind(self):
        from saorsa_deploy.cmd.print_recorded_results import _print_error_kind_counts

        rows = [
            self._row(OpRecordStatus.FAILURE, "not_found"),
            self._row(OpRecordStatus.FAILURE, "not_found"),
            self._row(OpRecordStatus.FAILURE, "quorum"),
            self._row(OpRecordStatus.SUCCESS, None),
        ]
        out = self._render(_print_error_kind_counts, rows, "Downloads")
        # The title wraps at the table's own width, so match on its words.
        assert "Downloads" in out and "error_kind" in out
        assert "not_found" in out
        assert "quorum" in out
        # Most frequent first.
        assert out.index("not_found") < out.index("quorum")

    def test_the_footer_is_silent_when_nothing_carries_a_kind(self):
        """Every run recorded before the columns existed reads NULL."""
        from saorsa_deploy.cmd.print_recorded_results import _print_error_kind_counts

        rows = [self._row(OpRecordStatus.SUCCESS, None), self._row(OpRecordStatus.FAILURE, None)]
        assert self._render(_print_error_kind_counts, rows, "Downloads") == ""

    def test_the_kind_column_appears_only_when_a_row_has_one(self):
        from saorsa_deploy.cmd.print_recorded_results import _print_op_records_tables

        def render(rows):
            from io import StringIO

            from rich.console import Console

            buffer = StringIO()
            console = Console(file=buffer, width=200, no_color=True, legacy_windows=False)
            _print_op_records_tables("download", rows, "PROD-DL-01", console)
            return buffer.getvalue()

        with_kind = render([self._row(OpRecordStatus.FAILURE, "not_found")])
        assert "Kind" in with_kind
        assert "not_found" in with_kind

        without = render([self._row(OpRecordStatus.SUCCESS, None)])
        assert "Kind" not in without
