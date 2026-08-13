import json
import os
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import certifi
import requests


MEMORY_DIR = Path(__file__).resolve().parent / ".falcon_memory"
SURGE_STATE_FILE = MEMORY_DIR / "surge_state.json"
SURGE_SETTINGS_FILE = MEMORY_DIR / "surge_settings.json"
ENV_FILE = Path(__file__).resolve().parent / ".env"
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


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


def _safe_contract(token: Dict[str, object]) -> str:
    return str(token.get("contract_address", "") or "").strip()


def _format_money(value) -> str:
    num = _to_float(value)
    if num >= 1_000_000:
        return f"${num / 1_000_000:.2f}M"
    if num >= 1_000:
        return f"${num / 1_000:.1f}k"
    return f"${num:.0f}"


def _format_age_minutes(value) -> str:
    minutes = _to_float(value, -1)
    if minutes < 0:
        return "N/A"
    if minutes >= 60:
        return f"{minutes / 60:.1f}h"
    return f"{minutes:.0f}m"


@dataclass
class SurgeConfig:
    enabled: bool = True
    min_market_cap_usd: float = 100_000
    max_market_cap_usd: float = 2_000_000
    min_liquidity_usd: float = 20_000
    breakout_min_liquidity_usd: float = 25_000

    watch_min_mc_change_pct: float = 15.0
    watch_min_buy_pressure_ratio: float = 1.0
    surge_min_mc_change_pct: float = 25.0
    surge_min_liquidity_usd: float = 20_000
    surge_min_buy_pressure_ratio: float = 1.0
    breakout_min_mc_change_pct: float = 50.0

    surge_min_volume_accel: float = 1.35
    breakout_min_volume_accel: float = 1.8

    breakout_min_buy_pressure_ratio: float = 1.2

    breakout_focus_near_500k_usd: float = 450_000
    breakout_focus_500k_usd: float = 500_000
    breakout_focus_1m_usd: float = 1_000_000

    alerts_enabled: bool = False
    alert_on_surge: bool = True
    alert_on_breakout: bool = True
    alert_dry_run: bool = True
    alert_cooldown_minutes: int = 8
    alert_reset_minutes: int = 35


@dataclass
class SurgeDispatchReport:
    enabled: bool
    dry_run: bool
    evaluated: int = 0
    qualified: int = 0
    sent: int = 0
    suppressed_duplicate: int = 0
    suppressed_cooldown: int = 0
    errors: int = 0

    def to_dict(self) -> Dict[str, object]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "evaluated": self.evaluated,
            "qualified": self.qualified,
            "sent": self.sent,
            "suppressed_duplicate": self.suppressed_duplicate,
            "suppressed_cooldown": self.suppressed_cooldown,
            "errors": self.errors,
        }


def _level_rank(level: str) -> int:
    ranks = {"NONE": 0, "WATCH": 1, "SURGE": 2, "BREAKOUT": 3}
    return ranks.get(str(level or "NONE").upper(), 0)


def evaluate_surge(
    token: Dict[str, object],
    previous_token: Optional[Dict[str, object]],
    config: SurgeConfig,
) -> Dict[str, object]:
    market_cap = _to_float(token.get("market_cap_usd"))
    liquidity = _to_float(token.get("liquidity_usd"))
    volume_5m = _to_float(token.get("volume_5m_usd"))
    buys_5m = _to_int(token.get("buys_5m"))
    sells_5m = _to_int(token.get("sells_5m"))
    age_minutes = token.get("pair_age_minutes")
    source_confirmation_count = _to_int(token.get("source_confirmation_count"))

    prev_market_cap = _to_float((previous_token or {}).get("market_cap_usd"))
    prev_volume_5m = _to_float((previous_token or {}).get("volume_5m_usd"))

    market_cap_change_pct = 0.0
    if prev_market_cap > 0:
        market_cap_change_pct = ((market_cap - prev_market_cap) / prev_market_cap) * 100.0

    if prev_volume_5m > 0:
        volume_acceleration = volume_5m / prev_volume_5m
    else:
        volume_acceleration = 1.0 if volume_5m <= 0 else 1.2

    buy_pressure_ratio = float(buys_5m) if sells_5m <= 0 else float(buys_5m) / float(sells_5m)
    buy_pressure_positive = buys_5m > sells_5m

    in_candidate_range = (
        market_cap >= config.min_market_cap_usd
        and market_cap <= config.max_market_cap_usd
        and liquidity >= config.min_liquidity_usd
    )

    level = "NONE"
    reasons: List[str] = []

    if in_candidate_range:
        if (
            market_cap_change_pct >= config.watch_min_mc_change_pct
            and buy_pressure_positive
            and buy_pressure_ratio >= config.watch_min_buy_pressure_ratio
        ):
            level = "WATCH"
            reasons.append("market cap acceleration is above WATCH threshold")
            reasons.append("buy pressure is positive")

        if (
            market_cap_change_pct >= config.surge_min_mc_change_pct
            and volume_acceleration >= config.surge_min_volume_accel
            and buy_pressure_ratio >= config.surge_min_buy_pressure_ratio
            and buy_pressure_positive
            and liquidity >= config.surge_min_liquidity_usd
        ):
            level = "SURGE"
            reasons = [
                "market cap acceleration is above SURGE threshold",
                "volume is accelerating",
                "buys exceed sells",
                "liquidity floor is healthy",
            ]

        if (
            market_cap_change_pct >= config.breakout_min_mc_change_pct
            and volume_acceleration >= config.breakout_min_volume_accel
            and buy_pressure_ratio >= config.breakout_min_buy_pressure_ratio
            and buy_pressure_positive
            and liquidity >= config.breakout_min_liquidity_usd
        ):
            level = "BREAKOUT"
            reasons = [
                "market cap acceleration is above BREAKOUT threshold",
                "volume acceleration is strong",
                "buy pressure is strong",
                "liquidity supports continuation",
            ]

    milestone_bonus = 0
    if market_cap >= config.breakout_focus_1m_usd:
        milestone_bonus = 12
    elif market_cap >= config.breakout_focus_500k_usd:
        milestone_bonus = 8
    elif market_cap >= config.breakout_focus_near_500k_usd:
        milestone_bonus = 4

    mc_score = max(0.0, min(35.0, market_cap_change_pct * 0.7))
    vol_score = max(0.0, min(24.0, (volume_acceleration - 1.0) * 24.0))
    buy_score = max(0.0, min(18.0, (buy_pressure_ratio - 1.0) * 20.0))
    liq_score = 14.0 if liquidity >= config.breakout_min_liquidity_usd else (10.0 if liquidity >= config.min_liquidity_usd else 0.0)

    age_score = 0.0
    if age_minutes is not None:
        age_val = _to_float(age_minutes)
        if age_val <= 30:
            age_score = 7.0
        elif age_val <= 120:
            age_score = 5.0
        elif age_val <= 720:
            age_score = 3.0
        else:
            age_score = 1.0

    source_score = min(6.0, float(max(0, source_confirmation_count)) * 2.0)

    surge_rating = int(max(0, min(100, round(mc_score + vol_score + buy_score + liq_score + age_score + source_score + milestone_bonus))))

    return {
        "surge_level": level,
        "surge_reasons": reasons,
        "surge_candidate": in_candidate_range,
        "surge_market_cap_change_pct": round(market_cap_change_pct, 2),
        "surge_volume_acceleration": round(volume_acceleration, 3),
        "surge_buy_pressure_ratio": round(buy_pressure_ratio, 3),
        "surge_buy_pressure_positive": buy_pressure_positive,
        "surge_rating": surge_rating,
    }


class FalconSurgeEngine:
    def __init__(self, config: SurgeConfig, state_file: Optional[Path] = None):
        self.config = config
        self.state_file = state_file or SURGE_STATE_FILE

    def _ensure_memory_dir(self) -> None:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> Dict[str, object]:
        self._ensure_memory_dir()
        if not self.state_file.exists():
            return {"contracts": {}}

        try:
            with self.state_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"contracts": {}}

        contracts = payload.get("contracts", {}) if isinstance(payload, dict) else {}
        if not isinstance(contracts, dict):
            contracts = {}
        return {"contracts": contracts}

    def _save_state(self, state: Dict[str, object]) -> None:
        self._ensure_memory_dir()
        payload = {"contracts": state.get("contracts", {})}
        with self.state_file.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True)

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

    def _in_cooldown(self, entry: Dict[str, object], now_dt: datetime) -> bool:
        last_alert_dt = _parse_iso_datetime(entry.get("last_alert_at"))
        if not last_alert_dt:
            return False
        cooldown = timedelta(minutes=max(1, int(self.config.alert_cooldown_minutes)))
        return (now_dt - last_alert_dt) < cooldown

    def _prune_and_reset(self, state: Dict[str, object], now_dt: datetime) -> None:
        contracts = state.get("contracts", {})
        if not isinstance(contracts, dict):
            state["contracts"] = {}
            return

        reset_delta = timedelta(minutes=max(1, int(self.config.alert_reset_minutes)))
        drop_cutoff = now_dt - timedelta(hours=24)

        cleaned: Dict[str, object] = {}
        for contract, raw_entry in contracts.items():
            if not isinstance(raw_entry, dict):
                continue
            entry = dict(raw_entry)
            last_seen_dt = _parse_iso_datetime(entry.get("last_seen_at"))
            if last_seen_dt and last_seen_dt < drop_cutoff:
                continue

            last_alert_dt = _parse_iso_datetime(entry.get("last_alert_at"))
            level_seen = str(entry.get("last_level_seen", "NONE") or "NONE").upper()
            if last_alert_dt and level_seen in {"NONE", "WATCH"} and (now_dt - last_alert_dt) >= reset_delta:
                entry["last_level_alerted"] = "NONE"
            cleaned[contract] = entry

        state["contracts"] = cleaned

    def _compose_message(self, token: Dict[str, object], level: str) -> str:
        heading = "🚨 FALCON SURGE" if level == "SURGE" else "🔥 FALCON BREAKOUT"
        symbol = str(token.get("token_symbol", "UNKNOWN") or "UNKNOWN")
        market_cap = _format_money(token.get("market_cap_usd"))
        mc_change = _to_float(token.get("surge_market_cap_change_pct"))
        volume_5m = _format_money(token.get("volume_5m_usd"))
        volume_accel = _to_float(token.get("surge_volume_acceleration"), 1.0)
        buys = _to_int(token.get("buys_5m"))
        sells = _to_int(token.get("sells_5m"))
        liquidity = _format_money(token.get("liquidity_usd"))
        age = _format_age_minutes(token.get("pair_age_minutes"))
        rating = _to_int(token.get("surge_rating"))
        contract = str(token.get("contract_address", "N/A") or "N/A")

        source_names = token.get("source_confirmation_names", [])
        if isinstance(source_names, list) and source_names:
            source_text = ", ".join(str(item) for item in source_names)
        else:
            source_text = "none"

        return (
            f"{heading}\n"
            f"Token: {symbol}\n"
            f"Market Cap: {market_cap}\n"
            f"MC Change: {mc_change:+.2f}%\n"
            f"Volume: {volume_5m}\n"
            f"Volume Acceleration: {volume_accel:.2f}x\n"
            f"Buy/Sell: {buys} / {sells}\n"
            f"Liquidity: {liquidity}\n"
            f"Age: {age}\n"
            f"Sources: {source_text}\n"
            f"Falcon Surge Rating: {rating}/100\n"
            f"Contract: {contract}"
        )

    def process_scan(self, tokens: List[Dict[str, object]], scanned_at: str) -> SurgeDispatchReport:
        report = SurgeDispatchReport(
            enabled=self.config.alerts_enabled,
            dry_run=self.config.alert_dry_run,
        )

        now_dt = _parse_iso_datetime(scanned_at) or datetime.now(timezone.utc)
        state = self._load_state()
        self._prune_and_reset(state, now_dt)

        contracts = state.setdefault("contracts", {})
        if not isinstance(contracts, dict):
            contracts = {}
            state["contracts"] = contracts

        for token in tokens:
            contract = _safe_contract(token)
            if not contract:
                continue

            report.evaluated += 1
            level = str(token.get("surge_level", "NONE") or "NONE").upper()
            entry = contracts.get(contract, {})
            if not isinstance(entry, dict):
                entry = {}

            entry["last_seen_at"] = scanned_at
            entry["last_level_seen"] = level

            if level not in {"SURGE", "BREAKOUT"}:
                contracts[contract] = entry
                continue

            if level == "SURGE" and not self.config.alert_on_surge:
                contracts[contract] = entry
                continue

            if level == "BREAKOUT" and not self.config.alert_on_breakout:
                contracts[contract] = entry
                continue

            report.qualified += 1
            current_rank = _level_rank(level)
            alerted_level = str(entry.get("last_level_alerted", "NONE") or "NONE").upper()
            alerted_rank = _level_rank(alerted_level)

            if current_rank <= alerted_rank:
                report.suppressed_duplicate += 1
                contracts[contract] = entry
                continue

            progression = current_rank > alerted_rank and alerted_rank > 0
            if not progression and self._in_cooldown(entry, now_dt):
                report.suppressed_cooldown += 1
                contracts[contract] = entry
                continue

            if self.config.alerts_enabled:
                message = self._compose_message(token, level)
                sent = True
                if not self.config.alert_dry_run:
                    sent = self._send_telegram(message)

                if sent:
                    report.sent += 1
                    entry["last_alert_at"] = scanned_at
                    entry["last_level_alerted"] = level
                else:
                    report.errors += 1
            contracts[contract] = entry

        self._save_state(state)
        return report


def _default_surge_config() -> SurgeConfig:
    return SurgeConfig(
        enabled=_to_bool(_get_setting("FALCON_SURGE_ENABLED", "1"), True),
        min_market_cap_usd=_to_float(_get_setting("FALCON_SURGE_MIN_MC_USD", "100000"), 100000),
        max_market_cap_usd=_to_float(_get_setting("FALCON_SURGE_MAX_MC_USD", "2000000"), 2000000),
        min_liquidity_usd=_to_float(_get_setting("FALCON_SURGE_MIN_LIQ_USD", "20000"), 20000),
        breakout_min_liquidity_usd=_to_float(_get_setting("FALCON_SURGE_BREAKOUT_MIN_LIQ_USD", "25000"), 25000),
        watch_min_mc_change_pct=_to_float(_get_setting("FALCON_SURGE_WATCH_MC_CHANGE_PCT", "15"), 15),
        watch_min_buy_pressure_ratio=_to_float(_get_setting("FALCON_SURGE_WATCH_MIN_BUY_PRESSURE", "1.0"), 1.0),
        surge_min_mc_change_pct=_to_float(_get_setting("FALCON_SURGE_SURGE_MC_CHANGE_PCT", "25"), 25),
        surge_min_liquidity_usd=_to_float(_get_setting("FALCON_SURGE_SURGE_MIN_LIQ_USD", "20000"), 20000),
        surge_min_volume_accel=_to_float(_get_setting("FALCON_SURGE_MIN_VOLUME_ACCEL", "1.35"), 1.35),
        surge_min_buy_pressure_ratio=_to_float(_get_setting("FALCON_SURGE_SURGE_MIN_BUY_PRESSURE", "1.0"), 1.0),
        breakout_min_mc_change_pct=_to_float(_get_setting("FALCON_SURGE_BREAKOUT_MC_CHANGE_PCT", "50"), 50),
        breakout_min_volume_accel=_to_float(_get_setting("FALCON_SURGE_BREAKOUT_MIN_VOLUME_ACCEL", "1.8"), 1.8),
        breakout_min_buy_pressure_ratio=_to_float(_get_setting("FALCON_SURGE_BREAKOUT_MIN_BUY_PRESSURE", "1.2"), 1.2),
        breakout_focus_near_500k_usd=_to_float(_get_setting("FALCON_SURGE_FOCUS_NEAR_500K", "450000"), 450000),
        breakout_focus_500k_usd=_to_float(_get_setting("FALCON_SURGE_FOCUS_500K", "500000"), 500000),
        breakout_focus_1m_usd=_to_float(_get_setting("FALCON_SURGE_FOCUS_1M", "1000000"), 1000000),
        alerts_enabled=_to_bool(_get_setting("FALCON_SURGE_ALERTS_ENABLED", "1"), True),
        alert_on_surge=_to_bool(_get_setting("FALCON_SURGE_ALERT_ON_SURGE", "1"), True),
        alert_on_breakout=_to_bool(_get_setting("FALCON_SURGE_ALERT_ON_BREAKOUT", "1"), True),
        alert_dry_run=_to_bool(_get_setting("FALCON_SURGE_ALERT_DRY_RUN", _get_setting("FALCON_ALERT_DRY_RUN", "1")), True),
        alert_cooldown_minutes=_to_int(_get_setting("FALCON_SURGE_ALERT_COOLDOWN_MINUTES", "8"), 8),
        alert_reset_minutes=_to_int(_get_setting("FALCON_SURGE_ALERT_RESET_MINUTES", "35"), 35),
    )


def _settings_allowed_keys() -> List[str]:
    return [
        "enabled",
        "min_market_cap_usd",
        "max_market_cap_usd",
        "min_liquidity_usd",
        "watch_min_mc_change_pct",
        "watch_min_buy_pressure_ratio",
        "surge_min_mc_change_pct",
        "surge_min_volume_accel",
        "surge_min_buy_pressure_ratio",
        "surge_min_liquidity_usd",
        "breakout_min_mc_change_pct",
        "breakout_min_volume_accel",
        "breakout_min_buy_pressure_ratio",
        "breakout_min_liquidity_usd",
        "alerts_enabled",
        "alert_on_surge",
        "alert_on_breakout",
        "alert_cooldown_minutes",
        "alert_reset_minutes",
    ]


def _load_persisted_settings() -> Dict[str, object]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    if not SURGE_SETTINGS_FILE.exists():
        return {}
    try:
        with SURGE_SETTINGS_FILE.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    allowed = set(_settings_allowed_keys())
    return {
        key: value
        for key, value in payload.items()
        if key in allowed
    }


def _save_persisted_settings(settings: Dict[str, object]) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    allowed = set(_settings_allowed_keys())
    payload = {
        key: settings[key]
        for key in sorted(settings.keys())
        if key in allowed
    }
    with SURGE_SETTINGS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True)


def _cast_settings(values: Dict[str, object]) -> Dict[str, object]:
    bool_keys = {
        "enabled",
        "alerts_enabled",
        "alert_on_surge",
        "alert_on_breakout",
    }
    int_keys = {
        "alert_cooldown_minutes",
        "alert_reset_minutes",
    }

    casted: Dict[str, object] = {}
    for key in _settings_allowed_keys():
        if key not in values:
            continue
        raw = values[key]
        if key in bool_keys:
            casted[key] = bool(raw)
            continue
        if key in int_keys:
            casted[key] = _to_int(raw)
            continue
        casted[key] = _to_float(raw)
    return casted


def _config_to_settings(config: SurgeConfig) -> Dict[str, object]:
    serialized = asdict(config)
    return {
        key: serialized[key]
        for key in _settings_allowed_keys()
        if key in serialized
    }


def get_default_surge_settings() -> Dict[str, object]:
    return _config_to_settings(_default_surge_config())


def get_effective_surge_settings() -> Dict[str, object]:
    defaults = _config_to_settings(_default_surge_config())
    persisted = _cast_settings(_load_persisted_settings())
    defaults.update(persisted)
    return defaults


def apply_surge_settings(engine: FalconSurgeEngine, settings: Dict[str, object], persist: bool = True) -> Dict[str, object]:
    updated = _cast_settings(settings)
    current = _config_to_settings(engine.config)
    current.update(updated)
    engine.config = SurgeConfig(**current)
    if persist:
        _save_persisted_settings(_config_to_settings(engine.config))
    return _config_to_settings(engine.config)


def reset_surge_settings(engine: FalconSurgeEngine, persist: bool = True) -> Dict[str, object]:
    defaults = _config_to_settings(_default_surge_config())
    engine.config = SurgeConfig(**defaults)
    if persist:
        _save_persisted_settings(defaults)
    return defaults


def create_default_surge_engine() -> FalconSurgeEngine:
    settings = get_effective_surge_settings()
    config = SurgeConfig(**settings)
    return FalconSurgeEngine(config)
