#include "rf_contract.h"
#include "scheduler.h"

#include <algorithm>
#include <charconv>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <tuple>
#include <utility>

namespace aismixer::lab::runtime {
void validate_rf_submission(const RfWorkSubmission& work) {
    if (work.schema_version != 1 || work.transmissions.size() > kMaxRfTransmissions ||
        work.receiver_changes.size() > kMaxRfReceivers || work.active_receivers.size() > kMaxRfReceivers ||
        work.observation_capacity == 0 ||
        work.observation_capacity > kMaxRfObservations) {
        throw std::invalid_argument("RF work schema/capacity is unsupported");
    }
    std::set<std::string> transmissions, receivers, active;
    std::map<std::string, std::vector<std::pair<SimulationInstant, SimulationInstant>>> station_intervals;
    for (const auto& receiver : work.active_receivers) {
        validate_identity(receiver.value, "RF active receiver");
        if (!active.insert(receiver.value).second) throw std::invalid_argument("RF duplicate active receiver");
    }
    for (const auto& tx : work.transmissions) {
        validate_identity(tx.transmission_id.value, "RF transmission_id");
        validate_identity(tx.station_id, "RF station_id");
        validate_identity(tx.payload_reference, "RF payload_reference");
        if (!transmissions.insert(tx.transmission_id.value).second || tx.channel > 1 ||
            tx.start < work.commit_watermark || tx.end <= tx.start) {
            throw std::invalid_argument("RF duplicate TX, channel, interval or committed-prefix violation");
        }
        station_intervals[tx.station_id].emplace_back(tx.start, tx.end);
    }
    for (auto& station : station_intervals) {
        auto& intervals = station.second;
        std::sort(intervals.begin(), intervals.end(), [](const auto& a, const auto& b) {
            return a.first != b.first ? a.first < b.first : a.second < b.second;
        });
        for (std::size_t i = 1; i < intervals.size(); ++i)
            if (intervals[i].first < intervals[i - 1].second)
                throw std::invalid_argument("RF station has overlapping physical TX intervals");
    }
    for (const auto& rx : work.receiver_changes) {
        validate_identity(rx.receiver_id.value, "RF receiver_id");
        if (!receivers.insert(rx.receiver_id.value).second ||
            rx.enabled != (active.count(rx.receiver_id.value) != 0) || rx.height_datum != "WGS84_ellipsoid" ||
            rx.longitude_ndeg < -180000000000LL || rx.longitude_ndeg > 180000000000LL ||
            rx.latitude_ndeg < -90000000000LL || rx.latitude_ndeg > 90000000000LL ||
            rx.antenna_height_mm < -1000000 || rx.antenna_height_mm > 10000000) {
            throw std::invalid_argument("RF receiver identity/location/height datum is invalid");
        }
    }
}

void validate_rf_completion(const RfWorkSubmission& work,
                            const std::vector<CompletedRfObservation>& observations) {
    validate_rf_submission(work);
    if (observations.size() > work.observation_capacity) {
        throw std::invalid_argument("RF compact completion exceeds submitted capacity");
    }
    std::map<std::string, const RfTransmissionWork*> transmissions;
    std::set<std::string> receivers, observation_ids, reception_ids;
    std::set<std::pair<std::string, std::string>> receiver_results;
    for (const auto& tx : work.transmissions) transmissions.emplace(tx.transmission_id.value, &tx);
    for (const auto& receiver : work.active_receivers) receivers.insert(receiver.value);
    for (const auto& observation : observations) {
        validate_identity(observation.observation_id.source_id, "RF observation source");
        if (observation.observation_id.sequence == 0) throw std::invalid_argument("RF observation sequence must be positive");
        const auto key = observation.observation_id.source_id + "/" + std::to_string(observation.observation_id.sequence);
        const auto tx = transmissions.find(observation.transmission_id.value);
        if (!observation_ids.insert(key).second || tx == transmissions.end() ||
            !receivers.count(observation.receiver_id.value) ||
            observation.availability_time < tx->second->start ||
            observation.availability_time < work.commit_watermark) {
            throw std::invalid_argument("RF completion identity/availability has no eligible submitted work");
        }
        if (observation.observing_station_id) validate_identity(*observation.observing_station_id, "RF local observing station");
        if (observation.disposition != DecodeDisposition::Undecoded &&
            observation.disposition != DecodeDisposition::Decoded &&
            observation.disposition != DecodeDisposition::Failed) {
            throw std::invalid_argument("RF decode disposition is unsupported");
        }
        if (observation.disposition == DecodeDisposition::Undecoded) {
            if (observation.availability_time < add_units(tx->second->start, 1))
                throw std::invalid_argument("Undecoded RF availability must be strictly after TX start");
            if (observation.reception_id || observation.decoded_payload_reference)
                throw std::invalid_argument("Undecoded RF observation cannot imply reception or decoded payload");
        } else {
            if (!observation.reception_id || observation.availability_time < tx->second->end)
                throw std::invalid_argument("RF decode result precedes complete TX or lacks reception identity");
            validate_identity(observation.reception_id->value, "RF reception_id");
            if (!reception_ids.insert(observation.reception_id->value).second)
                throw std::invalid_argument("RF duplicate reception identity");
            if (!receiver_results.emplace(observation.transmission_id.value, observation.receiver_id.value).second)
                throw std::invalid_argument("RF duplicate decode result for one TX/receiver pair");
            if (observation.disposition == DecodeDisposition::Decoded) {
                if (!observation.decoded_payload_reference ||
                    *observation.decoded_payload_reference != tx->second->payload_reference)
                    throw std::invalid_argument("RF decoded result must reference the submitted immutable payload");
            } else if (observation.decoded_payload_reference) {
                throw std::invalid_argument("Failed RF decoding cannot supply a decoded payload");
            }
        }
    }
}

namespace {
constexpr SimulationInstant earliest{std::numeric_limits<std::int64_t>::min()};
constexpr SimulationInstant latest{std::numeric_limits<std::int64_t>::max()};
[[noreturn]] void rf_invalid(const char* message) { throw std::invalid_argument(message); }
void event_identity(const EventId& id) {
    validate_identity(id.source_id, "RF causal event source");
    if (id.sequence == 0) rf_invalid("RF causal event sequence must be positive");
}
std::pair<std::string, std::uint64_t> transmission_identity(const TransmissionId& id) {
    const auto split = id.value.find(':', 3);
    if (id.value.compare(0, 3, "tx:") != 0 || split == std::string::npos)
        rf_invalid("RF schema 2 requires canonical producer/counter TX identity");
    const auto producer = id.value.substr(3, split - 3);
    std::uint64_t counter = 0;
    const auto begin = id.value.data() + split + 1;
    const auto end = id.value.data() + id.value.size();
    const auto parsed = std::from_chars(begin, end, counter);
    if (parsed.ec != std::errc{} || parsed.ptr != end ||
        make_transmission_id(producer, counter).value != id.value)
        rf_invalid("RF schema 2 requires canonical producer/counter TX identity");
    return {producer, counter};
}
std::uint8_t discovery_phase(RfPairDisposition disposition) {
    return disposition == RfPairDisposition::Carrier ? 1 : 2;
}
bool discovery_less(const RfDiscovery& a, const RfDiscovery& b) {
    return std::make_tuple(a.availability_time.units, discovery_phase(a.disposition),
               a.receiver_id.value, a.transmission_id.value, a.disposition, a.semantic_key,
               a.decoded_payload_reference) <
           std::make_tuple(b.availability_time.units, discovery_phase(b.disposition),
               b.receiver_id.value, b.transmission_id.value, b.disposition, b.semantic_key,
               b.decoded_payload_reference);
}
}

ReceiverRegistry::ReceiverRegistry(std::vector<std::string> stations,
                                 std::vector<ReceiverOwnership> receivers)
    : stations_(std::move(stations)), receivers_(std::move(receivers)) {
    if (stations_.size() > kLifecycleCapacity || receivers_.size() > kMaxRfReceivers)
        rf_invalid("RF ownership catalog capacity exceeded");
    std::sort(stations_.begin(), stations_.end());
    for (std::size_t i = 0; i < stations_.size(); ++i) {
        validate_identity(stations_[i], "RF ownership station");
        if (i && stations_[i - 1] == stations_[i]) rf_invalid("RF duplicate ownership station");
    }
    std::sort(receivers_.begin(), receivers_.end(), [](const auto& a, const auto& b) {
        return a.receiver_id.value < b.receiver_id.value;
    });
    for (std::size_t i = 0; i < receivers_.size(); ++i) {
        const auto& receiver = receivers_[i];
        validate_identity(receiver.receiver_id.value, "RF owned receiver");
        if (i && receivers_[i - 1].receiver_id == receiver.receiver_id)
            rf_invalid("RF duplicate owned receiver");
        if (receiver.role == ReceiverRole::TransceiverRadio) {
            if (!receiver.owner_station_id || !std::binary_search(stations_.begin(), stations_.end(),
                                                                 *receiver.owner_station_id))
                rf_invalid("RF transceiver requires a registered owner station");
        } else if (receiver.role == ReceiverRole::PassiveTerrestrial) {
            if (receiver.owner_station_id) rf_invalid("RF passive receiver cannot own AIS station knowledge");
        } else rf_invalid("RF receiver role is unsupported");
    }
}
const ReceiverOwnership& ReceiverRegistry::receiver(const ReceiverId& id) const {
    const auto found = std::lower_bound(receivers_.begin(), receivers_.end(), id.value,
        [](const auto& receiver, const auto& value) { return receiver.receiver_id.value < value; });
    if (found == receivers_.end() || !(found->receiver_id == id)) rf_invalid("RF receiver is not in ownership catalog");
    return *found;
}
const std::vector<ReceiverOwnership>& ReceiverRegistry::receivers() const noexcept { return receivers_; }

ReleasedRfObservation::ReleasedRfObservation(Event event,
    std::shared_ptr<const ReceiverRegistry> registry, const Scheduler* scheduler,
    std::optional<std::string> decoded_payload_reference)
    : event_(std::move(event)), registry_(std::move(registry)), scheduler_(scheduler),
      decoded_payload_reference_(std::move(decoded_payload_reference)) {}
const Event& ReleasedRfObservation::event() const noexcept { return event_; }
LocalRfObservation::LocalRfObservation(std::string station, ReceiverId receiver,
    SimulationInstant availability, EventId cause, DecodeDisposition disposition,
    std::optional<std::string> decoded_payload_reference)
    : station_id_(std::move(station)), receiver_id_(std::move(receiver)),
      availability_time_(availability), causal_event_id_(std::move(cause)), disposition_(disposition),
      decoded_payload_reference_(std::move(decoded_payload_reference)) {}
const std::string& LocalRfObservation::station_id() const noexcept { return station_id_; }
const ReceiverId& LocalRfObservation::receiver_id() const noexcept { return receiver_id_; }
SimulationInstant LocalRfObservation::availability_time() const noexcept { return availability_time_; }
const EventId& LocalRfObservation::causal_event_id() const noexcept { return causal_event_id_; }
DecodeDisposition LocalRfObservation::disposition() const noexcept { return disposition_; }
const std::optional<std::string>& LocalRfObservation::decoded_payload_reference() const noexcept {
    return decoded_payload_reference_;
}
LocalObservationRouter::LocalObservationRouter(std::shared_ptr<const ReceiverRegistry> registry)
    : registry_(std::move(registry)) {
    if (!registry_) rf_invalid("RF local router needs an ownership registry");
}
LocalRfObservation LocalObservationRouter::route(const ReleasedRfObservation& observation,
    const std::string& station, SimulationInstant decision_time) const {
    validate_identity(station, "RF local decision station");
    if (registry_ != observation.registry_) rf_invalid("RF observation belongs to a different ownership catalog");
    if (!observation.scheduler_->has_dispatched(observation.event_.event_id))
        rf_invalid("RF observation has not been validated and dispatched by Scheduler");
    if (observation.event_.kind != EventKind::RfObservation &&
        observation.event_.kind != EventKind::ReceptionSuccess &&
        observation.event_.kind != EventKind::ReceptionFailure)
        rf_invalid("RF coverage/no-evaluation disposition is not station-local radio knowledge");
    const auto id = event_receiver(observation.event_);
    if (!id) rf_invalid("RF token is not a receiver observation");
    const auto& receiver = registry_->receiver(*id);
    if (!receiver.enabled || receiver.role != ReceiverRole::TransceiverRadio ||
        !receiver.owner_station_id || *receiver.owner_station_id != station)
        rf_invalid("RF observation is not from this station's enabled own radio");
    if (decision_time < observation.event_.time)
        rf_invalid("RF local decision precedes observation availability");
    const auto disposition = observation.event_.kind == EventKind::RfObservation ? DecodeDisposition::Undecoded :
        observation.event_.kind == EventKind::ReceptionSuccess ? DecodeDisposition::Decoded : DecodeDisposition::Failed;
    return LocalRfObservation(station, *id, observation.event_.time, observation.event_.event_id,
        disposition, disposition == DecodeDisposition::Decoded ? observation.decoded_payload_reference_ : std::nullopt);
}

struct RfCausalBoundary::Impl : CausalProducerBarrier {
    struct Tx {
        RfTransmissionWork work;
        TransmissionCoverage coverage;
        EventId start_id;
        std::optional<EventId> end_id;
        std::map<std::string, bool> final;
    };
    std::string producer;
    std::shared_ptr<const ReceiverRegistry> registry;
    std::size_t capacity = 0;
    std::map<std::string, Tx> transmissions;
    std::map<std::string, std::uint64_t> tx_highwaters;
    std::vector<RfDiscovery> pending;
    std::size_t pairs = 0;
    std::uint64_t event_counter = 0, reception_counter = 0;
    SimulationInstant horizon = earliest;
    mutable SimulationInstant committed_watermark = earliest;
    Scheduler* scheduler = nullptr;  // Caller keeps attached Scheduler alive/unmoved.
    bool terminal = false, poisoned = false;
    void usable() const {
        if (poisoned) rf_invalid("RF producer is poisoned after failed materialization");
        if (!scheduler) rf_invalid("RF producer must attach its barrier before registration");
    }
    void open() const {
        usable();
        if (terminal) rf_invalid("RF producer is terminally resolved");
    }
    const Tx& tx(const TransmissionId& id) const {
        const auto found = transmissions.find(id.value);
        if (found == transmissions.end()) rf_invalid("RF TX has no registered in-flight context");
        return found->second;
    }
    void validate_advance(SimulationInstant watermark) const override {
        usable();
        if (watermark > horizon) rf_invalid("RF producer resolution horizon does not cover event watermark");
        for (const auto& observation : pending) if (observation.availability_time < watermark)
            rf_invalid("RF observation must be materialized before event watermark");
    }
    void validate_finish() const override {
        usable();
        if (!terminal || !pending.empty()) rf_invalid("RF terminal barrier has unresolved or unmaterialized work");
        for (const auto& entry : transmissions) {
            if (!entry.second.end_id) rf_invalid("RF terminal barrier lacks published TX end");
            for (const auto& pair : entry.second.final) if (!pair.second)
                rf_invalid("RF terminal barrier has an unresolved receiver obligation");
        }
    }
    void committed(SimulationInstant watermark) const override {
        if (watermark < committed_watermark) rf_invalid("RF committed watermark cannot regress");
        committed_watermark = watermark;
    }
    using Materialized = std::pair<Event, std::optional<std::string>>;
    std::vector<Materialized> materialize(SimulationInstant watermark, Scheduler& target, bool all) {
        usable();
        if (&target != scheduler) rf_invalid("RF materialization targets an unattached scheduler");
        if (all ? !terminal : watermark > horizon)
            rf_invalid("RF materialization requires a resolved producer horizon");
        std::sort(pending.begin(), pending.end(), discovery_less);
        const auto stop = all ? pending.end() : std::find_if(pending.begin(), pending.end(),
            [&](const auto& observation) { return observation.availability_time >= watermark; });
        // Validate all required causal anchors and counters before enqueueing.
        const auto count = static_cast<std::uint64_t>(stop - pending.begin());
        if (count > std::numeric_limits<std::uint64_t>::max() - event_counter)
            throw std::overflow_error("RF released event counter exhausted");
        std::uint64_t receptions = 0;
        for (auto i = pending.begin(); i != stop; ++i) {
            const auto& registered = tx(i->transmission_id);
            if (i->disposition != RfPairDisposition::Carrier && !registered.end_id)
                rf_invalid("RF final result requires the actual published TX end anchor");
            if (i->disposition == RfPairDisposition::Decoded || i->disposition == RfPairDisposition::Failed)
                ++receptions;
        }
        if (receptions > std::numeric_limits<std::uint64_t>::max() - reception_counter)
            throw std::overflow_error("RF released reception counter exhausted");
        std::vector<Materialized> result;
        result.reserve(static_cast<std::size_t>(count));
        try {
            for (auto i = pending.begin(); i != stop; ++i) {
                const auto& registered = tx(i->transmission_id);
                Event event;
                event.schema_version = 2;
                event.time = i->availability_time;
                event.source_id = producer;
                event.subject_id = i->receiver_id.value;
                event.event_id = {producer, ++event_counter};
                if (i->disposition == RfPairDisposition::Carrier) {
                    event.kind = EventKind::RfObservation;
                    event.causal_ref = registered.start_id;
                    const auto& receiver = registry->receiver(i->receiver_id);
                    event.payload = RfObservation{i->transmission_id, i->receiver_id,
                        i->availability_time, true, receiver.owner_station_id, "RF contract observation; no RF model"};
                } else {
                    event.causal_ref = *registered.end_id;
                    if (i->disposition == RfPairDisposition::Decoded || i->disposition == RfPairDisposition::Failed) {
                        event.kind = i->disposition == RfPairDisposition::Decoded ?
                            EventKind::ReceptionSuccess : EventKind::ReceptionFailure;
                        event.payload = ReceptionResult{make_reception_id(producer, ++reception_counter),
                            i->transmission_id, i->receiver_id, i->availability_time,
                            "RF contract explicit disposition; no RF model"};
                    } else {
                        event.kind = EventKind::RfNoReception;
                        event.payload = RfNoReception{i->transmission_id, i->receiver_id,
                            i->disposition == RfPairDisposition::NotAudible ?
                                NoReceptionReason::NotAudible : NoReceptionReason::NotScheduled,
                            "RF contract explicit no-reception disposition; no RF model"};
                    }
                }
                target.enqueue(event);
                result.emplace_back(std::move(event), i->decoded_payload_reference);
            }
        } catch (...) { poisoned = true; throw; }
        pending.erase(pending.begin(), stop);
        return result;
    }
};

RfCausalBoundary::RfCausalBoundary(std::string producer,
    std::shared_ptr<const ReceiverRegistry> registry, std::size_t capacity)
    : impl_(std::make_shared<Impl>()) {
    (void)make_reception_id(producer, 1);  // Same strict bounded producer grammar.
    if (!registry || capacity == 0 || capacity > kMaxRfObservations)
        rf_invalid("RF boundary registry/capacity is invalid");
    impl_->producer = std::move(producer); impl_->registry = std::move(registry); impl_->capacity = capacity;
}
RfCausalBoundary::~RfCausalBoundary() = default;
RfCausalBoundary::RfCausalBoundary(RfCausalBoundary&&) noexcept = default;
RfCausalBoundary& RfCausalBoundary::operator=(RfCausalBoundary&&) noexcept = default;
std::shared_ptr<const CausalProducerBarrier> RfCausalBoundary::barrier() const { return impl_; }
void RfCausalBoundary::attach(Scheduler& scheduler) {
    if (impl_->scheduler) rf_invalid("RF producer is already attached to a scheduler");
    impl_->scheduler = &scheduler;
    try { scheduler.attach_producer_barrier(impl_); }
    catch (...) { impl_->scheduler = nullptr; throw; }
}
void RfCausalBoundary::register_transmission(RfTransmissionWork work,
    TransmissionCoverage coverage, EventId start_id) {
    auto& state = *impl_; state.open();
    const auto identity = transmission_identity(work.transmission_id);
    event_identity(start_id);
    if (start_id.source_id != identity.first)
        rf_invalid("RF TX start anchor source differs from logical ID producer");
    validate_identity(work.station_id, "RF station");
    validate_identity(work.payload_reference, "RF payload reference");
    if (work.channel > 1 || work.end <= work.start || work.start < state.committed_watermark ||
        work.start < state.horizon || coverage.resolution_horizon < work.end)
        rf_invalid("RF fresh TX interval is invalid or retroactive to committed/resolved history");
    const auto highwater = state.tx_highwaters.find(identity.first);
    if ((highwater != state.tx_highwaters.end() && identity.second <= highwater->second) ||
        state.transmissions.count(work.transmission_id.value))
        rf_invalid("RF run-global TX identity was reused or regressed after retirement");
    if (highwater == state.tx_highwaters.end() && state.tx_highwaters.size() == kMaxEventSources)
        rf_invalid("RF stable TX producer catalog capacity exceeded");
    if (state.transmissions.size() == kMaxRfTransmissions ||
        coverage.candidate_receivers.size() > kMaxCoverageReceivers ||
        coverage.candidate_receivers.size() > kMaxActiveReceiverPairs - state.pairs)
        rf_invalid("RF active context/candidate roster capacity exceeded");
    for (const auto& entry : state.transmissions) if (entry.second.work.station_id == work.station_id &&
        work.start < entry.second.work.end && entry.second.work.start < work.end)
        rf_invalid("RF station has overlapping registered physical TX intervals");
    Impl::Tx registered{std::move(work), std::move(coverage), std::move(start_id), std::nullopt, {}};
    std::string previous_receiver;
    for (const auto& receiver : registered.coverage.candidate_receivers) {
        if (!previous_receiver.empty() && previous_receiver >= receiver.value)
            rf_invalid("RF candidate roster must be sorted and unique");
        if (!state.registry->receiver(receiver).enabled) rf_invalid("RF candidate receiver is disabled");
        if (!registered.final.emplace(receiver.value, false).second) rf_invalid("RF candidate roster duplicates receiver");
        previous_receiver = receiver.value;
    }
    const auto accepted = state.scheduler->accepted_event(registered.start_id);
    if (!accepted || accepted->schema_version != 2 || accepted->kind != EventKind::TransmissionStart)
        rf_invalid("RF TX start anchor is not an actual accepted schema 2 TX start");
    const auto& actual = std::get<TransmissionStart>(accepted->payload);
    if (!(actual.transmission_id == registered.work.transmission_id) ||
        actual.station_id != registered.work.station_id || accepted->subject_id != registered.work.station_id ||
        actual.channel != registered.work.channel || accepted->time != registered.work.start ||
        actual.end_time != registered.work.end || actual.payload_reference != registered.work.payload_reference ||
        !actual.coverage || actual.coverage->resolution_horizon != registered.coverage.resolution_horizon ||
        actual.coverage->candidate_receivers != registered.coverage.candidate_receivers)
        rf_invalid("RF TX snapshot differs from actual accepted TX start");
    const auto key = registered.work.transmission_id.value;
    state.transmissions.emplace(key, std::move(registered));
    state.pairs += state.transmissions.at(key).final.size();
    state.tx_highwaters[identity.first] = identity.second;
}
void RfCausalBoundary::record_transmission_end(const TransmissionId& id, EventId end_id) {
    auto& state = *impl_; state.open(); event_identity(end_id);
    const auto& original = state.tx(id);
    if (original.end_id || (end_id.source_id == original.start_id.source_id &&
        end_id.sequence <= original.start_id.sequence))
        rf_invalid("RF TX end causal anchor is repeated or precedes its start");
    const auto accepted = state.scheduler->accepted_event(end_id);
    if (!accepted || accepted->schema_version != 2 || accepted->kind != EventKind::TransmissionEnd ||
        !(std::get<TransmissionEnd>(accepted->payload).transmission_id == id) ||
        accepted->time != original.work.end || accepted->subject_id != original.work.station_id ||
        !accepted->causal_ref || !(*accepted->causal_ref == original.start_id))
        rf_invalid("RF TX end anchor differs from actual accepted TX end/time/cause");
    state.transmissions.at(id.value).end_id = std::move(end_id);
}
const RfTransmissionWork& RfCausalBoundary::context(const TransmissionId& id) const {
    impl_->usable(); return impl_->tx(id).work;
}
void RfCausalBoundary::discover(RfDiscovery observation) {
    auto& state = *impl_; state.open();
    validate_identity(observation.semantic_key, "RF discovery semantic key");
    const auto& original = state.tx(observation.transmission_id);
    const auto pair = original.final.find(observation.receiver_id.value);
    if (pair == original.final.end() || !state.registry->receiver(observation.receiver_id).enabled)
        rf_invalid("RF discovery has no enabled finite candidate pair");
    if (observation.availability_time < state.horizon || observation.availability_time < state.committed_watermark ||
        observation.availability_time > original.coverage.resolution_horizon)
        rf_invalid("RF discovery is late to producer/event barrier or beyond declared coverage horizon");
    if (observation.disposition == RfPairDisposition::Carrier) {
        if (observation.availability_time < add_units(original.work.start, 1) || observation.decoded_payload_reference)
            rf_invalid("RF carrier requires strictly positive availability and no decoded payload");
    } else if (observation.disposition == RfPairDisposition::Decoded || observation.disposition == RfPairDisposition::Failed ||
               observation.disposition == RfPairDisposition::NotAudible || observation.disposition == RfPairDisposition::NotScheduled) {
        if (pair->second || observation.availability_time < original.work.end)
            rf_invalid("RF final disposition is duplicate or precedes complete TX");
        if (observation.disposition == RfPairDisposition::Decoded) {
            if (!observation.decoded_payload_reference || *observation.decoded_payload_reference != original.work.payload_reference)
                rf_invalid("RF decoded disposition differs from immutable payload reference");
        } else if (observation.decoded_payload_reference)
            rf_invalid("RF unsuccessful disposition cannot contain decoded payload");
    } else rf_invalid("RF pair disposition is unsupported");
    if (state.pending.size() == state.capacity) rf_invalid("RF pending discovery capacity exceeded; apply backpressure");
    for (const auto& prior : state.pending) if (!discovery_less(prior, observation) && !discovery_less(observation, prior))
        rf_invalid("RF duplicate semantic discovery");
    const bool final = observation.disposition != RfPairDisposition::Carrier;
    const auto tx_key = observation.transmission_id.value;
    const auto rx_key = observation.receiver_id.value;
    state.pending.push_back(std::move(observation));
    if (final) state.transmissions.at(tx_key).final.at(rx_key) = true;
}
void RfCausalBoundary::resolve_before(SimulationInstant horizon) {
    auto& state = *impl_; state.open();
    if (horizon < state.horizon || horizon < state.committed_watermark) rf_invalid("RF resolution horizon cannot regress");
    for (const auto& entry : state.transmissions) if (entry.second.coverage.resolution_horizon < horizon)
        for (const auto& pair : entry.second.final) if (!pair.second)
            rf_invalid("RF resolution barrier has an overdue unresolved receiver obligation");
    state.horizon = horizon;
}
void RfCausalBoundary::resolve_terminal() {
    auto& state = *impl_; state.open();
    for (const auto& entry : state.transmissions) {
        if (!entry.second.end_id) rf_invalid("RF terminal resolution lacks actual TX end anchor");
        for (const auto& pair : entry.second.final) if (!pair.second)
            rf_invalid("RF terminal resolution has an unresolved receiver obligation");
    }
    state.horizon = latest; state.terminal = true;
}
std::vector<ReleasedRfObservation> RfCausalBoundary::materialize_before(SimulationInstant watermark, Scheduler& scheduler) {
    const auto events = impl_->materialize(watermark, scheduler, false);
    std::vector<ReleasedRfObservation> result; result.reserve(events.size());
    for (const auto& item : events) result.push_back(ReleasedRfObservation(item.first, impl_->registry, &scheduler, item.second));
    return result;
}
std::vector<ReleasedRfObservation> RfCausalBoundary::materialize_all(Scheduler& scheduler) {
    const auto events = impl_->materialize(latest, scheduler, true);
    std::vector<ReleasedRfObservation> result; result.reserve(events.size());
    for (const auto& item : events) result.push_back(ReleasedRfObservation(item.first, impl_->registry, &scheduler, item.second));
    return result;
}
void RfCausalBoundary::retire_transmission(const TransmissionId& id) {
    auto& state = *impl_; state.usable(); const auto& original = state.tx(id);
    if (!original.end_id || (!state.terminal && state.horizon <= original.coverage.resolution_horizon))
        rf_invalid("RF context cannot retire before TX end and declared resolution horizon");
    for (const auto& pair : original.final) if (!pair.second)
        rf_invalid("RF context cannot retire with an unresolved receiver obligation");
    for (const auto& observation : state.pending) if (observation.transmission_id == id)
        rf_invalid("RF context cannot retire before discoveries are materialized");
    state.pairs -= original.final.size(); state.transmissions.erase(id.value);
}
SimulationInstant RfCausalBoundary::rf_resolved_before() const noexcept { return impl_->horizon; }
SimulationInstant RfCausalBoundary::event_commit_watermark() const noexcept { return impl_->committed_watermark; }
RfBoundaryStats RfCausalBoundary::stats() const noexcept {
    return {impl_->transmissions.size(), impl_->pairs, impl_->pending.size(),
            impl_->tx_highwaters.size(), impl_->event_counter};
}
}  // namespace aismixer::lab::runtime
