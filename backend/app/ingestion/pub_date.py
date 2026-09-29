"""
When was this published? Answered from evidence, never assumed.

Search APIs often return no publication date: Exa leaves `publishedDate` empty
for pages that carry none, such as a cloud vendor's customer case study, a
filing tracker or a PDF. The adapters used to fill the gap with the fetch
time, so a 2021 AWS case study arrived as "today's news" - and because such
pages are long and dense, they produced about three quarters of the events in
the default 30-day view.

Now a missing date is looked for the way a person would, cheapest first:

  1. the URL             - /2026/01/20/, 2026-01-20, 20260120
  2. the page's metadata - article:published_time, JSON-LD datePublished,
                           <time datetime>, meta name="date" (one GET)
  3. the byline          - "Published: 2026-08-26", "January 20, 2026" at the
                           top of the text we already have

If none of those gives a date, the caller drops the document. A missing
article is a smaller error than an old one presented as new.
"""
import datetime
import json
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_RE = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"

_URL_PATTERNS = [
    re.compile(r"/(20\d\d)/(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])(?:/|$|[-_])"),
    re.compile(r"(?<!\d)(20\d\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(20\d\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"),
]
_META_KEYS = ("article:published_time", "og:published_time", "datepublished", "publishdate", "pubdate",
              "publish_date", "date", "dc.date", "dc.date.issued", "dcterms.created", "sailthru.date",
              "parsely-pub-date", "article.published")
# Deliberately absent: og:updated_time, article:modified_time, dateModified.
# They move whenever a page is edited - an AWS case study from years ago read
# as published yesterday because its template was touched.
_BYLINE = [
    re.compile(r"(?:Published|Posted|Released|Date)\s*(?:on)?\s*[:\-]?\s*(20\d\d-\d\d-\d\d)", re.I),
    re.compile(rf"\b{_MONTH_RE}\.?\s+(\d{{1,2}}),?\s+(20\d\d)\b"),
    re.compile(rf"\b(\d{{1,2}})\s+{_MONTH_RE}\.?\s+(20\d\d)\b"),
]
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _plausible(d: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """A publication date cannot be in the future or before the web mattered."""
    if d is None:
        return None
    if d.year < 2000 or d > datetime.datetime.utcnow() + datetime.timedelta(days=1):
        return None
    return d


def parse_iso(value) -> Optional[datetime.datetime]:
    """An ISO-ish timestamp as naive UTC, or None."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    try:
        d = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return _plausible(d)
    except ValueError:
        pass
    m = re.match(r"(20\d\d)-?(\d\d)-?(\d\d)", v)
    if m:
        try:
            return _plausible(datetime.datetime(int(m[1]), int(m[2]), int(m[3])))
        except ValueError:
            return None
    return None


def from_url(url: str) -> Optional[datetime.datetime]:
    for p in _URL_PATTERNS:
        m = p.search(url or "")
        if m:
            try:
                return _plausible(datetime.datetime(int(m[1]), int(m[2]), int(m[3])))
            except ValueError:
                continue
    return None


def from_text(text: str, window: int = 700) -> Optional[datetime.datetime]:
    """A date in the byline: only the top of the text, where bylines sit - a
    date deeper in an article is usually something it mentions."""
    head = (text or "")[:window]
    m = _BYLINE[0].search(head)
    if m:
        return parse_iso(m[1])
    best = None
    for p, order in ((_BYLINE[1], "mdy"), (_BYLINE[2], "dmy")):
        m = p.search(head)
        if not m:
            continue
        mon, day = (m[1], m[2]) if order == "mdy" else (m[2], m[1])
        try:
            d = _plausible(datetime.datetime(int(m[3]), _MONTHS[mon[:3].lower()], int(day)))
        except (ValueError, KeyError):
            d = None
        if d and (best is None or m.start() < best[0]):
            best = (m.start(), d)
    return best[1] if best else None


def from_html(html: str) -> Optional[datetime.datetime]:
    """The page's own statement of when it was published."""
    # JSON-LD first: the most deliberate of the lot.
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', block) or re.search(r'"dateCreated"\s*:\s*"([^"]+)"', block)
        if m and parse_iso(m[1]):
            return parse_iso(m[1])
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        key = re.search(r'(?:property|name|itemprop)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        val = re.search(r'content\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if key and val and key[1].strip().lower() in _META_KEYS and parse_iso(val[1]):
            return parse_iso(val[1])
    m = re.search(r'<time\b[^>]*datetime\s*=\s*["\']([^"\']+)["\']', html, re.I)
    if m and parse_iso(m[1]):
        return parse_iso(m[1])
    return None


def fetch_html_date(url: str, timeout: float = 8.0) -> Optional[datetime.datetime]:
    if not url or not url.startswith("http") or url.lower().split("?")[0].endswith(".pdf"):
        return None
    try:
        with httpx.Client(timeout=timeout, headers=_HEADERS, follow_redirects=True) as client:
            r = client.get(url)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "").lower():
            return None
        return from_html(r.text[:400_000])
    except Exception as ex:
        logger.debug(f"no page date for {url[:80]}: {ex}")
        return None


def find_published_date(url: str, text: str = "", title: str = "",
                        fetch: bool = True) -> Optional[datetime.datetime]:
    """Best evidence of the publication date, or None if there is none."""
    return (from_url(url) or from_text(title, 200)
            or (fetch and fetch_html_date(url)) or from_text(text) or None)


def resolve(api_date: Optional[str], url: str, text: str, title: str,
            floor: Optional[datetime.datetime] = None) -> Optional[datetime.datetime]:
    """
    The date to store for a search result, or None if the result should be
    dropped: no date could be established, or it predates `floor`.
    """
    d = parse_iso(api_date) or find_published_date(url, text, title)
    if d is None:
        logger.info(f"Dropping undated result: {title[:70]} ({url[:70]})")
        return None
    if floor is not None and d < floor:
        logger.info(f"Dropping result from {d:%Y-%m-%d}, before {floor:%Y-%m-%d}: {title[:70]}")
        return None
    return d


def floor_from(start_published_date: Optional[str]) -> Optional[datetime.datetime]:
    return parse_iso(start_published_date) if start_published_date else None
