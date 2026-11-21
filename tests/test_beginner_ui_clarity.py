"""Regression checks for newbie-facing Jarvis UI wording and navigation."""
import re

import main


EXPECTED_NAV = [
    ("home", "Home"),
    ("data-health", "Data Quality"),
    ("history", "Market Overview"),
    ("market-breadth", "Market Pulse"),
    ("momentum-evidence", "Momentum Tracker"),
    ("paper-trading", "Paper Trading"),
    ("settings", "Settings"),
    ("intelligence", "Signal Lab"),
    ("discovery-performance", "Signal Performance"),
]


def nav_links(html):
    match = re.search(r"<nav\b[^>]*>(.*?)</nav>", html, re.DOTALL)
    assert match, "Navigation should be present"
    return re.findall(r'<a\b[^>]*href="/([^"]+)"[^>]*>([^<]+)</a>', match.group(1))


def test_navigation_is_home_first_then_alphabetical_across_dashboard_pages():
    pages = [
        main.home_page().body.decode("utf-8"),
        main.history_page().body.decode("utf-8"),
        main.paper_trading_page().body.decode("utf-8"),
        main.research_page().body.decode("utf-8"),
        main.data_health_page().body.decode("utf-8"),
        main.customize_page(),
        main.analytics_tab_page("market-breadth"),
        main.analytics_tab_page("momentum-evidence"),
        main.analytics_tab_page("discovery-performance"),
    ]
    for page in pages:
        assert nav_links(page) == EXPECTED_NAV


def test_paper_page_explains_both_experiments_and_risk_off_blockers():
    page = main.paper_trading_page().body.decode("utf-8")
    assert "Paper Trading · start here" in page
    assert "Risk-Off blocks official BUYs and early-entry experiments" in page
    assert "Official BUYs and qualifying Risk-Off experimental entries are ranked first" in page
    assert "early-entry experiments have their own verified-price" in page
    assert "enabled for the latest scan" in page
    assert "No eligible official or experimental paper entries" in page
    assert "remain disabled until implementation/tests" not in page


def test_market_overview_is_labelled_saved_scan_not_live_stream():
    page = main.history_page().body.decode("utf-8")
    assert "latest completed scan, saved discovery ledger" in page
    assert "latest completed hourly scan" not in page
    assert "Time to Target 1" in page
    assert "Time to Target 2" in page
