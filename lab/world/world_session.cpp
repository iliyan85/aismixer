#include "world.h"
#include <algorithm>
#include <stdexcept>

namespace aismixer::lab::world {
WorldSession::WorldSession(Config config,Options options)
    : WorldSession(resolve_seed(std::move(config)),options,true) {}
WorldSession::WorldSession(Config config,Options options,bool)
    : capacity_(options.capacity),cuda_(options),timeline_(config,cuda_.sample_regions(config.effective_seed,region_specs(config))) {}
void WorldSession::advance(SimulationInstant target,const TransitionSink& sink){timeline_.advance(target,sink);}
Snapshot WorldSession::snapshot(){
    const auto prepared=timeline_.prepare(); std::vector<PositionQuery> queries;
    for(const auto& vessel:prepared.vessels)queries.push_back(vessel.position_query);
    std::vector<PositionResult> positions;
    for(std::size_t begin=0;begin<queries.size();begin+=capacity_) {
        const auto end=std::min(queries.size(),begin+capacity_);
        const auto values=cuda_.evaluate({queries.begin()+static_cast<std::ptrdiff_t>(begin),queries.begin()+static_cast<std::ptrdiff_t>(end)});
        positions.insert(positions.end(),values.begin(),values.end());
    }
    return materialize_snapshot(prepared,positions);
}
Timeline& WorldSession::timeline(){return timeline_;}
Session& WorldSession::cuda(){return cuda_;}
}  // namespace aismixer::lab::world
