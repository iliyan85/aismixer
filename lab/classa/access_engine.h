#pragma once

#include "station_knowledge.h"
#include "ais_encoder.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace aismixer::lab::classa {

inline constexpr std::uint32_t kAccessEngineVersion = 1;
inline constexpr const char* kAccessEngineId = "CLASSA-ACCESS-ENGINE-v1";
inline constexpr const char* kFirstFramePolicyId = "CLASSA-FIRST-FRAME-v1";
inline constexpr const char* kEntryRetryPolicyId = "CLASSA-ENTRY-RETRY-v1";

enum class AccessState : std::uint8_t {
    Off, Observe60Seconds, EntrySelect, EntryWait, FirstFrame, Continuous, Blocked
};
enum class ChannelPhase : std::uint8_t { Idle, WaitingFirst, FirstFrameChain, ContinuousSotdma };
enum class IntentStatus : std::uint8_t { Proposed = 1, Due, Withdrawn, Missed, Blocked };
enum class IntentMessage : std::uint8_t { Type3Entry = 1, Type3FirstFrame, Type3Terminal };
enum class RateSource : std::uint8_t { OwnSteadyNavigation, FixedFixture };
enum class BlockedReason : std::uint8_t {
    None, NoUtc, OwnInputsUnavailable, LowRatePolicyAbsent, UnsupportedRate,
    NoLegalCandidates, MissedDeadline, ReservationConflict, ReservationFailure,
    HistoryCapacity, RateChangeNotImplemented, LedgerOrDrawFailure
};
const char* access_state_name(AccessState state);
const char* channel_phase_name(ChannelPhase phase);
const char* intent_status_name(IntentStatus status);
const char* intent_message_name(IntentMessage message);
const char* blocked_reason_name(BlockedReason reason);

// SIMULATION_POLICY: fixed-rate first-frame engineering only. Automatic mode
// classifies a bounded steady-navigation Table 1 subset (6/10/30); it does not
// infer course-change history. FixedFixture requires explicit provenance and
// is the separate bounded fixture profile for Rr18, not automatic inference.
struct AccessPolicy {
    std::uint32_t version = kAccessEngineVersion;
    std::string first_frame_policy_id = kFirstFramePolicyId;
    RateSource rate_source = RateSource::OwnSteadyNavigation;
    std::int64_t fixed_rate_numerator = 6;
    std::int64_t fixed_rate_denominator = 1;
    std::string rate_provenance;
    std::uint32_t entry_retry_limit = 16;  // CLASSA-ENTRY-RETRY-v1: 1..256.
    std::size_t history_capacity = 4096;  // Combined due/proposal records: 1..65536.
};

struct ChannelPlan {
    Channel channel = Channel::Ais1;
    std::optional<std::int64_t> nss_index;
    std::optional<std::int64_t> first_nts_index;
    std::optional<std::int64_t> next_due_index;
    std::uint32_t transmissions_done = 0;
    ChannelPhase phase = ChannelPhase::Idle;
    std::uint64_t first_reservation_decision_id = 0;
};

// A future slot proposal is deliberately not a TxIntent. Its next ITDMA
// announcement and payload are not fabricated from future own inputs.
struct PlannedSlot {
    OwnIdentity owner;
    std::uint64_t power_generation = 0;
    Channel channel = Channel::Ais1;
    std::int64_t first_slot_index = 0;
    runtime::SimulationInstant slot_start;
    std::uint8_t block_length = 1;
    std::uint64_t proposal_id = 0;
    std::uint32_t ordinal = 0;
    std::int64_t nss_index = 0;
    std::uint64_t knowledge_revision = 0;
    std::uint64_t inputs_fingerprint = 0;
    std::vector<std::uint64_t> ledger_sequences;
    IntentStatus status = IntentStatus::Proposed;
    std::string reason;
};

// Fully formed LOCAL DUE intent only: never a physical transmission or a
// reception. slot_start == the exact step instant. S1 payload contains AIS
// data bits/armoring metadata, not radio framing, FCS or NMEA sentences.
struct TxIntent {
    OwnIdentity owner;
    std::uint64_t power_generation = 0;
    Channel channel = Channel::Ais1;
    std::int64_t first_slot_index = 0;
    runtime::SimulationInstant slot_start;
    std::uint8_t block_length = 1;
    IntentMessage message = IntentMessage::Type3Entry;
    IntentStatus status = IntentStatus::Due;
    std::uint32_t ordinal = 0;
    std::int64_t nss_index = 0;
    std::optional<std::int64_t> next_slot_index;
    std::uint32_t communication_state_19 = 0;
    std::uint64_t proposal_id = 0;
    std::uint64_t decision_id = 0;
    std::uint64_t knowledge_revision = 0;
    std::uint64_t inputs_fingerprint = 0;
    OwnNavigationFields fields;
    OwnInputProvenance provenance;
    std::vector<std::uint64_t> ledger_sequences;
    ais::EncodedPayload payload;
    std::string reason;
};

struct TimerChange {
    enum class Kind : std::uint8_t { ObservationComplete, NtsDue } kind = Kind::ObservationComplete;
    std::optional<Channel> channel;
    runtime::SimulationInstant due;
    bool cancelled = false;
};
struct EngineStep {
    std::vector<PlannedSlot> proposals;  // Future only; no fabricated communication state.
    std::vector<TxIntent> intents;      // DUE-at-now only; yielded exactly once.
    std::vector<PlannedSlot> withdrawn;
    std::vector<TimerChange> timers;
    std::vector<BlockedReason> blocked;
    std::vector<std::string> decisions;
    AccessState state = AccessState::Off;
};
struct EngineSnapshot {
    OwnIdentity owner;
    std::uint64_t power_generation = 0;
    AccessState state = AccessState::Off;
    BlockedReason blocked_reason = BlockedReason::None;
    std::optional<runtime::SimulationInstant> observation_deadline;
    std::array<ChannelPlan, 2> channels;
    std::vector<PlannedSlot> pending;
    std::int64_t reports_per_minute = 0;
    std::int64_t nominal_increment = 0;
    std::uint32_t entry_attempts = 0;
    std::array<std::uint64_t, kProtocolPurposeCount> next_draw_episode{};
    std::size_t due_history_records = 0, proposal_history_records = 0;
};

class ClassAAccessEngine {
public:
    // gate/knowledge must outlive the engine. Constructor checks owner,
    // scenario and configuration equivalence without mutating either object.
    // Their private actual gate binding is proved by BOTH revalidations on
    // every step before a draw or reservation, not by inspecting private data.
    ClassAAccessEngine(const OwnStationConfig& config, StationInputGate& gate,
                       StationKnowledge& knowledge, AccessPolicy policy = {});
    ~ClassAAccessEngine();
    ClassAAccessEngine(const ClassAAccessEngine&) = delete;
    ClassAAccessEngine& operator=(const ClassAAccessEngine&) = delete;
    ClassAAccessEngine(ClassAAccessEngine&&) = delete;
    ClassAAccessEngine& operator=(ClassAAccessEngine&&) = delete;

    // Phase 0: caller dispatches gate, knowledge, engine in that order.
    // Returned timer cancellations/withdrawals are local decisions only.
    // Power after this engine's phase-2/3 work at the same instant is rejected;
    // the caller must not expect rollback of its earlier external dispatch.
    EngineStep apply_power(const OwnPowerNotice& notice);
    // Phase 2 UNIT_FIXTURE boundary: explicit synchronize before forwarding;
    // the S3.1 acceptance transaction and S3-TIE-ORDER-v1 remain authoritative.
    EvidenceAcceptance deliver_evidence(const OwnedRadioEvidence& evidence,
                                        const QualifiedOwnInputs& own_now,
                                        runtime::SimulationInstant now);
    // Phase 3: fresh exact-now S2 inputs, explicit synchronize, decisions.
    EngineStep step(const QualifiedOwnInputs& own_now, runtime::SimulationInstant now);
    // Missing inputs have no invented token or UTC slot. This blocks the
    // epoch and withdraws plans; stale local S3 reservations are conservative
    // intentions until authoritative OFF clears them (no false clock proof).
    EngineStep block_inputs(runtime::SimulationInstant now, BlockedReason reason);

    AccessState state() const noexcept;
    const ChannelPlan& channel_plan(Channel channel) const;
    EngineSnapshot snapshot() const;
    const std::vector<TxIntent>& history() const noexcept;
    const std::vector<PlannedSlot>& proposal_history() const noexcept;
    // Explicit caller-owned history export; never silent eviction. Proposal
    // export requires no live pending slot so a later cancellation is retained.
    std::vector<TxIntent> drain_history();
    std::vector<PlannedSlot> drain_proposal_history();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace aismixer::lab::classa
