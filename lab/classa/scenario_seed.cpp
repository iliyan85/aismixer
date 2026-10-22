#include "scenario_seed.h"

#include <stdexcept>

namespace aismixer::lab::classa {

ScenarioSeedHandoff project_scenario_seed(const world::Config& config, std::uint64_t world_revision) {
    if (world_revision == 0)
        throw std::invalid_argument("Class A scenario seed: explicit nonzero scenario-instance revision required");
    const auto canonical = world::canonical_config(config);
    if (canonical.fresh_seed)
        throw std::invalid_argument("Class A scenario seed: resolve the W1 fresh seed before projecting it");
    return ScenarioSeedHandoff(canonical.effective_seed, world_revision, canonical.profile_id);
}

}  // namespace aismixer::lab::classa
