import time
import datetime
import logging
import httpx
from typing import List, Optional
from ..config import settings
from .base import BaseIngestionAdapter, IngestedDoc, is_relevant_to_company

logger = logging.getLogger(__name__)

class PatentAdapter(BaseIngestionAdapter):
    """
    Patent filings as a long-term R&D/tech-focus signal, via the PatentsView
    dataset now hosted on the USPTO Open Data Portal (api.uspto.gov).

    IMPORTANT — unverified against a live response: PatentsView's legacy
    endpoints (search.patentsview.org / api.patentsview.org) are decommissioned
    and now hard-redirect to a USPTO migration notice. The docs site
    (data.uspto.gov) is a JS-rendered app behind bot protection that couldn't
    be scraped for the exact current request/response shape. This adapter is
    built against the best-known contract (the classic PatentsView q/f/o JSON
    query language, which search results indicate carried over) — the
    endpoint path, auth header name, and field names below should be
    confirmed against a real response once PATENTSVIEW_API_KEY is set, the
    same way the earnings-deck exhibit filter (sec_edgar.py) was verified
    empirically before being finalized rather than assumed.

    Entirely optional: returns [] immediately if no API key is configured,
    so this is safe to wire into the pipeline before the key exists.
    """
    BASE_URL = "https://api.uspto.gov/api/v1/patent/"

    def __init__(self):
        super().__init__(source_name="patents")
        self.api_key = settings.PATENTSVIEW_API_KEY

    def fetch_documents(
        self,
        ticker: str,
        cik: str,
        company_name: str,
        limit: int = 5,
        aliases: Optional[List[str]] = None
    ) -> List[IngestedDoc]:
        if not self.api_key:
            return []

        clean_name = company_name.replace("&", "and").split(" Inc")[0].split(" Corp")[0].strip()
        query_body = {
            "q": {"_contains": {"assignees.assignee_organization": clean_name}},
            "f": ["patent_id", "patent_title", "patent_date", "patent_abstract", "assignees.assignee_organization"],
            "o": {"size": limit},
            "s": [{"patent_date": "desc"}]
        }

        docs: List[IngestedDoc] = []
        try:
            time.sleep(settings.PATENTSVIEW_RATE_LIMIT_DELAY)
            with httpx.Client(timeout=20.0) as client:
                response = client.post(
                    self.BASE_URL,
                    headers={"X-Api-Key": self.api_key, "Content-Type": "application/json"},
                    json=query_body
                )
                if response.status_code != 200:
                    logger.warning(
                        f"PatentsView/USPTO ODP API returned {response.status_code} for {company_name}: "
                        f"{response.text[:200]} (endpoint/query shape may need updating — see module docstring)"
                    )
                    return []

                data = response.json()
                for patent in data.get("patents", []):
                    title = (patent.get("patent_title") or "").strip()
                    abstract = (patent.get("patent_abstract") or "").strip()
                    patent_id = patent.get("patent_id")
                    if not title or not patent_id:
                        continue

                    assignees = patent.get("assignees") or []
                    assignee_org = ""
                    for a in assignees:
                        if isinstance(a, dict) and a.get("assignee_organization"):
                            assignee_org = a["assignee_organization"]
                            break

                    # Extra guard: _contains is a substring match on assignee
                    # org name, which can false-positive on subsidiaries or
                    # unrelated similarly-named organizations.
                    if not is_relevant_to_company(assignee_org, company_name, ticker, aliases):
                        logger.info(f"Discarding patent not actually assigned to {company_name}: {assignee_org[:80]}")
                        continue

                    filing_dt = self._parse_date(patent.get("patent_date"))
                    raw_text = f"{title}. {abstract}".strip()
                    if len(raw_text) < 30:
                        continue

                    docs.append(IngestedDoc(
                        source_type="patents",
                        doc_type="patent_grant",
                        title=f"{company_name} - Patent: {title}",
                        url=f"https://patents.google.com/patent/{patent_id}",
                        filing_date=filing_dt,
                        raw_text=raw_text,
                        metadata={
                            "ticker": ticker,
                            "patent_id": patent_id,
                            "assignee_organization": assignee_org
                        },
                        sections={"Patent Abstract": raw_text}
                    ))
        except Exception as e:
            logger.error(f"Error fetching patents for {company_name}: {e}")

        return docs

    def _parse_date(self, date_str) -> datetime.datetime:
        if date_str:
            try:
                return datetime.datetime.strptime(date_str, "%Y-%m-%d")
            except Exception:
                pass
        return datetime.datetime.utcnow()
