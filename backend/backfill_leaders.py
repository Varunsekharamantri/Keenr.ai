"""
First pull of People to Tap: search every company with a live opportunity.

Resumable by construction - a company searched once is no longer "due", so a
killed run simply continues where it stopped. After this, the daily job keeps
the data current: it re-searches a company only when a leadership-change story
arrives for it, or when its data passes LEADERS_TTL_DAYS.

Usage:
    python backfill_leaders.py --status        # what is due, and the budget
    python backfill_leaders.py                 # search everything due
    python backfill_leaders.py --max 20        # at most 20 searches
"""
import argparse
import io
import logging
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db.database import SessionLocal, init_db
from app.models.schema import Leader, LeaderFetch
from app.people.leaders import budget, companies_due, refresh_company, reverify


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--max", type=int, default=None)
    ap.add_argument("--reverify", action="store_true",
                    help="re-run today's rules over stored results; no searches")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    init_db()
    db = SessionLocal()
    if args.reverify:
        print(f"Replayed stored results: {reverify(db)}")
        current = db.query(Leader).filter(Leader.is_current.is_(True)).count()
        with_leaders = db.query(LeaderFetch).filter(LeaderFetch.verified > 0).count()
        print(f"{current} current leaders across {with_leaders} companies")
        db.close()
        return
    due = companies_due(db)
    usage = budget.get_usage_status()
    print(f"Due: {len(due)} companies · people budget {usage['used']}/{usage['budget']} this month")
    if args.status or not due:
        db.close()
        return

    done = kept = 0
    for company, reason in due:
        if args.max is not None and done >= args.max:
            break
        if budget.budget_exhausted or budget.quota_exhausted:
            print("  budget reached - stopping; the rest stay due for the next run")
            break
        r = refresh_company(db, company, reason)
        done += 1
        kept += r.get("verified", 0)
        note = r.get("error") or f"{r.get('verified', 0)} verified of {r.get('seen', 0)}"
        print(f"  [{done:3d}/{len(due)}] {company.name[:36]:36s} {reason:15s} {note}")

    with_leaders = db.query(LeaderFetch).filter(LeaderFetch.verified > 0).count()
    fetched = db.query(LeaderFetch).count()
    current = db.query(Leader).filter(Leader.is_current.is_(True)).count()
    print(f"\nSearched {done} · kept {kept} leaders this run")
    print(f"Companies with at least one verified leader: {with_leaders} of {fetched} searched · "
          f"{current} current leaders on file")
    print(f"Budget: {budget.get_usage_status()}")
    db.close()


if __name__ == "__main__":
    main()
