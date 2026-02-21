from __future__ import annotations

from fastapi import APIRouter, Query

from .models import (
    ActiveTradeInstructionOut,
    ControlResponse,
    EventLogItem,
    PortfolioSummaryOut,
    PositionItemOut,
    StrategyActionsPutIn,
    StrategyBasicPatchIn,
    StrategyConditionsPutIn,
    StrategyCreateIn,
    StrategyDetailOut,
    StrategySummaryOut,
    TradeLogOut,
)
from .store import store

router = APIRouter(prefix="/v1", tags=["ibx"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/strategies", response_model=StrategyDetailOut)
def create_strategy(payload: StrategyCreateIn) -> StrategyDetailOut:
    return store.create_strategy(payload)


@router.get("/strategies", response_model=list[StrategySummaryOut])
def list_strategies() -> list[StrategySummaryOut]:
    return store.list_strategies()


@router.get("/strategies/{strategy_id}", response_model=StrategyDetailOut)
def get_strategy(strategy_id: str) -> StrategyDetailOut:
    return store.get_strategy(strategy_id)


@router.patch("/strategies/{strategy_id}/basic", response_model=StrategyDetailOut)
def patch_strategy_basic(strategy_id: str, payload: StrategyBasicPatchIn) -> StrategyDetailOut:
    return store.patch_basic(strategy_id, payload)


@router.put("/strategies/{strategy_id}/conditions", response_model=StrategyDetailOut)
def put_strategy_conditions(
    strategy_id: str, payload: StrategyConditionsPutIn
) -> StrategyDetailOut:
    return store.put_conditions(strategy_id, payload)


@router.put("/strategies/{strategy_id}/actions", response_model=StrategyDetailOut)
def put_strategy_actions(strategy_id: str, payload: StrategyActionsPutIn) -> StrategyDetailOut:
    return store.put_actions(strategy_id, payload)


@router.post("/strategies/{strategy_id}/activate", response_model=ControlResponse)
def activate_strategy(strategy_id: str) -> ControlResponse:
    return store.activate(strategy_id)


@router.post("/strategies/{strategy_id}/pause", response_model=ControlResponse)
def pause_strategy(strategy_id: str) -> ControlResponse:
    return store.pause(strategy_id)


@router.post("/strategies/{strategy_id}/resume", response_model=ControlResponse)
def resume_strategy(strategy_id: str) -> ControlResponse:
    return store.resume(strategy_id)


@router.post("/strategies/{strategy_id}/cancel", response_model=ControlResponse)
def cancel_strategy(strategy_id: str) -> ControlResponse:
    return store.cancel(strategy_id)


@router.get("/strategies/{strategy_id}/events", response_model=list[EventLogItem])
def strategy_events(strategy_id: str) -> list[EventLogItem]:
    return store.strategy_events(strategy_id)


@router.get("/events", response_model=list[EventLogItem])
def global_events() -> list[EventLogItem]:
    return store.global_events()


@router.get(
    "/trade-instructions/active",
    response_model=list[ActiveTradeInstructionOut],
)
def active_trade_instructions() -> list[ActiveTradeInstructionOut]:
    return store.active_trade_instructions()


@router.get("/trade-logs", response_model=list[TradeLogOut])
def trade_logs() -> list[TradeLogOut]:
    return store.trade_logs()


@router.get("/portfolio-summary", response_model=PortfolioSummaryOut)
def portfolio_summary() -> PortfolioSummaryOut:
    return store.portfolio_summary()


@router.get("/positions", response_model=list[PositionItemOut])
def positions(
    sec_type: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
) -> list[PositionItemOut]:
    return store.positions(sec_type=sec_type, symbol=symbol)

