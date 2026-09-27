"""
Small shared state kept in the database: API budgets and the last daily run.

These used to live in JSON files under backend/data. That works on one laptop,
but the daily refresh now runs on GitHub Actions, whose disk starts empty every
morning: the Exa monthly budget reset to zero on every run (so the cap stopped
protecting the free credit), and the app never learned a refresh had happened.
In the database they are shared by every process that uses it - the Actions
run, the local app and a deployed one - so what the morning run writes is what
the dashboard reads.

The old files are imported once (see import_legacy_files), so switching over
carries today's counts across instead of starting them again.
"""
import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from ..models.schema import AppState

logger = logging.getLogger("market_signals.state")
_LOCK = threading.Lock()


def _session():
    from .database import SessionLocal
    return SessionLocal()


def get_state(key: str, default: Any = None) -> Any:
    db = _session()
    try:
        row = db.get(AppState, key)
        return row.value if row is not None else default
    finally:
        db.close()


def set_state(key: str, value: Any) -> None:
    # Round-trip through JSON so datetimes and the like are stored as text.
    clean = json.loads(json.dumps(value, default=str))
    with _LOCK:
        db = _session()
        try:
            db.merge(AppState(key=key, value=clean))
            db.commit()
        finally:
            db.close()


def import_legacy_files(data_dir: Path) -> dict:
    """Copy the pre-database JSON state in, once; existing rows are never overwritten."""
    from ..models.schema import AISummary
    moved = {}
    files = {
        "groq_usage": "groq_usage.json",
        "last_daily_run": "last_daily_run.json",
        "exa_budget:exa_usage": "exa_usage.json",
        "exa_budget:exa_ir_usage": "exa_ir_usage.json",
        "exa_budget:exa_career_usage": "exa_career_usage.json",
        "exa_budget:exa_people_usage": "exa_people_usage.json",
    }
    db = _session()
    try:
        for key, name in files.items():
            path = data_dir / name
            if db.get(AppState, key) is None and path.exists():
                try:
                    db.add(AppState(key=key, value=json.loads(path.read_text(encoding="utf-8"))))
                    moved[key] = name
                except Exception as ex:
                    logger.warning(f"could not import {name}: {ex}")
        cache = data_dir / "ai_summaries.json"
        if cache.exists() and db.query(AISummary).count() == 0:
            try:
                entries = json.loads(cache.read_text(encoding="utf-8"))
                for k, v in entries.items():
                    db.add(AISummary(key=k, kind=v.get("kind"), text=v.get("text", ""),
                                     source=v.get("source", "ai")))
                moved["ai_summaries"] = f"{len(entries)} summaries"
            except Exception as ex:
                logger.warning(f"could not import ai_summaries.json: {ex}")
        db.commit()
    finally:
        db.close()
    if moved:
        logger.info(f"imported file state into the database: {moved}")
    return moved
