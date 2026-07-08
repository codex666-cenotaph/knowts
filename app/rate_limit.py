"""In-process login rate limiting: per-IP and per-account backoff.

A single-container deployment (PLAN.md §2) means an in-memory limiter is
sufficient — no Redis. Failed attempts against a given key accumulate; once the
threshold is crossed the key is locked out for a cooldown window. A successful
login clears that key.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    failures: int = 0
    first_failure: float = 0.0
    locked_until: float = 0.0


@dataclass
class LoginRateLimiter:
    max_attempts: int
    lockout_seconds: int
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _retry_after(self, bucket: _Bucket, now: float) -> int:
        return max(0, int(bucket.locked_until - now))

    def check(self, *keys: str) -> int:
        """Return seconds remaining if any key is locked, else 0."""
        now = time.monotonic()
        worst = 0
        with self._lock:
            for key in keys:
                bucket = self._buckets.get(key)
                if bucket and bucket.locked_until > now:
                    worst = max(worst, self._retry_after(bucket, now))
        return worst

    def record_failure(self, *keys: str) -> int:
        """Record a failed attempt against each key. Return the max lockout
        (seconds) now in effect across those keys, or 0."""
        now = time.monotonic()
        worst = 0
        with self._lock:
            for key in keys:
                bucket = self._buckets.setdefault(key, _Bucket())
                # Reset a stale window that has fully elapsed.
                if bucket.locked_until and bucket.locked_until <= now:
                    bucket = _Bucket()
                    self._buckets[key] = bucket
                if bucket.failures == 0:
                    bucket.first_failure = now
                bucket.failures += 1
                if bucket.failures >= self.max_attempts:
                    bucket.locked_until = now + self.lockout_seconds
                    worst = max(worst, self._retry_after(bucket, now))
        return worst

    def reset(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._buckets.pop(key, None)
