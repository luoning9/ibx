#!/usr/bin/env python3
"""Fetch local-time trading sessions for today and tomorrow from IB."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import infer_ib_api_port, load_app_config, resolve_ib_client_id
from app.market_config import resolve_market_profile


UTC = timezone.utc
ET = ZoneInfo("America/New_York")
LOCAL_TZ = datetime.now().astimezone().tzinfo or ET


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_schedule_dt(value: str, *, fallback_tz: ZoneInfo) -> datetime:
    text = str(value).strip()
    for fmt in ("%Y%m%d-%H:%M:%S", "%Y%m%d %H:%M:%S", "%Y%m%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=fallback_tz)
        except ValueError:
            continue
    raise ValueError(f"unsupported schedule datetime: {value!r}")


def _pick_front_future_contract(ib: Any, details: list[Any]) -> Any:
    today = datetime.now(UTC).strftime("%Y%m%d")
    entries: list[tuple[str, str, Any]] = []
    for detail in details:
        contract = detail.contract
        month = str(getattr(contract, "lastTradeDateOrContractMonth", "")).strip()
        if not month:
            continue
        digits = "".join(ch for ch in month if ch.isdigit())
        if len(digits) >= 8:
            cmp_day = digits[:8]
        elif len(digits) == 6:
            cmp_day = digits + "99"
        else:
            continue
        entries.append((month, cmp_day, contract))
    if not entries:
        raise RuntimeError("failed to resolve front future contract: no dated contract details")

    entries.sort(key=lambda item: item[1])
    for _, cmp_day, contract in entries:
        if cmp_day < today:
            continue
        qualified = list(ib.qualifyContracts(contract))
        if qualified:
            return qualified[0]

    qualified = list(ib.qualifyContracts(entries[-1][2]))
    if qualified:
        return qualified[0]
    raise RuntimeError("failed to qualify front future contract")


def _resolve_contract_with_ib(ib: Any, *, code: str, market: str, contract_month: str | None) -> Any:
    profile = resolve_market_profile(market, None)
    try:
        from ib_insync import Future, Stock
    except ModuleNotFoundError as exc:
        raise RuntimeError("ib_insync is required; install with: pip install ib_insync") from exc

    symbol = code.strip().upper()
    if not symbol:
        raise ValueError("code cannot be empty")

    if profile.sec_type == "STK":
        candidate = Stock(symbol=symbol, exchange=profile.exchange, currency=profile.currency)
        qualified = list(ib.qualifyContracts(candidate))
        if not qualified:
            raise RuntimeError(f"failed to qualify stock contract: market={market}, code={symbol}")
        return qualified[0]

    if profile.sec_type != "FUT":
        raise RuntimeError(f"unsupported sec_type for market={market}: {profile.sec_type}")

    month = str(contract_month or "").strip()
    if month:
        candidate = Future(
            symbol=symbol,
            lastTradeDateOrContractMonth=month,
            exchange=profile.exchange,
            currency=profile.currency,
        )
        qualified = list(ib.qualifyContracts(candidate))
        if not qualified:
            raise RuntimeError(
                f"failed to qualify future contract: market={market}, code={symbol}, contract_month={month}"
            )
        return qualified[0]

    probe = Future(symbol=symbol, exchange=profile.exchange, currency=profile.currency)
    details = list(ib.reqContractDetails(probe))
    if not details:
        raise RuntimeError(
            f"failed to resolve future contract by details: market={market}, code={symbol}; "
            "try --contract-month"
        )
    return _pick_front_future_contract(ib, details)


def infer_default_port() -> int:
    cfg = load_app_config().ib_gateway
    mode = os.getenv("TRADING_MODE", cfg.trading_mode).strip().lower()
    return infer_ib_api_port(mode)


def parse_args() -> argparse.Namespace:
    cfg = load_app_config().ib_gateway
    parser = argparse.ArgumentParser(description="Get local-time trading calendar for today and tomorrow")
    parser.add_argument("--code", required=True, help="Product code, e.g. SLV or GC")
    parser.add_argument(
        "--market",
        default="US_STOCK",
        help="Market key from conf/markets.json (default: US_STOCK)",
    )
    parser.add_argument(
        "--contract-month",
        default="",
        help="Optional futures contract month (YYYYMM or YYYYMMDD)",
    )
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
        default=int(os.getenv("IB_CLIENT_ID", str(resolve_ib_client_id("cli")))),
        help="IB client id",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("IB_TIMEOUT", str(cfg.timeout_seconds))),
        help="IB connect/request timeout seconds",
    )
    rth_group = parser.add_mutually_exclusive_group()
    rth_group.add_argument(
        "--rth",
        dest="use_rth",
        action="store_true",
        help="Use regular trading hours (default).",
    )
    rth_group.add_argument(
        "--no-rth",
        dest="use_rth",
        action="store_false",
        help="Use all sessions, including extended hours.",
    )
    parser.set_defaults(use_rth=True)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    market = str(args.market).strip().upper()
    contract_month = str(args.contract_month).strip() or None
    use_rth = bool(args.use_rth)

    now_local = datetime.now(LOCAL_TZ)
    today_local = now_local.date()
    tomorrow_local = today_local + timedelta(days=1)
    target_dates_local = {today_local, tomorrow_local}
    # Pull through local tomorrow end-of-day to ensure both days are covered.
    end_dt_local = datetime.combine(tomorrow_local, time(23, 59, 59), tzinfo=LOCAL_TZ)

    try:
        from ib_insync import IB
    except ModuleNotFoundError:
        print("ERROR: missing dependency ib_insync. Install with: pip install ib_insync", file=sys.stderr)
        return 2

    ib = IB()
    try:
        setattr(ib, "RequestTimeout", float(args.timeout))
    except Exception:
        pass

    try:
        ib.connect(
            host=args.host,
            port=int(args.port),
            clientId=int(args.client_id),
            timeout=float(args.timeout),
            readonly=True,
        )
        contract = _resolve_contract_with_ib(
            ib,
            code=str(args.code),
            market=market,
            contract_month=contract_month,
        )
        schedule = ib.reqHistoricalSchedule(
            contract=contract,
            numDays=2,
            endDateTime=end_dt_local,
            useRTH=use_rth,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: failed to fetch trading calendar: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    schedule_tz_name = str(getattr(schedule, "timeZone", "") or "America/New_York")
    try:
        schedule_tz = ZoneInfo(schedule_tz_name)
    except Exception:
        schedule_tz = ET
        schedule_tz_name = "America/New_York"

    rows: list[dict[str, str]] = []
    for session in list(getattr(schedule, "sessions", []) or []):
        ref_date = str(getattr(session, "refDate", "")).strip()
        start_raw = str(getattr(session, "startDateTime", "")).strip()
        end_raw = str(getattr(session, "endDateTime", "")).strip()
        if not start_raw or not end_raw:
            continue
        start_exchange = _parse_schedule_dt(start_raw, fallback_tz=schedule_tz)
        end_exchange = _parse_schedule_dt(end_raw, fallback_tz=schedule_tz)
        start_local = start_exchange.astimezone(LOCAL_TZ)
        end_local = end_exchange.astimezone(LOCAL_TZ)
        # Keep only sessions touching local today/tomorrow.
        if (start_local.date() not in target_dates_local) and (end_local.date() not in target_dates_local):
            continue
        rows.append(
            {
                "ref_date": ref_date,
                "start_local": start_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "end_local": end_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "start_utc": _iso_utc(start_local),
                "end_utc": _iso_utc(end_local),
            }
        )

    rows.sort(key=lambda item: (item["ref_date"], item["start_utc"]))

    payload = {
        "code": str(args.code).strip().upper(),
        "market": market,
        "contract_month": contract_month,
        "use_rth": use_rth,
        "now_local": now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "local_timezone": str(LOCAL_TZ),
        "today_local": today_local.isoformat(),
        "tomorrow_local": tomorrow_local.isoformat(),
        "schedule_timezone": schedule_tz_name,
        "sessions": rows,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(
        "[INFO]",
        f"code={payload['code']}",
        f"market={payload['market']}",
        f"use_rth={payload['use_rth']}",
        f"now_local={payload['now_local']}",
        f"today_local={payload['today_local']}",
        f"tomorrow_local={payload['tomorrow_local']}",
        f"local_tz={payload['local_timezone']}",
        f"schedule_tz={payload['schedule_timezone']}",
    )
    if not rows:
        print("[INFO] no sessions returned for local today/tomorrow (weekend/holiday or symbol has no session)")
        return 0

    for row in rows:
        print(
            f"- ref_date={row['ref_date']} "
            f"start_local={row['start_local']} end_local={row['end_local']} "
            f"(UTC: {row['start_utc']} -> {row['end_utc']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
