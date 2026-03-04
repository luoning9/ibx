#!/usr/bin/env python3
"""Fetch latest completed historical bar from IB Gateway."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import SUPPORTED_IB_ROLES, load_app_config, resolve_ib_client_id
from app.ib_compat import INSTALL_HINT, is_missing_ib_dependency_error
from app.ib_session_manager import close_ib_session_manager
from app.market_data import (
    DirectIBMarketDataProvider,
    HistoricalBarsRequest,
    build_market_data_provider_from_config,
)
from app.ib_market_data import IBSessionHistoricalFetcher
from app.runtime_paths import resolve_market_cache_db_path


UTC = timezone.utc
CLIENT_ID_CONFLICT_ERROR_CODE = 5


class ClientIdConflictError(RuntimeError):
    """Raised when IB rejects the connection because client id is already in use."""


def _looks_like_client_id_conflict(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    if "error 326" in lowered:
        return True
    if "client id is already in use" in lowered:
        return True
    if "clientid" in lowered and "already in use" in lowered:
        return True
    if "client id" in lowered and "already in use" in lowered:
        return True
    return False


def _is_client_id_conflict_exception(exc: Exception) -> bool:
    pending: list[BaseException | None] = [exc]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None:
            continue
        obj_id = id(current)
        if obj_id in visited:
            continue
        visited.add(obj_id)
        if _looks_like_client_id_conflict(str(current)):
            return True
        pending.append(getattr(current, "__cause__", None))
        pending.append(getattr(current, "__context__", None))
    return False


def _extract_client_id_conflict_message_from_meta(meta: dict[str, Any]) -> str | None:
    error_codes = meta.get("ib_error_codes")
    if isinstance(error_codes, list):
        for item in error_codes:
            try:
                if int(item) == 326:
                    return "ib_error_codes contains 326 (client id already in use)"
            except Exception:
                continue

    ib_errors = meta.get("ib_errors")
    if isinstance(ib_errors, list):
        for item in ib_errors:
            if not isinstance(item, dict):
                continue
            code_raw = item.get("code")
            try:
                if int(code_raw) == 326:
                    message = str(item.get("message") or "").strip()
                    return message or "ib_errors contains code=326 (client id already in use)"
            except Exception:
                pass
            message = str(item.get("message") or "").strip()
            if _looks_like_client_id_conflict(message):
                return message
    return None


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


def _iso_utc(dt: datetime) -> str:
    return _to_utc(dt).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def parse_args() -> argparse.Namespace:
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
        "--use-role",
        default=str(os.getenv("IB_USE_ROLE", "cli")).strip().lower(),
        help=f"IB role connection to use ({', '.join(SUPPORTED_IB_ROLES)})",
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
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass local market cache and fetch directly from IB",
    )
    return parser.parse_args()


def _is_missing_ib_dependency(exc: Exception) -> bool:
    return is_missing_ib_dependency_error(exc)


def main() -> int:
    args = parse_args()
    cfg = load_app_config()
    use_role = str(args.use_role or "").strip().lower()
    if use_role not in SUPPORTED_IB_ROLES:
        use_role = "cli"
    bar_delta = _parse_bar_size_delta(args.bar_size)
    now = _aligned_query_end(datetime.now(UTC), bar_delta)

    request_contract: dict[str, Any] = {"market": args.market, "code": args.code}
    if args.contract_month:
        request_contract["contract_month"] = args.contract_month

    try:
        if cfg.providers.market_data == "fixture":
            cache = build_market_data_provider_from_config(now_fn=lambda: now)
        else:
            fetcher = IBSessionHistoricalFetcher(use_role=use_role)
            if args.no_cache:
                cache = DirectIBMarketDataProvider(
                    fetcher=fetcher,
                    now_fn=lambda: now,
                )
            else:
                cache = (
                    build_market_data_provider_from_config(fetcher=fetcher, now_fn=lambda: now)
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
            conflict_message = _extract_client_id_conflict_message_from_meta(current.meta)
            if conflict_message is not None:
                raise ClientIdConflictError(conflict_message)
            if current.bars:
                break
        assert result is not None
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ClientIdConflictError) or _is_client_id_conflict_exception(exc):
            detail = str(exc).strip() or f"role={use_role} client_id={resolve_ib_client_id(use_role)} already in use"
            print(f"[ERROR] IB client id conflict: {detail}", file=sys.stderr)
            return CLIENT_ID_CONFLICT_ERROR_CODE
        if _is_missing_ib_dependency(exc):
            print(f"[ERROR] {INSTALL_HINT}", file=sys.stderr)
            return 3
        print(f"[ERROR] Query latest bar failed: {exc}", file=sys.stderr)
        return 2
    finally:
        close_ib_session_manager()

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
