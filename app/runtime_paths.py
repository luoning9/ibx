from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_LOG_PATH = DEFAULT_DATA_DIR / "logs" / "ibx.log"


def resolve_data_dir() -> Path:
    env_path = os.getenv("IBX_DATA_DIR")
    if env_path:
        return Path(env_path)
    return DEFAULT_DATA_DIR


def resolve_log_path() -> Path:
    env_path = os.getenv("IBX_LOG_PATH")
    if env_path:
        return Path(env_path)
    return resolve_data_dir() / "logs" / "ibx.log"


def ensure_runtime_dirs() -> None:
    data_dir = resolve_data_dir()
    log_dir = resolve_log_path().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
