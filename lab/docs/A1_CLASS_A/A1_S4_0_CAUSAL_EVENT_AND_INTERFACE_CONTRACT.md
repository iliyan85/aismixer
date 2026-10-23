# A1-S4.0: causal event and interface contract for the A1-S4 access engine

Date: 2026-10-23. Documentation only; baseline `lab` HEAD
`b9558b97eb674d1b7aea37615b9d582d81f99337`. Everything marked DESIGN ONLY
is a proposal for the A1-S4 slices; everything marked IMPLEMENTED cites a
tracked file and symbol that exists at this HEAD. Companion documents:
[first-frame adjudication](A1_S4_0_FIRST_FRAME_NORMATIVE_ADJUDICATION.md),
[review and gate](A1_S4_0_REVIEW_AND_GATE.md).

## 1. Current API inventory (IMPLEMENTED at this HEAD)

| Area | File | Symbols (signatures abbreviated to parameters that matter here) |
| --- | --- | --- |
| Exact time | `lab/runtime/simulation_time.h` | `kUnitsPerSecond = 9830400`, `kUnitsPerSlot = 262144`, `kUnitsPerFrame = 589824000`, `kSlotsPerFrame = 2250`; `struct SimulationInstant { int64 units }`; `UtcAnchor`, `UtcSlot`; `add_units`, `difference_units`, `decompose_utc(instant, anchor)`, `compose_utc(slot, anchor)` |
| Events | `lab/runtime/event.h` | `EventKind` (journal v1 codes 1..19); `causal_phase(kind)`; `CanonicalLess`; `Event { schema_version, time, kind, source_id, subject_id, event_id, causal_ref, payload }`; payloads `StationLifecycle`, `SlotMapChange { station_id, channel, slot, reason }`, `TdmaDecision { station_id, decision }`, `TransmissionStart { transmission_id, station_id, channel, end_time, payload_reference, coverage }`, `TransmissionEnd`, `RfObservation`, `ReceptionResult`, `RfNoReception`, `TransmissionResolved`; `EventValidator` |
| Scheduler | `lab/runtime/scheduler.h/.cpp` | `Scheduler(pending_capacity, initial_watermark, profile)`; `enqueue(Event)`; `attach_producer_barrier`; `advance_watermark(target, Consumer)` and `advance_watermark_batches(target, PhaseConsumer)` (half-open: times < watermark final); `finish`; `watermark()`, `next_pending_time()`, `has_dispatched(EventId)`, `accepted_event(EventId)`; `CausalProducerBarrier { validate_advance, validate_finish, committed }` |
| RF boundary (R0.1, no RF model) | `lab/runtime/rf_contract.h` | `RfTransmissionWork`, `RfWorkSubmission`, `CompletedRfObservation`, `ReceiverRegistry`, `RfCausalBoundary { attach, register_transmission, record_transmission_end, discover, resolve_before, materialize_before }`, `ReleasedRfObservation`, `LocalRfObservation`, `LocalObservationRouter::route(observation, station_id, decision_time)` |
| World own events | `lab/world/world.h` | `TransitionKind { ..., InitialOff = 4, EquipmentOn = 5, EquipmentOff = 6 }`; `Transition { time, kind, entity_id, episode, value, requires_protocol_initialization, requires_abort_of_overlapping_burst }`; `VesselState { equipment_on, power_generation, ... }`; `VesselSnapshot { state, location, course_ndeg, time }`; `PowerModel` intervals; `inspect_burst(config, equipment_key, start, end) -> BurstPowerConstraint` |
| S2 own configuration and gate | `lab/classa/station_config.h`, `station_inputs.h/.cpp` | `OwnStationConfig` (private mint, `identity()`, `mmsi()`, `clock()`, `input_policy()` with `max_navigation_age_units`/`max_clock_age_units` in [0, 60 s], `world_revision()`); `OwnPowerNotice { own_identity, time, kind InitialOff/On/Off, power_generation }`; `StationInputGate(config)`: `apply_power`, `accept_clock(DirectUtcObservation, observed_at) -> optional<DirectUtcToken>`, `adapt_navigation(VesselSnapshot, EpfsFix, observed_at) -> OwnNavigationInput`, `current_utc(token, now)`, `qualify_inputs(navigation, clock, now) -> QualifiedOwnInputs`, `revalidate(previous, now)`, `equipment_on()`, `power_generation()`; `QualifiedOwnInputs { fields(), navigation(), clock(), current_slot(), qualified_at(), input_eligible() = true, ready_for_tx() = false }` |
| S3 knowledge | `lab/classa/station_knowledge.h/.cpp` | `StationKnowledge(config, StationInputGate&, ScenarioSeedHandoff, KnowledgeLimits)`: `apply_power`, `synchronize(own_now, now) -> now_index`, `accept_evidence(OwnedRadioEvidence, own_now, now) -> EvidenceAcceptance { allocations, superseded, unresolved, status }`, `reserve_own(OwnReservationRequest, own_now, now)`, `release_own`, `candidates(channel, SelectionInterval, block, current_own_slot, own_now, now) -> CandidateReport`, `choose_candidate(report, episode, draw, own_now, now)`, `draw_timeout`, `draw_ratdma_probability`, `draw_entry_offset(min, max, episode, draw, own_now, now)`, `choose_reuse_peer`, `drain_ledger`, `snapshot()`, `knowledge_revision()`, `map(channel)`, `directory()`; `own_inputs_fingerprint(own_now)` |
| S3 pure helpers | `candidate_policy.h`, `local_slot_map.h`, `generic_ratdma_policy.h`, `philox_protocol.h`, `radio_evidence.h` | `selection_interval(ns, ni_num, ni_den)` (LCP-P01, rejects NI outside {75,125,225,375}); `retained_frames_from_timeout_draw`; `continuous_timeout_visit/sequence`; `expiry_announcement`; `start_generic_ratdma`, `ratdma_attempt`, `replenish_candidates`; `bounded_draw`; `OwnedRadioEvidence` (UNIT_FIXTURE-only minter) |
| S1 codec | `lab/ais/communication_state.h/.cpp`, `ais_message.h`, `ais_encoder.h` | `ItdmaState19::from_effective(sync, effective_increment 0..16383, slot_count, keep)`, `effective_increment()`, `no_further_allocation()` (effective increment == 0), `SotdmaState19::{offset, offset_in_next_frame, utc, current_slot, received_stations}`, `decode`; Type 3 payload builders |

Not present at this HEAD (must not be assumed): any FSM, timer object,
TX intent type, `Type5` scheduling, RF1 physical model, S5 evidence minter,
journal-driven station adapter, playback clock.

## 2. Clocks and instants (distinct, never collapsed)

| Clock | Representation | Owner | Notes |
| --- | --- | --- | --- |
| Simulation time | `SimulationInstant.units` | runtime | The only ordering key of events; integer, exact |
| UTC information | `DirectUtcToken` (anchor, sampled_at, acquired_at, revision) and `QualifiedOwnInputs::current_slot()` | S2 gate | Availability can lapse (`accept_clock` with valid = false); the knowledge uses only the slot delivered inside the qualified token |
| Nominal slot time | absolute index = utc_minute * 2 250 + slot; slot start = `compose_utc` | S3 `slot_identity.h` | A transmission intent names a slot start instant, not an arbitrary instant |
| Physical RF interval | `TransmissionStart.time .. end_time` | RF1 (future) | Nominal span now; the 1 ms tail and timing error belong to RF1 (RADIO-EVIDENCE-v1) |
| Decode or disclosure time | `ReceptionResult.availability_time`, `OwnRadioObservation.disclosed_at` | RF1/S5 (future), UNIT_FIXTURE today | Must be at or after physical end |
| Wall-time playback | not represented | P1 (future) | Never enters a decision |

S2 freshness: `InputPolicy::max_navigation_age_units` and
`max_clock_age_units` default to 0 (`station_config.h:54-55`: "Zero requires
sampling at the requested instant"), so with the default policy a decision at
instant T needs navigation and clock samples generated exactly at T; the S3
tests use 60 s. S4 must therefore sample own W1 truth at every decision
instant (the world snapshot at T) or configure a bounded non-zero age
explicitly in the station definition; it must never reuse a token qualified
at an earlier instant (`QualifiedOwnInputs::qualified_at() == now` is
enforced by S3 `check_own`).

## 3. Runtime causal phases (IMPLEMENTED) and what S4 may do in each

`causal_phase` (`lab/runtime/event.cpp`): phase 0 `StationOn/StationOff/
ReceiverOn/ReceiverOff`; phase 1 `TransmissionEnd/RfObservation`; phase 2
`ReceptionSuccess/ReceptionFailure/RfNoReception`; phase 3 `SlotMapInit/
SlotMapReset/SlotReserve/SlotRelease/TdmaDecision`; phase 4
`TransmissionStart`; phase 5 `ReceiverNmea/OutputError/ReceptionNoOutput`;
phase 6 `TransmissionResolved`. Canonical order is (time, phase, source,
sequence, kind). The scheduler dispatches one (time, phase) group at a time
(`Scheduler::Impl::drain`), validates the whole group before exposing it,
and rejects a dynamically enqueued event unless its (time, phase) is
strictly later than the group being dispatched (`enqueue`: "dynamic event
must follow complete dispatched time/phase group"), unless its time is below
the committed half-open watermark, and unless its source sequence/time/phase
is monotone. A causal reference must point to an earlier time or a strictly
earlier phase at the same time (`validate_event`).

Mandatory sequence at one instant T for one station (contract):

1. Phase 0: own lifecycle. W1 `EquipmentOn/EquipmentOff` transitions are
   projected to `OwnPowerNotice` and applied in this order: S2
   `StationInputGate::apply_power`, S3 `StationKnowledge::apply_power`, S4
   engine `apply_power` (DESIGN ONLY). OFF at T revokes every pending intent
   and timer before any phase 2/3 work at T; ON at T starts the 60 s
   observation timer at T + 589 824 000 units.
2. Phase 1: completion of the station's own earlier physical transmission
   (RF1 future). The engine records the fact for its ledger; no decision.
3. Phase 2: disclosed local receptions. The engine first calls S3
   `synchronize(own_now(T), T)` once, then `accept_evidence` for every owned
   token in canonical order (receipt identities strictly increasing). No
   decision is taken in phase 2.
4. Phase 3: station-local decision. The engine revalidates own inputs through
   the gate at T, calls `synchronize` again (idempotent at the same slot),
   runs its due timers (observation complete, NTS due, duty due, reporting
   change), builds candidates, draws, reserves (S3 `reserve_own`), and emits
   its phase-3 events (`SlotReserve`, `SlotRelease`, `TdmaDecision`) and
   phase-4 `TransmissionStart` events for intents due at T. A phase-4 event
   at the same T is allowed because 4 > 3.
5. Phase 4: every simultaneous TX start of all stations is one group; the
   future RF1 registers them. One physical transmitter per station: the
   engine never emits two TX starts for one station at one T on different
   channels (LCP-P05 and the one-transmitter rule in S3 `reserve_own`).
6. Phases 5-6: output and resolution bookkeeping; no station decision.

Evidence arriving at T after a decision at T: structurally impossible to
enqueue (phase 2 is not later than the dispatching phase 3 group), so it is a
producer defect; S3-TIE-ORDER-v1 (`accept_evidence` rejects
`disclosed_at <= last_decision_at`) is the second, station-level line of
defence and reports it explicitly. Timers are the engine's own future
events: they are enqueued at strictly later times (never at the same time
and an earlier or equal phase). Nothing below the committed watermark is ever
enqueued (`enqueue` rejects it); a producer barrier must declare its
materialized results before `advance_watermark` (RF1 obligation, not S4's).

What S4 must NOT use the scheduler for: global slot allocation, collision
prevention between stations, truth about what other stations transmitted,
look-ahead into pending future events, or an exact future oracle. The only
station inputs are the own S2 gate, the own S3 knowledge, own timers and
owned evidence disclosed at or before the current phase.

## 4. S3 acceptance atomicity: analysis of `accept_evidence` (IMPLEMENTED behaviour)

Code path at this HEAD (`lab/classa/station_knowledge.cpp`): `accept_evidence`
calls `synchronize(own_now, now)` as its first statement (line 160);
`synchronize` (lines 139-155) runs `check_own` (owner, generation,
`qualified_at == now`, time monotonic, and the gate's `revalidate`, lines
110-127), computes the own UTC index, and when the index differs from the
previous one it advances both maps (`LocalSlotMap::advance`, pruning
records below the past horizon), expires directory peers
(`expire_stale`), prunes reuse exclusions and increments
`knowledge_revision`; it always sets `now_index_` and `latest_time_`. Only
afterwards does `accept_evidence` validate the token (provenance, owner,
receiver, channel, generation, catalog revision, receipt monotonicity,
interval, epoch start, disclosure order and lateness, horizon; lines
162-191). Map and directory mutations happen only after every token check
(lines 389-403), map before directory, superseded records never recorded,
so there is no compound partial acceptance. Ledger, draw watermarks,
receipt watermark and the OFF/ON epoch state are untouched by a rejected
token.

Consequence: a rejected token can advance the local time view, prune
records, expire peers and bump the revision, exactly as a legitimate
decision at the same instant would. This is an implementation consequence,
not an invariant violation of S3's documented contract (the S3 report
states "validation precedes mutation" for the evidence content, and
`synchronize` is a public, idempotent per-instant operation), but it
conflicts with the stronger transaction property S4 needs: "a rejected
delivery observes nothing".

Minimal witnesses (station ON at scenario 0, past horizon 4 frames, own
inputs valid; E1 = valid Type 1 from peer 2 with time-out 0 accepted at
now = index 100, which records an `ObservedUse` allocation at 100 and peer 2
in the directory; `r` = knowledge revision after E1):

| Failure class | Delivery | Before (now_index, revision, AIS1 records, peers) | After the throw | Observable side effect |
| --- | --- | --- | --- | --- |
| foreign owner | token with another vessel id at now = 2350 (one frame later) | 100, r, 1, 1 | 2350, r+1, 1, 1 | now advanced one frame, revision bumped |
| replayed receipt | receipt id equal to E1's at now = 2350 | 100, r, 1, 1 | 2350, r+1, 1, 1 | same |
| future disclosure | disclosed_at > now at now = 2350 | 100, r, 1, 1 | 2350, r+1, 1, 1 | same |
| late (S3-TIE-ORDER-v1) | disclosed_at <= last decision at now = 2350 after a decision at 2350 | 2350, r+1 (decision), 1, 1 | 2350, r+1, 1, 1 | none beyond `latest_time_` (same slot) |
| malformed | navigation status 16 at now = 2350 | 100, r, 1, 1 | 2350, r+1, 1, 1 | same as foreign owner |
| out of horizon | token received at index 100 delivered at now = 11350 (frame 5) | 100, r, 1, 1 | 11350, r+1, 0, 0 | E1's record (last covered index 100 < earliest 2350) and peer 2 (last heard 100 < 2350, no reaching reservation) are pruned by `advance`/`expire_stale` before the token is rejected |
| same slot, any class | any rejected token at now = 100 again | 100, r, 1, 1 | 100, r, 1, 1 | none (index unchanged: no advance, no bump) |

Every row also advances the S2 gate's internal `latest_time_` to `now`
through `revalidate` (a monotonic clock that S4 can never move backwards
anyway) and S3's `latest_time_`.

Recommendation: option (a). S4 must call `synchronize(own_now, now)`
explicitly at the start of every phase-2 delivery group and every phase-3
decision group, so that time advancement is an explicit, journaled act of
the station and never a by-product of a rejected delivery; and S3.1 must
make `accept_evidence` side-effect-free on rejection. Smallest S3.1 change
list (IMPLEMENTATION REQUIRED before S4-A integration acceptance, not before
S4-A coding):

1. `station_knowledge.cpp`: in `accept_evidence`, replace the leading
   `synchronize` call by `check_own(own_now, now)` plus a local
   `const auto now_index = absolute_index(slot_from_utc(own_now.current_slot()))`
   and `require(!now_index_ || now_index >= *now_index_, ...)`; keep every
   existing `require` unchanged against that local index; call
   `synchronize(own_now, now)` only immediately before the first mutation
   (`record_garble` loop or the `record_external` loop). The garble branch
   capacity check and the per-peer bound check stay before the mutation.
2. `station_knowledge.h`: document "rejected tokens leave every observable
   field of `snapshot()` unchanged; time advancement is only performed by
   `synchronize`, decisions and accepted evidence".
3. `classa_knowledge_tests.cpp`: extend `evidence_contract` so that each
   rejection class above is delivered at a later frame and
   `snapshot().now_index`, `knowledge_revision`, external counts and peer
   count are asserted unchanged; keep the 157 existing negatives.
4. `A1_S3_LOCAL_KNOWLEDGE_PHILOX_ACCEPTANCE.md`: one sentence in the
   evidence boundary section and the S3-TIE-ORDER-v1 list item 1.

Acceptance criteria for S3.1: existing suite and oracle pass unchanged
(vectors SHA `28faaace53aa323bd83667e6af02200557f4b8c6c0db554f5221b2ed7530524d`
unchanged because no emitter changes), the new assertions pass in the host
wrapper and the main Release CTest, static checks clean. S3.1 is small (one
function reordering, no interface change), and it strengthens the journal
invariant "an `EvidenceAcceptance` exception implies no state transition",
which S4's event log needs to replay deterministically. Option (b)
(documenting the side effect as allowed) is rejected because it would make
the station's revision counter depend on rejected foreign deliveries, i.e.
on data the station must not trust.

## 5. Bounded per-owner S4 interface (DESIGN ONLY, header sketch, not code)

Versioned, additive, consuming exclusively one owned configuration, gate and
knowledge; never `world::Config`, `world::Snapshot`, the peer register, the
global journal, or a "frame completed" Boolean.

```text
namespace aismixer::lab::classa {

inline constexpr std::uint32_t kAccessEngineVersion = 1;            // CLASSA-ACCESS-ENGINE-v1
inline constexpr const char* kFirstFramePolicyId = "CLASSA-FIRST-FRAME-v1";

enum class AccessState : std::uint8_t {
    Off = 0, Observe60Seconds = 1, EntrySelect = 2, EntryWait = 3,
    FirstFrame = 4,            // at least one channel still in its first-frame chain
    Continuous = 5,            // both channels in continuous SOTDMA
    LowRateItdma = 6,          // gated: rejected until CLASSA-LOWRATE-ITDMA-v1 exists
    Blocked = 7                // explicit: no legal opportunity / policy absent / inputs missing
};
enum class ChannelPhase : std::uint8_t { Idle = 0, FirstFrameChain = 1, ContinuousSotdma = 2, LowRateChain = 3 };

struct ChannelPlan {                       // one per AIS channel, owner-local
    Channel channel;
    std::optional<std::int64_t> nss_index;         // absolute
    std::optional<std::int64_t> first_nts_index;   // first-frame anchor (absolute)
    std::uint32_t transmissions_done = 0;          // n in A2-3.3.5.3.3
    ChannelPhase phase = ChannelPhase::Idle;
    std::optional<std::int64_t> next_due_index;    // next same-channel NTS
};

enum class IntentStatus : std::uint8_t { Proposed = 1, Withdrawn = 2, Blocked = 3, Missed = 4, Expired = 5 };
enum class IntentMessage : std::uint8_t { Type3Entry = 1, Type3FirstFrame = 2, Type3Terminal = 3, Type1Continuous = 4, Type5 = 5, Type24B = 6 };

struct TxIntent {                          // immutable proposal, never an RF completion
    OwnIdentity owner; std::uint64_t power_generation;
    Channel channel; std::int64_t first_slot_index; std::uint8_t block_length;   // absolute slot, 1..5
    runtime::SimulationInstant slot_start;                                        // compose_utc of the slot
    IntentMessage message; std::uint32_t communication_state_19;                  // from S1 codec
    std::uint64_t decision_id; std::uint64_t knowledge_revision; std::uint64_t inputs_fingerprint;
    std::vector<std::uint64_t> ledger_sequences;                                  // CLASSA-PHILOX-v1 draws used
    IntentStatus status; std::string reason;                                      // explicit status provenance
};

struct TimerChange { enum class Kind { ObservationComplete, NtsDue, DutyDue, RateChange } kind;
                     std::optional<Channel> channel; runtime::SimulationInstant due; bool cancelled; };

struct EngineStep {                        // result of one phase-3 decision at `now`
    std::vector<TxIntent> intents;         // due at `now` (slot start == now) or scheduled
    std::vector<TimerChange> timers;       // strictly later than `now`
    std::vector<std::string> decisions;    // TdmaDecision payload texts for the journal
    std::vector<std::string> blocked;      // explicit blocked/missed/withdrawn records
};

struct DutyRequest { IntentMessage message; runtime::SimulationInstant due; std::string cause; };  // LCP-P07 inputs

class ClassAAccessEngine {
public:
    ClassAAccessEngine(const OwnStationConfig&, StationInputGate&, StationKnowledge&, AccessPolicy);
    void apply_power(const OwnPowerNotice&);                                            // phase 0
    void deliver_evidence(const OwnedRadioEvidence&, const QualifiedOwnInputs&, runtime::SimulationInstant);  // phase 2
    EngineStep step(const QualifiedOwnInputs& own_now, runtime::SimulationInstant now); // phase 3
    void record_transmission_outcome(/* RF1 token, future */);                           // phase 1, DESIGN
    void request_duty(const DutyRequest&);                                                // Type 5 / 24B, S4-C
    AccessState state() const noexcept; const ChannelPlan& plan(Channel) const noexcept;
    EngineSnapshot snapshot() const;                                                     // owner/generation/channel/slot/decision-version provenance
};
}
```

Required input event types (projected to the engine, never raw journal
events): `OwnPowerNotice` (from W1 `EquipmentOn/EquipmentOff/InitialOff`),
`QualifiedOwnInputs` per instant (S2), `OwnedRadioEvidence` (UNIT_FIXTURE
today, S5 later), reporting-rate facts from `QualifiedOwnInputs::fields()`
(SOG, navigation status, course for Table 1 and LCP-P06), `DutyRequest`
(configuration-driven Type 5/24B duties under LCP-P07), and a future RF1
own-transmission outcome token. Outputs: `TxIntent` lists, `TimerChange`,
decision texts, explicit blocked/missed/withdrawn records, and snapshots.
A power OFF revokes pending intents (`Withdrawn`, reason `cancelled_by_off`)
and timers without inventing a normal completion and without dropping the
historical ledger or previously valid decodes. RX on both channels, one
shared TX, no simultaneous own physical starts, no global arbitration, and
no successful air outcome until RF1/I1.

Finite states and transitions (design level): `Off` -(ON)-> `Observe60Seconds`
-(ON+60 s, inputs valid)-> `EntrySelect` -(NSS/NTS chosen on both channels)->
`EntryWait` -(first NTS_A)-> `FirstFrame` (channel A `FirstFrameChain`;
channel B enters its chain at its own first NTS) -(each channel's terminal
zero)-> `Continuous` when both chains ended; `Blocked` whenever a required
input is missing, no candidate exists after NSS reselection limits, or the
configured rate is outside the policy (Rr < 2 -> `blocked_low_rate_policy_absent`);
`LowRateItdma` reserved, unreachable until its policy exists; pending Type
5/24B duties are tracked in parallel (S4-C). Report rate changes
(A2-3.3.5.5) are S4-D and are not assumed.

## 6. Own ON/OFF, history and independent clocks

- Each ON is a new power generation (S2/S3), a new CLASSA-PHILOX-v1 key
  domain and a new 60 s observation; the engine keeps the previous
  generation's intents and ledger as history (`drain_ledger` export before
  OFF processing is S4's job), never re-uses an old NSS, NTS or reservation.
- The engine's timers are derived from `OwnPowerNotice.time` (observation)
  and from absolute slot indices (NTS due); they are not derived from UTC
  minute boundaries. A UTC lapse (`accept_clock` valid = false) makes
  `qualify_inputs` fail; the engine then records `blocked_no_utc` and emits
  no intent (S1-NOGO-UTC-UNAVAILABLE stays open; A0-ISS-004 excluded).
- Deadlines (LCP-P07): Type 5 first due at ON + 360 s (policy), Class A 24B
  first due at ON + 720 s (source); both anchored at the ON notice instant,
  never shifted by the first-frame outcome; the engine records
  `not_yet_due`, `pending`, `blocked_no_legal_opportunity`, `missed_due`,
  `cancelled_by_off` explicitly (S4-C).

## 7. Staged implementation path and gates (DESIGN ONLY)

| Slice | Scope | Gate to pass before the next slice |
| --- | --- | --- |
| S3.1 | Side-effect-free rejection in `accept_evidence` (Section 4) | existing suite + new rejection-isolation assertions; vectors unchanged |
| S4-A | `Off`, `Observe60Seconds`, `EntrySelect`, `EntryWait`, per-channel `FirstFrameChain` with terminal zero at Rr in {6, 10, 18, 30} under `CLASSA-FIRST-FRAME-v1`; intents only; explicit `Blocked`; OFF handling | table-driven host tests F1/F2/F4 of Section 8 with independent expected values; determinism across worker/batch permutations; no Type 1 before the zero; no generic RATDMA during entry; S3.1 merged |
| S4-B | `ContinuousSotdma` per channel (LCP-P02 decrement, reselection with `expiry_announcement`, reuse, alternation) at fixed rate | Figure 15 oracle cases; kept-repeat continuity from S4-A chains; ledger replay |
| S4-C | Duties Type 5 / 24B via non-entry RATDMA (LCP-P03/P04, priority/FIFO) with LCP-P07 statuses | due/missed/blocked/cancelled oracle; two-slot block for Type 5; no deadline shift |
| S4-D | Reporting-interval changes (A2-3.3.5.5, Figure 16) and LCP-P06 hysteresis inputs | migration oracle; no deletion of useful reservations |
| S4-E (gated) | Low-rate ITDMA (`LowRateItdma`) | requires operator-approved `CLASSA-LOWRATE-ITDMA-v1`, LCP-P01 version extension, A0-ISS-007 adjudication |

Full S4 acceptance stays NO-GO until every slice has legitimate evidence;
the five-hour RF/UDP pilot stays NO-GO regardless (no RF1/I1/S5).

## 8. Independent test and tracing design (RQ8)

Expected values are hand-derived (see `arith_check.log`); none copies the
production implementation. Tiers: H = host unit (S4-A/S3.1), E = event
integration (future, needs the scheduler adapter), R = RF/S5 future.

| ID | Case | Expected owner-local state / transition | Numeric expectation | Negative oracle | Source / policy | Tier |
| --- | --- | --- | --- | --- | --- | --- |
| T4-01 | F1 observation and entry eligibility | `Observe60Seconds` until exactly ON + 589 824 000 units; `EntrySelect` at 67.000 s | eligibility at index 4012 (M0+1 slot 1762, 131 072 units into the slot) | any intent before 67.000 s fails | A2-3.3.3 / A0-MAC-011 | H |
| T4-02 | F1 NSS/NTS per channel | NSS_A in [4013, 4387]; fixture 4162; NSS_B = 4537; SI_A [4125, 4199], SI_B [4500, 4574] | first-frame A chain 4162, 4912, 5662, 6412; B chain 4537, 5287, 6037, 6787 | NSS_A = 4012 or 4388 rejected; NTS outside SI rejected | A2-3.3.5.2.1-.2, LCP-P01, policy E2 | H |
| T4-03 | F1 message IDs and ITDMA words | every chain transmission Message 3; increments 750, 750, 750, 0 | ITDMA19 words 12001, 12001, 12001, 1 per channel | Message 1 before the zero rejected; zero elsewhere rejected | A2-3.3.5.3.4-.6, policy F1 | H |
| T4-04 | F1 terminal zero per channel | A ends at 6412, B at 6787; both channels `ContinuousSotdma` after | 6412 - 4162 = 2250; 6787 - 4537 = 2250 | Interpretation-I behaviour (B kept repeat as Message 1) fails the policy oracle; must be reported as the retained alternative, not as PASS | FF-08 policy | H |
| T4-05 | F1 continuous first visit | A at 7162 Message 1 with time-out 2 (drawn 3 at 4912 minus one) | SOTDMA19 current-slot submessage 412 | two decrements rejected | LCP-P02 | H |
| T4-06 | F2 rollover | zero at 6749 (M0+2 slot 2249); increments cross 4500 unchanged | 4499 -> 5249 -> 5999 -> 6749 | zero at 5249 (UTC reading) rejected | FF-06/FF-12 | H |
| T4-07 | F3 anchored rate | configuration Rr = 1/3 yields `Blocked` with `blocked_low_rate_policy_absent`; no intent, no SOTDMA fallback | - | any Message 1/3 intent at Rr < 2 fails | FF-15..17, Section 7.3 of the adjudication | H |
| T4-08 | F4-a OFF at ON+59 s | zero intents; epoch cancelled; re-ON restarts 60 s | OFF at 648 806 400 units | any intent fails | A0-T02 | H |
| T4-09 | F4-b OFF at exactly ON+60 s | OFF (phase 0) wins over observation-complete (phase 3) | 658 636 800 units | `EntrySelect` reached fails | A0-SIM-004 | H / E |
| T4-10 | F4-c OFF at exactly NTS_A0 | intent withdrawn `cancelled_by_off`; no TransmissionStart at 697 827 328 | - | a TX start at that instant fails | S3-TIE-ORDER-v1 item 2 | H / E |
| T4-11 | F4-d OFF between A n=0 and B n=0 | A's single Message 3 is history; B never transmits; next ON new generation | OFF at 746 979 328 | reuse of NSS/NTS after re-ON fails | policy F4 | H |
| T4-12 | Reception at T phase 2 affects decision at T phase 3 | token disclosed at T delivered before `step(T)`; candidate set reflects it | - | same token delivered after `step(T)` rejected as late; the engine records it | S3-TIE-ORDER-v1, scheduler enqueue rule | H / E |
| T4-13 | No peer RF success fixture | a UNIT_FIXTURE reception changes owner A's map; owner B stays blind; no `ReceptionSuccess` event exists | - | any map change on B fails | invariant 3/4 | H |
| T4-14 | Retained old epoch | token with power generation 1 after re-ON generation 2 rejected; stale `QualifiedOwnInputs` fingerprint rejected by `choose_candidate` | - | acceptance fails | S3 lifecycle, fingerprint | H |
| T4-15 | INT64 / negative UTC | anchor with negative POSIX minute; index arithmetic exact; chain arithmetic unchanged | kMaxAbsoluteMinute = 4 099 276 460 824 343 | overflow must throw, never wrap | A0-T20 | H |
| T4-16 | Candidate exhaustion | no candidate on a channel -> NSS reselection; after a bounded number of reselections `Blocked`, no synthetic grant | - | a manufactured fourth candidate fails | A0-MAC-012, policy E2 | H |
| T4-17 | S3 advance/rejection isolation | after S3.1: every rejected class leaves `snapshot()` unchanged; before S3.1: documented side effect reproduced by the witness table | witness table Section 4 | any map/directory mutation on rejection fails | S3.1 | H |
| T4-18 | Ledger replay and unrelated station | same seed/keys reproduce the entry NSS/NTS draws; adding station C changes nothing | A0-T15/T16 | - | LCP-P08 | H |
| T4-19 | Worker/batch permutation | identical canonical event order and decisions with 1/2/4 workers and batch cutpoints before/after an NTS and a minute boundary | - | any trace difference fails | A0-INT-010, A0-T15 | E |
| T4-20 | No look-ahead | a decision at T uses no event with time > T or phase > 3 at T | - | reading a pending TX start of another station fails | A0-SIM-003 | E |

A passing policy test (T4-02 endpoints, T4-04, T4-07) closes no ITU
discrepancy; the source-derived tests (T4-01, T4-03, T4-05, T4-06) are the
ones that may later be cited as normative evidence. No test above has been
executed; the table is the specification for the S4-A task.
