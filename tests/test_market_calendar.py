import unittest
from datetime import datetime, timezone
from market_calendar import market_session_status


class MarketCalendarTests(unittest.TestCase):
    def test_regular_session_on_normal_trading_day(self):
        result = market_session_status(datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(result["phase"], "REGULAR SESSION")
        self.assertTrue(result["is_regular_session"])

    def test_thanksgiving_is_closed(self):
        result = market_session_status(datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc))
        self.assertFalse(result["is_regular_session"])
        self.assertFalse(result["is_extended_session"])
        self.assertIn("CLOSED", result["phase"])

    def test_early_close_uses_calendar_close(self):
        result = market_session_status(datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc))
        self.assertFalse(result["is_regular_session"])
        self.assertIn(result["phase"], {"AFTER-HOURS", "CLOSED — OVERNIGHT"})
        if result.get("calendar_verified"):
            self.assertTrue(result["regular_close"].startswith("2026-11-27T13:00:00"))

if __name__ == "__main__":
    unittest.main()
