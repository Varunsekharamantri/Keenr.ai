"""
Resumable, interruptible coverage backfill for companies that have no signals.

Why this exists: 90 of the 199 BFSI companies have zero events — not because
they were skipped, but because the rich Exa sources are budget-rationed by a
daily rotation that never reached them. They only ever got Google News RSS
headlines, which are too thin to extract initiatives from. This run gives every
one of them all six sources.

Design notes:
  * Purely ADDITIVE. These companies have no events, so nothing is deleted and
    the dashboard can only improve. Safe to kill at any moment.
  * Per-company atomic: each company is fetched, extracted, committed and
    recorded before the next one starts.
  * Resumable: progress lives in data/backfill_progress.json, so a company is
    never re-attempted after a completed pass (even if it legitimately returned
    nothing, which would otherwise loop forever).
  * Time-boxed: --minutes stops cleanly at a wave boundary, for laptop sessions.

Usage:
    python backfill_coverage.py --status              # what's left
    python backfill_coverage.py --minutes 90          # work for 90 minutes
    python backfill_coverage.py --minutes 90 --workers 6
    python backfill_coverage.py --retry-empty         # revisit zero-yield ones
"""
import argparse
import datetime
import io
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))

from sqlalchemy import func
from app.config import settings
from app.db.database import init_db, SessionLocal
from app.models.schema import Company, Event, RawDocument
from app.ingestion.pipeline import IngestionPipeline, CompanySnapshot
from app.signals.engine import signal_engine

PROGRESS_PATH = settings.DATA_DIR / "backfill_progress.json"


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        try:
            return json.loads(PROGRESS_PATH.read_text())
        except Exception:
            pass
    return {"attempted": {}}


def save_progress(state: dict):
    PROGRESS_PATH.write_text(json.dumps(state, indent=1))


def select_targets(db, state: dict, sector="BFSI", retry_empty=False):
    """Companies with no events, not already attempted in a completed pass."""
    # Resolved in Python rather than a NOT IN subquery: a single NULL company_id
    # would make SQL's NOT IN return the empty set and silently find no targets.
    covered = {r[0] for r in db.query(Event.company_id).distinct().all() if r[0]}
    gap = [c for c in db.query(Company).filter(Company.sector == sector)
           .order_by(Company.name).all() if c.id not in covered]
    if retry_empty:
        return gap
    attempted = state.get("attempted", {})
    return [c for c in gap if c.id not in attempted]


def print_status(db, state):
    total = db.query(func.count(Company.id)).filter(Company.sector == "BFSI").scalar()
    covered_ids = {r[0] for r in db.query(Event.company_id).distinct().all()}
    bfsi = db.query(Company).filter(Company.sector == "BFSI").all()
    have = sum(1 for c in bfsi if c.id in covered_ids)
    attempted = state.get("attempted", {})
    pending = select_targets(db, state)
    empty_yield = [a for a in attempted.values() if a.get("events", 0) == 0]

    print(f"  BFSI companies:        {total}")
    print(f"  with signals:          {have}  ({100*have/total:.1f}%)")
    print(f"  gap remaining:         {total - have}")
    print(f"  attempted this effort: {len(attempted)}  (of which {len(empty_yield)} yielded nothing)")
    print(f"  still to attempt:      {len(pending)}")
    if pending[:6]:
        print(f"  next up: {', '.join(c.name[:24] for c in pending[:6])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=None,
                    help="stop cleanly after this many minutes (omit = run to completion)")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--limit-docs", type=int, default=3)
    ap.add_argument("--sector", default="BFSI")
    ap.add_argument("--start-date", default=settings.REBUILD_START_DATE)
    ap.add_argument("--status", action="store_true", help="show progress and exit")
    ap.add_argument("--retry-empty", action="store_true",
                    help="also revisit companies previously attempted with no result")
    ap.add_argument("--max-companies", type=int, default=None)
    args = ap.parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", stream=sys.stdout)

    init_db()
    db = SessionLocal()
    state = load_progress()

    print("=" * 72)
    print("COVERAGE BACKFILL — additive, resumable, interruptible")
    print("=" * 72)
    print_status(db, state)

    if args.status:
        db.close()
        return

    targets = select_targets(db, state, args.sector, args.retry_empty)
    if args.max_companies:
        targets = targets[:args.max_companies]
    if not targets:
        print("\nNothing left to do. (Use --retry-empty to revisit zero-yield companies.)")
        db.close()
        return

    budget_s = args.minutes * 60 if args.minutes else None
    print(f"\nProcessing {len(targets)} companies · {args.workers} workers · "
          f"Exa news floored at {args.start_date}")
    if budget_s:
        print(f"Time box: {args.minutes:.0f} min — will stop at a wave boundary. "
              f"Safe to Ctrl-C at any time.")
    print("-" * 72)

    pipeline = IngestionPipeline()
    # URLs already stored: handed to the workers so they do not extract (and pay
    # for) documents that will be dropped as duplicates at write time.
    known_urls = {r[0] for r in db.query(RawDocument.url).all() if r[0]}
    print(f"Known documents: {len(known_urls)} (these are skipped before extraction)")
    started = time.perf_counter()
    stats, lock = {}, threading.Lock()
    done = docs_total = events_total = 0
    wave = max(1, args.workers)
    stopped_early = False

    try:
        for i in range(0, len(targets), wave):
            if budget_s and (time.perf_counter() - started) > budget_s:
                stopped_early = True
                print(f"\n  time box reached — stopping cleanly at {done} companies")
                break

            batch = targets[i:i + wave]
            snaps = [CompanySnapshot(c.id, c.name, c.ticker, c.cik, list(c.aliases or []))
                     for c in batch]
            by_id = {c.id: c for c in batch}

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(pipeline._prepare_company, s, args.limit_docs, True,
                                       args.start_date, stats, lock, known_urls): s for s in snaps}
                for fut in as_completed(futures):
                    snap = futures[fut]
                    try:
                        prepared = fut.result()
                    except Exception as ex:
                        print(f"    !! {snap.name[:30]:30s} prepare failed: {str(ex)[:60]}")
                        prepared = []
                    d, e = pipeline._persist_prepared(db, by_id[snap.id], prepared)
                    docs_total += d
                    events_total += e
                    done += 1

                    state["attempted"][snap.id] = {
                        "name": snap.name, "at": datetime.datetime.utcnow().isoformat(),
                        "docs": d, "events": e,
                    }
                    save_progress(state)   # after every company, so a kill loses nothing

                    elapsed = time.perf_counter() - started
                    rate = elapsed / done
                    remaining = len(targets) - done
                    flag = "" if e else "   (no signal found)"
                    print(f"  [{done:3d}/{len(targets)}] {snap.name[:30]:30s} "
                          f"docs={d:3d} events={e:3d}  "
                          f"{rate:.0f}s/co, ~{rate*remaining/60:.0f}min left{flag}")
    except KeyboardInterrupt:
        stopped_early = True
        print(f"\n  interrupted — {done} companies completed and saved")

    elapsed = time.perf_counter() - started
    print("-" * 72)
    print(f"Processed {done} companies in {elapsed/60:.1f} min "
          f"({elapsed/max(1,done):.0f}s each) → {docs_total} docs, {events_total} events")
    if stats:
        print(f"  docs by source: { {k: v['docs_ingested'] for k, v in stats.items()} }")

    print("\nRecomputing signals...")
    summary = signal_engine.recompute(db)
    print(f"  {summary}")

    print("\nExa budget after this run:")
    for name, adapter in [("news", pipeline.exa_adapter), ("ir", pipeline.ir_adapter),
                          ("career", pipeline.career_adapter)]:
        u = adapter.get_usage_status()
        print(f"  {name:7s} {u['used']:4d}/{u['budget']:4d} remaining={u['remaining']:4d}"
              f"{'  *** EXHAUSTED ***' if u['budget_exhausted'] else ''}")

    print("\nCoverage now:")
    print_status(db, state)
    if stopped_early:
        print("\nResume any time with the same command — completed companies are skipped.")
    db.close()


if __name__ == "__main__":
    main()
