"""
Data freshness, and a manual "refresh now".

The header used to show a static "Pipeline Active" label and a developer-facing
ingestion modal. This replaces both with facts: when the data was last
refreshed, when the next scheduled refresh is, and one button that runs exactly
what the 08:00 job runs.
"""
import threading

from fastapi import APIRouter

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
    if last.get("state") == "running" and not (_running["active"] or is_running()):
        last["state"] = "interrupted"
    return {
        "running": _running["active"] or is_running(),
        "last_run": {k: last.get(k) for k in ("date", "state", "ok", "finished_at", "started_at",
                                               "documents", "events", "minutes", "sources", "error")},
        "next_run": sched.get("next_run"),
        "schedule": f"{sched.get('time')} {sched.get('timezone')}",
        "scheduler_enabled": sched.get("enabled"),
    }


@router.post("/refresh")
def refresh_now():
    """Start today's refresh now. Only one run at a time."""
    if not _RUN_LOCK.acquire(blocking=False):
        return {"started": False, "reason": "A refresh is already running."}
    _running["active"] = True
    threading.Thread(target=_run_in_background, daemon=True).start()
    return {"started": True}
