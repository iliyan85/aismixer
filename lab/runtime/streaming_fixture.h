#pragma once

#include "journal.h"

namespace aismixer::lab::runtime {

// INFRASTRUCTURE TEST -- NOT AIS RF SIMULATION. This is a bounded scripted
// serializer/scheduler workload, not a protocol, RF or traffic-rate model.
inline constexpr std::uint64_t kStreamingFixtureTransmissions = 10800;
inline constexpr std::uint64_t kStreamingFixtureReceptions = 32400;
inline constexpr std::uint64_t kStreamingFixtureRecords = 97210;
inline constexpr std::int64_t kStreamingFixtureSeconds = 18000;
JournalMetadata streaming_fixture_metadata();
JournalStats record_streaming_fixture(const std::filesystem::path& new_run_directory,
                                     JournalOptions options = {}, bool fine_steps = false);

}  // namespace aismixer::lab::runtime
