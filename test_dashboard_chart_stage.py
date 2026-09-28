import json
import re
import unittest

import pandas as pd

import dashboard
from technical_engine import CandleDataError


def _candles(count):
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=count, freq="min", tz="UTC"),
        "open": [1.0] * count,
        "high": [1.1] * count,
        "low": [0.9] * count,
        "close": [1.05] * count,
        "volume": [10.0] * count,
    })


class ChartRenderStageTests(unittest.TestCase):
    """Regression tests: available candles must render on every timeframe, never gated on the 20-bar threshold."""

    def test_no_candles_and_no_error_reports_listening_state(self):
        stage, message = dashboard.chart_render_stage(_candles(0))
        self.assertEqual(stage, "empty")
        self.assertIn("Listening", message)

    def test_failed_historical_fetch_with_no_live_candles_reports_the_error_not_a_silent_hide(self):
        error = CandleDataError("Only 3 candles are available; Falcon needs at least 20.")
        stage, message = dashboard.chart_render_stage(_candles(0), historical_error=error)
        self.assertEqual(stage, "empty")
        self.assertIn("Historical chart data unavailable", message)
        self.assertIn("at least 20", message)

    def test_failed_historical_fetch_with_live_candles_present_still_renders_them(self):
        """The actual bug: a failed/short historical fetch must not hide real live candles."""
        error = CandleDataError("Only 3 candles are available; Falcon needs at least 20.")
        stage, message = dashboard.chart_render_stage(_candles(4), historical_error=error)
        self.assertEqual(stage, "partial")
        self.assertIn("WARMING UP", message)

    def test_stage_labels_progress_with_candle_count_below_twenty(self):
        self.assertIn("WARMING UP", dashboard.chart_render_stage(_candles(4))[1])
        self.assertIn("EARLY READ", dashboard.chart_render_stage(_candles(7))[1])
        self.assertIn("PRELIMINARY", dashboard.chart_render_stage(_candles(15))[1])

    def test_twenty_or_more_candles_are_ready_for_full_analysis(self):
        stage, message = dashboard.chart_render_stage(_candles(20))
        self.assertEqual(stage, "ready")
        self.assertIsNone(message)

    def test_one_m_and_five_m_timeframes_are_not_special_cased(self):
        """chart_render_stage is timeframe-agnostic: 1m/5m must behave exactly like the sub-minute timeframes."""
        stage, _ = dashboard.chart_render_stage(_candles(6))
        self.assertEqual(stage, "partial")


class MarketCapConversionFactorTests(unittest.TestCase):
    def test_documented_formula_is_market_cap_over_reference_price(self):
        self.assertAlmostEqual(dashboard.market_cap_conversion_factor(1_000_000.0, 0.02), 50_000_000.0)

    def test_missing_or_non_positive_inputs_are_unavailable_not_guessed(self):
        self.assertIsNone(dashboard.market_cap_conversion_factor(0.0, 0.02))
        self.assertIsNone(dashboard.market_cap_conversion_factor(1_000_000.0, 0.0))
        self.assertIsNone(dashboard.market_cap_conversion_factor(None, 0.02))


def _render_chart_payload(pool_address="poolAddr123", timeframe="1m", market_cap_factor=None):
    """Render the chart component and pull back the JSON payload embedded in the generated HTML."""
    candles = _candles(3)
    indicators = candles.copy()
    captured = {}

    def fake_html(body, **_kwargs):
        captured["body"] = body

    original_html = dashboard.components.html
    dashboard.components.html = fake_html
    try:
        dashboard.render_lightweight_chart(candles, indicators, [], pool_address, timeframe, [], None, 0.9, 1.2, False, False, market_cap_factor)
    finally:
        dashboard.components.html = original_html
    match = re.search(r"const chartData = (\{.*?\});\n", captured["body"], re.S)
    return json.loads(match.group(1))


class ChartPayloadModePersistenceTests(unittest.TestCase):
    """Regression tests for MCAP reverting to PRICE: the mode key must be stable across timeframes."""

    def test_mode_key_is_scoped_to_the_token_not_the_timeframe(self):
        payload_1m = _render_chart_payload(pool_address="poolAddr123", timeframe="1m")
        payload_15s = _render_chart_payload(pool_address="poolAddr123", timeframe="15s")
        self.assertEqual(payload_1m["modeKey"], payload_15s["modeKey"])
        self.assertNotEqual(payload_1m["chartKey"], payload_15s["chartKey"])

    def test_mode_key_differs_between_tokens(self):
        payload_a = _render_chart_payload(pool_address="poolAAA")
        payload_b = _render_chart_payload(pool_address="poolBBB")
        self.assertNotEqual(payload_a["modeKey"], payload_b["modeKey"])

    def test_market_cap_candles_are_only_present_when_a_factor_is_supplied(self):
        payload_no_mcap = _render_chart_payload(market_cap_factor=None)
        payload_with_mcap = _render_chart_payload(market_cap_factor=2.0)
        self.assertIsNone(payload_no_mcap["marketCapFactor"])
        self.assertEqual(payload_no_mcap["marketCapCandles"], [])
        self.assertEqual(payload_with_mcap["marketCapFactor"], 2.0)
        self.assertEqual(len(payload_with_mcap["marketCapCandles"]), len(payload_with_mcap["candles"]))
        # Every market-cap candle must be the price candle scaled by the same documented factor.
        for price_bar, mcap_bar in zip(payload_with_mcap["candles"], payload_with_mcap["marketCapCandles"]):
            self.assertAlmostEqual(mcap_bar["close"], price_bar["close"] * 2.0)


if __name__ == "__main__":
    unittest.main()
