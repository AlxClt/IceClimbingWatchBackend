"""Entrypoint of the outing weather scraper: python main.py update [--limit N] [--dry-run] [--recompute-altitudes]

Runs after the c2c scraper, on the same PostgreSQL database (DATABASE_URL).
"""
import argparse
import logging
import os
import sys

from outing_weather.config import DAILY_BUDGET
from outing_weather.db import get_engine, migrate_db
from outing_weather.logging_config import configure_logging, set_log_context
from outing_weather.scraper import OutingWeatherScraper

logger = logging.getLogger(__name__)


def main() -> None:
    run_id, listener = configure_logging()
    try:
        parser = argparse.ArgumentParser(
            prog="weatherScraper",
            description="Fetches Open-Meteo weather of the 2 months before each ice-climbing outing",
        )
        parser.add_argument("mode", choices=["update"])
        parser.add_argument("--limit", type=int, default=None, help="process at most N outings")
        parser.add_argument("--daily-budget", type=float, default=DAILY_BUDGET,
                            help=f"Open-Meteo weighted calls allowed over a rolling 24 h (default {DAILY_BUDGET})")
        parser.add_argument("--dry-run", action="store_true",
                            help="only count the pending outings and estimate the API weight (migrations still run)")
        parser.add_argument("--recompute-altitudes", action="store_true",
                            help="recompute the weather altitude of every ice-climbing route; outings whose "
                                 "altitude changes are fetched again")
        args = parser.parse_args()

        dbstring = os.getenv("DATABASE_URL")
        if not dbstring:
            raise RuntimeError("DATABASE_URL is not set")

        set_log_context(component="weather", mode=args.mode)
        logger.info("app.start", extra={"run_id": run_id, "mode": args.mode, "limit": args.limit,
                                        "dry_run": args.dry_run})

        engine = get_engine(dbstring)
        migrate_db(engine)
        logger.info("db.migrate.done")

        scraper = OutingWeatherScraper(
            engine,
            daily_budget=args.daily_budget,
            limit=args.limit,
            dry_run=args.dry_run,
            recompute_altitudes=args.recompute_altitudes,
        )
        summary = scraper.run()
        logger.info("weather.summary", extra={"summary": summary})

    except Exception:
        logger.exception("app.crash")
        listener.stop()
        sys.exit(1)
    listener.stop()


if __name__ == "__main__":
    main()
