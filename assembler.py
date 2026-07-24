from dataclasses import dataclass
from enum import Enum
import time

from core.ingress_frame import decode_frame_slice
from core.parsed_sentence import ParsedSentence


AssemblyKey = tuple[str, str, str, int]


class AssemblyStatus(Enum):
    INVALID = "invalid"
    SINGLE = "single"
    LIMIT_EXCEEDED = "limit_exceeded"
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


@dataclass(frozen=True)
class AssemblerStats:
    invalid: int
    single: int
    limit_exceeded: int
    pending: int
    duplicates: int
    conflicts: int
    completed: int
    expired: int
    capacity_evicted: int
    reset_discarded: int
    resets: int
    current_groups: int
    peak_groups: int
    current_fragments: int
    peak_fragments: int


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

    def __init__(
        self,
        timeout=1.0,
        clock=None,
        max_fragments_per_group=None,
        max_pending_groups=None,
    ):
        self.max_fragments_per_group = self._validate_limit(
            "max_fragments_per_group",
            max_fragments_per_group,
        )
        self.max_pending_groups = self._validate_limit(
            "max_pending_groups",
            max_pending_groups,
        )
        self._groups: dict[AssemblyKey, _AssemblyGroup] = {}
        self.timeout = timeout  # seconds
        self._clock = time.monotonic if clock is None else clock
        self._outcome_counts = {
            status: 0
            for status in AssemblyStatus
        }
        self._expired = 0
        self._capacity_evicted = 0
        self._reset_discarded = 0
        self._resets = 0
        self._peak_groups = 0
        self._peak_fragments = 0

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
            return self._outcome(AssemblyStatus.INVALID)

        try:
            total = int(parts[1])
            current = int(parts[2])
        except ValueError:
            return self._outcome(AssemblyStatus.INVALID)

        if total < 1 or current < 1 or current > total:
            return self._outcome(AssemblyStatus.INVALID)

        return self._feed_validated_fragment(
            source_identity=source_identity,
            sentence_text=line,
            declared_total=total,
            ordinal=current,
            sequential_id=parts[3],
            channel=parts[4],
        )

    def feed_parsed_outcome(
        self,
        parsed: ParsedSentence,
    ) -> AssemblyOutcome:
        """Process one parse-once sentence through the assembler lifecycle."""
        fragment = parsed.fragment
        if fragment is None:
            return self._outcome(AssemblyStatus.INVALID)

        sentence_span = parsed.match.sentence_span
        sentence_text = decode_frame_slice(
            parsed.frame,
            sentence_span.start,
            sentence_span.end,
        )
        return self._feed_validated_fragment(
            source_identity=parsed.frame.assembler_key,
            sentence_text=sentence_text,
            declared_total=fragment.declared_total,
            ordinal=fragment.ordinal,
            sequential_id=fragment.sequential_id,
            channel=fragment.channel,
        )

    def _feed_validated_fragment(
        self,
        source_identity: str,
        sentence_text: str,
        declared_total: int,
        ordinal: int,
        sequential_id: str,
        channel: str,
    ) -> AssemblyOutcome:
        if declared_total == 1:
            return self._outcome(
                AssemblyStatus.SINGLE,
                sentences=(sentence_text,),
            )

        if (
            self.max_fragments_per_group is not None
            and declared_total > self.max_fragments_per_group
        ):
            return self._outcome(AssemblyStatus.LIMIT_EXCEEDED)

        key: AssemblyKey = (
            source_identity,
            sequential_id,
            channel,
            declared_total,
        )

        now = self._clock()
        discarded_keys: list[AssemblyKey] = []
        cleanup_performed = False
        group = self._groups.get(key)
        if (
            group is not None
            and now - group.last_progress_at >= self.timeout
        ):
            self._expire_group(key)
            discarded_keys.append(key)
            group = None

        if group is None:
            if (
                self.max_pending_groups is not None
                and len(self._groups) >= self.max_pending_groups
            ):
                discarded_keys.extend(self._cleanup_expired(now))
                cleanup_performed = True
                if len(self._groups) >= self.max_pending_groups:
                    discarded_keys.append(self._evict_capacity_victim())

            group = _AssemblyGroup(
                fragments_by_ordinal={},
                last_progress_at=now,
            )
            self._groups[key] = group

        fragments = group.fragments_by_ordinal
        if ordinal in fragments:
            if fragments[ordinal] == sentence_text:
                discarded_keys.extend(self._cleanup_expired(now))
                return self._outcome(
                    AssemblyStatus.DUPLICATE,
                    group_key=key,
                    discarded_keys=tuple(sorted(discarded_keys)),
                )

            del self._groups[key]
            discarded_keys.append(key)
            discarded_keys.extend(self._cleanup_expired(now))
            return self._outcome(
                AssemblyStatus.CONFLICT,
                group_key=key,
                discarded_keys=tuple(sorted(discarded_keys)),
            )

        fragments[ordinal] = sentence_text
        group.last_progress_at = now

        # Validated, unique ordinals make cardinality a complete O(1) check.
        if group.received_count == declared_total:
            full_lines = tuple(
                fragments[index]
                for index in range(1, declared_total + 1)
            )
            del self._groups[key]
            return self._outcome(
                AssemblyStatus.COMPLETE,
                group_key=key,
                sentences=full_lines,
                discarded_keys=tuple(sorted(discarded_keys)),
            )

        if not cleanup_performed:
            discarded_keys.extend(self._cleanup_expired(now))
        self._update_peaks()
        return self._outcome(
            AssemblyStatus.PENDING,
            group_key=key,
            discarded_keys=tuple(sorted(discarded_keys)),
        )

    def cleanup_expired(self, now=None) -> tuple[AssemblyKey, ...]:
        if now is None:
            now = self._clock()

        return self._cleanup_expired(now)

    def _cleanup_expired(self, now) -> tuple[AssemblyKey, ...]:
        expired_keys = sorted(
            key
            for key, group in self._groups.items()
            if now - group.last_progress_at >= self.timeout
        )
        for key in expired_keys:
            self._expire_group(key)
        return tuple(expired_keys)

    def reset(self) -> tuple[AssemblyKey, ...]:
        discarded_keys = tuple(sorted(self._groups))
        self._groups.clear()
        self._resets += 1
        self._reset_discarded += len(discarded_keys)
        return discarded_keys

    def stats(self) -> AssemblerStats:
        return AssemblerStats(
            invalid=self._outcome_counts[AssemblyStatus.INVALID],
            single=self._outcome_counts[AssemblyStatus.SINGLE],
            limit_exceeded=self._outcome_counts[
                AssemblyStatus.LIMIT_EXCEEDED
            ],
            pending=self._outcome_counts[AssemblyStatus.PENDING],
            duplicates=self._outcome_counts[AssemblyStatus.DUPLICATE],
            conflicts=self._outcome_counts[AssemblyStatus.CONFLICT],
            completed=self._outcome_counts[AssemblyStatus.COMPLETE],
            expired=self._expired,
            capacity_evicted=self._capacity_evicted,
            reset_discarded=self._reset_discarded,
            resets=self._resets,
            current_groups=len(self._groups),
            peak_groups=self._peak_groups,
            current_fragments=sum(
                group.received_count
                for group in self._groups.values()
            ),
            peak_fragments=self._peak_fragments,
        )

    @staticmethod
    def _validate_limit(name, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be None or a positive integer")
        if value < 1:
            raise ValueError(f"{name} must be at least 1")
        return value

    def _outcome(
        self,
        status: AssemblyStatus,
        group_key: AssemblyKey | None = None,
        sentences: tuple[str, ...] = (),
        discarded_keys: tuple[AssemblyKey, ...] = (),
    ) -> AssemblyOutcome:
        self._outcome_counts[status] += 1
        return AssemblyOutcome(
            status=status,
            group_key=group_key,
            sentences=sentences,
            discarded_keys=discarded_keys,
        )

    def _expire_group(self, key: AssemblyKey) -> None:
        del self._groups[key]
        self._expired += 1

    def _evict_capacity_victim(self) -> AssemblyKey:
        victim = min(
            self._groups,
            key=lambda key: (
                self._groups[key].last_progress_at,
                key,
            ),
        )
        del self._groups[victim]
        self._capacity_evicted += 1
        return victim

    def _update_peaks(self) -> None:
        self._peak_groups = max(self._peak_groups, len(self._groups))
        current_fragments = sum(
            group.received_count
            for group in self._groups.values()
        )
        self._peak_fragments = max(
            self._peak_fragments,
            current_fragments,
        )
