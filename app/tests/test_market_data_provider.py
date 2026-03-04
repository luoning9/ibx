from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import clear_app_config_cache
from app.market_data import (
    DirectIBMarketDataProvider,
    FixtureMarketDataProvider,
    HistoricalBarsRequest,
    SQLiteMarketDataCache,
    TradingCalendarRequest,
    TradingCalendarResult,
    TradingCalendarSession,
    build_market_data_provider_from_config,
)


UTC = timezone.utc


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content.strip() + "\n", encoding="utf-8")


def test_fixture_market_data_provider_uses_default_sample() -> None:
    provider = FixtureMarketDataProvider()
    result = provider.get_historical_bars(
        HistoricalBarsRequest(
            contract="VGT",
            start_time=datetime(2026, 2, 22, 10, 0, tzinfo=UTC),
            end_time=datetime(2026, 2, 22, 10, 3, tzinfo=UTC),
            bar_size="1 min",
            what_to_show="TRADES",
            use_rth=True,
        )
    )
    assert len(result.bars) == 3
    assert result.meta["source"] == "FIXTURE"
    assert result.bars[0].open == 734.9


def test_build_market_data_provider_from_config_selects_fixture(tmp_path: Path) -> None:
    conf_path = tmp_path / "app.toml"
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        (
            "{"
            "\"series\":[{"
            "\"contract\":\"TSLA\","
            "\"bar_size\":\"1 min\","
            "\"what_to_show\":\"TRADES\","
            "\"use_rth\":true,"
            "\"bars\":[{\"ts\":\"2026-02-22T10:00:00Z\",\"open\":1,\"high\":1,\"low\":1,\"close\":1}]"
            "}]"
            "}"
        ),
        encoding="utf-8",
    )
    _write_toml(
        conf_path,
        """
        [providers]
        market_data = "fixture"
        """,
    )

    old_config_path = os.getenv("IBX_APP_CONFIG")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    clear_app_config_cache()
    try:
        provider = build_market_data_provider_from_config(fixture_path=fixture_path)
        assert isinstance(provider, FixtureMarketDataProvider)
        result = provider.get_historical_bars(
            HistoricalBarsRequest(
                contract="TSLA",
                start_time=datetime(2026, 2, 22, 10, 0, tzinfo=UTC),
                end_time=datetime(2026, 2, 22, 10, 1, tzinfo=UTC),
                bar_size="1 min",
            )
        )
        assert len(result.bars) == 1
    finally:
        if old_config_path is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_config_path
        clear_app_config_cache()


def test_build_market_data_provider_from_config_ib_requires_fetcher(tmp_path: Path) -> None:
    conf_path = tmp_path / "app.toml"
    _write_toml(
        conf_path,
        """
        [providers]
        market_data = "ib"
        """,
    )
    old_config_path = os.getenv("IBX_APP_CONFIG")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    clear_app_config_cache()
    try:
        with pytest.raises(ValueError, match="fetcher is required"):
            build_market_data_provider_from_config()
    finally:
        if old_config_path is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_config_path
        clear_app_config_cache()


def test_build_market_data_provider_from_config_ib_with_fetcher(tmp_path: Path) -> None:
    class _Fetcher:
        def fetch(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return []

    conf_path = tmp_path / "app.toml"
    _write_toml(
        conf_path,
        """
        [providers]
        market_data = "ib"
        """,
    )
    old_config_path = os.getenv("IBX_APP_CONFIG")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    clear_app_config_cache()
    try:
        provider = build_market_data_provider_from_config(
            fetcher=_Fetcher(),
            db_path=tmp_path / "market_cache.sqlite3",
            now_fn=lambda: datetime(2026, 2, 22, 10, 10, tzinfo=UTC),
        )
        assert isinstance(provider, SQLiteMarketDataCache)
        result = provider.get_historical_bars(
            HistoricalBarsRequest(
                contract="AAPL",
                start_time=datetime(2026, 2, 22, 10, 0, tzinfo=UTC),
                end_time=datetime(2026, 2, 22, 10, 2, tzinfo=UTC),
                bar_size="1 min",
            )
        )
        assert isinstance(result.bars, list)
        assert result.meta["delay_window_minutes"] == 20
    finally:
        if old_config_path is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_config_path
        clear_app_config_cache()


def test_build_market_data_provider_from_config_ib_with_custom_delay_window(tmp_path: Path) -> None:
    class _Fetcher:
        def fetch(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return []

    conf_path = tmp_path / "app.toml"
    _write_toml(
        conf_path,
        """
        [providers]
        market_data = "ib"
        market_data_delay_window_minutes = 7
        """,
    )
    old_config_path = os.getenv("IBX_APP_CONFIG")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    clear_app_config_cache()
    try:
        provider = build_market_data_provider_from_config(
            fetcher=_Fetcher(),
            db_path=tmp_path / "market_cache.sqlite3",
            now_fn=lambda: datetime(2026, 2, 22, 10, 10, tzinfo=UTC),
        )
        assert isinstance(provider, SQLiteMarketDataCache)
        result = provider.get_historical_bars(
            HistoricalBarsRequest(
                contract="AAPL",
                start_time=datetime(2026, 2, 22, 10, 0, tzinfo=UTC),
                end_time=datetime(2026, 2, 22, 10, 2, tzinfo=UTC),
                bar_size="1 min",
            )
        )
        assert result.meta["delay_window_minutes"] == 7
    finally:
        if old_config_path is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_config_path
        clear_app_config_cache()


def test_build_market_data_provider_from_config_ib_with_fetcher_cache_disabled(tmp_path: Path) -> None:
    class _Fetcher:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            self.calls += 1
            return []

    fetcher = _Fetcher()
    conf_path = tmp_path / "app.toml"
    _write_toml(
        conf_path,
        """
        [providers]
        market_data = "ib"
        market_data_disable_cache = true
        """,
    )
    old_config_path = os.getenv("IBX_APP_CONFIG")
    os.environ["IBX_APP_CONFIG"] = str(conf_path)
    clear_app_config_cache()
    try:
        provider = build_market_data_provider_from_config(
            fetcher=fetcher,
            db_path=tmp_path / "market_cache.sqlite3",
            now_fn=lambda: datetime(2026, 2, 22, 10, 10, tzinfo=UTC),
        )
        assert isinstance(provider, DirectIBMarketDataProvider)
        req = HistoricalBarsRequest(
            contract="AAPL",
            start_time=datetime(2026, 2, 22, 10, 0, tzinfo=UTC),
            end_time=datetime(2026, 2, 22, 10, 2, tzinfo=UTC),
            bar_size="1 min",
        )
        provider.get_historical_bars(req)
        provider.get_historical_bars(req)
        assert fetcher.calls == 2
    finally:
        if old_config_path is None:
            os.environ.pop("IBX_APP_CONFIG", None)
        else:
            os.environ["IBX_APP_CONFIG"] = old_config_path
        clear_app_config_cache()


def test_direct_provider_get_trading_calendar_delegates_to_fetcher() -> None:
    class _Fetcher:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return []

        def fetch_trading_calendar(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            return TradingCalendarResult(
                sessions=[
                    TradingCalendarSession(
                        ref_date="20260226",
                        start_time=datetime(2026, 2, 26, 14, 30, tzinfo=UTC),
                        end_time=datetime(2026, 2, 26, 21, 0, tzinfo=UTC),
                    )
                ],
                meta={"source": "TEST"},
            )

    fetcher = _Fetcher()
    provider = DirectIBMarketDataProvider(fetcher=fetcher)
    result = provider.get_trading_calendar(
        TradingCalendarRequest(
            contract_id=39039301,
            as_of_time=datetime(2026, 2, 26, 15, 0, tzinfo=UTC),
            use_rth=True,
        )
    )
    assert fetcher.calls == 1
    assert len(result.sessions) == 1
    assert result.meta["source"] == "TEST"


def test_sqlite_provider_get_trading_calendar_requires_fetcher_support(tmp_path: Path) -> None:
    class _Fetcher:
        def fetch(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return []

    provider = SQLiteMarketDataCache(fetcher=_Fetcher(), db_path=tmp_path / "market_cache.sqlite3")
    with pytest.raises(RuntimeError, match="does not support trading calendar"):
        provider.get_trading_calendar(
            TradingCalendarRequest(
                contract_id=39039301,
                as_of_time=datetime(2026, 2, 26, 15, 0, tzinfo=UTC),
                use_rth=True,
            )
        )


def test_sqlite_provider_get_trading_calendar_caches_by_same_day_key(tmp_path: Path) -> None:
    class _Fetcher:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return []

        def fetch_trading_calendar(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            self.calls += 1
            return TradingCalendarResult(
                sessions=[
                    TradingCalendarSession(
                        ref_date="20260226",
                        start_time=datetime(2026, 2, 26, 14, 30, tzinfo=UTC),
                        end_time=datetime(2026, 2, 26, 21, 0, tzinfo=UTC),
                    )
                ],
                meta={"source": "TEST"},
            )

    fetcher = _Fetcher()
    provider = SQLiteMarketDataCache(
        fetcher=fetcher,
        db_path=tmp_path / "market_cache.sqlite3",
        now_fn=lambda: datetime(2026, 2, 26, 15, 0, tzinfo=UTC),
    )
    first = provider.get_trading_calendar(
        TradingCalendarRequest(
            contract_id=39039301,
            as_of_time=datetime(2026, 2, 26, 10, 0, tzinfo=UTC),
            use_rth=True,
        )
    )
    second = provider.get_trading_calendar(
        TradingCalendarRequest(
            contract_id=39039301,
            as_of_time=datetime(2026, 2, 26, 19, 0, tzinfo=UTC),
            use_rth=True,
        )
    )

    assert fetcher.calls == 1
    assert first.meta["cache_hit"] is False
    assert second.meta["cache_hit"] is True
    assert len(second.sessions) == 1


def test_direct_provider_trading_calendar_defaults_as_of_to_now() -> None:
    fixed_now = datetime(2026, 2, 26, 15, 0, tzinfo=UTC)

    class _Fetcher:
        def __init__(self) -> None:
            self.received_as_of: datetime | None = None

        def fetch(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return []

        def fetch_trading_calendar(self, **kwargs):  # type: ignore[no-untyped-def]
            self.received_as_of = kwargs["as_of_time"]
            return TradingCalendarResult(sessions=[], meta={"source": "TEST"})

    fetcher = _Fetcher()
    provider = DirectIBMarketDataProvider(fetcher=fetcher, now_fn=lambda: fixed_now)
    provider.get_trading_calendar(
        TradingCalendarRequest(
            contract_id=39039301,
            use_rth=True,
        )
    )
    assert fetcher.received_as_of == fixed_now


def test_direct_provider_trading_calendar_rejects_as_of_later_than_one_week() -> None:
    fixed_now = datetime(2026, 2, 26, 15, 0, tzinfo=UTC)

    class _Fetcher:
        def fetch(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return []

        def fetch_trading_calendar(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return TradingCalendarResult(sessions=[], meta={"source": "TEST"})

    provider = DirectIBMarketDataProvider(fetcher=_Fetcher(), now_fn=lambda: fixed_now)
    with pytest.raises(ValueError, match="now \\+ 7 days"):
        provider.get_trading_calendar(
            TradingCalendarRequest(
                contract_id=39039301,
                as_of_time=fixed_now + timedelta(days=7, seconds=1),
                use_rth=True,
            )
        )


def test_direct_provider_trading_calendar_requires_positive_contract_id() -> None:
    class _Fetcher:
        def fetch(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return []

        def fetch_trading_calendar(self, **kwargs):  # type: ignore[no-untyped-def]
            _ = kwargs
            return TradingCalendarResult(sessions=[], meta={"source": "TEST"})

    provider = DirectIBMarketDataProvider(fetcher=_Fetcher())
    with pytest.raises(ValueError, match="contract_id must be positive"):
        provider.get_trading_calendar(
            TradingCalendarRequest(
                contract_id=0,
                as_of_time=datetime(2026, 2, 26, 15, 0, tzinfo=UTC),
                use_rth=True,
            )
        )
