#include "local_directory.h"

#include <algorithm>
#include <stdexcept>

namespace aismixer::lab::classa {

const char* allocation_basis_name(AllocationBasis basis) {
    switch (basis) {
    case AllocationBasis::ObservedUse: return "observed_use";
    case AllocationBasis::SotdmaTimeout: return "sotdma_timeout";
    case AllocationBasis::SotdmaNextFrameOffset: return "sotdma_next_frame_offset";
    case AllocationBasis::ItdmaIncrement: return "itdma_increment";
    case AllocationBasis::ItdmaKeep: return "itdma_keep";
    }
    throw std::invalid_argument("Class A directory: unknown allocation basis");
}

const char* peer_kind_name(PeerKind kind) {
    switch (kind) {
    case PeerKind::Mobile: return "mobile";
    case PeerKind::Base: return "base";
    }
    throw std::invalid_argument("Class A directory: unknown peer kind");
}

bool sotdma_basis(AllocationBasis basis) noexcept {
    return basis == AllocationBasis::ObservedUse || basis == AllocationBasis::SotdmaTimeout ||
           basis == AllocationBasis::SotdmaNextFrameOffset;
}

std::int64_t reservation_last_index(const PeerAnnouncedReservation& reservation) {
    const auto retained = reservation.reserved_through_minute - frame_of_index(reservation.first_index);
    return offset_index(reservation.first_index, retained * kSlotsPerFrame + reservation.block_length - 1);
}

PeerDirectory::PeerDirectory(OwnIdentity owner, std::size_t capacity) : owner_(owner), capacity_(capacity) {
    if (owner.equipment_id.value == 0 || owner.radio_id.value == 0)
        throw std::invalid_argument("Class A directory: owner identity must name an equipment and a radio");
    if (capacity == 0 || capacity > 65536)
        throw std::invalid_argument("Class A directory: capacity must be 1..65536 peers");
}

const PeerRecord* PeerDirectory::find(std::uint32_t mmsi) const noexcept {
    const auto found = peers_.find(mmsi);
    return found == peers_.end() ? nullptr : &found->second;
}

bool PeerDirectory::would_fit(std::uint32_t mmsi) const noexcept {
    return peers_.count(mmsi) != 0 || peers_.size() < capacity_;
}

void PeerDirectory::upsert(const PeerRecord& record) {
    if (record.mmsi == 0 || record.mmsi > 0x3fffffffu)
        throw std::invalid_argument("Class A directory: peer identity must be a nonzero 30-bit MMSI");
    if (!would_fit(record.mmsi))
        throw std::invalid_argument("Class A directory: bounded peer capacity exceeded; expire stale peers first");
    if (record.reservations.size() > kMaxPeerReservations)
        throw std::invalid_argument("Class A directory: per-peer reservation cache bound exceeded");
    (void)peer_kind_name(record.kind);
    const auto ordered = std::is_sorted(record.reservations.begin(), record.reservations.end(),
        [](const PeerAnnouncedReservation& a, const PeerAnnouncedReservation& b) {
            return a.channel != b.channel ? a.channel < b.channel : a.first_index < b.first_index;
        });
    if (!ordered) throw std::invalid_argument("Class A directory: peer reservations must be canonically ordered");
    for (const auto& reservation : record.reservations) {
        if (reservation.reserved_through_minute < frame_of_index(reservation.first_index) ||
            reservation.block_length < 1 || reservation.block_length > 5)
            throw std::invalid_argument("Class A directory: malformed peer reservation");
        (void)reservation_last_index(reservation);
    }
    peers_[record.mmsi] = record;
}

std::vector<PeerRecord> PeerDirectory::snapshot() const {
    std::vector<PeerRecord> result;
    result.reserve(peers_.size());
    for (const auto& entry : peers_) result.push_back(entry.second);
    return result;
}

std::size_t PeerDirectory::expire_stale(std::int64_t oldest_heard_index, std::int64_t now_index) {
    std::size_t removed = 0;
    for (auto iterator = peers_.begin(); iterator != peers_.end();) {
        const auto& record = iterator->second;
        const bool stale = record.last_heard_index < oldest_heard_index;
        const bool reaching = std::any_of(record.reservations.begin(), record.reservations.end(),
            [&](const PeerAnnouncedReservation& reservation) {
                return reservation_last_index(reservation) >= now_index;
            });
        if (stale && !reaching) {
            iterator = peers_.erase(iterator);
            ++removed;
        } else {
            ++iterator;
        }
    }
    return removed;
}

}  // namespace aismixer::lab::classa
