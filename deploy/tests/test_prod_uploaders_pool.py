"""Tests for wiring `prod uploaders` to the file pool.

The wiring has to leave everything downstream untouched: the chosen URLs are
written into the same list the VMs already fetch, so provisioning, the VM-side
download and the recorded file names never learn that a pool exists. What
changes is only where the list comes from — and, critically, that it cannot
contain a file that has already been uploaded.
"""

import argparse

import pytest
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import ClaimSource, PoolFileKind
from saorsa_db.repositories import SqlAlchemyProdFilePoolRepository
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy import pool_selection
from saorsa_deploy.cmd import prod_uploaders
from saorsa_deploy.cmd.prod_uploaders import parse_prod_uploader_spec

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


class TestSpecParsing:
    def test_pool_token_is_parsed(self):
        from saorsa_db.size_class import SizeClass

        spec = parse_prod_uploader_spec("lon1:s-4vcpu-8gb:pool=small:3,medium:2")
        assert spec["pool_request"] == {SizeClass.SMALL: 3, SizeClass.MEDIUM: 2}
        assert spec["urls_file"] is None

    def test_urls_token_still_works(self):
        spec = parse_prod_uploader_spec("lon1:s-4vcpu-8gb:urls=/tmp/list.txt")
        assert spec["urls_file"] == "/tmp/list.txt"
        assert spec["pool_request"] is None

    def test_both_sources_is_rejected(self):
        with pytest.raises(ValueError, match="not both"):
            parse_prod_uploader_spec("lon1:s-4vcpu-8gb:urls=/tmp/l.txt:pool=small:1")

    def test_neither_source_is_rejected(self):
        with pytest.raises(ValueError, match="One of urls=PATH or pool="):
            parse_prod_uploader_spec("lon1:s-4vcpu-8gb:payment=merkle")

    def test_bad_pool_token_names_the_expected_form(self):
        with pytest.raises(ValueError, match="Expected class:count"):
            parse_prod_uploader_spec("lon1:s-4vcpu-8gb:pool=small")

    def test_pool_coexists_with_other_tokens(self):
        spec = parse_prod_uploader_spec(
            "lon1:s-4vcpu-8gb:pool=small:2:payment=merkle:rust_log=a::b=debug"
        )
        assert spec["payment_type"] == "merkle"
        assert spec["rust_log"] == "a::b=debug"
        assert spec["pool_request"]


class TestSelectionAtDeploy:
    def _args(self, name="PROD-UL-05"):
        return argparse.Namespace(name=name)

    def test_selected_urls_become_the_upload_list(self, wired, capsys):
        _seed(wired, 50 * MIB, 5)
        specs = [parse_prod_uploader_spec("lon1:s-4vcpu-8gb:pool=small:3")]
        from rich.console import Console

        prod_uploaders._select_pool_files(self._args(), specs, Console())

        lines = [line for line in specs[0]["_urls_file_content"].splitlines() if line]
        assert len(lines) == 3
        assert all(line.startswith("https://archive.org/") for line in lines)
        # Downstream code path is unchanged: the same helper that reads a
        # hand-written list now reads this one.
        assert len(prod_uploaders._extract_file_names(specs[0]["_urls_file_content"])) == 3

    def test_each_uploader_gets_its_own_files(self, wired):
        from rich.console import Console

        _seed(wired, 50 * MIB, 6)
        specs = [
            parse_prod_uploader_spec("lon1:s-4vcpu-8gb:pool=small:2"),
            parse_prod_uploader_spec("nyc3:s-4vcpu-8gb:pool=small:2"),
        ]
        prod_uploaders._select_pool_files(self._args(), specs, Console())
        first = set(specs[0]["_urls_file_content"].split())
        second = set(specs[1]["_urls_file_content"].split())
        assert first and second
        assert not first & second

    def test_resume_reuses_the_same_files(self, wired, fake_s3):
        from rich.console import Console

        _seed(wired, 50 * MIB, 10)
        specs = [parse_prod_uploader_spec("lon1:s-4vcpu-8gb:pool=small:3")]
        prod_uploaders._select_pool_files(self._args(), specs, Console())
        first = specs[0]["_urls_file_content"]

        again = [parse_prod_uploader_spec("lon1:s-4vcpu-8gb:pool=small:3")]
        prod_uploaders._select_pool_files(self._args(), again, Console())

        assert again[0]["_urls_file_content"] == first
        # A retried deploy must not burn a second set of files.
        assert len(wired.list_pool_claims()) == 3

    def test_shortfall_exits_before_infrastructure(self, wired, capsys):
        from rich.console import Console

        _seed(wired, 50 * MIB, 1)
        specs = [parse_prod_uploader_spec("lon1:s-4vcpu-8gb:pool=small:5")]
        with pytest.raises(SystemExit):
            prod_uploaders._select_pool_files(self._args(), specs, Console())
        out = " ".join(capsys.readouterr().out.split())
        assert "url pool cannot satisfy" in out
        assert "No infrastructure was created" in out

    def test_missing_database_refuses_rather_than_degrading(self, monkeypatch, capsys, fake_s3):
        # The old name-based duplicate check warned and carried on. This cannot:
        # without the pool there is no list of files to upload at all.
        from rich.console import Console

        monkeypatch.setattr("saorsa_deploy.registry.get_file_pool_repository", lambda: None)
        specs = [parse_prod_uploader_spec("lon1:s-4vcpu-8gb:pool=small:1")]
        with pytest.raises(SystemExit):
            prod_uploaders._select_pool_files(self._args(), specs, Console())
        out = " ".join(capsys.readouterr().out.split())
        assert "no DATABASE_URL is configured" in out

    def test_urls_only_specs_need_no_database(self, monkeypatch, fake_s3):
        from rich.console import Console

        monkeypatch.setattr("saorsa_deploy.registry.get_file_pool_repository", lambda: None)
        specs = [parse_prod_uploader_spec("lon1:s-4vcpu-8gb:urls=/tmp/l.txt")]
        assert (
            prod_uploaders._select_pool_files(argparse.Namespace(name="X"), specs, Console()) == []
        )

    def test_claims_are_attributed_to_the_deployment_afterwards(self, wired, fake_s3):
        from rich.console import Console
        from saorsa_db.models.deployment import Deployment

        _seed(wired, 50 * MIB, 3)
        specs = [parse_prod_uploader_spec("lon1:s-4vcpu-8gb:pool=small:2")]
        selections = prod_uploaders._select_pool_files(self._args(), specs, Console())
        deployment = Deployment(name="PROD-UL-05", deploy_args={})
        wired.session.add(deployment)
        wired.session.commit()

        prod_uploaders._attach_pool_claims(selections, deployment.id, "PROD-UL-05")
        rows = wired.list_pool_claims(deployment_id=deployment.id)
        assert len(rows) == 2
        assert all(r.source is ClaimSource.PROD_UPLOADERS for r in rows)


class TestManifestMapping:
    def test_pool_becomes_a_spec_token(self):
        from saorsa_deploy.config.models import ProdUploader
        from saorsa_deploy.orchestrator.prod import _prod_uploader_spec

        uploader = ProdUploader(
            region="lon1", vm_size="s-4vcpu-8gb", pool={"medium": 2, "small": 3}
        )
        # Smallest-first regardless of mapping order: the step journal compares
        # argv, so an unstable ordering would make a resumed run look changed.
        assert _prod_uploader_spec(uploader) == ("lon1:s-4vcpu-8gb:pool=small:3,medium:2")

    def test_urls_still_maps(self):
        from saorsa_deploy.config.models import ProdUploader
        from saorsa_deploy.orchestrator.prod import _prod_uploader_spec

        uploader = ProdUploader(region="lon1", vm_size="s-4vcpu-8gb", urls="/tmp/list.txt")
        assert _prod_uploader_spec(uploader) == "lon1:s-4vcpu-8gb:urls=/tmp/list.txt"

    def test_manifest_requires_exactly_one_source(self):
        from pydantic import ValidationError

        from saorsa_deploy.config.models import ProdUploader

        with pytest.raises(ValidationError, match="exactly one of"):
            ProdUploader(region="lon1", vm_size="s", urls="/tmp/l", pool={"small": 1})
        with pytest.raises(ValidationError, match="exactly one of"):
            ProdUploader(region="lon1", vm_size="s")

    def test_spec_round_trips_through_the_parser(self):
        from saorsa_db.size_class import SizeClass

        from saorsa_deploy.config.models import ProdUploader
        from saorsa_deploy.orchestrator.prod import _prod_uploader_spec

        uploader = ProdUploader(
            region="lon1", vm_size="s-4vcpu-8gb", pool={"small": 3, "medium": 2}
        )
        parsed = parse_prod_uploader_spec(_prod_uploader_spec(uploader))
        assert parsed["pool_request"] == {SizeClass.SMALL: 3, SizeClass.MEDIUM: 2}
