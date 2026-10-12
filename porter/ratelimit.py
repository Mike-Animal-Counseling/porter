"""T5: per-client rate limiting (PRD §14). In-memory token bucket, v1.

60 requests/minute per key by default. Single-process; for multi-worker
deployments each worker enforces its own bucket (documented limitation —
acceptable for self-hosted v1, upgrade to Redis in P2 if needed).
"""
from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, per_minute: int) -> None:
        self.capacity = float(per_minute)
        self.refill_per_sec = per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_sec)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True

    def reset(self) -> None:
        """Test helper: clear all buckets."""
        with self._lock:
            self._buckets.clear()
