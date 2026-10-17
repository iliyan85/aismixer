#include "event.h"

#include <algorithm>
#include <charconv>
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
struct NmeaPayload { std::string armored; std::uint8_t fill = 0; std::uint8_t channel = 0; };
NmeaPayload nmea(const std::vector<std::string>& sentences) {
    if (sentences.empty() || sentences.size() > 9) invalid("receiver NMEA fragment count must be 1..9");
    std::string sequence, channel;
    NmeaPayload result;
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
        result.armored += fields[5];
        result.fill = static_cast<std::uint8_t>(fields[6][0] - '0');
        result.channel = fields[4] == "A" ? 0 : 1;
    }
    return result;
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
void producer(const std::string& value) {
    if (value.empty() || value.size() > 32) invalid("streaming producer length must be 1..32");
    for (char byte : value) if (!((byte >= 'a' && byte <= 'z') || (byte >= 'A' && byte <= 'Z') ||
        (byte >= '0' && byte <= '9') || byte == '_' || byte == '.' || byte == '-'))
        invalid("streaming producer requires ASCII alphanumeric, underscore, dot or dash");
}
std::pair<std::string, std::uint64_t> logical_id(const std::string& value, const char* prefix) {
    const std::string begin = std::string(prefix) + ':';
    if (value.rfind(begin, 0) != 0) invalid("streaming logical ID has wrong domain prefix");
    const auto separator = value.find(':', begin.size());
    if (separator == std::string::npos) invalid("streaming logical ID requires producer and counter");
    const auto name = value.substr(begin.size(), separator - begin.size()); producer(name);
    const auto digits = std::string_view(value).substr(separator + 1);
    std::uint64_t counter = 0;
    const auto parsed = std::from_chars(digits.data(), digits.data() + digits.size(), counter);
    if (digits.empty() || digits.front() == '0' || parsed.ec != std::errc{} ||
        parsed.ptr != digits.data() + digits.size() || counter == 0)
        invalid("streaming logical ID counter must be canonical positive uint64");
    return {name, counter};
}
}  // namespace

TransmissionId make_transmission_id(const std::string& name, std::uint64_t counter) {
    producer(name); if (counter == 0) invalid("streaming transmission counter must be positive");
    return {"tx:" + name + ':' + std::to_string(counter)};
}
ReceptionId make_reception_id(const std::string& name, std::uint64_t counter) {
    producer(name); if (counter == 0) invalid("streaming reception counter must be positive");
    return {"rx:" + name + ':' + std::to_string(counter)};
}

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
    case EventKind::RfNoReception: return 2;
    case EventKind::SlotMapInit: case EventKind::SlotMapReset: case EventKind::SlotReserve:
    case EventKind::SlotRelease: case EventKind::TdmaDecision: return 3;
    case EventKind::TransmissionStart: return 4;
    case EventKind::ReceiverNmea: case EventKind::OutputError: return 5;
    case EventKind::ReceptionNoOutput: return 5;
    case EventKind::TransmissionResolved: return 6;
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
    case EventKind::RfNoReception: return payload<RfNoReception>(event).receiver_id;
    case EventKind::ReceptionNoOutput: return payload<ReceptionNoOutput>(event).receiver_id;
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
    if (event.schema_version != 1 && event.schema_version != 2) invalid("unsupported event schema version");
    if (event.schema_version == 1 && static_cast<std::uint16_t>(event.kind) >= 17 && static_cast<std::uint16_t>(event.kind) <= 19)
        invalid("streaming closure events require schema 2");
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
        if (p.channel > 1 || p.end_time <= event.time) invalid("transmission requires valid channel and strictly later end time");
        if ((event.schema_version == 1 && p.coverage) || (event.schema_version == 2 && !p.coverage))
            invalid("transmission coverage must match declared event schema");
        if (p.coverage) {
            if (p.coverage->candidate_receivers.size() > kMaxCoverageReceivers || p.coverage->resolution_horizon < p.end_time)
                invalid("transmission coverage roster/horizon exceeds supported bounds");
            std::string previous;
            for (const auto& receiver : p.coverage->candidate_receivers) {
                validate_identity(receiver.value, "coverage receiver");
                if (!previous.empty() && !source_less(previous, receiver.value)) invalid("coverage receivers must be sorted and unique");
                previous = receiver.value;
            }
        }
        break;
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
    case EventKind::RfNoReception: {
        const auto& p = payload<RfNoReception>(event); validate_identity(p.transmission_id.value, "transmission");
        validate_identity(p.receiver_id.value, "receiver"); subject(event, p.receiver_id.value);
        if (p.reason != NoReceptionReason::NotAudible && p.reason != NoReceptionReason::NotScheduled)
            invalid("unsupported no-reception disposition");
        text(p.detail, "no-reception detail"); break;
    }
    case EventKind::ReceptionNoOutput: {
        const auto& p = payload<ReceptionNoOutput>(event); validate_identity(p.receiver_id.value, "receiver");
        validate_identity(p.reception_id.value, "reception"); validate_identity(p.transmission_id.value, "transmission");
        subject(event, p.receiver_id.value); text(p.reason, "missing-output reason", true); break;
    }
    case EventKind::TransmissionResolved: {
        const auto& p = payload<TransmissionResolved>(event); validate_identity(p.transmission_id.value, "transmission");
        if (event.time < p.resolution_horizon) invalid("transmission resolution precedes its declared horizon");
        break;
    }
    }
}

struct EventValidator::Impl {
    struct Life { bool on; SimulationInstant time; EventId event; };
    struct Pair { bool final = false; std::optional<std::string> reception; std::optional<EventId> observation; };
    struct Tx {
        TransmissionStart start;
        SimulationInstant start_time;
        EventId start_id;
        std::optional<EventId> end_id;
        std::optional<NmeaPayload> decoded_binding;
        std::map<std::string, Pair> pairs;
    };
    struct Rx { ReceptionResult result; EventId event; bool success; bool emitted = false; };
    struct Counters { std::uint64_t transmission = 0, reception = 0; };
    ValidationProfile profile;
    explicit Impl(ValidationProfile selected) : profile(selected) {}
    std::map<std::string, std::uint64_t> sources;
    std::map<std::string, Life> stations, receivers;
    std::map<std::string, Tx> transmissions;
    std::map<std::string, Rx> receptions;
    std::map<std::string, std::string> open_transmissions;
    std::map<std::string, Counters> counters;
    std::map<std::string, Event> anchors;
    std::map<std::string, EventId> slot_maps, reservations;
    std::size_t pair_count = 0;
    ValidatorStats statistics;
    std::deque<Event> history;
    std::map<std::string, Event> causes;
    std::optional<Event> last;
    bool sealed = false;
    bool streaming() const noexcept { return profile == ValidationProfile::StreamingV2; }
    void anchor(const Event& event) { if (streaming()) anchors.insert_or_assign(key(event.event_id), event); }
    void unanchor(const EventId& id) { if (streaming()) anchors.erase(key(id)); }
    void metrics() {
        statistics.active_transmissions = transmissions.size(); statistics.active_receptions = receptions.size();
        statistics.active_receiver_pairs = pair_count; statistics.typed_anchors = anchors.size();
        statistics.peak_active_transmissions = std::max(statistics.peak_active_transmissions, statistics.active_transmissions);
        statistics.peak_active_receptions = std::max(statistics.peak_active_receptions, statistics.active_receptions);
        statistics.peak_active_receiver_pairs = std::max(statistics.peak_active_receiver_pairs, statistics.active_receiver_pairs);
        statistics.peak_typed_anchors = std::max(statistics.peak_typed_anchors, statistics.typed_anchors);
    }
};
EventValidator::EventValidator(ValidationProfile profile) : impl_(std::make_unique<Impl>(profile)) {
    if (profile != ValidationProfile::LegacyV1 && profile != ValidationProfile::StreamingV2)
        invalid("unsupported event validation profile");
}
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
    if (event.schema_version != static_cast<std::uint32_t>(state.profile)) invalid("event schema does not match validation profile");
    if (state.last && !CanonicalLess{}(*state.last, event)) invalid("events must have strictly increasing canonical keys");
    const auto source = state.sources.find(event.source_id);
    if (source != state.sources.end() && event.event_id.sequence <= source->second) invalid("duplicate or regressing source-local event sequence");
    if (source == state.sources.end() && state.sources.size() == kMaxEventSources) invalid("event source catalog capacity exceeded");
    const Event* cause = nullptr;
    if (event.causal_ref) {
        const auto identity = key(*event.causal_ref);
        const auto found = state.causes.find(identity);
        if (found != state.causes.end()) cause = &found->second;
        else if (state.streaming()) {
            const auto retained = state.anchors.find(identity);
            if (retained != state.anchors.end()) cause = &retained->second;
        }
        if (!cause) invalid("causal reference is missing or outside retained 4096-event window and live typed anchors");
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
    const auto pair = [&](const Impl::Tx& original, const ReceiverId& receiver) -> const Impl::Pair* {
        const auto found = original.pairs.find(receiver.value);
        if (state.streaming() && found == original.pairs.end()) invalid("receiver is absent from declared TX coverage roster");
        return found == original.pairs.end() ? nullptr : &found->second;
    };
    const auto result_cause = [&](const Impl::Tx& original, const TransmissionId& transmission, const ReceiverId& receiver) {
        bool proven = original.end_id && event.causal_ref && *event.causal_ref == *original.end_id;
        if (cause && cause->kind == EventKind::RfObservation) {
            const auto& observation = payload<RfObservation>(*cause);
            proven = observation.transmission_id == transmission && observation.receiver_id == receiver;
        }
        if (!proven) invalid("reception requires matching TX end or delivered RF observation cause");
    };
    const auto counter = [&](const std::string& identity, const char* domain, bool transmission) {
        const auto parsed = logical_id(identity, domain);
        if (parsed.first != event.source_id) invalid("logical ID producer must match creation event source");
        const auto found = state.counters.find(parsed.first);
        if (found == state.counters.end()) {
            if (state.counters.size() == kMaxEventSources) invalid("logical producer catalog capacity exceeded");
        } else if (parsed.second <= (transmission ? found->second.transmission : found->second.reception))
            invalid("duplicate, retired or regressing run-global logical identity");
        return parsed.second;
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
        if (!on && &catalog == &state.stations && state.open_transmissions.count(event.subject_id))
            invalid("station OFF before active transmission ended");
        break;
    }
    case EventKind::SlotMapInit: case EventKind::SlotMapReset: case EventKind::SlotReserve: case EventKind::SlotRelease: {
        (void)life(state.stations, event.subject_id, "station");
        if (state.streaming()) {
            const auto& p = payload<SlotMapChange>(event);
            const auto map_key = p.station_id + '\n' + std::to_string(p.channel);
            const auto reservation_key = map_key + '\n' + std::to_string(p.slot);
            if (event.kind == EventKind::SlotMapInit) {
                if (state.slot_maps.count(map_key)) invalid("station-local map is already initialized");
                if (state.slot_maps.size() == kLifecycleCapacity) invalid("station-local map anchor capacity exceeded");
            } else if (!state.slot_maps.count(map_key)) invalid("station-local map has no live initialization anchor");
            if (event.kind == EventKind::SlotReserve) {
                if (state.reservations.count(reservation_key)) invalid("duplicate station-local reservation");
                if (state.reservations.size() == kLifecycleCapacity) invalid("station-local reservation anchor capacity exceeded");
            }
            if (event.kind == EventKind::SlotRelease) {
                const auto reserved = state.reservations.find(reservation_key);
                if (reserved == state.reservations.end()) invalid("station-local reservation has no live anchor");
                require_cause(event, reserved->second);
            }
        }
        break;
    }
    case EventKind::TdmaDecision: (void)life(state.stations, event.subject_id, "station"); break;
    case EventKind::TransmissionStart: {
        const auto& p = payload<TransmissionStart>(event); (void)life(state.stations, p.station_id, "transmitting station");
        if (state.transmissions.count(p.transmission_id.value)) invalid("duplicate logical transmission identity");
        if (state.transmissions.size() == (state.streaming() ? kMaxActiveTransmissions : kLifecycleCapacity))
            invalid(state.streaming() ? "active transmission capacity exceeded" : "4096 unique transmission identities per-run capacity exceeded");
        if (state.open_transmissions.count(p.station_id)) invalid("station starts overlapping physical transmissions");
        if (state.streaming()) {
            (void)counter(p.transmission_id.value, "tx", true);
            if (p.coverage->candidate_receivers.size() > kMaxActiveReceiverPairs - state.pair_count)
                invalid("active TX/receiver obligation capacity exceeded");
            for (const auto& receiver : p.coverage->candidate_receivers)
                if (!state.receivers.count(receiver.value)) invalid("coverage receiver lacks a published lifecycle registration");
        }
        break;
    }
    case EventKind::TransmissionEnd: {
        const auto& original = tx(payload<TransmissionEnd>(event).transmission_id);
        if (original.end_id || original.start.end_time != event.time) invalid("transmission end is repeated or differs from declared end time");
        subject(event, original.start.station_id); require_cause(event, original.start_id); break;
    }
    case EventKind::RfObservation: {
        const auto& p = payload<RfObservation>(event); const auto& original = tx(p.transmission_id);
        if (event.time <= original.start_time) invalid("undecoded RF observation availability must be strictly after TX start");
        if (state.streaming() && event.time > original.start.coverage->resolution_horizon)
            invalid("RF observation exceeds declared coverage availability horizon");
        (void)pair(original, p.receiver_id);
        (void)life(state.receivers, p.receiver_id.value, "observing receiver");
        if (p.local_station_id) (void)life(state.stations, *p.local_station_id, "local observing station");
        require_cause(event, original.start_id); break;
    }
    case EventKind::ReceptionSuccess: case EventKind::ReceptionFailure: {
        const auto& p = payload<ReceptionResult>(event); const auto& original = tx(p.transmission_id);
        (void)life(state.receivers, p.receiver_id.value, "receiving receiver");
        if (state.streaming() && event.time > original.start.coverage->resolution_horizon)
            invalid("reception disposition exceeds declared coverage availability horizon");
        if (!original.end_id || event.time < original.start.end_time) invalid("reception becomes available before complete transmission");
        const auto* disposition = pair(original, p.receiver_id);
        if (state.receptions.count(p.reception_id.value) || (disposition && disposition->final))
            invalid("duplicate logical reception identity or TX/receiver result");
        if (state.receptions.size() == (state.streaming() ? kMaxActiveReceptions : kLifecycleCapacity))
            invalid(state.streaming() ? "active reception capacity exceeded" : "4096 unique reception identities per-run capacity exceeded");
        if (state.streaming()) (void)counter(p.reception_id.value, "rx", false);
        result_cause(original, p.transmission_id, p.receiver_id);
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
    case EventKind::RfNoReception: {
        const auto& p = payload<RfNoReception>(event); const auto& original = tx(p.transmission_id);
        if (event.time > original.start.coverage->resolution_horizon)
            invalid("no-reception disposition exceeds declared coverage availability horizon");
        const auto* disposition = pair(original, p.receiver_id);
        if (!original.end_id || event.time < original.start.end_time) invalid("no-reception disposition precedes complete transmission");
        if (disposition && disposition->final) invalid("duplicate TX/receiver closure disposition");
        result_cause(original, p.transmission_id, p.receiver_id); break;
    }
    case EventKind::ReceptionNoOutput: {
        const auto& p = payload<ReceptionNoOutput>(event);
        const auto found = state.receptions.find(p.reception_id.value);
        if (found == state.receptions.end() || !found->second.success || found->second.emitted ||
            !(found->second.result.receiver_id == p.receiver_id) || !(found->second.result.transmission_id == p.transmission_id))
            invalid("missing-output disposition requires unique successful matching reception");
        require_cause(event, found->second.event); break;
    }
    case EventKind::TransmissionResolved: {
        const auto& p = payload<TransmissionResolved>(event); const auto& original = tx(p.transmission_id);
        subject(event, original.start.station_id);
        if (!original.end_id || p.resolution_horizon < original.start.coverage->resolution_horizon)
            invalid("TX resolution lacks end or complete coverage horizon");
        require_cause(event, *original.end_id);
        for (const auto& entry : original.pairs) {
            if (!entry.second.final) invalid("TX resolution has an unresolved receiver obligation");
            if (entry.second.reception) {
                const auto& result = state.receptions.at(*entry.second.reception);
                if (result.success && !result.emitted) invalid("TX resolution has an unmaterialized successful reception output");
            }
        }
        break;
    }
    }
    if (event.kind == EventKind::ReceiverNmea) {
        const auto& p = payload<ReceiverNmea>(event); const auto& original = tx(p.transmission_id);
        const auto bits = nmea(p.sentences);
        if (bits.channel != original.start.channel) invalid("receiver NMEA channel differs from physical TX channel");
        if (original.decoded_binding && (bits.armored != original.decoded_binding->armored || bits.fill != original.decoded_binding->fill))
            invalid("receiver NMEA AIS payload bits differ across one physical TX");
    }
    switch (event.kind) {
    case EventKind::StationOn: case EventKind::StationOff: case EventKind::ReceiverOn: case EventKind::ReceiverOff: {
        auto& catalog = (event.kind == EventKind::StationOn || event.kind == EventKind::StationOff) ? state.stations : state.receivers;
        const auto old = catalog.find(event.subject_id); if (old != catalog.end()) state.unanchor(old->second.event);
        catalog[event.subject_id] = {event.kind == EventKind::StationOn || event.kind == EventKind::ReceiverOn, event.time, event.event_id};
        state.anchor(event);
        if (event.kind == EventKind::StationOff && state.streaming()) {
            const auto prefix = event.subject_id + '\n';
            for (auto* entries : {&state.slot_maps, &state.reservations}) for (auto it = entries->begin(); it != entries->end();) {
                if (it->first.rfind(prefix, 0) == 0) { state.unanchor(it->second); it = entries->erase(it); } else ++it;
            }
        }
        break;
    }
    case EventKind::TransmissionStart: {
        const auto& p = payload<TransmissionStart>(event);
        Impl::Tx original{p, event.time, event.event_id, std::nullopt, std::nullopt, {}};
        if (state.streaming()) {
            for (const auto& receiver : p.coverage->candidate_receivers) original.pairs.emplace(receiver.value, Impl::Pair{});
            state.pair_count += original.pairs.size();
            state.counters[event.source_id].transmission = logical_id(p.transmission_id.value, "tx").second;
        }
        state.transmissions.emplace(p.transmission_id.value, std::move(original));
        state.open_transmissions.emplace(p.station_id, p.transmission_id.value); state.anchor(event); break;
    }
    case EventKind::TransmissionEnd: {
        auto& original = state.transmissions.at(payload<TransmissionEnd>(event).transmission_id.value);
        original.end_id = event.event_id; state.open_transmissions.erase(original.start.station_id); state.anchor(event); break;
    }
    case EventKind::RfObservation: {
        if (state.streaming()) {
            const auto& p = payload<RfObservation>(event); auto& disposition = state.transmissions.at(p.transmission_id.value).pairs.at(p.receiver_id.value);
            if (disposition.observation) state.unanchor(*disposition.observation);
            disposition.observation = event.event_id; state.anchor(event);
        }
        break;
    }
    case EventKind::ReceptionSuccess: case EventKind::ReceptionFailure: {
        const auto& p = payload<ReceptionResult>(event);
        state.receptions.emplace(p.reception_id.value, Impl::Rx{p, event.event_id, event.kind == EventKind::ReceptionSuccess, false});
        auto& original = state.transmissions.at(p.transmission_id.value);
        if (!state.streaming()) ++state.pair_count;
        auto& disposition = original.pairs[p.receiver_id.value]; disposition.final = true; disposition.reception = p.reception_id.value;
        if (state.streaming()) state.counters[event.source_id].reception = logical_id(p.reception_id.value, "rx").second;
        state.anchor(event); break;
    }
    case EventKind::ReceiverNmea: {
        const auto& p = payload<ReceiverNmea>(event); state.receptions.at(p.reception_id.value).emitted = true;
        auto& original = state.transmissions.at(p.transmission_id.value);
        if (!original.decoded_binding) original.decoded_binding = nmea(p.sentences);
        break;
    }
    case EventKind::ReceptionNoOutput: state.receptions.at(payload<ReceptionNoOutput>(event).reception_id.value).emitted = true; break;
    case EventKind::RfNoReception: {
        const auto& p = payload<RfNoReception>(event); state.transmissions.at(p.transmission_id.value).pairs.at(p.receiver_id.value).final = true; break;
    }
    case EventKind::TransmissionResolved: {
        const auto& identity = payload<TransmissionResolved>(event).transmission_id.value;
        const auto& original = state.transmissions.at(identity);
        for (const auto& entry : original.pairs) {
            if (entry.second.observation) state.unanchor(*entry.second.observation);
            if (entry.second.reception) {
                const auto found = state.receptions.find(*entry.second.reception);
                state.unanchor(found->second.event); state.receptions.erase(found);
            }
        }
        state.unanchor(original.start_id); state.unanchor(*original.end_id); state.pair_count -= original.pairs.size();
        state.transmissions.erase(identity); break;
    }
    case EventKind::SlotMapInit: case EventKind::SlotMapReset: case EventKind::SlotReserve: case EventKind::SlotRelease: {
        if (state.streaming()) {
            const auto& p = payload<SlotMapChange>(event); const auto map_key = p.station_id + '\n' + std::to_string(p.channel);
            const auto reservation_key = map_key + '\n' + std::to_string(p.slot);
            if (event.kind == EventKind::SlotMapInit || event.kind == EventKind::SlotMapReset) {
                const auto old = state.slot_maps.find(map_key); if (old != state.slot_maps.end()) state.unanchor(old->second);
                state.slot_maps[map_key] = event.event_id; state.anchor(event);
                const auto prefix = map_key + '\n';
                for (auto it = state.reservations.begin(); it != state.reservations.end();) {
                    if (it->first.rfind(prefix, 0) == 0) { state.unanchor(it->second); it = state.reservations.erase(it); } else ++it;
                }
            } else if (event.kind == EventKind::SlotReserve) { state.reservations[reservation_key] = event.event_id; state.anchor(event); }
            else { state.unanchor(state.reservations.at(reservation_key)); state.reservations.erase(reservation_key); }
        }
        break;
    }
    default: break;
    }
    state.sources[event.source_id] = event.event_id.sequence;
    state.last = event;
    state.history.push_back(event); state.causes.emplace(key(event.event_id), event);
    if (state.history.size() > kCausalHistoryCapacity) { state.causes.erase(key(state.history.front().event_id)); state.history.pop_front(); }
    state.metrics();
}
void EventValidator::finish() {
    if (impl_->sealed) invalid("event validator is already terminally sealed");
    for (const auto& tx : impl_->transmissions) if (!tx.second.end_id) invalid("terminal event stream contains an unfinished transmission");
    if (impl_->streaming() && !impl_->transmissions.empty()) invalid("terminal streaming event stream has unresolved RF/output obligations");
    impl_->sealed = true;
}
std::size_t EventValidator::retained_history() const noexcept { return impl_->history.size(); }
std::size_t EventValidator::source_count() const noexcept { return impl_->sources.size(); }
ValidatorStats EventValidator::stats() const noexcept { return impl_->statistics; }
ValidationProfile EventValidator::profile() const noexcept { return impl_->profile; }
std::optional<Event> EventValidator::retained_event(const EventId& event_id) const {
    const auto identity = key(event_id);
    const auto recent = impl_->causes.find(identity);
    if (recent != impl_->causes.end()) return recent->second;
    const auto live = impl_->anchors.find(identity);
    return live == impl_->anchors.end() ? std::nullopt : std::optional<Event>{live->second};
}

}  // namespace aismixer::lab::runtime
