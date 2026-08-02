import requests
from datetime import datetime, timezone

BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
TOKEN_URL = "https://api.dexscreener.com/tokens/v1/solana/{}"


def safe_number(value):
    """Convert missing or invalid numbers to zero."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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


def classify_signal(score, price_5m, liquidity, volume_5m):
    """Classify trade readiness as BUY, WATCH, or PASS."""
    buy_requirements = [
        score >= 70,
        price_5m > 0,
        liquidity >= 25_000,
        volume_5m >= 5_000,
    ]

    if all(buy_requirements):
        return "BUY"

    non_score_requirements = buy_requirements[1:]
    missing_non_score = sum(1 for passed in non_score_requirements if not passed)
    if 50 <= score <= 69 or (score >= 70 and missing_non_score == 1):
        return "WATCH"

    return "PASS"


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
            return {
                "ok": True,
                "error": None,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
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
            signal = classify_signal(score, price_change_5m_pct, liquidity_usd, volume_5m_usd)
            confidence = calculate_confidence(
                score,
                liquidity_usd,
                volume_5m_usd,
                buys_5m,
                sells_5m,
                momentum,
            )

            opportunities.append(
                {
                    "score": score,
                    "risk_label": risk_label,
                    "signal": signal,
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
            )

        opportunities.sort(key=lambda item: item["score"], reverse=True)
        top_tokens = opportunities[:top_n]

        return {
            "ok": True,
            "error": None,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
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