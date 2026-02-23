from __future__ import annotations

import os
from pathlib import Path

from .config import load_app_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_LOG_PATH = DEFAULT_DATA_DIR / "logs" / "ibx.log"
DEFAULT_MARKET_DATA_LOG_PATH = DEFAULT_DATA_DIR / "logs" / "market_data.log"
DEFAULT_MARKET_CACHE_DB_PATH = DEFAULT_DATA_DIR / "market_cache.sqlite3"


def _resolve_optional_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def resolve_data_dir() -> Path:
    env_path = os.getenv("IBX_DATA_DIR")
    if env_path:
        return Path(env_path)
    configured = _resolve_optional_path(load_app_config().runtime.data_dir)
    if configured is not None:
        return configured
    return DEFAULT_DATA_DIR


def resolve_log_path() -> Path:
    env_path = os.getenv("IBX_LOG_PATH")
    if env_path:
        return Path(env_path)
    configured = _resolve_optional_path(load_app_config().runtime.log_path)
    if configured is not None:
        return configured
    return resolve_data_dir() / "logs" / "ibx.log"


def resolve_market_data_log_path() -> Path:
    env_path = os.getenv("IBX_MARKET_DATA_LOG_PATH")
    if env_path:
        return Path(env_path)
    configured = _resolve_optional_path(load_app_config().runtime.market_data_log_path)
    if configured is not None:
        return configured
    return resolve_data_dir() / "logs" / "market_data.log"


def resolve_market_cache_db_path() -> Path:
    env_path = os.getenv("IBX_MARKET_CACHE_DB_PATH")
    if env_path:
        return Path(env_path)
    configured = _resolve_optional_path(load_app_config().runtime.market_cache_db_path)
    if configured is not None:
        return configured
    return resolve_data_dir() / "market_cache.sqlite3"


def ensure_runtime_dirs() -> None:
    data_dir = resolve_data_dir()
    log_dir = resolve_log_path().parent
    market_log_dir = resolve_market_data_log_path().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    market_log_dir.mkdir(parents=True, exist_ok=True)
