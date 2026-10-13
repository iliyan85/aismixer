#pragma once

#include <cstddef>
#include <cstdint>
#include <string_view>
#include <vector>

namespace aismixer::lab::ais {

// A codec safety limit, independent of the smaller lengths of B3 AIS types.
inline constexpr std::size_t kMaxPayloadBits = 4096;

// Appends individual 0/1 bits in wire order, most significant bit first.
// Invalid width, value or capacity throws std::invalid_argument before mutation.
// Widths 1..64 are supported, including the complete uint64/int64 range at 64.
class BitWriter {
public:
    void append_unsigned(std::uint64_t value, unsigned width,
                         std::string_view field);
    void append_signed(std::int64_t value, unsigned width,
                       std::string_view field);
    const std::vector<std::uint8_t>& bits() const noexcept { return bits_; }

private:
    void validate_capacity(unsigned width, std::string_view field) const;
    void append_validated(std::uint64_t value, unsigned width);

    std::vector<std::uint8_t> bits_;
};

}  // namespace aismixer::lab::ais
