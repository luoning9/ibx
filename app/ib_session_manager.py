from __future__ import annotations

import asyncio
import logging
import time
import threading
from concurrent.futures import Future
from queue import Empty, Queue
from threading import Lock, Thread
from typing import Any, Callable, Optional

# 确保依赖库存在
try:
    from ib_insync import IB, util
except ImportError:
    IB = None


class IBSessionError(RuntimeError):
    pass


_LOGGER = logging.getLogger("ibx.ib_session")


def _ensure_thread_event_loop() -> None:
    """确保当前线程有运行中的异步事件循环"""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


class IBClientSession:
    def __init__(
            self,
            *,
            host: str,
            port: int,
            client_id: int,
            timeout_seconds: float,
            readonly: bool,  # 严格保留 readonly 参数
            idle_ttl_seconds: float,
            ib_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)
        self.timeout_seconds = float(timeout_seconds)
        self.readonly = bool(readonly)
        self.idle_ttl_seconds = max(1.0, float(idle_ttl_seconds))
        self._ib_factory = ib_factory

        self._state_lock = Lock()
        self._closed = False
        self._command_queue: Queue[tuple[str, Any, Future[Any]]] = Queue()

        # 内部状态管理
        self._ib: Any | None = None
        self._in_flight = 0
        self._last_used_monotonic = time.monotonic()

        # 启动工作线程
        self._worker = Thread(
            target=self._worker_loop,
            name=f"ibx-ib-session-{self.host}:{self.port}:{self.client_id}",
            daemon=True,
        )
        self._worker.start()

    def _ensure_ib_worker(self) -> Any:
        """在工作线程内初始化 IB 实例"""
        if self._ib is not None:
            return self._ib

        if self._ib_factory is not None:
            ib = self._ib_factory()
        else:
            if IB is None:
                raise IBSessionError("ib_insync is not installed")
            ib = IB()

        self._ib = ib
        return ib

    def _connect_worker(self) -> Any:
        """执行实际连接，使用保留的 readonly 参数"""
        ib = self._ensure_ib_worker()
        if bool(getattr(ib, "isConnected", lambda: False)()):
            return ib
        try:
            # 这里的 readonly 参数被传递给 ib_insync
            ib.connect(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
                timeout=self.timeout_seconds,
                readonly=self.readonly,
            )
            return ib
        except Exception as exc:
            _LOGGER.exception("IB session connect failed")
            try:
                ib.disconnect()
            except:
                pass
            raise IBSessionError(f"Failed to connect IB gateway: {exc}") from exc

    def _worker_loop(self) -> None:
        """处理所有 IB 请求的后台循环"""
        _ensure_thread_event_loop()
        # 在 uvloop 下 patchAsyncio 可能抛 ValueError；这里仅做最佳努力，失败不影响工作线程。
        if "util" in globals():
            patch_asyncio = getattr(util, "patchAsyncio", None)
            if callable(patch_asyncio):
                try:
                    patch_asyncio()
                except Exception as exc:
                    _LOGGER.warning("skip patchAsyncio: %s", exc)

        while True:
            try:
                command, payload, fut = self._command_queue.get(timeout=0.5)
            except Empty:
                # 周期性检查闲置断开
                self._disconnect_if_idle_worker()
                continue

            try:
                if command == "run":
                    callback = payload
                    ib = self._connect_worker()
                    self._in_flight += 1
                    try:
                        result = callback(ib)
                        if not fut.done(): fut.set_result(result)
                    finally:
                        self._in_flight -= 1
                        self._last_used_monotonic = time.monotonic()

                elif command == "reap":
                    closed = self._disconnect_if_idle_worker(now_monotonic=payload)
                    if not fut.done(): fut.set_result(closed)

                elif command == "close" or command == "stop":
                    if self._ib: self._ib.disconnect()
                    if not fut.done(): fut.set_result(None)
                    if command == "stop": break  # 彻底退出线程
            except Exception as exc:
                if not fut.done(): fut.set_exception(exc)

    def _disconnect_if_idle_worker(self, *, now_monotonic: float | None = None) -> bool:
        """检查并执行断开逻辑"""
        now_value = time.monotonic() if now_monotonic is None else float(now_monotonic)
        if self._in_flight > 0 or self._ib is None:
            return False

        if not bool(getattr(self._ib, "isConnected", lambda: False)()):
            return False

        if (now_value - self._last_used_monotonic) >= self.idle_ttl_seconds:
            _LOGGER.info(f"Session {self.port} is idle, disconnecting (readonly={self.readonly})")
            self._ib.disconnect()
            return True
        return False

    def _submit(self, command: str, payload: Any = None) -> Future[Any]:
        with self._state_lock:
            if self._closed and command != "stop":
                raise IBSessionError("Session is closed")
            fut: Future[Any] = Future()
            self._command_queue.put((command, payload, fut))
            return fut

    # --- 保持不变的对外接口 ---

    def run(self, callback: Callable[[Any], Any]) -> Any:
        return self._submit("run", callback).result()

    def close_if_idle(self, *, now_monotonic: float | None = None) -> bool:
        return bool(self._submit("reap", now_monotonic).result())

    def force_close(self) -> None:
        try:
            self._submit("close").result()
        except IBSessionError:
            pass

    def stop(self) -> None:
        with self._state_lock:
            if self._closed: return
            self._closed = True
        try:
            self._submit("stop").result(timeout=2.0)
        except:
            pass
        self._worker.join(timeout=1.0)


class IBSessionManager:
    def __init__(
            self,
            *,
            sweep_interval_seconds: float = 1.0,
            ib_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._ib_factory = ib_factory
        self._lock = Lock()
        self._sessions: dict[tuple[str, int, int, bool], IBClientSession] = {}

    def get_session(
            self,
            *,
            host: str,
            port: int,
            client_id: int,
            timeout_seconds: float,
            readonly: bool,  # 保持不变
            idle_ttl_seconds: float,
    ) -> IBClientSession:
        # key 包含 readonly，确保不同模式的连接分开管理
        key = (str(host), int(port), int(client_id), bool(readonly))
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                existing.timeout_seconds = float(timeout_seconds)
                existing.idle_ttl_seconds = max(1.0, float(idle_ttl_seconds))
                return existing

            session = IBClientSession(
                host=str(host),
                port=int(port),
                client_id=int(client_id),
                timeout_seconds=float(timeout_seconds),
                readonly=bool(readonly),
                idle_ttl_seconds=float(idle_ttl_seconds),
                ib_factory=self._ib_factory,
            )
            self._sessions[key] = session
            return session

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stop()

    def reap_once(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        now = time.monotonic()
        for session in sessions:
            session.close_if_idle(now_monotonic=now)


# 全局管理辅助函数
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
