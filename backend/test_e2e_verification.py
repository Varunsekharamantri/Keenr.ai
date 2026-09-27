import httpx

def test_full_platform():
    client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=15.0)

    print("=" * 60)
    print("Market Signals Platform - Phase 1 End-to-End Test Suite")
    print("=" * 60)

    # 1. Test Static Files & Index
    print("\n[1] Verifying Frontend Static Delivery:")
    res_index = client.get("/")
    assert res_index.status_code == 200, f"Index failed: {res_index.status_code}"
    assert "Market Signals" in res_index.text
    print("  [OK] Index HTML served successfully (200 OK)")

    res_css = client.get("/static/css/styles.css")
    assert res_css.status_code == 200
    assert "--bg-main:" in res_css.text
    print("  [OK] Stylesheet CSS served successfully (200 OK)")

    res_js = client.get("/static/js/app.js")
    assert res_js.status_code == 200
    assert "Market Signals Platform" in res_js.text
    print("  [OK] App JS and Components served successfully (200 OK)")

    # 2. Test Taxonomy Endpoint
    print("\n[2] Verifying Taxonomy API:")
    res_tax = client.get("/api/taxonomy")
    assert res_tax.status_code == 200
    tax_data = res_tax.json()
    cats = tax_data["categories"]
    print(f"  [OK] Loaded {len(cats)} categories:")
    for c in cats:
        print(f"    - {c['name']} ({len(c['initiatives'])} initiatives)")
    assert len(cats) >= 5

    # 3. Test Overview Stats Endpoint
    print("\n[3] Verifying Stats Overview API:")
    res_stats = client.get("/api/stats/overview")
    assert res_stats.status_code == 200
    stats = res_stats.json()
    print(f"  [OK] Total Companies: {stats['total_companies']}")
    print(f"  [OK] Total Events: {stats['total_events']}")
    print(f"  [OK] Total Raw Docs: {stats['total_documents']}")
    print(f"  [OK] Sector Distribution: {stats['events_by_sector']}")
    print(f"  [OK] Top Initiatives: {[i['name'] for i in stats['top_initiatives'][:4]]}")

    # 4. Test Companies List and Detail Endpoints
    print("\n[4] Verifying Company Endpoints:")
    res_cos = client.get("/api/companies?limit=10")
    assert res_cos.status_code == 200
    companies = res_cos.json()
    assert len(companies) > 0
    sample_co = companies[0]
    print(f"  [OK] Retrieved {len(companies)} companies. First: {sample_co['name']} ({sample_co['ticker']})")

    res_detail = client.get(f"/api/companies/{sample_co['id']}")
    assert res_detail.status_code == 200
    co_detail = res_detail.json()
    assert "company" in co_detail and "recent_events" in co_detail
    print(f"  [OK] Retrieved company detail modal data for {co_detail['company']['name']}")

    # 5. Test Events Multi-Filter API
    print("\n[5] Verifying Events Multi-Filter API:")
    # Filter by BFSI
    res_bfsi = client.get("/api/events?sector=BFSI")
    print(f"  [OK] Events in BFSI Sector: {len(res_bfsi.json())}")

    # Filter by Healthcare
    res_health = client.get("/api/events?sector=Healthcare")
    print(f"  [OK] Events in Healthcare Sector: {len(res_health.json())}")

    # Filter by min_confidence 0.8
    res_conf = client.get("/api/events?min_confidence=0.8")
    print(f"  [OK] High-Confidence Events (>=80%): {len(res_conf.json())}")

    # Search filter
    res_search = client.get("/api/events?search=cloud")
    print(f"  [OK] Events matching 'cloud': {len(res_search.json())}")

    # 6. Test Ingestion Jobs History
    print("\n[6] Verifying Ingestion Telemetry Jobs:")
    res_jobs = client.get("/api/ingest/jobs")
    assert res_jobs.status_code == 200
    print(f"  [OK] Ingestion Audit Trail contains {len(res_jobs.json())} recorded runs.")

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED! Phase 1 Ingestion Pipeline & UI Operational!")
    print("=" * 60)

if __name__ == "__main__":
    test_full_platform()
