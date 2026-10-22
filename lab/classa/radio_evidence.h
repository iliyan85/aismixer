#pragma once

#include "slot_identity.h"
#include "station_config.h"

#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>

namespace aismixer::lab::classa {

// Observation trust boundary for station-local radio knowledge.
//
// A1-S3 contains no live RF decoder, FCS validator, receiver model or S5
// receipt capability. The only evidence this slice can admit is an explicitly
// labelled UNIT_FIXTURE token minted by a test-build-only accessor. Production
// callers have no way to construct OwnedRadioEvidence: the constructor is
// private and its single friend, UnitFixtureEvidenceAccess, is declared only
// under the unit-fixture compile definition and defined only by the unit
// test translation unit, so a production map remains disabled until a future
// versioned S5/RF1 minter adds an unforgeable, validated per-receiver token
// carrying at least: receiver owner, channel, exact physical interval,
// decoded payload/FCS proof, disclosure time, power epoch, catalog revision
// and receipt identity. A Boolean "received", a global journal or
// hand-authored NMEA is never authoritative reception.
//
// Physical interval contract (RADIO-EVIDENCE-v1): [physical_start,
// physical_end) is the NOMINAL slot-aligned span the frame occupied as seen
// by the owner's validated ideal UTC (one slot for Messages 1-4 and 20, up to
// five slots for a garble span). It is not an RF envelope: the 1 ms physical
// tail, source timing error and propagation delay belong to a future RF1/S5
// policy at that seam and must be removed before a token is minted. S3 maps
// the span to slots by floor(start) and floor(end - 1 unit).
inline constexpr std::uint32_t kRadioEvidenceContractVersion = 1;
inline constexpr const char* kUnitFixtureProvenance = "UNIT_FIXTURE";

enum class EvidenceProvenance : std::uint8_t { UnitFixture = 1 };
const char* provenance_name(EvidenceProvenance provenance);  // Throws on an unknown value.
enum class ObservationDisposition : std::uint8_t { Decoded = 1, FailedFcs = 2, EnergyOnly = 3 };
// Types 1/2/4 carry a SOTDMA state, Type 3 an ITDMA state (Annex 7 Table 47;
// Table 49). Message 20 (base data link management) carries no communication
// state: this slice records its decoded slot use and base identity and
// classifies the message itself DEFERRED (A0-MAC-022 pairing with Message 4
// is outside the autonomous terrestrial profile). Other kinds are rejected.
enum class DecodedMessageKind : std::uint8_t { Type1 = 1, Type2 = 2, Type3 = 3, Type4 = 4, Type20 = 20 };

// Independently specified semantic facts of a decoded report. These are
// fixture inputs, not the output of a decoder that this slice possesses.
struct DecodedReportFacts {
    DecodedMessageKind kind = DecodedMessageKind::Type1;
    std::uint32_t source_mmsi = 0;                        // 30-bit wire identity, nonzero.
    std::int64_t longitude = ais::kLongitudeUnavailable;  // 1/10000 minute or unavailable.
    std::int64_t latitude = ais::kLatitudeUnavailable;    // 1/10000 minute or unavailable.
    std::int64_t raw_communication_state = -1;            // 19-bit raw state; ignored for Message 20.
    std::int64_t navigation_status = 15;
};

struct OwnRadioObservation {
    OwnIdentity owner;
    world::RadioId receiver;
    Channel channel = Channel::Ais1;
    std::uint64_t power_generation = 0;
    std::uint64_t catalog_revision = 0;  // Must equal the station's scenario-instance revision.
    std::uint64_t receipt_id = 0;        // Strictly increasing within one own power epoch.
    runtime::SimulationInstant physical_start;
    runtime::SimulationInstant physical_end;   // Half-open nominal [start, end), see above.
    runtime::SimulationInstant disclosed_at;   // Causal availability to this station.
    ObservationDisposition disposition = ObservationDisposition::EnergyOnly;
    std::optional<DecodedReportFacts> decoded;
    EvidenceProvenance provenance = EvidenceProvenance::UnitFixture;
    std::string note;
};

class UnitFixtureEvidenceAccess;  // Defined by unit-fixture test builds only.

class OwnedRadioEvidence {
public:
    const OwnRadioObservation& observation() const noexcept { return observation_; }
    const char* provenance_name() const { return classa::provenance_name(observation_.provenance); }

private:
    // The friend declaration is unconditional so that this class definition
    // is identical in every translation unit (one definition rule).
    friend class UnitFixtureEvidenceAccess;
    explicit OwnedRadioEvidence(OwnRadioObservation observation)
        : observation_(std::move(observation)) {}
    OwnRadioObservation observation_;
};

inline const char* provenance_name(EvidenceProvenance provenance) {
    switch (provenance) {
    case EvidenceProvenance::UnitFixture: return kUnitFixtureProvenance;
    }
    throw std::invalid_argument("Class A evidence: unknown provenance");
}

#ifdef AISMIXER_LAB_CLASSA_UNIT_FIXTURE
// Test builds only. Every minted token is forced to UNIT_FIXTURE provenance.
// mint() is declared here and defined only in the unit test translation
// unit; a production target that merely defines the macro has nothing to
// link against.
class UnitFixtureEvidenceAccess {
public:
    static OwnedRadioEvidence mint(OwnRadioObservation observation);
};
#endif

}  // namespace aismixer::lab::classa
