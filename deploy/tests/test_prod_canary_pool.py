"""Tests for wiring `prod canary` to the file pool.

The canary is the pool's dominant consumer — 382, 311 and 313 distinct files
across the three runs to date, every one of them permanently spent. Two
properties follow. A shortfall must surface before any VM exists, because the
alternative is a half-built deployment on a funded mainnet wallet. And a
resumed deploy must reuse its persisted selection, or a retry would draw
another ~350 files while the first set sat claimed.
"""

import argparse

import pytest
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import ClaimSource, PoolFileKind
from saorsa_db.repositories import SqlAlchemyProdFilePoolRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy import pool_selection
from saorsa_deploy.cmd import prod_canary

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


def _seed(repo, size_bytes, count, prefix="f"):
    for i in range(count):
        repo.add_pool_file(
            PoolFileKind.URL,
            f"https://archive.org/download/i/{prefix}{i}.bin",
            f"{prefix}{i}.bin",
            size_bytes=size_bytes,
        )


def _args(pool="small:3", name="PROD-CANARY-04"):
    return argparse.Namespace(name=name, pool=pool)


def _flat(text):
    return " ".join(text.split())


class TestSelection:
    def test_produces_a_flat_url_list(self, wired, capsys):
        from rich.console import Console

        _seed(wired, 50 * MIB, 5)
        content, selected = prod_canary._select_canary_pool_files(_args(), Console())
        lines = [line for line in content.splitlines() if line]
        assert len(lines) == 3
        assert all(line.startswith("https://archive.org/") for line in lines)
        # Same shape the command already distributes, so provisioning and the
        # VM-side fetch never learn the pool exists.
        assert content.endswith("\n")
        assert len(selected) == 3

    def test_claims_are_attributed_to_the_canary(self, wired):
        from rich.console import Console

        _seed(wired, 50 * MIB, 5)
        prod_canary._select_canary_pool_files(_args(), Console())
        claims = wired.list_pool_claims()
        assert len(claims) == 3
        assert all(c.source is ClaimSource.PROD_CANARY for c in claims)

    def test_large_selection_is_summarised_not_listed(self, wired, capsys):
        # A real canary draws ~350 files. Listing them would bury the rest of
        # the deploy output entirely.
        from rich.console import Console

        _seed(wired, 50 * MIB, 60)
        prod_canary._select_canary_pool_files(_args("small:50"), Console())
        out = capsys.readouterr().out
        assert "50 file(s)" in out
        assert out.count("\n") < 15

    def test_resume_reuses_the_selection(self, wired, fake_s3):
        from rich.console import Console

        _seed(wired, 50 * MIB, 20)
        first, _ = prod_canary._select_canary_pool_files(_args(), Console())
        second, _ = prod_canary._select_canary_pool_files(_args(), Console())
        assert first == second
        # A retried canary must not consume another set.
        assert len(wired.list_pool_claims()) == 3

    def test_shortfall_exits_before_infrastructure(self, wired, capsys):
        from rich.console import Console

        _seed(wired, 50 * MIB, 2)
        with pytest.raises(SystemExit):
            prod_canary._select_canary_pool_files(_args("small:350"), Console())
        out = _flat(capsys.readouterr().out)
        assert "url pool cannot satisfy" in out
        assert "small 350 requested, 2 available" in out
        assert "No infrastructure was created" in out

    def test_missing_database_exits(self, monkeypatch, capsys, fake_s3):
        from rich.console import Console

        monkeypatch.setattr("saorsa_deploy.registry.get_file_pool_repository", lambda: None)
        with pytest.raises(SystemExit):
            prod_canary._select_canary_pool_files(_args(), Console())
        assert "--pool needs DATABASE_URL" in _flat(capsys.readouterr().out)

    def test_mixed_size_request_is_honoured(self, wired):
        from rich.console import Console

        _seed(wired, 5 * MIB, 5, "tiny")
        _seed(wired, 50 * MIB, 5, "small")
        _seed(wired, 500 * MIB, 5, "medium")
        content, _ = prod_canary._select_canary_pool_files(
            _args("tiny:2,small:3,medium:1"), Console()
        )
        assert len([line for line in content.splitlines() if line]) == 6


class TestManifest:
    def _spec(self, **kwargs):
        from saorsa_deploy.config.models import Binary, ProdCanarySpec, ProdProbe

        base = dict(
            binary=Binary(version="0.5.2"),
            upload_spec="lon1:s-4vcpu-8gb",
            probes=[ProdProbe(region="lon1", count=2)],
        )
        base.update(kwargs)
        return ProdCanarySpec(**base)

    def test_exactly_one_file_source_is_required(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="exactly one of"):
            self._spec()
        with pytest.raises(ValidationError, match="exactly one of"):
            self._spec(urls_file="/tmp/l.txt", pool={"small": 1})

    def test_pool_maps_to_the_cli_flag(self):
        from saorsa_deploy.config.models import Manifest
        from saorsa_deploy.orchestrator.prod import _canary

        manifest = Manifest(
            kind="prod-canary",
            name="PROD-CANARY-04",
            purpose="loss measurement",
            prod_canary=self._spec(pool={"medium": 110, "tiny": 40, "small": 230}),
        )
        argv = _canary(manifest)[0].argv
        assert "--pool" in argv
        # Smallest-first, so a re-plan produces identical argv and the step
        # journal does not report a spurious change on resume.
        assert argv[argv.index("--pool") + 1] == "tiny:40,small:230,medium:110"

    def test_urls_file_still_maps(self):
        from saorsa_deploy.config.models import Manifest
        from saorsa_deploy.orchestrator.prod import _canary

        manifest = Manifest(
            kind="prod-canary",
            name="PROD-CANARY-04",
            purpose="loss measurement",
            prod_canary=self._spec(urls_file="/tmp/list.txt"),
        )
        argv = _canary(manifest)[0].argv
        assert "--urls-file" in argv
        assert "--pool" not in argv
