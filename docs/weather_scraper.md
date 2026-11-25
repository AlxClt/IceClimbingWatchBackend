# Outing weather scraper

[weather_scraper/](../weather_scraper/) adds weather to the training data collected by the camptocamp scraper ([c2c_scraper.md](c2c_scraper.md)). For every **ice-climbing outing** it fetches the Open-Meteo hourly values of the 2 months before the outing and stores one row of daily features per day, linked to the outing by `outing_id`. It writes to the same PostgreSQL database as the c2c scraper and runs on the server right after `c2c update`. The daily `weather/` pipeline (SQLite, fixed locations) is separate and is not used.

## 1. Running it

```bash
cd weather_scraper
export DATABASE_URL=postgresql+psycopg://user:password@host:5432/dbname
python main.py update                       # pending outings, within the daily API budget
python main.py update --dry-run             # count pending outings and estimate the API weight
python main.py update --limit 20            # at most 20 outings (tests)
python main.py update --recompute-altitudes # recompute route altitudes, re-fetch outings whose altitude changed
```

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | (required) | Same database as the c2c scraper |
| `OPENMETEO_DAILY_BUDGET` | `8000` | Open-Meteo weighted calls allowed over a rolling 24 h (free tier: 10,000/day per IP). `--daily-budget` overrides it. |
| `LOG_LEVEL`, `LOG_JSON`, `LOG_TO_FILE`, `LOG_DIR` | as in the c2c scraper | Logging |

Every run first applies the pending migrations of the weather tables (section 6). The c2c tables must already exist: run `c2c init` first.

## 2. Which outings

An outing is **pending** when it is linked (`outings_mapping`) to at least one route with `ice_climbing = 1`, its date is at least 3 days old (so that the outing day's data is complete), and one of these holds:

- it has no `outing_weather_fetch` row yet;
- its date changed since it was fetched (the c2c scraper refreshes outings for 7 days);
- its route or that route's weather altitude changed (after `--recompute-altitudes`);
- `feature_version` is older than `FEATURE_VERSION` in [config.py](../weather_scraper/outing_weather/config.py).

Pending outings are processed newest first. The **route used** is the outing's ice-climbing route with the lowest `route_id`. Its `lat` / `lon` are the requested position. Only `outings.date` (camptocamp's `date_start`) is used, so a multi-day outing is handled like a one-day outing on its first day.

## 3. Route altitude

Open-Meteo downscales the temperature to the `elevation` parameter. Before selecting the outings, each run computes the altitude of every ice-climbing route that has none yet, into `route_weather_altitude`. The first rule that gives a value > 0 is used:

| rule_id | name | altitude |
|---|---|---|
| 1 | `difficulties_height` | `difficulties_height` (altitude where the difficulties start) |
| 2 | `elevation_max_minus_height_diff` | `elevation_max - height_diff_difficulties` |
| 3 | `elevation_max_minus_mean_height_diff` | `elevation_max - mean(height_diff_difficulties)`, where the mean is over the ice-climbing routes that have one |
| 4 | `elevation_min` | `elevation_min` |
| 5 | `open_meteo_dem` | NULL: no `elevation` is sent, and Open-Meteo uses its 90 m DEM at the route's position |

The rules are in the `weather_altitude_rules` table. Altitudes are computed once and then kept: rule 3's mean (stored in `mean_height_diff_m`) would otherwise shift slightly with every new route, and the stored weather would no longer match its altitude. `--recompute-altitudes` rebuilds the table, and outings whose altitude changed are fetched again.

## 4. Weather sources and features

The 61 local days D-60 … D are computed, where D is the outing date. The request covers D-61 … D+1, i.e. 63 days: the first day's night starts at 12:00 the day before, and the outing day's last accumulated hour ends at 00:00 the day after. The request uses `timezone=auto` and `timeformat=unixtime`, so local days follow the time zone of the route position, including DST days with 23 or 25 hours.

| Source (`outing_weather_fetch.source`) | Used when | Variables |
|---|---|---|
| `historical_forecast`: [Historical Forecast API](https://open-meteo.com/en/docs/historical-forecast-api), `best_match` | request start ≥ `HF_START` (2021-04-01) | Same as `weather/`: temperature_2m, precipitation, rain, showers, snowfall, freezing_level_height, cloud_cover, is_day |
| `archive`: [Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api), `best_match` (IFS HRES, ERA5, ERA5-Land) | older outings (back to 1940), or when the forecast API doesn't cover the outing day | temperature_2m, precipitation, rain, snowfall, cloud_cover, is_day. There is no showers and no freezing level: `rain_mm` is rain only and the `fl_*` columns are NULL. |

`HF_START` comes from test requests in the Alps. Before about 2021-03-23 the forecast API returns no showers and no freezing level.

### Retrieved values

**Hourly variables requested from Open-Meteo.** Hourly values are only used to compute the daily features; they are not stored.

| Open-Meteo variable | Unit | Kind | Historical forecast | Archive |
|---|---|---|---|---|
| `temperature_2m` | °C | instantaneous | ✓ | ✓ |
| `precipitation` | mm | accumulated, all phases | ✓ | ✓ |
| `rain` | mm | accumulated, liquid (large-scale) | ✓ | ✓ |
| `showers` | mm | accumulated, liquid (convective) | ✓ | (none) |
| `snowfall` | cm | accumulated, fresh snow | ✓ | ✓ |
| `freezing_level_height` | m a.s.l. | instantaneous | ✓ | (none) |
| `cloud_cover` | % | instantaneous, total | ✓ | ✓ |
| `is_day` | 0 / 1 | sun above the horizon | ✓ | ✓ |

The temperature is downscaled to the requested elevation (section 3). Instantaneous values hold at the hour's time. Accumulated values cover the hour that ends at that time (Open-Meteo convention).

**Time windows** of a local day d, the same as `daily_features` in `weather/` ([create_ice_weather_db.sql](../weather/data/create_ice_weather_db.sql)):

| Window | Hours | Used for |
|---|---|---|
| Day, instantaneous | [d 00:00, d+1 00:00) | temperature, freezing level, day sky (`is_day = 1` hours only) |
| Day, accumulated | hours ending in (d 00:00, d+1 00:00] | precipitation, rain, snowfall |
| Night before d | [d-1 12:00, d 12:00), `is_day = 0` hours only | night sky (the night that sets day d's ice conditions) |

A day is stored only when every hour of its windows has a temperature. A NULL feature means its inputs are all missing: for example, the freezing level from the archive API, or a sky window with no cloud cover.

**Daily columns of `outing_weather_daily`:**

| Column | Unit | Definition |
|---|---|---|
| `outing_id`, `date_local` | | Outing, and local date d (the route position's time zone) |
| `days_before_outing` | days | 0 = outing day … 60 |
| `n_hours` | h | Hours in the instantaneous window: 24, or 23 / 25 on DST days |
| `t_min_c` | °C | Min of the hourly temperature |
| `t_max_c` | °C | Max of the hourly temperature |
| `t_mean_c` | °C | Mean of the hourly temperature, rounded to 0.1 |
| `hours_above_0c` | h | Hours with temperature > 0 °C |
| `hours_air_le_0c` | h | Hours with temperature ≤ 0 °C. The sum with `hours_above_0c` is `n_hours`. |
| `precip_mm` | mm | Sum of `precipitation` |
| `rain_mm` | mm | Sum of `rain + showers` (archive: `rain` only) |
| `snow_we_mm` | mm | Snow as water equivalent: `max(0, precip_mm - rain_mm)` |
| `snowfall_cm` | cm | Sum of `snowfall` (fresh snow depth) |
| `fl_min_m`, `fl_mean_m`, `fl_max_m` | m a.s.l. | Min / mean (rounded to 0.1) / max of the freezing level. NULL for archive data. |
| `hours_fl_below_alt` | h | Hours with the freezing level below the elevation used (`outing_weather_fetch.grid_elevation_m`). NULL for archive data. |
| `night_hours` | h | Night hours with a cloud cover |
| `night_clear_h` | h | Night hours with cloud cover ≤ 25 % (≤ 2 oktas) |
| `night_partial_h` | h | Night hours with 25 % < cloud cover < 75 % |
| `night_cloudy_h` | h | Night hours with cloud cover ≥ 75 % (≥ 6 oktas) |
| `night_cloud_mean_pct` | % | Mean night cloud cover, rounded to 0.1 |
| `day_hours`, `day_clear_h`, `day_partial_h`, `day_cloudy_h`, `day_cloud_mean_pct` | h / % | Same as the night columns, over the daylight hours of d (`is_day = 1`) |

Precipitation sums are rounded to 0.01. The clear, partial and cloudy hours add up to the window's hours (a CHECK constraint enforces this). Compared with `daily_features` in `weather/`, there are three additions: `t_mean_c`, `snowfall_cm` and the day sky columns.

## 5. Open-Meteo rate limits

The free tier allows 600 calls/min, 5,000/h, 10,000/day and 300,000/month per IP. Open-Meteo counts `max(1, days/14) * max(1, variables/10)` calls per request. One outing (63 days, 8 or 6 variables) therefore counts **4.5 calls**, and the first backfill of about 13,500 outings counts about 61,000.

- **Pacing** (in-process, ported from `weather/`): at most 500 weight per rolling minute and 4,000 per rolling hour. A run of a full daily budget takes about 2 hours.
- **Daily budget across runs**: each fetch row stores its `api_weight`. A run may spend `OPENMETEO_DAILY_BUDGET` minus the weight spent over the last 24 hours, then stops cleanly (`"stopped": "daily_budget"`). With the default 8,000 that is about 1,750 outings per run, so the backfill takes about 8 daily runs. After that, only the new outings are fetched.
- **Errors**: 5xx errors, timeouts and per-minute 429s are retried with backoff. A 429 for the hourly, daily or monthly limit stops the run cleanly (`"stopped": "openmeteo_rate_limit"`). Any other failure skips the outing, which is retried next run. 10 failures in a row stop the run with an error.
- Each outing is written in its own transaction, so a stopped run keeps everything it fetched, and the next run continues with what is left.

The run ends with a `weather.summary` log line: pending, written, left, counts per status and per source, weight spent, and the reason it stopped.

## 6. Data written

All tables are created by this project's own Alembic history ([outing_weather/migrations/](../weather_scraper/outing_weather/migrations/)), whose revision is kept in `alembic_version_weather`. The c2c tables are not touched, and each Alembic environment ignores the other's tables.

| Table | Key | Content |
|---|---|---|
| `weather_altitude_rules` | `rule_id` | The 5 rules above (seeded by the migration) |
| `route_weather_altitude` | `route_id` → `routes` | `altitude_m`, `rule_id`, `mean_height_diff_m` (rule 3), `computed_at` |
| `outing_weather_fetch` | `outing_id` → `outings` (ON DELETE CASCADE) | One row per processed outing: `outing_date`, `route_id`, requested position and `elevation_m`, `grid_*` (cell and elevation used by Open-Meteo), `timezone`, `source`, `model`, requested dates, `request_url`, `status`, `n_days`, `api_weight`, `fetched_at`, `feature_version` |
| `outing_weather_daily` | (`outing_id` → `outing_weather_fetch`, `date_local`) | `days_before_outing` (0 = outing day … 60) and the daily features listed in [section 4](#retrieved-values) |

`status` is one of these values:

- `ok`: 61 days stored.
- `partial`: fewer than 61 days, because the source had missing hours. These outings are not retried unless their date changes.
- `no_location`: the route has no position.
- `out_of_range`: the window starts before 1940.

Example: the 2-week lead-up of each outing with its condition rating:

```sql
SELECT o.outing_id, o.date, o.conditions, d.days_before_outing, d.t_min_c, d.t_max_c,
       d.hours_above_0c, d.snow_we_mm, d.night_clear_h, d.day_clear_h
FROM outings o
JOIN outing_weather_daily d USING (outing_id)
WHERE d.days_before_outing < 14
ORDER BY o.outing_id, d.days_before_outing DESC;
```

## 7. Tests

```bash
cd weather_scraper
pip install -r requirements.txt pytest
pytest tests                       # feature tests (no network, no database)
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/test pytest tests
```

The database tests need a disposable PostgreSQL database: they drop its `public` schema. For example, start one with `docker run -d -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=test -p 5433:5432 postgres:16`.