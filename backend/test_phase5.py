import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))

from sqlalchemy import select
from app.db.database import init_db, SessionLocal
from app.models.schema import Company, Event, Tender
from app.signals.entities import normalize_entity, normalize_entities
from fastapi.testclient import TestClient
from app.main import app


def test_phase5():
    print("=" * 66)
    print("Testing Phase 5 — Geography, Coverage, Vendor Graph, Tenders")
    print("=" * 66)

    print("[1] Init DB (adds companies.country/region, creates tenders)...")
    init_db()
    client = TestClient(app)
    db = SessionLocal()

    print("\n[2] Region coverage of the BFSI universe")
    bfsi = db.query(Company).filter(Company.sector == "BFSI").all()
    classified = [c for c in bfsi if c.region]
    print(f"    classified: {len(classified)} / {len(bfsi)}  (unknown: {len(bfsi) - len(classified)})")
    americas = [c for c in bfsi if c.region == "Americas"]
    row = [c for c in bfsi if c.region == "Rest of World"]
    print(f"    Americas={len(americas)}  Rest of World={len(row)}  sum={len(americas)+len(row)}")

    print("\n    The CIK-less US institutions a naive 'CIK => Americas' rule would misfile:")
    tricky = ["U.S. Bancorp", "State Farm", "USAA", "Fannie Mae", "Freddie Mac",
              "Discover Financial Services", "The Charles Schwab Corporation", "KeyCorp"]
    wrong = 0
    for name in tricky:
        c = next((x for x in bfsi if x.name == name), None)
        if not c:
            print(f"      {name:34s} NOT FOUND")
            continue
        ok = c.region == "Americas"
        wrong += 0 if ok else 1
        print(f"      {name:34s} -> {str(c.country):15s} {str(c.region):15s} {'OK' if ok else '*** WRONG ***'}")
    print(f"    misfiled: {wrong} (expect 0)")

    print("\n[3] Region filters change results")
    for reg in ["Americas", "Rest of World"]:
        cos = client.get(f"/api/companies?sector=BFSI&region={reg}&limit=200").json()
        sig = client.get(f"/api/signals?region={reg}&limit=500&start=all").json()
        ev = client.get(f"/api/events?region={reg}&limit=200&start=all").json()
        print(f"    {reg:15s} companies={len(cos):3d}  signals={sig['total']:3d}  events={len(ev):3d}")
    ov = client.get("/api/stats/overview?start=all").json()
    print(f"    stats events_by_region: {ov['events_by_region']}")
    hm = client.get("/api/stats/heatmap?rows=region&start=all").json()
    print(f"    heatmap rows=region: {hm['rows']} totals={hm['row_totals']}")

    print("\n[4] Vendor graph — alias normalization")
    checks = [("Azure", "Microsoft Azure"), ("Microsoft Azure", "Microsoft Azure"),
              ("AWS", "AWS"), ("Amazon Web Services", "AWS"),
              ("GCP", "Google Cloud"), ("Google Cloud", "Google Cloud")]
    for raw, expected in checks:
        got = normalize_entity(raw)
        print(f"    {raw:22s} -> {str(got):18s} {'OK' if got == expected else '*** expected ' + expected + ' ***'}")
    print(f"    dedupe: {normalize_entities(['AWS', 'Amazon Web Services', 'Azure'])} (AWS should appear once)")

    g = client.get("/api/graph/vendors?start=all&limit=10").json()
    print(f"\n    graph: {g['total_vendors']} vendors over {g['events_scanned']} events")
    print(f"    by category: {g['by_category']}")
    for v in g["vendors"][:6]:
        print(f"      {v['vendor']:20s} {v['category']:24s} {v['company_count']:2d} companies, {v['mentions']:3d} mentions")

    print("\n[5] Tenders (TED — no credentials needed)")
    stats = client.get("/api/tenders/stats").json()
    print(f"    stored={stats['total']}  by_source={stats['by_source']}  matched_to_company={stats['matched_to_company']}")
    print(f"    SAM.gov configured: {stats['samgov_configured']} (expected False until a key is added)")
    listing = client.get("/api/tenders?limit=3&start=all").json()
    for t in listing["tenders"][:3]:
        print(f"      [{str(t['published_at'])[:10]}] {str(t['buyer_country']):5s} "
              f"{str(t['buyer_name'])[:30]:30s} | {t['title'][:44]}")
    dates_ok = all(t["published_at"] for t in listing["tenders"])
    print(f"    all rows have a parsed published_at: {dates_ok}")

    print("\n    dedupe on re-fetch:")
    again = client.post("/api/tenders/refresh?limit=30").json()
    print(f"      created={again['created']} skipped_existing={again['skipped_existing']} "
          f"(created should be 0 for already-seen notices)")

    print("\n[6] Coverage mode targets only companies with zero events")
    cov = client.get("/api/ingest/coverage?sector=BFSI").json()
    covered_sub = db.query(Event.company_id).distinct().subquery()
    uncovered_rows = db.query(Company).filter(
        Company.sector == "BFSI",
        ~Company.id.in_(select(covered_sub.c.company_id)),
    ).all()
    have_events = [c for c in uncovered_rows if db.query(Event).filter(Event.company_id == c.id).count() > 0]
    print(f"    endpoint: {cov['covered']}/{cov['total_companies']} covered ({cov['coverage_pct']}%), "
          f"{cov['uncovered']} uncovered")
    print(f"    selected rows that actually have events: {len(have_events)} (expect 0)")

    db.close()
    print("\n" + "=" * 66)
    print("Phase 5 Test Finished Successfully!")
    print("=" * 66)


if __name__ == "__main__":
    test_phase5()
