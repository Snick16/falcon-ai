import requests
import json
from datetime import datetime, timezone
from pathlib import Path

BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
TOKEN_URL = "https://api.dexscreener.com/tokens/v1/solana/{}"
REQUIRED_TOKEN_KEYS = ("signal", "momentum", "confidence", "risk_label")
MEMORY_DIR = Path(__file__).resolve().parent / ".falcon_memory"
SNAPSHOTS_DIR = MEMORY_DIR / "snapshots"
DISAPPEARED_HISTORY_FILE = MEMORY_DIR / "disappeared_history.jsonl"
MAX_SNAPSHOTS = 500


def safe_number(value):
    """Convert missing or invalid numbers to zero."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def ensure_memory_dirs():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


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


def scan_tokens(max_tokens=30, top_n=15):
    """
    Scan boosted Solana tokens and return structured opportunities.
    This keeps scoring logic unchanged and avoids printing-only output.
    """
    try:
        scanned_at = datetime.now(timezone.utc).isoformat()
        previous_snapshot = load_latest_snapshot()
        previous_tokens = (previous_snapshot or {}).get("tokens", [])
        previous_by_contract = {
            token.get("contract_address"): token
            for token in previous_tokens
            if token.get("contract_address")
        }

        boost_response = requests.get(BOOSTS_URL, timeout=15, verify=False)
        boost_response.raise_for_status()
        boosts = boost_response.json()

        addresses = []
        boost_amounts = {}

        for token in boosts:
            if token.get("chainId") != "solana":
                continue

            address = token.get("tokenAddress")
            if address and address not in addresses:
                addresses.append(address)
                boost_amounts[address] = token.get("totalAmount", token.get("amount", 0))

            if len(addresses) == max_tokens:
                break

        if not addresses:
            record_disappeared_tokens(previous_tokens, [], scanned_at)
            save_snapshot(scanned_at, [])
            return {
                "ok": True,
                "error": None,
                "scanned_at": scanned_at,
                "opportunities_count": 0,
                "highest_score": 0,
                "tokens": [],
            }

        token_response = requests.get(
            TOKEN_URL.format(",".join(addresses)),
            timeout=20,
            verify=False
        )
        token_response.raise_for_status()
        all_pairs = token_response.json()

        pairs_by_token = {}

        for pair in all_pairs:
            address = pair.get("baseToken", {}).get("address")
            if address:
                pairs_by_token.setdefault(address, []).append(pair)

        opportunities = []

        for address in addresses:
            token_pairs = pairs_by_token.get(address, [])
            if not token_pairs:
                continue

            pair = get_best_pair(token_pairs)
            score, reasons = calculate_score(pair)
            risk_label = classify_risk(score, pair)
            base = pair.get("baseToken", {})
            txns_5m = pair.get("txns", {}).get("m5", {})
            liquidity_usd = safe_number(pair.get("liquidity", {}).get("usd"))
            volume_5m_usd = safe_number(pair.get("volume", {}).get("m5"))
            price_change_5m_pct = safe_number(pair.get("priceChange", {}).get("m5"))
            buys_5m = int(txns_5m.get("buys", 0) or 0)
            sells_5m = int(txns_5m.get("sells", 0) or 0)

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
            )

            current_token = {
                "score": score,
                "risk_label": risk_label,
                "signal": signal,
                "signal_reasons": signal_reasons,
                "momentum": momentum,
                "confidence": confidence,
                "reasons": reasons,
                "token_name": base.get("name", "Unknown"),
                "token_symbol": base.get("symbol", "UNKNOWN"),
                "contract_address": base.get("address", "N/A"),
                "market_cap_usd": safe_number(pair.get("marketCap") or pair.get("fdv")),
                "liquidity_usd": liquidity_usd,
                "volume_5m_usd": volume_5m_usd,
                "price_change_5m_pct": price_change_5m_pct,
                "buys_5m": buys_5m,
                "sells_5m": sells_5m,
                "dexscreener_url": pair.get("url", ""),
                "boost_amount": boost_amounts.get(address, 0),
            }
            previous_token = previous_by_contract.get(current_token.get("contract_address"))
            current_token.update(build_memory_delta(current_token, previous_token))

            opportunities.append(
                normalize_token_shape(current_token)
            )

        opportunities.sort(key=lambda item: item["score"], reverse=True)
        top_tokens = opportunities[:top_n]

        top_tokens = [normalize_token_shape(token) for token in top_tokens]

        record_disappeared_tokens(previous_tokens, opportunities, scanned_at)
        save_snapshot(scanned_at, opportunities)

        if top_tokens:
            print(top_tokens[0])

        return {
            "ok": True,
            "error": None,
            "scanned_at": scanned_at,
            "opportunities_count": len(top_tokens),
            "highest_score": top_tokens[0]["score"] if top_tokens else 0,
            "tokens": top_tokens,
        }

    except requests.RequestException as error:
        return {
            "ok": False,
            "error": f"Network/API error: {error}",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "opportunities_count": 0,
            "highest_score": 0,
            "tokens": [],
        }
    except Exception as error:
        return {
            "ok": False,
            "error": f"Unexpected error: {error}",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "opportunities_count": 0,
            "highest_score": 0,
            "tokens": [],
        }


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