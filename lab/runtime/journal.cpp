#include "journal.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <fstream>
#include <limits>
#include <set>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <utility>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <cerrno>
#include <fcntl.h>
#include <unistd.h>
#endif

namespace aismixer::lab::runtime {
namespace {
using Bytes = std::vector<std::uint8_t>;
constexpr std::size_t kMetadataLimit = 256 * 1024;
constexpr std::uint64_t kSegmentLimit = 1000000;
constexpr std::size_t kSegmentHeader = 36;
constexpr std::size_t kSegmentOverhead = kSegmentHeader + 4;
constexpr std::uint64_t kFnvOffset = UINT64_C(14695981039346656037);

[[noreturn]] void invalid(std::string_view reason) {
    throw std::invalid_argument("Journal: " + std::string(reason));
}
std::uint16_t file_version(ValidationProfile profile) {
    switch (profile) {
        case ValidationProfile::LegacyV1: return 1;
        case ValidationProfile::StreamingV2: return 2;
    }
    invalid("unsupported validation profile");
}
class Crc {
public:
    void update(const std::uint8_t* bytes, std::size_t count) {
        for (std::size_t index = 0; index < count; ++index) {
            state_ ^= bytes[index];
            for (unsigned bit = 0; bit < 8; ++bit) state_ = (state_ >> 1) ^ ((state_ & 1u) ? 0x82f63b78u : 0u);
        }
    }
    std::uint32_t value() const { return state_ ^ 0xffffffffu; }
private:
    std::uint32_t state_ = 0xffffffffu;
};
void hash_bytes(std::uint64_t& hash, const std::uint8_t* bytes, std::size_t count) {
    for (std::size_t index = 0; index < count; ++index) {
        hash ^= bytes[index];
        hash *= UINT64_C(1099511628211);
    }
}
std::uint64_t fingerprint(const Bytes& bytes) {
    auto hash = kFnvOffset;
    hash_bytes(hash, bytes.data(), bytes.size());
    return hash;
}
void number(Bytes& bytes, std::uint64_t value, unsigned width) {
    for (unsigned index = 0; index < width; ++index) {
        bytes.push_back(static_cast<std::uint8_t>(value & 255u));
        value >>= 8;
    }
}
void signed_number(Bytes& bytes, std::int64_t value) { number(bytes, static_cast<std::uint64_t>(value), 8); }
void text(Bytes& bytes, const std::string& value) {
    if (value.size() > kMaxEventTextBytes) invalid("text exceeds4096bytes");
    number(bytes, value.size(), 4);
    bytes.insert(bytes.end(), value.begin(), value.end());
}
void id(Bytes& bytes, const EventId& value) { text(bytes, value.source_id); number(bytes, value.sequence, 8); }

class Cursor {
public:
    explicit Cursor(const Bytes& bytes) : bytes_(bytes) {}
    std::uint64_t number(unsigned width) {
        if (width > bytes_.size() - position_) invalid("truncated fixed-width field");
        std::uint64_t value = 0;
        for (unsigned index = 0; index < width; ++index) value |= std::uint64_t{bytes_[position_++]} << (index * 8);
        return value;
    }
    std::int64_t signed_number() {
        const auto bits = number(8);
        if (bits <= static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) return static_cast<std::int64_t>(bits);
        return -1 - static_cast<std::int64_t>(~bits);
    }
    bool boolean() {
        const auto value = number(1);
        if (value > 1) invalid("boolean must be0or1");
        return value != 0;
    }
    std::string text(std::size_t maximum = kMaxEventTextBytes) {
        const auto size = number(4);
        if (size > maximum || size > bytes_.size() - position_) invalid("oversized/truncated string");
        const auto begin = bytes_.begin() + static_cast<std::ptrdiff_t>(position_);
        std::string result(begin, begin + static_cast<std::ptrdiff_t>(size));
        position_ += static_cast<std::size_t>(size);
        return result;
    }
    EventId id() { return {text(kMaxIdentityBytes), number(8)}; }
    void end() const { if (position_ != bytes_.size()) invalid("trailing unknown record fields"); }
private:
    const Bytes& bytes_;
    std::size_t position_ = 0;
};

void utf8_text(const std::string& value, std::size_t limit, const char* field) {
    if (value.empty() || value.size() > limit) invalid(std::string(field) + " has an invalid length");
    std::size_t index = 0;
    while (index < value.size()) {
        const auto first = static_cast<unsigned char>(value[index]);
        const std::size_t width = first < 0x80 ? 1 : first >= 0xc2 && first <= 0xdf ? 2 :
            first >= 0xe0 && first <= 0xef ? 3 : first >= 0xf0 && first <= 0xf4 ? 4 : 0;
        bool valid = width != 0 && width <= value.size() - index && first >= 32 && first != 127;
        for (std::size_t offset = 1; valid && offset < width; ++offset) {
            const auto next = static_cast<unsigned char>(value[index + offset]);
            valid = next >= 0x80 && next <= 0xbf;
        }
        if (valid && width >= 3) {
            const auto second = static_cast<unsigned char>(value[index + 1]);
            valid = !(first == 0xe0 && second < 0xa0) && !(first == 0xed && second > 0x9f) &&
                    !(first == 0xf0 && second < 0x90) && !(first == 0xf4 && second > 0x8f);
        }
        if (!valid) invalid(std::string(field) + " must be bounded UTF-8 without controls");
        index += width;
    }
}
void validate_metadata(JournalMetadata& metadata) {
    (void)file_version(metadata.validation_profile);
    utf8_text(metadata.provenance, 4096, "provenance");
    utf8_text(metadata.model_version, 128, "model_version");
    utf8_text(metadata.codec_version, 128, "codec_version");
    validate_utc_anchor(metadata.utc_anchor);
    if (metadata.receivers.size() > kMaxEventSources) invalid("receiver catalog exceeds1024entries");
    std::sort(metadata.receivers.begin(), metadata.receivers.end(), [](const ReceiverId& a, const ReceiverId& b) { return a.value < b.value; });
    for (std::size_t index = 0; index < metadata.receivers.size(); ++index) {
        validate_identity(metadata.receivers[index].value, "manifest receiver");
        if (index != 0 && metadata.receivers[index - 1] == metadata.receivers[index]) invalid("duplicate manifest receiver");
    }
}
void validate_catalog_event(const JournalMetadata& metadata, const Event& event) {
    const auto known = [&](const ReceiverId& receiver) {
        if (!std::binary_search(metadata.receivers.begin(), metadata.receivers.end(), receiver,
            [](const ReceiverId& a, const ReceiverId& b) { return a.value < b.value; }))
            invalid("event receiver absent from manifest catalog");
    };
    if (const auto receiver = event_receiver(event)) known(*receiver);
    if (event.kind == EventKind::TransmissionStart) {
        const auto& start = std::get<TransmissionStart>(event.payload);
        if (start.coverage) for (const auto& receiver : start.coverage->candidate_receivers) known(receiver);
    }
}
Bytes metadata_bytes(const JournalMetadata& metadata, const JournalOptions& options) {
    Bytes bytes;
    if (metadata.validation_profile == ValidationProfile::StreamingV2)
        number(bytes, static_cast<std::uint32_t>(metadata.validation_profile), 4);
    number(bytes, 0, 1);  // INCOMPLETE.
    text(bytes, metadata.provenance);
    number(bytes, metadata.effective_seed ? 1 : 0, 1);
    if (metadata.effective_seed) number(bytes, *metadata.effective_seed, 8);
    signed_number(bytes, metadata.utc_anchor.posix_seconds);
    signed_number(bytes, metadata.utc_anchor.subsecond_units);
    text(bytes, metadata.model_version);
    text(bytes, metadata.codec_version);
    number(bytes, metadata.receivers.size(), 4);
    for (const auto& receiver : metadata.receivers) text(bytes, receiver.value);
    number(bytes, options.max_records_per_segment, 4);
    number(bytes, options.max_segment_bytes, 4);
    return bytes;
}
void validate_options(const JournalOptions& options) {
    if (options.max_records_per_segment == 0 || options.max_records_per_segment > kMaxJournalSegmentRecords) invalid("segment record limit outside1..65536");
    if (options.max_segment_bytes < kSegmentOverhead + 8 + 1 || options.max_segment_bytes > kMaxJournalSegmentBytes) invalid("segment byte limit outside49..8388608");
}
std::string numbered(const char* prefix, std::uint64_t index) {
    const auto digits = std::to_string(index);
    if (digits.size() > 10) invalid("segment number overflow");
    return std::string(prefix) + std::string(10 - digits.size(), '0') + digits + ".bin";
}
Bytes envelope(std::uint16_t kind, const Bytes& payload, std::uint16_t version) {
    if (payload.size() > kMetadataLimit) invalid("manifest/checkpoint exceeds bounded metadata size");
    Bytes bytes{'A','I','S','J','R','0','F','1'};
    number(bytes, version, 2);
    number(bytes, kind, 2);
    number(bytes, static_cast<std::uint64_t>(kUnitsPerSecond), 8);
    number(bytes, payload.size(), 4);
    bytes.insert(bytes.end(), payload.begin(), payload.end());
    number(bytes, crc32c(bytes.data(), bytes.size()), 4);
    return bytes;
}
void read_exact(std::ifstream& input, std::uint8_t* bytes, std::size_t count) {
    input.read(reinterpret_cast<char*>(bytes), static_cast<std::streamsize>(count));
    if (static_cast<std::size_t>(input.gcount()) != count || input.bad()) invalid("truncated/unreadable committed file");
}
void require_end(std::ifstream& input) {
    char extra = 0;
    if (input.get(extra) || !input.eof()) invalid("trailing bytes or committed file read error");
}
Bytes read_envelope(const std::filesystem::path& path, std::uint16_t expected_kind,
                    std::uint64_t* hash = nullptr, std::uint16_t required_version = 0,
                    std::uint16_t* detected_version = nullptr) {
    std::ifstream input(path, std::ios::binary);
    if (!input) invalid("missing committed manifest/checkpoint: " + path.u8string());
    Bytes header(24);
    read_exact(input, header.data(), header.size());
    if (std::string(header.begin(), header.begin() + 8) != "AISJR0F1") invalid("unknown file magic");
    Bytes fields(header.begin() + 8, header.end());
    Cursor cursor(fields);
    const auto version = cursor.number(2);
    if ((version != 1 && version != 2) || (required_version != 0 && version != required_version) ||
        cursor.number(2) != expected_kind ||
        cursor.number(8) != static_cast<std::uint64_t>(kUnitsPerSecond)) invalid("unsupported file/schema/timebase version");
    if (detected_version) *detected_version = static_cast<std::uint16_t>(version);
    const auto size = cursor.number(4);
    if (size > kMetadataLimit) invalid("oversized manifest/checkpoint");
    Bytes payload(static_cast<std::size_t>(size));
    read_exact(input, payload.data(), payload.size());
    Bytes suffix(4);
    read_exact(input, suffix.data(), suffix.size());
    require_end(input);
    Crc crc;
    crc.update(header.data(), header.size());
    crc.update(payload.data(), payload.size());
    Cursor tail(suffix);
    if (crc.value() != tail.number(4)) invalid("manifest/checkpoint CRC32C mismatch");
    if (hash) {
        *hash = kFnvOffset;
        hash_bytes(*hash, header.data(), header.size());
        hash_bytes(*hash, payload.data(), payload.size());
        hash_bytes(*hash, suffix.data(), suffix.size());
    }
    return payload;
}

enum class Fault { None, Write, Flush, Close, Publish, BeforeComplete };
struct Hooks {
    Fault fault = Fault::None;
    std::uint64_t publication_number = 1;
    std::function<void(const std::filesystem::path&)> before_publish;
};
struct View { const std::uint8_t* bytes; std::size_t count; };
void publish(const std::filesystem::path& target, const std::vector<View>& parts,
             const Hooks& hooks, std::uint64_t publication) {
    std::filesystem::path temporary;
    bool temporary_owned = false;
#ifdef _WIN32
    HANDLE handle = INVALID_HANDLE_VALUE;
#else
    int handle = -1;
#endif
    const auto cleanup = [&] {
#ifdef _WIN32
        if (handle != INVALID_HANDLE_VALUE) { CloseHandle(handle); handle = INVALID_HANDLE_VALUE; }
#else
        if (handle >= 0) { ::close(handle); handle = -1; }
#endif
        if (temporary_owned && !temporary.empty()) { std::error_code ignored; std::filesystem::remove(temporary, ignored); }
    };
    try {
        for (unsigned attempt = 0; attempt < 32; ++attempt) {
#ifdef _WIN32
            temporary = target.parent_path() / (target.filename().native() + L".aismixer-tmp-" + std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(attempt));
            handle = CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
            if (handle != INVALID_HANDLE_VALUE) { temporary_owned = true; break; }
            if (GetLastError() != ERROR_FILE_EXISTS && GetLastError() != ERROR_ALREADY_EXISTS) throw std::runtime_error("Journal temporary creation failed");
#else
            temporary = target.parent_path() / (target.filename().string() + ".aismixer-tmp-" + std::to_string(getpid()) + "-" + std::to_string(attempt));
            handle = ::open(temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
            if (handle >= 0) { temporary_owned = true; break; }
            if (errno != EEXIST) throw std::runtime_error("Journal temporary creation failed");
#endif
            temporary.clear();  // Never remove another writer's occupied name.
        }
#ifdef _WIN32
        if (handle == INVALID_HANDLE_VALUE) throw std::runtime_error("Journal exclusive temporary names exhausted");
#else
        if (handle < 0) throw std::runtime_error("Journal exclusive temporary names exhausted");
#endif
        const bool inject = publication == hooks.publication_number;
        std::size_t written_total = 0;
        for (const auto part : parts) {
            std::size_t offset = 0;
            while (offset < part.count) {
                if (inject && hooks.fault == Fault::Write && written_total >= 13) throw std::runtime_error("Injected journal partial write failure");
                std::size_t count = part.count - offset;
                if (inject && hooks.fault == Fault::Write) count = std::min(count, std::size_t{13} - written_total);
#ifdef _WIN32
                DWORD done = 0;
                if (!WriteFile(handle, part.bytes + offset, static_cast<DWORD>(count), &done, nullptr) || done == 0) throw std::runtime_error("Journal temporary write failed");
#else
                const auto done = ::write(handle, part.bytes + offset, count);
                if (done < 0 && errno == EINTR) continue;
                if (done <= 0) throw std::runtime_error("Journal temporary write failed");
#endif
                offset += static_cast<std::size_t>(done);
                written_total += static_cast<std::size_t>(done);
            }
        }
        if (inject && hooks.fault == Fault::Flush) throw std::runtime_error("Injected journal flush failure");
#ifdef _WIN32
        if (!FlushFileBuffers(handle)) throw std::runtime_error("Journal temporary flush failed");
        if (!CloseHandle(handle)) throw std::runtime_error("Journal temporary close failed");
        handle = INVALID_HANDLE_VALUE;
#else
        if (::fsync(handle) != 0) throw std::runtime_error("Journal temporary flush failed");
        const auto closing = handle; handle = -1;
        if (::close(closing) != 0) throw std::runtime_error("Journal temporary close failed");
#endif
        if (inject && hooks.fault == Fault::Close) throw std::runtime_error("Injected journal close failure");
        if (inject && hooks.before_publish) hooks.before_publish(target);
        if (inject && hooks.fault == Fault::Publish) throw std::runtime_error("Injected journal publication failure");
#ifdef _WIN32
        if (!MoveFileExW(temporary.c_str(), target.c_str(), MOVEFILE_WRITE_THROUGH)) throw std::runtime_error("Journal no-clobber publication failed: " + target.u8string());
        temporary.clear();
#else
        if (::link(temporary.c_str(), target.c_str()) != 0) throw std::runtime_error("Journal no-clobber publication failed: " + target.u8string());
#endif
        cleanup();
    } catch (...) { cleanup(); throw; }
}
void publish(const std::filesystem::path& target, const Bytes& bytes, const Hooks& hooks, std::uint64_t publication) {
    publish(target, std::vector<View>{{bytes.data(), bytes.size()}}, hooks, publication);
}

struct Checkpoint {
    std::uint64_t index = 0;
    std::uint32_t records = 0;
    std::uint64_t file_bytes = 0;
    std::uint32_t segment_crc = 0;
    std::uint64_t total_records = 0;
    std::uint64_t logical_digest = kFnvOffset;
    std::uint64_t prior_chain = 0;
    std::int64_t first_time = 0;
    std::int64_t last_time = 0;
};
Bytes checkpoint_bytes(const Checkpoint& checkpoint) {
    Bytes bytes;
    number(bytes, checkpoint.index, 8); number(bytes, checkpoint.records, 4);
    number(bytes, checkpoint.file_bytes, 8); number(bytes, checkpoint.segment_crc, 4);
    number(bytes, checkpoint.total_records, 8); number(bytes, checkpoint.logical_digest, 8);
    number(bytes, checkpoint.prior_chain, 8); signed_number(bytes, checkpoint.first_time); signed_number(bytes, checkpoint.last_time);
    return bytes;
}
Checkpoint checkpoint_from(const Bytes& bytes) {
    Cursor cursor(bytes);
    Checkpoint result;
    result.index = cursor.number(8); result.records = static_cast<std::uint32_t>(cursor.number(4));
    result.file_bytes = cursor.number(8); result.segment_crc = static_cast<std::uint32_t>(cursor.number(4));
    result.total_records = cursor.number(8); result.logical_digest = cursor.number(8);
    result.prior_chain = cursor.number(8); result.first_time = cursor.signed_number(); result.last_time = cursor.signed_number();
    cursor.end();
    return result;
}
std::uint64_t checkpoint_count(const std::filesystem::path& directory) {
    std::uint64_t largest = 0, count = 0;
    for (const auto& entry : std::filesystem::directory_iterator(directory)) {
        const auto filename = entry.path().filename().u8string();
        if (filename.rfind("checkpoint-", 0) != 0 || filename.size() != 25 || filename.substr(21) != ".bin") continue;
        const auto digits = filename.substr(11, 10);
        std::uint64_t index = 0;
        const auto parsed = std::from_chars(digits.data(), digits.data() + digits.size(), index);
        if (parsed.ec != std::errc{} || parsed.ptr != digits.data() + digits.size() || index == 0 || index > kSegmentLimit) invalid("invalid checkpoint index");
        ++count; largest = std::max(largest, index);
        if (count > kSegmentLimit) invalid("checkpoint resource limit exceeded");
    }
    if (count != largest) invalid("missing checkpoint in committed prefix");
    return count;
}

}  // namespace

std::uint32_t crc32c(const std::uint8_t* bytes, std::size_t count) {
    if (!bytes && count != 0) invalid("null CRC input");
    Crc crc; crc.update(bytes, count); return crc.value();
}

std::vector<std::uint8_t> encode_event(const Event& event) {
    validate_event(event);
    Bytes bytes;
    number(bytes, event.schema_version, 4); signed_number(bytes, event.time.units);
    number(bytes, static_cast<std::uint16_t>(event.kind), 2);
    text(bytes, event.source_id); text(bytes, event.subject_id); id(bytes, event.event_id);
    number(bytes, event.causal_ref ? 1 : 0, 1); if (event.causal_ref) id(bytes, *event.causal_ref);
    switch (event.kind) {
        case EventKind::StationOn: case EventKind::StationOff: text(bytes, std::get<StationLifecycle>(event.payload).station_id); break;
        case EventKind::ReceiverOn: case EventKind::ReceiverOff: text(bytes, std::get<ReceiverLifecycle>(event.payload).receiver_id.value); break;
        case EventKind::SlotMapInit: case EventKind::SlotMapReset: case EventKind::SlotReserve: case EventKind::SlotRelease: {
            const auto& p = std::get<SlotMapChange>(event.payload); text(bytes, p.station_id); number(bytes, p.channel, 1); number(bytes, p.slot, 2); text(bytes, p.reason); break;
        }
        case EventKind::TdmaDecision: { const auto& p = std::get<TdmaDecision>(event.payload); text(bytes, p.station_id); text(bytes, p.decision); break; }
        case EventKind::TransmissionStart: {
            const auto& p = std::get<TransmissionStart>(event.payload); text(bytes, p.transmission_id.value); text(bytes, p.station_id); number(bytes, p.channel, 1); signed_number(bytes, p.end_time.units); text(bytes, p.payload_reference);
            if (event.schema_version == 2) {
                const auto& coverage = *p.coverage;
                number(bytes, coverage.candidate_receivers.size(), 4);
                for (const auto& receiver : coverage.candidate_receivers) text(bytes, receiver.value);
                signed_number(bytes, coverage.resolution_horizon.units);
            }
            break;
        }
        case EventKind::TransmissionEnd: text(bytes, std::get<TransmissionEnd>(event.payload).transmission_id.value); break;
        case EventKind::RfObservation: {
            const auto& p = std::get<RfObservation>(event.payload); text(bytes, p.transmission_id.value); text(bytes, p.receiver_id.value); signed_number(bytes, p.availability_time.units); number(bytes, p.carrier_detected ? 1 : 0, 1); number(bytes, p.local_station_id ? 1 : 0, 1); if (p.local_station_id) text(bytes, *p.local_station_id); text(bytes, p.detail); break;
        }
        case EventKind::ReceptionSuccess: case EventKind::ReceptionFailure: {
            const auto& p = std::get<ReceptionResult>(event.payload); text(bytes, p.reception_id.value); text(bytes, p.transmission_id.value); text(bytes, p.receiver_id.value); signed_number(bytes, p.availability_time.units); text(bytes, p.detail); break;
        }
        case EventKind::ReceiverNmea: {
            const auto& p = std::get<ReceiverNmea>(event.payload); text(bytes, p.receiver_id.value); text(bytes, p.reception_id.value); text(bytes, p.transmission_id.value); number(bytes, p.sentences.size(), 4); for (const auto& line : p.sentences) text(bytes, line); break;
        }
        case EventKind::OutputError: { const auto& p = std::get<OutputError>(event.payload); text(bytes, p.receiver_id.value); text(bytes, p.code); text(bytes, p.detail); break; }
        case EventKind::RfNoReception: {
            const auto& p = std::get<RfNoReception>(event.payload); text(bytes, p.transmission_id.value);
            text(bytes, p.receiver_id.value); number(bytes, static_cast<std::uint8_t>(p.reason), 1); text(bytes, p.detail); break;
        }
        case EventKind::ReceptionNoOutput: {
            const auto& p = std::get<ReceptionNoOutput>(event.payload); text(bytes, p.receiver_id.value);
            text(bytes, p.reception_id.value); text(bytes, p.transmission_id.value); text(bytes, p.reason); break;
        }
        case EventKind::TransmissionResolved: {
            const auto& p = std::get<TransmissionResolved>(event.payload); text(bytes, p.transmission_id.value);
            signed_number(bytes, p.resolution_horizon.units); break;
        }
        default: invalid("unsupported event kind");
    }
    if (bytes.size() > kMaxJournalRecordBytes) invalid("event exceeds65536-byte record bound");
    return bytes;
}

Event decode_event(const std::vector<std::uint8_t>& bytes) {
    if (bytes.empty() || bytes.size() > kMaxJournalRecordBytes) invalid("invalid event record length");
    Cursor cursor(bytes);
    Event event;
    event.schema_version = static_cast<std::uint32_t>(cursor.number(4));
    event.time = {cursor.signed_number()}; event.kind = static_cast<EventKind>(cursor.number(2));
    event.source_id = cursor.text(kMaxIdentityBytes); event.subject_id = cursor.text(kMaxIdentityBytes); event.event_id = cursor.id();
    if (cursor.boolean()) event.causal_ref = cursor.id();
    switch (event.kind) {
        case EventKind::StationOn: case EventKind::StationOff: event.payload = StationLifecycle{cursor.text(kMaxIdentityBytes)}; break;
        case EventKind::ReceiverOn: case EventKind::ReceiverOff: event.payload = ReceiverLifecycle{{cursor.text(kMaxIdentityBytes)}}; break;
        case EventKind::SlotMapInit: case EventKind::SlotMapReset: case EventKind::SlotReserve: case EventKind::SlotRelease: {
            SlotMapChange p; p.station_id = cursor.text(kMaxIdentityBytes); p.channel = static_cast<std::uint8_t>(cursor.number(1)); p.slot = static_cast<std::uint16_t>(cursor.number(2)); p.reason = cursor.text(); event.payload = std::move(p); break;
        }
        case EventKind::TdmaDecision: { TdmaDecision p; p.station_id = cursor.text(kMaxIdentityBytes); p.decision = cursor.text(); event.payload = std::move(p); break; }
        case EventKind::TransmissionStart: {
            TransmissionStart p; p.transmission_id = {cursor.text(kMaxIdentityBytes)}; p.station_id = cursor.text(kMaxIdentityBytes); p.channel = static_cast<std::uint8_t>(cursor.number(1)); p.end_time = {cursor.signed_number()}; p.payload_reference = cursor.text();
            if (event.schema_version == 2) {
                TransmissionCoverage coverage;
                const auto count = cursor.number(4);
                if (count > kMaxCoverageReceivers) invalid("coverage roster exceeds512receivers");
                for (std::uint64_t index = 0; index < count; ++index)
                    coverage.candidate_receivers.push_back({cursor.text(kMaxIdentityBytes)});
                coverage.resolution_horizon = {cursor.signed_number()}; p.coverage = std::move(coverage);
            }
            event.payload = std::move(p); break;
        }
        case EventKind::TransmissionEnd: event.payload = TransmissionEnd{{cursor.text(kMaxIdentityBytes)}}; break;
        case EventKind::RfObservation: {
            RfObservation p; p.transmission_id = {cursor.text(kMaxIdentityBytes)}; p.receiver_id = {cursor.text(kMaxIdentityBytes)}; p.availability_time = {cursor.signed_number()}; p.carrier_detected = cursor.boolean(); if (cursor.boolean()) p.local_station_id = cursor.text(kMaxIdentityBytes); p.detail = cursor.text(); event.payload = std::move(p); break;
        }
        case EventKind::ReceptionSuccess: case EventKind::ReceptionFailure: {
            ReceptionResult p; p.reception_id = {cursor.text(kMaxIdentityBytes)}; p.transmission_id = {cursor.text(kMaxIdentityBytes)}; p.receiver_id = {cursor.text(kMaxIdentityBytes)}; p.availability_time = {cursor.signed_number()}; p.detail = cursor.text(); event.payload = std::move(p); break;
        }
        case EventKind::ReceiverNmea: {
            ReceiverNmea p; p.receiver_id = {cursor.text(kMaxIdentityBytes)}; p.reception_id = {cursor.text(kMaxIdentityBytes)}; p.transmission_id = {cursor.text(kMaxIdentityBytes)}; const auto count = cursor.number(4); if (count == 0 || count > 9) invalid("NMEA fragment count outside1..9"); for (std::uint64_t index = 0; index < count; ++index) p.sentences.push_back(cursor.text(82)); event.payload = std::move(p); break;
        }
        case EventKind::OutputError: { OutputError p; p.receiver_id = {cursor.text(kMaxIdentityBytes)}; p.code = cursor.text(); p.detail = cursor.text(); event.payload = std::move(p); break; }
        case EventKind::RfNoReception: {
            RfNoReception p; p.transmission_id = {cursor.text(kMaxIdentityBytes)};
            p.receiver_id = {cursor.text(kMaxIdentityBytes)}; p.reason = static_cast<NoReceptionReason>(cursor.number(1));
            p.detail = cursor.text(); event.payload = std::move(p); break;
        }
        case EventKind::ReceptionNoOutput: {
            ReceptionNoOutput p; p.receiver_id = {cursor.text(kMaxIdentityBytes)};
            p.reception_id = {cursor.text(kMaxIdentityBytes)}; p.transmission_id = {cursor.text(kMaxIdentityBytes)};
            p.reason = cursor.text(); event.payload = std::move(p); break;
        }
        case EventKind::TransmissionResolved: {
            TransmissionResolved p; p.transmission_id = {cursor.text(kMaxIdentityBytes)};
            p.resolution_horizon = {cursor.signed_number()}; event.payload = std::move(p); break;
        }
        default: invalid("unsupported event kind/version");
    }
    cursor.end(); validate_event(event); return event;
}

struct JournalWriter::Impl {
    Impl(const std::filesystem::path& requested, JournalMetadata initial,
         JournalOptions configured, Hooks injected = {})
        : metadata(std::move(initial)), options(configured), hooks(std::move(injected)),
          validator(metadata.validation_profile) {
        validate_metadata(metadata); validate_options(options);
        version = file_version(metadata.validation_profile);
        if (requested.empty()) invalid("run directory is empty");
        std::error_code error;
        directory = std::filesystem::absolute(requested, error).lexically_normal();
        if (error) throw std::runtime_error("Cannot resolve journal run directory");
        if (!std::filesystem::create_directory(directory, error)) {
            if (!error || error == std::errc::file_exists) invalid("run directory already exists; refusing overwrite");
            throw std::runtime_error("Cannot exclusively create journal run directory: " + error.message());
        }
        const auto initial_file = envelope(1, metadata_bytes(metadata, options), version);
        publish(directory / "manifest-incomplete.bin", initial_file, hooks, ++publications);
        chain = fingerprint(initial_file);
    }

    void catalog(const Event& event) const {
        validate_catalog_event(metadata, event);
    }

    void flush_segment() {
        if (buffer_records == 0) return;
        if (statistics.segments >= kSegmentLimit) invalid("run exceeds1000000segments");
        const auto index = statistics.segments + 1;
        Bytes header{'A','I','S','J','R','0','S','1'};
        number(header, version, 2); number(header, kSegmentHeader, 2); number(header, index, 8);
        number(header, buffer_records, 4); number(header, buffer.size(), 4);
        number(header, static_cast<std::uint64_t>(kUnitsPerSecond), 8);
        Crc crc; crc.update(header.data(), header.size()); crc.update(buffer.data(), buffer.size());
        Bytes footer; number(footer, crc.value(), 4);
        publish(directory / numbered("segment-", index),
            std::vector<View>{{header.data(), header.size()}, {buffer.data(), buffer.size()}, {footer.data(), footer.size()}},
            hooks, ++publications);
        Checkpoint checkpoint{index, buffer_records, header.size() + buffer.size() + footer.size(),
            crc.value(), statistics.records, statistics.logical_digest, chain, first_time, last_time};
        const auto checkpoint_file = envelope(2, checkpoint_bytes(checkpoint), version);
        publish(directory / numbered("checkpoint-", index), checkpoint_file, hooks, ++publications);
        chain = fingerprint(checkpoint_file);
        statistics.segments = index;
        buffer.clear(); buffer_records = 0;
    }

    void append(const Event& event) {
        if (failed || complete) invalid("writer is failed or already COMPLETE");
        try {
            auto bytes = encode_event(event);
            if (event.schema_version != version) invalid("event schema does not match journal validation profile");
            const auto framed_size = bytes.size() + 8;
            const auto body_limit = options.max_segment_bytes - kSegmentOverhead;
            if (framed_size > body_limit) invalid("event cannot fit configured segment byte limit");
            catalog(event);
            validator.append(event);
            statistics.validator = validator.stats();
            statistics.retained_recent_events = validator.retained_history();
            if (buffer_records == options.max_records_per_segment || framed_size > body_limit - buffer.size()) flush_segment();
            const auto required = buffer.size() + framed_size;
            if (required > buffer.capacity()) {
                const auto grown = std::max(required, std::min(body_limit, std::max(std::size_t{4096}, buffer.capacity() * 2)));
                buffer.reserve(grown);
            }
            Bytes length; number(length, bytes.size(), 4);
            buffer.insert(buffer.end(), length.begin(), length.end());
            buffer.insert(buffer.end(), bytes.begin(), bytes.end());
            number(buffer, crc32c(bytes.data(), bytes.size()), 4);
            hash_bytes(statistics.logical_digest, length.data(), length.size());
            hash_bytes(statistics.logical_digest, bytes.data(), bytes.size());
            if (statistics.records == std::numeric_limits<std::uint64_t>::max()) invalid("record count overflow");
            ++statistics.records;
            if (buffer_records == 0) first_time = event.time.units;
            last_time = event.time.units;
            ++buffer_records;
            statistics.peak_buffer_bytes = std::max(statistics.peak_buffer_bytes,
                buffer.capacity() + bytes.capacity() + kSegmentOverhead);
            if (buffer_records == options.max_records_per_segment) flush_segment();
        } catch (...) { failed = true; throw; }
    }

    void finish() {
        if (failed || complete) invalid("writer is failed or already COMPLETE");
        try {
            // Every append already passed the nonterminal validator. Preserve
            // that accepted prefix even when terminal obligations remain open.
            flush_segment();
            validator.finish();
            statistics.validator = validator.stats();
            statistics.retained_recent_events = validator.retained_history();
            if (hooks.fault == Fault::BeforeComplete) throw std::runtime_error("Injected interruption before COMPLETE publication");
            Bytes payload; number(payload, 1, 1);
            number(payload, statistics.segments, 8); number(payload, statistics.records, 8);
            number(payload, statistics.logical_digest, 8); number(payload, chain, 8);
            const auto terminal = envelope(3, payload, version);
            publish(directory / "manifest-complete.bin", terminal, hooks, ++publications);
            complete = true;
        } catch (...) { failed = true; throw; }
    }

    std::filesystem::path directory;
    JournalMetadata metadata;
    JournalOptions options;
    Hooks hooks;
    EventValidator validator;
    JournalStats statistics;
    Bytes buffer;
    std::uint32_t buffer_records = 0;
    std::int64_t first_time = 0, last_time = 0;
    std::uint64_t chain = 0, publications = 0;
    std::uint16_t version = 1;
    bool failed = false, complete = false;
};

namespace {
#ifdef AISMIXER_LAB_RUNTIME_JOURNAL_TESTING
Hooks testing_hooks(const testing::JournalHooks& requested) {
    Hooks hooks;
    switch (requested.fault) {
        case testing::JournalFault::None: break;
        case testing::JournalFault::Write: hooks.fault = Fault::Write; break;
        case testing::JournalFault::Flush: hooks.fault = Fault::Flush; break;
        case testing::JournalFault::Close: hooks.fault = Fault::Close; break;
        case testing::JournalFault::Publish: hooks.fault = Fault::Publish; break;
        case testing::JournalFault::BeforeComplete: hooks.fault = Fault::BeforeComplete; break;
    }
    hooks.publication_number = requested.publication_number;
    hooks.before_publish = requested.before_publish;
    return hooks;
}
#endif
}  // namespace

JournalWriter::JournalWriter(const std::filesystem::path& path, JournalMetadata metadata, JournalOptions options)
    : impl_(std::make_unique<Impl>(path, std::move(metadata), options)) {}
#ifdef AISMIXER_LAB_RUNTIME_JOURNAL_TESTING
JournalWriter::JournalWriter(const std::filesystem::path& path, JournalMetadata metadata, JournalOptions options,
                             const testing::JournalHooks& hooks)
    : impl_(std::make_unique<Impl>(path, std::move(metadata), options, testing_hooks(hooks))) {}
#endif
JournalWriter::~JournalWriter() = default;
JournalWriter::JournalWriter(JournalWriter&&) noexcept = default;
JournalWriter& JournalWriter::operator=(JournalWriter&&) noexcept = default;
void JournalWriter::append(const Event& event) { if (!impl_) invalid("moved-from writer"); impl_->append(event); }
void JournalWriter::finish() { if (!impl_) invalid("moved-from writer"); impl_->finish(); }
const JournalStats& JournalWriter::stats() const { if (!impl_) invalid("moved-from writer"); return impl_->statistics; }

struct JournalReader::Impl {
    Impl(const std::filesystem::path& requested, bool allow_incomplete) {
        std::error_code error;
        directory = std::filesystem::absolute(requested, error).lexically_normal();
        if (requested.empty() || error || !std::filesystem::is_directory(directory)) invalid("run directory is missing");
        auto initial = read_envelope(directory / "manifest-incomplete.bin", 1, &initial_chain, 0, &version);
        Cursor cursor(initial);
        if (version == 2) {
            if (cursor.number(4) != static_cast<std::uint32_t>(ValidationProfile::StreamingV2))
                invalid("unsupported version 2 validation profile tag");
            metadata.validation_profile = ValidationProfile::StreamingV2;
        }
        if (cursor.number(1) != 0) invalid("initial manifest must be INCOMPLETE");
        metadata.provenance = cursor.text();
        if (cursor.boolean()) metadata.effective_seed = cursor.number(8);
        metadata.utc_anchor = {cursor.signed_number(), cursor.signed_number()};
        metadata.model_version = cursor.text(128); metadata.codec_version = cursor.text(128);
        const auto receivers = cursor.number(4);
        if (receivers > kMaxEventSources) invalid("oversized receiver catalog");
        for (std::uint64_t index = 0; index < receivers; ++index) metadata.receivers.push_back({cursor.text(kMaxIdentityBytes)});
        options.max_records_per_segment = static_cast<std::uint32_t>(cursor.number(4));
        options.max_segment_bytes = static_cast<std::size_t>(cursor.number(4));
        cursor.end(); validate_metadata(metadata); validate_options(options);

        if (std::filesystem::exists(directory / "manifest-complete.bin")) {
            const auto terminal = read_envelope(directory / "manifest-complete.bin", 3, &terminal_hash, version);
            Cursor end(terminal);
            if (end.number(1) != 1) invalid("terminal manifest must be COMPLETE");
            expected.segments = end.number(8); expected.records = end.number(8);
            expected.logical_digest = end.number(8); expected_chain = end.number(8); end.end();
            state = JournalStatus::Complete;
            if (checkpoint_count(directory) != expected.segments) invalid("COMPLETE manifest/checkpoint count mismatch");
        } else {
            if (!allow_incomplete) invalid("run is INCOMPLETE; explicit inspection is required");
            const auto largest = checkpoint_count(directory);
            expected.segments = largest;
            if (largest != 0) {
                const auto checkpoint = checkpoint_from(read_envelope(directory / numbered("checkpoint-", largest), 2, &expected_chain, version));
                expected.records = checkpoint.total_records; expected.logical_digest = checkpoint.logical_digest;
            } else { expected.logical_digest = kFnvOffset; expected_chain = initial_chain; }
        }
        if (expected.segments > kSegmentLimit) invalid("manifest exceeds segment resource limit");
        scan({});
    }

    void scan(const std::function<void(const Event&)>& callback) const {
        std::uint64_t current_initial = 0;
        (void)read_envelope(directory / "manifest-incomplete.bin", 1, &current_initial, version);
        if (current_initial != initial_chain) invalid("initial manifest changed after opening");
        if (state == JournalStatus::Complete) {
            std::uint64_t current_terminal = 0;
            (void)read_envelope(directory / "manifest-complete.bin", 3, &current_terminal, version);
            if (current_terminal != terminal_hash || checkpoint_count(directory) != expected.segments) invalid("COMPLETE manifest/checkpoints changed after opening");
        }
        EventValidator validator(metadata.validation_profile);
        auto chain = initial_chain;
        JournalStats observed;
        for (std::uint64_t segment = 1; segment <= expected.segments; ++segment) {
            std::uint64_t next_chain = 0;
            const auto checkpoint = checkpoint_from(read_envelope(directory / numbered("checkpoint-", segment), 2, &next_chain, version));
            if (checkpoint.index != segment || checkpoint.prior_chain != chain || checkpoint.records == 0 ||
                checkpoint.records > options.max_records_per_segment || checkpoint.file_bytes > options.max_segment_bytes ||
                checkpoint.file_bytes < kSegmentOverhead) invalid("checkpoint metadata/chain/resource mismatch");
            std::ifstream input(directory / numbered("segment-", segment), std::ios::binary);
            if (!input) invalid("missing committed segment");
            Bytes header(kSegmentHeader); read_exact(input, header.data(), header.size());
            if (std::string(header.begin(), header.begin() + 8) != "AISJR0S1") invalid("unknown segment magic");
            Bytes fields(header.begin() + 8, header.end()); Cursor h(fields);
            if (h.number(2) != version || h.number(2) != kSegmentHeader || h.number(8) != segment ||
                h.number(4) != checkpoint.records) invalid("segment schema/index/count mismatch");
            const auto body_bytes = h.number(4);
            if (body_bytes + kSegmentOverhead != checkpoint.file_bytes || h.number(8) != static_cast<std::uint64_t>(kUnitsPerSecond)) invalid("segment length/timebase mismatch");
            Crc segment_crc; segment_crc.update(header.data(), header.size());
            std::uint64_t consumed = 0;
            std::int64_t first_time = 0, last_time = 0;
            for (std::uint32_t index = 0; index < checkpoint.records; ++index) {
                Bytes length(4); read_exact(input, length.data(), length.size());
                Cursor n(length); const auto size = n.number(4);
                if (size == 0 || size > kMaxJournalRecordBytes || consumed + size + 8 > body_bytes) invalid("oversized/truncated record framing");
                Bytes record(static_cast<std::size_t>(size)); read_exact(input, record.data(), record.size());
                Bytes suffix(4); read_exact(input, suffix.data(), suffix.size());
                Cursor crc(suffix);
                if (crc32c(record.data(), record.size()) != crc.number(4)) invalid("record CRC32C mismatch");
                segment_crc.update(length.data(), length.size()); segment_crc.update(record.data(), record.size()); segment_crc.update(suffix.data(), suffix.size());
                hash_bytes(observed.logical_digest, length.data(), length.size()); hash_bytes(observed.logical_digest, record.data(), record.size());
                auto event = decode_event(record);
                if (event.schema_version != version) invalid("event schema does not match journal validation profile");
                validate_catalog_event(metadata, event);
                validator.append(event);
                if (index == 0) first_time = event.time.units;
                last_time = event.time.units;
                consumed += size + 8; ++observed.records;
                observed.peak_buffer_bytes = std::max(observed.peak_buffer_bytes, record.capacity() + kSegmentHeader + 8);
                if (callback) callback(event);
            }
            Bytes footer(4); read_exact(input, footer.data(), footer.size()); require_end(input);
            Cursor c(footer);
            if (consumed != body_bytes || segment_crc.value() != c.number(4) || segment_crc.value() != checkpoint.segment_crc ||
                checkpoint.total_records != observed.records || checkpoint.logical_digest != observed.logical_digest ||
                checkpoint.first_time != first_time || checkpoint.last_time != last_time) invalid("segment/checkpoint integrity or chronology mismatch");
            chain = next_chain; ++observed.segments;
        }
        if (state == JournalStatus::Complete) validator.finish();
        observed.validator = validator.stats();
        observed.retained_recent_events = validator.retained_history();
        if (observed.records != expected.records || observed.logical_digest != expected.logical_digest || chain != expected_chain) invalid("terminal manifest content mismatch");
        statistics = observed;
    }

    std::filesystem::path directory;
    JournalMetadata metadata;
    JournalOptions options;
    JournalStatus state = JournalStatus::Incomplete;
    JournalStats expected;
    mutable JournalStats statistics;
    std::uint64_t initial_chain = 0, expected_chain = 0, terminal_hash = 0;
    std::uint16_t version = 1;
};

JournalReader::JournalReader(const std::filesystem::path& path, bool allow_incomplete)
    : impl_(std::make_unique<Impl>(path, allow_incomplete)) {}
JournalReader::~JournalReader() = default;
JournalReader::JournalReader(JournalReader&&) noexcept = default;
JournalReader& JournalReader::operator=(JournalReader&&) noexcept = default;
const JournalMetadata& JournalReader::metadata() const { if (!impl_) invalid("moved-from reader"); return impl_->metadata; }
JournalStatus JournalReader::status() const { if (!impl_) invalid("moved-from reader"); return impl_->state; }
const JournalStats& JournalReader::stats() const { if (!impl_) invalid("moved-from reader"); return impl_->statistics; }
void JournalReader::validate() const { if (!impl_) invalid("moved-from reader"); impl_->scan({}); }
void JournalReader::for_each(const std::function<void(const Event&)>& callback) const {
    if (!callback) invalid("empty reader callback");
    validate(); impl_->scan(callback);
}

#ifdef AISMIXER_LAB_RUNTIME_JOURNAL_TESTING
namespace testing {
JournalWriter make_journal_writer(const std::filesystem::path& path, JournalMetadata metadata,
                                  JournalOptions options, const JournalHooks& hooks) {
    return JournalWriter(path, std::move(metadata), options, hooks);
}
}  // namespace testing
#endif

}  // namespace aismixer::lab::runtime
