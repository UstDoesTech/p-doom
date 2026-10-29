"""Limits for the public, human-only app. Nothing here applies to the Databricks App, whose
users sign in through the workspace."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    """At most `limit` hits per `window_s` for each key, counted in a sliding window. A limit of
    0 turns it off."""

    def __init__(
        self, limit: int, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit, self.window_s, self._clock = limit, window_s, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._swept = clock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = self._clock()
        cutoff = now - self.window_s
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if now - self._swept > self.window_s:  # forget keys that have gone quiet
                self._swept = now
                for k in [k for k, h in self._hits.items() if not h or h[-1] <= cutoff]:
                    del self._hits[k]
            return True


def client_ip(forwarded_for: str | None, peer: str | None, trust_proxy: bool) -> str:
    """The address to count a request against. Behind Azure's ingress the peer is the proxy, and
    the proxy adds the real client as the last entry of X-Forwarded-For; earlier entries are
    whatever the client sent, so they are not trusted."""
    if trust_proxy and forwarded_for:
        last = forwarded_for.split(",")[-1].strip()
        if last:
            return last
    return peer or "unknown"
