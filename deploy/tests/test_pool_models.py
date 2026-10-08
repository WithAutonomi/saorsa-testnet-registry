"""Schema-level tests for the production upload file pool.

The partial unique index is the point of this suite. "A file is never handed
out twice" is the guarantee the whole feature exists to provide, and an
application-level check cannot make it when two concurrent deploys race for the
last large file. These tests assert the database itself refuses.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from saorsa_db.db import Base
from saorsa_db.models.deployment import Deployment
from saorsa_db.models.prod_file_pool import (
    BLOCKING_STATUSES,
    SELF_RESOLVING_SOURCES,
    ClaimSource,
    ClaimStatus,
    PoolFileKind,
    ProdFilePool,
    ProdFilePoolClaim,
    ProdFilePoolDigest,
)
from sqlalchemy import Float, Numeric, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

NOW = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    # SQLite does not enforce foreign keys unless asked, and the SET NULL /
    # CASCADE behaviours below are part of what is under test.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _file(
    session,
    *,
    locator="https://example.org/a.bin",
    kind=PoolFileKind.URL,
    name="a.bin",
    size_bytes=42 * 1024 * 1024,
    **kwargs,
) -> ProdFilePool:
    pool_file = ProdFilePool(
        kind=kind,
        locator=locator,
        locator_sha256=f"{abs(hash(locator)):064x}"[:64],
        name=name,
        size_bytes=size_bytes,
        added_at=NOW,
        **kwargs,
    )
    session.add(pool_file)
    session.commit()
    return pool_file


def _claim(
    session, pool_file, *, status=ClaimStatus.CLAIMED, source=ClaimSource.MANUAL, **kwargs
) -> ProdFilePoolClaim:
    claim = ProdFilePoolClaim(
        file_id=pool_file.id,
        source=source,
        status=status,
        claimed_at=NOW,
        **kwargs,
    )
    session.add(claim)
    session.commit()
    return claim


class TestSchemaCreates:
    def test_tables_and_indexes_are_creatable(self, session):
        # create_all in the fixture would already have raised, but assert the
        # round trip so a silently-empty schema cannot pass.
        pool_file = _file(session)
        assert pool_file.id is not None
        assert pool_file.active is True

    def test_size_bytes_is_nullable_for_unknown_size_urls(self, session):
        # A host publishing no Content-Length can still be catalogued.
        pool_file = _file(session, locator="https://example.org/stream", size_bytes=None)
        assert pool_file.size_bytes is None


class TestOneBlockingClaimPerFile:
    def test_second_open_claim_is_refused(self, session):
        pool_file = _file(session)
        _claim(session, pool_file)
        with pytest.raises(IntegrityError):
            _claim(session, pool_file)

    def test_claiming_an_uploaded_file_is_refused(self, session):
        # The stronger half of the rule: spent means spent. An UPLOADED claim
        # blocks a new one exactly as an open claim does.
        pool_file = _file(session)
        _claim(session, pool_file, status=ClaimStatus.UPLOADED)
        with pytest.raises(IntegrityError):
            _claim(session, pool_file)

    def test_released_claim_frees_the_file(self, session):
        pool_file = _file(session)
        _claim(session, pool_file, status=ClaimStatus.RELEASED)
        # No exception: the file is available again.
        second = _claim(session, pool_file)
        assert second.id is not None

    def test_many_released_claims_may_accumulate(self, session):
        pool_file = _file(session)
        for _ in range(3):
            _claim(session, pool_file, status=ClaimStatus.RELEASED)
        rows = session.query(ProdFilePoolClaim).filter_by(file_id=pool_file.id).all()
        assert len(rows) == 3

    def test_different_files_claim_independently(self, session):
        first = _file(session, locator="https://example.org/1.bin")
        second = _file(session, locator="https://example.org/2.bin")
        _claim(session, first)
        _claim(session, second)
        assert session.query(ProdFilePoolClaim).count() == 2


class TestLocatorUniqueness:
    def test_duplicate_locator_hash_is_refused(self, session):
        _file(session, locator="https://example.org/same.bin")
        duplicate = ProdFilePool(
            kind=PoolFileKind.URL,
            locator="https://example.org/same.bin",
            locator_sha256=f"{abs(hash('https://example.org/same.bin')):064x}"[:64],
            name="same.bin",
            size_bytes=1,
            added_at=NOW,
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()


class TestDigests:
    def test_one_value_per_algorithm_per_file(self, session):
        pool_file = _file(session)
        session.add(ProdFilePoolDigest(file_id=pool_file.id, algo="sha1", value="aa"))
        session.commit()
        session.add(ProdFilePoolDigest(file_id=pool_file.id, algo="sha1", value="bb"))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_a_file_may_carry_several_algorithms(self, session):
        # The whole point of a digest *set*: an Internet Archive manifest gives
        # md5+sha1, a local hash gives sha1+sha256, and they meet on sha1.
        pool_file = _file(session)
        for algo, value in (("md5", "1"), ("sha1", "2"), ("sha256", "3")):
            session.add(ProdFilePoolDigest(file_id=pool_file.id, algo=algo, value=value))
        session.commit()
        session.refresh(pool_file)
        assert {d.algo for d in pool_file.digests} == {"md5", "sha1", "sha256"}


class TestDuplicateLink:
    def test_a_file_can_point_at_the_content_it_duplicates(self, session):
        original = _file(session, kind=PoolFileKind.LOCAL, locator="/data/film.mkv")
        mirror = _file(
            session,
            locator="https://archive.org/download/x/film.mkv",
            duplicate_of=original.id,
        )
        session.refresh(mirror)
        assert mirror.duplicate_of == original.id


class TestClaimPayload:
    def test_cost_columns_are_exact_precision_not_float(self):
        # atto and wei are uint256-shaped, so the columns must be NUMERIC.
        #
        # This asserts the column TYPE rather than round-tripping a large value,
        # because SQLite — which these tests run on — has no NUMERIC type and
        # stores Decimal as a float regardless of what the model declares. The
        # round trip below therefore uses a value that survives a float, and the
        # exactness guarantee is checked here where it is actually decidable.
        # Do not "fix" a large-value round-trip failure by switching to Float:
        # that would break production, where the column is exact.
        for column in (
            ProdFilePoolClaim.__table__.c.storage_cost_atto,
            ProdFilePoolClaim.__table__.c.gas_cost_wei,
        ):
            assert isinstance(column.type, Numeric)
            assert not isinstance(column.type, Float)

    def test_resolved_payload_round_trips(self, session):
        pool_file = _file(session)
        _claim(
            session,
            pool_file,
            status=ClaimStatus.UPLOADED,
            address="a7f3c2",
            chunks_stored=77,
            chunks_failed=0,
            total_chunks=77,
            storage_cost_atto=Decimal("419203847182734"),
            gas_cost_wei=Decimal("1310000000000"),
            resolved_at=NOW + timedelta(minutes=5),
        )
        stored = session.query(ProdFilePoolClaim).one()
        assert stored.address == "a7f3c2"
        assert stored.chunks_stored == 77
        assert stored.total_chunks == 77
        assert stored.resolved_at is not None
        assert stored.to_dict()["chunks_stored"] == 77

    def test_deployment_deletion_keeps_the_claim(self, session):
        # The file is still on the network; losing the claim would silently
        # return a spent file to the pool.
        deployment = Deployment(name="PROD-UL-05", deploy_args={})
        session.add(deployment)
        session.commit()
        pool_file = _file(session)
        _claim(
            session,
            pool_file,
            status=ClaimStatus.UPLOADED,
            source=ClaimSource.PROD_UPLOADERS,
            deployment_id=deployment.id,
            deployment_name="PROD-UL-05",
        )
        session.delete(deployment)
        session.commit()
        surviving = session.query(ProdFilePoolClaim).one()
        assert surviving.deployment_id is None
        # The snapshot is why the run is still identifiable — names are reused,
        # so re-resolving the id later would attribute it to the wrong run.
        assert surviving.deployment_name == "PROD-UL-05"

    def test_deleting_a_file_removes_its_claims_and_digests(self, session):
        pool_file = _file(session)
        session.add(ProdFilePoolDigest(file_id=pool_file.id, algo="sha1", value="aa"))
        _claim(session, pool_file)
        session.delete(pool_file)
        session.commit()
        assert session.query(ProdFilePoolClaim).count() == 0
        assert session.query(ProdFilePoolDigest).count() == 0


class TestClaimSemantics:
    def test_is_open_only_for_claimed(self, session):
        pool_file = _file(session)
        claim = _claim(session, pool_file)
        assert claim.is_open is True
        claim.status = ClaimStatus.UPLOADED
        assert claim.is_open is False

    @pytest.mark.parametrize(
        "source,expected",
        [
            (ClaimSource.PROD_UPLOADERS, True),
            (ClaimSource.PROD_CANARY, True),
            (ClaimSource.MANUAL, False),
            # local-uploads resolves its claims inline as it uploads, so
            # record-results never sees them.
            (ClaimSource.PROD_LOCAL_UPLOADS, False),
        ],
    )
    def test_self_resolving_sources(self, session, source, expected):
        pool_file = _file(session)
        claim = _claim(session, pool_file, source=source)
        assert claim.self_resolving is expected
        assert (source in SELF_RESOLVING_SOURCES) is expected

    def test_blocking_statuses_match_the_index_predicate(self):
        # If these drift apart, the repository's availability query and the
        # database constraint would disagree about what "available" means.
        assert BLOCKING_STATUSES == {ClaimStatus.CLAIMED, ClaimStatus.UPLOADED}
        assert ClaimStatus.RELEASED not in BLOCKING_STATUSES
