"""Presentation-only regression tests for the second Jarvis design pass."""
from pathlib import Path
import shutil
import subprocess

import main


def html_body(response):
    return response.body.decode("utf-8")


def test_intelligence_preserves_existing_data_tables_in_four_groups():
    html = html_body(main.research_page())
    for group in ["signal-discovery", "signal-performance",
                  "missed-opportunities", "advanced-diagnostics"]:
        assert 'id="' + group + '"' in html
    assert 'id="signal-discovery" open' in html
    assert 'id="signal-performance" open' in html
    assert 'class="intel-section-jump"' in html
    assert html.count('<section class="section"') == 13
    for identifier in [
        "features", "outcomes", "shadowRadar", "earlyWarningRadar",
        "missedRunnerTracker", "signalQualityBySetup",
    ]:
        # Keep the existing sections and their JavaScript target IDs unchanged.
        assert identifier in html
    assert "Swipe horizontally for more columns" in html


def test_paper_trading_shows_four_important_metrics_before_the_details():
    html = html_body(main.paper_trading_page())
    assert 'id="paper-summary"' in html
    assert 'class="paper-extra-metrics"' in html
    assert 'id="metrics"' in html
    assert 'const topMetrics=["Account equity","Total return","Open positions","Available cash"]' in html
    assert 'topMetrics.map(label=>metrics.find' in html
    assert 'metrics.filter(x=>!topMetrics.includes(x[0]))' in html
    assert 'id="positions"' in html and 'id="closed"' in html
    assert 'id="ledger"' in html and 'id="allocation"' in html
    assert "Swipe horizontally for more columns" in html


def test_paper_trading_inline_javascript_parses_if_node_available(tmp_path):
    if shutil.which("node") is None:
        return
    html = html_body(main.paper_trading_page())
    js = html.split("<script>", 1)[1].split("</script>", 1)[0]
    script_path = tmp_path / "paper_inline.js"
    script_path.write_text(js, encoding="utf-8")
    subprocess.run(["node", "--check", str(script_path)], check=True,
                   capture_output=True, text=True)
