#!/usr/bin/env python3
"""Fetch latest completed historical bar from IB Gateway."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import infer_ib_api_port, load_app_config
from app.market_config import resolve_market_profile
from app.market_data import (
    HistoricalBar,
    HistoricalBarsRequest,
    build_market_data_provider_from_config,
)
from app.runtime_paths import resolve_market_cache_db_path

try:
    from ib_insync import IB, Future, Stock
except ModuleNotFoundError:
    IB = None  # type: ignore[assignment]
    Future = None  # type: ignore[assignment]
    Stock = None  # type: ignore[assignment]


UTC = timezone.utc


def infer_default_port() -> int:
    cfg = load_app_config().ib_gateway
    mode = os.getenv("TRADING_MODE", cfg.trading_mode).strip().lower()
    return infer_ib_api_port(mode)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_bar_size_delta(bar_size: str) -> timedelta | None:
    text = bar_size.strip().lower()
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


def _duration_str(start_time: datetime, end_time: datetime, *, bar_delta: timedelta | None) -> str:
    seconds = max(1, math.ceil((_to_utc(end_time) - _to_utc(start_time)).total_seconds()))

    # IB historical duration constraints differ by bar size.
    # Hour/day bars are safer with day-based duration to avoid error 321.
    if bar_delta is not None and bar_delta >= timedelta(hours=1):
        days = max(1, math.ceil(seconds / 86400))
        return f"{days} D"

    if seconds <= 86400:
        return f"{seconds} S"
    days = max(1, math.ceil(seconds / 86400))
    return f"{days} D"


def _iso_utc(dt: datetime) -> str:
    return _to_utc(dt).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ib_utc(dt: datetime) -> str:
    return _to_utc(dt).strftime("%Y%m%d %H:%M:%S UTC")


def _aligned_query_end(now: datetime, bar_delta: timedelta | None) -> datetime:
    base = _to_utc(now).replace(microsecond=0)
    if bar_delta is None:
        return base
    step = int(bar_delta.total_seconds())
    if step <= 0:
        return base
    epoch = int(base.timestamp())
    aligned = epoch - (epoch % step)
    return datetime.fromtimestamp(aligned, tz=UTC)


def _lookback_candidates(bar_delta: timedelta | None, lookback_bars: int) -> list[timedelta]:
    if bar_delta is None:
        return [timedelta(days=2), timedelta(days=7), timedelta(days=30)]

    base = bar_delta * max(2, lookback_bars)
    candidates = [base, base * 3, base * 8]
    if bar_delta >= timedelta(days=1):
        candidates.extend([timedelta(days=365)])
    elif bar_delta >= timedelta(hours=1):
        candidates.extend([timedelta(days=30), timedelta(days=90)])
    else:
        candidates.extend([timedelta(days=7), timedelta(days=30)])

    out: list[timedelta] = []
    seen: set[int] = set()
    for item in sorted(candidates, key=lambda x: x.total_seconds()):
        seconds = int(item.total_seconds())
        if seconds <= 0 or seconds in seen:
            continue
        seen.add(seconds)
        out.append(item)
    return out


def _coerce_ib_bar_ts(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return _to_utc(raw)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=UTC)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise RuntimeError("empty bar date string")
        if len(text) == 8 and text.isdigit():
            parsed = datetime.strptime(text, "%Y%m%d")
            return parsed.replace(tzinfo=UTC)
        normalized = " ".join(text.split())
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            return _to_utc(parsed)
        except ValueError:
            pass
        try:
            parsed = datetime.strptime(normalized, "%Y%m%d %H:%M:%S")
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
        raise RuntimeError(f"unsupported bar date string format: {text!r}")
    raise RuntimeError(f"unexpected bar date type: {type(raw)!r}")


class IBHistoricalFetcher:
    def __init__(self, ib: IB) -> None:
        self._ib = ib
        self._contract_cache: dict[str, Any] = {}

    def fetch(
        self,
        *,
        contract: Mapping[str, Any] | str,
        start_time: datetime,
        end_time: datetime,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
    ) -> list[HistoricalBar]:
        ib_contract = self._resolve_contract(contract)
        bar_delta = _parse_bar_size_delta(bar_size)
        bars = self._ib.reqHistoricalData(
            ib_contract,
            endDateTime=_ib_utc(end_time),
            durationStr=_duration_str(start_time, end_time, bar_delta=bar_delta),
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=2,
            keepUpToDate=False,
        )
        out: list[HistoricalBar] = []
        for item in bars:
            out.append(
                HistoricalBar(
                    ts=_coerce_ib_bar_ts(item.date),
                    open=float(item.open),
                    high=float(item.high),
                    low=float(item.low),
                    close=float(item.close),
                    volume=None if item.volume is None else float(item.volume),
                    wap=None if item.average is None else float(item.average),
                    count=None if item.barCount is None else int(item.barCount),
                )
            )
        return out

    def _resolve_contract(self, contract: Mapping[str, Any] | str) -> Any:
        if Stock is None or Future is None:
            raise RuntimeError("ib_insync is required for IB-backed market data fetcher")
        if isinstance(contract, str):
            payload = {"market": "US_STOCK", "code": contract}
        else:
            payload = dict(contract)

        code = str(payload.get("code", "")).strip().upper()
        if not code:
            raise ValueError("contract.code is required")
        market = str(payload.get("market", "US_STOCK")).strip().upper() or "US_STOCK"
        contract_month = str(payload.get("contract_month", "")).strip()

        cache_key = f"{market}|{code}|{contract_month}"
        cached = self._contract_cache.get(cache_key)
        if cached is not None:
            return cached

        profile = resolve_market_profile(market, None)
        if profile.sec_type == "STK":
            candidate = Stock(symbol=code, exchange=profile.exchange, currency=profile.currency)
            qualified = self._ib.qualifyContracts(candidate)
            if not qualified:
                raise RuntimeError(f"failed to qualify stock contract: market={market}, code={code}")
            resolved = qualified[0]
            self._contract_cache[cache_key] = resolved
            return resolved

        if profile.sec_type == "FUT":
            if contract_month:
                candidate = Future(
                    symbol=code,
                    lastTradeDateOrContractMonth=contract_month,
                    exchange=profile.exchange,
                    currency=profile.currency,
                )
                qualified = self._ib.qualifyContracts(candidate)
                if not qualified:
                    raise RuntimeError(
                        f"failed to qualify future contract: market={market}, code={code}, month={contract_month}"
                    )
                resolved = qualified[0]
                self._contract_cache[cache_key] = resolved
                return resolved

            probe = Future(symbol=code, exchange=profile.exchange, currency=profile.currency)
            details = self._ib.reqContractDetails(probe)
            if details:
                resolved = self._pick_front_contract(details)
                self._contract_cache[cache_key] = resolved
                return resolved

            fallback = Future(localSymbol=code, exchange=profile.exchange, currency=profile.currency)
            qualified = self._ib.qualifyContracts(fallback)
            if not qualified:
                raise RuntimeError(
                    f"failed to resolve future contract: market={market}, code={code}; "
                    "try --contract-month for explicit contract"
                )
            resolved = qualified[0]
            self._contract_cache[cache_key] = resolved
            return resolved

        raise RuntimeError(f"unsupported sec_type for market={market}: {profile.sec_type}")

    def _pick_front_contract(self, details: list[Any]) -> Any:
        today = datetime.now(UTC).strftime("%Y%m%d")
        entries: list[tuple[str, str, Any]] = []
        for detail in details:
            c = detail.contract
            month = str(getattr(c, "lastTradeDateOrContractMonth", "")).strip()
            if month:
                entries.append((month, self._to_cmp_day(month), c))
        if not entries:
            return details[0].contract

        ordered = sorted(entries, key=lambda item: item[1])
        for _, cmp_day, contract in ordered:
            if cmp_day >= today:
                qualified = self._ib.qualifyContracts(contract)
                if qualified:
                    return qualified[0]
        qualified = self._ib.qualifyContracts(ordered[-1][2])
        if qualified:
            return qualified[0]
        raise RuntimeError("failed to qualify resolved future contract")

    def _to_cmp_day(self, value: str) -> str:
        raw = value.strip()
        if len(raw) >= 8 and raw[:8].isdigit():
            return raw[:8]
        if len(raw) == 6 and raw.isdigit():
            return raw + "99"
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) >= 8:
            return digits[:8]
        if len(digits) == 6:
            return digits + "99"
        return "00000000"


def parse_args() -> argparse.Namespace:
    cfg = load_app_config().ib_gateway
    parser = argparse.ArgumentParser(description="Get latest completed historical bar by code")
    parser.add_argument("--code", required=True, help="Product code, e.g. AAPL or GC")
    parser.add_argument("--bar-size", required=True, help="IB bar size, e.g. '1 min', '5 mins', '1 hour'")
    parser.add_argument(
        "--market",
        default="US_STOCK",
        help="Market key from conf/markets.json (default: US_STOCK)",
    )
    parser.add_argument(
        "--contract-month",
        default="",
        help="Optional future contract month (YYYYMM or YYYYMMDD), only used for FUT market",
    )
    parser.add_argument("--what-to-show", default="TRADES", help="IB whatToShow (default: TRADES)")
    parser.add_argument(
        "--host",
        default=os.getenv("IB_HOST", cfg.host),
        help="IB host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("IB_API_PORT", str(infer_default_port()))),
        help="IB API port",
    )
    parser.add_argument(
        "--client-id",
        type=int,
        default=int(os.getenv("IB_CLIENT_ID", str(cfg.client_id))),
        help="IB client id",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("IB_TIMEOUT", str(cfg.timeout_seconds))),
        help="Connect timeout seconds",
    )
    parser.add_argument(
        "--lookback-bars",
        type=int,
        default=30,
        help="Lookback bars for query window (default: 30)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=500,
        help="Segment fetch page size in bars (default: 500)",
    )
    parser.add_argument(
        "--cache-db",
        default=os.getenv("IBX_MARKET_CACHE_DB_PATH", str(resolve_market_cache_db_path())),
        help="Override market cache sqlite path",
    )
    parser.add_argument(
        "--all-hours",
        action="store_true",
        help="Use all sessions (useRTH=0); default is regular trading hours",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON output",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_app_config()
    bar_delta = _parse_bar_size_delta(args.bar_size)
    now = _aligned_query_end(datetime.now(UTC), bar_delta)

    request_contract: dict[str, Any] = {"market": args.market, "code": args.code}
    if args.contract_month:
        request_contract["contract_month"] = args.contract_month

    ib: IB | None = None

    try:
        if cfg.providers.market_data == "fixture":
            cache = build_market_data_provider_from_config(
                now_fn=lambda: now,
            )
        else:
            if IB is None:
                print(
                    "[ERROR] Missing dependency: ib_insync. Install with: pip install ib_insync",
                    file=sys.stderr,
                )
                return 3
            ib = IB()
            try:
                ib.connect(
                    host=args.host,
                    port=args.port,
                    clientId=args.client_id,
                    timeout=args.timeout,
                    readonly=True,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] Failed to connect IB API: {exc}", file=sys.stderr)
                return 1
            fetcher = IBHistoricalFetcher(ib)
            cache = (
                build_market_data_provider_from_config(
                    fetcher=fetcher,
                    now_fn=lambda: now,
                )
                if not args.cache_db.strip()
                else build_market_data_provider_from_config(
                    fetcher=fetcher,
                    db_path=Path(args.cache_db.strip()),
                    now_fn=lambda: now,
                )
            )
        result = None
        for lookback in _lookback_candidates(bar_delta, args.lookback_bars):
            start = now - lookback
            current = cache.get_historical_bars(
                HistoricalBarsRequest(
                    contract=request_contract,
                    start_time=start,
                    end_time=now,
                    bar_size=args.bar_size,
                    what_to_show=args.what_to_show,
                    use_rth=not args.all_hours,
                    include_partial_bar=False,
                    max_bars=1,
                    page_size=args.page_size,
                )
            )
            result = current
            if current.bars:
                break
        assert result is not None
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Query latest bar failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if ib is not None and ib.isConnected():
            ib.disconnect()

    if not result.bars:
        print(
            "[WARN] No bars returned after expanding lookback windows. "
            "Try --all-hours or check symbol/market.",
            file=sys.stderr,
        )
        return 4

    latest = result.bars[-1]
    payload = {
        "code": args.code.upper(),
        "market": args.market.upper(),
        "bar_size": args.bar_size,
        "what_to_show": args.what_to_show,
        "use_rth": not args.all_hours,
        "bar": {
            "ts": _iso_utc(latest.ts),
            "open": latest.open,
            "high": latest.high,
            "low": latest.low,
            "close": latest.close,
            "volume": latest.volume,
            "wap": latest.wap,
            "count": latest.count,
        },
        "meta": {
            "cache_hit_ratio": result.meta.get("cache_hit_ratio"),
            "has_gaps": result.meta.get("has_gaps"),
            "fetched_segments": result.meta.get("fetched_segments"),
        },
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        bar = payload["bar"]
        print(
            f"[OK] {payload['market']} {payload['code']} {payload['bar_size']} "
            f"ts={bar['ts']} O={bar['open']} H={bar['high']} L={bar['low']} C={bar['close']} "
            f"V={bar['volume']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
