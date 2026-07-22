from dataclasses import FrozenInstanceError

import dedup
import pytest
from dedup import Deduplicator, DedupStats


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now

    def advance(self, seconds):
        self.now += seconds


def assert_stats(
    deduplicator,
    *,
    accepted=0,
    duplicates=0,
    expired=0,
    capacity_evicted=0,
    resets=0,
    current_entries=0,
    peak_entries=0,
):
    snapshot = deduplicator.stats()
    assert snapshot == DedupStats(
        accepted=accepted,
        duplicates=duplicates,
        expired=expired,
        capacity_evicted=capacity_evicted,
        resets=resets,
        current_entries=current_entries,
        peak_entries=peak_entries,
    )
    assert snapshot.current_entries == len(deduplicator.cache)
    return snapshot


def test_default_clock_uses_time_monotonic(monkeypatch):
    calls = []

    def fake_monotonic():
        calls.append(True)
        return 1000.0

    monkeypatch.setattr(dedup.time, "monotonic", fake_monotonic)
    deduplicator = Deduplicator(ttl=30)

    assert deduplicator.is_unique("message")
    assert calls


def test_unscoped_deduplication_is_global():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("message")
    assert not deduplicator.is_unique("message")

    clock.advance(31)

    assert deduplicator.is_unique("message")


def test_duplicate_message_in_same_explicit_scope_is_rejected():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("message", scope="udp:aishub")
    assert not deduplicator.is_unique("message", scope="udp:aishub")


def test_same_message_is_unique_in_different_scopes():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("message", scope="udp:a")
    assert not deduplicator.is_unique("message", scope="udp:a")
    assert deduplicator.is_unique("message", scope="udp:b")


def test_global_and_explicit_scopes_are_independent():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("message")
    assert deduplicator.is_unique("message", scope="udp:aishub")
    assert not deduplicator.is_unique("message")
    assert not deduplicator.is_unique("message", scope="udp:aishub")


def test_ttl_expiration_is_independent_per_scoped_entry():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("message", scope="udp:a")

    clock.advance(20)

    assert deduplicator.is_unique("message", scope="udp:b")

    clock.advance(11)

    assert deduplicator.is_unique("message", scope="udp:a")
    assert not deduplicator.is_unique("message", scope="udp:b")


def test_message_is_unique_exactly_at_ttl_boundary():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("message", scope="udp:aishub")

    clock.now = 1029.999

    assert not deduplicator.is_unique("message", scope="udp:aishub")

    clock.now = 1030.0

    assert deduplicator.is_unique("message", scope="udp:aishub")


def test_rejected_duplicate_does_not_refresh_ttl():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("message", scope="udp:aishub")
    cache_key = next(iter(deduplicator.cache))
    original_entry = deduplicator.cache[cache_key]
    expiry_count = len(deduplicator._expiry_index)

    clock.advance(20)

    assert not deduplicator.is_unique("message", scope="udp:aishub")
    assert deduplicator.cache[cache_key] is original_entry
    assert len(deduplicator._expiry_index) == expiry_count

    clock.advance(10)

    assert deduplicator.is_unique("message", scope="udp:aishub")


def test_cleanup_removes_entries_exactly_at_ttl_boundary():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("global")
    assert deduplicator.is_unique("scoped", scope="udp:a")

    clock.advance(30)

    assert deduplicator.cleanup_expired() is None
    assert deduplicator.is_unique("global")
    assert deduplicator.is_unique("scoped", scope="udp:a")


def test_cleanup_expired_uses_injected_clock():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("message")

    clock.advance(30)
    calls_before_cleanup = clock.calls

    assert deduplicator.cleanup_expired() is None
    assert clock.calls == calls_before_cleanup + 1
    assert deduplicator.is_unique("message")


def test_cleanup_expired_accepts_explicit_now():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("message")

    calls_before_cleanup = clock.calls

    assert deduplicator.cleanup_expired(now=1030.0) is None
    assert clock.calls == calls_before_cleanup
    assert deduplicator.is_unique("message")


def test_cleanup_removes_only_expired_prefix():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("first")
    first_key = next(iter(deduplicator.cache))

    clock.advance(10)
    assert deduplicator.is_unique("second")
    second_key = next(
        key for key in deduplicator.cache if key != first_key
    )

    clock.advance(10)
    assert deduplicator.is_unique("third")
    third_key = next(
        key
        for key in deduplicator.cache
        if key not in (first_key, second_key)
    )

    clock.advance(10)
    assert deduplicator.cleanup_expired() is None
    assert first_key not in deduplicator.cache
    assert second_key in deduplicator.cache
    assert third_key in deduplicator.cache
    assert len(deduplicator._expiry_index) == 2

    clock.advance(10)
    assert deduplicator.cleanup_expired() is None
    assert second_key not in deduplicator.cache
    assert third_key in deduplicator.cache
    assert len(deduplicator._expiry_index) == 1


def test_stale_expiry_record_cannot_remove_newer_incarnation():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("message")
    cache_key = next(iter(deduplicator.cache))
    old_entry = deduplicator.cache.pop(cache_key)

    clock.advance(10)
    assert deduplicator.is_unique("message")
    new_entry = deduplicator.cache[cache_key]
    assert new_entry is not old_entry
    assert len(deduplicator._expiry_index) == 2

    clock.advance(20)
    assert deduplicator.cleanup_expired() is None
    assert deduplicator.cache[cache_key] is new_entry
    assert len(deduplicator._expiry_index) == 1
    assert_stats(
        deduplicator,
        accepted=2,
        current_entries=1,
        peak_entries=1,
    )
    assert not deduplicator.is_unique("message")


def test_opaque_transport_agnostic_scope_strings_are_accepted():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    for scope in (
        "udp:aishub",
        "mqtt:clean_stream",
        "amqp:partner_exchange",
        "mongo:raw_archive",
    ):
        assert deduplicator.is_unique("message", scope=scope)
        assert not deduplicator.is_unique("message", scope=scope)


def test_two_different_messages_in_same_scope_are_independent():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.is_unique("message one", scope="udp:aishub")
    assert deduplicator.is_unique("message two", scope="udp:aishub")
    assert not deduplicator.is_unique("message one", scope="udp:aishub")
    assert not deduplicator.is_unique("message two", scope="udp:aishub")


def test_exact_tuple_key_is_suppressed_within_ttl():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)
    group_key = ("part one", "part two")

    assert deduplicator.is_unique(group_key)
    assert not deduplicator.is_unique(group_key)


def test_tuple_keys_differing_in_one_fragment_are_distinct():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)
    first_group = ("shared part one", "old part two")
    changed_group = ("shared part one", "new part two")

    assert deduplicator.is_unique(first_group)
    assert deduplicator.is_unique(changed_group)
    assert not deduplicator.is_unique(first_group)
    assert not deduplicator.is_unique(changed_group)


def test_same_tuple_key_is_independent_across_target_scopes():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)
    group_key = ("part one", "part two")

    assert deduplicator.is_unique(group_key, scope="udp:first")
    assert not deduplicator.is_unique(group_key, scope="udp:first")
    assert deduplicator.is_unique(group_key, scope="udp:second")
    assert not deduplicator.is_unique(group_key, scope="udp:second")


def test_reset_clears_global_scoped_string_and_tuple_entries():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)
    global_key = "global message"
    scoped_key = "scoped message"
    group_key = ("part one", "part two")

    assert deduplicator.is_unique(global_key)
    assert deduplicator.is_unique(scoped_key, scope="udp:first")
    assert deduplicator.is_unique(group_key, scope="udp:second")
    assert not deduplicator.is_unique(global_key)
    assert not deduplicator.is_unique(scoped_key, scope="udp:first")
    assert not deduplicator.is_unique(group_key, scope="udp:second")
    assert len(deduplicator.cache) == 3
    assert len(deduplicator._expiry_index) == 3

    assert deduplicator.reset() is None
    assert deduplicator.cache == {}
    assert not deduplicator._expiry_index

    assert deduplicator.is_unique(global_key)
    assert deduplicator.is_unique(scoped_key, scope="udp:first")
    assert deduplicator.is_unique(group_key, scope="udp:second")


def test_reset_is_safe_when_empty_and_preserves_configuration():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=30, clock=clock)

    assert deduplicator.reset() is None
    assert deduplicator.reset() is None

    calls_before_observation = clock.calls

    assert deduplicator.is_unique("message")
    assert clock.calls == calls_before_observation + 1

    clock.now = 1029.999

    assert not deduplicator.is_unique("message")

    clock.now = 1030.0

    assert deduplicator.is_unique("message")


def test_max_entries_none_preserves_unbounded_behavior():
    deduplicator = Deduplicator(max_entries=None)

    assert deduplicator.max_entries is None
    assert deduplicator.is_unique("one")
    assert deduplicator.is_unique("two")
    assert deduplicator.is_unique("three")
    assert_stats(
        deduplicator,
        accepted=3,
        current_entries=3,
        peak_entries=3,
    )


@pytest.mark.parametrize("max_entries", [1, 2, 100])
def test_positive_max_entries_is_accepted(max_entries):
    deduplicator = Deduplicator(max_entries=max_entries)

    assert deduplicator.max_entries == max_entries


@pytest.mark.parametrize("max_entries", [0, -1])
def test_max_entries_below_one_is_rejected(max_entries):
    with pytest.raises(ValueError):
        Deduplicator(max_entries=max_entries)


@pytest.mark.parametrize(
    "max_entries",
    [True, False, "2", 2.0, object()],
)
def test_non_integer_max_entries_is_rejected(max_entries):
    with pytest.raises(TypeError):
        Deduplicator(max_entries=max_entries)


def test_capacity_evicts_oldest_live_entries_in_order():
    clock = FakeClock()
    deduplicator = Deduplicator(
        ttl=100,
        clock=clock,
        max_entries=2,
    )

    assert deduplicator.is_unique("A")
    clock.advance(1)
    assert deduplicator.is_unique("B")

    clock.advance(1)
    calls_before_overflow = clock.calls
    assert deduplicator.is_unique("C")
    assert clock.calls == calls_before_overflow + 1
    assert not deduplicator.is_unique("C")
    assert not deduplicator.is_unique("B")

    clock.advance(1)
    assert deduplicator.is_unique("A")
    assert not deduplicator.is_unique("A")

    clock.advance(1)
    assert deduplicator.is_unique("B")
    assert_stats(
        deduplicator,
        accepted=5,
        duplicates=3,
        capacity_evicted=3,
        current_entries=2,
        peak_entries=2,
    )


def test_capacity_is_shared_across_scopes_and_key_types():
    clock = FakeClock()
    deduplicator = Deduplicator(
        ttl=100,
        clock=clock,
        max_entries=3,
    )
    group_key = ("part one", "part two")
    changed_group_key = ("part one", "changed part two")

    assert deduplicator.is_unique("same")
    clock.advance(1)
    assert deduplicator.is_unique("same", scope="udp:a")
    clock.advance(1)
    assert deduplicator.is_unique(group_key, scope="udp:b")

    assert not deduplicator.is_unique("same")
    assert not deduplicator.is_unique("same", scope="udp:a")
    assert not deduplicator.is_unique(group_key, scope="udp:b")

    clock.advance(1)
    assert deduplicator.is_unique(changed_group_key, scope="udp:b")
    assert not deduplicator.is_unique("same", scope="udp:a")
    assert not deduplicator.is_unique(group_key, scope="udp:b")

    clock.advance(1)
    assert deduplicator.is_unique("same")
    clock.advance(1)
    assert deduplicator.is_unique("same", scope="udp:a")
    assert_stats(
        deduplicator,
        accepted=6,
        duplicates=5,
        capacity_evicted=3,
        current_entries=3,
        peak_entries=3,
    )


def test_duplicate_at_capacity_changes_only_duplicate_count():
    clock = FakeClock()
    deduplicator = Deduplicator(
        ttl=30,
        clock=clock,
        max_entries=2,
    )

    assert deduplicator.is_unique("A")
    clock.advance(1)
    assert deduplicator.is_unique("B")

    clock.now = 1029.999
    expiry_index_before = tuple(deduplicator._expiry_index)
    stats_before = assert_stats(
        deduplicator,
        accepted=2,
        current_entries=2,
        peak_entries=2,
    )
    calls_before_duplicate = clock.calls

    assert not deduplicator.is_unique("A")
    assert clock.calls == calls_before_duplicate + 1
    assert tuple(deduplicator._expiry_index) == expiry_index_before
    stats_after = assert_stats(
        deduplicator,
        accepted=2,
        duplicates=1,
        current_entries=2,
        peak_entries=2,
    )
    assert stats_after.accepted == stats_before.accepted
    assert stats_after.expired == stats_before.expired
    assert stats_after.capacity_evicted == stats_before.capacity_evicted

    clock.now = 1030.0
    assert deduplicator.is_unique("C")
    assert_stats(
        deduplicator,
        accepted=3,
        duplicates=1,
        expired=1,
        current_entries=2,
        peak_entries=2,
    )


def test_live_entry_immediately_before_ttl_is_capacity_evicted():
    clock = FakeClock()
    deduplicator = Deduplicator(
        ttl=30,
        clock=clock,
        max_entries=2,
    )

    assert deduplicator.is_unique("A")
    clock.advance(1)
    assert deduplicator.is_unique("B")

    clock.now = 1029.999
    assert deduplicator.is_unique("C")
    assert deduplicator._cache_key("A", None) not in deduplicator.cache
    assert deduplicator._cache_key("C", None) in deduplicator.cache
    assert_stats(
        deduplicator,
        accepted=3,
        capacity_evicted=1,
        current_entries=2,
        peak_entries=2,
    )


def test_expired_counter_covers_both_cleanup_paths():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=10, clock=clock)

    assert deduplicator.is_unique("A")
    clock.advance(1)
    assert deduplicator.is_unique("B")

    assert deduplicator.cleanup_expired(now=1010.0) is None
    assert_stats(
        deduplicator,
        accepted=2,
        expired=1,
        current_entries=1,
        peak_entries=2,
    )

    clock.now = 1011.0
    assert deduplicator.is_unique("C")
    assert_stats(
        deduplicator,
        accepted=3,
        expired=2,
        current_entries=1,
        peak_entries=2,
    )


def test_capacity_eviction_skips_stale_front_record():
    clock = FakeClock()
    deduplicator = Deduplicator(
        ttl=100,
        clock=clock,
        max_entries=2,
    )

    assert deduplicator.is_unique("A")
    cache_key = next(iter(deduplicator.cache))
    old_entry = deduplicator.cache.pop(cache_key)

    clock.advance(1)
    assert deduplicator.is_unique("B")
    clock.advance(1)
    assert deduplicator.is_unique("A")
    new_entry = deduplicator.cache[cache_key]
    assert new_entry is not old_entry

    clock.advance(1)
    assert deduplicator.is_unique("C")
    assert deduplicator.cache[cache_key] is new_entry
    assert old_entry not in deduplicator._expiry_index
    assert len(deduplicator._expiry_index) == 2
    assert_stats(
        deduplicator,
        accepted=4,
        capacity_evicted=1,
        current_entries=2,
        peak_entries=2,
    )

    assert not deduplicator.is_unique("A")
    assert not deduplicator.is_unique("C")
    clock.advance(1)
    assert deduplicator.is_unique("B")
    assert_stats(
        deduplicator,
        accepted=5,
        duplicates=2,
        capacity_evicted=2,
        current_entries=2,
        peak_entries=2,
    )


def test_stats_snapshot_is_immutable_stable_and_side_effect_free():
    clock = FakeClock()
    deduplicator = Deduplicator(ttl=10, clock=clock)

    initial = assert_stats(deduplicator)
    with pytest.raises(FrozenInstanceError):
        initial.accepted = 1

    assert deduplicator.is_unique("A")
    retained = assert_stats(
        deduplicator,
        accepted=1,
        current_entries=1,
        peak_entries=1,
    )

    clock.advance(10)
    calls_before_stats = clock.calls
    cache_before_stats = dict(deduplicator.cache)
    expiry_index_before_stats = tuple(deduplicator._expiry_index)
    pending_expiry = assert_stats(
        deduplicator,
        accepted=1,
        current_entries=1,
        peak_entries=1,
    )
    assert clock.calls == calls_before_stats
    assert deduplicator.cache == cache_before_stats
    assert tuple(deduplicator._expiry_index) == expiry_index_before_stats
    assert pending_expiry.expired == 0

    assert deduplicator.cleanup_expired() is None
    assert_stats(
        deduplicator,
        accepted=1,
        expired=1,
        peak_entries=1,
    )
    assert initial == DedupStats(0, 0, 0, 0, 0, 0, 0)
    assert retained == DedupStats(1, 0, 0, 0, 0, 1, 1)
    assert pending_expiry == retained


def test_reset_preserves_cumulative_statistics_and_capacity():
    clock = FakeClock()
    deduplicator = Deduplicator(
        ttl=10,
        clock=clock,
        max_entries=2,
    )

    assert deduplicator.is_unique("A")
    clock.advance(1)
    assert deduplicator.is_unique("B")
    clock.advance(1)
    assert not deduplicator.is_unique("A")
    clock.advance(1)
    assert deduplicator.is_unique("C")

    clock.now = 1011.0
    assert deduplicator.cleanup_expired() is None
    assert_stats(
        deduplicator,
        accepted=3,
        duplicates=1,
        expired=1,
        capacity_evicted=1,
        current_entries=1,
        peak_entries=2,
    )

    assert deduplicator.reset() is None
    assert deduplicator.cache == {}
    assert not deduplicator._expiry_index
    assert_stats(
        deduplicator,
        accepted=3,
        duplicates=1,
        expired=1,
        capacity_evicted=1,
        resets=1,
        peak_entries=2,
    )

    assert deduplicator.reset() is None
    assert_stats(
        deduplicator,
        accepted=3,
        duplicates=1,
        expired=1,
        capacity_evicted=1,
        resets=2,
        peak_entries=2,
    )
    assert deduplicator.ttl == 10
    assert deduplicator._clock is clock
    assert deduplicator.max_entries == 2

    assert deduplicator.is_unique("C")
    assert deduplicator.is_unique("D")
    assert deduplicator.is_unique("E")
    assert_stats(
        deduplicator,
        accepted=6,
        duplicates=1,
        expired=1,
        capacity_evicted=2,
        resets=2,
        current_entries=2,
        peak_entries=2,
    )
