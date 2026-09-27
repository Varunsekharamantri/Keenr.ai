import re
import urllib.parse
import datetime
import logging
import httpx
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional
from .base import BaseIngestionAdapter, IngestedDoc, is_relevant_to_company

logger = logging.getLogger(__name__)

class NewsRssAdapter(BaseIngestionAdapter):
    """
    News & RSS Ingestion Adapter
    Pulls live news articles and press releases for companies using RSS feeds.
    Zero-paywall & no external paid API key requirement.
    """
    def __init__(self):
        super().__init__(source_name="news_rss")
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml, */*"
        }

    def fetch_documents(
        self,
        ticker: str,
        cik: str,
        company_name: str,
        limit: int = 5,
        aliases: Optional[List[str]] = None
    ) -> List[IngestedDoc]:
        docs: List[IngestedDoc] = []
        
        # Build search query for technology / strategic disclosures
        # e.g., "JPMorgan" AND (cloud OR AI OR technology OR digital OR modernization OR partner)
        clean_name = company_name.replace("&", "and").replace(",", "").split(" Inc")[0].split(" Corp")[0].strip()
        query = f'"{clean_name}" AND (cloud OR "artificial intelligence" OR AI OR technology OR digital OR platform OR vendor OR cyber)'
        encoded_query = urllib.parse.quote(query)
        rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
        
        try:
            with httpx.Client(timeout=15.0, headers=self.headers, follow_redirects=True) as client:
                res = client.get(rss_url)
                if res.status_code != 200:
                    logger.warning(f"Google News RSS returned {res.status_code} for {company_name}")
                    return []
                
                import xml.etree.ElementTree as ET
                try:
                    root = ET.fromstring(res.content)
                    channel = root.find("channel")
                    items = channel.findall("item") if channel is not None else root.findall(".//item")
                except Exception as xml_err:
                    logger.warning(f"XML parse fallback for {company_name}: {xml_err}")
                    soup = BeautifulSoup(res.content, "html.parser")
                    items = soup.find_all("item")

                for item in items[:limit]:
                    if hasattr(item, "find"):
                        title_el = item.find("title")
                        link_el = item.find("link")
                        pubdate_el = item.find("pubDate")
                        desc_el = item.find("description")
                        source_el = item.find("source")

                        title = title_el.text if title_el is not None and title_el.text else f"News for {company_name}"
                        link = link_el.text if link_el is not None and link_el.text else ""
                        pub_str = pubdate_el.text if pubdate_el is not None and pubdate_el.text else ""
                        desc_raw = desc_el.text if desc_el is not None and desc_el.text else ""
                        source_name = source_el.text if source_el is not None and source_el.text else "News Feed"
                    else:
                        title = getattr(item, "title", f"News for {company_name}")
                        link = getattr(item, "link", "")
                        pub_str = getattr(item, "pubDate", "")
                        desc_raw = getattr(item, "description", "")
                        source_name = "News Feed"
                    
                    # Parse publication date (e.g., 'Wed, 25 Feb 2026 14:30:00 GMT')
                    pub_dt = datetime.datetime.utcnow()
                    if pub_str:
                        try:
                            # Parse RFC-822 / GMT dates
                            pub_clean = pub_str.strip()
                            pub_dt = datetime.datetime.strptime(pub_clean[:25], "%a, %d %b %Y %H:%M:%S")
                        except Exception:
                            pub_dt = datetime.datetime.utcnow()
                    
                    desc_text = ""
                    if desc_raw:
                        desc_soup = BeautifulSoup(desc_raw, "html.parser")
                        desc_text = desc_soup.get_text(separator=" ").strip()
                    
                    full_text = f"{title}\n\n{desc_text}"

                    if not is_relevant_to_company(full_text, company_name, ticker, aliases):
                        logger.info(f"Discarding News RSS result not actually about {company_name}: {title[:80]}")
                        continue

                    doc = IngestedDoc(
                        source_type="news_rss",
                        doc_type="news_article",
                        title=f"[{source_name}] {title}",
                        url=link,
                        filing_date=pub_dt,
                        raw_text=full_text,
                        metadata={
                            "ticker": ticker,
                            "company_name": company_name,
                            "source_publisher": source_name,
                            "pub_date_raw": pub_str
                        },
                        sections={
                            "Headline": title,
                            "Snippet": desc_text
                        }
                    )
                    docs.append(doc)
                    
        except Exception as e:
            logger.error(f"Error fetching News RSS for {company_name}: {e}")
            
        return docs
