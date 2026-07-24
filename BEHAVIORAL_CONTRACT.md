# aismixer Behavioural Contract

## 1. Scope

This document defines the currently tested Python processing contract for:

- ingress event acceptance;
- AIS NMEA sentence extraction;
- multipart assembly;
- TAG metadata ownership;
- deduplication;
- secure-ingress local replay, session, and nonce state;
- routing snapshot use; and
- forwarding boundaries.

It is the reference contract for differential testing of a future native
processor. It is not a full AIS protocol specification, a storage or analytics
specification, a spoof-detection specification, or a native ABI.

## 2. Event boundary

An `IngressEvent.raw_line` must satisfy `isinstance(raw_line, str)` to enter
processing. The producer and queue boundary remains `IngressEvent`; the
forwarding consumer adapts each accepted event to an immutable, bytes-based
`IngressFrame`. This includes subclasses of `str`, and the legacy text adapter
uses an explicit mode that preserves surrogate code points. A non-string value
produces no frame and is ignored before routing, extraction, assembly, or
deduplication, and later queued events must continue to be processed. In
particular, the forwarding core does not decode `bytes` implicitly.

An accepted string may contain no accepted AIS sentence. Such an event still
follows the normal event-level routing snapshot and match when routing is
configured, then produces no output after extraction; it does not terminate
the consumer.

## 3. Accepted sentence extraction

The forwarding core scans the accepted frame payload as bytes and extracts
`VDM` and `VDO` sentences for the supported AIS talker identifiers `AI`, `AB`,
`AD`, `AN`, `AR`, `AS`, `AT`, `AX`, and `BS`. Each extracted sentence must
begin with `!`, use one of those talker/family combinations, and end with `*`
followed by exactly two hexadecimal characters. Extraction requires this
checksum-field syntax but does not recompute or verify the NMEA checksum value.

Input may contain surrounding text and multiple accepted sentences. Matches
must be processed in input order. A backslash-delimited TAG block is associated
with a sentence only when its closing backslash immediately precedes that
sentence. Associated TAG fields and NMEA fragment metadata are parsed once from
their byte spans, decoding only the required slices according to the frame's
explicit text mode. TAG association does not imply validation of the TAG
checksum.

## 4. Multipart assembly identity

The public assembler identity is exactly:

```python
AssemblyKey = tuple[str, str, str, int]
# (source_identity, sequential_id, channel, declared_total)
```

`source_identity` is the ingress assembler/source identity. `sequential_id` and
`channel` are the exact NMEA field strings, including an empty sequential ID.
`declared_total` is the NMEA total-fragment field parsed as an integer. The
fragment ordinal determines the occupied slot but is not another key field.

TAG `g` is metadata and does not participate in `AssemblyKey`. Its group ID,
part, and total fields are not promoted into assembler identity.

The production forwarding loop passes each `ParsedSentence` to
`feed_parsed_outcome()`, which enters the same established assembler lifecycle.
The Python compatibility implementation materializes the exact matched
sentence span as a string; pending groups and `AssemblyOutcome.sentences`
continue to store and return sentence strings. The public string-based
assembler API remains available.

## 5. Multipart lifecycle

A structurally valid input with declared total `1` and current ordinal `1`
takes a state-free fast path. `feed_outcome()` immediately returns
`AssemblyStatus.SINGLE` with `group_key=None`, `sentences=(line,)`, and
`discarded_keys=()`, preserving the exact original input string. This path does
not invoke the assembler clock and does not create, expire, discard, or
otherwise mutate any multipart generation. Single-only traffic therefore does
not trigger multipart expiry cleanup; pending generations remain unchanged
until a later multipart operation applies the normal lifecycle rules.

By default, `max_fragments_per_group=None` places no limit on a multipart
declaration, and `max_pending_groups=None` leaves the number of pending groups
unbounded. Each option may instead be a positive integer. A structurally valid
multipart declaration above `max_fragments_per_group` returns
`AssemblyStatus.LIMIT_EXCEEDED` with `group_key=None`, empty sentences, and no
discarded keys. This rejection is applied before key construction, clock use,
expiry cleanup, or multipart-state access. Structurally invalid input remains
`INVALID`, and the single-sentence fast path remains accepted when the fragment
limit is `1`.

For accepted input with a declared total greater than `1`, any valid ordinal
may open a multipart generation, and fragments may arrive fully out of order.
A generation completes only when it contains one unique fragment for every
ordinal from `1` through the declared total. Completed sentences must be
returned in ordinal order. Successful completion removes the assembler
generation; a later fragment with the same `AssemblyKey` starts a fresh
generation.

An exact repeat of the full sentence at an occupied ordinal is idempotent and
does not refresh assembly TTL. Forward-loop metadata observations carried by
such a duplicate may still refine that generation's metadata contexts. A
different full sentence at an occupied ordinal is a conflict: it invalidates
the whole generation, and the conflicting arrival is not retained as the first
fragment of a replacement generation.

TTL is measured from the most recent accepted unique progress. A generation is
live while `age < timeout` and expires when `age >= timeout`; exact duplicates
do not refresh that time. Matching-key expiry is applied before the current
fragment, so that fragment may open a fresh generation.

`max_pending_groups` is one instance-wide, process-local cap shared by all
source identities and multipart keys. It applies only when a fragment must
create a new group. When capacity is full, all groups expired at the current
time are removed before any live group is evicted. If capacity remains full,
exactly one live victim is selected by the smallest
`(group.last_progress_at, AssemblyKey)`: the least-recently-progressed group
wins, with `AssemblyKey` ordering as the deterministic timestamp tie-break.
Duplicates, unique progress in an existing group, conflicts, and completion do
not cause capacity eviction.

`feed_outcome()` exposes the lifecycle statuses `invalid`, `single`,
`limit_exceeded`, `pending`, `duplicate`, `conflict`, and `complete`. Its
`discarded_keys` is a deterministically sorted tuple of every `AssemblyKey`
discarded by that call, including a conflicting or expired matching generation,
any generation removed by an opportunistic expiry sweep, and a live
capacity-eviction victim.

`cleanup_expired(now=None)` returns a deterministically sorted tuple of every
group it removes, using the injected clock only when `now` is omitted.
`reset()` returns all pending keys in deterministic sorted order and clears the
group state; both methods return `()` when they remove nothing. Each reset call,
including an empty reset, increments the reset-call counter. Reset also counts
the groups it discards, but does not count them as expired or capacity-evicted,
and it preserves the configured timeout, clock, limits, cumulative statistics,
and peak statistics.

`stats()` returns an immutable point-in-time `AssemblerStats` snapshot with
`invalid`, `single`, `limit_exceeded`, `pending`, `duplicates`, `conflicts`,
`completed`, `expired`, `capacity_evicted`, `reset_discarded`, `resets`,
`current_groups`, `peak_groups`, `current_fragments`, and `peak_fragments`.
Exactly one outcome counter advances per `feed_outcome()` call; lifecycle
counters advance only for their corresponding removal reason. Reading
statistics neither invokes the clock nor performs cleanup, and earlier
snapshots do not change.

## 6. Blank sequential-ID compatibility

A blank NMEA sequential ID remains supported and must follow the same
out-of-order, duplicate, conflict, ordering, progress, and TTL rules as any
other exact sequential-ID string.

This is an intentional compatibility limitation. Within one live TTL
correlation window, fragments from multiple physical transmissions with the
same source identity, blank sequential ID, channel, and declared total may be
combined into one synthetic logical group. Completion is not proof that those
fragments share a physical transmission origin, and this ambiguity is not
considered solved.

## 7. Multipart TAG `s`

Multipart `s` context is keyed by `AssemblyKey`. An earlier-fragment `s` is
cached only while the group is pending or receiving an exact duplicate and the
same arrival has a TAG `g` that the existing parser recognizes structurally as
a `(part, total, group_id)` tuple. This condition does not establish agreement
between TAG `g` and the NMEA fragment fields.

A non-empty completion-arrival `s` must override an earlier cached `s`. When
completion carries no non-empty `s`, the cached earlier value becomes the
ingress-source candidate. Conflict, expiry, and capacity eviction discard
context for their discarded generation. Normal completion consumes the context
after processing, including a no-route completion or a completion suppressed by
deduplication.

Final precedence among configured station ID, configured input identity or
alias, ingress source metadata, and remote-IP fallback remains governed by the
existing `choose_s_value()` source policy.

## 8. Multipart TAG `c`

The final ingress `c` value is usable only when it is non-empty,
`str.isdigit()` is true, and conversion by `int()` succeeds. A digit-like value
such as `²`, for which `isdigit()` is true but `int()` raises, is an invalid
candidate and must not terminate forwarding. Usable values are converted to
integers and compared numerically, so leading zeroes normalize and Unicode
decimal digits accepted by `int()` remain valid. A multipart generation must
select the minimum valid observed value, independently of arrival order and of
which ordinal completes the group. An exact duplicate may lower that minimum
but must not raise it.

Conflict, expiry, and capacity eviction discard timestamp context for the
affected generation. Normal completion consumes timestamp context after
processing, including no-route and dedup-suppressed completion. If preservation
is enabled but no valid value was observed, emitted output uses the existing
server-time fallback. If preservation is disabled, ingress timestamps are
ignored and the server-time fallback is used.

A valid multipart `c:0` must be preserved as `0`. Single-sentence `c:0` retains
the existing compatibility behaviour of falling back to server time. This
single/multipart asymmetry is intentional in this contract.

## 9. Multipart TAG `g`

An ingress group-ID candidate must be non-empty and satisfy `str.isdigit()`.
Candidate agreement uses exact string equality and does not normalize through
integer conversion: for example, `001` and `1` are distinct observations.

With preservation enabled, exactly one distinct observed group ID must be
preserved. Zero observations or two or more distinct observations must cause a
new group ID to be generated. Metadata disagreement is sticky for the live
generation and does not invalidate otherwise valid NMEA assembly. The generated
ID must be created once per completed logical group, and every emitted fragment
of that group must use the same output ID. With preservation disabled, a new ID
must always be generated.

Conflict, expiry, capacity eviction, and normal completion clean group-ID
context according to the assembler generation lifecycle, including no-route
and dedup-suppressed completion. Ingress TAG-`g` part and total fields do not
participate in `AssemblyKey`, and the forwarding core does not validate their
consistency against the NMEA ordinal and total.

## 10. Deduplication

The logical key for a single sentence is its exact extracted NMEA sentence
string. The logical key for a multipart group is the ordinal-ordered tuple of
its exact extracted NMEA sentence strings. Ingress TAG metadata is therefore
not part of either key.

Deduplication is group-atomic for multipart data: the decision must be made
once for the logical tuple before fragment emission. An exact repeated tuple is
suppressed in full. A tuple changed in any fragment is a distinct group and is
emitted in full when otherwise eligible.

A dedup entry is live while `age < ttl` and expires at `age >= ttl`. A rejected
duplicate does not refresh the insertion time. Legacy/no-table forwarding uses
one global deduplication scope. Routed forwarding uses each target ID as an
independent logical-key scope, so a group already seen by one target may still
be new to another target. Ingress source identity does not create an additional
dedup scope for that target.

By default, `max_entries=None` leaves the retained entry count unbounded. A
positive `max_entries` applies one instance-wide, process-local cap shared by
the legacy global scope and every explicit target scope, and by single-sentence
string and multipart tuple keys. Scope independence is key-identity
independence, not a separate capacity quota. Before admitting a unique key,
entries at the TTL boundary are removed. If the cache remains full, the oldest
currently live insertion is evicted deterministically. Rejecting a live
duplicate causes no capacity eviction.

`stats()` returns an immutable point-in-time `DedupStats` snapshot containing
`accepted`, `duplicates`, `expired`, `capacity_evicted`, `resets`,
`current_entries`, and `peak_entries`. Decision counters are per `is_unique()`
call and cumulative for the `Deduplicator` instance. Reading statistics neither
invokes the clock nor performs cleanup, and earlier snapshots do not change.
`reset()` clears retained entries and expiry ordering and increments `resets`,
while preserving the other cumulative counters and `peak_entries`;
`current_entries` becomes zero.

Deduplication is in-memory and process-local; this contract does not specify
durable or distributed deduplication.

## 11. Secure local state

Secure ingress has one explicit `SecureState` owner for handshake replay
records, active sessions, per-session accepted data nonces, and their
statistics. The production default is module-wide, while an isolated state
owner and clocks may be injected into a secure listener. This state is
in-memory and process-local; it is neither durable nor shared across processes.

Wall time and monotonic time have separate ownership. Wall time is used only
for externally meaningful protocol or diagnostic timestamps: the transmitted
handshake timestamp check, the pong timestamp, and existing timestamped debug
output. Handshake freshness remains inclusive at the boundary:
`abs(wall_now - transmitted_timestamp) <= 30`. Monotonic time owns handshake
replay TTL, session creation and last-seen times, session TTL, data-nonce TTL,
and local capacity ordering. Each allowed received packet uses one monotonic
observation for all of that packet's local-state decisions. Network policy is
applied first; a denied packet performs no cryptographic work, state mutation,
session cleanup, or secure-state clock read.

Every process-local TTL uses the same exact boundary: state is live while
`age < ttl` and expires when `age >= ttl`. A duplicate handshake replay key or
data nonce does not refresh its expiry. Wall-clock changes do not expire,
revive, or extend replay, session, or nonce state.

Handshake replay identity is exactly the value produced by
`build_handshake_replay_key(station_id, timestamp, signature)`; the network
address is not part of that identity. A verified handshake consumes a newly
accepted replay key after timestamp, authorization, and signature validation
but before session installation. A later server-side failure does not remove
that key. The replay set retains at most `HANDSHAKE_REPLAY_MAX` records, expires
only its ordered front prefix during admission, and evicts the oldest live
record deterministically when capacity remains full.

An active session is identified by the exact peer socket address and retains
its authenticated station ID, AES-GCM owner, monotonic creation and last-seen
times, and a private data-nonce set. Sessions are ordered from least to most
recently seen. Installation and valid activity place a session at the
most-recent end; only a valid matching keepalive or a fully validated secure
data or ping packet counts as activity. Invalid, malformed, mismatched,
expired, or replayed traffic does not touch the session.

After network policy accepts any packet, including a handshake or an unknown
packet type, the expired least-recent session prefix is removed before
packet-type-specific state handling. The process may physically retain silent
expired sessions until later allowed traffic, but an expired directly
addressed session is never treated as active. At most `SESSION_MAX` sessions
are retained. A successful same-address handshake replaces the live session
and discards its nonce state without evicting an unrelated session. For a new
address, expired sessions are removed before capacity is considered; if still
full, exactly the least-recently-seen live session is evicted. Equal timestamps
are resolved by the deterministic activity order.

State operations that receive a session object first require exact retained
object identity at the supplied or stored address before performing lifecycle
cleanup. An already replaced, capacity-evicted, or otherwise removed handle is
rejected without cleanup, ordering changes, nonce access, or statistics
changes. A handle that is current when the operation begins still undergoes
normal exact-boundary expiry and is removed and accounted once if expired.

Secure-data nonce identity is the exact 12-byte nonce within its owning
session. Identical bytes in different sessions are independent. A live replay
is rejected before decryption and does not touch the session. A new nonce is
retained only after decryption, JSON decoding, source-ID matching, and message
type and required-field validation; it is recorded before the session is
touched and before the ping or NMEA action. Each nonce set retains at most
`DATA_NONCE_MAX_PER_SESSION` records, expires only its ordered front prefix,
and evicts the oldest live nonce deterministically when capacity remains full.
Replacing, expiring, or capacity-evicting a session discards all nonce state
owned by that session.

`stats()` returns an immutable point-in-time `SecureStateStats` snapshot. It
reports accepted, rejected or replayed, expired, capacity-evicted, created,
replaced, touched, and owning-session-discarded lifecycle counts as applicable,
plus current and peak handshake-replay, session, and data-nonce counts. Every
removed record has exactly one removal reason. Reading statistics invokes
neither clock, performs no cleanup, exposes no mutable state, and does not
change an earlier snapshot.

This section governs only process-local secure state. It does not redefine
secure packet formats, cryptographic algorithms, the signed handshake
transcript, session-key derivation, or `nmea_sproxy` protocol compatibility.

## 12. Routing snapshot boundary

When routing state is present, the forwarding consumer must acquire one
immutable routing snapshot per accepted string `IngressEvent`. If that snapshot
contains a table, the event source must be matched once. A non-string
`raw_line` acquires no snapshot and performs no match.

All accepted sentences extracted from one event must use the same match result.
A routing-table replacement during processing affects the next event, not the
event already in progress. A missing table, including a snapshot whose table is
`None`, selects legacy forwarding and global deduplication mode.

## 13. Forwarding and cleanup

For emitted multipart output, the first fragment receives the primary `c`, `s`,
and `g` TAG metadata. Continuation fragments receive the existing continuation
form containing `g` without repeating primary `c` or `s`.

Normal multipart completion consumes its metadata contexts even when no route
matches or deduplication suppresses all output. Every key reported through an
assembler outcome's `discarded_keys` must remove the forwarding core's cached
multipart `s`, `c`, and `g` contexts before metadata from the current arrival is
observed. If the forwarding core directly invokes `cleanup_expired()` or
`reset()`, it must apply their returned keys through the same cleanup path.
External assembler callers are likewise responsible for consuming returned
lifecycle keys to synchronize metadata they own.

Fragments are sent sequentially. Send-failure semantics were not redesigned:
this contract does not guarantee transactional delivery, rollback, replay, or
recovery after a partial multi-fragment send.

## 14. Explicit limitations and deferred decisions

The following boundaries are compatibility limitations or deferred decisions,
not additional guarantees:

1. Blank sequential IDs retain the cross-transmission ambiguity described in
   section 6 for the live TTL correlation window.
2. TAG-`g` part and total consistency is neither assembler identity nor checked
   against the NMEA part and total by the forwarding core.
3. The forwarding consumer defensively rejects non-string `raw_line` values,
   but this contract does not redefine upstream secure-ingress JSON schema
   validation.
4. Single-sentence and multipart `c:0` behaviour is intentionally not unified.
5. Send-failure recovery and transactional multi-fragment delivery remain out
   of scope.
6. Durable storage, AIS semantic decoding, analytics, and spoof detection are
   not part of this contract.
7. Extraction checks checksum-field syntax but does not validate checksum
   arithmetic.

## 15. Native implementation conformance

A future native processor should be checked through differential tests against
the Python reference for:

- ordered output sentences and TAG metadata;
- lifecycle outcome status and deterministic discarded keys;
- timestamp and group-ID selection;
- single and multipart deduplication decisions;
- routing targets; and
- explicit no-output cases.

Conformance does not define or require a C or C++ API or ABI.

## 16. Campaign A baseline

- Final branch: `main`.
- Final full-suite result: `765 passed, 18 skipped in 10.30s` (783 collected).
- Baseline date: 2026-07-22.
- Final commit immediately preceding this task:
  `48b1b09 Harden forward loop against non-string ingress payloads`.
- This document and the regression-test naming/coverage cleanup introduce no
  production behaviour change and select no new policy.

This contract was consolidated at the end of Campaign A.

## 17. Campaign B closure baseline

- Closure snapshot date: 2026-07-24.
- Branch: `main`.
- Audited source commit:
  `15a594501b0acbfa07e21b79fe863c22e1d07a4a` (`15a5945`).
- Environment: Python 3.14.5 on Windows 11
  (`Windows-11-10.0.26200-SP0`, AMD64).
- Focused results:
  - deduplication: `39 passed`;
  - multipart assembly and forwarding integration: `173 passed`;
  - secure state and protocol helpers: `222 passed`;
  - proxy/service compatibility: `94 passed`.
- Final full-suite result: `919 passed, 18 skipped, 0 failed`
  (937 collected).
- `git diff --check`: passed.
- This is a Campaign B closure snapshot, not a guarantee that future test
  counts will remain identical.
