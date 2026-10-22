#include "candidate_policy.h"

#include <algorithm>
#include <numeric>
#include <set>
#include <stdexcept>

namespace aismixer::lab::classa {
namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::invalid_argument(message);
}
unsigned severity(SlotClass value) noexcept { return static_cast<unsigned>(value); }
bool blocking_class(SlotClass value) noexcept { return severity(value) >= severity(SlotClass::Unavailable); }

// Classify one slot on one channel from the owner's map/directory only.
ClassifiedSlot classify(const LocalSlotMap& map, const PeerDirectory& directory,
                        const CandidateContext& context, std::int64_t index, bool apply_adjacency,
                        const std::vector<std::int64_t>& adjacency) {
    ClassifiedSlot result;
    result.index = index;
    if (apply_adjacency && std::binary_search(adjacency.begin(), adjacency.end(), index)) {
        result.value = SlotClass::OwnAdjacentExcluded;
        result.reason = "own transmission on the other channel within one slot (LCP-P05)";
        return result;
    }
    const auto view = map.view(index);
    if (view.own) {
        result.value = SlotClass::OwnAllocated;
        result.reason = "internally allocated";
        return result;
    }
    if (view.live.empty()) {
        if (!view.aged.empty()) {
            result.value = SlotClass::Free2;
            result.reason = "SOTDMA allocation unused during the preceding three frames (A2-3.1.6)";
        } else if (view.garble) {
            result.value = SlotClass::Free1;
            result.reason = "garbled energy without decode; Class A treats as free (A2-3.1.6)";
        } else {
            result.value = SlotClass::Free1;
            result.reason = "no live evidence";
        }
        return result;
    }
    bool any_within = false;
    std::optional<std::int64_t> nearest;
    std::optional<std::uint32_t> nearest_peer;
    for (const auto* allocation : view.live) {
        const auto* peer = directory.find(allocation->mmsi);
        const bool base = allocation->kind == PeerKind::Base || (peer && peer->kind == PeerKind::Base);
        if (!context.own_position) {
            result.value = SlotClass::Unavailable;
            result.reason = "own position unavailable; reuse and 120 NM rules cannot be evaluated (A2-4.4.1)";
            result.peer = allocation->mmsi;
            return result;
        }
        if (!peer) {
            result.value = SlotClass::Unavailable;
            result.peer = allocation->mmsi;
            result.reason = "peer directory record expired while its allocation is live: range unknown, protected";
            return result;
        }
        if (!peer->position_available) {
            result.value = SlotClass::Unavailable;
            result.peer = allocation->mmsi;
            result.reason = base ? "base station without usable position: protected"
                                 : "mobile peer reporting no position: not reusable (A2-4.4.1)";
            return result;
        }
        if (!peer->position_in_geodesic_domain) {
            result.value = SlotClass::Unavailable;
            result.peer = allocation->mmsi;
            result.reason = "peer position outside the qualified geodesic domain: range indeterminate";
            return result;
        }
        std::int64_t distance = 0;
        try {
            distance = world::geodesic_inverse(*context.own_position, peer->position).distance_um;
        } catch (const std::exception&) {
            result.value = SlotClass::Unavailable;
            result.peer = allocation->mmsi;
            result.reason = "geodesic range indeterminate: fail closed";
            return result;
        }
        if (range_class(distance) == RangeClass::Beyond120Nm) continue;  // Free by distance.
        if (base) {
            result.value = SlotClass::Unavailable;
            result.peer = allocation->mmsi;
            result.peer_distance_um = distance;
            result.reason = "base station within 120 NM: protected (A2-3.3.4.3.1 / A2-4.4.1)";
            return result;
        }
        for (const auto& exclusion : context.reuse_exclusions) {
            if (exclusion.mmsi == allocation->mmsi && context.now_index < exclusion.excluded_until_index) {
                result.value = SlotClass::Unavailable;
                result.peer = allocation->mmsi;
                result.peer_distance_um = distance;
                result.reason = "peer already subject to intentional reuse within one frame (A2-4.4.1)";
                return result;
            }
        }
        any_within = true;
        // Nearest live peer holds the slot; an equal-distance tie goes to the lowest MMSI (policy).
        if (!nearest || distance < *nearest || (distance == *nearest && allocation->mmsi < *nearest_peer)) {
            nearest = distance;
            nearest_peer = allocation->mmsi;
        }
    }
    if (!any_within) {
        if (!view.aged.empty()) {
            result.value = SlotClass::Free2;
            result.reason = "SOTDMA allocation unused during the preceding three frames; live holders beyond 120 NM (A2-3.1.6)";
        } else {
            result.value = SlotClass::Free1;
            result.reason = "every live allocation is held beyond 120 NM (A2-3.1.6)";
        }
        return result;
    }
    result.value = SlotClass::Available2;  // Available1 is assigned after the most-distant pass.
    result.peer = nearest_peer;
    result.peer_distance_um = nearest;
    result.reason = "externally allocated by an eligible mobile within 120 NM";
    return result;
}

std::uint8_t tier_of(SlotClass selection, SlotClass other) noexcept {
    const bool selection_free = free_class(selection);
    const bool other_free = free_class(other);
    const bool other_available = available_class(other);
    if (selection_free && other_free) return 0;
    if (selection_free && other_available) return 1;
    if (!selection_free && other_free) return 2;
    if (!selection_free && other_available) return 3;
    if (selection_free) return 4;
    return 5;
}
unsigned subrank_of(SlotClass selection, SlotClass other) noexcept {
    // Free1 before Free2 and Available1 before Available2 on each channel
    // (Figure 20 priorities No.1/2/5/6 and No.3/4); selection channel first.
    return (severity(selection) % 2 == 0 ? 2u : 0u) + (severity(other) % 2 == 0 ? 1u : 0u);
}
void require_within_horizon(const LocalSlotMap& map, const SelectionInterval& interval, std::int64_t now_index) {
    const auto& limits = map.limits();
    require(interval.first_index >= offset_index(now_index, -limits.past_frames * kSlotsPerFrame) &&
            interval.last_index <= offset_index(now_index, limits.future_frames * kSlotsPerFrame),
            "Class A candidate policy: selection interval outside the local map horizon");
}
}  // namespace

SelectionInterval selection_interval(std::int64_t ns_index, std::int64_t ni_numerator,
                                     std::int64_t ni_denominator) {
    require(ni_denominator > 0 && ni_numerator > 0, "LCP-P01: nominal increment must be a positive rational");
    const auto divisor = std::gcd(ni_numerator, ni_denominator);
    const auto numerator = ni_numerator / divisor;
    const auto denominator = ni_denominator / divisor;
    if (denominator != 1)
        throw std::invalid_argument("LCP-P01: fractional nominal increment (for example assigned-mode 37.5) is outside the qualified autonomous domain; rejected, not rounded");
    if (numerator != 75 && numerator != 125 && numerator != 225 && numerator != 375)
        throw std::invalid_argument("LCP-P01: nominal increment outside the qualified autonomous set {75,125,225,375}; rejected");
    SelectionInterval interval;
    interval.ns_index = ns_index;
    interval.ni = numerator;
    interval.half_width = numerator / 10;
    interval.first_index = offset_index(ns_index, -interval.half_width);
    interval.last_index = offset_index(ns_index, interval.half_width);
    interval.width = static_cast<std::uint16_t>(2 * interval.half_width + 1);
    return interval;
}

std::vector<std::int64_t> own_adjacency_exclusions(const std::vector<OwnBlock>& blocks) {
    std::set<std::int64_t> excluded;
    for (const auto& block : blocks) {
        require(block.block_length >= 1 && block.block_length <= kMaxBlockLength && block.retained_frames >= 0 &&
                block.retained_frames <= kMaxOwnTimeoutFrames, "LCP-P05: own block outside 1..5 slots / 0..7 frames");
        for (std::int64_t frame = 0; frame <= block.retained_frames; ++frame) {
            for (std::uint8_t offset = 0; offset < block.block_length; ++offset) {
                const auto occupied = offset_index(block.first_index, frame * kSlotsPerFrame + offset);
                excluded.insert(offset_index(occupied, -1));
                excluded.insert(occupied);
                excluded.insert(offset_index(occupied, 1));
            }
        }
    }
    return std::vector<std::int64_t>(excluded.begin(), excluded.end());
}

std::vector<OwnBlock> own_blocks(const LocalSlotMap& map) {
    std::vector<OwnBlock> result;
    for (const auto& reservation : map.own_reservations())
        result.push_back({reservation.first_index, reservation.block_length, own_retained_frames(reservation)});
    return result;
}

const char* slot_class_name(SlotClass value) {
    switch (value) {
    case SlotClass::Free1: return "free1";
    case SlotClass::Free2: return "free2";
    case SlotClass::Available1: return "available1";
    case SlotClass::Available2: return "available2";
    case SlotClass::Unavailable: return "unavailable";
    case SlotClass::OwnAllocated: return "own";
    case SlotClass::OwnAdjacentExcluded: return "own_adjacent_excluded";
    }
    throw std::invalid_argument("Class A candidate policy: unknown slot class");
}
bool free_class(SlotClass value) noexcept { return value == SlotClass::Free1 || value == SlotClass::Free2; }
bool available_class(SlotClass value) noexcept { return value == SlotClass::Available1 || value == SlotClass::Available2; }

const char* fallback_name(CandidateFallback value) {
    switch (value) {
    case CandidateFallback::None: return "none";
    case CandidateFallback::CurrentSlotUsable: return "current_slot_usable";
    case CandidateFallback::NoReservation: return "no_reservation";
    }
    throw std::invalid_argument("Class A candidate policy: unknown fallback");
}

CandidateReport build_candidates(const LocalSlotMap& selection_map, const LocalSlotMap& other_map,
                                 const PeerDirectory& directory, const CandidateContext& context,
                                 const SelectionInterval& interval) {
    require(selection_map.owner() == other_map.owner() && selection_map.owner() == directory.owner(),
            "Class A candidate policy: maps and directory must belong to one owner");
    require(selection_map.channel() == context.selection_channel &&
            other_map.channel() == other_channel(context.selection_channel),
            "Class A candidate policy: maps must be this station's selection and other channel");
    require(context.block_length >= 1 && context.block_length <= kMaxBlockLength,
            "Class A candidate policy: block length must be 1..5");
    require(interval.width >= 1 && interval.last_index >= interval.first_index &&
            interval.last_index - interval.first_index == static_cast<std::int64_t>(interval.width) - 1,
            "Class A candidate policy: malformed selection interval");
    require(selection_map.advanced() && other_map.advanced() && selection_map.now_index() == context.now_index &&
            other_map.now_index() == context.now_index, "Class A candidate policy: maps are not at the decision instant");
    require(interval.last_index > context.now_index, "Class A candidate policy: selection interval entirely at or before the decision slot");
    require_within_horizon(selection_map, interval, context.now_index);
    require_within_horizon(other_map, interval, context.now_index);
    if (context.own_position) world::validate_point(*context.own_position);
    CandidateReport report;
    report.owner = selection_map.owner();
    report.now_index = context.now_index;
    report.knowledge_revision = context.knowledge_revision;
    report.own_inputs_fingerprint = context.own_inputs_fingerprint;
    report.interval = interval;
    report.selection_channel = context.selection_channel;
    report.block_length = context.block_length;
    report.adjacency_exclusions = own_adjacency_exclusions(own_blocks(other_map));
    for (std::int64_t index = interval.first_index; index <= interval.last_index; ++index) {
        report.selection_classes.push_back(classify(selection_map, directory, context, index, true, report.adjacency_exclusions));
        report.other_classes.push_back(classify(other_map, directory, context, index, false, {}));
    }
    // Most distant eligible reuse peer(s) over the slots that could still be
    // chosen: after the decision slot and not blocked on the selection channel.
    const auto count = static_cast<std::size_t>(interval.width);
    for (std::size_t position = 0; position < count; ++position) {
        const auto& selection = report.selection_classes[position];
        const auto& other = report.other_classes[position];
        if (selection.index <= context.now_index || blocking_class(selection.value)) continue;
        for (const auto* slot : {&selection, &other}) {
            if (slot->value == SlotClass::Available2 && slot->peer_distance_um &&
                (!report.most_distant_um || *slot->peer_distance_um > *report.most_distant_um))
                report.most_distant_um = slot->peer_distance_um;
        }
    }
    for (auto* classes : {&report.selection_classes, &report.other_classes}) {
        for (auto& slot : *classes) {
            if (slot.value == SlotClass::Available2 && report.most_distant_um && slot.peer_distance_um == report.most_distant_um)
                slot.value = SlotClass::Available1;
        }
    }
    struct Pending {
        Candidate candidate;
        unsigned subrank;
        std::int64_t selection_distance;
        std::int64_t other_distance;
    };
    std::vector<Pending> pending[6];
    for (std::size_t start = 0; start + context.block_length <= count; ++start) {
        bool usable = true;
        SlotClass selection_worst = SlotClass::Free1;
        SlotClass other_worst = SlotClass::Free1;
        std::optional<std::uint32_t> peer;
        std::optional<std::int64_t> distance;
        std::int64_t other_distance = 0;
        std::set<std::uint32_t> reuse_peers;
        const auto first = report.selection_classes[start].index;
        const auto last = report.selection_classes[start + context.block_length - 1].index;
        if (frame_of_index(first) != frame_of_index(last)) continue;  // Frame-contained blocks only (A0-MAC-013).
        for (std::size_t offset = 0; offset < context.block_length && usable; ++offset) {
            const auto& selection = report.selection_classes[start + offset];
            const auto& other = report.other_classes[start + offset];
            // Future-only: the decision slot itself and every earlier slot are never candidates.
            if (blocking_class(selection.value) || selection.index <= context.now_index) { usable = false; break; }
            if (severity(selection.value) > severity(selection_worst)) {
                selection_worst = selection.value;
                peer = selection.peer;
                distance = selection.peer_distance_um;
            }
            if (available_class(selection.value) && selection.peer) reuse_peers.insert(*selection.peer);
            const auto other_value = severity(other.value) >= severity(SlotClass::Unavailable) ? SlotClass::Unavailable : other.value;
            if (severity(other_value) > severity(other_worst)) {
                other_worst = other_value;
                other_distance = available_class(other_value) ? other.peer_distance_um.value_or(0) : 0;
            }
        }
        if (!usable) continue;
        Pending entry;
        entry.candidate.first_index = first;
        entry.candidate.frame_slot = frame_slot(first);
        entry.candidate.tier = tier_of(selection_worst, other_worst);
        entry.candidate.selection_class = selection_worst;
        entry.candidate.other_class = other_worst;
        if (available_class(selection_worst)) {
            entry.candidate.reuse_peer = peer;
            entry.candidate.reuse_distance_um = distance;
            entry.candidate.reuse_peers.assign(reuse_peers.begin(), reuse_peers.end());
        }
        entry.subrank = subrank_of(selection_worst, other_worst);
        entry.selection_distance = available_class(selection_worst) ? distance.value_or(0) : 0;
        entry.other_distance = other_distance;
        pending[entry.candidate.tier].push_back(entry);
    }
    std::vector<Candidate> admitted;
    std::uint32_t rank = 0;
    for (unsigned tier = 0; tier < 6; ++tier) {
        auto& group = pending[tier];
        std::stable_sort(group.begin(), group.end(), [](const Pending& a, const Pending& b) {
            if (a.subrank != b.subrank) return a.subrank < b.subrank;
            if (a.selection_distance != b.selection_distance) return a.selection_distance > b.selection_distance;  // Most distant reuse first.
            if (a.other_distance != b.other_distance) return a.other_distance > b.other_distance;
            return a.candidate.first_index < b.candidate.first_index;
        });
        for (const auto& entry : group) {
            if (tier > 0 && admitted.size() >= kMinimumCandidateObjective) break;
            Candidate candidate = entry.candidate;
            candidate.admission_rank = rank++;
            admitted.push_back(candidate);
        }
        if (tier == 0) report.free_both_count = static_cast<std::uint32_t>(admitted.size());
    }
    std::sort(admitted.begin(), admitted.end(), [](const Candidate& a, const Candidate& b) {
        return a.first_index < b.first_index;
    });
    report.candidates = std::move(admitted);
    report.reached_objective = report.candidates.size() >= kMinimumCandidateObjective;
    if (!report.reached_objective) {
        report.shortfall_reason = "hierarchy exhausted with " + std::to_string(report.candidates.size()) +
                                  " legal candidate(s); no fourth candidate is manufactured";
    }
    if (report.candidates.empty()) {
        report.fallback = context.current_own_slot_index && selection_map.own_covering(*context.current_own_slot_index) &&
                          *context.current_own_slot_index >= context.now_index
                              ? CandidateFallback::CurrentSlotUsable : CandidateFallback::NoReservation;
    }
    report.digest = fnv1a64(canonical_candidate_text(report));
    return report;
}

std::string canonical_candidate_text(const CandidateReport& report) {
    std::string text = std::string(channel_name(report.selection_channel)) + "|L" +
                       std::to_string(report.block_length) + "|NS" + std::to_string(report.interval.ns_index) +
                       "|NI" + std::to_string(report.interval.ni);
    for (const auto& candidate : report.candidates) {
        std::string peers;
        for (const auto peer : candidate.reuse_peers) peers += (peers.empty() ? "" : "+") + std::to_string(peer);
        text += "|" + std::to_string(candidate.first_index) + ":" + std::to_string(candidate.tier) + ":" +
                slot_class_name(candidate.selection_class) + ":" + slot_class_name(candidate.other_class) + ":" +
                (peers.empty() ? std::string("-") : peers);
    }
    return text;
}

CandidateChoice choose_candidate(const CandidateReport& report, const DrawKey& key) {
    require(!report.candidates.empty(), "Class A candidate policy: no candidate to choose from; use the explicit fallback result");
    require(key.purpose == ProtocolPurpose::CandidateChoice, "Class A candidate policy: candidate choice needs its own purpose");
    CandidateChoice choice;
    choice.ledger = bounded_draw(key, 0, static_cast<std::int64_t>(report.candidates.size()) - 1, report.digest,
                                 "candidate_choice");
    choice.candidate = report.candidates[static_cast<std::size_t>(choice.ledger.value)];
    choice.chosen = true;
    choice.ledger.outcome = "candidate_choice:" + std::to_string(choice.candidate.first_index);
    return choice;
}

TimeoutVisit continuous_timeout_visit(std::uint8_t current_timeout) {
    require(current_timeout >= 1 && current_timeout <= kMaxOwnTimeoutFrames,
            "LCP-P02: a retained continuous allocation has timeout 1..7 before its visit");
    TimeoutVisit visit;
    visit.advertised_timeout = static_cast<std::uint8_t>(current_timeout - 1);
    visit.reselect = visit.advertised_timeout == 0;
    return visit;
}

std::vector<std::uint8_t> continuous_timeout_sequence(std::uint8_t initial) {
    require(initial >= 3 && initial <= kMaxOwnTimeoutFrames, "LCP-P02: initial continuous timeout is 3..7 inclusive");
    std::vector<std::uint8_t> sequence;
    std::uint8_t current = initial;
    while (true) {
        const auto visit = continuous_timeout_visit(current);
        sequence.push_back(visit.advertised_timeout);
        if (visit.reselect) break;
        current = visit.advertised_timeout;
    }
    return sequence;
}

std::uint8_t retained_frames_from_timeout_draw(std::int64_t drawn_timeout) {
    require(drawn_timeout >= 3 && drawn_timeout <= kMaxOwnTimeoutFrames, "LCP-P02: drawn continuous timeout is 3..7 inclusive");
    return static_cast<std::uint8_t>(drawn_timeout - 1);
}

ExpiryAnnouncement expiry_announcement(std::int64_t old_index, std::int64_t new_index, ais::SyncState sync) {
    require(frame_of_index(new_index) == frame_of_index(old_index) + 1,
            "LCP-P02: the replacement NTS must lie in the next frame (A2-3.3.5.4.3)");
    ExpiryAnnouncement announcement{old_index, new_index, new_index - old_index,
        ais::SotdmaState19::offset_in_next_frame(sync, frame_slot(old_index), frame_slot(new_index))};
    const auto offset = std::get<ais::SotdmaSlotOffset>(announcement.state.submessage()).offset;
    require(offset == announcement.slot_offset, "LCP-P02: next-frame offset relation mismatch");
    return announcement;
}

RangeClass range_class(std::int64_t distance_um) noexcept {
    return distance_um > kReuseRangeMicrometres ? RangeClass::Beyond120Nm : RangeClass::Within120Nm;
}

world::Point ais_position_to_nanodegrees(std::int64_t longitude_ais, std::int64_t latitude_ais) {
    require(longitude_ais >= -108000000 && longitude_ais <= 108000000 && latitude_ais >= -54000000 &&
            latitude_ais <= 54000000, "Class A candidate policy: AIS position outside +/-180/+/-90 degrees");
    const auto convert = [](std::int64_t value) {
        const auto scaled = value * 5000;  // |value| <= 1.08e8, product fits int64.
        auto quotient = scaled / 3;
        const auto remainder = scaled % 3;
        if (remainder == 2) ++quotient;
        else if (remainder == -2) --quotient;
        return quotient;
    };
    return {convert(longitude_ais), convert(latitude_ais)};
}

}  // namespace aismixer::lab::classa
