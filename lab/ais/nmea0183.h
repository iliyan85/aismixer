#pragma once

#include "ais_encoder.h"

#include <cstddef>
#include <cstdint>
#include <iosfwd>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace aismixer::lab::ais {

inline constexpr std::size_t kMaxNmeaSentenceBytes = 82;  // Includes CRLF.
inline constexpr unsigned kMaxNmeaFragments = 9;

// XOR exactly the body bytes between '!' and '*'; delimiters are not supplied.
std::uint8_t checksum(std::string_view body) noexcept;

// EncodedPayload must have coherent bits, armoring and zero-valued fill bits.
// Channel is exactly A or B; sequence ID is a caller-supplied decimal 0..9.
// Single fragments always use a blank sequence ID. Multipart requires an ID,
// uses at most nine ordered fragments, and sets fill only on the final one.
// All returned strings include CRLF. Validation completes before any output.
std::vector<std::string> format_aivdm(
    const EncodedPayload& payload, char channel,
    std::optional<unsigned> sequence_id = std::nullopt);
std::vector<std::string> encode_aivdm(
    const Message& message, char channel,
    std::optional<unsigned> sequence_id = std::nullopt);

// Validates the entire ordered message before writing canonical CRLF bytes.
// The stream must use binary mode where a host text stream translates LF
// (notably Windows stdout/files). An I/O failure throws std::runtime_error;
// already-written bytes cannot be rolled back by a general ostream.
void write_sentences(std::ostream& output,
                     const std::vector<std::string>& sentences);

}  // namespace aismixer::lab::ais
