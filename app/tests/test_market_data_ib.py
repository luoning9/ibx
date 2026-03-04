from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.market_config import MarketProfile
from app.ib_market_data import IBSessionHistoricalFetcher, _duration_str


UTC = timezone.utc


def _stock_profile() -> MarketProfile:
    return MarketProfile(
        market="US_STOCK",
        sec_type="STK",
        exchange="SMART",
        currency="USD",
        allowed_trade_types=frozenset({"buy", "sell"}),
    )


class _FakeStock:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeFuture:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeContract:
    def __init__(self, **kwargs: Any) -> None:
        self.conId = int(kwargs.get("conId", 0))


class _FakeIB:
    def __init__(self) -> None:
        self.qualify_calls = 0
        self.history_calls = 0

    def qualifyContracts(self, candidate: Any) -> list[Any]:  # noqa: N802
        self.qualify_calls += 1
        return [SimpleNamespace(conId=265598, candidate=candidate)]

    def reqHistoricalData(self, contract: Any, **kwargs: Any) -> list[Any]:  # noqa: N802
        _ = contract
        _ = kwargs
        self.history_calls += 1
        return [
            SimpleNamespace(
                date="20260222 10:01:00",
                open=10.0,
                high=11.0,
                low=9.5,
                close=10.5,
                volume=1200,
                average=10.3,
                barCount=15,
            )
        ]

    def reqHistoricalSchedule(self, contract: Any, **kwargs: Any):  # noqa: N802
        _ = contract
        _ = kwargs
        return SimpleNamespace(timeZone="US/Eastern", sessions=[])

    def reqContractDetails(self, contract: Any) -> list[Any]:  # noqa: N802
        return [SimpleNamespace(contract=contract)]


class _FakeSession:
    def __init__(self, ib: _FakeIB) -> None:
        self._ib = ib
        self.run_calls = 0

    def run(self, callback):  # type: ignore[no-untyped-def]
        self.run_calls += 1
        return callback(self._ib)


class _FakeSessionManager:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.get_session_calls: list[dict[str, Any]] = []

    def get_session(self, **kwargs: Any) -> _FakeSession:  # type: ignore[override]
        self.get_session_calls.append(dict(kwargs))
        return self._session


def test_ib_session_historical_fetcher_uses_session_manager(monkeypatch) -> None:
    fake_ib = _FakeIB()
    fake_session = _FakeSession(fake_ib)
    fake_manager = _FakeSessionManager(fake_session)

    monkeypatch.setattr(
        "app.ib_market_data.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    monkeypatch.setattr(
        "app.ib_market_data._load_ib_contract_types",
        lambda: (_FakeStock, _FakeFuture),
    )

    fetcher = IBSessionHistoricalFetcher(
        session_manager=fake_manager,
    )

    params = dict(
        contract={"market": "US_STOCK", "code": "AAPL"},
        start_time=datetime(2026, 2, 22, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 2, 22, 10, 2, tzinfo=UTC),
        bar_size="1 min",
        what_to_show="TRADES",
        use_rth=True,
    )
    first = fetcher.fetch(**params)
    second = fetcher.fetch(**params)

    assert len(fake_manager.get_session_calls) == 2
    assert fake_manager.get_session_calls[0] == {"role": "market_data"}
    assert fake_manager.get_session_calls[1] == {"role": "market_data"}
    assert fake_session.run_calls == 2
    assert fake_ib.history_calls == 2
    assert fake_ib.qualify_calls == 1
    assert first[0].close == 10.5
    assert second[0].count == 15


def test_duration_str_minimum_matches_bar_size() -> None:
    start = datetime(2026, 2, 24, 14, 50, 47, tzinfo=UTC)
    end = datetime(2026, 2, 24, 14, 51, 7, tzinfo=UTC)

    assert _duration_str(start, end, bar_delta=timedelta(minutes=1)) == "120 S"
    assert _duration_str(start, end, bar_delta=timedelta(minutes=5)) == "600 S"


class _FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, handler: Any):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler: Any):
        self.handlers = [item for item in self.handlers if item is not handler]
        return self

    def emit(self, *args: Any) -> None:
        for handler in list(self.handlers):
            handler(*args)


class _FakeBarList(list):
    def __init__(self, values: list[Any], req_id: int) -> None:
        super().__init__(values)
        self.reqId = req_id


class _FakeIBWithError(_FakeIB):
    def __init__(self) -> None:
        super().__init__()
        self.errorEvent = _FakeEvent()

    def reqHistoricalData(self, contract: Any, **kwargs: Any) -> list[Any]:  # noqa: N802
        _ = kwargs
        self.history_calls += 1
        self.errorEvent.emit(42, 162, "Historical Market Data Service error", contract)
        return _FakeBarList(
            [
                SimpleNamespace(
                    date="20260222 10:01:00",
                    open=10.0,
                    high=11.0,
                    low=9.5,
                    close=10.5,
                    volume=1200,
                    average=10.3,
                    barCount=15,
                )
            ],
            req_id=42,
        )


def test_ib_session_historical_fetcher_returns_ib_error_meta(monkeypatch) -> None:
    fake_ib = _FakeIBWithError()
    fake_session = _FakeSession(fake_ib)
    fake_manager = _FakeSessionManager(fake_session)

    monkeypatch.setattr(
        "app.ib_market_data.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    monkeypatch.setattr(
        "app.ib_market_data._load_ib_contract_types",
        lambda: (_FakeStock, _FakeFuture),
    )

    fetcher = IBSessionHistoricalFetcher(
        session_manager=fake_manager,
    )
    result = fetcher.fetch(
        contract={"market": "US_STOCK", "code": "AAPL"},
        start_time=datetime(2026, 2, 22, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 2, 22, 10, 2, tzinfo=UTC),
        bar_size="1 min",
        what_to_show="TRADES",
        use_rth=True,
    )

    assert isinstance(result, tuple)
    bars, meta = result
    assert len(bars) == 1
    assert meta["ib_error_count"] == 1
    assert meta["ib_error_codes"] == [162]
    assert meta["ib_req_id"] == 42


def test_ib_session_historical_fetcher_use_role_overrides_session_role(monkeypatch) -> None:
    fake_ib = _FakeIB()
    fake_session = _FakeSession(fake_ib)
    fake_manager = _FakeSessionManager(fake_session)

    monkeypatch.setattr(
        "app.ib_market_data.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    monkeypatch.setattr(
        "app.ib_market_data._load_ib_contract_types",
        lambda: (_FakeStock, _FakeFuture),
    )

    fetcher = IBSessionHistoricalFetcher(
        use_role="cli",
        session_manager=fake_manager,
    )
    _ = fetcher.fetch(
        contract={"market": "US_STOCK", "code": "AAPL"},
        start_time=datetime(2026, 2, 22, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 2, 22, 10, 2, tzinfo=UTC),
        bar_size="1 min",
        what_to_show="TRADES",
        use_rth=True,
    )

    assert len(fake_manager.get_session_calls) == 1
    assert fake_manager.get_session_calls[0] == {"role": "cli"}


class _FakeIBWithSchedule(_FakeIB):
    def __init__(self) -> None:
        super().__init__()
        self.schedule_calls: list[dict[str, Any]] = []

    def reqHistoricalSchedule(self, contract: Any, **kwargs: Any):  # noqa: N802
        _ = contract
        self.schedule_calls.append(dict(kwargs))
        return SimpleNamespace(
            timeZone="US/Eastern",
            sessions=[
                SimpleNamespace(
                    refDate="20260226",
                    startDateTime="20260226-09:30:00",
                    endDateTime="20260226-16:00:00",
                ),
                SimpleNamespace(
                    refDate="20260227",
                    startDateTime="20260227-09:30:00",
                    endDateTime="20260227-16:00:00",
                ),
            ],
        )


def test_ib_session_historical_fetcher_fetches_two_day_calendar(monkeypatch) -> None:
    fake_ib = _FakeIBWithSchedule()
    fake_session = _FakeSession(fake_ib)
    fake_manager = _FakeSessionManager(fake_session)

    monkeypatch.setattr(
        "app.ib_market_data.resolve_market_profile",
        lambda market, trade_type: _stock_profile(),
    )
    monkeypatch.setattr(
        "app.ib_market_data._load_ib_contract_types",
        lambda: (_FakeStock, _FakeFuture),
    )
    def _fake_require_ib_attr(name: str):  # type: ignore[no-untyped-def]
        if name == "Contract":
            return _FakeContract
        raise AttributeError(name)

    monkeypatch.setattr(
        "app.ib_market_data.require_ib_attr",
        _fake_require_ib_attr,
    )

    fetcher = IBSessionHistoricalFetcher(
        session_manager=fake_manager,
    )
    result = fetcher.fetch_trading_calendar(
        contract_id=265598,
        as_of_time=datetime(2026, 2, 26, 12, 0, tzinfo=UTC),
        use_rth=True,
    )

    assert len(fake_ib.schedule_calls) == 1
    assert fake_ib.schedule_calls[0]["numDays"] == 2
    assert fake_ib.schedule_calls[0]["useRTH"] is True
    assert len(result.sessions) == 2
    assert result.sessions[0].ref_date == "20260226"
    assert result.sessions[0].start_time == datetime(2026, 2, 26, 14, 30, tzinfo=UTC)
    assert result.sessions[1].ref_date == "20260227"
    assert result.meta["source"] == "IB"
    assert result.meta["contract_id"] == 265598
    assert result.meta["local_day"] == "2026-02-26"
    assert result.meta["next_local_day"] == "2026-02-27"
