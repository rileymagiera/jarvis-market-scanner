"""Presentation-only regression coverage for trustworthy discovery return summaries."""
import shutil
import subprocess

import main


def test_discovery_summary_marks_live_vs_provisional_without_changing_tracking():
    html = main.analytics_tab_page("discovery-performance")
    assert "LIVE-VERIFIED RETURNS" in html
    assert "EOD / PROVISIONAL RETURNS" in html
    assert "NOT paper-trade profit or loss" in html
    assert 'x.price_validation_status==="verified"' in html
    assert 'x.discovery_return_status!=="provisional"' in html
    assert " · PROVISIONAL" in html and " · EOD" in html


def test_discovery_summary_ignores_stale_and_provisional_values_in_live_average():
    if shutil.which("node") is None:
        return

    html = main.analytics_tab_page("discovery-performance")
    js = html.split("<script>", 1)[1].split("</script>", 1)[0]
    js = js.split('fetch("/history/data"', 1)[0]
    setup = """
    const nodes = {};
    const document = {
      getElementById(id) {
        if (id === "breadthHeatmap" || id === "breadthHeatmapFoot") return null;
        return nodes[id] ||= { innerHTML: "", textContent: "" };
      }
    };
    """
    check = """
    render({paper_tracker:{
      verified_up:{symbol:"UP",price_validation_status:"verified",discovery_baseline_status:"live_verified",discovery_return_pct:10},
      verified_down:{symbol:"DOWN",price_validation_status:"verified",discovery_baseline_status:"live_verified",discovery_return_pct:-10},
      eod:{symbol:"EOD",price_validation_status:"last_known",discovery_baseline_status:"last_known_eod",discovery_return_pct:-60},
      provisional:{symbol:"PROV",price_validation_status:"unverified",discovery_baseline_status:"unverified_at_discovery",discovery_return_status:"provisional",discovery_return_pct:-80},
      provisional_baseline:{symbol:"BASE",price_validation_status:"verified",discovery_baseline_status:"unverified_at_discovery",discovery_return_pct:-100}
    }});
    const summary = nodes.summary.innerHTML;
    const body = nodes.tbody.innerHTML;
    if (!summary.includes("LIVE-VERIFIED RETURNS</span><strong>2</strong>")) throw Error("live cohort count");
    if (!summary.includes("POSITIVE LIVE RETURNS</span><strong>1 / 2</strong>")) throw Error("live win count");
    if (!summary.includes("AVERAGE LIVE RETURN</span><strong>0.00%")) throw Error("live average contaminated");
    if (!summary.includes("EOD / PROVISIONAL RETURNS</span><strong>3</strong>")) throw Error("other cohort count");
    if (!body.includes("PROVISIONAL") || !body.includes("EOD")) throw Error("audit rows lost");
    """
    subprocess.run(["node", "-"], input=setup + js + check,
                   text=True, capture_output=True, check=True)
