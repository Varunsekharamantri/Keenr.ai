import logging
from typing import List, Dict, Any, Optional
from ..models.taxonomy import taxonomy_manager
from ..models.schema import Event, RawDocument, Company
from .rules import RuleMatcher, ExtractedSnippet
from .llm_client import LLMExtractorClient

logger = logging.getLogger(__name__)

class SignalExtractor:
    """
    Main Signal Extraction Engine
    Combines rule-based pattern matching with optional LLM enrichment.
    """
    def __init__(self):
        self.rule_matcher = RuleMatcher()
        self.llm_client = LLMExtractorClient()
        self.taxonomy = taxonomy_manager
        self._taxonomy_summary = self._build_taxonomy_summary()

    def _build_taxonomy_summary(self) -> str:
        lines = []
        for cat in self.taxonomy.get_all_categories():
            init_ids = ", ".join(i.id for i in cat.initiatives)
            lines.append(f"- {cat.id} ({cat.name}): {init_ids}")
        return "\n".join(lines)

    def extract_from_doc(
        self,
        company: Company,
        raw_doc_id: Optional[str],
        doc_title: str,
        doc_url: str,
        source_type: str,
        filing_date,
        sections: Dict[str, str],
        full_text: str
    ) -> List[Event]:
        events: List[Event] = []
        snippets: List[ExtractedSnippet] = []

        # 1. Normalize full text and clean unprintable chars
        clean_full_text = self._normalize_text(full_text)

        # 2. Extract from key sections first
        if sections:
            for sec_name, sec_text in sections.items():
                clean_sec_text = self._normalize_text(sec_text)
                sec_snippets = self.rule_matcher.extract_from_text(
                    text=clean_sec_text,
                    company_name=company.name,
                    ticker=company.ticker,
                    taxonomy_manager=self.taxonomy,
                    section_name=sec_name
                )
                snippets.extend(sec_snippets)

        # 3. Also extract from document chunks (ensuring comprehensive coverage)
        chunk_size = 40000
        max_scan_chars = 400000
        for i in range(0, min(len(clean_full_text), max_scan_chars), chunk_size):
            chunk = clean_full_text[i:i + chunk_size]
            chunk_snippets = self.rule_matcher.extract_from_text(
                text=chunk,
                company_name=company.name,
                ticker=company.ticker,
                taxonomy_manager=self.taxonomy,
                section_name="Document Text"
            )
            snippets.extend(chunk_snippets)

        # 4. Convert ExtractedSnippet to Event models
        seen_events = set()
        for snip in snippets:
            # Dedup key
            dedup_key = f"{snip.initiative_id}_{snip.quote_text[:60]}"
            if dedup_key in seen_events:
                continue
            seen_events.add(dedup_key)

            event = Event(
                company_id=company.id,
                raw_doc_id=raw_doc_id,
                initiative_id=snip.initiative_id,
                initiative_name=snip.initiative_name,
                category_id=snip.category_id,
                category_name=snip.category_name,
                it_offering=snip.it_offering,
                title=snip.title,
                quote_text=snip.quote_text,
                context_text=snip.context_text,
                occurred_at=filing_date,
                source_type=source_type,
                source_url=doc_url,
                confidence=snip.confidence,
                spend_amount=snip.spend_amount,
                timing_horizon=snip.timing_horizon,
                key_entities=snip.key_entities
            )
            events.append(event)

        # 5. LLM enrichment - the backstop for initiatives phrased in ways the
        # keyword rules miss.
        #
        # It used to run on every document, which cost about 1,500 tokens each:
        # 222 documents in one refresh consumed the whole 200,000-token daily
        # allowance and left nothing for the dashboard summaries. It now runs
        # only where it earns its keep:
        #   * the keyword pass found nothing (the case it exists for), or
        #   * the document is a first-party disclosure - a filing, an earnings
        #     deck or an IR release - where the wording is formal and the value
        #     of catching a missed initiative is highest.
        # Job postings never qualify: they are short, formulaic, and the keyword
        # matcher handles them well.
        # is_available asks the provider chain (Groq, then the OpenRouter and
        # Gemini fallbacks) whether any of them has budget left for extraction.
        high_value = source_type in ("sec_edgar", "earnings_deck", "ir_press")
        worth_llm = (not events or high_value) and source_type != "career_pages"
        if worth_llm and self.llm_client.is_available:
            llm_events = self._extract_with_llm(
                company=company,
                raw_doc_id=raw_doc_id,
                doc_url=doc_url,
                source_type=source_type,
                filing_date=filing_date,
                sections=sections,
                full_text=clean_full_text,
                seen_events=seen_events
            )
            events.extend(llm_events)

        return events

    def _extract_with_llm(
        self,
        company: Company,
        raw_doc_id: Optional[str],
        doc_url: str,
        source_type: str,
        filing_date,
        sections: Dict[str, str],
        full_text: str,
        seen_events: set
    ) -> List[Event]:
        if sections:
            sample_text = "\n\n".join(list(sections.values())[:2])
        else:
            sample_text = full_text
        sample_text = sample_text[:4000]

        if len(sample_text) < 50:
            return []

        try:
            raw_items = self.llm_client.extract_initiatives_llm(
                text_chunk=sample_text,
                company_name=company.name,
                ticker=company.ticker or company.name,
                taxonomy_summary=self._taxonomy_summary
            )
        except Exception as e:
            logger.warning(f"LLM extraction call failed for {company.name}: {e}")
            return []

        events: List[Event] = []
        for item in raw_items:
            try:
                initiative_id = item.get("initiative_id", "")
                initiative_def = self.taxonomy.get_initiative(initiative_id)
                category_def = self.taxonomy.get_category_for_initiative(initiative_id)
                if not initiative_def or not category_def:
                    continue

                quote_text = (item.get("quote_text") or "").strip()
                if not quote_text or len(quote_text) < 20:
                    continue

                dedup_key = f"{initiative_id}_{quote_text[:60]}"
                if dedup_key in seen_events:
                    continue
                seen_events.add(dedup_key)

                confidence = float(item.get("confidence", 0.75) or 0.75)
                confidence = max(0.0, min(0.95, confidence))

                events.append(Event(
                    company_id=company.id,
                    raw_doc_id=raw_doc_id,
                    initiative_id=initiative_id,
                    initiative_name=item.get("initiative_name") or initiative_def.name,
                    category_id=category_def.id,
                    category_name=category_def.name,
                    it_offering=item.get("it_offering") or initiative_def.it_offering,
                    title=item.get("title") or f"{company.name} {initiative_def.name} Signal (LLM)",
                    quote_text=quote_text,
                    context_text=quote_text,
                    occurred_at=filing_date,
                    source_type=source_type,
                    source_url=doc_url,
                    confidence=confidence,
                    spend_amount=self._clean_optional(item.get("spend_amount")),
                    timing_horizon=self._clean_optional(item.get("timing_horizon")),
                    key_entities=item.get("key_entities") or []
                ))
            except Exception as e:
                logger.warning(f"Skipping malformed LLM extraction item for {company.name}: {e}")
                continue

        return events

    def _clean_optional(self, value) -> Optional[str]:
        if not value or not isinstance(value, str):
            return None
        if value.strip().lower() in ("null", "none", "n/a", ""):
            return None
        return value.strip()

    def _normalize_text(self, text: str) -> str:
        if not text:
            return ""
        # Replace non-breaking spaces, curly quotes, smart dashes
        text = text.replace("\xa0", " ").replace("’", "'").replace("‘", "'")
        text = text.replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
        # Replace replacement character \ufffd with appropriate space/quote
        text = text.replace("\ufffd", "'")
        return text

signal_extractor = SignalExtractor()
