#pragma once

#include "scene_yaml.h"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <iosfwd>
#include <vector>

namespace aismixer::lab::runner {

// FNV-1a-64 over canonical records, with the B2 36-byte little-endian encoding.
std::uint64_t event_digest(const std::vector<EventIntent>& events);
void write_csv(std::ostream& output, const std::vector<EventIntent>& events);
void print_scene(std::ostream& output, const Scene& scene, std::size_t actor_preview_limit = 100);
void print_summary(std::ostream& output, const Scene& scene,
                   const std::vector<EventIntent>& events, std::size_t preview_limit = 10);

// Resolve relative to the current directory, reject existing destinations, and
// create missing parent directories. Throws FileError for filesystem failures.
std::filesystem::path prepare_output_path(const std::filesystem::path& path);

// Write a same-directory exclusive temporary file, flush/close it successfully,
// then publish without replacing any existing destination (including races).
// Formatting uses a fixed-size buffer, without a second event-sized allocation.
void export_csv(const std::filesystem::path& path, const std::vector<EventIntent>& events);

}  // namespace aismixer::lab::runner
