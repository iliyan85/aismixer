#include "streaming_fixture.h"
#include "playback.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <sstream>
#include <stdexcept>

namespace r = aismixer::lab::runtime;
namespace fs = std::filesystem;
namespace {
constexpr const char* a = "!AIVDM,1,1,,A,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*2B\r\n";
constexpr const char* b = "!AIVDM,1,1,,B,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*28\r\n";
void require(bool condition, const char* message) { if (!condition) throw std::runtime_error(message); }
void hash(std::uint64_t& value, const std::string& bytes) {
    for (unsigned char byte : bytes) { value ^= byte; value *= UINT64_C(1099511628211); }
}
bool equal_files(const fs::path& x, const fs::path& y) {
    if (fs::file_size(x) != fs::file_size(y)) return false;
    std::ifstream first(x, std::ios::binary), second(y, std::ios::binary);
    std::array<char, 4096> f{}, s{};
    do {
        first.read(f.data(), f.size()); second.read(s.data(), s.size());
        if (first.gcount() != second.gcount() || !std::equal(f.begin(), f.begin() + first.gcount(), s.begin())) return false;
    } while (first.gcount() != 0);
    return first.eof() && second.eof();
}
void equal_directories(const fs::path& x, const fs::path& y) {
    std::size_t first_count = 0, second_count = 0;
    for (const auto& file : fs::directory_iterator(x)) {
        require(file.is_regular_file() && equal_files(file.path(), y / file.path().filename()),
                "valid causal step partition changed physical journal bytes");
        ++first_count;
    }
    for (const auto& ignored : fs::directory_iterator(y)) { (void)ignored; ++second_count; }
    require(first_count == second_count, "step partition changed journal file count");
}
class Clock final : public r::MonotonicClock {
public:
    std::int64_t current = 17, last_deadline = 17;
    std::int64_t now_ns() override { return current; }
    void sleep_until_ns(std::int64_t deadline) override {
        require(deadline >= current, "fake playback deadline regressed");
        current = last_deadline = deadline;
    }
};
class DigestSink final : public r::OutputSink {
public:
    std::uint64_t digest = UINT64_C(14695981039346656037), records = 0;
    std::optional<r::SimulationInstant> last;
    void deliver(r::SimulationInstant time, const r::ReceiverNmea& record) override {
        require(!last || time >= *last, "streaming playback reordered output");
        last = time; ++records;
        for (const auto& sentence : record.sentences) hash(digest, sentence);
    }
    void flush() override {}
};
void bounded(const r::JournalStats& stats) {
    const auto& v = stats.validator;
    require(v.active_transmissions == 0 && v.active_receptions == 0 && v.active_receiver_pairs == 0,
            "terminal streaming run retained packet correlations");
    require(v.peak_active_transmissions <= 2 && v.peak_active_receptions <= 6 &&
            v.peak_active_receiver_pairs <= 6 && v.peak_typed_anchors <= 64,
            "validator retained history instead of bounded active correlations");
    require(stats.retained_recent_events == 4096, "generic cause history was not separately bounded at4096");
}
std::array<std::uint64_t, 3> inspect(const r::JournalReader& reader) {
    std::array<std::uint64_t, 3> hashes{};
    hashes.fill(UINT64_C(14695981039346656037));
    std::array<std::uint64_t, 3> outputs{};
    std::uint64_t records = 0, txs = 0, rxs = 0, closed = 0;
    std::optional<r::Event> last;
    reader.for_each([&](const r::Event& event) {
        require(event.schema_version == 2 && (!last || r::CanonicalLess{}(*last, event)), "schema/order round trip");
        last = event; ++records;
        if (event.kind == r::EventKind::TransmissionStart) ++txs;
        if (event.kind == r::EventKind::ReceptionSuccess) ++rxs;
        if (event.kind == r::EventKind::TransmissionResolved) ++closed;
        if (event.kind == r::EventKind::ReceiverNmea) {
            const auto& output = std::get<r::ReceiverNmea>(event.payload);
            require(output.receiver_id.value.size() == 11 && output.receiver_id.value.rfind("receiver.R", 0) == 0,
                    "streaming fixture receiver identity");
            const auto index = static_cast<std::size_t>(output.receiver_id.value.back() - '1');
            require(index < 3, "streaming fixture receiver range");
            const auto* expected = output.transmission_id.value.rfind("tx:station.A:", 0) == 0 ? a : b;
            require(output.sentences.size() == 1 && output.sentences.front() == expected, "pinned B3 output changed");
            hash(hashes[index], output.sentences.front()); ++outputs[index];
        }
    });
    require(records == r::kStreamingFixtureRecords && txs == 10800 && rxs == 32400 && closed == txs,
            "five-hour continuous stream count/closure mismatch");
    require(last && last->time == r::from_seconds(18000) && outputs[0] == txs && outputs[1] == txs && outputs[2] == txs,
            "five simulation hours / three complete NMEA streams were not retained");
    bounded(reader.stats());
    return hashes;
}
}
int main(int argc, char** argv) {
    try {
        require(argc == 2, "usage: runtime_streaming_tests WORK_DIR");
        const auto base = fs::u8path(argv[1]); fs::create_directories(base);
        fs::path work;
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        for (unsigned attempt = 0; attempt < 32; ++attempt) {
            const auto candidate = base / ("continuous-" + std::to_string(stamp) + "-" + std::to_string(attempt));
            if (fs::create_directory(candidate)) { work = candidate; break; }
        }
        require(!work.empty(), "cannot reserve streaming test directory");
        const auto small = r::record_streaming_fixture(work / "small", {127, 8 * 1024 * 1024}, false);
        const auto large = r::record_streaming_fixture(work / "large", {4096, 8 * 1024 * 1024}, true);
        const auto partitioned = r::record_streaming_fixture(work / "partitioned", {127, 8 * 1024 * 1024}, true);
        bounded(small); bounded(large); bounded(partitioned);
        require(small.logical_digest == large.logical_digest && small.logical_digest == partitioned.logical_digest,
                "segmentation/causal steps changed canonical logical digest");
        require(small.logical_digest == UINT64_C(0x7805b698778edaca), "pinned StreamingV2 fixture digest changed");
        equal_directories(work / "small", work / "partitioned");
        r::JournalReader first(work / "small"), second(work / "large");
        const auto first_hashes = inspect(first), second_hashes = inspect(second);
        require(first_hashes == second_hashes && first_hashes[0] == first_hashes[1] && first_hashes[1] == first_hashes[2],
                "segmentation changed per-receiver NMEA streams");
        require(first_hashes[0] == UINT64_C(0xc022d14ec8edd1d5), "independent pinned receiver stream digest changed");
        for (const auto speed : {"1", "10"}) {
            Clock clock; DigestSink sink; std::ostringstream diagnostics;
            r::PlaybackOptions options; options.receiver = r::ReceiverId{"receiver.R1"};
            options.speed = r::PlaybackSpeed::parse(speed);
            const auto played = r::play_recording(second, options, clock, sink, diagnostics);
            const auto duration = std::string(speed) == "1" ? INT64_C(18000000000000) : INT64_C(1800000000000);
            require(clock.last_deadline == 17 + duration && sink.digest == first_hashes[0] && sink.records == 10800 &&
                    played.events == r::kStreamingFixtureRecords && diagnostics.str().empty(),
                    "continuous five-hour replay timing/content failed (fake clock)");
        }
        std::cout << "PASS INFRASTRUCTURE TEST -- NOT AIS RF SIMULATION: simulation_seconds=18000 TX=10800 RX=32400 records="
                  << small.records << " segments=" << small.segments << '/' << large.segments
                  << " files=" << (small.segments * 2 + 2) << '/' << (large.segments * 2 + 2)
                  << " digest=" << std::hex << small.logical_digest << " receiver_digest=" << first_hashes[0] << std::dec
                  << " peak_TX=" << small.validator.peak_active_transmissions
                  << " peak_RX=" << small.validator.peak_active_receptions
                  << " peak_pairs=" << small.validator.peak_active_receiver_pairs
                  << " peak_anchors=" << small.validator.peak_typed_anchors
                  << " retained_recent=" << small.retained_recent_events
                  << "; x1/x10 fake clocks, bounded generator, same step-partition bytes; artifacts=" << work.u8string() << '\n';
        return 0;
    } catch (const std::exception& error) { std::cerr << "streaming infrastructure FAIL: " << error.what() << '\n'; return 1; }
}
