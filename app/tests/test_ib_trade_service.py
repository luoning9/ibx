from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace

import pytest

from app.ib_trade_service import IBTradeService, IBTradeServiceError, OrderStatusSnapshot
from app.market_config import MarketProfile


UTC = timezone.utc


def _mock_trade_validation_config(
    monkeypatch,
    *,
    allowed_sides: tuple[str, ...] = ("BUY", "SELL"),
    allowed_order_types: tuple[str, ...] = ("MKT", "LMT"),
    allow_outside_rth: bool = True,
    buy_open_max_amount_usd: float = 1000.0,
    allow_live_orders: bool = False,
    trading_mode: str = "paper",
    readonly: bool = False,
) -> None:  # type: ignore[no-untyped-def]
    cfg = SimpleNamespace(
        ib_gateway=SimpleNamespace(
            host="127.0.0.1",
            paper_port=4002,
            live_port=4001,
            role_ports=SimpleNamespace(
                broker_data=4002,
                market_data=4002,
                order=4002,
                cli=4002,
            ),
            timeout_seconds=5.0,
            account_code="",
            trading_mode=trading_mode,
            role_connections=SimpleNamespace(
                broker_data=SimpleNamespace(client_id=99, readonly=True),
                market_data=SimpleNamespace(client_id=98, readonly=True),
                order=SimpleNamespace(client_id=96, readonly=readonly),
                cli=SimpleNamespace(client_id=97, readonly=True),
            ),
        ),
        trade_validation=SimpleNamespace(
            allowed_sides=allowed_sides,
            allowed_order_types=allowed_order_types,
            allow_outside_rth=allow_outside_rth,
            buy_open_max_amount_usd=buy_open_max_amount_usd,
            allow_live_orders=allow_live_orders,
        ),
    )
    monkeypatch.setattr("app.ib_trade_service.load_app_config", lambda: cfg)


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


def _with_context(
    payload: dict[str, object],
    *,
    market: str = "US_STOCK",
    account_code: str = "DU123",
) -> dict[str, object]:
    out = dict(payload)
    out.setdefault("market", market)
    out.setdefault("account_code", account_code)
    return out


def _install_fake_ib_async(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    module = ModuleType("ib_async")

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
    monkeypatch.setitem(sys.modules, "ib_async", module)


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
        self.qualify_calls = 0
        self.last_contract: object | None = None
        self.last_order: object | None = None
        self.cancel_calls: list[dict[str, object]] = []
        self._next_order_id = 2000
        self._next_perm_id = 8000
        self._trades: list[_FakeTrade] = []
        self._summary_items: list[object] = [
            SimpleNamespace(account="DU123", tag="TotalCashValue", value="100000", currency="USD"),
            SimpleNamespace(account="DU123", tag="AvailableFunds", value="100000", currency="USD"),
        ]
        self._portfolio_items: list[object] = [
            SimpleNamespace(
                account="DU123",
                contract=SimpleNamespace(conId=101, symbol="AAPL", localSymbol="AAPL", secType="STK"),
                position=10.0,
            ),
            SimpleNamespace(
                account="DU123",
                contract=SimpleNamespace(conId=202, symbol="GC", localSymbol="GC", secType="FUT"),
                position=5.0,
            ),
        ]

    def connect(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.connected = True
        self.connect_calls.append(kwargs)

    def disconnect(self) -> None:
        self.connected = False

    def isConnected(self) -> bool:
        return self.connected

    def qualifyContracts(self, contract):  # type: ignore[no-untyped-def]
        self.qualify_calls += 1
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

    def cancelOrder(self, order):  # type: ignore[no-untyped-def]
        if order is None:
            return
        order_id = int(getattr(order, "orderId", 0) or 0)
        perm_id = int(getattr(order, "permId", 0) or 0)
        self.cancel_calls.append({"order_id": order_id, "perm_id": perm_id})
        for trade in self._trades:
            trade_order = getattr(trade, "order", None)
            trade_order_id = int(getattr(trade_order, "orderId", 0) or 0)
            trade_perm_id = int(getattr(trade_order, "permId", 0) or 0)
            if order_id > 0 and trade_order_id == order_id:
                trade.orderStatus.status = "Cancelled"
                trade.orderStatus.remaining = 0.0
                return
            if perm_id > 0 and trade_perm_id == perm_id:
                trade.orderStatus.status = "Cancelled"
                trade.orderStatus.remaining = 0.0
                return

    def trades(self):  # type: ignore[no-untyped-def]
        return list(self._trades)

    def openTrades(self):  # type: ignore[no-untyped-def]
        return list(self._trades)

    def reqOpenOrders(self):  # type: ignore[no-untyped-def]
        return list(self._trades)

    def reqTickers(self, *contracts):  # type: ignore[no-untyped-def]
        _ = contracts
        ticker = SimpleNamespace(
            marketPrice=lambda: 100.0,
            last=100.0,
            close=100.0,
            ask=100.1,
            bid=99.9,
        )
        return [ticker]

    def reqHistoricalData(self, contract, **kwargs):  # type: ignore[no-untyped-def]
        _ = contract
        self.last_historical_data_kwargs = dict(kwargs)
        return [
            SimpleNamespace(date="20260225", close=98.5),
            SimpleNamespace(date="20260226", close=101.2),
        ]

    def accountSummary(self):  # type: ignore[no-untyped-def]
        return list(self._summary_items)

    def portfolio(self):  # type: ignore[no-untyped-def]
        return list(self._portfolio_items)


def test_submit_trade_action_stock_market(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )

    svc = IBTradeService(
        ib=fake_ib,
        host="127.0.0.1",
        port=4002,
        client_id=97,
        timeout_seconds=5.0,
    )
    result = svc.submit_trade_action(
        trade_action=_with_context(
            {
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 3,
                "tif": "DAY",
                "allow_overnight": False,
            }
        ),
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
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _future_profile(),
    )

    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    result = svc.submit_trade_action(
        trade_action=_with_context(
            {
                "action_type": "FUT_POSITION",
                "symbol": "GC",
                "contract": "202612",
                "side": "SELL",
                "order_type": "LMT",
                "limit_price": 2800.5,
                "quantity": 1,
                "allow_overnight": True,
            },
            market="COMEX_FUTURES",
        ),
    )

    assert str(getattr(fake_ib.last_contract, "lastTradeDateOrContractMonth", "")) == "202612"
    assert str(getattr(fake_ib.last_order, "orderType", "")) == "LMT"
    assert float(getattr(fake_ib.last_order, "lmtPrice", 0.0)) == 2800.5
    assert bool(getattr(fake_ib.last_order, "outsideRth", False)) is True
    assert result.order_type == "LMT"
    assert result.side == "SELL"


def test_submit_trade_action_waits_for_trade_done_when_supported(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)

    class _DoneAwareIB(_FakeIB):
        def __init__(self) -> None:
            super().__init__()
            self.wait_on_update_calls = 0
            self._done_state = {"done": False}
            self._last_trade: _FakeTrade | None = None

        def placeOrder(self, contract, order):  # type: ignore[no-untyped-def]
            trade = super().placeOrder(contract, order)
            self._last_trade = trade

            def _is_done() -> bool:
                return bool(self._done_state["done"])

            setattr(trade, "isDone", _is_done)
            return trade

        def waitOnUpdate(self, timeout=0):  # type: ignore[no-untyped-def]
            _ = timeout
            self.wait_on_update_calls += 1
            if self.wait_on_update_calls >= 2 and self._last_trade is not None:
                self._done_state["done"] = True
                self._last_trade.orderStatus.status = "Filled"
                self._last_trade.orderStatus.filled = float(
                    getattr(self._last_trade.order, "totalQuantity", 0.0) or 0.0
                )
                self._last_trade.orderStatus.remaining = 0.0
                self._last_trade.orderStatus.avgFillPrice = 100.0
            return True

    fake_ib = _DoneAwareIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )

    svc = IBTradeService(
        ib=fake_ib,
        host="127.0.0.1",
        port=4002,
        client_id=97,
        timeout_seconds=1.0,
    )
    result = svc.submit_trade_action(
        trade_action=_with_context(
            {
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 3,
                "tif": "DAY",
                "allow_overnight": False,
            }
        ),
        order_ref="T-UNIT-DONE",
    )

    assert fake_ib.wait_on_update_calls >= 2
    assert result.normalized_status == "FILLED"
    assert result.terminal is True
    assert result.filled_qty == 3.0


def test_submit_trade_action_fails_when_perm_id_missing(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)

    class _MissingPermIdIB(_FakeIB):
        def placeOrder(self, contract, order):  # type: ignore[no-untyped-def]
            trade = super().placeOrder(contract, order)
            trade.order.permId = None
            setattr(trade.orderStatus, "permId", None)
            return trade

    fake_ib = _MissingPermIdIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )

    svc = IBTradeService(
        ib=fake_ib,
        host="127.0.0.1",
        port=4002,
        client_id=97,
        timeout_seconds=1.0,
    )
    with pytest.raises(IBTradeServiceError, match="permId is missing"):
        svc.submit_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "MKT",
                    "quantity": 1,
                    "tif": "DAY",
                    "allow_overnight": False,
                }
            ),
            order_ref="T-UNIT-NO-PERM",
        )


def test_submit_trade_action_limit_requires_price(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)

    with pytest.raises(ValueError, match="limit_price"):
        svc.submit_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "LMT",
                    "quantity": 1,
                }
            ),
        )


def test_submit_trade_action_rejects_order_type_not_in_allowed_list(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    _mock_trade_validation_config(
        monkeypatch,
        allowed_order_types=("LMT",),
        allow_outside_rth=True,
        buy_open_max_amount_usd=1000.0,
        allow_live_orders=False,
    )
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(ValueError, match="allowed_order_types"):
        svc.submit_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "MKT",
                    "quantity": 1,
                }
            ),
        )


def test_submit_trade_action_rejects_outside_rth_when_disabled(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    _mock_trade_validation_config(
        monkeypatch,
        allowed_order_types=("MKT", "LMT"),
        allow_outside_rth=False,
        buy_open_max_amount_usd=1000.0,
        allow_live_orders=False,
    )
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(ValueError, match="outside RTH orders are disabled"):
        svc.submit_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "LMT",
                    "limit_price": 100,
                    "quantity": 1,
                    "allow_overnight": True,
                }
            ),
        )


def test_submit_trade_action_rejects_buy_amount_exceeds_limit(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    _mock_trade_validation_config(
        monkeypatch,
        allowed_order_types=("MKT", "LMT"),
        allow_outside_rth=True,
        buy_open_max_amount_usd=1000.0,
        allow_live_orders=False,
    )
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(ValueError, match="exceeds configured max"):
        svc.submit_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "LMT",
                    "limit_price": 600,
                    "quantity": 2,
                }
            ),
        )


def test_submit_trade_action_rejects_live_order_when_disabled(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    _mock_trade_validation_config(
        monkeypatch,
        allowed_order_types=("MKT", "LMT"),
        allow_outside_rth=True,
        buy_open_max_amount_usd=1000.0,
        allow_live_orders=False,
        trading_mode="live",
    )
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(
        ib=fake_ib,
        host="127.0.0.1",
        port=4001,
        trading_mode="live",
        client_id=97,
        timeout_seconds=5.0,
    )
    with pytest.raises(ValueError, match="live trading orders are disabled"):
        svc.submit_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "SELL",
                    "order_type": "LMT",
                    "limit_price": 100,
                    "quantity": 1,
                }
            ),
        )


def test_validate_trade_action_not_blocked_by_live_switch(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    _mock_trade_validation_config(
        monkeypatch,
        allowed_order_types=("MKT", "LMT"),
        allow_outside_rth=True,
        buy_open_max_amount_usd=1000.0,
        allow_live_orders=False,
        trading_mode="live",
    )
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(
        ib=fake_ib,
        host="127.0.0.1",
        port=4001,
        trading_mode="live",
        client_id=97,
        timeout_seconds=5.0,
    )
    result = svc.validate_trade_action(
        trade_action=_with_context(
            {
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 1,
            }
        )
    )
    assert result.market == "US_STOCK"
    assert result.symbol == "AAPL"


def test_submit_trade_action_rejects_side_not_in_allowed_list(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    _mock_trade_validation_config(
        monkeypatch,
        allowed_sides=("BUY",),
        allowed_order_types=("MKT", "LMT"),
        allow_outside_rth=True,
        buy_open_max_amount_usd=1000.0,
        allow_live_orders=False,
    )
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(ValueError, match="allowed_sides"):
        svc.submit_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "SELL",
                    "order_type": "LMT",
                    "limit_price": 100,
                    "quantity": 1,
                }
            ),
        )


def test_submit_trade_action_rejects_buy_amount_exceeds_account_cash(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    _mock_trade_validation_config(
        monkeypatch,
        allowed_sides=("BUY", "SELL"),
        allowed_order_types=("MKT", "LMT"),
        allow_outside_rth=True,
        buy_open_max_amount_usd=100000.0,
        allow_live_orders=False,
    )
    fake_ib = _FakeIB()
    fake_ib._summary_items = [
        SimpleNamespace(account="DU123", tag="TotalCashValue", value="500", currency="USD"),
        SimpleNamespace(account="DU123", tag="AvailableFunds", value="500", currency="USD"),
    ]
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0, account_code="DU123")
    with pytest.raises(ValueError, match="exceeds account cash"):
        svc.submit_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "LMT",
                    "limit_price": 300,
                    "quantity": 2,
                }
            ),
        )


def test_submit_trade_action_rejects_sell_quantity_exceeds_position(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    _mock_trade_validation_config(
        monkeypatch,
        allowed_sides=("BUY", "SELL"),
        allowed_order_types=("MKT", "LMT"),
        allow_outside_rth=True,
        buy_open_max_amount_usd=100000.0,
        allow_live_orders=False,
    )
    fake_ib = _FakeIB()
    fake_ib._portfolio_items = [
        SimpleNamespace(
            account="DU123",
            contract=SimpleNamespace(conId=101, symbol="AAPL", localSymbol="AAPL", secType="STK"),
            position=3.0,
        )
    ]
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0, account_code="DU123")
    with pytest.raises(ValueError, match="exceeds available position"):
        svc.submit_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "SELL",
                    "order_type": "LMT",
                    "limit_price": 100,
                    "quantity": 5,
                }
            ),
        )


def test_validate_trade_action_requires_market(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(ValueError, match="trade_action.market is required"):
        svc.validate_trade_action(
            trade_action={
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 1,
            }
        )


def test_validate_trade_action_runs_dynamic_validation(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    result = svc.validate_trade_action(
        trade_action=_with_context(
            {
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 1,
            }
        )
    )
    assert result.market == "US_STOCK"
    assert result.symbol == "AAPL"
    assert result.side == "BUY"
    assert result.order_type == "MKT"
    assert result.quantity == 1
    assert result.account_code == "DU123"
    assert fake_ib.last_historical_data_kwargs["durationStr"] == "10 D"
    assert fake_ib.last_historical_data_kwargs["barSizeSetting"] == "1 day"
    assert fake_ib.last_historical_data_kwargs["whatToShow"] == "TRADES"
    assert fake_ib.last_historical_data_kwargs["useRTH"] is True


def test_validate_trade_action_rejects_when_daily_close_unavailable(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)

    class _NoDailyBarIB(_FakeIB):
        def reqHistoricalData(self, contract, **kwargs):  # type: ignore[no-untyped-def]
            _ = (contract, kwargs)
            return []

    fake_ib = _NoDailyBarIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(ValueError, match="failed to determine reference price for account cash check"):
        svc.validate_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "MKT",
                    "quantity": 1,
                }
            )
        )


def test_submit_prevalidated_does_not_repeat_qualify_and_dynamic_checks(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    context = svc.validate_trade_action(
        trade_action=_with_context(
            {
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 1,
            }
        )
    )
    assert fake_ib.qualify_calls == 1
    result = svc.submit_prevalidated(validated_context=context, order_ref="T-CTX-1")
    assert fake_ib.qualify_calls == 1
    assert result.order_id is not None
    assert str(getattr(fake_ib.last_order, "orderRef", "")) == "T-CTX-1"


def test_poll_order_status_maps_filled(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
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
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    snapshot = svc.poll_order_status(order_id=2101)
    assert snapshot is not None
    assert snapshot.normalized_status == "FILLED"
    assert snapshot.terminal is True
    assert snapshot.filled_qty == 2.0


def test_poll_order_status_returns_none_when_not_found(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    assert svc.poll_order_status(order_id=999999) is None


def test_poll_order_status_by_order_ref(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    trade = _FakeTrade(
        contract=SimpleNamespace(conId=901, symbol="AAPL", localSymbol="AAPL", secType="STK"),
        order=SimpleNamespace(orderId=2101, permId=8101, orderRef="T-ORDER-REF-1"),
        orderStatus=SimpleNamespace(status="Submitted", filled=0.0, remaining=2.0, avgFillPrice=0.0),
        log=[],
    )
    fake_ib._trades.append(trade)
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    snapshot = svc.poll_order_status_by_order_ref(order_ref="T-ORDER-REF-1")
    assert snapshot is not None
    assert snapshot.order_id == 2101
    assert snapshot.perm_id == 8101
    assert snapshot.normalized_status == "ORDER_SUBMITTED"


def test_cancel_order_by_perm_id(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    submit = svc.submit_trade_action(
        trade_action=_with_context(
            {
                "action_type": "STOCK_TRADE",
                "symbol": "AAPL",
                "side": "BUY",
                "order_type": "MKT",
                "quantity": 1,
            }
        ),
        order_ref="T-CANCEL-1",
    )
    assert submit.perm_id is not None
    snapshot = svc.cancel_order(perm_id=submit.perm_id)
    assert snapshot is not None
    assert snapshot.perm_id == submit.perm_id
    assert snapshot.normalized_status == "CANCELLED"
    assert snapshot.terminal is True
    assert len(fake_ib.cancel_calls) == 1


def test_cancel_order_returns_none_when_not_found(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    assert svc.cancel_order(perm_id=999999, wait_for_terminal=False) is None


def test_cancel_order_rejects_different_client_id(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    trade = _FakeTrade(
        contract=SimpleNamespace(conId=901, symbol="AAPL", localSymbol="AAPL", secType="STK"),
        order=SimpleNamespace(orderId=2102, permId=8102, clientId=0),
        orderStatus=SimpleNamespace(status="Submitted", filled=0.0, remaining=1.0, avgFillPrice=0.0),
        log=[],
    )
    fake_ib._trades.append(trade)
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(IBTradeServiceError, match="different clientId"):
        svc.cancel_order(perm_id=8102, wait_for_terminal=False)


def test_cancel_order_requires_identifier(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(ValueError, match="order_id or perm_id is required"):
        svc.cancel_order()


def test_submit_trade_action_rejects_when_gateway_readonly(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    _mock_trade_validation_config(monkeypatch, readonly=True)
    fake_ib = _FakeIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(ValueError, match="role_connections.order.readonly=true"):
        svc.submit_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "MKT",
                    "quantity": 1,
                }
            )
        )


def test_cancel_order_rejects_when_gateway_readonly(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    _mock_trade_validation_config(monkeypatch, readonly=True)
    fake_ib = _FakeIB()
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(ValueError, match="role_connections.order.readonly=true"):
        svc.cancel_order(perm_id=8101, wait_for_terminal=False)


def test_submit_trade_action_wraps_ib_errors(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)

    class _BrokenIB(_FakeIB):
        def placeOrder(self, contract, order):  # type: ignore[no-untyped-def]
            _ = (contract, order)
            raise RuntimeError("gateway rejected")

    fake_ib = _BrokenIB()
    monkeypatch.setattr(
        "app.ib_trade_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    with pytest.raises(IBTradeServiceError, match="gateway rejected"):
        svc.submit_trade_action(
            trade_action=_with_context(
                {
                    "action_type": "STOCK_TRADE",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "MKT",
                    "quantity": 1,
                }
            ),
        )


def test_list_active_orders_returns_non_terminal_only(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
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
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
    rows = svc.list_active_orders()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].order_id == 2101
    assert rows[0].normalized_status == "ORDER_SUBMITTED"


def test_list_active_orders_uses_req_all_open_orders(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)

    class _ReqAllOnlyIB(_FakeIB):
        def trades(self):  # type: ignore[no-untyped-def]
            return []

        def openTrades(self):  # type: ignore[no-untyped-def]
            return []

        def reqOpenOrders(self):  # type: ignore[no-untyped-def]
            return []

        def reqAllOpenOrders(self):  # type: ignore[no-untyped-def]
            return [
                _FakeTrade(
                    contract=SimpleNamespace(conId=1001, symbol="TSLA", localSymbol="TSLA", secType="STK"),
                    order=SimpleNamespace(
                        orderId=3101,
                        permId=9101,
                        action="BUY",
                        orderType="LMT",
                        totalQuantity=1.0,
                        lmtPrice=200.0,
                        account="DU999",
                    ),
                    orderStatus=SimpleNamespace(
                        status="Submitted",
                        filled=0.0,
                        remaining=1.0,
                        avgFillPrice=0.0,
                    ),
                    log=[],
                )
            ]

    svc = IBTradeService(ib=_ReqAllOnlyIB(), client_id=97, timeout_seconds=5.0)
    rows = svc.list_active_orders()
    assert len(rows) == 1
    assert rows[0].symbol == "TSLA"
    assert rows[0].order_id == 3101
    assert rows[0].account_code == "DU999"


def test_wait_for_terminal_status_returns_terminal_snapshot(monkeypatch) -> None:
    _install_fake_ib_async(monkeypatch)
    fake_ib = _FakeIB()
    svc = IBTradeService(ib=fake_ib, client_id=97, timeout_seconds=5.0)
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
