#include "geo_ais.h"
#include "ais_message.h"
#include "nmea0183.h"

#include <algorithm>
#include <map>
#include <set>
#include <stdexcept>
#include <utility>

namespace aismixer::lab::geo {
std::vector<Identity> synthetic_identities(const std::vector<Actor>& actors) {
    if (actors.size() > kMaxSceneActors) {
        throw std::invalid_argument("synthetic identity population exceeds B2 bound");
    }
    std::vector<std::uint32_t> ids;
    for (const auto& actor : actors) ids.push_back(actor.actor_id);
    std::sort(ids.begin(), ids.end());
    if (std::adjacent_find(ids.begin(), ids.end()) != ids.end()) {
        throw std::invalid_argument("duplicate actor_id in identity catalog");
    }
    std::vector<Identity> result;
    for (std::size_t i = 0; i < ids.size(); ++i) {
        result.push_back({ids[i], std::to_string(100000001 + i)});
    }
    return result;
}

std::vector<OfflineTransmission> encode_offline(
    const std::vector<GeoEvent>& events, const std::vector<Identity>& identities) {
    if (identities.size() > kMaxSceneActors || events.size() > kMaxSceneCandidates) {
        throw std::invalid_argument("offline bridge exceeds bounded population/events");
    }
    std::map<std::uint32_t, ais::Mmsi> catalog;
    std::set<std::uint32_t> mmsis;
    for (const auto& identity : identities) {
        ais::Mmsi mmsi(identity.mmsi);
        if (!mmsis.insert(mmsi.wire_value()).second ||
            !catalog.emplace(identity.actor_id, mmsi).second) {
            throw std::invalid_argument("duplicate actor/MMSI in explicit identity catalog");
        }
    }
    std::vector<OfflineTransmission> output;
    output.reserve(events.size());
    std::uint64_t inferred_tick_seconds = 0;
    for (std::size_t i = 0; i < events.size(); ++i) {
        const auto& event = events[i];
        if (event.absolute_tick > kMaxAbsoluteTick) {
            throw std::invalid_argument("offline logical transmission tick exceeds B2 bound");
        }
        if (event.absolute_tick == 0) {
            if (event.simulation_seconds != 0) throw std::invalid_argument("tick zero must have zero simulation seconds");
        } else {
            const auto duration = event.simulation_seconds / event.absolute_tick;
            if (event.simulation_seconds % event.absolute_tick != 0 || duration == 0 ||
                duration > kMaxTickSeconds || (inferred_tick_seconds && duration != inferred_tick_seconds)) {
                throw std::invalid_argument("inconsistent simulation-time metadata in offline events");
            }
            inferred_tick_seconds = duration;
        }
        if (i && (event.absolute_tick < events[i-1].absolute_tick ||
                  (event.absolute_tick == events[i-1].absolute_tick &&
                   event.actor_id <= events[i-1].actor_id))) {
            throw std::invalid_argument("offline bridge requires unique canonical events");
        }
        const auto identity = catalog.find(event.actor_id);
        if (identity == catalog.end()) throw std::invalid_argument("missing explicit actor MMSI");
        const auto longitude = ais_coordinate(event.location.longitude_ndeg, true);
        const auto latitude = ais_coordinate(event.location.latitude_ndeg, false);
        std::vector<std::string> sentences;
        if (event.category == ActorCategory::ClassA) {
            ais::Type1 message(identity->second, ais::RadioState19(0));
            message.longitude = longitude;
            message.latitude = latitude;
            sentences = ais::encode_aivdm(message, 'A');
        } else if (event.category == ActorCategory::ClassB) {
            ais::Type18 message(identity->second, ais::RadioState20(0));
            message.longitude = longitude;
            message.latitude = latitude;
            sentences = ais::encode_aivdm(message, 'A');
        } else {
            throw std::invalid_argument("offline bridge supports only ClassA/Type1 and ClassB/Type18");
        }
        output.push_back({(event.absolute_tick << 32) | event.actor_id, event.absolute_tick,
                          event.simulation_seconds, event.actor_id, std::move(sentences)});
    }
    return output;
}
}  // namespace aismixer::lab::geo
