import json
import threading
import time
import types
import unittest
from collections import deque
from unittest import mock

import pandas as pd

from datetime import datetime, timedelta, timezone

from technical_engine import CandleDataError, LiveCandleAggregator, LiveCandleSession, LiveTrade, analyze_candles, build_signal_markers, candles_to_market_cap, merge_historical_and_live_candles, validate_live_trade


def candles(prices, volumes=None):
    volumes = volumes or [100.0] * len(prices)
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "close": prices,
            "volume": volumes,
        }
    )


class TechnicalEngineTests(unittest.TestCase):
    def test_momentum_buy_on_rising_price_and_volume(self):
        prices = [1 + i * 0.01 for i in range(27)] + [1.35, 1.39, 1.44]
        volumes = [100.0] * 29 + [300.0]
        setup = analyze_candles(candles(prices, volumes))
        self.assertEqual(setup.verdict, "MOMENTUM BUY")
        self.assertGreater(setup.target_1, setup.entry_high)
        self.assertLess(setup.stop, setup.entry_low)

    def test_sell_on_falling_structure(self):
        prices = [2 - i * 0.025 for i in range(30)]
        setup = analyze_candles(candles(prices))
        self.assertEqual(setup.verdict, "SELL")

    def test_signal_markers_follow_buy_then_sell_lifecycle(self):
        prices = [1 + i * 0.01 for i in range(45)] + [1.44 - i * 0.018 for i in range(20)]
        volumes = [100.0] * 44 + [300.0] + [120.0] * 20
        frame = candles(prices, volumes)
        frame["timestamp"] = pd.date_range("2026-01-01", periods=len(frame), freq="min", tz="UTC")
        markers = build_signal_markers(frame)
        verdicts = [marker["verdict"] for marker in markers]
        self.assertTrue(verdicts)
        self.assertIn(verdicts[0], {"CONFIRMED BUY", "MOMENTUM BUY"})
        self.assertEqual(verdicts[-1], "SELL")
        self.assertFalse(any(a == b == "SELL" for a, b in zip(verdicts, verdicts[1:])))

    def test_one_second_live_candle_rollover(self):
        aggregator = LiveCandleAggregator("1s")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first, closed = aggregator.apply_trade(LiveTrade(start, 10.0, 2.0))
        self.assertFalse(closed)
        same, closed = aggregator.apply_trade(LiveTrade(start + timedelta(milliseconds=500), 11.0, 3.0))
        self.assertFalse(closed)
        self.assertEqual((same["open"], same["high"], same["low"], same["close"], same["volume"], same["trade_count"]), (10.0, 11.0, 10.0, 11.0, 5.0, 2))
        next_candle, closed = aggregator.apply_trade(LiveTrade(start + timedelta(seconds=1), 9.0, 1.0))
        self.assertTrue(closed)
        self.assertEqual(next_candle["open"], 9.0)

    def test_live_aggregation_uses_real_trade_buckets_only(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        fifteen = LiveCandleAggregator("15s")
        thirty = LiveCandleAggregator("30s")
        for aggregator in (fifteen, thirty):
            aggregator.apply_trade(LiveTrade(start, 1.0, 2.0))
            updated, closed = aggregator.apply_trade(LiveTrade(start + timedelta(seconds=14), 1.2, 3.0))
            self.assertFalse(closed)
            self.assertEqual((updated["open"], updated["high"], updated["low"], updated["close"], updated["volume"]), (1.0, 1.2, 1.0, 1.2, 5.0))
        _, closed = fifteen.apply_trade(LiveTrade(start + timedelta(seconds=15), 1.1, 1.0))
        self.assertTrue(closed)
        _, closed = thirty.apply_trade(LiveTrade(start + timedelta(seconds=15), 1.1, 1.0))
        self.assertFalse(closed)

    def test_fifteen_second_bucket_uses_all_validated_trades_for_ohlc(self):
        aggregator = LiveCandleAggregator("15s")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for offset, price in ((0, 0.00210), (3, 0.00214), (7, 0.00208), (14, 0.00217)):
            candle, closed = aggregator.apply_trade(LiveTrade(start + timedelta(seconds=offset), price, 1.0, quote_amount=price))
            self.assertFalse(closed)
        self.assertEqual((candle["open"], candle["high"], candle["low"], candle["close"], candle["trade_count"]), (0.00210, 0.00217, 0.00208, 0.00217, 4))

    def test_thirty_second_bucket_aggregates_multiple_fifteen_second_periods(self):
        aggregator = LiveCandleAggregator("30s")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for offset, price in ((0, 1.0), (14, 1.2), (15, 0.9), (29, 1.1)):
            candle, closed = aggregator.apply_trade(LiveTrade(start + timedelta(seconds=offset), price, 1.0, quote_amount=price))
            self.assertFalse(closed)
        self.assertEqual((candle["open"], candle["high"], candle["low"], candle["close"], candle["trade_count"]), (1.0, 1.2, 0.9, 1.1, 4))

    def test_price_validator_rejects_decimal_outlier_but_allows_volatile_move(self):
        reference = 0.0000031
        bad_trade = LiveTrade(datetime.now(timezone.utc), 0.00155, 10.0, token_mint="mint", quote_amount=0.0155, signature="bad")
        good_trade = LiveTrade(datetime.now(timezone.utc), 0.0000045, 10.0, token_mint="mint", quote_amount=0.000045, signature="good")
        self.assertFalse(validate_live_trade(bad_trade, reference, reference)[0])
        self.assertTrue(validate_live_trade(good_trade, reference, reference)[0])

    def test_market_cap_conversion_preserves_price_candle_shape(self):
        frame = pd.DataFrame({"open": [0.000003], "high": [0.000004], "low": [0.000002], "close": [0.0000035], "volume": [10.0]})
        converted = candles_to_market_cap(frame, 1_000_000_000)
        self.assertEqual(converted.loc[0, "high"], 4000.0)
        self.assertEqual(converted.loc[0, "low"], 2000.0)

    def test_first_live_candle_uses_only_actual_trade_not_reference_price(self):
        aggregator = LiveCandleAggregator("1s")
        trade = LiveTrade(datetime(2026, 1, 1, tzinfo=timezone.utc), 0.0000410, 12.0, quote_amount=0.000492)
        candle, closed = aggregator.apply_trade(trade)
        self.assertFalse(closed)
        self.assertEqual((candle["open"], candle["high"], candle["low"], candle["close"], candle["trade_count"]), (0.0000410, 0.0000410, 0.0000410, 0.0000410, 1))
        self.assertNotEqual(candle["open"], 0.0000031)

    def test_two_real_trades_in_one_second_create_exact_ohlc(self):
        aggregator = LiveCandleAggregator("1s")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        aggregator.apply_trade(LiveTrade(start, 0.0000410, 1.0, quote_amount=0.000041))
        candle, _ = aggregator.apply_trade(LiveTrade(start + timedelta(milliseconds=600), 0.0000430, 2.0, quote_amount=0.000086))
        self.assertEqual((candle["open"], candle["high"], candle["low"], candle["close"], candle["trade_count"]), (0.0000410, 0.0000430, 0.0000410, 0.0000430, 2))

    def test_non_usd_trade_cannot_enter_canonical_candle_series(self):
        aggregator = LiveCandleAggregator("1s")
        sol_trade = LiveTrade(datetime.now(timezone.utc), 0.0000002, 1.0, quote_amount=0.1, quote_currency="SOL", price_unit="SOL/token")
        with self.assertRaises(CandleDataError):
            aggregator.apply_trade(sol_trade)

    def test_mcap_calibration_transforms_identical_timestamped_candles(self):
        price = 0.0000411329
        effective_supply = 47_500 / price
        frame = pd.DataFrame({"timestamp": [datetime(2026, 1, 1, tzinfo=timezone.utc)], "open": [price], "high": [0.000043], "low": [0.00004], "close": [0.000042], "volume": [10.0]})
        mcap = candles_to_market_cap(frame, effective_supply)
        self.assertEqual(list(mcap["timestamp"]), list(frame["timestamp"]))
        self.assertAlmostEqual(mcap.loc[0, "open"], 47_500)
        for column in ("open", "high", "low", "close"):
            self.assertAlmostEqual(mcap.loc[0, column] / frame.loc[0, column], effective_supply)

    def test_live_store_refuses_historical_minute_candles(self):
        session = object.__new__(LiveCandleSession)
        with self.assertRaises(CandleDataError):
            session.append_historical(pd.DataFrame({"timestamp": [datetime.now(timezone.utc)]}))

    def test_historical_context_merges_live_candles_chronologically(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        historical = pd.DataFrame({"timestamp": [start, start + timedelta(minutes=1)], "open": [1.0, 1.1], "high": [1.1, 1.2], "low": [0.9, 1.0], "close": [1.05, 1.15], "volume": [10.0, 11.0]})
        live = pd.DataFrame({"timestamp": [start + timedelta(minutes=1), start + timedelta(minutes=2)], "open": [1.16, 1.2], "high": [1.3, 1.4], "low": [1.1, 1.15], "close": [1.25, 1.3], "volume": [5.0, 6.0], "trade_count": [2, 3]})
        merged = merge_historical_and_live_candles(historical, live)
        self.assertEqual(len(merged), 3)
        self.assertEqual(list(merged["timestamp"]), sorted(merged["timestamp"].tolist()))
        self.assertEqual(merged.iloc[1]["close"], 1.15)
        self.assertEqual(merged.iloc[2]["close"], 1.3)

    def test_rejects_short_history(self):
        with self.assertRaises(CandleDataError):
            analyze_candles(candles([1.0] * 10))


if __name__ == "__main__":
    unittest.main()

class HeliusLiveParserTests(unittest.TestCase):
    def test_mint_delta_parser_extracts_target_and_wsol(self):
        from technical_engine import HeliusLiveTradeProvider, WSOL_MINT
        mint = "TokenMint111"
        meta = {
            "preTokenBalances": [
                {"mint": mint, "owner": "wallet", "uiTokenAmount": {"uiAmount": 100.0, "decimals": 6}},
                {"mint": WSOL_MINT, "owner": "wallet", "uiTokenAmount": {"uiAmount": 2.0, "decimals": 9}},
            ],
            "postTokenBalances": [
                {"mint": mint, "owner": "wallet", "uiTokenAmount": {"uiAmount": 120.0, "decimals": 6}},
                {"mint": WSOL_MINT, "owner": "wallet", "uiTokenAmount": {"uiAmount": 1.8, "decimals": 9}},
            ],
        }
        deltas = HeliusLiveTradeProvider._mint_deltas(meta)
        self.assertAlmostEqual(deltas[mint], 20.0)
        self.assertAlmostEqual(deltas[WSOL_MINT], -0.2)

    def test_raw_token_amount_is_normalized_with_decimals(self):
        from technical_engine import HeliusLiveTradeProvider
        mint = "DecimalsMint"
        meta = {"preTokenBalances": [{"mint": mint, "owner": "wallet", "uiTokenAmount": {"amount": "1500000", "decimals": 6}}], "postTokenBalances": [{"mint": mint, "owner": "wallet", "uiTokenAmount": {"amount": "2500000", "decimals": 6}}]}
        self.assertEqual(HeliusLiveTradeProvider._mint_deltas(meta)[mint], 1.0)

    def test_lamports_are_normalized_to_sol_for_native_quote(self):
        from technical_engine import HeliusLiveTradeProvider
        tx = {"transaction": {"message": {"accountKeys": [{"pubkey": "wallet"}]}}, "meta": {"preBalances": [2_000_000_000], "postBalances": [1_500_000_000], "preTokenBalances": [{"mint": "mint", "owner": "wallet", "uiTokenAmount": {"amount": "0", "decimals": 6}}], "postTokenBalances": [{"mint": "mint", "owner": "wallet", "uiTokenAmount": {"amount": "1000000", "decimals": 6}}]}}
        self.assertAlmostEqual(HeliusLiveTradeProvider._native_sol_delta(tx, tx["meta"], "mint"), -0.5)

class HeliusFreeTierParserTests(unittest.TestCase):
    def test_native_sol_quote_is_detected_for_pump_style_swap(self):
        from technical_engine import HeliusLiveTradeProvider
        mint = "TokenMintPump111"
        tx = {
            "transaction": {"message": {"accountKeys": [
                {"pubkey": "wallet"}, {"pubkey": "token-account"}, {"pubkey": "curve"}
            ]}},
            "meta": {
                "err": None,
                "preBalances": [2_000_000_000, 0, 10_000_000_000],
                "postBalances": [1_799_995_000, 0, 10_200_000_000],
                "preTokenBalances": [
                    {"accountIndex": 1, "mint": mint, "owner": "wallet", "uiTokenAmount": {"uiAmount": 100.0, "decimals": 6}},
                ],
                "postTokenBalances": [
                    {"accountIndex": 1, "mint": mint, "owner": "wallet", "uiTokenAmount": {"uiAmount": 120.0, "decimals": 6}},
                ],
            },
        }
        components = HeliusLiveTradeProvider._trade_components(tx, mint)
        self.assertIsNotNone(components)
        size, sol_per_token = components
        self.assertAlmostEqual(size, 20.0)
        # Includes the small transaction fee in wallet SOL movement; close enough
        # for live price construction and subsequent calibration.
        self.assertAlmostEqual(sol_per_token, 0.01000025, places=7)


def _fake_ws_module(url_scripts):
    """Build a fake ``websocket`` module whose WebSocketApp plays scripted sessions per URL.

    ``url_scripts`` maps a URL to a list of callables; each call to ``run_forever()``
    for that URL consumes the next callable, which drives the ws's on_open/on_message/
    on_close callbacks synchronously (no real sockets, no real threads). Every created
    instance is recorded in ``module.instances[url]`` so tests can inspect what was sent.
    """
    counters = {url: 0 for url in url_scripts}
    instances: dict[str, list] = {url: [] for url in url_scripts}

    class FakeWebSocketApp:
        def __init__(self, url, on_open=None, on_message=None, on_error=None, on_close=None):
            self.url = url
            self.on_open = on_open
            self.on_message = on_message
            self.on_error = on_error
            self.on_close = on_close
            self.sent = []
            self.closed = False
            instances.setdefault(url, []).append(self)

        def send(self, data):
            self.sent.append(data)

        def close(self):
            self.closed = True

        def run_forever(self, **_kwargs):
            scripts = url_scripts.get(self.url, [])
            index = counters.get(self.url, 0)
            counters[self.url] = index + 1
            if index < len(scripts):
                scripts[index](self)

    return types.SimpleNamespace(WebSocketApp=FakeWebSocketApp, instances=instances)


def _enhanced_swap_transaction(mint, signature, block_time, token_delta_ui, quote_delta_ui, decimals=6):
    from technical_engine import USDC_MINT
    return {
        "transaction": {"message": {"accountKeys": [{"pubkey": "wallet"}]}, "signatures": [signature]},
        "meta": {
            "err": None,
            "blockTime": block_time,
            "preTokenBalances": [
                {"mint": mint, "owner": "wallet", "uiTokenAmount": {"uiAmount": 100.0, "decimals": decimals}},
                {"mint": USDC_MINT, "owner": "wallet", "uiTokenAmount": {"uiAmount": 50.0, "decimals": 6}},
            ],
            "postTokenBalances": [
                {"mint": mint, "owner": "wallet", "uiTokenAmount": {"uiAmount": 100.0 + token_delta_ui, "decimals": decimals}},
                {"mint": USDC_MINT, "owner": "wallet", "uiTokenAmount": {"uiAmount": 50.0 - quote_delta_ui, "decimals": 6}},
            ],
        },
    }


def _enhanced_notification_json(signature, tx):
    """Documented Helius shape: the transaction payload sits directly under params.result."""
    return json.dumps({"jsonrpc": "2.0", "method": "transactionNotification", "params": {"subscription": 1, "result": {"signature": signature, "transaction": tx}}})


def _enhanced_notification_json_wrapped(signature, tx):
    """Legacy/alternate shape some Solana WS notifications use: result nests payload under "value"."""
    return json.dumps({"jsonrpc": "2.0", "method": "transactionNotification", "params": {"subscription": 1, "result": {"value": {"signature": signature, "transaction": tx}}}})


_ACK_JSON = json.dumps({"jsonrpc": "2.0", "id": 1, "result": 1})


class HeliusEnhancedStreamTests(unittest.TestCase):
    def _worker(self, on_trade, **overrides):
        from technical_engine import HeliusLiveTradeProvider, _HeliusStreamWorker
        provider = HeliusLiveTradeProvider(api_key="testkey")
        kwargs = {"backoff_seconds": 0}
        kwargs.update(overrides)
        return provider, _HeliusStreamWorker(provider, "MintUnderTest111", on_trade, **kwargs)

    def test_successful_transaction_subscribe_parses_documented_direct_result_notification(self):
        """Regression test: Helius documents the tx payload directly under params.result (no "value" wrapper)."""
        trades = []
        provider, worker = self._worker(trades.append)
        tx = _enhanced_swap_transaction(worker.mint, "sig-1", 1_700_000_000, 20.0, 10.0)
        notification = _enhanced_notification_json("sig-1", tx)

        def session(ws):
            ws.on_open(ws)
            ws.on_message(ws, _ACK_JSON)
            ws.on_message(ws, notification)
            ws.on_close(ws)
            worker.stop.set()

        fake_module = _fake_ws_module({provider.enhanced_ws_url: [session]})
        with mock.patch("technical_engine.websocket", fake_module):
            result = worker.run_enhanced()

        self.assertTrue(result)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].signature, "sig-1")
        self.assertEqual(worker.diagnostics.stream_mode, "ENHANCED")
        self.assertEqual(worker.diagnostics.subscription_id, "1")
        self.assertEqual(worker.diagnostics.notifications_received, 1)
        self.assertEqual(worker.diagnostics.notifications_parsed, 1)
        self.assertEqual(worker.diagnostics.parse_failures, 0)
        self.assertEqual(worker.diagnostics.validated_swaps, 1)
        # Enhanced mode delivers the full transaction inline: no RPC fetch, no queue.
        self.assertEqual(worker.diagnostics.transactions_fetched, 0)
        self.assertEqual(worker.diagnostics.queue_depth, 0)

    def test_wrapped_value_notification_shape_is_still_accepted(self):
        """Backward compatibility: a params.result.value wrapper must still parse correctly."""
        trades = []
        provider, worker = self._worker(trades.append)
        tx = _enhanced_swap_transaction(worker.mint, "sig-wrapped", 1_700_000_050, 12.0, 6.0)
        notification = _enhanced_notification_json_wrapped("sig-wrapped", tx)

        def session(ws):
            ws.on_open(ws)
            ws.on_message(ws, _ACK_JSON)
            ws.on_message(ws, notification)
            ws.on_close(ws)
            worker.stop.set()

        fake_module = _fake_ws_module({provider.enhanced_ws_url: [session]})
        with mock.patch("technical_engine.websocket", fake_module):
            worker.run_enhanced()

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].signature, "sig-wrapped")
        self.assertEqual(worker.diagnostics.notifications_parsed, 1)

    def test_unparseable_notification_is_counted_and_reported_not_silently_dropped(self):
        provider, worker = self._worker(lambda trade: None)
        malformed = json.dumps({"jsonrpc": "2.0", "method": "transactionNotification", "params": {"subscription": 1, "result": {"signature": "sig-bad"}}})

        def session(ws):
            ws.on_open(ws)
            ws.on_message(ws, _ACK_JSON)
            ws.on_message(ws, malformed)
            ws.on_close(ws)
            worker.stop.set()

        fake_module = _fake_ws_module({provider.enhanced_ws_url: [session]})
        with mock.patch("technical_engine.websocket", fake_module):
            worker.run_enhanced()

        self.assertEqual(worker.diagnostics.notifications_received, 1)
        self.assertEqual(worker.diagnostics.notifications_parsed, 0)
        self.assertEqual(worker.diagnostics.parse_failures, 1)
        self.assertTrue(worker.diagnostics.last_parser_error)

    def test_duplicate_signature_is_rejected_after_reconnect(self):
        trades = []
        provider, worker = self._worker(trades.append)
        tx = _enhanced_swap_transaction(worker.mint, "sig-dup", 1_700_000_100, 5.0, 2.5)
        notification = _enhanced_notification_json("sig-dup", tx)

        def session(ws):
            ws.on_open(ws)
            ws.on_message(ws, _ACK_JSON)
            ws.on_message(ws, notification)
            ws.on_message(ws, notification)  # duplicate within the same connection
            ws.on_close(ws)
            worker.stop.set()

        fake_module = _fake_ws_module({provider.enhanced_ws_url: [session]})
        with mock.patch("technical_engine.websocket", fake_module):
            worker.run_enhanced()

        self.assertEqual(len(trades), 1)
        # Both notifications were received off the wire; only the first was newly parsed.
        self.assertEqual(worker.diagnostics.notifications_received, 2)
        self.assertEqual(worker.diagnostics.notifications_parsed, 1)

    def test_reconnect_resubscribes_after_a_transient_drop(self):
        trades = []
        provider, worker = self._worker(trades.append)
        tx = _enhanced_swap_transaction(worker.mint, "sig-2", 1_700_000_200, 8.0, 4.0)
        notification = _enhanced_notification_json("sig-2", tx)

        def dropped(ws):
            ws.on_open(ws)  # connects, but never acknowledges or delivers before closing
            ws.on_close(ws)

        def recovered(ws):
            ws.on_open(ws)
            ws.on_message(ws, _ACK_JSON)
            ws.on_message(ws, notification)
            ws.on_close(ws)
            worker.stop.set()

        fake_module = _fake_ws_module({provider.enhanced_ws_url: [dropped, recovered]})
        with mock.patch("technical_engine.websocket", fake_module):
            result = worker.run_enhanced()

        self.assertTrue(result)
        self.assertEqual(len(trades), 1)
        # Each connection attempt resubscribes exactly once; no duplicate subscriptions per socket.
        sockets = fake_module.instances[provider.enhanced_ws_url]
        self.assertEqual(len(sockets), 2)
        for socket in sockets:
            subscribe_calls = [json.loads(sent) for sent in socket.sent if json.loads(sent).get("method") == "transactionSubscribe"]
            self.assertEqual(len(subscribe_calls), 1)
        self.assertEqual(worker.diagnostics.subscription_id, "1")

    def test_repeated_enhanced_failures_trigger_permanent_fallback_signal(self):
        provider, worker = self._worker(lambda trade: None, max_consecutive_enhanced_failures=3)

        def dropped(ws):
            ws.on_open(ws)
            ws.on_close(ws)

        fake_module = _fake_ws_module({provider.enhanced_ws_url: [dropped, dropped, dropped]})
        with mock.patch("technical_engine.websocket", fake_module):
            result = worker.run_enhanced()

        self.assertFalse(result)
        self.assertIn("falling back", worker.diagnostics.last_parser_error.lower())

    def test_dispatcher_falls_back_to_standard_logs_subscribe_after_enhanced_failures(self):
        provider, worker = self._worker(lambda trade: None, max_consecutive_enhanced_failures=2)

        def dropped(ws):
            ws.on_open(ws)
            ws.on_close(ws)

        def standard_open(ws):
            ws.on_open(ws)
            worker.stop.set()

        fake_module = _fake_ws_module({
            provider.enhanced_ws_url: [dropped, dropped],
            provider.ws_url: [standard_open],
        })
        with mock.patch("technical_engine.websocket", fake_module):
            worker.dispatcher()

        self.assertEqual(worker.diagnostics.stream_mode, "STANDARD")
        standard_sockets = fake_module.instances[provider.ws_url]
        self.assertEqual(len(standard_sockets), 1)
        sent_methods = [json.loads(sent).get("method") for sent in standard_sockets[0].sent]
        self.assertIn("logsSubscribe", sent_methods)

    def test_standard_notification_queues_signature_for_get_transaction_fetch(self):
        provider, worker = self._worker(lambda trade: None)
        signature = "sig-standard-1"
        message = json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {"result": {"value": {"signature": signature, "err": None}}}})
        worker.handle_standard_message(None, message)
        self.assertEqual(list(worker.work), [signature])
        self.assertEqual(worker.diagnostics.queue_depth, 1)
        # A repeat notification for the same signature must not requeue it.
        worker.handle_standard_message(None, message)
        self.assertEqual(list(worker.work), [signature])


class HeliusOutOfOrderTests(unittest.TestCase):
    def test_out_of_order_trade_cannot_reopen_a_closed_bucket(self):
        aggregator = LiveCandleAggregator("1s")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        aggregator.apply_trade(LiveTrade(start, 1.0, 1.0))
        aggregator.apply_trade(LiveTrade(start + timedelta(seconds=1), 1.1, 1.0))
        with self.assertRaises(CandleDataError):
            aggregator.apply_trade(LiveTrade(start + timedelta(milliseconds=200), 0.5, 1.0))
        # The closed bucket's candle remains untouched by the rejected out-of-order trade.
        self.assertEqual(aggregator.current["open"], 1.1)


def _enhanced_sol_swap_transaction(mint, signature, block_time, token_delta_ui, sol_delta, decimals=6):
    """A native-SOL-quoted swap (no USDC balances present), mirroring a pump.fun-style bonding curve trade."""
    lamports_per_sol = 1_000_000_000
    pre_wallet_lamports = 2_000_000_000
    delta_lamports = int(sol_delta * lamports_per_sol)
    return {
        "transaction": {"message": {"accountKeys": [{"pubkey": "wallet"}, {"pubkey": "token-account"}, {"pubkey": "curve"}]}, "signatures": [signature]},
        "meta": {
            "err": None,
            "blockTime": block_time,
            "preBalances": [pre_wallet_lamports, 0, 10_000_000_000],
            "postBalances": [pre_wallet_lamports + delta_lamports, 0, 10_000_000_000 - delta_lamports],
            "preTokenBalances": [{"accountIndex": 1, "mint": mint, "owner": "wallet", "uiTokenAmount": {"uiAmount": 100.0, "decimals": decimals}}],
            "postTokenBalances": [{"accountIndex": 1, "mint": mint, "owner": "wallet", "uiTokenAmount": {"uiAmount": 100.0 + token_delta_ui, "decimals": decimals}}],
        },
    }


class SolUsdConversionTests(unittest.TestCase):
    def setUp(self):
        from technical_engine import SolUsdOracle
        # The process-wide oracle performs a real network call; disable it by
        # default so these tests exercise the session-local fallback feed
        # deterministically. Tests that specifically target the oracle patch
        # it again with a controllable fetcher.
        self._disabled_oracle = SolUsdOracle(refresh_interval=9999, fetcher=lambda: None)
        patcher = mock.patch("technical_engine._sol_usd_oracle", self._disabled_oracle)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _worker(self):
        from technical_engine import HeliusLiveTradeProvider, SolUsdPriceFeed, _HeliusStreamWorker
        trades = []
        provider = HeliusLiveTradeProvider(api_key="testkey")
        worker = _HeliusStreamWorker(provider, "MintUnderTest111", trades.append, backoff_seconds=0)
        worker.sol_usd_feed = SolUsdPriceFeed(max_age_seconds=15.0)
        return provider, worker, trades

    def test_valid_swap_converts_with_a_fresh_sol_usd_price(self):
        provider, worker, trades = self._worker()
        worker.sol_usd_feed.update(150.0, source="scanner-pair-ratio")
        tx = _enhanced_sol_swap_transaction(worker.mint, "sig-sol-1", 1_700_000_300, 10.0, -1.0)

        worker.process_transaction(tx, "sig-sol-1")

        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].price, 15.0)  # 1 SOL / 10 tokens * $150/SOL
        self.assertEqual(worker.diagnostics.validated_swaps, 1)
        self.assertEqual(worker.diagnostics.rejected_swaps, 0)

    def test_missing_sol_usd_price_rejects_without_guessing_a_number(self):
        provider, worker, trades = self._worker()
        # sol_usd_feed was never updated: no trusted price exists yet.
        tx = _enhanced_sol_swap_transaction(worker.mint, "sig-sol-2", 1_700_000_310, 10.0, -1.0)

        worker.process_transaction(tx, "sig-sol-2")

        self.assertEqual(trades, [])
        self.assertEqual(worker.diagnostics.rejected_swaps, 1)
        self.assertEqual(worker.diagnostics.rejection_reasons.get("sol_usd_missing"), 1)
        self.assertIn("sol_usd_missing", worker.diagnostics.last_rejection)

    def test_stale_sol_usd_price_is_rejected_and_distinguished_from_missing(self):
        from technical_engine import SolUsdPriceFeed
        provider, worker, trades = self._worker()
        worker.sol_usd_feed = SolUsdPriceFeed(max_age_seconds=1.0)
        worker.sol_usd_feed.update(150.0, source="scanner-pair-ratio")
        worker.sol_usd_feed._updated_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        tx = _enhanced_sol_swap_transaction(worker.mint, "sig-sol-3", 1_700_000_320, 10.0, -1.0)

        worker.process_transaction(tx, "sig-sol-3")

        self.assertEqual(trades, [])
        self.assertEqual(worker.diagnostics.rejection_reasons.get("sol_usd_stale"), 1)
        self.assertIn("sol_usd_stale", worker.diagnostics.last_rejection)

    def test_recovers_once_the_sol_usd_price_refreshes(self):
        provider, worker, trades = self._worker()
        first_tx = _enhanced_sol_swap_transaction(worker.mint, "sig-sol-4a", 1_700_000_330, 10.0, -1.0)
        worker.process_transaction(first_tx, "sig-sol-4a")
        self.assertEqual(trades, [])
        self.assertEqual(worker.diagnostics.rejection_reasons.get("sol_usd_missing"), 1)

        worker.sol_usd_feed.update(155.0, source="scanner-pair-ratio")
        second_tx = _enhanced_sol_swap_transaction(worker.mint, "sig-sol-4b", 1_700_000_331, 10.0, -1.0)
        worker.process_transaction(second_tx, "sig-sol-4b")

        self.assertEqual(len(trades), 1)
        self.assertEqual(worker.diagnostics.validated_swaps, 1)
        self.assertEqual(worker.diagnostics.rejected_swaps, 1)

    def test_global_oracle_supplies_conversion_when_session_feed_has_nothing(self):
        """The process-wide oracle is the primary source: it must work even with no scanner-pair calibration."""
        from technical_engine import SolUsdOracle
        oracle = SolUsdOracle(refresh_interval=9999, fetcher=lambda: 175.0)
        provider, worker, trades = self._worker()  # worker.sol_usd_feed is never updated

        with mock.patch("technical_engine._sol_usd_oracle", oracle):
            tx = _enhanced_sol_swap_transaction(worker.mint, "sig-oracle-1", 1_700_000_400, 10.0, -1.0)
            worker.process_transaction(tx, "sig-oracle-1")

        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].price, 17.5)  # 1 SOL / 10 tokens * $175/SOL
        self.assertEqual(worker.diagnostics.validated_swaps, 1)

    def test_oracle_exposes_bounded_fetch_failures_then_recovers(self):
        """Fetch failures must be bounded (one attempt per refresh) and visible, never retried forever or guessed."""
        from technical_engine import SolUsdOracle
        calls = {"n": 0}

        def fetcher():
            calls["n"] += 1
            if calls["n"] == 1:
                raise CandleDataError("simulated network failure")
            return 180.0

        oracle = SolUsdOracle(refresh_interval=0, fetcher=fetcher)

        price, reason = oracle.usable_price()
        self.assertIsNone(price)
        self.assertEqual(reason, "missing")
        self.assertEqual(oracle.consecutive_failures, 1)
        self.assertIn("simulated network failure", oracle.last_error)

        price, reason = oracle.usable_price()
        self.assertEqual(price, 180.0)
        self.assertEqual(reason, "")
        self.assertEqual(oracle.consecutive_failures, 0)
        self.assertEqual(oracle.last_error, "")
        self.assertEqual(calls["n"], 2)

    def test_live_candle_session_refreshes_the_shared_feed_on_every_tick(self):
        """Regression test for the frozen-forever bug: update_sol_usd must be callable repeatedly post-construction."""
        from technical_engine import LiveCandleSession, SolUsdPriceFeed
        session = object.__new__(LiveCandleSession)
        session.sol_usd_feed = SolUsdPriceFeed(max_age_seconds=10.0)

        session.update_sol_usd(142.5)
        price, reason = session.sol_usd_feed.usable_price()
        self.assertEqual(price, 142.5)
        self.assertEqual(reason, "")

        session.update_sol_usd(150.0)
        price, _ = session.sol_usd_feed.usable_price()
        self.assertEqual(price, 150.0)

    def test_missing_and_stale_sol_usd_price_report_source_and_age_via_usable_price(self):
        from technical_engine import SolUsdPriceFeed
        feed = SolUsdPriceFeed(max_age_seconds=5.0)
        self.assertEqual(feed.usable_price(), (None, "missing"))
        feed.update(100.0, source="scanner-pair-ratio")
        price, reason = feed.usable_price()
        self.assertEqual((price, reason), (100.0, ""))
        self.assertLess(feed.age_seconds(), 1.0)
        feed._updated_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        self.assertEqual(feed.usable_price(), (None, "stale"))


class LiveSessionIsolationTests(unittest.TestCase):
    """Regression tests: distinct tokens must never share diagnostics, candles, or the same trade stream."""

    @staticmethod
    def _instant_close_ws_module():
        class _InstantWS:
            def __init__(self, *args, **kwargs):
                pass

            def run_forever(self, **_kwargs):
                pass

            def close(self):
                pass

        return types.SimpleNamespace(WebSocketApp=_InstantWS)

    def test_subscribe_live_trades_never_touches_the_shared_provider_diagnostics(self):
        """Regression test: subscribe_live_trades used to overwrite provider.diagnostics on every new token."""
        from technical_engine import HeliusLiveTradeProvider, LiveFeedDiagnostics
        provider = HeliusLiveTradeProvider(api_key="testkey")
        provider.diagnostics.notifications_received = 999  # sentinel: must remain untouched

        diag_a = LiveFeedDiagnostics()
        diag_b = LiveFeedDiagnostics()

        with mock.patch("technical_engine.websocket", self._instant_close_ws_module()):
            unsubscribe_a = provider.subscribe_live_trades("MintA111", lambda trade: None, reference_price_usd=1.0, diagnostics=diag_a)
            unsubscribe_b = provider.subscribe_live_trades("MintB222", lambda trade: None, reference_price_usd=2.0, diagnostics=diag_b)
        unsubscribe_a()
        unsubscribe_b()

        self.assertEqual(provider.diagnostics.notifications_received, 999)
        self.assertEqual(diag_a.reference_price, 1.0)
        self.assertEqual(diag_b.reference_price, 2.0)
        self.assertIsNot(diag_a, diag_b)

    def test_two_workers_sharing_one_provider_have_independent_diagnostics(self):
        from technical_engine import HeliusLiveTradeProvider, _HeliusStreamWorker
        provider = HeliusLiveTradeProvider(api_key="testkey")
        trades_a, trades_b = [], []
        worker_a = _HeliusStreamWorker(provider, "MintA111", trades_a.append, backoff_seconds=0)
        worker_b = _HeliusStreamWorker(provider, "MintB222", trades_b.append, backoff_seconds=0)

        worker_a.diagnostics.notifications_received += 5
        worker_b.diagnostics.notifications_received += 1

        self.assertEqual(worker_a.diagnostics.notifications_received, 5)
        self.assertEqual(worker_b.diagnostics.notifications_received, 1)
        self.assertIsNot(worker_a.diagnostics, worker_b.diagnostics)

    @staticmethod
    def _bare_session(provider, mint):
        """Build a LiveCandleSession without starting a real subscription (no threads/sockets)."""
        from technical_engine import HistoricalBackfillResult, LiveCandleAggregator, LiveCandleSession, LiveFeedDiagnostics, LIVE_TIMEFRAME_SECONDS, SolUsdPriceFeed
        session = object.__new__(LiveCandleSession)
        session.aggregators = {timeframe: LiveCandleAggregator(timeframe) for timeframe in LIVE_TIMEFRAME_SECONDS}
        session.candles = {timeframe: deque(maxlen=600) for timeframe in LIVE_TIMEFRAME_SECONDS}
        session.lock = threading.RLock()
        session.last_trade_at = None
        session.last_candle_update_at = None
        session.recent_trades = deque(maxlen=30)
        session.provider = provider
        session.diagnostics = LiveFeedDiagnostics()
        session.sol_usd_feed = SolUsdPriceFeed()
        session.backfill = HistoricalBackfillResult()
        session.mint = mint
        return session

    def test_candle_stores_are_isolated_per_session_even_with_a_shared_provider(self):
        from technical_engine import HeliusLiveTradeProvider
        provider = HeliusLiveTradeProvider(api_key="testkey")
        session_a = self._bare_session(provider, "MintA111")
        session_b = self._bare_session(provider, "MintB222")

        trade = LiveTrade(datetime.now(timezone.utc), 1.0, 5.0, token_mint="MintA111")
        candle, _closed = session_a.aggregators["1s"].apply_trade(trade)
        session_a.candles["1s"].append(candle)

        self.assertEqual(len(session_a.candles["1s"]), 1)
        self.assertEqual(len(session_b.candles["1s"]), 0)

    def test_get_live_feed_diagnostics_is_scoped_to_the_requested_mint(self):
        import technical_engine as te
        provider = te.HeliusLiveTradeProvider(api_key="testkey")
        session_a = self._bare_session(provider, "MintA111")
        session_b = self._bare_session(provider, "MintB222")
        session_a.diagnostics.notifications_received = 10
        session_b.diagnostics.notifications_received = 20

        with mock.patch.object(te, "_live_sessions", {"MintA111": session_a, "MintB222": session_b}):
            diag_a = te.get_live_feed_diagnostics("MintA111")
            diag_b = te.get_live_feed_diagnostics("MintB222")

        self.assertEqual(diag_a["notifications_received"], 10)
        self.assertEqual(diag_b["notifications_received"], 20)

    def test_switching_the_selected_token_does_not_mutate_the_previous_tokens_diagnostics(self):
        """Simulates a user switching charts while the old token's subscription stays alive in the background."""
        import technical_engine as te
        provider = te.HeliusLiveTradeProvider(api_key="testkey")
        session_a = self._bare_session(provider, "MintA111")
        session_a.diagnostics.validated_swaps = 7

        with mock.patch.object(te, "_live_sessions", {"MintA111": session_a}):
            # User switches to a brand-new token: a fresh session is created and registered.
            session_b = self._bare_session(provider, "MintB222")
            session_b.diagnostics.validated_swaps = 3
            te._live_sessions["MintB222"] = session_b

            diag_a_after_switch = te.get_live_feed_diagnostics("MintA111")
            diag_b_after_switch = te.get_live_feed_diagnostics("MintB222")

        self.assertEqual(diag_a_after_switch["validated_swaps"], 7)
        self.assertEqual(diag_b_after_switch["validated_swaps"], 3)


class FifteenSecondChartTests(unittest.TestCase):
    """Regression tests for the "working 15-second charts" request."""

    def test_fifteen_second_bucket_aligns_to_the_utc_epoch_not_session_start(self):
        aggregator = LiveCandleAggregator("15s")
        # 00:00:07 UTC is 7s past a UTC-aligned 15s boundary (midnight is always
        # a multiple of 15s since epoch); the bucket must not depend on when
        # the session/aggregator itself happened to start.
        base = datetime(2026, 1, 1, 0, 0, 7, tzinfo=timezone.utc)
        candle, closed = aggregator.apply_trade(LiveTrade(base, 1.0, 1.0))
        self.assertFalse(closed)
        self.assertEqual(candle["timestamp"], datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))

        same_bucket, closed = aggregator.apply_trade(LiveTrade(base + timedelta(seconds=6), 1.2, 1.0))  # t=13
        self.assertFalse(closed)
        self.assertEqual(same_bucket["timestamp"], datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))

        next_bucket, closed = aggregator.apply_trade(LiveTrade(base + timedelta(seconds=8), 1.3, 1.0))  # t=15
        self.assertTrue(closed)
        self.assertEqual(next_bucket["timestamp"], datetime(2026, 1, 1, 0, 0, 15, tzinfo=timezone.utc))

    def test_repeated_trades_within_one_fifteen_second_bucket_update_the_same_candle(self):
        aggregator = LiveCandleAggregator("15s")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for offset, price in ((0, 1.0), (2, 1.05), (6, 0.95), (11, 1.2), (14, 1.1)):
            candle, closed = aggregator.apply_trade(LiveTrade(start + timedelta(seconds=offset), price, 1.0))
            self.assertFalse(closed)
        self.assertEqual((candle["open"], candle["high"], candle["low"], candle["close"], candle["trade_count"]), (1.0, 1.2, 0.95, 1.1, 5))

    def test_fifteen_second_out_of_order_trade_cannot_reopen_a_closed_bucket(self):
        aggregator = LiveCandleAggregator("15s")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        aggregator.apply_trade(LiveTrade(start, 1.0, 1.0))
        aggregator.apply_trade(LiveTrade(start + timedelta(seconds=15), 1.1, 1.0))
        with self.assertRaises(CandleDataError):
            aggregator.apply_trade(LiveTrade(start + timedelta(seconds=5), 0.5, 1.0))
        # The closed 15s bucket must remain exactly what it was before the rejected trade.
        self.assertEqual(aggregator.current["open"], 1.1)

    def test_get_live_candle_session_ignores_timeframe_and_reuses_one_session_per_mint(self):
        """Switching timeframes must preserve the token's live session and its collected candle history."""
        import technical_engine as te
        created = []

        class _FakeSession:
            def __init__(self, provider, mint, reference_price_usd, sol_usd=None):
                created.append(mint)
                self.mint = mint

            def update_sol_usd(self, price, source="scanner-pair-ratio"):
                pass

            def update_reference_price(self, price, source="scanner-quote"):
                pass

        with mock.patch.object(te, "LiveCandleSession", _FakeSession), mock.patch.object(te, "_live_sessions", {}):
            session_1s = te.get_live_candle_session("MintX111", timeframe="1s", reference_price_usd=1.0, sol_usd=100.0)
            session_15s = te.get_live_candle_session("MintX111", timeframe="15s", reference_price_usd=1.0, sol_usd=100.0)
            session_1m = te.get_live_candle_session("MintX111", timeframe="1m", reference_price_usd=1.0, sol_usd=100.0)

        self.assertIs(session_1s, session_15s)
        self.assertIs(session_15s, session_1m)
        self.assertEqual(created, ["MintX111"])

    def test_chart_render_stage_shows_fifteen_second_candles_immediately_below_twenty_bars(self):
        import dashboard
        candles = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=6, freq="15s", tz="UTC"),
            "open": [1.0] * 6, "high": [1.05] * 6, "low": [0.95] * 6, "close": [1.02] * 6, "volume": [5.0] * 6,
        })
        stage, message = dashboard.chart_render_stage(candles)
        self.assertEqual(stage, "partial")
        self.assertIn("EARLY READ", message)


class HistoricalCandleCacheTests(unittest.TestCase):
    @staticmethod
    def _frame(count=25):
        return pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=count, freq="min", tz="UTC"),
            "open": [1.0] * count, "high": [1.1] * count, "low": [0.9] * count, "close": [1.05] * count, "volume": [10.0] * count,
        })

    def test_within_ttl_reuses_the_cache_without_calling_the_provider_again(self):
        from technical_engine import HistoricalCandleCache
        frame = self._frame()
        calls = {"n": 0}

        class FakeProvider:
            def get_historical_candles(self, pool_address, timeframe, limit):
                calls["n"] += 1
                return frame.copy()

        cache = HistoricalCandleCache(ttls={"1m": 30.0})
        provider = FakeProvider()
        _candles1, delayed1 = cache.get(provider, "poolA", "1m")
        _candles2, delayed2 = cache.get(provider, "poolA", "1m")
        self.assertEqual(calls["n"], 1)
        self.assertFalse(delayed1)
        self.assertFalse(delayed2)

    def test_rate_limit_backs_off_globally_across_different_pools_and_timeframes(self):
        """A 429 anywhere must pause every other historical fetch too, preventing a retry storm."""
        from technical_engine import HistoricalCandleCache

        class RateLimitedProvider:
            def get_historical_candles(self, pool_address, timeframe, limit):
                raise CandleDataError("HTTP 429 Too Many Requests")

        cache = HistoricalCandleCache(ttls={"1m": 0.0, "5m": 0.0}, backoff_seconds=30.0)
        provider = RateLimitedProvider()
        with self.assertRaises(CandleDataError):
            cache.get(provider, "poolA", "1m")
        # A completely different pool and timeframe must not retry the network during the shared cooldown.
        with self.assertRaises(CandleDataError):
            cache.get(provider, "poolB", "5m")
        self.assertGreater(cache.rate_limited_seconds_remaining(), 0)

    def test_stale_cache_is_served_during_rate_limit_instead_of_raising(self):
        from technical_engine import HistoricalCandleCache
        frame = self._frame()
        state = {"fail": False}

        class Provider:
            def get_historical_candles(self, pool_address, timeframe, limit):
                if state["fail"]:
                    raise CandleDataError("HTTP 429 Too Many Requests")
                return frame.copy()

        cache = HistoricalCandleCache(ttls={"1m": 0.0}, backoff_seconds=30.0)
        provider = Provider()
        _candles, delayed = cache.get(provider, "poolA", "1m")
        self.assertFalse(delayed)
        state["fail"] = True
        candles2, delayed2 = cache.get(provider, "poolA", "1m")
        self.assertTrue(delayed2)
        self.assertEqual(len(candles2), len(frame))

    def test_concurrent_callers_for_the_same_key_share_one_fetch(self):
        from technical_engine import HistoricalCandleCache
        frame = self._frame()
        calls = {"n": 0}
        start_gate = threading.Event()

        class SlowProvider:
            def get_historical_candles(self, pool_address, timeframe, limit):
                calls["n"] += 1
                start_gate.wait(timeout=2)
                return frame.copy()

        cache = HistoricalCandleCache(ttls={"1m": 30.0})
        provider = SlowProvider()
        results = []

        def worker():
            results.append(cache.get(provider, "poolA", "1m"))

        workers = [threading.Thread(target=worker) for _ in range(3)]
        for worker_thread in workers:
            worker_thread.start()
        time.sleep(0.05)  # let followers register as waiting before the leader's fetch completes
        start_gate.set()
        for worker_thread in workers:
            worker_thread.join(timeout=5)

        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(results), 3)

    def test_live_timeframes_never_touch_the_historical_cache(self):
        """1s/15s/30s must render from the live session alone; historical rate limits cannot affect them."""
        import dashboard
        candles = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=25, freq="15s", tz="UTC"),
            "open": [1.0] * 25, "high": [1.05] * 25, "low": [0.95] * 25, "close": [1.02] * 25, "volume": [5.0] * 25,
        })
        stage, message = dashboard.chart_render_stage(candles, historical_error=None)
        self.assertEqual(stage, "ready")
        self.assertIsNone(message)


class AggregateTradesIntoCandlesTests(unittest.TestCase):
    def test_produces_one_closed_candle_per_bucket_in_order(self):
        from technical_engine import aggregate_trades_into_candles
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        trades = [
            LiveTrade(start, 1.0, 1.0, signature="a"),
            LiveTrade(start + timedelta(seconds=5), 1.2, 1.0, signature="b"),
            LiveTrade(start + timedelta(seconds=15), 0.9, 1.0, signature="c"),
        ]
        candles = aggregate_trades_into_candles(trades, "15s")
        self.assertEqual(len(candles), 2)
        self.assertEqual((candles[0]["open"], candles[0]["high"], candles[0]["close"]), (1.0, 1.2, 1.2))
        self.assertEqual(candles[1]["open"], 0.9)

    def test_a_stray_out_of_order_trade_is_skipped_not_fatal(self):
        from technical_engine import aggregate_trades_into_candles
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        trades = [
            LiveTrade(start, 1.0, 1.0, signature="a"),
            LiveTrade(start + timedelta(seconds=15), 1.1, 1.0, signature="b"),
            LiveTrade(start + timedelta(seconds=1), 0.5, 1.0, signature="stray"),  # already in the past
        ]
        candles = aggregate_trades_into_candles(trades, "15s")
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[-1]["open"], 1.1)


class HistoricalTradeBackfillTests(unittest.TestCase):
    def test_backfill_recent_trades_respects_the_time_cutoff_and_reuses_the_existing_parser(self):
        from technical_engine import HeliusLiveTradeProvider
        provider = HeliusLiveTradeProvider(api_key="testkey")
        now = time.time()
        mint = "BackfillMint111"
        pages = {None: [
            {"signature": "sig-new", "blockTime": now - 30, "err": None},
            {"signature": "sig-old", "blockTime": now - 3600, "err": None},
        ]}
        provider.fetch_recent_signatures = lambda address, limit=100, before=None: pages.get(before, [])
        provider._fetch_transaction = lambda signature: (
            _enhanced_swap_transaction(mint, signature, int(now - 30), 10.0, 5.0) if signature == "sig-new" else None
        )

        result = provider.backfill_recent_trades(mint, minutes=5.0, max_signatures=50)

        self.assertEqual(result.signatures_seen, 2)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].signature, "sig-new")
        self.assertEqual(result.error, "")

    def test_unavailable_provider_reports_a_clear_error_instead_of_raising(self):
        from technical_engine import HeliusLiveTradeProvider
        provider = HeliusLiveTradeProvider(api_key="testkey")
        provider.api_key = ""  # simulate a missing key without falling back to the real .env value
        result = provider.backfill_recent_trades("AnyMint", minutes=5.0)
        self.assertEqual(result.trades, [])
        self.assertTrue(result.error)

    def test_a_full_first_page_continues_pagination_instead_of_stopping_early(self):
        """Regression test: a full page (== requested limit) must not be mistaken for "no more history"."""
        from technical_engine import HeliusLiveTradeProvider
        provider = HeliusLiveTradeProvider(api_key="testkey")
        now = time.time()
        mint = "PaginatedMint111"
        page_one = [{"signature": f"sig-a{i}", "blockTime": now - i, "err": None} for i in range(100)]
        page_two = [{"signature": f"sig-b{i}", "blockTime": now - 100 - i, "err": None} for i in range(20)]
        calls = []

        def fake_fetch(address, limit=100, before=None):
            calls.append((limit, before))
            if before is None:
                return page_one
            if before == "sig-a99":
                return page_two
            return []

        provider.fetch_recent_signatures = fake_fetch
        provider._fetch_transaction = lambda signature: None

        result = provider.backfill_recent_trades(mint, minutes=60.0, max_signatures=120, max_transactions=0)

        self.assertEqual(len(calls), 2)  # a second page was actually requested
        self.assertEqual(result.signatures_seen, 120)

    def test_max_transactions_keeps_only_the_most_recent_trades_within_the_window(self):
        """Bounds the paced getTransaction cost independently of how many signatures were listed."""
        from technical_engine import HeliusLiveTradeProvider
        provider = HeliusLiveTradeProvider(api_key="testkey")
        now = time.time()
        mint = "BoundedMint111"
        sigs = [{"signature": f"sig-{i}", "blockTime": now - (10 - i), "err": None} for i in range(10)]
        provider.fetch_recent_signatures = lambda address, limit=100, before=None: sigs if before is None else []
        fetched = []

        def fake_fetch_tx(signature):
            fetched.append(signature)
            index = int(signature.split("-")[1])
            return _enhanced_swap_transaction(mint, signature, int(now - (10 - index)), 10.0, 5.0)

        provider._fetch_transaction = fake_fetch_tx

        result = provider.backfill_recent_trades(mint, minutes=60.0, max_signatures=50, max_transactions=3)

        self.assertEqual(len(fetched), 3)
        # The three most recent (highest index / latest blockTime) signatures, not the oldest.
        self.assertEqual(sorted(fetched), ["sig-7", "sig-8", "sig-9"])


class ReferencePriceAnchorTests(unittest.TestCase):
    """Regression tests for extreme MCAP/price wicks caused by a stale or missing validation anchor."""

    def _worker(self):
        from technical_engine import HeliusLiveTradeProvider, _HeliusStreamWorker
        provider = HeliusLiveTradeProvider(api_key="testkey")
        worker = _HeliusStreamWorker(provider, "MintUnderTest111", lambda trade: None, backoff_seconds=0)
        return provider, worker

    def test_without_any_anchor_a_single_bad_print_silently_validates(self):
        """Documents the narrow race the fix below closes."""
        provider, worker = self._worker()
        tx = _enhanced_swap_transaction(worker.mint, "sig-first", 1_700_000_000, 20.0, 10.0)  # price = 0.5
        worker.process_transaction(tx, "sig-first")
        self.assertEqual(worker.diagnostics.validated_swaps, 1)

    def test_a_fresh_reference_price_anchor_catches_the_same_bad_print(self):
        from technical_engine import SolUsdPriceFeed
        provider, worker = self._worker()
        worker.reference_price_feed = SolUsdPriceFeed(max_age_seconds=30.0)
        worker.reference_price_feed.update(0.0001)  # the scanner's real current price for this token
        tx = _enhanced_swap_transaction(worker.mint, "sig-first", 1_700_000_000, 20.0, 10.0)  # price = 0.5, 5000x off

        worker.process_transaction(tx, "sig-first")

        self.assertEqual(worker.diagnostics.validated_swaps, 0)
        self.assertEqual(worker.diagnostics.rejected_swaps, 1)

    def test_stale_reference_price_anchor_falls_back_to_the_session_start_snapshot(self):
        from technical_engine import SolUsdPriceFeed
        provider, worker = self._worker()
        worker.ref_usd = 0.0001  # session-start snapshot, still a valid fallback anchor
        worker.reference_price_feed = SolUsdPriceFeed(max_age_seconds=1.0)
        worker.reference_price_feed.update(0.0001)
        worker.reference_price_feed._updated_at = datetime.now(timezone.utc) - timedelta(seconds=30)  # now stale
        tx = _enhanced_swap_transaction(worker.mint, "sig-first", 1_700_000_000, 20.0, 10.0)  # price = 0.5, still 5000x off

        worker.process_transaction(tx, "sig-first")

        self.assertEqual(worker.diagnostics.validated_swaps, 0)
        self.assertEqual(worker.diagnostics.rejected_swaps, 1)

    def test_reference_price_refresh_is_visible_to_the_very_next_trade(self):
        """update_reference_price (called every dashboard tick) must actually change what gets validated."""
        from technical_engine import SolUsdPriceFeed
        provider, worker = self._worker()
        worker.reference_price_feed = SolUsdPriceFeed(max_age_seconds=30.0)
        # A trade priced near 0.5 should validate once the anchor also reflects ~0.5.
        worker.reference_price_feed.update(0.5)
        tx = _enhanced_swap_transaction(worker.mint, "sig-good", 1_700_000_100, 20.0, 10.0)  # price = 0.5

        worker.process_transaction(tx, "sig-good")

        self.assertEqual(worker.diagnostics.validated_swaps, 1)


class _FakeBackfillProvider:
    """Duck-typed provider for LiveCandleSession tests: no real network, no real threads."""

    def __init__(self, backfill_result):
        self._backfill_result = backfill_result
        self.received_preseed = None
        self.on_trade = None

    def backfill_recent_trades(self, mint, minutes, max_signatures, reference_price_usd, sol_usd_feed, max_transactions=None):
        return self._backfill_result

    def subscribe_live_trades(self, mint, on_trade, reference_price_usd=None, sol_usd_feed=None, reference_price_feed=None, diagnostics=None, preseed_signatures=None):
        self.on_trade = on_trade
        self.received_preseed = list(preseed_signatures or [])
        return lambda: None


class BackfillSessionIntegrationTests(unittest.TestCase):
    def test_backfilled_trades_seed_fifteen_second_history_before_live_starts(self):
        from technical_engine import HistoricalBackfillResult, LiveCandleSession
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        trades = [
            LiveTrade(start, 1.0, 1.0, signature="bf-1"),
            LiveTrade(start + timedelta(seconds=20), 1.1, 1.0, signature="bf-2"),
        ]
        result = HistoricalBackfillResult(trades=trades, signatures_seen=2, transactions_fetched=2, candidate_swaps=2, validated_swaps=2, coverage_seconds=20.0)
        provider = _FakeBackfillProvider(result)

        session = LiveCandleSession(provider, "MintZ111", reference_price_usd=1.0, sol_usd=None)

        self.assertEqual(len(session.frame("15s")), 2)
        # The live worker must know about backfilled signatures too, so a
        # duplicate delivery of the same transaction can never double-count.
        self.assertEqual(provider.received_preseed, ["bf-1", "bf-2"])
        self.assertEqual(session.backfill.coverage_seconds, 20.0)

    def test_live_trade_in_same_bucket_as_last_backfilled_candle_updates_it_in_place(self):
        from technical_engine import HistoricalBackfillResult, LiveCandleSession
        now = datetime.now(timezone.utc)
        bucket_start = int(now.timestamp()) // 15 * 15
        backfill_ts = datetime.fromtimestamp(bucket_start, tz=timezone.utc)
        result = HistoricalBackfillResult(trades=[LiveTrade(backfill_ts, 1.0, 1.0, signature="bf-1")])
        provider = _FakeBackfillProvider(result)

        session = LiveCandleSession(provider, "MintZ222", reference_price_usd=1.0, sol_usd=None)
        self.assertEqual(len(session.frame("15s")), 1)

        provider.on_trade(LiveTrade(backfill_ts + timedelta(seconds=2), 1.5, 2.0, signature="live-1"))

        frame = session.frame("15s")
        self.assertEqual(len(frame), 1)  # merged into the existing bucket, not appended as a duplicate
        self.assertEqual(frame.iloc[0]["close"], 1.5)
        self.assertEqual(frame.iloc[0]["high"], 1.5)

    def test_backfill_failure_does_not_prevent_the_live_subscription(self):
        from technical_engine import HistoricalBackfillResult, LiveCandleSession

        class _FailingBackfillProvider(_FakeBackfillProvider):
            def backfill_recent_trades(self, *args, **kwargs):
                raise RuntimeError("network down")

        provider = _FailingBackfillProvider(HistoricalBackfillResult())
        session = LiveCandleSession(provider, "MintZ333", reference_price_usd=1.0, sol_usd=None)

        self.assertEqual(session.backfill.error, "RuntimeError")
        self.assertIsNotNone(provider.on_trade)  # live subscription still happened
        self.assertEqual(len(session.frame("15s")), 0)



