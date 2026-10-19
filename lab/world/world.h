#pragma once

#include "geodesy.h"
#include "cuda_engine.h"
#include "simulation_time.h"
#include "rf_contract.h"
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace aismixer::lab::world {
using runtime::SimulationInstant;
template<class Tag> struct Id { std::uint32_t value = 0; };
template<class T> constexpr bool operator==(Id<T> a, Id<T> b) { return a.value == b.value; }
template<class T> constexpr bool operator!=(Id<T> a, Id<T> b) { return !(a == b); }
template<class T> constexpr bool operator<(Id<T> a, Id<T> b) { return a.value < b.value; }
struct VesselTag; struct EquipmentTag; struct RadioTag; struct WaypointTag; struct RegionTag;
using VesselId = Id<VesselTag>; using EquipmentId = Id<EquipmentTag>;
using RadioId = Id<RadioTag>; using WaypointId = Id<WaypointTag>; using RegionId = Id<RegionTag>;
inline constexpr std::uint32_t kWorldSchemaVersion = 1;
inline constexpr std::size_t kMaxVessels = 1024, kMaxWaypoints = 256, kMaxRegions = 64, kMaxPassiveReceivers = 4096;
inline constexpr std::int64_t kMaxScenarioSeconds = 604800;
inline constexpr std::uint64_t kMaxTransitions = 2000000;
inline constexpr std::uint64_t kDistanceDenominator = UINT64_C(900) * runtime::kUnitsPerSecond;
struct Interval { std::int64_t minimum = 0, maximum = 0; };
struct PowerModel {
    Interval initial_delay_seconds{0,0};
    Interval online_duration_seconds{100,1000};
    Interval offline_duration_seconds{10,300};
};
enum class SelectionMode : std::uint8_t { Fixed = 1, All = 2, Subset = 3 };
struct Selection {
    SelectionMode mode = SelectionMode::All;
    WaypointId waypoint_id;
    std::vector<WaypointId> waypoint_ids;
};
struct Waypoint { WaypointId id; std::string name; Point location; };
enum class NavigationStatus : std::uint8_t { UnderWayUsingEngine = 0, AtAnchor = 1, RestrictedManoeuvrability = 3 };
struct NavigationEpisode {
    NavigationStatus status = NavigationStatus::UnderWayUsingEngine;
    std::uint32_t weight = 1;
    Interval duration_seconds{100,200};
};
enum class HeightDatum : std::uint8_t { Unresolved = 0, Amsl = 1, Wgs84Ellipsoid = 2 };
struct Vessel {
    VesselId id; EquipmentId equipment_id; RadioId radio_id;
    std::string name;
    Selection initial, first_destination, subsequent_destination;
    Interval speed_mkn{10000,20000}, speed_hold_seconds{100,200};
    std::vector<NavigationEpisode> navigation{{}};
    std::int64_t restricted_speed_cap_mkn = 5000;
    PowerModel power;
    std::int64_t antenna_offset_mm = 18000;
    std::optional<std::int64_t> waterline_ellipsoid_mm;
    std::string waterline_provenance = "unresolved sea-surface datum; no geoid data";
    Interval reach_radius_mm{10000000,60000000}, reach_hold_seconds{100,200};
};
struct Region {
    RegionId id; std::string name; Point center;
    std::int64_t radius_mm = 0;
    Interval count{1,1}, elevation_mm{10000,300000};
    HeightDatum height_datum = HeightDatum::Amsl;
    std::string provenance;
    PowerModel power;
};
struct Config {
    std::uint32_t schema_version = kWorldSchemaVersion;
    std::string profile_id, provenance;
    SimulationInstant duration;
    bool fresh_seed = false;
    std::uint64_t effective_seed = 42;
    std::uint64_t scenario_hash = 0, waypoint_hash = 0, regions_hash = 0;
    std::vector<Waypoint> waypoints;
    std::vector<Vessel> vessels;
    std::vector<Region> regions;
};
// CUDA-free validation, including every eligible waypoint pair's bounded
// geodesic solvability. Returns a sorted value copy; no caller-owned mutation.
Config canonical_config(Config config);
// Consume an explicit fresh request exactly once and retain the actual seed.
// Lower-level deterministic APIs reject unresolved fresh requests.
Config resolve_seed(Config config);
std::uint64_t fresh_seed();
std::uint64_t passive_equipment_key(RegionId region, std::uint32_t ordinal);
std::string station_name(VesselId id);
std::string radio_name(RadioId id);
std::string passive_radio_name(RegionId id, std::uint32_t ordinal);

enum class TransitionKind : std::uint8_t {
    WaypointArrival = 0, SpeedChanged = 1, NavigationChanged = 2,
    ReachChanged = 3, InitialOff = 4, EquipmentOn = 5, EquipmentOff = 6
};
struct Transition {
    SimulationInstant time; TransitionKind kind;
    std::uint64_t entity_id = 0, episode = 0;
    std::int64_t value = 0;
    // These are integration requirements, never radio output events.
    bool requires_protocol_initialization = false;
    bool requires_abort_of_overlapping_burst = false;
};
using TransitionSink = std::function<void(const Transition&)>;
struct VesselState {
    VesselId id; EquipmentId equipment_id; RadioId radio_id;
    WaypointId leg_from, destination;
    std::uint64_t leg_index = 0;
    std::int64_t leg_length_mm = 0, leg_distance_mm = 0;
    std::uint64_t distance_remainder = 0;
    std::uint64_t total_distance_mm = 0, total_distance_remainder = 0;
    PositionQuery position_query;
    std::int64_t nominal_speed_mkn = 0, speed_mkn = 0;
    NavigationStatus navigation_status = NavigationStatus::UnderWayUsingEngine;
    bool course_valid = false, equipment_on = false;
    std::uint64_t power_generation = 0, environment_revision = 0;
    std::int64_t antenna_offset_mm = 0;
    std::optional<std::int64_t> antenna_ellipsoid_mm;
    std::int64_t effective_reach_radius_mm = 0;
};
struct PassiveState {
    RegionId region_id; std::uint32_t ordinal = 0;
    Point location; std::int64_t elevation_mm = 0;
    HeightDatum height_datum = HeightDatum::Unresolved;
    std::string provenance;
    bool equipment_on = false;
    std::uint64_t power_generation = 0;
    SimulationInstant snapshot_time;
};
struct PreparedSnapshot {
    SimulationInstant time;
    std::vector<VesselState> vessels;
    std::vector<PassiveState> receivers;
};
struct VesselSnapshot { VesselState state; Point location; std::optional<std::int64_t> course_ndeg; SimulationInstant time; };
struct Snapshot {
    SimulationInstant time;
    std::vector<VesselSnapshot> vessels;
    std::vector<PassiveState> receivers;
};
struct TimelineMetrics {
    std::uint64_t transitions = 0, integration_intervals = 0;
    std::size_t vessels = 0, receivers = 0, peak_pending_notices = 0;
    std::size_t owned_state_bytes = 0; // descriptors/state only, not process RSS
};
class Timeline {
public:
    // RegionSample positions must come from the CUDA sampler in production.
    // An explicitly supplied sample list supports CUDA-free contract tests.
    Timeline(Config config, std::vector<RegionSample> receiver_samples = {});
    ~Timeline();
    Timeline(Timeline&&) noexcept; Timeline& operator=(Timeline&&) noexcept;
    Timeline(const Timeline&) = delete; Timeline& operator=(const Timeline&) = delete;
    void advance(SimulationInstant target, const TransitionSink& sink = {});
    PreparedSnapshot prepare() const;
    std::optional<SimulationInstant> next_transition_time() const;
    const Config& config() const;
    const TimelineMetrics& metrics() const;
private:
    struct Impl; std::unique_ptr<Impl> impl_;
};
// Reference-only host materialization is explicit and never used as an
// unavailable-GPU fallback by the operator or production WorldSession.
Snapshot reference_snapshot(const PreparedSnapshot& prepared);
Snapshot materialize_snapshot(const PreparedSnapshot& prepared, const std::vector<PositionResult>& positions);
std::vector<RegionSpec> region_specs(const Config& config);
class WorldSession {
public:
    WorldSession(Config config, Options options = {});
    void advance(SimulationInstant target, const TransitionSink& sink = {});
    Snapshot snapshot();
    Timeline& timeline();
    Session& cuda();
private:
    WorldSession(Config selected_config, Options options, bool selected);
    std::size_t capacity_; Session cuda_; Timeline timeline_;
};
// Immutable R0.1 ownership view at a particular world instant. A live radio
// integration must handle power cuts/epochs; this does not mutate old tokens.
std::shared_ptr<const runtime::ReceiverRegistry> receiver_ownership(const Snapshot& snapshot);
struct AntennaGeometry { Point location; std::int64_t ellipsoid_height_mm; SimulationInstant time; };
AntennaGeometry rf_geometry(const VesselSnapshot& vessel, SimulationInstant time);
AntennaGeometry rf_geometry(const PassiveState& receiver, SimulationInstant time);
AntennaGeometry rf_geometry(const Snapshot& snapshot, VesselId vessel);
struct BurstPowerConstraint {
    bool may_start = false;
    std::optional<SimulationInstant> first_off_before_end;
};
// Pure world constraint: no runtime TX end, decoded packet or frame is minted.
BurstPowerConstraint inspect_burst(const Config& config, std::uint64_t equipment_key,
                                  SimulationInstant start, SimulationInstant end);
const char* transition_name(TransitionKind kind);
}  // namespace aismixer::lab::world
