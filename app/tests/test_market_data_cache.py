from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.market_data import HistoricalBar, HistoricalBarsRequest, SQLiteMarketDataCache


UTC = timezone.utc


@dataclass
class FetchCall:
    start: datetime
    end: datetime
    bar_size: str
    what_to_show: str
    use_rth: bool


class FakeFetcher:
    def __init__(self) -> None:
        self.calls: list[FetchCall] = []

    def fetch(
        self,
        *,
        contract: dict[str, Any] | str,
        start_time: datetime,
        end_time: datetime,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
    ) -> list[HistoricalBar]:
        self.calls.append(
            FetchCall(
                start=start_time,
                end=end_time,
                bar_size=bar_size,
                what_to_show=what_to_show,
                use_rth=use_rth,
            )
        )
        step = timedelta(minutes=1)
        out: list[HistoricalBar] = []
        cursor = start_time
        while cursor < end_time:
            n = int(cursor.timestamp() // 60)
            out.append(
                HistoricalBar(
                    ts=cursor,
                    open=float(n),
                    high=float(n) + 1,
                    low=float(n) - 1,
                    close=float(n) + 0.5,
                    volume=100.0,
                    wap=float(n) + 0.2,
                    count=10,
                )
            )
            cursor += step
        return out


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _request(
    start: str,
    end: str,
    *,
    include_partial_bar: bool = False,
    max_bars: int | None = None,
    page_size: int | None = 500,
) -> HistoricalBarsRequest:
    return HistoricalBarsRequest(
        contract={"conId": 12345, "secType": "STK"},
        start_time=_dt(start),
        end_time=_dt(end),
        bar_size="1 min",
        what_to_show="TRADES",
        use_rth=True,
        include_partial_bar=include_partial_bar,
        max_bars=max_bars,
        page_size=page_size,
    )


def test_cache_only_fetches_missing_segments(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    cache = SQLiteMarketDataCache(fetcher=fetcher, db_path=tmp_path / "market_cache.sqlite3")

    first = cache.get_historical_bars(_request("2026-02-22T10:00:00Z", "2026-02-22T10:10:00Z"))
    assert len(first.bars) == 10
    assert len(fetcher.calls) == 1
    assert fetcher.calls[0].start == _dt("2026-02-22T10:00:00Z")
    assert fetcher.calls[0].end == _dt("2026-02-22T10:10:00Z")

    second = cache.get_historical_bars(_request("2026-02-22T10:05:00Z", "2026-02-22T10:15:00Z"))
    assert len(second.bars) == 10
    assert len(fetcher.calls) == 2
    assert fetcher.calls[1].start == _dt("2026-02-22T10:10:00Z")
    assert fetcher.calls[1].end == _dt("2026-02-22T10:15:00Z")
    assert second.meta["has_gaps"] is True

    third = cache.get_historical_bars(_request("2026-02-22T10:05:00Z", "2026-02-22T10:15:00Z"))
    assert len(third.bars) == 10
    assert len(fetcher.calls) == 2
    assert third.meta["has_gaps"] is False
    assert third.meta["cache_hit_ratio"] == 1.0


def test_page_size_splits_fetch_chunks(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    cache = SQLiteMarketDataCache(fetcher=fetcher, db_path=tmp_path / "market_cache.sqlite3")

    result = cache.get_historical_bars(
        _request("2026-02-22T10:00:00Z", "2026-02-22T10:10:00Z", page_size=3)
    )
    assert len(result.bars) == 10
    assert len(fetcher.calls) == 4
    assert fetcher.calls[0].start == _dt("2026-02-22T10:00:00Z")
    assert fetcher.calls[0].end == _dt("2026-02-22T10:03:00Z")
    assert fetcher.calls[-1].start == _dt("2026-02-22T10:09:00Z")
    assert fetcher.calls[-1].end == _dt("2026-02-22T10:10:00Z")


def test_include_partial_bar_false_filters_incomplete_last_bar(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    now = _dt("2026-02-22T10:12:30Z")
    cache = SQLiteMarketDataCache(
        fetcher=fetcher,
        db_path=tmp_path / "market_cache.sqlite3",
        now_fn=lambda: now,
    )

    hidden = cache.get_historical_bars(
        _request(
            "2026-02-22T10:10:00Z",
            "2026-02-22T10:13:00Z",
            include_partial_bar=False,
        )
    )
    assert len(hidden.bars) == 2
    assert hidden.bars[-1].ts == _dt("2026-02-22T10:11:00Z")

    shown = cache.get_historical_bars(
        _request(
            "2026-02-22T10:10:00Z",
            "2026-02-22T10:13:00Z",
            include_partial_bar=True,
        )
    )
    assert len(shown.bars) == 3
    assert shown.bars[-1].ts == _dt("2026-02-22T10:12:00Z")


def test_max_bars_returns_latest_slice(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    cache = SQLiteMarketDataCache(fetcher=fetcher, db_path=tmp_path / "market_cache.sqlite3")

    result = cache.get_historical_bars(
        _request("2026-02-22T10:00:00Z", "2026-02-22T10:10:00Z", max_bars=3)
    )
    assert len(result.bars) == 3
    assert result.meta["truncated"] is True
    assert [bar.ts for bar in result.bars] == [
        _dt("2026-02-22T10:07:00Z"),
        _dt("2026-02-22T10:08:00Z"),
        _dt("2026-02-22T10:09:00Z"),
    ]
