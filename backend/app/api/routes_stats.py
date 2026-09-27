import datetime
from ..signals.scoring import HIGH_POINTS
from collections import defaultdict
from typing import Optional
import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from ..config import settings
from ..db.database import get_db
from ..db.region_data import resolve_company_region
from ..models.schema import Company, Event, RawDocument, SourceRun
from ..models.taxonomy import taxonomy_manager
from ..signals.engine import signal_engine
from ..scheduler import get_scheduler_status
from .date_range import resolve_window, describe_window

router = APIRouter(prefix="", tags=["Stats & Taxonomy"])

@router.get("/taxonomy")
def get_taxonomy():
    """Return full taxonomy tree for frontend filtering and tagging."""
    return {
        "categories": [
            {
                "id": cat.id,
                "name": cat.name,
                "description": cat.description,
                "initiatives": [
                    {
                        "id": init.id,
                        "name": init.name,
                        "it_offering": init.it_offering,
                        "keywords": init.keywords
                    }
                    for init in cat.initiatives
                ]
            }
            for cat in taxonomy_manager.get_all_categories()
        ]
    }

@router.get("/stats/overview")
def get_overview_stats(
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """All aggregates are scoped to the selected date window so the dashboard
    headline numbers always match what the views below them are showing."""
    start_dt, end_dt = resolve_window(start, end)

    def scoped(query, date_col=Event.occurred_at):
        query = query.filter(date_col <= end_dt)
        if start_dt is not None:
            query = query.filter(date_col >= start_dt)
        return query

    total_companies_tracked = db.query(func.count(Company.id)).scalar() or 0
    total_events = scoped(db.query(func.count(Event.id))).scalar() or 0
    total_docs = scoped(db.query(func.count(RawDocument.id)), RawDocument.filing_date).scalar() or 0
    high_confidence_events = scoped(db.query(func.count(Event.id)).filter(Event.confidence >= 0.8)).scalar() or 0
    active_companies = scoped(db.query(func.count(func.distinct(Event.company_id)))).scalar() or 0

    events_by_sector = dict(scoped(
        db.query(Company.sector, func.count(Event.id)).join(Event, Company.id == Event.company_id)
    ).group_by(Company.sector).all())

    events_by_region = dict(scoped(
        db.query(Company.region, func.count(Event.id)).join(Event, Company.id == Event.company_id)
    ).filter(Company.region.isnot(None)).group_by(Company.region).all())

    events_by_cat = dict(scoped(
        db.query(Event.category_name, func.count(Event.id))
    ).group_by(Event.category_name).all())

    top_initiatives = [
        {"name": name, "count": count}
        for name, count in scoped(db.query(Event.initiative_name, func.count(Event.id)))
        .group_by(Event.initiative_name).order_by(func.count(Event.id).desc()).limit(8).all()
    ]

    source_breakdown = dict(scoped(
        db.query(Event.source_type, func.count(Event.id))
    ).group_by(Event.source_type).all())

    signals = signal_engine.compute_window(db, start_dt, end_dt)

    return {
        # "total_companies" stays the key the UI reads, but now means companies
        # with activity in the window; the full universe is alongside it.
        "total_companies": active_companies,
        "total_companies_tracked": total_companies_tracked,
        "total_events": total_events,
        "total_documents": total_docs,
        "high_confidence_events": high_confidence_events,
        "signals_count": len(signals),
        "hot_signals_count": sum(1 for s in signals if s["intent_score"] >= HIGH_POINTS),
        "events_by_sector": events_by_sector,
        "events_by_region": events_by_region,
        "events_by_category": events_by_cat,
        "top_initiatives": top_initiatives,
        "source_breakdown": source_breakdown,
        "window": describe_window(start_dt, end_dt),
    }


@router.get("/stats/heatmap")
def get_heatmap(
    start: Optional[str] = None,
    end: Optional[str] = None,
    rows: str = Query("sector", pattern="^(sector|industry|region|country)$"),
    cols: str = Query("category", pattern="^(category|initiative)$"),
    db: Session = Depends(get_db),
):
    """
    Event counts as a rows x cols matrix. Defaults to sector x category (8x5);
    `industry` is capped to its top 15 by volume because there are 81 of them,
    which is unreadable as a heatmap.
    """
    start_dt, end_dt = resolve_window(start, end)
    row_col = {
        "sector": Company.sector,
        "industry": Company.industry,
        "region": Company.region,
        "country": Company.country,
    }[rows]
    # Group initiatives on the taxonomy-validated id, not initiative_name: the
    # LLM enrichment pass invents its own display names, so grouping on the
    # name yields 300+ one-off columns instead of the 15 real initiatives.
    col_col = Event.category_name if cols == "category" else Event.initiative_id

    q = db.query(row_col, col_col, func.count(Event.id)).join(Company, Company.id == Event.company_id)
    q = q.filter(Event.occurred_at <= end_dt)
    if start_dt is not None:
        q = q.filter(Event.occurred_at >= start_dt)
    data = q.group_by(row_col, col_col).all()

    def col_label(value: str) -> str:
        if cols == "category":
            return value
        definition = taxonomy_manager.get_initiative(value)
        return definition.name if definition else value

    row_totals, col_totals = defaultdict(int), defaultdict(int)
    cell = defaultdict(int)
    for r, c, n in data:
        if not r or not c:
            continue
        c = col_label(c)
        cell[(r, c)] += n
        row_totals[r] += n
        col_totals[c] += n

    row_labels = sorted(row_totals, key=lambda r: row_totals[r], reverse=True)
    if rows in ("industry", "country"):
        row_labels = row_labels[:15]
    col_labels = sorted(col_totals, key=lambda c: col_totals[c], reverse=True)

    matrix = [[cell.get((r, c), 0) for c in col_labels] for r in row_labels]
    return {
        "rows": row_labels,
        "cols": col_labels,
        "matrix": matrix,
        "row_totals": [row_totals[r] for r in row_labels],
        "col_totals": [col_totals[c] for c in col_labels],
        "max": max((n for row in matrix for n in row), default=0),
        "total": sum(n for row in matrix for n in row),
        "window": describe_window(start_dt, end_dt),
    }


@router.get("/stats/trends")
def get_trends(
    start: Optional[str] = None,
    end: Optional[str] = None,
    bucket: str = Query("auto", pattern="^(auto|day|week|month)$"),
    db: Session = Depends(get_db),
):
    """Event volume over time, split by source_type for a stacked chart."""
    start_dt, end_dt = resolve_window(start, end)
    lower = start_dt or (db.query(func.min(Event.occurred_at)).scalar() or end_dt)

    if bucket == "auto":
        span_days = max(1, (end_dt - lower).days)
        bucket = "day" if span_days <= 31 else ("week" if span_days <= 180 else "month")

    q = db.query(Event.occurred_at, Event.source_type).filter(Event.occurred_at <= end_dt)
    if start_dt is not None:
        q = q.filter(Event.occurred_at >= start_dt)
    rows = q.all()

    def bucket_key(dt: datetime.datetime) -> str:
        if bucket == "day":
            return dt.date().isoformat()
        if bucket == "week":
            return (dt - datetime.timedelta(days=dt.weekday())).date().isoformat()
        return dt.date().replace(day=1).isoformat()

    per_bucket = defaultdict(lambda: defaultdict(int))
    sources = set()
    for occurred_at, src in rows:
        per_bucket[bucket_key(occurred_at)][src] += 1
        sources.add(src)

    labels = sorted(per_bucket)
    source_list = sorted(sources)
    return {
        "bucket": bucket,
        "labels": labels,
        "sources": source_list,
        "series": [
            {"source_type": s, "counts": [per_bucket[b].get(s, 0) for b in labels]}
            for s in source_list
        ],
        "totals": [sum(per_bucket[b].values()) for b in labels],
        "total": len(rows),
        "window": describe_window(start_dt, end_dt),
    }


@router.post("/admin/backfill-regions")
def backfill_regions(
    sector: Optional[str] = "BFSI",
    overwrite: bool = False,
    db: Session = Depends(get_db),
):
    """
    Populate Company.country / .region from SEC (authoritative for CIK filers)
    plus the curated HQ table. Returns the full classification so it can be
    spot-checked — this is HQ data, and a wrong region silently mis-scopes
    every downstream Americas/RoW view.
    """
    query = db.query(Company)
    if sector and sector.lower() != "all":
        query = query.filter(Company.sector == sector)
    companies = query.order_by(Company.name).all()

    updated, skipped, unknown = 0, 0, []
    by_source = {"sec": 0, "curated": 0, "unknown": 0}
    classified = []

    with httpx.Client(timeout=15.0) as client:
        for c in companies:
            if c.region and not overwrite:
                skipped += 1
                classified.append({"company": c.name, "ticker": c.ticker,
                                   "country": c.country, "region": c.region, "source": "existing"})
                continue
            country, region, source = resolve_company_region(c.name, c.cik, session=client)
            by_source[source] += 1
            if region:
                c.country, c.region = country, region
                updated += 1
            else:
                unknown.append({"company": c.name, "ticker": c.ticker, "cik": c.cik})
            classified.append({"company": c.name, "ticker": c.ticker,
                               "country": country, "region": region, "source": source})
    db.commit()

    return {
        "companies_examined": len(companies),
        "updated": updated,
        "skipped_already_set": skipped,
        "by_source": by_source,
        "unclassified_count": len(unknown),
        "unclassified": unknown,
        "region_totals": {
            r: db.query(func.count(Company.id)).filter(
                Company.sector == sector if sector and sector.lower() != "all" else True,
                Company.region == r,
            ).scalar()
            for r in ("Americas", "Rest of World")
        },
        "classified": classified,
    }


@router.get("/admin/source-health")
def get_source_health(
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Per-source health from three angles: freshness (last successful document),
    volume in the window, and reliability (SourceRun history). Freshness comes
    from RawDocument rather than IngestionJob because a batch job rolls all
    sources into one status and would hide a single dead adapter.
    """
    start_dt, end_dt = resolve_window(start, end)
    now = datetime.datetime.utcnow()
    sla_hours = settings.SOURCE_FRESHNESS_SLA_HOURS

    freshness = {
        src: (cnt, last)
        for src, cnt, last in db.query(
            RawDocument.source_type, func.count(RawDocument.id), func.max(RawDocument.created_at)
        ).group_by(RawDocument.source_type).all()
    }

    windowed_docs_q = db.query(RawDocument.source_type, func.count(RawDocument.id)).filter(RawDocument.created_at <= end_dt)
    windowed_events_q = db.query(Event.source_type, func.count(Event.id)).filter(Event.occurred_at <= end_dt)
    if start_dt is not None:
        windowed_docs_q = windowed_docs_q.filter(RawDocument.created_at >= start_dt)
        windowed_events_q = windowed_events_q.filter(Event.occurred_at >= start_dt)
    windowed_docs = dict(windowed_docs_q.group_by(RawDocument.source_type).all())
    windowed_events = dict(windowed_events_q.group_by(Event.source_type).all())

    runs = defaultdict(list)
    for r in db.query(SourceRun).order_by(SourceRun.started_at.desc()).limit(500).all():
        runs[r.source_type].append(r)

    known_sources = sorted(
        set(freshness) | set(windowed_docs) | set(windowed_events) | set(runs)
        | {"sec_edgar", "earnings_deck", "news_rss", "exa_news", "ir_press", "career_pages"}
    )

    sources = []
    for src in known_sources:
        total_docs, last_doc = freshness.get(src, (0, None))
        age_hours = round((now - last_doc).total_seconds() / 3600, 1) if last_doc else None
        src_runs = runs.get(src, [])
        attempted = sum(r.companies_attempted or 0 for r in src_runs)
        errors = sum(r.error_count or 0 for r in src_runs)
        success_rate = round(100 * (attempted - errors) / attempted, 1) if attempted else None
        last_error = next((r.last_error for r in src_runs if r.last_error), None)

        if last_doc is None:
            status = "no_data"
        elif age_hours is not None and age_hours > sla_hours:
            status = "stale"
        else:
            status = "healthy"

        sources.append({
            "source_type": src,
            "status": status,
            "last_document_at": last_doc.isoformat() if last_doc else None,
            "age_hours": age_hours,
            "total_documents": total_docs,
            "documents_in_window": windowed_docs.get(src, 0),
            "events_in_window": windowed_events.get(src, 0),
            "runs_recorded": len(src_runs),
            "companies_attempted": attempted,
            "error_count": errors,
            "success_rate": success_rate,
            "last_error": last_error,
        })

    return {
        "sla_hours": sla_hours,
        "sources": sources,
        "healthy_count": sum(1 for s in sources if s["status"] == "healthy"),
        "stale_count": sum(1 for s in sources if s["status"] == "stale"),
        "scheduler": get_scheduler_status(),
        "window": describe_window(start_dt, end_dt),
    }
