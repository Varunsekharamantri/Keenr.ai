import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload

import re

from ..db.database import get_db
from ..ingestion.base import company_core_name
from ..ingestion.tenders import ted_adapter, samgov_adapter
from ..models.schema import Company, Tender
from .date_range import resolve_window, describe_window

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tenders", tags=["Procurement Tenders"])


# TED reports buyer country as ISO-3; company.country holds full names.
ISO3_TO_COUNTRY = {
    "ESP": "Spain", "DEU": "Germany", "FRA": "France", "ITA": "Italy",
    "POL": "Poland", "CZE": "Czechia", "PRT": "Portugal", "NLD": "Netherlands",
    "BEL": "Belgium", "AUT": "Austria", "CHE": "Switzerland", "SWE": "Sweden",
    "NOR": "Norway", "DNK": "Denmark", "FIN": "Finland", "IRL": "Ireland",
    "GRC": "Greece", "HUN": "Hungary", "ROU": "Romania", "BGR": "Bulgaria",
    "HRV": "Croatia", "SVK": "Slovakia", "SVN": "Slovenia", "LTU": "Lithuania",
    "LVA": "Latvia", "EST": "Estonia", "CYP": "Cyprus", "MLT": "Malta",
    "LUX": "Luxembourg", "GBR": "United Kingdom", "USA": "United States",
    "CAN": "Canada", "MDA": "Moldova", "SRB": "Serbia", "MKD": "North Macedonia",
    "ISL": "Iceland", "TUR": "Turkey", "UKR": "Ukraine",
}


def _match_buyer_to_company(buyer_name: str, buyer_country: Optional[str], companies) -> Optional[str]:
    """
    Link a tender buyer to a tracked company — deliberately far stricter than
    the `is_relevant_to_company` helper the news adapters use.

    That helper accepts a 2-letter ticker match, which is fine for English news
    but catastrophic here: Deere & Co's ticker "DE" matches the preposition
    "de" in almost every Spanish, French and Romanian public-body name, which
    mapped 9 unrelated EU hospitals and ministries onto Deere.

    So: full company core name only (>=5 chars, never ticker or alias), AND the
    tender's country must equal the company's HQ country. The country gate is
    what stops "Metro de Madrid" (a Spanish subway) matching "Metro AG" (a
    German wholesaler). A company with no known country never matches — a
    missed link costs nothing, a false one pollutes the lead data.
    """
    if not buyer_name:
        return None
    tender_country = ISO3_TO_COUNTRY.get((buyer_country or "").strip().upper())
    if not tender_country:
        return None

    haystack = buyer_name.lower()
    for c in companies:
        if not c.country or c.country != tender_country:
            continue
        core = company_core_name(c.name or "").lower().strip()
        if len(core) < 5:
            continue
        if re.search(r"\b" + re.escape(core) + r"\b", haystack):
            return c.id
    return None


def ingest_tenders(db: Session, limit: Optional[int] = None) -> dict:
    """
    Pull tenders from every configured source, dedupe on external_id, and
    opportunistically link a tender to a tracked company when the buyer name
    genuinely matches one (rare — TED buyers are public bodies).
    """
    companies = db.query(Company).all()
    existing_ids = {t[0] for t in db.query(Tender.external_id).all()}

    created, skipped, matched = 0, 0, 0
    per_source = {}

    for adapter in (ted_adapter, samgov_adapter):
        fetched = adapter.fetch_tenders(limit) if limit else adapter.fetch_tenders()
        per_source[adapter.source] = len(fetched)
        for row in fetched:
            if row["external_id"] in existing_ids:
                skipped += 1
                continue
            existing_ids.add(row["external_id"])

            matched_company_id = _match_buyer_to_company(
                row.get("buyer_name") or "", row.get("buyer_country"), companies
            )
            if matched_company_id:
                matched += 1

            db.add(Tender(**row, matched_company_id=matched_company_id))
            created += 1

    db.commit()
    result = {
        "created": created,
        "skipped_existing": skipped,
        "matched_to_company": matched,
        "fetched_per_source": per_source,
        "samgov_configured": bool(samgov_adapter.api_key),
    }
    logger.info(f"Tender ingestion: {result}")
    return result


@router.get("")
def list_tenders(
    start: Optional[str] = None,
    end: Optional[str] = None,
    source: Optional[str] = None,
    country: Optional[str] = None,
    search: Optional[str] = None,
    matched_only: bool = False,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    start_dt, end_dt = resolve_window(start, end)
    q = db.query(Tender).options(joinedload(Tender.matched_company)).filter(Tender.published_at <= end_dt)
    if start_dt is not None:
        q = q.filter(Tender.published_at >= start_dt)
    if source and source.lower() != "all":
        q = q.filter(Tender.source == source)
    if country and country.lower() != "all":
        q = q.filter(Tender.buyer_country == country)
    if matched_only:
        q = q.filter(Tender.matched_company_id.isnot(None))
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(Tender.title.ilike(term) | Tender.buyer_name.ilike(term))

    total = q.count()
    rows = q.order_by(Tender.published_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "tenders": [t.to_dict() for t in rows],
        "window": describe_window(start_dt, end_dt),
    }


@router.get("/stats")
def tender_stats(db: Session = Depends(get_db)):
    from sqlalchemy import func
    by_source = dict(db.query(Tender.source, func.count(Tender.id)).group_by(Tender.source).all())
    by_country = dict(
        db.query(Tender.buyer_country, func.count(Tender.id))
        .filter(Tender.buyer_country.isnot(None))
        .group_by(Tender.buyer_country)
        .order_by(func.count(Tender.id).desc()).limit(15).all()
    )
    latest = db.query(func.max(Tender.published_at)).scalar()
    return {
        "total": db.query(func.count(Tender.id)).scalar() or 0,
        "by_source": by_source,
        "by_country": by_country,
        "matched_to_company": db.query(func.count(Tender.id)).filter(Tender.matched_company_id.isnot(None)).scalar() or 0,
        "latest_published_at": latest.isoformat() if latest else None,
        "samgov_configured": bool(samgov_adapter.api_key),
    }


@router.post("/refresh")
def refresh_tenders(limit: Optional[int] = Query(None, ge=1, le=200), db: Session = Depends(get_db)):
    return ingest_tenders(db, limit=limit)
