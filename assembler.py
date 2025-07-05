import time
from collections import defaultdict


class AIVDMAssembler:
    def __init__(self, timeout=1.0):
        self.fragments = defaultdict(list)
        self.timestamps = {}
        self.timeout = timeout  # seconds

    def feed(self, source_ip, line):
        parts = line.split(',')

        if len(parts) < 7:
            return None  # invalid format

        total = int(parts[1])
        current = int(parts[2])
        seq_id = parts[3]
        channel = parts[4]

        key = (source_ip, seq_id, channel, total)

        now = time.time()
        self.timestamps[key] = now

        self.fragments[key].append((current, line))

        if len(self.fragments[key]) == total:
            full_lines = [frag[1] for frag in sorted(self.fragments[key])]
            del self.fragments[key]
            del self.timestamps[key]
            return full_lines

        self.cleanup_expired(now)
        return None

    def cleanup_expired(self, now):
        expired_keys = [k for k, t in self.timestamps.items()
                        if now - t > self.timeout]
        for key in expired_keys:
            del self.fragments[key]
            del self.timestamps[key]
