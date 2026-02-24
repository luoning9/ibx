#!/usr/bin/env python3
"""Real IB TTL reconnect check with a simple IB API call."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import infer_ib_api_port, load_app_config, resolve_ib_client_id
from app.ib_session_manager import close_ib_session_manager, get_ib_session_manager

DEFAULT_TEST_TTL_SECONDS = 5.0
DEFAULT_WAIT_EXTRA_SECONDS = 1.0


@dataclass(frozen=True)
class TimeProbeResult:
    name: str
    elapsed_seconds: float
    server_time: str


def _probe_ib_current_time(
    *,
    name: str,
    host: str,
    port: int,
    client_id: int,
    timeout_seconds: float,
    readonly: bool,
    idle_ttl_seconds: float,
) -> TimeProbeResult:
    manager = get_ib_session_manager()
    session = manager.get_session(
        host=host,
        port=port,
        client_id=client_id,
        timeout_seconds=timeout_seconds,
        readonly=readonly,
        idle_ttl_seconds=idle_ttl_seconds,
    )
    start = time.perf_counter()
    server_time_obj = session.run(lambda ib: ib.reqCurrentTime())
    elapsed = time.perf_counter() - start
    if hasattr(server_time_obj, "isoformat"):
        server_time = str(server_time_obj.isoformat())
    else:
        server_time = str(server_time_obj)
    return TimeProbeResult(name=name, elapsed_seconds=elapsed, server_time=server_time)


def _print_time_probe(result: TimeProbeResult) -> None:
    print(
        "[PASS] "
        f"{result.name}: elapsed={result.elapsed_seconds:.3f}s "
        f"server_time={result.server_time}"
    )


def parse_args() -> argparse.Namespace:
    cfg = load_app_config().ib_gateway
    parser = argparse.ArgumentParser(description="Test real IB reconnect after idle TTL")
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
        default=int(os.getenv("IB_CLIENT_ID", str(resolve_ib_client_id("broker_data")))),
        help="IB client id",
    )
    parser.add_argument(
        "--trading-mode",
        choices=("paper", "live"),
        default=os.getenv("TRADING_MODE", cfg.trading_mode).strip().lower(),
        help="Trading mode used when --port is not provided",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("IB_TIMEOUT", str(cfg.timeout_seconds))),
        help="Connect/request timeout seconds",
    )
    parser.add_argument(
        "--ttl",
        type=float,
        default=DEFAULT_TEST_TTL_SECONDS,
        help="Session idle TTL seconds (default: 5)",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=None,
        help="Wait seconds between probe #1 and #2 (default: ttl + 1)",
    )
    parser.add_argument(
        "--no-reap",
        action="store_true",
        help="Do not force manager.reap_once() after waiting",
    )
    parser.add_argument(
        "--readwrite",
        action="store_true",
        help="Use readonly=False when connecting",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    wait_seconds = float(args.wait if args.wait is not None else args.ttl + DEFAULT_WAIT_EXTRA_SECONDS)
    if args.ttl <= 0:
        print("[FAIL] --ttl must be > 0", file=sys.stderr)
        return 2
    if wait_seconds <= 0:
        print("[FAIL] --wait must be > 0", file=sys.stderr)
        return 2
    if wait_seconds <= args.ttl:
        print("[FAIL] --wait must be > --ttl for reconnect test", file=sys.stderr)
        return 2

    if args.port is None:
        args.port = infer_ib_api_port(args.trading_mode)

    readonly = not bool(args.readwrite)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    print(
        "[INFO] "
        f"host={args.host} port={args.port} client_id={args.client_id} "
        f"readonly={readonly} ttl={args.ttl}s wait={wait_seconds}s"
    )

    try:
        time_first = _probe_ib_current_time(
            name="ib_time#1",
            host=args.host,
            port=int(args.port),
            client_id=int(args.client_id),
            timeout_seconds=float(args.timeout),
            readonly=readonly,
            idle_ttl_seconds=float(args.ttl),
        )
        _print_time_probe(time_first)

        print(f"[INFO] sleep {wait_seconds:.1f}s to exceed idle TTL...")
        time.sleep(wait_seconds)

        if not args.no_reap:
            print("[INFO] manager.reap_once()")
            get_ib_session_manager().reap_once()

        time_second = _probe_ib_current_time(
            name="ib_time#2",
            host=args.host,
            port=int(args.port),
            client_id=int(args.client_id),
            timeout_seconds=float(args.timeout),
            readonly=readonly,
            idle_ttl_seconds=float(args.ttl),
        )
        _print_time_probe(time_second)
        print("[PASS] reconnect after idle TTL succeeded.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        close_ib_session_manager()


if __name__ == "__main__":
    raise SystemExit(main())
