#include "scene_api.h"

#ifdef AISMIXER_LAB_SCENE_TESTING
#include "scene_test_api.h"
#endif

#include <algorithm>
#include <array>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace {

using aismixer::lab::Actor;
using aismixer::lab::ActorCategory;
using aismixer::lab::EventIntent;
using aismixer::lab::generate_event_intents;

enum class Mode { Demo, SelfTest, Help };

void print_help() {
    std::cout
        << "Usage: aismixer_lab_scene_smoke [--demo | --self-test | --help]\n\n"
        << "  --demo       Print the fixed scene as CSV (default with no options).\n"
        << "  --self-test  Run scene oracles, determinism and bounds checks on the GPU.\n"
        << "  --help       Show help without accessing CUDA.\n\n"
        << "Modes are mutually exclusive; duplicate and unknown options are invalid.\n"
        << "Demo: seed=42, start_tick=0, tick_count=8, tick_seconds=2.\n"
        << "CSV columns: absolute_tick,actor_id,category,x_mm,y_mm,random_u32\n"
        << "Category codes: ClassA=0, ClassB=1, BaseStation=2, AtoN=3, SAR=4, Emergency=5.\n"
        << "Count, parameters and FNV-1a-64 digest go to stderr; stdout is CSV only.\n"
        << "Digest: each ordered record's tick (8 bytes), actor ID (4), category (4),\n"
        << "x (8), y (8), random word (4), explicitly little endian (36 bytes total).\n"
        << "Signed coordinates are serialized modulo 2^64; no object padding is used.\n";
}

Mode parse_options(int argc, char** argv) {
    Mode mode = Mode::Demo;
    bool mode_set = false;
    for (int i = 1; i < argc; ++i) {
        const std::string_view argument(argv[i]);
        Mode selected;
        if (argument == "--demo") {
            selected = Mode::Demo;
        } else if (argument == "--self-test") {
            selected = Mode::SelfTest;
        } else if (argument == "--help") {
            selected = Mode::Help;
        } else {
            throw std::invalid_argument("unknown option: " + std::string(argument));
        }
        if (mode_set) {
            throw std::invalid_argument("duplicate or conflicting mode: " + std::string(argument));
        }
        mode = selected;
        mode_set = true;
    }
    return mode;
}

void require(bool condition, std::string_view message) {
    if (!condition) {
        throw std::runtime_error("scene self-test validation failed: " + std::string(message));
    }
}

bool canonical_less(const EventIntent& left, const EventIntent& right) {
    return left.absolute_tick < right.absolute_tick
        || (left.absolute_tick == right.absolute_tick && left.actor_id < right.actor_id);
}

void validate_order(const std::vector<EventIntent>& events) {
    for (std::size_t i = 1; i < events.size(); ++i) {
        require(canonical_less(events[i - 1], events[i]), "events are not strictly canonical");
    }
}

// Convert integer fields explicitly, never object memory or host endian bytes.
std::array<std::uint8_t, 36> record_bytes(const EventIntent& event) {
    std::array<std::uint8_t, 36> bytes{};
    std::size_t offset = 0;
    const auto append = [&](std::uint64_t word, unsigned width) {
        for (unsigned i = 0; i < width; ++i) {
            bytes[offset++] = static_cast<std::uint8_t>(word & UINT64_C(0xff));
            word >>= 8;
        }
    };
    append(event.absolute_tick, 8);
    append(event.actor_id, 4);
    append(static_cast<std::uint32_t>(event.category), 4);
    append(static_cast<std::uint64_t>(event.x_mm), 8);
    append(static_cast<std::uint64_t>(event.y_mm), 8);
    append(event.random_u32, 4);
    return bytes;
}

std::uint64_t digest(const std::vector<EventIntent>& events) {
    std::uint64_t hash = UINT64_C(14695981039346656037);
    for (const auto& event : events) {
        for (const auto byte : record_bytes(event)) {
            hash ^= byte;
            hash *= UINT64_C(1099511628211);
        }
    }
    return hash;
}

void print_digest(std::ostream& output, const std::vector<EventIntent>& events) {
    output << "digest_fnv1a64=" << std::hex << std::setfill('0')
        << std::setw(16) << digest(events) << std::dec << std::setfill(' ') << '\n';
}

bool same_records(const std::vector<EventIntent>& left, const std::vector<EventIntent>& right) {
    return left.size() == right.size()
        && std::equal(left.begin(), left.end(), right.begin(),
            [](const EventIntent& a, const EventIntent& b) { return record_bytes(a) == record_bytes(b); });
}

bool same_deterministic_fields(const EventIntent& left, const EventIntent& right) {
    return left.absolute_tick == right.absolute_tick && left.actor_id == right.actor_id
        && left.category == right.category && left.x_mm == right.x_mm && left.y_mm == right.y_mm;
}

bool random_words_differ(const std::vector<EventIntent>& left, const std::vector<EventIntent>& right) {
    require(left.size() == right.size(), "seed comparison changed event count");
    bool changed = false;
    for (std::size_t i = 0; i < left.size(); ++i) {
        require(same_deterministic_fields(left[i], right[i]), "seed changed schedule or position");
        changed = changed || left[i].random_u32 != right[i].random_u32;
    }
    return changed;
}

std::vector<Actor> demo_scene() {
    // Intentionally unsorted. IDs are scenario identities, not MMSI or TAG sources.
    return {
        {60, ActorCategory::Emergency, -100, 200, 100, -50, 1, 0},
        {30, ActorCategory::BaseStation, 7000, -8000, 0, 0, 3, 1},
        {10, ActorCategory::ClassA, 1000, -2000, 250, -100, 2, 0},
        {50, ActorCategory::SAR, 0, 100, -300, 500, 2, 1},
        {20, ActorCategory::ClassB, -500, 600, -50, 25, 3, 2},
        {40, ActorCategory::AtoN, -9000, 10000, 0, 0, 4, 0},
    };
}

void validate_demo_oracle(const std::vector<EventIntent>& events) {
    // Handwritten schedules/coordinates for ticks 0..7 at two seconds per tick.
    // The oracle deliberately does not repeat the kernel's modulo/motion code.
    const std::vector<EventIntent> expected{
        {0, 10, ActorCategory::ClassA, 1000, -2000, 0},
        {0, 40, ActorCategory::AtoN, -9000, 10000, 0},
        {0, 60, ActorCategory::Emergency, -100, 200, 0},
        {1, 30, ActorCategory::BaseStation, 7000, -8000, 0},
        {1, 50, ActorCategory::SAR, -600, 1100, 0},
        {1, 60, ActorCategory::Emergency, 100, 100, 0},
        {2, 10, ActorCategory::ClassA, 2000, -2400, 0},
        {2, 20, ActorCategory::ClassB, -700, 700, 0},
        {2, 60, ActorCategory::Emergency, 300, 0, 0},
        {3, 50, ActorCategory::SAR, -1800, 3100, 0},
        {3, 60, ActorCategory::Emergency, 500, -100, 0},
        {4, 10, ActorCategory::ClassA, 3000, -2800, 0},
        {4, 30, ActorCategory::BaseStation, 7000, -8000, 0},
        {4, 40, ActorCategory::AtoN, -9000, 10000, 0},
        {4, 60, ActorCategory::Emergency, 700, -200, 0},
        {5, 20, ActorCategory::ClassB, -1000, 850, 0},
        {5, 50, ActorCategory::SAR, -3000, 5100, 0},
        {5, 60, ActorCategory::Emergency, 900, -300, 0},
        {6, 10, ActorCategory::ClassA, 4000, -3200, 0},
        {6, 60, ActorCategory::Emergency, 1100, -400, 0},
        {7, 30, ActorCategory::BaseStation, 7000, -8000, 0},
        {7, 50, ActorCategory::SAR, -4200, 7100, 0},
        {7, 60, ActorCategory::Emergency, 1300, -500, 0},
    };
    require(events.size() == expected.size(), "demo oracle event count differs");
    require(std::equal(events.begin(), events.end(), expected.begin(), same_deterministic_fields),
        "demo oracle schedule/category/coordinate mismatch");
    validate_order(events);
}

void demo() {
    const auto events = generate_event_intents(demo_scene(), 42, 0, 8, 2);
    validate_demo_oracle(events);
    std::cout << "absolute_tick,actor_id,category,x_mm,y_mm,random_u32\n";
    for (const auto& event : events) {
        std::cout << event.absolute_tick << ',' << event.actor_id << ','
            << static_cast<std::uint32_t>(event.category) << ',' << event.x_mm << ','
            << event.y_mm << ',' << event.random_u32 << '\n';
    }
    std::cerr << "seed=42 start_tick=0 tick_count=8 tick_seconds=2 event_count=" << events.size() << '\n';
    print_digest(std::cerr, events);
}

void validate_split(const std::vector<Actor>& actors, const std::vector<EventIntent>& reference,
                    const std::vector<std::pair<std::uint64_t, std::uint64_t>>& ranges) {
    std::vector<EventIntent> joined;
    for (const auto& range : ranges) {
        const auto part = generate_event_intents(actors, 42, range.first, range.second, 2);
        validate_order(part);
        joined.insert(joined.end(), part.begin(), part.end());
    }
    std::sort(joined.begin(), joined.end(), canonical_less);
    require(same_records(reference, joined), "split ranges changed canonical record bytes");
    require(digest(reference) == digest(joined), "split ranges changed digest");
}

void validate_random_key(const std::vector<Actor>& actors, const std::vector<EventIntent>& reference) {
    std::vector<EventIntent> sar_reference;
    for (const auto& event : reference) {
        if (event.actor_id == 50) {
            sar_reference.push_back(event);
        }
    }
    const auto sar = *std::find_if(actors.begin(), actors.end(),
        [](const Actor& actor) { return actor.actor_id == 50; });
    const auto alone = generate_event_intents({sar}, 42, 0, 8, 2);
    require(same_records(sar_reference, alone), "actor membership changed keyed event words");

    auto changed = sar;
    changed.category = ActorCategory::Emergency;
    changed.x_mm += 1000;
    changed.y_mm -= 1000;
    changed.vx_mm_per_s += 1;
    changed.vy_mm_per_s -= 1;
    changed.report_interval_ticks = 1;
    changed.phase_ticks = 0;
    const auto changed_events = generate_event_intents({changed}, 42, 0, 8, 2);
    require(changed_events.size() == 8, "changed schedule did not emit every tick");
    for (const auto& event : alone) {
        require(event.random_u32 == changed_events[event.absolute_tick].random_u32,
            "non-key category/motion/schedule fields changed a keyed PRNG word");
    }
    require(std::any_of(alone.begin() + 1, alone.end(),
        [&](const EventIntent& event) { return event.random_u32 != alone[0].random_u32; }),
        "selected absolute ticks did not change PRNG words");
    require(reference.size() >= 3 && reference[0].absolute_tick == 0
        && reference[1].absolute_tick == 0 && reference[2].absolute_tick == 0,
        "missing same-tick actor key samples");
    require(reference[0].random_u32 != reference[1].random_u32
        || reference[0].random_u32 != reference[2].random_u32,
        "selected actor IDs did not change PRNG words");
    std::cout << "PASS seed/actor ID/absolute tick key and membership/non-key field independence\n";
}

void validate_edges() {
    using namespace aismixer::lab;
    const std::vector<Actor> one{{0, ActorCategory::ClassB, -25, 75, -3, 7, 1, 0}};
    require(generate_event_intents({}, 42, 0, 8, 2).empty(), "empty scene emitted events");
    require(generate_event_intents(one, 42, 0, 0, 2).empty(), "zero ticks emitted events");
    require(generate_event_intents({}, 42, kMaxAbsoluteTick, 0, 60).empty(),
        "empty final-tick zero-range request failed");
    const auto single = generate_event_intents(one, 42, 0, 1, 2);
    require(single.size() == 1 && same_deterministic_fields(single[0],
        EventIntent{0, 0, ActorCategory::ClassB, -25, 75, 0}), "one actor at tick zero mismatch");
    auto inactive = one;
    inactive[0].report_interval_ticks = 2;
    inactive[0].phase_ticks = 1;
    require(generate_event_intents(inactive, 42, 0, 1, 2).empty(),
        "inactive phase emitted an event");

    // Nonzero start uses absolute tick time, and both uint32 ID endpoints are valid.
    auto high_id = one[0];
    high_id.actor_id = std::numeric_limits<std::uint32_t>::max();
    const auto id_endpoints = generate_event_intents({high_id, one[0]}, 42, 5, 3, 2);
    const std::vector<EventIntent> endpoint_oracle{
        {5, 0, ActorCategory::ClassB, -55, 145, 0},
        {5, std::numeric_limits<std::uint32_t>::max(), ActorCategory::ClassB, -55, 145, 0},
        {6, 0, ActorCategory::ClassB, -61, 159, 0},
        {6, std::numeric_limits<std::uint32_t>::max(), ActorCategory::ClassB, -61, 159, 0},
        {7, 0, ActorCategory::ClassB, -67, 173, 0},
        {7, std::numeric_limits<std::uint32_t>::max(), ActorCategory::ClassB, -67, 173, 0},
    };
    require(id_endpoints.size() == endpoint_oracle.size()
        && std::equal(id_endpoints.begin(), id_endpoints.end(), endpoint_oracle.begin(),
            same_deterministic_fields), "ID endpoint ordering/nonzero-start motion mismatch");
    validate_order(id_endpoints);

    const auto long_range = generate_event_intents(one, 42, 0, kMaxSceneTicks, 2);
    require(long_range.size() == kMaxSceneTicks, "maximum tick range count mismatch");
    require(same_deterministic_fields(long_range.back(),
        EventIntent{3599, 0, ActorCategory::ClassB, -21619, 50461, 0}),
        "maximum tick range endpoint mismatch");
    validate_order(long_range);

    const std::vector<Actor> extremes{{std::numeric_limits<std::uint32_t>::max(),
        ActorCategory::ClassA, kMaxInitialPositionMm, -kMaxInitialPositionMm,
        kMaxVelocityMmPerS, -kMaxVelocityMmPerS, 1, 0}};
    const auto final_tick = generate_event_intents(extremes, 42, kMaxAbsoluteTick, 1, kMaxTickSeconds);
    require(final_tick.size() == 1 && same_deterministic_fields(final_tick[0],
        EventIntent{1000000000, std::numeric_limits<std::uint32_t>::max(), ActorCategory::ClassA,
            INT64_C(6001000000000000), -INT64_C(6001000000000000), 0}),
        "maximum absolute tick/time/position/velocity arithmetic mismatch");

    // The admitted maximum has all one million candidates active.
    std::vector<Actor> full_scene;
    full_scene.reserve(kMaxSceneActors);
    for (std::uint32_t id = 0; id < kMaxSceneActors; ++id) {
        full_scene.push_back({id, ActorCategory::ClassA, id, -static_cast<std::int64_t>(id), 1, -1, 1, 0});
    }
    const auto full = generate_event_intents(full_scene, 42, 0, 100, 2);
    require(full.size() == kMaxSceneCandidates, "maximum workload did not emit one million intents");
    for (std::size_t i = 0; i < full.size(); ++i) {
        const auto tick = static_cast<std::uint64_t>(i / 10000);
        const auto id = static_cast<std::uint32_t>(i % 10000);
        const auto position = static_cast<std::int64_t>(id + 2 * tick);
        require(same_deterministic_fields(full[i],
            EventIntent{tick, id, ActorCategory::ClassA, position, -position, 0}),
            "maximum workload canonical schedule/position mismatch");
    }
    std::cout << "PASS empty/zero/one actor, maximum ticks, signed arithmetic and maximum workload\n";
}

void validate_invalid_inputs() {
    using namespace aismixer::lab;
    const std::vector<Actor> one{{1, ActorCategory::ClassA, 0, 0, 1, -1, 1, 0}};
    std::size_t rejected = 0;
    const auto expect_invalid = [&](std::string_view name, const std::vector<Actor>& actors,
                                   std::uint64_t start, std::uint64_t count, std::uint32_t seconds) {
        bool invalid = false;
        try {
            (void)generate_event_intents(actors, 42, start, count, seconds);
        } catch (const std::invalid_argument&) {
            invalid = true;
        }
        require(invalid, "accepted invalid input: " + std::string(name));
        ++rejected;
    };
    auto bad = one;
    bad.push_back(one[0]);
    expect_invalid("duplicate actor IDs", bad, 0, 1, 1);
    bad = one;
    bad[0].category = static_cast<ActorCategory>(6);
    expect_invalid("invalid category", bad, 0, 1, 1);
    bad = one;
    bad[0].report_interval_ticks = 0;
    expect_invalid("zero report interval", bad, 0, 1, 1);
    expect_invalid("zero report interval with zero ticks", bad, 0, 0, 1);
    bad = one;
    bad[0].phase_ticks = 1;
    expect_invalid("phase equals interval", bad, 0, 1, 1);
    bad[0].phase_ticks = std::numeric_limits<std::uint32_t>::max();
    expect_invalid("phase exceeds interval", bad, 0, 1, 1);
    for (const auto category : {ActorCategory::BaseStation, ActorCategory::AtoN}) {
        bad = one;
        bad[0].category = category;
        bad[0].vy_mm_per_s = 0;
        expect_invalid("stationary category moving in x", bad, 0, 1, 1);
        bad[0].vx_mm_per_s = 0;
        bad[0].vy_mm_per_s = -1;
        expect_invalid("stationary category moving in y", bad, 0, 1, 1);
    }
    for (const auto position : {kMaxInitialPositionMm + 1, -kMaxInitialPositionMm - 1,
                               std::numeric_limits<std::int64_t>::min(),
                               std::numeric_limits<std::int64_t>::max()}) {
        bad = one;
        bad[0].x_mm = position;
        expect_invalid("x outside position bounds", bad, 0, 1, 1);
        bad[0].x_mm = 0;
        bad[0].y_mm = position;
        expect_invalid("y outside position bounds", bad, 0, 1, 1);
    }
    for (const auto velocity : {kMaxVelocityMmPerS + 1, -kMaxVelocityMmPerS - 1,
                               std::numeric_limits<std::int32_t>::min(),
                               std::numeric_limits<std::int32_t>::max()}) {
        bad = one;
        bad[0].vx_mm_per_s = velocity;
        expect_invalid("x velocity outside bounds", bad, 0, 1, 1);
        bad[0].vx_mm_per_s = 0;
        bad[0].vy_mm_per_s = velocity;
        expect_invalid("y velocity outside bounds", bad, 0, 1, 1);
    }
    expect_invalid("zero tick seconds", one, 0, 1, 0);
    expect_invalid("tick seconds above maximum", one, 0, 1, kMaxTickSeconds + 1);
    expect_invalid("ticks above maximum", one, 0, kMaxSceneTicks + 1, 1);
    expect_invalid("tick count UINT64_MAX", one, 0, std::numeric_limits<std::uint64_t>::max(), 1);
    expect_invalid("start tick UINT64_MAX", one, std::numeric_limits<std::uint64_t>::max(), 1, 1);
    expect_invalid("last included tick above maximum", one, kMaxAbsoluteTick, 2, 1);
    expect_invalid("empty scene invalid tick duration", {}, 0, 1, 0);
    expect_invalid("zero ticks invalid duration", one, 0, 0, 0);
    expect_invalid("empty scene invalid count", {}, 0, std::numeric_limits<std::uint64_t>::max(), 1);
    expect_invalid("empty zero range invalid start", {}, kMaxAbsoluteTick + 1, 0, 1);

    std::vector<Actor> many;
    many.reserve(kMaxSceneActors + 1);
    for (std::uint32_t id = 0; id <= kMaxSceneActors; ++id) {
        many.push_back({id, ActorCategory::ClassA, 0, 0, 0, 0, 1, 0});
    }
    expect_invalid("too many actors", many, 0, 1, 1);
    expect_invalid("too many actors with zero ticks", many, 0, 0, 1);
    many.pop_back();
    expect_invalid("candidate product above maximum", many, 0, 101, 1);
    expect_invalid("candidate product/tick count overflow", many, 0,
        std::numeric_limits<std::uint64_t>::max(), 1);
    std::cout << "PASS invalid scenario inputs rejected: " << rejected << '\n';
}

void validate_launch_shapes(const std::vector<Actor>& actors) {
#ifdef AISMIXER_LAB_SCENE_TESTING
    // 600 candidates cross each supported block boundary and leave a partial block.
    const auto reference = generate_event_intents(actors, 42, 17, 100, 2);
    for (const unsigned block_size : {64u, 128u, 256u}) {
        const auto shaped = aismixer::lab::testing::generate_event_intents_with_block_size(
            actors, 42, 17, 100, 2, block_size);
        require(same_records(reference, shaped) && digest(reference) == digest(shaped),
            "test-only launch shape changed canonical records/digest");
    }
    for (const unsigned block_size : {0u, 63u, 512u}) {
        bool rejected = false;
        try {
            (void)aismixer::lab::testing::generate_event_intents_with_block_size(
                actors, 42, 17, 100, 2, block_size);
        } catch (const std::invalid_argument&) {
            rejected = true;
        }
        require(rejected, "test-only launch shape accepted an unsupported block size");
    }
    std::cout << "PASS test-only 64/128/256-thread launch shapes and invalid shapes\n";
#else
    (void)actors;
    std::cout << "NOT RUN launch-shape checks (BUILD_TESTING=OFF)\n";
#endif
}

void self_test() {
    const auto actors = demo_scene();
    const auto reference = generate_event_intents(actors, 42, 0, 8, 2);
    validate_demo_oracle(reference);
    const auto repeated = generate_event_intents(actors, 42, 0, 8, 2);
    require(same_records(reference, repeated) && digest(reference) == digest(repeated),
        "same scene/seed/range changed canonical bytes or digest");
    std::cout << "PASS handwritten schedule/motion/stationary oracle and repeatability\n";

    for (const std::uint64_t seed : {UINT64_C(43), UINT64_C(0),
        std::numeric_limits<std::uint64_t>::max(), UINT64_C(42) + (UINT64_C(1) << 32)}) {
        const auto changed = generate_event_intents(actors, seed, 0, 8, 2);
        const auto repeated_seed = generate_event_intents(actors, seed, 0, 8, 2);
        require(random_words_differ(reference, changed), "different seed did not change PRNG words");
        require(same_records(changed, repeated_seed), "full-width seed repeatability failed");
    }
    std::cout << "PASS different/full-width seeds change only PRNG words\n";
    validate_random_key(actors, reference);

    auto reordered = actors;
    std::reverse(reordered.begin(), reordered.end());
    const auto reverse_events = generate_event_intents(reordered, 42, 0, 8, 2);
    require(same_records(reference, reverse_events) && digest(reference) == digest(reverse_events),
        "actor input ordering changed canonical bytes or digest");
    validate_split(actors, reference, {{0, 3}, {3, 5}});
    validate_split(actors, reference, {{0, 2}, {2, 3}, {5, 3}});
    std::cout << "PASS actor reordering and two/three contiguous range splits\n";

    validate_edges();
    validate_invalid_inputs();
    validate_launch_shapes(actors);
    std::cout << "demo_event_count=" << reference.size() << '\n';
    print_digest(std::cout, reference);
    std::cout << "Scene GPU self-test: PASS\n";
}

}  // namespace

int main(int argc, char** argv) {
    Mode mode;
    try {
        mode = parse_options(argc, argv);
    } catch (const std::invalid_argument& error) {
        std::cerr << "Invalid CLI: " << error.what() << "\nUse --help for usage.\n";
        return 2;
    }
    try {
        if (mode == Mode::Help) {
            print_help();
        } else if (mode == Mode::SelfTest) {
            self_test();
        } else {
            demo();
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Scene GPU smoke failed: " << error.what() << '\n';
        return 1;
    }
}
