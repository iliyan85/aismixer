import dedup
from dedup import Deduplicator


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now

    def advance(self, seconds):
        self.now += seconds


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

    clock.advance(20)

    assert not deduplicator.is_unique("message", scope="udp:aishub")

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

    assert deduplicator.reset() is None

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
