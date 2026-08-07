import html
import os
from datetime import datetime, timezone
from textwrap import dedent
from pathlib import Path

import requests
import streamlit as st

from Scanner import scan_tokens

st.set_page_config(page_title="Falcon", page_icon="🦅", layout="wide")

REQUIRED_TOKEN_FIELDS = ("signal", "momentum", "confidence", "risk_label")
LIVE_REFRESH_MS = 2500
TELEGRAM_TEST_MESSAGE = (
    "🦅 Falcon AI Hunter\n"
    "✅ Test alert successful.\n"
    "Telegram notifications are working."
)


def load_env_value(name):
    value = os.getenv(name, "").strip()
    if value:
        return value

    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return ""

    try:
        with env_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, raw = line.split("=", 1)
                if key.strip() == name:
                    return raw.strip().strip('"').strip("'")
    except OSError:
        return ""

    return ""


def send_dashboard_test_telegram_alert():
    bot_token = load_env_value("TELEGRAM_BOT_TOKEN")
    chat_id = load_env_value("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing."

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": TELEGRAM_TEST_MESSAGE,
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=12)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("ok"):
            return True, "Telegram accepted the test alert."
        return False, f"Telegram returned a non-ok response: {data}"
    except Exception as error:
        return False, f"Telegram request failed: {error!r}"


def inject_css():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@500;600;700&display=swap');

        :root {
            --bg: #070a0f;
            --panel: #0e141d;
            --panel-2: #101924;
            --panel-3: #0b121b;
            --border: #1f2c3d;
            --text: #dfe9f7;
            --muted: #8fa3be;
            --ok: #42d989;
            --warn: #f1b74a;
            --bad: #ea4f68;
            --live: #33d17a;
            --safe-top: 32px;
        }

        .stApp {
            background:
                radial-gradient(1250px 540px at 8% -16%, #1a2d43 0%, rgba(14, 21, 31, 0.18) 46%, transparent 68%),
                linear-gradient(180deg, #070b11 0%, #06090e 100%);
            color: var(--text);
            font-size: 14px;
            font-family: 'IBM Plex Sans', 'Segoe UI', sans-serif;
        }

        .block-container {
            padding-top: var(--safe-top);
            padding-bottom: 0.35rem;
            max-width: 1420px;
        }

        .falcon-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 6px;
            padding: 6px 10px;
            border: 1px solid #2b3d53;
            border-radius: 9px;
            background: linear-gradient(180deg, #111b28 0%, #0d1622 100%);
            box-shadow: inset 0 0 0 1px rgba(174, 198, 230, 0.05);
            margin-bottom: 0;
        }

        .falcon-brand {
            display: flex;
            align-items: center;
            gap: 7px;
            min-width: 0;
        }

        .falcon-eye {
            font-size: 0.95rem;
            line-height: 1;
        }

        .falcon-title {
            font-size: 0.9rem;
            font-weight: 800;
            letter-spacing: 0.07em;
            white-space: nowrap;
        }

        .falcon-meta {
            font-size: 0.68rem;
            color: var(--muted);
            display: flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
        }

        .meta-sep {
            width: 1px;
            height: 11px;
            background: #2a384b;
            display: inline-block;
        }

        .meta-chip {
            color: #c2d4ea;
            font-weight: 700;
            letter-spacing: 0.02em;
            padding: 1px 6px;
            border-radius: 999px;
            border: 1px solid #2b3b50;
            background: #0f1a29;
        }

        .meta-chip .muted { color: #8599b4; font-weight: 600; margin-right: 3px; }

        .control-strip {
            margin-top: 4px;
            margin-bottom: 4px;
            border: 1px solid var(--border);
            border-radius: 8px;
            background: #0c131d;
            padding: 5px 7px;
        }

        div[data-testid="stSelectbox"] label p {
            font-size: 0.66rem;
            letter-spacing: 0.03em;
            text-transform: uppercase;
            color: #8fa4c0;
        }

        div[data-testid="stSelectbox"] > div > div,
        div[data-testid="stTextInput"] input {
            min-height: 30px;
            font-size: 0.74rem;
            border-radius: 7px;
            background: #0f1825;
            border-color: #2a3a4f;
        }

        div[data-testid="stButton"] button {
            min-height: 30px;
            border-radius: 7px;
            font-size: 0.7rem;
            font-weight: 700;
            letter-spacing: 0.03em;
        }

        div[data-testid="stToggle"] label p {
            font-size: 0.7rem;
        }

        .live-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            margin: 0;
            background: var(--live);
            box-shadow: 0 0 8px var(--live);
        }

        .section-title {
            display: flex;
            align-items: center;
            justify-content: space-between;
            color: #a8bdd8;
            font-size: 0.6rem;
            letter-spacing: 0.08em;
            font-weight: 800;
            text-transform: uppercase;
            margin: 5px 0 4px 0;
        }

        .section-title .right {
            color: #7f94b1;
            text-transform: none;
            font-weight: 700;
            letter-spacing: 0;
            font-size: 0.62rem;
        }

        .falcon-grid {
            display: grid;
            grid-template-columns: repeat(8, minmax(90px, 1fr));
            gap: 5px;
            margin: 3px 0 0 0;
        }

        .falcon-stat {
            border: 1px solid #27384c;
            border-radius: 7px;
            padding: 3px 6px;
            background: #0e161f;
            opacity: 0.9;
        }

        .falcon-stat .k {
            color: var(--muted);
            font-size: 0.62rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        .falcon-stat .v {
            font-size: 0.8rem;
            font-weight: 700;
            margin-top: 1px;
            font-family: 'IBM Plex Mono', monospace;
        }

        .opps-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(220px, 1fr));
            gap: 5px;
            margin: 1px 0 3px 0;
        }

        .opp-card {
            border: 1px solid #2b3d53;
            border-radius: 8px;
            background: linear-gradient(180deg, #121d2b 0%, #0f1824 100%);
            padding: 7px;
            min-width: 0;
            opacity: 0.95;
            box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.022), 0 8px 18px rgba(3, 8, 14, 0.34);
        }

        .opp-card.buy-now {
            border-color: #cc4e52;
            background: linear-gradient(180deg, #27121a 0%, #161622 100%);
            box-shadow: inset 0 0 0 1px rgba(255, 205, 138, 0.22), 0 0 0 1px rgba(204, 78, 82, 0.35), 0 10px 22px rgba(157, 37, 57, 0.26);
            opacity: 1;
        }

        .opp-head {
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .opp-avatar {
            width: 22px;
            height: 22px;
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            background: #17253a;
            color: #bdd0e7;
            font-size: 0.7rem;
            border: 1px solid #375073;
            font-weight: 800;
            flex: 0 0 auto;
        }

        .opp-top {
            display: flex;
            justify-content: space-between;
            gap: 6px;
            margin-bottom: 4px;
        }

        .opp-symbol {
            font-size: 0.78rem;
            font-weight: 700;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .opp-name {
            color: #8ea5c3;
            font-size: 0.58rem;
            margin-top: 1px;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }

        .opp-score {
            font-size: 1.7rem;
            font-weight: 900;
            line-height: 0.95;
            letter-spacing: 0.02em;
            font-family: 'IBM Plex Mono', monospace;
            text-shadow: 0 0 14px rgba(126, 226, 168, 0.2);
        }

        .opp-score small {
            font-size: 0.58rem;
            color: #8195b1;
            margin-left: 2px;
        }

        .opp-row {
            display: flex;
            justify-content: space-between;
            font-size: 0.69rem;
            color: var(--muted);
            margin-top: 2px;
            gap: 6px;
        }

        .opp-row span:last-child {
            font-family: 'IBM Plex Mono', monospace;
        }

        .opp-actions {
            margin-top: 4px;
            display: flex;
            gap: 5px;
            flex-wrap: wrap;
        }

        .fal-btn {
            border: 1px solid #2e425c;
            border-radius: 6px;
            padding: 1px 6px;
            color: #d6e5fb;
            font-size: 0.65rem;
            font-weight: 700;
            letter-spacing: 0.02em;
            text-decoration: none;
            background: #101a27;
            cursor: pointer;
        }

        .fal-btn:hover {
            border-color: #3d5878;
            background: #142032;
        }

        .fal-table-wrap {
            border: 1px solid #3a5070;
            border-radius: 8px;
            overflow: auto;
            background: #0e141e;
            margin-top: 1px;
            max-height: 56vh;
            box-shadow: 0 12px 26px rgba(3, 8, 14, 0.48);
        }

        .table-shell-title {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 4px 8px;
            border-bottom: 1px solid #2a3a52;
            background: linear-gradient(180deg, #101c2a 0%, #0e1722 100%);
            color: #adc3de;
            font-size: 0.62rem;
            text-transform: uppercase;
            letter-spacing: 0.07em;
            font-weight: 800;
        }

        .table-shell-title .right {
            text-transform: none;
            letter-spacing: 0;
            color: #7e94b2;
            font-size: 0.62rem;
            font-weight: 700;
        }

        table.falcon-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.78rem;
        }

        .falcon-table th,
        .falcon-table td {
            border-bottom: 1px solid #1d2838;
            padding: 5px 8px;
            text-align: left;
            vertical-align: middle;
        }

        .falcon-table th {
            color: #c5d4e8;
            font-size: 0.7rem;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            background: #141d2a;
            position: sticky;
            top: 0;
            z-index: 1;
            box-shadow: inset 0 -1px 0 #243247;
        }

        .falcon-table tbody tr:nth-child(4n + 1) td,
        .falcon-table tbody tr:nth-child(4n + 2) td {
            background: rgba(16, 24, 36, 0.24);
        }

        .falcon-table tbody tr:hover td {
            background: rgba(48, 66, 92, 0.2);
        }

        .falcon-table td:first-child span {
            font-size: 1.05rem;
            font-weight: 900;
            letter-spacing: 0.02em;
            font-family: 'IBM Plex Mono', monospace;
        }

        .badge {
            padding: 2px 7px;
            border-radius: 999px;
            font-size: 0.67rem;
            font-weight: 700;
            display: inline-block;
            border: 1px solid transparent;
            white-space: nowrap;
        }

        .sig-buy-now { background: #9a1a2e; border-color: #ff8b6f; color: #fff3cc; font-weight: 900; box-shadow: 0 0 10px rgba(255, 123, 94, 0.28); }
        .sig-buy { background: #124d34; border-color: #1f8056; color: #ddffef; }
        .sig-watch { background: #3c3215; border-color: #665828; color: #dccb9f; }
        .sig-pass { background: #1a212b; border-color: #2b394c; color: #7287a4; }

        .risk-low { background: #103f2d; color: #d6f7e8; }
        .risk-medium { background: #5b4718; color: #ffeec9; }
        .risk-high { background: #5f1f27; color: #ffd7dc; }

        .heat-viral { background: #7a0018; color: #ffe9bf; }
        .heat-hot { background: #8a2e00; color: #ffe9bf; }
        .heat-warm { background: #6c5a25; color: #fff2ce; }
        .heat-quiet { background: #29384f; color: #dce8f8; }

        .score-90 { color: #ffae69; font-weight: 800; }
        .score-70 { color: #7be2a7; font-weight: 700; }
        .score-low { color: #c9d5e8; }

        .move-pos { color: #7be2a7; font-weight: 800; }
        .move-neg { color: #f27f89; font-weight: 800; }
        .move-flat { color: #c9d5e8; }

        .row-buy-now td { background: rgba(122, 20, 40, 0.3); }
        .row-buy-now td:first-child { box-shadow: inset 3px 0 0 #ff7b5e; }
        .row-pass-subdued td { opacity: 0.44; }

        .desktop-table .row-details {
            margin: 3px 0;
            padding: 4px 6px;
        }

        .row-buy-now td:nth-child(2) .badge {
            box-shadow: 0 0 8px rgba(226, 106, 88, 0.34);
        }

        details.row-details {
            margin: 4px 0;
            padding: 5px 7px;
            border: 1px solid #2b3b52;
            border-radius: 8px;
            background: #0f1723;
        }

        details.row-details summary {
            cursor: pointer;
            font-size: 0.72rem;
            color: #d8e5f9;
            display: inline-block;
            border: 1px solid var(--border);
            border-radius: 6px;
            background: #0f1622;
            padding: 2px 7px;
            font-weight: 700;
        }

        .detail-grid {
            margin-top: 6px;
            display: grid;
            grid-template-columns: repeat(2, minmax(140px, 1fr));
            gap: 4px 10px;
            font-size: 0.72rem;
        }

        .detail-grid .label { color: #99abc5; }
        .detail-grid .value { color: #e7edf7; }

        .mobile-cards { display: none; }

        @media (max-width: 980px) {
            .falcon-grid { grid-template-columns: repeat(2, minmax(120px, 1fr)); gap: 6px; margin-top: 4px; }
            .opps-grid { grid-template-columns: 1fr; }
            .desktop-table { display: none; }
            .mobile-cards { display: block; }
            .fal-table-wrap { max-height: none; }
            .control-strip { padding: 4px 5px; margin-top: 3px; margin-bottom: 3px; }
            .falcon-header { padding: 6px 8px; }
            .opp-score { font-size: 1.55rem; }
            .mobile-token {
                border: 1px solid var(--border);
                border-radius: 10px;
                background: var(--panel);
                padding: 8px;
                margin-bottom: 7px;
            }
            .mobile-top {
                display: flex;
                justify-content: space-between;
                gap: 7px;
                margin-bottom: 4px;
            }
            .mobile-grid {
                display: grid;
                grid-template-columns: repeat(2, minmax(120px, 1fr));
                gap: 4px 8px;
                font-size: 0.76rem;
                color: var(--muted);
            }
            .mobile-actions { margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def inject_live_refresh(interval_ms):
    st.markdown(
        f"""
        <script>
        const falconRefreshMs = {int(interval_ms)};
        if (!window.__falcon_refresh_timer__) {{
            window.__falcon_refresh_timer__ = setTimeout(() => window.location.reload(), falconRefreshMs);
        }}
        </script>
        """,
        unsafe_allow_html=True,
    )


def inject_copy_script():
    st.markdown(
        """
        <script>
        document.addEventListener('click', function (ev) {
            const button = ev.target.closest('[data-copy]');
            if (!button) return;
            const value = button.getAttribute('data-copy') || '';
            if (!value) return;
            navigator.clipboard.writeText(value);
            button.innerText = 'Copied';
            setTimeout(() => { button.innerText = 'Copy'; }, 900);
        });
        </script>
        """,
        unsafe_allow_html=True,
    )


def load_scan(force_refresh=False):
    if force_refresh or "scan_payload" not in st.session_state:
        st.session_state.scan_payload = scan_tokens()

    payload = st.session_state.scan_payload
    if payload_needs_refresh(payload):
        payload = scan_tokens()
        st.session_state.scan_payload = payload
    return payload


def payload_needs_refresh(payload):
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
    if not iso_value:
        return "N/A"
    try:
        parsed = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%H:%M:%S UTC")
    except ValueError:
        return str(iso_value)


def to_int(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def fmt_money(value):
    num = float(value or 0)
    if num >= 1_000_000:
        return f"${num / 1_000_000:.2f}M"
    if num >= 1_000:
        return f"${num / 1_000:.1f}k"
    return f"${num:.0f}"


def fmt_age_minutes(value):
    if value is None:
        return "N/A"
    mins = float(value)
    if mins >= 60:
        return f"{mins / 60:.1f}h"
    return f"{mins:.0f}m"


def strongest_signal(tokens):
    ranking = {"BUY NOW": 4, "BUY": 3, "WATCH": 2, "PASS": 1}
    best = "PASS"
    for token in tokens:
        signal = str(token.get("signal", "PASS"))
        if ranking.get(signal, 0) > ranking.get(best, 0):
            best = signal
    return best


def market_temperature(tokens):
    if not tokens:
        return "QUIET"
    avg = sum(float(token.get("social_heat_score", 0) or 0) for token in tokens) / len(tokens)
    if avg >= 85:
        return "VIRAL"
    if avg >= 65:
        return "HOT"
    if avg >= 40:
        return "WARM"
    return "QUIET"


def signal_badge(signal):
    classes = {
        "BUY NOW": "sig-buy-now",
        "BUY": "sig-buy",
        "WATCH": "sig-watch",
        "PASS": "sig-pass",
    }
    c = classes.get(signal, "sig-pass")
    return f'<span class="badge {c}">{html.escape(signal)}</span>'


def risk_badge(risk):
    classes = {"LOW": "risk-low", "MEDIUM": "risk-medium", "HIGH": "risk-high"}
    c = classes.get(risk, "risk-high")
    return f'<span class="badge {c}">{html.escape(risk)}</span>'


def heat_badge(heat_text):
    text = str(heat_text)
    upper = text.upper()
    c = "heat-quiet"
    if "VIRAL" in upper:
        c = "heat-viral"
    elif "HOT" in upper:
        c = "heat-hot"
    elif "WARM" in upper:
        c = "heat-warm"
    return f'<span class="badge {c}">{html.escape(text)}</span>'


def score_style(score):
    if score >= 90:
        return "score-90"
    if score >= 70:
        return "score-70"
    return "score-low"


def move_class(value):
    val = float(value or 0)
    if val > 0:
        return "move-pos"
    if val < 0:
        return "move-neg"
    return "move-flat"


def chart_link_href(token):
    dexscreener_url = str(token.get("dexscreener_url", "") or "")
    contract = str(token.get("contract_address", "") or "").strip()
    if contract and contract.upper() != "N/A" and "/solana/" in dexscreener_url.lower():
        return f"https://gmgn.ai/sol/token/{contract}"
    return dexscreener_url


def render_top_cards(tokens):
    top = sorted(tokens, key=lambda t: to_int(t.get("score", 0)), reverse=True)[:3]
    cards = []
    for token in top:
        symbol = str(token.get("token_symbol", "UNKNOWN"))
        name = str(token.get("token_name", "Unknown"))
        contract = str(token.get("contract_address", ""))
        signal = str(token.get("signal", "PASS"))
        score = to_int(token.get("score", 0))
        move = float(token.get("price_change_5m_pct", 0) or 0)
        card_class = "opp-card buy-now" if signal == "BUY NOW" else "opp-card"
        avatar = html.escape(symbol[:1] if symbol else "?")
        cards.append(
            dedent(
                f"""
                <div class="{card_class}">
                    <div class="opp-top">
                        <div class="opp-head">
                            <span class="opp-avatar">{avatar}</span>
                            <div>
                                <div class="opp-symbol">{html.escape(name)}</div>
                                <div class="opp-name">{html.escape(symbol)}</div>
                            </div>
                        </div>
                        <div class="opp-score {score_style(score)}">{score}<small>/100</small></div>
                    </div>
                    <div>{signal_badge(signal)}</div>
                    <div class="opp-row"><span>MCap</span><span>{fmt_money(token.get('market_cap_usd', 0))}</span></div>
                    <div class="opp-row"><span>Liq</span><span>{fmt_money(token.get('liquidity_usd', 0))}</span></div>
                    <div class="opp-row"><span>5m</span><span class="{move_class(move)}">{move:+.2f}%</span></div>
                    <div class="opp-actions">
                        <button class="fal-btn" data-copy="{html.escape(contract)}">Copy</button>
                        <a class="fal-btn" href="{html.escape(chart_link_href(token))}" target="_blank">View on GMGN ↗</a>
                    </div>
                </div>
                """
            ).strip()
        )
    st.markdown('<div class="opps-grid">' + "".join(cards) + "</div>", unsafe_allow_html=True)


def render_desktop_table(tokens):
    rows_html = []
    for token in tokens:
        score = to_int(token.get("score", 0))
        signal = str(token.get("signal", "PASS"))
        risk = str(token.get("risk_label", "HIGH"))
        symbol = str(token.get("token_symbol", "UNKNOWN"))
        name = str(token.get("token_name", "Unknown"))
        contract = str(token.get("contract_address", ""))
        heat = str(token.get("social_heat", "⚪ QUIET"))
        heat_reasons = " | ".join(token.get("social_heat_reasons", []) or ["No social reasons"])
        high_priority = "HIGH PRIORITY" if bool(token.get("high_priority_alert", False)) else ""
        move = float(token.get("price_change_5m_pct", 0) or 0)
        row_state = ""
        if signal == "BUY NOW":
            row_state = ' class="row-buy-now"'
        elif signal == "PASS" and score < 60 and not bool(token.get("high_priority_alert", False)):
            row_state = ' class="row-pass-subdued"'
        action_html = (
            f'<button class="fal-btn" data-copy="{html.escape(contract)}">Copy</button> '
            f'<a class="fal-btn" href="{html.escape(chart_link_href(token))}" target="_blank">View on GMGN ↗</a>'
        )

        row = dedent(
            f"""
            <tr{row_state}>
                <td><span class="{score_style(score)}">{score}</span></td>
                <td>{signal_badge(signal)}</td>
                <td>{risk_badge(risk)}</td>
                <td>{html.escape(name)} ({html.escape(symbol)})</td>
                <td>{fmt_money(token.get('market_cap_usd', 0))}</td>
                <td>{fmt_money(token.get('liquidity_usd', 0))}</td>
                <td><span class="{move_class(move)}">{move:+.2f}%</span></td>
                <td>{to_int(token.get('buys_5m', 0))} / {to_int(token.get('sells_5m', 0))}</td>
                <td>{html.escape(str(token.get('smart_wallet_display', '-')))}</td>
                <td><span title="{html.escape(heat_reasons)}">{heat_badge(heat)}</span></td>
                <td>{fmt_age_minutes(token.get('pair_age_minutes'))}</td>
                <td>{action_html}</td>
            </tr>
            """
        ).strip()

        details = dedent(
            f"""
            <tr>
                <td colspan="12">
                    <details class="row-details">
                        <summary>Details</summary>
                        <div class="detail-grid">
                            <div class="label">Contract</div><div class="value">{html.escape(contract)}</div>
                            <div class="label">Confidence</div><div class="value">{to_int(token.get('confidence', 0))}</div>
                            <div class="label">5m Volume</div><div class="value">{fmt_money(token.get('volume_5m_usd', 0))}</div>
                            <div class="label">Momentum</div><div class="value">{html.escape(str(token.get('momentum', 'NEUTRAL')))}</div>
                            <div class="label">Intelligence Reasons</div><div class="value">{html.escape(', '.join(token.get('falcon_intelligence_reasons', []) or []))}</div>
                            <div class="label">BUY NOW Reasons</div><div class="value">{html.escape(', '.join(token.get('buy_now_reasons', []) or []))}</div>
                            <div class="label">Priority Reasons</div><div class="value">{html.escape(', '.join(token.get('high_priority_reasons', []) or []))}</div>
                            <div class="label">Transactions</div><div class="value">Buys {to_int(token.get('buys_5m', 0))}, Sells {to_int(token.get('sells_5m', 0))}</div>
                            <div class="label">DexScreener</div><div class="value"><a class="fal-btn" href="{html.escape(chart_link_href(token))}" target="_blank">View on GMGN ↗</a></div>
                            <div class="label">Copy Contract</div><div class="value"><button class="fal-btn" data-copy="{html.escape(contract)}">Copy</button></div>
                            <div class="label">High Priority</div><div class="value">{html.escape(high_priority)}</div>
                            <div class="label">Social Heat Score</div><div class="value">{to_int(token.get('social_heat_score', 0))}</div>
                        </div>
                    </details>
                </td>
            </tr>
            """
        ).strip()
        rows_html.append(row + details)

    table_html = (
        '<div class="desktop-table fal-table-wrap">'
        '<div class="table-shell-title"><span>Live Token Scanner</span><span class="right">Dense Mode</span></div>'
        '<table class="falcon-table">'
        "<thead><tr>"
        "<th>Score</th><th>Signal</th><th>Risk</th><th>Token</th><th>MCap</th><th>Liq</th>"
        "<th>5m %</th><th>Buys/Sells</th><th>Smart</th><th>Social</th><th>Age</th><th>Actions</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table></div>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


def render_mobile_cards(tokens):
    cards = []
    for token in tokens:
        score = to_int(token.get("score", 0))
        signal = str(token.get("signal", "PASS"))
        heat = str(token.get("social_heat", "⚪ QUIET"))
        move = float(token.get("price_change_5m_pct", 0) or 0)
        contract = str(token.get("contract_address", ""))
        details = html.escape(", ".join(token.get("falcon_intelligence_reasons", []) or []))
        card = dedent(
            f"""
            <div class="mobile-token">
                <div class="mobile-top">
                    <div><strong>{html.escape(str(token.get('token_name', 'Unknown')))} ({html.escape(str(token.get('token_symbol', 'UNKNOWN')))} )</strong></div>
                    <div class="{score_style(score)}">{score}</div>
                </div>
                <div>{signal_badge(signal)} {risk_badge(str(token.get('risk_label', 'HIGH')))}</div>
                <div class="mobile-grid">
                    <div>MCap: {fmt_money(token.get('market_cap_usd', 0))}</div>
                    <div>Liq: {fmt_money(token.get('liquidity_usd', 0))}</div>
                    <div>5m: <span class="{move_class(move)}">{move:+.2f}%</span></div>
                    <div>Smart: {html.escape(str(token.get('smart_wallet_display', '-')))}</div>
                    <div>Social: {heat_badge(heat)}</div>
                    <div>Age: {fmt_age_minutes(token.get('pair_age_minutes'))}</div>
                </div>
                <div class="mobile-actions">
                    <button class="fal-btn" data-copy="{html.escape(contract)}">Copy</button>
                    <a class="fal-btn" href="{html.escape(chart_link_href(token))}" target="_blank">View on GMGN ↗</a>
                </div>
                <details class="row-details">
                    <summary>Details</summary>
                    <div class="detail-grid">
                        <div class="label">Contract</div><div class="value">{html.escape(contract)}</div>
                        <div class="label">Confidence</div><div class="value">{to_int(token.get('confidence', 0))}</div>
                        <div class="label">Volume</div><div class="value">{fmt_money(token.get('volume_5m_usd', 0))}</div>
                        <div class="label">Intelligence</div><div class="value">{details}</div>
                        <div class="label">BUY NOW Reasons</div><div class="value">{html.escape(', '.join(token.get('buy_now_reasons', []) or []))}</div>
                        <div class="label">Priority Reasons</div><div class="value">{html.escape(', '.join(token.get('high_priority_reasons', []) or []))}</div>
                    </div>
                </details>
            </div>
            """
        ).strip()
        cards.append(card)
    st.markdown('<div class="mobile-cards">' + "".join(cards) + "</div>", unsafe_allow_html=True)


def render_scanner_status(scanner_status, scanner_elapsed_ms):
    if not scanner_status:
        return

    rows = []
    for status in scanner_status:
        source = html.escape(str(status.get("source", "unknown")))
        configured = bool(status.get("configured", False))
        success = bool(status.get("success", False))
        found = to_int(status.get("candidates_found", 0))
        elapsed = to_int(status.get("elapsed_ms", 0))
        error = html.escape(str(status.get("error", "") or ""))
        state = "OK" if success else "FAIL"
        configured_label = "YES" if configured else "NO"
        rows.append(
            "<tr>"
            f"<td>{source}</td>"
            f"<td>{configured_label}</td>"
            f"<td>{state}</td>"
            f"<td>{found}</td>"
            f"<td>{elapsed} ms</td>"
            f"<td>{error}</td>"
            "</tr>"
        )

    status_html = (
        '<div class="desktop-table fal-table-wrap">'
        '<div class="table-shell-title"><span>Scanner Status</span>'
        f'<span class="right">Total {to_int(scanner_elapsed_ms)} ms</span></div>'
        '<table class="falcon-table">'
        '<thead><tr><th>Source</th><th>Configured</th><th>Result</th><th>Found</th><th>Elapsed</th><th>Error</th></tr></thead>'
        '<tbody>'
        + "".join(rows)
        + '</tbody></table></div>'
    )
    st.markdown(status_html, unsafe_allow_html=True)


inject_css()
inject_copy_script()

left, right = st.columns([7, 3])
with left:
    st.markdown(
        '<div class="falcon-header">'
        '<div class="falcon-brand">'
        '<span class="falcon-eye">🦅</span>'
        '<span class="falcon-title">FALCON AI HUNTER</span>'
        '</div>'
        '<div class="falcon-meta">'
        '<span class="live-dot"></span><span class="meta-chip">LIVE</span>'
        '<span class="meta-sep"></span>'
        '<span class="meta-chip"><span class="muted">Mission</span>Terminal</span>'
        '</div>'
        '</div>',
        unsafe_allow_html=True,
    )

with right:
    refresh_clicked = st.button("Refresh", type="primary", use_container_width=True)
    live_mode = st.toggle("Live 2.5s", value=True)
    test_telegram_clicked = st.button("TEST TELEGRAM ALERT", use_container_width=True)

if test_telegram_clicked:
    sent_ok, sent_message = send_dashboard_test_telegram_alert()
    if sent_ok:
        st.success(sent_message)
    else:
        st.error(sent_message)

if live_mode:
    inject_live_refresh(LIVE_REFRESH_MS)

payload = load_scan(force_refresh=refresh_clicked or live_mode)

if not payload.get("ok"):
    st.error(payload.get("error", "Scan failed due to an unknown error."))
    st.stop()

tokens = payload.get("tokens", [])
if not tokens:
    st.info("No opportunities found in the latest scan.")
    st.stop()

signal_filter_col, conviction_filter_col = st.columns([1, 1])
with signal_filter_col:
    signal_filter = st.selectbox("Signal", ["ALL", "BUY NOW", "BUY", "WATCH", "PASS"], index=0)
with conviction_filter_col:
    conviction_filter = st.selectbox("Conviction", ["ALL", "LEGENDARY", "ELITE", "STRONG", "WATCH"], index=0)

filtered_tokens = tokens
if signal_filter != "ALL":
    filtered_tokens = [t for t in filtered_tokens if str(t.get("signal", "PASS")).upper() == signal_filter]
if conviction_filter != "ALL":
    filtered_tokens = [
        t for t in filtered_tokens if str(t.get("conviction_rating", "WATCH")).upper() == conviction_filter
    ]

filtered_tokens = sorted(filtered_tokens, key=lambda t: to_int(t.get("score", 0)), reverse=True)
if not filtered_tokens:
    st.info("No opportunities match current filters.")
    st.stop()

scan_time = format_scan_time(payload.get("scanned_at"))
strongest = strongest_signal(filtered_tokens)
temperature = market_temperature(filtered_tokens)

st.markdown('<div class="section-title"><span>Top 3 Opportunities</span><span class="right">View All Opportunities →</span></div>', unsafe_allow_html=True)

render_top_cards(filtered_tokens)

summary_html = f"""
<div class="falcon-grid">
    <div class="falcon-stat"><div class="k">Tokens Scanned</div><div class="v">{len(filtered_tokens)}</div></div>
    <div class="falcon-stat"><div class="k">Strongest Signal</div><div class="v">{html.escape(strongest)}</div></div>
    <div class="falcon-stat"><div class="k">New Tokens</div><div class="v">{to_int(payload.get('new_tokens_detected', 0))}</div></div>
    <div class="falcon-stat"><div class="k">Last Scan</div><div class="v">{html.escape(scan_time)}</div></div>
    <div class="falcon-stat"><div class="k">Market Temp</div><div class="v">{html.escape(temperature)}</div></div>
    <div class="falcon-stat"><div class="k">BUY NOW</div><div class="v">{sum(1 for t in filtered_tokens if str(t.get('signal', '')).upper() == 'BUY NOW')}</div></div>
    <div class="falcon-stat"><div class="k">High Priority</div><div class="v">{sum(1 for t in filtered_tokens if bool(t.get('high_priority_alert', False)))}</div></div>
    <div class="falcon-stat"><div class="k">Avg Score</div><div class="v">{round(sum(to_int(t.get('score', 0)) for t in filtered_tokens) / len(filtered_tokens), 1)}</div></div>
</div>
"""
st.markdown(summary_html, unsafe_allow_html=True)

render_scanner_status(payload.get("scanner_status", []), payload.get("scanner_elapsed_ms", 0))

render_desktop_table(filtered_tokens)
render_mobile_cards(filtered_tokens)