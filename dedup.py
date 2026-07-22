from collections import deque
import time


_GLOBAL_SCOPE = object()


class Deduplicator:
    def __init__(self, ttl=30, clock=None):
        self.ttl = ttl
        self.cache = {}
        self._expiry_index = deque()
        self._clock = time.monotonic if clock is None else clock

    def is_unique(self, message, scope=None):
        now = self._clock()
        key = self._cache_key(message, scope)
        self.cleanup_expired(now)
        if key in self.cache:
            return False
        entry = (now, key)
        self.cache[key] = entry
        self._expiry_index.append(entry)
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

    def reset(self):
        self.cache.clear()
        self._expiry_index.clear()

    def _cache_key(self, message, scope):
        scope_key = _GLOBAL_SCOPE if scope is None else scope
        return (scope_key, message)
