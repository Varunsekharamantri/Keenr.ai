import io
import re
import time
import datetime
import logging
import httpx
import pdfplumber
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional
from ..config import settings
from .base import BaseIngestionAdapter, IngestedDoc

logger = logging.getLogger(__name__)

class SecEdgarAdapter(BaseIngestionAdapter):
    """
    SEC EDGAR Ingestion Adapter
    Polls 8-K, 10-K, 10-Q filings, downloads documents, parses sections.
    Complies with SEC rate limits and User-Agent requirements.
    """
    def __init__(self):
        super().__init__(source_name="sec_edgar")
        self.headers = {
            "User-Agent": settings.SEC_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Host": "data.sec.gov"
        }
        self.doc_headers = {
            "User-Agent": settings.SEC_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Host": "www.sec.gov"
        }

    def fetch_documents(
        self,
        ticker: str,
        cik: str,
        company_name: str,
        limit: int = 5
    ) -> List[IngestedDoc]:
        formatted_cik = str(cik).zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{formatted_cik}.json"
        
        docs: List[IngestedDoc] = []
        try:
            time.sleep(settings.SEC_RATE_LIMIT_DELAY)
            with httpx.Client(timeout=15.0, headers=self.headers) as client:
                response = client.get(url)
                if response.status_code != 200:
                    logger.warning(f"SEC API returned {response.status_code} for CIK {formatted_cik}")
                    return []
                
                data = response.json()
                filings = data.get("filings", {}).get("recent", {})
                if not filings:
                    return []
                
                forms = filings.get("form", [])
                accession_numbers = filings.get("accessionNumber", [])
                filing_dates = filings.get("filingDate", [])
                primary_docs = filings.get("primaryDocument", [])
                descriptions = filings.get("primaryDocDescription", [])
                
                cik_no_zeros = str(int(cik))
                
                # 20-F/6-K are the foreign-private-issuer equivalents of 10-K/8-K
                # (common for BFSI RoW banks like HSBC, TD, Santander)
                target_forms = {"10-K", "10-Q", "8-K", "20-F", "6-K"}
                count = 0
                
                for i in range(len(forms)):
                    form_type = forms[i]
                    if form_type not in target_forms:
                        continue
                    
                    accession = accession_numbers[i]
                    accession_no_dashes = accession.replace("-", "")
                    primary_doc = primary_docs[i]
                    f_date_str = filing_dates[i]
                    doc_desc = descriptions[i] if i < len(descriptions) else ""
                    
                    try:
                        filing_dt = datetime.datetime.strptime(f_date_str, "%Y-%m-%d")
                    except Exception:
                        filing_dt = datetime.datetime.utcnow()
                    
                    doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_no_dashes}/{primary_doc}"
                    
                    # Fetch primary document
                    raw_text, sections = self._download_and_parse_filing(doc_url, form_type)
                    if not raw_text:
                        continue
                    
                    doc_title = f"{company_name} - SEC Form {form_type} ({f_date_str})"
                    if doc_desc:
                        doc_title += f" - {doc_desc}"
                        
                    ingested_doc = IngestedDoc(
                        source_type="sec_edgar",
                        doc_type=form_type,
                        title=doc_title,
                        url=doc_url,
                        filing_date=filing_dt,
                        raw_text=raw_text,
                        metadata={
                            "cik": formatted_cik,
                            "ticker": ticker,
                            "accessionNumber": accession,
                            "form": form_type,
                            "filingDate": f_date_str,
                            "primaryDocument": primary_doc
                        },
                        sections=sections
                    )
                    docs.append(ingested_doc)
                    count += 1
                    if count >= limit:
                        break
                        
        except Exception as e:
            logger.error(f"Error fetching SEC filings for {ticker} ({cik}): {e}", exc_info=True)
            
        return docs

    def fetch_earnings_decks(
        self,
        ticker: str,
        cik: str,
        company_name: str,
        limit: int = 5
    ) -> List[IngestedDoc]:
        """
        Earnings/investor-presentation exhibits attached to 8-K filings
        (Item 2.02 "Results of Operations" 8-Ks commonly include one).
        Unlike fetch_documents() above (which only ever looks at each
        filing's primaryDocument), this enumerates the OTHER documents in
        the same accession via EDGAR's per-filing index page, since the
        deck is filed as a separate exhibit, not the primary document.

        Verified against real filings: EDGAR's index page lists exhibits
        with a Type column (e.g. "EX-99") and a free-text Description
        (e.g. "EARNINGS PRESENTATION SLIDES") — filenames alone are NOT a
        reliable signal (large filers often use non-descriptive names).
        Large-cap filers typically file these as HTML reproductions, not
        native PDFs, but pdfplumber handles the PDF case for filers that do.
        """
        formatted_cik = str(cik).zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{formatted_cik}.json"

        docs: List[IngestedDoc] = []
        try:
            time.sleep(settings.SEC_RATE_LIMIT_DELAY)
            with httpx.Client(timeout=15.0, headers=self.headers) as client:
                response = client.get(url)
                if response.status_code != 200:
                    logger.warning(f"SEC API returned {response.status_code} for CIK {formatted_cik}")
                    return []

                data = response.json()
                filings = data.get("filings", {}).get("recent", {})
                if not filings:
                    return []

                forms = filings.get("form", [])
                accession_numbers = filings.get("accessionNumber", [])
                filing_dates = filings.get("filingDate", [])
                cik_no_zeros = str(int(cik))

                count = 0
                for i in range(len(forms)):
                    if forms[i] != "8-K":
                        continue

                    accession = accession_numbers[i]
                    accession_no_dashes = accession.replace("-", "")
                    f_date_str = filing_dates[i]
                    try:
                        filing_dt = datetime.datetime.strptime(f_date_str, "%Y-%m-%d")
                    except Exception:
                        filing_dt = datetime.datetime.utcnow()

                    exhibit_filenames = self._find_presentation_exhibits(cik_no_zeros, accession, accession_no_dashes)
                    for fname in exhibit_filenames:
                        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_no_dashes}/{fname}"

                        # Some 8-Ks file the earnings release itself as the
                        # primaryDocument with no separate exhibit — if this
                        # exhibit happens to be the same URL fetch_documents()
                        # already ingested under source_type="sec_edgar",
                        # _ingest_docs' URL dedup will silently skip it below.
                        if fname.lower().endswith(".pdf"):
                            raw_text = self._download_and_parse_pdf(doc_url)
                        else:
                            raw_text = self._download_and_parse_generic_html(doc_url)

                        if not raw_text or len(raw_text) < 100:
                            continue

                        docs.append(IngestedDoc(
                            source_type="earnings_deck",
                            doc_type="8-K-EX99",
                            title=f"{company_name} - Earnings/Investor Presentation ({f_date_str})",
                            url=doc_url,
                            filing_date=filing_dt,
                            raw_text=raw_text,
                            metadata={
                                "cik": formatted_cik,
                                "ticker": ticker,
                                "accessionNumber": accession,
                                "filingDate": f_date_str,
                                "exhibitFile": fname
                            },
                            sections={"Exhibit Content": raw_text[:30000]}
                        ))
                        count += 1
                        if count >= limit:
                            return docs

        except Exception as e:
            logger.error(f"Error fetching earnings decks for {ticker} ({cik}): {e}", exc_info=True)

        return docs

    def _find_presentation_exhibits(self, cik_no_zeros: str, accession: str, accession_no_dashes: str) -> List[str]:
        """Parses an 8-K's EDGAR index page for exhibit rows tagged EX-99
        or whose description reads like an investor/earnings presentation."""
        index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_no_dashes}/{accession}-index.html"
        try:
            time.sleep(settings.SEC_RATE_LIMIT_DELAY)
            with httpx.Client(timeout=20.0, headers=self.doc_headers, follow_redirects=True) as client:
                res = client.get(index_url)
                if res.status_code != 200:
                    return []

                soup = BeautifulSoup(res.text, "html.parser")
                matches: List[str] = []
                for row in soup.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) < 3:
                        continue
                    link = row.find("a", href=True)
                    if not link:
                        continue
                    filename = link["href"].split("/")[-1]
                    if not filename.lower().endswith((".htm", ".html", ".pdf")):
                        continue

                    row_text = " ".join(c.get_text(" ", strip=True) for c in cells).upper()
                    is_ex99 = "EX-99" in row_text
                    has_keyword = any(k in row_text for k in ("PRESENTATION", "EARNINGS", "SUPPLEMENT", "SLIDES"))
                    if is_ex99 or has_keyword:
                        matches.append(filename)
                return matches
        except Exception as e:
            logger.warning(f"Failed to fetch/parse filing index {index_url}: {e}")
            return []

    def _download_and_parse_generic_html(self, doc_url: str) -> str:
        try:
            time.sleep(settings.SEC_RATE_LIMIT_DELAY)
            with httpx.Client(timeout=25.0, headers=self.doc_headers, follow_redirects=True) as client:
                res = client.get(doc_url)
                if res.status_code != 200:
                    return ""
                soup = BeautifulSoup(res.text, "html.parser")
                for tag in soup(["script", "style", "meta", "noscript"]):
                    tag.decompose()
                text = soup.get_text(separator="\n")
                text = re.sub(r"\n\s*\n+", "\n\n", text)
                text = re.sub(r"[ \t]+", " ", text)
                return text
        except Exception as e:
            logger.warning(f"Failed to download/parse exhibit {doc_url}: {e}")
            return ""

    def _download_and_parse_pdf(self, doc_url: str) -> str:
        try:
            time.sleep(settings.SEC_RATE_LIMIT_DELAY)
            with httpx.Client(timeout=30.0, headers=self.doc_headers, follow_redirects=True) as client:
                res = client.get(doc_url)
                if res.status_code != 200:
                    return ""
                text_parts = []
                with pdfplumber.open(io.BytesIO(res.content)) as pdf:
                    for page in pdf.pages:
                        page_text = page.extract_text() or ""
                        if page_text:
                            text_parts.append(page_text)
                return "\n\n".join(text_parts)
        except Exception as e:
            logger.warning(f"Failed to download/parse PDF exhibit {doc_url}: {e}")
            return ""

    def _download_and_parse_filing(self, doc_url: str, form_type: str) -> tuple[str, Dict[str, str]]:
        try:
            time.sleep(settings.SEC_RATE_LIMIT_DELAY)
            with httpx.Client(timeout=25.0, headers=self.doc_headers, follow_redirects=True) as client:
                res = client.get(doc_url)
                if res.status_code != 200:
                    return "", {}
                
                content = res.text
                soup = BeautifulSoup(content, "html.parser")
                
                # Remove scripts, styles, xml tags
                for tag in soup(["script", "style", "meta", "noscript"]):
                    tag.decompose()
                
                text = soup.get_text(separator="\n")
                # Normalize spaces
                text = re.sub(r"\n\s*\n+", "\n\n", text)
                text = re.sub(r"[ \t]+", " ", text)
                
                # Section splitting based on common SEC Item headers
                sections: Dict[str, str] = {}
                
                if form_type in ["10-K", "10-Q"]:
                    # Look for Item 1, Item 1A, Item 7
                    sections = self._extract_10k_sections(text)
                elif form_type == "8-K":
                    sections = self._extract_8k_sections(text)
                elif form_type == "20-F":
                    sections = self._extract_20f_sections(text)
                # 6-K has no standardized Item numbering (often just wraps an
                # exhibit/press release) — falls through to the generic slice below

                if not sections:
                    # Fallback section: entire text chunk
                    sections["Main Filing Content"] = text[:30000]
                    
                return text, sections
        except Exception as e:
            logger.warning(f"Failed to download/parse filing {doc_url}: {e}")
            return "", {}

    def _extract_10k_sections(self, text: str) -> Dict[str, str]:
        sections = {}
        # Pattern for Item 1, 1A, 7, 7A
        patterns = {
            "Item 1. Business": r"(?:item\s+1\.\s+business)(.*?)(?:item\s+1a|\Z)",
            "Item 1A. Risk Factors": r"(?:item\s+1a\.\s+risk\s+factors)(.*?)(?:item\s+1b|item\s+2|\Z)",
            "Item 7. MD&A": r"(?:item\s+7\.\s+management['’]s\s+discussion)(.*?)(?:item\s+7a|item\s+8|\Z)",
        }
        for name, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                extracted = match.group(1).strip()
                if len(extracted) > 100:
                    sections[name] = extracted[:25000] # keep first 25k chars
        return sections

    def _extract_20f_sections(self, text: str) -> Dict[str, str]:
        sections = {}
        # Form 20-F (foreign private issuer annual report) uses different
        # item numbering than 10-K: Item 4 ~ business overview, Item 5 ~ MD&A
        patterns = {
            "Item 4. Information on the Company": r"(?:item\s+4\.\s+information\s+on\s+the\s+company)(.*?)(?:item\s+4a|item\s+5|\Z)",
            "Item 5. Operating and Financial Review": r"(?:item\s+5\.\s+operating\s+and\s+financial\s+review)(.*?)(?:item\s+6|\Z)",
        }
        for name, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                extracted = match.group(1).strip()
                if len(extracted) > 100:
                    sections[name] = extracted[:25000]
        return sections

    def _extract_8k_sections(self, text: str) -> Dict[str, str]:
        sections = {}
        # 8-K Items e.g. Item 1.01, Item 2.02, Item 7.01, Item 8.01
        matches = re.finditer(r"(Item\s+\d+\.\d+[\s\w,–-]+)\n", text, re.IGNORECASE)
        items_pos = [(m.group(1).strip(), m.start()) for m in matches]
        
        for idx, (title, start_pos) in enumerate(items_pos):
            end_pos = items_pos[idx + 1][1] if idx + 1 < len(items_pos) else start_pos + 10000
            section_content = text[start_pos:end_pos].strip()
            if len(section_content) > 50:
                sections[title] = section_content[:15000]
        return sections
