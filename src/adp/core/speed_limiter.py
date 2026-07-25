"""A thread-safe token-bucket bandwidth limiter.

Each DownloadManager owns one SpeedLimiter instance which is shared by all of
its DownloadWorker threads, so the limit applies to the download as a whole
rather than per-connection. A limit of 0 means "unlimited" and acquire()
becomes a no-op for speed.
"""
from __future__ import annotations

import threading
import time


class SpeedLimiter:
    def __init__(self, bytes_per_second: int = 0):
        self._lock = threading.Lock()
        self.set_limit(bytes_per_second)

    def set_limit(self, bytes_per_second: int) -> None:
        with self._lock:
            self.rate = max(0, int(bytes_per_second))
            # Allow short bursts up to ~0.25s worth of tokens, floor of 8KB.
            self.capacity = max(self.rate * 0.25, 8192) if self.rate else 0
            self.tokens = self.capacity
            self.last_refill = time.monotonic()

    @property
    def unlimited(self) -> bool:
        return self.rate <= 0

    def acquire(self, num_bytes: int) -> None:
        """Blocks (sleeps) as needed so that consumption stays under the rate."""
        if self.unlimited or num_bytes <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.last_refill = now
                # Cap the bucket at max(capacity, num_bytes): a single request
                # larger than the burst capacity must still be satisfiable,
                # otherwise tokens could never reach it and this would spin
                # forever.
                ceiling = max(self.capacity, num_bytes)
                self.tokens = min(ceiling, self.tokens + elapsed * self.rate)
                if self.tokens >= num_bytes:
                    self.tokens -= num_bytes
                    return
                deficit = num_bytes - self.tokens
                sleep_time = deficit / self.rate
            time.sleep(min(sleep_time, 0.5))
