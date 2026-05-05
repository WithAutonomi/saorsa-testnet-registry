"""Database access for the ``saorsa-deploy migration`` subcommand family.

Migration state is fully owned by the saorsa-db PostgreSQL database — unlike
the deployment ``registry`` module, there is no soft-fail mode. If the DB is
not reachable, the migration commands cannot run.
"""

import os

from saorsa_db.db import get_session_factory
from saorsa_db.repositories import (
    MigrationRepository,
    SqlAlchemyMigrationRepository,
)


class MigrationRegistryError(RuntimeError):
    """Raised when the migration registry cannot be reached."""


def get_migration_repository() -> MigrationRepository:
    """Return a ``MigrationRepository`` bound to a fresh session.

    Raises ``MigrationRegistryError`` when ``DATABASE_URL`` is unset or
    the database cannot be reached. Migration commands require a live DB
    so they fail loudly rather than silently no-op.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise MigrationRegistryError(
            "DATABASE_URL is not set. Migration commands require an "
            "accessible saorsa-db PostgreSQL database."
        )
    try:
        factory = get_session_factory(database_url)
        session = factory()
    except Exception as e:
        raise MigrationRegistryError(
            f"Could not connect to migration registry database: {e}"
        ) from e
    return SqlAlchemyMigrationRepository(session)
