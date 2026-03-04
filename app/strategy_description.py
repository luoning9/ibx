from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import ConditionItem, StrategySymbolItem

_MARKET_LABELS: dict[str, str] = {
    "US_STOCK": "美股",
    "COMEX_FUTURES": "COMEX期货",
}

_TRADE_TYPE_LABELS: dict[str, str] = {
    "buy": "买入",
    "sell": "卖出",
    "switch": "换仓",
    "open": "开仓",
    "close": "平仓",
    "spread": "价差/展期",
}


def _compact_codes(codes: list[str]) -> str:
    normalized = [str(item or "").strip().upper() for item in codes]
    hit = [item for item in normalized if item]
    return "/".join(hit) if hit else "-"


def _codes_by_trade_type(symbols: list[StrategySymbolItem], trade_type: str) -> list[str]:
    return [item.code for item in symbols if item.trade_type == trade_type]


def _format_relative_expire(expire_in_seconds: int | None) -> str:
    if expire_in_seconds is None or expire_in_seconds <= 0:
        return "有效期：激活后（未设置）"

    seconds = int(expire_in_seconds)
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"有效期：激活后 {days} 天"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"有效期：激活后 {hours} 小时"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"有效期：激活后 {minutes} 分钟"
    return f"有效期：激活后 {seconds} 秒"


def _format_absolute_expire(expire_at: datetime | None) -> str:
    if expire_at is None:
        return "有效期：absolute（未设置）"
    utc_text = expire_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"有效期：至 {utc_text}"


def _describe_action(trade_type: str, symbols: list[StrategySymbolItem]) -> str:
    buy_codes = _compact_codes(_codes_by_trade_type(symbols, "buy"))
    sell_codes = _compact_codes(_codes_by_trade_type(symbols, "sell"))
    open_codes = _compact_codes(_codes_by_trade_type(symbols, "open"))
    close_codes = _compact_codes(_codes_by_trade_type(symbols, "close"))

    if trade_type == "buy":
        return f"买入 {buy_codes}"
    if trade_type == "sell":
        return f"卖出 {sell_codes}"
    if trade_type == "switch":
        return f"卖出 {sell_codes}，买入 {buy_codes}"
    if trade_type == "open":
        return f"开仓 {open_codes}"
    if trade_type == "close":
        return f"平仓 {close_codes}"
    return f"开仓 {open_codes}，平仓 {close_codes}"


def _describe_condition_item(condition: ConditionItem) -> str:
    subject = (
        condition.product or "标的"
        if condition.condition_type == "SINGLE_PRODUCT"
        else f"{condition.product or 'A'}/{condition.product_b or 'B'}"
    )
    return (
        f"{subject} {condition.metric} {condition.trigger_mode} "
        f"{condition.operator} {condition.value:g} ({condition.evaluation_window})"
    )


def _describe_conditions(conditions: list[ConditionItem]) -> str:
    if len(conditions) == 0:
        return "触发条件：未设置"
    preview = [_describe_condition_item(item) for item in conditions[:2]]
    if len(conditions) > 2:
        return f"触发条件：{'；'.join(preview)}（共{len(conditions)}条）"
    return f"触发条件：{'；'.join(preview)}"


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_upper(value: Any) -> str:
    return _to_text(value).upper()


def _describe_order(trade_action_json: dict[str, Any] | None) -> str:
    if not trade_action_json:
        return "下单：未设置"

    action_type = _to_upper(trade_action_json.get("action_type"))
    quantity = _to_text(trade_action_json.get("quantity")) or "-"
    if action_type == "STOCK_TRADE":
        side = _to_upper(trade_action_json.get("side")) or "-"
        symbol = _to_upper(trade_action_json.get("symbol")) or "-"
        order_type = _to_upper(trade_action_json.get("order_type")) or "-"
        limit_part = ""
        if order_type == "LMT":
            limit_price = _to_text(trade_action_json.get("limit_price")) or "-"
            limit_part = f" @ {limit_price}"
        return f"下单：{side} {quantity} 股 {symbol} {order_type}{limit_part}"

    if action_type == "FUT_POSITION":
        effect = _to_upper(trade_action_json.get("position_effect")) or "-"
        side = _to_upper(trade_action_json.get("side")) or "-"
        contract = _to_upper(trade_action_json.get("contract")) or _to_upper(trade_action_json.get("symbol")) or "-"
        order_type = _to_upper(trade_action_json.get("order_type")) or "-"
        limit_part = ""
        if order_type == "LMT":
            limit_price = _to_text(trade_action_json.get("limit_price")) or "-"
            limit_part = f" @ {limit_price}"
        return f"下单：{effect}/{side} {quantity} 手 {contract} {order_type}{limit_part}"

    if action_type == "FUT_ROLL":
        close_contract = _to_upper(trade_action_json.get("close_contract")) or "-"
        open_contract = _to_upper(trade_action_json.get("open_contract")) or "-"
        close_order_type = _to_upper(trade_action_json.get("close_order_type")) or "-"
        open_order_type = _to_upper(trade_action_json.get("open_order_type")) or "-"
        return (
            f"下单：展期 {close_contract}->{open_contract} 数量 {quantity} 手 "
            f"(close={close_order_type}, open={open_order_type})"
        )

    return f"下单：{action_type or 'UNKNOWN'}"


def generate_strategy_description(
    *,
    market: str,
    trade_type: str,
    symbols: list[StrategySymbolItem],
    conditions: list[ConditionItem],
    trade_action_json: dict[str, Any] | None,
    upstream_only_activation: bool,
    expire_mode: str,
    expire_in_seconds: int | None,
    expire_at: datetime | None,
) -> str:
    market_key = str(market or "").strip().upper()
    market_text = _MARKET_LABELS.get(market_key, market_key or "UNKNOWN_MARKET")
    trade_type_text = _TRADE_TYPE_LABELS.get(trade_type, str(trade_type))
    action_text = _describe_action(trade_type, symbols)

    refs = _codes_by_trade_type(symbols, "ref")
    ref_text = f"，参考 {_compact_codes(refs)}" if refs else ""
    condition_text = _describe_conditions(conditions)
    order_text = _describe_order(trade_action_json)

    return (
        f"{market_text}{trade_type_text}策略：{action_text}{ref_text}；"
        f"{condition_text}；{order_text}"
    )
