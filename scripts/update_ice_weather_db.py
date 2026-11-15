"""Creates or updates the ice-climbing weather SQLite database.

Behaviour
---------
Database missing: create it using data/create_ice_weather_db.sql and backfills the last N days (default 21).
Database present but empty: backfill the last N days.
Database present with data: fetch the missing period from the last run date

A forecast series that has no data yet (e.g. a location added after the last run)
is backfilled like an empty database.

Data flow: Open-Meteo Historical Forecast API -> raw_hourly -> daily_features.

Hours up to the current hour are stored. 
daily_features rows are written only for local days whose windows are fully covered by hourly data, so the
most recent day appears once it is complete.

API usage limits: 

free tier: 600 calls/min, 5 000/h, 10 000/day, per IP. 
Open-Meteo weighs each request: weight = locations * max(1, days/14) * max(1, variables/10).
The script splits large requests so that no request weighs more than MAX_CALL_WEIGHT,
paces requests to stay under MINUTE_BUDGET / HOUR_BUDGET, and retries failed requests. 
An hourly/daily 429 or a 4xx error stops the run. Each group of series is
written in one transaction once all its requests have succeeded, so a failed run
writes nothing and the next run catches up from the same point.

After the update, the views v_series_daily_21d and v_series_summary_21d are exported
to CSV files (one file per view, named after the view) in data/exports/.

Usage:  python scripts/update_ice_weather_db.py [--db PATH] [--days 21]
                [--export-dir DIR] [--export-only | --no-export]
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import math
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "ice_weather.db"
SCHEMA = ROOT / "data" / "create_ice_weather_db.sql"
DEFAULT_EXPORT_DIR = ROOT / "data" / "exports"
EXPORT_VIEWS = ("v_series_daily_21d", "v_series_summary_21d")

OPEN_METEO_SOURCE_ID = 1
FEATURE_VERSION = 1
HTTP_TIMEOUT_S = 120

# Open-Meteo rate limiting (free tier: 600/min, 5 000/h, 10 000/day weighted calls per IP)
MINUTE_BUDGET = 500     # weight per rolling 60 s (limit 600; headroom for other users of the IP)
HOUR_BUDGET = 4000      # weight per rolling hour (limit 5 000)
MAX_CALL_WEIGHT = 150   # cap for a single request, so chunks fit easily in the minute budget
MAX_ATTEMPTS = 5        # per request, including the first one
BACKOFF_BASE_S = 2      # 2, 4, 8, 16 s (+ jitter), capped at BACKOFF_MAX_S
BACKOFF_MAX_S = 60
RATE_LIMIT_WAIT_S = 60  # wait after a per-minute 429 without Retry-After header

# Open-Meteo hourly variable -> raw_hourly column
HOURLY_VARS = {
    "temperature_2m": "temperature_2m_c",
    "precipitation": "precipitation_mm",
    "rain": "rain_mm",
    "showers": "showers_mm",
    "snowfall": "snowfall_cm",
    "freezing_level_height": "freezing_level_height_m",
    "cloud_cover": "cloud_cover_pct",
    "is_day": "is_day",
}
RAW_COLUMNS = list(HOURLY_VARS.values())
INTEGER_COLUMNS = {"cloud_cover_pct", "is_day"}

UTC = timezone.utc
HOUR = timedelta(hours=1)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(dt: datetime) -> str:
    """raw_hourly.time_utc format: 'YYYY-MM-DDTHH:00Z'."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:00Z")


def hours_between(start: datetime, end: datetime) -> list[datetime]:
    """UTC hours in [start, end)."""
    out, t = [], start
    while t < end:
        out.append(t)
        t += HOUR
    return out


def local_to_utc(d: date, hour: int, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, dt_time(hour), tzinfo=tz).astimezone(UTC)


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------
def open_db(db_path: Path) -> sqlite3.Connection:
    created = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    has_schema = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sources'"
    ).fetchone()
    if not has_schema:
        print(f"{'Creating' if created else 'Initialising'} database {db_path}")
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.commit()

    # Pick up locations / default altitudes added since the last run (idempotent).
    conn.execute(
        """
        INSERT OR IGNORE INTO series (location_id, type, source_id, elevation_m, model)
        SELECT location_id, 'forecast', source_id, elevation_m, 'best_match'
        FROM v_requested_altitudes
        ORDER BY location_id, elevation_m
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Planning: which series need which period
# ---------------------------------------------------------------------------
def plan_fetches(conn: sqlite3.Connection, days: int, today: date) -> dict[date, list[sqlite3.Row]]:
    """Group the Open-Meteo forecast series by the UTC start date they must be fetched from."""
    last_run_end = conn.execute(
        "SELECT MAX(end_date) FROM fetch_runs WHERE source_id = ?", (OPEN_METEO_SOURCE_ID,)
    ).fetchone()[0]

    # Backfill start: `days` complete local days ending yesterday, plus one spare day so
    # that `days` complete days exist even when yesterday's night window is not over yet,
    # plus one day of margin for the UTC/local offset.
    backfill_start = today - timedelta(days=days + 2)

    rows = conn.execute(
        """
        SELECT s.series_id, s.location_id, s.elevation_m, s.model,
               s.point_latitude, s.point_longitude, s.point_elevation_m,
               l.latitude, l.longitude, l.timezone,
               EXISTS (SELECT 1 FROM raw_hourly r WHERE r.series_id = s.series_id) AS has_data
        FROM series AS s
        JOIN locations AS l USING (location_id)
        WHERE s.type = 'forecast' AND s.source_id = ?
        ORDER BY s.series_id
        """,
        (OPEN_METEO_SOURCE_ID,),
    ).fetchall()

    groups: dict[date, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if r["has_data"] and last_run_end:
            start = min(date.fromisoformat(last_run_end), today)
        else:
            start = backfill_start
        groups[start].append(r)
    # Incremental group first: if a later backfill fails, its series stay empty and are
    # backfilled again next time, while MAX(fetch_runs.end_date) is only advanced by a
    # run that really covered the series that already had data.
    return dict(sorted(groups.items(), reverse=True))


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------
def call_weight(n_locations: int, n_days: int, n_variables: int) -> float:
    """Number of API calls Open-Meteo counts for one request."""
    return n_locations * max(1.0, n_days / 14) * max(1.0, n_variables / 10)


class RateLimiter:
    """Client-side pacing over rolling windows of request weights (within one process)."""

    def __init__(self, windows=((60.0, MINUTE_BUDGET), (3600.0, HOUR_BUDGET))):
        self.windows = windows
        self.longest = max(span for span, _ in windows)
        self.calls: deque[tuple[float, float]] = deque()  # (monotonic time, weight), oldest first

    def _wait_needed(self, weight: float, now: float) -> float:
        wait = 0.0
        for span, budget in self.windows:
            recent = [(t, w) for t, w in self.calls if t > now - span]
            excess = sum(w for _, w in recent) + min(weight, budget) - budget
            # Wait until the oldest calls carrying at least `excess` weight leave the window.
            for t, w in recent:
                if excess <= 0:
                    break
                excess -= w
                wait = max(wait, t + span - now)
        return wait

    def acquire(self, weight: float) -> None:
        while True:
            now = time.monotonic()
            while self.calls and self.calls[0][0] <= now - self.longest:
                self.calls.popleft()
            wait = self._wait_needed(weight, now)
            if wait <= 0:
                break
            print(f"  rate limit: pausing {wait:.0f} s")
            time.sleep(wait)
        self.calls.append((time.monotonic(), weight))


_limiter = RateLimiter()


def _error_reason(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - body is only informative
        return ""
    try:
        return str(json.loads(body).get("reason", body))
    except (ValueError, AttributeError):
        return body


def _retry_after_s(e: urllib.error.HTTPError) -> float | None:
    value = e.headers.get("Retry-After") if e.headers else None
    try:
        return min(max(float(value), 1.0), 3600.0) if value else None
    except ValueError:
        return None  # HTTP-date form: fall back to the default wait


def _backoff_s(attempt: int) -> float:
    return min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (attempt - 1)) + random.uniform(0, 1)


def http_get_json(url: str, weight: float):
    """GET a JSON document, paced by the rate limiter and retried on transient failures."""
    req = urllib.request.Request(url, headers={"User-Agent": "IceClimbingWatch/1.0"})
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _limiter.acquire(weight)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            reason = _error_reason(e)
            if e.code == 429:
                if any(k in reason.lower() for k in ("hourly", "daily", "monthly")):
                    raise RuntimeError(
                        f"Open-Meteo rate limit reached: {reason} "
                        "Nothing was written; rerun later to catch up."
                    ) from None
                delay = _retry_after_s(e) or RATE_LIMIT_WAIT_S
                problem = f"HTTP 429{': ' + reason if reason else ''}"
            elif e.code >= 500:
                delay = _backoff_s(attempt)
                problem = f"HTTP {e.code}"
            else:
                raise RuntimeError(f"Open-Meteo HTTP {e.code}: {reason}") from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                ConnectionError, ValueError) as e:  # ValueError covers invalid JSON
            delay = _backoff_s(attempt)
            problem = f"{type(e).__name__}: {e}"
        if attempt == MAX_ATTEMPTS:
            raise RuntimeError(f"Open-Meteo request failed after {MAX_ATTEMPTS} attempts ({problem})")
        print(f"  attempt {attempt}/{MAX_ATTEMPTS} failed ({problem}), retrying in {delay:.1f} s")
        time.sleep(delay)


def build_url(base: str, params: dict) -> str:
    return base + "?" + urllib.parse.urlencode(params, safe=",")


def plan_chunks(series: list, start: date, end: date, n_variables: int = len(HOURLY_VARS)):
    """Split a (series, period) request into requests weighing at most MAX_CALL_WEIGHT.
    Returns [(series_subset, chunk_start, chunk_end)], consecutive periods, oldest first."""
    var_factor = max(1.0, n_variables / 10)
    per_chunk = max(1, min(len(series), math.floor(MAX_CALL_WEIGHT / var_factor)))
    chunks = []
    for i in range(0, len(series), per_chunk):
        subset = series[i:i + per_chunk]
        days_per_chunk = max(14, math.floor(14 * MAX_CALL_WEIGHT / (len(subset) * var_factor)))
        d = start
        while d <= end:
            chunk_end = min(end, d + timedelta(days=days_per_chunk - 1))
            chunks.append((subset, d, chunk_end))
            d = chunk_end + timedelta(days=1)
    return chunks


def fetch_forecast(api_url: str, series: list[sqlite3.Row], start: date, end: date):
    """One multi-coordinate request for all `series`; returns (url, params, responses)."""
    params = {
        "latitude": ",".join(str(s["latitude"]) for s in series),
        "longitude": ",".join(str(s["longitude"]) for s in series),
        "elevation": ",".join(str(s["elevation_m"]) for s in series),
        "hourly": ",".join(HOURLY_VARS),
        "models": "best_match",
        "timezone": "GMT",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    url = build_url(api_url, params)
    n_days = (end - start).days + 1
    data = http_get_json(url, call_weight(len(series), n_days, len(HOURLY_VARS)))
    responses = data if isinstance(data, list) else [data]
    if len(responses) != len(series):
        raise RuntimeError(f"Expected {len(series)} locations in response, got {len(responses)}")
    return url, params, responses


def lookup_missing_points(api_url: str, series, responses) -> list[tuple]:
    """For series without grid-cell info: (series_id, cell lat, cell lon, cell height).
    Network only; the caller writes the result."""
    todo, seen = [], set()
    for s, r in zip(series, responses):
        if s["point_latitude"] is None and s["series_id"] not in seen:
            seen.add(s["series_id"])
            todo.append((s, r))
    out = []
    # Cell height: query the cell centre with elevation=nan (no downscaling).
    d = (utc_now().date() - timedelta(days=2)).isoformat()
    per_call = math.floor(MAX_CALL_WEIGHT)
    for i in range(0, len(todo), per_call):
        part = todo[i:i + per_call]
        params = {
            "latitude": ",".join(str(r["latitude"]) for _, r in part),
            "longitude": ",".join(str(r["longitude"]) for _, r in part),
            "elevation": ",".join("nan" for _ in part),
            "hourly": "temperature_2m",
            "models": "best_match",
            "timezone": "GMT",
            "start_date": d,
            "end_date": d,
        }
        cells = http_get_json(build_url(api_url, params), call_weight(len(part), 1, 1))
        cells = cells if isinstance(cells, list) else [cells]
        for (s, r), cell in zip(part, cells):
            out.append((s["series_id"], r["latitude"], r["longitude"], round(cell["elevation"])))
    return out


def store_hourly(conn, run_id: int, series, responses, cutoff: datetime) -> int:
    """Upsert hourly rows up to `cutoff` (inclusive). Rows with no value at all are skipped."""
    cols = ", ".join(RAW_COLUMNS)
    placeholders = ", ".join("?" for _ in range(len(RAW_COLUMNS) + 3))
    updates = ", ".join(f"{c} = excluded.{c}" for c in ["run_id"] + RAW_COLUMNS)
    sql = (
        f"INSERT INTO raw_hourly (series_id, time_utc, run_id, {cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (series_id, time_utc) DO UPDATE SET {updates}"
    )
    measured_cols = [c for c in RAW_COLUMNS if c != "is_day"]
    n = 0
    for s, resp in zip(series, responses):
        hourly = resp["hourly"]
        rows = []
        for i, t in enumerate(hourly["time"]):
            ts = datetime.fromisoformat(t).replace(tzinfo=UTC)
            if ts > cutoff:
                break
            values = {}
            for var, col in HOURLY_VARS.items():
                v = hourly.get(var, [None] * len(hourly["time"]))[i]
                if v is not None and col in INTEGER_COLUMNS:
                    v = int(round(v))
                values[col] = v
            if all(values[c] is None for c in measured_cols):
                continue
            rows.append((s["series_id"], iso_utc(ts), run_id, *(values[c] for c in RAW_COLUMNS)))
        conn.executemany(sql, rows)
        n += len(rows)
    return n


# ---------------------------------------------------------------------------
# Daily features
# ---------------------------------------------------------------------------
def _round(v, nd=2):
    return None if v is None else round(v, nd)


def _sum(values):
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def compute_daily_features(conn, series, first_day: date, last_day: date, computed_at: str) -> int:
    """Recompute daily_features for each series and local day in [first_day, last_day].
    Days whose windows are not fully covered by hourly rows are skipped."""
    sql = """
        INSERT OR REPLACE INTO daily_features (
            series_id, date_local, n_hours,
            t_min_c, t_max_c, hours_above_0c, hours_air_le_0c,
            precip_mm, rain_mm, snow_we_mm,
            fl_min_m, fl_mean_m, fl_max_m, hours_fl_below_alt,
            night_hours, night_clear_h, night_partial_h, night_cloudy_h, night_cloud_mean_pct,
            computed_at_utc, feature_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    written = 0
    for s in series:
        tz = ZoneInfo(s["timezone"])
        lo = iso_utc(local_to_utc(first_day, 0, tz))
        hi = iso_utc(local_to_utc(last_day + timedelta(days=1), 12, tz))
        hourly = {
            r["time_utc"]: r
            for r in conn.execute(
                "SELECT * FROM raw_hourly WHERE series_id = ? AND time_utc BETWEEN ? AND ?",
                (s["series_id"], lo, hi),
            )
        }

        d = first_day
        while d <= last_day:
            day_start = local_to_utc(d, 0, tz)
            next_start = local_to_utc(d + timedelta(days=1), 0, tz)
            night_start = local_to_utc(d, 12, tz)
            night_end = local_to_utc(d + timedelta(days=1), 12, tz)

            # All windows lie within [day_start, night_end]; require every hour to be present.
            if all(iso_utc(t) in hourly for t in hours_between(day_start, night_end)):
                inst = [hourly[iso_utc(t)] for t in hours_between(day_start, next_start)]
                acc = [hourly[iso_utc(t)] for t in hours_between(day_start + HOUR, next_start + HOUR)]
                night = [
                    hourly[iso_utc(t)]
                    for t in hours_between(night_start, night_end)
                    if hourly[iso_utc(t)]["is_day"] == 0
                ]
                conn.execute(sql, (s["series_id"], d.isoformat(), len(inst),
                                   *_features(inst, acc, night, s["elevation_m"]),
                                   computed_at, FEATURE_VERSION))
                written += 1
            d += timedelta(days=1)
    return written


def _features(inst, acc, night, elevation_m):
    temps = [r["temperature_2m_c"] for r in inst if r["temperature_2m_c"] is not None]
    if temps:
        t_min, t_max = min(temps), max(temps)
        above = sum(1 for t in temps if t > 0)
        le0 = len(temps) - above
    else:
        t_min = t_max = above = le0 = None

    precip = _sum(r["precipitation_mm"] for r in acc)
    liquid_per_hour = [
        None if r["rain_mm"] is None and r["showers_mm"] is None
        else (r["rain_mm"] or 0) + (r["showers_mm"] or 0)
        for r in acc
    ]
    rain = _sum(liquid_per_hour)
    snow_we = max(0.0, precip - rain) if precip is not None and rain is not None else None

    fls = [r["freezing_level_height_m"] for r in inst if r["freezing_level_height_m"] is not None]
    if fls:
        fl_min, fl_mean, fl_max = min(fls), sum(fls) / len(fls), max(fls)
        fl_below = sum(1 for f in fls if f < elevation_m)
    else:
        fl_min = fl_mean = fl_max = fl_below = None

    clouds = [r["cloud_cover_pct"] for r in night if r["cloud_cover_pct"] is not None]
    if clouds:
        n_night = len(clouds)
        clear = sum(1 for c in clouds if c <= 25)
        cloudy = sum(1 for c in clouds if c >= 75)
        partial = n_night - clear - cloudy
        cloud_mean = sum(clouds) / n_night
    else:
        n_night = clear = partial = cloudy = cloud_mean = None

    return (
        t_min, t_max, above, le0,
        _round(precip), _round(rain), _round(snow_we),
        fl_min, _round(fl_mean, 1), fl_max, fl_below,
        n_night, clear, partial, cloudy, _round(cloud_mean, 1),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(db_path: Path, days: int) -> None:
    conn = open_db(db_path)
    now = utc_now()
    today = now.date()
    cutoff = now.replace(minute=0, second=0, microsecond=0)  # last complete-or-current hour
    fetched_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    api_url = conn.execute(
        "SELECT api_url FROM sources WHERE source_id = ?", (OPEN_METEO_SOURCE_ID,)
    ).fetchone()[0]

    plan = plan_fetches(conn, days, today)
    if not plan:
        print("No forecast series defined; nothing to do.")
        return

    for start, series in plan.items():
        chunks = plan_chunks(series, start, today)
        print(f"Fetching {len(series)} series from {start} to {today} "
              f"in {len(chunks)} request(s) ...")

        # 1. Network only: every request of the group must succeed before anything is written.
        fetched = []
        for subset, chunk_start, chunk_end in chunks:
            url, params, responses = fetch_forecast(api_url, subset, chunk_start, chunk_end)
            fetched.append((subset, chunk_start, chunk_end, url, params, responses))
        points = lookup_missing_points(
            api_url,
            [s for f in fetched for s in f[0]],
            [r for f in fetched for r in f[5]],
        )

        # 2. One transaction per group: one fetch_runs row per request (exact URL called).
        n_hourly, run_ids = 0, []
        with conn:
            conn.executemany(
                "UPDATE series SET point_latitude = ?, point_longitude = ?, point_elevation_m = ? "
                "WHERE series_id = ?",
                [(lat, lon, z, sid) for sid, lat, lon, z in points],
            )
            for subset, chunk_start, chunk_end, url, params, responses in fetched:
                run_id = conn.execute(
                    "INSERT INTO fetch_runs (source_id, fetched_at_utc, endpoint, start_date, "
                    "end_date, request_params) VALUES (?, ?, ?, ?, ?, ?)",
                    (OPEN_METEO_SOURCE_ID, fetched_at, url, chunk_start.isoformat(),
                     chunk_end.isoformat(), json.dumps(params)),
                ).lastrowid
                run_ids.append(run_id)
                n_hourly += store_hourly(conn, run_id, subset, responses, cutoff)
            # The day before `start` too: its night window reaches into `start`.
            n_daily = compute_daily_features(
                conn, series, start - timedelta(days=1), today, fetched_at
            )
        if points:
            print(f"  filled grid-cell coordinates for {len(points)} new series")
        runs = f"run {run_ids[0]}" if len(run_ids) == 1 else f"runs {run_ids[0]}-{run_ids[-1]}"
        print(f"  {runs}: {n_hourly} hourly rows upserted, {n_daily} daily rows computed")

    summary = conn.execute(
        "SELECT COUNT(*), MIN(n_days), MAX(n_days), MAX(last_date) FROM v_series_summary_21d"
    ).fetchone()
    print(f"v_series_summary_21d: {summary[0]} series, n_days {summary[1]}-{summary[2]}, "
          f"last date {summary[3]}")
    conn.close()


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
def export_views(db_path: Path, out_dir: Path, views=EXPORT_VIEWS) -> None:
    """Write each view to <out_dir>/<view>.csv (header row + all rows, UTF-8).
    Each file is written to a temporary file first and then swapped in, so a failed
    export never leaves a truncated CSV behind."""
    if not db_path.exists():
        raise RuntimeError(f"Database {db_path} not found; nothing to export.")
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        for view in views:
            cur = conn.execute(f'SELECT * FROM "{view}"')
            target = out_dir / f"{view}.csv"
            tmp = target.with_suffix(".csv.tmp")
            with tmp.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(col[0] for col in cur.description)
                n = 0
                for row in cur:
                    writer.writerow(row)
                    n += 1
            tmp.replace(target)
            print(f"Exported {n} rows of {view} to {target}")
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite file (default: {DEFAULT_DB})")
    p.add_argument("--days", type=int, default=21, help="backfill length for empty series (default: 21)")
    p.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR,
                   help=f"folder for the CSV exports of the views (default: {DEFAULT_EXPORT_DIR})")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--export-only", action="store_true",
                      help="skip the update and only export the views to CSV")
    mode.add_argument("--no-export", action="store_true",
                      help="update the database without exporting the views to CSV")
    args = p.parse_args()
    try:
        if not args.export_only:
            run(args.db, args.days)
        if not args.no_export:
            export_views(args.db, args.export_dir)
    except Exception as e:  # noqa: BLE001 - report and exit non-zero
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
