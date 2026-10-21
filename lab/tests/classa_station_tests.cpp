#include "station_assembly.h"
#include "station_inputs.h"
#include "ais_encoder.h"
#include "nmea0183.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <iostream>
#include <limits>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>
#include <tuple>
#include <utility>
#include <vector>

// Explicit SYNTHETIC lab input fixtures. These are neither licensed identities,
// physical GNSS samples, received AIS reports, nor successful radio emissions.
namespace {
namespace c = aismixer::lab::classa;
namespace a = aismixer::lab::ais;
namespace r = aismixer::lab::runtime;
namespace w = aismixer::lab::world;
std::map<std::string, unsigned> negatives;
unsigned assertions = 0;

void require(bool value, std::string_view message) {
    if (!value) throw std::runtime_error(std::string(message));
    ++assertions;
}
template<class F> void invalid(const char* category, F action) {
    try { action(); }
    catch (const std::invalid_argument& error) {
        require(std::string_view(error.what()).size() > 5, "failure lacks an actionable diagnostic");
        ++negatives[category]; return;
    }
    throw std::runtime_error(std::string("accepted invalid ") + category);
}
template<class F> void overflow(const char* category, F action) {
    try { action(); }
    catch (const std::overflow_error& error) {
        require(std::string_view(error.what()).size() > 5, "overflow lacks context");
        ++negatives[category]; return;
    }
    throw std::runtime_error(std::string("accepted overflowing ") + category);
}

w::Config synthetic_world() {
    w::Config result;
    result.profile_id = "SYNTHETIC-A1-S2-WORLD-v1";
    result.provenance = "Explicit synthetic station-input unit fixture; no RF evidence";
    result.duration = r::from_seconds(1800);
    result.waypoints = {{{1}, "SYNTHETIC POINT A", {27000000000LL, 43000000000LL}},
                        {{2}, "SYNTHETIC POINT B", {27100000000LL, 43100000000LL}}};
    for (unsigned index = 0; index < 2; ++index) {
        w::Vessel vessel;
        vessel.id = {11 + index}; vessel.equipment_id = {21 + index}; vessel.radio_id = {31 + index};
        vessel.name = index == 0 ? "SYNTHETIC W1 A" : "SYNTHETIC W1 B";
        vessel.initial = {w::SelectionMode::Fixed, {1}, {}};
        vessel.first_destination = {w::SelectionMode::Fixed, {2}, {}};
        vessel.subsequent_destination = {w::SelectionMode::All, {}, {}};
        vessel.speed_mkn = {8000, 8000};
        vessel.power = {{0, 0}, {600, 600}, {10, 10}};
        result.vessels.push_back(std::move(vessel));
    }
    return result;
}

c::StationDefinition definition(unsigned index) {
    a::Mmsi mmsi(index == 0 ? "000000001" : "000000002");
    a::Type5 voyage(mmsi, index == 0 ? 1234567 : 7654321);
    voyage.call_sign = index == 0 ? "LAB0001" : "LAB0002";
    voyage.name = index == 0 ? "SYNTHETIC CLASS A 1" : "SYNTHETIC CLASS A 2";
    voyage.ship_type = index == 0 ? 70 : 80;
    voyage.dimension_a = 100 + index; voyage.dimension_b = 20 + index;
    voyage.dimension_c = 5 + index; voyage.dimension_d = 6 + index;
    voyage.position_fixing_device = 15;
    voyage.eta_month = 2; voyage.eta_day = 29; voyage.eta_hour = 23; voyage.eta_minute = 59;
    voyage.draught_tenths_m = 41 + index;
    voyage.destination = index == 0 ? "SYNTHETIC PORT A" : "SYNTHETIC PORT B";
    voyage.dte_not_ready = false;
    c::StationDefinition result({{11 + index}, {21 + index}, {31 + index}}, mmsi,
        voyage, a::ManufacturerIdentification(index == 0 ? "LAB" : "TST", 1 + index, 123 + index));
    result.clock.source_id = index == 0 ? "SYNTHETIC-UTC-A" : "SYNTHETIC-UTC-B";
    result.clock.provenance = "Explicit independent mathematical ideal direct UTC test source";
    result.clock.anchor = {0, 0}; result.clock.anchor_revision = 7;
    result.input_policy.epfs_source_id = index == 0 ? "SYNTHETIC-EPFS-A" : "SYNTHETIC-EPFS-B";
    result.input_policy.provenance = "Own W1 truth as idealized EPFS; no GPS accuracy or integrity claim";
    result.input_policy.max_navigation_age_units = 2 * r::kUnitsPerSecond;
    result.input_policy.max_clock_age_units = r::kUnitsPerSecond;
    result.origin.scenario_id = "SYNTHETIC-A1-S2-WORLD-v1";
    result.origin.provenance = "SYNTHETIC metadata; syntax-only identities with no allocation assertion";
    return result;
}

std::vector<c::OwnStationConfig> configurations() {
    return c::assemble_stations(synthetic_world(), 17, {definition(0), definition(1)});
}

std::vector<c::OwnStationConfig> perturbed_configurations() {
    auto world=synthetic_world();
    std::reverse(world.vessels.begin(),world.vessels.end());
    auto extra=world.vessels.front();extra.id={99};extra.equipment_id={199};extra.radio_id={299};
    extra.name="UNRELATED SYNTHETIC W1 VESSEL";world.vessels.push_back(extra);
    auto station=definition(1);station.identity={{99},{199},{299}};
    station.mmsi=a::Mmsi("000000003");station.static_voyage.mmsi=station.mmsi;
    station.static_voyage.imo_or_flag_state_number=3456789;
    station.static_voyage.call_sign="LAB0003";station.static_voyage.name="SYNTHETIC CLASS A 3";
    station.static_voyage.destination="SYNTHETIC PORT C";
    station.manufacturer=a::ManufacturerIdentification("THR",3,125);
    station.clock.source_id="SYNTHETIC-UTC-C";
    station.input_policy.epfs_source_id="SYNTHETIC-EPFS-C";
    return c::assemble_stations(world,17,{station,definition(1),definition(0)});
}

struct ScalarCase { std::int64_t input, expected; };
const std::vector<ScalarCase> coordinate_cases = {
    {-12500,-8}, {-7500,-5}, {-5001,-3}, {-5000,-3}, {-3750,-2},
    {-2501,-2}, {-2500,-2}, {-2499,-1}, {-1250,-1}, {-835,-1}, {-834,-1},
    {-833,0}, {-1,0}, {0,0}, {1,0}, {833,0}, {834,1}, {835,1}, {1250,1},
    {2499,1}, {2500,2}, {2501,2}, {3750,2}, {4999,3}, {5000,3}, {5001,3},
    {7500,5}, {12500,8}
};
const std::vector<ScalarCase> speed_cases = {
    {0,0}, {1,0}, {49,0}, {50,1}, {51,1}, {99,1}, {100,1}, {149,1}, {150,2},
    {151,2}, {99949,999}, {99950,1000}, {100000,1000}, {102149,1021},
    {102150,1022}, {102249,1022}, {102250,1022}, {INT64_MAX,1022}
};
const std::vector<ScalarCase> course_cases = {
    {0,0}, {1,0}, {49999999,0}, {50000000,1}, {50000001,1}, {99999999,1},
    {100000000,1}, {123450000000LL,1235}, {180000000000LL,1800},
    {359899999999LL,3599}, {359900000000LL,3599}, {359949999999LL,3599},
    {359950000000LL,0}, {359950000001LL,0}, {359999999999LL,0}
};

void arithmetic() {
    for (const auto& fixture : coordinate_cases) {
        require(c::quantize_longitude(fixture.input) == fixture.expected, "literal signed longitude quantization");
        require(c::quantize_latitude(fixture.input) == fixture.expected, "literal signed latitude quantization");
    }
    for (const auto sign : {-1LL,1LL}) {
        for (const auto nano : {179999999999LL,180000000000LL})
            require(c::quantize_longitude(sign*nano) == sign*108000000LL, "longitude geographic bound");
        for (const auto nano : {79999999999LL,80000000000LL})
            require(c::quantize_latitude(sign*nano) == sign*48000000LL, "W1 latitude geographic bound");
    }
    for (const auto& fixture : speed_cases)
        require(c::quantize_speed(fixture.input) == fixture.expected, "literal speed quantization/saturation");
    require(c::quantize_speed(std::nullopt) == 1023, "unknown SOG distinct from zero and high speed");
    for (const auto& fixture : course_cases)
        require(c::quantize_course(fixture.input) == fixture.expected, "literal course quantization/wrap");
    require(c::quantize_course(std::nullopt) == 3600, "unknown COG distinct from valid north");
    for (const auto value : {-180000000001LL,180000000001LL,INT64_MIN,INT64_MAX})
        invalid("coordinate", [=] { (void)c::quantize_longitude(value); });
    for (const auto value : {-80000000001LL,80000000001LL,INT64_MIN,INT64_MAX})
        invalid("coordinate", [=] { (void)c::quantize_latitude(value); });
    for (const auto value : {-1LL,INT64_MIN})
        invalid("speed", [=] { (void)c::quantize_speed(value); });
    for (const auto value : {-1LL,360000000000LL,INT64_MIN,INT64_MAX})
        invalid("course", [=] { (void)c::quantize_course(value); });
}

template<class F> void bad_definition(const char* category, F mutate) {
    auto definitions = std::vector<c::StationDefinition>{definition(0), definition(1)};
    mutate(definitions);
    invalid(category, [&] { (void)c::assemble_stations(synthetic_world(), 17, definitions); });
}

void configuration_contract() {
    const auto own = configurations();
    require(own.size() == 2 && own[0].identity().vessel_id.value == 11 && own[1].identity().vessel_id.value == 12,
            "assembly did not project stable independent W1 owners");
    require(own[0].mmsi().digits() == "000000001" && own[1].mmsi().digits() == "000000002", "MMSI leading zeros");
    require(own[0].world_revision() == 17 && own[0].clock().source_id != own[1].clock().source_id,
            "world revision and per-station clock identity");
    require(own[0].origin().category == "SYNTHETIC_LAB" && own[0].origin().identity_validity == "SYNTAX_ONLY_NOT_ALLOCATION_AUTHORITY",
            "synthetic identity provenance");
    require(own[0].static_voyage().call_sign == "LAB0001" && own[1].static_voyage().call_sign == "LAB0002" &&
            own[0].manufacturer().vendor_id() == "LAB" && own[1].manufacturer().vendor_id() == "TST",
            "explicit metadata was inferred from W1 or crossed owners");
    const auto bits5a = a::encode(own[0].static_voyage());
    const auto bits5b = a::encode(own[1].static_voyage());
    require(bits5a.bits.size() == 424 && bits5a.fill_bits == 2 && bits5a.armored != bits5b.armored,
            "configured Type5 is explicit, distinct and remains a payload");
    const auto manufacturer = a::encode(a::ClassAType24B(own[0].mmsi(), own[0].manufacturer()));
    require(manufacturer.bits.size() == 168, "manufacturer-only ClassA24B integration");
    const auto reprojection = perturbed_configurations();
    require(reprojection.size()==3 && reprojection[2].mmsi().digits()=="000000003",
            "unrelated addition must be a genuine third station, not only a world actor");
    for (std::size_t index = 0; index < 2; ++index)
        require(reprojection[index].identity().vessel_id == own[index].identity().vessel_id &&
                reprojection[index].clock().source_id == own[index].clock().source_id &&
                a::encode(reprojection[index].static_voyage()).bits == a::encode(own[index].static_voyage()).bits,
                "unrelated vessel or definition reordering altered owned projection");
    {
        auto source=own.front();const auto moved=std::move(source);
        require(source.mmsi().digits()==moved.mmsi().digits() && source.clock().source_id==moved.clock().source_id &&
                a::encode(source.static_voyage()).bits==a::encode(moved.static_voyage()).bits,
                "moving accepted immutable config left a reusable invalid shell");
    }
    {
        auto source=definition(0);const auto retained=std::move(source.mmsi);
        require(retained.digits()=="000000001", "explicit MMSI move lost owned identity");
        if(source.mmsi.digits().empty())
            invalid("moved-from MMSI",[&]{(void)c::assemble_stations(synthetic_world(),17,{source});});
        // A valid unspecified moved-from representation is still valid; no
        // particular standard-library moved-from string state is assumed.
    }
    {
        auto source=definition(0);const auto retained=std::move(source.manufacturer);
        require(retained.vendor_id()=="LAB", "manufacturer move lost explicit metadata");
        if(source.manufacturer.vendor_id().empty())
            invalid("moved-from manufacturer",[&]{(void)c::assemble_stations(synthetic_world(),17,{source});});
    }

    for (const auto digits : {"000000000", "1", "00000000X", "0000000001", " 00000001"})
        invalid("MMSI syntax", [=] { (void)a::Mmsi(digits); });
    bad_definition("duplicate MMSI", [](auto& d) { d[1].mmsi=d[0].mmsi; d[1].static_voyage.mmsi=d[0].mmsi; });
    bad_definition("owner triple", [](auto& d) { d[0].identity.vessel_id={0}; });
    bad_definition("owner triple", [](auto& d) { d[0].identity.vessel_id={999}; });
    bad_definition("owner triple", [](auto& d) { d[0].identity.equipment_id={22}; });
    bad_definition("owner triple", [](auto& d) { d[0].identity.radio_id={32}; });
    bad_definition("duplicate owner", [](auto& d) { d[1].identity=d[0].identity; });
    bad_definition("metadata", [](auto& d) { d[0].static_voyage.mmsi=a::Mmsi("000000009"); });
    bad_definition("metadata", [](auto& d) { d[0].clock.source_id.clear(); });
    bad_definition("metadata", [](auto& d) { d[0].clock.provenance.clear(); });
    bad_definition("metadata", [](auto& d) { d[0].clock.quality="INDIRECT_UTC"; });
    bad_definition("metadata", [](auto& d) { d[0].clock.anchor_revision=0; });
    bad_definition("clock identity", [](auto& d) { d[1].clock.source_id=d[0].clock.source_id; });
    bad_definition("clock identity", [](auto& d) { d[1].clock.source_id=d[0].input_policy.epfs_source_id; });
    bad_definition("clock identity", [](auto& d) { d[1].clock.anchor.posix_seconds=1; });
    bad_definition("anchor", [](auto& d) { d[0].clock.anchor.subsecond_units=-1; });
    bad_definition("anchor", [](auto& d) { d[0].clock.anchor.subsecond_units=r::kUnitsPerSecond; });
    bad_definition("profile version", [](auto& d) { d[0].model_id="OTHER"; });
    bad_definition("profile version", [](auto& d) { d[0].schema_version=2; });
    bad_definition("profile version", [](auto& d) { d[0].profile_id="FULL_CLASS_A"; });
    bad_definition("profile version", [](auto& d) { d[0].excluded_functions.clear(); });
    bad_definition("profile version", [](auto& d) { d[0].input_policy.version=2; });
    bad_definition("profile version", [](auto& d) { d[0].input_policy.epoch_policy="REUSE_OLD"; });
    bad_definition("metadata", [](auto& d) { d[0].input_policy.epfs_source_id.clear(); });
    bad_definition("metadata", [](auto& d) { d[0].input_policy.provenance.clear(); });
    bad_definition("metadata", [](auto& d) { d[0].input_policy.epfs_category="ACTUAL_GPS"; });
    bad_definition("freshness policy", [](auto& d) { d[0].input_policy.max_navigation_age_units=-1; });
    bad_definition("freshness policy", [](auto& d) { d[0].input_policy.max_navigation_age_units=60*r::kUnitsPerSecond+1; });
    bad_definition("freshness policy", [](auto& d) { d[0].input_policy.max_clock_age_units=-1; });
    bad_definition("freshness policy", [](auto& d) { d[0].input_policy.max_clock_age_units=60*r::kUnitsPerSecond+1; });
    bad_definition("metadata", [](auto& d) { d[0].origin.category="LICENSED_REAL_WORLD"; });
    bad_definition("metadata", [](auto& d) { d[0].origin.identity_validity="ALLOCATED"; });
    bad_definition("metadata", [](auto& d) { d[0].origin.scenario_id.clear(); });
    bad_definition("metadata", [](auto& d) { d[0].origin.scenario_id="ANOTHER-SCENARIO"; });
    bad_definition("metadata", [](auto& d) { d[0].origin.provenance.clear(); });
    for (const auto number : {INT64_MIN,-1LL,0LL,1LL,999999LL,1073741824LL,INT64_MAX})
        bad_definition("Type5 IMO/flag qualified domain", [=](auto& d) { d[0].static_voyage.imo_or_flag_state_number=number; });
    bad_definition("Type5 field", [](auto& d) { d[0].static_voyage.call_sign="TOO-LONG"; });
    bad_definition("Type5 field", [](auto& d) { d[0].static_voyage.name="lowercase"; });
    bad_definition("Type5 field", [](auto& d) { d[0].static_voyage.eta_month=13; });
    bad_definition("Type5 field", [](auto& d) { d[0].static_voyage.dimension_a=512; });
    for (const auto vendor : {"", "AB", "ABCD", "abC", "A@C"})
        invalid("manufacturer", [=] { (void)a::ManufacturerIdentification(vendor,1,0); });
    for (const auto model : {0LL,16LL,INT64_MIN,INT64_MAX})
        invalid("manufacturer", [=] { (void)a::ManufacturerIdentification("LAB",model,0); });
    for (const auto serial : {-1LL,1048576LL,INT64_MIN,INT64_MAX})
        invalid("manufacturer", [=] { (void)a::ManufacturerIdentification("LAB",1,serial); });
    invalid("world revision", [] { (void)c::assemble_stations(synthetic_world(),0,{definition(0)}); });
    invalid("W1 canonical validation", [] { auto world=synthetic_world();world.schema_version=2; (void)c::assemble_stations(world,17,{definition(0)}); });
    invalid("W1 canonical validation", [] { auto world=synthetic_world();world.vessels[1].equipment_id={21}; (void)c::assemble_stations(world,17,{definition(0)}); });
}

auto fields_tuple(const c::OwnNavigationFields& value) {
    return std::make_tuple(value.longitude,value.latitude,value.sog_tenths_knots,
        value.cog_tenths_degrees,value.navigation_status,value.true_heading_degrees,
        value.rot_wire,value.utc_second,value.position_accuracy,value.raim);
}

struct Fixture {
    c::OwnStationConfig own;
    c::StationInputGate gate;
    explicit Fixture(unsigned index = 0) : own(configurations().at(index)), gate(own) {
        gate.apply_power({own.identity(),{0},c::OwnPowerKind::On,1});
    }
    explicit Fixture(c::OwnStationConfig config) : own(std::move(config)), gate(own) {
        gate.apply_power({own.identity(),{0},c::OwnPowerKind::On,1});
    }
    w::VesselSnapshot snapshot(r::SimulationInstant time, bool powered = true,
                               std::uint64_t generation = 1) const {
        w::VesselSnapshot result;
        result.state.id=own.identity().vessel_id;
        result.state.equipment_id=own.identity().equipment_id;
        result.state.radio_id=own.identity().radio_id;
        result.state.equipment_on=powered;
        result.state.power_generation=generation;
        result.state.speed_mkn=8150;
        result.state.nominal_speed_mkn=8150;
        result.state.navigation_status=w::NavigationStatus::UnderWayUsingEngine;
        result.state.course_valid=true;
        result.state.environment_revision=999;  // Not the explicit world instance revision 17.
        result.location={-2500,12500}; result.course_ndeg=359950000000LL; result.time=time;
        return result;
    }
    c::EpfsFix fix(r::SimulationInstant time, bool valid = true,
                    std::uint64_t generation = 1) const {
        return {own.identity(),generation,own.input_policy().epfs_source_id,
                own.clock().source_id,17,time,valid};
    }
    c::DirectUtcObservation clock(r::SimulationInstant time, bool valid = true,
                                  std::uint64_t generation = 1) const {
        c::DirectUtcObservation result;
        result.own_identity=own.identity(); result.power_generation=generation;
        result.source_id=own.clock().source_id; result.anchor=own.clock().anchor;
        result.anchor_revision=own.clock().anchor_revision; result.sampled_at=time; result.valid=valid;
        return result;
    }
};

std::uint64_t unsigned_field(const std::vector<std::uint8_t>& bits, std::size_t offset, unsigned count) {
    require(offset+count<=bits.size(), "independent field extraction bounds");
    std::uint64_t result=0;
    for (unsigned bit=0;bit<count;++bit) result=(result<<1)|bits[offset+bit];
    return result;
}
std::int64_t signed_field(const std::vector<std::uint8_t>& bits, std::size_t offset, unsigned count) {
    const auto value=unsigned_field(bits,offset,count);
    return (value & (UINT64_C(1)<<(count-1))) ? static_cast<std::int64_t>(value)-(INT64_C(1)<<count)
                                           : static_cast<std::int64_t>(value);
}

void public_boundary() {
    static_assert(!std::is_default_constructible_v<c::OwnStationConfig>);
    static_assert(!std::is_default_constructible_v<c::DirectUtcToken>);
    static_assert(!std::is_default_constructible_v<c::OwnNavigationInput>);
    static_assert(!std::is_default_constructible_v<c::QualifiedOwnInputs>);
    static_assert(!std::is_constructible_v<c::StationInputGate,w::Config>);
    static_assert(!std::is_constructible_v<c::StationInputGate,w::Snapshot>);
    using Adapt=decltype(&c::StationInputGate::adapt_navigation);
    static_assert(!std::is_invocable_v<Adapt,c::StationInputGate&,const w::Snapshot&,const c::EpfsFix&,r::SimulationInstant>);
    static_assert(!std::is_invocable_v<Adapt,c::StationInputGate&,const w::Config&,const c::EpfsFix&,r::SimulationInstant>);
    using Qualify=decltype(&c::StationInputGate::qualify_inputs);
    static_assert(!std::is_invocable_v<Qualify,c::StationInputGate&,w::Snapshot,c::DirectUtcObservation,r::SimulationInstant>);
    static_assert(std::is_same_v<decltype(std::declval<const c::OwnStationConfig&>().static_voyage()), const a::Type5&>);
    static_assert(!std::is_copy_constructible_v<c::StationInputGate>);
}

void own_navigation_and_integration() {
    Fixture fixture;
    const auto generated=r::SimulationInstant{r::kUnitsPerSecond+123};
    const auto observed=r::SimulationInstant{2*r::kUnitsPerSecond};
    const auto acquired=fixture.gate.accept_clock(fixture.clock(generated),generated);
    require(acquired.has_value(), "explicit direct UTC acquisition");
    const auto source=fixture.snapshot(generated);
    const auto nav=fixture.gate.adapt_navigation(source,fixture.fix(generated),observed);
    require(nav.fix_valid() && nav.fix_generated_at()==generated && nav.observed_at()==observed && nav.world_revision()==17,
            "fix generation and later observation time must remain independent");
    require(!nav.input_eligible() && !nav.ready_for_tx() && nav.fields().utc_second==60,
            "W1 truth alone must not manufacture UTC or transmit eligibility");
    const auto clock=fixture.gate.accept_clock(fixture.clock(observed),observed);
    const auto qualified=fixture.gate.qualify_inputs(nav,*clock,observed);
    const auto& field=qualified.fields();
    require(fields_tuple(field)==std::make_tuple(-2LL,8LL,82LL,0LL,0LL,511LL,-128LL,1LL,false,false),
            "literal own field conversion, no heading/ROT/integrity invention, fix UTC second 1 not observation second 2");
    require(qualified.input_eligible() && !qualified.ready_for_tx() && qualified.sync_state()==a::SyncState::UtcDirect,
            "qualified input is not passive-observation or entry success");
    require(qualified.provenance().epfs_source_id==fixture.own.input_policy().epfs_source_id &&
            qualified.provenance().clock_source_id==fixture.own.clock().source_id &&
            qualified.provenance().position_and_motion=="OWN_W1_TRUTH_AS_EPFS" &&
            qualified.provenance().heading_rot_integrity=="POLICY_UNAVAILABLE_NO_SENSOR_EVIDENCE",
            "own field provenance and policy sentinels");
    a::Type3 payload(fixture.own.mmsi(),a::ItdmaState19::from_effective(qualified.sync_state(),37,1,false));
    payload.longitude=field.longitude;payload.latitude=field.latitude;
    payload.sog_tenths_knots=field.sog_tenths_knots;payload.cog_tenths_degrees=field.cog_tenths_degrees;
    payload.navigation_status=field.navigation_status;payload.utc_second=field.utc_second;
    payload.true_heading_degrees=field.true_heading_degrees;payload.rot_wire=field.rot_wire;
    payload.position_accuracy=field.position_accuracy;payload.raim=field.raim;
    const auto encoded=a::encode(payload);
    require(encoded.bits.size()==168 && encoded.fill_bits==0 && unsigned_field(encoded.bits,0,6)==3 &&
            unsigned_field(encoded.bits,8,30)==1 && signed_field(encoded.bits,61,28)==-2 &&
            signed_field(encoded.bits,89,27)==8 && unsigned_field(encoded.bits,50,10)==82 &&
            unsigned_field(encoded.bits,116,12)==0 && unsigned_field(encoded.bits,137,6)==1 &&
            unsigned_field(encoded.bits,128,9)==511 && signed_field(encoded.bits,42,8)==-128 &&
            unsigned_field(encoded.bits,60,1)==0 && unsigned_field(encoded.bits,148,1)==0,
            "independent literal offsets for qualified S2 -> S1 payload integration");
    a::Type1 scheduled(fixture.own.mmsi(),a::SotdmaState19::offset(qualified.sync_state(),0).raw());
    scheduled.longitude=field.longitude;scheduled.latitude=field.latitude;scheduled.utc_second=field.utc_second;
    require(a::encode(scheduled).bits.size()==168, "context-free Type1 payload integration only");

    // Returned navigation is an owned value: its backing vector can disappear.
    Fixture stable;
    std::vector<w::VesselSnapshot> snapshots={stable.snapshot({0}),Fixture(1).snapshot({0})};
    const auto owned=stable.gate.adapt_navigation(snapshots.front(),stable.fix({0}),{0});
    std::reverse(snapshots.begin(),snapshots.end());snapshots.clear();snapshots.shrink_to_fit();
    require(owned.fields().longitude==-2 && owned.fields().latitude==8 && owned.identity()==stable.own.identity(),
            "owned navigation retained ephemeral world vector references");
    const auto changed=perturbed_configurations();
    for(unsigned index=0;index<2;++index) {
        Fixture original(index), reordered(changed[index]);
        const auto before=original.gate.adapt_navigation(original.snapshot({0}),original.fix({0}),{0});
        const auto after=reordered.gate.adapt_navigation(reordered.snapshot({0}),reordered.fix({0}),{0});
        require(before.identity()==after.identity() && fields_tuple(before.fields())==fields_tuple(after.fields()) &&
                before.provenance().epfs_source_id==after.provenance().epfs_source_id &&
                before.provenance().clock_source_id==after.provenance().clock_source_id &&
                after.fields().longitude==-2 && after.fields().latitude==8 && after.fields().sog_tenths_knots==82,
                "adding/reordering stations altered literal owned conversion or leaked peer metadata");
    }
    for (const auto status : {w::NavigationStatus::UnderWayUsingEngine,w::NavigationStatus::AtAnchor,w::NavigationStatus::RestrictedManoeuvrability}) {
        Fixture item;auto snapshot=item.snapshot({0});snapshot.state.navigation_status=status;
        snapshot.state.speed_mkn=0;snapshot.state.course_valid=false;snapshot.course_ndeg.reset();
        const auto input=item.gate.adapt_navigation(snapshot,item.fix({0}),{0});
        require(input.fields().navigation_status==static_cast<std::int64_t>(status) && input.fields().sog_tenths_knots==0 &&
                input.fields().cog_tenths_degrees==3600 && input.fields().true_heading_degrees==511,
                "preserve W1 status, no speed-zero moored inference or invented course/heading");
    }
    Fixture absent;
    const auto nofix=absent.gate.adapt_navigation(absent.snapshot({0}),absent.fix({0},false),{0});
    require(!nofix.fix_valid() && nofix.fields().longitude==a::kLongitudeUnavailable &&
            nofix.fields().latitude==a::kLatitudeUnavailable && nofix.fields().utc_second==60 &&
            !nofix.input_eligible() && !nofix.ready_for_tx(), "absent explicit valid EPFS must fail closed");
    const auto token=absent.gate.accept_clock(absent.clock({0}),{0});
    invalid("unavailable fix",[&]{(void)absent.gate.qualify_inputs(nofix,*token,{0});});
}

template<class F> void bad_snapshot(const char* category,F mutate) {
    Fixture fixture;auto source=fixture.snapshot({0});auto fix=fixture.fix({0});mutate(source,fix);
    invalid(category,[&]{(void)fixture.gate.adapt_navigation(source,fix,{0});});
}
template<class F> void bad_clock(const char* category,F mutate) {
    Fixture fixture;auto clock=fixture.clock({0});mutate(clock);
    invalid(category,[&]{(void)fixture.gate.accept_clock(clock,{0});});
}

void rejected_inputs() {
    bad_snapshot("snapshot owner",[](auto& s,auto&){s.state.id={12};});
    bad_snapshot("snapshot owner",[](auto& s,auto&){s.state.equipment_id={22};});
    bad_snapshot("snapshot owner",[](auto& s,auto&){s.state.radio_id={32};});
    bad_snapshot("fix owner",[](auto&,auto& f){f.own_identity.vessel_id={12};});
    bad_snapshot("fix owner",[](auto&,auto& f){f.own_identity.equipment_id={22};});
    bad_snapshot("fix owner",[](auto&,auto& f){f.own_identity.radio_id={32};});
    bad_snapshot("fix source",[](auto&,auto& f){f.source_id="SYNTHETIC-EPFS-B";});
    bad_snapshot("fix source",[](auto&,auto& f){f.clock_source_id="SYNTHETIC-UTC-B";});
    bad_snapshot("world revision",[](auto&,auto& f){f.world_revision=999;});
    bad_snapshot("world revision",[](auto&,auto& f){f.world_revision=0;});
    bad_snapshot("generation",[](auto& s,auto&){s.state.power_generation=2;});
    bad_snapshot("generation",[](auto&,auto& f){f.power_generation=2;});
    bad_snapshot("power mismatch",[](auto& s,auto&){s.state.equipment_on=false;});
    bad_snapshot("fix time",[](auto&,auto& f){f.generated_at={1};});
    bad_snapshot("future snapshot",[](auto& s,auto& f){s.time={1};f.generated_at={1};});
    bad_snapshot("invalid position",[](auto& s,auto&){s.location.longitude_ndeg=180000000001LL;});
    bad_snapshot("invalid position",[](auto& s,auto&){s.location.latitude_ndeg=-80000000001LL;});
    bad_snapshot("invalid position",[](auto& s,auto&){s.location.longitude_ndeg=INT64_MIN;});
    bad_snapshot("invalid W1 speed",[](auto& s,auto&){s.state.speed_mkn=-1;});
    bad_snapshot("invalid W1 speed",[](auto& s,auto&){s.state.speed_mkn=100001;});
    bad_snapshot("invalid W1 speed",[](auto& s,auto&){s.state.speed_mkn=INT64_MAX;});
    bad_snapshot("invalid W1 status",[](auto& s,auto&){s.state.navigation_status=static_cast<w::NavigationStatus>(5);});
    bad_snapshot("invalid W1 course",[](auto& s,auto&){s.course_ndeg=-1;});
    bad_snapshot("invalid W1 course",[](auto& s,auto&){s.course_ndeg=360000000000LL;});
    bad_snapshot("invalid W1 course",[](auto& s,auto&){s.course_ndeg=INT64_MAX;});
    bad_snapshot("invalid W1 course",[](auto& s,auto&){s.state.course_valid=false;});
    bad_snapshot("invalid W1 course",[](auto& s,auto&){s.course_ndeg.reset();});
    bad_clock("clock owner",[](auto& k){k.own_identity.vessel_id={12};});
    bad_clock("clock owner",[](auto& k){k.own_identity.equipment_id={22};});
    bad_clock("clock owner",[](auto& k){k.own_identity.radio_id={32};});
    bad_clock("clock source",[](auto& k){k.source_id="SYNTHETIC-UTC-B";});
    bad_clock("clock source",[](auto& k){k.category="UTC_INDIRECT";});
    bad_clock("clock source",[](auto& k){k.category="NO_UTC";});
    bad_clock("clock anchor",[](auto& k){k.anchor_revision=8;});
    bad_clock("clock anchor",[](auto& k){k.anchor.posix_seconds=1;});
    bad_clock("clock anchor",[](auto& k){k.anchor.subsecond_units=-1;});
    bad_clock("clock anchor",[](auto& k){k.anchor.subsecond_units=r::kUnitsPerSecond;});
    bad_clock("clock generation",[](auto& k){k.power_generation=0;});
    bad_clock("clock generation",[](auto& k){k.power_generation=2;});
    bad_clock("future clock",[](auto& k){k.sampled_at={1};});
    {
        Fixture fixture;
        invalid("stale snapshot",[&]{(void)fixture.gate.adapt_navigation(fixture.snapshot({0}),fixture.fix({0}),{2*r::kUnitsPerSecond+1});});
    }
    {
        Fixture fixture;
        invalid("stale clock",[&]{(void)fixture.gate.accept_clock(fixture.clock({0}),{r::kUnitsPerSecond+1});});
    }
    {
        Fixture fixture; (void)fixture.gate.accept_clock(fixture.clock({1}),{1});
        invalid("reversed time",[&]{(void)fixture.gate.adapt_navigation(fixture.snapshot({0}),fixture.fix({0}),{0});});
        invalid("reversed time",[&]{(void)fixture.gate.accept_clock(fixture.clock({0}),{0});});
    }
    {
        Fixture fixture;
        (void)fixture.gate.adapt_navigation(fixture.snapshot({2}),fixture.fix({2}),{2});
        invalid("reversed navigation source",[&]{(void)fixture.gate.adapt_navigation(fixture.snapshot({1}),fixture.fix({1}),{3});});
    }
    {
        Fixture fixture;
        (void)fixture.gate.accept_clock(fixture.clock({2}),{2});
        invalid("reversed clock source",[&]{(void)fixture.gate.accept_clock(fixture.clock({1}),{3});});
    }
    {
        Fixture first,second(1);
        const auto foreign=second.gate.accept_clock(second.clock({0}),{0});
        const auto nav=first.gate.adapt_navigation(first.snapshot({0}),first.fix({0}),{0});
        invalid("foreign token",[&]{(void)first.gate.qualify_inputs(nav,*foreign,{0});});
        const auto clock=first.gate.accept_clock(first.clock({0}),{0});
        const auto foreign_nav=second.gate.adapt_navigation(second.snapshot({0}),second.fix({0}),{0});
        invalid("foreign token",[&]{(void)first.gate.qualify_inputs(foreign_nav,*clock,{0});});
    }
    {
        Fixture first,duplicate;  // Even equal config is not the same live gate.
        const auto token=duplicate.gate.accept_clock(duplicate.clock({0}),{0});
        invalid("foreign gate token",[&]{(void)first.gate.current_utc(*token,{0});});
    }
}

void clocks_and_lifecycle() {
    Fixture first,second(1);
    const auto clock_a=first.gate.accept_clock(first.clock({0}),{0});
    const auto clock_b=second.gate.accept_clock(second.clock({0}),{0});
    const auto slot_a=first.gate.current_utc(*clock_a,{0});
    const auto slot_b=second.gate.current_utc(*clock_b,{0});
    require(slot_a.utc_minute==slot_b.utc_minute && slot_a.slot==slot_b.slot && slot_a.units_into_slot==slot_b.units_into_slot &&
            clock_a->source_id()!=clock_b->source_id() && clock_a->identity()!=clock_b->identity(),
            "independent ideal UTC sources must share physical UTC without sharing identity");
    const auto nav_a=first.gate.adapt_navigation(first.snapshot({0}),first.fix({0}),{0});
    const auto historical=first.gate.qualify_inputs(nav_a,*clock_a,{0});
    const auto loss=first.gate.accept_clock(first.clock({1},false),{1});
    require(!loss, "explicit loss must not mint UTC-direct token");
    invalid("revoked clock",[&]{(void)first.gate.current_utc(*clock_a,{1});});
    invalid("revoked envelope",[&]{(void)first.gate.revalidate(historical,{1});});
    require(second.gate.current_utc(*clock_b,{1}).slot==0, "one station's loss invalidated its independent peer");
    const auto reacquired=first.gate.accept_clock(first.clock({2}),{2});
    require(reacquired && reacquired->acquired_at().units==2, "explicit UTC reacquisition history");
    invalid("pre-reacquisition fix",[&]{(void)first.gate.fix_utc_second(*reacquired,{0},{2});});
    require(historical.fields().utc_second==0 && historical.clock().source_id()=="SYNTHETIC-UTC-A" &&
            historical.input_eligible(), "later revocation retroactively mutated historical owned evidence");
    {
        Fixture item;const auto token=item.gate.accept_clock(item.clock({0}),{0});
        require(item.gate.current_utc(*token,{r::kUnitsPerSecond}).slot==37, "inclusive clock freshness endpoint");
        invalid("expired clock token",[&]{(void)item.gate.current_utc(*token,{r::kUnitsPerSecond+1});});
    }
    {
        Fixture item;
        const auto nav=item.gate.adapt_navigation(item.snapshot({0}),item.fix({0}),{0});
        const auto token=item.gate.accept_clock(item.clock({2*r::kUnitsPerSecond}),{2*r::kUnitsPerSecond});
        // This token was first acquired later, so an old fix cannot acquire UTC retroactively.
        invalid("fix before clock acquisition",[&]{(void)item.gate.qualify_inputs(nav,*token,{2*r::kUnitsPerSecond});});
    }
    {
        Fixture item;
        const auto token=item.gate.accept_clock(item.clock({0}),{0});
        const auto nav=item.gate.adapt_navigation(item.snapshot({0}),item.fix({0}),{0});
        const auto old=item.gate.qualify_inputs(nav,*token,{0});
        item.gate.apply_power({item.own.identity(),r::from_seconds(10),c::OwnPowerKind::Off,1});
        invalid("OFF clock",[&]{(void)item.gate.current_utc(*token,r::from_seconds(10));});
        invalid("OFF envelope",[&]{(void)item.gate.revalidate(old,r::from_seconds(10));});
        const auto movement=item.gate.adapt_navigation(item.snapshot(r::from_seconds(10),false),item.fix(r::from_seconds(10)),r::from_seconds(10));
        require(movement.fix_valid() && movement.fields().longitude==-2 && !movement.equipment_on() &&
                !movement.input_eligible() && !movement.ready_for_tx(), "own navigation must continue while equipment OFF");
        invalid("OFF direct clock",[&]{(void)item.gate.accept_clock(item.clock(r::from_seconds(10)),r::from_seconds(10));});
        item.gate.apply_power({item.own.identity(),r::from_seconds(20),c::OwnPowerKind::On,2});
        invalid("old epoch clock",[&]{(void)item.gate.current_utc(*token,r::from_seconds(20));});
        invalid("old epoch envelope",[&]{(void)item.gate.revalidate(old,r::from_seconds(20));});
        invalid("old epoch source",[&]{(void)item.gate.accept_clock(item.clock(r::from_seconds(20)),r::from_seconds(20));});
        const auto fresh_clock=item.gate.accept_clock(item.clock(r::from_seconds(20),true,2),r::from_seconds(20));
        const auto fresh_nav=item.gate.adapt_navigation(item.snapshot(r::from_seconds(20),true,2),item.fix(r::from_seconds(20),true,2),r::from_seconds(20));
        const auto fresh=item.gate.qualify_inputs(fresh_nav,*fresh_clock,r::from_seconds(20));
        require(fresh.input_eligible() && !fresh.ready_for_tx() && fresh.navigation().power_generation()==2,
                "fresh ON+0 is input qualification, not transmit readiness");
        require(old.fields().utc_second==0 && old.navigation().power_generation()==1, "OFF/re-ON altered history");
    }
    {
        Fixture item;const auto token=item.gate.accept_clock(item.clock({0}),{0});
        const auto nav=item.gate.adapt_navigation(item.snapshot({0}),item.fix({0}),{0});
        const auto minted=item.gate.qualify_inputs(nav,*token,{0});
        item.gate.apply_power({item.own.identity(),{0},c::OwnPowerKind::Off,1});
        invalid("same-instant OFF wins",[&]{(void)item.gate.revalidate(minted,{0});});
        invalid("same-instant ON suppressed",[&]{item.gate.apply_power({item.own.identity(),{0},c::OwnPowerKind::On,2});});
        require(!item.gate.equipment_on(), "same-instant OFF then ON must not permit newly emitted work");
    }
    {
        const auto own=configurations().front();c::StationInputGate gate(own);
        gate.apply_power({own.identity(),{0},c::OwnPowerKind::Off,1});
        invalid("OFF-first ON suppressed",[&]{gate.apply_power({own.identity(),{0},c::OwnPowerKind::On,1});});
        invalid("OFF-first ON suppressed",[&]{gate.apply_power({own.identity(),{0},c::OwnPowerKind::On,2});});
        require(!gate.equipment_on(), "first authoritative OFF must latch before later same-instant ON");
    }
    {
        Fixture item;
        const auto at60=r::from_seconds(60);
        const auto clock=item.gate.accept_clock(item.clock(at60),at60);
        const auto nav=item.gate.adapt_navigation(item.snapshot(at60),item.fix(at60),at60);
        const auto envelope=item.gate.qualify_inputs(nav,*clock,at60);
        require(!envelope.ready_for_tx(), "ON+60 is not network-entry or passive-observation acceptance");
        item.gate.apply_power({item.own.identity(),at60,c::OwnPowerKind::Off,1});
        invalid("observation-boundary OFF wins",[&]{(void)item.gate.revalidate(envelope,at60);});
    }
    {
        Fixture item;
        auto owner=item.own.identity();owner.radio_id={32};
        invalid("power owner",[&]{item.gate.apply_power({owner,{1},c::OwnPowerKind::Off,1});});
        invalid("power generation",[&]{item.gate.apply_power({item.own.identity(),{1},c::OwnPowerKind::Off,0});});
        invalid("power generation",[&]{item.gate.apply_power({item.own.identity(),{1},c::OwnPowerKind::On,1});});
    }
}

struct ClockCase { std::int64_t instant, seconds, phase; };
const std::vector<ClockCase> clock_cases = {
    {0,0,0},{1,0,0},{-1,0,0},{262143,0,0},{262144,0,0},{262145,0,0},
    {589561855,0,0},{589561856,0,0},{589561857,0,0},
    {589823999,0,0},{589824000,0,0},{589824001,0,0},
    {0,-1,0},{0,-60,0},{0,-61,0},{0,59,1},{1,59,1},{-1,59,1},
    {0,0,9830399},{1,0,9830399},{0,60,0},{0,-120,131073},
    {589824000,59,1},{-589824000,59,1},{INT64_MAX,0,0},{INT64_MIN,0,0}
};

void emit_vectors() {
    std::cout<<"A1-S2|SYNTHETIC_INPUT_FIXTURE|1\n";
    for(const auto& row:coordinate_cases) {
        std::cout<<"QUANT|longitude|"<<row.input<<'|'<<c::quantize_longitude(row.input)<<'\n';
        std::cout<<"QUANT|latitude|"<<row.input<<'|'<<c::quantize_latitude(row.input)<<'\n';
    }
    for(const auto sign:{-1LL,1LL}) {
        for(const auto value:{179999999999LL,180000000000LL})
            std::cout<<"QUANT|longitude|"<<sign*value<<'|'<<c::quantize_longitude(sign*value)<<'\n';
        for(const auto value:{79999999999LL,80000000000LL})
            std::cout<<"QUANT|latitude|"<<sign*value<<'|'<<c::quantize_latitude(sign*value)<<'\n';
    }
    for(const auto& row:speed_cases) std::cout<<"QUANT|speed|"<<row.input<<'|'<<c::quantize_speed(row.input)<<'\n';
    std::cout<<"QUANT|speed|UNKNOWN|"<<c::quantize_speed(std::nullopt)<<'\n';
    for(const auto& row:course_cases) std::cout<<"QUANT|course|"<<row.input<<'|'<<c::quantize_course(row.input)<<'\n';
    std::cout<<"QUANT|course|UNKNOWN|"<<c::quantize_course(std::nullopt)<<'\n';
    for(const auto& row:clock_cases) {
        auto configured=definition(0);configured.clock.anchor={row.seconds,row.phase};
        const auto own=c::assemble_stations(synthetic_world(),17,{configured}).front();
        c::StationInputGate gate(own);
        gate.apply_power({own.identity(),{std::min<std::int64_t>(0,row.instant)},c::OwnPowerKind::On,1});
        c::DirectUtcObservation observation;
        observation.own_identity=own.identity();observation.power_generation=1;observation.source_id=own.clock().source_id;
        observation.anchor=own.clock().anchor;observation.anchor_revision=7;observation.sampled_at={row.instant};observation.valid=true;
        const auto token=gate.accept_clock(observation,{row.instant});
        const auto slot=gate.current_utc(*token,{row.instant});
        const auto second=gate.fix_utc_second(*token,{row.instant},{row.instant});
        require(r::compose_utc(slot,own.clock().anchor).units==row.instant,"exact UTC compose/decompose roundtrip");
        std::cout<<"UTC|"<<row.instant<<'|'<<row.seconds<<'|'<<row.phase<<'|'<<slot.utc_minute<<'|'<<slot.slot
                 <<'|'<<slot.units_into_slot<<'|'<<second<<'\n';
    }
}

void time_overflows() {
    invalid("runtime UTC anchor",[]{(void)r::decompose_utc({0},{0,-1});});
    invalid("runtime UTC normalization",[]{(void)r::compose_utc({0,2250,0},{0,0});});
    overflow("checked time overflow",[]{(void)r::decompose_utc({r::kUnitsPerSecond},{INT64_MAX,0});});
    overflow("checked time underflow",[]{(void)r::decompose_utc({-r::kUnitsPerSecond},{INT64_MIN,0});});
    overflow("checked time overflow",[]{(void)r::compose_utc({INT64_MAX,0,0},{0,0});});
    overflow("checked time underflow",[]{(void)r::compose_utc({INT64_MIN,0,0},{0,0});});
    {
        auto selected=definition(0);selected.clock.anchor={INT64_MAX-1800,0};
        const auto own=c::assemble_stations(synthetic_world(),17,{selected}).front();
        c::StationInputGate gate(own);gate.apply_power({own.identity(),{0},c::OwnPowerKind::On,1});
        c::DirectUtcObservation clock;clock.own_identity=own.identity();clock.power_generation=1;
        clock.source_id=own.clock().source_id;clock.anchor=own.clock().anchor;clock.anchor_revision=7;
        clock.sampled_at={INT64_MAX};clock.valid=true;
        overflow("gate UTC mapping overflow",[&]{(void)gate.accept_clock(clock,{INT64_MAX});});
    }
    {
        const auto own=configurations().front();c::StationInputGate gate(own);
        gate.apply_power({own.identity(),{INT64_MIN},c::OwnPowerKind::On,1});
        c::DirectUtcObservation clock;clock.own_identity=own.identity();clock.power_generation=1;
        clock.source_id=own.clock().source_id;clock.anchor=own.clock().anchor;clock.anchor_revision=7;
        clock.sampled_at={INT64_MIN};clock.valid=true;
        const auto token=gate.accept_clock(clock,{INT64_MIN});
        overflow("gate freshness subtraction overflow",[&]{(void)gate.current_utc(*token,{INT64_MAX});});
    }
}

}  // namespace

int main(int argc,char** argv) {
    try {
        if(argc==2 && std::string_view(argv[1])=="--vectors") { emit_vectors();return 0; }
        if(argc!=1) throw std::invalid_argument("usage: classa_station_tests [--vectors]");
        public_boundary();arithmetic();configuration_contract();own_navigation_and_integration();
        rejected_inputs();clocks_and_lifecycle();time_overflows();
        unsigned total=0;
        for(const auto& row:negatives) {std::cout<<"NEGATIVE|"<<row.first<<'|'<<row.second<<'\n';total+=row.second;}
        std::cout<<"A1-S2 synthetic station-input tests PASS: "<<assertions<<" assertions, "<<total
                 <<" categorized invalid fixtures rejected; no transmission or RF observation produced\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<"A1-S2 test failure: "<<error.what()<<'\n';return 1;
    }
}
