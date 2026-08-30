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
handshake timestamp check, ping, pong, and graceful-close timestamps, and
timestamped debug output.
Handshake freshness remains inclusive at the boundary:
`abs(wall_now - transmitted_timestamp) <= 30`. Monotonic time owns handshake
replay TTL, pending-session creation and TTL, active-session creation and
last-seen times, active-session TTL, and local capacity ordering. Each allowed
received packet uses one monotonic observation for all of that packet's
local-state decisions. Network policy is applied first; a denied packet
performs no cryptographic work, state mutation, cleanup, or secure-state clock
read.

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
server-side pending state. Pending expiry, same-address replacement, capacity
eviction, or nonce exhaustion removes only that exact pending entry. Each such
removal makes the candidate traffic-key epoch unusable before its nonce state
is discarded and does not itself alter an active session at that address.

When multiple listeners share one `SecureState`, each listener retains the
exact pending object created through its socket. Only that listener may confirm
and promote that still-current object. A missing, stale, or replaced
listener-local handle cannot consume the candidate nonce or transfer promotion
ownership across listener sockets.

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
identity at its address. A replaced, capacity-evicted, promoted, expired,
nonce-exhausted, or otherwise stale handle cannot mutate state or trigger
unrelated cleanup.

UDPSEC has no plaintext session-reset or other unauthenticated session-control
packet. A DATA packet received for an address with no live pending or active
session is dropped without a wire response. Plaintext, malformed, unknown, or
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

On `nmea_sproxy`, only a matching authenticated encrypted pong from the pinned
remote tuple advances peer liveness. The pong must decrypt under the current
server-to-client owner, match the configured station identity, and carry the
exact sequence of the one outstanding ping. No pong is accepted when no ping
is outstanding. An accepted pong clears that expectation and advances liveness.
After it is cleared, a duplicate replay has no outstanding sequence to match;
the sequence is not reused later in that session. Stale or other-sequence pongs
therefore cannot refresh liveness, and ciphertext from an earlier session
cannot authenticate under the fresh directional keys. Plaintext, wrong-key,
wrong-address, wrong-source, malformed, and wrong-sequence packets provide no
liveness evidence.

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
deadlines coincide, deterministic priority is `peer_timeout`, then planned
session refresh, then the keepalive action: proactive rekey for an unresolved
ping or a new ping when none is outstanding. A due deadline is resolved before
poll-ready packets, so a matching pong must be fully authenticated and accepted
before its boundary to refresh liveness or prevent proactive rekey. Deadline
checks use fresh monotonic observations. After any pending local-input
forwarding, the final poll timeout is recomputed from another fresh monotonic
observation immediately before `select()`.

Timing values must be finite integer or float values, excluding booleans and
numeric strings. UDPSEC requires `keepalive_interval > 0`,
`peer_timeout > 0`, `session_refresh_interval >= 0`, and
`reconnect_delay >= 0`; a zero session refresh interval disables planned
refresh. These constraints are independent: there is no cross-field ordering
or ratio requirement. Plain UDP shares only the `reconnect_delay >= 0`
validation; the other three fields are UDPSEC-only.

Proactive recovery and configured planned refresh both reuse the normal signed
ClientHello, authenticated ServerHello, directional ECDHE-derived traffic keys,
and encrypted sequence-zero confirmation. They add no reset, probe, or separate
recovery protocol. Every attempt generates fresh ephemeral ECDHE material, so
each confirmed replacement has fresh directional traffic keys. The fresh
ClientHello installs or replaces only pending state; the old active server
session remains usable while confirmation is pending, failed confirmation does
not destroy it, and successful confirmation atomically promotes the candidate
and replaces the old active state as specified below. Planned refresh and the
first proactive-rekey attempt are immediate; peer graceful close,
`peer_timeout`, handshake failure, local forwarding failure, and socket failure
use `reconnect_delay`. No NMEA payload is buffered or replayed as part of any
recovery path.

A pending session is promoted only when a DATA packet decrypts under its
client-to-server AES-GCM owner and decodes to a confirmation ping. Confirmation
requires type `"ping"`, reserved sequence `0` as a built-in integer and not a
boolean, a built-in-integer timestamp, and a source identity equal to the
pending station ID. The packet nonce is admitted to the pending session before
promotion. Promotion removes the pending entry and, as one state-model
transition, replaces any live active session at the same address.
The station identity, both directional AES-GCM owners, and the pending nonce
set become the new active state; active creation and last-seen time begin at
promotion. The transferred confirmation nonce remains retained and counts
against `DATA_NONCE_MAX_PER_SESSION` for the promoted traffic-key epoch. The
server then returns an encrypted sequence-zero pong using the promoted
server-to-client owner; the proxy requires the same sequence and timestamp
rules before accepting that confirmation pong.

For promotion at a new address, expired active sessions are removed before
active capacity is considered; if capacity remains full, the
least-recently-seen live active session is evicted. Equal active timestamps are
resolved by deterministic activity order. At most `SESSION_MAX` active
sessions are retained. Every active-session removal, including replacement,
expiry, session-capacity eviction, graceful close, or nonce exhaustion, makes
that exact traffic-key epoch unusable before discarding its nonce state.

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

A client close is encrypted under the current client-to-server AES-GCM owner.
The server accepts it only at the exact active peer address and through the
listener that retained that exact live session object. Source identity and the
complete canonical message shape must match. Decryption, canonical validation,
and authoritative active data-nonce admission occur before close-specific state
changes. Only `ACCEPTED` processes the packet as a graceful close; `EXHAUSTED`
follows the fail-closed epoch-invalidating path above and is not counted as a
normal close. Pending state at the same address and sessions owned by other
listeners remain unchanged. Forged, plaintext, wrong-key, replayed,
stale-handle, or cross-listener close attempts cannot remove a session.

A server close is encrypted under the current server-to-client AES-GCM owner
and sent only through the listener socket that owns the exact retained live
session. The proxy considers it only from its pinned remote tuple and leaves the
current session only after successful decryption and canonical source and shape
validation. A validated peer close selects normal `reconnect_delay`, not an
immediate re-handshake. It does not require or carry a ping sequence.

Normal endpoint shutdown sends at most one such close per live relation before
its UDP socket is closed. This includes proxy SIGINT or SIGTERM and AISMixer
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
reports replay, pending-session, active-session, and data-nonce lifecycle
counts, including `sessions_closed` for normal active-session removal, plus
current and peak sizes. `data_nonce_exhaustions` counts fail-closed removal of
an exact active or pending traffic-key epoch and is not counted as normal close,
expiry, replacement, or active- or pending-session capacity eviction. The
legacy `data_nonces_expired` and `data_nonces_capacity_evicted` fields remain in
the snapshot for compatibility but stay zero; accepted data nonces no longer
expire or undergo live-entry eviction. Retained records discarded when their
owner epoch ends contribute to `data_nonces_session_discarded`. Every removed
record has exactly one removal reason. Reading statistics invokes neither
clock, performs no cleanup, exposes no mutable state, and does not change an
earlier snapshot.

This section governs only process-local secure state and the encrypted graceful
close described above. It does not otherwise redefine secure packet formats,
cryptographic algorithms, the signed handshake transcript, session-key
derivation, or `nmea_sproxy` protocol compatibility.

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
a full stage queue waits and applies backpressure; these AISMixer queues do not
implement a drop-on-full branch. That waiting supplies no network-delivery,
durability, replay, or recovery guarantee. UDP can lose data outside these
queues, and fail-fast shutdown does not replay queued work.

Private ingress queues keep one input's queued backlog from consuming another
input's private queue capacity. Each fan-in reader may nevertheless dequeue
and hold one supported frame while waiting for shared processing capacity, and
the held frame is not included in private queue depth. Reader scheduling and
shared admission define no fairness or total arrival-order guarantee between
inputs. This contract is separate from the bounded serial-input queue inside
`nmea_sproxy`, whose overflow policy is not an AISMixer runtime-stage policy.

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

Every UDP and UDPSEC producer, fan-in, processor-stage, and egress-stage task is
an essential task in one process-local fail-fast supervision lifecycle; fan-in
similarly owns its private readers. Failure, unexpected return, or unexpected
cancellation terminates siblings and retrieves their outcomes. This is
supervision of asyncio tasks in one service process. It supplies cleanup and
termination, not a coordinator process, ingress or egress worker processes,
IPC, cross-process routing-snapshot distribution, automatic worker restart,
recovery, or replay.

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
| `runtime.statistics.inputs` | `params` may be absent, empty, or exactly `{ "input": <non-empty string> }`. One detailed input-traffic pull returns `inputs` in runtime declaration order, optionally filtered by exact input name; no match returns an empty list. |
| `runtime.statistics.outputs` | `params` may be absent, empty, or contain exactly one of non-negative integer `target_id` or non-empty string `name`. One detailed output-traffic pull returns `outputs` in numeric target order, optionally filtered by that exact value; no match returns an empty list. |

These methods only read the injected statistics provider and cannot replace,
disable, or otherwise mutate routing or data-plane state. Conversely, routing
status and mutation methods do not pull statistics.

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
