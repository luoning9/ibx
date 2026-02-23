from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Mapping, Protocol

from .logging_config import configure_market_data_logging
from .runtime_paths import resolve_market_cache_db_path


UTC = timezone.utc


@dataclass(frozen=True)
class HistoricalBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    wap: float | None = None
    count: int | None = None


@dataclass(frozen=True)
class HistoricalBarsRequest:
    contract: Mapping[str, Any] | str
    start_time: datetime
    end_time: datetime
    bar_size: str
    what_to_show: str = "TRADES"
    use_rth: bool = True
    include_partial_bar: bool = False
    max_bars: int | None = None
    page_size: int | None = 500


@dataclass(frozen=True)
class HistoricalBarsResult:
    bars: list[HistoricalBar]
    meta: dict[str, Any]


class HistoricalBarsFetcher(Protocol):
    def fetch(
        self,
        *,
        contract: Mapping[str, Any] | str,
        start_time: datetime,
        end_time: datetime,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
    ) -> list[HistoricalBar | Mapping[str, Any]]:
        ...


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_iso_utc(dt: datetime) -> str:
    return _to_utc(dt).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _parse_bar_size(bar_size: str) -> timedelta | None:
    text = bar_size.strip().lower()
    if not text:
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    try:
        amount = int(parts[0])
    except ValueError:
        return None
    unit = parts[1]
    if unit in {"sec", "secs", "second", "seconds"}:
        return timedelta(seconds=amount)
    if unit in {"min", "mins", "minute", "minutes"}:
        return timedelta(minutes=amount)
    if unit in {"hour", "hours"}:
        return timedelta(hours=amount)
    if unit in {"day", "days"}:
        return timedelta(days=amount)
    return None


def _normalize_contract(contract: Mapping[str, Any] | str) -> str:
    if isinstance(contract, str):
        normalized = contract.strip()
        if not normalized:
            raise ValueError("contract cannot be empty")
        return normalized
    payload = {str(k): contract[k] for k in sorted(contract)}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cache_key(
    contract: Mapping[str, Any] | str,
    bar_size: str,
    what_to_show: str,
    use_rth: bool,
) -> str:
    return "|".join(
        [
            _normalize_contract(contract),
            bar_size.strip().lower(),
            what_to_show.strip().upper(),
            "1" if use_rth else "0",
        ]
    )


def _merge_segments(segments: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not segments:
        return []
    ordered = sorted(segments, key=lambda x: x[0])
    merged: list[tuple[datetime, datetime]] = []
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
            continue
        merged.append((cur_start, cur_end))
        cur_start, cur_end = start, end
    merged.append((cur_start, cur_end))
    return merged


def _missing_segments(
    start: datetime, end: datetime, coverage: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    if start >= end:
        return []
    gaps: list[tuple[datetime, datetime]] = []
    cursor = start
    for seg_start, seg_end in coverage:
        if seg_end <= cursor:
            continue
        if seg_start > cursor:
            gaps.append((cursor, min(seg_start, end)))
        cursor = max(cursor, seg_end)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return [(s, e) for s, e in gaps if s < e]


def _intersect_segments(
    start: datetime,
    end: datetime,
    coverage: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    hits: list[tuple[datetime, datetime]] = []
    for seg_start, seg_end in coverage:
        hit_start = max(start, seg_start)
        hit_end = min(end, seg_end)
        if hit_start < hit_end:
            hits.append((hit_start, hit_end))
    return hits


def _split_by_page_size(
    start: datetime,
    end: datetime,
    bar_delta: timedelta | None,
    page_size: int | None,
) -> list[tuple[datetime, datetime]]:
    if page_size is None or page_size <= 0 or bar_delta is None:
        return [(start, end)]
    if bar_delta.total_seconds() <= 0:
        return [(start, end)]

    out: list[tuple[datetime, datetime]] = []
    chunk = bar_delta * page_size
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + chunk)
        out.append((cursor, chunk_end))
        cursor = chunk_end
    return out


def _coerce_bar(raw: HistoricalBar | Mapping[str, Any]) -> HistoricalBar:
    if isinstance(raw, HistoricalBar):
        return HistoricalBar(
            ts=_to_utc(raw.ts),
            open=float(raw.open),
            high=float(raw.high),
            low=float(raw.low),
            close=float(raw.close),
            volume=None if raw.volume is None else float(raw.volume),
            wap=None if raw.wap is None else float(raw.wap),
            count=None if raw.count is None else int(raw.count),
        )
    data = dict(raw)
    ts_raw = data.get("ts", data.get("time", data.get("date")))
    if ts_raw is None:
        raise ValueError("bar missing ts/time/date")
    if isinstance(ts_raw, datetime):
        ts = _to_utc(ts_raw)
    else:
        ts = _parse_iso_utc(str(ts_raw))
    count_raw = data.get("count")
    return HistoricalBar(
        ts=ts,
        open=float(data["open"]),
        high=float(data["high"]),
        low=float(data["low"]),
        close=float(data["close"]),
        volume=None if data.get("volume") is None else float(data["volume"]),
        wap=None if data.get("wap") is None else float(data["wap"]),
        count=None if count_raw is None else int(count_raw),
    )


class SQLiteMarketDataCache:
    def __init__(
        self,
        *,
        fetcher: HistoricalBarsFetcher,
        db_path: str | Path | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        configure_market_data_logging()
        self._logger = logging.getLogger("ibx.market_data")
        self._fetcher = fetcher
        self._db_path = Path(db_path) if db_path is not None else resolve_market_cache_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._lock_guard = Lock()
        self._locks: dict[str, Lock] = {}
        self._init_db()
        self._logger.info("SQLiteMarketDataCache initialized db_path=%s", self._db_path.resolve())

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_bars (
                  cache_key TEXT NOT NULL,
                  ts TEXT NOT NULL,
                  open REAL NOT NULL,
                  high REAL NOT NULL,
                  low REAL NOT NULL,
                  close REAL NOT NULL,
                  volume REAL,
                  wap REAL,
                  count INTEGER,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (cache_key, ts)
                );

                CREATE TABLE IF NOT EXISTS market_coverage (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  cache_key TEXT NOT NULL,
                  start_ts TEXT NOT NULL,
                  end_ts TEXT NOT NULL,
                  CHECK (start_ts < end_ts)
                );

                CREATE INDEX IF NOT EXISTS idx_market_coverage_key_start
                  ON market_coverage (cache_key, start_ts, end_ts);
                """
            )
            conn.commit()

    def _key_lock(self, cache_key: str) -> Lock:
        with self._lock_guard:
            lock = self._locks.get(cache_key)
            if lock is None:
                lock = Lock()
                self._locks[cache_key] = lock
            return lock

    def _load_coverage(
        self, conn: sqlite3.Connection, cache_key: str
    ) -> list[tuple[datetime, datetime]]:
        rows = conn.execute(
            """
            SELECT start_ts, end_ts
            FROM market_coverage
            WHERE cache_key = ?
            ORDER BY start_ts ASC
            """,
            (cache_key,),
        ).fetchall()
        return [(_parse_iso_utc(r["start_ts"]), _parse_iso_utc(r["end_ts"])) for r in rows]

    def _replace_coverage(
        self, conn: sqlite3.Connection, cache_key: str, segments: list[tuple[datetime, datetime]]
    ) -> None:
        merged = _merge_segments(segments)
        conn.execute("DELETE FROM market_coverage WHERE cache_key = ?", (cache_key,))
        for start, end in merged:
            conn.execute(
                """
                INSERT INTO market_coverage (cache_key, start_ts, end_ts)
                VALUES (?, ?, ?)
                """,
                (cache_key, _to_iso_utc(start), _to_iso_utc(end)),
            )

    def _store_bars(
        self,
        conn: sqlite3.Connection,
        cache_key: str,
        bars: list[HistoricalBar],
        seg_start: datetime,
        seg_end: datetime,
    ) -> None:
        now_iso = _to_iso_utc(self._now_fn())
        for bar in bars:
            if not (seg_start <= bar.ts < seg_end):
                continue
            conn.execute(
                """
                INSERT INTO market_bars (
                  cache_key, ts, open, high, low, close, volume, wap, count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key, ts) DO UPDATE SET
                  open = excluded.open,
                  high = excluded.high,
                  low = excluded.low,
                  close = excluded.close,
                  volume = excluded.volume,
                  wap = excluded.wap,
                  count = excluded.count,
                  updated_at = excluded.updated_at
                """,
                (
                    cache_key,
                    _to_iso_utc(bar.ts),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.wap,
                    bar.count,
                    now_iso,
                ),
            )

    def _read_bars(
        self,
        conn: sqlite3.Connection,
        cache_key: str,
        start: datetime,
        end: datetime,
    ) -> list[HistoricalBar]:
        rows = conn.execute(
            """
            SELECT ts, open, high, low, close, volume, wap, count
            FROM market_bars
            WHERE cache_key = ? AND ts >= ? AND ts < ?
            ORDER BY ts ASC
            """,
            (cache_key, _to_iso_utc(start), _to_iso_utc(end)),
        ).fetchall()
        return [
            HistoricalBar(
                ts=_parse_iso_utc(r["ts"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=None if r["volume"] is None else float(r["volume"]),
                wap=None if r["wap"] is None else float(r["wap"]),
                count=None if r["count"] is None else int(r["count"]),
            )
            for r in rows
        ]

    def get_historical_bars(self, request: HistoricalBarsRequest) -> HistoricalBarsResult:
        start = _to_utc(request.start_time)
        end = _to_utc(request.end_time)
        if start >= end:
            raise ValueError("start_time must be earlier than end_time")
        if request.max_bars is not None and request.max_bars <= 0:
            raise ValueError("max_bars must be positive")
        if request.page_size is not None and request.page_size <= 0:
            raise ValueError("page_size must be positive")
        bar_size = request.bar_size.strip()
        if not bar_size:
            raise ValueError("bar_size cannot be empty")

        cache_key = _cache_key(
            request.contract,
            bar_size=bar_size,
            what_to_show=request.what_to_show,
            use_rth=request.use_rth,
        )
        self._logger.info(
            "historical_bars request cache_key=%s start=%s end=%s bar_size=%s what_to_show=%s use_rth=%s include_partial_bar=%s max_bars=%s page_size=%s",
            cache_key,
            _to_iso_utc(start),
            _to_iso_utc(end),
            bar_size,
            request.what_to_show,
            request.use_rth,
            request.include_partial_bar,
            request.max_bars,
            request.page_size,
        )
        key_lock = self._key_lock(cache_key)
        bar_delta = _parse_bar_size(bar_size)

        try:
            with key_lock, self._conn() as conn:
                coverage = self._load_coverage(conn, cache_key)
                missing = _missing_segments(start, end, coverage)
                self._logger.info(
                    "historical_bars cache_key=%s coverage_segments=%d missing_segments=%d",
                    cache_key,
                    len(coverage),
                    len(missing),
                )

                fetched_segments: list[dict[str, str]] = []
                for gap_start, gap_end in missing:
                    chunks = _split_by_page_size(gap_start, gap_end, bar_delta, request.page_size)
                    for chunk_start, chunk_end in chunks:
                        self._logger.info(
                            "historical_bars fetch cache_key=%s start=%s end=%s",
                            cache_key,
                            _to_iso_utc(chunk_start),
                            _to_iso_utc(chunk_end),
                        )
                        raw_bars = self._fetcher.fetch(
                            contract=request.contract,
                            start_time=chunk_start,
                            end_time=chunk_end,
                            bar_size=bar_size,
                            what_to_show=request.what_to_show,
                            use_rth=request.use_rth,
                        )
                        bars = [_coerce_bar(item) for item in raw_bars]
                        self._store_bars(conn, cache_key, bars, chunk_start, chunk_end)
                        fetched_segments.append(
                            {
                                "start": _to_iso_utc(chunk_start),
                                "end": _to_iso_utc(chunk_end),
                            }
                        )
                        coverage.append((chunk_start, chunk_end))

                if missing:
                    self._replace_coverage(conn, cache_key, coverage)
                    conn.commit()

                bars = self._read_bars(conn, cache_key, start, end)
                if not request.include_partial_bar and bar_delta is not None:
                    now = _to_utc(self._now_fn())
                    bars = [bar for bar in bars if bar.ts + bar_delta <= now]

                truncated = False
                if request.max_bars is not None and len(bars) > request.max_bars:
                    bars = bars[-request.max_bars :]
                    truncated = True

                covered_segments = _intersect_segments(
                    start,
                    end,
                    _merge_segments(self._load_coverage(conn, cache_key)),
                )
                requested_seconds = (end - start).total_seconds()
                missing_seconds = sum(
                    (seg_end - seg_start).total_seconds() for seg_start, seg_end in missing
                )
                cache_hit_ratio = 0.0
                if requested_seconds > 0:
                    cache_hit_ratio = max(
                        0.0,
                        min(1.0, (requested_seconds - missing_seconds) / requested_seconds),
                    )

                meta = {
                    "source": "IB",
                    "timezone": "UTC",
                    "bar_size": bar_size,
                    "what_to_show": request.what_to_show,
                    "use_rth": request.use_rth,
                    "include_partial_bar": request.include_partial_bar,
                    "cache_hit_ratio": cache_hit_ratio,
                    "has_gaps": len(missing) > 0,
                    "fetched_segments": fetched_segments,
                    "covered_segments": [
                        {"start": _to_iso_utc(seg_start), "end": _to_iso_utc(seg_end)}
                        for seg_start, seg_end in covered_segments
                    ],
                    "returned_bars": len(bars),
                    "truncated": truncated,
                }
                self._logger.info(
                    "historical_bars done cache_key=%s returned_bars=%d cache_hit_ratio=%.4f has_gaps=%s fetched_segments=%d truncated=%s",
                    cache_key,
                    len(bars),
                    cache_hit_ratio,
                    len(missing) > 0,
                    len(fetched_segments),
                    truncated,
                )
                return HistoricalBarsResult(bars=bars, meta=meta)
        except Exception:
            self._logger.exception("historical_bars failed cache_key=%s", cache_key)
            raise
