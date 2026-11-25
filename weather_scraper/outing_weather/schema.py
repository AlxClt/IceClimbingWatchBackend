"""SQLAlchemy schema of the outing weather tables (same PostgreSQL database as the c2c scraper).

Only the tables in OWN_TABLES belong to this project. `outings` and `routes` are owned by the
c2c scraper; they are declared here as stubs (primary key only) for the foreign keys, and are
ignored by this project's Alembic migrations.

Schema changes must come with an Alembic migration in outing_weather/migrations/versions/
(applied at startup by outing_weather.db.migrate_db).
"""
from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
)

metadata = MetaData()

OWN_TABLES = {
    "weather_altitude_rules",
    "route_weather_altitude",
    "outing_weather_fetch",
    "outing_weather_daily",
}

FETCH_STATUSES = ("ok", "partial", "no_location", "out_of_range")

# Stubs of the c2c scraper tables (not managed here)
outings_stub = Table("outings", metadata, Column("outing_id", Integer, primary_key=True))
routes_stub = Table("routes", metadata, Column("route_id", Integer, primary_key=True))


weather_altitude_rules = Table(
    "weather_altitude_rules",
    metadata,
    Column("rule_id", Integer, primary_key=True, autoincrement=False),
    Column("name", String, nullable=False, unique=True),
    Column("description", String, nullable=False),
)

route_weather_altitude = Table(
    "route_weather_altitude",
    metadata,
    Column("route_id", Integer, ForeignKey("routes.route_id", ondelete="CASCADE"), primary_key=True),
    Column("altitude_m", Integer, nullable=True),  # NULL: no elevation sent, Open-Meteo DEM
    Column("rule_id", Integer, ForeignKey("weather_altitude_rules.rule_id"), nullable=False),
    Column("mean_height_diff_m", Float, nullable=True),  # mean used by rule 3, NULL otherwise
    Column("computed_at", DateTime(timezone=True), nullable=False),
)

outing_weather_fetch = Table(
    "outing_weather_fetch",
    metadata,
    Column("outing_id", Integer, ForeignKey("outings.outing_id", ondelete="CASCADE"), primary_key=True),
    Column("outing_date", Date, nullable=False),        # outings.date when fetched
    Column("route_id", Integer, nullable=True),         # route whose position was used
    Column("latitude", Float, nullable=True),           # requested position (route)
    Column("longitude", Float, nullable=True),
    Column("elevation_m", Integer, nullable=True),      # requested elevation; NULL = Open-Meteo DEM
    Column("grid_latitude", Float, nullable=True),      # grid cell returned by Open-Meteo
    Column("grid_longitude", Float, nullable=True),
    Column("grid_elevation_m", Float, nullable=True),   # elevation used by Open-Meteo
    Column("timezone", String, nullable=True),          # IANA time zone of the local dates
    Column("source", String, nullable=True),            # historical_forecast | archive
    Column("model", String, nullable=True),
    Column("start_date", Date, nullable=True),          # requested local dates
    Column("end_date", Date, nullable=True),
    Column("request_url", String, nullable=True),
    Column("status", String, nullable=False),
    Column("n_days", Integer, nullable=False),          # rows in outing_weather_daily
    Column("api_weight", Float, nullable=False),        # Open-Meteo calls counted for this outing
    Column("fetched_at", DateTime(timezone=True), nullable=False, index=True),  # daily budget
    Column("feature_version", Integer, nullable=False),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{s}'" for s in FETCH_STATUSES) + ")",
        name="outing_weather_fetch_status_check",
    ),
    CheckConstraint(
        "source IS NULL OR source IN ('historical_forecast', 'archive')",
        name="outing_weather_fetch_source_check",
    ),
)

outing_weather_daily = Table(
    "outing_weather_daily",
    metadata,
    Column("outing_id", Integer, ForeignKey("outing_weather_fetch.outing_id", ondelete="CASCADE"),
           primary_key=True),
    Column("date_local", Date, primary_key=True),
    Column("days_before_outing", Integer, nullable=False),  # 0 = outing day
    Column("n_hours", Integer, nullable=False),
    # Air temperature (at outing_weather_fetch.grid_elevation_m)
    Column("t_min_c", Float),
    Column("t_max_c", Float),
    Column("t_mean_c", Float),      # mean of the hourly temperatures of the day
    Column("hours_above_0c", Integer),
    Column("hours_air_le_0c", Integer),
    # Precipitation
    Column("precip_mm", Float),
    Column("rain_mm", Float),       # rain + showers (archive: rain only)
    Column("snow_we_mm", Float),    # max(0, precip - rain)
    Column("snowfall_cm", Float),
    # Freezing level (historical forecast only)
    Column("fl_min_m", Float),
    Column("fl_mean_m", Float),
    Column("fl_max_m", Float),
    Column("hours_fl_below_alt", Integer),
    # Night sky: night before day d, [d-1 12:00, d 12:00) local, is_day = 0
    Column("night_hours", Integer),
    Column("night_clear_h", Integer),
    Column("night_partial_h", Integer),
    Column("night_cloudy_h", Integer),
    Column("night_cloud_mean_pct", Float),
    # Day sky: [d 00:00, d+1 00:00) local, is_day = 1
    Column("day_hours", Integer),
    Column("day_clear_h", Integer),
    Column("day_partial_h", Integer),
    Column("day_cloudy_h", Integer),
    Column("day_cloud_mean_pct", Float),
    CheckConstraint(
        "night_clear_h + night_partial_h + night_cloudy_h = night_hours",
        name="outing_weather_daily_night_check",
    ),
    CheckConstraint(
        "day_clear_h + day_partial_h + day_cloudy_h = day_hours",
        name="outing_weather_daily_day_check",
    ),
)
