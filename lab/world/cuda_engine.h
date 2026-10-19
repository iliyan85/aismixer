#pragma once

#include "geodesy.h"
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <type_traits>
#include <vector>

namespace aismixer::lab::world {
inline constexpr std::size_t kMaxCudaBatch = 65536;
struct PositionQuery { Point origin; std::int64_t initial_bearing_ndeg = 0; double distance_m = 0; };
struct PositionResult { Point location; std::int64_t final_bearing_ndeg = 0; };
struct RegionSpec {
    std::uint32_t region_id = 0;
    Point center;
    std::int64_t radius_mm = 0, min_elevation_mm = 0, max_elevation_mm = 0;
    std::uint32_t count = 0;
};
// Raw elevation remains in the host region's explicitly recorded datum.
// Sampling performs no AMSL/ellipsoid conversion or suitability validation.
struct RegionSample {
    // Stable positive ordinal 1..region.count, also the RNG episode key.
    std::uint32_t region_id = 0, ordinal = 0;
    Point location;
    std::int64_t elevation_mm = 0;
};
static_assert(std::is_trivially_copyable_v<PositionQuery> && sizeof(PositionQuery) == 32);
static_assert(std::is_trivially_copyable_v<PositionResult> && sizeof(PositionResult) == 24);
static_assert(std::is_trivially_copyable_v<RegionSpec> && sizeof(RegionSpec) == 56);
static_assert(std::is_trivially_copyable_v<RegionSample> && sizeof(RegionSample) == 32);
struct Options { std::size_t capacity = kMaxCudaBatch; unsigned block_size = 256; };
struct Metrics {
    double setup_wall_ms = 0, h2d_ms = 0, position_ms = 0, sampling_ms = 0, d2h_ms = 0, wall_ms = 0;
    std::uint64_t position_queries = 0, sampled_receivers = 0, launches = 0;
    std::uint64_t h2d_bytes = 0, d2h_bytes = 0, diagnostic_bytes = 0, device_bytes = 0;
    std::size_t peak_batch = 0;
};
struct DeviceInfo { std::string name; int major = 0, minor = 0, runtime_version = 0, driver_version = 0; };
// Synchronous, externally serialized production CUDA session. Device 0 is
// required even for an empty world. Buffers are allocated once and reused;
// queries carry immutable sparse host leg snapshots, not accumulated GPU steps.
// Regions are uploaded once per sampling call, all samples are calculated on
// device, and returned in (region_id,ordinal) order. No CPU production fallback.
class Session {
public:
    explicit Session(Options options = {});
    ~Session();
    Session(Session&&) noexcept;
    Session& operator=(Session&&) noexcept;
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    std::vector<PositionResult> evaluate(const std::vector<PositionQuery>& queries);
    std::vector<RegionSample> sample_regions(std::uint64_t seed, const std::vector<RegionSpec>& regions);
    const Metrics& metrics() const;
    const DeviceInfo& device_info() const;
private:
    struct Impl; std::unique_ptr<Impl> impl_;
};
}  // namespace aismixer::lab::world
