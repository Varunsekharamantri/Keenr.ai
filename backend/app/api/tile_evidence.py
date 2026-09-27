"""
Evidence behind a tile: the actual articles, filings and releases its numbers
came from, each with a link to the source.

Every tile on the dashboard now carries two things - a short analyst-style
summary, and the documents that support it. The summary is written by Groq from
statements we compose here; the evidence list is what "View Evidence" opens, so
any claim on screen can be traced to the page it came from in one click.

The same helper serves every tile because the shape is always the same: pick the
events that belong to this tile, keep one per company, and return a readable
headline with its URL.
"""
import re
from typing import List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models.schema import Company, Event, RawDocument
from ..models.taxonomy import taxonomy_manager

# A filing sometimes carries a machine filename as its title
# ("tm261348-1_nonfiling - none - 51.6367224s"). A usable headline reads like
# prose: mostly real words, no long alphanumeric blobs.
_JUNK_WORD = re.compile(r"^(?=.*\d)[A-Za-z0-9._-]{13,}$")


def clean_headline(raw: Optional[str]) -> Optional[str]:
    h = (raw or "").strip()
    if h.startswith("[") and "]" in h[:62]:
        publisher = h[1:h.index("]")]
        h = h[h.index("]") + 1:].strip()
        for sep in (" - ", " | ", " – ", " — "):
            if h.lower().endswith((sep + publisher).lower()):
                h = h[: -len(sep + publisher)].strip()
    # "News for <company>" is what the adapters store when a result has no
    # title - a placeholder, not a headline.
    if h.lower().startswith("news for "):
        return None
    words = [w for w in re.split(r"[\s\-_]+", h) if w]
    wordy = sum(1 for w in words if w.isalpha() and len(w) > 2)
    if len(h) < 15 or wordy < 4 or any(_JUNK_WORD.match(w) for w in words):
        return None
    return h


def publisher_of(raw: Optional[str]) -> Optional[str]:
    h = (raw or "").strip()
    if h.startswith("[") and "]" in h[:62]:
        return h[1:h.index("]")]
    return None


def collect_evidence(
    db: Session,
    start_dt,
    end_dt,
    sector: Optional[str],
    region: Optional[str],
    *,
    initiative_id: Optional[str] = None,
    initiative_ids: Optional[List[str]] = None,
    category_id: Optional[str] = None,
    source_types: Optional[List[str]] = None,
    exclude_sources: Optional[List[str]] = None,
    require_spend: bool = False,
    or_spend: bool = False,
    keep=None,
    order: str = "confidence",
    limit: int = 6,
    scan: int = 120,
) -> dict:
    """
    Documents supporting one tile, one per company.

    Returns {"items": [...], "vendors": [...], "company_count": n}. `items` is
    capped at `limit` - it is the examples - but `company_count` counts every
    matching company, so a summary never states the size of its example list
    as a total ("Six companies..." when there were thirty).

    order="newest" lists the most recent documents first, the same order as
    the tile's own list, so the examples a summary names are the ones at the
    top of what the reader sees. or_spend=True widens the topic filters to
    "...or any document with a stated spend" (the Budgets & Deals rule), and
    `keep(event, doc)` applies a tile's own filter, such as a leadership move.
    """
    q = (db.query(Event, Company, RawDocument)
         .join(Company, Company.id == Event.company_id)
         .outerjoin(RawDocument, RawDocument.id == Event.raw_doc_id)
         .filter(Event.occurred_at <= end_dt))
    if start_dt is not None:
        q = q.filter(Event.occurred_at >= start_dt)
    if sector and sector.lower() != "all":
        q = q.filter(Company.sector == sector)
    if region:
        q = q.filter(Company.region == region)
    topic = []
    if initiative_id:
        topic.append(Event.initiative_id == initiative_id)
    if initiative_ids:
        topic.append(Event.initiative_id.in_(initiative_ids))
    if category_id:
        topic.append(Event.category_id == category_id)
    if or_spend:
        q = q.filter(or_(*topic, Event.spend_amount.isnot(None)))
    else:
        for cond in topic:
            q = q.filter(cond)
    if source_types:
        q = q.filter(Event.source_type.in_(source_types))
    for src in (exclude_sources or []):
        q = q.filter(Event.source_type != src)
    if require_spend:
        q = q.filter(Event.spend_amount.isnot(None))

    if order == "newest":
        q = q.order_by(func.coalesce(RawDocument.filing_date, Event.occurred_at).desc(), Event.confidence.desc())
    else:
        q = q.order_by(Event.confidence.desc(), Event.occurred_at.desc())

    items, vendors, seen, companies = [], {}, set(), set()
    for n_row, (ev, co, doc) in enumerate(q.all()):
        if keep is not None and not keep(ev, doc):
            continue
        companies.add(co.id)
        if n_row >= scan and len(items) >= limit:
            continue                      # still counting companies, done with examples
        for ent in (ev.key_entities or []):
            if isinstance(ent, str) and ent.strip():
                vendors[ent.strip()] = vendors.get(ent.strip(), 0) + 1
        if co.id in seen or len(items) >= limit:
            continue
        headline = clean_headline(doc.title if doc else None) or clean_headline(ev.title)
        if not headline:
            continue
        seen.add(co.id)
        init = taxonomy_manager.get_initiative(ev.initiative_id)
        items.append({
            "company_id": co.id,
            "company": co.name,
            "headline": headline,
            "url": ev.source_url,
            "publisher": publisher_of(doc.title if doc else None),
            "date": ((doc.filing_date if doc and doc.filing_date else ev.occurred_at).isoformat()),
            "source_type": ev.source_type,
            "initiative": init.name if init else ev.initiative_name,
            "initiative_id": ev.initiative_id,
            "spend_amount": ev.spend_amount,
            # What the tile lists render besides the headline.
            "quote": " ".join((ev.quote_text or "").split())[:400],
            "doc_id": ev.raw_doc_id,
            "image": ((doc.metadata_json or {}).get("image_url") if doc else None),
        })
    return {"items": items, "vendors": [v for v, _ in sorted(vendors.items(), key=lambda kv: -kv[1])[:4]],
            "company_count": len(companies)}


def as_statements(evidence: dict, label: str = "Example") -> List[str]:
    """Turn evidence into the sentences the summariser is allowed to use."""
    out = []
    if evidence.get("vendors"):
        # Spelled out, because the model wrote "34 companies are in a vendor
        # partnership, with firms such as Google Cloud, AWS ... involved" - those
        # are the suppliers, not the banks and insurers being counted.
        out.append("Technology suppliers mentioned in these stories (these are vendors "
                   "selling to the companies, not companies being counted): "
                   + ", ".join(evidence["vendors"]) + ".")
    for it in evidence.get("items", [])[:3]:
        out.append(f'{label} - {it["company"]}: "{it["headline"][:110]}"')
    return out
