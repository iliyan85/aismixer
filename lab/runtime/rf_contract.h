#pragma once

#include "event.h"
#include <cstddef>
#include <cstdint>
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
    std::optional<std::string> observing_station_id;  // Local knowledge, not global truth.
};
void validate_rf_submission(const RfWorkSubmission& submission);
void validate_rf_completion(const RfWorkSubmission& submission,
                            const std::vector<CompletedRfObservation>& observations);

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
