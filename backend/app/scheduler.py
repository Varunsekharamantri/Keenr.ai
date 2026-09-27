"""
In-app scheduler: the daily refresh at 08:00 IST.

This only fires while the app is running. For a laptop that is often closed, two
safeguards cover the gap:
  * catch-up - on startup, if today's 08:00 run was missed, it runs a minute later;
  * once-a-day guard - app/daily_job.py records a successful day, so this and any
    external scheduler (Windows Task Scheduler, a cloud cron) never double-fetch.

Previously this ran "every 24 hours from whenever the app started", which drifted
with every restart and never landed at a predictable time.
"""
import datetime
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import settings
from .daily_job import TZ, already_ran_today, last_run, run_daily

logger = logging.getLogger("market_signals.scheduler")

_scheduler: BackgroundScheduler | None = None

JOB_ID = "daily_refresh_0800"
CATCHUP_ID = "daily_refresh_catchup"
RUN_HOUR = int(getattr(settings, "SCHEDULE_HOUR", 8))
RUN_MINUTE = int(getattr(settings, "SCHEDULE_MINUTE", 0))


def run_scheduled_ingestion():
    """Kept for callers that still import it (the manual-trigger route)."""
    return run_daily()


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.SCHEDULE_ENABLED:
        logger.info("Scheduler disabled via SCHEDULE_ENABLED=false.")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone=TZ)
    _scheduler.add_job(
        run_daily,
        trigger=CronTrigger(hour=RUN_HOUR, minute=RUN_MINUTE, timezone=TZ),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3 * 3600,   # a late wake-up within 3h still runs
    )

    now = datetime.datetime.now(TZ)
    due_today = now.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    if now >= due_today and not already_ran_today():
        _scheduler.add_job(
            run_daily,
            trigger=DateTrigger(run_date=now + datetime.timedelta(seconds=60), timezone=TZ),
            id=CATCHUP_ID,
            replace_existing=True,
        )
        logger.info("Today's 08:00 refresh was missed; catching up in 60 seconds.")

    _scheduler.start()
    job = _scheduler.get_job(JOB_ID)
    logger.info(f"Daily refresh scheduled at {RUN_HOUR:02d}:{RUN_MINUTE:02d} {TZ.key}; "
                f"next run {job.next_run_time.isoformat() if job else 'unknown'}")
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped.")


def get_scheduler_status() -> dict:
    status = {
        "enabled": settings.SCHEDULE_ENABLED,
        "running": bool(_scheduler and _scheduler.running),
        "time": f"{RUN_HOUR:02d}:{RUN_MINUTE:02d}",
        "timezone": TZ.key,
        "sector_filter": settings.SCHEDULE_SECTOR_FILTER or "all",
        "next_run": None,
        "last_run": last_run(),
    }
    if _scheduler is not None:
        job = _scheduler.get_job(JOB_ID)
        if job and job.next_run_time:
            status["next_run"] = job.next_run_time.isoformat()
    return status
