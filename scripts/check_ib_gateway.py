#!/usr/bin/env python3
"""IB Gateway health check.

Checks:
1) TCP reachability for one or more ports.
2) Optional minimal IB API handshake on a target port.
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import infer_ib_api_port, load_app_config


@dataclass
class CheckResult:
    ok: bool
    message: str


def parse_ports(raw: str) -> list[int]:
    ports: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            port = int(part)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Invalid port: {part}") from exc
        if port < 1 or port > 65535:
            raise argparse.ArgumentTypeError(f"Port out of range: {port}")
        ports.append(port)
    if not ports:
        raise argparse.ArgumentTypeError("At least one port is required")
    return ports


def check_tcp(host: str, port: int, timeout: float) -> CheckResult:
    start = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency_ms = (time.time() - start) * 1000.0
            return CheckResult(True, f"TCP connect ok ({latency_ms:.1f} ms)")
    except OSError as exc:
        return CheckResult(False, f"TCP connect failed: {exc}")


def read_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("socket closed by peer")
        buf.extend(chunk)
    return bytes(buf)


def read_frame(sock: socket.socket) -> bytes:
    header = read_exact(sock, 4)
    (length,) = struct.unpack(">I", header)
    if length == 0:
        return b""
    return read_exact(sock, length)


def check_ib_handshake(host: str, port: int, timeout: float) -> CheckResult:
    # Handshake negotiated as: "API\\0" + 4-byte-len-prefixed "v<min>..<max>"
    min_client_version = 157
    max_client_version = 178
    payload = f"v{min_client_version}..{max_client_version}".encode("ascii")
    framed = struct.pack(">I", len(payload)) + payload

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"API\0" + framed)
            server_reply = read_frame(sock)
            if not server_reply:
                return CheckResult(False, "API handshake failed: empty reply")

            # Expected format resembles: "<serverVersion>\\0<connectionTime>\\0"
            parts = server_reply.split(b"\0")
            version = parts[0].decode("ascii", errors="ignore").strip() if parts else ""
            if version.isdigit():
                return CheckResult(True, f"API handshake ok (serverVersion={version})")
            return CheckResult(True, "API handshake ok (received non-empty reply)")
    except Exception as exc:  # noqa: BLE001 - show root cause for ops debugging
        return CheckResult(False, f"API handshake failed: {exc}")


def fmt_ports(ports: Iterable[int]) -> str:
    return ",".join(str(p) for p in ports)


def main() -> int:
    cfg = load_app_config().ib_gateway
    trading_mode = os.getenv("TRADING_MODE", cfg.trading_mode).strip().lower()
    default_api_port = infer_ib_api_port(trading_mode)
    default_ports = str(default_api_port)

    parser = argparse.ArgumentParser(description="Check IB Gateway TCP/API health")
    parser.add_argument(
        "--host",
        default=os.getenv("IB_HOST", cfg.host),
        help="Gateway host (default: IB_HOST env or 127.0.0.1)",
    )
    parser.add_argument(
        "--ports",
        type=parse_ports,
        default=parse_ports(os.getenv("IB_PORTS", default_ports)),
        help="Comma-separated TCP ports to probe (default: TRADING_MODE selected port)",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=int(os.getenv("IB_API_PORT", str(default_api_port))),
        help="Port used for IB API handshake check (default: TRADING_MODE selected port)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("IB_TIMEOUT", str(cfg.timeout_seconds))),
        help="Socket timeout seconds (default: 3)",
    )
    parser.add_argument(
        "--skip-api",
        action="store_true",
        help="Skip IB API handshake check and only test TCP reachability",
    )
    args = parser.parse_args()

    print(f"[INFO] host={args.host} ports={fmt_ports(args.ports)} timeout={args.timeout}s")
    any_tcp_ok = False
    for port in args.ports:
        result = check_tcp(args.host, port, args.timeout)
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] tcp:{port} {result.message}")
        any_tcp_ok = any_tcp_ok or result.ok

    if not any_tcp_ok:
        print("[FAIL] No reachable IB Gateway TCP ports.")
        return 1

    if args.skip_api:
        print("[PASS] TCP reachability check passed (API handshake skipped).")
        return 0

    api_result = check_ib_handshake(args.host, args.api_port, args.timeout)
    api_status = "PASS" if api_result.ok else "FAIL"
    print(f"[{api_status}] api:{args.api_port} {api_result.message}")
    return 0 if api_result.ok else 2


if __name__ == "__main__":
    sys.exit(main())
