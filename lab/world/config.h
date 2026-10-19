#pragma once

#include "world.h"
#include <filesystem>

namespace aismixer::lab::world {

// Strict schema-1 world scenario plus its two explicitly referenced schema-1
// catalogue files. References resolve relative to the scenario file. No legacy
// geographic profile is reinterpreted and no implicit include is followed.
// Source fingerprints are FNV-1a64 over UTF-8 source bytes after CRLF/CR -> LF.
// Parsing/validation is host-only; it never allocates CUDA state or samples RNG.
Config load_world(const std::filesystem::path& path);

}  // namespace aismixer::lab::world
