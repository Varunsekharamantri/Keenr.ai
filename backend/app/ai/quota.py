"""
A daily token budget for Groq, shared by extraction and tile summaries.

Groq's free tier allows 200,000 tokens per day. A single refresh stores a few
hundred documents, and calling the model once per document costs roughly 1,500
tokens each - so on 25 Sept, 222 documents consumed the entire day's allowance
and every tile summary came back rate-limited with nothing left to spend.

Two rules keep that from happening:

  1. A reserve. Summaries may always draw on the last slice of the budget;
     extraction may not touch it. Seven tiles across three regions cost about
     45,000 tokens, so that is what is held back.
  2. Graceful degradation. When the budget runs out, extraction falls back to
     the keyword matcher and summaries fall back to their computed sentence.
     Nothing breaks; the dashboard is just plainer until the quota resets.

Usage is tracked locally per IST day, because the daily figure only appears in
Groq's response when you have already exceeded it.
"""
import datetime
import json
import logging
import threading
from zoneinfo import ZoneInfo

from ..config import settings

logger = logging.getLogger("market_signals.quota")

TZ = ZoneInfo(getattr(settings, "SCHEDULE_TIMEZONE", "Asia/Kolkata"))
PATH = settings.DATA_DIR / "groq_usage.json"
DAILY_LIMIT = int(getattr(settings, "GROQ_DAILY_TOKEN_LIMIT", 200_000))
SUMMARY_RESERVE = int(getattr(settings, "GROQ_SUMMARY_RESERVE", 45_000))

_LOCK = threading.Lock()


def _today() -> str:
    return datetime.datetime.now(TZ).date().isoformat()


def _read() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if data.get("date") != _today():
        data = {"date": _today(), "used": 0, "calls": 0, "by_purpose": {}}
    return data


def _write(data: dict):
    try:
        PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception as ex:
        logger.debug(f"token usage not written: {ex}")


def status() -> dict:
    with _LOCK:
        d = _read()
    used = d.get("used", 0)
    return {
        "date": d.get("date"),
        "used": used,
        "limit": DAILY_LIMIT,
        "remaining": max(0, DAILY_LIMIT - used),
        "extraction_remaining": max(0, DAILY_LIMIT - SUMMARY_RESERVE - used),
        "summary_reserve": SUMMARY_RESERVE,
        "calls": d.get("calls", 0),
        "by_purpose": d.get("by_purpose", {}),
    }


def can_spend(purpose: str, estimate: int = 2000) -> bool:
    """`purpose` is "summary" (may use the reserve) or anything else (may not)."""
    with _LOCK:
        used = _read().get("used", 0)
    ceiling = DAILY_LIMIT if purpose == "summary" else DAILY_LIMIT - SUMMARY_RESERVE
    ok = used + estimate <= ceiling
    if not ok:
        logger.info(f"daily Groq budget spent for {purpose}: {used}/{ceiling}")
    return ok


def record(tokens: int, purpose: str = "other"):
    if not tokens:
        return
    with _LOCK:
        d = _read()
        d["used"] = d.get("used", 0) + int(tokens)
        d["calls"] = d.get("calls", 0) + 1
        d["by_purpose"][purpose] = d["by_purpose"].get(purpose, 0) + int(tokens)
        _write(d)


def mark_exhausted(reason: str = ""):
    """
    Groq only reports the daily figure once you have exceeded it. When that
    happens, stop trying for the rest of the day rather than making failing
    calls on every page load.
    """
    with _LOCK:
        d = _read()
        d["used"] = DAILY_LIMIT
        d["exhausted_reason"] = reason[:200]
        _write(d)
    logger.info(f"Groq daily budget marked exhausted: {reason[:120]}")
