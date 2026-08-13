import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests


DEX_BOOSTS_LATEST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_TOKEN_PROFILES_LATEST_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_TOKEN_BATCH_URL = "https://api.dexscreener.com/tokens/v1/solana/{}"
DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
PUMPFUN_RECENT_COINS_URL = "https://frontend-api-v3.pump.fun/coins"
X_RECENT_SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"
AXIOM_NEW_PAIRS_URL = "https://api.axiom.trade/new-pairs"
GMGN_SOL_SWAPS_RANK_URL = "https://gmgn.ai/defi/quotation/v1/rank/sol/swaps/24h"

AXIOM_SOURCE_TAG = "axiom"
GMGN_SOURCE_TAG = "gmgn"
PHOTON_SOURCE_TAG = "photon"
NATIVE_CONFIRMATION_SOURCES = (AXIOM_SOURCE_TAG, GMGN_SOURCE_TAG, PHOTON_SOURCE_TAG)

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
    details: dict = field(default_factory=dict)


@dataclass
class CollectionResult:
    candidates: List[TokenCandidate]
    scanner_status: List[ScannerSourceStatus]
    elapsed_ms: int


class SourceScanError(RuntimeError):
    """Error with safe source-scanner message and optional structured details."""

    def __init__(self, safe_message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(safe_message)
        self.safe_message = str(safe_message)
        self.details = details or {}


def _to_utc_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        epoch_value = float(value)
        if epoch_value > 10_000_000_000:
            epoch_value = epoch_value / 1000.0
        try:
            return datetime.fromtimestamp(epoch_value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            numeric = float(raw)
            if numeric > 10_000_000_000:
                numeric = numeric / 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_int_env(name: str, default: int, minimum: int = 1, maximum: int = 10_000) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _parse_telegram_channels(raw_value: str) -> List[str]:
    if not raw_value:
        return []

    parts = []
    for segment in str(raw_value).replace("\n", ",").split(","):
        channel = segment.strip()
        if not channel:
            continue
        # Accept @username, bare usernames, and t.me links.
        if channel.startswith("https://t.me/"):
            channel = channel.rstrip("/")
        elif channel.startswith("http://t.me/"):
            channel = channel.replace("http://", "https://", 1).rstrip("/")
        elif channel.startswith("@"):  # normalize to bare username.
            channel = channel[1:]
        parts.append(channel)

    unique = []
    seen = set()
    for channel in parts:
        key = channel.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(channel)
    return unique


def _parse_csv_list(raw_value: str) -> List[str]:
    if not raw_value:
        return []
    values = []
    seen = set()
    for part in str(raw_value).replace("\n", ",").split(","):
        item = part.strip()
        if item.startswith("@"):  # normalize usernames/accounts.
            item = item[1:]
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        values.append(item)
    return values


TELEGRAM_CA_LABEL_RE = re.compile(
    r"(?i)(?:\bca\b|contract|mint|address)\s*[:\-]\s*`?([1-9A-HJ-NP-Za-km-z]{32,44})`?"
)
TOKEN_SYMBOL_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")
TOKEN_NAME_WITH_SYMBOL_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9' ._\-/]{1,40})\s*\(\$?([A-Za-z][A-Za-z0-9]{1,14})\)"
)
TOKEN_NAME_LABEL_RE = re.compile(r"(?i)(?:token|name)\s*[:\-]\s*([A-Za-z][A-Za-z0-9' ._\-/]{1,40})")


def _extract_symbol_and_name_from_text(text: str) -> Tuple[str, str]:
    value = str(text or "")
    symbol = "UNKNOWN"
    name = "Unknown"

    symbol_match = TOKEN_SYMBOL_RE.search(value)
    if symbol_match:
        symbol = symbol_match.group(1).upper()

    pair_match = TOKEN_NAME_WITH_SYMBOL_RE.search(value)
    if pair_match:
        name = pair_match.group(1).strip()
        if symbol == "UNKNOWN":
            symbol = pair_match.group(2).upper()
        return symbol, name

    name_match = TOKEN_NAME_LABEL_RE.search(value)
    if name_match:
        name = name_match.group(1).strip()

    return symbol, name


def parse_telegram_messages(message_rows: Sequence[dict]) -> List[dict]:
    """Parse rows of Telegram message metadata into unique contract call hits."""
    hits_by_contract: Dict[str, dict] = {}

    for row in message_rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text", "") or "")
        if not text.strip():
            continue

        contract_candidates = []
        for match in TELEGRAM_CA_LABEL_RE.findall(text):
            contract_candidates.append(_normalize_address(match))
        for match in SOLANA_ADDRESS_RE.findall(text):
            contract_candidates.append(_normalize_address(match))

        deduped_candidates = []
        seen_candidates = set()
        for contract in contract_candidates:
            if not contract or contract in seen_candidates:
                continue
            seen_candidates.add(contract)
            deduped_candidates.append(contract)

        symbol, name = _extract_symbol_and_name_from_text(text)

        for contract in deduped_candidates:
            if not contract:
                continue
            payload = hits_by_contract.get(contract)
            if not payload:
                payload = {
                    "token_address": contract,
                    "symbol": symbol,
                    "name": name,
                    "channels": [],
                    "message_refs": [],
                    "raw_samples": [],
                }
                hits_by_contract[contract] = payload

            channel_name = str(row.get("channel", "") or "")
            if channel_name and channel_name not in payload["channels"]:
                payload["channels"].append(channel_name)

            message_ref = {
                "channel": channel_name,
                "message_id": row.get("message_id"),
                "message_timestamp": row.get("message_timestamp"),
                "message_url": row.get("message_url", ""),
            }
            payload["message_refs"].append(message_ref)

            raw_text = text.strip()
            if raw_text and raw_text not in payload["raw_samples"]:
                payload["raw_samples"].append(raw_text)

            if payload.get("symbol") in ("", "UNKNOWN") and symbol not in ("", "UNKNOWN"):
                payload["symbol"] = symbol
            if payload.get("name") in ("", "Unknown") and name not in ("", "Unknown"):
                payload["name"] = name

    return list(hits_by_contract.values())


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
        "source_confirmations": {
            AXIOM_SOURCE_TAG: source == AXIOM_SOURCE_TAG,
            GMGN_SOURCE_TAG: source == GMGN_SOURCE_TAG,
            PHOTON_SOURCE_TAG: source == PHOTON_SOURCE_TAG,
        },
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


def _extract_solana_addresses(value) -> List[str]:
    """Extract unique Solana-like addresses from nested payload values."""
    addresses: List[str] = []
    seen = set()

    def _walk(item):
        if isinstance(item, dict):
            for nested in item.values():
                _walk(nested)
            return

        if isinstance(item, list):
            for nested in item:
                _walk(nested)
            return

        if item is None:
            return

        text = str(item)
        for match in SOLANA_ADDRESS_RE.findall(text):
            normalized = _normalize_address(match)
            if normalized and normalized not in seen:
                seen.add(normalized)
                addresses.append(normalized)

    _walk(value)
    return addresses


def _iter_payload_items(payload) -> List[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    # Some sources return nested payloads (for example: {"data": {"rank": [...]}}).
    data_payload = payload.get("data")
    if isinstance(data_payload, dict):
        for nested_key in ("rank", "list", "items", "tokens", "results", "pairs"):
            nested_value = data_payload.get(nested_key)
            if isinstance(nested_value, list):
                return [item for item in nested_value if isinstance(item, dict)]

    for key in ("data", "items", "tokens", "results", "pairs"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [payload]


def _normalize_native_source_candidates(
    source_tag: str,
    api_url: str,
    payload,
    *,
    limit: int,
    discovered_at: str,
) -> Tuple[List[TokenCandidate], Dict[str, object]]:
    """Normalize arbitrary native-source payload rows into Falcon token candidates."""
    items = _iter_payload_items(payload)
    rows_by_address: Dict[str, dict] = {}

    for item in items:
        addresses = _extract_solana_addresses(item)
        for token_address in addresses:
            if token_address not in rows_by_address:
                rows_by_address[token_address] = item
            if len(rows_by_address) >= max(1, int(limit or 1)):
                break
        if len(rows_by_address) >= max(1, int(limit or 1)):
            break

    addresses = list(rows_by_address.keys())
    pairs_by_token = _fetch_pairs_for_token_addresses(addresses) if addresses else {}
    candidates: List[TokenCandidate] = []

    for token_address in addresses:
        row = rows_by_address.get(token_address, {})
        pair = _best_pair(pairs_by_token.get(token_address, []))
        item_url = str((row or {}).get("url", "") or "")
        resolved_source_url = item_url or api_url

        if pair:
            candidate = _build_candidate_from_pair(
                pair,
                source=source_tag,
                source_url=resolved_source_url,
                discovered_at=discovered_at,
                extra_raw={
                    "source_confirmations": {
                        AXIOM_SOURCE_TAG: source_tag == AXIOM_SOURCE_TAG,
                        GMGN_SOURCE_TAG: source_tag == GMGN_SOURCE_TAG,
                        PHOTON_SOURCE_TAG: source_tag == PHOTON_SOURCE_TAG,
                    },
                    f"{source_tag}_raw": row,
                },
            )
            if candidate:
                fallback_symbol = str((row or {}).get("symbol", "") or "").strip()
                fallback_name = str((row or {}).get("name", "") or "").strip()
                if fallback_symbol and candidate.symbol in ("", "UNKNOWN"):
                    candidate.symbol = fallback_symbol
                if fallback_name and candidate.name in ("", "Unknown"):
                    candidate.name = fallback_name
                candidates.append(candidate)
            continue

        symbol = str((row or {}).get("symbol", "UNKNOWN") or "UNKNOWN")
        name = str((row or {}).get("name", "Unknown") or "Unknown")
        candidates.append(
            TokenCandidate(
                chain=SOLANA_CHAIN_ID,
                token_address=token_address,
                symbol=symbol,
                name=name,
                source=source_tag,
                source_url=resolved_source_url,
                pair_address="",
                discovered_at=discovered_at,
                market_cap=0.0,
                liquidity=0.0,
                volume_5m=0.0,
                volume_1h=0.0,
                volume_24h=0.0,
                price_change_5m=0.0,
                price_change_1h=0.0,
                buys_5m=0,
                sells_5m=0,
                token_age_minutes=0.0,
                social_mentions=0,
                raw_data={
                    "pair": {},
                    "found_by": [source_tag],
                    "source_confirmations": {
                        AXIOM_SOURCE_TAG: source_tag == AXIOM_SOURCE_TAG,
                        GMGN_SOURCE_TAG: source_tag == GMGN_SOURCE_TAG,
                        PHOTON_SOURCE_TAG: source_tag == PHOTON_SOURCE_TAG,
                    },
                    f"{source_tag}_raw": row,
                },
            )
        )

    details = {
        "payload_items": len(items),
        "addresses_detected": len(addresses),
        "candidates_returned": len(candidates),
    }
    return candidates, details


def _scan_native_source(
    *,
    source_tag: str,
    limit: int,
    enabled_env: str,
    api_url_env: str,
    api_key_env: str,
    required_access_note: str,
) -> Tuple[List[TokenCandidate], Dict[str, object]]:
    started = time.perf_counter()
    enabled = _bool_env(enabled_env, False)
    api_url = os.getenv(api_url_env, "").strip()
    api_key = os.getenv(api_key_env, "").strip()

    details: Dict[str, object] = {
        "configured": True,
        "source_url": api_url,
        "required_access": required_access_note,
        "candidates_returned": 0,
        "elapsed_ms": 0,
        "error_message": "",
    }

    if not enabled:
        details["configured"] = False
        details["error_message"] = (
            f"{source_tag.upper()} collector unavailable: set {enabled_env}=true and provide {api_url_env}."
        )
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    missing = []
    if not api_url:
        missing.append(api_url_env)
    if not api_key:
        missing.append(api_key_env)
    if missing:
        details["configured"] = False
        details["error_message"] = (
            f"{source_tag.upper()} collector unavailable: missing " + ", ".join(missing)
        )
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        payload = _request_json(api_url, headers=headers, timeout=12, retries=1)
    except Exception:
        details["error_message"] = f"{source_tag.upper()} endpoint unavailable or rejected credentials."
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    candidates, parse_details = _normalize_native_source_candidates(
        source_tag=source_tag,
        api_url=api_url,
        payload=payload,
        limit=limit,
        discovered_at=_utc_now_iso(),
    )
    details.update(parse_details)
    details["candidates_returned"] = len(candidates)
    details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return candidates, details


def scan_axiom_source(limit: int = 40) -> Tuple[List[TokenCandidate], Dict[str, object]]:
    """Scan Axiom feed using an authenticated API contract."""
    started = time.perf_counter()
    enabled = _bool_env("AXIOM_ENABLED", False)
    api_url = os.getenv("AXIOM_API_URL", "").strip() or AXIOM_NEW_PAIRS_URL
    axiom_cookie = os.getenv("AXIOM_COOKIE", "").strip()
    user_agent = os.getenv(
        "AXIOM_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    ).strip()

    details: Dict[str, object] = {
        "configured": True,
        "source_url": api_url,
        "required_access": (
            "Axiom API requires authenticated browser-session cookies. Provide AXIOM_COOKIE with valid session "
            "cookies from an active Axiom login and matching AXIOM_USER_AGENT."
        ),
        "candidates_returned": 0,
        "elapsed_ms": 0,
        "error_message": "",
    }

    if not enabled:
        details["configured"] = False
        details["error_message"] = "AXIOM collector unavailable: set AXIOM_ENABLED=true."
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    if not api_url:
        details["configured"] = False
        details["error_message"] = "AXIOM collector unavailable: missing AXIOM_API_URL."
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    if not axiom_cookie:
        details["configured"] = False
        details["error_message"] = (
            "AXIOM collector unavailable: missing AXIOM_COOKIE. "
            "Set AXIOM_COOKIE to valid session cookies from a logged-in Axiom browser session."
        )
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://axiom.trade/",
        "Origin": "https://axiom.trade",
        "Cookie": axiom_cookie,
    }
    params = {
        "limit": max(1, int(limit or 1)),
    }

    try:
        payload = _request_json(api_url, params=params, headers=headers, timeout=12, retries=1)
    except Exception as error:  # noqa: BLE001 - source-level safe status reporting.
        message = str(error)
        if "Session invalid" in message or "No auth cookies" in message:
            details["error_message"] = (
                "AXIOM session invalid. Refresh AXIOM_COOKIE from a valid logged-in browser session "
                "and ensure AXIOM_USER_AGENT matches that session."
            )
        elif "403" in message or "Cloudflare" in message:
            details["error_message"] = (
                "AXIOM endpoint blocked by anti-bot challenge. Refresh AXIOM_COOKIE and AXIOM_USER_AGENT "
                "from a valid browser session."
            )
        elif "404" in message:
            details["error_message"] = (
                "AXIOM endpoint contract unavailable from this environment (404). Confirm AXIOM_API_URL and "
                "authenticated AXIOM_COOKIE access."
            )
        else:
            details["error_message"] = (
                "AXIOM endpoint unavailable or blocked. Provide valid AXIOM_COOKIE and AXIOM_USER_AGENT."
            )
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    candidates, parse_details = _normalize_native_source_candidates(
        source_tag=AXIOM_SOURCE_TAG,
        api_url=api_url,
        payload=payload,
        limit=limit,
        discovered_at=_utc_now_iso(),
    )
    details.update(parse_details)
    details["candidates_returned"] = len(candidates)
    details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return candidates, details


def scan_gmgn_source(limit: int = 40) -> Tuple[List[TokenCandidate], Dict[str, object]]:
    """Scan GMGN Solana rank feed using an explicit endpoint and access contract."""
    started = time.perf_counter()
    enabled = _bool_env("GMGN_ENABLED", False)
    api_url = os.getenv("GMGN_API_URL", "").strip() or GMGN_SOL_SWAPS_RANK_URL
    gmgn_cookie = os.getenv("GMGN_COOKIE", "").strip()
    user_agent = os.getenv(
        "GMGN_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    ).strip()

    details: Dict[str, object] = {
        "configured": True,
        "source_url": api_url,
        "required_access": (
            "GMGN Solana rank endpoint is Cloudflare-gated. Provide GMGN_COOKIE with a valid "
            "browser-session clearance value (must include cf_clearance=...) and matching GMGN_USER_AGENT."
        ),
        "candidates_returned": 0,
        "elapsed_ms": 0,
        "error_message": "",
    }

    if not enabled:
        details["configured"] = False
        details["error_message"] = "GMGN collector unavailable: set GMGN_ENABLED=true."
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    if not gmgn_cookie:
        details["configured"] = False
        details["error_message"] = (
            "GMGN collector unavailable: missing GMGN_COOKIE. "
            "Set GMGN_COOKIE to include cf_clearance=... from an authenticated GMGN browser session."
        )
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://gmgn.ai/",
        "Origin": "https://gmgn.ai",
        "Cookie": gmgn_cookie,
    }
    params = {
        "limit": max(1, int(limit or 1)),
        "orderby": os.getenv("GMGN_ORDERBY", "swaps").strip() or "swaps",
        "direction": os.getenv("GMGN_DIRECTION", "desc").strip() or "desc",
    }

    try:
        payload = _request_json(api_url, params=params, headers=headers, timeout=12, retries=1)
    except Exception as error:  # noqa: BLE001 - surface safe source-level status.
        message = str(error)
        if "403" in message or "Cloudflare" in message:
            details["error_message"] = (
                "GMGN endpoint blocked by Cloudflare challenge. "
                "Refresh GMGN_COOKIE (cf_clearance) and GMGN_USER_AGENT from a valid browser session."
            )
        elif "404" in message:
            details["error_message"] = (
                "GMGN endpoint contract unavailable from this environment (404). "
                "Confirm GMGN_API_URL access and provide a valid GMGN_COOKIE (cf_clearance) with GMGN_USER_AGENT."
            )
        else:
            details["error_message"] = (
                "GMGN endpoint unavailable or blocked. "
                "Provide valid GMGN_COOKIE (cf_clearance) and matching GMGN_USER_AGENT."
            )
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    candidates, parse_details = _normalize_native_source_candidates(
        source_tag=GMGN_SOURCE_TAG,
        api_url=api_url,
        payload=payload,
        limit=limit,
        discovered_at=_utc_now_iso(),
    )
    details.update(parse_details)
    details["candidates_returned"] = len(candidates)
    details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return candidates, details


def scan_photon_source(limit: int = 40) -> Tuple[List[TokenCandidate], Dict[str, object]]:
    """Scan Photon feed using an authenticated API contract."""
    started = time.perf_counter()
    enabled = _bool_env("PHOTON_ENABLED", False)
    api_url = os.getenv("PHOTON_API_URL", "").strip()
    photon_cookie = os.getenv("PHOTON_COOKIE", "").strip()
    user_agent = os.getenv(
        "PHOTON_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    ).strip()

    details: Dict[str, object] = {
        "configured": True,
        "source_url": api_url,
        "required_access": (
            "Photon token feed is not publicly documented and is Cloudflare/session-gated. Provide PHOTON_API_URL "
            "for your provisioned feed endpoint plus PHOTON_COOKIE with valid browser-session cookies "
            "(including cf_clearance if challenged) and matching PHOTON_USER_AGENT."
        ),
        "candidates_returned": 0,
        "elapsed_ms": 0,
        "error_message": "",
    }

    if not enabled:
        details["configured"] = False
        details["error_message"] = "PHOTON collector unavailable: set PHOTON_ENABLED=true."
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    missing = []
    if not api_url:
        missing.append("PHOTON_API_URL")
    if not photon_cookie:
        missing.append("PHOTON_COOKIE")
    if missing:
        details["configured"] = False
        details["error_message"] = (
            "PHOTON collector unavailable: missing " + ", ".join(missing) + "."
        )
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://photon-sol.tinyastro.io/",
        "Origin": "https://photon-sol.tinyastro.io",
        "Cookie": photon_cookie,
    }
    params = {
        "limit": max(1, int(limit or 1)),
    }

    try:
        payload = _request_json(api_url, params=params, headers=headers, timeout=12, retries=1)
    except Exception as error:  # noqa: BLE001 - source-level safe status reporting.
        message = str(error)
        if "403" in message or "Cloudflare" in message:
            details["error_message"] = (
                "PHOTON endpoint blocked by anti-bot challenge. Refresh PHOTON_COOKIE and PHOTON_USER_AGENT "
                "from a valid browser session."
            )
        elif "Session invalid" in message or "No auth cookies" in message:
            details["error_message"] = (
                "PHOTON session invalid. Refresh PHOTON_COOKIE from a valid logged-in browser session and ensure "
                "PHOTON_USER_AGENT matches that session."
            )
        elif "404" in message:
            details["error_message"] = (
                "PHOTON endpoint contract unavailable from this environment (404). Confirm PHOTON_API_URL and "
                "authenticated PHOTON_COOKIE access."
            )
        else:
            details["error_message"] = (
                "PHOTON endpoint unavailable or blocked. Provide valid PHOTON_API_URL, PHOTON_COOKIE, and "
                "PHOTON_USER_AGENT."
            )
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    candidates, parse_details = _normalize_native_source_candidates(
        source_tag=PHOTON_SOURCE_TAG,
        api_url=api_url,
        payload=payload,
        limit=limit,
        discovered_at=_utc_now_iso(),
    )
    details.update(parse_details)
    details["candidates_returned"] = len(candidates)
    details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return candidates, details


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


def parse_pumpfun_payload(payload, lookback_minutes: int = 60, max_tokens: int = 50) -> List[dict]:
    now_dt = datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(minutes=max(1, int(lookback_minutes or 60)))

    if isinstance(payload, dict):
        items = payload.get("coins") or payload.get("data") or payload.get("tokens") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    normalized_rows = []
    seen = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        mint = _normalize_address(item.get("mint") or item.get("tokenAddress") or item.get("address"))
        if not mint:
            continue
        if not SOLANA_ADDRESS_RE.fullmatch(mint):
            continue

        created_dt = _to_utc_datetime(
            item.get("created_timestamp")
            or item.get("createdAt")
            or item.get("created_at")
            or item.get("timestamp")
        )
        if created_dt and created_dt < cutoff:
            continue

        if mint in seen:
            continue
        seen.add(mint)

        symbol = str(item.get("symbol", "UNKNOWN") or "UNKNOWN")
        name = str(item.get("name", "Unknown") or "Unknown")
        normalized_rows.append(
            {
                "mint": mint,
                "symbol": symbol,
                "name": name,
                "created_at": created_dt.isoformat() if created_dt else "",
                "raw": item,
            }
        )

    normalized_rows.sort(key=lambda row: row.get("created_at", ""), reverse=True)
    return normalized_rows[:max(1, int(max_tokens or 50))]


def scan_pumpfun_tokens(limit: int = 50):
    started = time.perf_counter()
    enabled = _bool_env("PUMPFUN_ENABLED", False)
    max_tokens_cfg = _parse_int_env("PUMPFUN_MAX_TOKENS", default=50, minimum=1, maximum=300)
    lookback_minutes = _parse_int_env("PUMPFUN_LOOKBACK_MINUTES", default=60, minimum=1, maximum=24 * 60)
    requested_limit = max(1, int(limit or 1))
    effective_limit = min(requested_limit, max_tokens_cfg)

    details = {
        "configured": bool(enabled),
        "source_url": PUMPFUN_RECENT_COINS_URL,
        "max_tokens": max_tokens_cfg,
        "lookback_minutes": lookback_minutes,
        "mints_detected": 0,
        "candidates_returned": 0,
        "elapsed_ms": 0,
        "error_message": "",
    }

    if not enabled:
        details["configured"] = False
        details["error_message"] = "Pump.fun scanner not configured: set PUMPFUN_ENABLED=true."
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    try:
        payload = _request_json(
            PUMPFUN_RECENT_COINS_URL,
            params={
                "offset": 0,
                "limit": effective_limit,
                "sort": "created_timestamp",
                "order": "DESC",
                "includeNsfw": "false",
            },
            timeout=12,
            retries=1,
        )
    except Exception:
        details["error_message"] = "Pump.fun public endpoint unavailable."
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    parsed_rows = parse_pumpfun_payload(
        payload,
        lookback_minutes=lookback_minutes,
        max_tokens=effective_limit,
    )
    details["mints_detected"] = len(parsed_rows)

    if not parsed_rows:
        details["error_message"] = "No recent Pump.fun mints found in lookback window."
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    addresses = [row.get("mint", "") for row in parsed_rows if row.get("mint")]
    pairs_by_token = _fetch_pairs_for_token_addresses(addresses)
    rows_by_address = {
        row.get("mint", ""): row
        for row in parsed_rows
        if row.get("mint")
    }

    candidates: List[TokenCandidate] = []
    discovered_at = _utc_now_iso()
    for token_address in addresses:
        row = rows_by_address.get(token_address, {})
        pair = _best_pair(pairs_by_token.get(token_address, []))

        if pair:
            candidate = _build_candidate_from_pair(
                pair,
                source="pumpfun_tokens",
                source_url=str(pair.get("url", "") or PUMPFUN_RECENT_COINS_URL),
                discovered_at=discovered_at,
                extra_raw={
                    "pumpfun": row.get("raw", {}),
                    "pumpfun_created_at": row.get("created_at", ""),
                },
            )
            if candidate:
                if row.get("symbol") not in (None, "", "UNKNOWN"):
                    candidate.symbol = str(row.get("symbol"))
                if row.get("name") not in (None, "", "Unknown"):
                    candidate.name = str(row.get("name"))
                candidates.append(candidate)
            continue

        candidates.append(
            TokenCandidate(
                chain=SOLANA_CHAIN_ID,
                token_address=token_address,
                symbol=str(row.get("symbol", "UNKNOWN") or "UNKNOWN"),
                name=str(row.get("name", "Unknown") or "Unknown"),
                source="pumpfun_tokens",
                source_url=PUMPFUN_RECENT_COINS_URL,
                pair_address="",
                discovered_at=discovered_at,
                market_cap=0.0,
                liquidity=0.0,
                volume_5m=0.0,
                volume_1h=0.0,
                volume_24h=0.0,
                price_change_5m=0.0,
                price_change_1h=0.0,
                buys_5m=0,
                sells_5m=0,
                token_age_minutes=0.0,
                social_mentions=0,
                raw_data={
                    "pair": {},
                    "found_by": ["pumpfun_tokens"],
                    "pumpfun": row.get("raw", {}),
                    "pumpfun_created_at": row.get("created_at", ""),
                },
            )
        )

    details["candidates_returned"] = len(candidates)
    details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return candidates, details


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


def parse_x_posts(post_rows: Sequence[dict]) -> List[dict]:
    hits_by_contract: Dict[str, dict] = {}

    for row in post_rows:
        if not isinstance(row, dict):
            continue

        text = str(row.get("text", "") or "")
        urls = row.get("urls", []) if isinstance(row.get("urls"), list) else []
        scan_blob = "\n".join([text] + [str(url or "") for url in urls])
        contracts = _extract_solana_addresses_from_text(scan_blob)
        if not contracts:
            continue

        symbol, name = _extract_symbol_and_name_from_text(text)
        author_id = str(row.get("author_id", "") or "")
        author_username = str(row.get("author_username", "") or "")
        tweet_id = row.get("tweet_id")
        tweet_url = ""
        if author_username and tweet_id is not None:
            tweet_url = f"https://x.com/{author_username}/status/{tweet_id}"

        for contract in contracts:
            payload = hits_by_contract.get(contract)
            if not payload:
                payload = {
                    "token_address": contract,
                    "symbol": symbol,
                    "name": name,
                    "mention_count": 0,
                    "unique_author_count": 0,
                    "author_ids": set(),
                    "author_usernames": set(),
                    "posts": [],
                }
                hits_by_contract[contract] = payload

            payload["mention_count"] += 1
            if author_id:
                payload["author_ids"].add(author_id)
            if author_username:
                payload["author_usernames"].add(author_username)

            payload["posts"].append(
                {
                    "tweet_id": tweet_id,
                    "author_id": author_id,
                    "author_username": author_username,
                    "created_at": row.get("created_at", ""),
                    "tweet_url": tweet_url,
                    "urls": urls,
                    "text": text,
                }
            )

            if payload.get("symbol") in ("", "UNKNOWN") and symbol not in ("", "UNKNOWN"):
                payload["symbol"] = symbol
            if payload.get("name") in ("", "Unknown") and name not in ("", "Unknown"):
                payload["name"] = name

    normalized_hits = []
    for contract, payload in hits_by_contract.items():
        payload["token_address"] = contract
        payload["unique_author_count"] = len(payload.get("author_ids", set()))
        payload["author_ids"] = sorted(payload.get("author_ids", set()))
        payload["author_usernames"] = sorted(payload.get("author_usernames", set()))
        normalized_hits.append(payload)

    return normalized_hits


def scan_x_social(limit: int = 40):
    started = time.perf_counter()
    bearer = os.getenv("X_BEARER_TOKEN", "").strip()
    accounts = _parse_csv_list(os.getenv("X_ACCOUNTS", "").strip())
    api_url = os.getenv("X_API_URL", X_RECENT_SEARCH_URL).strip() or X_RECENT_SEARCH_URL

    details = {
        "configured": True,
        "accounts_requested": len(accounts),
        "posts_checked": 0,
        "contracts_detected": 0,
        "candidates_returned": 0,
        "elapsed_ms": 0,
        "error_message": "",
    }

    missing = []
    if not bearer:
        missing.append("X_BEARER_TOKEN")
    if not accounts:
        missing.append("X_ACCOUNTS")
    if missing:
        details["configured"] = False
        details["error_message"] = "X scanner not configured: missing " + ", ".join(missing)
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    headers = {"Authorization": f"Bearer {bearer}"}
    collected_rows: List[dict] = []

    for account in accounts:
        try:
            payload = _request_json(
                api_url,
                params={
                    "query": f"from:{account} -is:retweet",
                    "max_results": min(100, max(10, int(limit or 1))),
                    "tweet.fields": "author_id,created_at,entities",
                    "expansions": "author_id",
                    "user.fields": "username",
                },
                headers=headers,
                timeout=10,
                retries=1,
            )
        except Exception:
            continue

        tweets = (payload or {}).get("data", []) if isinstance(payload, dict) else []
        includes = (payload or {}).get("includes", {}) if isinstance(payload, dict) else {}
        users = includes.get("users", []) if isinstance(includes, dict) else []
        user_by_id = {
            str(user.get("id", "") or ""): str(user.get("username", "") or "")
            for user in users
            if isinstance(user, dict)
        }

        for tweet in tweets:
            if not isinstance(tweet, dict):
                continue
            details["posts_checked"] += 1
            entities = tweet.get("entities", {}) if isinstance(tweet.get("entities"), dict) else {}
            urls = []
            for item in entities.get("urls", []) if isinstance(entities.get("urls"), list) else []:
                if not isinstance(item, dict):
                    continue
                expanded = str(item.get("expanded_url", "") or item.get("url", "") or "")
                if expanded:
                    urls.append(expanded)

            author_id = str(tweet.get("author_id", "") or "")
            collected_rows.append(
                {
                    "tweet_id": tweet.get("id"),
                    "author_id": author_id,
                    "author_username": user_by_id.get(author_id, account),
                    "created_at": tweet.get("created_at", ""),
                    "text": str(tweet.get("text", "") or ""),
                    "urls": urls,
                }
            )

    parsed_hits = parse_x_posts(collected_rows)
    details["contracts_detected"] = len(parsed_hits)
    parsed_hits = parsed_hits[:max(1, int(limit or 1))]

    addresses = [str(hit.get("token_address", "") or "").strip() for hit in parsed_hits]
    addresses = [address for address in addresses if address]
    if not addresses:
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    hits_by_address = {
        str(hit.get("token_address", "") or "").strip(): hit
        for hit in parsed_hits
        if str(hit.get("token_address", "") or "").strip()
    }
    pairs_by_token = _fetch_pairs_for_token_addresses(addresses)

    candidates: List[TokenCandidate] = []
    discovered_at = _utc_now_iso()
    for token_address in addresses:
        hit = hits_by_address.get(token_address, {})
        pair = _best_pair(pairs_by_token.get(token_address, []))
        primary_post = (hit.get("posts") or [{}])[0] if isinstance(hit.get("posts"), list) else {}
        primary_url = str(primary_post.get("tweet_url", "") or "")
        mention_count = int(hit.get("mention_count", 0) or 0)

        if pair:
            candidate = _build_candidate_from_pair(
                pair,
                source="x_social",
                source_url=str(pair.get("url", "") or primary_url or api_url),
                discovered_at=discovered_at,
                social_mentions=mention_count,
                extra_raw={
                    "x_posts": list(hit.get("posts", [])),
                    "x_author_ids": list(hit.get("author_ids", [])),
                    "x_author_usernames": list(hit.get("author_usernames", [])),
                    "x_mention_count": mention_count,
                    "x_unique_author_count": int(hit.get("unique_author_count", 0) or 0),
                },
            )
            if candidate:
                if hit.get("symbol") not in (None, "", "UNKNOWN"):
                    candidate.symbol = str(hit.get("symbol"))
                if hit.get("name") not in (None, "", "Unknown"):
                    candidate.name = str(hit.get("name"))
                candidates.append(candidate)
            continue

        candidates.append(
            TokenCandidate(
                chain=SOLANA_CHAIN_ID,
                token_address=token_address,
                symbol=str(hit.get("symbol", "UNKNOWN") or "UNKNOWN"),
                name=str(hit.get("name", "Unknown") or "Unknown"),
                source="x_social",
                source_url=primary_url or api_url,
                pair_address="",
                discovered_at=discovered_at,
                market_cap=0.0,
                liquidity=0.0,
                volume_5m=0.0,
                volume_1h=0.0,
                volume_24h=0.0,
                price_change_5m=0.0,
                price_change_1h=0.0,
                buys_5m=0,
                sells_5m=0,
                token_age_minutes=0.0,
                social_mentions=mention_count,
                raw_data={
                    "pair": {},
                    "found_by": ["x_social"],
                    "x_posts": list(hit.get("posts", [])),
                    "x_author_ids": list(hit.get("author_ids", [])),
                    "x_author_usernames": list(hit.get("author_usernames", [])),
                    "x_mention_count": mention_count,
                    "x_unique_author_count": int(hit.get("unique_author_count", 0) or 0),
                },
            )
        )

    details["candidates_returned"] = len(candidates)
    details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return candidates, details


def scan_telegram_channels(limit: int = 40):
    started = time.perf_counter()
    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    session_value = os.getenv("TELEGRAM_SESSION", "").strip()
    channels_raw = os.getenv("TELEGRAM_CHANNELS", "").strip()
    lookback_minutes = _parse_int_env("TELEGRAM_LOOKBACK_MINUTES", default=30, minimum=1, maximum=24 * 60)
    max_messages = _parse_int_env("TELEGRAM_MAX_MESSAGES_PER_CHANNEL", default=50, minimum=1, maximum=300)

    channels = _parse_telegram_channels(channels_raw)
    details = {
        "configured": True,
        "connected": False,
        "channels_requested": len(channels),
        "channels_scanned": 0,
        "messages_checked": 0,
        "contracts_detected": 0,
        "candidates_returned": 0,
        "elapsed_ms": 0,
        "error_message": "",
    }

    missing = []
    if not api_id_raw:
        missing.append("TELEGRAM_API_ID")
    if not api_hash:
        missing.append("TELEGRAM_API_HASH")
    if not session_value:
        missing.append("TELEGRAM_SESSION")
    if not channels:
        missing.append("TELEGRAM_CHANNELS")

    if missing:
        details["configured"] = False
        details["error_message"] = "Telegram scanner not configured: missing " + ", ".join(missing)
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    try:
        api_id = int(api_id_raw)
    except (TypeError, ValueError):
        details["configured"] = False
        details["error_message"] = "Telegram scanner not configured: TELEGRAM_API_ID is invalid."
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    try:
        from telethon.errors import FloodWaitError, RPCError  # noqa: PLC0415
        from telethon.sessions import StringSession  # noqa: PLC0415
        from telethon.sync import TelegramClient  # noqa: PLC0415
    except Exception:
        details["error_message"] = "Telethon is not installed."
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    collected_rows: List[dict] = []
    cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)

    try:
        with TelegramClient(StringSession(session_value), api_id, api_hash) as client:
            if not client.is_user_authorized():
                details["error_message"] = "Telegram session is not authorized. Run telegram_setup.py locally."
                details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
                return [], details

            details["connected"] = True

            for channel_ref in channels:
                try:
                    entity = client.get_entity(channel_ref)
                    details["channels_scanned"] += 1
                except Exception:
                    # Private/missing/unavailable channels should not crash scan.
                    continue

                channel_name = str(getattr(entity, "title", "") or getattr(entity, "username", "") or channel_ref)
                channel_username = str(getattr(entity, "username", "") or "")

                try:
                    for message in client.iter_messages(entity, limit=max_messages):
                        raw_text = str(getattr(message, "message", "") or "")
                        if not raw_text.strip():
                            continue

                        message_dt = _to_utc_datetime(getattr(message, "date", None))
                        if message_dt and message_dt < cutoff_dt:
                            break

                        details["messages_checked"] += 1
                        message_id = getattr(message, "id", None)
                        message_url = ""
                        if channel_username and message_id is not None:
                            message_url = f"https://t.me/{channel_username}/{message_id}"

                        collected_rows.append(
                            {
                                "channel": channel_name,
                                "channel_ref": channel_ref,
                                "message_id": message_id,
                                "message_timestamp": message_dt.isoformat() if message_dt else "",
                                "message_url": message_url,
                                "text": raw_text,
                            }
                        )
                except (FloodWaitError, RPCError):
                    continue
                except Exception:
                    continue
    except Exception as error:
        details["error_message"] = f"Telegram connection failed: {type(error).__name__}"
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    parsed_hits = parse_telegram_messages(collected_rows)
    details["contracts_detected"] = len(parsed_hits)
    parsed_hits = parsed_hits[:max(1, limit)]

    addresses = [str(hit.get("token_address", "") or "").strip() for hit in parsed_hits]
    addresses = [address for address in addresses if address]
    if not addresses:
        details["candidates_returned"] = 0
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return [], details

    hits_by_address = {
        str(hit.get("token_address", "") or "").strip(): hit
        for hit in parsed_hits
        if str(hit.get("token_address", "") or "").strip()
    }

    pairs_by_token = _fetch_pairs_for_token_addresses(addresses)
    candidates: List[TokenCandidate] = []
    discovered_at = _utc_now_iso()
    for token_address in addresses:
        hit = hits_by_address.get(token_address, {})
        pair = _best_pair(pairs_by_token.get(token_address, []))

        if pair:
            candidate = _build_candidate_from_pair(
                pair,
                source="telegram_channels",
                source_url=str(pair.get("url", "") or (hit.get("message_refs", [{}])[0].get("message_url", ""))),
                discovered_at=discovered_at,
                social_mentions=max(1, len(hit.get("message_refs", []))),
                extra_raw={
                    "telegram_channels": list(hit.get("channels", [])),
                    "telegram_messages": list(hit.get("message_refs", [])),
                    "telegram_raw_samples": list(hit.get("raw_samples", [])),
                },
            )
            if candidate:
                if hit.get("symbol") not in (None, "", "UNKNOWN"):
                    candidate.symbol = str(hit.get("symbol"))
                if hit.get("name") not in (None, "", "Unknown"):
                    candidate.name = str(hit.get("name"))
                candidates.append(candidate)
            continue

        candidates.append(
            TokenCandidate(
                chain=SOLANA_CHAIN_ID,
                token_address=token_address,
                symbol=str(hit.get("symbol", "UNKNOWN") or "UNKNOWN"),
                name=str(hit.get("name", "Unknown") or "Unknown"),
                source="telegram_channels",
                source_url=str((hit.get("message_refs", [{}])[0].get("message_url", "") if hit.get("message_refs") else "")),
                pair_address="",
                discovered_at=discovered_at,
                market_cap=0.0,
                liquidity=0.0,
                volume_5m=0.0,
                volume_1h=0.0,
                volume_24h=0.0,
                price_change_5m=0.0,
                price_change_1h=0.0,
                buys_5m=0,
                sells_5m=0,
                token_age_minutes=0.0,
                social_mentions=max(1, len(hit.get("message_refs", []))),
                raw_data={
                    "pair": {},
                    "found_by": ["telegram_channels"],
                    "telegram_channels": list(hit.get("channels", [])),
                    "telegram_messages": list(hit.get("message_refs", [])),
                    "telegram_raw_samples": list(hit.get("raw_samples", [])),
                },
            )
        )

    details["candidates_returned"] = len(candidates)
    details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return candidates, details


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
            details={},
        )

    try:
        result = scanner_fn(**scanner_kwargs)
        details = {}
        candidates = result
        if isinstance(result, tuple) and len(result) == 2:
            candidates, details = result

        if not isinstance(candidates, list):
            candidates = []

        status_configured = configured
        status_success = True
        status_error = ""
        if isinstance(details, dict):
            if "configured" in details:
                status_configured = bool(details.get("configured"))
            if "success" in details:
                status_success = bool(details.get("success"))
            status_error = str(details.get("error_message", "") or "")

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return candidates, ScannerSourceStatus(
            source=source_name,
            configured=status_configured,
            success=status_success,
            candidates_found=len(candidates),
            elapsed_ms=elapsed_ms,
            error=status_error,
            details=details if isinstance(details, dict) else {},
        )
    except SourceScanError as error:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return [], ScannerSourceStatus(
            source=source_name,
            configured=True,
            success=False,
            candidates_found=0,
            elapsed_ms=elapsed_ms,
            error=error.safe_message,
            details=error.details,
        )
    except Exception as error:  # noqa: BLE001 - source-level isolation is required.
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return [], ScannerSourceStatus(
            source=source_name,
            configured=True,
            success=False,
            candidates_found=0,
            elapsed_ms=elapsed_ms,
            error=f"{source_name} failed: {type(error).__name__}",
            details={},
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

        existing_confirmations = existing.raw_data.get("source_confirmations", {})
        candidate_confirmations = candidate.raw_data.get("source_confirmations", {})
        merged_confirmations = {
            source: bool((existing_confirmations or {}).get(source, False))
            or bool((candidate_confirmations or {}).get(source, False))
            for source in NATIVE_CONFIRMATION_SOURCES
        }
        existing.raw_data["source_confirmations"] = merged_confirmations

        existing_messages = existing.raw_data.get("telegram_messages", [])
        candidate_messages = candidate.raw_data.get("telegram_messages", [])
        if isinstance(existing_messages, list) and isinstance(candidate_messages, list):
            merged_messages = []
            seen_messages = set()
            for row in existing_messages + candidate_messages:
                if not isinstance(row, dict):
                    continue
                key = (
                    str(row.get("channel", "") or ""),
                    str(row.get("message_id", "") or ""),
                    str(row.get("message_timestamp", "") or ""),
                )
                if key in seen_messages:
                    continue
                seen_messages.add(key)
                merged_messages.append(row)
            existing.raw_data["telegram_messages"] = merged_messages

        existing_channels = existing.raw_data.get("telegram_channels", [])
        candidate_channels = candidate.raw_data.get("telegram_channels", [])
        if isinstance(existing_channels, list) and isinstance(candidate_channels, list):
            existing.raw_data["telegram_channels"] = sorted(
                {
                    str(name)
                    for name in (existing_channels + candidate_channels)
                    if str(name).strip()
                }
            )

        existing.social_mentions = max(existing.social_mentions, candidate.social_mentions)

        # Keep richer market snapshot where available.
        if candidate.liquidity > existing.liquidity:
            merged_candidate = candidate
            merged_candidate.raw_data["found_by"] = merged_sources
            merged_candidate.raw_data["source_confirmations"] = merged_confirmations
            merged_candidate.raw_data["telegram_messages"] = existing.raw_data.get("telegram_messages", [])
            merged_candidate.raw_data["telegram_channels"] = existing.raw_data.get("telegram_channels", [])
            merged_candidate.social_mentions = max(merged_candidate.social_mentions, existing.social_mentions)
            merged[key] = merged_candidate

    return list(merged.values())


def collect_all_candidates(max_candidates: int = 200) -> Dict[str, object]:
    started = time.perf_counter()
    all_candidates: List[TokenCandidate] = []
    scanner_status: List[ScannerSourceStatus] = []

    source_jobs = [
        {
            "source_name": "axiom",
            "configured": True,
            "scanner_fn": scan_axiom_source,
            "scanner_kwargs": {"limit": max(20, max_candidates // 4)},
            "not_configured_message": "Axiom collector unavailable.",
        },
        {
            "source_name": "gmgn",
            "configured": True,
            "scanner_fn": scan_gmgn_source,
            "scanner_kwargs": {"limit": max(20, max_candidates // 4)},
            "not_configured_message": "GMGN collector unavailable.",
        },
        {
            "source_name": "photon",
            "configured": True,
            "scanner_fn": scan_photon_source,
            "scanner_kwargs": {"limit": max(20, max_candidates // 4)},
            "not_configured_message": "Photon collector unavailable.",
        },
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
            "configured": True,
            "scanner_fn": scan_pumpfun_tokens,
            "scanner_kwargs": {"limit": max(30, max_candidates // 4)},
            "not_configured_message": "Pump.fun scanner not configured.",
        },
        {
            "source_name": "telegram_channels",
            "configured": True,
            "scanner_fn": scan_telegram_channels,
            "scanner_kwargs": {"limit": max(30, max_candidates // 4)},
            "not_configured_message": "Telegram scanner not configured.",
        },
        {
            "source_name": "x_social",
            "configured": True,
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
