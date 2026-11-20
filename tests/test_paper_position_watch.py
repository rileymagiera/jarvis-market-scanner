from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from historical_audit_export import build_historical_audit_export
from paper_position_watch import collect_paper_position_watch


NOW = datetime(2026, 11, 20, 16, 15, 20, tzinfo=timezone.utc)
WINDOW_END = NOW.replace(second=0)
POSITION = {"asset_type": "crypto", "symbol": "EVAAUSDT", "stop_loss": 9,
            "target_1": 12, "target_2": 15,
            "opened_at": (NOW - timedelta(minutes=4)).isoformat()}


def test_watches_holding_without_candidate_and_keeps_crossing_order_unknown():
    calls = []

    def kraken_get(url, *, params, timeout):
        calls.append((url, params, timeout))
        def bar(minutes, high, low, close):
            return [int((WINDOW_END - timedelta(minutes=minutes)).timestamp()),
                    10, high, low, close, 0, 0, 0]
        payload = {"result": {"EVAAUSD": [bar(4, 20, 1, 10),
                                           bar(3, 13, 8, 10),
                                           bar(1, 11, 9.5, 10),
                                           bar(0, 20, 1, 10)]}}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    scanner = SimpleNamespace(crypto_base=lambda symbol: "EVAA", KRAKEN="https://example.test",
                              kraken_pairs=lambda: {"EVAA": "EVAAUSD"},
                              KRAKEN_PAIR_QUOTE_MAP={"EVAA": "USD"}, kraken_get=kraken_get)
    position = deepcopy(POSITION)
    report = collect_paper_position_watch(scanner, [position], [], NOW, (NOW - timedelta(minutes=10)).isoformat())

    assert position == POSITION
    assert len(calls) == 1
    assert calls[0][1]["interval"] == 1
    assert report["positions_outside_candidate_list"] == 1
    assert report["positions_with_minute_bars"] == 1
    row, = report["positions"]
    assert row["status"] == "observed"
    assert row["quote_currency"] == "USD"
    assert row["completed_minutes_requested"] == 3
    assert row["minutes_with_bars"] == 2
    assert row["quote"] is None
    first, second = row["bars"]
    assert first["stop_and_target_same_bar"]
    assert first["target_1_touched"] and not first["target_2_touched"]
    assert not second["stop_touched"]
    assert build_historical_audit_export([], paper_position_watch=report)["paper_position_watch"] == report


def test_stale_quote_and_provider_failure_do_not_lose_holding_coverage():
    def unavailable(*args, **kwargs):
        raise TimeoutError
    scanner = SimpleNamespace(crypto_base=lambda symbol: "EVAA", kraken_pairs=unavailable)
    candidate = {"asset_type": "crypto", "symbol": "EVAAUSDT", "price": 10,
                 "price_validation_passed": True, "price_as_of": (NOW - timedelta(minutes=6)).isoformat()}
    report = collect_paper_position_watch(scanner, [POSITION], [candidate], NOW, None)
    row, = report["positions"]
    assert report["window_clipped"]
    assert row["in_market_scan"]
    assert row["quote"] is None
    assert row["status"] == "provider_unavailable"
    assert row["bars"] == []


def test_empty_watchlist_needs_no_provider():
    report = collect_paper_position_watch(None, [], [], NOW, None)
    assert report["positions_watched"] == 0
    assert report["positions"] == []
