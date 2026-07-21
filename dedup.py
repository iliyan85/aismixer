import time


_GLOBAL_SCOPE = object()


class Deduplicator:
    def __init__(self, ttl=30, clock=None):
        self.ttl = ttl
        self.cache = {}
        self._clock = time.monotonic if clock is None else clock

    def is_unique(self, message, scope=None):
        now = self._clock()
        key = self._cache_key(message, scope)
        self.cleanup_expired(now)
        if key in self.cache:
            return False
        self.cache[key] = now
        return True

    def cleanup_expired(self, now=None):
        if now is None:
            now = self._clock()

        self.cache = {k: v for k, v in self.cache.items() if now -
                      v < self.ttl}

    def reset(self):
        self.cache.clear()

    def _cache_key(self, message, scope):
        scope_key = _GLOBAL_SCOPE if scope is None else scope
        return (scope_key, message)
