#include "streaming_fixture.h"

#include "scheduler.h"

#include <algorithm>
#include <limits>
#include <map>
#include <stdexcept>
#include <utility>

namespace aismixer::lab::runtime {
namespace {
constexpr const char* sentence_a = "!AIVDM,1,1,,A,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*2B\r\n";
constexpr const char* sentence_b = "!AIVDM,1,1,,B,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*28\r\n";
struct ScriptedTx {
    TransmissionId tx;
    std::string station;
    SimulationInstant start, end, available, resolved;
    std::uint8_t channel;
    std::vector<ReceptionId> receptions;
};
struct Draft {
    Event event;
    std::size_t tx_index = 0;
    std::size_t receiver_index = 0;
    std::uint64_t semantic_key = 0;
};
Event event(SimulationInstant time, EventKind kind, const std::string& source,
            const std::string& subject, EventPayload payload) {
    return {2, time, kind, source, subject, {source, 0}, std::nullopt, std::move(payload)};
}
std::uint64_t next(std::uint64_t& value) {
    if (value == std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("streaming fixture producer counter exhausted");
    return ++value;
}
}

JournalMetadata streaming_fixture_metadata() {
    JournalMetadata metadata;
    metadata.provenance = "INFRASTRUCTURE TEST -- NOT AIS RF SIMULATION; five-hour scripted symbolic producers at illustrative 10s/2s intervals; explicit outcomes";
    metadata.utc_anchor = {1792195199, kUnitsPerSecond / 2};
    metadata.model_version = "r01-streaming-infrastructure-fixture-v2";
    metadata.codec_version = "B3-pinned-finalized-NMEA-bytes-v1";
    metadata.receivers = {{"receiver.R1"}, {"receiver.R2"}, {"receiver.R3"}};
    metadata.validation_profile = ValidationProfile::StreamingV2;
    return metadata;
}

JournalStats record_streaming_fixture(const std::filesystem::path& directory,
                                     JournalOptions options, bool fine_steps) {
    const auto metadata = streaming_fixture_metadata();
    JournalWriter writer(directory, metadata, options);
    Scheduler scheduler(64, {std::numeric_limits<std::int64_t>::min()}, ValidationProfile::StreamingV2);
    std::map<std::string, std::uint64_t> sequences;
    std::map<std::string, EventId> lifecycle;
    std::map<std::string, std::uint64_t> tx_counters, rx_counters;
    const auto consume = [&](const Event& value) { writer.append(value); };
    const auto enqueue_lifecycle = [&](SimulationInstant time, bool on) {
        std::vector<Event> group;
        for (const auto& station : {std::string("station.A"), std::string("station.B")})
            group.push_back(event(time, on ? EventKind::StationOn : EventKind::StationOff,
                                  station, station, StationLifecycle{station}));
        for (const auto& receiver : metadata.receivers)
            group.push_back(event(time, on ? EventKind::ReceiverOn : EventKind::ReceiverOff,
                                  receiver.value, receiver.value, ReceiverLifecycle{receiver}));
        std::sort(group.begin(), group.end(), CanonicalLess{});
        for (auto& value : group) {
            value.event_id.sequence = next(sequences[value.source_id]);
            if (on) lifecycle[value.source_id] = value.event_id;
            scheduler.enqueue(std::move(value));
        }
    };
    enqueue_lifecycle({0}, true);
    for (std::int64_t second = 0; second < kStreamingFixtureSeconds; second += 2) {
        // At most two TX / eighteen records are held, independent of run length.
        std::vector<ScriptedTx> transmissions;
        for (unsigned index = 0; index < 2; ++index) {
            if (index == 0 && second % 10 != 0) continue;
            const std::string station = index == 0 ? "station.A" : "station.B";
            const auto start = from_seconds(second);
            const auto end = add_units(start, kUnitsPerSlot);
            ScriptedTx tx{make_transmission_id(station, next(tx_counters[station])), station,
                start, end, add_units(end, 1), add_units(end, 2), static_cast<std::uint8_t>(index), {}};
            for (const auto& receiver : metadata.receivers)
                tx.receptions.push_back(make_reception_id(receiver.value, next(rx_counters[receiver.value])));
            transmissions.push_back(std::move(tx));
        }
        std::vector<Draft> drafts;
        for (std::size_t i = 0; i < transmissions.size(); ++i) {
            const auto& tx = transmissions[i];
            const auto key = static_cast<std::uint64_t>(i);
            const auto add = [&](Event value, std::size_t receiver = 0) {
                drafts.push_back({std::move(value), i, receiver, key});
            };
            add(event(tx.start, EventKind::TransmissionStart, tx.station, tx.station,
                TransmissionStart{tx.tx, tx.station, tx.channel, tx.end, "fixture.b3.type1.golden",
                    TransmissionCoverage{metadata.receivers, tx.resolved}}));
            add(event(tx.end, EventKind::TransmissionEnd, tx.station, tx.station, TransmissionEnd{tx.tx}));
            for (std::size_t receiver = 0; receiver < metadata.receivers.size(); ++receiver) {
                const auto& rx = metadata.receivers[receiver];
                add(event(tx.available, EventKind::ReceptionSuccess, rx.value, rx.value,
                    ReceptionResult{tx.receptions[receiver], tx.tx, rx, tx.available,
                        "Explicit synthetic infrastructure outcome; NOT RF"}), receiver);
                add(event(tx.available, EventKind::ReceiverNmea, rx.value, rx.value,
                    ReceiverNmea{rx, tx.receptions[receiver], tx.tx,
                        {tx.channel == 0 ? sentence_a : sentence_b}}), receiver);
            }
            add(event(tx.resolved, EventKind::TransmissionResolved, "runtime.closure", tx.station,
                      TransmissionResolved{tx.tx, tx.resolved}));
        }
        // Drafts have no EventIds. Stable semantic TX association breaks only
        // same-source/phase ties; producer sequences are allocated at release.
        std::sort(drafts.begin(), drafts.end(), [](const Draft& a, const Draft& b) {
            const auto& x = a.event; const auto& y = b.event;
            if (x.time != y.time) return x.time < y.time;
            if (causal_phase(x.kind) != causal_phase(y.kind)) return causal_phase(x.kind) < causal_phase(y.kind);
            if (x.source_id != y.source_id) return x.source_id < y.source_id;
            return a.semantic_key < b.semantic_key;
        });
        std::vector<EventId> starts(transmissions.size()), ends(transmissions.size());
        std::map<std::string, EventId> receptions;
        for (auto& draft : drafts) {
            auto& value = draft.event;
            value.event_id.sequence = next(sequences[value.source_id]);
            if (value.kind == EventKind::TransmissionStart) starts[draft.tx_index] = value.event_id;
            if (value.kind == EventKind::TransmissionEnd) ends[draft.tx_index] = value.event_id;
            if (value.kind == EventKind::ReceptionSuccess)
                receptions.emplace(std::get<ReceptionResult>(value.payload).reception_id.value, value.event_id);
        }
        for (auto& draft : drafts) {
            auto& value = draft.event;
            switch (value.kind) {
            case EventKind::TransmissionStart: value.causal_ref = lifecycle.at(value.source_id); break;
            case EventKind::TransmissionEnd: value.causal_ref = starts[draft.tx_index]; break;
            case EventKind::ReceptionSuccess: value.causal_ref = ends[draft.tx_index]; break;
            case EventKind::ReceiverNmea:
                value.causal_ref = receptions.at(std::get<ReceiverNmea>(value.payload).reception_id.value); break;
            case EventKind::TransmissionResolved: value.causal_ref = ends[draft.tx_index]; break;
            default: throw std::logic_error("unexpected streaming infrastructure draft");
            }
            scheduler.enqueue(std::move(value));
        }
        if (fine_steps) {
            const auto start = from_seconds(second), end = add_units(start, kUnitsPerSlot);
            for (const auto target : {add_units(start, 1), end, add_units(end, 1), add_units(end, 2), add_units(end, 3)})
                scheduler.advance_watermark(target, consume);
        }
        scheduler.advance_watermark(from_seconds(second + 2), consume);
    }
    enqueue_lifecycle(from_seconds(kStreamingFixtureSeconds), false);
    scheduler.finish(consume);
    writer.finish();
    if (writer.stats().records != kStreamingFixtureRecords)
        throw std::logic_error("streaming infrastructure record count mismatch");
    return writer.stats();
}

}  // namespace aismixer::lab::runtime
