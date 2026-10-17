#pragma once

#include "event.h"
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <type_traits>
#include <vector>

namespace aismixer::lab::runtime {

// Interface/validation only. R0 contains no RF implementation or RX_OK oracle.
inline constexpr std::size_t kMaxRfTransmissions = 1024;
inline constexpr std::size_t kMaxRfReceivers = 4096;
inline constexpr std::size_t kMaxRfObservations = 65536;
struct RfTransmissionWork {
    TransmissionId transmission_id;
    std::string station_id;
    std::uint8_t channel = 0;
    SimulationInstant start;
    SimulationInstant end;
    std::string payload_reference;
};
struct RfReceiverChange {
    ReceiverId receiver_id;
    bool enabled = false;
    std::int64_t longitude_ndeg = 0;
    std::int64_t latitude_ndeg = 0;
    std::int64_t antenna_height_mm = 0;
    std::string height_datum;  // Explicit WGS84_ellipsoid; no terrain inferred.
};
struct RfWorkSubmission {
    std::uint32_t schema_version = 1;
    // Legacy schema 1 is fresh-work-only. Registered in-flight context uses
    // RfCausalBoundary below; this field is never an RF processing horizon.
    SimulationInstant commit_watermark;
    std::vector<RfTransmissionWork> transmissions;
    // Host registry snapshot, not an H2D requirement. Unchanged enabled sites
    // may remain resident while receiver_changes uploads only actual deltas.
    std::vector<ReceiverId> active_receivers;
    std::vector<RfReceiverChange> receiver_changes;
    std::size_t observation_capacity = kMaxRfObservations;
};
enum class DecodeDisposition : std::uint8_t { Undecoded = 0, Decoded = 1, Failed = 2 };
struct CompletedRfObservation {
    EventId observation_id;  // Producer semantic key, never launch/batch index.
    TransmissionId transmission_id;
    ReceiverId receiver_id;
    std::optional<ReceptionId> reception_id;
    SimulationInstant availability_time;
    DecodeDisposition disposition = DecodeDisposition::Undecoded;
    std::optional<std::string> decoded_payload_reference;
    // Legacy descriptive label only; not ownership proof for station decisions.
    std::optional<std::string> observing_station_id;
};
void validate_rf_submission(const RfWorkSubmission& submission);
void validate_rf_completion(const RfWorkSubmission& submission,
                            const std::vector<CompletedRfObservation>& observations);

class Scheduler;
class CausalProducerBarrier;

enum class ReceiverRole : std::uint8_t { TransceiverRadio = 1, PassiveTerrestrial = 2 };
struct ReceiverOwnership {
    ReceiverId receiver_id;
    ReceiverRole role = ReceiverRole::PassiveTerrestrial;
    std::optional<std::string> owner_station_id;
    bool enabled = false;
};

// Immutable, bounded host catalog for this boundary's lifetime. Role/ownership
// are explicit, not inferred from journal v1 local_station_id or geography.
// A future live registry must version receiver lifecycle/position deltas.
class ReceiverRegistry {
public:
    ReceiverRegistry(std::vector<std::string> station_ids,
                     std::vector<ReceiverOwnership> receivers);
    ReceiverRegistry(const ReceiverRegistry&) = default;
    ReceiverRegistry(ReceiverRegistry&&) = delete;
    ReceiverRegistry& operator=(const ReceiverRegistry&) = delete;
    ReceiverRegistry& operator=(ReceiverRegistry&&) = delete;
    const ReceiverOwnership& receiver(const ReceiverId& id) const;
    const std::vector<ReceiverOwnership>& receivers() const noexcept;
private:
    std::vector<std::string> stations_;
    std::vector<ReceiverOwnership> receivers_;
};

enum class RfPairDisposition : std::uint8_t {
    Carrier = 0, Decoded = 1, Failed = 2, NotAudible = 3, NotScheduled = 4
};
struct RfDiscovery {
    // Stable semantic discriminator for otherwise equal carrier observations;
    // never a CUDA thread, compaction index, completion order or batch number.
    std::string semantic_key;
    TransmissionId transmission_id;
    ReceiverId receiver_id;
    SimulationInstant availability_time;
    RfPairDisposition disposition = RfPairDisposition::Carrier;
    std::optional<std::string> decoded_payload_reference;
};

// Only a stateful producer can mint this delivery token, after the actual
// corresponding Event was accepted by Scheduler. Routing additionally proves
// that Scheduler actually validated/dispatched it. Tokens/router calls must
// not outlive their attached Scheduler, which must remain at a stable address
// and must not be moved while its boundary/tokens are used. Arbitrary global Events
// cannot be injected into the station-local router.
class ReleasedRfObservation {
public:
    const Event& event() const noexcept;
private:
    friend class RfCausalBoundary;
    friend class LocalObservationRouter;
    ReleasedRfObservation(Event event, std::shared_ptr<const ReceiverRegistry> registry,
                          const Scheduler* scheduler,
                          std::optional<std::string> decoded_payload_reference);
    Event event_;
    std::shared_ptr<const ReceiverRegistry> registry_;
    const Scheduler* scheduler_;
    std::optional<std::string> decoded_payload_reference_;
};
// Filtered station input: no journal Event, physical TX ID, remote station,
// position or other global truth. Payload identity is present only on decode.
class LocalRfObservation {
public:
    const std::string& station_id() const noexcept;
    const ReceiverId& receiver_id() const noexcept;
    SimulationInstant availability_time() const noexcept;
    const EventId& causal_event_id() const noexcept;
    DecodeDisposition disposition() const noexcept;
    const std::optional<std::string>& decoded_payload_reference() const noexcept;
private:
    friend class LocalObservationRouter;
    LocalRfObservation(std::string station_id, ReceiverId receiver_id,
        SimulationInstant availability_time, EventId causal_event_id,
        DecodeDisposition disposition, std::optional<std::string> decoded_payload_reference);
    std::string station_id_;
    ReceiverId receiver_id_;
    SimulationInstant availability_time_;
    EventId causal_event_id_;
    DecodeDisposition disposition_;
    std::optional<std::string> decoded_payload_reference_;
};
class LocalObservationRouter {
public:
    explicit LocalObservationRouter(std::shared_ptr<const ReceiverRegistry> registry);
    LocalRfObservation route(const ReleasedRfObservation& observation,
                             const std::string& station_id,
                             SimulationInstant decision_time) const;
private:
    std::shared_ptr<const ReceiverRegistry> registry_;
};

struct RfBoundaryStats {
    std::size_t active_transmissions = 0;
    std::size_t candidate_pairs = 0;
    std::size_t pending_discoveries = 0;
    std::size_t producer_identities = 0;
    std::uint64_t released_events = 0;
};

// Additive in-memory schema 2; no journal-v1/POD ABI change and no RF model.
// Register each fresh physical TX once with its finite coverage roster and
// actual Scheduler-accepted causal anchors. Registration binds every snapshot
// field to that exact accepted TX start; the end binds its TX/time/start cause.
// Pending acceptance alone does not prove publication. context() supplies TX in later RF
// slices, including start < committed event watermark. New retroactive TXs
// remain forbidden. All final dispositions are explicit, including no decode.
class RfCausalBoundary {
public:
    static constexpr std::uint32_t schema_version = 2;
    RfCausalBoundary(std::string stable_producer_id,
                     std::shared_ptr<const ReceiverRegistry> registry,
                     std::size_t observation_capacity = kMaxRfObservations);
    ~RfCausalBoundary();
    RfCausalBoundary(RfCausalBoundary&&) noexcept;
    RfCausalBoundary& operator=(RfCausalBoundary&&) noexcept;
    RfCausalBoundary(const RfCausalBoundary&) = delete;
    RfCausalBoundary& operator=(const RfCausalBoundary&) = delete;
    std::shared_ptr<const CausalProducerBarrier> barrier() const;
    // Required before registration. Attaches to exactly one live, unmoved Scheduler;
    // known RF obligations then guard its existing advance/finish APIs.
    void attach(Scheduler& scheduler);
    void register_transmission(RfTransmissionWork transmission,
        TransmissionCoverage coverage, EventId start_event_id);
    void record_transmission_end(const TransmissionId& id, EventId end_event_id);
    const RfTransmissionWork& context(const TransmissionId& id) const;
    void discover(RfDiscovery observation);
    // Producer assertion: every potential discovery with availability < H is
    // known. Overdue open pair obligations reject the assertion; discoveries
    // arriving behind an asserted H are subsequently rejected, never accepted
    // as late feedback. H is distinct from the committed event watermark.
    void resolve_before(SimulationInstant horizon);
    // Explicit terminal barrier includes INT64_MAX without a last-time+1.
    void resolve_terminal();
    // Assign EventId/reception counters in canonical release order, then
    // enqueue the real events. Failure poisons the producer, blocking commit.
    std::vector<ReleasedRfObservation> materialize_before(
        SimulationInstant watermark, Scheduler& scheduler);
    std::vector<ReleasedRfObservation> materialize_all(Scheduler& scheduler);
    // Releases only bounded RF context, not core validator causal anchors.
    // The monotone producer-ID proof survives this retirement.
    void retire_transmission(const TransmissionId& id);
    SimulationInstant rf_resolved_before() const noexcept;
    SimulationInstant event_commit_watermark() const noexcept;
    RfBoundaryStats stats() const noexcept;
private:
    struct Impl;
    std::shared_ptr<Impl> impl_;
};

// CUDA-free POD transfer envelope. Numeric keys require a collision-checked,
// stable host registry binding to the typed ASCII IDs above. They are immutable
// producer identities, not dense-array positions. This is not a file ABI.
struct RfTransmissionKey { std::uint64_t value; };
struct RfReceiverKey { std::uint64_t value; };
struct RfReceptionKey { std::uint64_t value; };
struct CompactRfObservation {
    RfTransmissionKey transmission;
    RfReceiverKey receiver;
    RfReceptionKey reception;  // Zero means absent for an undecoded observation.
    std::uint64_t source_local_sequence;
    std::int64_t transmission_start_units;
    std::int64_t transmission_end_units;
    std::int64_t availability_units;
    std::uint32_t schema_version;
    std::uint8_t channel;
    DecodeDisposition disposition;
    std::uint8_t reserved[2];
};
static_assert(std::is_trivial_v<CompactRfObservation> &&
              std::is_standard_layout_v<CompactRfObservation> &&
              sizeof(CompactRfObservation) == 64,
              "Compact RF transfer envelope must be an explicit 64-byte POD");
static_assert(offsetof(CompactRfObservation, availability_units) == 48 &&
              offsetof(CompactRfObservation, schema_version) == 56,
              "Compact RF transfer offsets changed");

// Activation randomness is deliberately unspecified beyond semantic inputs.
// A later model must version its algorithm and key(seed, station_id, purpose),
// record the effective seed/model version, and preserve station association.
// R0 does not choose an activation time or sample a cylinder distribution.
struct FutureActivationKey {
    std::uint64_t effective_seed;
    std::string station_id;
    std::string rng_model_version;
    std::string purpose;
};

}  // namespace aismixer::lab::runtime
