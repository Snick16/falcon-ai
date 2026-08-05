import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

import requests


HELIUS_TX_ENDPOINT_TEMPLATE = "https://api.helius.xyz/v0/addresses/{wallet}/transactions"
SOLANA_ADDRESS_MIN_LEN = 32
SOLANA_ADDRESS_MAX_LEN = 44


def _normalize_address(value: str) -> str:
    return str(value or "").strip()


def _parse_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        parsed = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _parse_wallet_list(raw_value: str) -> List[str]:
    if not raw_value:
        return []

    wallets: List[str] = []
    seen: Set[str] = set()
    for part in str(raw_value).replace("\n", ",").split(","):
        wallet = _normalize_address(part)
        if not wallet:
            continue
        if len(wallet) < SOLANA_ADDRESS_MIN_LEN or len(wallet) > SOLANA_ADDRESS_MAX_LEN:
            continue
        key = wallet.lower()
        if key in seen:
            continue
        seen.add(key)
        wallets.append(wallet)
    return wallets


def _to_utc_datetime(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        epoch_value = float(value)
        if epoch_value > 10_000_000_000:
            epoch_value = epoch_value / 1000.0
        try:
            return datetime.fromtimestamp(epoch_value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _request_json(url: str, *, params: Optional[dict] = None, timeout: int = 12):
    response = requests.get(url, params=params, timeout=timeout, verify=False)
    response.raise_for_status()
    return response.json()


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _collect_buy_evidence_from_tx(wallet: str, tx: dict) -> List[dict]:
    if not isinstance(tx, dict):
        return []

    tx_type = str(tx.get("type", "") or "").upper()
    events = tx.get("events", {}) if isinstance(tx.get("events"), dict) else {}
    has_swap_event = bool(events.get("swap"))

    # Do not infer buys from ordinary transfers. Only count swap/buy-like transactions.
    if tx_type not in {"SWAP", "BUY"} and not has_swap_event:
        return []

    token_transfers = tx.get("tokenTransfers", [])
    if not isinstance(token_transfers, list):
        return []

    signature = str(tx.get("signature", "") or "")
    timestamp = tx.get("timestamp")
    when_dt = _to_utc_datetime(timestamp)
    when_iso = when_dt.isoformat() if when_dt else ""

    evidence = []
    wallet_key = wallet.lower()
    for transfer in token_transfers:
        if not isinstance(transfer, dict):
            continue

        mint = _normalize_address(transfer.get("mint"))
        if len(mint) < SOLANA_ADDRESS_MIN_LEN or len(mint) > SOLANA_ADDRESS_MAX_LEN:
            continue

        amount = _safe_float(transfer.get("tokenAmount"))
        if amount <= 0:
            continue

        to_owner = _normalize_address(transfer.get("toUserAccount") or transfer.get("toTokenAccount"))
        from_owner = _normalize_address(transfer.get("fromUserAccount") or transfer.get("fromTokenAccount"))

        if to_owner.lower() != wallet_key:
            continue
        if from_owner and from_owner.lower() == wallet_key:
            continue

        evidence_id = f"{wallet}:{signature}:{mint}"
        evidence.append(
            {
                "evidence_id": evidence_id,
                "wallet": wallet,
                "signature": signature,
                "token_address": mint,
                "amount": amount,
                "timestamp": when_iso,
                "tx_type": tx_type or "UNKNOWN",
            }
        )

    return evidence


def scan_whale_wallets(
    limit_wallets: int = 20,
    request_json_fn=None,
    now_dt: Optional[datetime] = None,
):
    """Scan configured whale wallets for recent token buys using Helius Enhanced Transactions API."""
    started = time.perf_counter()
    request_json = request_json_fn or _request_json

    api_key = os.getenv("HELIUS_API_KEY", "").strip()
    wallets_raw = os.getenv("WHALE_WALLETS", "").strip()
    endpoint_template = os.getenv("HELIUS_TX_ENDPOINT", HELIUS_TX_ENDPOINT_TEMPLATE).strip() or HELIUS_TX_ENDPOINT_TEMPLATE

    max_tx_per_wallet = _parse_int_env("WHALE_TX_LIMIT", default=40, minimum=5, maximum=100)
    lookback_minutes = _parse_int_env("WHALE_TX_LOOKBACK_MINUTES", default=60, minimum=5, maximum=24 * 60)

    wallets = _parse_wallet_list(wallets_raw)[: max(1, int(limit_wallets or 1))]
    details = {
        "configured": bool(api_key and wallets),
        "success": True,
        "provider": "helius",
        "wallets_configured": len(wallets),
        "wallets_scanned": 0,
        "transactions_checked": 0,
        "contracts_detected": 0,
        "confirmed_buys": 0,
        "lookback_minutes": lookback_minutes,
        "max_tx_per_wallet": max_tx_per_wallet,
        "elapsed_ms": 0,
        "error_message": "",
    }

    if not api_key or not wallets:
        details["configured"] = False
        details["success"] = True
        details["error_message"] = "Whale scanner not configured"
        details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return {}, details

    now = now_dt or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=lookback_minutes)
    signals_by_contract: Dict[str, dict] = {}
    seen_evidence_ids: Set[str] = set()

    for wallet in wallets:
        details["wallets_scanned"] += 1
        url = endpoint_template.format(wallet=wallet)

        try:
            payload = request_json(
                url,
                params={
                    "api-key": api_key,
                    "limit": max_tx_per_wallet,
                },
                timeout=12,
            )
        except Exception as error:  # noqa: BLE001 - scanner should fail soft.
            details["success"] = False
            details["error_message"] = f"Whale provider request failed: {type(error).__name__}"
            continue

        tx_rows = payload if isinstance(payload, list) else []
        for tx in tx_rows:
            details["transactions_checked"] += 1
            tx_dt = _to_utc_datetime((tx or {}).get("timestamp")) if isinstance(tx, dict) else None
            if tx_dt and tx_dt < cutoff:
                continue

            for evidence in _collect_buy_evidence_from_tx(wallet, tx):
                evidence_id = str(evidence.get("evidence_id", "") or "")
                if not evidence_id or evidence_id in seen_evidence_ids:
                    continue
                seen_evidence_ids.add(evidence_id)

                token_address = _normalize_address(evidence.get("token_address"))
                if not token_address:
                    continue

                signal = signals_by_contract.get(token_address)
                if not signal:
                    signal = {
                        "contract_address": token_address,
                        "wallets": [],
                        "buy_count": 0,
                        "last_buy_at": "",
                        "evidence": [],
                    }
                    signals_by_contract[token_address] = signal

                wallet_value = str(evidence.get("wallet", "") or "")
                if wallet_value and wallet_value not in signal["wallets"]:
                    signal["wallets"].append(wallet_value)

                signal["buy_count"] += 1
                signal["evidence"].append(evidence)

                last_buy_at = str(signal.get("last_buy_at", "") or "")
                current_ts = str(evidence.get("timestamp", "") or "")
                if current_ts and (not last_buy_at or current_ts > last_buy_at):
                    signal["last_buy_at"] = current_ts

    for signal in signals_by_contract.values():
        signal["wallets"] = sorted(signal.get("wallets", []))

    details["contracts_detected"] = len(signals_by_contract)
    details["confirmed_buys"] = sum(int(row.get("buy_count", 0) or 0) for row in signals_by_contract.values())
    details["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return signals_by_contract, details
