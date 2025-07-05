from time import time


class Deduplicator:
    def __init__(self, ttl=30):
        self.ttl = ttl
        self.cache = {}

    def is_unique(self, message):
        now = time()
        if message in self.cache:
            if now - self.cache[message] < self.ttl:
                return False
        self.cache[message] = now
        self._clean_cache(now)
        return True

    def _clean_cache(self, now):
        self.cache = {k: v for k, v in self.cache.items() if now -
                      v < self.ttl}
