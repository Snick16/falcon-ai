import html
import json
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from Scanner import scan_tokens
from technical_engine import (
    CandleDataError, analyze_candles, build_signal_markers, calculate_indicators,
    candles_to_market_cap, get_cached_historical_candles, get_live_candle_session, get_live_feed_diagnostics,
    historical_fetch_rate_limited_seconds, live_provider_available, merge_historical_and_live_candles,
)


st.set_page_config(page_title="Trade Scanner", layout="wide", initial_sidebar_state="collapsed")

TRACKED_TRADER_TOTAL = None
ALERT_WINDOW_MINUTES = 10


def _surge_field_key(name):
    return f"falcon_surge_{name}"


def _surge_pending_values_key():
    return "_falcon_surge_pending_values"


def _surge_notice_key():
    return "_falcon_surge_notice"


def _stage_surge_form_values(settings, notice_message=""):
    """Legacy state helper retained for existing settings tests and future settings UI."""
    st.session_state[_surge_pending_values_key()] = dict(settings)
    if notice_message:
        st.session_state[_surge_notice_key()] = str(notice_message)


def _consume_staged_surge_form_values():
    pending = st.session_state.pop(_surge_pending_values_key(), None)
    if not isinstance(pending, dict):
        return
    for key, value in pending.items():
        st.session_state[_surge_field_key(key)] = value


def inject_css():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500;600&family=Manrope:wght@400;500;600;700;800&display=swap');
        :root { --bg:#02070d; --panel:#06111d; --panel2:#081827; --line:#0b4f7a; --blue:#16b8ff; --blue2:#55d7ff; --pink:#ff2d78; --red:#ff416c; --text:#eef8ff; --muted:#7fa7bf; --green:#18e69b; --amber:#ffd166; }
        .stApp { background:radial-gradient(circle at 50% -15%,#08233a 0,#02070d 35%,#01050a 100%); color:var(--text); font-family:Manrope,sans-serif; }
        [data-testid="stHeader"], #MainMenu, footer { display:none; }
        .block-container { max-width:1540px; padding:.65rem 1rem 3rem; }
        .falcon-header { display:flex;align-items:center;justify-content:space-between;gap:1rem;border-bottom:1px solid #0b4f7a;padding:.35rem .2rem .75rem;margin-bottom:.65rem; }
        .falcon-brand { font-size:1.8rem;font-weight:800;letter-spacing:.09em;color:#56d9ff;text-shadow:0 0 18px #008cff88;line-height:1; }
        .falcon-sub { color:#16b8ff;font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.32em;margin-top:.3rem; }
        .live-pill { color:#18e69b;font:600 .72rem 'DM Mono',monospace;border:1px solid #12664d;background:#06231c;border-radius:999px;padding:.35rem .65rem; }
        .section-heading { margin:1rem 0 .45rem;font-size:1.05rem;font-weight:800;color:#eaf8ff; }
        .eyebrow { color:var(--muted);font:500 .67rem 'DM Mono',monospace;letter-spacing:.07em;text-transform:uppercase; }
        .summary-grid { display:grid;grid-template-columns:repeat(4,1fr);gap:.55rem;margin:.55rem 0 .8rem; }
        .summary-card { background:linear-gradient(180deg,#071827,#04101b);border:1px solid #0b5f91;border-radius:9px;padding:.72rem .85rem;box-shadow:inset 0 0 22px #009cff0a; }
        .summary-value { color:#56d9ff;font-size:1.28rem;font-weight:800; }.summary-label { color:#83a9bf;font:500 .62rem 'DM Mono',monospace;text-transform:uppercase;letter-spacing:.06em;margin-top:.12rem; }
        .hot-card { border-color:#ff2d78;box-shadow:0 0 16px #ff2d7833,inset 0 0 18px #ff2d7810; }.hot-card .summary-value{color:#ff4f91;}
        .decision { border:1px solid var(--line);border-left:4px solid var(--amber);background:var(--panel);padding:1rem;margin:.75rem 0; }.decision.buy{border-left-color:var(--blue)}.decision.sell{border-left-color:var(--pink)}
        .alert-card,.source-card { border:1px solid #0b4f7a;border-radius:9px;background:linear-gradient(180deg,#071522,#04101a);padding:.9rem;margin-bottom:.6rem; }
        .alert-card:hover { border-color:#16b8ff;box-shadow:0 0 16px #009cff22; }
        .alert-top{display:flex;justify-content:space-between;gap:1rem}.token-name{font-size:1.05rem;font-weight:800}.token-symbol,.contract{color:var(--muted);font:500 .68rem 'DM Mono',monospace}.contract{overflow-wrap:anywhere;margin:.55rem 0}.consensus{color:#ff4f91;font:600 .72rem 'DM Mono',monospace;text-align:right}
        .metric-grid{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid #0a3550;margin-top:.55rem}.metric{padding:.5rem .3rem}.metric-key{color:var(--muted);font:500 .59rem 'DM Mono',monospace;text-transform:uppercase}.metric-value{font-size:.83rem;font-weight:700;margin-top:.12rem}
        .source-name{font-weight:800}.source-status{color:#55d7ff;font:500 .67rem 'DM Mono',monospace;margin:.3rem 0}.source-copy{color:var(--muted);font-size:.75rem;line-height:1.4}.detail-row{padding:.6rem 0;border-bottom:1px solid #0a3550}.detail-label{color:var(--muted);font:500 .65rem 'DM Mono',monospace}.detail-value{font-size:.83rem;margin-top:.12rem}
        div[data-testid="stRadio"] > div { gap:.3rem; } div[data-testid="stRadio"] label { background:#06131f;border:1px solid #0b4f7a;border-radius:6px;padding:.34rem .6rem; }
        div[data-testid="stButton"] button,div[data-testid="stLinkButton"] a { border-radius:6px;border:1px solid #087bb8;background:#061a29;color:#dff6ff;font-weight:700;box-shadow:inset 0 0 12px #009cff12; }
        div[data-testid="stButton"] button:hover,div[data-testid="stLinkButton"] a:hover { border-color:#29c8ff;color:#55d7ff;box-shadow:0 0 14px #009cff35; }
        [data-testid="stMetric"]{background:#06131f;border:1px solid #0b4f7a;padding:.6rem;border-radius:8px}
        div[data-baseweb="select"] > div { background:#06131f;border-color:#0b4f7a; }
        [data-testid="stAlert"] { background:#071827;border:1px solid #0b5f91;color:#cdeeff;border-radius:8px; }
        @media(max-width:800px){.block-container{padding:.55rem .55rem 2rem}.summary-grid{grid-template-columns:repeat(2,1fr)}.falcon-brand{font-size:1.35rem}.alert-top{flex-direction:column}.consensus{text-align:left}}
        </style>
        """, unsafe_allow_html=True)


def safe_float(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def fmt_money(value):
    value = safe_float(value)
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.0f}"


def fmt_price(value):
    value = safe_float(value, None)
    if value is None or value <= 0:
        return "N/A"
    if value >= 1:
        return f"${value:,.4f}".rstrip("0").rstrip(".")
    return f"${value:.8f}".rstrip("0").rstrip(".")


def token_contract(token):
    return str(token.get("contract_address", "") or "").strip()


def token_pool(token):
    direct = str(token.get("source_pair_address", "") or "").strip()
    if direct:
        return direct
    pair = ((token.get("raw_data") or {}).get("pair") or {})
    return str(pair.get("pairAddress", "") or "").strip() if isinstance(pair, dict) else ""


def token_price(token):
    pair = ((token.get("raw_data") or {}).get("pair") or {})
    if isinstance(pair, dict):
        value = pair.get("priceUsd") or pair.get("price_usd") or pair.get("price")
        if value:
            return value
    # Scanner.py stores the current quote indirectly; for the streamlined
    # dashboard, market-cap conversion can still be calibrated from the live
    # reference price supplied to Helius.
    return token.get("price_usd")


def token_sol_usd(token):
    """Calibrate SOL/USD only from the scanner's paired USD and native SOL quotes."""
    pair = ((token.get("raw_data") or {}).get("pair") or {})
    usd_price = safe_float(token_price(token), 0.0)
    native_price = safe_float(pair.get("priceNative") if isinstance(pair, dict) else 0, 0.0)
    return usd_price / native_price if usd_price > 0 and native_price > 0 else None


def token_age_minutes(token):
    value = safe_float(token.get("pair_age_minutes"), None)
    if value is None or value <= 0:
        value = safe_float(token.get("token_age_minutes"), None)
    return value


def fmt_age(minutes):
    minutes = safe_float(minutes, None)
    if minutes is None or minutes < 0:
        return "UNKNOWN"
    if minutes < 60:
        return f"{max(1, int(minutes))}m"
    if minutes < 1440:
        return f"{int(minutes // 60)}h {int(minutes % 60)}m"
    days = int(minutes // 1440)
    hours = int((minutes % 1440) // 60)
    return f"{days}d {hours}h"


def tracked_trader_count(token):
    """Use the scanner's existing wallet-cluster count; it is an estimate until a wallet feed is connected."""
    return max(0, int(safe_float(token.get("smart_wallet_count", 0))))


def consensus_alerts(tokens):
    return sorted(
        (token for token in tokens if tracked_trader_count(token) >= 2),
        key=lambda token: (tracked_trader_count(token), safe_float(token.get("volume_5m_usd"))),
        reverse=True,
    )


def set_chart_token(token):
    st.session_state["chart_contract"] = token_contract(token)
    st.session_state["pending_scanner_view"] = "LIVE CHART"
    st.rerun()


def render_header(scanned_at):
    st.markdown(f"""<div class="falcon-header"><div><div class="falcon-brand">FALCON</div><div class="falcon-sub">TRADE SCANNER</div></div><div style="display:flex;gap:.7rem;align-items:center"><span class="eyebrow">LAST SCAN {html.escape(scanned_at)}</span><span class="live-pill">● LIVE</span></div></div>""", unsafe_allow_html=True)


def render_summary(tokens):
    total = len(tokens)
    hot = sum(1 for t in tokens if safe_float(t.get("price_change_5m_pct"), 0) >= 10 or tracked_trader_count(t) >= 2)
    buyers = sum(max(0, int(safe_float(t.get("buys_5m"), 0))) for t in tokens)
    volume = sum(max(0, safe_float(t.get("volume_5m_usd"), 0)) for t in tokens)
    st.markdown(f"""<div class="summary-grid">
      <div class="summary-card"><div class="summary-value">{total}</div><div class="summary-label">Tokens scanned</div></div>
      <div class="summary-card hot-card"><div class="summary-value">{hot}</div><div class="summary-label">High alerts</div></div>
      <div class="summary-card"><div class="summary-value">{buyers:,}</div><div class="summary-label">Buys · last 5m</div></div>
      <div class="summary-card"><div class="summary-value">{fmt_money(volume)}</div><div class="summary-label">Volume · last 5m</div></div>
    </div>""", unsafe_allow_html=True)


def render_alerts(tokens):
    st.markdown('<div class="section-heading">Alerts</div>', unsafe_allow_html=True)
    st.caption("Alerts require two or more clustered tracked-trader buys inside the rolling 10-minute window.")
    alerts = consensus_alerts(tokens)
    if not alerts:
        st.info("No tracked-trader consensus alerts in the latest scan. A direct FOMO wallet feed is not connected yet, so this screen will not invent activity.")
        return

    st.warning("Current wallet counts are scanner estimates from coordinated buy flow. Connect FOMO for direct tracked-wallet confirmations.")
    for index, token in enumerate(alerts):
        name = html.escape(str(token.get("token_name", "Unknown")))
        symbol = html.escape(str(token.get("token_symbol", "UNKNOWN")))
        count = tracked_trader_count(token)
        denominator = f" / {TRACKED_TRADER_TOTAL}" if TRACKED_TRADER_TOTAL else ""
        st.markdown(
            f'''<div class="alert-card"><div class="alert-top"><div><div class="token-name">{name} <span class="token-symbol">${symbol}</span></div><div class="contract">{html.escape(token_contract(token) or "Contract unavailable")}</div></div><div class="consensus">{count}{denominator} TRACKED TRADERS BOUGHT<br>LAST {ALERT_WINDOW_MINUTES} MIN</div></div><div class="metric-grid"><div class="metric"><div class="metric-key">Market cap</div><div class="metric-value">{fmt_money(token.get("market_cap_usd"))}</div></div><div class="metric"><div class="metric-key">Liquidity</div><div class="metric-value">{fmt_money(token.get("liquidity_usd"))}</div></div><div class="metric"><div class="metric-key">Volume 5m</div><div class="metric-value">{fmt_money(token.get("volume_5m_usd"))}</div></div></div></div>''',
            unsafe_allow_html=True,
        )
        if st.button("OPEN LIVE CHART", key=f"alert_chart_{index}", use_container_width=True):
            set_chart_token(token)


def render_following(tokens):
    st.markdown('<div class="section-heading">Following</div>', unsafe_allow_html=True)
    st.caption("Source-ready wallet tracking. Connections remain visibly inactive until their backend credentials and collectors are available.")
    source_columns = st.columns(3)
    sources = [
        ("FOMO", "NOT CONNECTED", "Planned primary tracked-wallet feed. Connect this source to replace scanner estimates with direct buy events."),
        ("Axiom", "NOT CONNECTED", "Reserved as a secondary discovery and wallet-activity source. No live activity is shown until connected."),
        ("GMGN", "NOT CONNECTED", "Reserved as an additional market and trader source. Existing links remain available from chart context."),
    ]
    for column, (name, status, copy) in zip(source_columns, sources):
        with column:
            st.markdown(f'<div class="source-card"><div class="source-name">{name}</div><div class="source-status">{status}</div><div class="source-copy">{copy}</div></div>', unsafe_allow_html=True)

    st.markdown('<div class="section-heading">Recent Activity</div>', unsafe_allow_html=True)
    recent = consensus_alerts(tokens)[:6]
    if not recent:
        st.info("No recent consensus activity is available from the current scan.")
        return
    st.caption("Estimated activity from the existing scanner, pending direct source confirmation.")
    for token in recent:
        st.markdown(
            f'<div class="detail-row"><div class="detail-label">ESTIMATED CLUSTER · {ALERT_WINDOW_MINUTES} MIN WINDOW</div><div class="detail-value">{html.escape(str(token.get("token_symbol", "UNKNOWN")))}: {tracked_trader_count(token)} tracked traders clustered in the current scan</div></div>',
            unsafe_allow_html=True,
        )


def chart_render_stage(candles, historical_error=None):
    """Classify what the chart should show for the candles actually available.

    Never gates raw candle visibility on the 20-bar signal-readiness threshold;
    that threshold only controls whether analyze_candles/build_signal_markers
    run. Returns (stage, message) where stage is "empty", "partial", or "ready".
    """
    if candles is None or candles.empty:
        if historical_error is not None:
            return "empty", f"Historical chart data unavailable: {historical_error}"
        return "empty", "Listening for the first confirmed swap…"
    count = len(candles)
    if count < 20:
        label = "WARMING UP" if count < 5 else "EARLY READ" if count < 10 else "PRELIMINARY"
        reason = f" (historical fetch unavailable: {historical_error})" if historical_error is not None else ". Empty periods are not invented; full technical decision starts at 20 traded bars."
        return "partial", f"{label} · {count}/20 real bars{reason}"
    return "ready", None


def load_chart_candles(pool_address, timeframe):
    """Historical OHLCV via the shared, process-wide cache (not per-browser-session).

    Caching, in-flight request dedup, and rate-limit backoff all live in
    technical_engine.get_cached_historical_candles so refreshing the page,
    switching tokens, or switching timeframes never fires a fresh retry storm
    into GeckoTerminal's shared rate limit.
    """
    return get_cached_historical_candles(pool_address, timeframe, limit=240)


def market_cap_conversion_factor(market_cap_usd, reference_price_usd):
    """The one conversion Falcon uses everywhere: market cap = price * effective_supply.

    ``effective_supply`` is derived once per render from the scanner's current
    market cap and reference price (assumed constant across the visible
    window) and then applied uniformly to every OHLC value — historical and
    live alike — via candles_to_market_cap. Returns None when either input is
    unusable, in which case MCAP must be treated as unavailable, not guessed.
    """
    if market_cap_usd and market_cap_usd > 0 and reference_price_usd and reference_price_usd > 0:
        return market_cap_usd / reference_price_usd
    return None


def chart_analysis_labels(candles, setup):
    recent = candles.tail(min(20, len(candles)))
    support = float(recent["low"].min())
    resistance = float(recent["high"].max())
    latest = candles.iloc[-1]
    previous_high = float(candles.iloc[-6:-1]["high"].max()) if len(candles) >= 6 else resistance
    breakout = float(latest["close"]) > previous_high
    pullback = setup.entry_low <= float(latest["close"]) <= setup.entry_high
    momentum = str(setup.volume_state or "Unknown")
    return support, resistance, breakout, pullback, momentum


def trend_overlay(candles):
    close = candles["close"].astype(float)
    trend = close.ewm(span=13, adjust=False).mean()
    candle_range = (candles["high"].astype(float) - candles["low"].astype(float)).rolling(14, min_periods=3).mean()
    band_size = candle_range.fillna(candle_range.median()).clip(lower=close.abs() * 0.002)
    bullish = close >= trend
    return trend, trend - band_size, trend + band_size, bullish


def to_chart_time(value):
    timestamp = pd.Timestamp(value)
    return int(timestamp.timestamp())


def chart_overlay_data(indicators, selected_indicators):
    definitions = {
        "EMA": [("EMA 9", "ema9", "#39FF88")],
        "SMA": [("SMA 20", "sma20", "#7db7ff")],
        "EMA Cross": [("EMA 9", "ema9", "#39FF88"), ("EMA 21", "ema21", "#ffd166")],
        "Bollinger Bands": [("BB Upper", "bb_upper", "#a78bfa"), ("BB Mid", "bb_mid", "#8470c9"), ("BB Lower", "bb_lower", "#a78bfa")],
        "VWAP": [("VWAP", "vwap", "#ffad5c")],
        "SuperTrend": [("SuperTrend", "supertrend", "#ff72a1")],
    }
    overlays = []
    for selection in selected_indicators:
        for name, column, color in definitions.get(selection, []):
            points = [{"time": to_chart_time(row.timestamp), "value": float(getattr(row, column))} for row in indicators.itertuples(index=False) if pd.notna(getattr(row, column))]
            overlays.append({"name": name, "color": color, "points": points})
    return overlays


def render_lightweight_chart(candles, indicators, selected_indicators, token_pool_address, timeframe, markers, setup, support, resistance, active_buy, data_delayed, market_cap_factor=None):
    """Render the existing candle and setup data through TradingView Lightweight Charts."""
    # Trend is used by the engine, but is not drawn automatically. Keeping it
    # off the price pane makes the actual OHLC candles visually primary.
    candle_data = [
        {"time": to_chart_time(row.timestamp), "open": float(row.open), "high": float(row.high), "low": float(row.low), "close": float(row.close)}
        for row in candles.itertuples(index=False)
    ]
    market_cap_data = []
    if market_cap_factor and market_cap_factor > 0:
        market_cap_candles = candles_to_market_cap(candles, float(market_cap_factor))
        market_cap_data = [
            {"time": to_chart_time(row.timestamp), "open": float(row.open), "high": float(row.high), "low": float(row.low), "close": float(row.close)}
            for row in market_cap_candles.itertuples(index=False)
        ]
    bullish_trend = []
    bearish_trend = []
    marker_data = [
        {
            "time": to_chart_time(marker["timestamp"]),
            "position": "belowBar" if "BUY" in str(marker["verdict"]) else "aboveBar",
            "color": "#36d399" if "BUY" in str(marker["verdict"]) else "#fb7185",
            "shape": "arrowUp" if "BUY" in str(marker["verdict"]) else "arrowDown",
            "text": "BUY" if "BUY" in str(marker["verdict"]) else "SELL",
        }
        for marker in markers
    ]
    level_data = [
        {"price": support, "label": "SUPPORT", "color": "rgba(119, 189, 251, 0.48)", "style": "dotted"},
        {"price": resistance, "label": "RESISTANCE", "color": "rgba(255, 209, 102, 0.48)", "style": "dotted"},
    ]
    if active_buy and setup is not None:
        level_data.extend([
            {"price": float(setup.entry_low), "label": "ENTRY", "color": "rgba(119, 189, 251, 0.72)", "style": "dashed"},
            {"price": float(setup.entry_high), "label": "ENTRY", "color": "rgba(119, 189, 251, 0.72)", "style": "dashed"},
            {"price": float(setup.target_1), "label": "TP1", "color": "rgba(54, 211, 153, 0.72)", "style": "dashed"},
            {"price": float(setup.target_2), "label": "TP2", "color": "rgba(54, 211, 153, 0.72)", "style": "dashed"},
            {"price": float(setup.stop), "label": "STOP", "color": "rgba(251, 113, 133, 0.72)", "style": "dashed"},
        ])
    chart_data = {
        "chartKey": f"{token_pool_address}:{timeframe}",
        # PRICE/MCAP is a per-token preference (not per-timeframe): keying this
        # on the pool address alone is what lets it survive timeframe switches.
        "modeKey": f"falcon-chart-mode:{token_pool_address}",
        "candles": candle_data,
        "bullishTrend": bullish_trend,
        "bearishTrend": bearish_trend,
        "markers": marker_data,
        "overlays": chart_overlay_data(indicators, selected_indicators),
        "levels": level_data,
        "currentPrice": candle_data[-1]["close"],
        "dataDelayed": data_delayed,
        "marketCapFactor": float(market_cap_factor) if market_cap_factor and market_cap_factor > 0 else None,
        "marketCapCandles": market_cap_data,
    }
    payload = json.dumps(chart_data).replace("</", "<\\/")
    components.html(
        f"""
        <div id="chart-shell">
          <div id="chart-controls"><button id="price-mode" type="button">PRICE</button><button id="mcap-mode" type="button">MCAP</button><span id="data-status"></span><button id="go-live" type="button">Go Live</button></div>
          <div id="chart-container"></div>
        </div>
        <script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
        <style>
          html, body {{ margin:0; background:#080a0b; overflow:hidden; font-family:Arial,sans-serif; }}
          #chart-shell {{ height:650px; background:#01060b; border:1px solid #0b5f91; border-radius:8px; box-sizing:border-box; position:relative; }}
          #chart-controls {{ height:34px; display:flex; align-items:center; gap:6px; padding:0 8px; border-bottom:1px solid #1c2429; box-sizing:border-box; }}
          #chart-controls button {{ background:#06131f; border:1px solid #0b4f7a; border-radius:3px; color:#93a09b; cursor:pointer; font-size:10px; font-weight:700; letter-spacing:.05em; padding:4px 7px; }}
          #chart-controls #price-mode {{ color:#55d7ff; border-color:#16b8ff; }}
          #chart-controls button:disabled {{ cursor:default; opacity:.45; }}
          #go-live {{ margin-left:auto; }} #data-status {{ color:#93a09b; font-size:10px; letter-spacing:.05em; }}
          #chart-container {{ height:614px; width:100%; background:#01060b; }}
        </style>
        <script>
          const chartData = {payload};
          const root = document.getElementById('chart-container');
          const status = document.getElementById('data-status');
          status.textContent = chartData.dataDelayed ? 'DATA DELAYED' : 'LIVE';
          status.style.color = chartData.dataDelayed ? '#ffd166' : '#63e6a5';
          const existing = window.tradeScannerChart;
          const lineStyle = {{ dotted: LightweightCharts.LineStyle.Dotted, dashed: LightweightCharts.LineStyle.Dashed }};
          const mcapButton = document.getElementById('mcap-mode');
          const priceButton = document.getElementById('price-mode');
          if (!chartData.marketCapFactor) {{ mcapButton.disabled = true; mcapButton.title = 'Market-cap calibration unavailable for this token'; }}
          const scaleValue = (v, mode) => mode === 'mcap' ? v * chartData.marketCapFactor : v;
          const scaledCandles = (mode) => mode === 'mcap' ? chartData.marketCapCandles : chartData.candles;

          // The embedded HTML document is rebuilt on every refresh (Streamlit
          // reloads this iframe's srcdoc each time), so in-memory JS state such
          // as `state.mode` never survives a routine 1s update on its own.
          // PRICE/MCAP and the visible zoom are therefore persisted to
          // localStorage and reapplied immediately on every rebuild so a
          // routine refresh is visually indistinguishable from an in-place update.
          function readStoredMode() {{
            try {{
              const stored = localStorage.getItem(chartData.modeKey);
              return stored === 'mcap' && chartData.marketCapFactor ? 'mcap' : 'price';
            }} catch (_) {{ return 'price'; }}
          }}
          function storeMode(mode) {{ try {{ localStorage.setItem(chartData.modeKey, mode); }} catch (_) {{}} }}
          function setModeButtons(mode) {{
            priceButton.style.color = mode === 'price' ? '#55d7ff' : '#7fa7bf';
            priceButton.style.borderColor = mode === 'price' ? '#16b8ff' : '#0b4f7a';
            mcapButton.style.color = mode === 'mcap' ? '#55d7ff' : '#7fa7bf';
            mcapButton.style.borderColor = mode === 'mcap' ? '#16b8ff' : '#0b4f7a';
          }}
          function applyMode(state, mode, persist) {{
            if (mode === 'mcap' && !chartData.marketCapFactor) mode = 'price';
            state.mode = mode;
            state.candles.applyOptions({{priceFormat: priceFormat(mode)}}); state.candles.setData(scaledCandles(mode));
            state.chart.priceScale('right').applyOptions({{autoScale:true}});
            state.lastTime = chartData.candles.at(-1).time;
            setModeButtons(mode);
            addOverlays(state);
            if (persist) storeMode(mode);
          }}
          function addOverlays(state) {{
            state.bullishTrend.setData(chartData.bullishTrend);
            state.bearishTrend.setData(chartData.bearishTrend);
            state.candles.setMarkers(chartData.markers);
            state.levels.forEach(line => state.candles.removePriceLine(line));
            state.levels = chartData.levels.map(level => state.candles.createPriceLine({{ price: scaleValue(level.price, state.mode), color: level.color, lineWidth: 1, lineStyle: lineStyle[level.style], axisLabelVisible: false, title: '' }}));
            if (state.currentPrice) state.candles.removePriceLine(state.currentPrice);
            state.currentPrice = state.candles.createPriceLine({{ price: scaleValue(chartData.currentPrice, state.mode), color: '#55d7ff', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true, title: 'PRICE' }});
                        Object.values(state.indicators).forEach(series => state.chart.removeSeries(series));
                        state.indicators = {{}};
                        chartData.overlays.forEach(overlay => {{
                            const series = state.chart.addLineSeries({{ color: overlay.color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }});
                            series.setData(overlay.points.map(p => ({{...p, value: scaleValue(p.value, state.mode)}}))); state.indicators[overlay.name] = series;
                        }});
          }}
          function priceFormat(mode) {{
            const v = mode === 'mcap' ? scaleValue(chartData.currentPrice, 'mcap') : chartData.currentPrice;
            if (mode === 'mcap') return {{ type:'custom', minMove:0.01, formatter:p => '$' + (p >= 1000000 ? (p/1000000).toFixed(2)+'M' : p >= 1000 ? (p/1000).toFixed(1)+'K' : p.toFixed(2)) }};
            const av = Math.abs(v || 0);
            const precision = av >= 1 ? 4 : av >= .01 ? 6 : av >= .0001 ? 8 : av >= .000001 ? 10 : 12;
            return {{ type:'price', precision, minMove: Math.pow(10, -precision) }};
          }}
          const rangeKey = 'falcon-chart-range:' + chartData.chartKey;
          const followKey = 'falcon-chart-follow:' + chartData.chartKey;
          function defaultRange() {{ return {{ from: Math.max(chartData.candles.length - 80, 0), to: chartData.candles.length + 5 }}; }}
          function saveRange(range) {{
            // Stored as the exact logical range, not a relative offset: once a
            // live session starts, candles are only ever appended (backfill only
            // prepends once, before the first candle ever reaches the browser),
            // so index 0 always means the same candle and the saved range can be
            // reapplied verbatim without the viewport visibly moving.
            try {{ localStorage.setItem(rangeKey, JSON.stringify({{ from: range.from, to: range.to }})); }} catch (_) {{}}
          }}
          function clearSavedRange() {{ try {{ localStorage.removeItem(rangeKey); }} catch (_) {{}} }}
          function readFollow() {{
            try {{ return localStorage.getItem(followKey) !== 'paused'; }} catch (_) {{ return true; }}
          }}
          function writeFollow(following) {{
            try {{ localStorage.setItem(followKey, following ? 'live' : 'paused'); }} catch (_) {{}}
          }}
          // Only genuine user pans/zooms may pause follow mode; our own
          // programmatic range changes (initial restore, Go Live, resize,
          // routine live-update re-follow) must not be mistaken for user
          // navigation. LightweightCharts' subscribeVisibleLogicalRangeChange
          // fires *asynchronously* (observed 100-200ms after the triggering
          // gesture) and Streamlit rebuilds this srcdoc iframe roughly once
          // per second, so a callback registered this execution can lose the
          // race against the very iframe teardown it needs to survive,
          // silently dropping the "user paused" write. Instead, commit the
          // pause+range directly from our own DOM-level end-of-gesture
          // listeners (mouseup/wheel/touchend), synchronously within the same
          // execution that observed the interaction, which removes that race.
          let state;
          function commitUserRange() {{
            if (!state) return;
            const range = state.chart.timeScale().getVisibleLogicalRange();
            if (!range) return;
            writeFollow(false);
            saveRange(range);
          }}
          function commitUserRangeSoon() {{
            // Wait a frame so the library's internal range update (driven by
            // the same browser event) has already applied before we read it.
            requestAnimationFrame(() => requestAnimationFrame(commitUserRange));
          }}
          let pointerDown = false;
          root.addEventListener('mousedown', () => {{ pointerDown = true; }});
          window.addEventListener('mouseup', () => {{ if (pointerDown) {{ pointerDown = false; commitUserRangeSoon(); }} }});
          root.addEventListener('wheel', commitUserRangeSoon, {{ passive: true }});
          root.addEventListener('touchend', commitUserRangeSoon, {{ passive: true }});
          function applyRangeProgrammatically(chart, range) {{
            chart.timeScale().setVisibleLogicalRange(range);
          }}
          function pausedRange() {{
            let saved = null;
            try {{ saved = JSON.parse(localStorage.getItem(rangeKey) || 'null'); }} catch (_) {{}}
            if (!saved || !Number.isFinite(saved.from) || !Number.isFinite(saved.to) || saved.to <= saved.from) return defaultRange();
            return {{ from: saved.from, to: saved.to }};
          }}
          function createChart() {{
            const chart = LightweightCharts.createChart(root, {{ width: root.clientWidth, height: root.clientHeight, layout: {{ background: {{ color: '#01060b' }}, textColor: '#7fa7bf', fontSize: 11 }}, grid: {{ vertLines: {{ color: 'rgba(72, 78, 82, 0.07)' }}, horzLines: {{ color: 'rgba(72, 78, 82, 0.07)' }} }}, rightPriceScale: {{ borderColor: '#0b4f7a', scaleMargins: {{ top: 0.08, bottom: 0.08 }} }}, timeScale: {{ borderColor: '#0b4f7a', timeVisible: true, secondsVisible: true, rightOffset: 5, barSpacing: 9, minBarSpacing: 3, shiftVisibleRangeOnNewBar: false }}, crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }}, handleScroll: {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false }}, handleScale: {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }} }});
            const candles = chart.addCandlestickSeries({{ upColor:'#16c8ff', downColor:'#ff2d78', borderUpColor:'#16c8ff', borderDownColor:'#ff2d78', wickUpColor:'#55d7ff', wickDownColor:'#ff5b96', priceLineVisible:false, lastValueVisible:false, priceFormat:priceFormat('price') }});
            const bullishTrend = chart.addLineSeries({{ color: '#36d399', lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }});
            const bearishTrend = chart.addLineSeries({{ color: '#fb7185', lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }});
            const localState = {{ key: chartData.chartKey, chart, candles, bullishTrend, bearishTrend, levels: [], currentPrice: null, indicators: {{}}, lastTime: 0, mode: 'price' }};
            applyMode(localState, readStoredMode(), false);
            applyRangeProgrammatically(chart, readFollow() ? defaultRange() : pausedRange());
            new ResizeObserver(() => {{
              chart.applyOptions({{ width: root.clientWidth, height: root.clientHeight }});
            }}).observe(root);
            window.tradeScannerChart = localState;
            return localState;
          }}
          state = existing && existing.key === chartData.chartKey ? existing : createChart();
          if (state === existing) {{
            const updates = chartData.candles.filter(bar => bar.time >= state.lastTime);
            updates.forEach(bar => state.candles.update(state.mode === 'mcap' ? chartData.marketCapCandles.find(mcapBar => mcapBar.time === bar.time) : bar));
            state.lastTime = chartData.candles.at(-1).time; addOverlays(state);
            // Routine live update: only advance the view if the user is still
            // following live; a paused/manual viewport must never be nudged.
            if (readFollow()) {{ applyRangeProgrammatically(state.chart, defaultRange()); }}
          }}
          priceButton.onclick = () => applyMode(state, 'price', true);
          mcapButton.onclick = () => applyMode(state, 'mcap', true);
          document.getElementById('go-live').onclick = () => {{
            writeFollow(true);
            clearSavedRange();
            applyRangeProgrammatically(state.chart, defaultRange());
          }};
        </script>
        """,
        height=652,
        scrolling=False,
    )


def mark_chart_render_dispatch(mint: str, timeframe: str) -> None:
    """Records when Python handed candles to the browser component (dispatch time, not confirmed paint time)."""
    marks = st.session_state.setdefault("chart_render_dispatch", {})
    marks[f"{mint}:{timeframe}"] = datetime.now(timezone.utc)


def render_live_feed_diagnostics(mint: str | None = None, timeframe: str | None = None):
    """Developer-only listener health; credentials and full signatures remain hidden."""
    with st.expander("Live feed diagnostics", expanded=False):
        diagnostics = get_live_feed_diagnostics(mint)
        status_columns = st.columns(4)
        status_columns[0].metric("Stream mode", diagnostics.get("stream_mode", "STANDARD"))
        status_columns[1].metric("WS status", diagnostics.get("ws_status", "DISCONNECTED"))
        status_columns[2].metric("Queue depth", diagnostics.get("queue_depth", 0))
        feed_lag = diagnostics.get("feed_lag_ms")
        status_columns[3].metric("Feed lag", f"{feed_lag:.0f} ms" if feed_lag is not None else "--")
        columns = st.columns(4)
        columns[0].metric("WebSocket", "CONNECTED" if diagnostics.get("websocket_connected") else "IDLE")
        columns[1].metric("Trades received", diagnostics.get("notifications_received", 0))
        columns[2].metric("Trades parsed", diagnostics.get("notifications_parsed", 0))
        columns[3].metric("Trades validated", diagnostics.get("validated_swaps", 0))
        chart_age = diagnostics.get("chart_age_ms")
        st.caption(
            f"Subscription: {diagnostics.get('subscription_id') or '--'} · Transactions: {diagnostics.get('transactions_fetched', 0)} · "
            f"Candidates: {diagnostics.get('candidate_swaps', 0)} · Rejected: {diagnostics.get('rejected_swaps', 0)} · "
            f"Parse failures: {diagnostics.get('parse_failures', 0)} · Last price: {fmt_price(diagnostics.get('last_validated_price'))} · "
            f"Reference: {fmt_price(diagnostics.get('reference_price'))}"
        )
        st.caption(
            f"Last WS message: {diagnostics.get('last_ws_message_at') or '--'} · Last valid trade: {diagnostics.get('last_valid_trade_at') or '--'} · "
            f"Chart age: {f'{chart_age:.0f} ms' if chart_age is not None else '--'}"
        )
        if mint and timeframe:
            dispatch_at = st.session_state.get("chart_render_dispatch", {}).get(f"{mint}:{timeframe}")
            candle_update_at = diagnostics.get("last_candle_update_at")
            dispatch_lag_ms = None
            if dispatch_at is not None and candle_update_at:
                try:
                    dispatch_lag_ms = (dispatch_at - datetime.fromisoformat(candle_update_at)).total_seconds() * 1000.0
                except ValueError:
                    dispatch_lag_ms = None
            st.caption(
                f"Last render dispatch: {dispatch_at.isoformat() if dispatch_at else '--'} · "
                f"Candle→dispatch lag: {f'{dispatch_lag_ms:.0f} ms' if dispatch_lag_ms is not None else '--'} "
                "(covers aggregation → Python dispatch only; actual browser paint time is not yet measured)"
            )
        sol_age = diagnostics.get("sol_usd_age_seconds")
        st.caption(
            f"SOL/USD: {'available' if diagnostics.get('sol_usd_available') else 'UNAVAILABLE'} "
            f"({diagnostics.get('sol_usd_status', 'missing')}) · Source: {diagnostics.get('sol_usd_source') or '--'} · "
            f"Price: {fmt_price(diagnostics.get('sol_usd_price'))} · Age: {f'{sol_age:.1f}s' if sol_age is not None else '--'}"
        )
        if diagnostics.get("sol_usd_fetch_error"):
            st.caption(f"SOL/USD fetch failures: {diagnostics.get('sol_usd_fetch_failures', 0)} · Last error: {diagnostics['sol_usd_fetch_error']}")
        backfill_coverage = diagnostics.get("backfill_coverage_seconds")
        st.caption(
            f"Pre-session 15s/30s/1s backfill: {diagnostics.get('backfill_trades', 0)} real trades "
            f"({f'{backfill_coverage:.0f}s' if backfill_coverage is not None else '--'} covered, "
            f"{diagnostics.get('backfill_signatures_seen', 0)} signatures scanned)"
            + (f" · {diagnostics['backfill_error']}" if diagnostics.get("backfill_error") else "")
        )
        historical_cooldown = historical_fetch_rate_limited_seconds()
        if historical_cooldown > 0:
            st.caption(f"Historical (1m/5m) fetch is rate-limited process-wide for {historical_cooldown:.0f}s more. This never affects live 1s/15s/30s candles, which never call the historical API.")
        rejection_reasons = diagnostics.get("rejection_reasons") or {}
        if rejection_reasons:
            breakdown = " · ".join(f"{reason}: {count}" for reason, count in sorted(rejection_reasons.items(), key=lambda item: -item[1]))
            st.caption(f"Rejections by reason — {breakdown}")
        if diagnostics.get("last_rejection"):
            st.code(f"Last rejected swap: {diagnostics['last_rejection']}", language=None)
        if diagnostics.get("last_parser_error"):
            st.caption(f"Last parser error: {diagnostics['last_parser_error']}")
        validated = diagnostics.get("recent_validated", [])
        if validated:
            st.caption("Most recent validated trades. Every live candle is built exclusively from these USD/token records.")
            st.dataframe(validated[-20:], use_container_width=True, hide_index=True)
        rejected = diagnostics.get("recent_rejected", [])
        if rejected:
            st.caption("Most recent rejected trades")
            st.dataframe(rejected[-5:], use_container_width=True, hide_index=True)


@st.fragment(run_every="1s")
def render_live_chart(tokens):
    st.markdown('<div class="section-heading">Live Chart</div>', unsafe_allow_html=True)
    selectable = [token for token in tokens if token_pool(token)]
    if not selectable:
        st.info("No pool addresses are available in this scan. Live OHLCV analysis will appear when a source provides a pool address.")
        return

    labels = [f"{token.get('token_symbol', 'UNKNOWN')} · {token.get('token_name', 'Unknown')}" for token in selectable]
    selected_contract = st.session_state.get("chart_contract", "")
    default_index = next((index for index, token in enumerate(selectable) if token_contract(token) == selected_contract), 0)
    selection_col, timeframe_col = st.columns([3, 2])
    with selection_col:
        selected_label = st.selectbox("Token", labels, index=default_index, key="chart_token")
    with timeframe_col:
        timeframe = st.radio("Timeframe", ["1s", "15s", "30s", "1m", "5m"], index=3, horizontal=True, key="chart_timeframe")
        if live_provider_available():
            st.caption(f"LIVE FEED: 1s · CHART: {timeframe}")
        else:
            st.caption("Add HELIUS_API_KEY to .env to activate 1s / 15s / 30s live candles")
    with st.popover("INDICATORS", use_container_width=False):
        st.caption("Overlay studies draw on price. Oscillators are saved for the forthcoming lower pane.")
        selected_indicators = st.multiselect(
            "Display",
            ["EMA", "SMA", "EMA Cross", "RSI", "MACD", "Bollinger Bands", "VWAP", "Volume", "ATR", "SuperTrend", "Stochastic RSI", "Money Flow Index", "On Balance Volume"],
            key="chart_indicators",
        )
    token = selectable[labels.index(selected_label)]
    symbol = str(token.get("token_symbol", "UNKNOWN"))
    age_minutes = token_age_minutes(token)
    age_text = fmt_age(age_minutes)
    mc_now = safe_float(token.get("market_cap_usd"), 0.0)
    st.caption(f"AGE: {age_text}  ·  MCAP: {fmt_money(mc_now) if mc_now > 0 else 'N/A'}  ·  CA: {token_contract(token)}")

    try:
        session = None
        reference_price = safe_float(token_price(token), 0.0)
        if live_provider_available():
            # One subscription receives every validated trade immediately; the
            # selected chart timeframe only chooses which local aggregation is viewed.
            session = get_live_candle_session(token_contract(token), reference_price_usd=reference_price, sol_usd=token_sol_usd(token))

        data_delayed = False
        historical_error: CandleDataError | None = None
        if timeframe in {"1s", "15s", "30s"}:
            if not live_provider_available():
                raise CandleDataError("Live candles need a Helius API key. Add HELIUS_API_KEY to .env, install requirements, and restart Streamlit.")
            candles = session.frame(timeframe)
        else:
            try:
                with st.spinner(f"Loading {timeframe} OHLCV for {symbol}..."):
                    candles, data_delayed = load_chart_candles(token_pool(token), timeframe)
            except CandleDataError as error:
                # A failed or too-short historical fetch must not hide whatever
                # valid live candles already exist for this timeframe.
                historical_error = error
                candles = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            if session is not None:
                candles = merge_historical_and_live_candles(candles, session.frame(timeframe))

        stage, message = chart_render_stage(candles, historical_error)
        if stage == "empty":
            if historical_error is not None:
                st.warning(f"{message} for {symbol}")
            else:
                st.info(f"Listening for the first confirmed {symbol} swap…")
            render_live_feed_diagnostics(token_contract(token), timeframe)
            return

        if stage == "partial":
            # Chart rendering is never gated on the 20-bar signal-readiness
            # threshold: show every real candle we have and defer only the
            # full technical verdict, which genuinely needs that history.
            st.info(f"{message} ({timeframe})")
            visible_candles = candles.tail(80).copy()
            visible_indicators = calculate_indicators(candles).tail(80).copy()
            support = float(visible_candles["low"].min())
            resistance = float(visible_candles["high"].max())
            ref_price = safe_float(token_price(token), 0.0) or float(visible_candles["close"].iloc[-1])
            mc_factor = market_cap_conversion_factor(mc_now, ref_price)
            render_lightweight_chart(visible_candles, visible_indicators, selected_indicators, token_pool(token), timeframe, [], None, support, resistance, False, data_delayed, mc_factor)
            mark_chart_render_dispatch(token_contract(token), timeframe)
            render_live_feed_diagnostics(token_contract(token), timeframe)
            return

        setup = analyze_candles(candles)
        markers = build_signal_markers(candles)
    except CandleDataError as error:
        st.warning(f"Chart data unavailable for {symbol}: {error}")
        return
    except Exception as error:
        st.warning(f"Could not load chart data for {symbol}: {error}")
        return

    engine_verdict = str(setup.verdict).upper()
    verdict = "BUY" if "BUY" in engine_verdict else engine_verdict
    verdict_class = "buy" if verdict == "BUY" else "sell" if verdict == "SELL" else ""
    tracked = tracked_trader_count(token)
    trend_name = str(setup.trend).upper()
    momentum_name = str(setup.volume_state or "UNKNOWN").upper()
    trader_display = f"{tracked} / --" if tracked else "-- / --"
    mtf_1m = trend_name.lower() if timeframe == "1m" else "pending"
    mtf_5m = trend_name.lower() if timeframe == "5m" else "pending"
    micro_label = f"{timeframe} {trend_name.lower()} | " if timeframe in {"1s", "15s", "30s"} else ""
    decision_color = "#36d399" if verdict == "BUY" else "#fb7185" if verdict == "SELL" else "#ffd166"
    st.markdown(f'''<div style="display:flex; flex-wrap:wrap; gap:.35rem .9rem; align-items:center; margin:.35rem 0 .45rem; padding:.48rem .65rem; border:1px solid #293238; border-radius:5px; background:#111519; font-family:'DM Mono',monospace; font-size:.68rem; letter-spacing:.025em"><span style="color:#93a09b">DECISION: <b style="color:{decision_color}">{html.escape(verdict)}</b></span><span style="color:#93a09b">TREND: <b style="color:#f2f5f4">{html.escape(trend_name)}</b></span><span style="color:#93a09b">MOMENTUM: <b style="color:#f2f5f4">{html.escape(momentum_name)}</b></span><span style="color:#93a09b">TRADERS: <b style="color:#f2f5f4">{trader_display}</b></span><span style="color:#93a09b">MTF: <b style="color:#f2f5f4">{html.escape(micro_label)}1m {html.escape(mtf_1m)} | 5m {html.escape(mtf_5m)}</b></span></div>''', unsafe_allow_html=True)

    visible_candles = candles.tail(80).copy()
    visible_indicators = calculate_indicators(candles).tail(80).copy()
    support, resistance, breakout, pullback, momentum = chart_analysis_labels(visible_candles, setup)
    active_buy = engine_verdict in {"MOMENTUM BUY", "CONFIRMED BUY"}
    recent_times = set(visible_candles["timestamp"])
    confirmed_markers = [marker for marker in markers if marker.get("timestamp") in recent_times and str(marker.get("verdict", "")) in {"CONFIRMED BUY", "MOMENTUM BUY", "SELL"}][-4:]
    ref_price = safe_float(token_price(token), 0.0) or float(visible_candles["close"].iloc[-1])
    mc_factor = market_cap_conversion_factor(mc_now, ref_price)
    render_lightweight_chart(visible_candles, visible_indicators, selected_indicators, token_pool(token), timeframe, confirmed_markers, setup, support, resistance, active_buy, data_delayed, mc_factor)
    mark_chart_render_dispatch(token_contract(token), timeframe)

    details = st.columns(2)
    with details[0]:
        st.markdown('<div class="section-heading">Trade Levels</div>', unsafe_allow_html=True)
        st.write(f"Entry zone: {fmt_price(setup.entry_low)} - {fmt_price(setup.entry_high)}")
        st.write(f"Invalidation / stop: {fmt_price(setup.stop)}")
        st.write(f"Take profit: {fmt_price(setup.target_1)} / {fmt_price(setup.target_2)}")
    with details[1]:
        st.markdown('<div class="section-heading">Chart State</div>', unsafe_allow_html=True)
        st.write(f"Regime: {setup.trend}")
        st.write(f"Pullback / retest: {'Detected in entry zone' if pullback else 'Not active'}")
        st.write(f"Breakout: {'Detected above recent range' if breakout else 'Not confirmed'}")
        st.write(f"Volume / momentum: {momentum}")
    render_live_feed_diagnostics(token_contract(token), timeframe)


def load_scan():
    if "scan_payload" not in st.session_state or st.session_state.get("refresh_scan"):
        st.session_state.scan_payload = scan_tokens()
        st.session_state.refresh_scan = False
    return st.session_state.scan_payload


def format_scan_time(value):
    if not value:
        return "Not scanned"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%H:%M:%S UTC")
    except ValueError:
        return str(value)


def display_dashboard():
    inject_css()
    pending_view = st.session_state.pop("pending_scanner_view", None)
    if pending_view:
        st.session_state.scanner_view = pending_view
    if "scanner_view" not in st.session_state:
        st.session_state.scanner_view = "ALERTS"
    payload = load_scan()
    scanned_at = format_scan_time(payload.get("scanned_at")) if isinstance(payload, dict) else "Not scanned"
    render_header(scanned_at)
    render_summary(payload.get("tokens", []) if isinstance(payload, dict) else [])

    controls, refresh = st.columns([4, 1])
    with controls:
        view = st.radio("Navigation", ["ALERTS", "FOLLOWING", "LIVE CHART"], horizontal=True, key="scanner_view")
    with refresh:
        st.write("")
        if st.button("REFRESH", use_container_width=True):
            st.session_state.refresh_scan = True
            st.rerun()

    if not isinstance(payload, dict) or not payload.get("ok"):
        st.error((payload or {}).get("error", "Scanner data is unavailable."))
        return
    tokens = payload.get("tokens", [])
    if view == "ALERTS":
        render_alerts(tokens)
    elif view == "FOLLOWING":
        render_following(tokens)
    else:
        render_live_chart(tokens)


if __name__ == "__main__":
    display_dashboard()