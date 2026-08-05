import tempfile
import unittest
from pathlib import Path

import falcon_alerts
from falcon_alerts import AlertConfig, FalconAlertEngine


class FalconAlertsTests(unittest.TestCase):
    def test_buy_now_alert_message_contains_required_fields(self):
        config = AlertConfig(enabled=True, dry_run=True, min_score=90)
        engine = FalconAlertEngine(config)

        token = {
            "token_name": "Falcon Alpha",
            "token_symbol": "FALC",
            "contract_address": "So11111111111111111111111111111111111111112",
            "score": 92,
            "confidence": 81,
            "confidence_tier": "HIGH",
            "market_cap_usd": 345000,
            "liquidity_usd": 98000,
            "first_seen_at": "2026-08-04T10:20:30+00:00",
            "first_seen_ago": "2m ago",
            "score_breakdown": {
                "pump": 24,
                "telegram": 10,
                "x": 18,
                "dex": 12,
                "smart": 7,
            },
            "dexscreener_url": "https://dexscreener.com/solana/test",
            "signal_reasons": ["Falcon Score 92 >= 90"],
        }

        message = engine._compose_alert_message(token, ["Falcon Score 92 >= 90"])

        self.assertIn("FALCON BUY NOW ALERT", message)
        self.assertIn("Token: Falcon Alpha (FALC)", message)
        self.assertIn("Contract: So11111111111111111111111111111111111111112", message)
        self.assertIn("Falcon Rating: 92/100", message)
        self.assertIn("Confidence: HIGH (81)", message)
        self.assertIn("Market Cap:", message)
        self.assertIn("Liquidity:", message)
        self.assertIn("First Seen: 2026-08-04 10:20:30 UTC (2m ago)", message)
        self.assertIn("Triggered Sources: Pump.fun, Telegram, X, Dex, Smart", message)

    def test_process_scan_sends_once_per_contract_and_only_score_gate_applies(self):
        config = AlertConfig(
            enabled=True,
            dry_run=True,
            one_time_per_contract=True,
            min_score=90,
        )
        engine = FalconAlertEngine(config)

        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp)
            original_memory_dir = falcon_alerts.MEMORY_DIR
            original_state_file = falcon_alerts.ALERT_STATE_FILE
            falcon_alerts.MEMORY_DIR = memory_dir
            falcon_alerts.ALERT_STATE_FILE = memory_dir / "alert_state.json"

            try:
                token = {
                    "token_name": "Falcon Beta",
                    "token_symbol": "FB",
                    "contract_address": "So11111111111111111111111111111111111111113",
                    "score": 90,
                    "confidence": 10,
                    "confidence_tier": "LOW",
                    "market_cap_usd": 1000,
                    "liquidity_usd": 100,
                    "momentum": "BEARISH",
                    "risk_label": "HIGH",
                    "first_seen_at": "2026-08-04T10:00:00+00:00",
                    "first_seen_ago": "1h ago",
                    "score_breakdown": {"pump": 30, "telegram": 0, "x": 0, "dex": 15, "smart": 0},
                }

                report_1 = engine.process_scan([token], "2026-08-04T11:00:00+00:00")
                self.assertEqual(report_1.eligible, 1)
                self.assertEqual(report_1.sent, 1)
                self.assertEqual(report_1.suppressed_by_contract, 0)

                report_2 = engine.process_scan([token], "2026-08-04T11:05:00+00:00")
                self.assertEqual(report_2.eligible, 1)
                self.assertEqual(report_2.sent, 0)
                self.assertEqual(report_2.suppressed_by_contract, 1)
            finally:
                falcon_alerts.MEMORY_DIR = original_memory_dir
                falcon_alerts.ALERT_STATE_FILE = original_state_file


if __name__ == "__main__":
    unittest.main()
