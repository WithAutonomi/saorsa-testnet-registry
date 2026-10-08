"""Partially-uploaded files: available again, but not untouched.

An upload that stored some chunks and stopped is neither a success nor a
non-event. The file is not on the network in full, so it goes back in the pool —
completing it is useful work. But chunks of it ARE in production, which is why
it cannot simply be `released`, and why a canary must not draw it: a loss
measurement over content that is already partly stored, possibly for months, is
measuring something other than what it reports.
"""

import argparse
from datetime import datetime, timezone

import pytest
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import (
    BLOCKING_STATUSES,
    NON_BLOCKING_STATUSES,
    ClaimSource,
    ClaimStatus,
    PoolFileKind,
    ProdFilePoolClaim,
)
from saorsa_db.repositories import (
    POOL_STATUS_AVAILABLE,
    POOL_STATUS_PARTIAL,
    InsufficientPoolInventory,
    SqlAlchemyProdFilePoolRepository,
)
from saorsa_db.size_class import SizeClass
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saorsa_deploy import pool_selection

MIB = 1024 * 1024
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


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


def _seed(repo, count, size=50 * MIB, prefix="f"):
    made = []
    for i in range(count):
        pool_file, _ = repo.add_pool_file(
            PoolFileKind.URL,
            f"https://example.org/{prefix}{i}.bin",
            f"{prefix}{i}.bin",
            size_bytes=size,
        )
        made.append(pool_file)
    return made


def _claim_and_partial(repo, **kwargs):
    """Draw a file and mark it partial. Which file is up to the selector."""
    claim = repo.claim_pool_files(
        PoolFileKind.URL, {SizeClass.SMALL: 1}, ClaimSource.PROD_UPLOADERS, when=NOW
    )[0]
    return repo.mark_pool_claim_partial(claim.id, **kwargs)


def _partial_specific(repo, pool_file, **kwargs):
    """Mark one NAMED file partial.

    Selection shuffles, so drawing twice can hand back the same file — which is
    correct behaviour but useless for a test that needs a known number of
    distinct partials.
    """
    claim = repo.seed_uploaded_claim(
        pool_file.id,
        source=ClaimSource.PROD_UPLOADERS,
        status=ClaimStatus.PARTIAL,
        when=NOW,
        **kwargs,
    )
    assert claim is not None
    return claim


class TestStatusSets:
    def test_partial_does_not_block(self):
        assert ClaimStatus.PARTIAL in NON_BLOCKING_STATUSES
        assert ClaimStatus.PARTIAL not in BLOCKING_STATUSES

    def test_the_two_sets_stay_disjoint_and_complete(self):
        # If they drift apart, the database index and the availability query
        # stop agreeing about what "available" means.
        assert not (BLOCKING_STATUSES & NON_BLOCKING_STATUSES)
        assert BLOCKING_STATUSES | NON_BLOCKING_STATUSES == set(ClaimStatus)


class TestSchema:
    def test_a_partial_claim_permits_a_later_claim(self, repo):
        files = _seed(repo, 1)
        _claim_and_partial(repo)
        again = repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.SMALL: 1}, ClaimSource.PROD_UPLOADERS
        )
        assert len(again) == 1
        assert again[0].file_id == files[0].id

    def test_two_open_claims_are_still_refused_after_a_partial(self, repo):
        _seed(repo, 1)
        _claim_and_partial(repo)
        repo.claim_pool_files(PoolFileKind.URL, {SizeClass.SMALL: 1}, ClaimSource.PROD_UPLOADERS)
        # The second live claim is what the index must still stop.
        with pytest.raises(IntegrityError):
            repo.session.add(
                ProdFilePoolClaim(
                    file_id=repo.list_pool_files()[0].id,
                    source=ClaimSource.MANUAL,
                    status=ClaimStatus.CLAIMED,
                    claimed_at=NOW,
                )
            )
            repo.session.flush()


class TestAvailability:
    def test_a_partial_file_is_available_again(self, repo):
        files = _seed(repo, 1)
        _claim_and_partial(repo)
        available = repo.list_pool_files(status=POOL_STATUS_AVAILABLE)
        assert files[0].id in {f.id for f in available}

    def test_partial_status_filter_finds_it(self, repo):
        files = _seed(repo, 2)
        _claim_and_partial(repo)
        partial = repo.list_pool_files(status=POOL_STATUS_PARTIAL)
        assert len(partial) == 1
        assert partial[0].id in {f.id for f in files}

    def test_chunk_progress_is_kept(self, repo):
        # "342 of 400 chunks" is the useful fact; "it failed" is not.
        _seed(repo, 1)
        claim = _claim_and_partial(
            repo,
            chunks_stored=342,
            chunks_failed=58,
            total_chunks=400,
            address="a7f3c2",
        )
        assert claim.status is ClaimStatus.PARTIAL
        assert (claim.chunks_stored, claim.total_chunks) == (342, 400)
        assert claim.address == "a7f3c2"

    def test_an_uploaded_claim_cannot_be_downgraded(self, repo):
        _seed(repo, 1)
        claim = repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.SMALL: 1}, ClaimSource.PROD_UPLOADERS
        )[0]
        repo.resolve_pool_claim(claim.id, address="a7f3c2")
        with pytest.raises(ValueError, match="already recorded as fully uploaded"):
            repo.mark_pool_claim_partial(claim.id)


class TestSelectionExclusion:
    def test_uploaders_may_draw_a_partial_file(self, repo):
        # Completing it is useful work.
        _seed(repo, 1)
        _claim_and_partial(repo)
        claims = repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.SMALL: 1}, ClaimSource.PROD_UPLOADERS
        )
        assert len(claims) == 1

    def test_exclude_partial_skips_it(self, repo):
        _seed(repo, 1)
        _claim_and_partial(repo)
        with pytest.raises(InsufficientPoolInventory):
            repo.claim_pool_files(
                PoolFileKind.URL,
                {SizeClass.SMALL: 1},
                ClaimSource.PROD_CANARY,
                exclude_partial=True,
            )

    def test_exclude_partial_still_draws_untouched_files(self, repo):
        files = _seed(repo, 3)
        _partial_specific(repo, files[0])
        partial_ids = {files[0].id}
        claims = repo.claim_pool_files(
            PoolFileKind.URL,
            {SizeClass.SMALL: 2},
            ClaimSource.PROD_CANARY,
            exclude_partial=True,
        )
        assert len(claims) == 2
        assert not ({c.file_id for c in claims} & partial_ids)
        assert {c.file_id for c in claims} <= {f.id for f in files}

    def test_shortfall_counts_exclude_partials(self, repo):
        # The count in the error must reflect what the canary can actually
        # draw, not what exists.
        files = _seed(repo, 3)
        for pool_file in files[:2]:
            _partial_specific(repo, pool_file)
        with pytest.raises(InsufficientPoolInventory) as exc:
            repo.claim_pool_files(
                PoolFileKind.URL,
                {SizeClass.SMALL: 3},
                ClaimSource.PROD_CANARY,
                exclude_partial=True,
            )
        assert exc.value.shortfall["small"]["available"] == 1


class TestSelectionEngineWiring:
    def test_canary_excludes_partials_by_construction(self, repo, fake_s3):
        # Not a flag a manifest can set: a contaminated canary must not be
        # expressible.
        _seed(repo, 1)
        _claim_and_partial(repo)
        with pytest.raises(pool_selection.PoolSelectionError):
            pool_selection.select_for_deployment(
                repo,
                name="PROD-CANARY-04",
                slot="canary-upload",
                kind=PoolFileKind.URL,
                request={SizeClass.SMALL: 1},
                source=ClaimSource.PROD_CANARY,
            )

    def test_uploaders_are_not_excluded(self, repo, fake_s3):
        _seed(repo, 1)
        _claim_and_partial(repo)
        selected = pool_selection.select_for_deployment(
            repo,
            name="PROD-UL-06",
            slot="uploader-1",
            kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 1},
            source=ClaimSource.PROD_UPLOADERS,
        )
        assert len(selected) == 1


class TestImportHistory:
    def test_partial_history_seeds_a_partial_claim(self, repo, monkeypatch, capsys):
        from saorsa_db.models.deployment import Deployment
        from saorsa_db.models.prod_upload_result import (
            DeploymentProdUploadResult,
            ProdUploadStatus,
        )
        from saorsa_db.repositories import SqlAlchemyDeploymentRepository

        from saorsa_deploy.cmd import prod_pool

        deployment_repo = SqlAlchemyDeploymentRepository(repo.session)
        monkeypatch.setattr(prod_pool, "get_file_pool_repository", lambda: repo)
        monkeypatch.setattr("saorsa_deploy.registry.get_repository", lambda: deployment_repo)

        pool_file, _ = repo.add_pool_file(
            PoolFileKind.URL, "https://example.org/a.bin", "a.bin", size_bytes=50 * MIB
        )
        deployment = Deployment(name="PROD-UL-05", deploy_args={})
        repo.session.add(deployment)
        repo.session.commit()
        repo.session.add(
            DeploymentProdUploadResult(
                deployment_id=deployment.id,
                client_hostname="host-1",
                region="lon1",
                source_url="https://example.org/a.bin",
                file_name="a.bin",
                file_size_bytes=50 * MIB,
                status=ProdUploadStatus.PARTIAL_UPLOAD,
                chunks_stored=342,
                total_chunks=400,
                started_at=NOW,
                finished_at=NOW,
            )
        )
        repo.session.commit()

        prod_pool.cmd_prod_pool_import_history(argparse.Namespace(dry_run=False))

        claim = repo.list_pool_claims()[0]
        assert claim.status is ClaimStatus.PARTIAL
        assert claim.chunks_stored == 342
        # Still available — it was never fully uploaded.
        assert pool_file.id in {f.id for f in repo.list_pool_files(status=POOL_STATUS_AVAILABLE)}
        out = " ".join(capsys.readouterr().out.split())
        assert "part-uploaded" in out
        assert "excluded from canaries" in out


class TestListAndShowRendering:
    """A part-uploaded file must not look identical to an untouched one.

    Partial claims do not block, so they are invisible to the blocking-claim
    lookup that drives both views. Without an explicit fallback, `list` shows
    `available` and `show` renders the file as pristine — no chunk counts, no
    hint that anything was ever stored.
    """

    def _wired(self, repo, monkeypatch):
        from saorsa_deploy.cmd import prod_pool

        monkeypatch.setattr(prod_pool, "get_file_pool_repository", lambda: repo)
        return prod_pool

    def _list_args(self, **kwargs):
        base = dict(status=None, kind=None, size=None, tag=None, since=None, limit=None, all=False)
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_list_marks_a_partial_file_with_an_asterisk(self, repo, monkeypatch, capsys):
        prod_pool = self._wired(repo, monkeypatch)
        files = _seed(repo, 2)
        _partial_specific(repo, files[0], chunks_stored=342, total_chunks=400)

        prod_pool.cmd_prod_pool_list(self._list_args())
        out = capsys.readouterr().out
        assert "available*" in out
        assert "partially uploaded" in " ".join(out.split())

    def test_the_legend_only_appears_when_relevant(self, repo, monkeypatch, capsys):
        prod_pool = self._wired(repo, monkeypatch)
        _seed(repo, 2)
        prod_pool.cmd_prod_pool_list(self._list_args())
        out = " ".join(capsys.readouterr().out.split())
        assert "*" not in out.replace("**", "")
        assert "partially uploaded" not in out

    def test_a_fully_uploaded_file_is_not_marked(self, repo, monkeypatch, capsys):
        # Once it is on the network the partial history is superseded; flagging
        # it would only confuse.
        prod_pool = self._wired(repo, monkeypatch)
        files = _seed(repo, 1)
        _partial_specific(repo, files[0])
        claim = repo.claim_pool_files(
            PoolFileKind.URL, {SizeClass.SMALL: 1}, ClaimSource.PROD_UPLOADERS
        )[0]
        repo.resolve_pool_claim(claim.id, address="a7f3c2")

        prod_pool.cmd_prod_pool_list(self._list_args())
        out = capsys.readouterr().out
        assert "uploaded" in out
        assert "uploaded*" not in out

    def test_show_surfaces_the_earlier_partial(self, repo, monkeypatch, capsys):
        prod_pool = self._wired(repo, monkeypatch)
        files = _seed(repo, 1)
        _partial_specific(repo, files[0], chunks_stored=342, total_chunks=400, address="a7f3c2")

        prod_pool.cmd_prod_pool_show(argparse.Namespace(file_id=files[0].id, address=None))
        out = " ".join(capsys.readouterr().out.split())
        assert "partially uploaded before" in out
        assert "342 of 400 chunk(s) stored" in out
        assert "excluded from canary selection" in out

    def test_show_is_unchanged_for_an_untouched_file(self, repo, monkeypatch, capsys):
        prod_pool = self._wired(repo, monkeypatch)
        files = _seed(repo, 1)
        prod_pool.cmd_prod_pool_show(argparse.Namespace(file_id=files[0].id, address=None))
        out = " ".join(capsys.readouterr().out.split())
        assert "status available" in out
        assert "partially uploaded" not in out


class TestExplicitExclusion:
    """Uploaders can ask for pristine files; canaries always get them.

    A canary must never draw a partial — that is enforced by source, not by a
    flag. An uploader run may legitimately want either: completing a partial is
    useful work, but a run testing whether an upload can be *sustained* over a
    slow link learns nothing from a file that is 85% uploaded, because ant
    sends only the chunks that are missing.
    """

    def test_uploaders_can_opt_out_of_partials(self, repo, fake_s3):
        files = _seed(repo, 3)
        _partial_specific(repo, files[0])
        selected = pool_selection.select_for_deployment(
            repo,
            name="PROD-UL-07",
            slot="uploader-1",
            kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 2},
            source=ClaimSource.PROD_UPLOADERS,
            exclude_partial=True,
        )
        assert files[0].id not in {entry.file_id for entry in selected}

    def test_uploaders_still_draw_partials_by_default(self, repo, fake_s3):
        _seed(repo, 1)
        _partial_specific(repo, repo.list_pool_files()[0])
        selected = pool_selection.select_for_deployment(
            repo,
            name="PROD-UL-08",
            slot="uploader-1",
            kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 1},
            source=ClaimSource.PROD_UPLOADERS,
        )
        assert len(selected) == 1

    def test_the_flag_cannot_re_enable_partials_for_a_canary(self, repo, fake_s3):
        _seed(repo, 1)
        _partial_specific(repo, repo.list_pool_files()[0])
        with pytest.raises(pool_selection.PoolSelectionError):
            pool_selection.select_for_deployment(
                repo,
                name="PROD-CANARY-05",
                slot="canary-upload",
                kind=PoolFileKind.URL,
                request={SizeClass.SMALL: 1},
                source=ClaimSource.PROD_CANARY,
                exclude_partial=False,
            )

    def test_manifest_maps_to_the_spec_token(self):
        from saorsa_deploy.config.models import ProdUploader
        from saorsa_deploy.orchestrator.prod import _prod_uploader_spec

        uploader = ProdUploader(
            region="lon1",
            vm_size="s-4vcpu-8gb",
            pool={"medium": 1},
            exclude_partial=True,
        )
        assert _prod_uploader_spec(uploader) == (
            "lon1:s-4vcpu-8gb:pool=medium:1:exclude_partial=true"
        )

    def test_spec_token_round_trips(self):
        from saorsa_deploy.cmd.prod_uploaders import parse_prod_uploader_spec

        spec = parse_prod_uploader_spec("lon1:s-4vcpu-8gb:pool=medium:1:exclude_partial=true")
        assert spec["exclude_partial"] is True

    def test_absent_token_defaults_to_false(self):
        from saorsa_deploy.cmd.prod_uploaders import parse_prod_uploader_spec

        spec = parse_prod_uploader_spec("lon1:s-4vcpu-8gb:pool=medium:1")
        assert spec["exclude_partial"] is False
