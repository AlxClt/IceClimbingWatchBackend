"""Unique country names, DATE type for outing and route dates.

- Countries sharing the same countryName are merged into the one with the lowest
  country_id (their route links are moved to it), then countryName gets a UNIQUE
  constraint. NULL names are still allowed.
- outings.date, outings.last_updated and routes.last_updated go from VARCHAR
  ('YYYY-MM-DD') to DATE.

The downgrade restores the column types and drops the constraint, but does not
restore merged countries.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-25
"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("c2c.migrations")

# Countries to remove (country_id) and the country they are merged into (keep_id)
DUPLICATE_COUNTRIES = """
    dups AS (
        SELECT country_id, keep_id
        FROM (
            SELECT country_id, MIN(country_id) OVER (PARTITION BY "countryName") AS keep_id
            FROM countries
            WHERE "countryName" IS NOT NULL
        ) ranked
        WHERE country_id <> keep_id
    )
"""

# (table, column, USING expression to DATE)
DATE_COLUMNS = [
    ("outings", "date", '"date"::date'),
    ("outings", "last_updated", "NULLIF(last_updated, '')::date"),
    ("routes", "last_updated", "NULLIF(last_updated, '')::date"),
]


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Merge countries with the same name
    merged = conn.execute(sa.text(f"WITH {DUPLICATE_COUNTRIES} SELECT COUNT(*) FROM dups")).scalar_one()
    if merged:
        conn.execute(sa.text(f"""
            WITH {DUPLICATE_COUNTRIES}
            INSERT INTO countries_mapping (route_id, country_id)
            SELECT m.route_id, d.keep_id
            FROM countries_mapping m JOIN dups d ON m.country_id = d.country_id
            ON CONFLICT DO NOTHING
        """))
        conn.execute(sa.text(f"""
            WITH {DUPLICATE_COUNTRIES}
            DELETE FROM countries_mapping WHERE country_id IN (SELECT country_id FROM dups)
        """))
        conn.execute(sa.text(f"""
            WITH {DUPLICATE_COUNTRIES}
            DELETE FROM countries WHERE country_id IN (SELECT country_id FROM dups)
        """))
    logger.info("db.migration.countries_merged", extra={"merged_countries": merged})

    # 2. Unique country names
    op.create_unique_constraint("countries_countryName_key", "countries", ["countryName"])

    # 3. Dates as DATE
    for table, column, using in DATE_COLUMNS:
        op.alter_column(
            table,
            column,
            type_=sa.Date(),
            existing_type=sa.String(),
            existing_nullable=(column != "date"),
            postgresql_using=using,
        )


def downgrade() -> None:
    for table, column, _ in DATE_COLUMNS:
        op.alter_column(
            table,
            column,
            type_=sa.String(),
            existing_type=sa.Date(),
            existing_nullable=(column != "date"),
            postgresql_using=f"to_char(\"{column}\", 'YYYY-MM-DD')",
        )
    op.drop_constraint("countries_countryName_key", "countries", type_="unique")
