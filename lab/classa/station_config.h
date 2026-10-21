#pragma once

#include "ais_message.h"
#include "world.h"

#include <cstdint>
#include <string>
#include <utility>

namespace aismixer::lab::classa {

// These interface identifiers and all input metadata rules are
// SIMULATION_POLICY, not ITU-specified configuration or identity allocation.
inline constexpr std::uint32_t kStationConfigurationVersion = 1;
inline constexpr const char* kStationModelId = "A1-CLASSA-OWN-STATION-v1";
inline constexpr const char* kCapabilityProfileId = "LCP-CLASSA-AUTO-TERR-v1";
inline constexpr const char* kExcludedFunctions =
    "satellite;assigned;polled;commands;base;indirect-UTC;degraded-clock;"
    "repeater;regional-management;silent-control;safety-binary;full-IEC";
inline constexpr const char* kIdealDirectUtcCategory = "IDEAL_DIRECT_UTC_SIM";
inline constexpr const char* kOwnTruthEpfsCategory = "OWN_W1_TRUTH_AS_EPFS";
inline constexpr const char* kPowerEpochPolicy = "OWN_POWER_GENERATION_EPOCH-v1";
inline constexpr const char* kSyntheticIdentityCategory = "SYNTHETIC_LAB";
inline constexpr const char* kSyntaxOnlyIdentityValidity =
    "SYNTAX_ONLY_NOT_ALLOCATION_AUTHORITY";

struct OwnIdentity {
    world::VesselId vessel_id;
    world::EquipmentId equipment_id;
    world::RadioId radio_id;
};
constexpr bool operator==(OwnIdentity a, OwnIdentity b) noexcept {
    return a.vessel_id == b.vessel_id && a.equipment_id == b.equipment_id &&
           a.radio_id == b.radio_id;
}
constexpr bool operator!=(OwnIdentity a, OwnIdentity b) noexcept { return !(a == b); }

struct ClockDefinition {
    std::string source_id;       // Required explicit opaque ID, not a path.
    std::string provenance;      // Required description of this ideal source.
    std::string quality = kIdealDirectUtcCategory;
    runtime::UtcAnchor anchor;   // UTC mapping for simulation instant zero.
    std::uint64_t anchor_revision = 0;  // Zero means missing, and is rejected.
};

struct InputPolicy {
    std::uint32_t version = 1;
    std::string epoch_policy = kPowerEpochPolicy;
    std::string epfs_source_id;  // Required explicit independent own-source ID.
    std::string provenance;     // Required truth-fed EPFS policy description.
    std::string epfs_category = kOwnTruthEpfsCategory;
    // Bounded [0, 60 seconds]. Zero requires sampling at the requested instant.
    // Freshness never changes the fix-generation timestamp into arrival time.
    std::int64_t max_navigation_age_units = 0;
    std::int64_t max_clock_age_units = 0;
};

struct SyntheticOrigin {
    std::string category = kSyntheticIdentityCategory;
    std::string identity_validity = kSyntaxOnlyIdentityValidity;
    std::string scenario_id;  // Must match the actual W1 configuration profile_id.
    std::string provenance;
};

// Mutable assembly input. Every identity and manufacturer value is explicit;
// the Type 5 constructor also requires its qualified nonzero IMO/flag value.
// The codec's explicit unavailable static fields remain valid unavailable
// values; no data are derived from W1 names, dimensions or actor order.
struct StationDefinition {
    StationDefinition(OwnIdentity own, ais::Mmsi configured_mmsi,
                      ais::Type5 voyage,
                      ais::ManufacturerIdentification manufacturer_metadata)
        : identity(own), mmsi(std::move(configured_mmsi)),
          static_voyage(std::move(voyage)),
          manufacturer(std::move(manufacturer_metadata)) {}

    OwnIdentity identity;
    ais::Mmsi mmsi;
    ais::Type5 static_voyage;
    ais::ManufacturerIdentification manufacturer;
    std::string model_id = kStationModelId;
    std::uint32_t schema_version = kStationConfigurationVersion;
    std::string profile_id = kCapabilityProfileId;
    std::string excluded_functions = kExcludedFunctions;
    ClockDefinition clock;
    InputPolicy input_policy;
    SyntheticOrigin origin;
};

namespace detail { class StationAssemblyAccess; }

// Owned, bounded and effectively immutable. Only scenario assembly can mint
// one, after canonical W1 validation and global referential/uniqueness checks.
// This API has no world configuration/snapshot, registry, peer lookup or
// shared-directory operation. Copying retains only this station's own values.
class OwnStationConfig {
public:
    OwnStationConfig(const OwnStationConfig&) = default;
    // Moving is intentionally value-preserving for the source as well: an
    // accepted immutable config must never become a reusable moved-from shell.
    OwnStationConfig(OwnStationConfig&& other)
        : definition_(other.definition_), world_revision_(other.world_revision_) {}
    OwnStationConfig& operator=(const OwnStationConfig&) = default;
    OwnStationConfig& operator=(OwnStationConfig&& other) {
        return operator=(static_cast<const OwnStationConfig&>(other));
    }

    const OwnIdentity& identity() const noexcept { return definition_.identity; }
    const ais::Mmsi& mmsi() const noexcept { return definition_.mmsi; }
    const ais::Type5& static_voyage() const noexcept { return definition_.static_voyage; }
    const ais::ManufacturerIdentification& manufacturer() const noexcept {
        return definition_.manufacturer;
    }
    const ClockDefinition& clock() const noexcept { return definition_.clock; }
    const InputPolicy& input_policy() const noexcept { return definition_.input_policy; }
    const SyntheticOrigin& origin() const noexcept { return definition_.origin; }
    const std::string& model_id() const noexcept { return definition_.model_id; }
    std::uint32_t schema_version() const noexcept { return definition_.schema_version; }
    const std::string& profile_id() const noexcept { return definition_.profile_id; }
    const std::string& excluded_functions() const noexcept {
        return definition_.excluded_functions;
    }
    // Explicit scenario-instance revision, not W1's changing reach revision.
    std::uint64_t world_revision() const noexcept { return world_revision_; }

private:
    friend class detail::StationAssemblyAccess;
    OwnStationConfig(StationDefinition definition, std::uint64_t world_revision);
    StationDefinition definition_;
    std::uint64_t world_revision_;
};

}  // namespace aismixer::lab::classa
