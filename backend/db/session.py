import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def create_db_engine(database_url: str | None = None) -> Engine:
    return create_engine(database_url or get_database_url(), pool_pre_ping=True)


def create_session_factory(
    database_url: str | None = None,
    engine: Engine | None = None,
) -> sessionmaker:
    """Return a sessionmaker bound to Postgres.

    Pass a URL or a ready engine. Otherwise DATABASE_URL is read from the environment.
    The engine connects on first use, so importing this module does not need a database.
    """
    bound = engine if engine is not None else create_db_engine(database_url)
    return sessionmaker(bind=bound, autoflush=False, expire_on_commit=False)
