#pragma once

#include "journal.h"
#include "output.h"

#include <cstdint>
#include <iosfwd>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace aismixer::lab::runtime {

class MonotonicClock {
public:
    virtual ~MonotonicClock() = default;
    virtual std::int64_t now_ns() = 0;
    virtual void sleep_until_ns(std::int64_t deadline) = 0;
};
class SteadyPlaybackClock final : public MonotonicClock {
public:
    std::int64_t now_ns() override;
    void sleep_until_ns(std::int64_t deadline) override;
};

class PlaybackSpeed {
public:
    PlaybackSpeed() = default;  // x1
    static PlaybackSpeed parse(std::string_view value);
    bool fast() const noexcept { return fast_; }
    std::uint32_t numerator() const noexcept { return numerator_; }
    std::uint32_t denominator() const noexcept { return denominator_; }
private:
    std::uint32_t numerator_ = 1;
    std::uint32_t denominator_ = 1;
    bool fast_ = false;
};
// Floor to whole monotonic nanoseconds (<1ns early rounding). Checked absolute
// offsets avoid accumulating relative sleep/rounding errors. Presets stop at
// x100, where one simulation unit still exceeds one wall nanosecond.
std::int64_t playback_offset_ns(std::int64_t delta_units, PlaybackSpeed speed);

enum class ConsoleCategory { Lifecycle, SlotMap, Tdma, Rf, Reception, Output, Metrics };
struct ConsoleOptions {
    std::vector<ConsoleCategory> categories;  // Empty means disabled.
    std::optional<std::string> view;  // Direct source/subject/station/receiver ID; no clusters.
};
ConsoleOptions parse_console_options(std::string_view categories,
                                      std::optional<std::string> view = std::nullopt);
ConsoleCategory console_category(EventKind kind);
bool visible_event(const Event& event, const ConsoleOptions& options);

struct PlaybackOptions {
    PlaybackSpeed speed;
    std::optional<ReceiverId> receiver;
    ConsoleOptions console;
};
struct PlaybackStats {
    std::uint64_t events = 0;
    std::uint64_t displayed_events = 0;
    std::uint64_t output_records = 0;
    std::uint64_t output_bytes = 0;
    std::uint64_t late_events = 0;
};
ReceiverId select_receiver(const JournalMetadata& metadata,
                            const std::optional<ReceiverId>& requested);
// COMPLETE journals only. One bounded record at a time, first canonical event
// maps to clock.now_ns(), all deadlines derive from that single origin.
// Console filtering is observational and cannot filter NMEA or modify journal.
PlaybackStats play_recording(const JournalReader& reader, const PlaybackOptions& options,
                             MonotonicClock& clock, OutputSink& sink,
                             std::ostream& diagnostics);

}  // namespace aismixer::lab::runtime
