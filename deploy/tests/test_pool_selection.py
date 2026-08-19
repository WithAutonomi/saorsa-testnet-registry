"""Tests for the shared pool selection engine.

Resume safety is the behaviour that would be most expensive to get wrong.
`deployments up` retries a failed run, and if selection drew fresh files each
time, a canary retried twice would burn ~1050 files to produce one 350-file
experiment — with the first two sets left claimed and unusable.
"""

import pytest
from saorsa_db.db import Base
from saorsa_db.models.prod_file_pool import ClaimSource, PoolFileKind
from saorsa_db.repositories import SqlAlchemyProdFilePoolRepository
from saorsa_db.size_class import SizeClass
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from saorsa_deploy import pool_selection
from saorsa_deploy.pool_selection import (
    PoolSelectionError,
    describe_request,
    parse_request,
    parse_request_token,
    select_for_deployment,
)

MIB = 1024 * 1024
GIB = 1024 * MIB


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield SqlAlchemyProdFilePoolRepository(session)


@pytest.fixture
def fake_s3(monkeypatch):
    """In-memory stand-in for the S3 selection store."""
    store: dict = {}
    monkeypatch.setattr(
        pool_selection, "load_pool_selection", lambda name: store.get(name)
    )
    monkeypatch.setattr(
        pool_selection,
        "save_pool_selection",
        lambda name, selection: store.__setitem__(name, selection),
    )
    return store


def _seed(repo, kind, size_bytes, count, prefix="f"):
    for i in range(count):
        repo.add_pool_file(
            kind,
            f"https://example.org/{prefix}{i}"
            if kind is PoolFileKind.URL
            else f"/data/{prefix}{i}",
            f"{prefix}{i}.bin",
            size_bytes=size_bytes,
        )


class TestParseRequest:
    def test_maps_names_to_classes(self):
        assert parse_request({"small": 3, "medium": 2}) == {
            SizeClass.SMALL: 3,
            SizeClass.MEDIUM: 2,
        }

    def test_zero_counts_are_dropped(self):
        assert parse_request({"small": 3, "large": 0}) == {SizeClass.SMALL: 3}

    def test_all_zero_is_rejected(self):
        # Silently selecting nothing would produce an empty upload run that
        # looks like it succeeded.
        with pytest.raises(PoolSelectionError, match="zero files"):
            parse_request({"small": 0})

    def test_empty_request_is_rejected(self):
        with pytest.raises(PoolSelectionError, match="empty pool request"):
            parse_request({})

    def test_unknown_class_is_rejected(self):
        with pytest.raises(ValueError, match="unknown size class"):
            parse_request({"huge": 1})

    def test_non_numeric_count_is_rejected(self):
        with pytest.raises(PoolSelectionError, match="whole number"):
            parse_request({"small": "many"})

    def test_negative_count_is_rejected(self):
        with pytest.raises(PoolSelectionError, match="cannot be negative"):
            parse_request({"small": -1})

    def test_string_counts_are_accepted(self):
        # Manifest YAML and CLI tokens both arrive as strings.
        assert parse_request({"small": "3"}) == {SizeClass.SMALL: 3}


class TestParseRequestToken:
    def test_parses_the_cli_form(self):
        assert parse_request_token("small:3,medium:2") == {
            SizeClass.SMALL: 3,
            SizeClass.MEDIUM: 2,
        }

    def test_tolerates_whitespace(self):
        assert parse_request_token(" small : 3 , tiny : 1 ") == {
            SizeClass.SMALL: 3,
            SizeClass.TINY: 1,
        }

    def test_missing_colon_is_rejected(self):
        with pytest.raises(PoolSelectionError, match="Expected class:count"):
            parse_request_token("small3")


class TestDescribeRequest:
    def test_renders_smallest_first_regardless_of_input_order(self):
        request = {SizeClass.MEDIUM: 2, SizeClass.TINY: 5}
        assert describe_request(request) == "tiny 5, medium 2"


class TestSelection:
    def test_claims_the_requested_files(self, repo, fake_s3):
        _seed(repo, PoolFileKind.URL, 50 * MIB, 5)
        selected = select_for_deployment(
            repo,
            name="PROD-UL-05",
            slot="uploader-1",
            kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 3},
            source=ClaimSource.PROD_UPLOADERS,
        )
        assert len(selected) == 3
        assert all(entry.claim_id is not None for entry in selected)
        assert len(repo.list_pool_files(status="available")) == 2

    def test_selection_is_persisted(self, repo, fake_s3):
        _seed(repo, PoolFileKind.URL, 50 * MIB, 5)
        select_for_deployment(
            repo,
            name="PROD-UL-05",
            slot="uploader-1",
            kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 3},
            source=ClaimSource.PROD_UPLOADERS,
        )
        assert "uploader-1" in fake_s3["PROD-UL-05"]["slots"]

    def test_resume_returns_the_same_files_without_claiming_more(self, repo, fake_s3):
        # The behaviour that stops a retried deploy burning inventory.
        _seed(repo, PoolFileKind.URL, 50 * MIB, 10)
        first = select_for_deployment(
            repo, name="PROD-UL-05", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 3}, source=ClaimSource.PROD_UPLOADERS,
        )
        second = select_for_deployment(
            repo, name="PROD-UL-05", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 3}, source=ClaimSource.PROD_UPLOADERS,
        )
        assert [e.file_id for e in first] == [e.file_id for e in second]
        assert len(repo.list_pool_claims()) == 3
        assert len(repo.list_pool_files(status="available")) == 7

    def test_separate_slots_draw_separately(self, repo, fake_s3):
        # One uploader VM per slot; each keeps its own files across a resume.
        _seed(repo, PoolFileKind.URL, 50 * MIB, 10)
        first = select_for_deployment(
            repo, name="PROD-UL-05", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 2}, source=ClaimSource.PROD_UPLOADERS,
        )
        second = select_for_deployment(
            repo, name="PROD-UL-05", slot="uploader-2", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 2}, source=ClaimSource.PROD_UPLOADERS,
        )
        assert not {e.file_id for e in first} & {e.file_id for e in second}
        assert len(repo.list_pool_claims()) == 4

    def test_a_spent_draw_is_not_reused_by_the_next_run_of_the_same_name(
        self, repo, fake_s3
    ):
        """The STG-UL-01 regression (2026-08-18).

        Prod deployment names are reused every release train, and the draw is
        persisted under the name alone. Run N finished and uploaded its files;
        run N+1 must not be handed them back, because they are on the
        production network and re-uploading them costs real money to test the
        already-stored path.
        """
        _seed(repo, PoolFileKind.URL, 50 * MIB, 10)
        first = select_for_deployment(
            repo, name="STG-UL-01", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 2}, source=ClaimSource.PROD_UPLOADERS,
        )
        # The run completes: record-results resolves every claim.
        for entry in first:
            repo.resolve_pool_claim(entry.claim_id, address=f"addr-{entry.file_id}")

        second = select_for_deployment(
            repo, name="STG-UL-01", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 2}, source=ClaimSource.PROD_UPLOADERS,
        )

        assert not {e.file_id for e in first} & {e.file_id for e in second}
        assert len(second) == 2

    def test_a_released_draw_is_not_reused(self, repo, fake_s3):
        # Released means the file went back to the pool and may since have been
        # drawn by someone else, so this run cannot assume it still holds it.
        _seed(repo, PoolFileKind.URL, 50 * MIB, 10)
        first = select_for_deployment(
            repo, name="STG-UL-01", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 2}, source=ClaimSource.PROD_UPLOADERS,
        )
        for entry in first:
            repo.release_pool_claim(entry.claim_id)

        second = select_for_deployment(
            repo, name="STG-UL-01", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 2}, source=ClaimSource.PROD_UPLOADERS,
        )
        assert len(second) == 2
        assert all(
            repo.blocking_claims_for_files([e.file_id])[e.file_id].id == e.claim_id
            for e in second
        )

    def test_a_stale_draw_does_not_leave_the_slot_behind(self, repo, fake_s3):
        # The fresh draw must replace the spent one in S3, or every subsequent
        # run re-does the staleness check against a draw that will never again
        # be valid.
        _seed(repo, PoolFileKind.URL, 50 * MIB, 10)
        first = select_for_deployment(
            repo, name="STG-UL-01", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 2}, source=ClaimSource.PROD_UPLOADERS,
        )
        for entry in first:
            repo.resolve_pool_claim(entry.claim_id, address="a")
        second = select_for_deployment(
            repo, name="STG-UL-01", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 2}, source=ClaimSource.PROD_UPLOADERS,
        )
        stored = fake_s3["STG-UL-01"]["slots"]["uploader-1"]["files"]
        stored_ids = [e["file_id"] for e in stored]
        assert stored_ids == [e.file_id for e in second]
        # The spent draw is gone, not merely shadowed.
        assert not set(stored_ids) & {e.file_id for e in first}

    def test_a_draw_with_no_claim_ids_is_not_trusted(self, repo, fake_s3):
        # Hand-written or pre-claim-id state: ownership is unprovable, and
        # assuming it is ours is the failure the check exists to prevent.
        _seed(repo, PoolFileKind.URL, 50 * MIB, 10)
        fake_s3["STG-UL-01"] = {
            "slots": {
                "uploader-1": {
                    "kind": "url",
                    "request": {"small": 2},
                    "min_size_bytes": None,
                    "max_size_bytes": None,
                    "files": [
                        {"file_id": 1, "name": "f0.bin",
                         "locator": "https://example.org/f0", "size_bytes": 50 * MIB},
                    ],
                }
            }
        }
        selected = select_for_deployment(
            repo, name="STG-UL-01", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 2}, source=ClaimSource.PROD_UPLOADERS,
        )
        assert len(selected) == 2
        assert all(e.claim_id is not None for e in selected)

    def test_records_the_reachable_locator(self, repo, fake_s3):
        # A duplicate group can hold both a local path and a URL; a remote
        # uploader needs the one it can actually fetch.
        repo.add_pool_file(
            PoolFileKind.LOCAL, "/data/film.mkv", "film.mkv",
            size_bytes=50 * MIB, digests={"sha1": "aaa"},
        )
        repo.add_pool_file(
            PoolFileKind.URL, "https://archive.org/download/i/film.mkv", "film.mkv",
            size_bytes=50 * MIB, digests={"sha1": "aaa"},
        )
        selected = select_for_deployment(
            repo, name="PROD-UL-05", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 1}, source=ClaimSource.PROD_UPLOADERS,
        )
        assert selected[0].locator.startswith("https://archive.org/")


class TestShortfall:
    def test_names_the_inventory_and_the_remedy(self, repo, fake_s3):
        # Hundreds of large LOCAL files are no help to a remote uploader, so the
        # error has to say which pool fell short.
        _seed(repo, PoolFileKind.LOCAL, 2 * GIB, 50, "local")
        _seed(repo, PoolFileKind.URL, 2 * GIB, 1, "url")
        with pytest.raises(PoolSelectionError) as exc:
            select_for_deployment(
                repo, name="PROD-CANARY-04", slot="upload", kind=PoolFileKind.URL,
                request={SizeClass.LARGE: 2}, source=ClaimSource.PROD_CANARY,
            )
        message = str(exc.value)
        assert "url pool" in message
        assert "large 2 requested, 1 available" in message
        assert "prod pool add --from-listing" in message
        assert "No infrastructure was created" in message

    def test_a_failed_selection_claims_nothing(self, repo, fake_s3):
        _seed(repo, PoolFileKind.URL, 5 * MIB, 10, "tiny")
        _seed(repo, PoolFileKind.URL, 2 * GIB, 1, "lg")
        with pytest.raises(PoolSelectionError):
            select_for_deployment(
                repo, name="PROD-CANARY-04", slot="upload", kind=PoolFileKind.URL,
                request={SizeClass.TINY: 5, SizeClass.LARGE: 2},
                source=ClaimSource.PROD_CANARY,
            )
        assert repo.list_pool_claims() == []
        assert "PROD-CANARY-04" not in fake_s3


class TestAttachDeployment:
    def test_backfills_claims_after_the_row_exists(self, repo, fake_s3):
        from saorsa_db.models.deployment import Deployment

        _seed(repo, PoolFileKind.URL, 50 * MIB, 3)
        selected = select_for_deployment(
            repo, name="PROD-UL-05", slot="uploader-1", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 2}, source=ClaimSource.PROD_UPLOADERS,
        )
        deployment = Deployment(name="PROD-UL-05", deploy_args={})
        repo.session.add(deployment)
        repo.session.commit()

        pool_selection.attach_deployment(
            repo, [selected], deployment.id, "PROD-UL-05"
        )
        rows = repo.list_pool_claims(deployment_id=deployment.id)
        assert len(rows) == 2

    def test_no_deployment_id_is_a_noop(self, repo, fake_s3):
        _seed(repo, PoolFileKind.URL, 50 * MIB, 2)
        selected = select_for_deployment(
            repo, name="X", slot="s", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 1}, source=ClaimSource.PROD_UPLOADERS,
        )
        pool_selection.attach_deployment(repo, [selected], None, "X")
        assert repo.list_pool_claims()[0].deployment_id is None


class TestRendering:
    def test_small_selections_are_listed_in_full(self, repo, fake_s3, capsys):
        from rich.console import Console

        _seed(repo, PoolFileKind.URL, 50 * MIB, 3)
        selected = select_for_deployment(
            repo, name="X", slot="s", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 3}, source=ClaimSource.PROD_UPLOADERS,
        )
        pool_selection.render_selection(
            Console(), "uploader #1 (lon1)", {SizeClass.SMALL: 3}, selected
        )
        out = capsys.readouterr().out
        assert out.count("#") >= 3

    def test_large_selections_are_summarised(self, repo, fake_s3, capsys):
        # A canary draws ~350 files; listing them would bury everything else.
        from rich.console import Console

        _seed(repo, PoolFileKind.URL, 50 * MIB, 30)
        selected = select_for_deployment(
            repo, name="X", slot="s", kind=PoolFileKind.URL,
            request={SizeClass.SMALL: 30}, source=ClaimSource.PROD_CANARY,
        )
        pool_selection.render_selection(
            Console(), "upload", {SizeClass.SMALL: 30}, selected
        )
        out = capsys.readouterr().out
        assert "30 file(s)" in out
        assert out.count("\n") < 10
