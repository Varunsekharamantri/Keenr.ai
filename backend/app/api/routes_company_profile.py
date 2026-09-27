"""
Everything the company page shows, in one request.

Sections map to what we actually hold:
  key_signals    counts by category over the window
  financials     SEC XBRL (companies with a CIK only)
  priorities     ranked signals, with the evidence behind each
  technology     technology initiatives, grouped, with recent moves
  leadership     executive appointments (strict - see Components.insIsLeadershipMove)
  deals          partnerships, RFPs, M&A and disclosed budgets
  news           recent stories
  why_it_matters computed from the above, never model-authored prose

Sections we cannot source (employee count, CEO name, website, share price) are
absent rather than guessed.
"""
import datetime
import re
from ..signals.scoring import NON_OPPORTUNITY_INITIATIVES, strength_of
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from ..data.sec_financials import financials_for
from ..db.database import get_db
from ..models.schema import Company, Event, RawDocument
from ..models.taxonomy import taxonomy_manager
from ..signals.engine import signal_engine
from .date_range import describe_window, resolve_window

router = APIRouter(prefix="", tags=["Company profile"])

# Same rule the Overview uses: a job title mentioned in passing is not a move.
_MOVE = re.compile(
    r"\b(appoint(s|ed|ment)?|name[sd]\b|hire[sd]?\b|joins?\b|joined|promot(ed|ion|es)|elevat(ed|es)"
    r"|steps? down|stepp(ed|ing) down|resign(s|ed|ation)?|retir(es|ed|ement)|succeed(s|ed)?|successor"
    r"|takes? over|to (lead|head)|replac(es|ed|ing)"
    r"|new (group )?(ceo|cio|cto|ciso|cdo|coo|cfo|chief|head|president|chair))\b", re.I)

_CATEGORY_LABELS = {
    "tech_initiatives": "Tech & Innovation",
    "spending_signals": "Partnerships / Deals",
    "organizational_change": "Leadership Changes",
    "regulatory_compliance": "Regulatory / Compliance",
    "strategic_priorities": "Strategic Priorities",
}


def _doc_view(e: Event) -> dict:
    doc = e.raw_document
    meta = (doc.metadata_json or {}) if doc else {}
    return {
        "event_id": e.id,
        "title": (doc.title if doc else None) or e.title,
        "url": e.source_url,
        "date": (doc.filing_date if doc and doc.filing_date else e.occurred_at).isoformat(),
        "source_type": e.source_type,
        "image": meta.get("image_url"),
        "quote": e.quote_text,
        "initiative_id": e.initiative_id,
        "initiative_name": (taxonomy_manager.get_initiative(e.initiative_id).name
                            if taxonomy_manager.get_initiative(e.initiative_id) else e.initiative_name),
        "category_id": e.category_id,
        "spend_amount": e.spend_amount,
    }


def _unique_by_doc(events, limit=None):
    seen, out = set(), []
    for e in sorted(events, key=lambda x: (x.raw_document.filing_date if x.raw_document and x.raw_document.filing_date
                                           else x.occurred_at), reverse=True):
        key = e.raw_doc_id or e.source_url
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
        if limit and len(out) >= limit:
            break
    return out


@router.get("/companies/{company_id}/profile")
def company_profile(
    company_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    signal_days: int = Query(90, ge=7, le=365),
    db: Session = Depends(get_db),
):
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    start_dt, end_dt = resolve_window(start, end)

    events = (db.query(Event).options(joinedload(Event.raw_document))
              .filter(Event.company_id == company_id, Event.occurred_at <= end_dt))
    if start_dt is not None:
        events = events.filter(Event.occurred_at >= start_dt)
    events = events.all()

    # Key signals use their own fixed lookback so the header reads the same
    # regardless of the date range chosen for the rest of the page.
    since = datetime.datetime.utcnow() - datetime.timedelta(days=signal_days)
    recent = (db.query(Event).filter(Event.company_id == company_id, Event.occurred_at >= since).all())
    key_counts = defaultdict(int)
    for e in recent:
        key_counts[e.category_id] += 1
    key_signals = [{"category_id": cid, "label": _CATEGORY_LABELS.get(cid, cid), "count": n}
                   for cid, n in sorted(key_counts.items(), key=lambda kv: -kv[1])]

    # Ranked signals for this company in the window - the same scoring the rest
    # of the dashboard uses, so a priority here matches its ranked-signal score.
    signals = [s for s in signal_engine.compute_window(db, start_dt, end_dt, company_id=company_id)
               if s["initiative_id"] not in NON_OPPORTUNITY_INITIATIVES]
    signals.sort(key=lambda s: -s["intent_score"])
    priorities = []
    for s in signals[:6]:
        # Taxonomy label, not the stored name: the LLM enrichment pass invents
        # its own ("Generative AI & Autonomous Agents"), which would make the
        # same initiative read differently on every page.
        known = taxonomy_manager.get_initiative(s["initiative_id"])
        priorities.append({
            "initiative_id": s["initiative_id"],
            "initiative_name": known.name if known else s["initiative_name"],
            "category_id": s.get("category_id"),
            "intent_score": s["intent_score"],
            "priority": strength_of(s["intent_score"]),
            "points_explained": (s.get("score_breakdown") or {}).get("points_explained"),
            "it_offering": s.get("it_offering"),
            "timing_window": s.get("timing_window"),
            "event_count": s.get("event_count"),
            "document_count": s.get("document_count"),
            "source_count": s.get("distinct_source_count"),
            "reasons": (s.get("score_breakdown") or {}).get("reasons", [])[:3],
        })

    tech_events = [e for e in events if e.category_id == "tech_initiatives"]
    by_init = defaultdict(list)
    for e in tech_events:
        by_init[e.initiative_id].append(e)
    technology = []
    for iid, evs in sorted(by_init.items(), key=lambda kv: -len(kv[1])):
        init = taxonomy_manager.get_initiative(iid)
        if not init:
            continue
        technology.append({
            "initiative_id": iid,
            "name": init.name,
            "it_offering": init.it_offering,
            "mentions": len(evs),
            "moves": [_doc_view(e) for e in _unique_by_doc(evs, limit=4)],
        })

    leadership = [_doc_view(e) for e in _unique_by_doc(
        [e for e in events
         if e.initiative_id == "executive_leadership_change"
         and e.source_type != "career_pages"
         and _MOVE.search(f"{(e.raw_document.title if e.raw_document else '') or ''} {e.quote_text or ''}")],
        limit=6)]

    deals = [_doc_view(e) for e in _unique_by_doc(
        [e for e in events
         if e.category_id == "spending_signals" or e.initiative_id == "ma_integration" or e.spend_amount],
        limit=8)]

    regulatory = [_doc_view(e) for e in _unique_by_doc(
        [e for e in events if e.category_id == "regulatory_compliance"], limit=5)]

    news = [_doc_view(e) for e in _unique_by_doc(
        [e for e in events if e.source_type in ("exa_news", "news_rss", "ir_press")], limit=8)]

    # "Why this matters": statements of fact drawn from the sections above.
    why = []
    if priorities:
        top = priorities[0]
        why.append({
            "tag": "Priority",
            "title": f"{top['initiative_name']} is the strongest signal",
            "detail": f"{top['priority']} strength: {top.get('points_explained') or top['intent_score']}, across "
                      f"{top['source_count']} source types."
                      + (f" Typical timing: {top['timing_window']}." if top.get("timing_window") else ""),
        })
    if technology:
        t = technology[0]
        why.append({"tag": "Tech", "title": f"{t['name']} is the busiest technology theme",
                    "detail": f"{t['mentions']} mentions in this window. Fits: {t['it_offering']}."})
    if leadership:
        why.append({"tag": "Leadership", "title": f"{len(leadership)} leadership "
                    f"{'move' if len(leadership) == 1 else 'moves'} detected",
                    "detail": "New decision-makers often reset vendor relationships."})
    if deals:
        spend = next((d["spend_amount"] for d in deals if d["spend_amount"]), None)
        why.append({"tag": "Deals", "title": f"{len(deals)} partnership, RFP or budget "
                    f"{'signal' if len(deals) == 1 else 'signals'}",
                    "detail": (f"Largest disclosed amount: {spend}." if spend
                               else "Active vendor engagement is the clearest sign of an open buying window.")})

    # Named technology leaders on file (People to Tap), so Leadership is not an
    # empty tile in a quiet month; and the technology suppliers named in this
    # company's documents - only names the vendor map recognises, so generic
    # words ("big tech") and counterparties ("World Bank") never read as partners.
    from ..people.leaders import FUNCTION_LABEL, current_leaders
    from ..signals.entities import category_for, normalize_entity
    on_file = sorted(current_leaders(db, [company_id]).get(company_id, []),
                     key=lambda l: (-(l.seniority or 1), l.name))
    leaders = [{**l.to_dict(), "areas": [FUNCTION_LABEL.get(f, f) for f in (l.functions or [])][:3]}
               for l in on_file[:8]]
    vendor_counts = defaultdict(int)
    for e in events:
        for raw in (e.key_entities or []):
            name = normalize_entity(raw)
            if name and category_for(name) != "Other" and name.lower() not in company.name.lower():
                vendor_counts[name] += 1
    vendors = [{"name": n, "mentions": c, "category": category_for(n)}
               for n, c in sorted(vendor_counts.items(), key=lambda kv: -kv[1])[:10]]

    return {
        "company": company.to_dict(),
        "leaders": leaders,
        "vendors": vendors,
        "window": describe_window(start_dt, end_dt),
        "signal_days": signal_days,
        "key_signals": key_signals,
        "totals": {
            "events": len(events),
            "documents": db.query(RawDocument).filter(RawDocument.company_id == company_id).count(),
            "signals": len(signals),
            "top_score": signals[0]["intent_score"] if signals else None,
        },
        "financials": financials_for(company.cik),
        "priorities": priorities,
        "technology": technology,
        "leadership": leadership,
        "deals": deals,
        "regulatory": regulatory,
        "news": news,
        "why_it_matters": why,
    }
