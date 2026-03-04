from __future__ import annotations

from .ib_data_service import BrokerDataProvider, build_broker_data_provider_from_config
from .ib_session_manager import close_ib_session_manager


def get_broker_data_provider() -> BrokerDataProvider:
    return build_broker_data_provider_from_config()


def close_broker_data_runtime() -> None:
    close_ib_session_manager()


def reset_broker_data_runtime() -> None:
    close_broker_data_runtime()
