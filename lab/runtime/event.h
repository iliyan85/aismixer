#pragma once

#include "simulation_time.h"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <variant>
#include <vector>

namespace aismixer::lab::runtime {

inline constexpr std::uint32_t kEventSchemaVersion = 1;
inline constexpr std::size_t kMaxIdentityBytes = 64;
inline constexpr std::size_t kMaxEventTextBytes = 4096;
inline constexpr std::size_t kMaxEventSources = 1024;
inline constexpr std::size_t kCausalHistoryCapacity = 4096;
inline constexpr std::size_t kLifecycleCapacity = 4096;
inline constexpr std::size_t kMaxCoverageReceivers = 512;
inline constexpr std::size_t kMaxActiveTransmissions = 1024;
inline constexpr std::size_t kMaxActiveReceptions = 4096;
inline constexpr std::size_t kMaxActiveReceiverPairs = 65536;

enum class ValidationProfile : std::uint32_t { LegacyV1 = 1, StreamingV2 = 2 };

struct ReceiverId { std::string value; };
struct TransmissionId { std::string value; };
struct ReceptionId { std::string value; };
struct EventId { std::string source_id; std::uint64_t sequence = 0; };
bool operator==(const ReceiverId& a, const ReceiverId& b) noexcept;
bool operator==(const TransmissionId& a, const TransmissionId& b) noexcept;
bool operator==(const ReceptionId& a, const ReceptionId& b) noexcept;
bool operator==(const EventId& a, const EventId& b) noexcept;

// Codes are journal v1 wire values; do not renumber.
enum class EventKind : std::uint16_t {
    StationOn = 1, StationOff = 2, ReceiverOn = 3, ReceiverOff = 4,
    SlotMapInit = 5, SlotMapReset = 6, SlotReserve = 7, SlotRelease = 8,
    TdmaDecision = 9, TransmissionStart = 10, TransmissionEnd = 11,
    RfObservation = 12, ReceptionSuccess = 13, ReceptionFailure = 14,
    ReceiverNmea = 15, OutputError = 16, RfNoReception = 17,
    ReceptionNoOutput = 18, TransmissionResolved = 19
};
struct StationLifecycle { std::string station_id; };
struct ReceiverLifecycle { ReceiverId receiver_id; };
struct SlotMapChange {
    std::string station_id;
    std::uint8_t channel = 0;  // AIS A=0, B=1; local station knowledge only.
    std::uint16_t slot = 0;
    std::string reason;
};
struct TdmaDecision { std::string station_id; std::string decision; };
struct TransmissionCoverage {
    std::vector<ReceiverId> candidate_receivers;
    SimulationInstant resolution_horizon;
};
struct TransmissionStart {
    TransmissionId transmission_id;
    std::string station_id;
    std::uint8_t channel = 0;
    SimulationInstant end_time;
    std::string payload_reference;  // Immutable external payload identity, not NMEA.
    std::optional<TransmissionCoverage> coverage;
};
struct TransmissionEnd { TransmissionId transmission_id; };
struct RfObservation {
    TransmissionId transmission_id;
    ReceiverId receiver_id;
    SimulationInstant availability_time;
    bool carrier_detected = false;  // Never implies a successful decode.
    std::optional<std::string> local_station_id;
    std::string detail;
};
struct ReceptionResult {
    ReceptionId reception_id;
    TransmissionId transmission_id;
    ReceiverId receiver_id;
    SimulationInstant availability_time;
    std::string detail;  // Fixture provenance / future RF model explanation.
};
struct ReceiverNmea {
    ReceiverId receiver_id;
    ReceptionId reception_id;
    TransmissionId transmission_id;
    std::vector<std::string> sentences;  // Exact finalized, ordered CRLF bytes.
};
struct OutputError { ReceiverId receiver_id; std::string code; std::string detail; };
enum class NoReceptionReason : std::uint8_t { NotAudible = 1, NotScheduled = 2 };
struct RfNoReception {
    TransmissionId transmission_id;
    ReceiverId receiver_id;
    NoReceptionReason reason = NoReceptionReason::NotAudible;
    std::string detail;
};
struct ReceptionNoOutput {
    ReceiverId receiver_id;
    ReceptionId reception_id;
    TransmissionId transmission_id;
    std::string reason;
};
struct TransmissionResolved {
    TransmissionId transmission_id;
    SimulationInstant resolution_horizon;
};
using EventPayload = std::variant<StationLifecycle, ReceiverLifecycle, SlotMapChange,
    TdmaDecision, TransmissionStart, TransmissionEnd, RfObservation,
    ReceptionResult, ReceiverNmea, OutputError, RfNoReception, ReceptionNoOutput,
    TransmissionResolved>;
struct Event {
    std::uint32_t schema_version = kEventSchemaVersion;
    SimulationInstant time;
    EventKind kind = EventKind::StationOn;
    std::string source_id;
    std::string subject_id;
    EventId event_id;
    std::optional<EventId> causal_ref;
    EventPayload payload;
};

// Phase 0 lifecycle; 1 TX end/RF; 2 receptions; 3 station-local slot/TDMA;
// 4 simultaneous TX starts; 5 NMEA/output; 6 schema2 resolution bookkeeping.
// Same-phase events cannot be causes
// of one another at the same instant, regardless of their display sort order.
std::uint8_t causal_phase(EventKind kind);
struct CanonicalLess { bool operator()(const Event& a, const Event& b) const; };
std::optional<ReceiverId> event_receiver(const Event& event);
void validate_identity(const std::string& identity, const char* field);
void validate_event(const Event& event);  // Shape, ranges and exact NMEA framing.
void validate_receiver_nmea(const ReceiverNmea& output);
TransmissionId make_transmission_id(const std::string& producer, std::uint64_t counter);
ReceptionId make_reception_id(const std::string& producer, std::uint64_t counter);

struct ValidatorStats {
    std::size_t active_transmissions = 0;
    std::size_t active_receptions = 0;
    std::size_t active_receiver_pairs = 0;
    std::size_t typed_anchors = 0;
    std::size_t peak_active_transmissions = 0;
    std::size_t peak_active_receptions = 0;
    std::size_t peak_active_receiver_pairs = 0;
    std::size_t peak_typed_anchors = 0;
};

// Profile1 preserves its4096-per-run TX/RX identity bounds and generic4096-event
// cause window. Opt-in profile2 retains only active correlations and live typed
// anchors, with run-global injective producer counters surviving retirement.
// The latest carrier anchor per active TX/receiver supersedes an earlier one;
// older arbitrary references remain subject to the generic recent window.
// Source sequences are positive and strictly increase in canonical release order.
class EventValidator {
public:
    explicit EventValidator(ValidationProfile profile = ValidationProfile::LegacyV1);
    ~EventValidator();
    EventValidator(EventValidator&&) noexcept;
    EventValidator& operator=(EventValidator&&) noexcept;
    EventValidator(const EventValidator&);
    EventValidator& operator=(const EventValidator&);
    void append(const Event& event);
    void finish();
    std::size_t retained_history() const noexcept;
    std::size_t source_count() const noexcept;
    ValidatorStats stats() const noexcept;
    ValidationProfile profile() const noexcept;
    std::optional<Event> retained_event(const EventId& event_id) const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace aismixer::lab::runtime
