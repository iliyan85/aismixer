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

import heapq
import os
import threading

IDENTIFIER_WIDTH_BYTES = 16
MAX_GENERATION_ATTEMPTS = 8

# How long a fully-released (ref count zero) value stays reserved before it
# becomes eligible for reuse. This exists only for assembly_namespace: a
# downstream AIVDMAssembler group keyed by that value can outlive the
# SecureState session that minted it, and the assembler's own group
# timeout only starts once a frame actually reaches the assembler --
# NOT when UDPSEC admits it -- so this margin must be measured against
# total pipeline residence time, not the assembler's timeout alone (a
# prior revision of this comment only accounted for the latter, which an
# independent audit correctly flagged as measuring against the wrong
# clock: none of the ingress/processing queues between admission and the
# assembler impose a maximum wait, only a maximum depth, so a frame could
# in principle sit queued for longer than the assembler timeout alone
# would suggest is safe). This is why
# `core.python_data_plane.MAX_INGRESS_FRAME_AGE_SECONDS` (currently 20.0)
# exists and is actually enforced (frames older than that are refused
# before ever reaching the assembler, in `PythonDataPlaneProcessor.
# _process_impl`), and why this constant is the SUM of that enforced
# bound, `AIVDMAssembler()`'s default `timeout=1.0` second group TTL, and
# a margin (9.0s) for scheduling/GC jitter -- not an independent
# multiplier applied to the assembler timeout alone. If a deployment ever
# configures a materially longer assembler timeout or a different maximum
# ingress frame age, this constant would need to grow with it -- there is
# no dynamic coupling between the three today.
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
        # `_retiring_until` remains the authoritative "is this value
        # currently retiring, and until when" lookup (used by
        # `is_retiring()`, `_is_occupied_locked()`, and `claim()`'s
        # cancel-retirement check). `_retirement_heap` is a lazy-deletion
        # min-heap of `(retire_at, value)` used only so `purge_expired()`
        # can find due entries in time proportional to how many are
        # actually due, not to how many are merely retiring (see
        # `purge_expired()` for why an insertion-ordered structure would
        # be unsafe here for the same reason it is unsafe for SecureState's
        # own expiry bookkeeping: two callers can commit `release()` in an
        # order that does not match the `now` values they were called
        # with, so heap ORDER -- not commit order -- must be authoritative).
        self._retiring_until: dict[bytes, float] = {}
        self._retirement_heap: list[tuple[float, bytes]] = []

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
        """Add one more live reference to an already-LIVE reservation
        (used when a same-relation/same-station replacement carries an
        existing, still-live `assembly_namespace` forward instead of
        drawing a fresh one).

        `value` must already be live. The only legitimate caller
        (`aismixer_secure._reused_or_fresh_assembly_namespace`) reads it
        directly off a `LogicalSession` object it has just confirmed is
        still live in `SecureState._sessions` -- so it can always prove
        liveness before calling this, and never needs claim() to revive a
        merely-retiring value or admit a value this registry never itself
        reserved. Earlier revisions did both silently; that let a caller
        fabricate a reservation this registry never checked for
        collisions, which is exactly the property this module exists to
        prevent. Raises `ValueError` for a retiring or entirely unknown
        value instead.
        """
        with self._lock:
            if value not in self._live_refcounts:
                raise ValueError(
                    "claim() target is not a currently live reservation"
                )
            self._live_refcounts[value] += 1

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

        This is NOT idempotent against a caller mistakenly releasing the
        SAME live reference twice when the count is 2 or more: a second,
        spurious release would still decrement it, potentially retiring a
        value another legitimate owner still holds. This module
        deliberately does not defend against that with a token/ownership-
        handle system -- the actual guarantee comes from the caller side:
        every release() call in this codebase is reached only through
        `SecureState`'s exact-object-identity removal checks (a
        `LogicalSession`/pending object is released at most once, exactly
        when it is the precise object being removed), never from a bare
        count or a value alone. A generic misuse-proof token system was
        judged unnecessary complexity for a single, already-disciplined
        in-process caller; do not add one without a concrete need.
        """
        with self._lock:
            count = self._live_refcounts.get(value)
            if count is None:
                return
            if count > 1:
                self._live_refcounts[value] = count - 1
                return
            del self._live_refcounts[value]
            retire_at = now + self._retirement_seconds
            self._retiring_until[value] = retire_at
            heapq.heappush(self._retirement_heap, (retire_at, value))

    def purge_expired(self, now: float) -> int:
        """Remove every retiring entry whose retirement window has
        elapsed, in time proportional to how many entries are actually
        due -- NOT to how many entries are merely retiring. A naive full
        scan of every retiring entry on every call (this module's earlier
        implementation) turns every accepted DATA packet's routine
        cleanup into O(retiring-set-size) work, and that set can genuinely
        hold thousands of entries under realistic churn; a heap ordered by
        `retire_at` lets this stop as soon as it reaches the first entry
        that is not yet due.

        The heap can contain stale entries -- a value `release()`d and
        later `claim()`ed again before its retirement window elapsed has a
        heap entry whose deadline no longer applies -- so each popped
        entry is validated against `_retiring_until` (the authoritative
        record) before being treated as a real expiry; a stale entry is
        silently discarded rather than counted or acted on. This lazy-
        deletion approach is why heap order, not insertion order, must be
        authoritative: unlike `_retiring_until`'s dict (whose iteration
        order reflects insertion, not necessarily `retire_at` order, since
        two `release()` calls can commit in an order that does not match
        the `now` values passed to them), the heap always yields entries
        in true `retire_at` order regardless of insertion order.

        Intended to be called from SecureState's existing monotonic
        cleanup pass (`cleanup_expired_sessions`), not from a dedicated
        thread or unconditionally scanning on every packet -- this keeps
        the registry's memory bounded by realistic session churn over one
        retirement window, not by the whole process lifetime. Returns the
        number of entries actually purged (for tests/diagnostics only;
        stale, discarded heap entries are not counted).
        """
        with self._lock:
            purged = 0
            while self._retirement_heap:
                retire_at, value = self._retirement_heap[0]
                if retire_at > now:
                    break
                heapq.heappop(self._retirement_heap)
                if self._retiring_until.get(value) != retire_at:
                    # Stale: superseded by a later claim()/release() cycle,
                    # or already removed. Not a real expiry.
                    continue
                del self._retiring_until[value]
                purged += 1
            return purged

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
