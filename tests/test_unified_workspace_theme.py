"""Presentation-only regression checks for the unified Jarvis workspace theme.

No finance, scanner, BUY eligibility or state persistence code is allowed to change
when adding shared page chrome and visual styling.
"""
from pathlib import Path

import main


ROOT = Path(__file__).resolve().parents[1]
PAGES = {
    "market-overview": main.history_page,
    "signal-lab": main.research_page,
    "data-quality": main.data_health_page,
    "paper-trading": main.paper_trading_page,
    "settings": main.customize,
    "market-pulse": main.market_breadth_page,
    "momentum-tracker": main.momentum_evidence_page,
    "signal-performance": main.discovery_performance_page,
}


def page_html(view):
    response = view()
    return response if isinstance(response, str) else response.body.decode("utf-8")


def test_every_working_page_has_single_cinematic_header_and_retains_navigation():
    for name, view in PAGES.items():
        html = page_html(view)
        assert '<body class="jarvis-workspace-page"><main>' in html, name
        assert html.count('class="jarvis-hero-topline"') == 1, name
        assert html.count('class="jarvis-hero-content"') == 1, name
        assert html.count('class="jarvis-hero-graphic"') == 1, name
        assert 'RESEARCH TERMINAL <strong>/ PAPER MODE</strong>' in html, name
        assert 'src="/assets/help_panels.js" defer' in html, name
        assert 'href="/home"' in html, name
        assert 'href="/history"' in html, name
        assert 'href="/paper-trading"' in html, name
        assert 'id="jarvis-mobile-navigation"' in html, name
        assert html.count('<nav class="tabs"') + html.count('<nav class="top-tabs"') == 1, name


def test_home_keeps_its_distinct_hero_without_working_page_class():
    home = page_html(main.home_page)
    assert '<header class="overview-deck"' in home
    assert '<body class="jarvis-workspace-page">' not in home
    assert 'id="home-tracked"' in home
    assert "fetch('/home/brief'" in home


def test_data_and_trading_controls_stay_connected_to_their_original_endpoints():
    market = page_html(main.history_page)
    paper = page_html(main.paper_trading_page)
    signal_lab = page_html(main.research_page)
    quality = page_html(main.data_health_page)
    settings = page_html(main.customize)
    assert market.count('id="updated"') == 1
    assert 'id="action-center"' in market
    assert 'id="opportunities"' in market
    assert 'id="candidate-ledger"' in market
    assert 'src="/history/dashboard.js?v=' in market
    assert paper.count('id="updated"') == 1
    for marker in ("paper-summary", "positions", "closed", "ledger", "strategyFilter"):
        assert f'id="{marker}"' in paper
    for marker in ("allocation", "strategy-performance", "edge-research"):
        assert f'data-paper-panel="{marker}"' in paper
    assert '"/paper-trading/data?refresh="' in paper
    assert 'fetch("/intelligence/data")' in signal_lab
    assert '"/data-health/data"' in quality
    assert "jarvisDashboardPrefsV1" in settings
    assert 'data-jarvis-help="settings-instructions"' in settings


def test_analytics_pages_keep_their_data_endpoints_and_kind_identifiers():
    for slug, view in (
        ("market-breadth", main.market_breadth_page),
        ("momentum-evidence", main.momentum_evidence_page),
        ("discovery-performance", main.discovery_performance_page),
    ):
        html = page_html(view)
        assert 'const kind="' + slug + '"' in html
        assert 'data-jarvis-help="' + slug + '-instructions"' in html
        assert 'id="thead"' in html and 'id="tbody"' in html
        assert 'id="summary"' in html
        assert 'id="status"' in html


def test_style_is_scoped_and_keeps_accessibility_and_user_preferences():
    css = (ROOT / "static" / "workspace.css").read_text(encoding="utf-8")
    assert "body.jarvis-workspace-page" in css
    assert "prefers-reduced-motion:reduce" in css
    assert "focus-visible" in css
    assert ".jarvis-help" in css
    assert ":root{--accent:#aba0ff}" in css
    assert "font-family:Georgia" not in css
    assert "paper_tracker" not in css
