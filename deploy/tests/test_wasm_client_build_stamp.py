"""Which WASM build performed each transfer — V2-1346.

`deployments upgrade-wasm-client` replaces the bundle underneath a permanently
running deployment, and `deploy_args.wasm_build` holds only the CURRENT build
and is overwritten. So the build that actually ran has to travel with each
operation: `ant-wasm` prints it, the service puts it on its Influx point, the
recording path writes it to the row, and the reporting path splits on it.

The other half of the contract is that nothing native changes. A native service
emits no such field, a native row keeps NULL, and a deployment carrying a single
build (or none) renders exactly as it did before any of this existed.
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from saorsa_db.db import Base
from saorsa_db.models import OpRecordStatus, ProdUploadStatus, ProdUploadVerifyStatus
from saorsa_db.repositories import SqlAlchemyDeploymentRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from saorsa_deploy import influx
from saorsa_deploy.cmd import print_recorded_results as prr

SCRIPTS = Path(__file__).resolve().parents[1] / "saorsa_deploy/resources/scripts"

COMMIT_A = "aaaa1111bbbb2222cccc3333"
COMMIT_B = "dddd4444eeee5555ffff6666"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def downloader():
    return _load("prod_downloader")


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield SqlAlchemyDeploymentRepository(session)
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# The download service's Influx point
# ---------------------------------------------------------------------------


class TestDownloaderInfluxField:
    def _line(self, downloader, **overrides):
        kwargs = dict(
            host="h",
            region="r",
            status=0,
            total_ms=5,
            download_secs=1.0,
            file_size=10,
            start_ns=1,
        )
        kwargs.update(overrides)
        return downloader._build_line(**kwargs)

    def test_the_build_is_a_field_when_the_child_reported_one(self, downloader):
        assert 'client_build="abc123"' in self._line(downloader, client_build="abc123")

    def test_a_native_point_is_byte_identical(self, downloader):
        # The native `ant` reports no build, so the field must be absent
        # entirely rather than present and empty — an empty string would read as
        # a build that was recorded and happened to be blank.
        assert "client_build" not in self._line(downloader)

    def test_the_build_is_escaped_like_every_other_string_field(self, downloader):
        line = self._line(downloader, client_build='a"b')
        assert 'client_build="a\\"b"' in line


class TestTheDownloadWalkCarriesTheBuild:
    """One ONE_SHOT pass against a fake `ant-wasm`, as the service runs it."""

    def _run(self, downloader, tmp_path, monkeypatch, *, fake_cli, wasm=True):
        cli = tmp_path / "ant-wasm"
        cli.write_text(fake_cli)
        cli.chmod(0o755)
        addrs = tmp_path / "addresses.txt"
        addrs.write_text("addr-one\n")

        posted: list[str] = []
        monkeypatch.setattr(
            downloader,
            "_post_influx",
            lambda url, token, org, bucket, line: posted.append(line),
        )
        monkeypatch.setattr(downloader, "LOG_DIR", str(tmp_path / "logs"))
        for key, value in {
            "CLIENT_KIND": "wasm" if wasm else "native",
            "ANT_WASM": str(cli),
            "ANT": str(cli),
            "REGION": "lon1",
            "ADDRESSES_FILE": str(addrs),
            "DOWNLOAD_DIR": str(tmp_path / "dl"),
            "ONE_SHOT": "true",
            "INFLUX_URL": "http://influx:8086",
            "INFLUX_TOKEN": "tok",
            "BOOTSTRAP_CACHE": "true",
        }.items():
            monkeypatch.setenv(key, value)
        assert downloader.main() == 0
        return posted

    def test_the_build_reaches_the_point(self, downloader, tmp_path, monkeypatch):
        fake = (
            "#!/bin/sh\n"
            'echo \'{"elapsed_secs": 1.5, "file_size": 10, '
            f'"client_build": "{COMMIT_A}"}}\'\n'
        )
        posted = self._run(downloader, tmp_path, monkeypatch, fake_cli=fake)
        assert len(posted) == 1
        assert f'client_build="{COMMIT_A}"' in posted[0]

    def test_a_bundle_without_provenance_omits_the_field(self, downloader, tmp_path, monkeypatch):
        # A bundle built before build-info.json existed reports null, which is
        # "unknown build" — not a build whose name happens to be empty.
        fake = '#!/bin/sh\necho \'{"elapsed_secs": 1.5, "client_build": null}\'\n'
        posted = self._run(downloader, tmp_path, monkeypatch, fake_cli=fake)
        assert "client_build" not in posted[0]

    def test_the_native_walk_is_unchanged(self, downloader, tmp_path, monkeypatch):
        fake = "#!/bin/sh\necho 'Time: 1.5'\n"
        posted = self._run(downloader, tmp_path, monkeypatch, fake_cli=fake, wasm=False)
        assert "client_build" not in posted[0]


# ---------------------------------------------------------------------------
# The Influx read path
# ---------------------------------------------------------------------------


class TestInfluxQuery:
    def test_the_pivot_asks_for_the_field(self):
        # A field the query does not name is simply absent from every row, and
        # the recording path would then write NULL over a known build.
        assert 'r._field == "client_build"' in influx.PIVOT_FLUX_TEMPLATE

    def test_the_parser_returns_it(self):
        csv = (
            ",result,table,_time,_measurement,host,region,address,client_build,"
            "error_kind,file_size,start_ns,status,total_ms\n"
            f",_result,0,2026-09-27T10:00:00Z,download,hostA,lon1,addr,{COMMIT_A},"
            ",10,1000,0,50\n"
        )
        points = influx._parse_pivoted_csv(csv)
        assert len(points) == 1
        assert points[0]["client_build"] == COMMIT_A

    def test_an_absent_column_reads_as_none(self):
        csv = (
            ",result,table,_time,_measurement,host,region,file_size,start_ns,"
            "status,total_ms\n"
            ",_result,0,2026-09-27T10:00:00Z,download,hostA,lon1,10,1000,0,50\n"
        )
        assert influx._parse_pivoted_csv(csv)[0]["client_build"] is None


# ---------------------------------------------------------------------------
# record-results, on the download kinds
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The prod-upload tables
# ---------------------------------------------------------------------------


def _upload(repo, deployment_id, source_url, client_build, status=ProdUploadStatus.SUCCESS):
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    return repo.upsert_prod_upload_result(
        deployment_id=deployment_id,
        client_hostname="hostA",
        region="lon1",
        source_url=source_url,
        file_name=source_url.rsplit("/", 1)[-1],
        file_size_bytes=1024,
        status=status,
        address="addr",
        chunks_stored=4,
        chunks_failed=0,
        total_chunks=4,
        duration_ms=1000,
        upload_secs=1.0,
        started_at=now,
        finished_at=now,
        client_kind="wasm",
        client_build=client_build,
    )


def _verification(repo, deployment_id, address, client_build, hostname="hostA"):
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    return repo.upsert_prod_upload_verification(
        deployment_id=deployment_id,
        client_hostname=hostname,
        region="lon1",
        address=address,
        file_name="a.bin",
        status=ProdUploadVerifyStatus.OK,
        started_at=now,
        finished_at=now,
        expected_sha256="a" * 64,
        actual_sha256="a" * 64,
        client_build=client_build,
    )


class TestProdUploadTables:
    def test_the_upload_row_stores_the_build(self, repo):
        deployment = repo.create(name="PROD-WUL-01", deploy_args={})
        row = _upload(repo, deployment.id, "https://x/a.bin", COMMIT_A)
        assert row.client_build == COMMIT_A

    def test_a_native_upload_row_stays_null(self, repo):
        deployment = repo.create(name="PROD-UL-01", deploy_args={})
        row = repo.upsert_prod_upload_result(
            deployment_id=deployment.id,
            client_hostname="hostA",
            region="lon1",
            source_url="https://x/a.bin",
            file_name="a.bin",
            file_size_bytes=1024,
            status=ProdUploadStatus.SUCCESS,
            address="addr",
            chunks_stored=4,
            chunks_failed=0,
            total_chunks=4,
            duration_ms=1000,
            upload_secs=1.0,
            started_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
            finished_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
        )
        assert row.client_build is None

    def test_a_rerun_without_a_build_keeps_the_stored_one(self, repo):
        deployment = repo.create(name="PROD-WUL-01", deploy_args={})
        _upload(repo, deployment.id, "https://x/a.bin", COMMIT_A)
        row = _upload(repo, deployment.id, "https://x/a.bin", None)
        assert row.client_build == COMMIT_A

    def test_the_verification_row_stores_the_build(self, repo):
        deployment = repo.create(name="PROD-WUL-01", deploy_args={})
        assert _verification(repo, deployment.id, "addr1", COMMIT_A).client_build == COMMIT_A

    def test_a_native_verifier_row_stays_null(self, repo):
        # The independent verifier VMs are native by design, so a digest
        # mismatch they report stays attributable to the upload.
        deployment = repo.create(name="PROD-WUL-01", deploy_args={})
        row = _verification(repo, deployment.id, "addr1", None, hostname="verifier")
        assert row.client_build is None

    def test_a_recheck_without_a_build_keeps_the_stored_one(self, repo):
        deployment = repo.create(name="PROD-WUL-01", deploy_args={})
        _verification(repo, deployment.id, "addr1", COMMIT_A)
        assert _verification(repo, deployment.id, "addr1", None).client_build == COMMIT_A


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _render(fn, *args) -> str:
    console = Console(width=200, force_terminal=False, no_color=True)
    with console.capture() as capture:
        fn(*args, console)
    return capture.get()


def _download_record(repo, deployment_id, start_ns, client_build):
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    return repo.upsert_download_record(
        deployment_id=deployment_id,
        client_hostname="hostA",
        region="lon1",
        file_size_bytes=1024,
        status=OpRecordStatus.SUCCESS,
        start_ns=start_ns,
        influx_time=now,
        started_at=now,
        finished_at=now,
        duration_ms=500,
        client_kind="wasm",
        address="addr",
        client_build=client_build,
    )


class TestPrintRecordedResultsSplitsByBuild:
    def test_two_builds_produce_two_tables(self, repo):
        deployment = repo.create(name="PROD-WDL-01", deploy_args={})
        _download_record(repo, deployment.id, 1, COMMIT_A)
        _download_record(repo, deployment.id, 2, COMMIT_B)
        rows = repo.list_download_records_for_deployment(deployment.id)

        out = _render(prr._print_op_records_for_kind, "download", rows, "PROD-WDL-01")
        assert f"build {COMMIT_A[:12]}" in out
        assert f"build {COMMIT_B[:12]}" in out

    def test_one_build_renders_exactly_as_it_did(self, repo):
        deployment = repo.create(name="PROD-WDL-01", deploy_args={})
        _download_record(repo, deployment.id, 1, COMMIT_A)
        _download_record(repo, deployment.id, 2, COMMIT_A)
        rows = repo.list_download_records_for_deployment(deployment.id)

        out = _render(prr._print_op_records_for_kind, "download", rows, "PROD-WDL-01")
        assert "build " not in out
        assert "download — hostA (2 record(s)) — 'PROD-WDL-01'" in " ".join(out.split())

    def test_no_build_at_all_renders_exactly_as_it_did(self, repo):
        deployment = repo.create(name="PROD-DL-01", deploy_args={})
        repo.upsert_download_record(
            deployment_id=deployment.id,
            client_hostname="hostA",
            region="lon1",
            file_size_bytes=1024,
            status=OpRecordStatus.SUCCESS,
            start_ns=1,
            influx_time=datetime(2026, 9, 27, tzinfo=timezone.utc),
            started_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
            finished_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
            duration_ms=500,
        )
        rows = repo.list_download_records_for_deployment(deployment.id)
        assert "build " not in _render(
            prr._print_op_records_for_kind, "download", rows, "PROD-DL-01"
        )

    def test_the_totals_footer_splits_too(self, repo):
        deployment = repo.create(name="PROD-WDL-01", deploy_args={})
        _download_record(repo, deployment.id, 1, COMMIT_A)
        _download_record(repo, deployment.id, 2, COMMIT_B)
        rows = repo.list_download_records_for_deployment(deployment.id)

        out = " ".join(_render(prr._print_transport_totals, [], rows).split())
        assert f"wasm 1/1 = 100.00% [build {COMMIT_A[:12]}]" in out
        assert f"wasm 1/1 = 100.00% [build {COMMIT_B[:12]}]" in out

    def test_the_prod_upload_section_splits(self, repo):
        deployment = repo.create(name="PROD-WUL-01", deploy_args={})
        _upload(repo, deployment.id, "https://x/a.bin", COMMIT_A)
        _upload(repo, deployment.id, "https://x/b.bin", COMMIT_B)
        rows = repo.list_prod_upload_results_for_deployment(deployment.id)

        out = _render(prr._print_prod_upload_section, rows, "PROD-WUL-01", False)
        assert f"build {COMMIT_A[:12]}" in out
        assert f"build {COMMIT_B[:12]}" in out

    def test_the_prod_upload_section_is_unchanged_with_one_build(self, repo):
        deployment = repo.create(name="PROD-WUL-01", deploy_args={})
        _upload(repo, deployment.id, "https://x/a.bin", COMMIT_A)
        rows = repo.list_prod_upload_results_for_deployment(deployment.id)
        assert "build " not in _render(prr._print_prod_upload_section, rows, "PROD-WUL-01", False)

    def test_the_verification_section_splits_the_native_verifier_out(self, repo):
        # The uploader's own browser-path read-backs carry a build; the native
        # verifier VM's rows do not, and land under "unknown".
        deployment = repo.create(name="PROD-WUL-01", deploy_args={})
        _verification(repo, deployment.id, "addr1", COMMIT_A)
        _verification(repo, deployment.id, "addr2", None, hostname="verifier")
        rows = repo.list_prod_upload_verifications_for_deployment(deployment.id)

        out = _render(prr._print_verification_section, rows, "PROD-WUL-01", False)
        assert f"build {COMMIT_A[:12]}" in out
        assert "build unknown" in out

    def test_the_verification_section_is_unchanged_with_one_build(self, repo):
        deployment = repo.create(name="PROD-UL-01", deploy_args={})
        _verification(repo, deployment.id, "addr1", None)
        rows = repo.list_prod_upload_verifications_for_deployment(deployment.id)
        assert "build " not in _render(prr._print_verification_section, rows, "PROD-UL-01", False)


class TestSplitByBuild:
    def test_unknown_is_a_bucket_of_its_own(self):
        rows = [
            SimpleNamespace(client_build=COMMIT_A),
            SimpleNamespace(client_build=None),
        ]
        labels = [label for label, _ in prr._split_by_build(rows)]
        assert labels == [f"build {COMMIT_A[:12]}", "build unknown"]

    def test_a_single_build_is_not_split(self):
        rows = [SimpleNamespace(client_build=COMMIT_A)] * 3
        assert prr._split_by_build(rows) == [(None, rows)]
