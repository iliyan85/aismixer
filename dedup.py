from time import time


_GLOBAL_SCOPE = object()


class Deduplicator:
    def __init__(self, ttl=30):
        self.ttl = ttl
        self.cache = {}

    def is_unique(self, message, scope=None):
        now = time()
        key = self._cache_key(message, scope)
        if key in self.cache:
            if now - self.cache[key] < self.ttl:
                return False
        self.cache[key] = now
        self._clean_cache(now)
        return True

    def _clean_cache(self, now):
        self.cache = {k: v for k, v in self.cache.items() if now -
                      v < self.ttl}

    def _cache_key(self, message, scope):
        scope_key = _GLOBAL_SCOPE if scope is None else scope
        return (scope_key, message)
