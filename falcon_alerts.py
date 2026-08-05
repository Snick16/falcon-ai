import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import certifi
import requests


MEMORY_DIR = Path(__file__).resolve().parent / ".falcon_memory"
ALERT_STATE_FILE = MEMORY_DIR / "alert_state.json"
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
ENV_FILE = Path(__file__).resolve().parent / ".env"


def _load_env_file_values() -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    try:
        with ENV_FILE.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, raw_value = line.split("=", 1)
                values[key.strip()] = raw_value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return values


def _get_setting(name: str, default: str = "") -> str:
    env_value = os.getenv(name)
    if env_value is not None and str(env_value).strip() != "":
        return str(env_value).strip()
    file_values = _load_env_file_values()
    value = file_values.get(name)
    if value is not None and str(value).strip() != "":
        return str(value).strip()
    return default


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _to_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_money(value) -> str:
    num = _to_float(value)
    if num >= 1_000_000:
        return f"${num / 1_000_000:.2f}M"
    if num >= 1_000:
        return f"${num / 1_000:.1f}k"
    return f"${num:.0f}"


def _format_first_seen(value: Optional[str]) -> str:
    parsed = _parse_iso_datetime(value)
    if not parsed:
        return "N/A"
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_source_triggers(token: Dict[str, object]) -> Tuple[str, str]:
    breakdown = token.get("score_breakdown", {})
    if not isinstance(breakdown, dict):
        breakdown = {}

    ordered_sources = [
        ("Pump.fun", "pump"),
        ("Telegram", "telegram"),
        ("X", "x"),
        ("Dex", "dex"),
        ("Smart", "smart"),
        ("Social", "social_momentum"),
    ]

    scored = []
    triggered = []
    for label, key in ordered_sources:
        points = _to_int(breakdown.get(key, 0))
        scored.append(f"{label}:{points}")
        if points > 0:
            triggered.append(label)

    triggered_text = ", ".join(triggered) if triggered else "None"
    score_text = " | ".join(scored)
    return triggered_text, score_text


@dataclass
class AlertConfig:
    enabled: bool = False
    dry_run: bool = True
    one_time_per_contract: bool = True
    cooldown_minutes: int = 20
    min_score: int = 90
    min_confidence: int = 65
    min_liquidity_usd: float = 20_000
    min_price_change_5m_pct: float = 0.0
    min_buy_sell_ratio: float = 1.1
    min_buys_5m: int = 10
    max_risk_rank: int = 2  # LOW=1, MEDIUM=2, HIGH=3
    allowed_momentum: Tuple[str, ...] = ("BULLISH", "NEUTRAL")


@dataclass
class AlertDispatchReport:
    enabled: bool
    dry_run: bool
    evaluated: int = 0
    eligible: int = 0
    sent: int = 0
    suppressed_by_cooldown: int = 0
    suppressed_by_contract: int = 0
    errors: int = 0
    mode: str = "idle"

    def to_dict(self) -> Dict[str, object]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "evaluated": self.evaluated,
            "eligible": self.eligible,
            "sent": self.sent,
            "suppressed_by_cooldown": self.suppressed_by_cooldown,
            "suppressed_by_contract": self.suppressed_by_contract,
            "errors": self.errors,
            "mode": self.mode,
        }


class FalconAlertEngine:
    def __init__(self, config: AlertConfig):
        self.config = config

    def _risk_rank(self, risk_label: str) -> int:
        mapping = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
        return mapping.get(str(risk_label).upper(), 3)

    def _ensure_memory_dir(self) -> None:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> Dict[str, object]:
        self._ensure_memory_dir()
        if not ALERT_STATE_FILE.exists():
            return {"contracts": {}}
        try:
            with ALERT_STATE_FILE.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"contracts": {}}

        if not isinstance(loaded, dict):
            return {"contracts": {}}
        contracts = loaded.get("contracts", {})
        if not isinstance(contracts, dict):
            contracts = {}
        return {"contracts": contracts}

    def _save_state(self, state: Dict[str, object]) -> None:
        self._ensure_memory_dir()
        payload = {"contracts": state.get("contracts", {})}
        with ALERT_STATE_FILE.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True)

    def _is_cooldown_active(self, state: Dict[str, object], contract: str, now_dt: datetime) -> bool:
        contracts = state.get("contracts", {})
        last_iso = (contracts.get(contract) or {}).get("last_alert_at") if isinstance(contracts, dict) else None
        last_dt = _parse_iso_datetime(last_iso)
        if not last_dt:
            return False
        cooldown = timedelta(minutes=max(1, int(self.config.cooldown_minutes)))
        return (now_dt - last_dt) < cooldown

    def _mark_alert_sent(self, state: Dict[str, object], contract: str, scanned_at: str) -> None:
        contracts = state.setdefault("contracts", {})
        if not isinstance(contracts, dict):
            contracts = {}
            state["contracts"] = contracts
        previous = contracts.get(contract, {})
        first_alert_at = previous.get("first_alert_at") if isinstance(previous, dict) else None
        contracts[contract] = {
            "last_alert_at": scanned_at,
            "first_alert_at": first_alert_at or scanned_at,
            "alerted_once": True,
        }

    def _already_alerted(self, state: Dict[str, object], contract: str) -> bool:
        contracts = state.get("contracts", {})
        contract_state = contracts.get(contract) if isinstance(contracts, dict) else None
        if not isinstance(contract_state, dict):
            return False
        return bool(contract_state.get("alerted_once", False))

    def _buy_sell_ratio(self, buys_5m: int, sells_5m: int) -> float:
        return float(buys_5m) if sells_5m <= 0 else float(buys_5m) / float(sells_5m)

    def _evaluate_requirements(self, token: Dict[str, object]) -> Tuple[bool, List[str], List[str]]:
        score = _to_int(token.get("score"))

        passes = []
        failures = []

        checks = [
            (score >= self.config.min_score, f"Falcon Score {score} >= {self.config.min_score}", f"score {score} < {self.config.min_score}"),
        ]

        for ok, pass_reason, fail_reason in checks:
            if ok:
                passes.append(pass_reason)
            else:
                failures.append(fail_reason)

        return len(failures) == 0, passes, failures

    def _compose_alert_message(self, token: Dict[str, object], reasons: List[str]) -> str:
        name = str(token.get("token_name", "Unknown"))
        symbol = str(token.get("token_symbol", "UNKNOWN"))
        contract = str(token.get("contract_address", "N/A"))
        score = _to_int(token.get("score"))
        confidence = _to_int(token.get("confidence"))
        confidence_tier = str(token.get("confidence_tier", "LOW"))
        market_cap = _format_money(token.get("market_cap_usd"))
        liquidity = _format_money(token.get("liquidity_usd"))
        first_seen_display = _format_first_seen(token.get("first_seen_at"))
        first_seen_ago = str(token.get("first_seen_ago", "") or "N/A")
        triggered_sources, source_points = _format_source_triggers(token)

        base_reasons = token.get("signal_reasons", []) or []
        if isinstance(base_reasons, list):
            base_reasons = [str(item) for item in base_reasons[:3]]
        else:
            base_reasons = [str(base_reasons)]

        all_reasons = reasons[:4] + base_reasons
        unique_reasons = []
        for reason in all_reasons:
            if reason and reason not in unique_reasons:
                unique_reasons.append(reason)

        reason_lines = "\n".join([f"- {line}" for line in unique_reasons[:5]]) or "- criteria matched"
        chart_url = str(token.get("dexscreener_url", "") or "N/A")

        return (
            "FALCON BUY NOW ALERT\n\n"
            f"Token: {name} ({symbol})\n"
            f"Contract: {contract}\n"
            f"Falcon Rating: {score}/100\n"
            f"Confidence: {confidence_tier} ({confidence})\n"
            f"Market Cap: {market_cap} | Liquidity: {liquidity}\n"
            f"First Seen: {first_seen_display} ({first_seen_ago})\n"
            f"Triggered Sources: {triggered_sources}\n"
            f"Source Points: {source_points}\n\n"
            "Main Reasons:\n"
            f"{reason_lines}\n\n"
            f"DexScreener: {chart_url}"
        )

    def _send_telegram(self, message: str) -> bool:
        token = _get_setting("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = _get_setting("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            return False

        payload = {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }

        try:
            response = requests.post(
                TELEGRAM_SEND_URL.format(token=token),
                json=payload,
                timeout=12,
                verify=certifi.where(),
            )
            response.raise_for_status()
            return True
        except requests.RequestException:
            return False

    def process_scan(self, tokens: List[Dict[str, object]], scanned_at: str) -> AlertDispatchReport:
        report = AlertDispatchReport(
            enabled=self.config.enabled,
            dry_run=self.config.dry_run,
            mode="disabled" if not self.config.enabled else ("dry_run" if self.config.dry_run else "live"),
        )

        if not self.config.enabled:
            return report

        state = self._load_state()
        now_dt = _parse_iso_datetime(scanned_at) or datetime.now(timezone.utc)

        for token in tokens:
            report.evaluated += 1
            contract = str(token.get("contract_address", "")).strip()
            if not contract:
                continue

            should_alert, passes, _ = self._evaluate_requirements(token)
            if not should_alert:
                continue

            report.eligible += 1
            if self.config.one_time_per_contract and self._already_alerted(state, contract):
                report.suppressed_by_contract += 1
                continue

            if self._is_cooldown_active(state, contract, now_dt):
                report.suppressed_by_cooldown += 1
                continue

            message = self._compose_alert_message(token, passes)
            sent = True
            if not self.config.dry_run:
                sent = self._send_telegram(message)

            if sent:
                report.sent += 1
                self._mark_alert_sent(state, contract, scanned_at)
            else:
                report.errors += 1

        self._save_state(state)
        return report

    def send_test_alert(self) -> bool:
        sample_token = {
            "token_name": "Falcon Test Token",
            "token_symbol": "FTEST",
            "contract_address": "TEST_CONTRACT_DO_NOT_TRADE",
            "score": 96,
            "conviction_rating": "ELITE",
            "market_cap_usd": 185000,
            "liquidity_usd": 62000,
            "price_change_5m_pct": 7.4,
            "buys_5m": 52,
            "sells_5m": 18,
            "pair_age_minutes": 22,
            "dexscreener_url": "https://dexscreener.com/solana/test",
            "signal_reasons": ["test alert channel verification"],
            "confidence": 88,
            "momentum": "BULLISH",
            "risk_label": "MEDIUM",
        }
        message = self._compose_alert_message(sample_token, ["Manual TEST ALERT executed"])
        if self.config.dry_run:
            return True
        return self._send_telegram(message)


def create_default_alert_engine() -> FalconAlertEngine:
    allowed_momentum = tuple(
        item.strip().upper()
        for item in _get_setting("FALCON_ALERT_ALLOWED_MOMENTUM", "BULLISH,NEUTRAL").split(",")
        if item.strip()
    )
    if not allowed_momentum:
        allowed_momentum = ("BULLISH", "NEUTRAL")

    config = AlertConfig(
        enabled=_to_bool(_get_setting("FALCON_ALERTS_ENABLED", "0"), False),
        dry_run=_to_bool(_get_setting("FALCON_ALERT_DRY_RUN", "1"), True),
        one_time_per_contract=_to_bool(_get_setting("FALCON_ALERT_ONE_TIME_PER_CONTRACT", "1"), True),
        cooldown_minutes=_to_int(_get_setting("FALCON_ALERT_COOLDOWN_MINUTES", "20"), 20),
        min_score=_to_int(_get_setting("FALCON_ALERT_MIN_SCORE", "90"), 90),
        min_confidence=_to_int(_get_setting("FALCON_ALERT_MIN_CONFIDENCE", "65"), 65),
        min_liquidity_usd=_to_float(_get_setting("FALCON_ALERT_MIN_LIQUIDITY_USD", "20000"), 20000),
        min_price_change_5m_pct=_to_float(_get_setting("FALCON_ALERT_MIN_5M_CHANGE_PCT", "0"), 0),
        min_buy_sell_ratio=_to_float(_get_setting("FALCON_ALERT_MIN_BUY_SELL_RATIO", "1.1"), 1.1),
        min_buys_5m=_to_int(_get_setting("FALCON_ALERT_MIN_BUYS_5M", "10"), 10),
        max_risk_rank=_to_int(_get_setting("FALCON_ALERT_MAX_RISK_RANK", "2"), 2),
        allowed_momentum=allowed_momentum,
    )
    return FalconAlertEngine(config)
