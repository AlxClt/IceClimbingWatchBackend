"""Entrypoint of the camptocamp scraper: python main.py {init,update}"""
import argparse
import logging
import sys
import os
import time

from c2c.config import read_config
from c2c.scraper import C2CScraper
from c2c.db import create_db, migrate_db
from c2c.logging_config import configure_logging, set_log_context

logger = logging.getLogger(__name__)

if __name__ == "__main__":

    run_id, listener = configure_logging()
    
    try:
        parser = argparse.ArgumentParser(
            prog="c2cScraper",
            description="Scrapes camptocamp.org routes and outings data",
        )
        parser.add_argument("mode", choices=["init", "update"])
        args = parser.parse_args()

        dbstring = os.getenv("DATABASE_URL")

        # Context for all log lines from here on
        set_log_context(component="c2c", mode=args.mode)

        conf = read_config()
        logger.info("app.start", extra={"run_id": run_id, "mode": args.mode})

        start_time = time.time()

        # Pending schema migrations, in both modes (creates the schema on an empty database)
        migrate_db(dbstring)
        logger.info("db.migrate.done")

        if args.mode == "init":
            create_db(dbstring)
            logger.info("db.init.done")

        scraper = C2CScraper(mode=args.mode, dbstring=dbstring, config=conf)

        logger.info("scraper.start")
        message = scraper.run()
        elapsed = time.time() - start_time

        logger.info("scraper.done", extra={"duration_s": round(elapsed, 2)})
        logger.info("scraper.summary", extra={"summary": message})

    except Exception:
        logger.exception("app.crash")
        raise
    finally:
        listener.stop()