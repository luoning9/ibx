from __future__ import annotations

import json
import logging
import queue
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable
from uuid import uuid4

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
from .ib_trade_service import IBTradeService
from .market_data import (
    HistoricalBar,
    HistoricalBarsRequest,
    MarketDataProvider,
    TradingCalendarRequest,
    build_market_data_provider_from_config,
)
from .ib_market_data import IBSessionHistoricalFetcher
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
TRADE_TERMINAL_TO_STRATEGY: dict[str, str] = {
    "FILLED": "FILLED",
    "CANCELLED": "CANCELLED",
    "FAILED": "FAILED",
    "EXPIRED": "EXPIRED",
}
DOWNSTREAM_ACTIVATABLE_STATUSES: set[str] = {"PENDING_ACTIVATION", "VERIFY_FAILED", "PAUSED"}


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


def _to_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_strategy_id(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    return normalized or None


def _safe_positive_quantity(value: Any) -> float:
    try:
        quantity = float(value)
    except (TypeError, ValueError):
        quantity = 0.0
    return quantity if quantity > 0 else 1.0


def _build_instruction_summary(trade_action: dict[str, object]) -> str:
    action_type = str(trade_action.get("action_type", "TRADE")).upper()
    side = str(trade_action.get("side", "")).upper()
    symbol = str(trade_action.get("symbol", "")).upper()
    order_type = str(trade_action.get("order_type", "")).upper()
    qty = trade_action.get("quantity")
    parts = [p for p in [action_type, side, symbol, order_type] if p]
    if qty is not None:
        parts.append(f"qty={qty}")
    return " ".join(parts) if parts else "TRADE_ACTION"


def _strategy_status_from_trade_status(trade_status: str) -> str:
    target_status = TRADE_TERMINAL_TO_STRATEGY.get(str(trade_status or "").upper())
    if target_status is not None:
        return target_status
    return "ORDER_SUBMITTED"


def _activate_downstream_strategy(
    conn: sqlite3.Connection,
    *,
    append_event: Callable[..., None],
    upstream_strategy_id: str,
    next_strategy_id: str | None,
    triggered_at: datetime,
    now: datetime,
) -> bool:
    downstream_id = _normalize_strategy_id(next_strategy_id)
    if downstream_id is None:
        return False

    row = conn.execute(
        """
        SELECT id, status, expire_mode, expire_in_seconds, expire_at
        FROM v_strategies_active
        WHERE id = ?
        """,
        (downstream_id,),
    ).fetchone()
    if row is None:
        return False
    if str(row["status"]) in TERMINAL_STATUSES:
        return False
    if str(row["status"]) not in DOWNSTREAM_ACTIVATABLE_STATUSES:
        return False

    triggered_at_iso = _to_iso_utc(triggered_at)
    expire_at_iso: str | None = str(row["expire_at"] or "").strip() or None
    if row["expire_mode"] == "relative":
        # Relative expiry is derived again after activation verification passes.
        expire_at_iso = None

    cursor = conn.execute(
        """
        UPDATE strategies
        SET status = 'VERIFYING',
            upstream_only_activation = 1,
            activated_at = NULL,
            logical_activated_at = ?,
            expire_at = ?,
            updated_at = ?,
            version = version + 1
        WHERE id = ?
          AND status IN ('PENDING_ACTIVATION', 'VERIFY_FAILED', 'PAUSED')
          AND is_deleted = 0
        """,
        (
            triggered_at_iso,
            expire_at_iso,
            _to_iso_utc(now),
            downstream_id,
        ),
    )
    if cursor.rowcount <= 0:
        return False

    append_event(
        conn,
        strategy_id=downstream_id,
        event_type="VERIFYING",
        detail=f"由上游策略 {upstream_strategy_id} 触发激活校验",
        ts=now,
    )
    append_event(
        conn,
        strategy_id=upstream_strategy_id,
        event_type="DOWNSTREAM_ACTIVATED",
        detail=f"已激活下游策略：{downstream_id}",
        ts=now,
    )
    return True


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


def _metric_observed_value_for_worker(
    *,
    metric: str,
    contract_values: dict[int, float],
    first_contract_id: int,
    second_contract_id: int | None,
) -> float | None:
    metric_key = metric.strip().upper()
    primary = contract_values.get(first_contract_id)
    if primary is None:
        return None
    if metric_key == "PRICE":
        return primary
    if metric_key in {"DRAWDOWN_PCT", "RALLY_PCT"}:
        # Worker market-data path currently does not provide state_values extrema.
        return None
    if second_contract_id is None:
        return None
    secondary = contract_values.get(second_contract_id)
    if secondary is None:
        return None
    if metric_key == "SPREAD":
        return primary - secondary
    if metric_key in {"VOLUME_RATIO", "AMOUNT_RATIO"}:
        if secondary <= 0:
            return None
        return primary / secondary
    return None


def _select_trigger_point(
    *,
    metric: str,
    operator: str,
    trigger_mode: str,
    threshold: float | None,
    effective_window_points: int,
    basis: str,
    require_time_alignment: bool,
    primary_contract_id: int,
    secondary_contract_id: int | None,
    bars_by_contract: dict[int, list[HistoricalBar]],
) -> tuple[float | None, datetime | None]:
    mode = trigger_mode.strip().upper()
    supported_modes = {
        "LEVEL_INSTANT",
        "LEVEL_CONFIRM",
        "CROSS_UP_INSTANT",
        "CROSS_UP_CONFIRM",
        "CROSS_DOWN_INSTANT",
        "CROSS_DOWN_CONFIRM",
    }
    if mode not in supported_modes:
        return None, None
    if threshold is None:
        return None, None

    primary_bars = bars_by_contract.get(primary_contract_id)
    if not primary_bars:
        return None, None
    secondary_bars = (
        bars_by_contract.get(secondary_contract_id)
        if secondary_contract_id is not None
        else None
    )

    observed_points: list[tuple[float, datetime]] = []
    if require_time_alignment and secondary_contract_id is not None and secondary_bars:
        aligned_points = min(len(primary_bars), len(secondary_bars))
        for idx in range(aligned_points):
            primary_bar = primary_bars[-aligned_points + idx]
            secondary_bar = secondary_bars[-aligned_points + idx]
            primary_value = _bar_value_for_metric(metric, basis=basis, bar=primary_bar)
            secondary_value = _bar_value_for_metric(metric, basis=basis, bar=secondary_bar)
            if primary_value is None or secondary_value is None:
                continue
            observed_value = _metric_observed_value_for_worker(
                metric=metric,
                contract_values={
                    primary_contract_id: primary_value,
                    secondary_contract_id: secondary_value,
                },
                first_contract_id=primary_contract_id,
                second_contract_id=secondary_contract_id,
            )
            if observed_value is None:
                continue
            point_at = max(_to_utc(primary_bar.ts), _to_utc(secondary_bar.ts))
            observed_points.append((observed_value, point_at))
    else:
        aligned_points = len(primary_bars)
        if secondary_contract_id is not None and secondary_bars:
            aligned_points = min(aligned_points, len(secondary_bars))
        for idx in range(aligned_points):
            primary_bar = primary_bars[-aligned_points + idx]
            primary_value = _bar_value_for_metric(metric, basis=basis, bar=primary_bar)
            if primary_value is None:
                continue
            contract_values: dict[int, float] = {primary_contract_id: primary_value}
            sample_times: list[datetime] = [_to_utc(primary_bar.ts)]
            if secondary_contract_id is not None and secondary_bars is not None:
                secondary_bar = secondary_bars[-aligned_points + idx]
                secondary_value = _bar_value_for_metric(metric, basis=basis, bar=secondary_bar)
                if secondary_value is None:
                    continue
                contract_values[secondary_contract_id] = secondary_value
                sample_times.append(_to_utc(secondary_bar.ts))
            observed_value = _metric_observed_value_for_worker(
                metric=metric,
                contract_values=contract_values,
                first_contract_id=primary_contract_id,
                second_contract_id=secondary_contract_id,
            )
            if observed_value is None:
                continue
            observed_points.append((observed_value, max(sample_times)))

    if not observed_points:
        return None, None

    if mode.endswith("_CONFIRM"):
        window_points = max(1, int(effective_window_points))
        if len(observed_points) > window_points:
            observed_points = observed_points[-window_points:]
        if not observed_points:
            return None, None

    if mode in {"LEVEL_INSTANT", "LEVEL_CONFIRM"}:
        if operator == ">=":
            candidates = [item for item in observed_points if item[0] >= threshold]
            if not candidates:
                return None, None
            selected_value, selected_bar_at = max(candidates, key=lambda item: item[0])
            return selected_value, selected_bar_at
        if operator == "<=":
            candidates = [item for item in observed_points if item[0] <= threshold]
            if not candidates:
                return None, None
            selected_value, selected_bar_at = min(candidates, key=lambda item: item[0])
            return selected_value, selected_bar_at
        return None, None

    cross_candidates: list[tuple[float, datetime]] = []
    for prev, curr in zip(observed_points, observed_points[1:]):
        if mode in {"CROSS_UP_INSTANT", "CROSS_UP_CONFIRM"}:
            if prev[0] < threshold <= curr[0]:
                cross_candidates.append(curr)
        else:
            if prev[0] > threshold >= curr[0]:
                cross_candidates.append(curr)
    if not cross_candidates:
        return None, None
    if mode in {"CROSS_UP_INSTANT", "CROSS_UP_CONFIRM"}:
        selected_value, selected_bar_at = max(cross_candidates, key=lambda item: item[0])
        return selected_value, selected_bar_at
    selected_value, selected_bar_at = min(cross_candidates, key=lambda item: item[0])
    return selected_value, selected_bar_at


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


def _build_worker_order_service() -> IBTradeService:
    return IBTradeService()


@dataclass(frozen=True)
class StrategyTask:
    strategy_id: str
    reason: str
    expected_status: str
    expected_version: int
    enqueued_at: datetime


@dataclass(frozen=True)
class DispatchingTradeState:
    trade_id: str
    updated_at: datetime | None
    ib_order_id: int | None


@dataclass(frozen=True)
class OrderSubmittedTradeState:
    trade_id: str
    instruction_status: str
    order_status: str
    ib_order_id: int | None
    ib_order_id_raw: str | None
    avg_fill_price: float | None
    filled_qty: float
    error_message: str | None


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
        order_service: IBTradeService | None = None,
        dispatching_reconcile_timeout_seconds: float | None = None,
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
        self._order_service = order_service or _build_worker_order_service()
        timeout_default = float(load_app_config().ib_gateway.timeout_seconds)
        resolved_timeout = (
            timeout_default
            if dispatching_reconcile_timeout_seconds is None
            else float(dispatching_reconcile_timeout_seconds)
        )
        self._dispatching_reconcile_timeout_seconds = max(0.1, resolved_timeout)
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
            with get_connection() as conn:
                seen_statuses: set[str] = set()
                while True:
                    now = _utcnow()
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
                    status_before = str(row["status"])
                    if status_before in TERMINAL_STATUSES:
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
                    status_before = str(latest["status"])
                    if status_before in seen_statuses:
                        self._logger.warning(
                            "stop inline chaining strategy_id=%s reason=%s repeated_status=%s seen=%s",
                            task.strategy_id,
                            task.reason,
                            status_before,
                            sorted(seen_statuses),
                        )
                        return
                    seen_statuses.add(status_before)
                    handler = self._handlers.get(status_before, self._handle_noop)
                    handler(conn, latest, now)
                    conn.commit()

                    refreshed = conn.execute(
                        """
                        SELECT status
                        FROM v_strategies_active
                        WHERE id = ? AND lock_until = ?
                        """,
                        (task.strategy_id, lock_until_iso),
                    ).fetchone()
                    if refreshed is None:
                        return
                    status_after = str(refreshed["status"])
                    if status_after == status_before:
                        return
                    # ACTIVE handler requires market data provider; stop inline chaining
                    # when it is unavailable and wait for the next scheduling cycle.
                    if status_after == "ACTIVE" and self._market_data_provider is None:
                        self._logger.debug(
                            "stop inline chaining strategy_id=%s status=%s (missing market data provider)",
                            task.strategy_id,
                            status_after,
                        )
                        return

                    self._logger.debug(
                        "inline transition strategy_id=%s reason=%s %s->%s seen_count=%s",
                        task.strategy_id,
                        task.reason,
                        status_before,
                        status_after,
                        len(seen_statuses),
                    )
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

    def _find_dispatching_trade_state(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
    ) -> DispatchingTradeState | None:
        row = conn.execute(
            """
            SELECT ti.trade_id AS trade_id,
                   ti.updated_at AS instruction_updated_at,
                   o.updated_at AS order_updated_at,
                   o.ib_order_id AS ib_order_id
            FROM trade_instructions ti
            LEFT JOIN orders o
              ON o.id = ti.trade_id
             AND o.strategy_id = ti.strategy_id
            WHERE ti.strategy_id = ?
              AND ti.status = 'ORDER_DISPATCHING'
            ORDER BY ti.updated_at DESC
            LIMIT 1
            """,
            (strategy_id,),
        ).fetchone()
        if row is not None and row["trade_id"] is not None:
            instruction_updated_at = _parse_iso_utc(row["instruction_updated_at"])
            order_updated_at = _parse_iso_utc(row["order_updated_at"])
            updated_at = instruction_updated_at
            if updated_at is None or (
                order_updated_at is not None and order_updated_at > updated_at
            ):
                updated_at = order_updated_at
            return DispatchingTradeState(
                trade_id=str(row["trade_id"]),
                updated_at=updated_at,
                ib_order_id=_to_int_or_none(row["ib_order_id"]),
            )

        row = conn.execute(
            """
            SELECT id, updated_at, ib_order_id
            FROM orders
            WHERE strategy_id = ?
              AND status = 'ORDER_DISPATCHING'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (strategy_id,),
        ).fetchone()
        if row is not None and row["id"] is not None:
            return DispatchingTradeState(
                trade_id=str(row["id"]),
                updated_at=_parse_iso_utc(row["updated_at"]),
                ib_order_id=_to_int_or_none(row["ib_order_id"]),
            )
        return None

    def _find_order_submitted_trade_state(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
    ) -> OrderSubmittedTradeState | None:
        row = conn.execute(
            """
            SELECT ti.trade_id AS trade_id,
                   ti.status AS instruction_status,
                   o.status AS order_status,
                   o.ib_order_id AS ib_order_id,
                   o.avg_fill_price AS avg_fill_price,
                   o.filled_qty AS filled_qty,
                   o.error_message AS error_message
            FROM trade_instructions ti
            LEFT JOIN orders o
              ON o.id = ti.trade_id
             AND o.strategy_id = ti.strategy_id
            WHERE ti.strategy_id = ?
            ORDER BY ti.updated_at DESC, ti.trade_id DESC
            LIMIT 1
            """,
            (strategy_id,),
        ).fetchone()
        if row is None or row["trade_id"] is None:
            return None

        instruction_status = str(row["instruction_status"] or "ORDER_SUBMITTED").strip().upper()
        order_status = str(row["order_status"] or instruction_status).strip().upper()
        ib_order_id_raw_text = str(row["ib_order_id"] or "").strip()
        ib_order_id_raw = ib_order_id_raw_text or None
        avg_fill_price = _to_float(row["avg_fill_price"], default=0.0)
        if avg_fill_price <= 0:
            avg_fill_price = None
        filled_qty = max(0.0, _to_float(row["filled_qty"], default=0.0))
        error_message = str(row["error_message"] or "").strip() or None
        return OrderSubmittedTradeState(
            trade_id=str(row["trade_id"]),
            instruction_status=instruction_status,
            order_status=order_status,
            ib_order_id=_to_int_or_none(ib_order_id_raw),
            ib_order_id_raw=ib_order_id_raw,
            avg_fill_price=avg_fill_price,
            filled_qty=filled_qty,
            error_message=error_message,
        )

    def _reconcile_dispatching_trade(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
        trade_state: DispatchingTradeState,
        now: datetime,
    ) -> None:
        trade_id = trade_state.trade_id
        snapshot: Any | None = None
        lookup_error: str | None = None
        try:
            if trade_state.ib_order_id is not None:
                snapshot = self._order_service.poll_order_status(perm_id=trade_state.ib_order_id)
            if snapshot is None:
                poll_by_ref = getattr(self._order_service, "poll_order_status_by_order_ref", None)
                if callable(poll_by_ref):
                    snapshot = poll_by_ref(order_ref=trade_id)
        except Exception as exc:  # noqa: BLE001
            lookup_error = str(exc).strip() or exc.__class__.__name__
            self._logger.warning(
                "dispatching trade lookup failed strategy_id=%s trade_id=%s error=%s",
                strategy_id,
                trade_id,
                lookup_error,
            )

        now_iso = _to_iso_utc(now)
        if snapshot is not None:
            recovered_status = str(getattr(snapshot, "normalized_status", "") or "ORDER_SUBMITTED").upper()
            recovered_order_id = _to_int_or_none(getattr(snapshot, "order_id", None))
            recovered_perm_id = _to_int_or_none(getattr(snapshot, "perm_id", None))
            recovered_ib_order_id = (
                str(recovered_perm_id)
                if recovered_perm_id is not None
                else (str(trade_state.ib_order_id) if trade_state.ib_order_id is not None else None)
            )
            recovered_error = str(getattr(snapshot, "error_message", "") or "").strip() or None
            recovered_avg_fill_price = _to_float(getattr(snapshot, "avg_fill_price", None), default=0.0)
            if recovered_avg_fill_price <= 0:
                recovered_avg_fill_price = None
            recovered_filled_qty = _to_float(getattr(snapshot, "filled_qty", 0.0), default=0.0)

            conn.execute(
                """
                UPDATE trade_instructions
                SET status = ?, updated_at = ?
                WHERE trade_id = ? AND strategy_id = ?
                """,
                (recovered_status, now_iso, trade_id, strategy_id),
            )
            conn.execute(
                """
                UPDATE orders
                SET ib_order_id = ?,
                    status = ?,
                    avg_fill_price = ?,
                    filled_qty = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ? AND strategy_id = ?
                """,
                (
                    recovered_ib_order_id,
                    recovered_status,
                    recovered_avg_fill_price,
                    recovered_filled_qty,
                    recovered_error,
                    now_iso,
                    trade_id,
                    strategy_id,
                ),
            )
            detail = (
                f"reconciled ORDER_DISPATCHING trade_id={trade_id} "
                f"order_id={recovered_order_id or '-'} perm_id={recovered_perm_id or '-'} "
                f"status={recovered_status}"
            )
            conn.execute(
                """
                INSERT INTO trade_logs (timestamp, strategy_id, trade_id, stage, result, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now_iso, strategy_id, trade_id, "EXECUTION", recovered_status, detail),
            )
            target_status = _strategy_status_from_trade_status(recovered_status)
            final_cursor = conn.execute(
                """
                UPDATE strategies
                SET status = ?, updated_at = ?, version = version + 1
                WHERE id = ?
                  AND status IN ('TRIGGERED', 'ORDER_SUBMITTED')
                  AND is_deleted = 0
                """,
                (target_status, now_iso, strategy_id),
            )
            if final_cursor.rowcount > 0:
                self._append_event(
                    conn,
                    strategy_id=strategy_id,
                    event_type=target_status,
                    detail=f"恢复挂起交易指令 {trade_id}: {recovered_status}",
                    ts=now,
                )
            return

        dispatch_updated_at = trade_state.updated_at
        elapsed_seconds = 0.0
        if dispatch_updated_at is not None:
            elapsed_seconds = max(0.0, (now - dispatch_updated_at).total_seconds())
        timeout_seconds = float(self._dispatching_reconcile_timeout_seconds)
        if elapsed_seconds < timeout_seconds:
            self._logger.info(
                "dispatching trade not found yet strategy_id=%s trade_id=%s age=%.1fs timeout=%.1fs",
                strategy_id,
                trade_id,
                elapsed_seconds,
                timeout_seconds,
            )
            return

        timeout_reason = (
            f"ORDER_DISPATCHING timeout>{timeout_seconds:.1f}s broker order not found"
            + (f" lookup_error={lookup_error}" if lookup_error else "")
        )
        conn.execute(
            """
            UPDATE trade_instructions
            SET status = 'FAILED', updated_at = ?
            WHERE trade_id = ? AND strategy_id = ?
            """,
            (now_iso, trade_id, strategy_id),
        )
        conn.execute(
            """
            UPDATE orders
            SET status = 'FAILED',
                error_message = ?,
                updated_at = ?
            WHERE id = ? AND strategy_id = ?
            """,
            (timeout_reason, now_iso, trade_id, strategy_id),
        )
        conn.execute(
            """
            INSERT INTO trade_logs (timestamp, strategy_id, trade_id, stage, result, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso,
                strategy_id,
                trade_id,
                "EXECUTION",
                "FAILED",
                f"dispatch timeout failed trade_id={trade_id} reason={timeout_reason}",
            ),
        )
        final_cursor = conn.execute(
            """
            UPDATE strategies
            SET status = 'FAILED', updated_at = ?, version = version + 1
            WHERE id = ?
              AND status IN ('TRIGGERED', 'ORDER_SUBMITTED')
              AND is_deleted = 0
            """,
            (now_iso, strategy_id),
        )
        if final_cursor.rowcount > 0:
            self._append_event(
                conn,
                strategy_id=strategy_id,
                event_type="FAILED",
                detail=f"交易指令 {trade_id} 提交超时：{timeout_reason}",
                ts=now,
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
                            "effective_window_points": int(contract_req.effective_window_points),
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
            condition_latest_bar_by_contract: dict[int, datetime] = {}
            condition_bars_by_contract: dict[int, list[HistoricalBar]] = {}
            condition_has_new_data = False
            condition_contract_ids: list[int] = []
            for contract_index, contract_req in enumerate(prepared.requirement.contracts):
                contract_id = contract_req.contract_id
                contract_summary: dict[str, Any] = {
                    "contract_id": contract_id,
                    "status": "waiting",
                    "effective_window_points": int(contract_req.effective_window_points),
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
                recorded_last_end_at, has_recorded_last_end = self._resolve_requirement_last_monitoring_data_end_at(
                    last_monitoring_data_end_map=last_monitoring_data_end_map,
                    condition_id=prepared.condition_id,
                    contract_id=contract_id,
                    default_last_monitoring_data_end_at=initial_last_monitoring_data_end_at,
                )
                overlap_points = max(0, int(contract_req.effective_window_points) - 1)
                requirement_last_end_at = recorded_last_end_at
                if has_recorded_last_end and overlap_points > 0:
                    requirement_last_end_at = recorded_last_end_at - (bar_delta * overlap_points)
                contract_summary["last_monitoring_data_end_at"] = _to_iso_utc(recorded_last_end_at)
                contract_summary["fetch_anchor_monitoring_end_at"] = _to_iso_utc(requirement_last_end_at)
                contract_summary["overlap_points"] = overlap_points
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
                condition_bars_by_contract[contract_id] = list(bars)
                latest_non_partial_bar = _latest_non_partial_bar_end_time(
                    bars,
                    bar_delta=bar_delta,
                    now=now,
                )
                if latest_non_partial_bar is not None:
                    condition_latest_bar_by_contract[contract_id] = latest_non_partial_bar
                    contract_summary["last_non_partial_bar_at"] = _to_iso_utc(latest_non_partial_bar)
                    if latest_non_partial_bar > recorded_last_end_at:
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
            condition_last_value = condition_result.observed_value
            condition_observed_bar_at: datetime | None = None
            if condition_latest_bar_by_contract:
                if prepared.requirement.require_time_alignment and len(condition_latest_bar_by_contract) > 1:
                    condition_observed_bar_at = min(condition_latest_bar_by_contract.values())
                else:
                    primary_contract_id = (
                        prepared.requirement.contracts[0].contract_id
                        if prepared.requirement.contracts
                        else None
                    )
                    if primary_contract_id is not None:
                        condition_observed_bar_at = condition_latest_bar_by_contract.get(primary_contract_id)
                    if condition_observed_bar_at is None:
                        condition_observed_bar_at = max(condition_latest_bar_by_contract.values())
            if condition_result.state == "TRUE":
                primary_contract_id = (
                    prepared.requirement.contracts[0].contract_id
                    if prepared.requirement.contracts
                    else None
                )
                secondary_contract_id = (
                    prepared.requirement.contracts[1].contract_id
                    if len(prepared.requirement.contracts) > 1
                    else None
                )
                if primary_contract_id is not None:
                    trigger_value, trigger_bar_at = _select_trigger_point(
                        metric=prepared.metric,
                        operator=prepared.operator,
                        trigger_mode=prepared.trigger_mode,
                        threshold=prepared.threshold,
                        effective_window_points=(
                            prepared.requirement.contracts[0].effective_window_points
                            if prepared.requirement.contracts
                            else 1
                        ),
                        basis=basis,
                        require_time_alignment=prepared.requirement.require_time_alignment,
                        primary_contract_id=primary_contract_id,
                        secondary_contract_id=secondary_contract_id,
                        bars_by_contract=condition_bars_by_contract,
                    )
                    if trigger_value is not None:
                        condition_last_value = trigger_value
                    if trigger_bar_at is not None:
                        condition_observed_bar_at = trigger_bar_at
            condition_states.append(
                ConditionEvaluationState(
                    condition_id=prepared.condition_id,
                    state=condition_result.state,
                    last_value=condition_last_value,
                    observed_bar_at=condition_observed_bar_at,
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
    ) -> tuple[datetime, bool]:
        by_contract = last_monitoring_data_end_map.get(condition_id)
        if by_contract is None:
            return default_last_monitoring_data_end_at, False
        resolved = by_contract.get(str(contract_id))
        if resolved is None:
            return default_last_monitoring_data_end_at, False
        return resolved, True

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
            trade_service=self._order_service,
        )
        if verification_result.trade_validation_context is not None:
            context_detail = json.dumps(
                verification_result.trade_validation_context,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._append_event(
                conn,
                strategy_id=strategy_id,
                event_type="VERIFY_TRADE_ACTION_CONTEXT",
                detail=context_detail,
                ts=now,
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
        strategy_id = str(strategy_row["id"])
        trade_action_json = (
            json.loads(strategy_row["trade_action_json"]) if strategy_row["trade_action_json"] else None
        )
        next_strategy_id = _normalize_strategy_id(strategy_row["next_strategy_id"])
        now_iso = _to_iso_utc(now)

        _activate_downstream_strategy(
            conn,
            append_event=self._append_event,
            upstream_strategy_id=strategy_id,
            next_strategy_id=next_strategy_id,
            triggered_at=now,
            now=now,
        )

        if isinstance(trade_action_json, dict):
            dispatching_trade_state = self._find_dispatching_trade_state(conn, strategy_id=strategy_id)
            if dispatching_trade_state is not None:
                self._reconcile_dispatching_trade(
                    conn,
                    strategy_id=strategy_id,
                    trade_state=dispatching_trade_state,
                    now=now,
                )
                return

            trade_action_payload = dict(trade_action_json)
            trade_action_payload["market"] = str(strategy_row["market"] or "").strip().upper()
            if not str(trade_action_payload.get("account_code", "")).strip():
                trade_action_payload["account_code"] = getattr(
                    self._order_service,
                    "default_account_code",
                    None,
                )
            trade_id = f"T-{uuid4().hex[:10].upper()}"
            instruction_summary = _build_instruction_summary(trade_action_payload)
            quantity = _safe_positive_quantity(trade_action_payload.get("quantity"))

            dispatch_payload: dict[str, Any] = {
                "trade_action": trade_action_payload,
                "dispatch": {
                    "order_ref": trade_id,
                    "client_id": getattr(self._order_service, "client_id", None),
                },
            }
            dispatch_detail = f"{instruction_summary} order_ref={trade_id} status=ORDER_DISPATCHING"
            conn.execute(
                """
                INSERT INTO trade_instructions (
                    trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    strategy_id,
                    instruction_summary,
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
                    trade_id,
                    strategy_id,
                    None,
                    "ORDER_DISPATCHING",
                    quantity,
                    None,
                    0.0,
                    None,
                    json.dumps(dispatch_payload, ensure_ascii=False, separators=(",", ":")),
                    now_iso,
                    now_iso,
                ),
            )
            conn.execute(
                """
                INSERT INTO trade_logs (timestamp, strategy_id, trade_id, stage, result, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso,
                    strategy_id,
                    trade_id,
                    "EXECUTION",
                    "ORDER_DISPATCHING",
                    dispatch_detail,
                ),
            )
            self._append_event(
                conn,
                strategy_id=strategy_id,
                event_type="ORDER_DISPATCHING",
                detail=f"开始提交交易指令 {trade_id}",
                ts=now,
            )

            # Persist dispatch marker before touching external broker side effects.
            conn.commit()

            submit_status = "FAILED"
            submit_detail = instruction_summary
            ib_order_id: str | None = None
            avg_fill_price: float | None = None
            filled_qty = 0.0
            error_message: str | None = None
            order_payload: dict[str, Any] = dict(dispatch_payload)
            try:
                submit_result = self._order_service.submit_trade_action(
                    trade_action=trade_action_payload,
                    order_ref=trade_id,
                )
                submit_status = str(submit_result.normalized_status or "ORDER_SUBMITTED").upper()
                ib_order_id = None if submit_result.perm_id is None else str(submit_result.perm_id)
                avg_fill_price = submit_result.avg_fill_price
                filled_qty = float(submit_result.filled_qty)
                quantity = _safe_positive_quantity(submit_result.quantity)
                order_payload = {
                    "trade_action": trade_action_payload,
                    "submit_result": {
                        "order_id": submit_result.order_id,
                        "perm_id": submit_result.perm_id,
                        "status": submit_result.status,
                        "normalized_status": submit_result.normalized_status,
                        "filled_qty": submit_result.filled_qty,
                        "remaining_qty": submit_result.remaining_qty,
                        "avg_fill_price": submit_result.avg_fill_price,
                    },
                }
                submit_detail = (
                    f"{instruction_summary} "
                    f"ib_order_id={submit_result.perm_id or '-'} "
                    f"order_id={submit_result.order_id or '-'} "
                    f"perm_id={submit_result.perm_id or '-'} "
                    f"status={submit_result.normalized_status}"
                )
            except Exception as exc:  # noqa: BLE001
                submit_status = "FAILED"
                error_message = str(exc).strip() or exc.__class__.__name__
                submit_detail = f"{instruction_summary} submit_failed={error_message}"

            finished_at = _utcnow()
            finished_iso = _to_iso_utc(finished_at)
            conn.execute(
                """
                UPDATE trade_instructions
                SET status = ?, updated_at = ?
                WHERE trade_id = ? AND strategy_id = ?
                """,
                (submit_status, finished_iso, trade_id, strategy_id),
            )
            conn.execute(
                """
                UPDATE orders
                SET ib_order_id = ?,
                    status = ?,
                    qty = ?,
                    avg_fill_price = ?,
                    filled_qty = ?,
                    error_message = ?,
                    order_payload_json = ?,
                    updated_at = ?
                WHERE id = ? AND strategy_id = ?
                """,
                (
                    ib_order_id,
                    submit_status,
                    quantity,
                    avg_fill_price,
                    filled_qty,
                    error_message,
                    json.dumps(order_payload, ensure_ascii=False, separators=(",", ":")),
                    finished_iso,
                    trade_id,
                    strategy_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO trade_logs (timestamp, strategy_id, trade_id, stage, result, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    finished_iso,
                    strategy_id,
                    trade_id,
                    "EXECUTION",
                    submit_status,
                    submit_detail,
                ),
            )
            if submit_status != "ORDER_SUBMITTED":
                self._append_event(
                    conn,
                    strategy_id=strategy_id,
                    event_type="ORDER_SUBMITTED_SKIPPED",
                    detail=f"提交交易指令 {trade_id} 直接返回 {submit_status}，跳过 ORDER_SUBMITTED",
                    ts=finished_at,
                )

            target_status = _strategy_status_from_trade_status(submit_status)
            final_cursor = conn.execute(
                """
                UPDATE strategies
                SET status = ?, updated_at = ?, version = version + 1
                WHERE id = ?
                  AND status IN ('TRIGGERED', 'ORDER_SUBMITTED')
                  AND is_deleted = 0
                """,
                (target_status, finished_iso, strategy_id),
            )
            if final_cursor.rowcount > 0:
                self._append_event(
                    conn,
                    strategy_id=strategy_id,
                    event_type=target_status,
                    detail=f"提交交易指令 {trade_id}: {submit_status}",
                    ts=finished_at,
                )
            return

        if next_strategy_id is not None:
            cursor = conn.execute(
                """
                UPDATE strategies
                SET status = 'FILLED', updated_at = ?, version = version + 1
                WHERE id = ? AND status = 'TRIGGERED' AND is_deleted = 0
                """,
                (now_iso, strategy_id),
            )
            if cursor.rowcount > 0:
                self._append_event(
                    conn,
                    strategy_id=strategy_id,
                    event_type="FILLED",
                    detail="无交易动作，完成下游激活后结束",
                    ts=now,
                )
            return

        cursor = conn.execute(
            """
            UPDATE strategies
            SET status = 'FAILED', updated_at = ?, version = version + 1
            WHERE id = ? AND status = 'TRIGGERED' AND is_deleted = 0
            """,
            (now_iso, strategy_id),
        )
        if cursor.rowcount > 0:
            self._append_event(
                conn,
                strategy_id=strategy_id,
                event_type="FAILED",
                detail="TRIGGERED 但无 trade_action_json 且无 next_strategy_id",
                ts=now,
            )

    def _handle_order_submitted(
        self,
        conn: sqlite3.Connection,
        strategy_row: sqlite3.Row,
        now: datetime,
    ) -> None:
        strategy_id = str(strategy_row["id"])
        trade_state = self._find_order_submitted_trade_state(conn, strategy_id=strategy_id)
        if trade_state is None:
            return

        now_iso = _to_iso_utc(now)
        reconciled_status = trade_state.instruction_status
        reconciled_ib_order_id = trade_state.ib_order_id_raw
        reconciled_avg_fill_price = trade_state.avg_fill_price
        reconciled_filled_qty = trade_state.filled_qty
        reconciled_error_message = trade_state.error_message
        snapshot: Any | None = None
        lookup_error: str | None = None
        try:
            if trade_state.ib_order_id is not None:
                snapshot = self._order_service.poll_order_status(perm_id=trade_state.ib_order_id)
            if snapshot is None:
                poll_by_ref = getattr(self._order_service, "poll_order_status_by_order_ref", None)
                if callable(poll_by_ref):
                    snapshot = poll_by_ref(order_ref=trade_state.trade_id)
        except Exception as exc:  # noqa: BLE001
            lookup_error = str(exc).strip() or exc.__class__.__name__
            self._logger.warning(
                "order_submitted poll failed strategy_id=%s trade_id=%s error=%s",
                strategy_id,
                trade_state.trade_id,
                lookup_error,
            )

        if snapshot is not None:
            reconciled_status = str(getattr(snapshot, "normalized_status", "") or "ORDER_SUBMITTED").upper()
            perm_id = _to_int_or_none(getattr(snapshot, "perm_id", None))
            if perm_id is not None:
                reconciled_ib_order_id = str(perm_id)
            reconciled_avg_fill_price = _to_float(getattr(snapshot, "avg_fill_price", None), default=0.0)
            if reconciled_avg_fill_price <= 0:
                reconciled_avg_fill_price = None
            reconciled_filled_qty = max(0.0, _to_float(getattr(snapshot, "filled_qty", 0.0), default=0.0))
            reconciled_error_message = str(getattr(snapshot, "error_message", "") or "").strip() or None

        instruction_changed = reconciled_status != trade_state.instruction_status
        order_changed = (
            reconciled_status != trade_state.order_status
            or reconciled_ib_order_id != trade_state.ib_order_id_raw
            or reconciled_avg_fill_price != trade_state.avg_fill_price
            or abs(reconciled_filled_qty - trade_state.filled_qty) > 1e-9
            or reconciled_error_message != trade_state.error_message
        )
        if instruction_changed:
            conn.execute(
                """
                UPDATE trade_instructions
                SET status = ?, updated_at = ?
                WHERE trade_id = ? AND strategy_id = ?
                """,
                (reconciled_status, now_iso, trade_state.trade_id, strategy_id),
            )
        if order_changed:
            conn.execute(
                """
                UPDATE orders
                SET ib_order_id = ?,
                    status = ?,
                    avg_fill_price = ?,
                    filled_qty = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ? AND strategy_id = ?
                """,
                (
                    reconciled_ib_order_id,
                    reconciled_status,
                    reconciled_avg_fill_price,
                    reconciled_filled_qty,
                    reconciled_error_message,
                    now_iso,
                    trade_state.trade_id,
                    strategy_id,
                ),
            )
        if snapshot is not None and (instruction_changed or order_changed):
            recovered_order_id = _to_int_or_none(getattr(snapshot, "order_id", None))
            recovered_perm_id = _to_int_or_none(getattr(snapshot, "perm_id", None))
            detail = (
                f"polled ORDER_SUBMITTED trade_id={trade_state.trade_id} "
                f"order_id={recovered_order_id or '-'} perm_id={recovered_perm_id or '-'} "
                f"status={reconciled_status}"
            )
            conn.execute(
                """
                INSERT INTO trade_logs (timestamp, strategy_id, trade_id, stage, result, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now_iso, strategy_id, trade_state.trade_id, "EXECUTION", reconciled_status, detail),
            )

        target_status = _strategy_status_from_trade_status(reconciled_status)
        if target_status == "ORDER_SUBMITTED":
            return

        final_cursor = conn.execute(
            """
            UPDATE strategies
            SET status = ?, updated_at = ?, version = version + 1
            WHERE id = ? AND status = 'ORDER_SUBMITTED' AND is_deleted = 0
            """,
            (target_status, now_iso, strategy_id),
        )
        if final_cursor.rowcount > 0:
            detail = f"订单状态同步 {trade_state.trade_id}: {reconciled_status}"
            if lookup_error:
                detail = f"{detail} (poll_error={lookup_error})"
            self._append_event(
                conn,
                strategy_id=strategy_id,
                event_type=target_status,
                detail=detail,
                ts=now,
            )

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
