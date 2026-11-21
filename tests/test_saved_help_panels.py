"""Every Jarvis tab remembers instructional panel visibility in the browser."""
import json
import re
import shutil
import subprocess

import pytest
import main


def _html_for_tab(tab):
    if tab == "home":
        return main.home_page().body.decode("utf-8")
    if tab == "history":
        return main.history_page().body.decode("utf-8")
    if tab == "intelligence":
        return main.research_page().body.decode("utf-8")
    if tab == "paper-trading":
        return main.paper_trading_page().body.decode("utf-8")
    if tab == "data-health":
        return main.data_health_page().body.decode("utf-8")
    if tab == "settings":
        return main.customize_page()
    return main.analytics_tab_page(tab)


def test_help_panels_cover_every_tab_independently():
    tabs = (
        "home", "history", "intelligence", "paper-trading", "data-health",
        "settings", "discovery-performance", "momentum-evidence",
        "market-breadth",
    )
    keys = set()
    for tab in tabs:
        html = _html_for_tab(tab)
        assert 'src="/assets/help_panels.js" defer' in html, tab
        marker = re.search(r'<details class="[^"]*jarvis-help" data-jarvis-help="([^"]+)" open', html)
        assert marker, tab
        key = marker.group(1)
        assert key not in keys, (tab, key)
        keys.add(key)
        assert "<summary>" in html, tab
    assert len(keys) == len(tabs)
    assert "home-instructions" in keys


def test_paper_rules_and_analytics_can_close_without_hiding_trades():
    html = _html_for_tab("paper-trading")
    assert 'data-jarvis-help="paper-trading-rules" open' in html
    for key in ("allocation", "strategy-performance", "edge-research", "extra-metrics"):
        assert f'data-paper-panel="{key}"' in html
    for live_table in ('paper-summary', 'positions', 'closed', 'ledger', 'strategyFilter'):
        assert f'id="{live_table}"' in html
    assert "PAPER ONLY · NO BROKER CONNECTION" in html
    assert "/paper-trading/data?refresh=" in html
    assert 'id="paperEdgeCohorts"' in html


def test_shared_js_is_served_and_does_not_modify_scanner():
    resp = main.help_panel_preferences_script()
    assert resp.status_code == 200
    assert resp.media_type == "application/javascript"
    body = resp.body.decode("utf-8")
    assert "jarvisHelpPanelsV1" in body
    assert 'details[data-jarvis-help]' in body
    assert "initJarvisHelpPanels" in body
    assert "fetch(" not in body
    assert "sendBeacon" not in body
    assert "paper_trading_state" not in body
    assert "save trade" not in body.lower()


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required for client-side state verification")
def test_independent_help_states_survive_reload_and_storage_failures():
    script = main.help_panel_preferences_script().body.decode("utf-8")
    js = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context={window:{},document:{readyState:'loading',addEventListener(){}}};
vm.createContext(context);
vm.runInContext(CODE,context);
const init=context.window.initJarvisHelpPanels;
assert.equal(typeof init,'function');
const state={};
const storage={
  getItem(key){return state[key]||null;},
  setItem(key,value){state[key]=value;}
};
function panel(id,open=true){
  return {dataset:{jarvisHelp:id},open,handlers:{},
    addEventListener(type,callback){this.handlers[type]=callback;},
    toggle(){this.open=!this.open;this.handlers.toggle();}
  };
}
function root(panels){
  return {querySelectorAll(selector){
    assert.equal(selector,'details[data-jarvis-help]');
    return panels;
  }};
}
const market=panel('market-overview-instructions');
const paper=panel('paper-trading-instructions');
init(root([market,paper]),storage);
assert.equal(market.open,true);
assert.equal(paper.open,true);
market.toggle();
assert.equal(JSON.parse(state.jarvisHelpPanelsV1)['market-overview-instructions'],false);
const newMarket=panel('market-overview-instructions');
const newPaper=panel('paper-trading-instructions');
init(root([newMarket,newPaper]),storage);
assert.equal(newMarket.open,false);
assert.equal(newPaper.open,true);
newPaper.toggle();
const last=panel('paper-trading-instructions');
init(root([last]),storage);
assert.equal(last.open,false);
const otherTab=panel('intelligence-instructions');
init(root([otherTab]),storage);
assert.equal(otherTab.open,true);
state.jarvisHelpPanelsV1='{corrupted';
const safe=panel('market-overview-instructions');
init(root([safe]),storage);
assert.equal(safe.open,true);
const blocked={getItem(){throw Error('browser blocks storage');},setItem(){throw Error('browser blocks storage');}};
const fallback=panel('data-health-instructions');
init(root([fallback]),blocked);
fallback.toggle();
assert.equal(fallback.open,false);
state.jarvisHelpPanelsV1=JSON.stringify({'settings-instructions':'false','data-health-instructions':true});
const invalid=panel('settings-instructions');
init(root([invalid]),storage);
assert.equal(invalid.open,true);
const verified=panel('data-health-instructions');
init(root([verified]),storage);
assert.equal(verified.open,true);
console.log('per-tab instruction preferences persisted');
""".replace("CODE",json.dumps(script))
    result = subprocess.run(["node","-e",js],capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    assert "per-tab instruction preferences persisted" in result.stdout
