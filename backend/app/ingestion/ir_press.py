import datetime
import logging
import httpx
from typing import List, Optional
from ..config import settings
from .base import BaseIngestionAdapter, IngestedDoc, is_relevant_to_company
from .exa_budget import ExaBudgetTracker, with_date_floor as _with_date_floor

logger = logging.getLogger(__name__)

class IrPressAdapter(BaseIngestionAdapter):
    """
    Exa neural search adapter tuned for a company's own investor-relations
    announcements (product/initiative launches, partnerships, leadership
    changes) rather than general tech-topic news. The goal is catching
    strategic-initiative language in the company's own words, often before
    it surfaces in a 10-K/10-Q.

    Uses the same Exa API as ExaNewsAdapter but tracks its own separate
    budget (own usage-counter file) so a busy news day never starves IR
    coverage, and vice versa — see ExaBudgetTracker.
    """
    def __init__(self):
        super().__init__(source_name="ir_press")
        self.api_key = settings.EXA_API_KEY
        self.max_characters = settings.EXA_MAX_CHARACTERS
        self.budget = ExaBudgetTracker(
            usage_file=settings.DATA_DIR / "exa_ir_usage.json",
            monthly_budget=settings.EXA_IR_MONTHLY_BUDGET
        )

    @property
    def quota_exhausted(self) -> bool:
        return self.budget.quota_exhausted

    @property
    def budget_exhausted(self) -> bool:
        return self.budget.budget_exhausted

    def get_usage_status(self) -> dict:
        return self.budget.get_usage_status()

    def fetch_documents(
        self,
        ticker: str,
        cik: str,
        company_name: str,
        limit: int = 5,
        aliases: Optional[List[str]] = None,
        start_published_date: Optional[str] = None,
    ) -> List[IngestedDoc]:
        if not self.api_key or self.quota_exhausted or self.budget_exhausted:
            return []

        clean_name = company_name.replace("&", "and").split(" Inc")[0].split(" Corp")[0].strip()
        query = (
            f'{clean_name} investor relations announces press release '
            f'strategic initiative program partnership launch appointment'
        )

        docs: List[IngestedDoc] = []
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    "https://api.exa.ai/search",
                    headers={
                        "x-api-key": self.api_key,
                        "Content-Type": "application/json"
                    },
                    json=_with_date_floor({
                        "query": query,
                        "category": "news",
                        "numResults": limit,
                        "contents": {"text": {"maxCharacters": self.max_characters}}
                    }, start_published_date)
                )
                self.budget.record_usage()
                if response.status_code in (402, 429):
                    self.budget.mark_quota_exhausted()
                    logger.warning(
                        f"Exa credits/quota exhausted for IR press (HTTP {response.status_code}) — "
                        f"will retry again in {self.budget.quota_retry_after}."
                    )
                    return []
                if response.status_code != 200:
                    logger.warning(f"Exa API returned {response.status_code} for IR press on {company_name}: {response.text[:200]}")
                    return []

                data = response.json()
                for result in data.get("results", []):
                    text = (result.get("text") or "").strip()
                    if len(text) < 50:
                        continue

                    pub_dt = self._parse_date(result.get("publishedDate"))
                    title = result.get("title") or f"IR Announcement for {company_name}"
                    url = result.get("url") or result.get("id") or ""
                    if not url:
                        continue
                    if not is_relevant_to_company(f"{title} {text}", company_name, ticker, aliases):
                        logger.info(f"Discarding Exa IR result not actually about {company_name}: {title[:80]}")
                        continue

                    docs.append(IngestedDoc(
                        source_type="ir_press",
                        doc_type="ir_announcement",
                        title=title,
                        url=url,
                        filing_date=pub_dt,
                        raw_text=text,
                        metadata={
                            "ticker": ticker,
                            "company_name": company_name,
                            "author": result.get("author"),
                            "exa_score": result.get("score")
                        },
                        sections={"Announcement Text": text}
                    ))
        except Exception as e:
            logger.error(f"Error fetching Exa IR press for {company_name}: {e}")

        return docs

    def _parse_date(self, date_str) -> datetime.datetime:
        if date_str:
            try:
                return datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                pass
        return datetime.datetime.utcnow()
