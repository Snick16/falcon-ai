import os
import unittest
from dataclasses import asdict
from unittest.mock import patch

from Scanner import derive_source_confirmation
from source_scanner import PHOTON_SOURCE_TAG, TokenCandidate, _dedupe_candidates, scan_photon_source


class PhotonCollectorTests(unittest.TestCase):
    def test_photon_available_and_token_returned_sets_confirmation(self):
        photon_token = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"
        payload = {
            "data": {
                "rank": [
                    {
                        "address": photon_token,
                        "symbol": "NOVA",
                        "name": "Nova Coin",
                    }
                ]
            }
        }

        with patch.dict(
            os.environ,
            {
                "PHOTON_ENABLED": "true",
                "PHOTON_API_URL": "https://photon-sol.tinyastro.io/api/discover",
                "PHOTON_COOKIE": "cf_clearance=test-clearance-token",
            },
            clear=False,
        ), patch("source_scanner._request_json", return_value=payload), patch(
            "source_scanner._fetch_pairs_for_token_addresses", return_value={}
        ):
            candidates, details = scan_photon_source(limit=5)

        self.assertEqual(details.get("candidates_returned"), 1)
        self.assertEqual(len(candidates), 1)
        candidate_dict = asdict(candidates[0])
        confirmation = derive_source_confirmation(candidate_dict)
        self.assertTrue(confirmation["source_confirmations"]["photon"])
        self.assertEqual(confirmation["source_confirmation_count"], 1)
        self.assertEqual(candidate_dict["raw_data"]["found_by"], [PHOTON_SOURCE_TAG])

    def test_photon_unavailable_contributes_zero_confirmation(self):
        with patch.dict(os.environ, {"PHOTON_ENABLED": "false"}, clear=False):
            candidates, details = scan_photon_source(limit=5)

        self.assertEqual(candidates, [])
        self.assertFalse(details.get("configured"))

        non_photon_candidate = {
            "source": "dexscreener_latest",
            "raw_data": {
                "found_by": ["dexscreener_latest"],
                "source_confirmations": {
                    "axiom": False,
                    "gmgn": False,
                    "photon": False,
                },
            },
        }
        confirmation = derive_source_confirmation(non_photon_candidate)
        self.assertFalse(confirmation["source_confirmations"]["photon"])
        self.assertEqual(confirmation["source_confirmation_count"], 0)

    def test_dedupe_does_not_duplicate_photon_confirmation(self):
        token_address = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"

        base_kwargs = {
            "chain": "solana",
            "token_address": token_address,
            "symbol": "NOVA",
            "name": "Nova Coin",
            "source": PHOTON_SOURCE_TAG,
            "source_url": "https://photon-sol.tinyastro.io/api/discover",
            "pair_address": "",
            "discovered_at": "2026-08-07T00:00:00+00:00",
            "market_cap": 0.0,
            "liquidity": 0.0,
            "volume_5m": 0.0,
            "volume_1h": 0.0,
            "volume_24h": 0.0,
            "price_change_5m": 0.0,
            "price_change_1h": 0.0,
            "buys_5m": 0,
            "sells_5m": 0,
            "token_age_minutes": 0.0,
            "social_mentions": 0,
        }

        first = TokenCandidate(
            **base_kwargs,
            raw_data={
                "found_by": [PHOTON_SOURCE_TAG],
                "source_confirmations": {"axiom": False, "gmgn": False, "photon": True},
            },
        )
        second = TokenCandidate(
            **base_kwargs,
            raw_data={
                "found_by": [PHOTON_SOURCE_TAG],
                "source_confirmations": {"axiom": False, "gmgn": False, "photon": True},
            },
        )

        deduped = _dedupe_candidates([first, second])
        self.assertEqual(len(deduped), 1)
        deduped_dict = asdict(deduped[0])
        self.assertEqual(deduped_dict["raw_data"]["found_by"], [PHOTON_SOURCE_TAG])

        confirmation = derive_source_confirmation(deduped_dict)
        self.assertTrue(confirmation["source_confirmations"]["photon"])
        self.assertEqual(confirmation["source_confirmation_count"], 1)


if __name__ == "__main__":
    unittest.main()
