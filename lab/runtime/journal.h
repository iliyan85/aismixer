#pragma once

#include "event.h"
#include "simulation_time.h"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace aismixer::lab::runtime {

inline constexpr std::size_t kMaxJournalRecordBytes = 65536;
inline constexpr std::size_t kMaxJournalSegmentBytes = 8 * 1024 * 1024;
inline constexpr std::uint32_t kMaxJournalSegmentRecords = 65536;

struct JournalMetadata {
    std::string provenance;
    std::optional<std::uint64_t> effective_seed;
    UtcAnchor utc_anchor;
    std::string model_version;
    std::string codec_version;
    std::vector<ReceiverId> receivers;
};
struct JournalOptions {
    std::uint32_t max_records_per_segment = 4096;
    std::size_t max_segment_bytes = kMaxJournalSegmentBytes;
};
enum class JournalStatus { Incomplete, Complete };
struct JournalStats {
    std::uint64_t records = 0;
    std::uint64_t segments = 0;
    std::uint64_t logical_digest = UINT64_C(14695981039346656037);
    std::size_t peak_buffer_bytes = 0;
};

namespace testing { struct JournalHooks; }

// CRC32C/Castagnoli, reflected polynomial 0x82F63B78, initial/final XOR all
// ones. Integrity diagnostics only; this is not authentication or encryption.
std::uint32_t crc32c(const std::uint8_t* bytes, std::size_t count);

// Portable canonical Event bytes (without journal framing/CRC). The schema is
// versioned explicitly; raw C++ object representation is never serialized.
std::vector<std::uint8_t> encode_event(const Event& event);
Event decode_event(const std::vector<std::uint8_t>& bytes);

class JournalWriter {
public:
    JournalWriter(const std::filesystem::path& new_run_directory,
                  JournalMetadata metadata, JournalOptions options = {});
    // Defined only in native test builds. This declaration remains identical
    // in every translation unit; production exposes no JournalHooks definition.
    JournalWriter(const std::filesystem::path& new_run_directory,
                  JournalMetadata metadata, JournalOptions options,
                  const testing::JournalHooks& hooks);
    ~JournalWriter();
    JournalWriter(JournalWriter&&) noexcept;
    JournalWriter& operator=(JournalWriter&&) noexcept;
    JournalWriter(const JournalWriter&) = delete;
    JournalWriter& operator=(const JournalWriter&) = delete;
    // Only final, canonical committed events belong here. Shared validation
    // rejects inconsistent chronology/identity/causality. Failure poisons this
    // writer; COMPLETE is never published after a rejected/failed append.
    void append(const Event& event);
    void finish();
    const JournalStats& stats() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

class JournalReader {
public:
    // COMPLETE is required by default. Explicit incomplete inspection reads
    // only immutable checkpoints and their validated committed segments.
    explicit JournalReader(const std::filesystem::path& run_directory,
                           bool allow_incomplete = false);
    ~JournalReader();
    JournalReader(JournalReader&&) noexcept;
    JournalReader& operator=(JournalReader&&) noexcept;
    JournalReader(const JournalReader&) = delete;
    JournalReader& operator=(const JournalReader&) = delete;
    const JournalMetadata& metadata() const;
    JournalStatus status() const;
    const JournalStats& stats() const;
    // A complete integrity pass precedes callbacks; iteration is bounded and
    // validates again. No all-history vector or authoritative side index.
    void validate() const;
    void for_each(const std::function<void(const Event&)>& callback) const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

#ifdef AISMIXER_LAB_RUNTIME_JOURNAL_TESTING
namespace testing {
enum class JournalFault { None, Write, Flush, Close, Publish, BeforeComplete };
struct JournalHooks {
    JournalFault fault = JournalFault::None;
    std::uint64_t publication_number = 1;
    std::function<void(const std::filesystem::path&)> before_publish;
};
// Explicit native-test construction; no environment or production CLI hook.
JournalWriter make_journal_writer(const std::filesystem::path& run_directory,
                                  JournalMetadata metadata, JournalOptions options,
                                  const JournalHooks& hooks);
}  // namespace testing
#endif

}  // namespace aismixer::lab::runtime
