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
    assert "Market Pulse" in html
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
    assert "Data Quality" in html
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


def test_radar_marks_cross_checked_delayed_quotes_as_delayed():
    body = main.history_dashboard_js().body.decode("utf-8")
    assert "VERIFIED · DELAYED" in body
    assert "data_quality.data_type==='delayed'" in body
    assert "discovery_return_status==='provisional'" in body


def test_market_overview_projects_saved_history_without_changing_full_ledger(monkeypatch):
    import json

    plan = {"target_1": 13, "calculation": {"risk_per_unit": 1}}
    candidate = {
        "symbol": "AAA",
        "asset_type": "stock",
        "last_stage": "DEVELOPING SETUP",
        "discovery_return_pct": 0,
        "discovery_return_status": "provisional",
        "price_validation_status": "verified",
        "data_quality": {"data_type": "delayed"},
        "levels": plan,
        "grade_history": [{"from": "EARLY WATCH", "to": "DEVELOPING SETUP"}],
        "signal_history": [{"entry_score": 65}],
        "buy_gate_history": [{"passed": False}],
    }
    state = {
        "paper_tracker": {
            "stock:AAA": candidate,
            "crypto:BBB": {"symbol": "BBB", "asset_type": "crypto", "currently_surfaced": False},
        },
        "lifetime": {"integrity": {"retained_stock_baselines": 1}, "checkpoints": {"1": {"wins": 1}}},
        "buy_signals": [{"symbol": "AAA", "status": "active"}],
        "last_scan_completed_at": "2026-11-20T19:45:00+00:00",
        "scan_run_id": "fixture-scan",
        "history_source": "render_snapshot_fast",
    }
    monkeypatch.setattr(main, "latest_history_state", lambda: state)

    response = main.market_overview_data()
    overview = json.loads(response.body)
    full = main.history_data()
    assert response.headers["cache-control"] == "no-store"
    assert overview["scan_run_id"] == full["scan_run_id"]
    assert overview["last_scan_completed_at"] == full["last_scan_completed_at"]
    assert overview["history_source"] == full["history_source"]
    assert overview["buy_signals"] == full["buy_signals"]
    assert overview["lifetime"] == full["lifetime"]
    assert list(overview["paper_tracker"]) == list(full["paper_tracker"])
    assert overview["paper_tracker"]["crypto:BBB"] == full["paper_tracker"]["crypto:BBB"]
    assert overview["paper_tracker"]["stock:AAA"] == {
        key: value for key, value in candidate.items() if key not in {"signal_history", "buy_gate_history"}
    }
    assert full["paper_tracker"]["stock:AAA"]["signal_history"] == [{"entry_score": 65}]
    assert full["paper_tracker"]["stock:AAA"]["buy_gate_history"] == [{"passed": False}]


def test_receipt_tracks_two_distinct_completed_runs(monkeypatch):
    import json
    for name in ("JARVIS_GMAIL_USER", "JARVIS_GMAIL_APP_PASSWORD", "JARVIS_ALERT_TO"):
        monkeypatch.setenv(name, "fixture")
    from send_alerts import completed_scan_pair

    candidate = {"symbol": "ABC", "asset_type": "stock", "stage": "EARLY WATCH",
                 "price_validation_status": "verified"}
    first = completed_scan_pair([], [candidate], "10", "2026-11-21T00:00:00Z")
    second = completed_scan_pair(first, [], "11", "2026-11-21T00:10:00Z")
    retry = completed_scan_pair(second, [candidate], "11", "2026-11-21T00:11:00Z")
    assert [scan["run_id"] for scan in retry] == ["10", "11"]
    assert second[-1]["members"] == {}
    history = {"paper_tracker": {"stock:ABC": candidate}, "lifetime": {}, "buy_signals": [],
               "scan_run_id": "11", "last_scan_completed_at": retry[-1]["completed_at"],
               "history_source": "render_snapshot_fast", "scan_diagnostics": {"completed_scan_pair": retry}}
    monkeypatch.setattr(main, "history_data", lambda: history)
    receipt = json.loads(main.market_overview_data().body)
    assert receipt["completed_scan_pair"][-1]["run_id"] == receipt["scan_run_id"]
    assert list(receipt["completed_scan_pair"][-1]["members"]) == list(receipt["paper_tracker"])
    assert completed_scan_pair(retry, [], "12", "2026-11-21T00:20:00Z")[0] == retry[-1]


def test_market_overview_retains_action_center_and_removes_redundant_archive_tables():
    html = main.history_page().body.decode('utf-8')
    js = main.history_dashboard_js().body.decode('utf-8')
    for required in ('id="opportunities"', 'id="performance-archive"',
                     'id="candidate-ledger"', 'id="radar-timeline"',
                     'Price Target 1 / Price Target 2 scorecard'):
        assert required in html
    assert 'Grade movement history' not in html
    assert 'Not in latest scan' not in html
    assert "getElementById('moves')" not in js
    assert "getElementById('faded')" not in js
    assert 'candidateChartUrl' in js
