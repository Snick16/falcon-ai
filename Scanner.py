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
    """Create a basic momentum score from 0 to 100."""
    score = 0
    reasons = []

    liquidity = safe_number(pair.get("liquidity", {}).get("usd"))
    volume_1h = safe_number(pair.get("volume", {}).get("h1"))
    volume_5m = safe_number(pair.get("volume", {}).get("m5"))
    price_5m = safe_number(pair.get("priceChange", {}).get("m5"))
    price_1h = safe_number(pair.get("priceChange", {}).get("h1"))

    transactions = pair.get("txns", {}).get("m5", {})
    buys_5m = int(transactions.get("buys", 0) or 0)
    sells_5m = int(transactions.get("sells", 0) or 0)

    if liquidity >= 100_000:
        score += 20
        reasons.append("healthy liquidity")
    elif liquidity >= 50_000:
        score += 10

    if volume_1h >= 100_000:
        score += 20
        reasons.append("strong 1-hour volume")
    elif volume_1h >= 25_000:
        score += 10

    if volume_5m >= 10_000:
        score += 15
        reasons.append("active 5-minute volume")

    if buys_5m > sells_5m * 1.5 and buys_5m >= 10:
        score += 20
        reasons.append("buys beating sells")
    elif buys_5m > sells_5m:
        score += 10

    if 1 <= price_5m <= 20:
        score += 10
        reasons.append("positive 5-minute momentum")

    if 2 <= price_1h <= 50:
        score += 10
        reasons.append("positive 1-hour momentum")

    pair_created = pair.get("pairCreatedAt")
    if pair_created:
        age_hours = (
            datetime.now(timezone.utc).timestamp()
            - pair_created / 1000
        ) / 3600

        if age_hours >= 1:
            score += 5
        if age_hours < 0.25:
            score -= 15
            reasons.append("extremely new pair")

    return max(0, min(score, 100)), reasons


def get_best_pair(pairs):
    """Choose the pair with the most USD liquidity."""
    return max(
        pairs,
        key=lambda pair: safe_number(pair.get("liquidity", {}).get("usd")),
    )


def main():
    print("\nCRYPTO HUNTER AI")
    print("Scanning current boosted Solana tokens...\n")

    try:
        boost_response = requests.get(BOOSTS_URL, timeout=15)
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

            if len(addresses) == 30:
                break

        if not addresses:
            print("No Solana tokens were returned.")
            return

        token_response = requests.get(
            TOKEN_URL.format(",".join(addresses)),
            timeout=20,
        )
        token_response.raise_for_status()
        all_pairs = token_response.json()

        pairs_by_token = {}

        for pair in all_pairs:
            address = pair.get("baseToken", {}).get("address")
            if address:
                pairs_by_token.setdefault(address, []).append(pair)

        results = []

        for address in addresses:
            token_pairs = pairs_by_token.get(address, [])
            if not token_pairs:
                continue

            pair = get_best_pair(token_pairs)
            score, reasons = calculate_score(pair)
            results.append((score, pair, reasons, boost_amounts.get(address, 0)))

        results.sort(key=lambda item: item[0], reverse=True)

        for position, (score, pair, reasons, boost_amount) in enumerate(
            results[:15],
            start=1,
        ):
            base = pair.get("baseToken", {})
            txns_5m = pair.get("txns", {}).get("m5", {})

            symbol = base.get("symbol", "UNKNOWN")
            name = base.get("name", "Unknown")
            price = pair.get("priceUsd", "N/A")
            liquidity = safe_number(pair.get("liquidity", {}).get("usd"))
            volume_5m = safe_number(pair.get("volume", {}).get("m5"))
            volume_1h = safe_number(pair.get("volume", {}).get("h1"))
            market_cap = safe_number(pair.get("marketCap") or pair.get("fdv"))
            change_5m = safe_number(pair.get("priceChange", {}).get("m5"))
            change_1h = safe_number(pair.get("priceChange", {}).get("h1"))
            buys = int(txns_5m.get("buys", 0) or 0)
            sells = int(txns_5m.get("sells", 0) or 0)

            print("=" * 65)
            print(f"#{position} {name} ({symbol}) — SCORE: {score}/100")
            print(f"Price:       ${price}")
            print(f"Liquidity:   ${liquidity:,.0f}")
            print(f"Market cap:  ${market_cap:,.0f}")
            print(f"Volume 5m:   ${volume_5m:,.0f}")
            print(f"Volume 1h:   ${volume_1h:,.0f}")
            print(f"Price 5m:    {change_5m:+.2f}%")
            print(f"Price 1h:    {change_1h:+.2f}%")
            print(f"Buys/Sells:  {buys}/{sells} during the last 5 minutes")
            print(f"Paid boosts: {boost_amount}")
            print("Reasons:     " + (", ".join(reasons) or "No strong signals"))
            print("DexScreener: " + pair.get("url", "No link"))

    except requests.RequestException as error:
        print(f"Network/API error: {error}")
    except Exception as error:
        print(f"Unexpected error: {error}")


if __name__ == "__main__":
    main()