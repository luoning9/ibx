from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import resolve_metric_allowed_rules, resolve_metric_allowed_windows
from .market_config import resolve_market_profile

StrategyStatus = Literal[
    "PENDING_ACTIVATION",
    "VERIFYING",
    "VERIFY_FAILED",
    "ACTIVE",
    "PAUSED",
    "TRIGGERED",
    "ORDER_SUBMITTED",
    "FILLED",
    "EXPIRED",
    "CANCELLED",
    "FAILED",
]

ConditionState = Literal["TRUE", "FALSE", "WAITING", "NOT_EVALUATED"]
TriggerGroupStatus = Literal["NOT_CONFIGURED", "MONITORING", "TRIGGERED", "EXPIRED"]
StrategyTradeType = Literal["buy", "sell", "switch", "open", "close", "spread"]
SymbolTradeType = Literal["buy", "sell", "open", "close", "ref"]
ConditionMetric = Literal[
    "PRICE",
    "DRAWDOWN_PCT",
    "RALLY_PCT",
    "VOLUME_RATIO",
    "AMOUNT_RATIO",
    "SPREAD",
]
ConditionTriggerMode = Literal[
    "LEVEL_INSTANT",
    "LEVEL_CONFIRM",
    "CROSS_UP_INSTANT",
    "CROSS_UP_CONFIRM",
    "CROSS_DOWN_INSTANT",
    "CROSS_DOWN_CONFIRM",
]
ConditionOperator = Literal[">=", "<="]
ConditionEvaluationWindow = Literal["1m", "5m", "30m", "1h", "2h", "4h", "1d", "2d"]

LEGACY_METRIC_ALIASES: dict[str, ConditionMetric] = {
    "LIQUIDITY_RATIO": "VOLUME_RATIO",
}

CONDITION_METRICS_BY_TYPE: dict[str, set[ConditionMetric]] = {
    "SINGLE_PRODUCT": {"PRICE", "DRAWDOWN_PCT", "RALLY_PCT"},
    "PAIR_PRODUCTS": {"VOLUME_RATIO", "AMOUNT_RATIO", "SPREAD"},
}

class Capabilities(BaseModel):
    can_activate: bool = False
    can_pause: bool = False
    can_resume: bool = False
    can_cancel: bool = False
    can_delete: bool = False


class CapabilityReasons(BaseModel):
    can_activate: str | None = None
    can_pause: str | None = None
    can_resume: str | None = None
    can_cancel: str | None = None
    can_delete: str | None = None


class EventLogItem(BaseModel):
    timestamp: datetime
    event_type: str
    detail: str
    strategy_id: str | None = None


class StrategySymbolItem(BaseModel):
    code: str
    trade_type: SymbolTradeType
    contract_id: int | None = None

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        v = value.strip().upper()
        if not v:
            raise ValueError("symbol code cannot be empty")
        return v

    @field_validator("contract_id")
    @classmethod
    def validate_contract_id(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("contract_id must be a positive integer")
        return value


def _validate_trade_symbol_combo(
    strategy_trade_type: StrategyTradeType, symbols: list[StrategySymbolItem]
) -> None:
    if not symbols:
        raise ValueError("symbols cannot be empty")

    buy_count = sum(1 for s in symbols if s.trade_type == "buy")
    sell_count = sum(1 for s in symbols if s.trade_type == "sell")
    open_count = sum(1 for s in symbols if s.trade_type == "open")
    close_count = sum(1 for s in symbols if s.trade_type == "close")
    stock_leg_count = buy_count + sell_count
    futures_leg_count = open_count + close_count

    if strategy_trade_type in {"buy", "sell"}:
        if futures_leg_count > 0:
            raise ValueError("stock trade_type only allows symbol trade_type buy/sell/ref")
        if (buy_count if strategy_trade_type == "buy" else sell_count) < 1:
            raise ValueError(f"trade_type={strategy_trade_type} requires at least one same-type symbol")
    elif strategy_trade_type == "switch":
        if futures_leg_count > 0:
            raise ValueError("trade_type=switch only allows symbol trade_type buy/sell/ref")
        if buy_count < 1 or sell_count < 1:
            raise ValueError("trade_type=switch requires at least one buy and one sell symbol")
    elif strategy_trade_type == "open":
        if stock_leg_count > 0:
            raise ValueError("trade_type=open only allows symbol trade_type open/close/ref")
        if open_count < 1:
            raise ValueError("trade_type=open requires at least one open symbol")
    elif strategy_trade_type == "close":
        if stock_leg_count > 0:
            raise ValueError("trade_type=close only allows symbol trade_type open/close/ref")
        if close_count < 1:
            raise ValueError("trade_type=close requires at least one close symbol")
    elif strategy_trade_type == "spread":
        if stock_leg_count > 0:
            raise ValueError("trade_type=spread only allows symbol trade_type open/close/ref")
        if open_count < 1 or close_count < 1:
            raise ValueError("trade_type=spread requires at least one open and one close symbol")


class ConditionItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    condition_id: str | None = None
    condition_nl: str | None = None
    condition_type: Literal["SINGLE_PRODUCT", "PAIR_PRODUCTS"]
    metric: ConditionMetric
    trigger_mode: ConditionTriggerMode
    evaluation_window: ConditionEvaluationWindow
    window_price_basis: Literal["CLOSE", "HIGH", "LOW", "AVG"] = "CLOSE"
    operator: ConditionOperator
    value: float
    product: str | None = None
    product_b: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        # Backward-compatibility: merge legacy product_a into product.
        if (not data.get("product")) and data.get("product_a"):
            data["product"] = data.get("product_a")
        data.pop("product_a", None)
        # price_reference is no longer part of condition schema.
        data.pop("price_reference", None)
        return data

    @field_validator("metric", mode="before")
    @classmethod
    def normalize_metric(cls, value: Any) -> str:
        metric = str(value).strip().upper()
        return LEGACY_METRIC_ALIASES.get(metric, metric)

    @field_validator("product", "product_b")
    @classmethod
    def normalize_products(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        return normalized or None

    @field_validator("evaluation_window", mode="before")
    @classmethod
    def normalize_evaluation_window(cls, value: Any) -> str:
        return str(value).strip().lower()

    @model_validator(mode="after")
    def validate_condition_shape(self) -> "ConditionItem":
        allowed_metrics = CONDITION_METRICS_BY_TYPE[self.condition_type]
        if self.metric not in allowed_metrics:
            raise ValueError(f"metric={self.metric} is not allowed for condition_type={self.condition_type}")

        allowed_rules = resolve_metric_allowed_rules(self.metric)
        if (self.trigger_mode, self.operator) not in allowed_rules:
            raise ValueError(
                f"metric={self.metric} does not allow trigger_mode={self.trigger_mode} with operator={self.operator}"
            )

        allowed_windows = resolve_metric_allowed_windows(self.metric)
        if self.evaluation_window not in allowed_windows:
            raise ValueError(
                f"metric={self.metric} does not allow evaluation_window={self.evaluation_window}"
            )

        if self.condition_type == "SINGLE_PRODUCT":
            if not self.product:
                raise ValueError("SINGLE_PRODUCT requires product")
            self.product_b = None
        else:
            if not self.product or not self.product_b:
                raise ValueError("PAIR_PRODUCTS requires product and product_b")
            if self.product == self.product_b:
                raise ValueError("PAIR_PRODUCTS requires different product and product_b")
        return self


class ConditionRuntimeItem(BaseModel):
    condition_id: str
    state: ConditionState
    last_value: float | None = None
    last_evaluated_at: datetime | None = None


class TradeActionRuntime(BaseModel):
    trade_status: str
    trade_id: str | None = None
    last_error: str | None = None


class NextStrategyProjection(BaseModel):
    id: str
    description: str | None = None
    status: StrategyStatus | Literal["NOT_SET", "UNKNOWN"] = "UNKNOWN"


class StrategySummaryOut(BaseModel):
    id: str
    status: StrategyStatus
    description: str
    updated_at: datetime
    expire_at: datetime | None = None
    upstream_strategy_id: str | None = None
    capabilities: Capabilities


class StrategyDetailOut(BaseModel):
    id: str
    description: str
    market: str
    sec_type: str
    exchange: str
    trade_type: StrategyTradeType
    symbols: list[StrategySymbolItem]
    upstream_only_activation: bool = False
    activated_at: datetime | None = None
    logical_activated_at: datetime | None = None
    expire_in_seconds: int | None = None
    expire_at: datetime | None = None
    status: StrategyStatus

    editable: bool
    editable_reason: str | None = None
    capabilities: Capabilities
    capability_reasons: CapabilityReasons

    condition_logic: Literal["AND", "OR"] = "AND"
    conditions_json: list[ConditionItem] = Field(default_factory=list)
    trigger_group_status: TriggerGroupStatus = "NOT_CONFIGURED"
    conditions_runtime: list[ConditionRuntimeItem] = Field(default_factory=list)

    trade_action_json: dict[str, Any] | None = None
    trade_action_runtime: TradeActionRuntime
    next_strategy: NextStrategyProjection | None = None
    upstream_strategy: NextStrategyProjection | None = None

    anchor_price: float | None = None
    events: list[EventLogItem] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class StrategyCreateIn(BaseModel):
    id: str | None = None
    idempotency_key: str | None = None
    description: str
    market: str | None = None
    trade_type: StrategyTradeType
    symbols: list[StrategySymbolItem]
    upstream_only_activation: bool = False
    expire_mode: Literal["relative", "absolute"] = "relative"
    expire_in_seconds: int | None = 172800
    expire_at: datetime | None = None
    condition_logic: Literal["AND", "OR"] = "AND"
    conditions: list[ConditionItem] = Field(default_factory=list)
    trade_action_json: dict[str, Any] | None = None
    next_strategy_id: str | None = None
    next_strategy_note: str | None = None

    @model_validator(mode="after")
    def validate_trade_type_and_symbols(self) -> "StrategyCreateIn":
        _validate_trade_symbol_combo(self.trade_type, self.symbols)
        self.market = resolve_market_profile(self.market, self.trade_type).market
        return self


class StrategyBasicPatchIn(BaseModel):
    description: str | None = None
    market: str | None = None
    trade_type: StrategyTradeType | None = None
    symbols: list[StrategySymbolItem] | None = None
    upstream_only_activation: bool | None = None
    expire_mode: Literal["relative", "absolute"] | None = None
    expire_in_seconds: int | None = None
    expire_at: datetime | None = None


class StrategyConditionsPutIn(BaseModel):
    condition_logic: Literal["AND", "OR"] = "AND"
    conditions: list[ConditionItem] = Field(default_factory=list)


class StrategyActionsPutIn(BaseModel):
    trade_action_json: dict[str, Any] | None = None
    next_strategy_id: str | None = None
    next_strategy_note: str | None = None


class ControlResponse(BaseModel):
    strategy_id: str
    status: StrategyStatus
    message: str
    updated_at: datetime


class PortfolioSummaryOut(BaseModel):
    net_liquidation: float
    available_funds: float
    unrealized_pnl: float
    realized_pnl: float
    daily_pnl: float
    updated_at: datetime


class PositionItemOut(BaseModel):
    sec_type: Literal["STK", "FUT"]
    symbol: str
    position_qty: float
    position_unit: Literal["股", "手"]
    avg_price: float | None = None
    last_price: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    updated_at: datetime


class ActiveTradeInstructionOut(BaseModel):
    updated_at: datetime
    strategy_id: str
    trade_id: str
    instruction_summary: str
    status: str
    expire_at: datetime | None = None


class TradeLogOut(BaseModel):
    timestamp: datetime
    strategy_id: str
    trade_id: str
    stage: str
    result: str
    detail: str
