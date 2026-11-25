"""Constants of the outing weather scraper."""
import datetime
import os

# ---------------------------------------------------------------------------
# Open-Meteo sources
# ---------------------------------------------------------------------------
SOURCE_HISTORICAL_FORECAST = "historical_forecast"
SOURCE_ARCHIVE = "archive"

API_URLS = {
    SOURCE_HISTORICAL_FORECAST: "https://historical-forecast-api.open-meteo.com/v1/forecast",
    SOURCE_ARCHIVE: "https://archive-api.open-meteo.com/v1/archive",
}
MODEL = "best_match"

# First request date for which the historical forecast API returns every variable below
# (showers and freezing_level_height start around 2021-03-23 in the Alps). Older outings use
# the archive API (ERA5 / ERA5-Land / IFS, back to 1940).
HF_START = datetime.date(2021, 4, 1)
ARCHIVE_START = datetime.date(1940, 1, 1)

# Open-Meteo hourly variable -> key used by features.py (same set and naming as weather/).
HOURLY_VARS = {
    SOURCE_HISTORICAL_FORECAST: {
        "temperature_2m": "temperature_2m_c",
        "precipitation": "precipitation_mm",
        "rain": "rain_mm",
        "showers": "showers_mm",
        "snowfall": "snowfall_cm",
        "freezing_level_height": "freezing_level_height_m",
        "cloud_cover": "cloud_cover_pct",
        "is_day": "is_day",
    },
    # The archive API has no showers and no freezing level: they stay NULL.
    SOURCE_ARCHIVE: {
        "temperature_2m": "temperature_2m_c",
        "precipitation": "precipitation_mm",
        "rain": "rain_mm",
        "snowfall": "snowfall_cm",
        "cloud_cover": "cloud_cover_pct",
        "is_day": "is_day",
    },
}

# ---------------------------------------------------------------------------
# Window and features
# ---------------------------------------------------------------------------
WINDOW_DAYS = 61            # local days D-60 .. D, D = outing date
MIN_OUTING_AGE_DAYS = 3     # outings more recent than today - 3 days wait for complete data
FEATURE_VERSION = 1         # bump when a feature definition changes (outings are then re-fetched)

# ---------------------------------------------------------------------------
# Rate limiting (free tier: 600/min, 5 000/h, 10 000/day, 300 000/month weighted calls per IP)
# Open-Meteo weighs each request: weight = locations * max(1, days/14) * max(1, variables/10).
# ---------------------------------------------------------------------------
MINUTE_BUDGET = 500
HOUR_BUDGET = 4000
DAILY_BUDGET = int(os.getenv("OPENMETEO_DAILY_BUDGET", "8000"))  # over a rolling 24 h, across runs
HTTP_TIMEOUT_S = 120
MAX_ATTEMPTS = 5            # per request, including the first one
BACKOFF_BASE_S = 2          # 2, 4, 8, 16 s (+ jitter), capped at BACKOFF_MAX_S
BACKOFF_MAX_S = 60
RATE_LIMIT_WAIT_S = 60      # wait after a per-minute 429 without Retry-After header
USER_AGENT = "IceClimbingWatch/1.0"
