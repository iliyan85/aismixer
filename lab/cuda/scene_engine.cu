#include "scene_api.h"
#include "scene_test_api.h"

#include <cuda_runtime.h>
#include <curand_kernel.h>

#include <algorithm>
#include <cstdio>
#include <stdexcept>
#include <string>

namespace aismixer::lab {
namespace {

constexpr unsigned int kThreadsPerBlock = 256;

// Explicit transfer record: active occupies EventIntent's tail-padding space.
// The host converts fields individually and never serializes object padding.
struct DeviceCandidate {
    std::uint64_t absolute_tick;
    std::uint32_t actor_id;
    ActorCategory category;
    std::int64_t x_mm;
    std::int64_t y_mm;
    std::uint32_t random_u32;
    std::uint32_t active;
};

static_assert(std::is_trivial_v<DeviceCandidate> &&
                  std::is_standard_layout_v<DeviceCandidate>,
              "DeviceCandidate must remain a POD transfer record");
static_assert(sizeof(DeviceCandidate) == 40 &&
                  offsetof(DeviceCandidate, absolute_tick) == 0 &&
                  offsetof(DeviceCandidate, actor_id) == 8 &&
                  offsetof(DeviceCandidate, category) == 12 &&
                  offsetof(DeviceCandidate, x_mm) == 16 &&
                  offsetof(DeviceCandidate, y_mm) == 24 &&
                  offsetof(DeviceCandidate, random_u32) == 32 &&
                  offsetof(DeviceCandidate, active) == 36,
              "DeviceCandidate transfer layout must remain explicit");

void check_cuda(cudaError_t status, const char* operation) {
    if (status != cudaSuccess) {
        throw std::runtime_error(std::string(operation) + ": " +
                                 cudaGetErrorName(status) + " (" +
                                 cudaGetErrorString(status) + ")");
    }
}

__global__ void generate_candidates(const Actor* actors, std::uint32_t actor_count,
                                    std::uint64_t seed, std::uint64_t start_tick,
                                    std::uint64_t candidate_count,
                                    std::uint32_t tick_seconds,
                                    DeviceCandidate* candidates) {
    const std::uint64_t index = static_cast<std::uint64_t>(blockIdx.x) * blockDim.x +
                                threadIdx.x;
    if (index >= candidate_count) {
        return;
    }

    // Actor input is sorted on a host copy. This tick-major grid already has
    // canonical order, so retaining active records requires no event sorting.
    const Actor actor = actors[index % actor_count];
    const std::uint64_t absolute_tick = start_tick + index / actor_count;
    DeviceCandidate candidate{};
    if (absolute_tick % actor.report_interval_ticks == actor.phase_ticks) {
        const std::int64_t elapsed_seconds = static_cast<std::int64_t>(absolute_tick) *
                                             static_cast<std::int64_t>(tick_seconds);
        curandStatePhilox4_32_10_t state;
        // Stable identity and absolute time determine the stream and offset;
        // neither batch boundaries nor thread/block indexes enter the key.
        curand_init(static_cast<unsigned long long>(seed),
                    static_cast<unsigned long long>(actor.actor_id),
                    static_cast<unsigned long long>(absolute_tick), &state);
        candidate.absolute_tick = absolute_tick;
        candidate.actor_id = actor.actor_id;
        candidate.category = actor.category;
        candidate.x_mm = actor.x_mm + static_cast<std::int64_t>(actor.vx_mm_per_s) *
                                          elapsed_seconds;
        candidate.y_mm = actor.y_mm + static_cast<std::int64_t>(actor.vy_mm_per_s) *
                                          elapsed_seconds;
        candidate.random_u32 = curand(&state);
        candidate.active = 1;
    }
    candidates[index] = candidate;
}

void select_compatible_device(unsigned int block_size) {
    int device_count = 0;
    check_cuda(cudaGetDeviceCount(&device_count), "cudaGetDeviceCount");
    if (device_count == 0) {
        throw std::runtime_error("No CUDA device is available");
    }
    check_cuda(cudaSetDevice(0), "cudaSetDevice(0)");
    cudaDeviceProp properties{};
    check_cuda(cudaGetDeviceProperties(&properties, 0), "cudaGetDeviceProperties(0)");
    cudaFuncAttributes attributes{};
    check_cuda(cudaFuncGetAttributes(&attributes, generate_candidates),
               "cudaFuncGetAttributes(generate_candidates): verify compiled GPU architecture");
    if (attributes.maxThreadsPerBlock < static_cast<int>(block_size) ||
        properties.maxThreadsPerBlock < static_cast<int>(block_size)) {
        throw std::runtime_error("CUDA device 0 cannot run the scene generation block");
    }
}

template <typename T>
class DeviceBuffer {
public:
    DeviceBuffer(std::size_t bytes, const char* label) : label_(label) {
        check_cuda(cudaMalloc(reinterpret_cast<void**>(&data_), bytes), label_);
    }

    DeviceBuffer(const DeviceBuffer&) = delete;
    DeviceBuffer& operator=(const DeviceBuffer&) = delete;

    ~DeviceBuffer() noexcept {
        if (data_ != nullptr) {
            const cudaError_t status = cudaFree(data_);
            if (status != cudaSuccess) {
                // Preserve an exception already in flight while exposing a
                // cleanup failure, matching the B1 buffer error contract.
                std::fprintf(stderr, "CUDA cleanup failed: cudaFree(%s): %s (%s)\n",
                             label_, cudaGetErrorName(status), cudaGetErrorString(status));
            }
        }
    }

    T* data() const noexcept { return data_; }

    void release(const char* operation) {
        T* allocation = data_;
        data_ = nullptr;
        check_cuda(cudaFree(allocation), operation);
    }

private:
    T* data_ = nullptr;
    const char* label_;
};

void validate_scene(const std::vector<Actor>& actors, std::uint64_t start_tick,
                    std::uint64_t tick_count, std::uint32_t tick_seconds) {
    if (actors.size() > kMaxSceneActors) {
        throw std::invalid_argument("Actor count exceeds maximum scene size " +
                                    std::to_string(kMaxSceneActors));
    }
    // Divide before multiplying, including requests with huge uint64 counts.
    if (!actors.empty() && tick_count > kMaxSceneCandidates / actors.size()) {
        throw std::invalid_argument("Actor-tick candidate count exceeds maximum " +
                                    std::to_string(kMaxSceneCandidates));
    }
    if (tick_count > kMaxSceneTicks) {
        throw std::invalid_argument("Tick count exceeds maximum scene range " +
                                    std::to_string(kMaxSceneTicks));
    }
    if (tick_seconds == 0 || tick_seconds > kMaxTickSeconds) {
        throw std::invalid_argument("tick_seconds must be in [1, " +
                                    std::to_string(kMaxTickSeconds) + "]");
    }
    if (start_tick > kMaxAbsoluteTick ||
        (tick_count != 0 && tick_count - 1 > kMaxAbsoluteTick - start_tick)) {
        throw std::invalid_argument("Requested absolute ticks exceed maximum " +
                                    std::to_string(kMaxAbsoluteTick));
    }
    for (const Actor& actor : actors) {
        const std::string context = "Actor " + std::to_string(actor.actor_id) + ": ";
        if (static_cast<std::uint32_t>(actor.category) >
            static_cast<std::uint32_t>(ActorCategory::Emergency)) {
            throw std::invalid_argument(context + "invalid category");
        }
        if (actor.report_interval_ticks == 0) {
            throw std::invalid_argument(context + "report_interval_ticks must be positive");
        }
        if (actor.phase_ticks >= actor.report_interval_ticks) {
            throw std::invalid_argument(context + "phase_ticks must be less than report_interval_ticks");
        }
        // Direct comparisons also reject INT64_MIN/INT32_MIN without abs overflow.
        if (actor.x_mm < -kMaxInitialPositionMm || actor.x_mm > kMaxInitialPositionMm ||
            actor.y_mm < -kMaxInitialPositionMm || actor.y_mm > kMaxInitialPositionMm) {
            throw std::invalid_argument(context + "initial position exceeds scene bounds");
        }
        if (actor.vx_mm_per_s < -kMaxVelocityMmPerS ||
            actor.vx_mm_per_s > kMaxVelocityMmPerS ||
            actor.vy_mm_per_s < -kMaxVelocityMmPerS ||
            actor.vy_mm_per_s > kMaxVelocityMmPerS) {
            throw std::invalid_argument(context + "velocity exceeds scene bounds");
        }
        if ((actor.category == ActorCategory::BaseStation ||
             actor.category == ActorCategory::AtoN) &&
            (actor.vx_mm_per_s != 0 || actor.vy_mm_per_s != 0)) {
            throw std::invalid_argument(context + "BaseStation and AtoN must be stationary");
        }
    }
}

std::vector<EventIntent> generate_with_block_size(const std::vector<Actor>& actors,
                                                 std::uint64_t seed,
                                                 std::uint64_t start_tick,
                                                 std::uint64_t tick_count,
                                                 std::uint32_t tick_seconds,
                                                 unsigned int block_size) {
    if (block_size != 64 && block_size != 128 && block_size != 256) {
        throw std::invalid_argument("Scene block size must be 64, 128, or 256");
    }
    validate_scene(actors, start_tick, tick_count, tick_seconds);
    std::vector<Actor> ordered_actors = actors;
    std::sort(ordered_actors.begin(), ordered_actors.end(),
              [](const Actor& left, const Actor& right) { return left.actor_id < right.actor_id; });
    for (std::size_t index = 1; index < ordered_actors.size(); ++index) {
        if (ordered_actors[index - 1].actor_id == ordered_actors[index].actor_id) {
            throw std::invalid_argument("Duplicate actor_id " +
                                        std::to_string(ordered_actors[index].actor_id));
        }
    }
    select_compatible_device(block_size);
    if (ordered_actors.empty() || tick_count == 0) {
        return {};
    }

    // Bounds were checked before any GPU allocation or count multiplication.
    const std::uint64_t candidate_count = tick_count * ordered_actors.size();
    const std::size_t actor_bytes = ordered_actors.size() * sizeof(Actor);
    const std::size_t candidate_bytes = static_cast<std::size_t>(candidate_count) *
                                        sizeof(DeviceCandidate);
    std::vector<DeviceCandidate> candidates(static_cast<std::size_t>(candidate_count));
    DeviceBuffer<Actor> device_actors(actor_bytes, "cudaMalloc(scene actors)");
    DeviceBuffer<DeviceCandidate> device_candidates(candidate_bytes,
                                                     "cudaMalloc(scene candidates)");
    check_cuda(cudaMemcpy(device_actors.data(), ordered_actors.data(), actor_bytes,
                          cudaMemcpyHostToDevice),
               "cudaMemcpy(scene actors to device)");
    const unsigned int blocks = static_cast<unsigned int>(
        (candidate_count + block_size - 1) / block_size);
    generate_candidates<<<blocks, block_size>>>(
        device_actors.data(), static_cast<std::uint32_t>(ordered_actors.size()), seed,
        start_tick, candidate_count, tick_seconds, device_candidates.data());
    check_cuda(cudaGetLastError(), "generate_candidates launch");
    check_cuda(cudaDeviceSynchronize(), "generate_candidates synchronization");
    check_cuda(cudaMemcpy(candidates.data(), device_candidates.data(), candidate_bytes,
                          cudaMemcpyDeviceToHost),
               "cudaMemcpy(scene candidates to host)");
    device_candidates.release("cudaFree(scene candidates)");
    device_actors.release("cudaFree(scene actors)");

    std::vector<EventIntent> events;
    events.reserve(candidates.size());
    for (const DeviceCandidate& candidate : candidates) {
        if (candidate.active != 0) {
            events.push_back(EventIntent{candidate.absolute_tick, candidate.actor_id,
                                         candidate.category, candidate.x_mm, candidate.y_mm,
                                         candidate.random_u32});
        }
    }
    return events;
}

}  // namespace

std::vector<EventIntent> generate_event_intents(const std::vector<Actor>& actors,
                                               std::uint64_t seed,
                                               std::uint64_t start_tick,
                                               std::uint64_t tick_count,
                                               std::uint32_t tick_seconds) {
    return generate_with_block_size(actors, seed, start_tick, tick_count, tick_seconds,
                                    kThreadsPerBlock);
}

#ifdef AISMIXER_LAB_SCENE_TESTING
namespace testing {

std::vector<EventIntent> generate_event_intents_with_block_size(
    const std::vector<Actor>& actors, std::uint64_t seed,
    std::uint64_t start_tick, std::uint64_t tick_count,
    std::uint32_t tick_seconds, unsigned int block_size) {
    return generate_with_block_size(actors, seed, start_tick, tick_count, tick_seconds,
                                    block_size);
}

}  // namespace testing
#endif

}  // namespace aismixer::lab
