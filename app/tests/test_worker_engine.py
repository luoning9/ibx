from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.config import clear_app_config_cache
from app.db import get_connection, init_db
from app.market_data import (
    HistoricalBar,
    HistoricalBarsRequest,
    HistoricalBarsResult,
    TradingCalendarRequest,
    TradingCalendarResult,
    TradingCalendarSession,
)
from app.verification import ActivationVerificationResult
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


def _insert_symbol(
    strategy_id: str,
    *,
    db_path: Path,
    position: int,
    code: str,
    contract_id: int | None,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_symbols (strategy_id, position, code, trade_type, contract_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (strategy_id, position, code, "buy", contract_id, _iso_now()),
        )
        conn.commit()


def _parse_bar_size_delta(bar_size: str) -> timedelta:
    parts = bar_size.strip().lower().split()
    if len(parts) != 2:
        return timedelta(minutes=1)
    try:
        amount = int(parts[0])
    except ValueError:
        return timedelta(minutes=1)
    unit = parts[1]
    if unit in {"min", "mins", "minute", "minutes"}:
        return timedelta(minutes=max(1, amount))
    if unit in {"hour", "hours"}:
        return timedelta(hours=max(1, amount))
    if unit in {"day", "days"}:
        return timedelta(days=max(1, amount))
    return timedelta(minutes=1)


class _FakeMarketDataProvider:
    def __init__(
        self,
        *,
        closes_by_symbol: dict[str, list[float]],
        trading_calendar_by_contract: dict[int, list[tuple[datetime, datetime]]] | None = None,
    ) -> None:
        self._closes_by_symbol = {k.upper(): list(v) for k, v in closes_by_symbol.items()}
        self._trading_calendar_by_contract = trading_calendar_by_contract or {}
        self.requests: list[HistoricalBarsRequest] = []
        self.calendar_requests: list[TradingCalendarRequest] = []

    def get_historical_bars(self, request: HistoricalBarsRequest) -> HistoricalBarsResult:
        self.requests.append(request)
        if isinstance(request.contract, str):
            symbol = request.contract.strip().upper()
        else:
            symbol = str(request.contract.get("code", "")).strip().upper()
        closes = self._closes_by_symbol.get(symbol, [])
        delta = _parse_bar_size_delta(request.bar_size)
        end = request.end_time.astimezone(UTC)
        bars: list[HistoricalBar] = []
        for idx, close in enumerate(closes):
            ts = end - delta * (len(closes) - idx)
            bars.append(
                HistoricalBar(
                    ts=ts,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1000.0,
                )
            )
        return HistoricalBarsResult(bars=bars, meta={"source": "TEST"})

    def get_trading_calendar(self, request: TradingCalendarRequest) -> TradingCalendarResult:
        self.calendar_requests.append(request)
        raw_sessions = self._trading_calendar_by_contract.get(int(request.contract_id), [])
        sessions: list[TradingCalendarSession] = []
        for start, end in raw_sessions:
            start_utc = start.astimezone(UTC)
            end_utc = end.astimezone(UTC)
            sessions.append(
                TradingCalendarSession(
                    ref_date=start_utc.strftime("%Y%m%d"),
                    start_time=start_utc,
                    end_time=end_utc,
                )
            )
        return TradingCalendarResult(sessions=sessions, meta={"source": "TEST"})


class _FakeOrderService:
    def __init__(
        self,
        *,
        normalized_status: str = "ORDER_SUBMITTED",
        order_id: int = 2812,
        perm_id: int = 91001,
        filled_qty: float = 0.0,
        remaining_qty: float = 1.0,
        avg_fill_price: float | None = None,
        poll_snapshot: object | None = None,
        error: Exception | None = None,
    ) -> None:
        self.normalized_status = normalized_status
        self.order_id = order_id
        self.perm_id = perm_id
        self.filled_qty = filled_qty
        self.remaining_qty = remaining_qty
        self.avg_fill_price = avg_fill_price
        self.poll_snapshot = poll_snapshot
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.poll_calls: list[dict[str, object]] = []

    def submit_trade_action(self, *, trade_action: dict[str, object], order_ref: str | None = None):  # type: ignore[no-untyped-def]
        self.calls.append({"trade_action": dict(trade_action), "order_ref": order_ref})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            order_id=self.order_id,
            perm_id=self.perm_id,
            status="SUBMITTED",
            normalized_status=self.normalized_status,
            terminal=self.normalized_status in {"FILLED", "CANCELLED", "FAILED"},
            filled_qty=self.filled_qty,
            remaining_qty=self.remaining_qty,
            avg_fill_price=self.avg_fill_price,
            symbol=str(trade_action.get("symbol", "")),
            side=str(trade_action.get("side", "")),
            order_type=str(trade_action.get("order_type", "")),
            quantity=float(trade_action.get("quantity", 0.0) or 0.0),
            account_code=None,
            submitted_at=datetime.now(UTC),
        )

    def poll_order_status(self, *, order_id=None, perm_id=None):  # type: ignore[no-untyped-def]
        self.poll_calls.append({"order_id": order_id, "perm_id": perm_id})
        return self.poll_snapshot

    def poll_order_status_by_order_ref(self, *, order_ref):  # type: ignore[no-untyped-def]
        self.poll_calls.append({"order_ref": order_ref})
        return self.poll_snapshot


def test_strategy_task_queue_deduplicates_inflight() -> None:
    task_queue = StrategyTaskQueue(maxsize=8)
    task = StrategyTask(
        strategy_id="S-TQ-1",
        reason="unit_test",
        expected_status="ACTIVE",
        expected_version=1,
        enqueued_at=datetime.now(UTC),
    )

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


def test_scan_once_enqueues_task_snapshot(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_scan_snapshot.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy("S-WORKER-SCAN-SNAPSHOT", db_path=db_path, status="ACTIVE")

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        enqueued = engine.scan_once()
        assert enqueued == 1

        task = engine._queue.pop(timeout=0.01)
        assert task is not None
        assert task.strategy_id == "S-WORKER-SCAN-SNAPSHOT"
        assert task.expected_status == "ACTIVE"
        assert task.expected_version == 1
        engine._queue.mark_done(task.strategy_id)
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


def test_start_clears_legacy_locks(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_start_clear_locks.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy("S-WORKER-LEGACY-LOCK", db_path=db_path, status="VERIFY_FAILED")
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE strategies SET lock_until = ? WHERE id = ?",
            ("2099-01-01T00:00:00Z", "S-WORKER-LEGACY-LOCK"),
        )
        conn.commit()

    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=600,
        worker_count=1,
    )
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.start()
        engine.stop()
        with get_connection() as conn:
            row = conn.execute(
                "SELECT lock_until FROM strategies WHERE id = ?",
                ("S-WORKER-LEGACY-LOCK",),
            ).fetchone()
            assert row is not None
            assert row["lock_until"] is None
    finally:
        if engine.running:
            engine.stop()
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_task_skips_when_status_changed_after_enqueue_snapshot(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_stale_status.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy("S-WORKER-STALE-STATUS", db_path=db_path, status="ACTIVE")
    stale_task = StrategyTask(
        strategy_id="S-WORKER-STALE-STATUS",
        reason="unit_test",
        expected_status="ACTIVE",
        expected_version=1,
        enqueued_at=datetime.now(UTC),
    )

    now_iso = _iso_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE strategies
            SET status = 'PAUSED', updated_at = ?, version = version + 1
            WHERE id = ?
            """,
            (now_iso, "S-WORKER-STALE-STATUS"),
        )
        conn.commit()

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine._process_task(stale_task)
        with get_connection() as conn:
            row = conn.execute(
                "SELECT status, version, lock_until FROM strategies WHERE id = ?",
                ("S-WORKER-STALE-STATUS",),
            ).fetchone()
            assert row is not None
            assert row["status"] == "PAUSED"
            assert row["version"] == 2
            assert row["lock_until"] is None
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_task_skips_when_version_changed_after_enqueue_snapshot(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_stale_version.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy("S-WORKER-STALE-VERSION", db_path=db_path, status="ACTIVE")
    stale_task = StrategyTask(
        strategy_id="S-WORKER-STALE-VERSION",
        reason="unit_test",
        expected_status="ACTIVE",
        expected_version=1,
        enqueued_at=datetime.now(UTC),
    )

    now_iso = _iso_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE strategies
            SET description = ?, updated_at = ?, version = version + 1
            WHERE id = ?
            """,
            ("updated by api", now_iso, "S-WORKER-STALE-VERSION"),
        )
        conn.commit()

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine._process_task(stale_task)
        with get_connection() as conn:
            row = conn.execute(
                "SELECT status, version, lock_until FROM strategies WHERE id = ?",
                ("S-WORKER-STALE-VERSION",),
            ).fetchone()
            assert row is not None
            assert row["status"] == "ACTIVE"
            assert row["version"] == 2
            assert row["lock_until"] is None
    finally:
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
        max_monitoring_interval_minutes = 66
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
        assert engine._max_monitoring_interval_minutes == 66
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
                    "trigger_mode": "CROSS_UP_INSTANT",
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
    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": [101.0]})

    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        market_data_provider=provider,
    )

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-ACTIVE", reason="unit_test")
        with get_connection() as conn:
            initial_run = conn.execute(
                """
                SELECT last_monitoring_data_end_at, check_count
                FROM strategy_runs
                WHERE strategy_id = ?
                """,
                ("S-WORKER-ACTIVE",),
            ).fetchone()
            assert initial_run is not None
            strategy_row = conn.execute(
                "SELECT logical_activated_at FROM strategies WHERE id = ?",
                ("S-WORKER-ACTIVE",),
            ).fetchone()
            assert strategy_row is not None
            monitoring_end_map = json.loads(initial_run["last_monitoring_data_end_at"])
            assert monitoring_end_map["c1"]["1"] == strategy_row["logical_activated_at"]
            assert initial_run["check_count"] == 1

        engine.process_once("S-WORKER-ACTIVE", reason="unit_test")
        with get_connection() as conn:
            run_row = conn.execute(
                """
                SELECT
                    last_monitoring_data_end_at,
                    condition_met,
                    decision_reason,
                    last_outcome,
                    check_count,
                    metrics_json
                FROM strategy_runs
                WHERE strategy_id = ?
                """,
                ("S-WORKER-ACTIVE",),
            ).fetchone()
            assert run_row is not None
            monitoring_end_map = json.loads(run_row["last_monitoring_data_end_at"])
            assert monitoring_end_map["c1"]["1"] == strategy_row["logical_activated_at"]
            assert run_row["condition_met"] == 0
            assert run_row["decision_reason"] == "waiting_for_market_data"
            assert run_row["last_outcome"] == "waiting_for_market_data"
            assert run_row["check_count"] == 2
            metrics = json.loads(run_row["metrics_json"])
            assert "market_data_preparation" in metrics

            check_count_row = conn.execute(
                "SELECT COUNT(1) AS c FROM strategy_runs WHERE strategy_id = ?",
                ("S-WORKER-ACTIVE",),
            ).fetchone()
            assert check_count_row is not None
            assert check_count_row["c"] == 1

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


def test_process_once_active_uses_market_data_provider_and_triggers(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_market_data.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-MD",
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
    _insert_symbol(
        "S-WORKER-MD",
        db_path=db_path,
        position=1,
        code="AAPL",
        contract_id=1,
    )

    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": [100.5, 101.2]})
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        market_data_provider=provider,
    )

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-MD", reason="unit_test")
        with get_connection() as conn:
            run_row = conn.execute(
                """
                SELECT condition_met, decision_reason, metrics_json, last_monitoring_data_end_at
                FROM strategy_runs
                WHERE strategy_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                ("S-WORKER-MD",),
            ).fetchone()
            assert run_row is not None
            assert run_row["condition_met"] == 1
            assert run_row["decision_reason"] == "conditions_met"
            metrics = json.loads(run_row["metrics_json"])
            summary = metrics.get("market_data_preparation")
            assert isinstance(summary, dict)
            assert summary.get("conditions_total") == 1
            assert summary.get("conditions_with_input") == 1
            conditions = summary.get("conditions")
            assert isinstance(conditions, list)
            assert len(conditions) == 1
            first = conditions[0]
            assert first.get("condition_id") == "c1"
            assert first.get("status") == "evaluated"
            contracts = first.get("contracts")
            assert isinstance(contracts, list)
            assert len(contracts) == 1
            assert contracts[0].get("status") == "ready"
            assert contracts[0].get("symbol") == "AAPL"
            assert contracts[0].get("series_points") == 2
            assert len(provider.requests) > 0
            req_end = provider.requests[-1].end_time.astimezone(UTC)
            expected_last_bar = req_end.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            monitoring_end_map = json.loads(run_row["last_monitoring_data_end_at"])
            assert monitoring_end_map["c1"]["1"] == expected_last_bar

            state_row = conn.execute(
                """
                SELECT state, last_value
                FROM condition_states
                WHERE strategy_id = ? AND condition_id = ?
                """,
                ("S-WORKER-MD", "c1"),
            ).fetchone()
            assert state_row is not None
            assert state_row["state"] == "TRUE"
            assert state_row["last_value"] is not None

            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-MD",),
            ).fetchone()
            assert strategy_row is not None
            assert strategy_row["status"] == "TRIGGERED"
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_market_data_requirement_uses_per_key_last_monitoring_end_as_start_time(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_market_data_start_from_last_end.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-MD-LAST-END",
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
    _insert_symbol(
        "S-WORKER-MD-LAST-END",
        db_path=db_path,
        position=1,
        code="AAPL",
        contract_id=1,
    )
    last_end_iso = (datetime.now(UTC) - timedelta(minutes=10)).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_runs (
                strategy_id, last_monitoring_data_end_at, first_evaluated_at, evaluated_at,
                condition_met, decision_reason, last_outcome, check_count, metrics_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-WORKER-MD-LAST-END",
                json.dumps({"c1": {"1": last_end_iso}}),
                _iso_now(),
                _iso_now(),
                0,
                "waiting_for_market_data",
                "waiting_for_market_data",
                1,
                "{}",
                _iso_now(),
            ),
        )
        conn.commit()

    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": [101.0, 102.0]})
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        market_data_provider=provider,
    )
    old_db_path = os.getenv("IBX_DB_PATH")
    old_gateway_ready = os.getenv("IBX_GATEWAY_READY")
    os.environ["IBX_DB_PATH"] = str(db_path)
    os.environ["IBX_GATEWAY_READY"] = "1"
    try:
        engine.process_once("S-WORKER-MD-LAST-END", reason="unit_test")
        assert len(provider.requests) > 0
        request = provider.requests[0]
        assert request.start_time.replace(microsecond=0).isoformat().replace("+00:00", "Z") == last_end_iso
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path
        if old_gateway_ready is None:
            os.environ.pop("IBX_GATEWAY_READY", None)
        else:
            os.environ["IBX_GATEWAY_READY"] = old_gateway_ready


def test_waiting_for_market_data_does_not_advance_last_monitoring_end(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_waiting_keep_last_end.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-MD-WAITING-LAST-END",
        db_path=db_path,
        status="ACTIVE",
        conditions_json=json.dumps(
            [
                {
                    "condition_id": "c1",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "PRICE",
                    "trigger_mode": "CROSS_UP_INSTANT",
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
    _insert_symbol(
        "S-WORKER-MD-WAITING-LAST-END",
        db_path=db_path,
        position=1,
        code="AAPL",
        contract_id=1,
    )

    last_end_iso = (datetime.now(UTC) - timedelta(minutes=15)).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_runs (
                strategy_id, last_monitoring_data_end_at, first_evaluated_at, evaluated_at,
                condition_met, decision_reason, last_outcome, check_count, metrics_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-WORKER-MD-WAITING-LAST-END",
                json.dumps({"c1": {"1": last_end_iso}}),
                _iso_now(),
                _iso_now(),
                0,
                "waiting_for_market_data",
                "waiting_for_market_data",
                1,
                "{}",
                _iso_now(),
            ),
        )
        conn.commit()

    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": [101.0]})
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        market_data_provider=provider,
    )

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-MD-WAITING-LAST-END", reason="unit_test")
        with get_connection() as conn:
            run_row = conn.execute(
                """
                SELECT last_monitoring_data_end_at, last_outcome
                FROM strategy_runs
                WHERE strategy_id = ?
                """,
                ("S-WORKER-MD-WAITING-LAST-END",),
            ).fetchone()
            assert run_row is not None
            assert run_row["last_outcome"] == "waiting_for_market_data"
            monitoring_end_map = json.loads(run_row["last_monitoring_data_end_at"])
            assert monitoring_end_map["c1"]["1"] == last_end_iso
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_active_skips_monitoring_when_before_suggested_next_and_within_max_interval(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_skip_by_suggested_next.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-SKIP-SUGGESTED",
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
    _insert_symbol(
        "S-WORKER-SKIP-SUGGESTED",
        db_path=db_path,
        position=1,
        code="AAPL",
        contract_id=1,
    )

    now = datetime.now(UTC).replace(microsecond=0)
    evaluated_at = now - timedelta(minutes=10)
    suggested_next = now + timedelta(minutes=30)
    last_end_iso = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_runs (
                strategy_id, last_monitoring_data_end_at, suggested_next_monitor_at, first_evaluated_at, evaluated_at,
                condition_met, decision_reason, last_outcome, check_count, metrics_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-WORKER-SKIP-SUGGESTED",
                json.dumps({"c1": {"1": last_end_iso}}),
                suggested_next.isoformat().replace("+00:00", "Z"),
                evaluated_at.isoformat().replace("+00:00", "Z"),
                evaluated_at.isoformat().replace("+00:00", "Z"),
                0,
                "no_new_data",
                "no_new_data",
                1,
                "{}",
                evaluated_at.isoformat().replace("+00:00", "Z"),
            ),
        )
        conn.commit()

    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": [101.0]})
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        max_monitoring_interval_minutes=60,
        worker_count=1,
        market_data_provider=provider,
    )

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-SKIP-SUGGESTED", reason="unit_test")
        assert len(provider.requests) == 0
        with get_connection() as conn:
            run_row = conn.execute(
                """
                SELECT check_count, evaluated_at, suggested_next_monitor_at
                FROM strategy_runs
                WHERE strategy_id = ?
                """,
                ("S-WORKER-SKIP-SUGGESTED",),
            ).fetchone()
            assert run_row is not None
            assert int(run_row["check_count"]) == 1
            assert run_row["evaluated_at"] == evaluated_at.isoformat().replace("+00:00", "Z")
            assert run_row["suggested_next_monitor_at"] == suggested_next.isoformat().replace("+00:00", "Z")
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_active_does_not_skip_when_max_monitoring_interval_exceeded(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_no_skip_after_max_interval.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-NO-SKIP-SUGGESTED",
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
    _insert_symbol(
        "S-WORKER-NO-SKIP-SUGGESTED",
        db_path=db_path,
        position=1,
        code="AAPL",
        contract_id=1,
    )

    now = datetime.now(UTC).replace(microsecond=0)
    evaluated_at = now - timedelta(minutes=120)
    suggested_next = now + timedelta(minutes=30)
    last_end_iso = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_runs (
                strategy_id, last_monitoring_data_end_at, suggested_next_monitor_at, first_evaluated_at, evaluated_at,
                condition_met, decision_reason, last_outcome, check_count, metrics_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-WORKER-NO-SKIP-SUGGESTED",
                json.dumps({"c1": {"1": last_end_iso}}),
                suggested_next.isoformat().replace("+00:00", "Z"),
                evaluated_at.isoformat().replace("+00:00", "Z"),
                evaluated_at.isoformat().replace("+00:00", "Z"),
                0,
                "no_new_data",
                "no_new_data",
                1,
                "{}",
                evaluated_at.isoformat().replace("+00:00", "Z"),
            ),
        )
        conn.commit()

    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": []})
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        max_monitoring_interval_minutes=60,
        worker_count=1,
        market_data_provider=provider,
    )

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-NO-SKIP-SUGGESTED", reason="unit_test")
        assert len(provider.requests) > 0
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_no_new_data_skips_evaluate_and_keeps_last_monitoring_end(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_no_new_data.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-MD-NO-NEW",
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
    _insert_symbol(
        "S-WORKER-MD-NO-NEW",
        db_path=db_path,
        position=1,
        code="AAPL",
        contract_id=1,
    )

    last_end_iso = (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    previous_eval_iso = (datetime.now(UTC) - timedelta(minutes=30)).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_runs (
                strategy_id, last_monitoring_data_end_at, first_evaluated_at, evaluated_at,
                condition_met, decision_reason, last_outcome, check_count, metrics_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-WORKER-MD-NO-NEW",
                json.dumps({"c1": {"1": last_end_iso}}),
                previous_eval_iso,
                previous_eval_iso,
                0,
                "waiting_for_market_data",
                "waiting_for_market_data",
                1,
                "{}",
                previous_eval_iso,
            ),
        )
        conn.commit()

    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": []})
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        market_data_provider=provider,
    )

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-MD-NO-NEW", reason="unit_test")
        with get_connection() as conn:
            run_row = conn.execute(
                """
                SELECT last_monitoring_data_end_at, decision_reason, last_outcome, suggested_next_monitor_at, evaluated_at, updated_at
                FROM strategy_runs
                WHERE strategy_id = ?
                """,
                ("S-WORKER-MD-NO-NEW",),
            ).fetchone()
            assert run_row is not None
            assert run_row["decision_reason"] == "no_new_data"
            assert run_row["last_outcome"] == "no_new_data"
            assert run_row["suggested_next_monitor_at"] is None
            assert run_row["evaluated_at"] == previous_eval_iso
            assert str(run_row["updated_at"]) >= previous_eval_iso
            monitoring_end_map = json.loads(run_row["last_monitoring_data_end_at"])
            assert monitoring_end_map["c1"]["1"] == last_end_iso
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_no_new_data_outside_session_sets_suggested_next_monitor_at(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_no_new_data_suggested_next.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-MD-NO-NEW-SUGGEST",
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
    _insert_symbol(
        "S-WORKER-MD-NO-NEW-SUGGEST",
        db_path=db_path,
        position=1,
        code="AAPL",
        contract_id=1,
    )

    last_end_iso = (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_runs (
                strategy_id, last_monitoring_data_end_at, first_evaluated_at, evaluated_at,
                condition_met, decision_reason, last_outcome, check_count, metrics_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-WORKER-MD-NO-NEW-SUGGEST",
                json.dumps({"c1": {"1": last_end_iso}}),
                _iso_now(),
                _iso_now(),
                0,
                "waiting_for_market_data",
                "waiting_for_market_data",
                1,
                "{}",
                _iso_now(),
            ),
        )
        conn.commit()

    now = datetime.now(UTC).replace(microsecond=0)
    next_session_start = now + timedelta(minutes=30)
    provider = _FakeMarketDataProvider(
        closes_by_symbol={"AAPL": []},
        trading_calendar_by_contract={
            1: [
                (
                    next_session_start,
                    next_session_start + timedelta(hours=6),
                )
            ]
        },
    )
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        market_data_provider=provider,
    )

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-MD-NO-NEW-SUGGEST", reason="unit_test")
        with get_connection() as conn:
            run_row = conn.execute(
                """
                SELECT decision_reason, last_outcome, suggested_next_monitor_at
                FROM strategy_runs
                WHERE strategy_id = ?
                """,
                ("S-WORKER-MD-NO-NEW-SUGGEST",),
            ).fetchone()
            assert run_row is not None
            assert run_row["decision_reason"] == "no_new_data"
            assert run_row["last_outcome"] == "no_new_data"
            assert run_row["suggested_next_monitor_at"] == next_session_start.isoformat().replace("+00:00", "Z")
            event_row = conn.execute(
                """
                SELECT event_type, detail
                FROM strategy_events
                WHERE strategy_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                ("S-WORKER-MD-NO-NEW-SUGGEST",),
            ).fetchone()
            assert event_row is not None
            assert event_row["event_type"] == "MONITOR_SCHEDULED"
            assert "suggested_next_monitor_at" in str(event_row["detail"])
            assert next_session_start.isoformat().replace("+00:00", "Z") in str(event_row["detail"])
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_or_short_circuit_keeps_skipped_condition_last_evaluated_at(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_or_short_circuit_keep_last_eval.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-OR-SHORT",
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
                },
                {
                    "condition_id": "c2",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "PRICE",
                    "trigger_mode": "LEVEL_INSTANT",
                    "evaluation_window": "1m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 100.0,
                    "product": "MSFT",
                    "contract_id": 2,
                },
            ]
        ),
    )
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE strategies SET condition_logic = 'OR' WHERE id = ?",
            ("S-WORKER-OR-SHORT",),
        )
        previous_eval_iso = (datetime.now(UTC) - timedelta(minutes=90)).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
        conn.execute(
            """
            INSERT INTO condition_states (
                strategy_id, condition_id, state, last_value, last_evaluated_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "S-WORKER-OR-SHORT",
                "c2",
                "FALSE",
                88.0,
                previous_eval_iso,
                previous_eval_iso,
            ),
        )
        conn.commit()

    _insert_symbol(
        "S-WORKER-OR-SHORT",
        db_path=db_path,
        position=1,
        code="AAPL",
        contract_id=1,
    )
    _insert_symbol(
        "S-WORKER-OR-SHORT",
        db_path=db_path,
        position=2,
        code="MSFT",
        contract_id=2,
    )

    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": [101.0], "MSFT": [90.0]})
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        market_data_provider=provider,
    )

    old_db_path = os.getenv("IBX_DB_PATH")
    old_gateway_ready = os.getenv("IBX_GATEWAY_READY")
    os.environ["IBX_DB_PATH"] = str(db_path)
    os.environ["IBX_GATEWAY_READY"] = "1"
    try:
        engine.process_once("S-WORKER-OR-SHORT", reason="unit_test")
        with get_connection() as conn:
            state_row = conn.execute(
                """
                SELECT state, last_evaluated_at
                FROM condition_states
                WHERE strategy_id = ? AND condition_id = ?
                """,
                ("S-WORKER-OR-SHORT", "c2"),
            ).fetchone()
            assert state_row is not None
            assert state_row["state"] == "NOT_EVALUATED"
            assert state_row["last_evaluated_at"] == previous_eval_iso
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path
        if old_gateway_ready is None:
            os.environ.pop("IBX_GATEWAY_READY", None)
        else:
            os.environ["IBX_GATEWAY_READY"] = old_gateway_ready


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

    order_service = _FakeOrderService()
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        order_service=order_service,
    )

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
            order_row = conn.execute(
                "SELECT status, ib_order_id FROM orders WHERE strategy_id = ?",
                ("S-WORKER-TRIG",),
            ).fetchone()
            assert order_row is not None
            assert order_row["status"] == "ORDER_SUBMITTED"
            assert order_row["ib_order_id"] == "91001"
            assert len(order_service.calls) == 1
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_triggered_skips_when_existing_order_dispatching(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_triggered_existing_dispatching.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-TRIG-DISPATCHING",
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
    now_iso = _iso_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trade_instructions (
                trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "T-EXISTING01",
                "S-WORKER-TRIG-DISPATCHING",
                "STOCK_TRADE BUY AAPL MKT qty=1",
                "ORDER_DISPATCHING",
                None,
                now_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO orders (
                id, strategy_id, ib_order_id, status, qty, avg_fill_price, filled_qty, error_message,
                order_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "T-EXISTING01",
                "S-WORKER-TRIG-DISPATCHING",
                None,
                "ORDER_DISPATCHING",
                1.0,
                None,
                0.0,
                None,
                json.dumps({"dispatch": {"order_ref": "T-EXISTING01"}}),
                now_iso,
                now_iso,
            ),
        )
        conn.commit()

    order_service = _FakeOrderService()
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        order_service=order_service,
    )
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-TRIG-DISPATCHING", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-TRIG-DISPATCHING",),
            ).fetchone()
            assert strategy_row is not None
            assert strategy_row["status"] == "TRIGGERED"

            instruction_rows = conn.execute(
                """
                SELECT trade_id, status
                FROM trade_instructions
                WHERE strategy_id = ?
                ORDER BY updated_at DESC
                """,
                ("S-WORKER-TRIG-DISPATCHING",),
            ).fetchall()
            assert len(instruction_rows) == 1
            assert instruction_rows[0]["trade_id"] == "T-EXISTING01"
            assert instruction_rows[0]["status"] == "ORDER_DISPATCHING"
            assert len(order_service.calls) == 0
            assert len(order_service.poll_calls) >= 1
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_triggered_reconciles_existing_order_dispatching(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_triggered_existing_dispatching_reconcile.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-TRIG-DISPATCHING-RECONCILE",
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
    now_iso = _iso_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trade_instructions (
                trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "T-EXISTING02",
                "S-WORKER-TRIG-DISPATCHING-RECONCILE",
                "STOCK_TRADE BUY AAPL MKT qty=1",
                "ORDER_DISPATCHING",
                None,
                now_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO orders (
                id, strategy_id, ib_order_id, status, qty, avg_fill_price, filled_qty, error_message,
                order_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "T-EXISTING02",
                "S-WORKER-TRIG-DISPATCHING-RECONCILE",
                None,
                "ORDER_DISPATCHING",
                1.0,
                None,
                0.0,
                None,
                json.dumps({"dispatch": {"order_ref": "T-EXISTING02"}}),
                now_iso,
                now_iso,
            ),
        )
        conn.commit()

    order_service = _FakeOrderService(
        poll_snapshot=SimpleNamespace(
            order_id=32001,
            perm_id=920001,
            normalized_status="ORDER_SUBMITTED",
            avg_fill_price=None,
            filled_qty=0.0,
            error_message=None,
        )
    )
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        order_service=order_service,
    )
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-TRIG-DISPATCHING-RECONCILE", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-TRIG-DISPATCHING-RECONCILE",),
            ).fetchone()
            assert strategy_row is not None
            assert strategy_row["status"] == "ORDER_SUBMITTED"

            instruction_row = conn.execute(
                "SELECT status FROM trade_instructions WHERE trade_id = ?",
                ("T-EXISTING02",),
            ).fetchone()
            assert instruction_row is not None
            assert instruction_row["status"] == "ORDER_SUBMITTED"
            order_row = conn.execute(
                "SELECT status, ib_order_id FROM orders WHERE id = ?",
                ("T-EXISTING02",),
            ).fetchone()
            assert order_row is not None
            assert order_row["status"] == "ORDER_SUBMITTED"
            assert order_row["ib_order_id"] == "920001"
            assert len(order_service.calls) == 0
            assert len(order_service.poll_calls) >= 1
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_triggered_dispatching_timeout_moves_to_failed(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_triggered_dispatching_timeout.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-TRIG-DISPATCHING-TIMEOUT",
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
    old_iso = (datetime.now(UTC) - timedelta(minutes=10)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trade_instructions (
                trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "T-EXISTING03",
                "S-WORKER-TRIG-DISPATCHING-TIMEOUT",
                "STOCK_TRADE BUY AAPL MKT qty=1",
                "ORDER_DISPATCHING",
                None,
                old_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO orders (
                id, strategy_id, ib_order_id, status, qty, avg_fill_price, filled_qty, error_message,
                order_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "T-EXISTING03",
                "S-WORKER-TRIG-DISPATCHING-TIMEOUT",
                None,
                "ORDER_DISPATCHING",
                1.0,
                None,
                0.0,
                None,
                json.dumps({"dispatch": {"order_ref": "T-EXISTING03"}}),
                old_iso,
                old_iso,
            ),
        )
        conn.commit()

    order_service = _FakeOrderService(poll_snapshot=None)
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        order_service=order_service,
        dispatching_reconcile_timeout_seconds=1.0,
    )
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-TRIG-DISPATCHING-TIMEOUT", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-TRIG-DISPATCHING-TIMEOUT",),
            ).fetchone()
            assert strategy_row is not None
            assert strategy_row["status"] == "FAILED"

            instruction_row = conn.execute(
                "SELECT status FROM trade_instructions WHERE trade_id = ?",
                ("T-EXISTING03",),
            ).fetchone()
            assert instruction_row is not None
            assert instruction_row["status"] == "FAILED"
            order_row = conn.execute(
                "SELECT status, error_message FROM orders WHERE id = ?",
                ("T-EXISTING03",),
            ).fetchone()
            assert order_row is not None
            assert order_row["status"] == "FAILED"
            assert "timeout" in str(order_row["error_message"]).lower()
            assert len(order_service.calls) == 0
            assert len(order_service.poll_calls) >= 1
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_order_submitted_polls_and_moves_to_filled(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_order_submitted_filled.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy("S-WORKER-ORDER-SUB-FILLED", db_path=db_path, status="ORDER_SUBMITTED")
    now_iso = _iso_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trade_instructions (
                trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "T-SUBMITTED01",
                "S-WORKER-ORDER-SUB-FILLED",
                "STOCK_TRADE BUY AAPL MKT qty=1",
                "ORDER_SUBMITTED",
                None,
                now_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO orders (
                id, strategy_id, ib_order_id, status, qty, avg_fill_price, filled_qty, error_message,
                order_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "T-SUBMITTED01",
                "S-WORKER-ORDER-SUB-FILLED",
                "91001",
                "ORDER_SUBMITTED",
                1.0,
                None,
                0.0,
                None,
                json.dumps({"dispatch": {"order_ref": "T-SUBMITTED01"}}),
                now_iso,
                now_iso,
            ),
        )
        conn.commit()

    order_service = _FakeOrderService(
        poll_snapshot=SimpleNamespace(
            order_id=32011,
            perm_id=91001,
            normalized_status="FILLED",
            avg_fill_price=188.5,
            filled_qty=1.0,
            error_message=None,
        )
    )
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        order_service=order_service,
    )
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-ORDER-SUB-FILLED", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-ORDER-SUB-FILLED",),
            ).fetchone()
            assert strategy_row is not None
            assert strategy_row["status"] == "FILLED"

            instruction_row = conn.execute(
                "SELECT status FROM trade_instructions WHERE trade_id = ?",
                ("T-SUBMITTED01",),
            ).fetchone()
            assert instruction_row is not None
            assert instruction_row["status"] == "FILLED"

            order_row = conn.execute(
                "SELECT status, ib_order_id, avg_fill_price, filled_qty FROM orders WHERE id = ?",
                ("T-SUBMITTED01",),
            ).fetchone()
            assert order_row is not None
            assert order_row["status"] == "FILLED"
            assert order_row["ib_order_id"] == "91001"
            assert float(order_row["avg_fill_price"]) == 188.5
            assert float(order_row["filled_qty"]) == 1.0
            assert len(order_service.calls) == 0
            assert len(order_service.poll_calls) >= 1
            assert order_service.poll_calls[0]["perm_id"] == 91001
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_order_submitted_polls_by_order_ref_for_partial_fill(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_order_submitted_partial.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy("S-WORKER-ORDER-SUB-PARTIAL", db_path=db_path, status="ORDER_SUBMITTED")
    now_iso = _iso_now()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trade_instructions (
                trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "T-SUBMITTED02",
                "S-WORKER-ORDER-SUB-PARTIAL",
                "STOCK_TRADE BUY AAPL MKT qty=1",
                "ORDER_SUBMITTED",
                None,
                now_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO orders (
                id, strategy_id, ib_order_id, status, qty, avg_fill_price, filled_qty, error_message,
                order_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "T-SUBMITTED02",
                "S-WORKER-ORDER-SUB-PARTIAL",
                None,
                "ORDER_SUBMITTED",
                1.0,
                None,
                0.0,
                None,
                json.dumps({"dispatch": {"order_ref": "T-SUBMITTED02"}}),
                now_iso,
                now_iso,
            ),
        )
        conn.commit()

    order_service = _FakeOrderService(
        poll_snapshot=SimpleNamespace(
            order_id=32012,
            perm_id=93002,
            normalized_status="PARTIAL_FILL",
            avg_fill_price=187.2,
            filled_qty=0.4,
            error_message=None,
        )
    )
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        order_service=order_service,
    )
    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-ORDER-SUB-PARTIAL", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-ORDER-SUB-PARTIAL",),
            ).fetchone()
            assert strategy_row is not None
            assert strategy_row["status"] == "ORDER_SUBMITTED"

            instruction_row = conn.execute(
                "SELECT status FROM trade_instructions WHERE trade_id = ?",
                ("T-SUBMITTED02",),
            ).fetchone()
            assert instruction_row is not None
            assert instruction_row["status"] == "PARTIAL_FILL"

            order_row = conn.execute(
                "SELECT status, ib_order_id, avg_fill_price, filled_qty FROM orders WHERE id = ?",
                ("T-SUBMITTED02",),
            ).fetchone()
            assert order_row is not None
            assert order_row["status"] == "PARTIAL_FILL"
            assert order_row["ib_order_id"] == "93002"
            assert float(order_row["avg_fill_price"]) == 187.2
            assert float(order_row["filled_qty"]) == 0.4
            assert len(order_service.calls) == 0
            assert len(order_service.poll_calls) >= 1
            assert any(call.get("order_ref") == "T-SUBMITTED02" for call in order_service.poll_calls)
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_verifying_moves_to_active(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "ibx_worker_verifying.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-VERIFY",
        db_path=db_path,
        status="VERIFYING",
    )
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_runs (
                strategy_id, last_monitoring_data_end_at, first_evaluated_at, evaluated_at,
                condition_met, decision_reason, last_outcome, check_count, metrics_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-WORKER-VERIFY",
                json.dumps({"c1": {"1": _iso_now()}}),
                _iso_now(),
                _iso_now(),
                0,
                "waiting_for_market_data",
                "waiting_for_market_data",
                1,
                "{}",
                _iso_now(),
            ),
        )
        conn.commit()
    monkeypatch.setattr(
        "app.worker.run_activation_verification",
        lambda conn, *, strategy_id, strategy_row, trade_service=None: ActivationVerificationResult(
            passed=True,
            reason="verification_passed",
            resolved_symbol_contracts=1,
            updated_condition_contracts=2,
        ),
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

            run_row = conn.execute(
                "SELECT COUNT(1) AS c FROM strategy_runs WHERE strategy_id = ?",
                ("S-WORKER-VERIFY",),
            ).fetchone()
            assert run_row is not None
            assert int(run_row["c"]) == 0
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_verifying_logs_trade_validation_context(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "ibx_worker_verifying_context.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-VERIFY-CONTEXT",
        db_path=db_path,
        status="VERIFYING",
    )
    monkeypatch.setattr(
        "app.worker.run_activation_verification",
        lambda conn, *, strategy_id, strategy_row, trade_service=None: ActivationVerificationResult(
            passed=True,
            reason="verification_passed",
            resolved_symbol_contracts=1,
            updated_condition_contracts=0,
            trade_validation_context={
                "market": "US_STOCK",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 1.0,
            },
        ),
    )
    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-VERIFY-CONTEXT", reason="unit_test")
        with get_connection() as conn:
            context_event = conn.execute(
                """
                SELECT event_type, detail
                FROM strategy_events
                WHERE strategy_id = ? AND event_type = 'VERIFY_TRADE_ACTION_CONTEXT'
                ORDER BY id DESC
                LIMIT 1
                """,
                ("S-WORKER-VERIFY-CONTEXT",),
            ).fetchone()
            assert context_event is not None
            payload = json.loads(str(context_event["detail"] or "{}"))
            assert payload["market"] == "US_STOCK"
            assert payload["symbol"] == "AAPL"
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_verifying_moves_to_verify_failed_when_verification_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "ibx_worker_verifying_failed.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-VERIFY-FAIL",
        db_path=db_path,
        status="VERIFYING",
    )
    monkeypatch.setattr(
        "app.worker.run_activation_verification",
        lambda conn, *, strategy_id, strategy_row, trade_service=None: ActivationVerificationResult(
            passed=False,
            reason="verification rejected",
        ),
    )

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-VERIFY-FAIL", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-VERIFY-FAIL",),
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
                ("S-WORKER-VERIFY-FAIL",),
            ).fetchone()
            assert event_row is not None
            assert event_row["event_type"] == "VERIFY_FAILED"
            assert "verification rejected" in event_row["detail"]
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_active_moves_to_verify_failed_when_activation_time_missing(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_active_missing_activation_time.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-ACTIVE-MISSING-ACTIVATION",
        db_path=db_path,
        status="ACTIVE",
    )
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE strategies
            SET activated_at = NULL, logical_activated_at = NULL
            WHERE id = ?
            """,
            ("S-WORKER-ACTIVE-MISSING-ACTIVATION",),
        )
        conn.commit()

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-ACTIVE-MISSING-ACTIVATION", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-ACTIVE-MISSING-ACTIVATION",),
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
                ("S-WORKER-ACTIVE-MISSING-ACTIVATION",),
            ).fetchone()
            assert event_row is not None
            assert event_row["event_type"] == "VERIFY_FAILED"
            assert "missing_activation_time" in str(event_row["detail"])

            run_row = conn.execute(
                "SELECT COUNT(1) AS c FROM strategy_runs WHERE strategy_id = ?",
                ("S-WORKER-ACTIVE-MISSING-ACTIVATION",),
            ).fetchone()
            assert run_row is not None
            assert int(run_row["c"]) == 0
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_active_missing_activation_time_has_priority_over_skip_gate(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_active_missing_activation_priority.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-ACTIVE-MISSING-ACTIVATION-PRIORITY",
        db_path=db_path,
        status="ACTIVE",
    )
    now = datetime.now(UTC).replace(microsecond=0)
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE strategies
            SET activated_at = NULL, logical_activated_at = NULL
            WHERE id = ?
            """,
            ("S-WORKER-ACTIVE-MISSING-ACTIVATION-PRIORITY",),
        )
        conn.execute(
            """
            INSERT INTO strategy_runs (
                strategy_id, last_monitoring_data_end_at, suggested_next_monitor_at, first_evaluated_at, evaluated_at,
                condition_met, decision_reason, last_outcome, check_count, metrics_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-WORKER-ACTIVE-MISSING-ACTIVATION-PRIORITY",
                json.dumps({"c1": {"1": _iso_now()}}),
                (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                now.isoformat().replace("+00:00", "Z"),
                now.isoformat().replace("+00:00", "Z"),
                0,
                "no_new_data",
                "no_new_data",
                1,
                "{}",
                now.isoformat().replace("+00:00", "Z"),
            ),
        )
        conn.commit()

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        engine.process_once("S-WORKER-ACTIVE-MISSING-ACTIVATION-PRIORITY", reason="unit_test")
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-ACTIVE-MISSING-ACTIVATION-PRIORITY",),
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
                ("S-WORKER-ACTIVE-MISSING-ACTIVATION-PRIORITY",),
            ).fetchone()
            assert event_row is not None
            assert event_row["event_type"] == "VERIFY_FAILED"
            assert "missing_activation_time" in str(event_row["detail"])
    finally:
        if old_db_path is None:
            os.environ.pop("IBX_DB_PATH", None)
        else:
            os.environ["IBX_DB_PATH"] = old_db_path


def test_process_once_active_raises_when_market_data_provider_missing(tmp_path) -> None:
    db_path = tmp_path / "ibx_worker_active_missing_market_data_provider.sqlite3"
    init_db(db_path=db_path)
    _insert_strategy(
        "S-WORKER-ACTIVE-MISSING-PROVIDER",
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
    _insert_symbol(
        "S-WORKER-ACTIVE-MISSING-PROVIDER",
        db_path=db_path,
        position=1,
        code="AAPL",
        contract_id=1,
    )

    engine = StrategyExecutionEngine(enabled=False, monitor_interval_seconds=60, worker_count=1)

    old_db_path = os.getenv("IBX_DB_PATH")
    os.environ["IBX_DB_PATH"] = str(db_path)
    try:
        try:
            engine.process_once("S-WORKER-ACTIVE-MISSING-PROVIDER", reason="unit_test")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "missing market data provider" in str(exc).lower()
        with get_connection() as conn:
            strategy_row = conn.execute(
                "SELECT status FROM strategies WHERE id = ?",
                ("S-WORKER-ACTIVE-MISSING-PROVIDER",),
            ).fetchone()
            assert strategy_row is not None
            assert strategy_row["status"] == "ACTIVE"

            event_row = conn.execute(
                """
                SELECT COUNT(1) AS c
                FROM strategy_events
                WHERE strategy_id = ?
                """,
                ("S-WORKER-ACTIVE-MISSING-PROVIDER",),
            ).fetchone()
            assert event_row is not None
            assert int(event_row["c"]) == 0

            run_row = conn.execute(
                "SELECT COUNT(1) AS c FROM strategy_runs WHERE strategy_id = ?",
                ("S-WORKER-ACTIVE-MISSING-PROVIDER",),
            ).fetchone()
            assert run_row is not None
            assert int(run_row["c"]) == 0
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

    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": [101.0]})
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        market_data_provider=provider,
    )

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
                SELECT event_type, detail
                FROM strategy_events
                WHERE strategy_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                ("S-WORKER-NO-COND",),
            ).fetchone()
            assert event_row is not None
            assert event_row["event_type"] == "VERIFY_FAILED"
            assert "missing_data_requirements" in str(event_row["detail"])
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

    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": [101.0]})
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        market_data_provider=provider,
    )

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

    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": [101.0]})
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        gateway_not_work_event_throttle_seconds=300,
        market_data_provider=provider,
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
                    "trigger_mode": "CROSS_UP_INSTANT",
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

    provider = _FakeMarketDataProvider(closes_by_symbol={"AAPL": [101.0]})
    engine = StrategyExecutionEngine(
        enabled=False,
        monitor_interval_seconds=60,
        worker_count=1,
        waiting_for_market_data_event_throttle_seconds=300,
        market_data_provider=provider,
    )

    old_db_path = os.getenv("IBX_DB_PATH")
    old_gateway_ready = os.getenv("IBX_GATEWAY_READY")
    os.environ["IBX_DB_PATH"] = str(db_path)
    os.environ["IBX_GATEWAY_READY"] = "1"
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
