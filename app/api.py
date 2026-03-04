from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from .config import export_condition_rules, load_app_config
from .market_data import HistoricalBarsRequest, MarketDataProvider, build_market_data_provider_from_config
from .ib_market_data import IBSessionHistoricalFetcher
from .market_config import load_market_profiles
from .models import (
    ActiveTradeInstructionOut,
    ControlResponse,
    EventLogItem,
    MarketDataBarOut,
    MarketProfileOut,
    MarketDataProbeIn,
    MarketDataProbeOut,
    PortfolioSummaryOut,
    PositionItemOut,
    OpenOrderCancelOut,
    StrategyActionsPutIn,
    StrategyBasicPatchIn,
    StrategyConditionsPutIn,
    StrategyCreateIn,
    StrategyDescriptionOut,
    StrategyDetailOut,
    OtherOpenOrderOut,
    TradeRecoveryIn,
    TradeRecoveryOut,
    SystemStatusOut,
    StrategySummaryOut,
    TradeLogOut,
    TradeOrderOut,
)
from .system_status import get_system_status
from .store import store
from .worker import worker_engine

router = APIRouter(prefix="/v1", tags=["ibx"])
_LOGGER = logging.getLogger("ibx.api")


def _resolve_market_data_provider_for_probe() -> MarketDataProvider:
    provider = getattr(worker_engine, "_market_data_provider", None)
    if provider is not None:
        return provider
    cfg = load_app_config()
    if cfg.providers.market_data == "fixture":
        return build_market_data_provider_from_config()
    fetcher = IBSessionHistoricalFetcher()
    return build_market_data_provider_from_config(fetcher=fetcher)


def _enqueue_strategy_after_control(result: ControlResponse, *, reason: str) -> None:
    try:
        accepted = worker_engine.enqueue_strategy(result.strategy_id, reason=reason)
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "immediate enqueue failed strategy_id=%s reason=%s status=%s",
            result.strategy_id,
            reason,
            result.status,
        )
        return
    if not accepted:
        _LOGGER.debug(
            "immediate enqueue skipped strategy_id=%s reason=%s status=%s",
            result.strategy_id,
            reason,
            result.status,
        )


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/condition-rules")
def condition_rules() -> dict[str, object]:
    return export_condition_rules()


@router.get("/markets", response_model=list[MarketProfileOut])
def list_markets() -> list[MarketProfileOut]:
    profiles = load_market_profiles()
    return [
        MarketProfileOut(
            market=profile.market,
            sec_type=profile.sec_type,
            exchange=profile.exchange,
            currency=profile.currency,
            allowed_trade_types=sorted(profile.allowed_trade_types),
        )
        for _, profile in sorted(profiles.items(), key=lambda item: item[0])
    ]


@router.get("/system-status", response_model=SystemStatusOut)
def system_status() -> SystemStatusOut:
    return get_system_status()


@router.post("/market-data/probe", response_model=MarketDataProbeOut)
def market_data_probe(payload: MarketDataProbeIn) -> MarketDataProbeOut:
    provider = _resolve_market_data_provider_for_probe()
    contract: dict[str, str] = {
        "market": payload.market.strip().upper(),
        "code": payload.code.strip().upper(),
    }
    if payload.contract_month:
        contract["contract_month"] = payload.contract_month.strip()

    result = provider.get_historical_bars(
        HistoricalBarsRequest(
            contract=contract,
            start_time=payload.start_time,
            end_time=payload.end_time,
            bar_size=payload.bar_size,
            what_to_show=payload.what_to_show,
            use_rth=payload.use_rth,
            include_partial_bar=payload.include_partial_bar,
            max_bars=payload.max_bars,
            page_size=payload.page_size,
        )
    )
    bars = [
        MarketDataBarOut(
            ts=bar.ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            wap=bar.wap,
            count=bar.count,
        )
        for bar in result.bars
    ]
    return MarketDataProbeOut(
        provider_class=provider.__class__.__name__,
        request={
            "contract": contract,
            "start_time": payload.start_time,
            "end_time": payload.end_time,
            "bar_size": payload.bar_size,
            "what_to_show": payload.what_to_show,
            "use_rth": payload.use_rth,
            "include_partial_bar": payload.include_partial_bar,
            "max_bars": payload.max_bars,
            "page_size": payload.page_size,
        },
        bars=bars,
        meta=result.meta,
    )


@router.post("/strategies", response_model=StrategyDetailOut)
def create_strategy(payload: StrategyCreateIn) -> StrategyDetailOut:
    return store.create_strategy(payload)


@router.get("/strategies/{strategy_id}/description/generate", response_model=StrategyDescriptionOut)
def generate_strategy_description_by_id(strategy_id: str) -> StrategyDescriptionOut:
    return store.generate_strategy_description_by_id(strategy_id)


@router.get("/strategies", response_model=list[StrategySummaryOut])
def list_strategies() -> list[StrategySummaryOut]:
    return store.list_strategies()


@router.get("/strategies/{strategy_id}", response_model=StrategyDetailOut)
def get_strategy(strategy_id: str) -> StrategyDetailOut:
    return store.get_strategy(strategy_id)


@router.post("/strategies/{strategy_id}/copy", response_model=StrategyDetailOut)
def copy_strategy(strategy_id: str) -> StrategyDetailOut:
    return store.copy_strategy(strategy_id)


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
    result = store.activate(strategy_id)
    _enqueue_strategy_after_control(result, reason="api_activate")
    return result


@router.post("/strategies/{strategy_id}/pause", response_model=ControlResponse)
def pause_strategy(strategy_id: str) -> ControlResponse:
    return store.pause(strategy_id)


@router.post("/strategies/{strategy_id}/stop", response_model=ControlResponse)
def stop_strategy(strategy_id: str) -> ControlResponse:
    return store.stop(strategy_id)


@router.post("/strategies/{strategy_id}/resume", response_model=ControlResponse)
def resume_strategy(strategy_id: str) -> ControlResponse:
    result = store.resume(strategy_id)
    _enqueue_strategy_after_control(result, reason="api_resume")
    return result


@router.post("/strategies/{strategy_id}/cancel", response_model=ControlResponse)
def cancel_strategy(strategy_id: str) -> ControlResponse:
    return store.cancel(strategy_id)


@router.delete("/strategies/{strategy_id}", response_model=ControlResponse)
def delete_strategy(strategy_id: str) -> ControlResponse:
    return store.delete_strategy(strategy_id)


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


@router.get(
    "/trade-instructions/completed-recent",
    response_model=list[ActiveTradeInstructionOut],
)
def completed_trade_instructions_recent() -> list[ActiveTradeInstructionOut]:
    return store.completed_trade_instructions_recent(days=7)


@router.get(
    "/trade-instructions/open-orders/others",
    response_model=list[OtherOpenOrderOut],
)
def other_open_orders() -> list[OtherOpenOrderOut]:
    return store.other_open_orders()


@router.post(
    "/trade-instructions/open-orders/{perm_id}/cancel",
    response_model=OpenOrderCancelOut,
)
def cancel_other_open_order(perm_id: int) -> OpenOrderCancelOut:
    return store.cancel_other_open_order(perm_id)


@router.get("/trade-logs", response_model=list[TradeLogOut])
def trade_logs(trade_id: str | None = Query(default=None)) -> list[TradeLogOut]:
    return store.trade_logs(trade_id=trade_id)


@router.get("/trade-instructions/{trade_id}/orders", response_model=list[TradeOrderOut])
def trade_instruction_orders(trade_id: str) -> list[TradeOrderOut]:
    return store.trade_instruction_orders(trade_id)


@router.post("/trade-instructions/{trade_id}/recover", response_model=TradeRecoveryOut)
def recover_trade_instruction(trade_id: str, payload: TradeRecoveryIn) -> TradeRecoveryOut:
    return store.recover_trade_instruction(trade_id, payload)


@router.get("/portfolio-summary", response_model=PortfolioSummaryOut)
def portfolio_summary() -> PortfolioSummaryOut:
    return store.portfolio_summary()


@router.get("/positions", response_model=list[PositionItemOut])
def positions(
    sec_type: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
) -> list[PositionItemOut]:
    return store.positions(sec_type=sec_type, symbol=symbol)
