import os
import unittest
from datetime import datetime, timezone

from whale_scanner import scan_whale_wallets


class WhaleScannerTests(unittest.TestCase):
    def setUp(self):
        self.original_env = {
            "HELIUS_API_KEY": os.environ.get("HELIUS_API_KEY"),
            "WHALE_WALLETS": os.environ.get("WHALE_WALLETS"),
            "HELIUS_TX_ENDPOINT": os.environ.get("HELIUS_TX_ENDPOINT"),
            "WHALE_TX_LIMIT": os.environ.get("WHALE_TX_LIMIT"),
            "WHALE_TX_LOOKBACK_MINUTES": os.environ.get("WHALE_TX_LOOKBACK_MINUTES"),
        }

    def tearDown(self):
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_not_configured_without_api_key_or_wallets(self):
        os.environ.pop("HELIUS_API_KEY", None)
        os.environ.pop("WHALE_WALLETS", None)

        signals, details = scan_whale_wallets(limit_wallets=10, request_json_fn=lambda *args, **kwargs: [])

        self.assertEqual(signals, {})
        self.assertFalse(details.get("configured"))
        self.assertTrue(details.get("success"))
        self.assertEqual(details.get("error_message"), "Whale scanner not configured")

    def test_detects_swap_buys_and_ignores_plain_transfers(self):
        os.environ["HELIUS_API_KEY"] = "test-key"
        os.environ["WHALE_WALLETS"] = "So11111111111111111111111111111111111111112"

        wallet = "So11111111111111111111111111111111111111112"

        def fake_request_json(url, params=None, timeout=12):
            return [
                {
                    "signature": "sig-swap",
                    "timestamp": int(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc).timestamp()),
                    "type": "SWAP",
                    "tokenTransfers": [
                        {
                            "mint": "To11111111111111111111111111111111111111111",
                            "fromUserAccount": "seller_wallet",
                            "toUserAccount": wallet,
                            "tokenAmount": 150.0,
                        }
                    ],
                },
                {
                    "signature": "sig-transfer",
                    "timestamp": int(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc).timestamp()),
                    "type": "TRANSFER",
                    "tokenTransfers": [
                        {
                            "mint": "Tr11111111111111111111111111111111111111111",
                            "fromUserAccount": "friend_wallet",
                            "toUserAccount": wallet,
                            "tokenAmount": 50.0,
                        }
                    ],
                },
            ]

        signals, details = scan_whale_wallets(
            limit_wallets=5,
            request_json_fn=fake_request_json,
            now_dt=datetime(2026, 8, 4, 12, 5, tzinfo=timezone.utc),
        )

        self.assertTrue(details.get("configured"))
        self.assertIn("To11111111111111111111111111111111111111111", signals)
        self.assertNotIn("Tr11111111111111111111111111111111111111111", signals)

        signal = signals["To11111111111111111111111111111111111111111"]
        self.assertEqual(signal.get("buy_count"), 1)
        self.assertEqual(signal.get("wallets"), [wallet])

    def test_dedupes_repeated_evidence_ids(self):
        os.environ["HELIUS_API_KEY"] = "test-key"
        os.environ["WHALE_WALLETS"] = "So11111111111111111111111111111111111111112"

        wallet = "So11111111111111111111111111111111111111112"

        duplicate_tx = {
            "signature": "sig-dup",
            "timestamp": int(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc).timestamp()),
            "type": "SWAP",
            "tokenTransfers": [
                {
                    "mint": "Du11111111111111111111111111111111111111111",
                    "fromUserAccount": "seller_wallet",
                    "toUserAccount": wallet,
                    "tokenAmount": 75.0,
                }
            ],
        }

        def fake_request_json(url, params=None, timeout=12):
            return [duplicate_tx, duplicate_tx]

        signals, _ = scan_whale_wallets(
            limit_wallets=5,
            request_json_fn=fake_request_json,
            now_dt=datetime(2026, 8, 4, 12, 5, tzinfo=timezone.utc),
        )

        signal = signals["Du11111111111111111111111111111111111111111"]
        self.assertEqual(signal.get("buy_count"), 1)
        self.assertEqual(len(signal.get("evidence", [])), 1)


if __name__ == "__main__":
    unittest.main()
