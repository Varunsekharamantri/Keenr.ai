"""
The 8 AM daily refresh. One function, called by every scheduler that exists
(the app's own APScheduler, Windows Task Scheduler via run_ingest.py, or a cloud
cron later), so they all behave identically.

Order matters:
  1. fetch    - only the sources the cadence says are worth polling today
  2. score    - recompute signals from the new events
  3. images   - fetch preview images for the newly stored articles
  4. tenders  - EU TED, free and quick
  5. warm     - pre-compute the Industry and Opportunity pages, which also writes
                their AI summaries into the cache, so the first person to open the
                dashboard in the morning is not the one waiting on Groq

It runs at most once per calendar day (IST). If the laptop was asleep at 8 AM the
app catches up when it next starts; if two schedulers fire, the second finds the
day already done and exits. A run that crashes does not count, so it is retried.
"""
import datetime
import json
import logging
import time
import traceback
from zoneinfo import ZoneInfo

from .config import settings
from .db.database import SessionLocal
from .ingestion.cadence import in_results_season, plan_for
from .ingestion.pipeline import IngestionPipeline
from .signals.engine import signal_engine
from .ai import summarizer

logger = logging.getLogger("market_signals.daily")

TZ = ZoneInfo(getattr(settings, "SCHEDULE_TIMEZONE", "Asia/Kolkata"))
STATUS_PATH = settings.DATA_DIR / "last_daily_run.json"


# Whether a refresh is live in THIS process. The status file cannot answer that:
# a run killed mid-way (server restart, laptop sleep) leaves "state: running"
# behind forever, which froze the header on "Refreshing..." and disabled the
# refresh button.
_ACTIVE = {"running": False}


def is_running() -> bool:
    return _ACTIVE["running"]


def today_local() -> datetime.date:
    return datetime.datetime.now(TZ).date()


def last_run() -> dict:
    # In the database (app_state), so the app sees a refresh that ran on GitHub
    # Actions - the header's "Updated ..." chip reads this.
    try:
        from .db.state import get_state
        return get_state("last_daily_run", {}) or {}
    except Exception:
        pass
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_status(status: dict):
    try:
        from .db.state import set_state
        set_state("last_daily_run", status)
        return
    except Exception as ex:
        logger.warning(f"daily status not saved to the database: {ex}")
    try:
        STATUS_PATH.write_text(json.dumps(status, indent=1, default=str), encoding="utf-8")
    except Exception as ex:
        logger.warning(f"could not write daily status: {ex}")


def already_ran_today() -> bool:
    s = last_run()
    return s.get("date") == today_local().isoformat() and s.get("ok") is True


def run_daily(force: bool = False, limit_per_company: int = None) -> dict:
    """Run today's refresh. Returns a status dict (also saved to disk)."""
    day = today_local()
    if not force and already_ran_today():
        logger.info(f"Daily refresh already completed for {day}; skipping.")
        return last_run()

    if _ACTIVE["running"]:
        logger.info("A refresh is already running in this process; not starting another.")
        return last_run()
    _ACTIVE["running"] = True

    sources, why = plan_for(day)
    started = datetime.datetime.now(TZ)
    status = {"date": day.isoformat(), "started_at": started.isoformat(), "ok": False,
              "results_season": in_results_season(day), "sources": sorted(sources), "why": why}
    _write_status({**status, "state": "running"})
    logger.info(f"Daily refresh {day}: polling {sorted(sources)}")

    db = SessionLocal()
    try:
        pipeline = IngestionPipeline()
        # Only news from the last NEWS_LOOKBACK_DAYS: the searches are told the
        # window, and results outside it (or with no date) are dropped.
        news_floor = (day - datetime.timedelta(days=settings.NEWS_LOOKBACK_DAYS)).isoformat()
        status["news_since"] = news_floor
        job = pipeline.run_batch_ingestion(
            db=db,
            limit_per_company=limit_per_company or settings.SCHEDULE_LIMIT_PER_COMPANY,
            sector_filter=settings.SCHEDULE_SECTOR_FILTER,
            sources=sources,
            start_published_date=news_floor,
        )
        status["documents"] = job.items_ingested
        status["events"] = job.events_extracted

        status["signals"] = signal_engine.recompute(db, sector_filter=settings.SCHEDULE_SECTOR_FILTER)

        try:
            from .ingestion.thumbnails import fill_thumbnails
            status["thumbnails"] = fill_thumbnails(db, since=datetime.datetime.now() - datetime.timedelta(days=3))
        except Exception as ex:
            status["thumbnails"] = f"skipped: {ex}"

        # People to Tap. Runs after scoring because the scores decide which
        # companies are in scope. A company is re-searched only when a
        # leadership-change event arrived since its last search (this run's
        # ingestion may just have stored one), when it is newly in scope, or
        # when its data passed the TTL - capped per day to protect the budget.
        try:
            from .people.leaders import refresh_due
            status["leaders"] = refresh_due(db, max_calls=settings.LEADERS_DAILY_MAX_CALLS)
        except Exception as ex:
            status["leaders"] = f"skipped: {ex}"

        if getattr(settings, "TED_ENABLED", False):
            try:
                from .api.routes_tenders import ingest_tenders
                status["tenders"] = ingest_tenders(db)
            except Exception as ex:
                status["tenders"] = f"skipped: {ex}"

        # Warm the two summary pages for every region, which caches their AI text.
        warmed = []
        try:
            from .api.routes_insights import insights_overview
            from .api.routes_opportunities import opportunities
            # The warm-up is the one place that writes every tile's summary:
            # it has time to spread the calls under the rate limit.
            original_many = summarizer.summarize_many
            summarizer.summarize_many = lambda jobs, budget=None: original_many(jobs, budget=None)
            regions = (None, "Americas", "Rest of World")
            for i, region in enumerate(regions):
                insights_overview(start=None, end=None, region=region, sector="BFSI", db=db)
                opportunities(start=None, end=None, region=region, sector="BFSI", limit_featured=8, db=db)
                warmed.append(region or "All regions")
                # Five summaries per region at ~1,500 tokens each would blow
                # Groq's 8,000 tokens/minute cap in one burst; a rate-limited
                # tile falls back to plain text and is not cached. Pace it.
                if i < len(regions) - 1:
                    time.sleep(45)
        except Exception as ex:
            logger.warning(f"page warm-up failed: {ex}")
        finally:
            try:
                summarizer.summarize_many = original_many
            except Exception:
                pass
        status["warmed"] = warmed

        status["ok"] = True
    except Exception as ex:
        logger.error(f"Daily refresh failed: {ex}", exc_info=True)
        status["error"] = f"{type(ex).__name__}: {ex}"
        status["trace"] = traceback.format_exc()[-1500:]
    finally:
        db.close()
        _ACTIVE["running"] = False

    status["finished_at"] = datetime.datetime.now(TZ).isoformat()
    status["minutes"] = round((datetime.datetime.now(TZ) - started).total_seconds() / 60, 1)
    status["state"] = "done" if status["ok"] else "failed"
    _write_status(status)
    logger.info(f"Daily refresh {day} {status['state']} in {status['minutes']} min")
    return status
