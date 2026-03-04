from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .broker_provider_registry import get_broker_data_provider
from .config import load_app_config
from .ib_data_service import BrokerDataProvider
from .ib_trade_service import IBTradeService
from .market_config import resolve_market_profile


@dataclass(frozen=True)
class ActivationVerificationResult:
    passed: bool
    reason: str
    resolved_symbol_contracts: int = 0
    updated_condition_contracts: int = 0
    trade_validation_context: dict[str, Any] | None = None


@dataclass(frozen=True)
class _StrategySymbolRow:
    row_id: int
    code: str
    contract_id: int | None


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_strategy_id(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    return normalized or None


def _has_executable_trade_action(raw_trade_action_json: Any) -> bool:
    if raw_trade_action_json is None:
        return False
    if isinstance(raw_trade_action_json, dict):
        return True
    if not isinstance(raw_trade_action_json, str):
        return False
    text = raw_trade_action_json.strip()
    if not text:
        return False
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict)


def _has_follow_up_actions(strategy_row: sqlite3.Row) -> bool:
    has_trade_action = _has_executable_trade_action(strategy_row["trade_action_json"])
    has_next_strategy = _normalize_strategy_id(strategy_row["next_strategy_id"]) is not None
    return has_trade_action or has_next_strategy


def _decode_trade_action(raw_trade_action_json: Any) -> tuple[dict[str, Any] | None, str | None]:
    if raw_trade_action_json is None:
        return None, None
    if isinstance(raw_trade_action_json, dict):
        return raw_trade_action_json, None
    if not isinstance(raw_trade_action_json, str):
        return None, "trade_action_json must be a JSON object"
    text = raw_trade_action_json.strip()
    if not text:
        return None, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"trade_action_json invalid JSON: {exc}"
    if parsed is None:
        return None, None
    if not isinstance(parsed, dict):
        return None, "trade_action_json must be a JSON object"
    return parsed, None


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _build_trade_validation_context(validation_result: Any) -> dict[str, Any]:
    validated = getattr(validation_result, "validated", None)
    context: dict[str, Any] = {
        "market": str(getattr(validation_result, "market", "") or "").strip().upper(),
        "account_code": getattr(validation_result, "account_code", None),
        "con_id": _to_int_or_none(getattr(validation_result, "con_id", None)),
        "symbol": str(getattr(validation_result, "symbol", "") or "").strip().upper(),
        "side": str(getattr(validation_result, "side", "") or "").strip().upper(),
        "order_type": str(getattr(validation_result, "order_type", "") or "").strip().upper(),
        "quantity": float(getattr(validation_result, "quantity", 0.0) or 0.0),
        "validated_at": str(getattr(validation_result, "validated_at", "") or ""),
    }
    if validated is None:
        return context
    context["action_type"] = str(getattr(validated, "action_type", "") or "").strip().upper()
    context["contract_month"] = str(getattr(validated, "contract_month", "") or "").strip() or None
    context["limit_price"] = getattr(validated, "limit_price", None)
    context["tif"] = str(getattr(validated, "tif", "") or "").strip().upper()
    context["outside_rth"] = bool(getattr(validated, "outside_rth", False))
    return context


def _load_strategy_symbols(conn: sqlite3.Connection, strategy_id: str) -> list[_StrategySymbolRow]:
    rows = conn.execute(
        """
        SELECT id, code, contract_id
        FROM strategy_symbols
        WHERE strategy_id = ?
        ORDER BY position ASC, id ASC
        """,
        (strategy_id,),
    ).fetchall()
    symbols: list[_StrategySymbolRow] = []
    for row in rows:
        raw_contract_id = row["contract_id"]
        contract_id: int | None
        if raw_contract_id is None:
            contract_id = None
        else:
            try:
                contract_id = int(raw_contract_id)
            except (TypeError, ValueError):
                contract_id = 0
        symbols.append(
            _StrategySymbolRow(
                row_id=int(row["id"]),
                code=_normalize_symbol(row["code"]),
                contract_id=contract_id,
            )
        )
    return symbols


def _validate_and_resolve_contract_ids(
    *,
    provider: BrokerDataProvider,
    market: str,
    symbols: list[_StrategySymbolRow],
) -> tuple[dict[str, int], str | None]:
    if not symbols:
        return {}, "symbols not configured"

    symbol_contract_ids: dict[str, int | None] = {}
    for symbol in symbols:
        if not symbol.code:
            return {}, "symbols contains empty code"
        if symbol.contract_id is not None and symbol.contract_id <= 0:
            return {}, f"symbols contains invalid contract_id for code={symbol.code}: {symbol.contract_id}"
        if symbol.code not in symbol_contract_ids:
            symbol_contract_ids[symbol.code] = symbol.contract_id
            continue
        previous = symbol_contract_ids[symbol.code]
        if symbol.contract_id is None:
            continue
        if previous is None:
            symbol_contract_ids[symbol.code] = symbol.contract_id
            continue
        if previous != symbol.contract_id:
            return {}, f"symbols contains conflicting contract_id for code={symbol.code}"

    resolved: dict[str, int] = {}
    for code, contract_id in symbol_contract_ids.items():
        if contract_id is not None:
            resolved[code] = contract_id
            continue
        try:
            resolved_id = provider.resolve_contract_id(code=code, market=market)
        except Exception as exc:  # noqa: BLE001
            return {}, f"resolve_contract_id failed for {code}: {exc}"
        if resolved_id <= 0:
            return {}, f"resolve_contract_id returned invalid contract_id for {code}: {resolved_id}"
        resolved[code] = resolved_id
    return resolved, None


def _enrich_conditions_with_contract_ids(
    *,
    conditions_json: str | None,
    symbol_contract_ids: dict[str, int],
) -> tuple[list[dict[str, Any]], int, str | None]:
    try:
        conditions_raw = json.loads(conditions_json or "[]")
    except json.JSONDecodeError as exc:
        return [], 0, f"conditions_json invalid: {exc}"
    if not isinstance(conditions_raw, list):
        return [], 0, "conditions_json must be a JSON array"

    updated_conditions: list[dict[str, Any]] = []
    updated_fields = 0
    for idx, item in enumerate(conditions_raw, start=1):
        if not isinstance(item, dict):
            return [], 0, f"condition #{idx} must be an object"
        condition = dict(item)
        condition_id = str(condition.get("condition_id") or f"c{idx}").strip() or f"c{idx}"
        condition_type = str(condition.get("condition_type", "")).strip().upper()
        product = _normalize_symbol(condition.get("product"))
        product_b = _normalize_symbol(condition.get("product_b"))

        if condition_type == "SINGLE_PRODUCT":
            if not product:
                return [], 0, f"condition {condition_id}: SINGLE_PRODUCT requires product"
            contract_id = symbol_contract_ids.get(product)
            if contract_id is None:
                return [], 0, f"condition {condition_id}: product={product} not found in symbols"
            current_contract_id = _to_int_or_none(condition.get("contract_id"))
            if current_contract_id != contract_id:
                condition["contract_id"] = contract_id
                updated_fields += 1
            condition.pop("contract_id_b", None)
        elif condition_type == "PAIR_PRODUCTS":
            if not product or not product_b:
                return [], 0, f"condition {condition_id}: PAIR_PRODUCTS requires product and product_b"
            if product == product_b:
                return [], 0, f"condition {condition_id}: product and product_b must be different"
            contract_id = symbol_contract_ids.get(product)
            contract_id_b = symbol_contract_ids.get(product_b)
            if contract_id is None:
                return [], 0, f"condition {condition_id}: product={product} not found in symbols"
            if contract_id_b is None:
                return [], 0, f"condition {condition_id}: product_b={product_b} not found in symbols"
            current_contract_id = _to_int_or_none(condition.get("contract_id"))
            current_contract_id_b = _to_int_or_none(condition.get("contract_id_b"))
            if current_contract_id != contract_id:
                condition["contract_id"] = contract_id
                updated_fields += 1
            if current_contract_id_b != contract_id_b:
                condition["contract_id_b"] = contract_id_b
                updated_fields += 1
        else:
            return [], 0, f"condition {condition_id}: unsupported condition_type={condition_type or '<empty>'}"

        updated_conditions.append(condition)
    return updated_conditions, updated_fields, None


def run_activation_verification(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    strategy_row: sqlite3.Row,
    broker_data_provider: BrokerDataProvider | None = None,
    trade_service: IBTradeService | None = None,
) -> ActivationVerificationResult:
    market = str(strategy_row["market"] or "").strip().upper()
    trade_type = str(strategy_row["trade_type"] or "").strip().lower()
    app_cfg = load_app_config()
    trade_validation_context: dict[str, Any] | None = None
    try:
        resolve_market_profile(market, trade_type)
    except ValueError as exc:
        return ActivationVerificationResult(passed=False, reason=str(exc))
    if not _has_follow_up_actions(strategy_row):
        return ActivationVerificationResult(passed=False, reason="follow-up actions not configured")
    trade_action, trade_action_error = _decode_trade_action(strategy_row["trade_action_json"])
    if trade_action_error is not None:
        return ActivationVerificationResult(passed=False, reason=trade_action_error)
    if trade_action is not None:
        trade_action_payload = dict(trade_action)
        trade_action_payload["market"] = market
        account_code = str(app_cfg.ib_gateway.account_code or "").strip() or None
        if not str(trade_action_payload.get("account_code", "")).strip():
            trade_action_payload["account_code"] = account_code
        validator = trade_service or IBTradeService()
        try:
            validation_result = validator.validate_trade_action(trade_action=trade_action_payload)
            trade_validation_context = _build_trade_validation_context(validation_result)
        except Exception as exc:  # noqa: BLE001
            return ActivationVerificationResult(
                passed=False,
                reason=f"trade_action validation failed: {exc}",
            )

    symbols = _load_strategy_symbols(conn, strategy_id)

    provider = broker_data_provider
    if provider is None:
        provider = get_broker_data_provider()

    account_code = str(app_cfg.ib_gateway.account_code or "").strip() or None
    try:
        provider.get_account_snapshot(account_code=account_code)
    except Exception as exc:  # noqa: BLE001
        return ActivationVerificationResult(
            passed=False,
            reason=f"get_account_snapshot failed: {exc}",
            trade_validation_context=trade_validation_context,
        )

    resolved_contract_ids, resolve_error = _validate_and_resolve_contract_ids(
        provider=provider,
        market=market,
        symbols=symbols,
    )
    if resolve_error is not None:
        return ActivationVerificationResult(
            passed=False,
            reason=resolve_error,
            trade_validation_context=trade_validation_context,
        )

    resolved_symbol_rows = 0
    for symbol in symbols:
        target_contract_id = resolved_contract_ids[symbol.code]
        if symbol.contract_id == target_contract_id:
            continue
        conn.execute(
            """
            UPDATE strategy_symbols
            SET contract_id = ?
            WHERE id = ? AND strategy_id = ?
            """,
            (target_contract_id, symbol.row_id, strategy_id),
        )
        resolved_symbol_rows += 1

    enriched_conditions, updated_condition_fields, conditions_error = _enrich_conditions_with_contract_ids(
        conditions_json=strategy_row["conditions_json"],
        symbol_contract_ids=resolved_contract_ids,
    )
    if conditions_error is not None:
        return ActivationVerificationResult(
            passed=False,
            reason=conditions_error,
            trade_validation_context=trade_validation_context,
        )
    if updated_condition_fields > 0:
        conn.execute(
            """
            UPDATE strategies
            SET conditions_json = ?
            WHERE id = ? AND is_deleted = 0
            """,
            (_json_dumps(enriched_conditions), strategy_id),
        )

    return ActivationVerificationResult(
        passed=True,
        reason="verification_passed",
        resolved_symbol_contracts=resolved_symbol_rows,
        updated_condition_contracts=updated_condition_fields,
        trade_validation_context=trade_validation_context,
    )
