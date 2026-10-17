#pragma once

#include <cstdint>

namespace aismixer::lab::runtime {

inline constexpr std::int64_t kUnitsPerSecond = 9830400;
inline constexpr std::int64_t kUnitsPerSlot = 262144;
inline constexpr std::int64_t kUnitsPerFrame = 589824000;
inline constexpr std::uint16_t kSlotsPerFrame = 2250;

struct SimulationInstant { std::int64_t units = 0; };
constexpr bool operator==(SimulationInstant a, SimulationInstant b) noexcept { return a.units == b.units; }
constexpr bool operator!=(SimulationInstant a, SimulationInstant b) noexcept { return !(a == b); }
constexpr bool operator<(SimulationInstant a, SimulationInstant b) noexcept { return a.units < b.units; }
constexpr bool operator<=(SimulationInstant a, SimulationInstant b) noexcept { return a.units <= b.units; }
constexpr bool operator>(SimulationInstant a, SimulationInstant b) noexcept { return b < a; }
constexpr bool operator>=(SimulationInstant a, SimulationInstant b) noexcept { return b <= a; }

// Relative simulation zero is explicitly anchored to a POSIX UTC second.
// POSIX excludes leap seconds; this is not a leap-second/astronomical clock.
struct UtcAnchor {
    std::int64_t posix_seconds = 0;
    std::int64_t subsecond_units = 0;  // Normalized: 0 <= value < units/second.
};
struct UtcSlot {
    std::int64_t utc_minute = 0;  // Floor(POSIX seconds / 60), including negatives.
    std::uint16_t slot = 0;      // 0..2249, UTC minute aligned.
    std::int64_t units_into_slot = 0;  // 0..262143.
};

SimulationInstant add_units(SimulationInstant instant, std::int64_t delta);
std::int64_t difference_units(SimulationInstant later, SimulationInstant earlier);
SimulationInstant from_seconds(std::int64_t seconds);
// Both external conversions round nearest, exact half ties away from zero.
// An unrepresentable result throws overflow_error, never wraps/saturates.
SimulationInstant from_nanoseconds(std::int64_t nanoseconds);
std::int64_t to_nanoseconds(SimulationInstant instant);
void validate_utc_anchor(const UtcAnchor& anchor);
UtcSlot decompose_utc(SimulationInstant instant, const UtcAnchor& anchor);
SimulationInstant compose_utc(const UtcSlot& slot, const UtcAnchor& anchor);

}  // namespace aismixer::lab::runtime
