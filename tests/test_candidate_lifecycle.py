import unittest
from datetime import datetime, timezone, timedelta
from candidate_lifecycle import update_candidate_lifecycle


class CandidateLifecycleTests(unittest.TestCase):
    def test_records_failed_gates_only_for_promising_candidate(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = {}
        update_candidate_lifecycle(record, {
            "stage": "EARLY SETUP", "score": 67, "latest_price": 10,
            "price_as_of": now.isoformat(), "buy_readiness": {
                "passed": 2, "total": 4, "percent": 50,
                "checks": {"volume": True, "breakout": False, "market": False},
                "missing_conditions": ["No breakout", "Risk off"],
            },
        }, now)
        self.assertEqual(record["buy_gate_history"][0]["failed_gate_keys"], ["breakout", "market"])
        self.assertEqual(record["buy_gate_history"][0]["price"], 10)

    def test_only_later_verified_observations_update_gate_outcome(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = {}
        item = {"stage": "EARLY SETUP", "score": 65, "latest_price": 10,
                "price_as_of": start.isoformat(), "buy_readiness": {
                    "percent": 50, "checks": {"volume": False}}}
        update_candidate_lifecycle(record, item, start)
        later = start + timedelta(hours=1)
        update_candidate_lifecycle(record, {
            "stage": "EARLY SETUP", "latest_price": 12,
            "price_as_of": later.isoformat(),
        }, later, observation_eligible=True)
        self.assertEqual(record["buy_gate_history"][0]["max_favorable_since_pct"], 20.0)
        self.assertEqual(record["buy_gate_history"][0]["follow_up_observations"], 1)

    def test_catalyst_lifecycle_records_changes_and_clearing(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = {}
        update_candidate_lifecycle(record, {"catalyst_labels": ["FDA update"],
            "dilution_flags": ["ATM"]}, now)
        later = now + timedelta(hours=1)
        update_candidate_lifecycle(record, {"catalyst_labels": [], "dilution_flags": []}, later)
        self.assertEqual(len(record["catalyst_dilution_history"]), 2)
        self.assertEqual(record["catalyst_lifecycle_status"], "NO CURRENT CLASSIFIED CATALYST")


if __name__ == "__main__":
    unittest.main()
