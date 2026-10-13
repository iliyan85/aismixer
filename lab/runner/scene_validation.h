#pragma once

#include "scene_yaml.h"

#include <cstdint>
#include <string>
#include <string_view>

namespace aismixer::lab::runner {

// Strict ASCII base-10 integers. No plus, whitespace, fractions, exponents, or
// alternate bases; signed values permit a leading minus. Leading zeros remain
// decimal. Every conversion validates its exact integer width before returning.
std::uint64_t parse_u64(std::string_view value, std::string_view field);
std::uint32_t parse_u32(std::string_view value, std::string_view field);
std::int64_t parse_i64(std::string_view value, std::string_view field);
std::int32_t parse_i32(std::string_view value, std::string_view field);

// Pure host validation using B2's public bounds, including empty/zero requests.
// Names must contain a non-space character, use at most 200 UTF-8 bytes, and
// contain no C0, DEL, or C1 control characters. Throws std::invalid_argument.
void validate_scene(const Scene& scene);

std::string_view category_name(ActorCategory category);
std::string path_text(const std::filesystem::path& path);

}  // namespace aismixer::lab::runner
