"""Setup Quality measures official BUY gate completion, never technical hype.

All tests are read-only. The scanner's BUY rules and paper positions are not
modified by the dashboard projection or its client-side rating.
"""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import main


def test_market_overview_exposes_only_bounded_stock_readiness(monkeypatch):
    checks = {
        "score_82": True,
        "volume_confirmed": False,
        "breakout_confirmed": True,
        "two_technical_confirmations": True,
        "market_not_risk_off": False,
        "fresh_intraday_confirmation": True,
        "five_bar_intraday_trend_up": True,
        "no_risk_flags": True,
        "not_chase_risk": True,
        "price_verified": True,
        "live_data_quality": True,
    }
    stock = {
        "asset_type": "stock", "symbol": "AAA", "last_stage": "ACTIONABLE SETUP",
        "price_validation_status": "verified", "currently_surfaced": True,
        "data_quality": {"data_type": "live", "buy_eligible": True},
        "score_components": {"momentum": 25, "volume": 25, "breakout": 20,
                             "technical_confirmation": 15, "trend": 15},
        "buy_readiness": {
            "checks": checks, "passed": 9, "total": 11,
            "all_gates_passed": False, "missing_conditions": ["secret-detail-" + "z" * 2500],
        },
        "buy_gate_history": [{"key": "unneeded"}],
        "signal_history": [{"key": "unneeded"}],
    }
    crypto = {"asset_type": "crypto", "symbol": "BBB",
              "buy_readiness": {"checks": {"score_82": False}}}
    original = copy.deepcopy({"stock:AAA": stock, "crypto:BBB": crypto})
    monkeypatch.setattr(main, "history_data", lambda: {
        "paper_tracker": {"stock:AAA": stock, "crypto:BBB": crypto},
        "lifetime": {}, "buy_signals": [],
        "scan_run_id": "r1", "last_scan_completed_at": "2026-11-21T12:00:00+00:00",
        "history_source": "test",
    })
    report = json.loads(main.market_overview_data().body)
    out = report["paper_tracker"]["stock:AAA"]
    assert out["buy_readiness"] == {
        "passed": 9, "total": 11, "all_gates_passed": False,
        "failed_gate_keys": ["volume_confirmed", "market_not_risk_off"],
    }
    assert report["paper_tracker"]["crypto:BBB"] == {"asset_type": "crypto", "symbol": "BBB"}
    assert len(json.dumps(out["buy_readiness"])) < 300
    assert "secret-detail" not in json.dumps(report)
    assert "signal_history" not in out
    assert "buy_gate_history" not in out
    assert {"stock:AAA": stock, "crypto:BBB": crypto} == original
    assert report["buy_signals"] == []


def test_missing_and_malformed_saved_gates_cannot_create_quality_scores():
    assert "buy_readiness" not in main.market_overview_candidate({"asset_type": "stock", "symbol": "AAA"})
    assert "buy_readiness" not in main.market_overview_candidate(
        {"asset_type": "stock", "buy_readiness": {"all_gates_passed": True, "checks": {}}})
    assert "buy_readiness" not in main.market_overview_candidate(
        {"asset_type": "stock", "buy_readiness": {"checks": None}})


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js required for pure browser quality tests")
def test_stock_quality_never_promises_a_buy_without_verified_active_fresh_gates():
    source = Path("static/history_dashboard.js").read_text(encoding="utf-8")
    begin = source.index("// SETUP QUALITY READINESS HELPER")
    end = source.index("// END SETUP QUALITY READINESS HELPER", begin)
    helper = source[begin:end]
    script = """
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = {};
vm.createContext(context);
vm.runInContext(SOURCE, context);
const read = context.setupQualityReadiness;
const candidate = {
  asset_type: 'stock', symbol: 'AAA', last_stage: 'BUY',
  price_validation_status: 'verified', data_quality: {data_type: 'live', buy_eligible: true},
  score_components: {momentum:25,volume:25,breakout:20,trend:15,technical_confirmation:15},
  buy_readiness: {passed: 11, total: 11, all_gates_passed:true, failed_gate_keys:[]}
};
const signals = [{symbol:'AAA',asset_type:'stock',status:'active'}];
const yes = read(candidate,signals,3);
assert.equal(yes.score,100);
assert.equal(yes.isBuy,true);
assert.match(yes.label,/BUY SIGNAL/);
function blocked(changes, signalList=signals, age=3) {
  const value = read({...candidate,...changes},signalList,age);
  assert.equal(value.isBuy,false);
  assert.ok(value.score===null || value.score<100);
  assert.match(value.label,/WAIT|UNAVAILABLE/);
  return value;
}
const missing = blocked({buy_readiness: {...candidate.buy_readiness,
  passed:10,all_gates_passed:false,failed_gate_keys:['market_not_risk_off']}});
assert.match(missing.detail,/Risk-Off/);
blocked({price_validation_status:'last_known'});
blocked({data_quality:{data_type:'delayed',buy_eligible:false}});
blocked({last_stage:'ACTIONABLE SETUP'});
blocked({},[],3);
blocked({},signals,21);
blocked({buy_readiness: undefined});
const inactive = blocked({}, [{symbol:'AAA',asset_type:'stock',status:'closed'}]);
assert.match(inactive.label,/NO ACTIVE BUY/);
const foreign = blocked({}, [{symbol:'AAA',asset_type:'crypto',status:'active'}]);
assert.equal(foreign.isBuy,false);
const noSignal = read({...candidate, buy_readiness: undefined},signals,3);
assert.equal(noSignal.score,null);
console.log('All BUY-readiness quality cases passed');
""".replace("SOURCE", json.dumps(helper))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "All BUY-readiness quality cases passed" in result.stdout


def test_quality_card_preserves_technical_evidence_and_crypto_lane():
    js = Path("static/history_dashboard.js").read_text(encoding="utf-8")
    html = Path("templates/history.html").read_text(encoding="utf-8")
    assert "SETUP QUALITY · BUY READINESS" in js
    assert "TECHNICAL EVIDENCE · " in js
    assert "CRYPTO RUNNER QUALITY" in js
    assert "setupQualityReadiness(x,activeBuys,scanAgeMinutes)" in js
    assert 'data-setup-status=' in js
    assert 'data-setup-status="buy"' in html
    assert "paper-trading" in html.lower()
    assert "BUY SIGNAL · ENTRY REVIEW" in js
