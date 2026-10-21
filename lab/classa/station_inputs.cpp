#include "station_inputs.h"

#include <limits>
#include <stdexcept>
#include <utility>

namespace aismixer::lab::classa {

// Local opaque capability identity: no global counter, registry or references
// into caller-owned W1 vectors. Historical tokens retain only this inert value.
struct InputTokenIdentity {};

namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::invalid_argument(message);
}
std::uint64_t next_revision(std::uint64_t value) {
    if (value == std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("Class A input qualification revision overflow");
    return value + 1;
}
bool same_anchor(const runtime::UtcAnchor& a, const runtime::UtcAnchor& b) {
    return a.posix_seconds == b.posix_seconds && a.subsecond_units == b.subsecond_units;
}
void fresh(runtime::SimulationInstant generated, runtime::SimulationInstant observed,
           std::int64_t maximum_age, const char* diagnostic) {
    require(generated <= observed, diagnostic);
    require(runtime::difference_units(observed, generated) <= maximum_age, diagnostic);
}
// Coordinates are already bounded before multiplication. 180e9*3 fits int64.
std::int64_t coordinate(std::int64_t nanodegrees) {
    const auto numerator = nanodegrees * 3;
    const auto magnitude = numerator < 0 ? -numerator : numerator;
    const auto rounded = magnitude / 5000 + (magnitude % 5000 >= 2500 ? 1 : 0);
    return numerator < 0 ? -rounded : rounded;
}
runtime::UtcSlot exact_slot(runtime::SimulationInstant instant, const runtime::UtcAnchor& anchor) {
    const auto result = runtime::decompose_utc(instant, anchor);
    // Refuse an overflowed/unrepresentable mapping in either direction; do not
    // introduce an alternate clock representation to rescue it.
    require(runtime::compose_utc(result, anchor) == instant,
            "Class A direct UTC: exact compose/decompose roundtrip failed");
    return result;
}
}  // namespace

std::int64_t quantize_longitude(std::int64_t nanodegrees) {
    require(nanodegrees >= -180 * world::kDegree && nanodegrees <= 180 * world::kDegree,
            "Class A longitude: W1 nanodegrees outside [-180,180] degrees");
    return coordinate(nanodegrees);
}
std::int64_t quantize_latitude(std::int64_t nanodegrees) {
    require(nanodegrees >= -world::kGeodesicMaxLatitude && nanodegrees <= world::kGeodesicMaxLatitude,
            "Class A latitude: W1 nanodegrees outside [-80,80] degrees");
    return coordinate(nanodegrees);
}
std::int64_t quantize_speed(std::optional<std::int64_t> milli_knots) {
    if (!milli_knots) return 1023;
    require(*milli_knots >= 0, "Class A speed: negative milli-knots");
    // Test the saturation threshold before adding the half-step, so INT64_MAX
    // is safe. 102150 mkn rounds to the explicit high-speed code 1022.
    if (*milli_knots >= 102150) return 1022;
    return *milli_knots / 100 + (*milli_knots % 100 >= 50 ? 1 : 0);
}
std::int64_t quantize_course(std::optional<std::int64_t> nanodegrees) {
    if (!nanodegrees) return 3600;
    require(*nanodegrees >= 0 && *nanodegrees < 360 * world::kDegree,
            "Class A COG: nanodegrees must be in [0,360) degrees or absent");
    const auto result = *nanodegrees / 100000000 + (*nanodegrees % 100000000 >= 50000000 ? 1 : 0);
    return result == 3600 ? 0 : result;
}

StationInputGate::StationInputGate(OwnStationConfig config)
    : config_(std::move(config)), identity_(std::make_shared<const InputTokenIdentity>()) {}

void StationInputGate::check_owner(const OwnIdentity& identity) const {
    require(identity == config_.identity(), "Class A input: foreign vessel/equipment/radio owner");
}
void StationInputGate::check_time(runtime::SimulationInstant now) const {
    require(!latest_time_ || now >= *latest_time_, "Class A input: reversed observation/request time");
}

void StationInputGate::apply_power(const OwnPowerNotice& notice) {
    check_owner(notice.own_identity);
    check_time(notice.time);
    switch (notice.kind) {
    case OwnPowerKind::InitialOff:
        require(!power_initialized_ && notice.power_generation == 0,
                "Class A power: InitialOff is only the generation-zero bootstrap notice");
        break;
    case OwnPowerKind::On:
        require(!on_, "Class A power: duplicate ON without an OFF");
        require(notice.power_generation > generation_,
                "Class A power: ON must establish a strictly newer nonzero generation");
        require(!last_off_at_ || notice.time != *last_off_at_,
                "Class A power: OFF dominates reactivation at the same instant");
        break;
    case OwnPowerKind::Off:
        // An authoritative OFF is safe even if the matching ON has not been
        // delivered yet. Latch it first so dispatch order at a boundary cannot
        // permit a subsequent equal-instant ON to qualify new work.
        require(notice.power_generation >= generation_,
                "Class A power: OFF has stale generation");
        break;
    default:
        throw std::invalid_argument("Class A power: unsupported own notice kind");
    }
    const auto epoch = next_revision(epoch_);
    const auto clock_revision = next_revision(clock_revision_);
    const auto navigation_revision = next_revision(navigation_revision_);
    power_initialized_ = true;
    on_ = notice.kind == OwnPowerKind::On;
    generation_ = notice.power_generation;
    epoch_ = epoch;
    clock_revision_ = clock_revision;
    navigation_revision_ = navigation_revision;
    clock_valid_ = false;
    clock_acquired_at_.reset();
    latest_clock_sample_.reset();
    latest_navigation_sample_.reset();
    latest_time_ = notice.time;
    power_effective_at_ = notice.time;
    if (on_) epoch_started_at_ = notice.time;
    else epoch_started_at_.reset();
    if (notice.kind == OwnPowerKind::Off) last_off_at_ = notice.time;
}

std::optional<DirectUtcToken> StationInputGate::accept_clock(
    const DirectUtcObservation& observation, runtime::SimulationInstant observed_at) {
    check_owner(observation.own_identity);
    check_time(observed_at);
    require(power_initialized_ && on_, "Class A direct UTC: own equipment is OFF/uninitialized");
    require(observation.power_generation == generation_, "Class A direct UTC: stale power generation");
    require(observation.source_id == config_.clock().source_id,
            "Class A direct UTC: absent/foreign independent source ID");
    require(observation.category == "IDEAL_DIRECT_UTC_SIM",
            "Class A direct UTC: indirect/no-UTC category is unsupported");
    require(observation.anchor_revision == config_.clock().anchor_revision,
            "Class A direct UTC: inconsistent anchor revision");
    runtime::validate_utc_anchor(observation.anchor);
    require(same_anchor(observation.anchor, config_.clock().anchor),
            "Class A direct UTC: anchor differs from own validated reference");
    require(observation.sampled_at >= *epoch_started_at_,
            "Class A direct UTC: sample predates own ON epoch");
    require(!latest_clock_sample_ || observation.sampled_at >= *latest_clock_sample_,
            "Class A direct UTC: reversed source sample time");
    fresh(observation.sampled_at, observed_at, config_.input_policy().max_clock_age_units,
          "Class A direct UTC: future/stale sample");
    const auto revision = next_revision(clock_revision_);
    if (!observation.valid) {
        clock_revision_ = revision;
        clock_valid_ = false;
        clock_acquired_at_.reset();
        latest_clock_sample_ = observation.sampled_at;
        latest_time_ = observed_at;
        return std::nullopt;
    }
    const auto sampled_slot = exact_slot(observation.sampled_at, observation.anchor);
    // A gap beyond the configured freshness window is loss, even if no
    // separate invalid observation was dispatched. Reacquisition starts now.
    bool continuous = clock_valid_ && clock_acquired_at_.has_value();
    if (continuous && latest_clock_sample_) {
        continuous = runtime::difference_units(observation.sampled_at, *latest_clock_sample_)
                     <= config_.input_policy().max_clock_age_units;
    }
    const auto acquired = continuous ? *clock_acquired_at_ : observation.sampled_at;
    DirectUtcToken token;
    token.identity_ = config_.identity();
    token.source_id_ = observation.source_id;
    token.generation_ = generation_;
    token.epoch_ = epoch_;
    token.revision_ = revision;
    token.anchor_revision_ = observation.anchor_revision;
    token.sampled_at_ = observation.sampled_at;
    token.observed_at_ = observed_at;
    token.acquired_at_ = acquired;
    token.sampled_slot_ = sampled_slot;
    token.anchor_ = observation.anchor;
    token.gate_identity_ = identity_;
    clock_revision_ = revision;
    clock_valid_ = true;
    clock_acquired_at_ = acquired;
    latest_clock_sample_ = observation.sampled_at;
    latest_time_ = observed_at;
    return token;
}

OwnNavigationInput StationInputGate::adapt_navigation(const world::VesselSnapshot& snapshot,
    const EpfsFix& fix, runtime::SimulationInstant observed_at) {
    check_owner(fix.own_identity);
    check_owner({snapshot.state.id, snapshot.state.equipment_id, snapshot.state.radio_id});
    check_time(observed_at);
    require(power_initialized_, "Class A navigation: own power authority is uninitialized");
    require(snapshot.state.power_generation == generation_ && fix.power_generation == generation_,
            "Class A navigation: stale power generation");
    require(snapshot.state.equipment_on == on_,
            "Class A navigation: snapshot power disagrees with authoritative own notice");
    require(fix.world_revision == config_.world_revision(), "Class A EPFS: wrong world revision");
    require(fix.source_id == config_.input_policy().epfs_source_id,
            "Class A EPFS: absent/foreign own source ID");
    require(fix.clock_source_id == config_.clock().source_id,
            "Class A EPFS: foreign independent clock source ID");
    require(fix.generated_at == snapshot.time, "Class A EPFS: fix generation is not own snapshot time");
    require(snapshot.time >= *power_effective_at_,
            "Class A navigation: snapshot predates own power notice");
    require(!latest_navigation_sample_ || snapshot.time >= *latest_navigation_sample_,
            "Class A navigation: reversed own source sample time");
    fresh(snapshot.time, observed_at, config_.input_policy().max_navigation_age_units,
          "Class A navigation: future/stale own snapshot");
    world::validate_point(snapshot.location);
    require(snapshot.state.speed_mkn >= 0 && snapshot.state.speed_mkn <= 100000,
            "Class A navigation: W1 speed outside qualified 0..100000 milli-knots");
    const auto status = static_cast<std::uint8_t>(snapshot.state.navigation_status);
    require(status == 0 || status == 1 || status == 3,
            "Class A navigation: unsupported W1 navigation status");
    require(snapshot.state.course_valid == snapshot.course_ndeg.has_value(),
            "Class A navigation: W1 course validity flag disagrees with optional course");
    // Validate optional course even with an invalid EPFS flag: invalid source
    // content is an error, not a way to smuggle a plausible unavailable fix.
    const auto course = quantize_course(snapshot.course_ndeg);
    const auto longitude = quantize_longitude(snapshot.location.longitude_ndeg);
    const auto latitude = quantize_latitude(snapshot.location.latitude_ndeg);
    const auto revision = next_revision(navigation_revision_);
    OwnNavigationInput input;
    input.identity_ = config_.identity();
    input.fields_.navigation_status = status;
    if (fix.valid) {
        input.fields_.longitude = longitude;
        input.fields_.latitude = latitude;
        input.fields_.sog_tenths_knots = quantize_speed(snapshot.state.speed_mkn);
        input.fields_.cog_tenths_degrees = course;
    }
    input.provenance_.epfs_source_id = fix.source_id;
    input.provenance_.clock_source_id = fix.clock_source_id;
    if (!fix.valid) input.provenance_.position_and_motion = "POLICY_UNAVAILABLE_EXPLICIT_INVALID_EPFS";
    input.generated_at_ = fix.generated_at;
    input.observed_at_ = observed_at;
    input.generation_ = generation_;
    input.epoch_ = epoch_;
    input.revision_ = revision;
    input.world_revision_ = fix.world_revision;
    input.fix_valid_ = fix.valid;
    input.equipment_on_ = on_;
    input.gate_identity_ = identity_;
    navigation_revision_ = revision;
    latest_navigation_sample_ = snapshot.time;
    latest_time_ = observed_at;
    return input;
}

void StationInputGate::check_clock(const DirectUtcToken& token, runtime::SimulationInstant now) const {
    check_time(now);
    require(on_ && power_initialized_, "Class A direct UTC: own equipment is OFF/uninitialized");
    check_owner(token.identity_);
    require(token.gate_identity_ == identity_, "Class A direct UTC: token belongs to another station gate");
    require(token.generation_ == generation_ && token.epoch_ == epoch_,
            "Class A direct UTC: historical power epoch token");
    require(clock_valid_ && token.revision_ == clock_revision_,
            "Class A direct UTC: revoked/superseded clock qualification");
    require(token.source_id_ == config_.clock().source_id &&
            token.anchor_revision_ == config_.clock().anchor_revision &&
            same_anchor(token.anchor_, config_.clock().anchor),
            "Class A direct UTC: source/reference mismatch");
    require(token.observed_at_ <= now, "Class A direct UTC: future observation");
    fresh(token.sampled_at_, now, config_.input_policy().max_clock_age_units,
          "Class A direct UTC: future/stale qualified clock");
}
void StationInputGate::check_navigation(const OwnNavigationInput& input,
                                       runtime::SimulationInstant now) const {
    check_time(now);
    check_owner(input.identity_);
    require(input.gate_identity_ == identity_, "Class A navigation: token belongs to another station gate");
    require(input.generation_ == generation_ && input.epoch_ == epoch_,
            "Class A navigation: historical power epoch token");
    require(input.revision_ == navigation_revision_, "Class A navigation: superseded own sample");
    require(input.world_revision_ == config_.world_revision(), "Class A navigation: wrong world revision");
    require(input.observed_at_ <= now, "Class A navigation: future observation");
    fresh(input.generated_at_, now, config_.input_policy().max_navigation_age_units,
          "Class A navigation: future/stale qualified fix");
    require(input.fix_valid_, "Class A navigation: explicitly invalid EPFS fix");
    require(on_ && input.equipment_on_, "Class A navigation: own equipment OFF; location is not eligible input");
}

runtime::UtcSlot StationInputGate::current_utc(const DirectUtcToken& token,
                                             runtime::SimulationInstant now) {
    check_clock(token, now);
    const auto result = exact_slot(now, token.anchor_);
    latest_time_ = now;
    return result;
}
std::int64_t StationInputGate::fix_utc_second(const DirectUtcToken& token,
    runtime::SimulationInstant fix_generated_at, runtime::SimulationInstant now) {
    check_clock(token, now);
    require(fix_generated_at >= token.acquired_at_ && fix_generated_at >= *epoch_started_at_,
            "Class A fix UTC: fix predates direct-clock validity/own ON epoch");
    require(fix_generated_at <= now, "Class A fix UTC: future fix generation");
    const auto slot = exact_slot(fix_generated_at, token.anchor_);
    const auto units = static_cast<std::int64_t>(slot.slot) * runtime::kUnitsPerSlot + slot.units_into_slot;
    const auto second = units / runtime::kUnitsPerSecond;
    require(second >= 0 && second <= 59, "Class A fix UTC: impossible UTC second");
    latest_time_ = now;
    return second;
}
QualifiedOwnInputs StationInputGate::qualify_inputs(const OwnNavigationInput& navigation,
    const DirectUtcToken& clock, runtime::SimulationInstant now) {
    check_navigation(navigation, now);
    check_clock(clock, now);
    const auto slot = exact_slot(now, clock.anchor_);
    const auto second = fix_utc_second(clock, navigation.generated_at_, now);
    QualifiedOwnInputs input(navigation, clock);
    input.fields_ = navigation.fields_;
    input.fields_.utc_second = second;
    input.provenance_ = navigation.provenance_;
    input.provenance_.utc_second = "OWN_INDEPENDENT_IDEAL_DIRECT_UTC_AT_FIX_GENERATION";
    input.current_slot_ = slot;
    input.qualified_at_ = now;
    latest_time_ = now;
    return input;
}
QualifiedOwnInputs StationInputGate::revalidate(const QualifiedOwnInputs& previous,
    runtime::SimulationInstant now) {
    return qualify_inputs(previous.navigation_, previous.clock_, now);
}

}  // namespace aismixer::lab::classa
