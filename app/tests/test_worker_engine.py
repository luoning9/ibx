from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.config import clear_app_config_cache
from app.db import get_connection, init_db
from app.worker import (
    StrategyExecutionEngine,
    StrategyTask,
    StrategyTaskQueue,
    build_execution_engine_from_env,
)


UTC = timezone.utc


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content.strip() + "\n", encoding="utf-8")


def _insert_strategy(
    strategy_id: str,
    *,
    db_path: Path,
    status: str,
    conditions_json: str = "[]",
    trade_action_json: str | None = None,
) -> None:
    now_iso = _iso_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategies (
                id, description, market, sec_type, exchange, trade_type, currency,
                upstream_only_activation, expire_mode, expire_in_seconds, expire_at,
                status, condition_logic, conditions_json, trade_action_json,
                created_at, updated_at, activated_at, logical_activated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                f"test {strategy_id}",
                "US_STOCK",
                "STK",
                "SMART",
                "buy",
                "USD",
                0,
                "relative",
                86400,
                None,
                status,
                "AND",
                conditions_json,
                trade_action_json,
                now_iso,
                now_iso,
                now_iso,
                now_iso,
            ),
        )
        conn.commit()


def test_strategy_task_queue_deduplicates_inflight() -> None:
    task_queue = StrategyTaskQueue(maxsize=8)
    task = StrategyTask(strategy_id="S-TQ-1", reason="unit_test", enqueued_at=datetime.now(UTC))

    assert task_queue.enqueue(task) is True
    assert task_queue.enqueue(task) is False

    popped = task_queue.pop(timeout=0.01)
    assert popped is not None
    assert popped.strategy_id == "S-TQ-1"
    task_queue.mark_done(popped.strategy_id)

    assert task_queue.enqueue(task) is True


def test_scan_once_excludes_verify_failed(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_scan.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy("S-WORKER-ACTIVE-SCAN", db_path=db_path, status="ACTIVE")
    _insert_strategy("S-WORKER-VERIFY-FAILED-SCAN", db_path=db_path, status="VERIFY_FAILED")

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        enqueued = engine.scan_once()
        assert enqueued == 1
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_skips_when_already_inflight(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_process_once_inflight.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-INFLIGHT",
        db_path=db_path,
        status="ACTIVE",
        conditions_json=json.dumps(
            [
                {
                    "condition_id": "c1",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "PRICE",
                    "trigger_mode": "LEVEL_INSTANT",
                    "evaluation_window": "1m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 100.0,
                    "product": "AAPL",
                    "contract_id": 1,
                }
            ]
        ),
    )

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        assert engine._queue.claim("S-WORKER-INFLIGHT") is True
        engine.process_once("S-WORKER-INFLIGHT", reason="unit_test")
        with get_connection() as conn:
            run_row = conn.execute(
                "SELECT COUNT(1) AS c FROM strategy_runs WHERE strategy_id = ?",
                ("S-WORKER-INFLIGHT",),
            ).fetchone()
            assert run_row is not None
            assert run_row["c"] == 0
    finally:
        engine._queue.release("S-WORKER-INFLIGHT")
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_build_execution_engine_ignores_worker_env_overrides(tmp_path) -> None:
    conf_path = tmp_path / "app.toml"
    _write_toml(
        conf_path,
        """
        [worker]
        enabled = true
        monitor_interval_seconds = 41
        threads = 3
        queue_maxsize = 777
        gateway_not_work_event_throttle_seconds = 601
        waiting_for_market_data_event_throttle_seconds = 181
        """,
    )

    old_config = os.getenv("IBX_APP_CONFIG")
    old_enabled = os.getenv("IBX_WORKER_ENABLED")
    old_interval = os.getenv("MONITOR_INTERVAL_SECONDS")
    old_threads = os.getenv("IBX_WORKER_THREADS")
    old_qsize = os.getenv("IBX_WORKER_QUEUE_MAXSIZE")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    os.environ["IBX_WORKER_ENABLED"] = "0"
    os.environ["MONITOR_INTERVAL_SECONDS"] = "300"
    os.environ["IBX_WORKER_THREADS"] = "9"
    os.environ["IBX_WORKER_QUEUE_MAXSIZE"] = "99999"
    clear_app_config_cache()
    try:
        engine = build_execution_engine_from_env()
        assert engine.enabled is True
        assert engine._monitor_interval_seconds == 41
        assert engine._worker_count == 3
        assert engine._queue._queue.maxsize == 777
        assert engine._gateway_not_work_event_throttle_seconds == 601
        assert engine._waiting_for_market_data_event_throttle_seconds == 181
    finally:
        if old_config is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_config
        if old_enabled is None:
            os.environ.pop("IBX_WORKER_ENABLED", None)
        else:
            os.environ["IBX_WORKER_ENABLED"] = old_enabled
        if old_interval is None:
            os.environ.pop("MONITOR_INTERVAL_SECONDS", None)
        else:
            os.environ["MONITOR_INTERVAL_SECONDS"] = old_interval
        if old_threads is None:
            os.environ.pop("IBX_WORKER_THREADS", None)
        else:
            os.environ["IBX_WORKER_THREADS"] = old_threads
        if old_qsize is None:
            os.environ.pop("IBX_WORKER_QUEUE_MAXSIZE", None)
        else:
            os.environ["IBX_WORKER_QUEUE_MAXSIZE"] = old_qsize
        clear_app_config_cache()


def test_process_once_active_persists_strategy_run(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-ACTIVE",
        db_path=db_path,
        status="ACTIVE",
        conditions_json=json.dumps(
            [
                {
                    "condition_id": "c1",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "PRICE",
                    "trigger_mode": "LEVEL_INSTANT",
                    "evaluation_window": "1m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 100.0,
                    "product": "AAPL",
                    "contract_id": 1,
                }
            ]
        ),
    )

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-ACTIVE", reason="unit_test")
        with get_connection() as conn:
            run_row = conn.execute(
                "SELECT condition_met, decision_reason FROM strategy_runs WHERE strategy_id = ?",
                ("S-WORKER-ACTIVE",),
            ).fetchone()
            assert run_row is not None
            assert run_row["condition_met"] == 0
            assert run_row["decision_reason"] == "waiting_for_market_data"

            state_row = conn.execute(
                """
                SELECT state, last_evaluated_at
                FROM condition_states
                WHERE strategy_id = ? AND condition_id = ?
                """,
                ("S-WORKER-ACTIVE", "c1"),
            ).fetchone()
            assert state_row is not None
            assert state_row["state"] == "WAITING"
            assert state_row["last_evaluated_at"] is not None
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_triggered_creates_trade_instruction(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_triggered.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-TRIG",
        db_path=db_path,
        status="TRIGGERED",
        trade_action_json=json.dumps(
            {
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 1,
            }
        ),
    )

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-TRIG", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-TRIG",),
            ).fetchone()
            assert strategy_row is not None
            assert strategy_row["status"] == "ORDER_SUBMITTED"

            instruction_row = conn.execute(
                "SELECT status FROM trade_instructions WHERE strategy_id = ?",
                ("S-WORKER-TRIG",),
            ).fetchone()
            assert instruction_row is not None
            assert instruction_row["status"] == "ORDER_SUBMITTED"
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_verifying_moves_to_active(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_verifying.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-VERIFY",
        db_path=db_path,
        status="VERIFYING",
    )

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-VERIFY", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status, activated_at, logical_activated_at FROM strategies WHERE id = ?",
                ("S-WORKER-VERIFY",),
            ).fetchone()
            assert strategy_row is not None
            assert strategy_row["status"] == "ACTIVE"
            assert strategy_row["activated_at"] is not None
            assert strategy_row["logical_activated_at"] is not None

            event_row = conn.execute(
                """
                SELECT event_type
                FROM strategy_events
                WHERE strategy_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                ("S-WORKER-VERIFY",),
            ).fetchone()
            assert event_row is not None
            assert event_row["event_type"] == "ACTIVATED"
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_active_without_conditions_moves_to_verify_failed(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_active_no_conditions.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-NO-COND",
        db_path=db_path,
        status="ACTIVE",
        conditions_json="[]",
    )

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-NO-COND", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-NO-COND",),
            ).fetchone()
            assert strategy_row is not None
            assert strategy_row["status"] == "VERIFY_FAILED"

            event_row = conn.execute(
                """
                SELECT event_type
                FROM strategy_events
                WHERE strategy_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                ("S-WORKER-NO-COND",),
            ).fetchone()
            assert event_row is not None
            assert event_row["event_type"] == "VERIFY_FAILED"
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_active_invalid_condition_moves_to_verify_failed(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_active_invalid_condition.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-INVALID-COND",
        db_path=db_path,
        status="ACTIVE",
        conditions_json=json.dumps(
            [
                {
                    "condition_id": "c1",
                    "operator": ">=",
                    "value": 100.0,
                    "product": "AAPL",
                    "contract_id": 1,
                }
            ]
        ),
    )

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-INVALID-COND", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-INVALID-COND",),
            ).fetchone()
            assert strategy_row is not None
            assert strategy_row["status"] == "VERIFY_FAILED"

            event_row = conn.execute(
                """
                SELECT event_type, detail
                FROM strategy_events
                WHERE strategy_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                ("S-WORKER-INVALID-COND",),
            ).fetchone()
            assert event_row is not None
            assert event_row["event_type"] == "VERIFY_FAILED"
            assert "condition_config_invalid" in event_row["detail"]
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_gateway_not_work_event_is_throttled_by_runtime_state(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_gateway_throttle.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-GW-THROTTLE",
        db_path=db_path,
        status="ACTIVE",
        conditions_json=json.dumps(
            [
                {
                    "condition_id": "c1",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "PRICE",
                    "trigger_mode": "LEVEL_INSTANT",
                    "evaluation_window": "1m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 100.0,
                    "product": "AAPL",
                    "contract_id": 1,
                }
            ]
        ),
    )

    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        gateway_not_work_event_throttle_seconds=300,
    )

    old_db_path = os.getenv("IBX_DB_PATH")
    old_gateway_ready = os.getenv("IBX_GATEWAY_READY")
    os.environ["IBX_DB_PATH"] = str(db_path)
    os.environ["IBX_GATEWAY_READY"] = "0"
    try:
        engine.process_once("S-WORKER-GW-THROTTLE", reason="unit_test")
        engine.process_once("S-WORKER-GW-THROTTLE", reason="unit_test")
        with get_connection() as conn:
            event_count_row = conn.execute(
                """
                SELECT COUNT(1) AS c
                FROM strategy_events
                WHERE strategy_id = ? AND event_type = 'GATEWAY_NOT_WORK'
                """,
                ("S-WORKER-GW-THROTTLE",),
            ).fetchone()
            assert event_count_row is not None
            assert event_count_row["c"] == 1

            throttle_row = conn.execute(
                """
                SELECT state_value
                FROM strategy_runtime_states
                WHERE strategy_id = ? AND state_key = 'event_throttle:GATEWAY_NOT_WORK'
                """,
                ("S-WORKER-GW-THROTTLE",),
            ).fetchone()
            assert throttle_row is not None
            assert throttle_row["state_value"] is not None
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path

        if old_gateway_ready is None:
            os.environ.pop("IBX_GATEWAY_READY", None)
        else:
            os.environ["IBX_GATEWAY_READY"] = old_gateway_ready


def test_waiting_for_market_data_event_is_throttled_by_runtime_state(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_waiting_throttle.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-WAIT-THROTTLE",
        db_path=db_path,
        status="ACTIVE",
        conditions_json=json.dumps(
            [
                {
                    "condition_id": "c1",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "PRICE",
                    "trigger_mode": "LEVEL_INSTANT",
                    "evaluation_window": "1m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 100.0,
                    "product": "AAPL",
                    "contract_id": 1,
                }
            ]
        ),
    )

    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        waiting_for_market_data_event_throttle_seconds=300,
    )

    old_db_path = os.getenv("IBX_DB_PATH")
    old_gateway_ready = os.getenv("IBX_GATEWAY_READY")
    os.environ["IBX_DB_PATH"] = str(db_path)
    os.environ.pop("IBX_GATEWAY_READY", None)
    try:
        engine.process_once("S-WORKER-WAIT-THROTTLE", reason="unit_test")
        engine.process_once("S-WORKER-WAIT-THROTTLE", reason="unit_test")
        with get_connection() as conn:
            event_count_row = conn.execute(
                """
                SELECT COUNT(1) AS c
                FROM strategy_events
                WHERE strategy_id = ? AND event_type = 'WAITING_FOR_MARKET_DATA'
                """,
                ("S-WORKER-WAIT-THROTTLE",),
            ).fetchone()
            assert event_count_row is not None
            assert event_count_row["c"] == 1

            throttle_row = conn.execute(
                """
                SELECT state_value
                FROM strategy_runtime_states
                WHERE strategy_id = ? AND state_key = 'event_throttle:WAITING_FOR_MARKET_DATA'
                """,
                ("S-WORKER-WAIT-THROTTLE",),
            ).fetchone()
            assert throttle_row is not None
            assert throttle_row["state_value"] is not None
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path

        if old_gateway_ready is None:
            os.environ.pop("IBX_GATEWAY_READY", None)
        else:
            os.environ["IBX_GATEWAY_READY"] = old_gateway_ready
