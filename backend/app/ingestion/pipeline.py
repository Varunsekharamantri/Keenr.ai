import os
import json
import datetime
import logging
import threading
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

# Duck-typed stand-in for a Company, safe to hand to worker threads: plain
# values only, so nothing can trigger a lazy load off the session.
CompanySnapshot = namedtuple("CompanySnapshot", "id name ticker cik aliases")
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..config import settings
from ..models.schema import Company, RawDocument, Event, IngestionJob, SourceRun
from .sec_edgar import SecEdgarAdapter
from .news_rss import NewsRssAdapter
from .exa_news import ExaNewsAdapter
from .ir_press import IrPressAdapter
from .career_pages import CareerPageAdapter
from .patents import PatentAdapter
from ..extraction.extractor import signal_extractor

logger = logging.getLogger(__name__)

class IngestionPipeline:
    """
    Orchestrates ingestion across public data sources (SEC EDGAR filings +
    earnings-deck exhibits, News RSS, Exa neural search for news + IR press
    releases + career pages, and patents), stores raw files, runs the NLP
    extractor, and writes structured events to DB.
    """
    def __init__(self):
        self.sec_adapter = SecEdgarAdapter()
        self.news_adapter = NewsRssAdapter()
        self.exa_adapter = ExaNewsAdapter()
        self.ir_adapter = IrPressAdapter()
        self.career_adapter = CareerPageAdapter()
        self.patent_adapter = PatentAdapter()

    # ------------------------------------------------------------------
    # Parallel rebuild path
    #
    # A sequential full rebuild measured ~13 min/company (199 companies =
    # 16-43h). The cost is almost entirely waiting on network: one Groq
    # extraction call per document (3-15s each) plus source fetches. That
    # parallelizes well, but SQLAlchemy sessions are not thread-safe and a
    # long LLM call inside an open SQLite transaction would hold write locks
    # for minutes.
    #
    # So: fetch AND extract in worker threads with no DB access at all
    # (using a plain company snapshot rather than an ORM object, so nothing
    # can lazy-load), then write everything from the main thread in short
    # transactions. The nightly sequential path is untouched.
    # ------------------------------------------------------------------

    def _prepare_company(self, snapshot: "CompanySnapshot", limit: int, enable_exa: bool,
                         start_published_date: Optional[str], source_stats: Optional[dict],
                         stats_lock: Optional[threading.Lock],
                         known_urls: Optional[set] = None) -> list:
        """
        Runs in a worker thread. Fetches every source and runs extraction,
        returning [(IngestedDoc, [Event])]. Touches no Session.
        """
        prepared = []

        def record(source_name: str, docs_n: int, events_n: int, error: Optional[str]):
            if source_stats is None:
                return
            with stats_lock:
                s = source_stats.setdefault(source_name, {
                    "companies_attempted": 0, "docs_ingested": 0,
                    "events_extracted": 0, "error_count": 0, "last_error": None,
                })
                s["companies_attempted"] += 1
                s["docs_ingested"] += docs_n
                s["events_extracted"] += events_n
                if error:
                    s["error_count"] += 1
                    s["last_error"] = error[:500]

        def run(source_name: str, fetch):
            try:
                docs = fetch() or []
            except Exception as ex:
                logger.error(f"[{source_name}] fetch failed for {snapshot.ticker or snapshot.name}: {ex}")
                record(source_name, 0, 0, f"{snapshot.ticker or snapshot.name}: {ex}")
                return
            n_events = 0
            for doc in docs:
                # Skip documents already stored. _persist_prepared drops them on
                # the URL check anyway, so extracting first meant paying for an
                # LLM call whose result was then thrown away - the same article
                # comes back from a news search every day.
                if known_urls is not None and doc.url in known_urls:
                    continue
                try:
                    events = signal_extractor.extract_from_doc(
                        company=snapshot, raw_doc_id=None, doc_title=doc.title,
                        doc_url=doc.url, source_type=doc.source_type,
                        filing_date=doc.filing_date, sections=doc.sections,
                        full_text=doc.raw_text,
                    )
                except Exception as ex:
                    logger.warning(f"[{source_name}] extraction failed for {doc.url[:60]}: {ex}")
                    events = []
                prepared.append((doc, events))
                n_events += len(events)
            record(source_name, len(docs), n_events, None)

        if snapshot.cik:
            run("sec_edgar", lambda: self.sec_adapter.fetch_documents(
                ticker=snapshot.ticker, cik=snapshot.cik, company_name=snapshot.name, limit=limit))
            run("earnings_deck", lambda: self.sec_adapter.fetch_earnings_decks(
                ticker=snapshot.ticker, cik=snapshot.cik, company_name=snapshot.name, limit=limit))
        run("news_rss", lambda: self.news_adapter.fetch_documents(
            ticker=snapshot.ticker, cik=snapshot.cik, company_name=snapshot.name,
            limit=limit, aliases=snapshot.aliases))
        if enable_exa:
            for name, adapter in (("exa_news", self.exa_adapter),
                                  ("ir_press", self.ir_adapter),
                                  ("career_pages", self.career_adapter)):
                if adapter.quota_exhausted or adapter.budget_exhausted:
                    continue
                run(name, lambda a=adapter: a.fetch_documents(
                    ticker=snapshot.ticker, cik=snapshot.cik, company_name=snapshot.name,
                    limit=limit, aliases=snapshot.aliases,
                    start_published_date=start_published_date))
        run("patents", lambda: self.patent_adapter.fetch_documents(
            ticker=snapshot.ticker, cik=snapshot.cik, company_name=snapshot.name,
            limit=limit, aliases=snapshot.aliases))
        return prepared

    def _persist_prepared(self, db: Session, company: Company, prepared: list) -> tuple:
        """Main-thread write of what the workers prepared. Short transaction."""
        docs_added = events_added = 0
        name_slug = company.ticker or company.id[:8]

        for doc, events in prepared:
            if db.query(RawDocument).filter(RawDocument.url == doc.url).first():
                continue
            local_path = None
            if settings.RAW_STORE_ENABLED:
                local_fname = (f"{doc.source_type}_{name_slug}_{doc.doc_type}_"
                               f"{doc.filing_date.strftime('%Y%m%d')}_{abs(hash(doc.url)) % 10000}.txt")
                local_path = settings.RAW_STORE_DIR / local_fname
                with open(local_path, "w", encoding="utf-8", errors="ignore") as f:
                    f.write(doc.raw_text)

            raw_doc = RawDocument(
                company_id=company.id, source_type=doc.source_type, doc_type=doc.doc_type,
                title=doc.title, url=doc.url,
                local_path=str(local_path) if local_path else None,
                filing_date=doc.filing_date, metadata_json=doc.metadata,
                raw_text_snippet=doc.raw_text[:1000],
            )
            db.add(raw_doc)
            db.flush()
            docs_added += 1

            for ev in events:
                ev.company_id = company.id      # snapshot id, re-affirmed
                ev.raw_doc_id = raw_doc.id      # only knowable after the flush
                db.add(ev)
            events_added += len(events)

        db.commit()
        return docs_added, events_added

    def _ingest_docs(self, db: Session, company: Company, docs: list) -> tuple:
        """Shared persist+extract logic for any adapter's fetched documents."""
        docs_added = 0
        events_added = 0
        name_slug = company.ticker or company.id[:8]

        for doc in docs:
            existing_raw = db.query(RawDocument).filter(RawDocument.url == doc.url).first()
            if existing_raw:
                continue

            # The archive copy is write-only (local_path is never read back), so
            # cloud runners with ephemeral disks can skip it entirely.
            local_path = None
            if settings.RAW_STORE_ENABLED:
                local_fname = f"{doc.source_type}_{name_slug}_{doc.doc_type}_{doc.filing_date.strftime('%Y%m%d')}_{abs(hash(doc.url)) % 10000}.txt"
                local_path = settings.RAW_STORE_DIR / local_fname
                with open(local_path, "w", encoding="utf-8", errors="ignore") as f:
                    f.write(doc.raw_text)

            raw_doc = RawDocument(
                company_id=company.id,
                source_type=doc.source_type,
                doc_type=doc.doc_type,
                title=doc.title,
                url=doc.url,
                local_path=str(local_path) if local_path else None,
                filing_date=doc.filing_date,
                metadata_json=doc.metadata,
                raw_text_snippet=doc.raw_text[:1000]
            )
            db.add(raw_doc)
            db.flush()
            docs_added += 1

            extracted_events = signal_extractor.extract_from_doc(
                company=company,
                raw_doc_id=raw_doc.id,
                doc_title=doc.title,
                doc_url=doc.url,
                source_type=doc.source_type,
                filing_date=doc.filing_date,
                sections=doc.sections,
                full_text=doc.raw_text
            )
            for ev in extracted_events:
                db.add(ev)
            events_added += len(extracted_events)

        return docs_added, events_added

    def _run_source(self, db, company, source_name: str, fetch, source_stats: Optional[dict]) -> tuple:
        """
        Fetch + ingest one source for one company, recording the outcome.

        Per-source stats are what make source health measurable: a batch
        IngestionJob rolls every source into one "completed" row, so without
        this an adapter could fail for every company nightly and nothing
        would show it.
        """
        stats = None
        if source_stats is not None:
            stats = source_stats.setdefault(source_name, {
                "companies_attempted": 0, "docs_ingested": 0,
                "events_extracted": 0, "error_count": 0, "last_error": None,
            })
            stats["companies_attempted"] += 1
        try:
            docs = fetch()
            d, e = self._ingest_docs(db, company, docs)
            if stats is not None:
                stats["docs_ingested"] += d
                stats["events_extracted"] += e
            return d, e
        except Exception as ex:
            if stats is not None:
                stats["error_count"] += 1
                stats["last_error"] = f"{company.ticker or company.name}: {ex}"[:500]
            logger.error(f"Error during {source_name} ingestion for {company.ticker}: {ex}")
            return 0, 0

    def run_company_ingestion(
        self,
        db: Session,
        company: Company,
        source_type: str = "all", # "sec_edgar", "news_rss", "all"
        limit: int = 5,
        job_id: Optional[str] = None,
        enable_exa: bool = True,
        source_stats: Optional[dict] = None,
        start_published_date: Optional[str] = None,
        sources: Optional[set] = None,
    ) -> dict:
        total_docs = 0
        total_events = 0

        def wants(name: str) -> bool:
            # `source_type` picks one source (or "all"); `sources` narrows "all"
            # to the set the scheduler decided is worth polling today.
            if source_type not in (name, "all"):
                return False
            return sources is None or name in sources

        def account(result):
            nonlocal total_docs, total_events
            total_docs += result[0]
            total_events += result[1]

        # 1. SEC EDGAR (only possible for companies with a resolved CIK)
        if wants("sec_edgar") and company.cik:
            account(self._run_source(db, company, "sec_edgar", lambda: self.sec_adapter.fetch_documents(
                ticker=company.ticker, cik=company.cik, company_name=company.name, limit=limit
            ), source_stats))

        # 1b. SEC earnings-deck exhibits (Item 2.02 8-Ks) — free, low-volume
        # (a handful per company per year), no separate budget needed.
        if wants("earnings_deck") and company.cik:
            account(self._run_source(db, company, "earnings_deck", lambda: self.sec_adapter.fetch_earnings_decks(
                ticker=company.ticker, cik=company.cik, company_name=company.name, limit=limit
            ), source_stats))

        # 2. Google News RSS (free, headline + short snippet). Pull more items
        # than usual if Exa's credits are known to be exhausted, to partly
        # compensate for the lost full-text coverage.
        if wants("news_rss"):
            news_limit = limit + 3 if self.exa_adapter.quota_exhausted else limit
            account(self._run_source(db, company, "news_rss", lambda: self.news_adapter.fetch_documents(
                ticker=company.ticker, cik=company.cik, company_name=company.name,
                limit=news_limit, aliases=company.aliases
            ), source_stats))

        # 3. Exa neural search (full article text, richer signal than RSS).
        # Gated by enable_exa so batch runs can ration Exa calls across a
        # daily sub-budget instead of spending the whole month's cap at once.
        exa_ready = not self.exa_adapter.quota_exhausted and not self.exa_adapter.budget_exhausted
        if wants("exa_news") and enable_exa and exa_ready:
            d, e = self._run_source(db, company, "exa_news", lambda: self.exa_adapter.fetch_documents(
                ticker=company.ticker, cik=company.cik, company_name=company.name,
                limit=limit, aliases=company.aliases, start_published_date=start_published_date
            ), source_stats)
            account((d, e))
            company.last_exa_ingest_at = datetime.datetime.utcnow()
            db.add(company)

        # 4. Exa neural search for IR press releases — own separate budget
        # (see ExaBudgetTracker) so it doesn't compete with news-Exa's pool,
        # but piggybacks on the SAME daily rotation eligibility (enable_exa)
        # since both are Exa consumers rationed against the same daily-slot
        # concept; run_batch_ingestion sizes that rotation from whichever of
        # the budgets has the most remaining, so a depleted news budget alone
        # never blocks IR eligibility.
        ir_ready = not self.ir_adapter.quota_exhausted and not self.ir_adapter.budget_exhausted
        if wants("ir_press") and enable_exa and ir_ready:
            account(self._run_source(db, company, "ir_press", lambda: self.ir_adapter.fetch_documents(
                ticker=company.ticker, cik=company.cik, company_name=company.name,
                limit=limit, aliases=company.aliases, start_published_date=start_published_date
            ), source_stats))

        # 5. Exa neural search for career-page / hiring-intent signals — own
        # separate budget, same rotation-eligibility reasoning as IR press.
        career_ready = not self.career_adapter.quota_exhausted and not self.career_adapter.budget_exhausted
        if wants("career_pages") and enable_exa and career_ready:
            account(self._run_source(db, company, "career_pages", lambda: self.career_adapter.fetch_documents(
                ticker=company.ticker, cik=company.cik, company_name=company.name,
                limit=limit, aliases=company.aliases, start_published_date=start_published_date
            ), source_stats))

        # 6. Patents (USPTO/PatentsView) — free API, no Exa budget involved.
        # The adapter itself is a no-op (returns []) until PATENTSVIEW_API_KEY
        # is configured, so this is always safe to include in "all".
        if wants("patents"):
            account(self._run_source(db, company, "patents", lambda: self.patent_adapter.fetch_documents(
                ticker=company.ticker, cik=company.cik, company_name=company.name,
                limit=limit, aliases=company.aliases
            ), source_stats))

        db.commit()
        return {
            "company_id": company.id,
            "ticker": company.ticker,
            "name": company.name,
            "docs_ingested": total_docs,
            "events_extracted": total_events
        }

    def run_batch_parallel(
        self,
        db: Session,
        limit_per_company: int = 3,
        sector_filter: Optional[str] = None,
        only_uncovered: bool = False,
        start_published_date: Optional[str] = None,
        max_workers: int = 5,
        progress_every: int = 5,
    ) -> IngestionJob:
        """
        Concurrent full-coverage rebuild: every company gets every source.
        Fetch+extract run in `max_workers` threads; all DB writes stay on the
        main thread. Well under SEC's 10 req/s guidance at these worker counts.
        """
        job = IngestionJob(job_type="parallel_rebuild", status="running",
                           started_at=datetime.datetime.utcnow())
        db.add(job)
        db.commit()

        query = db.query(Company)
        if sector_filter:
            query = query.filter(Company.sector == sector_filter)
        if only_uncovered:
            covered = db.query(Event.company_id).distinct().subquery()
            query = query.filter(~Company.id.in_(select(covered.c.company_id)))
        companies = query.all()
        by_id = {c.id: c for c in companies}
        snapshots = [CompanySnapshot(c.id, c.name, c.ticker, c.cik, list(c.aliases or []))
                     for c in companies]

        source_stats: dict = {}
        stats_lock = threading.Lock()
        run_started = datetime.datetime.utcnow()
        total_docs = total_events = done = 0

        logger.info(f"Parallel rebuild: {len(snapshots)} companies, {max_workers} workers, "
                    f"news floor={start_published_date}")
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(self._prepare_company, snap, limit_per_company, True,
                                start_published_date, source_stats, stats_lock): snap
                    for snap in snapshots
                }
                for fut in as_completed(futures):
                    snap = futures[fut]
                    try:
                        prepared = fut.result()
                    except Exception as ex:
                        logger.error(f"Prepare failed for {snap.name}: {ex}")
                        continue
                    d, e = self._persist_prepared(db, by_id[snap.id], prepared)
                    total_docs += d
                    total_events += e
                    done += 1
                    if done % progress_every == 0 or done == len(snapshots):
                        elapsed = (datetime.datetime.utcnow() - run_started).total_seconds()
                        rate = elapsed / done
                        logger.info(f"  [{done}/{len(snapshots)}] docs={total_docs} events={total_events} "
                                    f"({rate:.0f}s/company, ~{rate*(len(snapshots)-done)/60:.0f} min left)")

            job.status = "completed"
        except Exception as ex:
            job.status = "failed"
            job.error_message = str(ex)
            logger.error(f"Parallel rebuild crashed: {ex}", exc_info=True)

        job.items_ingested = total_docs
        job.events_extracted = total_events
        job.finished_at = datetime.datetime.utcnow()

        finished = datetime.datetime.utcnow()
        for source_type_name, stats in source_stats.items():
            db.add(SourceRun(
                job_id=job.id, source_type=source_type_name,
                companies_attempted=stats["companies_attempted"],
                docs_ingested=stats["docs_ingested"],
                events_extracted=stats["events_extracted"],
                error_count=stats["error_count"], last_error=stats["last_error"],
                started_at=run_started, finished_at=finished,
            ))
        db.commit()
        return job

    def run_batch_ingestion(
        self,
        db: Session,
        limit_per_company: int = 3,
        sector_filter: Optional[str] = None,
        only_uncovered: bool = False,
        force_all_sources: bool = False,
        start_published_date: Optional[str] = None,
        sources: Optional[set] = None,
    ) -> IngestionJob:
        job = IngestionJob(
            job_type="batch_pipeline",
            status="running",
            started_at=datetime.datetime.utcnow()
        )
        db.add(job)
        db.commit()

        query = db.query(Company)
        if sector_filter:
            query = query.filter(Company.sector == sector_filter)
        if only_uncovered:
            # Catch-up mode: spend the run on companies that have never
            # produced an event, instead of re-polling well-covered names.
            covered = db.query(Event.company_id).distinct().subquery()
            query = query.filter(~Company.id.in_(select(covered.c.company_id)))
        companies = query.all()

        # Ration Exa to a daily sub-budget so the monthly cap doesn't get
        # spent in the first few days of the month. Least-recently-touched
        # companies (by last_exa_ingest_at, NULLs first) go first so coverage
        # rotates fairly across the whole universe over time.
        #
        # News-Exa, IR-Exa, and career-Exa each draw from their OWN separate
        # monthly budget (see ExaBudgetTracker), but share this one
        # rotation-eligibility list (enable_exa) rather than each getting its
        # own — so the slot count must be sized from whichever of the three
        # still has the most room. Sizing it from any single one alone would
        # silently starve the others the moment that one's budget
        # (independently) runs out for the month, even though the others'
        # budgets might be untouched.
        news_usage = self.exa_adapter.get_usage_status()
        ir_usage = self.ir_adapter.get_usage_status()
        career_usage = self.career_adapter.get_usage_status()
        news_slots = min(settings.EXA_DAILY_BUDGET, news_usage["remaining"])
        ir_slots = min(settings.EXA_IR_DAILY_BUDGET, ir_usage["remaining"])
        career_slots = min(settings.EXA_CAREER_DAILY_BUDGET, career_usage["remaining"])
        exa_slots_today = max(0, news_slots, ir_slots, career_slots)
        rotation_order = sorted(
            companies,
            key=lambda c: c.last_exa_ingest_at or datetime.datetime.min
        )
        # force_all_sources is for a clean rebuild: every company gets the rich
        # Exa sources, not just today's rotation slice. The per-adapter
        # budget_exhausted gate still hard-stops spending, so this can't
        # overspend — it just stops rationing *which* companies benefit.
        if force_all_sources:
            exa_eligible_ids = {c.id for c in companies}
            logger.info(f"Batch ingestion (FORCED, no rotation): {len(companies)} companies, all Exa sources")
        else:
            exa_eligible_ids = {c.id for c in rotation_order[:exa_slots_today]}
            logger.info(
                f"Batch ingestion: {len(companies)} companies, "
                f"Exa slots today: {exa_slots_today} (news remaining {news_usage['remaining']}, "
                f"IR remaining {ir_usage['remaining']}, career remaining {career_usage['remaining']})"
            )

        total_docs = 0
        total_events = 0
        source_stats: dict = {}
        run_started = datetime.datetime.utcnow()
        try:
            for comp in companies:
                res = self.run_company_ingestion(
                    db=db,
                    company=comp,
                    source_type="all",
                    limit=limit_per_company,
                    job_id=job.id,
                    enable_exa=comp.id in exa_eligible_ids,
                    source_stats=source_stats,
                    start_published_date=start_published_date,
                    sources=sources,
                )
                total_docs += res["docs_ingested"]
                total_events += res["events_extracted"]

            job.status = "completed"
            job.items_ingested = total_docs
            job.events_extracted = total_events
            job.finished_at = datetime.datetime.utcnow()
        except Exception as e:
            job.status = "failed"
            job.error_message = str(e)
            job.finished_at = datetime.datetime.utcnow()

        # One SourceRun row per source for this batch — the per-source
        # reliability history the health panel reads.
        finished = datetime.datetime.utcnow()
        for source_type_name, stats in source_stats.items():
            db.add(SourceRun(
                job_id=job.id,
                source_type=source_type_name,
                companies_attempted=stats["companies_attempted"],
                docs_ingested=stats["docs_ingested"],
                events_extracted=stats["events_extracted"],
                error_count=stats["error_count"],
                last_error=stats["last_error"],
                started_at=run_started,
                finished_at=finished,
            ))

        db.commit()
        return job
