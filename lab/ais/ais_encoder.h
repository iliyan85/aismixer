#pragma once

#include "ais_message.h"

#include <cstdint>
#include <string>
#include <vector>

namespace aismixer::lab::ais {

// Bits contain one 0/1 byte per AIS bit in MSB-first wire order. They do not
// include armoring fill bits, NMEA framing, TAG blocks, or radio-layer padding.
struct EncodedPayload {
    std::vector<std::uint8_t> bits;
    std::string armored;
    std::uint8_t fill_bits;
};

// Low-level diagnostic helper for bounded, nonempty 0/1 vectors. It validates
// every bit before armoring; typed AIS input should use encode() below.
EncodedPayload armor_bits(const std::vector<std::uint8_t>& bits);

// Validate the complete typed input, serialize exact standard fields, and armor
// six-bit groups. Invalid values throw std::invalid_argument with the field
// name; no truncation, clock, RNG, GPU, or scenario-derived metadata is used.
EncodedPayload encode(const Type1& message);
EncodedPayload encode(const Type4& message);
EncodedPayload encode(const Type9& message);
EncodedPayload encode(const Type18& message);
EncodedPayload encode(const Type21& message);
EncodedPayload encode(const Message& message);

}  // namespace aismixer::lab::ais
