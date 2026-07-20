import streamlit as st

st.set_page_config(
    page_title="Falcon",
    page_icon="🦅",
    layout="wide"
)

st.title("🦅 FALCON")

st.subheader("AI Market Intelligence")

st.divider()

col1, col2 = st.columns(2)

with col1:
    st.metric(
        "🔥 Opportunities Rising",
        "3",
        "+1"
    )

    st.metric(
        "📈 Market Mood",
        "Bullish"
    )

with col2:
    st.metric(
        "🏆 Top Falcon Rating",
        "94"
    )

    st.metric(
        "⭐ Watchlist",
        "12 Tokens"
    )

st.divider()

st.success("Falcon identified 3 opportunities today.")