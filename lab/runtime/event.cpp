#include "event.h"

#include <algorithm>
#include <deque>
#include <map>
#include <stdexcept>
#include <string_view>
#include <utility>

namespace aismixer::lab::runtime {
namespace {
[[noreturn]] void invalid(const std::string& message) { throw std::invalid_argument(message); }
void text(const std::string& value, const char* field, bool nonempty = false) {
    if (value.size() > kMaxEventTextBytes || (nonempty && value.empty()))
        invalid(std::string(field) + ": invalid bounded text length");
    // Validate UTF-8 without locale dependence, overlongs, surrogates or NUL.
    for (std::size_t i = 0; i < value.size();) {
        const auto lead = static_cast<unsigned char>(value[i++]);
        if (lead < 0x80) {
            if (lead < 0x20 || lead == 0x7f) invalid(std::string(field) + ": control byte");
            continue;
        }
        unsigned continuation = 0;
        std::uint32_t code = 0, minimum = 0;
        if (lead >= 0xc2 && lead <= 0xdf) { continuation = 1; code = lead & 0x1fu; minimum = 0x80; }
        else if (lead >= 0xe0 && lead <= 0xef) { continuation = 2; code = lead & 0x0fu; minimum = 0x800; }
        else if (lead >= 0xf0 && lead <= 0xf4) { continuation = 3; code = lead & 0x07u; minimum = 0x10000; }
        else invalid(std::string(field) + ": invalid UTF-8 lead byte");
        if (continuation > value.size() - i) invalid(std::string(field) + ": truncated UTF-8");
        while (continuation-- != 0) {
            const auto byte = static_cast<unsigned char>(value[i++]);
            if ((byte & 0xc0u) != 0x80u) invalid(std::string(field) + ": invalid UTF-8 continuation");
            code = (code << 6) | (byte & 0x3fu);
        }
        if (code < minimum || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff))
            invalid(std::string(field) + ": invalid UTF-8 scalar");
    }
}
void event_id(const EventId& id) {
    validate_identity(id.source_id, "event ID source");
    if (id.sequence == 0) invalid("event ID sequence must be positive");
}
int hex(char value) {
    if (value >= '0' && value <= '9') return value - '0';
    if (value >= 'A' && value <= 'F') return value - 'A' + 10;
    return -1;
}
void nmea(const std::vector<std::string>& sentences) {
    if (sentences.empty() || sentences.size() > 9) invalid("receiver NMEA fragment count must be 1..9");
    std::string sequence, channel;
    for (std::size_t ordinal = 0; ordinal < sentences.size(); ++ordinal) {
        const auto& value = sentences[ordinal];
        if (value.size() < 20 || value.size() > 82 || value.front() != '!' ||
            value.substr(value.size() - 2) != "\r\n" || value[value.size() - 5] != '*')
            invalid("receiver NMEA requires <=82-byte canonical CRLF sentence");
        unsigned check = 0;
        for (std::size_t i = 1; i < value.size() - 5; ++i) {
            const auto byte = static_cast<unsigned char>(value[i]);
            if (byte < 0x20 || byte > 0x7e) invalid("receiver NMEA body must be printable ASCII");
            check ^= byte;
        }
        const int first = hex(value[value.size() - 4]), second = hex(value[value.size() - 3]);
        if (first < 0 || second < 0 || check != static_cast<unsigned>(first * 16 + second))
            invalid("receiver NMEA checksum mismatch");
        std::vector<std::string_view> fields;
        const std::string_view body(value.data() + 1, value.size() - 6);
        std::size_t start = 0;
        while (true) {
            const auto delimiter = body.find(',', start);
            fields.push_back(body.substr(start, delimiter == std::string_view::npos ? delimiter : delimiter - start));
            if (delimiter == std::string_view::npos) break;
            start = delimiter + 1;
        }
        if (fields.size() != 7 || fields[0] != "AIVDM" || fields[1].size() != 1 ||
            fields[1][0] != static_cast<char>('0' + sentences.size()) || fields[2].size() != 1 ||
            fields[2][0] != static_cast<char>('1' + ordinal) || fields[4].size() != 1 ||
            (fields[4] != "A" && fields[4] != "B") || fields[5].empty() || fields[6].size() != 1 ||
            fields[6][0] < '0' || fields[6][0] > '5') invalid("receiver NMEA fragment fields are invalid");
        if ((sentences.size() == 1 && !fields[3].empty()) ||
            (sentences.size() > 1 && (fields[3].size() != 1 || fields[3][0] < '0' || fields[3][0] > '9')) ||
            (ordinal + 1 != sentences.size() && fields[6] != "0"))
            invalid("receiver NMEA fragment sequence/fill is invalid");
        for (char byte : fields[5]) if (!((byte >= '0' && byte <= 'W') || (byte >= '`' && byte <= 'w')))
            invalid("receiver NMEA payload armoring is invalid");
        if (ordinal + 1 == sentences.size() && fields[6] != "0") {
            const auto byte = static_cast<unsigned char>(fields[5].back());
            const auto sextet = static_cast<unsigned>(byte - (byte < 88 ? 48 : 56));
            const auto fill = static_cast<unsigned>(fields[6][0] - '0');
            if ((sextet & ((1u << fill) - 1u)) != 0) invalid("receiver NMEA fill padding must contain zero bits");
        }
        if (ordinal == 0) { sequence = std::string(fields[3]); channel = std::string(fields[4]); }
        else if (fields[3] != sequence || fields[4] != channel) invalid("receiver NMEA fragmented message mismatch");
    }
}
template <typename T> const T& payload(const Event& event) {
    const auto* value = std::get_if<T>(&event.payload);
    if (!value) invalid("event kind does not match typed payload");
    return *value;
}
void subject(const Event& event, const std::string& expected) {
    if (event.subject_id != expected) invalid("event subject does not match typed payload identity");
}
bool source_less(const std::string& a, const std::string& b) {
    return std::lexicographical_compare(a.begin(), a.end(), b.begin(), b.end(),
        [](char x, char y) { return static_cast<unsigned char>(x) < static_cast<unsigned char>(y); });
}
std::string key(const EventId& value) { return value.source_id + "\n" + std::to_string(value.sequence); }
void require_cause(const Event& event, const EventId& expected) {
    if (!event.causal_ref || !(*event.causal_ref == expected)) invalid("event requires its proven lifecycle causal predecessor");
}
}  // namespace

bool operator==(const ReceiverId& a, const ReceiverId& b) noexcept { return a.value == b.value; }
bool operator==(const TransmissionId& a, const TransmissionId& b) noexcept { return a.value == b.value; }
bool operator==(const ReceptionId& a, const ReceptionId& b) noexcept { return a.value == b.value; }
bool operator==(const EventId& a, const EventId& b) noexcept { return a.source_id == b.source_id && a.sequence == b.sequence; }

void validate_identity(const std::string& identity, const char* field) {
    if (identity.empty() || identity.size() > kMaxIdentityBytes) invalid(std::string(field) + ": ASCII identity length must be 1..64");
    for (unsigned char byte : identity) if (byte < 0x21 || byte > 0x7e)
        invalid(std::string(field) + ": identity must contain printable non-space ASCII bytes");
}
std::uint8_t causal_phase(EventKind kind) {
    switch (kind) {
    case EventKind::StationOn: case EventKind::StationOff: case EventKind::ReceiverOn: case EventKind::ReceiverOff: return 0;
    case EventKind::TransmissionEnd: case EventKind::RfObservation: return 1;
    case EventKind::ReceptionSuccess: case EventKind::ReceptionFailure: return 2;
    case EventKind::SlotMapInit: case EventKind::SlotMapReset: case EventKind::SlotReserve:
    case EventKind::SlotRelease: case EventKind::TdmaDecision: return 3;
    case EventKind::TransmissionStart: return 4;
    case EventKind::ReceiverNmea: case EventKind::OutputError: return 5;
    }
    invalid("unknown event kind");
}
bool CanonicalLess::operator()(const Event& a, const Event& b) const {
    if (a.time != b.time) return a.time < b.time;
    if (causal_phase(a.kind) != causal_phase(b.kind)) return causal_phase(a.kind) < causal_phase(b.kind);
    if (a.source_id != b.source_id) return source_less(a.source_id, b.source_id);
    if (a.event_id.sequence != b.event_id.sequence) return a.event_id.sequence < b.event_id.sequence;
    return static_cast<std::uint16_t>(a.kind) < static_cast<std::uint16_t>(b.kind);
}
std::optional<ReceiverId> event_receiver(const Event& event) {
    switch (event.kind) {
    case EventKind::ReceiverOn: case EventKind::ReceiverOff: return payload<ReceiverLifecycle>(event).receiver_id;
    case EventKind::RfObservation: return payload<RfObservation>(event).receiver_id;
    case EventKind::ReceptionSuccess: case EventKind::ReceptionFailure: return payload<ReceptionResult>(event).receiver_id;
    case EventKind::ReceiverNmea: return payload<ReceiverNmea>(event).receiver_id;
    case EventKind::OutputError: return payload<OutputError>(event).receiver_id;
    default: return std::nullopt;
    }
}
void validate_receiver_nmea(const ReceiverNmea& output) {
    validate_identity(output.receiver_id.value, "receiver");
    validate_identity(output.reception_id.value, "reception");
    validate_identity(output.transmission_id.value, "transmission");
    nmea(output.sentences);
}
void validate_event(const Event& event) {
    if (event.schema_version != kEventSchemaVersion) invalid("unsupported event schema version");
    validate_identity(event.source_id, "event source"); validate_identity(event.subject_id, "event subject");
    event_id(event.event_id);
    if (event.event_id.source_id != event.source_id) invalid("event ID source must match envelope source");
    if (event.causal_ref) { event_id(*event.causal_ref); if (*event.causal_ref == event.event_id) invalid("event cannot cause itself"); }
    (void)causal_phase(event.kind);
    switch (event.kind) {
    case EventKind::StationOn: case EventKind::StationOff: {
        const auto& p = payload<StationLifecycle>(event); validate_identity(p.station_id, "station"); subject(event, p.station_id); break;
    }
    case EventKind::ReceiverOn: case EventKind::ReceiverOff: {
        const auto& p = payload<ReceiverLifecycle>(event); validate_identity(p.receiver_id.value, "receiver"); subject(event, p.receiver_id.value); break;
    }
    case EventKind::SlotMapInit: case EventKind::SlotMapReset: case EventKind::SlotReserve: case EventKind::SlotRelease: {
        const auto& p = payload<SlotMapChange>(event); validate_identity(p.station_id, "station"); subject(event, p.station_id);
        if (p.channel > 1 || p.slot >= kSlotsPerFrame) invalid("station-local slotmap channel/slot out of range");
        text(p.reason, "slotmap reason"); break;
    }
    case EventKind::TdmaDecision: {
        const auto& p = payload<TdmaDecision>(event); validate_identity(p.station_id, "station"); subject(event, p.station_id); text(p.decision, "TDMA decision", true); break;
    }
    case EventKind::TransmissionStart: {
        const auto& p = payload<TransmissionStart>(event); validate_identity(p.transmission_id.value, "transmission");
        validate_identity(p.station_id, "station"); validate_identity(p.payload_reference, "payload reference"); subject(event, p.station_id);
        if (p.channel > 1 || p.end_time <= event.time) invalid("transmission requires valid channel and strictly later end time"); break;
    }
    case EventKind::TransmissionEnd: validate_identity(payload<TransmissionEnd>(event).transmission_id.value, "transmission"); break;
    case EventKind::RfObservation: {
        const auto& p = payload<RfObservation>(event); validate_identity(p.transmission_id.value, "transmission");
        validate_identity(p.receiver_id.value, "receiver"); subject(event, p.receiver_id.value);
        if (p.availability_time != event.time) invalid("RF observation event time must equal its availability time");
        if (p.local_station_id) validate_identity(*p.local_station_id, "local observation station");
        text(p.detail, "RF observation detail"); break;
    }
    case EventKind::ReceptionSuccess: case EventKind::ReceptionFailure: {
        const auto& p = payload<ReceptionResult>(event); validate_identity(p.reception_id.value, "reception");
        validate_identity(p.transmission_id.value, "transmission"); validate_identity(p.receiver_id.value, "receiver"); subject(event, p.receiver_id.value);
        if (p.availability_time != event.time) invalid("reception event time must equal its availability time");
        text(p.detail, "reception detail"); break;
    }
    case EventKind::ReceiverNmea: {
        const auto& p = payload<ReceiverNmea>(event); validate_receiver_nmea(p);
        subject(event, p.receiver_id.value); break;
    }
    case EventKind::OutputError: {
        const auto& p = payload<OutputError>(event); validate_identity(p.receiver_id.value, "receiver"); subject(event, p.receiver_id.value);
        validate_identity(p.code, "output error code"); text(p.detail, "output error detail"); break;
    }
    }
}

struct EventValidator::Impl {
    struct Life { bool on; SimulationInstant time; EventId event; };
    struct Tx { TransmissionStart start; EventId start_id; std::optional<EventId> end_id; };
    struct Rx { ReceptionResult result; EventId event; bool success; bool emitted = false; };
    std::map<std::string, std::uint64_t> sources;
    std::map<std::string, Life> stations, receivers;
    std::map<std::string, Tx> transmissions;
    std::map<std::string, Rx> receptions;
    std::map<std::pair<std::string, std::string>, std::string> receiver_results;
    std::deque<Event> history;
    std::map<std::string, Event> causes;
    std::optional<Event> last;
    bool sealed = false;
};
EventValidator::EventValidator() : impl_(std::make_unique<Impl>()) {}
EventValidator::~EventValidator() = default;
EventValidator::EventValidator(EventValidator&&) noexcept = default;
EventValidator& EventValidator::operator=(EventValidator&&) noexcept = default;
EventValidator::EventValidator(const EventValidator& other) : impl_(std::make_unique<Impl>(*other.impl_)) {}
EventValidator& EventValidator::operator=(const EventValidator& other) {
    if (this != &other) impl_ = std::make_unique<Impl>(*other.impl_);
    return *this;
}
void EventValidator::append(const Event& event) {
    auto& state = *impl_;
    if (state.sealed) invalid("event validator is terminally sealed");
    validate_event(event);
    if (state.last && !CanonicalLess{}(*state.last, event)) invalid("events must have strictly increasing canonical keys");
    const auto source = state.sources.find(event.source_id);
    if (source != state.sources.end() && event.event_id.sequence <= source->second) invalid("duplicate or regressing source-local event sequence");
    if (source == state.sources.end() && state.sources.size() == kMaxEventSources) invalid("event source catalog capacity exceeded");
    const Event* cause = nullptr;
    if (event.causal_ref) {
        const auto found = state.causes.find(key(*event.causal_ref));
        if (found == state.causes.end()) invalid("causal reference is missing or outside retained 4096-event window");
        cause = &found->second;
        if (cause->time > event.time || (cause->time == event.time && causal_phase(cause->kind) >= causal_phase(event.kind)))
            invalid("causal reference needs earlier time or strictly earlier simultaneous phase");
    }
    const auto life = [&](const auto& catalog, const std::string& identity, const char* label) -> const Impl::Life& {
        const auto found = catalog.find(identity);
        if (found == catalog.end() || !found->second.on) invalid(std::string(label) + " is not ON");
        return found->second;
    };
    const auto tx = [&](const TransmissionId& identity) -> const Impl::Tx& {
        const auto found = state.transmissions.find(identity.value);
        if (found == state.transmissions.end()) invalid("transmission has no proven start");
        return found->second;
    };
    // All validation precedes semantic mutation, so rejected records do not
    // consume IDs/sequences or change lifecycle state.
    switch (event.kind) {
    case EventKind::StationOn: case EventKind::StationOff: case EventKind::ReceiverOn: case EventKind::ReceiverOff: {
        auto& catalog = (event.kind == EventKind::StationOn || event.kind == EventKind::StationOff) ? state.stations : state.receivers;
        const bool on = event.kind == EventKind::StationOn || event.kind == EventKind::ReceiverOn;
        const auto found = catalog.find(event.subject_id);
        if (found == catalog.end()) {
            if (!on) invalid("lifecycle OFF has no earlier ON");
            if (catalog.size() == kLifecycleCapacity) invalid("lifecycle identity catalog capacity exceeded");
        } else if (found->second.on == on || event.time <= found->second.time) invalid("lifecycle transition is repeated or lacks strictly earlier state");
        if (!on && &catalog == &state.stations) for (const auto& entry : state.transmissions)
            if (entry.second.start.station_id == event.subject_id && !entry.second.end_id) invalid("station OFF before active transmission ended");
        break;
    }
    case EventKind::SlotMapInit: case EventKind::SlotMapReset: case EventKind::SlotReserve: case EventKind::SlotRelease:
    case EventKind::TdmaDecision: (void)life(state.stations, event.subject_id, "station"); break;
    case EventKind::TransmissionStart: {
        const auto& p = payload<TransmissionStart>(event); (void)life(state.stations, p.station_id, "transmitting station");
        if (state.transmissions.count(p.transmission_id.value)) invalid("duplicate logical transmission identity");
        if (state.transmissions.size() == kLifecycleCapacity) invalid("4096 unique transmission identities per-run capacity exceeded");
        for (const auto& entry : state.transmissions) if (entry.second.start.station_id == p.station_id && !entry.second.end_id)
            invalid("station starts overlapping physical transmissions");
        break;
    }
    case EventKind::TransmissionEnd: {
        const auto& original = tx(payload<TransmissionEnd>(event).transmission_id);
        if (original.end_id || original.start.end_time != event.time) invalid("transmission end is repeated or differs from declared end time");
        subject(event, original.start.station_id); require_cause(event, original.start_id); break;
    }
    case EventKind::RfObservation: {
        const auto& p = payload<RfObservation>(event); const auto& original = tx(p.transmission_id);
        (void)life(state.receivers, p.receiver_id.value, "observing receiver");
        if (p.local_station_id) (void)life(state.stations, *p.local_station_id, "local observing station");
        require_cause(event, original.start_id); break;
    }
    case EventKind::ReceptionSuccess: case EventKind::ReceptionFailure: {
        const auto& p = payload<ReceptionResult>(event); const auto& original = tx(p.transmission_id);
        (void)life(state.receivers, p.receiver_id.value, "receiving receiver");
        if (!original.end_id || event.time < original.start.end_time) invalid("reception becomes available before complete transmission");
        if (state.receptions.count(p.reception_id.value) || state.receiver_results.count({p.transmission_id.value, p.receiver_id.value}))
            invalid("duplicate logical reception identity or TX/receiver result");
        if (state.receptions.size() == kLifecycleCapacity) invalid("4096 unique reception identities per-run capacity exceeded");
        bool proven = event.causal_ref && *event.causal_ref == *original.end_id;
        if (cause && cause->kind == EventKind::RfObservation) {
            const auto& observation = payload<RfObservation>(*cause);
            proven = observation.transmission_id == p.transmission_id && observation.receiver_id == p.receiver_id;
        }
        if (!proven) invalid("reception requires matching TX end or delivered RF observation cause");
        break;
    }
    case EventKind::ReceiverNmea: {
        const auto& p = payload<ReceiverNmea>(event); (void)life(state.receivers, p.receiver_id.value, "NMEA receiver");
        const auto found = state.receptions.find(p.reception_id.value);
        if (found == state.receptions.end() || !found->second.success || found->second.emitted ||
            !(found->second.result.receiver_id == p.receiver_id) || !(found->second.result.transmission_id == p.transmission_id))
            invalid("receiver NMEA requires unique successful matching reception");
        require_cause(event, found->second.event); break;
    }
    case EventKind::OutputError: (void)life(state.receivers, payload<OutputError>(event).receiver_id.value, "output receiver"); break;
    }
    switch (event.kind) {
    case EventKind::StationOn: case EventKind::StationOff:
        state.stations[event.subject_id] = {event.kind == EventKind::StationOn, event.time, event.event_id}; break;
    case EventKind::ReceiverOn: case EventKind::ReceiverOff:
        state.receivers[event.subject_id] = {event.kind == EventKind::ReceiverOn, event.time, event.event_id}; break;
    case EventKind::TransmissionStart: {
        const auto& p = payload<TransmissionStart>(event); state.transmissions.emplace(p.transmission_id.value, Impl::Tx{p, event.event_id, std::nullopt}); break;
    }
    case EventKind::TransmissionEnd: state.transmissions.at(payload<TransmissionEnd>(event).transmission_id.value).end_id = event.event_id; break;
    case EventKind::ReceptionSuccess: case EventKind::ReceptionFailure: {
        const auto& p = payload<ReceptionResult>(event);
        state.receptions.emplace(p.reception_id.value, Impl::Rx{p, event.event_id, event.kind == EventKind::ReceptionSuccess, false});
        state.receiver_results.emplace(std::make_pair(p.transmission_id.value, p.receiver_id.value), p.reception_id.value); break;
    }
    case EventKind::ReceiverNmea: state.receptions.at(payload<ReceiverNmea>(event).reception_id.value).emitted = true; break;
    default: break;
    }
    state.sources[event.source_id] = event.event_id.sequence;
    state.last = event;
    state.history.push_back(event); state.causes.emplace(key(event.event_id), event);
    if (state.history.size() > kCausalHistoryCapacity) { state.causes.erase(key(state.history.front().event_id)); state.history.pop_front(); }
}
void EventValidator::finish() {
    if (impl_->sealed) invalid("event validator is already terminally sealed");
    for (const auto& tx : impl_->transmissions) if (!tx.second.end_id) invalid("terminal event stream contains an unfinished transmission");
    impl_->sealed = true;
}
std::size_t EventValidator::retained_history() const noexcept { return impl_->history.size(); }
std::size_t EventValidator::source_count() const noexcept { return impl_->sources.size(); }

}  // namespace aismixer::lab::runtime
