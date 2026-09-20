"""SQLAlchemy engine, short sessions, and explicit schema version management."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


def migration_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    return config


class Database:
    def __init__(self, url: str, *, check_version: bool = True) -> None:
        parsed = make_url(url)
        if parsed.get_backend_name() not in {"postgresql", "postgres"}:
            raise ValueError("MINICODEX_DATABASE_URL must point to PostgreSQL")
        parsed = parsed.set(drivername="postgresql+psycopg")
        self.engine = create_engine(
            parsed,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_timeout=10,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 10,
                "options": f"{parsed.query.get('options', '')} -c statement_timeout=15000",
            },
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        if check_version:
            try:
                with self.engine.connect() as connection:
                    actual = set(MigrationContext.configure(connection).get_current_heads())
                expected = set(ScriptDirectory.from_config(migration_config()).get_heads())
                if actual != expected:
                    raise RuntimeError(
                        "Database schema is not current. Stop the console and run "
                        "python -m minicodex.web.database upgrade"
                    )
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        self.engine.dispose()


def upgrade(url: str) -> None:
    db = Database(url, check_version=False)
    try:
        with db.engine.begin() as connection:
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        db.close()
