import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests


DEX_BOOSTS_LATEST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_TOKEN_PROFILES_LATEST_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_TOKEN_BATCH_URL = "https://api.dexscreener.com/tokens/v1/solana/{}"
DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"

SOLANA_CHAIN_ID = "solana"
SOLANA_ADDRESS_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_RETRIES = 2


@dataclass
class TokenCandidate:
    chain: str
    token_address: str
    symbol: str
    name: str
    source: str
    source_url: str
    pair_address: str
    discovered_at: str
    market_cap: float
    liquidity: float
    volume_5m: float
    volume_1h: float
    volume_24h: float
    price_change_5m: float
    price_change_1h: float
    buys_5m: int
    sells_5m: int
    token_age_minutes: float
    social_mentions: int
    raw_data: dict = field(default_factory=dict)


@dataclass
class ScannerSourceStatus:
    source: str
    configured: bool
    success: bool
    candidates_found: int
    elapsed_ms: int
    error: str


@dataclass
class CollectionResult:
    candidates: List[TokenCandidate]
    scanner_status: List[ScannerSourceStatus]
    elapsed_ms: int


def _safe_number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_address(address: str) -> str:
    return str(address or "").strip()


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _request_json(
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
):
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                verify=False,
            )
            response.raise_for_status()
            return response.json()
        except Exception as error:  # noqa: BLE001 - scanner should capture and report all source errors.
            last_error = error
            if attempt < retries:
                time.sleep(0.25 * (attempt + 1))

    raise RuntimeError(f"Request failed for {url}: {last_error}") from last_error


def _chunked(items: Sequence[str], chunk_size: int) -> Iterable[List[str]]:
    if chunk_size <= 0:
        chunk_size = 1
    for index in range(0, len(items), chunk_size):
        yield list(items[index:index + chunk_size])


def _best_pair(pairs: Sequence[dict]) -> Optional[dict]:
    if not pairs:
        return None
    return max(
        pairs,
        key=lambda pair: _safe_number((pair or {}).get("liquidity", {}).get("usd")),
    )


def _token_age_minutes(pair: dict) -> float:
    created_ms = (pair or {}).get("pairCreatedAt")
    if not created_ms:
        return 0.0
    age_seconds = datetime.now(timezone.utc).timestamp() - (_safe_number(created_ms) / 1000.0)
    return max(0.0, age_seconds / 60.0)


def _build_candidate_from_pair(
    pair: dict,
    *,
    source: str,
    source_url: str,
    discovered_at: str,
    social_mentions: int = 0,
    extra_raw: Optional[dict] = None,
) -> Optional[TokenCandidate]:
    if not isinstance(pair, dict):
        return None

    chain = str(pair.get("chainId", "") or "").strip().lower()
    if chain != SOLANA_CHAIN_ID:
        return None

    base = pair.get("baseToken", {}) if isinstance(pair.get("baseToken"), dict) else {}
    token_address = _normalize_address(base.get("address"))
    if not token_address:
        return None

    txns_5m = pair.get("txns", {}).get("m5", {}) if isinstance(pair.get("txns"), dict) else {}
    volume = pair.get("volume", {}) if isinstance(pair.get("volume"), dict) else {}
    price = pair.get("priceChange", {}) if isinstance(pair.get("priceChange"), dict) else {}
    liquidity = pair.get("liquidity", {}) if isinstance(pair.get("liquidity"), dict) else {}

    raw_data = {
        "pair": pair,
        "found_by": [source],
    }
    if extra_raw:
        raw_data.update(extra_raw)

    return TokenCandidate(
        chain=SOLANA_CHAIN_ID,
        token_address=token_address,
        symbol=str(base.get("symbol", "UNKNOWN") or "UNKNOWN"),
        name=str(base.get("name", "Unknown") or "Unknown"),
        source=source,
        source_url=source_url or str(pair.get("url", "") or ""),
        pair_address=str(pair.get("pairAddress", "") or ""),
        discovered_at=discovered_at,
        market_cap=_safe_number(pair.get("marketCap") or pair.get("fdv")),
        liquidity=_safe_number(liquidity.get("usd")),
        volume_5m=_safe_number(volume.get("m5")),
        volume_1h=_safe_number(volume.get("h1")),
        volume_24h=_safe_number(volume.get("h24")),
        price_change_5m=_safe_number(price.get("m5")),
        price_change_1h=_safe_number(price.get("h1")),
        buys_5m=_safe_int(txns_5m.get("buys")),
        sells_5m=_safe_int(txns_5m.get("sells")),
        token_age_minutes=_token_age_minutes(pair),
        social_mentions=max(0, _safe_int(social_mentions)),
        raw_data=raw_data,
    )


def _fetch_pairs_for_token_addresses(token_addresses: Sequence[str], timeout: int = 20) -> Dict[str, List[dict]]:
    pairs_by_token: Dict[str, List[dict]] = {}
    cleaned = []
    seen = set()
    for token_address in token_addresses:
        normalized = _normalize_address(token_address)
        if normalized and normalized not in seen:
            seen.add(normalized)
            cleaned.append(normalized)

    for chunk in _chunked(cleaned, 30):
        payload = _request_json(
            DEX_TOKEN_BATCH_URL.format(",".join(chunk)),
            timeout=timeout,
            retries=2,
        )
        if not isinstance(payload, list):
            continue

        for pair in payload:
            if not isinstance(pair, dict):
                continue
            if str(pair.get("chainId", "") or "").lower() != SOLANA_CHAIN_ID:
                continue
            base = pair.get("baseToken", {}) if isinstance(pair.get("baseToken"), dict) else {}
            token_address = _normalize_address(base.get("address"))
            if not token_address:
                continue
            pairs_by_token.setdefault(token_address, []).append(pair)

    return pairs_by_token


def _collect_from_boosts_endpoint(endpoint: str, source: str, limit: int) -> List[TokenCandidate]:
    discovered_at = _utc_now_iso()
    payload = _request_json(endpoint, timeout=15, retries=2)
    if not isinstance(payload, list):
        return []

    token_addresses = []
    boost_by_token: Dict[str, float] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        if str(item.get("chainId", "") or "").lower() != SOLANA_CHAIN_ID:
            continue
        token_address = _normalize_address(item.get("tokenAddress"))
        if not token_address:
            continue
        if token_address in boost_by_token:
            continue
        token_addresses.append(token_address)
        boost_by_token[token_address] = _safe_number(item.get("totalAmount", item.get("amount", 0)))
        if len(token_addresses) >= max(1, limit):
            break

    if not token_addresses:
        return []

    pairs_by_token = _fetch_pairs_for_token_addresses(token_addresses)
    candidates: List[TokenCandidate] = []
    for token_address in token_addresses:
        pair = _best_pair(pairs_by_token.get(token_address, []))
        if not pair:
            continue
        candidate = _build_candidate_from_pair(
            pair,
            source=source,
            source_url=str(pair.get("url", "") or endpoint),
            discovered_at=discovered_at,
            extra_raw={"boost_amount": boost_by_token.get(token_address, 0)},
        )
        if candidate:
            candidates.append(candidate)
    return candidates


def scan_dexscreener_latest(limit: int = 80) -> List[TokenCandidate]:
    return _collect_from_boosts_endpoint(
        DEX_BOOSTS_LATEST_URL,
        source="dexscreener_latest",
        limit=limit,
    )


def scan_dexscreener_boosted(limit: int = 80) -> List[TokenCandidate]:
    return _collect_from_boosts_endpoint(
        DEX_BOOSTS_TOP_URL,
        source="dexscreener_boosted",
        limit=limit,
    )


def scan_dexscreener_trending(limit: int = 80) -> List[TokenCandidate]:
    discovered_at = _utc_now_iso()
    pairs: List[dict] = []

    queries = ["solana", "raydium", "pump"]
    for query in queries:
        payload = _request_json(
            DEX_SEARCH_URL,
            params={"q": query},
            timeout=15,
            retries=1,
        )
        for pair in (payload or {}).get("pairs", []) if isinstance(payload, dict) else []:
            if not isinstance(pair, dict):
                continue
            if str(pair.get("chainId", "") or "").lower() != SOLANA_CHAIN_ID:
                continue
            base = pair.get("baseToken", {}) if isinstance(pair.get("baseToken"), dict) else {}
            if not _normalize_address(base.get("address")):
                continue
            pairs.append(pair)

    # Rank by current liquidity plus near-term activity to prioritize active pairs.
    ranked = sorted(
        pairs,
        key=lambda pair: (
            _safe_number(pair.get("liquidity", {}).get("usd")),
            _safe_number(pair.get("volume", {}).get("m5")),
            _safe_number(pair.get("volume", {}).get("h1")),
        ),
        reverse=True,
    )

    candidates: List[TokenCandidate] = []
    seen_contracts = set()
    for pair in ranked:
        base = pair.get("baseToken", {}) if isinstance(pair.get("baseToken"), dict) else {}
        token_address = _normalize_address(base.get("address"))
        if not token_address or token_address in seen_contracts:
            continue
        seen_contracts.add(token_address)

        candidate = _build_candidate_from_pair(
            pair,
            source="dexscreener_trending",
            source_url=str(pair.get("url", "") or DEX_SEARCH_URL),
            discovered_at=discovered_at,
        )
        if candidate:
            candidates.append(candidate)
        if len(candidates) >= max(1, limit):
            break

    return candidates


def scan_new_solana_pairs(limit: int = 80) -> List[TokenCandidate]:
    discovered_at = _utc_now_iso()
    payload = _request_json(DEX_TOKEN_PROFILES_LATEST_URL, timeout=15, retries=2)
    if not isinstance(payload, list):
        return []

    token_addresses: List[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if str(item.get("chainId", "") or "").lower() != SOLANA_CHAIN_ID:
            continue
        token_address = _normalize_address(item.get("tokenAddress"))
        if not token_address:
            continue
        token_addresses.append(token_address)
        if len(token_addresses) >= max(1, limit):
            break

    if not token_addresses:
        return []

    pairs_by_token = _fetch_pairs_for_token_addresses(token_addresses)
    candidates: List[TokenCandidate] = []
    for token_address in token_addresses:
        pair = _best_pair(pairs_by_token.get(token_address, []))
        if not pair:
            continue
        candidate = _build_candidate_from_pair(
            pair,
            source="new_solana_pairs",
            source_url=str(pair.get("url", "") or DEX_TOKEN_PROFILES_LATEST_URL),
            discovered_at=discovered_at,
        )
        if candidate:
            candidates.append(candidate)
    return candidates


def scan_pumpfun_tokens(limit: int = 50) -> List[TokenCandidate]:
    if not _bool_env("PUMPFUN_ENABLED", False):
        return []

    api_url = os.getenv("PUMPFUN_API_URL", "").strip()
    if not api_url:
        return []

    headers = {}
    api_key = os.getenv("PUMPFUN_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = _request_json(api_url, headers=headers, timeout=12, retries=1)
    if isinstance(payload, dict):
        items = payload.get("tokens") or payload.get("items") or payload.get("data") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    token_addresses: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        token_address = _normalize_address(item.get("tokenAddress") or item.get("mint") or item.get("address"))
        if token_address:
            token_addresses.append(token_address)
        if len(token_addresses) >= max(1, limit):
            break

    if not token_addresses:
        return []

    pairs_by_token = _fetch_pairs_for_token_addresses(token_addresses)
    candidates: List[TokenCandidate] = []
    discovered_at = _utc_now_iso()
    for token_address in token_addresses:
        pair = _best_pair(pairs_by_token.get(token_address, []))
        if not pair:
            continue
        candidate = _build_candidate_from_pair(
            pair,
            source="pumpfun_tokens",
            source_url=str(pair.get("url", "") or api_url),
            discovered_at=discovered_at,
        )
        if candidate:
            candidates.append(candidate)
    return candidates


def _extract_solana_addresses_from_text(text: str) -> List[str]:
    if not text:
        return []
    found = SOLANA_ADDRESS_RE.findall(str(text))
    cleaned = []
    seen = set()
    for address in found:
        normalized = _normalize_address(address)
        if normalized and normalized not in seen:
            seen.add(normalized)
            cleaned.append(normalized)
    return cleaned


def scan_x_social(limit: int = 40) -> List[TokenCandidate]:
    bearer = os.getenv("X_BEARER_TOKEN", "").strip()
    terms = os.getenv("X_SEARCH_TERMS", "").strip()
    if not bearer or not terms:
        return []

    api_url = os.getenv("X_API_URL", "https://api.twitter.com/2/tweets/search/recent").strip()
    headers = {"Authorization": f"Bearer {bearer}"}

    all_addresses: List[str] = []
    for raw_term in [item.strip() for item in terms.split(",") if item.strip()]:
        payload = _request_json(
            api_url,
            params={"query": raw_term, "max_results": 20, "tweet.fields": "created_at,text"},
            headers=headers,
            timeout=10,
            retries=1,
        )
        rows = (payload or {}).get("data", []) if isinstance(payload, dict) else []
        for row in rows:
            text = str((row or {}).get("text", "") or "")
            for address in _extract_solana_addresses_from_text(text):
                all_addresses.append(address)

    seen = set()
    unique_addresses: List[str] = []
    for address in all_addresses:
        if address in seen:
            continue
        seen.add(address)
        unique_addresses.append(address)
        if len(unique_addresses) >= max(1, limit):
            break

    if not unique_addresses:
        return []

    pairs_by_token = _fetch_pairs_for_token_addresses(unique_addresses)
    candidates: List[TokenCandidate] = []
    discovered_at = _utc_now_iso()
    for token_address in unique_addresses:
        pair = _best_pair(pairs_by_token.get(token_address, []))
        if not pair:
            continue
        candidate = _build_candidate_from_pair(
            pair,
            source="x_social",
            source_url=str(pair.get("url", "") or api_url),
            discovered_at=discovered_at,
            social_mentions=1,
        )
        if candidate:
            candidates.append(candidate)
    return candidates


def scan_telegram_channels(limit: int = 40) -> List[TokenCandidate]:
    scan_api_url = os.getenv("TELEGRAM_SCAN_API_URL", "").strip()
    if not scan_api_url:
        return []

    headers = {}
    bearer = os.getenv("TELEGRAM_SCAN_BEARER", "").strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    payload = _request_json(scan_api_url, headers=headers, timeout=12, retries=1)
    if isinstance(payload, dict):
        messages = payload.get("messages") or payload.get("items") or payload.get("data") or []
    elif isinstance(payload, list):
        messages = payload
    else:
        messages = []

    all_addresses: List[str] = []
    for row in messages:
        text = str((row or {}).get("text", "") or "") if isinstance(row, dict) else str(row)
        for address in _extract_solana_addresses_from_text(text):
            all_addresses.append(address)

    seen = set()
    unique_addresses: List[str] = []
    for address in all_addresses:
        if address in seen:
            continue
        seen.add(address)
        unique_addresses.append(address)
        if len(unique_addresses) >= max(1, limit):
            break

    if not unique_addresses:
        return []

    pairs_by_token = _fetch_pairs_for_token_addresses(unique_addresses)
    candidates: List[TokenCandidate] = []
    discovered_at = _utc_now_iso()
    for token_address in unique_addresses:
        pair = _best_pair(pairs_by_token.get(token_address, []))
        if not pair:
            continue
        candidate = _build_candidate_from_pair(
            pair,
            source="telegram_channels",
            source_url=str(pair.get("url", "") or scan_api_url),
            discovered_at=discovered_at,
            social_mentions=1,
        )
        if candidate:
            candidates.append(candidate)
    return candidates


def _run_source(
    source_name: str,
    configured: bool,
    scanner_fn,
    scanner_kwargs: Optional[dict] = None,
    not_configured_message: str = "",
) -> Tuple[List[TokenCandidate], ScannerSourceStatus]:
    scanner_kwargs = scanner_kwargs or {}
    started = time.perf_counter()

    if not configured:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return [], ScannerSourceStatus(
            source=source_name,
            configured=False,
            success=True,
            candidates_found=0,
            elapsed_ms=elapsed_ms,
            error=not_configured_message or f"{source_name} scanner not configured.",
        )

    try:
        candidates = scanner_fn(**scanner_kwargs)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return candidates, ScannerSourceStatus(
            source=source_name,
            configured=True,
            success=True,
            candidates_found=len(candidates),
            elapsed_ms=elapsed_ms,
            error="",
        )
    except Exception as error:  # noqa: BLE001 - source-level isolation is required.
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return [], ScannerSourceStatus(
            source=source_name,
            configured=True,
            success=False,
            candidates_found=0,
            elapsed_ms=elapsed_ms,
            error=str(error),
        )


def _dedupe_candidates(candidates: Sequence[TokenCandidate]) -> List[TokenCandidate]:
    merged: Dict[str, TokenCandidate] = {}

    for candidate in candidates:
        key = _normalize_address(candidate.token_address)
        if not key:
            continue

        existing = merged.get(key)
        if not existing:
            merged[key] = candidate
            continue

        existing_sources = existing.raw_data.get("found_by", [existing.source])
        candidate_sources = candidate.raw_data.get("found_by", [candidate.source])
        merged_sources = sorted({str(item) for item in existing_sources + candidate_sources if item})
        existing.raw_data["found_by"] = merged_sources

        # Keep richer market snapshot where available.
        if candidate.liquidity > existing.liquidity:
            merged_candidate = candidate
            merged_candidate.raw_data["found_by"] = merged_sources
            merged[key] = merged_candidate

    return list(merged.values())


def collect_all_candidates(max_candidates: int = 200) -> Dict[str, object]:
    started = time.perf_counter()
    all_candidates: List[TokenCandidate] = []
    scanner_status: List[ScannerSourceStatus] = []

    source_jobs = [
        {
            "source_name": "dexscreener_latest",
            "configured": True,
            "scanner_fn": scan_dexscreener_latest,
            "scanner_kwargs": {"limit": max(40, max_candidates // 2)},
            "not_configured_message": "",
        },
        {
            "source_name": "dexscreener_boosted",
            "configured": True,
            "scanner_fn": scan_dexscreener_boosted,
            "scanner_kwargs": {"limit": max(40, max_candidates // 2)},
            "not_configured_message": "",
        },
        {
            "source_name": "dexscreener_trending",
            "configured": True,
            "scanner_fn": scan_dexscreener_trending,
            "scanner_kwargs": {"limit": max(50, max_candidates // 2)},
            "not_configured_message": "",
        },
        {
            "source_name": "new_solana_pairs",
            "configured": True,
            "scanner_fn": scan_new_solana_pairs,
            "scanner_kwargs": {"limit": max(50, max_candidates // 2)},
            "not_configured_message": "",
        },
        {
            "source_name": "pumpfun_tokens",
            "configured": _bool_env("PUMPFUN_ENABLED", False),
            "scanner_fn": scan_pumpfun_tokens,
            "scanner_kwargs": {"limit": max(30, max_candidates // 4)},
            "not_configured_message": "Pump.fun scanner not configured.",
        },
        {
            "source_name": "telegram_channels",
            "configured": bool(os.getenv("TELEGRAM_SCAN_API_URL", "").strip()),
            "scanner_fn": scan_telegram_channels,
            "scanner_kwargs": {"limit": max(30, max_candidates // 4)},
            "not_configured_message": "Telegram scanner not configured.",
        },
        {
            "source_name": "x_social",
            "configured": bool(os.getenv("X_BEARER_TOKEN", "").strip() and os.getenv("X_SEARCH_TERMS", "").strip()),
            "scanner_fn": scan_x_social,
            "scanner_kwargs": {"limit": max(30, max_candidates // 4)},
            "not_configured_message": "X scanner not configured.",
        },
    ]

    for job in source_jobs:
        candidates, status = _run_source(**job)
        all_candidates.extend(candidates)
        scanner_status.append(status)

    deduped_candidates = _dedupe_candidates(all_candidates)

    # Keep the highest-liquidity and most active names near the front before trimming.
    deduped_candidates = sorted(
        deduped_candidates,
        key=lambda item: (
            item.liquidity,
            item.volume_5m,
            item.buys_5m,
            -item.sells_5m,
        ),
        reverse=True,
    )
    if max_candidates > 0:
        deduped_candidates = deduped_candidates[:max_candidates]

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result = CollectionResult(
        candidates=deduped_candidates,
        scanner_status=scanner_status,
        elapsed_ms=elapsed_ms,
    )

    return {
        "candidates": [asdict(item) for item in result.candidates],
        "scanner_status": [asdict(item) for item in result.scanner_status],
        "elapsed_ms": result.elapsed_ms,
    }
