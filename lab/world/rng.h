#pragma once

#include <cstdint>
#include <stdexcept>

#ifdef __CUDACC__
#define W1_HD __host__ __device__
#else
#define W1_HD
#endif

namespace aismixer::lab::world {
inline constexpr std::uint32_t kRngVersion = 1;
enum class Purpose : std::uint32_t {
    InitialWaypoint = 1, FirstDestination = 2, NextDestination = 3,
    SpeedValue = 4, SpeedDuration = 5, StatusChoice = 6, StatusDuration = 7,
    EquipmentInitial = 8, EquipmentOnline = 9, EquipmentOffline = 10,
    RegionCount = 11, RegionRadial = 12, RegionBearing = 13, RegionElevation = 14,
    ReachValue = 15, ReachDuration = 16
};
// W1 semantic-key adapter, not the legacy cuRAND subsequence adapter.
// Key = seed XOR entity; counter = episode low/high, draw/4, version|purpose.
W1_HD inline std::uint32_t rng_word(std::uint64_t seed, std::uint64_t entity,
                                  Purpose purpose, std::uint64_t episode,
                                  std::uint32_t draw = 0) {
    std::uint32_t a = static_cast<std::uint32_t>(episode);
    std::uint32_t b = static_cast<std::uint32_t>(episode >> 32);
    std::uint32_t c = draw / 4;
    std::uint32_t d = (kRngVersion << 24) | static_cast<std::uint32_t>(purpose);
    std::uint32_t x = static_cast<std::uint32_t>(seed ^ entity);
    std::uint32_t y = static_cast<std::uint32_t>((seed ^ entity) >> 32);
    for (unsigned round = 0; round < 10; ++round) {
        const std::uint64_t p = UINT64_C(0xD2511F53) * a;
        const std::uint64_t q = UINT64_C(0xCD9E8D57) * c;
        const std::uint32_t na = static_cast<std::uint32_t>(q >> 32) ^ b ^ x;
        b = static_cast<std::uint32_t>(q);
        c = static_cast<std::uint32_t>(p >> 32) ^ d ^ y;
        d = static_cast<std::uint32_t>(p);
        a = na; x += UINT32_C(0x9E3779B9); y += UINT32_C(0xBB67AE85);
    }
    switch (draw % 4) { case 0: return a; case 1: return b; case 2: return c; default: return d; }
}
// Status return is shared with CUDA: exhausted rejection budget is an error,
// never modulo-biased fallback. Bounds are signed nonnegative in W1 models.
W1_HD inline bool rng_bounded(std::uint64_t seed, std::uint64_t entity,
                            Purpose purpose, std::uint64_t episode,
                            std::uint64_t minimum, std::uint64_t maximum,
                            std::uint64_t& result) {
    if (minimum > maximum) return false;
    const std::uint64_t span = maximum - minimum + 1;
    if (span == 0) return false;
    const std::uint64_t threshold = (std::uint64_t{0} - span) % span;
    for (std::uint32_t proposal = 0; proposal < 32; ++proposal) {
        const std::uint64_t value = (static_cast<std::uint64_t>(rng_word(seed, entity, purpose, episode, proposal * 2)) << 32)
            | rng_word(seed, entity, purpose, episode, proposal * 2 + 1);
        if (value >= threshold) { result = minimum + value % span; return true; }
    }
    return false;
}
inline std::int64_t sample_integer(std::uint64_t seed, std::uint64_t entity,
                                  Purpose purpose, std::uint64_t episode,
                                  std::int64_t minimum, std::int64_t maximum) {
    if (minimum < 0 || maximum < minimum) throw std::invalid_argument("W1 invalid nonnegative random interval");
    std::uint64_t result = 0;
    if (!rng_bounded(seed, entity, purpose, episode, static_cast<std::uint64_t>(minimum),
                     static_cast<std::uint64_t>(maximum), result))
        throw std::runtime_error("W1 unbiased integer proposal budget exhausted");
    return static_cast<std::int64_t>(result);
}
W1_HD inline double rng_uniform(std::uint64_t seed, std::uint64_t entity,
                                Purpose purpose, std::uint64_t episode) {
    return (static_cast<double>(rng_word(seed, entity, purpose, episode)) + 0.5) / 4294967296.0;
}
}  // namespace aismixer::lab::world
