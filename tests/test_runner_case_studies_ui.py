"""Regression checks for the read-only Intelligence ticker review UI."""
from pathlib import Path
import shutil
import subprocess

import main


def test_intelligence_ticker_review_in_existing_section():
    html = main.research_page().body.decode("utf-8")
    assert 'id="missed-opportunities"' in html
    assert 'id="runner-case-study"' in html
    assert 'id="runner-case-form"' in html
    assert 'id="runner-case-rows"' in html
    assert "LVWR, PYXS, KRNT" in html
    assert "/research/audit-export?symbols=" in html
    assert "credentials:\"same-origin\"" in html
    assert "not proof" in html
    assert html.count('<section class="section"') == 13


def test_ticker_review_javascript_parses_with_node_if_installed(tmp_path):
    if shutil.which("node") is None:
        return
    html = main.research_page().body.decode("utf-8")
    script = html.split('<script id="jarvis-ticker-review">', 1)[1].split("</script>", 1)[0]
    file = tmp_path / "runner_case_review.js"
    file.write_text(script, encoding="utf-8")
    subprocess.run(["node", "--check", str(file)], check=True,
                   capture_output=True, text=True)
