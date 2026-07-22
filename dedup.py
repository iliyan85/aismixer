from collections import deque
from dataclasses import dataclass
import time


_GLOBAL_SCOPE = object()


@dataclass(frozen=True)
class DedupStats:
    accepted: int
    duplicates: int
    expired: int
    capacity_evicted: int
    resets: int
    current_entries: int
    peak_entries: int


class Deduplicator:
    def __init__(self, ttl=30, clock=None, max_entries=None):
        if isinstance(max_entries, bool) or (
            max_entries is not None and not isinstance(max_entries, int)
        ):
            raise TypeError("max_entries must be an integer or None")
        if max_entries is not None and max_entries < 1:
            raise ValueError("max_entries must be at least 1")

        self.ttl = ttl
        self.max_entries = max_entries
        self.cache = {}
        self._expiry_index = deque()
        self._clock = time.monotonic if clock is None else clock
        self._accepted = 0
        self._duplicates = 0
        self._expired = 0
        self._capacity_evicted = 0
        self._resets = 0
        self._peak_entries = 0

    def is_unique(self, message, scope=None):
        now = self._clock()
        self.cleanup_expired(now)
        key = self._cache_key(message, scope)
        if key in self.cache:
            self._duplicates += 1
            return False

        while (
            self.max_entries is not None
            and len(self.cache) >= self.max_entries
        ):
            self._evict_oldest_live()

        entry = (now, key)
        self.cache[key] = entry
        self._expiry_index.append(entry)
        self._accepted += 1
        self._peak_entries = max(self._peak_entries, len(self.cache))
        return True

    def cleanup_expired(self, now=None):
        if now is None:
            now = self._clock()

        while self._expiry_index:
            entry = self._expiry_index[0]
            inserted_at, key = entry
            if now - inserted_at < self.ttl:
                break

            self._expiry_index.popleft()
            if self.cache.get(key) is entry:
                del self.cache[key]
                self._expired += 1

    def reset(self):
        self.cache.clear()
        self._expiry_index.clear()
        self._resets += 1

    def stats(self):
        return DedupStats(
            accepted=self._accepted,
            duplicates=self._duplicates,
            expired=self._expired,
            capacity_evicted=self._capacity_evicted,
            resets=self._resets,
            current_entries=len(self.cache),
            peak_entries=self._peak_entries,
        )

    def _evict_oldest_live(self):
        while self._expiry_index:
            entry = self._expiry_index.popleft()
            _, key = entry
            if self.cache.get(key) is entry:
                del self.cache[key]
                self._capacity_evicted += 1
                return

        raise RuntimeError("deduplication expiry index is inconsistent")

    def _cache_key(self, message, scope):
        scope_key = _GLOBAL_SCOPE if scope is None else scope
        return (scope_key, message)
