"""Alembic environment of the camptocamp scraper database (online mode only).

When run by the app (c2c.db.migrate_db), the connection is passed in config.attributes["connection"].
When run from the alembic CLI, an engine is created from the DATABASE_URL environment variable.
Logging is not configured here: the app (or the CLI default) owns it.
"""
import os

from alembic import context
from sqlalchemy import create_engine

from c2c.schema import Base

config = context.config
target_metadata = Base.metadata


def run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline (--sql) mode is not supported for this project")

connection = config.attributes.get("connection")
if connection is not None:
    run_migrations(connection)
else:
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as connection:
        run_migrations(connection)
