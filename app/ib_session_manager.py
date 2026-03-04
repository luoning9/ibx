from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future
from dataclasses import dataclass
from queue import Queue
from threading import Lock, Thread
from typing import Any, Callable

from .config import (
    load_app_config,
    resolve_ib_client_id,
    resolve_ib_role_port,
    resolve_ib_role_readonly,
)
from .ib_compat import require_ib_attr


class IBSessionError(RuntimeError):
    pass


_LOGGER = logging.getLogger("ibx.ib_session")


def _ensure_thread_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except Exception:
        asyncio.set_event_loop(asyncio.new_event_loop())


@dataclass(frozen=True)
class _SessionKey:
    client_id: int


@dataclass
class _SessionState:
    role: str
    host: str
    port: int
    readonly: bool
    timeout_seconds: float
    ib: Any | None = None
    connected_host: str | None = None
    connected_port: int | None = None
    connected_readonly: bool | None = None


class IBClientSession:
    def __init__(
        self,
        *,
        manager: "IBSessionManager",
        key: _SessionKey,
        role: str,
        host: str,
        port: int,
        readonly: bool,
        timeout_seconds: float,
    ) -> None:
        self._manager = manager
        self._key = key
        self._lock = Lock()
        self.role = str(role).strip().lower() or "broker_data"
        self.host = str(host)
        self.port = int(port)
        self.readonly = bool(readonly)
        self.timeout_seconds = float(timeout_seconds)

    def update_runtime_params(
        self,
        *,
        role: str,
        host: str,
        port: int,
        readonly: bool,
        timeout_seconds: float,
    ) -> None:
        with self._lock:
            self.role = str(role).strip().lower() or "broker_data"
            self.host = str(host)
            self.port = int(port)
            self.readonly = bool(readonly)
            self.timeout_seconds = float(timeout_seconds)

    def _snapshot_runtime_params(self) -> tuple[str, str, int, bool, float]:
        with self._lock:
            return (
                str(self.role),
                str(self.host),
                int(self.port),
                bool(self.readonly),
                float(self.timeout_seconds),
            )

    def run(self, callback: Callable[[Any], Any]) -> Any:
        role, host, port, readonly, timeout_seconds = self._snapshot_runtime_params()
        return self._manager._run_session(  # noqa: SLF001
            key=self._key,
            role=role,
            host=host,
            port=port,
            readonly=readonly,
            callback=callback,
            timeout_seconds=timeout_seconds,
        )

    # Keep API compatibility; idle-reap is intentionally disabled.
    def close_if_idle(self, *, now_monotonic: float | None = None) -> bool:
        _ = now_monotonic
        return False

    def force_close(self) -> None:
        self._manager._close_session(self._key)  # noqa: SLF001

    def stop(self) -> None:
        self.force_close()


class IBSessionManager:
    def __init__(
        self,
        *,
        sweep_interval_seconds: float = 1.0,
        ib_factory: Callable[[], Any] | None = None,
    ) -> None:
        _ = sweep_interval_seconds
        self._ib_factory = ib_factory
        self._lock = Lock()
        self._closed = False
        self._sessions: dict[_SessionKey, IBClientSession] = {}
        self._command_queue: Queue[tuple[str, Any, Future[Any]]] = Queue()
        self._worker = Thread(target=self._worker_loop, name="ibx-ib-broker", daemon=True)
        self._worker.start()

    def get_session(
        self,
        *,
        role: str,
    ) -> IBClientSession:
        normalized_role = str(role or "").strip().lower()
        if normalized_role not in {"broker_data", "market_data", "order", "cli"}:
            normalized_role = "broker_data"

        cfg = load_app_config().ib_gateway
        host = str(cfg.host)
        client_id = int(resolve_ib_client_id(normalized_role))
        port = int(resolve_ib_role_port(normalized_role))
        readonly = bool(resolve_ib_role_readonly(normalized_role))
        timeout_seconds = float(cfg.timeout_seconds)

        key = _SessionKey(client_id=client_id)
        with self._lock:
            if self._closed:
                raise IBSessionError("Session manager is closed")
            existing = self._sessions.get(key)
            if existing is not None:
                existing.update_runtime_params(
                    role=normalized_role,
                    host=host,
                    port=port,
                    readonly=readonly,
                    timeout_seconds=timeout_seconds,
                )
                return existing

            session = IBClientSession(
                manager=self,
                key=key,
                role=normalized_role,
                host=host,
                port=port,
                readonly=readonly,
                timeout_seconds=timeout_seconds,
            )
            self._sessions[key] = session
            return session

    def _submit(self, command: str, payload: Any = None) -> Future[Any]:
        with self._lock:
            if self._closed and command != "stop":
                raise IBSessionError("Session manager is closed")
            fut: Future[Any] = Future()
            self._command_queue.put((command, payload, fut))
            return fut

    def _build_ib_worker(self) -> Any:
        if self._ib_factory is not None:
            return self._ib_factory()
        try:
            return require_ib_attr("IB")()
        except ModuleNotFoundError as exc:
            raise IBSessionError("ib_async is not installed") from exc

    def _connect_worker(self, *, key: _SessionKey, state: _SessionState) -> Any:
        ib = state.ib
        if ib is None:
            ib = self._build_ib_worker()
            state.ib = ib

        if bool(getattr(ib, "isConnected", lambda: False)()):
            if (
                state.connected_host == state.host
                and state.connected_port == state.port
                and state.connected_readonly == state.readonly
            ):
                return ib
            try:
                ib.disconnect()
            except Exception:
                pass

        try:
            ib.connect(
                host=state.host,
                port=state.port,
                clientId=key.client_id,
                timeout=state.timeout_seconds,
                readonly=state.readonly,
            )
            state.connected_host = state.host
            state.connected_port = state.port
            state.connected_readonly = state.readonly
            return ib
        except Exception as exc:
            _LOGGER.exception("IB session connect failed")
            try:
                ib.disconnect()
            except Exception:
                pass
            state.connected_host = None
            state.connected_port = None
            state.connected_readonly = None
            raise IBSessionError(f"Failed to connect IB gateway: {exc}") from exc

    def _disconnect_state_worker(self, *, key: _SessionKey, state: _SessionState) -> None:
        ib = state.ib
        if ib is None:
            return
        try:
            ib.disconnect()
        except Exception:
            _LOGGER.debug(
                "IB session disconnect failed role=%s host=%s port=%s client_id=%s readonly=%s",
                state.role,
                state.host,
                state.port,
                key.client_id,
                state.readonly,
                exc_info=True,
            )
        finally:
            state.connected_host = None
            state.connected_port = None
            state.connected_readonly = None

    def _worker_loop(self) -> None:
        _ensure_thread_event_loop()
        states: dict[_SessionKey, _SessionState] = {}

        while True:
            command, payload, fut = self._command_queue.get()
            try:
                if command == "run":
                    key, role, host, port, readonly, callback, timeout_seconds = payload
                    state = states.get(key)
                    if state is None:
                        state = _SessionState(
                            role=str(role),
                            host=str(host),
                            port=int(port),
                            readonly=bool(readonly),
                            timeout_seconds=float(timeout_seconds),
                        )
                        states[key] = state
                    else:
                        state.role = str(role)
                        state.host = str(host)
                        state.port = int(port)
                        state.readonly = bool(readonly)
                        state.timeout_seconds = float(timeout_seconds)

                    ib = self._connect_worker(key=key, state=state)
                    result = callback(ib)
                    if not fut.done():
                        fut.set_result(result)

                elif command == "close":
                    key = payload
                    state = states.get(key)
                    if state is not None:
                        self._disconnect_state_worker(key=key, state=state)
                    if not fut.done():
                        fut.set_result(None)

                elif command == "stop":
                    for key, state in states.items():
                        self._disconnect_state_worker(key=key, state=state)
                    states.clear()
                    if not fut.done():
                        fut.set_result(None)
                    break

                else:
                    raise IBSessionError(f"unsupported broker command: {command}")
            except Exception as exc:
                if not fut.done():
                    fut.set_exception(exc)

    def _run_session(
        self,
        *,
        key: _SessionKey,
        role: str,
        host: str,
        port: int,
        readonly: bool,
        callback: Callable[[Any], Any],
        timeout_seconds: float,
    ) -> Any:
        return self._submit(
            "run",
            (
                key,
                str(role),
                str(host),
                int(port),
                bool(readonly),
                callback,
                float(timeout_seconds),
            ),
        ).result()

    def _close_session(self, key: _SessionKey) -> None:
        try:
            self._submit("close", key).result()
        except IBSessionError:
            pass

    # Keep API compatibility; idle reaping is intentionally disabled.
    def reap_once(self) -> None:
        return None

    def close_all(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._sessions.clear()
            fut: Future[Any] = Future()
            self._command_queue.put(("stop", None, fut))
        try:
            fut.result(timeout=5.0)
        except Exception:
            pass
        self._worker.join(timeout=2.0)


_MANAGER_LOCK = Lock()
_MANAGER: IBSessionManager | None = None


def get_ib_session_manager() -> IBSessionManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = IBSessionManager()
        return _MANAGER


def close_ib_session_manager() -> None:
    global _MANAGER
    with _MANAGER_LOCK:
        manager = _MANAGER
        _MANAGER = None
    if manager is not None:
        manager.close_all()
