#include "nmea0183.h"

#include "ais_bits.h"

#include <array>
#include <ostream>
#include <stdexcept>
#include <string>

namespace aismixer::lab::ais {
namespace {

[[noreturn]] void invalid(const char* reason) {
    throw std::invalid_argument(std::string("AIVDM: ") + reason);
}

unsigned armor_value(char ch) {
    const unsigned value = static_cast<unsigned char>(ch);
    if (value >= '0' && value <= 'W') {
        return value - '0';
    }
    if (value >= '`' && value <= 'w') {
        return value - '`' + 40;
    }
    invalid("payload contains a character outside AIS armoring");
}

void validate_payload(const EncodedPayload& payload) {
    if (payload.bits.empty() || payload.bits.size() > kMaxPayloadBits) {
        invalid("payload bit count must be in 1..4096");
    }
    if (payload.armored.empty() ||
        payload.armored.size() > (kMaxPayloadBits + 5) / 6) {
        invalid("armored payload exceeds the codec size limit or is empty");
    }
    if (payload.fill_bits > 5 ||
        payload.armored.size() * 6 - payload.fill_bits != payload.bits.size()) {
        invalid("fill count does not match the payload bit count");
    }
    std::size_t bit_index = 0;
    for (const char ch : payload.armored) {
        const unsigned value = armor_value(ch);
        for (unsigned remaining = 6; remaining > 0; --remaining, ++bit_index) {
            const unsigned bit = (value >> (remaining - 1)) & 1u;
            if (bit_index < payload.bits.size()) {
                if (payload.bits[bit_index] > 1 ||
                    payload.bits[bit_index] != bit) {
                    invalid("armoring does not match the 0/1 payload bits");
                }
            } else if (bit != 0) {
                invalid("armored padding bits must be zero");
            }
        }
    }
}

std::string sentence(unsigned total, unsigned part, std::string_view sequence,
                     char channel, std::string_view armored, unsigned fill) {
    std::string body = "AIVDM," + std::to_string(total) + "," +
                       std::to_string(part) + ",";
    body.append(sequence);
    body.push_back(',');
    body.push_back(channel);
    body.push_back(',');
    body.append(armored);
    body.push_back(',');
    body += std::to_string(fill);
    const unsigned check = checksum(body);
    constexpr char hex[] = "0123456789ABCDEF";
    std::string result = "!" + body + "*";
    result.push_back(hex[check >> 4]);
    result.push_back(hex[check & 15]);
    result += "\r\n";
    return result;
}

std::size_t payload_capacity(unsigned total, unsigned part,
                             std::string_view sequence, char channel,
                             unsigned fill) {
    // Derive capacity from the complete actual header and suffix, including
    // delimiters, ID, fill digit, checksum and CRLF rather than a magic chunk.
    const std::size_t overhead =
        sentence(total, part, sequence, channel, {}, fill).size();
    if (overhead >= kMaxNmeaSentenceBytes) {
        invalid("sentence header leaves no payload capacity");
    }
    return kMaxNmeaSentenceBytes - overhead;
}

std::array<std::string_view, 7> fields(std::string_view body) {
    std::array<std::string_view, 7> result{};
    std::size_t start = 0;
    for (std::size_t index = 0; index < result.size(); ++index) {
        const std::size_t end = body.find(',', start);
        if (index + 1 == result.size()) {
            if (end != std::string_view::npos) {
                invalid("sentence has extra comma-delimited fields");
            }
            result[index] = body.substr(start);
        } else {
            if (end == std::string_view::npos) {
                invalid("sentence has missing comma-delimited fields");
            }
            result[index] = body.substr(start, end - start);
            start = end + 1;
        }
    }
    return result;
}

unsigned decimal_digit(std::string_view value, char minimum, char maximum,
                       const char* reason) {
    if (value.size() != 1 || value[0] < minimum || value[0] > maximum) {
        invalid(reason);
    }
    return static_cast<unsigned>(value[0] - '0');
}

void validate_sentences(const std::vector<std::string>& sentences) {
    if (sentences.empty() || sentences.size() > kMaxNmeaFragments) {
        invalid("an ordered message must contain 1..9 sentences");
    }
    std::string_view sequence;
    char channel = '\0';
    std::size_t armored_count = 0;
    unsigned last_value = 0;
    unsigned final_fill = 0;
    for (std::size_t index = 0; index < sentences.size(); ++index) {
        const std::string& current = sentences[index];
        if (current.size() < 22 || current.size() > kMaxNmeaSentenceBytes ||
            current.front() != '!' ||
            current.compare(current.size() - 2, 2, "\r\n") != 0 ||
            current[current.size() - 5] != '*') {
            invalid("sentence must be canonical !AIVDM, bounded, and end CRLF");
        }
        const std::size_t star = current.size() - 5;
        const std::string_view body(current.data() + 1, star - 1);
        constexpr char hex[] = "0123456789ABCDEF";
        const unsigned check = checksum(body);
        if (current[star + 1] != hex[check >> 4] ||
            current[star + 2] != hex[check & 15]) {
            invalid("sentence checksum is not correct uppercase hexadecimal");
        }
        const auto parsed = fields(body);
        if (parsed[0] != "AIVDM" ||
            decimal_digit(parsed[1], '1', '9', "invalid fragment count") !=
                sentences.size() ||
            decimal_digit(parsed[2], '1', '9', "invalid fragment number") !=
                index + 1) {
            invalid("sentences are not a complete ordered AIVDM message");
        }
        if (sentences.size() == 1) {
            if (!parsed[3].empty()) {
                invalid("single-fragment sequence ID must be blank");
            }
        } else {
            decimal_digit(parsed[3], '0', '9', "invalid multipart sequence ID");
        }
        if (parsed[4].size() != 1 ||
            (parsed[4][0] != 'A' && parsed[4][0] != 'B')) {
            invalid("channel must be A or B");
        }
        if (index == 0) {
            sequence = parsed[3];
            channel = parsed[4][0];
        } else if (parsed[3] != sequence || parsed[4][0] != channel) {
            invalid("fragment sequence ID and channel must remain identical");
        }
        if (parsed[5].empty()) {
            invalid("fragment payload must not be empty");
        }
        for (const char ch : parsed[5]) {
            last_value = armor_value(ch);
        }
        armored_count += parsed[5].size();
        final_fill = decimal_digit(parsed[6], '0', '5', "invalid fill count");
        if (index + 1 != sentences.size() && final_fill != 0) {
            invalid("only the final fragment may have fill bits");
        }
    }
    if (armored_count * 6 - final_fill > kMaxPayloadBits ||
        (last_value & ((1u << final_fill) - 1u)) != 0) {
        invalid("message exceeds the bit limit or has nonzero padding");
    }
}

}  // namespace

std::uint8_t checksum(std::string_view body) noexcept {
    std::uint8_t result = 0;
    for (const char ch : body) {
        result ^= static_cast<std::uint8_t>(static_cast<unsigned char>(ch));
    }
    return result;
}

std::vector<std::string> format_aivdm(const EncodedPayload& payload, char channel,
                                     std::optional<unsigned> sequence_id) {
    if (channel != 'A' && channel != 'B') {
        invalid("channel must be A or B");
    }
    if (sequence_id && *sequence_id > 9) {
        invalid("sequence ID must be a decimal digit in 0..9");
    }
    validate_payload(payload);
    const std::size_t single_capacity =
        payload_capacity(1, 1, {}, channel, payload.fill_bits);
    if (payload.armored.size() <= single_capacity) {
        return {sentence(1, 1, {}, channel, payload.armored, payload.fill_bits)};
    }
    if (!sequence_id) {
        invalid("multipart payload requires an explicit sequence ID");
    }
    const std::string sequence = std::to_string(*sequence_id);
    unsigned total = 2;
    for (; total <= kMaxNmeaFragments; ++total) {
        std::size_t capacity = 0;
        for (unsigned part = 1; part <= total; ++part) {
            capacity += payload_capacity(total, part, sequence, channel,
                                         part == total ? payload.fill_bits : 0);
        }
        if (payload.armored.size() <= capacity) {
            break;
        }
    }
    if (total > kMaxNmeaFragments) {
        invalid("payload requires more than nine NMEA fragments");
    }
    std::vector<std::string> result;
    result.reserve(total);
    std::size_t offset = 0;
    for (unsigned part = 1; part <= total; ++part) {
        const unsigned fill = part == total ? payload.fill_bits : 0;
        const std::size_t capacity =
            payload_capacity(total, part, sequence, channel, fill);
        const std::size_t remaining = payload.armored.size() - offset;
        const std::size_t count = remaining < capacity ? remaining : capacity;
        result.push_back(sentence(total, part, sequence, channel,
                                  std::string_view(payload.armored).substr(offset, count),
                                  fill));
        offset += count;
    }
    return result;
}

std::vector<std::string> encode_aivdm(const Message& message, char channel,
                                     std::optional<unsigned> sequence_id) {
    return format_aivdm(encode(message), channel, sequence_id);
}

void write_sentences(std::ostream& output,
                     const std::vector<std::string>& sentences) {
    validate_sentences(sentences);
    for (const std::string& current : sentences) {
        output.write(current.data(), static_cast<std::streamsize>(current.size()));
        if (!output) {
            throw std::runtime_error("AIVDM: ostream write failed");
        }
    }
}

}  // namespace aismixer::lab::ais
