"""
Copy the local SQLite database into a Postgres target (Neon).

Sequencing matters: rebuild/backfill locally FIRST, verify, and only then
migrate the good data up. Running a 199-company ingest directly against a
remote database would pay a network round-trip on every write.

Safety properties:
  * Read-only on the source. SQLite is never modified.
  * Refuses to run against a non-empty target unless --replace is passed,
    so it cannot silently double-insert on a second run.
  * Verifies row counts per table and aborts on any mismatch.
  * Streams in batches, so a large events table doesn't load into memory.

Usage:
    # target from the environment (never pass a password on the command line —
    # it would land in your shell history)
    export DATABASE_URL='postgresql://...neon.tech/db?sslmode=require'
    python migrate_to_postgres.py --dry-run
    python migrate_to_postgres.py
    python migrate_to_postgres.py --replace     # wipe target tables first
"""
import argparse
import io
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))

from sqlalchemy import create_engine, func, inspect
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.schema import (
    Base, Alert, Company, Event, IngestionJob, RawDocument, Signal, SourceRun,
    Tender, Watchlist, Leader, LeaderFetch, AppState, AISummary,
)

# FK-safe insert order: a row's parents must already exist.
#   companies → raw_documents → events
#   ingestion_jobs → source_runs
#   companies → signals → alerts (alerts also reference watchlists)
ORDER = [
    ("companies", Company),
    ("ingestion_jobs", IngestionJob),
    ("raw_documents", RawDocument),
    ("events", Event),
    ("source_runs", SourceRun),
    ("watchlists", Watchlist),
    ("signals", Signal),
    ("alerts", Alert),
    ("tenders", Tender),
    # People to Tap, and the state shared with GitHub Actions
    ("leaders", Leader),
    ("leader_fetches", LeaderFetch),
    ("app_state", AppState),
    ("ai_summaries", AISummary),
]

BATCH = 500


def mask(url: str) -> str:
    """Never print a connection string with its password intact."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def copy_table(src_session, dst_session, model, name: str, dry_run: bool) -> tuple:
    cols = [c.key for c in inspect(model).mapper.column_attrs]
    total = src_session.query(func.count()).select_from(model).scalar()
    if dry_run or total == 0:
        return total, 0

    copied = 0
    for offset in range(0, total, BATCH):
        rows = src_session.query(model).order_by(*inspect(model).mapper.primary_key) \
                          .offset(offset).limit(BATCH).all()
        dst_session.bulk_insert_mappings(
            model, [{c: getattr(r, c) for c in cols} for r in rows]
        )
        dst_session.commit()
        copied += len(rows)
        print(f"      {copied}/{total}", end="\r")
    return total, copied


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=os.getenv("DATABASE_URL"),
                    help="Postgres URL (defaults to $DATABASE_URL)")
    ap.add_argument("--source", default=f"sqlite:///{settings.DATA_DIR / 'signals.db'}")
    ap.add_argument("--dry-run", action="store_true", help="count rows, write nothing")
    ap.add_argument("--replace", action="store_true",
                    help="delete existing rows in the target before copying")
    args = ap.parse_args()

    if not args.target or not args.target.startswith("postgres"):
        print("ERROR: no Postgres target. Set DATABASE_URL or pass --target.")
        print("       Neon gives you this under 'Connection string' (use the pooled one).")
        return 1
    if not Path(str(settings.DATA_DIR / "signals.db")).exists():
        print(f"ERROR: source SQLite file not found at {settings.DATA_DIR / 'signals.db'}")
        return 1

    print("=" * 70)
    print("SQLITE -> POSTGRES MIGRATION")
    print(f"  source: {args.source}")
    print(f"  target: {mask(args.target)}")
    if args.dry_run:
        print("  MODE:   dry run — nothing will be written")
    print("=" * 70)

    src_engine = create_engine(args.source, connect_args={"check_same_thread": False})
    dst_engine = create_engine(args.target, pool_pre_ping=True)
    SrcSession = sessionmaker(bind=src_engine)
    DstSession = sessionmaker(bind=dst_engine)
    src, dst = SrcSession(), DstSession()

    try:
        print("\n[1] Creating schema on the target (missing tables only)...")
        Base.metadata.create_all(bind=dst_engine)

        print("\n[2] Checking the target is safe to write...")
        existing = {name: dst.query(func.count()).select_from(model).scalar()
                    for name, model in ORDER}
        occupied = {k: v for k, v in existing.items() if v}
        if occupied and not args.replace and not args.dry_run:
            print(f"    ABORT — target already holds rows: {occupied}")
            print("    Re-run with --replace to wipe those tables first, or point at an empty database.")
            return 1
        if occupied and args.replace and not args.dry_run:
            print(f"    --replace: clearing {occupied}")
            for name, model in reversed(ORDER):   # children before parents
                dst.query(model).delete()
            dst.commit()

        print("\n[3] Copying...")
        results = []
        for name, model in ORDER:
            total, copied = copy_table(src, dst, model, name, args.dry_run)
            results.append((name, total, copied))
            print(f"    {name:16s} source={total:6d} copied={copied:6d}")

        print("\n[4] Verifying row counts...")
        ok = True
        for name, model in ORDER:
            s = src.query(func.count()).select_from(model).scalar()
            d = dst.query(func.count()).select_from(model).scalar()
            flag = "OK" if (s == d or args.dry_run) else "MISMATCH"
            if s != d and not args.dry_run:
                ok = False
            print(f"    {name:16s} sqlite={s:6d} postgres={d:6d}  {flag}")

        if args.dry_run:
            print("\nDry run complete — nothing written.")
            return 0
        if not ok:
            print("\nFAILED: row counts do not match. Target left as-is for inspection.")
            return 1

        print("\n" + "=" * 70)
        print("Migration verified. Next: set DATABASE_URL in .env to the Neon string")
        print("and restart the app — every tab should render identically.")
        print("=" * 70)
        return 0
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    sys.exit(main())
