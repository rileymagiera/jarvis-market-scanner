"""Presentation-only regressions for the Market Overview Signal Operations header."""
import main


def _page():
    return main.history_page().body.decode("utf-8")


def test_new_header_is_a_full_replacement_and_keeps_all_navigation():
    html = _page()
    assert html.count('<header class="overview-deck"') == 1
    header = html.split('<header class="overview-deck"', 1)[1].split("</header>", 1)[0]
    assert 'class="command-header"' not in header
    assert 'class="brand-mark"' not in header
    assert '<h1>Market' in header and 'deck-title-second">Overview</span>' in header
    assert 'aria-label="Illustrative research workflow, not a live market chart"' in header
    assert 'NOT A PRICE CHART' in header
    assert 'href="#action-center"' in header
    assert 'href="/paper-trading"' in header
    assert 'href="/data-health"' in header
    assert '<nav class="top-tabs"' in html
    assert 'href="/history" aria-current="page">Market Overview</a>' in html


def test_new_header_read_only_status_is_connected_to_saved_scan():
    html = _page()
    header = html.split('<header class="overview-deck"', 1)[1].split("</header>", 1)[0]
    js = main.history_dashboard_js().body.decode("utf-8")
    assert header.count('id="updated"') == 1
    assert 'role="status" aria-live="polite"' in header
    for identifier in ("deck-status-orb", "deck-health-label", "deck-snapshot-at"):
        assert f'id="{identifier}"' in header
        assert f"getElementById('{identifier}')" in js
    assert "deckStamp.textContent=snapshotTime" in js
    assert "SAVED SCAN · AGED" in js and "SAVED SCAN · UNAVAILABLE" in js
    assert "fetch('/history/overview-data'" in js
    assert "setSnapshotIndicator('error')" in js
    assert "setSnapshotIndicator(Number.isFinite(snapshotMs)" in js


def test_header_responsive_and_other_trade_controls_unchanged():
    html = _page()
    assert '@media(max-width:760px)' in html
    assert '@media(max-width:420px)' in html
    assert '@media(prefers-reduced-motion:reduce)' in html
    for identifier in ("scan-regime", "scan-official-buys", "scan-verified-prices",
                       "scan-age", "action-center", "opportunities"):
        assert f'id="{identifier}"' in html
    assert 'PAPER MODE' in html
    assert 'PAPER SIMULATION · NO LIVE ORDERS' in html
    assert 'latest completed scan, saved discovery ledger' in html
