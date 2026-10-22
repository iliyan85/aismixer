#include "generic_ratdma_policy.h"

#include <algorithm>
#include <limits>
#include <numeric>
#include <stdexcept>

namespace aismixer::lab::classa {
namespace {
constexpr auto kHigh = std::numeric_limits<std::int64_t>::max();
constexpr std::array<std::uint32_t, kRatdmaGapPrimeCount> kPrimes = {
    2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97,
    101, 103, 107, 109, 113, 127, 131, 137, 139, 149};

void require(bool condition, const char* message) {
    if (!condition) throw std::invalid_argument(message);
}

// Bounded nonnegative big integer: little-endian base 2^32 limbs without
// leading zero limbs; an empty limb vector is zero. Only the operations the
// gap arithmetic needs exist (multiply/divide by a small word, subtract,
// compare, decimal rendering); every value is bounded by 100 * 150^151.
struct Big {
    std::vector<std::uint32_t> limbs;
};
void trim(Big& value) {
    while (!value.limbs.empty() && value.limbs.back() == 0) value.limbs.pop_back();
}
Big big_one() { return Big{{1u}}; }
void mul_small(Big& value, std::uint32_t factor) {
    if (factor == 0) { value.limbs.clear(); return; }
    std::uint64_t carry = 0;
    for (auto& limb : value.limbs) {
        const std::uint64_t product = static_cast<std::uint64_t>(limb) * factor + carry;
        limb = static_cast<std::uint32_t>(product);
        carry = product >> 32;
    }
    if (carry != 0) value.limbs.push_back(static_cast<std::uint32_t>(carry));
}
std::uint32_t divmod_small(Big& value, std::uint32_t divisor) {
    std::uint64_t remainder = 0;
    for (std::size_t index = value.limbs.size(); index-- > 0;) {
        const std::uint64_t current = (remainder << 32) | value.limbs[index];
        value.limbs[index] = static_cast<std::uint32_t>(current / divisor);
        remainder = current % divisor;
    }
    trim(value);
    return static_cast<std::uint32_t>(remainder);
}
int compare(const Big& a, const Big& b) noexcept {
    if (a.limbs.size() != b.limbs.size()) return a.limbs.size() < b.limbs.size() ? -1 : 1;
    for (std::size_t index = a.limbs.size(); index-- > 0;)
        if (a.limbs[index] != b.limbs[index]) return a.limbs[index] < b.limbs[index] ? -1 : 1;
    return 0;
}
Big subtract(const Big& a, const Big& b) {  // Requires a >= b.
    Big result = a;
    std::int64_t borrow = 0;
    for (std::size_t index = 0; index < result.limbs.size(); ++index) {
        std::int64_t value = static_cast<std::int64_t>(result.limbs[index]) - borrow -
                             (index < b.limbs.size() ? static_cast<std::int64_t>(b.limbs[index]) : 0);
        if (value < 0) { value += (INT64_C(1) << 32); borrow = 1; } else borrow = 0;
        result.limbs[index] = static_cast<std::uint32_t>(value);
    }
    trim(result);
    return result;
}
std::string to_decimal(Big value) {
    if (value.limbs.empty()) return "0";
    std::string text;
    while (!value.limbs.empty()) {
        std::string chunk = std::to_string(divmod_small(value, 1000000000u));
        if (!value.limbs.empty()) chunk.insert(0, 9 - chunk.size(), '0');
        text.insert(0, chunk);
    }
    return text;
}

// Multiply the exponent vector by value^sign; value is 1..150 (Table 14).
void accumulate(RatdmaGap& gap, std::uint32_t value, int sign) {
    require(value >= 1 && value <= kRatdmaMaximumCandidates, "generic RATDMA: gap factor outside 1..150");
    for (std::size_t index = 0; index < kPrimes.size() && value > 1; ++index) {
        while (value % kPrimes[index] == 0) {
            value /= kPrimes[index];
            gap.exponents[index] = static_cast<std::int16_t>(gap.exponents[index] + sign);
        }
    }
}
Big power_product(const RatdmaGap& gap, bool positive) {
    Big product = big_one();
    for (std::size_t index = 0; index < kPrimes.size(); ++index) {
        const int exponent = gap.exponents[index];
        const int count = positive ? (exponent > 0 ? exponent : 0) : (exponent < 0 ? -exponent : 0);
        for (int step = 0; step < count; ++step) mul_small(product, kPrimes[index]);
    }
    return product;
}
Big gap_numerator(const RatdmaGap& gap) { return power_product(gap, true); }
Big gap_denominator(const RatdmaGap& gap) { return power_product(gap, false); }

RatdmaGap initial_gap(std::uint32_t initial_candidates) {
    RatdmaGap gap;
    accumulate(gap, 100, +1);
    accumulate(gap, initial_candidates - 1, +1);
    accumulate(gap, initial_candidates, -1);
    return gap;
}
RatdmaGap inhibited_gap(RatdmaGap gap, std::uint32_t count) {
    if (gap.zero) return gap;
    if (count == 1) { gap.zero = true; gap.exponents.fill(0); return gap; }
    accumulate(gap, count - 1, +1);
    accumulate(gap, count, -1);
    return gap;
}
ExactRational render_delta(const RatdmaGap& gap, std::uint32_t count) {  // q / C reduced.
    if (gap.zero) return ExactRational{};
    Big numerator = gap_numerator(gap);
    Big denominator = gap_denominator(gap);
    Big probe = numerator;
    const auto remainder = divmod_small(probe, count);
    const auto divisor = std::gcd(remainder, count);
    if (divisor > 1) (void)divmod_small(numerator, divisor);
    mul_small(denominator, count / divisor);
    return ExactRational{to_decimal(numerator), to_decimal(denominator)};
}
void refresh(RatdmaBookkeeping& state) { state.probability = render_probability(state.gap); }
}  // namespace

bool operator==(const ExactRational& a, const ExactRational& b) noexcept {
    return a.numerator == b.numerator && a.denominator == b.denominator;
}
bool operator!=(const ExactRational& a, const ExactRational& b) noexcept { return !(a == b); }

ExactRational make_rational(std::uint64_t numerator, std::uint64_t denominator) {
    require(denominator != 0, "RATDMA rational: zero denominator");
    if (numerator == 0) return ExactRational{};
    const auto divisor = std::gcd(numerator, denominator);
    return ExactRational{std::to_string(numerator / divisor), std::to_string(denominator / divisor)};
}

ExactRational render_gap(const RatdmaGap& gap) {
    if (gap.zero) return ExactRational{};
    return ExactRational{to_decimal(gap_numerator(gap)), to_decimal(gap_denominator(gap))};
}

ExactRational render_probability(const RatdmaGap& gap) {
    if (gap.zero) return ExactRational{"100", "1"};
    const Big numerator = gap_numerator(gap);
    const Big denominator = gap_denominator(gap);
    Big hundred = denominator;
    mul_small(hundred, 100);
    require(compare(numerator, hundred) < 0, "generic RATDMA: gap state exceeds 100");
    // gcd(100 D - N, D) == gcd(N, D) == 1 because N and D have disjoint prime support.
    return ExactRational{to_decimal(subtract(hundred, numerator)), to_decimal(denominator)};
}

bool ratdma_permits(const RatdmaGap& gap, std::uint32_t draw) {
    require(draw <= kRatdmaMaximumDraw, "generic RATDMA: draw must be an inclusive integer 0..100");
    if (gap.zero) return true;
    const Big numerator = gap_numerator(gap);
    Big bound = gap_denominator(gap);
    mul_small(bound, kRatdmaMaximumDraw - draw);
    return compare(numerator, bound) <= 0;  // q <= 100 - u  <=>  u <= p.
}

RatdmaBookkeeping start_generic_ratdma(std::int64_t creation_index, std::int64_t original_rtes_index,
                                       std::vector<std::int64_t> candidates) {
    const auto latest_rtes = creation_index > kHigh - kRatdmaMaximumIntervalSlots
                                 ? kHigh : creation_index + kRatdmaMaximumIntervalSlots;
    require(original_rtes_index > creation_index && original_rtes_index <= latest_rtes,
            "generic RATDMA: RTES must be 1..150 slots after the creation slot");
    require(candidates.size() >= kRatdmaMinimumInitialCandidates &&
            candidates.size() <= kRatdmaMaximumCandidates,
            "generic RATDMA: initial candidate set must contain 4..150 slots");
    require(std::is_sorted(candidates.begin(), candidates.end()) &&
            std::adjacent_find(candidates.begin(), candidates.end()) == candidates.end(),
            "generic RATDMA: candidates must be ascending and distinct");
    require(candidates.front() > creation_index && candidates.back() <= original_rtes_index,
            "generic RATDMA: candidates must lie after creation and at or before the original RTES");
    RatdmaBookkeeping state;
    state.creation_index = creation_index;
    state.original_rtes_index = original_rtes_index;
    state.initial_candidates = static_cast<std::uint32_t>(candidates.size());
    state.candidates = std::move(candidates);
    state.gap = initial_gap(state.initial_candidates);
    refresh(state);
    return state;
}

std::uint32_t candidate_count(const RatdmaBookkeeping& state) noexcept {
    return static_cast<std::uint32_t>(state.candidates.size());
}

RatdmaAttemptResult ratdma_attempt(RatdmaBookkeeping& state, std::uint32_t draw) {
    require(draw <= kRatdmaMaximumDraw, "generic RATDMA: draw must be an inclusive integer 0..100");
    if (state.candidates.empty())
        throw std::runtime_error("generic RATDMA: no candidate remains; explicit exhausted status required");
    RatdmaAttemptResult result;
    result.candidate_index = state.candidates.front();
    result.draw = draw;
    result.threshold_before = render_probability(state.gap);
    const auto count = candidate_count(state);
    const bool permitted = ratdma_permits(state.gap, draw);
    RatdmaGap next = state.gap;
    if (!permitted) {
        result.delta = render_delta(state.gap, count);
        next = inhibited_gap(state.gap, count);
    }
    // All arithmetic succeeded; mutate only now.
    state.attempted.push_back(state.candidates.front());
    state.candidates.erase(state.candidates.begin());
    if (!permitted) {
        state.gap = next;
        state.attempts += 1;
    }
    refresh(state);
    result.outcome = permitted ? RatdmaAttemptOutcome::WouldRequest : RatdmaAttemptOutcome::Inhibited;
    result.probability_after = state.probability;
    result.candidates_after = candidate_count(state);
    result.attempts_after = state.attempts;
    return result;
}

bool remove_peer_candidate(RatdmaBookkeeping& state, std::int64_t slot_index) {
    const auto found = std::find(state.candidates.begin(), state.candidates.end(), slot_index);
    if (found == state.candidates.end()) return false;
    state.candidates.erase(found);
    state.removed.insert(std::upper_bound(state.removed.begin(), state.removed.end(), slot_index), slot_index);
    refresh(state);
    return true;
}

RatdmaReplenishment replenish_candidates(RatdmaBookkeeping& state, std::int64_t current_index,
                                         const std::vector<std::int64_t>& eligible_ascending) {
    require(std::is_sorted(eligible_ascending.begin(), eligible_ascending.end()),
            "generic RATDMA: eligible replenishment list must be ascending");
    require(current_index >= state.creation_index,
            "generic RATDMA: replenishment cannot precede the creation slot (candidates stay within (creation, RTES])");
    RatdmaReplenishment result;
    const auto sum = static_cast<std::uint64_t>(candidate_count(state)) + state.attempts;
    result.needed = sum < kRatdmaMinimumCandidateObjective;
    if (!result.needed) {
        result.candidates_after = candidate_count(state);
        result.status = "not_needed";
        return result;
    }
    auto remaining = kRatdmaMinimumCandidateObjective - static_cast<std::uint32_t>(sum);
    std::vector<std::int64_t> additions;
    for (const auto slot : eligible_ascending) {
        if (remaining == 0) break;
        if (slot <= current_index || slot > state.original_rtes_index) continue;
        if (std::binary_search(state.candidates.begin(), state.candidates.end(), slot)) continue;
        if (std::find(state.attempted.begin(), state.attempted.end(), slot) != state.attempted.end()) continue;
        if (std::binary_search(state.removed.begin(), state.removed.end(), slot)) continue;
        if (!additions.empty() && additions.back() == slot) continue;
        additions.push_back(slot);
        --remaining;
    }
    for (const auto slot : additions) {
        state.candidates.insert(std::upper_bound(state.candidates.begin(), state.candidates.end(), slot), slot);
    }
    refresh(state);
    result.added = static_cast<std::uint32_t>(additions.size());
    result.candidates_after = candidate_count(state);
    result.shortfall = remaining;
    result.status = additions.empty() ? "no_eligible" : remaining == 0 ? "replenished" : "partial";
    return result;
}

RatdmaStatus ratdma_status(const RatdmaBookkeeping& state, std::int64_t current_index) noexcept {
    if (current_index > state.original_rtes_index) return RatdmaStatus::DeadlinePassed;
    if (state.candidates.empty()) return RatdmaStatus::Exhausted;
    return RatdmaStatus::Pending;
}

const char* ratdma_status_name(RatdmaStatus status) noexcept {
    switch (status) {
    case RatdmaStatus::Pending: return "pending";
    case RatdmaStatus::Exhausted: return "exhausted";
    case RatdmaStatus::DeadlinePassed: return "deadline_passed";
    }
    return "unknown";
}

}  // namespace aismixer::lab::classa
