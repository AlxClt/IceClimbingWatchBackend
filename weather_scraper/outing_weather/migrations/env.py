"""Alembic environment of the outing weather tables (online mode only).

The database is shared with the c2c scraper, which has its own Alembic history. This one:
- stores its revision in alembic_version_weather;
- only compares the tables of OWN_TABLES (the c2c tables, including the outings / routes
  stubs of the metadata, are ignored).

When run by the app (outing_weather.db.migrate_db), the connection is passed in
config.attributes["connection"]. When run from the alembic CLI, an engine is created from the
DATABASE_URL environment variable.
"""
import os

from alembic import context
from sqlalchemy import create_engine

from outing_weather.schema import OWN_TABLES, metadata

VERSION_TABLE = "alembic_version_weather"

config = context.config


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in OWN_TABLES
    return True


def run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        version_table=VERSION_TABLE,
        include_object=include_object,
    )
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
