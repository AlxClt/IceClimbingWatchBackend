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


def include_object(obj, name, type_, reflected, compare_to):
    """Ignore the database tables that are not in the c2c metadata: the outing weather tables
    (weather_scraper/, own Alembic history) live in the same database, and autogenerate would
    otherwise propose to drop them."""
    if type_ == "table" and reflected and compare_to is None:
        return False
    return True


def run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
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
