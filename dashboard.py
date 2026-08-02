import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from Scanner import scan_tokens

st.set_page_config(
    page_title="Falcon",
    page_icon="🦅",
    layout="wide"
)

st.title("🦅 FALCON")
st.subheader("AI Market Intelligence")
st.divider()


def load_scan(force_refresh=False):
    if force_refresh or "scan_payload" not in st.session_state:
        st.session_state.scan_payload = scan_tokens()
    return st.session_state.scan_payload


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
        rows = []
        for token in tokens:
            rows.append(
                {
                    "Signal": token.get("signal", "PASS"),
                    "Momentum": token.get("momentum", "NEUTRAL"),
                    "Confidence": int(token.get("confidence", 0)),
                    "Falcon Score": token.get("score", 0),
                    "Risk": token.get("risk_label", "MEDIUM"),
                    "Token": f"{token.get('token_name', 'Unknown')} ({token.get('token_symbol', 'UNKNOWN')})",
                    "Contract Address": token.get("contract_address", "N/A"),
                    "Market Cap (USD)": f"${token.get('market_cap_usd', 0):,.0f}",
                    "Liquidity (USD)": f"${token.get('liquidity_usd', 0):,.0f}",
                    "5m Volume (USD)": f"${token.get('volume_5m_usd', 0):,.0f}",
                    "5m Price Change": f"{token.get('price_change_5m_pct', 0):+.2f}%",
                    "Buys/Sells (5m)": f"{token.get('buys_5m', 0)}/{token.get('sells_5m', 0)}",
                    "Reasons": ", ".join(token.get("reasons", [])) or "No strong signals",
                    "DexScreener": token.get("dexscreener_url", ""),
                }
            )

        table_df = pd.DataFrame(rows)
        styled_table = table_df.style.map(style_signal, subset=["Signal"]).map(style_momentum, subset=["Momentum"])
        st.dataframe(styled_table, use_container_width=True, hide_index=True)