#pragma once

#include "candidate_policy.h"
#include "generic_ratdma_policy.h"
#include "scenario_seed.h"
#include "station_inputs.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace aismixer::lab::classa {

// OWN-LOCAL-KNOWLEDGE-v1 (SIMULATION_POLICY composition over source-derived
// map/directory/candidate rules). One StationKnowledge belongs to one
// (vessel, equipment, radio) owner, one StationInputGate and one power
// generation. It couples the two channel maps, the directory, the reuse
// ledger and the CLASSA-PHILOX-v1 draw keys to the owner's authoritative
// power notices. Every decision input is OWN QualifiedOwnInputs qualified at
// the exact decision instant AND revalidated through the owning gate inside
// every call, so a token revoked at that same instant, a token from a copy
// of the configuration (shadow gate) or a gate that is already OFF is
// rejected; the gate is neither replaced nor bypassed. There is no
// world/config/snapshot/peer/journal query, no transmitter and no network
// entry decision. ready_for_tx() is permanently false in A1-S3.
//
// S3-TIE-ORDER-v1 (SIMULATION_POLICY):
//  1. Reception precedes decision at an equal instant: evidence whose
//     disclosure instant is at or before the last decision instant is
//     rejected as late (A0-SIM-003/004, A0-INT-010); callers deliver all
//     evidence disclosed at t before any decision at t.
//  2. OFF dominates: ON at the OFF instant is rejected; evidence or decisions
//     after OFF are rejected; knowledge accepted before an OFF at the same
//     instant is cleared by it.
//  3. Own and external retention are inclusive of their last frame; a reuse
//     exclusion ends exclusively at excluded_until_index; a candidate report
//     is consumable only at its own now_index and knowledge revision.
//  4. Draw keys (episode, draw_index) strictly increase per purpose within a
//     power generation; S4 owns the counters, S3 rejects any replay.
//  5. The draw ledger is immutable and bounded: when full, further draws are
//     rejected explicitly until drain_ledger() exports the entries.
//
// EVIDENCE-ACCEPTANCE-ATOMICITY-v1 (A1-S3.1, SIMULATION_POLICY): accept_evidence
// is transactional. Validation and the complete application run on staged
// copies of the containers the token would change; only then are they
// installed by non-throwing moves. A rejected or throwing acceptance leaves
// every observable field unchanged (snapshot(), both maps with their
// instants and records, the directory, reuse exclusions, the receipt,
// disclosure and decision frontiers, draw watermarks and the ledger), does
// not consume the receipt identity and performs no slot advance; its only
// side effects are the owning StationInputGate's monotonic revalidation clock
// (S2 authority, not rolled back) and this object's mirror of that clock, the
// reversed-time guard that check_own advances once the gate accepted the
// revalidation (as A1-S3 did through synchronize). A valid token at a later own UTC slot
// advances both maps, expires the directory and the reuse exclusions exactly
// as synchronize would and then records the evidence, in one commit with the
// same revision increments (advance plus evidence); capacity, horizon and
// lineage rules are judged on that post-advance state. Callers may still
// synchronize explicitly at the start of a delivery or decision group: that
// explicit transition, not a rejected delivery, is the rollback baseline.
inline constexpr std::uint32_t kStationKnowledgeVersion = 1;
inline constexpr const char* kStationKnowledgeId = "OWN-LOCAL-KNOWLEDGE-v1";
inline constexpr std::size_t kProtocolPurposeCount = 5;  // Purposes 101..105.

struct KnowledgeLimits {
    MapLimits map;
    std::size_t peer_capacity = kDefaultPeerCapacity;
    // Sized for a five-hour fixture at the 2 s Table 1 rate (about 12 draws
    // per minute per station) with margin; S4 drains before it fills.
    std::size_t ledger_capacity = 16384;
};

struct EvidenceAcceptance {
    std::uint64_t receipt_id = 0;
    Channel channel = Channel::Ais1;
    std::int64_t received_index = 0;
    std::uint8_t span_slots = 1;
    ObservationDisposition disposition = ObservationDisposition::EnergyOnly;
    std::optional<std::uint32_t> peer;
    std::vector<ExternalAllocation> allocations;  // Recorded in the owner's map.
    std::vector<ExternalAllocation> superseded;   // Valid but older than a known announcement; not recorded.
    std::vector<std::string> unresolved;          // Typed announcement facts that yielded no allocation.
    std::string status;
};

struct OwnReservationRequest {
    Channel channel = Channel::Ais1;
    std::int64_t first_index = 0;
    std::uint8_t block_length = 1;
    OwnReservationKind kind = OwnReservationKind::Sotdma;
    std::uint8_t timeout_frames = 0;  // retained_frames_from_timeout_draw(drawn) for continuous SOTDMA.
    bool keep = false;
    std::string cause;
};

// FNV-1a64 over the own fix (generation and observation instants, position),
// clock sample and qualification instant: a report built from superseded own
// inputs is refused even when the knowledge state and decision slot match.
std::uint64_t own_inputs_fingerprint(const QualifiedOwnInputs& own_now);

struct DrawKeyWatermark {
    ProtocolPurpose purpose = ProtocolPurpose::CandidateChoice;
    std::uint64_t episode = 0;
    std::uint64_t draw_index = 0;
};

struct KnowledgeSnapshot {
    std::uint64_t power_generation = 0;
    bool equipment_on = false;
    std::optional<std::int64_t> now_index;
    std::uint64_t knowledge_revision = 0;
    std::optional<runtime::SimulationInstant> last_decision_at;
    std::vector<OwnReservation> own_ais1;
    std::vector<OwnReservation> own_ais2;
    std::size_t external_ais1 = 0, external_ais2 = 0, garble_ais1 = 0, garble_ais2 = 0;
    std::vector<PeerRecord> peers;
    std::vector<ReuseExclusion> reuse_exclusions;
    std::vector<DrawKeyWatermark> draw_watermarks;
    std::size_t ledger_entries = 0;
    std::uint64_t ledger_issued = 0;
    std::uint64_t ledger_drained = 0;
    std::uint64_t last_receipt_id = 0;
};

class StationKnowledge {
public:
    // The gate must outlive the knowledge and belong to the same station.
    StationKnowledge(const OwnStationConfig& config, StationInputGate& gate, const ScenarioSeedHandoff& seed,
                     KnowledgeLimits limits = {});
    StationKnowledge(const StationKnowledge&) = delete;
    StationKnowledge& operator=(const StationKnowledge&) = delete;
    StationKnowledge(StationKnowledge&&) = delete;
    StationKnowledge& operator=(StationKnowledge&&) = delete;

    const OwnStationConfig& config() const noexcept { return config_; }
    bool equipment_on() const noexcept { return on_; }
    std::uint64_t power_generation() const noexcept { return generation_; }
    std::uint64_t knowledge_revision() const noexcept { return revision_; }
    bool ready_for_tx() const noexcept { return false; }

    // Authoritative OWN lifecycle. OFF clears live knowledge, own reservations,
    // reuse exclusions, receipt counters and draw watermarks; a new ON starts
    // a fresh epoch and key domain.
    void apply_power(const OwnPowerNotice& notice);
    // Revalidate the inputs through the owning gate and advance both maps and
    // the directory horizon to the owner's current UTC slot.
    std::int64_t synchronize(const QualifiedOwnInputs& own_now, runtime::SimulationInstant now);
    // Closed boundary: only owned evidence tokens. Transactional since A1-S3.1
    // (EVIDENCE-ACCEPTANCE-ATOMICITY-v1 above): validation and the complete
    // application precede any visible mutation; a rejection changes no S3-owned
    // observable (the gate clock and its mirror are the documented exceptions).
    EvidenceAcceptance accept_evidence(const OwnedRadioEvidence& evidence,
                                       const QualifiedOwnInputs& own_now, runtime::SimulationInstant now);
    // Own intentions: local expectations, never exclusive grants or permission to transmit.
    OwnReservation reserve_own(const OwnReservationRequest& request,
                               const QualifiedOwnInputs& own_now, runtime::SimulationInstant now);
    bool release_own(Channel channel, std::int64_t first_index,
                     const QualifiedOwnInputs& own_now, runtime::SimulationInstant now);
    // Pure candidate construction at the decision instant.
    CandidateReport candidates(Channel channel, const SelectionInterval& interval, std::uint8_t block_length,
                               std::optional<std::int64_t> current_own_slot_index,
                               const QualifiedOwnInputs& own_now, runtime::SimulationInstant now);
    // Semantic draws; every entry is appended to the bounded ledger. The
    // report must have been built by this object at the same decision slot
    // and knowledge revision (no evidence, reservation or power change since).
    CandidateChoice choose_candidate(const CandidateReport& report, std::uint64_t episode, std::uint64_t draw_index,
                                     const QualifiedOwnInputs& own_now, runtime::SimulationInstant now);
    DrawLedgerEntry draw_timeout(std::uint64_t episode, std::uint64_t draw_index,
                                 const QualifiedOwnInputs& own_now, runtime::SimulationInstant now);
    DrawLedgerEntry draw_ratdma_probability(std::uint64_t episode, std::uint64_t draw_index,
                                            const QualifiedOwnInputs& own_now, runtime::SimulationInstant now);
    // Caller-supplied inclusive nonnegative offset bounds: this slice neither
    // interprets nor bounds the entry interval (A0-ISS-003 stays with S4).
    DrawLedgerEntry draw_entry_offset(std::int64_t minimum, std::int64_t maximum, std::uint64_t episode,
                                      std::uint64_t draw_index, const QualifiedOwnInputs& own_now,
                                      runtime::SimulationInstant now);
    // Equiprobable choice among peers tying at the maximum reuse distance (S4 rule).
    DrawLedgerEntry choose_reuse_peer(const std::vector<std::uint32_t>& peers_ascending, std::uint64_t episode,
                                      std::uint64_t draw_index, const QualifiedOwnInputs& own_now,
                                      runtime::SimulationInstant now);
    DrawKey draw_key(ProtocolPurpose purpose, std::uint64_t episode, std::uint64_t draw_index) const;

    const LocalSlotMap& map(Channel channel) const;
    const PeerDirectory& directory() const noexcept { return directory_; }
    std::vector<DrawLedgerEntry> ledger() const;
    // Exports and clears the retained entries; issued/drained counters keep the history auditable.
    std::vector<DrawLedgerEntry> drain_ledger();
    std::uint64_t ledger_issued() const noexcept { return ledger_issued_; }
    std::uint64_t ledger_drained() const noexcept { return ledger_drained_; }
    const std::vector<ReuseExclusion>& reuse_exclusions() const noexcept { return reuse_exclusions_; }
    KnowledgeSnapshot snapshot() const;

private:
    void check_own(const QualifiedOwnInputs& own_now, runtime::SimulationInstant now);
    LocalSlotMap& map_for(Channel channel);
    void require_ledger_room() const;
    void check_key(ProtocolPurpose purpose, std::uint64_t episode, std::uint64_t draw_index) const;
    void commit_key(ProtocolPurpose purpose, std::uint64_t episode, std::uint64_t draw_index);
    void record(DrawLedgerEntry entry, runtime::SimulationInstant now);
    void reset_epoch_state();
    OwnStationConfig config_;
    StationInputGate& gate_;
    ScenarioSeedHandoff seed_;
    KnowledgeLimits limits_;
    LocalSlotMap ais1_;
    LocalSlotMap ais2_;
    PeerDirectory directory_;
    std::deque<DrawLedgerEntry> ledger_;
    std::uint64_t ledger_issued_ = 0, ledger_drained_ = 0;
    std::vector<ReuseExclusion> reuse_exclusions_;
    std::array<std::optional<std::pair<std::uint64_t, std::uint64_t>>, kProtocolPurposeCount> watermarks_;
    std::uint64_t generation_ = 0;
    std::uint64_t revision_ = 0;
    bool power_initialized_ = false, on_ = false;
    std::optional<runtime::SimulationInstant> latest_time_, epoch_started_at_, last_off_at_, last_disclosed_at_;
    std::optional<runtime::SimulationInstant> last_decision_at_;
    std::optional<std::int64_t> now_index_;
    std::uint64_t last_receipt_id_ = 0;
    std::uint64_t next_decision_id_ = 1;
};

}  // namespace aismixer::lab::classa
