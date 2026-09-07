"""Unit tests for `core.session_identity_registry.SessionIdentityRegistry`.

These tests exercise the registry in isolation, independent of
`aismixer_secure.SecureState`. See tests/test_secure_udp_helpers.py for the
integration-level proofs that `SecureState` actually shares one such
registry across owners, and that its `session_handle`/`assembly_namespace`
lifecycle (claim/release around install/promote/remove) is wired correctly.
"""

from __future__ import annotations

import threading
import time

import pytest

from core.session_identity_registry import (
    IDENTIFIER_WIDTH_BYTES,
    MAX_GENERATION_ATTEMPTS,
    RETIREMENT_SECONDS,
    SessionIdentityExhaustedError,
    SessionIdentityRegistry,
)


def test_reserve_returns_distinct_correctly_sized_bytes(monkeypatch):
    registry = SessionIdentityRegistry()

    values = [registry.reserve() for _ in range(200)]

    for value in values:
        assert isinstance(value, bytes)
        assert len(value) == IDENTIFIER_WIDTH_BYTES
        assert registry.is_live(value)
    assert len(set(values)) == len(values)


def test_reserve_does_not_forbid_an_all_zero_draw(monkeypatch):
    """A finite random space is a checked-and-retried probability, not a
    mathematical impossibility claim: an all-zero draw is a perfectly
    valid 1-in-2**128 outcome and must be accepted like any other free
    value, not specially rejected."""
    registry = SessionIdentityRegistry()
    all_zero = b"\x00" * IDENTIFIER_WIDTH_BYTES
    monkeypatch.setattr("os.urandom", lambda _length: all_zero)

    result = registry.reserve()

    assert result == all_zero
    assert registry.is_live(all_zero)


def test_reserve_retries_past_a_deterministic_injected_collision(monkeypatch):
    registry = SessionIdentityRegistry()
    occupied = registry.reserve()
    free = b"\x02" * IDENTIFIER_WIDTH_BYTES
    draws = iter((occupied, free))
    call_count = 0

    def fake_urandom(_length):
        nonlocal call_count
        call_count += 1
        return next(draws)

    monkeypatch.setattr("os.urandom", fake_urandom)

    result = registry.reserve()

    assert result == free
    assert call_count == 2
    assert registry.is_live(occupied)
    assert registry.is_live(free)


def test_reserve_exhausts_at_exact_bounded_limit_without_leaking(monkeypatch):
    """Exactly `max_attempts` draws, no more, no unbounded retry, and no
    weaker fallback: exhaustion fails closed with the dedicated error and
    leaves no partial reservation behind -- only the pre-existing
    collision value remains live."""
    registry = SessionIdentityRegistry(max_attempts=5)
    always_occupied = registry.reserve()
    call_count = 0

    def fake_urandom(_length):
        nonlocal call_count
        call_count += 1
        return always_occupied

    monkeypatch.setattr("os.urandom", fake_urandom)
    live_before = registry.live_count()
    retiring_before = registry.retiring_count()

    with pytest.raises(SessionIdentityExhaustedError):
        registry.reserve()

    assert call_count == 5
    assert registry.live_count() == live_before
    assert registry.retiring_count() == retiring_before
    assert registry.is_live(always_occupied)


def test_reserve_treats_a_still_retiring_value_as_occupied(monkeypatch):
    """A released-but-not-yet-purged (retiring) value is not eligible for
    a fresh draw: it is occupied bookkeeping, not a free slot, even though
    it is no longer counted as live."""
    registry = SessionIdentityRegistry()
    retiring_value = registry.reserve()
    registry.release(retiring_value, now=0.0)
    assert registry.is_retiring(retiring_value)
    assert not registry.is_live(retiring_value)

    free = b"\x03" * IDENTIFIER_WIDTH_BYTES
    draws = iter((retiring_value, free))
    monkeypatch.setattr("os.urandom", lambda _length: next(draws))

    result = registry.reserve()

    assert result == free


def test_reserve_accepts_a_value_once_its_retirement_is_purged(monkeypatch):
    registry = SessionIdentityRegistry()
    value = registry.reserve()
    registry.release(value, now=0.0)
    purged = registry.purge_expired(now=1000.0)

    assert purged == 1
    assert not registry.is_retiring(value)

    monkeypatch.setattr("os.urandom", lambda _length: value)
    result = registry.reserve()

    assert result == value
    assert registry.is_live(value)


def test_reserve_never_admits_two_threads_into_its_transaction_at_once():
    """Proves mutual exclusion by direct measurement, not by a blocked/
    finished proxy signal (which an artificially-serializing test double
    can satisfy even with the lock removed -- a double that unconditionally
    parks every caller on the same gate blocks the second caller whether or
    not any real lock exists, so it cannot tell the two cases apart).
    Instead this counts how many threads are simultaneously inside
    `reserve()`'s check-and-insert transaction: an instrumented
    `_is_occupied_locked` increments a shared counter on entry, sleeps
    briefly (widening the window a real race would need), then decrements
    it, recording the peak observed concurrency. A correctly-locked
    `reserve()` can never let that peak exceed 1; this is verified against
    a deliberately-unlocked control (a registry with its lock swapped for
    a no-op) to confirm the instrumentation would actually catch the
    regression, not just happen to pass."""
    def run(use_broken_lock):
        registry = SessionIdentityRegistry()
        if use_broken_lock:
            class NullLock:
                def __enter__(self):
                    return self

                def __exit__(self, *_exc_info):
                    return False

            registry._lock = NullLock()

        concurrent = 0
        peak = [0]
        counter_lock = threading.Lock()
        original_is_occupied = registry._is_occupied_locked

        def instrumented_is_occupied(value):
            nonlocal concurrent
            with counter_lock:
                concurrent += 1
                peak[0] = max(peak[0], concurrent)
            time.sleep(0.03)
            with counter_lock:
                concurrent -= 1
            return original_is_occupied(value)

        registry._is_occupied_locked = instrumented_is_occupied

        thread_count = 8
        barrier = threading.Barrier(thread_count)

        def worker():
            barrier.wait(timeout=5.0)
            registry.reserve()

        threads = [
            threading.Thread(target=worker) for _ in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
        return peak[0]

    assert run(use_broken_lock=False) == 1
    assert run(use_broken_lock=True) > 1, (
        "the deliberately-unlocked control did not exhibit real concurrent "
        "entry -- this instrumentation would not catch a missing lock"
    )


def test_claim_adds_a_reference_to_an_already_live_value():
    registry = SessionIdentityRegistry()
    value = registry.reserve()

    registry.claim(value)

    assert registry.is_live(value)
    # Two live references now: one release() must not be enough to retire
    # it.
    registry.release(value, now=0.0)
    assert registry.is_live(value)
    assert not registry.is_retiring(value)


def test_claim_rejects_a_merely_retiring_value(monkeypatch):
    """F5 hardening: claim() must never silently revive a retiring value.
    The only legitimate caller
    (`aismixer_secure._reused_or_fresh_assembly_namespace`) only ever
    claims a value it has just confirmed is still live, so retirement
    revival is not a real use case -- it is a bug/misuse signal and must
    raise, not silently fabricate a reservation this registry never
    checked for collisions at the point of revival."""
    registry = SessionIdentityRegistry()
    value = registry.reserve()
    registry.release(value, now=0.0)
    assert registry.is_retiring(value)

    with pytest.raises(ValueError):
        registry.claim(value)

    # Rejection must not mutate state: still retiring, not live, not
    # purged early.
    assert registry.is_retiring(value)
    assert not registry.is_live(value)


def test_claim_rejects_an_entirely_unknown_value():
    registry = SessionIdentityRegistry()
    never_seen = b"\x44" * IDENTIFIER_WIDTH_BYTES

    with pytest.raises(ValueError):
        registry.claim(never_seen)

    assert not registry.is_live(never_seen)
    assert not registry.is_retiring(never_seen)


def test_release_decrements_reference_count_before_retiring():
    registry = SessionIdentityRegistry()
    value = registry.reserve()
    registry.claim(value)
    registry.claim(value)  # refcount now 3

    registry.release(value, now=0.0)
    assert registry.is_live(value)
    assert not registry.is_retiring(value)

    registry.release(value, now=0.0)
    assert registry.is_live(value)
    assert not registry.is_retiring(value)

    registry.release(value, now=0.0)
    assert not registry.is_live(value)
    assert registry.is_retiring(value)


def test_release_of_a_value_with_no_live_reference_is_a_noop():
    registry = SessionIdentityRegistry()
    never_reserved = b"\x55" * IDENTIFIER_WIDTH_BYTES

    registry.release(never_reserved, now=0.0)

    assert not registry.is_live(never_reserved)
    assert not registry.is_retiring(never_reserved)


def test_release_never_affects_a_different_live_value():
    """A released allocation must never release another live session's
    reservation: releasing one value leaves every other live value's
    status completely untouched."""
    registry = SessionIdentityRegistry()
    value_a = registry.reserve()
    value_b = registry.reserve()
    assert value_a != value_b

    registry.release(value_a, now=0.0)

    assert not registry.is_live(value_a)
    assert registry.is_live(value_b)


def test_purge_expired_removes_only_entries_past_their_retirement_window():
    registry = SessionIdentityRegistry(retirement_seconds=30.0)
    early = registry.reserve()
    late = registry.reserve()
    registry.release(early, now=0.0)
    registry.release(late, now=10.0)

    purged_count = registry.purge_expired(now=31.0)

    assert purged_count == 1
    assert not registry.is_retiring(early)
    assert registry.is_retiring(late)

    purged_count = registry.purge_expired(now=41.0)

    assert purged_count == 1
    assert not registry.is_retiring(late)


class _ItemsCountingDict(dict):
    """Tracks how many times `.items()` is called on this exact dict, so a
    test can prove a full-dict scan did or did not happen without relying
    on timing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.items_call_count = 0

    def items(self):
        self.items_call_count += 1
        return super().items()


def test_purge_expired_does_not_scan_every_retiring_entry_when_none_are_due():
    """Regression for F3: the earlier implementation iterated
    `_retiring_until.items()` in full on every call -- turning routine
    per-packet cleanup (`purge_expired()` runs inside
    `SecureState.cleanup_expired_sessions()`, which every accepted DATA
    packet reaches) into O(retiring-set-size) work even when nothing is
    due. With 1000 entries retiring but none yet due, `purge_expired()`
    must do no full-dict scan at all."""
    registry = SessionIdentityRegistry(retirement_seconds=30.0)
    for _ in range(1000):
        value = registry.reserve()
        registry.release(value, now=0.0)
    assert registry.retiring_count() == 1000

    counting_dict = _ItemsCountingDict(registry._retiring_until)
    registry._retiring_until = counting_dict

    purged = registry.purge_expired(now=5.0)

    assert purged == 0
    assert counting_dict.items_call_count == 0
    assert registry.retiring_count() == 1000


def test_purge_expired_finds_due_entries_without_scanning_unexpired_ones():
    """Same proof, but with a real mix of due and not-yet-due entries: the
    due ones must still be found and purged, and the scan must still
    never enumerate the full retiring set."""
    registry = SessionIdentityRegistry(retirement_seconds=30.0)
    due_values = []
    for _ in range(10):
        value = registry.reserve()
        registry.release(value, now=0.0)
        due_values.append(value)
    not_due_values = []
    for _ in range(1000):
        value = registry.reserve()
        registry.release(value, now=20.0)
        not_due_values.append(value)

    counting_dict = _ItemsCountingDict(registry._retiring_until)
    registry._retiring_until = counting_dict

    purged = registry.purge_expired(now=31.0)

    assert purged == 10
    assert counting_dict.items_call_count == 0
    for value in due_values:
        assert not registry.is_retiring(value)
    for value in not_due_values:
        assert registry.is_retiring(value)


def test_purge_expired_ignores_a_heap_entry_stale_relative_to_retiring_until():
    """Defensive lazy-deletion proof: `purge_expired()` validates each
    popped heap entry against `_retiring_until` (the authoritative record)
    before treating it as a real expiry, discarding anything that no
    longer matches rather than assuming the heap and the dict always
    agree. `claim()`'s F5 hardening (it now rejects a merely-retiring
    value) means this cannot arise through this registry's own public API
    today -- `reserve()` never draws an already-retiring value, and
    `claim()` can no longer revive one -- but the check remains cheap,
    correct safety-in-depth against a future internal change, exercised
    here directly via white-box manipulation of the authoritative
    record."""
    registry = SessionIdentityRegistry(retirement_seconds=30.0)
    value = registry.reserve()
    registry.release(value, now=0.0)  # pushes (30.0, value) onto the heap

    # Simulate the authoritative record having moved on to a later
    # deadline without the stale (30.0, value) heap entry being removed --
    # exactly the shape a stale heap entry has.
    registry._retiring_until[value] = 80.0

    purged = registry.purge_expired(now=31.0)

    assert purged == 0
    assert registry.is_retiring(value)
    assert registry._retiring_until[value] == 80.0


def test_memory_use_is_bounded_by_churn_and_retirement_not_unbounded_history():
    """The registry must not accumulate an ever-growing historical record:
    reserving and releasing many values over time, with retirement
    windows periodically purged, must not leave the live/retiring sets
    growing without bound relative to how many are actually concurrently
    outstanding."""
    registry = SessionIdentityRegistry(retirement_seconds=5.0)
    now = 0.0
    outstanding = []

    for _ in range(500):
        value = registry.reserve()
        outstanding.append(value)
        if len(outstanding) > 10:
            registry.release(outstanding.pop(0), now)
        now += 1.0
        registry.purge_expired(now)

    # Only the still-outstanding live values, plus whatever is within one
    # retirement window of release, may remain -- not all 500 ever minted.
    assert registry.live_count() <= 11
    assert registry.retiring_count() <= 6


def test_default_registry_uses_the_module_level_bounded_attempt_budget(
    monkeypatch,
):
    registry = SessionIdentityRegistry()
    always_occupied = registry.reserve()
    call_count = 0

    def fake_urandom(_length):
        nonlocal call_count
        call_count += 1
        return always_occupied

    monkeypatch.setattr("os.urandom", fake_urandom)

    with pytest.raises(SessionIdentityExhaustedError):
        registry.reserve()

    assert call_count == MAX_GENERATION_ATTEMPTS


def test_retirement_seconds_constant_documents_a_concrete_margin():
    """`RETIREMENT_SECONDS` is grounded in a concrete, known value (the
    only production `AIVDMAssembler` instantiation's default group
    timeout), not an arbitrary grace period -- this pins the constant so
    a future change is a deliberate, reviewed decision."""
    assert RETIREMENT_SECONDS == 30.0
