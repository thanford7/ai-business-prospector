import os
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker

from db.session import create_db_engine

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _load_local_env() -> None:
    """Load a local .env without overriding variables already set."""
    candidates = (
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    )
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        for line in resolved.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _endpoint(url: str) -> tuple[str, int | None, str]:
    parsed = make_url(url)
    port = parsed.port
    if port is None and parsed.drivername.startswith("postgresql"):
        port = 5432
    return ((parsed.host or "").lower(), port, (parsed.database or "").lower())


def _test_database_url(app_url: str | None) -> str | None:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    if not app_url:
        return None
    parsed = make_url(app_url)
    name = parsed.database
    if not name:
        return None
    if name.endswith("_test"):
        return app_url
    return parsed.set(database=f"{name}_test").render_as_string(hide_password=False)


def _ensure_database(test_url: str, admin_url: str) -> None:
    name = make_url(test_url).database
    if not name or _IDENT.fullmatch(name) is None:
        raise RuntimeError(f"Refusing to create a database named {name!r}")
    admin = create_db_engine(admin_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": name},
            ).scalar()
            if exists is None:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()


def _truncate_application_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                DO $$
                DECLARE
                    row record;
                BEGIN
                    FOR row IN
                        SELECT tablename
                        FROM pg_tables
                        WHERE schemaname = 'public' AND tablename <> 'alembic_version'
                    LOOP
                        EXECUTE format(
                            'TRUNCATE TABLE %I.%I RESTART IDENTITY CASCADE',
                            'public',
                            row.tablename
                        );
                    END LOOP;
                END $$;
                """
            )
        )


@pytest.fixture(scope="session")
def migrated_engine():
    """Postgres engine for tests, migrated and emptied at the start of the run.

    Uses TEST_DATABASE_URL when it is set. Otherwise uses the DATABASE_URL
    with `_test` added to the database name. Alembic reads DATABASE_URL, so
    that variable is pointed at the test database before migrate. The
    application database is never migrated or truncated by this fixture.
    """
    _load_local_env()
    app_url = os.environ.get("DATABASE_URL")
    test_url = _test_database_url(app_url)
    if not test_url:
        pytest.skip("DATABASE_URL or TEST_DATABASE_URL is not set")
    if app_url and _endpoint(app_url) == _endpoint(test_url):
        pytest.fail(
            "Tests refuse to use the application database. "
            "Set TEST_DATABASE_URL to a different database."
        )
    admin_url = app_url or make_url(test_url).set(database="postgres").render_as_string(
        hide_password=False
    )
    _ensure_database(test_url, admin_url)
    os.environ["DATABASE_URL"] = test_url
    from alembic.config import Config

    from alembic import command

    engine = create_db_engine(test_url)
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    _truncate_application_tables(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(migrated_engine):
    connection = migrated_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()
