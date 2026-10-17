#include "simulation_time.h"

#include <limits>
#include <stdexcept>
#include <string>

namespace aismixer::lab::runtime {
namespace {
constexpr auto low = std::numeric_limits<std::int64_t>::min();
constexpr auto high = std::numeric_limits<std::int64_t>::max();

std::int64_t checked_add(std::int64_t a, std::int64_t b) {
    if ((b > 0 && a > high - b) || (b < 0 && a < low - b))
        throw std::overflow_error("simulation time addition overflow: " + std::to_string(a) + " + " + std::to_string(b));
    return a + b;
}
std::int64_t checked_subtract(std::int64_t a, std::int64_t b) {
    if ((b > 0 && a < low + b) || (b < 0 && a > high + b))
        throw std::overflow_error("simulation time subtraction overflow");
    return a - b;
}
std::int64_t checked_multiply(std::int64_t value, std::int64_t factor) {
    if (value > high / factor || value < low / factor)
        throw std::overflow_error("simulation time multiplication overflow");
    return value * factor;
}
// Quotient/remainder scaling avoids intermediate overflow and signed abs(MIN).
std::int64_t rounded_scale(std::int64_t input, std::uint64_t numerator,
                           std::uint64_t denominator) {
    const bool negative = input < 0;
    const auto magnitude = negative ? std::uint64_t{0} - static_cast<std::uint64_t>(input)
                                    : static_cast<std::uint64_t>(input);
    const auto limit = static_cast<std::uint64_t>(high) + (negative ? 1u : 0u);
    const auto whole = magnitude / denominator;
    if (whole > limit / numerator) throw std::overflow_error("external time conversion overflow");
    const auto partial = (magnitude % denominator) * numerator;
    const auto fraction = partial / denominator;
    const auto rounded = fraction + (partial % denominator >= (denominator + 1) / 2 ? 1u : 0u);
    const auto base = whole * numerator;
    if (rounded > limit - base) throw std::overflow_error("external time conversion overflow");
    const auto result = base + rounded;
    if (negative && result == static_cast<std::uint64_t>(high) + 1u) return low;
    return negative ? -static_cast<std::int64_t>(result) : static_cast<std::int64_t>(result);
}
std::int64_t floor_divide(std::int64_t value, std::int64_t divisor) {
    const auto quotient = value / divisor;
    return quotient - (value % divisor < 0 ? 1 : 0);
}
}  // namespace

SimulationInstant add_units(SimulationInstant instant, std::int64_t delta) {
    return {checked_add(instant.units, delta)};
}
std::int64_t difference_units(SimulationInstant later, SimulationInstant earlier) {
    return checked_subtract(later.units, earlier.units);
}
SimulationInstant from_seconds(std::int64_t seconds) {
    return {checked_multiply(seconds, kUnitsPerSecond)};
}
SimulationInstant from_nanoseconds(std::int64_t nanoseconds) {
    return {rounded_scale(nanoseconds, 768, 78125)};
}
std::int64_t to_nanoseconds(SimulationInstant instant) {
    return rounded_scale(instant.units, 78125, 768);
}
void validate_utc_anchor(const UtcAnchor& anchor) {
    if (anchor.subsecond_units < 0 || anchor.subsecond_units >= kUnitsPerSecond)
        throw std::invalid_argument("UTC anchor subsecond units must be normalized");
}
UtcSlot decompose_utc(SimulationInstant instant, const UtcAnchor& anchor) {
    validate_utc_anchor(anchor);
    auto relative_seconds = floor_divide(instant.units, kUnitsPerSecond);
    auto fraction = instant.units % kUnitsPerSecond;
    if (fraction < 0) fraction += kUnitsPerSecond;
    fraction += anchor.subsecond_units;
    if (fraction >= kUnitsPerSecond) {
        relative_seconds = checked_add(relative_seconds, 1);
        fraction -= kUnitsPerSecond;
    }
    const auto seconds = checked_add(anchor.posix_seconds, relative_seconds);
    const auto minute = floor_divide(seconds, 60);
    auto second = seconds % 60;
    if (second < 0) second += 60;
    const auto units_in_minute = second * kUnitsPerSecond + fraction;
    return {minute, static_cast<std::uint16_t>(units_in_minute / kUnitsPerSlot),
            units_in_minute % kUnitsPerSlot};
}
SimulationInstant compose_utc(const UtcSlot& slot, const UtcAnchor& anchor) {
    validate_utc_anchor(anchor);
    if (slot.slot >= kSlotsPerFrame || slot.units_into_slot < 0 || slot.units_into_slot >= kUnitsPerSlot)
        throw std::invalid_argument("UTC minute/slot decomposition is not normalized");
    const auto units = static_cast<std::int64_t>(slot.slot) * kUnitsPerSlot + slot.units_into_slot;
    auto minute = slot.utc_minute;
    auto second = units / kUnitsPerSecond;
    if (minute < 0 && second > 0) { minute = checked_add(minute, 1); second -= 60; }
    const auto seconds = checked_add(checked_multiply(minute, 60), second);
    const auto seconds_difference = checked_subtract(seconds, anchor.posix_seconds);
    const auto subsecond_difference = units % kUnitsPerSecond - anchor.subsecond_units;
    // Normalize toward zero before multiplying, so a representable negative
    // result near INT64_MIN is not lost to an overflowing negative intermediate.
    auto whole = seconds_difference;
    auto partial = subsecond_difference;
    if (whole < 0 && partial > 0) { whole = checked_add(whole, 1); partial -= kUnitsPerSecond; }
    if (whole > 0 && partial < 0) { whole = checked_add(whole, -1); partial += kUnitsPerSecond; }
    return {checked_add(checked_multiply(whole, kUnitsPerSecond), partial)};
}

}  // namespace aismixer::lab::runtime
