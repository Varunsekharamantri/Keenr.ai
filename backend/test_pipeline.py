import os
import sys
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))

from app.db.database import init_db, SessionLocal
from app.db.seed_data import seed_companies
from app.models.schema import Company, Event, RawDocument
from app.models.taxonomy import taxonomy_manager
from app.ingestion.pipeline import IngestionPipeline

def test_pipeline():
    print("=" * 60)
    print("Testing Market Signals Pipeline (Phase 1)")
    print("=" * 60)

    # 1. Initialize DB
    print("[1] Initializing SQLite database...")
    init_db()
    db = SessionLocal()
    
    # 2. Seed Companies
    print("[2] Seeding company master...")
    seeded = seed_companies(db)
    print(f"    Seeded {seeded} new companies.")
    total_companies = db.query(Company).count()
    print(f"    Total companies in universe: {total_companies}")

    # 3. Check Taxonomy
    print("[3] Checking Taxonomy...")
    cats = taxonomy_manager.get_all_categories()
    print(f"    Loaded {len(cats)} categories and {len(taxonomy_manager.initiatives_by_id)} initiatives.")
    for c in cats:
        print(f"    - {c.name} ({len(c.initiatives)} initiatives)")

    # 4. Ingest Target Company (e.g. JPMorgan Chase & Co. - JPM)
    print("\n[4] Running live Ingestion for JPMorgan Chase (JPM)...")
    jpm = db.query(Company).filter(Company.ticker == "JPM").first()
    if not jpm:
        print("ERROR: JPM not found in seeded DB")
        return

    pipeline = IngestionPipeline()
    res = pipeline.run_company_ingestion(
        db=db,
        company=jpm,
        source_type="all",
        limit=3
    )
    print(f"    Ingestion Result for {jpm.name}:")
    print(f"    - Raw Documents Ingested: {res['docs_ingested']}")
    print(f"    - Events / Signals Extracted: {res['events_extracted']}")

    # 4b. Phase 2 check: source_type="all" above already includes every
    # source, so break down events to confirm each one actually produced
    # signal (a repeat fetch here would just dedupe against the URLs "all"
    # already ingested and report 0 docs). "patents" will show 0 until
    # PATENTSVIEW_API_KEY is configured — expected, not a failure.
    print("\n[4b] Phase 2 event breakdown by source for JPM...")
    from collections import Counter
    all_events = db.query(Event).filter(Event.company_id == jpm.id).all()
    counts = Counter(e.source_type for e in all_events)
    for source in ("sec_edgar", "earnings_deck", "news_rss", "exa_news", "ir_press", "career_pages", "patents"):
        print(f"    {source}: {counts.get(source, 0)} events")

    # 5. Inspect Extracted Events
    events = db.query(Event).filter(Event.company_id == jpm.id).limit(5).all()
    print(f"\n[5] Sample Extracted Events ({len(events)} shown):")
    for idx, ev in enumerate(events, 1):
        print(f"\n    [{idx}] {ev.title}")
        print(f"        Category: {ev.category_name} | Initiative: {ev.initiative_name}")
        print(f"        IT Offering: {ev.it_offering}")
        print(f"        Confidence: {ev.confidence} | Spend: {ev.spend_amount} | Timing: {ev.timing_horizon}")
        print(f"        Entities: {ev.key_entities}")
        print(f"        Quote: \"{ev.quote_text[:120]}...\"")
        print(f"        Source: {ev.source_type} ({ev.source_url[:60]}...)")

    print("\n" + "=" * 60)
    print("Pipeline Test Finished Successfully!")
    print("=" * 60)

if __name__ == "__main__":
    test_pipeline()
