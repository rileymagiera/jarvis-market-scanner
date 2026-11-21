"""Paper Trading analysis panels remember the user's local display preference.

The shared paper account, its state, and the simulation engine are unchanged.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

import main


PANEL_IDS = ("extra-metrics", "allocation", "strategy-performance", "edge-research")


def test_paper_analytics_sections_are_collapsible_without_hiding_positions_or_ledger():
    html = main.paper_trading_page().body.decode("utf-8")
    for panel in PANEL_IDS:
        assert html.count(f'data-paper-panel="{panel}"') == 1
    assert re.search(
        r'<details class="paper-chart-panel section" data-paper-panel="allocation" open>'
        r'.*?id="allocation".*?</details>', html, flags=re.DOTALL)
    assert re.search(
        r'<details class="paper-chart-panel section" data-paper-panel="strategy-performance">'
        r'.*?id="strategyPerformance".*?</details>', html, flags=re.DOTALL)
    assert re.search(
        r'<details class="paper-chart-panel section" data-paper-panel="edge-research">'
        r'.*?id="paperEdgeCohorts".*?</details>', html, flags=re.DOTALL)
    assert 'id="paper-summary"' in html
    assert 'id="positions"' in html
    assert 'id="closed"' in html
    assert 'id="ledger"' in html
    assert 'id="strategyFilter"' in html
    assert 'id="paperEdgeStats"' in html
    assert 'id="metrics"' in html
    assert html.index('id="positions"') > html.index('</details>', html.index('data-paper-panel="edge-research"'))
    assert "paper-trading/data?refresh=" in html
    assert "function render(d)" in html


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js needed to validate browser storage behavior")
def test_panel_prefs_survive_reload_and_keep_sections_independent():
    page = Path("templates/paper_trading.html").read_text(encoding="utf-8")
    begin = page.index("// PAPER PANEL PERSISTENCE")
    end = page.index("// END PAPER PANEL PERSISTENCE", begin)
    code = page[begin:end]
    node_test = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const context = {};
vm.createContext(context);
vm.runInContext(CODE, context);
const initialize = context.initPaperPanelPersistence;
const saved = {};
const store = {
  getItem(key) { return saved[key] || null; },
  setItem(key,value) { saved[key] = value; }
};
function makePanel(key, open) {
  return {
    dataset: {paperPanel:key}, open, handlers: {},
    addEventListener(event,handler) { this.handlers[event] = handler; },
    toggle() { this.open = !this.open; this.handlers.toggle(); }
  };
}
function makePage() {
  const allocation=makePanel('allocation',true);
  const performance=makePanel('strategy-performance',false);
  const edge=makePanel('edge-research',false);
  const metrics=makePanel('extra-metrics',false);
  const panels=[allocation,performance,edge,metrics];
  const root={querySelectorAll(selector) {
    assert.equal(selector,'details[data-paper-panel]');
    return panels;
  }};
  return {root,allocation,performance,edge,metrics};
}
const first=makePage();
initialize(first.root,store);
assert.equal(first.allocation.open,true);
assert.equal(first.edge.open,false);
first.allocation.toggle();
first.edge.toggle();
first.metrics.toggle();
const written=JSON.parse(saved.jarvisPaperPanelsV1);
assert.deepEqual(written,{'allocation':false,'edge-research':true,'extra-metrics':true});
const reloaded=makePage();
initialize(reloaded.root,store);
assert.equal(reloaded.allocation.open,false);
assert.equal(reloaded.performance.open,false);
assert.equal(reloaded.edge.open,true);
assert.equal(reloaded.metrics.open,true);
reloaded.performance.toggle();
const reloadedTwice=makePage();
initialize(reloadedTwice.root,store);
assert.equal(reloadedTwice.performance.open,true);
assert.equal(reloadedTwice.edge.open,true);
assert.equal(reloadedTwice.allocation.open,false);
saved.jarvisPaperPanelsV1='{broken json';
initialize(makePage().root,store); // Corrupted preferences must not break the page.
saved.jarvisPaperPanelsV1=JSON.stringify({'allocation':'false','edge-research':true});
const malformed=makePage();
initialize(malformed.root,store);
assert.equal(malformed.allocation.open,true); // Non-boolean ignored.
assert.equal(malformed.edge.open,true);
const blocked={getItem(){throw Error('storage blocked');},setItem(){throw Error('storage blocked');}};
const fallback=makePage();
initialize(fallback.root,blocked);
fallback.allocation.toggle(); // Storage errors must not prevent local interactions.
assert.equal(fallback.allocation.open,false);
console.log('Paper Trading panel persistence validated');
""".replace("CODE", json.dumps(code))
    result = subprocess.run(
        ["node", "-e", node_test], capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "Paper Trading panel persistence validated" in result.stdout
