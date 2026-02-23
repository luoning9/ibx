from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .runtime_paths import ensure_runtime_dirs, resolve_log_path, resolve_market_data_log_path

_CONFIGURED = False
_MARKET_DATA_CONFIGURED = False


def _has_file_handler(logger: logging.Logger, path: Path) -> bool:
    target = str(path.resolve())
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            if Path(handler.baseFilename).resolve().as_posix() == Path(target).as_posix():
                return True
    return False


def configure_logging() -> Path:
    global _CONFIGURED
    if _CONFIGURED:
        return resolve_log_path()

    ensure_runtime_dirs()
    log_path = resolve_log_path().resolve()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        filename=log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger("")
    if not _has_file_handler(root_logger, log_path):
        root_logger.addHandler(file_handler)
    if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)

    # Uvicorn loggers may install their own stream handlers; keep propagation enabled
    # so records also land in project data/logs.
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.propagate = True
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)

    root_logger.info("File logging initialized at %s", log_path)
    _CONFIGURED = True
    return log_path


def configure_market_data_logging() -> Path:
    global _MARKET_DATA_CONFIGURED
    if _MARKET_DATA_CONFIGURED:
        return resolve_market_data_log_path()

    ensure_runtime_dirs()
    log_path = resolve_market_data_log_path().resolve()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        filename=log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    logger = logging.getLogger("ibx.market_data")
    if not _has_file_handler(logger, log_path):
        logger.addHandler(file_handler)
    logger.propagate = False
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)

    logger.info("Market data file logging initialized at %s", log_path)
    _MARKET_DATA_CONFIGURED = True
    return log_path
