"""Alembic uses a caller-owned connection; credentials never enter an INI file."""

import os

from alembic import context
from sqlalchemy import text

from minicodex.web.db_engine import Database
from minicodex.web.db_models import Base


def run_with_connection(connection) -> None:
    # Serialize schema changes if deployment accidentally starts two migrators.
    connection.execute(text("SELECT pg_advisory_xact_lock(71624001)"))
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        run_with_connection(connection)
        return
    url = os.environ.get("MINICODEX_DATABASE_URL", "")
    if not url:
        raise RuntimeError("Set MINICODEX_DATABASE_URL before running Alembic")
    db = Database(url, check_version=False)
    try:
        with db.engine.begin() as connection:
            run_with_connection(connection)
    finally:
        db.close()


if getattr(context, "config", None) is not None:
    run_migrations()
