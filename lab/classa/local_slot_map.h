#pragma once

#include "local_directory.h"

#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace aismixer::lab::classa {

// One station-local slot map for ONE terrestrial channel (A2-3.1.6 slot
// states; Tables 18-20 announcement semantics applied by the owner). It holds
// only this owner's intentions and personally accepted evidence, keyed by
// absolute UTC-frame slot index, and is stamped with the owner's identity.
// It is neither a global occupancy table nor proof that any other station
// heard anything. Storage bounds, the past/future horizon, the aging
// evaluation instant and the in-place refresh identity are SIMULATION_POLICY
// (LOCAL-SLOT-MAP-v1).
inline constexpr std::uint32_t kLocalSlotMapVersion = 1;
inline constexpr std::int64_t kSotdmaUnusedAgingFrames = 3;  // A2-3.1.6 "preceding three frames".
inline constexpr std::uint8_t kMaxOwnTimeoutFrames = 7;      // Table 16 TMO_MAX / Table 18 maximum advertised.
// Own continuous SOTDMA retention after the first use equals the value
// advertised at that first use, i.e. drawn timeout (3..7) minus one (LCP-P02).
inline constexpr std::uint8_t kMaxOwnSotdmaRetainedFrames = 6;
inline constexpr std::uint8_t kMaxBlockLength = 5;           // A2-3.2.2.11.

enum class SlotState : std::uint8_t { Free = 0, InternalOwn = 1, ExternalObserved = 2, GarbledObserved = 3 };
const char* slot_state_name(SlotState state);
enum class OwnReservationKind : std::uint8_t { Sotdma = 1, Itdma = 2 };

struct OwnReservation {
    Channel channel = Channel::Ais1;
    std::int64_t first_index = 0;
    std::uint8_t block_length = 1;     // 1..5 consecutive slots inside one frame (A0-MAC-013).
    OwnReservationKind kind = OwnReservationKind::Sotdma;
    std::uint8_t timeout_frames = 0;   // SOTDMA: frames retained after the first = value advertised there, 0..6.
    bool keep = false;                 // ITDMA: present block retained one additional frame.
    std::uint64_t power_generation = 0;
    runtime::SimulationInstant decided_at;
    std::uint64_t decision_id = 0;
    std::string cause;                 // Local decision provenance, never RF proof.
};
std::int64_t own_retained_frames(const OwnReservation& reservation) noexcept;

struct ExternalAllocation {
    Channel channel = Channel::Ais1;
    std::int64_t first_index = 0;
    std::uint8_t block_length = 1;
    std::uint32_t mmsi = 0;
    PeerKind kind = PeerKind::Mobile;
    AllocationBasis basis = AllocationBasis::ObservedUse;
    std::int64_t announced_minute = 0;         // Frame in which the evidence was decoded.
    std::int64_t announced_index = 0;          // Decoded slot of that frame: physical recency key within a lineage.
    // Last frame in which the block's FIRST slot is reserved: the block repeats
    // per frame for SOTDMA bases and ITDMA keep, while a one-shot ITDMA
    // increment block has reserved_through_minute == its first frame and its
    // later slots follow from the block length alone (never frame periodic).
    std::int64_t reserved_through_minute = 0;
    std::int64_t last_use_minute = 0;          // Last frame in which the peer was decoded there (>= announced).
    std::uint64_t receipt_id = 0;
    std::uint64_t power_generation = 0;
    EvidenceProvenance provenance = EvidenceProvenance::UnitFixture;
};
// True when `existing` was announced physically later than `incoming` within
// one lineage: greater first index, or equal first index and a greater
// announcing slot (a next-frame offset announced in frame F and the use of
// that slot in frame F+1 share the first index).
bool physically_newer(const ExternalAllocation& existing, const ExternalAllocation& incoming) noexcept;
// Last absolute slot index the allocation covers (checked arithmetic).
std::int64_t allocation_last_index(const ExternalAllocation& allocation);

struct GarbleRecord {
    Channel channel = Channel::Ais1;
    std::int64_t index = 0;
    runtime::SimulationInstant disclosed_at;
    std::uint64_t receipt_id = 0;
    ObservationDisposition disposition = ObservationDisposition::EnergyOnly;
    EvidenceProvenance provenance = EvidenceProvenance::UnitFixture;
};

struct MapLimits {
    std::int64_t past_frames = 4;    // Must exceed kSotdmaUnusedAgingFrames.
    std::int64_t future_frames = 8;  // Covers the 16383-slot ITDMA reach.
    std::size_t max_own = 64;
    std::size_t max_external = 9000;
    std::size_t max_garble = 4500;
};

// Pointers in a SlotView are valid only until the next non-const call on the
// map that produced it (reserve_own/release_own/advance/record_*/clear).
struct SlotView {
    std::int64_t index = 0;
    SlotState state = SlotState::Free;
    const OwnReservation* own = nullptr;
    std::vector<const ExternalAllocation*> live;  // Reserving this slot at the map instant; canonical (first_index, mmsi, basis).
    std::vector<const ExternalAllocation*> aged;  // SOTDMA allocations unused for the preceding three frames.
    const GarbleRecord* garble = nullptr;
};
struct MapSnapshotRow {
    std::int64_t index = 0;
    std::uint16_t frame_slot = 0;
    SlotState state = SlotState::Free;
    std::string detail;
};

class LocalSlotMap {
public:
    LocalSlotMap(OwnIdentity owner, Channel channel, MapLimits limits = {});
    const OwnIdentity& owner() const noexcept { return owner_; }
    Channel channel() const noexcept { return channel_; }
    const MapLimits& limits() const noexcept { return limits_; }
    bool advanced() const noexcept { return advanced_; }
    std::int64_t now_index() const noexcept { return now_index_; }
    // Monotonic map instant; prunes records entirely below the past horizon.
    void advance(std::int64_t now_index);
    // Validation (channel, block, frame containment, retention, horizon, overlap, capacity) precedes mutation.
    void reserve_own(const OwnReservation& reservation);
    bool release_own(std::int64_t first_index);
    // Records or refreshes an announcement. Within a lineage (SOTDMA bases of
    // one peer at one frame slot; ITDMA records of one peer, first index and
    // basis) the physically newest announcement wins: older records are
    // replaced; if a physically newer one already exists nothing changes and
    // false is returned. Validation precedes mutation.
    bool record_external(const ExternalAllocation& allocation);
    void record_garble(const GarbleRecord& record);
    const OwnReservation* own_covering(std::int64_t index) const noexcept;
    SlotView view(std::int64_t index) const;
    const std::vector<OwnReservation>& own_reservations() const noexcept { return own_; }
    std::vector<ExternalAllocation> external_allocations() const;
    std::vector<GarbleRecord> garbles() const;
    std::size_t external_count() const noexcept { return external_.size(); }
    std::size_t garble_count() const noexcept { return garble_.size(); }
    // Canonical, read-only, stably ordered diagnostic rows for an index range.
    std::vector<MapSnapshotRow> snapshot(std::int64_t first_index, std::int64_t last_index) const;
    void clear() noexcept;

private:
    void require_horizon(std::int64_t index, const char* what) const;
    bool external_live(const ExternalAllocation& allocation, std::int64_t index, bool& aged_out) const noexcept;
    OwnIdentity owner_;
    Channel channel_;
    MapLimits limits_;
    bool advanced_ = false;
    std::int64_t now_index_ = 0;
    std::vector<OwnReservation> own_;                      // Ascending first_index.
    std::multimap<std::int64_t, ExternalAllocation> external_;  // Keyed by first_index.
    std::map<std::int64_t, GarbleRecord> garble_;
};

}  // namespace aismixer::lab::classa
