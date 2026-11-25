"""Daily features computed from synthetic Open-Meteo responses (no network, no database)."""
import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

from outing_weather.config import HOURLY_VARS, SOURCE_ARCHIVE, SOURCE_HISTORICAL_FORECAST, WINDOW_DAYS
from outing_weather.features import local_to_utc, outing_daily_rows
from outing_weather.scraper import pick_source, request_period

TZ = "Europe/Paris"
HF_VARS = HOURLY_VARS[SOURCE_HISTORICAL_FORECAST]
AR_VARS = HOURLY_VARS[SOURCE_ARCHIVE]


def make_response(outing_date, values, var_map=HF_VARS, tz=TZ, elevation=1800.0):
    """Response as returned with timezone=auto&timeformat=unixtime for the request period of
    `outing_date`. `values(t_local)` returns {open-meteo variable: value} for one hour."""
    start, end = request_period(outing_date)
    zone = ZoneInfo(tz)
    t = local_to_utc(start, 0, zone)
    stop = local_to_utc(end + timedelta(days=1), 0, zone)
    hourly = {"time": []} | {v: [] for v in var_map}
    while t < stop:
        hourly["time"].append(int(t.timestamp()))
        vals = values(t.astimezone(zone))
        for v in var_map:
            hourly[v].append(vals.get(v))
        t += timedelta(hours=1)
    return {"latitude": 45.9, "longitude": 6.9, "elevation": elevation, "timezone": tz, "hourly": hourly}


def constant(t_local):
    return {
        "temperature_2m": -2.0 if t_local.hour < 12 else 3.0,
        "precipitation": 0.5,
        "rain": 0.1,
        "showers": 0.1,
        "snowfall": 0.2,
        "freezing_level_height": 1500.0 if t_local.hour < 12 else 2500.0,
        "cloud_cover": 10 if t_local.hour < 6 else (50 if t_local.hour < 18 else 90),
        "is_day": 1 if 8 <= t_local.hour < 18 else 0,
    }


def test_full_window_one_row_per_day():
    d = datetime.date(2024, 1, 20)
    rows = outing_daily_rows(make_response(d, constant), HF_VARS, d, WINDOW_DAYS, 1800.0)
    assert len(rows) == WINDOW_DAYS
    assert rows[0]["date_local"] == d - timedelta(days=WINDOW_DAYS - 1)
    assert rows[0]["days_before_outing"] == WINDOW_DAYS - 1
    assert rows[-1]["date_local"] == d and rows[-1]["days_before_outing"] == 0

    r = rows[-1]
    assert r["n_hours"] == 24
    assert (r["t_min_c"], r["t_max_c"], r["hours_above_0c"], r["hours_air_le_0c"]) == (-2.0, 3.0, 12, 12)
    assert r["t_mean_c"] == 0.5
    assert r["precip_mm"] == 12.0 and r["rain_mm"] == 4.8 and r["snow_we_mm"] == 7.2
    assert r["snowfall_cm"] == 4.8
    assert (r["fl_min_m"], r["fl_mean_m"], r["fl_max_m"], r["hours_fl_below_alt"]) == (1500.0, 2000.0, 2500.0, 12)
    # Day sky: 08:00-17:00 local, 10 hours at 50 %
    assert (r["day_hours"], r["day_clear_h"], r["day_partial_h"], r["day_cloudy_h"], r["day_cloud_mean_pct"]) == (
        10, 0, 10, 0, 50.0)
    # Night before: 18:00-23:00 (6 h at 90 %), 00:00-05:00 (6 h at 10 %), 06:00-07:00 (2 h at 50 %)
    assert (r["night_hours"], r["night_clear_h"], r["night_partial_h"], r["night_cloudy_h"]) == (14, 6, 2, 6)
    assert r["night_cloud_mean_pct"] == round((6 * 90 + 6 * 10 + 2 * 50) / 14, 1)


def test_dst_days():
    spring, autumn = datetime.date(2024, 3, 31), datetime.date(2024, 10, 27)
    rows = outing_daily_rows(make_response(spring, constant), HF_VARS, spring, WINDOW_DAYS, 1800.0)
    assert len(rows) == WINDOW_DAYS and rows[-1]["n_hours"] == 23
    rows = outing_daily_rows(make_response(autumn, constant), HF_VARS, autumn, WINDOW_DAYS, 1800.0)
    assert len(rows) == WINDOW_DAYS and rows[-1]["n_hours"] == 25


def test_missing_hour_drops_the_days_using_it():
    d = datetime.date(2024, 1, 20)
    gap = datetime.datetime(2024, 1, 10, 15, tzinfo=ZoneInfo(TZ))

    def with_gap(t_local):
        vals = constant(t_local)
        if t_local == gap:
            vals["temperature_2m"] = None
        return vals

    rows = outing_daily_rows(make_response(d, with_gap), HF_VARS, d, WINDOW_DAYS, 1800.0)
    dates = {r["date_local"] for r in rows}
    # 2024-01-10 15:00 is in day 01-10 and in the night window of 01-11 (from 01-10 12:00)
    assert datetime.date(2024, 1, 10) not in dates
    assert datetime.date(2024, 1, 11) not in dates
    assert len(rows) == WINDOW_DAYS - 2


def test_archive_has_no_freezing_level_and_rain_without_showers():
    d = datetime.date(2015, 2, 1)
    rows = outing_daily_rows(make_response(d, constant, AR_VARS), AR_VARS, d, WINDOW_DAYS, 1800.0)
    r = rows[-1]
    assert len(rows) == WINDOW_DAYS
    assert r["fl_min_m"] is None and r["fl_mean_m"] is None and r["hours_fl_below_alt"] is None
    assert r["rain_mm"] == 2.4 and r["snow_we_mm"] == 9.6


def test_source_choice():
    assert pick_source(request_period(datetime.date(2023, 1, 15))[0]) == SOURCE_HISTORICAL_FORECAST
    assert pick_source(request_period(datetime.date(2021, 5, 1))[0]) == SOURCE_ARCHIVE  # window starts in March
    assert pick_source(request_period(datetime.date(2010, 1, 15))[0]) == SOURCE_ARCHIVE
    assert pick_source(request_period(datetime.date(1940, 2, 1))[0]) is None
