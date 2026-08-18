"""Shared helpers for the `saorsa-deploy releases ...` commands."""

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from saorsa_db.db import get_session_factory
from saorsa_db.repositories import (
    ReleaseRepository,
    SqlAlchemyReleaseCandidateRepository,
    SqlAlchemyReleaseRepository,
)

logger = logging.getLogger(__name__)


def get_release_repository() -> ReleaseRepository | None:
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
        return SqlAlchemyReleaseRepository(session)
    except Exception:
        logger.debug("Could not connect to registry database", exc_info=True)
        return None


def require_release_repository() -> ReleaseRepository:
    repo = get_release_repository()
    if repo is None:
        sys.stderr.write(
            "releases: DATABASE_URL is not set or the database "
            "is unreachable.\n"
        )
        sys.exit(1)
    return repo


def resolve_release_candidate_id(
    repo: ReleaseRepository,
    binary_target: str,
    rc_version: Optional[str],
) -> Optional[int]:
    """Look up the RC row this release shipped from.

    By ``(binary_target, binary_version)``, because the release skills never
    hold the RC row id — they record it into the Linear issue and move on.
    A miss is not an error: the ship record is worth having without the link.
    """
    if not rc_version:
        return None
    rc_repo = SqlAlchemyReleaseCandidateRepository(repo.session)
    rc = rc_repo.get_by_binary_version(binary_target, rc_version)
    return rc.id if rc is not None else None


def parse_timestamp(value: Optional[str], *, field: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, or a bare ``YYYY-MM-DD`` date.

    A bare date is taken as midnight UTC — the ship record's planned promotion
    is a day, not an instant. A naive timestamp is likewise read as UTC rather
    than as the invoking machine's zone, so the same command run from two
    laptops records the same moment.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        sys.stderr.write(
            f"releases: {field} is not a valid ISO-8601 timestamp or "
            f"YYYY-MM-DD date: {value!r}\n"
        )
        sys.exit(2)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_duration(seconds: Optional[float]) -> str:
    """Render a soak age as ``3d 4h`` / ``5h 12m`` / ``42m``."""
    if seconds is None:
        return "-"
    total = int(seconds)
    if total < 0:
        return "-"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
