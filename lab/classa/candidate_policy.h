#pragma once

#include "local_slot_map.h"
#include "philox_protocol.h"

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace aismixer::lab::classa {

// Pure, reviewable candidate functions over ONE station's own two maps and
// directory (A2-3.3.1.2, A2-3.1.6, A2-4.4.1, Figures 9/20/21). They consume
// caller-supplied intended channel, nominal slot, nominal increment, block
// length and instant; they never schedule, select an NSS from global truth
// or decide first-frame timing (A0-ISS-003 stays open). SIMULATION_POLICY
// (CANDIDATE-POLICY-v1):
//  - FUTURE-ONLY candidates: a block containing the decision slot
//    (index <= now_index) is never a regular candidate, because a transmission
//    must start at a slot boundary that has not yet passed; the Figure 9
//    current-slot fallback is reported explicitly instead.
//  - FRAME-CONTAINED blocks: a multi-slot candidate lies within one UTC frame
//    (A0-MAC-013 expected evidence: blocks crossing the frame boundary are
//    rejected).
//  - MOST-DISTANT pool: the reuse distance that earns Available1 is the
//    maximum over externally held slots, on either channel, that could still
//    belong to a candidate (index after the decision slot and not blocked on
//    the selection channel); slots that can never be chosen do not steal the
//    label.
//  - In-tier order before the four-candidate cut: Free1/Available1 before
//    Free2/Available2 on the selection channel then the other channel, larger
//    selection-channel reuse distance first, then larger other-channel reuse
//    distance, then lower index.
//  - A multi-slot block reusing several stations names them all; every one
//    is excluded from further intentional reuse for one frame.
//  - Aged SOTDMA evidence (Free2) is kept when the only live holders are
//    beyond 120 NM; the lowest MMSI wins an equal-distance nearest tie; an
//    indeterminate geodesic range (wgs84-vincenty-bounded-v1 beyond 2500 km)
//    yields Unavailable; exactly 120 NM is within range; the conservative
//    own-position-unavailable rule protects every held slot.
inline constexpr std::uint32_t kCandidatePolicyVersion = 1;
inline constexpr std::uint32_t kMinimumCandidateObjective = 4;   // A2-3.3.1.2 / A2-4.4.1.
inline constexpr std::int64_t kReuseExclusionSlots = kSlotsPerFrame;  // A2-4.4.1 one frame.

struct SelectionInterval {
    std::int64_t ns_index = 0;     // Absolute nominal slot.
    std::int64_t ni = 0;           // Exact integer nominal increment.
    std::int64_t half_width = 0;   // floor(NI / 10).
    std::int64_t first_index = 0;  // NS - half_width.
    std::int64_t last_index = 0;   // NS + half_width.
    std::uint16_t width = 0;       // 2 * half_width + 1.
};
// LCP-P01: the rational NI must reduce exactly to 75, 125, 225 or 375.
// Any other value (for example assigned-mode 75/2) is rejected explicitly as
// outside the qualified autonomous domain; nothing is rounded.
SelectionInterval selection_interval(std::int64_t ns_index, std::int64_t ni_numerator,
                                     std::int64_t ni_denominator = 1);

struct OwnBlock {
    std::int64_t first_index = 0;
    std::uint8_t block_length = 1;
    std::int64_t retained_frames = 0;
};
// LCP-P05: ascending distinct absolute indices j-1, j, j+1 for every slot j
// occupied by an own block on the other channel, in every retained frame.
// Absolute indices: at frame slot 0 the previous frame's slot 2249 is
// excluded, at 2249 the next frame's slot 0; the oracle reading "modulo
// 2250" denotes exactly this absolute-neighbour set.
std::vector<std::int64_t> own_adjacency_exclusions(const std::vector<OwnBlock>& own_blocks_on_other_channel);
std::vector<OwnBlock> own_blocks(const LocalSlotMap& map);

enum class SlotClass : std::uint8_t {
    Free1 = 1,            // Unused, beyond 120 NM, garbled (Class A) or no live evidence.
    Free2 = 2,            // SOTDMA allocation unused during the preceding three frames.
    Available1 = 3,       // Reusable, held by a most-distant eligible mobile.
    Available2 = 4,       // Reusable, held by another eligible mobile.
    Unavailable = 5,      // Protected: base within 120 NM, positionless peer, recent reuse, unresolved range.
    OwnAllocated = 6,
    OwnAdjacentExcluded = 7
};
const char* slot_class_name(SlotClass value);
bool free_class(SlotClass value) noexcept;
bool available_class(SlotClass value) noexcept;

struct ClassifiedSlot {
    std::int64_t index = 0;
    SlotClass value = SlotClass::Free1;
    std::optional<std::uint32_t> peer;
    std::optional<std::int64_t> peer_distance_um;
    std::string reason;
};

struct ReuseExclusion {
    std::uint32_t mmsi = 0;
    std::int64_t excluded_until_index = 0;  // Exclusive.
};

struct CandidateContext {
    Channel selection_channel = Channel::Ais1;
    std::uint8_t block_length = 1;
    std::int64_t now_index = 0;
    std::optional<world::Point> own_position;        // Reuse and range rules need it.
    std::optional<std::int64_t> current_own_slot_index;  // Figure 9 fallback input.
    std::vector<ReuseExclusion> reuse_exclusions;
    std::uint64_t knowledge_revision = 0;            // Copied into the report for freshness checks.
    std::uint64_t own_inputs_fingerprint = 0;        // Copied into the report: the own inputs it was built from.
};

struct Candidate {
    std::int64_t first_index = 0;
    std::uint16_t frame_slot = 0;
    std::uint8_t tier = 0;  // 0 free/free; 1..5 Rule 1..5.
    SlotClass selection_class = SlotClass::Free1;
    SlotClass other_class = SlotClass::Free1;
    std::optional<std::uint32_t> reuse_peer;          // Holder of the block's worst reusable selection slot.
    std::optional<std::int64_t> reuse_distance_um;
    std::vector<std::uint32_t> reuse_peers;           // Every distinct station reused on the selection channel, ascending.
    std::uint32_t admission_rank = 0;  // Order of admission before canonical sorting.
};
enum class CandidateFallback : std::uint8_t { None = 0, CurrentSlotUsable = 1, NoReservation = 2 };
const char* fallback_name(CandidateFallback value);

struct CandidateReport {
    OwnIdentity owner;                              // The maps' and directory's owner.
    std::int64_t now_index = 0;                     // Decision slot the report was built at.
    std::uint64_t knowledge_revision = 0;           // Knowledge state revision the report was built from.
    std::uint64_t own_inputs_fingerprint = 0;       // Own inputs (fix, position, clock sample) the report was built from.
    SelectionInterval interval;
    Channel selection_channel = Channel::Ais1;
    std::uint8_t block_length = 1;
    std::vector<ClassifiedSlot> selection_classes;  // One per SI slot, ascending.
    std::vector<ClassifiedSlot> other_classes;      // Same indices on the other channel.
    std::vector<std::int64_t> adjacency_exclusions;
    std::optional<std::int64_t> most_distant_um;
    std::uint32_t free_both_count = 0;
    std::vector<Candidate> candidates;              // Canonical ascending by first_index.
    bool reached_objective = false;
    std::string shortfall_reason;
    CandidateFallback fallback = CandidateFallback::None;
    std::uint64_t digest = 0;                       // FNV-1a64 of the canonical candidate serialization.
};
// Both maps and the directory must share one owner, be advanced to the same
// decision slot and cover the selection interval within their horizons.
CandidateReport build_candidates(const LocalSlotMap& selection_map, const LocalSlotMap& other_map,
                                 const PeerDirectory& directory, const CandidateContext& context,
                                 const SelectionInterval& interval);
std::string canonical_candidate_text(const CandidateReport& report);

struct CandidateChoice {
    bool chosen = false;
    Candidate candidate;
    DrawLedgerEntry ledger;
};
// Equiprobable choice over the canonical candidate order; throws if empty.
CandidateChoice choose_candidate(const CandidateReport& report, const DrawKey& key);

// LCP-P02: continuous SOTDMA visit decrements exactly once before the zero test.
struct TimeoutVisit {
    std::uint8_t advertised_timeout = 0;
    bool reselect = false;  // Advertised zero: the old slot expires; a new NTS gets its own 3..7.
};
TimeoutVisit continuous_timeout_visit(std::uint8_t current_timeout);          // current 1..7.
std::vector<std::uint8_t> continuous_timeout_sequence(std::uint8_t initial);  // initial 3..7.
// A drawn continuous timeout T (3..7) is used in exactly T frames: the first
// use advertises T-1, so the own reservation retains T-1 additional frames.
std::uint8_t retained_frames_from_timeout_draw(std::int64_t drawn_timeout);

struct ExpiryAnnouncement {
    std::int64_t old_index = 0;
    std::int64_t new_index = 0;
    std::int64_t slot_offset = 0;  // new - old = 2250 + new_slot - old_slot.
    ais::SotdmaState19 state;
};
// Zero-timeout announcement for the expiring OLD slot pointing at the NEW
// next-frame slot; the new reservation's own timeout is separate.
ExpiryAnnouncement expiry_announcement(std::int64_t old_index, std::int64_t new_index, ais::SyncState sync);

enum class RangeClass : std::uint8_t { Within120Nm = 1, Beyond120Nm = 2 };
RangeClass range_class(std::int64_t distance_um) noexcept;  // Exactly 120 NM is within.
// AIS 1/10000-minute coordinates to integer nanodegrees, nearest (x*5000/3).
world::Point ais_position_to_nanodegrees(std::int64_t longitude_ais, std::int64_t latitude_ais);

}  // namespace aismixer::lab::classa
