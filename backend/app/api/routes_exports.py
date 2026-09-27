import csv
import datetime
import io
import logging
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..db.database import get_db
from ..models.schema import Company, Event, Watchlist
from ..signals.engine import signal_engine
from .date_range import resolve_window
from .routes_signals import _build_lead_list, _split_csv

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/exports", tags=["Exports"])


def _csv_response(rows: List[dict], columns: List[str], basename: str,
                  start_dt: Optional[datetime.datetime], end_dt: datetime.datetime) -> StreamingResponse:
    """Serialize rows to CSV with the exported range stamped into the filename."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    buf.seek(0)

    span = f"{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}" if start_dt else f"all_time_to_{end_dt.date().isoformat()}"
    filename = f"{basename}_{span}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


SIGNAL_COLUMNS = [
    "company", "ticker", "sector", "industry", "initiative", "category",
    "intent_score", "timing_window", "timing_estimate", "stated_spend", "stated_timing",
    "source_types", "distinct_source_count", "event_count", "it_offering",
    "first_seen", "last_seen", "top_citation_url",
]


def _signal_rows(db: Session, signals: List[dict]) -> List[dict]:
    citation_ids = [s["top_event_ids"][0] for s in signals if s.get("top_event_ids")]
    urls = {}
    if citation_ids:
        for e in db.query(Event.id, Event.source_url).filter(Event.id.in_(citation_ids)).all():
            urls[e[0]] = e[1]
    rows = []
    for s in signals:
        top_id = s["top_event_ids"][0] if s.get("top_event_ids") else None
        rows.append({
            "company": s["company_name"],
            "ticker": s["company_ticker"],
            "sector": s["company_sector"],
            "industry": s["company_industry"],
            "initiative": s["initiative_name"],
            "category": s["category_name"],
            "intent_score": s["intent_score"],
            "timing_window": s["timing_window"],
            "timing_estimate": s["timing_estimate"],
            "stated_spend": s["stated_spend"],
            "stated_timing": s["stated_timing"],
            "source_types": "; ".join(s["source_types"] or []),
            "distinct_source_count": s["distinct_source_count"],
            "event_count": s["event_count"],
            "it_offering": s["it_offering"],
            "first_seen": (s["first_seen_at"] or "")[:10],
            "last_seen": (s["last_seen_at"] or "")[:10],
            "top_citation_url": urls.get(top_id, ""),
        })
    return rows


@router.get("/signals.csv")
def export_signals_csv(
    start: Optional[str] = None,
    end: Optional[str] = None,
    sector: Optional[str] = None,
    category_id: Optional[str] = None,
    initiative_id: Optional[str] = None,
    min_score: int = Query(0, ge=0, le=100),
    db: Session = Depends(get_db),
):
    """Ranked signals for the selected window — what you see is what you export."""
    start_dt, end_dt = resolve_window(start, end)
    signals = signal_engine.compute_window(
        db, start_dt, end_dt,
        sector=sector if sector and sector.lower() != "all" else None,
        category_id=category_id if category_id and category_id.lower() != "all" else None,
        initiative_id=initiative_id if initiative_id and initiative_id.lower() != "all" else None,
    )
    signals = [s for s in signals if s["intent_score"] >= min_score]
    return _csv_response(_signal_rows(db, signals), SIGNAL_COLUMNS, "signals", start_dt, end_dt)


LEAD_COLUMNS = [
    "rank", "company", "ticker", "sector", "industry", "lead_score",
    "matching_signals", "strong_signals", "best_initiative", "best_category",
    "timing_window", "stated_spend", "it_offering", "citation_1", "citation_2",
]


def _lead_rows(result: dict) -> List[dict]:
    rows = []
    for i, lead in enumerate(result.get("leads", []), 1):
        c = lead["company"]
        best = lead["best_signal"]
        cites = [x.get("source_url", "") for x in lead.get("best_signal_citations", [])][:2]
        rows.append({
            "rank": i,
            "company": c.get("name"),
            "ticker": c.get("ticker"),
            "sector": c.get("sector"),
            "industry": c.get("industry"),
            "lead_score": lead["lead_score"],
            "matching_signals": lead["matching_signal_count"],
            "strong_signals": lead["strong_signal_count"],
            "best_initiative": best["initiative_name"],
            "best_category": best["category_name"],
            "timing_window": best["timing_window"],
            "stated_spend": best["stated_spend"],
            "it_offering": best["it_offering"],
            "citation_1": cites[0] if cites else "",
            "citation_2": cites[1] if len(cites) > 1 else "",
        })
    return rows


@router.get("/leads.csv")
def export_leads_csv(
    start: Optional[str] = None,
    end: Optional[str] = None,
    sectors: Optional[str] = None,
    initiative_ids: Optional[str] = None,
    min_score: int = Query(6, ge=0, le=100),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """The ICP lead list — the artifact a salesperson actually works from."""
    start_dt, end_dt = resolve_window(start, end)
    signals = signal_engine.compute_window(db, start_dt, end_dt)
    result = _build_lead_list(db, signals, None, _split_csv(sectors), _split_csv(initiative_ids), min_score, limit)
    return _csv_response(_lead_rows(result), LEAD_COLUMNS, "leads", start_dt, end_dt)


EVENT_COLUMNS = [
    "occurred_at", "company", "ticker", "sector", "initiative", "category",
    "source_type", "confidence", "spend_amount", "timing_horizon", "title", "quote", "source_url",
]


@router.get("/events.csv")
def export_events_csv(
    start: Optional[str] = None,
    end: Optional[str] = None,
    sector: Optional[str] = None,
    category_id: Optional[str] = None,
    source_type: Optional[str] = None,
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(5000, ge=1, le=20000),
    db: Session = Depends(get_db),
):
    start_dt, end_dt = resolve_window(start, end)
    query = db.query(Event).options(joinedload(Event.company)).filter(Event.occurred_at <= end_dt)
    if start_dt is not None:
        query = query.filter(Event.occurred_at >= start_dt)
    if sector and sector.lower() != "all":
        query = query.join(Company).filter(Company.sector == sector)
    if category_id and category_id.lower() != "all":
        query = query.filter(Event.category_id == category_id)
    if source_type and source_type.lower() != "all":
        query = query.filter(Event.source_type == source_type)
    if min_confidence > 0:
        query = query.filter(Event.confidence >= min_confidence)

    events = query.order_by(Event.occurred_at.desc()).limit(limit).all()
    rows = [{
        "occurred_at": e.occurred_at.isoformat() if e.occurred_at else "",
        "company": e.company.name if e.company else "",
        "ticker": e.company.ticker if e.company else "",
        "sector": e.company.sector if e.company else "",
        "initiative": e.initiative_name,
        "category": e.category_name,
        "source_type": e.source_type,
        "confidence": round(e.confidence or 0, 2),
        "spend_amount": e.spend_amount or "",
        "timing_horizon": e.timing_horizon or "",
        "title": e.title,
        "quote": e.quote_text,
        "source_url": e.source_url,
    } for e in events]
    return _csv_response(rows, EVENT_COLUMNS, "events", start_dt, end_dt)


@router.get("/crm-status")
def crm_status():
    """Lets the UI hide the 'Push to CRM' action when no webhook is set up."""
    return {"configured": bool(settings.CRM_WEBHOOK_URL)}


@router.post("/crm-webhook")
def push_leads_to_crm(
    start: Optional[str] = None,
    end: Optional[str] = None,
    sectors: Optional[str] = None,
    initiative_ids: Optional[str] = None,
    min_score: int = Query(6, ge=0, le=100),
    limit: int = Query(50, ge=1, le=500),
    watchlist_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    POST the ranked lead list as a JSON batch to CRM_WEBHOOK_URL. Deliberately
    generic (no vendor SDK) so it works with a HubSpot workflow, Zapier or Make
    webhook. Delivery failures are returned to the caller, not raised.
    """
    if not settings.CRM_WEBHOOK_URL:
        return {
            "status": "not_configured",
            "message": "Set CRM_WEBHOOK_URL in .env to push leads to your CRM "
                       "(any HubSpot workflow / Zapier / Make inbound webhook URL works).",
        }

    start_dt, end_dt = resolve_window(start, end)
    signals = signal_engine.compute_window(db, start_dt, end_dt)

    company_ids = None
    sector_list = _split_csv(sectors)
    initiative_list = _split_csv(initiative_ids)
    if watchlist_id:
        wl = db.query(Watchlist).filter(Watchlist.id == watchlist_id).first()
        if wl:
            company_ids = set(wl.company_ids) if wl.company_ids else None
            sector_list = [] if wl.company_ids else (wl.icp_sectors or [])
            initiative_list = wl.icp_initiative_ids or []

    result = _build_lead_list(db, signals, company_ids, sector_list, initiative_list, min_score, limit)
    payload = {
        "source": "Keenr.ai",
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "window": {"start": start_dt.isoformat() if start_dt else None, "end": end_dt.isoformat()},
        "criteria": {"sectors": sector_list, "initiative_ids": initiative_list, "min_score": min_score},
        "lead_count": len(result["leads"]),
        "leads": _lead_rows(result),
    }

    try:
        resp = httpx.post(settings.CRM_WEBHOOK_URL, json=payload, timeout=15.0)
        ok = resp.status_code < 300
        return {
            "status": "delivered" if ok else "failed",
            "http_status": resp.status_code,
            "lead_count": payload["lead_count"],
            "message": "Leads delivered to CRM webhook." if ok else f"Webhook responded {resp.status_code}: {resp.text[:200]}",
        }
    except Exception as e:
        logger.warning(f"CRM webhook delivery failed: {e}")
        return {"status": "failed", "lead_count": payload["lead_count"], "message": f"Delivery failed: {e}"}
