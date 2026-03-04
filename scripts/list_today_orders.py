#!/usr/bin/env python3
"""List intraday IB orders directly from IB Gateway/TWS."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import infer_ib_api_port, load_app_config, resolve_ib_client_id
from app.ib_compat import INSTALL_HINT, is_missing_ib_dependency_error
from app.ib_session_manager import close_ib_session_manager, get_ib_session_manager


@dataclass
class OrderRow:
    source: str
    account: str
    symbol: str
    sec_type: str
    side: str
    order_type: str
    quantity: float
    filled_qty: float
    avg_fill_price: float | None
    limit_price: float | None
    status: str
    order_id: int | None
    perm_id: int | None
    client_id: int | None
    timestamp: datetime | None


def parse_args() -> argparse.Namespace:
    cfg = load_app_config().ib_gateway
    parser = argparse.ArgumentParser(
        description="List intraday IB orders from reqCompletedOrders + reqAllOpenOrders"
    )
    parser.add_argument("--host", default=os.getenv("IB_HOST", cfg.host), help="IB host")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="IB API port (default: trading mode selected port)",
    )
    parser.add_argument(
        "--client-id",
        type=int,
        default=int(os.getenv("IB_CLIENT_ID", str(resolve_ib_client_id("cli")))),
        help="IB client id (default: cli client id)",
    )
    parser.add_argument(
        "--account",
        default=os.getenv("IB_ACCOUNT_CODE", cfg.account_code),
        help="Filter by account code (optional)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("IB_TIMEOUT", str(cfg.timeout_seconds))),
        help="Connect timeout seconds",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="For completed orders, only include API orders",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON instead of a table",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Force live mode connection (uses live port when --port is not set)",
    )

    args = parser.parse_args()
    if args.port is None:
        if os.getenv("IB_API_PORT"):
            args.port = int(os.getenv("IB_API_PORT", "0"))
        else:
            mode = "live" if bool(args.live) else os.getenv("TRADING_MODE", cfg.trading_mode).strip().lower()
            args.port = infer_ib_api_port(mode)
    return args


def _to_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _to_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _normalize_limit_price(value: Any) -> float | None:
    parsed = _to_float_or_none(value)
    if parsed is None or parsed <= 0 or parsed >= 1e20:
        return None
    return parsed


def _normalize_side(value: Any) -> str:
    side = str(value or "").strip().upper()
    if side == "BOT":
        return "BUY"
    if side == "SLD":
        return "SELL"
    return side


def _normalize_status(raw_status: str, filled_qty: float, remaining_qty: float) -> str:
    status = str(raw_status or "").strip().upper()
    if status == "FILLED":
        return "FILLED"
    if status in {"CANCELLED", "APICANCELLED"}:
        return "CANCELLED"
    if status == "INACTIVE":
        return "FAILED"
    if status in {"PENDINGSUBMIT", "PRESUBMITTED", "SUBMITTED", "PENDINGCANCEL"}:
        if filled_qty > 0 and remaining_qty > 0:
            return "PARTIAL_FILL"
        return "ORDER_SUBMITTED"
    if filled_qty > 0 and remaining_qty <= 0:
        return "FILLED"
    if filled_qty > 0 and remaining_qty > 0:
        return "PARTIAL_FILL"
    return status or "UNKNOWN"


def _parse_ib_datetime(raw: Any) -> datetime | None:
    if raw is None:
        return None
    local_tz = datetime.now().astimezone().tzinfo
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=local_tz)

    text = str(raw).strip()
    if not text:
        return None
    normalized = text.replace("  ", " ").strip()

    for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d-%H:%M:%S"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=local_tz)
        except ValueError:
            pass

    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=local_tz)
    except ValueError:
        return None


def _extract_trade_timestamp(trade: Any) -> datetime | None:
    order_state = getattr(trade, "orderState", None)
    completed_time = _parse_ib_datetime(getattr(order_state, "completedTime", None))
    if completed_time is not None:
        return completed_time

    log_entries = getattr(trade, "log", None)
    if isinstance(log_entries, list):
        latest_log_time: datetime | None = None
        for entry in log_entries:
            ts = _parse_ib_datetime(getattr(entry, "time", None))
            if ts is None:
                continue
            if latest_log_time is None or ts > latest_log_time:
                latest_log_time = ts
        if latest_log_time is not None:
            return latest_log_time

    fills = getattr(trade, "fills", None)
    if isinstance(fills, list):
        latest_fill_time: datetime | None = None
        for fill in fills:
            ts = _parse_ib_datetime(getattr(fill, "time", None))
            if ts is None:
                ts = _parse_ib_datetime(getattr(getattr(fill, "execution", None), "time", None))
            if ts is None:
                continue
            if latest_fill_time is None or ts > latest_fill_time:
                latest_fill_time = ts
        if latest_fill_time is not None:
            return latest_fill_time

    return None


def _build_identity(*, order_id: int | None, perm_id: int | None, client_id: int | None) -> tuple[str, int, int] | None:
    if perm_id is not None:
        return ("perm", int(perm_id), 0)
    if order_id is not None:
        return ("order", int(client_id or 0), int(order_id))
    return None


def _trade_to_row(trade: Any, source: str) -> OrderRow:
    contract = getattr(trade, "contract", None)
    order = getattr(trade, "order", None)
    status_obj = getattr(trade, "orderStatus", None)
    order_state = getattr(trade, "orderState", None)

    raw_status = str(getattr(status_obj, "status", "") or "").strip().upper()
    filled_qty = _to_float(getattr(status_obj, "filled", 0.0), default=0.0)
    remaining_qty = _to_float(getattr(status_obj, "remaining", 0.0), default=0.0)
    normalized_status = _normalize_status(raw_status, filled_qty, remaining_qty)
    completed_status = str(getattr(order_state, "completedStatus", "") or "").strip().upper()

    return OrderRow(
        source=source,
        account=str(getattr(order, "account", "") or "").strip(),
        symbol=str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or "").strip().upper(),
        sec_type=str(getattr(contract, "secType", "") or "").strip().upper(),
        side=_normalize_side(getattr(order, "action", "")),
        order_type=str(getattr(order, "orderType", "") or "").strip().upper(),
        quantity=_to_float(getattr(order, "totalQuantity", 0.0), default=0.0),
        filled_qty=filled_qty,
        avg_fill_price=_to_float_or_none(getattr(status_obj, "avgFillPrice", None)),
        limit_price=_normalize_limit_price(getattr(order, "lmtPrice", None)),
        status=completed_status or normalized_status,
        order_id=_to_int_or_none(getattr(order, "orderId", None)),
        perm_id=_to_int_or_none(getattr(order, "permId", None))
        or _to_int_or_none(getattr(status_obj, "permId", None)),
        client_id=_to_int_or_none(getattr(order, "clientId", None)),
        timestamp=_extract_trade_timestamp(trade),
    )


def fetch_orders(*, host: str, port: int, client_id: int, timeout: float, api_only: bool) -> list[OrderRow]:
    _ = (host, port, client_id, timeout)
    session = get_ib_session_manager().get_session(role="cli")

    def _query(ib: Any) -> list[OrderRow]:
        completed: list[Any] = []
        req_completed = getattr(ib, "reqCompletedOrders", None)
        if callable(req_completed):
            completed = list(req_completed(bool(api_only)))

        open_trades: list[Any] = []
        req_all_open = getattr(ib, "reqAllOpenOrders", None)
        if callable(req_all_open):
            open_trades = list(req_all_open())

        rows = [_trade_to_row(trade, "completed") for trade in completed]
        rows.extend(_trade_to_row(trade, "open") for trade in open_trades)

        deduped: dict[tuple[str, int, int], OrderRow] = {}
        for row in rows:
            key = _build_identity(order_id=row.order_id, perm_id=row.perm_id, client_id=row.client_id)
            if key is None:
                continue
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = row
                continue
            if existing.source == "completed" and row.source == "open":
                deduped[key] = row
                continue
            if existing.timestamp is None and row.timestamp is not None:
                deduped[key] = row
            elif existing.timestamp is not None and row.timestamp is not None and row.timestamp > existing.timestamp:
                deduped[key] = row

        return list(deduped.values())

    try:
        return session.run(_query)
    finally:
        close_ib_session_manager()


def filter_orders(rows: list[OrderRow], *, account_filter: str) -> list[OrderRow]:
    account = str(account_filter or "").strip()
    filtered: list[OrderRow] = []
    for row in rows:
        if account and row.account != account:
            continue
        filtered.append(row)

    local_tz = datetime.now().astimezone().tzinfo
    filtered.sort(
        key=lambda item: (
            item.timestamp or datetime.min.replace(tzinfo=local_tz),
            int(item.order_id or 0),
            int(item.perm_id or 0),
        ),
        reverse=True,
    )
    return filtered


def print_table(rows: list[OrderRow]) -> None:
    if not rows:
        print("[INFO] No IB intraday orders found.")
        return

    headers = [
        "Time",
        "Source",
        "Account",
        "Symbol",
        "Type",
        "Side",
        "OrdType",
        "Qty",
        "Filled",
        "AvgFill",
        "LmtPrice",
        "Status",
        "OrderId",
        "PermId",
        "ClientId",
    ]

    table_rows: list[list[str]] = []
    for row in rows:
        ts = row.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S") if row.timestamp else ""
        table_rows.append(
            [
                ts,
                row.source,
                row.account,
                row.symbol,
                row.sec_type,
                row.side,
                row.order_type,
                f"{row.quantity:.4f}",
                f"{row.filled_qty:.4f}",
                "" if row.avg_fill_price is None else f"{row.avg_fill_price:.4f}",
                "" if row.limit_price is None else f"{row.limit_price:.4f}",
                row.status,
                "" if row.order_id is None else str(row.order_id),
                "" if row.perm_id is None else str(row.perm_id),
                "" if row.client_id is None else str(row.client_id),
            ]
        )

    widths = [len(h) for h in headers]
    for cols in table_rows:
        for i, col in enumerate(cols):
            widths[i] = max(widths[i], len(col))

    def _fmt(cols: list[str]) -> str:
        return " | ".join(col.ljust(widths[i]) for i, col in enumerate(cols))

    print(_fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for cols in table_rows:
        print(_fmt(cols))
    print()
    print(f"[SUMMARY] total_orders={len(rows)}")


def main() -> int:
    args = parse_args()
    try:
        rows = fetch_orders(
            host=str(args.host),
            port=int(args.port),
            client_id=int(args.client_id),
            timeout=float(args.timeout),
            api_only=bool(args.api_only),
        )
    except Exception as exc:  # noqa: BLE001
        if is_missing_ib_dependency_error(exc):
            print(
                f"[ERROR] {INSTALL_HINT}",
                file=sys.stderr,
            )
            return 3
        print(f"[ERROR] Failed to query IB API: {exc}", file=sys.stderr)
        return 1

    filtered = filter_orders(rows, account_filter=str(args.account or ""))
    if args.json:
        payload = []
        for row in filtered:
            item = asdict(row)
            if row.timestamp is not None:
                item["timestamp"] = row.timestamp.astimezone().isoformat()
            payload.append(item)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_table(filtered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
