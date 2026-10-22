#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace aismixer::lab::classa {

// Generic NON-ENTRY RATDMA bookkeeping arithmetic for A1-S4 (A2-3.3.4.2,
// Table 14; A0-MAC-019..021/036/041). LCP-P03 replenishment and LCP-P04
// exact rational thresholds/inclusive integer draws are SIMULATION_POLICY.
// This is a pure helper: it has no clock, no candidate map, no priority queue
// and no transmitter. A WouldRequest outcome is a bookkeeping statement, never
// an on-air transmission, a confirmed TX or an RF result. Network entry must
// not use this probability gate (A2-3.3.4.2.1 exception, A0-MAC-018).
//
// Representation (RATDMA-EXACT-GAP-v1, SIMULATION_POLICY): the inhibition gap
// q = 100 - p obeys q' = q * (C - 1) / C on every inhibition (derived from
// p' = p + (100 - p)/C) and q0 = 100 * (C0 - 1) / C0. Peer removals between
// inhibitions break the telescoping of the C values, so q is kept exactly as
// a signed prime-exponent vector over the 35 primes <= 149 and rendered on
// demand with bounded big integers. No float, no rounding, no clamping and
// no 64-bit overflow exist on any lawful sequence of at most 150 attempts.
inline constexpr std::uint32_t kGenericRatdmaPolicyVersion = 1;
inline constexpr const char* kGenericRatdmaArithmeticId = "RATDMA-EXACT-GAP-v1";
inline constexpr std::uint32_t kRatdmaMinimumInitialCandidates = 4;    // Table 14 note 1.
inline constexpr std::uint32_t kRatdmaMaximumCandidates = 150;         // Table 14 RTCSC maximum.
inline constexpr std::int64_t kRatdmaMaximumIntervalSlots = 150;       // Table 14 RTES: 150 slots ahead.
inline constexpr std::uint32_t kRatdmaMaximumDraw = 100;               // LCP-P04 inclusive 0..100.
inline constexpr std::uint32_t kRatdmaMinimumCandidateObjective = 4;   // LCP-P03 sum C+A < 4.
inline constexpr std::size_t kRatdmaGapPrimeCount = 35;                // Primes 2..149.

// Exact nonnegative rational in canonical reduced form, rendered as decimal
// digit strings so that values wider than 64 bits stay exact and reviewable.
struct ExactRational {
    std::string numerator = "0";
    std::string denominator = "1";
};
bool operator==(const ExactRational& a, const ExactRational& b) noexcept;
bool operator!=(const ExactRational& a, const ExactRational& b) noexcept;
// Reduced canonical form of numerator/denominator; throws on a zero denominator.
ExactRational make_rational(std::uint64_t numerator, std::uint64_t denominator);

// q = 100 - p as a product of prime powers (zero flag for p == 100).
struct RatdmaGap {
    bool zero = false;
    std::array<std::int16_t, kRatdmaGapPrimeCount> exponents{};
};
ExactRational render_gap(const RatdmaGap& gap);          // q, reduced.
ExactRational render_probability(const RatdmaGap& gap);  // p = 100 - q, reduced.
// LCP-P04 comparison u <= p evaluated exactly as q <= 100 - u.
bool ratdma_permits(const RatdmaGap& gap, std::uint32_t draw);

struct RatdmaBookkeeping {
    std::int64_t creation_index = 0;         // Absolute slot index when the message became due.
    std::int64_t original_rtes_index = 0;    // Fixed absolute deadline: never extended.
    std::uint32_t initial_candidates = 0;    // C_initial >= 4.
    std::vector<std::int64_t> candidates;    // Ascending distinct absolute indices in (creation, rtes].
    std::uint32_t attempts = 0;              // RTA.
    RatdmaGap gap;                           // Authoritative exact state of 100 - RTP2.
    ExactRational probability;               // RTP2 rendering; refreshed by every function here, never edited.
    std::vector<std::int64_t> attempted;     // Candidates consumed by attempts, ascending.
    std::vector<std::int64_t> removed;       // Candidates taken by peer allocations; never re-added (policy).
};

// Candidates must be ascending, distinct, within (creation, rtes], at least
// four and at most 150; rtes - creation must be 1..150 (checked arithmetic).
// p starts at 100/C.
RatdmaBookkeeping start_generic_ratdma(std::int64_t creation_index, std::int64_t original_rtes_index,
                                       std::vector<std::int64_t> candidates);
std::uint32_t candidate_count(const RatdmaBookkeeping& state) noexcept;

enum class RatdmaAttemptOutcome : std::uint8_t { WouldRequest = 1, Inhibited = 2 };
struct RatdmaAttemptResult {
    RatdmaAttemptOutcome outcome = RatdmaAttemptOutcome::Inhibited;
    std::int64_t candidate_index = 0;   // The earliest remaining candidate, consumed either way.
    std::uint32_t draw = 0;             // u in 0..100 inclusive.
    ExactRational threshold_before;     // p compared against u.
    ExactRational delta;                // (100 - p)/C from pre-update values; 0/1 when permitted.
    ExactRational probability_after;    // p' = p + delta on inhibition, else unchanged.
    std::uint32_t candidates_after = 0;
    std::uint32_t attempts_after = 0;
};
// Apply the p-persistent comparison at the earliest remaining candidate with
// an externally drawn inclusive integer u: WouldRequest iff u <= p. On
// inhibition delta = (100 - p)/C uses pre-update values, then C -= 1, A += 1.
// Throws if u > 100 or no candidate remains; validation precedes mutation.
RatdmaAttemptResult ratdma_attempt(RatdmaBookkeeping& state, std::uint32_t draw);
// A peer allocation removes its candidate without an attempt or probability change.
bool remove_peer_candidate(RatdmaBookkeeping& state, std::int64_t slot_index);

struct RatdmaReplenishment {
    bool needed = false;            // C + A < 4 before the call.
    std::uint32_t added = 0;
    std::uint32_t candidates_after = 0;
    std::uint32_t shortfall = 0;    // Remaining distance to the sum objective, if any.
    std::string status;             // not_needed | replenished | partial | no_eligible.
};
// LCP-P03: only when C + A < 4, add distinct eligible candidates (ascending)
// strictly after current_index and at or before the original RTES, excluding
// candidates already present, already attempted or removed by a peer
// allocation, until C + A == 4 or the finite eligible list is exhausted.
// Never moves RTES or fabricates a slot.
RatdmaReplenishment replenish_candidates(RatdmaBookkeeping& state, std::int64_t current_index,
                                         const std::vector<std::int64_t>& eligible_ascending);

// Pending: candidates remain before the deadline. Exhausted: no candidate
// remains before the original RTES (LCP-P03 "blocked/pending until an
// eligible causal change": replenishment may return the state to Pending;
// it is not terminal). DeadlinePassed: the original RTES has passed without
// a lawful request (LCP-P03 "exhausted/missed", terminal).
enum class RatdmaStatus : std::uint8_t { Pending = 1, Exhausted = 2, DeadlinePassed = 3 };
RatdmaStatus ratdma_status(const RatdmaBookkeeping& state, std::int64_t current_index) noexcept;
const char* ratdma_status_name(RatdmaStatus status) noexcept;

}  // namespace aismixer::lab::classa
