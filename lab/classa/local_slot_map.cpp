#include "local_slot_map.h"

#include <algorithm>
#include <limits>
#include <optional>
#include <stdexcept>

namespace aismixer::lab::classa {
namespace {
constexpr auto kLowest = std::numeric_limits<std::int64_t>::min();
constexpr auto kHighest = std::numeric_limits<std::int64_t>::max();

void require(bool condition, const char* message) {
    if (!condition) throw std::invalid_argument(message);
}
std::int64_t last_covered_index(std::int64_t first_index, std::uint8_t block_length, std::int64_t retained_frames) {
    return offset_index(first_index, retained_frames * kSlotsPerFrame + block_length - 1);
}
std::optional<std::int64_t> checked_offset(std::int64_t index, std::int64_t delta) noexcept {
    if ((delta > 0 && index > kHighest - delta) || (delta < 0 && index < kLowest - delta)) return std::nullopt;
    return index + delta;
}
// index - first without overflow: nullopt when index precedes first or the
// distance is not representable (then it exceeds every retention anyway).
std::optional<std::int64_t> distance_from(std::int64_t first, std::int64_t index) noexcept {
    if (index < first) return std::nullopt;
    if (first < 0 && index > kHighest + first) return std::nullopt;
    return index - first;
}
bool canonical_before(const ExternalAllocation* a, const ExternalAllocation* b) noexcept {
    if (a->first_index != b->first_index) return a->first_index < b->first_index;
    if (a->mmsi != b->mmsi) return a->mmsi < b->mmsi;
    return a->basis < b->basis;
}
}  // namespace

const char* slot_state_name(SlotState state) {
    switch (state) {
    case SlotState::Free: return "free";
    case SlotState::InternalOwn: return "internal_own";
    case SlotState::ExternalObserved: return "external_observed";
    case SlotState::GarbledObserved: return "garbled_observed";
    }
    throw std::invalid_argument("Class A slot map: unknown slot state");
}

std::int64_t own_retained_frames(const OwnReservation& reservation) noexcept {
    return reservation.kind == OwnReservationKind::Sotdma ? reservation.timeout_frames
                                                          : (reservation.keep ? 1 : 0);
}

bool physically_newer(const ExternalAllocation& existing, const ExternalAllocation& incoming) noexcept {
    if (existing.first_index != incoming.first_index) return existing.first_index > incoming.first_index;
    return existing.announced_index > incoming.announced_index;
}

std::int64_t allocation_last_index(const ExternalAllocation& allocation) {
    return last_covered_index(allocation.first_index, allocation.block_length,
                              allocation.reserved_through_minute - frame_of_index(allocation.first_index));
}

LocalSlotMap::LocalSlotMap(OwnIdentity owner, Channel channel, MapLimits limits)
    : owner_(owner), channel_(channel), limits_(limits) {
    validate_channel(channel);
    require(owner.equipment_id.value != 0 && owner.radio_id.value != 0,
            "Class A slot map: owner identity must name an equipment and a radio");
    require(limits.past_frames > kSotdmaUnusedAgingFrames && limits.past_frames <= 16 && limits.future_frames >= 1 &&
            limits.future_frames <= 16, "Class A slot map: past horizon must be 4..16 frames (beyond SOTDMA aging) and future 1..16");
    require(limits.max_own >= 1 && limits.max_own <= 4096 && limits.max_external >= 1 &&
            limits.max_external <= 65536 && limits.max_garble >= 1 && limits.max_garble <= 65536,
            "Class A slot map: record bounds are outside the supported range");
}

void LocalSlotMap::require_horizon(std::int64_t index, const char* what) const {
    if (!advanced_) throw std::invalid_argument("Class A slot map: map instant not established before recording");
    const auto earliest = offset_index(now_index_, -limits_.past_frames * kSlotsPerFrame);
    const auto latest = offset_index(now_index_, limits_.future_frames * kSlotsPerFrame);
    if (index < earliest || index > latest) throw std::invalid_argument(what);
}

void LocalSlotMap::advance(std::int64_t now_index) {
    require(!advanced_ || now_index >= now_index_, "Class A slot map: map instant must not regress");
    (void)offset_index(now_index, -limits_.past_frames * kSlotsPerFrame);
    (void)offset_index(now_index, limits_.future_frames * kSlotsPerFrame);
    advanced_ = true;
    now_index_ = now_index;
    const auto earliest = offset_index(now_index_, -limits_.past_frames * kSlotsPerFrame);
    own_.erase(std::remove_if(own_.begin(), own_.end(), [&](const OwnReservation& reservation) {
        return last_covered_index(reservation.first_index, reservation.block_length,
                                  own_retained_frames(reservation)) < earliest;
    }), own_.end());
    for (auto iterator = external_.begin(); iterator != external_.end();) {
        if (allocation_last_index(iterator->second) < earliest) iterator = external_.erase(iterator);
        else ++iterator;
    }
    for (auto iterator = garble_.begin(); iterator != garble_.end();) {
        if (iterator->first < earliest) iterator = garble_.erase(iterator);
        else ++iterator;
    }
}

const OwnReservation* LocalSlotMap::own_covering(std::int64_t index) const noexcept {
    for (const auto& reservation : own_) {
        const auto distance = distance_from(reservation.first_index, index);
        if (!distance) continue;
        if (*distance % kSlotsPerFrame < reservation.block_length &&
            *distance / kSlotsPerFrame <= own_retained_frames(reservation))
            return &reservation;
    }
    return nullptr;
}

void LocalSlotMap::reserve_own(const OwnReservation& reservation) {
    require(reservation.channel == channel_, "Class A slot map: own reservation addressed to another channel");
    require(reservation.block_length >= 1 && reservation.block_length <= kMaxBlockLength,
            "Class A slot map: own block length must be 1..5 slots");
    require(reservation.timeout_frames <= kMaxOwnSotdmaRetainedFrames,
            "Class A slot map: own retained frames after the first use must be 0..6 (drawn timeout minus one)");
    require(reservation.kind == OwnReservationKind::Sotdma ? !reservation.keep : reservation.timeout_frames == 0,
            "Class A slot map: SOTDMA reservations carry a timeout, ITDMA reservations carry keep");
    require(reservation.power_generation != 0, "Class A slot map: own reservation needs its power generation");
    require(reservation.first_index >= now_index_, "Class A slot map: own reservation in the past");
    require_horizon(reservation.first_index, "Class A slot map: own reservation beyond the future horizon");
    require(frame_of_index(reservation.first_index) == frame_of_index(offset_index(reservation.first_index, reservation.block_length - 1)),
            "Class A slot map: an own block must lie within one frame (A0-MAC-013)");
    const auto retained = own_retained_frames(reservation);
    (void)last_covered_index(reservation.first_index, reservation.block_length, retained);
    for (std::int64_t frame = 0; frame <= retained; ++frame) {
        for (std::uint8_t offset = 0; offset < reservation.block_length; ++offset) {
            const auto index = offset_index(reservation.first_index, frame * kSlotsPerFrame + offset);
            require(own_covering(index) == nullptr, "Class A slot map: own reservations overlap on this channel");
        }
    }
    require(own_.size() < limits_.max_own, "Class A slot map: bounded own reservation capacity exceeded");
    own_.insert(std::upper_bound(own_.begin(), own_.end(), reservation,
        [](const OwnReservation& a, const OwnReservation& b) { return a.first_index < b.first_index; }),
        reservation);
}

bool LocalSlotMap::release_own(std::int64_t first_index) {
    const auto found = std::find_if(own_.begin(), own_.end(), [&](const OwnReservation& reservation) {
        return reservation.first_index == first_index;
    });
    if (found == own_.end()) return false;
    own_.erase(found);
    return true;
}

bool LocalSlotMap::record_external(const ExternalAllocation& allocation) {
    require(allocation.channel == channel_, "Class A slot map: external allocation addressed to another channel");
    require(allocation.block_length >= 1 && allocation.block_length <= kMaxBlockLength,
            "Class A slot map: external block length must be 1..5 slots");
    require(allocation.mmsi != 0 && allocation.mmsi <= 0x3fffffffu, "Class A slot map: external allocation needs a peer identity");
    (void)allocation_basis_name(allocation.basis);
    (void)peer_kind_name(allocation.kind);
    (void)provenance_name(allocation.provenance);
    require_horizon(allocation.first_index, "Class A slot map: external allocation outside the map horizon");
    const auto first_frame = frame_of_index(allocation.first_index);  // |first_frame| <= INT64_MAX / 2250: small sums are safe.
    require(allocation.reserved_through_minute >= first_frame &&
            allocation.reserved_through_minute <= first_frame + kMaxOwnTimeoutFrames,
            "Class A slot map: external retention must cover its first frame and at most seven further frames");
    require(allocation.basis != AllocationBasis::ItdmaIncrement || allocation.reserved_through_minute == first_frame,
            "Class A slot map: a one-shot ITDMA block is not frame periodic");
    const auto now_frame = frame_of_index(now_index_);
    require(allocation.announced_minute <= allocation.last_use_minute && allocation.last_use_minute <= now_frame,
            "Class A slot map: external allocation announced or used in the future, or used before its announcement");
    require(allocation.announced_minute >= first_frame - (kMaxOwnTimeoutFrames + 1),
            "Class A slot map: external allocation announced more than eight frames before its first frame");
    require(frame_of_index(allocation.announced_index) == allocation.announced_minute && allocation.announced_index <= now_index_,
            "Class A slot map: announcing slot must lie in the announced frame and not after the map instant");
    (void)allocation_last_index(allocation);
    std::vector<std::multimap<std::int64_t, ExternalAllocation>::iterator> superseded;
    if (sotdma_basis(allocation.basis)) {
        // Every retained record of this lineage lies within past+future+8 frames
        // behind and past+future frames ahead of any admissible first index.
        const auto behind = -(limits_.past_frames + limits_.future_frames + kMaxOwnTimeoutFrames + 1);
        const auto ahead = limits_.past_frames + limits_.future_frames;
        for (std::int64_t frames = behind; frames <= ahead; ++frames) {
            const auto key = checked_offset(allocation.first_index, frames * kSlotsPerFrame);
            if (!key) continue;
            const auto range = external_.equal_range(*key);
            for (auto iterator = range.first; iterator != range.second; ++iterator) {
                if (iterator->second.mmsi != allocation.mmsi || !sotdma_basis(iterator->second.basis)) continue;
                if (physically_newer(iterator->second, allocation)) return false;
                superseded.push_back(iterator);
            }
        }
    } else {
        const auto range = external_.equal_range(allocation.first_index);
        for (auto iterator = range.first; iterator != range.second; ++iterator) {
            if (iterator->second.mmsi != allocation.mmsi || iterator->second.basis != allocation.basis) continue;
            if (physically_newer(iterator->second, allocation)) return false;
            superseded.push_back(iterator);
        }
    }
    require(external_.size() - superseded.size() < limits_.max_external,
            "Class A slot map: bounded external allocation capacity exceeded");
    for (const auto iterator : superseded) external_.erase(iterator);
    external_.emplace(allocation.first_index, allocation);
    return true;
}

void LocalSlotMap::record_garble(const GarbleRecord& record) {
    require(record.channel == channel_, "Class A slot map: garble addressed to another channel");
    require(record.disposition != ObservationDisposition::Decoded,
            "Class A slot map: a decoded observation is not garble");
    (void)provenance_name(record.provenance);
    require_horizon(record.index, "Class A slot map: garble outside the map horizon");
    require(record.index <= now_index_, "Class A slot map: garble observed in the future");
    const auto existing = garble_.find(record.index);
    require(existing != garble_.end() || garble_.size() < limits_.max_garble,
            "Class A slot map: bounded garble capacity exceeded");
    garble_[record.index] = record;
}

bool LocalSlotMap::external_live(const ExternalAllocation& allocation, std::int64_t index,
                                 bool& aged_out) const noexcept {
    aged_out = false;
    const auto distance = distance_from(allocation.first_index, index);
    if (!distance || *distance % kSlotsPerFrame >= allocation.block_length) return false;
    const auto frame = frame_of_index(allocation.first_index) + *distance / kSlotsPerFrame;
    if (frame > allocation.reserved_through_minute) return false;
    if (sotdma_basis(allocation.basis) &&
        frame_of_index(now_index_) - allocation.last_use_minute > kSotdmaUnusedAgingFrames) {
        aged_out = true;
        return false;
    }
    return true;
}

SlotView LocalSlotMap::view(std::int64_t index) const {
    SlotView result;
    result.index = index;
    result.own = own_covering(index);
    const auto reach = (kMaxOwnTimeoutFrames + 1) * kSlotsPerFrame + kMaxBlockLength;
    const auto lower = external_.lower_bound(index >= kLowest + reach ? index - reach : kLowest);
    const auto upper = external_.upper_bound(index);
    for (auto iterator = lower; iterator != upper; ++iterator) {
        bool aged_out = false;
        if (external_live(iterator->second, index, aged_out)) result.live.push_back(&iterator->second);
        else if (aged_out) result.aged.push_back(&iterator->second);
    }
    std::sort(result.live.begin(), result.live.end(), canonical_before);
    std::sort(result.aged.begin(), result.aged.end(), canonical_before);
    const auto garble = garble_.find(index);
    if (garble != garble_.end()) result.garble = &garble->second;
    result.state = result.own ? SlotState::InternalOwn
                 : !result.live.empty() ? SlotState::ExternalObserved
                 : result.garble ? SlotState::GarbledObserved : SlotState::Free;
    return result;
}

std::vector<ExternalAllocation> LocalSlotMap::external_allocations() const {
    std::vector<ExternalAllocation> result;
    result.reserve(external_.size());
    for (const auto& entry : external_) result.push_back(entry.second);
    std::stable_sort(result.begin(), result.end(), [](const ExternalAllocation& a, const ExternalAllocation& b) {
        return canonical_before(&a, &b);
    });
    return result;
}

std::vector<GarbleRecord> LocalSlotMap::garbles() const {
    std::vector<GarbleRecord> result;
    result.reserve(garble_.size());
    for (const auto& entry : garble_) result.push_back(entry.second);
    return result;
}

std::vector<MapSnapshotRow> LocalSlotMap::snapshot(std::int64_t first_index, std::int64_t last_index) const {
    require(last_index >= first_index, "Class A slot map: snapshot range must be ascending");
    const auto width_limit = 16 * kSlotsPerFrame;
    const bool bounded = (first_index >= 0 || last_index < 0) ? last_index - first_index < width_limit
                                                               : last_index < width_limit + first_index;
    require(bounded, "Class A slot map: snapshot range must be shorter than sixteen frames");
    std::vector<MapSnapshotRow> rows;
    std::int64_t index = first_index;
    while (true) {
        const auto slot = view(index);
        if (!(slot.state == SlotState::Free && slot.aged.empty())) {
            MapSnapshotRow row;
            row.index = index;
            row.frame_slot = frame_slot(index);
            row.state = slot.state;
            std::string detail;
            if (slot.own) detail += "own:" + std::to_string(slot.own->first_index) + ";";
            for (const auto* allocation : slot.live)
                detail += "ext:" + std::to_string(allocation->mmsi) + ":" + allocation_basis_name(allocation->basis) +
                          ":through" + std::to_string(allocation->reserved_through_minute) + ":" +
                          provenance_name(allocation->provenance) + ";";
            for (const auto* allocation : slot.aged) detail += "aged:" + std::to_string(allocation->mmsi) + ";";
            if (slot.garble) detail += std::string("garble:") + provenance_name(slot.garble->provenance) + ";";
            row.detail = detail;
            rows.push_back(row);
        }
        if (index == last_index) break;  // Checked before the increment: last_index may be INT64_MAX.
        ++index;
    }
    return rows;
}

void LocalSlotMap::clear() noexcept {
    own_.clear();
    external_.clear();
    garble_.clear();
}

}  // namespace aismixer::lab::classa
