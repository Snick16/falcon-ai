import time
import unittest

from source_scanner import parse_pumpfun_payload


class PumpfunParserTests(unittest.TestCase):
    def test_parser_dedupes_and_filters_old_or_invalid_mints(self):
        now_seconds = int(time.time())
        recent = now_seconds - 120
        old = now_seconds - (3 * 3600)

        payload = {
            "coins": [
                {
                    "mint": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
                    "symbol": "NOVA",
                    "name": "Nova Coin",
                    "created_timestamp": recent,
                },
                {
                    "mint": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
                    "symbol": "NOVA",
                    "name": "Nova Coin Duplicate",
                    "created_timestamp": recent,
                },
                {
                    "mint": "INVALID_MINT_123",
                    "symbol": "BAD",
                    "name": "Bad Mint",
                    "created_timestamp": recent,
                },
                {
                    "mint": "8x5VqbHA8D7NkD52uNuS5nnt3PwA8pLD34ymskeSo2Wn",
                    "symbol": "OLD",
                    "name": "Old Coin",
                    "created_timestamp": old,
                },
            ]
        }

        rows = parse_pumpfun_payload(payload, lookback_minutes=60, max_tokens=50)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mint"], "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump")
        self.assertEqual(rows[0]["symbol"], "NOVA")
        self.assertEqual(rows[0]["name"], "Nova Coin")


if __name__ == "__main__":
    unittest.main()
