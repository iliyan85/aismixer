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


def test_claim_cancels_pending_retirement_and_restores_live_status():
    registry = SessionIdentityRegistry()
    value = registry.reserve()
    registry.release(value, now=0.0)
    assert registry.is_retiring(value)

    registry.claim(value)

    assert registry.is_live(value)
    assert not registry.is_retiring(value)


def test_claim_on_an_unknown_value_admits_it_as_a_fresh_live_entry():
    registry = SessionIdentityRegistry()
    never_seen = b"\x44" * IDENTIFIER_WIDTH_BYTES

    registry.claim(never_seen)

    assert registry.is_live(never_seen)
    # A single claim() is one reference: one release() must be enough to
    # move it to retiring, not leave it live from a phantom extra count.
    registry.release(never_seen, now=0.0)
    assert registry.is_retiring(never_seen)


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
