#!/usr/bin/env python3
"""Real IB session persistence check with a simple IB API call."""

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

from app.config import load_app_config
from app.ib_session_manager import close_ib_session_manager, get_ib_session_manager

DEFAULT_WAIT_SECONDS = 6.0


@dataclass(frozen=True)
class TimeProbeResult:
    name: str
    elapsed_seconds: float
    server_time: str


def _probe_ib_current_time(
    *,
    name: str,
) -> TimeProbeResult:
    manager = get_ib_session_manager()
    session = manager.get_session(role="cli")
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
    _ = load_app_config()
    parser = argparse.ArgumentParser(description="Test real IB session persistence/reconnect")
    parser.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_WAIT_SECONDS,
        help="Wait seconds between probe #1 and #2",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    wait_seconds = float(args.wait)
    if wait_seconds <= 0:
        print("[FAIL] --wait must be > 0", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    print(f"[INFO] role=cli wait={wait_seconds}s")

    try:
        time_first = _probe_ib_current_time(
            name="ib_time#1",
        )
        _print_time_probe(time_first)

        print(f"[INFO] sleep {wait_seconds:.1f}s before second probe...")
        time.sleep(wait_seconds)

        time_second = _probe_ib_current_time(
            name="ib_time#2",
        )
        _print_time_probe(time_second)
        print("[PASS] session probe succeeded.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        close_ib_session_manager()


if __name__ == "__main__":
    raise SystemExit(main())
