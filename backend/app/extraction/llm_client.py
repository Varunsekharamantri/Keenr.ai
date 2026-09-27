import json
import re
import time
import logging
import httpx
from typing import List, Dict, Any, Optional
from ..config import settings

logger = logging.getLogger(__name__)

class LLMExtractorClient:
    """
    Optional LLM client for enriching signal extractions.
    Uses Groq, OpenAI, or Gemini if configured, otherwise falls back gracefully
    to rule-based-only extraction.
    """
    def __init__(self):
        self.groq_key = settings.GROQ_API_KEY
        self.openai_key = settings.OPENAI_API_KEY
        self.gemini_key = settings.GEMINI_API_KEY
        self.ollama_url = settings.OLLAMA_BASE_URL

    @property
    def is_available(self) -> bool:
        return bool(self.groq_key or self.openai_key or self.gemini_key)

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

        if self.groq_key:
            try:
                return self._call_groq(prompt)
            except Exception as e:
                logger.warning(f"Groq extraction failed, falling back: {e}")

        if self.openai_key:
            try:
                return self._call_openai(prompt)
            except Exception as e:
                logger.warning(f"OpenAI extraction failed, falling back: {e}")

        return []

    def _call_groq(self, prompt: str, _retries_left: int = 2) -> List[Dict[str, Any]]:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.groq_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": settings.GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": "You are a precise corporate intelligence extractor. Return pure JSON only, no markdown fences, no commentary."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"}
                }
            )
            if response.status_code == 429:
                if "per day" in (response.text or "").lower():
                    try:
                        from ..ai.quota import mark_exhausted
                        mark_exhausted(response.text[:200])
                    except Exception:
                        pass
                    return []
                if _retries_left <= 0:
                    logger.warning("Groq rate limit still hit after retries — skipping LLM enrichment for this document.")
                    return []
                wait_s = self._parse_retry_wait(response.text) or 6.0
                wait_s = min(wait_s, 15.0)
                logger.info(f"Groq rate-limited (429) — backing off {wait_s:.1f}s and retrying ({_retries_left} left).")
                time.sleep(wait_s)
                return self._call_groq(prompt, _retries_left=_retries_left - 1)
            if response.status_code != 200:
                logger.warning(f"Groq API returned {response.status_code}: {response.text[:300]}")
                return []
            body = response.json()
            try:
                from ..ai.quota import record
                record((body.get("usage") or {}).get("total_tokens", 0), "extraction")
            except Exception:
                pass
            raw_out = body["choices"][0]["message"]["content"] or ""
            return self._parse_initiatives(raw_out)

    def _parse_retry_wait(self, error_text: str) -> Optional[float]:
        match = re.search(r"try again in ([\d.]+)\s*s", error_text)
        return float(match.group(1)) if match else None

    def _call_openai(self, prompt: str) -> List[Dict[str, Any]]:
        from openai import OpenAI
        client = OpenAI(api_key=self.openai_key)
        response = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a precise corporate intelligence extractor. Return pure JSON only."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            response_format={"type": "json_object"} if "gpt-4o" in settings.OPENAI_MODEL else None
        )
        raw_out = response.choices[0].message.content or ""
        return self._parse_initiatives(raw_out)

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
                return []
            parsed = json.loads(match.group(0))

        if isinstance(parsed, dict) and "initiatives" in parsed:
            return parsed["initiatives"] or []
        elif isinstance(parsed, list):
            return parsed
        return []
