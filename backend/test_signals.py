import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
backend_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(backend_dir))

from app.db.database import init_db, SessionLocal
from app.models.schema import Company, Signal, Watchlist
from app.signals.engine import signal_engine


def test_signals():
    print("=" * 60)
    print("Testing Signal Engine (Phase 3)")
    print("=" * 60)

    print("[1] Initializing DB (creates signals/watchlists/alerts tables if missing)...")
    init_db()
    db = SessionLocal()

    print("[2] Recomputing signals from existing events...")
    summary = signal_engine.recompute(db)
    for k, v in summary.items():
        print(f"    {k}: {v}")

    print("\n[3] Top 10 ranked signals:")
    top = (
        db.query(Signal).filter(Signal.status == "active")
        .order_by(Signal.intent_score.desc()).limit(10).all()
    )
    for i, s in enumerate(top, 1):
        comps = s.score_breakdown.get("components", {})
        print(f"\n    [{i}] {s.company.name} ({s.company.ticker}) — {s.initiative_name}")
        print(f"        Intent score: {s.intent_score}  "
              f"[evidence {comps.get('evidence')}, corroboration {comps.get('corroboration')}, "
              f"recency {comps.get('recency')}, spend/timing {comps.get('spend_timing')}]")
        print(f"        Sources: {s.source_types} ({s.event_count} events) | Timing: {s.timing_window} — {s.timing_estimate}")
        if s.stated_spend or s.stated_timing:
            print(f"        Stated spend: {s.stated_spend} | Stated timing: {s.stated_timing}")
        print(f"        Why: {'; '.join(s.score_breakdown.get('reasons', [])[:4])}")
        if s.peer_context:
            peers = ", ".join(f"{p['name']} ({p['intent_score']})" for p in s.peer_context)
            print(f"        Peers on same initiative: {peers}")

    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)

    print("\n[4] Creating a JPM watchlist via API (threshold 60) — should alert on already-hot signals...")
    jpm = db.query(Company).filter(Company.ticker == "JPM").first()
    for old in db.query(Watchlist).filter(Watchlist.name == "Test — JPM cloud/AI").all():
        db.delete(old)
    db.commit()
    r = client.post("/api/watchlists", json={
        "name": "Test — JPM cloud/AI",
        "company_ids": [jpm.id],
        "icp_initiative_ids": ["cloud_migration", "ai_genai_ml"],
        "alert_threshold": 60,
    })
    wl = r.json()
    print(f"    POST /api/watchlists -> {r.status_code}, alerts_created on creation: {wl.get('alerts_created')}")
    alerts = client.get("/api/alerts?unread_only=true&limit=5").json()
    for a in alerts:
        if a["watchlist_id"] == wl["id"]:
            print(f"    ALERT: {a['message']}  [channels: {a['delivered_channels']}]")

    print("\n[4b] Recompute again — same signals must NOT re-alert (no threshold crossing)...")
    summary2 = signal_engine.recompute(db)
    print(f"    alerts_created on recompute: {summary2['alerts_created']}")

    print("\n[5] ICP lead list via API (BFSI × cloud_migration, ai_genai_ml, min_score 50)...")
    r = client.get("/api/signals/leads?sectors=BFSI&initiative_ids=cloud_migration,ai_genai_ml&min_score=50&limit=5")
    data = r.json()
    print(f"    status {r.status_code} — {data.get('total_companies')} companies, {data.get('total_matching_signals')} matching signals")
    for lead in data.get("leads", []):
        c = lead["company"]
        print(f"    - {c['name']} ({c.get('ticker')}): lead score {lead['lead_score']}, "
              f"{lead['matching_signal_count']} matching signals; best = {lead['best_signal']['initiative_name']}")
        for cit in lead["best_signal_citations"][:2]:
            print(f"        cite [{cit['source_type']}]: \"{cit['quote_text'][:100]}...\"")

    print("\n[6] Watchlist leads + signal detail + alert endpoints...")
    r = client.get(f"/api/watchlists/{wl['id']}/leads")
    print(f"    GET /api/watchlists/{{id}}/leads -> {r.status_code}, companies: {r.json().get('total_companies')}")
    if top:
        r2 = client.get(f"/api/signals/{top[0].id}")
        print(f"    GET /api/signals/{{id}} -> {r2.status_code}, citations: {len(r2.json().get('citations', []))}, "
              f"contributing events: {r2.json().get('contributing_event_count')}")
    r3 = client.get("/api/signals?sector=BFSI&min_score=60&limit=3")
    print(f"    GET /api/signals?sector=BFSI&min_score=60 -> {r3.status_code}, total: {r3.json().get('total')}")
    r4 = client.get("/api/alerts/unread-count")
    print(f"    GET /api/alerts/unread-count -> {r4.json()}")
    if alerts:
        r5 = client.post(f"/api/alerts/{alerts[0]['id']}/read")
        print(f"    POST /api/alerts/{{id}}/read -> {r5.status_code}, is_read: {r5.json().get('is_read')}")

    print("\n[7] Phase 3b — date-window slicing...")
    import datetime as _dt
    today = _dt.date.today()
    windows = [
        ("7d", f"start={(today - _dt.timedelta(days=7)).isoformat()}"),
        ("30d (default)", ""),
        ("90d", f"start={(today - _dt.timedelta(days=90)).isoformat()}"),
        ("all time", "start=all"),
    ]
    print(f"    {'window':16s} {'events':>7s} {'active cos':>11s} {'signals':>8s} {'hot':>4s} {'leads':>6s}")
    for label, q in windows:
        stats = client.get("/api/stats/overview" + (f"?{q}" if q else "")).json()
        leads = client.get("/api/signals/leads?sectors=BFSI&min_score=50" + (f"&{q}" if q else "")).json()
        print(f"    {label:16s} {stats['total_events']:7d} "
              f"{stats['total_companies']:>4d}/{stats['total_companies_tracked']:<6d} "
              f"{stats['signals_count']:8d} {stats['hot_signals_count']:4d} {leads['total_companies']:6d}")

    # Same company, different windows -> different scores (the whole point)
    print("\n    JPM 'Generative AI' scored across windows:")
    for label, q in windows:
        d = client.get(f"/api/signals?company_id={jpm.id}&limit=50" + (f"&{q}" if q else "")).json()
        gen = [s for s in d["signals"] if "generative" in s["initiative_name"].lower() or s["initiative_id"] == "ai_genai_ml"]
        if gen:
            s = max(gen, key=lambda x: x["intent_score"])
            print(f"      {label:16s} score={s['intent_score']:3d} events={s['event_count']:2d} "
                  f"sources={s['distinct_source_count']} timing={s['timing_window']}")
        else:
            print(f"      {label:16s} (no GenAI signal in window)")

    # Citations must stay inside the window
    d30 = client.get(f"/api/signals?company_id={jpm.id}&limit=50").json()
    if d30["signals"]:
        sid = d30["signals"][0]["id"]
        detail = client.get(f"/api/signals/{sid}").json()
        cutoff = (today - _dt.timedelta(days=30)).isoformat()
        outside = [c for c in detail.get("citations", []) if (c["occurred_at"] or "")[:10] < cutoff]
        print(f"\n    Composite-id detail: {len(detail.get('citations', []))} citations, "
              f"{len(outside)} outside the 30d window (expect 0)")

    db.close()
    print("\n" + "=" * 60)
    print("Signal Engine Test Finished Successfully!")
    print("=" * 60)


if __name__ == "__main__":
    test_signals()
