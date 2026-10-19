#include "world.h"
#include "rng.h"
#include <algorithm>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <tuple>
#define NOMINMAX
#include <Windows.h>
#include <bcrypt.h>

namespace aismixer::lab::world {
namespace {
constexpr std::int64_t kNever = INT64_MAX;
void require(bool condition, const char* message) { if (!condition) throw std::invalid_argument(message); }
void interval(Interval value, std::int64_t low, std::int64_t high, const char* message) {
    require(value.minimum >= low && value.maximum >= value.minimum && value.maximum <= high, message);
}
void seconds(Interval value, bool zero) {
    interval(value, zero ? 0 : 1, INT64_MAX / runtime::kUnitsPerSecond, "W1 invalid or overflowing duration interval");
    (void)runtime::from_seconds(value.maximum);
}
void power_model(const PowerModel& power) {
    seconds(power.initial_delay_seconds, true); seconds(power.online_duration_seconds, false); seconds(power.offline_duration_seconds, false);
}
void label(const std::string& value) {
    require(!value.empty() && value.size() <= 256, "W1 missing or excessive name/provenance");
    for (unsigned char c : value) require(c >= 32 && c != 127, "W1 control character in metadata");
}
std::int64_t draw(std::uint64_t seed, std::uint64_t key, Purpose purpose, std::uint64_t episode, Interval range) {
    return sample_integer(seed, key, purpose, episode, range.minimum, range.maximum);
}
SimulationInstant deadline(SimulationInstant time, std::int64_t duration_seconds) {
    return runtime::add_units(time, runtime::from_seconds(duration_seconds).units);
}
template<class T, class IdType> const T& by_id(const std::vector<T>& values, IdType id) {
    const auto found = std::lower_bound(values.begin(), values.end(), id, [](const T& item, IdType key) { return item.id < key; });
    if (found == values.end() || found->id != id) throw std::invalid_argument("W1 unknown stable ID");
    return *found;
}
std::vector<WaypointId> candidates(const Config& config, const Selection& selection, WaypointId exclude = {}) {
    std::vector<WaypointId> result;
    switch (selection.mode) {
    case SelectionMode::Fixed:
        require(selection.waypoint_ids.empty(), "W1 fixed selection contains a subset");
        (void)by_id(config.waypoints, selection.waypoint_id); result.push_back(selection.waypoint_id); break;
    case SelectionMode::All:
        require(selection.waypoint_id.value == 0 && selection.waypoint_ids.empty(), "W1 all selection contains fixed/subset values");
        for (const auto& point : config.waypoints) result.push_back(point.id); break;
    case SelectionMode::Subset:
        require(selection.waypoint_id.value == 0 && !selection.waypoint_ids.empty(), "W1 empty or contradictory subset selection");
        result = selection.waypoint_ids; std::sort(result.begin(), result.end());
        require(std::adjacent_find(result.begin(), result.end()) == result.end(), "W1 duplicate waypoint in subset");
        for (auto id : result) (void)by_id(config.waypoints, id); break;
    default: throw std::invalid_argument("W1 unsupported selection mode");
    }
    result.erase(std::remove(result.begin(), result.end(), exclude), result.end());
    return result;
}
WaypointId initial_exclusion(const Vessel& vessel) {
    return vessel.first_destination.mode == SelectionMode::Fixed ? vessel.first_destination.waypoint_id : WaypointId{};
}
WaypointId choose(const Config& config, const Selection& selection, VesselId id, Purpose purpose,
                  std::uint64_t episode, WaypointId exclude = {}) {
    const auto pool = candidates(config, selection, exclude);
    require(!pool.empty(), "W1 impossible waypoint selection after excluding current point");
    const auto selected = sample_integer(config.effective_seed, id.value, purpose, episode, 0, static_cast<std::int64_t>(pool.size()-1));
    return pool.at(static_cast<std::size_t>(selected));
}
struct Power {
    PowerModel model; std::uint64_t key = 0, seed = 0, online_episode = 0, offline_episode = 0, generation = 0;
    bool on = false; SimulationInstant next;
    Power(PowerModel value, std::uint64_t identity, std::uint64_t effective_seed)
        : model(value), key(identity), seed(effective_seed) {
        next = runtime::from_seconds(draw(seed, key, Purpose::EquipmentInitial, 0, model.initial_delay_seconds));
        if (next.units == 0) toggle({0});
    }
    void toggle(SimulationInstant time) {
        on = !on;
        if (on) {
            if (generation == UINT64_MAX || online_episode == UINT64_MAX) throw std::overflow_error("W1 power episode overflow");
            ++generation;
            next = deadline(time, draw(seed, key, Purpose::EquipmentOnline, online_episode++, model.online_duration_seconds));
        } else {
            if (offline_episode == UINT64_MAX) throw std::overflow_error("W1 power episode overflow");
            next = deadline(time, draw(seed, key, Purpose::EquipmentOffline, offline_episode++, model.offline_duration_seconds));
        }
    }
};
std::int64_t effective_speed(const Vessel& vessel, const VesselState& state) {
    if (state.navigation_status == NavigationStatus::AtAnchor) return 0;
    return state.navigation_status == NavigationStatus::RestrictedManoeuvrability
        ? std::min(state.nominal_speed_mkn, vessel.restricted_speed_cap_mkn) : state.nominal_speed_mkn;
}
void add_distance(std::uint64_t& millimetres, std::uint64_t& remainder, std::uint64_t coefficient, std::uint64_t units) {
    const auto fractional_numerator = (units % kDistanceDenominator) * coefficient + remainder;
    const auto whole = (units / kDistanceDenominator) * coefficient + fractional_numerator / kDistanceDenominator;
    if (millimetres > UINT64_MAX - whole) throw std::overflow_error("W1 accumulated distance overflow");
    millimetres += whole; remainder = fractional_numerator % kDistanceDenominator;
}
}  // namespace

Config canonical_config(Config config) {
    require(config.schema_version == 1, "W1 unsupported world schema version");
    label(config.profile_id); label(config.provenance);
    require(config.duration.units > 0 && config.duration <= runtime::from_seconds(kMaxScenarioSeconds), "W1 scenario duration outside 1 unit..7 days");
    require(config.waypoints.size() >= 2 && config.waypoints.size() <= kMaxWaypoints, "W1 waypoint catalogue size outside 2..256");
    require(!config.vessels.empty() && config.vessels.size() <= kMaxVessels, "W1 vessel catalogue size outside 1..1024");
    require(config.regions.size() <= kMaxRegions, "W1 excessive receiver regions");
    auto sort_catalogue = [](auto& values) {
        std::sort(values.begin(), values.end(), [](const auto& a, const auto& b) { return a.id < b.id; });
        std::set<std::string> names;
        std::uint32_t previous = 0;
        for (const auto& value : values) { require(value.id.value > previous, "W1 zero/duplicate stable ID"); previous = value.id.value;
            label(value.name); require(names.insert(value.name).second, "W1 duplicate catalogue name"); }
    };
    sort_catalogue(config.waypoints); sort_catalogue(config.vessels); sort_catalogue(config.regions);
    std::set<std::pair<std::int64_t,std::int64_t>> coordinates;
    for (const auto& point : config.waypoints) {
        (void)geodesic_direct(point.location, 0, 0);
        require(coordinates.emplace(point.location.longitude_ndeg, point.location.latitude_ndeg).second, "W1 duplicate waypoint position");
    }
    std::set<std::uint32_t> equipment, radios;
    std::set<std::pair<std::uint32_t,std::uint32_t>> checked_pairs;
    auto check_leg = [&](WaypointId from, WaypointId to) {
        if (from == to || !checked_pairs.emplace(from.value,to.value).second) return;
        const auto result = geodesic_inverse(by_id(config.waypoints,from).location, by_id(config.waypoints,to).location);
        require(result.distance_um >= 1000000 && result.distance_um <= INT64_C(2000000000000), "W1 leg is shorter than1m or exceeds qualified2000km");
    };
    for (auto& vessel : config.vessels) {
        require(vessel.equipment_id.value && equipment.insert(vessel.equipment_id.value).second, "W1 duplicate/zero Class A equipment ID");
        require(vessel.radio_id.value && radios.insert(vessel.radio_id.value).second, "W1 duplicate/zero coupled radio ID");
        power_model(vessel.power); interval(vessel.speed_mkn,1,100000,"W1 nominal speed outside 0.001..100 kn");
        seconds(vessel.speed_hold_seconds,false); seconds(vessel.reach_hold_seconds,false);
        interval(vessel.reach_radius_mm,0,2000000000,"W1 negative/excessive effective reach radius");
        require(vessel.restricted_speed_cap_mkn > 0 && vessel.restricted_speed_cap_mkn <= 100000, "W1 invalid restricted speed cap");
        require(vessel.antenna_offset_mm >= 0 && vessel.antenna_offset_mm <= 1000000, "W1 invalid fixed antenna mounting");
        label(vessel.waterline_provenance);
        if (vessel.waterline_ellipsoid_mm) require(*vessel.waterline_ellipsoid_mm >= -10000000 && *vessel.waterline_ellipsoid_mm <= 10000000, "W1 invalid waterline ellipsoid height");
        require(!vessel.navigation.empty() && vessel.navigation.size() <= 3,"W1 navigation episode catalogue outside 1..3");
        std::set<NavigationStatus> statuses; std::uint64_t weight = 0;
        for (const auto& item : vessel.navigation) {
            require(item.status == NavigationStatus::UnderWayUsingEngine || item.status == NavigationStatus::AtAnchor || item.status == NavigationStatus::RestrictedManoeuvrability, "W1 unsupported navigation status");
            require(statuses.insert(item.status).second && item.weight > 0, "W1 duplicate/zero-weight navigation episode");
            weight += item.weight; seconds(item.duration_seconds,false);
        }
        require(weight <= UINT32_MAX,"W1 excessive navigation weights");
        std::sort(vessel.navigation.begin(), vessel.navigation.end(), [](auto a, auto b) { return a.status < b.status; });
        const auto initial = candidates(config,vessel.initial,initial_exclusion(vessel)), first = candidates(config,vessel.first_destination);
        require(!initial.empty(),"W1 initial selection has no eligible waypoint after excluding the fixed first destination");
        const auto cycle = candidates(config,vessel.subsequent_destination);
        require(vessel.subsequent_destination.mode != SelectionMode::Fixed && cycle.size() >= 2,"W1 cycling pool requires at least two distinct candidates");
        for (auto start : initial) {
            require(std::any_of(first.begin(),first.end(),[&](auto item){return item != start;}), "W1 impossible first destination");
            for (auto destination : first) check_leg(start,destination);
        }
        for (auto start : first) for (auto destination : cycle) check_leg(start,destination);
        for (auto start : cycle) for (auto destination : cycle) check_leg(start,destination);
    }
    std::size_t receiver_bound = config.vessels.size();
    for (const auto& region : config.regions) {
        (void)geodesic_direct(region.center,0,0); label(region.provenance); power_model(region.power);
        require(region.radius_mm >= 0 && region.radius_mm <= 100000000,"W1 receiver footprint outside 0..100 km");
        interval(region.count,1,static_cast<std::int64_t>(kMaxPassiveReceivers),"W1 receiver count outside 1..4096");
        receiver_bound += static_cast<std::size_t>(region.count.maximum);
        require(receiver_bound <= kMaxPassiveReceivers,"W1 aggregate Class A and passive radio catalogue exceeds4096");
        interval(region.elevation_mm,0,10000000,"W1 receiver elevation outside 0..10000 m");
        require(region.height_datum == HeightDatum::Amsl || region.height_datum == HeightDatum::Wgs84Ellipsoid,"W1 unsupported receiver height datum; no implicit conversion");
    }
    return config;
}
std::uint64_t fresh_seed() {
    std::uint64_t seed = 0;
    if (BCryptGenRandom(nullptr,reinterpret_cast<PUCHAR>(&seed),sizeof(seed),BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0)
        throw std::runtime_error("W1 OS fresh seed acquisition failed");
    return seed;
}
Config resolve_seed(Config config) {
    config=canonical_config(std::move(config));
    if(config.fresh_seed)config.effective_seed=fresh_seed();
    config.fresh_seed=false;
    return config;
}
std::uint64_t passive_equipment_key(RegionId region, std::uint32_t ordinal) {
    require(region.value != 0 && ordinal != 0,"W1 passive equipment key needs positive region and ordinal");
    return (static_cast<std::uint64_t>(region.value)<<32) | ordinal;
}
std::string station_name(VesselId id) { require(id.value != 0,"W1 invalid vessel ID"); return "world.station."+std::to_string(id.value); }
std::string radio_name(RadioId id) { require(id.value != 0,"W1 invalid radio ID"); return "world.radio."+std::to_string(id.value); }
std::string passive_radio_name(RegionId id,std::uint32_t ordinal) { (void)passive_equipment_key(id,ordinal); return "world.passive."+std::to_string(id.value)+"."+std::to_string(ordinal); }

struct Timeline::Impl {
    struct Ship {
        VesselState view; Power power;
        std::uint64_t speed_episode=0,status_episode=0,reach_episode=0;
        SimulationInstant next_speed,next_status,next_reach;
        Ship(const Vessel& vessel,std::uint64_t seed) : power(vessel.power,vessel.equipment_id.value,seed) {}
    };
    struct Ground { PassiveState view; Power power;
        Ground(const Region& region,const RegionSample& sample,std::uint64_t seed)
            : power(region.power,passive_equipment_key(region.id,sample.ordinal),seed) {
            view.region_id=region.id; view.ordinal=sample.ordinal; view.location=sample.location;
            view.elevation_mm=sample.elevation_mm; view.height_datum=region.height_datum; view.provenance=region.provenance;
        }
    };
    Config config; SimulationInstant now{0}; std::vector<Ship> ships; std::vector<Ground> ground;
    std::vector<Transition> initial; TimelineMetrics metrics; bool failed=false,busy=false;
    explicit Impl(Config value,std::vector<RegionSample> samples) : config(canonical_config(std::move(value))) {
        require(!config.fresh_seed,"W1 unresolved fresh seed: use resolve_seed or WorldSession before Timeline");
        for (const auto& vessel : config.vessels) {
            ships.emplace_back(vessel,config.effective_seed); auto& ship=ships.back(); auto& view=ship.view;
            view.id=vessel.id; view.equipment_id=vessel.equipment_id; view.radio_id=vessel.radio_id;
            view.leg_from=choose(config,vessel.initial,vessel.id,Purpose::InitialWaypoint,0,initial_exclusion(vessel));
            view.destination=choose(config,vessel.first_destination,vessel.id,Purpose::FirstDestination,0,view.leg_from);
            view.antenna_offset_mm=vessel.antenna_offset_mm;
            if (vessel.waterline_ellipsoid_mm) view.antenna_ellipsoid_mm=*vessel.waterline_ellipsoid_mm+vessel.antenna_offset_mm;
            new_leg(ship); speed(ship,vessel,{0}); status(ship,vessel,{0}); reach(ship,vessel,{0});
            initial.push_back({{0},TransitionKind::SpeedChanged,vessel.id.value,0,view.nominal_speed_mkn});
            initial.push_back({{0},TransitionKind::NavigationChanged,vessel.id.value,0,static_cast<std::int64_t>(view.navigation_status)});
            initial.push_back({{0},TransitionKind::ReachChanged,vessel.equipment_id.value,0,view.effective_reach_radius_mm});
            initial_power(ship.power,true);
        }
        std::sort(samples.begin(),samples.end(),[](const auto& a,const auto& b){return std::tie(a.region_id,a.ordinal)<std::tie(b.region_id,b.ordinal);});
        std::size_t position=0;
        for (const auto& region : config.regions) {
            const auto count=draw(config.effective_seed,region.id.value,Purpose::RegionCount,0,region.count);
            for (std::uint32_t ordinal=1;ordinal<=static_cast<std::uint32_t>(count);++ordinal) {
                require(position<samples.size(),"W1 missing CUDA receiver samples"); const auto& sample=samples.at(position++);
                require(sample.region_id==region.id.value && sample.ordinal==ordinal,"W1 receiver sample identity/count mismatch");
                require(sample.elevation_mm>=region.elevation_mm.minimum && sample.elevation_mm<=region.elevation_mm.maximum,"W1 receiver elevation outside declared region");
                const auto distance=geodesic_inverse(region.center,sample.location).distance_um;
                require(distance<=region.radius_mm*1000+1000,"W1 receiver sample outside geodesic footprint");
                ground.emplace_back(region,sample,config.effective_seed); initial_power(ground.back().power,false);
            }
        }
        require(position==samples.size(),"W1 extra or duplicate receiver samples");
        metrics.vessels=ships.size(); metrics.receivers=ground.size(); metrics.peak_pending_notices=initial.size();
        metrics.owned_state_bytes=ships.capacity()*sizeof(Ship)+ground.capacity()*sizeof(Ground)+initial.capacity()*sizeof(Transition);
    }
    void initial_power(const Power& power,bool class_a) {
        initial.push_back({{0},TransitionKind::InitialOff,power.key,0,0,false,false});
        if (power.on) initial.push_back({{0},TransitionKind::EquipmentOn,power.key,power.generation,1,class_a,false});
    }
    void new_leg(Ship& ship) {
        const auto origin=by_id(config.waypoints,ship.view.leg_from).location;
        const auto destination=by_id(config.waypoints,ship.view.destination).location;
        const auto inverse=geodesic_inverse(origin,destination);
        ship.view.leg_length_mm=(inverse.distance_um+500)/1000;
        require(ship.view.leg_length_mm>0 && ship.view.leg_length_mm<=INT64_C(2000000000),"W1 invalid geodesic leg length");
        ship.view.position_query={origin,inverse.initial_bearing_ndeg,0.0};
    }
    void speed(Ship& ship,const Vessel& vessel,SimulationInstant time) {
        ship.view.nominal_speed_mkn=draw(config.effective_seed,vessel.id.value,Purpose::SpeedValue,ship.speed_episode,vessel.speed_mkn);
        ship.next_speed=deadline(time,draw(config.effective_seed,vessel.id.value,Purpose::SpeedDuration,ship.speed_episode++,vessel.speed_hold_seconds));
        ship.view.speed_mkn=effective_speed(vessel,ship.view); ship.view.course_valid=ship.view.speed_mkn>0;
    }
    void status(Ship& ship,const Vessel& vessel,SimulationInstant time) {
        std::int64_t total=0; for(const auto& item:vessel.navigation) total+=item.weight;
        auto selection=sample_integer(config.effective_seed,vessel.id.value,Purpose::StatusChoice,ship.status_episode,0,total-1);
        const NavigationEpisode* selected=nullptr;
        for(const auto& item:vessel.navigation) { if(selection<item.weight){selected=&item;break;} selection-=item.weight; }
        if (!selected) throw std::logic_error("W1 status selection invariant");
        ship.view.navigation_status=selected->status;
        ship.next_status=deadline(time,draw(config.effective_seed,vessel.id.value,Purpose::StatusDuration,ship.status_episode++,selected->duration_seconds));
        ship.view.speed_mkn=effective_speed(vessel,ship.view); ship.view.course_valid=ship.view.speed_mkn>0;
    }
    void reach(Ship& ship,const Vessel& vessel,SimulationInstant time) {
        ship.view.effective_reach_radius_mm=draw(config.effective_seed,vessel.equipment_id.value,Purpose::ReachValue,ship.reach_episode,vessel.reach_radius_mm);
        ship.next_reach=deadline(time,draw(config.effective_seed,vessel.equipment_id.value,Purpose::ReachDuration,ship.reach_episode++,vessel.reach_hold_seconds));
        ship.view.environment_revision=ship.reach_episode;
    }
    SimulationInstant arrival(const Ship& ship) const {
        if (ship.view.speed_mkn==0) return {kNever};
        const auto whole=static_cast<std::uint64_t>(ship.view.leg_length_mm-ship.view.leg_distance_mm)*kDistanceDenominator;
        const auto needed=whole-ship.view.distance_remainder;
        const auto coefficient=static_cast<std::uint64_t>(ship.view.speed_mkn)*463;
        const auto units=needed/coefficient+(needed%coefficient!=0);
        if (units>static_cast<std::uint64_t>(INT64_MAX-now.units)) return {kNever};
        return runtime::add_units(now,static_cast<std::int64_t>(units));
    }
    SimulationInstant next() const {
        SimulationInstant time{kNever};
        for(const auto& ship:ships) time=std::min({time,ship.power.next,ship.next_speed,ship.next_status,ship.next_reach,arrival(ship)});
        for(const auto& receiver:ground) time=std::min(time,receiver.power.next);
        return time;
    }
    void integrate(SimulationInstant target) {
        const auto units=static_cast<std::uint64_t>(runtime::difference_units(target,now));
        if (units==0) return;
        for(auto& ship:ships) {
            const auto coefficient=static_cast<std::uint64_t>(ship.view.speed_mkn)*463;
            auto distance=static_cast<std::uint64_t>(ship.view.leg_distance_mm);
            add_distance(distance,ship.view.distance_remainder,coefficient,units);
            ship.view.leg_distance_mm=static_cast<std::int64_t>(distance);
            add_distance(ship.view.total_distance_mm,ship.view.total_distance_remainder,coefficient,units);
        }
        ++metrics.integration_intervals; now=target;
    }
    void publish(std::vector<Transition>& notices,const TransitionSink& sink) {
        std::sort(notices.begin(),notices.end(),[](const auto& a,const auto& b){return std::tie(a.time.units,a.kind,a.entity_id,a.episode)<std::tie(b.time.units,b.kind,b.entity_id,b.episode);});
        metrics.peak_pending_notices=std::max(metrics.peak_pending_notices,notices.size());
        for(const auto& notice:notices) { if(metrics.transitions>=kMaxTransitions) throw std::runtime_error("W1 sparse transition capacity exhausted"); ++metrics.transitions; if(sink)sink(notice); }
        notices.clear();
    }
    void events(const TransitionSink& sink) {
        std::vector<Transition> notices;
        for(std::size_t index=0;index<ships.size();++index) {
            auto& ship=ships[index]; const auto& vessel=config.vessels[index];
            if(ship.view.leg_distance_mm>=ship.view.leg_length_mm) {
                const auto arrived=ship.view.destination;
                ship.view.leg_distance_mm-=ship.view.leg_length_mm; ship.view.leg_from=arrived;
                if(ship.view.leg_index==UINT64_MAX)throw std::overflow_error("W1 leg counter overflow");
                ++ship.view.leg_index;
                ship.view.destination=choose(config,vessel.subsequent_destination,vessel.id,Purpose::NextDestination,ship.view.leg_index,arrived);
                new_leg(ship);
                notices.push_back({now,TransitionKind::WaypointArrival,vessel.id.value,ship.view.leg_index,arrived.value});
            }
            if(ship.next_speed==now) { const auto episode=ship.speed_episode; speed(ship,vessel,now); notices.push_back({now,TransitionKind::SpeedChanged,vessel.id.value,episode,ship.view.nominal_speed_mkn}); }
            if(ship.next_status==now) { const auto episode=ship.status_episode; status(ship,vessel,now); notices.push_back({now,TransitionKind::NavigationChanged,vessel.id.value,episode,static_cast<std::int64_t>(ship.view.navigation_status)}); }
            if(ship.next_reach==now) { const auto episode=ship.reach_episode; reach(ship,vessel,now); notices.push_back({now,TransitionKind::ReachChanged,vessel.equipment_id.value,episode,ship.view.effective_reach_radius_mm}); }
            if(ship.power.next==now) { ship.power.toggle(now); notices.push_back({now,ship.power.on?TransitionKind::EquipmentOn:TransitionKind::EquipmentOff,ship.power.key,ship.power.generation,ship.power.on?1:0,ship.power.on,!ship.power.on}); }
        }
        for(auto& receiver:ground) if(receiver.power.next==now) {
            receiver.power.toggle(now); notices.push_back({now,receiver.power.on?TransitionKind::EquipmentOn:TransitionKind::EquipmentOff,receiver.power.key,receiver.power.generation,receiver.power.on?1:0,false,false});
        }
        publish(notices,sink);
    }
};

Timeline::Timeline(Config config,std::vector<RegionSample> samples):impl_(std::make_unique<Impl>(std::move(config),std::move(samples))){}
Timeline::~Timeline()=default; Timeline::Timeline(Timeline&&) noexcept=default; Timeline& Timeline::operator=(Timeline&&) noexcept=default;
void Timeline::advance(SimulationInstant target,const TransitionSink& sink) {
    auto& self=*impl_;
    if(self.failed)throw std::runtime_error("W1 timeline is failed");
    require(!self.busy,"W1 reentrant timeline advance");
    require(target>=self.now && target<=self.config.duration,"W1 regressed/out-of-horizon world time");
    self.busy=true;
    try {
        self.publish(self.initial,sink);
        while(self.next()<=target) { const auto next=self.next(); require(next>self.now,"W1 non-progressing world transition"); self.integrate(next); self.events(sink); }
        self.integrate(target); self.busy=false;
    } catch(...) { self.failed=true; self.busy=false; throw; }
}
PreparedSnapshot Timeline::prepare() const {
    const auto& self=*impl_; if(self.failed)throw std::runtime_error("W1 timeline is failed");
    PreparedSnapshot result; result.time=self.now;
    for(const auto& ship:self.ships) {
        auto view=ship.view; view.equipment_on=ship.power.on; view.power_generation=ship.power.generation;
        view.position_query.distance_m=(static_cast<double>(view.leg_distance_mm)+static_cast<double>(view.distance_remainder)/static_cast<double>(kDistanceDenominator))/1000.0;
        result.vessels.push_back(view);
    }
    for(const auto& receiver:self.ground) { auto view=receiver.view; view.equipment_on=receiver.power.on; view.power_generation=receiver.power.generation; view.snapshot_time=self.now; result.receivers.push_back(std::move(view)); }
    return result;
}
std::optional<SimulationInstant> Timeline::next_transition_time()const {
    if(impl_->failed)throw std::runtime_error("W1 timeline is failed");
    if(!impl_->initial.empty())return SimulationInstant{0};
    const auto time=impl_->next();
    return time.units==kNever || time>impl_->config.duration?std::nullopt:std::optional<SimulationInstant>{time};
}
const Config& Timeline::config()const{return impl_->config;}
const TimelineMetrics& Timeline::metrics()const{return impl_->metrics;}
Snapshot materialize_snapshot(const PreparedSnapshot& prepared,const std::vector<PositionResult>& positions) {
    require(positions.size()==prepared.vessels.size(),"W1 materialized position count mismatch"); Snapshot result; result.time=prepared.time; result.receivers=prepared.receivers;
    for(std::size_t index=0;index<positions.size();++index) {
        const auto& state=prepared.vessels[index]; const auto& point=positions[index];
        result.vessels.push_back({state,point.location,state.course_valid?std::optional<std::int64_t>{point.final_bearing_ndeg}:std::nullopt,prepared.time});
    }
    return result;
}
Snapshot reference_snapshot(const PreparedSnapshot& prepared) {
    std::vector<PositionResult> positions;
    for(const auto& vessel:prepared.vessels) { const auto& query=vessel.position_query; const auto point=geodesic_direct(query.origin,query.initial_bearing_ndeg,query.distance_m); positions.push_back({point.location,point.final_bearing_ndeg}); }
    return materialize_snapshot(prepared,positions);
}
std::vector<RegionSpec> region_specs(const Config& config) {
    require(!config.fresh_seed,"W1 unresolved fresh seed: resolve before sampling receiver counts");
    std::vector<RegionSpec> result;
    for(const auto& region:config.regions) {
        const auto count=draw(config.effective_seed,region.id.value,Purpose::RegionCount,0,region.count);
        result.push_back({region.id.value,region.center,region.radius_mm,region.elevation_mm.minimum,region.elevation_mm.maximum,static_cast<std::uint32_t>(count)});
    }
    return result;
}
AntennaGeometry rf_geometry(const VesselSnapshot& vessel,SimulationInstant time) {
    require(time==vessel.time,"W1 stale vessel snapshot cannot be relabeled with another RF geometry time");
    if(!vessel.state.antenna_ellipsoid_mm)throw std::invalid_argument("W1 vessel RF geometry unresolved: waterline datum/geoid conversion unavailable");
    return {vessel.location,*vessel.state.antenna_ellipsoid_mm,time};
}
AntennaGeometry rf_geometry(const PassiveState& receiver,SimulationInstant time) {
    require(time==receiver.snapshot_time,"W1 stale receiver state cannot be relabeled with another RF geometry time");
    if(receiver.height_datum!=HeightDatum::Wgs84Ellipsoid)throw std::invalid_argument("W1 receiver RF geometry unresolved: AMSL/AGL is not WGS84 ellipsoid; no geoid conversion");
    return {receiver.location,receiver.elevation_mm,time};
}
AntennaGeometry rf_geometry(const Snapshot& snapshot,VesselId id) {
    const auto found=std::find_if(snapshot.vessels.begin(),snapshot.vessels.end(),[&](const auto& item){return item.state.id==id;});
    require(found!=snapshot.vessels.end(),"W1 unknown vessel in RF geometry snapshot");
    return rf_geometry(*found,snapshot.time);
}
std::shared_ptr<const runtime::ReceiverRegistry> receiver_ownership(const Snapshot& snapshot) {
    std::vector<std::string> stations; std::vector<runtime::ReceiverOwnership> radios;
    for(const auto& vessel:snapshot.vessels) {
        require(vessel.time==snapshot.time,"W1 mixed-time vessel ownership snapshot");
        const auto station=station_name(vessel.state.id); stations.push_back(station);
        radios.push_back({{radio_name(vessel.state.radio_id)},runtime::ReceiverRole::TransceiverRadio,station,vessel.state.equipment_on});
    }
    for(const auto& receiver:snapshot.receivers) {
        require(receiver.snapshot_time==snapshot.time,"W1 mixed-time receiver ownership snapshot");
        radios.push_back({{passive_radio_name(receiver.region_id,receiver.ordinal)},runtime::ReceiverRole::PassiveTerrestrial,std::nullopt,receiver.equipment_on});
    }
    return std::make_shared<const runtime::ReceiverRegistry>(std::move(stations),std::move(radios));
}
BurstPowerConstraint inspect_burst(const Config& input,std::uint64_t equipment_key,SimulationInstant start,SimulationInstant end) {
    const auto config=canonical_config(input);
    require(!config.fresh_seed,"W1 unresolved fresh seed: resolve before inspecting burst constraints");
    require(start.units>=0 && end>start && end<=config.duration,"W1 invalid hypothetical burst interval");
    std::optional<PowerModel> model;
    for(const auto& vessel:config.vessels)if(vessel.equipment_id.value==equipment_key)model=vessel.power;
    require(model.has_value(),"W1 burst requires Class A equipment; passive/unknown equipment cannot transmit");
    Power power(*model,equipment_key,config.effective_seed); std::uint64_t transitions=0;
    while(power.next<=start) { if(++transitions>kMaxTransitions)throw std::runtime_error("W1 power inspection capacity exceeded"); const auto time=power.next; power.toggle(time); }
    BurstPowerConstraint result; result.may_start=power.on;
    if(power.on && power.next<end)result.first_off_before_end=power.next;
    return result;
}
const char* transition_name(TransitionKind kind) {
    switch(kind){case TransitionKind::WaypointArrival:return "waypoint_arrival";case TransitionKind::SpeedChanged:return "speed_changed";
    case TransitionKind::NavigationChanged:return "navigation_changed";case TransitionKind::ReachChanged:return "reach_changed";
    case TransitionKind::InitialOff:return "initial_network_entry_preparation";case TransitionKind::EquipmentOn:return "equipment_on";case TransitionKind::EquipmentOff:return "equipment_off";}
    throw std::invalid_argument("W1 unsupported transition kind");
}
}  // namespace aismixer::lab::world
