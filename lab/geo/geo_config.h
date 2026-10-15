#pragma once

#include "geo_api.h"

#include <filesystem>
#include <vector>

namespace aismixer::lab::geo {

// Optional yaml-cpp integration. These loaders use bounded UTF-8, exactly one
// version-1 document, strict named coordinates/metadata and no aliases, custom
// tags, includes, URLs or execution. Returned values are host-validated.
Domain load_domain(const std::filesystem::path& path);
std::vector<Anchor> load_overlay(const std::filesystem::path& path);
std::vector<Receiver> load_receivers(const std::filesystem::path& path);

}  // namespace aismixer::lab::geo
