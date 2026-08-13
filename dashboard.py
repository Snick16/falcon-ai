import html
import os
import base64
from datetime import datetime, timezone
from textwrap import dedent
from pathlib import Path

import requests
import streamlit as st

from Scanner import (
    apply_surge_settings,
    get_default_surge_settings,
    get_surge_settings,
    reset_surge_settings,
    scan_tokens,
)

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
            --bg: #020202;
            --panel: #0a0a0a;
            --panel-2: #0c0c0c;
            --panel-3: #080808;
            --border: #2aff84;
            --border-soft: #1f8f4f;
            --text: #f2f2f2;
            --muted: #a3a3a3;
            --ok: #66ff9f;
            --warn: #d7d76e;
            --bad: #ff5d5d;
            --eye: #ff5e1a;
            --eye-bright: #ff6f2d;
            --safe-top: 14px;
        }

        .stApp {
            background: #000000;
            color: var(--text);
            font-size: 14px;
            font-family: 'IBM Plex Sans', 'Segoe UI', sans-serif;
        }

        .block-container {
            padding-top: var(--safe-top);
            padding-bottom: 0.4rem;
            max-width: 1540px;
        }

        .fal-shell {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .fal-panel {
            border: 1px solid var(--border-soft);
            border-radius: 11px;
            background: #000000;
            box-shadow: none;
            padding: 5px 8px;
        }

        .fal-title-wrap {
            margin: 0;
            padding: 0;
            position: relative;
            width: 100%;
            overflow: hidden;
            background: #000000;
            min-height: 172px;
        }

        .fal-header-image-wrap {
            position: absolute;
            inset: 0;
            width: 100% !important;
            height: 100% !important;
            max-width: none !important;
            background: #000000;
            min-height: 0;
            padding: 0 !important;
            margin: 0 !important;
            overflow: hidden;
        }

        .fal-header-image-wrap.normal {
            box-shadow: inset 0 0 10px rgba(255, 94, 26, 0.14);
        }

        .fal-header-image-wrap.high-priority {
            box-shadow: inset 0 0 14px rgba(255, 94, 26, 0.24), 0 0 16px rgba(255, 94, 26, 0.22);
            animation: falEyePulse 1.3s ease-in-out infinite;
        }

        @keyframes falEyePulse {
            0%, 100% { filter: saturate(1); }
            50% { filter: saturate(1.35) brightness(1.1); }
        }

        .fal-eye-img {
            position: absolute;
            left: 0;
            right: 0;
            top: 0;
            bottom: 0;
            width: 100% !important;
            height: 100% !important;
            max-width: none !important;
            margin: 0 !important;
            padding: 0 !important;
            object-fit: fill !important;
            object-position: center;
            opacity: 0.98;
            filter: drop-shadow(0 0 8px rgba(255, 94, 26, 0.45));
            z-index: 1;
            display: block;
            transform: none;
        }

        .fal-header-image-wrap.high-priority .fal-eye-img {
            filter: drop-shadow(0 0 12px rgba(255, 94, 26, 0.78));
        }

        .fal-header-image-wrap.no-image {
            background: #000000;
            box-shadow: inset 0 0 10px rgba(255, 94, 26, 0.12);
        }

        .fal-header-image-wrap.no-image.high-priority {
            box-shadow: inset 0 0 14px rgba(255, 94, 26, 0.28), 0 0 14px rgba(255, 94, 26, 0.2);
        }

        .fal-controls-panel {
            padding: 6px 8px;
        }

        .fal-top-row,
        .fal-bottom-row {
            display: flex;
            justify-content: space-between;
            gap: 8px;
            align-items: center;
        }

        .fal-bottom-row {
            margin-top: 6px;
        }

        .fal-status-left,
        .fal-status-right,
        .fal-center-nav {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
        }

        .status-chip {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 3px 9px;
            border-radius: 999px;
            border: 1px solid var(--border-soft);
            background: #000000;
            color: #c9ffd9;
            font-size: 0.63rem;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            text-decoration: none;
            white-space: nowrap;
        }

        .status-chip.active {
            border-color: #3ef08d;
            box-shadow: none;
        }

        .status-chip.dim {
            color: #86b997;
        }

        .icon-chip {
            width: 30px;
            height: 30px;
            border-radius: 8px;
            border: 1px solid var(--border-soft);
            background: #000000;
            color: #c9ffd9;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            text-decoration: none;
            font-weight: 800;
            font-size: 0.82rem;
            line-height: 1;
        }

        .icon-chip:hover {
            border-color: #3ef08d;
            box-shadow: none;
        }

        .control-strip {
            border: 1px solid var(--border-soft);
            border-radius: 11px;
            background: #000000;
            padding: 5px 7px;
        }

        .fal-filter-shell {
            border: 1px solid var(--border-soft);
            border-radius: 11px;
            background: #000000;
            padding: 5px 7px;
            margin-top: 1px;
        }

        .fal-filter-status {
            height: 100%;
            border: 1px solid var(--border-soft);
            border-radius: 8px;
            background: #000000;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 44px;
            padding: 4px 6px;
        }

        .fal-filter-status .status-chip {
            width: 100%;
            justify-content: center;
            font-size: 0.61rem;
            padding: 3px 6px;
        }

        div[data-testid="stSelectbox"] label p {
            font-size: 0.63rem;
            letter-spacing: 0.09em;
            text-transform: uppercase;
            color: #a7ffca;
        }

        div[data-testid="stSelectbox"] > div > div,
        div[data-testid="stTextInput"] input {
            min-height: 30px;
            font-size: 0.74rem;
            border-radius: 7px;
            background: #000000;
            border-color: var(--border-soft);
            color: var(--text);
        }

        div[data-testid="stButton"] button {
            min-height: 30px;
            border-radius: 7px;
            font-size: 0.69rem;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            border: 1px solid var(--border-soft);
            background: #000000;
            color: #dcffe8;
            box-shadow: none;
        }

        div[data-testid="stButton"] button:hover {
            border-color: #3ef08d;
            box-shadow: none;
        }

        div[data-testid="stToggle"] label p {
            font-size: 0.66rem;
            color: #b6ffd0;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }

        .section-title {
            display: flex;
            align-items: center;
            justify-content: space-between;
            color: #beffd7;
            font-size: 0.6rem;
            letter-spacing: 0.08em;
            font-weight: 800;
            text-transform: uppercase;
            margin: 2px 0 3px 0;
        }

        .section-title .right {
            color: #98d7ad;
            text-transform: none;
            font-weight: 700;
            letter-spacing: 0;
            font-size: 0.62rem;
        }

        .fal-nav-strip-wrap {
            text-align: center;
            margin-top: 1px;
        }

        .fal-nav-strip {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
            border: 1px solid var(--border-soft);
            border-radius: 11px;
            padding: 5px 8px;
            background: #000000;
        }

        .falcon-grid {
            display: grid;
            grid-template-columns: repeat(8, minmax(90px, 1fr));
            gap: 5px;
            margin: 0;
        }

        .falcon-stat {
            border: 1px solid var(--border-soft);
            border-radius: 8px;
            padding: 4px 6px;
            background: #000000;
        }

        .falcon-stat .k {
            color: #a7ffca;
            font-size: 0.62rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        .falcon-stat .v {
            font-size: 0.82rem;
            font-weight: 700;
            margin-top: 1px;
            font-family: 'IBM Plex Mono', monospace;
            color: #efefef;
        }

        .opps-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(230px, 1fr));
            gap: 8px;
            margin: 0;
        }

        .opp-card {
            border: 1px solid var(--border-soft);
            border-radius: 10px;
            background: #000000;
            padding: 9px;
            min-width: 0;
            min-height: 252px;
            display: flex;
            flex-direction: column;
            box-shadow: none;
        }

        .opp-card.buy-now {
            border-color: #3ef08d;
            box-shadow: none;
        }

        .opp-head {
            display: flex;
            align-items: center;
            gap: 7px;
        }

        .opp-avatar {
            width: 24px;
            height: 24px;
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            background: #000000;
            color: #c4ffd8;
            font-size: 0.7rem;
            border: 1px solid var(--border-soft);
            font-weight: 800;
            flex: 0 0 auto;
        }

        .opp-top {
            display: flex;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 6px;
        }

        .opp-symbol {
            font-size: 0.8rem;
            font-weight: 700;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .opp-name {
            color: #a6ffc9;
            font-size: 0.58rem;
            margin-top: 1px;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }

        .opp-score {
            font-size: 2.2rem;
            font-weight: 900;
            line-height: 0.92;
            letter-spacing: 0.01em;
            font-family: 'IBM Plex Mono', monospace;
            color: #75ffab;
            text-shadow: none;
        }

        .opp-score small {
            font-size: 0.58rem;
            color: #a0cfae;
            margin-left: 2px;
        }

        .opp-metrics {
            margin-top: 6px;
            display: grid;
            grid-template-columns: repeat(2, minmax(92px, 1fr));
            gap: 4px 9px;
            flex: 1;
            align-content: start;
        }

        .opp-row {
            display: flex;
            justify-content: space-between;
            font-size: 0.69rem;
            color: #dddddd;
            gap: 6px;
            border-bottom: 1px solid rgba(42, 255, 132, 0.12);
            padding-bottom: 2px;
        }

        .opp-row span:last-child {
            font-family: 'IBM Plex Mono', monospace;
        }

        .opp-actions {
            margin-top: auto;
            display: flex;
            gap: 7px;
            justify-content: space-between;
        }

        .fal-btn {
            border: 1px solid var(--border-soft);
            border-radius: 7px;
            padding: 2px 7px;
            color: #dcffe8;
            font-size: 0.65rem;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            text-decoration: none;
            background: #000000;
            cursor: pointer;
            text-align: center;
            min-width: 96px;
        }

        .fal-btn:hover {
            border-color: #3ef08d;
            box-shadow: none;
        }

        .fal-table-wrap {
            border: 1px solid var(--border-soft);
            border-radius: 10px;
            overflow: auto;
            background: #000000;
            margin-top: 1px;
            max-height: 56vh;
            box-shadow: none;
        }

        .table-shell-title {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 4px 8px;
            border-bottom: 1px solid var(--border-soft);
            background: #000000;
            color: #beffd7;
            font-size: 0.62rem;
            text-transform: uppercase;
            letter-spacing: 0.07em;
            font-weight: 800;
        }

        .table-shell-title .right {
            text-transform: none;
            letter-spacing: 0;
            color: #95cfaa;
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
            border-bottom: 1px solid rgba(42, 255, 132, 0.14);
            padding: 5px 8px;
            text-align: left;
            vertical-align: middle;
        }

        .falcon-table th {
            color: #beffd7;
            font-size: 0.7rem;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            background: #000000;
            position: sticky;
            top: 0;
            z-index: 1;
            box-shadow: none;
        }

        .falcon-table tbody tr:nth-child(4n + 1) td,
        .falcon-table tbody tr:nth-child(4n + 2) td {
            background: #070707;
        }

        .falcon-table tbody tr:hover td {
            background: #101010;
        }

        .falcon-table td {
            color: #e9e9e9;
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

        .sig-buy-now { background: #050505; border-color: #3ef08d; color: #e9fff0; font-weight: 900; box-shadow: none; }
        .sig-buy { background: #111111; border-color: #2d8459; color: #e3fff1; }
        .sig-watch { background: #111111; border-color: #476a4a; color: #d8f0d9; }
        .sig-pass { background: #111111; border-color: #2c4737; color: #b6b6b6; }

        .risk-low { background: #111111; color: #d6f7e8; }
        .risk-medium { background: #111111; color: #ecf8c8; }
        .risk-high { background: #111111; color: #f0efc9; }

        .heat-viral { background: #111111; color: #dcffdf; }
        .heat-hot { background: #111111; color: #d7ffd8; }
        .heat-warm { background: #111111; color: #d2f4d2; }
        .heat-quiet { background: #111111; color: #c2c2c2; }

        .score-90 { color: #75ffab; font-weight: 900; text-shadow: none; }
        .score-70 { color: #95ffbb; font-weight: 800; }
        .score-low { color: #cfe3d4; }

        .move-pos { color: #91ffb4; font-weight: 800; }
        .move-neg { color: var(--bad); font-weight: 800; }
        .move-flat { color: #cfe3d4; }

        .surge-wrap {
            border: 1px solid var(--border-soft);
            border-radius: 10px;
            background: #000000;
            margin-top: 7px;
            overflow: auto;
        }

        .surge-title {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 4px 8px;
            border-bottom: 1px solid var(--border-soft);
            color: #beffd7;
            font-size: 0.62rem;
            text-transform: uppercase;
            letter-spacing: 0.07em;
            font-weight: 800;
        }

        .surge-title .right {
            text-transform: none;
            letter-spacing: 0;
            color: #95cfaa;
            font-size: 0.62rem;
            font-weight: 700;
        }

        table.surge-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.74rem;
        }

        .surge-table th,
        .surge-table td {
            border-bottom: 1px solid rgba(42, 255, 132, 0.14);
            padding: 5px 8px;
            text-align: left;
        }

        .surge-table th {
            color: #beffd7;
            font-size: 0.67rem;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            background: #000000;
        }

        .surge-watch { color: #d7d76e; font-weight: 800; }
        .surge-surge { color: #9cffb4; font-weight: 900; }
        .surge-breakout { color: #ffb067; font-weight: 900; }

        .row-buy-now td { background: rgba(16, 16, 16, 0.92); }
        .row-buy-now td:first-child { box-shadow: inset 3px 0 0 var(--ok); }
        .row-pass-subdued td { opacity: 0.5; }

        .desktop-table .row-details {
            margin: 3px 0;
            padding: 4px 6px;
        }

        .row-buy-now td:nth-child(2) .badge {
            box-shadow: none;
        }

        details.row-details {
            margin: 4px 0;
            padding: 5px 7px;
            border: 1px solid var(--border-soft);
            border-radius: 10px;
            background: #000000;
        }

        details.row-details summary {
            cursor: pointer;
            font-size: 0.72rem;
            color: #c6ffdc;
            display: inline-block;
            border: 1px solid var(--border-soft);
            border-radius: 7px;
            background: #000000;
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

        .detail-grid .label { color: #a1f8c3; }
        .detail-grid .value { color: #efefef; }

        .mobile-only { display: none; }
        .mobile-cards { display: none; }

        @media (max-width: 980px) {
            .fal-top-row,
            .fal-bottom-row {
                flex-direction: column;
                align-items: stretch;
            }
            .mobile-only { display: flex; }
            .falcon-grid { grid-template-columns: repeat(2, minmax(120px, 1fr)); gap: 6px; margin-top: 4px; }
            .opps-grid { grid-template-columns: 1fr; }
            .desktop-table { display: none; }
            .mobile-cards { display: block; }
            .fal-table-wrap { max-height: none; }
            .control-strip { padding: 4px 5px; margin-top: 2px; margin-bottom: 2px; }
            .fal-title-wrap { min-height: 118px; padding: 0; }
            .fal-header-image-wrap { min-height: 0; height: 100% !important; }
            .fal-eye-img { width: 100% !important; height: 100% !important; max-width: none !important; object-fit: fill !important; transform: none; }
            .opp-score { font-size: 1.7rem; }
            .fal-nav-strip { width: 100%; justify-content: center; }
            .mobile-token {
                border: 1px solid var(--border-soft);
                border-radius: 12px;
                background: #0b0b0b;
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


def surge_level_class(level):
    value = str(level or "NONE").upper()
    if value == "BREAKOUT":
        return "surge-breakout"
    if value == "SURGE":
        return "surge-surge"
    if value == "WATCH":
        return "surge-watch"
    return ""


def render_surge_section(tokens):
    candidates = [
        token
        for token in tokens
        if str(token.get("surge_level", "NONE")).upper() in {"WATCH", "SURGE", "BREAKOUT"}
    ]
    if not candidates:
        return

    rank = {"BREAKOUT": 3, "SURGE": 2, "WATCH": 1}
    candidates = sorted(
        candidates,
        key=lambda token: (
            rank.get(str(token.get("surge_level", "NONE")).upper(), 0),
            to_int(token.get("surge_rating", 0)),
            float(token.get("surge_market_cap_change_pct", 0) or 0),
        ),
        reverse=True,
    )[:8]

    rows = []
    for token in candidates:
        symbol = html.escape(str(token.get("token_symbol", "UNKNOWN") or "UNKNOWN"))
        market_cap = fmt_money(token.get("market_cap_usd", 0))
        mc_accel = float(token.get("surge_market_cap_change_pct", 0) or 0)
        vol_accel = float(token.get("surge_volume_acceleration", 0) or 0)
        buy_pressure = float(token.get("surge_buy_pressure_ratio", 0) or 0)
        level = str(token.get("surge_level", "NONE") or "NONE").upper()
        rating = to_int(token.get("surge_rating", 0))
        rows.append(
            "<tr>"
            f"<td>{symbol}</td>"
            f"<td>{market_cap}</td>"
            f"<td class='{move_class(mc_accel)}'>{mc_accel:+.2f}%</td>"
            f"<td>{vol_accel:.2f}x</td>"
            f"<td>{buy_pressure:.2f}x</td>"
            f"<td class='{surge_level_class(level)}'>{html.escape(level)}</td>"
            f"<td>{rating}/100</td>"
            "</tr>"
        )

    surge_html = (
        '<div class="surge-wrap">'
        '<div class="surge-title"><span>Surge Candidates</span><span class="right">WATCH · SURGE · BREAKOUT</span></div>'
        '<table class="surge-table">'
        '<thead><tr><th>Symbol</th><th>MC</th><th>MC Accel</th><th>Vol Accel</th><th>Buy Pressure</th><th>Level</th><th>Rating</th></tr></thead>'
        '<tbody>' + "".join(rows) + '</tbody></table></div>'
    )
    st.markdown(surge_html, unsafe_allow_html=True)


def chart_link_href(token):
    dexscreener_url = str(token.get("dexscreener_url", "") or "")
    contract = str(token.get("contract_address", "") or "").strip()
    if contract and contract.upper() != "N/A" and "/solana/" in dexscreener_url.lower():
        return f"https://gmgn.ai/sol/token/{contract}"
    return dexscreener_url


def falcon_eye_state_class(high_priority_active=False):
    return "high-priority" if high_priority_active else "normal"


def token_pair_price_usd(token):
    pair = ((token.get("raw_data") or {}).get("pair") or {})
    if not isinstance(pair, dict):
        return None
    for key in ("priceUsd", "price_usd", "price"):
        value = pair.get(key)
        if value in (None, ""):
            continue
        try:
            price_value = float(value)
        except (TypeError, ValueError):
            continue
        if price_value > 0:
            return price_value
    return None


def fmt_price(value):
    if value is None:
        return "N/A"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if number <= 0:
        return "N/A"
    if number >= 1000:
        return f"${number:,.2f}"
    if number >= 1:
        return f"${number:,.4f}".rstrip("0").rstrip(".")
    return f"${number:.8f}".rstrip("0").rstrip(".")


def nav_chip(label, href=None, active=False, dim=False):
    classes = ["status-chip"]
    if active:
        classes.append("active")
    if dim:
        classes.append("dim")
    class_name = " ".join(classes)
    label_html = html.escape(label)
    if href:
        return f'<a class="{class_name}" href="{html.escape(href)}" target="_blank" rel="noopener noreferrer">{label_html}</a>'
    return f'<span class="{class_name}">{label_html}</span>'


def render_falcon_masthead(high_priority_active=False):
    state_class = falcon_eye_state_class(high_priority_active)
    eyes_asset = "assets/icons/falcon_eyes.png"
    eyes_asset_path = Path(__file__).resolve().parent / eyes_asset
    eyes_data_uri = ""
    if eyes_asset_path.exists():
        try:
            encoded = base64.b64encode(eyes_asset_path.read_bytes()).decode("ascii")
            eyes_data_uri = f"data:image/png;base64,{encoded}"
        except OSError:
            eyes_data_uri = ""
    has_eyes_asset = bool(eyes_data_uri)
    zone_class = f"fal-header-image-wrap {state_class}{'' if has_eyes_asset else ' no-image'}"
    eyes_markup = f'<img class="fal-eye-img" src="{html.escape(eyes_data_uri)}" alt="" aria-hidden="true" />' if has_eyes_asset else ""
    st.markdown(
        f"""
        <div class="fal-panel fal-title-wrap">
            <div class="{zone_class}">
                {eyes_markup}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


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
        price_display = fmt_price(token_pair_price_usd(token))
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
                    <div class="opp-metrics">
                        <div class="opp-row"><span>Price</span><span>{html.escape(price_display)}</span></div>
                        <div class="opp-row"><span>5m %</span><span class="{move_class(move)}">{move:+.2f}%</span></div>
                        <div class="opp-row"><span>Liq</span><span>{fmt_money(token.get('liquidity_usd', 0))}</span></div>
                        <div class="opp-row"><span>Vol 5m</span><span>{fmt_money(token.get('volume_5m_usd', 0))}</span></div>
                        <div class="opp-row"><span>Age</span><span>{fmt_age_minutes(token.get('pair_age_minutes'))}</span></div>
                        <div class="opp-row"><span>Risk</span><span>{html.escape(str(token.get('risk_label', 'HIGH')))}</span></div>
                    </div>
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
        price_display = fmt_price(token_pair_price_usd(token))
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
                    <div>Price: {html.escape(price_display)}</div>
                    <div>Liq: {fmt_money(token.get('liquidity_usd', 0))}</div>
                    <div>Vol 5m: {fmt_money(token.get('volume_5m_usd', 0))}</div>
                    <div>Age: {fmt_age_minutes(token.get('pair_age_minutes'))}</div>
                    <div>Smart: {html.escape(str(token.get('smart_wallet_display', '-')))}</div>
                    <div>Social: {heat_badge(heat)}</div>
                    <div>5m: <span class="{move_class(move)}">{move:+.2f}%</span></div>
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


def _surge_settings_form_key():
    return "falcon_surge_settings_form"


def _surge_field_key(name):
    return f"falcon_surge_{name}"


def _initialize_surge_form_state(settings):
    for key, value in settings.items():
        state_key = _surge_field_key(key)
        if state_key not in st.session_state:
            st.session_state[state_key] = value


def _read_surge_form_state():
    return {
        "enabled": bool(st.session_state.get(_surge_field_key("enabled"), True)),
        "min_market_cap_usd": float(st.session_state.get(_surge_field_key("min_market_cap_usd"), 100000.0)),
        "max_market_cap_usd": float(st.session_state.get(_surge_field_key("max_market_cap_usd"), 2000000.0)),
        "min_liquidity_usd": float(st.session_state.get(_surge_field_key("min_liquidity_usd"), 20000.0)),
        "watch_min_mc_change_pct": float(st.session_state.get(_surge_field_key("watch_min_mc_change_pct"), 15.0)),
        "watch_min_buy_pressure_ratio": float(st.session_state.get(_surge_field_key("watch_min_buy_pressure_ratio"), 1.0)),
        "surge_min_mc_change_pct": float(st.session_state.get(_surge_field_key("surge_min_mc_change_pct"), 25.0)),
        "surge_min_volume_accel": float(st.session_state.get(_surge_field_key("surge_min_volume_accel"), 1.35)),
        "surge_min_buy_pressure_ratio": float(st.session_state.get(_surge_field_key("surge_min_buy_pressure_ratio"), 1.0)),
        "surge_min_liquidity_usd": float(st.session_state.get(_surge_field_key("surge_min_liquidity_usd"), 20000.0)),
        "breakout_min_mc_change_pct": float(st.session_state.get(_surge_field_key("breakout_min_mc_change_pct"), 50.0)),
        "breakout_min_volume_accel": float(st.session_state.get(_surge_field_key("breakout_min_volume_accel"), 1.8)),
        "breakout_min_buy_pressure_ratio": float(st.session_state.get(_surge_field_key("breakout_min_buy_pressure_ratio"), 1.2)),
        "breakout_min_liquidity_usd": float(st.session_state.get(_surge_field_key("breakout_min_liquidity_usd"), 25000.0)),
        "alerts_enabled": bool(st.session_state.get(_surge_field_key("alerts_enabled"), True)),
        "alert_on_surge": bool(st.session_state.get(_surge_field_key("alert_on_surge"), True)),
        "alert_on_breakout": bool(st.session_state.get(_surge_field_key("alert_on_breakout"), True)),
        "alert_cooldown_minutes": int(st.session_state.get(_surge_field_key("alert_cooldown_minutes"), 8)),
        "alert_reset_minutes": int(st.session_state.get(_surge_field_key("alert_reset_minutes"), 35)),
    }


def _validate_surge_settings(settings):
    errors = []
    if settings["min_market_cap_usd"] > settings["max_market_cap_usd"]:
        errors.append("Minimum Market Cap cannot be greater than Maximum Market Cap.")

    numeric_non_negative = [
        "min_market_cap_usd",
        "max_market_cap_usd",
        "min_liquidity_usd",
        "watch_min_mc_change_pct",
        "watch_min_buy_pressure_ratio",
        "surge_min_mc_change_pct",
        "surge_min_volume_accel",
        "surge_min_buy_pressure_ratio",
        "surge_min_liquidity_usd",
        "breakout_min_mc_change_pct",
        "breakout_min_volume_accel",
        "breakout_min_buy_pressure_ratio",
        "breakout_min_liquidity_usd",
        "alert_cooldown_minutes",
        "alert_reset_minutes",
    ]
    for key in numeric_non_negative:
        if settings[key] < 0:
            errors.append(f"{key} cannot be negative.")

    if settings["breakout_min_mc_change_pct"] < settings["surge_min_mc_change_pct"]:
        errors.append("BREAKOUT minimum MC Change % cannot be weaker than SURGE.")
    if settings["breakout_min_volume_accel"] < settings["surge_min_volume_accel"]:
        errors.append("BREAKOUT minimum Volume Acceleration cannot be weaker than SURGE.")
    if settings["breakout_min_buy_pressure_ratio"] < settings["surge_min_buy_pressure_ratio"]:
        errors.append("BREAKOUT minimum Buy Pressure cannot be weaker than SURGE.")
    if settings["breakout_min_liquidity_usd"] < settings["surge_min_liquidity_usd"]:
        errors.append("BREAKOUT minimum Liquidity cannot be weaker than SURGE.")

    if settings["alert_cooldown_minutes"] < 1:
        errors.append("Alert cooldown must be at least 1 minute.")
    if settings["alert_reset_minutes"] < 1:
        errors.append("Reset time must be at least 1 minute.")

    return errors


def render_surge_settings_panel():
    active_settings = get_surge_settings()
    default_settings = get_default_surge_settings()
    _initialize_surge_form_state(active_settings)

    with st.expander("⚡ Surge Settings", expanded=False):
        st.caption("WATCH = interesting acceleration, monitor only")
        st.caption("SURGE = strong acceleration, Telegram alert")
        st.caption("BREAKOUT = exceptional acceleration, urgent Telegram alert")

        st.toggle("Enable Surge Detector", key=_surge_field_key("enabled"))

        st.markdown("**Candidate Filters**")
        candidate_cols = st.columns(3)
        with candidate_cols[0]:
            st.number_input("Minimum Market Cap", min_value=0.0, step=10000.0, format="%.0f", key=_surge_field_key("min_market_cap_usd"))
        with candidate_cols[1]:
            st.number_input("Maximum Market Cap", min_value=0.0, step=10000.0, format="%.0f", key=_surge_field_key("max_market_cap_usd"))
        with candidate_cols[2]:
            st.number_input("Minimum Liquidity", min_value=0.0, step=1000.0, format="%.0f", key=_surge_field_key("min_liquidity_usd"))

        st.markdown("**WATCH**")
        watch_cols = st.columns(2)
        with watch_cols[0]:
            st.number_input("Minimum MC Change %", min_value=0.0, step=0.5, format="%.2f", key=_surge_field_key("watch_min_mc_change_pct"))
        with watch_cols[1]:
            st.number_input("Minimum Buy Pressure", min_value=0.0, step=0.05, format="%.2f", key=_surge_field_key("watch_min_buy_pressure_ratio"))

        st.markdown("**SURGE**")
        surge_cols = st.columns(4)
        with surge_cols[0]:
            st.number_input("Minimum MC Change % ", min_value=0.0, step=0.5, format="%.2f", key=_surge_field_key("surge_min_mc_change_pct"))
        with surge_cols[1]:
            st.number_input("Minimum Volume Acceleration", min_value=0.0, step=0.05, format="%.2f", key=_surge_field_key("surge_min_volume_accel"))
        with surge_cols[2]:
            st.number_input("Minimum Buy Pressure ", min_value=0.0, step=0.05, format="%.2f", key=_surge_field_key("surge_min_buy_pressure_ratio"))
        with surge_cols[3]:
            st.number_input("Minimum Liquidity ", min_value=0.0, step=1000.0, format="%.0f", key=_surge_field_key("surge_min_liquidity_usd"))

        st.markdown("**BREAKOUT**")
        breakout_cols = st.columns(4)
        with breakout_cols[0]:
            st.number_input("Minimum MC Change %  ", min_value=0.0, step=0.5, format="%.2f", key=_surge_field_key("breakout_min_mc_change_pct"))
        with breakout_cols[1]:
            st.number_input("Minimum Volume Acceleration ", min_value=0.0, step=0.05, format="%.2f", key=_surge_field_key("breakout_min_volume_accel"))
        with breakout_cols[2]:
            st.number_input("Minimum Buy Pressure  ", min_value=0.0, step=0.05, format="%.2f", key=_surge_field_key("breakout_min_buy_pressure_ratio"))
        with breakout_cols[3]:
            st.number_input("Minimum Liquidity  ", min_value=0.0, step=1000.0, format="%.0f", key=_surge_field_key("breakout_min_liquidity_usd"))

        st.markdown("**Alert Controls**")
        alert_cols = st.columns(4)
        with alert_cols[0]:
            st.toggle("Enable SURGE Telegram alerts", key=_surge_field_key("alert_on_surge"))
        with alert_cols[1]:
            st.toggle("Enable BREAKOUT Telegram alerts", key=_surge_field_key("alert_on_breakout"))
        with alert_cols[2]:
            st.number_input("Alert cooldown", min_value=1, step=1, key=_surge_field_key("alert_cooldown_minutes"))
        with alert_cols[3]:
            st.number_input("Reset time", min_value=1, step=1, key=_surge_field_key("alert_reset_minutes"))

        st.toggle("Enable surge Telegram dispatch", key=_surge_field_key("alerts_enabled"))

        action_cols = st.columns(2)
        apply_clicked = action_cols[0].button("Apply Settings", use_container_width=True, key="falcon_surge_apply")
        reset_clicked = action_cols[1].button("Reset to Defaults", use_container_width=True, key="falcon_surge_reset")

        if apply_clicked:
            pending = _read_surge_form_state()
            validation_errors = _validate_surge_settings(pending)
            if validation_errors:
                for message in validation_errors:
                    st.error(message)
            else:
                applied = apply_surge_settings(pending)
                for key, value in applied.items():
                    st.session_state[_surge_field_key(key)] = value
                st.success("Surge settings applied and saved locally.")
                st.session_state.scan_payload = scan_tokens()

        if reset_clicked:
            reset_values = reset_surge_settings()
            for key, value in reset_values.items():
                st.session_state[_surge_field_key(key)] = value
            st.success("Surge settings reset to defaults.")
            st.session_state.scan_payload = scan_tokens()


def display_falcon():
    inject_css()
    inject_copy_script()

    platform_links = {
        "x": os.getenv("FALCON_X_URL") or os.getenv("X_URL") or os.getenv("X_PROFILE_URL"),
        "telegram": os.getenv("FALCON_TELEGRAM_URL") or os.getenv("TELEGRAM_URL"),
        "discord": os.getenv("FALCON_DISCORD_URL") or os.getenv("DISCORD_URL"),
    }

    payload = st.session_state.get("scan_payload", {})
    if payload and payload.get("ok") and payload.get("tokens"):
        prefiltered = payload.get("tokens", [])
        high_priority_active = any(
            bool(token.get("high_priority_alert", False)) or str(token.get("signal", "")).upper() == "BUY NOW"
            for token in prefiltered
        )
    else:
        high_priority_active = False

    st.markdown('<div class="fal-shell">', unsafe_allow_html=True)

    render_falcon_masthead(high_priority_active=high_priority_active)

    top_row_cols = st.columns([4, 2])
    with top_row_cols[0]:
        st.markdown(
            '<div class="fal-status-left">'
            + nav_chip('MISSION LIVE', active=True)
            + nav_chip('SCANNER READY', active=True)
            + nav_chip('GMGN', dim=True)
            + '</div>',
            unsafe_allow_html=True,
        )
    with top_row_cols[1]:
        st.markdown(
            '<div class="fal-status-right">'
            + f'<a class="icon-chip" href="{html.escape(platform_links["x"] or "#")}" target="_blank" rel="noopener noreferrer" title="X">𝕏</a>'
            + f'<a class="icon-chip" href="{html.escape(platform_links["telegram"] or "#")}" target="_blank" rel="noopener noreferrer" title="Telegram">✈</a>'
            + f'<a class="icon-chip" href="{html.escape(platform_links["discord"] or "#")}" target="_blank" rel="noopener noreferrer" title="Discord">◎</a>'
            + '<span class="icon-chip" title="Settings">⚙</span>'
            + '</div>',
            unsafe_allow_html=True,
        )

    bottom_row_cols = st.columns([4, 2])
    with bottom_row_cols[0]:
        st.markdown('<div class="control-strip">', unsafe_allow_html=True)
        left_controls = st.columns([1.35, 1])
        with left_controls[0]:
            refresh_clicked = st.button("Refresh Scan", type="primary", use_container_width=True, key="falcon_refresh_scan")
        with left_controls[1]:
            live_mode = st.toggle("Live 2.5s", value=True, key="falcon_live_mode")
        st.markdown('</div>', unsafe_allow_html=True)
    with bottom_row_cols[1]:
        st.markdown('<div class="control-strip">', unsafe_allow_html=True)
        test_telegram_clicked = st.button("TEST TELEGRAM ALERT", use_container_width=True, key="falcon_test_telegram_alert")
        st.markdown('</div>', unsafe_allow_html=True)
    if test_telegram_clicked:
        sent_ok, sent_message = send_dashboard_test_telegram_alert()
        if sent_ok:
            st.success(sent_message)
        else:
            st.error(sent_message)

    if live_mode:
        inject_live_refresh(LIVE_REFRESH_MS)

    render_surge_settings_panel()

    payload = load_scan(force_refresh=refresh_clicked or live_mode)

    if not payload.get("ok"):
        st.error(payload.get("error", "Scan failed due to an unknown error."))
        st.stop()

    tokens = payload.get("tokens", [])
    if not tokens:
        st.info("No opportunities found in the latest scan.")
        st.stop()

    scan_time = format_scan_time(payload.get("scanned_at"))
    st.markdown('<div class="fal-filter-shell">', unsafe_allow_html=True)
    filter_cols = st.columns([2.1, 2.1, 1.2, 1.3])
    with filter_cols[0]:
        signal_filter = st.selectbox("Signal", ["ALL", "BUY NOW", "BUY", "WATCH", "PASS"], index=0, key="falcon_signal_filter")
    with filter_cols[1]:
        conviction_filter = st.selectbox("Conviction", ["ALL", "LEGENDARY", "ELITE", "STRONG", "WATCH"], index=0, key="falcon_conviction_filter")
    with filter_cols[2]:
        filtered_for_temp = tokens
        if signal_filter != "ALL":
            filtered_for_temp = [t for t in filtered_for_temp if str(t.get("signal", "PASS")).upper() == signal_filter]
        if conviction_filter != "ALL":
            filtered_for_temp = [t for t in filtered_for_temp if str(t.get("conviction_rating", "WATCH")).upper() == conviction_filter]
        temperature = market_temperature(filtered_for_temp)
        st.markdown('<div class="fal-filter-status">' + nav_chip(f"Market Temp {temperature}", active=True) + '</div>', unsafe_allow_html=True)
    with filter_cols[3]:
        st.markdown('<div class="fal-filter-status">' + nav_chip(f"Last Scan {scan_time}", dim=True) + '</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

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
        st.markdown('</div>', unsafe_allow_html=True)
        st.stop()

    strongest = strongest_signal(filtered_tokens)
    temperature = market_temperature(filtered_tokens)

    st.markdown(
        '<div class="fal-nav-strip-wrap"><div class="fal-nav-strip">'
        + nav_chip('X', platform_links['x'], active=bool(platform_links['x']))
        + nav_chip('Telegram', platform_links['telegram'], active=bool(platform_links['telegram']))
        + nav_chip('Discord', platform_links['discord'], active=bool(platform_links['discord']))
        + nav_chip('Settings', '#', active=True)
        + nav_chip('Normal', active=not any(bool(token.get("high_priority_alert", False)) for token in filtered_tokens), dim=any(bool(token.get("high_priority_alert", False)) for token in filtered_tokens))
        + '</div></div>',
        unsafe_allow_html=True,
    )

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

    render_surge_section(filtered_tokens)

    render_scanner_status(payload.get("scanner_status", []), payload.get("scanner_elapsed_ms", 0))

    render_desktop_table(filtered_tokens)
    render_mobile_cards(filtered_tokens)

    st.markdown('</div>', unsafe_allow_html=True)


def live_falcon():
    display_falcon()


if __name__ == "__main__":
    live_falcon()