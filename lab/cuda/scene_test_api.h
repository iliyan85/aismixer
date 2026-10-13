#pragma once

#include "scene_api.h"

// CMake enables this private test hook only when BUILD_TESTING is enabled.
// Launch shape is deliberately absent from the public scene API.
#ifdef AISMIXER_LAB_SCENE_TESTING
namespace aismixer::lab::testing {

// Accepts exactly 64, 128, or 256 threads per block.
std::vector<EventIntent> generate_event_intents_with_block_size(
    const std::vector<Actor>& actors, std::uint64_t seed,
    std::uint64_t start_tick, std::uint64_t tick_count,
    std::uint32_t tick_seconds, unsigned int block_size);

}  // namespace aismixer::lab::testing
#endif
