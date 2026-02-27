from __future__ import annotations

import json
import logging
import queue
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable

from .chain import execute_triggered_strategy, sync_order_submitted_strategy_status
from .config import load_app_config
from .db import get_connection, init_db
from .evaluator import (
    ConditionEvaluationInput,
    ConditionEvaluationState,
    ConditionEvaluator,
    StrategyEvaluationResult,
    gateway_is_working,
    persist_evaluation_result,
)
from .market_data import (
    HistoricalBar,
    HistoricalBarsRequest,
    MarketDataProvider,
    TradingCalendarRequest,
    build_market_data_provider_from_config,
)
from .market_data_ib import IBSessionHistoricalFetcher
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


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _compact_bar_size_to_ib(value: str) -> tuple[str, timedelta] | None:
    text = str(value or "").strip().lower()
    if len(text) < 2:
        return None
    unit = text[-1]
    try:
        amount = int(text[:-1])
    except ValueError:
        return None
    if amount <= 0:
        return None
    if unit == "m":
        return f"{amount} min", timedelta(minutes=amount)
    if unit == "h":
        return f"{amount} hour", timedelta(hours=amount)
    if unit == "d":
        return f"{amount} day", timedelta(days=amount)
    return None


def _bar_price_value(bar: HistoricalBar, basis: str) -> float:
    normalized = basis.strip().upper()
    if normalized == "HIGH":
        return float(bar.high)
    if normalized == "LOW":
        return float(bar.low)
    if normalized == "AVG":
        if bar.wap is not None:
            return float(bar.wap)
        return float((bar.open + bar.high + bar.low + bar.close) / 4.0)
    return float(bar.close)


def _bar_value_for_metric(metric: str, *, basis: str, bar: HistoricalBar) -> float | None:
    metric_key = metric.strip().upper()
    if metric_key in {"PRICE", "DRAWDOWN_PCT", "RALLY_PCT", "SPREAD"}:
        return _bar_price_value(bar, basis)
    if metric_key == "VOLUME_RATIO":
        if bar.volume is None:
            return None
        return float(bar.volume)
    if metric_key == "AMOUNT_RATIO":
        if bar.volume is None:
            return None
        return float(bar.volume) * _bar_price_value(bar, basis)
    return _bar_price_value(bar, basis)


def _latest_non_partial_bar_end_time(
    bars: list[HistoricalBar],
    *,
    bar_delta: timedelta,
    now: datetime,
) -> datetime | None:
    if not bars:
        return None
    now_utc = _to_utc(now)
    latest: datetime | None = None
    for bar in bars:
        end_ts = _to_utc(bar.ts) + bar_delta
        if end_ts > now_utc:
            continue
        if latest is None or end_ts > latest:
            latest = end_ts
    return latest


def _build_worker_market_data_provider() -> MarketDataProvider:
    cfg = load_app_config()
    if cfg.providers.market_data == "fixture":
        return build_market_data_provider_from_config()
    fetcher = IBSessionHistoricalFetcher()
    return build_market_data_provider_from_config(fetcher=fetcher)


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

    def qsize(self) -> int:
        return int(self._queue.qsize())

    def maxsize(self) -> int:
        return int(self._queue.maxsize)

    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)


StrategyHandler = Callable[[sqlite3.Connection, sqlite3.Row, datetime], None]


class StrategyExecutionEngine:
    def __init__(
        self,
        *,
        enabled: bool = False,
        monitor_interval_seconds: int = 60,
        max_monitoring_interval_minutes: int = 60,
        worker_count: int = 2,
        queue_maxsize: int = 4096,
        gateway_not_work_event_throttle_seconds: int = 300,
        waiting_for_market_data_event_throttle_seconds: int = 120,
        strategy_lock_ttl_seconds: int = DEFAULT_STRATEGY_LOCK_TTL_SECONDS,
        market_data_provider: MarketDataProvider | None = None,
    ) -> None:
        self._logger = logging.getLogger("ibx.worker")
        self._enabled = enabled
        self._monitor_interval_seconds = monitor_interval_seconds
        self._max_monitoring_interval_minutes = max(1, int(max_monitoring_interval_minutes))
        self._worker_count = worker_count
        self._queue = StrategyTaskQueue(maxsize=queue_maxsize)
        self._gateway_not_work_event_throttle_seconds = gateway_not_work_event_throttle_seconds
        self._waiting_for_market_data_event_throttle_seconds = (
            waiting_for_market_data_event_throttle_seconds
        )
        self._strategy_lock_ttl_seconds = max(1, int(strategy_lock_ttl_seconds))
        self._market_data_provider = market_data_provider
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

    def runtime_status(self) -> dict[str, int | bool]:
        scanner = self._scanner_thread
        worker_threads = list(self._worker_threads)
        live_worker_threads = sum(1 for thread in worker_threads if thread.is_alive())
        return {
            "enabled": bool(self._enabled),
            "running": bool(self._running),
            "monitor_interval_seconds": int(self._monitor_interval_seconds),
            "max_monitoring_interval_minutes": int(self._max_monitoring_interval_minutes),
            "configured_threads": int(self._worker_count),
            "live_threads": int(live_worker_threads),
            "scanner_alive": bool(scanner is not None and scanner.is_alive()),
            "queue_length": self._queue.qsize(),
            "queue_maxsize": self._queue.maxsize(),
            "inflight_tasks": self._queue.inflight_count(),
        }

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
            init_db()
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

    def _load_contract_payloads(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
        market: str,
    ) -> tuple[dict[int, dict[str, str]], dict[str, dict[str, str]]]:
        rows = conn.execute(
            """
            SELECT code, contract_id
            FROM strategy_symbols
            WHERE strategy_id = ?
            ORDER BY position ASC, id ASC
            """,
            (strategy_id,),
        ).fetchall()
        by_contract_id: dict[int, dict[str, str]] = {}
        by_symbol: dict[str, dict[str, str]] = {}
        for row in rows:
            symbol = _normalize_symbol(row["code"])
            if not symbol:
                continue
            payload = {"market": market, "code": symbol}
            by_symbol.setdefault(symbol, payload)
            contract_id = _to_int_or_none(row["contract_id"])
            if contract_id is not None:
                by_contract_id.setdefault(contract_id, payload)
        return by_contract_id, by_symbol

    def _resolve_contract_payload(
        self,
        *,
        contract_id: int | None,
        product_hint: Any,
        market: str,
        by_contract_id: dict[int, dict[str, str]],
        by_symbol: dict[str, dict[str, str]],
    ) -> dict[str, str] | None:
        if contract_id is not None:
            payload = by_contract_id.get(contract_id)
            if payload is not None:
                return payload
        symbol = _normalize_symbol(product_hint)
        if symbol:
            return by_symbol.get(symbol, {"market": market, "code": symbol})
        return None

    def _build_condition_inputs_from_market_data(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_row: sqlite3.Row,
        initial_last_monitoring_data_end_at: datetime,
        now: datetime,
    ) -> tuple[
        StrategyEvaluationResult,
        dict[str, Any] | None,
        dict[tuple[str, int], datetime],
        bool,
        datetime | None,
        bool,
    ]:
        provider = self._market_data_provider
        strategy_id = str(strategy_row["id"])
        default_metrics = {
            "evaluation_engine": "skeleton_v1",
            "evaluated_at": _to_iso_utc(now),
        }

        try:
            conditions_raw = json.loads(strategy_row["conditions_json"] or "[]")
        except json.JSONDecodeError:
            result = StrategyEvaluationResult(
                outcome="condition_config_invalid",
                condition_met=False,
                decision_reason="condition_config_invalid",
                metrics={
                    **default_metrics,
                    "conditions": 0,
                    "trigger_policies": [],
                    "error": "invalid_conditions_json",
                },
                condition_states=[],
            )
            return result, {"conditions_total": 0, "conditions_with_input": 0, "conditions": []}, {}, True, None, False
        if not isinstance(conditions_raw, list):
            result = StrategyEvaluationResult(
                outcome="condition_config_invalid",
                condition_met=False,
                decision_reason="condition_config_invalid",
                metrics={
                    **default_metrics,
                    "conditions": 0,
                    "trigger_policies": [],
                    "error": "invalid_conditions_payload",
                },
                condition_states=[],
            )
            return result, {"conditions_total": 0, "conditions_with_input": 0, "conditions": []}, {}, True, None, False
        if not conditions_raw:
            result = StrategyEvaluationResult(
                outcome="no_conditions_configured",
                condition_met=False,
                decision_reason="no_conditions_configured",
                metrics={
                    **default_metrics,
                    "conditions": 0,
                    "trigger_policies": [],
                },
                condition_states=[],
            )
            return result, {"conditions_total": 0, "conditions_with_input": 0, "conditions": []}, {}, False, None, False

        if not gateway_is_working():
            condition_states = []
            for idx, condition in enumerate(conditions_raw, start=1):
                condition_dict = condition if isinstance(condition, dict) else {}
                condition_id = str(condition_dict.get("condition_id") or f"c{idx}")
                condition_states.append(
                    ConditionEvaluationState(
                        condition_id=condition_id,
                        state="NOT_EVALUATED",
                        last_evaluated_at=now,
                    )
                )
            result = StrategyEvaluationResult(
                outcome="gateway_not_work",
                condition_met=False,
                decision_reason="gateway_not_work",
                metrics={
                    **default_metrics,
                    "conditions": len(condition_states),
                    "trigger_policies": [],
                },
                condition_states=condition_states,
            )
            return (
                result,
                {"conditions_total": len(conditions_raw), "conditions_with_input": 0, "conditions": []},
                {},
                True,
                None,
                False,
            )

        last_monitoring_data_end_map = self._load_last_monitoring_data_end_map(
            conn,
            strategy_id=strategy_id,
        )
        market = str(strategy_row["market"] or "").strip().upper()
        by_contract_id, by_symbol = self._load_contract_payloads(
            conn,
            strategy_id=strategy_id,
            market=market,
        )

        fetch_cache: dict[tuple[str, str, bool, str], list[HistoricalBar]] = {}
        summary_conditions: list[dict[str, Any]] = []
        monitoring_end_updates: dict[tuple[str, int], datetime] = {}
        has_data_requirements = False
        has_condition_evaluated = False
        conditions_with_input = 0
        condition_states: list[ConditionEvaluationState] = []
        condition_outcomes: list[str] = []
        condition_no_new_data_suggestions: list[datetime | None] = []
        trigger_policies: list[dict[str, Any]] = []
        condition_logic = str(strategy_row["condition_logic"] or "AND").strip().upper()
        if condition_logic not in {"AND", "OR"}:
            condition_logic = "AND"
        or_short_circuit_from_index: int | None = None

        for idx, item in enumerate(conditions_raw, start=1):
            condition = item if isinstance(item, dict) else {}
            condition_summary: dict[str, Any] = {
                "condition_id": str(condition.get("condition_id") or f"c{idx}"),
                "status": "waiting",
                "contracts": [],
                "input_ready": False,
            }
            summary_conditions.append(condition_summary)
            evaluator = ConditionEvaluator(condition)
            try:
                evaluator.prepare()
            except ValueError as exc:
                condition_id = str(condition.get("condition_id") or f"c{idx}")
                result = StrategyEvaluationResult(
                    outcome="condition_config_invalid",
                    condition_met=False,
                    decision_reason="condition_config_invalid",
                    metrics={
                        **default_metrics,
                        "conditions": len(conditions_raw),
                        "trigger_policies": trigger_policies,
                        "invalid_condition_id": condition_id,
                        "error": str(exc),
                    },
                    condition_states=[
                        ConditionEvaluationState(
                            condition_id=condition_id,
                            state="NOT_EVALUATED",
                            last_evaluated_at=now,
                        )
                    ],
                )
                condition_summary["status"] = "prepare_error"
                condition_summary["reason"] = str(exc)
                return (
                    result,
                    {
                        "conditions_total": len(conditions_raw),
                        "conditions_with_input": conditions_with_input,
                        "conditions": summary_conditions,
                    },
                    {},
                    True,
                    None,
                    False,
                )
            prepared = evaluator.prepared
            if prepared is None:
                condition_id = str(condition.get("condition_id") or f"c{idx}")
                result = StrategyEvaluationResult(
                    outcome="condition_config_invalid",
                    condition_met=False,
                    decision_reason="condition_config_invalid",
                    metrics={
                        **default_metrics,
                        "conditions": len(conditions_raw),
                        "trigger_policies": trigger_policies,
                        "invalid_condition_id": condition_id,
                        "error": "prepared_condition_missing",
                    },
                    condition_states=[
                        ConditionEvaluationState(
                            condition_id=condition_id,
                            state="NOT_EVALUATED",
                            last_evaluated_at=now,
                        )
                    ],
                )
                condition_summary["status"] = "prepare_error"
                condition_summary["reason"] = "prepared_condition_missing"
                return (
                    result,
                    {
                        "conditions_total": len(conditions_raw),
                        "conditions_with_input": conditions_with_input,
                        "conditions": summary_conditions,
                    },
                    {},
                    True,
                    None,
                    False,
                )

            if prepared.requirement.contracts:
                has_data_requirements = True
            trigger_policies.append(
                {
                    "condition_id": prepared.condition_id,
                    "trigger_mode": prepared.trigger_mode,
                    "evaluation_window": prepared.evaluation_window,
                    "missing_data_policy": prepared.requirement.missing_data_policy,
                    "require_time_alignment": prepared.requirement.require_time_alignment,
                    "contracts": [
                        {
                            "contract_id": contract_req.contract_id,
                            "base_bar": contract_req.base_bar,
                            "required_points": int(contract_req.required_points),
                            "include_partial_bar": bool(contract_req.include_partial_bar),
                            "use_rth": True,
                        }
                        for contract_req in prepared.requirement.contracts
                    ],
                }
            )

            metric = prepared.metric
            basis = str(condition.get("window_price_basis", "CLOSE")).strip().upper() or "CLOSE"
            values_by_contract: dict[int, list[float]] = {}
            contracts_obj = condition_summary["contracts"]
            if not isinstance(contracts_obj, list):
                contracts_obj = []
                condition_summary["contracts"] = contracts_obj
            contracts_summary: list[dict[str, Any]] = contracts_obj
            condition_summary["condition_id"] = prepared.condition_id
            condition_summary["metric"] = metric
            condition_summary["trigger_mode"] = prepared.trigger_mode
            condition_summary["evaluation_window"] = prepared.evaluation_window
            condition_summary["status"] = "prepared"

            condition_monitoring_end_updates: dict[tuple[str, int], datetime] = {}
            condition_has_new_data = False
            condition_contract_ids: list[int] = []
            for contract_index, contract_req in enumerate(prepared.requirement.contracts):
                contract_id = contract_req.contract_id
                contract_summary: dict[str, Any] = {
                    "contract_id": contract_id,
                    "status": "waiting",
                    "required_points": int(contract_req.required_points),
                    "include_partial_bar": bool(contract_req.include_partial_bar),
                    "base_bar": contract_req.base_bar,
                    "use_rth": True,
                }
                contracts_summary.append(contract_summary)
                if contract_id is None:
                    contract_summary["status"] = "missing_contract_id"
                    continue
                condition_contract_ids.append(contract_id)
                bar_cfg = _compact_bar_size_to_ib(contract_req.base_bar)
                if bar_cfg is None:
                    contract_summary["status"] = "invalid_base_bar"
                    continue
                ib_bar_size, bar_delta = bar_cfg
                contract_summary["bar_size"] = ib_bar_size
                product_hint = condition.get("product") if contract_index == 0 else condition.get("product_b")
                payload = self._resolve_contract_payload(
                    contract_id=contract_id,
                    product_hint=product_hint,
                    market=market,
                    by_contract_id=by_contract_id,
                    by_symbol=by_symbol,
                )
                if payload is None:
                    contract_summary["status"] = "unresolved_contract"
                    continue
                contract_summary["symbol"] = payload["code"]

                lookback_points = max(3, int(contract_req.required_points) + 2)
                contract_summary["lookback_points"] = lookback_points
                required_start_time = now - (bar_delta * lookback_points)
                requirement_last_end_at = self._resolve_requirement_last_monitoring_data_end_at(
                    last_monitoring_data_end_map=last_monitoring_data_end_map,
                    condition_id=prepared.condition_id,
                    contract_id=contract_id,
                    default_last_monitoring_data_end_at=initial_last_monitoring_data_end_at,
                )
                contract_summary["last_monitoring_data_end_at"] = _to_iso_utc(requirement_last_end_at)
                start_time = requirement_last_end_at if required_start_time > requirement_last_end_at else required_start_time
                cache_key = (
                    f"{payload['market']}|{payload['code']}",
                    ib_bar_size,
                    bool(contract_req.include_partial_bar),
                    _to_iso_utc(start_time),
                )
                bars = fetch_cache.get(cache_key)
                contract_summary["from_cache"] = bars is not None
                if bars is None:
                    if provider is None:
                        raise RuntimeError("missing market data provider")
                    request = HistoricalBarsRequest(
                        contract=payload,
                        start_time=start_time,
                        end_time=now,
                        bar_size=ib_bar_size,
                        what_to_show="TRADES",
                        use_rth=True,
                        include_partial_bar=bool(contract_req.include_partial_bar),
                    )
                    try:
                        fetch_result = provider.get_historical_bars(request)
                        bars = list(fetch_result.bars)
                    except Exception as exc:  # noqa: BLE001
                        self._logger.debug(
                            "market data fetch failed strategy_id=%s condition_id=%s contract_id=%s payload=%s error=%s",
                            strategy_id,
                            prepared.condition_id or f"c{idx}",
                            contract_id,
                            payload,
                            exc,
                        )
                        contract_summary["status"] = "fetch_failed"
                        contract_summary["error"] = type(exc).__name__
                        bars = []
                    fetch_cache[cache_key] = bars

                contract_summary["bars"] = len(bars)
                latest_non_partial_bar = _latest_non_partial_bar_end_time(
                    bars,
                    bar_delta=bar_delta,
                    now=now,
                )
                if latest_non_partial_bar is not None:
                    contract_summary["last_non_partial_bar_at"] = _to_iso_utc(latest_non_partial_bar)
                    if latest_non_partial_bar > requirement_last_end_at:
                        condition_has_new_data = True
                        contract_summary["has_new_data"] = True
                        key = (prepared.condition_id, contract_id)
                        current = condition_monitoring_end_updates.get(key)
                        if current is None or latest_non_partial_bar > current:
                            condition_monitoring_end_updates[key] = latest_non_partial_bar
                    else:
                        contract_summary["has_new_data"] = False
                else:
                    contract_summary["has_new_data"] = False

                series: list[float] = []
                for bar in bars:
                    value = _bar_value_for_metric(metric, basis=basis, bar=bar)
                    if value is not None:
                        series.append(value)
                if not series:
                    if contract_summary["status"] == "waiting":
                        contract_summary["status"] = "empty_series"
                    continue
                values_by_contract[contract_id] = series
                contract_summary["series_points"] = len(values_by_contract[contract_id])
                if contract_summary["status"] == "waiting":
                    contract_summary["status"] = "ready"

            # No fresh non-partial bar for this condition: skip evaluate and mark as condition-level no_new_data.
            if not condition_has_new_data:
                condition_summary["status"] = "no_new_data"
                condition_summary["condition_result"] = "NO_NEW_DATA"
                next_monitor_at = self._suggest_next_monitor_at_for_contract_ids(
                    now=now,
                    contract_ids=condition_contract_ids,
                )
                condition_no_new_data_suggestions.append(next_monitor_at)
                if next_monitor_at is not None:
                    condition_summary["suggested_next_monitor_at"] = _to_iso_utc(next_monitor_at)
                condition_states.append(
                    ConditionEvaluationState(
                        condition_id=prepared.condition_id,
                        state="NOT_EVALUATED",
                        last_evaluated_at=now,
                    )
                )
                condition_outcomes.append("NO_NEW_DATA")
                continue

            condition_summary["input_ready"] = True
            conditions_with_input += 1
            has_condition_evaluated = True
            points_by_contract: dict[int, int] = {
                cid: len(series) for cid, series in values_by_contract.items()
            }
            required_points_by_contract = {
                int(contract_req.contract_id): int(contract_req.required_points)
                for contract_req in prepared.requirement.contracts
                if contract_req.contract_id is not None
            }
            condition_result = evaluator.evaluate(
                ConditionEvaluationInput(
                    values_by_contract=values_by_contract,
                    state_values=None,
                )
            )
            self._logger.info(
                "condition evaluate condition_id=%s metric=%s trigger_mode=%s evaluation_window=%s "
                "state=%s reason=%s observed_value=%s points_by_contract=%s required_points_by_contract=%s",
                prepared.condition_id,
                prepared.metric,
                prepared.trigger_mode,
                prepared.evaluation_window,
                condition_result.state,
                condition_result.reason,
                condition_result.observed_value,
                points_by_contract,
                required_points_by_contract,
            )
            condition_summary["condition_result"] = condition_result.state
            condition_summary["status"] = "evaluated"
            condition_summary["reason"] = condition_result.reason

            if condition_result.state == "WAITING":
                # WAITING means input still insufficient; keep last_monitoring_data_end_at unchanged for retry.
                condition_outcomes.append("WAITING")
                condition_states.append(
                    ConditionEvaluationState(
                        condition_id=prepared.condition_id,
                        state="WAITING",
                        last_evaluated_at=now,
                    )
                )
                continue

            if condition_result.state == "TRUE":
                condition_outcomes.append("TRUE")
            else:
                condition_outcomes.append("FALSE")
            condition_states.append(
                ConditionEvaluationState(
                    condition_id=prepared.condition_id,
                    state=condition_result.state,
                    last_value=condition_result.observed_value,
                    last_evaluated_at=now,
                )
            )
            # Only TRUE/FALSE outcomes advance last_monitoring_data_end_at.
            for key, update_ts in condition_monitoring_end_updates.items():
                current = monitoring_end_updates.get(key)
                if current is None or update_ts > current:
                    monitoring_end_updates[key] = update_ts
            if condition_logic == "OR" and condition_result.state == "TRUE":
                # OR strategy is already met, remaining conditions can be skipped.
                condition_summary["short_circuit"] = "or_true_met"
                or_short_circuit_from_index = idx
                break

        if or_short_circuit_from_index is not None:
            for rest_idx, rest_item in enumerate(
                conditions_raw[or_short_circuit_from_index:],
                start=or_short_circuit_from_index + 1,
            ):
                rest_condition = rest_item if isinstance(rest_item, dict) else {}
                rest_condition_id = str(rest_condition.get("condition_id") or f"c{rest_idx}")
                summary_conditions.append(
                    {
                        "condition_id": rest_condition_id,
                        "status": "skipped_or_short_circuit",
                        "contracts": [],
                        "input_ready": False,
                    }
                )
                condition_states.append(
                    ConditionEvaluationState(
                        condition_id=rest_condition_id,
                        state="NOT_EVALUATED",
                    )
                )

        has_waiting = any(item == "WAITING" for item in condition_outcomes)
        any_true = any(item == "TRUE" for item in condition_outcomes)
        any_false = any(item == "FALSE" for item in condition_outcomes)
        all_true = bool(condition_outcomes) and all(item == "TRUE" for item in condition_outcomes)
        all_false = bool(condition_outcomes) and all(item == "FALSE" for item in condition_outcomes)

        strategy_outcome = "no_new_data"
        condition_met = False
        decision_reason = "no_new_data"
        if condition_logic == "AND":
            if any_false:
                strategy_outcome = "evaluated"
                decision_reason = "conditions_not_met"
            elif all_true:
                strategy_outcome = "evaluated"
                condition_met = True
                decision_reason = "conditions_met"
            elif has_waiting:
                strategy_outcome = "waiting_for_market_data"
                decision_reason = "waiting_for_market_data"
        else:
            if any_true:
                strategy_outcome = "evaluated"
                condition_met = True
                decision_reason = "conditions_met"
            elif all_false:
                strategy_outcome = "evaluated"
                decision_reason = "conditions_not_met"
            elif has_waiting:
                strategy_outcome = "waiting_for_market_data"
                decision_reason = "waiting_for_market_data"

        suggested_next_monitor_at: datetime | None = None
        if strategy_outcome == "no_new_data":
            candidates = [item for item in condition_no_new_data_suggestions if item is not None]
            if candidates:
                suggested_next_monitor_at = min(candidates)

        metrics: dict[str, Any] = {
            **default_metrics,
            "condition_logic": condition_logic,
            "conditions": len(condition_states),
            "trigger_policies": trigger_policies,
        }
        if strategy_outcome == "no_new_data":
            metrics["suggested_next_monitor_at"] = (
                _to_iso_utc(suggested_next_monitor_at) if suggested_next_monitor_at is not None else None
            )

        result = StrategyEvaluationResult(
            outcome=strategy_outcome,
            condition_met=condition_met,
            decision_reason=decision_reason,
            metrics=metrics,
            condition_states=condition_states,
        )
        return (
            result,
            {
                "conditions_total": len(conditions_raw),
                "conditions_with_input": conditions_with_input,
                "conditions": summary_conditions,
            },
            monitoring_end_updates,
            has_data_requirements,
            suggested_next_monitor_at,
            has_condition_evaluated,
        )

    def _suggest_next_monitor_at_for_contract_ids(
        self,
        *,
        now: datetime,
        contract_ids: list[int],
    ) -> datetime | None:
        provider = self._market_data_provider
        if provider is None:
            return None
        if not contract_ids:
            return None

        now_utc = _to_utc(now)
        in_any_session = False
        next_start: datetime | None = None
        for contract_id in contract_ids:
            try:
                calendar = provider.get_trading_calendar(
                    TradingCalendarRequest(
                        contract_id=contract_id,
                        as_of_time=now_utc,
                        use_rth=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.debug(
                    "trading calendar fetch failed contract_id=%s error=%s",
                    contract_id,
                    exc,
                )
                continue
            for session in calendar.sessions:
                session_start = _to_utc(session.start_time)
                session_end = _to_utc(session.end_time)
                if session_start <= now_utc < session_end:
                    in_any_session = True
                if session_start > now_utc:
                    if next_start is None or session_start < next_start:
                        next_start = session_start
        if in_any_session:
            return None
        return next_start

    def _resolve_initial_last_monitoring_data_end_at(
        self,
        *,
        strategy_row: sqlite3.Row,
    ) -> datetime | None:
        return (
            _parse_iso_utc(strategy_row["logical_activated_at"])
            or _parse_iso_utc(strategy_row["activated_at"])
        )

    def _load_strategy_run_timing(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
    ) -> tuple[datetime | None, datetime | None]:
        row = conn.execute(
            """
            SELECT suggested_next_monitor_at, updated_at
            FROM strategy_runs
            WHERE strategy_id = ?
            """,
            (strategy_id,),
        ).fetchone()
        if row is None:
            return None, None
        return _parse_iso_utc(row["suggested_next_monitor_at"]), _parse_iso_utc(row["updated_at"])

    def _should_skip_active_monitoring_cycle(
        self,
        *,
        now: datetime,
        suggested_next_monitor_at: datetime | None,
        updated_at: datetime | None,
    ) -> bool:
        if suggested_next_monitor_at is None or updated_at is None:
            return False
        now_utc = _to_utc(now)
        if now_utc >= suggested_next_monitor_at:
            return False
        forced_monitor_at = _to_utc(updated_at) + timedelta(minutes=self._max_monitoring_interval_minutes)
        return now_utc < forced_monitor_at

    def _load_last_monitoring_data_end_map(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
    ) -> dict[str, dict[str, datetime]]:
        row = conn.execute(
            """
            SELECT last_monitoring_data_end_at
            FROM strategy_runs
            WHERE strategy_id = ?
            """,
            (strategy_id,),
        ).fetchone()
        if row is None:
            return {}
        raw = row["last_monitoring_data_end_at"]
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, datetime]] = {}
        for condition_id, by_contract in data.items():
            if not isinstance(condition_id, str) or not isinstance(by_contract, dict):
                continue
            normalized_contracts: dict[str, datetime] = {}
            for contract_id, raw_ts in by_contract.items():
                if not isinstance(contract_id, str):
                    continue
                parsed = _parse_iso_utc(str(raw_ts))
                if parsed is None:
                    continue
                normalized_contracts[contract_id] = parsed
            if normalized_contracts:
                out[condition_id] = normalized_contracts
        return out

    def _resolve_requirement_last_monitoring_data_end_at(
        self,
        *,
        last_monitoring_data_end_map: dict[str, dict[str, datetime]],
        condition_id: str,
        contract_id: int,
        default_last_monitoring_data_end_at: datetime,
    ) -> datetime:
        by_contract = last_monitoring_data_end_map.get(condition_id)
        if by_contract is None:
            return default_last_monitoring_data_end_at
        return by_contract.get(str(contract_id), default_last_monitoring_data_end_at)

    def _handle_active(self, conn: sqlite3.Connection, strategy_row: sqlite3.Row, now: datetime) -> None:
        strategy_id = strategy_row["id"]
        initial_last_monitoring_data_end_at = self._resolve_initial_last_monitoring_data_end_at(
            strategy_row=strategy_row,
        )
        if initial_last_monitoring_data_end_at is None:
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
                    detail="ACTIVE 阶段评估失败：missing_activation_time",
                    ts=now,
                )
            return
        if self._market_data_provider is None:
            raise RuntimeError("ACTIVE stage missing market data provider")
        (
            previous_suggested_next_monitor_at,
            previous_updated_at,
        ) = self._load_strategy_run_timing(
            conn,
            strategy_id=strategy_id,
        )
        if self._should_skip_active_monitoring_cycle(
            now=now,
            suggested_next_monitor_at=previous_suggested_next_monitor_at,
            updated_at=previous_updated_at,
        ):
            self._logger.info(
                "skip active monitoring strategy_id=%s now=%s suggested_next_monitor_at=%s updated_at=%s max_interval_minutes=%s",
                strategy_id,
                _to_iso_utc(now),
                _to_iso_utc(previous_suggested_next_monitor_at) if previous_suggested_next_monitor_at else None,
                _to_iso_utc(previous_updated_at) if previous_updated_at else None,
                self._max_monitoring_interval_minutes,
            )
            return

        (
            result,
            market_data_preparation,
            monitoring_end_updates,
            has_data_requirements,
            suggested_next_monitor_at,
            has_condition_evaluated,
        ) = self._build_condition_inputs_from_market_data(
            conn,
            strategy_row=strategy_row,
            initial_last_monitoring_data_end_at=initial_last_monitoring_data_end_at,
            now=now,
        )
        if not has_data_requirements:
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
                    detail="ACTIVE 阶段评估失败：missing_data_requirements",
                    ts=now,
                )
            return
        evaluated_at_for_store: datetime | None = _utcnow() if has_condition_evaluated else None
        self._logger.info(
            "strategy evaluate strategy_id=%s outcome=%s condition_met=%s decision_reason=%s conditions=%s",
            strategy_id,
            result.outcome,
            result.condition_met,
            result.decision_reason,
            len(result.condition_states),
        )
        if market_data_preparation is not None:
            result.metrics["market_data_preparation"] = market_data_preparation
        persist_evaluation_result(
            conn,
            strategy_id=strategy_id,
            updated_at=now,
            evaluated_at=evaluated_at_for_store,
            initial_last_monitoring_data_end_at=initial_last_monitoring_data_end_at,
            monitoring_end_updates=monitoring_end_updates,
            suggested_next_monitor_at=suggested_next_monitor_at,
            result=result,
        )
        if result.outcome == "no_new_data" and suggested_next_monitor_at is not None:
            prev_iso = (
                _to_iso_utc(previous_suggested_next_monitor_at)
                if previous_suggested_next_monitor_at is not None
                else None
            )
            next_iso = _to_iso_utc(suggested_next_monitor_at)
            if prev_iso != next_iso:
                self._append_event(
                    conn,
                    strategy_id=strategy_id,
                    event_type="MONITOR_SCHEDULED",
                    detail=(
                        "no_new_data 且当前非交易时段，"
                        f"suggested_next_monitor_at: {prev_iso or 'NULL'} -> {next_iso}"
                    ),
                    ts=now,
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
        conn.execute(
            "DELETE FROM strategy_runs WHERE strategy_id = ?",
            (strategy_id,),
        )
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
    market_data_provider = _build_worker_market_data_provider()
    return StrategyExecutionEngine(
        enabled=worker_cfg.enabled,
        monitor_interval_seconds=worker_cfg.monitor_interval_seconds,
        max_monitoring_interval_minutes=worker_cfg.max_monitoring_interval_minutes,
        worker_count=worker_cfg.threads,
        queue_maxsize=worker_cfg.queue_maxsize,
        gateway_not_work_event_throttle_seconds=worker_cfg.gateway_not_work_event_throttle_seconds,
        waiting_for_market_data_event_throttle_seconds=(
            worker_cfg.waiting_for_market_data_event_throttle_seconds
        ),
        market_data_provider=market_data_provider,
    )


def build_execution_engine_from_env() -> StrategyExecutionEngine:
    # Backward-compatible alias; worker settings are now config-file only.
    return build_execution_engine_from_config()


worker_engine = build_execution_engine_from_config()
