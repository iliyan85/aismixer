#include "event.h"
#include "fixture.h"
#include "scheduler.h"
#include "simulation_time.h"

#include <algorithm>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {
using namespace aismixer::lab::runtime;
std::size_t rejection_count = 0;
void require(bool condition, std::string_view message) {
    if (!condition) throw std::runtime_error(std::string(message));
}
template <typename Action> void rejects(Action action, std::string_view fragment) {
    bool caught = false;
    try { action(); }
    catch (const std::invalid_argument& error) {
        require(std::string_view(error.what()).find(fragment) != std::string_view::npos,
                std::string("wrong invalid_argument branch: ") + error.what());
        caught = true;
    }
    require(caught, std::string("expected rejection: ") + std::string(fragment));
    ++rejection_count;
}
template <typename Action> void overflows(Action action) {
    bool caught = false;
    try { action(); } catch (const std::overflow_error&) { caught = true; }
    require(caught, "checked time operation accepted overflow");
    ++rejection_count;
}
Event station(bool on, std::string id, std::int64_t time, std::uint64_t sequence) {
    Event event;
    event.time = {time}; event.kind = on ? EventKind::StationOn : EventKind::StationOff;
    event.source_id = id; event.subject_id = id; event.event_id = {id, sequence};
    event.payload = StationLifecycle{std::move(id)};
    return event;
}
Event receiver(bool on, std::string id, std::int64_t time, std::uint64_t sequence) {
    auto event = station(on, id, time, sequence);
    event.kind = on ? EventKind::ReceiverOn : EventKind::ReceiverOff;
    event.payload = ReceiverLifecycle{{std::move(id)}};
    return event;
}
const Event& find_kind(const Fixture& fixture, EventKind kind, const std::string& subject = {}) {
    const auto found = std::find_if(fixture.events.begin(), fixture.events.end(), [&](const Event& event) {
        return event.kind == kind && (subject.empty() || event.subject_id == subject);
    });
    if (found == fixture.events.end()) throw std::runtime_error("test fixture kind missing");
    return *found;
}
EventValidator before(const Fixture& fixture, const Event& until) {
    EventValidator validator;
    for (const auto& event : fixture.events) {
        if (event.event_id == until.event_id) break;
        validator.append(event);
    }
    return validator;
}
void time_contract() {
    require(kUnitsPerSlot * kSlotsPerFrame == kUnitsPerFrame, "slot/frame integer identity");
    require(kUnitsPerSecond * 60 == kUnitsPerFrame, "second/frame integer identity");
    require(from_nanoseconds(1000000000).units == kUnitsPerSecond, "external second conversion");
    require(from_nanoseconds(1).units == 0 && from_nanoseconds(-1).units == 0, "nearest small external unit");
    require(from_nanoseconds(51).units == 1 && from_nanoseconds(-51).units == -1, "nearest external unit threshold");
    require(to_nanoseconds({384}) == 39063 && to_nanoseconds({-384}) == -39063, "exact half ties away from zero");
    require(to_nanoseconds(from_seconds(5)) == 5000000000, "integer seconds roundtrip");
    for (std::int64_t minute : {-1000000, -1, 0, 1, 1000000}) {
        for (std::uint16_t slot = 0; slot < kSlotsPerFrame; ++slot) {
            for (std::int64_t fraction : {std::int64_t{0}, kUnitsPerSlot - 1}) {
                const UtcSlot input{minute, slot, fraction};
                const auto instant = compose_utc(input, {59, kUnitsPerSecond - 1});
                const auto output = decompose_utc(instant, {59, kUnitsPerSecond - 1});
                require(output.utc_minute == minute && output.slot == slot && output.units_into_slot == fraction,
                        "all-slot arbitrary-anchor decomposition inverse");
            }
        }
    }
    const auto just_before = decompose_utc({-1}, {0, 0});
    require(just_before.utc_minute == -1 && just_before.slot == 2249 && just_before.units_into_slot == kUnitsPerSlot - 1,
            "negative UTC floor normalization");
    const auto boundary = decompose_utc({1}, {59, kUnitsPerSecond - 1});
    require(boundary.utc_minute == 1 && boundary.slot == 0 && boundary.units_into_slot == 0,
            "arbitrary anchor crosses actual UTC minute");
    require(decompose_utc({0}, {17, 0}).slot == 637, "simulation zero does not reset UTC AIS frame");
    for (auto units : {std::numeric_limits<std::int64_t>::min(), std::numeric_limits<std::int64_t>::max()}) {
        require(compose_utc(decompose_utc({units}, {0, 0}), {0, 0}).units == units, "full signed time range UTC roundtrip");
        require(compose_utc(decompose_utc({0}, {units, 0}), {units, 0}).units == 0, "extreme POSIX anchor roundtrip");
    }
    const UtcAnchor cancellation_anchor{INT64_MIN, 1};
    const auto cancellation = decompose_utc({-1}, cancellation_anchor);
    require(compose_utc(cancellation, {INT64_MIN, 0}).units == 0 &&
            compose_utc(cancellation, cancellation_anchor).units == -1,
            "normalize subsecond carry before checking extreme-anchor second addition");
    require(difference_units({-7}, {-9}) == 2 && add_units({-9}, 2).units == -7, "checked arithmetic exact signed values");
    overflows([] { (void)add_units({INT64_MAX}, 1); });
    overflows([] { (void)add_units({INT64_MIN}, -1); });
    overflows([] { (void)difference_units({INT64_MAX}, {-1}); });
    overflows([] { (void)difference_units({INT64_MIN}, {1}); });
    overflows([] { (void)from_seconds(INT64_MAX); });
    overflows([] { (void)from_seconds(INT64_MIN); });
    overflows([] { (void)to_nanoseconds({INT64_MAX}); });
    overflows([] { (void)to_nanoseconds({INT64_MIN}); });
    overflows([] { (void)decompose_utc(from_seconds(1), {INT64_MAX, 0}); });
    overflows([] { (void)compose_utc({INT64_MAX, 0, 0}, {0, 0}); });
    rejects([] { validate_utc_anchor({0, -1}); }, "normalized");
    rejects([] { validate_utc_anchor({0, kUnitsPerSecond}); }, "normalized");
    rejects([] { (void)compose_utc({0, 2250, 0}, {}); }, "normalized");
    rejects([] { (void)compose_utc({0, 0, -1}, {}); }, "normalized");
    rejects([] { (void)compose_utc({0, 0, kUnitsPerSlot}, {}); }, "normalized");
}
void fixture_contract() {
    const auto fixture = make_synthetic_fixture();
    require(fixture.provenance.find("NOT AIS RADIO SIMULATION") != std::string::npos, "fixture truthful label");
    require(fixture.events.size() == 17 && fixture.receivers.size() == 3, "fixed fixture structure");
    EventValidator validator;
    std::size_t tx = 0, success = 0, failure = 0, output = 0;
    std::string bytes;
    for (const auto& event : fixture.events) {
        validator.append(event);
        if (event.kind == EventKind::TransmissionStart) ++tx;
        if (event.kind == EventKind::ReceptionSuccess) ++success;
        if (event.kind == EventKind::ReceptionFailure) ++failure;
        if (event.kind == EventKind::ReceiverNmea) {
            ++output;
            const auto& nmea = std::get<ReceiverNmea>(event.payload);
            require(nmea.receiver_id.value != "receiver.R3", "failed reception produced NMEA");
            bytes += nmea.sentences.at(0);
        }
    }
    validator.finish();
    const std::string golden = "!AIVDM,1,1,,A,1Amg=5EmQso>f;LFGd5DlSoo7D<Q,0*2B\r\n";
    require(tx == 1 && success == 2 && failure == 1 && output == 2, "physical TX versus independent receiver outcomes");
    require(bytes == golden + golden && bytes.size() == 98, "stored independent B3 NMEA exact CRLF bytes");
    rejects([&] { validator.append(fixture.events.back()); }, "sealed");
    rejects([&] { validator.finish(); }, "sealed");
}
void event_shape_and_causality() {
    const auto fixture = make_synthetic_fixture();
    auto sample = find_kind(fixture, EventKind::ReceiverNmea);
    for (const std::string identity : {std::string{}, std::string(65, 'a'), std::string("with space"), std::string("\xc3\xa9")})
        rejects([&] { validate_identity(identity, "test identity"); }, "identity");
    { auto value = sample; value.schema_version = 3; rejects([&] { validate_event(value); }, "schema"); }
    { auto value = sample; value.kind = static_cast<EventKind>(99); rejects([&] { validate_event(value); }, "unknown event kind"); }
    { auto value = sample; value.event_id.source_id = "other"; rejects([&] { validate_event(value); }, "match envelope"); }
    { auto value = sample; value.event_id.sequence = 0; rejects([&] { validate_event(value); }, "positive"); }
    { auto value = sample; value.subject_id = "other"; rejects([&] { validate_event(value); }, "subject"); }
    { auto value = sample; value.payload = StationLifecycle{"station.A"}; rejects([&] { validate_event(value); }, "typed payload"); }
    { auto value = sample; value.causal_ref = value.event_id; rejects([&] { validate_event(value); }, "itself"); }
    auto good_nmea = std::get<ReceiverNmea>(sample.payload);
    { auto value = good_nmea; value.sentences.clear(); rejects([&] { validate_receiver_nmea(value); }, "fragment count"); }
    { auto value = good_nmea; value.sentences[0].pop_back(); rejects([&] { validate_receiver_nmea(value); }, "CRLF"); }
    { auto value = good_nmea; value.sentences[0][20] ^= 1; rejects([&] { validate_receiver_nmea(value); }, "checksum"); }
    { auto value = good_nmea; value.sentences.assign(10, value.sentences.front()); rejects([&] { validate_receiver_nmea(value); }, "fragment count"); }
    {
        auto value = find_kind(fixture, EventKind::TransmissionEnd); auto validator = before(fixture, value);
        value.causal_ref.reset(); rejects([&] { validator.append(value); }, "causal predecessor");
        validator.append(find_kind(fixture, EventKind::TransmissionEnd));  // Rejection consumed no sequence/state.
    }
    {
        auto value = find_kind(fixture, EventKind::ReceptionSuccess); auto validator = before(fixture, value);
        value.causal_ref = EventId{"missing.source", 1}; rejects([&] { validator.append(value); }, "missing or outside");
        value = find_kind(fixture, EventKind::ReceptionSuccess); std::get<ReceptionResult>(value.payload).availability_time.units += 1;
        rejects([&] { validator.append(value); }, "availability time");
    }
    {
        auto value = find_kind(fixture, EventKind::ReceiverNmea); auto validator = before(fixture, value);
        value.causal_ref = EventId{"station.A", 3}; rejects([&] { validator.append(value); }, "causal predecessor");
        value = find_kind(fixture, EventKind::ReceiverNmea); std::get<ReceiverNmea>(value.payload).reception_id = {"fixture.rx.3"};
        rejects([&] { validator.append(value); }, "successful matching reception");
    }
    {
        EventValidator validator; const auto on = station(true, "station.same", 0, 1); validator.append(on);
        auto same = station(false, "station.same", 0, 2); same.causal_ref = on.event_id;
        rejects([&] { validator.append(same); }, "strictly earlier simultaneous phase");
    }
    {
        EventValidator validator; validator.append(receiver(true, "receiver.off", 0, 1));
        validator.append(receiver(false, "receiver.off", 1, 2));
        Event output{1, {2}, EventKind::OutputError, "receiver.off", "receiver.off", {"receiver.off", 3},
                     std::nullopt, OutputError{{"receiver.off"}, "fixture_only", "Explicit test error"}};
        rejects([&] { validator.append(output); }, "not ON");
    }
    {
        auto start = find_kind(fixture, EventKind::TransmissionStart); auto validator = before(fixture, start);
        auto future = start; std::get<TransmissionStart>(future.payload).end_time = future.time;
        rejects([&] { validator.append(future); }, "strictly later");
        validator.append(start); rejects([&] { validator.finish(); }, "unfinished transmission");
        auto overlapping = start; overlapping.time.units += 1; overlapping.event_id.sequence = 3;
        std::get<TransmissionStart>(overlapping.payload).transmission_id = {"another.tx"};
        rejects([&] { validator.append(overlapping); }, "overlapping");
    }
}
void retained_bounds() {
    EventValidator validator;
    EventId ancient;
    for (std::uint64_t sequence = 1; sequence <= 5000; ++sequence) {
        auto event = station(sequence % 2 == 1, "station.history", static_cast<std::int64_t>(sequence), sequence);
        if (sequence == 1) ancient = event.event_id;
        validator.append(event);
    }
    require(validator.retained_history() == 4096 && validator.source_count() == 1, "history bounded independently of stream length");
    auto next = station(true, "station.history", 5001, 5001);
    next.causal_ref = ancient; rejects([&] { validator.append(next); }, "outside retained");
    next.causal_ref.reset(); next.event_id.sequence = 1; rejects([&] { validator.append(next); }, "regressing source-local");
    next.event_id.sequence = 5001; validator.append(next);
    EventValidator copy = validator;
    copy.append(station(false, "station.history", 5002, 5002));
    validator.append(station(false, "station.history", 5002, 5002));
    copy.finish(); validator.finish();
    EventValidator sources;
    for (std::size_t index = 0; index < kMaxEventSources; ++index)
        sources.append(station(true, "source." + std::to_string(index), static_cast<std::int64_t>(index), 1));
    rejects([&] { sources.append(station(true, "source.overflow", 1025, 1)); }, "source catalog capacity");
}
void typed_observation_boundary() {
    EventValidator validator;
    const std::string s = "A.station.typed", r = "receiver.typed";
    validator.append(station(true, s, 0, 1));
    validator.append(receiver(true, r, 1, 1));
    std::uint64_t sequence = 2;
    for (const auto kind : {EventKind::SlotMapInit, EventKind::SlotReserve, EventKind::SlotRelease, EventKind::SlotMapReset}) {
        Event event{1, {static_cast<std::int64_t>(sequence)}, kind, s, s, {s, sequence}, std::nullopt,
                    SlotMapChange{s, 1, 2249, "Labelled synthetic local observation; no TDMA algorithm"}};
        validate_event(event); validator.append(event); ++sequence;
    }
    Event decision{1, {6}, EventKind::TdmaDecision, s, s, {s, 6}, std::nullopt,
                   TdmaDecision{s, "Synthetic typed-schema test; not a computed TDMA decision"}};
    validator.append(decision);
    Event start{1, {10}, EventKind::TransmissionStart, s, s, {s, 7}, EventId{s, 1},
                TransmissionStart{{"typed.tx"}, s, 0, {20}, "fixture.payload"}};
    validator.append(start);
    Event observation{1, {11}, EventKind::RfObservation, r, r, {r, 2}, start.event_id,
                      RfObservation{{"typed.tx"}, {r}, {11}, false, s, "Synthetic undecoded carrier tuple; no propagation"}};
    validator.append(observation);
    Event result{1, {15}, EventKind::ReceptionSuccess, r, r, {r, 3}, observation.event_id,
                 ReceptionResult{{"typed.rx"}, {"typed.tx"}, {r}, {15}, "Scripted outcome"}};
    rejects([&] { validator.append(result); }, "before complete transmission");
    Event end{1, {20}, EventKind::TransmissionEnd, s, s, {s, 8}, start.event_id, TransmissionEnd{{"typed.tx"}}};
    validator.append(end);
    auto same_phase = observation; same_phase.time = {20}; same_phase.event_id.sequence = 3;
    same_phase.causal_ref = end.event_id; std::get<RfObservation>(same_phase.payload).availability_time = {20};
    rejects([&] { validator.append(same_phase); }, "strictly earlier simultaneous phase");
    result.time = {20}; std::get<ReceptionResult>(result.payload).availability_time = {20};
    validator.append(result);
    require(!std::get<RfObservation>(observation.payload).carrier_detected, "observation must not imply decoding");
    const auto nmea = std::get<ReceiverNmea>(find_kind(make_synthetic_fixture(), EventKind::ReceiverNmea).payload).sentences;
    Event output{1, {20}, EventKind::ReceiverNmea, r, r, {r, 4}, result.event_id,
                 ReceiverNmea{{r}, {"typed.rx"}, {"typed.tx"}, nmea}};
    validator.append(output);
    auto duplicate = output; duplicate.time = {21}; duplicate.event_id.sequence = 5;
    rejects([&] { validator.append(duplicate); }, "unique successful");
    validator.finish();
    {
        auto bad = decision; std::get<TdmaDecision>(bad.payload).decision = std::string("\xc0\x80");
        rejects([&] { validate_event(bad); }, "UTF-8");
        std::get<TdmaDecision>(bad.payload).decision = std::string("\xed\xa0\x80");
        rejects([&] { validate_event(bad); }, "UTF-8");
        std::get<TdmaDecision>(bad.payload).decision = std::string(4097, 'x');
        rejects([&] { validate_event(bad); }, "length");
    }
}
void correlation_capacity() {
    // Exhaustion is explicit rather than forgetting old logical IDs and later
    // accepting reuse. This is a bounded R0 run, not unlimited TX storage.
    const std::string source = "fixture.capacity";
    EventValidator transmissions;
    auto on = station(true, "station.capacity", 0, 1);
    on.source_id = source; on.event_id.source_id = source;
    transmissions.append(on);
    std::uint64_t sequence = 1;
    for (std::size_t index = 0; index < kLifecycleCapacity; ++index) {
        const auto time = static_cast<std::int64_t>(index * 2 + 1);
        const TransmissionId tx{"capacity.tx." + std::to_string(index)};
        Event start{1, {time}, EventKind::TransmissionStart, source, "station.capacity", {source, ++sequence}, std::nullopt,
                    TransmissionStart{tx, "station.capacity", 0, {time + 1}, "fixture.payload"}};
        transmissions.append(start);
        transmissions.append({1, {time + 1}, EventKind::TransmissionEnd, source, "station.capacity", {source, ++sequence},
                              start.event_id, TransmissionEnd{tx}});
    }
    Event extra{1, {10000}, EventKind::TransmissionStart, source, "station.capacity", {source, sequence + 1}, std::nullopt,
                TransmissionStart{{"capacity.tx.overflow"}, "station.capacity", 0, {10001}, "fixture.payload"}};
    rejects([&] { transmissions.append(extra); }, "unique transmission identities");
    std::get<TransmissionStart>(extra.payload).transmission_id = {"capacity.tx.0"};
    rejects([&] { transmissions.append(extra); }, "duplicate logical transmission");
    transmissions.finish();

    EventValidator receptions;
    receptions.append(on); sequence = 1;
    for (std::size_t index = 0; index < kLifecycleCapacity; ++index) {
        auto event = receiver(true, "capacity.receiver." + std::to_string(index), static_cast<std::int64_t>(index + 1), ++sequence);
        event.source_id = source; event.event_id.source_id = source;
        receptions.append(event);
    }
    auto extra_receiver = receiver(true, "capacity.receiver.overflow", 4500, sequence + 1);
    extra_receiver.source_id = source; extra_receiver.event_id.source_id = source;
    rejects([&] { receptions.append(extra_receiver); }, "lifecycle identity catalog capacity");
    Event start{1, {5000}, EventKind::TransmissionStart, source, "station.capacity", {source, ++sequence}, std::nullopt,
                TransmissionStart{{"capacity.broadcast"}, "station.capacity", 0, {5001}, "fixture.payload"}};
    receptions.append(start);
    Event end{1, {5001}, EventKind::TransmissionEnd, source, "station.capacity", {source, ++sequence}, start.event_id,
              TransmissionEnd{{"capacity.broadcast"}}};
    receptions.append(end);
    for (std::size_t index = 0; index < kLifecycleCapacity; ++index) {
        const ReceiverId receiver_id{"capacity.receiver." + std::to_string(index)};
        receptions.append({1, {5001}, EventKind::ReceptionFailure, source, receiver_id.value, {source, ++sequence}, end.event_id,
            ReceptionResult{{"capacity.rx." + std::to_string(index)}, {"capacity.broadcast"}, receiver_id, {5001}, "Scripted failure"}});
    }
    start.time = {5002}; start.event_id.sequence = ++sequence;
    auto& next_tx = std::get<TransmissionStart>(start.payload); next_tx.transmission_id = {"capacity.broadcast.2"}; next_tx.end_time = {5003};
    receptions.append(start);
    end.time = {5003}; end.event_id.sequence = ++sequence; end.causal_ref = start.event_id;
    std::get<TransmissionEnd>(end.payload).transmission_id = next_tx.transmission_id;
    receptions.append(end);
    Event extra_result{1, {5003}, EventKind::ReceptionFailure, source, "capacity.receiver.0", {source, sequence + 1}, end.event_id,
        ReceptionResult{{"capacity.rx.overflow"}, next_tx.transmission_id, {"capacity.receiver.0"}, {5003}, "Scripted failure"}};
    rejects([&] { receptions.append(extra_result); }, "unique reception identities");
    receptions.finish();
    require(receptions.retained_history() == kCausalHistoryCapacity, "large correlation run retains bounded causal history");
}
void scheduler_contract() {
    const auto fixture = make_synthetic_fixture();
    Scheduler queue(32, {0});
    // Source-local order is preserved; source population submission order is
    // deliberately reversed to demonstrate global canonical ordering.
    std::vector<std::string> ids{"station.A", "receiver.R3", "receiver.R2", "receiver.R1"};
    for (const auto& id : ids) for (const auto& event : fixture.events) if (event.source_id == id) queue.enqueue(event);
    std::vector<EventId> observed;
    queue.advance_watermark({0}, [&](const Event&) { throw std::runtime_error("half-open zero incorrectly emitted event"); });
    require(queue.pending_size() == fixture.events.size(), "half-open equality remains uncommitted");
    queue.advance_watermark(from_seconds(3), [&](const Event& event) { observed.push_back(event.event_id); });
    require(observed.size() == 6, "watermark excludes simultaneous TX exactly at boundary");
    rejects([&] { queue.enqueue(station(true, "late", 0, 1)); }, "watermark");
    rejects([&] { queue.advance_watermark(from_seconds(2), [](const Event&) {}); }, "regress");
    require(!queue.finished(), "rejected regressing watermark must not seal queue");
    queue.finish([&](const Event& event) { observed.push_back(event.event_id); });
    require(queue.finished() && queue.pending_size() == 0 && observed.size() == fixture.events.size(), "terminal drain state");
    for (std::size_t index = 0; index < observed.size(); ++index) require(observed[index] == fixture.events[index].event_id, "canonical order independent of input source order");
    rejects([&] { queue.enqueue(station(true, "after.finish", INT64_MAX, 1)); }, "sealed");
    Scheduler terminal(1, {INT64_MAX}); terminal.enqueue(station(true, "terminal", INT64_MAX, 1));
    std::size_t final_count = 0; terminal.finish([&](const Event&) { ++final_count; });
    require(final_count == 1 && terminal.finished(), "terminal flush needs no overflowing fake timestamp");
    rejects([] { Scheduler invalid(0); }, "capacity");
    rejects([] { Scheduler invalid(65537); }, "capacity");
    Scheduler bounded(1); bounded.enqueue(station(true, "bounded", 0, 1));
    rejects([&] { bounded.enqueue(station(true, "other", 1, 1)); }, "capacity");
    Scheduler sequence; sequence.enqueue(station(true, "sequence", 1, 1));
    rejects([&] { sequence.enqueue(station(false, "sequence", 2, 1)); }, "sequence");
    rejects([&] { sequence.enqueue(station(false, "sequence", 0, 2)); }, "regresses");
    Scheduler dynamic;
    dynamic.enqueue(station(true, "dynamic", 0, 1));
    std::vector<std::int64_t> times;
    dynamic.advance_watermark_batches({10}, [&](const std::vector<Event>& group) {
        times.push_back(group.front().time.units);
        if (group.front().time.units == 0) {
            rejects([&] { dynamic.enqueue(station(false, "dynamic", 0, 2)); }, "complete dispatched");
            dynamic.enqueue(station(false, "dynamic", 5, 2));
        }
    });
    require(times == std::vector<std::int64_t>{0, 5} && dynamic.watermark().units == 10, "dynamic feedback within target interval precedes commit");
    dynamic.finish([](const Event&) {});
    Scheduler failed;
    failed.enqueue(station(true, "failed", 0, 1));
    bool io_error = false;
    try { failed.finish([](const Event&) { throw std::runtime_error("consumer failed"); }); }
    catch (const std::runtime_error&) { io_error = true; }
    require(io_error && failed.finished(), "consumer failure terminally seals partially consumed scheduler");
}
void simultaneous_transmissions() {
    Scheduler scheduler;
    for (const std::string id : {"station.B", "station.A"}) {
        scheduler.enqueue(station(true, id, 0, 1));
        Event start{1, {10}, EventKind::TransmissionStart, id, id, {id, 2}, EventId{id, 1},
                    TransmissionStart{{"tx." + id}, id, 0, {20}, "fixture.payload"}};
        scheduler.enqueue(start);
        scheduler.enqueue({1, {20}, EventKind::TransmissionEnd, id, id, {id, 3}, start.event_id, TransmissionEnd{{"tx." + id}}});
    }
    std::size_t start_groups = 0;
    scheduler.finish_batches([&](const std::vector<Event>& group) {
        if (group.front().kind == EventKind::TransmissionStart) {
            require(group.size() == 2 && group[0].source_id == "station.A" && group[1].source_id == "station.B",
                    "simultaneous TX starts must form a complete coherent phase");
            ++start_groups;
        }
    });
    require(start_groups == 1, "exactly one simultaneous TX phase");
}
}  // namespace

int main() {
    try {
        time_contract(); fixture_contract(); event_shape_and_causality();
        retained_bounds(); typed_observation_boundary(); correlation_capacity(); scheduler_contract(); simultaneous_transmissions();
        std::cout << "runtime core PASS: exact time, canonical causality, bounded state, coherent phases; "
                  << rejection_count << " branch-specific rejections\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "runtime core FAIL: " << error.what() << '\n';
        return 1;
    }
}
