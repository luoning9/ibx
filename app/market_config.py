from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import load_app_config
from .runtime_paths import PROJECT_ROOT

DEFAULT_MARKET_CONFIG_PATH = PROJECT_ROOT / "conf" / "markets.json"


@dataclass(frozen=True)
class MarketProfile:
    market: str
    sec_type: str
    exchange: str
    currency: str
    allowed_trade_types: frozenset[str]


def resolve_market_config_path() -> Path:
    env_path = os.getenv("IBX_MARKET_CONFIG")
    if env_path:
        return Path(env_path)
    configured = load_app_config().runtime.market_config_path
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path
    return DEFAULT_MARKET_CONFIG_PATH


@lru_cache(maxsize=1)
def load_market_profiles() -> dict[str, MarketProfile]:
    path = resolve_market_config_path()
    if not path.exists():
        raise RuntimeError(f"market config not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    markets_raw = data.get("markets")
    if not isinstance(markets_raw, dict) or len(markets_raw) == 0:
        raise RuntimeError("market config must contain non-empty `markets` object")

    profiles: dict[str, MarketProfile] = {}
    for raw_name, raw_profile in markets_raw.items():
        market = str(raw_name).strip().upper()
        if not market:
            raise RuntimeError("market name cannot be empty")
        if not isinstance(raw_profile, dict):
            raise RuntimeError(f"market profile for {market} must be an object")

        sec_type = str(raw_profile.get("sec_type", "")).strip().upper()
        exchange = str(raw_profile.get("exchange", "")).strip().upper()
        currency = str(raw_profile.get("currency", "")).strip().upper()
        if not sec_type or not exchange or not currency:
            raise RuntimeError(f"market {market} must define sec_type/exchange/currency")

        raw_allowed = raw_profile.get("allowed_trade_types", [])
        if not isinstance(raw_allowed, list):
            raise RuntimeError(f"market {market} field allowed_trade_types must be a list")
        allowed_trade_types = frozenset(str(item).strip().lower() for item in raw_allowed if str(item).strip())

        profiles[market] = MarketProfile(
            market=market,
            sec_type=sec_type,
            exchange=exchange,
            currency=currency,
            allowed_trade_types=allowed_trade_types,
        )

    return profiles


def resolve_market_profile(market: str | None, trade_type: str | None) -> MarketProfile:
    profiles = load_market_profiles()
    normalized_trade_type = str(trade_type or "").strip().lower()

    if market is not None and str(market).strip():
        normalized_market = str(market).strip().upper()
        profile = profiles.get(normalized_market)
        if profile is None:
            supported = ", ".join(sorted(profiles))
            raise ValueError(f"unsupported market={normalized_market}, supported: {supported}")
        if normalized_trade_type and profile.allowed_trade_types and normalized_trade_type not in profile.allowed_trade_types:
            allowed = ", ".join(sorted(profile.allowed_trade_types))
            raise ValueError(
                f"market={normalized_market} does not allow trade_type={normalized_trade_type}; allowed: {allowed}"
            )
        return profile

    if not normalized_trade_type:
        raise ValueError("market is required")

    matched = [
        profile
        for profile in profiles.values()
        if not profile.allowed_trade_types or normalized_trade_type in profile.allowed_trade_types
    ]
    if len(matched) == 1:
        return matched[0]
    if len(matched) == 0:
        raise ValueError(f"cannot infer market from trade_type={normalized_trade_type}")
    raise ValueError(
        f"trade_type={normalized_trade_type} matches multiple markets, please provide market explicitly"
    )
