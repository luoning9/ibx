from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import (
    clear_app_config_cache,
    load_app_config,
    resolve_metric_allowed_rules,
    resolve_metric_allowed_windows,
    resolve_trigger_window_policy,
)
from app.db import resolve_db_path
from app.runtime_paths import resolve_data_dir


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content.strip() + "\n", encoding="utf-8")


def test_load_app_config_from_conf_file(tmp_path: Path) -> None:
    conf_path = tmp_path / "app.toml"
    _write_toml(
        conf_path,
        """
        [ib_gateway]
        host = "10.0.0.8"
        paper_port = 5002
        live_port = 5001
        client_id = 123
        timeout_seconds = 9
        trading_mode = "live"
        
        [ib_gateway.client_ids]
        broker_data = 223
        market_data = 224
        cli = 225

        [runtime]
        data_dir = "/tmp/ibx-data"
        enable_live_trading = true

        [worker]
        enabled = true
        monitor_interval_seconds = 45
        threads = 4
        queue_maxsize = 8000
        gateway_not_work_event_throttle_seconds = 600
        waiting_for_market_data_event_throttle_seconds = 180
        """,
    )

    old_config_path = os.getenv("IBX_APP_CONFIG")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    clear_app_config_cache()
    try:
        cfg = load_app_config()
        assert cfg.ib_gateway.host == "10.0.0.8"
        assert cfg.ib_gateway.paper_port == 5002
        assert cfg.ib_gateway.live_port == 5001
        assert cfg.ib_gateway.client_id == 123
        assert cfg.ib_gateway.client_ids.broker_data == 223
        assert cfg.ib_gateway.client_ids.market_data == 224
        assert cfg.ib_gateway.client_ids.cli == 225
        assert cfg.ib_gateway.timeout_seconds == 9
        assert cfg.ib_gateway.trading_mode == "live"
        assert cfg.runtime.data_dir == "/tmp/ibx-data"
        assert cfg.runtime.enable_live_trading is True
        assert cfg.worker.enabled is True
        assert cfg.worker.monitor_interval_seconds == 45
        assert cfg.worker.threads == 4
        assert cfg.worker.queue_maxsize == 8000
        assert cfg.worker.gateway_not_work_event_throttle_seconds == 600
        assert cfg.worker.waiting_for_market_data_event_throttle_seconds == 180
        assert cfg.providers.broker_data == "ib"
        assert cfg.providers.market_data == "ib"
    finally:
        if old_config_path is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_config_path
        clear_app_config_cache()


def test_runtime_data_dir_priority_env_over_conf(tmp_path: Path) -> None:
    conf_path = tmp_path / "app.toml"
    conf_data_dir = tmp_path / "from_conf"
    env_data_dir = tmp_path / "from_env"
    _write_toml(
        conf_path,
        f"""
        [runtime]
        data_dir = "{conf_data_dir}"
        """,
    )

    old_config_path = os.getenv("IBX_APP_CONFIG")
    old_data_dir = os.getenv("IBX_DATA_DIR")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    clear_app_config_cache()
    try:
        os.environ.pop("IBX_DATA_DIR", None)
        assert resolve_data_dir() == conf_data_dir

        os.environ["IBX_DATA_DIR"] = str(env_data_dir)
        assert resolve_data_dir() == env_data_dir
    finally:
        if old_config_path is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_config_path

        if old_data_dir is None:
            os.environ.pop("IBX_DATA_DIR", None)
        else:
            os.environ["IBX_DATA_DIR"] = old_data_dir
        clear_app_config_cache()


def test_resolve_db_path_from_conf(tmp_path: Path) -> None:
    conf_path = tmp_path / "app.toml"
    _write_toml(
        conf_path,
        """
        [runtime]
        data_dir = "/tmp/ibx-data"
        db_path = "/tmp/ibx-data/custom.sqlite3"
        """,
    )

    old_config_path = os.getenv("IBX_APP_CONFIG")
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    clear_app_config_cache()
    try:
        os.environ.pop("IBX_DB_PATH", None)
        assert resolve_db_path() == Path("/tmp/ibx-data/custom.sqlite3")

        os.environ["IBX_DB_PATH"] = str(tmp_path / "override.sqlite3")
        assert resolve_db_path() == tmp_path / "override.sqlite3"
    finally:
        if old_config_path is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_config_path

        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path
        clear_app_config_cache()


def test_trigger_mode_policy_loaded_from_json() -> None:
    clear_app_config_cache()
    instant_policy = resolve_trigger_window_policy("LEVEL_INSTANT", "1m")
    assert instant_policy.trigger_mode == "LEVEL_INSTANT"
    assert instant_policy.base_bar == "1m"
    assert instant_policy.confirm_consecutive == 1
    assert instant_policy.confirm_ratio == 1.0
    assert instant_policy.include_partial_bar is True
    assert instant_policy.missing_data_policy == "fail"

    confirm_policy = resolve_trigger_window_policy("CROSS_UP_CONFIRM", "5m")
    assert confirm_policy.trigger_mode == "CROSS_UP_CONFIRM"
    assert confirm_policy.base_bar == "1m"
    assert confirm_policy.confirm_consecutive == 4
    assert confirm_policy.confirm_ratio == 0.8
    assert confirm_policy.include_partial_bar is False
    assert confirm_policy.missing_data_policy == "fail"

    shared_policy = resolve_trigger_window_policy("CROSS_DOWN_INSTANT", "1m")
    assert shared_policy.base_bar == "1m"
    assert shared_policy.confirm_consecutive == 1
    assert shared_policy.confirm_ratio == 1.0


def test_metric_trigger_operator_rules_loaded_from_json() -> None:
    clear_app_config_cache()
    price_rules = resolve_metric_allowed_rules("PRICE")
    assert ("LEVEL_INSTANT", ">=") in price_rules
    assert ("CROSS_UP_INSTANT", ">=") in price_rules
    assert ("CROSS_UP_INSTANT", "<=") not in price_rules
    spread_rules = resolve_metric_allowed_rules("SPREAD")
    assert ("LEVEL_CONFIRM", ">=") in spread_rules
    assert ("CROSS_UP_CONFIRM", ">=") in spread_rules
    assert ("LEVEL_INSTANT", ">=") not in spread_rules
    assert ("CROSS_DOWN_INSTANT", "<=") not in spread_rules
    ratio_windows = resolve_metric_allowed_windows("VOLUME_RATIO")
    assert ratio_windows == {"1h", "2h", "4h", "1d", "2d"}
    assert "5d" not in ratio_windows


def test_confirm_window_rejects_below_5m() -> None:
    clear_app_config_cache()
    with pytest.raises(ValueError, match="does not allow evaluation_window"):
        resolve_trigger_window_policy("CROSS_UP_CONFIRM", "1m")


def test_provider_broker_data_can_be_configured(tmp_path: Path) -> None:
    conf_path = tmp_path / "app.toml"
    _write_toml(
        conf_path,
        """
        [providers]
        broker_data = "fixture"
        market_data = "fixture"
        """,
    )

    old_config_path = os.getenv("IBX_APP_CONFIG")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    clear_app_config_cache()
    try:
        cfg = load_app_config()
        assert cfg.providers.broker_data == "fixture"
        assert cfg.providers.market_data == "fixture"
    finally:
        if old_config_path is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_config_path
        clear_app_config_cache()


def test_client_ids_fallback_to_client_id_when_not_configured(tmp_path: Path) -> None:
    conf_path = tmp_path / "app.toml"
    _write_toml(
        conf_path,
        """
        [ib_gateway]
        client_id = 321
        """,
    )

    old_config_path = os.getenv("IBX_APP_CONFIG")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    clear_app_config_cache()
    try:
        cfg = load_app_config()
        assert cfg.ib_gateway.client_id == 321
        assert cfg.ib_gateway.client_ids.broker_data == 321
        assert cfg.ib_gateway.client_ids.market_data == 321
        assert cfg.ib_gateway.client_ids.cli == 321
    finally:
        if old_config_path is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_config_path
        clear_app_config_cache()
