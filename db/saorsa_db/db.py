import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()

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
