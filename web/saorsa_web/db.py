from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, scoped_session, sessionmaker


class Base(DeclarativeBase):
    pass


session_factory = sessionmaker()
db_session = scoped_session(session_factory)


def init_db(app):
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    kwargs = {}
    if uri.startswith("postgresql"):
        kwargs.update(
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=1,
            pool_recycle=300,
        )
    engine = create_engine(uri, **kwargs)
    session_factory.configure(bind=engine)

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        db_session.remove()
