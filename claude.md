# Project goal

This project collects historical weather data (historical forecasts and historical measured values) for different locations in the alps, in order to offer a unified historical view of weather conditions infuencing the possibility of ice climbing in these different areas. Values related to temperature, precipitations and night sky cover are collected.

The main and default source for historical forecast is open meteo historical forecast API: https://historical-forecast-api.open-meteo.com/v1/forecast

Measured values will be added later.

This project contains only the backend, a CSV copy of the v_series_daily_21d view is pushed to another repo containing the frontend.

It also contains the camptocamp scraper, which collects routes and outings (condition reports) from camptocamp.org as training data for an ice-conditions model.

# Repository layout

- `weather/`: weather pipeline (standard-library Python + SQLite). `weather/scripts/` holds the scripts, `weather/data/` the schema, the local DB and the CSV exports. Run daily by `.github/workflows/update_ice_weather_db.yml` (`working-directory: weather`).
- `c2c_scraper/`: camptocamp scraper (Python package `c2c`, entrypoint `main.py`, PostgreSQL through `DATABASE_URL`). It is also the Docker build context; the image is built and pushed to Docker Hub by `.github/workflows/c2c_scraper_image.yml`.
- `docs/`: documentation (`c2c_scraper.md`, `c2c_scraper_pipeline.md`).

# Forecasts for the next 7 days

After the historical update, `weather/scripts/update_ice_weather_db.py` fetches the next 7 days from the Open-Meteo live forecast API (source 2, https://api.open-meteo.com/v1/forecast) for every forecast series. They are stored in `forecast_hourly` and `forecast_daily`, separate from `raw_hourly` and `daily_features`. Only the latest forecast is kept: each run replaces it. If the forecast request fails, the run only prints a warning and keeps the previous forecast.

`v_series_daily_21d` (and therefore its CSV copy) contains both: `is_prediction = 0` for the last 21 days of history, `is_prediction = 1` for the forecast days after the last historical date of each series.

# Data storage

`weather/data/ice_weather.db` and `weather/data/exports/` are not tracked in git. The daily workflow downloads the DB from the `data` GitHub release, updates it, and overwrites the release assets (`ice_weather.db` and the CSV exports): https://github.com/AlxClt/IceClimbingWatchBackend/releases/tag/data. On every run, the CSV is also uploaded (overwritten) as an asset of the `data` release of the frontend repo (AlxClt/IceClimbingWatch), then a `data-updated` repository_dispatch triggers the frontend "Deploy Pages" workflow, which downloads it into `data/` and deploys the GitHub Pages site.

# Camptocamp scraper

`c2c_scraper/` scrapes camptocamp.org routes and outings into a PostgreSQL database (separate from the weather SQLite DB). Two modes: `python main.py init` (create the schema + full route scrape) and `python main.py update` (incremental scrape of recent outings, plus a refresh of known outings from the last 7 days). It runs on a server as a Docker image (`<DOCKERHUB_USERNAME>/iceclimbingwatch-c2c_scraper`), published by a manually triggered workflow. The schema is managed by Alembic (`c2c_scraper/c2c/migrations/versions/`), and pending migrations are applied at every start, so any change to `c2c/schema.py` needs a new migration. Behavior: `docs/c2c_scraper.md`. Build and deployment: `docs/c2c_scraper_pipeline.md`.
