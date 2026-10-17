#include "fixture.h"
#include "journal.h"
#include "playback.h"
#include "scheduler.h"
#include "streaming_fixture.h"

#include <charconv>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#endif

namespace {
namespace r = aismixer::lab::runtime;
struct Arguments {
    bool help = false;
    bool streaming_fixture = false, fine_steps = false;
    std::optional<std::filesystem::path> fixture, play, output;
    std::optional<r::ReceiverId> receiver;
    r::JournalOptions journal;
    r::PlaybackOptions playback;
};

Arguments parse(const std::vector<std::string>& args) {
    Arguments result;
    std::set<std::string> seen;
    std::string categories = "disabled";
    std::optional<std::string> view;
    for (std::size_t index = 1; index < args.size(); ++index) {
        const auto& option = args[index];
        if (!seen.insert(option).second) throw std::invalid_argument("duplicate runtime option: " + option);
        const auto value = [&]() -> const std::string& {
            if (++index >= args.size() || args[index].empty() || args[index].rfind("--", 0) == 0) {
                throw std::invalid_argument("runtime option requires a nonempty value: " + option);
            }
            return args[index];
        };
        if (option == "--help") result.help = true;
        else if (option == "--fixture" || option == "--streaming-fixture") {
            if (result.fixture) throw std::invalid_argument("choose only one fixture profile");
            result.fixture = std::filesystem::u8path(value());
            result.streaming_fixture = option == "--streaming-fixture";
        }
        else if (option == "--fine-steps") result.fine_steps = true;
        else if (option == "--play") result.play = std::filesystem::u8path(value());
        else if (option == "--output") result.output = std::filesystem::u8path(value());
        else if (option == "--receiver") result.receiver = r::ReceiverId{value()};
        else if (option == "--speed") result.playback.speed = r::PlaybackSpeed::parse(value());
        else if (option == "--events") categories = value();
        else if (option == "--view") view = value();
        else if (option == "--segment-records") {
            const auto& text = value();
            std::uint32_t number = 0;
            const auto parsed = std::from_chars(text.data(), text.data() + text.size(), number);
            if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() || number == 0 ||
                number > r::kMaxJournalSegmentRecords) {
                throw std::invalid_argument("segment-records must be a decimal integer in 1..65536");
            }
            result.journal.max_records_per_segment = number;
        } else throw std::invalid_argument("unknown runtime option: " + option);
    }
    if (result.help) {
        if (seen.size() != 1) throw std::invalid_argument("--help must be used alone");
        return result;
    }
    if (result.fixture.has_value() == result.play.has_value()) {
        throw std::invalid_argument("choose exactly --fixture NEW_RUN_DIR or --play COMPLETE_RUN_DIR");
    }
    if (result.fine_steps && !result.streaming_fixture)
        throw std::invalid_argument("fine-steps applies only to the streaming infrastructure fixture");
    if (result.fixture && (seen.count("--receiver") || seen.count("--speed") || seen.count("--output") ||
                           seen.count("--events") || seen.count("--view"))) {
        throw std::invalid_argument("fixture generation accepts only --fixture and optional --segment-records");
    }
    if (result.play && seen.count("--segment-records")) {
        throw std::invalid_argument("segment-records applies only to fixture recording");
    }
    result.playback.receiver = result.receiver;
    result.playback.console = r::parse_console_options(categories, view);
    return result;
}

void help() {
    std::cout << "AISMixer Lab R0 recorded runtime; no RF, TDMA or network implementation\n"
              << "--fixture NEW_RUN_DIR [--segment-records 1..65536]\n"
              << "  SYNTHETIC INFRASTRUCTURE TEST (NOT AIS RADIO SIMULATION).\n"
              << "--streaming-fixture NEW_RUN_DIR [--segment-records 1..65536] [--fine-steps]\n"
              << "  Five simulation hours; INFRASTRUCTURE TEST -- NOT AIS RF SIMULATION.\n"
              << "--play COMPLETE_RUN_DIR [--receiver ID] [--speed 0.5|1|2|10|100|fast]\n"
              << "  [--events disabled|all|lifecycle,slotmap,tdma,rf,reception,output,metrics]\n"
              << "  [--view all|ID] [--output NEW_FILE]\n"
              << "Playback defaults to x1; use fast for immediate recorded output.\n"
              << "Raw stdout/file requires one receiver; multi-receiver journals need --receiver.\n"
              << "Event views/categories affect stderr only; NMEA bytes are replayed verbatim.\n"
              << "Named output uses no-clobber publication; no UDP destination is contacted.\n";
    std::cout.flush();
    if (!std::cout) throw std::runtime_error("runtime help stdout flush failed");
}

int run(const std::vector<std::string>& args) {
    const auto options = parse(args);
    if (options.help) { help(); return 0; }
    if (options.fixture) {
        if (options.streaming_fixture) {
            const auto stats = r::record_streaming_fixture(*options.fixture, options.journal, options.fine_steps);
            std::cerr << "INFRASTRUCTURE TEST -- NOT AIS RF SIMULATION\n"
                      << "status=COMPLETE profile=StreamingV2 simulation_seconds=" << r::kStreamingFixtureSeconds
                      << " transmissions=" << r::kStreamingFixtureTransmissions
                      << " receptions=" << r::kStreamingFixtureReceptions
                      << " records=" << stats.records << " segments=" << stats.segments
                      << " logical_digest=" << std::hex << std::setfill('0') << std::setw(16)
                      << stats.logical_digest << std::dec << std::setfill(' ') << '\n';
            std::cerr.flush();
            if (!std::cerr) throw std::runtime_error("runtime fixture diagnostic flush failed");
            return 0;
        }
        const auto fixture = r::make_synthetic_fixture();
        r::JournalMetadata metadata;
        metadata.provenance = fixture.provenance;
        metadata.utc_anchor = fixture.utc_anchor;
        metadata.model_version = "r0-scripted-infrastructure-fixture-v1";
        metadata.codec_version = "B3-pinned-finalized-NMEA-bytes-v1";
        metadata.receivers = fixture.receivers;
        r::JournalWriter writer(*options.fixture, metadata, options.journal);
        r::Scheduler scheduler;
        for (const auto& event : fixture.events) scheduler.enqueue(event);
        scheduler.finish([&](const r::Event& event) { writer.append(event); });
        writer.finish();
        const auto& stats = writer.stats();
        std::cerr << "SYNTHETIC INFRASTRUCTURE TEST (NOT AIS RADIO SIMULATION)\n"
                  << "run=" << options.fixture->u8string() << " status=COMPLETE records=" << stats.records
                  << " segments=" << stats.segments << " logical_digest=" << std::hex
                  << std::setfill('0') << std::setw(16) << stats.logical_digest << std::dec
                  << std::setfill(' ') << '\n';
        std::cerr.flush();
        if (!std::cerr) throw std::runtime_error("runtime fixture diagnostic flush failed");
        return 0;
    }
    r::JournalReader reader(*options.play);
    const auto selected = r::select_receiver(reader.metadata(), options.receiver);
    r::SteadyPlaybackClock clock;
    if (options.output) {
        r::FileOutputSink sink(*options.output, selected);
        r::play_recording(reader, options.playback, clock, sink, std::cerr);
        sink.complete();
    } else {
#ifdef _WIN32
        if (_setmode(_fileno(stdout), _O_BINARY) == -1) throw std::runtime_error("cannot set runtime NMEA stdout binary mode");
#endif
        r::StreamOutputSink sink(std::cout, selected);
        r::play_recording(reader, options.playback, clock, sink, std::cerr);
    }
    return 0;
}

int guarded(const std::vector<std::string>& args) {
    try { return run(args); }
    catch (const std::invalid_argument& error) {
        std::cerr << "R0 invalid input: " << error.what() << '\n'; return 2;
    } catch (const std::exception& error) {
        std::cerr << "R0 runtime failure: " << error.what() << '\n'; return 3;
    }
}

#ifdef _WIN32
std::string utf8_argument(const wchar_t* argument) {
    const int count = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, argument, -1,
                                         nullptr, 0, nullptr, nullptr);
    if (count == 0) throw std::invalid_argument("command-line argument is not valid Unicode");
    std::string value(static_cast<std::size_t>(count), '\0');
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, argument, -1, value.data(), count,
                            nullptr, nullptr) != count) throw std::invalid_argument("cannot convert runtime argument to UTF-8");
    value.pop_back();
    return value;
}
#endif
}  // namespace

#ifdef _WIN32
int wmain(int argc, wchar_t** argv) {
    try {
        std::vector<std::string> arguments;
        arguments.reserve(static_cast<std::size_t>(argc));
        for (int index = 0; index < argc; ++index) arguments.push_back(utf8_argument(argv[index]));
        return guarded(arguments);
    } catch (const std::exception& error) {
        std::cerr << "R0 invalid command line: " << error.what() << '\n'; return 2;
    }
}
#else
int main(int argc, char** argv) { return guarded(std::vector<std::string>(argv, argv + argc)); }
#endif
