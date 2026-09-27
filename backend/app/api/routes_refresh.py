"""
Data freshness, and a manual "refresh now".

The header used to show a static "Pipeline Active" label and a developer-facing
ingestion modal. This replaces both with facts: when the data was last
refreshed, when the next scheduled refresh is, and one button that runs exactly
what the 08:00 job runs.
"""
import datetime
import threading
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from ..config import settings

from ..daily_job import is_running, last_run, run_daily
from ..scheduler import get_scheduler_status

router = APIRouter(prefix="", tags=["Refresh"])

_RUN_LOCK = threading.Lock()
_running = {"active": False}


def _run_in_background():
    try:
        run_daily(force=True)
    finally:
        _running["active"] = False
        _RUN_LOCK.release()


@router.get("/refresh/status")
def refresh_status():
    sched = get_scheduler_status()
    last = dict(last_run())
    running = _running["active"] or is_running()
    if last.get("state") == "running" and not running:
        # Not running in this process - but the daily refresh now runs on
        # GitHub Actions, writing "running" to the shared database. Treat it as
        # live unless it started so long ago that it must have died.
        try:
            started = datetime.datetime.fromisoformat(last.get("started_at"))
            age = datetime.datetime.now(started.tzinfo) - started
        except Exception:
            age = datetime.timedelta(days=1)
        if age < datetime.timedelta(hours=5):
            running = True
        else:
            last["state"] = "interrupted"
    if not sched.get("next_run") and settings.PUBLIC_MODE:
        sched = {**sched, **_actions_schedule()}
    return {
        "public_mode": settings.PUBLIC_MODE,
        "running": running,
        "last_run": {k: last.get(k) for k in ("date", "state", "ok", "finished_at", "started_at",
                                               "documents", "events", "minutes", "sources", "error")},
        "next_run": sched.get("next_run"),
        "schedule": f"{sched.get('time')} {sched.get('timezone')}",
        "scheduler_enabled": sched.get("enabled"),
    }


def _actions_schedule() -> dict:
    """The GitHub Actions run: daily at 08:00 IST (cron 30 2 * * * UTC)."""
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime.datetime.now(tz)
    nxt = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += datetime.timedelta(days=1)
    return {"next_run": nxt.isoformat(), "time": "08:00", "timezone": "Asia/Kolkata"}


@router.post("/refresh")
def refresh_now():
    """Start today's refresh now. Only one run at a time."""
    if not _RUN_LOCK.acquire(blocking=False):
        return {"started": False, "reason": "A refresh is already running."}
    _running["active"] = True
    threading.Thread(target=_run_in_background, daemon=True).start()
    return {"started": True}
