#pragma once

#include "scene_api.h"

#include <cstdint>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>

namespace aismixer::lab::runner {

struct RunParameters {
    std::uint64_t seed;
    std::uint64_t start_tick;
    std::uint64_t tick_count;
    std::uint32_t tick_seconds;
};

struct Scene {
    std::string name;
    RunParameters simulation;
    std::vector<Actor> actors;
};

class FileError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

// Load exactly one UTF-8 YAML schema_version: 1 document, capped at 1 MiB.
// File I/O failures throw FileError; malformed/schema-invalid scenes throw
// std::invalid_argument with path, field, and available line/column context.
// Parsing and validation never initialize CUDA or allocate GPU buffers.
Scene load_scene(const std::filesystem::path& path);

}  // namespace aismixer::lab::runner
