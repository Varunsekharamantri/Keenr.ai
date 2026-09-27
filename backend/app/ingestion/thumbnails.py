"""
Article thumbnails from each page's own preview image (og:image / twitter:image).

Measured before building (12 recent documents per source):
  ir_press  6/12   PR Newswire, American Banker and similar publish real images
  exa_news  4/12   low partly because some results are LinkedIn profiles or job boards
  news_rss  0/12   Google News links are redirect wrappers with no preview image
So this is best-effort by design: the UI falls back to category artwork, and a
missing image is recorded so the same URL is not re-fetched on every run.

Stored in RawDocument.metadata_json (no schema change):
  image_url         the preview image, or None
  image_checked_at  ISO timestamp of the attempt
"""
import datetime
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy.orm import Session

from ..models.schema import Company, RawDocument

logger = logging.getLogger("market_signals.thumbnails")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_META = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*'
    r'content=["\']([^"\']+)'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    re.I,
)

# Hosts whose pages never carry a usable article image.
_SKIP_HOSTS = ("news.google.com", "sec.gov", "linkedin.com/in/")

# A site-wide logo or default share card is not a thumbnail for *this* story;
# showing the same generic image on every card is worse than category artwork.
_GENERIC = re.compile(
    r"(logo|favicon|placeholder|default[-_]?(share|og|image)|ogimage\.|og-default|share-default|"
    r"/brand/|apple-touch-icon|blank\.(png|gif)|spacer)",
    re.I,
)


def _extract(html: str, base_url: str) -> Optional[str]:
    m = _META.search(html[:400_000])
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or "").strip().replace("&amp;", "&")
    if not raw or raw.startswith("data:"):
        return None
    url = urljoin(base_url, raw)
    if urlparse(url).scheme not in ("http", "https"):
        return None
    if _GENERIC.search(url):
        return None
    return url


def fetch_preview_image(client: httpx.Client, url: str) -> Optional[str]:
    """Return the page's preview image URL, or None. Never raises."""
    if not url or any(h in url for h in _SKIP_HOSTS):
        return None
    try:
        r = client.get(url)
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
            return None
        return _extract(r.text, str(r.url))
    except Exception as ex:  # network, TLS, decoding - all just mean "no image"
        logger.debug(f"thumbnail fetch failed for {url[:80]}: {ex}")
        return None


def fill_thumbnails(
    db: Session,
    since: Optional[datetime.datetime] = None,
    sector: Optional[str] = "BFSI",
    limit: Optional[int] = None,
    workers: int = 8,
    recheck: bool = False,
    progress_every: int = 50,
) -> dict:
    """
    Attempt a preview image for every document not yet checked.
    Network fetches run in threads; all database writes stay on this thread.
    """
    q = db.query(RawDocument)
    if sector:
        q = q.join(Company, Company.id == RawDocument.company_id).filter(Company.sector == sector)
    if since is not None:
        q = q.filter(RawDocument.filing_date >= since)
    docs = [d for d in q.order_by(RawDocument.filing_date.desc()).all()
            if recheck or "image_checked_at" not in (d.metadata_json or {})]
    docs = [d for d in docs if not any(h in (d.url or "") for h in _SKIP_HOSTS)] + \
           [d for d in docs if any(h in (d.url or "") for h in _SKIP_HOSTS)]
    if limit:
        docs = docs[:limit]

    stats = {"checked": 0, "found": 0, "skipped_host": 0}
    if not docs:
        return stats

    by_id = {d.id: d for d in docs}
    stamp = datetime.datetime.utcnow().isoformat()

    def record(doc_id: str, image: Optional[str]):
        doc = by_id[doc_id]
        meta = dict(doc.metadata_json or {})       # new dict so SQLAlchemy sees the change
        meta["image_url"] = image
        meta["image_checked_at"] = stamp
        doc.metadata_json = meta
        stats["checked"] += 1
        if image:
            stats["found"] += 1

    # Skipped hosts need no request - mark them immediately.
    fetchable = []
    for d in docs:
        if any(h in (d.url or "") for h in _SKIP_HOSTS):
            record(d.id, None)
            stats["skipped_host"] += 1
        else:
            fetchable.append(d)
    db.commit()

    headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=12.0) as client, \
            ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_preview_image, client, d.url): d.id for d in fetchable}
        for n, fut in enumerate(as_completed(futures), 1):
            record(futures[fut], fut.result())
            if n % progress_every == 0:
                db.commit()
                logger.info(f"thumbnails: {n}/{len(fetchable)} fetched, {stats['found']} images")
    db.commit()
    return stats


def image_for(doc: Optional[RawDocument]) -> Optional[str]:
    if not doc or not doc.metadata_json:
        return None
    return (doc.metadata_json or {}).get("image_url")
