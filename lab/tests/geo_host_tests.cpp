#include "geo_api.h"
#include "geo_ais.h"
#include "nmea0183.h"
#include "philox_reference.h"

#include <algorithm>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace {
using namespace aismixer::lab;
using namespace aismixer::lab::geo;
std::size_t failures_checked = 0;

void require(bool value, std::string_view message) {
    if (!value) throw std::runtime_error(std::string(message));
}
template <typename Action>
void invalid(Action action) {
    bool rejected = false;
    try { action(); }
    catch (const std::invalid_argument& error) {
        require(std::string_view(error.what()).size() > 5, "invalid diagnostic needs context");
        rejected = true;
    }
    require(rejected, "invalid geographic input was accepted");
    ++failures_checked;
}
Domain square() {
    return {1, "host-square-v1", "Synthetic unit-test geometry; not a chart",
            {{27 * kDegree, 41 * kDegree}, {29 * kDegree, 41 * kDegree},
             {29 * kDegree, 43 * kDegree}, {27 * kDegree, 43 * kDegree},
             {27 * kDegree, 41 * kDegree}}};
}
Actor actor(std::uint32_t id = 42) {
    return {id, ActorCategory::ClassA, 123456, -654321, -1000, 2000, 2, 1};
}

void polygon_validation() {
    const auto baseline = square();
    validate_domain(baseline);
    auto reversed = baseline;
    std::reverse(reversed.ring.begin(), reversed.ring.end());
    validate_domain(reversed);  // Both orientations are supported.
    auto triangle = baseline;
    triangle.ring.erase(triangle.ring.begin() + 2);
    validate_domain(triangle);
    for (unsigned version : {0u, 2u, UINT32_MAX}) {
        auto candidate = baseline;
        candidate.schema_version = version;
        invalid([&] { validate_domain(candidate); });
    }
    for (const std::string text : std::vector<std::string>{"", std::string(129, 'x'), "x\n", "x\x7f"}) {
        auto candidate = baseline;
        candidate.profile_id = text;
        invalid([&] { validate_domain(candidate); });
    }
    for (const std::string text : std::vector<std::string>{"", std::string(4097, 'x'), "x\r"}) {
        auto candidate = baseline;
        candidate.provenance = text;
        invalid([&] { validate_domain(candidate); });
    }
    const std::vector<std::vector<Point>> bad_rings = {
        {}, {{0, 0}}, {{0, 0}, {kDegree, 0}, {0, 0}},
        {{0, 0}, {kDegree, 0}, {kDegree, kDegree}},  // Not explicitly closed.
        {{0, 0}, {kDegree, 0}, {kDegree, 0}, {0, kDegree}, {0, 0}},
        {{0, 0}, {kDegree, 0}, {2 * kDegree, 0}, {0, 0}},
        {{0, 0}, {2 * kDegree, 0}, {kDegree, kDegree}, {kDegree, 0}, {0, 0}},
        {{0, 0}, {2 * kDegree, 2 * kDegree}, {0, 2 * kDegree}, {2 * kDegree, 0}, {0, 0}},
        {{0, 0}, {2 * kDegree, 0}, {2 * kDegree, 2 * kDegree},
         {0, 2 * kDegree}, {2 * kDegree, 0}, {0, 0}},
        {{0, 0}, {kDegree, 0}, {2 * kDegree, 0}, {2 * kDegree, kDegree},
         {0, kDegree}, {0, 0}},  // Consecutive collinear edge is unsupported.
        {{179 * kDegree, 0}, {-179 * kDegree, 0}, {-179 * kDegree, kDegree},
         {179 * kDegree, kDegree}, {179 * kDegree, 0}},  // Antimeridian crossing.
    };
    for (const auto& ring : bad_rings) {
        auto candidate = baseline;
        candidate.ring = ring;
        invalid([&] { validate_domain(candidate); });
    }
    for (const Point position : std::vector<Point>{
             {180 * kDegree + 1, 0}, {-180 * kDegree - 1, 0},
             {0, 70 * kDegree + 1}, {0, -70 * kDegree - 1},
             {INT64_MIN, 0}, {INT64_MAX, 0}, {0, INT64_MIN}, {0, INT64_MAX}}) {
        auto candidate = baseline;
        candidate.ring[1] = position;
        invalid([&] { validate_domain(candidate); });
    }
    auto wide = baseline;
    wide.ring = {{0, 0}, {60 * kDegree, 0}, {60 * kDegree, kDegree},
                 {0, kDegree}, {0, 0}};
    validate_domain(wide);
    wide.ring[1].longitude_ndeg += 1;
    wide.ring[2].longitude_ndeg += 1;
    invalid([&] { validate_domain(wide); });
    auto very_small = baseline;
    very_small.ring = {{0, 0}, {1, 0}, {1, 1}, {0, 1}, {0, 0}};
    validate_domain(very_small);  // Data quality is distinct from valid geometry.
    auto too_many = baseline;
    too_many.ring.resize(kMaxPolygonVertices + 2, {0, 0});
    too_many.ring.back() = too_many.ring.front();
    invalid([&] { validate_domain(too_many); });
    require(domain_digest(baseline) == domain_digest(square()), "domain fingerprint changed on replay");
    auto changed = baseline;
    changed.ring[1].longitude_ndeg += 1;
    require(domain_digest(changed) != domain_digest(baseline), "domain geometry absent from fingerprint");
    std::cout << "PASS polygon closure, orientation, intersections, exact bounds and bounded schema\n";
}

void polygon_rule_diagnostics() {
    const auto check = [](std::vector<Point> ring, std::string_view expected_rule,
                          bool check_turns) {
        require(ring.size() >= 4, "branch fixture accidentally hits minimum ring size");
        if (check_turns) {
            const auto count = ring.size() - 1;
            for (std::size_t index = 0; index < count; ++index) {
                const auto a = ring[(index + count - 1) % count];
                const auto b = ring[index];
                const auto c = ring[(index + 1) % count];
                const auto cross =
                    static_cast<long double>(b.longitude_ndeg - a.longitude_ndeg) *
                        (c.latitude_ndeg - a.latitude_ndeg) -
                    static_cast<long double>(b.latitude_ndeg - a.latitude_ndeg) *
                        (c.longitude_ndeg - a.longitude_ndeg);
                require(cross != 0, "branch fixture accidentally hits consecutive-collinear rejection");
            }
        }
        auto candidate = square();
        candidate.ring = std::move(ring);
        bool reached = false;
        try { validate_domain(candidate); }
        catch (const std::invalid_argument& error) {
            const std::string_view diagnostic(error.what());
            require(diagnostic.find("domain.ring:") != std::string_view::npos &&
                        diagnostic.find(expected_rule) != std::string_view::npos,
                    "polygon branch fixture was rejected for the wrong rule");
            reached = true;
        }
        require(reached, "invalid simple-ring fixture was accepted");
        ++failures_checked;
    };
    // Four points pass the minimum-size guard; the missing repeated first
    // point must specifically reach the explicit-closure diagnostic.
    check({{0, 0}, {6 * kDegree, 0}, {6 * kDegree, 4 * kDegree}, {0, 4 * kDegree}},
          "first point must be repeated exactly at the end", false);
    // Vertex (3,0) touches the interior of the bottom edge. Every consecutive
    // turn is non-collinear; this must reach the nonadjacent-edge predicate.
    check({{0, 0}, {6 * kDegree, 0}, {6 * kDegree, 4 * kDegree},
           {3 * kDegree, 4 * kDegree}, {3 * kDegree, 0},
           {2 * kDegree, 3 * kDegree}, {0, 4 * kDegree}, {0, 0}},
          "nonadjacent edges intersect or touch", true);
    // A repeated nonclosing point, with no degenerate consecutive turns.
    check({{0, 0}, {4 * kDegree, 0}, {2 * kDegree, 2 * kDegree},
           {4 * kDegree, 4 * kDegree}, {0, 4 * kDegree},
           {2 * kDegree, 2 * kDegree}, {0, 0}},
          "repeated nonclosing vertices", true);
    // Edge (-2,0)->(2,0) overlaps (0,0)->(6,0). The preceding bridge ends
    // outside the bottom edge, so the first offending edge pair is collinear
    // overlap, not an earlier T-junction or a consecutive-collinear turn.
    check({{0, 0}, {6 * kDegree, 0}, {6 * kDegree, 4 * kDegree},
           {-2 * kDegree, 0}, {2 * kDegree, 0}, {2 * kDegree, -2 * kDegree},
           {0, -2 * kDegree}, {0, 0}},
          "nonadjacent edges intersect or touch", true);
    std::cout << "PASS four targeted polygon branches: unclosed, non-collinear self-touch, revisit, overlap; rule-specific diagnostics\n";
}

void philox_known_answers() {
    using namespace aismixer::lab::test_reference;
    require(philox4x32_10({{0, 0, 0, 0}}, {{0, 0}}) ==
                PhiloxBlock{{0x6627E8D5, 0xE169C58D, 0xBC57AC4C, 0x9B00DBD8}},
            "portable Philox4x32-10 zero-key/counter known answer");
    require(philox4x32_10({{UINT32_MAX, UINT32_MAX, UINT32_MAX, UINT32_MAX}},
                          {{UINT32_MAX, UINT32_MAX}}) ==
                PhiloxBlock{{0x408F276D, 0x41C83B0E, 0xA20BC7C6, 0x6D5451FD}},
            "portable Philox4x32-10 all-ones known answer");
    require(philox4x32_10({{0x243F6A88, 0x85A308D3, 0x13198A2E, 0x03707344}},
                          {{0xA4093822, 0x299F31D0}}) ==
                PhiloxBlock{{0xD16CFE09, 0x94FDCCEB, 0x5001E420, 0x24126EA1}},
            "portable Philox4x32-10 nontrivial known answer");
    struct QualifiedWord { std::uint64_t seed, subsequence, offset; std::uint32_t expected; };
    const QualifiedWord words[] = {{42, 10, 0, 1037580057}, {42, 10, 2, 4007308748},
        {43, 0, 3, 456409990}, {123456789, 902, 200, 2801256552},
        {UINT64_MAX, UINT32_MAX, UINT64_MAX, 2839554561}};
    for (const auto& word : words) {
        require(curand_qualified_word(word.seed, word.subsequence, word.offset) == word.expected,
                "CUDA12.9 seed/subsequence/scalar-offset adapter known answer");
    }
    struct InjectedWords {
        unsigned calls = 0;
        std::uint32_t next() { return calls++ < 2 ? 0 : UINT32_MAX; }
    } injected;
    require(bounding_integer(injected, 0, 10) == 4 && injected.calls == 4,
            "unbiased integer reference failed to reject a below-threshold proposal");
    struct ZeroWords { unsigned calls = 0; std::uint32_t next() { ++calls; return 0; } } zeros;
    bool exhausted = false;
    try { (void)bounding_integer(zeros, 0, 10); }
    catch (const std::runtime_error&) { exhausted = true; }
    require(exhausted && zeros.calls == 32, "reference integer rejection must stop after16 proposals");
    std::cout << "PASS independent pure-integer Philox:3 portable block vectors,5 qualified scalar vectors, unbiased/bounded proposal probes\n";
}

void anchors_receivers() {
    const std::vector<Actor> actors{actor(0), actor(UINT32_MAX)};
    std::vector<Anchor> anchors{{UINT32_MAX, {30 * kDegree, 40 * kDegree}},
                                {0, {28 * kDegree, 42 * kDegree}}};
    validate_anchors(actors, anchors);
    const auto digest = anchor_digest(anchors);
    std::reverse(anchors.begin(), anchors.end());
    require(anchor_digest(anchors) == digest, "anchor fingerprint depends on input order");
    invalid([&] { validate_anchors(actors, {}); });
    invalid([&] { validate_anchors(actors, {anchors[0]}); });
    auto duplicate = anchors;
    duplicate[1].actor_id = duplicate[0].actor_id;
    invalid([&] { validate_anchors(actors, duplicate); });
    auto unknown = anchors;
    unknown[0].actor_id = 123;
    invalid([&] { validate_anchors(actors, unknown); });
    auto impossible = anchors;
    impossible[0].tick_zero.latitude_ndeg = 70 * kDegree + 1;
    invalid([&] { validate_anchors(actors, impossible); });
    validate_anchors({}, {});

    std::vector<Receiver> receivers{{0, {28 * kDegree, 42 * kDegree}, -1000000, "WGS84_ellipsoid"},
                                    {UINT32_MAX, {-180 * kDegree, -70 * kDegree}, 10000000, "WGS84_ellipsoid"}};
    validate_receivers(receivers);
    validate_receivers({});
    auto duplicate_receiver = receivers;
    duplicate_receiver[1].receiver_id = duplicate_receiver[0].receiver_id;
    invalid([&] { validate_receivers(duplicate_receiver); });
    for (const std::string datum : {"", "MSL", "WGS84", "WGS84_ellipsoid\n"}) {
        auto candidate = receivers;
        candidate[0].height_datum = datum;
        invalid([&] { validate_receivers(candidate); });
    }
    for (const std::int64_t height : std::vector<std::int64_t>{-1000001, 10000001, INT64_MIN, INT64_MAX}) {
        auto candidate = receivers;
        candidate[0].antenna_height_mm = height;
        invalid([&] { validate_receivers(candidate); });
    }
    auto invalid_location = receivers;
    invalid_location[0].location.longitude_ndeg = 181 * kDegree;
    invalid([&] { validate_receivers(invalid_location); });
    std::vector<Receiver> excessive(kMaxReceivers + 1, receivers[0]);
    invalid([&] { validate_receivers(excessive); });
    std::cout << "PASS explicit anchor association and separate passive receiver identity/height datum\n";
}

void run_validation() {
    const auto normal = actor();
    validate_run({normal}, 0, 2, 1, 2);
    validate_run({normal}, 0, 0, 60, 1);
    validate_run({}, kMaxAbsoluteTick, 1, kMaxTickSeconds, 1);
    validate_run({}, 0, 0, 1, 1);
    auto stationary = normal;
    stationary.vx_mm_per_s = stationary.vy_mm_per_s = 0;
    validate_run({stationary}, kMaxAbsoluteTick, 1, kMaxTickSeconds, 1);
    invalid([&] { validate_run({normal, normal}, 0, 1, 1, 2); });
    for (unsigned failure = 0; failure < 10; ++failure) {
        auto candidate = normal;
        if (failure == 0) candidate.category = static_cast<ActorCategory>(6);
        if (failure == 1) candidate.report_interval_ticks = 0;
        if (failure == 2) candidate.phase_ticks = candidate.report_interval_ticks;
        if (failure == 3) candidate.x_mm = kMaxInitialPositionMm + 1;
        if (failure == 4) candidate.y_mm = -kMaxInitialPositionMm - 1;
        if (failure == 5) candidate.vx_mm_per_s = kMaxVelocityMmPerS + 1;
        if (failure == 6) candidate.vy_mm_per_s = -kMaxVelocityMmPerS - 1;
        if (failure == 7) { candidate.category = ActorCategory::BaseStation; }
        if (failure == 8) { candidate.category = ActorCategory::AtoN; }
        if (failure == 9) candidate.x_mm = INT64_MIN;
        invalid([&] { validate_run({candidate}, 0, 1, 1, 1); });
    }
    invalid([&] { validate_run({normal}, 0, 1, 0, 1); });
    invalid([&] { validate_run({normal}, 0, 1, kMaxTickSeconds + 1, 1); });
    invalid([&] { validate_run({}, kMaxAbsoluteTick + 1, 0, 1, 1); });
    invalid([&] { validate_run({}, kMaxAbsoluteTick, 2, 1, 1); });
    invalid([&] { validate_run({}, UINT64_MAX, UINT64_MAX, 1, 1); });
    invalid([&] { validate_run({}, 0, kMaxSceneTicks + 1, 1, 1); });
    invalid([&] { validate_run({}, 0, 0, 1, 0); });
    invalid([&] { validate_run({}, 0, 0, 1, kMaxSceneCandidates + 1); });
    invalid([&] { validate_run({stationary, actor(99)}, 0, 1, 1, 1); });
    auto radial = normal;
    radial.report_interval_ticks = 1;
    radial.phase_ticks = 0;
    radial.vx_mm_per_s = 100000;
    radial.vy_mm_per_s = 0;
    validate_run({radial}, 10, 1, 1, 1);  // Exactly 1000m from tick zero.
    invalid([&] { validate_run({radial}, 11, 1, 1, 1); });
    radial.vx_mm_per_s = radial.vy_mm_per_s = 100000;
    invalid([&] { validate_run({radial}, 8, 1, 1, 1); });  // Radial, not per-axis bound.
    radial.report_interval_ticks = 100;
    radial.phase_ticks = 99;
    invalid([&] { validate_run({radial}, 10, 1, 1, 1); });  // No silent bypass for unscheduled actors.
    std::vector<Actor> many(kMaxSceneActors + 1, stationary);
    invalid([&] { validate_run(many, 0, 0, 1, 1); });
    std::cout << "PASS B2 scheduling bounds, absolute time, capacity and conservative radial displacement checks\n";
}

std::int64_t reference_ais(std::int64_t value) {
    // 1 nanodegree * 600000 AIS units/degree = 3/5000 wire units.
    const bool negative = value < 0;
    const std::uint64_t magnitude = static_cast<std::uint64_t>(negative ? -value : value);
    const std::uint64_t scaled = magnitude * 3;
    auto rounded = scaled / 5000;
    if (scaled % 5000 >= 2500) ++rounded;
    return negative ? -static_cast<std::int64_t>(rounded) : static_cast<std::int64_t>(rounded);
}
std::uint64_t reference_digest(const std::vector<GeoEvent>& events) {
    std::uint64_t digest = 14695981039346656037ULL;
    const auto append = [&digest](std::uint64_t value, unsigned width) {
        for (unsigned index = 0; index < width; ++index) {
            digest ^= (value >> (8 * index)) & 255;
            digest *= 1099511628211ULL;
        }
    };
    for (const auto& event : events) {
        append(event.absolute_tick, 8); append(event.simulation_seconds, 8);
        append(event.actor_id, 4); append(static_cast<std::uint32_t>(event.category), 4);
        append(static_cast<std::uint64_t>(event.x_mm), 8);
        append(static_cast<std::uint64_t>(event.y_mm), 8);
        append(static_cast<std::uint64_t>(event.location.longitude_ndeg), 8);
        append(static_cast<std::uint64_t>(event.location.latitude_ndeg), 8);
        append(event.random_u32, 4);
    }
    return digest;
}
void seeds_units_digest() {
    for (const auto seed : {UINT64_C(0), UINT64_C(42), UINT64_MAX}) {
        const auto selected = select_seed(SeedMode::Fixed, seed);
        require(selected.mode == SeedMode::Fixed && selected.value == seed, "fixed seed changed");
    }
    const auto fresh = select_seed(SeedMode::Fresh);
    require(fresh.mode == SeedMode::Fresh &&
                select_seed(SeedMode::Fixed, fresh.value).value == fresh.value,
            "fresh seed cannot be explicitly replayed");
    invalid([] { (void)select_seed(static_cast<SeedMode>(99), 1); });
    for (const auto purpose : {RngPurpose::Placement, RngPurpose::Motion, RngPurpose::Atmosphere,
                               RngPurpose::ReceiverFading, RngPurpose::Failure}) {
        for (const auto id : {0u, 1u, UINT32_MAX}) {
            const auto expected = (std::uint64_t{1} << 56) |
                (static_cast<std::uint64_t>(purpose) << 48) | id;
            require(rng_subsequence(purpose, id) == expected, "RNG semantic key/version packing");
        }
    }
    invalid([] { (void)rng_subsequence(static_cast<RngPurpose>(0), 1); });
    invalid([] { (void)rng_subsequence(static_cast<RngPurpose>(6), 1); });
    for (const auto coordinate : std::vector<std::int64_t>{
             0, 1, -1, 833, 834, -833, -834, 2499, 2500, 2501, -2499, -2500, -2501,
             7500, -7500, 28 * kDegree, -28 * kDegree, 70 * kDegree,
             90 * kDegree, -90 * kDegree, 180 * kDegree, -180 * kDegree}) {
        require(ais_coordinate(coordinate, true) == reference_ais(coordinate), "AIS longitude integer rounding");
        if (coordinate >= -90 * kDegree && coordinate <= 90 * kDegree) {
            require(ais_coordinate(coordinate, false) == reference_ais(coordinate), "AIS latitude integer rounding");
        }
    }
    require(ais_coordinate(2500, true) == 2 && ais_coordinate(-2500, true) == -2 &&
                ais_coordinate(7500, false) == 5 && ais_coordinate(-7500, false) == -5,
            "exact half-way rounding must go away from zero");
    for (const auto coordinate : std::vector<std::int64_t>{
             180 * kDegree + 1, -180 * kDegree - 1, 181 * kDegree, INT64_MIN, INT64_MAX}) {
        invalid([&] { (void)ais_coordinate(coordinate, true); });
    }
    for (const auto coordinate : std::vector<std::int64_t>{
             90 * kDegree + 1, -90 * kDegree - 1, 91 * kDegree, INT64_MIN, INT64_MAX}) {
        invalid([&] { (void)ais_coordinate(coordinate, false); });
    }
    const std::vector<GeoEvent> events{{5, 15, 42, ActorCategory::ClassA, 100, -200,
                                      {28 * kDegree, 42 * kDegree}, 0x12345678}};
    require(event_digest(events) == reference_digest(events) &&
                event_digest({}) == 14695981039346656037ULL,
            "GeoEvent digest must cover explicit little-endian 60-byte fields, no padding");
    auto changed = events;
    changed[0].simulation_seconds += 1;
    require(event_digest(changed) != event_digest(events), "simulation time absent from digest");
    std::cout << "PASS OS fresh seed/fixed replay, five RNG purpose domains, exact AIS rounding and canonical digest\n";
    std::cout << "Fresh seed selected for replay: " << fresh.value << '\n';
}

void offline_bridge() {
    const std::vector<Actor> actors{actor(20), actor(10)};
    const auto identities = synthetic_identities(actors);
    require(identities.size() == 2 && identities[0].actor_id == 10 &&
                identities[0].mmsi == "100000001" && identities[1].actor_id == 20 &&
                identities[1].mmsi == "100000002", "explicit synthetic sorted-rank identity catalog");
    auto reversed = actors;
    std::reverse(reversed.begin(), reversed.end());
    const auto again = synthetic_identities(reversed);
    require(again[0].mmsi == identities[0].mmsi && again[1].mmsi == identities[1].mmsi,
            "synthetic identities depend on input order");
    invalid([&] { (void)synthetic_identities({actors[0], actors[0]}); });
    const std::vector<GeoEvent> events{{0, 0, 10, ActorCategory::ClassA, 1, -2, {-2500, 7500}, 99},
                                      {1, 2, 20, ActorCategory::ClassB, 3, -4, {2500, -7500}, 123}};
    const auto output = encode_offline(events, identities);
    require(output.size() == 2 && output[0].logical_transmission_id == 10 &&
                output[1].logical_transmission_id == (UINT64_C(1) << 32 | 20) &&
                output[1].simulation_seconds == 2, "offline logical transmission/time metadata");
    for (const auto& transmission : output) {
        require(transmission.sentences.size() == 1 && transmission.sentences[0].size() <= 82 &&
                    transmission.sentences[0].substr(transmission.sentences[0].size() - 2) == "\r\n",
                "offline bridge altered B3 framing");
    }
    require(encode_offline({}, {}).empty(), "empty offline bridge created transmissions");
    for (const auto category : {ActorCategory::BaseStation, ActorCategory::AtoN,
                                ActorCategory::SAR, ActorCategory::Emergency}) {
        auto bad = events;
        bad[0].category = category;
        invalid([&] { (void)encode_offline(bad, identities); });
    }
    invalid([&] { (void)encode_offline(events, {}); });
    invalid([&] { (void)encode_offline(events, {{10, "100000001"}, {20, "100000001"}}); });
    invalid([&] { (void)encode_offline(events, {{10, "000000000"}, {20, "100000002"}}); });
    auto bad = events;
    bad[0].simulation_seconds = 1;
    invalid([&] { (void)encode_offline(bad, identities); });
    bad = events;
    std::reverse(bad.begin(), bad.end());
    invalid([&] { (void)encode_offline(bad, identities); });
    bad = events;
    bad[0].location.longitude_ndeg = 181 * kDegree;
    invalid([&] { (void)encode_offline(bad, identities); });
    bad = events;
    bad[1].category = static_cast<ActorCategory>(99);
    invalid([&] { (void)encode_offline(bad, identities); });
    std::cout << "PASS host-only offline ClassA/Type1 and ClassB/Type18 bridge; explicit identities, simulation time, forbidden categories\n";
}
}  // namespace

int main() {
    try {
        polygon_validation();
        polygon_rule_diagnostics();
        philox_known_answers();
        anchors_receivers();
        run_validation();
        seeds_units_digest();
        offline_bridge();
        std::cout << "PASS " << failures_checked << " invalid host geography cases; no CUDA calls\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL geo host: " << error.what() << '\n';
        return 1;
    }
}
