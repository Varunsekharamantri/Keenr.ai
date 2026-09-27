import re
import datetime
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from pydantic import BaseModel

_LEGAL_SUFFIX_RE = re.compile(
    r",?\s+(&\s+)?(Corporation|Incorporated|Inc\.?|Corp\.?|Company|Co\.?|plc|PLC|"
    r"Ltd\.?|Limited|Group|Holdings?|LLC|LP|S\.A\.?|N\.V\.?|AG|SE)\s*$",
    re.IGNORECASE
)

def company_core_name(company_name: str) -> str:
    """Strips legal suffixes/articles to get the name's identifying core,
    e.g. 'JPMorgan Chase & Co.' -> 'JPMorgan Chase', 'The Progressive
    Corporation' -> 'Progressive'."""
    core = company_name.strip()
    for _ in range(2):  # handle compound suffixes like "Group Holdings"
        stripped = _LEGAL_SUFFIX_RE.sub("", core).strip()
        if stripped == core:
            break
        core = stripped
    if core.lower().startswith("the "):
        core = core[4:].strip()
    return core

def is_relevant_to_company(
    text: str,
    company_name: str,
    ticker: Optional[str],
    aliases: Optional[List[str]] = None
) -> bool:
    """Name-search sources (news RSS, neural search) can return results that
    are only thematically/semantically similar, not actually about the target
    company (e.g. 'The Progressive Corporation' matching a 'Progress
    Software' article via loose relevance ranking). Require the company's
    core name, a known alias, or its ticker to literally appear.

    News commonly uses a shortened brand form ("Citi" for Citigroup, "Capital
    One" for "Capital One Financial Corp") rather than the full legal name, so
    this also accepts a leading two-word prefix of a multi-word core name and
    any parenthetical hint found in an alias (e.g. "... (CIBC)" -> "CIBC").
    This intentionally leans toward recall over precision — the keyword-based
    extractor is a second filter that still has to find a real IT-initiative
    signal in the text, so a wrongly-accepted article rarely produces an event
    on its own.
    """
    if not text:
        return False
    text_lower = text.lower()

    def word_bounded(phrase: str) -> bool:
        """Substring containment alone lets short strings match inside unrelated
        words (e.g. alias 'PGR' inside 'upgrades', or core name 'AON' inside
        'salmon') — always require real word boundaries."""
        phrase = phrase.strip()
        if len(phrase) < 2:
            return False
        return re.search(r"\b" + re.escape(phrase.lower()) + r"\b", text_lower) is not None

    core = company_core_name(company_name)
    if core and word_bounded(core):
        return True

    core_words = core.split()
    if len(core_words) >= 2:
        short_form = " ".join(core_words[:2])
        if len(short_form) >= 6 and word_bounded(short_form):
            return True

    if ticker and len(ticker) >= 2 and word_bounded(ticker):
        return True

    for alias in (aliases or []):
        alias = (alias or "").strip()
        if len(alias) >= 3 and word_bounded(alias):
            return True
        paren_match = re.search(r"\(([A-Za-z]{2,10})\)\s*$", alias)
        if paren_match and word_bounded(paren_match.group(1)):
            return True

    return False

class IngestedDoc(BaseModel):
    source_type: str # "sec_edgar", "news_rss"
    doc_type: str # "10-K", "10-Q", "8-K", "news_article"
    title: str
    url: str
    filing_date: datetime.datetime
    raw_text: str
    metadata: Dict[str, Any] = {}
    sections: Dict[str, str] = {} # e.g. {"Item 1": "...", "Item 7": "..."}

class BaseIngestionAdapter(ABC):
    def __init__(self, source_name: str):
        self.source_name = source_name

    @abstractmethod
    def fetch_documents(
        self,
        ticker: str,
        cik: str,
        company_name: str,
        limit: int = 5
    ) -> List[IngestedDoc]:
        """Fetch raw documents for a given company."""
        pass
