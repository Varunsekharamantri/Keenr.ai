"""
Repair documents whose date was invented at fetch time.

Before app/ingestion/pub_date.py, a search result with no publication date was
stored with the time it was fetched, so an undated 2021 case study counted as
today's news. Such a document is recognisable: its date equals its
created_at to within a couple of minutes.

For each one, look for the real date (URL, the page's own metadata, the byline
in the stored text). Found: move the document and its events to that date.
Not found: delete the document and its events - an old page cannot be
presented as new, and there is no honest date to give it. Then rescore.

    python fix_undated_docs.py            # dry run: report only
    python fix_undated_docs.py --apply    # make the changes
"""
import argparse
import datetime
import io
import pathlib
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sqlalchemy import func  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.database import SessionLocal  # noqa: E402
from app.ingestion.pub_date import find_published_date  # noqa: E402
from app.models.schema import Event, RawDocument  # noqa: E402

STAMP_TOLERANCE = datetime.timedelta(minutes=2)


def stored_text(doc: RawDocument) -> str:
    try:
        if doc.local_path and pathlib.Path(doc.local_path).exists():
            return pathlib.Path(doc.local_path).read_text(encoding="utf-8", errors="ignore")[:6000]
    except Exception:
        pass
    return doc.raw_text_snippet or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    docs = [d for d in db.query(RawDocument).all()
            if d.filing_date and d.created_at and abs(d.created_at - d.filing_date) < STAMP_TOLERANCE]
    events_of = dict(db.query(Event.raw_doc_id, func.count())
                     .filter(Event.raw_doc_id.in_([d.id for d in docs])).group_by(Event.raw_doc_id).all())
    print(f"Documents dated at fetch time: {len(docs)} "
          f"({dict(Counter(d.source_type for d in docs))}), carrying {sum(events_of.values())} events")

    def look(d):
        return d, find_published_date(d.url, stored_text(d), d.title or "")

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = list(pool.map(look, docs))

    cut = datetime.datetime.utcnow() - datetime.timedelta(days=30)
    redate = [(d, when) for d, when in found if when]
    drop = [d for d, when in found if not when]
    print(f"\nReal date found: {len(redate)} documents ({sum(events_of.get(d.id, 0) for d, _ in redate)} events)")
    print(f"  of which still in the last 30 days: {sum(1 for _, w in redate if w >= cut)}")
    print("  by year:", dict(sorted(Counter(w.year for _, w in redate).items())))
    print(f"No date anywhere -> remove: {len(drop)} documents ({sum(events_of.get(d.id, 0) for d in drop)} events)")
    for d, w in sorted(redate, key=lambda x: -events_of.get(x[0].id, 0))[:6]:
        print(f"   re-date {w:%Y-%m-%d}  {events_of.get(d.id, 0):3d} ev  {(d.title or '')[:60]}")
    for d in sorted(drop, key=lambda x: -events_of.get(x.id, 0))[:6]:
        print(f"   remove            {events_of.get(d.id, 0):3d} ev  {(d.title or '')[:60]}")

    before = db.query(Event).filter(Event.occurred_at >= cut).count()
    moved_out = sum(events_of.get(d.id, 0) for d, w in redate if w < cut) + sum(events_of.get(d.id, 0) for d in drop)
    print(f"\nEvents in the last 30 days: {before} now -> about {before - moved_out} after")

    if not args.apply:
        print("\nDry run - nothing changed. Re-run with --apply to make these changes.")
        return

    for d, when in redate:
        d.filing_date = when
        db.query(Event).filter(Event.raw_doc_id == d.id).update({Event.occurred_at: when}, synchronize_session=False)
    for d in drop:
        db.delete(d)                      # events go with it (cascade)
    db.commit()
    print(f"\nApplied: {len(redate)} re-dated, {len(drop)} removed.")

    from app.signals.engine import signal_engine
    print("Rescoring signals...", signal_engine.recompute(db, sector_filter=settings.SCHEDULE_SECTOR_FILTER))
    db.close()


if __name__ == "__main__":
    main()
