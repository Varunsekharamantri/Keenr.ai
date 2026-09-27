from collections import defaultdict
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from ..db.database import get_db
from ..models.schema import (
    Alert, Company, Event, Signal, Watchlist,
    WatchlistCreate, WatchlistUpdate, RecomputeRequest,
)
from ..signals.engine import signal_engine
from ..signals.alerts import initial_alerts_for_watchlist
from .date_range import resolve_window, describe_window

router = APIRouter(tags=["Signals, Watchlists & Alerts"])


def _split_csv(value: Optional[str]) -> List[str]:
    if not value or value.lower() == "all":
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _citations_for_ids(db: Session, ids: List[str]) -> List[dict]:
    if not ids:
        return []
    events = db.query(Event).options(joinedload(Event.company)).filter(Event.id.in_(ids)).all()
    by_id = {e.id: e for e in events}
    return [by_id[i].to_dict() for i in ids if i in by_id]


def _build_lead_list(
    db: Session,
    signals: List[dict],
    company_ids: Optional[set],
    sectors: List[str],
    initiative_ids: List[str],
    min_score: int,
    limit: int,
) -> dict:
    """Rank companies by their best matching signal; the exit-criterion
    'ranked lead list with citations'. Operates on windowed signal dicts."""
    matching = [
        s for s in signals
        if s["intent_score"] >= min_score
        and (company_ids is None or s["company_id"] in company_ids)
        and (not sectors or s["company_sector"] in sectors)
        and (not initiative_ids or s["initiative_id"] in initiative_ids)
    ]

    grouped: Dict[str, List[dict]] = defaultdict(list)
    for s in matching:
        grouped[s["company_id"]].append(s)

    company_rows = {c.id: c for c in db.query(Company).filter(Company.id.in_(grouped.keys())).all()} if grouped else {}

    leads = []
    for cid, sigs in grouped.items():
        sigs.sort(key=lambda s: s["intent_score"], reverse=True)
        best = sigs[0]
        company = company_rows.get(cid)
        leads.append({
            "company": company.to_dict() if company else {"id": cid, "name": best["company_name"]},
            "lead_score": best["intent_score"],
            "matching_signal_count": len(sigs),
            "strong_signal_count": sum(1 for s in sigs if s["intent_score"] >= 50),
            "best_signal": best,
            "best_signal_citations": _citations_for_ids(db, best["top_event_ids"]),
            "matching_signals": sigs,
        })
    leads.sort(key=lambda l: (l["lead_score"], l["strong_signal_count"]), reverse=True)
    return {"leads": leads[:limit], "total_matching_signals": len(matching), "total_companies": len(leads)}


# ---------------------------------------------------------------- signals

@router.get("/signals")
def list_signals(
    start: Optional[str] = None,
    end: Optional[str] = None,
    sector: Optional[str] = None,
    industry: Optional[str] = None,
    region: Optional[str] = None,
    country: Optional[str] = None,
    category_id: Optional[str] = None,
    initiative_id: Optional[str] = None,
    company_id: Optional[str] = None,
    ticker: Optional[str] = None,
    min_score: int = Query(0, ge=0, le=100),
    search: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    start_dt, end_dt = resolve_window(start, end)
    signals = signal_engine.compute_window(
        db, start_dt, end_dt,
        sector=sector if sector and sector.lower() != "all" else None,
        industry=industry,
        region=region if region and region.lower() != "all" else None,
        country=country,
        category_id=category_id if category_id and category_id.lower() != "all" else None,
        initiative_id=initiative_id if initiative_id and initiative_id.lower() != "all" else None,
        company_id=company_id,
        ticker=ticker,
    )
    if min_score > 0:
        signals = [s for s in signals if s["intent_score"] >= min_score]
    if search:
        term = search.strip().lower()
        signals = [
            s for s in signals
            if term in (s["company_name"] or "").lower()
            or term in (s["initiative_name"] or "").lower()
            or term in (s["it_offering"] or "").lower()
        ]

    return {
        "total": len(signals),
        "signals": signals[offset:offset + limit],
        "window": describe_window(start_dt, end_dt),
    }


@router.get("/signals/leads")
def icp_lead_list(
    start: Optional[str] = None,
    end: Optional[str] = None,
    sectors: Optional[str] = None,
    initiative_ids: Optional[str] = None,
    min_score: int = Query(6, ge=0, le=100),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    start_dt, end_dt = resolve_window(start, end)
    signals = signal_engine.compute_window(db, start_dt, end_dt)
    result = _build_lead_list(db, signals, None, _split_csv(sectors), _split_csv(initiative_ids), min_score, limit)
    result["window"] = describe_window(start_dt, end_dt)
    return result


@router.post("/signals/recompute")
def recompute_signals(payload: RecomputeRequest = RecomputeRequest(), db: Session = Depends(get_db)):
    sector = payload.sector if payload.sector and payload.sector.lower() != "all" else None
    return signal_engine.recompute(db, sector_filter=sector)


@router.get("/signals/{signal_id}")
def get_signal(
    signal_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Accepts either a windowed composite id ("{company_id}__{initiative_id}")
    or a stored Signal uuid (e.g. from an alert). Either way the breakdown and
    citations are re-scored over the requested window, so they always match
    the card the user clicked.
    """
    if "__" in signal_id:
        company_id, initiative_id = signal_id.split("__", 1)
    else:
        stored = db.query(Signal).filter(Signal.id == signal_id).first()
        if not stored:
            raise HTTPException(status_code=404, detail="Signal not found")
        company_id, initiative_id = stored.company_id, stored.initiative_id

    start_dt, end_dt = resolve_window(start, end)
    matches = signal_engine.compute_window(
        db, start_dt, end_dt, company_id=company_id, initiative_id=initiative_id
    )
    if not matches:
        raise HTTPException(status_code=404, detail="Signal has no evidence in the selected date range")

    data = matches[0]
    data["citations"] = _citations_for_ids(db, data["top_event_ids"])
    data["contributing_event_count"] = data["event_count"]
    data["window"] = describe_window(start_dt, end_dt)
    return data


# ------------------------------------------------------------- watchlists

@router.get("/watchlists")
def list_watchlists(db: Session = Depends(get_db)):
    rows = db.query(Watchlist).order_by(Watchlist.created_at.desc()).all()
    return [w.to_dict() for w in rows]


@router.post("/watchlists", status_code=201)
def create_watchlist(payload: WatchlistCreate, db: Session = Depends(get_db)):
    wl = Watchlist(**payload.model_dump())
    db.add(wl)
    db.flush()
    alerts_created = initial_alerts_for_watchlist(db, wl)
    db.commit()
    db.refresh(wl)
    data = wl.to_dict()
    data["alerts_created"] = alerts_created
    return data


@router.get("/watchlists/{watchlist_id}")
def get_watchlist(watchlist_id: str, db: Session = Depends(get_db)):
    wl = db.query(Watchlist).filter(Watchlist.id == watchlist_id).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    data = wl.to_dict()
    if wl.company_ids:
        companies = db.query(Company).filter(Company.id.in_(wl.company_ids)).all()
        data["companies"] = [c.to_dict() for c in companies]
    return data


@router.put("/watchlists/{watchlist_id}")
def update_watchlist(watchlist_id: str, payload: WatchlistUpdate, db: Session = Depends(get_db)):
    wl = db.query(Watchlist).filter(Watchlist.id == watchlist_id).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(wl, field, value)
    db.flush()
    # Widening the ICP or lowering the threshold can bring already-hot
    # accounts into scope; alert on those now rather than waiting for a
    # score change that may never come.
    alerts_created = 0
    if {"company_ids", "icp_sectors", "icp_initiative_ids", "alert_threshold"} & set(changes):
        alerts_created = initial_alerts_for_watchlist(db, wl)
    db.commit()
    db.refresh(wl)
    data = wl.to_dict()
    data["alerts_created"] = alerts_created
    return data


@router.delete("/watchlists/{watchlist_id}", status_code=204)
def delete_watchlist(watchlist_id: str, db: Session = Depends(get_db)):
    wl = db.query(Watchlist).filter(Watchlist.id == watchlist_id).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    db.delete(wl)
    db.commit()
    return None


@router.get("/watchlists/{watchlist_id}/leads")
def watchlist_leads(
    watchlist_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    min_score: int = Query(0, ge=0, le=100),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    wl = db.query(Watchlist).filter(Watchlist.id == watchlist_id).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    start_dt, end_dt = resolve_window(start, end)
    company_ids = set(wl.company_ids) if wl.company_ids else None
    sectors = [] if wl.company_ids else (wl.icp_sectors or [])
    if company_ids is None and not sectors:
        return {"watchlist": wl.to_dict(), "leads": [], "total_matching_signals": 0, "total_companies": 0,
                "window": describe_window(start_dt, end_dt)}
    signals = signal_engine.compute_window(db, start_dt, end_dt)
    result = _build_lead_list(db, signals, company_ids, sectors, wl.icp_initiative_ids or [], min_score, limit)
    result["watchlist"] = wl.to_dict()
    result["window"] = describe_window(start_dt, end_dt)
    return result


# ----------------------------------------------------------------- alerts

@router.get("/alerts")
def list_alerts(
    unread_only: bool = False,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(Alert).options(
        joinedload(Alert.company), joinedload(Alert.signal), joinedload(Alert.watchlist)
    )
    if unread_only:
        query = query.filter(Alert.is_read == False)  # noqa: E712
    rows = query.order_by(Alert.created_at.desc()).limit(limit).all()
    return [a.to_dict() for a in rows]


@router.get("/alerts/unread-count")
def unread_alert_count(db: Session = Depends(get_db)):
    return {"unread": db.query(Alert).filter(Alert.is_read == False).count()}  # noqa: E712


@router.post("/alerts/read-all")
def mark_all_alerts_read(db: Session = Depends(get_db)):
    updated = db.query(Alert).filter(Alert.is_read == False).update({"is_read": True})  # noqa: E712
    db.commit()
    return {"marked_read": updated}


@router.post("/alerts/{alert_id}/read")
def mark_alert_read(alert_id: str, db: Session = Depends(get_db)):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_read = True
    db.commit()
    return alert.to_dict()
