import time


class AIVDMAssembler:
    """Correlate multipart NMEA sentences within a bounded TTL window.

    Groups are keyed by source identity, sequential message ID, channel, and
    declared total. Fragments may arrive out of order and complete only after
    every unique ordinal is present. Blank sequential IDs are supported but
    weaken correlation identity: complete ordinal coverage within the TTL does
    not prove that all fragments share a common transmission origin.
    """

    def __init__(self, timeout=1.0, clock=None):
        self.fragments = {}
        self.timestamps = {}
        self.timeout = timeout  # seconds
        self._clock = time.monotonic if clock is None else clock

    def feed(self, source_ip, line):
        parts = line.split(',')

        if len(parts) < 7:
            return None  # invalid format

        try:
            total = int(parts[1])
            current = int(parts[2])
        except ValueError:
            return None

        if total < 1 or current < 1 or current > total:
            return None

        if total == 1:
            return [line]

        seq_id = parts[3]
        channel = parts[4]

        key = (source_ip, seq_id, channel, total)

        now = self._clock()
        timestamp = self.timestamps.get(key)
        if timestamp is not None and now - timestamp >= self.timeout:
            del self.fragments[key]
            del self.timestamps[key]

        group = self.fragments.setdefault(key, {})
        if current in group:
            if group[current] == line:
                self.cleanup_expired(now)
                return None

            del self.fragments[key]
            del self.timestamps[key]
            self.cleanup_expired(now)
            return None

        group[current] = line
        self.timestamps[key] = now

        if all(ordinal in group for ordinal in range(1, total + 1)):
            full_lines = [group[ordinal] for ordinal in range(1, total + 1)]
            del self.fragments[key]
            del self.timestamps[key]
            return full_lines

        self.cleanup_expired(now)
        return None

    def cleanup_expired(self, now=None):
        if now is None:
            now = self._clock()

        expired_keys = [k for k, t in self.timestamps.items()
                        if now - t >= self.timeout]
        for key in expired_keys:
            del self.fragments[key]
            del self.timestamps[key]

    def reset(self):
        self.fragments.clear()
        self.timestamps.clear()
