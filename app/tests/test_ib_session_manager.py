from __future__ import annotations

from threading import Event, Lock, Thread, get_ident
import time

import pytest

from app.ib_session_manager import IBSessionManager


class _FakeIB:
    def __init__(self) -> None:
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0

    def isConnected(self) -> bool:
        return self.connected

    def connect(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        _ = kwargs
        self.connected = True
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.connected = False
        self.disconnect_calls += 1


class _ThreadBoundFakeIB:
    """Simulate an IB client that becomes unstable if disconnected from another thread."""

    def __init__(self) -> None:
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.connect_thread_id: int | None = None
        self.poisoned = False

    def isConnected(self) -> bool:
        return self.connected

    def connect(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        _ = kwargs
        if self.poisoned:
            raise TimeoutError("simulated reconnect timeout after cross-thread disconnect")
        current = get_ident()
        if self.connect_thread_id is None:
            self.connect_thread_id = current
        self.connected = True
        self.connect_calls += 1

    def disconnect(self) -> None:
        current = get_ident()
        if self.connect_thread_id is not None and current != self.connect_thread_id:
            self.poisoned = True
        self.connected = False
        self.disconnect_calls += 1


class _LegacyThreadSplitSession:
    """Old model: connect/request in caller thread, idle disconnect in reaper thread."""

    def __init__(self, ib: _ThreadBoundFakeIB, *, idle_ttl_seconds: float, sweep_interval_seconds: float) -> None:
        self._ib = ib
        self._idle_ttl_seconds = float(idle_ttl_seconds)
        self._sweep_interval_seconds = float(sweep_interval_seconds)
        self._lock = Lock()
        self._in_flight = 0
        self._last_used_monotonic = time.monotonic()
        self._stop_event = Event()
        self._reaper = Thread(target=self._reaper_loop, daemon=True)
        self._reaper.start()

    def _connect_locked(self) -> _ThreadBoundFakeIB:
        if self._ib.isConnected():
            return self._ib
        self._ib.connect(host="127.0.0.1", port=4001, clientId=99, timeout=5, readonly=True)
        return self._ib

    def run(self, callback):  # type: ignore[no-untyped-def]
        with self._lock:
            ib = self._connect_locked()
            self._in_flight += 1
            try:
                return callback(ib)
            finally:
                self._in_flight -= 1
                self._last_used_monotonic = time.monotonic()

    def close_if_idle(self) -> bool:
        with self._lock:
            if self._in_flight > 0:
                return False
            if not self._ib.isConnected():
                return False
            if time.monotonic() - self._last_used_monotonic < self._idle_ttl_seconds:
                return False
            self._ib.disconnect()
            return True

    def _reaper_loop(self) -> None:
        while not self._stop_event.wait(self._sweep_interval_seconds):
            self.close_if_idle()

    def stop(self) -> None:
        self._stop_event.set()
        self._reaper.join(timeout=1.0)


def test_legacy_thread_split_disconnect_can_break_reconnect() -> None:
    """Reproduce the old bug shape: idle disconnect on another thread can poison reconnect."""
    fake = _ThreadBoundFakeIB()
    session = _LegacyThreadSplitSession(fake, idle_ttl_seconds=0.2, sweep_interval_seconds=0.05)
    try:
        session.run(lambda ib: bool(ib.isConnected()))
        assert fake.connect_calls == 1

        deadline = time.time() + 2.0
        while fake.disconnect_calls == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert fake.disconnect_calls > 0
        assert fake.poisoned is True

        with pytest.raises(TimeoutError):
            session.run(lambda ib: bool(ib.isConnected()))
    finally:
        session.stop()


def test_session_manager_reuses_connection_before_idle_ttl() -> None:
    fake = _FakeIB()
    manager = IBSessionManager(ib_factory=lambda: fake, sweep_interval_seconds=0.5)
    try:
        session = manager.get_session(
            host="127.0.0.1",
            port=4002,
            client_id=99,
            timeout_seconds=5.0,
            readonly=True,
            idle_ttl_seconds=2.0,
        )
        first = session.run(lambda ib: bool(ib.isConnected()))
        second = session.run(lambda ib: bool(ib.isConnected()))
        assert first is True
        assert second is True
        assert fake.connect_calls == 1
    finally:
        manager.close_all()


def test_session_manager_closes_idle_connection_and_reconnects_on_next_request() -> None:
    fake = _FakeIB()
    manager = IBSessionManager(ib_factory=lambda: fake, sweep_interval_seconds=0.5)
    try:
        session = manager.get_session(
            host="127.0.0.1",
            port=4002,
            client_id=99,
            timeout_seconds=5.0,
            readonly=True,
            idle_ttl_seconds=1.0,
        )
        session.run(lambda ib: bool(ib.isConnected()))
        assert fake.connect_calls == 1
        assert fake.disconnect_calls == 0

        time.sleep(1.1)
        manager.reap_once()
        assert fake.disconnect_calls == 1

        session.run(lambda ib: bool(ib.isConnected()))
        assert fake.connect_calls == 2
    finally:
        manager.close_all()
