from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_app_config
from app.db import get_connection, init_db
from app.market_config import resolve_market_profile


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dumps_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _table_exists(cur, table_name: str) -> bool:  # type: ignore[no-untyped-def]
    row = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _random_hex4() -> str:
    return uuid4().hex[:4].upper()


def _generate_sample_strategy_id(*, used: set[str]) -> str:
    for _ in range(128):
        candidate = f"S-MP-{_random_hex4()}"
        if candidate in used:
            continue
        used.add(candidate)
        return candidate
    raise RuntimeError("failed to generate unique sample strategy id")


def seed(db_path: str | None = None, *, clean_all: bool = True) -> None:
    path = init_db(db_path=db_path)
    now = datetime.now(timezone.utc)
    default_account_code = _normalize_optional_text(load_app_config().ib_gateway.account_code)
    legacy_sample_ids = ["SMP-A", "SMP-B0", "SMP-B1", "SMP-C", "SMP-D", "SMP-E"]
    used_sample_ids: set[str] = set()
    sample_id_map = {
        legacy_id: _generate_sample_strategy_id(used=used_sample_ids)
        for legacy_id in legacy_sample_ids
    }

    def _sample_id(value: str | None) -> str | None:
        if value is None:
            return None
        return sample_id_map.get(value, value)

    def _replace_sample_ids(text: str) -> str:
        replaced = text
        for legacy_id, sample_id in sample_id_map.items():
            replaced = replaced.replace(legacy_id, sample_id)
        return replaced

    strategies = [
        {
            "id": "SMP-B1",
            "idempotency_key": "smp-b1-v1",
            "description": "若 SLV 相对激活后最高价回撤达到 10%，卖出 100 股。",
            "market": "US_STOCK",
            "trade_type": "sell",
            "currency": "USD",
            "upstream_only_activation": 1,
            "expire_mode": "relative",
            "expire_in_seconds": 172800,
            "expire_at": None,
            "status": "ORDER_SUBMITTED",
            "condition_logic": "AND",
            "conditions_json": [
                {
                    "condition_id": "c1",
                    "condition_nl": "当 SLV 相对激活后最高价回撤达到 10% 时触发。",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "DRAWDOWN_PCT",
                    "trigger_mode": "LEVEL_INSTANT",
                    "evaluation_window": "5m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 0.1,
                    "product": "SLV",
                }
            ],
            "trade_action_json": {
                "action_type": "STOCK_TRADE",
                "symbol": "SLV",
                "side": "SELL",
                "quantity": 100,
                "order_type": "MKT",
                "tif": "DAY",
                "allow_overnight": False,
                "cancel_on_expiry": False,
            },
            "next_strategy_id": None,
            "next_strategy_note": None,
            "next_strategy_activation_mode": "IMMEDIATE",
            "upstream_strategy_id": "SMP-B0",
            "anchor_price": 101.24,
            "activated_at": to_iso(now - timedelta(hours=1, minutes=15)),
            "logical_activated_at": to_iso(now - timedelta(hours=1, minutes=15)),
            "created_at": to_iso(now - timedelta(hours=6)),
            "updated_at": to_iso(now - timedelta(minutes=5)),
            "version": 1,
            "symbols": [("SLV", "sell")],
        },
        {
            "id": "SMP-B0",
            "idempotency_key": "smp-b0-v1",
            "description": "当 SLV 价格触及 100 美元时，激活回撤 10% 卖出策略。",
            "market": "US_STOCK",
            "trade_type": "buy",
            "currency": "USD",
            "upstream_only_activation": 1,
            "expire_mode": "absolute",
            "expire_in_seconds": None,
            "expire_at": to_iso(now + timedelta(days=2)),
            "status": "FILLED",
            "condition_logic": "AND",
            "conditions_json": [
                {
                    "condition_id": "c1",
                    "condition_nl": "当 SLV 价格大于等于 100 美元时触发。",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "PRICE",
                    "trigger_mode": "LEVEL_INSTANT",
                    "evaluation_window": "1m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 100.0,
                    "product": "SLV",
                }
            ],
            "trade_action_json": None,
            "next_strategy_id": "SMP-B1",
            "next_strategy_note": "激活回撤 10% 卖出策略",
            "next_strategy_activation_mode": "IMMEDIATE",
            "upstream_strategy_id": None,
            "anchor_price": None,
            "activated_at": to_iso(now - timedelta(hours=2)),
            "logical_activated_at": to_iso(now - timedelta(hours=2)),
            "created_at": to_iso(now - timedelta(hours=8)),
            "updated_at": to_iso(now - timedelta(hours=1)),
            "version": 1,
            "symbols": [("SLV", "buy")],
        },
        {
            "id": "SMP-C",
            "idempotency_key": "smp-c-v1",
            "description": "当 QQQ 相对 SPY 成交量更高且价差满足阈值时执行调仓，完成后激活回补策略。",
            "market": "US_STOCK",
            "trade_type": "switch",
            "currency": "USD",
            "upstream_only_activation": 0,
            "expire_mode": "relative",
            "expire_in_seconds": 259200,
            "expire_at": None,
            "status": "ACTIVE",
            "condition_logic": "AND",
            "conditions_json": [
                {
                    "condition_id": "c1",
                    "condition_nl": "当 volume(QQQ)/volume(SPY) >= 1.1 时满足条件。",
                    "condition_type": "PAIR_PRODUCTS",
                    "metric": "VOLUME_RATIO",
                    "trigger_mode": "LEVEL_CONFIRM",
                    "evaluation_window": "1d",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 1.1,
                    "product": "QQQ",
                    "product_b": "SPY",
                },
                {
                    "condition_id": "c2",
                    "condition_nl": "当 price(QQQ)-price(SPY) <= -120 时满足条件。",
                    "condition_type": "PAIR_PRODUCTS",
                    "metric": "SPREAD",
                    "trigger_mode": "LEVEL_CONFIRM",
                    "evaluation_window": "5m",
                    "window_price_basis": "CLOSE",
                    "operator": "<=",
                    "value": -120.0,
                    "product": "QQQ",
                    "product_b": "SPY",
                },
            ],
            "trade_action_json": {
                "action_type": "STOCK_TRADE",
                "symbol": "QQQ",
                "side": "BUY",
                "quantity": 50,
                "order_type": "LMT",
                "limit_price": 450.5,
                "tif": "DAY",
                "allow_overnight": False,
                "cancel_on_expiry": False,
            },
            "next_strategy_id": "SMP-A",
            "next_strategy_note": "交易完成后激活回补策略",
            "next_strategy_activation_mode": "AFTER_TRADE_COMPLETED",
            "upstream_strategy_id": None,
            "anchor_price": None,
            "activated_at": to_iso(now - timedelta(hours=4)),
            "logical_activated_at": to_iso(now - timedelta(hours=4)),
            "created_at": to_iso(now - timedelta(days=1)),
            "updated_at": to_iso(now - timedelta(minutes=2)),
            "version": 1,
            "symbols": [("SPY", "sell"), ("QQQ", "buy"), ("VIX", "ref")],
        },
        {
            "id": "SMP-D",
            "idempotency_key": "smp-d-v1",
            "description": "期货展期：满足组合条件时，将 SIH6 平仓并开仓 SIK6，发单后激活下游监测策略。",
            "market": "COMEX_FUTURES",
            "trade_type": "spread",
            "currency": "USD",
            "upstream_only_activation": 0,
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "expire_at": None,
            "status": "ORDER_SUBMITTED",
            "condition_logic": "AND",
            "conditions_json": [
                {
                    "condition_id": "c1",
                    "condition_nl": "当近远月合约成交量比达到 1.2 时触发。",
                    "condition_type": "PAIR_PRODUCTS",
                    "metric": "VOLUME_RATIO",
                    "trigger_mode": "LEVEL_CONFIRM",
                    "evaluation_window": "1d",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 1.2,
                    "product": "SIH6",
                    "product_b": "SIK6",
                },
                {
                    "condition_id": "c2",
                    "condition_nl": "当近远月价差大于等于 0.2 时触发。",
                    "condition_type": "PAIR_PRODUCTS",
                    "metric": "SPREAD",
                    "trigger_mode": "LEVEL_CONFIRM",
                    "evaluation_window": "5m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 0.2,
                    "product": "SIH6",
                    "product_b": "SIK6",
                },
            ],
            "trade_action_json": {
                "action_type": "FUT_ROLL",
                "symbol": "SI",
                "quantity": 2,
                "close_contract": "SIH6",
                "open_contract": "SIK6",
                "close_order_type": "MKT",
                "open_order_type": "MKT",
                "max_leg_slippage_usd": 150.0,
                "tif": "DAY",
                "allow_overnight": False,
                "cancel_on_expiry": False,
            },
            "next_strategy_id": "SMP-E",
            "next_strategy_note": "交易指令发送后激活下游展期监测策略",
            "next_strategy_activation_mode": "AFTER_TRADE_SUBMITTED",
            "upstream_strategy_id": None,
            "anchor_price": None,
            "activated_at": to_iso(now - timedelta(hours=3)),
            "logical_activated_at": to_iso(now - timedelta(hours=3)),
            "created_at": to_iso(now - timedelta(days=1, hours=2)),
            "updated_at": to_iso(now - timedelta(minutes=7)),
            "version": 1,
            "symbols": [("SIH6", "close"), ("SIK6", "open"), ("SI", "ref")],
        },
        {
            "id": "SMP-A",
            "idempotency_key": "smp-a-v1",
            "description": "当 SLV 价格 <= 60 美元时，买入 100 股。",
            "market": "US_STOCK",
            "trade_type": "buy",
            "currency": "USD",
            "upstream_only_activation": 0,
            "expire_mode": "absolute",
            "expire_in_seconds": None,
            "expire_at": to_iso(now + timedelta(days=1)),
            "status": "PENDING_ACTIVATION",
            "condition_logic": "AND",
            "conditions_json": [
                {
                    "condition_id": "c1",
                    "condition_nl": "当 SLV 价格小于等于 60 美元时触发。",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "PRICE",
                    "trigger_mode": "LEVEL_INSTANT",
                    "evaluation_window": "1m",
                    "window_price_basis": "CLOSE",
                    "operator": "<=",
                    "value": 60.0,
                    "product": "SLV",
                }
            ],
            "trade_action_json": {
                "action_type": "STOCK_TRADE",
                "symbol": "SLV",
                "side": "BUY",
                "quantity": 100,
                "order_type": "LMT",
                "limit_price": 59.8,
                "tif": "DAY",
                "allow_overnight": False,
                "cancel_on_expiry": False,
            },
            "next_strategy_id": None,
            "next_strategy_note": None,
            "next_strategy_activation_mode": "IMMEDIATE",
            "upstream_strategy_id": "SMP-C",
            "anchor_price": None,
            "activated_at": None,
            "logical_activated_at": None,
            "created_at": to_iso(now - timedelta(hours=1)),
            "updated_at": to_iso(now - timedelta(minutes=30)),
            "version": 1,
            "symbols": [("SLV", "buy")],
        },
        {
            "id": "SMP-E",
            "idempotency_key": "smp-e-v1",
            "description": "下游策略：展期发单后开始监测，若 SIU6 回撤达 5% 则减仓 1 手。",
            "market": "COMEX_FUTURES",
            "trade_type": "close",
            "currency": "USD",
            "upstream_only_activation": 1,
            "expire_mode": "relative",
            "expire_in_seconds": 86400,
            "expire_at": None,
            "status": "ACTIVE",
            "condition_logic": "AND",
            "conditions_json": [
                {
                    "condition_id": "c1",
                    "condition_nl": "当 SIU6 相对激活后最高价回撤达到 5% 时触发。",
                    "condition_type": "SINGLE_PRODUCT",
                    "metric": "DRAWDOWN_PCT",
                    "trigger_mode": "LEVEL_INSTANT",
                    "evaluation_window": "5m",
                    "window_price_basis": "CLOSE",
                    "operator": ">=",
                    "value": 0.05,
                    "product": "SIU6",
                }
            ],
            "trade_action_json": {
                "action_type": "FUT_POSITION",
                "symbol": "SI",
                "contract": "SIU6",
                "position_effect": "CLOSE",
                "side": "SELL",
                "quantity": 1,
                "order_type": "MKT",
                "tif": "DAY",
                "allow_overnight": False,
                "cancel_on_expiry": False,
            },
            "next_strategy_id": None,
            "next_strategy_note": None,
            "next_strategy_activation_mode": "IMMEDIATE",
            "upstream_strategy_id": "SMP-D",
            "anchor_price": 31.42,
            "activated_at": to_iso(now - timedelta(minutes=8)),
            "logical_activated_at": to_iso(now - timedelta(minutes=9)),
            "created_at": to_iso(now - timedelta(hours=7)),
            "updated_at": to_iso(now - timedelta(minutes=1)),
            "version": 1,
            "symbols": [("SIU6", "close"), ("SI", "ref")],
        },
    ]

    for strategy in strategies:
        strategy["id"] = _sample_id(str(strategy["id"]))
        strategy["next_strategy_id"] = _sample_id(_normalize_optional_text(strategy.get("next_strategy_id")))
        strategy["upstream_strategy_id"] = _sample_id(_normalize_optional_text(strategy.get("upstream_strategy_id")))
        strategy["next_strategy_activation_mode"] = (
            str(strategy.get("next_strategy_activation_mode") or "IMMEDIATE").strip().upper() or "IMMEDIATE"
        )

    for strategy in strategies:
        market = str(strategy.get("market") or "US_STOCK").strip().upper() or "US_STOCK"
        strategy["market"] = market
        trade_action_json = strategy.get("trade_action_json")
        if isinstance(trade_action_json, dict):
            enriched_action = dict(trade_action_json)
            enriched_action["market"] = market
            enriched_action["account_code"] = default_account_code
            strategy["trade_action_json"] = enriched_action

    with get_connection(path) as conn:
        cur = conn.cursor()

        if clean_all:
            # Reset all runtime rows first, then insert a clean sample dataset.
            cur.execute("DELETE FROM condition_states")
            cur.execute("DELETE FROM strategy_runs")
            cur.execute("DELETE FROM trade_logs")
            cur.execute("DELETE FROM trade_instructions")
            cur.execute("DELETE FROM orders")
            if _table_exists(cur, "strategy_activations"):
                cur.execute("DELETE FROM strategy_activations")
            cur.execute("DELETE FROM strategy_events")
            cur.execute("DELETE FROM strategy_symbols")
            cur.execute("DELETE FROM strategies")
            cur.execute("DELETE FROM positions")
            cur.execute("DELETE FROM portfolio_snapshots")
        else:
            # Only refresh S-MP-* sample rows and keep non-sample runtime data.
            cur.execute(
                "DELETE FROM condition_states WHERE strategy_id LIKE 'S-MP-%'"
            )
            cur.execute(
                "DELETE FROM strategy_runs WHERE strategy_id LIKE 'S-MP-%'"
            )
            cur.execute(
                "DELETE FROM orders WHERE strategy_id LIKE 'S-MP-%' OR id LIKE 'T-SMP-%'"
            )
            cur.execute(
                "DELETE FROM trade_logs WHERE strategy_id LIKE 'S-MP-%' OR trade_id LIKE 'T-SMP-%'"
            )
            cur.execute(
                "DELETE FROM trade_instructions WHERE strategy_id LIKE 'S-MP-%' OR trade_id LIKE 'T-SMP-%'"
            )
            if _table_exists(cur, "strategy_activations"):
                cur.execute(
                    """
                    DELETE FROM strategy_activations
                    WHERE from_strategy_id LIKE 'S-MP-%'
                       OR to_strategy_id LIKE 'S-MP-%'
                       OR trigger_event_id IN (
                           SELECT id FROM strategy_events WHERE strategy_id LIKE 'S-MP-%'
                       )
                    """
                )
            cur.execute(
                "DELETE FROM strategy_events WHERE strategy_id LIKE 'S-MP-%'"
            )
            cur.execute(
                "DELETE FROM strategy_symbols WHERE strategy_id LIKE 'S-MP-%'"
            )
            cur.execute(
                "DELETE FROM strategies WHERE id LIKE 'S-MP-%'"
            )

        # Insert base strategy rows first, then fill chain pointers to avoid FK order dependency.
        for s in strategies:
            market = str(s["market"])
            profile = resolve_market_profile(market, str(s["trade_type"]))
            cur.execute(
                """
                INSERT INTO strategies (
                    id, idempotency_key, description, market, sec_type, exchange, trade_type, currency,
                    upstream_only_activation, expire_mode, expire_in_seconds, expire_at,
                    status, condition_logic, conditions_json, trade_action_json,
                    next_strategy_id, next_strategy_note, next_strategy_activation_mode,
                    upstream_strategy_id, anchor_price,
                    activated_at, logical_activated_at, created_at, updated_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    s["id"],
                    s["idempotency_key"],
                    s["description"],
                    market,
                    profile.sec_type,
                    profile.exchange,
                    s["trade_type"],
                    s["currency"],
                    s["upstream_only_activation"],
                    s["expire_mode"],
                    s["expire_in_seconds"],
                    s["expire_at"],
                    s["status"],
                    s["condition_logic"],
                    dumps_json(s["conditions_json"]),
                    dumps_json(s["trade_action_json"]) if s["trade_action_json"] is not None else None,
                    None,
                    None,
                    str(s.get("next_strategy_activation_mode") or "IMMEDIATE").strip().upper(),
                    None,
                    s["anchor_price"],
                    s["activated_at"],
                    s["logical_activated_at"],
                    s["created_at"],
                    s["updated_at"],
                    s["version"],
                ),
            )
            for idx, (code, sym_trade_type) in enumerate(s["symbols"], start=1):
                cur.execute(
                    """
                    INSERT INTO strategy_symbols (
                        strategy_id, position, code, trade_type, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (s["id"], idx, code, sym_trade_type, s["created_at"]),
                )

        for s in strategies:
            if (
                s["next_strategy_id"] is None
                and s["next_strategy_note"] is None
                and str(s.get("next_strategy_activation_mode") or "IMMEDIATE").strip().upper() == "IMMEDIATE"
                and s["upstream_strategy_id"] is None
            ):
                continue
            cur.execute(
                """
                UPDATE strategies
                SET next_strategy_id = ?,
                    next_strategy_note = ?,
                    next_strategy_activation_mode = ?,
                    upstream_strategy_id = ?
                WHERE id = ?
                """,
                (
                    s["next_strategy_id"],
                    s["next_strategy_note"],
                    str(s.get("next_strategy_activation_mode") or "IMMEDIATE").strip().upper(),
                    s["upstream_strategy_id"],
                    s["id"],
                ),
            )

        event_rows_legacy = [
            ("SMP-B0", to_iso(now - timedelta(hours=1, minutes=20)), "TRIGGERED", "触发条件满足：SLV >= 100"),
            ("SMP-B0", to_iso(now - timedelta(hours=1, minutes=19)), "DOWNSTREAM_ACTIVATED", "激活下游策略 SMP-B1"),
            ("SMP-B1", to_iso(now - timedelta(hours=1, minutes=15)), "ACTIVATED", "由上游策略 SMP-B0 激活"),
            ("SMP-B1", to_iso(now - timedelta(minutes=50)), "ORDER_SUBMITTED", "提交卖出订单 T-SMP-0001"),
            ("SMP-C", to_iso(now - timedelta(minutes=12)), "CONDITION_EVALUATED", "条件组状态：MONITORING"),
            (
                "SMP-C",
                to_iso(now - timedelta(minutes=11)),
                "ACTIONS_UPDATED",
                "已配置下游策略 SMP-A（next_strategy_activation_mode=AFTER_TRADE_COMPLETED）",
            ),
            ("SMP-D", to_iso(now - timedelta(minutes=9)), "ORDER_SUBMITTED", "提交展期订单 T-SMP-0002"),
            (
                "SMP-D",
                to_iso(now - timedelta(minutes=8)),
                "DOWNSTREAM_ACTIVATED",
                "已激活下游策略 SMP-E（next_strategy_activation_mode=AFTER_TRADE_SUBMITTED）",
            ),
            ("SMP-E", to_iso(now - timedelta(minutes=8)), "ACTIVATED", "由上游策略 SMP-D 激活"),
            ("SMP-A", to_iso(now - timedelta(minutes=25)), "CREATED", "策略已创建，等待上游触发后激活"),
        ]
        event_rows = [
            (
                _sample_id(strategy_id),
                timestamp,
                event_type,
                _replace_sample_ids(detail),
            )
            for strategy_id, timestamp, event_type, detail in event_rows_legacy
        ]
        cur.executemany(
            """
            INSERT INTO strategy_events (strategy_id, timestamp, event_type, detail)
            VALUES (?, ?, ?, ?)
            """,
            event_rows,
        )

        condition_state_rows_legacy = [
            ("SMP-B1", "c1", "FALSE", 0.073, to_iso(now - timedelta(seconds=20)), to_iso(now - timedelta(seconds=20))),
            ("SMP-C", "c1", "TRUE", 1.14, to_iso(now - timedelta(seconds=20)), to_iso(now - timedelta(seconds=20))),
            ("SMP-C", "c2", "FALSE", -95.0, to_iso(now - timedelta(seconds=20)), to_iso(now - timedelta(seconds=20))),
            ("SMP-D", "c1", "TRUE", 1.23, to_iso(now - timedelta(seconds=20)), to_iso(now - timedelta(seconds=20))),
            ("SMP-D", "c2", "TRUE", 0.27, to_iso(now - timedelta(seconds=20)), to_iso(now - timedelta(seconds=20))),
            ("SMP-E", "c1", "FALSE", 0.018, to_iso(now - timedelta(seconds=20)), to_iso(now - timedelta(seconds=20))),
            ("SMP-A", "c1", "NOT_EVALUATED", None, None, to_iso(now - timedelta(minutes=30))),
        ]
        condition_state_rows = [
            (_sample_id(strategy_id), condition_id, state, last_value, last_evaluated_at, updated_at)
            for strategy_id, condition_id, state, last_value, last_evaluated_at, updated_at in condition_state_rows_legacy
        ]
        cur.executemany(
            """
            INSERT INTO condition_states (
                strategy_id, condition_id, state, last_value, last_evaluated_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            condition_state_rows,
        )

        strategy_run_rows_legacy = [
            (
                "SMP-B1",
                {"c1": {"1": to_iso(now - timedelta(minutes=1))}},
                to_iso(now + timedelta(seconds=30)),
                to_iso(now - timedelta(hours=1)),
                to_iso(now - timedelta(seconds=20)),
                0,
                "drawdown_pct=0.073 < threshold=0.1",
                "CONTINUE",
                128,
                {"c1": {"metric": "DRAWDOWN_PCT", "value": 0.073, "threshold": 0.1}},
                to_iso(now - timedelta(seconds=20)),
            ),
            (
                "SMP-C",
                {"c1": {"1": to_iso(now - timedelta(minutes=1))}, "c2": {"1": to_iso(now - timedelta(minutes=1))}},
                to_iso(now + timedelta(seconds=20)),
                to_iso(now - timedelta(hours=4)),
                to_iso(now - timedelta(seconds=20)),
                0,
                "c1=true AND c2=false",
                "CONTINUE",
                322,
                {
                    "c1": {"metric": "VOLUME_RATIO", "value": 1.14, "threshold": 1.1},
                    "c2": {"metric": "SPREAD", "value": -95.0, "threshold": -120.0},
                },
                to_iso(now - timedelta(seconds=20)),
            ),
            (
                "SMP-D",
                {"c1": {"1": to_iso(now - timedelta(minutes=10))}, "c2": {"1": to_iso(now - timedelta(minutes=10))}},
                None,
                to_iso(now - timedelta(hours=3)),
                to_iso(now - timedelta(minutes=9)),
                1,
                "c1=true AND c2=true => TRIGGERED",
                "TRIGGERED",
                204,
                {
                    "c1": {"metric": "VOLUME_RATIO", "value": 1.23, "threshold": 1.2},
                    "c2": {"metric": "SPREAD", "value": 0.27, "threshold": 0.2},
                },
                to_iso(now - timedelta(minutes=9)),
            ),
            (
                "SMP-E",
                {"c1": {"1": to_iso(now - timedelta(minutes=1))}},
                to_iso(now + timedelta(seconds=25)),
                to_iso(now - timedelta(minutes=8)),
                to_iso(now - timedelta(seconds=20)),
                0,
                "drawdown_pct=0.018 < threshold=0.05",
                "CONTINUE",
                39,
                {"c1": {"metric": "DRAWDOWN_PCT", "value": 0.018, "threshold": 0.05}},
                to_iso(now - timedelta(seconds=20)),
            ),
        ]
        strategy_run_rows = [
            (
                _sample_id(strategy_id),
                dumps_json(last_monitoring_data_end_at),
                suggested_next_monitor_at,
                first_evaluated_at,
                evaluated_at,
                condition_met,
                decision_reason,
                last_outcome,
                check_count,
                dumps_json(metrics_json),
                updated_at,
            )
            for (
                strategy_id,
                last_monitoring_data_end_at,
                suggested_next_monitor_at,
                first_evaluated_at,
                evaluated_at,
                condition_met,
                decision_reason,
                last_outcome,
                check_count,
                metrics_json,
                updated_at,
            ) in strategy_run_rows_legacy
        ]
        cur.executemany(
            """
            INSERT INTO strategy_runs (
                strategy_id, last_monitoring_data_end_at, suggested_next_monitor_at,
                first_evaluated_at, evaluated_at, condition_met, decision_reason,
                last_outcome, check_count, metrics_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            strategy_run_rows,
        )

        order_rows_legacy = [
            (
                "T-SMP-0001",
                "T-SMP-0001",
                "SMP-B1",
                "SINGLE",
                1,
                "2812",
                "ORDER_SUBMITTED",
                100,
                None,
                0,
                None,
                dumps_json({"symbol": "SLV", "side": "SELL", "order_type": "MKT", "quantity": 100}),
                to_iso(now - timedelta(minutes=50)),
                to_iso(now - timedelta(minutes=5)),
            ),
            (
                "T-SMP-0002-01",
                "T-SMP-0002",
                "SMP-D",
                "ROLL_CLOSE",
                1,
                "2813",
                "FILLED",
                2,
                31.18,
                2,
                None,
                dumps_json(
                    {
                        "action_type": "FUT_ROLL",
                        "leg_role": "ROLL_CLOSE",
                        "close_contract": "SIH6",
                        "open_contract": "SIK6",
                        "quantity": 2,
                    }
                ),
                to_iso(now - timedelta(minutes=9)),
                to_iso(now - timedelta(minutes=4)),
            ),
            (
                "T-SMP-0002-02",
                "T-SMP-0002",
                "SMP-D",
                "ROLL_OPEN",
                2,
                "2814",
                "ORDER_SUBMITTED",
                2,
                None,
                0,
                None,
                dumps_json(
                    {
                        "action_type": "FUT_ROLL",
                        "leg_role": "ROLL_OPEN",
                        "close_contract": "SIH6",
                        "open_contract": "SIK6",
                        "quantity": 2,
                    }
                ),
                to_iso(now - timedelta(minutes=9)),
                to_iso(now - timedelta(minutes=2)),
            ),
        ]
        order_rows = [
            (
                order_id,
                trade_id,
                _sample_id(strategy_id),
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
                updated_at,
            )
            for (
                order_id,
                trade_id,
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
                updated_at,
            ) in order_rows_legacy
        ]
        order_leg_rows_legacy = [
            ("T-SMP-0002-01", 1, 700001, "SI", "202603", "SELL", 1.0, "COMEX", to_iso(now - timedelta(minutes=9))),
            ("T-SMP-0002-02", 1, 700002, "SI", "202605", "BUY", 1.0, "COMEX", to_iso(now - timedelta(minutes=9))),
        ]
        trade_log_rows_legacy = [
            (to_iso(now - timedelta(minutes=51)), "SMP-B1", "T-SMP-0001", "VERIFICATION", "PASSED", "All verification rules passed"),
            (to_iso(now - timedelta(minutes=50)), "SMP-B1", "T-SMP-0001", "EXECUTION", "ORDER_SUBMITTED", "IB Order #2812, SELL 100 MKT"),
            (to_iso(now - timedelta(minutes=10)), "SMP-D", "T-SMP-0002", "VERIFICATION", "PASSED", "Roll checks passed"),
            (to_iso(now - timedelta(minutes=9)), "SMP-D", "T-SMP-0002", "EXECUTION", "ORDER_SUBMITTED", "Close/Open legs dispatched"),
            (to_iso(now - timedelta(minutes=4)), "SMP-D", "T-SMP-0002", "EXECUTION", "FILLED", "Close leg filled 2/2"),
            (to_iso(now - timedelta(minutes=2)), "SMP-D", "T-SMP-0002", "EXECUTION", "ORDER_SUBMITTED", "Open leg submitted waiting fill"),
        ]
        trade_log_rows = [
            (timestamp, _sample_id(strategy_id), trade_id, stage, result, detail)
            for timestamp, strategy_id, trade_id, stage, result, detail in trade_log_rows_legacy
        ]
        cur.executemany(
            """
            INSERT INTO trade_logs (timestamp, strategy_id, trade_id, stage, result, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            trade_log_rows,
        )

        trade_instruction_rows_legacy = [
            (
                "T-SMP-0001",
                "SMP-B1",
                "SELL 100 SLV MKT, DAY",
                "ORDER_SUBMITTED",
                to_iso(now.replace(hour=16, minute=0, second=0, microsecond=0)),
                to_iso(now - timedelta(minutes=5)),
            ),
            (
                "T-SMP-0002",
                "SMP-D",
                "ROLL SIH6 -> SIK6, qty=2",
                "PARTIAL_FILL",
                to_iso(now.replace(hour=16, minute=0, second=0, microsecond=0)),
                to_iso(now - timedelta(minutes=2)),
            ),
        ]
        trade_instruction_rows = [
            (trade_id, _sample_id(strategy_id), instruction_summary, status, expire_at, updated_at)
            for trade_id, strategy_id, instruction_summary, status, expire_at, updated_at in trade_instruction_rows_legacy
        ]
        cur.executemany(
            """
            INSERT INTO trade_instructions (
                trade_id, strategy_id, instruction_summary, status, expire_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            trade_instruction_rows,
        )

        cur.executemany(
            """
            INSERT INTO orders (
                id, trade_id, strategy_id, leg_role, sequence_no, ib_order_id, status, qty,
                avg_fill_price, filled_qty, error_message, order_payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            order_rows,
        )
        cur.executemany(
            """
            INSERT INTO order_legs (
                order_id, leg_index, con_id, symbol, contract_month, side, ratio, exchange, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            order_leg_rows_legacy,
        )

        cur.execute(
            """
            INSERT INTO portfolio_snapshots (
                net_liquidation, available_funds, daily_pnl, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (128540.72, 43228.10, 1128.34, to_iso(now - timedelta(minutes=1))),
        )

        cur.execute(
            """
            INSERT INTO positions (
                sec_type, symbol, position_qty, position_unit, avg_price, last_price, market_value, unrealized_pnl, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sec_type, symbol) DO UPDATE SET
                position_qty=excluded.position_qty,
                position_unit=excluded.position_unit,
                avg_price=excluded.avg_price,
                last_price=excluded.last_price,
                market_value=excluded.market_value,
                unrealized_pnl=excluded.unrealized_pnl,
                updated_at=excluded.updated_at
            """,
            ("STK", "SLV", 320, "股", 89.37, 90.82, 29062.40, 464.0, to_iso(now - timedelta(minutes=1))),
        )
        cur.execute(
            """
            INSERT INTO positions (
                sec_type, symbol, position_qty, position_unit, avg_price, last_price, market_value, unrealized_pnl, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sec_type, symbol) DO UPDATE SET
                position_qty=excluded.position_qty,
                position_unit=excluded.position_unit,
                avg_price=excluded.avg_price,
                last_price=excluded.last_price,
                market_value=excluded.market_value,
                unrealized_pnl=excluded.unrealized_pnl,
                updated_at=excluded.updated_at
            """,
            ("FUT", "SIH6", 3, "手", 31.26, 31.10, 466500.0, -2400.0, to_iso(now - timedelta(minutes=1))),
        )

        conn.commit()

    print(f"[OK] Seeded sample data into: {path}")
    print("[OK] Sample strategies: " + ", ".join(sorted(sample_id_map.values())))


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed sample data for IBX")
    parser.add_argument(
        "--db-path",
        default=None,
        help="SQLite file path (defaults to IBX_DB_PATH or data/ibx.sqlite3)",
    )
    parser.add_argument(
        "--keep-non-sample",
        action="store_true",
        help="Only refresh S-MP-* rows and keep non-sample runtime data",
    )
    args = parser.parse_args()
    seed(db_path=args.db_path, clean_all=not args.keep_non_sample)


if __name__ == "__main__":
    main()
