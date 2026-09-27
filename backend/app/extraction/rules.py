import re
from typing import List, Dict, Any, Optional
from pydantic import BaseModel

class ExtractedSnippet(BaseModel):
    initiative_id: str
    initiative_name: str
    category_id: str
    category_name: str
    it_offering: str
    title: str
    quote_text: str
    context_text: str
    confidence: float
    spend_amount: Optional[str] = None
    timing_horizon: Optional[str] = None
    key_entities: List[str] = []

class RuleMatcher:
    """
    Regex and Pattern matching engine for public filings & news text.
    Extracts high-intent IT initiative sentences, dollar amounts, timing, and tech vendors.
    """
    def __init__(self):
        # Known tech vendors and platforms
        self.tech_entities = [
            "AWS", "Amazon Web Services", "Azure", "Microsoft Azure", "Google Cloud", "GCP",
            "Snowflake", "Databricks", "Salesforce", "SAP", "ServiceNow", "Workday",
            "CrowdStrike", "Palo Alto Networks", "Cisco", "Oracle", "IBM", "OpenAI",
            "Anthropic", "Kubernetes", "Docker", "Terraform", "Datadog", "Splunk",
            "Kafka", "MongoDB", "PostgreSQL", "Epic Systems", "Cerner"
        ]
        
        # Spend amount patterns
        self.spend_patterns = [
            r"\$\s*\d+(?:\.\d+)?\s*(?:billion|million|B|M)\b",
            r"(?:multi-million|multi-billion)\s*dollar",
            r"\$\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?"
        ]
        
        # Timing horizon patterns
        self.timing_patterns = [
            r"\b(?:Q[1-4]\s*202\d|202[4-9]|2030)\b",
            r"\b(?:multi-year|next\s+\d+\s+(?:months|years)|by\s+year-end|fiscal\s+202\d)\b"
        ]
        
        # High-intent action verbs
        self.action_verbs = [
            "invest", "investing", "invested", "allocate", "migrat", "deploy", "moderniz",
            "adopt", "partner", "transform", "launch", "implement", "select", "build",
            "expand", "acquire", "appoint", "accelerat"
        ]

    def extract_from_text(
        self,
        text: str,
        company_name: str,
        ticker: str,
        taxonomy_manager,
        section_name: str = "General"
    ) -> List[ExtractedSnippet]:
        if not text or len(text) < 30:
            return []
        
        # Split into sentences (handling punctuation carefully)
        sentences = self._split_sentences(text)
        results: List[ExtractedSnippet] = []
        seen_quotes = set()
        
        for idx, sentence in enumerate(sentences):
            sentence_clean = sentence.strip()
            if len(sentence_clean) < 40 or len(sentence_clean) > 800:
                continue
            
            sentence_lower = sentence_clean.lower()
            
            # Check matches across taxonomy keywords
            for cat in taxonomy_manager.get_all_categories():
                for init in cat.initiatives:
                    matched_kw = []
                    for kw in init.keywords:
                        # check whole word match
                        if re.search(r"\b" + re.escape(kw) + r"\b", sentence_lower):
                            matched_kw.append(kw)
                            
                    if not matched_kw:
                        continue
                    
                    # Compute confidence
                    confidence = 0.70
                    
                    # Boost if company or ticker is mentioned. Both are optional:
                    # the CIK-less institutions (ANZ, Allianz, BNP Paribas...)
                    # carry no ticker, and an unguarded .lower() here aborted
                    # extraction for the whole document.
                    name_l = (company_name or "").lower()
                    ticker_l = (ticker or "").lower()
                    if (name_l and name_l in sentence_lower) or (ticker_l and ticker_l in sentence_lower):
                        confidence += 0.05
                    
                    # Boost if high-intent action verb is present
                    has_action = any(v in sentence_lower for v in self.action_verbs)
                    if has_action:
                        confidence += 0.10
                        
                    # Boost if multiple keywords matched
                    if len(matched_kw) > 1:
                        confidence += 0.05
                        
                    # Find spend amount
                    spend_amount = None
                    for sp_pat in self.spend_patterns:
                        sp_match = re.search(sp_pat, sentence_clean, re.IGNORECASE)
                        if sp_match:
                            spend_amount = sp_match.group(0)
                            confidence += 0.05
                            break
                            
                    # Find timing
                    timing_horizon = None
                    for t_pat in self.timing_patterns:
                        t_match = re.search(t_pat, sentence_clean, re.IGNORECASE)
                        if t_match:
                            timing_horizon = t_match.group(0)
                            break
                            
                    # Find tech entities
                    entities_found = []
                    for ent in self.tech_entities:
                        if re.search(r"\b" + re.escape(ent) + r"\b", sentence_clean, re.IGNORECASE):
                            entities_found.append(ent)
                            
                    if entities_found:
                        confidence += 0.05
                        
                    confidence = min(0.95, confidence)
                    
                    # Dedup quote
                    quote_hash = f"{init.id}_{sentence_clean[:50]}"
                    if quote_hash in seen_quotes:
                        continue
                    seen_quotes.add(quote_hash)
                    
                    # Context window (previous + current + next sentence)
                    prev_sent = sentences[idx - 1].strip() if idx > 0 else ""
                    next_sent = sentences[idx + 1].strip() if idx < len(sentences) - 1 else ""
                    context = f"{prev_sent} {sentence_clean} {next_sent}".strip()
                    
                    title = f"{company_name} {init.name} Signal"
                    if spend_amount:
                        title += f" ({spend_amount})"
                    elif entities_found:
                        title += f" ({', '.join(entities_found[:2])})"
                        
                    snippet = ExtractedSnippet(
                        initiative_id=init.id,
                        initiative_name=init.name,
                        category_id=cat.id,
                        category_name=cat.name,
                        it_offering=init.it_offering,
                        title=title,
                        quote_text=sentence_clean,
                        context_text=context[:800],
                        confidence=confidence,
                        spend_amount=spend_amount,
                        timing_horizon=timing_horizon,
                        key_entities=entities_found
                    )
                    results.append(snippet)
                    
        return results

    def _split_sentences(self, text: str) -> List[str]:
        # Clean text
        text = re.sub(r"\s+", " ", text)
        # Split on period/question/exclamation followed by space and capital letter
        raw_sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text)
        return [s.strip() for s in raw_sentences if s.strip()]
