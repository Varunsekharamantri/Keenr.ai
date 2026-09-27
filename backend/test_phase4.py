import csv
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))

from app.db.database import init_db
from fastapi.testclient import TestClient
from app.main import app


def parse_csv(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_phase4():
    print("=" * 64)
    print("Testing Phase 4 — Exports, Analytics, Source Health")
    print("=" * 64)

    print("[1] Init DB (creates source_runs if missing)...")
    init_db()
    client = TestClient(app)

    print("\n[2] CSV exports — row counts must reconcile with the JSON endpoints")
    signals_json = client.get("/api/signals?limit=500&min_score=40").json()
    signals_csv = parse_csv(client.get("/api/exports/signals.csv?min_score=40").text)
    print(f"    signals.csv  rows={len(signals_csv):4d}  JSON total={signals_json['total']:4d}  "
          f"match={len(signals_csv) == signals_json['total']}")
    print(f"      columns: {', '.join(list(signals_csv[0].keys())[:8])}...")

    leads_json = client.get("/api/signals/leads?sectors=BFSI&min_score=50&limit=200").json()
    leads_csv = parse_csv(client.get("/api/exports/leads.csv?sectors=BFSI&min_score=50&limit=200").text)
    print(f"    leads.csv    rows={len(leads_csv):4d}  JSON companies={leads_json['total_companies']:4d}  "
          f"match={len(leads_csv) == leads_json['total_companies']}")

    events_json = client.get("/api/events?limit=200").json()
    events_csv = parse_csv(client.get("/api/exports/events.csv?limit=200").text)
    print(f"    events.csv   rows={len(events_csv):4d}  JSON rows={len(events_json):4d}  "
          f"match={len(events_csv) == len(events_json)}")

    r = client.get("/api/exports/signals.csv?min_score=40")
    print(f"    filename header: {r.headers.get('content-disposition')}")

    print("\n[3] Exports respect the date window")
    narrow = parse_csv(client.get("/api/exports/events.csv?start=2026-09-01&limit=5000").text)
    wide = parse_csv(client.get("/api/exports/events.csv?start=all&limit=5000").text)
    print(f"    events since 2026-09-01: {len(narrow)}   all time: {len(wide)}   narrower<wider={len(narrow) < len(wide)}")

    print("\n[4] CRM webhook (unconfigured should degrade cleanly)")
    crm = client.post("/api/exports/crm-webhook?sectors=BFSI").json()
    print(f"    status={crm['status']}  configured={client.get('/api/exports/crm-status').json()['configured']}")
    print(f"    message: {crm['message'][:90]}")

    print("\n[5] Heatmap — dimensions and totals reconcile")
    overview = client.get("/api/stats/overview").json()
    for label, q in [("sector x category", ""), ("industry x initiative", "?rows=industry&cols=initiative")]:
        h = client.get("/api/stats/heatmap" + q).json()
        dims_ok = len(h["matrix"]) == len(h["rows"]) and all(len(row) == len(h["cols"]) for row in h["matrix"])
        print(f"    {label:24s} {len(h['rows']):2d} x {len(h['cols']):2d}  total={h['total']:4d}  dims_ok={dims_ok}")
    h = client.get("/api/stats/heatmap").json()
    print(f"    heatmap total {h['total']} == overview events {overview['total_events']}: {h['total'] == overview['total_events']}")

    print("\n[6] Trends — buckets sum to the window total")
    for bucket in ["auto", "day", "week", "month"]:
        t = client.get(f"/api/stats/trends?bucket={bucket}").json()
        summed = sum(t["totals"])
        print(f"    bucket={t['bucket']:6s} labels={len(t['labels']):3d} sources={len(t['sources'])} "
              f"sum={summed:4d} total={t['total']:4d} match={summed == t['total']}")

    print("\n[7] Source health")
    sh = client.get("/api/admin/source-health").json()
    print(f"    SLA={sh['sla_hours']}h  healthy={sh['healthy_count']}  stale={sh['stale_count']}")
    print(f"    {'source':16s}{'status':10s}{'last doc':>12s}{'docs':>8s}{'events':>8s}{'success':>9s}")
    for s in sh["sources"]:
        age = "never" if s["age_hours"] is None else f"{s['age_hours']}h"
        rate = "-" if s["success_rate"] is None else f"{s['success_rate']}%"
        print(f"    {s['source_type']:16s}{s['status']:10s}{age:>12s}{s['total_documents']:8d}"
              f"{s['events_in_window']:8d}{rate:>9s}")

    print("\n[8] Company timeline")
    company = client.get("/api/companies?search=JPMorgan&limit=1").json()[0]
    tl = client.get(f"/api/companies/{company['id']}/timeline?start=all").json()
    print(f"    {company['name']}: {tl['event_count']} events / {len(tl['months'])} months / {len(tl['signals'])} signals")
    print(f"    months: {[(m['month'], len(m['events'])) for m in tl['months'][:5]]}")

    print("\n" + "=" * 64)
    print("Phase 4 Test Finished Successfully!")
    print("=" * 64)


if __name__ == "__main__":
    test_phase4()
