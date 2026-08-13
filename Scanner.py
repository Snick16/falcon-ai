import requests
import json
from datetime import datetime, timezone
from pathlib import Path


from falcon_alerts import create_default_alert_engine
from social_intelligence import SocialContext, create_default_social_engine
from source_scanner import collect_all_candidates

BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
TOKEN_URL = "https://api.dexscreener.com/tokens/v1/solana/{}"
REQUIRED_TOKEN_KEYS = ("signal", "momentum", "confidence", "risk_label")
MEMORY_DIR = Path(__file__).resolve().parent / ".falcon_memory"
SNAPSHOTS_DIR = MEMORY_DIR / "snapshots"
DISAPPEARED_HISTORY_FILE = MEMORY_DIR / "disappeared_history.jsonl"
SCANNED_CONTRACTS_FILE = MEMORY_DIR / "scanned_contracts.json"
SMART_WALLET_TRACKER_FILE = MEMORY_DIR / "smart_wallet_tracker.json"
MAX_SNAPSHOTS = 500
SOCIAL_ENGINE = create_default_social_engine()
ALERT_ENGINE = create_default_alert_engine()

FALCON_SCORING_CONFIG = {
    "confirmation": {
        "sources": {
            "axiom": ("axiom", "axiom.trade"),
            "gmgn": ("gmgn", "gmgn.ai"),
            "photon": ("photon", "photon-sol", "photon-sol.tinyastro.io"),
        },
        "score_by_count": {
            0: 0,
            1: 5,
            2: 12,
            3: 20,
        },
    },
    "weights": {
        "market_baseline": 0.52,
        "intelligence": 0.28,
    },
    "acceleration": {
        "volume_growth_1_2x": 4,
        "volume_growth_1_8x": 8,
        "volume_growth_2_5x": 12,
        "buy_pressure_light": 4,
        "buy_pressure_strong": 8,
        "buy_pressure_extreme": 11,
        "price_positive": 2,
        "price_strong": 4,
    },
    "social": {
        "hot_with_market_confirmation": 8,
        "viral_with_market_confirmation": 12,
    },
    "early_opportunity": {
        "max_age_minutes": 40,
        "required_volume_growth": 1.8,
        "bonus": 8,
    },
    "safety_penalties": {
        "low_liquidity": 9,
        "high_risk": 8,
        "holder_concentration_70": 10,
        "holder_concentration_85": 18,
        "sniper_like": 8,
        "rug_like": 10,
    },
    "signal": {
        "watch_min": 58,
        "buy_min": 72,
        "buy_now_min": 90,
        "high_priority_min": 92,
        "min_confirmation_for_buy": 2,
        "min_confirmation_for_buy_now": 2,
        "min_confirmation_for_high_priority": 2,
    },
}


def safe_number(value):
    """Convert missing or invalid numbers to zero."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _clamp_int(value, low=0, high=100):
    return int(max(low, min(int(round(value)), high)))


def _to_compact_text(value):
    return str(value or "").strip().lower()


def _contains_keyword(haystack, keywords):
    text = _to_compact_text(haystack)
    if not text:
        return False
    for keyword in keywords:
        if _to_compact_text(keyword) and _to_compact_text(keyword) in text:
            return True
    return False


def derive_source_confirmation(candidate):
    """Return per-source confirmation flags using explicit collector tags/flags."""
    raw_data = (candidate or {}).get("raw_data") or {}
    source_names = list(raw_data.get("found_by", [])) or [str((candidate or {}).get("source", "unknown"))]
    explicit_flags = raw_data.get("source_confirmations") if isinstance(raw_data, dict) else None
    config_sources = FALCON_SCORING_CONFIG["confirmation"]["sources"]

    if isinstance(explicit_flags, dict):
        flags = {
            source_key: bool(explicit_flags.get(source_key, False))
            for source_key in config_sources.keys()
        }
    else:
        normalized_sources = {_to_compact_text(name) for name in source_names if _to_compact_text(name)}
        flags = {
            "axiom": "axiom" in normalized_sources,
            "gmgn": "gmgn" in normalized_sources,
            "photon": "photon" in normalized_sources,
        }

    confirmed = [name.upper() for name, enabled in flags.items() if enabled]
    count = len(confirmed)
    score_by_count = FALCON_SCORING_CONFIG["confirmation"]["score_by_count"]
    confirmation_score = score_by_count.get(count, score_by_count[max(score_by_count.keys())])

    return {
        "source_confirmations": flags,
        "source_confirmation_count": count,
        "source_confirmation_names": confirmed,
        "source_confirmation_score": confirmation_score,
    }


def _extract_holder_concentration_pct(pair):
    """Best-effort extraction of top-holder concentration (0-100)."""
    if not isinstance(pair, dict):
        return None

    candidates = [
        pair.get("holderConcentrationPct"),
        pair.get("topHoldersPct"),
        (pair.get("info") or {}).get("holderConcentrationPct") if isinstance(pair.get("info"), dict) else None,
        (pair.get("info") or {}).get("topHoldersPct") if isinstance(pair.get("info"), dict) else None,
        (pair.get("tokenomics") or {}).get("top10Pct") if isinstance(pair.get("tokenomics"), dict) else None,
    ]

    for value in candidates:
        if value in (None, ""):
            continue
        try:
            pct = float(value)
        except (TypeError, ValueError):
            continue
        if 0 <= pct <= 1:
            pct *= 100
        if 0 <= pct <= 100:
            return pct
    return None


def _sniper_like_flow(pair, candidate):
    """Heuristic suspicious flow detector using available tx and optional source metadata."""
    txns_5m = (pair.get("txns", {}) or {}).get("m5", {}) if isinstance(pair, dict) else {}
    buys_5m = int(txns_5m.get("buys", 0) or 0)
    sells_5m = int(txns_5m.get("sells", 0) or 0)
    volume_5m = safe_number((pair.get("volume", {}) or {}).get("m5", 0) if isinstance(pair, dict) else 0)

    raw_data = (candidate or {}).get("raw_data") or {}
    suspicious_raw = [
        raw_data.get("sniper_count"),
        raw_data.get("bot_count"),
        raw_data.get("suspicious_bots"),
        raw_data.get("bundle_count"),
    ]
    for value in suspicious_raw:
        try:
            if int(value or 0) >= 5:
                return True
        except (TypeError, ValueError):
            continue

    avg_tx = (volume_5m / (buys_5m + sells_5m)) if (buys_5m + sells_5m) > 0 else 0
    return buys_5m >= 18 and sells_5m <= 2 and avg_tx >= 2500


def compute_acceleration_metrics(pair, previous_token):
    txns_5m = (pair.get("txns", {}) or {}).get("m5", {})
    buys_5m = int(txns_5m.get("buys", 0) or 0)
    sells_5m = int(txns_5m.get("sells", 0) or 0)
    volume_5m = safe_number((pair.get("volume", {}) or {}).get("m5"))
    price_5m = safe_number((pair.get("priceChange", {}) or {}).get("m5"))

    prev_volume_5m = safe_number((previous_token or {}).get("volume_5m_usd"))
    prev_price_5m = safe_number((previous_token or {}).get("price_change_5m_pct"))

    volume_growth_ratio = (volume_5m / prev_volume_5m) if prev_volume_5m > 0 else (2.0 if volume_5m >= 5_000 else 1.0)
    price_delta = price_5m - prev_price_5m
    buy_sell_ratio = float(buys_5m) if sells_5m <= 0 else float(buys_5m) / float(sells_5m)

    weights = FALCON_SCORING_CONFIG["acceleration"]
    acceleration_score = 0
    reasons = []

    if volume_growth_ratio >= 2.5:
        acceleration_score += weights["volume_growth_2_5x"]
        reasons.append("volume acceleration >=2.5x scan-over-scan")
    elif volume_growth_ratio >= 1.8:
        acceleration_score += weights["volume_growth_1_8x"]
        reasons.append("volume acceleration >=1.8x scan-over-scan")
    elif volume_growth_ratio >= 1.2:
        acceleration_score += weights["volume_growth_1_2x"]
        reasons.append("volume acceleration >=1.2x scan-over-scan")

    if buy_sell_ratio >= 2.2 and buys_5m >= 14:
        acceleration_score += weights["buy_pressure_extreme"]
        reasons.append("extreme buy pressure")
    elif buy_sell_ratio >= 1.5 and buys_5m >= 10:
        acceleration_score += weights["buy_pressure_strong"]
        reasons.append("strong buy pressure")
    elif buy_sell_ratio >= 1.1 and buys_5m >= 6:
        acceleration_score += weights["buy_pressure_light"]
        reasons.append("net positive buy pressure")

    if price_5m >= 3 and price_delta > 0:
        acceleration_score += weights["price_strong"]
        reasons.append("price momentum confirms acceleration")
    elif price_5m > 0 and price_delta > 0:
        acceleration_score += weights["price_positive"]
        reasons.append("price trend is improving")

    return {
        "acceleration_score": acceleration_score,
        "volume_growth_ratio": round(volume_growth_ratio, 3),
        "price_delta_5m_pct": round(price_delta, 3),
        "buy_sell_ratio_5m": round(buy_sell_ratio, 3),
        "acceleration_reasons": reasons,
    }


def compose_falcon_score(
    *,
    base_market_score,
    intelligence_score,
    liquidity_usd,
    risk_label,
    pair,
    candidate,
    pair_age_minutes,
    social_heat_label,
    source_confirmation,
    acceleration,
):
    """Combine independent score families into final 0-100 Falcon score."""
    weights = FALCON_SCORING_CONFIG["weights"]
    penalties_cfg = FALCON_SCORING_CONFIG["safety_penalties"]

    score = (
        (base_market_score * weights["market_baseline"])
        + (intelligence_score * weights["intelligence"])
        + source_confirmation["source_confirmation_score"]
        + acceleration["acceleration_score"]
    )

    reasons = [
        f"market baseline contribution: {base_market_score} x {weights['market_baseline']}",
        f"intelligence contribution: {intelligence_score} x {weights['intelligence']}",
        f"source confirmation bonus: +{source_confirmation['source_confirmation_score']}",
    ] + acceleration["acceleration_reasons"]

    social_cfg = FALCON_SCORING_CONFIG["social"]
    if source_confirmation["source_confirmation_count"] >= 2 and acceleration["acceleration_score"] >= 14:
        if social_heat_label == "VIRAL":
            score += social_cfg["viral_with_market_confirmation"]
            reasons.append("viral social catalyst confirmed by market acceleration")
        elif social_heat_label == "HOT":
            score += social_cfg["hot_with_market_confirmation"]
            reasons.append("hot social catalyst confirmed by market acceleration")

    early_cfg = FALCON_SCORING_CONFIG["early_opportunity"]
    safety_ok = risk_label in ("LOW", "MEDIUM") and liquidity_usd >= 15_000
    if (
        pair_age_minutes is not None
        and pair_age_minutes <= early_cfg["max_age_minutes"]
        and acceleration["volume_growth_ratio"] >= early_cfg["required_volume_growth"]
        and safety_ok
    ):
        score += early_cfg["bonus"]
        reasons.append("early-opportunity bonus: very new token with accelerating real volume")

    penalties = []
    if liquidity_usd < 12_000:
        score -= penalties_cfg["low_liquidity"]
        penalties.append("low liquidity penalty")

    if risk_label == "HIGH":
        score -= penalties_cfg["high_risk"]
        penalties.append("high risk penalty")

    concentration_pct = _extract_holder_concentration_pct(pair)
    if concentration_pct is not None:
        if concentration_pct >= 85:
            score -= penalties_cfg["holder_concentration_85"]
            penalties.append(f"holder concentration penalty ({concentration_pct:.1f}% top holders)")
        elif concentration_pct >= 70:
            score -= penalties_cfg["holder_concentration_70"]
            penalties.append(f"holder concentration warning ({concentration_pct:.1f}% top holders)")

    if _sniper_like_flow(pair, candidate):
        score -= penalties_cfg["sniper_like"]
        penalties.append("sniper/bot-like flow penalty")

    market_cap = safe_number(pair.get("marketCap") or pair.get("fdv"))
    if market_cap > 0 and liquidity_usd > 0:
        liq_to_mcap = liquidity_usd / market_cap
        if liq_to_mcap < 0.03 and (pair_age_minutes is not None and pair_age_minutes <= 90):
            score -= penalties_cfg["rug_like"]
            penalties.append("rug-like liquidity depth penalty")

    bounded_score = _clamp_int(score, 0, 100)

    return {
        "score": bounded_score,
        "score_reasons": reasons,
        "score_penalties": penalties,
        "holder_concentration_pct": concentration_pct,
    }


def ensure_memory_dirs():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def load_seen_contracts():
    """Load persistent set of contracts already scanned in prior runs."""
    ensure_memory_dirs()
    if not SCANNED_CONTRACTS_FILE.exists():
        return set()

    try:
        with SCANNED_CONTRACTS_FILE.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return set()

    if not isinstance(raw, list):
        return set()

    return {
        str(contract).strip()
        for contract in raw
        if str(contract).strip()
    }


def save_seen_contracts(contracts):
    """Persist scanned contracts to prevent duplicate new-token detections."""
    ensure_memory_dirs()
    serialized = sorted(
        {
            str(contract).strip()
            for contract in contracts
            if str(contract).strip()
        }
    )
    with SCANNED_CONTRACTS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(serialized, handle, ensure_ascii=True)


def parse_iso_datetime(value):
    """Parse an ISO datetime string safely."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_smart_wallet_tracker():
    """Load smart-wallet activity memory used for rolling 10-minute clustering."""
    ensure_memory_dirs()
    if not SMART_WALLET_TRACKER_FILE.exists():
        return {}
    try:
        with SMART_WALLET_TRACKER_FILE.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}
    contracts = payload.get("contracts", payload)
    if not isinstance(contracts, dict):
        return {}
    return contracts


def save_smart_wallet_tracker(tracker_state):
    """Persist smart-wallet tracker state."""
    ensure_memory_dirs()
    payload = {"contracts": tracker_state}
    with SMART_WALLET_TRACKER_FILE.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True)


def prune_smart_wallet_tracker(tracker_state, now_dt):
    """Keep tracker compact by retaining only recent activity windows."""
    cutoff = now_dt.timestamp() - (24 * 3600)
    pruned = {}
    for contract, events in tracker_state.items():
        if not isinstance(events, list):
            continue
        kept = []
        for event in events:
            event_dt = parse_iso_datetime((event or {}).get("ts"))
            if not event_dt:
                continue
            if event_dt.timestamp() >= cutoff:
                kept.append(event)
        if kept:
            pruned[contract] = kept
    return pruned


def estimate_tracked_wallet_buys(pair, age_minutes):
    """Estimate tracked profitable-wallet buy count from early trading behavior."""
    txns_5m = pair.get("txns", {}).get("m5", {})
    buys_5m = int(txns_5m.get("buys", 0) or 0)
    sells_5m = int(txns_5m.get("sells", 0) or 0)
    volume_5m = safe_number(pair.get("volume", {}).get("m5"))
    liquidity = safe_number(pair.get("liquidity", {}).get("usd"))

    if age_minutes is None or age_minutes > 240:
        return 0, []

    reasons = ["tracking newly launched token flow"]

    if buys_5m >= 45 and buys_5m > sells_5m and volume_5m >= 12_000 and liquidity >= 20_000:
        return 3, reasons + ["3 tracked wallets estimated from strong coordinated buy flow"]
    if buys_5m >= 22 and buys_5m > sells_5m and volume_5m >= 5_000:
        return 2, reasons + ["2 tracked wallets estimated from sustained buy pressure"]
    if buys_5m >= 8 and buys_5m > sells_5m:
        return 1, reasons + ["1 tracked wallet estimated from early buy pressure"]
    return 0, []


def get_recent_wallet_count(tracker_state, contract_address, now_dt, window_minutes=10):
    """Count tracked-wallet events for a token over a rolling window."""
    events = tracker_state.get(contract_address, [])
    if not isinstance(events, list):
        return 0

    cutoff = now_dt.timestamp() - (window_minutes * 60)
    total = 0
    for event in events:
        event_dt = parse_iso_datetime((event or {}).get("ts"))
        if not event_dt or event_dt.timestamp() < cutoff:
            continue
        try:
            count = int((event or {}).get("wallet_count", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        total += max(0, count)
    return total


def append_wallet_event(tracker_state, contract_address, scanned_at, wallet_count):
    """Append wallet activity for the contract when tracked wallets are detected."""
    if wallet_count <= 0:
        return
    tracker_state.setdefault(contract_address, []).append(
        {"ts": scanned_at, "wallet_count": int(wallet_count)}
    )


def get_smart_wallet_display(wallet_count):
    """Render wallet intensity markers for dashboard display."""
    if wallet_count <= 0:
        return "-"
    if wallet_count == 1:
        return "🔥 1"
    if wallet_count == 2:
        return "🔥🔥 2"
    return "🔥🔥🔥 3+"


def get_wallet_cluster_bonus(wallet_count):
    """Increase Falcon score when wallet clustering appears inside 10 minutes."""
    if wallet_count >= 3:
        return 12, "multiple tracked wallets (3+) bought within 10 minutes"
    if wallet_count >= 2:
        return 7, "multiple tracked wallets (2) bought within 10 minutes"
    return 0, ""


def load_latest_snapshot():
    """Load the latest scan snapshot if one exists."""
    ensure_memory_dirs()
    snapshot_files = sorted(SNAPSHOTS_DIR.glob("*.json"))
    if not snapshot_files:
        return None
    latest_path = snapshot_files[-1]
    try:
        with latest_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def prune_snapshots():
    """Keep only the newest MAX_SNAPSHOTS snapshot files."""
    snapshot_files = sorted(SNAPSHOTS_DIR.glob("*.json"))
    if len(snapshot_files) <= MAX_SNAPSHOTS:
        return
    for old_path in snapshot_files[:-MAX_SNAPSHOTS]:
        try:
            old_path.unlink()
        except OSError:
            pass


def save_snapshot(scanned_at, tokens):
    """Persist the completed scan as local JSON snapshot."""
    ensure_memory_dirs()
    safe_name = scanned_at.replace(":", "-").replace(".", "-")
    snapshot_path = SNAPSHOTS_DIR / f"{safe_name}.json"
    payload = {
        "scanned_at": scanned_at,
        "token_count": len(tokens),
        "tokens": tokens,
    }
    with snapshot_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True)
    prune_snapshots()


def record_disappeared_tokens(previous_tokens, current_tokens, scanned_at):
    """Append tokens missing from current scan to history."""
    if not previous_tokens:
        return

    previous_by_contract = {
        token.get("contract_address"): token
        for token in previous_tokens
        if token.get("contract_address")
    }
    current_contracts = {
        token.get("contract_address")
        for token in current_tokens
        if token.get("contract_address")
    }

    disappeared = [
        token
        for contract, token in previous_by_contract.items()
        if contract not in current_contracts
    ]
    if not disappeared:
        return

    ensure_memory_dirs()
    with DISAPPEARED_HISTORY_FILE.open("a", encoding="utf-8") as handle:
        for token in disappeared:
            record = {
                "scanned_at": scanned_at,
                "contract_address": token.get("contract_address"),
                "token_name": token.get("token_name"),
                "token_symbol": token.get("token_symbol"),
                "last_signal": token.get("signal"),
                "last_score": token.get("score"),
                "last_confidence": token.get("confidence"),
                "last_momentum": token.get("momentum"),
                "last_risk": token.get("risk_label"),
            }
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")


def build_memory_delta(current_token, previous_token):
    """Build scan-over-scan comparison fields for Falcon Memory."""
    if previous_token is None:
        return {
            "score_delta": "NEW",
            "confidence_delta": "NEW",
            "signal_change": "NEW",
            "momentum_change": "NEW",
            "risk_change": "NEW",
            "liquidity_delta": "NEW",
            "volume_5m_delta": "NEW",
            "market_cap_delta": "NEW",
        }

    current_score = int(current_token.get("score", 0))
    previous_score = int(previous_token.get("score", 0))
    current_conf = int(current_token.get("confidence", 0))
    previous_conf = int(previous_token.get("confidence", 0))

    current_signal = str(current_token.get("signal", "PASS"))
    previous_signal = str(previous_token.get("signal", "PASS"))
    current_momentum = str(current_token.get("momentum", "NEUTRAL"))
    previous_momentum = str(previous_token.get("momentum", "NEUTRAL"))
    current_risk = str(current_token.get("risk_label", "MEDIUM"))
    previous_risk = str(previous_token.get("risk_label", "MEDIUM"))

    current_liquidity = safe_number(current_token.get("liquidity_usd"))
    previous_liquidity = safe_number(previous_token.get("liquidity_usd"))
    current_volume = safe_number(current_token.get("volume_5m_usd"))
    previous_volume = safe_number(previous_token.get("volume_5m_usd"))
    current_market_cap = safe_number(current_token.get("market_cap_usd"))
    previous_market_cap = safe_number(previous_token.get("market_cap_usd"))

    return {
        "score_delta": current_score - previous_score,
        "confidence_delta": current_conf - previous_conf,
        "signal_change": (
            "UNCHANGED"
            if current_signal == previous_signal
            else f"{previous_signal} -> {current_signal}"
        ),
        "momentum_change": (
            "UNCHANGED"
            if current_momentum == previous_momentum
            else f"{previous_momentum} -> {current_momentum}"
        ),
        "risk_change": (
            "UNCHANGED"
            if current_risk == previous_risk
            else f"{previous_risk} -> {current_risk}"
        ),
        "liquidity_delta": round(current_liquidity - previous_liquidity, 2),
        "volume_5m_delta": round(current_volume - previous_volume, 2),
        "market_cap_delta": round(current_market_cap - previous_market_cap, 2),
    }


def calculate_score(pair):
    """Create a momentum/liquidity score from 0 to 100 with clear reasons."""
    score = 0
    reasons = []

    liquidity = safe_number(pair.get("liquidity", {}).get("usd"))
    market_cap = safe_number(pair.get("marketCap") or pair.get("fdv"))
    volume_1h = safe_number(pair.get("volume", {}).get("h1"))
    volume_5m = safe_number(pair.get("volume", {}).get("m5"))
    price_5m = safe_number(pair.get("priceChange", {}).get("m5"))
    price_1h = safe_number(pair.get("priceChange", {}).get("h1"))

    transactions = pair.get("txns", {}).get("m5", {})
    buys_5m = int(transactions.get("buys", 0) or 0)
    sells_5m = int(transactions.get("sells", 0) or 0)
    total_txns_5m = buys_5m + sells_5m
    liq_to_mcap = (liquidity / market_cap) if market_cap > 0 else 0.0

    if liquidity <= 1_000:
        score -= 25
        reasons.append("near-zero liquidity")
    elif liquidity < 10_000:
        score -= 14
        reasons.append("very low liquidity")
    elif liquidity >= 150_000:
        score += 18
        reasons.append("strong liquidity base")
    elif liquidity >= 75_000:
        score += 12
        reasons.append("healthy liquidity")
    elif liquidity >= 30_000:
        score += 6
        reasons.append("adequate liquidity")

    if market_cap > 0:
        if liq_to_mcap >= 0.20:
            score += 14
            reasons.append("excellent liquidity depth versus market cap")
        elif liq_to_mcap >= 0.10:
            score += 9
            reasons.append("good liquidity depth versus market cap")
        elif liq_to_mcap >= 0.05:
            score += 4
            reasons.append("acceptable liquidity depth versus market cap")
        else:
            score -= 10
            reasons.append("weak liquidity depth versus market cap")
    else:
        reasons.append("market cap unavailable")

    if market_cap >= 5_000_000 and liq_to_mcap < 0.03:
        score -= 14
        reasons.append("suspiciously high market cap with weak liquidity")

    if volume_5m >= 20_000:
        score += 14
        reasons.append("rising 5-minute volume")
    elif volume_5m >= 8_000:
        score += 9
        reasons.append("solid 5-minute volume")
    elif volume_5m >= 3_000:
        score += 4
        reasons.append("modest 5-minute volume")
    else:
        score -= 8
        reasons.append("very low 5-minute volume")

    if volume_1h >= 120_000:
        score += 10
        reasons.append("strong 1-hour volume")
    elif volume_1h >= 40_000:
        score += 6
        reasons.append("healthy 1-hour volume")
    elif volume_1h < 10_000:
        score -= 5
        reasons.append("weak 1-hour volume")

    if total_txns_5m >= 30:
        score += 8
        reasons.append("healthy transaction activity")
    elif total_txns_5m >= 12:
        score += 4
        reasons.append("decent transaction activity")
    elif total_txns_5m <= 2:
        score -= 6
        reasons.append("very low transaction activity")

    if buys_5m > sells_5m * 1.5 and buys_5m >= 10:
        score += 12
        reasons.append("buys clearly outpacing sells")
    elif buys_5m > sells_5m:
        score += 7
        reasons.append("buys outpacing sells")
    elif sells_5m > buys_5m * 1.2 and sells_5m >= 10:
        score -= 12
        reasons.append("sells outpacing buys")

    if 0.5 <= price_5m <= 8:
        score += 8
        reasons.append("positive 5-minute momentum")
    elif 8 < price_5m <= 15:
        score += 3
        reasons.append("strong short-term momentum")
    elif price_5m > 25:
        score -= 12
        reasons.append("extreme 5-minute spike may be exhausted")
    elif price_5m < -8:
        score -= 10
        reasons.append("severe negative 5-minute momentum")

    if 1 <= price_1h <= 20:
        score += 8
        reasons.append("positive 1-hour momentum")
    elif 20 < price_1h <= 40:
        score += 2
        reasons.append("very strong 1-hour momentum")
    elif price_1h > 60:
        score -= 10
        reasons.append("extreme 1-hour spike may be overextended")
    elif price_1h < -15:
        score -= 10
        reasons.append("severe negative 1-hour momentum")

    pair_created = pair.get("pairCreatedAt")
    if pair_created:
        age_hours = (
            datetime.now(timezone.utc).timestamp()
            - pair_created / 1000
        ) / 3600

        if age_hours < 0.03:
            score -= 20
            reasons.append("pair is only seconds old")
        elif age_hours < 0.20:
            score -= 8
            reasons.append("very new pair")
        elif age_hours <= 48:
            score += 6
            reasons.append("newer pair with enough market time")
    else:
        reasons.append("pair age unavailable")

    if not reasons:
        reasons.append("no strong bullish or bearish signals")

    return max(0, min(score, 100)), reasons


def classify_risk(score, pair):
    """Classify risk as LOW, MEDIUM, or HIGH using score and key risk factors."""
    liquidity = safe_number(pair.get("liquidity", {}).get("usd"))
    market_cap = safe_number(pair.get("marketCap") or pair.get("fdv"))
    price_5m = safe_number(pair.get("priceChange", {}).get("m5"))
    price_1h = safe_number(pair.get("priceChange", {}).get("h1"))
    liq_to_mcap = (liquidity / market_cap) if market_cap > 0 else 0.0

    if (
        score < 40
        or liquidity < 10_000
        or (market_cap >= 5_000_000 and liq_to_mcap < 0.03)
        or price_5m > 25
        or price_1h < -15
    ):
        return "HIGH"

    if score >= 70 and liquidity >= 50_000 and liq_to_mcap >= 0.08 and price_5m > -5:
        return "LOW"

    return "MEDIUM"


def classify_momentum(price_5m, buys_5m, sells_5m):
    """Classify short-term momentum as BULLISH, NEUTRAL, or BEARISH."""
    if price_5m > 1 and buys_5m > sells_5m:
        return "BULLISH"

    if price_5m < -1:
        return "BEARISH"

    if sells_5m > buys_5m * 1.2:
        return "BEARISH"

    return "NEUTRAL"


def has_healthy_buy_sell_ratio(buys_5m, sells_5m):
    """Define healthy short-term flow as non-negative buy pressure with activity."""
    total = buys_5m + sells_5m
    if total < 6:
        return False
    return buys_5m >= sells_5m


def qualifies_highlight(score, liquidity_usd, price_change_5m_pct, buys_5m, sells_5m):
    """Apply Falcon V2 highlight criteria for standout candidates."""
    return (
        score >= 90
        and liquidity_usd > 10_000
        and price_change_5m_pct > 0
        and has_healthy_buy_sell_ratio(buys_5m, sells_5m)
    )


def get_pair_age_minutes(pair):
    """Return pair age in minutes from DexScreener timestamp."""
    pair_created = pair.get("pairCreatedAt")
    if not pair_created:
        return None
    age_seconds = datetime.now(timezone.utc).timestamp() - (pair_created / 1000)
    return max(0.0, age_seconds / 60)


def get_holder_count(pair):
    """Best-effort holder count extraction from available payload keys."""
    candidates = [
        pair.get("holderCount"),
        pair.get("holders"),
        pair.get("info", {}).get("holderCount") if isinstance(pair.get("info"), dict) else None,
        pair.get("info", {}).get("holders") if isinstance(pair.get("info"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            for nested_key in ("count", "total", "holders"):
                nested_value = candidate.get(nested_key)
                if nested_value is None:
                    continue
                try:
                    return int(nested_value)
                except (TypeError, ValueError):
                    continue
        if candidate is None:
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def classify_conviction(score):
    """Map intelligence score to Falcon conviction rating tiers."""
    if score >= 95:
        return "LEGENDARY"
    if score >= 90:
        return "ELITE"
    if score >= 80:
        return "STRONG"
    return "WATCH"


def calculate_intelligence_engine(pair, previous_token):
    """Weighted Falcon Intelligence Engine for high-conviction opportunities."""
    score = 0
    reasons = []
    penalties = []

    liquidity = safe_number(pair.get("liquidity", {}).get("usd"))
    market_cap = safe_number(pair.get("marketCap") or pair.get("fdv"))
    volume_5m = safe_number(pair.get("volume", {}).get("m5"))
    price_5m = safe_number(pair.get("priceChange", {}).get("m5"))
    txns_5m = pair.get("txns", {}).get("m5", {})
    buys_5m = int(txns_5m.get("buys", 0) or 0)
    sells_5m = int(txns_5m.get("sells", 0) or 0)
    total_txns_5m = buys_5m + sells_5m
    pair_age_minutes = get_pair_age_minutes(pair)
    holder_count = get_holder_count(pair)

    previous_volume_5m = safe_number((previous_token or {}).get("volume_5m_usd"))
    previous_price_5m = safe_number((previous_token or {}).get("price_change_5m_pct"))
    previous_holder_count = (previous_token or {}).get("holder_count")
    try:
        previous_holder_count = int(previous_holder_count)
    except (TypeError, ValueError):
        previous_holder_count = None

    if 10_000 <= liquidity <= 250_000:
        score += 25
        reasons.append("+25 liquidity between $10k and $250k")

    if buys_5m > sells_5m:
        score += 20
        reasons.append("+20 buy volume exceeds sell volume")

    if previous_token and volume_5m > previous_volume_5m:
        score += 15
        reasons.append("+15 5-minute volume increasing scan-over-scan")

    if (
        holder_count is not None
        and previous_holder_count is not None
        and holder_count > previous_holder_count
    ):
        score += 10
        reasons.append("+10 holder count increasing")

    if pair_age_minutes is not None and pair_age_minutes < 30:
        score += 10
        reasons.append("+10 token age under 30 minutes")

    if 0 < market_cap < 250_000:
        score += 10
        reasons.append("+10 market cap below $250k")

    if total_txns_5m >= 20:
        score += 5
        reasons.append("+5 healthy transaction count")

    if previous_token and price_5m > previous_price_5m:
        score += 5
        reasons.append("+5 momentum increasing")

    avg_tx_size = (volume_5m / total_txns_5m) if total_txns_5m > 0 else 0.0
    if avg_tx_size > 2_500 and total_txns_5m < 20:
        score -= 20
        penalties.append("-20 large whale concentration suspected")

    if liquidity < 10_000:
        score -= 20
        penalties.append("-20 low liquidity")

    if sells_5m > buys_5m * 1.2 and sells_5m >= 10:
        score -= 15
        penalties.append("-15 sell pressure")

    honeypot_like = (
        liquidity < 5_000
        and volume_5m > 10_000
        and buys_5m > sells_5m * 1.8
    )
    if honeypot_like:
        score -= 20
        penalties.append("-20 honeypot indicator risk")

    rug_risk = False
    if market_cap > 0:
        liq_to_mcap = liquidity / market_cap
        rug_risk = liq_to_mcap < 0.03 and pair_age_minutes is not None and pair_age_minutes < 60
    if rug_risk:
        score -= 25
        penalties.append("-25 rug risk profile")

    bounded_score = int(max(0, min(score, 100)))
    conviction = classify_conviction(bounded_score)
    all_reasons = reasons + penalties
    if not all_reasons:
        all_reasons = ["No weighted criteria triggered"]

    return {
        "falcon_intelligence_score": bounded_score,
        "conviction_rating": conviction,
        "falcon_intelligence_reasons": all_reasons,
        "falcon_intelligence_penalties": penalties,
        "holder_count": holder_count,
        "pair_age_minutes": pair_age_minutes,
    }


def classify_signal(
    score,
    confidence,
    momentum,
    risk_label,
    liquidity,
    volume_5m,
    buys_5m,
    sells_5m,
    source_confirmation_count=0,
    acceleration_score=0,
    social_heat_label="QUIET",
):
    """Classify trade readiness via weighted checklist with minimum hit counts."""
    total_txns_5m = buys_5m + sells_5m
    buy_pressure = buys_5m >= sells_5m
    strong_buy_pressure = buys_5m >= sells_5m * 1.1 and buys_5m >= 10

    if liquidity < 10_000:
        return "PASS", ["liquidity too low"]
    if volume_5m < 500:
        return "PASS", ["5-minute volume too low"]
    if sells_5m > buys_5m * 1.35 and sells_5m >= 12:
        return "PASS", ["sell pressure dominates buys"]
    if risk_label == "HIGH" and momentum == "BEARISH":
        return "PASS", ["high risk with bearish momentum"]

    checks = [
        ("score>=55", score >= 55, 3),
        ("confidence>=60", confidence >= 60, 3),
        ("momentum bullish", momentum == "BULLISH", 2),
        ("risk not high", risk_label in ("LOW", "MEDIUM"), 2),
        ("liquidity>=25k", liquidity >= 25_000, 1),
        ("volume5m>=5k", volume_5m >= 5_000, 1),
        ("buys>=sells", buy_pressure, 1),
        ("strong buy pressure", strong_buy_pressure, 1),
        ("txn activity>=10", total_txns_5m >= 10, 1),
        ("source confirmations>=2", source_confirmation_count >= 2, 2),
        ("acceleration>=10", acceleration_score >= 10, 2),
    ]

    passed_checks = [name for name, ok, _ in checks if ok]
    failed_checks = [name for name, ok, _ in checks if not ok]
    weighted_score = sum(weight for _, ok, weight in checks if ok)
    passed_count = len(passed_checks)

    core_checks = [
        score >= 55,
        confidence >= 60,
        momentum == "BULLISH",
        risk_label in ("LOW", "MEDIUM"),
    ]
    core_hits = sum(1 for ok in core_checks if ok)

    # BUY requires a strong combination, not necessarily every condition.
    # Thresholds: weighted>=11, at least 6 checks passed, and at least 3 core hits.
    signal_cfg = FALCON_SCORING_CONFIG["signal"]

    if (
        score >= signal_cfg["buy_min"]
        and
        weighted_score >= 13
        and passed_count >= 7
        and core_hits >= 3
        and source_confirmation_count >= signal_cfg["min_confirmation_for_buy"]
    ):
        reasons = [
            "weighted strength is high",
            f"{passed_count} checklist conditions satisfied",
            f"{core_hits}/4 core conditions satisfied",
            f"{source_confirmation_count} source confirmations",
        ]
        return "BUY", reasons

    # WATCH captures near-BUY setups with decent combined strength.
    # Path A: weighted>=7, at least 4 checks, at least 2 core hits, no high-risk bearish state.
    # Path B: weighted>=6 with bullish momentum and risk not high.
    watch_path_a = score >= signal_cfg["watch_min"] and weighted_score >= 8 and passed_count >= 5 and core_hits >= 2
    watch_path_b = score >= signal_cfg["watch_min"] and weighted_score >= 6 and momentum == "BULLISH" and risk_label != "HIGH"
    if watch_path_a or watch_path_b:
        reasons = [
            "near-BUY weighted checklist",
            f"{passed_count} checklist conditions satisfied",
            f"source confirmations: {source_confirmation_count}",
            "missing: " + ", ".join(failed_checks[:2]) if failed_checks else "minor gaps only",
        ]
        return "WATCH", reasons

    reasons = [
        "insufficient combined checklist strength",
        f"{passed_count} checklist conditions satisfied",
        "missing: " + ", ".join(failed_checks[:2]) if failed_checks else "multiple weaknesses",
    ]
    return "PASS", reasons


def calculate_confidence(score, liquidity, volume_5m, buys_5m, sells_5m, momentum):
    """
    Conservative confidence score (0-100) based on score, liquidity,
    volume, buy/sell balance, and short-term momentum.
    """
    confidence = 0.0

    confidence += max(0, min(score, 100)) * 0.5

    if liquidity >= 100_000:
        confidence += 15
    elif liquidity >= 50_000:
        confidence += 11
    elif liquidity >= 25_000:
        confidence += 8
    elif liquidity >= 10_000:
        confidence += 4

    if volume_5m >= 20_000:
        confidence += 15
    elif volume_5m >= 10_000:
        confidence += 11
    elif volume_5m >= 5_000:
        confidence += 8
    elif volume_5m >= 2_000:
        confidence += 4

    if buys_5m > sells_5m * 1.5 and buys_5m >= 10:
        confidence += 10
    elif buys_5m > sells_5m:
        confidence += 7
    elif buys_5m == sells_5m and (buys_5m + sells_5m) >= 10:
        confidence += 4

    if momentum == "BULLISH":
        confidence += 10
    elif momentum == "NEUTRAL":
        confidence += 5

    return int(max(0, min(round(confidence), 100)))


def normalize_token_shape(token):
    """Ensure required trade-readiness fields exist with safe default types."""
    normalized = dict(token)
    normalized["signal"] = str(normalized.get("signal", "PASS") or "PASS")
    normalized["momentum"] = str(normalized.get("momentum", "NEUTRAL") or "NEUTRAL")
    normalized["risk_label"] = str(normalized.get("risk_label", "MEDIUM") or "MEDIUM")

    confidence = normalized.get("confidence", 0)
    try:
        confidence = int(confidence)
    except (TypeError, ValueError):
        confidence = 0
    normalized["confidence"] = max(0, min(confidence, 100))

    for key in REQUIRED_TOKEN_KEYS:
        if key not in normalized:
            raise KeyError(f"Missing required token key: {key}")

    return normalized


def get_best_pair(pairs):
    """Choose the pair with the most USD liquidity."""
    return max(
        pairs,
        key=lambda pair: safe_number(pair.get("liquidity", {}).get("usd")),
    )


def scan_tokens(max_tokens=200, top_n=30):
    """
    Scan multi-source Solana token candidates and return structured opportunities.
    Existing scoring, alerting, and memory logic stays unchanged.
    """
    try:
        scanned_at = datetime.now(timezone.utc).isoformat()
        scanned_at_dt = parse_iso_datetime(scanned_at) or datetime.now(timezone.utc)
        seen_contracts = load_seen_contracts()
        smart_wallet_tracker = prune_smart_wallet_tracker(
            load_smart_wallet_tracker(),
            scanned_at_dt,
        )
        previous_snapshot = load_latest_snapshot()
        previous_tokens = (previous_snapshot or {}).get("tokens", [])
        previous_by_contract = {
            token.get("contract_address"): token
            for token in previous_tokens
            if token.get("contract_address")
        }

        collection = collect_all_candidates(max_candidates=max_tokens)
        candidates = collection.get("candidates", [])
        scanner_status = collection.get("scanner_status", [])
        scanner_elapsed_ms = int(collection.get("elapsed_ms", 0) or 0)

        if not candidates:
            alert_report = ALERT_ENGINE.process_scan([], scanned_at)
            record_disappeared_tokens(previous_tokens, [], scanned_at)
            save_snapshot(scanned_at, [])
            return {
                "ok": True,
                "error": None,
                "scanned_at": scanned_at,
                "opportunities_count": 0,
                "highest_score": 0,
                "new_tokens_detected": 0,
                "scanner_status": scanner_status,
                "scanner_elapsed_ms": scanner_elapsed_ms,
                "tokens": [],
                "alerts": alert_report.to_dict(),
            }

        opportunities = []
        seen_contracts_in_scan = set()
        new_tokens_detected = []

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue

            pair = ((candidate.get("raw_data") or {}).get("pair") or {})
            if not isinstance(pair, dict) or not pair:
                continue

            base = pair.get("baseToken", {})
            contract_address = str(candidate.get("token_address") or base.get("address", "") or "").strip()
            if contract_address:
                base["address"] = contract_address
            previous_token = previous_by_contract.get(contract_address)
            base_market_score, base_reasons = calculate_score(pair)
            risk_label = classify_risk(base_market_score, pair)
            if not contract_address or contract_address in seen_contracts_in_scan:
                continue
            seen_contracts_in_scan.add(contract_address)

            txns_5m = pair.get("txns", {}).get("m5", {})
            liquidity_usd = safe_number(pair.get("liquidity", {}).get("usd"))
            volume_5m_usd = safe_number(pair.get("volume", {}).get("m5"))
            price_change_5m_pct = safe_number(pair.get("priceChange", {}).get("m5"))
            buys_5m = int(txns_5m.get("buys", 0) or 0)
            sells_5m = int(txns_5m.get("sells", 0) or 0)
            pair_age_minutes = get_pair_age_minutes(pair)

            estimated_wallet_count, smart_wallet_reasons = estimate_tracked_wallet_buys(
                pair,
                pair_age_minutes,
            )
            recent_wallet_count = get_recent_wallet_count(
                smart_wallet_tracker,
                contract_address,
                scanned_at_dt,
                window_minutes=10,
            )
            combined_wallet_count = recent_wallet_count + estimated_wallet_count
            wallet_bonus, wallet_bonus_reason = get_wallet_cluster_bonus(combined_wallet_count)

            append_wallet_event(
                smart_wallet_tracker,
                contract_address,
                scanned_at,
                estimated_wallet_count,
            )

            intelligence = calculate_intelligence_engine(pair, previous_token)
            social_intelligence = SOCIAL_ENGINE.evaluate(
                SocialContext(
                    pair=pair,
                    previous_token=previous_token,
                    boost_amount=safe_number((candidate.get("raw_data") or {}).get("boost_amount", 0)),
                    holder_count=intelligence.get("holder_count"),
                    scanned_at=scanned_at_dt,
                )
            )
            source_confirmation = derive_source_confirmation(candidate)
            acceleration = compute_acceleration_metrics(pair, previous_token)
            score_bundle = compose_falcon_score(
                base_market_score=base_market_score,
                intelligence_score=intelligence.get("falcon_intelligence_score", 0),
                liquidity_usd=liquidity_usd,
                risk_label=risk_label,
                pair=pair,
                candidate=candidate,
                pair_age_minutes=pair_age_minutes,
                social_heat_label=str(social_intelligence.get("social_heat_label", "QUIET") or "QUIET"),
                source_confirmation=source_confirmation,
                acceleration=acceleration,
            )
            score = int(score_bundle["score"])
            reasons = list(base_reasons) + list(intelligence.get("falcon_intelligence_reasons", [])) + score_bundle["score_reasons"]
            score_penalties = list(intelligence.get("falcon_intelligence_penalties", [])) + score_bundle["score_penalties"]

            if wallet_bonus and risk_label != "HIGH":
                score = min(100, score + wallet_bonus)
                reasons.append(wallet_bonus_reason)

            momentum = classify_momentum(price_change_5m_pct, buys_5m, sells_5m)
            confidence = calculate_confidence(
                score,
                liquidity_usd,
                volume_5m_usd,
                buys_5m,
                sells_5m,
                momentum,
            )
            signal, signal_reasons = classify_signal(
                score=score,
                confidence=confidence,
                momentum=momentum,
                risk_label=risk_label,
                liquidity=liquidity_usd,
                volume_5m=volume_5m_usd,
                buys_5m=buys_5m,
                sells_5m=sells_5m,
                source_confirmation_count=source_confirmation["source_confirmation_count"],
                acceleration_score=acceleration["acceleration_score"],
                social_heat_label=str(social_intelligence.get("social_heat_label", "QUIET") or "QUIET"),
            )

            smart_wallet_high = combined_wallet_count >= 3
            buy_now_reasons = []
            signal_cfg = FALCON_SCORING_CONFIG["signal"]
            if (
                score >= signal_cfg["buy_now_min"]
                and risk_label in ("LOW", "MEDIUM")
                and source_confirmation["source_confirmation_count"] >= signal_cfg["min_confirmation_for_buy_now"]
                and acceleration["acceleration_score"] >= 14
                and confidence >= 70
                and momentum == "BULLISH"
            ):
                signal = "BUY NOW"
                buy_now_reasons = [
                    "unusually strong early momentum and acceleration",
                    f"{source_confirmation['source_confirmation_count']} independent source confirmations",
                    f"Falcon Score is {score} (>= {signal_cfg['buy_now_min']})",
                ]
                signal_reasons = buy_now_reasons + signal_reasons

            high_priority_alert = False
            high_priority_reasons = []
            social_heat_label = str(social_intelligence.get("social_heat_label", "QUIET") or "QUIET")
            if (
                score >= signal_cfg["high_priority_min"]
                and social_heat_label in ("HOT", "VIRAL")
                and source_confirmation["source_confirmation_count"] >= signal_cfg["min_confirmation_for_high_priority"]
                and acceleration["acceleration_score"] >= 16
                and smart_wallet_high
                and risk_label in ("LOW", "MEDIUM")
            ):
                high_priority_alert = True
                high_priority_reasons = [
                    f"Social Heat is {social_heat_label}",
                    "market acceleration and smart-wallet convergence detected",
                    f"{source_confirmation['source_confirmation_count']} source confirmations",
                    f"Falcon Score is {score} (>= {signal_cfg['high_priority_min']})",
                ]

            is_brand_new = contract_address not in seen_contracts
            if is_brand_new:
                new_tokens_detected.append(
                    {
                        "token_name": base.get("name", "Unknown"),
                        "token_symbol": base.get("symbol", "UNKNOWN"),
                        "contract_address": contract_address,
                    }
                )

            current_token = {
                "score": score,
                "risk_label": risk_label,
                "signal": signal,
                "signal_reasons": signal_reasons,
                "momentum": momentum,
                "confidence": confidence,
                "reasons": reasons,
                "score_penalties": score_penalties,
                "token_name": base.get("name", "Unknown"),
                "token_symbol": base.get("symbol", "UNKNOWN"),
                "contract_address": contract_address or "N/A",
                "market_cap_usd": safe_number(pair.get("marketCap") or pair.get("fdv")),
                "liquidity_usd": liquidity_usd,
                "volume_5m_usd": volume_5m_usd,
                "price_change_5m_pct": price_change_5m_pct,
                "buys_5m": buys_5m,
                "sells_5m": sells_5m,
                "falcon_intelligence_score": intelligence["falcon_intelligence_score"],
                "conviction_rating": intelligence["conviction_rating"],
                "falcon_intelligence_reasons": intelligence["falcon_intelligence_reasons"],
                "falcon_intelligence_penalties": intelligence["falcon_intelligence_penalties"],
                "holder_count": intelligence["holder_count"],
                "holder_concentration_pct": score_bundle["holder_concentration_pct"],
                "pair_age_minutes": intelligence["pair_age_minutes"],
                "smart_wallet_count": combined_wallet_count,
                "smart_wallet_display": get_smart_wallet_display(combined_wallet_count),
                "smart_wallet_high": smart_wallet_high,
                "smart_wallet_reasons": smart_wallet_reasons,
                "social_heat_score": social_intelligence.get("social_heat_score", 0),
                "social_heat_label": social_intelligence.get("social_heat_label", "QUIET"),
                "social_heat": social_intelligence.get("social_heat_badge", "⚪ QUIET"),
                "social_heat_reasons": social_intelligence.get("social_heat_reasons", []),
                "social_provider_scores": social_intelligence.get("social_provider_scores", {}),
                "source_confirmations": source_confirmation["source_confirmations"],
                "source_confirmation_count": source_confirmation["source_confirmation_count"],
                "source_confirmation_names": source_confirmation["source_confirmation_names"],
                "source_confirmation_score": source_confirmation["source_confirmation_score"],
                "acceleration_score": acceleration["acceleration_score"],
                "volume_growth_ratio_5m": acceleration["volume_growth_ratio"],
                "price_delta_5m_pct": acceleration["price_delta_5m_pct"],
                "buy_sell_ratio_5m": acceleration["buy_sell_ratio_5m"],
                "high_priority_alert": high_priority_alert,
                "high_priority_reasons": high_priority_reasons,
                "buy_now_reasons": buy_now_reasons,
                "healthy_buy_sell_ratio": has_healthy_buy_sell_ratio(buys_5m, sells_5m),
                "highlight": qualifies_highlight(
                    score,
                    liquidity_usd,
                    price_change_5m_pct,
                    buys_5m,
                    sells_5m,
                ),
                "is_brand_new": is_brand_new,
                "dexscreener_url": pair.get("url", ""),
                "boost_amount": safe_number((candidate.get("raw_data") or {}).get("boost_amount", 0)),
                "source": str(candidate.get("source", "unknown")),
                "source_url": str(candidate.get("source_url", "")),
                "source_names": list((candidate.get("raw_data") or {}).get("found_by", [candidate.get("source", "unknown")])),
                "source_discovered_at": str(candidate.get("discovered_at", scanned_at)),
                "source_pair_address": str(candidate.get("pair_address", "")),
                "source_social_mentions": int(candidate.get("social_mentions", 0) or 0),
                "telegram_channels": list((candidate.get("raw_data") or {}).get("telegram_channels", [])),
                "telegram_messages": list((candidate.get("raw_data") or {}).get("telegram_messages", [])),
            }
            current_token.update(build_memory_delta(current_token, previous_token))

            opportunities.append(
                normalize_token_shape(current_token)
            )

        opportunities.sort(key=lambda item: item["score"], reverse=True)
        top_tokens = opportunities[:top_n]

        top_tokens = [normalize_token_shape(token) for token in top_tokens]

        if seen_contracts_in_scan:
            save_seen_contracts(seen_contracts.union(seen_contracts_in_scan))
        save_smart_wallet_tracker(smart_wallet_tracker)

        for token in new_tokens_detected:
            print(
                "NEW TOKEN DETECTED | "
                f"{token.get('token_name', 'Unknown')} "
                f"({token.get('token_symbol', 'UNKNOWN')}) | "
                f"{token.get('contract_address', 'N/A')}"
            )

        alert_report = ALERT_ENGINE.process_scan(opportunities, scanned_at)
        record_disappeared_tokens(previous_tokens, opportunities, scanned_at)
        save_snapshot(scanned_at, opportunities)

        return {
            "ok": True,
            "error": None,
            "scanned_at": scanned_at,
            "opportunities_count": len(top_tokens),
            "highest_score": top_tokens[0]["score"] if top_tokens else 0,
            "new_tokens_detected": len(new_tokens_detected),
            "scanner_status": scanner_status,
            "scanner_elapsed_ms": scanner_elapsed_ms,
            "candidates_rated": len(opportunities),
            "tokens": top_tokens,
            "alerts": alert_report.to_dict(),
        }

    except requests.RequestException as error:
        return {
            "ok": False,
            "error": f"Network/API error: {error}",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "opportunities_count": 0,
            "highest_score": 0,
            "new_tokens_detected": 0,
            "scanner_status": [],
            "scanner_elapsed_ms": 0,
            "tokens": [],
            "alerts": {
                "enabled": ALERT_ENGINE.config.enabled,
                "dry_run": ALERT_ENGINE.config.dry_run,
                "evaluated": 0,
                "eligible": 0,
                "sent": 0,
                "suppressed_by_cooldown": 0,
                "suppressed_by_contract": 0,
                "errors": 1,
                "mode": "error",
            },
        }
    except Exception as error:
        return {
            "ok": False,
            "error": f"Unexpected error: {error}",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "opportunities_count": 0,
            "highest_score": 0,
            "new_tokens_detected": 0,
            "scanner_status": [],
            "scanner_elapsed_ms": 0,
            "tokens": [],
            "alerts": {
                "enabled": ALERT_ENGINE.config.enabled,
                "dry_run": ALERT_ENGINE.config.dry_run,
                "evaluated": 0,
                "eligible": 0,
                "sent": 0,
                "suppressed_by_cooldown": 0,
                "suppressed_by_contract": 0,
                "errors": 1,
                "mode": "error",
            },
        }


def send_test_alert():
    """Trigger a safe Falcon TEST ALERT to validate Telegram connectivity."""
    return ALERT_ENGINE.send_test_alert()


def main():
    print("\nCRYPTO HUNTER AI")
    print("Scanning current boosted Solana tokens...\n")

    payload = scan_tokens()
    if not payload.get("ok"):
        print(payload.get("error", "Unknown scan error"))
        return

    tokens = payload.get("tokens", [])
    if not tokens:
        print("No Solana opportunities were returned.")
        return

    for position, token in enumerate(tokens, start=1):
        print("=" * 65)
        print(
            f"#{position} {token.get('token_name', 'Unknown')} "
            f"({token.get('token_symbol', 'UNKNOWN')}) — SCORE: {token.get('score', 0)}/100"
        )
        print(
            "Trade:       "
            f"{token.get('signal', 'PASS')} | {token.get('momentum', 'NEUTRAL')} | "
            f"Confidence {token.get('confidence', 0)}"
        )
        print(
            "Decision:    "
            + (", ".join(token.get("signal_reasons", [])) or "No decision reasons")
        )
        print(f"Risk:        {token.get('risk_label', 'MEDIUM')}")
        print(f"Liquidity:   ${token.get('liquidity_usd', 0):,.0f}")
        print(f"Market cap:  ${token.get('market_cap_usd', 0):,.0f}")
        print(f"Volume 5m:   ${token.get('volume_5m_usd', 0):,.0f}")
        print(f"Price 5m:    {token.get('price_change_5m_pct', 0):+.2f}%")
        print(
            "Buys/Sells:  "
            f"{token.get('buys_5m', 0)}/{token.get('sells_5m', 0)} during the last 5 minutes"
        )
        print(f"Paid boosts: {token.get('boost_amount', 0)}")
        print("Reasons:     " + (", ".join(token.get("reasons", [])) or "No strong signals"))
        print("DexScreener: " + (token.get("dexscreener_url") or "No link"))


if __name__ == "__main__":
    main()