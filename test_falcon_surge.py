import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from falcon_surge import FalconSurgeEngine, SurgeConfig, evaluate_surge


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _base_token(**overrides):
    token = {
        "token_symbol": "TEST",
        "contract_address": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
        "market_cap_usd": 250_000,
        "liquidity_usd": 30_000,
        "volume_5m_usd": 20_000,
        "buys_5m": 30,
        "sells_5m": 15,
        "pair_age_minutes": 20,
        "source_confirmation_count": 1,
        "source_confirmation_names": ["GMGN"],
    }
    token.update(overrides)
    return token


class FalconSurgeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SurgeConfig(
            enabled=True,
            alerts_enabled=True,
            alert_dry_run=True,
            alert_cooldown_minutes=20,
            alert_reset_minutes=30,
        )

    def test_watch_detection(self):
        prev = _base_token(market_cap_usd=200_000, volume_5m_usd=10_000)
        cur = _base_token(market_cap_usd=235_000, volume_5m_usd=11_000, buys_5m=22, sells_5m=12)
        surge = evaluate_surge(cur, prev, self.cfg)
        self.assertEqual(surge["surge_level"], "WATCH")

    def test_surge_detection(self):
        prev = _base_token(market_cap_usd=200_000, volume_5m_usd=10_000)
        cur = _base_token(market_cap_usd=260_000, volume_5m_usd=15_000, buys_5m=24, sells_5m=12)
        surge = evaluate_surge(cur, prev, self.cfg)
        self.assertEqual(surge["surge_level"], "SURGE")

    def test_breakout_detection(self):
        prev = _base_token(market_cap_usd=300_000, volume_5m_usd=10_000)
        cur = _base_token(market_cap_usd=510_000, liquidity_usd=40_000, volume_5m_usd=22_000, buys_5m=30, sells_5m=15)
        surge = evaluate_surge(cur, prev, self.cfg)
        self.assertEqual(surge["surge_level"], "BREAKOUT")
        self.assertGreaterEqual(surge["surge_rating"], 70)

    def test_insufficient_liquidity(self):
        prev = _base_token(market_cap_usd=200_000, volume_5m_usd=10_000)
        cur = _base_token(market_cap_usd=260_000, liquidity_usd=15_000, volume_5m_usd=18_000, buys_5m=30, sells_5m=10)
        surge = evaluate_surge(cur, prev, self.cfg)
        self.assertEqual(surge["surge_level"], "NONE")

    def test_negative_buy_pressure(self):
        prev = _base_token(market_cap_usd=200_000, volume_5m_usd=10_000)
        cur = _base_token(market_cap_usd=260_000, volume_5m_usd=18_000, buys_5m=9, sells_5m=20)
        surge = evaluate_surge(cur, prev, self.cfg)
        self.assertEqual(surge["surge_level"], "NONE")

    def test_duplicate_alert_suppression(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "surge_state.json"
            engine = FalconSurgeEngine(self.cfg, state_file=state_file)
            now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)

            prev = _base_token(market_cap_usd=200_000, volume_5m_usd=10_000)
            cur = _base_token(market_cap_usd=270_000, volume_5m_usd=16_000, buys_5m=26, sells_5m=10)
            cur.update(evaluate_surge(cur, prev, self.cfg))

            first = engine.process_scan([cur], _iso(now))
            second = engine.process_scan([cur], _iso(now + timedelta(minutes=2)))

            self.assertEqual(first.sent, 1)
            self.assertEqual(second.sent, 0)
            self.assertEqual(second.suppressed_duplicate, 1)

    def test_progression_from_surge_to_breakout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "surge_state.json"
            engine = FalconSurgeEngine(self.cfg, state_file=state_file)
            now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)

            prev = _base_token(market_cap_usd=220_000, volume_5m_usd=10_000)

            surge_token = _base_token(market_cap_usd=285_000, volume_5m_usd=16_000, buys_5m=24, sells_5m=12)
            surge_token.update(evaluate_surge(surge_token, prev, self.cfg))

            breakout_token = _base_token(market_cap_usd=520_000, liquidity_usd=45_000, volume_5m_usd=24_000, buys_5m=34, sells_5m=15)
            breakout_token.update(evaluate_surge(breakout_token, prev, self.cfg))

            first = engine.process_scan([surge_token], _iso(now))
            second = engine.process_scan([breakout_token], _iso(now + timedelta(minutes=3)))

            self.assertEqual(surge_token["surge_level"], "SURGE")
            self.assertEqual(breakout_token["surge_level"], "BREAKOUT")
            self.assertEqual(first.sent, 1)
            self.assertEqual(second.sent, 1)
            self.assertEqual(second.suppressed_cooldown, 0)


if __name__ == "__main__":
    unittest.main()
