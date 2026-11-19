import copy
import unittest

from signal_lead_time_audit import build_signal_lead_time_audit


class SignalLeadTimeAuditTests(unittest.TestCase):
    def snap(self, minute, price, label="EARLY ACCUMULATION", *, verified=True, eligible=True,
             scan_id=None, price_as_of=True, evidence=None):
        at = f"2026-11-19T15:{minute:02d}:00+00:00"
        return {
            "scan_id": scan_id or f"run-{minute}", "observed_at": at,
            "price_as_of": at if price_as_of else None,
            "price": price, "outcome_eligible": eligible,
            "price_verified": verified, "shadow_classification": label,
            "evidence": evidence or ["Accumulation evidence developing"],
        }

    def record(self, asset, symbol, history):
        return {"asset_type": asset, "symbol": symbol, "signal_history": history}

    def test_first_observed_hit_not_same_scan_and_lead_minutes(self):
        rec = self.record("crypto", "ABCUSDT", [
            self.snap(0, 10), self.snap(15, 10.25), self.snap(30, 11),
        ])
        original = copy.deepcopy(rec)
        report = build_signal_lead_time_audit([rec])
        self.assertEqual(rec, original)
        result = next(c for c in report["cohorts"] if c["indicator_type"] == "classification"
                      and c["threshold_pct"] == 5)
        self.assertEqual(result["signals_with_followup"], 1)
        self.assertEqual(result["later_observed_crossings"], 1)
        self.assertEqual(result["median_lead_minutes"], 30)
        self.assertEqual(report["candidates_with_followup"], 1)

    def test_late_signal_is_not_credited_for_prior_crossing(self):
        report = build_signal_lead_time_audit([self.record("crypto", "LATEUSDT", [
            self.snap(0, 10, "EARLY WATCH"),
            self.snap(15, 11, "BREAKOUT DEVELOPING"),
            self.snap(30, 10.8, "BREAKOUT DEVELOPING"),
        ])])
        late = next(c for c in report["cohorts"] if c["signal"] == "BREAKOUT DEVELOPING"
                    and c["threshold_pct"] == 5)
        self.assertEqual(late["already_crossed_at_first_signal"], 1)
        self.assertEqual(late["later_observed_crossings"], 0)

    def test_excludes_unverified_unknown_timestamp_and_stale_price(self):
        good = self.snap(0, 10)
        stale = self.snap(15, 11)
        stale["price_as_of"] = "2026-11-19T14:00:00+00:00"
        no_timezone = self.snap(20, 12)
        no_timezone["price_as_of"] = "2026-11-19T15:20:00"
        report = build_signal_lead_time_audit([self.record("stock", "AAA", [
            good, stale, no_timezone,
            self.snap(30, 12, verified=False),
            self.snap(45, 12, eligible=False),
        ])])
        self.assertEqual(report["eligible_snapshots"], 1)
        self.assertEqual(report["excluded_snapshots"], 4)
        self.assertEqual(report["cohorts"], [])

    def test_deduplicates_scans_and_does_not_include_no_followup(self):
        record = self.record("crypto", "AAA", [
            self.snap(0, 10, scan_id="same"),
            self.snap(15, 11, scan_id="same"),
            self.snap(30, 12, "PRE-BREAKOUT WATCH"),
        ])
        r = build_signal_lead_time_audit([record])
        self.assertEqual(r["excluded_snapshots"], 1)
        self.assertEqual(r["eligible_snapshots"], 2)
        later = next(c for c in r["cohorts"] if c["signal"] == "PRE-BREAKOUT WATCH"
                     and c["threshold_pct"] == 5)
        self.assertEqual(later["signals_with_followup"], 0)
        self.assertEqual(later["later_observed_crossings"], 0)

    def test_stock_crypto_cohorts_remain_separate(self):
        records = [self.record(asset, symbol, [self.snap(0, 10), self.snap(15, 11)])
                   for asset, symbol in [("stock", "S"), ("crypto", "C")]]
        r = build_signal_lead_time_audit(records)
        hits = [c for c in r["cohorts"] if c["indicator_type"] == "classification"
                and c["threshold_pct"] == 10]
        self.assertEqual({c["asset_type"] for c in hits}, {"stock", "crypto"})

    def test_missing_history_safely_ignored(self):
        r = build_signal_lead_time_audit([{}, {"asset_type": "stock", "signal_history": None}])
        self.assertEqual(r["eligible_candidates"], 0)
        self.assertEqual(r["cohorts"], [])


if __name__ == "__main__":
    unittest.main()
