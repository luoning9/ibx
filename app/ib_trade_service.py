from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Any

from .config import infer_ib_api_port, load_app_config, resolve_ib_client_id
from .ib_session_manager import get_ib_session_manager
from .market_config import resolve_market_profile


UTC = timezone.utc
TERMINAL_ORDER_STATUSES: set[str] = {"FILLED", "CANCELLED", "FAILED"}


class IBOrderServiceError(RuntimeError):
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
    updated_at: datetime


def _normalize_account(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized if normalized else None


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _to_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


class IBOrderService:
    def __init__(
        self,
        *,
        ib: Any | None = None,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
        timeout_seconds: float | None = None,
        session_idle_ttl_seconds: float | None = None,
        account_code: str | None = None,
        trading_mode: str | None = None,
        readonly: bool = False,
    ) -> None:
        cfg = load_app_config().ib_gateway
        mode = str(trading_mode or cfg.trading_mode).strip().lower()
        self.host = str(host or cfg.host)
        self.port = int(port if port is not None else infer_ib_api_port(mode))
        self.client_id = int(client_id if client_id is not None else resolve_ib_client_id("order"))
        self.timeout_seconds = float(timeout_seconds if timeout_seconds is not None else cfg.timeout_seconds)
        self.session_idle_ttl_seconds = float(
            session_idle_ttl_seconds if session_idle_ttl_seconds is not None else cfg.session_idle_ttl_seconds
        )
        self.default_account_code = _normalize_account(account_code or cfg.account_code)
        self.readonly = bool(readonly)
        self._ib = ib

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
                    raise IBOrderServiceError(
                        "failed to connect IB gateway"
                        f" host={self.host} port={self.port} client_id={self.client_id}: {exc}"
                    ) from exc
            try:
                return callback(self._ib)
            except Exception as exc:
                detail = str(exc).strip() or exc.__class__.__name__
                raise IBOrderServiceError(f"ib order request failed: {detail}") from exc

        session = get_ib_session_manager().get_session(
            host=self.host,
            port=self.port,
            client_id=self.client_id,
            timeout_seconds=self.timeout_seconds,
            readonly=self.readonly,
            idle_ttl_seconds=self.session_idle_ttl_seconds,
        )
        try:
            return session.run(callback)
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise IBOrderServiceError(
                "ib order request failed"
                f" host={self.host} port={self.port} client_id={self.client_id}: {detail}"
            ) from exc

    def _build_ib_contract(self, *, market: str, trade_action: dict[str, Any]) -> Any:
        profile = resolve_market_profile(market, None)
        action_type = str(trade_action.get("action_type", "")).strip().upper()
        symbol = str(trade_action.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("trade_action.symbol is required")

        try:
            from ib_insync import Future, Stock
        except ModuleNotFoundError as exc:
            raise IBOrderServiceError(
                "Missing dependency: ib_insync. Install with: pip install ib_insync"
            ) from exc

        if action_type == "STOCK_TRADE":
            if profile.sec_type != "STK":
                raise ValueError(f"market={profile.market} does not support STOCK_TRADE")
            return Stock(symbol=symbol, exchange=profile.exchange, currency=profile.currency)
        if action_type == "FUT_POSITION":
            if profile.sec_type != "FUT":
                raise ValueError(f"market={profile.market} does not support FUT_POSITION")
            contract_month = str(trade_action.get("contract", "") or "").strip() or None
            kwargs: dict[str, Any] = {
                "symbol": symbol,
                "exchange": profile.exchange,
                "currency": profile.currency,
            }
            if contract_month is not None:
                kwargs["lastTradeDateOrContractMonth"] = contract_month
            return Future(**kwargs)
        if action_type == "FUT_ROLL":
            raise ValueError("FUT_ROLL requires multi-leg execution and is not supported by submit_trade_action")
        raise ValueError(f"unsupported action_type={action_type or '<empty>'}")

    def _build_ib_order(
        self,
        *,
        trade_action: dict[str, Any],
        account_code: str | None,
        order_ref: str | None,
    ) -> tuple[str, str, float, Any]:
        side = str(trade_action.get("side", "")).strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("trade_action.side must be BUY or SELL")
        quantity = _to_float(trade_action.get("quantity"), default=0.0)
        if quantity <= 0:
            raise ValueError("trade_action.quantity must be > 0")

        order_type = str(trade_action.get("order_type", "MKT")).strip().upper() or "MKT"
        tif = str(trade_action.get("tif", "DAY")).strip().upper() or "DAY"
        outside_rth = bool(trade_action.get("allow_overnight", False))

        try:
            from ib_insync import LimitOrder, MarketOrder
        except ModuleNotFoundError as exc:
            raise IBOrderServiceError(
                "Missing dependency: ib_insync. Install with: pip install ib_insync"
            ) from exc

        if order_type == "LMT":
            limit_price = _to_float(trade_action.get("limit_price"), default=0.0)
            if limit_price <= 0:
                raise ValueError("trade_action.limit_price must be > 0 when order_type=LMT")
            order = LimitOrder(side, quantity, limit_price)
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

        return side, order_type, quantity, order

    def submit_trade_action(
        self,
        *,
        market: str,
        trade_action: dict[str, Any],
        account_code: str | None = None,
        order_ref: str | None = None,
    ) -> SubmitOrderResult:
        if not isinstance(trade_action, dict):
            raise ValueError("trade_action must be an object")
        normalized_account = _normalize_account(account_code) or self.default_account_code
        contract = self._build_ib_contract(market=market, trade_action=trade_action)
        symbol = str(trade_action.get("symbol", "")).strip().upper()
        side, order_type, quantity, order = self._build_ib_order(
            trade_action=trade_action,
            account_code=normalized_account,
            order_ref=order_ref,
        )

        def _submit(ib: Any) -> SubmitOrderResult:
            qualified = list(ib.qualifyContracts(contract))
            if not qualified:
                raise IBOrderServiceError("failed to qualify contract before placeOrder")
            resolved_contract = qualified[0]
            con_id = _to_int_or_none(getattr(resolved_contract, "conId", None))
            trade = ib.placeOrder(resolved_contract, order)
            payload = _extract_order_status_payload(trade)
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
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                account_code=normalized_account,
                submitted_at=datetime.now(UTC),
            )

        return self._run_with_ib(_submit)

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

    def _collect_trade_candidates(self, ib: Any) -> list[Any]:
        candidates: list[Any] = []
        for attr in ("trades", "openTrades", "reqOpenOrders"):
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
                        updated_at=datetime.now(UTC),
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
