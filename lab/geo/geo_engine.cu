#include "geo_api.h"

#include <cuda_runtime.h>
#include <curand_kernel.h>
#include <cub/device/device_select.cuh>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <limits>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>

namespace aismixer::lab::geo {
namespace {

using Clock = std::chrono::steady_clock;

constexpr double kPi = 3.141592653589793238462643383279502884;
constexpr double kWgs84SemiMajorM = 6378137.0;
constexpr double kWgs84EccentricitySquared = 6.6943799901413165e-3;

struct DeviceActor {
    Actor actor;
    Point explicit_anchor;
};
struct DeviceAnchor {
    Point point;
    double longitude_ndeg_per_mm;
    double latitude_ndeg_per_mm;
};
struct Counters {
    unsigned long long scheduled;
    unsigned long long rejected;
    unsigned long long placement_proposals;
    unsigned int placement_errors;
    unsigned int projection_errors;
};
static_assert(std::is_trivial_v<GeoEvent> &&
                  std::is_standard_layout_v<GeoEvent>,
              "GeoEvent must be a plain CUDA transfer record");
static_assert(sizeof(GeoEvent) == 64 && offsetof(GeoEvent, absolute_tick) == 0 &&
                  offsetof(GeoEvent, simulation_seconds) == 8 &&
                  offsetof(GeoEvent, actor_id) == 16 && offsetof(GeoEvent, category) == 20 &&
                  offsetof(GeoEvent, x_mm) == 24 && offsetof(GeoEvent, y_mm) == 32 &&
                  offsetof(GeoEvent, location) == 40 && offsetof(GeoEvent, random_u32) == 56,
              "GeoEvent CUDA transfer ABI must remain explicit");

void check_cuda(cudaError_t status, const char* operation) {
    if (status != cudaSuccess) {
        throw std::runtime_error(std::string(operation) + ": " +
                                 cudaGetErrorName(status) + " (" +
                                 cudaGetErrorString(status) + ")");
    }
}

void validate_block(unsigned block_size) {
    if (block_size != 64 && block_size != 128 && block_size != 256) {
        throw std::invalid_argument("Geospatial block size must be 64, 128, or 256");
    }
}

template <typename T>
class DeviceBuffer {
public:
    DeviceBuffer() = default;
    DeviceBuffer(const DeviceBuffer&) = delete;
    DeviceBuffer& operator=(const DeviceBuffer&) = delete;
    ~DeviceBuffer() noexcept {
        if (data_ != nullptr) {
            const cudaError_t status = cudaFree(data_);
            if (status != cudaSuccess) {
                std::fprintf(stderr, "CUDA cleanup failed: cudaFree(%s): %s (%s)\n",
                             label_, cudaGetErrorName(status), cudaGetErrorString(status));
            }
        }
    }
    void allocate(std::size_t count, const char* label) {
        label_ = label;
        if (count == 0) {
            return;
        }
        bytes_ = count * sizeof(T);
        check_cuda(cudaMalloc(reinterpret_cast<void**>(&data_), bytes_), label_);
    }
    T* data() const noexcept { return data_; }
    std::size_t bytes() const noexcept { return bytes_; }
private:
    T* data_ = nullptr;
    std::size_t bytes_ = 0;
    const char* label_ = "geospatial buffer";
};

class TimedStage {
public:
    TimedStage() {
        check_cuda(cudaEventCreate(&start_), "cudaEventCreate(stage start)");
        const cudaError_t status = cudaEventCreate(&stop_);
        if (status != cudaSuccess) {
            cudaEventDestroy(start_);
            start_ = nullptr;
            check_cuda(status, "cudaEventCreate(stage stop)");
        }
    }
    TimedStage(const TimedStage&) = delete;
    TimedStage& operator=(const TimedStage&) = delete;
    ~TimedStage() noexcept {
        cleanup(start_);
        cleanup(stop_);
    }
    void start() { check_cuda(cudaEventRecord(start_), "cudaEventRecord(stage start)"); }
    void stop() { check_cuda(cudaEventRecord(stop_), "cudaEventRecord(stage stop)"); }
    void synchronize() {
        check_cuda(cudaEventSynchronize(stop_), "cudaEventSynchronize(stage stop)");
    }
    double elapsed() const {
        float milliseconds = 0;
        check_cuda(cudaEventElapsedTime(&milliseconds, start_, stop_),
                   "cudaEventElapsedTime(stage)");
        return milliseconds;
    }
private:
    static void cleanup(cudaEvent_t event) noexcept {
        if (event != nullptr) {
            const cudaError_t status = cudaEventDestroy(event);
            if (status != cudaSuccess) {
                std::fprintf(stderr, "CUDA cleanup failed: cudaEventDestroy: %s (%s)\n",
                             cudaGetErrorName(status), cudaGetErrorString(status));
            }
        }
    }
    cudaEvent_t start_ = nullptr;
    cudaEvent_t stop_ = nullptr;
};

// Exact 128-bit signed products and differences for integer-nanodegree
// orientation. Geographic coordinate differences are <=360e9, so the high
// words are far from int64 limits; neither signed subtraction overflows.
struct WideInteger {
    unsigned long long low;
    long long high;
};
__device__ WideInteger product(std::int64_t a, std::int64_t b) {
    return {static_cast<unsigned long long>(a) * static_cast<unsigned long long>(b),
            __mul64hi(static_cast<long long>(a), static_cast<long long>(b))};
}
__device__ int orientation(Point a, Point b, Point p) {
    const WideInteger left = product(b.longitude_ndeg - a.longitude_ndeg,
                                     p.latitude_ndeg - a.latitude_ndeg);
    const WideInteger right = product(b.latitude_ndeg - a.latitude_ndeg,
                                      p.longitude_ndeg - a.longitude_ndeg);
    const unsigned long long low = left.low - right.low;
    const long long high = left.high - right.high - (left.low < right.low ? 1 : 0);
    return high < 0 ? -1 : ((high != 0 || low != 0) ? 1 : 0);
}
__device__ bool between(std::int64_t value, std::int64_t a, std::int64_t b) {
    return (value >= a && value <= b) || (value >= b && value <= a);
}
__device__ bool inside_polygon(Point point, const Point* ring,
                                std::uint32_t vertex_count) {
    bool inside = false;
    // The host validates a simple, explicitly closed ring. The repeated final
    // vertex is included in vertex_count, so each actual edge appears once.
    for (std::uint32_t index = 1; index < vertex_count; ++index) {
        const Point a = ring[index - 1];
        const Point b = ring[index];
        const int cross = orientation(a, b, point);
        if (cross == 0 && between(point.longitude_ndeg, a.longitude_ndeg, b.longitude_ndeg) &&
            between(point.latitude_ndeg, a.latitude_ndeg, b.latitude_ndeg)) {
            return true;
        }
        if ((a.latitude_ndeg > point.latitude_ndeg) !=
            (b.latitude_ndeg > point.latitude_ndeg)) {
            if ((cross > 0) == (b.latitude_ndeg > a.latitude_ndeg)) {
                inside = !inside;
            }
        }
    }
    return inside;
}

__device__ unsigned long long semantic_subsequence(std::uint32_t purpose,
                                                   std::uint32_t actor_id) {
    return (static_cast<unsigned long long>(kRngDomainVersion) << 56) |
           (static_cast<unsigned long long>(purpose) << 48) | actor_id;
}

__device__ bool sample_integer(curandStatePhilox4_32_10_t& random,
                               std::int64_t minimum, std::int64_t maximum,
                               std::int64_t& result) {
    const unsigned long long span =
        static_cast<unsigned long long>(maximum - minimum) + 1;
    const unsigned long long threshold = (0ull - span) % span;
    // Rejection removes modulo bias. Even this inner draw loop is bounded;
    // exhaustion fails placement rather than replacing it with a biased draw.
    for (unsigned draw = 0; draw < 16; ++draw) {
        const unsigned long long high = curand(&random);
        const unsigned long long low = curand(&random);
        const unsigned long long value = (high << 32) | low;
        if (value >= threshold) {
            result = minimum + static_cast<std::int64_t>(value % span);
            return true;
        }
    }
    return false;
}

__global__ void place_anchors(const DeviceActor* actors, std::uint32_t actor_count,
                              const Point* ring, std::uint32_t vertex_count,
                              Point minimum, Point maximum, bool generated,
                              std::uint64_t seed, std::uint32_t max_attempts,
                              DeviceAnchor* anchors, Counters* counters) {
    const std::uint32_t index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= actor_count) {
        return;
    }
    Point point = actors[index].explicit_anchor;
    if (generated) {
        curandStatePhilox4_32_10_t random;
        curand_init(static_cast<unsigned long long>(seed),
                    semantic_subsequence(static_cast<std::uint32_t>(RngPurpose::Placement),
                                         actors[index].actor.actor_id), 0, &random);
        bool found = false;
        unsigned attempted = 0;
        for (; attempted < max_attempts; ++attempted) {
            if (!sample_integer(random, minimum.longitude_ndeg, maximum.longitude_ndeg,
                                 point.longitude_ndeg) ||
                !sample_integer(random, minimum.latitude_ndeg, maximum.latitude_ndeg,
                                 point.latitude_ndeg)) {
                break;
            }
            if (inside_polygon(point, ring, vertex_count)) {
                found = true;
                ++attempted;
                break;
            }
        }
        atomicAdd(&counters->placement_proposals, static_cast<unsigned long long>(attempted));
        if (!found) {
            atomicAdd(&counters->placement_errors, 1u);
            return;
        }
    }
    if (point.longitude_ndeg < -180 * kDegree || point.longitude_ndeg > 180 * kDegree ||
        point.latitude_ndeg < -kMaxLatitude || point.latitude_ndeg > kMaxLatitude) {
        atomicAdd(&counters->placement_errors, 1u);
        return;
    }
    const double latitude_radians = static_cast<double>(point.latitude_ndeg) *
                                    (1e-9 * kPi / 180.0);
    const double sine = sin(latitude_radians);
    const double denominator = 1.0 - kWgs84EccentricitySquared * sine * sine;
    const double root = sqrt(denominator);
    const double prime_vertical = kWgs84SemiMajorM / root;
    const double meridional = kWgs84SemiMajorM * (1.0 - kWgs84EccentricitySquared) /
                              (denominator * root);
    anchors[index] = {point,
                      (180.0 / kPi) * 1e6 / (prime_vertical * cos(latitude_radians)),
                      (180.0 / kPi) * 1e6 / meridional};
}

__global__ void motion_candidates(const DeviceActor* actors,
                                  const DeviceAnchor* anchors,
                                  std::uint32_t actor_count, std::uint64_t seed,
                                  std::uint64_t start_tick, std::uint32_t tick_seconds,
                                  std::uint32_t candidate_count, GeoEvent* candidates,
                                  std::uint8_t* flags, Counters* counters) {
    const std::uint32_t index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= candidate_count) {
        return;
    }
    flags[index] = 0;
    candidates[index] = GeoEvent{};
    const std::uint32_t actor_index = index % actor_count;
    const Actor actor = actors[actor_index].actor;
    const std::uint64_t tick = start_tick + index / actor_count;
    if (tick % actor.report_interval_ticks != actor.phase_ticks) {
        return;
    }
    atomicAdd(&counters->scheduled, 1ull);
    const std::uint64_t simulation_seconds = tick * tick_seconds;
    const std::int64_t east_mm = static_cast<std::int64_t>(actor.vx_mm_per_s) *
                                static_cast<std::int64_t>(simulation_seconds);
    const std::int64_t north_mm = static_cast<std::int64_t>(actor.vy_mm_per_s) *
                                 static_cast<std::int64_t>(simulation_seconds);
    if (east_mm < -kMaxDisplacementMm || east_mm > kMaxDisplacementMm ||
        north_mm < -kMaxDisplacementMm || north_mm > kMaxDisplacementMm ||
        east_mm * east_mm + north_mm * north_mm >
            kMaxDisplacementMm * kMaxDisplacementMm) {
        atomicAdd(&counters->projection_errors, 1u);
        return;
    }
    const DeviceAnchor anchor = anchors[actor_index];
    const Point point{
        llround(static_cast<double>(anchor.point.longitude_ndeg) +
                static_cast<double>(east_mm) * anchor.longitude_ndeg_per_mm),
        llround(static_cast<double>(anchor.point.latitude_ndeg) +
                static_cast<double>(north_mm) * anchor.latitude_ndeg_per_mm)};
    if (point.longitude_ndeg < -180 * kDegree || point.longitude_ndeg > 180 * kDegree ||
        point.latitude_ndeg < -kMaxLatitude || point.latitude_ndeg > kMaxLatitude) {
        atomicAdd(&counters->projection_errors, 1u);
        return;
    }
    curandStatePhilox4_32_10_t random;
    // This diagnostic word deliberately preserves B2's original identity/time
    // stream. It is separate from semantic-purpose placement/future streams.
    curand_init(static_cast<unsigned long long>(seed), actor.actor_id,
                static_cast<unsigned long long>(tick), &random);
    candidates[index] = {tick, simulation_seconds, actor.actor_id, actor.category,
                          actor.x_mm + east_mm, actor.y_mm + north_mm, point,
                          curand(&random)};
    flags[index] = 1;
}

__global__ void polygon_flags(const GeoEvent* candidates, std::uint8_t* flags,
                              std::uint32_t count, const Point* ring,
                              std::uint32_t vertex_count, Counters* counters) {
    const std::uint32_t index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= count || flags[index] == 0) {
        return;
    }
    if (!inside_polygon(candidates[index].location, ring, vertex_count)) {
        flags[index] = 0;
        atomicAdd(&counters->rejected, 1ull);
    }
}

#ifdef AISMIXER_LAB_GEO_TESTING
__global__ void diagnostic_words(const std::uint32_t* actor_ids, std::uint32_t count,
                                 std::uint64_t seed, std::uint32_t purpose,
                                 std::uint64_t absolute_tick, std::uint32_t* words) {
    const std::uint32_t index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= count) {
        return;
    }
    curandStatePhilox4_32_10_t random;
    curand_init(static_cast<unsigned long long>(seed),
                semantic_subsequence(purpose, actor_ids[index]),
                static_cast<unsigned long long>(absolute_tick), &random);
    words[index] = curand(&random);
}
#endif

DeviceInfo select_device(unsigned block_size) {
    int count = 0;
    check_cuda(cudaGetDeviceCount(&count), "cudaGetDeviceCount");
    if (count == 0) {
        throw std::runtime_error("No CUDA device is available for geospatial execution");
    }
    check_cuda(cudaSetDevice(0), "cudaSetDevice(0)");
    cudaDeviceProp properties{};
    check_cuda(cudaGetDeviceProperties(&properties, 0), "cudaGetDeviceProperties(0)");
    cudaFuncAttributes attributes{};
    check_cuda(cudaFuncGetAttributes(&attributes, motion_candidates),
               "cudaFuncGetAttributes(geospatial motion): verify compiled GPU architecture");
    if (properties.maxThreadsPerBlock < static_cast<int>(block_size) ||
        attributes.maxThreadsPerBlock < static_cast<int>(block_size)) {
        throw std::runtime_error("CUDA device 0 cannot execute the geospatial block size");
    }
    int runtime = 0;
    int driver = 0;
    check_cuda(cudaRuntimeGetVersion(&runtime), "cudaRuntimeGetVersion");
    check_cuda(cudaDriverGetVersion(&driver), "cudaDriverGetVersion");
    return {properties.name, properties.major, properties.minor, runtime, driver};
}

void require_free_memory(std::size_t bytes) {
    std::size_t available = 0;
    std::size_t total = 0;
    check_cuda(cudaMemGetInfo(&available, &total), "cudaMemGetInfo(geospatial budget)");
    if (bytes > available) {
        throw std::runtime_error("Geospatial device allocation requires " +
                                 std::to_string(bytes) + " bytes, but only " +
                                 std::to_string(available) + " bytes are free");
    }
}

}  // namespace

struct Session::Impl {
    Impl(const Domain& domain, const std::vector<Actor>& input_actors,
         const std::vector<Anchor>& input_anchors, Seed selected_seed,
         Options requested_options) : actors(input_actors), seed(selected_seed),
                                      options(requested_options) {
        const auto beginning = Clock::now();
        validate_block(options.block_size);
        if (options.placement_attempts == 0 || options.placement_attempts > 65536) {
            throw std::invalid_argument("placement_attempts must be in 1..65536");
        }
        if (options.anchor_mode != AnchorMode::Explicit &&
            options.anchor_mode != AnchorMode::CudaPlacement) {
            throw std::invalid_argument("Invalid geographic anchor mode");
        }
        if (seed.mode != SeedMode::Fixed && seed.mode != SeedMode::Fresh) {
            throw std::invalid_argument("Invalid geographic seed mode");
        }
        validate_domain(domain);
        validate_run(actors, 0, 0, 1, options.candidate_capacity);
        if (options.anchor_mode == AnchorMode::Explicit) {
            validate_anchors(actors, input_anchors);
        } else if (!input_anchors.empty()) {
            throw std::invalid_argument("CUDA placement does not accept explicit anchors");
        }
        std::sort(actors.begin(), actors.end(),
                  [](const Actor& a, const Actor& b) { return a.actor_id < b.actor_id; });
        std::vector<Anchor> anchors = input_anchors;
        std::sort(anchors.begin(), anchors.end(),
                  [](const Anchor& a, const Anchor& b) { return a.actor_id < b.actor_id; });
        std::vector<DeviceActor> upload;
        upload.reserve(actors.size());
        for (std::size_t index = 0; index < actors.size(); ++index) {
            upload.push_back({actors[index], options.anchor_mode == AnchorMode::Explicit
                                                ? anchors[index].tick_zero : Point{0, 0}});
        }
        vertex_count = static_cast<std::uint32_t>(domain.ring.size());
        const std::size_t capacity = actors.empty() ? 0 : options.candidate_capacity;
        info = select_device(options.block_size);
        stages = std::make_unique<Stages>();
        const std::size_t base_bytes = upload.size() * sizeof(DeviceActor) +
                                       actors.size() * sizeof(DeviceAnchor) +
                                       domain.ring.size() * sizeof(Point) +
                                       capacity * (2 * sizeof(GeoEvent) + sizeof(std::uint8_t)) +
                                       sizeof(Counters) + sizeof(int);
        require_free_memory(base_bytes);
        device_actors.allocate(upload.size(), "cudaMalloc(geospatial actors)");
        device_anchors.allocate(actors.size(), "cudaMalloc(geospatial anchors)");
        device_ring.allocate(domain.ring.size(), "cudaMalloc(geospatial ring)");
        candidates.allocate(capacity, "cudaMalloc(geospatial candidates)");
        selected.allocate(capacity, "cudaMalloc(geospatial selected)");
        flags.allocate(capacity, "cudaMalloc(geospatial flags)");
        counters.allocate(1, "cudaMalloc(geospatial counters)");
        selected_count.allocate(1, "cudaMalloc(geospatial selected count)");
        if (capacity != 0) {
            check_cuda(cub::DeviceSelect::Flagged(nullptr, scratch_bytes,
                                                  candidates.data(), flags.data(), selected.data(),
                                                  selected_count.data(), static_cast<int>(capacity)),
                       "CUB DeviceSelect::Flagged scratch query");
            require_free_memory(scratch_bytes);
            scratch.allocate(scratch_bytes, "cudaMalloc(CUB stable-selection scratch)");
        }
        setup.device_bytes = base_bytes + scratch_bytes;
        setup.h2d_bytes = upload.size() * sizeof(DeviceActor) +
                           domain.ring.size() * sizeof(Point);
        stages->upload.start();
        if (!upload.empty()) {
            check_cuda(cudaMemcpy(device_actors.data(), upload.data(),
                                  upload.size() * sizeof(DeviceActor), cudaMemcpyHostToDevice),
                       "cudaMemcpy(geospatial actors to device)");
        }
        check_cuda(cudaMemcpy(device_ring.data(), domain.ring.data(),
                              domain.ring.size() * sizeof(Point), cudaMemcpyHostToDevice),
                   "cudaMemcpy(geospatial ring to device)");
        stages->upload.stop();
        check_cuda(cudaMemset(counters.data(), 0, sizeof(Counters)),
                   "cudaMemset(geospatial placement counters)");
        if (!actors.empty()) {
            Point minimum = domain.ring.front();
            Point maximum = minimum;
            for (const Point point : domain.ring) {
                minimum.longitude_ndeg = std::min(minimum.longitude_ndeg, point.longitude_ndeg);
                minimum.latitude_ndeg = std::min(minimum.latitude_ndeg, point.latitude_ndeg);
                maximum.longitude_ndeg = std::max(maximum.longitude_ndeg, point.longitude_ndeg);
                maximum.latitude_ndeg = std::max(maximum.latitude_ndeg, point.latitude_ndeg);
            }
            stages->placement.start();
            const auto actor_count = static_cast<std::uint32_t>(actors.size());
            const unsigned blocks = (actor_count + options.block_size - 1) / options.block_size;
            place_anchors<<<blocks, options.block_size>>>(
                device_actors.data(), actor_count, device_ring.data(), vertex_count,
                minimum, maximum, options.anchor_mode == AnchorMode::CudaPlacement,
                seed.value, options.placement_attempts, device_anchors.data(), counters.data());
            check_cuda(cudaGetLastError(), "place_anchors launch");
            stages->placement.stop();
            stages->diagnostics.start();
            Counters status{};
            check_cuda(cudaMemcpy(&status, counters.data(), sizeof(status), cudaMemcpyDeviceToHost),
                       "cudaMemcpy(geospatial placement diagnostics)");
            stages->diagnostics.stop();
            stages->diagnostics.synchronize();
            if (status.placement_errors != 0) {
                throw std::runtime_error("CUDA geographic placement failed for " +
                                         std::to_string(status.placement_errors) +
                                         " actor(s): rejection budget exhausted or invalid anchor");
            }
            setup.placement_ms = stages->placement.elapsed();
            setup.d2h_ms = stages->diagnostics.elapsed();
            setup.d2h_diagnostic_bytes = sizeof(Counters);
        } else {
            stages->upload.synchronize();
        }
        setup.h2d_ms = stages->upload.elapsed();
        setup.setup_wall_ms = std::chrono::duration<double, std::milli>(Clock::now() - beginning).count();
        setup.wall_ms = setup.setup_wall_ms;
    }

    Result run(std::uint64_t start_tick, std::uint64_t tick_count,
               std::uint32_t tick_seconds) {
        const auto beginning = Clock::now();
        validate_run(actors, start_tick, tick_count, tick_seconds, options.candidate_capacity);
        check_cuda(cudaSetDevice(0), "cudaSetDevice(0) for geospatial session");
        Result result;
        result.metrics.device_bytes = setup.device_bytes;
        result.metrics.candidates = actors.size() * tick_count;
        result.metrics.uncompacted_record_bytes = result.metrics.candidates * sizeof(GeoEvent);
        if (result.metrics.candidates == 0) {
            result.metrics.wall_ms = std::chrono::duration<double, std::milli>(Clock::now() - beginning).count();
            return result;
        }
        const auto count = static_cast<std::uint32_t>(result.metrics.candidates);
        const auto actor_count = static_cast<std::uint32_t>(actors.size());
        const unsigned blocks = (count + options.block_size - 1) / options.block_size;
        check_cuda(cudaMemset(counters.data(), 0, sizeof(Counters)),
                   "cudaMemset(geospatial run counters)");
        stages->motion.start();
        motion_candidates<<<blocks, options.block_size>>>(
            device_actors.data(), device_anchors.data(), actor_count, seed.value,
            start_tick, tick_seconds, count, candidates.data(), flags.data(), counters.data());
        check_cuda(cudaGetLastError(), "motion_candidates launch");
        stages->motion.stop();
        stages->predicate.start();
        polygon_flags<<<blocks, options.block_size>>>(
            candidates.data(), flags.data(), count, device_ring.data(), vertex_count, counters.data());
        check_cuda(cudaGetLastError(), "polygon_flags launch");
        stages->predicate.stop();
        stages->compaction.start();
        std::size_t available_scratch = scratch_bytes;
        check_cuda(cub::DeviceSelect::Flagged(scratch.data(), available_scratch,
                                              candidates.data(), flags.data(), selected.data(),
                                              selected_count.data(), static_cast<int>(count)),
                   "CUB DeviceSelect::Flagged stable compaction");
        check_cuda(cudaGetLastError(), "stable-selection launch");
        stages->compaction.stop();
        stages->diagnostics.start();
        Counters status{};
        int accepted = 0;
        check_cuda(cudaMemcpy(&status, counters.data(), sizeof(status), cudaMemcpyDeviceToHost),
                   "cudaMemcpy(geospatial small counters)");
        check_cuda(cudaMemcpy(&accepted, selected_count.data(), sizeof(accepted), cudaMemcpyDeviceToHost),
                   "cudaMemcpy(geospatial selected count)");
        stages->diagnostics.stop();
        stages->diagnostics.synchronize();
        if (status.projection_errors != 0) {
            throw std::runtime_error("CUDA geographic transform rejected " +
                                     std::to_string(status.projection_errors) +
                                     " report(s) outside supported projection coordinates/displacement");
        }
        if (accepted < 0 || static_cast<std::uint32_t>(accepted) > count ||
            status.scheduled > count || status.rejected > status.scheduled ||
            static_cast<unsigned long long>(accepted) + status.rejected != status.scheduled) {
            throw std::runtime_error("CUDA geospatial selection counters are inconsistent");
        }
        result.metrics.scheduled = status.scheduled;
        result.metrics.accepted = static_cast<std::uint64_t>(accepted);
        result.metrics.rejected = status.rejected;
        result.metrics.unscheduled = count - status.scheduled;
        result.metrics.d2h_diagnostic_bytes = sizeof(Counters) + sizeof(int);
        result.metrics.d2h_record_bytes = static_cast<std::uint64_t>(accepted) * sizeof(GeoEvent);
        result.events.resize(static_cast<std::size_t>(accepted));
        // No candidate array, rejected records, or flags cross to the host.
        // Allocate the accepted host vector before timing its record transfer.
        if (accepted != 0) {
            stages->records.start();
            check_cuda(cudaMemcpy(result.events.data(), selected.data(),
                                  result.events.size() * sizeof(GeoEvent), cudaMemcpyDeviceToHost),
                       "cudaMemcpy(compact geographic events to host)");
            stages->records.stop();
            stages->records.synchronize();
            result.metrics.d2h_ms = stages->records.elapsed();
        }
        result.metrics.motion_ms = stages->motion.elapsed();
        result.metrics.predicate_ms = stages->predicate.elapsed();
        result.metrics.compaction_ms = stages->compaction.elapsed();
        result.metrics.d2h_ms += stages->diagnostics.elapsed();
        result.metrics.wall_ms = std::chrono::duration<double, std::milli>(Clock::now() - beginning).count();
        return result;
    }

    struct Stages {
        TimedStage upload;
        TimedStage placement;
        TimedStage motion;
        TimedStage predicate;
        TimedStage compaction;
        TimedStage diagnostics;
        TimedStage records;
    };
    std::vector<Actor> actors;
    Seed seed;
    Options options;
    std::uint32_t vertex_count = 0;
    DeviceInfo info;
    Metrics setup;
    std::unique_ptr<Stages> stages;
    DeviceBuffer<DeviceActor> device_actors;
    DeviceBuffer<DeviceAnchor> device_anchors;
    DeviceBuffer<Point> device_ring;
    DeviceBuffer<GeoEvent> candidates;
    DeviceBuffer<GeoEvent> selected;
    DeviceBuffer<std::uint8_t> flags;
    DeviceBuffer<Counters> counters;
    DeviceBuffer<int> selected_count;
    DeviceBuffer<std::uint8_t> scratch;
    std::size_t scratch_bytes = 0;
};

Session::Session(const Domain& domain, const std::vector<Actor>& actors,
                 const std::vector<Anchor>& anchors, Seed seed, Options options)
    : impl_(std::make_unique<Impl>(domain, actors, anchors, seed, options)) {}
Session::~Session() = default;
Session::Session(Session&&) noexcept = default;
Session& Session::operator=(Session&&) noexcept = default;

Result Session::run(std::uint64_t start_tick, std::uint64_t tick_count,
                    std::uint32_t tick_seconds) {
    if (!impl_) {
        throw std::logic_error("Cannot run a moved-from geospatial session");
    }
    return impl_->run(start_tick, tick_count, tick_seconds);
}
const Metrics& Session::setup_metrics() const {
    if (!impl_) {
        throw std::logic_error("Cannot inspect a moved-from geospatial session");
    }
    return impl_->setup;
}
const DeviceInfo& Session::device_info() const {
    if (!impl_) {
        throw std::logic_error("Cannot inspect a moved-from geospatial session");
    }
    return impl_->info;
}

#ifdef AISMIXER_LAB_GEO_TESTING
std::vector<std::uint32_t> diagnostic_random_words(
    const std::vector<std::uint32_t>& actor_ids, std::uint64_t seed,
    RngPurpose purpose, std::uint64_t absolute_tick, unsigned block_size) {
    validate_block(block_size);
    if (actor_ids.size() > kMaxSceneActors || absolute_tick > kMaxAbsoluteTick) {
        throw std::invalid_argument("Random diagnostic IDs/tick exceed scene bounds");
    }
    // Host validation and device key calculation remain independent paths.
    (void)rng_subsequence(purpose, 0);
    (void)select_device(block_size);
    if (actor_ids.empty()) {
        return {};
    }
    DeviceBuffer<std::uint32_t> ids;
    DeviceBuffer<std::uint32_t> words;
    ids.allocate(actor_ids.size(), "cudaMalloc(diagnostic actor IDs)");
    words.allocate(actor_ids.size(), "cudaMalloc(diagnostic random words)");
    check_cuda(cudaMemcpy(ids.data(), actor_ids.data(), ids.bytes(), cudaMemcpyHostToDevice),
               "cudaMemcpy(diagnostic actor IDs)");
    const auto count = static_cast<std::uint32_t>(actor_ids.size());
    const unsigned blocks = (count + block_size - 1) / block_size;
    diagnostic_words<<<blocks, block_size>>>(ids.data(), count, seed,
        static_cast<std::uint32_t>(purpose), absolute_tick, words.data());
    check_cuda(cudaGetLastError(), "diagnostic_words launch");
    std::vector<std::uint32_t> result(actor_ids.size());
    check_cuda(cudaMemcpy(result.data(), words.data(), words.bytes(), cudaMemcpyDeviceToHost),
               "cudaMemcpy(diagnostic random words)");
    return result;
}
#endif

}  // namespace aismixer::lab::geo
