"""Process-wide, in-memory registry for UDPSEC's opaque internal session
identifiers (`session_handle`, `assembly_namespace`).

These identifiers are not credentials, not DATA locators, not wire fields,
and not durable external IDs -- see `aismixer_secure.LogicalSession` for
what they actually mean. This module's only job is to guarantee that no
two identifiers with overlapping *live* lifetimes are ever the same value,
across every `SecureState` owner sharing one process (including two owners
each running on their own OS thread, which this project's own test suite
genuinely exercises for multi-listener isolation).

Design summary
--------------

`reserve()` draws a fixed-width random value from `os.urandom` and checks
it against every currently live-or-retiring reservation, retrying a
bounded number of times, all while holding one short internal lock -- so
the draw-check-insert sequence is one atomic transaction, not three
separate steps a second caller could interleave with. This is deliberately
the same standard of guarantee `session_locator` generation already uses
(`aismixer_secure.generate_session_locator`): collision is a matter of
128-bit birthday-bound probability, checked and retried, never an
unqualified claim that a finite random space cannot repeat.

`release()`/`claim()` implement reference counting, not a simple boolean
reservation, specifically because `assembly_namespace` needs it:
`session_handle` has exactly one owner for its whole life and can be
released the instant its `LogicalSession` is removed, but a downstream
`AIVDMAssembler` group can still be alive under an `assembly_namespace`'s
assembler key for a bounded time *after* the owning session is gone (see
`RETIREMENT_SECONDS` below), and a same-relation/same-station replacement
transfers (claims) an existing namespace rather than drawing a fresh one.
Reference counting turns "is this value still meaningfully in use" into a
question the registry can answer precisely, rather than one this module
would otherwise have to guess at with a timer alone.

What is NOT guaranteed: uniqueness across processes (this is in-memory,
process-local, non-durable -- a restart starts every counter and every
reservation over), or uniqueness against a value from a *previous* process
lifetime. Within one process, this registry does not keep an unbounded
historical record either: a released value is retired for
`RETIREMENT_SECONDS` (bounding it to realistic session churn during that
window) and then purged, not kept forever.
"""

from __future__ import annotations

import os
import threading

IDENTIFIER_WIDTH_BYTES = 16
MAX_GENERATION_ATTEMPTS = 8

# How long a fully-released (ref count zero) value stays reserved before it
# becomes eligible for reuse. This exists only for assembly_namespace: a
# downstream AIVDMAssembler group keyed by that value can outlive the
# SecureState session that minted it. Production's only assembler
# instantiation (`core.python_data_plane`) uses AIVDMAssembler()'s default
# `timeout=1.0` second group TTL; this retirement window is a deliberate,
# documented, generous (30x) safety margin over that known value, not an
# arbitrary grace timer picked without a concrete reference point. If a
# deployment ever configures a materially longer assembler timeout, this
# constant would need to grow with it -- there is no dynamic coupling
# between the two today.
RETIREMENT_SECONDS = 30.0


class SessionIdentityExhaustedError(RuntimeError):
    """Raised when a fixed-width random draw cannot find a value free of
    every live-or-retiring reservation within the bounded attempt budget.
    Fails closed rather than falling back to a shorter value, a counter,
    or any other predictable/weaker source.
    """


class SessionIdentityRegistry:
    """Shared, lock-protected, reference-counted registry of live and
    recently-retired opaque identifier reservations.

    One collision domain serves both `session_handle` and
    `assembly_namespace`: a single dict of value -> live reference count,
    plus a single dict of value -> retirement-eligible monotonic time for
    entries whose reference count has reached zero. Using one shared
    domain (rather than two separate ones) means a `session_handle` value
    and an `assembly_namespace` value can never coincide either, which is
    a strictly stronger guarantee than either identifier's own contract
    requires, at no extra cost.
    """

    def __init__(
        self,
        width_bytes: int = IDENTIFIER_WIDTH_BYTES,
        max_attempts: int = MAX_GENERATION_ATTEMPTS,
        retirement_seconds: float = RETIREMENT_SECONDS,
    ) -> None:
        self._width_bytes = width_bytes
        self._max_attempts = max_attempts
        self._retirement_seconds = retirement_seconds
        self._lock = threading.Lock()
        self._live_refcounts: dict[bytes, int] = {}
        self._retiring_until: dict[bytes, float] = {}

    def _is_occupied_locked(self, value: bytes) -> bool:
        return value in self._live_refcounts or value in self._retiring_until

    def reserve(self) -> bytes:
        """Atomically draw and reserve one fresh, currently-unused value
        with an initial reference count of 1.

        Raises `SessionIdentityExhaustedError` after `max_attempts`
        consecutive collisions rather than reusing a live or still-
        retiring value, spinning unboundedly, or falling back to a
        shorter/predictable value.
        """
        with self._lock:
            for _ in range(self._max_attempts):
                candidate = os.urandom(self._width_bytes)
                if not self._is_occupied_locked(candidate):
                    self._live_refcounts[candidate] = 1
                    return candidate
            raise SessionIdentityExhaustedError(
                "unable to reserve a unique session identifier after "
                f"{self._max_attempts} attempts"
            )

    def claim(self, value: bytes) -> None:
        """Add one more live reference to an already-reserved value (used
        when a same-relation/same-station replacement carries an existing
        `assembly_namespace` forward instead of drawing a fresh one).

        If the value was retiring (its previous owner already released
        it, but its retirement window has not yet elapsed), claiming it
        cancels that retirement -- it is live again, not merely revived
        from history. If the value is not currently known at all (should
        not happen for a value this registry itself issued and the caller
        never lost track of), it is admitted as a fresh live entry with a
        reference count of 1 rather than raising, since the alternative
        would be to fail an otherwise-valid same-station continuity
        transfer over pure bookkeeping.
        """
        with self._lock:
            if value in self._retiring_until:
                del self._retiring_until[value]
                self._live_refcounts[value] = 1
                return
            self._live_refcounts[value] = self._live_refcounts.get(value, 0) + 1

    def release(self, value: bytes, now: float) -> None:
        """Drop one live reference. When the reference count reaches zero,
        the value moves to "retiring" (still occupied, ineligible for a
        fresh `reserve()` draw, but no longer counted as live) for
        `retirement_seconds`, then becomes eligible for `purge_expired()`
        to remove.

        Releasing a value with no live reference at all (e.g. already
        fully released) is a no-op, not an error: callers releasing
        `session_handle` unconditionally at session removal should not
        need to separately track whether they already released it.
        """
        with self._lock:
            count = self._live_refcounts.get(value)
            if count is None:
                return
            if count > 1:
                self._live_refcounts[value] = count - 1
                return
            del self._live_refcounts[value]
            self._retiring_until[value] = now + self._retirement_seconds

    def purge_expired(self, now: float) -> int:
        """Remove every retiring entry whose retirement window has
        elapsed. Intended to be called from SecureState's existing
        monotonic cleanup pass (`cleanup_expired_sessions`), not from a
        dedicated thread or on every packet -- this keeps the registry's
        memory bounded by realistic session churn over one retirement
        window, not by the whole process lifetime. Returns the number of
        entries purged (for tests/diagnostics only).
        """
        with self._lock:
            expired = [
                value
                for value, retire_at in self._retiring_until.items()
                if now >= retire_at
            ]
            for value in expired:
                del self._retiring_until[value]
            return len(expired)

    def is_live(self, value: bytes) -> bool:
        with self._lock:
            return value in self._live_refcounts

    def is_retiring(self, value: bytes) -> bool:
        with self._lock:
            return value in self._retiring_until

    def live_count(self) -> int:
        with self._lock:
            return len(self._live_refcounts)

    def retiring_count(self) -> int:
        with self._lock:
            return len(self._retiring_until)
