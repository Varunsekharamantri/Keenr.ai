from collections import OrderedDict
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_
from ..db.database import get_db
from ..models.schema import Company, Event, RawDocument, CompanyCreate, CompanyOut
from ..signals.engine import signal_engine
from .date_range import resolve_window

router = APIRouter(prefix="/companies", tags=["Companies"])

@router.get("", response_model=List[CompanyOut])
def list_companies(
    start: Optional[str] = None,
    end: Optional[str] = None,
    sector: Optional[str] = Query(None, description="Filter by sector (e.g. BFSI, Healthcare)"),
    region: Optional[str] = Query(None, description="Americas | Rest of World"),
    country: Optional[str] = None,
    search: Optional[str] = Query(None, description="Search by name or ticker"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    start_dt, end_dt = resolve_window(start, end)
    # Date-bound the join, not the WHERE clause, so companies with no events
    # in the window still appear (with events_count 0).
    join_on = [Company.id == Event.company_id, Event.occurred_at <= end_dt]
    if start_dt is not None:
        join_on.append(Event.occurred_at >= start_dt)

    query = db.query(
        Company,
        func.count(Event.id).label("events_count")
    ).outerjoin(Event, and_(*join_on))

    if sector and sector.lower() != "all":
        query = query.filter(Company.sector == sector)

    if region and region.lower() != "all":
        query = query.filter(Company.region == region)

    if country:
        query = query.filter(Company.country == country)

    if search:
        s_term = f"%{search.strip()}%"
        query = query.filter(
            (Company.name.ilike(s_term)) | (Company.ticker.ilike(s_term))
        )

    query = query.group_by(Company.id).order_by(Company.name.asc())
    results = query.offset(offset).limit(limit).all()

    out = []
    for comp, count in results:
        data = comp.to_dict()
        data["events_count"] = count
        out.append(CompanyOut(**data))
    return out

@router.get("/{company_id}")
def get_company_detail(
    company_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: Session = Depends(get_db),
):
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    start_dt, end_dt = resolve_window(start, end)
    events_q = db.query(Event).filter(Event.company_id == company_id, Event.occurred_at <= end_dt)
    if start_dt is not None:
        events_q = events_q.filter(Event.occurred_at >= start_dt)
    events = events_q.order_by(Event.occurred_at.desc()).limit(20).all()
    raw_docs = db.query(RawDocument).filter(RawDocument.company_id == company_id).order_by(RawDocument.filing_date.desc()).limit(10).all()

    return {
        "company": company.to_dict(),
        "recent_events": [e.to_dict() for e in events],
        "raw_documents": [d.to_dict() for d in raw_docs]
    }

@router.get("/{company_id}/timeline")
def get_company_timeline(
    company_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Chronological events grouped by month, plus the company's current
    signals — the per-company story behind its scores."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    start_dt, end_dt = resolve_window(start, end)
    q = db.query(Event).filter(Event.company_id == company_id, Event.occurred_at <= end_dt)
    if start_dt is not None:
        q = q.filter(Event.occurred_at >= start_dt)
    events = q.order_by(Event.occurred_at.desc()).all()

    groups = OrderedDict()
    for e in events:
        key = e.occurred_at.strftime("%Y-%m") if e.occurred_at else "unknown"
        groups.setdefault(key, []).append(e.to_dict())

    signals = signal_engine.compute_window(db, start_dt, end_dt, company_id=company_id)
    return {
        "company": company.to_dict(),
        "event_count": len(events),
        "months": [{"month": k, "events": v} for k, v in groups.items()],
        "signals": signals,
    }


@router.post("", response_model=CompanyOut)
def create_company(payload: CompanyCreate, db: Session = Depends(get_db)):
    ticker = payload.ticker.upper().strip() if payload.ticker else None
    cik = payload.cik.zfill(10) if payload.cik else None

    if ticker or cik:
        conditions = []
        if ticker:
            conditions.append(Company.ticker == ticker)
        if cik:
            conditions.append(Company.cik == cik)
        existing = db.query(Company).filter(or_(*conditions)).first()
        if existing:
            raise HTTPException(status_code=400, detail="Company with this ticker or CIK already exists")

    company = Company(
        name=payload.name,
        ticker=ticker,
        cik=cik,
        industry=payload.industry,
        sector=payload.sector,
        naics=payload.naics,
        aliases=payload.aliases,
        description=payload.description
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    return CompanyOut(**company.to_dict(), events_count=0)
