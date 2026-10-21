#include "station_config.h"

namespace aismixer::lab::classa {

// Only the assembly friend can call this after validation. Neither constructor
// nor storage retains a world collection or a reference to caller-owned data.
OwnStationConfig::OwnStationConfig(StationDefinition definition,
                                   std::uint64_t world_revision)
    : definition_(std::move(definition)), world_revision_(world_revision) {}

}  // namespace aismixer::lab::classa
