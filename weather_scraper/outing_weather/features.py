"""Hourly Open-Meteo values -> daily features of the days before an outing.

Same definitions as weather/ (daily_features), computed in memory:
  instantaneous fields: hours in [d 00:00, d+1 00:00) local
  accumulated fields:   hours ending in (d 00:00, d+1 00:00] local (Open-Meteo: value at t = sum over (t-1h, t])
  night sky:            hours of [d-1 12:00, d 12:00) local with is_day = 0 (the night BEFORE day d)
plus the day sky:       hours of [d 00:00, d+1 00:00) local with is_day = 1
A day is computed only when every hour of its windows has a temperature.
"""
from __future__ import annotations

import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

UTC = datetime.timezone.utc
HOUR = timedelta(hours=1)

CLEAR_MAX_PCT = 25    # cloud_cover <= 25 %: clear (<= 2 oktas)
CLOUDY_MIN_PCT = 75   # cloud_cover >= 75 %: cloudy (>= 6 oktas)

FEATURE_COLUMNS = [
    "n_hours",
    "t_min_c", "t_max_c", "t_mean_c", "hours_above_0c", "hours_air_le_0c",
    "precip_mm", "rain_mm", "snow_we_mm", "snowfall_cm",
    "fl_min_m", "fl_mean_m", "fl_max_m", "hours_fl_below_alt",
    "night_hours", "night_clear_h", "night_partial_h", "night_cloudy_h", "night_cloud_mean_pct",
    "day_hours", "day_clear_h", "day_partial_h", "day_cloudy_h", "day_cloud_mean_pct",
]


def local_to_utc(d: datetime.date, hour: int, tz: ZoneInfo) -> datetime.datetime:
    return datetime.datetime.combine(d, datetime.time(hour), tzinfo=tz).astimezone(UTC)


def hours_between(start: datetime.datetime, end: datetime.datetime) -> list[datetime.datetime]:
    """UTC hours in [start, end)."""
    out, t = [], start
    while t < end:
        out.append(t)
        t += HOUR
    return out


def hourly_rows(response: dict, var_map: dict[str, str]) -> dict[datetime.datetime, dict]:
    """{UTC hour: {key: value}} from an Open-Meteo response requested with timeformat=unixtime.
    Variables missing from `var_map` (e.g. showers for the archive API) are None."""
    hourly = response["hourly"]
    times = hourly["time"]
    keys = set(var_map.values()) | {
        "temperature_2m_c", "precipitation_mm", "rain_mm", "showers_mm", "snowfall_cm",
        "freezing_level_height_m", "cloud_cover_pct", "is_day",
    }
    rows = {}
    for i, ts in enumerate(times):
        row = dict.fromkeys(keys)
        for var, key in var_map.items():
            values = hourly.get(var)
            v = values[i] if values is not None else None
            if v is not None and key in ("cloud_cover_pct", "is_day"):
                v = int(round(v))
            row[key] = v
        rows[datetime.datetime.fromtimestamp(ts, UTC)] = row
    return rows


def _round(v, nd=2):
    return None if v is None else round(v, nd)


def _sum(values):
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def _sky(rows) -> tuple:
    """(hours, clear_h, partial_h, cloudy_h, mean_pct) over the rows with a cloud cover."""
    clouds = [r["cloud_cover_pct"] for r in rows if r["cloud_cover_pct"] is not None]
    if not clouds:
        return None, None, None, None, None
    n = len(clouds)
    clear = sum(1 for c in clouds if c <= CLEAR_MAX_PCT)
    cloudy = sum(1 for c in clouds if c >= CLOUDY_MIN_PCT)
    return n, clear, n - clear - cloudy, cloudy, _round(sum(clouds) / n, 1)


def day_features(inst, acc, night, elevation_m: float | None) -> dict:
    """Features of one day from its instantaneous rows, accumulated rows and night rows."""
    temps = [r["temperature_2m_c"] for r in inst if r["temperature_2m_c"] is not None]
    if temps:
        t_min, t_max, t_mean = min(temps), max(temps), _round(sum(temps) / len(temps), 1)
        above = sum(1 for t in temps if t > 0)
        le0 = len(temps) - above
    else:
        t_min = t_max = t_mean = above = le0 = None

    precip = _sum(r["precipitation_mm"] for r in acc)
    liquid_per_hour = [
        None if r["rain_mm"] is None and r["showers_mm"] is None
        else (r["rain_mm"] or 0) + (r["showers_mm"] or 0)
        for r in acc
    ]
    rain = _sum(liquid_per_hour)
    snow_we = max(0.0, precip - rain) if precip is not None and rain is not None else None
    snowfall = _sum(r["snowfall_cm"] for r in acc)

    fls = [r["freezing_level_height_m"] for r in inst if r["freezing_level_height_m"] is not None]
    if fls:
        fl_min, fl_mean, fl_max = min(fls), sum(fls) / len(fls), max(fls)
        fl_below = sum(1 for f in fls if f < elevation_m) if elevation_m is not None else None
    else:
        fl_min = fl_mean = fl_max = fl_below = None

    night_sky = _sky(night)
    day_sky = _sky([r for r in inst if r["is_day"] == 1])

    return dict(zip(FEATURE_COLUMNS, (
        len(inst),
        t_min, t_max, t_mean, above, le0,
        _round(precip), _round(rain), _round(snow_we), _round(snowfall),
        fl_min, _round(fl_mean, 1), fl_max, fl_below,
        *night_sky,
        *day_sky,
    )))


def outing_daily_rows(
    response: dict,
    var_map: dict[str, str],
    outing_date: datetime.date,
    window_days: int,
    elevation_m: float | None,
) -> list[dict]:
    """Daily rows for the local days outing_date - (window_days - 1) .. outing_date.
    `elevation_m` is the altitude the temperature refers to (used for hours_fl_below_alt).
    Days whose windows are not fully covered by hours with a temperature are left out."""
    tz = ZoneInfo(response["timezone"])
    rows = hourly_rows(response, var_map)
    out = []
    for back in range(window_days - 1, -1, -1):
        d = outing_date - timedelta(days=back)
        day_start = local_to_utc(d, 0, tz)
        next_start = local_to_utc(d + timedelta(days=1), 0, tz)
        night_start = local_to_utc(d - timedelta(days=1), 12, tz)
        night_end = local_to_utc(d, 12, tz)

        # All windows lie within [night_start, next_start] (accumulated values end one hour
        # after next_start); require every hour to be present with a temperature.
        needed = hours_between(night_start, next_start + HOUR)
        if not all(t in rows and rows[t]["temperature_2m_c"] is not None for t in needed):
            continue
        inst = [rows[t] for t in hours_between(day_start, next_start)]
        acc = [rows[t] for t in hours_between(day_start + HOUR, next_start + HOUR)]
        night = [rows[t] for t in hours_between(night_start, night_end) if rows[t]["is_day"] == 0]
        out.append({
            "date_local": d,
            "days_before_outing": back,
            **day_features(inst, acc, night, elevation_m),
        })
    return out
