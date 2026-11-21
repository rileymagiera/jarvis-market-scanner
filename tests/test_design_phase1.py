"""Read-only UI presentation regression tests for Jarvis design phase 1."""
from pathlib import Path
import shutil
import subprocess

import main


PATHS = [
    "/home", "/history", "/intelligence", "/paper-trading", "/data-health",
    "/discovery-performance", "/market-breadth", "/momentum-evidence",
    "/settings",
]


def page_body(response):
    return response.body.decode("utf-8")


def test_mobile_navigation_keeps_all_nine_pages_and_three_shortcuts():
    pages = [
        page_body(main.home_page()),
        page_body(main.history_page()),
        page_body(main.paper_trading_page()),
        page_body(main.research_page()),
        page_body(main.data_health_page()),
        page_body(main.customize()),
        page_body(main.market_breadth_page()),
        page_body(main.momentum_evidence_page()),
        page_body(main.discovery_performance_page()),
    ]
    for page in pages:
        assert page.count('id="jarvis-mobile-navigation"') == 1
        mobile = page.split('<nav id="jarvis-mobile-navigation"', 1)[1].split("</nav>", 1)[0]
        shortcuts = mobile.split('<details class="jarvis-mobile-more">', 1)[0]
        assert shortcuts.count("<a href=") == 3
        for path in PATHS:
            assert 'href="' + path + '"' in mobile
        assert 'jarvis-mobile-more-links' in mobile
        assert 'min-height:48px' in page


def test_current_mobile_tab_is_identified_on_primary_and_more_pages():
    landing = page_body(main.home_page())
    overview = page_body(main.history_page())
    intelligence = page_body(main.research_page())
    breadth = page_body(main.market_breadth_page())
    assert '<a href="/home" aria-current="page">Home</a>' in landing
    assert '<a href="/history" aria-current="page">Market Overview</a>' in overview
    assert '<a href="/intelligence" aria-current="page">Signal Lab</a>' in intelligence
    assert "More · Market Pulse" in breadth
    assert '<a href="/market-breadth" aria-current="page">Market Pulse</a>' in breadth


def test_status_cards_separate_official_buy_from_research_and_show_age():
    html = page_body(main.history_page())
    js = main.history_dashboard_js().body.decode("utf-8")
    for field in ["scan-regime", "scan-official-buys", "scan-verified-prices", "scan-age"]:
        assert 'id="' + field + '"' in html
        assert "getElementById('" + field + "')" in js
    assert "ACTIONABLE SETUPS" in html
    assert "BUY + actionable" not in html
    assert "setSnapshotIndicator('error')" in js
    assert "setSnapshotIndicator(Number.isFinite(snapshotMs)" in js
    assert "verified ≠ BUY-eligible" in js


def test_card_buttons_open_analysis_or_flip_targets_with_keyboard_access():
    js = main.history_dashboard_js().body.decode("utf-8")
    assert 'data-card-action="analysis"' in js
    assert 'data-card-action="targets"' in js
    assert 'data-card-action="back"' in js
    assert "function openCandidateAnalysis(card)" in js
    assert "function setCardFace(card,flipped)" in js
    assert "front.inert=flipped" in js
    assert "back.inert=!flipped" in js
    assert "card.click()" not in js
    assert "role=\\\"button\\\" aria-pressed" not in js
    assert "candidatePriceStatus" in js


def test_history_javascript_remains_parseable_when_node_available():
    if shutil.which("node") is None:
        return
    path = Path(__file__).resolve().parents[1] / "static" / "history_dashboard.js"
    subprocess.run(["node", "--check", str(path)], check=True, capture_output=True, text=True)
