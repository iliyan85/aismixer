#include "world.h"
#include "rng.h"
#include "philox_reference.h"
#include "world_geodesic_reference.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <tuple>
#include <vector>

namespace {
namespace w = aismixer::lab::world;
namespace r = aismixer::lab::runtime;
namespace reference = aismixer::lab::test_reference;
namespace geo = reference::world_geodesic;
std::size_t invalid_count = 0;
void require(bool value, std::string_view reason) { if (!value) throw std::runtime_error(std::string(reason)); }
template<class Action> void invalid(Action action, std::string_view reason) {
    bool rejected = false;
    try { action(); } catch (const std::invalid_argument& error) {
        require(std::string_view(error.what()).size() > 5, "world contract error lacks context"); rejected = true;
    }
    require(rejected, reason); ++invalid_count;
}
w::Selection fixed(std::uint32_t id) { return {w::SelectionMode::Fixed, {id}, {}}; }
w::Selection subset(std::initializer_list<std::uint32_t> ids) {
    w::Selection result; result.mode = w::SelectionMode::Subset;
    for (auto id : ids) result.waypoint_ids.push_back({id}); return result;
}
w::Config base() {
    w::Config config; config.profile_id = "independent-host-world-v1";
    config.provenance = "SYNTHETIC WORLD CONTRACT TEST; NO AIS TDMA OR RF RECEPTION";
    config.duration = r::from_seconds(18000); config.effective_seed = 42;
    config.waypoints = {{{1}, "Varna illustrative settlement", {27910000000LL,43210000000LL}},
                        {{2}, "Istanbul illustrative settlement", {28960000000LL,41010000000LL}}};
    w::Vessel vessel; vessel.id = {11}; vessel.equipment_id = {21}; vessel.radio_id = {31}; vessel.name = "vessel A";
    vessel.initial = fixed(1); vessel.first_destination = fixed(2); vessel.subsequent_destination = subset({1,2});
    vessel.speed_mkn = {20000,20000}; vessel.speed_hold_seconds = {600,600};
    vessel.navigation = {{w::NavigationStatus::UnderWayUsingEngine,1,{600,600}}};
    vessel.power = {{30,30},{10,10},{10,10}};
    vessel.reach_radius_mm = {1000000,2000000}; vessel.reach_hold_seconds = {17,17};
    vessel.waterline_ellipsoid_mm = 500; vessel.waterline_provenance = "Explicit synthetic ellipsoid waterline assumption";
    config.vessels.push_back(vessel); return config;
}
std::uint32_t word(std::uint64_t seed, std::uint64_t entity, w::Purpose purpose,
                   std::uint64_t episode, std::uint32_t draw = 0) {
    const reference::PhiloxBlock counter{{static_cast<std::uint32_t>(episode), static_cast<std::uint32_t>(episode >> 32),
        draw / 4, UINT32_C(0x01000000) | static_cast<std::uint32_t>(purpose)}};
    const auto key = seed ^ entity;
    return reference::philox4x32_10(counter, {{static_cast<std::uint32_t>(key),static_cast<std::uint32_t>(key >> 32)}})[draw % 4];
}
std::uint64_t bounded(std::uint64_t seed, std::uint64_t entity, w::Purpose purpose,
                      std::uint64_t episode, std::uint64_t low, std::uint64_t high, unsigned* proposals = nullptr) {
    const auto width = high - low + 1;
    require(low <= high && width != 0, "independent inclusive interval invalid");
    const auto threshold = (std::uint64_t{0} - width) % width;
    for (unsigned i = 0; i < 32; ++i) {
        const auto candidate = (std::uint64_t{word(seed,entity,purpose,episode,i*2)} << 32) |
            word(seed,entity,purpose,episode,i*2+1);
        if (candidate >= threshold) { if (proposals) *proposals = i + 1; return low + candidate % width; }
    }
    throw std::runtime_error("independent proposal budget exhausted");
}
std::int64_t draw(std::uint64_t seed, std::uint64_t entity, w::Purpose purpose,
                  std::uint64_t episode, w::Interval interval) {
    return static_cast<std::int64_t>(bounded(seed,entity,purpose,episode,
        static_cast<std::uint64_t>(interval.minimum),static_cast<std::uint64_t>(interval.maximum)));
}
const w::VesselSnapshot& vessel(const w::Snapshot& snapshot, std::uint32_t id = 11) {
    const auto found = std::find_if(snapshot.vessels.begin(),snapshot.vessels.end(),[&](const auto& item){return item.state.id.value == id;});
    require(found != snapshot.vessels.end(), "snapshot missing stable vessel identity"); return *found;
}
w::Snapshot at(w::Timeline& timeline, r::SimulationInstant time, const w::TransitionSink& sink = {}) {
    timeline.advance(time,sink); return w::reference_snapshot(timeline.prepare());
}
void same_vessel(const w::VesselSnapshot& a, const w::VesselSnapshot& b, bool compare_power = true, bool compare_reach = true) {
    const auto& x = a.state; const auto& y = b.state;
    require(a.time == b.time && a.location == b.location && a.course_ndeg == b.course_ndeg && x.id == y.id &&
        x.equipment_id == y.equipment_id && x.radio_id == y.radio_id && x.leg_from == y.leg_from &&
        x.destination == y.destination && x.leg_index == y.leg_index && x.leg_length_mm == y.leg_length_mm &&
        x.leg_distance_mm == y.leg_distance_mm && x.distance_remainder == y.distance_remainder &&
        x.total_distance_mm == y.total_distance_mm && x.total_distance_remainder == y.total_distance_remainder &&
        x.nominal_speed_mkn == y.nominal_speed_mkn && x.speed_mkn == y.speed_mkn &&
        x.navigation_status == y.navigation_status && x.course_valid == y.course_valid &&
        x.antenna_offset_mm == y.antenna_offset_mm && x.antenna_ellipsoid_mm == y.antenna_ellipsoid_mm,
        "navigation state changed under independent-world perturbation");
    if (compare_power) require(x.equipment_on == y.equipment_on && x.power_generation == y.power_generation,
        "equipment lifecycle changed under unrelated perturbation");
    if (compare_reach) require(x.effective_reach_radius_mm == y.effective_reach_radius_mm && x.environment_revision == y.environment_revision,
        "environment schedule changed under unrelated perturbation");
}
void rng_contract() {
    for (std::uint64_t seed : {UINT64_C(0),UINT64_C(42),UINT64_MAX})
        for (std::uint64_t entity : {UINT64_C(1),UINT64_C(0x123456789abcdef0),UINT64_MAX})
            for (unsigned purpose = 1; purpose <= 16; ++purpose)
                for (std::uint64_t episode : {UINT64_C(0),UINT64_C(1),UINT64_C(0x123456789abcdef0)})
                    for (std::uint32_t draw_index : {0u,1u,3u,4u,31u,63u,UINT32_MAX})
                        require(w::rng_word(seed,entity,static_cast<w::Purpose>(purpose),episode,draw_index) ==
                            word(seed,entity,static_cast<w::Purpose>(purpose),episode,draw_index), "independent semantic-key Philox mismatch");
    bool low_seen = false, high_seen = false;
    for (std::uint64_t episode = 0; episode < 512; ++episode) {
        const auto expected = bounded(42,21,w::Purpose::EquipmentInitial,episode,0,10);
        require(w::sample_integer(42,21,w::Purpose::EquipmentInitial,episode,0,10) == static_cast<std::int64_t>(expected),
                "inclusive unbiased interval mismatch");
        low_seen |= expected == 0; high_seen |= expected == 10;
    }
    require(low_seen && high_seen, "inclusive interval did not exercise both extremes");
    const auto upper = UINT64_C(0x8000000000000000);
    bool rejected_first = false;
    for (std::uint64_t seed = 0; seed < 100 && !rejected_first; ++seed) {
        unsigned proposals = 0;
        const auto expected = bounded(seed,7,w::Purpose::SpeedValue,0,0,upper,&proposals);
        if (proposals > 1) { std::uint64_t actual = 0;
            require(w::rng_bounded(seed,7,w::Purpose::SpeedValue,0,0,upper,actual) && actual == expected,
                    "rejected Philox candidate was replaced with modulo-biased fallback"); rejected_first = true; }
    }
    require(rejected_first, "independent rejection-path fixture missing");
    std::uint64_t result = 0;
    require(!w::rng_bounded(1,1,w::Purpose::SpeedValue,0,2,1,result) &&
            !w::rng_bounded(1,1,w::Purpose::SpeedValue,0,0,UINT64_MAX,result), "invalid uint64 inclusive range accepted");
    invalid([]{(void)w::sample_integer(1,1,w::Purpose::SpeedValue,0,-1,1);}, "negative interval accepted");
    require(w::passive_equipment_key({UINT32_MAX},UINT32_MAX) == UINT64_MAX &&
            w::passive_equipment_key({1},1) > UINT32_MAX, "equipment/region identity key is not injective");
    invalid([]{(void)w::passive_equipment_key({0},1);}, "region zero collided with vessel equipment key");
    invalid([]{(void)w::passive_equipment_key({1},0);}, "passive ordinal zero accepted");
}
void constant_voyage_and_power() {
    const auto config = w::canonical_config(base());
    w::Timeline timeline(config); std::vector<w::Transition> notices;
    const auto collect = [&](const auto& notice){notices.push_back(notice);};
    const auto start = at(timeline,{0},collect);
    require(!vessel(start).state.equipment_on && vessel(start).state.power_generation == 0, "initial OFF preparation interval absent");
    std::set<std::int64_t> radii;
    for (const auto time : std::vector<r::SimulationInstant>{{1},{30*r::kUnitsPerSecond-1},r::from_seconds(30),
        {40*r::kUnitsPerSecond-1},r::from_seconds(40),r::from_seconds(50),r::from_seconds(3600),r::from_seconds(18000)}) {
        const auto snapshot = at(timeline,time,collect); const auto& ship = vessel(snapshot);
        const auto numerator = static_cast<std::uint64_t>(time.units) * UINT64_C(20000) * 463;
        require(ship.state.total_distance_mm == numerator / w::kDistanceDenominator &&
                ship.state.total_distance_remainder == numerator % w::kDistanceDenominator,
                "power transitions or query boundaries changed exact integrated speed distance");
        require(ship.state.antenna_offset_mm == 18000 && ship.state.antenna_ellipsoid_mm == 18500,
                "physical antenna mounting changed with world episodes");
        require(ship.state.speed_mkn == 20000 && ship.course_ndeg.has_value(), "OFF equipment stopped physical navigation");
        const auto expected = geo::direct(27.91,43.21,160.081995740407,
            static_cast<double>(numerator) / static_cast<double>(w::kDistanceDenominator) / 1000);
        require(geo::endpoint_error_m(ship.location.longitude_ndeg/1e9,ship.location.latitude_ndeg/1e9,expected) < 0.002,
                "world long-voyage position differs from independent ellipsoid ODE");
        if (time.units == 30*r::kUnitsPerSecond || time.units == 50*r::kUnitsPerSecond)
            require(ship.state.equipment_on, "ON not effective at inclusive boundary");
        if (time.units == 40*r::kUnitsPerSecond) require(!ship.state.equipment_on, "OFF not effective at exact boundary");
        radii.insert(ship.state.effective_reach_radius_mm);
    }
    require(radii.size() > 1, "environment schedule stopped while equipment OFF");
    require(std::any_of(notices.begin(),notices.end(),[](const auto& n){return n.kind == w::TransitionKind::InitialOff && n.entity_id == 21 && n.time.units == 0;}),
            "initial OFF not visibly distinguished from recurring OFF");
    require(std::any_of(notices.begin(),notices.end(),[](const auto& n){return n.kind == w::TransitionKind::EquipmentOff && n.time == r::from_seconds(40) && n.requires_abort_of_overlapping_burst;}),
            "power cut did not expose future partial-burst abort requirement");
    const auto burst = w::inspect_burst(config,21,r::from_seconds(35),r::from_seconds(45));
    require(burst.may_start && burst.first_off_before_end && *burst.first_off_before_end == r::from_seconds(40), "mid-burst power cut hidden");
    const auto exact_end = w::inspect_burst(config,21,r::from_seconds(35),r::from_seconds(40));
    require(exact_end.may_start && !exact_end.first_off_before_end, "half-open burst interval included its end boundary");
    require(!w::inspect_burst(config,21,r::from_seconds(40),r::from_seconds(45)).may_start, "OFF interface accepted new burst");
    require(timeline.metrics().transitions < 20000 && timeline.metrics().owned_state_bytes < 1000000,
            "five-hour host world retained an all-history event vector");
}
struct ExpectedPower { bool on; std::uint64_t generation; };
ExpectedPower power_at(std::uint64_t seed, std::uint64_t key, const w::PowerModel& power, std::int64_t seconds) {
    auto time = draw(seed,key,w::Purpose::EquipmentInitial,0,power.initial_delay_seconds);
    if (seconds < time) return {false,0};
    std::uint64_t episode = 0;
    for (;;) {
        ++episode;
        time += draw(seed,key,w::Purpose::EquipmentOnline,episode-1,power.online_duration_seconds);
        if (seconds < time) return {true,episode};
        time += draw(seed,key,w::Purpose::EquipmentOffline,episode-1,power.offline_duration_seconds);
        if (seconds < time) return {false,episode};
    }
}
void independent_episode_model() {
    auto config = base(); auto& model = config.vessels[0];
    model.power = {{0,10},{100,1000},{10,300}};
    model.speed_mkn = {10000,20000}; model.speed_hold_seconds = {10,10}; model.reach_hold_seconds = {10,10};
    model.navigation = {{w::NavigationStatus::UnderWayUsingEngine,1,{10,10}},
        {w::NavigationStatus::AtAnchor,1,{10,10}},{w::NavigationStatus::RestrictedManoeuvrability,1,{10,10}}};
    w::Timeline timeline(config);
    std::uint64_t numerator = 0; std::int64_t previous_speed = 0; bool anchored = false, restricted = false, moving = false;
    std::optional<w::Point> prior_anchor;
    for (std::uint64_t episode = 0; episode <= 120; ++episode) {
        if (episode) numerator += static_cast<std::uint64_t>(previous_speed) * 463 * 10 * r::kUnitsPerSecond;
        const auto seconds = static_cast<std::int64_t>(episode * 10);
        const auto snapshot = at(timeline,r::from_seconds(seconds)); const auto& ship = vessel(snapshot);
        const auto nominal = draw(42,11,w::Purpose::SpeedValue,episode,model.speed_mkn);
        const auto choice = bounded(42,11,w::Purpose::StatusChoice,episode,0,2);
        const std::array<w::NavigationStatus,3> statuses{{w::NavigationStatus::UnderWayUsingEngine,w::NavigationStatus::AtAnchor,w::NavigationStatus::RestrictedManoeuvrability}};
        const auto status = statuses[static_cast<std::size_t>(choice)];
        const auto effective = status == w::NavigationStatus::AtAnchor ? 0 :
            status == w::NavigationStatus::RestrictedManoeuvrability ? std::min(nominal,model.restricted_speed_cap_mkn) : nominal;
        require(ship.state.nominal_speed_mkn == nominal && ship.state.navigation_status == status && ship.state.speed_mkn == effective,
                "speed/status episode did not match independent semantic Philox draws");
        require(ship.state.total_distance_mm == numerator / w::kDistanceDenominator &&
                ship.state.total_distance_remainder == numerator % w::kDistanceDenominator,
                "piecewise speed/status integration lost exact rational distance");
        const auto power = power_at(42,21,model.power,seconds);
        require(ship.state.equipment_on == power.on && ship.state.power_generation == power.generation,
                "power schedule coupled to navigation or used wrong episode domain");
        require(ship.state.effective_reach_radius_mm == draw(42,21,w::Purpose::ReachValue,episode,model.reach_radius_mm),
                "reach radius did not use independent equipment/environment episode");
        if (status == w::NavigationStatus::AtAnchor) {
            anchored = true; require(!ship.course_ndeg && !ship.state.course_valid, "anchored vessel claimed meaningful course over ground");
            const auto held = at(timeline,r::add_units(r::from_seconds(seconds),1));
            require(vessel(held).location == ship.location && vessel(held).state.total_distance_mm == ship.state.total_distance_mm,
                    "anchor hold moved vessel or completed leg");
            prior_anchor = ship.location;
        } else {
            moving = true; if (status == w::NavigationStatus::RestrictedManoeuvrability) {
                restricted = true; require(effective <= 5000 && effective > 0, "restricted vessel only relabelled full speed");
            }
            require(ship.course_ndeg.has_value(), "moving vessel omitted COG");
        }
        previous_speed = effective;
    }
    require(anchored && restricted && moving && prior_anchor.has_value(), "fixed seed did not exercise required navigation modes");
}
void independent_entities_and_chunking() {
    auto original = base(); original.vessels[0].speed_mkn = {10000,20000};
    original.vessels[0].speed_hold_seconds = {13,27};
    original.vessels[0].navigation = {{w::NavigationStatus::UnderWayUsingEngine,2,{17,40}}, {w::NavigationStatus::AtAnchor,1,{11,19}},
                                    {w::NavigationStatus::RestrictedManoeuvrability,1,{31,57}}};
    original.vessels[0].power = {{0,10},{100,1000},{10,300}};
    auto extra = original; auto other = original.vessels[0]; other.id = {99}; other.equipment_id = {199}; other.radio_id = {299}; other.name = "unrelated vessel";
    extra.vessels.insert(extra.vessels.begin(),other); std::reverse(extra.waypoints.begin(),extra.waypoints.end());
    auto changed_power = original; changed_power.vessels[0].power = {{10000,10000},{1,1},{1,1}};
    auto changed_reach = original; changed_reach.vessels[0].reach_radius_mm = {0,0}; changed_reach.vessels[0].reach_hold_seconds = {1,1};
    w::Timeline direct(original), chunks(original), unrelated(extra), power_changed(changed_power), reach_changed(changed_reach);
    for (std::int64_t second = 0; second <= 18000; second += 300) {
        const auto time = r::from_seconds(second);
        const auto expected = at(direct,time);
        while (chunks.prepare().time < time) chunks.advance({std::min(time.units,chunks.prepare().time.units + 7*r::kUnitsPerSecond)});
        same_vessel(vessel(expected),vessel(w::reference_snapshot(chunks.prepare())));
        same_vessel(vessel(expected),vessel(at(unrelated,time)));
        same_vessel(vessel(expected),vessel(at(power_changed,time)),false,true);
        same_vessel(vessel(expected),vessel(at(reach_changed,time)),true,false);
    }
    invalid([&]{ direct.advance(r::from_seconds(17999)); }, "world clock regressed without error");
}
void passive_and_height_contract() {
    auto config = base(); w::Region region; region.id = {71}; region.name = "synthetic candidate region";
    region.center = {27910000000LL,43210000000LL}; region.radius_mm = 20000000; region.count = {1,1};
    region.elevation_mm = {10000,10000}; region.height_datum = w::HeightDatum::Amsl;
    region.provenance = "Unqualified synthetic candidate; no DEM, coastline or surveyed site";
    region.power = {{0,0},{10,10},{5,5}}; config.regions.push_back(region);
    const std::vector<w::RegionSample> samples{{71,1,region.center,10000}};
    w::Timeline timeline(config,samples), without_receiver(base());
    bool initial_passive_on = false;
    timeline.advance({0},[&](const auto& transition){
        if (transition.entity_id == w::passive_equipment_key({71},1) && transition.kind == w::TransitionKind::EquipmentOn) {
            require(!transition.requires_protocol_initialization && !transition.requires_abort_of_overlapping_burst,
                    "passive initial ON requested transmitter protocol initialization");
            initial_passive_on = true;
        }
    });
    require(initial_passive_on, "zero-delay receiver ON was not visible at time zero");
    const auto initial_registry = w::receiver_ownership(w::reference_snapshot(timeline.prepare()));
    const r::ReceiverId ship_radio{w::radio_name({31})}, passive_radio{w::passive_radio_name({71},1)};
    require(initial_registry->receiver(ship_radio).role == r::ReceiverRole::TransceiverRadio &&
            initial_registry->receiver(ship_radio).owner_station_id == w::station_name({11}) &&
            !initial_registry->receiver(ship_radio).enabled, "coupled receiver owner or initial OFF state lost");
    require(initial_registry->receiver(passive_radio).role == r::ReceiverRole::PassiveTerrestrial &&
            !initial_registry->receiver(passive_radio).owner_station_id && initial_registry->receiver(passive_radio).enabled,
            "passive receiver was assigned a vessel owner or initial ON lost");
    for (auto seconds : {0,9,10,14,15,30}) {
        const auto snapshot = at(timeline,r::from_seconds(seconds));
        same_vessel(vessel(snapshot),vessel(at(without_receiver,r::from_seconds(seconds))));
        require(snapshot.receivers.size() == 1 && snapshot.receivers[0].height_datum == w::HeightDatum::Amsl &&
            snapshot.receivers[0].location == region.center && snapshot.receivers[0].elevation_mm == 10000 &&
            snapshot.receivers[0].provenance == region.provenance, "candidate location/elevation/provenance changed with power");
        const auto expected = power_at(42,w::passive_equipment_key({71},1),region.power,seconds);
        require(snapshot.receivers[0].equipment_on == expected.on, "passive receiver power not independent");
        require(vessel(snapshot).time == snapshot.time && snapshot.receivers[0].snapshot_time == snapshot.time,
                "materialized world components lost snapshot time");
        const auto current_registry = w::receiver_ownership(snapshot);
        require(current_registry->receiver(ship_radio).enabled == vessel(snapshot).state.equipment_on &&
                current_registry->receiver(passive_radio).enabled == expected.on,
                "radio ownership view did not bind independent current power");
        require(!initial_registry->receiver(ship_radio).enabled && initial_registry->receiver(passive_radio).enabled,
                "new world power state mutated an earlier immutable ownership view");
        invalid([&]{ (void)w::rf_geometry(snapshot.receivers[0],snapshot.time); }, "AMSL silently treated as ellipsoid RF geometry");
    }
    auto unresolved = base(); unresolved.vessels[0].waterline_ellipsoid_mm.reset(); w::Timeline sea(unresolved);
    const auto vessel_snapshot = at(sea,{0}); invalid([&]{ (void)w::rf_geometry(vessel(vessel_snapshot),{0}); }, "unresolved waterline became ellipsoid zero");
    const auto qualified = at(without_receiver,r::from_seconds(30));
    require(w::rf_geometry(vessel(qualified),qualified.time).ellipsoid_height_mm == 18500, "explicit ellipsoid antenna offset not resolved");
    const auto geometry = w::rf_geometry(qualified,w::VesselId{11});
    require(geometry.time == qualified.time && geometry.location == vessel(qualified).location,
            "snapshot geometry changed vessel identity, location or time");
    invalid([&]{ (void)w::rf_geometry(vessel(qualified),r::add_units(qualified.time,1)); }, "stale vessel position relabeled with fresh time");
    auto forged_snapshot = qualified; forged_snapshot.time = r::add_units(qualified.time,1);
    invalid([&]{ (void)w::rf_geometry(forged_snapshot,w::VesselId{11}); }, "snapshot wrapper relabeled stale vessel position");
    invalid([&]{ (void)w::receiver_ownership(forged_snapshot); }, "mixed-time vessel ownership snapshot accepted");
    invalid([&]{ (void)w::rf_geometry(qualified,w::VesselId{999}); }, "unknown vessel geometry silently borrowed another identity");
    auto ellipsoid = config; ellipsoid.regions[0].height_datum = w::HeightDatum::Wgs84Ellipsoid;
    w::Timeline explicit_height(ellipsoid,samples); const auto fixed = at(explicit_height,{0});
    require(w::rf_geometry(fixed.receivers[0],fixed.time).ellipsoid_height_mm == 10000, "explicit receiver ellipsoid height changed");
    invalid([&]{ (void)w::rf_geometry(fixed.receivers[0],{1}); }, "stale receiver position relabeled with fresh time");
    auto mixed_receiver = fixed; mixed_receiver.receivers[0].snapshot_time = {1};
    invalid([&]{ (void)w::receiver_ownership(mixed_receiver); }, "mixed-time passive ownership snapshot accepted");
    invalid([&]{ w::Timeline missing(config); }, "missing CUDA receiver sample list accepted");
    auto duplicate = samples; duplicate.push_back(samples[0]); invalid([&]{ w::Timeline bad(config,duplicate); }, "duplicate receiver sample accepted");
    auto wrong = samples; wrong[0].ordinal = 2; invalid([&]{ w::Timeline bad(config,wrong); }, "unstable receiver ordinal accepted");
    wrong = samples; wrong[0].location.longitude_ndeg += w::kDegree;
    invalid([&]{ w::Timeline bad(config,wrong); }, "receiver sample outside footprint accepted");
}
void seed_resolution_and_transition_peek() {
    auto requested = base(); requested.fresh_seed = true;
    invalid([&]{ w::Timeline bad(requested); }, "unresolved fresh request silently reused stored seed in Timeline");
    invalid([&]{ (void)w::region_specs(requested); }, "unresolved fresh request silently reused stored seed for region counts");
    invalid([&]{ (void)w::inspect_burst(requested,21,{0},r::from_seconds(1)); }, "unresolved fresh request silently reused stored power schedule");
    const auto resolved = w::resolve_seed(requested);
    require(!resolved.fresh_seed && requested.fresh_seed, "OS seed resolution did not consume only the copied request flag");
    const auto repeated = w::resolve_seed(resolved);
    require(!repeated.fresh_seed && repeated.effective_seed == resolved.effective_seed,
            "resolved effective seed was acquired again rather than replayed");
    w::Timeline first(resolved), replay(repeated);
    same_vessel(vessel(at(first,r::from_seconds(3600))),vessel(at(replay,r::from_seconds(3600))));

    auto brief = base(); brief.duration = r::from_seconds(1);
    w::Timeline timeline(brief);
    require(timeline.next_transition_time() == r::SimulationInstant{0}, "peek hid pending initial preparation notices");
    timeline.advance({0});
    require(!timeline.next_transition_time(), "peek exposed an event outside the configured horizon");
    timeline.advance(brief.duration);
    require(!timeline.next_transition_time(), "completed world retained a future transition beyond its horizon");
    w::Timeline failed(brief); bool sink_failed = false;
    try { failed.advance({0},[](const auto&){ throw std::runtime_error("synthetic failed consumer"); }); }
    catch (const std::runtime_error&) { sink_failed = true; }
    require(sink_failed, "failing initial transition consumer was ignored");
    bool peek_failed = false;
    try { (void)failed.next_transition_time(); } catch (const std::runtime_error&) { peek_failed = true; }
    require(peek_failed, "failed timeline advertised an actionable future transition");
}
void waypoint_arrivals() {
    auto config = base(); config.duration = r::from_seconds(600);
    config.waypoints = {{{1},"synthetic near A",{28000000000LL,43000000000LL}},
        {{2},"synthetic near B",{28001000000LL,43000000000LL}},{{3},"synthetic near C",{28000000000LL,43001000000LL}}};
    config.vessels[0].subsequent_destination = subset({1,2,3});
    w::Timeline timeline(config); std::uint64_t arrivals = 0; auto prior = at(timeline,{0});
    while (timeline.next_transition_time() && *timeline.next_transition_time() <= config.duration) {
        const auto next = *timeline.next_transition_time(); const auto just_before = r::add_units(next,-1);
        if (just_before >= timeline.prepare().time) prior = at(timeline,just_before);
        std::vector<w::Transition> notices; const auto current = at(timeline,next,[&](const auto& notice){notices.push_back(notice);});
        if (std::any_of(notices.begin(),notices.end(),[](const auto& notice){return notice.kind == w::TransitionKind::WaypointArrival;})) {
            const auto& a = vessel(prior); const auto& b = vessel(current);
            require(b.state.leg_index == a.state.leg_index + 1 && b.state.leg_from == a.state.destination && b.state.destination != b.state.leg_from,
                    "waypoint cycling teleported/reused destination or lost leg identity");
            const auto new_leg_start = std::find_if(config.waypoints.begin(),config.waypoints.end(),[&](const auto& point){return point.id == b.state.leg_from;});
            require(new_leg_start != config.waypoints.end() && b.location == new_leg_start->location,
                    "new leg did not start at exact previous destination");
            const geo::Result endpoint{b.location.longitude_ndeg/1e9,b.location.latitude_ndeg/1e9,0};
            require(geo::endpoint_error_m(a.location.longitude_ndeg/1e9,a.location.latitude_ndeg/1e9,endpoint) < 0.002,
                    "waypoint arrival introduced positional jump");
            ++arrivals;
        }
    }
    require(arrivals > 20 && timeline.metrics().peak_pending_notices < 100, "cycling route or bounded sparse notices not exercised");
}
void initial_waypoint_exclusion() {
    auto config = base(); config.duration = r::from_seconds(120);
    config.waypoints = {{{1},"initial A",{28000000000LL,43000000000LL}},
        {{2},"fixed destination B",{28001000000LL,43000000000LL}},{{3},"initial C",{28000000000LL,43001000000LL}}};
    config.vessels[0].first_destination = fixed(2);
    config.vessels[0].subsequent_destination = subset({3,2,1});
    const std::array<std::uint32_t,2> eligible{{1,3}};
    for (bool use_subset : {false,true}) {
        std::set<std::uint32_t> observed;
        for (std::uint64_t seed = 0; seed < 64; ++seed) {
            config.effective_seed = seed;
            config.vessels[0].initial = use_subset ? subset({3,2,1}) : w::Selection{};
            const auto normalized = w::canonical_config(config);
            require(normalized.vessels[0].initial.mode == config.vessels[0].initial.mode &&
                    normalized.vessels[0].initial.waypoint_ids == config.vessels[0].initial.waypoint_ids,
                    "effective initial pool rewrote the requested selector");
            const auto expected = eligible.at(static_cast<std::size_t>(bounded(seed,11,w::Purpose::InitialWaypoint,0,0,1)));
            w::Timeline timeline(config); const auto start = at(timeline,{0}); const auto& first = vessel(start);
            require(first.state.leg_from.value == expected && first.state.destination.value == 2 && expected != 2,
                    "initial start did not match independent Philox draw over sorted filtered pool");
            observed.insert(first.state.leg_from.value);

            auto reordered = config; std::reverse(reordered.waypoints.begin(),reordered.waypoints.end());
            if (use_subset) std::reverse(reordered.vessels[0].initial.waypoint_ids.begin(),reordered.vessels[0].initial.waypoint_ids.end());
            auto other = config.vessels[0]; other.id = {99}; other.equipment_id = {199}; other.radio_id = {299}; other.name = "unrelated initial selector";
            reordered.vessels.insert(reordered.vessels.begin(),other);
            w::Timeline unrelated(reordered); same_vessel(first,vessel(at(unrelated,{0})));

            // The first fixed endpoint is followed by the unchanged cycling
            // domain/episode, with no reset or teleport at arrival.
            bool arrived = false;
            while (const auto next = timeline.next_transition_time()) {
                const auto before = at(timeline,r::add_units(*next,-1));
                std::vector<w::Transition> notices;
                const auto after = at(timeline,*next,[&](const auto& notice){notices.push_back(notice);});
                if (std::none_of(notices.begin(),notices.end(),[](const auto& notice){return notice.kind == w::TransitionKind::WaypointArrival;})) continue;
                const auto& next_leg = vessel(after);
                const auto destination = eligible.at(static_cast<std::size_t>(bounded(seed,11,w::Purpose::NextDestination,1,0,1)));
                require(next_leg.state.leg_index == 1 && next_leg.state.leg_from.value == 2 && next_leg.state.destination.value == destination &&
                        next_leg.state.position_query.origin == config.waypoints[1].location,
                        "first fixed destination changed subsequent route identity or RNG episode");
                const geo::Result endpoint{next_leg.location.longitude_ndeg/1e9,next_leg.location.latitude_ndeg/1e9,0};
                require(geo::endpoint_error_m(vessel(before).location.longitude_ndeg/1e9,vessel(before).location.latitude_ndeg/1e9,endpoint) < 0.002,
                        "filtered initial selection introduced a waypoint discontinuity");
                same_vessel(next_leg,vessel(at(unrelated,*next))); arrived = true; break;
            }
            require(arrived,"filtered initial route did not reach its fixed first destination");
        }
        require(observed == std::set<std::uint32_t>{1,3},"seed set did not cover both eligible initial choices");
    }

    // A nonfixed first selector retains the original full initial pool and
    // independent first-destination draw, excluding only the chosen start.
    const std::array<std::uint32_t,3> all{{1,2,3}};
    const std::array<std::array<std::uint32_t,2>,3> after_start{{{{2,3}},{{1,3}},{{1,2}}}};
    for (bool initial_subset : {false,true}) for (bool first_subset : {false,true}) {
        config.vessels[0].initial = initial_subset ? subset({3,1,2}) : w::Selection{};
        config.vessels[0].first_destination = first_subset ? subset({2,3,1}) : w::Selection{};
        for (std::uint64_t seed = 0; seed < 32; ++seed) {
            config.effective_seed = seed;
            const auto expected = all.at(static_cast<std::size_t>(bounded(seed,11,w::Purpose::InitialWaypoint,0,0,2)));
            const auto destination = after_start.at(expected-1).at(static_cast<std::size_t>(bounded(seed,11,w::Purpose::FirstDestination,0,0,1)));
            w::Timeline timeline(config); const auto start = at(timeline,{0});
            require(vessel(start).state.leg_from.value == expected && vessel(start).state.destination.value == destination,
                    "nonfixed first destination changed the formerly valid initial or destination draw");
        }
    }

    auto bad = config; bad.vessels[0].first_destination = fixed(2); bad.vessels[0].initial = subset({2,99,1});
    invalid([&]{ (void)w::canonical_config(bad); },"invalid initial waypoint ID survived filtering");
    bad.vessels[0].initial = subset({2,2,3});
    invalid([&]{ (void)w::canonical_config(bad); },"duplicate initial waypoint IDs survived filtering");
    bad.vessels[0].initial = subset({2,3}); bad.vessels[0].subsequent_destination = subset({1,2});
    bad.waypoints[2].location = {28001000001LL,43000000000LL};
    invalid([&]{ (void)w::canonical_config(bad); },"submetre eligible initial leg bypassed geographic qualification");
    bad.waypoints[2].location = {54000000000LL,43000000000LL};
    invalid([&]{ (void)w::canonical_config(bad); },"overrange eligible initial leg bypassed geographic qualification");
}
void fixed_initial_trace_regression() {
    auto config = base(); config.duration = r::from_seconds(120);
    w::Timeline timeline(config); std::vector<w::Transition> actual;
    const auto sink = [&](const auto& notice){actual.push_back(notice);};
    const auto start = at(timeline,{0},sink);
    require(vessel(start).state.leg_from.value == 1 && vessel(start).state.destination.value == 2 &&
            vessel(start).location == config.waypoints[0].location,"formerly valid fixed startup changed");
    const auto end = at(timeline,config.duration,sink);
    std::vector<w::Transition> expected{{{0},w::TransitionKind::SpeedChanged,11,0,20000},
        {{0},w::TransitionKind::NavigationChanged,11,0,0},{{0},w::TransitionKind::InitialOff,21,0,0}};
    for (std::uint64_t episode = 0; episode <= 7; ++episode)
        expected.push_back({r::from_seconds(static_cast<std::int64_t>(17*episode)),w::TransitionKind::ReachChanged,21,episode,
            draw(42,21,w::Purpose::ReachValue,episode,config.vessels[0].reach_radius_mm)});
    for (std::int64_t time = 30; time <= 120; time += 10) {
        const bool on = (time-30)%20 == 0;
        expected.push_back({r::from_seconds(time),on?w::TransitionKind::EquipmentOn:w::TransitionKind::EquipmentOff,
            21,static_cast<std::uint64_t>((time-30)/20+1),on?1:0,on,!on});
    }
    std::sort(expected.begin(),expected.end(),[](const auto& a,const auto& b){
        return std::tie(a.time.units,a.kind,a.entity_id,a.episode)<std::tie(b.time.units,b.kind,b.entity_id,b.episode);});
    require(actual.size() == expected.size(),"formerly valid fixed route transition count changed");
    for (std::size_t index = 0; index < expected.size(); ++index) {
        const auto& a = actual[index]; const auto& b = expected[index];
        require(a.time == b.time && a.kind == b.kind && a.entity_id == b.entity_id && a.episode == b.episode && a.value == b.value &&
                a.requires_protocol_initialization == b.requires_protocol_initialization &&
                a.requires_abort_of_overlapping_burst == b.requires_abort_of_overlapping_burst,
                "formerly valid fixed route transition trace changed");
    }
    require(vessel(end).state.leg_from.value == 1 && vessel(end).state.destination.value == 2 &&
            vessel(end).state.total_distance_mm == 1234666 && vessel(end).state.total_distance_remainder == 5898240000,
            "formerly valid fixed voyage distance or route changed");
}
void invalid_configuration() {
    const auto valid = base(); (void)w::canonical_config(valid);
    for (unsigned variant = 0; variant < 24; ++variant) {
        auto config = valid; auto& ship = config.vessels[0];
        switch (variant) {
        case 0: config.schema_version = 2; break;
        case 1: config.duration = {-1}; break;
        case 2: config.duration = r::from_seconds(w::kMaxScenarioSeconds+1); break;
        case 3: ship.power.initial_delay_seconds = {-1,0}; break;
        case 4: ship.power.online_duration_seconds = {0,1}; break;
        case 5: ship.power.offline_duration_seconds = {0,0}; break;
        case 6: ship.power.online_duration_seconds = {1,INT64_MAX}; break;
        case 7: ship.speed_hold_seconds = {0,0}; break;
        case 8: ship.speed_mkn = {-1,100}; break;
        case 9: ship.reach_radius_mm = {-1,1}; break;
        case 10: ship.reach_hold_seconds = {0,1}; break;
        case 11: ship.navigation[0].duration_seconds = {0,0}; break;
        case 12: ship.navigation[0].status = static_cast<w::NavigationStatus>(2); break;
        case 13: ship.navigation[0].weight = 0; break;
        case 14: ship.restricted_speed_cap_mkn = 0; break;
        case 15: ship.antenna_offset_mm = -1; break;
        case 16: ship.initial = fixed(1); ship.first_destination = fixed(1); break;
        case 17: ship.initial = subset({1}); ship.first_destination = fixed(1); break;
        case 18: ship.subsequent_destination = subset({1}); break;
        case 19: ship.first_destination = subset({2,2}); break;
        case 20: config.waypoints[1].id = config.waypoints[0].id; break;
        case 21: config.waypoints[1].location = config.waypoints[0].location; break;
        case 22: ship.equipment_id = {0}; break;
        case 23: config.waypoints[1].name = config.waypoints[0].name; break;
        }
        invalid([&]{ (void)w::canonical_config(config); }, "invalid world configuration accepted");
    }
    auto duplicate = valid; duplicate.vessels.push_back(duplicate.vessels[0]);
    invalid([&]{ (void)w::canonical_config(duplicate); }, "duplicate vessel accepted");
    duplicate.vessels.back().id = {99}; duplicate.vessels.back().name = "different vessel";
    invalid([&]{ (void)w::canonical_config(duplicate); }, "duplicate coupled equipment accepted");
    duplicate.vessels.back().equipment_id = {199};
    invalid([&]{ (void)w::canonical_config(duplicate); }, "duplicate receiver radio interface accepted");
    w::Timeline timeline(valid); invalid([&]{timeline.advance({-1});}, "negative world clock accepted");
    invalid([&]{timeline.advance(r::add_units(valid.duration,1));}, "query past scenario horizon accepted");
}
}  // namespace

int main() {
    try {
        rng_contract(); constant_voyage_and_power(); independent_episode_model();
        independent_entities_and_chunking(); passive_and_height_contract(); seed_resolution_and_transition_peek();
        waypoint_arrivals(); initial_waypoint_exclusion(); fixed_initial_trace_regression(); invalid_configuration();
        std::cout << "PASS W1 CUDA-hidden host contracts: independent Philox/ellipsoid ODE, five-hour continuous navigation, "
                  << invalid_count << " invalid contracts; WORLD ONLY, NO AIS TDMA OR RF\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << "W1 host contract FAIL: " << error.what() << '\n'; return 1; }
}
