#pragma once

#include "station_config.h"

#include <cstdint>
#include <vector>

namespace aismixer::lab::classa {

// Scenario/configuration layer ONLY. Calls W1 canonical_config without
// modifying the caller. Rejects unresolved fresh seeds, duplicate identities
// or source IDs, unmatched triples, invalid static codecs and absent metadata.
// A common physical UTC mapping does not share source validity or epochs.
// Returns values in stable VesselId order; retains no registry or world list.
// world_revision identifies this scenario instance and must be nonzero.
std::vector<OwnStationConfig> assemble_stations(
    const world::Config& world_config, std::uint64_t world_revision,
    std::vector<StationDefinition> definitions);

}  // namespace aismixer::lab::classa
