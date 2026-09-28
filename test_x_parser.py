import unittest

from source_scanner import parse_x_posts


class XParserTests(unittest.TestCase):
    def test_parse_x_posts_merges_mentions_and_authors(self):
        rows = [
            {
                "tweet_id": "1001",
                "author_id": "u1",
                "author_username": "alpha_calls",
                "created_at": "2026-08-04T17:00:00+00:00",
                "text": "Nova Coin ($NOVA) CA: 9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
                "urls": [],
            },
            {
                "tweet_id": "1002",
                "author_id": "u2",
                "author_username": "beta_calls",
                "created_at": "2026-08-04T17:02:00+00:00",
                "text": "Chart here",
                "urls": [
                    "https://pump.fun/coin/9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
                    "https://dexscreener.com/solana/9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
                ],
            },
            {
                "tweet_id": "1003",
                "author_id": "u1",
                "author_username": "alpha_calls",
                "created_at": "2026-08-04T17:05:00+00:00",
                "text": "invalid mint HELLO_WORLD_123",
                "urls": [],
            },
        ]

        hits = parse_x_posts(rows)
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit["token_address"], "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump")
        self.assertEqual(hit["symbol"], "NOVA")
        self.assertEqual(hit["name"], "Nova Coin")
        self.assertEqual(hit["mention_count"], 2)
        self.assertEqual(hit["unique_author_count"], 2)
        self.assertEqual(sorted(hit["author_usernames"]), ["alpha_calls", "beta_calls"])


if __name__ == "__main__":
    unittest.main()
