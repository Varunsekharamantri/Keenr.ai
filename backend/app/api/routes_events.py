from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from ..db.database import get_db
from ..models.schema import Event, Company, EventOut
from .date_range import resolve_window

router = APIRouter(prefix="/events", tags=["Events & Signals"])

@router.get("", response_model=List[EventOut])
def list_events(
    start: Optional[str] = None,
    end: Optional[str] = None,
    company_id: Optional[str] = None,
    ticker: Optional[str] = None,
    sector: Optional[str] = None,
    region: Optional[str] = None,
    initiative_id: Optional[str] = None,
    category_id: Optional[str] = None,
    source_type: Optional[str] = None,
    min_confidence: Optional[float] = Query(0.0, ge=0.0, le=1.0),
    search: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    query = db.query(Event).options(joinedload(Event.company), joinedload(Event.raw_document))

    start_dt, end_dt = resolve_window(start, end)
    query = query.filter(Event.occurred_at <= end_dt)
    if start_dt is not None:
        query = query.filter(Event.occurred_at >= start_dt)

    if company_id:
        query = query.filter(Event.company_id == company_id)

    if ticker:
        query = query.join(Company).filter(Company.ticker == ticker.upper())

    needs_company_join = (sector and sector.lower() != "all") or (region and region.lower() != "all")
    if needs_company_join and not ticker:  # prevent double join
        query = query.join(Company)
    if sector and sector.lower() != "all":
        query = query.filter(Company.sector == sector)
    if region and region.lower() != "all":
        query = query.filter(Company.region == region)

    if initiative_id and initiative_id.lower() != "all":
        query = query.filter(Event.initiative_id == initiative_id)

    if category_id and category_id.lower() != "all":
        query = query.filter(Event.category_id == category_id)

    if source_type and source_type.lower() != "all":
        query = query.filter(Event.source_type == source_type)

    if min_confidence and min_confidence > 0:
        query = query.filter(Event.confidence >= min_confidence)

    if search:
        s_term = f"%{search.strip()}%"
        query = query.filter(
            (Event.title.ilike(s_term)) | 
            (Event.quote_text.ilike(s_term)) | 
            (Event.it_offering.ilike(s_term))
        )

    events = query.order_by(Event.occurred_at.desc(), Event.created_at.desc()).offset(offset).limit(limit).all()

    return [EventOut(**e.to_dict()) for e in events]

@router.get("/{event_id}")
def get_event(event_id: str, db: Session = Depends(get_db)):
    event = db.query(Event).options(joinedload(Event.company), joinedload(Event.raw_document)).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event.to_dict()
