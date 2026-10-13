#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <type_traits>
#include <vector>

namespace aismixer::lab {

struct Sample {
    std::uint32_t index;
    std::uint32_t random_value;
};

static_assert(std::is_trivial_v<Sample> && std::is_standard_layout_v<Sample>,
              "Sample must remain a POD record");
static_assert(sizeof(Sample) == 8 && offsetof(Sample, random_value) == 4,
              "Sample must remain two consecutive 32-bit fields");

inline constexpr std::uint32_t kMaxBatchSize = 1000000;

struct DeviceInfo {
    std::string name;
    int compute_major;
    int compute_minor;
    int runtime_version;
    int driver_version;
    std::uint64_t total_memory_bytes;
    std::uint64_t free_memory_bytes;
};

// Select CUDA device 0 and verify it can load the compiled generation kernel.
// Throws std::runtime_error for CUDA failures or an incompatible/unavailable GPU.
DeviceInfo probe_device();

// Return ordered samples [0, count), using device-side Philox(seed, index, 0).
// Identical seed/count yields identical output with the same cuRAND/toolchain.
// Count zero still validates device 0, but allocates no samples and launches no
// kernel. Throws std::invalid_argument above kMaxBatchSize and std::runtime_error
// for CUDA failures or an incompatible/unavailable GPU.
std::vector<Sample> generate_batch(std::uint64_t seed, std::uint32_t count);

}  // namespace aismixer::lab
