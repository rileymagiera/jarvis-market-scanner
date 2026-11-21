"""Verify card chart links target the candidate, without changing trading logic."""
from pathlib import Path
import subprocess


def test_candidate_chart_logos_and_urls():
    root = Path(__file__).resolve().parents[1]
    js_file = root / "static" / "history_dashboard.js"
    node_script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function candidateChartUrl(');
const end = source.indexOf('function priceTiming(', start);
assert.ok(start >= 0 && end > start, 'chart helpers exist');
const sandbox = {};
vm.runInNewContext("const e=v=>String(v??'');" + String.fromCharCode(10) + source.slice(start,end), sandbox);
const link = sandbox.candidateChartUrl;
assert.equal(link({asset_type:'crypto',symbol:'BTCUSDT'}), 'https://www.coinbase.com/advanced-trade/spot/BTC-USD');
assert.equal(link({asset_type:'crypto',symbol:'SOL'}), 'https://www.coinbase.com/advanced-trade/spot/SOL-USD');
assert.equal(link({asset_type:'crypto',symbol:'XBTUSD'}), 'https://www.coinbase.com/advanced-trade/spot/BTC-USD');
assert.equal(link({asset_type:'crypto',symbol:'BTCUSDC',coinbase_quote_currency:'USDC'}), 'https://www.coinbase.com/advanced-trade/spot/BTC-USDC');
assert.equal(link({asset_type:'stock',symbol:'AAPL',exchange:'NASDAQ'}), 'https://www.tradingview.com/symbols/NASDAQ-AAPL/');
assert.equal(link({asset_type:'stock',symbol:'LVWR'}), 'https://www.tradingview.com/symbols/LVWR/');
assert.equal(link({asset_type:'stock',symbol:'PYXS',primary_exchange:'NASDAQ'}), 'https://www.tradingview.com/symbols/NASDAQ-PYXS/');
assert.equal(link({asset_type:'stock',symbol:'<img onerror=x>'}), '');
assert.equal(link({asset_type:'other',symbol:'AAPL'}), '');
const coin = sandbox.candidateChartLogo({asset_type:'crypto',symbol:'BTCUSDT'});
const stock = sandbox.candidateChartLogo({asset_type:'stock',symbol:'LVWR'});
assert.match(coin, /class="op-chart-link coinbase"/);
assert.match(coin, /<svg/);
assert.match(coin, /BTC-USD/);
assert.match(stock, /class="op-chart-link tradingview"/);
assert.ok(stock.includes('/symbols/LVWR/'));
assert.match(stock, /target="_blank"/);
assert.match(stock, /rel="noopener noreferrer"/);
assert.equal(sandbox.candidateChartLogo({asset_type:'stock',symbol:'<invalid>'}), '');
"""
    run = subprocess.run(
        ["node", "-e", node_script, str(js_file)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_market_overview_has_chart_link_styling():
    html = (Path(__file__).resolve().parents[1] / "templates" / "history.html").read_text()
    js = (Path(__file__).resolve().parents[1] / "static" / "history_dashboard.js").read_text()
    assert ".op-card-actions .op-chart-link" in html
    assert "candidateChartLogo(x)" in js
    assert 'data-card-action="analysis"' in js
    assert 'data-card-action="targets"' in js
