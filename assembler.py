from dataclasses import dataclass
from enum import Enum
import time


AssemblyKey = tuple[str, str, str, int]


class AssemblyStatus(Enum):
    INVALID = "invalid"
    SINGLE = "single"
    PENDING = "pending"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    COMPLETE = "complete"


@dataclass(frozen=True)
class AssemblyOutcome:
    status: AssemblyStatus
    group_key: AssemblyKey | None = None
    sentences: tuple[str, ...] = ()
    discarded_keys: tuple[AssemblyKey, ...] = ()


@dataclass
class _AssemblyGroup:
    """Indexed fragments and unique-progress time for one live generation."""

    fragments_by_ordinal: dict[int, str]
    last_progress_at: float

    @property
    def received_count(self) -> int:
        return len(self.fragments_by_ordinal)


class AIVDMAssembler:
    """Correlate multipart NMEA sentences within a bounded TTL window.

    Groups are keyed by source identity, sequential message ID, channel, and
    declared total. Fragments may arrive out of order and complete only after
    every unique ordinal is present. Blank sequential IDs are supported but
    weaken correlation identity: complete ordinal coverage within the TTL does
    not prove that all fragments share a common transmission origin.
    """

    def __init__(self, timeout=1.0, clock=None):
        self._groups: dict[AssemblyKey, _AssemblyGroup] = {}
        self.timeout = timeout  # seconds
        self._clock = time.monotonic if clock is None else clock

    def feed(self, source_ip, line):
        """Preserve the legacy list-or-None assembly interface."""
        outcome = self.feed_outcome(source_ip, line)
        if outcome.status in {AssemblyStatus.SINGLE, AssemblyStatus.COMPLETE}:
            return list(outcome.sentences)
        return None

    def feed_outcome(self, source_identity, line) -> AssemblyOutcome:
        """Process one sentence and report its assembly lifecycle outcome."""
        parts = line.split(',')

        if len(parts) < 7:
            return AssemblyOutcome(AssemblyStatus.INVALID)

        try:
            total = int(parts[1])
            current = int(parts[2])
        except ValueError:
            return AssemblyOutcome(AssemblyStatus.INVALID)

        if total < 1 or current < 1 or current > total:
            return AssemblyOutcome(AssemblyStatus.INVALID)

        if total == 1:
            return AssemblyOutcome(
                AssemblyStatus.SINGLE,
                sentences=(line,),
            )

        seq_id = parts[3]
        channel = parts[4]

        key: AssemblyKey = (source_identity, seq_id, channel, total)

        now = self._clock()
        discarded_keys = []
        group = self._groups.get(key)
        if (
            group is not None
            and now - group.last_progress_at >= self.timeout
        ):
            del self._groups[key]
            discarded_keys.append(key)
            group = None

        if group is None:
            group = _AssemblyGroup(
                fragments_by_ordinal={},
                last_progress_at=now,
            )
            self._groups[key] = group

        fragments = group.fragments_by_ordinal
        if current in fragments:
            if fragments[current] == line:
                discarded_keys.extend(self._cleanup_expired(now))
                return AssemblyOutcome(
                    AssemblyStatus.DUPLICATE,
                    group_key=key,
                    discarded_keys=tuple(sorted(discarded_keys)),
                )

            del self._groups[key]
            discarded_keys.append(key)
            discarded_keys.extend(self._cleanup_expired(now))
            return AssemblyOutcome(
                AssemblyStatus.CONFLICT,
                group_key=key,
                discarded_keys=tuple(sorted(discarded_keys)),
            )

        fragments[current] = line
        group.last_progress_at = now

        # Validated, unique ordinals make cardinality a complete O(1) check.
        if group.received_count == total:
            full_lines = tuple(
                fragments[ordinal] for ordinal in range(1, total + 1)
            )
            del self._groups[key]
            return AssemblyOutcome(
                AssemblyStatus.COMPLETE,
                group_key=key,
                sentences=full_lines,
                discarded_keys=tuple(sorted(discarded_keys)),
            )

        discarded_keys.extend(self._cleanup_expired(now))
        return AssemblyOutcome(
            AssemblyStatus.PENDING,
            group_key=key,
            discarded_keys=tuple(sorted(discarded_keys)),
        )

    def cleanup_expired(self, now=None):
        if now is None:
            now = self._clock()

        self._cleanup_expired(now)

    def _cleanup_expired(self, now) -> tuple[AssemblyKey, ...]:
        expired_keys = sorted(
            key
            for key, group in self._groups.items()
            if now - group.last_progress_at >= self.timeout
        )
        for key in expired_keys:
            del self._groups[key]
        return tuple(expired_keys)

    def reset(self):
        self._groups.clear()
