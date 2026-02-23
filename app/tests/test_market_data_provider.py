from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import clear_app_config_cache
from app.market_data import (
    FixtureMarketDataProvider,
    HistoricalBarsRequest,
    SQLiteMarketDataCache,
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
