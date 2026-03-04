#!/usr/bin/env python3
"""List current IB portfolio positions.

Connects to IB Gateway/TWS using ib_async and prints portfolio holdings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import infer_ib_api_port, load_app_config, resolve_ib_client_id
from app.ib_compat import INSTALL_HINT, is_missing_ib_dependency_error
from app.ib_session_manager import close_ib_session_manager, get_ib_session_manager


@dataclass
class Holding:
    account: str
    symbol: str
    sec_type: str
    currency: str
    exchange: str
    position: float
    market_price: float
    market_value: float
    average_cost: float
    unrealized_pnl: float
    realized_pnl: float
    weight_pct: float = 0.0


def infer_default_port() -> int:
    cfg = load_app_config().ib_gateway
    mode = os.getenv("TRADING_MODE", cfg.trading_mode).strip().lower()
    return infer_ib_api_port(mode)


def parse_args() -> argparse.Namespace:
    cfg = load_app_config().ib_gateway
    parser = argparse.ArgumentParser(description="List current IB portfolio holdings")
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
        help="IB client id",
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


def build_holdings(items: list[Any], account_filter: str) -> list[Holding]:
    holdings: list[Holding] = []
    for item in items:
        if account_filter and item.account != account_filter:
            continue
        c = item.contract
        holdings.append(
            Holding(
                account=item.account,
                symbol=getattr(c, "localSymbol", "") or getattr(c, "symbol", ""),
                sec_type=getattr(c, "secType", ""),
                currency=getattr(c, "currency", ""),
                exchange=getattr(c, "exchange", ""),
                position=float(item.position),
                market_price=float(item.marketPrice),
                market_value=float(item.marketValue),
                average_cost=float(item.averageCost),
                unrealized_pnl=float(item.unrealizedPNL),
                realized_pnl=float(item.realizedPNL),
            )
        )

    total_mv = sum(h.market_value for h in holdings)
    if total_mv != 0:
        for h in holdings:
            h.weight_pct = h.market_value / total_mv * 100

    holdings.sort(key=lambda x: abs(x.market_value), reverse=True)
    return holdings


def print_table(holdings: list[Holding]) -> None:
    if not holdings:
        print("[INFO] No positions found.")
        return

    headers = [
        "Account",
        "Symbol",
        "Type",
        "Pos",
        "MktPrice",
        "MktValue",
        "AvgCost",
        "UnrealPNL",
        "RealPNL",
        "Weight%",
    ]

    rows = [
        [
            h.account,
            h.symbol,
            h.sec_type,
            f"{h.position:.4f}",
            f"{h.market_price:.4f}",
            f"{h.market_value:.2f}",
            f"{h.average_cost:.4f}",
            f"{h.unrealized_pnl:.2f}",
            f"{h.realized_pnl:.2f}",
            f"{h.weight_pct:.2f}",
        ]
        for h in holdings
    ]

    widths = [len(h) for h in headers]
    for row in rows:
        for i, col in enumerate(row):
            widths[i] = max(widths[i], len(col))

    def fmt_line(cols: list[str]) -> str:
        return " | ".join(col.ljust(widths[i]) for i, col in enumerate(cols))

    print(fmt_line(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt_line(row))

    total_mv = sum(h.market_value for h in holdings)
    total_unreal = sum(h.unrealized_pnl for h in holdings)
    total_real = sum(h.realized_pnl for h in holdings)
    print()
    print(
        f"[SUMMARY] positions={len(holdings)} total_market_value={total_mv:.2f} "
        f"unrealized_pnl={total_unreal:.2f} realized_pnl={total_real:.2f}"
    )


def main() -> int:
    args = parse_args()
    session = get_ib_session_manager().get_session(role="broker_data")

    try:
        raw_items = session.run(lambda ib: list(ib.portfolio()))
    except Exception as exc:  # noqa: BLE001
        if is_missing_ib_dependency_error(exc):
            print(f"[ERROR] {INSTALL_HINT}", file=sys.stderr)
            return 3
        print(f"[ERROR] Failed to query IB API: {exc}", file=sys.stderr)
        return 1
    finally:
        close_ib_session_manager()

    holdings = build_holdings(raw_items, args.account.strip())
    if args.json:
        print(json.dumps([asdict(h) for h in holdings], ensure_ascii=False, indent=2))
    else:
        print_table(holdings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
