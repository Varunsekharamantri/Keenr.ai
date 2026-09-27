"""
Every Industry tile: an analyst-style summary plus the sources behind it.

Each tile returns the same shape, so the page renders them uniformly:

    {"summary": {"text": ..., "source": "ai" | "computed"},
     "evidence": [{company, headline, url, publisher, date, ...}],
     "stat": "17 of 23 companies"}

We compose the true statements (numbers and meaning are ours); Groq only turns
them into prose, and never sees a fact we did not give it. The evidence list is
what "View Evidence" opens - every claim on a tile can be traced to the page it
came from in one click.
"""
import re
from typing import Optional

from ..ai.summarizer import summarize_many
from ..models.taxonomy import taxonomy_manager
from .tile_evidence import as_statements, collect_evidence

NEWS_SOURCES = ["exa_news", "news_rss", "ir_press"]

# An appointment needs a verb that moves someone; a job ad or a passing mention
# of a CTO is not a leadership move.
_MOVE = re.compile(
    r"\b(appoint\w*|name[sd]\b|hire[sd]?\b|joins?\b|joined|promot\w*|elevat\w*|steps? down"
    r"|resign\w*|retir\w*|succeed\w*|successor|takes? over|to (lead|head)|replac\w*"
    r"|new (group )?(ceo|cio|cto|ciso|cdo|coo|cfo|chief|head|president|chair))\b", re.I)


def _tile(summary, evidence, stat=None, extra=None):
    out = {"summary": summary, "evidence": evidence.get("items", []), "stat": stat}
    if extra:
        out.update(extra)
    return out


def build_industry_tiles(db, start_dt, end_dt, sector, region, active, tech, business,
                         window_label: str, results_items: Optional[list] = None) -> dict:
    """Facts, evidence and narration for all seven Industry tiles."""
    jobs, keys, tiles = [], [], {}
    ev_cache = {}

    def add(key, kind, statements, fallback, evidence, stat=None, extra=None):
        ev_cache[key] = (evidence, stat, extra)
        if statements:
            keys.append(key)
            jobs.append((kind, {"window": window_label, "statements": statements}, fallback))
        else:
            tiles[key] = _tile({"text": fallback, "source": "computed"}, evidence, stat, extra)

    # ---- Technology trends -------------------------------------------------
    lead = tech[0] if tech else None
    second = tech[1] if len(tech) > 1 else None
    if lead and lead["companies"]:
        ev = collect_evidence(db, start_dt, end_dt, sector, region,
                              initiative_id=lead["id"], exclude_sources=["career_pages"])
        st = [f"{lead['name']} is the most common technology initiative: {lead['companies']} of "
              f"the {active} companies with activity are pursuing it ({lead['share']}%)."]
        if second and second["companies"]:
            st.append(f"{second['name']} is second, pursued by {second['share']}% of those companies.")
        if lead.get("it_offering"):
            st.append(f"It fits vendors selling {lead['it_offering']}.")
        st += as_statements(ev)
        add("tech", "Technology trends", st,
            f"{lead['name']} leads: {lead['companies']} of {active} companies ({lead['share']}%).",
            ev, f"{lead['companies']} of {active} companies")
    else:
        add("tech", "Technology trends", [], "No technology activity in this window.", {"items": []})

    # ---- Business trends ---------------------------------------------------
    biz = sorted([b for b in business if b["companies"]], key=lambda b: -b["companies"])
    blead = biz[0] if biz else None
    deals_row = next((b for b in business if b["id"] == "vendor_partnership_rfp"), None)
    if blead:
        ev = collect_evidence(db, start_dt, end_dt, sector, region,
                              initiative_id=blead["id"], exclude_sources=["career_pages"])
        st = [f"{blead['name']} is the most common strategic move, seen at {blead['share']}% of "
              f"the {active} companies with activity."]
        if deals_row and deals_row["companies"]:
            st.append(f"{deals_row['companies']} companies are in a vendor partnership or a major "
                      f"RFP, the clearest sign of an open buying window.")
        st += as_statements(ev, "Illustrative case")
        add("business", "Business trends", st,
            f"{blead['name']} leads at {blead['share']}% of active companies.",
            ev, f"{blead['companies']} of {active} companies")
    else:
        add("business", "Business trends", [], "No strategic moves in this window.", {"items": []})

    # ---- Regulatory --------------------------------------------------------
    ev = collect_evidence(db, start_dt, end_dt, sector, region, category_id="regulatory_compliance")
    n = len(ev["items"])
    if n:
        st = [f"{n} {'company has' if n == 1 else 'companies have'} disclosed a regulatory, privacy "
              f"or security mandate affecting their technology."] + as_statements(ev, "Case")
        add("regulatory", "Regulatory and compliance", st,
            f"{n} regulatory {'signal' if n == 1 else 'signals'} in this window.", ev,
            f"{n} {'company' if n == 1 else 'companies'}")
    else:
        add("regulatory", "Regulatory and compliance", [],
            "No regulatory mandates reported in this window.", ev, "0 companies")

    # ---- Budgets and deals -------------------------------------------------
    ev = collect_evidence(db, start_dt, end_dt, sector, region,
                          initiative_ids=["vendor_partnership_rfp", "capex_it_budget", "ma_integration"])
    n = len(ev["items"])
    if n:
        st = [f"{n} {'company is' if n == 1 else 'companies are'} in a vendor partnership, a major "
              f"RFP, a technology investment or an acquisition."] + as_statements(ev, "Case")
        add("deals", "Budgets and deals", st,
            f"{n} budget or deal {'signal' if n == 1 else 'signals'} in this window.", ev,
            f"{n} {'company' if n == 1 else 'companies'}")
    else:
        add("deals", "Budgets and deals", [], "No budgets or vendor deals disclosed in this window.",
            ev, "0 companies")

    # ---- Leadership --------------------------------------------------------
    raw = collect_evidence(db, start_dt, end_dt, sector, region,
                           initiative_id="executive_leadership_change",
                           exclude_sources=["career_pages"], limit=12)
    raw["items"] = [i for i in raw["items"] if _MOVE.search(i["headline"])][:6]
    n = len(raw["items"])
    if n:
        st = [f"{n} technology leadership {'move' if n == 1 else 'moves'} - a new CIO, CTO, CDO or "
              f"CISO - at companies in this window. A new decision-maker often resets vendor "
              f"relationships."] + as_statements(raw, "Move")
        add("leadership", "Leadership moves", st,
            f"{n} leadership {'move' if n == 1 else 'moves'} in this window.", raw,
            f"{n} {'move' if n == 1 else 'moves'}")
    else:
        add("leadership", "Leadership moves", [], "No leadership moves in this window.", raw, "0 moves")

    # ---- Daily news --------------------------------------------------------
    ev = collect_evidence(db, start_dt, end_dt, sector, region, source_types=NEWS_SOURCES, limit=8)
    n = len(ev["items"])
    if n:
        themes = []
        for it in ev["items"]:
            if it["initiative"] and it["initiative"] not in themes:
                themes.append(it["initiative"])
        st = [f"{n} {'company' if n == 1 else 'companies'} appeared in the news in this window."]
        if themes:
            st.append("The themes covered were: " + ", ".join(themes[:4]) + ".")
        st += as_statements(ev, "Story")
        add("news", "Daily news", st, f"{n} {'story' if n == 1 else 'stories'} in this window.", ev,
            f"{n} {'story' if n == 1 else 'stories'}")
    else:
        add("news", "Daily news", [], "No news in this window.", ev, "0 stories")

    # ---- Recent results ----------------------------------------------------
    results_items = results_items or []
    r_ev = {"items": [{"company_id": r["company_id"], "company": r["company_name"],
                       "headline": r["headline"], "url": r["url"], "publisher": r.get("publisher"),
                       "date": r["reported_at"], "source_type": r["kind"],
                       "initiative": r["kind"], "initiative_id": "_results",
                       "delta_pct": r.get("delta_pct")}
                      for r in results_items[:6]], "vendors": []}
    n = len(results_items)
    if n:
        st = [f"{n} {'company has' if n == 1 else 'companies have'} published results in this window."]
        movers = [r for r in results_items if r.get("delta_pct") is not None][:2]
        for m in movers:
            st.append(f"{m['company_name']} reported {m['headline'][:90]}, a change of "
                      f"{m['delta_pct']}% stated in the headline.")
        st += as_statements(r_ev, "Filing")
        add("results", "Recent results", st,
            f"{n} {'company' if n == 1 else 'companies'} reported in this window.", r_ev,
            f"{n} {'company' if n == 1 else 'companies'}")
    else:
        add("results", "Recent results", [], "No companies reported results in this window.",
            r_ev, "0 companies")

    # One batched, parallel call for every tile that has something to say.
    written = dict(zip(keys, summarize_many(jobs)))
    for key, (evidence, stat, extra) in ev_cache.items():
        if key in written:
            tiles[key] = _tile(written[key], evidence, stat, extra)
    return tiles
