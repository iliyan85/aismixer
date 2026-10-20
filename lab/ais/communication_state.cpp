#include "communication_state.h"

#include "ais_message.h"

#include <stdexcept>
#include <string>
#include <string_view>

namespace aismixer::lab::ais {
namespace {

void require_range(std::int64_t value, std::int64_t minimum,
                   std::int64_t maximum, std::string_view field) {
    if (value < minimum || value > maximum) {
        throw std::invalid_argument(std::string(field) + ": expected " +
                                    std::to_string(minimum) + ".." +
                                    std::to_string(maximum) + ", got " +
                                    std::to_string(value));
    }
}

std::uint32_t sync_bits(SyncState sync) {
    const auto value = static_cast<std::int64_t>(sync);
    if (value < 0 || value > 3) {
        throw std::invalid_argument("communication state: sync must be 0..3");
    }
    return static_cast<std::uint32_t>(value) << 17;
}

void require_timeout(bool matches) {
    if (!matches) {
        throw std::invalid_argument(
            "SOTDMA19: slot timeout does not match the semantic submessage tag");
    }
}

SotdmaSubmessage inspect_sotdma(std::uint32_t value) noexcept {
    const auto timeout = (value >> 14) & 7u;
    const auto submessage = value & 0x3fffu;
    switch (timeout) {
    case 0:
        return SotdmaSlotOffset{static_cast<std::int64_t>(submessage)};
    case 1:
        return SotdmaUtc{static_cast<std::int64_t>((submessage >> 9) & 31u),
                         static_cast<std::int64_t>((submessage >> 2) & 127u)};
    case 2:
    case 4:
    case 6:
        return SotdmaCurrentSlot{static_cast<std::int64_t>(submessage)};
    default:  // Timeouts 3, 5, 7.
        return SotdmaReceivedStations{static_cast<std::int64_t>(submessage)};
    }
}

}  // namespace

ItdmaState19 ItdmaState19::from_fields(SyncState sync,
                                      std::int64_t wire_increment,
                                      std::int64_t slot_count_code, bool keep) {
    const auto sync_value = sync_bits(sync);
    require_range(wire_increment, 0, 8191, "ITDMA19.wire_increment");
    require_range(slot_count_code, 0, 7, "ITDMA19.slot_count_code");
    return ItdmaState19(
        sync_value | (static_cast<std::uint32_t>(wire_increment) << 4) |
        (static_cast<std::uint32_t>(slot_count_code) << 1) | (keep ? 1u : 0u));
}

ItdmaState19 ItdmaState19::from_effective(SyncState sync,
                                         std::int64_t effective_increment,
                                         std::int64_t slot_count, bool keep) {
    require_range(effective_increment, 0, 16383,
                  "ITDMA19.effective_increment");
    require_range(slot_count, 1, effective_increment >= 8192 ? 3 : 5,
                  "ITDMA19.slot_count");
    const bool extended = effective_increment >= 8192;
    return from_fields(sync, extended ? effective_increment - 8192
                                     : effective_increment,
                       extended ? slot_count + 4 : slot_count - 1, keep);
}

ItdmaState19 ItdmaState19::decode(const RadioState19& raw) {
    const auto value = raw.value();
    return from_fields(static_cast<SyncState>((value >> 17) & 3u),
                       (value >> 4) & 0x1fffu, (value >> 1) & 7u,
                       (value & 1u) != 0);
}

RadioState19 ItdmaState19::raw() const { return RadioState19(value_); }

SyncState ItdmaState19::sync_state() const noexcept {
    return static_cast<SyncState>((value_ >> 17) & 3u);
}

std::uint32_t ItdmaState19::wire_increment() const noexcept {
    return (value_ >> 4) & 0x1fffu;
}

std::uint32_t ItdmaState19::slot_count_code() const noexcept {
    return (value_ >> 1) & 7u;
}

std::uint32_t ItdmaState19::effective_increment() const noexcept {
    return wire_increment() + (slot_count_code() >= 5 ? 8192u : 0u);
}

std::uint32_t ItdmaState19::slot_count() const noexcept {
    const auto code = slot_count_code();
    return code >= 5 ? code - 4 : code + 1;
}

bool ItdmaState19::keep() const noexcept { return (value_ & 1u) != 0; }

bool ItdmaState19::no_further_allocation() const noexcept {
    return effective_increment() == 0;
}

SotdmaState19 SotdmaState19::from_submessage(
    SyncState sync, std::int64_t slot_timeout,
    const SotdmaSubmessage& submessage) {
    const auto sync_value = sync_bits(sync);
    require_range(slot_timeout, 0, 7, "SOTDMA19.slot_timeout");
    std::uint32_t encoded = 0;
    if (const auto* offset_field = std::get_if<SotdmaSlotOffset>(&submessage)) {
        require_timeout(slot_timeout == 0);
        require_range(offset_field->offset, 0, 4499, "SOTDMA19.slot_offset");
        encoded = static_cast<std::uint32_t>(offset_field->offset);
    } else if (const auto* utc_field = std::get_if<SotdmaUtc>(&submessage)) {
        require_timeout(slot_timeout == 1);
        require_range(utc_field->hour, 0, 23, "SOTDMA19.utc_hour");
        require_range(utc_field->minute, 0, 59, "SOTDMA19.utc_minute");
        // Table 19 uses submessage bits 13..9 for hour and 8..2 for minute.
        // v1 intentionally emits zero for the two unused low bits.
        encoded = (static_cast<std::uint32_t>(utc_field->hour) << 9) |
                  (static_cast<std::uint32_t>(utc_field->minute) << 2);
    } else if (const auto* slot_field =
                   std::get_if<SotdmaCurrentSlot>(&submessage)) {
        require_timeout(slot_timeout == 2 || slot_timeout == 4 ||
                        slot_timeout == 6);
        require_range(slot_field->slot, 0, 2249, "SOTDMA19.current_slot");
        encoded = static_cast<std::uint32_t>(slot_field->slot);
    } else if (const auto* stations_field =
                   std::get_if<SotdmaReceivedStations>(&submessage)) {
        require_timeout(slot_timeout == 3 || slot_timeout == 5 ||
                        slot_timeout == 7);
        require_range(stations_field->count, 0, 16383,
                      "SOTDMA19.received_stations");
        encoded = static_cast<std::uint32_t>(stations_field->count);
    } else {
        throw std::invalid_argument("SOTDMA19: missing semantic submessage");
    }
    return SotdmaState19(sync_value |
                         (static_cast<std::uint32_t>(slot_timeout) << 14) |
                         encoded);
}

SotdmaState19 SotdmaState19::offset(SyncState sync,
                                  std::int64_t slot_offset) {
    return from_submessage(sync, 0, SotdmaSlotOffset{slot_offset});
}

SotdmaState19 SotdmaState19::offset_in_next_frame(SyncState sync,
                                                std::int64_t current_slot,
                                                std::int64_t next_slot) {
    require_range(current_slot, 0, 2249, "SOTDMA19.current_slot");
    require_range(next_slot, 0, 2249, "SOTDMA19.next_slot");
    return offset(sync, 2250 + next_slot - current_slot);
}

SotdmaState19 SotdmaState19::utc(SyncState sync, std::int64_t hour,
                               std::int64_t minute) {
    return from_submessage(sync, 1, SotdmaUtc{hour, minute});
}

SotdmaState19 SotdmaState19::current_slot(SyncState sync,
                                        std::int64_t slot_timeout,
                                        std::int64_t slot_number) {
    return from_submessage(sync, slot_timeout, SotdmaCurrentSlot{slot_number});
}

SotdmaState19 SotdmaState19::received_stations(
    SyncState sync, std::int64_t slot_timeout, std::int64_t station_count) {
    return from_submessage(sync, slot_timeout,
                           SotdmaReceivedStations{station_count});
}

SotdmaState19 SotdmaState19::decode(const RadioState19& raw) {
    const auto value = raw.value();
    const auto timeout = (value >> 14) & 7u;
    if (timeout == 1 && (value & 3u) != 0) {
        throw std::invalid_argument(
            "SOTDMA19: nonzero unused UTC bits are outside canonical v1");
    }
    return from_submessage(static_cast<SyncState>((value >> 17) & 3u),
                           timeout, inspect_sotdma(value));
}

RadioState19 SotdmaState19::raw() const { return RadioState19(value_); }

SyncState SotdmaState19::sync_state() const noexcept {
    return static_cast<SyncState>((value_ >> 17) & 3u);
}

std::uint32_t SotdmaState19::slot_timeout() const noexcept {
    return (value_ >> 14) & 7u;
}

SotdmaSubmessage SotdmaState19::submessage() const noexcept {
    return inspect_sotdma(value_);
}

}  // namespace aismixer::lab::ais
