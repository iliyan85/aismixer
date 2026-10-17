#include "playback.h"

#include <algorithm>
#include <chrono>
#include <limits>
#include <ostream>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <type_traits>

namespace aismixer::lab::runtime {
namespace {

bool enabled(ConsoleCategory category, const ConsoleOptions& options) {
    return std::find(options.categories.begin(), options.categories.end(), category) != options.categories.end();
}

const char* category_name(ConsoleCategory category) {
    switch (category) {
        case ConsoleCategory::Lifecycle: return "lifecycle";
        case ConsoleCategory::SlotMap: return "slotmap";
        case ConsoleCategory::Tdma: return "tdma";
        case ConsoleCategory::Rf: return "rf";
        case ConsoleCategory::Reception: return "reception";
        case ConsoleCategory::Output: return "output";
        case ConsoleCategory::Metrics: return "metrics";
    }
    throw std::invalid_argument("unknown console category");
}

bool matches_view(const Event& event, const std::string& view) {
    if (event.source_id == view || event.subject_id == view) return true;
    if (const auto receiver = event_receiver(event); receiver && receiver->value == view) return true;
    return std::visit([&](const auto& payload) {
        using T = std::decay_t<decltype(payload)>;
        if constexpr (std::is_same_v<T, StationLifecycle> || std::is_same_v<T, SlotMapChange> ||
                      std::is_same_v<T, TdmaDecision> || std::is_same_v<T, TransmissionStart>) {
            return payload.station_id == view;
        } else if constexpr (std::is_same_v<T, RfObservation>) {
            return payload.local_station_id && *payload.local_station_id == view;
        }
        return false;
    }, event.payload);
}

void emit_event(std::ostream& diagnostics, const Event& event) {
    constexpr const char* names[]{"invalid", "station_on", "station_off", "receiver_on", "receiver_off",
        "slot_map_init", "slot_map_reset", "slot_reserve", "slot_release", "tdma_decision",
        "transmission_start", "transmission_end", "rf_observation", "reception_success",
        "reception_failure", "receiver_nmea", "output_error", "rf_no_reception",
        "reception_no_output", "transmission_resolved"};
    const auto code = static_cast<unsigned>(event.kind);
    if (code == 0 || code >= sizeof(names) / sizeof(names[0])) throw std::invalid_argument("unsupported console event kind");
    std::ostringstream row;
    row << "[r0 " << category_name(console_category(event.kind)) << "] time_units=" << event.time.units
        << " phase=" << static_cast<unsigned>(causal_phase(event.kind))
        << " kind=" << names[code]
        << " id=" << event.event_id.source_id << ':' << event.event_id.sequence
        << " source=" << event.source_id << " subject=" << event.subject_id << '\n';
    const auto text = row.str();
    diagnostics.write(text.data(), static_cast<std::streamsize>(text.size()));
    if (!diagnostics) throw std::runtime_error("runtime console diagnostics write failed");
}

}  // namespace

std::int64_t SteadyPlaybackClock::now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
void SteadyPlaybackClock::sleep_until_ns(std::int64_t deadline) {
    std::this_thread::sleep_until(std::chrono::time_point<std::chrono::steady_clock,
                                  std::chrono::nanoseconds>(std::chrono::nanoseconds(deadline)));
}

PlaybackSpeed PlaybackSpeed::parse(std::string_view value) {
    if (!value.empty() && value.front() == 'x') value.remove_prefix(1);
    PlaybackSpeed speed;
    if (value == "fast") speed.fast_ = true;
    else if (value == "0.5") speed.denominator_ = 2;
    else if (value == "1") {}
    else if (value == "2") speed.numerator_ = 2;
    else if (value == "10") speed.numerator_ = 10;
    else if (value == "100") speed.numerator_ = 100;
    else throw std::invalid_argument("playback speed must be 0.5, 1, 2, 10, 100 or fast");
    return speed;
}

std::int64_t playback_offset_ns(std::int64_t delta_units, PlaybackSpeed speed) {
    if (delta_units < 0) throw std::invalid_argument("negative playback simulation offset");
    if (speed.fast()) return 0;
    const auto units = static_cast<std::uint64_t>(delta_units);
    const std::uint64_t denominator = static_cast<std::uint64_t>(kUnitsPerSecond) * speed.numerator();
    const std::uint64_t factor = UINT64_C(1000000000) * speed.denominator();
    const std::uint64_t whole = units / denominator;
    const std::uint64_t remainder = units % denominator;
    const auto maximum = static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
    if (whole > maximum / factor) throw std::overflow_error("playback nanosecond deadline offset overflow");
    const auto fractional = (remainder * factor) / denominator;
    const auto integral = whole * factor;
    if (fractional > maximum - integral) throw std::overflow_error("playback nanosecond deadline offset overflow");
    return static_cast<std::int64_t>(integral + fractional);
}

ConsoleOptions parse_console_options(std::string_view categories, std::optional<std::string> view) {
    ConsoleOptions result;
    if (view && *view == "all") view.reset();
    if (view) validate_identity(*view, "console observational view");
    result.view = std::move(view);
    if (categories == "disabled") return result;
    const std::vector<ConsoleCategory> all{ConsoleCategory::Lifecycle, ConsoleCategory::SlotMap,
        ConsoleCategory::Tdma, ConsoleCategory::Rf, ConsoleCategory::Reception,
        ConsoleCategory::Output, ConsoleCategory::Metrics};
    if (categories == "all") {
        result.categories = all;
        return result;
    }
    std::size_t start = 0;
    while (start <= categories.size()) {
        const auto end = categories.find(',', start);
        const auto part = categories.substr(start, end == std::string_view::npos ? end : end - start);
        const auto found = std::find_if(all.begin(), all.end(), [&](ConsoleCategory category) {
            return part == category_name(category);
        });
        if (found == all.end() || enabled(*found, result)) {
            throw std::invalid_argument("unknown, empty or duplicate runtime console category");
        }
        result.categories.push_back(*found);
        if (end == std::string_view::npos) break;
        start = end + 1;
    }
    return result;
}

ConsoleCategory console_category(EventKind kind) {
    switch (kind) {
        case EventKind::StationOn: case EventKind::StationOff:
        case EventKind::ReceiverOn: case EventKind::ReceiverOff: return ConsoleCategory::Lifecycle;
        case EventKind::SlotMapInit: case EventKind::SlotMapReset:
        case EventKind::SlotReserve: case EventKind::SlotRelease: return ConsoleCategory::SlotMap;
        case EventKind::TdmaDecision: return ConsoleCategory::Tdma;
        case EventKind::TransmissionStart: case EventKind::TransmissionEnd:
        case EventKind::RfObservation: case EventKind::TransmissionResolved: return ConsoleCategory::Rf;
        case EventKind::ReceptionSuccess: case EventKind::ReceptionFailure:
        case EventKind::RfNoReception: return ConsoleCategory::Reception;
        case EventKind::ReceiverNmea: case EventKind::OutputError:
        case EventKind::ReceptionNoOutput: return ConsoleCategory::Output;
    }
    throw std::invalid_argument("unrecognized runtime event kind for display");
}
bool visible_event(const Event& event, const ConsoleOptions& options) {
    if (!enabled(console_category(event.kind), options)) return false;
    return !options.view || matches_view(event, *options.view);
}

ReceiverId select_receiver(const JournalMetadata& metadata, const std::optional<ReceiverId>& requested) {
    if (requested) {
        validate_identity(requested->value, "playback receiver");
        if (std::none_of(metadata.receivers.begin(), metadata.receivers.end(), [&](const ReceiverId& receiver) {
            return receiver == *requested;
        })) throw std::invalid_argument("selected playback receiver is absent from the journal catalog");
        return *requested;
    }
    if (metadata.receivers.size() != 1) {
        throw std::invalid_argument("raw NMEA output requires one selected receiver; multi-receiver playback is ambiguous");
    }
    return metadata.receivers.front();
}

PlaybackStats play_recording(const JournalReader& reader, const PlaybackOptions& options,
                             MonotonicClock& clock, OutputSink& sink, std::ostream& diagnostics) {
    if (reader.status() != JournalStatus::Complete) throw std::invalid_argument("playback requires a COMPLETE journal");
    const ReceiverId receiver = select_receiver(reader.metadata(), options.receiver);
    PlaybackStats stats;
    std::optional<SimulationInstant> simulation_origin;
    std::int64_t clock_origin = 0;
    std::optional<std::int64_t> previous_clock;
    reader.for_each([&](const Event& event) {
        if (!simulation_origin) {
            simulation_origin = event.time;
            clock_origin = clock.now_ns();
            previous_clock = clock_origin;
        }
        if (!options.speed.fast()) {
            const auto offset = playback_offset_ns(difference_units(event.time, *simulation_origin), options.speed);
            if (clock_origin > std::numeric_limits<std::int64_t>::max() - offset) {
                throw std::overflow_error("absolute playback deadline exceeds int64 nanoseconds");
            }
            const auto deadline = clock_origin + offset;
            auto now = clock.now_ns();
            if (previous_clock && now < *previous_clock) throw std::runtime_error("playback clock regressed");
            if (now < deadline) {
                clock.sleep_until_ns(deadline);
                const auto after_sleep = clock.now_ns();
                if (after_sleep < deadline || after_sleep < now) {
                    throw std::runtime_error("playback sleeper returned before its monotonic deadline");
                }
                now = after_sleep;
            }
            previous_clock = now;
            if (now > deadline) ++stats.late_events;
        }
        ++stats.events;
        if (visible_event(event, options.console)) {
            emit_event(diagnostics, event);
            ++stats.displayed_events;
        }
        if (event.kind == EventKind::ReceiverNmea) {
            const auto& record = std::get<ReceiverNmea>(event.payload);
            if (record.receiver_id == receiver) {
                sink.deliver(event.time, record);
                ++stats.output_records;
                for (const auto& sentence : record.sentences) stats.output_bytes += sentence.size();
            }
        }
    });
    sink.flush();
    if (enabled(ConsoleCategory::Metrics, options.console)) {
        diagnostics << "[r0 metrics] events=" << stats.events << " displayed=" << stats.displayed_events
                    << " receiver=" << receiver.value << " output_records=" << stats.output_records
                    << " output_bytes=" << stats.output_bytes << " late_events=" << stats.late_events << '\n';
    }
    diagnostics.flush();
    if (!diagnostics) throw std::runtime_error("runtime console diagnostics flush failed");
    return stats;
}

}  // namespace aismixer::lab::runtime
