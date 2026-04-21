import threading
import time


class RateLimiter:
    """Token-bucket, thread-safe, one per source."""

    def __init__(self, qps: float, burst: int = 5):
        self._qps   = float(qps)
        self._burst = int(burst)
        self._tokens = float(burst)
        self._last   = time.monotonic()
        self._lock   = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._burst, self._tokens + elapsed * self._qps)
            self._last = now
            if self._tokens < 1:
                deficit = 1 - self._tokens
                time.sleep(deficit / self._qps if self._qps > 0 else 1)
                self._tokens = 0
            else:
                self._tokens -= 1

    def try_acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._last) * self._qps)
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False
