#!/usr/bin/env python3
"""Check IB trade service by listing current active orders."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import infer_ib_api_port, load_app_config, resolve_ib_client_id
from app.ib_trade_service import ActiveOrderSnapshot, IBOrderService
from app.ib_session_manager import close_ib_session_manager


def parse_args() -> argparse.Namespace:
    cfg = load_app_config().ib_gateway
    parser = argparse.ArgumentParser(description="Check IB trade service and list active IB orders")
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


def filter_rows(rows: list[ActiveOrderSnapshot], account_filter: str) -> list[ActiveOrderSnapshot]:
    account = str(account_filter or "").strip()
    if not account:
        return rows
    filtered = [row for row in rows if str(row.account_code or "").strip() == account]
    return filtered


def print_table(rows: list[ActiveOrderSnapshot]) -> None:
    if not rows:
        print("[INFO] No active orders.")
        return

    headers = [
        "Account",
        "Symbol",
        "Type",
        "Side",
        "OrdType",
        "Qty",
        "Filled",
        "Remain",
        "Status",
        "OrderId",
        "PermId",
        "ConId",
        "LmtPrice",
        "AvgFill",
    ]
    data_rows = [
        [
            str(row.account_code or ""),
            row.symbol,
            row.sec_type,
            row.side,
            row.order_type,
            f"{row.quantity:.4f}",
            f"{row.filled_qty:.4f}",
            f"{row.remaining_qty:.4f}",
            row.normalized_status or row.status,
            str(row.order_id or ""),
            str(row.perm_id or ""),
            str(row.con_id or ""),
            f"{row.limit_price:.4f}" if row.limit_price is not None else "",
            f"{row.avg_fill_price:.4f}" if row.avg_fill_price is not None else "",
        ]
        for row in rows
    ]
    widths = [len(h) for h in headers]
    for r in data_rows:
        for i, col in enumerate(r):
            widths[i] = max(widths[i], len(col))

    def _fmt(cols: list[str]) -> str:
        return " | ".join(col.ljust(widths[i]) for i, col in enumerate(cols))

    print(_fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for row in data_rows:
        print(_fmt(row))
    print()
    print(f"[SUMMARY] active_orders={len(rows)}")


def main() -> int:
    args = parse_args()
    cfg = load_app_config().ib_gateway
    service = IBOrderService(
        host=str(args.host),
        port=int(args.port),
        client_id=int(args.client_id),
        timeout_seconds=float(args.timeout),
        session_idle_ttl_seconds=float(cfg.session_idle_ttl_seconds),
        readonly=True,
    )

    try:
        rows = service.list_active_orders()
    except Exception as exc:  # noqa: BLE001
        if "ib_insync is not installed" in str(exc):
            print(
                "[ERROR] Missing dependency: ib_insync. Install with: pip install ib_insync",
                file=sys.stderr,
            )
            return 3
        print(f"[ERROR] Failed to query active orders: {exc}", file=sys.stderr)
        return 1
    finally:
        close_ib_session_manager()

    filtered = filter_rows(rows, args.account)
    if args.json:
        print(json.dumps([asdict(row) for row in filtered], ensure_ascii=False, indent=2, default=str))
    else:
        print_table(filtered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
