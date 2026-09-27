"""
Replay already-downloaded documents through the fixed extractor.

Why this exists: RuleMatcher.extract_from_text called `ticker.lower()`
unguarded. For a company with no ticker — which is most of the Rest-of-World
BFSI universe (ANZ, Allianz, BNP Paribas, BBVA, ICBC...) — that raised
AttributeError on the *first* taxonomy keyword match, and since the chunk pass
runs on every document, it killed rule-based extraction for every document
those companies ever had. Only the LLM path (which guards with
`ticker or company.name`) still produced anything, so whenever Groq was rate
limited these companies silently recorded zero events.

The documents were fetched and archived; only the extraction was lost. This
replays them from disk, so it costs **zero** API budget on the fetch side and
recovers signal that has already been paid for.

Ingestion dedupes on RawDocument.url, so a normal re-run would skip these
documents forever — this script is the only way to get that signal back.

Usage:
    python reextract_lost_docs.py --dry-run       # what would be recovered
    python reextract_lost_docs.py --no-llm        # rules only: fast, free
    python reextract_lost_docs.py                 # rules + LLM enrichment
"""
import argparse
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))

from sqlalchemy import func
from app.db.database import init_db, SessionLocal
from app.models.schema import Company, Event, RawDocument
from app.extraction.extractor import signal_extractor
from app.signals.engine import signal_engine


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--no-llm", action="store_true",
                    help="rule-based extraction only (fast, no Groq rate limits)")
    ap.add_argument("--sector", default="BFSI")
    ap.add_argument("--all-docs", action="store_true",
                    help="replay every zero-event document, not just ticker-less companies")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    import logging
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", stream=sys.stdout)

    init_db()
    db = SessionLocal()

    if args.no_llm:
        # The LLM pass is the half that already worked; disabling it isolates
        # the recovery to the rule matcher and avoids Groq rate limiting.
        # The extractor gates on `llm_client.is_available`, so this must be a
        # stub with that attribute rather than None.
        class _NoLLM:
            is_available = False
        signal_extractor.llm_client = _NoLLM()

    # Documents that yielded no events, on companies that have none either.
    docs_with_events = {r[0] for r in db.query(Event.raw_doc_id).distinct().all() if r[0]}
    companies_with_events = {r[0] for r in db.query(Event.company_id).distinct().all() if r[0]}

    q = db.query(RawDocument, Company).join(Company, Company.id == RawDocument.company_id) \
          .filter(Company.sector == args.sector)
    candidates = []
    for doc, company in q.all():
        if doc.id in docs_with_events:
            continue
        if company.id in companies_with_events and not args.all_docs:
            continue
        if not args.all_docs and (company.ticker or "").strip():
            continue
        candidates.append((doc, company))
    if args.limit:
        candidates = candidates[:args.limit]

    print("=" * 70)
    print("RE-EXTRACT — replay archived documents through the fixed extractor")
    print("=" * 70)
    print(f"  candidate documents: {len(candidates)}")
    print(f"  distinct companies:  {len({c.id for _, c in candidates})}")
    print(f"  mode: {'rules only' if args.no_llm else 'rules + LLM'}"
          f"{' · DRY RUN' if args.dry_run else ''}")
    if not candidates:
        print("\nNothing to recover.")
        db.close()
        return 0
    print("-" * 70)

    recovered = missing_text = failed = 0
    by_company = {}

    for i, (doc, company) in enumerate(candidates, 1):
        text = None
        if doc.local_path and Path(doc.local_path).exists():
            try:
                text = Path(doc.local_path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                text = None
        if not text:
            # The 1000-char snippet is a poor substitute but better than nothing.
            text = doc.raw_text_snippet or ""
            if len(text) < 100:
                missing_text += 1
                continue

        try:
            events = signal_extractor.extract_from_doc(
                company=company, raw_doc_id=doc.id, doc_title=doc.title,
                doc_url=doc.url, source_type=doc.source_type,
                filing_date=doc.filing_date, sections={}, full_text=text,
            )
        except Exception as ex:
            failed += 1
            print(f"    !! {company.name[:28]:28s} {str(ex)[:50]}")
            continue

        if not events:
            continue
        if not args.dry_run:
            for ev in events:
                ev.company_id = company.id
                ev.raw_doc_id = doc.id
                db.add(ev)
            db.commit()
        recovered += len(events)
        by_company[company.name] = by_company.get(company.name, 0) + len(events)

        if i % 20 == 0:
            print(f"  [{i}/{len(candidates)}] recovered {recovered} events so far")

    print("-" * 70)
    print(f"  events recovered:      {recovered}")
    print(f"  companies helped:      {len(by_company)}")
    print(f"  docs with no text:     {missing_text}")
    print(f"  extraction failures:   {failed}")
    if by_company:
        print("\n  Top recoveries:")
        for name, n in sorted(by_company.items(), key=lambda kv: -kv[1])[:12]:
            print(f"    {name[:38]:38s} +{n}")

    if args.dry_run:
        print("\nDry run — nothing written.")
    else:
        print("\nRecomputing signals...")
        print(f"  {signal_engine.recompute(db)}")
        total = db.query(func.count(Company.id)).filter(Company.sector == args.sector).scalar()
        covered = len({r[0] for r in db.query(Event.company_id).distinct().all() if r[0]}
                      & {c.id for c in db.query(Company).filter(Company.sector == args.sector).all()})
        print(f"\n  {args.sector} coverage now: {covered}/{total} ({100*covered/total:.1f}%)")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
