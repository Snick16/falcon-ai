import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests


@dataclass
class SocialContext:
    pair: dict
    previous_token: Optional[dict]
    boost_amount: float
    holder_count: Optional[int]
    scanned_at: datetime


@dataclass
class ProviderResult:
    provider: str
    score: float
    max_score: float
    reason: str


class SocialProvider:
    name = "base"

    def collect(self, context: SocialContext) -> ProviderResult:
        raise NotImplementedError()


class DexBoostsProvider(SocialProvider):
    name = "dex_boosts"

    def collect(self, context: SocialContext) -> ProviderResult:
        boost_amount = float(context.boost_amount or 0)
        score = 0.0
        if boost_amount >= 500:
            score = 25.0
        elif boost_amount >= 100:
            score = 18.0
        elif boost_amount >= 30:
            score = 12.0
        elif boost_amount > 0:
            score = 6.0

        reason = f"DexScreener boosts activity: {int(boost_amount)}"
        return ProviderResult(self.name, score, 25.0, reason)


class _OptionalMentionsApiProvider(SocialProvider):
    env_key = ""
    channel_name = ""
    max_score = 0.0

    def _extract_social_links(self, pair: dict) -> List[str]:
        links: List[str] = []
        info = pair.get("info", {})
        if isinstance(info, dict):
            websites = info.get("websites", [])
            if isinstance(websites, list):
                for item in websites:
                    if isinstance(item, dict):
                        url = str(item.get("url", "") or "")
                        if url:
                            links.append(url.lower())
            socials = info.get("socials", [])
            if isinstance(socials, list):
                for item in socials:
                    if isinstance(item, dict):
                        url = str(item.get("url", "") or "")
                        if url:
                            links.append(url.lower())
        return links

    def _live_fallback_score(self, context: SocialContext) -> float:
        pair = context.pair
        volume_5m = float(pair.get("volume", {}).get("m5", 0) or 0)
        txns_5m = pair.get("txns", {}).get("m5", {})
        buys = int(txns_5m.get("buys", 0) or 0)
        sells = int(txns_5m.get("sells", 0) or 0)
        links = self._extract_social_links(pair)

        linked = any(self.channel_name in link for link in links)
        score = 0.0
        if linked:
            score += self.max_score * 0.35
        if volume_5m >= 12_000:
            score += self.max_score * 0.30
        elif volume_5m >= 5_000:
            score += self.max_score * 0.20
        if buys > sells:
            score += self.max_score * 0.20
        if buys + sells >= 30:
            score += self.max_score * 0.15
        return min(score, self.max_score)

    def collect(self, context: SocialContext) -> ProviderResult:
        api_url = os.getenv(self.env_key, "").strip()
        pair = context.pair
        address = str(pair.get("baseToken", {}).get("address", "") or "")
        symbol = str(pair.get("baseToken", {}).get("symbol", "") or "")

        if api_url:
            try:
                response = requests.get(
                    api_url,
                    params={"address": address, "symbol": symbol},
                    timeout=4,
                )
                response.raise_for_status()
                payload = response.json()
                mentions_10m = int(payload.get("mentions_10m", 0) or 0)
                if mentions_10m >= 100:
                    score = self.max_score
                elif mentions_10m >= 40:
                    score = self.max_score * 0.75
                elif mentions_10m >= 15:
                    score = self.max_score * 0.45
                elif mentions_10m > 0:
                    score = self.max_score * 0.20
                else:
                    score = 0.0
                reason = f"{self.channel_name.title()} mentions (10m): {mentions_10m}"
                return ProviderResult(self.name, score, self.max_score, reason)
            except Exception:
                pass

        score = self._live_fallback_score(context)
        reason = f"{self.channel_name.title()} activity proxy from social links + trading flow"
        return ProviderResult(self.name, score, self.max_score, reason)


class XMentionsProvider(_OptionalMentionsApiProvider):
    name = "x_mentions"
    env_key = "FALCON_X_API_URL"
    channel_name = "twitter"
    max_score = 20.0


class TelegramMentionsProvider(_OptionalMentionsApiProvider):
    name = "telegram_mentions"
    env_key = "FALCON_TELEGRAM_API_URL"
    channel_name = "telegram"
    max_score = 15.0


class HolderGrowthProvider(SocialProvider):
    name = "holder_growth"

    def collect(self, context: SocialContext) -> ProviderResult:
        previous = context.previous_token or {}
        current_holders = context.holder_count
        previous_holders = previous.get("holder_count")
        score = 0.0

        try:
            previous_holders = int(previous_holders)
        except (TypeError, ValueError):
            previous_holders = None

        if current_holders is not None and previous_holders is not None:
            delta = current_holders - previous_holders
            if delta >= 40:
                score = 20.0
            elif delta >= 20:
                score = 15.0
            elif delta >= 8:
                score = 9.0
            elif delta > 0:
                score = 5.0
            reason = f"Holder growth delta: {delta}"
            return ProviderResult(self.name, score, 20.0, reason)

        txns_5m = context.pair.get("txns", {}).get("m5", {})
        buys = int(txns_5m.get("buys", 0) or 0)
        sells = int(txns_5m.get("sells", 0) or 0)
        if buys >= 25 and buys > sells:
            score = 8.0
            reason = "Holder growth proxy: sustained buy-side participation"
        else:
            reason = "Holder growth unavailable; weak proxy signal"
        return ProviderResult(self.name, score, 20.0, reason)


class TradingVelocityProvider(SocialProvider):
    name = "trading_velocity"

    def collect(self, context: SocialContext) -> ProviderResult:
        pair = context.pair
        previous = context.previous_token or {}
        volume_5m = float(pair.get("volume", {}).get("m5", 0) or 0)
        txns_5m = pair.get("txns", {}).get("m5", {})
        total_txns = int(txns_5m.get("buys", 0) or 0) + int(txns_5m.get("sells", 0) or 0)
        previous_volume = float(previous.get("volume_5m_usd", 0) or 0)

        score = 0.0
        if volume_5m >= 20_000:
            score += 10.0
        elif volume_5m >= 8_000:
            score += 7.0
        elif volume_5m >= 3_000:
            score += 4.0

        if total_txns >= 60:
            score += 6.0
        elif total_txns >= 25:
            score += 4.0
        elif total_txns >= 12:
            score += 2.0

        if previous_volume > 0 and volume_5m > previous_volume:
            growth_ratio = volume_5m / previous_volume
            if growth_ratio >= 1.8:
                score += 4.0
            elif growth_ratio >= 1.2:
                score += 2.0

        score = min(score, 20.0)
        reason = (
            f"Trading velocity from volume ${volume_5m:,.0f}, "
            f"txns {total_txns}, prev5m ${previous_volume:,.0f}"
        )
        return ProviderResult(self.name, score, 20.0, reason)


class SocialIntelligenceEngine:
    def __init__(self, providers: List[SocialProvider]):
        self.providers = providers

    @staticmethod
    def classify_heat(score: float) -> str:
        if score >= 85:
            return "VIRAL"
        if score >= 65:
            return "HOT"
        if score >= 40:
            return "WARM"
        return "QUIET"

    @staticmethod
    def heat_badge(label: str) -> str:
        mapping = {
            "VIRAL": "🔥 VIRAL",
            "HOT": "🔥 HOT",
            "WARM": "🟡 WARM",
            "QUIET": "⚪ QUIET",
        }
        return mapping.get(label, "⚪ QUIET")

    def evaluate(self, context: SocialContext) -> Dict[str, object]:
        provider_results = [provider.collect(context) for provider in self.providers]
        total_score = sum(result.score for result in provider_results)
        score = int(max(0, min(round(total_score), 100)))
        label = self.classify_heat(score)
        badge = self.heat_badge(label)

        reasons = [
            f"{result.provider}: {result.reason} (+{int(round(result.score))})"
            for result in provider_results
        ]
        provider_scores = {
            result.provider: int(round(result.score))
            for result in provider_results
        }

        return {
            "social_heat_score": score,
            "social_heat_label": label,
            "social_heat_badge": badge,
            "social_heat_reasons": reasons,
            "social_provider_scores": provider_scores,
        }


def create_default_social_engine() -> SocialIntelligenceEngine:
    return SocialIntelligenceEngine(
        providers=[
            DexBoostsProvider(),
            XMentionsProvider(),
            TelegramMentionsProvider(),
            HolderGrowthProvider(),
            TradingVelocityProvider(),
        ]
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
