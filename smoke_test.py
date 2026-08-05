import os
import py_compile


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def run_compile_checks():
    py_compile.compile("Scanner.py", doraise=True)
    py_compile.compile("dashboard.py", doraise=True)
    py_compile.compile("source_scanner.py", doraise=True)


def run_scanner_shape_checks():
    from source_scanner import collect_all_candidates, scan_dexscreener_latest  # noqa: PLC0415

    latest = scan_dexscreener_latest(limit=5)
    assert_true(isinstance(latest, list), "scan_dexscreener_latest should return a list")

    required_candidate_keys = {
        "chain",
        "token_address",
        "symbol",
        "name",
        "source",
        "source_url",
        "pair_address",
        "discovered_at",
        "market_cap",
        "liquidity",
        "volume_5m",
        "volume_1h",
        "volume_24h",
        "price_change_5m",
        "price_change_1h",
        "buys_5m",
        "sells_5m",
        "token_age_minutes",
        "social_mentions",
        "raw_data",
    }

    for row in latest[:3]:
        row_dict = row.__dict__ if hasattr(row, "__dict__") else {}
        missing = required_candidate_keys - set(row_dict.keys())
        assert_true(not missing, f"Missing candidate keys: {sorted(missing)}")

    original_env = {
        "X_BEARER_TOKEN": os.environ.get("X_BEARER_TOKEN"),
        "X_SEARCH_TERMS": os.environ.get("X_SEARCH_TERMS"),
        "TELEGRAM_SCAN_API_URL": os.environ.get("TELEGRAM_SCAN_API_URL"),
        "PUMPFUN_ENABLED": os.environ.get("PUMPFUN_ENABLED"),
    }
    try:
        os.environ.pop("X_BEARER_TOKEN", None)
        os.environ.pop("X_SEARCH_TERMS", None)
        os.environ.pop("TELEGRAM_SCAN_API_URL", None)
        os.environ["PUMPFUN_ENABLED"] = "false"

        payload = collect_all_candidates(max_candidates=30)
        assert_true(isinstance(payload, dict), "collect_all_candidates should return a dict")
        statuses = payload.get("scanner_status", [])
        assert_true(isinstance(statuses, list), "scanner_status should be a list")

        status_by_source = {row.get("source"): row for row in statuses if isinstance(row, dict)}
        for source_name in ("x_social", "telegram_channels", "pumpfun_tokens"):
            source_status = status_by_source.get(source_name)
            assert_true(source_status is not None, f"Missing scanner status for {source_name}")
            assert_true(source_status.get("configured") is False, f"{source_name} should be not configured")
            assert_true(isinstance(source_status.get("error", ""), str), f"{source_name} should provide status message")

        candidates = payload.get("candidates", [])
        token_addresses = [str(item.get("token_address", "")).strip() for item in candidates if isinstance(item, dict)]
        non_empty_addresses = [address for address in token_addresses if address]
        assert_true(
            len(non_empty_addresses) == len(set(non_empty_addresses)),
            "Duplicate token addresses should be merged by collect_all_candidates",
        )
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    run_compile_checks()
    run_scanner_shape_checks()
    print("Smoke test passed.")
