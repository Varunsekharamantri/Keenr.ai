"""
Rule-based intent scoring for a group of Events sharing (company, initiative).

Deliberately transparent rather than an ML model: every component is capped,
recorded in the breakdown, and turned into a human-readable reason so the
dashboard's "Why this signal?" panel can cite exactly what drove the score.
"""
import math
import re
import datetime
from collections import Counter
from typing import Dict, List, Optional, Tuple

# The architecture doc's "filing mention + hiring + news volume" weighting:
# first-party disclosures count most, hiring is strong implementation
# evidence, third-party news is corroborating but noisier.
SOURCE_WEIGHTS: Dict[str, float] = {
    "sec_edgar": 1.0,
    "earnings_deck": 0.9,
    "ir_press": 0.9,
    "career_pages": 0.8,
    "exa_news": 0.6,
    "patents": 0.5,
    "news_rss": 0.4,
}
DEFAULT_SOURCE_WEIGHT = 0.5

# ---- Strength: points per unique document --------------------------------
# The score used to add four weighted parts through an exponential curve (out
# of 100). It ranked sensibly but could not be explained without a table and
# a formula, and two of its parts carried little: freshness was a near-constant
# 20 in the default 30-day view, and spend read revenue and finance figures as
# IT budgets. It is now a count anyone can check by hand: who is speaking, and
# how many separate documents say it.
POINTS: Dict[str, int] = {
    "sec_edgar": 3,       # the company said it itself
    "earnings_deck": 3,
    "ir_press": 3,
    "career_pages": 2,    # the company is hiring for it
    "exa_news": 1,        # the press reported it
    "news_rss": 1,
    "patents": 1,
}
HIGH_POINTS, MEDIUM_POINTS = 6, 3
_POINT_LABEL = {"sec_edgar": ("filing", "filings"), "earnings_deck": ("earnings deck", "earnings decks"),
                "ir_press": ("IR release", "IR releases"), "career_pages": ("job posting", "job postings"),
                "exa_news": ("news story", "news stories"), "news_rss": ("news story", "news stories"),
                "patents": ("patent", "patents")}


def strength_of(points: int) -> str:
    """High 6+, Medium 3-5, Low 1-2."""
    if points >= HIGH_POINTS:
        return "High"
    if points >= MEDIUM_POINTS:
        return "Medium"
    return "Low"


def explain_points(documents: List) -> str:
    """ "8 points = 2 filings (6) + 2 news stories (2)" """
    groups: Dict[tuple, List[int]] = {}          # (singular, plural) -> [documents, points]
    for d in documents:
        g = groups.setdefault(_POINT_LABEL.get(d.source_type, (d.source_type, d.source_type)), [0, 0])
        g[0] += 1
        g[1] += POINTS.get(d.source_type, 1)
    total = sum(pts for _, pts in groups.values())
    parts = [f"{n} {single if n == 1 else plural} ({pts})"
             for (single, plural), (n, pts) in sorted(groups.items(), key=lambda kv: -kv[1][1])]
    return f"{total} {'point' if total == 1 else 'points'} = " + " + ".join(parts)


# Initiatives that are real signals but not opportunities. A new CIO is an
# entry point for outreach - it feeds People to Tap and the Leadership tile -
# but it is not something a vendor sells into, so it is never ranked as an
# opportunity or shown as "rising".
NON_OPPORTUNITY_INITIATIVES = {"executive_leadership_change"}

DISCLOSURE_SOURCES = {"sec_edgar", "earnings_deck", "ir_press"}
HIRING_SOURCES = {"career_pages"}

SOURCE_LABELS = {
    "sec_edgar": "SEC filing mention",
    "earnings_deck": "earnings-deck mention",
    "ir_press": "investor-relations announcement",
    "career_pages": "hiring/job posting",
    "exa_news": "news article",
    "news_rss": "news headline",
    "patents": "patent filing",
}

EVIDENCE_MAX = 40.0
CORROBORATION_MAX = 25.0
RECENCY_MAX = 20.0
SPEND_TIMING_MAX = 15.0

_SPEND_RE = re.compile(
    r"\$?\s*([0-9][0-9,]*\.?[0-9]*)\s*(billion|bn|b|million|mn|mm|m|thousand|k)?",
    re.IGNORECASE,
)
_MULTIPLIERS = {
    "billion": 1e9, "bn": 1e9, "b": 1e9,
    "million": 1e6, "mn": 1e6, "mm": 1e6, "m": 1e6,
    "thousand": 1e3, "k": 1e3,
}


def parse_spend_usd(text: Optional[str]) -> Optional[float]:
    """'$17.5B' -> 1.75e10, '$200M' -> 2e8, '200 million' -> 2e8. None if unparseable."""
    if not text:
        return None
    m = _SPEND_RE.search(text.replace("USD", "$"))
    if not m or not m.group(1):
        return None
    try:
        amount = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    return amount * _MULTIPLIERS.get(unit, 1.0)


def format_spend_usd(value: float) -> str:
    if value >= 1e9:
        return f"${value / 1e9:.1f}B".replace(".0B", "B")
    if value >= 1e6:
        return f"${value / 1e6:.0f}M"
    if value >= 1e3:
        return f"${value / 1e3:.0f}K"
    return f"${value:.0f}"


def _pluralize(n: int, singular: str) -> str:
    return f"{n} {singular}{'' if n == 1 else 's'}"


def score_event_group(events: List, now: Optional[datetime.datetime] = None) -> dict:
    """
    events: Event ORM rows for one (company, initiative). Returns a dict with
    intent_score, breakdown, timing fields, stated spend/timing, and the
    citation event ids — everything the Signal row needs except peers.
    """
    now = now or datetime.datetime.utcnow()

    # --- one piece of proof per document, not per sentence
    # The extractor records every sentence that mentions an initiative, so one
    # Azure article about Voya produced 8 events and one AI job posting 7.
    # Summing events made the score measure how long-winded a source was:
    # Voya scored 88 from three documents, Swiss Re 78 from two. A document
    # now counts once, at its strongest sentence. Spend and timing are still
    # read from every sentence below - a date stated anywhere in the article
    # is still stated.
    best_per_doc: Dict[str, object] = {}
    for e in events:
        key = e.raw_doc_id or e.source_url or e.id
        cur = best_per_doc.get(key)
        if cur is None or (e.confidence or 0) > (cur.confidence or 0):
            best_per_doc[key] = e
    documents = list(best_per_doc.values())
    source_counts = Counter(d.source_type for d in documents)   # documents per source type
    distinct_sources = len(source_counts)

    # --- evidence: saturating weighted sum so volume alone can't max it out
    weighted_sum = sum(
        (d.confidence or 0.85) * SOURCE_WEIGHTS.get(d.source_type, DEFAULT_SOURCE_WEIGHT)
        for d in documents
    )
    evidence = EVIDENCE_MAX * (1 - math.exp(-0.35 * weighted_sum))

    # --- corroboration: independent source types agreeing
    if distinct_sources >= 3:
        corroboration = CORROBORATION_MAX
    elif distinct_sources == 2:
        corroboration = 15.0
    else:
        corroboration = 5.0

    # --- recency: how fresh is the newest evidence
    newest = max(e.occurred_at for e in events)
    oldest = min(e.occurred_at for e in events)
    age_days = max(0, (now - newest).days)
    if age_days <= 30:
        recency = RECENCY_MAX
    elif age_days <= 90:
        recency = 14.0
    elif age_days <= 180:
        recency = 8.0
    else:
        recency = 3.0

    # --- spend & timing: stated budgets and timelines signal a procurement window
    spend_values = [(parse_spend_usd(e.spend_amount), e.spend_amount) for e in events if e.spend_amount]
    spend_values = [(v, raw) for v, raw in spend_values if v]
    max_spend_value, max_spend_raw = max(spend_values, key=lambda x: x[0]) if spend_values else (None, None)
    spend_points = 0.0
    if max_spend_value:
        if max_spend_value >= 1e9:
            spend_points = 10.0
        elif max_spend_value >= 1e8:
            spend_points = 8.0
        else:
            spend_points = 5.0
    timings = [e.timing_horizon.strip() for e in events if e.timing_horizon and e.timing_horizon.strip()]
    timing_points = 5.0 if timings else 0.0
    spend_timing = min(SPEND_TIMING_MAX, spend_points + timing_points)
    stated_timing = Counter(timings).most_common(1)[0][0] if timings else None

    # The score is the points. The four parts above are no longer added into
    # it; they are kept only because the reasons, timing and spend notes below
    # still read them.
    intent_score = sum(POINTS.get(d.source_type, 1) for d in documents)
    strength = strength_of(intent_score)
    points_explained = explain_points(documents)

    # --- timing estimate (rule-based: "announced Q1, hiring Q2 -> RFP Q3")
    has_disclosure = any(s in DISCLOSURE_SOURCES for s in source_counts)
    has_hiring = any(s in HIRING_SOURCES for s in source_counts)
    if has_disclosure and has_hiring:
        timing_window = "0-3mo"
        timing_estimate = (
            "Implementation underway — active hiring against a disclosed initiative; "
            "RFP / vendor selection likely within 0–3 months"
        )
    elif has_disclosure:
        timing_window = "3-6mo"
        timing_estimate = "Announced in disclosures — hiring and vendor evaluation likely in 3–6 months"
    elif has_hiring:
        timing_window = "3-6mo"
        timing_estimate = "Hiring detected without a matching disclosure — capability build-out likely in 3–6 months"
    else:
        timing_window = "6-12mo"
        timing_estimate = "Early signal from news only — watch for filing or hiring confirmation; 6–12 months"

    # --- human reasons for the "Why this signal?" panel
    reasons: List[str] = []
    for source, count in source_counts.most_common():
        reasons.append(_pluralize(count, SOURCE_LABELS.get(source, source.replace("_", " "))))
    if distinct_sources >= 2:
        reasons.append(f"Corroborated by {distinct_sources} independent source types")
    if max_spend_value:
        reasons.append(f"Stated spend {format_spend_usd(max_spend_value)}")
    if stated_timing:
        reasons.append(f"Stated timeline: {stated_timing}")
    if age_days <= 30:
        reasons.append("Fresh evidence within the last 30 days")
    if has_disclosure and has_hiring:
        reasons.append("Disclosure + hiring pattern indicates active implementation")

    # --- citations: best document per source type first, then fill by confidence.
    # Drawn from `documents`, so one article never fills several citation slots.
    by_source: Dict[str, List] = {}
    for d in documents:
        by_source.setdefault(d.source_type, []).append(d)
    citations: List = []
    for source in sorted(by_source, key=lambda s: -SOURCE_WEIGHTS.get(s, DEFAULT_SOURCE_WEIGHT)):
        citations.append(max(by_source[source], key=lambda e: (e.confidence or 0, e.occurred_at)))
    remaining = sorted(
        (d for d in documents if d not in citations),
        key=lambda e: ((e.confidence or 0), e.occurred_at),
        reverse=True,
    )
    citations.extend(remaining)
    top_event_ids = [e.id for e in citations[:5]]

    return {
        "intent_score": intent_score,         # points
        "strength": strength,                 # High / Medium / Low
        "points_explained": points_explained,
        "score_breakdown": {
            "components": {
                "company_statements": sum(POINTS[d.source_type] for d in documents if d.source_type in DISCLOSURE_SOURCES),
                "hiring": sum(POINTS[d.source_type] for d in documents if d.source_type in HIRING_SOURCES),
                "press": sum(POINTS.get(d.source_type, 1) for d in documents
                             if d.source_type not in DISCLOSURE_SOURCES and d.source_type not in HIRING_SOURCES),
            },
            "strength": strength,
            "points_explained": points_explained,
            "source_counts": dict(source_counts),
            "weights_used": {s: SOURCE_WEIGHTS.get(s, DEFAULT_SOURCE_WEIGHT) for s in source_counts},
            "weighted_evidence_sum": round(weighted_sum, 2),
            "newest_evidence_age_days": age_days,
            "reasons": reasons,
        },
        "event_count": len(events),          # sentences - what the extractor recorded
        "document_count": len(documents),     # unique documents - what the score counts
        "source_types": sorted(source_counts),
        "distinct_source_count": distinct_sources,
        "first_seen_at": oldest,
        "last_seen_at": newest,
        "timing_window": timing_window,
        "timing_estimate": timing_estimate,
        "stated_timing": stated_timing,
        "stated_spend": format_spend_usd(max_spend_value) if max_spend_value else (max_spend_raw or None),
        "top_event_ids": top_event_ids,
    }
