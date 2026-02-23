from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from .db import get_connection, init_db
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
    StrategyDetailOut,
    StrategyStatus,
    StrategySummaryOut,
    StrategySymbolItem,
    TradeActionRuntime,
    TradeLogOut,
    TriggerGroupStatus,
    _validate_trade_symbol_combo,
)

TERMINAL_STATUSES: set[str] = {"FILLED", "EXPIRED", "CANCELLED", "FAILED"}
TRADE_INSTRUCTION_TERMINAL_STATUSES: tuple[str, ...] = ("FILLED", "CANCELLED", "FAILED", "EXPIRED")
EDITABLE_STATUSES: set[str] = {"PENDING_ACTIVATION", "PAUSED", "VERIFY_FAILED"}
STOCK_TRADE_TYPES: set[str] = {"buy", "sell", "switch"}
ACTIVE_STRATEGIES_SOURCE = "v_strategies_active"


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


def _editable(status: str) -> tuple[bool, str | None]:
    if status in EDITABLE_STATUSES:
        return True, None
    return False, "仅 PENDING_ACTIVATION / PAUSED / VERIFY_FAILED 可编辑；ACTIVE 请先暂停。"


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

    can_pause = status == "ACTIVE"
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
        can_pause=None if can_pause else "仅 ACTIVE 可暂停",
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


class SQLiteStore:
    def __init__(self) -> None:
        self._lock = Lock()
        init_db()

    def _conn(self) -> sqlite3.Connection:
        return get_connection()

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
            SELECT id, status, error_message
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
                trade_id=row["id"],
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
                logical_activated_at = NULL,
                updated_at = ?,
                version = version + 1
            WHERE id = ?
            """,
            (to_iso(now), strategy_id),
        )
        self._append_event(conn, strategy_id, "RESET_TO_PENDING", "配置变更，状态重置为待激活", now)

    def _run_activation_verification(
        self, conn: sqlite3.Connection, strategy_id: str, row: sqlite3.Row
    ) -> str | None:
        try:
            resolve_market_profile(row["market"], row["trade_type"])
        except ValueError as exc:
            return str(exc)

        symbols = self._load_symbols(conn, strategy_id)
        invalid_codes = [s.code for s in symbols if not s.code.strip()]
        if invalid_codes:
            return f"symbols contains invalid code: {','.join(invalid_codes)}"

        invalid_contract_ids = [str(s.contract_id) for s in symbols if s.contract_id is not None and s.contract_id <= 0]
        if invalid_contract_ids:
            return f"symbols contains invalid contract_id: {','.join(invalid_contract_ids)}"
        return None

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

    def create_strategy(self, payload: StrategyCreateIn) -> StrategyDetailOut:
        with self._lock, self._conn() as conn:
            if payload.idempotency_key:
                row = conn.execute(
                    f"SELECT id FROM {ACTIVE_STRATEGIES_SOURCE} WHERE idempotency_key = ?",
                    (payload.idempotency_key,),
                ).fetchone()
                if row:
                    strategy = self._get_strategy_row(conn, row["id"])
                    return self._to_detail(conn, strategy)

            strategy_id = payload.id or f"S-{uuid4().hex[:6].upper()}"
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
                        dumps_json(payload.trade_action_json)
                        if payload.trade_action_json is not None
                        else None,
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

            self._append_event(conn, strategy_id, "CREATED", "策略创建成功", now)
            conn.commit()
            row = self._get_strategy_row(conn, strategy_id)
            return self._to_detail(conn, row)

    def patch_basic(self, strategy_id: str, payload: StrategyBasicPatchIn) -> StrategyDetailOut:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
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
            expire_at = row["expire_at"]
            if expire_mode == "relative":
                expire_at = None
            elif "expire_at" in fields_set:
                expire_at = to_iso(payload.expire_at) if payload.expire_at is not None else None

            now = utcnow()
            conn.execute(
                """
                UPDATE strategies
                SET description = ?, market = ?, sec_type = ?, exchange = ?, currency = ?, trade_type = ?,
                    upstream_only_activation = ?,
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
                    upstream_only_activation,
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

            self._append_event(conn, strategy_id, "BASIC_UPDATED", "已更新基本信息", now)
            self._reset_to_pending_after_config_change(conn, strategy_id, now)
            conn.commit()
            row = self._get_strategy_row(conn, strategy_id)
            return self._to_detail(conn, row)

    def put_conditions(
        self, strategy_id: str, payload: StrategyConditionsPutIn
    ) -> StrategyDetailOut:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
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
                        dumps_json(payload.trade_action_json)
                        if payload.trade_action_json is not None
                        else None,
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

            verify_error = self._run_activation_verification(conn, strategy_id, row)
            if verify_error:
                fail_now = utcnow()
                conn.execute(
                    """
                    UPDATE strategies
                    SET status = 'VERIFY_FAILED', updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (to_iso(fail_now), strategy_id),
                )
                self._append_event(conn, strategy_id, "VERIFY_FAILED", verify_error, fail_now)
                conn.commit()
                return ControlResponse(
                    strategy_id=strategy_id,
                    status="VERIFY_FAILED",
                    message="verify_failed",
                    updated_at=fail_now,
                )

            activated_now = utcnow()
            expire_at = row["expire_at"]
            if row["expire_mode"] == "relative" and row["expire_in_seconds"]:
                expire_at = to_iso(activated_now + timedelta(seconds=row["expire_in_seconds"]))

            conn.execute(
                """
                UPDATE strategies
                SET status = 'ACTIVE', activated_at = ?, logical_activated_at = ?,
                    expire_at = ?, updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (to_iso(activated_now), to_iso(activated_now), expire_at, to_iso(activated_now), strategy_id),
            )
            self._append_event(conn, strategy_id, "ACTIVATED", "策略已通过校验并激活", activated_now)
            conn.commit()
            return ControlResponse(
                strategy_id=strategy_id,
                status="ACTIVE",
                message="activated",
                updated_at=activated_now,
            )

    def pause(self, strategy_id: str) -> ControlResponse:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
            if row["status"] != "ACTIVE":
                raise HTTPException(status_code=409, detail="only ACTIVE can pause")
            now = utcnow()
            conn.execute(
                "UPDATE strategies SET status = 'PAUSED', updated_at = ?, version = version + 1 WHERE id = ?",
                (to_iso(now), strategy_id),
            )
            self._append_event(conn, strategy_id, "PAUSED", "策略已暂停", now)
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
            if row["status"] != "PAUSED":
                raise HTTPException(status_code=409, detail="only PAUSED can resume")
            now = utcnow()
            conn.execute(
                "UPDATE strategies SET status = 'ACTIVE', updated_at = ?, version = version + 1 WHERE id = ?",
                (to_iso(now), strategy_id),
            )
            self._append_event(conn, strategy_id, "RESUMED", "策略已恢复", now)
            conn.commit()
            return ControlResponse(
                strategy_id=strategy_id,
                status="ACTIVE",
                message="resumed",
                updated_at=now,
            )

    def cancel(self, strategy_id: str) -> ControlResponse:
        with self._lock, self._conn() as conn:
            row = self._get_strategy_row(conn, strategy_id)
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
                SELECT updated_at, strategy_id, trade_id, instruction_summary, status, expire_at
                FROM trade_instructions
                WHERE status NOT IN (?, ?, ?, ?)
                ORDER BY updated_at DESC
                """,
                TRADE_INSTRUCTION_TERMINAL_STATUSES,
            ).fetchall()
            return [
                ActiveTradeInstructionOut(
                    updated_at=parse_iso(r["updated_at"]) or utcnow(),
                    strategy_id=r["strategy_id"],
                    trade_id=r["trade_id"],
                    instruction_summary=r["instruction_summary"],
                    status=r["status"],
                    expire_at=parse_iso(r["expire_at"]),
                )
                for r in rows
            ]

    def trade_logs(self) -> list[TradeLogOut]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, strategy_id, trade_id, stage, result, detail
                FROM trade_logs
                ORDER BY timestamp DESC, id DESC
                """
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

    def portfolio_summary(self) -> PortfolioSummaryOut:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """
                SELECT net_liquidation, available_funds, daily_pnl, updated_at
                FROM portfolio_snapshots
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="portfolio summary not found")
            return PortfolioSummaryOut(
                net_liquidation=row["net_liquidation"],
                available_funds=row["available_funds"],
                daily_pnl=row["daily_pnl"],
                updated_at=parse_iso(row["updated_at"]) or utcnow(),
            )

    def positions(
        self,
        sec_type: str | None = None,
        symbol: str | None = None,
    ) -> list[PositionItemOut]:
        with self._lock, self._conn() as conn:
            sql = """
                SELECT sec_type, symbol, position_qty, position_unit, avg_price, last_price,
                       market_value, unrealized_pnl, updated_at
                FROM positions
                WHERE 1=1
            """
            params: list[Any] = []
            if sec_type:
                sql += " AND sec_type = ?"
                params.append(sec_type)
            if symbol:
                sql += " AND symbol = ?"
                params.append(symbol.upper())
            sql += " ORDER BY symbol ASC"
            rows = conn.execute(sql, params).fetchall()
            return [
                PositionItemOut(
                    sec_type=r["sec_type"],
                    symbol=r["symbol"],
                    position_qty=r["position_qty"],
                    position_unit=r["position_unit"],
                    avg_price=r["avg_price"],
                    last_price=r["last_price"],
                    market_value=r["market_value"],
                    unrealized_pnl=r["unrealized_pnl"],
                    updated_at=parse_iso(r["updated_at"]) or utcnow(),
                )
                for r in rows
            ]


store = SQLiteStore()
