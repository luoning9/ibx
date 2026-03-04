from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

_IB_MODULE: ModuleType | None = None

BACKEND = "ib_async"
INSTALL_HINT = "Missing dependency: ib_async. Install with: pip install ib_async"


def _load_backend_module() -> ModuleType | None:
    global _IB_MODULE
    if _IB_MODULE is not None:
        return _IB_MODULE
    try:
        _IB_MODULE = import_module(BACKEND)
    except ModuleNotFoundError:
        return None
    return _IB_MODULE


def require_ib_module() -> ModuleType:
    module = _load_backend_module()
    if module is None:
        raise ModuleNotFoundError(INSTALL_HINT)
    return module


def get_ib_backend_name() -> str | None:
    return BACKEND if _load_backend_module() is not None else None


def require_ib_attr(name: str) -> Any:
    module = _load_backend_module()
    if module is None:
        raise ModuleNotFoundError(INSTALL_HINT)
    return getattr(module, name)


def optional_ib_attr(name: str) -> Any | None:
    try:
        return require_ib_attr(name)
    except (AttributeError, ModuleNotFoundError):
        return None


def is_missing_ib_dependency_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if not text:
        return False
    needles = (
        "missing dependency: ib_async",
        "ib_async is not installed",
        "ib_async is required",
    )
    return any(item in text for item in needles)
