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
    assert "deck-flow-gradient" in landing
    assert 'NOT A PRICE CHART' in header
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
                       "home-tracked", "home-radar", "home-verified"):
        assert f'id="{identifier}"' in landing
    assert "fetch('/home/brief'" in landing
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
            "stock:AAA": {"symbol": "AAA", "currently_surfaced": True,
                          "price_validation_status": "verified"},
            "stock:BBB": {"symbol": "BBB", "currently_surfaced": False,
                          "price_validation_status": "verified"},
            "crypto:CCC": {"symbol": "CCC", "currently_surfaced": True,
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
        "snapshot_available": True,
    }
    assert "paper_tracker" not in result
    monkeypatch.setattr(main, "latest_history_state", lambda: {})
    empty = json.loads(main.home_brief().body)
    assert empty["snapshot_available"] is False
    assert empty["tracked_assets"] == 0


def test_market_overview_research_and_buy_separation_still_present():
    html = _overview()
    js = main.history_dashboard_js().body.decode("utf-8")
    assert 'PAPER SIMULATION · NO LIVE ORDERS' in html
    assert 'latest completed scan, saved discovery ledger' in html
    assert "setSnapshotIndicator('error')" in js
    assert "setSnapshotIndicator(Number.isFinite(snapshotMs)" in js
    assert "verified ≠ BUY-eligible" in js
    assert "fetch('/history/overview-data'" in js
