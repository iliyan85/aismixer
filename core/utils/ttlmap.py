import time
from collections import deque
from typing import Callable, Optional, Any

_MONO = time.monotonic_ns


class TTLMap:
    __slots__ = ("_ttl_ns", "_max_entries", "_on_evict", "_d", "_q",
                 "_last_sweep_ns", "_sweep_every_ns", "_ops", "_ops_per_sweep")

    def __init__(self, ttl_seconds: float, max_entries: int = 200_000,
                 on_evict: Optional[Callable[[Any], None]] = None,
                 sweep_every_seconds: float = 1.0, ops_per_sweep: int = 2048):
        self._ttl_ns = int(ttl_seconds * 1e9)
        self._max_entries = max_entries
        self._on_evict = on_evict
        self._d = {}
        self._q = deque()
        self._last_sweep_ns = _MONO()
        self._sweep_every_ns = int(sweep_every_seconds * 1e9)
        self._ops = 0
        self._ops_per_sweep = ops_per_sweep

    def touch(self, key: Any, now_ns: Optional[int] = None) -> None:
        n = _MONO() if now_ns is None else now_ns
        exp = n + self._ttl_ns
        self._d[key] = exp
        self._q.append((exp, key))
        self._maybe_sweep(n)
        if len(self._d) > self._max_entries:
            self._evict_oldest(n, hard=True)

    def contains(self, key: Any, now_ns: Optional[int] = None) -> bool:
        n = _MONO() if now_ns is None else now_ns
        exp = self._d.get(key)
        if exp is None:
            self._maybe_sweep(n)
            return False
        if exp <= n:
            self._evict_key_if_expired(key, n)
            return False
        self._maybe_sweep(n)
        return True

    def __len__(self) -> int: return len(self._d)

    # --- вътрешно ---
    def _maybe_sweep(self, now_ns: int) -> None:
        self._ops += 1
        if self._ops >= self._ops_per_sweep or (now_ns - self._last_sweep_ns) >= self._sweep_every_ns:
            self._sweep(now_ns)
            self._ops = 0
            self._last_sweep_ns = now_ns

    def _sweep(self, now_ns: int) -> None:
        q, d, on_evict = self._q, self._d, self._on_evict
        while q and q[0][0] <= now_ns:
            exp, key = q.popleft()
            cur = d.get(key)
            if cur is not None and cur <= now_ns and cur == exp:
                del d[key]
                if on_evict:
                    on_evict(key)

    def _evict_key_if_expired(self, key: Any, now_ns: int) -> None:
        exp = self._d.get(key)
        if exp is not None and exp <= now_ns:
            del self._d[key]
            if self._on_evict:
                self._on_evict(key)

    def _evict_oldest(self, now_ns: int, hard: bool = False) -> None:
        q, d, on_evict, target = self._q, self._d, self._on_evict, self._max_entries
        while len(d) > target and q:
            exp, key = q.popleft()
            cur = d.get(key)
            if cur is None:
                continue
            if hard or cur <= now_ns:
                del d[key]
                if on_evict:
                    on_evict(key)
