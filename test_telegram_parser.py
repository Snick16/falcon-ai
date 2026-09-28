import unittest
from unittest.mock import patch

from source_scanner import parse_telegram_messages, scan_telegram_channels


class TelegramParserTests(unittest.TestCase):
    def test_ca_extraction_with_labels_and_symbol(self):
        rows = [
            {
                "channel": "SoapsGems",
                "message_id": 101,
                "message_timestamp": "2026-08-04T17:00:00+00:00",
                "message_url": "https://t.me/SoapsGems/101",
                "text": "NEW CALL: Nova Coin ($NOVA)\\nCA: 9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
            }
        ]

        hits = parse_telegram_messages(rows)
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit["token_address"], "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump")
        self.assertEqual(hit["symbol"], "NOVA")
        self.assertEqual(hit["name"], "Nova Coin")
        self.assertEqual(hit["channels"], ["SoapsGems"])

    def test_duplicate_contracts_merge(self):
        rows = [
            {
                "channel": "chanA",
                "message_id": 1,
                "message_timestamp": "2026-08-04T17:00:00+00:00",
                "message_url": "https://t.me/chanA/1",
                "text": "contract: 8x5VqbHA8D7NkD52uNuS5nnt3PwA8pLD34ymskeSo2Wn",
            },
            {
                "channel": "chanB",
                "message_id": 2,
                "message_timestamp": "2026-08-04T17:05:00+00:00",
                "message_url": "https://t.me/chanB/2",
                "text": "mint: 8x5VqbHA8D7NkD52uNuS5nnt3PwA8pLD34ymskeSo2Wn",
            },
        ]

        hits = parse_telegram_messages(rows)
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(sorted(hit["channels"]), ["chanA", "chanB"])
        self.assertEqual(len(hit["message_refs"]), 2)

    def test_malformed_addresses_rejected(self):
        rows = [
            {
                "channel": "bad",
                "message_id": 1,
                "message_timestamp": "2026-08-04T17:00:00+00:00",
                "message_url": "",
                "text": "CA: HELLOWORLD contract: 0OIlNotBase58 and address: short123",
            }
        ]

        hits = parse_telegram_messages(rows)
        self.assertEqual(hits, [])

    def test_missing_credentials_no_crash(self):
        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_API_ID": "",
                "TELEGRAM_API_HASH": "",
                "TELEGRAM_SESSION": "",
                "TELEGRAM_CHANNELS": "",
            },
            clear=False,
        ):
            candidates, details = scan_telegram_channels(limit=20)
            self.assertEqual(candidates, [])
            self.assertFalse(details.get("configured"))
            self.assertIn("missing", str(details.get("error_message", "")).lower())


if __name__ == "__main__":
    unittest.main()
