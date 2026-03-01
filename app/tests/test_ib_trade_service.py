from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace

import pytest

from app.ib_trade_service import IBOrderService, IBOrderServiceError, OrderStatusSnapshot
from app.market_config import MarketProfile


UTC = timezone.utc


def _stock_profile() -> MarketProfile:
    return MarketProfile(
        market="US_STOCK",
        sec_type="STK",
        exchange="SMART",
        currency="USD",
        allowed_trade_types=frozenset({"buy", "sell"}),
    )


def _future_profile() -> MarketProfile:
    return MarketProfile(
        market="COMEX_FUTURES",
        sec_type="FUT",
        exchange="COMEX",
        currency="USD",
        allowed_trade_types=frozenset({"open", "close", "spread"}),
    )


def _install_fake_ib_insync(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    module = ModuleType("ib_insync")

    class Stock:
        def __init__(self, *, symbol: str, exchange: str, currency: str) -> None:
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency

    class Future:
        def __init__(
            self,
            *,
            symbol: str,
            exchange: str,
            currency: str,
            lastTradeDateOrContractMonth: str | None = None,
        ) -> None:
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency
            self.lastTradeDateOrContractMonth = lastTradeDateOrContractMonth

    class MarketOrder:
        def __init__(self, action: str, quantity: float) -> None:
            self.action = action
            self.totalQuantity = quantity
            self.orderType = "MKT"
            self.orderId = None
            self.permId = None
            self.tif = "DAY"
            self.outsideRth = False
            self.account = None
            self.orderRef = None

    class LimitOrder(MarketOrder):
        def __init__(self, action: str, quantity: float, limit_price: float) -> None:
            super().__init__(action, quantity)
            self.orderType = "LMT"
            self.lmtPrice = limit_price

    module.Stock = Stock
    module.Future = Future
    module.MarketOrder = MarketOrder
    module.LimitOrder = LimitOrder
    monkeypatch.setitem(sys.modules, "ib_insync", module)


@dataclass
class _FakeTrade:
    contract: object | None
    order: object
    orderStatus: object
    log: list[object]


class _FakeIB:
    def __init__(self) -> None:
        self.connected = False
        self.connect_calls: list[dict[str, object]] = []
        self.last_contract: object | None = None
        self.last_order: object | None = None
        self._next_order_id = 2000
        self._next_perm_id = 8000
        self._trades: list[_FakeTrade] = []

    def connect(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.connected = True
        self.connect_calls.append(kwargs)

    def disconnect(self) -> None:
        self.connected = False

    def isConnected(self) -> bool:
        return self.connected

    def qualifyContracts(self, contract):  # type: ignore[no-untyped-def]
        return [contract]

    def placeOrder(self, contract, order):  # type: ignore[no-untyped-def]
        self.last_contract = contract
        self.last_order = order
        self._next_order_id += 1
        self._next_perm_id += 1
        order.orderId = self._next_order_id
        order.permId = self._next_perm_id
        trade = _FakeTrade(
            contract=contract,
            order=order,
            orderStatus=SimpleNamespace(
                status="Submitted",
                filled=0.0,
                remaining=float(getattr(order, "totalQuantity", 0.0) or 0.0),
                avgFillPrice=0.0,
            ),
            log=[],
        )
        self._trades.append(trade)
        return trade

    def trades(self):  # type: ignore[no-untyped-def]
        return list(self._trades)

    def openTrades(self):  # type: ignore[no-untyped-def]
        return list(self._trades)

    def reqOpenOrders(self):  # type: ignore[no-untyped-def]
        return list(self._trades)


def test_submit_trade_action_stock_market(monkeypatch) -> None:
    _install_fake_ib_insync(monkeypatch)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )

    svc = IBOrderService(
        ib=fake_ib,
        host="127.0.0.1",
        port=4002,
        client_id=97,
        timeout_seconds=5.0,
    )
    result = svc.submit_trade_action(
        market="US_STOCK",
        trade_action={
            "action_type": "STOCK_TRADE",
            "symbol": "AAPL",
            "side": "BUY",
            "order_type": "MKT",
            "quantity": 3,
            "tif": "DAY",
            "allow_overnight": False,
        },
        account_code="DU123",
        order_ref="T-UNIT-1",
    )

    assert len(fake_ib.connect_calls) == 1
    assert int(fake_ib.connect_calls[0]["clientId"]) == 97
    assert bool(fake_ib.connect_calls[0]["readonly"]) is False
    assert str(getattr(fake_ib.last_contract, "symbol", "")) == "AAPL"
    assert str(getattr(fake_ib.last_order, "action", "")) == "BUY"
    assert float(getattr(fake_ib.last_order, "totalQuantity", 0.0)) == 3.0
    assert str(getattr(fake_ib.last_order, "orderType", "")) == "MKT"
    assert str(getattr(fake_ib.last_order, "account", "")) == "DU123"
    assert str(getattr(fake_ib.last_order, "orderRef", "")) == "T-UNIT-1"
    assert result.order_id is not None
    assert result.perm_id is not None
    assert result.normalized_status == "ORDER_SUBMITTED"


def test_submit_trade_action_future_limit(monkeypatch) -> None:
    _install_fake_ib_insync(monkeypatch)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _future_profile(),
    )

    svc = IBOrderService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    result = svc.submit_trade_action(
        market="COMEX_FUTURES",
        trade_action={
            "action_type": "FUT_POSITION",
            "symbol": "GC",
            "contract": "202612",
            "side": "SELL",
            "order_type": "LMT",
            "limit_price": 2800.5,
            "quantity": 1,
            "allow_overnight": True,
        },
    )

    assert str(getattr(fake_ib.last_contract, "lastTradeDateOrContractMonth", "")) == "202612"
    assert str(getattr(fake_ib.last_order, "orderType", "")) == "LMT"
    assert float(getattr(fake_ib.last_order, "lmtPrice", 0.0)) == 2800.5
    assert bool(getattr(fake_ib.last_order, "outsideRth", False)) is True
    assert result.order_type == "LMT"
    assert result.side == "SELL"


def test_submit_trade_action_limit_requires_price(monkeypatch) -> None:
    _install_fake_ib_insync(monkeypatch)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBOrderService(ib=fake_ib, client_id=97, timeout_seconds=5.0)

    with pytest.raises(ValueError, match="limit_price"):
        svc.submit_trade_action(
            market="US_STOCK",
            trade_action={
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "LMT",
                "quantity": 1,
            },
        )


def test_poll_order_status_maps_filled(monkeypatch) -> None:
    _install_fake_ib_insync(monkeypatch)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )

    trade = _FakeTrade(
        contract=SimpleNamespace(conId=901, symbol="AAPL", localSymbol="AAPL", secType="STK"),
        order=SimpleNamespace(orderId=2101, permId=8101),
        orderStatus=SimpleNamespace(status="Filled", filled=2.0, remaining=0.0, avgFillPrice=175.2),
        log=[],
    )
    fake_ib._trades.append(trade)
    svc = IBOrderService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    snapshot = svc.poll_order_status(order_id=2101)
    assert snapshot is not None
    assert snapshot.normalized_status == "FILLED"
    assert snapshot.terminal is True
    assert snapshot.filled_qty == 2.0


def test_poll_order_status_returns_none_when_not_found(monkeypatch) -> None:
    _install_fake_ib_insync(monkeypatch)
    fake_ib = _FakeIB()
    svc = IBOrderService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    assert svc.poll_order_status(order_id=999999) is None


def test_submit_trade_action_wraps_ib_errors(monkeypatch) -> None:
    _install_fake_ib_insync(monkeypatch)

    class _BrokenIB(_FakeIB):
        def placeOrder(self, contract, order):  # type: ignore[no-untyped-def]
            _ = (contract, order)
            raise RuntimeError("gateway rejected")

    fake_ib = _BrokenIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBOrderService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(IBOrderServiceError, match="gateway rejected"):
        svc.submit_trade_action(
            market="US_STOCK",
            trade_action={
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 1,
            },
        )


def test_list_active_orders_returns_non_terminal_only(monkeypatch) -> None:
    _install_fake_ib_insync(monkeypatch)
    fake_ib = _FakeIB()
    fake_ib._trades.extend(
        [
            _FakeTrade(
                contract=SimpleNamespace(conId=901, symbol="AAPL", localSymbol="AAPL", secType="STK"),
                order=SimpleNamespace(
                    orderId=2101,
                    permId=8101,
                    action="BUY",
                    orderType="LMT",
                    totalQuantity=2.0,
                    lmtPrice=170.5,
                    account="DU123",
                ),
                orderStatus=SimpleNamespace(
                    status="Submitted",
                    filled=0.0,
                    remaining=2.0,
                    avgFillPrice=0.0,
                ),
                log=[],
            ),
            _FakeTrade(
                contract=SimpleNamespace(conId=902, symbol="MSFT", localSymbol="MSFT", secType="STK"),
                order=SimpleNamespace(
                    orderId=2102,
                    permId=8102,
                    action="SELL",
                    orderType="MKT",
                    totalQuantity=1.0,
                    lmtPrice=None,
                    account="DU123",
                ),
                orderStatus=SimpleNamespace(
                    status="Filled",
                    filled=1.0,
                    remaining=0.0,
                    avgFillPrice=420.2,
                ),
                log=[],
            ),
        ]
    )
    svc = IBOrderService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    rows = svc.list_active_orders()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].order_id == 2101
    assert rows[0].normalized_status == "ORDER_SUBMITTED"


def test_wait_for_terminal_status_returns_terminal_snapshot(monkeypatch) -> None:
    _install_fake_ib_insync(monkeypatch)
    fake_ib = _FakeIB()
    svc = IBOrderService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    queue = [
        OrderStatusSnapshot(
            order_id=123,
            perm_id=456,
            status="SUBMITTED",
            normalized_status="ORDER_SUBMITTED",
            terminal=False,
            filled_qty=0.0,
            remaining_qty=1.0,
            avg_fill_price=None,
            error_message=None,
            updated_at=datetime.now(UTC),
        ),
        OrderStatusSnapshot(
            order_id=123,
            perm_id=456,
            status="FILLED",
            normalized_status="FILLED",
            terminal=True,
            filled_qty=1.0,
            remaining_qty=0.0,
            avg_fill_price=100.0,
            error_message=None,
            updated_at=datetime.now(UTC),
        ),
    ]

    def _fake_poll(*, order_id=None, perm_id=None):  # type: ignore[no-untyped-def]
        _ = (order_id, perm_id)
        if queue:
            return queue.pop(0)
        return None

    monkeypatch.setattr(svc, "poll_order_status", _fake_poll)
    snapshot = svc.wait_for_terminal_status(order_id=123, timeout_seconds=1.0, poll_interval_seconds=0.01)
    assert snapshot is not None
    assert snapshot.terminal is True
    assert snapshot.normalized_status == "FILLED"
