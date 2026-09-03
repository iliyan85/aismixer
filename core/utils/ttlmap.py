import time
from collections import OrderedDict
from typing import Callable, Optional, Any

_MONO = time.monotonic_ns


class TTLMap:
    __slots__ = ("_ttl_ns", "_max_entries", "_on_evict", "_d",
                 "_last_sweep_ns", "_sweep_every_ns", "_ops", "_ops_per_sweep")

    def __init__(self, ttl_seconds: float, max_entries: int = 200_000,
                 on_evict: Optional[Callable[[Any], None]] = None,
                 sweep_every_seconds: float = 1.0, ops_per_sweep: int = 2048):
        self._ttl_ns = int(ttl_seconds * 1e9)
        self._max_entries = max_entries
        self._on_evict = on_evict
        self._d: "OrderedDict[Any, int]" = OrderedDict()
        self._last_sweep_ns = _MONO()
        self._sweep_every_ns = int(sweep_every_seconds * 1e9)
        self._ops = 0
        self._ops_per_sweep = ops_per_sweep

    def touch(self, key: Any, now_ns: Optional[int] = None) -> None:
        n = _MONO() if now_ns is None else now_ns
        exp = n + self._ttl_ns
        self._d[key] = exp
        self._d.move_to_end(key)
        self._maybe_sweep(n)
        if len(self._d) > self._max_entries:
            self._evict_oldest()

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

    def clear(self) -> int:
        """Discard all live entries, preserving config.

        The eviction callback is invoked exactly once for each key that was
        live when the clear began. Callback order is not part of the
        contract.
        """
        live_keys = tuple(self._d)
        removed = len(live_keys)

        self._d.clear()
        self._ops = 0
        self._last_sweep_ns = _MONO()

        if self._on_evict:
            for key in live_keys:
                self._on_evict(key)

        return removed

    # --- вътрешно ---
    def _maybe_sweep(self, now_ns: int) -> None:
        self._ops += 1
        if self._ops >= self._ops_per_sweep or (now_ns - self._last_sweep_ns) >= self._sweep_every_ns:
            self._sweep(now_ns)
            self._ops = 0
            self._last_sweep_ns = now_ns

    def _sweep(self, now_ns: int) -> None:
        d, on_evict = self._d, self._on_evict
        while d:
            key, exp = next(iter(d.items()))
            if exp > now_ns:
                break
            del d[key]
            if on_evict:
                on_evict(key)

    def _evict_key_if_expired(self, key: Any, now_ns: int) -> None:
        exp = self._d.get(key)
        if exp is not None and exp <= now_ns:
            del self._d[key]
            if self._on_evict:
                self._on_evict(key)

    def _evict_oldest(self) -> None:
        d, on_evict, target = self._d, self._on_evict, self._max_entries
        while len(d) > target:
            key, _ = next(iter(d.items()))
            del d[key]
            if on_evict:
                on_evict(key)
