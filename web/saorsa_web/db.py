from saorsa_db.db import Base, get_engine  # noqa: F401 — re-export Base
from sqlalchemy.orm import scoped_session, sessionmaker

session_factory = sessionmaker()
db_session = scoped_session(session_factory)


def init_db(app):
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    engine = get_engine(uri)
    session_factory.configure(bind=engine)

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        db_session.remove()
