from __future__ import annotations

import pytest

from app.evaluator import reset_gateway_probe_cache


@pytest.fixture(autouse=True)
def _default_gateway_ready(monkeypatch):
    # Keep unit tests deterministic and independent from local gateway runtime.
    monkeypatch.setenv("IBX_GATEWAY_READY", "1")
    reset_gateway_probe_cache()
    yield
    reset_gateway_probe_cache()
