#pragma once

#include "geo_api.h"
#include <string>
#include <vector>

namespace aismixer::lab::geo {
struct Identity {
    std::uint32_t actor_id;
    std::string mmsi;
};
struct OfflineTransmission {
    std::uint64_t logical_transmission_id;
    std::uint64_t absolute_tick;
    std::uint64_t simulation_seconds;
    std::uint32_t actor_id;
    std::vector<std::string> sentences;
};
// Sorted population rank -> 100000001 + rank. Purely synthetic offline IDs;
// no assertion that this prefix is an allocated testing range. Save this
// catalog; changing the population may change rank-derived assignments.
std::vector<Identity> synthetic_identities(const std::vector<Actor>& actors);
// Class A -> Type 1, Class B -> Type 18 only. Every other category fails.
// Unknown motion/navigation/UTC fields use documented B3 unavailable codes;
// raw radio state zero is an explicit test value, without live TDMA meaning.
std::vector<OfflineTransmission> encode_offline(
    const std::vector<GeoEvent>& events, const std::vector<Identity>& identities);
}  // namespace aismixer::lab::geo
