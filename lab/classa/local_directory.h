#pragma once

#include "radio_evidence.h"
#include "communication_state.h"
#include "geodesy.h"

#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <vector>

namespace aismixer::lab::classa {

// Owner-local directory of personally decoded and accepted peer evidence
// (A2-4.2.1 user directory; A2-3.1.6 slot-state provenance). Keyed by the
// validated remote MMSI. It stores no world truth and is never shared; it is
// stamped with its owner's identity so that pure candidate functions can
// refuse a foreign directory. Capacities and retention are SIMULATION_POLICY
// (PEER-DIRECTORY-v1).
inline constexpr std::uint32_t kPeerDirectoryVersion = 1;
inline constexpr std::size_t kDefaultPeerCapacity = 1024;
// Per-peer announced-reservation cache bound: a Class A peer at the fastest
// Table 1 rate (2 s) uses 30 frame slots per frame; with in-place refresh per
// (channel, frame slot) each slot lineage holds at most one live and one
// released-but-not-yet-pruned record, plus next-frame offset transients.
inline constexpr std::size_t kMaxPeerReservations = 128;

enum class PeerKind : std::uint8_t { Mobile = 1, Base = 2 };
enum class AllocationBasis : std::uint8_t {
    ObservedUse = 1,            // The slot the peer was decoded in, this frame only.
    SotdmaTimeout = 2,          // Decoded slot retained for the announced remaining frames.
    SotdmaNextFrameOffset = 3,  // Timeout 0 with a positive next-frame offset.
    ItdmaIncrement = 4,         // Announced relative block, one-shot (not frame periodic).
    ItdmaKeep = 5               // Present slot retained for one additional frame.
};
const char* allocation_basis_name(AllocationBasis basis);
const char* peer_kind_name(PeerKind kind);
// SOTDMA-basis records of one peer refresh in place per (channel, frame slot);
// ITDMA records are identified by their exact first index and basis. Within
// a lineage the physically newest announcement wins: greater first index,
// then greater announcing (decoded) slot index.
bool sotdma_basis(AllocationBasis basis) noexcept;

struct PeerAnnouncedReservation {
    Channel channel = Channel::Ais1;
    std::int64_t first_index = 0;
    std::uint8_t block_length = 1;
    std::int64_t announced_minute = 0;
    std::int64_t reserved_through_minute = 0;
    AllocationBasis basis = AllocationBasis::ObservedUse;
    std::uint64_t receipt_id = 0;
    std::int64_t announced_index = 0;  // Decoded slot of the announcing frame (physical recency key).
};
// Last absolute slot index the announcement covers (checked arithmetic).
std::int64_t reservation_last_index(const PeerAnnouncedReservation& reservation);

struct PeerRecord {
    std::uint32_t mmsi = 0;
    PeerKind kind = PeerKind::Mobile;
    DecodedMessageKind last_message = DecodedMessageKind::Type1;
    Channel last_channel = Channel::Ais1;
    std::int64_t last_heard_index = 0;     // Physically latest decoded slot (monotonic per peer).
    runtime::SimulationInstant last_disclosed_at;
    std::uint64_t first_receipt_id = 0;
    std::uint64_t last_receipt_id = 0;
    std::uint32_t receipts = 0;
    bool position_available = false;      // Explicitly absent otherwise.
    bool position_in_geodesic_domain = false;
    std::int64_t longitude_ais = ais::kLongitudeUnavailable;
    std::int64_t latitude_ais = ais::kLatitudeUnavailable;
    world::Point position;                 // Valid only when position_in_geodesic_domain.
    ais::SyncState sync_state = ais::SyncState::Other;
    std::optional<std::uint32_t> received_station_count;
    std::optional<std::uint32_t> utc_hour;
    std::optional<std::uint32_t> utc_minute_field;
    std::int64_t navigation_status = 15;
    std::vector<PeerAnnouncedReservation> reservations;  // Ascending by (channel, first_index).
    std::uint64_t learned_generation = 0;
    EvidenceProvenance provenance = EvidenceProvenance::UnitFixture;
};

class PeerDirectory {
public:
    explicit PeerDirectory(OwnIdentity owner, std::size_t capacity = kDefaultPeerCapacity);
    const OwnIdentity& owner() const noexcept { return owner_; }
    std::size_t capacity() const noexcept { return capacity_; }
    std::size_t size() const noexcept { return peers_.size(); }
    const PeerRecord* find(std::uint32_t mmsi) const noexcept;
    // Returns false (no mutation) when a new peer would exceed capacity.
    bool would_fit(std::uint32_t mmsi) const noexcept;
    // Replaces or inserts the record. Throws before mutation if capacity or
    // the per-peer reservation bound would be exceeded.
    void upsert(const PeerRecord& record);
    std::vector<PeerRecord> snapshot() const;  // Ascending MMSI.
    // Deterministic expiry: a peer last heard before oldest_heard_index is
    // removed unless one of its cached announcements still covers now_index
    // or a later slot (an announced ITDMA block may lie up to 16383 slots
    // ahead of the last decode and may straddle a frame boundary).
    std::size_t expire_stale(std::int64_t oldest_heard_index, std::int64_t now_index);
    void clear() noexcept { peers_.clear(); }

private:
    OwnIdentity owner_;
    std::size_t capacity_;
    std::map<std::uint32_t, PeerRecord> peers_;
};

}  // namespace aismixer::lab::classa
