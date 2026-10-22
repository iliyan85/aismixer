#pragma once

#include "simulation_time.h"

#include <cstdint>
#include <limits>
#include <stdexcept>

namespace aismixer::lab::classa {

// Channel-specific absolute UTC-frame slot identities for station-local maps.
// Representation and index arithmetic are SIMULATION_POLICY; the frame of
// 2250 slots per UTC minute is A2-3.1.2/A2-3.1.4. Runtime UtcSlot remains the
// authority for mapping an instant to a minute/slot.
inline constexpr std::uint32_t kSlotIdentityVersion = 1;  // SLOT-IDENTITY-v1.
enum class Channel : std::uint8_t { Ais1 = 0, Ais2 = 1 };
inline constexpr std::int64_t kSlotsPerFrame = 2250;
inline constexpr std::int64_t kMicrometresPerNauticalMile = INT64_C(1852000000);
inline constexpr std::int64_t kReuseRangeMicrometres = 120 * kMicrometresPerNauticalMile;  // 222 240 000 000.
inline constexpr std::int64_t kMaxAbsoluteMinute = std::numeric_limits<std::int64_t>::max() / kSlotsPerFrame - 1;

inline Channel other_channel(Channel channel) noexcept {
    return channel == Channel::Ais1 ? Channel::Ais2 : Channel::Ais1;
}
inline const char* channel_name(Channel channel) {
    switch (channel) {
    case Channel::Ais1: return "AIS1";
    case Channel::Ais2: return "AIS2";
    }
    throw std::invalid_argument("Class A channel: only AIS1/AIS2 exist in this profile");
}
inline void validate_channel(Channel channel) { (void)channel_name(channel); }

struct AbsoluteSlot {
    std::int64_t utc_minute = 0;
    std::uint16_t slot = 0;  // 0..2249.
};
inline bool operator==(AbsoluteSlot a, AbsoluteSlot b) noexcept {
    return a.utc_minute == b.utc_minute && a.slot == b.slot;
}

inline std::int64_t absolute_index(AbsoluteSlot value) {
    if (value.slot >= kSlotsPerFrame) throw std::invalid_argument("Class A slot index must be 0..2249");
    if (value.utc_minute > kMaxAbsoluteMinute || value.utc_minute < -kMaxAbsoluteMinute)
        throw std::overflow_error("Class A absolute slot index overflow");
    return value.utc_minute * kSlotsPerFrame + value.slot;
}
inline std::int64_t frame_of_index(std::int64_t index) noexcept {
    const auto quotient = index / kSlotsPerFrame;
    return quotient - (index % kSlotsPerFrame < 0 ? 1 : 0);
}
inline std::uint16_t frame_slot(std::int64_t index) noexcept {
    auto remainder = index % kSlotsPerFrame;
    if (remainder < 0) remainder += kSlotsPerFrame;
    return static_cast<std::uint16_t>(remainder);
}
inline AbsoluteSlot slot_from_index(std::int64_t index) noexcept {
    return {frame_of_index(index), frame_slot(index)};
}
inline AbsoluteSlot slot_from_utc(const runtime::UtcSlot& utc) {
    if (utc.slot >= kSlotsPerFrame || utc.units_into_slot < 0 || utc.units_into_slot >= runtime::kUnitsPerSlot)
        throw std::invalid_argument("Class A UTC slot is not normalized");
    return {utc.utc_minute, utc.slot};
}
inline std::int64_t offset_index(std::int64_t index, std::int64_t delta) {
    if ((delta > 0 && index > std::numeric_limits<std::int64_t>::max() - delta) ||
        (delta < 0 && index < std::numeric_limits<std::int64_t>::min() - delta))
        throw std::overflow_error("Class A slot offset overflow");
    return index + delta;
}

}  // namespace aismixer::lab::classa
