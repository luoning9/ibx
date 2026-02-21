from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from .models import (
    ActiveTradeInstructionOut,
    Capabilities,
    CapabilityReasons,
    ConditionItem,
    ConditionRuntimeItem,
    ConditionState,
    ControlResponse,
    EventLogItem,
    NextStrategyProjection,
    PortfolioSummaryOut,
    PositionItemOut,
    StrategySymbolItem,
    StrategyActionsPutIn,
    StrategyBasicPatchIn,
    StrategyConditionsPutIn,
    StrategyCreateIn,
    StrategyDetailOut,
    StrategyStatus,
    StrategySummaryOut,
    TradeActionRuntime,
    TradeLogOut,
    TriggerGroupStatus,
    _validate_trade_symbol_combo,
)

TERMINAL_STATUSES: set[str] = {"FILLED", "EXPIRED", "CANCELLED", "FAILED"}
EDITABLE_STATUSES: set[str] = {"PENDING_ACTIVATION", "PAUSED"}
STOCK_TRADE_TYPES: set[str] = {"buy", "sell", "switch"}
FUT_TRADE_TYPES: set[str] = {"open", "close", "spread"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _generate_condition_nl(condition: ConditionItem) -> str:
    if condition.condition_type == "SINGLE_PRODUCT":
        subject = condition.product or "标的"
    else:
        subject = f"{condition.product_a or 'A'} / {condition.product_b or 'B'}"
    return (
        f"当 {subject} 的 {condition.metric} 在 {condition.evaluation_window} 窗口满足 "
        f"{condition.trigger_mode} {condition.operator} {condition.value} 时触发。"
    )


@dataclass
class StrategyRecord:
    id: str
    description: str
    trade_type: str
    symbols: list[StrategySymbolItem]
    currency: str
    upstream_only_activation: bool
    expire_mode: str
    expire_in_seconds: int | None
    expire_at: datetime | None
    status: StrategyStatus
    created_at: datetime
    updated_at: datetime
    activated_at: datetime | None = None
    logical_activated_at: datetime | None = None
    idempotency_key: str | None = None

    condition_logic: str = "AND"
    conditions_json: list[ConditionItem] = field(default_factory=list)
    conditions_runtime: list[ConditionRuntimeItem] = field(default_factory=list)

    trade_action_json: dict[str, Any] | None = None
    trade_action_runtime: TradeActionRuntime = field(
        default_factory=lambda: TradeActionRuntime(trade_status="NOT_SET")
    )
    next_strategy_id: str | None = None
    next_strategy_note: str | None = None
    anchor_price: float | None = None


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


class InMemoryStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._strategies: dict[str, StrategyRecord] = {}
        self._events_by_strategy: dict[str, list[EventLogItem]] = {}
        self._global_events: list[EventLogItem] = []
        self._active_trade_instructions: list[ActiveTradeInstructionOut] = []
        self._trade_logs: list[TradeLogOut] = []
        self._positions: list[PositionItemOut] = []
        self._portfolio_summary: PortfolioSummaryOut | None = None
        self._seed()

    def _seed(self) -> None:
        now = utcnow()

        s0 = StrategyRecord(
            id="S0",
            description="当 SLV 价格触及 100 美元时，激活回撤 10% 卖出策略（不直接下单）。",
            trade_type="buy",
            symbols=[StrategySymbolItem(code="SLV", trade_type="buy")],
            currency="USD",
            upstream_only_activation=False,
            expire_mode="absolute",
            expire_in_seconds=None,
            expire_at=now + timedelta(days=2),
            status="PENDING_ACTIVATION",
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=1, minutes=30),
        )
        s1 = StrategyRecord(
            id="S1",
            description="若 SLV 相对锚定价回撤达到 10%，卖出 100 股，并激活 20% 回撤策略。",
            trade_type="sell",
            symbols=[StrategySymbolItem(code="SLV", trade_type="sell")],
            currency="USD",
            upstream_only_activation=True,
            expire_mode="relative",
            expire_in_seconds=172800,
            expire_at=now + timedelta(days=2),
            status="ACTIVE",
            created_at=now - timedelta(hours=3),
            updated_at=now - timedelta(minutes=45),
            activated_at=now - timedelta(hours=1, minutes=10),
            logical_activated_at=now - timedelta(hours=1, minutes=10),
            condition_logic="AND",
            trade_action_json={
                "action_type": "STOCK_TRADE",
                "symbol": "SLV",
                "side": "SELL",
                "quantity": 100,
                "order_type": "MKT",
                "tif": "DAY",
                "allow_overnight": False,
                "cancel_on_expiry": False,
            },
            trade_action_runtime=TradeActionRuntime(
                trade_status="ORDER_SUBMITTED",
                trade_id="T-20260220-00041",
            ),
            next_strategy_id="S2",
            next_strategy_note="SLV 回撤达到 20% 时再卖出 100 股。",
            anchor_price=101.24,
        )

        c1 = ConditionItem(
            condition_id="c1",
            condition_type="SINGLE_PRODUCT",
            metric="DRAWDOWN_PCT",
            trigger_mode="LEVEL",
            evaluation_window="5m",
            window_price_basis="CLOSE",
            operator=">=",
            value=0.1,
            product="SLV",
            price_reference="HIGHEST_SINCE_ACTIVATION",
            condition_nl="当 SLV 相对激活后最高价回撤达到 10% 时触发。",
        )
        s1.conditions_json = [c1]
        s1.conditions_runtime = [
            ConditionRuntimeItem(
                condition_id="c1",
                state="FALSE",
                last_value=0.06,
                last_evaluated_at=now - timedelta(seconds=20),
            )
        ]

        self._strategies[s0.id] = s0
        self._strategies[s1.id] = s1
        self._events_by_strategy[s0.id] = []
        self._events_by_strategy[s1.id] = []
        self._append_event(
            s1.id,
            "ACTIVATED",
            "由上游策略 S0 激活，写入 anchor_price=101.24",
            now - timedelta(hours=1, minutes=10),
        )
        self._append_event(
            s1.id,
            "ORDER_SUBMITTED",
            "已发送 IB 订单：trade_id=T-20260220-00041，SELL 100 MKT",
            now - timedelta(minutes=45),
        )

        self._active_trade_instructions = [
            ActiveTradeInstructionOut(
                updated_at=now - timedelta(minutes=20),
                strategy_id="S1",
                trade_id="T-20260220-00041",
                instruction_summary="SELL 100 SLV LMT @ 91.20, DAY",
                status="ORDER_SUBMITTED",
                expire_at=now.replace(hour=16, minute=0, second=0, microsecond=0),
            ),
            ActiveTradeInstructionOut(
                updated_at=now - timedelta(minutes=19),
                strategy_id="S7",
                trade_id="T-20260220-00042",
                instruction_summary="ROLL SIH6 -> SIK6, qty=2",
                status="PARTIAL_FILL",
                expire_at=now.replace(hour=16, minute=0, second=0, microsecond=0),
            ),
        ]

        self._trade_logs = [
            TradeLogOut(
                timestamp=now - timedelta(minutes=21),
                strategy_id="S1",
                trade_id="T-20260220-00041",
                stage="VERIFICATION",
                result="PASSED",
                detail="All verification rules passed",
            ),
            TradeLogOut(
                timestamp=now - timedelta(minutes=20),
                strategy_id="S1",
                trade_id="T-20260220-00041",
                stage="EXECUTION",
                result="ORDER_SUBMITTED",
                detail="IB Order #2812, SELL 100 MKT",
            ),
        ]

        self._portfolio_summary = PortfolioSummaryOut(
            net_liquidation=128540.72,
            available_funds=43228.10,
            daily_pnl=1128.34,
            updated_at=now - timedelta(minutes=1),
        )
        self._positions = [
            PositionItemOut(
                sec_type="STK",
                symbol="SLV",
                position_qty=320,
                position_unit="股",
                avg_price=89.37,
                last_price=90.82,
                market_value=29062.40,
                unrealized_pnl=464.00,
                updated_at=now - timedelta(minutes=1),
            ),
            PositionItemOut(
                sec_type="FUT",
                symbol="SIH6",
                position_qty=3,
                position_unit="手",
                avg_price=31.26,
                last_price=31.10,
                market_value=466500.00,
                unrealized_pnl=-2400.00,
                updated_at=now - timedelta(minutes=1),
            ),
        ]

    def _append_event(
        self, strategy_id: str, event_type: str, detail: str, ts: datetime | None = None
    ) -> None:
        event = EventLogItem(
            timestamp=ts or utcnow(),
            event_type=event_type,
            detail=detail,
            strategy_id=strategy_id,
        )
        self._events_by_strategy.setdefault(strategy_id, []).append(event)
        self._global_events.append(event)

    def _get(self, strategy_id: str) -> StrategyRecord:
        strategy = self._strategies.get(strategy_id)
        if not strategy:
            raise HTTPException(status_code=404, detail=f"strategy {strategy_id} not found")
        return strategy

    def _editable(self, status: StrategyStatus) -> tuple[bool, str | None]:
        if status in EDITABLE_STATUSES:
            return True, None
        return False, "仅 PENDING_ACTIVATION / PAUSED 可编辑；ACTIVE 请先暂停。"

    def _capabilities(self, s: StrategyRecord) -> tuple[Capabilities, CapabilityReasons]:
        can_activate = s.status == "PENDING_ACTIVATION"
        activate_reason = None
        if s.upstream_only_activation:
            can_activate = False
            activate_reason = "upstream_only_activation=true"
        elif not s.conditions_json:
            can_activate = False
            activate_reason = "触发条件未配置"
        elif not s.trade_action_json and not s.next_strategy_id:
            can_activate = False
            activate_reason = "后续动作未配置"

        can_pause = s.status == "ACTIVE"
        can_resume = s.status == "PAUSED"
        can_cancel = s.status not in TERMINAL_STATUSES

        caps = Capabilities(
            can_activate=can_activate,
            can_pause=can_pause,
            can_resume=can_resume,
            can_cancel=can_cancel,
        )
        reasons = CapabilityReasons(
            can_activate=activate_reason,
            can_pause=None if can_pause else "仅 ACTIVE 可暂停",
            can_resume=None if can_resume else "仅 PAUSED 可恢复",
            can_cancel=None if can_cancel else "终态策略不可取消",
        )
        return caps, reasons

    def _trigger_group_status(self, s: StrategyRecord) -> TriggerGroupStatus:
        if not s.conditions_json:
            return "NOT_CONFIGURED"
        if s.status == "EXPIRED":
            return "EXPIRED"
        if s.status in {"TRIGGERED", "ORDER_SUBMITTED", "FILLED"}:
            return "TRIGGERED"
        return "MONITORING"

    def _to_summary(self, s: StrategyRecord) -> StrategySummaryOut:
        caps, _ = self._capabilities(s)
        return StrategySummaryOut(
            id=s.id,
            status=s.status,
            description=s.description,
            updated_at=s.updated_at,
            expire_at=s.expire_at,
            capabilities=caps,
        )

    def _to_detail(self, s: StrategyRecord) -> StrategyDetailOut:
        editable, editable_reason = self._editable(s.status)
        capabilities, capability_reasons = self._capabilities(s)

        next_strategy = None
        if s.next_strategy_id:
            downstream = self._strategies.get(s.next_strategy_id)
            next_strategy = NextStrategyProjection(
                id=s.next_strategy_id,
                description=(downstream.description if downstream else s.next_strategy_note),
                status=(downstream.status if downstream else "UNKNOWN"),
            )

        return StrategyDetailOut(
            id=s.id,
            description=s.description,
            trade_type=s.trade_type,  # type: ignore[arg-type]
            symbols=s.symbols,
            currency=s.currency,
            upstream_only_activation=s.upstream_only_activation,
            activated_at=s.activated_at,
            logical_activated_at=s.logical_activated_at,
            expire_in_seconds=s.expire_in_seconds,
            expire_at=s.expire_at,
            status=s.status,
            editable=editable,
            editable_reason=editable_reason,
            capabilities=capabilities,
            capability_reasons=capability_reasons,
            condition_logic=s.condition_logic,  # type: ignore[arg-type]
            conditions_json=s.conditions_json,
            trigger_group_status=self._trigger_group_status(s),
            conditions_runtime=s.conditions_runtime,
            trade_action_json=s.trade_action_json,
            trade_action_runtime=s.trade_action_runtime,
            next_strategy=next_strategy,
            anchor_price=s.anchor_price,
            events=self._events_by_strategy.get(s.id, []),
            created_at=s.created_at,
            updated_at=s.updated_at,
        )

    def list_strategies(self) -> list[StrategySummaryOut]:
        with self._lock:
            return [self._to_summary(s) for s in self._strategies.values()]

    def get_strategy(self, strategy_id: str) -> StrategyDetailOut:
        with self._lock:
            return self._to_detail(self._get(strategy_id))

    def create_strategy(self, payload: StrategyCreateIn) -> StrategyDetailOut:
        with self._lock:
            strategy_id = payload.id or f"S-{uuid4().hex[:6].upper()}"
            if strategy_id in self._strategies:
                raise HTTPException(status_code=409, detail=f"strategy {strategy_id} already exists")

            now = utcnow()
            conditions: list[ConditionItem] = []
            runtime: list[ConditionRuntimeItem] = []
            for idx, cond in enumerate(payload.conditions, start=1):
                condition_id = cond.condition_id or f"c{idx}"
                cond = cond.model_copy(
                    update={
                        "condition_id": condition_id,
                        "condition_nl": cond.condition_nl or _generate_condition_nl(cond),
                    }
                )
                conditions.append(cond)
                runtime.append(ConditionRuntimeItem(condition_id=condition_id, state="NOT_EVALUATED"))

            expire_at = payload.expire_at
            if payload.expire_mode == "relative":
                expire_at = None

            _validate_trade_action_compatibility(payload.trade_type, payload.trade_action_json)

            record = StrategyRecord(
                id=strategy_id,
                description=payload.description,
                trade_type=payload.trade_type,
                symbols=payload.symbols,
                currency=payload.currency,
                upstream_only_activation=payload.upstream_only_activation,
                expire_mode=payload.expire_mode,
                expire_in_seconds=payload.expire_in_seconds,
                expire_at=expire_at,
                status="PENDING_ACTIVATION",
                created_at=now,
                updated_at=now,
                idempotency_key=payload.idempotency_key,
                condition_logic=payload.condition_logic,
                conditions_json=conditions,
                conditions_runtime=runtime,
                trade_action_json=payload.trade_action_json,
                trade_action_runtime=TradeActionRuntime(
                    trade_status="NOT_SET" if not payload.trade_action_json else "NOT_TRIGGERED"
                ),
                next_strategy_id=payload.next_strategy_id,
                next_strategy_note=payload.next_strategy_note,
            )
            self._strategies[strategy_id] = record
            self._events_by_strategy[strategy_id] = []
            self._append_event(strategy_id, "CREATED", "策略创建成功")
            return self._to_detail(record)

    def patch_basic(self, strategy_id: str, payload: StrategyBasicPatchIn) -> StrategyDetailOut:
        with self._lock:
            s = self._get(strategy_id)
            if s.status not in EDITABLE_STATUSES:
                raise HTTPException(status_code=409, detail="strategy is not editable")

            fields_set = payload.model_fields_set
            next_trade_type = payload.trade_type if "trade_type" in fields_set else s.trade_type
            next_symbols = payload.symbols if "symbols" in fields_set else s.symbols
            if not next_trade_type:
                raise HTTPException(status_code=422, detail="trade_type cannot be null")
            if not next_symbols:
                raise HTTPException(status_code=422, detail="symbols cannot be null/empty")

            _validate_trade_symbol_combo(next_trade_type, next_symbols)
            _validate_trade_action_compatibility(next_trade_type, s.trade_action_json)

            if "description" in fields_set and payload.description is not None:
                s.description = payload.description
            if "trade_type" in fields_set:
                s.trade_type = next_trade_type
            if "symbols" in fields_set:
                s.symbols = next_symbols
            if "upstream_only_activation" in fields_set and payload.upstream_only_activation is not None:
                s.upstream_only_activation = payload.upstream_only_activation
            if "expire_mode" in fields_set and payload.expire_mode is not None:
                s.expire_mode = payload.expire_mode
                if s.expire_mode == "relative":
                    s.expire_at = None
            if "expire_in_seconds" in fields_set:
                s.expire_in_seconds = payload.expire_in_seconds
            if "expire_at" in fields_set and s.expire_mode == "absolute":
                s.expire_at = payload.expire_at

            s.updated_at = utcnow()
            self._append_event(strategy_id, "BASIC_UPDATED", "已更新基本信息")
            return self._to_detail(s)

    def put_conditions(
        self, strategy_id: str, payload: StrategyConditionsPutIn
    ) -> StrategyDetailOut:
        with self._lock:
            s = self._get(strategy_id)
            if s.status not in EDITABLE_STATUSES:
                raise HTTPException(status_code=409, detail="strategy is not editable")

            conditions: list[ConditionItem] = []
            runtime: list[ConditionRuntimeItem] = []
            for idx, cond in enumerate(payload.conditions, start=1):
                condition_id = cond.condition_id or f"c{idx}"
                cond = cond.model_copy(
                    update={
                        "condition_id": condition_id,
                        "condition_nl": cond.condition_nl or _generate_condition_nl(cond),
                    }
                )
                conditions.append(cond)
                runtime.append(ConditionRuntimeItem(condition_id=condition_id, state="NOT_EVALUATED"))

            s.condition_logic = payload.condition_logic
            s.conditions_json = conditions
            s.conditions_runtime = runtime
            s.updated_at = utcnow()
            self._append_event(strategy_id, "CONDITIONS_UPDATED", "已更新触发条件")
            return self._to_detail(s)

    def put_actions(self, strategy_id: str, payload: StrategyActionsPutIn) -> StrategyDetailOut:
        with self._lock:
            s = self._get(strategy_id)
            if s.status not in EDITABLE_STATUSES:
                raise HTTPException(status_code=409, detail="strategy is not editable")

            _validate_trade_action_compatibility(s.trade_type, payload.trade_action_json)
            s.trade_action_json = payload.trade_action_json
            s.trade_action_runtime = TradeActionRuntime(
                trade_status="NOT_SET" if not payload.trade_action_json else "NOT_TRIGGERED"
            )
            s.next_strategy_id = payload.next_strategy_id
            s.next_strategy_note = payload.next_strategy_note
            s.updated_at = utcnow()
            self._append_event(strategy_id, "ACTIONS_UPDATED", "已更新后续动作")
            return self._to_detail(s)

    def activate(self, strategy_id: str) -> ControlResponse:
        with self._lock:
            s = self._get(strategy_id)
            if s.status != "PENDING_ACTIVATION":
                raise HTTPException(status_code=409, detail="only PENDING_ACTIVATION can activate")
            if s.upstream_only_activation:
                raise HTTPException(status_code=409, detail="upstream_only_activation=true")
            if not s.conditions_json:
                raise HTTPException(status_code=409, detail="conditions not configured")
            if not s.trade_action_json and not s.next_strategy_id:
                raise HTTPException(status_code=409, detail="follow-up actions not configured")

            now = utcnow()
            s.activated_at = now
            s.logical_activated_at = now
            if s.expire_mode == "relative" and s.expire_in_seconds:
                s.expire_at = now + timedelta(seconds=s.expire_in_seconds)
            s.status = "ACTIVE"
            s.updated_at = now
            self._append_event(strategy_id, "ACTIVATED", "策略已手动激活")
            return ControlResponse(
                strategy_id=strategy_id,
                status=s.status,
                message="activated",
                updated_at=s.updated_at,
            )

    def pause(self, strategy_id: str) -> ControlResponse:
        with self._lock:
            s = self._get(strategy_id)
            if s.status != "ACTIVE":
                raise HTTPException(status_code=409, detail="only ACTIVE can pause")
            s.status = "PAUSED"
            s.updated_at = utcnow()
            self._append_event(strategy_id, "PAUSED", "策略已暂停")
            return ControlResponse(
                strategy_id=strategy_id,
                status=s.status,
                message="paused",
                updated_at=s.updated_at,
            )

    def resume(self, strategy_id: str) -> ControlResponse:
        with self._lock:
            s = self._get(strategy_id)
            if s.status != "PAUSED":
                raise HTTPException(status_code=409, detail="only PAUSED can resume")
            s.status = "ACTIVE"
            s.updated_at = utcnow()
            self._append_event(strategy_id, "RESUMED", "策略已恢复")
            return ControlResponse(
                strategy_id=strategy_id,
                status=s.status,
                message="resumed",
                updated_at=s.updated_at,
            )

    def cancel(self, strategy_id: str) -> ControlResponse:
        with self._lock:
            s = self._get(strategy_id)
            if s.status in TERMINAL_STATUSES:
                raise HTTPException(status_code=409, detail="terminal status cannot cancel")
            s.status = "CANCELLED"
            s.updated_at = utcnow()
            self._append_event(strategy_id, "CANCELLED", "策略已取消")
            return ControlResponse(
                strategy_id=strategy_id,
                status=s.status,
                message="cancelled",
                updated_at=s.updated_at,
            )

    def strategy_events(self, strategy_id: str) -> list[EventLogItem]:
        with self._lock:
            self._get(strategy_id)
            return list(self._events_by_strategy.get(strategy_id, []))

    def global_events(self) -> list[EventLogItem]:
        with self._lock:
            return list(self._global_events)

    def active_trade_instructions(self) -> list[ActiveTradeInstructionOut]:
        with self._lock:
            return list(self._active_trade_instructions)

    def trade_logs(self) -> list[TradeLogOut]:
        with self._lock:
            return list(self._trade_logs)

    def portfolio_summary(self) -> PortfolioSummaryOut:
        with self._lock:
            if not self._portfolio_summary:
                raise HTTPException(status_code=404, detail="portfolio summary not found")
            return self._portfolio_summary

    def positions(self, sec_type: str | None = None, symbol: str | None = None) -> list[PositionItemOut]:
        with self._lock:
            items = self._positions
            if sec_type:
                items = [p for p in items if p.sec_type == sec_type]
            if symbol:
                items = [p for p in items if p.symbol == symbol.upper()]
            return list(items)


store = InMemoryStore()
