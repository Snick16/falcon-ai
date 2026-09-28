"""Falcon Technical Intelligence Engine.

Fetches real pool OHLCV candles and turns them into an explainable trade setup.
This module intentionally contains no Streamlit code so it can be tested alone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
import json
import os
import threading
import time
from collections import deque

import pandas as pd
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import websocket
except ImportError:
    websocket = None


GECKO_TERMINAL_API = "https://api.geckoterminal.com/api/v2"
SUPPORTED_TIMEFRAMES = {"1m": 1, "5m": 5}
LIVE_TIMEFRAME_SECONDS = {"1s": 1, "15s": 15, "30s": 30, "1m": 60, "5m": 300}
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYDqg6sB3Dg1r5r5r5r5r5r"  # optional; stablecoin matching also uses symbol-independent balance deltas


class CandleDataError(RuntimeError):
    """Raised when a pool cannot supply usable candles."""


@dataclass(frozen=True)
class LiveTrade:
    """Validated normalized swap event accepted by the live candle pipeline."""
    timestamp: datetime
    price: float
    size: float
    token_mint: str = ""
    quote_amount: float = 0.0
    side: str | None = None
    signature: str = ""
    quote_currency: str = "USD"
    raw_token_delta: float = 0.0
    raw_quote_delta: float = 0.0
    token_decimals: int = 0
    price_unit: str = "USD/token"


@dataclass
class LiveFeedDiagnostics:
    websocket_connected: bool = False
    stream_mode: str = "STANDARD"  # "ENHANCED" (transactionSubscribe) or "STANDARD" (logsSubscribe fallback)
    ws_status: str = "DISCONNECTED"  # CONNECTED / RECONNECTING / DISCONNECTED
    subscription_id: str = ""
    notifications_received: int = 0
    notifications_parsed: int = 0
    parse_failures: int = 0
    transactions_fetched: int = 0
    candidate_swaps: int = 0
    validated_swaps: int = 0
    rejected_swaps: int = 0
    queue_depth: int = 0
    last_ws_message_at: str = ""
    last_valid_trade_at: str = ""
    feed_lag_ms: float | None = None
    last_validated_price: float | None = None
    reference_price: float | None = None
    last_parser_error: str = ""
    last_rejection: str = ""
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    recent_validated: list[dict[str, Any]] = field(default_factory=list)
    recent_rejected: list[dict[str, Any]] = field(default_factory=list)

    def record_rejection(self, reason: str) -> None:
        """Single counting path so every rejection (validator, SOL/USD, out-of-order) groups consistently."""
        self.rejected_swaps += 1
        category = classify_rejection_reason(reason)
        self.rejection_reasons[category] = self.rejection_reasons.get(category, 0) + 1


def classify_rejection_reason(reason: str) -> str:
    """Bucket free-form rejection text into a small, stable set of diagnostic categories."""
    lowered = (reason or "unknown").strip().lower()
    if lowered in ("sol_usd_missing", "sol_usd_stale"):
        return lowered
    if lowered.startswith("non-positive"):
        return "non_positive_values"
    if lowered.startswith("non-canonical"):
        return "non_canonical_unit"
    if "sanity range" in lowered:
        return "price_sanity_range"
    if "closed candle" in lowered or "out-of-order" in lowered:
        return "out_of_order_bucket"
    return "other"


@dataclass(frozen=True)
class SolUsdSnapshot:
    price: float | None = None
    source: str = ""
    updated_at: datetime | None = None


class SolUsdPriceFeed:
    """Thread-safe trusted SOL/USD price with an explicit staleness cutoff.

    This never fabricates a price: it only ever holds the last value a caller
    supplied via ``update`` (Falcon's own scanner-derived USD/native pair
    quote), timestamped so consumers can refuse to trade on data that is too
    old instead of silently reusing a frozen number forever.
    """

    def __init__(self, max_age_seconds: float = 15.0):
        self._lock = threading.Lock()
        self._price: float | None = None
        self._source: str = ""
        self._updated_at: datetime | None = None
        self.max_age_seconds = max_age_seconds

    def update(self, price: float | None, source: str = "scanner-pair-ratio") -> None:
        if price is None or price <= 0:
            return
        with self._lock:
            self._price = float(price)
            self._source = source
            self._updated_at = datetime.now(timezone.utc)

    def snapshot(self) -> SolUsdSnapshot:
        with self._lock:
            return SolUsdSnapshot(price=self._price, source=self._source, updated_at=self._updated_at)

    def age_seconds(self) -> float | None:
        snapshot = self.snapshot()
        if snapshot.updated_at is None:
            return None
        return (datetime.now(timezone.utc) - snapshot.updated_at).total_seconds()

    def usable_price(self) -> tuple[float | None, str]:
        """Returns (price, "") when usable, or (None, "missing"/"stale") without ever guessing a number."""
        snapshot = self.snapshot()
        if snapshot.price is None or snapshot.price <= 0 or snapshot.updated_at is None:
            return None, "missing"
        age = (datetime.now(timezone.utc) - snapshot.updated_at).total_seconds()
        if age > self.max_age_seconds:
            return None, "stale"
        return snapshot.price, ""


SOL_USD_ORACLE_URL = f"{GECKO_TERMINAL_API}/simple/networks/solana/token_price/{WSOL_MINT}"
SOL_USD_ORACLE_REFRESH_SECONDS = 20.0
SOL_USD_ORACLE_MAX_AGE_SECONDS = 90.0


class SolUsdOracle:
    """Process-wide trusted SOL/USD price, independent of any single token's own pair data.

    Many freshly-launched tokens (pre-DexScreener-indexing pump.fun style pools)
    never populate a usable per-token USD/native ratio, which previously left
    the SOL/USD conversion permanently unavailable for those sessions. This
    fetches a real market quote (GeckoTerminal's WSOL price) on a bounded,
    lazy schedule: at most one attempt per ``refresh_interval``, never blocking
    the trade path, and always surfacing failures instead of retrying forever
    or fabricating a number.
    """

    def __init__(self, refresh_interval: float = SOL_USD_ORACLE_REFRESH_SECONDS, max_age_seconds: float = SOL_USD_ORACLE_MAX_AGE_SECONDS, fetcher: Callable[[], float | None] | None = None):
        self._lock = threading.Lock()
        self._price: float | None = None
        self._updated_at: datetime | None = None
        self._last_attempt_monotonic: float = 0.0
        self.refresh_interval = refresh_interval
        self.max_age_seconds = max_age_seconds
        self.last_error: str = ""
        self.consecutive_failures: int = 0
        self._fetch = fetcher or self._fetch_from_geckoterminal

    @staticmethod
    def _fetch_from_geckoterminal() -> float | None:
        response = requests.get(SOL_USD_ORACLE_URL, headers={"Accept": "application/json", "User-Agent": "Falcon-AI-Hunter/1.0"}, timeout=6)
        response.raise_for_status()
        payload = response.json()
        prices = (((payload or {}).get("data") or {}).get("attributes") or {}).get("token_prices") or {}
        if not prices:
            raise CandleDataError("GeckoTerminal simple-price response had no token_prices")
        value = prices.get(WSOL_MINT)
        if value is None and len(prices) == 1:
            # Some networks echo back a different address casing; a single-address
            # request only ever has one entry, so take it regardless of key case.
            value = next(iter(prices.values()))
        if value is None:
            raise CandleDataError("GeckoTerminal simple-price response did not include the SOL price")
        return float(value)

    def _maybe_refresh(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_attempt_monotonic < self.refresh_interval:
                return
            self._last_attempt_monotonic = now
        try:
            price = self._fetch()
        except Exception as error:
            with self._lock:
                self.consecutive_failures += 1
                self.last_error = f"{type(error).__name__}: {error}"[:200]
            return
        with self._lock:
            if price and price > 0:
                self._price = float(price)
                self._updated_at = datetime.now(timezone.utc)
                self.consecutive_failures = 0
                self.last_error = ""
            else:
                self.consecutive_failures += 1
                self.last_error = "GeckoTerminal returned a non-positive SOL price"

    def snapshot(self) -> SolUsdSnapshot:
        self._maybe_refresh()
        with self._lock:
            return SolUsdSnapshot(price=self._price, source="geckoterminal" if self._price else "", updated_at=self._updated_at)

    def age_seconds(self) -> float | None:
        with self._lock:
            updated_at = self._updated_at
        if updated_at is None:
            return None
        return (datetime.now(timezone.utc) - updated_at).total_seconds()

    def usable_price(self) -> tuple[float | None, str]:
        """Returns (price, "") when fresh, or (None, "missing"/"stale") after a bounded, visible refresh attempt."""
        self._maybe_refresh()
        with self._lock:
            price, updated_at = self._price, self._updated_at
        if price is None or price <= 0 or updated_at is None:
            return None, "missing"
        age = (datetime.now(timezone.utc) - updated_at).total_seconds()
        if age > self.max_age_seconds:
            return None, "stale"
        return price, ""


_sol_usd_oracle = SolUsdOracle()


def sol_usd_conversion_status(local_feed: "SolUsdPriceFeed | None") -> dict[str, Any]:
    """Single source of truth for diagnostics and trade processing: prefer the trusted process-wide oracle,
    then fall back to the session's own scanner-calibrated ratio. Never fabricates a price."""
    oracle_price, oracle_reason = _sol_usd_oracle.usable_price()
    base = {
        "sol_usd_fetch_error": _sol_usd_oracle.last_error,
        "sol_usd_fetch_failures": _sol_usd_oracle.consecutive_failures,
    }
    if oracle_price is not None:
        return {**base, "sol_usd_available": True, "sol_usd_price": oracle_price, "sol_usd_source": "geckoterminal", "sol_usd_age_seconds": _sol_usd_oracle.age_seconds(), "sol_usd_status": "fresh"}
    local_price, local_reason = local_feed.usable_price() if local_feed is not None else (None, "missing")
    if local_price is not None:
        return {**base, "sol_usd_available": True, "sol_usd_price": local_price, "sol_usd_source": local_feed.snapshot().source, "sol_usd_age_seconds": local_feed.age_seconds(), "sol_usd_status": "fresh"}
    # Neither source is usable: "stale" (a price existed once) is more specific
    # and actionable than "missing", so prefer it if either source reports it.
    combined_reason = "stale" if "stale" in (oracle_reason, local_reason) else oracle_reason
    return {**base, "sol_usd_available": False, "sol_usd_price": None, "sol_usd_source": "", "sol_usd_age_seconds": _sol_usd_oracle.age_seconds(), "sol_usd_status": combined_reason}


@dataclass(frozen=True)
class SwapComponents:
    token_quantity: float
    quote_quantity: float
    quote_per_token: float
    quote_currency: str
    side: str
    token_decimals: int
    raw_token_delta: float
    raw_quote_delta: float


def validate_live_trade(trade: LiveTrade, reference_price: float | None, last_price: float | None) -> tuple[bool, str]:
    """Reject unit/decimal corruption while leaving room for real meme-token volatility."""
    if trade.price <= 0 or trade.size <= 0 or trade.quote_amount < 0:
        return False, "non-positive normalized price, quantity, or quote amount"
    if trade.price_unit != "USD/token":
        return False, f"non-canonical price unit: {trade.price_unit}"
    for anchor, label, maximum_ratio in ((reference_price, "reference", 50.0), (last_price, "last validated", 25.0)):
        if anchor and anchor > 0:
            ratio = trade.price / anchor
            if ratio > maximum_ratio or ratio < 1 / maximum_ratio:
                return False, f"price ratio {ratio:.3g} outside {label} sanity range"
    return True, ""


def candles_to_market_cap(candles: pd.DataFrame, effective_supply: float) -> pd.DataFrame:
    """Convert the same real price candles into market-cap candles using a trusted supply calibration."""
    if effective_supply <= 0:
        raise CandleDataError("A positive effective token supply is required for market-cap candles.")
    converted = candles.copy()
    for column in ("open", "high", "low", "close"):
        converted[column] = converted[column].astype(float) * effective_supply
    return converted


def merge_historical_and_live_candles(historical: pd.DataFrame, live: pd.DataFrame) -> pd.DataFrame:
    """Append only newer matching-timeframe live candles without rewriting history."""
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    history = historical.copy() if historical is not None else pd.DataFrame(columns=required)
    stream = live.copy() if live is not None else pd.DataFrame(columns=required)
    if stream.empty:
        return history.sort_values("timestamp").reset_index(drop=True)
    for frame in (history, stream):
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    if not history.empty:
        stream = stream[stream["timestamp"] > history["timestamp"].max()]
    return pd.concat([history, stream], ignore_index=True).sort_values("timestamp").reset_index(drop=True)


class MarketDataProvider(ABC):
    """Chart market-data boundary; providers own transport, not chart or signals."""

    supported_historical_timeframes: frozenset[str] = frozenset()
    supported_live_timeframes: frozenset[str] = frozenset()

    @abstractmethod
    def get_historical_candles(self, token: str, timeframe: str, limit: int) -> pd.DataFrame:
        """Return ascending real OHLCV candles for a token or pool identifier."""

    def subscribe_live_trades(self, token: str, on_trade: Callable[[LiveTrade], None]) -> Callable[[], None]:
        """Start a live trade subscription and return an unsubscribe callback."""
        raise CandleDataError("This market-data provider does not support live trades.")


class GeckoTerminalHistoricalProvider(MarketDataProvider):
    """Historical-only GeckoTerminal adapter. It must not be used as a live feed."""

    supported_historical_timeframes = frozenset(SUPPORTED_TIMEFRAMES)

    def get_historical_candles(self, token: str, timeframe: str, limit: int) -> pd.DataFrame:
        pool_address = str(token or "").strip()
        if not pool_address:
            raise CandleDataError("This token does not include a pool address.")
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise CandleDataError(f"GeckoTerminal has no configured historical timeframe: {timeframe}")
        url = f"{GECKO_TERMINAL_API}/networks/solana/pools/{pool_address}/ohlcv/minute"
        params = {"aggregate": SUPPORTED_TIMEFRAMES[timeframe], "limit": max(30, min(int(limit), 1000)), "currency": "usd", "token": "base"}
        try:
            response = requests.get(url, params=params, headers={"Accept": "application/json", "User-Agent": "Falcon-AI-Hunter/1.0"}, timeout=15)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise CandleDataError(f"Candle service request failed: {error}") from error
        rows = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not rows:
            raise CandleDataError("No candle history is available for this pool yet.")
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="s", utc=True, errors="coerce")
        frame = frame.dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        if len(frame) < 20:
            raise CandleDataError(f"Only {len(frame)} candles are available; Falcon needs at least 20.")
        return frame


class LiveCandleAggregator:
    """Locally updates the current OHLCV candle from normalized live trades."""

    def __init__(self, timeframe: str):
        if timeframe not in LIVE_TIMEFRAME_SECONDS:
            raise CandleDataError(f"Unsupported live timeframe: {timeframe}")
        self.timeframe = timeframe
        self.interval_seconds = LIVE_TIMEFRAME_SECONDS[timeframe]
        self.current: dict[str, Any] | None = None

    def apply_trade(self, trade: LiveTrade) -> tuple[dict[str, Any], bool]:
        if trade.price_unit != "USD/token":
            raise CandleDataError("Live candles only accept canonical USD/token prices.")
        timestamp = trade.timestamp.astimezone(timezone.utc)
        bucket = int(timestamp.timestamp()) // self.interval_seconds * self.interval_seconds
        if self.current is not None and bucket < int(self.current["timestamp"].timestamp()):
            raise CandleDataError("Out-of-order live trade cannot modify a closed candle bucket.")
        closed = self.current is not None and int(self.current["timestamp"].timestamp()) != bucket
        if self.current is None or closed:
            self.current = {"timestamp": datetime.fromtimestamp(bucket, tz=timezone.utc), "open": trade.price, "high": trade.price, "low": trade.price, "close": trade.price, "volume": trade.size, "trade_count": 1}
        else:
            self.current["high"] = max(self.current["high"], trade.price)
            self.current["low"] = min(self.current["low"], trade.price)
            self.current["close"] = trade.price
            self.current["volume"] += trade.size
            self.current["trade_count"] += 1
        return dict(self.current), closed


def aggregate_trades_into_candles(trades: list[LiveTrade], timeframe: str) -> list[dict[str, Any]]:
    """One-shot OHLC aggregation for a chronological trade batch (used only for historical backfill)."""
    aggregator = LiveCandleAggregator(timeframe)
    candles: list[dict[str, Any]] = []
    for trade in trades:
        try:
            candle, closed = aggregator.apply_trade(trade)
        except CandleDataError:
            continue  # a stray out-of-order trade must not abort the whole backfill
        if closed or not candles:
            candles.append(candle)
        else:
            candles[-1] = candle
    return candles


@dataclass
class HistoricalBackfillResult:
    """Transparent accounting for the best-effort pre-session trade backfill; never a guarantee of coverage."""
    trades: list[LiveTrade] = field(default_factory=list)
    signatures_seen: int = 0
    transactions_fetched: int = 0
    candidate_swaps: int = 0
    validated_swaps: int = 0
    rejected_swaps: int = 0
    coverage_seconds: float | None = None
    error: str = ""


class HeliusLiveTradeProvider(MarketDataProvider):
    """Real-time Solana trades using Helius Standard WebSockets.

    Free Helius accounts do not include ``transactionSubscribe``.  This adapter
    therefore uses the standard ``logsSubscribe`` method (available on Free),
    then fetches the matching transaction over JSON-RPC and derives token/quote
    balance deltas locally.  Paid Developer+ accounts can later be upgraded to
    Enhanced WSS without changing the candle/session API.
    """

    supported_live_timeframes = frozenset(LIVE_TIMEFRAME_SECONDS)

    def __init__(self, api_key: str | None = None):
        self.api_key = (api_key or os.getenv("HELIUS_API_KEY", "")).strip()
        self.http_url = f"https://mainnet.helius-rpc.com/?api-key={self.api_key}" if self.api_key else ""
        self.ws_url = f"wss://mainnet.helius-rpc.com/?api-key={self.api_key}" if self.api_key else ""
        self.enhanced_ws_url = f"wss://atlas-mainnet.helius-rpc.com/?api-key={self.api_key}" if self.api_key else ""
        # Legacy/no-session view only: each live session owns and mutates its own
        # LiveFeedDiagnostics (see subscribe_live_trades); this is never written to
        # by a worker, so two tokens' streams can never bleed into each other here.
        self.diagnostics = LiveFeedDiagnostics()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def get_historical_candles(self, token: str, timeframe: str, limit: int) -> pd.DataFrame:
        del token, timeframe, limit
        raise CandleDataError("Helius live provider does not supply historical OHLCV in this adapter.")

    @staticmethod
    def _ui_amount(balance: dict[str, Any]) -> float:
        ui = balance.get("uiTokenAmount") or {}
        value = ui.get("uiAmount")
        if value is not None:
            return float(value)
        raw = float(ui.get("amount") or 0)
        decimals = int(ui.get("decimals") or 0)
        return raw / (10 ** decimals)

    @staticmethod
    def _raw_amount(balance: dict[str, Any]) -> tuple[float, int]:
        ui = balance.get("uiTokenAmount") or {}
        return float(ui.get("amount") or 0), int(ui.get("decimals") or 0)

    @classmethod
    def _owner_mint_deltas(cls, meta: dict[str, Any]) -> dict[tuple[str, str], float]:
        totals: dict[str, list[float]] = {}
        for side, key in ((0, "preTokenBalances"), (1, "postTokenBalances")):
            for item in meta.get(key) or []:
                mint = str(item.get("mint") or "")
                if not mint:
                    continue
                owner = str(item.get("owner") or item.get("accountIndex") or "")
                slot = totals.setdefault(f"{mint}|{owner}", [0.0, 0.0])
                slot[side] += cls._ui_amount(item)
        return {tuple(key.split("|", 1)): post - pre for key, (pre, post) in totals.items()}

    @classmethod
    def _mint_deltas(cls, meta: dict[str, Any]) -> dict[str, float]:
        """Compatibility summary for callers/tests; parsing uses owner-paired deltas."""
        by_mint: dict[str, float] = {}
        for (mint, _owner), delta in cls._owner_mint_deltas(meta).items():
            if abs(delta) > abs(by_mint.get(mint, 0.0)):
                by_mint[mint] = delta
        return by_mint

    @staticmethod
    def _account_keys(transaction: dict[str, Any]) -> list[str]:
        message = (((transaction or {}).get("transaction") or {}).get("message") or {})
        keys = []
        for item in message.get("accountKeys") or []:
            if isinstance(item, dict):
                keys.append(str(item.get("pubkey") or ""))
            else:
                keys.append(str(item or ""))
        return keys

    @classmethod
    def _target_owner(cls, meta: dict[str, Any], mint: str) -> str | None:
        # Prefer the owner with the largest target-token balance movement; this
        # is normally the trader wallet and lets us pair native SOL movement.
        owners = [(owner, delta) for (balance_mint, owner), delta in cls._owner_mint_deltas(meta).items() if balance_mint == mint and owner]
        return max(owners, key=lambda item: abs(item[1]))[0] if owners else None

    @classmethod
    def _native_sol_delta(cls, transaction: dict[str, Any], meta: dict[str, Any], mint: str, owner: str | None = None) -> float:
        owner = owner or cls._target_owner(meta, mint)
        if not owner:
            return 0.0
        keys = cls._account_keys(transaction)
        try:
            index = keys.index(owner)
        except ValueError:
            return 0.0
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        if index >= len(pre) or index >= len(post):
            return 0.0
        return (float(post[index]) - float(pre[index])) / 1_000_000_000.0

    @classmethod
    def _trade_details(cls, transaction: dict[str, Any], mint: str) -> SwapComponents | None:
        meta = (transaction or {}).get("meta") or {}
        if not meta or meta.get("err") is not None:
            return None
        owner_deltas = cls._owner_mint_deltas(meta)
        candidates = [(owner, delta) for (balance_mint, owner), delta in owner_deltas.items() if balance_mint == mint and delta]
        # Pair token and quote deltas from the same owner. This avoids treating
        # pool-reserve movements as a trader's purchase or sale.
        for owner, token_delta in sorted(candidates, key=lambda item: abs(item[1]), reverse=True):
            token_items = [item for key in ("preTokenBalances", "postTokenBalances") for item in meta.get(key) or [] if str(item.get("mint") or "") == mint and str(item.get("owner") or item.get("accountIndex") or "") == owner]
            token_decimals = max((cls._raw_amount(item)[1] for item in token_items), default=0)
            raw_token_delta = sum(cls._raw_amount(item)[0] for item in meta.get("postTokenBalances") or [] if str(item.get("mint") or "") == mint and str(item.get("owner") or item.get("accountIndex") or "") == owner) - sum(cls._raw_amount(item)[0] for item in meta.get("preTokenBalances") or [] if str(item.get("mint") or "") == mint and str(item.get("owner") or item.get("accountIndex") or "") == owner)
            for quote_mint, currency in ((USDC_MINT, "USD"), (WSOL_MINT, "SOL")):
                quote_delta = owner_deltas.get((quote_mint, owner), 0.0)
                if quote_delta and token_delta * quote_delta < 0:
                    raw_quote_delta = sum(cls._raw_amount(item)[0] for item in meta.get("postTokenBalances") or [] if str(item.get("mint") or "") == quote_mint and str(item.get("owner") or item.get("accountIndex") or "") == owner) - sum(cls._raw_amount(item)[0] for item in meta.get("preTokenBalances") or [] if str(item.get("mint") or "") == quote_mint and str(item.get("owner") or item.get("accountIndex") or "") == owner)
                    return SwapComponents(abs(token_delta), abs(quote_delta), abs(quote_delta / token_delta), currency, "BUY" if token_delta > 0 else "SELL", token_decimals, raw_token_delta, raw_quote_delta)
            native_sol = cls._native_sol_delta(transaction, meta, mint, owner)
            if native_sol and token_delta * native_sol < 0:
                return SwapComponents(abs(token_delta), abs(native_sol), abs(native_sol / token_delta), "SOL", "BUY" if token_delta > 0 else "SELL", token_decimals, raw_token_delta, native_sol * 1_000_000_000)
        return None

    @classmethod
    def _trade_components(cls, transaction: dict[str, Any], mint: str) -> tuple[float, float] | None:
        """Compatibility helper returning normalized token size and quote-per-token."""
        details = cls._trade_details(transaction, mint)
        return (details.token_quantity, details.quote_per_token) if details else None

    def _fetch_transaction(self, signature: str) -> dict[str, Any] | None:
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
            "params": [signature, {"commitment": "confirmed", "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}],
        }
        try:
            response = requests.post(self.http_url, json=payload, timeout=8)
            response.raise_for_status()
            body = response.json()
            result = body.get("result")
            return result if isinstance(result, dict) else None
        except (requests.RequestException, ValueError):
            return None

    def fetch_recent_signatures(self, address: str, limit: int = 100, before: str | None = None) -> list[dict[str, Any]]:
        """Standard Solana RPC getSignaturesForAddress; used only for the best-effort historical backfill."""
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": [address, params]}
        try:
            response = requests.post(self.http_url, json=payload, timeout=8)
            response.raise_for_status()
            body = response.json()
            result = body.get("result")
            return result if isinstance(result, list) else []
        except (requests.RequestException, ValueError):
            return []

    def backfill_recent_trades(
        self,
        mint: str,
        minutes: float = 10.0,
        max_signatures: int = 80,
        max_transactions: int = 120,
        reference_price_usd: float | None = None,
        sol_usd_feed: "SolUsdPriceFeed | None" = None,
    ) -> "HistoricalBackfillResult":
        """Best-effort pre-session history from real trades, not a candle API.

        Uses only standard Solana RPC (getSignaturesForAddress + getTransaction,
        both already covered by the existing Helius key) and reuses the exact
        same parser/validator as the live stream, so backfilled and live
        candles are held to the same trust standard. This is NOT guaranteed to
        cover ``minutes`` of history: getSignaturesForAddress returns the most
        recent signatures for the mint, which for a busy pool may only span a
        few minutes, and for a quiet one may span much longer. A failure here
        must never block the live stream from starting.
        """
        if not self.available:
            return HistoricalBackfillResult(trades=[], error="Helius API key not configured")
        cutoff = time.time() - minutes * 60
        signatures: list[dict[str, Any]] = []
        before: str | None = None
        try:
            while len(signatures) < max_signatures:
                page_limit = min(100, max_signatures - len(signatures))
                page = self.fetch_recent_signatures(mint, limit=page_limit, before=before)
                if not page:
                    break
                signatures.extend(page)
                before = page[-1].get("signature")
                oldest_block_time = page[-1].get("blockTime")
                # A short page (fewer than requested) means the RPC ran out of
                # history; anything else means more pages may still exist.
                if len(page) < page_limit or (oldest_block_time and oldest_block_time < cutoff):
                    break
        except Exception as error:
            return HistoricalBackfillResult(trades=[], signatures_seen=len(signatures), error=type(error).__name__)

        in_window = [item for item in signatures if item.get("blockTime") and item["blockTime"] >= cutoff and not item.get("err")]
        in_window.sort(key=lambda item: item.get("blockTime") or 0)
        # Bounded independently of signature count: prioritize the trades
        # closest to "now" so the live handoff has maximum continuity, and
        # keep the worst-case paced-fetch time predictable regardless of pool activity.
        if max_transactions >= 0 and len(in_window) > max_transactions:
            in_window = in_window[max(len(in_window) - max_transactions, 0):]

        ref_usd = float(reference_price_usd or 0)
        last_validated_price: float | None = None
        trades: list[LiveTrade] = []
        candidate_swaps = transactions_fetched = validated_swaps = rejected_swaps = 0
        last_request = 0.0
        for item in in_window:
            signature = str(item.get("signature") or "")
            if not signature:
                continue
            delay = 0.13 - (time.monotonic() - last_request)
            if delay > 0:
                time.sleep(delay)
            tx = self._fetch_transaction(signature)
            last_request = time.monotonic()
            if not tx:
                continue
            transactions_fetched += 1
            details = self._trade_details(tx, mint)
            if not details:
                continue
            candidate_swaps += 1
            if details.quote_currency == "USD":
                price_usd = details.quote_per_token
            else:
                sol_price, _reason = sol_usd_feed.usable_price() if sol_usd_feed is not None else (None, "missing")
                if sol_price is None:
                    sol_price, _reason = _sol_usd_oracle.usable_price()
                if sol_price is None:
                    rejected_swaps += 1
                    continue
                price_usd = details.quote_per_token * sol_price
            if price_usd <= 0:
                continue
            block_time = tx.get("blockTime") or item.get("blockTime")
            timestamp = datetime.fromtimestamp(float(block_time), tz=timezone.utc) if block_time else datetime.now(timezone.utc)
            trade = LiveTrade(
                timestamp=timestamp, price=float(price_usd), size=float(details.token_quantity), token_mint=mint,
                quote_amount=float(details.quote_quantity), side=details.side, signature=signature, quote_currency=details.quote_currency,
                raw_token_delta=details.raw_token_delta, raw_quote_delta=details.raw_quote_delta, token_decimals=details.token_decimals,
            )
            accepted, _reason = validate_live_trade(trade, ref_usd or None, last_validated_price)
            if not accepted:
                rejected_swaps += 1
                continue
            validated_swaps += 1
            last_validated_price = trade.price
            trades.append(trade)

        coverage_seconds = (trades[-1].timestamp - trades[0].timestamp).total_seconds() if trades else None
        return HistoricalBackfillResult(
            trades=trades, signatures_seen=len(signatures), transactions_fetched=transactions_fetched,
            candidate_swaps=candidate_swaps, validated_swaps=validated_swaps, rejected_swaps=rejected_swaps,
            coverage_seconds=coverage_seconds,
        )

    @staticmethod
    def _extract_enhanced_transaction(value: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """Normalize a transactionSubscribe notification into (signature, tx) shaped like a getTransaction result."""
        if not isinstance(value, dict):
            return None
        signature = str(value.get("signature") or "")
        nested = value.get("transaction")
        if isinstance(nested, dict) and "meta" in nested:
            candidate = nested
        elif "meta" in value:
            candidate = value
        elif isinstance(nested, dict):
            candidate = nested
        else:
            candidate = None
        if not isinstance(candidate, dict):
            return None
        if not signature:
            try:
                signature = str((((candidate.get("transaction") or {}).get("signatures") or [""])[0]))
            except Exception:
                signature = ""
        if not signature:
            return None
        return signature, candidate

    def subscribe_live_trades(
        self,
        token: str,
        on_trade: Callable[[LiveTrade], None],
        reference_price_usd: float | None = None,
        sol_usd_feed: "SolUsdPriceFeed | None" = None,
        reference_price_feed: "SolUsdPriceFeed | None" = None,
        diagnostics: "LiveFeedDiagnostics | None" = None,
        preseed_signatures: "Iterable[str] | None" = None,
    ) -> Callable[[], None]:
        if not self.available:
            raise CandleDataError("Set HELIUS_API_KEY in .env to enable real-time 1s/15s/30s candles.")
        if websocket is None:
            raise CandleDataError("Install websocket-client to enable the Helius live trade stream.")

        mint = str(token or "").strip()
        if not mint:
            raise CandleDataError("A token mint is required for the live trade stream.")

        ref_usd = float(reference_price_usd or 0)
        # Own diagnostics per subscription (normally supplied by the caller's
        # LiveCandleSession) so concurrent tokens never overwrite each other's counters.
        session_diagnostics = diagnostics if diagnostics is not None else LiveFeedDiagnostics()
        session_diagnostics.reference_price = ref_usd or None
        session_diagnostics.stream_mode = "ENHANCED" if self.enhanced_ws_url else "STANDARD"
        worker = _HeliusStreamWorker(self, mint, on_trade, reference_price_usd=ref_usd, sol_usd_feed=sol_usd_feed, reference_price_feed=reference_price_feed, diagnostics=session_diagnostics)
        for signature in preseed_signatures or ():
            worker.register_signature(signature)
        return worker.start()


class _HeliusStreamWorker:
    """Owns one live-trade subscription's mutable state.

    Prefers Helius Enhanced ``transactionSubscribe`` (full transaction delivered
    inline, no ``getTransaction`` round-trip). If it is rejected, unavailable, or
    keeps dropping, this falls back automatically to the standard
    ``logsSubscribe`` -> ``getTransaction`` path. Both paths share the same
    signature dedupe set and the same trade parser/validator so exactly one
    candle engine is ever fed.
    """

    def __init__(
        self,
        provider: "HeliusLiveTradeProvider",
        mint: str,
        on_trade: Callable[[LiveTrade], None],
        reference_price_usd: float = 0.0,
        sol_usd_feed: "SolUsdPriceFeed | None" = None,
        reference_price_feed: "SolUsdPriceFeed | None" = None,
        diagnostics: "LiveFeedDiagnostics | None" = None,
        backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        max_consecutive_enhanced_failures: int = 3,
    ):
        self.provider = provider
        self.mint = mint
        self.on_trade = on_trade
        self.ref_usd = float(reference_price_usd or 0)
        self.sol_usd_feed = sol_usd_feed
        # A continuously-refreshed anchor (falls back to the frozen self.ref_usd
        # snapshot above when absent); see LiveCandleSession.reference_price_feed.
        self.reference_price_feed = reference_price_feed
        # Owned by this worker/session, never the shared provider: two tokens'
        # streams must never be able to overwrite each other's diagnostics.
        self._diagnostics = diagnostics if diagnostics is not None else LiveFeedDiagnostics()
        self.initial_backoff = backoff_seconds
        self.max_backoff = max_backoff_seconds
        self.max_consecutive_enhanced_failures = max_consecutive_enhanced_failures
        self.stop = threading.Event()
        self.ws_holder: dict[str, Any] = {}
        self.signatures: deque[str] = deque(maxlen=500)
        self.signature_set: set[str] = set()
        self.seen_lock = threading.Lock()
        self.work: deque[str] = deque()
        self.work_lock = threading.Lock()

    @property
    def diagnostics(self) -> LiveFeedDiagnostics:
        return self._diagnostics

    def _resolve_reference_price(self) -> float | None:
        """Prefers the live, continuously-refreshed anchor; falls back to the session-start snapshot."""
        if self.reference_price_feed is not None:
            price, _reason = self.reference_price_feed.usable_price()
            if price is not None:
                return price
        return self.ref_usd or None

    def record_trade(self, trade: LiveTrade, accepted: bool, reason: str, bucket: int | None = None) -> None:
        reference_price = self._resolve_reference_price()
        record = {
            "timestamp": trade.timestamp.isoformat(), "signature": trade.signature, "token_mint": trade.token_mint,
            "token_decimals": trade.token_decimals, "raw_token_delta": trade.raw_token_delta,
            "normalized_token_quantity": trade.size, "raw_quote_delta": trade.raw_quote_delta,
            "normalized_quote_quantity": trade.quote_amount, "calculated_price": trade.price,
            "reference_price": reference_price, "previous_validated_price": self.diagnostics.last_validated_price,
            "price_unit": trade.price_unit, "accepted": accepted, "rejection_reason": reason,
            "bucket_timestamp": datetime.fromtimestamp(bucket, tz=timezone.utc).isoformat() if bucket is not None else "",
        }
        target = self.diagnostics.recent_validated if accepted else self.diagnostics.recent_rejected
        target.append(record)
        del target[:-20]

    def register_signature(self, signature: str) -> bool:
        """Cross-mode dedupe: a signature already seen on either stream cannot enter the candle engine twice."""
        if not signature:
            return False
        with self.seen_lock:
            if signature in self.signature_set:
                return False
            self.signatures.append(signature)
            self.signature_set.add(signature)
            while len(self.signature_set) > 450 and self.signatures:
                self.signature_set.discard(self.signatures.popleft())
            return True

    def queue_signature(self, signature: str) -> None:
        if not self.register_signature(signature):
            return
        with self.work_lock:
            self.work.append(signature)
            self.diagnostics.queue_depth = len(self.work)

    def _resolve_sol_usd_price(self) -> tuple[float | None, str]:
        """Returns (price, "") for a fresh trusted SOL/USD quote (oracle first, then this session's own calibration)."""
        status = sol_usd_conversion_status(self.sol_usd_feed)
        if status["sol_usd_available"]:
            return status["sol_usd_price"], ""
        return None, f"sol_usd_{status['sol_usd_status']}"

    def process_transaction(self, tx: dict[str, Any], signature: str) -> None:
        """Shared parser/validator path used by both the enhanced and standard streams."""
        details = self.provider._trade_details(tx, self.mint)
        if not details:
            return
        self.diagnostics.candidate_swaps += 1
        self.diagnostics.reference_price = self._resolve_reference_price()
        token_size, quote_amount, quote_per_token, quote_currency, side = (
            details.token_quantity, details.quote_quantity, details.quote_per_token, details.quote_currency, details.side,
        )
        # USDC quotes are already USD. SOL quotes need a live, freshness-checked
        # SOL/USD price; a frozen or missing rate must reject, never guess.
        if quote_currency == "USD":
            price_usd = quote_per_token
        else:
            sol_price, sol_reason = self._resolve_sol_usd_price()
            if sol_price is None:
                self.diagnostics.record_rejection(sol_reason)
                self.diagnostics.last_rejection = f"{signature[:12]}… no trusted SOL/USD conversion ({sol_reason})"
                return
            price_usd = quote_per_token * sol_price
        if price_usd <= 0:
            return
        block_time = tx.get("blockTime")
        timestamp = datetime.fromtimestamp(float(block_time), tz=timezone.utc) if block_time else datetime.now(timezone.utc)
        trade = LiveTrade(
            timestamp=timestamp, price=float(price_usd), size=float(token_size), token_mint=self.mint,
            quote_amount=float(quote_amount), side=side, signature=signature, quote_currency=quote_currency,
            raw_token_delta=details.raw_token_delta, raw_quote_delta=details.raw_quote_delta, token_decimals=details.token_decimals,
        )
        accepted, reason = validate_live_trade(trade, self._resolve_reference_price(), self.diagnostics.last_validated_price)
        bucket = int(timestamp.timestamp()) // LIVE_TIMEFRAME_SECONDS["1s"] * LIVE_TIMEFRAME_SECONDS["1s"]
        if not accepted:
            self.diagnostics.record_rejection(reason)
            self.diagnostics.last_rejection = f"{signature[:12]}… {reason}; token={trade.raw_token_delta:.8g} quote={trade.raw_quote_delta:.8g} price={trade.price:.8g} ref={(self._resolve_reference_price() or 0):.8g}"
            self.record_trade(trade, False, reason)
            return
        self.diagnostics.validated_swaps += 1
        self.diagnostics.last_validated_price = trade.price
        now_wall = datetime.now(timezone.utc)
        self.diagnostics.last_valid_trade_at = now_wall.isoformat()
        self.diagnostics.feed_lag_ms = max(0.0, (now_wall - timestamp).total_seconds() * 1000.0)
        self.record_trade(trade, True, "", bucket)
        self.on_trade(trade)

    # -- Enhanced (transactionSubscribe) path: full tx parsed inline, no RPC fetch -----------------

    def handle_enhanced_message(self, _ws, raw) -> None:
        self.diagnostics.last_ws_message_at = datetime.now(timezone.utc).isoformat()
        try:
            message = json.loads(raw)
            if message.get("error"):
                self.diagnostics.last_parser_error = f"Enhanced subscription error: {message['error']}"
                return
            if message.get("id") and message.get("result") is not None:
                self.diagnostics.subscription_id = str(message["result"])
                return
            if message.get("method") != "transactionNotification":
                return
            self.diagnostics.notifications_received += 1
            result = (message.get("params") or {}).get("result")
            # Helius documents the transaction payload directly under params.result
            # (see transaction-subscribe docs). Some other Solana WS notifications
            # nest it one level deeper under a "value" key, so accept both shapes.
            value = result.get("value") if isinstance(result, dict) and isinstance(result.get("value"), dict) else result
            extracted = HeliusLiveTradeProvider._extract_enhanced_transaction(value)
            if not extracted:
                self.diagnostics.parse_failures += 1
                self.diagnostics.last_parser_error = "Enhanced notification did not match a known transaction shape"
                return
            signature, tx = extracted
            meta = (tx or {}).get("meta") or {}
            if not signature:
                self.diagnostics.parse_failures += 1
                self.diagnostics.last_parser_error = "Enhanced notification was missing a transaction signature"
                return
            if meta.get("err") is not None:
                return
            if not self.register_signature(signature):
                return
            self.diagnostics.notifications_parsed += 1
            self.process_transaction(tx, signature)
        except Exception as error:
            self.diagnostics.parse_failures += 1
            self.diagnostics.last_parser_error = type(error).__name__

    def run_enhanced(self) -> bool:
        """Runs the enhanced WebSocket connect/reconnect loop. Returns False only on permanent fallback."""
        if websocket is None or not self.provider.enhanced_ws_url:
            return False
        backoff = self.initial_backoff
        consecutive_failures = 0
        while not self.stop.is_set():
            self.diagnostics.subscription_id = ""

            def on_open(ws):
                nonlocal backoff
                backoff = self.initial_backoff
                self.diagnostics.websocket_connected = True
                self.diagnostics.ws_status = "CONNECTED"
                self.diagnostics.stream_mode = "ENHANCED"
                ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe",
                    "params": [
                        {"failed": False, "accountInclude": [self.mint]},
                        {"commitment": "processed", "encoding": "jsonParsed", "transactionDetails": "full", "maxSupportedTransactionVersion": 1},
                    ],
                }))

            def on_error(_ws, error):
                self.diagnostics.last_parser_error = f"Enhanced WebSocket error: {error}"

            def on_close(*_args):
                self.diagnostics.websocket_connected = False

            ws = websocket.WebSocketApp(self.provider.enhanced_ws_url, on_open=on_open, on_message=self.handle_enhanced_message, on_error=on_error, on_close=on_close)
            self.ws_holder["ws"] = ws
            try:
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as error:
                self.diagnostics.last_parser_error = f"Enhanced WebSocket connection error: {error}"
            self.diagnostics.websocket_connected = False
            if self.stop.is_set():
                return True
            if self.diagnostics.subscription_id:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= self.max_consecutive_enhanced_failures:
                    self.diagnostics.last_parser_error = "Enhanced transactionSubscribe unavailable; falling back to standard stream."
                    return False
            self.diagnostics.ws_status = "RECONNECTING"
            self.stop.wait(backoff)
            backoff = min(backoff * 2, self.max_backoff)
        return True

    # -- Standard (logsSubscribe -> getTransaction) fallback path ----------------------------------

    def handle_standard_message(self, _ws, raw) -> None:
        self.diagnostics.last_ws_message_at = datetime.now(timezone.utc).isoformat()
        try:
            message = json.loads(raw)
            # Subscription errors (for example unsupported methods) are not
            # treated as a successful connection.
            if message.get("error"):
                self.diagnostics.last_parser_error = "WebSocket subscription error"
                return
            if message.get("id") and message.get("result") is not None:
                self.diagnostics.subscription_id = str(message["result"])
                return
            value = (((message.get("params") or {}).get("result") or {}).get("value") or {})
            if value.get("err") is not None:
                return
            self.diagnostics.notifications_received += 1
            self.queue_signature(str(value.get("signature") or ""))
        except Exception as error:
            self.diagnostics.last_parser_error = type(error).__name__

    def fetch_worker_loop(self) -> None:
        """Free/standard tier is ~10 RPC requests/sec; this queue only exists for the fallback path."""
        last_request = 0.0
        while not self.stop.is_set():
            signature = None
            with self.work_lock:
                if self.work:
                    signature = self.work.popleft()
                    self.diagnostics.queue_depth = len(self.work)
            if not signature:
                self.stop.wait(0.03)
                continue
            delay = 0.13 - (time.monotonic() - last_request)
            if delay > 0:
                self.stop.wait(delay)
            if self.stop.is_set():
                break
            tx = self.provider._fetch_transaction(signature)
            last_request = time.monotonic()
            if not tx:
                self.diagnostics.last_parser_error = "Transaction fetch returned no result"
                continue
            self.diagnostics.transactions_fetched += 1
            self.process_transaction(tx, signature)

    def run_standard(self) -> None:
        if websocket is None:
            self.diagnostics.last_parser_error = "Install websocket-client to enable the Helius live trade stream."
            return
        backoff = self.initial_backoff
        self.diagnostics.stream_mode = "STANDARD"
        while not self.stop.is_set():
            def on_open(ws):
                nonlocal backoff
                backoff = self.initial_backoff
                self.diagnostics.websocket_connected = True
                self.diagnostics.ws_status = "CONNECTED"
                ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                    "params": [{"mentions": [self.mint]}, {"commitment": "confirmed"}],
                }))

            def on_close(*_args):
                self.diagnostics.websocket_connected = False

            ws = websocket.WebSocketApp(self.provider.ws_url, on_open=on_open, on_message=self.handle_standard_message, on_close=on_close)
            self.ws_holder["ws"] = ws
            try:
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                self.diagnostics.last_parser_error = "WebSocket connection error"
            self.diagnostics.websocket_connected = False
            if not self.stop.is_set():
                self.diagnostics.ws_status = "RECONNECTING"
                self.stop.wait(backoff)
                backoff = min(backoff * 2, self.max_backoff)

    def dispatcher(self) -> None:
        """Prefer the Enhanced WebSocket; fall back to the standard stream automatically and permanently for this session."""
        if self.provider.enhanced_ws_url and websocket is not None:
            stayed_up = self.run_enhanced()
            if self.stop.is_set():
                self.diagnostics.ws_status = "DISCONNECTED"
                return
            if stayed_up is False:
                self.run_standard()
        else:
            self.run_standard()
        self.diagnostics.ws_status = "DISCONNECTED"

    def start(self) -> Callable[[], None]:
        threading.Thread(target=self.fetch_worker_loop, name=f"helius-worker-{self.mint[:8]}", daemon=True).start()
        threading.Thread(target=self.dispatcher, name=f"helius-wss-{self.mint[:8]}", daemon=True).start()

        def unsubscribe():
            self.stop.set()
            ws = self.ws_holder.get("ws")
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        return unsubscribe


class LiveCandleSession:
    """Thread-safe shared trade stream with one local aggregator per chart timeframe."""

    #: Only these timeframes have no other historical source (GeckoTerminal has
    #: no sub-minute OHLCV), so only these are worth backfilling from raw trades.
    BACKFILL_TIMEFRAMES = ("1s", "15s", "30s")

    def __init__(
        self,
        provider: HeliusLiveTradeProvider,
        mint: str,
        reference_price_usd: float | None,
        sol_usd: float | None = None,
        max_candles: int = 600,
        # Signature listing now paginates correctly (see fetch_recent_signatures
        # usage below), so this bound is about depth of history, not blocking
        # time; max_transactions below is what actually bounds the paced
        # getTransaction cost (worst case ~100 * 0.13s = ~13s on first open).
        backfill_minutes: float = 8.0,
        backfill_max_signatures: int = 250,
        backfill_max_transactions: int = 100,
        enable_backfill: bool = True,
    ):
        self.aggregators = {timeframe: LiveCandleAggregator(timeframe) for timeframe in LIVE_TIMEFRAME_SECONDS}
        self.candles = {timeframe: deque(maxlen=max_candles) for timeframe in LIVE_TIMEFRAME_SECONDS}
        self.lock = threading.RLock()
        self.started_at = datetime.now(timezone.utc)
        self.last_trade_at: datetime | None = None
        self.last_candle_update_at: datetime | None = None
        self.recent_trades: deque[LiveTrade] = deque(maxlen=30)
        self.provider = provider
        # Owned by this session alone: switching tokens must never let an older
        # (still-running) session's stream overwrite this token's counters.
        self.diagnostics = LiveFeedDiagnostics()
        self.sol_usd_feed = SolUsdPriceFeed()
        self.update_sol_usd(sol_usd)
        # A continuously-refreshed price anchor (mirrors sol_usd_feed): without
        # this, price validation fell back to a one-time snapshot from session
        # creation, or the previous validated price alone, so a single bad
        # print could drift unchecked into extreme wicks (and matching MCAP
        # wicks, since MCAP is just price * a fixed factor).
        self.reference_price_feed = SolUsdPriceFeed(max_age_seconds=30.0)
        self.update_reference_price(reference_price_usd)

        self.backfill = self._run_backfill(mint, reference_price_usd, backfill_minutes, backfill_max_signatures, backfill_max_transactions) if enable_backfill else HistoricalBackfillResult()

        def on_trade(trade: LiveTrade):
            with self.lock:
                self.last_trade_at = trade.timestamp
                self.last_candle_update_at = datetime.now(timezone.utc)
                self.recent_trades.append(trade)
                for timeframe, aggregator in self.aggregators.items():
                    try:
                        candle, closed = aggregator.apply_trade(trade)
                    except CandleDataError as error:
                        self.diagnostics.record_rejection(str(error))
                        self.diagnostics.last_rejection = f"{trade.signature[:12]}… {error}"
                        return
                    store = self.candles[timeframe]
                    if not store or closed:
                        # Empty periods remain absent: every stored OHLC derives from trades.
                        store.append(candle)
                    else:
                        store[-1] = candle

        preseed = [trade.signature for trade in self.backfill.trades]
        self.unsubscribe = provider.subscribe_live_trades(mint, on_trade, reference_price_usd=reference_price_usd, sol_usd_feed=self.sol_usd_feed, reference_price_feed=self.reference_price_feed, diagnostics=self.diagnostics, preseed_signatures=preseed)

    def _run_backfill(self, mint: str, reference_price_usd: float | None, minutes: float, max_signatures: int, max_transactions: int) -> "HistoricalBackfillResult":
        """A failure here must never block the live stream from starting."""
        try:
            result = self.provider.backfill_recent_trades(mint, minutes=minutes, max_signatures=max_signatures, max_transactions=max_transactions, reference_price_usd=reference_price_usd, sol_usd_feed=self.sol_usd_feed)
        except Exception as error:
            return HistoricalBackfillResult(error=type(error).__name__)
        if not result.trades:
            return result
        for timeframe in self.BACKFILL_TIMEFRAMES:
            historical_candles = aggregate_trades_into_candles(result.trades, timeframe)
            if not historical_candles:
                continue
            self.candles[timeframe].extend(historical_candles)
            # Resume from the last historical bar so a live trade in the same
            # bucket updates it in place instead of creating a duplicate.
            self.aggregators[timeframe].current = dict(historical_candles[-1])
        return result

    def update_sol_usd(self, price: float | None, source: str = "scanner-pair-ratio") -> None:
        """Called on every dashboard refresh tick so the SOL/USD rate never goes stale mid-session."""
        self.sol_usd_feed.update(price, source=source)

    def update_reference_price(self, price: float | None, source: str = "scanner-quote") -> None:
        """Called on every dashboard refresh tick so the price-sanity anchor never goes stale mid-session."""
        self.reference_price_feed.update(price, source=source)

    def frame(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in self.candles:
            raise CandleDataError(f"Unsupported live timeframe: {timeframe}")
        with self.lock:
            rows = list(self.candles[timeframe])
        if not rows:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

    def append_historical(self, _candles: pd.DataFrame) -> None:
        """Live sessions deliberately never reinterpret minute history as sub-minute candles."""
        raise CandleDataError("Historical OHLCV cannot be inserted into a live trade candle store.")

    def chart_diagnostics(self) -> dict[str, Any]:
        """Candle/chart-side health: separate from the provider's stream/websocket diagnostics."""
        with self.lock:
            now = datetime.now(timezone.utc)
            chart_age_ms = (now - self.last_candle_update_at).total_seconds() * 1000.0 if self.last_candle_update_at else None
            return {
                "last_trade_at": self.last_trade_at.isoformat() if self.last_trade_at else "",
                "last_candle_update_at": self.last_candle_update_at.isoformat() if self.last_candle_update_at else "",
                "chart_age_ms": chart_age_ms,
                "backfill_trades": len(self.backfill.trades),
                "backfill_coverage_seconds": self.backfill.coverage_seconds,
                "backfill_signatures_seen": self.backfill.signatures_seen,
                "backfill_error": self.backfill.error,
                **sol_usd_conversion_status(self.sol_usd_feed),
            }

    def diagnostics_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {**self.diagnostics.__dict__, **self.chart_diagnostics(), "recent_trades": list(self.recent_trades)}


_live_provider = HeliusLiveTradeProvider()
_live_sessions: dict[str, LiveCandleSession] = {}
_live_sessions_lock = threading.Lock()

def live_provider_available() -> bool:
    return _live_provider.available


def get_live_feed_diagnostics(mint: str | None = None) -> dict[str, Any]:
    """Return this token's own listener health for the developer panel; never another token's."""
    if mint:
        with _live_sessions_lock:
            session = _live_sessions.get(str(mint))
        if session is not None:
            return {**session.diagnostics.__dict__, **session.chart_diagnostics()}
    # No active session for this mint yet: a neutral, unmutated default view.
    return dict(_live_provider.diagnostics.__dict__)


def get_live_candle_session(mint: str, timeframe: str | None = None, reference_price_usd: float | None = None, sol_usd: float | None = None) -> LiveCandleSession:
    del timeframe
    key = str(mint)
    with _live_sessions_lock:
        session = _live_sessions.get(key)
        if session is None:
            session = LiveCandleSession(_live_provider, key, reference_price_usd, sol_usd)
            _live_sessions[key] = session
        else:
            # Refresh on every call (the dashboard reruns this every ~1s); otherwise
            # the SOL/USD rate and price-sanity anchor freeze at whatever they
            # were when the session was first created.
            session.update_sol_usd(sol_usd)
            session.update_reference_price(reference_price_usd)
    return session


_historical_provider: MarketDataProvider = GeckoTerminalHistoricalProvider()


def get_market_data_provider() -> MarketDataProvider:
    """Return the configured historical provider; swap here when a real feed is chosen."""
    return _historical_provider


class HistoricalCandleCache:
    """Process-wide historical OHLCV cache shared across every token, timeframe, and dashboard rerun.

    GeckoTerminal's public API is rate-limited per process (~10 calls/min
    total), not per pool or timeframe. A 429 on any request therefore pauses
    every other historical fetch for a bounded cooldown instead of only
    remembering the one pool+timeframe that failed, which previously let
    switching tokens or timeframes fire a fresh retry storm into the same
    limit. Concurrent callers for the exact same key share one in-flight
    request instead of racing the network.
    """

    def __init__(self, ttls: dict[str, float], backoff_seconds: float = 60.0):
        self.ttls = ttls
        self.backoff_seconds = backoff_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[pd.DataFrame, float]] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_error: dict[str, CandleDataError] = {}
        self._provider_retry_after: float = 0.0

    @staticmethod
    def _cache_key(pool_address: str, timeframe: str) -> str:
        return f"{pool_address}:{timeframe}"

    def rate_limited_seconds_remaining(self) -> float:
        return max(0.0, self._provider_retry_after - time.monotonic())

    def get(self, provider: MarketDataProvider, pool_address: str, timeframe: str, limit: int = 240) -> tuple[pd.DataFrame, bool]:
        key = self._cache_key(pool_address, timeframe)
        ttl = self.ttls.get(timeframe, 30.0)

        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and time.monotonic() - cached[1] < ttl:
                return cached[0].copy(), False
            if time.monotonic() < self._provider_retry_after:
                if cached is not None:
                    return cached[0].copy(), True
                raise CandleDataError("Candle data is delayed after a rate-limited request. Please wait before retrying.")
            wait_event = self._inflight.get(key)
            am_leader = wait_event is None
            if am_leader:
                wait_event = threading.Event()
                self._inflight[key] = wait_event

        if not am_leader:
            wait_event.wait(timeout=15)
            with self._lock:
                cached = self._entries.get(key)
                error = self._inflight_error.pop(key, None)
            if cached is not None and time.monotonic() - cached[1] < ttl:
                return cached[0].copy(), False
            if error is not None:
                raise error
            # The leader's result already aged out or vanished (rare race): fetch it ourselves.
            with self._lock:
                wait_event = threading.Event()
                self._inflight[key] = wait_event

        try:
            candles = provider.get_historical_candles(pool_address, timeframe=timeframe, limit=limit)
        except CandleDataError as error:
            with self._lock:
                if "429" in str(error):
                    self._provider_retry_after = time.monotonic() + self.backoff_seconds
                stale = self._entries.get(key)
                self._inflight_error[key] = error
                self._inflight.pop(key, None)
                wait_event.set()
            if stale is not None and "429" in str(error):
                return stale[0].copy(), True
            raise
        with self._lock:
            self._entries[key] = (candles.copy(), time.monotonic())
            self._inflight.pop(key, None)
            wait_event.set()
        return candles, False


_historical_cache = HistoricalCandleCache(ttls={"1m": 30.0, "5m": 60.0})


def get_cached_historical_candles(pool_address: str, timeframe: str, limit: int = 240) -> tuple[pd.DataFrame, bool]:
    """Shared, deduplicated, rate-limit-aware historical OHLCV fetch used by the dashboard."""
    return _historical_cache.get(get_market_data_provider(), pool_address, timeframe, limit=limit)


def historical_fetch_rate_limited_seconds() -> float:
    """Seconds remaining before the shared historical cache will retry GeckoTerminal again."""
    return _historical_cache.rate_limited_seconds_remaining()


@dataclass(frozen=True)
class TechnicalSetup:
    verdict: str
    confidence: int
    entry_low: float
    entry_high: float
    stop: float
    target_1: float
    target_2: float
    trend: str
    momentum: str
    volume_state: str
    reasons: list[str]
    indicator_votes: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_indicators(candles: pd.DataFrame) -> pd.DataFrame:
    """Calculate reusable local indicators from already-loaded OHLCV candles."""
    frame = candles.copy().reset_index(drop=True)
    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    volume = frame["volume"].astype(float)
    typical = (high + low + close) / 3
    frame["ema9"] = close.ewm(span=9, adjust=False).mean()
    frame["ema21"] = close.ewm(span=21, adjust=False).mean()
    frame["sma20"] = close.rolling(20, min_periods=20).mean()
    frame["hull20"] = (2 * close.rolling(10, min_periods=10).mean() - close.rolling(20, min_periods=20).mean()).rolling(4, min_periods=4).mean()
    frame["macd"] = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    frame["macd_signal"] = frame["macd"].ewm(span=9, adjust=False).mean()
    delta = close.diff()
    gains = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    losses = -delta.clip(upper=0).rolling(14, min_periods=14).mean()
    relative_strength = gains / losses.replace(0, float("nan"))
    frame["rsi14"] = (100 - 100 / (1 + relative_strength)).where(losses != 0, 100.0)
    rsi_low = frame["rsi14"].rolling(14, min_periods=14).min()
    rsi_high = frame["rsi14"].rolling(14, min_periods=14).max()
    frame["stoch_rsi"] = (frame["rsi14"] - rsi_low) / (rsi_high - rsi_low).replace(0, float("nan")) * 100
    frame["stoch_k"] = (close - low.rolling(14, min_periods=14).min()) / (high.rolling(14, min_periods=14).max() - low.rolling(14, min_periods=14).min()).replace(0, float("nan")) * 100
    frame["roc10"] = close.pct_change(10) * 100
    frame["momentum10"] = close - close.shift(10)
    frame["williams_r"] = -100 * (high.rolling(14, min_periods=14).max() - close) / (high.rolling(14, min_periods=14).max() - low.rolling(14, min_periods=14).min()).replace(0, float("nan"))
    true_range = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    frame["atr14"] = true_range.rolling(14, min_periods=14).mean()
    frame["volume_ma20"] = volume.rolling(20, min_periods=5).mean()
    frame["vwap"] = (typical * volume).cumsum() / volume.cumsum().replace(0, float("nan"))
    direction = close.diff().fillna(0).apply(lambda value: 1 if value > 0 else -1 if value < 0 else 0)
    frame["obv"] = (direction * volume).cumsum()
    money_ratio = typical * volume
    positive_flow = money_ratio.where(typical.diff() > 0, 0).rolling(14, min_periods=14).sum()
    negative_flow = money_ratio.where(typical.diff() < 0, 0).rolling(14, min_periods=14).sum().abs()
    frame["mfi14"] = 100 - 100 / (1 + positive_flow / negative_flow.replace(0, float("nan")))
    multiplier = ((close - low) - (high - close)) / (high - low).replace(0, float("nan"))
    frame["cmf20"] = (multiplier.fillna(0) * volume).rolling(20, min_periods=20).sum() / volume.rolling(20, min_periods=20).sum().replace(0, float("nan"))
    frame["ad_line"] = (multiplier.fillna(0) * volume).cumsum()
    frame["bb_mid"] = close.rolling(20, min_periods=20).mean()
    bb_std = close.rolling(20, min_periods=20).std()
    frame["bb_upper"] = frame["bb_mid"] + 2 * bb_std
    frame["bb_lower"] = frame["bb_mid"] - 2 * bb_std
    frame["bb_width"] = (frame["bb_upper"] - frame["bb_lower"]) / frame["bb_mid"].replace(0, float("nan"))
    frame["historical_volatility"] = close.pct_change().rolling(20, min_periods=20).std() * (20 ** 0.5)
    frame["supertrend"] = (high + low) / 2 - 2 * frame["atr14"].fillna(0)
    return frame


def category_states(frame: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
    """Return one state per purpose category; correlated studies never become separate votes."""
    last = frame.iloc[-1]
    close = float(last["close"])
    trend_bull = close > float(last["ema9"]) > float(last["ema21"]) and float(last["macd"]) > float(last["macd_signal"])
    trend_bear = close < float(last["ema9"]) < float(last["ema21"]) and float(last["macd"]) < float(last["macd_signal"])
    momentum_bull = float(last["rsi14"]) >= 52 and float(last["roc10"]) > 0 and float(last["stoch_k"]) >= 50
    momentum_bear = float(last["rsi14"]) <= 48 and float(last["roc10"]) < 0 and float(last["stoch_k"]) <= 50
    volume_ratio = float(last["volume"] / last["volume_ma20"]) if float(last["volume_ma20"] or 0) else 1.0
    volume_state = "STRONG" if volume_ratio >= 1.25 and float(last["cmf20"]) > 0 else "WEAK" if volume_ratio < 0.8 or float(last["cmf20"]) < 0 else "NORMAL"
    volatility = "EXPANDING" if float(last["bb_width"]) > float(frame["bb_width"].tail(20).median()) else "CONTRACTING" if float(last["bb_width"]) < float(frame["bb_width"].tail(20).median()) * 0.8 else "NORMAL"
    recent = frame.tail(20)
    support = float(recent["low"].min())
    resistance = float(recent["high"].max())
    previous_high = float(frame["high"].iloc[-11:-1].max())
    previous_low = float(frame["low"].iloc[-11:-1].min())
    structure = "BULLISH" if close > previous_high or (close > support and float(last["ema9"]) > float(last["ema21"])) else "BEARISH" if close < previous_low else "NEUTRAL"
    states = {"trend": "BULLISH" if trend_bull else "BEARISH" if trend_bear else "NEUTRAL", "momentum": "BULLISH" if momentum_bull else "BEARISH" if momentum_bear else "NEUTRAL", "volume_confirmation": volume_state, "volatility": volatility, "structure": structure}
    reasons = [f"Trend: {states['trend'].lower()} (EMA/MACD)", f"Momentum: {states['momentum'].lower()} (RSI {float(last['rsi14']):.0f}, ROC {float(last['roc10']):+.1f}%)", f"Volume: {volume_state.lower()} ({volume_ratio:.1f}x, CMF {float(last['cmf20']):+.2f})", f"Volatility: {volatility.lower()}", f"Structure: {structure.lower()} (support {support:.6g}, resistance {resistance:.6g})"]
    return states, reasons


def fetch_pool_ohlcv(
    pool_address: str,
    timeframe: str = "1m",
    limit: int = 240,
    timeout: int = 15,
) -> pd.DataFrame:
    """Compatibility helper for existing callers; new code should use MarketDataProvider."""
    del timeout
    return get_market_data_provider().get_historical_candles(pool_address, timeframe, limit)


def analyze_candles(candles: pd.DataFrame) -> TechnicalSetup:
    """Classify a fast trade from price structure, momentum, and volume."""
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(candles.columns) or len(candles) < 20:
        raise CandleDataError("Falcon needs at least 20 complete OHLCV candles.")

    frame = candles.copy().reset_index(drop=True)
    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float)
    frame["ema9"] = close.ewm(span=9, adjust=False).mean()
    frame["ema21"] = close.ewm(span=21, adjust=False).mean()
    frame["volume_ma20"] = volume.rolling(20, min_periods=5).mean()
    frame["range"] = frame["high"].astype(float) - frame["low"].astype(float)
    frame["atr14"] = frame["range"].rolling(14, min_periods=5).mean()

    # Falcon's internal indicator panel. These values drive the simple verdict;
    # they are intentionally not plotted as extra chart panels.
    frame["macd"] = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    frame["macd_signal"] = frame["macd"].ewm(span=9, adjust=False).mean()
    median_price = (frame["high"].astype(float) + frame["low"].astype(float)) / 2
    frame["ao"] = median_price.rolling(5, min_periods=3).mean() - median_price.rolling(34, min_periods=10).mean()
    frame["accelerator"] = frame["ao"] - frame["ao"].rolling(5, min_periods=3).mean()
    frame["efi"] = (close.diff() * volume).ewm(span=13, adjust=False).mean()
    frame["envelope_mid"] = close.rolling(20, min_periods=5).mean()
    envelope_width = max(float(close.pct_change().rolling(20, min_periods=5).std().iloc[-1] or 0) * 2, 0.012)
    frame["envelope_upper"] = frame["envelope_mid"] * (1 + envelope_width)
    frame["envelope_lower"] = frame["envelope_mid"] * (1 - envelope_width)

    last = frame.iloc[-1]
    current = float(last["close"])
    ema9 = float(last["ema9"])
    ema21 = float(last["ema21"])
    atr = max(float(last["atr14"]), current * 0.006)
    vol_average = float(last["volume_ma20"] or 0)
    vol_ratio = float(last["volume"] / vol_average) if vol_average > 0 else 1.0
    change_3 = (current / float(close.iloc[-4]) - 1.0) * 100 if len(frame) >= 4 else 0.0
    recent_high = float(frame["high"].iloc[-11:-1].max()) if len(frame) > 10 else float(frame["high"].max())
    breakout = current > recent_high

    # Fixed-range volume profile approximation using candle typical price.
    typical = (frame["high"].astype(float) + frame["low"].astype(float) + close) / 3
    price_span = float(typical.max() - typical.min())
    if price_span > 0:
        bins = pd.cut(typical, bins=min(24, max(8, len(frame) // 8)), duplicates="drop")
        profile = volume.groupby(bins, observed=True).sum()
        poc_interval = profile.idxmax()
        poc = float((poc_interval.left + poc_interval.right) / 2)
    else:
        poc = current

    bullish_trend = current > ema9 > ema21
    bearish_trend = current < ema9 < ema21
    momentum_up = change_3 >= 1.0
    momentum_down = change_3 <= -1.0
    strong_volume = vol_ratio >= 1.35

    votes = {
        "EMA Cross": "BULL" if ema9 > ema21 else "BEAR",
        "MACD": "BULL" if float(last["macd"]) > float(last["macd_signal"]) else "BEAR",
        "Accelerator": "BULL" if float(last["accelerator"]) > 0 else "BEAR",
        "Elder Force Index": "BULL" if float(last["efi"]) > 0 else "BEAR",
        "Envelopes": "BULL" if current > float(last["envelope_mid"]) else "BEAR",
        "Volume Profile": "BULL" if current >= poc else "BEAR",
    }
    bull_votes = sum(value == "BULL" for value in votes.values())
    bear_votes = len(votes) - bull_votes

    reasons: list[str] = []
    score = 50
    if bullish_trend:
        score += 14
        reasons.append("Price is above rising 9/21 EMAs")
    elif bearish_trend:
        score -= 18
        reasons.append("Price is below falling 9/21 EMAs")
    else:
        reasons.append("Trend is mixed around the 9/21 EMAs")
    if momentum_up:
        score += 12
        reasons.append(f"Three-candle momentum is +{change_3:.1f}%")
    elif momentum_down:
        score -= 14
        reasons.append(f"Three-candle momentum is {change_3:.1f}%")
    if strong_volume:
        score += 10
        reasons.append(f"Volume is {vol_ratio:.1f}x its 20-candle average")
    else:
        reasons.append(f"Volume is {vol_ratio:.1f}x its 20-candle average")
    if breakout:
        score += 10
        reasons.append("Price broke the prior 10-candle high")
    if bull_votes >= 4:
        score += 8
        reasons.append(f"Indicator consensus is bullish ({bull_votes}/{len(votes)})")
    elif bear_votes >= 4:
        score -= 8
        reasons.append(f"Indicator consensus is bearish ({bear_votes}/{len(votes)})")
    reasons.append(f"Fixed-range volume POC is {poc:.8g}")

    score = max(1, min(99, round(score)))
    if bearish_trend and momentum_down and bear_votes >= 4:
        verdict = "SELL"
    elif bullish_trend and momentum_up and strong_volume and bull_votes >= 4:
        verdict = "MOMENTUM BUY"
    elif bullish_trend and (breakout or change_3 > 0) and bull_votes >= 4:
        verdict = "CONFIRMED BUY"
    else:
        verdict = "WAIT"

    # Levels are volatility-aware and always ordered. They are decision support,
    # not promises: the stop is the point where this specific setup is invalid.
    entry_low = min(current, ema9)
    entry_high = max(current, ema9)
    stop = min(entry_low - 1.2 * atr, float(frame["low"].iloc[-8:].min()) - 0.15 * atr)
    risk = max(entry_high - stop, atr)
    target_1 = entry_high + 1.5 * risk
    target_2 = entry_high + 2.5 * risk

    # Mature histories receive one confirmation per category. Shorter histories
    # intentionally retain the established decision behavior above.
    if len(frame) >= 60:
        indicator_frame = calculate_indicators(candles)
        states, category_reasons = category_states(indicator_frame)
        votes.update({f"{name.title()} State": value for name, value in states.items()})
        reasons.extend(category_reasons)
        bullish_categories = sum(states[name] == "BULLISH" for name in ("trend", "momentum", "structure"))
        bearish_categories = sum(states[name] == "BEARISH" for name in ("trend", "momentum", "structure"))
        volume_confirmed = states["volume_confirmation"] == "STRONG"
        # Category confirmation deliberately avoids requiring a volume spike for every buy.
        # A clean trend + momentum + structure alignment is enough for a confirmed setup;
        # strong volume upgrades it to a momentum setup. This prevents quiet but valid
        # reversals/retests from being filtered out while still keeping categories independent.
        if bullish_categories >= 3 and states["structure"] == "BULLISH":
            verdict = "MOMENTUM BUY" if volume_confirmed and states["volatility"] == "EXPANDING" else "CONFIRMED BUY"
        elif bearish_categories >= 2 and states["structure"] == "BEARISH":
            verdict = "SELL"
        else:
            verdict = "WAIT"
        score = max(1, min(99, score + (bullish_categories - bearish_categories) * 4 + (5 if volume_confirmed else 0)))

    return TechnicalSetup(
        verdict=verdict,
        confidence=score,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=max(stop, 0.0),
        target_1=target_1,
        target_2=target_2,
        trend="BULLISH" if bullish_trend else "BEARISH" if bearish_trend else "MIXED",
        momentum="RISING" if momentum_up else "FALLING" if momentum_down else "NEUTRAL",
        volume_state="EXPANDING" if strong_volume else "NORMAL",
        reasons=reasons,
        indicator_votes=votes,
    )


def build_signal_markers(candles: pd.DataFrame, warmup: int = 35, cooldown_bars: int = 3) -> list[dict[str, Any]]:
    """Build sparse trade-lifecycle markers instead of repeating condition labels.

    BUY is emitted only when a fresh buy setup appears while flat. SELL is an exit
    marker, so it is emitted only after a prior BUY marker. WAIT rearms neither side.
    A small bar cooldown also prevents rapid flip-flop markers in choppy candles.
    """
    markers: list[dict[str, Any]] = []
    in_position = False
    previous_verdict = "WAIT"
    last_marker_end = -10_000

    for end in range(max(20, warmup), len(candles) + 1):
        window = candles.iloc[:end]
        try:
            verdict = analyze_candles(window).verdict
        except CandleDataError:
            continue

        is_buy = verdict in {"MOMENTUM BUY", "CONFIRMED BUY"}
        is_sell = verdict == "SELL"
        fresh_buy = is_buy and previous_verdict not in {"MOMENTUM BUY", "CONFIRMED BUY"}
        fresh_sell = is_sell and previous_verdict != "SELL"
        cooled_down = end - last_marker_end >= max(1, int(cooldown_bars))

        emit_verdict: str | None = None
        if not in_position and fresh_buy and cooled_down:
            emit_verdict = verdict
            in_position = True
        elif in_position and fresh_sell and cooled_down:
            emit_verdict = "SELL"
            in_position = False

        if emit_verdict:
            row = window.iloc[-1]
            markers.append({
                "timestamp": row.get("timestamp"),
                "price": float(row["close"]),
                "verdict": emit_verdict,
            })
            last_marker_end = end

        previous_verdict = verdict

    return markers[-12:]
