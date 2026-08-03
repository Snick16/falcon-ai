import streamlit as st
import pandas as pd
from datetime import datetime, timezone
import html
from Scanner import scan_tokens

st.set_page_config(
    page_title="Falcon",
    page_icon="🦅",
    layout="wide"
)

st.title("🦅 FALCON")
st.subheader("AI Market Intelligence")
st.divider()

REQUIRED_TOKEN_FIELDS = ("signal", "momentum", "confidence", "risk_label")


def load_scan(force_refresh=False):
    if force_refresh or "scan_payload" not in st.session_state:
        st.session_state.scan_payload = scan_tokens()

    payload = st.session_state.scan_payload
    if payload_needs_refresh(payload):
        payload = scan_tokens()
        st.session_state.scan_payload = payload

    return payload


def payload_needs_refresh(payload):
    """Refresh once if cached payload predates trade-readiness fields."""
    if not payload or not payload.get("ok"):
        return False

    tokens = payload.get("tokens", [])
    if not tokens:
        return False

    for token in tokens:
        for field in REQUIRED_TOKEN_FIELDS:
            if field not in token:
                return True
    return False


def format_scan_time(iso_value):
    """Convert scan timestamp to a clear UTC label."""
    if not iso_value:
        return "N/A"
    try:
        parsed = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return str(iso_value)


def style_signal(value):
    styles = {
        "BUY": "background-color: #1f7a1f; color: white; font-weight: 700;",
        "WATCH": "background-color: #d4a017; color: black; font-weight: 700;",
        "PASS": "background-color: #b22222; color: white; font-weight: 700;",
    }
    return styles.get(str(value), "")


def style_momentum(value):
    styles = {
        "BULLISH": "color: #1f7a1f; font-weight: 700;",
        "NEUTRAL": "color: #666666; font-weight: 700;",
        "BEARISH": "color: #b22222; font-weight: 700;",
    }
    return styles.get(str(value), "")


def style_risk(value):
    styles = {
        "LOW": "background-color: #1f7a1f; color: white; font-weight: 700;",
        "MEDIUM": "background-color: #d4a017; color: black; font-weight: 700;",
        "HIGH": "background-color: #b22222; color: white; font-weight: 700;",
    }
    return styles.get(str(value), "")


def style_falcon_score(value):
    try:
        score = int(value)
    except (TypeError, ValueError):
        return ""

    if score >= 70:
        return "background-color: #1f7a1f; color: white; font-weight: 700;"
    if score >= 40:
        return "background-color: #d4a017; color: black; font-weight: 700;"
    return "background-color: #b22222; color: white; font-weight: 700;"


def format_delta(value):
    if value == "NEW":
        return "NEW"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    if number > 0:
        return f"+{number}"
    return str(number)


def style_delta(value):
    if value == "NEW":
        return "background-color: #e6f7ff; color: #005a9e; font-weight: 700;"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if number > 0:
        return "background-color: #e7f6e7; color: #136c13; font-weight: 700;"
    if number < 0:
        return "background-color: #fdeaea; color: #8b1e1e; font-weight: 700;"
    return ""


def style_signal_change(value):
    if value == "NEW":
        return "background-color: #e6f7ff; color: #005a9e; font-weight: 700;"
    if value == "UNCHANGED":
        return ""
    ranks = {"PASS": 0, "WATCH": 1, "BUY": 2}
    if "->" not in str(value):
        return ""
    left, right = [part.strip() for part in str(value).split("->", 1)]
    if ranks.get(right, 0) > ranks.get(left, 0):
        return "background-color: #e7f6e7; color: #136c13; font-weight: 700;"
    if ranks.get(right, 0) < ranks.get(left, 0):
        return "background-color: #fdeaea; color: #8b1e1e; font-weight: 700;"
    return ""


def style_momentum_change(value):
    if value == "NEW":
        return "background-color: #e6f7ff; color: #005a9e; font-weight: 700;"
    if value == "UNCHANGED":
        return ""
    ranks = {"BEARISH": 0, "NEUTRAL": 1, "BULLISH": 2}
    if "->" not in str(value):
        return ""
    left, right = [part.strip() for part in str(value).split("->", 1)]
    if ranks.get(right, 0) > ranks.get(left, 0):
        return "background-color: #e7f6e7; color: #136c13; font-weight: 700;"
    if ranks.get(right, 0) < ranks.get(left, 0):
        return "background-color: #fdeaea; color: #8b1e1e; font-weight: 700;"
    return ""


refresh_clicked = st.button("Refresh Scan", type="primary")
payload = load_scan(force_refresh=refresh_clicked)


st.divider()

if not payload.get("ok"):
    st.error(payload.get("error", "Scan failed due to an unknown error."))
else:
    tokens = payload.get("tokens", [])
    buy_count = sum(1 for token in tokens if token.get("signal", "PASS") == "BUY")
    watch_count = sum(1 for token in tokens if token.get("signal", "PASS") == "WATCH")
    highest_score = max((int(token.get("score", 0)) for token in tokens), default=0)
    average_confidence = (
        round(sum(float(token.get("confidence", 0)) for token in tokens) / len(tokens), 1)
        if tokens else 0.0
    )
    last_scan_time = format_scan_time(payload.get("scanned_at"))

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("BUY Signals", str(buy_count))
    with col2:
        st.metric("WATCH Signals", str(watch_count))
    with col3:
        st.metric("Highest Falcon Score", str(highest_score))
    with col4:
        st.metric("Average Confidence", str(average_confidence))
    with col5:
        st.metric("Last Scan Time", last_scan_time)

    st.divider()

    if not tokens:
        st.info("No opportunities found in the latest scan.")
    else:
        signal_filter = st.selectbox(
            "Signal Filter",
            options=["ALL", "BUY", "WATCH", "PASS"],
            index=0,
        )

        filtered_tokens = tokens
        if signal_filter != "ALL":
            filtered_tokens = [
                token for token in tokens
                if str(token.get("signal", "MISSING")).upper() == signal_filter
            ]

        filtered_tokens = sorted(
            filtered_tokens,
            key=lambda token: int(token.get("score", 0)),
            reverse=True,
        )

        if not filtered_tokens:
            st.info("No opportunities match the selected signal filter.")
            st.stop()

        rows = []
        for token in filtered_tokens:
            chart_url = str(token.get("dexscreener_url", "") or "")
            chart_link = (
                f'<a href="{html.escape(chart_url)}" target="_blank">Open Chart</a>'
                if chart_url else ""
            )
            rows.append(
                {
                    "Score Δ": format_delta(token.get("score_delta", "NEW")),
                    "Confidence Δ": format_delta(token.get("confidence_delta", "NEW")),
                    "Signal Change": token.get("signal_change", "NEW"),
                    "Momentum Change": token.get("momentum_change", "NEW"),
                    "Signal": token.get("signal", "MISSING"),
                    "Momentum": token.get("momentum", "MISSING"),
                    "Confidence": int(token.get("confidence", 0)),
                    "Falcon Score": int(token.get("score", 0)),
                    "Risk": token.get("risk_label", "MISSING"),
                    "Token": f"{token.get('token_name', 'Unknown')} ({token.get('token_symbol', 'UNKNOWN')})",
                    "Contract Address": token.get("contract_address", "N/A"),
                    "Market Cap (USD)": f"${token.get('market_cap_usd', 0):,.0f}",
                    "Liquidity (USD)": f"${token.get('liquidity_usd', 0):,.0f}",
                    "5m Volume (USD)": f"${token.get('volume_5m_usd', 0):,.0f}",
                    "5m Price Change": f"{token.get('price_change_5m_pct', 0):+.2f}%",
                    "Buys/Sells (5m)": f"{token.get('buys_5m', 0)}/{token.get('sells_5m', 0)}",
                    "Reasons": ", ".join(token.get("reasons", [])) or "No strong signals",
                    "DexScreener": chart_link,
                }
            )

        table_df = pd.DataFrame(rows)
        styled_table = (
            table_df.style
            .map(style_delta, subset=["Score Δ"])
            .map(style_delta, subset=["Confidence Δ"])
            .map(style_signal_change, subset=["Signal Change"])
            .map(style_momentum_change, subset=["Momentum Change"])
            .map(style_signal, subset=["Signal"])
            .map(style_momentum, subset=["Momentum"])
            .map(style_risk, subset=["Risk"])
            .map(style_falcon_score, subset=["Falcon Score"])
            .set_properties(**{"text-align": "left"})
            .set_table_styles(
                [
                    {"selector": "th", "props": [("text-align", "left")]},
                    {"selector": "td", "props": [("padding", "6px 10px")]},
                ]
            )
        )
        st.markdown(styled_table.to_html(escape=False), unsafe_allow_html=True)