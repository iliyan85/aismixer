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
    ReceiverNmea = 15, OutputError = 16
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
struct TransmissionStart {
    TransmissionId transmission_id;
    std::string station_id;
    std::uint8_t channel = 0;
    SimulationInstant end_time;
    std::string payload_reference;  // Immutable external payload identity, not NMEA.
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
using EventPayload = std::variant<StationLifecycle, ReceiverLifecycle, SlotMapChange,
    TdmaDecision, TransmissionStart, TransmissionEnd, RfObservation,
    ReceptionResult, ReceiverNmea, OutputError>;
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
// 4 simultaneous TX starts; 5 NMEA/output. Same-phase events cannot be causes
// of one another at the same instant, regardless of their display sort order.
std::uint8_t causal_phase(EventKind kind);
struct CanonicalLess { bool operator()(const Event& a, const Event& b) const; };
std::optional<ReceiverId> event_receiver(const Event& event);
void validate_identity(const std::string& identity, const char* field);
void validate_event(const Event& event);  // Shape, ranges and exact NMEA framing.
void validate_receiver_nmea(const ReceiverNmea& output);

// Streaming bounded validator. References older than the 4096-event retained
// window explicitly fail. Source sequences are positive and strictly increase.
// Retained TX/reception and lifecycle catalogs have explicit 4096-entry bounds;
// callers must partition runs rather than silently discard identity evidence.
class EventValidator {
public:
    EventValidator();
    ~EventValidator();
    EventValidator(EventValidator&&) noexcept;
    EventValidator& operator=(EventValidator&&) noexcept;
    EventValidator(const EventValidator&);
    EventValidator& operator=(const EventValidator&);
    void append(const Event& event);
    void finish();
    std::size_t retained_history() const noexcept;
    std::size_t source_count() const noexcept;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace aismixer::lab::runtime
