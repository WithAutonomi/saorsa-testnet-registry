"""SQLAlchemy engine and session factory for the registry.

Importing this module has **no side effect on the environment**. It used to call
`load_dotenv()`, which gave every importer the checkout's `.env` for free and
gave an installed copy of `saorsa-deploy` nothing at all — python-dotenv
searches upward from the calling module's file, which under
`~/.cache/uv/.../saorsa_db/` is not a checkout (V2-1624). Each entry point loads
its own environment now: `saorsa_deploy.main` via `saorsa_deploy.env_file`, the
web app via `saorsa_web.config`, both Alembic `env.py` files and the MCP server
with their own `load_dotenv()`.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DEFAULT_DATABASE_URL = (
    "postgresql://saorsa_testnet:saorsa_testnet@localhost:5432/saorsa_testnet_registry"
)


class Base(DeclarativeBase):
    pass


def get_engine(database_url: str | None = None):
    uri = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    kwargs = {}
    if uri.startswith("postgresql"):
        kwargs.update(
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=1,
            pool_recycle=300,
        )
    return create_engine(uri, **kwargs)


def get_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    engine = get_engine(database_url)
    return sessionmaker(bind=engine)
