#pragma once

#include <cstdint>
#include <variant>

namespace aismixer::lab::ais {

class RadioState19;

// CLASSA-COMMSTATE-v1: a context-free semantic adapter for the 19-bit fields
// in M.1371-6 Tables 18-20. It does not select a slot, validate a reservation
// against a clock/map, or prove synchronization-source validity. Include
// ais_message.h when using the returned legacy RadioState19 value.
inline constexpr std::uint32_t kCommunicationStateCodecVersion = 1;

// A wide underlying type preserves invalid caller-supplied values until the
// semantic builders can reject them rather than wrapping to a legal state.
enum class SyncState : std::int64_t {
    UtcDirect = 0,
    UtcIndirect = 1,
    BaseDirect = 2,
    // Mobile source selected by highest received-station count, or a mobile
    // source directly synchronized to a base (Table 18 sync value 3).
    Other = 3
};

class ItdmaState19 {
public:
    // Table 20, printed 49-50: codes 0..4 represent lengths 1..5; codes 5..7
    // represent lengths 1..3 and add 8192 to the 13-bit wire increment.
    static ItdmaState19 from_fields(SyncState sync, std::int64_t wire_increment,
                                    std::int64_t slot_count_code, bool keep);
    static ItdmaState19 from_effective(SyncState sync,
                                       std::int64_t effective_increment,
                                       std::int64_t slot_count, bool keep);
    static ItdmaState19 decode(const RadioState19& raw);

    RadioState19 raw() const;
    SyncState sync_state() const noexcept;
    std::uint32_t wire_increment() const noexcept;
    std::uint32_t slot_count_code() const noexcept;
    std::uint32_t effective_increment() const noexcept;
    std::uint32_t slot_count() const noexcept;
    bool keep() const noexcept;
    // An extended code with wire increment 0 has effective increment 8192.
    // slot_count() still translates the code when no future block is allocated.
    bool no_further_allocation() const noexcept;

private:
    explicit ItdmaState19(std::uint32_t value) noexcept : value_(value) {}
    std::uint32_t value_;
};

// Wide signed inputs keep negative and oversized values visible to validation.
// The tag, not just its numeric width, determines the submessage's meaning.
struct SotdmaSlotOffset {
    std::int64_t offset;
};
struct SotdmaUtc {
    std::int64_t hour;
    std::int64_t minute;
};
struct SotdmaCurrentSlot {
    std::int64_t slot;
};
struct SotdmaReceivedStations {
    std::int64_t count;
};
using SotdmaSubmessage =
    std::variant<SotdmaSlotOffset, SotdmaUtc, SotdmaCurrentSlot,
                 SotdmaReceivedStations>;

class SotdmaState19 {
public:
    static SotdmaState19 from_submessage(SyncState sync,
                                         std::int64_t slot_timeout,
                                         const SotdmaSubmessage& submessage);
    // A next-frame offset is 2250 + target_slot - current_slot: its necessary
    // context-free domain is 1..4499, with 0 reserved for release. This bound
    // alone cannot certify the target for a particular current slot/epoch.
    static SotdmaState19 offset(SyncState sync, std::int64_t slot_offset);
    // Both explicit indices are 0..2249. This computes their relative offset;
    // it does not select a target or certify that the chosen slot is available.
    static SotdmaState19 offset_in_next_frame(SyncState sync,
                                             std::int64_t current_slot,
                                             std::int64_t next_slot);
    // Table 19 supplies no no-UTC marker. Only explicit hour 0..23/minute 0..59
    // are supported; the two unused low bits are canonically zero in v1.
    static SotdmaState19 utc(SyncState sync, std::int64_t hour,
                             std::int64_t minute);
    static SotdmaState19 current_slot(SyncState sync,
                                      std::int64_t slot_timeout,
                                      std::int64_t slot_number);
    static SotdmaState19 received_stations(SyncState sync,
                                           std::int64_t slot_timeout,
                                           std::int64_t station_count);
    // Strict adapter decode rejects malformed modes/ranges and noncanonical
    // UTC low bits. It cannot attest the provenance of a received UTC value.
    static SotdmaState19 decode(const RadioState19& raw);

    RadioState19 raw() const;
    SyncState sync_state() const noexcept;
    std::uint32_t slot_timeout() const noexcept;
    SotdmaSubmessage submessage() const noexcept;

private:
    explicit SotdmaState19(std::uint32_t value) noexcept : value_(value) {}
    std::uint32_t value_;
};

}  // namespace aismixer::lab::ais
