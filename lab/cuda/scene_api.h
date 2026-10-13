#pragma once

#include <cstddef>
#include <cstdint>
#include <type_traits>
#include <vector>

namespace aismixer::lab {

// Scenario classifications only: B2 does not implement AIS reporting standards.
enum class ActorCategory : std::uint32_t {
    ClassA = 0,
    ClassB = 1,
    BaseStation = 2,
    AtoN = 3,
    SAR = 4,
    Emergency = 5,
};

// Tick zero is the initial state in a local planar coordinate system. actor_id
// is a scenario identity, independent of MMSI, TAG sources, and input order.
struct Actor {
    std::uint32_t actor_id;
    ActorCategory category;
    std::int64_t x_mm;
    std::int64_t y_mm;
    std::int32_t vx_mm_per_s;
    std::int32_t vy_mm_per_s;
    std::uint32_t report_interval_ticks;
    std::uint32_t phase_ticks;
};

// A scheduled logical report, without any assertion of transmission/reception.
// random_u32 is a diagnostic PRNG word, not an AIS field or security material.
struct EventIntent {
    std::uint64_t absolute_tick;
    std::uint32_t actor_id;
    ActorCategory category;
    std::int64_t x_mm;
    std::int64_t y_mm;
    std::uint32_t random_u32;
};

static_assert(std::is_trivial_v<Actor> && std::is_standard_layout_v<Actor>,
              "Actor must remain a POD transfer record");
static_assert(sizeof(Actor) == 40 && offsetof(Actor, actor_id) == 0 &&
                  offsetof(Actor, category) == 4 && offsetof(Actor, x_mm) == 8 &&
                  offsetof(Actor, y_mm) == 16 && offsetof(Actor, vx_mm_per_s) == 24 &&
                  offsetof(Actor, vy_mm_per_s) == 28 &&
                  offsetof(Actor, report_interval_ticks) == 32 &&
                  offsetof(Actor, phase_ticks) == 36,
              "Actor transfer layout must remain explicit");
static_assert(std::is_trivial_v<EventIntent> && std::is_standard_layout_v<EventIntent>,
              "EventIntent must remain a POD record");
static_assert(sizeof(EventIntent) == 40 && offsetof(EventIntent, absolute_tick) == 0 &&
                  offsetof(EventIntent, actor_id) == 8 &&
                  offsetof(EventIntent, category) == 12 &&
                  offsetof(EventIntent, x_mm) == 16 && offsetof(EventIntent, y_mm) == 24 &&
                  offsetof(EventIntent, random_u32) == 32,
              "EventIntent layout includes tail padding that must not be serialized");

inline constexpr std::size_t kMaxSceneActors = 10000;
inline constexpr std::uint64_t kMaxSceneTicks = 3600;
inline constexpr std::uint64_t kMaxSceneCandidates = 1000000;
inline constexpr std::uint64_t kMaxAbsoluteTick = 1000000000;
inline constexpr std::uint32_t kMaxTickSeconds = 60;
inline constexpr std::int64_t kMaxInitialPositionMm = 1000000000000;
inline constexpr std::int32_t kMaxVelocityMmPerS = 100000;

// GPU schedules (t % interval == phase), computes integer positions
// x0 + vx * t * tick_seconds and y0 + vy * t * tick_seconds, and takes one
// cuRAND Philox word from curand_init(seed, actor_id, absolute_tick).
// Output is ordered by absolute_tick, then actor_id, independently of scene
// input order, launch shape, and contiguous-range batching. Reproducibility
// assumes the same cuRAND/toolchain; no CPU simulation or PRNG fallback exists.
// Invalid inputs throw std::invalid_argument before GPU allocation. CUDA or
// unavailable/incompatible device 0 failures throw std::runtime_error. An empty
// scene or zero ticks still validates inputs and the compiled B2 kernel/device,
// but allocates no GPU buffers and launches no kernel.
std::vector<EventIntent> generate_event_intents(const std::vector<Actor>& actors,
                                               std::uint64_t seed,
                                               std::uint64_t start_tick,
                                               std::uint64_t tick_count,
                                               std::uint32_t tick_seconds);

}  // namespace aismixer::lab
