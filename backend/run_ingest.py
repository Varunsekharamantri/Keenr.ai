"""
Run the daily refresh without the web app - for Windows Task Scheduler, a cloud
cron, or a manual run. Identical to the app's own 08:00 job (app/daily_job.py),
including the once-a-day guard, so running both never double-fetches.

Exit codes: 0 = done (or already done today), 1 = failed.

Usage:
    python run_ingest.py              # today's planned sources, once per day
    python run_ingest.py --force      # run again even if today already succeeded
    python run_ingest.py --plan       # just print what today's run would poll
"""
import argparse
import datetime
import io
import logging
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db.database import init_db
from app.daily_job import run_daily, today_local
from app.ingestion.cadence import describe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="run even if today already succeeded")
    ap.add_argument("--plan", action="store_true", help="print today's plan and exit")
    ap.add_argument("--limit-per-company", type=int, default=None)
    args = ap.parse_args()

    print(describe(today_local()))
    if args.plan:
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    init_db()
    status = run_daily(force=args.force, limit_per_company=args.limit_per_company)
    print()
    print(f"state: {status.get('state')}  ok: {status.get('ok')}  "
          f"docs: {status.get('documents')}  events: {status.get('events')}  "
          f"minutes: {status.get('minutes')}")
    if status.get("error"):
        print("error:", status["error"])
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
