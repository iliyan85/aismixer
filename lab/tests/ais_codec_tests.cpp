#include "ais_bits.h"
#include "ais_encoder.h"
#include "nmea0183.h"

#include <algorithm>
#include <cstdint>
#include <iostream>
#include <limits>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

namespace {

using namespace aismixer::lab::ais;

std::size_t rejected_cases = 0;

void require(bool condition, std::string_view message) {
    if (!condition) {
        throw std::runtime_error(std::string(message));
    }
}

template <typename Action>
void invalid(Action action) {
    bool rejected = false;
    try {
        action();
    } catch (const std::invalid_argument& error) {
        rejected = true;
        require(std::string_view(error.what()).size() > 5,
                "invalid input needs an actionable diagnostic");
    }
    require(rejected, "invalid input was accepted");
    ++rejected_cases;
}

std::string bit_string(const std::vector<std::uint8_t>& bits) {
    std::string result;
    for (const auto bit : bits) {
        require(bit <= 1, "nonbinary output bit");
        result.push_back(static_cast<char>('0' + bit));
    }
    return result;
}

std::uint64_t read_unsigned(const std::vector<std::uint8_t>& bits,
                            std::size_t offset, unsigned width) {
    require(width > 0 && width <= 64 && offset + width <= bits.size(),
            "test decoder field outside payload");
    std::uint64_t value = 0;
    for (unsigned index = 0; index < width; ++index) {
        value = value * 2 + bits[offset + index];
    }
    return value;
}

std::int64_t read_signed(const std::vector<std::uint8_t>& bits,
                         std::size_t offset, unsigned width) {
    // Used only for the independently checked 27-/28-bit position fields.
    const auto value = static_cast<std::int64_t>(read_unsigned(bits, offset, width));
    return bits[offset] ? value - (std::int64_t{1} << width) : value;
}

std::vector<std::uint8_t> reverse_armor(std::string_view armored,
                                      unsigned fill) {
    require(fill <= 5, "incorrect fill count");
    std::vector<std::uint8_t> result;
    for (const unsigned char ch : armored) {
        require((ch >= 48 && ch <= 87) || (ch >= 96 && ch <= 119),
                "armored character outside alphabet");
        const unsigned value = ch <= 87 ? ch - 48 : ch - 56;
        for (int power = 5; power >= 0; --power) {
            result.push_back(static_cast<std::uint8_t>((value / (1u << power)) % 2));
        }
    }
    require(result.size() > fill, "empty decoded payload");
    for (unsigned index = 0; index < fill; ++index) {
        require(result.back() == 0, "nonzero armoring padding");
        result.pop_back();
    }
    return result;
}

std::vector<std::string> split_commas(std::string_view body) {
    std::vector<std::string> fields;
    std::size_t start = 0;
    for (std::size_t index = 0; index <= body.size(); ++index) {
        if (index == body.size() || body[index] == ',') {
            fields.emplace_back(body.substr(start, index - start));
            start = index + 1;
        }
    }
    return fields;
}

std::string independent_sentence(std::string_view body) {
    unsigned check = 0;
    for (const unsigned char ch : body) {
        check ^= ch;
    }
    constexpr char hex[] = "0123456789ABCDEF";
    std::string result = "!" + std::string(body) + "*";
    result.push_back(hex[check / 16]);
    result.push_back(hex[check % 16]);
    return result + "\r\n";
}

void wire_check(const EncodedPayload& payload,
                const std::vector<std::string>& sentences, char channel,
                std::optional<unsigned> sequence = std::nullopt) {
    require(!sentences.empty() && sentences.size() <= 9, "fragment count");
    std::string joined;
    unsigned final_fill = 0;
    for (std::size_t index = 0; index < sentences.size(); ++index) {
        const auto& line = sentences[index];
        require(line.size() <= 82 && line.size() >= 22, "complete NMEA byte limit");
        require(line.substr(line.size() - 2) == "\r\n", "canonical CRLF");
        const auto star = line.find('*');
        require(star == line.size() - 5 && line.front() == '!', "delimiter position");
        const auto body = std::string_view(line).substr(1, star - 1);
        require(independent_sentence(body) == line, "independent checksum mismatch");
        const auto fields = split_commas(body);
        require(fields.size() == 7 && fields[0] == "AIVDM", "seven canonical fields");
        require(fields[1] == std::to_string(sentences.size()) &&
                    fields[2] == std::to_string(index + 1), "fragment order");
        require(fields[3] == (sentences.size() == 1 ? "" : std::to_string(*sequence)),
                "caller-controlled fragment sequence");
        require(fields[4] == std::string(1, channel), "explicit channel");
        require(fields[6].size() == 1 && fields[6][0] >= '0' && fields[6][0] <= '5',
                "fill is a single decimal digit");
        final_fill = static_cast<unsigned>(fields[6][0] - '0');
        require(final_fill == (index + 1 == sentences.size()
                                  ? static_cast<unsigned>(payload.fill_bits) : 0u),
                "only final fragment has fill");
        joined += fields[5];
    }
    require(joined == payload.armored, "ordered fragment reconstruction");
    require(reverse_armor(joined, final_fill) == payload.bits,
            "independent reverse armoring differs from payload bits");
    std::ostringstream output;
    write_sentences(output, sentences);
    std::string expected;
    for (const auto& line : sentences) {
        expected += line;
    }
    require(output.str() == expected, "ostream altered canonical bytes");
}

void bit_writer_tests() {
    BitWriter writer;
    writer.append_unsigned(5, 3, "unsigned");
    writer.append_signed(-3, 4, "signed");
    require(bit_string(writer.bits()) == "1011101", "MSB-first signed/unsigned fields");
    const auto saved = writer.bits();
    for (const unsigned width : {0u, 65u, std::numeric_limits<unsigned>::max()}) {
        invalid([&] { writer.append_unsigned(0, width, "width"); });
        invalid([&] { writer.append_signed(0, width, "width"); });
        require(writer.bits() == saved, "invalid widths partially changed writer");
    }
    invalid([&] { writer.append_unsigned(8, 3, "overflow"); });
    invalid([&] { writer.append_unsigned(UINT64_MAX, 63, "overflow"); });
    invalid([&] { writer.append_signed(8, 4, "overflow"); });
    invalid([&] { writer.append_signed(-9, 4, "underflow"); });
    require(writer.bits() == saved, "invalid values partially changed writer");

    BitWriter wide;
    wide.append_unsigned(UINT64_MAX, 64, "uint64 maximum");
    wide.append_signed(INT64_MIN, 64, "int64 minimum");
    wide.append_signed(INT64_MAX, 64, "int64 maximum");
    wide.append_signed(-1, 1, "signed one-bit negative");
    require(bit_string(wide.bits()) == std::string(64, '1') + "1" +
                std::string(63, '0') + "0" + std::string(64, '1'),
            "64-bit endpoints or sign extension");
    invalid([&] { wide.append_signed(1, 1, "signed one-bit positive"); });
    invalid([&] { wide.append_signed(INT64_MIN, 63, "signed lower overflow"); });
    invalid([&] { wide.append_signed(INT64_MAX, 63, "signed upper overflow"); });
    BitWriter bounded;
    for (unsigned index = 0; index < 64; ++index) {
        bounded.append_unsigned(0, 64, "bounded");
    }
    require(bounded.bits().size() == 4096, "maximum writer capacity");
    invalid([&] { bounded.append_unsigned(0, 1, "capacity"); });
    require(bounded.bits().size() == 4096, "capacity failure mutated writer");
    std::cout << "PASS bit writer MSB order, two's complement, 64-bit endpoints, atomic failures\n";
}

void armor_tests() {
    constexpr std::string_view alphabet =
        "0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVW`abcdefghijklmnopqrstuvw";
    require(alphabet.size() == 64, "test alphabet length");
    std::vector<std::uint8_t> bits;
    for (unsigned value = 0; value < 64; ++value) {
        for (int power = 5; power >= 0; --power) {
            bits.push_back(static_cast<std::uint8_t>((value / (1u << power)) % 2));
        }
    }
    const auto all = armor_bits(bits);
    require(all.armored == alphabet && all.fill_bits == 0 && all.bits == bits,
            "all 64 armoring characters");
    for (unsigned fill = 0; fill <= 5; ++fill) {
        const std::vector<std::uint8_t> sample(6 - fill, 1);
        const auto payload = armor_bits(sample);
        require(payload.fill_bits == fill && payload.armored.size() == 1 &&
                    payload.armored[0] == alphabet[64 - (1u << fill)] &&
                    reverse_armor(payload.armored, fill) == sample,
                "fill 0..5 and zero padding");
    }
    const auto large = armor_bits(std::vector<std::uint8_t>(4096, 1));
    require(large.bits.size() == 4096 && large.armored.size() == 683 &&
                large.fill_bits == 2, "bounded armor maximum");
    invalid([] { (void)armor_bits({}); });
    invalid([] { (void)armor_bits({0, 2, 1}); });
    invalid([] { (void)armor_bits(std::vector<std::uint8_t>(4097, 0)); });
    std::cout << "PASS all 64 armor values, every fill count, zero padding and bounded inputs\n";
}

void nmea_tests() {
    require(checksum("AIVDM,1,1,,A,0,0") == 0x16, "independent pinned XOR");
    std::string high_bytes;
    high_bytes.push_back(static_cast<char>(0x80));
    high_bytes.push_back(static_cast<char>(0xFF));
    require(checksum(high_bytes) == 0x7F && checksum("") == 0, "unsigned byte checksum");
    for (const std::size_t size : {1u, 5u, 6u, 167u, 168u, 272u, 365u, 366u,
                                   367u, 372u, 719u, 720u, 721u, 3239u, 3240u}) {
        std::vector<std::uint8_t> bits(size);
        for (std::size_t index = 0; index < bits.size(); ++index) {
            bits[index] = static_cast<std::uint8_t>((index * 7 + index / 3) % 2);
        }
        const auto payload = armor_bits(bits);
        for (const char channel : {'A', 'B'}) {
            for (const unsigned sequence : {0u, 9u}) {
                const auto sentences = format_aivdm(payload, channel, sequence);
                wire_check(payload, sentences, channel, sequence);
                require(format_aivdm(payload, channel, sequence) == sentences,
                        "fragmentation depends on hidden state");
                if (payload.armored.size() <= 61) {
                    require(sentences.size() == 1, "single sentence unnecessarily fragmented");
                    require(format_aivdm(payload, channel) == sentences,
                            "single sentence depends on supplied sequence");
                } else {
                    require(sentences.front().size() == 82, "actual header capacity not used");
                    invalid([&] { (void)format_aivdm(payload, channel); });
                }
            }
        }
    }
    const auto single = armor_bits(std::vector<std::uint8_t>(366, 0));
    require(format_aivdm(single, 'A').front().size() == 82, "single maximum includes CRLF");
    const auto multipart = armor_bits(std::vector<std::uint8_t>(367, 0));
    const auto lines = format_aivdm(multipart, 'B', 7);
    require(lines.size() == 2 && lines[0].size() == 82 && lines[1].size() == 24,
            "sequence ID changes fragment payload capacity");
    invalid([&] { (void)format_aivdm(single, 'a'); });
    invalid([&] { (void)format_aivdm(single, '\0'); });
    invalid([&] { (void)format_aivdm(single, ','); });
    invalid([&] { (void)format_aivdm(single, 'A', 10); });
    invalid([&] { (void)format_aivdm(single, 'A', UINT32_MAX); });
    invalid([] { (void)format_aivdm(armor_bits(std::vector<std::uint8_t>(3241, 0)), 'A', 0); });
    invalid([] { (void)format_aivdm(armor_bits(std::vector<std::uint8_t>(4096, 0)), 'A', 0); });
    for (unsigned failure = 0; failure < 7; ++failure) {
        auto broken = single;
        if (failure == 0) broken.fill_bits = 6;
        if (failure == 1) broken.fill_bits = 1;
        if (failure == 2) broken.bits[0] = 2;
        if (failure == 3) broken.bits[0] = 1;
        if (failure == 4) broken.armored[0] = 'X';
        if (failure == 5) broken.armored.clear();
        if (failure == 6) broken.bits.clear();
        invalid([&] { (void)format_aivdm(broken, 'A'); });
    }
    auto nonzero_fill = armor_bits({1});
    nonzero_fill.armored[0] = 'w';
    invalid([&] { (void)format_aivdm(nonzero_fill, 'A'); });
    for (unsigned failure = 0; failure < 8; ++failure) {
        auto broken = lines;
        if (failure == 0) broken.pop_back();
        if (failure == 1) std::reverse(broken.begin(), broken.end());
        if (failure == 2) {
            auto& digit = broken[1][broken[1].size() - 4];
            digit = digit == '0' ? '1' : '0';
        }
        if (failure == 3) broken[1].pop_back();
        if (failure == 4) broken[1] = independent_sentence("AIVDM,2,2,8,B,00,5");
        if (failure == 5) broken[1] = independent_sentence("AIVDM,2,2,7,A,00,5");
        if (failure == 6) broken[0] = independent_sentence("AIVDM,2,1,7,B,0,1");
        if (failure == 7) broken[1] = independent_sentence("AIVDM,2,2,7,B,0w,5");
        std::ostringstream output;
        output << "prefix";
        invalid([&] { write_sentences(output, broken); });
        require(output.str() == "prefix", "invalid final fragment caused partial output");
    }
    invalid([] { std::ostringstream output; write_sentences(output, {}); });
    std::ostringstream bad_stream;
    bad_stream.setstate(std::ios::badbit);
    bool io_failed = false;
    try {
        write_sentences(bad_stream, lines);
    } catch (const std::runtime_error&) {
        io_failed = true;
    }
    require(io_failed, "failed ostream ignored");
    std::cout << "PASS NMEA XOR, exact CRLF/82-byte bounds, 1..9 fragments, reconstruction, atomic validation\n";
}

std::vector<Message> golden_messages() {
    // Explicit synthetic inputs. These do not represent a B2 actor, operational
    // identity, valid slot allocation, or current time. Test data is intentionally
    // separate from the diagnostic app's object construction.
    Type1 one(Mmsi("123456789"), RadioState19(0x54321));
    one.repeat_indicator = 1;
    one.navigation_status = 5;
    one.rot_wire = -42;
    one.sog_tenths_knots = 123;
    one.position_accuracy = true;
    one.longitude = -73567890;
    one.latitude = 23456789;
    one.cog_tenths_degrees = 1234;
    one.true_heading_degrees = 123;
    one.utc_second = 59;
    one.special_manoeuvre = 2;
    one.low_transmit_power = true;
    one.raim = true;

    Type4 four(Mmsi("001234567"), RadioState19(0x23456));
    four.repeat_indicator = 2;
    four.utc_year = 2024;
    four.utc_month = 2;
    four.utc_day = 29;
    four.utc_hour = 23;
    four.utc_minute = 59;
    four.utc_second = 58;
    four.position_accuracy = true;
    four.longitude = -108000000;
    four.latitude = 54000000;
    four.position_fixing_device = 15;
    four.satellite_transmission_control = true;
    four.raim = true;

    Type9 nine(Mmsi("111222333"), RadioState20(0xABCDE));
    nine.repeat_indicator = 3;
    nine.altitude_m = 1234;
    nine.sog_knots = 321;
    nine.position_accuracy = true;
    nine.longitude = 108000000;
    nine.latitude = -54000000;
    nine.cog_tenths_degrees = 3599;
    nine.utc_second = 62;
    nine.altitude_barometric = true;
    nine.dte_not_ready = true;
    nine.assigned_mode = true;
    nine.raim = true;

    Type18 eighteen(Mmsi("987654321"), RadioState20(0x54321));
    eighteen.low_transmit_power = true;
    eighteen.display = true;
    eighteen.dsc = true;
    eighteen.whole_band = true;
    eighteen.message22 = true;
    eighteen.assigned_mode = true;
    eighteen.raim = true;

    Type21 twenty_one(Mmsi("993661234"));
    twenty_one.repeat_indicator = 1;
    twenty_one.aid_type = 6;
    twenty_one.name = "LAB LIGHT 01";
    twenty_one.position_accuracy = true;
    twenty_one.longitude = -1;
    twenty_one.latitude = -2;
    twenty_one.dimension_a = 9;
    twenty_one.dimension_b = 10;
    twenty_one.dimension_c = 11;
    twenty_one.dimension_d = 12;
    twenty_one.position_fixing_device = 7;
    twenty_one.utc_second = 58;
    twenty_one.off_position = true;
    twenty_one.aton_status = 0xA5;
    twenty_one.raim = true;
    twenty_one.assigned_mode = true;
    return {one, four, nine, eighteen, twenty_one};
}

void golden_tests() {
    // Independently synthesized binary constants from M.1371-6 Tables 46,49,
    // 57,68,71. Neither the production field table nor BitWriter generates the
    // expected data. Python acceptance independently decodes every field too.
    const std::vector<std::string> expected_bits = {
        "000001010001110101101111001101000101010101110101100001111011110111001110101110001011011100010110010111101100000101010100110100100011110111110111000111010100001100100001",
        "000100100000000001001011010110100001110001111110100000101110110111111011111010110011001000000001101000000000110011011111111001100000001111100000000010100011010001010110",
        "001001110001101010000100011110001111010100110100100101000001101100110111111110011000000001001100100000000110100000001110000011111111101000000010001110101011110011011110",
        "010010001110101101111001101000101100010000000011111111110011001111001000110101100000001101000001001000010100000011100001000011111111111110010011111101010100001100100001",
        "01010101111011001110100001000100110010001100011000000010000101000000011000010010001110010000101001000001100001100010000000000000000000000000000000000000000000000001111111111111111111111111111111111111111111111111111111000000100100000101000101100110001111110101101001011010",
    };
    const std::vector<std::string> expected_nmea = {
        "!AIVDM,1,1,,A,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*2B\r\n",
        "!AIVDM,1,1,,A,4P1;FQivPfosrk81`0kOqP?P2SAF,0*26\r\n",
        "!AIVDM,1,1,,A,9ib4N?DlU1dovH1<P6P>3w`2>ckN,0*43\r\n",
        "!AIVDM,1,1,,A,B>eq`d@3wk?8mP=18D3Q3wvCuD<Q,0*20\r\n",
        "!AIVDM,1,1,,A,ENk`A<S60Q@64ST:@HHP0000000Owwwwwwwwh91AISuJFP,4*59\r\n",
    };
    const auto messages = golden_messages();
    for (std::size_t index = 0; index < messages.size(); ++index) {
        const auto payload = encode(messages[index]);
        require(bit_string(payload.bits) == expected_bits[index], "independent golden bit vector");
        require(payload.bits.size() == (index == 4 ? 272 : 168), "standard message length");
        require(payload.fill_bits == (index == 4 ? 4 : 0), "golden fill count");
        const auto sentences = encode_aivdm(messages[index], 'A');
        require(sentences.size() == 1 && sentences.front() == expected_nmea[index],
                "independent golden NMEA bytes");
        wire_check(payload, sentences, 'A');
        const auto direct = std::visit([](const auto& typed) { return encode(typed); }, messages[index]);
        require(direct.bits == payload.bits && direct.armored == payload.armored &&
                    direct.fill_bits == payload.fill_bits, "typed/variant encoder inconsistency");
    }
    for (std::size_t reverse = messages.size(); reverse > 0; --reverse) {
        require(encode_aivdm(messages[reverse - 1], 'A')[0] == expected_nmea[reverse - 1],
                "execution order affects golden bytes");
    }
    std::cout << "PASS five independently pinned complete bit vectors and golden NMEA CRLF strings\n";
}

void published_fixture_test() {
    // Short factual telemetry fixture from Eric S. Raymond, GPSD project,
    // AIVDM/AIVDO protocol decoding v1.58 (24 June 2023), sentence-layer
    // example. No GPSD implementation code is copied. Project BSD-2-Clause
    // provenance and notice are recorded in README.md (repository root).
    // https://gpsd.gitlab.io/gpsd/AIVDM.html#_aivdm_aivdo_sentence_layer
    // https://gitlab.com/gpsd/gpsd/-/raw/master/COPYING
    constexpr std::string_view fixture =
        "!AIVDM,1,1,,B,177KQJ5000G?tO`K>RA1wUbN0TKH,0*5C\r\n";
    // Independently reverse-armored and decoded by the Python test oracle:
    // MMSI477553000, status5, ROT0, SOG0, low accuracy, -73407500/+28549700
    // wire coordinates, COG510, heading181, second15, no manoeuvre/RAIM.
    Type1 message(Mmsi("477553000"), RadioState19(149208));
    message.navigation_status = 5;
    message.rot_wire = 0;
    message.sog_tenths_knots = 0;
    message.longitude = -73407500;
    message.latitude = 28549700;
    message.cog_tenths_degrees = 510;
    message.true_heading_degrees = 181;
    message.utc_second = 15;
    const auto payload = encode(message);
    const auto sentences = encode_aivdm(Message(message), 'B');
    require(sentences.size() == 1 && sentences[0] == fixture,
            "published GPSD Type1 fixture reproduction");
    wire_check(payload, sentences, 'B');
    std::cout << "PASS published GPSD v1.58 Type1 factual telemetry fixture, independently decoded fields\n";
}

template <typename T>
void numeric_bounds(T message, std::int64_t T::* member,
                    std::int64_t minimum, std::int64_t maximum) {
    for (const std::int64_t value : {minimum, maximum}) {
        message.*member = value;
        (void)encode(message);
    }
    for (const std::int64_t value : {minimum - 1, maximum + 1, INT64_MIN, INT64_MAX}) {
        message.*member = value;
        invalid([&] { (void)encode(message); });
    }
}

template <typename T>
void position_bounds(T message, std::size_t longitude_offset,
                     std::size_t latitude_offset) {
    for (const std::int64_t longitude : {-108000000, -1, 0, 1, 108000000, 108600000}) {
        message.longitude = longitude;
        const auto bits = encode(message).bits;
        require(read_signed(bits, longitude_offset, 28) == longitude,
                "longitude boundary/sentinel not encoded exactly");
    }
    for (const std::int64_t longitude : std::vector<std::int64_t>{
             -108000001, 108000001, 108599999, -108600000, INT64_MIN, INT64_MAX}) {
        message.longitude = longitude;
        invalid([&] { (void)encode(message); });
    }
    message.longitude = 0;
    for (const std::int64_t latitude : {-54000000, -1, 0, 1, 54000000, 54600000}) {
        message.latitude = latitude;
        const auto bits = encode(message).bits;
        require(read_signed(bits, latitude_offset, 27) == latitude,
                "latitude boundary/sentinel not encoded exactly");
    }
    for (const std::int64_t latitude : std::vector<std::int64_t>{
             -54000001, 54000001, 54599999, -54600000, INT64_MIN, INT64_MAX}) {
        message.latitude = latitude;
        invalid([&] { (void)encode(message); });
    }
}

void model_tests() {
    static_assert(!std::is_default_constructible_v<Mmsi>);
    static_assert(!std::is_default_constructible_v<RadioState19>);
    static_assert(!std::is_default_constructible_v<RadioState20>);
    static_assert(!std::is_default_constructible_v<Type1>);
    static_assert(!std::is_default_constructible_v<Type4>);
    static_assert(!std::is_default_constructible_v<Type9>);
    static_assert(!std::is_default_constructible_v<Type18>);
    static_assert(!std::is_default_constructible_v<Type21>);
    static_assert(std::is_same_v<decltype(Type1::position_accuracy), bool>);
    static_assert(std::is_same_v<decltype(Type21::virtual_aid), bool>);
    static_assert(std::is_same_v<decltype(Type1::sog_tenths_knots), std::int64_t>);

    require(Mmsi("000000001").digits() == "000000001" &&
                Mmsi("000000001").wire_value() == 1, "leading-zero MMSI preservation");
    require(Mmsi("999999999").wire_value() == 999999999, "nine-digit maximum MMSI");
    for (const std::string digits : {"", "000000000", "12345678", "1234567890",
                                     "1073741824", "12345678A", "-12345678", "+12345678",
                                     " 12345678", "12345678 ", "12345678\n",
                                     "12345.789", "12345_789"}) {
        invalid([&] { (void)Mmsi(digits); });
    }
    invalid([] { (void)Mmsi(std::string("12345678\0", 9)); });
    invalid([] { (void)Mmsi("\xEF\xBC\x91" "23456789"); });
    require(RadioState19(0).value() == 0 && RadioState19(0x7FFFF).value() == 0x7FFFF,
            "19-bit explicit endpoints");
    require(RadioState20(0).value() == 0 && RadioState20(0xFFFFF).value() == 0xFFFFF,
            "20-bit explicit endpoints");
    for (const std::int64_t value : std::vector<std::int64_t>{-1, 0x80000, INT64_MIN, INT64_MAX}) {
        invalid([&] { (void)RadioState19(value); });
    }
    for (const std::int64_t value : std::vector<std::int64_t>{-1, 0x100000, INT64_MIN, INT64_MAX}) {
        invalid([&] { (void)RadioState20(value); });
    }

    Type1 one(Mmsi("000000001"), RadioState19(0));
    Type4 four(Mmsi("000000001"), RadioState19(0));
    Type9 nine(Mmsi("000000001"), RadioState20(0));
    Type18 eighteen(Mmsi("000000001"), RadioState20(0));
    Type21 twenty_one(Mmsi("000000001"));
    position_bounds(one, 61, 89);
    position_bounds(four, 79, 107);
    position_bounds(nine, 61, 89);
    position_bounds(eighteen, 57, 85);
    position_bounds(twenty_one, 164, 192);
    numeric_bounds(one, &Type1::repeat_indicator, 0, 3);
    numeric_bounds(four, &Type4::repeat_indicator, 0, 3);
    numeric_bounds(nine, &Type9::repeat_indicator, 0, 3);
    numeric_bounds(eighteen, &Type18::repeat_indicator, 0, 3);
    numeric_bounds(twenty_one, &Type21::repeat_indicator, 0, 3);
    numeric_bounds(one, &Type1::rot_wire, -128, 127);
    numeric_bounds(one, &Type1::sog_tenths_knots, 0, 1023);
    numeric_bounds(nine, &Type9::sog_knots, 0, 1023);
    numeric_bounds(eighteen, &Type18::sog_tenths_knots, 0, 1023);
    numeric_bounds(one, &Type1::cog_tenths_degrees, 0, 3600);
    numeric_bounds(nine, &Type9::cog_tenths_degrees, 0, 3600);
    numeric_bounds(eighteen, &Type18::cog_tenths_degrees, 0, 3600);
    numeric_bounds(one, &Type1::utc_second, 0, 63);
    numeric_bounds(nine, &Type9::utc_second, 0, 63);
    numeric_bounds(eighteen, &Type18::utc_second, 0, 63);
    numeric_bounds(twenty_one, &Type21::utc_second, 0, 63);
    numeric_bounds(one, &Type1::special_manoeuvre, 0, 3);
    numeric_bounds(nine, &Type9::altitude_m, 0, 4095);
    numeric_bounds(four, &Type4::utc_year, 0, 9999);
    numeric_bounds(four, &Type4::utc_month, 0, 12);
    numeric_bounds(four, &Type4::utc_day, 0, 31);
    numeric_bounds(four, &Type4::utc_hour, 0, 24);
    numeric_bounds(four, &Type4::utc_minute, 0, 60);
    numeric_bounds(four, &Type4::utc_second, 0, 60);
    numeric_bounds(twenty_one, &Type21::aid_type, 0, 31);
    numeric_bounds(twenty_one, &Type21::dimension_a, 0, 511);
    numeric_bounds(twenty_one, &Type21::dimension_b, 0, 511);
    numeric_bounds(twenty_one, &Type21::dimension_c, 0, 63);
    numeric_bounds(twenty_one, &Type21::dimension_d, 0, 63);
    numeric_bounds(twenty_one, &Type21::aton_status, 0, 255);
    for (const std::int64_t status : {0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 14, 15}) {
        one.navigation_status = status;
        require(read_unsigned(encode(one).bits, 38, 4) == static_cast<std::uint64_t>(status),
                "navigation status exact code");
    }
    for (const std::int64_t status : {-1, 9, 10, 13, 16}) {
        one.navigation_status = status;
        invalid([&] { (void)encode(one); });
    }
    one.navigation_status = 15;
    for (const std::int64_t heading : {0, 359, 511}) {
        one.true_heading_degrees = heading;
        eighteen.true_heading_degrees = heading;
        require(read_unsigned(encode(one).bits, 128, 9) == static_cast<std::uint64_t>(heading),
                "Type1 heading code");
        require(read_unsigned(encode(eighteen).bits, 124, 9) == static_cast<std::uint64_t>(heading),
                "Type18 heading code");
    }
    for (const std::int64_t heading : {-1, 360, 510, 512}) {
        one.true_heading_degrees = heading;
        eighteen.true_heading_degrees = heading;
        invalid([&] { (void)encode(one); });
        invalid([&] { (void)encode(eighteen); });
    }
    eighteen.true_heading_degrees = 511;
    for (const std::int64_t epfs : {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 15}) {
        four.position_fixing_device = epfs;
        twenty_one.position_fixing_device = epfs;
        require(read_unsigned(encode(four).bits, 134, 4) == static_cast<std::uint64_t>(epfs),
                "Type4 updated EPFS code");
        require(read_unsigned(encode(twenty_one).bits, 249, 4) == static_cast<std::uint64_t>(epfs),
                "Type21 updated EPFS code");
    }
    for (const std::int64_t epfs : {-1, 10, 11, 16}) {
        four.position_fixing_device = epfs;
        twenty_one.position_fixing_device = epfs;
        invalid([&] { (void)encode(four); });
        invalid([&] { (void)encode(twenty_one); });
    }
    std::cout << "PASS explicit identities/state, numeric widths, positions/boundaries, sentinels and reserved enums\n";
}

void date_text_profile_tests() {
    Type4 four(Mmsi("000000001"), RadioState19(0));
    four.utc_month = 2;
    four.utc_day = 29;
    for (const std::int64_t year : {0, 2000, 2024, 2400}) {
        four.utc_year = year;
        (void)encode(four);
    }
    for (const std::int64_t year : {1, 1900, 2023, 2100, 9999}) {
        four.utc_year = year;
        invalid([&] { (void)encode(four); });
    }
    four.utc_year = 0;
    four.utc_day = 30;
    invalid([&] { (void)encode(four); });
    four.utc_month = 4;
    four.utc_day = 31;
    invalid([&] { (void)encode(four); });
    four.utc_month = 0;
    (void)encode(four);  // Unknown month does not invent a calendar constraint.
    four.utc_month = 2;
    four.utc_day = 0;
    (void)encode(four);  // Per-component unavailable markers are independently legal.

    Type21 aid(Mmsi("000000001"));
    const auto unavailable_name = encode(aid).bits;
    require(std::all_of(unavailable_name.begin() + 43, unavailable_name.begin() + 163,
                        [](auto bit) { return bit == 0; }), "empty name must pad 20 @ symbols");
    aid.name = std::string(20, 'Z');
    const auto maximum_name = encode(aid).bits;
    for (unsigned index = 0; index < 20; ++index) {
        require(read_unsigned(maximum_name, 43 + index * 6, 6) == 26, "20-character name maximum");
    }
    for (unsigned character = 32; character <= 95; ++character) {
        if (character == '@') continue;
        aid.name.assign(1, static_cast<char>(character));
        require(read_unsigned(encode(aid).bits, 43, 6) == (character & 63),
                "permitted six-bit text alphabet");
    }
    for (const std::string name : std::vector<std::string>{"@", "A@B", "lowercase", "`", "{", "}", "|", "~",
                                   "\x7F", "\x1F", "\n", "\t", "\xC3\xA9",
                                   std::string(21, 'A'), std::string(10000, 'A')}) {
        aid.name = name;
        invalid([&] { (void)encode(aid); });
    }
    aid.name = std::string("A\0B", 3);
    invalid([&] { (void)encode(aid); });
    aid.name.clear();
    aid.virtual_aid = true;
    (void)encode(aid);
    for (const auto member : {&Type21::dimension_a, &Type21::dimension_b,
                              &Type21::dimension_c, &Type21::dimension_d}) {
        aid.*member = 1;
        invalid([&] { (void)encode(aid); });
        aid.*member = 0;
    }
    aid.virtual_aid = false;
    aid.aid_type = 1;
    (void)encode(aid);
    aid.dimension_a = 1;
    invalid([&] { (void)encode(aid); });
    aid.dimension_a = 0;
    aid.aid_type = 6;
    aid.off_position = true;
    for (const std::int64_t second : {0, 59}) {
        aid.utc_second = second;
        require(read_unsigned(encode(aid).bits, 259, 1) == 1,
                "latest fixed-aid GNSS anomaly off-position semantics");
    }
    for (const std::int64_t second : {60, 61, 62, 63}) {
        aid.utc_second = second;
        // The standard tells consumers to consider the flag only at 0..59;
        // it does not forbid preserving the caller's explicit bit otherwise.
        require(read_unsigned(encode(aid).bits, 259, 1) == 1,
                "explicit off-position bit is preserved when not meaningful");
    }

    Type18 cs(Mmsi("000000001"), RadioState20(0xE0006));
    cs.cs_unit = true;
    require(read_unsigned(encode(cs).bits, 141, 1) == 1 &&
                read_unsigned(encode(cs).bits, 148, 20) == 0xE0006,
            "Class B CS exact required selector/state");
    for (const std::int64_t second : {0, 59, 60}) {
        cs.utc_second = second;
        (void)encode(cs);
    }
    for (const std::int64_t second : {61, 62, 63}) {
        cs.utc_second = second;
        invalid([&] { (void)encode(cs); });
    }
    cs.utc_second = 60;
    cs.repeat_indicator = 1;
    invalid([&] { (void)encode(cs); });
    cs.repeat_indicator = 0;
    for (const std::int64_t state : {0, 0x60006, 0xE0005, 0xE0007, 0xFFFFF}) {
        cs.radio_state = RadioState20(state);
        invalid([&] { (void)encode(cs); });
    }
    std::cout << "PASS Gregorian/partial UTC, AIS text alphabet/20-character bound and explicit CS/AtoN constraints\n";
}

void defaults_tests() {
    const auto one = encode(Type1(Mmsi("000000001"), RadioState19(0))).bits;
    require(read_unsigned(one, 38, 4) == 15 && read_unsigned(one, 42, 8) == 128 &&
                read_unsigned(one, 50, 10) == 1023 && read_unsigned(one, 116, 12) == 3600 &&
                read_unsigned(one, 128, 9) == 511 && read_unsigned(one, 137, 6) == 60 &&
                read_unsigned(one, 143, 6) == 0 && read_unsigned(one, 149, 19) == 0,
            "Type1 documented unavailable defaults");
    const auto four = encode(Type4(Mmsi("000000001"), RadioState19(0))).bits;
    require(read_unsigned(four, 38, 23) == 0 && read_unsigned(four, 61, 5) == 24 &&
                read_unsigned(four, 66, 6) == 60 && read_unsigned(four, 72, 6) == 60 &&
                read_unsigned(four, 134, 34) == 0,
            "Type4 unavailable date/time and zero spares");
    const auto nine = encode(Type9(Mmsi("000000001"), RadioState20(0))).bits;
    require(read_unsigned(nine, 38, 12) == 4095 && read_unsigned(nine, 50, 10) == 1023 &&
                read_unsigned(nine, 116, 12) == 3600 && read_unsigned(nine, 128, 6) == 60 &&
                read_unsigned(nine, 134, 8) == 0 && read_unsigned(nine, 142, 1) == 1 &&
                read_unsigned(nine, 143, 25) == 0,
            "Type9 unavailable altitude/speed, GNSS, DTE and spares");
    const auto eighteen = encode(Type18(Mmsi("000000001"), RadioState20(0))).bits;
    require(read_unsigned(eighteen, 38, 8) == 0 && read_unsigned(eighteen, 46, 10) == 1023 &&
                read_unsigned(eighteen, 112, 12) == 3600 && read_unsigned(eighteen, 124, 9) == 511 &&
                read_unsigned(eighteen, 133, 6) == 60 && read_unsigned(eighteen, 139, 29) == 0,
            "Type18 unavailable defaults and zero capability/spare bits");
    const auto aid = encode(Type21(Mmsi("000000001"))).bits;
    require(read_unsigned(aid, 38, 5) == 0 && read_unsigned(aid, 219, 34) == 0 &&
                read_unsigned(aid, 253, 6) == 60 && read_unsigned(aid, 259, 13) == 0,
            "Type21 unavailable defaults, physical/unassigned aid and spare bit");
    std::cout << "PASS per-type documented unavailable/default marker bit offsets\n";
}

template <typename T>
void flag_offset(T message, bool T::* member, std::size_t offset) {
    message.*member = false;
    const auto zero = encode(message).bits;
    message.*member = true;
    const auto one = encode(message).bits;
    require(zero[offset] == 0 && one[offset] == 1, "Boolean field at wrong standard offset");
    for (std::size_t index = 0; index < zero.size(); ++index) {
        if (index != offset) {
            require(zero[index] == one[index], "Boolean toggle changed unrelated wire field");
        }
    }
}

void distinct_flag_and_raw_code_tests() {
    Type1 one(Mmsi("000000001"), RadioState19(0));
    flag_offset(one, &Type1::position_accuracy, 60);
    flag_offset(one, &Type1::low_transmit_power, 147);
    flag_offset(one, &Type1::raim, 148);
    Type4 four(Mmsi("000000001"), RadioState19(0));
    flag_offset(four, &Type4::position_accuracy, 78);
    flag_offset(four, &Type4::satellite_transmission_control, 138);
    flag_offset(four, &Type4::raim, 148);
    Type9 nine(Mmsi("000000001"), RadioState20(0));
    flag_offset(nine, &Type9::position_accuracy, 60);
    flag_offset(nine, &Type9::altitude_barometric, 134);
    flag_offset(nine, &Type9::dte_not_ready, 142);
    flag_offset(nine, &Type9::assigned_mode, 146);
    flag_offset(nine, &Type9::raim, 147);
    Type18 eighteen(Mmsi("000000001"), RadioState20(0xE0006));
    flag_offset(eighteen, &Type18::position_accuracy, 56);
    flag_offset(eighteen, &Type18::low_transmit_power, 139);
    flag_offset(eighteen, &Type18::cs_unit, 141);
    flag_offset(eighteen, &Type18::display, 142);
    flag_offset(eighteen, &Type18::dsc, 143);
    flag_offset(eighteen, &Type18::whole_band, 144);
    flag_offset(eighteen, &Type18::message22, 145);
    flag_offset(eighteen, &Type18::assigned_mode, 146);
    flag_offset(eighteen, &Type18::raim, 147);
    Type21 aid(Mmsi("000000001"));
    flag_offset(aid, &Type21::position_accuracy, 163);
    flag_offset(aid, &Type21::off_position, 259);
    flag_offset(aid, &Type21::raim, 268);
    flag_offset(aid, &Type21::virtual_aid, 269);
    flag_offset(aid, &Type21::assigned_mode, 270);

    for (const std::int64_t rot : {-128, -127, -126, -1, 0, 1, 126, 127}) {
        one.rot_wire = rot;
        require(read_signed(encode(one).bits, 42, 8) == rot, "signed ROT code exact serialization");
    }
    for (const std::int64_t speed : {0, 1, 1022, 1023}) {
        one.sog_tenths_knots = speed;
        nine.sog_knots = speed;
        eighteen.sog_tenths_knots = speed;
        require(read_unsigned(encode(one).bits, 50, 10) == static_cast<std::uint64_t>(speed) &&
                    read_unsigned(encode(nine).bits, 50, 10) == static_cast<std::uint64_t>(speed) &&
                    read_unsigned(encode(eighteen).bits, 46, 10) == static_cast<std::uint64_t>(speed),
                "speed code saturation/unavailable marker exact serialization");
    }
    for (const std::int64_t altitude : {0, 1, 4094, 4095}) {
        nine.altitude_m = altitude;
        require(read_unsigned(encode(nine).bits, 38, 12) == static_cast<std::uint64_t>(altitude),
                "aircraft altitude saturation/unavailable marker exact serialization");
    }
    one.radio_state = RadioState19(0x7FFFF);
    four.radio_state = RadioState19(0x7FFFF);
    nine.radio_state = RadioState20(0xFFFFF);
    eighteen.radio_state = RadioState20(0xFFFFF);
    require(read_unsigned(encode(one).bits, 149, 19) == 0x7FFFF &&
                read_unsigned(encode(four).bits, 149, 19) == 0x7FFFF &&
                read_unsigned(encode(nine).bits, 148, 20) == 0xFFFFF &&
                read_unsigned(encode(eighteen).bits, 148, 20) == 0xFFFFF,
            "raw communication state width/selector offsets");
    std::cout << "PASS every Boolean independently toggled at its standard bit offset; raw ROT/speed/altitude/state codes\n";
}

}  // namespace

int main() {
    try {
        bit_writer_tests();
        armor_tests();
        nmea_tests();
        golden_tests();
        published_fixture_test();
        model_tests();
        date_text_profile_tests();
        defaults_tests();
        distinct_flag_and_raw_code_tests();
        std::cout << "PASS " << rejected_cases << " invalid host codec cases\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL AIS host codec: " << error.what() << '\n';
        return 1;
    }
}
