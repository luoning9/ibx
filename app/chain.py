from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4


UTC = timezone.utc
TERMINAL_STATUSES: set[str] = {"FILLED", "EXPIRED", "CANCELLED", "FAILED"}
DOWNSTREAM_ACTIVATABLE_STATUSES: set[str] = {"PENDING_ACTIVATION", "VERIFY_FAILED", "PAUSED"}
TRADE_TERMINAL_TO_STRATEGY: dict[str, str] = {
    "FILLED": "FILLED",
    "CANCELLED": "CANCELLED",
    "FAILED": "FAILED",
    "EXPIRED": "EXPIRED",
}


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_iso_utc(dt: datetime) -> str:
    return _to_utc(dt).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_strategy_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    return normalized or None


def _append_event(
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


def activate_downstream_strategy(
    conn: sqlite3.Connection,
    *,
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
        SELECT id, status, expire_mode, expire_in_seconds
        FROM v_strategies_active
        WHERE id = ?
        """,
        (downstream_id,),
    ).fetchone()
    if row is None:
        return False
    if row["status"] in TERMINAL_STATUSES:
        return False
    if row["status"] not in DOWNSTREAM_ACTIVATABLE_STATUSES:
        return False

    activated_at_iso = _to_iso_utc(now)
    expire_at_iso: str | None = None
    if row["expire_mode"] == "relative" and row["expire_in_seconds"]:
        expire_at_iso = _to_iso_utc(now + timedelta(seconds=int(row["expire_in_seconds"])))

    cursor = conn.execute(
        """
        UPDATE strategies
        SET status = 'ACTIVE',
            upstream_only_activation = 1,
            activated_at = ?,
            logical_activated_at = ?,
            expire_at = ?,
            updated_at = ?,
            version = version + 1
        WHERE id = ?
          AND status IN ('PENDING_ACTIVATION', 'VERIFY_FAILED', 'PAUSED')
          AND is_deleted = 0
        """,
        (
            activated_at_iso,
            _to_iso_utc(triggered_at),
            expire_at_iso,
            activated_at_iso,
            downstream_id,
        ),
    )
    if cursor.rowcount <= 0:
        return False

    _append_event(
        conn,
        strategy_id=downstream_id,
        event_type="ACTIVATED",
        detail=f"由上游策略 {upstream_strategy_id} 激活",
        ts=now,
    )
    _append_event(
        conn,
        strategy_id=upstream_strategy_id,
        event_type="DOWNSTREAM_ACTIVATED",
        detail=f"已激活下游策略：{downstream_id}",
        ts=now,
    )
    return True


def execute_triggered_strategy(
    conn: sqlite3.Connection,
    *,
    strategy_row: sqlite3.Row,
    now: datetime,
) -> str:
    strategy_id = strategy_row["id"]
    trade_action_json = (
        json.loads(strategy_row["trade_action_json"]) if strategy_row["trade_action_json"] else None
    )
    next_strategy_id = _normalize_strategy_id(strategy_row["next_strategy_id"])

    activate_downstream_strategy(
        conn,
        upstream_strategy_id=strategy_id,
        next_strategy_id=next_strategy_id,
        triggered_at=now,
        now=now,
    )

    now_iso = _to_iso_utc(now)
    if isinstance(trade_action_json, dict):
        trade_id = f"T-{uuid4().hex[:10].upper()}"
        instruction_summary = _build_instruction_summary(trade_action_json)
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
                "ORDER_SUBMITTED",
                None,
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
                "ORDER_SUBMITTED",
                instruction_summary,
            ),
        )
        cursor = conn.execute(
            """
            UPDATE strategies
            SET status = 'ORDER_SUBMITTED', updated_at = ?, version = version + 1
            WHERE id = ? AND status = 'TRIGGERED' AND is_deleted = 0
            """,
            (now_iso, strategy_id),
        )
        if cursor.rowcount > 0:
            _append_event(
                conn,
                strategy_id=strategy_id,
                event_type="ORDER_SUBMITTED",
                detail=f"提交交易指令 {trade_id}",
                ts=now,
            )
            return "ORDER_SUBMITTED"
        return str(strategy_row["status"])

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
            _append_event(
                conn,
                strategy_id=strategy_id,
                event_type="FILLED",
                detail="无交易动作，完成下游激活后结束",
                ts=now,
            )
            return "FILLED"
        return str(strategy_row["status"])

    cursor = conn.execute(
        """
        UPDATE strategies
        SET status = 'FAILED', updated_at = ?, version = version + 1
        WHERE id = ? AND status = 'TRIGGERED' AND is_deleted = 0
        """,
        (now_iso, strategy_id),
    )
    if cursor.rowcount > 0:
        _append_event(
            conn,
            strategy_id=strategy_id,
            event_type="FAILED",
            detail="TRIGGERED 但无 trade_action_json 且无 next_strategy_id",
            ts=now,
        )
        return "FAILED"
    return str(strategy_row["status"])


def sync_order_submitted_strategy_status(
    conn: sqlite3.Connection,
    *,
    strategy_row: sqlite3.Row,
    now: datetime,
) -> str:
    strategy_id = strategy_row["id"]
    row = conn.execute(
        """
        SELECT status
        FROM trade_instructions
        WHERE strategy_id = ?
        ORDER BY updated_at DESC, trade_id DESC
        LIMIT 1
        """,
        (strategy_id,),
    ).fetchone()
    if row is None:
        return str(strategy_row["status"])

    instruction_status = str(row["status"] or "").upper()
    target_status = TRADE_TERMINAL_TO_STRATEGY.get(instruction_status)
    if target_status is None:
        return str(strategy_row["status"])

    now_iso = _to_iso_utc(now)
    cursor = conn.execute(
        """
        UPDATE strategies
        SET status = ?, updated_at = ?, version = version + 1
        WHERE id = ? AND status = 'ORDER_SUBMITTED' AND is_deleted = 0
        """,
        (target_status, now_iso, strategy_id),
    )
    if cursor.rowcount > 0:
        _append_event(
            conn,
            strategy_id=strategy_id,
            event_type=target_status,
            detail=f"根据交易指令状态回写：{instruction_status}",
            ts=now,
        )
        return target_status
    return str(strategy_row["status"])
