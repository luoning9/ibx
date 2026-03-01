from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from .config import load_app_config
from .db import get_connection, init_db
from .broker_provider_registry import (
    get_broker_data_provider,
    close_broker_data_runtime,
)
from .ib_data_service import (
    AccountSnapshot,
    IBDataServiceError,
)
from .ib_trade_service import IBTradeService, IBTradeServiceError, OrderStatusSnapshot
from .market_config import resolve_market_profile
from .models import (
    ActiveTradeInstructionOut,
    Capabilities,
    CapabilityReasons,
    ConditionItem,
    ConditionRuntimeItem,
    ControlResponse,
    EventLogItem,
    NextStrategyProjection,
    PortfolioSummaryOut,
    PositionItemOut,
    StrategyActionsPutIn,
    StrategyBasicPatchIn,
    StrategyConditionsPutIn,
    StrategyCreateIn,
    StrategyDescriptionOut,
    StrategyDetailOut,
    StrategyRunSummaryOut,
    StrategyStatus,
    StrategySummaryOut,
    StrategySymbolItem,
    OtherOpenOrderOut,
    OpenOrderCancelOut,
    TradeOrderLegOut,
    TradeOrderOut,
    TradeRecoveryIn,
    TradeRecoveryOut,
    TradeActionRuntime,
    TradeLogOut,
    TriggerGroupStatus,
    _validate_trade_symbol_combo,
)
from .strategy_description import generate_strategy_description

TERMINAL_STATUSES: set[str] = {"FILLED", "EXPIRED", "CANCELLED", "FAILED"}
TRADE_INSTRUCTION_TERMINAL_STATUSES: tuple[str, ...] = ("FILLED", "CANCELLED", "FAILED", "EXPIRED")
CANCEL_OTHER_OPEN_ORDER_WAIT_TIMEOUT_SECONDS = 5.0
EDITABLE_STATUSES: set[str] = {"PENDING_ACTIVATION", "PAUSED", "VERIFY_FAILED"}
STOCK_TRADE_TYPES: set[str] = {"buy", "sell", "switch"}
ACTIVE_STRATEGIES_SOURCE = "v_strategies_active"
RUNTIME_KEY_PAUSED_FROM_STATUS = "paused_from_status"
STRATEGY_LOCKED_ERROR_CODE = "STRATEGY_LOCKED"
_LOGGER = logging.getLogger("ibx.store")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def dumps_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _generate_strategy_id(conn: sqlite3.Connection) -> str:
    for _ in range(64):
        candidate = f"S-{uuid4().hex[:4].upper()}"
        hit = conn.execute("SELECT 1 FROM strategies WHERE id = ?", (candidate,)).fetchone()
        if hit is None:
            return candidate
    raise HTTPException(status_code=500, detail="failed to allocate strategy id")


def _generate_condition_nl(condition: ConditionItem) -> str:
    if condition.condition_type == "SINGLE_PRODUCT":
        subject = condition.product or "标的"
    else:
        subject = f"{condition.product or 'A'} / {condition.product_b or 'B'}"
    return (
        f"当 {subject} 的 {condition.metric} 在 {condition.evaluation_window} 窗口满足 "
        f"{condition.trigger_mode} {condition.operator} {condition.value} 时触发。"
    )


def _is_stock_trade_type(trade_type: str) -> bool:
    return trade_type in STOCK_TRADE_TYPES


def _validate_trade_action_compatibility(
    trade_type: str, trade_action_json: dict[str, Any] | None
) -> None:
    if not trade_action_json:
        return
    action_type = str(trade_action_json.get("action_type", "")).upper()
    if _is_stock_trade_type(trade_type):
        if action_type != "STOCK_TRADE":
            raise HTTPException(
                status_code=422,
                detail=f"trade_type={trade_type} only allows action_type=STOCK_TRADE",
            )
        return
    if action_type not in {"FUT_POSITION", "FUT_ROLL"}:
        raise HTTPException(
            status_code=422,
            detail=f"trade_type={trade_type} only allows action_type in FUT_POSITION/FUT_ROLL",
        )


def _resolve_trade_action_account_code() -> str | None:
    return _normalize_optional_text(load_app_config().ib_gateway.account_code)


def _enrich_trade_action_with_strategy_context(
    *,
    trade_action_json: dict[str, Any] | None,
    market: str,
    account_code: str | None,
) -> dict[str, Any] | None:
    if trade_action_json is None:
        return None
    enriched = dict(trade_action_json)
    enriched["market"] = str(market or "").strip().upper()
    enriched["account_code"] = _normalize_optional_text(account_code)
    return enriched


def _normalize_strategy_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    return normalized or None


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _to_int_including_zero_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_change_value_for_event(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    if not text:
        return '""'
    if len(text) > 120:
        return f"{text[:117]}..."
    return text


def _format_symbols_for_event(symbols: list[StrategySymbolItem]) -> str:
    if len(symbols) == 0:
        return "[]"
    parts: list[str] = []
    for symbol in symbols:
        trade_type = str(symbol.trade_type or "").strip().lower() or "-"
        code = str(symbol.code or "").strip().upper() or "-"
        contract = _normalize_optional_text(symbol.contract_id)
        if contract:
            parts.append(f"{trade_type}:{code}({contract})")
        else:
            parts.append(f"{trade_type}:{code}")
    return ",".join(parts)


def _build_basic_update_event_detail(
    *,
    old_description: str,
    new_description: str,
    old_market: str,
    new_market: str,
    old_trade_type: str,
    new_trade_type: str,
    old_symbols: list[StrategySymbolItem],
    new_symbols: list[StrategySymbolItem],
    old_upstream_only_activation: bool,
    new_upstream_only_activation: bool,
    old_logical_activated_at: str | None,
    new_logical_activated_at: str | None,
    old_expire_mode: str,
    new_expire_mode: str,
    old_expire_in_seconds: int | None,
    new_expire_in_seconds: int | None,
    old_expire_at: str | None,
    new_expire_at: str | None,
) -> str:
    changes: list[str] = []

    def add_change(field_name: str, old_value: Any, new_value: Any) -> None:
        if old_value == new_value:
            return
        changes.append(
            f"{field_name}: {_format_change_value_for_event(old_value)} -> {_format_change_value_for_event(new_value)}"
        )

    add_change("description", old_description, new_description)
    add_change("market", old_market, new_market)
    add_change("trade_type", old_trade_type, new_trade_type)
    add_change("symbols", _format_symbols_for_event(old_symbols), _format_symbols_for_event(new_symbols))
    add_change("upstream_only_activation", old_upstream_only_activation, new_upstream_only_activation)
    add_change("logical_activated_at", old_logical_activated_at, new_logical_activated_at)
    add_change("expire_mode", old_expire_mode, new_expire_mode)
    add_change("expire_in_seconds", old_expire_in_seconds, new_expire_in_seconds)
    add_change("expire_at", old_expire_at, new_expire_at)

    if len(changes) == 0:
        return "已更新基本信息（未检测到字段变化）"
    return f"已更新基本信息：{'；'.join(changes)}"


def _editable(status: str) -> tuple[bool, str | None]:
    if status in EDITABLE_STATUSES:
        return True, None
    return False, "仅 PENDING_ACTIVATION / PAUSED / VERIFY_FAILED 可编辑；ACTIVE 请先暂停。"


def _raise_if_strategy_locked(*, row: sqlite3.Row, now: datetime, action: str) -> None:
    lock_until = parse_iso(row["lock_until"])
    if lock_until is None or lock_until <= now:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": STRATEGY_LOCKED_ERROR_CODE,
            "message": "strategy is locked by worker",
            "action": action,
            "lock_until": to_iso(lock_until),
        },
    )


def _capabilities(
    *,
    status: str,
    upstream_only_activation: bool,
    has_conditions: bool,
    has_actions: bool,
    has_active_trade_instruction: bool,
    has_upstream_strategy: bool,
) -> tuple[Capabilities, CapabilityReasons]:
    can_activate = status in {"PENDING_ACTIVATION", "VERIFY_FAILED"}
    activate_reason = None
    if upstream_only_activation:
        can_activate = False
        activate_reason = "upstream_only_activation=true"
    elif not has_conditions:
        can_activate = False
        activate_reason = "触发条件未配置"
    elif not has_actions:
        can_activate = False
        activate_reason = "后续动作未配置"

    can_pause = status in {"ACTIVE", "VERIFYING"}
    can_resume = status == "PAUSED"
    can_cancel = status not in TERMINAL_STATUSES
    can_delete, delete_reason = _delete_capability(
        status=status,
        has_active_trade_instruction=has_active_trade_instruction,
        has_upstream_strategy=has_upstream_strategy,
    )

    caps = Capabilities(
        can_activate=can_activate,
        can_pause=can_pause,
        can_resume=can_resume,
        can_cancel=can_cancel,
        can_delete=can_delete,
    )
    reasons = CapabilityReasons(
        can_activate=activate_reason,
        can_pause=None if can_pause else "仅 ACTIVE / VERIFYING 可暂停",
        can_resume=None if can_resume else "仅 PAUSED 可恢复",
        can_cancel=None if can_cancel else "终态策略不可取消",
        can_delete=delete_reason,
    )
    return caps, reasons


def _delete_capability(
    *,
    status: str,
    has_active_trade_instruction: bool,
    has_upstream_strategy: bool,
) -> tuple[bool, str | None]:
    if status == "ACTIVE":
        return False, "ACTIVE 状态不可删除"
    if status == "PAUSED":
        return False, "PAUSED 状态不可删除"
    if has_upstream_strategy:
        return False, "存在上游策略，不可删除"
    if has_active_trade_instruction:
        return False, "交易未终止，不可删除"
    return True, None


def _trigger_group_status(status: str, has_conditions: bool) -> TriggerGroupStatus:
    if not has_conditions:
        return "NOT_CONFIGURED"
    if status == "EXPIRED":
        return "EXPIRED"
    if status in {"TRIGGERED", "ORDER_SUBMITTED", "FILLED"}:
        return "TRIGGERED"
    return "MONITORING"


def _strategy_status_from_trade_status(trade_status: str) -> StrategyStatus:
    normalized = str(trade_status or "").strip().upper()
    if normalized == "FILLED":
        return "FILLED"
    if normalized == "CANCELLED":
        return "CANCELLED"
    if normalized == "FAILED":
        return "FAILED"
    if normalized == "EXPIRED":
        return "EXPIRED"
    return "ORDER_SUBMITTED"


class SQLiteStore:
    def __init__(self) -> None:
        self._lock = Lock()
        init_db()

    def _conn(self) -> sqlite3.Connection:
        return get_connection()

    def _load_broker_snapshot(self) -> AccountSnapshot:
        provider = get_broker_data_provider()
        try:
            return provider.get_account_snapshot()
        except (IBDataServiceError, ValueError, RuntimeError, TimeoutError) as exc:
            _LOGGER.exception("Failed to load broker snapshot from provider=%s", provider.__class__.__name__)
            detail = str(exc).strip()
            if not detail:
                detail = exc.__class__.__name__
            raise HTTPException(status_code=502, detail=f"broker_data unavailable: {detail}") from exc
        except Exception as exc:
            _LOGGER.exception("Unexpected failure while loading broker snapshot provider=%s", provider.__class__.__name__)
            detail = str(exc).strip()
            if not detail:
                detail = exc.__class__.__name__
            raise HTTPException(status_code=502, detail=f"broker_data unavailable: {detail}") from exc

    def _get_strategy_row(
        self,
        conn: sqlite3.Connection,
        strategy_id: str,
        *,
        include_deleted: bool = False,
    ) -> sqlite3.Row:
        source = "strategies" if include_deleted else ACTIVE_STRATEGIES_SOURCE
        row = conn.execute(f"SELECT * FROM {source} WHERE id = ?", (strategy_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"strategy {strategy_id} not found")
        return row

    def _validate_next_strategy_target(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
        next_strategy_id: str | None,
    ) -> str | None:
        normalized = _normalize_strategy_id(next_strategy_id)
        if normalized is None:
            return None
        if normalized == strategy_id:
            raise HTTPException(status_code=422, detail="next_strategy_id cannot be self")

        target = conn.execute(
            f"SELECT id, upstream_strategy_id FROM {ACTIVE_STRATEGIES_SOURCE} WHERE id = ?",
            (normalized,),
        ).fetchone()
        if target is None:
            raise HTTPException(status_code=422, detail=f"next_strategy_id {normalized} not found")

        target_upstream = _normalize_strategy_id(target["upstream_strategy_id"])
        if target_upstream and target_upstream != strategy_id:
            raise HTTPException(
                status_code=422,
                detail=f"strategy {normalized} already has upstream {target_upstream}",
            )

        conflict = conn.execute(
            """
            SELECT id FROM v_strategies_active
            WHERE next_strategy_id = ? AND id <> ?
            LIMIT 1
            """,
            (normalized, strategy_id),
        ).fetchone()
        if conflict is not None:
            raise HTTPException(
                status_code=422,
                detail=f"strategy {normalized} already linked by upstream {conflict['id']}",
            )

        return normalized

    def _sync_downstream_upstream_link(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
        old_next_strategy_id: str | None,
        new_next_strategy_id: str | None,
        now: datetime,
    ) -> None:
        old_next = _normalize_strategy_id(old_next_strategy_id)
        new_next = _normalize_strategy_id(new_next_strategy_id)
        now_iso = to_iso(now)

        if old_next and old_next != new_next:
            conn.execute(
                """
                UPDATE strategies
                SET upstream_strategy_id = NULL, updated_at = ?, version = version + 1
                WHERE id = ? AND upstream_strategy_id = ?
                """,
                (now_iso, old_next, strategy_id),
            )

        if new_next:
            conn.execute(
                """
                UPDATE strategies
                SET upstream_strategy_id = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND (upstream_strategy_id IS NULL OR upstream_strategy_id <> ?)
                """,
                (strategy_id, now_iso, new_next, strategy_id),
            )

    def _load_symbols(self, conn: sqlite3.Connection, strategy_id: str) -> list[StrategySymbolItem]:
        rows = conn.execute(
            """
            SELECT code, trade_type, contract_id
            FROM strategy_symbols
            WHERE strategy_id = ?
            ORDER BY position ASC
            """,
            (strategy_id,),
        ).fetchall()
        return [
            StrategySymbolItem(
                code=r["code"],
                trade_type=r["trade_type"],
                contract_id=r["contract_id"],
            )
            for r in rows
        ]

    def _load_conditions(self, row: sqlite3.Row) -> list[ConditionItem]:
        raw = row["conditions_json"] or "[]"
        data = json.loads(raw)
        return [ConditionItem.model_validate(item) for item in data]

    def _load_conditions_runtime(
        self,
        conn: sqlite3.Connection,
        strategy_id: str,
        conditions: list[ConditionItem],
    ) -> list[ConditionRuntimeItem]:
        rows = conn.execute(
            """
            SELECT condition_id, state, last_value, last_evaluated_at
            FROM condition_states
            WHERE strategy_id = ?
            """,
            (strategy_id,),
        ).fetchall()
        by_id = {r["condition_id"]: r for r in rows}
        runtime: list[ConditionRuntimeItem] = []
        for cond in conditions:
            cid = cond.condition_id or ""
            hit = by_id.get(cid)
            if hit:
                runtime.append(
                    ConditionRuntimeItem(
                        condition_id=cid,
                        state=hit["state"],
                        last_value=hit["last_value"],
                        last_evaluated_at=parse_iso(hit["last_evaluated_at"]),
                    )
                )
            else:
                runtime.append(ConditionRuntimeItem(condition_id=cid, state="NOT_EVALUATED"))
        return runtime

    def _load_trade_action_runtime(
        self,
        conn: sqlite3.Connection,
        strategy_id: str,
        has_trade_action: bool,
    ) -> TradeActionRuntime:
        if not has_trade_action:
            return TradeActionRuntime(trade_status="NOT_SET")

        row = conn.execute(
            """
            SELECT trade_id, status
            FROM trade_instructions
            WHERE strategy_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (strategy_id,),
        ).fetchone()
        if row:
            return TradeActionRuntime(trade_status=row["status"], trade_id=row["trade_id"])

        row = conn.execute(
            """
            SELECT COALESCE(NULLIF(trade_id, ''), id) AS resolved_trade_id, status, error_message
            FROM orders
            WHERE strategy_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (strategy_id,),
        ).fetchone()
        if row:
            return TradeActionRuntime(
                trade_status=row["status"],
                trade_id=row["resolved_trade_id"],
                last_error=row["error_message"],
            )
        return TradeActionRuntime(trade_status="NOT_TRIGGERED")

    def _has_active_trade_instruction(self, conn: sqlite3.Connection, strategy_id: str) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM trade_instructions
            WHERE strategy_id = ?
              AND status NOT IN (?, ?, ?, ?)
            LIMIT 1
            """,
            (strategy_id, *TRADE_INSTRUCTION_TERMINAL_STATUSES),
        ).fetchone()
        return row is not None

    def _load_next_strategy(
        self,
        conn: sqlite3.Connection,
        next_strategy_id: str | None,
        next_strategy_note: str | None,
    ) -> NextStrategyProjection | None:
        if not next_strategy_id:
            return None
        row = conn.execute(
            f"SELECT id, description, status FROM {ACTIVE_STRATEGIES_SOURCE} WHERE id = ?",
            (next_strategy_id,),
        ).fetchone()
        if row:
            return NextStrategyProjection(
                id=row["id"],
                description=row["description"],
                status=row["status"],
            )
        return NextStrategyProjection(
            id=next_strategy_id,
            description=next_strategy_note,
            status="UNKNOWN",
        )

    def _load_upstream_strategy(
        self,
        conn: sqlite3.Connection,
        upstream_strategy_id: str | None,
    ) -> NextStrategyProjection | None:
        if not upstream_strategy_id:
            return None
        row = conn.execute(
            f"SELECT id, description, status FROM {ACTIVE_STRATEGIES_SOURCE} WHERE id = ?",
            (upstream_strategy_id,),
        ).fetchone()
        if row:
            return NextStrategyProjection(
                id=row["id"],
                description=row["description"],
                status=row["status"],
            )
        return NextStrategyProjection(id=upstream_strategy_id, status="UNKNOWN")

    def _load_events(self, conn: sqlite3.Connection, strategy_id: str) -> list[EventLogItem]:
        rows = conn.execute(
            """
            SELECT timestamp, event_type, detail, strategy_id
            FROM strategy_events
            WHERE strategy_id = ?
            ORDER BY timestamp DESC, id DESC
            """,
            (strategy_id,),
        ).fetchall()
        return [
            EventLogItem(
                timestamp=parse_iso(r["timestamp"]) or utcnow(),
                event_type=r["event_type"],
                detail=r["detail"],
                strategy_id=r["strategy_id"],
            )
            for r in rows
        ]

    def _load_strategy_run_summary(
        self,
        conn: sqlite3.Connection,
        strategy_id: str,
    ) -> StrategyRunSummaryOut | None:
        # strategy_runs is optional at rest; it appears after runtime evaluation creates it.
        row = conn.execute(
            """
            SELECT
                first_evaluated_at,
                evaluated_at,
                suggested_next_monitor_at,
                condition_met,
                decision_reason,
                last_outcome,
                check_count,
                last_monitoring_data_end_at,
                updated_at
            FROM strategy_runs
            WHERE strategy_id = ?
            """,
            (strategy_id,),
        ).fetchone()
        if row is None:
            return None

        raw_monitoring_end = row["last_monitoring_data_end_at"]
        monitoring_end_map: dict[str, dict[str, str]] = {}
        if isinstance(raw_monitoring_end, str) and raw_monitoring_end.strip():
            try:
                # Keep detail API resilient even if historical rows contain malformed JSON.
                payload = json.loads(raw_monitoring_end)
                if isinstance(payload, dict):
                    monitoring_end_map = payload
            except json.JSONDecodeError:
                monitoring_end_map = {}

        return StrategyRunSummaryOut(
            first_evaluated_at=parse_iso(row["first_evaluated_at"]) or utcnow(),
            evaluated_at=parse_iso(row["evaluated_at"]) or utcnow(),
            suggested_next_monitor_at=parse_iso(row["suggested_next_monitor_at"]),
            condition_met=bool(row["condition_met"]),
            decision_reason=row["decision_reason"],
            last_outcome=row["last_outcome"],
            check_count=int(row["check_count"]),
            last_monitoring_data_end_at=monitoring_end_map,
            updated_at=parse_iso(row["updated_at"]) or utcnow(),
        )

    def _effective_expire_at(self, row: sqlite3.Row) -> datetime | None:
        explicit = parse_iso(row["expire_at"])
        if explicit is not None:
            return explicit

        if row["expire_mode"] != "relative":
            return None
        if not row["expire_in_seconds"]:
            return None

        base = parse_iso(row["logical_activated_at"]) or parse_iso(row["activated_at"])
        if base is None:
            return None
        return base + timedelta(seconds=int(row["expire_in_seconds"]))

    def _to_detail(self, conn: sqlite3.Connection, row: sqlite3.Row) -> StrategyDetailOut:
        strategy_id = row["id"]
        symbols = self._load_symbols(conn, strategy_id)
        conditions = self._load_conditions(row)
        conditions_runtime = self._load_conditions_runtime(conn, strategy_id, conditions)

        trade_action_json = (
            json.loads(row["trade_action_json"]) if row["trade_action_json"] is not None else None
        )
        has_conditions = len(conditions) > 0
        has_actions = trade_action_json is not None or row["next_strategy_id"] is not None

        editable, editable_reason = _editable(row["status"])
        capabilities, capability_reasons = _capabilities(
            status=row["status"],
            upstream_only_activation=bool(row["upstream_only_activation"]),
            has_conditions=has_conditions,
            has_actions=has_actions,
            has_active_trade_instruction=self._has_active_trade_instruction(conn, strategy_id),
            has_upstream_strategy=_normalize_strategy_id(row["upstream_strategy_id"]) is not None,
        )

        return StrategyDetailOut(
            id=row["id"],
            description=row["description"],
            market=row["market"],
            sec_type=row["sec_type"],
            exchange=row["exchange"],
            trade_type=row["trade_type"],
            symbols=symbols,
            upstream_only_activation=bool(row["upstream_only_activation"]),
            activated_at=parse_iso(row["activated_at"]),
            logical_activated_at=parse_iso(row["logical_activated_at"]),
            expire_in_seconds=row["expire_in_seconds"],
            expire_at=self._effective_expire_at(row),
            status=row["status"],
            editable=editable,
            editable_reason=editable_reason,
            capabilities=capabilities,
            capability_reasons=capability_reasons,
            condition_logic=row["condition_logic"],
            conditions_json=conditions,
            trigger_group_status=_trigger_group_status(row["status"], has_conditions),
            conditions_runtime=conditions_runtime,
            trade_action_json=trade_action_json,
            trade_action_runtime=self._load_trade_action_runtime(
                conn, strategy_id=strategy_id, has_trade_action=trade_action_json is not None
            ),
            next_strategy=self._load_next_strategy(
                conn,
                next_strategy_id=row["next_strategy_id"],
                next_strategy_note=row["next_strategy_note"],
            ),
            upstream_strategy=self._load_upstream_strategy(
                conn,
                upstream_strategy_id=row["upstream_strategy_id"],
            ),
            strategy_run=self._load_strategy_run_summary(conn, strategy_id),
            anchor_price=row["anchor_price"],
            events=self._load_events(conn, strategy_id),
            created_at=parse_iso(row["created_at"]) or utcnow(),
            updated_at=parse_iso(row["updated_at"]) or utcnow(),
        )

    def _append_event(
        self,
        conn: sqlite3.Connection,
        strategy_id: str,
        event_type: str,
        detail: str,
        ts: datetime | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO strategy_events (strategy_id, timestamp, event_type, detail)
            VALUES (?, ?, ?, ?)
            """,
            (strategy_id, to_iso(ts or utcnow()), event_type, detail),
        )

    def _get_runtime_state(self, conn: sqlite3.Connection, strategy_id: str, state_key: str) -> str | None:
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
        state_value = row["state_value"]
        if state_value is None:
            return None
        normalized = str(state_value).strip()
        return normalized or None

    def _set_runtime_state(
        self,
        conn: sqlite3.Connection,
        strategy_id: str,
        state_key: str,
        state_value: str,
        now: datetime,
    ) -> None:
        conn.execute(
            """
            INSERT INTO strategy_runtime_states (strategy_id, state_key, state_value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(strategy_id, state_key) DO UPDATE SET
                state_value = excluded.state_value,
                updated_at = excluded.updated_at
            """,
            (strategy_id, state_key, state_value, to_iso(now)),
        )

    def _delete_runtime_state(self, conn: sqlite3.Connection, strategy_id: str, state_key: str) -> None:
        conn.execute(
            """
            DELETE FROM strategy_runtime_states
            WHERE strategy_id = ? AND state_key = ?
            """,
            (strategy_id, state_key),
        )

    def _reset_to_pending_after_config_change(
        self, conn: sqlite3.Connection, strategy_id: str, now: datetime
    ) -> None:
        row = self._get_strategy_row(conn, strategy_id)
        if row["status"] == "PENDING_ACTIVATION":
            return
        conn.execute(
            """
            UPDATE strategies
            SET status = 'PENDING_ACTIVATION',
                activated_at = NULL,
                updated_at = ?,
                version = version + 1
            WHERE id = ?
            """,
            (to_iso(now), strategy_id),
        )
        self._append_event(conn, strategy_id, "RESET_TO_PENDING", "配置变更，状态重置为待激活", now)

    def list_strategies(self) -> list[StrategySummaryOut]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, status, description, updated_at, expire_at, expire_mode, expire_in_seconds,
                       activated_at, logical_activated_at,
                       upstream_only_activation, conditions_json, trade_action_json, next_strategy_id,
                       upstream_strategy_id
                FROM v_strategies_active
                ORDER BY updated_at DESC, id ASC
                """
            ).fetchall()
            active_trade_instruction_ids = {
                item["strategy_id"]
                for item in conn.execute(
                    """
                    SELECT DISTINCT strategy_id
                    FROM trade_instructions
                    WHERE status NOT IN (?, ?, ?, ?)
                    """,
                    TRADE_INSTRUCTION_TERMINAL_STATUSES,
                ).fetchall()
            }
            out: list[StrategySummaryOut] = []
            for row in rows:
                conditions = json.loads(row["conditions_json"] or "[]")
                has_conditions = len(conditions) > 0
                has_actions = row["trade_action_json"] is not None or row["next_strategy_id"] is not None
                caps, _ = _capabilities(
                    status=row["status"],
                    upstream_only_activation=bool(row["upstream_only_activation"]),
                    has_conditions=has_conditions,
                    has_actions=has_actions,
                    has_active_trade_instruction=row["id"] in active_trade_instruction_ids,
                    has_upstream_strategy=_normalize_strategy_id(row["upstream_strategy_id"]) is not None,
                )
                out.append(
                    StrategySummaryOut(
                        id=row["id"],
                        status=row["status"],
                        description=row["description"],
                        updated_at=parse_iso(row["updated_at"]) or utcnow(),
                        expire_at=self._effective_expire_at(row),
                        upstream_strategy_id=_normalize_strategy_id(row["upstream_strategy_id"]),
                        capabilities=caps,
                    )
                )
            return out

    def get_strategy(self, strategy_id: str) -> StrategyDetailOut:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
            return self._to_detail(conn, row)

    def generate_strategy_description_by_id(self, strategy_id: str) -> StrategyDescriptionOut:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
            symbols = self._load_symbols(conn, strategy_id)
            expire_mode = str(row["expire_mode"] or "relative").strip().lower()
            if expire_mode not in {"relative", "absolute"}:
                expire_mode = "relative"
            expire_in_seconds = (
                int(row["expire_in_seconds"])
                if row["expire_in_seconds"] is not None
                else None
            )
            conditions = self._load_conditions(row)
            trade_action_json = (
                json.loads(row["trade_action_json"])
                if row["trade_action_json"] is not None
                else None
            )
            description = generate_strategy_description(
                market=str(row["market"] or "").strip().upper(),
                trade_type=str(row["trade_type"] or "buy").strip().lower(),
                symbols=symbols,
                conditions=conditions,
                trade_action_json=trade_action_json,
                upstream_only_activation=bool(row["upstream_only_activation"]),
                expire_mode=expire_mode,
                expire_in_seconds=expire_in_seconds,
                expire_at=parse_iso(row["expire_at"]),
            )
            return StrategyDescriptionOut(description=description)

    def _create_strategy_locked(
        self,
        conn: sqlite3.Connection,
        payload: StrategyCreateIn,
        *,
        created_event_detail: str,
    ) -> StrategyDetailOut:
        if payload.idempotency_key:
            row = conn.execute(
                f"SELECT id FROM {ACTIVE_STRATEGIES_SOURCE} WHERE idempotency_key = ?",
                (payload.idempotency_key,),
            ).fetchone()
            if row:
                strategy = self._get_strategy_row(conn, row["id"])
                return self._to_detail(conn, strategy)

        strategy_id = payload.id or _generate_strategy_id(conn)
        if payload.id:
            hit = conn.execute("SELECT 1 FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
            if hit:
                raise HTTPException(status_code=409, detail=f"strategy {strategy_id} already exists")

        _validate_trade_action_compatibility(payload.trade_type, payload.trade_action_json)
        try:
            market_profile = resolve_market_profile(payload.market, payload.trade_type)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        next_strategy_id = self._validate_next_strategy_target(
            conn,
            strategy_id=strategy_id,
            next_strategy_id=payload.next_strategy_id,
        )
        next_strategy_note = _normalize_optional_text(payload.next_strategy_note)
        if next_strategy_id is None:
            next_strategy_note = None

        now = utcnow()
        expire_at = payload.expire_at if payload.expire_mode == "absolute" else None
        trade_action_json = _enrich_trade_action_with_strategy_context(
            trade_action_json=payload.trade_action_json,
            market=market_profile.market,
            account_code=_resolve_trade_action_account_code(),
        )

        conditions: list[ConditionItem] = []
        for idx, cond in enumerate(payload.conditions, start=1):
            condition_id = cond.condition_id or f"c{idx}"
            conditions.append(
                cond.model_copy(
                    update={
                        "condition_id": condition_id,
                        "condition_nl": cond.condition_nl or _generate_condition_nl(cond),
                    }
                )
            )

        try:
            conn.execute(
                """
                INSERT INTO strategies (
                    id, idempotency_key, description, market, sec_type, exchange, currency,
                    trade_type, upstream_only_activation,
                    expire_mode, expire_in_seconds, expire_at, status, condition_logic,
                    conditions_json, trade_action_json, next_strategy_id, next_strategy_note,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    payload.idempotency_key,
                    payload.description,
                    market_profile.market,
                    market_profile.sec_type,
                    market_profile.exchange,
                    market_profile.currency,
                    payload.trade_type,
                    int(payload.upstream_only_activation),
                    payload.expire_mode,
                    payload.expire_in_seconds,
                    to_iso(expire_at),
                    "PENDING_ACTIVATION",
                    payload.condition_logic,
                    dumps_json([c.model_dump(exclude_none=True) for c in conditions]),
                    dumps_json(trade_action_json) if trade_action_json is not None else None,
                    next_strategy_id,
                    next_strategy_note,
                    to_iso(now),
                    to_iso(now),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        self._sync_downstream_upstream_link(
            conn,
            strategy_id=strategy_id,
            old_next_strategy_id=None,
            new_next_strategy_id=next_strategy_id,
            now=now,
        )

        for idx, sym in enumerate(payload.symbols, start=1):
            conn.execute(
                """
                INSERT INTO strategy_symbols (
                    strategy_id, position, code, trade_type, contract_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (strategy_id, idx, sym.code, sym.trade_type, sym.contract_id, to_iso(now)),
            )

        for cond in conditions:
            conn.execute(
                """
                INSERT INTO condition_states (
                    strategy_id, condition_id, state, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (strategy_id, cond.condition_id, "NOT_EVALUATED", to_iso(now)),
            )

        self._append_event(conn, strategy_id, "CREATED", created_event_detail, now)
        row = self._get_strategy_row(conn, strategy_id)
        return self._to_detail(conn, row)

    def create_strategy(self, payload: StrategyCreateIn) -> StrategyDetailOut:
        with self._lock, self._conn() as conn:
            detail = self._create_strategy_locked(
                conn,
                payload,
                created_event_detail="策略创建成功",
            )
            conn.commit()
            return detail

    def copy_strategy(self, strategy_id: str) -> StrategyDetailOut:
        with self._lock, self._conn() as conn:
            source = self._get_strategy_row(conn, strategy_id)
            source_id = str(source["id"])
            source_description = str(source["description"] or "").strip()
            copied_description = (
                f"{source_description}（由策略 {source_id} 复制而来）"
                if source_description
                else f"由策略 {source_id} 复制而来"
            )
            source_trade_action_json = (
                json.loads(source["trade_action_json"]) if source["trade_action_json"] is not None else None
            )
            source_conditions = self._load_conditions(source)
            source_symbols = self._load_symbols(conn, source_id)
            source_expire_mode = str(source["expire_mode"] or "relative").strip().lower()
            if source_expire_mode not in {"relative", "absolute"}:
                source_expire_mode = "relative"

            payload = StrategyCreateIn(
                description=copied_description,
                market=str(source["market"] or "").strip().upper() or None,
                trade_type=str(source["trade_type"]),
                symbols=source_symbols,
                upstream_only_activation=bool(source["upstream_only_activation"]),
                expire_mode=source_expire_mode,
                expire_in_seconds=(
                    int(source["expire_in_seconds"]) if source["expire_in_seconds"] is not None else None
                ),
                expire_at=parse_iso(source["expire_at"]) if source_expire_mode == "absolute" else None,
                condition_logic=str(source["condition_logic"] or "AND").strip().upper(),
                conditions=source_conditions,
                trade_action_json=source_trade_action_json,
                # 不复制上下游关系，避免复制后形成链路冲突。
                next_strategy_id=None,
                next_strategy_note=None,
            )
            detail = self._create_strategy_locked(
                conn,
                payload,
                created_event_detail=f"策略创建成功（由策略 {source_id} 复制而来）",
            )
            conn.commit()
            return detail

    def patch_basic(self, strategy_id: str, payload: StrategyBasicPatchIn) -> StrategyDetailOut:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
            _raise_if_strategy_locked(row=row, now=utcnow(), action="patch_basic")
            status = row["status"]
            if status not in EDITABLE_STATUSES:
                raise HTTPException(status_code=409, detail="strategy is not editable")

            current_symbols = self._load_symbols(conn, strategy_id)
            current_trade_action = (
                json.loads(row["trade_action_json"]) if row["trade_action_json"] is not None else None
            )

            fields_set = payload.model_fields_set
            next_trade_type = payload.trade_type if "trade_type" in fields_set else row["trade_type"]
            next_symbols = payload.symbols if "symbols" in fields_set else current_symbols
            if next_trade_type is None:
                raise HTTPException(status_code=422, detail="trade_type cannot be null")
            if next_symbols is None:
                raise HTTPException(status_code=422, detail="symbols cannot be null")

            market_input = payload.market if "market" in fields_set else row["market"]
            try:
                market_profile = resolve_market_profile(market_input, next_trade_type)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

            _validate_trade_symbol_combo(next_trade_type, next_symbols)
            _validate_trade_action_compatibility(next_trade_type, current_trade_action)
            next_trade_action_json = _enrich_trade_action_with_strategy_context(
                trade_action_json=current_trade_action,
                market=market_profile.market,
                account_code=_resolve_trade_action_account_code(),
            )

            description = (
                payload.description if "description" in fields_set and payload.description is not None else row["description"]
            )
            upstream_only_activation = (
                int(payload.upstream_only_activation)
                if "upstream_only_activation" in fields_set and payload.upstream_only_activation is not None
                else row["upstream_only_activation"]
            )
            expire_mode = (
                payload.expire_mode if "expire_mode" in fields_set and payload.expire_mode is not None else row["expire_mode"]
            )
            expire_in_seconds = (
                payload.expire_in_seconds if "expire_in_seconds" in fields_set else row["expire_in_seconds"]
            )
            logical_activated_at = (
                to_iso(payload.logical_activated_at)
                if "logical_activated_at" in fields_set and payload.logical_activated_at is not None
                else None
                if "logical_activated_at" in fields_set
                else row["logical_activated_at"]
            )
            expire_at = row["expire_at"]
            if expire_mode == "relative":
                expire_at = None
            elif "expire_at" in fields_set:
                expire_at = to_iso(payload.expire_at) if payload.expire_at is not None else None

            basic_update_event_detail = _build_basic_update_event_detail(
                old_description=str(row["description"] or ""),
                new_description=str(description or ""),
                old_market=str(row["market"] or ""),
                new_market=str(market_profile.market or ""),
                old_trade_type=str(row["trade_type"] or ""),
                new_trade_type=str(next_trade_type or ""),
                old_symbols=current_symbols,
                new_symbols=next_symbols,
                old_upstream_only_activation=bool(row["upstream_only_activation"]),
                new_upstream_only_activation=bool(upstream_only_activation),
                old_logical_activated_at=_normalize_optional_text(row["logical_activated_at"]),
                new_logical_activated_at=_normalize_optional_text(logical_activated_at),
                old_expire_mode=str(row["expire_mode"] or ""),
                new_expire_mode=str(expire_mode or ""),
                old_expire_in_seconds=(
                    int(row["expire_in_seconds"]) if row["expire_in_seconds"] is not None else None
                ),
                new_expire_in_seconds=int(expire_in_seconds) if expire_in_seconds is not None else None,
                old_expire_at=_normalize_optional_text(row["expire_at"]),
                new_expire_at=_normalize_optional_text(expire_at),
            )

            now = utcnow()
            conn.execute(
                """
                UPDATE strategies
                SET description = ?, market = ?, sec_type = ?, exchange = ?, currency = ?, trade_type = ?,
                    trade_action_json = ?,
                    upstream_only_activation = ?,
                    logical_activated_at = ?,
                    expire_mode = ?, expire_in_seconds = ?, expire_at = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ?
                """,
                (
                    description,
                    market_profile.market,
                    market_profile.sec_type,
                    market_profile.exchange,
                    market_profile.currency,
                    next_trade_type,
                    dumps_json(next_trade_action_json) if next_trade_action_json is not None else None,
                    upstream_only_activation,
                    logical_activated_at,
                    expire_mode,
                    expire_in_seconds,
                    expire_at,
                    to_iso(now),
                    strategy_id,
                ),
            )

            if "symbols" in fields_set:
                conn.execute("DELETE FROM strategy_symbols WHERE strategy_id = ?", (strategy_id,))
                for idx, sym in enumerate(next_symbols, start=1):
                    conn.execute(
                        """
                        INSERT INTO strategy_symbols (
                            strategy_id, position, code, trade_type, contract_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (strategy_id, idx, sym.code, sym.trade_type, sym.contract_id, to_iso(now)),
                    )

            self._append_event(conn, strategy_id, "BASIC_UPDATED", basic_update_event_detail, now)
            self._reset_to_pending_after_config_change(conn, strategy_id, now)
            conn.commit()
            row = self._get_strategy_row(conn, strategy_id)
            return self._to_detail(conn, row)

    def put_conditions(
        self, strategy_id: str, payload: StrategyConditionsPutIn
    ) -> StrategyDetailOut:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
            _raise_if_strategy_locked(row=row, now=utcnow(), action="put_conditions")
            if row["status"] not in EDITABLE_STATUSES:
                raise HTTPException(status_code=409, detail="strategy is not editable")

            conditions: list[ConditionItem] = []
            for idx, cond in enumerate(payload.conditions, start=1):
                condition_id = cond.condition_id or f"c{idx}"
                conditions.append(
                    cond.model_copy(
                        update={
                            "condition_id": condition_id,
                            "condition_nl": cond.condition_nl or _generate_condition_nl(cond),
                        }
                    )
                )

            now = utcnow()
            conn.execute(
                """
                UPDATE strategies
                SET condition_logic = ?, conditions_json = ?, updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (
                    payload.condition_logic,
                    dumps_json([c.model_dump(exclude_none=True) for c in conditions]),
                    to_iso(now),
                    strategy_id,
                ),
            )
            conn.execute("DELETE FROM condition_states WHERE strategy_id = ?", (strategy_id,))
            for cond in conditions:
                conn.execute(
                    """
                    INSERT INTO condition_states (
                        strategy_id, condition_id, state, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (strategy_id, cond.condition_id, "NOT_EVALUATED", to_iso(now)),
                )

            self._append_event(conn, strategy_id, "CONDITIONS_UPDATED", "已更新触发条件", now)
            self._reset_to_pending_after_config_change(conn, strategy_id, now)
            conn.commit()
            row = self._get_strategy_row(conn, strategy_id)
            return self._to_detail(conn, row)

    def put_actions(self, strategy_id: str, payload: StrategyActionsPutIn) -> StrategyDetailOut:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
            _raise_if_strategy_locked(row=row, now=utcnow(), action="put_actions")
            if row["status"] not in EDITABLE_STATUSES:
                raise HTTPException(status_code=409, detail="strategy is not editable")

            _validate_trade_action_compatibility(row["trade_type"], payload.trade_action_json)
            old_next_strategy_id = _normalize_strategy_id(row["next_strategy_id"])
            next_strategy_id = self._validate_next_strategy_target(
                conn,
                strategy_id=strategy_id,
                next_strategy_id=payload.next_strategy_id,
            )
            next_strategy_note = _normalize_optional_text(payload.next_strategy_note)
            if next_strategy_id is None:
                next_strategy_note = None
            trade_action_json = _enrich_trade_action_with_strategy_context(
                trade_action_json=payload.trade_action_json,
                market=str(row["market"] or "").strip().upper(),
                account_code=_resolve_trade_action_account_code(),
            )
            now = utcnow()
            try:
                conn.execute(
                    """
                    UPDATE strategies
                    SET trade_action_json = ?, next_strategy_id = ?, next_strategy_note = ?,
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (
                        dumps_json(trade_action_json) if trade_action_json is not None else None,
                        next_strategy_id,
                        next_strategy_note,
                        to_iso(now),
                        strategy_id,
                    ),
                )
                self._sync_downstream_upstream_link(
                    conn,
                    strategy_id=strategy_id,
                    old_next_strategy_id=old_next_strategy_id,
                    new_next_strategy_id=next_strategy_id,
                    now=now,
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

            self._append_event(conn, strategy_id, "ACTIONS_UPDATED", "已更新后续动作", now)
            self._reset_to_pending_after_config_change(conn, strategy_id, now)
            conn.commit()
            row = self._get_strategy_row(conn, strategy_id)
            return self._to_detail(conn, row)

    def activate(self, strategy_id: str) -> ControlResponse:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
            _raise_if_strategy_locked(row=row, now=utcnow(), action="activate")
            if row["status"] not in {"PENDING_ACTIVATION", "VERIFY_FAILED"}:
                raise HTTPException(
                    status_code=409,
                    detail="only PENDING_ACTIVATION/VERIFY_FAILED can activate",
                )
            if bool(row["upstream_only_activation"]):
                raise HTTPException(status_code=409, detail="upstream_only_activation=true")

            has_conditions = len(json.loads(row["conditions_json"] or "[]")) > 0
            has_actions = row["trade_action_json"] is not None or row["next_strategy_id"] is not None
            if not has_conditions:
                raise HTTPException(status_code=409, detail="conditions not configured")
            if not has_actions:
                raise HTTPException(status_code=409, detail="follow-up actions not configured")

            now = utcnow()
            conn.execute(
                """
                UPDATE strategies
                SET status = 'VERIFYING', updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (to_iso(now), strategy_id),
            )
            self._append_event(conn, strategy_id, "VERIFYING", "策略开始激活前校验", now)
            conn.commit()
            return ControlResponse(
                strategy_id=strategy_id,
                status="VERIFYING",
                message="verifying",
                updated_at=now,
            )

    def pause(self, strategy_id: str) -> ControlResponse:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
            _raise_if_strategy_locked(row=row, now=utcnow(), action="pause")
            source_status = str(row["status"])
            if source_status not in {"ACTIVE", "VERIFYING"}:
                raise HTTPException(status_code=409, detail="only ACTIVE/VERIFYING can pause")
            now = utcnow()
            conn.execute(
                "UPDATE strategies SET status = 'PAUSED', updated_at = ?, version = version + 1 WHERE id = ?",
                (to_iso(now), strategy_id),
            )
            self._set_runtime_state(
                conn,
                strategy_id,
                RUNTIME_KEY_PAUSED_FROM_STATUS,
                source_status,
                now,
            )
            detail = "策略已暂停"
            if source_status != "ACTIVE":
                detail = f"策略已暂停（来源状态：{source_status}）"
            self._append_event(conn, strategy_id, "PAUSED", detail, now)
            conn.commit()
            return ControlResponse(
                strategy_id=strategy_id,
                status="PAUSED",
                message="paused",
                updated_at=now,
            )

    def resume(self, strategy_id: str) -> ControlResponse:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
            _raise_if_strategy_locked(row=row, now=utcnow(), action="resume")
            if row["status"] != "PAUSED":
                raise HTTPException(status_code=409, detail="only PAUSED can resume")
            source_status = self._get_runtime_state(
                conn,
                strategy_id,
                RUNTIME_KEY_PAUSED_FROM_STATUS,
            )
            target_status: StrategyStatus = "VERIFYING" if source_status == "VERIFYING" else "ACTIVE"
            now = utcnow()
            conn.execute(
                "UPDATE strategies SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                (target_status, to_iso(now), strategy_id),
            )
            self._delete_runtime_state(conn, strategy_id, RUNTIME_KEY_PAUSED_FROM_STATUS)
            detail = "策略已恢复"
            if target_status == "VERIFYING":
                detail = "策略已恢复到校验中"
            self._append_event(conn, strategy_id, "RESUMED", detail, now)
            conn.commit()
            return ControlResponse(
                strategy_id=strategy_id,
                status=target_status,
                message="resumed",
                updated_at=now,
            )

    def cancel(self, strategy_id: str) -> ControlResponse:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
            _raise_if_strategy_locked(row=row, now=utcnow(), action="cancel")
            if row["status"] in TERMINAL_STATUSES:
                raise HTTPException(status_code=409, detail="terminal status cannot cancel")
            now = utcnow()
            conn.execute(
                """
                UPDATE strategies
                SET status = 'CANCELLED', updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (to_iso(now), strategy_id),
            )
            self._append_event(conn, strategy_id, "CANCELLED", "策略已取消", now)
            conn.commit()
            return ControlResponse(
                strategy_id=strategy_id,
                status="CANCELLED",
                message="cancelled",
                updated_at=now,
            )

    def delete_strategy(self, strategy_id: str) -> ControlResponse:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id, include_deleted=True)
            if bool(row["is_deleted"]):
                return ControlResponse(
                    strategy_id=strategy_id,
                    status=row["status"],
                    message="already_deleted",
                    updated_at=parse_iso(row["updated_at"]) or utcnow(),
                )
            _raise_if_strategy_locked(row=row, now=utcnow(), action="delete")

            can_delete, delete_reason = _delete_capability(
                status=row["status"],
                has_active_trade_instruction=self._has_active_trade_instruction(conn, strategy_id),
                has_upstream_strategy=_normalize_strategy_id(row["upstream_strategy_id"]) is not None,
            )
            if not can_delete:
                raise HTTPException(status_code=409, detail=delete_reason or "strategy cannot be deleted")

            now = utcnow()
            now_iso = to_iso(now)
            conn.execute(
                """
                UPDATE strategies
                SET next_strategy_id = NULL, next_strategy_note = NULL, updated_at = ?, version = version + 1
                WHERE next_strategy_id = ? AND is_deleted = 0
                """,
                (now_iso, strategy_id),
            )
            conn.execute(
                """
                UPDATE strategies
                SET upstream_strategy_id = NULL, updated_at = ?, version = version + 1
                WHERE upstream_strategy_id = ? AND is_deleted = 0
                """,
                (now_iso, strategy_id),
            )
            conn.execute(
                """
                UPDATE strategies
                SET status = 'CANCELLED',
                    next_strategy_id = NULL,
                    next_strategy_note = NULL,
                    upstream_strategy_id = NULL,
                    is_deleted = 1,
                    deleted_at = ?,
                    updated_at = ?,
                    version = version + 1
                WHERE id = ?
                """,
                (now_iso, now_iso, strategy_id),
            )
            self._append_event(conn, strategy_id, "DELETED", "策略已删除（软删除）", now)
            conn.commit()
            return ControlResponse(
                strategy_id=strategy_id,
                status="CANCELLED",
                message="deleted",
                updated_at=now,
            )

    def strategy_events(self, strategy_id: str) -> list[EventLogItem]:
        with self._lock, self._conn() as conn:
            self._get_strategy_row(conn, strategy_id)
            return self._load_events(conn, strategy_id)

    def global_events(self) -> list[EventLogItem]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, event_type, detail, strategy_id
                FROM strategy_events
                ORDER BY timestamp DESC, id DESC
                """
            ).fetchall()
            return [
                EventLogItem(
                    timestamp=parse_iso(r["timestamp"]) or utcnow(),
                    event_type=r["event_type"],
                    detail=r["detail"],
                    strategy_id=r["strategy_id"],
                )
                for r in rows
            ]

    def active_trade_instructions(self) -> list[ActiveTradeInstructionOut]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT ti.updated_at,
                       ti.strategy_id,
                       ti.trade_id,
                       ti.instruction_summary,
                       ti.status,
                       ti.expire_at,
                       (
                         SELECT o.ib_order_id
                         FROM orders o
                         WHERE o.strategy_id = ti.strategy_id
                           AND COALESCE(NULLIF(o.trade_id, ''), o.id) = ti.trade_id
                         ORDER BY o.sequence_no ASC, o.updated_at DESC, o.id DESC
                         LIMIT 1
                       ) AS primary_ib_order_id,
                       (
                         SELECT COUNT(*)
                         FROM orders o
                         WHERE o.strategy_id = ti.strategy_id
                           AND COALESCE(NULLIF(o.trade_id, ''), o.id) = ti.trade_id
                       ) AS order_count,
                       (
                         SELECT COUNT(*)
                         FROM orders o
                         WHERE o.strategy_id = ti.strategy_id
                           AND COALESCE(NULLIF(o.trade_id, ''), o.id) = ti.trade_id
                           AND UPPER(o.status) = 'FILLED'
                       ) AS filled_order_count
                FROM trade_instructions ti
                WHERE ti.status NOT IN (?, ?, ?, ?)
                ORDER BY ti.updated_at DESC
                """,
                TRADE_INSTRUCTION_TERMINAL_STATUSES,
            ).fetchall()
            return [
                ActiveTradeInstructionOut(
                    updated_at=parse_iso(r["updated_at"]) or utcnow(),
                    strategy_id=r["strategy_id"],
                    trade_id=r["trade_id"],
                    perm_id=_to_int_or_none(r["primary_ib_order_id"]),
                    order_count=int(r["order_count"] or 0),
                    filled_order_count=int(r["filled_order_count"] or 0),
                    instruction_summary=r["instruction_summary"],
                    status=r["status"],
                    expire_at=parse_iso(r["expire_at"]),
                )
                for r in rows
            ]

    def completed_trade_instructions_recent(self, *, days: int = 7) -> list[ActiveTradeInstructionOut]:
        with self._lock, self._conn() as conn:
            lookback_days = max(1, int(days))
            cutoff_iso = to_iso(utcnow() - timedelta(days=lookback_days)) or "1970-01-01T00:00:00Z"
            rows = conn.execute(
                """
                SELECT ti.updated_at,
                       ti.strategy_id,
                       ti.trade_id,
                       ti.instruction_summary,
                       ti.status,
                       ti.expire_at,
                       (
                         SELECT o.ib_order_id
                         FROM orders o
                         WHERE o.strategy_id = ti.strategy_id
                           AND COALESCE(NULLIF(o.trade_id, ''), o.id) = ti.trade_id
                         ORDER BY o.sequence_no ASC, o.updated_at DESC, o.id DESC
                         LIMIT 1
                       ) AS primary_ib_order_id,
                       (
                         SELECT COUNT(*)
                         FROM orders o
                         WHERE o.strategy_id = ti.strategy_id
                           AND COALESCE(NULLIF(o.trade_id, ''), o.id) = ti.trade_id
                       ) AS order_count,
                       (
                         SELECT COUNT(*)
                         FROM orders o
                         WHERE o.strategy_id = ti.strategy_id
                           AND COALESCE(NULLIF(o.trade_id, ''), o.id) = ti.trade_id
                           AND UPPER(o.status) = 'FILLED'
                       ) AS filled_order_count
                FROM trade_instructions ti
                WHERE ti.status IN (?, ?, ?, ?)
                  AND ti.updated_at >= ?
                ORDER BY ti.updated_at DESC
                """,
                (*TRADE_INSTRUCTION_TERMINAL_STATUSES, cutoff_iso),
            ).fetchall()
            return [
                ActiveTradeInstructionOut(
                    updated_at=parse_iso(r["updated_at"]) or utcnow(),
                    strategy_id=r["strategy_id"],
                    trade_id=r["trade_id"],
                    perm_id=_to_int_or_none(r["primary_ib_order_id"]),
                    order_count=int(r["order_count"] or 0),
                    filled_order_count=int(r["filled_order_count"] or 0),
                    instruction_summary=r["instruction_summary"],
                    status=r["status"],
                    expire_at=parse_iso(r["expire_at"]),
                )
                for r in rows
            ]

    def other_open_orders(self) -> list[OtherOpenOrderOut]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT o.ib_order_id
                FROM trade_instructions ti
                JOIN orders o
                  ON COALESCE(NULLIF(o.trade_id, ''), o.id) = ti.trade_id
                 AND o.strategy_id = ti.strategy_id
                WHERE ti.status NOT IN (?, ?, ?, ?)
                """,
                TRADE_INSTRUCTION_TERMINAL_STATUSES,
            ).fetchall()
            active_perm_ids: set[int] = set()
            for row in rows:
                perm_id = _to_int_or_none(row["ib_order_id"])
                if perm_id is not None:
                    active_perm_ids.add(perm_id)

        order_service = IBTradeService()
        try:
            snapshots = order_service.list_active_orders()
        except (IBTradeServiceError, ValueError, RuntimeError, TimeoutError) as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise HTTPException(status_code=502, detail=f"broker query failed: {detail}") from exc

        trade_service_client_id = _to_int_including_zero_or_none(getattr(order_service, "client_id", None))
        out: list[OtherOpenOrderOut] = []
        for item in snapshots:
            perm_id = _to_int_or_none(item.perm_id)
            if perm_id is None:
                continue
            if perm_id in active_perm_ids:
                continue
            item_client_id = _to_int_including_zero_or_none(getattr(item, "client_id", None))
            can_cancel = (
                item_client_id is not None
                and trade_service_client_id is not None
                and item_client_id == trade_service_client_id
            )
            out.append(
                OtherOpenOrderOut(
                    updated_at=item.updated_at,
                    perm_id=perm_id,
                    order_id=_to_int_or_none(item.order_id),
                    can_cancel=can_cancel,
                    client_id=item_client_id,
                    trade_service_client_id=trade_service_client_id,
                    symbol=str(item.symbol or "").strip().upper(),
                    sec_type=str(item.sec_type or "").strip().upper(),
                    side=str(item.side or "").strip().upper(),
                    order_type=str(item.order_type or "").strip().upper(),
                    quantity=float(item.quantity),
                    status=str(item.normalized_status or item.status or "").strip().upper(),
                    filled_qty=float(item.filled_qty),
                    remaining_qty=float(item.remaining_qty),
                    avg_fill_price=item.avg_fill_price,
                    account_code=_normalize_optional_text(item.account_code),
                )
            )
        out.sort(
            key=lambda item: (
                item.updated_at or datetime(1970, 1, 1, tzinfo=timezone.utc),
                item.perm_id,
            ),
            reverse=True,
        )
        return out

    def trade_instruction_orders(self, trade_id: str) -> list[TradeOrderOut]:
        normalized_trade_id = _normalize_optional_text(trade_id)
        if normalized_trade_id is None:
            raise HTTPException(status_code=422, detail="trade_id is required")

        with self._lock, self._conn() as conn:
            head = conn.execute(
                """
                SELECT trade_id, strategy_id
                FROM trade_instructions
                WHERE trade_id = ?
                LIMIT 1
                """,
                (normalized_trade_id,),
            ).fetchone()
            if head is None:
                raise HTTPException(status_code=404, detail=f"trade {normalized_trade_id} not found")

            rows = conn.execute(
                """
                SELECT id,
                       COALESCE(NULLIF(trade_id, ''), id) AS resolved_trade_id,
                       strategy_id,
                       leg_role,
                       sequence_no,
                       ib_order_id,
                       status,
                       qty,
                       avg_fill_price,
                       filled_qty,
                       error_message,
                       order_payload_json,
                       created_at,
                       updated_at
                FROM orders
                WHERE strategy_id = ?
                  AND COALESCE(NULLIF(trade_id, ''), id) = ?
                ORDER BY sequence_no ASC, created_at ASC, id ASC
                """,
                (head["strategy_id"], normalized_trade_id),
            ).fetchall()

            order_ids = [str(r["id"]) for r in rows]
            legs_by_order_id: dict[str, list[TradeOrderLegOut]] = {}
            if order_ids:
                placeholders = ",".join("?" for _ in order_ids)
                leg_rows = conn.execute(
                    f"""
                    SELECT order_id,
                           leg_index,
                           con_id,
                           symbol,
                           contract_month,
                           side,
                           ratio,
                           exchange
                    FROM order_legs
                    WHERE order_id IN ({placeholders})
                    ORDER BY order_id ASC, leg_index ASC, id ASC
                    """,
                    tuple(order_ids),
                ).fetchall()
                for leg in leg_rows:
                    order_id_key = str(leg["order_id"])
                    legs_by_order_id.setdefault(order_id_key, []).append(
                        TradeOrderLegOut(
                            leg_index=int(leg["leg_index"]),
                            con_id=_to_int_or_none(leg["con_id"]),
                            symbol=_normalize_optional_text(leg["symbol"]),
                            contract_month=_normalize_optional_text(leg["contract_month"]),
                            side=str(leg["side"] or "").strip().upper(),
                            ratio=float(leg["ratio"] or 1.0),
                            exchange=_normalize_optional_text(leg["exchange"]),
                        )
                    )

            out: list[TradeOrderOut] = []
            for row in rows:
                order_payload: dict[str, Any] | None = None
                raw_payload = str(row["order_payload_json"] or "").strip()
                if raw_payload:
                    try:
                        parsed = json.loads(raw_payload)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, dict):
                        order_payload = parsed
                row_id = str(row["id"])
                out.append(
                    TradeOrderOut(
                        id=row_id,
                        trade_id=str(row["resolved_trade_id"]),
                        strategy_id=str(row["strategy_id"]),
                        leg_role=str(row["leg_role"] or "SINGLE").strip().upper(),
                        sequence_no=max(1, int(row["sequence_no"] or 1)),
                        ib_order_id=_normalize_optional_text(row["ib_order_id"]),
                        status=str(row["status"] or "").strip().upper(),
                        qty=float(row["qty"] or 0.0),
                        avg_fill_price=(
                            float(row["avg_fill_price"]) if row["avg_fill_price"] is not None else None
                        ),
                        filled_qty=float(row["filled_qty"] or 0.0),
                        error_message=_normalize_optional_text(row["error_message"]),
                        order_payload=order_payload,
                        created_at=parse_iso(row["created_at"]) or utcnow(),
                        updated_at=parse_iso(row["updated_at"]) or utcnow(),
                        legs=legs_by_order_id.get(row_id, []),
                    )
                )
            return out

    def cancel_other_open_order(self, perm_id: int) -> OpenOrderCancelOut:
        target_perm_id = _to_int_or_none(perm_id)
        if target_perm_id is None:
            raise HTTPException(status_code=422, detail="perm_id must be positive integer")

        order_service = IBTradeService()
        try:
            snapshot = order_service.cancel_order(
                perm_id=target_perm_id,
                wait_for_terminal=True,
                timeout_seconds=CANCEL_OTHER_OPEN_ORDER_WAIT_TIMEOUT_SECONDS,
            )
        except IBTradeServiceError as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            if "different clientId" in detail:
                raise HTTPException(status_code=409, detail=detail) from exc
            raise HTTPException(status_code=502, detail=f"broker cancel failed: {detail}") from exc
        except (ValueError, RuntimeError, TimeoutError) as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise HTTPException(status_code=502, detail=f"broker cancel failed: {detail}") from exc

        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"open order not found by perm_id={target_perm_id}")

        status = str(snapshot.normalized_status or snapshot.status or "").strip().upper()
        if not status:
            status = "UNKNOWN"
        return OpenOrderCancelOut(
            perm_id=target_perm_id,
            order_id=snapshot.order_id,
            status=status,
            terminal=bool(snapshot.terminal),
            message=f"cancel requested for perm_id={target_perm_id}",
            updated_at=snapshot.updated_at,
        )

    def trade_logs(self, *, trade_id: str | None = None) -> list[TradeLogOut]:
        with self._lock, self._conn() as conn:
            normalized_trade_id = _normalize_optional_text(trade_id)
            if normalized_trade_id is None:
                rows = conn.execute(
                    """
                    SELECT timestamp, strategy_id, trade_id, stage, result, detail
                    FROM trade_logs
                    ORDER BY timestamp DESC, id DESC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT timestamp, strategy_id, trade_id, stage, result, detail
                    FROM trade_logs
                    WHERE trade_id = ?
                    ORDER BY timestamp DESC, id DESC
                    """,
                    (normalized_trade_id,),
                ).fetchall()
            return [
                TradeLogOut(
                    timestamp=parse_iso(r["timestamp"]) or utcnow(),
                    strategy_id=r["strategy_id"],
                    trade_id=r["trade_id"],
                    stage=r["stage"],
                    result=r["result"],
                    detail=r["detail"],
                )
                for r in rows
            ]

    def recover_trade_instruction(self, trade_id: str, payload: TradeRecoveryIn) -> TradeRecoveryOut:
        normalized_trade_id = _normalize_optional_text(trade_id)
        if normalized_trade_id is None:
            raise HTTPException(status_code=422, detail="trade_id is required")

        now = utcnow()
        now_iso = to_iso(now)
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """
                SELECT ti.trade_id,
                       ti.strategy_id,
                       ti.status AS instruction_status,
                       o.id AS order_row_id,
                       o.status AS order_status,
                       o.ib_order_id,
                       o.sequence_no,
                       o.error_message,
                       o.order_payload_json,
                       s.status AS strategy_status,
                       s.market,
                       s.lock_until,
                       s.trade_action_json AS strategy_trade_action_json
                FROM trade_instructions ti
                LEFT JOIN orders o
                  ON o.id = (
                    SELECT oo.id
                    FROM orders oo
                    WHERE oo.strategy_id = ti.strategy_id
                      AND COALESCE(NULLIF(oo.trade_id, ''), oo.id) = ti.trade_id
                    ORDER BY oo.sequence_no DESC, oo.updated_at DESC, oo.id DESC
                    LIMIT 1
                  )
                 AND o.strategy_id = ti.strategy_id
                JOIN strategies s
                  ON s.id = ti.strategy_id
                WHERE ti.trade_id = ?
                LIMIT 1
                """,
                (normalized_trade_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"trade {normalized_trade_id} not found")

            _raise_if_strategy_locked(row=row, now=now, action="recover_trade_instruction")
            order_row_id = _normalize_optional_text(row["order_row_id"])
            if order_row_id is None:
                raise HTTPException(status_code=409, detail="trade has no order rows")
            instruction_status = str(row["instruction_status"] or "").strip().upper()
            order_status = str(row["order_status"] or "").strip().upper()
            if instruction_status != "ORDER_DISPATCHING" and order_status != "ORDER_DISPATCHING":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "trade is not in ORDER_DISPATCHING"
                        f" instruction_status={instruction_status or '-'}"
                        f" order_status={order_status or '-'}"
                    ),
                )

            strategy_id = str(row["strategy_id"])
            order_payload_raw = str(row["order_payload_json"] or "{}")
            try:
                order_payload = json.loads(order_payload_raw)
            except json.JSONDecodeError:
                order_payload = {}
            if not isinstance(order_payload, dict):
                order_payload = {}

            dispatch_payload = order_payload.get("dispatch")
            order_ref_from_payload = None
            if isinstance(dispatch_payload, dict):
                order_ref_from_payload = _normalize_optional_text(
                    dispatch_payload.get("order_ref"),  # type: ignore[arg-type]
                )
            query_order_ref = (
                payload.order_ref
                or order_ref_from_payload
                or normalized_trade_id
            )

            recovered_status = "FAILED"
            recovered_order_id: int | None = None
            recovered_perm_id: int | None = None
            recovered_ib_order_id: str | None = _normalize_optional_text(row["ib_order_id"])
            recovered_error_message: str | None = _normalize_optional_text(row["error_message"])
            recovery_message = ""

            def _query_snapshot(order_service: IBTradeService) -> OrderStatusSnapshot | None:
                if payload.perm_id is not None:
                    return order_service.poll_order_status(perm_id=payload.perm_id)
                if payload.order_id is not None:
                    return order_service.poll_order_status(order_id=payload.order_id)
                return order_service.poll_order_status_by_order_ref(order_ref=query_order_ref)

            action = payload.action
            if action == "mark_failed":
                recovered_status = "FAILED"
                recovered_error_message = payload.reason or "manually marked failed"
                recovery_message = "marked failed manually"
            else:
                order_service = IBTradeService()
                try:
                    snapshot = _query_snapshot(order_service)
                except (IBTradeServiceError, ValueError, RuntimeError, TimeoutError) as exc:
                    detail = str(exc).strip() or exc.__class__.__name__
                    raise HTTPException(status_code=502, detail=f"broker query failed: {detail}") from exc

                if snapshot is not None:
                    recovered_status = str(snapshot.normalized_status or "ORDER_SUBMITTED").upper()
                    recovered_order_id = snapshot.order_id
                    recovered_perm_id = snapshot.perm_id
                    recovered_ib_order_id = (
                        str(snapshot.perm_id) if snapshot.perm_id is not None else recovered_ib_order_id
                    )
                    recovered_error_message = snapshot.error_message
                    recovery_message = "adopted broker order"
                elif action == "retry_dispatch":
                    trade_action_payload = order_payload.get("trade_action")
                    if not isinstance(trade_action_payload, dict):
                        strategy_trade_action_raw = row["strategy_trade_action_json"]
                        strategy_trade_action = (
                            json.loads(strategy_trade_action_raw) if strategy_trade_action_raw else None
                        )
                        if not isinstance(strategy_trade_action, dict):
                            raise HTTPException(
                                status_code=422,
                                detail="missing trade_action payload for retry_dispatch",
                            )
                        trade_action_payload = strategy_trade_action

                    account_code = _normalize_optional_text(trade_action_payload.get("account_code"))
                    enriched_action = _enrich_trade_action_with_strategy_context(
                        trade_action_json=trade_action_payload,
                        market=str(row["market"] or "").strip().upper(),
                        account_code=account_code or _resolve_trade_action_account_code(),
                    )
                    if not isinstance(enriched_action, dict):
                        raise HTTPException(
                            status_code=422,
                            detail="failed to build trade_action payload for retry_dispatch",
                        )
                    try:
                        submit_result = order_service.submit_trade_action(
                            trade_action=enriched_action,
                            order_ref=query_order_ref,
                        )
                    except (IBTradeServiceError, ValueError, RuntimeError, TimeoutError) as exc:
                        detail = str(exc).strip() or exc.__class__.__name__
                        raise HTTPException(status_code=502, detail=f"retry dispatch failed: {detail}") from exc

                    recovered_status = str(submit_result.normalized_status or "ORDER_SUBMITTED").upper()
                    recovered_order_id = submit_result.order_id
                    recovered_perm_id = submit_result.perm_id
                    recovered_ib_order_id = (
                        str(submit_result.perm_id) if submit_result.perm_id is not None else recovered_ib_order_id
                    )
                    recovered_error_message = None
                    recovery_message = "retry dispatch submitted"
                    order_payload = {
                        "trade_action": enriched_action,
                        "submit_result": {
                            "order_id": submit_result.order_id,
                            "perm_id": submit_result.perm_id,
                            "status": submit_result.status,
                            "normalized_status": submit_result.normalized_status,
                            "filled_qty": submit_result.filled_qty,
                            "remaining_qty": submit_result.remaining_qty,
                            "avg_fill_price": submit_result.avg_fill_price,
                        },
                        "recovery": {
                            "action": action,
                            "order_ref": query_order_ref,
                            "timestamp": now_iso,
                        },
                    }
                else:
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            "broker order not found for dispatching trade; "
                            "provide perm_id/order_id or use action=retry_dispatch/mark_failed"
                        ),
                    )

            strategy_status = _strategy_status_from_trade_status(recovered_status)
            if action == "mark_failed":
                strategy_status = "FAILED"
            if action != "retry_dispatch":
                order_payload = {
                    **order_payload,
                    "recovery": {
                        "action": action,
                        "order_ref": query_order_ref,
                        "order_id": recovered_order_id,
                        "perm_id": recovered_perm_id,
                        "reason": payload.reason,
                        "timestamp": now_iso,
                    },
                }

            conn.execute(
                """
                UPDATE trade_instructions
                SET status = ?, updated_at = ?
                WHERE trade_id = ? AND strategy_id = ?
                """,
                (recovered_status, now_iso, normalized_trade_id, strategy_id),
            )
            conn.execute(
                """
                UPDATE orders
                SET ib_order_id = ?,
                    status = ?,
                    error_message = ?,
                    order_payload_json = ?,
                    updated_at = ?
                WHERE id = ? AND strategy_id = ?
                """,
                (
                    recovered_ib_order_id,
                    recovered_status,
                    recovered_error_message,
                    dumps_json(order_payload),
                    now_iso,
                    order_row_id,
                    strategy_id,
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
                    normalized_trade_id,
                    "RECOVERY",
                    recovered_status,
                    (
                        f"action={action} order_ref={query_order_ref} "
                        f"order_id={recovered_order_id or '-'} perm_id={recovered_perm_id or '-'} "
                        f"message={recovery_message or '-'}"
                    ),
                ),
            )

            strategy_cursor = conn.execute(
                """
                UPDATE strategies
                SET status = ?, updated_at = ?, version = version + 1
                WHERE id = ?
                  AND status IN ('TRIGGERED', 'ORDER_SUBMITTED')
                  AND is_deleted = 0
                """,
                (strategy_status, now_iso, strategy_id),
            )
            if strategy_cursor.rowcount > 0:
                self._append_event(
                    conn,
                    strategy_id,
                    strategy_status,
                    f"人工恢复交易指令 {normalized_trade_id}: {recovered_status}",
                    now,
                )
            self._append_event(
                conn,
                strategy_id,
                "ORDER_RECOVERED",
                (
                    f"trade_id={normalized_trade_id} action={action} "
                    f"order_id={recovered_order_id or '-'} perm_id={recovered_perm_id or '-'} "
                    f"status={recovered_status}"
                ),
                now,
            )
            conn.commit()
            return TradeRecoveryOut(
                trade_id=normalized_trade_id,
                strategy_id=strategy_id,
                trade_status=recovered_status,
                strategy_status=strategy_status,
                message=recovery_message or f"manual recovery completed action={action}",
                order_id=recovered_order_id,
                perm_id=recovered_perm_id,
                ib_order_id=recovered_ib_order_id,
                updated_at=now,
            )

    def portfolio_summary(self) -> PortfolioSummaryOut:
        snapshot = self._load_broker_snapshot()
        values = snapshot.values_float
        net_liquidation = float(values.get("NetLiquidation") or 0.0)
        available_funds = float(values.get("AvailableFunds") or values.get("ExcessLiquidity") or 0.0)
        positions_unrealized = sum(float(item.unrealized_pnl or 0.0) for item in snapshot.positions)
        positions_realized = sum(float(item.realized_pnl or 0.0) for item in snapshot.positions)
        unrealized_pnl = float(values.get("UnrealizedPnL", positions_unrealized))
        realized_pnl = float(values.get("RealizedPnL", positions_realized))
        daily_pnl = values.get("DailyPnL")
        if daily_pnl is None:
            daily_pnl = unrealized_pnl + realized_pnl
        return PortfolioSummaryOut(
            net_liquidation=net_liquidation,
            available_funds=available_funds,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized_pnl,
            daily_pnl=float(daily_pnl),
            updated_at=snapshot.fetched_at,
        )

    def positions(
        self,
        sec_type: str | None = None,
        symbol: str | None = None,
    ) -> list[PositionItemOut]:
        snapshot = self._load_broker_snapshot()
        sec_type_filter = str(sec_type or "").strip().upper() or None
        symbol_filter = str(symbol or "").strip().upper() or None

        items: list[PositionItemOut] = []
        for row in snapshot.positions:
            normalized_sec_type = str(row.sec_type).strip().upper()
            if normalized_sec_type not in {"STK", "FUT"}:
                continue
            normalized_symbol = str(row.symbol).strip().upper()
            if sec_type_filter and normalized_sec_type != sec_type_filter:
                continue
            if symbol_filter and normalized_symbol != symbol_filter:
                continue
            items.append(
                PositionItemOut(
                    sec_type=normalized_sec_type,
                    symbol=normalized_symbol,
                    position_qty=float(row.position),
                    position_unit="股" if normalized_sec_type == "STK" else "手",
                    avg_price=float(row.average_cost),
                    last_price=float(row.market_price),
                    market_value=float(row.market_value),
                    unrealized_pnl=float(row.unrealized_pnl),
                    updated_at=snapshot.fetched_at,
                )
            )
        items.sort(key=lambda item: (item.symbol, item.sec_type))
        return items

    def shutdown(self) -> None:
        with self._lock:
            close_broker_data_runtime()


store = SQLiteStore()
