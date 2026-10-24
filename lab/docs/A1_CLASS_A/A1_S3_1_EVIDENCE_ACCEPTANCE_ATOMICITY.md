# A1-S3.1: station-local evidence acceptance atomicity and explicit-time handoff

Date: 2026-10-23. Host-side corrective slice for the accepted A1-S3 knowledge
layer: `StationKnowledge::accept_evidence` becomes transactional. This is not
the A1-S4 access FSM, not an integration campaign, not a new ITU reading and
not a change to the S1 codecs, the S2 gate, the maps, the directory, the
runtime, W1, YAML, CUDA or any golden. Companion documents: the A1-S3
acceptance report with its dated S3.1 addendum
([A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md](A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md)),
the A1-S4.0 causal contract whose Section 4 analysed the defect
([A1_S4_0_CAUSAL_EVENT_AND_INTERFACE_CONTRACT.md](A1_S4_0_CAUSAL_EVENT_AND_INTERFACE_CONTRACT.md))
and the A0.2 gap register entry that made S3.1 a prerequisite for S4-A
([A0_A1_INTERFACE_AND_GAP_REGISTER.md](../A0_CLASS_A/A0_A1_INTERFACE_AND_GAP_REGISTER.md)).

## 1. Baseline and scope

Starting and ending branch `lab`, HEAD `4095e9e0defc32229bd01ea05459f0d54132864d`
(`lab: record A0.2 first-frame policy and low-rate Class A gap`). The worktree
and the index were clean at the start; no Git mutation (checkout, reset,
stash, rebase, merge, cherry-pick, tag, stage, commit, push, fetch, pull)
was performed and the index is empty at the end. Scratch builds, logs, the
baseline copies and the probes live only under ignored
`lab/build/a1-s3-1-research/`, `lab/build/a1-s3-1-host-source/`,
`lab/build/a1-s3-1-host-only/`, `lab/build/a1-s3-1-release/` and
`lab/build/a1-s3-1-no-yaml/`.

Change inventory (tracked files):

| Path | Status | Change |
| --- | --- | --- |
| `lab/classa/station_knowledge.cpp` | modified | `accept_evidence` rewritten as a two-phase transaction (same checks, same allocation semantics, staged application, non-throwing commit); four `static_assert`s pin the noexcept facts the commit relies on; `<optional>` and `<type_traits>` included |
| `lab/classa/station_knowledge.h` | modified | comments only: the EVIDENCE-ACCEPTANCE-ATOMICITY-v1 contract text and the `accept_evidence` note; no declaration, enum, value or default changed; `kStationKnowledgeVersion` stays 1 |
| `lab/tests/classa_knowledge_tests.cpp` | modified | exact-state serializer `observe()`, result serializer `acceptance_text()`, new section `evidence_acceptance_atomicity` (S31-01..S31-10) registered in `main()`; `<tuple>` included; every pre-existing section and negative category untouched |
| `lab/docs/A1_CLASS_A/A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md` | modified | dated A1-S3.1 addendum appended; historical text preserved verbatim |
| `lab/docs/A1_CLASS_A/A1_S3_1_EVIDENCE_ACCEPTANCE_ATOMICITY.md` | new | this report |

No CMake, runtime, world, codec, input-gate, map or directory change was
required: the new tests live inside the existing knowledge test executable
(no new target, no new CTest name), the correction is confined to one
member function of one translation unit, and the existing public API of
`LocalSlotMap` (copy construction, `advance`, `record_garble`,
`record_external`, `advanced()`, `now_index()`) and of `PeerDirectory` (copy
construction, `expire_stale`, `would_fit`, `find`, `upsert`) is sufficient to
stage the complete operation on copies. The runtime keeps its causal phases
0..6, no `EventKind`, scheduler consumer, journal schema, FSM, timer object
or TX intent type was added, and no production evidence minter exists:
every token in the tests is a `UNIT_FIXTURE` minted by the test-only
accessor.

## 2. Defect witnesses before and after

The A1-S4.0 contract (Section 4) recorded that `accept_evidence` called
`synchronize` as its first statement, so a token rejected at a later own
UTC slot still advanced both maps, expired directory peers, pruned reuse
exclusions, bumped the knowledge revision and moved `now_index_`; a
horizon-old token at five frames even pruned the very records it then
referred to. The same test file was built twice in the host wrapper: once
against the S3.1 sources and once against the committed A1-S3
`station_knowledge.cpp` (baseline copy under the ignored research tree,
target `knowledge_before_tests`).

| Build | Result line |
| --- | --- |
| baseline A1-S3 `station_knowledge.cpp` + S3.1 test file (`knowledge-before-run-7.log`) | `A1-S3 test failure: evidence_acceptance_atomicity: rejected evidence left the exact S3 state unchanged` (first S31-01 class: a foreign-owner token delivered one frame later moved `now_index` 100 -> 2350 and bumped the revision) |
| S3.1 `station_knowledge.cpp` + S3.1 test file (`knowledge-tests-run-7.log`) | `A1-S3 synthetic local-knowledge tests PASS: 1070 assertions, 207 categorized invalid fixtures rejected; no transmission, RF reception, FCS, NMEA or UDP produced` |

Every pre-existing section passes unchanged in both builds (the baseline
build fails only in the new section), so the stronger contract is proven by
the new assertions and not by a weakened old suite.

## 3. Guarantee (EVIDENCE-ACCEPTANCE-ATOMICITY-v1, SIMULATION_POLICY)

A. A call to `accept_evidence` that throws for any reason (invalid, foreign,
stale, late, replayed, malformed, out-of-horizon or out-of-capacity
evidence, an own-input revalidation failure, an integer-overflow guard, or
an allocation failure while staging) leaves every S3-owned observable
unchanged: `now_index`, the knowledge revision, both channel maps including
their map instants and every own, external and garble record, the peer
directory with full record contents and announcement caches, the reuse
exclusions, `last_receipt_id`, the disclosure and decision frontiers, the
draw watermarks, the ledger and its counters, and the own reservations. The
receipt identity and the disclosure frontier are not consumed. The exception
is reported, never recorded as accepted evidence. The one S3 field that a
rejected call may move is the reversed-time guard (`latest_time_`): `check_own`
sets it to `now` once the owning gate has accepted the revalidation, which
mirrors the gate clock exactly as A1-S3 did through `synchronize`, so a power
notice or decision earlier than a revalidation is refused by S2 and S3 alike
(the independent review showed that leaving it behind would let a causally
reversed OFF notice pass S3 while the gate refuses it).

B. The owning `StationInputGate::revalidate` runs inside every call
(invariant 6 of A1-S3) and advances the gate's own monotonic clock before the
token is examined. That S2 side effect is intentional and is not rolled
back; S3 only mirrors it (A). S3.1 claims nothing about S2 state. All
own-input invalidity rules of A1-S3 (owner, power generation,
`qualified_at == now`, revoked or shadow tokens, OFF) are unchanged.

C. A valid token at a later own UTC slot advances both maps, expires the
directory and prunes the reuse exclusions exactly as `synchronize` would,
then records the evidence, in one commit with the same revision arithmetic
(one advance increment plus one evidence increment). Capacity, horizon and
lineage rules are judged on that post-advance, post-supersession state,
never on counts that expired at `now`.

D. An explicit `synchronize(own_now, T)` at the start of a phase-2 delivery
group or a phase-3 decision group is an intentional transition and remains
the rollback baseline for every token delivered afterwards at `T`.
`synchronize` itself is unchanged.

E. Evidence accepted at the current slot costs one evidence increment and
no advance increment. All monotonic frontiers (time, receipt, disclosure,
decision) are preserved; a corrected token with the same receipt identity
is accepted later, or at the same instant, with the semantics of a clean
station.

F. The semantics of valid inputs are those of A1-S3: `UNIT_FIXTURE`
provenance only, decoded Types 1/2/3/4 and deferred Message 20, no identity
from energy or failed-FCS garble, physical-recency supersession within a
lineage, ITDMA keep and one-shot blocks, SOTDMA timeout retention, map
aging, directory identity facts, typed `unresolved` announcements and
received-only slot knowledge; both channel maps stay owner-local and no
global or world data enters the call. The only behaviour changes for valid
inputs are the removal of three over-rejections (Section 6).

G. Strong exception safety covers the whole operation, including a failure
at the second to fifth record of a multi-record decode or garble span and a
failure of the directory update after the map records were staged.

H. No public signature, enum, value, serialization, golden, RNG purpose,
draw order or CMake test name changed; `ready_for_tx()` remains false.

## 4. Transaction mechanism and non-throwing proof

Phase 1 (validation and preparation, no S3 mutation beyond the time-guard
mirror): `check_own` (S2 revalidation, then `latest_time_ = now`), the own
slot index derived from `own_now.current_slot()`
without `synchronize`, the monotonic-slot guard, the two `offset_index`
computations that `LocalSlotMap::advance` would perform (past and future
horizon of the new index) and the map-instant monotonicity guard, then every
A1-S3 token check in its original order and wording against that index,
then the allocation construction. Only after the last token-level check of
the branch does `stage()` run: it copy-constructs the evidence channel's
`LocalSlotMap` into a `std::optional`, and, when the slot index changed,
also the other channel's map (advanced on the copy), the `PeerDirectory`
(expired on the copy with the same `earliest`/`now_index` arguments as
`synchronize`) and the reuse-exclusion vector (pruned on the copy). The
garble span is recorded on the staged map; a decoded token derives its peer
record from the staged (post-expiry) directory, records its allocations on
the staged map (the map's own horizon, lineage-recency and bounded-store
checks run there, after supersession), sorts the announcement cache, checks
the per-peer bound on the final count and upserts into the staged
directory. The complete `EvidenceAcceptance` (status string, `unresolved`
and allocation vectors) is built before the commit.

Phase 2 (`commit`, a `noexcept` lambda): move-assign the staged map(s), the
staged directory and the staged exclusions into the members, store
`now_index_`, `last_receipt_id_` and `last_disclosed_at_` and add the
revision increments. The proof that nothing in phase 2 can throw:

- `std::is_nothrow_move_assignable_v<LocalSlotMap>`,
  `std::is_nothrow_move_assignable_v<PeerDirectory>`,
  `std::is_nothrow_move_assignable_v<std::vector<ReuseExclusion>>` and
  `std::is_nothrow_move_constructible_v<EvidenceAcceptance>` are
  `static_assert`ed in `station_knowledge.cpp` and hold under MSVC 19.44 /
  C++17 (map and multimap move assignment is `noexcept` when the allocator
  is always equal and the comparator is nothrow-move-assignable; vector,
  bool, integer, enum and `std::optional<SimulationInstant>` assignments are
  nothrow). Move construction and `std::swap` are deliberately not used in
  phase 2: the MSVC tree move constructor allocates a head node and can
  throw, which the independent verification pass reproduced with a probe.
- The two map references used by the commit are bound in phase 1, so
  `map_for`/`validate_channel` never execute inside phase 2.
- Scalar stores and `std::optional<std::int64_t>`/`std::optional<SimulationInstant>`
  assignments of trivially copyable values cannot throw; the result is
  returned by (elided or nothrow) move after the commit.

Throw sites that remain, all in phase 1 and all leaving S3 unchanged:
`std::invalid_argument` from the token checks, from the staged map
(`record_garble`/`record_external`) and from the staged directory
(`would_fit`/`upsert`); `std::overflow_error` from the checked index
arithmetic; `std::bad_alloc` while copying or while growing the staged
containers, the result vectors or the status string. Residual
allocation-failure assumptions: none for atomicity (a `bad_alloc` in phase 1
is reported like any other rejection with the real state untouched; phase 2
performs no allocation). Pre-existing, unchanged and outside this slice:
the decision operations (`reserve_own`, `release_own`, `candidates`, the
draws) still call `synchronize` first, because a decision is an intentional
time transition under S3-TIE-ORDER-v1; a decision rejected after that
advance leaves the advance in place, exactly as before.

## 5. Post-expiry capacity rationale

A token delivered at `T` must be judged against the station's knowledge at
`T`. Records whose last covered slot lies below the past horizon of `T` and
peers that were last heard before that horizon without a still-reaching
announcement no longer exist at `T`; refusing a new peer or a new record
because of them would reject legal evidence that an explicit
`synchronize(T)` followed by the same delivery accepts. The staged copies are
therefore advanced and expired before any capacity check, so
`accept_evidence(E, T)` and `synchronize(T); accept_evidence(E, T)` yield
byte-identical knowledge (S31-08) and the same acceptance (S31-04, S31-05).
Supersession is handled the same way: a record that refreshes an existing
lineage in place frees that lineage's slot before the bound is tested, as
`LocalSlotMap::record_external` already specifies.

## 6. Behaviour changes for valid inputs (the three removed pre-checks)

The A1-S3 code tested three conservative inequalities before mutating,
against the present (already advanced) counts: `garble_count + span <=
max_garble`, `external_count + allocations.size() <= max_external` and
`record.reservations.size() + allocations.size() <= kMaxPeerReservations`.
S3.1 replaces them by the exact checks that the staged containers perform.
The assignment asked for exactly this: its edge-case list states that the
existing `external_count + allocations.size()` pre-check "is not a
substitute for actual post-advance, post-supersession checks", that
`record_external` may reject "due to ... bounded max_external AFTER
supersession", that garble staging must check "partial capacity and
replacement semantics", and that an implementation "checking capacity
against an expired pre-advance state is WRONG". The accepted set becomes a
strict superset of A1-S3's; no previously accepted input is rejected and no
result of a previously accepted input changes:

| Input | A1-S3 | S3.1 |
| --- | --- | --- |
| re-observation of an already garbled slot while the garble store is full | rejected (`bounded garble capacity would be exceeded`) | accepted; the record is replaced in place (S31-05) |
| decode whose allocation refreshes an existing lineage while the external store is full | rejected | accepted; the superseded record frees the slot (map rule unchanged) |
| decode whose later allocation is reported `superseded` while the peer cache is at the bound | rejected | accepted; only recorded allocations count (knowledge check on the final cache) |
| decode or garble whose store or directory would have room only after the records that expire at `T` are pruned | rejected when delivered without a preceding explicit `synchronize(T)` | accepted (S31-04, S31-05) |
| any rejected token delivered at a later slot | advanced, pruned, expired and bumped the revision | changes nothing (S31-01..S31-03) |

Diagnostics: capacity rejections of garble and external records are now
raised by the staged map (`Class A slot map: bounded garble capacity
exceeded`, `Class A slot map: bounded external allocation capacity
exceeded`) instead of the removed knowledge-level texts; the directory
capacity and per-peer texts are unchanged. No test and no production caller
inspects these strings beyond their length.

## 7. Source versus policy scope

No new ITU-R M.1371-6 reading is made or claimed. The slice implements the
A1-S4.0 contract requirement "an `EvidenceAcceptance` exception implies no
state transition" (Section 4, option (a)) with a stronger mechanism than the
one sketched there (a staged two-phase commit instead of a deferred
`synchronize`); the contract's Section 4 witness table therefore describes
HEAD `b9558b9`..`4095e9e` behaviour, superseded at this slice, and its
"one function reordering" estimate is replaced by this report. The A1-S4.0
documents themselves are not edited (A0.2 froze them as research authority).
Everything in this slice is `SIMULATION_POLICY` composition over the
unchanged A1-S3 source-derived rules; A0-ISS-003 and A0-ISS-007 stay as
recorded in A0.2.

## 8. Tests

Section `evidence_acceptance_atomicity` of `lab/tests/classa_knowledge_tests.cpp`
(`KT.evidence_acceptance_atomicity`). `observe()` renders every S3-owned
observable to canonical text (knowledge scalars; per channel `advanced()`,
`now_index()`, every own reservation, external allocation and garble record
with all fields; the directory size/capacity and every peer record with all
21 scalar fields and its announcement cache on a total key; reuse
exclusions; draw watermarks; every ledger entry with every proposal);
`acceptance_text()` renders a result. States and results are compared by
exact string equality, never by counts alone; expected values are
hand-derived from the written rules.

| ID | Case | What is asserted |
| --- | --- | --- |
| S31-01 REJECT-LATER-FRAME | peer at slot 90 established at instant 100; at instant 2350 twenty-three rejection classes (foreign vessel, foreign receiver, foreign equipment, replayed receipt, wrong and zero power generation, wrong catalog revision, future disclosure, pre-epoch interval, empty interval, disclosure before physical completion, six-slot interval, navigation status 16, longitude out of range, raw state -1 with a decoded disposition, decoded facts under an undecoded disposition, missing decoded facts, unqualified message kind, own MMSI, zero MMSI, 31-bit MMSI, channel 2, foreign qualified inputs, inputs qualified at another instant) | each throws and `observe()` is byte-identical before and after; `now_index` and both map instants stay 100; the unmutated base token is then accepted as the positive control; the comparator is shown sensitive by that acceptance and a real decision; after the decision a token disclosed at or before it is late and changes nothing further |
| S31-02 REJECT-PAST-HORIZON | timeout-0 report at slot 90 (present slot plus next-frame announcement) accepted at 100; at 5 x 2250 + 100 an otherwise plausible token received at slot 100 | rejected with no change: the lineage record and peer 2 are retained, `now_index` stays 100; the explicit `synchronize` then prunes and expires (revision +1) and a repeated rejection changes nothing again |
| S31-03 EXPLICIT-ADVANCE-THEN-REJECT | explicit `synchronize` at frame 5 (no decision), then foreign, replayed and out-of-horizon tokens | no mutation and no revision after the explicit transition; a token disclosed earlier than the rejected tokens is still accepted (neither the receipt identity nor the disclosure frontier was consumed); after a decision, evidence disclosed at that instant is late |
| S31-04 VALID-POST-EXPIRY-PEER-CAPACITY | `peer_capacity = 1`; peer 2 learned at 100; new MMSI 3 at frame 1 (peer 2 retained) and at frame 5 (peer 2 expired); on two default stations peer 2 is re-heard at frame 5, one after an explicit `synchronize` | frame 1: rejected without change; frame 5: accepted directly, old peer gone, new peer fresh (`first_receipt_id` 2, `receipts` 1), both maps advanced, revision +2; the re-heard expired peer is re-learned as a fresh record (`first_receipt_id` 2, one announcement) identically on both stations, so the record comes from the post-expiry directory |
| S31-05 VALID-POST-EXPIRY-MAP-CAPACITY | `max_external = max_garble = 2`; both stores filled at frame 0; frame 1 deliveries including a lineage refresh; frame 8 deliveries | frame 1: a new decode and a garble rejected without change, then peer 2's refresh of its own lineage accepted at capacity (the superseded record frees its slot, the directory cache holds one announcement); frame 8: two decodes and a two-slot garble accepted, old records pruned, peers 2/3 expired, other channel advanced; re-observing a garbled slot at capacity replaces the record (receipt 8) instead of being rejected |
| S31-06 PARTIAL-APPLY-ROLLBACK | `max_external = max_garble = 1`: a decode with two lineages (observed use plus a one-shot ITDMA block) where only the first fits; a staged rejection disclosed later than everything accepted, followed by a token disclosed earlier; garble spans of 2..5 slots where the second fails; a peer whose cache holds 128 announcements from 64 one-shot blocks receiving a 65th, then a refresh of two existing lineages; a station with 14 peers, a lineage record at 2330 and a reuse exclusion receiving an over-capacity garble at frame 5 | nothing of the first record, the instant (`now_index` still empty, map not advanced), the receipt or the peer remains; the one-record decode with the same receipt id then fits; the earlier-disclosed token is accepted after the later-disclosed rejection (frontier not consumed); no partial garble span; the 65th decode is rejected by the per-peer bound after both of its records were staged, leaving the 128 real records and `last_receipt_id` 64, while the refresh of two lineages is accepted at the bound and adds nothing; the frame-5 rejection leaves all 14 peers, the record at 2330, the reuse exclusion and both instants at 2500 although the staged advance had expired, pruned and dropped them on the copies |
| S31-07 CORRECTED-RETRY | malformed token at 100 rejected, corrected token with the same receipt id accepted at frame 1 and, on another station, at the same instant | acceptance text and `observe()` identical to a clean station that saw only the corrected token |
| S31-08 SUCCESS-REVISION-AND-CONTENT | six cases (Type 1 SOTDMA timeout, Type 3 keep plus increment, deferred Type 20, three-slot failed-FCS garble, energy-only garble, refreshed lineage followed by a superseded older physical report) on two stations with identical history: one calls `synchronize(T)` then `accept_evidence`, the other `accept_evidence` alone | identical acceptance text, identical `observe()` (maps, directory, exclusions, ledger, frontiers), revision `+2` on both, both map instants at T |
| S31-09 OWNER-ISOLATION | A's receipts and rejections; OFF then ON | B's `observe()` unchanged; A's other channel only advanced; OFF clears peers, records, own reservations, exclusions, watermarks, frontiers and the receipt counter while the ledger history stays; the fresh epoch rejects a generation-1 token without change and accepts receipt 1 again |
| S31-10 BOUNDARIES | evidence in the current slot (disclosed inside it), last slot of a frame accepted at the next frame's first instant, pre-epoch and future-disclosed tokens at a later frame, an ITDMA block beyond a narrowed future horizon, a wrong-channel token, and a station anchored at POSIX 1 900 000 000 receiving in slot 2249 of minute 31 666 666 at slot 0 of the next minute | one evidence increment without advance; one advance plus one evidence increment at the frame boundary with both maps at 4500 and the record announced in frame 1, reserved through frame 4, live at slots 4499 and 6749 and absent at 4500; rejections without change; the far block stays `unresolved` and never reaches the map; absolute indices follow the own anchored UTC across the minute rollover |

Counts from the final host run (`knowledge-tests-run-7.log`): 1070
assertions (A1-S3 had 847) and 207 categorized invalid fixtures (A1-S3 had
157; every one of the 46 original categories keeps its count). The 50 new
rejections are in fourteen S3.1 categories: atomic owner 5, atomic replay 2,
atomic epoch 3, atomic catalog 2, atomic time 7, atomic malformed 7, atomic
identity 3, atomic inputs 2, atomic channel 2, atomic late 3, atomic
horizon 3, atomic directory capacity 1, atomic map capacity 2, atomic
partial apply 8. No assertion inspects an exception message beyond its
length; every failing class is proven by the exact-state comparison, not by
the text. Integer-overflow rejections (`std::overflow_error` from the
checked slot arithmetic) are unreachable through the fixture anchors and
the 1..16-frame horizons, so their atomicity rests on the phase ordering
(every such guard runs before staging), not on an executed case.

## 9. Validation executed

Tool chain: VS2022 BuildTools MSVC 19.44 x64, CMake/CTest 3.31.6, CUDA 12.9
(hidden with `CUDA_VISIBLE_DEVICES=-1` for host runs), Python 3.14.7,
`/utf-8 /W4` on every target. Logs under ignored
`lab/build/a1-s3-1-research/`.

Stage A, static checks (`check_s31.py`, output recorded in Section 11):
branch and HEAD, the exact change set, empty index, `git diff --check`,
UTF-8/LF/no BOM/final newline/no trailing whitespace/no NUL on the changed
text files, SHA-256 of every other tracked file identical to the preflight
inventory, golden vector hash.

Stage B, independent CXX-only host wrapper (`lab/build/a1-s3-1-host-source/CMakeLists.txt`,
no CUDA, no yaml; the fixture macro only on the test targets), final run 7:

```
cmake --build lab/build/a1-s3-1-host-only --config Release --parallel 4     # host-build-7.log, 0 warnings
CUDA_VISIBLE_DEVICES=-1 lab/build/a1-s3-1-host-only/Release/knowledge_host_tests.exe   # knowledge-tests-run-7.log
python lab/tests/classa_knowledge_oracle.py --exe <knowledge_host_tests.exe>          # oracle-run-7.log
CUDA_VISIBLE_DEVICES=-1 ctest --test-dir lab/build/a1-s3-1-host-only -C Release       # ctest-host-only-7.log
CUDA_VISIBLE_DEVICES=-1 lab/build/a1-s3-1-host-only/Release/knowledge_before_tests.exe # knowledge-before-run-7.log (baseline sources)
```

```
A1-S3 synthetic local-knowledge tests PASS: 1070 assertions, 207 categorized invalid fixtures rejected; no transmission, RF reception, FCS, NMEA or UDP produced
A1-S3 independent oracle PASS: 461 rows, 15 families, 14 corrupted rows rejected, 2 fresh processes; 44886 bytes SHA256=28faaace53aa323bd83667e6af02200557f4b8c6c0db554f5221b2ed7530524d
100% tests passed, 0 tests failed out of 9
A1-S3 test failure: evidence_acceptance_atomicity: rejected evidence left the exact S3 state unchanged   (baseline sources, expected)
```

Runs 1-6 of the same pipeline were development iterations: run 1 exposed
two wrong expectations of mine about the in-place SOTDMA lineage refresh
(a timeout-0 announcement replaces the present-slot record instead of adding
one) and runs 2-5 fixed a non-monotonic disclosure, a pre-epoch index and a
second anchor in the fixtures while the production code stayed as in run 1;
run 6 applied the staging reorder recommended by the verification pass
(copies only after the last token-level check); run 7 applied the review
triage (time-guard mirror in `check_own`, the coverage additions of Section
8). Every run reproduced the unchanged vector hash and the baseline witness.

Stage C/D/E, main configured native Release build and CTest (same commands
as the A1-S3 report, fresh build trees `lab/build/a1-s3-1-release` and
`lab/build/a1-s3-1-no-yaml`, `main-release-pipeline.sh`):

| Stage | Tests | Result | Wall |
| --- | ---: | --- | ---: |
| D: full configured native Release CTest (CUDA visible, includes the CUDA geo/runtime tests) | 39 | 100% passed, 0 failed | 120.40 s |
| C: host-labelled CTest with `CUDA_VISIBLE_DEVICES=-1` (historical S1/S2/legacy/runtime/world host tests included) | 29 | 100% passed, 0 failed | 104.86 s |
| E: no-yaml/no-world-runner project build, knowledge-labelled CTest (C++ + Python oracle) | 2 | 100% passed, 0 failed | 0.49 s |

Configure 20 s and build 39 s on the reused `lab/build/a1-s3-1-release` tree (the first, fresh-tree run of the same pipeline on the run-6 code took 40 s and 188 s and passed the same 39/29/2 tests), whole pipeline 288 s.

Build warnings in the main tree: 0 `warning C` lines in this run; the fresh-tree run compiled the CUDA targets and produced two C4324 "structure was padded" warnings from NVIDIA cub headers inside the pre-existing CUDA bridge test, none from `lab/classa` or the knowledge tests. The knowledge test
inside the main Release build printed the same 1070/207 line; its vectors
and the historical goldens are byte-identical:

```
main Release vectors    SHA256=28faaace53aa323bd83667e6af02200557f4b8c6c0db554f5221b2ed7530524d (44886 bytes; identical to the host wrapper)
legacy demo (unchanged) SHA256=bb2838bded8b32ad4d8167109c23c1f4ee837c265c2eddea1f5c4d9f5a8f1314 bytes 583
S1 vectors (unchanged)  SHA256=5ca21cbd05980375d06f6c150c5dac00015928963aa96062cff44a38f9b94cd2 bytes 1783
S2 vectors (unchanged)  SHA256=32bc8b927825b91558012ed75590848764204cd60788848995c6d00a40068179 bytes 3343
```

The expected Windows CRLF golden `28faaace53aa323bd83667e6af02200557f4b8c6c0db554f5221b2ed7530524d`
(44 886 bytes) is reproduced by the wrapper build and the main build in
fresh processes; the oracle still validates its 461 rows, 15 families and
14 corruption rejections. The `--demo` output and the S1/S2 vectors are
unchanged.

Independent review passes (multi-agent, read-only, logs under
`lab/build/a1-s3-1-research/agents/`): a design-verification pass
(exception sites, noexcept facts, observable inventory, semantic drift)
returned SOUND_WITH_CHANGES and its required documentation items are in
this report and the S3 addendum; its recommended disclosure-frontier test
and the copy-free rejection ordering were adopted in run 6. An adversarial
five-dimension review with three refuters per finding: four of its five finders completed (the edge-case finder and all 69 refuter agents were cut off by the session limit, so no finding was machine-refuted or machine-confirmed) and its 23 raw findings were triaged by hand against the code. None is a correctness failure of the shipped code; the semantics finder explicitly found no result, state, revision or frontier drift for valid tokens and confirmed the `--vectors` emitter unaffected. Six were adopted in run 7: the reversed-time guard now mirrors the gate clock inside `check_own`, so a rejected delivery can no longer let a causally reversed power notice pass S3 while the gate refuses it (the finders' only semantic divergence from A1-S3); a disclosure-frontier probe after a staged rejection; the expired-peer re-learning case against an explicit-synchronize twin; `now accepted` guards for a lineage refresh at external capacity and at the per-peer bound; an advancing delivery rejected after staging whose staged advance expired peers, pruned a record and dropped a reuse exclusion only on the copies; the frame-boundary record fields; plus eleven further rejection classes with a positive control in S31-01 and the removal of three tautological address assertions. Eight are documented in this report (capacity diagnostics now raised by the map, the relaxed over-rejections and the brief clauses that authorize them, the linear copy cost including the directory copy per decoded token, overflow being unreachable through the fixture anchors, the A1-S4.0 Section 4 text being superseded without editing that frozen document, gate-first power-notice dispatch). The rest were informational or already satisfied by the documents written after the review ran.

## 10. Cost of the staged transaction

Measured with the ignored probe `bench_accept.cpp` (same program linked
against the S3.1 and the baseline library, Release, one core, 200
deliveries per point; `bench-after-3.log` for the final code,
`bench-before.log` for the baseline): a station whose maps and directory
hold N peers (one lineage record per peer, alternating channels, one
announcement per peer) receiving decoded Type 1 tokens at an unchanged own
slot. Single runs, not averaged; variation between runs of the same code
was within about 25 percent at 1 000 peers.

| retained peers | S3.1 same-slot delivery | A1-S3 same-slot delivery |
| ---: | ---: | ---: |
| 10 | 11.9 us | 3.4 us |
| 50 | 16.1 us | 2.9 us |
| 200 | 48.9 us | 3.2 us |
| 1000 | 288 us | 3.4 us |

The S3.1 cost is linear in the station's retained records of every kind
(external and garble records of the evidence channel, peer records with
their announcement caches): roughly 12 us plus 0.2 to 0.3 us per retained
peer per delivery in this scene, because every decoded delivery copies the
evidence channel's map and the whole directory even when only one peer
record changes (a design choice made for the strong guarantee; a garble
delivery copies only the map; a delivery that advances the slot
additionally copies the other map and the exclusions, which under the S4
explicit `synchronize`-per-group discipline never happens inside
`accept_evidence`). Transient memory is the size of the copied containers
(about 300 KB at 1 000 peers). Bounds at the default limits (9 000 external
and 4 500 garble records per map, 1 024 peers) are about 15 600 node
allocations per decoded delivery, i.e. low milliseconds. For the planned
terrestrial pilot scale (tens of vessels, Table 1 rates) the cost is well
below one percent of a core per station-hour; for a 1 000-peer scene it is
about 6 percent of a core per receiving station at 200 deliveries per
second and would need the cheaper mechanism below before a five-hour run at
that scale. Rejected tokens pay no copy (staging follows the last
token-level check).

Cheaper alternatives were considered and rejected for this slice: a deferred
`synchronize` placed before the first record (the A1-S4.0 sketch) and a
copy-only-on-advance variant are zero-copy for valid tokens but not atomic
under an allocation failure inside `record_external`, `record_garble` or
`upsert` (a torn `PeerRecord` copy), and an exact copy-free preflight would
duplicate the map's lineage and capacity logic in the knowledge layer. The
provable O(records touched) design is a batch API inside `LocalSlotMap` and
`PeerDirectory` with node-handle rollback; it changes files the brief asked
to leave alone and is recorded as the optimization path should scale demand
it.

## 11. Static-check record

Command (repository root): `python lab/build/a1-s3-1-research/check_s31.py`. Recorded output:

```text
PASS HEAD unchanged (4095e9e)
PASS branch lab
PASS git status is exactly the S3.1 change set ( M lab/classa/station_knowledge.cpp;  M lab/classa/station_knowledge.h;  M lab/docs/A1_CLASS_A/A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md;  M lab/tests/classa_knowledge_tests.cpp; ?? lab/docs/A1_CLASS_A/A1_S3_1_EVIDENCE_ACCEPTANCE_ATOMICITY.md)
PASS index empty
PASS git diff --check clean
PASS lab/classa/station_knowledge.cpp: no BOM
PASS lab/classa/station_knowledge.cpp: LF only
PASS lab/classa/station_knowledge.cpp: final newline
PASS lab/classa/station_knowledge.cpp: no NUL
PASS lab/classa/station_knowledge.cpp: UTF-8
PASS lab/classa/station_knowledge.cpp: no trailing whitespace (lines [])
PASS lab/classa/station_knowledge.h: no BOM
PASS lab/classa/station_knowledge.h: LF only
PASS lab/classa/station_knowledge.h: final newline
PASS lab/classa/station_knowledge.h: no NUL
PASS lab/classa/station_knowledge.h: UTF-8
PASS lab/classa/station_knowledge.h: no trailing whitespace (lines [])
PASS lab/tests/classa_knowledge_tests.cpp: no BOM
PASS lab/tests/classa_knowledge_tests.cpp: LF only
PASS lab/tests/classa_knowledge_tests.cpp: final newline
PASS lab/tests/classa_knowledge_tests.cpp: no NUL
PASS lab/tests/classa_knowledge_tests.cpp: UTF-8
PASS lab/tests/classa_knowledge_tests.cpp: no trailing whitespace (lines [])
PASS lab/docs/A1_CLASS_A/A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md: no BOM
PASS lab/docs/A1_CLASS_A/A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md: LF only
PASS lab/docs/A1_CLASS_A/A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md: final newline
PASS lab/docs/A1_CLASS_A/A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md: no NUL
PASS lab/docs/A1_CLASS_A/A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md: UTF-8
PASS lab/docs/A1_CLASS_A/A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md: no trailing whitespace (lines [])
PASS lab/docs/A1_CLASS_A/A1_S3_1_EVIDENCE_ACCEPTANCE_ATOMICITY.md: no BOM
PASS lab/docs/A1_CLASS_A/A1_S3_1_EVIDENCE_ACCEPTANCE_ATOMICITY.md: LF only
PASS lab/docs/A1_CLASS_A/A1_S3_1_EVIDENCE_ACCEPTANCE_ATOMICITY.md: final newline
PASS lab/docs/A1_CLASS_A/A1_S3_1_EVIDENCE_ACCEPTANCE_ATOMICITY.md: no NUL
PASS lab/docs/A1_CLASS_A/A1_S3_1_EVIDENCE_ACCEPTANCE_ATOMICITY.md: UTF-8
PASS lab/docs/A1_CLASS_A/A1_S3_1_EVIDENCE_ACCEPTANCE_ATOMICITY.md: no trailing whitespace (lines [])
PASS tracked file set unchanged (150 files; nothing added to the index)
PASS all 146 other tracked files byte-identical to the preflight inventory []
PASS lab/classa/station_knowledge.cpp: actually modified
PASS lab/classa/station_knowledge.h: actually modified
PASS lab/tests/classa_knowledge_tests.cpp: actually modified
PASS lab/docs/A1_CLASS_A/A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md: actually modified
PASS no unfilled placeholder in the reports (the check-output block may still be pending)
PASS S3 report keeps/has: A1-S3 synthetic local-knowledge tests PASS: 847 assertions,
PASS S3 report keeps/has: ## A1-S3.1 addendum - evidence acceptance atomicity (2026-10
PASS S3 report keeps/has: Negative categories (46, 157 rejections)
PASS S3 report keeps/has: HEAD `46601d521ea387765549172b8c307b88654a4fe5`
PASS S3.1 report states: 1070 assertions, 207 categorized invalid fixtures rejected
PASS S3.1 report states: EVIDENCE-ACCEPTANCE-ATOMICITY-v1
PASS S3.1 report states: S31-10
PASS S3.1 report states: knowledge_before_tests
PASS S3.1 report states: 4095e9e0defc32229bd01ea05459f0d54132864d
PASS relative links in both reports resolve []
PASS header keeps version 1 and documents the atomicity contract
PASS source has the four noexcept assertions and the noexcept commit
PASS accept_evidence no longer calls synchronize
PASS host wrapper golden vectors unchanged (vectors-run-7.txt)
PASS main Release golden vectors unchanged

RESULT: ALL CHECKS PASS
```

The checker was re-run after this output was embedded (the report is part of the checked change set); the re-run produced the same PASS lines and result.

## 12. Git state, decisions and residual limitations

Branch `lab`, HEAD `4095e9e0defc32229bd01ea05459f0d54132864d` (unchanged), index empty (`git diff --cached --name-only` printed nothing), nothing staged, committed, tagged, pushed, fetched, merged or reset. `git status --short --untracked-files=all`:

```
 M lab/classa/station_knowledge.cpp
 M lab/classa/station_knowledge.h
 M lab/docs/A1_CLASS_A/A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md
 M lab/tests/classa_knowledge_tests.cpp
?? lab/docs/A1_CLASS_A/A1_S3_1_EVIDENCE_ACCEPTANCE_ATOMICITY.md
```

Suggested **operator-only** commit subject (not executed): `lab: make A1-S3 evidence acceptance atomic before S4`.

Decisions:

- D1-S3.1: **PASS** for the scope of this slice (gates G0-G5 of the
  assignment met as recorded above): rejected evidence never mutates S3
  state, including late-frame, horizon-old and partial-apply failures; the
  S2 gate clock is documented, not promised rolled back; explicit
  `synchronize` still advances and prunes; valid evidence at a later slot
  yields the same result and revision as the synchronized sequence;
  post-expiry capacity cases pass; the full existing suite, the oracle and
  every golden pass unchanged; no input authority widening, no global
  knowledge, no scheduler/FSM/transmission/RF/UDP or RNG drift.
- A1-S4-A isolated intent-only engineering may now proceed to its own
  review under A0.2 (CONDITIONAL GO TO IMPLEMENT there); nothing of S4-A is
  implemented or claimed here.
- Full A1-S4 acceptance, RF1, S5, I1, P1, NMEA/UDP and the five-hour
  RF-closed-loop pilot (D2): **NO-GO**, unchanged.

Residual limitations and open gates: A0-ISS-003 remains source-open with
the bounded CLASSA-FIRST-FRAME-v1 policy adopted in A0.2; A0-ISS-007 keeps
Rr < 2 blocked; S1-NOGO-IMO-ZERO and S1-NOGO-UTC-UNAVAILABLE remain in
force; S2-GAP-SIDECAR remains open; the decision operations still perform
their intentional advance before validating the decision (unchanged A1-S3
behaviour); the reversed-time guard mirrors the gate clock and therefore
moves on every accepted revalidation, including one that precedes a
rejected token (Section 3); power notices must keep the gate-first dispatch
order of the A1-S4.0 contract; the transaction cost is linear in the
retained records (Section 10); no production evidence minter exists, so
every observation is a `UNIT_FIXTURE`.
