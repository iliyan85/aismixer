#pragma once

// Independent, integer-only test oracle; no CUDA/cuRAND headers or production
// RNG helpers are included. Algorithm: Philox4x32-10 (Random123), multipliers
// D2511F53/CD9E8D57 and Weyl increments 9E3779B9/BB67AE85. The three block
// known-answer vectors in geo_host_tests.cpp are portable algorithmic facts.
//
// The adapter below separately qualifies the CUDA 12.9 cuRAND convention,
// checked against the installed curand_kernel.h and curand_philox4x32_x.h:
//   key = (low32(seed), high32(seed));
//   counter = (low32(offset/4), high32(offset/4), low32(subseq), high32(subseq));
//   next scalar = Philox(counter,key)[offset%4].
// A subsequence therefore skips 2^66 scalar words; offset counts 32-bit words,
// not Philox blocks. This convention is toolchain-qualified, not a promise
// about future cuRAND versions. Tests never read installed headers or ignored
// audit files at runtime, and this oracle is not a production CPU fallback.

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>

namespace aismixer::lab::test_reference {

using PhiloxBlock = std::array<std::uint32_t, 4>;
using PhiloxKey = std::array<std::uint32_t, 2>;

inline PhiloxBlock philox4x32_10(PhiloxBlock counter, PhiloxKey key) {
    for (unsigned round = 0; round < 10; ++round) {
        const std::uint64_t even0 = UINT64_C(0xD2511F53) * counter[0];
        const std::uint64_t even2 = UINT64_C(0xCD9E8D57) * counter[2];
        const PhiloxBlock permuted{{
            static_cast<std::uint32_t>(even2 >> 32) ^ counter[1] ^ key[0],
            static_cast<std::uint32_t>(even2),
            static_cast<std::uint32_t>(even0 >> 32) ^ counter[3] ^ key[1],
            static_cast<std::uint32_t>(even0)}};
        counter = permuted;
        key[0] += UINT32_C(0x9E3779B9);
        key[1] += UINT32_C(0xBB67AE85);
    }
    return counter;
}

inline std::uint32_t curand_qualified_word(std::uint64_t seed,
                                          std::uint64_t subsequence,
                                          std::uint64_t offset) {
    const auto block = offset / 4;
    const PhiloxBlock counter{{static_cast<std::uint32_t>(block),
        static_cast<std::uint32_t>(block >> 32),
        static_cast<std::uint32_t>(subsequence),
        static_cast<std::uint32_t>(subsequence >> 32)}};
    const PhiloxKey key{{static_cast<std::uint32_t>(seed),
                         static_cast<std::uint32_t>(seed >> 32)}};
    return philox4x32_10(counter, key)[static_cast<std::size_t>(offset % 4)];
}

class PhiloxWords {
public:
    PhiloxWords(std::uint64_t seed, std::uint64_t subsequence)
        : seed_(seed), subsequence_(subsequence) {}

    std::uint32_t next() {
        if (exhausted_) throw std::runtime_error("reference scalar offset overflow");
        const auto result = curand_qualified_word(seed_, subsequence_, offset_);
        if (offset_ == std::numeric_limits<std::uint64_t>::max()) exhausted_ = true;
        else ++offset_;
        return result;
    }
private:
    std::uint64_t seed_;
    std::uint64_t subsequence_;
    std::uint64_t offset_ = 0;
    bool exhausted_ = false;
};

// Reference for the documented unbiased discrete bounding-box proposal.
// The first draw is the high half of the 64-bit value; the next is low.
// The finite 16-proposal limit is tested using injected words on the host.
template <typename Words>
std::int64_t bounding_integer(Words& words, std::int64_t minimum,
                              std::int64_t maximum) {
    if (minimum > maximum) throw std::invalid_argument("reference integer range is reversed");
    const auto span = static_cast<std::uint64_t>(maximum) -
                      static_cast<std::uint64_t>(minimum) + 1;
    if (span == 0 || span > static_cast<std::uint64_t>(INT64_MAX)) {
        throw std::invalid_argument("reference integer range exceeds signed mapping bound");
    }
    const auto threshold = (std::uint64_t{0} - span) % span;
    for (unsigned draw = 0; draw < 16; ++draw) {
        const std::uint64_t high = words.next();
        const std::uint64_t low = words.next();
        const auto value = high * UINT64_C(0x100000000) + low;
        if (value >= threshold) return minimum + static_cast<std::int64_t>(value % span);
    }
    throw std::runtime_error("reference unbiased integer proposal budget exhausted");
}

}  // namespace aismixer::lab::test_reference
