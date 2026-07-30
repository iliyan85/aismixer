# aismixer Behavioural Contract

## 1. Scope

This document defines the currently tested Python processing contract for:

- ingress frame production and compatibility-event acceptance;
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

## 2. Ingress frame and compatibility-event boundary

The built-in UDP and UDPSEC producers enqueue immutable `IngressFrame`
instances. Ingress fan-in transports queue items unchanged and performs no
conversion, validation, routing, or parsing. The processor stage accepts a
direct frame by object identity and retains compatibility for `IngressEvent`
through one adapter. A compatibility event's `raw_line` must satisfy
`isinstance(raw_line, str)`, including subclasses; its explicit legacy-text
mode preserves surrogate code points.

After coercion, direct frames and adapted compatibility events enter one common
frame-processing pipeline. There is no parallel legacy routing, scanning,
parsing, assembly, metadata, deduplication, or forwarding path.

An invalid compatibility event or any unsupported queue-item type is ignored
before routing, extraction, assembly, or deduplication, and later queued items
must continue to be processed. In particular, a bare `bytes` or `str` queue
item is not implicitly converted; bytes must already be owned by an
`IngressFrame`.

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

Wall time and monotonic time have separate ownership. Wall time is used only
for externally meaningful protocol or diagnostic timestamps: the transmitted
handshake timestamp check, pong timestamps, and timestamped debug output.
Handshake freshness remains inclusive at the boundary:
`abs(wall_now - transmitted_timestamp) <= 30`. Monotonic time owns handshake
replay TTL, pending-session creation and TTL, active-session creation and
last-seen times, active-session TTL, data-nonce TTL, and local capacity
ordering. Each allowed received packet uses one monotonic observation for all
of that packet's local-state decisions. Network policy is applied first; a
denied packet performs no cryptographic work, state mutation, cleanup, or
secure-state clock read.

Every process-local TTL uses the same exact boundary: state is live while
`age < ttl` and expires when `age >= ttl`. A duplicate handshake replay key or
data nonce does not refresh its expiry. Wall-clock changes do not expire,
revive, or extend replay, pending-session, active-session, or nonce state.

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

A pending session is identified by the exact peer socket address and retains
the authenticated station ID, separate client-to-server and server-to-client
AES-GCM owners, its monotonic creation time, and a private data-nonce set.
Pending sessions have their own TTL, capacity, and creation order, independent
of active-session TTL, capacity, and activity order. Pending lifetime is not
refreshed by traffic. Expiry removes only the expired ordered front prefix. A
new pending address is installed at the newest end; when capacity remains
full, the oldest live pending entry is evicted deterministically. A newer live
pending session at the same address replaces only the older pending entry and
occupies the newest position. Equal creation times follow deterministic
installation order. At most `PENDING_SESSION_MAX` pending sessions are retained.

Installing or replacing a pending session does not remove, replace, or touch
an active session at the same address. While the candidate remains pending,
confirmation failure or client timeout leaves any existing active session
intact; it does not promote the pending entry and does not itself delete
server-side pending state. Pending expiry, same-address replacement, or capacity
eviction discards only that pending entry and its nonce state and does not itself
alter an active session at that address.

An active session is identified by the exact peer socket address and retains
its authenticated station ID, separate client-to-server and server-to-client
AES-GCM owners, monotonic creation and last-seen times, and a private
data-nonce set. Active sessions are ordered from least to most recently seen.
Installation and valid activity place a session at the most-recent end. Only
promotion or a fully validated active secure NMEA or ping packet counts as
activity; invalid, malformed, mismatched, expired, or replayed traffic does not
touch the active session.

After network policy accepts any packet, including a handshake or unknown
packet type, the expired ordered prefixes of both pending and active session
stores are removed before packet-type-specific handling. Expired state may
therefore remain physically present until later allowed traffic, but an
expired directly addressed session is never treated as live. State operations
receiving an active or pending handle first require exact retained-object
identity at its address. A replaced, capacity-evicted, promoted, expired, or
otherwise stale handle cannot mutate state or trigger unrelated cleanup.

A pending session is promoted only when a DATA packet decrypts under its
client-to-server AES-GCM owner and decodes to a confirmation ping. Confirmation
requires type `"ping"`, reserved sequence `0` as a built-in integer and not a
boolean, an integer timestamp that is not a boolean, and a source identity equal
to the pending station ID. The packet nonce is admitted to the pending session
before promotion. Promotion removes the pending entry and, as one state-model
transition, replaces any live active session at the same address.
The station identity, both directional AES-GCM owners, and the pending nonce
set become the new active state; active creation and last-seen time begin at
promotion. The server then returns an encrypted sequence-zero pong using the
promoted server-to-client owner. Ordinary active-session ping sequences must
be exact built-in integers strictly greater than zero.

For promotion at a new address, expired active sessions are removed before
active capacity is considered; if capacity remains full, the
least-recently-seen live active session is evicted. Equal active timestamps are
resolved by deterministic activity order. At most `SESSION_MAX` active
sessions are retained. Replacing, expiring, or capacity-evicting an active
session discards its nonce state.

Secure-data nonce identity is the exact 12-byte nonce within its owning pending
or active session. Identical bytes in different session states are independent.
A live nonce replay for an owner is rejected before decryption under that owner
and does not refresh nonce expiry. A new pending nonce is retained only after
decryption and complete confirmation validation; a new active nonce is retained
only after decryption, JSON decoding, source matching, and message-type and
required-field validation. Admission occurs before promotion, session touch,
pong generation, or NMEA action. Each nonce set retains at most
`DATA_NONCE_MAX_PER_SESSION` records, expires only its ordered front prefix,
and evicts the oldest live nonce deterministically when capacity remains full.
Promotion transfers the pending nonce set without discarding it; other removal
of its owning session discards it.

An NMEA message that contains the required `payload` key but whose value is not
a string retains that accepted nonce and touches its active session before
frame construction is attempted. It produces no frame or queue item and is not
promoted to a protocol exception; later packets continue to be processed.

`stats()` returns an immutable point-in-time `SecureStateStats` snapshot. It
reports replay, pending-session, active-session, and data-nonce lifecycle
counts, plus current and peak sizes. Every removed record has exactly one
removal reason. Reading statistics invokes neither clock, performs no cleanup,
exposes no mutable state, and does not change an earlier snapshot.

This section governs only process-local secure state. It does not redefine
secure packet formats, cryptographic algorithms, the signed handshake
transcript, session-key derivation, or `nmea_sproxy` protocol compatibility.

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

When routing state is present, the processor stage acquires exactly one
immutable routing snapshot per accepted or successfully coerced frame. If that
snapshot contains a table, orchestration calls the numeric target-only matcher
exactly once with `frame.source_id`. Unsupported queue items and invalid
compatibility events acquire no snapshot and perform no match. All accepted
sentences extracted from one frame use the same resolved numeric tuple. A
routing-table replacement during processing affects the next accepted frame,
not the frame already in progress.

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
table selects `PER_TARGET` mode and passes the one resolved tuple. Therefore
`GLOBAL + ()` means legacy mode with no configured destinations, while
`PER_TARGET + ()` means routing is enabled but the source matched no target.
Snapshot construction preserves target order and rejects duplicate numeric IDs
rather than silently normalizing them; production numeric matching already
returns a unique first-occurrence tuple. The routed empty-target case performs
no global deduplication admission and emits no output while retaining normal
assembler and multipart metadata cleanup.

## 13. Campaign D processor/egress boundary

`PythonDataPlaneProcessor.process(frame, snapshot)` completes synchronous
processing of the entire accepted frame and constructs the complete returned
tuple of `ProcessorOutput` values before orchestration begins its first
asynchronous egress send. The processor stage resolves the frame's target-only
snapshot before this call. Parsing, assembly, multipart metadata observation
and cleanup, deduplication decisions, TAG formatting, wall-clock observations
used for formatting, GID generation, and `touch_s` effects belonging to that
frame therefore all occur before the first send begins.

The single egress stage dispatches the returned outputs sequentially in tuple
order. Legacy-broadcast outputs retain an empty target tuple and call
`Forwarder.send()`. Targeted outputs contain ordered numeric IDs and call
`Forwarder.send_to_ids()`; production processing never uses the string
`send_to()` compatibility API. A send failure stops dispatch before any later
output is sent, but it does not undo processor state, deduplication state,
multipart metadata cleanup, wall-clock observations, GID generation, `touch_s`
effects, or already constructed later outputs. The runtime-only completion
signal described below is an ordering barrier, not an acknowledgement to an
ingress source or a network-delivery guarantee. The boundary provides no
transactional delivery, rollback, replay, ingress acknowledgement, delivery
acknowledgement, or recovery guarantee, including after a partial
multi-fragment send.

`ProcessorOutput.message` is an exact immutable `bytes` payload containing one
completely formatted output sentence, normally terminated by CRLF. The
processor-output boundary accepts an existing `bytes` object without copying
it and rejects `str`, mutable buffers, views, and other payload types. It does
not decode or encode the payload and does not require CRLF at this general
immutable boundary.

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
sent to the forwarder, so debug output cannot alter or block network dispatch.

Legacy broadcast and numeric targeted egress remain separate branches and
continue to use `Forwarder.send()` and `Forwarder.send_to_ids()`,
respectively. `ProcessorOutput`, `RoutingDisposition`, the returned processor
tuple, and the private runtime `_EgressBatch` envelope remain in place.
`OutputBatch` and a unified egress branch are deferred to Campaign E4. This
bytes-boundary change introduces no native API or ABI, bindings, IPC,
multiprocessing, worker pool, or egress concurrency.

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

The stages run in one process. Ingress fan-in preserves the established
ordering into one processor-stage queue. Exactly one long-lived processor-stage
consumer uses the runtime-owned, long-lived `PythonDataPlaneProcessor`, and
exactly one long-lived egress-stage consumer dispatches its results. The egress
stage performs no routing matching, parsing, assembly, multipart metadata work,
deduplication, TAG construction, GID generation, or processor-state mutation.

For each accepted frame, the processor stage coerces the queue item once,
acquires exactly one routing snapshot, resolves exactly one target-only
`ProcessingSnapshot`, calls the configured `DataPlaneProcessor` exactly once,
and treats the complete returned `tuple[ProcessorOutput, ...]` as that frame's
one ordered processor batch. Unsupported queue items and invalid compatibility
events are rejected before snapshot acquisition, target matching or processor
invocation. An empty output batch may complete locally because it has no
egress work.

After handing a non-empty batch to egress, the processor stage must await an
explicit process-local completion acknowledgement. It must not consume or
process the next ingress item until egress has dispatched the current batch's
final output and acknowledged success. Removing a batch from an inter-stage
queue does not satisfy this barrier. Thus processor work cannot run ahead
across frames while prior egress is incomplete. After the barrier completes
successfully, the next accepted frame acquires its own snapshot; a routing
replacement while the prior batch is blocked can affect that next frame, but
routing generation remains observational and cannot reset processor state.

If a processor call fails, no batch is handed to egress and the exception
propagates through runtime lifecycle management. If egress fails, it signals
that failure through the completion barrier, stops the current batch before
later sends, and propagates the exception through runtime lifecycle management.
The already completed processor effects retain the non-rollback semantics
above, and no later accepted frame is processed after the failure. Runtime
shutdown or cancellation must resolve or cancel pending stage work and
acknowledgements so that no stage remains blocked or orphaned.

The inter-stage queues, batch envelope, and completion acknowledgement are
private runtime-orchestration mechanisms. They do not alter
`DataPlaneProcessor`, `ProcessingSnapshot`, or `ProcessorOutput`, and they
define neither a native API or ABI nor an IPC protocol. Campaign D3 introduces
no multiprocessing, threads, worker pool, or second processor implementation.

### Runtime lifecycle supervision

Every essential long-lived runtime task—each UDP and UDPSEC ingress producer,
ingress fan-in, the processor stage, and the egress stage—is owned by one
process-local supervision lifecycle. The fan-in in turn owns its private reader
tasks. Failure, cancellation, or unexpected normal return by any essential task
terminates the runtime: every still-running sibling is cancelled, and all owned
task outcomes are awaited and retrieved before the primary failure, or a clear
unexpected-termination error, propagates. Exceptions are not left detached
from the runtime lifecycle.

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

## 14. Explicit limitations and deferred decisions

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

## 18. Campaign C closure baseline

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

## 19. Campaign D closure baseline

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
