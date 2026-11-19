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
