# Project goal

This project collects historical weather data (historical forecasts and historical measured values) for different locations in the alps, in order to offer a unified historical view of weather conditions infuencing the possibility of ice climbing in these different areas. Values related to temperature, precipitations and night sky cover are collected.

The main and default source for historical forecast is open meteo historical forecast API: https://historical-forecast-api.open-meteo.com/v1/forecast

Measured values will be added later.

This project contains only the backend, a CSV copy of the v_series_daily_21d view is pushed to another repo containing the frontend.

# Forecasts for the next 7 days

After the historical update, `scripts/update_ice_weather_db.py` fetches the next 7 days from the Open-Meteo live forecast API (source 2, https://api.open-meteo.com/v1/forecast) for every forecast series. They are stored in `forecast_hourly` and `forecast_daily`, separate from `raw_hourly` and `daily_features`. Only the latest forecast is kept: each run replaces it. If the forecast request fails, the run only prints a warning and keeps the previous forecast.

`v_series_daily_21d` (and therefore its CSV copy) contains both: `is_prediction = 0` for the last 21 days of history, `is_prediction = 1` for the forecast days after the last historical date of each series.
