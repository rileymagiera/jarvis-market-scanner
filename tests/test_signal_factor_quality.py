"""Regression tests: research-only factor comparison must not use future features."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from entry_timing_lab import build_entry_timing_lab, build_signal_factor_quality

BASE = datetime(2026, 11, 20, 15, tzinfo=timezone.utc)


def snap(minute, price, features=None, *, quote_minute=None, status="verified"):
    observed = BASE + timedelta(minutes=minute)
    quoted = BASE + timedelta(minutes=minute if quote_minute is None else quote_minute)
    return {
        "scan_id": f"scan-{minute}",
        "observed_at": observed.isoformat(),
        "price_as_of": quoted.isoformat(),
        "price": price,
        "price_verified": True,
        "price_validation_status": status,
        "outcome_eligible": True,
        "shadow_classification": "EARLY WATCH",
        "features": features if features is not None else {},
    }


def candidate(asset, symbol, positive):
    return {
        "asset_type": asset,
        "symbol": symbol,
        "signal_history": [
            snap(0, 10, {"higher_lows": positive, "relative_volume": 1.6 if positive else 1.0}),
            # Later feature values deliberately contradict the first signal;
            # a lookahead bug would invert the comparison.
            snap(60, 10, {"higher_lows": not positive, "relative_volume": 1.0 if positive else 1.6}),
            snap(120, 11 if positive else 9),
        ],
    }


def find_factor(report, name="higher_lows", asset="crypto", horizon=60):
    return next(row for row in report["cohorts"]
                if row["asset_type"] == asset and row["factor_at_first_signal"] == name
                and row["horizon_minutes"] == horizon)


def test_frozen_first_signal_only_hourly_next_quote_and_forward_returns():
    records = [candidate("crypto", "UP", True), candidate("crypto", "DOWN", False)]
    before = deepcopy(records)
    quality = build_entry_timing_lab(records)["signal_factor_quality"]
    assert records == before
    assert quality["mode"] == "shadow_only_no_trades"
    assert quality["first_signals_with_next_verified_scan"] == 2
    row = find_factor(quality)
    assert row["present_candidates"] == row["absent_candidates"] == 1
    assert row["median_gross_return_pct_present"] == 10.0
    assert row["median_gross_return_pct_absent"] == -10.0
    assert row["median_return_difference_pct_points"] is None
    assert row["review_status"] == "insufficient_distinct_candidates_do_not_rank"
    rvol = find_factor(quality, name="relative_volume_ge_1_5")
    assert rvol["present_candidates"] == 1 and rvol["absent_candidates"] == 1


def test_unknown_stale_and_duplicate_quote_cannot_create_synthetic_evidence():
    unknown = {"asset_type": "crypto", "symbol": "UNK", "signal_history": [
        snap(0, 10), snap(60, 10, {"higher_lows": True}), snap(120, 20), snap(180, 22),
    ]}
    stale = {"asset_type": "crypto", "symbol": "STALE", "signal_history": [
        snap(0, 10, {"higher_lows": True}, status="stale"),
        snap(60, 10, {"higher_lows": True}), snap(120, 20),
    ]}
    repeat = {"asset_type": "crypto", "symbol": "REPEAT", "signal_history": [
        snap(0, 10, {"higher_lows": True}),
        snap(60, 10, {"higher_lows": True}, quote_minute=0),
        snap(120, 20),
    ]}
    quality = build_signal_factor_quality([unknown, stale, repeat])
    row = find_factor(quality)
    assert row["present_candidates"] == 1  # STALE only, first eligible signal at 60
    assert row["absent_candidates"] == 0
    assert row["unknown_feature_candidates"] == 1  # UNK's first signal had no feature
    assert quality["first_signals_without_usable_next_scan"] == 1


def test_unique_candidates_separate_assets_and_require_both_sides_of_comparison():
    records = [candidate("crypto", f"C{i}", i < 30) for i in range(60)]
    records.append(deepcopy(records[0]))  # same symbol cannot inflate a win rate
    records.append(candidate("stock", "STOCK1", True))
    report = build_signal_factor_quality(records)
    crypto = find_factor(report)
    stock = find_factor(report, asset="stock")
    assert crypto["present_candidates"] == 30 and crypto["absent_candidates"] == 30
    assert crypto["median_return_difference_pct_points"] == 20.0
    assert crypto["review_status"] == "directional_only_not_proven_edge"
    assert stock["present_candidates"] == 1
    assert stock["review_status"] == "insufficient_distinct_candidates_do_not_rank"
    assert report["distinct_candidates_with_first_signal"] == 61
    assert report["followed_candidates_by_horizon"][60] == 61


def test_missing_horizon_is_not_counted_as_negative_outcome():
    record = {"asset_type": "crypto", "symbol": "TOO_EARLY", "signal_history": [
        snap(0, 10, {"higher_lows": True}), snap(60, 11),
    ]}
    report = build_signal_factor_quality([record])
    assert report["first_signals_with_next_verified_scan"] == 1
    assert report["followed_candidates_by_horizon"] == {60: 0, 240: 0}
    assert report["cohorts"] == []
