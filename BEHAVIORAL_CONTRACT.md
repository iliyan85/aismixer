# aismixer Behavioural Contract

## 1. Scope

This document defines the currently tested Python processing contract for:

- ingress frame production and compatibility-event acceptance;
- AIS NMEA sentence extraction;
- multipart assembly;
- TAG metadata ownership;
- deduplication;
- secure-ingress local replay, session, and nonce state;
- routing snapshot use;
- processor and runtime-queue lifecycle;
- forwarding boundaries; and
- process-local runtime statistics.

It is the reference contract for differential testing of a future native
processor. It is not a full AIS protocol specification, a storage or analytics
specification, a spoof-detection specification, or a native ABI.

## 2. Ingress frame and compatibility-event boundary

The built-in UDP and UDPSEC producers enqueue immutable `IngressFrame`
instances. Each ingress fan-in reader dequeues one private-queue item and
applies the single compatibility adapter before processing admission. A direct
`IngressFrame` is retained by object identity. An `IngressEvent` is adapted to
an `IngressFrame`; its `raw_line` must satisfy `isinstance(raw_line, str)`,
including subclasses, and its explicit legacy-text mode preserves surrogate
code points.

After coercion, direct frames and adapted compatibility events enter one common
frame-processing pipeline. Fan-in waits for processing capacity and then binds
the frame and `ProcessingSnapshot` into a `ProcessingWorkItem`; the processor
stage accepts that work item, not a raw frame or compatibility event. There is
no parallel legacy routing, scanning, parsing, assembly, metadata,
deduplication, or forwarding path.

An invalid compatibility event or any unsupported queue-item type is ignored
before processing admission, routing, extraction, assembly, or deduplication,
and later queued items must continue to be processed. In particular, a bare
`bytes` or `str` queue item is not implicitly converted; bytes must already be
owned by an `IngressFrame`.

A plain UDP receive yields one complete datagram, which is normalized into one
`IngressFrame`. The receive buffer is large enough that ordinary IPv4/IPv6 UDP
datagrams are not truncated at the application receive boundary, and the ingress
path does not intentionally line-split the datagram before frame construction.
Sentence scanning happens only after that datagram boundary has been preserved.

UDP datagrams retain their historical full-datagram normalization: decode as
UTF-8 with `errors="ignore"`, apply Python `str.strip()`, then encode the
normalized text as UTF-8 frame bytes. These frames use `UTF8_IGNORE`, including
when normalization produces an empty payload. UDPSEC NMEA payload strings are
not stripped; they use surrogate-preserving UTF-8 conversion and
`UTF8_SURROGATEPASS`.

An accepted frame may contain no accepted AIS sentence, including an empty
payload. It still follows the normal frame-level routing snapshot and match
when routing is configured, then produces no output after extraction; it does
not terminate the consumer.

A built-in plain UDP or UDPSEC ingress producer contains a recoverable per-peer
receive condition. `ConnectionResetError` and `ConnectionRefusedError` (for
example an ICMP port-unreachable response surfacing on a later receive) are
logged, produce no `IngressFrame`, are not counted as a received transport
packet or transport bytes, and the producer continues awaiting the next
datagram. Any other `OSError` propagates to runtime supervision, and
`asyncio.CancelledError` propagates unchanged. Plain and secure producers
currently apply this same receive-error policy.

## 3. Accepted sentence extraction

The Python data-plane processor scans the accepted frame payload as bytes and extracts
`VDM` and `VDO` sentences for the supported AIS talker identifiers `AI`, `AB`,
`AD`, `AN`, `AR`, `AS`, `AT`, `AX`, and `BS`. Each extracted sentence must
begin with `!`, use one of those talker/family combinations, and end with `*`
followed by exactly two hexadecimal characters. Extraction requires this
checksum-field syntax but does not recompute or verify the NMEA checksum value.

Scanner results are immutable matches containing half-open byte spans into the
original frame payload. Scanning itself neither decodes nor copies sentence or
TAG payload bytes.

Input may contain surrounding text and multiple accepted sentences. Matches
must be processed in input order. A backslash-delimited TAG block is associated
with a sentence only when its closing backslash immediately precedes that
sentence. Associated TAG fields and NMEA fragment metadata are parsed once from
their byte spans, decoding only the required slices according to the frame's
explicit text mode. TAG association does not imply validation of the TAG
checksum. Each immutable `ParsedSentence` retains its originating frame, scan
match and spans, and parsed fragment and TAG metadata rather than a decoded
full-payload copy.

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

The Python data-plane processor passes each `ParsedSentence` to
`feed_parsed_outcome()`, which enters the same established assembler lifecycle.
The Python compatibility implementation materializes the exact matched
sentence span as a string; pending groups and `AssemblyOutcome.sentences`
continue to store and return sentence strings. The public legacy string APIs
`feed()` and `feed_outcome()` remain available.

## 5. Multipart lifecycle

A structurally valid input with declared total `1` and current ordinal `1`
takes a state-free fast path through either `feed_outcome()` or
`feed_parsed_outcome()`. Both return `AssemblyStatus.SINGLE` with
`group_key=None` and `discarded_keys=()`. The legacy `feed_outcome()` result is
`sentences=(line,)` and preserves the exact original input string object; the
parsed path materializes the exact matched sentence span as its one sentence
string. This path does not invoke the assembler clock and does not create,
expire, discard, or otherwise mutate any multipart generation. Single-only
traffic therefore does not trigger multipart expiry cleanup; pending
generations remain unchanged until a later multipart operation applies the
normal lifecycle rules.

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

`feed_outcome()` and `feed_parsed_outcome()` expose the lifecycle statuses
`invalid`, `single`, `limit_exceeded`, `pending`, `duplicate`, `conflict`, and
`complete`. Their `discarded_keys` value is a deterministically sorted tuple of
every `AssemblyKey` discarded by that call, including a conflicting or expired
matching generation, any generation removed by an opportunistic expiry sweep,
and a live capacity-eviction victim.

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
Exactly one normal outcome counter advances per call to either
`feed_outcome()` or `feed_parsed_outcome()`; lifecycle counters advance only
for their corresponding removal reason. Reading statistics neither invokes
the clock nor performs cleanup, and earlier snapshots do not change.

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
participate in `AssemblyKey`, and the processor does not validate their
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
one global deduplication scope. Routed forwarding uses each process-local
numeric `EgressTargetId` as an independent logical-key scope, so a group already
seen by one target may still be new to another target. Ingress source identity
does not create an additional dedup scope for that target. Routing-generation
changes do not reset global or per-target deduplication state.

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
records, pending sessions, active sessions, the accepted data nonces privately
owned by each pending or active session, and their statistics. The production
default is module-wide, while an isolated state owner and clocks may be
injected into a secure listener. This state is in-memory and process-local; it
is neither durable nor shared across processes.

UDPSEC is protocol version 2 (`UDPSEC_PROTOCOL_VERSION = 2`). There is no
negotiation or downgrade: a ClientHello or ServerHello whose declared version
is not exactly `2` fails closed, and the version is itself part of the
authenticated handshake transcript (the client digest, the server auth
digest, and the session transcript hash all bind it). Revision 1 wire
compatibility does not exist.

The V2 DATA wire format is epoch-aware: this is an evolution of the V2 wire
format, not a new protocol version. The DATA frame carries a one-byte
plaintext epoch selector and the full 32-bit epoch generation is bound into
the AEAD associated data, which lets the same established `LogicalSession`
and `session_locator` carry more than one directional traffic-key epoch
during a bounded, authenticated in-session refresh (see the dedicated
subsection below). This is NOT wire-compatible with pre-refresh V2 binaries:
a DATA frame without the epoch selector, or with the old locator-only
associated data, fails current authentication and admission -- there is no
legacy parser, negotiation, version fallback, or silent downgrade -- so
`aismixer` and `nmea_sproxy` must be upgraded together.

An established relation is internally represented as three separate
ownership layers: a `LogicalSession` (authenticated station identity, its
endpoint token, two distinct process-local identifiers described below,
`created_at`/`last_seen`, and its at-most-one `pending_epoch` /
`retiring_epoch` refresh transition slots), the `LogicalSession`'s
`current_epoch` (the directional AES-GCM owners, the private data-nonce
set, and the epoch `generation` plus, for a refresh-derived epoch, the
confirmed refresh `transaction_id`), and the `LogicalSession`'s `path_state`
(`active_path`, the address ordinary established-session replies are sent
to, plus its at-most-one `candidate_path` / `retired_path` migration slots
and a monotonic `path_generation` counter -- see the server-side path
migration subsection below). An in-session epoch refresh replaces
`current_epoch` on the exact same `LogicalSession` object -- its
`_session_key`, `session_locator`, `session_handle`, `assembly_namespace`,
`station_id`, `created_at`, `path_state` object identity and its
`active_path`, listener ownership, registry reservations, and the
`sessions_created`/`sessions_replaced` counters are all untouched. An
in-session epoch refresh authorizes no path change and a server-side path
migration authorizes no epoch/key change; the two mechanisms are
independent (see the conservative interaction rules in the path migration
subsection).

**Established (active) session identity is the exact combination of the
endpoint token and a server-minted, opaque 16-byte `session_locator` -- not
the remote peer tuple.** The server mints this locator during ServerHello
(bounded collision-retry against currently live locators on that
endpoint_token, failing closed on exhaustion), and it is bound into both the
server auth digest and the session transcript hash, so a client cannot be
tricked into accepting a substituted locator. The locator is a process-local,
session-lifetime lookup hint, never a credential: it is never derived from
station identity, network address, or ECDHE material, and by itself it
cannot bypass AEAD authentication, `allow_from`, active-path checks, replay
admission, or listener scoping. Every DATA packet (NMEA payload, ping, pong,
graceful close, and the four epoch refresh control messages alike) uses the
epoch-aware V2 framing: `DATA_PREFIX` (`b"NMEA-D2"`) followed by the 16-byte
`session_locator`, a one-byte epoch selector, a 12-byte nonce, and the
AES-GCM ciphertext plus tag; associated data is
`build_data_aad(session_locator, epoch_generation) = DATA_PREFIX ||
session_locator || uint32_be(epoch_generation)`, so the locator AND the
full epoch generation are cryptographically bound into every authenticated
DATA exchange in both directions, not merely used for lookup. The epoch
selector is `epoch_generation & 0xFF` -- a plaintext lookup hint only: at
most three epoch generations (retiring `G-1`, current `G`, pending `G+1`)
are ever simultaneously live for one locator and consecutive generations
have distinct low bytes, so the selector resolves to exactly one live epoch
by exact-generation match, with a single AEAD attempt and no trial
decryption. A captured epoch-`G` ciphertext re-sent with an epoch-`G+1`
selector fails authentication under either key because the generation in
the AAD no longer matches. A packet whose locator identifies no live
pending or active session on the receiving endpoint_token, or whose
selector names no live epoch on that session, is dropped without attempting
decryption or any trial across other locators/epochs. A pending relation
remains
represented the same way, minus `path_state` and minus both identifiers
below, and -- unlike an active session -- pending identity is still the
exact endpoint-token/peer-address tuple from the handshake; see below.

A `LogicalSession` carries two deliberately separate process-local
identifiers that must not be conflated. Both are opaque 16-byte values,
reserved through one shared, process-wide, in-memory
`core.session_identity_registry.SessionIdentityRegistry` instance
(`aismixer_secure._SESSION_IDENTITY_REGISTRY`), not merely drawn independently
from `os.urandom` and hoped not to collide: `reserve()` draws a fixed-width
random candidate and, under the registry's own short internal lock, checks
it against every currently live-or-recently-retired reservation before
committing it, retrying a bounded number of times and failing closed
(`SessionIdentityExhaustedError`) rather than ever returning an unchecked or
weaker value. This is the same standard of guarantee `session_locator`
generation already used, applied to a value two independent owners can also
mint from. Every owner sharing one real Python import of `aismixer_secure`
-- the shared production default and any additional `SecureState()`
constructed by a multi-listener configuration or a test, including two
owners running on two genuinely different OS threads -- shares this exact
registry object (`sys.modules` import caching), so two owners can never
simultaneously hold the same `session_handle` or `assembly_namespace`. A
test harness that deliberately reloads `aismixer_secure.py` under a
synthetic module name to simulate a second process gets its own completely
independent registry, exactly like it gets its own independent
`AUTHORIZED_KEYS` and `secure_state` -- this is a fully separate simulated
process for every piece of module state, not a partial gap in identifier
sharing alone. This matters because the assembler namespace derived from
`assembly_namespace` (see below) can be shared across owners by whatever
feeds their frames to a common downstream assembler; per-owner-only
uniqueness would let two unrelated authenticated stations on different
owners collide onto the same assembly group. `session_handle` identifies
one concrete `LogicalSession` *object* incarnation: it is always freshly
reserved on construction, including when a same-relation authenticated
replacement occurs, is never reused merely because the relation key
matches, and is unaffected by touch/activity; it is released back to the
registry (entering a bounded retirement window, described below) the
instant its exact `LogicalSession` incarnation is removed, for any reason.
`assembly_namespace` identifies secure multipart continuity lineage: it is
normally also freshly reserved, but a same-relation authenticated
replacement carries it forward -- `claim()`ing an additional live reference
on the SAME reservation rather than drawing a new one -- from the session it
replaces *only* when that replacement's authenticated `station_id` exactly
matches the station_id of the live session currently at that relation.
Relation equality alone is never sufficient for this carry-forward -- a
replacement authenticating as a different station always gets a fresh
`assembly_namespace`, with no exception. Neither identifier survives a
relation that is not currently live: expiry, close, capacity eviction,
nonce exhaustion, or process restart all mean the next establishment at that
relation gets both a fresh `session_handle` and a fresh `assembly_namespace`.
The registry itself is not an unlimited historical database: a released
reservation is retired (occupied, but no longer counted live, so it cannot
be re-drawn) for `RETIREMENT_SECONDS` (30 seconds) and then purged, via a
bounded, lazy-deletion min-heap scan (see below) piggybacked on
`SecureState`'s own existing monotonic cleanup pass rather than a dedicated
thread or a full per-packet scan of every retiring entry.

R6/Blocker B: the age-based policy described above (`MAX_INGRESS_FRAME_AGE_
SECONDS`, `RETIREMENT_SECONDS`'s formula) remains in force, but is no
longer, by itself, what makes namespace reuse safe -- a one-time age
observation taken at the top of `_process_impl` only protects against a
delay that already elapsed BEFORE that observation; it cannot protect
against an arbitrary pause (real OS thread preemption or a GC pause, not
merely the absence of `await`) landing AFTER the check passes but BEFORE
the frame actually reaches the assembler. The authoritative safety
mechanism is an explicit, frame-scoped hold: `_secure_server_loop`
attaches a `core.session_identity_registry.SessionIdentityRegistry.lease`
on a namespace-bearing frame's `assembly_namespace` to it as
`IngressFrame.admission_lease`. This is an ADDITIONAL live reference on
the same registry reservation the owning session already holds
(reference-counted, exactly like a same-station replacement's `claim()`),
independent of that session's own lifetime: for as long as the lease is
outstanding, `reserve()` cannot draw that value for anyone else, no matter
how long the frame takes to actually reach the assembler or how long the
owning session has already been gone. `PythonDataPlaneProcessor.process()`
holds this lease for the frame's entire `_process_impl` call and releases
it exactly once, in its own `finally`, regardless of outcome (normal
completion, an early stale-age drop, or a raised exception), using a
FRESH clock reading taken at that release instant -- so a frame that is
mid-`process()` when its owning session's OWN reference is released still
keeps the namespace alive until `process()` itself returns, and the
namespace's own eventual retirement is timed from when this frame's use
of it actually ended, not from admission time.

R7/Correction A: every OTHER release call site along this frame's journey
-- `_secure_server_loop`'s own release on a queue-put failure or a task
cancellation while awaiting queue capacity (potentially after an
arbitrarily long wait), and `ingress_fan_in_loop`'s reader releasing on an
admission failure or cancellation while blocked waiting for processing
capacity (see below) -- likewise samples a FRESH reading from that
component's own injected monotonic clock at the actual release instant,
never a cached admission/receive-time value. A stale cached timestamp
used at release would backdate the computed retirement deadline and could
let the namespace become eligible for reuse before an assembler group fed
by this exact frame (or, in the queue-wait case, by an earlier frame that
legitimately reached the assembler under the same namespace) has actually
had a chance to expire.

R7/Correction B: ownership of a dequeued ingress frame (and any lease it
carries) belongs to `ingress_fan_in_loop`'s reader from the moment it
dequeues the item until `processing_queue.admit()` actually transfers it
into the processing queue. If `admit()` fails or the reader is cancelled
before that transfer completes -- whether while still waiting for
capacity, or during the atomic bind-and-enqueue step itself --
ownership never passed anywhere else, and the reader releases the lease
itself before the failure/cancellation propagates; once `admit()`
succeeds, the reader releases nothing further, since the processing
queue's eventual consumer (`processor_stage_loop`/`PythonDataPlaneProcessor
.process()`) now owns that responsibility. Without this, a frame accepted
by UDPSEC but never consumed before its reader task is cancelled (a
supported, low-severity but genuine scenario -- not merely a theoretical
one) would leave its lease permanently outstanding: a real, if bounded,
registry-reference leak that neither garbage collection nor another
owner's own cleanup would ever reclaim on its own.

R7/Correction C: `SecureState.acquire_namespace_lease(session, now)` is
the sole, atomic way `_secure_server_loop` acquires this hold -- it
re-verifies, under this owner's own lock, that `session` is STILL the
exact live incarnation at its own key immediately before acquiring the
lease, in ONE locked transaction with no gap between the two steps.
Separately confirming liveness (via `touch_session`/
`is_live_session_handle`) and only later asking the shared registry for a
lease against the raw `assembly_namespace` bytes leaves a window, however
small, between those two separate lock acquisitions -- another thread
sharing this exact owner could remove `session` in between, and if its
exact namespace bytes ever retired and were reissued to an unrelated
incarnation before the second call ran, that raw two-step pattern would
attach the frame's protection to the WRONG lineage without any error,
since a bare `lease()` call only knows whether the raw bytes are
currently live, never whether they still belong to the caller that
originally authorized them. `acquire_namespace_lease` closes this
possibility entirely rather than merely narrowing it: a session no longer
live returns `None` without ever touching the registry, exactly like
every other `_get_live_session_handle`-gated method's exact-object-
identity semantics.

Every other admission-time loss (an abandoned `None` from
`frame_from_text_payload`) releases the lease right there instead.
`aismixer.py`'s final shutdown additionally drains any residual
ingress/processing backlog and releases whatever leases those abandoned
frames still held, since none of this pipeline's queues are drained by
ordinary task cancellation alone. A frame with no `admitted_at` (every
non-UDPSEC source) never carries a lease and is completely unaffected by
any of this.

`RETIREMENT_SECONDS`'s own formula still matters for the tail AFTER a
lease is released: `RETIREMENT_SECONDS` is NOT simply a multiple of the
assembler's own group timeout -- a downstream `AIVDMAssembler` group's
timeout only starts once a frame actually reaches the assembler, not when
UDPSEC admits it, and no ingress/processing queue between those two points
imposes a maximum residence time (only a maximum depth) -- but the lease
above already makes that residence-time gap itself unconditionally safe.
The actual enforcement point for the REMAINING (post-release) tail is
`core.python_data_plane.MAX_INGRESS_FRAME_AGE_SECONDS` (20 seconds), a
bounded-backlog/QoS policy applied independently of the lease: any
namespace-bearing frame older than that when the processor is about to
feed it to the assembler is refused outright, never reaching the assembler
in any form. `RETIREMENT_SECONDS` is the SUM of that enforced bound, the
assembler's own default 1-second group timeout, and a margin for
scheduling/GC jitter -- so no assembler group tied to a given namespace can
still be alive once that namespace becomes eligible for reuse. This
accounts for an assembler group's LATEST possible progress, not only its
first fragment (`AIVDMAssembler` resets a group's own timeout on every
uniquely-accepted fragment, not just the first): every fragment
contributing to one group was itself admitted while its owning session was
still live, so the latest any fragment can have been admitted is the
instant that session closed, bounding the group's own last possible
expiry at `session_close_time + max_ingress_frame_age + assembler.timeout`
-- which is exactly the relationship `PythonDataPlaneProcessor` validates
at construction (`max_ingress_frame_age + assembler.timeout <=
RETIREMENT_SECONDS`), rejecting an incompatible configured combination
(a longer assembler timeout, a longer permitted frame age, non-finite or
non-positive values) with a clear error rather than silently claiming the
same guarantee as the default. The age check itself, and the computed
`age` it is based on, are validated too: a non-finite or negative age (a
corrupted `admitted_at`, or one appearing to be in the future relative to
the processor's own clock -- impossible for a genuine same-clock-domain
observation) fails closed as stale rather than bypassing the check.
None of this is a claim of durable or cross-process uniqueness, or that a
finite random space can never repeat in principle -- only that every draw is
checked and retried under a real lock, and that this process's bookkeeping
of what is currently live or recently retired is accurate. An in-session
authenticated epoch refresh preserves both identifiers trivially, because
it replaces only `current_epoch` on the unchanged `LogicalSession` object
and never draws or claims a namespace at all.
This carry-forward mechanism now applies ONLY to genuine
whole-`LogicalSession` replacement -- a fresh establishment handshake for
terminal recovery or a genuinely new session -- where a same-relation,
same-authenticated-station replacement still carries the
`assembly_namespace` forward so a routine reconnect does not split an
in-flight multipart message. An ordinary successful refresh never
exercises it.

`claim()` only ever adds a reference to an ALREADY-live reservation -- it
raises rather than silently reviving a merely-retiring value or admitting
an unknown one as a fresh live entry, since the only legitimate caller
always reads the value directly off a `LogicalSession` it has just
confirmed is still live. `reserve()`/`claim()`/`release()` on
`install_session`/`promote_pending_session` follow a strict prepare-then-
commit discipline: `assembly_namespace` and `session_handle` are both
acquired, and the full replacement `LogicalSession` object constructed,
BEFORE any destructive mutation -- removing the previous session at that
relation, evicting a capacity victim, or (for promotion) popping the
pending candidate and releasing its locator reservation. If any
acquisition or construction step fails, exactly what was newly acquired in
that attempt is released once and every pre-existing store is left
untouched; a still-valid previous session, its pending candidate's locator
reservation, and any already-admitted confirmation-nonce accounting can
never be destroyed by a failed replacement. Legitimate expiry cleanup (see
below) may still remove genuinely expired state before an unrelated
operation fails -- that is not a violation of this guarantee.

`SecureState` itself is safe under the real shared-owner, multi-thread
model its own test suite exercises: one reentrant lock
(`threading.RLock()`, reentrant because several public methods call other
public methods as an internal implementation detail -- `install_session`
and `promote_pending_session` both call `cleanup_expired_sessions`;
`accept_data_nonce` calls `admit_data_nonce`) protects every composite
state transaction -- handshake replay admission, pending and active
install/replace/remove, locator reservation, promotion, nonce admission,
capacity eviction, and statistics snapshots -- as a single linearizable
commit point. Internal `_`-prefixed helper methods do not acquire their own
lock; they rely on their caller already holding it, and must never be
called directly from outside a public method's locked section. The lock is
held only for in-memory dict/set/counter work: it is never held across the
identity registry's own calls needing to re-enter it (lock order is always
`SecureState`'s lock first, the registry's own internal lock second, never
the reverse), and never across ECDHE/AEAD/JSON, network I/O, or an `await`.
This closes the specific defect an earlier corrective pass introduced: a
naive single-owner-thread lock model that broke the real multi-listener
tests below has been replaced by a lock scoped to each public method's own
transaction rather than to the whole receive loop, which those same real
concurrent-thread tests (and dedicated capacity/locator/nonce race tests
using real threads and, for the aggregate and pending capacity cases, real
loopback listeners) now verify directly.

Active and pending expiry each have their own authoritative deadline index
-- a lazy-deletion min-heap ordered strictly by deadline VALUE -- separate
from `_sessions`'/`_pending_sessions`' `OrderedDict` position, which exists
only for LRU/capacity-eviction ordering. The lock above serializes
mutations, but does not make commit order track the `now` values callers
happened to sample before acquiring it: two listener threads can commit in
an order that does not match their own timestamps. A front-prefix scan of
an `OrderedDict` silently assumes commit order tracks deadline order; where
it does not, a genuinely expired entry can sit behind a newer one and never
be found, letting expired active or pending state be treated as live. The
heap has no such assumption -- popping always yields the true minimum
deadline regardless of insertion order.

R6/Blocker A: each heap entry carries the exact object (`LogicalSession` or
`_PendingSecureSession`) it was pushed for, not merely its key. A popped
entry is validated by OBJECT IDENTITY against whatever currently occupies
that key -- not by the key's mere presence -- before being acted on at all;
an entry whose object no longer matches (superseded by a replacement, or an
active session's locator-derived key coincidentally reused after a long
retirement) is discarded outright, never treated as authority to inspect or
reschedule whatever object actually occupies that key now. This closes a
previously-reported unbounded-growth defect specific to PENDING candidates:
a pending relation key, unlike an active session's fresh-per-replacement
locator-derived key, is NOT fresh across a same-relation replacement, so a
stale entry belonging to an already-replaced candidate would still resolve
that key to whichever candidate currently occupied it and recompute/
reschedule THAT candidate's deadline on the strength of an entry that was
never pushed for it -- one entry added per replacement, never discarded, so
continuous same-relation handshake churn (an authorized station replacing
its own pending candidate repeatedly, entirely within its own rate) grew
this heap linearly with total historical replacement count rather than with
live/due state. With object identity checked, a superseded entry is
discarded in O(1) the moment it is popped, keeping retained heap size
bounded by live state plus recent churn rather than by all-time traffic.
For an ACTIVE session, whose `last_seen` DOES change via `touch_session`
without pushing a new heap entry, an identity-matched but now-stale entry
(superseded by a later touch) is still re-queued with its real current
deadline rather than discarded, so `touch_session` clamping `last_seen` to
never move backwards still means a stale, out-of-order `now` can never
shorten a session's real remaining lifetime, and repeated touches alone
never grow retained heap size beyond one entry per live session.

The heap fix above corrects a commit-order defect, but not a distinct one:
correct heap ordering still trusts whatever `now` a caller actually
supplies for the admission decision, and a `now` sampled before a delay (a
different thread, a slow prior parsing/crypto step, or an old asynchronous
task holding a stale session reference) can itself be wrong relative to
real elapsed time, independent of commit order. `SecureState` accepts an
optional authoritative `clock` at construction (`__init__`'s `clock`
parameter, `None` by default, preserving every existing caller's exact
prior behavior); when configured, every public method that takes `now`
floors it at that clock's own current reading before using it for
anything -- expiry comparison, `last_seen`/`created_at` recording, heap
deadline computation, registry release timestamps -- so a stale, too-small
`now` can no longer make an object the clock already knows is past its
deadline appear live, while a `now` already at or ahead of the clock is
used unchanged (ordinary deterministic testing is unaffected). Production's
module-level `secure_state` singleton is the one owner explicitly
configured this way, with real `time.monotonic`; a `SecureState` a test
constructs directly defaults to no clock and is unaffected unless it opts
in. `run_periodic_maintenance()` -- an independent, opt-in coroutine an
operator's runtime spawns once per shared owner (not per listener) to run
this same expiry/retirement-purge housekeeping on a timer, so it still runs
eventually even on an owner that currently receives no packets on any of
its listeners -- samples this SAME configured clock (falling back to real
`time.monotonic` if none is configured), never an independently injectable
one, so maintenance can never observe a different, incoherent clock domain
than every other decision this owner makes. None of this is security-
relevant on its own, since every public entry point already re-validates
liveness through this same authoritative path regardless of when
maintenance last ran; it only bounds memory reclamation on an idle owner. A
`SecureState` sharing the process-wide identity registry with other owners
must use a clock domain compatible with theirs, since the registry's own
retirement bookkeeping is keyed by whatever `now` each owner's removals
pass it -- every owner sharing the process-wide default registry in
production shares the same real `time.monotonic`.

A standalone `SecureState` owner has two distinct, explicit teardown paths
at two different scopes. `close_owned_sessions()` gracefully closes (sends
a close message for, then removes) every active session ONE LISTENER
installed or promoted, and the companion `close_owned_pending_sessions()`
immediately discards that same listener's own still-pending candidates
(freeing their locator reservation and pending-capacity slot) rather than
leaving them to expire on their own TTL -- sending no wire message, since
an unconfirmed candidate has no confirmed key material to notify with.
Both are exact-object-checked (`close_session`/`close_pending_session`) so
a handle already superseded by a later replacement is silently ignored,
never discarding whatever legitimately occupies that relation now. Pending
candidates hold no `_SESSION_IDENTITY_REGISTRY` reservation of their own to
release (`session_handle`/`assembly_namespace` are minted only by
`install_session`/`promote_pending_session`), so discarding one never
touches the registry; tearing down one listener's own sessions/pending
candidates never affects any other owner, or any OTHER listener sharing
the same owner, sharing the same process-wide registry.

`close()` is the wider, OWNER-level operation: it discards EVERYTHING one
exact `SecureState` instance holds -- every active session and pending
candidate regardless of which listener installed it, releasing exactly
that owner's own registry reservations -- and marks the owner closed.
Idempotent (closing an already-closed owner is a no-op); `install_session`/
`install_pending_session` raise `SecureStateClosedError` afterward rather
than silently creating new state, since there is no reopen contract (a
fresh `SecureState()` is the supported way to get a new owner). It never
touches any other owner or clears the shared registry, and does not rely on
`__del__`/garbage collection. This is a genuinely different operation from
`close_owned_sessions()`/`close_owned_pending_sessions()`, which exist
specifically so ONE listener can stop without disturbing a state SEVERAL
listeners still share -- `close()` ends the owner's own lifecycle and must
only be called once nothing else is still using that exact instance. The
default daemon runtime calls it exactly once, on the process-wide
`secure_state` singleton, in `aismixer.py`'s own final shutdown -- strictly
after every listener sharing it has already stopped and run its own
listener-level cleanup -- and only when UDPSEC was actually configured, so
a plain-UDP-only deployment never forces the otherwise-lazy import.

R6/F5 residual completion: `close()` also discards this owner's handshake
replay ledger and clears both expiry heaps outright -- a closed owner has
no active/pending state left for either heap's entries to meaningfully
describe (dead weight, not a live hazard, now that the heap fix above
discards a superseded entry on sight regardless), but the replay ledger is
a genuine resurrection surface if left populated: `accept_handshake_replay`
itself now checks `self._closed` first and rejects (an ordinary handshake
failure, not an exception) rather than admitting a new record into a
ledger `close()` just emptied. Every other mutating public method already
fails closed against removed state through its own existing exact-object-
identity check; a bare locator-generation call is not restricted, since it
reserves nothing and is not itself a demonstrated resurrection path.
Final-shutdown ordering in `aismixer.py` is a nested try/finally chain,
mirroring `secure_server()`'s own shutdown chain for its per-listener
cleanup: a UDP socket, the control server, or the forwarder failing to
close cleanly does not prevent a later step (including the final owner
`close()`) from running, and does not silently mask the failure either --
each step's own exception, if any, still propagates once every later step
has had its chance to run. Within the UDP-socket step itself, every
socket's own `close()` is attempted individually (the first failure, if
any, is what ultimately propagates) rather than a single `for sock in
udp_sockets: sock.close()` that would abort on the first failure and
leave every later socket in that same loop unclosed. That same final
shutdown also drains any
residual UDPSEC ingress/processing backlog and releases whatever namespace
lease each abandoned frame still held (see Blocker B above), immediately
before calling `close()`, so a frame that was accepted but never consumed
by this same shutdown does not hold the registry open indefinitely.

Each physical secure-listener socket incarnation owns one opaque endpoint
token. The token has process-local object-identity semantics, remains stable
for that socket's lifetime, and is never transmitted or derived from listener
configuration, network addresses, station identity, or cryptographic key
material. Pending relation identity is the exact combination of that
endpoint token and the raw peer socket address returned by `recvfrom()`.
Active session identity is the combination of that endpoint token and the
session's `session_locator`, as described above; the raw address a session
was established or last promoted at is retained separately as
`path_state.active_path`, the sole address ordinary established-session
replies use and the sole ordinary transport authority for a session. A DATA
packet whose locator matches a live active session but whose source address
does not structurally match that session's `active_path` is no longer
dropped outright: it may be a migration candidate, a late straggler on the
just-retired path, or a return-routability proof (see the server-side path
migration subsection below). It is still subject to source policy first and
to at most one bounded AEAD attempt under the exact current epoch, and an
unproved path may never drive session close, epoch refresh, or any other
state-changing control; a locator is a lookup hint, never a bypass for path
authorization. Structural path
equality -- one shared model (`core.sockaddr_identity`) used identically by
the server's active-path/relation-index comparison and the client's pinned-
remote-address check -- normalizes a raw address tuple to `(family, ip,
port)` for IPv4 and `(family, ip, port, scope_id)` for IPv6, where `ip` is
the canonical string form (so equivalent spellings, e.g. differing case or
IPv6 zero-compression, compare equal); IPv6 flow label (`flowinfo`) is not
significant to path identity. A bare IPv6 2-tuple (no scope information
available) is compared as scope_id `0` explicitly -- never as a wildcard
that matches any scope_id -- so it agrees with, and only with, a native
4-tuple whose own scope_id is `0`. The canonical result is a distinct
immutable type, not a plain tuple: a native IPv6 4-tuple and this
canonical IPv6 form are both 4 elements long, so a plain-tuple result
could otherwise be fed back in and silently misparsed as a different raw
address; the distinct type makes normalization explicitly one-way, and
handing a canonical value back in fails closed rather than being
reinterpreted. A malformed or ambiguous address -- an unsupported tuple
shape, a non-string IP, an IP string that does not parse, an out-of-range
or non-integer port, a non-integer or negative flowinfo/scope_id, or an
IPv6 zone-qualified address string (native OS sockaddrs convey scope only
as the separate 4th tuple element, never embedded in the address string)
-- fails closed rather than being treated as equivalent to a well-formed
address. IPv4-mapped IPv6 literals remain IPv6 identity; they are never
collapsed to IPv4.

A bounded secondary relation index, keyed by endpoint token and the
structurally-canonicalized active path, maps a relation to its current
active session key for same-relation-replacement detection and
`assembly_namespace` carry-forward only -- it is never consulted for
admission, authentication, or DATA lookup, and it is kept consistent with
the flowinfo-insensitive path equality above. Pending-session lookup never
uses this canonicalization; it stays exactly tuple-bound as described above.
Session-locator uniqueness per endpoint_token is enforced as a `SecureState`
invariant at installation time (covering direct installation, pending
installation, and promotion alike), not merely by convention of locator
generation: installing a session whose locator already identifies a
different live session or live pending candidate on that endpoint_token
fails closed rather than silently overwriting or aliasing state. The same
raw locator bytes on a different endpoint_token are an entirely independent
identity. This locator-collision (or otherwise inconsistent-ownership) check
is always a precondition, never a rollback: it is evaluated against
still-live state before any replacement, capacity eviction, or promotion
mutation begins, so a rejected installation or promotion leaves every
previously-live active/pending session, the relation index, locator
ownership records, nonce ledgers, and lifecycle statistics exactly as they
were -- normal expiry cleanup already due at the operation's own timestamp
is the only state change a failed attempt may still produce. A
secure-ingress `IngressFrame`'s assembler namespace is derived instead from
the owning `LogicalSession`'s `assembly_namespace` -- never from
`session_handle`, the address, `path_state`, or the locator -- so that
multipart continuity is tied to the unchanged `LogicalSession`, not to any
one remote tuple. Two fragments of one multipart message that reach the
same continuing authenticated station on that same `LogicalSession` stay in
one assembly group regardless of which address either arrived from,
including fragments that legitimately span the `active_path` and a
current-epoch candidate path, and late in-flight fragments from the
short-lived retired path within its grace -- all under the same
`assembly_namespace`. A replacement (or new session) authenticating as a
different station, or a genuinely different `LogicalSession`, never inherits
that group. This path-spanning continuity is only about how a session's
own admitted fragments are grouped: it grants candidate and retired paths
no ordinary outbound authority (see the server-side path migration
subsection), and every frame still had to pass source policy, exact
locator/epoch selection, one AEAD attempt, per-epoch replay admission, and
semantic validation to be admitted at all -- `blocked`, wrong-key,
malformed, mismatched, expired, and replayed traffic still fails closed and
never reaches assembly. Identical
raw peer addresses on different physical listener incarnations are
independent relations. A replacement socket receives a fresh endpoint token
and cannot select retained state from the prior incarnation; abnormally
retained old state remains unreachable until its existing lifecycle removes
it.

Wall time and monotonic time have separate ownership. Wall time is used only
for externally meaningful protocol or diagnostic timestamps: the transmitted
handshake timestamp check, ping, pong, and graceful-close timestamps, and
timestamped debug output.
Handshake freshness remains inclusive at the boundary:
`abs(wall_now - transmitted_timestamp) <= 30`. Monotonic time owns handshake
replay TTL, pending-session creation and TTL, active-session creation and
last-seen times, active-session TTL, and local capacity ordering. Each allowed
received packet takes one cheap monotonic observation up front for locator
lookup, expiry cleanup, and cheap PRE-crypto early rejection. An ordinary
validated DATA packet (`nmea`/`ping`/`close`) then takes a SECOND, fresh
authoritative monotonic observation immediately before its locked
nonce-admission transaction, because the expensive AEAD and message
validation can themselves cross a deadline-sensitive boundary (a candidate
TTL or a retired-path grace) after the pre-crypto observation. That locked
transaction is one short decision: fresh authoritative time, path-state and
epoch-transition expiry, fresh source-path classification against the
current `PathState`, exact `CryptoEpoch` object/role revalidation, the
path-role x epoch-role x message-type authorization matrix, and the
per-epoch nonce admission -- and it returns the AUTHORITATIVE path role and
admission time, which the downstream ordinary logic (session touch, pong,
ingress queue, candidate observation, frame `admitted_at`) uses instead of
the stale pre-crypto role/time. The specialized atomic transactions
(PATH_RESPONSE in the migration commit, REFRESH_INIT/REFRESH_CONFIRM in
their refresh transactions) keep their own single locked admission and are
not double-admitted. Network policy is applied first; a denied packet
performs no cryptographic work, state mutation, cleanup, or secure-state
clock read.

Every process-local TTL uses the same exact boundary: state is live while
`age < ttl` and expires when `age >= ttl`. A duplicate handshake replay key
does not refresh its expiry. Wall-clock changes do not expire, revive, or
extend handshake-replay, pending-session, or active-session state and do not
alter data-nonce state. Accepted data nonces have no independent TTL.

Handshake replay identity is exactly the value produced by
`build_handshake_replay_key(client_auth_digest, client_signature)`. The digest
is the authenticated ClientHello digest built from the parsed ClientHello, and
the signature is the exact client identity signature verified over that
digest. The peer network address is not part of replay identity. Replay
admission occurs after freshness, station authorization, identity-signature
verification, and ephemeral-point validation, but before pending-session
installation. A later server-side failure does not remove an admitted key. The
replay set retains at most `HANDSHAKE_REPLAY_MAX` records, expires only its
ordered front prefix during admission, and evicts the oldest live record
deterministically when capacity remains full.

A pending session is identified by its exact endpoint-token/peer-address
relation and retains the raw peer address, authenticated station ID, its
monotonic creation time, and a `current_epoch` (separate client-to-server and
server-to-client AES-GCM owners plus a private data-nonce set).
Pending sessions have their own TTL, capacity, and creation order, independent
of active-session TTL, capacity, and activity order. Pending lifetime is not
refreshed by traffic. Expiry removes only the expired ordered front prefix. A
new pending relation is installed at the newest end; when capacity remains
full, the oldest live pending entry is evicted deterministically. A newer live
pending session for the same endpoint/peer relation replaces only that older
pending entry and occupies the newest position. A same-peer entry in another
endpoint namespace is not a replacement. Equal creation times follow
deterministic installation order. At most `PENDING_SESSION_MAX` pending
sessions are retained across the process-wide `SecureState`.

Installing or replacing a pending session does not remove, replace, or touch
any active session at that relation. While the candidate remains pending,
confirmation failure or client timeout leaves any existing active session
intact; it does not promote the pending entry and does not itself delete
server-side pending state. Pending expiry, same-relation replacement, capacity
eviction, or nonce exhaustion removes only that exact pending entry. Each such
removal makes the candidate traffic-key epoch unusable before its nonce state
is discarded and does not itself alter an active session in that relation.

When multiple listeners share one `SecureState`, endpoint-scoped state lookup
prevents a packet received through one socket from selecting any same-peer
pending or active object owned by another socket. Each listener additionally
retains weak exact-object ownership handles for confirmation, client close,
and shutdown as defense in depth. Only that listener may confirm and promote
its still-current pending object or close its exact active object. A missing,
stale, replaced, or wrong-endpoint handle cannot consume nonce state, transfer
promotion ownership, touch, exhaust, or close another listener's relation.

An active session (a `LogicalSession`) is identified by its exact
endpoint-token/`session_locator` combination and retains a `session_handle`
(identifying this exact object incarnation) and an `assembly_namespace`
(identifying secure multipart continuity lineage; see above for how the two
differ and when the second may be carried forward across a replacement),
authenticated station ID, monotonic creation and last-seen times, a
`current_epoch` (separate client-to-server and server-to-client AES-GCM
owners plus a private data-nonce set), and a `path_state` whose
`active_path` is the address ordinary established-session replies use and
the sole ordinary transport authority, plus its at-most-one
`candidate_path` / `retired_path` migration slots and a `path_generation`
counter (see above and the server-side path migration subsection). Active
sessions are ordered from least to most recently seen across the
process-wide `SecureState`.
Installation and valid activity place a session at the most-recent end.
Promotion counts as activity, and so does any fully validated,
replay-admitted secure NMEA or ping packet that has passed the AUTHORITATIVE
post-decrypt path-role and message gates (the fresh-time locked transaction
described in the epoch-refresh subsection) -- this includes a packet from
the `active_path` and, under Prompt 4 path migration, a packet admitted for
continuity from a newly observed current-epoch path (which then becomes the
candidate), from an existing candidate path, or -- while its grace is still
live at the authoritative post-decrypt instant -- from the short-lived
retired path. A `blocked` frame (source path owned by a different live
session on this endpoint token), and a straggler whose candidate TTL or
retired grace elapsed during the AEAD so that its authoritative role no
longer authorizes it under the epoch it selected, do not touch. Invalid,
malformed, mismatched, wrong-epoch, expired, or replayed traffic does not
touch the active session, and neither does a graceful-close packet (it
removes the session instead). `path_response` handling commits the
migration but is not itself recorded as session activity; refresh-control
(`refresh_init` / `refresh_confirm`) handling touches the session exactly
as described in the in-session epoch refresh subsection.

After network policy accepts any packet, including a handshake or unknown
packet type, the expired ordered prefixes of both pending and active session
stores are removed before packet-type-specific handling. Expired state may
therefore remain physically present until later allowed traffic, but an
expired directly addressed session is never treated as live. State operations
receiving an active handle first require exact retained-object identity at
its endpoint-token/`session_locator` key; a pending handle requires exact
retained-object identity at its endpoint-token/peer-address tuple. A
replaced, capacity-evicted, promoted, expired,
nonce-exhausted, or otherwise stale handle cannot mutate state or trigger
unrelated cleanup.

UDPSEC has no plaintext session-reset or other unauthenticated session-control
packet. A DATA packet's locator is checked against an exact pending match on
its endpoint_token first, then against a live active session on that
endpoint_token; a locator matching neither is dropped without a wire response
and without attempting decryption. Plaintext,
malformed, unknown, or
otherwise unauthenticated datagrams cannot touch, promote, replace, extend, or
delete a live session. The monotonic cleanup of state that has already reached
its locally owned TTL remains the only state effect an allowed unknown packet
may trigger, as specified above; that cleanup is not peer-supplied lifecycle
evidence.

Sequence `0` is reserved for the encrypted confirmation ping and pong. Every
ordinary active-session ping and pong sequence has exact built-in `int` type
and is strictly greater than zero; ordinary sequences begin at `1` in each
confirmed session and are not reused within that session. Every ping and pong
requires a `timestamp` whose exact type is built-in `int`, not `bool`; this
field has no freshness semantics and is not compared with wall or monotonic
time. Ping and pong objects remain open-schema: additional JSON object members
do not alter validation of the required fields.

On `nmea_sproxy`, a matching authenticated encrypted pong from the pinned
remote tuple is the baseline peer-liveness signal; a validated in-session
epoch-refresh control message (see the epoch-refresh subsection) and a
migration-proof-matched PATH_ACK (see the path-migration subsection) are the
only other documented exceptions that also advance peer liveness. The pong
must decrypt under the current server-to-client owner, match the configured
station identity, and carry the exact sequence of the one outstanding ping.
No pong is accepted when no ping is outstanding. An accepted pong clears that
expectation and advances liveness. After it is cleared, a duplicate replay has
no outstanding sequence to match; the sequence is not reused later in that
session. Stale or other-sequence pongs therefore cannot refresh liveness, and
ciphertext from an earlier session cannot authenticate under the fresh
directional keys. Plaintext, wrong-key, wrong-address, wrong-source,
malformed, and wrong-sequence packets provide no liveness evidence.

At a keepalive deadline with no outstanding ping, the proxy sends one encrypted
ping and retains its expected sequence. It does not overwrite an unresolved
expectation with later ping sequences. If that expectation is still unresolved
when the next keepalive deadline is reached, the proxy ends the local forwarding
loop with a proactive-rekey reason and immediately makes one fresh signed ECDHE
handshake attempt. A failure of that attempt returns to normal
`reconnect_delay`; it cannot create a busy retry loop. `peer_timeout` remains an
ultimate fallback and retains priority when its deadline is reached. With the
defaults `keepalive_interval: 30` and `peer_timeout: 90`, the first ping is due
at about 30 seconds and an unanswered ping normally selects proactive rekey at
about 60 seconds, before the 90-second timeout.

Forwarding-loop deadlines use monotonic time and become due at equality. When
deadlines coincide, deterministic priority is `peer_timeout`, then the planned
session-refresh action (start an in-session epoch refresh transaction, or --
if the loop was not given the station/server keys -- end the loop with a
planned-refresh reason for a fresh establishment handshake), then the
keepalive action: proactive rekey for an unresolved ping or a new ping when
none is outstanding. A due deadline is resolved before poll-ready packets, so
a matching pong must be fully authenticated and accepted before its boundary
to refresh liveness or prevent proactive rekey. The planned-refresh action
does not end the loop when an epoch refresh is available; the transaction
runs alongside normal forwarding and its own retransmit/timeout deadline
additionally bounds the poll wait. Deadline checks use fresh monotonic
observations. After any pending local-input forwarding, the final poll
timeout is recomputed from another fresh monotonic observation immediately
before `select()`.

Timing values must be finite integer or float values, excluding booleans and
numeric strings. UDPSEC requires `keepalive_interval > 0`,
`peer_timeout > 0`, `session_refresh_interval >= 0`, and
`reconnect_delay >= 0`; a zero session refresh interval disables planned
refresh. These constraints are independent: there is no cross-field ordering
or ratio requirement. Plain UDP shares only the `reconnect_delay >= 0`
validation; the other three fields are UDPSEC-only.

Proactive recovery (an unanswered keepalive ping) reuses the normal signed
ClientHello, authenticated ServerHello, directional ECDHE-derived traffic
keys, and encrypted sequence-zero confirmation for a genuine fresh
establishment -- it adds no reset, probe, or separate recovery protocol,
generates fresh ephemeral ECDHE material, installs or replaces only pending
state (the old active server session stays usable while confirmation is
pending, failed confirmation does not destroy it, and successful
confirmation atomically promotes the candidate as specified below).
Configured planned refresh (`session_refresh_interval > 0`) instead runs
the in-session authenticated epoch refresh described in the epoch-refresh
subsection above: it does not restart forwarding, does not
invoke ClientHello/ServerHello on success, and leaves the same server
`LogicalSession` in place. The first proactive-rekey attempt and each
planned-refresh transaction start are immediate; peer graceful close,
`peer_timeout`, handshake failure, local forwarding failure, and socket
failure use `reconnect_delay`. No NMEA payload is buffered or replayed as
part of any recovery or refresh path.

A pending session is promoted only when a DATA packet decrypts under its
client-to-server AES-GCM owner and decodes to a confirmation ping. Confirmation
requires type `"ping"`, reserved sequence `0` as a built-in integer and not a
boolean, a built-in-integer timestamp, and a source identity equal to the
pending station ID. The packet nonce is admitted to the pending session before
promotion. Promotion removes the pending entry and, as one state-model
transition, replaces any live active session found through the relation
index at the confirming packet's structural path on that endpoint_token.
The station identity, `session_locator` (already minted for this candidate
during its own ServerHello and transferred, not re-derived, at promotion),
and the pending `current_epoch` (both directional AES-GCM owners and the
pending nonce set, including the already-admitted confirmation nonce) become
the new `LogicalSession`'s state exactly as one transferred object; active
creation and last-seen time begin at promotion, a fresh `path_state` is
created with `active_path` set to the confirming packet's address, and a
fresh `session_handle` is always minted (promotion is never
object-identity-preserving in this stage). Because each ServerHello mints an
independent locator, the promoted session's locator is always different from
whatever locator any live session it replaces was using -- there is no
notion of a rekey that preserves a locator in this revision. The new
session's `assembly_namespace` is carried forward from the exact live
session this promotion replaces only when that session's authenticated
`station_id` matches the promoted station_id; otherwise -- a different
authenticated station, or no live session currently at this relation -- a
fresh `assembly_namespace` is minted instead. The transferred confirmation
nonce remains
retained and counts against `DATA_NONCE_MAX_PER_SESSION` for the promoted
traffic-key epoch. The server then returns an encrypted sequence-zero pong
using the promoted server-to-client owner, addressed to the confirming
packet's own tuple (pending confirmation remains tuple-bound; this reply
does not consult `path_state`); the proxy requires the same sequence and
timestamp rules before accepting that confirmation pong.

For promotion at a new endpoint/peer relation, expired active sessions are
removed before active capacity is considered; if capacity remains full, the
least-recently-seen live active session is evicted. Equal active timestamps are
resolved by deterministic activity order. At most `SESSION_MAX` active
sessions are retained. Every active-session removal, including replacement,
expiry, session-capacity eviction, graceful close, or nonce exhaustion, makes
that exact traffic-key epoch unusable before discarding its nonce state.

Active and pending capacity limits remain aggregate process-wide resource
policy. Exact endpoint-token equality plus the structural (flowinfo-
insensitive) path equality described above controls relation-index
replacement, so a same-path relation on another endpoint is never mistaken
for the relation being replaced. When an aggregate store is genuinely full,
its existing deterministic global capacity policy may nevertheless evict the
oldest live entry belonging to another endpoint.

Secure-data replay identity is the exact 12-byte nonce within its receiver-side
directional traffic-key epoch. Identical bytes under distinct epochs are
independent. Once admitted, a nonce remains retained without an independent
TTL or live-entry eviction until its exact owning epoch is unusable. A
pre-decrypt membership check may reject a retained replay early, but
authoritative post-validation admission distinguishes `ACCEPTED`, `REPLAY`,
and `EXHAUSTED`. Membership is checked before capacity, so a retained duplicate
at exact capacity is `REPLAY`, leaves the epoch intact, and does not mutate its
ledger.

A new pending nonce reaches authoritative admission only after decryption and
complete confirmation validation. A new active nonce reaches admission only
after decryption, JSON decoding, source matching, and complete message-type and
required-field validation. Authentication failures, malformed data, wrong
source identities, invalid message shapes, and unknown types cannot retain a
nonce or exhaust a live owner. `ACCEPTED` retains a new nonce below
`DATA_NONCE_MAX_PER_SESSION` before promotion, session touch, pong generation,
graceful-close handling, or NMEA action.

For a distinct valid nonce at full capacity, `EXHAUSTED` retains no new nonce
and evicts no existing nonce. Active exhaustion removes the exact active object
with reason `nonce_exhausted`, making that traffic-key epoch unusable before its
ledger is discarded; the triggering packet is dropped without session touch,
pong generation, graceful-close handling, or NMEA action. Pending exhaustion
removes only the exact pending candidate, sends no confirmation pong, performs
no promotion, and leaves any existing active session intact.

Active exhaustion has no wire response. Later old-epoch DATA, including pings,
is silently dropped. The unresolved-ping lifecycle above recovers through a
fresh signed ECDHE handshake and fresh directional keys; no NMEA payload is
buffered or replayed during recovery.

Graceful close is a canonical JSON control message with type `"close"`, reason
`"shutdown"`, an unsigned integer timestamp, and the authenticated station
identity in `source_id`. It is carried inside the existing encrypted DATA
channel; there is no plaintext close prefix. It is best-effort and
unacknowledged, and neither endpoint waits for a reply.

A client close is encrypted under the current client-to-server AES-GCM owner
and framed like any other DATA packet, so it is subject to the same locator
lookup and active-path admission described above. The server accepts it only
against the receiving endpoint_token's exact live active session identified
by the packet's locator, whose source address structurally matches that
session's `active_path`, and only through the listener that retained that
exact live session object. Source identity and the
complete canonical message shape must match. Decryption, canonical validation,
and authoritative active data-nonce admission occur before close-specific state
changes. Only `ACCEPTED` processes the packet as a graceful close; `EXHAUSTED`
follows the fail-closed epoch-invalidating path above and is not counted as a
normal close. Pending state on the same endpoint_token and sessions owned
by other listeners remain unchanged. Forged, plaintext, wrong-key,
wrong-locator, wrong-path, replayed, stale-handle, or cross-listener close
attempts cannot remove a session.

A server close is encrypted under the current server-to-client AES-GCM owner
and sent only through the listener socket that owns the exact retained live
session, addressed to that session's `path_state.active_path` (the address
the session was established or last promoted at, or -- after a committed
server-side path migration -- the migrated-to path). The proxy considers it
only from its pinned remote tuple and leaves the
current session only after successful decryption and canonical source and shape
validation. A validated peer close selects normal `reconnect_delay`, not an
immediate re-handshake. It does not require or carry a ping sequence.

Normal endpoint shutdown sends at most one such close per live relation before
its UDP socket is closed. This includes proxy SIGINT or SIGTERM and aismixer
async cancellation reached through SIGINT, SIGTERM, systemd, or procd service
termination. Process crashes cannot send it, and UDP may lose it; active-session
TTL, authenticated liveness, proactive re-handshake, and `peer_timeout` remain
the fallbacks. Each fresh confirmed ECDHE session has fresh traffic keys, so a
close captured under an older session cannot authenticate against or terminate
the replacement session.

An NMEA message is semantically valid for nonce admission only when its required
`payload` value is a string. A missing or non-string payload neither retains a
nonce nor touches or exhausts its active session. It produces no frame or queue
item; later packets continue to be processed.

`stats()` returns an immutable point-in-time `SecureStateStats` snapshot. It
reports replay, pending-session, active-session, data-nonce, and epoch-
refresh lifecycle counts, including `sessions_closed` for normal active-
session removal, plus current and peak sizes. `data_nonce_exhaustions`
counts fail-closed removal of an exact active, pending-session, pending-
epoch, or retiring-epoch traffic-key epoch and is not counted as normal
close, expiry, replacement, or active- or pending-session capacity
eviction. `epoch_refreshes_committed`, `pending_epochs_created`,
`pending_epochs_discarded`, `retiring_epochs_retired`,
`current_pending_epochs`, and `current_retiring_epochs` are the epoch-
refresh lifecycle counters. `path_candidates_opened`,
`path_candidates_replaced`, `path_candidates_expired`,
`path_migrations_committed`, `retired_paths_expired`,
`current_candidate_paths`, and `current_retired_paths` are the server-side
path migration lifecycle counters (see that subsection). The legacy
`data_nonces_expired` and
`data_nonces_capacity_evicted` fields remain in the snapshot for
compatibility but stay zero; accepted data nonces no longer expire or
undergo live-entry eviction. Retained records discarded when their owner
epoch ends contribute to `data_nonces_session_discarded`. Every removed
record has exactly one removal reason. Reading statistics invokes neither
clock, performs no cleanup, exposes no mutable state, and does not change an
earlier snapshot.

### In-session authenticated epoch refresh

A confirmed `LogicalSession` can refresh its traffic-key epoch in place,
without re-establishing the session, its wire locator, its assembly
identity, or its validated network path. The transition is:

    Before:  S -> current_epoch E1 (generation G)
    Pending: S -> E1 + pending_epoch E2 (generation G+1)
    After:   S -> current_epoch E2 (generation G+1), retiring_epoch E1

The exact `LogicalSession` object S is unchanged; a successful refresh
mints no locator/handle/assembly namespace, never invokes
`install_session`/`promote_pending_session`, releases none of S's
identities, and does not increment `sessions_created`/`sessions_replaced`.
The same-station `assembly_namespace` carry-forward mechanism remains for
genuine whole-session replacement and terminal recovery only; an ordinary
successful refresh never exercises it, and in-flight multipart AIS
messages simply continue under S's unchanged `assembly_namespace`.

**Wire.** Four strict, closed-schema JSON control messages carried inside
the existing encrypted DATA channel (same framing, locator/generation AAD
binding, active-path admission, source-policy-before-crypto, per-epoch
nonce admission):

- `refresh_init` (client -> server, under E1 client->server keys, selector
  `G & 0xFF`): carries a fresh 32-byte transaction id, the parent (`G`)
  and next (`G+1`) generations, a fresh 32-byte client epoch random, a
  fresh 33-byte client P-256 ephemeral point, a timestamp, and a P-256
  ECDSA identity signature over `build_refresh_init_digest(...)` in a
  distinct `AISMIXER-UDPSEC-EPOCH-REFRESH` domain that binds the protocol
  revision, station id, session locator, both generations, the
  transaction id, and both fresh client contributions.
- `refresh_reply` (server -> client, under E1 server->client keys): echoes
  the transaction identity and adds a fresh server epoch random, a fresh
  server P-256 ephemeral point, and a server identity signature over
  `build_refresh_reply_digest(...)` (all `refresh_init` fields plus the
  client signature plus both server contributions).
- `refresh_confirm` (client -> server, under the candidate E2 client->
  server keys, selector `(G+1) & 0xFF`): proves the client derived E2
  correctly. Carries only the transaction id, next generation, timestamp.
- `refresh_ack` (server -> client, under E2 server->client keys): the
  authenticated evidence of the server commit.

Directional E2 keys are HKDF-SHA256 over a refresh-domain-separated key
schedule with the fresh P-256 ECDHE secret as IKM and
`build_refresh_transcript_hash(...)` as salt -- never derived from E1's
symmetric keys, and never able to collide with the establishment key
schedule.

**Server state machine.** A confirmed session owns at most one
`pending_epoch` and, for a short window immediately after a commit, at
most one `retiring_epoch`. On a valid `refresh_init` the server verifies
the signature and derives E2 entirely outside the `SecureState` lock, then
under the lock re-verifies that S is still live, `current_epoch` is still
the exact epoch object the `refresh_init` was decrypted under, its
generation is still `G`, and no incompatible transition is unresolved
(no other pending epoch, no still-live retiring epoch); it admits the
`refresh_init` nonce into E1's ledger and attaches the candidate. A
`refresh_init` for a different transaction while one is pending, or while
a retiring epoch is still within its cutoff, is rejected: there is no
chain of pending refreshes. A retransmit of the SAME transaction is
idempotent -- its fresh nonce is admitted but no new keys are derived and
the cached `refresh_reply` packet is re-sent. A byte-for-byte replayed
`refresh_init`/`refresh_confirm` (identical nonce) is rejected by the
pre-decrypt per-epoch replay ledger before any state-machine step, so it
can never re-derive, re-commit, move liveness or extend a deadline.

On a valid `refresh_confirm` for the live candidate the server commits
atomically under the lock (all fallible work already happened at
`refresh_init` time): `retiring_epoch <- current_epoch` with an exact
monotonic cutoff `now + RETIRING_EPOCH_OVERLAP_SECONDS` (5s),
`current_epoch <- candidate` (its `created_at` set to now, generation
`G+1`, confirmed `transaction_id` recorded), `pending_epoch <- None`; the
confirmation nonce is retained through the commit in what becomes the new
current epoch's ledger. The server then sends `refresh_ack` under E2. A
`refresh_confirm` that arrives after the commit (a lost ACK) selects the
now-current epoch, matches its recorded transaction, has its nonce
admitted, and gets the ACK re-sent WITHOUT a second commit. There is no
rollback from a committed E2 back to E1.

**Client.** `nmea_sproxy` runs the transaction inside its existing
forwarding runtime: no `forward_loop()` restart, no second receiver on the
output socket, and the input adapter, output socket, `ForwardingStats`,
session locator, and liveness/ping counters all stay put. Normal NMEA
forwarding continues under E1 while E2 is pending; after the client
receives the `refresh_ack` it switches sending to E2. A planned
`session_refresh_interval` now triggers this epoch refresh instead of a
fresh establishment handshake; `session_refresh_interval = 0` still
disables planned refresh, and proactive rekey on an unanswered keepalive
ping still performs a genuine fresh-establishment handshake for real
liveness failure.

The client caches the two canonical control messages (`refresh_init` and,
once E2 is derived, `refresh_confirm`) -- never their ciphertext -- and
re-encrypts every scheduled (re)transmission with a FRESH AEAD nonce under
the same key/generation. The signed ECDHE contributions, candidate keys,
transaction id, generations and the original transaction deadline are
never regenerated for a retry; only the datagram nonce changes, so the
server's strict per-epoch replay ledger admits every retransmission and
answers it idempotently.

The transaction is abandoned (E1 stays current) if not committed within
`REFRESH_TRANSACTION_TIMEOUT_SECONDS` (15s). Every `refresh_init` /
`refresh_confirm` datagram -- the first and every retransmission alike --
passes through one transaction-owned send-admission gate. The gate builds
and AEAD-encrypts the datagram, then takes a FRESH monotonic observation
from the same injectable clock domain immediately before the actual send,
because encryption / allocation / an OS deschedule can themselves cross
the deadline after the last state-machine check. `now >= deadline`
(equality included) refuses: nothing is transmitted, the pending candidate
is abandoned, no attempt is counted, and no progress / liveness is
reported. An admitted send counts exactly one attempt (at most
`REFRESH_MAX_ATTEMPTS` (6) per phase; no send once the budget is spent, no
busy loop), and the next allowed send is anchored to the ACTUAL admitted
send time, so a send delayed across its 2s (`REFRESH_RETRANSMIT_SECONDS`)
slot is never immediately followed by another. If a reentrant transport
replaces the transaction/phase while the datagram is in flight, the older
send does not clobber the newer transaction's timers or attempt count. A
duplicate `refresh_reply` received after E2 is derived is recognised only
when its signed transcript hash is identical to the reply the client
already verified; it is then a benign duplicate that triggers no send and,
unlike the first verified `refresh_reply`, is NOT fresh peer evidence.

Exact epoch-role authority is enforced for inbound control datagrams at
BOTH the packet dispatcher and the state machine: a `refresh_reply` is
accepted only when it authenticated under the exact current parent (E1)
epoch of the live transaction, and a `refresh_ack` only when it
authenticated under the exact pending candidate (E2) epoch, in
`CONFIRM_SENT`, matching the transaction, generation and station identity.
A parent-E1-authenticated `refresh_ack` (correct JSON, wrong epoch)
commits nothing, clears no pending state, credits no liveness and resets
no timer; only genuine server-commit evidence under E2 commits the client.

Every deadline-sensitive transition -- `refresh_reply` acceptance,
duplicate handling, retransmission, candidate abandonment, the actual
`refresh_init` / `refresh_confirm` send (see the send-admission gate
above), and the final `refresh_ack` commit -- takes a fresh monotonic
observation immediately before it acts (never a value sampled before
blocking I/O, before the signature / ECDHE / HKDF work, or before AEAD
encryption) and treats the deadline as exclusive: `now >= deadline`
rejects and abandons the candidate rather than committing, sending outside
budget, renewing liveness or retaining the candidate. An independently
valid E1 is preserved; further recovery is the existing terminal path.
`start`, `tick`, `on_reply` and `on_ack` all take a zero-argument
monotonic clock callable (the same domain as `forward_loop`'s deadlines);
there is no scalar-`now` send path.

The first verified `refresh_reply` and a genuine `refresh_ack` commit
advance `last_authenticated_peer` and therefore reanchor the peer-timeout
deadline (`last_authenticated_peer + peer_timeout`); the configured
`peer_timeout` duration remains unchanged. They do NOT clear an outstanding
keepalive `expected_ping_seq`, reset `last_ping_at`, or extend the
transaction deadline; a duplicate control datagram advances none of these.
Normal keepalive/rekey deadlines remain due on their existing schedule.
A successful commit also reanchors the client's own planned-refresh
cadence, never `session.created_at` on the server.

**Epoch-specific replay and receive admission.** Every DATA operation
retains the EXACT `CryptoEpoch` object it selected for authentication.
The server receive path is: source policy -> locator/endpoint-token
lookup -> CHEAP PRE-CRYPTO path-role classification against `PathState`
(active / candidate / retired / blocked / unknown -- this session's own
`active_path`, `candidate_path`, and `retired_path` records are matched
first and always take precedence; only a source matching none of them is
tested against the bounded relation index, where a match to a DIFFERENT
live session on this endpoint token is 'blocked'. A 'blocked' frame is
dropped before any cryptographic work exactly as a wrong-path frame was
before path migration existed; any other off-path frame takes the
server-side path migration receive path in the subsection above rather
than the ordinary path below, and an unproved off-path frame is confined
to the exact current epoch. This pre-crypto observation is NOT
authoritative for a deadline-sensitive role) ->
exact epoch selection by the plaintext selector among {retiring, current,
pending} -> per-role message gate (a pending epoch carries only
`refresh_confirm`; a retiring epoch carries only `nmea`/`ping`/`close`,
never a refresh, path change, or current-epoch replacement) -> pre-decrypt
replay check against THAT epoch's ledger -> AEAD outside the lock with
AAD binding that generation -> canonical message + source + semantic
validation -> ONE authoritative locked transaction at a FRESH authoritative
monotonic observation: expire epoch-transition and path state, re-classify
the source path against the CURRENT `PathState`, re-verify the exact
`CryptoEpoch` object is still a live role, enforce the path-role x
epoch-role x message-type authorization matrix, and admit the nonce into
THAT epoch's ledger -> ordinary action (session touch, pong, ingress
queue, candidate observation) authorized by the RETURNED authoritative
path role and admission time, never the stale pre-crypto ones. The
authorization matrix: `active` admits `nmea`/`ping`/`close` under the
current or retiring epoch; `candidate` and `unknown` admit `nmea`/`ping`
under the current epoch ONLY (a retiring epoch never authorizes an
`unknown` or `candidate` source); `retired` admits `nmea`/`ping` under the
current or retiring epoch ONLY while the grace is still live -- a
straggler whose retired grace elapsed during the AEAD is re-classified
`unknown` and, under a retiring epoch, fails closed with nothing admitted
or touched, while under the current epoch it is legitimate new-path
continuity that also opens a fresh reverse candidate; `blocked` admits
nothing. Candidate/retired deadline equality is expired (`now >=
deadline`), and a deadline is never extended by this transaction. A
receive thread that decrypts under E1 and then loses a concurrent commit
race admits its nonce into E1's own (now retiring) ledger, or is told E1
is stale -- never into E2's ledger. Each usable epoch keeps its own bounded replay
ledger for its entire accepted lifetime; the same 12 nonce bytes may
occur under independently derived E1 and E2 keys but never twice under
one key. Aggregate process-wide nonce accounting spans all live ledgers
(at most three per session), each capped at
`DATA_NONCE_MAX_PER_SESSION`. Current-epoch exhaustion is terminal for
the whole session exactly as before; pending- or retiring-epoch
exhaustion discards only that epoch and leaves the session and E1 intact.
A pending E2 is never permission to use an expired or exhausted E1, and
E1's ledger is never reset to buy time.

**Retiring epoch cutoff.** During the exact monotonic overlap window the
retiring epoch (generation `G-1`) still authenticates INCOMING
`nmea`/`ping`/`close` DATA only, with its full replay ledger retained,
so an in-flight straggler is not lost and a server pong for an E1
keepalive ping still returns under E1. The boundary is exact: `age >=
cutoff` retires it (matching every other UDPSEC TTL), and it is then
discarded deterministically (lazily per packet and by
`run_periodic_maintenance`, via a third lazy-deletion deadline heap
alongside the active and pending session heaps). Session close, idle
expiry, capacity eviction, nonce exhaustion, listener shutdown, and owner
`close()` all discard any pending and retiring epoch state.

**Bounds.** `PENDING_EPOCH_TTL_SECONDS = 30` (server-side unconfirmed
candidate lifetime; a retry never extends it),
`RETIRING_EPOCH_OVERLAP_SECONDS = 5`, and the client
transaction-timeout/retransmit/attempt bounds above. All are validated
through the existing positive-int/positive-TTL helpers (NaN, infinity,
booleans rejected). No mandatory permanent session-age cap is introduced:
an epoch's usable lifetime is the logical-session lifetime unless planned
refresh or nonce pressure triggers earlier.

This section's UDPSEC in-session epoch refresh is a real evolution of the
V2 wire format, not merely local bookkeeping: the DATA framing
(`DATA_PREFIX b"NMEA-D2"`, the one-byte epoch selector, and the
generation-bound AAD), the four authenticated refresh transcripts and
their domain-separated key schedule, and `nmea_sproxy` wire compatibility
(no legacy parser, no fallback, no downgrade, no negotiation; both
endpoints upgraded together) are all part of this format and are
documented above and in the surrounding protocol/crypto modules, not left
unspecified. The protocol version number stays `2`. What this section
does NOT change is the underlying cryptographic algorithms (P-256
ECDSA/ECDHE, HKDF-SHA256, AES-256-GCM) or the lifecycle policies
intentionally preserved from before the refresh work (TTL, capacity, LRU,
replay, and nonce-exhaustion semantics as documented above). An in-session
epoch refresh authorizes no path change; server-side active-path migration
of an established session is a separate mechanism, described in the next
subsection. A genuine fresh-establishment handshake (a new `LogicalSession`
with a fresh locator) still exists for a genuinely new session and for
terminal recovery after real liveness or security failure.

### Server-side path migration and return routability

A confirmed `LogicalSession` can migrate its authoritative outbound
network path in place, without re-establishing the session, its wire
locator, its assembly identity, its authenticated station, or its
`CryptoEpoch`. The transition is:

    Before:  S, current_epoch E, active_path A, no candidate, no retired
    Candidate: S, E, active_path A, candidate_path Candidate(B, R, g, deadline, E)
    After:   S, E, active_path B, no candidate, retired_path Retired(A, short deadline)

The exact `LogicalSession` object S and its exact `PathState` object are
unchanged; a successful migration mints no locator/handle/assembly
namespace, never invokes `install_session`/`promote_pending_session`,
releases none of S's identities, derives no traffic key, runs no ECDHE,
promotes/discards/rekeys no `CryptoEpoch`, and does not increment
`sessions_created`/`sessions_replaced`. `_session_key`, `session_locator`,
`session_handle`, `assembly_namespace`, `station_id`, `created_at`, every
`CryptoEpoch` object and its `generation`/`created_at`/keys, every epoch's
replay ledger and its admitted nonces, listener/endpoint-token ownership,
and session-registry reservations are all identical across a successful
A -> B migration. Migration does not reset or extend an epoch origin,
refresh deadline, nonce budget, session creation time, assembler lineage,
or security lifetime merely because the path changed.

**`PathState` ownership.** The `LogicalSession` owns exactly one
`active_path`, at most one `candidate_path` (a typed `CandidatePath`
record), at most one `retired_path` (a typed `RetiredPath` record), and a
bounded monotonic `path_generation` counter (generation 0 = no candidate
has ever existed; each new candidate incarnation, replacement included,
takes the next value, in the finite closed range `1 .. 2**32 - 1`, failing
closed rather than wrapping). A `CandidatePath` retains the raw sockaddr
for `sendto`, the canonical `core.sockaddr_identity` identity used for
every equality check, the opaque 32-byte challenge token `R`, its
`path_generation`, its creation/deadline, and the exact `CryptoEpoch`
object (and generation) it was opened under. A `RetiredPath` retains the
raw/canonical previous address, its retirement time/deadline, and the
`path_generation` of the candidate that displaced it. Path comparison
uses the same `core.sockaddr_identity` semantics as active-path admission
(IPv6 scope significant, flowinfo excluded, malformed/ambiguous fails
closed). Candidate and retired state are bounded per session -- there is
no unbounded address history and no queue of candidate paths. Both use
short fixed monotonic lifetimes, not wall time:
`PATH_CANDIDATE_TTL_SECONDS = 10.0` and `RETIRED_PATH_GRACE_SECONDS = 5.0`
(process constants, not operator configuration; validated through the
existing positive-TTL helper). The boundary is exact: live while
`now < deadline`, expired at `now >= deadline`. Duplicate traffic on the
same candidate never refreshes its original deadline. There is deliberately
NO per-transition deadline heap for path migration: unlike a pending epoch
(whose creation is refused while one is unresolved), a candidate path can
be replaced by fresh authenticated current-epoch traffic at an unbounded
rate, so a per-replacement heap entry would be transition-history-scaled.
Instead `PathState` itself is authoritative for its at-most-one candidate
and at-most-one retired record (a replacement overwrites the slot in place
and the old record is dereferenced), `_expire_session_path_state()`
rejects expired authority lazily in O(1) on every exact-session path
operation, and `cleanup_expired_path_transitions()` is one bounded
`O(number of live sessions)` sweep from `run_periodic_maintenance` for the
fully-idle case. Total path-housekeeping storage is thus hard-bounded at
two optional records per live session (`<= 2 * SESSION_MAX`), independent
of transition history. Regardless of physical reclamation timing an
expired record never authorizes behavior.

**Wire.** Three strict, closed-schema JSON control messages carried inside
the existing encrypted DATA channel -- same framing, locator/generation
AAD binding, source-policy-before-crypto, per-epoch nonce admission, no
new outer frame, no protocol-version bump, no negotiation. All three
share one exact member set
`{"type", "source_id", "challenge_token", "path_generation", "timestamp"}`:

- `path_challenge` (server -> candidate path, under the candidate-bound
  current epoch's server->client keys): carries the opaque 32-byte
  server-minted token `R` and the integer `path_generation`.
- `path_response` (candidate path -> server, under that same current
  epoch's client->server keys): echoes `R` and `path_generation`.
- `path_ack` (server -> newly active path, under the unchanged current
  epoch): the authenticated evidence of an already-completed server
  commit.

The observed NAT/public candidate address is never carried in a message
body: the server already knows the candidate from `recvfrom()`, and return
routability is established only when the matching `path_response` actually
arrives FROM that candidate address. `path_generation` is a path-state
generation, never a `CryptoEpoch` generation.

**Candidate discovery.** Source policy is the first gate; a denied source
performs no migration crypto or state mutation. For a DATA frame whose
locator selects a live session on the receiving endpoint token but whose
source path is not `active_path`: the frame is allowed exactly one AEAD
attempt under the exact CURRENT epoch (an unproved path may never
authenticate under a retiring epoch, and retiring-epoch traffic can never
open or prove a migration). Only after the plaintext is a semantically
permitted message for an unproved path (`nmea`, `ping`, or
`path_response`), its nonce passes that epoch's authoritative replay
admission at a FRESH authoritative monotonic observation, and the
session/epoch/path role are re-verified as the exact live owners under the
state-owner lock is a candidate created or replaced -- and the candidate is
opened for the role the AUTHORITATIVE post-decrypt classification returns
(`unknown` for a brand-new path, or an existing `candidate`), never the
stale pre-crypto role. A candidate does not change `active_path`. Authenticated `nmea` from a candidate is
admitted to the normal ingress/assembler path under the session's exact
existing `assembly_namespace` (so a multipart message may span the active
and candidate paths), but the candidate has no outbound authority: an
ordinary `pong` still goes only to `active_path`, refresh/close/path
control still resolve only through the active-path authority, and the
candidate receives only the one bounded `path_challenge`. Fail-closed for
unproved-path attempts at any other state-changing control (session close,
epoch refresh INIT/CONFIRM, a path ACK/CHALLENGE in the wrong direction,
future/unknown types): they mutate no unrelated protocol state.

**Candidate creation/replacement.** On the first eligible authenticated
current-epoch packet from a new path B, the server mints a fresh `R`,
allocates the next `path_generation`, installs `Candidate(B, R, generation,
now + PATH_CANDIDATE_TTL_SECONDS, current epoch)`, and sends exactly one
`path_challenge` to B. More eligible traffic from the SAME candidate,
under the SAME still-current epoch, keeps the identical
object/token/generation/deadline and sends nothing further (Prompt 4
issues only the initial challenge; there is no retransmission or liveness
protocol here). Eligible current-epoch DATA from a DIFFERENT new path C
while B is candidate replaces B outright: fresh token, advanced
`path_generation`, same `LogicalSession`/current epoch; B's token and
generation are stale immediately and a late `path_response` from B does
nothing. This is "newer candidate replaces older", not a queue.

A same-address candidate whose bound `CryptoEpoch` is no longer the
session's current epoch -- because a Prompt 3 in-session epoch refresh
committed while the candidate was still unproved -- is a STALE incarnation:
the next eligible packet from that same address under the new current
epoch replaces it exactly like a different-address candidate (fresh token,
advanced `path_generation`, fresh original deadline, bound to the new
current epoch), never revives or mutates the stale object in place, and
the stale `path_response` can never migrate under either the old or the
new epoch. A candidate whose target relation is already occupied by a
DIFFERENT live session on this endpoint token is not created at all (it
could never commit -- see below), and no `path_challenge` is sent.

All AEAD/JSON/packet work and every `sendto` happen outside the state-owner
lock; a short revalidation immediately before the challenge send ensures a
candidate already replaced by a newer one draws no challenge from stale
work.

**Return-routability proof and atomic commit.** Server acceptance of a
`path_response` requires ALL of: the exact live `LogicalSession` object;
the exact current `CandidatePath` object/incarnation; the candidate not
expired at fresh authoritative monotonic time; the source path
structurally equal to the candidate; the exact token `R` (constant-time
comparison); the exact `path_generation`; the candidate-bound
`CryptoEpoch` still the session's current epoch and the exact epoch the
response authenticated under; the response's nonce passing that epoch's
replay admission; and the station identity matching. A response from A,
from another new path, from another listener, for another locator/session,
under a stale candidate generation, with the wrong token, under a
wrong/retiring/stale epoch, or against an expired candidate does not
commit. "Knowing `R`" alone is never enough -- the response must arrive
FROM the candidate. A byte-for-byte replay is a replay; a re-encrypted
stale response with a fresh nonce is still semantically stale once
token/generation/candidate ownership no longer match.

The commit additionally re-checks, authoritatively under the owner lock
and independently of any earlier candidate-creation check, that the
migration target relation is NOT occupied by a different live session on
this endpoint token (occupancy can change between candidate creation and
proof). If it is, the migration fails closed BEFORE the response nonce is
consumed and before any state change: `active_path` / `retired_path` / the
old A relation mapping / the foreign B mapping are untouched, no migration
is counted, no `path_ack` is sent, and the foreign session is never
evicted, replaced, closed, or mutated. The stale-but-valid candidate
simply remains until its own (never-extended) deadline. The existing
one-active-session-per-canonical-relation invariant of `_relation_index`
is preserved; it is never turned into a multimap.

On a valid `path_response` the server commits atomically under the lock (a
pure in-memory swap on the unchanged `LogicalSession`/`PathState`): capture
old active A; make B the sole `active_path`; clear the candidate; install A
as the one `retired_path` with deadline `now + RETIRED_PATH_GRACE_SECONDS`;
retain the same `LogicalSession` and every `CryptoEpoch`/replay/identity
value; move this session's `_relation_index` entry from canonical A to
canonical B (canonical B was verified free of any foreign live session
above). No gap exists in which both A and B are ordinary outbound
authorities. The server then sends `path_ack` to B under the current
epoch, carrying the matching token/generation. `path_ack` is evidence of
an already-completed commit: its loss never rolls the server back to A.
Migration does not move `last_seen` backwards, `created_at`, epoch
`created_at`, a planned epoch-refresh origin/deadline, nonce ledgers, the
assembler namespace, `session_handle`, or the locator.

**Retired path.** After A -> B, A is retained only as `retired_path` for
its short grace. Retired A is never ordinary outbound authority, cannot
automatically reactivate, cannot answer a `path_response` for the
committed candidate, and cannot perform close/refresh/path-control changes
merely because it was previously active. During the grace, replay-safe
`nmea`/`ping` from A is admitted as late in-flight data under the same
`LogicalSession`/`assembly_namespace` but creates no reverse candidate and
does not change active B; an authenticated `ping` from retired A gets no
ordinary pong. `age >= grace` retires A with no special standing left:
genuinely fresh eligible current-epoch traffic from A is then simply a new
path and MUST run a fresh candidate/challenge/response cycle (a new token
and `path_generation`) before B -> A can occur. "Previously active" is
never proof.

**Interaction with epoch refresh (conservative Prompt 4 boundary).** A
candidate is bound to the exact current `CryptoEpoch` object/generation at
creation; `path_challenge` uses that epoch; a `path_response` can commit
only while that exact epoch is still current. If an epoch refresh commits
before the path proof, the candidate becomes stale and cannot commit under
old authority -- and the next eligible packet under the new current epoch,
including one from the SAME candidate address, opens a fresh candidate
incarnation bound to the new epoch (fresh token, advanced
`path_generation`, fresh deadline) rather than being treated as duplicate
traffic on the stale one. A retiring epoch cannot open or prove a
migration. Path
migration never promotes/discards/rekeys `CryptoEpoch` state, and epoch
refresh never inherits authority from an unproved candidate path. A stale
candidate cannot resurrect a closed/expired/exhausted session. The full
migration x refresh x lifecycle race matrix is deferred to a later stage;
these are the safe primitives it will build on.

**Lifecycle.** Session expiry, close, capacity eviction, nonce exhaustion,
listener teardown, and owner `close()` all discard the candidate and
retired path with the session, because `PathState` belongs to that exact
`LogicalSession`; no candidate/retired object survives as authority after
the session is removed, and stale prepared challenge/ack work cannot
resurrect it. Namespace leases for candidate and retired NMEA follow the
same ownership/release rules as active NMEA. This subsection describes the
server-side mechanism; `nmea_sproxy`'s own choreography against it --
answering `path_challenge`, and treating a committed `path_ack` as
liveness evidence -- is the next subsection.

### Client-side path-migration choreography (`nmea_sproxy`)

`nmea_sproxy` never decides to migrate, never tracks a candidate the way
the server does, and never learns its own apparent address -- but it is
not stateless: it owns one small, bounded, O(1) piece of per-session proof
state (`_ClientPathMigration`), separate from and independent of session
age, refresh scheduling, and the normal keepalive clock, that exists
specifically so a PATH_ACK can only ever be recognised against a
PATH_CHALLENGE this exact client actually observed and answered.

That state is a greatest-observed-challenge-generation watermark
(`path_generation` plus its `challenge_token`), which persists for the
life of the confirmed session -- surviving proof consumption, proof
expiry, and an in-session `CryptoEpoch` refresh -- plus at most ONE
pending proof incarnation. A valid strictly newer challenge advances the
watermark and immediately discards any older pending proof, before trying
to send its response, even if that send fails.

Both control messages are decrypted through the same per-epoch resolution
(`_ClientEpochSet.inbound_epoch`) from the same pinned remote tuple as
`refresh_reply`/`refresh_ack`, and accepted only under the session's exact
CURRENT epoch (never a pending or retiring one, since migration never
promotes/discards/rekeys the `CryptoEpoch`):

- `path_challenge`: a strictly newer generation than the watermark -- or a
  retry of the watermark's own generation for which no proof was yet
  successfully established -- gets exactly one `path_response` sent,
  echoing its `challenge_token`/`path_generation` verbatim; only a
  SUCCESSFUL send opens the one bounded pending proof for that watermark
  generation. If no proof has yet been established for that incarnation,
  a failed send leaves none and permits a retry with the same
  generation/token. The proof's fixed monotonic deadline is anchored exactly
  once by a fresh clock observation after send completion; its `CryptoEpoch`
  generation authority and the outstanding keepalive ping
  sequence (or lack of one) at that exact moment are captured into it. A
  duplicate of an already-established incarnation may still be answered,
  but never moves its deadline, never recaptures its ping sequence, and
  never revives it once expired or consumed; a failed resend leaves that
  existing proof unchanged. A same-generation challenge whose token
  conflicts with the watermark's, or a strictly older generation, gets no
  response and no state change (fail closed). Either way, a PATH_CHALLENGE
  carries no liveness effect of its own -- it is not yet proof the server
  has committed anything, only that the server is probing a path.
- `path_ack`: the server's authenticated evidence of an already-committed
  A -> B migration for this exact session. It is recognised as fresh
  migration-completion evidence ONLY when it exactly matches the one live
  pending proof -- same `path_generation`, same `challenge_token`
  (constant-time comparison), the exact `CryptoEpoch` generation authority
  the matching `path_response` was sent under, and a fresh monotonic
  observation strictly before the proof's deadline. The proof must also
  match the watermark's generation and token. It is then consumed exactly
  once: a replay, a duplicate, a PATH_ACK delivered for the first time only
  after its proof's deadline has already passed,
  an unseen or superseded generation, a wrong token, or one bound to a
  since-superseded epoch are all authenticated-but-benign and change nothing.
  A matched PATH_ACK does not gain final liveness or ping-clear authority
  merely by passing the post-receipt deadline check: that check, and the
  `drive_refresh()` and acknowledgement logging that follow it, can
  themselves consume enough time for a terminal deadline to become due. Its
  state effects are therefore gated by a bounded, two-phase final admission,
  never by the ordinary per-iteration deadline helper (which is itself
  effectful -- it can send a keepalive ping or start an in-session refresh,
  and either could block past a terminal deadline). Phase one is a
  side-effect-free terminal classification (`terminal_deadline_reason`): no
  send, no logging, no refresh start/tick, no mutation, no second clock
  sample -- pure local comparisons against one already-captured `now`. If it
  finds a terminal deadline due, that reason wins outright and the ACK gets
  no effects at all. Otherwise phase two performs AT MOST the one
  non-terminal maintenance action that is due at that same instant --
  sending a keepalive ping with none outstanding, or starting/reanchoring a
  supported in-session planned refresh -- and only then does the
  implementation take a fresh monotonic sample and run the SAME pure
  terminal classification again. Any terminal deadline due at that final,
  post-maintenance classification -- `peer_timeout`, planned-refresh (when
  in-session refresh is unsupported), or keepalive/proactive-recovery, exact
  equality included -- still wins outright: the ACK is
  authenticated-but-benign and neither reanchors liveness nor clears a ping.
  Between that final classification and the mutation of
  `last_authenticated_peer`/`expected_ping_seq` there is no further
  potentially blocking work of any kind. A supported planned refresh that is
  due at the same instant as unresolved-ping proactive recovery is
  deliberately classified as non-terminal maintenance, never as a terminal
  reason, precisely so it can never mask recovery: when both are due at
  once, proactive recovery wins and the refresh is never even started. Its
  ping-clear authority is the concrete ping sequence captured at the
  successful `path_response` send, carried unchanged until that final
  admission passes. It clears an outstanding ping ONLY if the captured
  sequence is not `None` and that exact sequence is still outstanding at
  that final admission instant. A proof that captured no ping has no
  ping-clear authority, but its matched ACK still advances liveness. A
  different, later ping -- including one created by that same final
  admission's own maintenance phase when it sends a fresh keepalive ping --
  is left untouched.

A matched PATH_ACK is proof of liveness, not a lease renewal or a fresh
session. Because it advances `last_authenticated_peer` -- to the fresh
monotonic sample taken AFTER any due non-terminal maintenance at its final
terminal-deadline admission, never an older sample taken before that
maintenance, `drive_refresh()`, or acknowledgement logging -- it
correspondingly reanchors the `peer_timeout` deadline
(`last_authenticated_peer + peer_timeout`) forward from that moment,
exactly as an accepted pong already does; this is the intended, expected
effect, not something the implementation avoids. What it never does is
reset `session_started_at` outside of that admission's own maintenance,
extend or postpone the planned-refresh deadline, or touch
`last_ping_at`/restart or delay the normal keepalive schedule beyond that
same maintenance -- an already-due terminal deadline still wins at that
final admission, before the ACK's liveness effect is applied. Migration
stays strictly a transport/session concern: it never pauses, buffers, or
otherwise touches NMEA/application forwarding.

### 11.1 Migration x epoch-refresh x lifecycle binding (Major Prompt 6)

Path migration is not timeless authority: a migration transaction (server
`CandidatePath`, client pending proof) is bound to the exact
`LogicalSession` incarnation, candidate path/generation, challenge token,
and `CryptoEpoch`/generation live at the moment it was created, and
remains valid only while all of those still hold.

1. **Epoch binding.** A candidate records the exact `CryptoEpoch` object it
   was authenticated under. Migration commit (server `PATH_RESPONSE`) and
   client ACK admission both require that recorded epoch to still be the
   live current epoch by object identity, not merely a matching
   generation number.
2. **Epoch swap invalidates outstanding authority.** If an in-session
   epoch refresh commits (E1 -> E2) while a candidate/challenge created
   under E1 is still outstanding, that candidate is left bound to the
   now-retiring E1 object -- neither rebound to E2 nor extended -- so its
   old response can never commit under either the demoted E1 encoding or
   a re-encryption of the same stale token under E2. It simply lapses at
   its own unmoved deadline; fresh traffic from the same address opens a
   brand-new, independently-anchored candidate under the new current
   epoch, and only that fresh incarnation may migrate the path.
3. **Migration does not postpone refresh.** A path-migration commit never
   moves a pending or in-flight refresh transaction's deadline,
   retransmission cadence, attempt count, or transaction identity.
4. **Refresh does not reset or extend migration/session lifecycle.** An
   epoch-refresh commit never resets `path_generation`, extends a live
   candidate's TTL or a retired path's grace deadline, or grants the
   session (or an in-flight migration) any generic lease extension;
   conversely, migration never grants the session a generic lease
   extension either. Refresh and migration/session-lifecycle timers are
   independent fields, mutated only by their own transaction.
5. **Deadlines advance through processing delay.** Candidate TTL and
   retired-path grace are monotonic-time lifetimes resampled fresh at the
   authoritative commit/admission point, never a timestamp captured
   earlier at packet receipt -- queueing, backpressure, or scheduler delay
   between receipt and commit cannot freeze or extend them, and exact
   equality at a deadline is treated as expired on both the server and
   client.
6. **Terminal session lifecycle wins.** Once a session becomes terminal
   (idle/peer-timeout expiry, graceful close, nonce-ledger exhaustion of
   the current epoch, or owner/listener teardown), any in-flight or
   delayed migration control (a late `PATH_RESPONSE`/`PATH_ACK`) has no
   authority to resurrect it -- the session, and with it any live
   candidate/retired-path state, is simply gone.
7. **Replay/nonce ownership is per-epoch, not per-path.** A migration
   commit's own `PATH_RESPONSE` nonce is admitted into the same replay
   ledger as ordinary session traffic for that epoch. Changing the source
   address via migration never resets or duplicates that ledger: the same
   ciphertext/nonce pair stays exactly as replay-rejected when represented
   from the active, candidate, or retired path.
8. **Shutdown close targets the current active path.** A best-effort
   graceful close sent during owner/listener shutdown always resolves the
   destination from the session's current, validated `active_path` at
   that moment -- never the original establishment tuple, a live
   candidate, or a retired path -- regardless of whether a refresh is also
   mid-flight at shutdown time.

## 12. Routing snapshot boundary

Routing configuration, `RouteDefinition.to`, route errors, status, control
responses and CLI-visible target identifiers use canonical external strings
such as `udp:aishub`. Initial and dynamically replaced routing candidates
resolve every such name once through the immutable
`Forwarder.target_id_by_name` mapping before installation. A candidate is
installed only after every name has resolved and the complete immutable numeric
route program has been built. Failed compilation leaves the active routing
snapshot unchanged.

The descriptive `RoutingTable.match(source_id)` API remains string-facing and
returns ordered route names and ordered unique external target strings.
Production frame processing instead uses `match_target_ids(source_id)`, whose
immutable compiled route program contains resolved source sets and ordered
numeric targets but no route names. Matching performs no external-name lookup.
Route declaration order and target declaration order are retained, and a
target matched more than once appears only at its first occurrence.

Numeric egress IDs are dense zero-based positions in the immutable forwarder
destination tuple. They are process-local implementation values, are never
written to routing configuration or control JSON, and may change after a
restart when destination declaration order changes. Unnamed legacy
destinations have numeric IDs even though they have no external routing name.

After processing capacity is acquired, ingress fan-in acquires exactly one
immutable routing snapshot for the accepted or successfully coerced frame and
immediately binds it into one `ProcessingWorkItem`. If that snapshot contains
a table, orchestration calls the numeric target-only matcher exactly once with
`frame.source_id`. Unsupported queue items and invalid compatibility events
acquire no processing capacity, snapshot, or match. A frame still waiting for
processing capacity has not captured routing state; once admitted, every
accepted sentence extracted from that frame uses the one resolved numeric
tuple even if the work item waits before processing. A routing-table
replacement can therefore affect a frame that is still waiting for admission,
but not a work item that has already been admitted and bound.

The frozen, slotted processor view contains exactly:

```text
ProcessingSnapshot(
    routing_generation: int,
    deduplication_mode: DeduplicationMode,
    target_ids: tuple[EgressTargetId, ...],
)
```

It contains no routing table, routing state, mapping, transport or asyncio
object. An absent or disabled routing table selects `GLOBAL` mode and passes
all numeric forwarder IDs, including unnamed destinations. An enabled routing
table selects `PER_TARGET` mode and passes the one resolved tuple. Runtime
orchestration must supply `legacy_target_ids` explicitly: omission is a
call-contract error, while an explicit empty tuple represents a genuinely
empty global destination registry. Accordingly,
`GLOBAL + ()` means global deduplication remains active with no configured
forwarder destinations. A globally unique message still completes normal
processor state changes and output construction, but its explicit empty target
tuple results in no datagrams. `PER_TARGET + ()` means routing is enabled but
the source matched no target; it performs no global deduplication admission,
does not invoke the output builder, and returns no processor output while
retaining normal assembler and multipart metadata cleanup. Snapshot
construction preserves target order and rejects duplicate numeric IDs rather
than silently normalizing them; production numeric matching already returns a
unique first-occurrence tuple.

## 13. Campaign D processor/egress boundary

`PythonDataPlaneProcessor.process(frame, snapshot)` completes synchronous
processing of the entire accepted frame and constructs the complete returned
`OutputBatch` before orchestration begins its first asynchronous egress send.
The admitted `ProcessingWorkItem` already carries the frame's target-only
snapshot before the processor stage dequeues it. Parsing, assembly, multipart
metadata observation and cleanup,
deduplication decisions, TAG formatting, wall-clock observations used for
formatting, GID generation, and `touch_s` effects belonging to that frame
therefore all occur before the first send begins.

The public, transport-agnostic result contracts are frozen and slotted:

```text
ProcessorOutput(
    message: bytes,
    target_ids: tuple[EgressTargetId, ...],
)

OutputBatch(
    outputs: tuple[ProcessorOutput, ...],
)
```

`OutputBatch` is the processor return value and is a valid result when empty.
It preserves output order and the exact `ProcessorOutput` object identities
while defensively converting an accepted mutable sequence to a tuple. It
contains no completion Future, queue, transport, asyncio object, or other
runtime state.

Each `ProcessorOutput` contains one completely formatted output sentence and
its explicit ordered numeric target IDs. `ProcessorOutput.message` is an exact
immutable `bytes` payload, normally terminated by CRLF. The boundary accepts
an existing `bytes` object without copying it and rejects `str`, mutable
buffers, views, and other payload types. It does not decode or encode the
payload and does not require CRLF at this general immutable boundary. Target
order and repeats are preserved, and an empty target tuple is valid.

The single egress stage dispatches `OutputBatch.outputs` sequentially in their
stored order. Every output uses the one numeric production path,
`Forwarder.send_to_ids(output.target_ids, output.message)`. `Forwarder.send()`
and the string-targeted `Forwarder.send_to()` remain public compatibility APIs
but production orchestration calls neither. A send failure stops dispatch
before any later output is sent, but it does not undo processor state,
deduplication state, multipart metadata cleanup, wall-clock observations, GID
generation, `touch_s` effects, or already constructed later outputs. The
runtime-only completion signal described below is an ordering barrier, not an
acknowledgement to an ingress source or a network-delivery guarantee. The
boundary provides no transactional delivery, rollback, replay, ingress
acknowledgement, delivery acknowledgement, or recovery guarantee, including
after a partial multi-fragment send.

`core.output_builder.build_output_bytes()` is the sole production output
builder. It delegates canonical TAG formatting and checksum calculation to the
existing string-facing `meta_writer.wrap_with_meta()` implementation, appends
exactly one CRLF terminator to the complete TAG-plus-NMEA text, and then
performs one explicit UTF-8 encoding operation. Encoding therefore occurs once
for each emitted NMEA sentence, including once for each emitted multipart
fragment, and never once for an entire multipart group. TAG fields, NMEA text,
and framing are not encoded separately.

The forwarder accepts the immutable bytes payload and passes the same object
unchanged to `transport.sendto()` for every selected destination. It performs
no encoding, decoding, normalization, or per-destination payload copy. Debug
presentation is observational only: it removes one trailing `b"\r\n"` for
display when present and decodes that display view as UTF-8 with replacement
for invalid input. The original unmodified bytes object remains the object
sent to the forwarder. Invalid UTF-8 is replaced only in the display view and
cannot alter the network payload.

The unified numeric egress path preserves per-sentence payload construction,
same-object reuse across selected destinations, sequential output ordering,
and sequential destination ordering. Campaign E4 introduces no native API or
ABI, bindings, IPC, multiprocessing, worker pool, batch-level payload
concatenation, or egress concurrency.

This whole-frame-before-egress ordering intentionally replaces the former
processing/send interleaving and is part of the Campaign D processor boundary.
Routing generation is observational only and does not reset or otherwise
mutate processor state.

### Runtime stages and ordered handoff

The Python reference runtime has one production path:

```text
ingress producers
    -> ingress fan-in
    -> processor stage
    -> egress stage
    -> network forwarders
```

The stages run in one process. Each configured input has its own FIFO ingress
queue and one fan-in reader; the order in which those readers successfully
admit work establishes the order in the shared processor-stage queue. This is
not a total arrival-order or fairness guarantee across inputs. Exactly one
long-lived processor-stage consumer uses the runtime-owned, long-lived
`PythonDataPlaneProcessor`, and exactly one long-lived egress-stage consumer
dispatches its results. The egress stage performs no routing matching, parsing,
assembly, multipart metadata work, deduplication, TAG construction, GID
generation, or processor-state mutation.

For each supported frame, fan-in coerces the queue item once and, only after
obtaining processing capacity, resolves exactly one target-only
`ProcessingSnapshot` and constructs one immutable `ProcessingWorkItem`. The
processor stage calls the configured `DataPlaneProcessor` exactly once for that
work item and treats the complete returned `OutputBatch` as the frame's one
ordered processor result. Unsupported queue items and invalid compatibility
events are rejected before processing admission, snapshot acquisition, target
matching, or processor invocation. An `OutputBatch` with no outputs completes
locally because it has no egress work.

After handing a non-empty batch to egress, the processor stage must await an
explicit process-local completion acknowledgement. It must not consume or
process the next ingress item until egress has dispatched the current batch's
final output and acknowledged success. Removing a batch from an inter-stage
queue does not satisfy this barrier. Thus processor work cannot run ahead
across frames while prior egress is incomplete. The barrier constrains
processor execution, not fan-in admission: later work items may already be
queued with their snapshots bound. After the barrier completes successfully,
the processor stage dequeues the next admitted work item. A routing replacement
while the prior batch is blocked affects only frames that have not yet been
admitted and bound; routing generation remains observational and cannot reset
processor state.

If a processor call fails, no batch is handed to egress and the exception
propagates through runtime lifecycle management. If egress fails, it signals
that failure through the completion barrier, stops the current batch before
later sends, and propagates the exception through runtime lifecycle management.
The already completed processor effects retain the non-rollback semantics
above, and no later accepted frame is processed after the failure. Runtime
shutdown or cancellation must resolve or cancel pending stage work and
acknowledgements so that no stage remains blocked or orphaned.

The inter-stage queues and completion acknowledgement are private
runtime-orchestration mechanisms. The private `_EgressBatch` envelope contains
one public `OutputBatch` and one process-local completion Future; the Future
remains outside `OutputBatch` and every other public data-plane contract. These
mechanisms define neither a native API or ABI nor an IPC protocol. The runtime
uses no multiprocessing, threads, worker pool, or second processor
implementation.

### Runtime lifecycle supervision

Every essential long-lived runtime task—each UDP and UDPSEC ingress producer,
ingress fan-in, the processor stage, the egress stage, and the sparse
statistics heartbeat—is owned by one process-local supervision lifecycle. The
fan-in in turn owns its private reader tasks. Failure, cancellation, or
unexpected normal return by any essential task terminates the runtime: every
still-running sibling is cancelled, and all owned task outcomes are awaited and
retrieved before the primary failure, or a clear unexpected-termination error,
propagates. Exceptions are not left detached from the runtime lifecycle.

External cancellation cancels and awaits all owned tasks and is re-raised.
Stage cleanup resolves or cancels pending batch-completion acknowledgement
state, and fan-in cleanup cancels and awaits every private reader, so neither a
blocked acknowledgement nor a nested reader outlives its owner.

This lifecycle defines termination only. It provides no automatic restart,
retry, persistence, or delivery replay. Supervision is process-local and
defines no coordinator/worker supervision or IPC protocol.

### Output formatting and cleanup

For emitted multipart output, the first fragment receives the primary `c`, `s`,
and `g` TAG metadata. Continuation fragments receive the existing continuation
form containing `g` without repeating primary `c` or `s`.

Normal multipart completion consumes its metadata contexts even when no route
matches or deduplication suppresses all output. Every key reported through an
assembler outcome's `discarded_keys` must remove the processor's cached
multipart `s`, `c`, and `g` contexts before metadata from the current arrival is
observed. If the processor directly invokes `cleanup_expired()` or
`reset()`, it must apply their returned keys through the same cleanup path.
External assembler callers are likewise responsible for consuming returned
lifecycle keys to synchronize metadata they own.

## 14. Campaign F worker-readiness runtime contract

Campaign F establishes bounded, observable, process-local boundaries around
the current Python runtime. It prepares those boundaries for later process
separation; it does not create operating-system worker processes.

### Bounded queues and processing admission

One production `main()` invocation owns the following queue topology:

- one private bounded ingress queue for every configured UDP or UDPSEC input,
  with a default capacity of 1024 `IngressFrame` items per input;
- one shared bounded processing-admission queue, with a default capacity of
  1024 `ProcessingWorkItem` items; and
- one bounded egress queue with capacity 1, whose private runtime items each
  carry one `OutputBatch` and its process-local completion Future.

All capacities count queue or work items, not payload bytes. A producer awaits
its private ingress queue, each fan-in reader awaits processing admission, and
the processor stage awaits egress-queue capacity. An operation that encounters
a full stage queue waits and applies backpressure; these aismixer queues do not
implement a drop-on-full branch. That waiting supplies no network-delivery,
durability, replay, or recovery guarantee. UDP can lose data outside these
queues, and fail-fast shutdown does not replay queued work.

Private ingress queues keep one input's queued backlog from consuming another
input's private queue capacity. Each fan-in reader may nevertheless dequeue
and hold one supported frame while waiting for shared processing capacity, and
the held frame is not included in private queue depth. Reader scheduling and
shared admission define no fairness or total arrival-order guarantee between
inputs. This contract is separate from the bounded serial-input queue inside
`nmea_sproxy`, whose overflow policy is not an aismixer runtime-stage policy.

Processing admission reserves a shared capacity permit before invoking the
work-item factory. While a frame waits for that permit, no routing snapshot is
read and no `ProcessingSnapshot` or `ProcessingWorkItem` exists for it. After
the permit is granted, one routing snapshot is read, target matching is
performed when routing is enabled, and the exact `IngressFrame` plus the
resulting frozen `ProcessingSnapshot` are synchronously constructed as one
frozen `ProcessingWorkItem`. Construction and immediate queue insertion have
no asynchronous suspension point between them. Construction or insertion
failure releases the reserved capacity; cancellation while waiting does not
invoke the factory.

The processing permit is released as soon as the processor stage dequeues the
work item, before `process()` begins. Processing-queue depth therefore measures
admitted queued work, not the active processor call. Fan-in can admit and bind
later work while the processor stage is waiting for an earlier non-empty
batch's egress completion. Once admitted, a work item's snapshot remains fixed;
only a frame still waiting for capacity can observe a later routing
replacement at its eventual admission.

### Processor ownership, lifecycle, and reset

Production constructs exactly one `PythonDataPlaneProcessor` inside each
`main()` invocation and gives it to exactly one serial processor-stage
consumer. There is no import-time global processor. Its assembler,
deduplicator, `SourceState`, multipart `s`/`c`/`g` context maps, processing
configuration, helper references, and processor counters belong exclusively
to that processor instance. Injected mutable components become lifecycle-owned
by the instance and must not be shared or reset externally. Other runtime
owners—queues, routing state, forwarder, egress metrics, and the statistics
provider—remain separate process-local components.

`process()`, `reset()`, and `metrics_snapshot()` are synchronous. The owner
must serialize `process()` and `reset()`; the processor adds no locking and has
no asynchronous start, stop, close, or worker lifecycle.

A successful `PythonDataPlaneProcessor.reset()` performs these steps in order:

1. reset the assembler and count discarded pending groups;
2. reset the deduplicator and count discarded live entries;
3. reset `SourceState` and count discarded live source entries;
4. count and clear the multipart `s` contexts;
5. count and clear the multipart `c` contexts; and
6. count and clear the multipart `g` contexts.

It returns one immutable report with the exact shape:

```text
ProcessorResetReport(
    assembler_groups_discarded: int,
    dedup_entries_discarded: int,
    source_entries_discarded: int,
    multipart_s_contexts_discarded: int,
    multipart_c_contexts_discarded: int,
    multipart_gid_contexts_discarded: int,
)
```

Reset retains the processor and owned-component identities, processing
configuration, injected clocks and GID generator, configured TTLs and capacity
limits, assembler and deduplicator cumulative and peak statistics, and all
processor process/output/reset metrics. It does not drain stage queues, alter
routing or forwarder state, clear queue or egress metrics, or replace the
processor. Previously retained deduplication and multipart/source live state no
longer affects later processing after a successful reset.

Reset is ordered and fail-fast, not transactional. An exception stops later
owners, preserves the effects of earlier successful steps, performs no
rollback, propagates the original exception, increments `reset_failed`, and
clears `reset_in_flight` in `finally`; no report is returned. A successful call
increments `reset_completed`. Every call first increments `reset_calls` and
`reset_in_flight`, including an empty successful reset. Production shutdown,
routing changes, and the current control protocol do not invoke processor
reset; it is an established lifecycle boundary, not an operator reset command.

### Ordered processor-to-egress handoff and supervision

The Campaign D ordered handoff remains in force. One serial processor call
produces one complete immutable `OutputBatch`. An empty batch finishes locally.
A non-empty batch is placed into the bounded egress queue and the processor
stage waits for its completion Future. The single egress stage dispatches the
batch's `ProcessorOutput` values sequentially in tuple order and acknowledges
completion only after every awaited local `send_to_ids()` call returns.
Processor execution of the next work item cannot begin before that
acknowledgement, although later work may already have been admitted and bound.

Egress failure stops later outputs in the batch and fails the completion
barrier. Already completed processor effects and local sends are not rolled
back, and later admitted work is not processed after fail-fast shutdown. Local
send completion is neither remote receipt nor a delivery acknowledgement.

Every UDP and UDPSEC producer, fan-in, processor-stage, egress-stage, and sparse
statistics-heartbeat task is an essential task in one process-local fail-fast
supervision lifecycle; fan-in similarly owns its private readers. Failure,
unexpected return, or unexpected cancellation terminates siblings and retrieves
their outcomes. This is supervision of asyncio tasks in one service process. It
supplies cleanup and termination, not a coordinator process, ingress or egress
worker processes, IPC, cross-process routing-snapshot distribution, automatic
worker restart, recovery, or replay.

### Pull-based runtime statistics

Runtime metric owners return new frozen, slotted snapshot values when pulled.
The implemented categories and field meanings are:

| Snapshot | Fields and normative meaning |
|---|---|
| Queue | `name`; item `capacity`; current and lifetime-high `depth` / `peak_depth`; successful `enqueued` / `dequeued`; historical put or admission attempts that initially encountered unavailable capacity in `put_waits`; and currently outstanding such waits in `current_put_waiters`. Cancelled waits remain historical but do not count as enqueues. |
| Processor | `process_calls`, `process_completed`, `process_failed`, and current `process_in_flight`; successful zero-output `outputless_calls`; successful non-empty `output_batches`; total `ProcessorOutput` values in those batches as `output_messages`; and the corresponding `reset_calls`, `reset_completed`, `reset_failed`, and current `reset_in_flight`. Outputs count constructed processor results, not deliveries. |
| Egress operation | `batches_started`, `batches_completed`, `batches_failed`, `batches_cancelled`, and current `active_batches`; plus `outputs_started`, `outputs_completed`, `outputs_failed`, `outputs_cancelled`, and current `active_outputs`. One output operation represents one `ProcessorOutput`, regardless of its target count. Outputless processor calls create no egress operation. |
| Input traffic | Input `name` and `kind`; raw `transport_packets` / `transport_bytes` observed immediately after socket receive; and `accepted_frames` / `payload_bytes` counted only after a constructed frame has completed private ingress-queue admission. Transport counts can therefore include denied, malformed, handshake, or other non-frame UDP/UDPSEC datagrams. |
| Output traffic | Numeric `target_id` and optional external `name`; per-target local `dispatch_attempts`, `dispatch_completed`, `dispatch_failed`, `messages`, and `bytes`. Every configured destination, including unnamed legacy destinations, has its own row in numeric order. Completion does not mean remote UDP receipt. |
| Aggregate runtime | Ordered ingress-queue snapshots plus the processing queue, processor, egress queue, and egress-operation snapshots. Detailed input and output traffic are deliberately separate pulls, not aggregate fields. |

For each selected target, `dispatch_attempts` increments before transport setup
or send. Only a successful local return increments `dispatch_completed`,
`messages`, and the exact payload `bytes`; any `BaseException` increments
`dispatch_failed` and is re-raised. A cancellation during that per-target
operation is therefore failed at the output-traffic layer, which has no
cancelled field, while the separate egress-operation snapshot records the
corresponding output and batch cancellation.

The public snapshot field invariants include:

- queue `enqueued - dequeued == depth`, with
  `0 <= depth <= peak_depth <= capacity` and
  `put_waits >= current_put_waiters`;
- processor `process_calls == process_completed + process_failed +
  process_in_flight`, `process_completed == outputless_calls +
  output_batches`, and `output_messages >= output_batches`;
- processor `reset_calls == reset_completed + reset_failed +
  reset_in_flight`; and
- egress batch and output `started` counts each equal their respective
  completed, failed, cancelled, and active counts.

Current depth, active-operation, in-flight, and current-waiter fields are
gauges at observation time; the remaining counts and peaks are in-memory
lifetime values of their owning component instance. Queue owners and the egress
operation owner have no reset operation. Processor reset does not zero
processor metrics or any other runtime owner's counters.

Statistics are pull-based. Reading them creates fresh immutable internal
snapshots and neither mutates processing state nor resets counters. The
aggregate provider holds references to existing owners and pulls each one once
in a fixed sequence; it is not a stop-the-world or transactionally atomic view
across independently changing owners. Protocol serialization produces ordinary
JSON values from those snapshots. Metrics are process-local and non-durable;
restart begins new owner lifetimes. No Prometheus exporter, push or distributed
collector, persistence layer, cross-process aggregation, rate calculation, or
historical time series is implied.

### Read-only local control exposure

When the optional local version-1 control plane is enabled, it exposes three
read-only protocol methods:

| Protocol method | Parameters and result |
|---|---|
| `runtime.statistics` | `params` must be absent. One aggregate pull returns `ingress_queues`, `processing_queue`, `processor`, `egress_queue`, and `egress_operations`. |
| `runtime.statistics.inputs` | `params` may be absent, empty, or exactly `{ "input": <non-empty string> }`. One detailed input-traffic pull returns `inputs` in runtime declaration order, optionally filtered by exact input `name`; no match returns an empty list. |
| `runtime.statistics.outputs` | `params` may be absent, empty, or contain exactly one of non-negative integer `target_id` or non-empty string `name`. One detailed output-traffic pull returns `outputs` in numeric target order, optionally filtered by that exact value; no match returns an empty list. |

These methods only read the injected statistics provider and cannot replace,
disable, or otherwise mutate routing or data-plane state. Conversely, routing
status and mutation methods do not pull statistics.

Each `runtime.statistics.inputs` row carries two deliberately separate
string fields: `name` is the stable, machine-facing selector identity --
the exact value `{ "input": ... }` matches against -- and `display` is
the separate, purely operator-facing rendering. `name` is
address-independent by construction, not merely differently formatted
from `display`: for an ingress input lacking a configured `id`, `name` is
exactly `role-ingress:index` (the declaration position within its role's
list, which AISMixer's own startup already requires to be unique), never
a rendered endpoint of any form -- this project's earlier bracketed-IPv6
selector convention (`[ip]:port`) has been removed outright, not relocated
to another field, since it was project-owned and never required by
UDPSEC, socket identity, or the OS. A configured `id` remains part of
`name` (`role-ingress:index:id`), since an operator-chosen label is
already address-independent. `display` is the configured `id` verbatim
when one exists, otherwise this project's tcpdump-style endpoint
convention (`ip.port` for IPv6, `ip:port` for IPv4). `display` is never
matched against a filter and must never be parsed back into an identity.
`aismixerctl`'s interactive table renders both -- `display` under an
`INPUT` column, `name` under a `SELECTOR` column -- so an operator can see
the value to pass back as a filter.

`ROUTING_CONTROL_PROTOCOL_VERSION` is `2`. It was deliberately bumped from
`1` because the `runtime.statistics.inputs` result schema and the unnamed-
input selector convention both changed in a way this local control
protocol does not treat as backward-compatible; a version-`1` request now
fails closed with `ERROR_UNSUPPORTED_VERSION`, with no negotiation or
downgrade. This version number is entirely local to AISMixer's optional
control plane and is unrelated to `UDPSEC_PROTOCOL_VERSION` (currently
`2` for a different reason -- UDPSEC's own wire revision); the two must
never be conflated.

`aismixerctl` presents these protocol methods as `show statistics`,
`show statistics inputs [INPUT]`, and `show statistics outputs [OUTPUT]`.
Those are CLI spellings, not additional protocol methods. One-shot CLI use
prints the validated JSON response envelope; the interactive shell renders
successful statistics results as tables.

## 15. Explicit limitations and deferred decisions

The following boundaries are compatibility limitations or deferred decisions,
not additional guarantees:

1. Blank sequential IDs retain the cross-transmission ambiguity described in
   section 6 for the live TTL correlation window.
2. TAG-`g` part and total consistency is neither assembler identity nor checked
   against the NMEA part and total by the processor.
3. Single-sentence and multipart `c:0` behaviour is intentionally not unified.
4. Send-failure recovery and transactional multi-fragment delivery remain out
   of scope.
5. Durable storage, AIS semantic decoding, analytics, and spoof detection are
   not part of this contract.
6. Extraction checks checksum-field syntax but does not validate checksum
   arithmetic.

## 16. Native implementation conformance

A future native processor should be checked through differential tests against
the Python reference for:

- ordered output sentences and TAG metadata;
- lifecycle outcome status and deterministic discarded keys;
- timestamp and group-ID selection;
- single and multipart deduplication decisions;
- routing targets; and
- explicit no-output cases.

Conformance does not define or require a C or C++ API or ABI.

## 17. Campaign A baseline

- Final branch: `main`.
- Final full-suite result: `765 passed, 18 skipped in 10.30s` (783 collected).
- Baseline date: 2026-07-22.
- Final commit immediately preceding this task:
  `48b1b09 Harden forward loop against non-string ingress payloads`.
- This document and the regression-test naming/coverage cleanup introduce no
  production behaviour change and select no new policy.

This contract was consolidated at the end of Campaign A.

## 18. Campaign B closure baseline

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

## 19. Campaign C closure baseline

- Closure snapshot date: 2026-07-25.
- Branch: `main`.
- Audited source commit:
  `8f3e608611bfc9e6c4f0dc92e5087618917a354d` (`8f3e608`).
- Environment: Python 3.14.5 on Windows 11
  (`Windows-11-10.0.26200-SP0`, AMD64).
- Focused results:
  - ingress frame and compatibility coercion: `48 passed`;
  - bytes-native scanner and parsed sentence: `157 passed`;
  - legacy and parsed assembler paths: `109 passed`;
  - UDP producer: `6 passed`;
  - UDPSEC producer and secure state: `228 passed`;
  - complete forwarding loop: `91 passed`;
  - routing and deduplication: `247 passed, 15 skipped`.
- Final full-suite result: `1164 passed, 18 skipped, 0 failed`
  (1182 collected).
- `git diff --check`: passed.
- Built-in UDP and UDPSEC producers enqueue immutable `IngressFrame`
  instances. Legacy `IngressEvent` compatibility remains through one adapter
  into the same frame-processing pipeline.
- Campaign C introduced no native implementation or bindings and defined no
  native processor API or ABI.
- This is a Campaign C closure snapshot, not a guarantee that future test
  counts will remain identical.

## 20. Campaign D closure baseline

- Closure snapshot date: 2026-07-26.
- Branch: `main`.
- Audited source commit:
  `d35de4d84233b27e8541f0cc1b5c041ad464dbc2` (`d35de4d`).
- Environment: Python 3.14.5 and pytest 9.0.3 on Windows 11
  (`Windows-11-10.0.26200-SP0`, AMD64).
- Focused results:
  - Campaign D processor, runtime-stage, and supervision coverage:
    `449 passed, 1 skipped` (450 collected);
  - Campaign A-C semantic regression coverage: `473 passed` (473 collected);
  - asyncio-debug and warnings-as-errors lifecycle coverage: `9 passed`
    (9 collected).
- Final full-suite result: `1248 passed, 18 skipped, 0 failed`
  (1266 collected).
- `git diff --check`: passed.
- The closed processor boundary consists of immutable ingress, processing
  snapshot, and processor-output values plus the synchronous
  `DataPlaneProcessor` protocol. `PythonDataPlaneProcessor` is the sole
  production processor and remains the behavioural reference implementation.
- Runtime orchestration uses explicit process-local ingress fan-in, processor,
  and ordered egress stages. A completion acknowledgement prevents processor
  work on a later frame from running ahead of the current non-empty batch's
  egress dispatch.
- UDP, UDPSEC, fan-in, processor, and egress tasks share one process-local
  fail-fast supervision lifecycle. This lifecycle supplies termination and
  cleanup, not restart, rollback, replay, or transactional delivery.
- Routing generations remain observational and do not reset processor state.
- Campaign D introduced no native implementation, native API or ABI, bindings,
  multiprocessing, coordinator/worker model, or IPC protocol.
- This is a Campaign D closure snapshot, not a guarantee that future test
  counts will remain identical.

## 21. Campaign E closure baseline

- Closure snapshot date: 2026-07-30.
- Branch: `main`.
- Audited source commit:
  `4d80cf817728833fd4385d0cf6eedd217148afc4` (`4d80cf8`).
- Environment: Python 3.14.5 and pytest 9.1.1 on Windows 11
  (`Windows-11-10.0.26200-SP0`, AMD64, 64-bit).
- Focused results:
  - target registry, forwarder, routing compilation, routing state, and
    routing control: `182 passed`;
  - routing-control protocol and transports, runtime control, runtime routing
    integration, and runtime stages: `192 passed, 16 skipped`;
  - data-plane contracts, Python processor, and output builder: `90 passed`;
  - runtime stages, supervision, routing integration, and complete forwarding:
    `138 passed`.
- Final full-suite result: `1922 passed, 18 skipped, 0 failed`
  (1940 collected).
- `git diff --check`: passed.
- Campaign E established immutable dense numeric egress target identity and
  compiled numeric target-only routing while keeping external target names
  string-facing.
- The processor-output boundary now carries exact immutable bytes, with one
  UTF-8 encoding per emitted sentence. The public ordered result is
  `OutputBatch`, and every `ProcessorOutput` carries explicit numeric target
  IDs.
- Production egress is unified through `send_to_ids()`. Frame-level
  processing, sequential output and destination dispatch, and the non-empty
  batch completion barrier remain preserved.
- Campaign E introduced no native implementation, native API or ABI, bindings,
  IPC, multiprocessing, coordinator/worker process architecture, or worker
  model.
- This is a Campaign E closure snapshot, not a guarantee that future test
  counts will remain identical.

## 22. Campaign F closure baseline

- Closure snapshot date: 2026-08-09.
- Branch: `main`.
- Audited source commit:
  `9f8b84d6304154d0570e28be75d26f64d3b83720` (`9f8b84d`,
  `feat(control): add per-target traffic accounting`).
- Environment: Python 3.14.7 and pytest 9.1.1 on Windows 11
  (`Windows-11-10.0.26200-SP0`, AMD64, 64-bit).
- Focused queue, runtime-stage, supervision, processor/reset, metrics,
  statistics, control-protocol, CLI, forwarder, UDP, and UDPSEC result:
  `877 passed, 1 skipped`.
- Final full-suite result: `2514 passed, 18 skipped, 0 failed`
  (2532 collected).
- `git diff --check`: passed.
- Campaign F establishes bounded process-local stage queues and backpressure,
  admission-time `IngressFrame` / `ProcessingSnapshot` binding,
  processor-instance state and reset ownership, ordered egress acknowledgement,
  and immutable pull-based runtime statistics with input and output traffic
  accounting.
- The statistics protocol is read-only, process-local, and non-durable. Local
  queue or dispatch completion is not a delivery guarantee.
- Campaign F introduced no coordinator, ingress or egress worker process, IPC,
  cross-process routing or metrics aggregation, automatic worker restart,
  recovery protocol, native implementation, or bindings.
- This documentation closure changes no Python or runtime behaviour and is not
  a guarantee that future test counts will remain identical.
