"""
Backfill article thumbnails for stored documents. Safe to re-run: documents
already checked are skipped, including those where no image was found.

Usage:
    python fetch_thumbnails.py                    # BFSI docs since 1 Aug
    python fetch_thumbnails.py --since all
    python fetch_thumbnails.py --recheck          # retry everything
"""
import argparse
import datetime
import io
import logging
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings
from app.db.database import init_db, SessionLocal
from app.ingestion.thumbnails import fill_thumbnails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=settings.REBUILD_START_DATE, help="YYYY-MM-DD or 'all'")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--recheck", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    since = None if args.since == "all" else datetime.datetime.fromisoformat(args.since)
    init_db()
    db = SessionLocal()
    try:
        started = datetime.datetime.now()
        stats = fill_thumbnails(db, since=since, workers=args.workers,
                                limit=args.limit, recheck=args.recheck)
        secs = (datetime.datetime.now() - started).total_seconds()
        rate = (100 * stats["found"] / stats["checked"]) if stats["checked"] else 0
        print(f"\nChecked {stats['checked']} documents in {secs:.0f}s - "
              f"{stats['found']} images ({rate:.0f}%), "
              f"{stats['skipped_host']} skipped (Google News / SEC / LinkedIn profiles)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
