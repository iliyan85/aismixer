#include "fixture.h"
#include "journal.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#ifndef AISMIXER_LAB_RUNTIME_JOURNAL_TESTING
#error Native journal fault tests require the private journal testing definition
#endif

namespace {
using namespace aismixer::lab::runtime;
namespace fs = std::filesystem;
using Bytes = std::vector<std::uint8_t>;
std::size_t rejections = 0;

void require(bool condition, std::string_view message) {
    if (!condition) throw std::runtime_error(std::string(message));
}
template <class Action> void rejects(Action action, std::string_view diagnostic = {}) {
    bool caught = false;
    try { action(); }
    catch (const std::exception& error) {
        require(diagnostic.empty() || std::string_view(error.what()).find(diagnostic) != std::string_view::npos,
                std::string("unexpected rejection: ") + error.what());
        caught = true;
    }
    require(caught, "expected journal rejection");
    ++rejections;
}

class Scratch {
public:
    explicit Scratch(const fs::path& work) : parent_(fs::absolute(work).lexically_normal()) {
        // CTest supplies a dedicated work root that may not exist on first run.
        // Leave that parent intact; cleanup owns only the exclusive child below.
        std::error_code parent_error;
        fs::create_directories(parent_, parent_error);
        require(!parent_error, "cannot create journal test work directory");
        require(fs::is_directory(parent_), "journal test work directory is missing");
        const auto nonce = std::chrono::steady_clock::now().time_since_epoch().count();
        for (unsigned attempt = 0; attempt < 100; ++attempt) {
            path_ = parent_ / ("runtime-journal-tests-" + std::to_string(nonce) + "-" + std::to_string(attempt));
            std::error_code error;
            if (fs::create_directory(path_, error)) return;
            require(!error || error == std::errc::file_exists, "cannot create exclusive test scratch directory");
        }
        throw std::runtime_error("exclusive test scratch names exhausted");
    }
    ~Scratch() {
        // The exclusive, directly owned child is the only recursive cleanup target.
        if (path_.parent_path() == parent_ && path_.filename().string().rfind("runtime-journal-tests-", 0) == 0) {
            std::error_code ignored;
            fs::remove_all(path_, ignored);
        }
    }
    fs::path operator/(const fs::path& child) const { return path_ / child; }
private:
    fs::path parent_, path_;
};

Bytes read(const fs::path& path) {
    std::ifstream stream(path, std::ios::binary);
    require(static_cast<bool>(stream), "test cannot read file");
    return Bytes(std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>());
}
void write(const fs::path& path, const Bytes& bytes) {
    std::ofstream stream(path, std::ios::binary | std::ios::trunc);
    require(static_cast<bool>(stream), "test cannot write file");
    stream.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    stream.flush();
    require(static_cast<bool>(stream), "test write failed");
    stream.close();
    require(!stream.fail(), "test close failed");
}
void write(const fs::path& path, const std::string& value) { write(path, Bytes(value.begin(), value.end())); }
void no_temporary(const fs::path& directory) {
    for (const auto& entry : fs::directory_iterator(directory))
        require(entry.path().filename().u8string().find(".aismixer-tmp-") == std::string::npos,
                "owned partial temporary file leaked");
}
fs::path clone(const fs::path& original, const fs::path& target) {
    require(fs::create_directory(target), "copy scratch already exists");
    for (const auto& entry : fs::directory_iterator(original)) {
        require(entry.is_regular_file(), "unexpected fixture subdirectory");
        require(fs::copy_file(entry.path(), target / entry.path().filename()), "cannot clone fixture file");
    }
    return target;
}
JournalMetadata metadata(const Fixture& fixture) {
    return {fixture.provenance, UINT64_C(0x0123456789abcdef), fixture.utc_anchor,
            "runtime-fixture-v1", "qualified-b3-stored-vector-v1", fixture.receivers};
}
Event station(std::int64_t time, std::uint64_t sequence, bool on = true) {
    Event event;
    event.time = {time}; event.kind = on ? EventKind::StationOn : EventKind::StationOff;
    event.source_id = "A"; event.subject_id = "A"; event.event_id = {"A", sequence};
    event.payload = StationLifecycle{"A"};
    return event;
}
void add_number(Bytes& bytes, std::uint64_t value, unsigned width) {
    for (unsigned index = 0; index < width; ++index) {
        bytes.push_back(static_cast<std::uint8_t>(value & 255u)); value >>= 8;
    }
}
void independent_hash(std::uint64_t& hash, const Bytes& bytes) {
    for (const auto byte : bytes) { hash ^= byte; hash *= UINT64_C(1099511628211); }
}
std::uint64_t logical_digest(const std::vector<Event>& events) {
    auto hash = UINT64_C(14695981039346656037);
    for (const auto& event : events) {
        const auto bytes = encode_event(event);
        Bytes length; add_number(length, bytes.size(), 4);
        independent_hash(hash, length); independent_hash(hash, bytes);
    }
    return hash;
}
void save_fixture(const fs::path& path, const Fixture& fixture, JournalOptions options = {}) {
    JournalWriter writer(path, metadata(fixture), options);
    for (const auto& event : fixture.events) writer.append(event);
    writer.finish();
    no_temporary(path);
}

void portable_event_codec(const Fixture& fixture) {
    const std::string check = "123456789";
    require(crc32c(reinterpret_cast<const std::uint8_t*>(check.data()), check.size()) == 0xe3069283u,
            "CRC32C independent Castagnoli check vector");
    require(crc32c(nullptr, 0) == 0, "empty CRC32C vector");
    rejects([] { (void)crc32c(nullptr, 1); });
    // Explicit hand-pinned LE wire bytes, including signed -2, one-character
    // IDs, sequence one, absent cause and the typed station payload.
    const Bytes expected{
        1,0,0,0, 254,255,255,255,255,255,255,255, 1,0,
        1,0,0,0,'A', 1,0,0,0,'A', 1,0,0,0,'A',
        1,0,0,0,0,0,0,0, 0, 1,0,0,0,'A'};
    require(encode_event(station(-2, 1)) == expected, "pinned portable event wire vector");
    require(decode_event(expected).time.units == -2, "negative signed wire time");
    auto extreme = station(std::numeric_limits<std::int64_t>::min(), UINT64_MAX);
    require(encode_event(decode_event(encode_event(extreme))) == encode_event(extreme), "signed minimum and unsigned maximum wire round trip");

    std::vector<Event> samples = fixture.events;
    for (const auto kind : {EventKind::SlotMapInit, EventKind::SlotMapReset, EventKind::SlotReserve, EventKind::SlotRelease}) {
        auto event = station(1, 2); event.kind = kind;
        event.payload = SlotMapChange{"A", 1, 2249, "UTF-8 detail: \xD0\x91"}; samples.push_back(event);
    }
    auto decision = station(1, 2); decision.kind = EventKind::TdmaDecision;
    decision.payload = TdmaDecision{"A", "scripted local choice"}; samples.push_back(decision);
    auto rf = station(-2, 2); rf.kind = EventKind::RfObservation; rf.subject_id = "R";
    rf.causal_ref = EventId{"other", UINT64_MAX};
    rf.payload = RfObservation{{"T"}, {"R"}, {-2}, true, "A", "delivered observation"}; samples.push_back(rf);
    std::get<RfObservation>(rf.payload).carrier_detected = false;
    std::get<RfObservation>(rf.payload).local_station_id.reset(); samples.push_back(rf);
    auto error = station(4, 2); error.kind = EventKind::OutputError; error.subject_id = "R";
    error.payload = OutputError{{"R"}, "OUTPUT_WRITE", "bounded detail"}; samples.push_back(error);
    std::vector<bool> kinds(17, false);
    for (const auto& sample : samples) {
        const auto bytes = encode_event(sample);
        const auto decoded = decode_event(bytes);
        require(encode_event(decoded) == bytes, "typed event round trip changed bytes");
        require(decoded.kind == sample.kind && decoded.event_id == sample.event_id, "typed event identity round trip");
        kinds.at(static_cast<std::size_t>(sample.kind)) = true;
    }
    for (std::size_t kind = 1; kind <= 16; ++kind) require(kinds[kind], "missing typed payload family in wire tests");
    for (std::size_t size = 0; size < expected.size(); ++size)
        rejects([&] { (void)decode_event(Bytes(expected.begin(), expected.begin() + static_cast<std::ptrdiff_t>(size))); });
    auto bad = expected; bad[0] = 3; rejects([&] { (void)decode_event(bad); }, "schema");
    bad = expected; bad[12] = 255; rejects([&] { (void)decode_event(bad); }, "kind");
    bad = expected; bad[37] = 2; rejects([&] { (void)decode_event(bad); }, "boolean");
    bad = expected; bad[14] = 255; bad[15] = 255; rejects([&] { (void)decode_event(bad); }, "string");
    bad = expected; bad.push_back(0); rejects([&] { (void)decode_event(bad); }, "trailing");
    rejects([] { (void)decode_event(Bytes(kMaxJournalRecordBytes + 1)); }, "length");
}

void round_trip_and_bounds(const Scratch& scratch, const Fixture& fixture) {
    const auto small = scratch / "one-record-segments";
    const auto large = scratch / fs::u8path(u8"\u0436\u0443\u0440\u043d\u0430\u043b-\u6D4B\u8BD5");
    save_fixture(small, fixture, {1, 2048}); save_fixture(large, fixture);
    JournalReader a(small), b(large);
    require(a.status() == JournalStatus::Complete && b.status() == JournalStatus::Complete, "COMPLETE status");
    require(a.stats().records == fixture.events.size() && a.stats().segments == fixture.events.size(), "one record segment accounting");
    require(b.stats().segments == 1, "large segment accounting");
    require(a.stats().logical_digest == b.stats().logical_digest && a.stats().logical_digest == logical_digest(fixture.events),
            "logical content digest must ignore segmentation");
    require(a.metadata().effective_seed == metadata(fixture).effective_seed, "seed provenance");
    require(a.metadata().provenance == fixture.provenance && a.metadata().utc_anchor.posix_seconds == fixture.utc_anchor.posix_seconds &&
            a.metadata().utc_anchor.subsecond_units == fixture.utc_anchor.subsecond_units &&
            a.metadata().model_version == "runtime-fixture-v1" && a.metadata().codec_version == "qualified-b3-stored-vector-v1",
            "manifest metadata round trip");
    std::size_t index = 0;
    std::map<std::string, std::string> output;
    a.for_each([&](const Event& event) {
        require(index < fixture.events.size() && encode_event(event) == encode_event(fixture.events[index++]), "streamed record order/content");
        if (event.kind == EventKind::ReceiverNmea) {
            const auto& payload = std::get<ReceiverNmea>(event.payload);
            for (const auto& sentence : payload.sentences) output[payload.receiver_id.value] += sentence;
        }
    });
    require(index == fixture.events.size(), "streamed record count");
    const std::string golden = "!AIVDM,1,1,,A,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*2B\r\n";
    require(output.size() == 2 && output.at("receiver.R1") == golden && output.at("receiver.R2") == golden,
            "exact retained per-receiver NMEA CRLF bytes");
    require(a.stats().peak_buffer_bytes <= kMaxJournalRecordBytes + 44, "streaming reader record bound");
    rejects([&] { a.for_each({}); }, "callback");
    const auto before = read(small / "manifest-incomplete.bin");
    rejects([&] { JournalWriter other(small, metadata(fixture)); }, "exists");
    require(read(small / "manifest-incomplete.bin") == before, "existing run was changed");

    const auto empty = scratch / "empty-complete";
    JournalWriter writer(empty, metadata(fixture)); writer.finish();
    JournalReader empty_reader(empty);
    require(empty_reader.stats().records == 0 && empty_reader.stats().segments == 0 &&
            empty_reader.stats().logical_digest == UINT64_C(14695981039346656037), "empty complete run");
    rejects([&] { writer.finish(); }, "COMPLETE");
    rejects([&] { writer.append(fixture.events.front()); }, "COMPLETE");

    auto unordered_metadata = metadata(fixture);
    std::reverse(unordered_metadata.receivers.begin(), unordered_metadata.receivers.end());
    unordered_metadata.effective_seed.reset();
    const auto sorted = scratch / "sorted-catalog";
    JournalWriter sorted_writer(sorted, unordered_metadata); sorted_writer.finish();
    JournalReader sorted_reader(sorted);
    require(!sorted_reader.metadata().effective_seed && sorted_reader.metadata().receivers.front().value == "receiver.R1", "canonical receiver metadata and absent seed");

    auto bad = metadata(fixture); bad.receivers.push_back(bad.receivers.front());
    rejects([&] { JournalWriter invalid(scratch / "duplicate-catalog", bad); }, "duplicate");
    bad = metadata(fixture); bad.receivers[0].value = "invalid receiver";
    rejects([&] { JournalWriter invalid(scratch / "invalid-catalog", bad); }, "identity");
    bad = metadata(fixture); bad.provenance = std::string("bad") + '\0';
    rejects([&] { JournalWriter invalid(scratch / "bad-utf8", bad); }, "UTF-8");
    bad = metadata(fixture); bad.utc_anchor.subsecond_units = kUnitsPerSecond;
    rejects([&] { JournalWriter invalid(scratch / "bad-anchor", bad); });
    rejects([&] { JournalWriter invalid(scratch / "bad-count", metadata(fixture), {0, 2048}); });
    rejects([&] { JournalWriter invalid(scratch / "big-count", metadata(fixture), {65537, 2048}); });
    rejects([&] { JournalWriter invalid(scratch / "big-bytes", metadata(fixture), {1, kMaxJournalSegmentBytes + 1}); });
    const auto too_small = scratch / "too-small";
    JournalWriter too_small_writer(too_small, metadata(fixture), {1, 49});
    rejects([&] { too_small_writer.append(fixture.events.front()); }, "fit");
    rejects([&] { too_small_writer.finish(); }, "failed");
    require(!fs::exists(too_small / "manifest-complete.bin"), "oversized record marked COMPLETE");

    bad = metadata(fixture); bad.receivers.clear();
    const auto missing = scratch / "missing-catalog";
    JournalWriter missing_writer(missing, bad);
    const auto receiver = std::find_if(fixture.events.begin(), fixture.events.end(), [](const Event& event) { return event.kind == EventKind::ReceiverOn; });
    require(receiver != fixture.events.end(), "fixture lacks receiver lifecycle");
    rejects([&] { missing_writer.append(*receiver); }, "catalog");
    rejects([&] { missing_writer.finish(); }, "failed");
}

void incomplete_and_faults(const Scratch& scratch, const Fixture& fixture) {
    const auto abandoned = scratch / "abandoned";
    {
        JournalWriter writer(abandoned, metadata(fixture), {1, 2048});
        writer.append(fixture.events.front());
    }
    rejects([&] { JournalReader reader(abandoned); }, "INCOMPLETE");
    JournalReader inspected(abandoned, true);
    require(inspected.status() == JournalStatus::Incomplete && inspected.stats().records == 1, "committed incomplete prefix");
    write(abandoned / "segment-0000000002.bin", "uncommitted segment ignored");
    write(abandoned / "checkpoint-0000000002.bin.aismixer-tmp-stray", "interrupted temporary ignored");
    require(JournalReader(abandoned, true).stats().records == 1, "stray files became committed events");
    const auto buffered = scratch / "buffered-abandoned";
    { JournalWriter writer(buffered, metadata(fixture)); writer.append(fixture.events.front()); }
    require(JournalReader(buffered, true).stats().records == 0, "unpublished buffered event became committed");

    using testing::JournalFault;
    for (const auto fault : {JournalFault::Write, JournalFault::Flush, JournalFault::Close, JournalFault::Publish}) {
        const auto directory = scratch / ("segment-fault-" + std::to_string(static_cast<unsigned>(fault)));
        testing::JournalHooks hooks; hooks.fault = fault; hooks.publication_number = 2;
        auto writer = testing::make_journal_writer(directory, metadata(fixture), {1, 2048}, hooks);
        rejects([&] { writer.append(fixture.events.front()); }, "journal");
        rejects([&] { writer.finish(); }, "failed");
        require(!fs::exists(directory / "manifest-complete.bin") && !fs::exists(directory / "segment-0000000001.bin"), "failed segment published evidence");
        require(JournalReader(directory, true).stats().records == 0, "failed segment entered committed prefix");
        no_temporary(directory);
    }
    const auto orphan = scratch / "checkpoint-fault";
    testing::JournalHooks checkpoint_fault; checkpoint_fault.fault = JournalFault::Publish; checkpoint_fault.publication_number = 3;
    auto orphan_writer = testing::make_journal_writer(orphan, metadata(fixture), {1, 2048}, checkpoint_fault);
    rejects([&] { orphan_writer.append(fixture.events.front()); });
    require(fs::exists(orphan / "segment-0000000001.bin") && !fs::exists(orphan / "checkpoint-0000000001.bin"), "orphan segment evidence shape");
    require(JournalReader(orphan, true).stats().records == 0, "orphan segment was consumed");
    no_temporary(orphan);

    const auto interrupted = scratch / "interrupted-before-complete";
    testing::JournalHooks before_complete; before_complete.fault = JournalFault::BeforeComplete;
    auto interrupted_writer = testing::make_journal_writer(interrupted, metadata(fixture), {3, 4096}, before_complete);
    for (const auto& event : fixture.events) interrupted_writer.append(event);
    rejects([&] { interrupted_writer.finish(); }, "COMPLETE");
    rejects([&] { JournalReader reader(interrupted); }, "INCOMPLETE");
    require(JournalReader(interrupted, true).stats().records == fixture.events.size(), "interrupted fully committed stream inspection");
    require(!fs::exists(interrupted / "manifest-complete.bin"), "interruption manufactured COMPLETE");
    no_temporary(interrupted);

    const auto race = scratch / "publication-race";
    const std::string competitor = "COMPETITOR MUST REMAIN EXACT";
    testing::JournalHooks racing; racing.publication_number = 2;
    racing.before_publish = [&](const fs::path& target) { write(target, competitor); };
    auto racing_writer = testing::make_journal_writer(race, metadata(fixture), {1, 2048}, racing);
    rejects([&] { racing_writer.append(fixture.events.front()); }, "no-clobber");
    require(read(race / "segment-0000000001.bin") == Bytes(competitor.begin(), competitor.end()), "late competitor was overwritten");
    require(JournalReader(race, true).stats().records == 0, "competing uncommitted file entered prefix");
    no_temporary(race);

    const auto rejected = scratch / "rejected-event";
    JournalWriter rejected_writer(rejected, metadata(fixture), {1, 2048});
    rejected_writer.append(fixture.events.front());
    rejects([&] { rejected_writer.append(fixture.events.front()); });
    rejects([&] { rejected_writer.finish(); }, "failed");
    require(JournalReader(rejected, true).stats().records == 1, "rejected event changed committed prefix");
}

void unfinished_terminal_preserves_prefix(const Scratch& scratch, const Fixture& fixture) {
    std::vector<Event> accepted;
    for (const auto& event : fixture.events) {
        accepted.push_back(event);
        if (event.kind == EventKind::TransmissionStart) break;
    }
    require(accepted.size() > 2 && accepted.back().kind == EventKind::TransmissionStart,
            "unfinished terminal fixture must contain a proven open TX");
    for (const auto limit : {std::uint32_t{1}, std::uint32_t{4096}}) {
        const auto directory = scratch / ("unfinished-terminal-" + std::to_string(limit));
        JournalWriter writer(directory, metadata(fixture), {limit, kMaxJournalSegmentBytes});
        for (const auto& event : accepted) writer.append(event);
        rejects([&] { writer.finish(); }, "unfinished");
        require(!fs::exists(directory / "manifest-complete.bin"), "unfinished TX marked COMPLETE");
        rejects([&] { writer.finish(); }, "failed");
        rejects([&] { writer.append(fixture.events.back()); }, "failed");
        rejects([&] { JournalReader complete(directory); }, "INCOMPLETE");
        JournalReader inspected(directory, true);
        require(inspected.stats().records == accepted.size() &&
                inspected.stats().logical_digest == logical_digest(accepted),
                "terminal validation lost accepted buffered prefix");
        std::size_t index = 0;
        inspected.for_each([&](const Event& event) {
            require(index < accepted.size() && encode_event(event) == encode_event(accepted[index++]),
                    "terminal failure prefix changed canonical bytes");
        });
        require(index == accepted.size(), "terminal failure prefix ended early");
        const auto initial = read(directory / "manifest-incomplete.bin");
        rejects([&] { JournalWriter clobber(directory, metadata(fixture)); }, "exists");
        require(read(directory / "manifest-incomplete.bin") == initial, "terminal failure run overwritten");
        no_temporary(directory);
    }
}

void integrity_rejections(const Scratch& scratch, const Fixture& fixture) {
    const auto original = scratch / "integrity-original";
    save_fixture(original, fixture, {1, 2048});
    const auto damaged_segment = [&](const char* name, const auto& mutation) {
        const auto copy = clone(original, scratch / name);
        const auto path = copy / "segment-0000000001.bin";
        auto bytes = read(path); mutation(bytes); write(path, bytes);
        rejects([&] { JournalReader reader(copy); });
    };
    damaged_segment("crc-corrupt", [](Bytes& bytes) { bytes.at(45) ^= 1; });
    damaged_segment("truncated", [](Bytes& bytes) { bytes.resize(bytes.size() - 1); });
    damaged_segment("oversized-record", [](Bytes& bytes) { for (std::size_t index = 36; index < 40; ++index) bytes.at(index) = 255; });
    damaged_segment("unknown-segment-version", [](Bytes& bytes) { bytes.at(8) = 2; });
    damaged_segment("trailing-segment", [](Bytes& bytes) { bytes.push_back(0); });
    auto missing = clone(original, scratch / "missing-segment");
    require(fs::remove(missing / "segment-0000000001.bin"), "test missing segment setup");
    rejects([&] { JournalReader reader(missing); }, "missing");
    missing = clone(original, scratch / "missing-checkpoint");
    require(fs::remove(missing / "checkpoint-0000000001.bin"), "test missing checkpoint setup");
    rejects([&] { JournalReader reader(missing); }, "checkpoint");
    const auto extra = clone(original, scratch / "extra-checkpoint");
    require(fs::copy_file(extra / "checkpoint-0000000001.bin", extra / "checkpoint-0000000018.bin"), "test extra checkpoint setup");
    rejects([&] { JournalReader reader(extra); }, "checkpoint");

    for (const auto* filename : {"manifest-incomplete.bin", "checkpoint-0000000001.bin", "manifest-complete.bin"}) {
        const auto copy = clone(original, scratch / (std::string("bad-") + filename));
        auto bytes = read(copy / filename); bytes.at(24) ^= 1; write(copy / filename, bytes);
        rejects([&] { JournalReader reader(copy); }, "CRC32C");
    }
    const auto manifest_size = clone(original, scratch / "oversized-manifest");
    auto bytes = read(manifest_size / "manifest-incomplete.bin");
    for (std::size_t index = 20; index < 24; ++index) bytes.at(index) = 255;
    write(manifest_size / "manifest-incomplete.bin", bytes);
    rejects([&] { JournalReader reader(manifest_size); }, "oversized");

    // Opening validates the full stream, and iteration validates again before
    // any callback, even if late corruption is in the final segment.
    const auto late = clone(original, scratch / "late-corruption");
    JournalReader reader(late);
    auto final = read(late / "segment-0000000017.bin"); final.at(final.size() - 1) ^= 1;
    write(late / "segment-0000000017.bin", final);
    std::size_t callbacks = 0;
    rejects([&] { reader.for_each([&](const Event&) { ++callbacks; }); });
    require(callbacks == 0, "late corrupt stream emitted prefix callbacks");
    const auto late_manifest = clone(original, scratch / "late-manifest-corruption");
    JournalReader manifest_reader(late_manifest);
    bytes = read(late_manifest / "manifest-incomplete.bin"); bytes.at(24) ^= 1;
    write(late_manifest / "manifest-incomplete.bin", bytes);
    rejects([&] { manifest_reader.for_each([&](const Event&) { ++callbacks; }); });
    require(callbacks == 0, "late corrupt metadata emitted callbacks");
}

void bounded_stress(const Scratch& scratch, const Fixture& fixture) {
    constexpr std::uint64_t records = 24000;
    const auto directory = scratch / "streaming-stress";
    JournalWriter writer(directory, metadata(fixture), {1024, 128 * 1024});
    // One reusable lifecycle identity and one source exercise a long journal
    // without approaching the explicit unique TX/reception catalog limits.
    for (std::uint64_t index = 0; index < records; ++index)
        writer.append(station(static_cast<std::int64_t>(index), index + 1, index % 2 == 0));
    writer.finish();
    require(writer.stats().records == records && writer.stats().segments > 20, "stress did not stream across segment boundaries");
    require(writer.stats().peak_buffer_bytes <= 128 * 1024 + kMaxJournalRecordBytes, "writer tracked wire buffers grew with history");
    JournalReader reader(directory);
    std::uint64_t seen = 0;
    reader.for_each([&](const Event& event) {
        require(event.event_id.sequence == seen + 1 && event.time.units == static_cast<std::int64_t>(seen), "stress chronology");
        ++seen;
    });
    require(seen == records && reader.stats().logical_digest == writer.stats().logical_digest, "stress digest/count mismatch");
    require(reader.stats().peak_buffer_bytes <= kMaxJournalRecordBytes + 44, "reader accumulated wire history");
    no_temporary(directory);
}
}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 2, "usage: runtime_journal_tests WORK_DIRECTORY");
        const Scratch scratch(fs::u8path(argv[1]));
        const auto fixture = make_synthetic_fixture();
        require(fixture.events.size() == 17, "fixture contract changed; update pinned corruption segment index");
        portable_event_codec(fixture);
        round_trip_and_bounds(scratch, fixture);
        incomplete_and_faults(scratch, fixture);
        unfinished_terminal_preserves_prefix(scratch, fixture);
        integrity_rejections(scratch, fixture);
        bounded_stress(scratch, fixture);
        std::cout << "runtime journal: portable wire/CRC, all typed payloads, exact NMEA, immutable checkpoints, no-clobber faults, "
                  << rejections << " rejections, and 24000-record bounded streaming passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "runtime journal test failed: " << error.what() << '\n';
        return 1;
    }
}
