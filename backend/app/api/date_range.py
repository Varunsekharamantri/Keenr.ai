import datetime
from typing import Optional, Tuple

from fastapi import HTTPException

from ..config import settings

# Sentinel a client sends for the "All time" preset.
ALL_TIME = "all"


def resolve_window(start: Optional[str], end: Optional[str]) -> Tuple[Optional[datetime.datetime], datetime.datetime]:
    """
    Resolve the dashboard's shared `start`/`end` query params (ISO YYYY-MM-DD)
    into a datetime range.

    - Neither given -> the default trailing window (SIGNAL_DEFAULT_WINDOW_DAYS).
    - start="all"   -> no lower bound (returns None), i.e. all history.
    - `end` is end-of-day inclusive, so start==end returns that whole day.
    """
    now = datetime.datetime.utcnow()

    if end:
        try:
            end_dt = datetime.datetime.strptime(end, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid 'end' date '{end}', expected YYYY-MM-DD")
        end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    else:
        end_dt = now

    if start and start.lower() == ALL_TIME:
        return None, end_dt

    if start:
        try:
            start_dt = datetime.datetime.strptime(start, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid 'start' date '{start}', expected YYYY-MM-DD")
    else:
        start_dt = now - datetime.timedelta(days=settings.SIGNAL_DEFAULT_WINDOW_DAYS)

    if start_dt > end_dt:
        raise HTTPException(status_code=400, detail="'start' must not be after 'end'")

    return start_dt, end_dt


def describe_window(start_dt: Optional[datetime.datetime], end_dt: datetime.datetime) -> dict:
    """Echoed back on responses so the UI can label what it's showing."""
    return {
        "start": start_dt.isoformat() if start_dt else None,
        "end": end_dt.isoformat(),
        "all_time": start_dt is None,
        "days": (end_dt - start_dt).days if start_dt else None,
    }
