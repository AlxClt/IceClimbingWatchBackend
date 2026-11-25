"""Outing weather tables and route altitude rules.

- weather_altitude_rules: the rules giving the altitude sent to Open-Meteo for a route (seeded).
- route_weather_altitude: altitude used for each ice-climbing route, and the rule that gave it.
- outing_weather_fetch: one row per processed outing (position, source, status, API weight).
- outing_weather_daily: daily features of the 61 days ending on the outing date.

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

ALTITUDE_RULES = [
    (1, "difficulties_height",
     "routes.difficulties_height (altitude of the start of the difficulties)"),
    (2, "elevation_max_minus_height_diff",
     "routes.elevation_max - routes.height_diff_difficulties"),
    (3, "elevation_max_minus_mean_height_diff",
     "routes.elevation_max - mean(height_diff_difficulties) over the ice-climbing routes having one "
     "(the mean used is stored in route_weather_altitude.mean_height_diff_m)"),
    (4, "elevation_min", "routes.elevation_min"),
    (5, "open_meteo_dem",
     "no altitude known: no elevation is sent, Open-Meteo uses its 90 m DEM height at the route position"),
]

def upgrade() -> None:
    rules = op.create_table(
        "weather_altitude_rules",
        sa.Column("rule_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.String(), nullable=False),
    )
    op.bulk_insert(rules, [{"rule_id": i, "name": n, "description": d} for i, n, d in ALTITUDE_RULES])

    op.create_table(
        "route_weather_altitude",
        sa.Column("route_id", sa.Integer(), sa.ForeignKey("routes.route_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("altitude_m", sa.Integer(), nullable=True),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("weather_altitude_rules.rule_id"), nullable=False),
        sa.Column("mean_height_diff_m", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "outing_weather_fetch",
        sa.Column("outing_id", sa.Integer(), sa.ForeignKey("outings.outing_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("outing_date", sa.Date(), nullable=False),
        sa.Column("route_id", sa.Integer(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("elevation_m", sa.Integer(), nullable=True),
        sa.Column("grid_latitude", sa.Float(), nullable=True),
        sa.Column("grid_longitude", sa.Float(), nullable=True),
        sa.Column("grid_elevation_m", sa.Float(), nullable=True),
        sa.Column("timezone", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("request_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("n_days", sa.Integer(), nullable=False),
        sa.Column("api_weight", sa.Float(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("status IN ('ok', 'partial', 'no_location', 'out_of_range')",
                           name="outing_weather_fetch_status_check"),
        sa.CheckConstraint("source IS NULL OR source IN ('historical_forecast', 'archive')",
                           name="outing_weather_fetch_source_check"),
    )
    # Daily budget: weight spent over the last 24 hours
    op.create_index("ix_outing_weather_fetch_fetched_at", "outing_weather_fetch", ["fetched_at"])

    op.create_table(
        "outing_weather_daily",
        sa.Column("outing_id", sa.Integer(),
                  sa.ForeignKey("outing_weather_fetch.outing_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("date_local", sa.Date(), primary_key=True),
        sa.Column("days_before_outing", sa.Integer(), nullable=False),
        sa.Column("n_hours", sa.Integer(), nullable=False),
        sa.Column("t_min_c", sa.Float()),
        sa.Column("t_max_c", sa.Float()),
        sa.Column("t_mean_c", sa.Float()),
        sa.Column("hours_above_0c", sa.Integer()),
        sa.Column("hours_air_le_0c", sa.Integer()),
        sa.Column("precip_mm", sa.Float()),
        sa.Column("rain_mm", sa.Float()),
        sa.Column("snow_we_mm", sa.Float()),
        sa.Column("snowfall_cm", sa.Float()),
        sa.Column("fl_min_m", sa.Float()),
        sa.Column("fl_mean_m", sa.Float()),
        sa.Column("fl_max_m", sa.Float()),
        sa.Column("hours_fl_below_alt", sa.Integer()),
        sa.Column("night_hours", sa.Integer()),
        sa.Column("night_clear_h", sa.Integer()),
        sa.Column("night_partial_h", sa.Integer()),
        sa.Column("night_cloudy_h", sa.Integer()),
        sa.Column("night_cloud_mean_pct", sa.Float()),
        sa.Column("day_hours", sa.Integer()),
        sa.Column("day_clear_h", sa.Integer()),
        sa.Column("day_partial_h", sa.Integer()),
        sa.Column("day_cloudy_h", sa.Integer()),
        sa.Column("day_cloud_mean_pct", sa.Float()),
        sa.CheckConstraint("night_clear_h + night_partial_h + night_cloudy_h = night_hours",
                           name="outing_weather_daily_night_check"),
        sa.CheckConstraint("day_clear_h + day_partial_h + day_cloudy_h = day_hours",
                           name="outing_weather_daily_day_check"),
    )


def downgrade() -> None:
    op.drop_table("outing_weather_daily")
    op.drop_index("ix_outing_weather_fetch_fetched_at", table_name="outing_weather_fetch")
    op.drop_table("outing_weather_fetch")
    op.drop_table("route_weather_altitude")
    op.drop_table("weather_altitude_rules")
