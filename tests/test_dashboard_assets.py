import main


def test_history_dashboard_route_serves_extracted_javascript_asset():
    response = main.history_dashboard_js()

    assert response.media_type == "application/javascript"
    body = response.body.decode("utf-8")
    assert "loadHistoryDashboard" in body
    assert "paper-tracked" in body or "candidate" in body.lower()


def test_intelligence_page_preserves_javascript_word_boundary_regex():
    html = main.research_page().body.decode("utf-8")

    assert r"replace(/\b\w/g" in html
    assert "\x08" not in html


def test_history_page_loads_extracted_html_template():
    response = main.history_page()
    html = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "<title>Jarvis · Market Overview</title>" in html
    assert 'id="opportunities"' in html
    assert '/history/dashboard.js?v=' in html


def test_shared_analytics_template_replaces_navigation_and_page_labels():
    html = main.analytics_tab_page("market-breadth")

    assert "__TITLE__" not in html
    assert "__NAV__" not in html
    assert "Market Breadth" in html
    assert 'href="/market-breadth"' in html


def test_settings_page_loads_extracted_template():
    html = main.customize_page()

    assert "<title>Jarvis · Settings</title>" in html
    assert "Appearance" in html


def test_paper_trading_page_loads_extracted_template():
    response = main.paper_trading_page()
    html = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "<title>Jarvis Paper Trading</title>" in html
    assert "PAPER ONLY · NO BROKER CONNECTION" in html


def test_data_health_page_and_api_are_read_only_and_descriptive(monkeypatch):
    monkeypatch.setattr(main, "latest_history_state", lambda: {
        "paper_tracker": {
            "A": {"symbol": "AAA", "asset_type": "stock", "currently_surfaced": True,
                  "price_validation_status": "verified", "latest_price": 12.5,
                  "price_source": "Alpaca IEX", "price_as_of": "2026-11-19T15:00:00+00:00",
                  "data_quality": {"data_type": "live", "verification_status": "verified",
                                   "age_seconds": 30, "buy_eligible": True}},
            "B": {"symbol": "BBB", "asset_type": "crypto", "currently_surfaced": True,
                  "price_validation_status": "unverified", "latest_price": 0,
                  "data_quality": {"data_type": "unknown"}},
        },
        "scan_diagnostics": {"diagnostics": {"alpaca_quote_status": {"feed": "iex"},
            "crypto": {"price_validation": {"verified": 0, "unverified": 1}},
            "stocks": {"scan_errors": 0}, "timing_breakdown": {"provider_activity": {
                "provider_http_429_count": 0, "provider_http_5xx_count": 0}}}},
        "last_scan_completed_at": "2026-11-19T15:00:00+00:00",
        "scan_run_id": "test-run", "history_source": "test",
    })
    html = main.data_health_page().body.decode("utf-8")
    data = main.data_health_data()
    assert "Data Health" in html
    assert 'href="/data-health"' in html
    assert data["candidate_count"] == 2
    assert data["price_status"]["all"]["verified"] == 1
    assert data["price_status"]["all"]["unverified"] == 1
    assert data["data_gaps"]["missing_source"] == 1
    assert data["providers"]["stock_price_feed"] == "iex"
    assert data["scan_errors"] == 0
