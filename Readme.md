# Ice climing watch - backend

Data collection pipeline for weather conditions influencing the possibility of ice climbing in the Alps

Serving https://alxclt.github.io/IceClimbingWatch/

Work in progress: a machine learning model for ice climbing conditions forecast

## Repository layout

| Folder | What it does |
|---|---|
| [weather/](weather/) | Daily weather pipeline (Open-Meteo historical + 7-day forecast, SQLite), run by [update_ice_weather_db.yml](.github/workflows/update_ice_weather_db.yml). Its CSV export feeds the frontend. |
| [c2c_scraper/](c2c_scraper/) | Camptocamp scraper: routes and outings (condition reports) from camptocamp.org into PostgreSQL, as future training data for an ice-conditions model. Shipped as a Docker image by [c2c_scraper_image.yml](.github/workflows/c2c_scraper_image.yml), runs on a separate server. |
| [docs/](docs/) | [Camptocamp scraper](docs/c2c_scraper.md), [its CI/CD pipeline](docs/c2c_scraper_pipeline.md) |
