from scripts.historical_opportunity_audit import analyze, feature_match, outcome_summary


def test_missing_features_are_unknown_not_false():
    assert feature_match({}, "stock_volume_rvol_1_5x") is None
    assert feature_match({"hourly_change_pct": None}, "crypto_hourly_momentum_positive") is None


def test_stock_cohort_requires_session_aware_tracking():
    tracker = {
        "stock:OLD": {
            "asset_type": "stock",
            "early_runner_snapshot_at": "2026-01-01T10:00:00-05:00",
            "early_runner_initial_feature_snapshot": {"cumulative_rvol_same_time": 2.0},
            "early_runner_outcomes": {"5d": {"return_pct": 25}},
            "session_aware_tracking": False,
        },
        "stock:NEW": {
            "asset_type": "stock",
            "early_runner_snapshot_at": "2026-01-01T10:00:00-05:00",
            "early_runner_initial_feature_snapshot": {"cumulative_rvol_same_time": 2.0},
            "early_runner_outcomes": {"5d": {"return_pct": 25}},
            "session_aware_tracking": True,
            "research_max_favorable_pct": 30,
            "research_max_adverse_pct": -4,
        },
    }
    report = analyze(tracker, {}, [])
    assert report["eligible_snapshot_count"] == 1
    assert report["assets"]["stock"]["completed_5d_count"] == 1


def test_outcome_summary_calculates_runner_rates():
    rows = [
        {"outcome": {"return_pct": 25}, "record": {}},
        {"outcome": {"return_pct": 12}, "record": {}},
        {"outcome": {"return_pct": -3}, "record": {}},
    ]
    result = outcome_summary(rows)
    assert result["observations"] == 3
    assert result["runner_20pct_rate_pct"] == 33.3
    assert result["runner_10pct_rate_pct"] == 66.7


def test_discovery_analysis_identifies_runner_without_buy_stage():
    from scripts.historical_opportunity_audit import discovery_analysis

    records = [{
        "asset_type": "crypto",
        "symbol": "TEST",
        "initial_price": 1.0,
        "discovery_return_status": "verified",
        "max_gain_pct": 25.0,
        "max_drawdown_pct": -6.0,
        "first_seen": "2026-01-01T00:00:00+00:00",
        "first_grade": "EARLY WATCH",
        "last_stage": "DEVELOPING SETUP",
        "grade_history": [
            {"from": None, "to": "EARLY WATCH", "at": "2026-01-01T00:00:00+00:00"},
            {"from": "EARLY WATCH", "to": "DEVELOPING SETUP", "at": "2026-01-01T01:00:00+00:00"},
        ],
        "price_history": [
            {"at": "2026-01-01T00:00:00+00:00", "price": 1.0, "return_pct": 0},
            {"at": "2026-01-01T02:00:00+00:00", "price": 1.1, "return_pct": 10},
        ],
    }]
    result = discovery_analysis(records, "crypto")
    assert result["runner_20pct_count"] == 1
    assert len(result["potential_missed_20pct_runners"]) == 1
    assert result["potential_missed_20pct_runners"][0]["first_10pct_crossing"]["stage_at_cross"] == "DEVELOPING SETUP"
