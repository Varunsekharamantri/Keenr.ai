"""
Aggregates for the Overview tiles (Tech Trends, Business Trends, headline counts).

Two deliberate differences from /stats/overview, which the header cards use:
  * Groups by the taxonomy's initiative_id, never Event.initiative_name. The LLM
    enrichment pass invents its own names, so grouping on the name fragments one
    initiative into many near-duplicates and understates every count.
  * One unit throughout: the share of *active companies* showing an initiative.
    "41% of active companies are pursuing Digital Transformation" can be checked;
    a share of raw mentions is dominated by whichever company generated the most.
"""
import re
from ..signals.scoring import HIGH_POINTS
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db.database import get_db
from ..models.schema import Company, Event, RawDocument
from ..models.taxonomy import taxonomy_manager
from .industry_tiles import build_industry_tiles
from ..signals.engine import signal_engine
from .date_range import describe_window, resolve_window

router = APIRouter(prefix="", tags=["Insights"])

# Initiatives that describe business moves rather than technology programmes,
# in the order the Business Trends tile presents them.
BUSINESS_INITIATIVES = [
    "vendor_partnership_rfp",     # Strategic Vendor Partnership / Major RFP
    "digital_transformation",     # Enterprise Digital Transformation
    "cost_optimization",          # IT Cost Optimization & Vendor Consolidation
    "capex_it_budget",            # CapEx & Technology Investment Surge
    "ma_integration",             # M&A, Divestiture & Systems Integration
    "esg_sustainability_tech",    # ESG, Green IT & Sustainability Tracking
]


def _initiative_rows(ids, companies_by_init, mentions_by_init, active):
    rows = []
    for iid in ids:
        init = taxonomy_manager.get_initiative(iid)
        if not init:
            continue
        cos = len(companies_by_init.get(iid, ()))
        rows.append({
            "id": iid,
            "name": init.name,
            "it_offering": init.it_offering,
            "companies": cos,
            "mentions": mentions_by_init.get(iid, 0),
            "share": round(100 * cos / active) if active else 0,
        })
    return rows



def _concrete_examples(db, initiative_id, start_dt, end_dt, sector, region, limit=3):
    """
    Real examples of an initiative: which company, and what the headline said they
    were doing. This is what lets a summary say what the technology is used for
    instead of only how many companies use it.

    Job postings are excluded - a hiring ad shows intent, not a deployment.
    """
    q = (db.query(Event, Company, RawDocument)
         .join(Company, Company.id == Event.company_id)
         .outerjoin(RawDocument, RawDocument.id == Event.raw_doc_id)
         .filter(Event.initiative_id == initiative_id,
                 Event.occurred_at <= end_dt,
                 Event.source_type != "career_pages"))
    if start_dt is not None:
        q = q.filter(Event.occurred_at >= start_dt)
    if sector and sector.lower() != "all":
        q = q.filter(Company.sector == sector)
    if region:
        q = q.filter(Company.region == region)

    examples, vendors, seen = [], {}, set()
    for ev, co, doc in q.order_by(Event.confidence.desc(), Event.occurred_at.desc()).limit(60).all():
        for ent in (ev.key_entities or []):
            if isinstance(ent, str) and ent.strip():
                vendors[ent.strip()] = vendors.get(ent.strip(), 0) + 1
        if co.id in seen or len(examples) >= limit:
            continue
        headline = (doc.title if doc else None) or ev.title or ""
        if headline.startswith("[") and "]" in headline[:62]:      # strip publisher prefix
            headline = headline[headline.index("]") + 1:].strip()
        # Some filings carry a machine filename as their title
        # ("tm261348-1_nonfiling - none - 51.6367224s"). Require a headline that
        # reads like prose: mostly real words, no long alphanumeric blobs.
        words = [w for w in re.split(r"[\s\-_]+", headline) if w]
        wordy = sum(1 for w in words if w.isalpha() and len(w) > 2)
        junk = any(len(w) > 12 and any(ch.isdigit() for ch in w) for w in words)
        if len(headline) < 15 or junk or wordy < 4:
            continue
        seen.add(co.id)
        examples.append(f"{co.name}: \"{headline[:110]}\"")
    top_vendors = [v for v, _ in sorted(vendors.items(), key=lambda kv: -kv[1])[:4]]
    return examples, top_vendors

@router.get("/insights/overview")
def insights_overview(
    start: Optional[str] = None,
    end: Optional[str] = None,
    region: Optional[str] = None,
    sector: str = "BFSI",
    db: Session = Depends(get_db),
):
    start_dt, end_dt = resolve_window(start, end)
    region = None if not region or region.lower() == "all" else region

    q = (db.query(Event.company_id, Event.initiative_id, Event.category_id)
         .join(Company, Company.id == Event.company_id)
         .filter(Event.occurred_at <= end_dt))
    if start_dt is not None:
        q = q.filter(Event.occurred_at >= start_dt)
    if sector and sector.lower() != "all":
        q = q.filter(Company.sector == sector)
    if region:
        q = q.filter(Company.region == region)

    companies_by_init = defaultdict(set)
    mentions_by_init = defaultdict(int)
    active_companies = set()
    mentions = 0
    for company_id, initiative_id, _category_id in q.all():
        mentions += 1
        active_companies.add(company_id)
        # Only count initiatives the taxonomy knows; an invented id has no
        # stable label and would show up as a phantom trend.
        if taxonomy_manager.get_initiative(initiative_id):
            companies_by_init[initiative_id].add(company_id)
            mentions_by_init[initiative_id] += 1
    active = len(active_companies)

    tech_ids = []
    for cat in taxonomy_manager.get_all_categories():
        if cat.id == "tech_initiatives":
            tech_ids = [i.id for i in cat.initiatives]
    tech = sorted(_initiative_rows(tech_ids, companies_by_init, mentions_by_init, active),
                  key=lambda r: (-r["companies"], -r["mentions"]))
    business = _initiative_rows(
        [i for i in BUSINESS_INITIATIVES if taxonomy_manager.get_initiative(i)],
        companies_by_init, mentions_by_init, active)
    business = [b for b in business if b["companies"] > 0] or business[:3]

    signals = signal_engine.compute_window(db, start_dt, end_dt,
                                           sector=None if sector.lower() == "all" else sector,
                                           region=region)

    # The denominator must match the filter: "64 of 199" on the Americas view
    # compared a regional count against every BFSI company worldwide.
    tracked_q = db.query(Company)
    if sector and sector.lower() != "all":
        tracked_q = tracked_q.filter(Company.sector == sector)
    if region:
        tracked_q = tracked_q.filter(Company.region == region)

    # Newest data regardless of the window, so an empty "last 7 days" can say
    # why it is empty instead of looking broken.
    latest_q = db.query(func.max(Event.occurred_at)).join(Company, Company.id == Event.company_id)
    if sector and sector.lower() != "all":
        latest_q = latest_q.filter(Company.sector == sector)
    if region:
        latest_q = latest_q.filter(Company.region == region)
    latest = latest_q.scalar()

    # Every tile gets an analyst-style summary and the sources behind it.
    window_label = describe_window(start_dt, end_dt).get("label", "this window")
    try:
        from .routes_results import recent_results
        results = recent_results(start=start, end=end, sector=sector, region=region,
                                 limit=50, db=db).get("items", [])
    except Exception:
        results = []
    tiles = build_industry_tiles(db, start_dt, end_dt,
                                 None if sector.lower() == "all" else sector, region,
                                 active, tech, business, window_label, results)

    return {
        "window": describe_window(start_dt, end_dt),
        "region": region or "All regions",
        "tiles": tiles,
        # Kept so an older cached page still finds what it expects.
        "tech_summary": tiles.get("tech", {}).get("summary"),
        "business_summary": tiles.get("business", {}).get("summary"),
        "latest_data_at": latest.isoformat() if latest else None,
        "active_companies": active,
        "tracked_companies": tracked_q.count(),
        "mentions": mentions,
        "signals": len(signals),
        "hot_signals": sum(1 for s in signals if s["intent_score"] >= HIGH_POINTS),
        "tech": tech,
        "business": business,
    }
