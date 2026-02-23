from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .broker_provider_registry import get_shared_broker_data_provider
from .config import load_app_config
from .ib_data_service import BrokerDataProvider
from .market_config import resolve_market_profile


@dataclass(frozen=True)
class ActivationVerificationResult:
    passed: bool
    reason: str
    resolved_symbol_contracts: int = 0
    updated_condition_contracts: int = 0


@dataclass(frozen=True)
class _StrategySymbolRow:
    row_id: int
    code: str
    contract_id: int | None


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


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


def _validate_and_collect_contract_ids(
    symbols: list[_StrategySymbolRow],
) -> tuple[dict[str, int | None], str | None]:
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
    return symbol_contract_ids, None


def _resolve_missing_contract_ids(
    *,
    provider: BrokerDataProvider,
    market: str,
    symbol_contract_ids: dict[str, int | None],
) -> tuple[dict[str, int], str | None]:
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
) -> ActivationVerificationResult:
    market = str(strategy_row["market"] or "").strip().upper()
    trade_type = str(strategy_row["trade_type"] or "").strip().lower()
    try:
        resolve_market_profile(market, trade_type)
    except ValueError as exc:
        return ActivationVerificationResult(passed=False, reason=str(exc))

    symbols = _load_strategy_symbols(conn, strategy_id)
    symbol_contract_ids, symbol_error = _validate_and_collect_contract_ids(symbols)
    if symbol_error is not None:
        return ActivationVerificationResult(passed=False, reason=symbol_error)

    provider = broker_data_provider
    if provider is None:
        provider = get_shared_broker_data_provider()

    account_code = str(load_app_config().ib_gateway.account_code or "").strip() or None
    try:
        provider.get_account_snapshot(account_code=account_code)
    except Exception as exc:  # noqa: BLE001
        return ActivationVerificationResult(
            passed=False,
            reason=f"get_account_snapshot failed: {exc}",
        )

    resolved_contract_ids, resolve_error = _resolve_missing_contract_ids(
        provider=provider,
        market=market,
        symbol_contract_ids=symbol_contract_ids,
    )
    if resolve_error is not None:
        return ActivationVerificationResult(passed=False, reason=resolve_error)

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
        return ActivationVerificationResult(passed=False, reason=conditions_error)
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
    )
