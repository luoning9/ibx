#!/usr/bin/env python3
"""Submit BUY orders in paper mode for testing positions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_app_config, resolve_ib_client_id
from app.ib_session_manager import close_ib_session_manager, get_ib_session_manager
from app.ib_trade_service import IBOrderService


@dataclass
class OrderResult:
    symbol: str
    con_id: int | None
    order_id: int | None
    perm_id: int | None
    status: str
    filled: float
    remaining: float
    avg_fill_price: float | None


def parse_args() -> argparse.Namespace:
    cfg = load_app_config().ib_gateway
    parser = argparse.ArgumentParser(description="Buy paper positions for testing")
    parser.add_argument(
        "--symbols",
        default="AAPL,MSFT",
        help="Comma-separated symbols to buy (default: AAPL,MSFT)",
    )
    parser.add_argument(
        "--qty",
        type=float,
        default=1.0,
        help="Quantity per symbol (default: 1)",
    )
    parser.add_argument(
        "--order-type",
        choices=("MKT", "LMT"),
        default="MKT",
        help="Order type (default: MKT)",
    )
    parser.add_argument(
        "--limit-price",
        type=float,
        default=None,
        help="Limit price when --order-type=LMT",
    )
    parser.add_argument(
        "--account",
        default=os.getenv("IB_ACCOUNT_CODE", cfg.account_code),
        help="Account code override (optional)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("IB_HOST", cfg.host),
        help="IB host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("IB_API_PORT", str(cfg.paper_port))),
        help="IB API port (default: paper port from config)",
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
        help="IB connect timeout seconds",
    )
    parser.add_argument(
        "--wait-fill-seconds",
        type=float,
        default=8.0,
        help="Max seconds to wait per order for terminal status (default: 8)",
    )
    parser.add_argument(
        "--outside-rth",
        action="store_true",
        help="Allow fill outside regular trading hours",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only qualify contracts and print plan; do not place orders",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON output",
    )
    args = parser.parse_args()
    return args


def _parse_symbols(raw: str) -> list[str]:
    symbols: list[str] = []
    for part in str(raw).split(","):
        symbol = part.strip().upper()
        if not symbol:
            continue
        symbols.append(symbol)
    deduped: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        if symbol in seen:
            continue
        seen.add(symbol)
        deduped.append(symbol)
    return deduped


def _qualify_symbol(
    *,
    host: str,
    port: int,
    client_id: int,
    timeout_seconds: float,
    idle_ttl_seconds: float,
    symbol: str,
) -> int | None:
    session = get_ib_session_manager().get_session(
        host=host,
        port=port,
        client_id=client_id,
        timeout_seconds=timeout_seconds,
        readonly=True,
        idle_ttl_seconds=idle_ttl_seconds,
    )

    def _run(ib: Any) -> int | None:
        try:
            from ib_insync import Stock
        except ModuleNotFoundError as exc:
            raise RuntimeError("ib_insync is not installed") from exc
        contract = Stock(symbol=symbol, exchange="SMART", currency="USD")
        qualified = list(ib.qualifyContracts(contract))
        if not qualified:
            raise RuntimeError(f"failed to qualify contract: {symbol}")
        return int(getattr(qualified[0], "conId", 0) or 0) or None

    return session.run(_run)


def _build_trade_action(args: argparse.Namespace, *, symbol: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action_type": "STOCK_TRADE",
        "symbol": symbol,
        "side": "BUY",
        "order_type": str(args.order_type).upper(),
        "quantity": float(args.qty),
        "tif": "DAY",
        "allow_overnight": bool(args.outside_rth),
    }
    if payload["order_type"] == "LMT":
        payload["limit_price"] = args.limit_price
    return payload


def main() -> int:
    args = parse_args()
    cfg = load_app_config().ib_gateway
    symbols = _parse_symbols(args.symbols)
    if not symbols:
        print("[ERROR] no valid symbols", file=sys.stderr)
        return 2
    if args.qty <= 0:
        print("[ERROR] --qty must be > 0", file=sys.stderr)
        return 2
    live_port = int(cfg.live_port)
    if int(args.port) == live_port:
        print(
            f"[ERROR] safety guard: live port is forbidden for this script (port={live_port}).",
            file=sys.stderr,
        )
        return 2
    paper_port = int(cfg.paper_port)
    if int(args.port) != paper_port:
        print(
            f"[ERROR] safety guard: port={args.port} is not paper_port={paper_port}.",
            file=sys.stderr,
        )
        return 2

    order_service = IBOrderService(
        host=str(args.host),
        port=int(args.port),
        client_id=int(args.client_id),
        timeout_seconds=float(args.timeout),
        session_idle_ttl_seconds=float(cfg.session_idle_ttl_seconds),
        account_code=args.account,
        readonly=False,
    )

    try:
        results: list[OrderResult] = []
        for symbol in symbols:
            if args.dry_run:
                con_id = _qualify_symbol(
                    host=str(args.host),
                    port=int(args.port),
                    client_id=int(args.client_id),
                    timeout_seconds=float(args.timeout),
                    idle_ttl_seconds=float(cfg.session_idle_ttl_seconds),
                    symbol=symbol,
                )
                results.append(
                    OrderResult(
                        symbol=symbol,
                        con_id=con_id,
                        order_id=None,
                        perm_id=None,
                        status="DRY_RUN",
                        filled=0.0,
                        remaining=float(args.qty),
                        avg_fill_price=None,
                    )
                )
                continue
            trade_action = _build_trade_action(args, symbol=symbol)
            submit = order_service.submit_trade_action(
                market="US_STOCK",
                trade_action=trade_action,
                account_code=args.account,
                order_ref=f"PAPER-{symbol}",
            )
            con_id = submit.con_id
            status = str(submit.normalized_status or submit.status or "UNKNOWN")
            filled = float(submit.filled_qty)
            remaining = float(submit.remaining_qty)
            avg_fill_price = submit.avg_fill_price
            if submit.order_id is not None and float(args.wait_fill_seconds) > 0:
                snapshot = order_service.wait_for_terminal_status(
                    order_id=submit.order_id,
                    timeout_seconds=float(args.wait_fill_seconds),
                    poll_interval_seconds=0.5,
                )
                if snapshot is not None:
                    status = str(snapshot.normalized_status or snapshot.status or status)
                    filled = float(snapshot.filled_qty)
                    remaining = float(snapshot.remaining_qty)
                    avg_fill_price = snapshot.avg_fill_price
            results.append(
                OrderResult(
                    symbol=symbol,
                    con_id=con_id,
                    order_id=submit.order_id,
                    perm_id=submit.perm_id,
                    status=status,
                    filled=filled,
                    remaining=remaining,
                    avg_fill_price=avg_fill_price,
                )
            )
    except Exception as exc:  # noqa: BLE001
        if "ib_insync is not installed" in str(exc):
            print(
                "[ERROR] Missing dependency: ib_insync. Install with: pip install ib_insync",
                file=sys.stderr,
            )
            return 3
        print(f"[ERROR] submit failed: {exc}", file=sys.stderr)
        return 1
    finally:
        close_ib_session_manager()

    if args.json:
        print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    else:
        for row in results:
            print(
                f"[OK] {row.symbol} conId={row.con_id} status={row.status} "
                f"filled={row.filled} remaining={row.remaining} "
                f"avgFillPrice={row.avg_fill_price} orderId={row.order_id}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
