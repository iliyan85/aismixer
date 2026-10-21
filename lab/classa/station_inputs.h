#pragma once

#include "station_config.h"

#include <cstdint>
#include <memory>
#include <optional>
#include <string>

namespace aismixer::lab::classa {

// OWN-INPUT-QUANTIZATION-v1 is SIMULATION_POLICY, not an ITU rounding rule.
// Exact nearest integer; half ties go away from zero. Invalid values throw.
std::int64_t quantize_longitude(std::int64_t nanodegrees);
std::int64_t quantize_latitude(std::int64_t nanodegrees);
// This scalar helper accepts all nonnegative int64 speeds to expose the AIS
// saturation boundary. The W1 snapshot adapter separately enforces <=100 kn.
std::int64_t quantize_speed(std::optional<std::int64_t> milli_knots);
std::int64_t quantize_course(std::optional<std::int64_t> nanodegrees);

enum class OwnPowerKind { InitialOff, On, Off };
struct OwnPowerNotice {
    OwnIdentity own_identity;
    runtime::SimulationInstant time;
    OwnPowerKind kind;
    std::uint64_t power_generation = 0;
};

// Explicit independently supplied source observation. A SimulationInstant
// alone is never evidence of UTC. Anchor/revision must match this station.
struct DirectUtcObservation {
    OwnIdentity own_identity;
    std::uint64_t power_generation = 0;
    std::string source_id;
    std::string category = "IDEAL_DIRECT_UTC_SIM";
    runtime::UtcAnchor anchor;
    std::uint64_t anchor_revision = 0;
    runtime::SimulationInstant sampled_at;
    bool valid = false;
};

// Explicit truth-fed simulation EPFS authorization, not a physical GNSS fix.
// generated_at must equal the single materialized W1 snapshot's own time.
struct EpfsFix {
    OwnIdentity own_identity;
    std::uint64_t power_generation = 0;
    std::string source_id;
    std::string clock_source_id;
    std::uint64_t world_revision = 0;
    runtime::SimulationInstant generated_at;
    bool valid = false;
};

struct OwnNavigationFields {
    std::int64_t longitude = ais::kLongitudeUnavailable;
    std::int64_t latitude = ais::kLatitudeUnavailable;
    std::int64_t sog_tenths_knots = 1023;
    std::int64_t cog_tenths_degrees = 3600;
    std::int64_t navigation_status = 15;
    std::int64_t true_heading_degrees = 511;
    std::int64_t rot_wire = -128;
    std::int64_t utc_second = 60;
    bool position_accuracy = false;
    bool raim = false;
};

struct OwnInputProvenance {
    std::string epfs_source_id;
    std::string clock_source_id;
    std::string position_and_motion = "OWN_W1_TRUTH_AS_EPFS";
    std::string navigation_status = "OWN_W1_NAVIGATION_STATUS";
    std::string heading_rot_integrity = "POLICY_UNAVAILABLE_NO_SENSOR_EVIDENCE";
    std::string utc_second = "POLICY_UNAVAILABLE_NO_QUALIFIED_FIX_UTC";
};

class StationInputGate;
struct InputTokenIdentity;

// Owned historical value; only a gate can mint it. A later loss/OFF does not
// mutate this record. New work must return to its gate for revalidation.
class DirectUtcToken {
public:
    const OwnIdentity& identity() const noexcept { return identity_; }
    const std::string& source_id() const noexcept { return source_id_; }
    std::uint64_t power_generation() const noexcept { return generation_; }
    runtime::SimulationInstant sampled_at() const noexcept { return sampled_at_; }
    runtime::SimulationInstant observed_at() const noexcept { return observed_at_; }
    runtime::SimulationInstant acquired_at() const noexcept { return acquired_at_; }
    const runtime::UtcSlot& sampled_slot() const noexcept { return sampled_slot_; }
    const runtime::UtcAnchor& anchor() const noexcept { return anchor_; }
    std::uint64_t anchor_revision() const noexcept { return anchor_revision_; }
private:
    friend class StationInputGate;
    DirectUtcToken() = default;
    OwnIdentity identity_;
    std::string source_id_;
    std::uint64_t generation_ = 0, epoch_ = 0, revision_ = 0;
    std::uint64_t anchor_revision_ = 0;
    runtime::SimulationInstant sampled_at_, observed_at_, acquired_at_;
    runtime::UtcSlot sampled_slot_;
    runtime::UtcAnchor anchor_;
    std::shared_ptr<const InputTokenIdentity> gate_identity_;
};

class OwnNavigationInput {
public:
    const OwnIdentity& identity() const noexcept { return identity_; }
    const OwnNavigationFields& fields() const noexcept { return fields_; }
    const OwnInputProvenance& provenance() const noexcept { return provenance_; }
    runtime::SimulationInstant fix_generated_at() const noexcept { return generated_at_; }
    runtime::SimulationInstant observed_at() const noexcept { return observed_at_; }
    std::uint64_t power_generation() const noexcept { return generation_; }
    std::uint64_t world_revision() const noexcept { return world_revision_; }
    bool fix_valid() const noexcept { return fix_valid_; }
    bool equipment_on() const noexcept { return equipment_on_; }
    // Navigation is useful while OFF or without UTC; it is not a qualified
    // input envelope until qualify_inputs checks the independent clock too.
    bool input_eligible() const noexcept { return false; }
    bool ready_for_tx() const noexcept { return false; }
private:
    friend class StationInputGate;
    OwnNavigationInput() = default;
    OwnIdentity identity_;
    OwnNavigationFields fields_;
    OwnInputProvenance provenance_;
    runtime::SimulationInstant generated_at_, observed_at_;
    std::uint64_t generation_ = 0, epoch_ = 0, revision_ = 0, world_revision_ = 0;
    bool fix_valid_ = false, equipment_on_ = false;
    std::shared_ptr<const InputTokenIdentity> gate_identity_;
};

class QualifiedOwnInputs {
public:
    const OwnNavigationFields& fields() const noexcept { return fields_; }
    const OwnInputProvenance& provenance() const noexcept { return provenance_; }
    const OwnNavigationInput& navigation() const noexcept { return navigation_; }
    const DirectUtcToken& clock() const noexcept { return clock_; }
    const runtime::UtcSlot& current_slot() const noexcept { return current_slot_; }
    runtime::SimulationInstant qualified_at() const noexcept { return qualified_at_; }
    ais::SyncState sync_state() const noexcept { return ais::SyncState::UtcDirect; }
    bool input_eligible() const noexcept { return true; }
    // S2 has no passive-observation/entry FSM, maps, radio or transmitter.
    bool ready_for_tx() const noexcept { return false; }
private:
    friend class StationInputGate;
    QualifiedOwnInputs(OwnNavigationInput navigation, DirectUtcToken clock)
        : navigation_(std::move(navigation)), clock_(std::move(clock)) {}
    OwnNavigationInput navigation_;
    DirectUtcToken clock_;
    OwnNavigationFields fields_;
    OwnInputProvenance provenance_;
    runtime::UtcSlot current_slot_;
    runtime::SimulationInstant qualified_at_;
};

// OWN_POWER_GENERATION_EPOCH-v1: own lifecycle validity gate, not a TDMA FSM.
// No global world/config/registry or peer metadata enters these methods.
// InitialOff is W1 bootstrap; actual Off dominates On/new work at equal time.
// Callers dispatch authoritative own notices before requesting new work; if an
// OFF arrives at that same instant, revalidation rejects previously minted work.
class StationInputGate {
public:
    explicit StationInputGate(OwnStationConfig config);
    StationInputGate(const StationInputGate&) = delete;
    StationInputGate& operator=(const StationInputGate&) = delete;
    StationInputGate(StationInputGate&&) = delete;
    StationInputGate& operator=(StationInputGate&&) = delete;

    const OwnStationConfig& config() const noexcept { return config_; }
    void apply_power(const OwnPowerNotice& notice);
    std::optional<DirectUtcToken> accept_clock(const DirectUtcObservation& observation,
                                               runtime::SimulationInstant observed_at);
    OwnNavigationInput adapt_navigation(const world::VesselSnapshot& snapshot,
                                         const EpfsFix& fix,
                                         runtime::SimulationInstant observed_at);
    runtime::UtcSlot current_utc(const DirectUtcToken& token,
                                  runtime::SimulationInstant now);
    std::int64_t fix_utc_second(const DirectUtcToken& token,
                                runtime::SimulationInstant fix_generated_at,
                                runtime::SimulationInstant now);
    QualifiedOwnInputs qualify_inputs(const OwnNavigationInput& navigation,
                                       const DirectUtcToken& clock,
                                       runtime::SimulationInstant now);
    QualifiedOwnInputs revalidate(const QualifiedOwnInputs& previous,
                                   runtime::SimulationInstant now);

    bool equipment_on() const noexcept { return on_; }
    std::uint64_t power_generation() const noexcept { return generation_; }
private:
    void check_owner(const OwnIdentity& identity) const;
    void check_time(runtime::SimulationInstant now) const;
    void check_clock(const DirectUtcToken& token, runtime::SimulationInstant now) const;
    void check_navigation(const OwnNavigationInput& input, runtime::SimulationInstant now) const;
    OwnStationConfig config_;
    std::shared_ptr<const InputTokenIdentity> identity_;
    std::optional<runtime::SimulationInstant> latest_time_, epoch_started_at_, last_off_at_, power_effective_at_;
    std::optional<runtime::SimulationInstant> clock_acquired_at_, latest_clock_sample_, latest_navigation_sample_;
    std::uint64_t generation_ = 0, epoch_ = 0, clock_revision_ = 0, navigation_revision_ = 0;
    bool power_initialized_ = false, on_ = false, clock_valid_ = false;
};

}  // namespace aismixer::lab::classa
