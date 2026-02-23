from __future__ import annotations

from threading import Lock

from .ib_data_service import BrokerDataProvider, build_broker_data_provider_from_config


_BROKER_PROVIDER_LOCK = Lock()
_BROKER_PROVIDER: BrokerDataProvider | None = None


def get_shared_broker_data_provider() -> BrokerDataProvider:
    global _BROKER_PROVIDER
    with _BROKER_PROVIDER_LOCK:
        provider = _BROKER_PROVIDER
        if provider is None:
            provider = build_broker_data_provider_from_config()
            _BROKER_PROVIDER = provider
        return provider


def close_shared_broker_data_provider() -> None:
    global _BROKER_PROVIDER
    with _BROKER_PROVIDER_LOCK:
        provider = _BROKER_PROVIDER
        _BROKER_PROVIDER = None
    if provider is None:
        return
    disconnect = getattr(provider, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect()
        except Exception:
            pass


def reset_shared_broker_data_provider() -> None:
    close_shared_broker_data_provider()
