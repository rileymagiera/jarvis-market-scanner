import unittest
from datetime import datetime, timezone, timedelta

from main import premarket_provider_audit


class PremarketProviderAuditTests(unittest.TestCase):
    def test_recent_yahoo_bar_is_not_promoted_to_live(self):
        now = datetime(2026, 11, 19, 13, 0, tzinfo=timezone.utc)
        result = premarket_provider_audit({
            "intraday_price": 12.34,
            "intraday_as_of": (now - timedelta(seconds=60)).isoformat(),
        }, checked_at=now)
        self.assertTrue(result["yahoo"]["status"].startswith("RECENT"))
        self.assertFalse(result["yahoo"]["counts_as_live"])

    def test_fresh_iex_quote_is_not_called_consolidated(self):
        now = datetime(2026, 11, 19, 13, 0, tzinfo=timezone.utc)
        result = premarket_provider_audit({
            "alpaca_price": 12.35,
            "alpaca_price_age_minutes": 0.5,
            "alpaca_price_as_of": (now - timedelta(seconds=30)).isoformat(),
            "alpaca_feed": "iex",
        }, checked_at=now)
        self.assertEqual(result["alpaca"]["status"], "FRESH IEX/SIP QUOTE")
        self.assertFalse(result["alpaca"]["counts_as_consolidated_live"])

    def test_fresh_sip_quote_is_marked_consolidated(self):
        now = datetime(2026, 11, 19, 13, 0, tzinfo=timezone.utc)
        result = premarket_provider_audit({
            "alpaca_price": 12.35,
            "alpaca_price_age_minutes": 0.5,
            "alpaca_feed": "sip",
        }, checked_at=now)
        self.assertTrue(result["alpaca"]["counts_as_consolidated_live"])

    def test_stale_and_missing_quotes_are_unavailable(self):
        now = datetime(2026, 11, 19, 13, 0, tzinfo=timezone.utc)
        result = premarket_provider_audit({
            "intraday_price": 12.34,
            "intraday_as_of": (now - timedelta(minutes=20)).isoformat(),
            "alpaca_price": 12.35,
            "alpaca_price_age_minutes": 20,
            "alpaca_feed": "iex",
        }, checked_at=now)
        self.assertEqual(result["yahoo"]["status"], "STALE / UNAVAILABLE")
        self.assertEqual(result["alpaca"]["status"], "STALE / UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
