#include "geo_api.h"

#include <algorithm>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <bcrypt.h>
#endif

namespace aismixer::lab::geo {
namespace {

[[noreturn]] void invalid(std::string_view field, std::string_view reason) {
    throw std::invalid_argument(std::string(field) + ": " + std::string(reason));
}

bool same(Point first, Point second) {
    return first.longitude_ndeg == second.longitude_ndeg &&
           first.latitude_ndeg == second.latitude_ndeg;
}

void validate_point(Point point, std::string_view field) {
    if (point.longitude_ndeg < -180 * kDegree || point.longitude_ndeg > 180 * kDegree) {
        invalid(field, "longitude must be in [-180,180] degrees, in nanodegrees");
    }
    if (point.latitude_ndeg < -kMaxLatitude || point.latitude_ndeg > kMaxLatitude) {
        invalid(field, "latitude must be in the qualified [-70,70] degree region");
    }
}

void validate_text(std::string_view value, std::size_t maximum, std::string_view field) {
    if (value.empty() || value.size() > maximum) {
        invalid(field, "must contain 1.." + std::to_string(maximum) + " bytes");
    }
    bool nonspace = false;
    for (const unsigned char byte : value) {
        if (byte < 32 || byte == 127) invalid(field, "control characters are unsupported");
        if (byte != ' ') nonspace = true;
    }
    if (!nonspace) invalid(field, "must contain a non-space character");
}

// Portable exact product comparison, rather than MSVC's double-width long
// double or a nonportable __int128. Validated coordinate differences fit int64;
// their products may need up to 77 bits. Only comparison/sign is needed.
struct Wide {
    std::uint64_t high;
    std::uint64_t low;
};

Wide multiply(std::uint64_t first, std::uint64_t second) {
    constexpr std::uint64_t mask = 0xffffffffu;
    const std::uint64_t a0 = first & mask;
    const std::uint64_t a1 = first >> 32;
    const std::uint64_t b0 = second & mask;
    const std::uint64_t b1 = second >> 32;
    const std::uint64_t initial = a0 * b0;
    const std::uint64_t middle = a1 * b0 + (initial >> 32);
    const std::uint64_t last = a0 * b1 + (middle & mask);
    return {a1 * b1 + (middle >> 32) + (last >> 32),
            (last << 32) | (initial & mask)};
}

int compare(Wide first, Wide second) {
    if (first.high != second.high) return first.high < second.high ? -1 : 1;
    if (first.low != second.low) return first.low < second.low ? -1 : 1;
    return 0;
}

std::uint64_t magnitude(std::int64_t value) {
    // Coordinate differences never approach INT64_MIN; this form is still
    // defined for the entire int64 range and introduces no signed negation UB.
    return value < 0 ? std::uint64_t{0} - static_cast<std::uint64_t>(value)
                     : static_cast<std::uint64_t>(value);
}

int product_sign(std::int64_t first, std::int64_t second) {
    if (first == 0 || second == 0) return 0;
    return (first < 0) != (second < 0) ? -1 : 1;
}

int orientation(Point a, Point b, Point c) {
    const auto abx = b.longitude_ndeg - a.longitude_ndeg;
    const auto aby = b.latitude_ndeg - a.latitude_ndeg;
    const auto acx = c.longitude_ndeg - a.longitude_ndeg;
    const auto acy = c.latitude_ndeg - a.latitude_ndeg;
    const int first_sign = product_sign(abx, acy);
    const int second_sign = product_sign(aby, acx);
    if (first_sign != second_sign) return first_sign < second_sign ? -1 : 1;
    if (first_sign == 0) return 0;
    return first_sign * compare(multiply(magnitude(abx), magnitude(acy)),
                                multiply(magnitude(aby), magnitude(acx)));
}

bool on_segment(Point a, Point b, Point point) {
    return point.longitude_ndeg >= std::min(a.longitude_ndeg, b.longitude_ndeg) &&
           point.longitude_ndeg <= std::max(a.longitude_ndeg, b.longitude_ndeg) &&
           point.latitude_ndeg >= std::min(a.latitude_ndeg, b.latitude_ndeg) &&
           point.latitude_ndeg <= std::max(a.latitude_ndeg, b.latitude_ndeg);
}

bool intersects(Point a, Point b, Point c, Point d) {
    const int ac = orientation(a, b, c);
    const int ad = orientation(a, b, d);
    const int ca = orientation(c, d, a);
    const int cb = orientation(c, d, b);
    if (ac == 0 && on_segment(a, b, c)) return true;
    if (ad == 0 && on_segment(a, b, d)) return true;
    if (ca == 0 && on_segment(c, d, a)) return true;
    if (cb == 0 && on_segment(c, d, b)) return true;
    return ac != 0 && ad != 0 && ca != 0 && cb != 0 && ac != ad && ca != cb;
}

class Digest {
public:
    void unsigned_value(std::uint64_t value, unsigned bytes) {
        for (unsigned index = 0; index < bytes; ++index) {
            octet(static_cast<std::uint8_t>(value & 255u));
            value >>= 8;
        }
    }
    void signed_value(std::int64_t value) { unsigned_value(static_cast<std::uint64_t>(value), 8); }
    void text(std::string_view value) {
        unsigned_value(value.size(), 8);
        for (const unsigned char byte : value) octet(byte);
    }
    void point(Point value) {
        signed_value(value.longitude_ndeg);
        signed_value(value.latitude_ndeg);
    }
    std::uint64_t value() const { return state_; }
private:
    void octet(std::uint8_t byte) {
        state_ ^= byte;
        state_ *= UINT64_C(1099511628211);
    }
    std::uint64_t state_ = UINT64_C(14695981039346656037);
};

}  // namespace

void validate_domain(const Domain& domain) {
    if (domain.schema_version != 1) invalid("domain.schema_version", "only version 1 is supported");
    validate_text(domain.profile_id, 128, "domain.profile_id");
    validate_text(domain.provenance, 4096, "domain.provenance");
    if (domain.ring.size() < 4 || domain.ring.size() > kMaxPolygonVertices + 1) {
        invalid("domain.ring", "requires 3..1024 nonclosing vertices plus explicit closing point");
    }
    for (std::size_t index = 0; index < domain.ring.size(); ++index) {
        validate_point(domain.ring[index], "domain.ring[" + std::to_string(index) + "]");
    }
    if (!same(domain.ring.front(), domain.ring.back())) {
        invalid("domain.ring", "first point must be repeated exactly at the end");
    }
    const std::size_t count = domain.ring.size() - 1;
    std::int64_t minimum = domain.ring.front().longitude_ndeg;
    std::int64_t maximum = minimum;
    for (std::size_t index = 0; index < count; ++index) {
        minimum = std::min(minimum, domain.ring[index].longitude_ndeg);
        maximum = std::max(maximum, domain.ring[index].longitude_ndeg);
        for (std::size_t prior = 0; prior < index; ++prior) {
            if (same(domain.ring[index], domain.ring[prior])) {
                invalid("domain.ring", "repeated nonclosing vertices and degenerate edges are unsupported");
            }
        }
        if (orientation(domain.ring[(index + count - 1) % count],
                        domain.ring[index], domain.ring[(index + 1) % count]) == 0) {
            invalid("domain.ring", "consecutive collinear edges are degenerate/unsupported");
        }
    }
    if (maximum - minimum > 60 * kDegree) {
        invalid("domain.ring", "longitude span exceeds 60 degrees; antimeridian crossing is unsupported");
    }
    for (std::size_t first = 0; first < count; ++first) {
        for (std::size_t second = first + 1; second < count; ++second) {
            if (second == first + 1 || (first == 0 && second + 1 == count)) continue;
            if (intersects(domain.ring[first], domain.ring[first + 1],
                           domain.ring[second], domain.ring[second + 1])) {
                invalid("domain.ring", "nonadjacent edges intersect or touch");
            }
        }
    }
}

void validate_anchors(const std::vector<Actor>& actors, const std::vector<Anchor>& anchors) {
    if (actors.size() > kMaxSceneActors || anchors.size() != actors.size()) {
        invalid("anchors", "requires exactly one anchor per actor within the B2 population bound");
    }
    std::unordered_set<std::uint32_t> identities;
    for (const auto& actor : actors) {
        if (!identities.insert(actor.actor_id).second) invalid("actors.actor_id", "duplicate actor identity");
    }
    std::unordered_set<std::uint32_t> seen;
    for (const auto& anchor : anchors) {
        if (identities.find(anchor.actor_id) == identities.end()) invalid("anchors.actor_id", "unmatched actor identity");
        if (!seen.insert(anchor.actor_id).second) invalid("anchors.actor_id", "duplicate actor anchor");
        validate_point(anchor.tick_zero, "anchors.tick_zero");
    }
}

void validate_receivers(const std::vector<Receiver>& receivers) {
    if (receivers.size() > kMaxReceivers) invalid("receivers", "catalog exceeds 10000 stations");
    std::unordered_set<std::uint32_t> identities;
    for (const auto& receiver : receivers) {
        if (!identities.insert(receiver.receiver_id).second) invalid("receiver_id", "duplicate receiver identity");
        validate_point(receiver.location, "receiver.location");
        if (receiver.height_datum != "WGS84_ellipsoid") invalid("receiver.height_datum", "requires WGS84_ellipsoid");
        if (receiver.antenna_height_mm < -1000000 || receiver.antenna_height_mm > 10000000) {
            invalid("receiver.antenna_height_mm", "ellipsoidal antenna height must be in [-1000000,10000000] mm");
        }
    }
}

void validate_run(const std::vector<Actor>& actors, std::uint64_t start_tick,
                  std::uint64_t tick_count, std::uint32_t tick_seconds,
                  std::size_t candidate_capacity) {
    if (candidate_capacity == 0 || candidate_capacity > kMaxSceneCandidates) {
        invalid("candidate_capacity", "must be in [1,1000000]");
    }
    if (actors.size() > kMaxSceneActors) invalid("actors", "population exceeds the B2 bound");
    if (tick_count > kMaxSceneTicks) invalid("tick_count", "must be at most 3600");
    if (!actors.empty() && tick_count > kMaxSceneCandidates / actors.size()) {
        invalid("tick_count", "actor-tick candidates exceed the B2 bound of 1000000");
    }
    if (!actors.empty() && tick_count > candidate_capacity / actors.size()) {
        invalid("candidate_capacity", "actor-tick candidates exceed this session's configured buffer capacity");
    }
    if (tick_seconds == 0 || tick_seconds > kMaxTickSeconds) invalid("tick_seconds", "must be in [1,60]");
    if (start_tick > kMaxAbsoluteTick) invalid("start_tick", "absolute tick exceeds the B2 bound");
    if (tick_count != 0 && tick_count - 1 > kMaxAbsoluteTick - start_tick) {
        invalid("tick_count", "last included absolute tick exceeds the B2 bound");
    }
    const std::uint64_t last_tick = tick_count == 0 ? start_tick : start_tick + tick_count - 1;
    if (last_tick > std::numeric_limits<std::uint64_t>::max() / tick_seconds) {
        invalid("simulation_seconds", "absolute tick * tick_seconds overflows");
    }
    const std::uint64_t seconds = last_tick * tick_seconds;
    std::unordered_set<std::uint32_t> identities;
    for (const auto& actor : actors) {
        if (!identities.insert(actor.actor_id).second) invalid("actor_id", "duplicate actor identity");
        if (static_cast<std::uint32_t>(actor.category) > static_cast<std::uint32_t>(ActorCategory::Emergency)) {
            invalid("category", "unsupported actor category");
        }
        if (actor.report_interval_ticks == 0 || actor.phase_ticks >= actor.report_interval_ticks) {
            invalid("report_schedule", "requires positive interval and phase less than interval");
        }
        if (actor.x_mm < -kMaxInitialPositionMm || actor.x_mm > kMaxInitialPositionMm ||
            actor.y_mm < -kMaxInitialPositionMm || actor.y_mm > kMaxInitialPositionMm) {
            invalid("initial_position", "local B2 position exceeds its millimetre bound");
        }
        if (actor.vx_mm_per_s < -kMaxVelocityMmPerS || actor.vx_mm_per_s > kMaxVelocityMmPerS ||
            actor.vy_mm_per_s < -kMaxVelocityMmPerS || actor.vy_mm_per_s > kMaxVelocityMmPerS) {
            invalid("velocity", "local B2 velocity exceeds its mm/s bound");
        }
        if ((actor.category == ActorCategory::BaseStation || actor.category == ActorCategory::AtoN) &&
            (actor.vx_mm_per_s != 0 || actor.vy_mm_per_s != 0)) {
            invalid("velocity", "BaseStation and AtoN actors must be stationary");
        }
        // Validate the whole trajectory, not just scheduled reports. Signed
        // velocities use magnitudes; initial local x/y is not displacement.
        if (tick_count != 0) {
            const std::uint64_t vx = magnitude(actor.vx_mm_per_s);
            const std::uint64_t vy = magnitude(actor.vy_mm_per_s);
            if ((vx != 0 && seconds > static_cast<std::uint64_t>(kMaxDisplacementMm) / vx) ||
                (vy != 0 && seconds > static_cast<std::uint64_t>(kMaxDisplacementMm) / vy)) {
                invalid("displacement", "absolute-tick displacement exceeds the 1000m tangent limit");
            }
            const std::uint64_t east = vx * seconds;
            const std::uint64_t north = vy * seconds;
            if (east * east + north * north >
                static_cast<std::uint64_t>(kMaxDisplacementMm) * kMaxDisplacementMm) {
                invalid("displacement", "radial displacement exceeds the 1000m tangent limit");
            }
        }
    }
}

Seed select_seed(SeedMode mode, std::uint64_t fixed_value) {
    if (mode == SeedMode::Fixed) return {mode, fixed_value};
    if (mode != SeedMode::Fresh) invalid("seed_mode", "unsupported seed mode");
#ifdef _WIN32
    Seed selected{mode, 0};
    const NTSTATUS status = BCryptGenRandom(nullptr, reinterpret_cast<PUCHAR>(&selected.value),
                                           static_cast<ULONG>(sizeof(selected.value)),
                                           BCRYPT_USE_SYSTEM_PREFERRED_RNG);
    if (status < 0) throw std::runtime_error("Fresh seed: BCryptGenRandom failed; no entropy fallback exists");
    return selected;
#else
    throw std::runtime_error("Fresh seed: this build has no qualified OS entropy provider");
#endif
}

std::uint64_t rng_subsequence(RngPurpose purpose, std::uint32_t actor_id) {
    const auto code = static_cast<std::uint32_t>(purpose);
    if (code < 1 || code > 5) invalid("RNG purpose", "unsupported stream domain");
    return (static_cast<std::uint64_t>(kRngDomainVersion) << 56) |
           (static_cast<std::uint64_t>(code) << 48) | actor_id;
}

std::uint64_t domain_digest(const Domain& domain) {
    validate_domain(domain);
    Digest digest;
    digest.text("aismixer.geo.domain.v1");
    digest.unsigned_value(domain.schema_version, 4);
    digest.text(domain.profile_id);
    digest.text(domain.provenance);
    digest.unsigned_value(domain.ring.size(), 8);
    for (const auto point : domain.ring) digest.point(point);
    return digest.value();
}

std::uint64_t anchor_digest(const std::vector<Anchor>& anchors) {
    if (anchors.size() > kMaxSceneActors) invalid("anchors", "anchor count exceeds the B2 bound");
    auto sorted = anchors;
    std::sort(sorted.begin(), sorted.end(), [](const Anchor& first, const Anchor& second) {
        return first.actor_id < second.actor_id;
    });
    Digest digest;
    digest.text("aismixer.geo.anchors.v1");
    digest.unsigned_value(sorted.size(), 8);
    bool first = true;
    std::uint32_t previous = 0;
    for (const auto& anchor : sorted) {
        validate_point(anchor.tick_zero, "anchors.tick_zero");
        if (!first && previous == anchor.actor_id) invalid("anchors.actor_id", "duplicate actor anchor");
        first = false;
        previous = anchor.actor_id;
        digest.unsigned_value(anchor.actor_id, 4);
        digest.point(anchor.tick_zero);
    }
    return digest.value();
}

std::uint64_t event_digest(const std::vector<GeoEvent>& events) {
    if (events.size() > kMaxSceneCandidates) invalid("events", "event count exceeds the B2 bound");
    Digest digest;
    for (const auto& event : events) {
        digest.unsigned_value(event.absolute_tick, 8);
        digest.unsigned_value(event.simulation_seconds, 8);
        digest.unsigned_value(event.actor_id, 4);
        digest.unsigned_value(static_cast<std::uint32_t>(event.category), 4);
        digest.signed_value(event.x_mm);
        digest.signed_value(event.y_mm);
        digest.point(event.location);
        digest.unsigned_value(event.random_u32, 4);
    }
    return digest.value();
}

std::int64_t ais_coordinate(std::int64_t nanodegrees, bool longitude) {
    const auto bound = (longitude ? 180 : 90) * kDegree;
    if (nanodegrees < -bound || nanodegrees > bound) {
        invalid(longitude ? "AIS longitude" : "AIS latitude", "impossible geographic coordinate");
    }
    // 600000 wire units/degree divided by 1e9 nanodegrees/degree = 3/5000.
    const std::uint64_t numerator = magnitude(nanodegrees) * 3;
    std::int64_t rounded = static_cast<std::int64_t>(numerator / 5000);
    if (numerator % 5000 >= 2500) ++rounded;
    return nanodegrees < 0 ? -rounded : rounded;
}

}  // namespace aismixer::lab::geo
