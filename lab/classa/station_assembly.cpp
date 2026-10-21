#include "station_assembly.h"

#include "ais_encoder.h"

#include <algorithm>
#include <set>
#include <stdexcept>
#include <string>

namespace aismixer::lab::classa {
namespace {
void require(bool condition, const std::string& message) {
    if (!condition) throw std::invalid_argument("Class A station assembly: " + message);
}
void metadata(const std::string& value, const char* field, bool identifier) {
    const std::size_t maximum = identifier ? 64 : 256;
    require(!value.empty() && value.size() <= maximum,
            std::string(field) + ": required length is 1.." + std::to_string(maximum));
    for (const unsigned char character : value) {
        const bool valid_identifier =
            (character >= 'A' && character <= 'Z') ||
            (character >= 'a' && character <= 'z') ||
            (character >= '0' && character <= '9') ||
            character == '_' || character == '-' || character == '.' || character == ':';
        require(identifier ? valid_identifier : character >= 32 && character <= 126,
                std::string(field) + ": invalid metadata character");
    }
    require(identifier || value.find_first_not_of(' ') != std::string::npos,
            std::string(field) + ": whitespace-only metadata is missing");
}
void validate_definition(const StationDefinition& definition,
                         runtime::SimulationInstant scenario_end) {
    const auto& own = definition.identity;
    require(own.vessel_id.value != 0 && own.equipment_id.value != 0 && own.radio_id.value != 0,
            "identity: vessel/equipment/radio IDs must all be nonzero");
    // Mmsi is normally valid by construction. Reconstructing also rejects a
    // mutable assembly definition whose MMSI was already moved out of it.
    const ais::Mmsi checked_mmsi(definition.mmsi.digits());
    require(checked_mmsi.wire_value() == definition.mmsi.wire_value(),
            "mmsi: text and wire identity disagree");
    const ais::Mmsi checked_static_mmsi(definition.static_voyage.mmsi.digits());
    require(checked_static_mmsi.wire_value() == definition.static_voyage.mmsi.wire_value(),
            "static_voyage.mmsi: text and wire identity disagree");
    require(definition.model_id == kStationModelId, "model_id: unsupported or missing model");
    require(definition.schema_version == kStationConfigurationVersion,
            "schema_version: unsupported station configuration version");
    require(definition.profile_id == kCapabilityProfileId,
            "profile_id: expected LCP-CLASSA-AUTO-TERR-v1");
    require(definition.excluded_functions == kExcludedFunctions,
            "excluded_functions: expected complete frozen limited-profile exclusions");
    require(definition.origin.category == kSyntheticIdentityCategory,
            "origin.category: only explicitly synthetic lab identities are supported");
    require(definition.origin.identity_validity == kSyntaxOnlyIdentityValidity,
            "origin.identity_validity: syntax validation is not allocation authority");
    metadata(definition.origin.scenario_id, "origin.scenario_id", true);
    metadata(definition.origin.provenance, "origin.provenance", false);

    const auto& clock = definition.clock;
    metadata(clock.source_id, "clock.source_id", true);
    metadata(clock.provenance, "clock.provenance", false);
    require(clock.quality == kIdealDirectUtcCategory,
            "clock.quality: only explicit IDEAL_DIRECT_UTC_SIM is supported");
    require(clock.anchor_revision != 0, "clock.anchor_revision: explicit nonzero revision required");
    runtime::validate_utc_anchor(clock.anchor);
    // Check the entire bounded scenario's endpoint mapping. Runtime checked
    // integer arithmetic owns UTC decomposition; no approximate clock exists.
    for (const auto instant : {runtime::SimulationInstant{0}, scenario_end}) {
        const auto slot = runtime::decompose_utc(instant, clock.anchor);
        require(runtime::compose_utc(slot, clock.anchor) == instant,
                "clock.anchor: UTC mapping does not roundtrip");
    }

    const auto& policy = definition.input_policy;
    require(policy.version == 1, "input_policy.version: unsupported policy version");
    require(policy.epoch_policy == kPowerEpochPolicy,
            "input_policy.epoch_policy: unsupported power-generation handling");
    metadata(policy.epfs_source_id, "input_policy.epfs_source_id", true);
    metadata(policy.provenance, "input_policy.provenance", false);
    require(policy.epfs_category == kOwnTruthEpfsCategory,
            "input_policy.epfs_category: expected explicit OWN_W1_TRUTH_AS_EPFS");
    constexpr auto maximum_age = 60 * runtime::kUnitsPerSecond;
    require(policy.max_navigation_age_units >= 0 && policy.max_navigation_age_units <= maximum_age,
            "input_policy.max_navigation_age_units: expected 0..60 exact seconds");
    require(policy.max_clock_age_units >= 0 && policy.max_clock_age_units <= maximum_age,
            "input_policy.max_clock_age_units: expected 0..60 exact seconds");
    require(definition.static_voyage.mmsi.digits() == definition.mmsi.digits(),
            "static_voyage.mmsi: must match the station's explicit MMSI");
    // Reuse S1's complete text/domain validation, including IMO-zero NO-GO
    // and manufacturer revalidation (also protects moved-from metadata).
    (void)ais::encode(definition.static_voyage);
    (void)ais::encode(ais::ClassAType24B(definition.mmsi, definition.manufacturer));
}
}  // namespace

namespace detail {
class StationAssemblyAccess {
public:
    static OwnStationConfig project(StationDefinition definition, std::uint64_t revision) {
        return OwnStationConfig(std::move(definition), revision);
    }
};
}  // namespace detail

std::vector<OwnStationConfig> assemble_stations(
    const world::Config& world_config, std::uint64_t world_revision,
    std::vector<StationDefinition> definitions) {
    require(world_revision != 0, "world_revision: explicit nonzero scenario-instance revision required");
    // The accepted W1 validator remains authoritative for all world fields,
    // duplicate W1 identities, eligible legs and geographical qualification.
    const auto canonical = world::canonical_config(world_config);
    require(!canonical.fresh_seed, "world configuration: resolve the W1 fresh seed before assembly");
    require(!definitions.empty() && definitions.size() <= world::kMaxVessels,
            "definitions: expected 1..1024 explicitly configured stations");
    std::set<std::uint32_t> vessels, equipment, radios;
    std::set<std::string> mmsis, source_ids;
    for (const auto& definition : definitions) {
        const auto prefix = "vessel " + std::to_string(definition.identity.vessel_id.value) + ": ";
        try {
            validate_definition(definition, canonical.duration);
            require(definition.origin.scenario_id == canonical.profile_id,
                    "origin.scenario_id: must reference the actual W1 configuration profile_id");
            const auto own = definition.identity;
            require(vessels.insert(own.vessel_id.value).second, "identity.vessel_id: duplicate station ID");
            require(equipment.insert(own.equipment_id.value).second, "identity.equipment_id: duplicate station ID");
            require(radios.insert(own.radio_id.value).second, "identity.radio_id: duplicate station ID");
            require(mmsis.insert(definition.mmsi.digits()).second, "mmsi: duplicate station identity");
            require(source_ids.insert(definition.clock.source_id).second,
                    "clock.source_id: duplicate own input source ID");
            require(source_ids.insert(definition.input_policy.epfs_source_id).second,
                    "input_policy.epfs_source_id: duplicate own input source ID");
            const auto matched = std::find_if(canonical.vessels.begin(), canonical.vessels.end(),
                [own](const world::Vessel& vessel) { return vessel.id == own.vessel_id; });
            require(matched != canonical.vessels.end(), "identity.vessel_id: no matching W1 vessel");
            require(matched->equipment_id == own.equipment_id && matched->radio_id == own.radio_id,
                    "identity: vessel/equipment/radio triple does not match W1");
            const auto& reference = definitions.front().clock.anchor;
            require(definition.clock.anchor.posix_seconds == reference.posix_seconds &&
                    definition.clock.anchor.subsecond_units == reference.subsecond_units,
                    "clock.anchor: independently ideal sources must map the same simulation instant to the same physical UTC");
        } catch (const std::exception& error) {
            throw std::invalid_argument("Class A station " + prefix + error.what());
        }
    }
    std::sort(definitions.begin(), definitions.end(), [](const auto& left, const auto& right) {
        return left.identity.vessel_id < right.identity.vessel_id;
    });
    std::vector<OwnStationConfig> projected;
    projected.reserve(definitions.size());
    for (auto& definition : definitions)
        projected.push_back(detail::StationAssemblyAccess::project(std::move(definition), world_revision));
    return projected;
}

}  // namespace aismixer::lab::classa
