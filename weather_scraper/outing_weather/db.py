"""Database helpers of the outing weather scraper (PostgreSQL, shared with the c2c scraper)."""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, inspect, text
from sqlalchemy.dialects.postgresql import insert

from outing_weather.config import FEATURE_VERSION
from outing_weather.schema import outing_weather_daily, outing_weather_fetch

logger = logging.getLogger(__name__)

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def get_engine(dbstring: str) -> Engine:
    return create_engine(dbstring)


def migrate_db(engine: Engine) -> None:
    """Apply pending migrations of the weather tables, in one transaction.
    The c2c scraper tables must already exist (foreign keys to outings and routes)."""
    cfg = Config(str(ALEMBIC_INI))
    with engine.begin() as conn:
        insp = inspect(conn)
        missing = [t for t in ("outings", "routes", "outings_mapping") if not insp.has_table(t)]
        if missing:
            raise RuntimeError(
                f"Tables {missing} not found: run the c2c scraper (`python main.py init`) first"
            )
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# Route altitudes
# ---------------------------------------------------------------------------
# First rule giving an altitude > 0 (rule ids: see the weather_altitude_rules table):
#   1 difficulties_height
#   2 elevation_max - height_diff_difficulties
#   3 elevation_max - mean(height_diff_difficulties) over the ice-climbing routes
#   4 elevation_min
#   5 NULL: Open-Meteo DEM
FILL_ROUTE_ALTITUDES = text("""
    WITH mean AS (
        SELECT AVG(height_diff_difficulties)::double precision AS m
        FROM routes
        WHERE ice_climbing = 1 AND height_diff_difficulties IS NOT NULL
    ),
    cand AS (
        SELECT r.route_id,
               CASE WHEN r.difficulties_height > 0 THEN r.difficulties_height END AS a1,
               CASE WHEN r.elevation_max - r.height_diff_difficulties > 0
                    THEN r.elevation_max - r.height_diff_difficulties END AS a2,
               CASE WHEN r.elevation_max - mean.m > 0
                    THEN ROUND(r.elevation_max - mean.m)::integer END AS a3,
               CASE WHEN r.elevation_min > 0 THEN r.elevation_min END AS a4,
               mean.m
        FROM routes AS r CROSS JOIN mean
        WHERE r.ice_climbing = 1
          AND NOT EXISTS (SELECT 1 FROM route_weather_altitude AS w WHERE w.route_id = r.route_id)
    )
    INSERT INTO route_weather_altitude (route_id, altitude_m, rule_id, mean_height_diff_m, computed_at)
    SELECT route_id,
           COALESCE(a1, a2, a3, a4),
           CASE WHEN a1 IS NOT NULL THEN 1
                WHEN a2 IS NOT NULL THEN 2
                WHEN a3 IS NOT NULL THEN 3
                WHEN a4 IS NOT NULL THEN 4
                ELSE 5 END,
           CASE WHEN a1 IS NULL AND a2 IS NULL AND a3 IS NOT NULL THEN m END,
           now()
    FROM cand
""")


def fill_route_altitudes(engine: Engine, recompute: bool = False) -> int:
    """Compute the weather altitude of the ice-climbing routes that have none yet (all of them
    with `recompute`). Returns the number of routes written."""
    with engine.begin() as conn:
        if recompute:
            conn.execute(text("DELETE FROM route_weather_altitude"))
        return conn.execute(FILL_ROUTE_ALTITUDES).rowcount


# ---------------------------------------------------------------------------
# Pending outings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PendingOuting:
    outing_id: int
    outing_date: datetime.date
    route_id: int
    latitude: float | None
    longitude: float | None
    altitude_m: int | None


# An outing is pending when it is linked to an ice-climbing route and it has no weather yet, or
# its weather was fetched for another date / route / altitude / feature version.
# Route used: the ice-climbing route with coordinates and the lowest route_id.
PENDING_OUTINGS = """
    SELECT o.outing_id, o.date AS outing_date, r.route_id, r.lat AS latitude, r.lon AS longitude,
           a.altitude_m
    FROM outings AS o
    JOIN LATERAL (
        SELECT r.route_id, r.lat, r.lon
        FROM outings_mapping AS m
        JOIN routes AS r ON r.route_id = m.route_id
        WHERE m.outing_id = o.outing_id AND r.ice_climbing = 1
        ORDER BY (r.lat IS NULL OR r.lon IS NULL), r.route_id
        LIMIT 1
    ) AS r ON TRUE
    LEFT JOIN route_weather_altitude AS a ON a.route_id = r.route_id
    LEFT JOIN outing_weather_fetch AS f ON f.outing_id = o.outing_id
    WHERE o.date <= :max_date
      AND (f.outing_id IS NULL
           OR f.outing_date <> o.date
           OR f.feature_version < :feature_version
           OR f.route_id IS DISTINCT FROM r.route_id
           OR (f.status <> 'no_location' AND f.elevation_m IS DISTINCT FROM a.altitude_m)
           OR (f.status = 'no_location' AND r.lat IS NOT NULL AND r.lon IS NOT NULL))
    ORDER BY o.date DESC, o.outing_id DESC
"""


def load_pending_outings(engine: Engine, max_date: datetime.date, limit: int | None = None) -> list[PendingOuting]:
    sql = PENDING_OUTINGS + (" LIMIT :limit" if limit is not None else "")
    params = {"max_date": max_date, "feature_version": FEATURE_VERSION, "limit": limit}
    with engine.connect() as conn:
        return [PendingOuting(**row._mapping) for row in conn.execute(text(sql), params)]


def used_weight_last_24h(engine: Engine) -> float:
    """Open-Meteo weight spent by the outings fetched over the last 24 hours (all runs)."""
    with engine.connect() as conn:
        return float(conn.execute(text(
            "SELECT COALESCE(SUM(api_weight), 0) FROM outing_weather_fetch "
            "WHERE fetched_at > now() - interval '24 hours'"
        )).scalar_one())


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def write_outing(engine: Engine, fetch_row: dict, daily_rows: list[dict]) -> None:
    """Replace the weather of one outing (fetch row + daily rows), in one transaction."""
    outing_id = fetch_row["outing_id"]
    stmt = insert(outing_weather_fetch).values(**fetch_row)
    stmt = stmt.on_conflict_do_update(
        index_elements=["outing_id"],
        set_={c: stmt.excluded[c] for c in fetch_row if c != "outing_id"},
    )
    with engine.begin() as conn:
        conn.execute(stmt)
        conn.execute(delete(outing_weather_daily).where(outing_weather_daily.c.outing_id == outing_id))
        if daily_rows:
            conn.execute(
                outing_weather_daily.insert(),
                [{"outing_id": outing_id, **row} for row in daily_rows],
            )
