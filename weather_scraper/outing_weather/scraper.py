"""Fetch the weather of the 2 months before each ice-climbing outing and store its daily features."""
from __future__ import annotations

import datetime
import logging
import time
from collections import Counter
from datetime import timedelta

from sqlalchemy import Engine

from outing_weather.config import (
    ARCHIVE_START,
    DAILY_BUDGET,
    FEATURE_VERSION,
    HF_START,
    HOURLY_VARS,
    MIN_OUTING_AGE_DAYS,
    MODEL,
    SOURCE_ARCHIVE,
    SOURCE_HISTORICAL_FORECAST,
    WINDOW_DAYS,
)
from outing_weather.db import (
    PendingOuting,
    fill_route_altitudes,
    load_pending_outings,
    used_weight_last_24h,
    write_outing,
)
from outing_weather.features import outing_daily_rows
from outing_weather.openmeteo import ApiError, RateLimitReached, fetch_hourly, request_weight

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 10   # stop the run: something is wrong beyond a single outing
PROGRESS_EVERY = 50


class BudgetExhausted(Exception):
    """The next request would exceed the daily Open-Meteo budget."""


def request_period(outing_date: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Local dates to request for an outing: the first day's night starts at 12:00 the day before
    it, and the outing day's last accumulated hour ends at 00:00 the day after."""
    return outing_date - timedelta(days=WINDOW_DAYS), outing_date + timedelta(days=1)


def pick_source(start: datetime.date) -> str | None:
    if start < ARCHIVE_START:
        return None
    return SOURCE_HISTORICAL_FORECAST if start >= HF_START else SOURCE_ARCHIVE


class OutingWeatherScraper:
    def __init__(
        self,
        engine: Engine,
        daily_budget: float = DAILY_BUDGET,
        limit: int | None = None,
        dry_run: bool = False,
        recompute_altitudes: bool = False,
    ) -> None:
        self.engine = engine
        self.daily_budget = daily_budget
        self.limit = limit
        self.dry_run = dry_run
        self.recompute_altitudes = recompute_altitudes
        self.budget_left = 0.0
        self.spent = 0.0

    # -----------------------
    # One outing
    # -----------------------
    @staticmethod
    def _fetch_row(o: PendingOuting, status: str, **values) -> dict:
        row = {
            "outing_id": o.outing_id,
            "outing_date": o.outing_date,
            "route_id": o.route_id,
            "latitude": o.latitude,
            "longitude": o.longitude,
            "elevation_m": o.altitude_m,
            "grid_latitude": None,
            "grid_longitude": None,
            "grid_elevation_m": None,
            "timezone": None,
            "source": None,
            "model": None,
            "start_date": None,
            "end_date": None,
            "request_url": None,
            "status": status,
            "n_days": 0,
            "api_weight": 0.0,
            "fetched_at": datetime.datetime.now(datetime.timezone.utc),
            "feature_version": FEATURE_VERSION,
        }
        row.update(values)
        return row

    def _spend(self, weight: float) -> None:
        if self.spent + weight > self.budget_left:
            raise BudgetExhausted
        self.spent += weight

    def process_outing(self, o: PendingOuting) -> tuple[dict, list[dict]]:
        """Fetch the weather of one outing. Returns (outing_weather_fetch row, daily rows)."""
        if o.latitude is None or o.longitude is None:
            return self._fetch_row(o, "no_location"), []

        start, end = request_period(o.outing_date)
        source = pick_source(start)
        if source is None:
            return self._fetch_row(o, "out_of_range"), []

        # Historical forecast first; the archive API if the outing day is not covered.
        sources = [source] + ([SOURCE_ARCHIVE] if source == SOURCE_HISTORICAL_FORECAST else [])
        best, total_weight = None, 0.0
        for src in sources:
            self._spend(request_weight(src, start, end))
            url, data, weight = fetch_hourly(src, o.latitude, o.longitude, o.altitude_m, start, end)
            total_weight += weight
            rows = outing_daily_rows(data, HOURLY_VARS[src], o.outing_date, WINDOW_DAYS, data.get("elevation"))
            if best is None or len(rows) > len(best[3]):
                best = (src, url, data, rows)
            if rows and rows[-1]["days_before_outing"] == 0:
                break
            logger.info(
                "weather.outing.source_incomplete",
                extra={"outing_id": o.outing_id, "source": src, "n_days": len(rows)},
            )

        src, url, data, rows = best
        status = "ok" if len(rows) == WINDOW_DAYS else "partial"
        fetch_row = self._fetch_row(
            o,
            status,
            grid_latitude=data.get("latitude"),
            grid_longitude=data.get("longitude"),
            grid_elevation_m=data.get("elevation"),
            timezone=data["timezone"],
            source=src,
            model=MODEL,
            start_date=start,
            end_date=end,
            request_url=url,
            n_days=len(rows),
            api_weight=total_weight,
        )
        return fetch_row, rows

    # -----------------------
    # Run
    # -----------------------
    def run(self) -> dict:
        t0 = time.time()
        if not self.dry_run:
            n_alt = fill_route_altitudes(self.engine, recompute=self.recompute_altitudes)
            logger.info("weather.altitudes.filled", extra={"routes": n_alt, "recompute": self.recompute_altitudes})

        max_date = datetime.date.today() - timedelta(days=MIN_OUTING_AGE_DAYS)
        pending = load_pending_outings(self.engine, max_date, self.limit)
        used = used_weight_last_24h(self.engine)
        self.budget_left = max(0.0, self.daily_budget - used)
        logger.info(
            "weather.pending",
            extra={
                "pending": len(pending),
                "max_outing_date": max_date.isoformat(),
                "daily_budget": self.daily_budget,
                "used_last_24h": round(used, 1),
                "budget_left": round(self.budget_left, 1),
            },
        )

        if self.dry_run:
            estimate = 0.0
            for o in pending:
                start, end = request_period(o.outing_date)
                source = pick_source(start)
                if source is not None and o.latitude is not None and o.longitude is not None:
                    estimate += request_weight(source, start, end)
            summary = {"pending": len(pending), "estimated_weight": round(estimate, 1),
                       "estimated_runs": round(estimate / self.daily_budget, 1) if self.daily_budget else None}
            logger.info("weather.dry_run", extra=summary)
            return summary

        counts: Counter = Counter()
        consecutive_errors = 0
        stopped = None
        for i, o in enumerate(pending, start=1):
            try:
                fetch_row, rows = self.process_outing(o)
            except BudgetExhausted:
                stopped = "daily_budget"
                break
            except RateLimitReached as e:
                logger.warning("weather.rate_limit_reached", extra={"error": str(e)})
                stopped = "openmeteo_rate_limit"
                break
            except ApiError as e:
                counts["error"] += 1
                consecutive_errors += 1
                logger.error("weather.outing.error", extra={"outing_id": o.outing_id, "error": str(e)})
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    raise RuntimeError(f"{consecutive_errors} consecutive Open-Meteo errors, last: {e}") from e
                continue

            consecutive_errors = 0
            write_outing(self.engine, fetch_row, rows)
            counts[fetch_row["status"]] += 1
            if fetch_row["source"]:
                counts[fetch_row["source"]] += 1
            logger.debug(
                "weather.outing.done",
                extra={"outing_id": o.outing_id, "status": fetch_row["status"], "source": fetch_row["source"],
                       "n_days": fetch_row["n_days"]},
            )
            if i % PROGRESS_EVERY == 0:
                logger.info(
                    "weather.progress",
                    extra={"done": i, "pending": len(pending), "weight_spent": round(self.spent, 1),
                           "elapsed_s": round(time.time() - t0)},
                )

        written = sum(counts[s] for s in ("ok", "partial", "no_location", "out_of_range"))
        summary = {
            "pending": len(pending),
            "written": written,
            "left": len(pending) - written - counts["error"],
            "ok": counts["ok"],
            "partial": counts["partial"],
            "no_location": counts["no_location"],
            "out_of_range": counts["out_of_range"],
            "errors": counts["error"],
            "historical_forecast": counts[SOURCE_HISTORICAL_FORECAST],
            "archive": counts[SOURCE_ARCHIVE],
            "weight_spent": round(self.spent, 1),
            "stopped": stopped,
            "duration_s": round(time.time() - t0, 1),
        }
        if stopped:
            logger.warning("weather.stopped", extra={"reason": stopped, "left": summary["left"]})
        return summary
