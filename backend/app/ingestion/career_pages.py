import datetime
import logging
import httpx
from typing import List, Optional
from ..config import settings
from .base import BaseIngestionAdapter, IngestedDoc, is_relevant_to_company
from .exa_budget import ExaBudgetTracker, with_date_floor as _with_date_floor

logger = logging.getLogger(__name__)

class CareerPageAdapter(BaseIngestionAdapter):
    """
    Exa neural search adapter for hiring-intent signals (role-based, e.g.
    "Head of GenAI", "Director of Cloud Platforms"). Deliberately omits
    Exa's `category` param — unlike exa_news/ir_press's `"news"` category,
    leaving it unset is what surfaces Greenhouse/LinkedIn/job-board results
    (verified live: a "news"-scoped search excludes job postings entirely).

    No taxonomy changes needed — job-posting text naturally contains the
    same IT-offering keywords already in taxonomy.yaml, so the existing
    rule/LLM extractor picks up role-based signal unmodified.
    """
    def __init__(self):
        super().__init__(source_name="career_pages")
        self.api_key = settings.EXA_API_KEY
        self.max_characters = settings.EXA_MAX_CHARACTERS
        self.budget = ExaBudgetTracker(
            usage_file=settings.DATA_DIR / "exa_career_usage.json",
            monthly_budget=settings.EXA_CAREER_MONTHLY_BUDGET
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
            f'{clean_name} hiring Head of Cloud OR Head of AI OR Director of Data Engineering '
            f'OR VP Cybersecurity OR Chief Information Security Officer OR Head of GenAI '
            f'job posting careers'
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
                        "numResults": limit,
                        "contents": {"text": {"maxCharacters": self.max_characters}}
                    }, start_published_date)
                )
                self.budget.record_usage()
                if response.status_code in (402, 429):
                    self.budget.mark_quota_exhausted()
                    logger.warning(
                        f"Exa credits/quota exhausted for career pages (HTTP {response.status_code}) — "
                        f"will retry again in {self.budget.quota_retry_after}."
                    )
                    return []
                if response.status_code != 200:
                    logger.warning(f"Exa API returned {response.status_code} for career pages on {company_name}: {response.text[:200]}")
                    return []

                data = response.json()
                for result in data.get("results", []):
                    text = (result.get("text") or "").strip()
                    if len(text) < 50:
                        continue

                    pub_dt = self._parse_date(result.get("publishedDate"))
                    title = result.get("title") or f"Job Posting for {company_name}"
                    url = result.get("url") or result.get("id") or ""
                    if not url:
                        continue
                    if not is_relevant_to_company(f"{title} {text}", company_name, ticker, aliases):
                        logger.info(f"Discarding Exa career result not actually about {company_name}: {title[:80]}")
                        continue

                    docs.append(IngestedDoc(
                        source_type="career_pages",
                        doc_type="job_posting",
                        title=title,
                        url=url,
                        filing_date=pub_dt,
                        raw_text=text,
                        metadata={
                            "ticker": ticker,
                            "company_name": company_name,
                            "exa_score": result.get("score")
                        },
                        sections={"Job Posting Text": text}
                    ))
        except Exception as e:
            logger.error(f"Error fetching Exa career pages for {company_name}: {e}")

        return docs

    def _parse_date(self, date_str) -> datetime.datetime:
        if date_str:
            try:
                return datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                pass
        return datetime.datetime.utcnow()
