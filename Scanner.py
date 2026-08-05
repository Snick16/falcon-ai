import requests
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from falcon_alerts import create_default_alert_engine
from social_intelligence import SocialContext, create_default_social_engine
from source_scanner import collect_all_candidates
from whale_scanner import scan_whale_wallets

BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
TOKEN_URL = "https://api.dexscreener.com/tokens/v1/solana/{}"
REQUIRED_TOKEN_KEYS = ("signal", "momentum", "confidence", "risk_label")
MEMORY_DIR = Path(__file__).resolve().parent / ".falcon_memory"
SNAPSHOTS_DIR = MEMORY_DIR / "snapshots"
DISAPPEARED_HISTORY_FILE = MEMORY_DIR / "disappeared_history.jsonl"
SCANNED_CONTRACTS_FILE = MEMORY_DIR / "scanned_contracts.json"
SMART_WALLET_TRACKER_FILE = MEMORY_DIR / "smart_wallet_tracker.json"
FIRST_SEEN_CONTRACTS_FILE = MEMORY_DIR / "first_seen_contracts.json"
WHALE_EVIDENCE_IDS_FILE = MEMORY_DIR / "whale_evidence_ids.json"
MAX_SNAPSHOTS = 500
SOCIAL_ENGINE = create_default_social_engine()
ALERT_ENGINE = create_default_alert_engine()

DEFAULT_SOURCE_TRUST_WEIGHTS = {
    "DexScreener": 5,
    "Pump.fun": 7,
    "X": 6,
    "Telegram": 6,
    "GMGN": 8,
    "TrojanOnSolana": 7,
    "solanakingcalls": 7,
    "Bonk": 5,
}


def safe_number(value):
    """Convert missing or invalid numbers to zero."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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


def load_first_seen_contracts():
    """Load first-seen timestamps keyed by contract address."""
    ensure_memory_dirs()
    if not FIRST_SEEN_CONTRACTS_FILE.exists():
        return {}

    try:
        with FIRST_SEEN_CONTRACTS_FILE.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(raw, dict):
        return {}

    normalized = {}
    for contract, first_seen in raw.items():
        key = str(contract or "").strip()
        value = str(first_seen or "").strip()
        if not key or not value:
            continue
        normalized[key] = value
    return normalized


def save_first_seen_contracts(first_seen_by_contract):
    """Persist first-seen timestamps for detected contracts."""
    ensure_memory_dirs()
    serialized = {
        str(contract).strip(): str(first_seen).strip()
        for contract, first_seen in (first_seen_by_contract or {}).items()
        if str(contract).strip() and str(first_seen).strip()
    }
    with FIRST_SEEN_CONTRACTS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(dict(sorted(serialized.items())), handle, ensure_ascii=True)


def load_whale_evidence_ids():
    """Load persistent whale evidence ids to avoid counting same proof twice."""
    ensure_memory_dirs()
    if not WHALE_EVIDENCE_IDS_FILE.exists():
        return set()

    try:
        with WHALE_EVIDENCE_IDS_FILE.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return set()

    if not isinstance(raw, list):
        return set()

    return {
        str(item).strip()
        for item in raw
        if str(item).strip()
    }


def save_whale_evidence_ids(evidence_ids):
    """Persist whale evidence ids seen by Scanner integration."""
    ensure_memory_dirs()
    serialized = sorted(
        {
            str(item).strip()
            for item in (evidence_ids or set())
            if str(item).strip()
        }
    )
    with WHALE_EVIDENCE_IDS_FILE.open("w", encoding="utf-8") as handle:
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


def _normalize_trust_key(value):
    return str(value or "").strip().lower().lstrip("@")


def load_source_trust_weights():
    """Load trust weights from safe defaults with optional JSON env override."""
    weights = {
        _normalize_trust_key(name): int(value)
        for name, value in DEFAULT_SOURCE_TRUST_WEIGHTS.items()
    }

    raw = os.getenv("SOURCE_TRUST_WEIGHTS", "").strip()
    if not raw:
        return weights

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return weights

    if not isinstance(parsed, dict):
        return weights

    for key, value in parsed.items():
        normalized_key = _normalize_trust_key(key)
        if not normalized_key:
            continue
        try:
            numeric = int(float(value))
        except (TypeError, ValueError):
            continue
        weights[normalized_key] = max(0, min(numeric, 20))

    return weights


def collect_unique_source_evidence(candidate, smart_wallet_count=0):
    """Collect unique source/channel/account evidence keys for trust scoring."""
    raw_data = (candidate or {}).get("raw_data", {}) if isinstance(candidate, dict) else {}
    found_by = raw_data.get("found_by", []) if isinstance(raw_data.get("found_by"), list) else []

    evidence_keys = set()

    found_by_set = {str(item or "").strip() for item in found_by if str(item or "").strip()}
    if found_by_set.intersection({"dexscreener_latest", "dexscreener_boosted", "dexscreener_trending", "new_solana_pairs"}):
        evidence_keys.add(_normalize_trust_key("DexScreener"))
    if "pumpfun_tokens" in found_by_set:
        evidence_keys.add(_normalize_trust_key("Pump.fun"))
    if "x_social" in found_by_set:
        evidence_keys.add(_normalize_trust_key("X"))
    if "telegram_channels" in found_by_set:
        evidence_keys.add(_normalize_trust_key("Telegram"))

    telegram_channels = raw_data.get("telegram_channels", [])
    if isinstance(telegram_channels, list):
        for channel in telegram_channels:
            normalized = _normalize_trust_key(channel)
            if normalized:
                evidence_keys.add(normalized)

    x_authors = raw_data.get("x_author_usernames", [])
    if isinstance(x_authors, list):
        for author in x_authors:
            normalized = _normalize_trust_key(author)
            if normalized:
                evidence_keys.add(normalized)

    if int(smart_wallet_count or 0) > 0:
        evidence_keys.add(_normalize_trust_key("Smart"))

    return evidence_keys


def calculate_source_trust_bonus(candidate, smart_wallet_count=0, score_before_trust=0):
    """Apply modest trust bonus from unique sources/channels/accounts."""
    weights = load_source_trust_weights()
    evidence_keys = collect_unique_source_evidence(candidate, smart_wallet_count)
    evidence_with_weights = {
        key: int(weights.get(key, 0))
        for key in evidence_keys
        if key in weights
    }

    if not evidence_with_weights:
        return 0, [], {}

    # Treat weight 5 as neutral; only incremental trust above neutral adds score.
    raw_bonus = sum(max(0, value - 5) for value in evidence_with_weights.values())
    trust_bonus = min(8, raw_bonus)

    if len(evidence_with_weights) <= 1:
        trust_bonus = min(trust_bonus, 4)
        if int(score_before_trust or 0) < 90:
            trust_bonus = min(trust_bonus, max(0, 89 - int(score_before_trust or 0)))

    evidence_labels = sorted(evidence_with_weights.keys())
    return int(trust_bonus), evidence_labels, evidence_with_weights


def format_elapsed_since(earlier_dt, later_dt):
    """Format elapsed time between two UTC datetimes for dashboard labels."""
    if earlier_dt is None or later_dt is None:
        return "N/A"
    delta_seconds = max(0, int((later_dt - earlier_dt).total_seconds()))
    if delta_seconds < 60:
        return f"{delta_seconds}s ago"

    minutes = delta_seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"

    hours = minutes // 60
    remainder_minutes = minutes % 60
    if hours < 24:
        if remainder_minutes == 0:
            return f"{hours}h ago"
        return f"{hours}h {remainder_minutes}m ago"

    days = hours // 24
    remainder_hours = hours % 24
    if remainder_hours == 0:
        return f"{days}d ago"
    return f"{days}d {remainder_hours}h ago"


def get_or_set_first_seen(first_seen_by_contract, contract_address, scanned_at):
    """Return existing first-seen timestamp or set it once if missing/invalid."""
    existing = str((first_seen_by_contract or {}).get(contract_address, "") or "").strip()
    if parse_iso_datetime(existing) is not None:
        return existing

    first_seen_by_contract[contract_address] = scanned_at
    return scanned_at


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


def _calculate_pumpfun_points(candidate, pair):
    raw_data = (candidate or {}).get("raw_data", {}) if isinstance(candidate, dict) else {}
    found_by = set(raw_data.get("found_by", [])) if isinstance(raw_data.get("found_by", []), list) else set()
    if "pumpfun_tokens" not in found_by:
        return 0

    discovered_dt = parse_iso_datetime(raw_data.get("pumpfun_created_at"))
    if discovered_dt is None:
        discovered_dt = parse_iso_datetime((candidate or {}).get("discovered_at"))
    age_minutes = None
    if discovered_dt is not None:
        age_minutes = max(0.0, (datetime.now(timezone.utc) - discovered_dt).total_seconds() / 60.0)
    if age_minutes is None:
        age_minutes = get_pair_age_minutes(pair)

    if age_minutes is None:
        return 18
    if age_minutes <= 15:
        return 30
    if age_minutes <= 60:
        return 24
    if age_minutes <= 180:
        return 18
    return 10


def _calculate_telegram_points(candidate):
    raw_data = (candidate or {}).get("raw_data", {}) if isinstance(candidate, dict) else {}
    message_count = len(raw_data.get("telegram_messages", [])) if isinstance(raw_data.get("telegram_messages", []), list) else 0
    channel_count = len(raw_data.get("telegram_channels", [])) if isinstance(raw_data.get("telegram_channels", []), list) else 0
    return min(25, (message_count * 6) + (channel_count * 4))


def _calculate_x_points(candidate):
    raw_data = (candidate or {}).get("raw_data", {}) if isinstance(candidate, dict) else {}
    mention_count = int(raw_data.get("x_mention_count", 0) or 0)
    unique_author_count = int(raw_data.get("x_unique_author_count", 0) or 0)
    return min(20, (mention_count * 4) + (unique_author_count * 4))


def _calculate_dex_activity_points(pair):
    liquidity = safe_number(pair.get("liquidity", {}).get("usd"))
    volume_5m = safe_number(pair.get("volume", {}).get("m5"))
    price_5m = safe_number(pair.get("priceChange", {}).get("m5"))
    transactions = pair.get("txns", {}).get("m5", {})
    buys_5m = int(transactions.get("buys", 0) or 0)
    sells_5m = int(transactions.get("sells", 0) or 0)
    total_txns_5m = buys_5m + sells_5m

    points = 0
    if liquidity >= 100_000:
        points += 5
    elif liquidity >= 50_000:
        points += 4
    elif liquidity >= 20_000:
        points += 3
    elif liquidity >= 10_000:
        points += 2

    if volume_5m >= 20_000:
        points += 5
    elif volume_5m >= 8_000:
        points += 4
    elif volume_5m >= 3_000:
        points += 3
    elif volume_5m >= 1_000:
        points += 2

    if buys_5m > sells_5m and buys_5m >= 10:
        points += 3
    elif buys_5m >= sells_5m and total_txns_5m >= 8:
        points += 2

    if 0.5 <= price_5m <= 15:
        points += 2

    return min(points, 15)


def _calculate_smart_wallet_points(smart_wallet_count):
    if smart_wallet_count >= 3:
        return 10
    if smart_wallet_count == 2:
        return 7
    if smart_wallet_count == 1:
        return 3
    return 0


def calculate_score(pair, candidate=None, smart_wallet_count=0):
    """Create Falcon Rating v2 from source and activity component buckets."""
    pump_points = _calculate_pumpfun_points(candidate, pair)
    telegram_points = _calculate_telegram_points(candidate)
    x_points = _calculate_x_points(candidate)
    dex_points = _calculate_dex_activity_points(pair)
    smart_points = _calculate_smart_wallet_points(smart_wallet_count)

    raw_data = (candidate or {}).get("raw_data", {}) if isinstance(candidate, dict) else {}
    found_by = set(raw_data.get("found_by", [])) if isinstance(raw_data.get("found_by", []), list) else set()

    has_pumpfun = "pumpfun_tokens" in found_by
    has_telegram = "telegram_channels" in found_by
    has_x = "x_social" in found_by

    confidence_bonus = 0
    confidence_bonus_reason = "None"
    if has_pumpfun and has_telegram and has_x:
        confidence_bonus = 20
        confidence_bonus_reason = "Pump.fun + Telegram + X"
    elif has_telegram and has_x:
        confidence_bonus = 15
        confidence_bonus_reason = "Telegram + X"
    elif has_pumpfun and has_telegram:
        confidence_bonus = 10
        confidence_bonus_reason = "Pump.fun + Telegram"
    elif has_pumpfun and has_x:
        confidence_bonus = 10
        confidence_bonus_reason = "Pump.fun + X"

    base_score = pump_points + telegram_points + x_points + dex_points + smart_points
    score_pre_trust = base_score + confidence_bonus
    trust_bonus, trust_evidence, trust_weight_hits = calculate_source_trust_bonus(
        candidate,
        smart_wallet_count=smart_wallet_count,
        score_before_trust=score_pre_trust,
    )
    score = max(0, min(score_pre_trust + trust_bonus, 100))
    breakdown = {
        "pump": pump_points,
        "telegram": telegram_points,
        "x": x_points,
        "dex": dex_points,
        "smart": smart_points,
        "confidence_bonus": confidence_bonus,
        "trust_bonus": trust_bonus,
        "trust_evidence": trust_evidence,
        "trust_weight_hits": trust_weight_hits,
    }
    breakdown_lines = [
        f"Pump: {pump_points}",
        f"Telegram: {telegram_points}",
        f"X: {x_points}",
        f"Dex: {dex_points}",
        f"Smart: {smart_points}",
        f"Confidence bonus: +{confidence_bonus} ({confidence_bonus_reason})",
        f"Trust bonus: +{trust_bonus} ({', '.join(trust_evidence) if trust_evidence else 'none'})",
    ]
    return score, breakdown_lines, breakdown


def classify_confidence_tier(score):
    """Map final Falcon score to confidence tier labels."""
    score_value = int(max(0, min(100, safe_number(score))))
    if score_value < 60:
        return "LOW"
    if score_value <= 74:
        return "MEDIUM"
    if score_value <= 89:
        return "HIGH"
    return "ELITE"


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
    if weighted_score >= 11 and passed_count >= 6 and core_hits >= 3:
        reasons = [
            "weighted strength is high",
            f"{passed_count} checklist conditions satisfied",
            f"{core_hits}/4 core conditions satisfied",
        ]
        return "BUY", reasons

    # WATCH captures near-BUY setups with decent combined strength.
    # Path A: weighted>=7, at least 4 checks, at least 2 core hits, no high-risk bearish state.
    # Path B: weighted>=6 with bullish momentum and risk not high.
    watch_path_a = weighted_score >= 7 and passed_count >= 4 and core_hits >= 2
    watch_path_b = weighted_score >= 6 and momentum == "BULLISH" and risk_label != "HIGH"
    if watch_path_a or watch_path_b:
        reasons = [
            "near-BUY weighted checklist",
            f"{passed_count} checklist conditions satisfied",
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

    confidence_tier = str(normalized.get("confidence_tier", "") or "").strip().upper()
    if confidence_tier not in {"LOW", "MEDIUM", "HIGH", "ELITE"}:
        confidence_tier = classify_confidence_tier(normalized.get("score", 0))
    normalized["confidence_tier"] = confidence_tier

    if not isinstance(normalized.get("score_breakdown"), dict):
        normalized["score_breakdown"] = {}
    if not isinstance(normalized.get("score_breakdown_lines"), list):
        normalized["score_breakdown_lines"] = []

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
        first_seen_by_contract = load_first_seen_contracts()
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
        whale_signals, whale_details = scan_whale_wallets(limit_wallets=20)
        whale_details = whale_details if isinstance(whale_details, dict) else {}
        whale_status = {
            "source": "whale_wallets",
            "configured": bool(whale_details.get("configured", False)),
            "success": bool(whale_details.get("success", True)),
            "candidates_found": int(whale_details.get("contracts_detected", 0) or 0),
            "elapsed_ms": int(whale_details.get("elapsed_ms", 0) or 0),
            "error": str(whale_details.get("error_message", "") or ""),
            "details": whale_details,
        }
        if isinstance(scanner_status, list):
            scanner_status.append(whale_status)
        scanner_elapsed_ms += int(whale_details.get("elapsed_ms", 0) or 0)
        whale_signals = whale_signals if isinstance(whale_signals, dict) else {}
        seen_whale_evidence_ids = load_whale_evidence_ids()

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
            whale_signal = whale_signals.get(contract_address, {}) if isinstance(whale_signals, dict) else {}
            whale_evidence_rows = whale_signal.get("evidence", []) if isinstance(whale_signal, dict) else []
            new_whale_evidence = []
            if isinstance(whale_evidence_rows, list):
                for row in whale_evidence_rows:
                    if not isinstance(row, dict):
                        continue
                    evidence_id = str(row.get("evidence_id", "") or "").strip()
                    if not evidence_id or evidence_id in seen_whale_evidence_ids:
                        continue
                    seen_whale_evidence_ids.add(evidence_id)
                    new_whale_evidence.append(row)

            confirmed_whale_wallets = sorted(
                {
                    str(row.get("wallet", "") or "").strip()
                    for row in new_whale_evidence
                    if str(row.get("wallet", "") or "").strip()
                }
            )
            confirmed_whale_count = len(confirmed_whale_wallets)
            if confirmed_whale_count > 0:
                smart_wallet_reasons = list(smart_wallet_reasons) + [
                    f"confirmed whale buys from {confirmed_whale_count} configured wallet(s)"
                ]

            combined_wallet_count = recent_wallet_count + estimated_wallet_count + confirmed_whale_count

            score, breakdown_lines, breakdown = calculate_score(
                pair,
                candidate=candidate,
                smart_wallet_count=combined_wallet_count,
            )
            reasons = list(breakdown_lines)
            risk_label = classify_risk(score, pair)

            append_wallet_event(
                smart_wallet_tracker,
                contract_address,
                scanned_at,
                estimated_wallet_count + confirmed_whale_count,
            )

            momentum = classify_momentum(price_change_5m_pct, buys_5m, sells_5m)
            confidence = calculate_confidence(
                score,
                liquidity_usd,
                volume_5m_usd,
                buys_5m,
                sells_5m,
                momentum,
            )
            confidence_tier = classify_confidence_tier(score)
            signal, signal_reasons = classify_signal(
                score=score,
                confidence=confidence,
                momentum=momentum,
                risk_label=risk_label,
                liquidity=liquidity_usd,
                volume_5m=volume_5m_usd,
                buys_5m=buys_5m,
                sells_5m=sells_5m,
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

            smart_wallet_high = combined_wallet_count >= 3
            buy_now_reasons = []
            if smart_wallet_high and score > 90:
                signal = "BUY NOW"
                buy_now_reasons = [
                    "Smart wallet cluster is HIGH (3+ tracked wallets in 10 minutes)",
                    f"Falcon Score is {score} (>90)",
                ]
                signal_reasons = buy_now_reasons + signal_reasons

            high_priority_alert = False
            high_priority_reasons = []
            if social_intelligence.get("social_heat_label") == "VIRAL" and score > 90:
                high_priority_alert = True
                high_priority_reasons = [
                    "Social Heat is VIRAL",
                    f"Falcon Score is {score} (>90)",
                ]

            is_brand_new = contract_address not in seen_contracts
            first_seen_at = get_or_set_first_seen(
                first_seen_by_contract,
                contract_address,
                scanned_at,
            )
            first_seen_dt = parse_iso_datetime(first_seen_at) or scanned_at_dt
            first_seen_ago = format_elapsed_since(first_seen_dt, scanned_at_dt)

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
                "confidence_tier": confidence_tier,
                "reasons": reasons,
                "score_breakdown": breakdown,
                "score_breakdown_lines": breakdown_lines,
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
                "pair_age_minutes": intelligence["pair_age_minutes"],
                "smart_wallet_count": combined_wallet_count,
                "smart_wallet_display": get_smart_wallet_display(combined_wallet_count),
                "smart_wallet_high": smart_wallet_high,
                "smart_wallet_reasons": smart_wallet_reasons,
                "whale_confirmed_buy_count": int((whale_signal or {}).get("buy_count", 0) or 0),
                "whale_wallets": confirmed_whale_wallets,
                "whale_last_buy_at": str((whale_signal or {}).get("last_buy_at", "") or ""),
                "whale_new_evidence_count": len(new_whale_evidence),
                "social_heat_score": social_intelligence.get("social_heat_score", 0),
                "social_heat_label": social_intelligence.get("social_heat_label", "QUIET"),
                "social_heat": social_intelligence.get("social_heat_badge", "⚪ QUIET"),
                "social_heat_reasons": social_intelligence.get("social_heat_reasons", []),
                "social_provider_scores": social_intelligence.get("social_provider_scores", {}),
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
                "first_seen_at": first_seen_at,
                "first_seen_ago": first_seen_ago,
                "last_updated_at": scanned_at,
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
        save_first_seen_contracts(first_seen_by_contract)
        save_whale_evidence_ids(seen_whale_evidence_ids)
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