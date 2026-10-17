#include "journal.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

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
                std::string("wrong journal-v2 rejection: ") + error.what());
        caught = true;
    }
    require(caught, "expected journal-v2 rejection"); ++rejections;
}
class Scratch {
public:
    explicit Scratch(const fs::path& work) : parent_(fs::absolute(work).lexically_normal()) {
        fs::create_directories(parent_);
        const auto nonce = std::chrono::steady_clock::now().time_since_epoch().count();
        for (unsigned attempt = 0; attempt < 100; ++attempt) {
            path_ = parent_ / ("runtime-journal-v2-" + std::to_string(nonce) + "-" + std::to_string(attempt));
            std::error_code error;
            if (fs::create_directory(path_, error)) return;
            require(!error || error == std::errc::file_exists, "cannot create test scratch directory");
        }
        throw std::runtime_error("test scratch names exhausted");
    }
    ~Scratch() {
        if (path_.parent_path() == parent_ && path_.filename().string().rfind("runtime-journal-v2-", 0) == 0) {
            std::error_code ignored; fs::remove_all(path_, ignored);
        }
    }
    fs::path operator/(const fs::path& child) const { return path_ / child; }
private:
    fs::path parent_, path_;
};
void number(Bytes& bytes, std::uint64_t value, unsigned width) {
    for (unsigned index = 0; index < width; ++index) {
        bytes.push_back(static_cast<std::uint8_t>(value & 255u)); value >>= 8;
    }
}
std::uint64_t get(const Bytes& bytes, std::size_t offset, unsigned width) {
    require(offset <= bytes.size() && width <= bytes.size() - offset, "test wire field truncated");
    std::uint64_t value = 0;
    for (unsigned index = 0; index < width; ++index) value |= std::uint64_t{bytes[offset + index]} << (index * 8);
    return value;
}
void put(Bytes& bytes, std::size_t offset, std::uint64_t value, unsigned width) {
    require(offset <= bytes.size() && width <= bytes.size() - offset, "test wire overwrite out of range");
    for (unsigned index = 0; index < width; ++index) {
        bytes[offset + index] = static_cast<std::uint8_t>(value & 255u); value >>= 8;
    }
}
std::uint64_t fnv(const Bytes& bytes, std::uint64_t state = UINT64_C(14695981039346656037)) {
    for (const auto byte : bytes) { state ^= byte; state *= UINT64_C(1099511628211); }
    return state;
}
Bytes read(const fs::path& path) {
    std::ifstream stream(path, std::ios::binary); require(static_cast<bool>(stream), "cannot read test file");
    return Bytes(std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>());
}
void write(const fs::path& path, const Bytes& bytes) {
    std::ofstream stream(path, std::ios::binary | std::ios::trunc); require(static_cast<bool>(stream), "cannot write test file");
    stream.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    stream.flush(); require(static_cast<bool>(stream), "test file write/flush failed");
    stream.close(); require(!stream.fail(), "test file close failed");
}
void repair_crc(Bytes& bytes) {
    require(bytes.size() >= 4, "test checksum trailer missing");
    put(bytes, bytes.size() - 4, crc32c(bytes.data(), bytes.size() - 4), 4);
}
std::string filename(const char* prefix, std::uint64_t index) {
    const auto digits = std::to_string(index);
    return std::string(prefix) + std::string(10 - digits.size(), '0') + digits + ".bin";
}
fs::path clone(const fs::path& source, const fs::path& target) {
    require(fs::create_directory(target), "test clone target exists");
    for (const auto& entry : fs::directory_iterator(source))
        require(fs::copy_file(entry.path(), target / entry.path().filename()), "test clone failed");
    return target;
}
JournalMetadata metadata() {
    return {"SYNTHETIC INFRASTRUCTURE TEST - NOT AIS RF SIMULATION; explicit closure fixture",
            UINT64_C(42), {1792195199, 1}, "streaming-contract-v2", "stored-qualified-b3-v1", {{"R"}},
            ValidationProfile::StreamingV2};
}
Event envelope(EventKind kind, std::string source, std::string subject, std::uint64_t sequence,
               std::int64_t time, EventPayload payload, std::optional<EventId> cause = std::nullopt) {
    Event event;
    event.schema_version = 2; event.time = {time}; event.kind = kind;
    event.source_id = source; event.subject_id = std::move(subject);
    event.event_id = {std::move(source), sequence}; event.causal_ref = std::move(cause);
    event.payload = std::move(payload); return event;
}
std::vector<Event> stream(bool success, unsigned cycles = 1) {
    std::vector<Event> events;
    std::uint64_t p = 0, r = 0;
    events.push_back(envelope(EventKind::StationOn, "p", "S", ++p, 0, StationLifecycle{"S"}));
    events.push_back(envelope(EventKind::ReceiverOn, "r", "R", ++r, 0, ReceiverLifecycle{{"R"}}));
    for (unsigned cycle = 0; cycle < cycles; ++cycle) {
        const auto start = std::int64_t{10} + std::int64_t{30} * cycle;
        const auto end = start + 10, horizon = end + 10;
        const auto tx = make_transmission_id("p", cycle + 1);
        TransmissionStart start_payload{tx, "S", 0, {end}, "qualified.b3.literal", TransmissionCoverage{{{"R"}}, {horizon}}};
        const auto start_id = EventId{"p", p + 1};
        events.push_back(envelope(EventKind::TransmissionStart, "p", "S", ++p, start, start_payload));
        const auto end_id = EventId{"p", p + 1};
        events.push_back(envelope(EventKind::TransmissionEnd, "p", "S", ++p, end, TransmissionEnd{tx}, start_id));
        if (success) {
            const auto rx = make_reception_id("r", cycle + 1);
            const auto reception_id = EventId{"r", r + 1};
            events.push_back(envelope(EventKind::ReceptionSuccess, "r", "R", ++r, end,
                ReceptionResult{rx, tx, {"R"}, {end}, "Supplied infrastructure result"}, end_id));
            events.push_back(envelope(EventKind::ReceptionNoOutput, "r", "R", ++r, end,
                ReceptionNoOutput{{"R"}, rx, tx, "Explicit fixture no-output disposition"}, reception_id));
        } else {
            events.push_back(envelope(EventKind::RfNoReception, "p", "R", ++p, end,
                RfNoReception{tx, {"R"}, NoReceptionReason::NotAudible, "Explicit fixture coverage closure"}, end_id));
        }
        events.push_back(envelope(EventKind::TransmissionResolved, "p", "S", ++p, horizon,
            TransmissionResolved{tx, {horizon}}, end_id));
    }
    return events;
}
void save(const fs::path& directory, const std::vector<Event>& events, std::uint32_t limit) {
    JournalWriter writer(directory, metadata(), {limit, kMaxJournalSegmentBytes});
    for (const auto& event : events) writer.append(event);
    writer.finish();
    require(writer.stats().validator.active_transmissions == 0 && writer.stats().validator.active_receptions == 0 &&
            writer.stats().validator.active_receiver_pairs == 0, "completed v2 writer retained retired objects");
    require(writer.stats().validator.peak_active_transmissions <= 1 && writer.stats().validator.peak_active_receptions <= 1 &&
            writer.stats().validator.peak_active_receiver_pairs <= 1, "v2 fixture active correlation grew with cycles");
}

void wire_round_trips() {
    auto no_reception = envelope(EventKind::RfNoReception, "A", "R", 1, -2,
        RfNoReception{{"tx:A:1"}, {"R"}, NoReceptionReason::NotScheduled, "x"});
    // Independent full schema-2 kind17 vector pins field order, signed time,
    // enum width and exact identity/text lengths.
    const Bytes expected{
        2,0,0,0, 254,255,255,255,255,255,255,255, 17,0,
        1,0,0,0,'A', 1,0,0,0,'R', 1,0,0,0,'A', 1,0,0,0,0,0,0,0, 0,
        6,0,0,0,'t','x',':','A',':','1', 1,0,0,0,'R', 2, 1,0,0,0,'x'};
    require(encode_event(no_reception) == expected, "pinned schema2 no-reception wire bytes");
    const auto decoded_no_reception = decode_event(expected);
    require(std::get<RfNoReception>(decoded_no_reception.payload).reason == NoReceptionReason::NotScheduled,
            "no-reception enum round trip");
    std::vector<Event> samples = stream(false);
    const auto successful = stream(true); samples.insert(samples.end(), successful.begin(), successful.end());
    samples.push_back(no_reception);
    for (const auto& event : samples) {
        const auto bytes = encode_event(event); const auto decoded = decode_event(bytes);
        require(encode_event(decoded) == bytes, "schema2 typed round trip changed canonical bytes");
    }
    auto malformed = expected; malformed[53] = 3;
    rejects([&] { (void)decode_event(malformed); });
    malformed = expected; malformed[0] = 1;
    rejects([&] { (void)decode_event(malformed); });
    malformed = expected; malformed[0] = 3;
    rejects([&] { (void)decode_event(malformed); });
    malformed = expected; malformed.push_back(0);
    rejects([&] { (void)decode_event(malformed); }, "trailing");
    for (std::size_t size = 0; size < expected.size(); ++size)
        rejects([&] { (void)decode_event(Bytes(expected.begin(), expected.begin() + static_cast<std::ptrdiff_t>(size))); });

    auto tx = stream(false).at(2);
    auto legacy = tx; legacy.schema_version = 1; std::get<TransmissionStart>(legacy.payload).coverage.reset();
    const auto legacy_bytes = encode_event(legacy);
    auto& coverage = *std::get<TransmissionStart>(tx.payload).coverage;
    coverage.candidate_receivers.clear();
    for (unsigned index = 0; index < kMaxCoverageReceivers; ++index) {
        auto digits = std::to_string(index); digits = std::string(4 - digits.size(), '0') + digits;
        coverage.candidate_receivers.push_back({"R" + std::string(59, 'a') + digits});
    }
    const auto largest = encode_event(tx);
    require(largest.size() < kMaxJournalRecordBytes &&
            std::get<TransmissionStart>(decode_event(largest).payload).coverage->candidate_receivers.size() == kMaxCoverageReceivers,
            "512 maximum-length receiver identities exceed fixed event bound");
    malformed = largest; put(malformed, legacy_bytes.size(), 513, 4);
    rejects([&] { (void)decode_event(malformed); }, "roster");
    auto absent = tx; std::get<TransmissionStart>(absent.payload).coverage.reset();
    rejects([&] { (void)encode_event(absent); });
    auto forbidden = tx; forbidden.schema_version = 1;
    rejects([&] { (void)encode_event(forbidden); });
}

// Rebuild checksums, digests, checkpoint chain and terminal manifest after a
// test mutation. Thus semantic/profile rejection cannot accidentally pass by
// failing an unrelated CRC first. This helper is confined to small owned tests.
void rewrite_records(const fs::path& directory, const std::function<void(std::size_t, Bytes&)>& mutate) {
    auto terminal = read(directory / "manifest-complete.bin");
    const auto segments = get(terminal, 25, 8);
    auto chain = fnv(read(directory / "manifest-incomplete.bin"));
    auto rolling = UINT64_C(14695981039346656037);
    std::uint64_t total = 0;
    std::size_t ordinal = 0;
    for (std::uint64_t index = 1; index <= segments; ++index) {
        const auto path = directory / filename("segment-", index);
        const auto original = read(path);
        Bytes segment(original.begin(), original.begin() + 36);
        std::size_t offset = 36;
        const auto count = get(original, 20, 4);
        std::uint64_t first_time = 0, last_time = 0;
        for (std::uint64_t record_index = 0; record_index < count; ++record_index) {
            const auto size = get(original, offset, 4); offset += 4;
            require(size <= original.size() - offset, "test record framing malformed");
            Bytes event(original.begin() + static_cast<std::ptrdiff_t>(offset),
                        original.begin() + static_cast<std::ptrdiff_t>(offset + size));
            offset += static_cast<std::size_t>(size) + 4;
            mutate(ordinal++, event);
            require(!event.empty() && event.size() <= kMaxJournalRecordBytes, "test mutation record bound");
            Bytes length; number(length, event.size(), 4);
            rolling = fnv(length, rolling); rolling = fnv(event, rolling);
            segment.insert(segment.end(), length.begin(), length.end());
            segment.insert(segment.end(), event.begin(), event.end());
            number(segment, crc32c(event.data(), event.size()), 4);
            if (record_index == 0) first_time = get(event, 4, 8);
            last_time = get(event, 4, 8); ++total;
        }
        put(segment, 24, segment.size() - 36, 4);
        const auto segment_crc = crc32c(segment.data(), segment.size()); number(segment, segment_crc, 4);
        write(path, segment);
        auto checkpoint = read(directory / filename("checkpoint-", index));
        put(checkpoint, 36, segment.size(), 8); put(checkpoint, 44, segment_crc, 4);
        put(checkpoint, 48, total, 8); put(checkpoint, 56, rolling, 8); put(checkpoint, 64, chain, 8);
        put(checkpoint, 72, first_time, 8); put(checkpoint, 80, last_time, 8);
        repair_crc(checkpoint); write(directory / filename("checkpoint-", index), checkpoint); chain = fnv(checkpoint);
    }
    put(terminal, 33, total, 8); put(terminal, 41, rolling, 8); put(terminal, 49, chain, 8);
    repair_crc(terminal); write(directory / "manifest-complete.bin", terminal);
}

void journal_profiles_and_corruption(const Scratch& scratch) {
    const auto events = stream(false, 2);
    const auto tiny = scratch / "v2-one-record", large = scratch / "v2-large";
    save(tiny, events, 1); save(large, events, 4096);
    JournalReader a(tiny), b(large);
    require(a.metadata().validation_profile == ValidationProfile::StreamingV2 &&
            a.stats().logical_digest == b.stats().logical_digest && a.stats().records == events.size(),
            "v2 profile/digest/segmentation mismatch");
    for (const auto& directory : {tiny, large}) {
        std::size_t index = 0;
        JournalReader reader(directory);
        reader.for_each([&](const Event& event) {
            require(index < events.size() && encode_event(event) == encode_event(events[index++]), "v2 streamed canonical bytes");
        });
        require(index == events.size() && reader.stats().validator.active_transmissions == 0 &&
                reader.stats().validator.active_receiver_pairs == 0, "v2 replay failed to retire completed obligations");
        for (const auto& entry : fs::directory_iterator(directory)) {
            const auto bytes = read(entry.path());
            require(get(bytes, 8, 2) == 2, "v2 run mixed physical file versions");
        }
    }
    const auto successful = scratch / "v2-no-output";
    save(successful, stream(true, 2), 1);
    require(JournalReader(successful).stats().validator.active_receptions == 0, "explicit no-output failed replay retirement");

    const auto initial = read(large / "manifest-incomplete.bin");
    require(get(initial, 24, 4) == 2 && initial.at(28) == 0, "v2 initial explicit profile prefix");
    const auto future = clone(large, scratch / "future-file-version");
    auto modified = read(future / "manifest-incomplete.bin"); put(modified, 8, 3, 2); repair_crc(modified);
    write(future / "manifest-incomplete.bin", modified);
    rejects([&] { JournalReader reader(future); }, "version");
    const auto unsupported = clone(large, scratch / "unsupported-profile");
    modified = initial; put(modified, 24, 99, 4); repair_crc(modified); write(unsupported / "manifest-incomplete.bin", modified);
    rejects([&] { JournalReader reader(unsupported); }, "profile");
    for (const auto* target : {"segment-0000000001.bin", "checkpoint-0000000001.bin", "manifest-complete.bin"}) {
        const auto mixed = clone(large, scratch / (std::string("mixed-") + target));
        modified = read(mixed / target); put(modified, 8, 1, 2); repair_crc(modified); write(mixed / target, modified);
        rejects([&] { JournalReader reader(mixed); });
    }
    const auto event_mix = clone(tiny, scratch / "mixed-event-schema");
    rewrite_records(event_mix, [](std::size_t index, Bytes& bytes) { if (index == 0) put(bytes, 0, 1, 4); });
    rejects([&] { JournalReader reader(event_mix); }, "profile");
    const auto future_schema = clone(tiny, scratch / "future-event-schema");
    rewrite_records(future_schema, [](std::size_t index, Bytes& bytes) { if (index == 0) put(bytes, 0, 3, 4); });
    rejects([&] { JournalReader reader(future_schema); }, "schema");
    const auto reorder = clone(tiny, scratch / "valid-crc-reorder");
    rewrite_records(reorder, [&](std::size_t index, Bytes& bytes) {
        if (index == 0) bytes = encode_event(events.at(1));
        else if (index == 1) bytes = encode_event(events.at(0));
    });
    rejects([&] { JournalReader reader(reorder); }, "canonical");
    const auto reused = clone(tiny, scratch / "retired-tx-id-reused");
    rewrite_records(reused, [](std::size_t, Bytes& bytes) {
        auto event = decode_event(bytes); const auto first = make_transmission_id("p", 1), second = make_transmission_id("p", 2);
        if (auto* start = std::get_if<TransmissionStart>(&event.payload)) { if (start->transmission_id == second) start->transmission_id = first; }
        if (auto* end = std::get_if<TransmissionEnd>(&event.payload)) { if (end->transmission_id == second) end->transmission_id = first; }
        if (auto* outcome = std::get_if<RfNoReception>(&event.payload)) { if (outcome->transmission_id == second) outcome->transmission_id = first; }
        if (auto* closed = std::get_if<TransmissionResolved>(&event.payload)) { if (closed->transmission_id == second) closed->transmission_id = first; }
        bytes = encode_event(event);
    });
    rejects([&] { JournalReader reader(reused); });
    const auto reused_rx = clone(successful, scratch / "retired-rx-id-reused");
    rewrite_records(reused_rx, [](std::size_t, Bytes& bytes) {
        auto event = decode_event(bytes); const auto first = make_reception_id("r", 1), second = make_reception_id("r", 2);
        if (auto* result = std::get_if<ReceptionResult>(&event.payload)) { if (result->reception_id == second) result->reception_id = first; }
        if (auto* omitted = std::get_if<ReceptionNoOutput>(&event.payload)) { if (omitted->reception_id == second) omitted->reception_id = first; }
        bytes = encode_event(event);
    });
    rejects([&] { JournalReader reader(reused_rx); });
    // Exercise the writer independently at the exact creation boundary, after
    // the first identity retired and across one-record segment checkpoints.
    JournalWriter duplicate_tx_writer(scratch / "writer-retired-tx-id", metadata(), {1, kMaxJournalSegmentBytes});
    for (std::size_t index = 0; index < 6; ++index) duplicate_tx_writer.append(events.at(index));
    require(duplicate_tx_writer.stats().segments == 6 &&
            duplicate_tx_writer.stats().validator.active_transmissions == 0, "writer TX reuse precondition");
    auto duplicate_start = events.at(6);
    std::get<TransmissionStart>(duplicate_start.payload).transmission_id = make_transmission_id("p", 1);
    rejects([&] { duplicate_tx_writer.append(duplicate_start); });
    require(!fs::exists((scratch / "writer-retired-tx-id") / "manifest-complete.bin"), "reused TX marked COMPLETE");
    const auto success_events = stream(true, 2);
    JournalWriter duplicate_rx_writer(scratch / "writer-retired-rx-id", metadata(), {1, kMaxJournalSegmentBytes});
    for (std::size_t index = 0; index < 9; ++index) duplicate_rx_writer.append(success_events.at(index));
    require(duplicate_rx_writer.stats().segments == 9 &&
            duplicate_rx_writer.stats().validator.active_receptions == 0, "writer RX reuse precondition");
    auto duplicate_reception = success_events.at(9);
    std::get<ReceptionResult>(duplicate_reception.payload).reception_id = make_reception_id("r", 1);
    rejects([&] { duplicate_rx_writer.append(duplicate_reception); });
    require(!fs::exists((scratch / "writer-retired-rx-id") / "manifest-complete.bin"), "reused RX marked COMPLETE");
    const auto bad_catalog = scratch / "coverage-outside-catalog";
    auto no_receivers = metadata(); no_receivers.receivers.clear();
    JournalWriter writer(bad_catalog, no_receivers);
    writer.append(events.at(0));
    rejects([&] { writer.append(events.at(2)); }, "catalog");
    rejects([&] { writer.finish(); }, "failed");

    auto invalid_profile = metadata(); invalid_profile.validation_profile = static_cast<ValidationProfile>(99);
    rejects([&] { JournalWriter invalid(scratch / "invalid-profile", invalid_profile); });
    auto v1 = metadata(); v1.validation_profile = ValidationProfile::LegacyV1;
    JournalWriter v1_writer(scratch / "v1-reject-v2", v1);
    rejects([&] { v1_writer.append(events.front()); }, "profile");
    JournalWriter v2_writer(scratch / "v2-reject-v1", metadata());
    auto old = events.front(); old.schema_version = 1;
    rejects([&] { v2_writer.append(old); }, "profile");
}

void incomplete_v2_prefix(const Scratch& scratch) {
    const auto events = stream(false);
    for (const auto limit : {std::uint32_t{1}, std::uint32_t{4096}}) {
        const auto directory = scratch / ("unfinished-v2-" + std::to_string(limit));
        JournalWriter writer(directory, metadata(), {limit, kMaxJournalSegmentBytes});
        for (std::size_t index = 0; index < 3; ++index) writer.append(events.at(index));
        rejects([&] { writer.finish(); });
        require(!fs::exists(directory / "manifest-complete.bin"), "open v2 coverage manufactured COMPLETE");
        JournalReader inspected(directory, true);
        require(inspected.stats().records == 3 && inspected.stats().validator.active_transmissions == 1,
                "v2 terminal failure lost valid buffered TX prefix");
        rejects([&] { JournalReader complete(directory); }, "INCOMPLETE");
        std::size_t index = 0;
        inspected.for_each([&](const Event& event) { require(encode_event(event) == encode_event(events.at(index++)), "v2 prefix wire bytes"); });
        require(index == 3, "v2 incomplete prefix callbacks");
    }
}
}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 2, "usage: runtime_journal_v2_tests WORK_DIRECTORY");
        const Scratch scratch(fs::u8path(argv[1]));
        wire_round_trips(); journal_profiles_and_corruption(scratch); incomplete_v2_prefix(scratch);
        std::cout << "SYNTHETIC INFRASTRUCTURE TEST - NOT AIS RF SIMULATION: journal v2 codec/profile, valid-CRC semantic corruption, "
                  << "retired ID reuse and incomplete prefixes passed; " << rejections << " rejections\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "runtime journal v2 test failed: " << error.what() << '\n'; return 1;
    }
}
