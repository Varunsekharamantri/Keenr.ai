import datetime
import uuid
from typing import List, Optional
from pydantic import BaseModel, Field
from sqlalchemy import (
    Column,
    String,
    Text,
    DateTime,
    Float,
    Integer,
    Boolean,
    ForeignKey,
    JSON,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

def generate_uuid() -> str:
    return str(uuid.uuid4())

# ==========================================
# SQLAlchemy ORM Models
# ==========================================

class Company(Base):
    __tablename__ = "companies"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(255), nullable=False, index=True)
    ticker = Column(String(20), nullable=True, unique=True, index=True)
    cik = Column(String(20), nullable=True, unique=True, index=True)
    industry = Column(String(100), nullable=False, index=True)
    sector = Column(String(100), nullable=False, index=True)
    # Headquarters. region is the coarse Americas / Rest of World split the
    # dashboard filters on; country keeps the detail behind it.
    country = Column(String(80), nullable=True, index=True)
    region = Column(String(40), nullable=True, index=True)
    vertical = Column(String(60), nullable=True, index=True)       # BFSI star: Banking, Insurance, ...
    sub_industry = Column(String(80), nullable=True, index=True)   # BFSI planet: US Regional Banks, ...
    naics = Column(String(20), nullable=True)
    aliases = Column(JSON, default=list) # e.g. ["JPMorgan", "Chase Bank"]
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    last_exa_ingest_at = Column(DateTime, nullable=True, index=True)  # drives fair daily Exa rotation

    # Relationships
    raw_documents = relationship("RawDocument", back_populates="company", cascade="all, delete-orphan")
    events = relationship("Event", back_populates="company", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "ticker": self.ticker,
            "cik": self.cik,
            "industry": self.industry,
            "sector": self.sector,
            "country": self.country,
            "region": self.region,
            "vertical": self.vertical,
            "sub_industry": self.sub_industry,
            "naics": self.naics,
            "aliases": self.aliases or [],
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

class RawDocument(Base):
    __tablename__ = "raw_documents"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    company_id = Column(String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type = Column(String(50), nullable=False, index=True) # "sec_edgar", "news_rss"
    doc_type = Column(String(50), nullable=False) # "10-K", "10-Q", "8-K", "news_article"
    title = Column(String(500), nullable=False)
    url = Column(String(1000), nullable=False)
    local_path = Column(String(500), nullable=True)
    filing_date = Column(DateTime, nullable=True, index=True)
    metadata_json = Column(JSON, default=dict)
    raw_text_snippet = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    company = relationship("Company", back_populates="raw_documents")
    events = relationship("Event", back_populates="raw_document", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "source_type": self.source_type,
            "doc_type": self.doc_type,
            "title": self.title,
            "url": self.url,
            "local_path": self.local_path,
            "filing_date": self.filing_date.isoformat() if self.filing_date else None,
            "metadata": self.metadata_json or {},
            "raw_text_snippet": self.raw_text_snippet,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

class Event(Base):
    __tablename__ = "events"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    company_id = Column(String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    raw_doc_id = Column(String(36), ForeignKey("raw_documents.id", ondelete="SET NULL"), nullable=True, index=True)
    
    initiative_id = Column(String(100), nullable=False, index=True) # e.g. "cloud_migration"
    initiative_name = Column(String(200), nullable=False)
    category_id = Column(String(100), nullable=False, index=True) # e.g. "tech_initiatives"
    category_name = Column(String(200), nullable=False)
    it_offering = Column(String(300), nullable=True) # IT vendor match

    title = Column(String(500), nullable=False)
    quote_text = Column(Text, nullable=False) # exact sentence/paragraph citation
    context_text = Column(Text, nullable=True) # section context
    occurred_at = Column(DateTime, nullable=False, index=True)
    source_type = Column(String(50), nullable=False) # "sec_edgar", "news_rss"
    source_url = Column(String(1000), nullable=False)
    
    confidence = Column(Float, default=0.85) # 0.0 - 1.0
    spend_amount = Column(String(100), nullable=True) # e.g. "$200M"
    timing_horizon = Column(String(100), nullable=True) # e.g. "Q2 2026", "multi-year"
    key_entities = Column(JSON, default=list) # e.g. ["AWS", "Kubernetes"]
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    company = relationship("Company", back_populates="events")
    raw_document = relationship("RawDocument", back_populates="events")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "company_name": self.company.name if self.company else None,
            "company_ticker": self.company.ticker if self.company else None,
            "company_sector": self.company.sector if self.company else None,
            "company_industry": self.company.industry if self.company else None,
            "raw_doc_id": self.raw_doc_id,
            # The real headline of the source document. `title` below is a
            # synthesized label ("<Company> <Initiative> Signal"), which is
            # useful for grouping but is NOT what the article said — the UI
            # shows this headline and falls back to `title` only when absent.
            "doc_headline": self.raw_document.title if self.raw_document else None,
            "doc_published_at": (
                self.raw_document.filing_date.isoformat()
                if self.raw_document and self.raw_document.filing_date else None
            ),
            # Article preview image, when the page publishes one. Best-effort:
            # the UI falls back to category artwork when this is None.
            "doc_image": ((self.raw_document.metadata_json or {}).get("image_url")
                          if self.raw_document else None),
            "initiative_id": self.initiative_id,
            "initiative_name": self.initiative_name,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "it_offering": self.it_offering,
            "title": self.title,
            "quote_text": self.quote_text,
            "context_text": self.context_text,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "confidence": round(self.confidence, 2) if self.confidence else 0.85,
            "spend_amount": self.spend_amount,
            "timing_horizon": self.timing_horizon,
            "key_entities": self.key_entities or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    job_type = Column(String(50), nullable=False) # "sec_edgar", "news_rss", "full_scan"
    target_ticker = Column(String(20), nullable=True)
    target_cik = Column(String(20), nullable=True)
    status = Column(String(50), default="running") # "running", "completed", "failed"
    items_ingested = Column(Integer, default=0)
    events_extracted = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "job_type": self.job_type,
            "target_ticker": self.target_ticker,
            "target_cik": self.target_cik,
            "status": self.status,
            "items_ingested": self.items_ingested,
            "events_extracted": self.events_extracted,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class SourceRun(Base):
    """
    Per-source outcome of one batch ingestion run (6 rows/run, not per-company).

    IngestionJob alone can't answer "is a source healthy?" — a batch_pipeline
    row aggregates every source into one record, so a single adapter could fail
    for every company each night and the job would still read "completed".
    """
    __tablename__ = "source_runs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    job_id = Column(String(36), ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), nullable=True, index=True)
    source_type = Column(String(50), nullable=False, index=True)
    companies_attempted = Column(Integer, default=0)
    docs_ingested = Column(Integer, default=0)
    events_extracted = Column(Integer, default=0)
    error_count = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    finished_at = Column(DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "job_id": self.job_id,
            "source_type": self.source_type,
            "companies_attempted": self.companies_attempted,
            "docs_ingested": self.docs_ingested,
            "events_extracted": self.events_extracted,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class Tender(Base):
    """
    A public-procurement IT tender (an actual RFP).

    Deliberately separate from Company/Event: TED's buyers are public bodies
    (ministries, hospitals, city governments), not the private BFSI firms in
    the company universe, so folding them together would corrupt that universe.
    `matched_company_id` links the rare case where a buyer really is a tracked
    company (e.g. a state-owned bank).
    """
    __tablename__ = "tenders"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    source = Column(String(30), nullable=False, index=True)  # "ted" | "sam_gov"
    external_id = Column(String(120), nullable=False, unique=True, index=True)
    title = Column(Text, nullable=False)
    buyer_name = Column(String(400), nullable=True, index=True)
    buyer_country = Column(String(80), nullable=True, index=True)
    cpv_codes = Column(JSON, default=list)
    published_at = Column(DateTime, nullable=True, index=True)
    deadline_at = Column(DateTime, nullable=True)
    value_amount = Column(String(60), nullable=True)
    currency = Column(String(10), nullable=True)
    url = Column(String(1000), nullable=True)
    description = Column(Text, nullable=True)
    matched_company_id = Column(String(36), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    matched_company = relationship("Company")

    def to_dict(self):
        return {
            "id": self.id,
            "source": self.source,
            "external_id": self.external_id,
            "title": self.title,
            "buyer_name": self.buyer_name,
            "buyer_country": self.buyer_country,
            "cpv_codes": self.cpv_codes or [],
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "value_amount": self.value_amount,
            "currency": self.currency,
            "url": self.url,
            "description": self.description,
            "matched_company_id": self.matched_company_id,
            "matched_company_name": self.matched_company.name if self.matched_company else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Signal(Base):
    """One ranked market signal per (company, initiative): the aggregation of
    every Event on that theme into an intent score with explainability."""
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("company_id", "initiative_id", name="uq_signal_company_initiative"),)

    id = Column(String(36), primary_key=True, default=generate_uuid)
    company_id = Column(String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)

    initiative_id = Column(String(100), nullable=False, index=True)
    initiative_name = Column(String(200), nullable=False)
    category_id = Column(String(100), nullable=False, index=True)
    category_name = Column(String(200), nullable=False)
    it_offering = Column(String(300), nullable=True)

    intent_score = Column(Integer, nullable=False, default=0, index=True)
    previous_intent_score = Column(Integer, nullable=True)
    score_breakdown = Column(JSON, default=dict)

    event_count = Column(Integer, default=0)
    source_types = Column(JSON, default=list)
    distinct_source_count = Column(Integer, default=0)
    first_seen_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True, index=True)

    timing_window = Column(String(20), nullable=True)   # "0-3mo" | "3-6mo" | "6-12mo"
    timing_estimate = Column(String(300), nullable=True)
    stated_timing = Column(String(100), nullable=True)
    stated_spend = Column(String(100), nullable=True)

    peer_context = Column(JSON, default=list)
    top_event_ids = Column(JSON, default=list)

    status = Column(String(20), default="active", index=True)  # "active" | "stale"
    computed_at = Column(DateTime, default=datetime.datetime.utcnow)

    company = relationship("Company")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "company_name": self.company.name if self.company else None,
            "company_ticker": self.company.ticker if self.company else None,
            "company_sector": self.company.sector if self.company else None,
            "company_industry": self.company.industry if self.company else None,
            "initiative_id": self.initiative_id,
            "initiative_name": self.initiative_name,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "it_offering": self.it_offering,
            "intent_score": self.intent_score,
            "previous_intent_score": self.previous_intent_score,
            "score_breakdown": self.score_breakdown or {},
            "event_count": self.event_count,
            "source_types": self.source_types or [],
            "distinct_source_count": self.distinct_source_count,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "timing_window": self.timing_window,
            "timing_estimate": self.timing_estimate,
            "stated_timing": self.stated_timing,
            "stated_spend": self.stated_spend,
            "peer_context": self.peer_context or [],
            "top_event_ids": self.top_event_ids or [],
            "status": self.status,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
        }


class Watchlist(Base):
    """An IT vendor's ICP + target accounts. Empty company_ids means "match by
    ICP alone" (every company in icp_sectors)."""
    __tablename__ = "watchlists"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    company_ids = Column(JSON, default=list)
    icp_sectors = Column(JSON, default=list)
    icp_initiative_ids = Column(JSON, default=list)
    alert_threshold = Column(Integer, default=70)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "company_ids": self.company_ids or [],
            "icp_sectors": self.icp_sectors or [],
            "icp_initiative_ids": self.icp_initiative_ids or [],
            "alert_threshold": self.alert_threshold,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    signal_id = Column(String(36), ForeignKey("signals.id", ondelete="CASCADE"), nullable=False, index=True)
    watchlist_id = Column(String(36), ForeignKey("watchlists.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id = Column(String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    initiative_id = Column(String(100), nullable=False)
    intent_score = Column(Integer, nullable=False)
    previous_score = Column(Integer, nullable=True)
    message = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False, index=True)
    delivered_channels = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)

    signal = relationship("Signal")
    watchlist = relationship("Watchlist")
    company = relationship("Company")

    def to_dict(self):
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "watchlist_id": self.watchlist_id,
            "watchlist_name": self.watchlist.name if self.watchlist else None,
            "company_id": self.company_id,
            "company_name": self.company.name if self.company else None,
            "company_ticker": self.company.ticker if self.company else None,
            "initiative_id": self.initiative_id,
            "initiative_name": self.signal.initiative_name if self.signal else None,
            "intent_score": self.intent_score,
            "previous_score": self.previous_score,
            "message": self.message,
            "is_read": bool(self.is_read),
            "delivered_channels": self.delivered_channels or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AppState(Base):
    """
    Small shared state - API budgets, the last daily run. In the database
    rather than in files so a GitHub Actions run (whose disk starts empty) and
    the app read and write the same values. See app/db/state.py.
    """
    __tablename__ = "app_state"

    key = Column(String(100), primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class AISummary(Base):
    """
    Written tile summaries, keyed by a hash of the facts they were written from
    (so a changed fact is a new key, never a stale text). Stored in the database
    so summaries written by the morning run reach the app that shows them.
    """
    __tablename__ = "ai_summaries"

    key = Column(String(40), primary_key=True)
    kind = Column(String(120), nullable=True)
    text = Column(Text, nullable=False)
    source = Column(String(20), default="ai")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Leader(Base):
    """
    A named technology or business leader at a tracked company, taken from
    their public LinkedIn profile via Exa search.

    Nothing here is recalled by a model or guessed. A row exists only when the
    profile's own headline names the company and a senior title together, and
    does not mark the role as a former one ("Ex CIO at ..."). The LinkedIn URL
    is the one the search returned, never one constructed from a name.

    `is_current` flips to False when a later refresh of the company no longer
    finds the person - the row is kept as history rather than deleted.
    """
    __tablename__ = "leaders"
    __table_args__ = (UniqueConstraint("company_id", "profile_key", name="uq_leader_company_profile"),)

    id = Column(String(36), primary_key=True, default=generate_uuid)
    company_id = Column(String(36), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    headline = Column(String(500), nullable=False)       # the profile's own words, verbatim
    functions = Column(JSON, default=list)               # e.g. ["data_ai", "technology"]
    seniority = Column(Integer, default=1)               # 3 C-level, 2 head/EVP/SVP, 1 VP/director
    linkedin_url = Column(String(500), nullable=False)
    profile_key = Column(String(300), nullable=False)    # normalised URL, for de-duplication
    location = Column(String(200), nullable=True)
    source = Column(String(40), default="exa_linkedin")
    is_current = Column(Boolean, default=True, index=True)
    first_seen_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.datetime.utcnow)

    company = relationship("Company")

    def to_dict(self):
        return {
            "id": self.id,
            "company_id": self.company_id,
            "name": self.name,
            "headline": self.headline,
            "functions": self.functions or [],
            "seniority": self.seniority,
            "linkedin_url": self.linkedin_url,
            "location": self.location,
            "source": self.source,
            "is_current": self.is_current,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


class LeaderFetch(Base):
    """
    When a company's leaders were last looked up, and why.

    There is no "needs refresh" flag. A company is due when it has never been
    fetched, when its last fetch is older than the TTL, or when a leadership
    change event was stored after that fetch - computed at the time rather than
    set by the ingestion code, so no ingestion path can forget to set it and a
    crash between steps cannot lose the trigger.
    """
    __tablename__ = "leader_fetches"

    company_id = Column(String(36), ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    reason = Column(String(40), nullable=True)           # initial | leadership news | stale | retry
    status = Column(String(20), nullable=False)          # ok | none_verified | error
    results_seen = Column(Integer, default=0)
    verified = Column(Integer, default=0)
    error = Column(String(300), nullable=True)
    # Every profile the search returned, verified or not - so a rule change can
    # be replayed without spending another search, and any rejection audited.
    candidates = Column(JSON, default=list)


# ==========================================
# Pydantic Request/Response Models
# ==========================================

class CompanyCreate(BaseModel):
    name: str
    ticker: Optional[str] = None
    cik: Optional[str] = None
    industry: str
    sector: str
    naics: Optional[str] = None
    aliases: List[str] = []
    description: Optional[str] = None

class CompanyOut(BaseModel):
    id: str
    name: str
    ticker: Optional[str] = None
    cik: Optional[str] = None
    industry: str
    sector: str
    country: Optional[str] = None
    region: Optional[str] = None
    naics: Optional[str] = None
    aliases: List[str] = []
    description: Optional[str] = None
    events_count: Optional[int] = 0

class EventOut(BaseModel):
    id: str
    company_id: str
    company_name: Optional[str] = None
    company_ticker: Optional[str] = None
    company_sector: Optional[str] = None
    company_industry: Optional[str] = None
    raw_doc_id: Optional[str] = None
    # Real headline + publication date of the source document. Without these
    # the UI can only show the synthesized `title`, which is not what the
    # article said.
    doc_headline: Optional[str] = None
    doc_published_at: Optional[str] = None
    doc_image: Optional[str] = None
    initiative_id: str
    initiative_name: str
    category_id: str
    category_name: str
    it_offering: Optional[str] = None
    title: str
    quote_text: str
    context_text: Optional[str] = None
    occurred_at: Optional[str] = None
    source_type: str
    source_url: str
    confidence: float
    spend_amount: Optional[str] = None
    timing_horizon: Optional[str] = None
    key_entities: List[str] = []
    created_at: Optional[str] = None

class IngestionTriggerRequest(BaseModel):
    company_id: Optional[str] = None
    ticker: Optional[str] = None
    cik: Optional[str] = None
    sector: Optional[str] = None
    source_type: Optional[str] = "all" # "sec_edgar", "news_rss", "all"
    limit_docs: Optional[int] = 5
    # "uncovered" restricts a batch run to companies with zero events so a
    # catch-up pass spends its time on the coverage gap.
    mode: Optional[str] = None


class WatchlistCreate(BaseModel):
    name: str
    description: Optional[str] = None
    company_ids: List[str] = []
    icp_sectors: List[str] = []
    icp_initiative_ids: List[str] = []
    alert_threshold: int = Field(70, ge=0, le=100)

class WatchlistUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    company_ids: Optional[List[str]] = None
    icp_sectors: Optional[List[str]] = None
    icp_initiative_ids: Optional[List[str]] = None
    alert_threshold: Optional[int] = Field(None, ge=0, le=100)

class RecomputeRequest(BaseModel):
    sector: Optional[str] = None
