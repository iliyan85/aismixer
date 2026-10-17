#include "fixture.h"
#include "journal.h"
#include "playback.h"
#include "scheduler.h"

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <streambuf>
#include <string>
#include <vector>

namespace {
using namespace aismixer::lab::runtime;
constexpr const char* kGolden = "!AIVDM,1,1,,A,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*2B\r\n";
std::size_t invalid_cases = 0;
void require(bool condition, const char* reason) {
    if (!condition) throw std::runtime_error(reason);
}
template <typename Action> void rejected(Action action) {
    bool failed = false;
    try { action(); } catch (const std::exception&) { failed = true; }
    require(failed, "expected invalid/error path was accepted");
    ++invalid_cases;
}
std::string read_file(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("test cannot read output");
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}
JournalMetadata metadata(const Fixture& fixture) {
    JournalMetadata result;
    result.provenance = fixture.provenance;
    result.utc_anchor = fixture.utc_anchor;
    result.model_version = "r0-scripted-infrastructure-fixture-v1";
    result.codec_version = "B3-pinned-finalized-NMEA-bytes-v1";
    result.receivers = fixture.receivers;
    return result;
}
void record(const Fixture& fixture, const std::filesystem::path& path, std::uint32_t segment_records) {
    JournalOptions options;
    options.max_records_per_segment = segment_records;
    JournalWriter writer(path, metadata(fixture), options);
    Scheduler scheduler;
    for (const auto& event : fixture.events) scheduler.enqueue(event);
    scheduler.finish([&](const Event& event) { writer.append(event); });
    writer.finish();
}
class FakeClock final : public MonotonicClock {
public:
    std::int64_t now = 1234567;
    std::int64_t oversleep = 0;
    std::vector<std::int64_t> deadlines;
    std::function<void(std::int64_t)> before_sleep;
    std::int64_t now_ns() override { return now; }
    void sleep_until_ns(std::int64_t deadline) override {
        require(deadline >= now, "fake clock received a relative/past deadline");
        if (before_sleep) before_sleep(deadline);
        deadlines.push_back(deadline);
        now = deadline + oversleep;
    }
};
struct Played { std::string bytes, diagnostics; PlaybackStats stats; FakeClock clock; };
Played play(const JournalReader& reader, const char* speed, const char* categories,
            std::optional<std::string> view = std::nullopt, const char* receiver = "receiver.R1",
            std::int64_t oversleep = 0) {
    Played result;
    result.clock.oversleep = oversleep;
    std::ostringstream output, diagnostics;
    StreamOutputSink sink(output, {receiver});
    PlaybackOptions options;
    options.speed = PlaybackSpeed::parse(speed);
    options.receiver = ReceiverId{receiver};
    options.console = parse_console_options(categories, std::move(view));
    result.stats = play_recording(reader, options, result.clock, sink, diagnostics);
    result.bytes = output.str(); result.diagnostics = diagnostics.str();
    require(sink.counters().records == result.stats.output_records &&
            sink.counters().bytes == result.stats.output_bytes, "sender/playback counters differ");
    return result;
}

void clock_and_playback(const std::filesystem::path& work) {
    auto fixture = make_synthetic_fixture();
    // Stretch only the final scripted OFF group; no wall-clock sleep occurs.
    for (auto& event : fixture.events) if (event.time == from_seconds(4)) event.time = from_seconds(18000);
    record(fixture, work / "five-hours", 2);
    JournalReader reader(work / "five-hours");
    const auto digest = reader.stats().logical_digest;
    const auto one = play(reader, "1", "disabled");
    const auto ten = play(reader, "10", "all");
    const auto fast = play(reader, "fast", "output", "receiver.R2");
    require(one.bytes == kGolden && one.bytes == ten.bytes && one.bytes == fast.bytes,
            "replay speeds/console filters changed finalized NMEA bytes");
    require(one.diagnostics.empty() && !ten.diagnostics.empty() && fast.diagnostics.find("receiver.R1") == std::string::npos,
            "console disabled/receiver view leaked display-only data");
    require(!one.clock.deadlines.empty() && one.clock.deadlines.back() == 1234567 + INT64_C(18000000000000),
            "x1 five-hour schedule did not use exact absolute deadline");
    require(ten.clock.deadlines.back() == 1234567 + INT64_C(1800000000000), "x10 five-hour schedule duration");
    require(fast.clock.deadlines.empty() && fast.clock.now == 1234567, "fast replay slept or advanced fake wall clock");
    const auto overslept = play(reader, "10", "disabled", std::nullopt, "receiver.R1", 77);
    require(overslept.clock.deadlines.back() == ten.clock.deadlines.back() && overslept.stats.late_events > 0,
            "sleep lateness accumulated into future deadlines");
    const auto station = play(reader, "fast", "lifecycle,rf", "station.A");
    const auto receiver = play(reader, "fast", "all", "receiver.R1");
    require(station.stats.displayed_events == 4 && receiver.stats.displayed_events == 4 &&
            station.bytes == receiver.bytes && station.diagnostics.find("station_on") != std::string::npos,
            "station/receiver observational filters changed stream or direct-ID association");
    require(reader.stats().logical_digest == digest && JournalReader(work / "five-hours").stats().logical_digest == digest,
            "playback/filtering modified canonical journal");
    require(play(reader, "fast", "disabled", std::nullopt, "receiver.R2").bytes == kGolden &&
            play(reader, "fast", "disabled", std::nullopt, "receiver.R3").bytes.empty(),
            "independent successful/failed receiver streams changed");
    record(fixture, work / "large-segments", 65536);
    JournalReader other(work / "large-segments");
    require(other.stats().logical_digest == digest && play(other, "fast", "all").bytes == one.bytes,
            "journal segment size changed playback history/content");
    for (const auto& speed : {"0.5", "1", "2", "10", "100"}) {
        const auto parsed = PlaybackSpeed::parse(speed);
        require(playback_offset_ns(kUnitsPerSecond, parsed) ==
                    INT64_C(1000000000) * parsed.denominator() / parsed.numerator(),
                "speed preset rational deadline arithmetic");
    }
    require(playback_offset_ns(1, PlaybackSpeed::parse("1")) == 101 &&
            playback_offset_ns(1, PlaybackSpeed::parse("100")) == 1, "nanosecond floor rounding contract");
    for (const auto* bad : {"0", "-1", "NaN", "inf", "1.5", "", "1e3"}) rejected([&] { PlaybackSpeed::parse(bad); });
    rejected([&] { playback_offset_ns(-1, PlaybackSpeed::parse("fast")); });
    rejected([&] { playback_offset_ns(std::numeric_limits<std::int64_t>::max(), PlaybackSpeed::parse("0.5")); });
    rejected([&] { select_receiver(reader.metadata(), std::nullopt); });
    rejected([&] { select_receiver(reader.metadata(), ReceiverId{"missing"}); });
    for (const auto* bad : {"", "all,rf", "rf,rf", "rf,", "unknown"}) rejected([&] { parse_console_options(bad); });
    require(console_category(EventKind::SlotMapInit) == ConsoleCategory::SlotMap &&
            console_category(EventKind::TdmaDecision) == ConsoleCategory::Tdma, "console category contracts");
    std::ostringstream output, diagnostics;
    StreamOutputSink sink(output, {"receiver.R1"});
    PlaybackOptions options; options.receiver = ReceiverId{"receiver.R1"};
    FakeClock overflow; overflow.now = std::numeric_limits<std::int64_t>::max() - 4;
    rejected([&] { play_recording(reader, options, overflow, sink, diagnostics); });
    require(output.str().empty(), "deadline overflow partially delivered future records");
    { JournalWriter unfinished(work / "incomplete", metadata(fixture)); }
    JournalReader incomplete(work / "incomplete", true);
    require(incomplete.status() == JournalStatus::Incomplete, "explicit inspection lost incomplete run status");
    rejected([&] { play_recording(incomplete, options, overflow, sink, diagnostics); });
    require(output.str().empty(), "incomplete journal leaked playback bytes");
    class ObservedBuffer final : public std::stringbuf {
    public:
        std::size_t flushed_bytes = 0;
    protected:
        int sync() override { flushed_bytes = str().size(); return 0; }
    } observed_buffer;
    std::ostream observed_stream(&observed_buffer);
    StreamOutputSink observed_sink(observed_stream, {"receiver.R1"});
    FakeClock observed_clock;
    bool saw_final_sleep = false;
    observed_clock.before_sleep = [&](std::int64_t deadline) {
        if (deadline == 1234567 + INT64_C(18000000000000)) {
            saw_final_sleep = true;
            require(observed_buffer.flushed_bytes == std::string(kGolden).size(),
                    "scheduled NMEA remained buffered across later five-hour sleep");
        }
    };
    play_recording(reader, options, observed_clock, observed_sink, diagnostics);
    require(saw_final_sleep && observed_buffer.str() == kGolden, "timed observable-stream flush regression did not run");
    std::cout << "PASS five simulated hours x1/x10/fast with injected clock, absolute deadlines, filters and exact stored bytes\n";
}

class FailingBuffer final : public std::streambuf {
public:
    explicit FailingBuffer(std::size_t limit, bool flush_failure = false) : remaining(limit), fail_flush(flush_failure) {}
    std::size_t remaining;
    bool fail_flush;
    std::string bytes;
protected:
    std::streamsize xsputn(const char* data, std::streamsize count) override {
        const auto accepted = std::min(remaining, static_cast<std::size_t>(count));
        bytes.append(data, accepted); remaining -= accepted;
        return static_cast<std::streamsize>(accepted);
    }
    int sync() override { return fail_flush ? -1 : 0; }
};

void outputs(const std::filesystem::path& work) {
    const auto fixture = make_synthetic_fixture();
    std::vector<ReceiverNmea> records;
    for (const auto& event : fixture.events) if (event.kind == EventKind::ReceiverNmea) records.push_back(std::get<ReceiverNmea>(event.payload));
    require(records.size() == 2, "fixture output cardinality");
    const auto& a = records[0]; const auto& b = records[1];
    ReceiverQueues queues(fixture.receivers, {1, 1024});
    require(queues.try_enqueue({0}, a) && !queues.try_enqueue({1}, a) && queues.try_enqueue({0}, b),
            "per-receiver queue backpressure blocked another receiver or discarded a record");
    std::ostringstream receiver_b;
    StreamOutputSink good(receiver_b, b.receiver_id);
    require(queues.drain_one(b.receiver_id, good), "independent receiver queue did not drain");
    FailingBuffer buffer(5); std::ostream broken(&buffer); StreamOutputSink bad(broken, a.receiver_id);
    rejected([&] { queues.drain_one(a.receiver_id, bad); });
    require(queues.state(a.receiver_id).failed && queues.state(a.receiver_id).records == 1 &&
            queues.state(a.receiver_id).counters.errors == 1 && queues.state(a.receiver_id).counters.backpressure == 1,
            "failed output silently popped/retried/dropped receiver record");
    require(queues.try_enqueue({1}, b) && queues.drain_one(b.receiver_id, good) && receiver_b.str() == std::string(kGolden) + kGolden,
            "one sink failure corrupted another receiver stream");
    rejected([&] { queues.enqueue_or_throw({2}, a); });
    FailingBuffer flush_buffer(1024, true); std::ostream flush_stream(&flush_buffer);
    StreamOutputSink flush_sink(flush_stream, a.receiver_id);
    rejected([&] { flush_sink.deliver({0}, a); });
    require(flush_sink.counters().errors == 1 && flush_sink.counters().records == 0,
            "record flush failure was counted as successfully delivered");
    rejected([&] { flush_sink.flush(); });
    rejected([&] { good.deliver({0}, a); });
    ReceiverQueues ordered(fixture.receivers, {2, 1024});
    ordered.enqueue_or_throw({2}, a);
    rejected([&] { ordered.enqueue_or_throw({1}, a); });
    rejected([&] { ReceiverQueues invalid(fixture.receivers, {0, 1024}); });
    ReceiverQueues full(fixture.receivers, {1, 1024});
    full.enqueue_or_throw({0}, a);
    rejected([&] { full.enqueue_or_throw({1}, a); });
    require(full.state(a.receiver_id).records == 1, "explicit full-queue error dropped queued data");
    EndpointBindings bindings(fixture.receivers);
    bindings.bind(a.receiver_id, {"127.0.0.1", 9001});
    rejected([&] { bindings.bind(b.receiver_id, {"127.0.0.1", 9001}); });
    rejected([&] { bindings.bind(a.receiver_id, {"127.0.0.1", 9002}); });
    for (const auto& endpoint : std::vector<Endpoint>{{"localhost", 9002}, {"127.00.0.1", 9002}, {"::1", 9002}, {"127.0.0.1", 0}, {"127.0.0.1", 65536}}) {
        rejected([&] { bindings.bind(b.receiver_id, endpoint); });
    }
    bindings.bind(b.receiver_id, {"127.0.0.1", 9002});
    rejected([&] { bindings.bind({"unknown.receiver"}, {"127.0.0.1", 9003}); });
    require(bindings.endpoint(a.receiver_id).port == 9001 && bindings.endpoint(b.receiver_id).port == 9002,
            "one-receiver/one-endpoint contract changed");
    const auto file = work / "recorded.nmea";
    { FileOutputSink sink(file, a.receiver_id); sink.deliver({0}, a); sink.complete(); }
    require(read_file(file) == kGolden, "atomic file sink re-encoded or changed exact NMEA bytes");
    rejected([&] { FileOutputSink existing(file, a.receiver_id); });
    require(read_file(file) == kGolden, "existing playback output was overwritten");
    const auto raced = work / "competing.nmea";
    {
        FileOutputSink sink(raced, a.receiver_id); sink.deliver({0}, a);
        { std::ofstream competitor(raced, std::ios::binary); competitor << "competing destination"; }
        rejected([&] { sink.complete(); });
    }
    require(read_file(raced) == "competing destination", "late destination race clobbered existing bytes");
    const auto abandoned = work / "abandoned.nmea";
    { FileOutputSink sink(abandoned, a.receiver_id); sink.deliver({0}, a); }
    require(!std::filesystem::exists(abandoned), "unfinished playback output was presented as completed");
    for (const auto& entry : std::filesystem::directory_iterator(work)) {
        require(entry.path().filename().u8string().find(".r0-tmp-") == std::string::npos, "handled playback output left owned temporary");
    }
    std::cout << "PASS bounded independent receiver queues/backpressure/errors, endpoint bindings, exact bytes and no-clobber file publication\n";
}

}  // namespace
int main(int argc, char** argv) {
    try {
        require(argc == 2, "usage: runtime_playback_tests WORK_DIR");
        const auto base = std::filesystem::u8path(argv[1]);
        std::filesystem::create_directories(base);
        std::filesystem::path work;
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        for (unsigned attempt = 0; attempt < 32; ++attempt) {
            const auto candidate = base / ("playback-" + std::to_string(stamp) + "-" + std::to_string(attempt));
            if (std::filesystem::create_directory(candidate)) { work = candidate; break; }
        }
        require(!work.empty(), "cannot reserve unique playback test directory");
        clock_and_playback(work); outputs(work);
        std::cout << "PASS " << invalid_cases << " invalid/output-error playback cases; no real waits/network/GPU\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL runtime playback: " << error.what() << '\n'; return 1;
    }
}
