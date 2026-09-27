"""
Clean rebuild of the event/signal data with provable coverage.

Why a wipe is needed rather than a top-up: _ingest_docs dedupes on
RawDocument.url, so leaving the old raw documents in place would make
re-ingestion skip every URL and produce zero new events. Clearing
raw_documents is what actually allows a genuine re-fetch.

Preserved: companies (including the curated country/region data), watchlists,
tenders. Wiped: events, signals, alerts, raw_documents.

Usage:
    python rebuild_data.py --confirm                 # full BFSI rebuild
    python rebuild_data.py --confirm --limit 5       # smoke test on 5 companies
    python rebuild_data.py --report-only             # coverage report, no changes
"""
import argparse
import datetime
import io
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

# line_buffering=True matters: the plain TextIOWrapper used elsewhere in this
# project re-introduces buffering that `python -u` cannot defeat, which made an
# earlier long background run look completely silent.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))

from sqlalchemy import func
from app.config import settings
from app.db.database import init_db, SessionLocal, engine
from app.models.schema import Alert, Company, Event, RawDocument, Signal, SourceRun
from app.ingestion.pipeline import IngestionPipeline
from app.signals.engine import signal_engine


def backup_database() -> Path:
    """Snapshot the SQLite file before anything destructive."""
    db_path = settings.DATA_DIR / "signals.db"
    if not db_path.exists():
        print("  (no local SQLite file — probably running against Postgres; skipping file backup)")
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = db_path.with_name(f"signals.db.backup-{stamp}")
    shutil.copy2(db_path, dest)
    print(f"  backup written: {dest.name} ({dest.stat().st_size/1024/1024:.1f} MB)")
    return dest


def coverage_report(db, sector="BFSI"):
    """Per-company: which sources produced documents, and how many events."""
    companies = db.query(Company).filter(Company.sector == sector).order_by(Company.name).all()
    docs = defaultdict(lambda: Counter())
    for cid, src, n in (db.query(RawDocument.company_id, RawDocument.source_type, func.count(RawDocument.id))
                        .group_by(RawDocument.company_id, RawDocument.source_type).all()):
        docs[cid][src] = n
    events = dict(db.query(Event.company_id, func.count(Event.id)).group_by(Event.company_id).all())

    rows = []
    for c in companies:
        rows.append({
            "company": c.name,
            "ticker": c.ticker,
            "region": c.region,
            "sources": dict(docs.get(c.id, {})),
            "docs": sum(docs.get(c.id, {}).values()),
            "events": events.get(c.id, 0),
        })
    return rows


def print_report(rows):
    with_events = [r for r in rows if r["events"] > 0]
    with_docs_no_events = [r for r in rows if r["docs"] > 0 and r["events"] == 0]
    nothing = [r for r in rows if r["docs"] == 0]

    print(f"    companies:                     {len(rows)}")
    print(f"    with events (real coverage):   {len(with_events)}  ({100*len(with_events)/len(rows):.1f}%)")
    print(f"    fetched docs but 0 events:     {len(with_docs_no_events)}")
    print(f"    nothing fetched at all:        {len(nothing)}")

    src_totals = Counter()
    for r in rows:
        for s, n in r["sources"].items():
            src_totals[s] += n
    print(f"    documents by source: {dict(src_totals)}")

    if with_docs_no_events:
        print("\n    Companies with documents but no extracted signal (first 10):")
        for r in with_docs_no_events[:10]:
            print(f"      {r['company'][:34]:34s} docs={r['docs']:3d} sources={list(r['sources'])}")
    if nothing:
        print("\n    Companies with nothing fetched (first 10):")
        for r in nothing[:10]:
            print(f"      {r['company'][:34]:34s} region={r['region']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true", help="actually wipe and rebuild everything")
    ap.add_argument("--report-only", action="store_true", help="print coverage, change nothing")
    ap.add_argument("--sector", default="BFSI")
    # Smoke test is deliberately NON-destructive: it ingests a few companies
    # on top of existing data so the pipeline can be validated without
    # destroying anything. Only --confirm wipes.
    ap.add_argument("--smoke", type=int, default=None,
                    help="ingest N companies WITHOUT wiping (validation run)")
    ap.add_argument("--limit", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--limit-docs", type=int, default=3, help="documents per source per company")
    ap.add_argument("--start-date", default=settings.REBUILD_START_DATE)
    ap.add_argument("--workers", type=int, default=5,
                    help="concurrent companies (fetch+extract are network-bound)")
    args = ap.parse_args()

    # Surface progress from the pipeline logger in the log file.
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    init_db()
    db = SessionLocal()

    print("=" * 70)
    print("REBUILD — clean event/signal rebuild with provable coverage")
    print("=" * 70)

    print("\n[current state]")
    print_report(coverage_report(db, args.sector))

    if args.report_only:
        db.close()
        return

    pipeline = IngestionPipeline()
    started = datetime.datetime.now()

    if args.smoke:
        # Non-destructive validation: prove the forced-Exa path and the date
        # floor work on a few companies before wiping anything.
        print(f"\n[SMOKE TEST — nothing wiped] {args.smoke} companies via the PARALLEL path "
              f"({args.workers} workers), Exa floored at {args.start_date}")
        ids = [c.id for c in (db.query(Company).filter(Company.sector == args.sector)
                              .order_by(Company.name).limit(args.smoke).all())]
        before_e = db.query(func.count(Event.id)).scalar()
        before_d = db.query(func.count(RawDocument.id)).scalar()

        # Scope the parallel runner to just these companies for the trial.
        import app.ingestion.pipeline as pl
        orig_all = pl.Company
        job = pipeline.run_batch_parallel(
            db=db, limit_per_company=args.limit_docs, sector_filter=args.sector,
            start_published_date=args.start_date, max_workers=args.workers,
            progress_every=1,
        ) if args.smoke >= 199 else None

        if job is None:
            # Manual subset run using the same parallel machinery.
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import threading as _th
            companies = db.query(Company).filter(Company.id.in_(ids)).all()
            by_id = {c.id: c for c in companies}
            snaps = [pl.CompanySnapshot(c.id, c.name, c.ticker, c.cik, list(c.aliases or [])) for c in companies]
            stats, lock = {}, _th.Lock()
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futs = {pool.submit(pipeline._prepare_company, s, args.limit_docs, True,
                                    args.start_date, stats, lock): s for s in snaps}
                for i, f in enumerate(as_completed(futs), 1):
                    s = futs[f]
                    d, e = pipeline._persist_prepared(db, by_id[s.id], f.result())
                    print(f"    [{i}/{len(snaps)}] {s.name[:34]:34s} docs={d:3d} events={e:3d}")
            print(f"    per-source docs: { {k: v['docs_ingested'] for k, v in stats.items()} }")

        el = (datetime.datetime.now() - started).total_seconds()
        added_e = db.query(func.count(Event.id)).scalar() - before_e
        added_d = db.query(func.count(RawDocument.id)).scalar() - before_d
        per = el / max(1, args.smoke)
        print(f"\n    added {added_d} docs / {added_e} events in {el:.0f}s "
              f"({per:.0f}s per company with {args.workers} workers)")
        print(f"    -> projected full 199-company run: {per*199/60:.0f} min ({per*199/3600:.1f} h)")
        db.close()
        return

    if not args.confirm:
        print("\nNothing changed. Use --smoke N to validate, or --confirm to wipe and rebuild.")
        db.close()
        return

    print("\n[1] Backing up before the wipe...")
    backup_database()

    print("\n[2] Wiping events / signals / alerts / raw_documents "
          "(companies, regions, watchlists and tenders are preserved)...")
    counts = {
        "alerts": db.query(Alert).delete(),
        "signals": db.query(Signal).delete(),
        "events": db.query(Event).delete(),
        "raw_documents": db.query(RawDocument).delete(),
    }
    db.commit()
    for k, v in counts.items():
        print(f"    deleted {v:5d} {k}")
    print(f"    preserved: {db.query(Company).count()} companies "
          f"({db.query(Company).filter(Company.region.isnot(None)).count()} with region)")

    print(f"\n[3] Full re-ingest — sector={args.sector}, every source for every company,")
    print(f"    {args.workers} concurrent workers, Exa news floored at {args.start_date}")
    print(f"    (SEC filings deliberately unbounded — a Q2 10-Q filed in July still counts).")
    job = pipeline.run_batch_parallel(
        db=db, limit_per_company=args.limit_docs, sector_filter=args.sector,
        start_published_date=args.start_date, max_workers=args.workers,
    )
    print(f"    job {job.status}: {job.items_ingested} docs, {job.events_extracted} events")

    elapsed = (datetime.datetime.now() - started).total_seconds()
    print(f"    elapsed: {elapsed/60:.1f} min")

    print("\n[4] Recomputing signals...")
    summary = signal_engine.recompute(db)
    print(f"    {summary}")

    print("\n[5] Coverage after rebuild")
    rows = coverage_report(db, args.sector)
    print_report(rows)

    print("\n[6] Exa budget consumed")
    for name, adapter in [("news", pipeline.exa_adapter), ("ir", pipeline.ir_adapter), ("career", pipeline.career_adapter)]:
        u = adapter.get_usage_status()
        print(f"    {name:8s} {u['used']:4d}/{u['budget']:4d}  remaining={u['remaining']:4d}"
              f"{'  *** EXHAUSTED ***' if u['budget_exhausted'] else ''}")

    oldest = db.query(func.min(Event.occurred_at)).scalar()
    print(f"\n[7] Oldest event now in DB: {oldest}")
    db.close()
    print("\n" + "=" * 70)
    print("Rebuild complete.")
    print("=" * 70)


if __name__ == "__main__":
    main()
