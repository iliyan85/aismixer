#include "ais_bits.h"

#include <stdexcept>
#include <string>

namespace aismixer::lab::ais {
namespace {

[[noreturn]] void invalid(std::string_view field, const char* reason) {
    throw std::invalid_argument(std::string(field) + ": " + reason);
}

}  // namespace

void BitWriter::validate_capacity(unsigned width, std::string_view field) const {
    if (width == 0 || width > 64) {
        invalid(field, "bit width must be in 1..64");
    }
    if (width > kMaxPayloadBits - bits_.size()) {
        invalid(field, "payload exceeds the 4096-bit codec limit");
    }
}

void BitWriter::append_validated(std::uint64_t value, unsigned width) {
    // Reserve before appending: allocation failure also leaves prior bits intact.
    bits_.reserve(bits_.size() + width);
    for (unsigned remaining = width; remaining > 0; --remaining) {
        bits_.push_back(static_cast<std::uint8_t>((value >> (remaining - 1)) & 1u));
    }
}

void BitWriter::append_unsigned(std::uint64_t value, unsigned width,
                                std::string_view field) {
    validate_capacity(width, field);
    if (width < 64 && value >= (std::uint64_t{1} << width)) {
        invalid(field, "unsigned value does not fit the field width");
    }
    append_validated(value, width);
}

void BitWriter::append_signed(std::int64_t value, unsigned width,
                              std::string_view field) {
    validate_capacity(width, field);
    if (width < 64) {
        const std::int64_t limit = std::int64_t{1} << (width - 1);
        if (value < -limit || value >= limit) {
            invalid(field, "signed value does not fit the field width");
        }
    }
    // Conversion to unsigned is defined modulo 2^64, so negative values yield
    // the low-width two's-complement representation without signed shifts.
    append_validated(static_cast<std::uint64_t>(value), width);
}

}  // namespace aismixer::lab::ais
