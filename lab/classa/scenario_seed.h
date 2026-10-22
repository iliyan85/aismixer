#pragma once

#include "world.h"

#include <cstdint>
#include <string>

namespace aismixer::lab::classa {

// Once-per-scenario projected seed handoff (SIMULATION_POLICY). Only the
// configuration boundary can mint it from the resolved canonical W1
// configuration; station runtime never receives a world::Config. It carries
// the effective seed, the explicit scenario-instance revision and the W1
// profile identifier, so a StationKnowledge can verify it belongs to the same
// scenario as its OwnStationConfig. It is not a random stream and holds no
// world state.
class ScenarioSeedHandoff {
public:
    std::uint64_t effective_seed() const noexcept { return seed_; }
    std::uint64_t world_revision() const noexcept { return revision_; }
    const std::string& scenario_id() const noexcept { return scenario_id_; }

private:
    friend ScenarioSeedHandoff project_scenario_seed(const world::Config& config,
                                                     std::uint64_t world_revision);
    ScenarioSeedHandoff(std::uint64_t seed, std::uint64_t revision, std::string scenario_id)
        : seed_(seed), revision_(revision), scenario_id_(std::move(scenario_id)) {}
    std::uint64_t seed_;
    std::uint64_t revision_;
    std::string scenario_id_;
};

// Validates through the accepted W1 canonical validator, rejects an
// unresolved fresh seed or zero revision, and projects the resolved seed.
ScenarioSeedHandoff project_scenario_seed(const world::Config& config, std::uint64_t world_revision);

}  // namespace aismixer::lab::classa
