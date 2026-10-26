#include "access_engine.h"
#include "station_assembly.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <tuple>
#include <type_traits>
#include <vector>

// Every observation and local-pressure reservation below is an explicit
// SYNTHETIC UNIT_FIXTURE. No radio, FCS, receiver, network or air event exists.
namespace aismixer::lab::classa {
OwnedRadioEvidence UnitFixtureEvidenceAccess::mint(OwnRadioObservation observation) {
    observation.provenance = EvidenceProvenance::UnitFixture;
    return OwnedRadioEvidence(std::move(observation));
}
}

namespace {
namespace c = aismixer::lab::classa;
namespace a = aismixer::lab::ais;
namespace r = aismixer::lab::runtime;
namespace w = aismixer::lab::world;
constexpr std::uint64_t kWorldRevision = 29;
constexpr std::int64_t kDisplayBase = INT64_C(71249998500);
unsigned assertions = 0;
std::map<std::string, unsigned> negatives;

void require(bool condition, std::string_view reason) {
    ++assertions;
    if (!condition) throw std::runtime_error(std::string(reason));
}
template<class F> void invalid(const char* category, F action) {
    try { action(); }
    catch (const std::invalid_argument& error) {
        require(std::string_view(error.what()).size() > 5, "missing invalid-input diagnostic");
        ++negatives[category]; return;
    }
    catch (const std::overflow_error& error) {
        require(std::string_view(error.what()).size() > 5, "missing overflow diagnostic");
        ++negatives[category]; return;
    }
    throw std::runtime_error(std::string("accepted invalid ") + category);
}
template<class F> void section(const char* name, F action) {
    try { action(); }
    catch (const std::exception& error) { throw std::runtime_error(std::string(name) + ": " + error.what()); }
}

w::Config synthetic_world(std::uint64_t seed) {
    w::Config world;
    world.profile_id = "SYNTHETIC-A1-S4-A-WORLD-v1";
    world.provenance = "Explicit input-only synthetic fixture; no RF or allocation authority";
    world.duration = r::from_seconds(7200); world.effective_seed = seed;
    world.waypoints = {{{1}, "SYNTHETIC POINT A", {27000000000LL,43000000000LL}},
                       {{2}, "SYNTHETIC POINT B", {27100000000LL,43100000000LL}}};
    for (unsigned index = 0; index < 3; ++index) {
        w::Vessel vessel;
        vessel.id = {11 + index}; vessel.equipment_id = {21 + index}; vessel.radio_id = {31 + index};
        vessel.name = std::string("SYNTHETIC VESSEL ") + static_cast<char>('A' + index);
        vessel.initial = {w::SelectionMode::Fixed,{1},{}};
        vessel.first_destination = {w::SelectionMode::Fixed,{2},{}};
        vessel.subsequent_destination = {w::SelectionMode::All,{},{}};
        vessel.speed_mkn = {8000,8000}; vessel.power = {{0,0},{600,600},{10,10}};
        world.vessels.push_back(std::move(vessel));
    }
    return world;
}
c::StationDefinition definition(unsigned index, r::UtcAnchor anchor) {
    const char* mmsis[] = {"000000001","000000002","000000003"};
    a::Mmsi mmsi(mmsis[index]); a::Type5 voyage(mmsi,1234567 + index);
    voyage.call_sign = "LAB0001"; voyage.name = "SYNTHETIC ACCESS"; voyage.ship_type = 70;
    c::StationDefinition result({{11 + index},{21 + index},{31 + index}},mmsi,voyage,
                                a::ManufacturerIdentification("LAB",1,100 + index));
    result.clock.source_id = std::string("SYNTHETIC-UTC-") + static_cast<char>('A' + index);
    result.clock.provenance = "Independent mathematical ideal direct UTC source";
    result.clock.anchor = anchor; result.clock.anchor_revision = 7;
    result.input_policy.epfs_source_id = std::string("SYNTHETIC-EPFS-") + static_cast<char>('A' + index);
    result.input_policy.provenance = "Own truth-fed simulation EPFS; no sensor integrity claim";
    result.origin.scenario_id = "SYNTHETIC-A1-S4-A-WORLD-v1";
    result.origin.provenance = "SYNTHETIC syntax-only identities and static metadata";
    return result;
}
c::OwnStationConfig configuration(unsigned index = 0, r::UtcAnchor anchor = {1900000000,0}) {
    return c::assemble_stations(synthetic_world(42),kWorldRevision,{definition(index,anchor)}).front();
}
c::ScenarioSeedHandoff seed_handoff(std::uint64_t seed) {
    return c::project_scenario_seed(synthetic_world(seed),kWorldRevision);
}
c::AccessPolicy fixed_rate(std::int64_t numerator = 6, std::int64_t denominator = 1) {
    c::AccessPolicy policy; policy.rate_source = c::RateSource::FixedFixture;
    policy.fixed_rate_numerator = numerator; policy.fixed_rate_denominator = denominator;
    policy.rate_provenance = "SYNTHETIC_FIXED_RATE_UNIT_FIXTURE_NOT_AUTOMATIC_TABLE1_INFERENCE";
    return policy;
}
r::SimulationInstant at_index(std::int64_t index, r::UtcAnchor anchor) {
    const auto slot = c::slot_from_index(index);
    return r::compose_utc({slot.utc_minute,slot.slot,0},anchor);
}
std::int64_t index_at(r::SimulationInstant now, r::UtcAnchor anchor) {
    return c::absolute_index(c::slot_from_utc(r::decompose_utc(now,anchor)));
}

struct Station {
    c::OwnStationConfig own;
    c::StationInputGate gate;
    c::StationKnowledge knowledge;
    c::ClassAAccessEngine engine;
    std::int64_t speed_mkn = 8000;
    w::NavigationStatus status = w::NavigationStatus::UnderWayUsingEngine;
    Station(const c::OwnStationConfig& config, std::uint64_t seed = 1062,
            c::AccessPolicy policy = fixed_rate(), c::KnowledgeLimits limits = {})
        : own(config),gate(own),knowledge(own,gate,seed_handoff(seed),limits),
          engine(own,gate,knowledge,std::move(policy)) {}
    c::EngineStep power(r::SimulationInstant time, c::OwnPowerKind kind, std::uint64_t generation) {
        const c::OwnPowerNotice notice{own.identity(),time,kind,generation};
        gate.apply_power(notice); knowledge.apply_power(notice); return engine.apply_power(notice);
    }
    c::QualifiedOwnInputs qualify(r::SimulationInstant now) {
        c::DirectUtcObservation observation;
        observation.own_identity = own.identity(); observation.power_generation = gate.power_generation();
        observation.source_id = own.clock().source_id; observation.anchor = own.clock().anchor;
        observation.anchor_revision = own.clock().anchor_revision; observation.sampled_at = now; observation.valid = true;
        const auto clock = gate.accept_clock(observation,now);
        w::VesselSnapshot snapshot;
        snapshot.state.id = own.identity().vessel_id; snapshot.state.equipment_id = own.identity().equipment_id;
        snapshot.state.radio_id = own.identity().radio_id; snapshot.state.equipment_on = gate.equipment_on();
        snapshot.state.power_generation = gate.power_generation(); snapshot.state.speed_mkn = speed_mkn;
        snapshot.state.navigation_status = status; snapshot.state.course_valid = true;
        snapshot.course_ndeg = 90000000000LL; snapshot.location = {27000000000LL,43000000000LL}; snapshot.time = now;
        const c::EpfsFix fix{own.identity(),gate.power_generation(),own.input_policy().epfs_source_id,
                             own.clock().source_id,kWorldRevision,now,true};
        const auto navigation = gate.adapt_navigation(snapshot,fix,now);
        return gate.qualify_inputs(navigation,*clock,now);
    }
    c::EngineStep step(r::SimulationInstant now) { const auto inputs = qualify(now); return engine.step(inputs,now); }
};

std::optional<r::SimulationInstant> next_due(const Station& station) {
    std::optional<r::SimulationInstant> result;
    for (const auto channel : {c::Channel::Ais1,c::Channel::Ais2}) {
        const auto& plan = station.engine.channel_plan(channel);
        if (plan.next_due_index) {
            const auto instant = at_index(*plan.next_due_index,station.own.clock().anchor);
            if (!result || instant < *result) result = instant;
        }
    }
    return result;
}
std::uint64_t field(const std::vector<std::uint8_t>& bits, std::size_t offset, unsigned width) {
    require(offset + width <= bits.size(),"payload interval outside data");
    std::uint64_t result = 0;
    for (unsigned bit = 0; bit < width; ++bit) result = result * 2 + bits[offset + bit];
    return result;
}
void check_intent(const Station& station, const c::TxIntent& intent, r::SimulationInstant now) {
    require(intent.owner == station.own.identity() && intent.power_generation == station.gate.power_generation(),"due owner/epoch");
    require(intent.status == c::IntentStatus::Due && intent.slot_start == now && intent.block_length == 1,"due != proposal/physical event");
    require(at_index(intent.first_slot_index,station.own.clock().anchor) == now,"own-anchor exact slot boundary");
    require(intent.payload.bits.size() == 168 && intent.payload.fill_bits == 0,"Type3 data-bit width");
    require(field(intent.payload.bits,0,6) == 3 && field(intent.payload.bits,149,19) == intent.communication_state_19,"independent Type3/state offsets");
    require(field(intent.payload.bits,128,9) == 511 && field(intent.payload.bits,42,8) == 128 &&
            field(intent.payload.bits,60,1) == 0 && field(intent.payload.bits,148,1) == 0,"no invented heading/ROT/PA/RAIM");
    require((intent.communication_state_19 & 1) == 1,"first-frame keep true");
    require(((intent.communication_state_19 >> 1) & 7) == 0,"one-slot count uses ordinary code0");
    require(intent.knowledge_revision != 0 && intent.inputs_fingerprint != 0 && !intent.provenance.epfs_source_id.empty(),"decision own provenance");
    require(!station.gate.config().mmsi().digits().empty() && !station.knowledge.ready_for_tx(),"S3 readiness stays false");
    if (intent.message == c::IntentMessage::Type3Terminal) {
        require(intent.communication_state_19 == 1 && !intent.next_slot_index,"terminal ordinary zero word");
    } else {
        require(intent.next_slot_index && *intent.next_slot_index > intent.first_slot_index,"forward same-channel successor");
        require((intent.communication_state_19 >> 4) == static_cast<std::uint64_t>(*intent.next_slot_index - intent.first_slot_index),"advertise actual selected difference");
    }
}

// Public S3 local-intention pressure, NOT an empty-network claim. Every other
// SI slot is occupied by explicit synthetic own blocks; exactly one legal
// candidate remains. S3 honestly reports its one-candidate shortfall; no fourth
// candidate or choose-NTS hook is manufactured. Pressure records are exported.
struct Pressure { c::Channel channel; std::int64_t first; std::uint8_t length; };
void pressure_range(Station& station, const c::QualifiedOwnInputs& inputs, r::SimulationInstant now,
                    c::Channel channel, std::int64_t first, std::int64_t last,
                    std::vector<Pressure>* output = nullptr) {
    while (first <= last) {
        auto length = std::min<std::int64_t>(5,last - first + 1);
        length = std::min<std::int64_t>(length,c::kSlotsPerFrame - c::frame_slot(first));
        const c::OwnReservationRequest request{channel,first,static_cast<std::uint8_t>(length),
            c::OwnReservationKind::Sotdma,0,false,"UNIT_FIXTURE_ACCESS_PRESSURE"};
        (void)station.knowledge.reserve_own(request,inputs,now);
        if (output) output->push_back({channel,first,static_cast<std::uint8_t>(length)});
        first += length;
    }
}
std::vector<Pressure> nominal_pressure(Station& station, r::SimulationInstant now,
                                     std::int64_t nss_a, std::int64_t ni, unsigned rr) {
    const auto inputs = station.qualify(now); std::vector<Pressure> result;
    for (const auto channel : {c::Channel::Ais1,c::Channel::Ais2}) {
        const auto first = nss_a + (channel == c::Channel::Ais2 ? ni : 0);
        for (unsigned n = 0; n < rr / 2; ++n) {
            const auto ns = first + static_cast<std::int64_t>(n) * 2 * ni;
            pressure_range(station,inputs,now,channel,ns - ni / 10,ns - 1,&result);
            pressure_range(station,inputs,now,channel,ns + 1,ns + ni / 10,&result);
        }
    }
    return result;
}
std::vector<c::TxIntent> complete(Station& station, unsigned rr) {
    std::vector<c::TxIntent> due;
    for (unsigned step = 0; step < rr + 2; ++step) {
        const auto next = next_due(station); require(next.has_value(),"missing next first-frame timer");
        const auto result = station.step(*next);
        require(result.intents.size() == 1,"exactly one shared-TX due intent per timer");
        check_intent(station,result.intents.front(),*next); due.push_back(result.intents.front());
        const auto ledger_count = station.knowledge.ledger_issued();
        const auto duplicate = station.step(*next);
        require(duplicate.intents.empty() && duplicate.proposals.empty() && station.knowledge.ledger_issued() == ledger_count,"duplicate step does not publish/burn draw");
    }
    require(station.engine.state() == c::AccessState::Continuous && !next_due(station),"both zeros produce marker only");
    unsigned zeros[2] = {};
    for (std::size_t i = 0; i < due.size(); ++i) {
        const auto& value = due[i];
        if (value.message == c::IntentMessage::Type3Terminal) ++zeros[static_cast<unsigned>(value.channel)];
        if (i && value.channel != due[i - 1].channel)
            require(value.first_slot_index - due[i - 1].first_slot_index > 1,"one shared TX opposite-channel adjacency");
    }
    require(zeros[0] == 1 && zeros[1] == 1,"two independent terminal zeros");
    for (const auto& draw : station.knowledge.ledger())
        require(draw.key.purpose != c::ProtocolPurpose::GenericRatdmaProbability,"no generic RATDMA at entry");
    return due;
}

void public_contracts() {
    static_assert(!std::is_constructible_v<c::ClassAAccessEngine,w::Config>);
    static_assert(!std::is_copy_constructible_v<c::ClassAAccessEngine>);
    static_assert(!std::is_default_constructible_v<c::OwnedRadioEvidence>);
    require(c::kAccessEngineVersion == 1 && std::string_view(c::kFirstFramePolicyId) == "CLASSA-FIRST-FRAME-v1","versioned bounded policy");
    const auto own = configuration(); c::StationInputGate gate(own); c::StationKnowledge knowledge(own,gate,seed_handoff(42));
    for (unsigned which = 0; which < 5; ++which) {
        auto policy = fixed_rate();
        if (which == 0) policy.version = 2;
        if (which == 1) policy.first_frame_policy_id = "UNKNOWN";
        if (which == 2) policy.entry_retry_limit = 0;
        if (which == 3) policy.entry_retry_limit = 257;
        if (which == 4) policy.rate_provenance.clear();
        invalid("policy",[&] { c::ClassAAccessEngine engine(own,gate,knowledge,policy); });
    }
    auto foreign = configuration(1);
    invalid("constructor owner",[&] { c::ClassAAccessEngine engine(foreign,gate,knowledge); });
    c::StationInputGate shadow(own); c::ClassAAccessEngine shadow_engine(own,shadow,knowledge,fixed_rate());
    const c::OwnPowerNotice on{own.identity(),r::from_seconds(7),c::OwnPowerKind::On,1};
    gate.apply_power(on); shadow.apply_power(on); knowledge.apply_power(on); shadow_engine.apply_power(on);
    const auto now = r::from_seconds(67);
    c::DirectUtcObservation clock_observation;
    clock_observation.own_identity = own.identity(); clock_observation.power_generation = 1;
    clock_observation.source_id = own.clock().source_id; clock_observation.anchor = own.clock().anchor;
    clock_observation.anchor_revision = own.clock().anchor_revision; clock_observation.sampled_at = now; clock_observation.valid = true;
    const auto shadow_clock = shadow.accept_clock(clock_observation,now);
    w::VesselSnapshot source;
    source.state.id = own.identity().vessel_id; source.state.equipment_id = own.identity().equipment_id;
    source.state.radio_id = own.identity().radio_id; source.state.equipment_on = true; source.state.power_generation = 1;
    source.state.speed_mkn = 8000; source.state.course_valid = true; source.course_ndeg = 90000000000LL;
    source.location = {27000000000LL,43000000000LL}; source.time = now;
    const auto navigation = shadow.adapt_navigation(source,{own.identity(),1,own.input_policy().epfs_source_id,
        own.clock().source_id,kWorldRevision,now,true},now);
    const auto shadow_inputs = shadow.qualify_inputs(navigation,*shadow_clock,now);
    // This is valid for the engine's supplied gate but NOT for knowledge's
    // private bound gate. Both revalidations must run before any draw.
    invalid("shadow gate binding",[&] { (void)shadow_engine.step(shadow_inputs,now); });
    require(knowledge.ledger_issued() == 0 && shadow_engine.history().empty(),"shadow binding fails before draws/intent");
}
void frozen_fixtures() {
    // Frozen Section 6 uses POSIX1900000000. Its labels are relative to M0,
    // not S3's absolute indices. The brief's anchor{0,0} restatement is tested
    // separately below without silently offsetting the real slot identity.
    Station f1(configuration(),1062); f1.power(r::from_seconds(7),c::OwnPowerKind::On,1);
    const auto early = f1.step(r::add_units(r::from_seconds(67),-1));
    require(early.intents.empty() && early.proposals.empty() && f1.knowledge.ledger_issued() == 0,"no pre60 draw/intent");
    nominal_pressure(f1,r::from_seconds(67),kDisplayBase + 4162,375,6);
    const auto entry = f1.step(r::from_seconds(67));
    require(entry.intents.empty() && entry.proposals.size() == 2 && entry.state == c::AccessState::EntryWait,"future proposals are not due intents");
    require(f1.engine.channel_plan(c::Channel::Ais1).nss_index == kDisplayBase + 4162 &&
            f1.engine.channel_plan(c::Channel::Ais2).nss_index == kDisplayBase + 4537,"F1 real absolute NSS pair");
    require(next_due(f1).value().units == 697827328,"F1 firstA hand-worked instant");
    const auto due = complete(f1,6);
    const std::int64_t labels[] = {4162,4537,4912,5287,5662,6037,6412,6787};
    const std::int64_t units[] = {697827328,796131328,894435328,992739328,1091043328,1189347328,1287651328,1385955328};
    for (std::size_t i = 0; i < due.size(); ++i) {
        require(due[i].first_slot_index == kDisplayBase + labels[i] && due[i].slot_start.units == units[i],"F1 exact labelled/absolute slot and instant");
        require(due[i].communication_state_19 == (i < 6 ? 12001u : 1u),"F1 literal raw19 words");
    }
    const auto extra = f1.step(r::from_seconds(151));
    require(extra.intents.empty() && extra.proposals.empty(),"S4-A no continuous Type1");
    Station f2(configuration(),939); f2.power(r::from_seconds(10),c::OwnPowerKind::On,1);
    nominal_pressure(f2,r::from_seconds(70),kDisplayBase + 4499,375,6); f2.step(r::from_seconds(70));
    const auto wrapped = complete(f2,6); std::vector<std::int64_t> channel_a;
    for (const auto& intent : wrapped) if (intent.channel == c::Channel::Ais1) channel_a.push_back(intent.first_slot_index - kDisplayBase);
    require(channel_a == std::vector<std::int64_t>{4499,5249,5999,6749},"F2 own first-NTS anchor survives minute rollover");
    Station zero(configuration(0,{0,0}),1062); zero.power(r::from_seconds(7),c::OwnPowerKind::On,1);
    nominal_pressure(zero,r::from_seconds(67),2662,375,6); zero.step(r::from_seconds(67));
    require(zero.engine.channel_plan(c::Channel::Ais1).first_nts_index == 2662,"zero-anchor corrected F1 absolute index");
    require(next_due(zero).value().units == 697827328,"zero-anchor same instant, different actual index");
    (void)complete(zero,6);
}
void rates_and_non_nominal() {
    for (const auto rr : {6,10,18,30}) {
        Station station(configuration(0,{0,0}),42,fixed_rate(rr)); station.power({0},c::OwnPowerKind::On,1);
        station.step(r::from_seconds(60)); const auto snapshot = station.engine.snapshot();
        require(snapshot.reports_per_minute == rr && snapshot.nominal_increment == 2250 / rr,"supported fixed rate and exact NI");
        const auto due = complete(station,static_cast<unsigned>(rr));
        require(due.size() == static_cast<std::size_t>(rr + 2),"Rr/2 nonterminal plus one terminal per channel");
        bool actual_not_nominal = false;
        for (const auto& intent : due) if (intent.next_slot_index &&
            *intent.next_slot_index - intent.first_slot_index != 2 * snapshot.nominal_increment) actual_not_nominal = true;
        require(actual_not_nominal,"unconstrained genuine Philox trace exercises non-nominal increments");
    }
    for (const auto [mkn,rr] : {std::pair{14000,6},std::pair{14100,10},std::pair{23000,10},std::pair{23100,30}}) {
        Station station(configuration(),42,{}); station.speed_mkn = mkn; station.power({0},c::OwnPowerKind::On,1);
        station.step(r::from_seconds(60)); require(station.engine.snapshot().reports_per_minute == rr,"bounded own SOG Table1 classification");
    }
    Station low(configuration(),42,fixed_rate(1,3)); low.power({0},c::OwnPowerKind::On,1);
    const auto blocked = low.step(r::from_seconds(60));
    require(blocked.intents.empty() && low.engine.snapshot().blocked_reason == c::BlockedReason::LowRatePolicyAbsent &&
            low.knowledge.ledger_issued() == 0,"F3 low rate never silently becomes Type1/SOTDMA");
    ++negatives["low rate policy"];
    Station anchor(configuration(),42,{}); anchor.status = w::NavigationStatus::AtAnchor; anchor.speed_mkn = 0;
    anchor.power({0},c::OwnPowerKind::On,1); anchor.step(r::from_seconds(60));
    require(anchor.engine.snapshot().blocked_reason == c::BlockedReason::LowRatePolicyAbsent,"own anchored source low-rate obligation explicitly blocked");
    ++negatives["own anchored low rate"];
    Station unsupported(configuration(),42,fixed_rate(7)); unsupported.power({0},c::OwnPowerKind::On,1);
    unsupported.step(r::from_seconds(60)); require(unsupported.engine.snapshot().blocked_reason == c::BlockedReason::UnsupportedRate,"unsupported fixed regime fails closed");
    ++negatives["unsupported rate"];
    Station changed(configuration(),42,{}); changed.power({0},c::OwnPowerKind::On,1); changed.step(r::from_seconds(60));
    changed.speed_mkn = 24000; const auto now = *next_due(changed); changed.step(now);
    require(changed.engine.snapshot().blocked_reason == c::BlockedReason::RateChangeNotImplemented && changed.engine.history().empty(),"dynamic cadence deferred to S4-D");
    ++negatives["rate migration deferred"];
}

void lifecycle_and_failures() {
    Station lifecycle(configuration());
    const c::OwnPowerNotice lifecycle_on{lifecycle.own.identity(),r::from_seconds(7),c::OwnPowerKind::On,1};
    invalid("power dispatch order",[&] { (void)lifecycle.engine.apply_power(lifecycle_on); });
    lifecycle.power(r::from_seconds(7),c::OwnPowerKind::On,1);
    invalid("duplicate engine ON",[&] { (void)lifecycle.engine.apply_power(lifecycle_on); });
    Station phase_three(configuration()); phase_three.power({0},c::OwnPowerKind::On,1);
    phase_three.step(r::from_seconds(60)); const auto decision_time = *next_due(phase_three);
    require(phase_three.step(decision_time).intents.size() == 1,"phase3 inversion fixture has a due intent");
    invalid("phase0 after phase3",[&] { phase_three.power(decision_time,c::OwnPowerKind::Off,1); });
    require(phase_three.engine.history().size() == 1 && phase_three.engine.state() == c::AccessState::FirstFrame,
            "late phase0 is rejected, not a retroactive withdrawal of an already-yielded due decision");
    for (const auto off_seconds : {66,67}) {
        Station station(configuration()); station.power(r::from_seconds(7),c::OwnPowerKind::On,1);
        station.power(r::from_seconds(off_seconds),c::OwnPowerKind::Off,1);
        require(station.engine.state() == c::AccessState::Off && station.engine.history().empty() &&
                station.knowledge.ledger_issued() == 0,"OFF at59/60 beats entry");
        station.power(r::from_seconds(80),c::OwnPowerKind::On,2);
        station.step(r::add_units(r::from_seconds(140),-1));
        require(station.engine.state() == c::AccessState::Observe60Seconds && station.knowledge.ledger_issued() == 0,"reON observes fresh full60 seconds");
    }
    for (const auto mode : {0,1,2}) {
        Station station(configuration()); station.power(r::from_seconds(7),c::OwnPowerKind::On,1);
        nominal_pressure(station,r::from_seconds(67),kDisplayBase + 4162,375,6); station.step(r::from_seconds(67));
        r::SimulationInstant off = r::from_seconds(69);
        if (mode == 1) off = at_index(kDisplayBase + 4162,station.own.clock().anchor);
        if (mode == 2) {
            const auto first = *next_due(station); station.step(first); off = {746979328};
        }
        const auto before = station.engine.history().size(); const auto issued = station.knowledge.ledger_issued();
        const auto cancellation = station.power(off,c::OwnPowerKind::Off,1);
        require(cancellation.intents.empty() && !cancellation.withdrawn.empty() && station.engine.history().size() == before,
                "F4 OFF withdraws proposals, preserves due history");
        for (const auto& proposal : cancellation.withdrawn)
            require(proposal.status == c::IntentStatus::Withdrawn && proposal.reason == "cancelled_by_off","explicit OFF cancellation provenance");
        require(station.knowledge.ledger_issued() == issued && station.knowledge.ledger().size() == issued,"OFF preserves audited S3 draws");
        require(station.knowledge.map(c::Channel::Ais1).own_reservations().empty() &&
                station.knowledge.map(c::Channel::Ais2).own_reservations().empty(),"S3 OFF clears live intentions");
        invalid("same-instant reON",[&] { station.power(off,c::OwnPowerKind::On,2); });
        station.power(r::add_units(off,1),c::OwnPowerKind::On,2);
        const auto old_history = before; station.step(r::add_units(off,r::kUnitsPerFrame));
        require(station.engine.history().size() == old_history && station.engine.state() == c::AccessState::Observe60Seconds,"generation2 cannot reuse generation1 plan");
    }
    Station missed(configuration()); missed.power({0},c::OwnPowerKind::On,1); missed.step(r::from_seconds(60));
    const auto later = r::add_units(*next_due(missed),1); const auto miss = missed.step(later);
    require(miss.intents.empty() && missed.engine.snapshot().blocked_reason == c::BlockedReason::MissedDeadline,"late call never fabricates past due intent");
    ++negatives["missed deadline"];
    Station unavailable(configuration()); unavailable.power({0},c::OwnPowerKind::On,1);
    const auto absent = unavailable.engine.block_inputs(r::from_seconds(60),c::BlockedReason::NoUtc);
    require(absent.intents.empty() && unavailable.knowledge.ledger_issued() == 0 &&
            unavailable.engine.snapshot().blocked_reason == c::BlockedReason::NoUtc,"no UTC does not fabricate own slot/token");
    ++negatives["no UTC"];
    Station absent_fix(configuration()); absent_fix.power({0},c::OwnPowerKind::On,1);
    absent_fix.step(r::from_seconds(60)); const auto absent_time = *next_due(absent_fix);
    const auto no_fix = absent_fix.engine.block_inputs(absent_time,c::BlockedReason::OwnInputsUnavailable);
    require(no_fix.intents.empty() && no_fix.withdrawn.size() == 2 &&
            absent_fix.engine.snapshot().blocked_reason == c::BlockedReason::OwnInputsUnavailable,
            "missing EPFS withdraws future plans without fabricating qualified input or a due payload");
    require(!absent_fix.knowledge.map(c::Channel::Ais1).own_reservations().empty(),
            "missing-input path conservatively retains unclocked own intentions until OFF");
    absent_fix.power(r::add_units(absent_time,1),c::OwnPowerKind::Off,1);
    require(absent_fix.knowledge.map(c::Channel::Ais1).own_reservations().empty(),"OFF clears conservative missing-input reservations");
    ++negatives["missing EPFS"];
    Station epochs(configuration()); epochs.power({0},c::OwnPowerKind::On,1);
    const auto old_epoch = epochs.qualify(r::from_seconds(60));
    epochs.power(r::from_seconds(61),c::OwnPowerKind::Off,1); epochs.power(r::from_seconds(62),c::OwnPowerKind::On,2);
    invalid("historical epoch input",[&] { (void)epochs.engine.step(old_epoch,r::from_seconds(122)); });
    require(epochs.engine.history().empty() && epochs.knowledge.ledger_issued() == 0 &&
            epochs.engine.state() == c::AccessState::Observe60Seconds,"old-epoch token cannot advance or draw for new generation");
    Station stale(configuration()); stale.power({0},c::OwnPowerKind::On,1); const auto token = stale.qualify(r::from_seconds(60));
    invalid("stale own token",[&] { (void)stale.engine.step(token,r::add_units(r::from_seconds(60),1)); });
    require(stale.engine.history().empty() && stale.knowledge.ledger_issued() == 0,"stale inputs fail before draws");
    invalid("foreign power",[&] { stale.engine.apply_power({configuration(1).identity(),r::from_seconds(61),c::OwnPowerKind::Off,1}); });
    invalid("reversed power",[&] { stale.engine.apply_power({stale.own.identity(),r::from_seconds(59),c::OwnPowerKind::Off,1}); });
    Station overflow(configuration());
    invalid("observation overflow",[&] { overflow.power({std::numeric_limits<std::int64_t>::max()},c::OwnPowerKind::On,1); });
    Station negative(configuration(0,{-59,123}),42); negative.power(r::from_seconds(7),c::OwnPowerKind::On,1);
    negative.step(r::from_seconds(67)); (void)complete(negative,6);
}

void bounded_reselection_and_compensation() {
    auto bounded_history = fixed_rate(); bounded_history.history_capacity = 3;
    Station history_limit(configuration(),1062,bounded_history); history_limit.power({0},c::OwnPowerKind::On,1);
    history_limit.step(r::from_seconds(60)); const auto issued_before_capacity = history_limit.knowledge.ledger_issued();
    const auto capacity_time = *next_due(history_limit); const auto capacity = history_limit.step(capacity_time);
    require(capacity.intents.empty() && capacity.withdrawn.size() == 2 &&
            history_limit.engine.snapshot().blocked_reason == c::BlockedReason::HistoryCapacity &&
            history_limit.knowledge.ledger_issued() == issued_before_capacity,
            "bounded combined history fails before successor draws and never silently evicts");
    history_limit.power(r::add_units(capacity_time,1),c::OwnPowerKind::Off,1);
    const auto withdrawn_export = history_limit.engine.drain_proposal_history();
    require(withdrawn_export.size() == 2 && history_limit.engine.proposal_history().empty(),
            "explicit proposal export after OFF retains capacity withdrawals");
    ++negatives["history capacity"];
    Station audit(configuration()); audit.power({0},c::OwnPowerKind::On,1); audit.step(r::from_seconds(60));
    const auto first_due = *next_due(audit); audit.step(first_due);
    const auto issued_before_off = audit.knowledge.ledger_issued();
    audit.power(r::add_units(first_due,1),c::OwnPowerKind::Off,1);
    audit.power(r::add_units(first_due,2),c::OwnPowerKind::On,2);
    const auto due_export = audit.engine.drain_history(); const auto ledger_export = audit.knowledge.drain_ledger();
    require(due_export.size() == 1 && due_export.front().power_generation == 1 && audit.engine.history().empty() &&
            ledger_export.size() == issued_before_off && audit.knowledge.ledger().empty(),
            "explicit old-generation due history and draw export survives OFF and new ON");
    c::KnowledgeLimits limits; limits.map.max_own = 512;
    auto policy = fixed_rate(); policy.entry_retry_limit = 3;
    Station full(configuration(),1062,policy,limits); full.power(r::from_seconds(7),c::OwnPowerKind::On,1);
    const auto time = r::from_seconds(67); const auto inputs = full.qualify(time);
    const auto current = index_at(time,full.own.clock().anchor);
    pressure_range(full,inputs,time,c::Channel::Ais1,current + 1,current + 375 + 37);
    const auto result = full.step(time);
    require(result.intents.empty() && result.proposals.empty() && full.engine.state() == c::AccessState::Blocked &&
            full.engine.snapshot().entry_attempts == 3,"bounded NSS reselection ends with zero intent");
    require(full.engine.snapshot().blocked_reason == c::BlockedReason::NoLegalCandidates && full.knowledge.ledger_issued() == 3,
            "no synthetic fourth candidate or replayed draw on exhaustion");
    ++negatives["candidate exhaustion"];
    c::KnowledgeLimits tiny; tiny.map.max_own = 1;
    Station pair(configuration(),1062,policy,tiny); pair.power(r::from_seconds(7),c::OwnPowerKind::On,1);
    const auto own = pair.qualify(time);
    pair.knowledge.reserve_own({c::Channel::Ais2,kDisplayBase + 6000,1,c::OwnReservationKind::Sotdma,0,false,"UNIT_FIXTURE_CAPACITY"},own,time);
    const auto attempted = pair.step(time);
    require(attempted.intents.empty() && attempted.proposals.empty() && pair.engine.state() == c::AccessState::Blocked,"half-pair reservation cannot publish plan");
    require(pair.knowledge.map(c::Channel::Ais1).own_reservations().empty() &&
            pair.knowledge.map(c::Channel::Ais2).own_reservations().size() == 1,"compensate only attempted A, preserve pre-existing B");
    require(pair.knowledge.ledger_issued() > 0 && pair.knowledge.ledger().size() == pair.knowledge.ledger_issued(),"failed attempts retain Philox audit history");
    ++negatives["dual reservation compensation"];
}

c::OwnedRadioEvidence fixture_evidence(Station& station, std::uint64_t receipt,
                                      r::SimulationInstant disclosed, std::int64_t target) {
    const auto current = index_at(disclosed,station.own.clock().anchor);
    const auto received = current - 1;
    c::OwnRadioObservation observation;
    observation.owner = station.own.identity(); observation.receiver = station.own.identity().radio_id;
    observation.channel = c::Channel::Ais1; observation.power_generation = station.gate.power_generation();
    observation.catalog_revision = kWorldRevision; observation.receipt_id = receipt;
    observation.physical_start = at_index(received,station.own.clock().anchor);
    observation.physical_end = at_index(current,station.own.clock().anchor); observation.disclosed_at = disclosed;
    observation.disposition = c::ObservationDisposition::Decoded;
    c::DecodedReportFacts decoded; decoded.kind = c::DecodedMessageKind::Type3; decoded.source_mmsi = 100000009;
    decoded.raw_communication_state = a::ItdmaState19::from_effective(a::SyncState::UtcDirect,target - received,1,false).raw().value();
    observation.decoded = decoded; observation.note = "UNIT_FIXTURE ONLY; no physical reception";
    return c::UnitFixtureEvidenceAccess::mint(std::move(observation));
}
void evidence_phase_and_isolation() {
    Station phase_two(configuration()); phase_two.power(r::from_seconds(7),c::OwnPowerKind::On,1);
    const auto phase_two_time = r::from_seconds(67); const auto phase_two_own = phase_two.qualify(phase_two_time);
    const auto phase_two_evidence = fixture_evidence(phase_two,1,phase_two_time,kDisplayBase + 4162);
    (void)phase_two.engine.deliver_evidence(phase_two_evidence,phase_two_own,phase_two_time);
    invalid("phase0 after phase2",[&] { phase_two.power(phase_two_time,c::OwnPowerKind::Off,1); });
    require(phase_two.engine.state() == c::AccessState::Observe60Seconds && phase_two.engine.history().empty(),
            "same-time phase0 after phase2 is an explicit caller-order error");
    Station observed(configuration(),1062); Station blind(configuration(1),1062);
    observed.power(r::from_seconds(7),c::OwnPowerKind::On,1); blind.power(r::from_seconds(7),c::OwnPowerKind::On,1);
    const auto now = r::from_seconds(67); const auto own = observed.qualify(now);
    const auto evidence = fixture_evidence(observed,1,now,kDisplayBase + 4162);
    const auto accepted = observed.engine.deliver_evidence(evidence,own,now);
    require(accepted.status == "decoded_peer_evidence_accepted" && observed.knowledge.directory().size() == 1 &&
            blind.knowledge.directory().size() == 0,"phase2 own reception does not leak to blind peer");
    const auto interval = c::selection_interval(kDisplayBase + 4162,375);
    const auto observed_pool = observed.knowledge.candidates(c::Channel::Ais1,interval,1,std::nullopt,own,now);
    const auto own_blind = blind.qualify(now);
    const auto blind_pool = blind.knowledge.candidates(c::Channel::Ais1,interval,1,std::nullopt,own_blind,now);
    const auto is_target = [](const c::Candidate& candidate) { return candidate.first_index == kDisplayBase + 4162; };
    require(std::none_of(observed_pool.candidates.begin(),observed_pool.candidates.end(),is_target) &&
            std::any_of(blind_pool.candidates.begin(),blind_pool.candidates.end(),is_target),
            "same-T decoded reservation excludes the target only for its receiving owner's candidate pool");
    observed.engine.step(own,now);
    const auto revision = observed.knowledge.knowledge_revision(); const auto external = observed.knowledge.map(c::Channel::Ais1).external_count();
    const auto late = fixture_evidence(observed,2,now,kDisplayBase + 4163);
    invalid("phase inversion",[&] { observed.engine.deliver_evidence(late,own,now); });
    require(observed.knowledge.knowledge_revision() == revision && observed.knowledge.map(c::Channel::Ais1).external_count() == external,
            "S3.1 late evidence rejection preserves explicit-synchronize baseline");
    invalid("foreign receipt",[&] { blind.engine.deliver_evidence(evidence,own_blind,now); });
    require(blind.knowledge.directory().size() == 0,"foreign receipt never populates another owner");
}

std::string canonical_history(const Station& station) {
    std::ostringstream out;
    for (const auto& intent : station.engine.history())
        out << static_cast<unsigned>(intent.channel) << ':' << intent.first_slot_index << ':' << intent.communication_state_19 << ':'
            << intent.power_generation << ':' << intent.inputs_fingerprint << ';';
    for (const auto& draw : station.knowledge.ledger())
        out << static_cast<unsigned>(draw.key.purpose) << ':' << draw.key.episode << ':' << draw.key.draw_index << ':' << draw.value << ';';
    return out.str();
}
std::array<std::string,2> replay(bool reverse, bool unrelated) {
    Station first(configuration(0,{0,0}),42); Station second(configuration(1,{0,0}),42);
    std::unique_ptr<Station> third;
    if (unrelated) third = std::make_unique<Station>(configuration(2,{0,0}),42);
    std::vector<Station*> stations = {&first,&second}; if (third) stations.push_back(third.get());
    if (reverse) std::reverse(stations.begin(),stations.end());
    for (auto* station : stations) station->power({0},c::OwnPowerKind::On,1);
    for (auto* station : stations) station->step(r::from_seconds(60));
    for (;;) {
        std::optional<r::SimulationInstant> earliest;
        for (auto* station : stations) { const auto next = next_due(*station); if (next && (!earliest || *next < *earliest)) earliest = next; }
        if (!earliest) break;
        for (auto* station : stations) { const auto next = next_due(*station); if (next && *next == *earliest) station->step(*earliest); }
    }
    return {canonical_history(first),canonical_history(second)};
}
void deterministic_replay() {
    const auto baseline = replay(false,false);
    require(replay(true,false) == baseline && replay(false,true) == baseline && replay(true,true) == baseline,
            "host input-order/unrelated station permutations preserve own semantic traces (not runtime worker integration)");
    bool collision = false;
    for (std::uint64_t seed = 1; seed <= 2048 && !collision; ++seed) {
        Station first(configuration(0,{0,0}),seed); Station second(configuration(1,{0,0}),seed);
        first.power({0},c::OwnPowerKind::On,1); second.power({0},c::OwnPowerKind::On,1);
        first.step(r::from_seconds(60)); second.step(r::from_seconds(60));
        const auto a_due = first.engine.channel_plan(c::Channel::Ais1).next_due_index;
        const auto b_due = second.engine.channel_plan(c::Channel::Ais1).next_due_index;
        if (a_due && b_due && a_due == b_due) {
            const auto time = at_index(*a_due,first.own.clock().anchor);
            require(first.step(time).intents.size() == 1 && second.step(time).intents.size() == 1,
                    "blind colliding local intentions are not centrally repaired"); collision = true;
        }
    }
    require(collision,"bounded synthetic seed search found blind intent collision");
}

// Stable pipe schema CLASSA-ACCESS-TRACE-v1, documented by the independent
// oracle. Units are exact SimulationInstant units and true absolute slots;
// SCENE display_base is only the frozen document's human-label origin.
std::string number(const std::optional<std::int64_t>& value) { return value ? std::to_string(*value) : "-"; }
std::string sequence(const std::vector<std::uint64_t>& values) {
    std::string result; for (auto value : values) { if (!result.empty()) result += ','; result += std::to_string(value); } return result.empty() ? "-" : result;
}
class Trace {
public:
    std::string name; Station station; std::size_t ledger_cursor = 0;
    Trace(std::string scene, c::OwnStationConfig own, std::uint64_t seed, c::AccessPolicy policy,
          r::SimulationInstant on, std::int64_t base, const char* kind)
        : name(std::move(scene)),station(own,seed,policy) {
        const auto& id = station.own.identity();
        std::cout << "SCENE|" << name << '|' << seed << '|' << id.vessel_id.value << '|' << id.equipment_id.value << '|'
            << id.radio_id.value << '|' << on.units << '|' << own.clock().anchor.posix_seconds << '|' << own.clock().anchor.subsecond_units << '|'
            << policy.fixed_rate_numerator << '|' << policy.fixed_rate_denominator << '|' << base << '|' << kind << "|16200000|25800000\n";
        power(on,c::OwnPowerKind::On,1);
    }
    void power(r::SimulationInstant time,c::OwnPowerKind kind,std::uint64_t generation) {
        std::cout << "POWER|" << name << '|' << time.units << '|' << generation << '|'
            << (kind == c::OwnPowerKind::On ? "ON" : kind == c::OwnPowerKind::Off ? "OFF" : "INITIAL_OFF") << '\n';
        emit(station.power(time,kind,generation),time);
    }
    void emit(const c::EngineStep& step,r::SimulationInstant now) {
        const auto ledger = station.knowledge.ledger();
        while (ledger_cursor < ledger.size()) {
            const auto& draw = ledger[ledger_cursor]; ++ledger_cursor;
            std::cout << "DRAW|" << name << '|' << now.units << '|' << ledger_cursor << '|' << draw.key.power_generation << '|'
                << static_cast<unsigned>(draw.key.purpose) << '|' << draw.key.episode << '|' << draw.key.draw_index << '|' << draw.minimum << '|'
                << draw.maximum << '|' << draw.value << '|' << draw.accepted_word << '|' << draw.rejections << '|' << draw.input_digest << '|' << draw.outcome << '\n';
        }
        const auto snapshot = station.engine.snapshot();
        for (const auto& proposal : step.proposals)
            std::cout << "PROPOSAL|" << name << '|' << now.units << '|' << proposal.power_generation << '|' << proposal.proposal_id << '|'
                << c::channel_name(proposal.channel) << '|' << proposal.first_slot_index << '|' << proposal.slot_start.units << '|' << proposal.ordinal << '|'
                << proposal.nss_index << '|' << c::intent_status_name(proposal.status) << '|' << proposal.knowledge_revision << '|' << proposal.inputs_fingerprint << '|'
                << sequence(proposal.ledger_sequences) << '|' << proposal.reason << '\n';
        for (const auto& intent : step.intents) {
            const auto& id = intent.owner;
            const auto first = station.engine.channel_plan(intent.channel).first_nts_index;
            std::cout << "INTENT|" << name << '|' << now.units << '|' << id.vessel_id.value << '|' << id.equipment_id.value << '|' << id.radio_id.value << '|'
                << intent.power_generation << '|' << intent.decision_id << '|' << intent.proposal_id << '|' << c::channel_name(intent.channel) << '|'
                << intent.first_slot_index << '|' << intent.slot_start.units << '|' << intent.nss_index << '|' << number(first) << '|' << intent.ordinal << '|'
                << number(intent.next_slot_index) << '|' << intent.communication_state_19 << '|' << (intent.communication_state_19 & 1) << '|'
                << unsigned(intent.block_length) << '|' << c::intent_status_name(intent.status) << '|' << c::intent_message_name(intent.message) << '|'
                << intent.knowledge_revision << '|' << intent.inputs_fingerprint << '|' << sequence(intent.ledger_sequences) << '|' << intent.reason << '\n';
        }
        for (const auto& withdrawn : step.withdrawn)
            std::cout << "WITHDRAW|" << name << '|' << now.units << '|' << withdrawn.power_generation << '|' << c::channel_name(withdrawn.channel)
                << '|' << withdrawn.first_slot_index << '|' << withdrawn.reason << '\n';
        std::cout << "STEP|" << name << '|' << now.units << '|' << snapshot.power_generation << '|' << c::access_state_name(step.state) << '|'
            << c::blocked_reason_name(snapshot.blocked_reason) << '|' << step.intents.size() << '|' << step.proposals.size() << '\n';
        for (const auto channel : {c::Channel::Ais1,c::Channel::Ais2}) {
            const auto& plan = station.engine.channel_plan(channel);
            std::cout << "PLAN|" << name << '|' << now.units << '|' << snapshot.power_generation << '|' << c::channel_name(channel) << '|'
                << number(plan.nss_index) << '|' << number(plan.first_nts_index) << '|' << number(plan.next_due_index) << '|'
                << plan.transmissions_done << '|' << c::channel_phase_name(plan.phase) << '\n';
        }
    }
    void step(r::SimulationInstant now) { emit(station.step(now),now); }
    void pressure(r::SimulationInstant now,std::int64_t nss) {
        for (const auto& block : nominal_pressure(station,now,nss,375,6))
            std::cout << "PRESSURE|" << name << '|' << station.gate.power_generation() << '|' << c::channel_name(block.channel) << '|'
                << block.first << '|' << unsigned(block.length) << "|0\n";
    }
    void finish() {
        for (unsigned guard = 0; guard < 40; ++guard) { const auto next = next_due(station); if (!next) break; step(*next); }
        unsigned zeros[2] = {}; for (const auto& intent : station.engine.history()) if (intent.communication_state_19 == 1) ++zeros[static_cast<unsigned>(intent.channel)];
        std::cout << "END|" << name << '|' << c::access_state_name(station.engine.state()) << '|' << station.engine.history().size() << '|'
            << zeros[0] << '|' << zeros[1] << '|' << station.knowledge.ledger_issued() << '\n';
    }
};
void vectors() {
    std::cout << "ACCESS|CLASSA-ACCESS-TRACE-v1\n";
    { Trace trace("F1",configuration(),1062,fixed_rate(),r::from_seconds(7),kDisplayBase,"UNIT_FIXTURE_OWN_PRESSURE");
      trace.step(r::add_units(r::from_seconds(67),-1));trace.pressure(r::from_seconds(67),kDisplayBase+4162);trace.step(r::from_seconds(67));trace.finish(); }
    { Trace trace("F2",configuration(),939,fixed_rate(),r::from_seconds(10),kDisplayBase,"UNIT_FIXTURE_OWN_PRESSURE");
      trace.pressure(r::from_seconds(70),kDisplayBase+4499);trace.step(r::from_seconds(70));trace.finish(); }
    { Trace trace("ZERO_ANCHOR",configuration(0,{0,0}),1062,fixed_rate(),r::from_seconds(7),0,"UNIT_FIXTURE_OWN_PRESSURE");
      trace.pressure(r::from_seconds(67),2662);trace.step(r::from_seconds(67));trace.finish(); }
    for (const auto rate : {6,10,18,30}) {
        Trace trace(std::string("EMPTY_R")+std::to_string(rate),configuration(0,{0,0}),42,fixed_rate(rate),{0},0,"EMPTY_LOCAL_MAPS");
        trace.step(r::from_seconds(60));trace.finish();
    }
    { Trace trace("NEGATIVE_ANCHOR",configuration(0,{-59,123}),42,fixed_rate(),r::from_seconds(7),0,"EMPTY_LOCAL_MAPS");
      trace.step(r::from_seconds(67));trace.finish(); }
    { Trace trace("F3",configuration(),42,fixed_rate(1,3),r::from_seconds(7),kDisplayBase,"BLOCKED_LOW_RATE");
      trace.step(r::from_seconds(67));trace.finish(); }
    for (unsigned mode=0;mode<5;++mode) {
        Trace trace(std::string("F4_")+std::to_string(mode),configuration(),1062,fixed_rate(),r::from_seconds(7),kDisplayBase,"UNIT_FIXTURE_LIFECYCLE");
        r::SimulationInstant off = r::from_seconds(mode==0?66:67);
        if(mode>=2) {trace.pressure(r::from_seconds(67),kDisplayBase+4162);trace.step(r::from_seconds(67));off=r::from_seconds(69);}
        if(mode==3)off=at_index(kDisplayBase+4162,trace.station.own.clock().anchor);
        if(mode==4){trace.step(at_index(kDisplayBase+4162,trace.station.own.clock().anchor));off={746979328};}
        trace.power(off,c::OwnPowerKind::Off,1);trace.finish();
    }
}
}  // namespace

int main(int argc,char** argv) {
    try {
        if(argc==2 && std::string_view(argv[1])=="--vectors") { vectors();return 0; }
        if(argc!=1)throw std::invalid_argument("usage: classa_access_tests [--vectors]");
        section("public_contracts",public_contracts);section("frozen_fixtures",frozen_fixtures);
        section("rates_and_non_nominal",rates_and_non_nominal);section("lifecycle_and_failures",lifecycle_and_failures);
        section("bounded_reselection_and_compensation",bounded_reselection_and_compensation);
        section("evidence_phase_and_isolation",evidence_phase_and_isolation);section("deterministic_replay",deterministic_replay);
        unsigned total=0;for(const auto& [category,count]:negatives){std::cout<<"NEGATIVE|"<<category<<'|'<<count<<'\n';total+=count;}
        std::cout<<"A1-S4-A synthetic access tests PASS: "<<assertions<<" assertions, "<<total
                 <<" categorized negative fixtures; intents only, no physical TX/RX/FCS/NMEA/UDP\n";return 0;
    } catch(const std::exception& error){std::cerr<<"A1-S4-A test failure: "<<error.what()<<'\n';return 1;}
}
