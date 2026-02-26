import hashlib
import secrets

import pytest
from sqlalchemy import create_engine, event

from saorsa_web.app import create_app
from saorsa_web.db import Base, db_session, session_factory
from saorsa_web.models.api_token import ApiToken


@pytest.fixture()
def app():
    """Create a Flask app backed by an in-memory SQLite database.

    The engine is created once, the app's ``init_db`` is allowed to
    reconfigure the session factory, and then we *re*-configure it to
    point at our single in-memory engine so that both table creation and
    session queries hit the same database.
    """
    engine = create_engine("sqlite://", echo=False)

    # SQLite requires foreign key enforcement to be turned on per-connection.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # Let create_app run (it calls init_db which reconfigures session_factory)
    application = create_app(
        config={
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "WORKFLOW_RUNNER_PAT": "fake-pat-for-testing",
            "GITHUB_REPO": "test-owner/test-repo",
            "WORKFLOW_FILE": "test-workflow.yml",
            "GITHUB_CLIENT_ID": "test-client-id",
            "GITHUB_CLIENT_SECRET": "test-client-secret",
        },
    )

    # Override the binding to point at *our* engine and create tables on it.
    session_factory.configure(bind=engine)
    Base.metadata.create_all(engine)

    yield application

    db_session.remove()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture()
def db(app):
    """Provide the scoped db_session, bound to the in-memory SQLite engine."""
    yield db_session
    db_session.remove()


@pytest.fixture()
def auth_session(client):
    """Return a test client with an authenticated session."""
    with client.session_transaction() as sess:
        sess["user"] = "testuser"
    return client


@pytest.fixture()
def api_token(db):
    """Create an API token and return (raw_token, ApiToken record)."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    token_record = ApiToken(
        user="testuser",
        token_hash=token_hash,
        name="test-token",
    )
    db.add(token_record)
    db.commit()
    return raw_token, token_record


@pytest.fixture()
def auth_header(api_token):
    """Return an Authorization header dict with a valid Bearer token."""
    raw_token, _ = api_token
    return {"Authorization": f"Bearer {raw_token}"}
