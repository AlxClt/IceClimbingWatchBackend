"""Baseline: the schema as created by Base.metadata.create_all before Alembic was introduced.

Databases created before Alembic are stamped at this revision by c2c.db.migrate_db
instead of running it. On an empty database it creates the original tables, which
the following revisions then alter.

Revision ID: 0001
Revises:
Create Date: 2026-11-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "routes",
        sa.Column("route_id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("snow_ice_mixed", sa.Integer(), nullable=True),
        sa.Column("mountain_climbing", sa.Integer(), nullable=True),
        sa.Column("ice_climbing", sa.Integer(), nullable=True),
        sa.Column("elevation_min", sa.Integer(), nullable=True),
        sa.Column("elevation_max", sa.Integer(), nullable=True),
        sa.Column("difficulties_height", sa.Integer(), nullable=True),
        sa.Column("height_diff_difficulties", sa.Integer(), nullable=True),
        sa.Column("glacier", sa.String(), nullable=True),
        sa.Column("global_rating", sa.String(), nullable=True),
        sa.Column("ice_rating", sa.String(), nullable=True),
        sa.Column("mixed_rating", sa.String(), nullable=True),
        sa.Column("rock_free_rating", sa.String(), nullable=True),
        sa.Column("last_updated", sa.String(), nullable=True),
        # create_all put the annotated (Mapped[...]) columns last
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
    )
    op.create_table(
        "countries",
        sa.Column("country_id", sa.Integer(), primary_key=True),
        sa.Column("countryName", sa.String(), nullable=True),
    )
    op.create_table(
        "orientations",
        sa.Column("orientation_id", sa.Integer(), primary_key=True),
        sa.Column("orientation", sa.String(), nullable=False),
        sa.UniqueConstraint("orientation"),
    )
    op.create_table(
        "outings",
        sa.Column("outing_id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.String(), nullable=False),
        sa.Column("conditions", sa.String(), nullable=True),
        sa.Column("last_updated", sa.String(), nullable=True),
    )
    op.create_table(
        "orientation_mapping",
        sa.Column("route_id", sa.Integer(), sa.ForeignKey("routes.route_id"), primary_key=True),
        sa.Column("orientation_id", sa.Integer(), sa.ForeignKey("orientations.orientation_id"), primary_key=True),
    )
    op.create_table(
        "countries_mapping",
        sa.Column("route_id", sa.Integer(), sa.ForeignKey("routes.route_id"), primary_key=True),
        sa.Column("country_id", sa.Integer(), sa.ForeignKey("countries.country_id"), primary_key=True),
    )
    op.create_table(
        "outings_mapping",
        sa.Column("route_id", sa.Integer(), sa.ForeignKey("routes.route_id"), primary_key=True),
        sa.Column("outing_id", sa.Integer(), sa.ForeignKey("outings.outing_id"), primary_key=True),
    )


def downgrade() -> None:
    for table in (
        "outings_mapping",
        "countries_mapping",
        "orientation_mapping",
        "outings",
        "orientations",
        "countries",
        "routes",
    ):
        op.drop_table(table)
