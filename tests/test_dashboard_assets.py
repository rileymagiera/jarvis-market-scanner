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
                  "price_source": "Alpaca IEX", "price_as_of": "2026-11-19T15:00:00+00:00", "catalyst_headline_count": 2,
                  "data_quality": {"data_type": "live", "verification_status": "verified",
                                   "age_seconds": 30, "buy_eligible": True}},
            "B": {"symbol": "BBB", "asset_type": "crypto", "currently_surfaced": True,
                  "price_validation_status": "unverified", "price_validation_note": "Kraken ticker not validated: trade age=412.0s; ticker/trade difference=0.12%", "latest_price": 0, "catalyst_headline_count": 1,
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
    assert data["candidates"][0]["validation_note"].startswith("Kraken ticker not validated")
    assert data["candidates"][0]["failure_reason"] == "Kraken ticker not validated: trade age=412.0s; ticker/trade difference=0.12%"
    assert data["failure_reasons"] == [{"reason": data["candidates"][0]["failure_reason"], "count": 1}]
    assert data["data_gaps"]["missing_source"] == 1
    assert data["providers"]["stock_price_feed"] == "iex"
    assert data["field_coverage"]["stocks"]["catalyst_headline_count"] == 1
    assert data["field_coverage"]["crypto"]["catalyst_headline_count"] == 1
    assert data["scan_errors"] == 0



def test_webull_yahoo_shadow_comparison_is_observational_only():
    webull = {"price": 100.0, "as_of": "2026-11-19T15:00:00+00:00", "age_minutes": 1.0}
    comparison = main.compare_webull_yahoo_prices(webull, 100.5, "2026-11-19T15:00:30+00:00", 1.5)
    assert comparison["status"] == "comparable"
    assert comparison["difference_pct"] == 0.4988
    assert comparison["timestamp_difference_seconds"] == 30.0
    stale = main.compare_webull_yahoo_prices(webull, 100.5, "2026-11-19T14:00:00+00:00", 60)
    assert stale["status"] == "yahoo_unavailable_or_stale"
    assert stale["difference_pct"] is None


def test_webull_shadow_sample_rotates_without_changing_universe():
    symbols = [f"SYM{i}" for i in range(65)]
    first = main.select_webull_shadow_symbols(symbols, run_number=1, sample_size=30)
    second = main.select_webull_shadow_symbols(symbols, run_number=2, sample_size=30)
    assert len(first) == len(second) == 30
    assert set(first).isdisjoint(set(second))
    assert set(first + second).issubset(set(symbols))


def test_webull_yahoo_history_persists_comparison_records(tmp_path):
    path = tmp_path / "comparison.json"
    records = [
        {"symbol": "AAA", "status": "comparable", "difference_pct": 0.25},
        {"symbol": "BBB", "status": "yahoo_unavailable_or_stale", "difference_pct": None},
    ]
    result = main.persist_webull_yahoo_history(records, "2026-11-19T15:00:00+00:00", run_id="test-run", path=path)
    assert result["summary"]["record_count"] == 2
    assert result["summary"]["comparable_count"] == 1
    assert result["summary"]["mean_difference_pct"] == 0.25
    saved = __import__("json").loads(path.read_text())
    assert len(saved["records"]) == 2
    assert saved["records"][0]["run_id"] == "test-run"


def test_crypto_shadow_comparison_is_observational_and_persisted(tmp_path):
    quote = {"price": 100.0, "as_of": "2026-11-19T15:00:00+00:00", "age_seconds": 20.0, "spread_pct": 0.1, "quote_currency": "USD"}
    row = main.compare_crypto_shadow_quote("coinbase", "BTCUSDT", 100.2, "2026-11-19T15:00:00+00:00", quote)
    assert row["status"] == "comparable"
    assert row["difference_pct"] == 0.1998
    same_source = main.compare_crypto_shadow_quote("coinbase", "BTCUSDT", 100.2, "2026-11-19T15:00:00+00:00", quote, None, "USD", "Coinbase Exchange public API")
    assert same_source["status"] == "same_as_primary_source"
    mismatch = main.compare_crypto_shadow_quote("binance_us", "BTCUSDT", 100.2, "2026-11-19T15:00:00+00:00", {**quote, "quote_currency": "USDT"})
    assert mismatch["status"] == "quote_currency_mismatch"
    assert mismatch["difference_pct"] is None
    missing = main.compare_crypto_shadow_quote("binance_us", "BTCUSDT", 100.2, None, None, "HTTPError")
    assert missing["status"] == "unavailable"
    assert missing["difference_pct"] is None
    result = main.persist_crypto_shadow_history(
        [row, missing], "2026-11-19T15:00:00+00:00", "test-run", tmp_path / "crypto.json"
    )
    assert result["summary"]["providers"]["coinbase"]["comparable"] == 1
    assert result["summary"]["providers"]["binance_us"]["unavailable"] == 1


def test_crypto_shadow_sampling_rotates_and_is_bounded():
    symbols = [f"COIN{i}USDT" for i in range(55)]
    first = main.select_crypto_shadow_symbols(symbols, run_number=1, sample_size=20)
    second = main.select_crypto_shadow_symbols(symbols, run_number=2, sample_size=20)
    assert len(first) == len(second) == 20
    assert set(first).isdisjoint(set(second))
    assert set(first + second).issubset(set(symbols))
