"""Bridges background threads (the REST/MCP API servers) into the Qt main
thread, where DownloadPanel/TorrentPanel/StatsPanel and everything they own
actually live.

PyQt objects are not safe to call directly from an arbitrary thread. The
idiomatic Qt answer is QMetaObject.invokeMethod with a blocking queued
connection, but that requires C++-style return-type marshaling that's
awkward for arbitrary Python objects (dicts, lists, exceptions). Instead,
this uses a plain thread-safe queue: background threads submit a callable
and block on a threading.Event; a QTimer on the main thread drains the
queue and runs each callable directly (safe, since by construction it's
now executing on the same thread the GUI objects live on), then unblocks
the waiting caller with the result or a re-raised exception.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from PyQt6.QtCore import QObject, QTimer

DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass
class _PendingCall:
    func: Callable
    args: tuple
    kwargs: dict
    done_event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException = None
    has_error: bool = False


class GuiBridge(QObject):
    def __init__(self, poll_interval_ms: int = 15, parent=None):
        super().__init__(parent)
        self._queue: "queue.Queue[_PendingCall]" = queue.Queue()
        self._timer = QTimer(self)
        self._timer.setInterval(poll_interval_ms)
        self._timer.timeout.connect(self._drain)
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _drain(self):
        while True:
            try:
                pending = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                pending.result = pending.func(*pending.args, **pending.kwargs)
            except BaseException as e:  # noqa: BLE001 -- must re-raise verbatim on the calling thread
                pending.error = e
                pending.has_error = True
            pending.done_event.set()

    def call(self, func: Callable, *args, timeout: float = DEFAULT_TIMEOUT_SECONDS, **kwargs) -> Any:
        """Callable from any thread. Blocks until `func(*args, **kwargs)` has
        actually executed on the Qt main thread, then returns its result (or
        re-raises whatever it raised) on the calling thread."""
        pending = _PendingCall(func=func, args=args, kwargs=kwargs)
        self._queue.put(pending)
        if not pending.done_event.wait(timeout):
            raise TimeoutError(f"GUI did not respond within {timeout}s (is the app frozen or closed?)")
        if pending.has_error:
            raise pending.error
        return pending.result
