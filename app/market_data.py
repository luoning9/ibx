from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Mapping, Protocol

from .config import PROJECT_ROOT, load_app_config
from .logging_config import configure_market_data_logging
from .runtime_paths import resolve_market_cache_db_path


UTC = timezone.utc
DEFAULT_MARKET_DATA_FIXTURE_PATH = PROJECT_ROOT / "conf" / "fixtures" / "market_data.sample.json"


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


@dataclass(frozen=True)
class TradingCalendarRequest:
    contract_id: int
    as_of_time: datetime | None = None
    use_rth: bool = True


@dataclass(frozen=True)
class TradingCalendarSession:
    ref_date: str
    start_time: datetime
    end_time: datetime


@dataclass(frozen=True)
class TradingCalendarResult:
    sessions: list[TradingCalendarSession]
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
    ) -> (
        list[HistoricalBar | Mapping[str, Any]]
        | tuple[list[HistoricalBar | Mapping[str, Any]], Mapping[str, Any]]
    ):
        ...


class MarketDataProvider(Protocol):
    def get_historical_bars(self, request: HistoricalBarsRequest) -> HistoricalBarsResult:
        ...

    def get_trading_calendar(self, request: TradingCalendarRequest) -> TradingCalendarResult:
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


def _history_days_limit_for_bar(bar_delta: timedelta) -> tuple[int, str] | None:
    if bar_delta <= timedelta(minutes=1):
        return 7, "<=1 min"
    if bar_delta <= timedelta(minutes=10):
        return 30, "<=10 min"
    if bar_delta <= timedelta(minutes=30):
        return 60, "<=30 min"
    if bar_delta <= timedelta(hours=1):
        return 120, "<=1 hour"
    if bar_delta <= timedelta(hours=4):
        return 240, "<=4 hours"
    return None


def _validate_historical_window_limits(start: datetime, end: datetime, bar_size: str) -> timedelta:
    bar_delta = _parse_bar_size(bar_size)
    if bar_delta is None:
        raise ValueError("invalid bar_size")
    if bar_delta < timedelta(minutes=1):
        raise ValueError("bar_size must be at least 1 min")

    limit_spec = _history_days_limit_for_bar(bar_delta)
    if limit_spec is not None:
        max_days, label = limit_spec
        max_window = timedelta(days=max_days)
        if (end - start) > max_window:
            raise ValueError(
                f"requested window exceeds {max_days} days limit for bar_size {label}"
            )
    return bar_delta


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


def _align_segment_to_bar(
    start: datetime,
    end: datetime,
    bar_delta: timedelta | None,
) -> tuple[datetime, datetime]:
    if bar_delta is None:
        return start, end
    step_seconds = bar_delta.total_seconds()
    if step_seconds <= 0:
        return start, end

    start_ts = _to_utc(start).timestamp()
    end_ts = _to_utc(end).timestamp()
    aligned_start = datetime.fromtimestamp(math.floor(start_ts / step_seconds) * step_seconds, tz=UTC)
    aligned_end = datetime.fromtimestamp(math.ceil(end_ts / step_seconds) * step_seconds, tz=UTC)
    if aligned_end <= aligned_start:
        aligned_end = aligned_start + bar_delta
    return aligned_start, aligned_end


def _align_time_down_to_bar(
    ts: datetime,
    bar_delta: timedelta | None,
) -> datetime:
    if bar_delta is None:
        return _to_utc(ts)
    step_seconds = bar_delta.total_seconds()
    if step_seconds <= 0:
        return _to_utc(ts)
    ts_seconds = _to_utc(ts).timestamp()
    aligned_seconds = math.floor(ts_seconds / step_seconds) * step_seconds
    return datetime.fromtimestamp(aligned_seconds, tz=UTC)


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


def _split_fetch_response(
    raw_fetch: Any,
) -> tuple[list[HistoricalBar | Mapping[str, Any]], dict[str, Any]]:
    if (
        isinstance(raw_fetch, tuple)
        and len(raw_fetch) == 2
        and isinstance(raw_fetch[1], Mapping)
    ):
        raw_bars = list(raw_fetch[0])
        raw_meta = dict(raw_fetch[1])
        return raw_bars, raw_meta
    return list(raw_fetch), {}


def _require_fetch_trading_calendar(fetcher: Any) -> Any:
    fetch_fn = getattr(fetcher, "fetch_trading_calendar", None)
    if not callable(fetch_fn):
        raise RuntimeError("market data fetcher does not support trading calendar")
    return fetch_fn


def _resolve_trading_calendar_as_of(
    *,
    as_of_time: datetime | None,
    now_fn: Callable[[], datetime],
) -> datetime:
    now_raw = now_fn()
    now = now_raw if now_raw.tzinfo is not None else now_raw.replace(tzinfo=UTC)
    if as_of_time is None:
        return now
    normalized = as_of_time if as_of_time.tzinfo is not None else as_of_time.replace(tzinfo=UTC)
    if _to_utc(normalized) > _to_utc(now) + timedelta(days=7):
        raise ValueError("as_of_time cannot be later than now + 7 days")
    return normalized


def _trading_calendar_cache_key(*, contract_id: int, use_rth: bool, as_of_time: datetime) -> str:
    tz = as_of_time.tzinfo or UTC
    local_day = as_of_time.astimezone(tz).date().isoformat()
    tz_name = str(getattr(tz, "key", "") or tz)
    return "|".join(
        [
            str(int(contract_id)),
            "1" if use_rth else "0",
            tz_name,
            local_day,
        ]
    )


def _serialize_trading_calendar_result(result: TradingCalendarResult) -> str:
    payload = {
        "sessions": [
            {
                "ref_date": str(item.ref_date),
                "start_time": _to_iso_utc(item.start_time),
                "end_time": _to_iso_utc(item.end_time),
            }
            for item in result.sessions
        ],
        "meta": dict(result.meta),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _deserialize_trading_calendar_result(payload: str) -> TradingCalendarResult:
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("invalid trading calendar cache payload")
    raw_sessions = parsed.get("sessions", [])
    sessions: list[TradingCalendarSession] = []
    if isinstance(raw_sessions, list):
        for item in raw_sessions:
            if not isinstance(item, Mapping):
                continue
            ref_date = str(item.get("ref_date", "")).strip()
            start_raw = str(item.get("start_time", "")).strip()
            end_raw = str(item.get("end_time", "")).strip()
            if not ref_date or not start_raw or not end_raw:
                continue
            sessions.append(
                TradingCalendarSession(
                    ref_date=ref_date,
                    start_time=_parse_iso_utc(start_raw),
                    end_time=_parse_iso_utc(end_raw),
                )
            )
    meta_raw = parsed.get("meta", {})
    meta = dict(meta_raw) if isinstance(meta_raw, Mapping) else {}
    sessions.sort(key=lambda item: (item.start_time, item.end_time, item.ref_date))
    return TradingCalendarResult(sessions=sessions, meta=meta)


class SQLiteMarketDataCache:
    def __init__(
        self,
        *,
        fetcher: HistoricalBarsFetcher,
        db_path: str | Path | None = None,
        now_fn: Callable[[], datetime] | None = None,
        delay_window_minutes: int = 20,
    ) -> None:
        configure_market_data_logging()
        self._logger = logging.getLogger("ibx.market_data")
        self._fetcher = fetcher
        self._db_path = Path(db_path) if db_path is not None else resolve_market_cache_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._delay_window = timedelta(minutes=max(0, int(delay_window_minutes)))
        self._lock_guard = Lock()
        self._locks: dict[str, Lock] = {}
        self._init_db()
        self._logger.info(
            "SQLiteMarketDataCache initialized db_path=%s delay_window_minutes=%s",
            self._db_path.resolve(),
            int(self._delay_window.total_seconds() // 60),
        )

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

                CREATE TABLE IF NOT EXISTS market_trading_calendar (
                  cache_key TEXT NOT NULL PRIMARY KEY,
                  payload TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
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

    def _confirmed_segment_end(
        self,
        *,
        chunk_start: datetime,
        chunk_end: datetime,
        bars: list[HistoricalBar],
        bar_delta: timedelta | None,
        now: datetime,
    ) -> datetime:
        if chunk_start >= chunk_end:
            return chunk_start

        stable_cutoff = _to_utc(now) - self._delay_window
        confirmed_end = min(chunk_end, stable_cutoff) if stable_cutoff > chunk_start else chunk_start
        if bar_delta is None or bar_delta.total_seconds() <= 0:
            return max(chunk_start, confirmed_end)

        last_bar_start: datetime | None = None
        for bar in bars:
            bar_ts = _to_utc(bar.ts)
            if not (chunk_start <= bar_ts < chunk_end):
                continue
            if last_bar_start is None or bar_ts > last_bar_start:
                last_bar_start = bar_ts

        if last_bar_start is not None:
            last_bar_end = min(chunk_end, last_bar_start + bar_delta)
            if last_bar_end > confirmed_end:
                confirmed_end = last_bar_end
        confirmed_end = _align_time_down_to_bar(confirmed_end, bar_delta)
        return max(chunk_start, confirmed_end)

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

    def _load_trading_calendar(
        self,
        conn: sqlite3.Connection,
        cache_key: str,
    ) -> TradingCalendarResult | None:
        row = conn.execute(
            """
            SELECT payload
            FROM market_trading_calendar
            WHERE cache_key = ?
            LIMIT 1
            """,
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        return _deserialize_trading_calendar_result(str(row["payload"]))

    def _store_trading_calendar(
        self,
        conn: sqlite3.Connection,
        cache_key: str,
        result: TradingCalendarResult,
    ) -> None:
        now_iso = _to_iso_utc(self._now_fn())
        payload = _serialize_trading_calendar_result(result)
        conn.execute(
            """
            INSERT INTO market_trading_calendar (cache_key, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              payload = excluded.payload,
              updated_at = excluded.updated_at
            """,
            (cache_key, payload, now_iso),
        )

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
        bar_delta = _validate_historical_window_limits(start, end, bar_size)

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

                fetch_segments = missing
                if bar_delta is not None and bar_delta.total_seconds() > 0:
                    aligned: list[tuple[datetime, datetime]] = []
                    for gap_start, gap_end in missing:
                        aligned_start, aligned_end = _align_segment_to_bar(
                            gap_start,
                            gap_end,
                            bar_delta,
                        )
                        if aligned_start < aligned_end:
                            aligned.append((aligned_start, aligned_end))
                    fetch_segments = _merge_segments(aligned)

                fetched_segments: list[dict[str, str]] = []
                ib_errors: list[dict[str, Any]] = []
                ib_error_codes: set[int] = set()
                now = _to_utc(self._now_fn())
                for gap_start, gap_end in fetch_segments:
                    chunks = _split_by_page_size(gap_start, gap_end, bar_delta, request.page_size)
                    for chunk_start, chunk_end in chunks:
                        self._logger.info(
                            "historical_bars fetch cache_key=%s start=%s end=%s",
                            cache_key,
                            _to_iso_utc(chunk_start),
                            _to_iso_utc(chunk_end),
                        )
                        raw_fetch = self._fetcher.fetch(
                            contract=request.contract,
                            start_time=chunk_start,
                            end_time=chunk_end,
                            bar_size=bar_size,
                            what_to_show=request.what_to_show,
                            use_rth=request.use_rth,
                        )
                        raw_bars, fetch_meta = _split_fetch_response(raw_fetch)
                        bars = [_coerce_bar(item) for item in raw_bars]
                        for item in fetch_meta.get("ib_errors", []):
                            if not isinstance(item, Mapping):
                                continue
                            payload = {str(k): item[k] for k in item}
                            payload["segment_start"] = _to_iso_utc(chunk_start)
                            payload["segment_end"] = _to_iso_utc(chunk_end)
                            ib_errors.append(payload)
                            code_raw = payload.get("code")
                            try:
                                ib_error_codes.add(int(code_raw))
                            except Exception:
                                pass
                        self._store_bars(conn, cache_key, bars, chunk_start, chunk_end)
                        fetched_segments.append(
                            {
                                "start": _to_iso_utc(chunk_start),
                                "end": _to_iso_utc(chunk_end),
                            }
                        )
                        confirmed_end = self._confirmed_segment_end(
                            chunk_start=chunk_start,
                            chunk_end=chunk_end,
                            bars=bars,
                            bar_delta=bar_delta,
                            now=now,
                        )
                        if confirmed_end > chunk_start:
                            coverage.append((chunk_start, confirmed_end))
                        self._logger.info(
                            "historical_bars coverage cache_key=%s chunk_start=%s chunk_end=%s confirmed_end=%s",
                            cache_key,
                            _to_iso_utc(chunk_start),
                            _to_iso_utc(chunk_end),
                            _to_iso_utc(confirmed_end),
                        )

                if fetched_segments:
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
                    "delay_window_minutes": int(self._delay_window.total_seconds() // 60),
                    "cache_hit_ratio": cache_hit_ratio,
                    "has_gaps": len(missing) > 0,
                    "fetched_segments": fetched_segments,
                    "covered_segments": [
                        {"start": _to_iso_utc(seg_start), "end": _to_iso_utc(seg_end)}
                        for seg_start, seg_end in covered_segments
                    ],
                    "ib_error_count": len(ib_errors),
                    "ib_error_codes": sorted(ib_error_codes),
                    "ib_errors": ib_errors,
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

    def get_trading_calendar(self, request: TradingCalendarRequest) -> TradingCalendarResult:
        if int(request.contract_id) <= 0:
            raise ValueError("contract_id must be positive")
        as_of_time = _resolve_trading_calendar_as_of(
            as_of_time=request.as_of_time,
            now_fn=self._now_fn,
        )
        cache_key = _trading_calendar_cache_key(
            contract_id=int(request.contract_id),
            use_rth=request.use_rth,
            as_of_time=as_of_time,
        )
        self._logger.info(
            "trading_calendar request cache_enabled=%s cache_key=%s as_of_time=%s use_rth=%s",
            True,
            cache_key,
            _to_iso_utc(as_of_time),
            request.use_rth,
        )
        key_lock = self._key_lock(f"trading_calendar:{cache_key}")
        fetch_fn = _require_fetch_trading_calendar(self._fetcher)
        with key_lock, self._conn() as conn:
            cached = self._load_trading_calendar(conn, cache_key)
            if cached is not None:
                cached_meta = dict(cached.meta)
                cached_meta["cache_enabled"] = True
                cached_meta["cache_hit"] = True
                self._logger.info(
                    "trading_calendar cache_hit cache_key=%s sessions=%d",
                    cache_key,
                    len(cached.sessions),
                )
                return TradingCalendarResult(
                    sessions=list(cached.sessions),
                    meta=cached_meta,
                )

            result = fetch_fn(
                contract_id=int(request.contract_id),
                as_of_time=as_of_time,
                use_rth=request.use_rth,
            )
            if not isinstance(result, TradingCalendarResult):
                raise RuntimeError("fetch_trading_calendar returned unsupported payload")

            self._store_trading_calendar(conn, cache_key, result)
            conn.commit()

            meta = dict(result.meta)
            meta["cache_enabled"] = True
            meta["cache_hit"] = False
            self._logger.info(
                "trading_calendar cache_miss cache_key=%s sessions=%d",
                cache_key,
                len(result.sessions),
            )
            return TradingCalendarResult(
                sessions=list(result.sessions),
                meta=meta,
            )


class DirectIBMarketDataProvider:
    def __init__(
        self,
        *,
        fetcher: HistoricalBarsFetcher,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        configure_market_data_logging()
        self._logger = logging.getLogger("ibx.market_data")
        self._fetcher = fetcher
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._logger.info("DirectIBMarketDataProvider initialized (cache disabled)")

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
        bar_delta = _validate_historical_window_limits(start, end, bar_size)

        cache_key = _cache_key(
            request.contract,
            bar_size=bar_size,
            what_to_show=request.what_to_show,
            use_rth=request.use_rth,
        )
        self._logger.info(
            "historical_bars direct request cache_key=%s start=%s end=%s bar_size=%s what_to_show=%s use_rth=%s include_partial_bar=%s max_bars=%s page_size=%s",
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

        fetch_segments = _split_by_page_size(start, end, bar_delta, request.page_size)
        fetched_segments: list[dict[str, str]] = []
        ib_errors: list[dict[str, Any]] = []
        ib_error_codes: set[int] = set()
        bars_by_ts: dict[datetime, HistoricalBar] = {}
        try:
            for chunk_start, chunk_end in fetch_segments:
                self._logger.info(
                    "historical_bars direct fetch cache_key=%s start=%s end=%s",
                    cache_key,
                    _to_iso_utc(chunk_start),
                    _to_iso_utc(chunk_end),
                )
                raw_fetch = self._fetcher.fetch(
                    contract=request.contract,
                    start_time=chunk_start,
                    end_time=chunk_end,
                    bar_size=bar_size,
                    what_to_show=request.what_to_show,
                    use_rth=request.use_rth,
                )
                raw_bars, fetch_meta = _split_fetch_response(raw_fetch)
                for item in raw_bars:
                    bar = _coerce_bar(item)
                    if start <= bar.ts < end:
                        bars_by_ts[bar.ts] = bar
                for item in fetch_meta.get("ib_errors", []):
                    if not isinstance(item, Mapping):
                        continue
                    payload = {str(k): item[k] for k in item}
                    payload["segment_start"] = _to_iso_utc(chunk_start)
                    payload["segment_end"] = _to_iso_utc(chunk_end)
                    ib_errors.append(payload)
                    code_raw = payload.get("code")
                    try:
                        ib_error_codes.add(int(code_raw))
                    except Exception:
                        pass
                fetched_segments.append(
                    {
                        "start": _to_iso_utc(chunk_start),
                        "end": _to_iso_utc(chunk_end),
                    }
                )

            bars = [bars_by_ts[ts] for ts in sorted(bars_by_ts)]
            if not request.include_partial_bar and bar_delta is not None:
                now = _to_utc(self._now_fn())
                bars = [bar for bar in bars if bar.ts + bar_delta <= now]

            truncated = False
            if request.max_bars is not None and len(bars) > request.max_bars:
                bars = bars[-request.max_bars :]
                truncated = True

            meta = {
                "source": "IB",
                "timezone": "UTC",
                "bar_size": bar_size,
                "what_to_show": request.what_to_show,
                "use_rth": request.use_rth,
                "include_partial_bar": request.include_partial_bar,
                "cache_enabled": False,
                "cache_hit_ratio": 0.0,
                "has_gaps": len(bars) == 0,
                "fetched_segments": fetched_segments,
                "covered_segments": fetched_segments,
                "ib_error_count": len(ib_errors),
                "ib_error_codes": sorted(ib_error_codes),
                "ib_errors": ib_errors,
                "returned_bars": len(bars),
                "truncated": truncated,
            }
            self._logger.info(
                "historical_bars direct done cache_key=%s returned_bars=%d fetched_segments=%d truncated=%s",
                cache_key,
                len(bars),
                len(fetched_segments),
                truncated,
            )
            return HistoricalBarsResult(bars=bars, meta=meta)
        except Exception:
            self._logger.exception("historical_bars direct failed cache_key=%s", cache_key)
            raise

    def get_trading_calendar(self, request: TradingCalendarRequest) -> TradingCalendarResult:
        if int(request.contract_id) <= 0:
            raise ValueError("contract_id must be positive")
        as_of_time = _resolve_trading_calendar_as_of(
            as_of_time=request.as_of_time,
            now_fn=self._now_fn,
        )
        self._logger.info(
            "trading_calendar direct request as_of_time=%s use_rth=%s",
            _to_iso_utc(as_of_time),
            request.use_rth,
        )
        fetch_fn = _require_fetch_trading_calendar(self._fetcher)
        result = fetch_fn(
            contract_id=int(request.contract_id),
            as_of_time=as_of_time,
            use_rth=request.use_rth,
        )
        if not isinstance(result, TradingCalendarResult):
            raise RuntimeError("fetch_trading_calendar returned unsupported payload")
        self._logger.info(
            "trading_calendar direct done sessions=%d",
            len(result.sessions),
        )
        return result


class FixtureMarketDataProvider:
    def __init__(
        self,
        *,
        fixture_path: str | Path | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        configure_market_data_logging()
        self._logger = logging.getLogger("ibx.market_data")
        self._fixture_path = Path(fixture_path) if fixture_path is not None else DEFAULT_MARKET_DATA_FIXTURE_PATH
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._cache: dict[str, Any] | None = None
        self._logger.info("FixtureMarketDataProvider initialized fixture_path=%s", self._fixture_path.resolve())

    def _load_payload(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if not self._fixture_path.exists():
            raise RuntimeError(f"market data fixture not found: {self._fixture_path}")
        try:
            payload = json.loads(self._fixture_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid market data fixture JSON: {self._fixture_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("market data fixture root must be a JSON object")
        self._cache = payload
        return payload

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
        bar_delta = _validate_historical_window_limits(start, end, bar_size)

        cache_key = _cache_key(
            request.contract,
            bar_size=bar_size,
            what_to_show=request.what_to_show,
            use_rth=request.use_rth,
        )
        payload = self._load_payload()
        series = payload.get("series")
        if not isinstance(series, list):
            raise RuntimeError("market data fixture must contain `series` list")

        bars: list[HistoricalBar] = []
        for item in series:
            row = item if isinstance(item, dict) else {}
            row_contract = row.get("contract")
            row_bar_size = str(row.get("bar_size", "")).strip() or bar_size
            row_what_to_show = str(row.get("what_to_show", "TRADES"))
            row_use_rth = bool(row.get("use_rth", True))
            try:
                row_key = _cache_key(
                    row_contract,
                    bar_size=row_bar_size,
                    what_to_show=row_what_to_show,
                    use_rth=row_use_rth,
                )
            except ValueError:
                continue
            if row_key != cache_key:
                continue
            raw_bars = row.get("bars")
            if not isinstance(raw_bars, list):
                continue
            for raw_bar in raw_bars:
                bar = _coerce_bar(raw_bar)
                if start <= bar.ts < end:
                    bars.append(bar)

        bars.sort(key=lambda x: x.ts)
        if not request.include_partial_bar and bar_delta is not None:
            now = _to_utc(self._now_fn())
            bars = [bar for bar in bars if bar.ts + bar_delta <= now]

        truncated = False
        if request.max_bars is not None and len(bars) > request.max_bars:
            bars = bars[-request.max_bars :]
            truncated = True

        meta = {
            "source": "FIXTURE",
            "timezone": "UTC",
            "bar_size": bar_size,
            "what_to_show": request.what_to_show,
            "use_rth": request.use_rth,
            "include_partial_bar": request.include_partial_bar,
            "cache_hit_ratio": 1.0,
            "has_gaps": len(bars) == 0,
            "fetched_segments": [],
            "covered_segments": [{"start": _to_iso_utc(start), "end": _to_iso_utc(end)}],
            "ib_error_count": 0,
            "ib_error_codes": [],
            "ib_errors": [],
            "returned_bars": len(bars),
            "truncated": truncated,
        }
        return HistoricalBarsResult(bars=bars, meta=meta)

    def get_trading_calendar(self, request: TradingCalendarRequest) -> TradingCalendarResult:
        if int(request.contract_id) <= 0:
            raise ValueError("contract_id must be positive")
        as_of_utc = _resolve_trading_calendar_as_of(
            as_of_time=request.as_of_time,
            now_fn=self._now_fn,
        )
        return TradingCalendarResult(
            sessions=[],
            meta={
                "source": "FIXTURE",
                "supported": False,
                "reason": "fixture provider does not provide exchange trading calendar",
                "contract_id": int(request.contract_id),
                "as_of_time": _to_iso_utc(as_of_utc),
                "use_rth": request.use_rth,
            },
        )


def build_market_data_provider_from_config(
    *,
    fetcher: HistoricalBarsFetcher | None = None,
    db_path: str | Path | None = None,
    fixture_path: str | Path | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> MarketDataProvider:
    cfg = load_app_config()
    provider = cfg.providers.market_data
    if provider == "fixture":
        return FixtureMarketDataProvider(
            fixture_path=fixture_path,
            now_fn=now_fn,
        )
    if fetcher is None:
        raise ValueError("fetcher is required when providers.market_data=ib")
    if cfg.providers.market_data_disable_cache:
        return DirectIBMarketDataProvider(
            fetcher=fetcher,
            now_fn=now_fn,
        )
    return SQLiteMarketDataCache(
        fetcher=fetcher,
        db_path=db_path,
        now_fn=now_fn,
        delay_window_minutes=cfg.providers.market_data_delay_window_minutes,
    )
