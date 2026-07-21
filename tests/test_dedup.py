import dedup
from dedup import Deduplicator


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_unscoped_deduplication_behavior_remains_global(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)

    assert deduplicator.is_unique("message")
    assert not deduplicator.is_unique("message")

    clock.advance(31)

    assert deduplicator.is_unique("message")


def test_duplicate_message_in_same_explicit_scope_is_rejected(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)

    assert deduplicator.is_unique("message", scope="udp:aishub")
    assert not deduplicator.is_unique("message", scope="udp:aishub")


def test_same_message_is_unique_in_different_scopes(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)

    assert deduplicator.is_unique("message", scope="udp:a")
    assert not deduplicator.is_unique("message", scope="udp:a")
    assert deduplicator.is_unique("message", scope="udp:b")


def test_global_and_explicit_scopes_are_independent(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)

    assert deduplicator.is_unique("message")
    assert deduplicator.is_unique("message", scope="udp:aishub")
    assert not deduplicator.is_unique("message")
    assert not deduplicator.is_unique("message", scope="udp:aishub")


def test_ttl_expiration_is_independent_per_scoped_entry(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)

    assert deduplicator.is_unique("message", scope="udp:a")

    clock.advance(20)

    assert deduplicator.is_unique("message", scope="udp:b")

    clock.advance(11)

    assert deduplicator.is_unique("message", scope="udp:a")
    assert not deduplicator.is_unique("message", scope="udp:b")


def test_current_behavior_accepts_message_exactly_at_ttl_boundary(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)

    assert deduplicator.is_unique("message", scope="udp:aishub")

    clock.now = 1029.999

    assert not deduplicator.is_unique("message", scope="udp:aishub")

    clock.now = 1030.0

    assert deduplicator.is_unique("message", scope="udp:aishub")


def test_current_behavior_rejected_duplicate_does_not_refresh_ttl(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)

    assert deduplicator.is_unique("message", scope="udp:aishub")

    clock.advance(20)

    assert not deduplicator.is_unique("message", scope="udp:aishub")

    # Characterization: expiry remains anchored to the last accepted observation.
    clock.advance(10)

    assert deduplicator.is_unique("message", scope="udp:aishub")


def test_cleanup_removes_expired_scoped_and_unscoped_entries(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)

    assert deduplicator.is_unique("global")
    assert deduplicator.is_unique("scoped", scope="udp:a")

    clock.advance(31)

    assert deduplicator.is_unique("fresh", scope="udp:b")

    assert len(deduplicator.cache) == 1
    assert deduplicator.is_unique("global")
    assert deduplicator.is_unique("scoped", scope="udp:a")


def test_opaque_transport_agnostic_scope_strings_are_accepted(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)

    for scope in (
        "udp:aishub",
        "mqtt:clean_stream",
        "amqp:partner_exchange",
        "mongo:raw_archive",
    ):
        assert deduplicator.is_unique("message", scope=scope)
        assert not deduplicator.is_unique("message", scope=scope)


def test_two_different_messages_in_same_scope_are_independent(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)

    assert deduplicator.is_unique("message one", scope="udp:aishub")
    assert deduplicator.is_unique("message two", scope="udp:aishub")
    assert not deduplicator.is_unique("message one", scope="udp:aishub")
    assert not deduplicator.is_unique("message two", scope="udp:aishub")


def test_exact_tuple_key_is_suppressed_within_ttl(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)
    group_key = ("part one", "part two")

    assert deduplicator.is_unique(group_key)
    assert not deduplicator.is_unique(group_key)


def test_tuple_keys_differing_in_one_fragment_are_distinct(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)
    first_group = ("shared part one", "old part two")
    changed_group = ("shared part one", "new part two")

    assert deduplicator.is_unique(first_group)
    assert deduplicator.is_unique(changed_group)
    assert not deduplicator.is_unique(first_group)
    assert not deduplicator.is_unique(changed_group)


def test_same_tuple_key_is_independent_across_target_scopes(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dedup, "time", clock)
    deduplicator = Deduplicator(ttl=30)
    group_key = ("part one", "part two")

    assert deduplicator.is_unique(group_key, scope="udp:first")
    assert not deduplicator.is_unique(group_key, scope="udp:first")
    assert deduplicator.is_unique(group_key, scope="udp:second")
    assert not deduplicator.is_unique(group_key, scope="udp:second")
