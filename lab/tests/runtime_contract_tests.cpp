#include "event.h"
#include "fixture.h"
#include "scheduler.h"

#include <algorithm>
#include <cstdint>
#include <iostream>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {
using namespace aismixer::lab::runtime;
std::size_t rejections = 0;
void require(bool value, std::string_view reason) { if (!value) throw std::runtime_error(std::string(reason)); }
template<class F> void invalid(F action, std::string_view expected) {
    bool rejected = false;
    try { action(); } catch (const std::invalid_argument& error) {
        require(std::string_view(error.what()).find(expected) != std::string_view::npos,
                std::string("unexpected rejection branch: ") + error.what());
        rejected = true;
    }
    require(rejected, std::string("missing rejection: ") + std::string(expected)); ++rejections;
}
Event make(std::uint32_t schema, SimulationInstant time, EventKind kind, std::string source,
           std::string subject, std::uint64_t sequence, EventPayload payload,
           std::optional<EventId> cause = std::nullopt) {
    Event event; event.schema_version = schema; event.time = time; event.kind = kind;
    event.source_id = source; event.subject_id = std::move(subject); event.event_id = {std::move(source), sequence};
    event.payload = std::move(payload); event.causal_ref = std::move(cause); return event;
}
Event station(std::uint32_t schema, SimulationInstant time, std::uint64_t sequence = 1) {
    return make(schema, time, EventKind::StationOn, "control", "station.A", sequence, StationLifecycle{"station.A"});
}
Event receiver(std::uint32_t schema, SimulationInstant time, std::uint64_t sequence = 2) {
    return make(schema, time, EventKind::ReceiverOn, "control", "receiver.R", sequence, ReceiverLifecycle{{"receiver.R"}});
}
std::string sentence(std::string body) {
    unsigned checksum = 0; for (unsigned char byte : body) checksum ^= byte;
    constexpr char hex[] = "0123456789ABCDEF";
    return "!" + body + '*' + hex[checksum >> 4] + hex[checksum & 15] + "\r\n";
}
void immediate_phase_guard() {
    Scheduler scheduler;
    scheduler.enqueue(station(1, {0}));
    const auto start = make(1, {100}, EventKind::TransmissionStart, "control", "station.A", 2,
        TransmissionStart{{"legacy.tx"}, "station.A", 0, {101}, "fixture.payload"}, EventId{"control", 1});
    scheduler.enqueue(start);
    auto decision = make(1, {100}, EventKind::TdmaDecision, "control", "station.A", 3,
        TdmaDecision{"station.A", "SYNTHETIC phase-order probe, not TDMA"});
    invalid([&] { scheduler.enqueue(decision); }, "phase regresses");
    require(!scheduler.finished() && scheduler.pending_size() == 2, "enqueue rejection poisoned previously valid queue");
    const auto end = make(1, {101}, EventKind::TransmissionEnd, "control", "station.A", 3,
        TransmissionEnd{{"legacy.tx"}}, start.event_id);
    scheduler.enqueue(end);  // Rejected phase inversion did not consume sequence3.
    require(scheduler.next_pending_time() && scheduler.next_pending_time()->units == 0, "next pending query");
    require(!scheduler.has_dispatched(start.event_id), "future enqueued event claimed dispatched");
    auto pending = scheduler.accepted_event(start.event_id);
    require(pending && std::get<TransmissionStart>(pending->payload).payload_reference == "fixture.payload",
            "RF binding requires exact immutable pending snapshot");
    std::get<TransmissionStart>(pending->payload).payload_reference = "caller-mutated-copy";
    require(std::get<TransmissionStart>(scheduler.accepted_event(start.event_id)->payload).payload_reference == "fixture.payload",
            "snapshot lookup exposed mutable scheduler-owned state");
    require(!scheduler.accepted_event({"missing", 1}), "snapshot lookup fabricated unknown event");
    std::size_t count = 0;
    scheduler.finish([&](const Event& event) { require(scheduler.has_dispatched(event.event_id), "consumer needs validated publication proof"); ++count; });
    require(count == 3 && scheduler.has_dispatched(start.event_id) && !scheduler.next_pending_time(), "successful phase stream or retained release proof");
    require(scheduler.accepted_event(start.event_id).has_value(), "dispatched recent snapshot unavailable");
    Scheduler lawful;
    lawful.enqueue(station(1, {0})); decision.event_id.sequence = 2; lawful.enqueue(decision);
    auto later = start; later.event_id.sequence = 3; lawful.enqueue(later);
    auto final = end; final.event_id.sequence = 4; final.causal_ref = later.event_id; lawful.enqueue(final);
    lawful.finish([](const Event&) {});
}
void nmea_parent_consistency() {
    const auto fixture = make_synthetic_fixture();
    const auto second = std::find_if(fixture.events.begin(), fixture.events.end(), [](const Event& event) {
        return event.kind == EventKind::ReceiverNmea && event.subject_id == "receiver.R2";
    });
    require(second != fixture.events.end(), "fixed fixture second output");
    EventValidator validator;
    for (auto it = fixture.events.begin(); it != second; ++it) validator.append(*it);
    const std::string armor = "1Amg=5EmQso>f;LFGd5DlSoo7D<Q";
    auto output = *second;
    auto& record = std::get<ReceiverNmea>(output.payload);
    record.sentences = {sentence("AIVDM,1,1,,B," + armor + ",0")};
    invalid([&] { validator.append(output); }, "physical TX channel");
    auto changed = armor; changed.back() = 'P';
    record.sentences = {sentence("AIVDM,1,1,,A," + changed + ",0")};
    invalid([&] { validator.append(output); }, "payload bits differ");
    record.sentences = {sentence("AIVDM,1,1,,A," + changed + ",1")};
    invalid([&] { validator.append(output); }, "payload bits differ");
    record.sentences = {sentence("AIVDM,1,1,,A," + armor + ",1")};
    invalid([&] { validator.append(output); }, "padding");
    record.sentences = {sentence("AIVDM,2,2,7,A," + armor.substr(10) + ",0"),
                        sentence("AIVDM,2,1,7,A," + armor.substr(0, 10) + ",0")};
    invalid([&] { validator.append(output); }, "fragment fields");
    // Receiver-local serial fragment count, boundaries and sequence metadata
    // can differ while the over-the-air AIS payload bits remain identical.
    record.sentences = {sentence("AIVDM,2,1,7,A," + armor.substr(0, 10) + ",0"),
                        sentence("AIVDM,2,2,7,A," + armor.substr(10) + ",0")};
    validator.append(output);
    for (auto it = second + 1; it != fixture.events.end(); ++it) validator.append(*it);
    validator.finish();
    EventValidator untouched;
    for (const auto& event : fixture.events) untouched.append(event);
    untouched.finish();
}
struct Stream {
    EventValidator validator{ValidationProfile::StreamingV2};
    std::map<std::string, std::uint64_t> sequences;
    Stream() { push(station(2, {0})); push(receiver(2, {0})); }
    void push(const Event& event) { validator.append(event); sequences[event.source_id] = event.event_id.sequence; }
    Event tx(std::uint64_t id, std::int64_t time, std::int64_t end, std::int64_t horizon) {
        return make(2, {time}, EventKind::TransmissionStart, "txp", "station.A", sequences["txp"] + 1,
            TransmissionStart{make_transmission_id("txp", id), "station.A", 0, {end}, "fixture.payload",
                TransmissionCoverage{{{"receiver.R"}}, {horizon}}}, EventId{"control", 1});
    }
    Event end(const Event& start) {
        const auto& p = std::get<TransmissionStart>(start.payload);
        return make(2, p.end_time, EventKind::TransmissionEnd, "txp", "station.A", sequences["txp"] + 1,
                    TransmissionEnd{p.transmission_id}, start.event_id);
    }
    Event result(const Event& end_event, std::uint64_t id, bool success = true) {
        const auto tx_id = std::get<TransmissionEnd>(end_event.payload).transmission_id;
        return make(2, end_event.time, success ? EventKind::ReceptionSuccess : EventKind::ReceptionFailure,
            "rf", "receiver.R", sequences["rf"] + 1,
            ReceptionResult{make_reception_id("rf", id), tx_id, {"receiver.R"}, end_event.time,
                "SYNTHETIC INFRASTRUCTURE TEST; no RF computation"}, end_event.event_id);
    }
    Event no_output(const Event& result_event, SimulationInstant time) {
        const auto& p = std::get<ReceptionResult>(result_event.payload);
        return make(2, time, EventKind::ReceptionNoOutput, "output", "receiver.R", sequences["output"] + 1,
            ReceptionNoOutput{p.receiver_id, p.reception_id, p.transmission_id, "Explicit fixture missing-output disposition"}, result_event.event_id);
    }
    Event resolved(const Event& start, const Event& end_event, SimulationInstant time) {
        const auto& p = std::get<TransmissionStart>(start.payload);
        return make(2, time, EventKind::TransmissionResolved, "txp", "station.A", sequences["txp"] + 1,
            TransmissionResolved{p.transmission_id, p.coverage->resolution_horizon}, end_event.event_id);
    }
};
void streaming_lifetime_and_uniqueness() {
    Stream stream;
    for (std::uint64_t id = 1; id <= 5000; ++id) {
        const auto time = static_cast<std::int64_t>(id * 10);
        const auto start = stream.tx(id, time, time + 1, time + 2); stream.push(start);
        const auto end = stream.end(start); stream.push(end);
        const auto result = stream.result(end, id); stream.push(result);
        auto closure = stream.resolved(start, end, {time + 2});
        if (id == 1) invalid([&] { stream.validator.append(closure); }, "unmaterialized successful");
        stream.push(stream.no_output(result, end.time));
        closure = stream.resolved(start, end, {time + 2}); stream.push(closure);
        const auto stats = stream.validator.stats();
        require(stats.active_transmissions == 0 && stats.active_receptions == 0 && stats.active_receiver_pairs == 0,
                "retired correlation objects accumulated across continuous run");
    }
    const auto stats = stream.validator.stats();
    require(stats.peak_active_transmissions == 1 && stats.peak_active_receptions == 1 &&
            stats.peak_active_receiver_pairs == 1 && stats.peak_typed_anchors <= 5,
            "active correlation/typed-anchor retention unbounded");
    require(stream.validator.retained_history() == 4096 && stream.validator.source_count() == 4,
            "stable producer/history bounds");
    auto duplicate_tx = stream.tx(1, 60000, 60001, 60002);
    invalid([&] { stream.validator.append(duplicate_tx); }, "retired or regressing");
    const auto next = stream.tx(5001, 60000, 60001, 60002); stream.push(next);
    const auto next_end = stream.end(next); stream.push(next_end);
    auto duplicate_rx = stream.result(next_end, 1);
    invalid([&] { stream.validator.append(duplicate_rx); }, "retired or regressing");
    const auto next_rx = stream.result(next_end, 5001, false); stream.push(next_rx);
    stream.push(stream.resolved(next, next_end, {60002}));
    stream.validator.finish();
}
void typed_anchors_outlive_history() {
    Stream stream;
    for (std::uint64_t i = 1; i <= 5000; ++i) stream.push(make(2, {static_cast<std::int64_t>(i)}, EventKind::OutputError,
        "diag", "receiver.R", i, OutputError{{"receiver.R"}, "fixture_probe", "Synthetic bounded-history probe"}));
    const auto start = stream.tx(1, 6000, 20000, 26000); stream.push(start);  // Old station ON anchor.
    for (std::uint64_t i = 5001; i <= 10000; ++i) stream.push(make(2, {static_cast<std::int64_t>(i + 1500)}, EventKind::OutputError,
        "diag", "receiver.R", i, OutputError{{"receiver.R"}, "fixture_probe", "Synthetic bounded-history probe"}));
    const auto end = stream.end(start); stream.push(end);  // Old TX start anchor.
    require(stream.validator.retained_event(start.event_id).has_value(), "old live TX snapshot must survive generic history eviction");
    const auto result = stream.result(end, 1); stream.push(result);
    for (std::uint64_t i = 10001; i <= 15000; ++i) stream.push(make(2, {static_cast<std::int64_t>(i + 10000)}, EventKind::OutputError,
        "diag", "receiver.R", i, OutputError{{"receiver.R"}, "fixture_probe", "Synthetic bounded-history probe"}));
    stream.push(stream.no_output(result, {25001}));  // Old successful RX anchor.
    stream.push(stream.resolved(start, end, {26000}));  // Old TX end anchor.
    require(stream.validator.stats().typed_anchors == 2, "retirement retained stale correlation anchors");
    stream.validator.finish();

    EventValidator maps(ValidationProfile::StreamingV2); maps.append(station(2, {0})); maps.append(receiver(2, {0}));
    const auto init = make(2, {1}, EventKind::SlotMapInit, "local", "station.A", 1,
        SlotMapChange{"station.A", 0, 0, "Synthetic local belief anchor"}, EventId{"control", 1}); maps.append(init);
    const auto reserve = make(2, {2}, EventKind::SlotReserve, "local", "station.A", 2,
        SlotMapChange{"station.A", 0, 17, "Synthetic local belief anchor"}, init.event_id); maps.append(reserve);
    for (std::uint64_t i = 1; i <= 5000; ++i) maps.append(make(2, {static_cast<std::int64_t>(i + 2)}, EventKind::OutputError,
        "diag", "receiver.R", i, OutputError{{"receiver.R"}, "fixture_probe", "Synthetic bounded-history probe"}));
    maps.append(make(2, {6000}, EventKind::SlotRelease, "local", "station.A", 3,
        SlotMapChange{"station.A", 0, 17, "Synthetic release of live local belief"}, reserve.event_id));
    maps.append(make(2, {6001}, EventKind::SlotMapReset, "local", "station.A", 4,
        SlotMapChange{"station.A", 0, 0, "Synthetic reset"}, init.event_id)); maps.finish();
}
void profile_and_closure_guards() {
    Stream stream;
    for (const std::string value : {"tx:txp:0", "tx:txp:01", "tx:txp:+1", "tx:txp:-1", "tx:txp:18446744073709551616", "tx:other:1"}) {
        auto start = stream.tx(1, 10, 20, 30); std::get<TransmissionStart>(start.payload).transmission_id = {value};
        invalid([&] { stream.validator.append(start); }, value == "tx:other:1" ? "producer must match" : "canonical positive");
    }
    auto wrong_domain = stream.tx(1, 10, 20, 30); std::get<TransmissionStart>(wrong_domain.payload).transmission_id = {"rx:txp:1"};
    invalid([&] { stream.validator.append(wrong_domain); }, "domain prefix");
    auto unknown_roster = stream.tx(1, 10, 20, 30);
    std::get<TransmissionStart>(unknown_roster.payload).coverage->candidate_receivers = {{"unregistered"}};
    invalid([&] { stream.validator.append(unknown_roster); }, "registration");
    auto start = stream.tx(1, 10, 20, 30); stream.push(start);
    const auto end = stream.end(start); stream.push(end);
    const auto transmission = std::get<TransmissionEnd>(end.payload).transmission_id;
    auto late_carrier = make(2, {31}, EventKind::RfObservation, "rf", "receiver.R", 1,
        RfObservation{transmission, {"receiver.R"}, {31}, false, std::nullopt, "Explicit late synthetic carrier"}, start.event_id);
    invalid([&] { stream.validator.append(late_carrier); }, "availability horizon");
    auto late_result = stream.result(end, 1); late_result.time = {31};
    std::get<ReceptionResult>(late_result.payload).availability_time = {31};
    invalid([&] { stream.validator.append(late_result); }, "availability horizon");
    auto late_no_rx = make(2, {31}, EventKind::RfNoReception, "rf", "receiver.R", 1,
        RfNoReception{transmission, {"receiver.R"}, NoReceptionReason::NotScheduled, "Explicit late synthetic closure"}, end.event_id);
    invalid([&] { stream.validator.append(late_no_rx); }, "availability horizon");
    auto resolved = stream.resolved(start, end, {30});
    invalid([&] { stream.validator.append(resolved); }, "unresolved receiver obligation");
    invalid([&] { stream.validator.finish(); }, "unresolved RF/output");
    auto no_rx = make(2, end.time, EventKind::RfNoReception, "rf", "receiver.R", 1,
        RfNoReception{std::get<TransmissionEnd>(end.payload).transmission_id, {"receiver.R"}, NoReceptionReason::NotAudible,
            "Explicit synthetic coverage resolution, not RF computation"}, end.event_id);
    stream.push(no_rx); resolved = stream.resolved(start, end, {30}); stream.push(resolved); stream.validator.finish();
    require(make_transmission_id("producer-1", UINT64_MAX).value == "tx:producer-1:18446744073709551615", "max injective counter");
    invalid([] { (void)make_transmission_id("invalid:producer", 1); }, "ASCII");
    invalid([] { (void)make_reception_id("p", 0); }, "positive");
    EventValidator legacy; invalid([&] { legacy.append(station(2, {0})); }, "validation profile");
    EventValidator modern(ValidationProfile::StreamingV2); invalid([&] { modern.append(station(1, {0})); }, "validation profile");
    invalid([] { EventValidator bad(static_cast<ValidationProfile>(99)); }, "profile");
}
class Barrier final : public CausalProducerBarrier {
public:
    bool ready = false; mutable SimulationInstant committed_time{INT64_MIN};
    void validate_advance(SimulationInstant) const override { if (!ready) throw std::invalid_argument("producer is unresolved"); }
    void validate_finish() const override { if (!ready) throw std::invalid_argument("producer is unresolved"); }
    void committed(SimulationInstant time) const override { committed_time = time; }
};
void producer_barrier() {
    Scheduler scheduler; const auto barrier = std::make_shared<Barrier>(); scheduler.attach_producer_barrier(barrier);
    scheduler.enqueue(station(1, {0}));
    invalid([&] { scheduler.advance_watermark({1}, [](const Event&) {}); }, "producer is unresolved");
    require(!scheduler.finished() && scheduler.pending_size() == 1 && scheduler.watermark().units == INT64_MIN,
            "not-ready producer mutated committed prefix");
    barrier->ready = true; scheduler.advance_watermark({1}, [](const Event&) {});
    require(barrier->committed_time.units == 1 && scheduler.has_dispatched({"control", 1}), "producer commit notification");
    invalid([&] { scheduler.attach_producer_barrier(std::make_shared<Barrier>()); }, "before first");
    scheduler.finish([](const Event&) {});
}
}  // namespace
int main() {
    try {
        immediate_phase_guard(); nmea_parent_consistency(); streaming_lifetime_and_uniqueness();
        typed_anchors_outlive_history(); profile_and_closure_guards(); producer_barrier();
        std::cout << "PASS SYNTHETIC INFRASTRUCTURE TEST, NOT AIS RF SIMULATION: streaming retirement/identity/typed causes, "
                  << rejections << " targeted rejections\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << "runtime contract FAIL: " << error.what() << '\n'; return 1; }
}
