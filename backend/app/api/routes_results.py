"""
Recently reported results: which BFSI companies have published earnings in the
selected window.

Most of these documents never became Events - a results release rarely names an
IT initiative - so they were stored but invisible on the dashboard. This reads
them straight from RawDocument.

Precision matters more than recall here. A title merely mentioning a stock
("Svenska Handelsbanken stock trades steadily") is not a results release, so a
document qualifies only as a periodic filing / earnings presentation, or when its
title uses unambiguous results language.
"""
import re
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db.database import get_db
from ..models.schema import Company, RawDocument
from .date_range import describe_window, resolve_window

router = APIRouter(prefix="", tags=["Results"])

# Document types that are results by definition.
_FILING_KINDS = {
    "8-K-EX99": "Earnings presentation",
    "10-Q": "Quarterly report (10-Q)",
    "10-K": "Annual report (10-K)",
    "20-F": "Annual report (20-F)",
}

_PERIOD = r"(?:q[1-4]|[1-4]q|first|second|third|fourth|h[12]|[12]h|half[- ]year|half[- ]yearly|interim|annual|full[- ]year|fy\s?\d{2,4}|quarterly|nine[- ]months?|9m)"
_RESULTS_TITLE = re.compile(
    r"|".join([
        rf"\b{_PERIOD}\b[^.|]{{0,40}}\b(?:results|earnings|profit|net income)\b",
        r"\bearnings (?:call|release|report|rise|rises|jump|jumps|fall|falls|drop|drops|beat|beats|miss|misses|growth|surge|surges|climb|climbs)\b",
        r"\b(?:net profit|net income|profit after tax|pat)\b[^.|]{0,40}\b(?:rise|rises|rose|jump|jumps|fall|falls|fell|drop|drops|up|down|grew|grows|surge|surges|declin\w*|increas\w*)\b",
        r"\breports?\s+(?:record\s+)?(?:q[1-4]|quarterly|annual|half[- ]year|net|profit|earnings|results)\b",
        # "Allstate Reports Excellent Operating Results" - adjectives in between.
        r"\breports?\b[^.|]{0,35}\b(?:results|earnings)\b",
        r"\b(?:delivers?|posts?|announces?)\b[^.|]{0,40}\b(?:earnings|results|profit)\b",
        r"\b\d+(?:\.\d+)?\s?%\s+(?:earnings|profit|net income|revenue)\s+(?:rise|jump|growth|increase|fall|drop|decline)\b",
    ]),
    re.I,
)

# The % must sit directly against its verb. "higher" is deliberately absent: in
# "profit falls 12% on higher provisions" it describes costs, not the result.
_UP = re.compile(r"(\d+(?:\.\d+)?)\s?%\s*(?:\w+\s)?\b(?:rise|rises|jump|jumps|growth|increase|gain|surge|climb)\b"
                 r"|\b(?:up|rises?|rose|jumps?|grew|gains?|surges?|climbs?)\s+(?:by\s+)?(\d+(?:\.\d+)?)\s?%", re.I)
_DOWN = re.compile(r"(\d+(?:\.\d+)?)\s?%\s*(?:\w+\s)?\b(?:fall|falls|drop|drops|decline|slump|plunge)\b"
                   r"|\b(?:down|falls?|fell|drops?|declin\w*|slump\w*|plunge\w*)\s+(?:by\s+)?(\d+(?:\.\d+)?)\s?%", re.I)


def _delta(title: str) -> Optional[float]:
    """A signed % stated in the headline itself. Never inferred."""
    up, down = _UP.search(title or ""), _DOWN.search(title or "")
    if up and not down:
        return float(up.group(1) or up.group(2))
    if down and not up:
        return -float(down.group(1) or down.group(2))
    return None


def _clean_title(title: str) -> str:
    t = (title or "").strip()
    if t.startswith("[") and "]" in t[:62]:
        pub = t[1:t.index("]")]
        t = t[t.index("]") + 1:].strip()
        for sep in (" - ", " | ", " – ", " — "):
            if t.lower().endswith((sep + pub).lower()):
                t = t[: -len(sep + pub)].strip()
    return t


def classify(doc: RawDocument) -> Optional[str]:
    if doc.doc_type in _FILING_KINDS:
        return _FILING_KINDS[doc.doc_type]
    if _RESULTS_TITLE.search(doc.title or ""):
        return {"ir_press": "Results release", "news_rss": "Results coverage",
                "exa_news": "Results coverage"}.get(doc.source_type, "Results coverage")
    return None


# Headline preference when a company has several results documents: a written
# release or article reads better than a bare filing name.
_KIND_RANK = {"Results release": 0, "Results coverage": 1, "Earnings presentation": 2,
              "Quarterly report (10-Q)": 3, "Annual report (10-K)": 4, "Annual report (20-F)": 4}


@router.get("/results/recent")
def recent_results(
    start: Optional[str] = None,
    end: Optional[str] = None,
    sector: str = "BFSI",
    region: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """One entry per company that reported results in the window, newest first."""
    start_dt, end_dt = resolve_window(start, end)
    q = (db.query(RawDocument, Company)
         .join(Company, Company.id == RawDocument.company_id)
         .filter(RawDocument.filing_date <= end_dt))
    if start_dt is not None:
        q = q.filter(RawDocument.filing_date >= start_dt)
    if sector and sector.lower() != "all":
        q = q.filter(Company.sector == sector)
    if region and region.lower() != "all":
        q = q.filter(Company.region == region)

    per_company = defaultdict(list)
    for doc, co in q.all():
        kind = classify(doc)
        if kind:
            per_company[co.id].append((doc, co, kind))

    items = []
    for docs in per_company.values():
        latest = max(d.filing_date for d, _, _ in docs)
        # Headline: best-ranked kind, then newest.
        doc, co, kind = sorted(docs, key=lambda x: (_KIND_RANK.get(x[2], 9), -x[0].filing_date.timestamp()))[0]
        title = _clean_title(doc.title)
        if kind in ("Quarterly report (10-Q)", "Annual report (10-K)", "Annual report (20-F)") or not title:
            title = f"{co.name} filed its {kind.lower()}"
        pub = None
        if (doc.title or "").startswith("[") and "]" in doc.title[:62]:
            pub = doc.title[1:doc.title.index("]")]
        image = next(((d.metadata_json or {}).get("image_url") for d, _, _ in docs
                      if (d.metadata_json or {}).get("image_url")), None)
        items.append({
            "company_id": co.id,
            "company_name": co.name,
            "ticker": co.ticker,
            "region": co.region,
            "country": co.country,
            "industry": co.industry,
            "reported_at": latest.isoformat(),
            "headline": title,
            "publisher": pub,
            "url": doc.url,
            "kind": kind,
            "delta_pct": _delta(doc.title or ""),
            "documents": len(docs),
            "kinds": sorted({k for _, _, k in docs}, key=lambda k: _KIND_RANK.get(k, 9)),
            "image": image,
        })

    items.sort(key=lambda i: i["reported_at"], reverse=True)
    return {
        "window": describe_window(start_dt, end_dt),
        "total_companies": len(items),
        "items": items[:limit],
    }
