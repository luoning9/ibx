from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Any

from .config import (
    load_app_config,
    resolve_ib_client_id,
)
from .ib_compat import INSTALL_HINT, require_ib_attr
from .ib_session_manager import get_ib_session_manager
from .market_config import resolve_market_profile


UTC = timezone.utc
TERMINAL_ORDER_STATUSES: set[str] = {"FILLED", "CANCELLED", "FAILED"}
REFERENCE_PRICE_DAILY_LOOKBACK_DAYS = 10


class IBTradeServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SubmitOrderResult:
    con_id: int | None
    order_id: int | None
    perm_id: int | None
    status: str
    normalized_status: str
    terminal: bool
    filled_qty: float
    remaining_qty: float
    avg_fill_price: float | None
    symbol: str
    side: str
    order_type: str
    quantity: float
    account_code: str | None
    submitted_at: datetime


@dataclass(frozen=True)
class OrderStatusSnapshot:
    order_id: int | None
    perm_id: int | None
    status: str
    normalized_status: str
    terminal: bool
    filled_qty: float
    remaining_qty: float
    avg_fill_price: float | None
    error_message: str | None
    updated_at: datetime


@dataclass(frozen=True)
class ActiveOrderSnapshot:
    con_id: int | None
    client_id: int | None
    symbol: str
    sec_type: str
    side: str
    order_type: str
    quantity: float
    limit_price: float | None
    order_id: int | None
    perm_id: int | None
    status: str
    normalized_status: str
    terminal: bool
    filled_qty: float
    remaining_qty: float
    avg_fill_price: float | None
    account_code: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class ValidatedStaticOrderPolicy:
    market: str
    market_profile: Any
    action_type: str
    symbol: str
    contract_month: str | None
    side: str
    order_type: str
    quantity: float
    limit_price: float | None
    tif: str
    outside_rth: bool


@dataclass(frozen=True)
class TradeValidationResult:
    con_id: int | None
    market: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    account_code: str | None
    validated: ValidatedStaticOrderPolicy
    resolved_contract: Any
    validated_at: datetime


def validate_static_order_policy(
    *,
    market: str,
    trade_action: dict[str, Any],
    allowed_sides: set[str],
    allowed_order_types: set[str],
    allow_outside_rth: bool,
    buy_open_max_amount_usd: float,
) -> ValidatedStaticOrderPolicy:
    if not isinstance(trade_action, dict):
        raise ValueError("trade_action must be an object")
    normalized_market = str(market or "").strip().upper()
    profile = resolve_market_profile(normalized_market, None)
    action_type = str(trade_action.get("action_type", "")).strip().upper()
    if action_type == "STOCK_TRADE":
        if profile.sec_type != "STK":
            raise ValueError(f"market={profile.market} does not support STOCK_TRADE")
    elif action_type == "FUT_POSITION":
        if profile.sec_type != "FUT":
            raise ValueError(f"market={profile.market} does not support FUT_POSITION")
    elif action_type == "FUT_ROLL":
        raise ValueError("FUT_ROLL requires multi-leg execution and is not supported by submit_trade_action")
    else:
        raise ValueError(f"unsupported action_type={action_type or '<empty>'}")

    symbol = str(trade_action.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("trade_action.symbol is required")
    side = str(trade_action.get("side", "")).strip().upper()
    if side not in allowed_sides:
        raise ValueError(
            "trade_action.side is not allowed by trade_validation.allowed_sides"
            f" side={side or '<empty>'}"
        )
    quantity = _to_float(trade_action.get("quantity"), default=0.0)
    if quantity <= 0:
        raise ValueError("trade_action.quantity must be > 0")
    order_type = str(trade_action.get("order_type", "MKT")).strip().upper() or "MKT"
    if order_type not in allowed_order_types:
        raise ValueError(
            "trade_action.order_type is not allowed by trade_validation.allowed_order_types"
            f" order_type={order_type or '<empty>'}"
        )
    if order_type not in {"MKT", "LMT"}:
        raise ValueError(f"unsupported order_type={order_type}")
    tif = str(trade_action.get("tif", "DAY")).strip().upper() or "DAY"
    outside_rth = bool(trade_action.get("allow_overnight", False))
    if outside_rth and not allow_outside_rth:
        raise ValueError("outside RTH orders are disabled by trade_validation.allow_outside_rth=false")
    limit_price: float | None = None
    if order_type == "LMT":
        limit_value = _to_float(trade_action.get("limit_price"), default=0.0)
        if limit_value <= 0:
            raise ValueError("trade_action.limit_price must be > 0 when order_type=LMT")
        limit_price = limit_value
    if side == "BUY":
        max_amount = float(buy_open_max_amount_usd)
        if max_amount > 0 and order_type == "LMT" and limit_price is not None:
            amount = float(quantity) * float(limit_price)
            if amount - max_amount > 1e-9:
                raise ValueError(
                    f"buy/open amount {amount:.2f} exceeds configured max {max_amount:.2f} USD"
                )
    return ValidatedStaticOrderPolicy(
        market=profile.market,
        market_profile=profile,
        action_type=action_type,
        symbol=symbol,
        contract_month=str(trade_action.get("contract", "") or "").strip() or None,
        side=side,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price,
        tif=tif,
        outside_rth=outside_rth,
    )


def _normalize_account(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized if normalized else None


def _normalize_order_ref(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _to_int_including_zero(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_trade_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            return None
    return None


def _extract_trade_updated_at(trade: Any) -> datetime | None:
    raw_log = getattr(trade, "log", None)
    if isinstance(raw_log, list):
        for item in reversed(raw_log):
            for attr in ("time", "timestamp", "datetime", "date"):
                parsed = _parse_trade_timestamp(getattr(item, attr, None))
                if parsed is not None:
                    return parsed
    for obj in (trade, getattr(trade, "orderStatus", None), getattr(trade, "order", None)):
        if obj is None:
            continue
        for attr in ("time", "timestamp", "datetime", "date", "lastUpdate", "last_update"):
            parsed = _parse_trade_timestamp(getattr(obj, attr, None))
            if parsed is not None:
                return parsed
    return None


def _ensure_thread_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except Exception:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _normalize_order_status(
    *,
    raw_status: str,
    filled_qty: float,
    remaining_qty: float,
) -> tuple[str, bool]:
    status = str(raw_status or "").strip().upper()
    if status == "FILLED":
        return "FILLED", True
    if status in {"CANCELLED", "APICANCELLED"}:
        return "CANCELLED", True
    if status == "INACTIVE":
        return "FAILED", True
    if status in {"PENDINGSUBMIT", "PRESUBMITTED", "SUBMITTED", "PENDINGCANCEL"}:
        if filled_qty > 0 and remaining_qty > 0:
            return "PARTIAL_FILL", False
        return "ORDER_SUBMITTED", False
    if filled_qty > 0 and remaining_qty <= 0:
        return "FILLED", True
    if filled_qty > 0 and remaining_qty > 0:
        return "PARTIAL_FILL", False
    if status:
        return "ORDER_SUBMITTED", False
    return "UNKNOWN", False


def _normalize_trading_mode(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"paper", "live"}:
        return normalized
    return "paper"


def _extract_order_status_payload(trade: Any) -> dict[str, Any]:
    order = getattr(trade, "order", None)
    status_obj = getattr(trade, "orderStatus", None)
    order_id = _to_int_or_none(getattr(order, "orderId", None))
    perm_id = _to_int_or_none(getattr(order, "permId", None))
    if perm_id is None:
        perm_id = _to_int_or_none(getattr(status_obj, "permId", None))
    raw_status = str(getattr(status_obj, "status", "") or "").strip().upper()
    filled_qty = _to_float(getattr(status_obj, "filled", 0.0), default=0.0)
    remaining_qty = _to_float(getattr(status_obj, "remaining", 0.0), default=0.0)
    avg_fill_price_raw = getattr(status_obj, "avgFillPrice", None)
    avg_fill_price = None if avg_fill_price_raw is None else _to_float(avg_fill_price_raw, default=0.0)
    normalized_status, terminal = _normalize_order_status(
        raw_status=raw_status,
        filled_qty=filled_qty,
        remaining_qty=remaining_qty,
    )
    return {
        "order_id": order_id,
        "perm_id": perm_id,
        "status": raw_status,
        "normalized_status": normalized_status,
        "terminal": terminal,
        "filled_qty": filled_qty,
        "remaining_qty": remaining_qty,
        "avg_fill_price": avg_fill_price,
    }


def _extract_trade_perm_id(trade: Any) -> int | None:
    order = getattr(trade, "order", None)
    status_obj = getattr(trade, "orderStatus", None)
    perm_id = _to_int_or_none(getattr(order, "permId", None))
    if perm_id is not None:
        return perm_id
    return _to_int_or_none(getattr(status_obj, "permId", None))


def _extract_trade_error_message(trade: Any) -> str | None:
    raw_log = getattr(trade, "log", None)
    if not isinstance(raw_log, list):
        return None
    for item in reversed(raw_log):
        message = str(getattr(item, "message", "") or "").strip()
        if message:
            return message
        error_message = str(getattr(item, "errorMsg", "") or "").strip()
        if error_message:
            return error_message
    return None


class IBTradeService:
    def __init__(
        self,
        *,
        ib: Any | None = None,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
        timeout_seconds: float | None = None,
        account_code: str | None = None,
        trading_mode: str | None = None,
    ) -> None:
        app_cfg = load_app_config()
        cfg = app_cfg.ib_gateway
        mode = _normalize_trading_mode(str(trading_mode or cfg.trading_mode))
        resolved_port = int(port if port is not None else cfg.role_ports.order)
        resolved_mode = mode
        if resolved_port == int(cfg.live_port):
            resolved_mode = "live"
        elif resolved_port == int(cfg.paper_port):
            resolved_mode = "paper"
        self.host = str(host or cfg.host)
        self.port = resolved_port
        self.trading_mode = resolved_mode
        self.client_id = int(client_id if client_id is not None else resolve_ib_client_id("order"))
        self.timeout_seconds = float(timeout_seconds if timeout_seconds is not None else cfg.timeout_seconds)
        self.default_account_code = _normalize_account(account_code or cfg.account_code)
        self.allowed_sides = {str(item).strip().upper() for item in app_cfg.trade_validation.allowed_sides}
        self.allowed_order_types = {
            str(item).strip().upper() for item in app_cfg.trade_validation.allowed_order_types
        }
        self.allow_outside_rth = bool(app_cfg.trade_validation.allow_outside_rth)
        self.buy_open_max_amount_usd = float(app_cfg.trade_validation.buy_open_max_amount_usd)
        self.allow_live_orders = bool(app_cfg.trade_validation.allow_live_orders)
        self.readonly = bool(cfg.role_connections.order.readonly)
        self._ib = ib

    def _require_writable(self, *, action: str) -> None:
        if self.readonly:
            raise ValueError(f"ib gateway role_connections.order.readonly=true; {action} is not allowed")

    def _run_with_ib(self, callback: Any) -> Any:
        if self._ib is not None:
            _ensure_thread_event_loop()
            if not bool(getattr(self._ib, "isConnected", lambda: False)()):
                try:
                    self._ib.connect(
                        host=self.host,
                        port=self.port,
                        clientId=self.client_id,
                        timeout=self.timeout_seconds,
                        readonly=self.readonly,
                    )
                except Exception as exc:
                    raise IBTradeServiceError(
                        "failed to connect IB gateway"
                        f" host={self.host} port={self.port} client_id={self.client_id}: {exc}"
                    ) from exc
            try:
                return callback(self._ib)
            except Exception as exc:
                if isinstance(exc, ValueError):
                    raise
                if isinstance(exc, IBTradeServiceError):
                    raise
                detail = str(exc).strip() or exc.__class__.__name__
                raise IBTradeServiceError(f"ib order request failed: {detail}") from exc

        session = get_ib_session_manager().get_session(role="order")
        try:
            return session.run(callback)
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise
            if isinstance(exc, IBTradeServiceError):
                raise
            detail = str(exc).strip() or exc.__class__.__name__
            raise IBTradeServiceError(
                "ib order request failed"
                f" host={self.host} port={self.port} client_id={self.client_id}: {detail}"
            ) from exc

    def _build_ib_contract(self, *, validated: ValidatedStaticOrderPolicy) -> Any:
        try:
            Future = require_ib_attr("Future")
            Stock = require_ib_attr("Stock")
        except ModuleNotFoundError as exc:
            raise IBTradeServiceError(INSTALL_HINT) from exc

        profile = validated.market_profile
        if validated.action_type == "STOCK_TRADE":
            return Stock(
                symbol=validated.symbol,
                exchange=profile.exchange,
                currency=profile.currency,
            )
        if validated.action_type == "FUT_POSITION":
            kwargs: dict[str, Any] = {
                "symbol": validated.symbol,
                "exchange": profile.exchange,
                "currency": profile.currency,
            }
            if validated.contract_month is not None:
                kwargs["lastTradeDateOrContractMonth"] = validated.contract_month
            return Future(**kwargs)
        raise ValueError(f"unsupported action_type={validated.action_type or '<empty>'}")

    def _build_ib_order(
        self,
        *,
        validated: ValidatedStaticOrderPolicy,
        account_code: str | None,
        order_ref: str | None,
    ) -> tuple[Any, str, str, float]:
        side = validated.side
        quantity = validated.quantity
        order_type = validated.order_type
        tif = validated.tif
        outside_rth = validated.outside_rth

        try:
            LimitOrder = require_ib_attr("LimitOrder")
            MarketOrder = require_ib_attr("MarketOrder")
        except ModuleNotFoundError as exc:
            raise IBTradeServiceError(INSTALL_HINT) from exc

        if order_type == "LMT":
            if validated.limit_price is None:
                raise ValueError("trade_action.limit_price must be > 0 when order_type=LMT")
            order = LimitOrder(side, quantity, validated.limit_price)
        elif order_type == "MKT":
            order = MarketOrder(side, quantity)
        else:
            raise ValueError(f"unsupported order_type={order_type}")

        order.tif = tif
        order.outsideRth = outside_rth
        if account_code is not None:
            order.account = account_code
        if order_ref is not None and order_ref.strip():
            order.orderRef = order_ref.strip()

        return order, side, order_type, quantity

    def _extract_trade_action_context(
        self,
        *,
        trade_action: dict[str, Any],
    ) -> tuple[str, str | None]:
        if not isinstance(trade_action, dict):
            raise ValueError("trade_action must be an object")
        market = str(trade_action.get("market", "")).strip().upper()
        if not market:
            raise ValueError("trade_action.market is required")
        normalized_account = _normalize_account(trade_action.get("account_code")) or self.default_account_code
        return market, normalized_account

    def _qualify_and_validate_dynamic(
        self,
        *,
        ib: Any,
        contract: Any,
        trade_action: dict[str, Any],
        account_code: str | None,
        validated: ValidatedStaticOrderPolicy,
    ) -> Any:
        qualified = list(ib.qualifyContracts(contract))
        if not qualified:
            raise IBTradeServiceError("failed to qualify contract before placeOrder")
        resolved_contract = qualified[0]
        self._validate_dynamic_order_policy(
            ib=ib,
            resolved_contract=resolved_contract,
            trade_action=trade_action,
            account_code=account_code,
            validated=validated,
        )
        return resolved_contract

    def validate_trade_action(
        self,
        *,
        trade_action: dict[str, Any],
    ) -> TradeValidationResult:
        market, normalized_account = self._extract_trade_action_context(trade_action=trade_action)
        validated = self._validate_static_order_policy(
            market=market,
            trade_action=trade_action,
        )
        contract = self._build_ib_contract(validated=validated)

        def _validate(ib: Any) -> TradeValidationResult:
            resolved_contract = self._qualify_and_validate_dynamic(
                ib=ib,
                contract=contract,
                trade_action=trade_action,
                account_code=normalized_account,
                validated=validated,
            )
            return TradeValidationResult(
                con_id=_to_int_or_none(getattr(resolved_contract, "conId", None)),
                market=validated.market,
                symbol=validated.symbol,
                side=validated.side,
                order_type=validated.order_type,
                quantity=validated.quantity,
                account_code=normalized_account,
                validated=validated,
                resolved_contract=resolved_contract,
                validated_at=datetime.now(UTC),
            )

        return self._run_with_ib(_validate)

    def submit_prevalidated(
        self,
        *,
        validated_context: TradeValidationResult,
        order_ref: str | None = None,
    ) -> SubmitOrderResult:
        self._require_writable(action="submit order")
        if not isinstance(validated_context, TradeValidationResult):
            raise ValueError("validated_context must be TradeValidationResult")
        if self.trading_mode == "live" and not self.allow_live_orders:
            raise ValueError("live trading orders are disabled by trade_validation.allow_live_orders=false")
        validated = validated_context.validated
        resolved_contract = validated_context.resolved_contract
        normalized_account = _normalize_account(validated_context.account_code) or self.default_account_code
        order, side, order_type, quantity = self._build_ib_order(
            validated=validated,
            account_code=normalized_account,
            order_ref=order_ref,
        )

        def _submit(ib: Any) -> SubmitOrderResult:
            _ = ib
            con_id = _to_int_or_none(getattr(resolved_contract, "conId", None))
            trade = ib.placeOrder(resolved_contract, order)
            self._wait_trade_done(ib=ib, trade=trade)
            payload = _extract_order_status_payload(trade)
            if payload["perm_id"] is None:
                raise IBTradeServiceError(
                    "ib order request failed: placeOrder completed but permId is missing"
                )
            return SubmitOrderResult(
                con_id=con_id,
                order_id=payload["order_id"],
                perm_id=payload["perm_id"],
                status=payload["status"],
                normalized_status=payload["normalized_status"],
                terminal=payload["terminal"],
                filled_qty=payload["filled_qty"],
                remaining_qty=payload["remaining_qty"],
                avg_fill_price=payload["avg_fill_price"],
                symbol=validated.symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                account_code=normalized_account,
                submitted_at=datetime.now(UTC),
            )

        return self._run_with_ib(_submit)

    def _wait_trade_done(self, *, ib: Any, trade: Any) -> None:
        is_done = getattr(trade, "isDone", None)
        if not callable(is_done):
            return

        timeout = max(0.1, float(self.timeout_seconds))
        deadline = time.monotonic() + timeout
        while True:
            done = False
            try:
                done = bool(is_done())
            except Exception:
                return

            if done and _extract_trade_perm_id(trade) is not None:
                return

            if time.monotonic() >= deadline:
                return

            wait_on_update = getattr(ib, "waitOnUpdate", None)
            if callable(wait_on_update):
                try:
                    wait_on_update(timeout=0.2)
                    continue
                except TypeError:
                    try:
                        wait_on_update(0.2)
                        continue
                    except Exception:
                        pass
                except Exception:
                    pass

            sleep_fn = getattr(ib, "sleep", None)
            if callable(sleep_fn):
                try:
                    sleep_fn(0.05)
                    continue
                except Exception:
                    pass
            time.sleep(0.05)

    def submit_trade_action(
        self,
        *,
        trade_action: dict[str, Any],
        order_ref: str | None = None,
    ) -> SubmitOrderResult:
        validated_context = self.validate_trade_action(trade_action=trade_action)
        return self.submit_prevalidated(
            validated_context=validated_context,
            order_ref=order_ref,
        )

    def _validate_static_order_policy(
        self,
        *,
        market: str,
        trade_action: dict[str, Any],
    ) -> ValidatedStaticOrderPolicy:
        return validate_static_order_policy(
            market=market,
            trade_action=trade_action,
            allowed_sides=self.allowed_sides,
            allowed_order_types=self.allowed_order_types,
            allow_outside_rth=self.allow_outside_rth,
            buy_open_max_amount_usd=self.buy_open_max_amount_usd,
        )

    def _resolve_reference_price(
        self,
        *,
        ib: Any,
        resolved_contract: Any,
        trade_action: dict[str, Any],
        order_type: str,
    ) -> float | None:
        if order_type == "LMT":
            price = _to_float(trade_action.get("limit_price"), default=0.0)
            return price if price > 0 else None

        explicit_price = _to_float(trade_action.get("reference_price"), default=0.0)
        if explicit_price > 0:
            return explicit_price

        return self._resolve_reference_price_from_daily_close(
            ib=ib,
            resolved_contract=resolved_contract,
        )

    def _resolve_reference_price_from_daily_close(
        self,
        *,
        ib: Any,
        resolved_contract: Any,
    ) -> float | None:
        req_historical_data = getattr(ib, "reqHistoricalData", None)
        if not callable(req_historical_data):
            return None
        lookback_days = REFERENCE_PRICE_DAILY_LOOKBACK_DAYS
        try:
            bars = list(
                req_historical_data(
                    resolved_contract,  # type: ignore[arg-type]
                    endDateTime="",
                    durationStr=f"{lookback_days} D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=2,
                    keepUpToDate=False,
                )
            )
        except Exception:
            return None
        for bar in reversed(bars):
            close = _to_float(getattr(bar, "close", None), default=0.0)
            if close > 0:
                return close
        return None

    def _validate_dynamic_order_policy(
        self,
        *,
        ib: Any,
        resolved_contract: Any,
        trade_action: dict[str, Any],
        account_code: str | None,
        validated: ValidatedStaticOrderPolicy,
    ) -> None:
        values_float, filtered_positions = self._load_account_snapshot_for_check(
            ib=ib,
            account_code=account_code,
        )
        side = validated.side
        order_type = validated.order_type
        quantity = validated.quantity
        if side == "SELL":
            available_position = self._resolve_available_position_qty(
                positions=filtered_positions,
                resolved_contract=resolved_contract,
                symbol=validated.symbol,
            )
            if quantity - available_position > 1e-9:
                raise ValueError(
                    f"sell quantity {quantity:.4f} exceeds available position {available_position:.4f}"
                )
            return

        if side != "BUY":
            return
        cash = self._extract_account_cash(values_float)
        if cash is None:
            raise ValueError("failed to determine account cash from account snapshot")
        reference_price_for_cash = self._resolve_reference_price(
            ib=ib,
            resolved_contract=resolved_contract,
            trade_action=trade_action,
            order_type=order_type,
        )
        if reference_price_for_cash is None:
            raise ValueError(
                "failed to determine reference price for account cash check; "
                "use LMT with limit_price or provide trade_action.reference_price"
            )
        amount_for_cash = float(quantity) * float(reference_price_for_cash)
        if amount_for_cash - cash > 1e-9:
            raise ValueError(
                f"buy amount {amount_for_cash:.2f} exceeds account cash {cash:.2f}"
            )

        max_amount = float(self.buy_open_max_amount_usd)
        if max_amount <= 0:
            return
        if order_type == "LMT":
            return
        reference_price = self._resolve_reference_price(
            ib=ib,
            resolved_contract=resolved_contract,
            trade_action=trade_action,
            order_type=order_type,
        )
        if reference_price is None:
            raise ValueError(
                "failed to determine reference price for buy/open max amount check; "
                "use LMT with limit_price or provide trade_action.reference_price"
            )
        amount = float(quantity) * float(reference_price)
        if amount - max_amount > 1e-9:
            raise ValueError(
                f"buy/open amount {amount:.2f} exceeds configured max {max_amount:.2f} USD"
            )

    def _load_account_snapshot_for_check(
        self,
        *,
        ib: Any,
        account_code: str | None,
    ) -> tuple[dict[str, float], list[Any]]:
        account_summary_fn = getattr(ib, "accountSummary", None)
        portfolio_fn = getattr(ib, "portfolio", None)
        if not callable(account_summary_fn) or not callable(portfolio_fn):
            raise ValueError("ib account snapshot API is unavailable")
        summary_items = list(account_summary_fn())
        portfolio_items = list(portfolio_fn())

        target_account = _normalize_account(account_code)
        if target_account is None and summary_items:
            target_account = _normalize_account(getattr(summary_items[0], "account", None))
        if target_account is None and portfolio_items:
            target_account = _normalize_account(getattr(portfolio_items[0], "account", None))

        filtered_summary = summary_items
        filtered_portfolio = portfolio_items
        if target_account is not None:
            filtered_summary = [
                item
                for item in summary_items
                if _normalize_account(getattr(item, "account", None)) == target_account
            ]
            filtered_portfolio = [
                item
                for item in portfolio_items
                if _normalize_account(getattr(item, "account", None)) == target_account
            ]

        values_float: dict[str, float] = {}
        for item in filtered_summary:
            tag = str(getattr(item, "tag", "")).strip()
            if not tag:
                continue
            parsed = _to_float_or_none(getattr(item, "value", None))
            if parsed is None:
                continue
            values_float[tag] = parsed
        return values_float, filtered_portfolio

    def _extract_account_cash(self, values_float: dict[str, float]) -> float | None:
        for tag in ("TotalCashValue", "CashBalance", "AvailableFunds", "ExcessLiquidity"):
            value = values_float.get(tag)
            if value is None:
                continue
            return float(value)
        return None

    def _resolve_available_position_qty(
        self,
        *,
        positions: list[Any],
        resolved_contract: Any,
        symbol: str,
    ) -> float:
        target_con_id = _to_int_or_none(getattr(resolved_contract, "conId", None))
        target_symbol = str(
            getattr(resolved_contract, "localSymbol", "")
            or getattr(resolved_contract, "symbol", "")
            or symbol
            or ""
        ).strip().upper()
        target_sec_type = str(getattr(resolved_contract, "secType", "") or "").strip().upper()
        available = 0.0
        for item in positions:
            contract = getattr(item, "contract", None)
            if contract is None:
                continue
            position_qty = _to_float(getattr(item, "position", 0.0), default=0.0)
            if position_qty <= 0:
                continue
            item_con_id = _to_int_or_none(getattr(contract, "conId", None))
            if target_con_id is not None and item_con_id == target_con_id:
                available += position_qty
                continue
            item_symbol = str(
                getattr(contract, "localSymbol", "")
                or getattr(contract, "symbol", "")
                or ""
            ).strip().upper()
            if target_symbol and item_symbol != target_symbol:
                continue
            if target_sec_type:
                item_sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
                if item_sec_type and item_sec_type != target_sec_type:
                    continue
            available += position_qty
        return available

    def poll_order_status(
        self,
        *,
        order_id: int | None = None,
        perm_id: int | None = None,
    ) -> OrderStatusSnapshot | None:
        target_order_id = _to_int_or_none(order_id)
        target_perm_id = _to_int_or_none(perm_id)
        if target_order_id is None and target_perm_id is None:
            raise ValueError("order_id or perm_id is required")

        def _poll(ib: Any) -> OrderStatusSnapshot | None:
            candidates = self._collect_trade_candidates(ib)
            for trade in reversed(candidates):
                payload = _extract_order_status_payload(trade)
                row_order_id = payload["order_id"]
                row_perm_id = payload["perm_id"]
                if target_order_id is not None and row_order_id != target_order_id:
                    continue
                if target_perm_id is not None and row_perm_id != target_perm_id:
                    continue
                return OrderStatusSnapshot(
                    order_id=row_order_id,
                    perm_id=row_perm_id,
                    status=payload["status"],
                    normalized_status=payload["normalized_status"],
                    terminal=payload["terminal"],
                    filled_qty=payload["filled_qty"],
                    remaining_qty=payload["remaining_qty"],
                    avg_fill_price=payload["avg_fill_price"],
                    error_message=_extract_trade_error_message(trade),
                    updated_at=datetime.now(UTC),
                )
            return None

        return self._run_with_ib(_poll)

    def poll_order_status_by_order_ref(self, *, order_ref: str) -> OrderStatusSnapshot | None:
        target_order_ref = _normalize_order_ref(order_ref)
        if target_order_ref is None:
            raise ValueError("order_ref is required")

        def _poll(ib: Any) -> OrderStatusSnapshot | None:
            candidates = self._collect_trade_candidates(ib)
            for trade in reversed(candidates):
                order = getattr(trade, "order", None)
                row_order_ref = _normalize_order_ref(getattr(order, "orderRef", None))
                if row_order_ref != target_order_ref:
                    continue
                payload = _extract_order_status_payload(trade)
                return OrderStatusSnapshot(
                    order_id=payload["order_id"],
                    perm_id=payload["perm_id"],
                    status=payload["status"],
                    normalized_status=payload["normalized_status"],
                    terminal=payload["terminal"],
                    filled_qty=payload["filled_qty"],
                    remaining_qty=payload["remaining_qty"],
                    avg_fill_price=payload["avg_fill_price"],
                    error_message=_extract_trade_error_message(trade),
                    updated_at=datetime.now(UTC),
                )
            return None

        return self._run_with_ib(_poll)

    def cancel_order(
        self,
        *,
        perm_id: int | None = None,
        order_id: int | None = None,
        wait_for_terminal: bool = True,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.5,
    ) -> OrderStatusSnapshot | None:
        self._require_writable(action="cancel order")
        target_order_id = _to_int_or_none(order_id)
        target_perm_id = _to_int_or_none(perm_id)
        if target_order_id is None and target_perm_id is None:
            raise ValueError("order_id or perm_id is required")

        def _cancel(ib: Any) -> tuple[int | None, int | None, OrderStatusSnapshot] | None:
            candidates = self._collect_trade_candidates(ib)
            matched_trade: Any | None = None
            matched_payload: dict[str, Any] | None = None
            for trade in reversed(candidates):
                payload = _extract_order_status_payload(trade)
                if target_order_id is not None and payload["order_id"] != target_order_id:
                    continue
                if target_perm_id is not None and payload["perm_id"] != target_perm_id:
                    continue
                matched_trade = trade
                matched_payload = payload
                break
            if matched_trade is None or matched_payload is None:
                return None

            order = getattr(matched_trade, "order", None)
            order_client_id = _to_int_including_zero(getattr(order, "clientId", None))
            if (
                self.client_id != 0
                and order_client_id is not None
                and order_client_id != self.client_id
            ):
                raise IBTradeServiceError(
                    "cancelOrder rejected: order belongs to a different clientId"
                    f" order_client_id={order_client_id} current_client_id={self.client_id}"
                )
            cancel_order = getattr(ib, "cancelOrder", None)
            if callable(cancel_order):
                try:
                    cancel_order(order)
                except TypeError:
                    cancel_order(matched_trade)
            else:
                cancel_trade = getattr(matched_trade, "cancel", None)
                if callable(cancel_trade):
                    cancel_trade()
                else:
                    raise IBTradeServiceError("ib cancelOrder API is unavailable")

            payload_after = _extract_order_status_payload(matched_trade)
            resolved_order_id = payload_after["order_id"] or matched_payload["order_id"]
            resolved_perm_id = payload_after["perm_id"] or matched_payload["perm_id"]
            snapshot = OrderStatusSnapshot(
                order_id=resolved_order_id,
                perm_id=resolved_perm_id,
                status=payload_after["status"],
                normalized_status=payload_after["normalized_status"],
                terminal=payload_after["terminal"],
                filled_qty=payload_after["filled_qty"],
                remaining_qty=payload_after["remaining_qty"],
                avg_fill_price=payload_after["avg_fill_price"],
                error_message=_extract_trade_error_message(matched_trade),
                updated_at=datetime.now(UTC),
            )
            return resolved_order_id, resolved_perm_id, snapshot

        cancelled = self._run_with_ib(_cancel)
        if cancelled is None:
            return None
        resolved_order_id, resolved_perm_id, snapshot = cancelled
        if not wait_for_terminal:
            return snapshot
        if resolved_order_id is None and resolved_perm_id is None:
            return snapshot
        wait_timeout = float(timeout_seconds if timeout_seconds is not None else self.timeout_seconds)
        terminal = self.wait_for_terminal_status(
            order_id=resolved_order_id,
            perm_id=resolved_perm_id,
            timeout_seconds=wait_timeout,
            poll_interval_seconds=poll_interval_seconds,
        )
        return terminal or snapshot

    def _collect_trade_candidates(self, ib: Any) -> list[Any]:
        candidates: list[Any] = []
        # ib_async docs recommend openTrades/trades over reqOpenOrders/reqAllOpenOrders
        # because the latter can be stale. Keep stale APIs as fallback only by
        # ordering them earlier so reversed iteration prefers live trade objects.
        for attr in ("reqAllOpenOrders", "reqOpenOrders", "trades", "openTrades"):
            fn = getattr(ib, attr, None)
            if not callable(fn):
                continue
            try:
                items = list(fn())
            except Exception:
                items = []
            candidates.extend(items)
        return candidates

    def list_active_orders(self) -> list[ActiveOrderSnapshot]:
        def _list(ib: Any) -> list[ActiveOrderSnapshot]:
            rows: list[ActiveOrderSnapshot] = []
            for trade in self._collect_trade_candidates(ib):
                payload = _extract_order_status_payload(trade)
                if bool(payload["terminal"]):
                    continue
                contract = getattr(trade, "contract", None)
                order = getattr(trade, "order", None)
                sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
                symbol = str(
                    getattr(contract, "localSymbol", "")
                    or getattr(contract, "symbol", "")
                    or ""
                ).strip().upper()
                quantity = _to_float(getattr(order, "totalQuantity", 0.0), default=0.0)
                limit_price_raw = getattr(order, "lmtPrice", None)
                limit_price = None if limit_price_raw is None else _to_float(limit_price_raw, default=0.0)
                if limit_price is not None and limit_price <= 0:
                    limit_price = None
                rows.append(
                    ActiveOrderSnapshot(
                        con_id=_to_int_or_none(getattr(contract, "conId", None)),
                        client_id=_to_int_including_zero(getattr(order, "clientId", None)),
                        symbol=symbol,
                        sec_type=sec_type,
                        side=str(getattr(order, "action", "") or "").strip().upper(),
                        order_type=str(getattr(order, "orderType", "") or "").strip().upper(),
                        quantity=quantity,
                        limit_price=limit_price,
                        order_id=payload["order_id"],
                        perm_id=payload["perm_id"],
                        status=payload["status"],
                        normalized_status=payload["normalized_status"],
                        terminal=bool(payload["terminal"]),
                        filled_qty=payload["filled_qty"],
                        remaining_qty=payload["remaining_qty"],
                        avg_fill_price=payload["avg_fill_price"],
                        account_code=_normalize_account(getattr(order, "account", None)),
                        updated_at=_extract_trade_updated_at(trade),
                    )
                )
            rows.sort(
                key=lambda item: (
                    int(item.order_id or 0),
                    int(item.perm_id or 0),
                ),
                reverse=True,
            )
            deduped: list[ActiveOrderSnapshot] = []
            seen: set[tuple[int | None, int | None]] = set()
            for row in rows:
                key = (row.order_id, row.perm_id)
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(row)
            return deduped

        return self._run_with_ib(_list)

    def wait_for_terminal_status(
        self,
        *,
        order_id: int | None = None,
        perm_id: int | None = None,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 1.0,
    ) -> OrderStatusSnapshot | None:
        target_order_id = _to_int_or_none(order_id)
        target_perm_id = _to_int_or_none(perm_id)
        if target_order_id is None and target_perm_id is None:
            raise ValueError("order_id or perm_id is required")
        timeout = max(0.1, float(timeout_seconds))
        interval = max(0.05, float(poll_interval_seconds))
        deadline = time.monotonic() + timeout
        latest: OrderStatusSnapshot | None = None
        while time.monotonic() < deadline:
            snapshot = self.poll_order_status(order_id=target_order_id, perm_id=target_perm_id)
            if snapshot is not None:
                latest = snapshot
                if snapshot.terminal:
                    return snapshot
            time.sleep(interval)
        return latest


IBOrderServiceError = IBTradeServiceError
IBOrderService = IBTradeService
