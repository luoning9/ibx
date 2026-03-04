from __future__ import annotations

from typing import Any

from .broker_provider_registry import get_broker_data_provider
from .config import load_app_config
from .ib_data_service import FixtureBrokerDataProvider, IBDataService
from .market_data import DirectIBMarketDataProvider, FixtureMarketDataProvider, SQLiteMarketDataCache
from .ib_market_data import IBSessionHistoricalFetcher
from .models import (
    SystemGatewayStatusOut,
    SystemProviderStatusOut,
    SystemStatusOut,
    SystemWorkerStatusOut,
)
from .worker import worker_engine


def _non_empty_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _broker_provider_status() -> SystemProviderStatusOut:
    cfg = load_app_config()
    provider = get_broker_data_provider()

    details: dict[str, Any] = {}
    runtime_mode: str | None = None
    if isinstance(provider, IBDataService):
        runtime_mode = "ib"
        details = {
            "host": provider.host,
            "port": provider.port,
            "client_id": provider.client_id,
            "readonly": provider.readonly,
            "timeout_seconds": provider.timeout_seconds,
            "account_code": _non_empty_str(provider.default_account_code),
        }
    elif isinstance(provider, FixtureBrokerDataProvider):
        runtime_mode = "fixture"
        details = {
            "fixture_path": str(provider._fixture_path),  # noqa: SLF001
        }

    return SystemProviderStatusOut(
        configured=cfg.providers.broker_data,
        runtime_class=provider.__class__.__name__,
        runtime_mode=runtime_mode,
        details=details,
    )


def _market_data_provider_status() -> SystemProviderStatusOut:
    cfg = load_app_config()
    provider = getattr(worker_engine, "_market_data_provider", None)
    if provider is None:
        return SystemProviderStatusOut(
            configured=cfg.providers.market_data,
            runtime_class=None,
            runtime_mode=None,
            details={"state": "not_initialized"},
        )

    details: dict[str, Any] = {}
    runtime_mode: str | None = None
    if isinstance(provider, FixtureMarketDataProvider):
        runtime_mode = "fixture"
        details = {
            "fixture_path": str(provider._fixture_path),  # noqa: SLF001
        }
    elif isinstance(provider, DirectIBMarketDataProvider):
        runtime_mode = "ib_direct"
        details = {"cache_disabled": True}
        fetcher = getattr(provider, "_fetcher", None)
        if fetcher is not None:
            details["fetcher_class"] = fetcher.__class__.__name__
            if isinstance(fetcher, IBSessionHistoricalFetcher):
                details["fetcher"] = {
                    "host": fetcher.host,
                    "port": fetcher.port,
                    "client_id": fetcher.client_id,
                    "readonly": fetcher.readonly,
                    "timeout_seconds": fetcher.timeout_seconds,
                }
    elif isinstance(provider, SQLiteMarketDataCache):
        runtime_mode = "ib_cache"
        details = {
            "cache_db_path": str(provider._db_path),  # noqa: SLF001
            "cache_disabled": False,
        }
        fetcher = getattr(provider, "_fetcher", None)
        if fetcher is not None:
            details["fetcher_class"] = fetcher.__class__.__name__
            if isinstance(fetcher, IBSessionHistoricalFetcher):
                details["fetcher"] = {
                    "host": fetcher.host,
                    "port": fetcher.port,
                    "client_id": fetcher.client_id,
                    "readonly": fetcher.readonly,
                    "timeout_seconds": fetcher.timeout_seconds,
                }

    return SystemProviderStatusOut(
        configured=cfg.providers.market_data,
        runtime_class=provider.__class__.__name__,
        runtime_mode=runtime_mode,
        details=details,
    )


def get_system_status() -> SystemStatusOut:
    cfg = load_app_config()
    trading_mode = str(cfg.ib_gateway.trading_mode).strip().lower()
    if trading_mode not in {"paper", "live"}:
        trading_mode = "paper"

    gateway = SystemGatewayStatusOut(
        trading_mode=trading_mode,
        host=cfg.ib_gateway.host,
        api_port=int(cfg.ib_gateway.role_ports.broker_data),
        paper_port=cfg.ib_gateway.paper_port,
        live_port=cfg.ib_gateway.live_port,
        account_code=_non_empty_str(cfg.ib_gateway.account_code),
    )
    providers = {
        "broker_data": _broker_provider_status(),
        "market_data": _market_data_provider_status(),
    }
    worker_runtime = worker_engine.runtime_status()
    worker = SystemWorkerStatusOut(
        enabled=bool(worker_runtime["enabled"]),
        running=bool(worker_runtime["running"]),
        monitor_interval_seconds=int(worker_runtime["monitor_interval_seconds"]),
        configured_threads=int(worker_runtime["configured_threads"]),
        live_threads=int(worker_runtime["live_threads"]),
        scanner_alive=bool(worker_runtime["scanner_alive"]),
        queue_length=int(worker_runtime["queue_length"]),
        queue_maxsize=int(worker_runtime["queue_maxsize"]),
        inflight_tasks=int(worker_runtime["inflight_tasks"]),
    )
    return SystemStatusOut(gateway=gateway, worker=worker, providers=providers)
