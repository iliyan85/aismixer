#include "station_assembly.h"
#include "station_knowledge.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <map>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

// Explicit SYNTHETIC A1-S3 unit fixtures. Every observation here is a
// UNIT_FIXTURE token: not a physical reception, not an FCS-validated frame,
// not a successful transmission and not RF evidence of any kind.
namespace aismixer::lab::classa {
// Unit-fixture minter: declared by radio_evidence.h under the fixture macro, defined only here.
OwnedRadioEvidence UnitFixtureEvidenceAccess::mint(OwnRadioObservation observation) {
    observation.provenance = EvidenceProvenance::UnitFixture;
    return OwnedRadioEvidence(std::move(observation));
}
}  // namespace aismixer::lab::classa

namespace {
namespace c = aismixer::lab::classa;
namespace a = aismixer::lab::ais;
namespace r = aismixer::lab::runtime;
namespace w = aismixer::lab::world;
std::map<std::string, unsigned> negatives;
unsigned assertions = 0;
constexpr std::int64_t kSlotUnits = r::kUnitsPerSlot;
constexpr std::int64_t kFrame = c::kSlotsPerFrame;
constexpr std::uint64_t kWorldRevision = 17;

void require(bool value, std::string_view message) {
    if (!value) throw std::runtime_error(std::string(message));
    ++assertions;
}
template <class F> void invalid(const char* category, F action) {
    try { action(); }
    catch (const std::invalid_argument& error) {
        require(std::string_view(error.what()).size() > 5, "failure lacks an actionable diagnostic");
        ++negatives[category]; return;
    }
    throw std::runtime_error(std::string("accepted invalid ") + category);
}
template <class F> void overflow(const char* category, F action) {
    try { action(); }
    catch (const std::overflow_error& error) {
        require(std::string_view(error.what()).size() > 5, "overflow lacks context");
        ++negatives[category]; return;
    }
    throw std::runtime_error(std::string("accepted overflowing ") + category);
}
template <class F> void section(const char* name, F function) {
    try { function(); }
    catch (const std::exception& error) { throw std::runtime_error(std::string(name) + ": " + error.what()); }
}
template <class F> void exhausted(const char* category, F action) {
    try { action(); }
    catch (const std::runtime_error& error) {
        require(std::string_view(error.what()).size() > 5, "exhaustion lacks context");
        ++negatives[category]; return;
    }
    throw std::runtime_error(std::string("accepted exhausted ") + category);
}

r::SimulationInstant at_slot(std::int64_t index) { return {index * kSlotUnits}; }  // Anchor {0,0}.

w::Config synthetic_world() {
    w::Config result;
    result.profile_id = "SYNTHETIC-A1-S3-WORLD-v1";
    result.provenance = "Explicit synthetic local-knowledge unit fixture; no RF evidence";
    result.duration = r::from_seconds(7200);
    result.waypoints = {{{1}, "SYNTHETIC POINT A", {27000000000LL, 43000000000LL}},
                        {{2}, "SYNTHETIC POINT B", {27100000000LL, 43100000000LL}}};
    for (unsigned index = 0; index < 3; ++index) {
        w::Vessel vessel;
        vessel.id = {11 + index}; vessel.equipment_id = {21 + index}; vessel.radio_id = {31 + index};
        vessel.name = index == 0 ? "SYNTHETIC W1 A" : index == 1 ? "SYNTHETIC W1 B" : "SYNTHETIC W1 C";
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
    const char* mmsis[] = {"000000001", "000000002", "000000003"};
    a::Mmsi mmsi(mmsis[index]);
    a::Type5 voyage(mmsi, 1234567 + static_cast<std::int64_t>(index));
    voyage.call_sign = index == 0 ? "LAB0001" : index == 1 ? "LAB0002" : "LAB0003";
    voyage.name = index == 0 ? "SYNTHETIC CLASS A 1" : index == 1 ? "SYNTHETIC CLASS A 2" : "SYNTHETIC CLASS A 3";
    voyage.ship_type = 70;
    voyage.position_fixing_device = 15;
    voyage.destination = "SYNTHETIC PORT";
    voyage.dte_not_ready = false;
    c::StationDefinition result({{11 + index}, {21 + index}, {31 + index}}, mmsi, voyage,
                                a::ManufacturerIdentification(index == 0 ? "LAB" : index == 1 ? "TST" : "THR", 1 + index, 100 + index));
    result.clock.source_id = std::string("SYNTHETIC-UTC-") + static_cast<char>('A' + index);
    result.clock.provenance = "Explicit independent mathematical ideal direct UTC test source";
    result.clock.anchor = {0, 0}; result.clock.anchor_revision = 7;
    result.input_policy.epfs_source_id = std::string("SYNTHETIC-EPFS-") + static_cast<char>('A' + index);
    result.input_policy.provenance = "Own W1 truth as idealized EPFS; no GPS accuracy or integrity claim";
    result.input_policy.max_navigation_age_units = 60 * r::kUnitsPerSecond;
    result.input_policy.max_clock_age_units = 60 * r::kUnitsPerSecond;
    result.origin.scenario_id = "SYNTHETIC-A1-S3-WORLD-v1";
    result.origin.provenance = "SYNTHETIC metadata; syntax-only identities with no allocation assertion";
    return result;
}

std::vector<c::OwnStationConfig> configurations(unsigned count = 2) {
    std::vector<c::StationDefinition> definitions;
    for (unsigned index = 0; index < count; ++index) definitions.push_back(definition(index));
    return c::assemble_stations(synthetic_world(), kWorldRevision, std::move(definitions));
}
c::ScenarioSeedHandoff handoff() { return c::project_scenario_seed(synthetic_world(), kWorldRevision); }

c::QualifiedOwnInputs qualify_with(c::StationInputGate& gate, const c::OwnStationConfig& own, const w::Point& location,
                                   r::SimulationInstant now, bool valid_fix = true,
                                   std::optional<r::SimulationInstant> fix_generated_at = std::nullopt) {
    const auto generated_at = fix_generated_at.value_or(now);
    c::DirectUtcObservation clock;
    clock.own_identity = own.identity(); clock.power_generation = gate.power_generation();
    clock.source_id = own.clock().source_id; clock.anchor = own.clock().anchor;
    clock.anchor_revision = own.clock().anchor_revision; clock.sampled_at = now; clock.valid = true;
    const auto token = gate.accept_clock(clock, now);
    w::VesselSnapshot snapshot;
    snapshot.state.id = own.identity().vessel_id; snapshot.state.equipment_id = own.identity().equipment_id;
    snapshot.state.radio_id = own.identity().radio_id; snapshot.state.equipment_on = true;
    snapshot.state.power_generation = gate.power_generation(); snapshot.state.speed_mkn = 8150;
    snapshot.state.nominal_speed_mkn = 8150; snapshot.state.navigation_status = w::NavigationStatus::UnderWayUsingEngine;
    snapshot.state.course_valid = true; snapshot.location = location; snapshot.course_ndeg = 90000000000LL; snapshot.time = generated_at;
    const c::EpfsFix fix{own.identity(), gate.power_generation(), own.input_policy().epfs_source_id,
                         own.clock().source_id, kWorldRevision, generated_at, valid_fix};
    const auto navigation = gate.adapt_navigation(snapshot, fix, now);
    return gate.qualify_inputs(navigation, *token, now);
}

struct Station {
    c::OwnStationConfig own;
    c::StationInputGate gate;
    c::StationKnowledge knowledge;
    w::Point location{27000000000LL, 43000000000LL};
    Station(const c::OwnStationConfig& config, const c::ScenarioSeedHandoff& seed, c::KnowledgeLimits limits = {})
        : own(config), gate(own), knowledge(own, gate, seed, limits) {}
    void on(r::SimulationInstant time, std::uint64_t generation) {
        const c::OwnPowerNotice notice{own.identity(), time, c::OwnPowerKind::On, generation};
        gate.apply_power(notice); knowledge.apply_power(notice);
    }
    void off(r::SimulationInstant time, std::uint64_t generation) {
        const c::OwnPowerNotice notice{own.identity(), time, c::OwnPowerKind::Off, generation};
        gate.apply_power(notice); knowledge.apply_power(notice);
    }
    c::QualifiedOwnInputs qualify(r::SimulationInstant now, bool valid_fix = true) {
        return qualify_with(gate, own, location, now, valid_fix);
    }
};

std::int64_t to_ais(std::int64_t nanodegrees) {
    const auto numerator = nanodegrees * 3;
    const auto magnitude = numerator < 0 ? -numerator : numerator;
    const auto rounded = magnitude / 5000 + (magnitude % 5000 >= 2500 ? 1 : 0);
    return numerator < 0 ? -rounded : rounded;
}
w::Point peer_at(const w::Point& origin, std::int64_t bearing_ndeg, double distance_m) {
    return w::geodesic_direct(origin, bearing_ndeg, distance_m).location;
}

c::DecodedReportFacts sotdma_facts(std::uint32_t mmsi, const a::SotdmaState19& state,
                                   std::optional<w::Point> position = std::nullopt,
                                   c::DecodedMessageKind kind = c::DecodedMessageKind::Type1) {
    c::DecodedReportFacts facts;
    facts.kind = kind; facts.source_mmsi = mmsi; facts.raw_communication_state = state.raw().value();
    if (position) { facts.longitude = to_ais(position->longitude_ndeg); facts.latitude = to_ais(position->latitude_ndeg); }
    return facts;
}
c::DecodedReportFacts itdma_facts(std::uint32_t mmsi, const a::ItdmaState19& state, std::optional<w::Point> position = std::nullopt) {
    c::DecodedReportFacts facts;
    facts.kind = c::DecodedMessageKind::Type3; facts.source_mmsi = mmsi; facts.raw_communication_state = state.raw().value();
    if (position) { facts.longitude = to_ais(position->longitude_ndeg); facts.latitude = to_ais(position->latitude_ndeg); }
    return facts;
}
c::OwnRadioObservation observation(const Station& station, c::Channel channel, std::uint64_t receipt,
                                   std::int64_t index, std::optional<c::DecodedReportFacts> facts,
                                   c::ObservationDisposition disposition = c::ObservationDisposition::Decoded) {
    c::OwnRadioObservation result;
    result.owner = station.own.identity(); result.receiver = station.own.identity().radio_id;
    result.channel = channel; result.power_generation = station.knowledge.power_generation();
    result.catalog_revision = kWorldRevision; result.receipt_id = receipt;
    result.physical_start = at_slot(index); result.physical_end = {index * kSlotUnits + 232 * 1024};
    result.disclosed_at = result.physical_end; result.disposition = disposition; result.decoded = facts;
    result.note = "UNIT_FIXTURE synthetic observation";
    return result;
}
c::OwnedRadioEvidence mint(const c::OwnRadioObservation& value) { return c::UnitFixtureEvidenceAccess::mint(value); }
const c::OwnIdentity kOwnerA{{11}, {21}, {31}};

std::string hex_word(std::uint32_t word) {
    std::ostringstream stream; stream << std::hex << std::setw(8) << std::setfill('0') << word; return stream.str();
}

// ---------------------------------------------------------------------------
void public_boundary() {
    static_assert(!std::is_default_constructible_v<c::OwnedRadioEvidence>);
    static_assert(!std::is_constructible_v<c::OwnedRadioEvidence, c::OwnRadioObservation>);
    static_assert(!std::is_default_constructible_v<c::ScenarioSeedHandoff>);
    static_assert(!std::is_constructible_v<c::ScenarioSeedHandoff, std::uint64_t, std::uint64_t, std::string>);
    static_assert(!std::is_constructible_v<c::StationKnowledge, w::Config>);
    static_assert(!std::is_constructible_v<c::StationKnowledge, c::OwnStationConfig, c::ScenarioSeedHandoff>);  // The owning gate is mandatory.
    static_assert(!std::is_constructible_v<c::StationKnowledge, w::Snapshot>);
    static_assert(!std::is_copy_constructible_v<c::StationKnowledge>);
    using Accept = decltype(&c::StationKnowledge::accept_evidence);
    static_assert(!std::is_invocable_v<Accept, c::StationKnowledge&, const c::OwnRadioObservation&, const c::QualifiedOwnInputs&, r::SimulationInstant>);
    static_assert(!std::is_invocable_v<Accept, c::StationKnowledge&, bool, const c::QualifiedOwnInputs&, r::SimulationInstant>);
    using Candidates = decltype(&c::StationKnowledge::candidates);
    static_assert(!std::is_invocable_v<Candidates, c::StationKnowledge&, c::Channel, const c::SelectionInterval&, std::uint8_t, std::optional<std::int64_t>, const w::Snapshot&, r::SimulationInstant>);
    static_assert(static_cast<unsigned>(c::ProtocolPurpose::EntryNominalStartSlot) > 16 &&
                  static_cast<unsigned>(c::ProtocolPurpose::IntentionalReusePeer) > 16);
    require(std::string(c::kProtocolRngId) == "CLASSA-PHILOX-v1" && c::kProtocolRngVersion == 1, "protocol RNG identity");
    require(std::string(c::kGenericRatdmaArithmeticId) == "RATDMA-EXACT-GAP-v1" && c::kStationKnowledgeVersion == 1 &&
            c::kLocalSlotMapVersion == 1 && c::kPeerDirectoryVersion == 1 && c::kSlotIdentityVersion == 1, "versioned contract identities");
    require(std::string(c::kUnitFixtureProvenance) == "UNIT_FIXTURE", "fixture provenance label");
}

void slot_identity() {
    require(c::absolute_index({0, 0}) == 0 && c::absolute_index({0, 2249}) == 2249 && c::absolute_index({1, 0}) == 2250,
            "absolute slot index");
    require(c::absolute_index({-1, 2249}) == -1 && c::absolute_index({-1, 0}) == -2250, "negative minute index");
    for (const auto index : {-4500LL, -2251LL, -2250LL, -2249LL, -1LL, 0LL, 1LL, 2249LL, 2250LL, 4499LL, 4500LL}) {
        const auto slot = c::slot_from_index(index);
        require(c::absolute_index(slot) == index && slot.slot < 2250, "index/slot roundtrip");
    }
    require(c::frame_slot(-1) == 2249 && c::frame_of_index(-1) == -1 && c::frame_slot(2250) == 0 && c::frame_of_index(2250) == 1,
            "frame wrap arithmetic");
    require(c::slot_from_utc({5, 2249, 0}) == c::AbsoluteSlot{5, 2249}, "UTC slot adoption");
    require(c::other_channel(c::Channel::Ais1) == c::Channel::Ais2 && c::other_channel(c::Channel::Ais2) == c::Channel::Ais1, "channel pairing");
    require(c::kReuseRangeMicrometres == 222240000000LL, "120 NM in micrometres");
    invalid("slot identity", [] { (void)c::absolute_index({0, 2250}); });
    invalid("slot identity", [] { (void)c::slot_from_utc({0, 2250, 0}); });
    invalid("slot identity", [] { (void)c::slot_from_utc({0, 0, r::kUnitsPerSlot}); });
    invalid("slot identity", [] { (void)c::channel_name(static_cast<c::Channel>(2)); });
    overflow("slot identity overflow", [] { (void)c::absolute_index({INT64_MAX, 0}); });
    overflow("slot identity overflow", [] { (void)c::offset_index(INT64_MAX, 1); });
    overflow("slot identity overflow", [] { (void)c::offset_index(INT64_MIN, -1); });
}

void philox_contract() {
    using Block = c::PhiloxBlock;
    require(c::philox4x32_10({{0, 0, 0, 0}}, {{0, 0}}) == Block{{0x6627E8D5, 0xE169C58D, 0xBC57AC4C, 0x9B00DBD8}}, "Philox KAT zero");
    require(c::philox4x32_10({{UINT32_MAX, UINT32_MAX, UINT32_MAX, UINT32_MAX}}, {{UINT32_MAX, UINT32_MAX}}) ==
            Block{{0x408F276D, 0x41C83B0E, 0xA20BC7C6, 0x6D5451FD}}, "Philox KAT ones");
    require(c::philox4x32_10({{0x243F6A88, 0x85A308D3, 0x13198A2E, 0x03707344}}, {{0xA4093822, 0x299F31D0}}) ==
            Block{{0xD16CFE09, 0x94FDCCEB, 0x5001E420, 0x24126EA1}}, "Philox KAT pi");
    c::DrawKey key;
    key.scenario_seed = 0x0123456789ABCDEFULL; key.equipment_id = 21; key.power_generation = 1;
    key.purpose = c::ProtocolPurpose::CandidateChoice; key.episode = 5; key.draw_index = 7;
    const auto position = c::pack_draw(key, 0);
    require(position.key == c::PhiloxKey{{0x89ABCDEF, 0x01234567}} && position.counter == Block{{21, 5, 0x66000001, 0x01000007}},
            "literal fixed-width packing");
    const auto bytes = c::serialize_draw_position(position);
    const std::array<std::uint8_t, 24> expected{{0xef, 0xcd, 0xab, 0x89, 0x67, 0x45, 0x23, 0x01, 0x15, 0, 0, 0, 0x05, 0, 0, 0,
                                                 0x01, 0, 0, 0x66, 0x07, 0, 0, 0x01}};
    require(bytes == expected, "literal little-endian serialization");
    require(c::pack_draw(key, 3).counter[3] == 0x01030007u, "rejection index field");
    for (const auto generation : {0ULL, 0x1000000ULL, UINT64_MAX}) {
        auto bad = key; bad.power_generation = generation;
        invalid("RNG key domain", [&] { (void)c::pack_draw(bad, 0); });
    }
    { auto bad = key; bad.equipment_id = 0; invalid("RNG key domain", [&] { (void)c::pack_draw(bad, 0); }); }
    { auto bad = key; bad.episode = 0x100000000ULL; invalid("RNG key domain", [&] { (void)c::pack_draw(bad, 0); }); }
    { auto bad = key; bad.draw_index = 0x10000; invalid("RNG key domain", [&] { (void)c::pack_draw(bad, 0); }); }
    { auto bad = key; bad.purpose = static_cast<c::ProtocolPurpose>(7); invalid("RNG key domain", [&] { (void)c::pack_draw(bad, 0); }); }
    invalid("RNG key domain", [&] { (void)c::pack_draw(key, 256); });
    invalid("RNG range", [&] { (void)c::bounded_draw(key, 5, 4); });
    invalid("RNG range", [&] { (void)c::bounded_draw(key, 0, 4294967296LL); });
    invalid("RNG range", [&] { (void)c::bounded_draw(key, INT64_MIN, INT64_MAX); });
    std::uint64_t index = 0;
    const std::uint64_t span = 3221225472ULL;  // 3 * 2^30: limit equals span, one quarter rejected.
    require(c::acceptance_limit(span) == span && !c::accept_word(0xFFFFFFFFu, span, index) &&
            !c::accept_word(static_cast<std::uint32_t>(span), span, index) &&
            c::accept_word(static_cast<std::uint32_t>(span - 1), span, index) && index == span - 1, "injected acceptance words");
    require(c::acceptance_limit(1) == (UINT64_C(1) << 32) && c::acceptance_limit(UINT64_C(1) << 32) == (UINT64_C(1) << 32) &&
            c::acceptance_limit(3) == 4294967295ULL && c::acceptance_limit(0) == 0, "acceptance limits");
    const auto single = c::bounded_draw(key, 42, 42);
    require(single.value == 42 && single.rejections == 0 && single.proposals.size() == 1 && single.span == 1, "single-value draw");
    const auto full = c::bounded_draw(key, 0, 4294967295LL);
    require(full.span == (UINT64_C(1) << 32) && full.value == full.accepted_word && full.rejections == 0, "full 32-bit span");
    bool forced = false;
    for (std::uint64_t draw = 0; draw < 64 && !forced; ++draw) {
        auto probe = key; probe.draw_index = draw;
        const auto entry = c::bounded_draw(probe, 0, static_cast<std::int64_t>(span) - 1);
        if (entry.rejections > 0) {
            forced = true;
            require(entry.proposals.size() == entry.rejections + 1 && entry.proposals.front().words[0] >= span &&
                    !entry.proposals.front().accepted && entry.proposals.back().accepted && entry.accepted_word < span &&
                    entry.value == static_cast<std::int64_t>(entry.accepted_word % span), "forced rejected word uses the next rejection index");
            require(c::pack_draw(probe, entry.rejections).counter[3] == ((entry.rejections << 16) | (1u << 24) | static_cast<std::uint32_t>(draw)),
                    "accepted proposal position");
        }
    }
    require(forced, "a natural rejection must occur within 64 draws at 25 percent rejection");
    // Purpose/episode/draw separation and determinism of the pure function.
    const auto base = c::bounded_draw(key, 0, 1000000);
    require(c::bounded_draw(key, 0, 1000000).accepted_word == base.accepted_word, "pure draw determinism");
    auto other = key; other.purpose = c::ProtocolPurpose::ContinuousTimeout;
    auto next = key; next.draw_index = 8;
    auto epoch = key; epoch.power_generation = 2;
    auto episode = key; episode.episode = 6;
    require(c::bounded_draw(other, 0, 1000000).accepted_word != base.accepted_word &&
            c::bounded_draw(next, 0, 1000000).accepted_word != base.accepted_word &&
            c::bounded_draw(epoch, 0, 1000000).accepted_word != base.accepted_word &&
            c::bounded_draw(episode, 0, 1000000).accepted_word != base.accepted_word, "semantic coordinates separate streams");
    // W1 packs draw/4 < 2^30 into counter[2]; every protocol counter[2] carries purpose << 24 >= 0x65000000.
    const std::uint32_t w1_counter2_max = UINT32_MAX / 4;
    require(w1_counter2_max < (101u << 24) && c::pack_draw(key, 0).counter[2] >= (101u << 24) &&
            c::pack_draw(key, 0).counter[2] <= ((105u << 24) | 0xFFFFFFu), "W1 and protocol counter tuples are disjoint by counter[2]");
    require(c::fnv1a64(std::string("")) == UINT64_C(14695981039346656037) && c::fnv1a64(std::string("a")) == UINT64_C(0xaf63dc4c8601ec8c),
            "FNV-1a64 reference values");
}

void ratdma_contract() {
    const auto quarter = c::make_rational(25, 1);
    require(c::make_rational(50, 200) == c::make_rational(1, 4) && c::make_rational(0, 7) == c::ExactRational{} &&
            c::make_rational(0, 7).denominator == "1" && quarter.numerator == "25" && quarter.denominator == "1", "rational normalization");
    invalid("rational", [] { (void)c::make_rational(1, 0); });
    auto state = c::start_generic_ratdma(0, 150, {10, 40, 80, 120});
    require(state.probability == quarter && c::candidate_count(state) == 4 && state.attempts == 0 && state.original_rtes_index == 150 &&
            c::render_gap(state.gap) == c::make_rational(75, 1), "initial generic RATDMA bookkeeping");
    auto pass = state;
    const auto permitted = c::ratdma_attempt(pass, 25);
    require(permitted.outcome == c::RatdmaAttemptOutcome::WouldRequest && permitted.candidate_index == 10 && permitted.delta == c::ExactRational{} &&
            pass.probability == quarter && c::candidate_count(pass) == 3 && pass.attempts == 0, "u25 equals p25: would request, no attempt consumed");
    auto inhibit = state;
    const auto first = c::ratdma_attempt(inhibit, 26);
    require(first.outcome == c::RatdmaAttemptOutcome::Inhibited && inhibit.probability == c::make_rational(175, 4) && first.delta == c::make_rational(75, 4) &&
            c::candidate_count(inhibit) == 3 && inhibit.attempts == 1 && first.threshold_before == quarter, "u26 inhibits: p 175/4, C3, A1");
    auto pass43 = inhibit;
    require(c::ratdma_attempt(pass43, 43).outcome == c::RatdmaAttemptOutcome::WouldRequest && pass43.probability == c::make_rational(175, 4),
            "u43 below 175/4 passes");
    const auto second = c::ratdma_attempt(inhibit, 44);
    require(second.outcome == c::RatdmaAttemptOutcome::Inhibited && inhibit.probability == c::make_rational(125, 2) &&
            c::candidate_count(inhibit) == 2 && inhibit.attempts == 2, "u44 inhibits: p 125/2, C2, A2");
    (void)c::ratdma_attempt(inhibit, 100);
    (void)c::ratdma_attempt(inhibit, 100);
    require(inhibit.probability == c::make_rational(100, 1) && inhibit.gap.zero && c::candidate_count(inhibit) == 0 && inhibit.attempts == 4,
            "last candidate reaches exactly 100");
    require(c::ratdma_status(inhibit, 121) == c::RatdmaStatus::Exhausted && c::ratdma_status(inhibit, 151) == c::RatdmaStatus::DeadlinePassed &&
            c::ratdma_status(state, 5) == c::RatdmaStatus::Pending, "explicit exhausted/deadline statuses");
    exhausted("RATDMA exhausted", [&] { (void)c::ratdma_attempt(inhibit, 0); });
    unsigned passes = 0;
    for (std::uint32_t u = 0; u <= 100; ++u) {
        auto probe = state;
        if (c::ratdma_attempt(probe, u).outcome == c::RatdmaAttemptOutcome::WouldRequest) ++passes;
    }
    require(passes == 26, "26 of 101 inclusive draws pass at p25");
    auto five = c::start_generic_ratdma(0, 150, {10, 20, 30, 40, 50});
    require(c::ratdma_attempt(five, 21).outcome == c::RatdmaAttemptOutcome::Inhibited && five.probability == c::make_rational(36, 1), "C5 p20 u21 -> 36");
    require(c::ratdma_attempt(five, 37).outcome == c::RatdmaAttemptOutcome::Inhibited && five.probability == c::make_rational(52, 1), "then -> 52");
    std::vector<std::int64_t> wide;
    for (std::int64_t slot = 1; slot <= 150; ++slot) wide.push_back(slot);
    auto large = c::start_generic_ratdma(0, 150, wide);
    require(large.probability == c::make_rational(2, 3), "C150 start probability 2/3");
    const auto large_inhibit = c::ratdma_attempt(large, 1);
    require(large_inhibit.outcome == c::RatdmaAttemptOutcome::Inhibited && large.probability == c::make_rational(299, 225) &&
            large_inhibit.delta == c::make_rational(149, 225) && c::candidate_count(large) == 149 && large.attempts == 1,
            "C150 delta 149/225 without clamping");
    // Interleaved peer removals and inhibitions from C150 break the telescoping denominators: the exact
    // value after 40 such steps (independently computed with Python fractions) needs 34-digit terms.
    auto interleaved = c::start_generic_ratdma(0, 150, wide);
    for (unsigned step = 0; step < 40; ++step) {
        require(c::remove_peer_candidate(interleaved, interleaved.candidates.back()), "peer removal of the last candidate");
        require(c::ratdma_attempt(interleaved, 100).outcome == c::RatdmaAttemptOutcome::Inhibited, "u100 always inhibits below p100");
    }
    require(interleaved.probability == c::ExactRational{"6264842590398423236034723643154476", "194129042518445670321426145332675"} &&
            c::candidate_count(interleaved) == 70 && interleaved.attempts == 40, "exact 40-step interleaved probability (no overflow, no clamping)");
    require(c::ratdma_permits(interleaved.gap, 32) && !c::ratdma_permits(interleaved.gap, 33), "exact comparison around p = 32.27...");
    auto exhaustive = c::start_generic_ratdma(0, 150, wide);
    for (unsigned step = 0; step < 150; ++step) (void)c::ratdma_attempt(exhaustive, 100);
    require(exhaustive.probability == c::make_rational(100, 1) && exhaustive.attempts == 150 && c::candidate_count(exhaustive) == 0,
            "150 contiguous inhibitions end at exactly 100");
    // LCP-P03: sums 3/4/5, peer removal, finite replenishment, immutable RTES.
    auto sum = c::start_generic_ratdma(0, 150, {10, 40, 80, 120});
    require(!c::replenish_candidates(sum, 5, {20, 30}).needed, "sum 4 does not replenish");
    auto five_sum = c::start_generic_ratdma(0, 150, {10, 40, 80, 120, 130});
    require(!c::replenish_candidates(five_sum, 5, {20}).needed && c::candidate_count(five_sum) == 5, "sum 5 does not replenish");
    require(c::remove_peer_candidate(sum, 40) && !c::remove_peer_candidate(sum, 40) && c::candidate_count(sum) == 3 &&
            sum.attempts == 0 && sum.probability == quarter, "peer removal consumes no attempt and keeps p");
    const auto refill = c::replenish_candidates(sum, 5, {5, 10, 60, 60, 160});
    require(refill.needed && refill.added == 1 && refill.status == "replenished" && sum.candidates == std::vector<std::int64_t>{10, 60, 80, 120} &&
            sum.original_rtes_index == 150, "sum 3 replenishes one eligible slot in (current, RTES] excluding duplicates/out-of-range");
    (void)c::remove_peer_candidate(sum, 60);
    const auto none = c::replenish_candidates(sum, 5, {5, 10, 160});
    require(none.needed && none.added == 0 && none.status == "no_eligible" && none.shortfall == 1, "no eligible replacement is explicit, not fabricated");
    auto attempted = c::start_generic_ratdma(0, 150, {10, 40, 80, 120});
    (void)c::ratdma_attempt(attempted, 99);
    (void)c::remove_peer_candidate(attempted, 40);
    const auto partial = c::replenish_candidates(attempted, 11, {10, 40, 50});
    require(partial.needed && partial.added == 1 && partial.status == "replenished" && attempted.candidates == std::vector<std::int64_t>{50, 80, 120},
            "attempted and removed slots are never re-added");
    const auto near_max = c::start_generic_ratdma(INT64_MAX - 150, INT64_MAX, {INT64_MAX - 3, INT64_MAX - 2, INT64_MAX - 1, INT64_MAX});
    require(near_max.original_rtes_index == INT64_MAX, "checked RTES arithmetic accepts the top of the index domain");
    invalid("RATDMA domain", [] { (void)c::start_generic_ratdma(INT64_MIN, 10, {1, 2, 3, 4}); });  // Unchecked subtraction would wrap.
    invalid("RATDMA domain", [] { (void)c::start_generic_ratdma(0, 150, {10, 40, 80}); });
    invalid("RATDMA domain", [] { (void)c::start_generic_ratdma(0, 150, {10, 40, 80, 151}); });
    invalid("RATDMA domain", [] { (void)c::start_generic_ratdma(0, 151, {10, 40, 80, 120}); });
    invalid("RATDMA domain", [] { (void)c::start_generic_ratdma(0, 0, {10, 40, 80, 120}); });
    invalid("RATDMA domain", [] { (void)c::start_generic_ratdma(0, 150, {40, 10, 80, 120}); });
    invalid("RATDMA domain", [] { (void)c::start_generic_ratdma(0, 150, {10, 10, 80, 120}); });
    invalid("RATDMA domain", [] { (void)c::start_generic_ratdma(0, 150, {0, 10, 80, 120}); });
    invalid("RATDMA domain", [&] { auto probe = state; (void)c::ratdma_attempt(probe, 101); });
    invalid("RATDMA domain", [&] { auto probe = state; (void)c::replenish_candidates(probe, 5, {30, 20}); });
    invalid("RATDMA domain", [&] { (void)c::ratdma_permits(state.gap, 101); });
}

void selection_and_adjacency() {
    const auto interval = c::selection_interval(1000, 375);
    require(interval.first_index == 963 && interval.last_index == 1037 && interval.width == 75 && interval.half_width == 37,
            "NI375 NS1000 window includes +37");
    require(interval.last_index < 1038, "NI375 NS1000 excludes +38");
    for (const auto [ni, width] : {std::pair{75, 15}, std::pair{125, 25}, std::pair{225, 45}, std::pair{375, 75}}) {
        const auto value = c::selection_interval(50, ni);
        require(value.width == width && value.last_index - value.first_index + 1 == width, "LCP-P01 widths");
    }
    const auto wrapped = c::selection_interval(-3, 75);
    require(wrapped.first_index == -10 && wrapped.last_index == 4 && c::frame_slot(wrapped.first_index) == 2240 &&
            c::frame_of_index(wrapped.first_index) == -1, "signed wrap across frames");
    require(c::selection_interval(10, 750, 10).ni == 75, "exact rational reduces to a qualified NI");
    invalid("LCP-P01 domain", [] { (void)c::selection_interval(10, 75, 2); });
    invalid("LCP-P01 domain", [] { (void)c::selection_interval(10, 100); });
    invalid("LCP-P01 domain", [] { (void)c::selection_interval(10, 1125); });
    invalid("LCP-P01 domain", [] { (void)c::selection_interval(10, 0); });
    invalid("LCP-P01 domain", [] { (void)c::selection_interval(10, 75, 0); });
    const auto zero = c::own_adjacency_exclusions({{0, 1, 0}});
    require(zero == std::vector<std::int64_t>{-1, 0, 1}, "adjacency at j=0 wraps to the previous frame slot");
    const auto last = c::own_adjacency_exclusions({{2249, 1, 0}});
    require(last == std::vector<std::int64_t>{2248, 2249, 2250}, "adjacency at j=2249 wraps to the next frame slot");
    const auto block = c::own_adjacency_exclusions({{100, 3, 1}});
    require(block == std::vector<std::int64_t>{99, 100, 101, 102, 103, 2349, 2350, 2351, 2352, 2353}, "multi-slot own block in two frames");
    require(!std::binary_search(zero.begin(), zero.end(), 2) && !std::binary_search(zero.begin(), zero.end(), -2), "j+/-2 not excluded by adjacency alone");
    invalid("LCP-P05 domain", [] { (void)c::own_adjacency_exclusions({{0, 6, 0}}); });
    invalid("LCP-P05 domain", [] { (void)c::own_adjacency_exclusions({{0, 1, 8}}); });
}

void timeout_and_expiry() {
    require(c::continuous_timeout_sequence(3) == std::vector<std::uint8_t>{2, 1, 0} &&
            c::continuous_timeout_sequence(7) == std::vector<std::uint8_t>{6, 5, 4, 3, 2, 1, 0}, "LCP-P02 sequences");
    const auto visit = c::continuous_timeout_visit(1);
    require(visit.advertised_timeout == 0 && visit.reselect && !c::continuous_timeout_visit(2).reselect, "single decrement before zero test");
    invalid("LCP-P02 domain", [] { (void)c::continuous_timeout_visit(0); });
    invalid("LCP-P02 domain", [] { (void)c::continuous_timeout_visit(8); });
    invalid("LCP-P02 domain", [] { (void)c::continuous_timeout_sequence(2); });
    for (const int drawn : {3, 4, 5, 6, 7})
        require(c::retained_frames_from_timeout_draw(drawn) + 1u == c::continuous_timeout_sequence(static_cast<std::uint8_t>(drawn)).size(),
                "retained frames after the first use equal drawn timeout minus one");
    invalid("LCP-P02 domain", [] { (void)c::retained_frames_from_timeout_draw(2); });
    invalid("LCP-P02 domain", [] { (void)c::retained_frames_from_timeout_draw(8); });
    const auto announcement = c::expiry_announcement(2249, 2250, a::SyncState::UtcDirect);
    require(announcement.slot_offset == 1 && announcement.state.raw().value() == 1u, "next-frame offset 2249 -> 0");
    require(c::expiry_announcement(100, 2450, a::SyncState::BaseDirect).state.raw().value() == ((2u << 17) | 2350u), "offset 2250+200-100");
    require(c::expiry_announcement(0, 4499, a::SyncState::UtcDirect).slot_offset == 4499, "maximum next-frame offset");
    invalid("LCP-P02 domain", [] { (void)c::expiry_announcement(100, 200, a::SyncState::UtcDirect); });
    invalid("LCP-P02 domain", [] { (void)c::expiry_announcement(100, 4600, a::SyncState::UtcDirect); });
    require(c::range_class(222239999999LL) == c::RangeClass::Within120Nm && c::range_class(222240000000LL) == c::RangeClass::Within120Nm &&
            c::range_class(222240000001LL) == c::RangeClass::Beyond120Nm, "exactly 120 NM is within; one micrometre beyond is beyond");
    const auto point = c::ais_position_to_nanodegrees(2500, -2500);
    require(point.longitude_ndeg == 4166667 && point.latitude_ndeg == -4166667 && c::ais_position_to_nanodegrees(3, 3).longitude_ndeg == 5000 &&
            c::ais_position_to_nanodegrees(1, -1).longitude_ndeg == 1667 && c::ais_position_to_nanodegrees(1, -1).latitude_ndeg == -1667,
            "AIS to nanodegree conversion");
    invalid("position domain", [] { (void)c::ais_position_to_nanodegrees(108000001, 0); });
    invalid("position domain", [] { (void)c::ais_position_to_nanodegrees(0, -54000001); });
}

void map_contract() {
    c::LocalSlotMap map(kOwnerA, c::Channel::Ais1);
    invalid("map instant", [&] { c::OwnReservation reservation; reservation.first_index = 5; reservation.power_generation = 1; map.reserve_own(reservation); });
    map.advance(2300);
    invalid("map instant", [&] { map.advance(2299); });
    c::OwnReservation own;
    own.channel = c::Channel::Ais1; own.first_index = 2400; own.block_length = 2; own.kind = c::OwnReservationKind::Sotdma;
    own.timeout_frames = 3; own.power_generation = 1; own.cause = "unit fixture";
    map.reserve_own(own);
    require(map.own_covering(2400) && map.own_covering(2401) && map.own_covering(2400 + 3 * kFrame) && !map.own_covering(2400 + 4 * kFrame) &&
            !map.own_covering(2402) && map.view(2400).state == c::SlotState::InternalOwn, "own SOTDMA block retained for its timeout frames");
    invalid("own overlap", [&] { auto twin = own; twin.first_index = 2401 + kFrame; map.reserve_own(twin); });
    invalid("own domain", [&] { auto bad = own; bad.first_index = 2299; map.reserve_own(bad); });
    invalid("own domain", [&] { auto bad = own; bad.first_index = 2300 + 9 * kFrame; map.reserve_own(bad); });
    invalid("own domain", [&] { auto bad = own; bad.block_length = 6; map.reserve_own(bad); });
    invalid("own domain", [&] { auto bad = own; bad.timeout_frames = 7; map.reserve_own(bad); });  // Drawn 7 retains six more frames.
    for (const int drawn : {3, 7}) {
        // Own retention follows LCP-P02: a drawn T is used in exactly T frames.
        c::LocalSlotMap probe(kOwnerA, c::Channel::Ais1);
        probe.advance(2300);
        c::OwnReservation continuous = own; continuous.first_index = 2400; continuous.timeout_frames = c::retained_frames_from_timeout_draw(drawn);
        probe.reserve_own(continuous);
        unsigned frames = 0;
        for (std::int64_t frame = 0; frame < 9; ++frame) if (probe.own_covering(2400 + frame * kFrame)) ++frames;
        require(frames == static_cast<unsigned>(drawn), "own slot is covered in exactly the drawn number of frames");
    }
    invalid("own domain", [&] { auto bad = own; bad.keep = true; map.reserve_own(bad); });
    invalid("own domain", [&] { auto bad = own; bad.channel = c::Channel::Ais2; map.reserve_own(bad); });
    invalid("own domain", [&] { auto bad = own; bad.power_generation = 0; map.reserve_own(bad); });
    c::ExternalAllocation external;
    external.channel = c::Channel::Ais1; external.first_index = 90; external.block_length = 1; external.mmsi = 777;
    external.basis = c::AllocationBasis::SotdmaTimeout; external.announced_minute = 0; external.reserved_through_minute = 7; external.announced_index = 90;
    external.last_use_minute = 0; external.receipt_id = 1; external.power_generation = 1;
    map.record_external(external);
    require(map.view(90 + kFrame).state == c::SlotState::ExternalObserved && map.view(91).state == c::SlotState::Free, "external SOTDMA allocation covers later frames");
    external.receipt_id = 2; external.reserved_through_minute = 5;
    map.record_external(external);
    require(map.external_count() == 1 && map.external_allocations().front().reserved_through_minute == 5, "refresh replaces the same peer/slot allocation");
    map.advance(4 * kFrame + 10);
    const auto aged = map.view(90 + 4 * kFrame);
    require(aged.state == c::SlotState::Free && aged.live.empty() && aged.aged.size() == 1, "unused SOTDMA allocation ages out after three preceding frames");
    c::ExternalAllocation itdma = external;
    itdma.first_index = 4 * kFrame + 500; itdma.basis = c::AllocationBasis::ItdmaIncrement; itdma.announced_minute = 4;
    itdma.reserved_through_minute = 4; itdma.last_use_minute = 4; itdma.receipt_id = 3; itdma.block_length = 3; itdma.announced_index = 4 * kFrame + 5;
    require(map.record_external(itdma), "ITDMA block recorded");
    require(map.view(4 * kFrame + 502).state == c::SlotState::ExternalObserved && map.view(4 * kFrame + 503).state == c::SlotState::Free,
            "ITDMA block is not subject to SOTDMA aging and respects its block length");
    c::GarbleRecord garble;
    garble.channel = c::Channel::Ais1; garble.index = 4 * kFrame + 5; garble.disclosed_at = at_slot(4 * kFrame + 6); garble.receipt_id = 4;
    garble.disposition = c::ObservationDisposition::FailedFcs;
    map.record_garble(garble);
    require(map.view(4 * kFrame + 5).state == c::SlotState::GarbledObserved && map.garble_count() == 1, "garble recorded without identity");
    invalid("garble domain", [&] { auto bad = garble; bad.index = 4 * kFrame + 11; map.record_garble(bad); });
    invalid("garble domain", [&] { auto bad = garble; bad.disposition = c::ObservationDisposition::Decoded; map.record_garble(bad); });
    invalid("external domain", [&] { auto bad = itdma; bad.mmsi = 0; map.record_external(bad); });
    invalid("external domain", [&] { auto bad = itdma; bad.reserved_through_minute = 3; map.record_external(bad); });
    invalid("external domain", [&] { auto bad = itdma; bad.reserved_through_minute = 12; map.record_external(bad); });
    invalid("external domain", [&] { auto bad = itdma; bad.reserved_through_minute = 5; map.record_external(bad); });  // One-shot block is not periodic.
    invalid("external domain", [&] { auto bad = itdma; bad.last_use_minute = 3; map.record_external(bad); });  // Used before announced.
    invalid("external domain", [&] { auto bad = itdma; bad.announced_minute = -5; bad.last_use_minute = -5; map.record_external(bad); });  // Announced > 8 frames before.
    invalid("external domain", [&] { auto bad = external; bad.first_index = 4 * kFrame + 600; bad.announced_minute = 4; bad.announced_index = 4 * kFrame + 5; bad.last_use_minute = 4; bad.reserved_through_minute = INT64_MAX; map.record_external(bad); });
    {
        // A one-shot ITDMA block straddling the frame boundary never reappears in the following frame.
        c::LocalSlotMap straddle(kOwnerA, c::Channel::Ais2);
        straddle.advance(10 * kFrame);
        c::ExternalAllocation block = itdma; block.channel = c::Channel::Ais2; block.first_index = 10 * kFrame + 2248; block.block_length = 5;
        block.announced_minute = 10; block.announced_index = 10 * kFrame; block.last_use_minute = 10; block.reserved_through_minute = 10;
        require(straddle.record_external(block), "straddling block recorded");
        require(straddle.view(10 * kFrame + 2249).state == c::SlotState::ExternalObserved && straddle.view(11 * kFrame + 2).state == c::SlotState::ExternalObserved &&
                straddle.view(11 * kFrame + 3).state == c::SlotState::Free && straddle.view(11 * kFrame + 2248).state == c::SlotState::Free &&
                straddle.view(12 * kFrame).state == c::SlotState::Free, "straddling ITDMA block covers exactly its five absolute slots");
        // SOTDMA announcements of one peer at one frame slot refresh in place; a physically newer one supersedes an older late arrival.
        c::ExternalAllocation sotdma = external; sotdma.channel = c::Channel::Ais2; sotdma.first_index = 10 * kFrame + 100; sotdma.announced_minute = 10;
        sotdma.last_use_minute = 10; sotdma.reserved_through_minute = 13; sotdma.receipt_id = 7; sotdma.announced_index = 10 * kFrame;
        require(straddle.record_external(sotdma), "first announcement");
        auto next = sotdma; next.first_index += kFrame; next.announced_minute = 11; next.announced_index = 11 * kFrame + 100; next.last_use_minute = 11; next.reserved_through_minute = 13; next.receipt_id = 8;
        straddle.advance(11 * kFrame + 100);
        require(straddle.record_external(next) && straddle.external_count() == 2, "next-frame refresh replaces the older record of the same frame slot");
        require(!straddle.record_external(sotdma) && straddle.external_count() == 2, "an older announcement never supersedes a newer one");
        auto other_peer = next; other_peer.mmsi = 778;
        require(straddle.record_external(other_peer) && straddle.external_count() == 3, "another peer at the same slot is a separate record");
        const auto shared = straddle.view(11 * kFrame + 100);
        require(shared.live.size() == 2 && shared.live.front()->mmsi == 777 && shared.live.back()->mmsi == 778, "live records are canonically ordered");
    }
    {
        // Equal first index: a next-frame offset announced in frame F and the use of that slot in frame F+1 share the
        // first index; the physically newer announcement wins regardless of delivery order. ITDMA records follow the
        // announcing slot as well.
        c::LocalSlotMap order(kOwnerA, c::Channel::Ais1);
        order.advance(12 * kFrame + 700);
        c::ExternalAllocation use = external; use.first_index = 12 * kFrame + 600; use.announced_minute = 12; use.announced_index = 12 * kFrame + 600;
        use.last_use_minute = 12; use.reserved_through_minute = 18; use.receipt_id = 20;
        c::ExternalAllocation offset = use; offset.basis = c::AllocationBasis::SotdmaNextFrameOffset; offset.announced_minute = 11; offset.announced_index = 11 * kFrame + 10;
        offset.last_use_minute = 11; offset.reserved_through_minute = 12; offset.receipt_id = 21;
        require(order.record_external(use) && !order.record_external(offset) && order.external_count() == 1 &&
                order.external_allocations().front().basis == c::AllocationBasis::SotdmaTimeout, "a late offset announcement never replaces the newer use");
        c::LocalSlotMap reverse(kOwnerA, c::Channel::Ais1);
        reverse.advance(12 * kFrame + 700);
        require(reverse.record_external(offset) && reverse.record_external(use) && reverse.external_count() == 1 &&
                reverse.external_allocations().front().basis == c::AllocationBasis::SotdmaTimeout, "in-order delivery refreshes the offset record");
        c::ExternalAllocation announced = itdma; announced.first_index = 12 * kFrame + 1200; announced.announced_minute = 12; announced.announced_index = 12 * kFrame + 200;
        announced.last_use_minute = 12; announced.reserved_through_minute = 12; announced.receipt_id = 22; announced.block_length = 5;
        require(reverse.record_external(announced), "ITDMA block recorded");
        c::ExternalAllocation earlier = announced; earlier.announced_index = 12 * kFrame + 100; earlier.block_length = 1; earlier.receipt_id = 23;
        require(!reverse.record_external(earlier) && reverse.external_allocations().back().block_length == 5, "an earlier ITDMA announcement of the same block is superseded");
        c::ExternalAllocation newest = announced; newest.announced_index = 12 * kFrame + 300; newest.block_length = 2; newest.receipt_id = 24;
        require(reverse.record_external(newest) && reverse.external_allocations().back().block_length == 2, "a later announcement replaces it");
        invalid("external domain", [&] { auto bad = use; bad.announced_index = 11 * kFrame + 600; reverse.record_external(bad); });  // Outside the announced frame.
        invalid("external domain", [&] { auto bad = use; bad.announced_index = 12 * kFrame + 701; reverse.record_external(bad); });  // After the map instant.
        require(order.snapshot(INT64_MAX - 3, INT64_MAX).empty(), "snapshot terminates at the top of the index domain");
        invalid("own domain", [&] { c::OwnReservation crossing = own; crossing.first_index = 12 * kFrame + 2249; crossing.timeout_frames = 0; order.reserve_own(crossing); });
    }
    invalid("external domain", [&] { auto bad = itdma; bad.announced_minute = 5; map.record_external(bad); });
    invalid("external domain", [&] { auto bad = itdma; bad.first_index = 13 * kFrame; map.record_external(bad); });
    invalid("external domain", [&] { auto bad = itdma; bad.channel = c::Channel::Ais2; map.record_external(bad); });
    require(map.external_allocations().back().provenance == c::EvidenceProvenance::UnitFixture && map.garbles().front().provenance == c::EvidenceProvenance::UnitFixture,
            "map records keep their UNIT_FIXTURE label");
    const auto rows = map.snapshot(4 * kFrame, 4 * kFrame + 600);
    require(rows.size() == 7 && rows.front().index == 4 * kFrame + 5 && rows[1].index == 4 * kFrame + 90 && rows[1].state == c::SlotState::Free &&
            rows[1].detail.find("aged:777") != std::string::npos && rows[2].index == 2400 + 3 * kFrame && rows[2].state == c::SlotState::InternalOwn &&
            rows[3].index == 2401 + 3 * kFrame && rows.back().index == 4 * kFrame + 502, "canonical ascending snapshot rows");
    invalid("snapshot domain", [&] { (void)map.snapshot(10, 5); });
    invalid("snapshot domain", [&] { (void)map.snapshot(INT64_MIN, 1); });  // The width check must not overflow.
    require(map.snapshot(-5, -1).empty() && map.own_covering(INT64_MAX) == nullptr && map.own_covering(INT64_MIN) == nullptr &&
            map.view(INT64_MIN).state == c::SlotState::Free && map.view(INT64_MAX).state == c::SlotState::Free, "extreme indices are safe");
    c::LocalSlotMap bounded(kOwnerA, c::Channel::Ais2, c::MapLimits{4, 8, 1, 2, 1});
    bounded.advance(0);
    c::OwnReservation small; small.channel = c::Channel::Ais2; small.first_index = 10; small.power_generation = 1;
    bounded.reserve_own(small);
    invalid("map capacity", [&] { auto more = small; more.first_index = 20; bounded.reserve_own(more); });
    c::ExternalAllocation one = external; one.channel = c::Channel::Ais2; one.first_index = 30; one.reserved_through_minute = 0; one.announced_minute = 0; one.announced_index = 0;
    bounded.record_external(one);
    one.first_index = 31; bounded.record_external(one);
    invalid("map capacity", [&] { one.first_index = 32; bounded.record_external(one); });
    c::GarbleRecord noise; noise.channel = c::Channel::Ais2; noise.index = 0; noise.disposition = c::ObservationDisposition::EnergyOnly;
    bounded.record_garble(noise);
    invalid("map capacity", [&] { noise.index = 1; bounded.record_garble(noise); });  // Index 0 remains.
    invalid("map limits", [] { (void)c::LocalSlotMap(kOwnerA, c::Channel::Ais1, c::MapLimits{0, 8, 1, 1, 1}); });
    invalid("map limits", [] { (void)c::LocalSlotMap(kOwnerA, c::Channel::Ais1, c::MapLimits{3, 8, 1, 1, 1}); });  // Past horizon must exceed SOTDMA aging.
    invalid("map limits", [] { (void)c::LocalSlotMap(c::OwnIdentity{{11}, {0}, {31}}, c::Channel::Ais1); });
    c::PeerDirectory directory(kOwnerA, 2);
    c::PeerRecord record; record.mmsi = 5; record.last_heard_index = 10;
    directory.upsert(record); record.mmsi = 6; directory.upsert(record);
    invalid("directory capacity", [&] { record.mmsi = 7; directory.upsert(record); });
    record.mmsi = 0; invalid("directory domain", [&] { directory.upsert(record); });
    record.mmsi = 6; record.reservations.push_back({c::Channel::Ais1, 7 * kFrame, 1, 0, 7, c::AllocationBasis::ItdmaIncrement, 1}); directory.upsert(record);
    require(directory.expire_stale(11, 5) == 1 && directory.size() == 1 && directory.find(6), "a stale peer with a still-reaching announcement is retained");
    require(directory.expire_stale(11, 7 * kFrame) == 0 && directory.expire_stale(11, 7 * kFrame + 1) == 1 && directory.size() == 0,
            "deterministic peer expiry once the announcement's last slot has passed");
    invalid("directory domain", [&] { c::PeerRecord broken; broken.mmsi = 9; broken.reservations.push_back({c::Channel::Ais1, 7 * kFrame, 1, 0, 6, c::AllocationBasis::ItdmaIncrement, 1}); directory.upsert(broken); });
    invalid("directory domain", [] { (void)c::PeerDirectory(kOwnerA, 0); });
    invalid("directory domain", [] { (void)c::PeerDirectory(c::OwnIdentity{{11}, {21}, {0}}); });
}

struct Pair {
    std::vector<c::OwnStationConfig> configs = configurations();
    c::ScenarioSeedHandoff seed = handoff();
    Station a{configs[0], seed};
    Station b{configs[1], seed};
    Pair() { a.on({0}, 1); b.on({0}, 1); }
};

void evidence_contract() {
    Pair pair;
    auto& a_station = pair.a;
    const std::uint32_t peer_b = 2;
    const auto now = at_slot(100);
    const auto inputs = a_station.qualify(now);
    // Type 1 with SOTDMA timeout 3 and the received-stations submessage at slot 90.
    const auto timeout3 = a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 3, 5);
    const auto peer_position = peer_at(a_station.location, 90000000000LL, 20000.0);
    auto first = observation(a_station, c::Channel::Ais1, 1, 90, sotdma_facts(peer_b, timeout3, peer_position));
    const auto accepted = a_station.knowledge.accept_evidence(mint(first), inputs, now);
    require(accepted.status == "decoded_peer_evidence_accepted" && accepted.peer == peer_b && accepted.received_index == 90 &&
            accepted.allocations.size() == 1 && accepted.allocations.front().basis == c::AllocationBasis::SotdmaTimeout &&
            accepted.allocations.front().reserved_through_minute == 3, "SOTDMA timeout 3 reserves the slot through three more frames");
    const auto* record = a_station.knowledge.directory().find(peer_b);
    require(record && record->kind == c::PeerKind::Mobile && record->position_available && record->position_in_geodesic_domain &&
            record->received_station_count == 5u && record->sync_state == a::SyncState::UtcDirect && record->receipts == 1 &&
            record->reservations.size() == 1 && record->provenance == c::EvidenceProvenance::UnitFixture, "directory learns only decoded peer facts");
    require(a_station.knowledge.map(c::Channel::Ais1).view(90).state == c::SlotState::ExternalObserved &&
            a_station.knowledge.map(c::Channel::Ais2).view(90).state == c::SlotState::Free, "reservation is channel specific");
    // Zero timeout with next-frame offset: slot 92 -> next frame slot 120 (offset 2250 + 120 - 92).
    const auto zero = a::SotdmaState19::offset_in_next_frame(a::SyncState::UtcDirect, 92, 120);
    auto second = observation(a_station, c::Channel::Ais2, 2, 92, sotdma_facts(peer_b, zero, peer_position));
    const auto target = a_station.knowledge.accept_evidence(mint(second), inputs, now);
    require(target.allocations.size() == 2 && target.allocations[1].first_index == kFrame + 120 &&
            target.allocations[1].basis == c::AllocationBasis::SotdmaNextFrameOffset && target.allocations[0].reserved_through_minute == 0 &&
            target.unresolved.empty() && target.superseded.empty(), "zero timeout announces the next-frame slot and releases the present one");
    // FCS-valid decodes with an implausible announcement: the observed use is learned, the announcement is reported unresolved.
    auto same_frame = observation(a_station, c::Channel::Ais2, 3, 93, sotdma_facts(peer_b, a::SotdmaState19::offset(a::SyncState::UtcDirect, 10), peer_position));
    const auto unresolved_offset = a_station.knowledge.accept_evidence(mint(same_frame), inputs, now);
    require(unresolved_offset.allocations.size() == 1 && unresolved_offset.allocations.front().basis == c::AllocationBasis::ObservedUse &&
            unresolved_offset.unresolved.size() == 1 && unresolved_offset.unresolved.front().rfind("sotdma_offset_not_next_frame:10", 0) == 0,
            "same-frame offset: observed use learned, announcement unresolved");
    auto wrong_slot = observation(a_station, c::Channel::Ais2, 4, 94, sotdma_facts(peer_b, a::SotdmaState19::current_slot(a::SyncState::UtcDirect, 2, 95), peer_position));
    const auto disagreeing = a_station.knowledge.accept_evidence(mint(wrong_slot), inputs, now);
    require(disagreeing.allocations.size() == 1 && disagreeing.allocations.front().basis == c::AllocationBasis::SotdmaTimeout &&
            disagreeing.allocations.front().reserved_through_minute == 2 && disagreeing.unresolved.size() == 1 &&
            disagreeing.unresolved.front().rfind("slot_number_submessage_disagrees_with_own_utc:95", 0) == 0,
            "slot-number disagreement is reported; Table 18 retention still applies");
    // Message 20 from a base station: explicitly deferred, slot use and base identity only.
    auto link = observation(a_station, c::Channel::Ais2, 5, 95, sotdma_facts(8, timeout3, std::nullopt, c::DecodedMessageKind::Type20));
    link.decoded->raw_communication_state = -1;  // Message 20 carries no communication state.
    const auto deferred = a_station.knowledge.accept_evidence(mint(link), inputs, now);
    require(deferred.status == "decoded_base_message_deferred" && deferred.allocations.size() == 1 && deferred.allocations.front().kind == c::PeerKind::Base &&
            deferred.allocations.front().basis == c::AllocationBasis::ObservedUse && deferred.unresolved.size() == 1 &&
            deferred.unresolved.front().rfind("message_20_data_link_management_deferred", 0) == 0 &&
            a_station.knowledge.directory().find(8)->kind == c::PeerKind::Base, "Message 20 is classified deferred");
    auto right_slot = observation(a_station, c::Channel::Ais2, 6, 96, sotdma_facts(peer_b, a::SotdmaState19::current_slot(a::SyncState::UtcDirect, 2, 96), peer_position));
    const auto agreeing = a_station.knowledge.accept_evidence(mint(right_slot), inputs, now);
    require(agreeing.allocations.front().reserved_through_minute == 2 && agreeing.unresolved.empty(), "current-slot submessage consistent with own UTC");
    // Type 3 ITDMA: increment 37, two slots, keep.
    const auto itdma = a::ItdmaState19::from_effective(a::SyncState::UtcDirect, 37, 2, true);
    auto third = observation(a_station, c::Channel::Ais1, 7, 97, itdma_facts(3, itdma, peer_position));
    const auto announced = a_station.knowledge.accept_evidence(mint(third), inputs, now);
    require(announced.allocations.size() == 2 && announced.allocations[0].basis == c::AllocationBasis::ItdmaKeep &&
            announced.allocations[0].reserved_through_minute == 1 && announced.allocations[1].first_index == 134 &&
            announced.allocations[1].block_length == 2 && announced.allocations[1].basis == c::AllocationBasis::ItdmaIncrement,
            "ITDMA increment and keep allocations");
    require(a_station.knowledge.map(c::Channel::Ais1).view(135).state == c::SlotState::ExternalObserved &&
            a_station.knowledge.map(c::Channel::Ais1).view(136).state == c::SlotState::Free &&
            a_station.knowledge.map(c::Channel::Ais1).view(97 + kFrame).state == c::SlotState::ExternalObserved, "announced block and kept slot");
    const auto terminal = a::ItdmaState19::from_fields(a::SyncState::UtcDirect, 0, 0, false);
    auto fourth = observation(a_station, c::Channel::Ais1, 8, 98, itdma_facts(3, terminal, peer_position));
    require(a_station.knowledge.accept_evidence(mint(fourth), inputs, now).allocations.size() == 1, "increment zero makes no further allocation");
    // A five-slot ITDMA block announced across the frame boundary (98 + 4400 = frame 1 slot 2248) is one-shot.
    auto straddle = observation(a_station, c::Channel::Ais2, 9, 98, itdma_facts(5, a::ItdmaState19::from_effective(a::SyncState::UtcDirect, 4400, 5, false), peer_position));
    const auto crossing = a_station.knowledge.accept_evidence(mint(straddle), inputs, now);
    require(crossing.allocations.size() == 2 && crossing.allocations[1].first_index == kFrame + 2248 && crossing.allocations[1].reserved_through_minute == 1 &&
            a_station.knowledge.map(c::Channel::Ais2).view(kFrame + 2249).state == c::SlotState::ExternalObserved &&
            a_station.knowledge.map(c::Channel::Ais2).view(2 * kFrame + 2).state == c::SlotState::ExternalObserved &&
            a_station.knowledge.map(c::Channel::Ais2).view(2 * kFrame + 3).state == c::SlotState::Free &&
            a_station.knowledge.map(c::Channel::Ais2).view(2 * kFrame + 2248).state == c::SlotState::Free,
            "straddling ITDMA block covers its five absolute slots and nothing in the following frame");
    // EnergyOnly garble spanning three slots (disclosed early in slot 99), then Type 4 base evidence and a FailedFcs garble.
    auto energy = observation(a_station, c::Channel::Ais2, 10, 97, std::nullopt, c::ObservationDisposition::EnergyOnly);
    energy.physical_end = {97 * kSlotUnits + 2 * kSlotUnits + 100}; energy.disclosed_at = energy.physical_end;
    require(a_station.knowledge.accept_evidence(mint(energy), inputs, now).span_slots == 3 &&
            a_station.knowledge.map(c::Channel::Ais2).view(99).state == c::SlotState::GarbledObserved, "multi-slot energy garble");
    auto base = observation(a_station, c::Channel::Ais1, 11, 99, sotdma_facts(4, timeout3, peer_position, c::DecodedMessageKind::Type4));
    require(a_station.knowledge.accept_evidence(mint(base), inputs, now).allocations.front().kind == c::PeerKind::Base &&
            a_station.knowledge.directory().find(4)->kind == c::PeerKind::Base, "base station evidence");
    auto noise = observation(a_station, c::Channel::Ais1, 12, 99, std::nullopt, c::ObservationDisposition::FailedFcs);
    const auto garbled = a_station.knowledge.accept_evidence(mint(noise), inputs, now);
    require(garbled.status == "garble_recorded_no_identity" && !garbled.peer && garbled.allocations.empty() &&
            a_station.knowledge.directory().size() == 5 && a_station.knowledge.map(c::Channel::Ais1).garble_count() == 1,
            "bad FCS adds no peer, reservation or synchronization evidence");
    const auto snapshot = a_station.knowledge.snapshot();
    require(snapshot.last_receipt_id == 12 && snapshot.external_ais1 == 5 && snapshot.external_ais2 == 8 && snapshot.garble_ais1 == 1 &&
            snapshot.garble_ais2 == 3 && !pair.b.knowledge.directory().find(1), "snapshot counters; B learned nothing from A's receipts");
    // Rejections, each before any mutation.
    const auto before = a_station.knowledge.snapshot();
    auto reject = [&](const char* category, auto mutate) {
        auto value = observation(a_station, c::Channel::Ais1, 13, 99, sotdma_facts(peer_b, timeout3, peer_position));
        mutate(value);
        invalid(category, [&] { (void)a_station.knowledge.accept_evidence(mint(value), inputs, now); });
    };
    reject("evidence owner", [](auto& o) { o.owner.vessel_id = {12}; });
    reject("evidence owner", [](auto& o) { o.owner.equipment_id = {22}; });
    reject("evidence owner", [](auto& o) { o.receiver = {32}; });
    reject("evidence channel", [](auto& o) { o.channel = static_cast<c::Channel>(2); });
    reject("evidence epoch", [](auto& o) { o.power_generation = 2; });
    reject("evidence epoch", [](auto& o) { o.power_generation = 0; });
    reject("evidence catalog", [](auto& o) { o.catalog_revision = 18; });
    reject("evidence replay", [](auto& o) { o.receipt_id = 12; });
    reject("evidence replay", [](auto& o) { o.receipt_id = 3; });
    reject("evidence time", [](auto& o) { o.disclosed_at = o.physical_start; });
    reject("evidence time", [](auto& o) { o.disclosed_at = at_slot(101); });
    reject("evidence time", [](auto& o) { o.physical_end = o.physical_start; });
    reject("evidence time", [](auto& o) { o.physical_start = {-1}; o.physical_end = {5}; });
    reject("evidence time", [](auto& o) { o.physical_start = at_slot(101); o.physical_end = at_slot(102); o.disclosed_at = at_slot(102); });
    reject("evidence time", [](auto& o) { o.physical_end = {99 * kSlotUnits + 6 * kSlotUnits}; o.disclosed_at = o.physical_end; });
    reject("evidence time", [](auto& o) { o.physical_end = {99 * kSlotUnits + 2 * kSlotUnits}; o.disclosed_at = o.physical_end; });
    reject("evidence identity", [](auto& o) { o.decoded->source_mmsi = 1; });
    reject("evidence identity", [](auto& o) { o.decoded->source_mmsi = 0; });
    reject("evidence identity", [](auto& o) { o.decoded->source_mmsi = 0x40000000u; });
    reject("evidence malformed", [](auto& o) { o.decoded->longitude = 108000001; });
    reject("evidence malformed", [](auto& o) { o.decoded->latitude = -54000001; });
    reject("evidence malformed", [](auto& o) { o.decoded->raw_communication_state = 0x80000; });
    reject("evidence malformed", [](auto& o) { o.decoded->raw_communication_state = -1; });
    reject("evidence malformed", [](auto& o) { o.decoded->navigation_status = 16; });
    reject("evidence malformed", [](auto& o) { o.decoded->kind = static_cast<c::DecodedMessageKind>(5); });
    reject("evidence malformed", [](auto& o) { o.decoded.reset(); });
    reject("evidence malformed", [](auto& o) { o.disposition = c::ObservationDisposition::EnergyOnly; });
    const auto after = a_station.knowledge.snapshot();
    require(after.last_receipt_id == before.last_receipt_id && after.external_ais1 == before.external_ais1 && after.peers.size() == before.peers.size() &&
            after.knowledge_revision == before.knowledge_revision, "rejected evidence mutates nothing");
    // A strict-codec rejection of the communication state (non-canonical UTC bits) is an FCS-valid decode: use learned, announcement unresolved.
    auto undecodable = observation(a_station, c::Channel::Ais1, 13, 99, sotdma_facts(peer_b, timeout3, peer_position));
    undecodable.decoded->raw_communication_state = 0x4001;
    const auto undecoded = a_station.knowledge.accept_evidence(mint(undecodable), inputs, now);
    require(undecoded.allocations.size() == 1 && undecoded.allocations.front().basis == c::AllocationBasis::ObservedUse && undecoded.unresolved.size() == 1 &&
            undecoded.unresolved.front().rfind("communication_state_undecodable:", 0) == 0, "undecodable communication state: use learned, announcement unresolved");
    // The maximum extended ITDMA increment (16383) always lands inside the eight-frame horizon.
    auto extreme = observation(a_station, c::Channel::Ais1, 14, 99,
                               itdma_facts(3, a::ItdmaState19::from_effective(a::SyncState::UtcDirect, 16383, 3, false), peer_position));
    const auto distant = a_station.knowledge.accept_evidence(mint(extreme), inputs, now);
    require(distant.allocations.size() == 2 && distant.allocations[1].first_index == 99 + 16383 && distant.allocations[1].block_length == 3 &&
            distant.allocations[1].reserved_through_minute == c::frame_of_index(99 + 16383) &&
            a_station.knowledge.map(c::Channel::Ais1).view(99 + 16383 + 2).state == c::SlotState::ExternalObserved,
            "extended increment announces a three-slot block seven frames ahead");
    // Foreign qualified inputs and stale decision instants.
    const auto foreign = pair.b.qualify(now);
    auto fresh = observation(a_station, c::Channel::Ais1, 15, 99, sotdma_facts(peer_b, timeout3, peer_position));
    invalid("foreign inputs", [&] { (void)a_station.knowledge.accept_evidence(mint(fresh), foreign, now); });
    invalid("stale inputs", [&] { (void)a_station.knowledge.accept_evidence(mint(fresh), inputs, at_slot(101)); });
    const auto later = a_station.qualify(at_slot(101));
    (void)a_station.knowledge.synchronize(later, at_slot(101));
    invalid("stale inputs", [&] { (void)a_station.knowledge.accept_evidence(mint(fresh), inputs, now); });  // Reversed decision time.
    const auto late = a_station.knowledge.accept_evidence(mint(fresh), later, at_slot(101));
    require(late.receipt_id == 15, "revalidated inputs at the new instant are accepted");
    // Per-peer identity facts follow the physically latest decode: a late disclosure of an older frame does not regress them.
    auto older = observation(a_station, c::Channel::Ais2, 16, 98, sotdma_facts(peer_b, timeout3, peer_at(a_station.location, 0, 5000.0)));
    older.disclosed_at = at_slot(101);
    (void)a_station.knowledge.accept_evidence(mint(older), later, at_slot(101));
    const auto* merged = a_station.knowledge.directory().find(peer_b);
    require(merged && merged->last_heard_index == 99 && merged->last_channel == c::Channel::Ais1 && merged->longitude_ais == to_ais(peer_position.longitude_ndeg) &&
            merged->last_receipt_id == 16 && merged->receipts == 8, "older physical frame never regresses the peer record");
    // S3-TIE-ORDER-v1: after a decision at an instant, evidence disclosed at or before it is late.
    (void)a_station.knowledge.draw_timeout(0, 0, later, at_slot(101));
    auto tardy = observation(a_station, c::Channel::Ais1, 17, 100, sotdma_facts(peer_b, timeout3, peer_position));
    invalid("evidence late", [&] { (void)a_station.knowledge.accept_evidence(mint(tardy), later, at_slot(101)); });
    tardy.disclosed_at = at_slot(101);
    invalid("evidence late", [&] { (void)a_station.knowledge.accept_evidence(mint(tardy), later, at_slot(101)); });
    // Evidence older than the past horizon is rejected before any mutation (directory and maps untouched).
    const auto far_now = at_slot(5 * kFrame + 100);
    const auto far_inputs = a_station.qualify(far_now);
    (void)a_station.knowledge.synchronize(far_inputs, far_now);
    const auto before_horizon = a_station.knowledge.snapshot();
    auto ancient = observation(a_station, c::Channel::Ais1, 17, 100, sotdma_facts(peer_b, timeout3, peer_position));
    ancient.disclosed_at = far_now;
    invalid("evidence horizon", [&] { (void)a_station.knowledge.accept_evidence(mint(ancient), far_inputs, far_now); });
    const auto after_horizon = a_station.knowledge.snapshot();
    require(after_horizon.peers.size() == before_horizon.peers.size() && after_horizon.external_ais1 == before_horizon.external_ais1 &&
            after_horizon.last_receipt_id == before_horizon.last_receipt_id && after_horizon.knowledge_revision == before_horizon.knowledge_revision,
            "horizon rejection leaves directory, maps and counters untouched");
    invalid("invalid fix", [&] { (void)a_station.qualify(at_slot(102), false); });
    // Invariant 6: the owning gate revalidates at the decision instant; same-instant revocation and shadow-gate tokens are rejected.
    {
        Pair bound;
        const auto t = at_slot(40);
        const auto token = bound.a.qualify(t);
        c::DirectUtcObservation lost;
        lost.own_identity = bound.a.own.identity(); lost.power_generation = 1; lost.source_id = bound.a.own.clock().source_id;
        lost.anchor = bound.a.own.clock().anchor; lost.anchor_revision = bound.a.own.clock().anchor_revision; lost.sampled_at = t; lost.valid = false;
        (void)bound.a.gate.accept_clock(lost, t);  // Revokes the clock qualification at the same instant.
        invalid("revoked inputs", [&] { (void)bound.a.knowledge.draw_timeout(0, 0, token, t); });
        c::StationInputGate twin(bound.a.own);  // A second gate built from a copy of the same configuration.
        twin.apply_power({bound.a.own.identity(), {0}, c::OwnPowerKind::On, 1});
        const auto shadow = qualify_with(twin, bound.a.own, bound.a.location, at_slot(41));
        invalid("foreign inputs", [&] { (void)bound.a.knowledge.draw_timeout(0, 0, shadow, at_slot(41)); });
        invalid("foreign inputs", [] { Pair twins; (void)c::StationKnowledge(twins.configs[0], twins.b.gate, twins.seed); });
    }
}

void asymmetry_and_isolation() {
    Pair pair;
    const auto now = at_slot(300);
    const auto inputs_a = pair.a.qualify(now);
    const auto inputs_b = pair.b.qualify(now);
    const auto peer_position = peer_at(pair.a.location, 45000000000LL, 15000.0);
    // B reserves its own slot; A never hears it.
    c::OwnReservationRequest request;
    request.channel = c::Channel::Ais1; request.first_index = 320; request.timeout_frames = 4; request.cause = "unit fixture own NTS";
    const auto own_b = pair.b.knowledge.reserve_own(request, inputs_b, now);
    require(own_b.decision_id == 1 && pair.b.knowledge.map(c::Channel::Ais1).view(320).state == c::SlotState::InternalOwn &&
            pair.a.knowledge.map(c::Channel::Ais1).view(320).state == c::SlotState::Free, "B knows its own intention; A does not");
    // A hears B's report (fixture receipt at A only), not the reverse.
    const auto state = a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 5, 1);
    auto report = observation(pair.a, c::Channel::Ais1, 1, 290, sotdma_facts(2, state, peer_position));
    (void)pair.a.knowledge.accept_evidence(mint(report), inputs_a, now);
    require(pair.a.knowledge.directory().find(2) && !pair.b.knowledge.directory().find(1) &&
            pair.a.knowledge.map(c::Channel::Ais1).view(290 + kFrame).state == c::SlotState::ExternalObserved &&
            pair.b.knowledge.map(c::Channel::Ais1).view(290 + kFrame).state == c::SlotState::Free, "asymmetric knowledge");
    const auto interval = c::selection_interval(2250 + 300, 75);
    const auto before = pair.a.knowledge.candidates(c::Channel::Ais1, interval, 1, std::nullopt, inputs_a, now);
    // B's undisclosed physical movement must not change A's map or candidates.
    pair.b.location = peer_at(pair.b.location, 180000000000LL, 50000.0);
    const auto moved_b = pair.b.qualify(at_slot(301));
    (void)pair.b.knowledge.candidates(c::Channel::Ais1, interval, 1, std::nullopt, moved_b, at_slot(301));
    const auto after = pair.a.knowledge.candidates(c::Channel::Ais1, interval, 1, std::nullopt, inputs_a, now);
    require(after.digest == before.digest && after.candidates.size() == before.candidates.size(), "peer movement does not touch A's local knowledge");
    // Both blind stations may select the same slot: A's own reservation does not remove it from B's candidates.
    c::OwnReservationRequest same; same.channel = c::Channel::Ais1; same.first_index = 2250 + 300; same.timeout_frames = 3; same.cause = "A NTS";
    (void)pair.a.knowledge.reserve_own(same, inputs_a, now);
    const auto report_b = pair.b.knowledge.candidates(c::Channel::Ais1, interval, 1, std::nullopt, moved_b, at_slot(301));
    require(std::any_of(report_b.candidates.begin(), report_b.candidates.end(), [](const c::Candidate& value) { return value.first_index == 2250 + 300; }),
            "no global grant removes A's chosen slot from B's blind candidate set");
    bool overlap = false;
    for (std::uint64_t episode = 0; episode < 400 && !overlap; ++episode)
        overlap = pair.b.knowledge.choose_candidate(report_b, episode, 0, moved_b, at_slot(301)).candidate.first_index == 2250 + 300;
    require(overlap, "independent draws can select the overlapping slot; no retroactive repair");
    // Unrelated third station and container reordering leave A's semantic draws unchanged.
    const auto reference = pair.a.knowledge.draw_timeout(11, 0, inputs_a, now);
    std::vector<c::OwnStationConfig> three = configurations(3);
    std::reverse(three.begin(), three.end());
    const auto seed = handoff();
    std::vector<std::unique_ptr<Station>> stations;
    for (const auto& config : three) { stations.push_back(std::make_unique<Station>(config, seed)); stations.back()->on({0}, 1); }
    auto& reordered_a = *stations.back();
    auto& unrelated_c = *stations.front();
    const auto c_inputs = unrelated_c.qualify(now);
    for (std::uint64_t episode = 0; episode < 5; ++episode) (void)unrelated_c.knowledge.draw_timeout(episode, 0, c_inputs, now);
    const auto replay = reordered_a.knowledge.draw_timeout(11, 0, reordered_a.qualify(now), now);
    require(replay.value == reference.value && replay.accepted_word == reference.accepted_word && replay.key == reference.key &&
            replay.proposals.size() == reference.proposals.size(), "actor order/count never enters the protocol stream");
    require(reordered_a.own.identity() == pair.a.own.identity() && unrelated_c.own.mmsi().digits() == "000000003", "fixture identities");
}

struct Scene {
    Pair pair;
    r::SimulationInstant now;
    c::QualifiedOwnInputs inputs;
    explicit Scene(std::int64_t now_slot = kFrame + 50) : now(at_slot(now_slot)), inputs(pair.a.qualify(now)) {}
    std::uint64_t receipt = 0;
    c::SelectionInterval interval = c::selection_interval(kFrame + 97, 75);  // NS 2347: [2340, 2354].
    void peer_slot(c::Channel channel, std::uint32_t mmsi, std::int64_t index_next_frame, std::optional<w::Point> position,
                   std::uint8_t timeout = 3, c::DecodedMessageKind kind = c::DecodedMessageKind::Type1) {
        const auto state = a::SotdmaState19::received_stations(a::SyncState::UtcDirect, timeout, 0);
        auto value = observation(pair.a, channel, ++receipt, index_next_frame - kFrame, sotdma_facts(mmsi, state, position, kind));
        (void)pair.a.knowledge.accept_evidence(mint(value), inputs, now);
    }
    c::CandidateReport report(std::uint8_t block = 1, std::optional<std::int64_t> current = std::nullopt) {
        return pair.a.knowledge.candidates(c::Channel::Ais1, interval, block, current, inputs, now);
    }
};

std::vector<std::int64_t> indices(const c::CandidateReport& report) {
    std::vector<std::int64_t> result;
    for (const auto& candidate : report.candidates) result.push_back(candidate.first_index);
    return result;
}
const c::ClassifiedSlot& slot_class(const c::CandidateReport& report, std::int64_t index, bool selection = true) {
    const auto& classes = selection ? report.selection_classes : report.other_classes;
    for (const auto& value : classes) if (value.index == index) return value;
    throw std::runtime_error("slot outside the selection interval");
}

void candidate_hierarchy() {
    {
        Scene scene;
        const auto empty = scene.report();
        require(empty.candidates.size() == 15 && empty.free_both_count == 15 && empty.reached_objective && empty.fallback == c::CandidateFallback::None,
                "all free on both channels: every SI slot is a candidate");
        require(empty.digest == scene.report().digest, "deterministic candidate digest");
    }
    {
        Scene scene;
        const auto near = peer_at(scene.pair.a.location, 0, 30000.0);
        const auto far = peer_at(scene.pair.a.location, 0, 60000.0);
        scene.peer_slot(c::Channel::Ais1, 2, 2341, near);
        scene.peer_slot(c::Channel::Ais1, 3, 2343, far);
        scene.peer_slot(c::Channel::Ais1, 4, 2345, std::nullopt);                                  // Positionless mobile.
        scene.peer_slot(c::Channel::Ais1, 5, 2347, near, 3, c::DecodedMessageKind::Type4);         // Base within 120 NM.
        scene.peer_slot(c::Channel::Ais2, 6, 2349, far);                                           // Other-channel reuse.
        scene.peer_slot(c::Channel::Ais2, 7, 2351, std::nullopt);                                  // Other-channel unavailable.
        const auto report = scene.report();
        require(slot_class(report, 2341).value == c::SlotClass::Available2 && slot_class(report, 2343).value == c::SlotClass::Available1 &&
                slot_class(report, 2345).value == c::SlotClass::Unavailable && slot_class(report, 2347).value == c::SlotClass::Unavailable &&
                slot_class(report, 2349, false).value == c::SlotClass::Available1 && slot_class(report, 2351, false).value == c::SlotClass::Unavailable &&
                report.most_distant_um && *report.most_distant_um > 59990000000LL, "classification: most distant, positionless, base, other channel");
        require(report.candidates.size() == 9 && report.free_both_count == 9 && report.reached_objective, "free/free slots alone satisfy the objective");
        const auto admitted = indices(report);
        for (const auto index : {2341LL, 2343LL, 2345LL, 2347LL, 2349LL, 2351LL})
            require(std::find(admitted.begin(), admitted.end(), index) == admitted.end(), "non-free slots not admitted when free/free suffice");
    }
    {
        // Only three free/free slots: Rule 1 (free on X, reusable on Y) adds the most distant other-channel peer first.
        Scene scene;
        const auto near = peer_at(scene.pair.a.location, 0, 30000.0);
        const auto far = peer_at(scene.pair.a.location, 0, 60000.0);
        for (std::int64_t index = 2340; index <= 2354; ++index) {
            if (index == 2340 || index == 2347 || index == 2354) continue;
            if (index == 2342) scene.peer_slot(c::Channel::Ais2, 20, index, near);
            else if (index == 2344) scene.peer_slot(c::Channel::Ais2, 21, index, far);
            else if (index == 2346) scene.peer_slot(c::Channel::Ais1, 22, index, far);
            else if (index == 2348) scene.peer_slot(c::Channel::Ais1, 23, index, std::nullopt);
            else scene.peer_slot(c::Channel::Ais2, 30 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        }
        const auto report = scene.report();
        require(report.free_both_count == 3 && report.candidates.size() == 4 && report.reached_objective, "hierarchy adds exactly one candidate to reach four");
        const auto added = std::find_if(report.candidates.begin(), report.candidates.end(), [](const c::Candidate& value) { return value.tier != 0; });
        require(added != report.candidates.end() && added->first_index == 2344 && added->tier == 1 && added->other_class == c::SlotClass::Available1,
                "Rule 1 prefers free/most-distant-reusable over free/other-reusable and over reusable/free");
        // Remove Rule 1 options: Rule 2 reusable/free then Rule 4 free/unavailable.
        Scene second;
        for (std::int64_t index = 2340; index <= 2354; ++index) {
            if (index == 2340 || index == 2347 || index == 2354) continue;
            if (index == 2346) second.peer_slot(c::Channel::Ais1, 22, index, far);
            else if (index == 2348) second.peer_slot(c::Channel::Ais1, 23, index, std::nullopt);
            else second.peer_slot(c::Channel::Ais2, 30 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        }
        const auto rule2 = second.report();
        require(rule2.candidates.size() == 4 && std::any_of(rule2.candidates.begin(), rule2.candidates.end(), [](const c::Candidate& value) {
            return value.first_index == 2346 && value.tier == 2 && value.reuse_peer == 22u; }), "Rule 2 reusable/free admitted before Rule 4");
        Scene fourth;
        for (std::int64_t index = 2340; index <= 2354; ++index) {
            if (index == 2340 || index == 2347 || index == 2354) continue;
            if (index == 2348) fourth.peer_slot(c::Channel::Ais1, 23, index, std::nullopt);
            else fourth.peer_slot(c::Channel::Ais2, 30 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        }
        const auto rule4 = fourth.report();
        require(rule4.candidates.size() == 4 && std::any_of(rule4.candidates.begin(), rule4.candidates.end(), [](const c::Candidate& value) {
            return value.first_index == 2341 && value.tier == 4 && value.other_class == c::SlotClass::Unavailable; }),
            "Rule 4 free/unavailable admits the lowest index when nothing better exists");
    }
    {
        // Fewer than four legal candidates and the explicit empty result with current-slot fallback.
        Scene scene;
        for (std::int64_t index = 2340; index <= 2354; ++index)
            if (index != 2342 && index != 2350) scene.peer_slot(c::Channel::Ais1, 40 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        const auto report = scene.report();
        require(report.candidates.size() == 2 && !report.reached_objective && report.shortfall_reason.find("2 legal") != std::string::npos &&
                report.fallback == c::CandidateFallback::None, "explicit shortfall, no fabricated fourth candidate");
        Scene blocked;
        for (std::int64_t index = 2340; index <= 2354; ++index) blocked.peer_slot(c::Channel::Ais1, 40 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        require(blocked.report().candidates.empty() && blocked.report().fallback == c::CandidateFallback::NoReservation, "no candidate and no usable current slot");
        c::OwnReservationRequest current; current.channel = c::Channel::Ais1; current.first_index = kFrame + 60; current.timeout_frames = 3; current.cause = "current NTS";
        (void)blocked.pair.a.knowledge.reserve_own(current, blocked.inputs, blocked.now);
        require(blocked.report(1, kFrame + 60).fallback == c::CandidateFallback::CurrentSlotUsable, "current slot fallback only when own slot is usable");
        require(blocked.report(1, kFrame + 61).fallback == c::CandidateFallback::NoReservation, "a foreign current slot is not usable");
        invalid("choice domain", [&] { auto report_empty = blocked.report(); (void)blocked.pair.a.knowledge.choose_candidate(report_empty, 0, 0, blocked.inputs, blocked.now); });
    }
    {
        // Own adjacency: own AIS2 block at j excludes j-1, j, j+1 on AIS1; j+/-2 remain; base at j+2 excludes independently.
        Scene scene;
        c::OwnReservationRequest own; own.channel = c::Channel::Ais2; own.first_index = 2347; own.timeout_frames = 3; own.cause = "own AIS2";
        (void)scene.pair.a.knowledge.reserve_own(own, scene.inputs, scene.now);
        const auto report = scene.report();
        require(slot_class(report, 2346).value == c::SlotClass::OwnAdjacentExcluded && slot_class(report, 2347).value == c::SlotClass::OwnAdjacentExcluded &&
                slot_class(report, 2348).value == c::SlotClass::OwnAdjacentExcluded && slot_class(report, 2345).value == c::SlotClass::Free1 &&
                slot_class(report, 2349).value == c::SlotClass::Free1 && report.candidates.size() == 12, "LCP-P05 three-slot exclusion");
        require(report.adjacency_exclusions.size() == 12, "retained frames extend the exclusion");
        // A protected base at j+2 excludes independently of adjacency (delivered before any decision: S3-TIE-ORDER-v1).
        Scene based;
        based.peer_slot(c::Channel::Ais1, 9, 2349, peer_at(based.pair.a.location, 0, 1000.0), 3, c::DecodedMessageKind::Type4);
        (void)based.pair.a.knowledge.reserve_own(own, based.inputs, based.now);
        const auto with_base = based.report();
        require(slot_class(with_base, 2349).value == c::SlotClass::Unavailable && slot_class(with_base, 2345).value == c::SlotClass::Free1 &&
                with_base.candidates.size() == 11, "protected base at j+2 excludes independently of adjacency");
        c::OwnReservationRequest conflict; conflict.channel = c::Channel::Ais1; conflict.first_index = 2348; conflict.cause = "adjacent";
        invalid("own transmitter", [&] { (void)scene.pair.a.knowledge.reserve_own(conflict, scene.inputs, scene.now); });
        conflict.first_index = 2347;
        invalid("own transmitter", [&] { (void)scene.pair.a.knowledge.reserve_own(conflict, scene.inputs, scene.now); });
        conflict.first_index = 2345;
        (void)scene.pair.a.knowledge.reserve_own(conflict, scene.inputs, scene.now);
        require(scene.report().candidates.size() < 12, "own AIS1 allocation leaves the candidate set");
    }
    {
        // 120 NM boundaries at block length 1, then three-slot blocks inheriting the reusable class.
        Scene scene;
        const auto within = peer_at(scene.pair.a.location, 90000000000LL, 222239.0);
        const auto beyond = peer_at(scene.pair.a.location, 90000000000LL, 222241.0);
        scene.peer_slot(c::Channel::Ais1, 50, 2344, within);
        scene.peer_slot(c::Channel::Ais1, 61, 2347, std::nullopt);
        scene.peer_slot(c::Channel::Ais1, 62, 2349, std::nullopt);
        scene.peer_slot(c::Channel::Ais1, 52, 2350, beyond, 3, c::DecodedMessageKind::Type4);
        scene.peer_slot(c::Channel::Ais1, 63, 2351, std::nullopt);
        scene.peer_slot(c::Channel::Ais1, 51, 2352, beyond);
        scene.peer_slot(c::Channel::Ais1, 64, 2353, std::nullopt);
        const auto single = scene.report();
        require(slot_class(single, 2344).value == c::SlotClass::Available1 && slot_class(single, 2352).value == c::SlotClass::Free1 &&
                slot_class(single, 2350).value == c::SlotClass::Free1 && slot_class(single, 2347).value == c::SlotClass::Unavailable &&
                single.candidates.size() == 10 && single.free_both_count == 10,
                "just inside 120 NM is reusable; just beyond is free, base included; positionless is unavailable");
        const auto report = scene.report(3);
        require(report.free_both_count == 2 && report.candidates.size() == 4 && report.reached_objective, "two free blocks plus two reusable blocks");
        const auto block = std::find_if(report.candidates.begin(), report.candidates.end(), [](const c::Candidate& value) { return value.first_index == 2342; });
        require(block != report.candidates.end() && block->selection_class == c::SlotClass::Available1 && block->reuse_peer == 50u && block->tier == 2,
                "a block containing one reusable slot inherits the reusable class");
        require(std::none_of(report.candidates.begin(), report.candidates.end(), [](const c::Candidate& value) { return value.first_index >= 2344; }),
                "blocks crossing an occupied slot are not candidates and the fifth reusable block is not admitted");
    }
    {
        // Equiprobable choice over the canonical order (all-free scene), key replay rejection and the ledger.
        Scene scene;
        const auto report = scene.report();
        std::map<std::int64_t, unsigned> counts;
        for (std::uint64_t episode = 0; episode < 450; ++episode)
            ++counts[scene.pair.a.knowledge.choose_candidate(report, episode, 0, scene.inputs, scene.now).candidate.first_index];
        require(counts.size() == 15, "every candidate is selectable");
        for (const auto& [index, count] : counts) require(count > 10 && count < 60, "comparable frequencies (30 expected per candidate)");
        const auto ledger = scene.pair.a.knowledge.ledger();
        require(ledger.size() == 450 && ledger.back().key.purpose == c::ProtocolPurpose::CandidateChoice && ledger.back().input_digest == report.digest,
                "ledger entries carry purpose and candidate digest");
        invalid("RNG key replay", [&] { (void)scene.pair.a.knowledge.choose_candidate(report, 449, 0, scene.inputs, scene.now); });
        invalid("RNG key replay", [&] { (void)scene.pair.a.knowledge.choose_candidate(report, 100, 5, scene.inputs, scene.now); });
        // One-frame reuse exclusion: choosing the reusable candidate invalidates the report and protects the peer.
        Scene reuse;
        const auto far = peer_at(reuse.pair.a.location, 0, 60000.0);
        for (std::int64_t index = 2340; index <= 2354; ++index) {
            if (index == 2348) reuse.peer_slot(c::Channel::Ais1, 90, index, far);
            else if (index != 2344 && index != 2352) reuse.peer_slot(c::Channel::Ais1, 60 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        }
        const auto three = reuse.report();
        require(three.candidates.size() == 3 && !three.reached_objective, "two free plus one reusable candidate");
        std::uint64_t episode = 0;
        while (c::choose_candidate(three, reuse.pair.a.knowledge.draw_key(c::ProtocolPurpose::CandidateChoice, episode, 0)).candidate.first_index != 2348) ++episode;
        const auto chosen = reuse.pair.a.knowledge.choose_candidate(three, episode, 0, reuse.inputs, reuse.now);
        require(chosen.candidate.first_index == 2348 && chosen.candidate.reuse_peer == 90u, "pure and stateful choices agree");
        const auto reused = reuse.pair.a.knowledge.reuse_exclusions();
        require(reused.size() == 1 && reused.front().mmsi == 90 && reused.front().excluded_until_index == kFrame + 50 + kFrame, "intentional reuse excludes the peer for one frame");
        invalid("stale report", [&] { (void)reuse.pair.a.knowledge.choose_candidate(three, episode + 1, 0, reuse.inputs, reuse.now); });
        require(slot_class(reuse.report(), 2348).value == c::SlotClass::Unavailable, "excluded peer is not reusable again this frame");
        const auto foreign_report = reuse.pair.b.knowledge.candidates(c::Channel::Ais1, reuse.interval, 1, std::nullopt, reuse.pair.b.qualify(reuse.now), reuse.now);
        invalid("stale report", [&] { (void)reuse.pair.a.knowledge.choose_candidate(foreign_report, episode + 2, 0, reuse.inputs, reuse.now); });
    }
}

void knowledge_draws_and_lifecycle() {
    Pair pair;
    auto& station = pair.a;
    const auto now = at_slot(10);
    const auto inputs = station.qualify(now);
    // Evidence first: under S3-TIE-ORDER-v1 evidence disclosed at or before a decision instant is late afterwards.
    const auto peer = peer_at(station.location, 0, 10000.0);
    auto report = observation(station, c::Channel::Ais1, 1, 5, sotdma_facts(2, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 7, 0), peer));
    (void)station.knowledge.accept_evidence(mint(report), inputs, now);
    std::set<std::int64_t> timeouts;
    for (std::uint64_t episode = 0; episode < 60; ++episode) {
        const auto value = station.knowledge.draw_timeout(episode, 0, inputs, now).value;
        require(value >= 3 && value <= 7, "timeout draw domain");
        timeouts.insert(value);
    }
    require(timeouts.size() == 5, "all five timeout values occur");
    for (std::uint64_t episode = 0; episode < 20; ++episode) {
        const auto value = station.knowledge.draw_ratdma_probability(episode, 0, inputs, now).value;
        require(value >= 0 && value <= 100, "RATDMA draw domain");
    }
    const auto entry = station.knowledge.draw_entry_offset(0, 375, 3, 0, inputs, now);
    require(entry.value >= 0 && entry.value <= 375 && entry.key.purpose == c::ProtocolPurpose::EntryNominalStartSlot, "entry offset draw only");
    invalid("entry domain", [&] { (void)station.knowledge.draw_entry_offset(5, 4, 4, 0, inputs, now); });
    invalid("entry domain", [&] { (void)station.knowledge.draw_entry_offset(-1, 4, 4, 0, inputs, now); });
    const auto reuse = station.knowledge.choose_reuse_peer({5, 9, 12}, 1, 0, inputs, now);
    require(reuse.value >= 0 && reuse.value <= 2 && reuse.outcome.rfind("intentional_reuse_peer:", 0) == 0, "reuse peer draw");
    invalid("reuse domain", [&] { (void)station.knowledge.choose_reuse_peer({9, 5}, 1, 0, inputs, now); });
    invalid("reuse domain", [&] { (void)station.knowledge.choose_reuse_peer({}, 1, 0, inputs, now); });
    const auto key = station.knowledge.draw_key(c::ProtocolPurpose::ContinuousTimeout, 4, 2);
    require(key.scenario_seed == 42 && key.equipment_id == 21 && key.power_generation == 1 && key.episode == 4 && key.draw_index == 2, "draw key coordinates");
    require(station.knowledge.ledger().size() == 82 && station.knowledge.ledger_issued() == 82 && station.knowledge.ledger_drained() == 0 &&
            !station.knowledge.ready_for_tx(), "bounded ledger, never TX ready");
    // Draw keys strictly increase per purpose within a generation; replay is rejected before any draw or ledger write.
    invalid("RNG key replay", [&] { (void)station.knowledge.draw_timeout(59, 0, inputs, now); });
    invalid("RNG key replay", [&] { (void)station.knowledge.draw_ratdma_probability(3, 0, inputs, now); });
    require(station.knowledge.draw_timeout(59, 1, inputs, now).key.draw_index == 1 && station.knowledge.ledger_issued() == 83,
            "the draw index advances within an episode");
    require(station.knowledge.snapshot().draw_watermarks.size() == 4, "every drawn purpose carries its watermark");
    // Bounded ledger: an explicit rejection when full; draining exports in order and the issued sequence continues.
    Station small(pair.configs[0], pair.seed, c::KnowledgeLimits{{}, 1024, 16});
    small.on({0}, 1);
    const auto small_inputs = small.qualify(now);
    for (std::uint64_t episode = 0; episode < 16; ++episode) (void)small.knowledge.draw_timeout(episode, 0, small_inputs, now);
    invalid("ledger full", [&] { (void)small.knowledge.draw_timeout(16, 0, small_inputs, now); });
    const auto drained = small.knowledge.drain_ledger();
    require(drained.size() == 16 && drained.front().key.episode == 0 && drained.back().key.episode == 15 && small.knowledge.ledger().empty() &&
            small.knowledge.ledger_drained() == 16, "draining exports every entry in order");
    require(small.knowledge.draw_timeout(16, 0, small_inputs, now).key.episode == 16 && small.knowledge.ledger_issued() == 17,
            "draws continue after draining; nothing was silently dropped");
    invalid("knowledge limits", [&] { (void)Station(pair.configs[0], pair.seed, c::KnowledgeLimits{{}, 1024, 8}); });
    // Power epochs: OFF clears, re-ON uses a fresh key domain, stale tokens fail.
    c::OwnReservationRequest own; own.channel = c::Channel::Ais1; own.first_index = 40; own.timeout_frames = 6; own.cause = "pre-OFF";
    (void)station.knowledge.reserve_own(own, inputs, now);
    const auto before_off = station.knowledge.draw_timeout(99, 0, inputs, now);
    station.off(at_slot(20), 1);
    const auto cleared = station.knowledge.snapshot();
    require(!cleared.equipment_on && cleared.peers.empty() && cleared.own_ais1.empty() && cleared.external_ais1 == 0 && cleared.last_receipt_id == 0 &&
            cleared.draw_watermarks.empty() && cleared.ledger_entries == 84, "OFF clears live knowledge, intentions and watermarks; historical ledger stays inspectable");
    invalid("OFF decision", [&] { (void)station.knowledge.draw_timeout(100, 0, inputs, at_slot(20)); });
    invalid("OFF decision", [&] { (void)station.knowledge.candidates(c::Channel::Ais1, c::selection_interval(100, 75), 1, std::nullopt, inputs, at_slot(20)); });
    invalid("lifecycle", [&] { station.knowledge.apply_power({station.own.identity(), at_slot(20), c::OwnPowerKind::On, 2}); });  // Same-instant OFF wins.
    station.on(at_slot(30), 2);
    invalid("stale inputs", [&] { (void)station.knowledge.draw_timeout(99, 0, inputs, at_slot(30)); });
    const auto fresh = station.qualify(at_slot(30));
    const auto after_on = station.knowledge.draw_timeout(99, 0, fresh, at_slot(30));
    require(after_on.key.power_generation == 2 && after_on.accepted_word != before_off.accepted_word, "new generation keys a fresh stream");
    auto stale = observation(station, c::Channel::Ais1, 1, 25, sotdma_facts(2, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 7, 0), peer));
    stale.power_generation = 1;
    invalid("evidence epoch", [&] { (void)station.knowledge.accept_evidence(mint(stale), fresh, at_slot(30)); });
    auto pre_epoch = observation(station, c::Channel::Ais1, 1, 25, sotdma_facts(2, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 7, 0), peer));
    invalid("evidence epoch", [&] { (void)station.knowledge.accept_evidence(mint(pre_epoch), fresh, at_slot(30)); });  // Received before ON.
    require(station.knowledge.map(c::Channel::Ais1).view(40).state == c::SlotState::Free, "pre-OFF reservation does not survive");
    invalid("lifecycle", [&] { station.knowledge.apply_power({station.own.identity(), at_slot(31), c::OwnPowerKind::On, 3}); });
    invalid("lifecycle", [&] { station.knowledge.apply_power({station.own.identity(), at_slot(29), c::OwnPowerKind::Off, 2}); });
    invalid("lifecycle", [&] { station.knowledge.apply_power({station.own.identity(), at_slot(31), c::OwnPowerKind::Off, 1}); });
    invalid("lifecycle", [&] { station.knowledge.apply_power({station.own.identity(), at_slot(31), c::OwnPowerKind::InitialOff, 0}); });
    invalid("lifecycle", [&] { station.knowledge.apply_power({pair.b.own.identity(), at_slot(31), c::OwnPowerKind::Off, 2}); });
    {
        c::StationInputGate gate(pair.configs[1]);
        c::StationKnowledge bootstrap(pair.configs[1], gate, pair.seed);
        bootstrap.apply_power({pair.configs[1].identity(), {0}, c::OwnPowerKind::InitialOff, 0});
        require(!bootstrap.equipment_on() && bootstrap.power_generation() == 0, "InitialOff is bootstrap, not an OFF");
        invalid("lifecycle", [&] { bootstrap.apply_power({pair.configs[1].identity(), {0}, c::OwnPowerKind::On, 0x1000000}); });
        bootstrap.apply_power({pair.configs[1].identity(), {0}, c::OwnPowerKind::On, 1});
        require(bootstrap.equipment_on(), "ON after bootstrap");
    }
    {
        // Seed handoff must match the scenario; foreign handoff rejected.
        auto other = synthetic_world(); other.profile_id = "OTHER-SCENARIO";
        const auto foreign = c::project_scenario_seed(other, kWorldRevision);
        invalid("seed handoff", [&] { (void)c::StationKnowledge(pair.configs[0], pair.a.gate, foreign); });
        invalid("seed handoff", [&] { (void)c::StationKnowledge(pair.configs[0], pair.a.gate, c::project_scenario_seed(synthetic_world(), 18)); });
        invalid("seed handoff", [] { (void)c::project_scenario_seed(synthetic_world(), 0); });
        invalid("seed handoff", [] { auto fresh_world = synthetic_world(); fresh_world.fresh_seed = true; (void)c::project_scenario_seed(fresh_world, 5); });
        require(pair.seed.effective_seed() == 42 && pair.seed.scenario_id() == "SYNTHETIC-A1-S3-WORLD-v1", "handoff values");
    }
}

void review_regressions() {
    {
        // A late next-frame-offset announcement never replaces the physically newer use of the same slot (equal first index).
        Pair pair;
        const auto now = at_slot(101 * kFrame + 700);
        const auto inputs = pair.a.qualify(now);
        const auto position = peer_at(pair.a.location, 0, 20000.0);
        auto use = observation(pair.a, c::Channel::Ais1, 1, 101 * kFrame + 600, sotdma_facts(2, a::SotdmaState19::current_slot(a::SyncState::UtcDirect, 6, 600), position));
        const auto first = pair.a.knowledge.accept_evidence(mint(use), inputs, now);
        require(first.allocations.size() == 1 && first.allocations.front().reserved_through_minute == 107, "use with timeout 6 reserves through frame 107");
        auto late = observation(pair.a, c::Channel::Ais1, 2, 100 * kFrame + 10, sotdma_facts(2, a::SotdmaState19::offset_in_next_frame(a::SyncState::UtcDirect, 10, 600), position));
        late.disclosed_at = now;
        const auto announced = pair.a.knowledge.accept_evidence(mint(late), inputs, now);
        require(announced.allocations.size() == 1 && announced.allocations.front().basis == c::AllocationBasis::ObservedUse &&
                announced.superseded.size() == 1 && announced.superseded.front().basis == c::AllocationBasis::SotdmaNextFrameOffset,
                "the older offset announcement is reported superseded");
        require(pair.a.knowledge.map(c::Channel::Ais1).view(103 * kFrame + 600).state == c::SlotState::ExternalObserved &&
                pair.a.knowledge.map(c::Channel::Ais1).external_count() == 2, "the newer timeout record survives in the map");
        const auto* record = pair.a.knowledge.directory().find(2);
        require(record && std::any_of(record->reservations.begin(), record->reservations.end(), [](const c::PeerAnnouncedReservation& value) {
            return value.basis == c::AllocationBasis::SotdmaTimeout && value.reserved_through_minute == 107; }), "and in the directory");
    }
    {
        // An older ITDMA announcement of the same block never shrinks it.
        Pair pair;
        const auto now = at_slot(300);
        const auto inputs = pair.a.qualify(now);
        const auto position = peer_at(pair.a.location, 0, 20000.0);
        auto newer = observation(pair.a, c::Channel::Ais1, 1, 200, itdma_facts(6, a::ItdmaState19::from_effective(a::SyncState::UtcDirect, 1000, 5, false), position));
        (void)pair.a.knowledge.accept_evidence(mint(newer), inputs, now);
        auto older = observation(pair.a, c::Channel::Ais1, 2, 100, itdma_facts(6, a::ItdmaState19::from_effective(a::SyncState::UtcDirect, 1100, 1, false), position));
        older.disclosed_at = now;
        const auto result = pair.a.knowledge.accept_evidence(mint(older), inputs, now);
        require(result.allocations.size() == 1 && result.superseded.size() == 1 && result.superseded.front().block_length == 1 &&
                pair.a.knowledge.map(c::Channel::Ais1).view(1204).state == c::SlotState::ExternalObserved, "superseded ITDMA announcement");
    }
    {
        // The refresh scan covers the whole retained span even when the future horizon is shorter than the past one.
        c::KnowledgeLimits limits; limits.map.future_frames = 4;
        Pair pair;
        Station station(pair.configs[0], pair.seed, limits);
        station.on({0}, 1);
        const auto now = at_slot(100 * kFrame + 500);
        const auto inputs = station.qualify(now);
        const auto position = peer_at(station.location, 0, 20000.0);
        auto offset = observation(station, c::Channel::Ais1, 1, 100 * kFrame + 10, sotdma_facts(2, a::SotdmaState19::offset_in_next_frame(a::SyncState::UtcDirect, 10, 600), position));
        const auto first = station.knowledge.accept_evidence(mint(offset), inputs, now);
        require(first.allocations.size() == 2 && first.allocations[1].first_index == 101 * kFrame + 600, "offset to next-frame slot 600");
        auto stale = observation(station, c::Channel::Ais1, 2, 96 * kFrame + 600, sotdma_facts(2, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 7, 3), position));
        stale.disclosed_at = now;
        const auto late = station.knowledge.accept_evidence(mint(stale), inputs, now);
        require(late.allocations.empty() && late.superseded.size() == 1 && station.knowledge.map(c::Channel::Ais1).external_count() == 2 &&
                station.knowledge.directory().find(2)->reservations.size() == 2, "a late older announcement five frames behind is superseded, not duplicated");
        const auto report = station.knowledge.candidates(c::Channel::Ais1, c::selection_interval(102 * kFrame + 600, 75), 1, std::nullopt, inputs, now);
        require(slot_class(report, 102 * kFrame + 600).value == c::SlotClass::Free1, "no phantom aged record");
    }
    {
        // A one-shot block straddling the frame boundary keeps its peer record until its last slot has passed.
        Pair pair;
        const auto t0 = at_slot(1700);
        const auto inputs0 = pair.a.qualify(t0);
        const auto position = peer_at(pair.a.location, 0, 20000.0);
        auto block = observation(pair.a, c::Channel::Ais1, 1, 1616, itdma_facts(5, a::ItdmaState19::from_effective(a::SyncState::UtcDirect, 16383, 3, false), position));
        const auto accepted = pair.a.knowledge.accept_evidence(mint(block), inputs0, t0);
        require(accepted.allocations.size() == 2 && accepted.allocations[1].first_index == 17999, "block 17999..18001 straddles frames 7/8");
        const auto t1 = at_slot(18000);
        const auto inputs1 = pair.a.qualify(t1);
        const auto report = pair.a.knowledge.candidates(c::Channel::Ais1, c::selection_interval(18008, 75), 1, std::nullopt, inputs1, t1);
        require(pair.a.knowledge.directory().find(5) && slot_class(report, 18001).value == c::SlotClass::Available1, "peer retained while its block is live");
        const auto t2 = at_slot(18002);
        const auto inputs2 = pair.a.qualify(t2);
        (void)pair.a.knowledge.synchronize(inputs2, t2);
        require(!pair.a.knowledge.directory().find(5), "and expired right after");
    }
    {
        // Aged SOTDMA evidence stays Free2 when the only live holder is beyond 120 NM.
        Pair pair;
        const auto t0 = at_slot(150);
        const auto inputs0 = pair.a.qualify(t0);
        auto aged = observation(pair.a, c::Channel::Ais1, 1, 100, sotdma_facts(80, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 7, 1), peer_at(pair.a.location, 0, 30000.0)));
        (void)pair.a.knowledge.accept_evidence(mint(aged), inputs0, t0);
        const auto t3 = at_slot(3 * kFrame + 150);
        const auto inputs3 = pair.a.qualify(t3);
        auto far = observation(pair.a, c::Channel::Ais1, 2, 3 * kFrame + 100, sotdma_facts(82, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 3, 1), peer_at(pair.a.location, 0, 300000.0)));
        (void)pair.a.knowledge.accept_evidence(mint(far), inputs3, t3);
        const auto t4 = at_slot(4 * kFrame + 50);
        const auto inputs4 = pair.a.qualify(t4);
        const auto report = pair.a.knowledge.candidates(c::Channel::Ais1, c::selection_interval(4 * kFrame + 100, 75), 1, std::nullopt, inputs4, t4);
        require(slot_class(report, 4 * kFrame + 100).value == c::SlotClass::Free2, "aged evidence is kept when live holders are beyond 120 NM");
    }
    {
        // The most-distant pool excludes slots that can never be chosen; Rule 1 then admits the more distant other-channel station.
        Scene scene(kFrame + 95);  // Decision slot 2345 inside the SI [2340, 2354].
        scene.peer_slot(c::Channel::Ais1, 22, 2341, peer_at(scene.pair.a.location, 0, 110000.0));  // Past slot.
        scene.peer_slot(c::Channel::Ais2, 20, 2348, peer_at(scene.pair.a.location, 0, 20000.0));
        scene.peer_slot(c::Channel::Ais2, 21, 2350, peer_at(scene.pair.a.location, 0, 100000.0));
        for (const std::int64_t index : {2351, 2352, 2353, 2354}) scene.peer_slot(c::Channel::Ais1, 60 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        const auto report = scene.report();
        require(indices(report) == std::vector<std::int64_t>{2346, 2347, 2349, 2350} && report.free_both_count == 3,
                "Rule 1 admits the slot of the more distant other-channel station");
        require(slot_class(report, 2341).value == c::SlotClass::Available2 && slot_class(report, 2350, false).value == c::SlotClass::Available1 &&
                slot_class(report, 2348, false).value == c::SlotClass::Available2 && report.most_distant_um &&
                *report.most_distant_um > 99990000000LL && *report.most_distant_um < 100010000000LL, "the most-distant pool excludes unusable slots");
        require(std::none_of(report.candidates.begin(), report.candidates.end(), [](const c::Candidate& value) { return value.first_index <= 2345; }), "future-only candidates");
        Scene plain(kFrame + 95);
        require(plain.report().candidates.size() == 9 && plain.report().candidates.front().first_index == 2346 && plain.report(3).candidates.size() == 7,
                "slots at or before the decision slot are never candidates");
    }
    {
        // A two-slot block reusing two stations names both; choosing it excludes both for one frame.
        Scene scene;
        scene.peer_slot(c::Channel::Ais1, 50, 2341, peer_at(scene.pair.a.location, 0, 30000.0));
        scene.peer_slot(c::Channel::Ais1, 51, 2342, peer_at(scene.pair.a.location, 0, 60000.0));
        for (std::int64_t index = 2343; index <= 2354; ++index)
            if (index != 2350 && index != 2351) scene.peer_slot(c::Channel::Ais1, 60 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        const auto report = scene.report(2);
        require(indices(report) == std::vector<std::int64_t>{2340, 2341, 2350}, "two reusable two-slot blocks plus one free block");
        const auto block = std::find_if(report.candidates.begin(), report.candidates.end(), [](const c::Candidate& value) { return value.first_index == 2341; });
        require(block != report.candidates.end() && block->reuse_peers == std::vector<std::uint32_t>{50, 51} && block->reuse_peer == 50u, "a block reusing two stations names both");
        std::uint64_t episode = 0;
        while (c::choose_candidate(report, scene.pair.a.knowledge.draw_key(c::ProtocolPurpose::CandidateChoice, episode, 0)).candidate.first_index != 2341) ++episode;
        (void)scene.pair.a.knowledge.choose_candidate(report, episode, 0, scene.inputs, scene.now);
        const auto excluded = scene.pair.a.knowledge.reuse_exclusions();
        require(excluded.size() == 2 && excluded[0].mmsi == 50 && excluded[1].mmsi == 51, "both reused stations are excluded for one frame");
        const auto after = scene.report(2);
        require(slot_class(after, 2341).value == c::SlotClass::Unavailable && slot_class(after, 2342).value == c::SlotClass::Unavailable, "neither is reusable again this frame");
    }
    {
        // A no-op release is not a decision; a real release is.
        Pair pair;
        const auto now = at_slot(100);
        const auto inputs = pair.a.qualify(now);
        const auto position = peer_at(pair.a.location, 0, 20000.0);
        require(!pair.a.knowledge.release_own(c::Channel::Ais1, 999, inputs, now), "nothing to release");
        auto evidence = observation(pair.a, c::Channel::Ais1, 1, 98, sotdma_facts(2, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 3, 1), position));
        evidence.disclosed_at = now;
        require(pair.a.knowledge.accept_evidence(mint(evidence), inputs, now).peer == 2u, "same-instant evidence still accepted");
        c::OwnReservationRequest own; own.channel = c::Channel::Ais1; own.first_index = 120; own.cause = "release test";
        (void)pair.a.knowledge.reserve_own(own, inputs, now);
        require(pair.a.knowledge.release_own(c::Channel::Ais1, 120, inputs, now), "released");
        auto late = observation(pair.a, c::Channel::Ais1, 2, 99, sotdma_facts(2, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 3, 1), position));
        late.disclosed_at = now;
        invalid("evidence late", [&] { (void)pair.a.knowledge.accept_evidence(mint(late), inputs, now); });
    }
    {
        // Message 20 and an undecodable communication state never erase earlier identity facts.
        Pair pair;
        const auto now = at_slot(100);
        const auto inputs = pair.a.qualify(now);
        const auto position = peer_at(pair.a.location, 90000000000LL, 222241.0);
        auto type4 = observation(pair.a, c::Channel::Ais1, 1, 95, sotdma_facts(8, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 3, 1), position, c::DecodedMessageKind::Type4));
        (void)pair.a.knowledge.accept_evidence(mint(type4), inputs, now);
        auto link = observation(pair.a, c::Channel::Ais1, 2, 96, sotdma_facts(8, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 3, 1), std::nullopt, c::DecodedMessageKind::Type20));
        link.decoded->raw_communication_state = -1;
        (void)pair.a.knowledge.accept_evidence(mint(link), inputs, now);
        const auto* base = pair.a.knowledge.directory().find(8);
        require(base && base->position_available && base->sync_state == a::SyncState::UtcDirect && base->last_heard_index == 96 &&
                base->last_message == c::DecodedMessageKind::Type20, "Message 20 updates receipt facts but keeps position and synchronization");
        auto synced = observation(pair.a, c::Channel::Ais1, 3, 97, sotdma_facts(2, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 3, 1), position));
        (void)pair.a.knowledge.accept_evidence(mint(synced), inputs, now);
        auto broken = observation(pair.a, c::Channel::Ais1, 4, 98, sotdma_facts(2, a::SotdmaState19::received_stations(a::SyncState::UtcDirect, 3, 1), position));
        broken.decoded->raw_communication_state = 0x4001;
        (void)pair.a.knowledge.accept_evidence(mint(broken), inputs, now);
        require(pair.a.knowledge.directory().find(2)->sync_state == a::SyncState::UtcDirect, "an undecodable state keeps the last decoded synchronization");
    }
    {
        // A report built from own inputs the gate has since superseded is not consumable.
        Pair pair;
        const auto now = at_slot(2300);
        (void)pair.a.qualify(r::add_units(now, -30 * r::kUnitsPerSecond));  // Acquire the clock before the older fix.
        const auto older = qualify_with(pair.a.gate, pair.a.own, pair.a.location, now, true, r::add_units(now, -30 * r::kUnitsPerSecond));
        const auto report = pair.a.knowledge.candidates(c::Channel::Ais1, c::selection_interval(2347, 75), 1, std::nullopt, older, now);
        const auto newer = pair.a.qualify(now);
        invalid("stale report", [&] { (void)pair.a.knowledge.choose_candidate(report, 0, 0, newer, now); });
        invalid("stale inputs", [&] { (void)pair.a.knowledge.choose_candidate(report, 0, 0, older, now); });
        const auto fresh = pair.a.knowledge.candidates(c::Channel::Ais1, c::selection_interval(2347, 75), 1, std::nullopt, newer, now);
        require(pair.a.knowledge.choose_candidate(fresh, 0, 0, newer, now).chosen, "a report built from the current inputs is consumable");
    }
    {
        // LCP-P03 replenishment never reaches back before the creation slot.
        auto state = c::start_generic_ratdma(100, 250, {110, 120, 130, 140});
        (void)c::remove_peer_candidate(state, 110);
        invalid("RATDMA domain", [&] { (void)c::replenish_candidates(state, 50, {60, 100}); });
        require(c::replenish_candidates(state, 100, {60, 100, 115}).added == 1 && state.candidates.front() == 115, "replenishment stays after the creation slot");
    }
}

// Table 18 pairs each slot timeout with one submessage kind; timeout 0 announces the same slot next frame here.
a::SotdmaState19 sotdma_for_timeout(std::int64_t timeout, std::int64_t frame_slot) {
    switch (timeout) {
    case 0: return a::SotdmaState19::offset(a::SyncState::UtcDirect, kFrame);
    case 1: return a::SotdmaState19::utc(a::SyncState::UtcDirect, 12, 30);
    case 2: case 4: case 6: return a::SotdmaState19::current_slot(a::SyncState::UtcDirect, timeout, frame_slot);
    default: return a::SotdmaState19::received_stations(a::SyncState::UtcDirect, timeout, 3);
    }
}

void fast_peer_refresh() {
    // One peer at the fastest Table 1 rate (2 s: 15 slots per channel per frame) re-announcing for ten frames
    // must stay accepted with bounded map and directory records (in-place refresh per frame slot).
    Pair pair;
    auto& station = pair.a;
    const auto position = peer_at(station.location, 0, 20000.0);
    std::uint64_t receipt = 0;
    std::size_t max_external = 0, max_reservations = 0;
    for (std::int64_t frame = 0; frame < 10; ++frame) {
        const auto now = at_slot(frame * kFrame + 2200);
        const auto inputs = station.qualify(now);
        for (std::int64_t slot = 0; slot < 30; ++slot) {
            const auto channel = slot % 2 == 0 ? c::Channel::Ais1 : c::Channel::Ais2;
            const auto index = frame * kFrame + 10 + slot * 70;
            const auto countdown = static_cast<std::int64_t>(7 - (frame + slot) % 8);  // 7..0 cycling per slot.
            const auto state = sotdma_for_timeout(countdown, c::frame_slot(index));
            auto value = observation(station, channel, ++receipt, index, sotdma_facts(2, state, position));
            const auto accepted = station.knowledge.accept_evidence(mint(value), inputs, now);
            require(accepted.status == "decoded_peer_evidence_accepted", "fast peer evidence accepted every frame");
        }
        max_external = std::max(max_external, station.knowledge.map(c::Channel::Ais1).external_count() + station.knowledge.map(c::Channel::Ais2).external_count());
        max_reservations = std::max(max_reservations, station.knowledge.directory().find(2)->reservations.size());
    }
    require(receipt == 300 && max_external <= 60 && max_reservations <= 60 && station.knowledge.directory().size() == 1,
            "ten frames of 30 announcements per frame stay within two records per slot lineage");
}

// ---------------------------------------------------------------------------
std::string class_token(const c::ClassifiedSlot& value) {
    switch (value.value) {
    case c::SlotClass::Free1: return "F1";
    case c::SlotClass::Free2: return "F2";
    case c::SlotClass::Available1: return "A1@" + std::to_string(value.peer_distance_um.value_or(0));
    case c::SlotClass::Available2: return "A2@" + std::to_string(value.peer_distance_um.value_or(0));
    case c::SlotClass::Unavailable: return "U";
    case c::SlotClass::OwnAllocated: return "O";
    case c::SlotClass::OwnAdjacentExcluded: return "X";
    }
    return "?";
}
std::string short_class(c::SlotClass value) {
    switch (value) {
    case c::SlotClass::Free1: return "F1";
    case c::SlotClass::Free2: return "F2";
    case c::SlotClass::Available1: return "A1";
    case c::SlotClass::Available2: return "A2";
    default: return "U";
    }
}
void emit_draw(const c::DrawLedgerEntry& entry) {
    std::cout << "DRAW|" << entry.key.scenario_seed << '|' << entry.key.equipment_id << '|' << entry.key.power_generation << '|'
              << static_cast<unsigned>(entry.key.purpose) << '|' << entry.key.episode << '|' << entry.key.draw_index << '|'
              << entry.minimum << '|' << entry.maximum << '|' << entry.span << '|' << entry.acceptance_limit << '|' << entry.rejections << '|'
              << entry.accepted_word << '|' << entry.value << '|';
    for (std::size_t index = 0; index < entry.proposals.size(); ++index) {
        const auto& proposal = entry.proposals[index];
        if (index) std::cout << ';';
        std::cout << proposal.rejection_index << ':' << proposal.words[0] << ',' << proposal.words[1] << ',' << proposal.words[2] << ',' << proposal.words[3];
    }
    std::cout << '\n';
}
void emit_candidate_row(const std::string& name, const c::CandidateReport& report, bool current_usable) {
    std::cout << "CAND|" << name << '|' << c::channel_name(report.selection_channel) << '|' << static_cast<unsigned>(report.block_length) << '|'
              << report.interval.ns_index << '|' << report.interval.ni << '|' << report.now_index << '|' << (current_usable ? 1 : 0) << '|';
    for (std::size_t index = 0; index < report.selection_classes.size(); ++index) std::cout << (index ? "," : "") << class_token(report.selection_classes[index]);
    std::cout << '|';
    for (std::size_t index = 0; index < report.other_classes.size(); ++index) std::cout << (index ? "," : "") << class_token(report.other_classes[index]);
    std::cout << '|';
    for (std::size_t index = 0; index < report.candidates.size(); ++index) {
        const auto& candidate = report.candidates[index];
        std::string peers;
        for (const auto peer : candidate.reuse_peers) peers += (peers.empty() ? "" : "+") + std::to_string(peer);
        std::cout << (index ? ";" : "") << candidate.first_index << ':' << static_cast<unsigned>(candidate.tier) << ':' << short_class(candidate.selection_class)
                  << ':' << short_class(candidate.other_class) << ':' << (peers.empty() ? std::string("-") : peers);
    }
    std::cout << '|' << report.free_both_count << '|' << (report.reached_objective ? 1 : 0) << '|' << c::fallback_name(report.fallback) << '\n';
}

void emit_vectors() {
    std::cout << "A1-S3|SYNTHETIC_KNOWLEDGE_FIXTURE|1\n";
    const std::vector<std::pair<c::PhiloxBlock, c::PhiloxKey>> kats = {
        {{{0, 0, 0, 0}}, {{0, 0}}}, {{{UINT32_MAX, UINT32_MAX, UINT32_MAX, UINT32_MAX}}, {{UINT32_MAX, UINT32_MAX}}},
        {{{0x243F6A88, 0x85A308D3, 0x13198A2E, 0x03707344}}, {{0xA4093822, 0x299F31D0}}},
        {{{21, 5, 0x66000001, 0x01000007}}, {{0x89ABCDEF, 0x01234567}}}, {{{1, 2, 3, 4}}, {{5, 6}}}};
    for (const auto& [counter, key] : kats) {
        const auto out = c::philox4x32_10(counter, key);
        std::cout << "PHILOX";
        for (const auto word : counter) std::cout << '|' << hex_word(word);
        for (const auto word : key) std::cout << '|' << hex_word(word);
        for (const auto word : out) std::cout << '|' << hex_word(word);
        std::cout << '\n';
    }
    const std::vector<c::DrawKey> keys = [] {
        std::vector<c::DrawKey> result;
        for (const auto purpose : {c::ProtocolPurpose::EntryNominalStartSlot, c::ProtocolPurpose::CandidateChoice, c::ProtocolPurpose::ContinuousTimeout,
                                   c::ProtocolPurpose::GenericRatdmaProbability, c::ProtocolPurpose::IntentionalReusePeer}) {
            for (const auto [seed, equipment, generation, episode, draw] : {
                     std::tuple{42ULL, 21u, 1ULL, 0ULL, 0ULL}, std::tuple{0x0123456789ABCDEFULL, 21u, 1ULL, 5ULL, 7ULL},
                     std::tuple{UINT64_MAX, UINT32_MAX, 0xFFFFFFULL, 0xFFFFFFFFULL, 0xFFFFULL}, std::tuple{7ULL, 22u, 2ULL, 3ULL, 1ULL}}) {
                c::DrawKey key; key.scenario_seed = seed; key.equipment_id = equipment; key.power_generation = generation;
                key.purpose = purpose; key.episode = episode; key.draw_index = draw; result.push_back(key);
            }
        }
        return result;
    }();
    for (const auto& key : keys) {
        for (const std::uint32_t rejection : {0u, 1u, 255u}) {
            const auto position = c::pack_draw(key, rejection);
            const auto bytes = c::serialize_draw_position(position);
            std::cout << "PACK|" << key.scenario_seed << '|' << key.equipment_id << '|' << key.power_generation << '|' << static_cast<unsigned>(key.purpose)
                      << '|' << key.episode << '|' << key.draw_index << '|' << rejection << '|' << hex_word(position.key[0]) << '|' << hex_word(position.key[1]);
            for (const auto word : position.counter) std::cout << '|' << hex_word(word);
            std::cout << '|';
            for (const auto byte : bytes) std::cout << std::hex << std::setw(2) << std::setfill('0') << static_cast<unsigned>(byte);
            std::cout << std::dec << '\n';
        }
        for (const auto [minimum, maximum] : {std::pair<std::int64_t, std::int64_t>{3, 7}, {0, 100}, {0, 374}, {-5, 5}, {0, 3221225471LL}, {42, 42},
                                              {INT64_MIN, INT64_MIN + 10}, {0, 4294967295LL}})
            emit_draw(c::bounded_draw(key, minimum, maximum));
    }
    for (const auto [ns, ni] : {std::pair<std::int64_t, std::int64_t>{1000, 375}, {1000, 225}, {1000, 125}, {1000, 75}, {-3, 75}, {2249, 375}, {0, 125}, {5000000, 225}}) {
        const auto interval = c::selection_interval(ns, ni);
        std::cout << "SI|" << ns << '|' << ni << "|1|" << interval.half_width << '|' << interval.first_index << '|' << interval.last_index << '|' << interval.width << '\n';
    }
    {
        const auto reduced = c::selection_interval(500, 750, 10);  // 750/10 reduces to the qualified NI 75.
        std::cout << "SI|500|750|10|" << reduced.half_width << '|' << reduced.first_index << '|' << reduced.last_index << '|' << reduced.width << '\n';
    }
    for (const auto [numerator, denominator] : {std::pair<std::int64_t, std::int64_t>{75, 2}, {100, 1}, {1125, 1}, {0, 1}, {1225, 1}}) {
        try { (void)c::selection_interval(10, numerator, denominator); throw std::runtime_error("unexpected accepted NI"); }
        catch (const std::invalid_argument&) { std::cout << "SIREJECT|" << numerator << '|' << denominator << '\n'; }
    }
    for (const auto [first, block, frames] : {std::tuple<std::int64_t, int, std::int64_t>{0, 1, 0}, {2249, 1, 0}, {100, 3, 1}, {-1, 2, 0}, {2250, 5, 2}, {7, 1, 7}}) {
        const auto excluded = c::own_adjacency_exclusions({{first, static_cast<std::uint8_t>(block), frames}});
        std::cout << "ADJ|" << first << '|' << block << '|' << frames << '|';
        for (std::size_t index = 0; index < excluded.size(); ++index) std::cout << (index ? "," : "") << excluded[index];
        std::cout << '\n';
    }
    auto emit_ratdma = [](const c::RatdmaBookkeeping& state, std::uint32_t u) {
        auto probe = state;
        const auto result = c::ratdma_attempt(probe, u);
        std::cout << "RATDMA|" << c::candidate_count(state) << '|' << state.attempts << '|' << state.probability.numerator << '|' << state.probability.denominator
                  << '|' << u << '|' << (result.outcome == c::RatdmaAttemptOutcome::WouldRequest ? "would_request" : "inhibited") << '|'
                  << result.probability_after.numerator << '|' << result.probability_after.denominator << '|' << result.candidates_after << '|'
                  << result.attempts_after << '\n';
    };
    const auto initial = c::start_generic_ratdma(0, 150, {10, 40, 80, 120});
    for (std::uint32_t u = 0; u <= 100; ++u) emit_ratdma(initial, u);
    auto inhibited = initial; (void)c::ratdma_attempt(inhibited, 26);
    emit_ratdma(inhibited, 43); emit_ratdma(inhibited, 44);
    auto twice = inhibited; (void)c::ratdma_attempt(twice, 44);
    emit_ratdma(twice, 62); emit_ratdma(twice, 63);
    std::vector<std::int64_t> wide;
    for (std::int64_t slot = 1; slot <= 150; ++slot) wide.push_back(slot);
    const auto large = c::start_generic_ratdma(0, 150, wide);
    emit_ratdma(large, 0); emit_ratdma(large, 1); emit_ratdma(large, 100);
    auto five = c::start_generic_ratdma(0, 150, {10, 20, 30, 40, 50});
    emit_ratdma(five, 20); emit_ratdma(five, 21);
    (void)c::ratdma_attempt(five, 21); emit_ratdma(five, 36); emit_ratdma(five, 37);
    auto interleaved = c::start_generic_ratdma(0, 150, wide);  // Peer removal then inhibition, forty times: non-telescoping exact terms.
    for (unsigned step = 0; step < 40; ++step) {
        (void)c::remove_peer_candidate(interleaved, interleaved.candidates.back());
        emit_ratdma(interleaved, 100);
        (void)c::ratdma_attempt(interleaved, 100);
    }
    emit_ratdma(interleaved, 32); emit_ratdma(interleaved, 33);
    for (const int initial_timeout : {3, 4, 5, 6, 7}) {
        const auto sequence = c::continuous_timeout_sequence(static_cast<std::uint8_t>(initial_timeout));
        std::cout << "TIMEOUT|" << initial_timeout << '|';
        for (std::size_t index = 0; index < sequence.size(); ++index) std::cout << (index ? "," : "") << static_cast<unsigned>(sequence[index]);
        std::cout << '\n';
    }
    for (const auto [old_index, new_index, sync] : {std::tuple<std::int64_t, std::int64_t, int>{2249, 2250, 0}, {100, 2450, 2}, {0, 4499, 3}, {-1, 0, 1}}) {
        const auto value = c::expiry_announcement(old_index, new_index, static_cast<a::SyncState>(sync));
        std::cout << "EXPIRY|" << old_index << '|' << new_index << '|' << value.slot_offset << '|' << sync << '|' << value.state.raw().value() << '\n';
    }
    for (const auto distance : {222239999999LL, 222240000000LL, 222240000001LL, 0LL, 1LL, 1000000000000LL})
        std::cout << "RANGE|" << distance << '|' << (c::range_class(distance) == c::RangeClass::Within120Nm ? "within" : "beyond") << '\n';
    for (const auto [longitude, latitude] : {std::pair<std::int64_t, std::int64_t>{2500, -2500}, {3, 3}, {1, -1}, {-108000000, 54000000}, {108000000, -54000000}, {0, 0}, {2, -2}}) {
        const auto point = c::ais_position_to_nanodegrees(longitude, latitude);
        std::cout << "POS|" << longitude << '|' << latitude << '|' << point.longitude_ndeg << '|' << point.latitude_ndeg << '\n';
    }
    for (const auto [minute, slot] : {std::pair<std::int64_t, int>{0, 0}, {0, 2249}, {1, 0}, {-1, 2249}, {-1, 0}, {-2, 1}, {12345, 678}, {-12345, 678},
                                      {4102444800LL / 60, 2249}, {c::kMaxAbsoluteMinute, 2249}, {-c::kMaxAbsoluteMinute, 0}})
        std::cout << "ABS|" << minute << '|' << slot << '|' << c::absolute_index({minute, static_cast<std::uint16_t>(slot)}) << '\n';
    for (const auto [index, delta] : {std::pair<std::int64_t, std::int64_t>{0, -1}, {2249, 1}, {-1, 1}, {-2250, -1}, {1000, 37}, {1000, 38}, {5, 16383}, {4499, -4499}}) {
        const auto result = c::offset_index(index, delta);
        const auto slot = c::slot_from_index(result);
        std::cout << "ABSOFF|" << index << '|' << delta << '|' << result << '|' << slot.utc_minute << '|' << slot.slot << '\n';
    }
    // Candidate scenarios and choices.
    {
        Scene scene;
        emit_candidate_row("all_free", scene.report(), false);
        Scene mixed;
        const auto near = peer_at(mixed.pair.a.location, 0, 30000.0);
        const auto far = peer_at(mixed.pair.a.location, 0, 60000.0);
        mixed.peer_slot(c::Channel::Ais1, 2, 2341, near); mixed.peer_slot(c::Channel::Ais1, 3, 2343, far);
        mixed.peer_slot(c::Channel::Ais1, 4, 2345, std::nullopt); mixed.peer_slot(c::Channel::Ais1, 5, 2347, near, 3, c::DecodedMessageKind::Type4);
        mixed.peer_slot(c::Channel::Ais2, 6, 2349, far); mixed.peer_slot(c::Channel::Ais2, 7, 2351, std::nullopt);
        emit_candidate_row("mixed_free_enough", mixed.report(), false);
        emit_candidate_row("mixed_block3", mixed.report(3), false);
        Scene rule1;
        for (std::int64_t index = 2340; index <= 2354; ++index) {
            if (index == 2340 || index == 2347 || index == 2354) continue;
            if (index == 2342) rule1.peer_slot(c::Channel::Ais2, 20, index, near);
            else if (index == 2344) rule1.peer_slot(c::Channel::Ais2, 21, index, far);
            else if (index == 2346) rule1.peer_slot(c::Channel::Ais1, 22, index, far);
            else if (index == 2348) rule1.peer_slot(c::Channel::Ais1, 23, index, std::nullopt);
            else rule1.peer_slot(c::Channel::Ais2, 30 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        }
        emit_candidate_row("rule1_most_distant", rule1.report(), false);
        Scene rule2;
        for (std::int64_t index = 2340; index <= 2354; ++index) {
            if (index == 2340 || index == 2347 || index == 2354) continue;
            if (index == 2346) rule2.peer_slot(c::Channel::Ais1, 22, index, far);
            else if (index == 2348) rule2.peer_slot(c::Channel::Ais1, 23, index, std::nullopt);
            else rule2.peer_slot(c::Channel::Ais2, 30 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        }
        emit_candidate_row("rule2_then_rule4", rule2.report(), false);
        Scene short_set;
        for (std::int64_t index = 2340; index <= 2354; ++index)
            if (index != 2342 && index != 2350) short_set.peer_slot(c::Channel::Ais1, 40 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        emit_candidate_row("two_candidates", short_set.report(), false);
        Scene blocked;
        for (std::int64_t index = 2340; index <= 2354; ++index) blocked.peer_slot(c::Channel::Ais1, 40 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        emit_candidate_row("no_reservation", blocked.report(), false);
        c::OwnReservationRequest current; current.channel = c::Channel::Ais1; current.first_index = kFrame + 60; current.timeout_frames = 3; current.cause = "current";
        (void)blocked.pair.a.knowledge.reserve_own(current, blocked.inputs, blocked.now);
        emit_candidate_row("current_slot_usable", blocked.report(1, kFrame + 60), true);
        Scene adjacency;
        adjacency.peer_slot(c::Channel::Ais1, 9, 2349, peer_at(adjacency.pair.a.location, 0, 1000.0), 3, c::DecodedMessageKind::Type4);
        c::OwnReservationRequest own; own.channel = c::Channel::Ais2; own.first_index = 2347; own.timeout_frames = 3; own.cause = "own AIS2";
        (void)adjacency.pair.a.knowledge.reserve_own(own, adjacency.inputs, adjacency.now);
        emit_candidate_row("adjacency_and_base", adjacency.report(), false);
        Scene inside(kFrame + 95);  // Decision slot 2345 inside the SI: future-only candidates and the eligible most-distant pool.
        inside.peer_slot(c::Channel::Ais1, 22, 2341, peer_at(inside.pair.a.location, 0, 110000.0));
        inside.peer_slot(c::Channel::Ais2, 20, 2348, near);
        inside.peer_slot(c::Channel::Ais2, 21, 2350, peer_at(inside.pair.a.location, 0, 100000.0));
        for (const std::int64_t index : {2351, 2352, 2353, 2354}) inside.peer_slot(c::Channel::Ais1, 60 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        emit_candidate_row("now_inside_si_most_distant", inside.report(), false);
        Scene pair_block;
        pair_block.peer_slot(c::Channel::Ais1, 50, 2341, near);
        pair_block.peer_slot(c::Channel::Ais1, 51, 2342, far);
        for (std::int64_t index = 2343; index <= 2354; ++index)
            if (index != 2350 && index != 2351) pair_block.peer_slot(c::Channel::Ais1, 60 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        emit_candidate_row("two_slot_two_peers", pair_block.report(2), false);
        Scene reuse;
        for (std::int64_t index = 2340; index <= 2354; ++index) {
            if (index == 2348) reuse.peer_slot(c::Channel::Ais1, 90, index, far);
            else if (index != 2344 && index != 2352) reuse.peer_slot(c::Channel::Ais1, 60 + static_cast<std::uint32_t>(index - 2340), index, std::nullopt);
        }
        const auto reuse_report = reuse.report();
        emit_candidate_row("three_with_reuse", reuse_report, false);
        for (std::uint64_t episode = 0; episode < 6; ++episode) {
            const auto choice = c::choose_candidate(reuse_report, reuse.pair.a.knowledge.draw_key(c::ProtocolPurpose::CandidateChoice, episode, 0));
            std::cout << "CHOICE|three_with_reuse|" << choice.ledger.key.scenario_seed << '|' << choice.ledger.key.equipment_id << '|'
                      << choice.ledger.key.power_generation << '|' << episode << "|0|" << reuse_report.candidates.size() << '|' << choice.ledger.value << '|'
                      << choice.candidate.first_index << '\n';
        }
        const auto all_report = scene.report();
        for (std::uint64_t episode = 0; episode < 3; ++episode) {
            const auto choice = c::choose_candidate(all_report, scene.pair.a.knowledge.draw_key(c::ProtocolPurpose::CandidateChoice, episode, 1));
            std::cout << "CHOICE|all_free|" << choice.ledger.key.scenario_seed << '|' << choice.ledger.key.equipment_id << '|'
                      << choice.ledger.key.power_generation << '|' << episode << "|1|" << all_report.candidates.size() << '|' << choice.ledger.value << '|'
                      << choice.candidate.first_index << '\n';
        }
    }
}

}  // namespace

int main(int argc, char** argv) {
    try {
        if (argc == 2 && std::string_view(argv[1]) == "--vectors") { emit_vectors(); return 0; }
        if (argc != 1) throw std::invalid_argument("usage: classa_knowledge_tests [--vectors]");
        section("public_boundary", public_boundary); section("slot_identity", slot_identity); section("philox_contract", philox_contract);
        section("ratdma_contract", ratdma_contract); section("selection_and_adjacency", selection_and_adjacency);
        section("timeout_and_expiry", timeout_and_expiry); section("map_contract", map_contract); section("evidence_contract", evidence_contract);
        section("asymmetry_and_isolation", asymmetry_and_isolation); section("candidate_hierarchy", candidate_hierarchy);
        section("knowledge_draws_and_lifecycle", knowledge_draws_and_lifecycle); section("fast_peer_refresh", fast_peer_refresh);
        section("review_regressions", review_regressions);
        unsigned total = 0;
        for (const auto& row : negatives) { std::cout << "NEGATIVE|" << row.first << '|' << row.second << '\n'; total += row.second; }
        std::cout << "A1-S3 synthetic local-knowledge tests PASS: " << assertions << " assertions, " << total
                  << " categorized invalid fixtures rejected; no transmission, RF reception, FCS, NMEA or UDP produced\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "A1-S3 test failure: " << error.what() << '\n';
        return 1;
    }
}
