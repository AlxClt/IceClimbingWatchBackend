-- Ice-climbing weather store — SQLite schema
-- Requires SQLite >= 3.33. Run: sqlite3 ice_weather.db < ice_weather_schema.sql
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 0. SOURCE
--    type: 'forecast' = model output (Open-Meteo), 'measured' = station observations.
--    UNIQUE (source_id, type) lets series enforce series.type = sources.type through a composite FK.
-- ---------------------------------------------------------------------------
CREATE TABLE sources (
    source_id  INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    type       TEXT    NOT NULL CHECK (type IN ('forecast', 'measured')),
    api_url    TEXT    NOT NULL,                    -- base endpoint
    UNIQUE (source_id, type)
);
INSERT INTO sources (source_id, name, type, api_url) VALUES
    (1, 'Open-Meteo', 'forecast', 'https://historical-forecast-api.open-meteo.com/v1/forecast');

-- ---------------------------------------------------------------------------
-- 1. LOCATIONS — one row per climbing area (not per altitude).
-- ---------------------------------------------------------------------------
CREATE TABLE locations (
    location_id                  INTEGER PRIMARY KEY,
    name                         TEXT    NOT NULL UNIQUE,  -- 'Chamonix'
    country                      TEXT    NOT NULL,         -- ISO 3166-1 alpha-2
    latitude                     REAL    NOT NULL,         -- WGS84, village point (sent to coordinate-based APIs)
    longitude                    REAL    NOT NULL,
    ground_elevation_m           INTEGER NOT NULL CHECK (ground_elevation_m > 0),  -- village altitude, m a.s.l.
    forecasted_values_source_id  INTEGER NOT NULL DEFAULT 1 REFERENCES sources(source_id),  
    measured_values_source_id    INTEGER NULL REFERENCES sources(source_id),               
    timezone                     TEXT    NOT NULL DEFAULT 'Europe/Paris'                   
);

CREATE TRIGGER trg_locations_source_types_ins BEFORE INSERT ON locations
BEGIN
    SELECT RAISE(ABORT, 'forecasted_values_source_id must reference a forecast source')
    WHERE (SELECT type FROM sources WHERE source_id = NEW.forecasted_values_source_id) IS NOT 'forecast';
    SELECT RAISE(ABORT, 'measured_values_source_id must reference a measured source')
    WHERE NEW.measured_values_source_id IS NOT NULL
      AND (SELECT type FROM sources WHERE source_id = NEW.measured_values_source_id) IS NOT 'measured';
END;
CREATE TRIGGER trg_locations_source_types_upd
BEFORE UPDATE OF forecasted_values_source_id, measured_values_source_id ON locations
BEGIN
    SELECT RAISE(ABORT, 'forecasted_values_source_id must reference a forecast source')
    WHERE (SELECT type FROM sources WHERE source_id = NEW.forecasted_values_source_id) IS NOT 'forecast';
    SELECT RAISE(ABORT, 'measured_values_source_id must reference a measured source')
    WHERE NEW.measured_values_source_id IS NOT NULL
      AND (SELECT type FROM sources WHERE source_id = NEW.measured_values_source_id) IS NOT 'measured';
END;

-- ---------------------------------------------------------------------------
-- 2. SERIES 
--    forecast series: one per requested altitude. Open-Meteo picks a grid cell per altitude.
--    measured series: one per station. elevation_m = station altitude.
-- ---------------------------------------------------------------------------
CREATE TABLE series (
    series_id        INTEGER PRIMARY KEY,
    location_id      INTEGER NOT NULL REFERENCES locations(location_id),
    type             TEXT    NOT NULL CHECK (type IN ('forecast', 'measured')),
    source_id        INTEGER NOT NULL,
    source_point_id  TEXT    NULL,       -- id used to query the source: station id ('74056001'); NULL when queried by lat/lon
    elevation_m      INTEGER NOT NULL CHECK (elevation_m > 0),  -- forecast: requested &elevation=; measured: station altitude
    model            TEXT    NULL,       -- forecast: &models= ('best_match'); measured: NULL
    point_latitude   REAL    NULL,       -- forecast: grid cell centre returned (response.latitude); measured: station position
    point_longitude  REAL    NULL,
    point_elevation_m INTEGER NULL,      -- forecast: model grid-cell height (elevation=nan); measured: = elevation_m. Altitude of precipitation & cloud values
    FOREIGN KEY (source_id, type) REFERENCES sources (source_id, type),   -- series.type must equal sources.type
    CHECK ((type = 'forecast') = (model IS NOT NULL))                     -- model iff forecast
);
-- Uniqueness with NULL-able columns (SQLite treats NULLs as distinct in UNIQUE constraints).
CREATE UNIQUE INDEX ux_series_identity ON series (
    location_id, source_id, elevation_m, COALESCE(model, ''), COALESCE(source_point_id, '')
);

-- ---------------------------------------------------------------------------
-- 2b. DEFAULT_ALTITUDES + V_REQUESTED_ALTITUDES — altitude rule for FORECAST series.
--     Requested altitudes = {ground_elevation_m} ∪ {a ∈ default_altitudes : a - ground_elevation_m >= 100}
-- ---------------------------------------------------------------------------
CREATE TABLE default_altitudes (
    elevation_m INTEGER PRIMARY KEY CHECK (elevation_m > 0)
);
INSERT INTO default_altitudes (elevation_m) VALUES (1000), (1200), (1500), (1800), (2000), (2200);

CREATE VIEW v_requested_altitudes AS
    SELECT location_id, forecasted_values_source_id AS source_id,
           ground_elevation_m AS elevation_m, 1 AS is_ground
    FROM locations
    UNION
    SELECT l.location_id, l.forecasted_values_source_id, d.elevation_m, 0
    FROM locations AS l
    JOIN default_altitudes AS d ON d.elevation_m - l.ground_elevation_m >= 100;

-- ---------------------------------------------------------------------------
-- 3. FETCH_RUNS
-- ---------------------------------------------------------------------------
CREATE TABLE fetch_runs (
    run_id          INTEGER PRIMARY KEY,
    source_id       INTEGER NOT NULL REFERENCES sources(source_id),
    fetched_at_utc  TEXT    NOT NULL,               -- ISO 8601, e.g. '2026-11-15T08:50:00Z'
    endpoint        TEXT    NOT NULL,               -- exact URL called (may differ from sources.api_url path)
    start_date      TEXT    NOT NULL,               -- requested period start (YYYY-MM-DD)
    end_date        TEXT    NOT NULL,
    request_params  TEXT    NOT NULL                -- full query string or JSON, for exact replay
);

-- ---------------------------------------------------------------------------
-- 4. RAW_HOURLY
--    Open-Meteo: values as returned. Other sources: converted to these units/semantics on load;
--    a quantity the source does not provide stays NULL (never imputed here).
--    Time convention (Open-Meteo's): time_utc = valid time.
--      instantaneous fields -> value at time_utc
--      accumulated fields   -> sum over the hour ENDING at time_utc, i.e. (time_utc - 1 h, time_utc]
--    Request Open-Meteo with timezone=GMT; convert station data to the same end-of-interval convention.
-- ---------------------------------------------------------------------------
CREATE TABLE raw_hourly (
    series_id                  INTEGER NOT NULL REFERENCES series(series_id),
    time_utc                   TEXT    NOT NULL,    -- 'YYYY-MM-DDTHH:00Z'
    run_id                     INTEGER NOT NULL REFERENCES fetch_runs(run_id),  -- last run that wrote the row
    temperature_2m_c           REAL,    -- instant, °C (forecast: lapse-rate downscaled to elevation_m)
    precipitation_mm           REAL,    -- accumulated, mm, all phases
    rain_mm                    REAL,    -- accumulated, mm, large-scale liquid       (stations: usually NULL)
    showers_mm                 REAL,    -- accumulated, mm, convective liquid        (stations: usually NULL)
    snowfall_cm                REAL,    -- accumulated, cm of fresh snow             (stations: snow-board/sensor if any)
    freezing_level_height_m    REAL,    -- instant, m a.s.l.                         (stations: NULL)
    cloud_cover_pct            INTEGER, -- instant, % total (stations: oktas × 12.5 if observed, else NULL)
    is_day                     INTEGER CHECK (is_day IN (0, 1)),  -- sun above horizon; computable for any point
    PRIMARY KEY (series_id, time_utc)
) WITHOUT ROWID;

-- ---------------------------------------------------------------------------
-- 5. DAILY_FEATURES
--    (locations.timezone). Two windows, consistent with the time convention above:
--      instantaneous fields: time_local in [d 00:00, d+1 00:00)
--      accumulated fields:   time_local in (d 00:00, d+1 00:00]   (hours ENDING in day d)
--    Aggregates ignore NULLs; a feature whose inputs are all NULL is NULL (e.g. no freezing level at a station).
-- ---------------------------------------------------------------------------
CREATE TABLE daily_features (
    series_id            INTEGER NOT NULL REFERENCES series(series_id),
    date_local           TEXT    NOT NULL,          -- 'YYYY-MM-DD' in locations.timezone
    n_hours              INTEGER NOT NULL,          -- hourly rows in the instantaneous window (expect 23/24/25 with DST)

    -- Air temperature (at series.elevation_m)
    t_min_c              REAL,                      -- MIN(temperature_2m_c)
    t_max_c              REAL,                      -- MAX(temperature_2m_c)
    hours_above_0c       INTEGER,                   -- COUNT(h : temperature_2m_c > 0)
    hours_air_le_0c      INTEGER,                   -- COUNT(h : temperature_2m_c <= 0); sum with hours_above_0c = non-NULL temperature hours

    -- Precipitation
    precip_mm            REAL,                      -- SUM(precipitation_mm)
    rain_mm              REAL,                      -- SUM(rain_mm + showers_mm) = liquid; NULL if the source gives no phase
    snow_we_mm           REAL,                      -- MAX(0, precip_mm - rain_mm) = solid, water equivalent; NULL if rain_mm NULL
    snow_pct             REAL GENERATED ALWAYS AS
                           (CASE WHEN precip_mm > 0 THEN 100.0 * snow_we_mm / precip_mm END) VIRTUAL,  -- NULL on dry days or unknown phase

    -- Freezing level (forecast only; depends on grid cell, not on elevation_m)
    fl_min_m             REAL,                      -- MIN(freezing_level_height_m)
    fl_mean_m            REAL,                      -- AVG(freezing_level_height_m)
    fl_max_m             REAL,                      -- MAX(freezing_level_height_m)
    hours_fl_below_alt   INTEGER,                   -- COUNT(h : freezing_level_height_m < series.elevation_m); NULL if no freezing level

    -- Night sky, attributed to the EVENING date: window [d 12:00, d+1 12:00) local, hours with is_day = 0
    night_hours          INTEGER,                   -- COUNT(night hours with non-NULL cloud_cover_pct)
    night_clear_h        INTEGER,                   -- COUNT(night h : cloud_cover_pct <= 25)       (<= 2 oktas)
    night_partial_h      INTEGER,                   -- COUNT(night h : 25 < cloud_cover_pct < 75)
    night_cloudy_h       INTEGER,                   -- COUNT(night h : cloud_cover_pct >= 75)       (>= 6 oktas)
    night_cloud_mean_pct REAL,                      -- AVG(cloud_cover_pct) over night hours

    computed_at_utc      TEXT    NOT NULL,
    feature_version      INTEGER NOT NULL DEFAULT 1,-- bump when a definition above changes
    PRIMARY KEY (series_id, date_local),
    CHECK (night_clear_h + night_partial_h + night_cloudy_h = night_hours)  -- passes when all NULL
) WITHOUT ROWID;

-- ---------------------------------------------------------------------------
-- 6. V_SERIES_DAILY_21D — daily rows over the last 21 local days available for each series:
--    dates in [date(last_date, '-20 days'), last_date], last_date = MAX(date_local),
--    denormalised with location, source and series metadata: one flat table for CSV export
--    and for the dashboard template (charts + map).
-- ---------------------------------------------------------------------------
CREATE VIEW v_series_daily_21d AS
WITH last AS (
    SELECT series_id, MAX(date_local) AS last_date
    FROM daily_features
    GROUP BY series_id
)
SELECT
    s.series_id,
    s.location_id,
    l.name                 AS location_name,
    l.country,
    l.latitude,
    l.longitude,
    l.ground_elevation_m,
    l.timezone,
    s.type,
    s.source_id,
    src.name               AS source_name,
    s.source_point_id,
    s.model,
    s.elevation_m,
    s.point_latitude,
    s.point_longitude,
    s.point_elevation_m,
    f.date_local,
    f.n_hours,
    f.t_min_c,
    f.t_max_c,
    f.hours_above_0c,
    f.hours_air_le_0c,
    f.precip_mm,
    f.rain_mm,
    f.snow_we_mm,
    f.snow_pct,
    f.fl_min_m,
    f.fl_mean_m,
    f.fl_max_m,
    f.hours_fl_below_alt,
    f.night_hours,
    f.night_clear_h,
    f.night_partial_h,
    f.night_cloudy_h,
    f.night_cloud_mean_pct,
    f.feature_version
FROM daily_features AS f
JOIN last              USING (series_id)
JOIN series   AS s     USING (series_id)
JOIN locations AS l    USING (location_id)
JOIN sources  AS src   ON src.source_id = s.source_id
WHERE f.date_local BETWEEN date(last.last_date, '-20 days') AND last.last_date;

-- ---------------------------------------------------------------------------
-- Seed: 8 locations (all default to Open-Meteo for forecasts; no measured source yet).
--   Coordinates: ids 1-3 are the demo's points; ids 4-8 from the Open-Meteo geocoding API.
--   ground_elevation_m: Open-Meteo geocoding API for all 8
--   (GeoNames ids 3027301, 3178483, 2660191, 2996205, 3017116, 2657918, 2661713, 3028016).
-- ---------------------------------------------------------------------------
INSERT INTO locations (location_id, name, country, latitude, longitude, ground_elevation_m) VALUES
    (1, 'Chamonix',      'FR', 45.9237,  6.8694,  1060),
    (2, 'Cogne',         'IT', 45.6085,  7.3567,  1544),
    (3, 'Kandersteg',    'CH', 46.4947,  7.6731,  1172),
    (4, 'Manigod',       'FR', 45.86127, 6.36868, 1023),
    (5, 'Freissinières', 'FR', 44.75386, 6.53727, 1216),
    (6, 'Zinal',         'CH', 46.13426, 7.62868, 1640),
    (7, 'Arolla',        'CH', 46.0254,  7.48358, 1953),
    (8, 'Ceillac',       'FR', 44.66804, 6.77891, 1658);

-- Forecast series from the altitude rule. Rerun after adding a location or a default altitude
-- (INSERT OR IGNORE + ux_series_identity skip existing series).
INSERT OR IGNORE INTO series (location_id, type, source_id, elevation_m, model)
SELECT location_id, 'forecast', source_id, elevation_m, 'best_match'
FROM v_requested_altitudes
ORDER BY location_id, elevation_m;

-- Point coordinates of every forecast series: grid cell centre returned by the Open-Meteo
-- Historical Forecast API (response.latitude/longitude, best_match) on 2026-11-15 for the
-- requested lat/lon + elevation; point_elevation_m = that cell's height (same API, cell centre, elevation=nan).
-- Series sharing a cell (same coordinates) share all fields except
-- the lapse-rate-adjusted temperature.
WITH k(loc, elev, lat, lon, z) AS (VALUES
    (1, 1060, 45.920174, 6.856599, 1261),  (1, 1200, 45.920174, 6.856599, 1261),
    (1, 1500, 45.911495, 6.874663, 1500),  (1, 1800, 45.918034, 6.8968377, 1866),
    (1, 2000, 45.89375, 6.8727155, 2064),  (1, 2200, 45.89375, 6.8727155, 2064),
    (2, 1544, 45.611526, 7.352649, 1711),  (2, 1800, 45.611526, 7.352649, 1711),
    (2, 2000, 45.605087, 7.3305073, 2074),  (2, 2200, 45.5786, 7.3462486, 2197),
    (3, 1172, 46.513187, 7.671485, 1342),  (3, 1500, 46.480286, 7.664663, 1489),
    (3, 1800, 46.48672, 7.687335, 1727),  (3, 2000, 46.515583, 7.63074, 1978),
    (3, 2200, 46.477875, 7.70537, 2313),  (4, 1023, 45.861107, 6.3604155, 985),
    (4, 1200, 45.867737, 6.3824306, 1218),  (4, 1500, 45.8855, 6.3842406, 1499),
    (4, 1800, 45.8855, 6.3842406, 1499),  (4, 2000, 45.8855, 6.3842406, 1499),
    (4, 2200, 45.8855, 6.3842406, 1499),  (5, 1216, 44.757442, 6.5600863, 1250),
    (5, 1500, 44.75091, 6.5385613, 1452),  (5, 1800, 44.752903, 6.499311, 1787),
    (5, 2000, 44.732975, 6.5367537, 1897),  (5, 2200, 44.726444, 6.5152464, 2268),
    (6, 1640, 46.150528, 7.6267595, 1968),  (6, 1800, 46.150528, 7.6267595, 1968),
    (6, 2000, 46.132847, 7.6245937, 2023),  (6, 2200, 46.124046, 7.642512, 2177),
    (7, 1953, 46.038704, 7.4858985, 2144),  (7, 2200, 46.038704, 7.4858985, 2144),
    (8, 1658, 44.669876, 6.76449, 1843),  (8, 1800, 44.661327, 6.7821255, 1869),
    (8, 2000, 44.66781, 6.8036685, 2054),  (8, 2200, 44.6943, 6.7879105, 2181)
)
UPDATE series SET point_latitude = k.lat, point_longitude = k.lon, point_elevation_m = k.z
FROM k
WHERE series.location_id = k.loc AND series.elevation_m = k.elev
  AND series.source_id = 1 AND series.model = 'best_match';

-- Adding measured data later (example, not executed):
--   INSERT INTO sources (name, type, api_url) VALUES ('Météo-France climatologie', 'measured', 'https://...');
--   UPDATE locations SET measured_values_source_id = <id> WHERE name = 'Chamonix';
--   INSERT INTO series (location_id, type, source_id, source_point_id, elevation_m, point_latitude, point_longitude)
--   VALUES (1, 'measured', <id>, '<station id>', <station altitude>, <lat>, <lon>);