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

# The News tile lists news articles and headlines; IR releases are filings.
NEWS_SOURCES = ["exa_news", "news_rss"]

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

    # Every summary below is built from exactly what its graphic shows - the
    # same measure, filter and order - so the text and the chart can never
    # disagree. (The Tech summary used to quote "% of companies" under a donut
    # of the technology mix; the list tiles quoted the size of their example
    # list as a total.)

    # ---- Technology trends: the donut --------------------------------------
    # The donut is the mix of technology opportunities: each company pursuing
    # an initiative counts once, so the slices sum to 100%. Same rounding as the
    # page (half up), so a slice reading 30% is described as 30%.
    shown = [t for t in tech if t["companies"]]
    pairs = sum(t["companies"] for t in shown)
    mix = lambda t: int(100 * t["companies"] / pairs + 0.5) if pairs else 0
    if shown:
        lead = shown[0]
        ev = collect_evidence(db, start_dt, end_dt, sector, region,
                              initiative_id=lead["id"], exclude_sources=["career_pages"])
        st = [f"Across {pairs} technology signals - each is one company active on one initiative - "
              f"{lead['name']} makes up {mix(lead)}%."]
        if len(shown) > 2:
            st.append(f"{shown[1]['name']} makes up {mix(shown[1])}% and {shown[2]['name']} {mix(shown[2])}%.")
        elif len(shown) == 2:
            st.append(f"{shown[1]['name']} makes up {mix(shown[1])}%.")
        if lead.get("it_offering"):
            st.append(f"{lead['name']} fits vendors selling {lead['it_offering']}.")
        st += as_statements(ev)
        add("tech", "Technology trends", st,
            f"{lead['name']} makes up {mix(lead)}% of {pairs} technology signals.",
            ev, f"{pairs} technology signals")
    else:
        add("tech", "Technology trends", [], "No technology activity in this window.", {"items": []})

    # ---- Business trends: the four bars ------------------------------------
    # Same four bars as the page (business moves by share of active companies,
    # highest first), described with the percentages printed on them.
    bars = [b for b in sorted(business, key=lambda b: -b["share"])[:4] if b["companies"]]
    if bars:
        b1 = bars[0]
        ev = collect_evidence(db, start_dt, end_dt, sector, region,
                              initiative_id=b1["id"], exclude_sources=["career_pages"])
        st = [f"{b1['name']} is the most common strategic move, seen at {b1['share']}% of the "
              f"{active} active companies."]
        others = [f"{b['name']} at {b['share']}%" for b in bars[1:3]]
        if others:
            st.append("Next come " + " and ".join(others) + ".")
        st += as_statements(ev, "Illustrative case")
        add("business", "Business trends", st,
            f"{b1['name']} leads at {b1['share']}% of active companies.",
            ev, f"{b1['companies']} of {active} companies")
    else:
        add("business", "Business trends", [], "No strategic moves in this window.", {"items": []})

    # ---- The four list tiles -----------------------------------------------
    # Real totals (every matching company, not the example list) and the most
    # recent documents first - the order of the tile's own list.
    def list_tile(key, kind, noun_total, statement, ev, empty):
        n = ev["company_count"]
        if n:
            add(key, kind, [statement(n)] + as_statements(ev, "Most recent"),
                f"{n} {noun_total(n)} in this window.", ev,
                f"{n} {'company' if n == 1 else 'companies'}")
        else:
            add(key, kind, [], empty, ev, "0 companies")

    list_tile("regulatory", "Regulatory and compliance",
              lambda n: "company disclosed a regulatory mandate" if n == 1 else "companies disclosed regulatory mandates",
              lambda n: f"{n} {'company has' if n == 1 else 'companies have'} disclosed a regulatory, privacy "
                        f"or security mandate affecting their technology.",
              collect_evidence(db, start_dt, end_dt, sector, region,
                               category_id="regulatory_compliance", order="newest", limit=12),
              "No regulatory mandates reported in this window.")

    # Budgets & Deals, as the tile lists them: spending signals, M&A, or any
    # document with a stated spend.
    list_tile("deals", "Budgets and deals",
              lambda n: "company with a budget or deal" if n == 1 else "companies with budgets or deals",
              lambda n: f"{n} {'company is' if n == 1 else 'companies are'} in a vendor partnership, a major "
                        f"RFP, a stated technology budget or an acquisition.",
              collect_evidence(db, start_dt, end_dt, sector, region, category_id="spending_signals",
                               initiative_ids=["ma_integration"], or_spend=True, order="newest", limit=12),
              "No budgets or vendor deals disclosed in this window.")

    # Leadership, as the tile lists it: an appointment verb in the headline or
    # quote; job postings excluded.
    is_move = lambda ev, doc: _MOVE.search(f"{doc.title if doc else ''} {ev.quote_text or ''}") is not None
    list_tile("leadership", "Leadership moves",
              lambda n: "company with a leadership move" if n == 1 else "companies with leadership moves",
              lambda n: f"{n} {'company has' if n == 1 else 'companies have'} had a technology leadership move - "
                        f"a new CIO, CTO, CDO or CISO. A new decision-maker often resets vendor relationships.",
              collect_evidence(db, start_dt, end_dt, sector, region, initiative_id="executive_leadership_change",
                               exclude_sources=["career_pages"], keep=is_move, order="newest", limit=12),
              "No leadership moves in this window.")

    # News, as the tile lists it: news articles and headlines only.
    news_ev = collect_evidence(db, start_dt, end_dt, sector, region, source_types=NEWS_SOURCES,
                               order="newest", limit=12)
    themes = []
    for it in news_ev["items"][:4]:
        if it["initiative"] and it["initiative"] not in themes:
            themes.append(it["initiative"])
    list_tile("news", "Daily news",
              lambda n: "company in the news" if n == 1 else "companies in the news",
              lambda n: f"{n} {'company' if n == 1 else 'companies'} appeared in the news in this window"
                        + (f"; the latest stories cover {', '.join(themes)}." if themes else "."),
              news_ev, "No news in this window.")

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
