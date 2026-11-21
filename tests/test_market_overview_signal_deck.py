"""Home landing-page presentation, saved-scan contract, and Market Overview isolation."""
import json
import main


def _home():
    return main.home_page().body.decode("utf-8")


def _overview():
    return main.history_page().body.decode("utf-8")


def test_futuristic_header_lives_on_home_and_market_overview_remains_full_scanner():
    landing, overview = _home(), _overview()
    assert landing.count('<header class="overview-deck"') == 1
    header = landing.split('<header class="overview-deck"', 1)[1].split("</header>", 1)[0]
    assert "RUNNER IMPACT" in landing
    assert 'id="runner-impact-rows"' in landing
    assert "NOT A PRICE CHART" not in header
    assert "Peak gains are historical observations, not captured profits" in header
    assert "recorded peak" in header.lower()
    assert 'href="/history#action-center"' in header
    assert 'href="/paper-trading"' in header
    assert 'href="/data-health"' in header
    assert "<h1>JARVIS" in header
    assert "The edge" not in header
    assert '<h1>JARVIS<span class="deck-title-dot">.</span></h1>' in header
    assert '<header class="overview-deck"' not in overview
    assert '<header class="command-header"' in overview
    assert 'id="updated"' in overview
    assert '<nav class="top-tabs"' in landing and '<nav class="top-tabs"' in overview
    for identifier in ("scan-regime", "scan-official-buys", "scan-verified-prices",
                       "scan-age", "action-center", "opportunities"):
        assert f'id="{identifier}"' in overview


def test_home_header_uses_snapshot_status_and_has_no_live_order_controls():
    landing = _home()
    header = landing.split('<header class="overview-deck"', 1)[1].split("</header>", 1)[0]
    assert header.count('id="updated"') == 1
    assert 'role="status" aria-live="polite"' in header
    for identifier in ("deck-status-orb", "deck-health-label", "deck-snapshot-at",
                       "home-tracked", "home-radar", "home-verified", "runner-impact-rows",
                       "runner-impact-status"):
        assert f'id="{identifier}"' in landing
    assert "fetch('/home/brief'" in landing
    assert 'renderLeaders(data)' in landing
    assert 'data.runner_highlights' in landing
    assert "No eligible verified runner gains" in landing
    assert "SAVED SCAN · AGED" in landing
    assert "SAVED SCAN · UNAVAILABLE" in landing
    assert "not a live quote feed" in landing
    assert "No live brokerage orders" in landing
    assert '@media(max-width:760px)' in landing
    assert '@media(max-width:420px)' in landing
    assert '@media(prefers-reduced-motion:reduce)' in landing


def test_home_brief_is_read_only_and_minimal(monkeypatch):
    state = {
        "last_scan_completed_at": "2026-11-21T13:00:00+00:00",
        "paper_tracker": {
            "stock:AAA": {"symbol": "AAA", "asset_type": "stock", "currently_surfaced": True,
                          "price_validation_status": "verified", "discovery_baseline_status": "live_verified",
                          "session_aware_tracking": True, "initial_price": 10.0,
                          "max_gain_pct": 70.0, "return_pct": 30.0},
            "stock:BBB": {"symbol": "BBB", "currently_surfaced": False,
                          "price_validation_status": "verified"},
            "crypto:CCC": {"symbol": "CCC", "asset_type": "crypto", "currently_surfaced": True,
                           "price_validation_status": "last_known"},
        },
    }
    monkeypatch.setattr(main, "latest_history_state", lambda: state)
    result = json.loads(main.home_brief().body)
    assert result == {
        "last_scan_completed_at": "2026-11-21T13:00:00+00:00",
        "tracked_assets": 3,
        "radar_candidates": 2,
        "verified_candidates": 1,
        "runner_highlights": [{"symbol": "AAA", "asset_type": "stock", "peak_gain_pct": 70.0,
                                "latest_return_pct": 30.0}],
        "snapshot_available": True,
    }
    assert "paper_tracker" not in result
    monkeypatch.setattr(main, "latest_history_state", lambda: {})
    empty = json.loads(main.home_brief().body)
    assert empty["snapshot_available"] is False
    assert empty["tracked_assets"] == 0
    assert empty["runner_highlights"] == []


def test_market_overview_research_and_buy_separation_still_present():
    html = _overview()
    js = main.history_dashboard_js().body.decode("utf-8")
    assert 'PAPER SIMULATION · NO LIVE ORDERS' in html
    assert 'latest completed scan, saved discovery ledger' in html
    assert "setSnapshotIndicator('error')" in js
    assert "setSnapshotIndicator(Number.isFinite(snapshotMs)" in js
    assert "verified ≠ BUY-eligible" in js
    assert "fetch('/history/overview-data'" in js

def test_verified_runner_board_excludes_provisional_legacy_stale_and_unsurfaced(monkeypatch):
    verified = {"asset_type": "crypto", "currently_surfaced": True,
                "price_validation_status": "verified",
                "discovery_baseline_status": "live_verified",
                "initial_price": 1.0, "return_pct": 20.0, "max_gain_pct": 100.0}
    records = {
        "crypto:SOL": dict(verified, symbol="SOL"),
        "crypto:BTC": dict(verified, symbol="BTC", max_gain_pct=150.0, return_pct=-5.0),
        "crypto:provisional": dict(verified, symbol="NOTVALID", discovery_baseline_status="unverified_at_discovery", max_gain_pct=15000.0),
        "crypto:unverified": dict(verified, symbol="UNVERIFIED", price_validation_status="unverified", max_gain_pct=12000.0),
        "crypto:archived": dict(verified, symbol="ARCHIVED", currently_surfaced=False, max_gain_pct=9999.0),
        "stock:legacy": dict(verified, symbol="LEGACY", asset_type="stock", max_gain_pct=999.0),
        "crypto:bad-value": dict(verified, symbol="BAD", max_gain_pct=float("inf")),
        "crypto:bad-latest": dict(verified, symbol="BROKEN", max_gain_pct=10.0, return_pct=100.0),
        "crypto:zero": dict(verified, symbol="ZERO", max_gain_pct=0.0),
    }
    monkeypatch.setattr(main, "latest_history_state", lambda: {
        "last_scan_completed_at": "2026-11-21T13:00:00+00:00", "paper_tracker": records})
    result = json.loads(main.home_brief().body)
    assert [row["symbol"] for row in result["runner_highlights"]] == ["BTC", "SOL"]
    assert result["runner_highlights"][0] == {
        "symbol": "BTC", "asset_type": "crypto", "peak_gain_pct": 150.0, "latest_return_pct": -5.0}
    assert len(result["runner_highlights"]) == 2
    assert result["radar_candidates"] == 8
    assert "paper_tracker" not in result


def test_home_uses_text_safe_browser_rendering_and_keeps_bounded_read_only_payload():
    html = _home()
    assert "leaderRows.replaceChildren()" in html
    assert "document.createElement(tag)" in html
    assert "node.textContent=label" in html
    assert "leaderRows.innerHTML" not in html
    assert "slice(0,4)" in html
    assert "Captured profits" not in html
    assert "Peak gains are historical observations" in html


def test_home_inline_script_parses_with_node_if_available():
    import shutil
    import subprocess
    import re
    if not shutil.which("node"):
        return
    inline = re.findall(r"<script>(.*?)</script>", _home(), flags=re.DOTALL)
    assert len(inline) == 1
    result = subprocess.run(["node", "--check", "-"], input=inline[0],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr

