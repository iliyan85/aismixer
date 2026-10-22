#include "philox_protocol.h"

#include <limits>
#include <stdexcept>
#include <utility>

namespace aismixer::lab::classa {
namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::invalid_argument(message);
}
}  // namespace

PhiloxBlock philox4x32_10(PhiloxBlock counter, PhiloxKey key) noexcept {
    for (unsigned round = 0; round < 10; ++round) {
        const std::uint64_t product0 = UINT64_C(0xD2511F53) * counter[0];
        const std::uint64_t product2 = UINT64_C(0xCD9E8D57) * counter[2];
        const PhiloxBlock next{{
            static_cast<std::uint32_t>(product2 >> 32) ^ counter[1] ^ key[0],
            static_cast<std::uint32_t>(product2),
            static_cast<std::uint32_t>(product0 >> 32) ^ counter[3] ^ key[1],
            static_cast<std::uint32_t>(product0)}};
        counter = next;
        key[0] += UINT32_C(0x9E3779B9);
        key[1] += UINT32_C(0xBB67AE85);
    }
    return counter;
}

const char* purpose_name(ProtocolPurpose purpose) {
    switch (purpose) {
    case ProtocolPurpose::EntryNominalStartSlot: return "entry_nominal_start_slot";
    case ProtocolPurpose::CandidateChoice: return "candidate_choice";
    case ProtocolPurpose::ContinuousTimeout: return "continuous_timeout";
    case ProtocolPurpose::GenericRatdmaProbability: return "generic_ratdma_probability";
    case ProtocolPurpose::IntentionalReusePeer: return "intentional_reuse_peer";
    }
    throw std::invalid_argument("CLASSA-PHILOX-v1: unknown protocol purpose");
}

bool operator==(const DrawKey& a, const DrawKey& b) noexcept {
    return a.scenario_seed == b.scenario_seed && a.equipment_id == b.equipment_id &&
           a.power_generation == b.power_generation && a.purpose == b.purpose &&
           a.episode == b.episode && a.draw_index == b.draw_index;
}

DrawPosition pack_draw(const DrawKey& key, std::uint32_t rejection_index) {
    require(key.equipment_id != 0, "CLASSA-PHILOX-v1: zero equipment identity is not a stable owner");
    require(key.power_generation >= 1 && key.power_generation <= kMaxPowerGenerationKey,
            "CLASSA-PHILOX-v1: power generation outside 1..16777215 key domain");
    require(key.episode <= kMaxEpisodeKey, "CLASSA-PHILOX-v1: episode outside 32-bit key domain");
    require(key.draw_index <= kMaxDrawIndexKey, "CLASSA-PHILOX-v1: draw index outside 16-bit key domain");
    require(rejection_index <= kMaxRejectionIndexKey, "CLASSA-PHILOX-v1: rejection index outside 8-bit key domain");
    (void)purpose_name(key.purpose);
    DrawPosition position;
    position.key = {{static_cast<std::uint32_t>(key.scenario_seed),
                     static_cast<std::uint32_t>(key.scenario_seed >> 32)}};
    position.counter = {{key.equipment_id,
                         static_cast<std::uint32_t>(key.episode),
                         static_cast<std::uint32_t>(key.power_generation) |
                             (static_cast<std::uint32_t>(key.purpose) << 24),
                         static_cast<std::uint32_t>(key.draw_index) |
                             (rejection_index << 16) | (kProtocolRngVersion << 24)}};
    return position;
}

std::array<std::uint8_t, 24> serialize_draw_position(const DrawPosition& position) noexcept {
    std::array<std::uint8_t, 24> bytes{};
    const std::uint32_t words[6] = {position.key[0], position.key[1], position.counter[0],
                                    position.counter[1], position.counter[2], position.counter[3]};
    for (std::size_t word = 0; word < 6; ++word) {
        for (std::size_t octet = 0; octet < 4; ++octet) {
            bytes[word * 4 + octet] = static_cast<std::uint8_t>(words[word] >> (8 * octet));
        }
    }
    return bytes;
}

std::uint64_t acceptance_limit(std::uint64_t span) noexcept {
    if (span == 0 || span > (UINT64_C(1) << 32)) return 0;
    return ((UINT64_C(1) << 32) / span) * span;
}

bool accept_word(std::uint32_t word, std::uint64_t span, std::uint64_t& index) noexcept {
    const auto limit = acceptance_limit(span);
    if (limit == 0 || word >= limit) return false;
    index = word % span;
    return true;
}

DrawLedgerEntry bounded_draw(const DrawKey& key, std::int64_t minimum, std::int64_t maximum,
                             std::uint64_t input_digest, std::string outcome) {
    require(minimum <= maximum, "CLASSA-PHILOX-v1: reversed inclusive draw range");
    const std::uint64_t difference =
        static_cast<std::uint64_t>(maximum) - static_cast<std::uint64_t>(minimum);
    require(difference <= 0xFFFFFFFFu, "CLASSA-PHILOX-v1: inclusive range exceeds 2^32 values");
    DrawLedgerEntry entry;
    entry.key = key;
    entry.minimum = minimum;
    entry.maximum = maximum;
    entry.span = difference + 1;
    entry.acceptance_limit = acceptance_limit(entry.span);
    entry.input_digest = input_digest;
    entry.outcome = std::move(outcome);
    for (std::uint32_t rejection = 0; rejection < kMaxDrawProposals; ++rejection) {
        const auto position = pack_draw(key, rejection);
        DrawProposal proposal;
        proposal.rejection_index = rejection;
        proposal.words = philox4x32_10(position.counter, position.key);
        std::uint64_t index = 0;
        proposal.accepted = accept_word(proposal.words[0], entry.span, index);
        entry.proposals.push_back(proposal);
        if (proposal.accepted) {
            entry.rejections = rejection;
            entry.accepted_word = proposal.words[0];
            entry.value = static_cast<std::int64_t>(static_cast<std::uint64_t>(minimum) + index);
            return entry;
        }
    }
    throw std::runtime_error("CLASSA-PHILOX-v1: finite rejection proposal budget exhausted");
}

std::uint64_t fnv1a64(const std::uint8_t* bytes, std::size_t count) noexcept {
    std::uint64_t hash = UINT64_C(14695981039346656037);
    for (std::size_t index = 0; index < count; ++index) {
        hash ^= bytes[index];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}

std::uint64_t fnv1a64(const std::string& text) noexcept {
    return fnv1a64(reinterpret_cast<const std::uint8_t*>(text.data()), text.size());
}

}  // namespace aismixer::lab::classa
