import os
import unittest

from Scanner import (
    calculate_score,
    calculate_source_trust_bonus,
    get_or_set_first_seen,
)


def _pair_template():
    return {
        "liquidity": {"usd": 12000},
        "volume": {"m5": 1500, "h1": 3500},
        "priceChange": {"m5": 1.2, "h1": 2.0},
        "txns": {"m5": {"buys": 10, "sells": 7}},
        "pairCreatedAt": 1730000000000,
    }


class SourceTrustScoringTests(unittest.TestCase):
    def setUp(self):
        self._original_trust_env = os.environ.get("SOURCE_TRUST_WEIGHTS")

    def tearDown(self):
        if self._original_trust_env is None:
            os.environ.pop("SOURCE_TRUST_WEIGHTS", None)
        else:
            os.environ["SOURCE_TRUST_WEIGHTS"] = self._original_trust_env

    def test_trusted_source_bonus(self):
        os.environ.pop("SOURCE_TRUST_WEIGHTS", None)
        pair = _pair_template()

        base_candidate = {
            "raw_data": {
                "found_by": ["telegram_channels"],
                "telegram_messages": [{"channel": "randomalpha", "message_id": 1, "message_timestamp": "2026-08-04T00:00:00+00:00"}],
                "telegram_channels": ["randomalpha"],
            }
        }
        trusted_candidate = {
            "raw_data": {
                "found_by": ["telegram_channels"],
                "telegram_messages": [{"channel": "GMGN", "message_id": 1, "message_timestamp": "2026-08-04T00:00:00+00:00"}],
                "telegram_channels": ["GMGN"],
            }
        }

        base_score, _, base_breakdown = calculate_score(pair, base_candidate, smart_wallet_count=0)
        trusted_score, _, trusted_breakdown = calculate_score(pair, trusted_candidate, smart_wallet_count=0)

        self.assertGreater(trusted_score, base_score)
        self.assertGreater(trusted_breakdown.get("trust_bonus", 0), base_breakdown.get("trust_bonus", 0))

    def test_duplicate_evidence_not_double_counted(self):
        os.environ.pop("SOURCE_TRUST_WEIGHTS", None)

        candidate = {
            "raw_data": {
                "found_by": ["telegram_channels", "x_social"],
                "telegram_channels": ["GMGN", "gmgn", "@GMGN", " GMGN "],
                "x_author_usernames": ["GMGN", "gmgn", "gmgn"],
            }
        }

        trust_bonus, evidence_labels, trust_hits = calculate_source_trust_bonus(
            candidate,
            smart_wallet_count=0,
            score_before_trust=70,
        )

        self.assertEqual(trust_bonus, 5)
        self.assertEqual(trust_hits.get("telegram"), 6)
        self.assertEqual(trust_hits.get("x"), 6)
        self.assertEqual(trust_hits.get("gmgn"), 8)
        self.assertEqual(len([key for key in evidence_labels if key == "gmgn"]), 1)

    def test_rating_increases_when_new_source_appended(self):
        os.environ.pop("SOURCE_TRUST_WEIGHTS", None)
        pair = _pair_template()

        before_candidate = {
            "raw_data": {
                "found_by": ["pumpfun_tokens"],
                "pumpfun_created_at": "2026-08-04T00:00:00+00:00",
            }
        }
        after_candidate = {
            "raw_data": {
                "found_by": ["pumpfun_tokens", "x_social"],
                "pumpfun_created_at": "2026-08-04T00:00:00+00:00",
                "x_mention_count": 2,
                "x_unique_author_count": 1,
                "x_author_usernames": ["trojanonsolana"],
            }
        }

        score_before, _, _ = calculate_score(pair, before_candidate, smart_wallet_count=0)
        score_after, _, _ = calculate_score(pair, after_candidate, smart_wallet_count=0)

        self.assertGreater(score_after, score_before)

    def test_first_seen_remains_unchanged(self):
        first_seen_map = {
            "So11111111111111111111111111111111111111112": "2026-08-04T09:00:00+00:00"
        }

        persisted = get_or_set_first_seen(
            first_seen_map,
            "So11111111111111111111111111111111111111112",
            "2026-08-04T11:00:00+00:00",
        )

        self.assertEqual(persisted, "2026-08-04T09:00:00+00:00")
        self.assertEqual(
            first_seen_map["So11111111111111111111111111111111111111112"],
            "2026-08-04T09:00:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
