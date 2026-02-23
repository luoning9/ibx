from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

from app.config import clear_app_config_cache
from app.ib_data_service import (
    IBDataService,
    IBDataServiceError,
    FixtureBrokerDataProvider,
    build_broker_data_provider_from_config,
)
from app.market_config import MarketProfile


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


class _FakeIB:
    def __init__(self) -> None:
        self.connected = False
        self.connect_calls: list[dict[str, object]] = []
        self.qualify_calls = 0
        self.detail_calls = 0
        self.qualify_result: list[object] = []
        self.detail_result: list[object] = []
        self.summary_result: list[object] = []
        self.portfolio_result: list[object] = []

    def connect(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.connected = True
        self.connect_calls.append(kwargs)

    def disconnect(self) -> None:
        self.connected = False

    def isConnected(self) -> bool:
        return self.connected

    def qualifyContracts(self, candidate):  # type: ignore[no-untyped-def]
        _ = candidate
        self.qualify_calls += 1
        return list(self.qualify_result)

    def reqContractDetails(self, probe):  # type: ignore[no-untyped-def]
        _ = probe
        self.detail_calls += 1
        return list(self.detail_result)

    def accountSummary(self):  # type: ignore[no-untyped-def]
        return list(self.summary_result)

    def portfolio(self):  # type: ignore[no-untyped-def]
        return list(self.portfolio_result)


def test_resolve_contract_id_stock(monkeypatch) -> None:
    fake_ib = _FakeIB()
    fake_ib.qualify_result = [SimpleNamespace(conId=265598)]

    monkeypatch.setattr(
        "app.ib_data_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )

    built: list[dict[str, str | None]] = []

    def builder(*, sec_type: str, code: str, exchange: str, currency: str, contract_month: str | None):
        built.append(
            {
                "sec_type": sec_type,
                "code": code,
                "exchange": exchange,
                "currency": currency,
                "contract_month": contract_month,
            }
        )
        return SimpleNamespace()

    svc = IBDataService(
        ib=fake_ib,
        host="127.0.0.1",
        port=4002,
        client_id=99,
        timeout_seconds=5.0,
        contract_builder=builder,
    )

    con_id = svc.resolve_contract_id(code="AAPL", market="US_STOCK")
    assert con_id == 265598
    assert len(fake_ib.connect_calls) == 1
    assert fake_ib.qualify_calls == 1
    assert built == [
        {
            "sec_type": "STK",
            "code": "AAPL",
            "exchange": "SMART",
            "currency": "USD",
            "contract_month": None,
        }
    ]


def test_resolve_contract_id_future_prefers_front_detail(monkeypatch) -> None:
    fake_ib = _FakeIB()
    fake_ib.detail_result = [
        SimpleNamespace(contract=SimpleNamespace(conId=302, lastTradeDateOrContractMonth="209902")),
        SimpleNamespace(contract=SimpleNamespace(conId=301, lastTradeDateOrContractMonth="209901")),
    ]

    monkeypatch.setattr(
        "app.ib_data_service.resolve_market_profile",
        lambda market, trade_type: _future_profile(),
    )

    svc = IBDataService(
        ib=fake_ib,
        host="127.0.0.1",
        port=4002,
        client_id=99,
        timeout_seconds=5.0,
        contract_builder=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    con_id = svc.resolve_contract_id(code="GC", market="COMEX_FUTURES")
    assert con_id == 301
    assert fake_ib.detail_calls == 1
    assert fake_ib.qualify_calls == 0


def test_get_account_snapshot_filters_account_and_parses_values(monkeypatch) -> None:
    fake_ib = _FakeIB()
    fake_ib.summary_result = [
        SimpleNamespace(account="U111", tag="NetLiquidation", value="12345.67", currency="USD"),
        SimpleNamespace(account="U111", tag="AvailableFunds", value="1000", currency="USD"),
        SimpleNamespace(account="U111", tag="NonNumeric", value="N/A", currency="USD"),
        SimpleNamespace(account="U222", tag="NetLiquidation", value="9", currency="USD"),
    ]
    fake_ib.portfolio_result = [
        SimpleNamespace(
            account="U111",
            contract=SimpleNamespace(
                conId=101,
                symbol="AAPL",
                localSymbol="AAPL",
                secType="STK",
                currency="USD",
                exchange="SMART",
            ),
            position=10,
            marketPrice=180.2,
            marketValue=1802,
            averageCost=170.0,
            unrealizedPNL=102.0,
            realizedPNL=15.0,
        ),
        SimpleNamespace(
            account="U222",
            contract=SimpleNamespace(
                conId=202,
                symbol="MSFT",
                localSymbol="MSFT",
                secType="STK",
                currency="USD",
                exchange="SMART",
            ),
            position=1,
            marketPrice=1,
            marketValue=1,
            averageCost=1,
            unrealizedPNL=0,
            realizedPNL=0,
        ),
    ]

    monkeypatch.setattr(
        "app.ib_data_service.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    svc = IBDataService(
        ib=fake_ib,
        host="127.0.0.1",
        port=4002,
        client_id=99,
        timeout_seconds=5.0,
    )

    snapshot = svc.get_account_snapshot(account_code="U111")
    assert snapshot.account_code == "U111"
    assert snapshot.values["NetLiquidation"] == "12345.67"
    assert snapshot.values_float["NetLiquidation"] == 12345.67
    assert snapshot.values_float["AvailableFunds"] == 1000.0
    assert "NonNumeric" not in snapshot.values_float
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].contract_id == 101
    assert snapshot.positions[0].symbol == "AAPL"


def test_connect_initializes_event_loop_when_missing(monkeypatch) -> None:
    fake_ib = _FakeIB()
    svc = IBDataService(
        ib=fake_ib,
        host="127.0.0.1",
        port=4002,
        client_id=99,
        timeout_seconds=5.0,
    )

    recorded = {"set_called": False}

    def fake_get_event_loop() -> object:
        raise RuntimeError("no event loop")

    def fake_new_event_loop() -> object:
        return object()

    def fake_set_event_loop(loop: object) -> None:
        recorded["set_called"] = loop is not None

    monkeypatch.setattr(asyncio, "get_event_loop", fake_get_event_loop)
    monkeypatch.setattr(asyncio, "new_event_loop", fake_new_event_loop)
    monkeypatch.setattr(asyncio, "set_event_loop", fake_set_event_loop)

    svc.connect()
    assert recorded["set_called"] is True
    assert len(fake_ib.connect_calls) == 1


def test_connect_fails_when_client_id_conflicted() -> None:
    class _ConflictedIB(_FakeIB):
        def connect(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            self.connect_calls.append(kwargs)
            raise RuntimeError("client id is already in use")

    fake_ib = _ConflictedIB()
    svc = IBDataService(
        ib=fake_ib,
        host="127.0.0.1",
        port=4002,
        client_id=99,
        timeout_seconds=5.0,
    )
    try:
        svc.connect()
        raise AssertionError("expected IBDataServiceError")
    except IBDataServiceError as exc:
        message = str(exc)
        assert "client_id=99" in message
        assert "already in use" in message

    assert len(fake_ib.connect_calls) == 1
    assert int(fake_ib.connect_calls[0]["clientId"]) == 99
    assert svc.client_id == 99


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content.strip() + "\n", encoding="utf-8")


def test_fixture_provider_uses_default_snapshot_file() -> None:
    provider = FixtureBrokerDataProvider()
    con_id = provider.resolve_contract_id(code="VGT", market="US_STOCK")
    snapshot = provider.get_account_snapshot()
    assert con_id == 910001
    assert snapshot.account_code == "U13883817"
    assert snapshot.values["NetLiquidation"] == "143445.18"
    assert len(snapshot.positions) == 9
    assert snapshot.positions[0].contract_id == 910001


def test_fixture_provider_resolve_contract_id_covers_snapshot_symbols() -> None:
    provider = FixtureBrokerDataProvider()
    symbols = ("VGT", "IAU", "SLV", "IEI", "MGK", "USAR", "CRML", "LTBR", "IPX")
    resolved = {code: provider.resolve_contract_id(code=code, market="US_STOCK") for code in symbols}
    assert all(resolved[code] > 0 for code in symbols)
    assert len(set(resolved.values())) == len(symbols)


def test_build_broker_data_provider_from_config_selects_fixture(tmp_path: Path) -> None:
    conf_path = tmp_path / "app.toml"
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        (
            "{"
            "\"contracts\":[{\"market\":\"US_STOCK\",\"code\":\"TSLA\",\"contract_id\":76792991}],"
            "\"account_snapshot\":{"
            "\"fetched_at\":\"2026-02-01T00:00:00Z\","
            "\"values\":{\"NetLiquidation\":\"1\"},"
            "\"positions\":[]"
            "}"
            "}"
        ),
        encoding="utf-8",
    )
    _write_toml(
        conf_path,
        """
        [providers]
        broker_data = "fixture"
        """,
    )

    old_path = os.getenv("IBX_APP_CONFIG")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    clear_app_config_cache()
    try:
        provider = build_broker_data_provider_from_config(fixture_path=fixture_path)
        assert isinstance(provider, FixtureBrokerDataProvider)
        con_id = provider.resolve_contract_id(code="TSLA", market="US_STOCK")
        assert con_id == 76792991
    finally:
        if old_path is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_path
        clear_app_config_cache()
