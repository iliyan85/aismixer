#include "ais_encoder.h"
#include "nmea0183.h"

#include <cstdio>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

namespace {

namespace ais = aismixer::lab::ais;

struct Example {
    unsigned type;
    ais::Message message;
    const char* golden;
};

// Explicit synthetic codec inputs, independently checked against M.1371-6 by
// ais_codec_tests and ais_wire_oracle.py. No scene identities or clocks enter.
std::vector<Example> examples() {
    ais::Type1 one(ais::Mmsi("123456789"), ais::RadioState19(0x54321));
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

    ais::Type4 four(ais::Mmsi("001234567"), ais::RadioState19(0x23456));
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

    ais::Type9 nine(ais::Mmsi("111222333"), ais::RadioState20(0xabcde));
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

    ais::Type18 eighteen(ais::Mmsi("987654321"), ais::RadioState20(0x54321));
    eighteen.low_transmit_power = true;
    eighteen.display = true;
    eighteen.dsc = true;
    eighteen.whole_band = true;
    eighteen.message22 = true;
    eighteen.assigned_mode = true;
    eighteen.raim = true;

    ais::Type21 twenty_one(ais::Mmsi("993661234"));
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
    twenty_one.aton_status = 0xa5;
    twenty_one.raim = true;
    twenty_one.assigned_mode = true;

    return {
        {1, one, "!AIVDM,1,1,,A,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*2B\r\n"},
        {4, four, "!AIVDM,1,1,,A,4P1;FQivPfosrk81`0kOqP?P2SAF,0*26\r\n"},
        {9, nine, "!AIVDM,1,1,,A,9ib4N?DlU1dovH1<P6P>3w`2>ckN,0*43\r\n"},
        {18, eighteen, "!AIVDM,1,1,,A,B>eq`d@3wk?8mP=18D3Q3wvCuD<Q,0*20\r\n"},
        {21, twenty_one, "!AIVDM,1,1,,A,ENk`A<S60Q@64ST:@HHP0000000Owwwwwwwwh91AISuJFP,4*59\r\n"},
    };
}

void require(bool condition, std::string_view message) {
    if (!condition) {
        throw std::runtime_error("AIS smoke validation failed: " + std::string(message));
    }
}

void demo() {
    for (const auto& example : examples()) {
        const auto payload = ais::encode(example.message);
        const auto sentences = ais::format_aivdm(payload, 'A');
        std::cout << "type=" << example.type << " bits=" << payload.bits.size()
            << " armored=" << payload.armored << " fill=" << unsigned(payload.fill_bits) << '\n';
        ais::write_sentences(std::cout, sentences);
    }
}

void self_test() {
    for (const auto& example : examples()) {
        const auto first = ais::encode(example.message);
        const auto repeated = ais::encode(example.message);
        const auto sentences = ais::format_aivdm(first, 'A');
        const auto expected_bits = example.type == 21 ? 272u : 168u;
        require(first.bits.size() == expected_bits, "incorrect exact payload length");
        require(first.bits == repeated.bits && first.armored == repeated.armored
            && first.fill_bits == repeated.fill_bits, "repeated encoding changed bytes");
        require(sentences.size() == 1 && sentences.front() == example.golden,
            "independently checked golden sentence changed");
        std::cout << "PASS Type " << example.type << " bits=" << expected_bits
            << " sentence_bytes=" << sentences.front().size() << '\n';
    }
    bool rejected = false;
    try { (void)ais::Mmsi("000000000"); }
    catch (const std::invalid_argument&) { rejected = true; }
    require(rejected, "all-zero MMSI was accepted");

    ais::Type1 invalid(ais::Mmsi("123456789"), ais::RadioState19(0));
    invalid.cog_tenths_degrees = 3601;
    rejected = false;
    try { (void)ais::encode(invalid); }
    catch (const std::invalid_argument&) { rejected = true; }
    require(rejected, "reserved course value was accepted");

    const auto multipart = ais::armor_bits(std::vector<std::uint8_t>(62 * 6, 0));
    rejected = false;
    try { (void)ais::format_aivdm(multipart, 'A'); }
    catch (const std::invalid_argument&) { rejected = true; }
    require(rejected, "multipart output lacked an explicit sequence ID");
    const auto fragments = ais::format_aivdm(multipart, 'B', 7);
    require(fragments.size() == 2, "multipart boundary did not produce two fragments");
    for (const auto& fragment : fragments) {
        require(fragment.size() <= 82, "NMEA limit including CRLF exceeded");
    }
    std::cout << "AIS CPU self-test: PASS\n";
}

enum class Mode { Help, Demo, SelfTest };

Mode options(int argc, char** argv) {
    if (argc == 1) return Mode::Demo;
    if (argc != 2) throw std::invalid_argument("choose exactly one of --help, --demo, --self-test");
    const std::string_view option(argv[1]);
    if (option == "--help") return Mode::Help;
    if (option == "--demo") return Mode::Demo;
    if (option == "--self-test") return Mode::SelfTest;
    throw std::invalid_argument("unknown option: " + std::string(option));
}

}  // namespace

int main(int argc, char** argv) {
    Mode mode;
    try { mode = options(argc, argv); }
    catch (const std::invalid_argument& error) {
        std::cerr << "Invalid CLI: " << error.what() << "\nUse --help for usage.\n";
        return 2;
    }
    try {
#ifdef _WIN32
        if (_setmode(_fileno(stdout), _O_BINARY) == -1) {
            throw std::runtime_error("Cannot select binary stdout for canonical NMEA CRLF");
        }
#endif
        if (mode == Mode::Help) {
            std::cout << "Usage: aismixer_lab_ais_smoke [--help | --demo | --self-test]\n"
                << "Default: explicit synthetic Type 1/4/9/18/21 demo (channel A).\n"
                << "Each demo header gives type, bit count, armored payload and fill,\n"
                << "followed by complete !AIVDM sentence(s) with canonical CRLF.\n"
                << "CPU only: no scene mapping, YAML, CUDA, clock, network or files.\n";
        } else if (mode == Mode::Demo) {
            demo();
        } else {
            self_test();
        }
        std::cout.flush();
        if (!std::cout) throw std::runtime_error("Cannot write complete AIS diagnostic output");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "AIS codec smoke failed: " << error.what() << '\n';
        return 1;
    }
}
