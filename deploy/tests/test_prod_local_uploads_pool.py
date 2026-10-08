"""Tests for wiring `prod local-uploads` to the file pool.

This kind resolves its claims *inline*, unlike the remote kinds: it writes
result rows as it goes, so there is no later `record-results` pass to close
them. A claim left open would make a finished upload look merely checked out —
and one left open on a failed upload would burn a file that never reached the
network.
"""

import argparse

import pytest
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import ClaimSource, ClaimStatus, PoolFileKind
from saorsa_db.models.prod_upload_result import ProdUploadStatus
from saorsa_db.repositories import SqlAlchemyProdFilePoolRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy import pool_selection
from saorsa_deploy.cmd import prod_local_uploads

MIB = 1024 * 1024


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyProdFilePoolRepository(session)


@pytest.fixture
def fake_s3(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(pool_selection, "load_pool_selection", lambda name: store.get(name))
    monkeypatch.setattr(
        pool_selection,
        "save_pool_selection",
        lambda name, selection: store.__setitem__(name, selection),
    )
    return store


@pytest.fixture
def wired(repo, monkeypatch, fake_s3):
    monkeypatch.setattr("saorsa_deploy.registry.get_file_pool_repository", lambda: repo)
    return repo


def _seed_files(repo, tmp_path, count=3, size=5 * MIB):
    made = []
    for i in range(count):
        target = tmp_path / f"f{i}.tar"
        target.write_bytes(b"x" * size)
        pool_file, _ = repo.add_pool_file(
            PoolFileKind.LOCAL, str(target), target.name, size_bytes=size
        )
        made.append((pool_file, target))
    return made


def _args(from_pool="tiny:2", name="LOCAL-UL-03"):
    return argparse.Namespace(name=name, from_pool=from_pool)


def _flat(text):
    return " ".join(text.split())


class TestSelection:
    def test_returns_path_and_name_pairs(self, wired, tmp_path, capsys):
        from rich.console import Console

        _seed_files(wired, tmp_path, count=3)
        pairs, claims = prod_local_uploads._select_from_pool(_args(), Console())

        assert len(pairs) == 2
        for path, name in pairs:
            assert path.is_file()
            assert name == path.name
        assert len(claims) == 2
        assert all(c["claim_id"] is not None for c in claims)

    def test_draws_local_entries_only(self, wired, tmp_path):
        # A URL entry cannot be read from disk, so this command must never be
        # able to draw one — there is deliberately no flag to make it try.
        from rich.console import Console

        wired.add_pool_file(PoolFileKind.URL, "https://example.org/a", "a", size_bytes=5 * MIB)
        with pytest.raises(SystemExit):
            prod_local_uploads._select_from_pool(_args(), Console())

    def test_missing_file_fails_before_any_upload(self, wired, tmp_path, capsys):
        # The catalogue holds a reference, not the bytes. A file that moved
        # since it was catalogued must fail here, not partway through a run
        # that is spending real money.
        from rich.console import Console

        made = _seed_files(wired, tmp_path, count=2)
        made[0][1].unlink()

        with pytest.raises(SystemExit):
            prod_local_uploads._select_from_pool(_args(), Console())
        out = _flat(capsys.readouterr().out)
        assert "no longer on disk" in out
        assert "prod pool verify --kind local" in out
        assert "prod pool release" in out

    def test_shortfall_exits_with_guidance(self, wired, tmp_path, capsys):
        from rich.console import Console

        _seed_files(wired, tmp_path, count=1)
        with pytest.raises(SystemExit):
            prod_local_uploads._select_from_pool(_args("tiny:5"), Console())
        assert "local pool cannot satisfy" in _flat(capsys.readouterr().out)

    def test_missing_database_exits(self, monkeypatch, capsys, fake_s3):
        from rich.console import Console

        monkeypatch.setattr("saorsa_deploy.registry.get_file_pool_repository", lambda: None)
        with pytest.raises(SystemExit):
            prod_local_uploads._select_from_pool(_args(), Console())
        assert "--from-pool needs DATABASE_URL" in _flat(capsys.readouterr().out)

    def test_bad_request_token_exits(self, wired, capsys):
        from rich.console import Console

        with pytest.raises(SystemExit):
            prod_local_uploads._select_from_pool(_args("tiny"), Console())
        assert "Expected class:count" in _flat(capsys.readouterr().out)

    def test_resume_reuses_the_same_files(self, wired, tmp_path, fake_s3):
        from rich.console import Console

        _seed_files(wired, tmp_path, count=5)
        first, _ = prod_local_uploads._select_from_pool(_args(), Console())
        second, _ = prod_local_uploads._select_from_pool(_args(), Console())
        assert [p for p, _ in first] == [p for p, _ in second]
        assert len(wired.list_pool_claims()) == 2


class TestInlineResolution:
    def test_successful_upload_marks_the_claim_uploaded(self, wired, tmp_path):
        from rich.console import Console

        _seed_files(wired, tmp_path, count=2)
        _, claims = prod_local_uploads._select_from_pool(_args(), Console())

        prod_local_uploads._resolve_pool_claim(
            claims, claims[0]["name"], ProdUploadStatus.SUCCESS, Console()
        )
        claim = wired.list_pool_claims(status=ClaimStatus.UPLOADED)
        assert len(claim) == 1
        assert claim[0].source is ClaimSource.PROD_LOCAL_UPLOADS

    def test_partial_upload_marks_the_claim_partial(self, wired, tmp_path):
        # Real chunks reached the network, so it is neither spent nor untouched.
        from rich.console import Console

        _seed_files(wired, tmp_path, count=2)
        _, claims = prod_local_uploads._select_from_pool(_args(), Console())

        prod_local_uploads._resolve_pool_claim(
            claims, claims[0]["name"], ProdUploadStatus.PARTIAL_UPLOAD, Console()
        )
        partial = wired.list_pool_claims(status=ClaimStatus.PARTIAL)
        assert len(partial) == 1
        # Back in the pool — completing it is useful work. The other file of
        # the pair is still claimed, so exactly one becomes available.
        available = {f.id for f in wired.list_pool_files(status="available")}
        assert available == {partial[0].file_id}

    def test_failed_upload_releases_the_file(self, wired, tmp_path):
        # It never reached the network, so it goes back to the pool rather than
        # being burned by the attempt.
        from rich.console import Console

        _seed_files(wired, tmp_path, count=2)
        _, claims = prod_local_uploads._select_from_pool(_args(), Console())

        prod_local_uploads._resolve_pool_claim(
            claims, claims[0]["name"], ProdUploadStatus.DOWNLOAD_FAILED, Console()
        )
        assert len(wired.list_pool_claims(status=ClaimStatus.RELEASED)) == 1
        assert len(wired.list_pool_files(status="available")) == 1

    def test_unknown_name_is_ignored(self, wired, tmp_path):
        from rich.console import Console

        _seed_files(wired, tmp_path, count=2)
        _, claims = prod_local_uploads._select_from_pool(_args(), Console())
        prod_local_uploads._resolve_pool_claim(
            claims, "nope.tar", ProdUploadStatus.SUCCESS, Console()
        )
        assert wired.list_pool_claims(status=ClaimStatus.UPLOADED) == []

    def test_no_claims_is_a_noop(self):
        from rich.console import Console

        prod_local_uploads._resolve_pool_claim([], "a", ProdUploadStatus.SUCCESS, Console())


class TestManifest:
    def test_exactly_one_input_mode_is_required(self):
        from pydantic import ValidationError

        from saorsa_deploy.config.models import Binary, ProdLocalUploadsSpec

        binary = Binary(version="0.5.2")
        with pytest.raises(ValidationError, match="exactly one of"):
            ProdLocalUploadsSpec(binary=binary)
        with pytest.raises(ValidationError, match="exactly one of"):
            ProdLocalUploadsSpec(binary=binary, urls="/tmp/l", pool={"tiny": 1})

    def test_pool_maps_to_the_cli_flag(self):
        from saorsa_deploy.config.models import Binary, Manifest, ProdLocalUploadsSpec
        from saorsa_deploy.orchestrator.prod import _local_uploads

        manifest = Manifest(
            kind="prod-local-uploads",
            name="LOCAL-UL-03",
            purpose="test",
            prod_local_uploads=ProdLocalUploadsSpec(
                binary=Binary(version="0.5.2"), pool={"small": 2, "tiny": 10}
            ),
        )
        argv = _local_uploads(manifest)[0].argv
        assert "--from-pool" in argv
        # Smallest-first ordering, so the step journal sees stable argv.
        assert argv[argv.index("--from-pool") + 1] == "tiny:10,small:2"


class TestCommandLineSurface:
    """Parse real argv, not a hand-built Namespace.

    Every other test here calls `_select_from_pool` with a Namespace it
    constructed itself, which bypasses argparse entirely — so `--from-pool` was
    added *outside* the mutually-exclusive input group and argparse went on
    requiring --urls or --local-dir-path. The manifests validated, the plan
    rendered, and the run died at step 1 with "one of the arguments --urls
    --local-dir-path is required".
    """

    def _parse(self, argv):
        import contextlib
        import io

        from saorsa_deploy.main import main

        # main() dispatches after parsing; capture the parsed args by making the
        # dispatch raise before doing anything.
        captured = {}

        def _spy(args):
            captured["args"] = args
            raise SystemExit(0)

        import saorsa_deploy.cmd.prod_local_uploads as mod

        real = mod.cmd_prod_local_uploads
        mod.cmd_prod_local_uploads = _spy
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with pytest.raises(SystemExit):
                    main(argv)
        finally:
            mod.cmd_prod_local_uploads = real
        return captured.get("args")

    def test_from_pool_alone_is_accepted(self):
        args = self._parse(
            [
                "prod",
                "local-uploads",
                "--name",
                "LOCAL-UL-MED",
                "--purpose",
                "test",
                "--from-pool",
                "medium:1",
                "--exclude-partial",
                "--ant-version",
                "0.3.2",
            ]
        )
        assert args is not None, "--from-pool alone must satisfy the parser"
        assert args.from_pool == "medium:1"
        assert args.exclude_partial is True
        assert args.urls is None and args.local_dir_path is None

    def test_urls_alone_still_accepted(self):
        args = self._parse(
            [
                "prod",
                "local-uploads",
                "--name",
                "X",
                "--purpose",
                "t",
                "--urls",
                "/tmp/list.txt",
            ]
        )
        assert args is not None
        assert args.urls == "/tmp/list.txt"
        assert args.from_pool is None

    def test_from_pool_with_urls_is_rejected(self):
        # Three input sources, exactly one required — the same rule the
        # manifest validator enforces.
        assert (
            self._parse(
                [
                    "prod",
                    "local-uploads",
                    "--name",
                    "X",
                    "--purpose",
                    "t",
                    "--from-pool",
                    "tiny:1",
                    "--urls",
                    "/tmp/list.txt",
                ]
            )
            is None
        )

    def test_no_input_source_is_rejected(self):
        assert (
            self._parse(
                [
                    "prod",
                    "local-uploads",
                    "--name",
                    "X",
                    "--purpose",
                    "t",
                ]
            )
            is None
        )
