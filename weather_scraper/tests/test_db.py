"""Database tests (route altitudes, pending outings, writes) on a disposable PostgreSQL database.

Set TEST_DATABASE_URL (e.g. postgresql+psycopg://postgres:postgres@localhost:5433/test) to run them;
every table of that database's public schema is dropped first.
"""
import datetime
import os

import pytest
from sqlalchemy import create_engine, text

from outing_weather.config import FEATURE_VERSION
from outing_weather.db import fill_route_altitudes, load_pending_outings, migrate_db, write_outing
from outing_weather.scraper import OutingWeatherScraper

DB_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

# Minimal c2c scraper tables (columns used by the weather scraper)
C2C_TABLES = """
    CREATE TABLE routes (
        route_id integer PRIMARY KEY, lat double precision, lon double precision, ice_climbing integer,
        elevation_min integer, elevation_max integer, difficulties_height integer,
        height_diff_difficulties integer);
    CREATE TABLE outings (outing_id integer PRIMARY KEY, date date NOT NULL);
    CREATE TABLE outings_mapping (route_id integer REFERENCES routes, outing_id integer REFERENCES outings,
                                  PRIMARY KEY (route_id, outing_id));
"""

#          route_id, lat,  lon, ice, el_min, el_max, diff_height, height_diff  -> expected rule
ROUTES = [
    (1, 45.9, 6.9, 1, 1000, 1500, 1200, 300),   # 1: difficulties_height
    (2, 45.9, 6.9, 1, 1000, 1500, None, 300),   # 2: 1500 - 300
    (3, 45.9, 6.9, 1, 1000, 1500, None, None),  # 3: 1500 - mean(300, 300, 900) = 1000
    (4, 45.9, 6.9, 1, 1000, None, None, None),  # 4: elevation_min
    (5, 45.9, 6.9, 1, None, None, None, None),  # 5: DEM
    (6, 45.9, 6.9, 1, 800, 400, None, 900),     # 2 and 3 give <= 0 -> 4: 800
    (7, None, None, 1, 1000, 1500, 1200, None),  # no coordinates
    (8, 45.9, 6.9, None, 1000, 1500, 1200, 300),  # not ice climbing
]


@pytest.fixture()
def engine():
    eng = create_engine(DB_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        conn.execute(text(C2C_TABLES))
        conn.execute(
            text("INSERT INTO routes VALUES (:r, :lat, :lon, :ice, :emin, :emax, :dh, :hd)"),
            [dict(zip(("r", "lat", "lon", "ice", "emin", "emax", "dh", "hd"), r)) for r in ROUTES],
        )
    migrate_db(eng)
    yield eng
    eng.dispose()


def add_outing(engine, outing_id, date, *route_ids):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO outings VALUES (:o, :d)"), {"o": outing_id, "d": date})
        for r in route_ids:
            conn.execute(text("INSERT INTO outings_mapping VALUES (:r, :o)"), {"r": r, "o": outing_id})


def test_migration_is_idempotent_and_seeds_rules(engine):
    migrate_db(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM weather_altitude_rules")).scalar_one() == 5
        assert conn.execute(text("SELECT version_num FROM alembic_version_weather")).scalar_one() == "0001"


def test_route_altitude_rules(engine):
    assert fill_route_altitudes(engine) == 7
    with engine.connect() as conn:
        got = {r.route_id: (r.altitude_m, r.rule_id, r.mean_height_diff_m) for r in conn.execute(text(
            "SELECT route_id, altitude_m, rule_id, mean_height_diff_m FROM route_weather_altitude"))}
    assert got == {
        1: (1200, 1, None),
        2: (1200, 2, None),
        3: (1000, 3, 500.0),
        4: (1000, 4, None),
        5: (None, 5, None),
        6: (800, 4, None),
        7: (1200, 1, None),
    }
    assert fill_route_altitudes(engine) == 0                  # kept once computed
    assert fill_route_altitudes(engine, recompute=True) == 7


def test_pending_outings(engine):
    fill_route_altitudes(engine)
    add_outing(engine, 100, "2024-01-20", 8, 3, 2)   # ice routes 2 and 3: route 2 is used
    add_outing(engine, 101, "2024-02-20", 7)        # only an ice route without coordinates
    add_outing(engine, 102, "2024-03-20", 8)        # no ice route
    add_outing(engine, 103, "2030-01-01", 1)        # too recent

    pending = load_pending_outings(engine, datetime.date(2025, 1, 1))
    assert [(o.outing_id, o.route_id, o.altitude_m) for o in pending] == [(101, 7, 1200), (100, 2, 1200)]
    assert pending[0].latitude is None


def fake_fetch_row(outing_id, date, route_id, elevation_m, status="ok"):
    return {
        "outing_id": outing_id, "outing_date": datetime.date.fromisoformat(date), "route_id": route_id,
        "latitude": 45.9, "longitude": 6.9, "elevation_m": elevation_m, "status": status, "n_days": 1,
        "api_weight": 4.5, "fetched_at": datetime.datetime.now(datetime.timezone.utc),
        "feature_version": FEATURE_VERSION,
    }


def test_write_and_refetch_conditions(engine):
    fill_route_altitudes(engine)
    add_outing(engine, 100, "2024-01-20", 2)
    day = {"date_local": datetime.date(2024, 1, 20), "days_before_outing": 0, "n_hours": 24, "t_min_c": -3.0,
           "night_hours": 10, "night_clear_h": 5, "night_partial_h": 3, "night_cloudy_h": 2}
    write_outing(engine, fake_fetch_row(100, "2024-01-20", 2, 1200), [day])
    write_outing(engine, fake_fetch_row(100, "2024-01-20", 2, 1200), [day])  # replace, no duplicate
    assert load_pending_outings(engine, datetime.date(2025, 1, 1)) == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM outing_weather_daily")).scalar_one() == 1

    # Outing date changed by the c2c refresh -> fetched again
    with engine.begin() as conn:
        conn.execute(text("UPDATE outings SET date = '2024-01-21' WHERE outing_id = 100"))
    assert [o.outing_id for o in load_pending_outings(engine, datetime.date(2025, 1, 1))] == [100]

    # Altitude changed (recompute) -> fetched again
    write_outing(engine, fake_fetch_row(100, "2024-01-21", 2, 1200), [day])
    with engine.begin() as conn:
        conn.execute(text("UPDATE routes SET difficulties_height = 1300 WHERE route_id = 2"))
    fill_route_altitudes(engine, recompute=True)
    assert [o.outing_id for o in load_pending_outings(engine, datetime.date(2025, 1, 1))] == [100]

    # Deleting the outing removes its weather
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM outings_mapping; DELETE FROM outings"))
        assert conn.execute(text("SELECT COUNT(*) FROM outing_weather_daily")).scalar_one() == 0


def test_no_location_and_budget(engine, monkeypatch):
    add_outing(engine, 101, "2024-02-20", 7)
    add_outing(engine, 100, "2024-01-20", 2)
    calls = []

    def fake_fetch(*args):
        calls.append(args)
        raise AssertionError("budget must stop the run before any request")

    monkeypatch.setattr("outing_weather.scraper.fetch_hourly", fake_fetch)
    summary = OutingWeatherScraper(engine, daily_budget=1.0).run()
    assert summary["no_location"] == 1 and summary["stopped"] == "daily_budget" and summary["left"] == 1
    assert calls == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM outing_weather_fetch")).scalar_one() == "no_location"
