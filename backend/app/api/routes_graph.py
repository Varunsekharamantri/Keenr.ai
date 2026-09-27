from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from ..db.database import get_db
from ..models.schema import Company, Event
from ..signals.entities import category_for, normalize_entities
from .date_range import resolve_window, describe_window

router = APIRouter(prefix="/graph", tags=["Vendor & Technology Graph"])


def _windowed_events(db: Session, start_dt, end_dt, sector=None, region=None, company_id=None):
    q = db.query(Event).options(joinedload(Event.company)).filter(Event.occurred_at <= end_dt)
    if start_dt is not None:
        q = q.filter(Event.occurred_at >= start_dt)
    if sector or region:
        q = q.join(Company)
        if sector and sector.lower() != "all":
            q = q.filter(Company.sector == sector)
        if region and region.lower() != "all":
            q = q.filter(Company.region == region)
    if company_id:
        q = q.filter(Event.company_id == company_id)
    return q.all()


@router.get("/vendors")
def vendor_graph(
    start: Optional[str] = None,
    end: Optional[str] = None,
    sector: Optional[str] = None,
    region: Optional[str] = None,
    min_mentions: int = Query(1, ge=1),
    limit: int = Query(40, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    Which companies are adopting which vendors/technologies, mined from the
    entities the extractor already pulls out of each event.
    """
    start_dt, end_dt = resolve_window(start, end)
    events = _windowed_events(db, start_dt, end_dt, sector=sector, region=region)

    mentions = defaultdict(int)
    companies_by_vendor = defaultdict(dict)
    initiatives_by_vendor = defaultdict(lambda: defaultdict(int))
    citations_by_vendor = defaultdict(list)

    for e in events:
        for vendor in normalize_entities(e.key_entities):
            mentions[vendor] += 1
            if e.company:
                entry = companies_by_vendor[vendor].setdefault(e.company_id, {
                    "company_id": e.company_id,
                    "name": e.company.name,
                    "ticker": e.company.ticker,
                    "region": e.company.region,
                    "mentions": 0,
                })
                entry["mentions"] += 1
            initiatives_by_vendor[vendor][e.initiative_name] += 1
            if len(citations_by_vendor[vendor]) < 3:
                citations_by_vendor[vendor].append({
                    "company": e.company.name if e.company else None,
                    "quote": (e.quote_text or "")[:200],
                    "source_type": e.source_type,
                    "source_url": e.source_url,
                    "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                })

    vendors = []
    for vendor, count in mentions.items():
        if count < min_mentions:
            continue
        companies = sorted(companies_by_vendor[vendor].values(), key=lambda c: c["mentions"], reverse=True)
        initiatives = sorted(initiatives_by_vendor[vendor].items(), key=lambda kv: kv[1], reverse=True)
        vendors.append({
            "vendor": vendor,
            "category": category_for(vendor),
            "mentions": count,
            "company_count": len(companies),
            "companies": companies[:25],
            "initiatives": [{"name": n, "count": c} for n, c in initiatives[:6]],
            "citations": citations_by_vendor[vendor],
        })
    vendors.sort(key=lambda v: (v["company_count"], v["mentions"]), reverse=True)

    by_category = defaultdict(int)
    for v in vendors:
        by_category[v["category"]] += v["mentions"]

    return {
        "vendors": vendors[:limit],
        "total_vendors": len(vendors),
        "events_scanned": len(events),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)),
        "window": describe_window(start_dt, end_dt),
    }


@router.get("/company/{company_id}/vendors")
def company_vendor_footprint(
    company_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """The technology footprint of a single company."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    start_dt, end_dt = resolve_window(start, end)
    events = _windowed_events(db, start_dt, end_dt, company_id=company_id)

    counts = defaultdict(int)
    initiatives = defaultdict(set)
    for e in events:
        for vendor in normalize_entities(e.key_entities):
            counts[vendor] += 1
            initiatives[vendor].add(e.initiative_name)

    return {
        "company": company.to_dict(),
        "vendors": [
            {
                "vendor": v,
                "category": category_for(v),
                "mentions": n,
                "initiatives": sorted(initiatives[v]),
            }
            for v, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "window": describe_window(start_dt, end_dt),
    }
