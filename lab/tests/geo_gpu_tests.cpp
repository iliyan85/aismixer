#include "geo_api.h"
#include "philox_reference.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {
using namespace aismixer::lab;
using namespace aismixer::lab::geo;
constexpr double kPi = 3.1415926535897932384626433832795;
constexpr double kA = 6378137.0;
constexpr double kF = 1.0 / 298.257223563;
constexpr double kE2 = kF * (2.0 - kF);
std::size_t invalid_cases = 0;
double maximum_oracle_error_m = 0;

void require(bool condition, std::string_view message) {
    if (!condition) throw std::runtime_error(std::string(message));
}
template <typename Action>
void invalid(Action action) {
    bool rejected = false;
    try { action(); }
    catch (const std::invalid_argument& error) {
        require(std::string_view(error.what()).size() > 5, "missing invalid-input context");
        rejected = true;
    }
    require(rejected, "invalid GPU scenario accepted");
    ++invalid_cases;
}
Domain rectangle(std::int64_t west = 27 * kDegree, std::int64_t east = 29 * kDegree,
                 std::int64_t south = 41 * kDegree, std::int64_t north = 43 * kDegree) {
    return {1, "cuda-qualification-v1", "Synthetic test polygon; no geographic data claim",
            {{west, south}, {east, south}, {east, north}, {west, north}, {west, south}}};
}
Domain concave() {
    return {1, "cuda-concave-v1", "Synthetic L-shaped longitude/latitude test polygon",
            {{27 * kDegree, 41 * kDegree}, {29 * kDegree, 41 * kDegree},
             {29 * kDegree, 42 * kDegree}, {28 * kDegree, 42 * kDegree},
             {28 * kDegree, 43 * kDegree}, {27 * kDegree, 43 * kDegree},
             {27 * kDegree, 41 * kDegree}}};
}
Options options(std::size_t capacity = 128, unsigned block = 256) {
    Options result;
    result.candidate_capacity = capacity;
    result.block_size = block;
    return result;
}
Actor stationary(std::uint32_t id, ActorCategory category = ActorCategory::ClassA) {
    return {id, category, 1234567, -7654321, 0, 0, 1, 0};
}
bool same_point(Point a, Point b) {
    return a.longitude_ndeg == b.longitude_ndeg && a.latitude_ndeg == b.latitude_ndeg;
}
bool same_event(const GeoEvent& a, const GeoEvent& b) {
    return a.absolute_tick == b.absolute_tick && a.simulation_seconds == b.simulation_seconds &&
        a.actor_id == b.actor_id && a.category == b.category && a.x_mm == b.x_mm &&
        a.y_mm == b.y_mm && same_point(a.location, b.location) && a.random_u32 == b.random_u32;
}
bool same_events(const std::vector<GeoEvent>& first, const std::vector<GeoEvent>& second) {
    return first.size() == second.size() && std::equal(first.begin(), first.end(), second.begin(), same_event);
}

std::uint64_t independent_digest(const std::vector<GeoEvent>& events) {
    std::uint64_t hash = UINT64_C(14695981039346656037);
    const auto append = [&hash](std::uint64_t value, unsigned bytes) {
        for (unsigned index = 0; index < bytes; ++index) {
            hash = (hash ^ ((value >> (8 * index)) & 255)) * UINT64_C(1099511628211);
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
    return hash;
}

// Independent winding-number oracle, rather than the production CUDA crossing
// predicate. Exact horizontal/vertical and integer diagonal boundary fixtures
// below additionally pin 1-nanodegree distinctions without floating tolerance.
bool inside(const Domain& domain, Point point) {
    int winding = 0;
    for (std::size_t index = 0; index + 1 < domain.ring.size(); ++index) {
        const auto a = domain.ring[index];
        const auto b = domain.ring[index + 1];
        const long double x = static_cast<long double>(point.longitude_ndeg) - a.longitude_ndeg;
        const long double y = static_cast<long double>(point.latitude_ndeg) - a.latitude_ndeg;
        const long double dx = static_cast<long double>(b.longitude_ndeg) - a.longitude_ndeg;
        const long double dy = static_cast<long double>(b.latitude_ndeg) - a.latitude_ndeg;
        const long double cross = dx * y - dy * x;
        if (cross == 0 && point.longitude_ndeg >= std::min(a.longitude_ndeg, b.longitude_ndeg) &&
            point.longitude_ndeg <= std::max(a.longitude_ndeg, b.longitude_ndeg) &&
            point.latitude_ndeg >= std::min(a.latitude_ndeg, b.latitude_ndeg) &&
            point.latitude_ndeg <= std::max(a.latitude_ndeg, b.latitude_ndeg)) return true;
        if (a.latitude_ndeg <= point.latitude_ndeg && b.latitude_ndeg > point.latitude_ndeg && cross > 0) ++winding;
        if (a.latitude_ndeg > point.latitude_ndeg && b.latitude_ndeg <= point.latitude_ndeg && cross < 0) --winding;
    }
    return winding != 0;
}
struct Ecef { double x, y, z; };
Ecef ecef(double longitude_rad, double latitude_rad) {
    const double sin_phi = std::sin(latitude_rad);
    const double radius = kA / std::sqrt(1.0 - kE2 * sin_phi * sin_phi);
    return {radius * std::cos(latitude_rad) * std::cos(longitude_rad),
            radius * std::cos(latitude_rad) * std::sin(longitude_rad),
            radius * (1.0 - kE2) * sin_phi};
}
struct Geodetic { double longitude_rad, latitude_rad; };
Geodetic independent_tangent(Point anchor, double east_m, double north_m) {
    const double longitude = static_cast<double>(anchor.longitude_ndeg) / kDegree * kPi / 180.0;
    const double latitude = static_cast<double>(anchor.latitude_ndeg) / kDegree * kPi / 180.0;
    auto xyz = ecef(longitude, latitude);
    // Cartesian ENU basis at the WGS84 ellipsoid surface; zero input height.
    // This reference performs ECEF -> geodetic inversion rather than sharing
    // the production local-curvature angle increment equations.
    xyz.x += -east_m * std::sin(longitude) - north_m * std::sin(latitude) * std::cos(longitude);
    xyz.y += east_m * std::cos(longitude) - north_m * std::sin(latitude) * std::sin(longitude);
    xyz.z += north_m * std::cos(latitude);
    const double p = std::hypot(xyz.x, xyz.y);
    double phi = std::atan2(xyz.z, p * (1.0 - kE2));
    for (unsigned iteration = 0; iteration < 12; ++iteration) {
        const double sin_phi = std::sin(phi);
        const double radius = kA / std::sqrt(1.0 - kE2 * sin_phi * sin_phi);
        phi = std::atan2(xyz.z + kE2 * radius * sin_phi, p);
    }
    return {std::atan2(xyz.y, xyz.x), phi};
}
void projection_check(const GeoEvent& event, Point anchor, const Actor& actor,
                      std::uint32_t tick_seconds) {
    const auto seconds = event.absolute_tick * tick_seconds;
    require(event.simulation_seconds == seconds, "execution time leaked into simulated seconds");
    const auto signed_seconds = static_cast<std::int64_t>(seconds);
    require(event.x_mm == actor.x_mm + static_cast<std::int64_t>(actor.vx_mm_per_s) * signed_seconds &&
                event.y_mm == actor.y_mm + static_cast<std::int64_t>(actor.vy_mm_per_s) * signed_seconds,
            "B2 local trajectory changed");
    if (seconds == 0) require(same_point(event.location, anchor), "tick-zero anchor included local x/y twice");
    const auto reference = independent_tangent(anchor,
        static_cast<double>(actor.vx_mm_per_s) * static_cast<double>(seconds) / 1000.0,
        static_cast<double>(actor.vy_mm_per_s) * static_cast<double>(seconds) / 1000.0);
    const auto expected_xyz = ecef(reference.longitude_rad, reference.latitude_rad);
    const auto actual_xyz = ecef(static_cast<double>(event.location.longitude_ndeg) / kDegree * kPi / 180.0,
                                 static_cast<double>(event.location.latitude_ndeg) / kDegree * kPi / 180.0);
    const double error = std::sqrt(std::pow(actual_xyz.x - expected_xyz.x, 2) +
                                  std::pow(actual_xyz.y - expected_xyz.y, 2) +
                                  std::pow(actual_xyz.z - expected_xyz.z, 2));
    maximum_oracle_error_m = std::max(maximum_oracle_error_m, error);
    require(error <= 0.3, "GPU tangent output exceeds independent WGS84 0.3m error budget");
}
std::uint64_t scheduled_count(const std::vector<Actor>& actors, std::uint64_t start,
                               std::uint64_t count) {
    std::uint64_t result = 0;
    for (std::uint64_t tick = start; tick < start + count; ++tick) {
        for (const auto& actor : actors) result += tick % actor.report_interval_ticks == actor.phase_ticks;
    }
    return result;
}
void metrics_check(const Result& result, std::size_t actors, std::uint64_t ticks,
                   std::uint64_t scheduled) {
    require(result.metrics.candidates == actors * ticks && result.metrics.scheduled == scheduled,
            "candidate/scheduling counters");
    require(result.metrics.accepted == result.events.size() &&
                result.metrics.rejected == scheduled - result.events.size() &&
                result.metrics.unscheduled == actors * ticks - scheduled,
            "accepted/rejected/unscheduled partition");
    require(result.metrics.d2h_record_bytes == result.events.size() * sizeof(GeoEvent),
            "D2H contains unaccepted candidate records");
    require(result.metrics.uncompacted_record_bytes == actors * ticks * sizeof(GeoEvent),
            "precompaction transfer comparison");
    require(result.metrics.d2h_diagnostic_bytes <= 4096, "diagnostics are not a compact summary");
    require(result.metrics.wall_ms >= 0 && result.metrics.motion_ms >= 0 &&
                result.metrics.predicate_ms >= 0 && result.metrics.compaction_ms >= 0 &&
                result.metrics.d2h_ms >= 0, "negative timing metrics");
    for (std::size_t index = 1; index < result.events.size(); ++index) {
        const auto& first = result.events[index - 1];
        const auto& second = result.events[index];
        require(first.absolute_tick < second.absolute_tick ||
                    (first.absolute_tick == second.absolute_tick && first.actor_id < second.actor_id),
                "device compaction output is not canonical tick/ID order");
    }
}

std::vector<Actor> actors_fixture() {
    return {{30, ActorCategory::SAR, 7000, -8000, -1000, 2000, 3, 1},
            {10, ActorCategory::ClassA, 1000, -2000, 1000, -500, 2, 0},
            {40, ActorCategory::Emergency, -9000, 10000, 0, 0, 1, 0},
            {20, ActorCategory::ClassB, -3000, 4000, -500, -1000, 2, 1},
            {50, ActorCategory::BaseStation, 11000, -12000, 0, 0, 5, 0},
            {60, ActorCategory::AtoN, -13000, 14000, 0, 0, 4, 1}};
}
std::vector<Anchor> anchors_fixture() {
    return {{60, {28006000000, 42006000000}}, {20, {28002000000, 42002000000}},
            {40, {28004000000, 42004000000}}, {10, {28001000000, 42001000000}},
            {50, {28005000000, 42005000000}}, {30, {28003000000, 42003000000}}};
}
void deterministic_motion() {
    auto actors = actors_fixture();
    auto anchors = anchors_fixture();
    Session reference(rectangle(), actors, anchors, {SeedMode::Fixed, 42}, options());
    const auto baseline = reference.run(0, 8, 2);
    require(baseline.events.size() == 23 && baseline.metrics.candidates == 48,
            "independently counted six-actor fixture cardinality");
    metrics_check(baseline, 6, 8, 23);
    const auto old = generate_event_intents(actors, 42, 0, 8, 2);
    require(old.size() == baseline.events.size(), "B2 scheduled count changed");
    std::map<std::uint32_t, Actor> by_actor;
    std::map<std::uint32_t, Point> by_anchor;
    for (const auto& actor : actors) by_actor.emplace(actor.actor_id, actor);
    for (const auto& anchor : anchors) by_anchor.emplace(anchor.actor_id, anchor.tick_zero);
    for (std::size_t index = 0; index < old.size(); ++index) {
        const auto& event = baseline.events[index];
        require(event.absolute_tick == old[index].absolute_tick && event.actor_id == old[index].actor_id &&
                    event.category == old[index].category && event.x_mm == old[index].x_mm &&
                    event.y_mm == old[index].y_mm && event.random_u32 == old[index].random_u32,
                "B2 event semantics/diagnostic Philox word changed in geospatial path");
        require(event.random_u32 == test_reference::curand_qualified_word(42, event.actor_id,
                                                                          event.absolute_tick),
                "B2 diagnostic word differs from independent integer Philox reference");
        require(inside(rectangle(), event.location), "GPU accepted point outside independent polygon oracle");
        projection_check(event, by_anchor.at(event.actor_id), by_actor.at(event.actor_id), 2);
    }
    require(same_events(reference.run(0, 8, 2).events, baseline.events), "fixed seed repeatability");
    auto first = reference.run(0, 3, 2).events;
    const auto second = reference.run(3, 5, 2).events;
    first.insert(first.end(), second.begin(), second.end());
    require(same_events(first, baseline.events), "absolute split-range batch boundary affects trajectory/RNG");
    std::reverse(actors.begin(), actors.end());
    std::reverse(anchors.begin(), anchors.end());
    for (const unsigned block : {64u, 128u, 256u}) {
        Session reordered(rectangle(), actors, anchors, {SeedMode::Fixed, 42}, options(128, block));
        require(same_events(reordered.run(0, 8, 2).events, baseline.events),
                "actor/anchor order or launch geometry affected canonical output");
    }
    Session bounded(rectangle(), actors, anchors, {SeedMode::Fixed, 42}, options(30));
    invalid([&] { (void)bounded.run(0, 8, 2); });
    auto bounded_first = bounded.run(0, 3, 2).events;
    const auto bounded_second = bounded.run(3, 5, 2).events;
    bounded_first.insert(bounded_first.end(), bounded_second.begin(), bounded_second.end());
    require(same_events(bounded_first, baseline.events), "reusable smaller GPU batch changes accepted bytes");
    const auto zero = reference.run(12, 0, 60);
    metrics_check(zero, actors.size(), 0, 0);
    require(zero.events.empty(), "zero ticks produced records");
    Session empty(rectangle(), {}, {}, {SeedMode::Fixed, 42}, options(1));
    metrics_check(empty.run(7, 4, 2), 0, 4, 0);
    std::cout << "PASS actual CUDA motion/scheduling/compaction: 48 candidates, 23 accepted; B2 comparison, reorder, launch shapes and split ranges\n";
}

void boundaries_and_filtering() {
    const auto domain = rectangle();
    const std::vector<Point> points{{27 * kDegree, 41 * kDegree}, {29 * kDegree, 43 * kDegree},
        {28 * kDegree, 41 * kDegree}, {29 * kDegree, 42 * kDegree},
        {27 * kDegree - 1, 42 * kDegree}, {29 * kDegree + 1, 42 * kDegree},
        {28 * kDegree, 41 * kDegree - 1}, {28 * kDegree, 43 * kDegree + 1},
        {28 * kDegree, 42 * kDegree}};
    std::vector<Actor> actors;
    std::vector<Anchor> anchors;
    std::vector<std::uint32_t> expected_ids;
    for (std::size_t index = 0; index < points.size(); ++index) {
        const auto id = static_cast<std::uint32_t>(100 + index);
        actors.push_back(stationary(id));
        anchors.push_back({id, points[index]});
        if (inside(domain, points[index])) expected_ids.push_back(id);
    }
    require(expected_ids == std::vector<std::uint32_t>({100, 101, 102, 103, 108}),
            "independent exact inclusive boundary fixture changed");
    Session session(domain, actors, anchors, {SeedMode::Fixed, 1}, options());
    const auto result = session.run(0, 1, 1);
    metrics_check(result, 9, 1, 9);
    require(result.events.size() == expected_ids.size(), "boundary-inclusive GPU predicate count");
    for (std::size_t index = 0; index < expected_ids.size(); ++index) {
        require(result.events[index].actor_id == expected_ids[index] &&
                    same_point(result.events[index].location, points[expected_ids[index] - 100]),
                "boundary classification/stable compaction association");
    }
    auto all_out = anchors;
    for (auto& anchor : all_out) anchor.tick_zero = {32 * kDegree, 42 * kDegree};
    Session rejected(domain, actors, all_out, {SeedMode::Fixed, 1}, options());
    const auto no_events = rejected.run(0, 3, 1);
    metrics_check(no_events, 9, 3, 27);
    require(no_events.events.empty() && no_events.metrics.rejected == 27 &&
                no_events.metrics.d2h_record_bytes == 0, "all-rejected path downloaded candidates");

    // A genuine expected domain exit, rather than malformed geography. B2 local
    // x/y is deliberately far from zero to catch adding it to the anchor twice.
    const auto narrow = rectangle(28 * kDegree, 28 * kDegree + 1000000,
                                 42 * kDegree - 1000000, 42 * kDegree + 1000000);
    Actor mover{77, ActorCategory::ClassA, 999999999, -999999999, 100000, 0, 1, 0};
    Session exits(narrow, {mover}, {{77, {28 * kDegree, 42 * kDegree}}},
                  {SeedMode::Fixed, 1}, options());
    const auto exit_result = exits.run(0, 4, 1);
    metrics_check(exit_result, 1, 4, 4);
    require(exit_result.events.size() == 1 && exit_result.events[0].absolute_tick == 0 &&
                exit_result.metrics.rejected == 3, "valid polygon exit was treated as malformed or silently accepted");

    const Domain triangle{1, "diagonal-v1", "Synthetic diagonal exact-boundary geometry",
        {{0, 0}, {2 * kDegree, 0}, {0, 2 * kDegree}, {0, 0}}};
    const std::vector<Point> diagonal{{kDegree, kDegree}, {kDegree, kDegree - 1},
                                      {kDegree, kDegree + 1}, {0, 0}};
    actors.clear(); anchors.clear();
    for (unsigned index = 0; index < diagonal.size(); ++index) {
        actors.push_back(stationary(index)); anchors.push_back({index, diagonal[index]});
    }
    Session diagonal_session(triangle, actors, anchors, {SeedMode::Fixed, 1}, options());
    const auto diagonal_result = diagonal_session.run(0, 1, 1);
    require(diagonal_result.events.size() == 3 && diagonal_result.events[0].actor_id == 0 &&
                diagonal_result.events[1].actor_id == 1 && diagonal_result.events[2].actor_id == 3,
            "one-nanodegree diagonal boundary predicate");
    std::cout << "PASS actual CUDA inclusive polygon boundaries, one-nanodegree distinctions, stable rejection and expected domain exits\n";
}

void projection_qualification() {
    // Many compass directions, radial <=1000m; both hemispheres, equator and
    // high latitude. Oracle uses ECEF tangent inversion, never GPU equations.
    for (const std::int64_t latitude : std::vector<std::int64_t>{0, 45 * kDegree, -45 * kDegree,
                                                                   69980000000, -69980000000}) {
        std::vector<Actor> actors;
        std::vector<Anchor> anchors;
        for (unsigned bearing = 0; bearing < 360; bearing += 15) {
            const double angle = bearing * kPi / 180.0;
            const auto vx = static_cast<std::int32_t>(std::trunc(100000 * std::cos(angle)));
            const auto vy = static_cast<std::int32_t>(std::trunc(100000 * std::sin(angle)));
            actors.push_back({bearing + 1, ActorCategory::ClassA, 700000000, -300000000, vx, vy, 1, 0});
            anchors.push_back({bearing + 1, {28 * kDegree, latitude}});
        }
        const auto south = std::max(-70 * kDegree, latitude - kDegree / 10);
        const auto north = std::min(70 * kDegree, latitude + kDegree / 10);
        const auto domain = rectangle(27 * kDegree, 29 * kDegree, south, north);
        Session session(domain, actors, anchors, {SeedMode::Fixed, 42}, options());
        const auto result = session.run(10, 1, 1);
        require(result.events.size() == actors.size(), "qualified compass projection unexpectedly rejected");
        for (std::size_t index = 0; index < actors.size(); ++index) {
            projection_check(result.events[index], anchors[index].tick_zero, actors[index], 1);
        }
    }
    for (const std::int64_t latitude : {70 * kDegree, -70 * kDegree}) {
        Actor toward_equator{1, ActorCategory::ClassA, 111, -222, 0,
            static_cast<std::int32_t>(latitude > 0 ? -100000 : 100000), 1, 0};
        const auto domain = latitude > 0 ? rectangle(27 * kDegree, 29 * kDegree, 69 * kDegree, 70 * kDegree)
                                         : rectangle(27 * kDegree, 29 * kDegree, -70 * kDegree, -69 * kDegree);
        Session session(domain, {toward_equator}, {{1, {28 * kDegree, latitude}}},
                        {SeedMode::Fixed, 42}, options());
        const auto result = session.run(10, 1, 1);
        require(result.events.size() == 1, "exact qualified latitude inward projection failed");
        projection_check(result.events[0], {28 * kDegree, latitude}, toward_equator, 1);
        auto away = toward_equator;
        away.vy_mm_per_s = -away.vy_mm_per_s;
        Session outside_qualification(domain, {away}, {{1, {28 * kDegree, latitude}}},
                                     {SeedMode::Fixed, 42}, options());
        bool rejected = false;
        try { (void)outside_qualification.run(1, 1, 1); }
        catch (const std::exception& error) {
            require(std::string_view(error.what()).size() > 5, "unsupported projected latitude lacks diagnostic");
            rejected = true;
        }
        require(rejected, "unsupported projected latitude silently skipped/clamped");
        ++invalid_cases;
    }
    for (const std::int64_t longitude : {180 * kDegree, -180 * kDegree}) {
        Actor inward{1, ActorCategory::ClassA, 123, -456,
            static_cast<std::int32_t>(longitude > 0 ? -100000 : 100000), 0, 1, 0};
        const auto domain = longitude > 0 ? rectangle(178 * kDegree, 180 * kDegree)
                                          : rectangle(-180 * kDegree, -178 * kDegree);
        Session session(domain, {inward}, {{1, {longitude, 42 * kDegree}}},
                        {SeedMode::Fixed, 42}, options());
        const auto result = session.run(10, 1, 1);
        require(result.events.size() == 1, "valid inward longitude-boundary projection failed");
        projection_check(result.events[0], {longitude, 42 * kDegree}, inward, 1);
        auto outward = inward;
        outward.vx_mm_per_s = -outward.vx_mm_per_s;
        Session unsupported(domain, {outward}, {{1, {longitude, 42 * kDegree}}},
                            {SeedMode::Fixed, 42}, options());
        bool rejected = false;
        try { (void)unsupported.run(1, 1, 1); }
        catch (const std::runtime_error& error) {
            require(std::string_view(error.what()).find("projection") != std::string_view::npos,
                    "longitude qualification failure lacks projection context");
            rejected = true;
        }
        require(rejected, "longitude exit silently wrapped/skipped/clamped");
        ++invalid_cases;
    }
    std::cout << "PASS actual CUDA WGS84 qualification: <=1000m, |latitude|<=70; independent ECEF inverse max error="
              << maximum_oracle_error_m << " m (budget0.3m)\n";
}

void placement_and_rng() {
    const auto domain = concave();
    std::vector<Actor> actors;
    for (unsigned index = 0; index < 512; ++index) actors.push_back(stationary(index * 17 + 3));
    auto settings = options(4096);
    settings.anchor_mode = AnchorMode::CudaPlacement;
    settings.placement_attempts = 4096;
    Session reference(domain, actors, {}, {SeedMode::Fixed, 42}, settings);
    const auto baseline = reference.run(0, 1, 1);
    metrics_check(baseline, actors.size(), 1, actors.size());
    require(baseline.events.size() == actors.size(), "CUDA placement did not create one accepted anchor per actor");
    require(reference.setup_metrics().placement_ms > 0, "placement stage did not record actual device execution");
    for (const auto& event : baseline.events) require(inside(domain, event.location), "placement fails independent winding oracle");
    require(same_events(reference.run(0, 1, 1).events, baseline.events), "placement fixed seed replay");
    const auto first_ticks = reference.run(0, 2, 1).events;
    auto split = reference.run(0, 1, 1).events;
    const auto remainder = reference.run(1, 1, 1).events;
    split.insert(split.end(), remainder.begin(), remainder.end());
    require(same_events(first_ticks, split), "placement/session state depends on absolute batch boundaries");
    std::reverse(actors.begin(), actors.end());
    for (const unsigned block : {64u, 128u, 256u}) {
        settings.block_size = block;
        Session reordered(domain, actors, {}, {SeedMode::Fixed, 42}, settings);
        require(same_events(reordered.run(0, 1, 1).events, baseline.events),
                "placement streams depend on input actor order/launch shape");
    }
    Session different(domain, actors, {}, {SeedMode::Fixed, 43}, settings);
    require(!same_events(different.run(0, 1, 1).events, baseline.events), "different placement seed did not diverge");
    const auto fresh = select_seed(SeedMode::Fresh);
    Session fresh_session(domain, actors, {}, fresh, settings);
    Session replay(domain, actors, {}, {SeedMode::Fixed, fresh.value}, settings);
    require(same_events(fresh_session.run(0, 1, 1).events, replay.run(0, 1, 1).events),
            "reported OS fresh seed cannot replay exact CUDA bytes in fixed mode");

#ifdef AISMIXER_LAB_GEO_TESTING
    const std::vector<std::uint32_t> ids{0, UINT32_MAX, 7, 42, 1000};
    std::vector<std::vector<std::uint32_t>> purpose_words;
    for (const auto purpose : {RngPurpose::Placement, RngPurpose::Motion, RngPurpose::Atmosphere,
                               RngPurpose::ReceiverFading, RngPurpose::Failure}) {
        const auto baseline_words = diagnostic_random_words(ids, UINT64_MAX, purpose, 123, 64);
        require(baseline_words.size() == ids.size(), "device random diagnostics lost actor association");
        for (std::size_t index = 0; index < ids.size(); ++index) {
            const auto subsequence = (UINT64_C(1) << 56) |
                (static_cast<std::uint64_t>(purpose) << 48) | ids[index];
            require(baseline_words[index] ==
                        test_reference::curand_qualified_word(UINT64_MAX, subsequence, 123),
                    "device purpose stream differs from independent integer Philox reference");
        }
        for (unsigned block : {128u, 256u}) {
            require(diagnostic_random_words(ids, UINT64_MAX, purpose, 123, block) == baseline_words,
                    "random semantic stream depends on launch shape");
        }
        auto reversed_ids = ids;
        std::reverse(reversed_ids.begin(), reversed_ids.end());
        auto reordered = diagnostic_random_words(reversed_ids, UINT64_MAX, purpose, 123, 256);
        std::reverse(reordered.begin(), reordered.end());
        require(reordered == baseline_words, "random stream depends on input position, not stable actor ID");
        require(diagnostic_random_words(ids, UINT64_MAX, purpose, 124, 64) != baseline_words,
                "random semantic stream ignores absolute tick");
        purpose_words.push_back(baseline_words);
    }
    for (std::size_t first = 0; first < purpose_words.size(); ++first) {
        for (std::size_t second = first + 1; second < purpose_words.size(); ++second) {
            require(purpose_words[first] != purpose_words[second], "RNG purposes share one stream");
        }
    }
    invalid([&] { (void)diagnostic_random_words(ids, 42, RngPurpose::Placement, 0, 32); });
    invalid([&] { (void)diagnostic_random_words(ids, 42, static_cast<RngPurpose>(0), 0, 64); });
#else
    throw std::runtime_error("GPU test target lacks AISMIXER_LAB_GEO_TESTING random-purpose hook");
#endif
    std::cout << "PASS actual CUDA Philox polygon placement:512 actors, repeat/reorder/launch/fresh replay; five isolated semantic purposes\n";
}

struct ReferencePlacement { Point point; unsigned attempts; };
ReferencePlacement reference_placement(const Domain& domain, std::uint64_t seed,
                                        std::uint32_t actor_id) {
    Point minimum = domain.ring.front();
    Point maximum = minimum;
    for (const auto point : domain.ring) {
        minimum.longitude_ndeg = std::min(minimum.longitude_ndeg, point.longitude_ndeg);
        minimum.latitude_ndeg = std::min(minimum.latitude_ndeg, point.latitude_ndeg);
        maximum.longitude_ndeg = std::max(maximum.longitude_ndeg, point.longitude_ndeg);
        maximum.latitude_ndeg = std::max(maximum.latitude_ndeg, point.latitude_ndeg);
    }
    // The purpose/version key is reconstructed here, not obtained from the
    // production helper. Polygon acceptance uses the independent winding rule.
    const auto subsequence = (UINT64_C(1) << 56) | (UINT64_C(1) << 48) | actor_id;
    test_reference::PhiloxWords random(seed, subsequence);
    for (unsigned attempt = 1; attempt <= 4096; ++attempt) {
        const Point proposal{
            test_reference::bounding_integer(random, minimum.longitude_ndeg, maximum.longitude_ndeg),
            test_reference::bounding_integer(random, minimum.latitude_ndeg, maximum.latitude_ndeg)};
        if (inside(domain, proposal)) return {proposal, attempt};
    }
    throw std::runtime_error("independent placement oracle exhausted its bounded attempts");
}

void independent_philox_cuda() {
    using test_reference::curand_qualified_word;
    const std::vector<std::uint32_t> ids{0, 3, 17, 701, 902, UINT32_MAX};
    const std::vector<std::uint64_t> seeds{42, 43, 123456789};
    // Fully integer proposals in the synthetic L polygon. Values were derived
    // independently from Philox blocks/key/counter arithmetic and winding,
    // not recorded from CUDA execution or loaded from ignored audit artifacts.
    const std::vector<std::vector<ReferencePlacement>> pinned{
        {{{28714846157, 41250123442}, 1}, {{27198881751, 42138886443}, 1},
         {{28840241907, 41764587369}, 1}, {{27804029332, 42300889357}, 1},
         {{28630623490, 41534744845}, 1}, {{27703812889, 41900895033}, 1}},
        {{{27744866529, 42682415771}, 2}, {{27646754186, 41795185536}, 1},
         {{27756281902, 41823475693}, 2}, {{27672632435, 41912035439}, 1},
         {{27073144911, 41125834124}, 1}, {{27755298676, 42875270454}, 1}},
        {{{28352099796, 41831326681}, 2}, {{27939527529, 42779233235}, 1},
         {{27992154354, 42433874408}, 2}, {{28469997216, 41307781426}, 1},
         {{28960353209, 41512762309}, 2}, {{28364297880, 41142458193}, 1}}};
    std::vector<Actor> actors;
    for (const auto id : ids) actors.push_back(stationary(id));
    auto reversed = actors;
    std::reverse(reversed.begin(), reversed.end());
    auto settings = options(64);
    settings.anchor_mode = AnchorMode::CudaPlacement;
    unsigned rejected_polygon_proposals = 0;
    for (std::size_t seed_index = 0; seed_index < seeds.size(); ++seed_index) {
        const auto seed = seeds[seed_index];
        Session session(concave(), reversed, {}, {SeedMode::Fixed, seed}, settings);
        const auto actual = session.run(0, 1, 1).events;
        require(actual.size() == ids.size(), "independent placement fixture lost an actor");
        for (std::size_t index = 0; index < ids.size(); ++index) {
            const auto reference = reference_placement(concave(), seed, ids[index]);
            require(same_point(reference.point, pinned[seed_index][index].point) &&
                        reference.attempts == pinned[seed_index][index].attempts,
                    "integer Philox/winding placement oracle disagrees with pinned synthetic vector");
            require(actual[index].actor_id == ids[index] && same_point(actual[index].location, reference.point),
                    "actual CUDA placement differs from independent integer Philox proposals");
            rejected_polygon_proposals += reference.attempts - 1;
        }
        for (const auto tick : {UINT64_C(0), UINT64_C(2), UINT64_C(3), UINT64_C(4),
                                UINT64_C(200), kMaxAbsoluteTick}) {
            const auto legacy = generate_event_intents(actors, seed, tick, 1, 1);
            for (const auto& event : legacy) {
                require(event.random_u32 == curand_qualified_word(seed, event.actor_id, tick),
                        "original B2 GPU Philox word differs from independent seed/subsequence/offset reference");
            }
        }
    }
    require(rejected_polygon_proposals == 5, "placement vectors no longer exercise polygon rejection/retry");
    std::cout << "PASS independent integer Philox vs actual CUDA:18 pinned anchors/5 polygon retries,108 original B2 words,25 purpose words\n";
}

void mixed_large_compaction() {
    // Unique per-actor longitude anchors at the equator make the declared
    // tangent longitude model independently calculable without a trig/library
    // approximation. Separate ECEF qualification above tests the geodetic model.
    // 5000 actors x200 absolute ticks =1M candidates, interval1/2/3/4 schedules,
    // stationary inside/outside groups and bidirectional boundary crossers.
    const auto domain = rectangle(0, 1000000, -1000000, 1000000);
    constexpr std::int64_t initial_longitude[] =
        {100000, 200000, -200000, 800000, 1200000, 500000, 1200000, 200000};
    constexpr std::int32_t velocities[] = {0, 0, 1000, 1000, 0, 0, -1000, -1000};
    std::vector<Actor> actors;
    std::vector<Anchor> anchors;
    for (std::uint32_t index = 0; index < 5000; ++index) {
        const auto interval = index % 4 + 1;
        const auto id = 100000 + 3 * index;
        const auto velocity = index % 16 == 12 ? -200 : velocities[index % 8];
        actors.push_back({id, index % 2 ? ActorCategory::ClassB : ActorCategory::ClassA,
                          31 * static_cast<std::int64_t>(index) - 70000,
                          20000 - 17 * static_cast<std::int64_t>(index), velocity, 0,
                          interval, (index / 4) % interval});
        anchors.push_back({id, {initial_longitude[index % 8] + index, 0}});
    }
    const double nanodegrees_per_mm = (180.0 / kPi) * 1e6 / kA;
    std::vector<GeoEvent> expected;
    std::uint64_t scheduled = 0;
    std::vector<bool> was_inside(actors.size());
    std::vector<bool> seen(actors.size());
    std::vector<bool> crossed(actors.size());
    for (std::uint64_t tick = 0; tick < 200; ++tick) {
        for (std::size_t index = 0; index < actors.size(); ++index) {
            const auto& actor = actors[index];
            if (tick % actor.report_interval_ticks != actor.phase_ticks) continue;
            ++scheduled;
            const auto east = static_cast<std::int64_t>(tick) * actor.vx_mm_per_s;
            const Point location{
                static_cast<std::int64_t>(std::llround(static_cast<double>(anchors[index].tick_zero.longitude_ndeg) +
                                                       static_cast<double>(east) * nanodegrees_per_mm)), 0};
            const bool accepted = inside(domain, location);
            if (seen[index] && accepted != was_inside[index]) crossed[index] = true;
            seen[index] = true; was_inside[index] = accepted;
            if (!accepted) continue;
            expected.push_back({tick, tick, actor.actor_id, actor.category, actor.x_mm + east,
                actor.y_mm, location, test_reference::curand_qualified_word(7, actor.actor_id, tick)});
        }
    }
    require(scheduled == 520834 && expected.size() == 330521 &&
                scheduled - expected.size() == 190313 && 1000000 - scheduled == 479166 &&
                std::count(crossed.begin(), crossed.end(), true) == 2812,
            "independently calculated mixed fixture cardinality/crossing pin changed");
    const auto reference_hash = independent_digest(expected);
    // Independently computed by a separate standard-library Python integer
    // Philox/struct.pack/FNV calculation before any CUDA fixture execution.
    require(reference_hash == UINT64_C(0xAC9B635E46CEFD1A),
            "mixed CPU oracle differs from independently pinned canonical digest");
    std::reverse(actors.begin(), actors.end());
    std::reverse(anchors.begin(), anchors.end());
    Session session(domain, actors, anchors, {SeedMode::Fixed, 7}, options(1000000));
    const auto actual = session.run(0, 200, 1);
    metrics_check(actual, 5000, 200, scheduled);
    require(same_events(actual.events, expected), "large mixed stable CUDA selection differs from independent schedule/membership/Philox oracle");
    require(event_digest(actual.events) == reference_hash && independent_digest(actual.events) == reference_hash,
            "mixed CUDA result digest differs from independently serialized60-byte reference");
    require(actual.metrics.rejected == 190313 && actual.metrics.unscheduled == 479166 &&
                actual.metrics.d2h_record_bytes == UINT64_C(330521) * 64 &&
                actual.metrics.d2h_record_bytes < actual.metrics.uncompacted_record_bytes,
            "mixed counter conservation or compact-only D2H byte count");
    for (const auto& event : actual.events) require(inside(domain, event.location), "mixed GPU accepted an outside point");
    require(same_events(session.run(0, 200, 1).events, expected), "large mixed compaction replay changed bytes");
    auto split = session.run(0, 73, 1).events;
    const auto rest = session.run(73, 127, 1).events;
    split.insert(split.end(), rest.begin(), rest.end());
    require(same_events(split, expected), "large mixed absolute-tick split batches changed selection/order");
    std::cout << "PASS actual CUDA mixed1M:scheduled520834 accepted330521 rejected190313 unscheduled479166 crossers2812"
              << " compact_D2H_bytes=" << actual.metrics.d2h_record_bytes << " FNV=" << std::hex << reference_hash << std::dec << '\n';
}

void resource_and_large_batch() {
    const auto actor = stationary(1);
    for (const unsigned block : {0u, 32u, 512u}) {
        invalid([&] { Session bad(rectangle(), {actor}, {{1, {28 * kDegree, 42 * kDegree}}},
                                 {SeedMode::Fixed, 42}, options(1, block)); });
    }
    auto bad_settings = options(1);
    bad_settings.anchor_mode = static_cast<AnchorMode>(99);
    invalid([&] { Session bad(rectangle(), {actor}, {}, {SeedMode::Fixed, 42}, bad_settings); });
    const Domain skinny{1, "bounded-placement-exhaustion-v1", "Synthetic thin valid triangle",
        {{0, 0}, {kDegree, kDegree}, {kDegree, kDegree + 1}, {0, 0}}};
    std::vector<Actor> unlucky;
    for (std::uint32_t id = 0; id < 64; ++id) unlucky.push_back(stationary(id));
    auto exhausted_settings = options(64);
    exhausted_settings.anchor_mode = AnchorMode::CudaPlacement;
    exhausted_settings.placement_attempts = 1;
    bool exhausted = false;
    try { Session fails(skinny, unlucky, {}, {SeedMode::Fixed, 42}, exhausted_settings); }
    catch (const std::runtime_error& error) {
        require(std::string_view(error.what()).find("exhausted") != std::string_view::npos,
                "bounded placement failure was not identified as exhausted sampling budget");
        exhausted = true;
    }
    require(exhausted, "qualified fixed-seed skinny-domain placement did not expose budget exhaustion");
    ++invalid_cases;
    bad_settings.anchor_mode = AnchorMode::CudaPlacement;
    bad_settings.placement_attempts = 0;
    invalid([&] { Session bad(rectangle(), {actor}, {}, {SeedMode::Fixed, 42}, bad_settings); });
    Session valid(rectangle(), {actor}, {{1, {28 * kDegree, 42 * kDegree}}},
                  {SeedMode::Fixed, 42}, options(1));
    invalid([&] { (void)valid.run(0, 2, 1); });
    invalid([&] { (void)valid.run(UINT64_MAX, UINT64_MAX, UINT32_MAX); });
    invalid([&] { (void)valid.run(0, 1, 0); });
    Session moved(std::move(valid));
    require(moved.run(0, 1, 1).events.size() == 1, "moved Session lost device ownership");

    std::vector<Actor> actors;
    std::vector<Anchor> anchors;
    for (std::uint32_t index = 0; index < 10000; ++index) {
        actors.push_back(stationary(100000 + index, index % 2 ? ActorCategory::ClassB : ActorCategory::ClassA));
        anchors.push_back({100000 + index, {28 * kDegree, 42 * kDegree}});
    }
    Session maximum(rectangle(), actors, anchors, {SeedMode::Fixed, 42}, options(1000000));
    const auto result = maximum.run(0, 100, 1);
    metrics_check(result, 10000, 100, 1000000);
    require(result.events.size() == 1000000 && result.metrics.rejected == 0,
            "one-million candidate maximum cardinality");
    require(result.metrics.device_bytes > 0 && result.metrics.device_bytes < UINT64_C(512) * 1024 * 1024,
            "estimated device buffers do not fit declared Pascal memory plan");
    for (std::size_t index = 0; index < result.events.size(); ++index) {
        const auto& event = result.events[index];
        require(event.absolute_tick == index / 10000 && event.actor_id == 100000 + index % 10000 &&
                    same_point(event.location, {28 * kDegree, 42 * kDegree}),
                "maximum batch stable ordering or stationary placement");
    }
    std::cout << "PASS actual CUDA maximum1000000 candidates/accepted, compact D2H="
              << result.metrics.d2h_record_bytes << " bytes, device owned=" << result.metrics.device_bytes << " bytes\n";
    std::cout << "PASS genuine CUDA bounded rejection-sampling exhaustion, no partial placement success\n";
}

int unavailable_test() {
    bool failed = false;
    try {
        Session session(rectangle(), {stationary(1)}, {{1, {28 * kDegree, 42 * kDegree}}},
                        {SeedMode::Fixed, 42}, options(1));
        (void)session.run(0, 1, 1);
    } catch (const std::runtime_error& error) {
        const std::string_view diagnostic(error.what());
        const bool zero_count = diagnostic == "No CUDA device is available for geospatial execution";
        const bool cuda_no_device = diagnostic.find("cudaGetDeviceCount: cudaErrorNoDevice (") == 0;
        require(zero_count || cuda_no_device,
                "GPU-hidden test received a different runtime failure, not an explicit no-device result");
        failed = true;
        std::cout << "EXPECTED GPU-unavailable rejection: " << error.what() << '\n';
    }
    require(failed, "--expect-no-gpu unexpectedly ran a device; set CUDA_VISIBLE_DEVICES=-1");
    return 0;
}
}  // namespace

int main(int argc, char** argv) {
    try {
        if (argc == 2 && std::string_view(argv[1]) == "--expect-no-gpu") return unavailable_test();
        require(argc == 1, "usage: geo_gpu_tests [--expect-no-gpu]");
        {
            Session probe(rectangle(), {}, {}, {SeedMode::Fixed, 42}, options(1));
            const auto info = probe.device_info();
            std::cout << "Actual CUDA device: " << info.name << " sm_" << info.major << info.minor
                      << " runtime=" << info.runtime_version << " driver=" << info.driver_version << '\n';
        }
        deterministic_motion();
        boundaries_and_filtering();
        projection_qualification();
        placement_and_rng();
        independent_philox_cuda();
        resource_and_large_batch();
        mixed_large_compaction();
        std::cout << "PASS " << invalid_cases << " invalid/unsupported GPU geography cases; all normal paths executed CUDA\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL geo GPU (no CPU fallback): " << error.what() << '\n';
        return 1;
    }
}
