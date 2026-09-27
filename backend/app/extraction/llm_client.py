import json
import re
import logging
from typing import List, Dict, Any
from ..config import settings

logger = logging.getLogger(__name__)

_SYSTEM = "You are a precise corporate intelligence extractor. Return pure JSON only, no markdown fences, no commentary."

class LLMExtractorClient:
    """
    Optional LLM client for enriching signal extractions.
    Uses Groq, with OpenRouter and Gemini as fallbacks, if configured;
    otherwise extraction falls back gracefully to rules alone.
    """
    @property
    def is_available(self) -> bool:
        from ..ai import providers
        return providers.can_serve("extraction")

    def extract_initiatives_llm(
        self,
        text_chunk: str,
        company_name: str,
        ticker: str,
        taxonomy_summary: str
    ) -> List[Dict[str, Any]]:
        """
        Calls LLM to extract structured initiatives.
        """
        if not self.is_available or len(text_chunk) < 50:
            return []

        prompt = f"""You are a market intelligence analyst for IT vendors.
Analyze the following corporate disclosure text for {company_name} ({ticker}).
Identify any technology initiatives, digital transformation, cloud/AI investments, executive changes, or vendor procurement signals.

Taxonomy categories:
{taxonomy_summary}

Text to analyze:
\"\"\"{text_chunk[:4000]}\"\"\"

Return a JSON object with a single key "initiatives", whose value is a list of objects with this schema:
{{
  "initiatives": [
    {{
      "initiative_id": "cloud_migration | ai_genai_ml | data_platform_analytics | cybersecurity_zero_trust | etc",
      "initiative_name": "Name of the initiative",
      "category_id": "tech_initiatives | strategic_priorities | organizational_change | spending_signals | regulatory_compliance",
      "category_name": "Category Name",
      "it_offering": "What IT products/services can be pitched",
      "title": "Short title describing the signal",
      "quote_text": "Exact sentence or excerpt from the text",
      "confidence": 0.85,
      "spend_amount": "$200M or null",
      "timing_horizon": "Q2 2026 or null",
      "key_entities": ["AWS", "Azure", "Snowflake"]
    }}
  ]
}}
Only return valid JSON. If no relevant initiative is mentioned, return {{"initiatives": []}}."""

        # Groq, then OpenRouter, then Gemini (app/ai/providers.py). A reply
        # that is not parseable JSON counts as a failure and goes to the next.
        from ..ai import providers
        for reply in providers.answers(_SYSTEM, prompt, purpose="extraction", max_tokens=3000,
                                       temperature=0.1, json_mode=True):
            try:
                return self._parse_initiatives(reply["text"])
            except (json.JSONDecodeError, ValueError):
                logger.info(f"{reply['provider']} returned unparseable JSON; trying the next provider")
        return []

    def _parse_initiatives(self, raw_out: str) -> List[Dict[str, Any]]:
        cleaned = raw_out.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
            cleaned = re.sub(r"```\s*$", "", cleaned)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"[\[{].*[\]}]", cleaned, re.DOTALL)
            if not match:
                # Not "no initiatives" - no answer at all. Raise so the next
                # provider in the chain is asked.
                raise ValueError("reply contains no JSON")
            parsed = json.loads(match.group(0))

        if isinstance(parsed, dict) and "initiatives" in parsed:
            return parsed["initiatives"] or []
        elif isinstance(parsed, list):
            return parsed
        return []
