#pragma once

#include "geo_ais.h"

#include <filesystem>
#include <ostream>
#include <vector>

#ifdef AISMIXER_LAB_GEO_OUTPUT_TESTING
#include <functional>
#endif

namespace aismixer::lab::geo {

// Resolve a nonempty destination in an existing directory and refuse any
// existing directory entry. Publication repeats this check and is no-clobber.
std::filesystem::path prepare_nmea_output(const std::filesystem::path& path);

// Write to an exclusively created same-directory temporary file, flush/close,
// then publish without replacing an existing destination. Failure removes the
// owned temporary file best-effort; forced termination may leave that temp.
void export_nmea(const std::filesystem::path& path,
                 const std::vector<OfflineTransmission>& transmissions);

// Preserve canonical event= metadata with O(log P) identity lookup and one
// bounded row write per event. Identities must have unique ascending actor IDs.
void write_event_metadata(std::ostream& output, const std::vector<GeoEvent>& events,
                          const std::vector<Identity>& identities);

#ifdef AISMIXER_LAB_GEO_OUTPUT_TESTING
namespace testing {
enum class OutputFailure { None, Write, Flush, Close, Publish };
struct OutputHooks {
    OutputFailure failure = OutputFailure::None;
    std::size_t fail_write_after_bytes = 13;
    std::function<void(const std::filesystem::path& temporary,
                       const std::filesystem::path& destination)> before_publish;
};
// Explicit test-call injection only. No environment variable or CLI option
// enables faults in the production exporter.
void export_nmea_with_hooks(const std::filesystem::path& path,
                            const std::vector<OfflineTransmission>& transmissions,
                            const OutputHooks& hooks);
}  // namespace testing
#endif

}  // namespace aismixer::lab::geo
