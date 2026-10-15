#pragma once

#include "scene_api.h"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace aismixer::lab::geo {

inline constexpr std::int64_t kDegree = 1000000000;
inline constexpr std::int64_t kMaxLatitude = 70 * kDegree;
inline constexpr std::size_t kMaxPolygonVertices = 1024;
inline constexpr std::size_t kMaxReceivers = 10000;
inline constexpr std::int64_t kMaxDisplacementMm = 1000000;
inline constexpr std::uint32_t kRngDomainVersion = 1;

// EPSG:4326 / WGS84, longitude first. Integer nanodegrees, no AIS sentinels.
struct Point {
    std::int64_t longitude_ndeg;
    std::int64_t latitude_ndeg;
};
struct Domain {
    std::uint32_t schema_version = 1;
    std::string profile_id;
    std::string provenance;
    // Explicitly closed simple ring; first point is repeated exactly at end.
    // Inclusive boundary, straight lon/lat edges; not a geodesic water mask.
    std::vector<Point> ring;
};
struct Anchor {
    std::uint32_t actor_id;
    Point tick_zero;
};
struct Receiver {
    std::uint32_t receiver_id;
    Point location;
    std::int64_t antenna_height_mm;
    std::string height_datum;  // "WGS84_ellipsoid" in version 1
};
enum class SeedMode { Fixed, Fresh };
struct Seed {
    SeedMode mode;
    std::uint64_t value;
};
enum class RngPurpose : std::uint32_t {
    Placement = 1, Motion = 2, Atmosphere = 3, ReceiverFading = 4, Failure = 5
};
enum class AnchorMode { Explicit, CudaPlacement };
struct Options {
    AnchorMode anchor_mode = AnchorMode::Explicit;
    std::size_t candidate_capacity = static_cast<std::size_t>(kMaxSceneCandidates);
    std::uint32_t placement_attempts = 4096;
    unsigned block_size = 256;  // 64, 128, 256; diagnostic launch-shape control
};
struct GeoEvent {
    std::uint64_t absolute_tick;
    std::uint64_t simulation_seconds;
    std::uint32_t actor_id;
    ActorCategory category;
    std::int64_t x_mm;
    std::int64_t y_mm;
    Point location;
    std::uint32_t random_u32;  // B2 diagnostic stream, never an AIS field
};
struct Metrics {
    double setup_wall_ms = 0;
    double h2d_ms = 0;
    double placement_ms = 0;
    double motion_ms = 0;
    double predicate_ms = 0;
    double compaction_ms = 0;
    double d2h_ms = 0;
    double wall_ms = 0;
    std::uint64_t candidates = 0;
    std::uint64_t scheduled = 0;
    std::uint64_t accepted = 0;
    std::uint64_t rejected = 0;  // scheduled reports outside polygon
    std::uint64_t unscheduled = 0;
    std::uint64_t h2d_bytes = 0;
    std::uint64_t d2h_record_bytes = 0;
    std::uint64_t d2h_diagnostic_bytes = 0;
    std::uint64_t uncompacted_record_bytes = 0;
    std::uint64_t device_bytes = 0;  // owned buffers, including CUB scratch
};
struct Result {
    std::vector<GeoEvent> events;
    Metrics metrics;
};
struct DeviceInfo {
    std::string name;
    int major = 0;
    int minor = 0;
    int runtime_version = 0;
    int driver_version = 0;
};

// All host validation is CUDA-free. Invalid configuration throws
// invalid_argument before device allocation; resource/device failures throw
// runtime_error. No CPU simulation or placement fallback exists.
void validate_domain(const Domain& domain);
void validate_anchors(const std::vector<Actor>& actors,
                      const std::vector<Anchor>& anchors);
void validate_receivers(const std::vector<Receiver>& receivers);
void validate_run(const std::vector<Actor>& actors, std::uint64_t start_tick,
                  std::uint64_t tick_count, std::uint32_t tick_seconds,
                  std::size_t candidate_capacity);
Seed select_seed(SeedMode mode, std::uint64_t fixed_value = 0);
std::uint64_t rng_subsequence(RngPurpose purpose, std::uint32_t actor_id);
std::uint64_t domain_digest(const Domain& domain);
std::uint64_t anchor_digest(const std::vector<Anchor>& anchors);
std::uint64_t event_digest(const std::vector<GeoEvent>& events);
// Nearest 1/10000-minute, exact integer rounding; ties away from zero.
// Reject impossible positions; never saturate or synthesize unavailable codes.
std::int64_t ais_coordinate(std::int64_t nanodegrees, bool longitude);

class Session {
public:
    Session(const Domain& domain, const std::vector<Actor>& actors,
            const std::vector<Anchor>& anchors, Seed seed, Options options = {});
    ~Session();
    Session(Session&&) noexcept;
    Session& operator=(Session&&) noexcept;
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    // Absolute B2 ticks; reports remain (tick % interval == phase).
    // GPU transforms displacement vx/vy * absolute_tick * tick_seconds,
    // not the initial local x/y twice. Total radial displacement <=1000m.
    Result run(std::uint64_t start_tick, std::uint64_t tick_count,
               std::uint32_t tick_seconds);
    const Metrics& setup_metrics() const;
    const DeviceInfo& device_info() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

#ifdef AISMIXER_LAB_GEO_TESTING
// Device Philox diagnostics for independent purpose-key/launch-order tests.
std::vector<std::uint32_t> diagnostic_random_words(
    const std::vector<std::uint32_t>& actor_ids, std::uint64_t seed,
    RngPurpose purpose, std::uint64_t absolute_tick, unsigned block_size);
#endif

}  // namespace aismixer::lab::geo
