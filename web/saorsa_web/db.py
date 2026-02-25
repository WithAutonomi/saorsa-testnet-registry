from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, scoped_session, sessionmaker


class Base(DeclarativeBase):
    pass


session_factory = sessionmaker()
db_session = scoped_session(session_factory)


def init_db(app):
    engine = create_engine(app.config["SQLALCHEMY_DATABASE_URI"])
    session_factory.configure(bind=engine)

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        db_session.remove()
