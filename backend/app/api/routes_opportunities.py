"""
Opportunity Radar: the ranked buying opportunities across the BFSI universe.

An "opportunity" is one company pursuing one initiative - the same unit the rest
of the dashboard calls a signal - so a number here always reconciles with the
company page it came from.

Deliberately absent: any monetary value. Nothing we collect supports one. The
extractor's `stated_spend` is not a budget - it captures any figure in the
sentence, which in practice is often revenue ("Microsoft Cloud revenue increased
to $214.4 billion"), an expense line, or another currency read as dollars
("R$ 13.2 billion"). Until it can tell spend from revenue, no money is shown
here rather than a number that cannot be trusted.

Growth is measured against the previous window of equal length, not year over
year: ingestion does not yet span a year, so a YoY figure would be fiction.
"""
import datetime
import re
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.orm import Session, joinedload

from ..db.database import get_db
from ..models.schema import Company, Event
from ..models.taxonomy import taxonomy_manager
from ..signals.engine import signal_engine
from .date_range import describe_window, resolve_window
from .routes_results import _clean_title
from .routes_insights import _concrete_examples
from ..ai.summarizer import summarize_many

router = APIRouter(prefix="", tags=["Opportunities"])

from ..signals.scoring import HIGH_POINTS, MEDIUM_POINTS, NON_OPPORTUNITY_INITIATIVES, POINTS, strength_of
HIGH, MEDIUM = HIGH_POINTS, MEDIUM_POINTS   # one definition, shared with every page

_MARKET_TAGS = {
    "regulatory_compliance": "Regulatory",
    "spending_signals": "Deals",
    "organizational_change": "Leadership",
    "tech_initiatives": "Technology",
    "strategic_priorities": "Strategy",
}


def _priority(score: int) -> str:
    return strength_of(score)


def _strength(score: int) -> str:
    return strength_of(score)


def _theme_name(initiative_id: str, fallback: str) -> str:
    init = taxonomy_manager.get_initiative(initiative_id)
    return init.name if init else fallback


@router.get("/opportunities")
def opportunities(
    start: Optional[str] = None,
    end: Optional[str] = None,
    region: Optional[str] = None,
    sector: str = "BFSI",
    limit_featured: int = Query(8, ge=1, le=50),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    start_dt, end_dt = resolve_window(start, end)
    region = None if not region or region.lower() == "all" else region
    sector_arg = None if sector.lower() == "all" else sector

    signals = [x for x in signal_engine.compute_window(db, start_dt, end_dt, sector=sector_arg, region=region)
               if x["initiative_id"] not in NON_OPPORTUNITY_INITIATIVES]
    # Points produce many ties (hundreds of opportunities have 1 point), so
    # break them by the newest evidence: two sorts, the stable second one wins.
    signals.sort(key=lambda s: s.get("last_seen_at") or "", reverse=True)
    signals.sort(key=lambda s: -s["intent_score"])

    # Previous window of equal length, for a change figure that is measurable.
    comparison = None
    rising = []
    if start_dt is not None:
        span = end_dt - start_dt
        prev = [x for x in signal_engine.compute_window(db, start_dt - span, start_dt, sector=sector_arg, region=region)
                if x["initiative_id"] not in NON_OPPORTUNITY_INITIATIVES]
        prev_high = sum(1 for s in prev if s["intent_score"] >= HIGH)
        cur_high = sum(1 for s in signals if s["intent_score"] >= HIGH)
        pct = (lambda now, before: None if not before else round(100 * (now - before) / before))
        comparison = {
            "previous_opportunities": len(prev),
            "opportunities_change_pct": pct(len(signals), len(prev)),
            "previous_high_priority": prev_high,
            "high_priority_change_pct": pct(cur_high, prev_high),
            "basis": "vs the previous window of the same length",
        }

        # Rising Fast: the same (company, initiative) pair scored against itself
        # a window ago. `prev` above is already computed for the KPI comparison
        # and was previously thrown away after two aggregate counts - the
        # per-pair difference was sitting right there unused. A pair absent from
        # `prev` is brand new this window, so its "previous" score is treated as
        # zero rather than excluded - a new 60 and an existing 40->95 are both
        # genuine "why now" stories, just distinguished as new vs. accelerating.
        prev_index = {(p["company_id"], p["initiative_id"]): p for p in prev}
        candidates = []
        for s in signals:
            if s["intent_score"] < MEDIUM:
                continue
            prev_s = prev_index.get((s["company_id"], s["initiative_id"]))
            prev_score = prev_s["intent_score"] if prev_s else 0
            delta = s["intent_score"] - prev_score
            if delta <= 0:
                continue
            candidates.append({
                "company_id": s["company_id"],
                "company_name": s["company_name"],
                "initiative_id": s["initiative_id"],
                "theme": _theme_name(s["initiative_id"], s["initiative_name"]),
                "intent_score": s["intent_score"],
                "previous_score": prev_s["intent_score"] if prev_s else None,
                "delta": delta,
                "is_new": prev_s is None,
                "strength": _strength(s["intent_score"]),
                "previous_strength": _strength(prev_s["intent_score"]) if prev_s else None,
                "document_count": s.get("document_count"),
                "document_delta": (s.get("document_count") or 0) - ((prev_s or {}).get("document_count") or 0),
                "timing_window": s.get("timing_window"),
            })
        candidates.sort(key=lambda c: -c["delta"])
        rising = candidates[:8]

    scores = [s["intent_score"] for s in signals]
    avg = round(sum(scores) / len(scores)) if scores else 0

    # Themes
    by_theme = defaultdict(list)
    for s in signals:
        if taxonomy_manager.get_initiative(s["initiative_id"]):
            by_theme[s["initiative_id"]].append(s)
    themes = []
    for iid, group in by_theme.items():
        init = taxonomy_manager.get_initiative(iid)
        best = max(group, key=lambda s: s["intent_score"])
        theme_avg = round(sum(s["intent_score"] for s in group) / len(group))
        themes.append({
            "initiative_id": iid,
            "name": init.name,
            "it_offering": init.it_offering,
            "opportunities": len(group),
            "companies": len({s["company_id"] for s in group}),
            "avg_score": theme_avg,
            "top_score": best["intent_score"],
            "priority": _priority(best["intent_score"]),
            "high_count": sum(1 for s in group if s["intent_score"] >= HIGH),
            "top_company": {"id": best["company_id"], "name": best["company_name"]},
        })
    themes.sort(key=lambda t: (-t["opportunities"], -t["top_score"]))

    # Competitive Pressure: for a handful of the top-scored initiatives, who
    # else in the same industry (falling back to sector) is also active on it.
    # `peer_context` is already computed by the engine for every signal - this
    # just picks a few worth showing and turns each into a small ranked group
    # instead of the per-row chips buried in signal detail. `signals` is
    # already sorted by score above, so the first signal seen per initiative
    # is that initiative's current leader.
    competitive = []
    seen_initiatives = set()
    for s in signals:
        iid = s["initiative_id"]
        if iid in seen_initiatives:
            continue
        peers = s.get("peer_context") or []
        if not peers:
            continue
        seen_initiatives.add(iid)
        rows = {s["company_id"]: {"company_id": s["company_id"], "name": s["company_name"],
                                   "ticker": s.get("company_ticker"), "intent_score": s["intent_score"]}}
        for p in peers:
            rows.setdefault(p["company_id"], {"company_id": p["company_id"], "name": p["name"],
                                               "ticker": p.get("ticker"), "intent_score": p["intent_score"]})
        ranked = sorted(rows.values(), key=lambda r: -r["intent_score"])[:4]
        for r in ranked:
            r["strength"] = _strength(r["intent_score"])
        competitive.append({
            "initiative_id": iid,
            "theme": _theme_name(iid, s["initiative_name"]),
            "scope": peers[0].get("scope", "sector"),
            "rows": ranked,
            "leader_id": ranked[0]["company_id"],
        })
        if len(competitive) >= 8:            # fills the two-row tile with bars, not spacing
            break

    # Featured opportunities
    featured = []
    for s in signals[:limit_featured]:
        reasons = (s.get("score_breakdown") or {}).get("reasons", [])
        featured.append({
            "company_id": s["company_id"],
            "company_name": s["company_name"],
            "company_region": s.get("company_region"),
            "initiative_id": s["initiative_id"],
            "theme": _theme_name(s["initiative_id"], s["initiative_name"]),
            "it_offering": s.get("it_offering"),
            "intent_score": s["intent_score"],
            "strength": _strength(s["intent_score"]),
            "priority": _priority(s["intent_score"]),
            "points_explained": (s.get("score_breakdown") or {}).get("points_explained"),
            "timing_window": s.get("timing_window"),
            "timing_estimate": s.get("timing_estimate"),
            "triggers": s.get("source_types") or [],
            "event_count": s.get("event_count"),
            "document_count": s.get("document_count"),
            "why_now": reasons[:3],
        })

    # Accounts with the most opportunity
    by_company = defaultdict(list)
    for s in signals:
        by_company[s["company_id"]].append(s)
    accounts = []
    for cid, group in by_company.items():
        best = max(group, key=lambda s: s["intent_score"])
        accounts.append({
            "company_id": cid,
            "company_name": best["company_name"],
            "ticker": best.get("company_ticker"),
            "region": best.get("company_region"),
            "opportunities": len(group),
            "high_count": sum(1 for s in group if s["intent_score"] >= HIGH),
            "best_score": best["intent_score"],
            "strength": _strength(best["intent_score"]),
            "themes": [_theme_name(s["initiative_id"], s["initiative_name"])
                       for s in sorted(group, key=lambda x: -x["intent_score"])[:3]],
        })
    accounts.sort(key=lambda a: (-a["high_count"], -a["opportunities"], -a["best_score"]))

    # Where the evidence comes from
    cat_counts = defaultdict(int)
    for s in signals:
        cat_counts[s.get("category_name") or "Other"] += 1
    total = sum(cat_counts.values()) or 1
    breakdown = sorted(
        [{"label": k, "count": v, "share": round(100 * v / total)} for k, v in cat_counts.items()],
        key=lambda d: -d["count"])

    # Key market signals: the newest documents, tagged by what kind of move they are
    ev_q = (db.query(Event).options(joinedload(Event.raw_document))
            .join(Company, Company.id == Event.company_id)
            .filter(Event.occurred_at <= end_dt))
    if start_dt is not None:
        ev_q = ev_q.filter(Event.occurred_at >= start_dt)
    if sector_arg:
        ev_q = ev_q.filter(Company.sector == sector_arg)
    if region:
        ev_q = ev_q.filter(Company.region == region)
    # Job postings are hiring signals, not market news - they belong on the
    # company page, not in "Key Market Signals".
    ev_q = ev_q.filter(Event.source_type != "career_pages")
    seen, seen_titles, market = set(), set(), []
    for e in sorted(ev_q.all(), key=lambda e: (e.raw_document.filing_date if e.raw_document and e.raw_document.filing_date
                                               else e.occurred_at), reverse=True):
        key = e.raw_doc_id or e.source_url
        if key in seen:
            continue
        seen.add(key)
        doc = e.raw_document
        # The same story arrives from several feeds (Exa and Google News both
        # carry one press release), so dedupe on the headline as well.
        title = _clean_title((doc.title if doc else None) or e.title)
        norm = "".join(ch for ch in title.lower() if ch.isalnum() or ch == " ")[:70].strip()
        if norm in seen_titles:
            continue
        seen_titles.add(norm)
        market.append({
            "title": title,
            "company_id": e.company_id,
            "company_name": e.company.name if e.company else None,
            "url": e.source_url,
            "date": ((doc.filing_date if doc and doc.filing_date else e.occurred_at).isoformat()),
            "tag": _MARKET_TAGS.get(e.category_id, "Signal"),
            "initiative_id": e.initiative_id,
            "image": ((doc.metadata_json or {}).get("image_url") if doc else None),
        })
        if len(market) >= 6:
            break

    filters = {
        "themes": [{"id": t["initiative_id"], "label": t["name"], "count": t["opportunities"]}
                   for t in themes],
        "priority": [{"level": lvl, "count": sum(1 for s in signals if _priority(s["intent_score"]) == lvl)}
                     for lvl in ("High", "Medium", "Low")],
        "timing": [{"window": w, "count": sum(1 for s in signals if s.get("timing_window") == w)}
                   for w in ("0-3mo", "3-6mo", "6-12mo")],
    }

    # Insight and next steps: statements of fact assembled from the above.
    lead = themes[0] if themes else None
    insight = None
    if lead:
        pursuing = lead["companies"]
        universe = len(by_company) or 1
        insight = {
            "headline": f"{lead['name']} is the widest opening in this window",
            "detail": f"{pursuing} of {universe} companies with any activity are pursuing it "
                      f"({round(100 * pursuing / universe)}%), and {lead['high_count']} of those "
                      f"rate High.",
            "bullets": [f"Sell into it with: {lead['it_offering']}." if lead.get("it_offering") else None,
                        f"{filters['timing'][0]['count']} opportunities look 0-3 months out - active hiring "
                        f"against a disclosed initiative." if filters["timing"][0]["count"] else None,
                        f"{lead['opportunities']} opportunities sit in this theme across "
                        f"{lead['companies']} companies."],
            "target_accounts": [{"id": a["company_id"], "name": a["company_name"]} for a in accounts[:5]],
        }
        insight["bullets"] = [b for b in insight["bullets"] if b]

    next_steps = []
    if accounts:
        top = accounts[0]
        next_steps.append({"title": f"Start with {top['company_name']}",
                           "detail": f"{top['opportunities']} opportunities, best score {top['best_score']}."})
    near = [f for f in featured if f.get("timing_window") == "0-3mo"]
    if near:
        next_steps.append({"title": f"{len(near)} featured opportunities are 0-3 months out",
                           "detail": "Disclosure plus active hiring - the shortest path to an RFP."})
    if themes:
        next_steps.append({"title": f"Lead with {themes[0]['name']}",
                           "detail": f"The widest theme: {themes[0]['companies']} companies, "
                                     f"{themes[0]['high_count']} of them high priority."})

    # ---- People to Tap -----------------------------------------------------
    # The leaders on file for the top opportunities, one row per person: a
    # company with two opportunities used to list the same two people twice.
    # Each person carries every opportunity they are relevant to and why
    # (their title covers it, or they are a senior technology leader).
    #
    # Companies with nobody on file are left out rather than shown as empty
    # rows; if they were never searched, a background task searches them after
    # this response is sent, so they appear on a later load.
    from ..people.leaders import current_leaders, match_leaders, missing_ids, search_missing
    from ..models.schema import RawDocument
    PEOPLE_POOL = 20
    pool = signals[:PEOPLE_POOL]
    pool_ids = {x["company_id"] for x in pool}
    on_file = current_leaders(db, pool_ids)

    by_person, order = {}, 0
    for x in pool:
        theme = _theme_name(x["initiative_id"], x["initiative_name"])
        opp = {"initiative_id": x["initiative_id"], "theme": theme, "strength": x["strength"]}
        for m in match_leaders(on_file.get(x["company_id"], []), x["initiative_id"]):
            row = by_person.get(m["id"])
            if row is None:
                by_person[m["id"]] = {**m, "company_name": x["company_name"], "opportunities": [opp], "rank": order}
                continue
            if all(o["initiative_id"] != opp["initiative_id"] for o in row["opportunities"]):
                row["opportunities"].append(opp)
            if row["match"] == "general" and m["match"] == "specific":
                row["match"], row["reason"] = "specific", m["reason"]
        order += 1
    people = sorted(by_person.values(),
                    key=lambda r: (r["rank"], r["match"] != "specific", -(r.get("seniority") or 1)))[:30]

    pending = missing_ids(db, pool_ids)
    if pending and background_tasks is not None:
        background_tasks.add_task(search_missing, pending)

    # ---- Recommended targets -----------------------------------------------
    # Which opportunities to go after and why, assembled from the figures on
    # this page - nothing inferred, nothing model-written. Each play appears
    # only when the data supports it.
    def _acct(x):
        return {"company_id": x["company_id"], "company_name": x["company_name"],
                "theme": _theme_name(x["initiative_id"], x["initiative_name"]),
                "strength": x["strength"], "points_explained": x.get("points_explained")}

    recommendations = []
    # 1. Engage now: the company has disclosed it AND is hiring for it.
    ready = [x for x in signals if x.get("timing_window") == "0-3mo" and x["intent_score"] >= MEDIUM]
    if ready:
        recommendations.append({
            "kind": "engage_now", "title": "Engage now",
            "detail": f"{len(ready)} {'opportunity has' if len(ready) == 1 else 'opportunities have'} both a company "
                      f"disclosure and active hiring - the pattern that usually comes just before vendor selection.",
            "accounts": [_acct(x) for x in ready[:4]]})
    # 2. Lead theme: the initiative with the most Medium-or-High accounts.
    strong = defaultdict(list)
    for x in signals:
        if x["intent_score"] >= MEDIUM and taxonomy_manager.get_initiative(x["initiative_id"]):
            strong[x["initiative_id"]].append(x)
    if strong:
        iid, group = max(strong.items(), key=lambda kv: (len(kv[1]), sum(g["intent_score"] for g in kv[1])))
        init = taxonomy_manager.get_initiative(iid)
        highs = sum(1 for g in group if g["intent_score"] >= HIGH)
        recommendations.append({
            "kind": "lead_theme", "title": f"Lead with {init.name}",
            "detail": f"{len(group)} accounts rate Medium or High on it ({highs} High) - the widest strong opening "
                      f"in this window.",
            "offering": init.it_offering,
            "accounts": [_acct(x) for x in group[:4]]})
    # 3. Moving up: an existing opportunity climbed a strength band.
    movers = [r for r in rising if not r["is_new"] and r.get("previous_strength")
              and r["previous_strength"] != r["strength"]]
    if movers:
        recommendations.append({
            "kind": "momentum", "title": "Moving up",
            "detail": f"{len(movers)} {'opportunity' if len(movers) == 1 else 'opportunities'} climbed a strength band "
                      f"since the previous window - interest is building.",
            "accounts": [{"company_id": r["company_id"], "company_name": r["company_name"], "theme": r["theme"],
                          "strength": r["strength"], "from": r["previous_strength"]} for r in movers[:4]]})
    # 4. Peer-pressure play: a High leader, with peers trailing it.
    for g in competitive:
        lead = g["rows"][0]
        trailing = [r for r in g["rows"][1:] if r["intent_score"] < HIGH]
        if lead["intent_score"] >= HIGH and trailing:
            recommendations.append({
                "kind": "peer_pressure", "title": f"Peer-pressure play: {g['theme']}",
                "detail": f"{lead['name']} rates High on this; {len(trailing)} industry "
                          f"{'peer trails' if len(trailing) == 1 else 'peers trail'} it - pitch them on catching up.",
                "leader": {"company_id": lead["company_id"], "name": lead["name"]},
                "accounts": [{"company_id": r["company_id"], "company_name": r["name"], "theme": g["theme"],
                              "strength": r["strength"]} for r in trailing[:4]]})
            break

    # ---- AI narration -----------------------------------------------------
    # Same contract as the Industry tiles: we write true statements, Groq only
    # turns them into prose, and every number and name it uses is checked
    # against what we gave it.
    #
    # This used to narrate three things - the lead theme ("Radar Insight"),
    # the top themes, and the top accounts. The first two had no tile left to
    # render them after Themes and Radar Insight were cut, which meant Groq
    # was still being charged for prose nobody could see. The one narration
    # kept moves onto Featured Opportunities - the tile it should have
    # described all along, since Featured is the actual ranked list of
    # company+initiative pairs, and "Voya Financial leads the top accounts"
    # said less about *why* than "Voya Financial - AI, GenAI & ML: evidence
    # score 88, driven by 8 news articles and 7 job postings" does.
    _SOURCE_LABEL = {"sec_edgar": "SEC filings", "earnings_deck": "earnings decks",
                      "ir_press": "IR announcements", "career_pages": "hiring activity",
                      "exa_news": "news coverage", "news_rss": "news coverage"}
    window_label = describe_window(start_dt, end_dt).get("label", "this window")
    jobs, keys = [], []

    if featured:
        st = []
        for f in featured[:3]:
            # The on-screen form "2 filings (6)" puts the points in brackets;
            # handed that, the model wrote "six filing points". Plain words
            # only: "8 points from 2 filings and 2 news stories".
            parts = (f.get("points_explained") or "").split(" = ", 1)
            evidence = (re.sub(r"\s*\(\d+\)", "", parts[1]).replace(" + ", ", ") if len(parts) == 2 else "")
            evidence = re.sub(r", ([^,]+)$", r" and \1", evidence)
            st.append(f"{f['company_name']} - {f['theme']}: {f['strength']} strength, "
                      f"{f['intent_score']} points from {evidence or 'the available evidence'}; "
                      f"timing window {f['timing_window']}.")
        keys.append("featured")
        # Shown whenever the model is unavailable (e.g. the daily Groq
        # allowance is spent): the first statement is already a readable,
        # true sentence, so use it rather than a terser one.
        jobs.append(("Featured opportunities", {"window": window_label, "statements": st},
                     st[0].replace(" - ", " leads on ", 1)))

    narrated = dict(zip(keys, summarize_many(jobs)))

    return {
        "window": describe_window(start_dt, end_dt),
        "region": region or "All regions",
        "totals": {
            "opportunities": len(signals),
            "high_priority": sum(1 for s in signals if s["intent_score"] >= HIGH),
            "medium": sum(1 for s in signals if MEDIUM <= s["intent_score"] < HIGH),
            "emerging": sum(1 for s in signals if s["intent_score"] < MEDIUM),
            "companies": len(by_company),
            "avg_score": avg,
            "avg_strength": _strength(avg),
        },
        "comparison": comparison,
        "rising": rising,
        "competitive": competitive,
        "people": people,
        "people_pending": len(pending),
        "recommendations": recommendations,
        "themes": themes,
        "featured": featured,
        "accounts": accounts[:8],
        "breakdown": breakdown,
        "market_signals": market,
        "filters": filters,
        "insight": insight,
        "featured_summary": narrated.get("featured"),
        "next_steps": next_steps,
    }


_SOURCE_NAMES = {"sec_edgar": "SEC filing", "earnings_deck": "Earnings deck", "ir_press": "IR release",
                 "career_pages": "Job posting", "exa_news": "News story", "news_rss": "News story",
                 "patents": "Patent"}


@router.get("/opportunities/evidence")
def opportunity_evidence(
    company_id: str,
    initiative_id: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Every document behind one opportunity's Strength, with the points each
    earned - so a position on the page can be traced to its sources. Counted
    exactly as the score counts them: each document once, at its strongest
    sentence, within the same window.
    """
    from ..models.schema import RawDocument
    from .tile_evidence import clean_headline, publisher_of
    start_dt, end_dt = resolve_window(start, end)
    q = (db.query(Event, RawDocument).outerjoin(RawDocument, RawDocument.id == Event.raw_doc_id)
         .filter(Event.company_id == company_id, Event.initiative_id == initiative_id,
                 Event.occurred_at <= end_dt))
    if start_dt is not None:
        q = q.filter(Event.occurred_at >= start_dt)
    best = {}
    for ev, doc in q.all():
        key = ev.raw_doc_id or ev.source_url or ev.id
        if key not in best or (ev.confidence or 0) > (best[key][0].confidence or 0):
            best[key] = (ev, doc)

    documents = []
    for ev, doc in best.values():
        raw_title = doc.title if doc else None
        documents.append({
            "title": clean_headline(raw_title) or clean_headline(ev.title) or (raw_title or ev.title),
            "publisher": publisher_of(raw_title),
            "url": ev.source_url,
            "source_type": ev.source_type,
            "source_label": _SOURCE_NAMES.get(ev.source_type, ev.source_type),
            "points": POINTS.get(ev.source_type, 1),
            "date": (doc.filing_date if doc and doc.filing_date else ev.occurred_at).isoformat(),
            "quote": " ".join((ev.quote_text or "").split())[:260],
        })
    documents.sort(key=lambda d: d["date"], reverse=True)
    documents.sort(key=lambda d: -d["points"])
    total = sum(d["points"] for d in documents)
    company = db.get(Company, company_id)
    init = taxonomy_manager.get_initiative(initiative_id)
    return {
        "company_id": company_id,
        "company_name": company.name if company else None,
        "initiative_id": initiative_id,
        "theme": init.name if init else initiative_id,
        "points": total,
        "strength": strength_of(total),
        "window": describe_window(start_dt, end_dt),
        "documents": documents,
    }
