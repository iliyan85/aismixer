#include "scene_validation.h"

#include <charconv>
#include <limits>
#include <system_error>
#include <unordered_map>

namespace aismixer::lab::runner {
namespace {

[[noreturn]] void invalid(std::string_view field, std::string_view explanation) {
    throw std::invalid_argument(std::string(field) + ": " + std::string(explanation));
}

template <typename Integer>
Integer parse_integer(std::string_view value, std::string_view field) {
    std::size_t first_digit = 0;
    if constexpr (std::numeric_limits<Integer>::is_signed) {
        if (!value.empty() && value.front() == '-') {
            first_digit = 1;
        }
    }
    if (value.size() == first_digit) {
        invalid(field, "expected a base-10 integer");
    }
    for (std::size_t index = first_digit; index < value.size(); ++index) {
        if (value[index] < '0' || value[index] > '9') {
            invalid(field, "expected ASCII base-10 digits (signed fields allow a leading '-')");
        }
    }
    Integer result{};
    const auto conversion = std::from_chars(value.data(), value.data() + value.size(), result, 10);
    if (conversion.ec == std::errc::result_out_of_range) {
        invalid(field, "integer is outside the required " +
                           std::to_string(sizeof(Integer) * 8) + "-bit " +
                           (std::numeric_limits<Integer>::is_signed ? "signed" : "unsigned") +
                           " range");
    }
    if (conversion.ec != std::errc{} || conversion.ptr != value.data() + value.size()) {
        invalid(field, "expected a base-10 integer");
    }
    return result;
}

void validate_name(std::string_view name) {
    if (name.empty() || name.size() > 200) {
        invalid("name", "must contain 1 through 200 UTF-8 bytes");
    }
    bool useful = false;
    for (std::size_t index = 0; index < name.size(); ++index) {
        const auto byte = static_cast<unsigned char>(name[index]);
        if (byte < 0x20 || byte == 0x7f ||
            (byte == 0xc2 && index + 1 < name.size() &&
             static_cast<unsigned char>(name[index + 1]) >= 0x80 &&
             static_cast<unsigned char>(name[index + 1]) <= 0x9f)) {
            invalid("name", "control characters are not allowed");
        }
        if (byte != ' ') {
            useful = true;
        }
    }
    if (!useful) {
        invalid("name", "must contain a non-space character");
    }
}

}  // namespace

std::uint64_t parse_u64(std::string_view value, std::string_view field) {
    return parse_integer<std::uint64_t>(value, field);
}

std::uint32_t parse_u32(std::string_view value, std::string_view field) {
    return parse_integer<std::uint32_t>(value, field);
}

std::int64_t parse_i64(std::string_view value, std::string_view field) {
    return parse_integer<std::int64_t>(value, field);
}

std::int32_t parse_i32(std::string_view value, std::string_view field) {
    return parse_integer<std::int32_t>(value, field);
}

std::string_view category_name(ActorCategory category) {
    switch (category) {
        case ActorCategory::ClassA: return "ClassA";
        case ActorCategory::ClassB: return "ClassB";
        case ActorCategory::BaseStation: return "BaseStation";
        case ActorCategory::AtoN: return "AtoN";
        case ActorCategory::SAR: return "SAR";
        case ActorCategory::Emergency: return "Emergency";
    }
    invalid("category", "invalid actor category code");
}

std::string path_text(const std::filesystem::path& path) {
    return path.u8string();
}

void validate_scene(const Scene& scene) {
    validate_name(scene.name);
    if (scene.actors.size() > kMaxSceneActors) {
        invalid("actors", "actor count exceeds " + std::to_string(kMaxSceneActors));
    }
    const RunParameters& run = scene.simulation;
    // These deliberately mirror the B2 API's host checks. The engine does not
    // expose a CPU-only validation entry point, and must retain its own checks.
    if (!scene.actors.empty() && run.tick_count > kMaxSceneCandidates / scene.actors.size()) {
        invalid("simulation.tick_count", "actor-tick candidate count exceeds " +
                                             std::to_string(kMaxSceneCandidates));
    }
    if (run.tick_count > kMaxSceneTicks) {
        invalid("simulation.tick_count", "must be at most " + std::to_string(kMaxSceneTicks));
    }
    if (run.tick_seconds == 0 || run.tick_seconds > kMaxTickSeconds) {
        invalid("simulation.tick_seconds", "must be in [1, " +
                                               std::to_string(kMaxTickSeconds) + "]");
    }
    if (run.start_tick > kMaxAbsoluteTick) {
        invalid("simulation.start_tick", "must be at most " +
                                             std::to_string(kMaxAbsoluteTick));
    }
    if (run.tick_count != 0 && run.tick_count - 1 > kMaxAbsoluteTick - run.start_tick) {
        invalid("simulation.tick_count", "last included absolute tick exceeds " +
                                             std::to_string(kMaxAbsoluteTick));
    }

    std::unordered_map<std::uint32_t, std::size_t> actor_indexes;
    actor_indexes.reserve(scene.actors.size());
    for (std::size_t index = 0; index < scene.actors.size(); ++index) {
        const Actor& actor = scene.actors[index];
        const std::string field = "actors[" + std::to_string(index) + "].";
        const auto inserted = actor_indexes.emplace(actor.actor_id, index);
        if (!inserted.second) {
            invalid(field + "actor_id", "duplicate actor_id " + std::to_string(actor.actor_id) +
                                            " (also actors[" +
                                            std::to_string(inserted.first->second) + "])");
        }
        if (static_cast<std::uint32_t>(actor.category) >
            static_cast<std::uint32_t>(ActorCategory::Emergency)) {
            invalid(field + "category", "invalid actor category code");
        }
        if (actor.report_interval_ticks == 0) {
            invalid(field + "report_interval_ticks", "must be positive");
        }
        if (actor.phase_ticks >= actor.report_interval_ticks) {
            invalid(field + "phase_ticks", "must be less than report_interval_ticks");
        }
        if (actor.x_mm < -kMaxInitialPositionMm || actor.x_mm > kMaxInitialPositionMm) {
            invalid(field + "x_mm", "initial position must be in [-" +
                                        std::to_string(kMaxInitialPositionMm) + ", " +
                                        std::to_string(kMaxInitialPositionMm) + "] mm");
        }
        if (actor.y_mm < -kMaxInitialPositionMm || actor.y_mm > kMaxInitialPositionMm) {
            invalid(field + "y_mm", "initial position must be in [-" +
                                        std::to_string(kMaxInitialPositionMm) + ", " +
                                        std::to_string(kMaxInitialPositionMm) + "] mm");
        }
        if (actor.vx_mm_per_s < -kMaxVelocityMmPerS || actor.vx_mm_per_s > kMaxVelocityMmPerS) {
            invalid(field + "vx_mm_per_s", "velocity must be in [-" +
                                               std::to_string(kMaxVelocityMmPerS) + ", " +
                                               std::to_string(kMaxVelocityMmPerS) + "] mm/s");
        }
        if (actor.vy_mm_per_s < -kMaxVelocityMmPerS || actor.vy_mm_per_s > kMaxVelocityMmPerS) {
            invalid(field + "vy_mm_per_s", "velocity must be in [-" +
                                               std::to_string(kMaxVelocityMmPerS) + ", " +
                                               std::to_string(kMaxVelocityMmPerS) + "] mm/s");
        }
        if (actor.category == ActorCategory::BaseStation || actor.category == ActorCategory::AtoN) {
            if (actor.vx_mm_per_s != 0) {
                invalid(field + "vx_mm_per_s", "BaseStation and AtoN must be stationary");
            }
            if (actor.vy_mm_per_s != 0) {
                invalid(field + "vy_mm_per_s", "BaseStation and AtoN must be stationary");
            }
        }
    }
}

}  // namespace aismixer::lab::runner
