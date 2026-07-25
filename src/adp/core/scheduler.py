"""Lightweight download scheduler.

A QTimer ticks on an interval and asks the scheduler to check whether any
registered (download_id, scheduled_time) pairs are due. When one is due, it's
removed from the schedule and a callback is invoked to actually start it.
This is intentionally decoupled from Qt's timer so the due-check logic itself
is trivially unit-testable with an injected clock.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal


class DownloadScheduler(QObject):
    """Tracks scheduled start times and fires `due` when one elapses."""

    due = pyqtSignal(str)  # download_id

    def __init__(self, tick_ms: int = 1000, clock: Optional[Callable[[], datetime]] = None,
                 parent=None):
        super().__init__(parent)
        self._schedule: Dict[str, datetime] = {}
        self._clock = clock or datetime.now
        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        self._timer.timeout.connect(self.check_due)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def schedule(self, download_id: str, when: datetime):
        self._schedule[download_id] = when

    def unschedule(self, download_id: str):
        self._schedule.pop(download_id, None)

    def is_scheduled(self, download_id: str) -> bool:
        return download_id in self._schedule

    def scheduled_time(self, download_id: str) -> Optional[datetime]:
        return self._schedule.get(download_id)

    def check_due(self):
        """Emits `due` for every entry whose time has passed, then removes it."""
        now = self._clock()
        due_ids = [did for did, when in self._schedule.items() if when <= now]
        for download_id in due_ids:
            del self._schedule[download_id]
            self.due.emit(download_id)
