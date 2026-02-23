from __future__ import annotations

import logging
import queue
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Callable

from .chain import execute_triggered_strategy, sync_order_submitted_strategy_status
from .config import load_app_config
from .db import get_connection
from .evaluator import evaluate_strategy, persist_evaluation_result
from .verification import run_activation_verification


UTC = timezone.utc
TERMINAL_STATUSES: set[str] = {"FILLED", "EXPIRED", "CANCELLED", "FAILED"}
SCANNABLE_STATUSES: tuple[str, ...] = (
    "PENDING_ACTIVATION",
    "VERIFYING",
    "ACTIVE",
    "PAUSED",
    "TRIGGERED",
    "ORDER_SUBMITTED",
)
EXPIRABLE_STATUSES: set[str] = {"PENDING_ACTIVATION", "ACTIVE", "PAUSED", "TRIGGERED"}
RUNTIME_KEY_LAST_EVALUATION_OUTCOME = "last_evaluation_outcome"
RUNTIME_KEY_GATEWAY_NOT_WORK_EVENT_TS = "event_throttle:GATEWAY_NOT_WORK"
RUNTIME_KEY_WAITING_FOR_MARKET_DATA_EVENT_TS = "event_throttle:WAITING_FOR_MARKET_DATA"
DEFAULT_STRATEGY_LOCK_TTL_SECONDS = 120


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_iso_utc(dt: datetime) -> str:
    return _to_utc(dt).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class StrategyTask:
    strategy_id: str
    reason: str
    expected_status: str
    expected_version: int
    enqueued_at: datetime


class StrategyTaskQueue:
    def __init__(self, *, maxsize: int) -> None:
        self._queue: queue.Queue[StrategyTask] = queue.Queue(maxsize=maxsize)
        self._inflight: set[str] = set()
        self._lock = Lock()

    def enqueue(self, task: StrategyTask) -> bool:
        if not self.claim(task.strategy_id):
            return False
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            self.release(task.strategy_id)
            return False
        return True

    def pop(self, timeout: float) -> StrategyTask | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def mark_done(self, strategy_id: str) -> None:
        self.release(strategy_id)
        self._queue.task_done()

    def claim(self, strategy_id: str) -> bool:
        with self._lock:
            if strategy_id in self._inflight:
                return False
            self._inflight.add(strategy_id)
            return True

    def release(self, strategy_id: str) -> None:
        with self._lock:
            self._inflight.discard(strategy_id)


StrategyHandler = Callable[[sqlite3.Connection, sqlite3.Row, datetime], None]


class StrategyExecutionEngine:
    def __init__(
        self,
        *,
        enabled: bool = False,
        monitor_interval_seconds: int = 60,
        worker_count: int = 2,
        queue_maxsize: int = 4096,
        gateway_not_work_event_throttle_seconds: int = 300,
        waiting_for_market_data_event_throttle_seconds: int = 120,
        strategy_lock_ttl_seconds: int = DEFAULT_STRATEGY_LOCK_TTL_SECONDS,
    ) -> None:
        self._logger = logging.getLogger("ibx.worker")
        self._enabled = enabled
        self._monitor_interval_seconds = monitor_interval_seconds
        self._worker_count = worker_count
        self._queue = StrategyTaskQueue(maxsize=queue_maxsize)
        self._gateway_not_work_event_throttle_seconds = gateway_not_work_event_throttle_seconds
        self._waiting_for_market_data_event_throttle_seconds = (
            waiting_for_market_data_event_throttle_seconds
        )
        self._strategy_lock_ttl_seconds = max(1, int(strategy_lock_ttl_seconds))
        self._stop_event = Event()
        self._start_lock = Lock()
        self._running = False
        self._scanner_thread: Thread | None = None
        self._worker_threads: list[Thread] = []
        self._handlers: dict[str, StrategyHandler] = {}
        self._register_default_handlers()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def running(self) -> bool:
        return self._running

    def register_handler(self, statuses: list[str] | tuple[str, ...], handler: StrategyHandler) -> None:
        for status in statuses:
            self._handlers[status] = handler

    def start_if_enabled(self) -> None:
        if self._enabled:
            self.start()
        else:
            self._logger.info("strategy execution engine disabled (worker.enabled=false)")

    def start(self) -> None:
        with self._start_lock:
            if self._running:
                return
            cleared_locks = self._clear_legacy_locks()
            if cleared_locks > 0:
                self._logger.info("cleared legacy strategy locks count=%s", cleared_locks)
            self._stop_event.clear()
            self._scanner_thread = Thread(
                target=self._scan_loop,
                name="ibx-strategy-scanner",
                daemon=True,
            )
            self._worker_threads = [
                Thread(
                    target=self._worker_loop,
                    args=(idx + 1,),
                    name=f"ibx-strategy-worker-{idx + 1}",
                    daemon=True,
                )
                for idx in range(self._worker_count)
            ]
            self._scanner_thread.start()
            for thread in self._worker_threads:
                thread.start()
            self._running = True
            self._logger.info(
                "strategy execution engine started interval=%ss workers=%s",
                self._monitor_interval_seconds,
                self._worker_count,
            )

    def _clear_legacy_locks(self) -> int:
        with get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE strategies
                SET lock_until = NULL
                WHERE lock_until IS NOT NULL AND is_deleted = 0
                """
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def stop(self, timeout_seconds: float = 10.0) -> None:
        with self._start_lock:
            if not self._running:
                return
            self._stop_event.set()
            scanner = self._scanner_thread
            workers = list(self._worker_threads)
            self._scanner_thread = None
            self._worker_threads = []
            self._running = False

        if scanner is not None:
            scanner.join(timeout=timeout_seconds)
        for worker in workers:
            worker.join(timeout=timeout_seconds)
        self._logger.info("strategy execution engine stopped")

    def _load_task_snapshot(self, strategy_id: str) -> tuple[str, int] | None:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT status, version
                FROM v_strategies_active
                WHERE id = ?
                """,
                (strategy_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["status"]), int(row["version"])

    def enqueue_strategy(
        self,
        strategy_id: str,
        *,
        reason: str = "manual",
        expected_status: str | None = None,
        expected_version: int | None = None,
    ) -> bool:
        if expected_status is None or expected_version is None:
            snapshot = self._load_task_snapshot(strategy_id)
            if snapshot is None:
                return False
            expected_status, expected_version = snapshot
        task = StrategyTask(
            strategy_id=strategy_id,
            reason=reason,
            expected_status=expected_status,
            expected_version=expected_version,
            enqueued_at=_utcnow(),
        )
        accepted = self._queue.enqueue(task)
        if not accepted:
            self._logger.debug("skip enqueue strategy_id=%s reason=%s", strategy_id, reason)
        return accepted

    def scan_once(self) -> int:
        placeholders = ",".join("?" for _ in SCANNABLE_STATUSES)
        with get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT id, status, version
                FROM v_strategies_active
                WHERE status IN ({placeholders})
                ORDER BY updated_at ASC, id ASC
                """,
                SCANNABLE_STATUSES,
            ).fetchall()
        enqueued = 0
        for row in rows:
            if self.enqueue_strategy(
                row["id"],
                reason="periodic_scan",
                expected_status=str(row["status"]),
                expected_version=int(row["version"]),
            ):
                enqueued += 1
        return enqueued

    def process_once(self, strategy_id: str, *, reason: str = "manual") -> None:
        if not self._queue.claim(strategy_id):
            self._logger.debug("skip process_once strategy_id=%s reason=%s (already inflight)", strategy_id, reason)
            return
        try:
            snapshot = self._load_task_snapshot(strategy_id)
            if snapshot is None:
                return
            expected_status, expected_version = snapshot
            task = StrategyTask(
                strategy_id=strategy_id,
                reason=reason,
                expected_status=expected_status,
                expected_version=expected_version,
                enqueued_at=_utcnow(),
            )
            self._process_task(task)
        finally:
            self._queue.release(strategy_id)

    def _register_default_handlers(self) -> None:
        self.register_handler(["ACTIVE"], self._handle_active)
        self.register_handler(["VERIFYING"], self._handle_verifying)
        self.register_handler(["TRIGGERED"], self._handle_triggered)
        self.register_handler(["ORDER_SUBMITTED"], self._handle_order_submitted)
        self.register_handler(
            ["PENDING_ACTIVATION", "VERIFY_FAILED", "PAUSED"],
            self._handle_noop,
        )

    def _scan_loop(self) -> None:
        self._logger.info("scanner loop started")
        while not self._stop_event.is_set():
            try:
                enqueued = self.scan_once()
                self._logger.debug("scanner enqueued=%s", enqueued)
            except Exception:
                self._logger.exception("scanner loop failed")
            if self._stop_event.wait(timeout=float(self._monitor_interval_seconds)):
                break
        self._logger.info("scanner loop stopped")

    def _worker_loop(self, worker_index: int) -> None:
        self._logger.info("worker loop started worker=%s", worker_index)
        while not self._stop_event.is_set():
            task = self._queue.pop(timeout=0.5)
            if task is None:
                continue
            try:
                self._process_task(task)
            except Exception:
                self._logger.exception(
                    "worker failed strategy_id=%s reason=%s", task.strategy_id, task.reason
                )
            finally:
                self._queue.mark_done(task.strategy_id)
        self._logger.info("worker loop stopped worker=%s", worker_index)

    def _process_task(self, task: StrategyTask) -> None:
        now = _utcnow()
        lock_until_iso: str | None = None
        with get_connection() as conn:
            lock_until = _to_utc(now + timedelta(seconds=self._strategy_lock_ttl_seconds))
            lock_until_iso = _to_iso_utc(lock_until)
            cursor = conn.execute(
                """
                UPDATE strategies
                SET lock_until = ?
                WHERE id = ?
                  AND status = ?
                  AND version = ?
                  AND is_deleted = 0
                  AND (lock_until IS NULL OR lock_until <= ?)
                """,
                (
                    lock_until_iso,
                    task.strategy_id,
                    task.expected_status,
                    task.expected_version,
                    _to_iso_utc(now),
                ),
            )
            if cursor.rowcount <= 0:
                self._logger.debug(
                    "skip task strategy_id=%s reason=%s (snapshot changed status/version)",
                    task.strategy_id,
                    task.reason,
                )
                return
            conn.commit()

        try:
            now = _utcnow()
            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT *
                    FROM v_strategies_active
                    WHERE id = ? AND lock_until = ?
                    """,
                    (task.strategy_id, lock_until_iso),
                ).fetchone()
                if row is None:
                    return
                status = str(row["status"])
                if status in TERMINAL_STATUSES:
                    return

                if self._expire_if_needed(conn, strategy_row=row, now=now):
                    conn.commit()
                    return

                latest = conn.execute(
                    """
                    SELECT *
                    FROM v_strategies_active
                    WHERE id = ? AND lock_until = ?
                    """,
                    (task.strategy_id, lock_until_iso),
                ).fetchone()
                if latest is None:
                    return
                handler = self._handlers.get(str(latest["status"]), self._handle_noop)
                handler(conn, latest, now)
                conn.commit()
        finally:
            if lock_until_iso is not None:
                with get_connection() as conn:
                    conn.execute(
                        """
                        UPDATE strategies
                        SET lock_until = NULL
                        WHERE id = ? AND lock_until = ? AND is_deleted = 0
                        """,
                        (task.strategy_id, lock_until_iso),
                    )
                    conn.commit()

    def _effective_expire_at(self, strategy_row: sqlite3.Row) -> datetime | None:
        explicit = _parse_iso_utc(strategy_row["expire_at"])
        if explicit is not None:
            return explicit

        if strategy_row["expire_mode"] != "relative":
            return None
        if not strategy_row["expire_in_seconds"]:
            return None

        base = _parse_iso_utc(strategy_row["logical_activated_at"]) or _parse_iso_utc(
            strategy_row["activated_at"]
        )
        if base is None:
            return None
        return base + timedelta(seconds=int(strategy_row["expire_in_seconds"]))

    def _expire_if_needed(self, conn: sqlite3.Connection, *, strategy_row: sqlite3.Row, now: datetime) -> bool:
        status = str(strategy_row["status"])
        if status not in EXPIRABLE_STATUSES:
            return False
        expire_at = self._effective_expire_at(strategy_row)
        if expire_at is None or now < expire_at:
            return False

        now_iso = _to_iso_utc(now)
        cursor = conn.execute(
            """
            UPDATE strategies
            SET status = 'EXPIRED', updated_at = ?, version = version + 1
            WHERE id = ? AND status = ? AND is_deleted = 0
            """,
            (now_iso, strategy_row["id"], status),
        )
        if cursor.rowcount <= 0:
            return False

        self._append_event(
            conn,
            strategy_id=strategy_row["id"],
            event_type="EXPIRED",
            detail="策略到期，已终止执行",
            ts=now,
        )
        return True

    def _append_event(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
        event_type: str,
        detail: str,
        ts: datetime,
    ) -> None:
        conn.execute(
            """
            INSERT INTO strategy_events (strategy_id, timestamp, event_type, detail)
            VALUES (?, ?, ?, ?)
            """,
            (strategy_id, _to_iso_utc(ts), event_type, detail),
        )

    def _get_runtime_state(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
        state_key: str,
    ) -> str | None:
        row = conn.execute(
            """
            SELECT state_value
            FROM strategy_runtime_states
            WHERE strategy_id = ? AND state_key = ?
            """,
            (strategy_id, state_key),
        ).fetchone()
        if row is None:
            return None
        value = row["state_value"]
        return None if value is None else str(value)

    def _set_runtime_state(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
        state_key: str,
        state_value: str | None,
        now: datetime,
    ) -> None:
        now_iso = _to_iso_utc(now)
        conn.execute(
            """
            INSERT INTO strategy_runtime_states (strategy_id, state_key, state_value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(strategy_id, state_key) DO UPDATE SET
                state_value = excluded.state_value,
                updated_at = excluded.updated_at
            """,
            (strategy_id, state_key, state_value, now_iso),
        )

    def _should_emit_throttled_event(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
        event_state_key: str,
        now: datetime,
        throttle_seconds: int,
    ) -> bool:
        last_emitted_raw = self._get_runtime_state(
            conn,
            strategy_id=strategy_id,
            state_key=event_state_key,
        )
        if last_emitted_raw is None:
            return True
        last_emitted_at = _parse_iso_utc(last_emitted_raw)
        if last_emitted_at is None:
            return True
        return (now - last_emitted_at).total_seconds() >= float(throttle_seconds)

    def _handle_active(self, conn: sqlite3.Connection, strategy_row: sqlite3.Row, now: datetime) -> None:
        strategy_id = strategy_row["id"]
        result = evaluate_strategy(strategy_row, now=now)
        persist_evaluation_result(
            conn,
            strategy_id=strategy_id,
            evaluated_at=now,
            result=result,
        )
        previous_outcome = self._get_runtime_state(
            conn,
            strategy_id=strategy_id,
            state_key=RUNTIME_KEY_LAST_EVALUATION_OUTCOME,
        )
        self._set_runtime_state(
            conn,
            strategy_id=strategy_id,
            state_key=RUNTIME_KEY_LAST_EVALUATION_OUTCOME,
            state_value=result.outcome,
            now=now,
        )
        if result.outcome == "no_conditions_configured":
            cursor = conn.execute(
                """
                UPDATE strategies
                SET status = 'VERIFY_FAILED', updated_at = ?, version = version + 1
                WHERE id = ? AND status = 'ACTIVE' AND is_deleted = 0
                """,
                (_to_iso_utc(now), strategy_id),
            )
            if cursor.rowcount > 0:
                self._append_event(
                    conn,
                    strategy_id=strategy_id,
                    event_type="VERIFY_FAILED",
                    detail="ACTIVE 阶段评估失败：no_conditions_configured",
                    ts=now,
                )
            return
        if result.outcome == "condition_config_invalid":
            cursor = conn.execute(
                """
                UPDATE strategies
                SET status = 'VERIFY_FAILED', updated_at = ?, version = version + 1
                WHERE id = ? AND status = 'ACTIVE' AND is_deleted = 0
                """,
                (_to_iso_utc(now), strategy_id),
            )
            if cursor.rowcount > 0:
                error_detail = str(result.metrics.get("error") or "").strip()
                detail = "ACTIVE 阶段评估失败：condition_config_invalid"
                if error_detail:
                    detail = f"{detail}: {error_detail}"
                self._append_event(
                    conn,
                    strategy_id=strategy_id,
                    event_type="VERIFY_FAILED",
                    detail=detail,
                    ts=now,
                )
            return
        if result.outcome == "gateway_not_work":
            should_emit = previous_outcome != "gateway_not_work" or self._should_emit_throttled_event(
                conn,
                strategy_id=strategy_id,
                event_state_key=RUNTIME_KEY_GATEWAY_NOT_WORK_EVENT_TS,
                now=now,
                throttle_seconds=self._gateway_not_work_event_throttle_seconds,
            )
            if should_emit:
                self._append_event(
                    conn,
                    strategy_id=strategy_id,
                    event_type="GATEWAY_NOT_WORK",
                    detail="网关不可用，跳过本轮评估",
                    ts=now,
                )
                self._set_runtime_state(
                    conn,
                    strategy_id=strategy_id,
                    state_key=RUNTIME_KEY_GATEWAY_NOT_WORK_EVENT_TS,
                    state_value=_to_iso_utc(now),
                    now=now,
                )
            return
        if result.outcome == "waiting_for_market_data":
            should_emit = previous_outcome != "waiting_for_market_data" or self._should_emit_throttled_event(
                conn,
                strategy_id=strategy_id,
                event_state_key=RUNTIME_KEY_WAITING_FOR_MARKET_DATA_EVENT_TS,
                now=now,
                throttle_seconds=self._waiting_for_market_data_event_throttle_seconds,
            )
            if should_emit:
                self._append_event(
                    conn,
                    strategy_id=strategy_id,
                    event_type="WAITING_FOR_MARKET_DATA",
                    detail="行情数据未就绪，跳过本轮评估",
                    ts=now,
                )
                self._set_runtime_state(
                    conn,
                    strategy_id=strategy_id,
                    state_key=RUNTIME_KEY_WAITING_FOR_MARKET_DATA_EVENT_TS,
                    state_value=_to_iso_utc(now),
                    now=now,
                )
            return
        if result.outcome != "evaluated":
            return
        if not result.condition_met:
            return

        cursor = conn.execute(
            """
            UPDATE strategies
            SET status = 'TRIGGERED', updated_at = ?, version = version + 1
            WHERE id = ? AND status = 'ACTIVE' AND is_deleted = 0
            """,
            (_to_iso_utc(now), strategy_id),
        )
        if cursor.rowcount <= 0:
            return
        self._append_event(
            conn,
            strategy_id=strategy_id,
            event_type="TRIGGERED",
            detail=result.decision_reason,
            ts=now,
        )

    def _handle_verifying(self, conn: sqlite3.Connection, strategy_row: sqlite3.Row, now: datetime) -> None:
        strategy_id = strategy_row["id"]
        now_iso = _to_iso_utc(now)
        verification_result = run_activation_verification(
            conn,
            strategy_id=strategy_id,
            strategy_row=strategy_row,
        )
        if not verification_result.passed:
            cursor = conn.execute(
                """
                UPDATE strategies
                SET status = 'VERIFY_FAILED', updated_at = ?, version = version + 1
                WHERE id = ? AND status = 'VERIFYING' AND is_deleted = 0
                """,
                (now_iso, strategy_id),
            )
            if cursor.rowcount > 0:
                self._append_event(
                    conn,
                    strategy_id=strategy_id,
                    event_type="VERIFY_FAILED",
                    detail=verification_result.reason,
                    ts=now,
                )
            return

        activated_at_iso = strategy_row["activated_at"] or now_iso
        logical_activated_at_iso = strategy_row["logical_activated_at"] or activated_at_iso
        expire_at_iso = strategy_row["expire_at"]
        if (
            expire_at_iso is None
            and strategy_row["expire_mode"] == "relative"
            and strategy_row["expire_in_seconds"]
        ):
            base = _parse_iso_utc(logical_activated_at_iso) or now
            expire_at_iso = _to_iso_utc(base + timedelta(seconds=int(strategy_row["expire_in_seconds"])))

        cursor = conn.execute(
            """
            UPDATE strategies
            SET status = 'ACTIVE',
                activated_at = ?,
                logical_activated_at = ?,
                expire_at = ?,
                updated_at = ?,
                version = version + 1
            WHERE id = ? AND status = 'VERIFYING' AND is_deleted = 0
            """,
            (
                activated_at_iso,
                logical_activated_at_iso,
                expire_at_iso,
                now_iso,
                strategy_id,
            ),
        )
        if cursor.rowcount <= 0:
            return
        self._append_event(
            conn,
            strategy_id=strategy_id,
            event_type="ACTIVATED",
            detail=(
                "策略已通过激活校验并转 ACTIVE"
                f" (resolved_symbol_contracts={verification_result.resolved_symbol_contracts},"
                f" updated_condition_contracts={verification_result.updated_condition_contracts})"
            ),
            ts=now,
        )

    def _handle_triggered(self, conn: sqlite3.Connection, strategy_row: sqlite3.Row, now: datetime) -> None:
        execute_triggered_strategy(conn, strategy_row=strategy_row, now=now)

    def _handle_order_submitted(
        self,
        conn: sqlite3.Connection,
        strategy_row: sqlite3.Row,
        now: datetime,
    ) -> None:
        sync_order_submitted_strategy_status(conn, strategy_row=strategy_row, now=now)

    def _handle_noop(self, conn: sqlite3.Connection, strategy_row: sqlite3.Row, now: datetime) -> None:
        _ = (conn, strategy_row, now)


def build_execution_engine_from_config() -> StrategyExecutionEngine:
    worker_cfg = load_app_config().worker
    return StrategyExecutionEngine(
        enabled=worker_cfg.enabled,
        monitor_interval_seconds=worker_cfg.monitor_interval_seconds,
        worker_count=worker_cfg.threads,
        queue_maxsize=worker_cfg.queue_maxsize,
        gateway_not_work_event_throttle_seconds=worker_cfg.gateway_not_work_event_throttle_seconds,
        waiting_for_market_data_event_throttle_seconds=(
            worker_cfg.waiting_for_market_data_event_throttle_seconds
        ),
    )


def build_execution_engine_from_env() -> StrategyExecutionEngine:
    # Backward-compatible alias; worker settings are now config-file only.
    return build_execution_engine_from_config()


worker_engine = build_execution_engine_from_config()
