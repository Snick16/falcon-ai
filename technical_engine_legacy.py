"""Falcon Technical Intelligence Engine.

Fetches real pool OHLCV candles and turns them into an explainable trade setup.
This module intentionally contains no Streamlit code so it can be tested alone.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd
import requests


GECKO_TERMINAL_API = "https://api.geckoterminal.com/api/v2"
SUPPORTED_TIMEFRAMES = {"1m": 1, "5m": 5}


class CandleDataError(RuntimeError):
    """Raised when a pool cannot supply usable candles."""


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


def fetch_pool_ohlcv(
    pool_address: str,
    timeframe: str = "1m",
    limit: int = 240,
    timeout: int = 15,
) -> pd.DataFrame:
    """Return ascending UTC OHLCV candles for a Solana pool."""
    pool_address = str(pool_address or "").strip()
    if not pool_address:
        raise CandleDataError("This token does not include a pool address.")
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise CandleDataError(f"Unsupported timeframe: {timeframe}")

    aggregate = SUPPORTED_TIMEFRAMES[timeframe]
    url = f"{GECKO_TERMINAL_API}/networks/solana/pools/{pool_address}/ohlcv/minute"
    params = {
        "aggregate": aggregate,
        "limit": max(30, min(int(limit), 1000)),
        "currency": "usd",
        "token": "base",
    }
    try:
        response = requests.get(
            url,
            params=params,
            headers={"Accept": "application/json", "User-Agent": "Falcon-AI-Hunter/1.0"},
            timeout=timeout,
        )
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


def build_signal_markers(candles: pd.DataFrame, warmup: int = 35) -> list[dict[str, Any]]:
    """Return only verdict transitions so the live chart stays uncluttered."""
    markers: list[dict[str, Any]] = []
    previous = "WAIT"
    for end in range(max(20, warmup), len(candles) + 1):
        window = candles.iloc[:end]
        try:
            verdict = analyze_candles(window).verdict
        except CandleDataError:
            continue
        if verdict != previous and verdict in {"MOMENTUM BUY", "CONFIRMED BUY", "SELL"}:
            row = window.iloc[-1]
            markers.append({"timestamp": row.get("timestamp"), "price": float(row["close"]), "verdict": verdict})
        previous = verdict
    return markers[-12:]
