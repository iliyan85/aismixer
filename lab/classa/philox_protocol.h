#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace aismixer::lab::classa {

// CLASSA-PHILOX-v1 is SIMULATION_POLICY (A0.1 LCP-P08 / A0-SIM-016). The source
// requires random, equiprobable protocol choices; it prescribes neither this
// generator, this key layout nor this bounded sampler. This is a portable host
// Philox4x32-10, not cuRAND's host generator, not std::mt19937 and not W1's
// frozen purpose domain: W1 keys seed XOR entity and counts a draw cursor,
// whereas this contract keys the raw scenario seed and packs semantic
// coordinates, so no W1 stream position is reused for any purpose number.
inline constexpr std::uint32_t kProtocolRngVersion = 1;
inline constexpr const char* kProtocolRngId = "CLASSA-PHILOX-v1";
inline constexpr unsigned kMaxDrawProposals = 64;
inline constexpr std::uint64_t kMaxPowerGenerationKey = 0xFFFFFF;  // 24-bit field.
inline constexpr std::uint64_t kMaxEpisodeKey = 0xFFFFFFFF;        // 32-bit field.
inline constexpr std::uint64_t kMaxDrawIndexKey = 0xFFFF;          // 16-bit field.
inline constexpr std::uint32_t kMaxRejectionIndexKey = 0xFF;       // 8-bit field.

using PhiloxBlock = std::array<std::uint32_t, 4>;
using PhiloxKey = std::array<std::uint32_t, 2>;

// Exactly ten rounds; multipliers 0xD2511F53 and 0xCD9E8D57; Weyl increments
// 0x9E3779B9 and 0xBB67AE85 applied to the key after every round.
PhiloxBlock philox4x32_10(PhiloxBlock counter, PhiloxKey key) noexcept;

// Purpose numbers 101..105 are separated from W1 Purpose 1..16 by counter
// construction, not merely by numbering: W1 packs draw/4 (< 2^30) into
// counter[2] and version|purpose into counter[3], whereas this contract packs
// purpose << 24 (>= 0x65000000) into counter[2]. Even when W1 keys seed XOR
// entity with entity 0 (equal to the raw seed) no W1 tuple coincides with a
// protocol tuple. IntentionalReusePeer (105) is reserved for S4: an
// equiprobable draw over the canonical ascending MMSI set of peers that tie
// at the maximum eligible reuse distance; S3's own hierarchy breaks such
// ties by ascending slot index and never consumes this purpose itself.
enum class ProtocolPurpose : std::uint8_t {
    EntryNominalStartSlot = 101,
    CandidateChoice = 102,
    ContinuousTimeout = 103,
    GenericRatdmaProbability = 104,
    IntentionalReusePeer = 105
};
const char* purpose_name(ProtocolPurpose purpose);  // Throws on an unknown value.

struct DrawKey {
    std::uint64_t scenario_seed = 0;
    std::uint32_t equipment_id = 0;      // Stable nonzero OWN W1 equipment identity.
    std::uint64_t power_generation = 0;  // 1..kMaxPowerGenerationKey.
    ProtocolPurpose purpose = ProtocolPurpose::CandidateChoice;
    std::uint64_t episode = 0;           // 0..kMaxEpisodeKey semantic decision index.
    std::uint64_t draw_index = 0;        // 0..kMaxDrawIndexKey draw within the episode.
};
bool operator==(const DrawKey& a, const DrawKey& b) noexcept;

struct DrawPosition {
    PhiloxKey key{};
    PhiloxBlock counter{};
};
// Fixed-width bijective packing without hashing: key = seed low/high words;
// counter[0] = equipment; counter[1] = episode; counter[2] = generation[23:0]
// | purpose << 24; counter[3] = draw[15:0] | rejection << 16 | version << 24.
// Every field is domain-checked before packing; a violation throws.
DrawPosition pack_draw(const DrawKey& key, std::uint32_t rejection_index);
// The six packed words in order key[0], key[1], counter[0..3], each as four
// little-endian bytes: the reviewable 24-byte serialization of a draw position.
std::array<std::uint8_t, 24> serialize_draw_position(const DrawPosition& position) noexcept;

struct DrawProposal {
    std::uint32_t rejection_index = 0;
    PhiloxBlock words{};
    bool accepted = false;
};
struct DrawLedgerEntry {
    std::uint32_t protocol_version = kProtocolRngVersion;
    DrawKey key;
    std::int64_t minimum = 0;
    std::int64_t maximum = 0;
    std::uint64_t span = 0;              // n = maximum - minimum + 1.
    std::uint64_t acceptance_limit = 0;  // floor(2^32 / n) * n.
    std::vector<DrawProposal> proposals;
    std::uint32_t rejections = 0;
    std::uint32_t accepted_word = 0;
    std::int64_t value = 0;
    std::uint64_t input_digest = 0;      // Caller's canonical semantic input digest.
    std::string outcome;
};

std::uint64_t acceptance_limit(std::uint64_t span) noexcept;
// Pure: accepted iff word < acceptance_limit(span); index = word % span.
bool accept_word(std::uint32_t word, std::uint64_t span, std::uint64_t& index) noexcept;
// Pure inclusive bounded draw: n in [1, 2^32]; one Philox block per explicit
// increasing rejection index; word 0 of each block is the proposal. Throws
// std::invalid_argument on a domain error and std::runtime_error if the finite
// proposal budget is exhausted. No state is read or mutated.
DrawLedgerEntry bounded_draw(const DrawKey& key, std::int64_t minimum, std::int64_t maximum,
                             std::uint64_t input_digest = 0, std::string outcome = {});
// FNV-1a 64 reproducibility fingerprint for canonical semantic inputs; not authentication.
std::uint64_t fnv1a64(const std::uint8_t* bytes, std::size_t count) noexcept;
std::uint64_t fnv1a64(const std::string& text) noexcept;

}  // namespace aismixer::lab::classa
