#include "rf_contract.h"
#include "scheduler.h"
#include "journal.h"

#include <algorithm>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string_view>
#include <type_traits>
#include <utility>

namespace r = aismixer::lab::runtime;
namespace {
template<class T, class = void> struct HasGlobalEvent : std::false_type {};
template<class T> struct HasGlobalEvent<T, std::void_t<decltype(std::declval<const T&>().event())>> : std::true_type {};
void require(bool value, const char* reason) { if (!value) throw std::runtime_error(reason); }
template<class F> void invalid(F action, std::string_view diagnostic = {}) {
    try { action(); } catch (const std::invalid_argument& error) {
        require(std::string_view(error.what()).find(diagnostic) != std::string_view::npos,
                "RF boundary rejected through an unexpected validation branch");
        return;
    }
    throw std::runtime_error("invalid RF boundary contract accepted");
}
r::Event event(r::SimulationInstant time, r::EventKind kind, std::string source,
               std::uint64_t sequence, std::string subject, r::EventPayload payload,
               std::optional<r::EventId> cause = std::nullopt) {
    r::Event result;
    result.schema_version = 2; result.time = time; result.kind = kind;
    result.source_id = source; result.subject_id = std::move(subject);
    result.event_id = {std::move(source), sequence}; result.causal_ref = std::move(cause);
    result.payload = std::move(payload); return result;
}
std::shared_ptr<const r::ReceiverRegistry> registry() {
    return std::make_shared<r::ReceiverRegistry>(std::vector<std::string>{"station.A", "station.B"},
        std::vector<r::ReceiverOwnership>{
            {{"ground"}, r::ReceiverRole::PassiveTerrestrial, std::nullopt, true},
            {{"off"}, r::ReceiverRole::TransceiverRadio, "station.A", false},
            {{"radio.A"}, r::ReceiverRole::TransceiverRadio, "station.A", true},
            {{"radio.B"}, r::ReceiverRole::TransceiverRadio, "station.B", true}});
}
void lifecycle(r::Scheduler& scheduler, r::SimulationInstant time) {
    scheduler.enqueue(event(time, r::EventKind::StationOn, "life", 1, "station.A", r::StationLifecycle{"station.A"}));
    scheduler.enqueue(event(time, r::EventKind::StationOn, "life", 2, "station.B", r::StationLifecycle{"station.B"}));
    scheduler.enqueue(event(time, r::EventKind::ReceiverOn, "life", 3, "ground", r::ReceiverLifecycle{{"ground"}}));
    scheduler.enqueue(event(time, r::EventKind::ReceiverOn, "life", 4, "radio.A", r::ReceiverLifecycle{{"radio.A"}}));
    scheduler.enqueue(event(time, r::EventKind::ReceiverOn, "life", 5, "radio.B", r::ReceiverLifecycle{{"radio.B"}}));
}
struct Tx {
    r::RfTransmissionWork work;
    r::TransmissionCoverage coverage;
    r::Event start, end, close;
};
Tx transmission(const std::string& producer, const std::string& station,
                std::int64_t start, std::int64_t end, std::int64_t horizon,
                std::vector<r::ReceiverId> receivers) {
    Tx tx;
    tx.work = {r::make_transmission_id(producer, 1), station, 0, {start}, {end}, "payload.literal"};
    tx.coverage = {std::move(receivers), {horizon}};
    tx.start = event({start}, r::EventKind::TransmissionStart, producer, 1, station,
        r::TransmissionStart{tx.work.transmission_id, station, 0, {end}, "payload.literal", tx.coverage});
    tx.end = event({end}, r::EventKind::TransmissionEnd, producer, 2, station,
        r::TransmissionEnd{tx.work.transmission_id}, tx.start.event_id);
    tx.close = event({horizon}, r::EventKind::TransmissionResolved, "closure."+producer, 1, station,
        r::TransmissionResolved{tx.work.transmission_id, {horizon}}, tx.end.event_id);
    return tx;
}
void register_tx(r::RfCausalBoundary& boundary, r::Scheduler& scheduler, const Tx& tx) {
    scheduler.enqueue(tx.start);
    boundary.register_transmission(tx.work, tx.coverage, tx.start.event_id);
}
void publish_end(r::RfCausalBoundary& boundary, r::Scheduler& scheduler, const Tx& tx) {
    scheduler.enqueue(tx.end);
    boundary.record_transmission_end(tx.work.transmission_id, tx.end.event_id);
}
r::RfDiscovery discovery(const Tx& tx, const char* receiver, std::int64_t time,
                         r::RfPairDisposition disposition, const char* semantic = "outcome") {
    r::RfDiscovery result{semantic, tx.work.transmission_id, {receiver}, {time}, disposition, std::nullopt};
    if (disposition == r::RfPairDisposition::Decoded) result.decoded_payload_reference = tx.work.payload_reference;
    return result;
}
void append_bytes(std::vector<std::uint8_t>& bytes, const r::Event& value) {
    const auto encoded = r::encode_event(value);
    const auto count = static_cast<std::uint32_t>(encoded.size());
    for (unsigned shift = 0; shift < 32; shift += 8) bytes.push_back(static_cast<std::uint8_t>(count >> shift));
    bytes.insert(bytes.end(), encoded.begin(), encoded.end());
}

std::vector<std::uint8_t> cross_slice(bool small_steps, bool reverse_discovery) {
    r::Scheduler scheduler(256, {std::numeric_limits<std::int64_t>::min()}, r::ValidationProfile::StreamingV2);
    const auto owners = registry();
    r::RfCausalBoundary boundary("rf", owners);
    boundary.attach(scheduler);
    lifecycle(scheduler, {-10});
    const auto tx = transmission("shipA", "station.A", 0, 100, 120, {{"ground"}, {"radio.A"}});
    register_tx(boundary, scheduler, tx);
    r::LocalObservationRouter router(owners);
    auto carrier = discovery(tx, "radio.A", 25, r::RfPairDisposition::Carrier, "carrier.25");
    boundary.discover(carrier);
    boundary.resolve_before({50});
    invalid([&] { scheduler.advance_watermark({50}, [](const auto&) {}); }, "materialized");
    require(!scheduler.finished(), "not-ready RF barrier poisoned scheduler");
    const auto early = boundary.materialize_before({50}, scheduler);
    require(early.size() == 1 && early.front().event().event_id.sequence == 1,
            "RF producer failed to assign first release sequence");
    invalid([&] { (void)router.route(early.front(), "station.A", {25}); }, "not been validated");
    std::vector<std::uint8_t> bytes;
    bool decided = false;
    const auto consume = [&](const r::Event& value) {
        append_bytes(bytes, value);
        if (value.event_id == early.front().event().event_id) {
            const auto local = router.route(early.front(), "station.A", value.time);
            require(local.station_id() == "station.A", "own-radio router changed station");
            require(local.receiver_id().value == "radio.A" && local.availability_time() == value.time &&
                    local.disposition() == r::DecodeDisposition::Undecoded && !local.decoded_payload_reference(),
                    "undecoded local radio observation exposed decoded payload identity");
            invalid([&] { (void)router.route(early.front(), "station.B", value.time); }, "own radio");
            invalid([&] { (void)router.route(early.front(), "station.A", {24}); }, "precedes");
            scheduler.enqueue(event(value.time, r::EventKind::TdmaDecision, "decision", 1, "station.A",
                r::TdmaDecision{"station.A", "SYNTHETIC INFRASTRUCTURE TEST: local observation consumed"},
                local.causal_event_id()));
            decided = true;
        }
    };
    if (small_steps) scheduler.advance_watermark({20}, consume);
    scheduler.advance_watermark({50}, consume);
    require(decided && boundary.event_commit_watermark().units == 50,
            "same-minute owned observation did not drive later causal decision");
    require(boundary.context(tx.work.transmission_id).start.units == 0,
            "registered in-flight context was lost after start crossed watermark");
    invalid([&] { boundary.discover(discovery(tx, "radio.A", 26, r::RfPairDisposition::Carrier)); }, "late");
    auto retro = tx.work; retro.transmission_id = r::make_transmission_id("shipA", 2);
    retro.start = {40}; retro.end = {200};
    invalid([&] { boundary.register_transmission(retro, {{{"ground"}}, {220}}, {"shipA", 3}); }, "retroactive");
    publish_end(boundary, scheduler, tx);
    std::vector<r::RfDiscovery> finals{
        discovery(tx, "ground", 110, r::RfPairDisposition::Failed),
        discovery(tx, "radio.A", 110, r::RfPairDisposition::Failed)};
    if (reverse_discovery) std::reverse(finals.begin(), finals.end());
    for (auto value : finals) boundary.discover(std::move(value));
    boundary.resolve_before({121});
    const auto late = boundary.materialize_before({121}, scheduler);
    require(late.size() == 2 && late[0].event().event_id.sequence == 2 &&
            late[1].event().event_id.sequence == 3,
            "RF release sequences followed discovery order rather than canonical order");
    scheduler.enqueue(tx.close);
    if (small_steps) scheduler.advance_watermark({101}, consume);
    scheduler.advance_watermark({121}, consume);
    invalid([&] { (void)router.route(late.front(), "station.A", {111}); }, "own radio");
    r::LocalObservationRouter different_registry(registry());
    invalid([&] { (void)different_registry.route(early.front(), "station.A", {111}); }, "different");
    boundary.retire_transmission(tx.work.transmission_id);
    require(boundary.stats().active_transmissions == 0 && boundary.stats().candidate_pairs == 0 &&
            boundary.stats().producer_identities == 1, "RF retirement lost bounded producer uniqueness proof");
    auto reused = tx.work; reused.start = {200}; reused.end = {210};
    invalid([&] { boundary.register_transmission(reused, {{{"ground"}}, {220}}, {"shipA", 3}); }, "reused");
    boundary.resolve_terminal();
    require(boundary.materialize_all(scheduler).empty(), "retired RF context left hidden observations");
    scheduler.finish(consume);
    return bytes;
}

std::vector<std::uint8_t> simultaneous(bool reversed) {
    r::Scheduler scheduler(256, {std::numeric_limits<std::int64_t>::min()}, r::ValidationProfile::StreamingV2);
    r::RfCausalBoundary boundary("rf", registry()); boundary.attach(scheduler);
    lifecycle(scheduler, {-10});
    const auto a = transmission("shipA", "station.A", 0, 10, 11, {{"ground"}});
    const auto b = transmission("shipB", "station.B", 0, 10, 11, {{"ground"}});
    register_tx(boundary, scheduler, a); register_tx(boundary, scheduler, b);
    publish_end(boundary, scheduler, a); publish_end(boundary, scheduler, b);
    auto first = discovery(a, "ground", 10, r::RfPairDisposition::Failed);
    auto second = discovery(b, "ground", 10, r::RfPairDisposition::Failed);
    if (reversed) std::swap(first, second);
    boundary.discover(first); boundary.discover(second); boundary.resolve_terminal();
    const auto outcomes = boundary.materialize_all(scheduler);
    require(outcomes.size() == 2 && outcomes[0].event().kind == r::EventKind::ReceptionFailure &&
            outcomes[1].event().kind == r::EventKind::ReceptionFailure,
            "simultaneous same-channel TXs collapsed receiver obligations");
    scheduler.enqueue(a.close); scheduler.enqueue(b.close);
    std::vector<std::uint8_t> bytes;
    scheduler.finish([&](const auto& value) { append_bytes(bytes, value); });
    return bytes;
}

void explicit_closures_and_limits() {
    r::Scheduler scheduler(256, {std::numeric_limits<std::int64_t>::min()}, r::ValidationProfile::StreamingV2);
    const auto owners = registry(); r::RfCausalBoundary boundary("rf", owners, 2); boundary.attach(scheduler);
    lifecycle(scheduler, {-10});
    const auto tx = transmission("shipA", "station.A", 0, 10, 12, {{"ground"}, {"radio.A"}});
    register_tx(boundary, scheduler, tx); publish_end(boundary, scheduler, tx);
    invalid([&] { boundary.resolve_before({13}); }, "unresolved");
    invalid([&] { boundary.resolve_terminal(); }, "unresolved");
    invalid([&] { scheduler.finish([](const auto&) {}); }, "terminal");
    require(!scheduler.finished(), "terminal not-ready RF barrier sealed valid prefix");
    invalid([&] { boundary.discover(discovery(tx, "radio.A", 0, r::RfPairDisposition::Carrier)); }, "strictly positive");
    invalid([&] { boundary.discover(discovery(tx, "ground", 9, r::RfPairDisposition::Decoded)); }, "precedes");
    invalid([&] { boundary.discover(discovery(tx, "ground", 13, r::RfPairDisposition::Failed)); }, "beyond");
    boundary.discover(discovery(tx, "ground", 10, r::RfPairDisposition::NotAudible));
    boundary.discover(discovery(tx, "radio.A", 10, r::RfPairDisposition::NotScheduled));
    invalid([&] { boundary.discover(discovery(tx, "radio.A", 11, r::RfPairDisposition::Carrier, "extra")); }, "capacity");
    invalid([&] { boundary.discover(discovery(tx, "ground", 11, r::RfPairDisposition::Failed)); }, "duplicate");
    boundary.resolve_terminal(); const auto outcomes = boundary.materialize_all(scheduler);
    scheduler.enqueue(tx.close); scheduler.finish([](const auto&) {});
    require(outcomes.size() == 2 && outcomes[0].event().kind == r::EventKind::RfNoReception &&
            outcomes[1].event().kind == r::EventKind::RfNoReception,
            "legitimate zero-decode TX did not close finite obligations");
    r::LocalObservationRouter router(owners);
    invalid([&] { (void)router.route(outcomes.back(), "station.A", {12}); }, "not station-local");

    r::Scheduler disabled_scheduler(32, {std::numeric_limits<std::int64_t>::min()}, r::ValidationProfile::StreamingV2);
    r::RfCausalBoundary disabled("rf", owners); disabled.attach(disabled_scheduler);
    const auto off_tx = transmission("shipA", "station.A", 0, 10, 12, {{"off"}});
    invalid([&] { disabled.register_transmission(off_tx.work, off_tx.coverage, off_tx.start.event_id); }, "disabled");
    invalid([&] { r::ReceiverRegistry bad({"station.A"}, {{{"radio"}, r::ReceiverRole::TransceiverRadio, std::nullopt, true}}); }, "owner");
    invalid([&] { r::ReceiverRegistry bad({"station.A"}, {{{"radio"}, r::ReceiverRole::TransceiverRadio, "station.B", true}}); }, "owner");
    invalid([&] { r::ReceiverRegistry bad({"station.A"}, {{{"ground"}, r::ReceiverRole::PassiveTerrestrial, "station.A", true}}); }, "passive");
    r::RfCausalBoundary unattached("rf", owners);
    invalid([&] { unattached.register_transmission(tx.work, tx.coverage, tx.start.event_id); }, "attach");
}

void decoded_materialization() {
    r::Scheduler scheduler(128, {std::numeric_limits<std::int64_t>::min()}, r::ValidationProfile::StreamingV2);
    const auto owners = registry(); r::RfCausalBoundary boundary("rf", owners); boundary.attach(scheduler);
    lifecycle(scheduler, {-10});
    const auto tx = transmission("shipA", "station.A", 0, 10, 12, {{"radio.A"}});
    register_tx(boundary, scheduler, tx); publish_end(boundary, scheduler, tx);
    auto decoded = discovery(tx, "radio.A", 10, r::RfPairDisposition::Decoded);
    auto wrong_payload = decoded; wrong_payload.decoded_payload_reference = "another.payload";
    invalid([&] { boundary.discover(wrong_payload); }, "immutable payload");
    boundary.discover(decoded); boundary.resolve_terminal();
    const auto released = boundary.materialize_all(scheduler);
    require(released.size() == 1, "decoded materialization count changed");
    scheduler.enqueue(tx.close);
    r::LocalObservationRouter router(owners);
    bool explicit_output = false;
    scheduler.finish([&](const auto& value) {
        if (value.kind == r::EventKind::ReceptionSuccess) {
            const auto local = router.route(released.front(), "station.A", value.time);
            require(local.disposition() == r::DecodeDisposition::Decoded &&
                    local.decoded_payload_reference() == tx.work.payload_reference &&
                    local.causal_event_id() == value.event_id,
                    "decoded local observation lost qualified payload reference/causal receipt");
            const auto& result = std::get<r::ReceptionResult>(value.payload);
            scheduler.enqueue(event(value.time, r::EventKind::ReceptionNoOutput, "output", 1,
                result.receiver_id.value, r::ReceptionNoOutput{result.receiver_id, result.reception_id,
                    result.transmission_id, "SYNTHETIC INFRASTRUCTURE TEST: deliberately no output"}, value.event_id));
        } else if (value.kind == r::EventKind::ReceptionNoOutput) explicit_output = true;
    });
    require(explicit_output, "decoded RX retired without an explicit output disposition");
}

void accepted_snapshot_binding() {
    r::Scheduler scheduler(128, {std::numeric_limits<std::int64_t>::min()}, r::ValidationProfile::StreamingV2);
    r::RfCausalBoundary boundary("rf", registry()); boundary.attach(scheduler);
    lifecycle(scheduler, {-10});
    const auto tx = transmission("shipA", "station.A", 0, 100, 120, {{"ground"}, {"radio.A"}});
    scheduler.enqueue(tx.start);
    invalid([&] { auto work=tx.work; work.payload_reference="payload.P2";
        boundary.register_transmission(work,tx.coverage,tx.start.event_id); }, "snapshot");
    invalid([&] { auto work=tx.work; work.channel=1;
        boundary.register_transmission(work,tx.coverage,tx.start.event_id); }, "snapshot");
    invalid([&] { auto work=tx.work; work.start={1};
        boundary.register_transmission(work,tx.coverage,tx.start.event_id); }, "snapshot");
    invalid([&] { auto work=tx.work; work.end={99};
        boundary.register_transmission(work,tx.coverage,tx.start.event_id); }, "snapshot");
    invalid([&] { auto work=tx.work; work.station_id="station.B";
        boundary.register_transmission(work,tx.coverage,tx.start.event_id); }, "snapshot");
    invalid([&] { auto coverage=tx.coverage; coverage.resolution_horizon={121};
        boundary.register_transmission(tx.work,coverage,tx.start.event_id); }, "snapshot");
    invalid([&] { auto coverage=tx.coverage; coverage.candidate_receivers.erase(coverage.candidate_receivers.begin());
        boundary.register_transmission(tx.work,coverage,tx.start.event_id); }, "snapshot");
    invalid([&] { boundary.register_transmission(tx.work,tx.coverage,{"shipA",999}); }, "actual accepted");
    boundary.register_transmission(tx.work,tx.coverage,tx.start.event_id);
    require(boundary.context(tx.work.transmission_id).payload_reference == "payload.literal",
            "RF context accepted a different payload than the actual queued physical TX");
    invalid([&] { boundary.record_transmission_end(tx.work.transmission_id,{"timer.unknown",1}); }, "actual accepted");
    auto wrong_time=tx.end; wrong_time.source_id="timer.time"; wrong_time.event_id={wrong_time.source_id,1};
    wrong_time.time={99}; scheduler.enqueue(wrong_time);
    invalid([&] { boundary.record_transmission_end(tx.work.transmission_id,wrong_time.event_id); }, "actual accepted");
    auto wrong_cause=tx.end; wrong_cause.source_id="timer.cause"; wrong_cause.event_id={wrong_cause.source_id,1};
    wrong_cause.causal_ref=r::EventId{"shipA",999}; scheduler.enqueue(wrong_cause);
    invalid([&] { boundary.record_transmission_end(tx.work.transmission_id,wrong_cause.event_id); }, "actual accepted");
    auto wrong_tx=tx.end; wrong_tx.source_id="timer.tx"; wrong_tx.event_id={wrong_tx.source_id,1};
    wrong_tx.payload=r::TransmissionEnd{r::make_transmission_id("shipB",1)}; scheduler.enqueue(wrong_tx);
    invalid([&] { boundary.record_transmission_end(tx.work.transmission_id,wrong_tx.event_id); }, "actual accepted");
    // These deliberately invalid pending end records are never dispatched or
    // journaled. The boundary rejects them before any local token can exist.
    scheduler.enqueue(tx.end); boundary.record_transmission_end(tx.work.transmission_id,tx.end.event_id);
}

void signed_endpoint(std::int64_t start, std::int64_t end, std::int64_t horizon) {
    r::Scheduler scheduler(256, {std::numeric_limits<std::int64_t>::min()}, r::ValidationProfile::StreamingV2);
    r::RfCausalBoundary boundary("rf", registry()); boundary.attach(scheduler);
    lifecycle(scheduler, {start-1});
    const auto tx = transmission("shipA", "station.A", start, end, horizon, {{"radio.A"}});
    register_tx(boundary, scheduler, tx); publish_end(boundary, scheduler, tx);
    invalid([&] { boundary.discover(discovery(tx, "radio.A", start, r::RfPairDisposition::Carrier)); }, "strictly positive");
    boundary.discover(discovery(tx, "radio.A", start+1, r::RfPairDisposition::Carrier, "carrier"));
    boundary.discover(discovery(tx, "radio.A", end, r::RfPairDisposition::Failed));
    boundary.resolve_terminal();
    const auto released = boundary.materialize_all(scheduler);
    scheduler.enqueue(tx.close);
    std::size_t observed = 0;
    scheduler.finish([&](const auto&) { ++observed; });
    require(released.size() == 2 && observed == 10, "signed terminal boundary lost RF events");
}
}

int main() {
    try {
        static_assert(!std::is_default_constructible_v<r::ReleasedRfObservation>);
        static_assert(!std::is_constructible_v<r::ReleasedRfObservation, r::Event,
                      std::shared_ptr<const r::ReceiverRegistry>, const r::Scheduler*, std::optional<std::string>>);
        static_assert(!std::is_constructible_v<r::LocalRfObservation, r::Event, std::string>);
        static_assert(!HasGlobalEvent<r::LocalRfObservation>::value,
                      "station-local input must not expose a global journal Event");
        const auto one = cross_slice(false, false);
        require(one == cross_slice(true, true), "RF slice sizes/discovery order changed canonical event bytes");
        require(simultaneous(false) == simultaneous(true), "same-channel final ordering depended on completion order");
        explicit_closures_and_limits();
        decoded_materialization();
        accepted_snapshot_binding();
        signed_endpoint(-100, -99, -98);
        signed_endpoint(std::numeric_limits<std::int64_t>::max()-1,
                        std::numeric_limits<std::int64_t>::max(),
                        std::numeric_limits<std::int64_t>::max());
        std::cout << "PASS SYNTHETIC INFRASTRUCTURE TEST - NOT AIS RF SIMULATION: cross-slice context, finite dispositions, producer barriers, owned local feedback, canonical release, signed terminal bounds\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
