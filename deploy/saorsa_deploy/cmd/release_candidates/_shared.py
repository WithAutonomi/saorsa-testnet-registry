"""Shared helpers for the `saorsa-deploy release-candidates ...` commands."""

import logging
import os
import sys

from saorsa_db.db import get_session_factory
from saorsa_db.repositories import (
    ReleaseCandidateRepository,
    SqlAlchemyReleaseCandidateRepository,
)

logger = logging.getLogger(__name__)


def get_rc_repository() -> ReleaseCandidateRepository | None:
    """Open a session against the configured registry DB.

    Returns None if DATABASE_URL is unset or the database is unreachable;
    callers should treat that as a fatal error for this command set
    (unlike the deployment registry, which silently no-ops on missing DB).
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None
    try:
        factory = get_session_factory(database_url)
        session = factory()
        return SqlAlchemyReleaseCandidateRepository(session)
    except Exception:
        logger.debug("Could not connect to registry database", exc_info=True)
        return None


def require_rc_repository() -> ReleaseCandidateRepository:
    repo = get_rc_repository()
    if repo is None:
        sys.stderr.write(
            "release-candidates: DATABASE_URL is not set or the database is unreachable.\n"
        )
        sys.exit(1)
    return repo
