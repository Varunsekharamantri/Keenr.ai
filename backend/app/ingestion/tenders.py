"""
Public-procurement tender adapters.

A published tender is the strongest possible buying signal — it's a real RFP
with a deadline. Unlike every other adapter here, tenders are NOT tied to the
company universe: TED's buyers are public bodies, so tenders land in their own
table and are matched to a tracked company only opportunistically.
"""
import datetime
import logging
from typing import Dict, List, Optional

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"


def _pick_language(value, prefer: str = "eng") -> Optional[str]:
    """TED returns multilingual dicts ({'eng': ..., 'fra': ...}); prefer English."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _pick_language(value[0], prefer) if value else None
    if isinstance(value, dict):
        if prefer in value:
            return _pick_language(value[prefer], prefer)
        for v in value.values():
            picked = _pick_language(v, prefer)
            if picked:
                return picked
    return None


class TedAdapter:
    """
    EU Tenders Electronic Daily. Verified live: no credentials required, and
    CPV + publication-date filtering both work.
    """
    source = "ted"

    def __init__(self):
        self.enabled = settings.TED_ENABLED
        self.cpv_codes = [c.strip() for c in (settings.TED_CPV_CODES or "").split(",") if c.strip()]
        self.lookback_days = settings.TED_LOOKBACK_DAYS
        self.max_notices = settings.TED_MAX_NOTICES_PER_RUN

    def fetch_tenders(self, limit: Optional[int] = None) -> List[dict]:
        if not self.enabled or not self.cpv_codes:
            return []

        since = (datetime.datetime.utcnow() - datetime.timedelta(days=self.lookback_days)).strftime("%Y%m%d")
        cpv_clause = " OR ".join(f"classification-cpv={c}" for c in self.cpv_codes)
        query = f"({cpv_clause}) AND publication-date>={since}"
        page_size = min(limit or self.max_notices, 100)

        out: List[dict] = []
        try:
            with httpx.Client(timeout=40.0) as client:
                resp = client.post(TED_SEARCH_URL, json={
                    "query": query,
                    "limit": page_size,
                    "fields": [
                        "publication-number", "notice-title", "buyer-name",
                        "buyer-country", "publication-date", "deadline-receipt-request",
                        "classification-cpv", "total-value", "links",
                    ],
                })
                if resp.status_code != 200:
                    logger.warning(f"TED returned {resp.status_code}: {resp.text[:200]}")
                    return []
                data = resp.json()
        except Exception as e:
            logger.error(f"TED fetch failed: {e}")
            return []

        for notice in data.get("notices", []):
            pub_no = notice.get("publication-number")
            if not pub_no:
                continue
            title = _pick_language(notice.get("notice-title")) or "Untitled tender"
            links = notice.get("links") or {}
            url = _pick_language((links.get("html") or {})) or f"https://ted.europa.eu/en/notice/-/detail/{pub_no}"

            out.append({
                "source": self.source,
                "external_id": f"ted:{pub_no}",
                "title": title,
                "buyer_name": _pick_language(notice.get("buyer-name")),
                "buyer_country": _pick_language(notice.get("buyer-country")),
                "cpv_codes": notice.get("classification-cpv") or [],
                "published_at": self._parse_date(notice.get("publication-date")),
                "deadline_at": self._parse_date(notice.get("deadline-receipt-request")),
                "value_amount": str(notice.get("total-value")) if notice.get("total-value") else None,
                "currency": None,
                "url": url,
                "description": None,
            })
        logger.info(f"TED: fetched {len(out)} notices (of {data.get('totalNoticeCount')} matching)")
        return out

    def _parse_date(self, value) -> Optional[datetime.datetime]:
        """
        TED dates come in several shapes: "2026-08-03+02:00" (date with a UTC
        offset and no time) and ["2026-08-13T09:00:00+02:00"] (list-wrapped ISO
        datetime). Both are handled; everything is stored naive-UTC-ish.
        """
        raw = _pick_language(value)
        if not raw:
            return None
        text = str(raw).strip()
        try:
            return datetime.datetime.fromisoformat(text).replace(tzinfo=None)
        except ValueError:
            pass
        # Fall back to the leading calendar date, which both shapes start with.
        try:
            return datetime.datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None


class SamGovAdapter:
    """
    US federal opportunities (SAM.gov).

    UNVERIFIED: SAM.gov rejects unauthenticated requests, so the request and
    response shape below follow the published contract but have never been
    exercised against a live response. Gated on SAMGOV_API_KEY — a complete
    no-op until a key is configured, at which point it needs the same
    live-verification pass the earnings-deck exhibit filter got.
    """
    source = "sam_gov"
    BASE_URL = "https://api.sam.gov/prod/opportunities/v2/search"

    def __init__(self):
        self.api_key = settings.SAMGOV_API_KEY
        self.lookback_days = settings.TED_LOOKBACK_DAYS

    def fetch_tenders(self, limit: int = 100) -> List[dict]:
        if not self.api_key:
            return []

        now = datetime.datetime.utcnow()
        params = {
            "api_key": self.api_key,
            "limit": min(limit, 100),
            "postedFrom": (now - datetime.timedelta(days=self.lookback_days)).strftime("%m/%d/%Y"),
            "postedTo": now.strftime("%m/%d/%Y"),
            "ncode": "541512",  # computer systems design services
        }
        try:
            with httpx.Client(timeout=40.0) as client:
                resp = client.get(self.BASE_URL, params=params)
                if resp.status_code != 200:
                    logger.warning(f"SAM.gov returned {resp.status_code}: {resp.text[:200]} "
                                   f"(contract unverified — may need adjusting)")
                    return []
                data = resp.json()
        except Exception as e:
            logger.error(f"SAM.gov fetch failed: {e}")
            return []

        out = []
        for opp in data.get("opportunitiesData", []):
            notice_id = opp.get("noticeId")
            if not notice_id:
                continue
            out.append({
                "source": self.source,
                "external_id": f"sam:{notice_id}",
                "title": opp.get("title") or "Untitled opportunity",
                "buyer_name": opp.get("fullParentPathName") or opp.get("department"),
                "buyer_country": "United States",
                "cpv_codes": [opp.get("naicsCode")] if opp.get("naicsCode") else [],
                "published_at": self._parse_us_date(opp.get("postedDate")),
                "deadline_at": self._parse_us_date(opp.get("responseDeadLine")),
                "value_amount": None,
                "currency": "USD",
                "url": opp.get("uiLink"),
                "description": opp.get("description"),
            })
        return out

    def _parse_us_date(self, value) -> Optional[datetime.datetime]:
        if not value:
            return None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.datetime.strptime(str(value)[:19], fmt).replace(tzinfo=None)
            except ValueError:
                continue
        return None


ted_adapter = TedAdapter()
samgov_adapter = SamGovAdapter()
