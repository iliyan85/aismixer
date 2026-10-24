#include "station_knowledge.h"

#include <algorithm>
#include <optional>
#include <stdexcept>
#include <type_traits>
#include <utility>
#include <variant>

namespace aismixer::lab::classa {
namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::invalid_argument(message);
}
bool valid_ais_coordinate(std::int64_t longitude, std::int64_t latitude) noexcept {
    const bool longitude_ok = longitude == ais::kLongitudeUnavailable ||
                              (longitude >= -108000000 && longitude <= 108000000);
    const bool latitude_ok = latitude == ais::kLatitudeUnavailable ||
                             (latitude >= -54000000 && latitude <= 54000000);
    return longitude_ok && latitude_ok;
}
std::size_t purpose_slot(ProtocolPurpose purpose) {
    (void)purpose_name(purpose);
    return static_cast<std::size_t>(static_cast<unsigned>(purpose) - static_cast<unsigned>(ProtocolPurpose::EntryNominalStartSlot));
}
// Reservation lineage identity and physical recency (mirrors LocalSlotMap::record_external).
bool same_reservation_lineage(const PeerAnnouncedReservation& existing, const ExternalAllocation& value) noexcept {
    if (existing.channel != value.channel) return false;
    if (sotdma_basis(value.basis))
        return sotdma_basis(existing.basis) && frame_slot(existing.first_index) == frame_slot(value.first_index);
    return existing.first_index == value.first_index && existing.basis == value.basis;
}
bool reservation_physically_newer(const PeerAnnouncedReservation& existing, const ExternalAllocation& value) noexcept {
    if (existing.first_index != value.first_index) return existing.first_index > value.first_index;
    return existing.announced_index > value.announced_index;
}
std::int64_t units_of(runtime::SimulationInstant instant) {
    return runtime::difference_units(instant, runtime::SimulationInstant{});
}
}  // namespace

std::uint64_t own_inputs_fingerprint(const QualifiedOwnInputs& own_now) {
    const auto& fields = own_now.fields();
    const std::string text = std::to_string(fields.longitude) + "|" + std::to_string(fields.latitude) + "|" +
                             std::to_string(units_of(own_now.navigation().fix_generated_at())) + "|" +
                             std::to_string(units_of(own_now.navigation().observed_at())) + "|" +
                             std::to_string(units_of(own_now.clock().sampled_at())) + "|" +
                             std::to_string(units_of(own_now.qualified_at()));
    return fnv1a64(text);
}

StationKnowledge::StationKnowledge(const OwnStationConfig& config, StationInputGate& gate,
                                   const ScenarioSeedHandoff& seed, KnowledgeLimits limits)
    : config_(config), gate_(gate), seed_(seed), limits_(limits),
      ais1_(config.identity(), Channel::Ais1, limits.map), ais2_(config.identity(), Channel::Ais2, limits.map),
      directory_(config.identity(), limits.peer_capacity) {
    require(gate.config().identity() == config.identity() && gate.config().world_revision() == config.world_revision(),
            "Class A knowledge: input gate belongs to another station or scenario instance");
    require(seed.world_revision() == config.world_revision(),
            "Class A knowledge: scenario seed handoff belongs to another scenario-instance revision");
    require(seed.scenario_id() == config.origin().scenario_id,
            "Class A knowledge: scenario seed handoff belongs to another W1 profile");
    require(config.identity().equipment_id.value != 0, "Class A knowledge: zero equipment identity");
    require(limits.ledger_capacity >= 16 && limits.ledger_capacity <= 1048576,
            "Class A knowledge: ledger capacity must be 16..1048576 entries");
}

void StationKnowledge::reset_epoch_state() {
    ais1_.clear();
    ais2_.clear();
    directory_.clear();
    reuse_exclusions_.clear();
    for (auto& watermark : watermarks_) watermark.reset();
    last_receipt_id_ = 0;
    last_disclosed_at_.reset();
    last_decision_at_.reset();
    now_index_.reset();
    revision_ += 1;
}

void StationKnowledge::apply_power(const OwnPowerNotice& notice) {
    require(notice.own_identity == config_.identity(), "Class A knowledge: foreign power notice owner");
    require(!latest_time_ || notice.time >= *latest_time_, "Class A knowledge: reversed power notice time");
    switch (notice.kind) {
    case OwnPowerKind::InitialOff:
        require(!power_initialized_ && notice.power_generation == 0,
                "Class A knowledge: InitialOff is only the generation-zero bootstrap notice");
        break;
    case OwnPowerKind::On:
        require(!on_, "Class A knowledge: duplicate ON without an OFF");
        require(notice.power_generation > generation_ && notice.power_generation <= kMaxPowerGenerationKey,
                "Class A knowledge: ON must establish a strictly newer generation within the key domain");
        require(!last_off_at_ || notice.time != *last_off_at_,
                "Class A knowledge: OFF dominates reactivation at the same instant");
        break;
    case OwnPowerKind::Off:
        require(notice.power_generation >= generation_, "Class A knowledge: OFF has stale generation");
        break;
    default:
        throw std::invalid_argument("Class A knowledge: unsupported own power notice kind");
    }
    reset_epoch_state();
    power_initialized_ = true;
    on_ = notice.kind == OwnPowerKind::On;
    generation_ = notice.power_generation;
    latest_time_ = notice.time;
    if (on_) epoch_started_at_ = notice.time;
    else epoch_started_at_.reset();
    if (notice.kind == OwnPowerKind::Off) last_off_at_ = notice.time;
}

void StationKnowledge::check_own(const QualifiedOwnInputs& own_now, runtime::SimulationInstant now) {
    require(power_initialized_ && on_, "Class A knowledge: own equipment is OFF/uninitialized");
    require(own_now.navigation().identity() == config_.identity() && own_now.clock().identity() == config_.identity(),
            "Class A knowledge: qualified inputs belong to another owner");
    require(own_now.navigation().power_generation() == generation_ && own_now.clock().power_generation() == generation_,
            "Class A knowledge: qualified inputs belong to another power generation");
    require(own_now.qualified_at() == now, "Class A knowledge: inputs must be revalidated at the decision instant");
    require(!latest_time_ || now >= *latest_time_, "Class A knowledge: reversed decision time");
    require(own_now.input_eligible() && !own_now.ready_for_tx(), "Class A knowledge: inconsistent qualified inputs");
    // Invariant 6: the owning gate revalidates the presented token at this
    // exact instant; revocation, OFF or a shadow gate token throws here.
    const auto fresh = gate_.revalidate(own_now, now);
    require(gate_.equipment_on() && gate_.power_generation() == generation_,
            "Class A knowledge: owning gate and knowledge disagree on the power epoch");
    require(fresh.current_slot().utc_minute == own_now.current_slot().utc_minute &&
            fresh.current_slot().slot == own_now.current_slot().slot,
            "Class A knowledge: revalidated UTC slot differs from the presented inputs");
    // The reversed-time guard mirrors the owning gate's monotonic clock: once
    // the gate accepted this revalidation, `now` is the station's time frontier
    // even if the token presented afterwards is rejected (A1-S3 reached the
    // same point through synchronize), so power notices and decisions earlier
    // than a revalidation are refused by both objects alike.
    latest_time_ = now;
}

LocalSlotMap& StationKnowledge::map_for(Channel channel) {
    validate_channel(channel);
    return channel == Channel::Ais1 ? ais1_ : ais2_;
}

const LocalSlotMap& StationKnowledge::map(Channel channel) const {
    validate_channel(channel);
    return channel == Channel::Ais1 ? ais1_ : ais2_;
}

std::int64_t StationKnowledge::synchronize(const QualifiedOwnInputs& own_now, runtime::SimulationInstant now) {
    check_own(own_now, now);
    const auto index = absolute_index(slot_from_utc(own_now.current_slot()));
    require(!now_index_ || index >= *now_index_, "Class A knowledge: own UTC slot regressed");
    if (!now_index_ || index != *now_index_) {
        ais1_.advance(index);
        ais2_.advance(index);
        (void)directory_.expire_stale(offset_index(index, -limits_.map.past_frames * kSlotsPerFrame), index);
        reuse_exclusions_.erase(std::remove_if(reuse_exclusions_.begin(), reuse_exclusions_.end(),
            [&](const ReuseExclusion& exclusion) { return exclusion.excluded_until_index <= index; }),
            reuse_exclusions_.end());
        revision_ += 1;
    }
    now_index_ = index;
    latest_time_ = now;
    return index;
}

EvidenceAcceptance StationKnowledge::accept_evidence(const OwnedRadioEvidence& evidence,
                                                     const QualifiedOwnInputs& own_now,
                                                     runtime::SimulationInstant now) {
    // EVIDENCE-ACCEPTANCE-ATOMICITY-v1 (A1-S3.1, SIMULATION_POLICY). Phase 1
    // revalidates the own inputs through the owning gate, derives the own slot
    // index WITHOUT synchronize, validates the token against that index and
    // prepares the complete result on staged copies of exactly the containers
    // the acceptance would change. Phase 2 installs the staged state with
    // non-throwing moves. A rejected or throwing call therefore leaves every
    // S3-owned observable unchanged and consumes no receipt identity; the owning
    // S2 gate's monotonic revalidation clock and its mirror in check_own (the
    // reversed-time guard) are the documented exceptions. A
    // valid token at a later slot performs the same advance as synchronize
    // followed by acceptance (both maps, directory expiry, reuse exclusions,
    // one advance increment) in the same commit, and every capacity, horizon
    // and lineage rule is evaluated on the post-advance, post-supersession
    // state, never on counts that would have expired at `now`.
    check_own(own_now, now);
    const auto now_index = absolute_index(slot_from_utc(own_now.current_slot()));
    require(!now_index_ || now_index >= *now_index_, "Class A knowledge: own UTC slot regressed");
    const bool advancing = !now_index_ || now_index != *now_index_;
    // Every precondition that LocalSlotMap::advance evaluates for this index
    // is established here, before any copy or mutation: a regressing slot keeps
    // its A1-S3 rejection position and the staged advance below can only fail
    // for a reason already reported here (stored records were range-checked
    // when they were recorded). The commit itself performs only noexcept moves.
    const auto earliest = offset_index(now_index, -limits_.map.past_frames * kSlotsPerFrame);
    const auto horizon_last = offset_index(now_index, limits_.map.future_frames * kSlotsPerFrame);
    require((!ais1_.advanced() || now_index >= ais1_.now_index()) && (!ais2_.advanced() || now_index >= ais2_.now_index()),
            "Class A knowledge: map instant would regress");
    const auto& observation = evidence.observation();
    require(observation.provenance == EvidenceProvenance::UnitFixture,
            "Class A knowledge: unknown evidence provenance");
    require(observation.owner == config_.identity(), "Class A knowledge: evidence delivered to a foreign owner");
    require(observation.receiver == config_.identity().radio_id,
            "Class A knowledge: evidence from a receiver this station does not own");
    validate_channel(observation.channel);
    require(observation.power_generation == generation_, "Class A knowledge: evidence from a stale/foreign power epoch");
    require(observation.catalog_revision == config_.world_revision(),
            "Class A knowledge: evidence catalog revision does not match this scenario instance");
    require(observation.receipt_id > last_receipt_id_, "Class A knowledge: replayed or non-monotonic receipt identity");
    require(observation.physical_end > observation.physical_start, "Class A knowledge: empty physical interval");
    require(observation.physical_start >= *epoch_started_at_, "Class A knowledge: evidence predates own ON epoch");
    require(observation.disclosed_at >= observation.physical_end,
            "Class A knowledge: disclosure precedes physical completion of the frame");
    require(!last_disclosed_at_ || observation.disclosed_at >= *last_disclosed_at_,
            "Class A knowledge: non-monotonic evidence disclosure");
    require(observation.disclosed_at <= now, "Class A knowledge: evidence disclosed in the future");
    require(!last_decision_at_ || observation.disclosed_at > *last_decision_at_,
            "Class A knowledge: evidence disclosed at or before the last decision instant arrives late (S3-TIE-ORDER-v1)");
    require(runtime::difference_units(observation.physical_end, observation.physical_start) <=
            kMaxBlockLength * runtime::kUnitsPerSlot, "Class A knowledge: physical interval exceeds five slots");
    const auto& anchor = own_now.clock().anchor();
    const auto received_index = absolute_index(slot_from_utc(runtime::decompose_utc(observation.physical_start, anchor)));
    const auto last_index = absolute_index(slot_from_utc(runtime::decompose_utc(
        runtime::add_units(observation.physical_end, -1), anchor)));
    require(last_index >= received_index && last_index - received_index < kMaxBlockLength,
            "Class A knowledge: physical interval slot span is malformed");
    require(received_index <= now_index, "Class A knowledge: evidence received in a future slot");
    require(received_index >= earliest, "Class A knowledge: evidence older than the local past horizon");
    const auto span = static_cast<std::uint8_t>(last_index - received_index + 1);

    EvidenceAcceptance acceptance;
    acceptance.receipt_id = observation.receipt_id;
    acceptance.channel = observation.channel;
    acceptance.received_index = received_index;
    acceptance.span_slots = span;
    acceptance.disposition = observation.disposition;
    LocalSlotMap& target = map_for(observation.channel);
    LocalSlotMap& other_target = map_for(other_channel(observation.channel));

    // Staged copies: the evidence channel's map always; the other map, the
    // directory and the reuse exclusions only when the slot index advances
    // (the directory also for decoded evidence, below). Staging runs after the
    // last token-level check of each branch, so a rejected token pays no copy;
    // copying and advancing may throw there, still before any visible mutation.
    std::optional<LocalSlotMap> staged_map;
    std::optional<LocalSlotMap> staged_other;
    std::optional<PeerDirectory> staged_directory;
    std::vector<ReuseExclusion> staged_exclusions;
    const auto stage = [&]() {
        staged_map.emplace(target);
        if (!advancing) return;
        staged_map->advance(now_index);
        staged_other.emplace(other_target);
        staged_other->advance(now_index);
        staged_directory.emplace(directory_);
        (void)staged_directory->expire_stale(earliest, now_index);
        staged_exclusions = reuse_exclusions_;
        staged_exclusions.erase(std::remove_if(staged_exclusions.begin(), staged_exclusions.end(),
            [&](const ReuseExclusion& exclusion) { return exclusion.excluded_until_index <= now_index; }),
            staged_exclusions.end());
    };
    // Phase 2: installation by non-throwing moves and scalar assignments only
    // (the static assertions after this function establish the noexcept moves).
    const auto commit = [&]() noexcept {
        target = std::move(*staged_map);
        if (staged_other) other_target = std::move(*staged_other);
        if (staged_directory) directory_ = std::move(*staged_directory);
        if (advancing) {
            reuse_exclusions_ = std::move(staged_exclusions);
            revision_ += 1;
        }
        now_index_ = now_index;
        last_receipt_id_ = observation.receipt_id;
        last_disclosed_at_ = observation.disclosed_at;
        revision_ += 1;
    };

    if (observation.disposition != ObservationDisposition::Decoded) {
        require(!observation.decoded.has_value(), "Class A knowledge: undecoded evidence cannot carry decoded facts");
        stage();
        // The staged map enforces the bounded garble store exactly, after the
        // advance and with in-place replacement of an already garbled slot.
        for (std::int64_t index = received_index; index <= last_index; ++index) {
            GarbleRecord record;
            record.channel = observation.channel;
            record.index = index;
            record.disclosed_at = observation.disclosed_at;
            record.receipt_id = observation.receipt_id;
            record.disposition = observation.disposition;
            record.provenance = observation.provenance;
            staged_map->record_garble(record);
        }
        acceptance.status = "garble_recorded_no_identity";
        commit();
        return acceptance;
    }

    require(observation.decoded.has_value(), "Class A knowledge: decoded disposition without decoded facts");
    const auto& facts = *observation.decoded;
    require(span == 1, "Class A knowledge: Messages 1-4 and 20 occupy exactly one slot");
    require(facts.source_mmsi != 0 && facts.source_mmsi <= 0x3fffffffu,
            "Class A knowledge: decoded source identity is not a nonzero 30-bit MMSI");
    require(facts.source_mmsi != config_.mmsi().wire_value(), "Class A knowledge: own MMSI cannot be a peer");
    require(valid_ais_coordinate(facts.longitude, facts.latitude), "Class A knowledge: malformed remote position");
    require(facts.navigation_status >= 0 && facts.navigation_status <= 15,
            "Class A knowledge: malformed remote navigation status");
    PeerKind kind = PeerKind::Mobile;
    bool deferred = false;
    switch (facts.kind) {
    case DecodedMessageKind::Type1: case DecodedMessageKind::Type2: case DecodedMessageKind::Type3: break;
    case DecodedMessageKind::Type4: kind = PeerKind::Base; break;
    case DecodedMessageKind::Type20: kind = PeerKind::Base; deferred = true; break;
    default: throw std::invalid_argument("Class A knowledge: message kind not qualified in A1-S3");
    }
    if (!deferred)
        require(facts.raw_communication_state >= 0 && facts.raw_communication_state <= 0x7ffff,
                "Class A knowledge: malformed remote communication state");
    const auto frame = frame_of_index(received_index);
    const auto allocation = [&](std::int64_t first, std::uint8_t block, AllocationBasis basis, std::int64_t through) {
        ExternalAllocation value;
        value.channel = observation.channel;
        value.first_index = first;
        value.block_length = block;
        value.mmsi = facts.source_mmsi;
        value.kind = kind;
        value.basis = basis;
        value.announced_minute = frame;
        value.announced_index = received_index;
        value.reserved_through_minute = through;
        value.last_use_minute = frame;
        value.receipt_id = observation.receipt_id;
        value.power_generation = generation_;
        value.provenance = observation.provenance;
        return value;
    };
    // The decoded frame physically occupied its slot: that use is always learned.
    std::vector<ExternalAllocation> allocations{allocation(received_index, 1, AllocationBasis::ObservedUse, frame)};
    std::vector<std::string> unresolved;
    bool carries_sync = false;  // Only a decodable communication state refreshes the peer's synchronization facts.
    ais::SyncState sync = ais::SyncState::Other;
    std::optional<std::uint32_t> station_count;
    std::optional<std::uint32_t> utc_hour, utc_minute_field;
    if (deferred) {
        unresolved.push_back("message_20_data_link_management_deferred:base link management is outside LCP-CLASSA-AUTO-TERR-v1 (A0-MAC-022)");
    } else {
        const ais::RadioState19 raw(facts.raw_communication_state);
        std::optional<ais::ItdmaState19> itdma;
        std::optional<ais::SotdmaState19> sotdma;
        try {
            if (facts.kind == DecodedMessageKind::Type3) itdma = ais::ItdmaState19::decode(raw);
            else sotdma = ais::SotdmaState19::decode(raw);
        } catch (const std::exception& error) {
            unresolved.push_back(std::string("communication_state_undecodable:") + error.what());
        }
        if (itdma) {
            carries_sync = true;
            sync = itdma->sync_state();
            if (itdma->keep()) {
                allocations.front().basis = AllocationBasis::ItdmaKeep;
                allocations.front().reserved_through_minute = frame + 1;
            }
            if (!itdma->no_further_allocation()) {
                const auto target_index = offset_index(received_index, itdma->effective_increment());
                const auto block = static_cast<std::uint8_t>(itdma->slot_count());
                if (target_index > horizon_last)
                    unresolved.push_back("itdma_block_beyond_local_horizon:" + std::to_string(target_index));
                else
                    allocations.push_back(allocation(target_index, block, AllocationBasis::ItdmaIncrement, frame_of_index(target_index)));
            }
        } else if (sotdma) {
            carries_sync = true;
            sync = sotdma->sync_state();
            const auto timeout = static_cast<std::int64_t>(sotdma->slot_timeout());
            const auto submessage = sotdma->submessage();
            if (const auto* offset = std::get_if<ais::SotdmaSlotOffset>(&submessage)) {
                if (offset->offset > 0) {
                    const auto target_index = offset_index(received_index, offset->offset);
                    if (frame_of_index(target_index) != frame + 1)
                        unresolved.push_back("sotdma_offset_not_next_frame:" + std::to_string(offset->offset));
                    else if (target_index > horizon_last)
                        unresolved.push_back("sotdma_offset_beyond_local_horizon:" + std::to_string(target_index));
                    else
                        allocations.push_back(allocation(target_index, 1, AllocationBasis::SotdmaNextFrameOffset, frame + 1));
                }
            } else {
                if (const auto* current = std::get_if<ais::SotdmaCurrentSlot>(&submessage)) {
                    if (current->slot != frame_slot(received_index))
                        unresolved.push_back("slot_number_submessage_disagrees_with_own_utc:" + std::to_string(current->slot));
                } else if (const auto* count = std::get_if<ais::SotdmaReceivedStations>(&submessage)) {
                    station_count = static_cast<std::uint32_t>(count->count);
                } else if (const auto* utc = std::get_if<ais::SotdmaUtc>(&submessage)) {
                    utc_hour = static_cast<std::uint32_t>(utc->hour);
                    utc_minute_field = static_cast<std::uint32_t>(utc->minute);
                }
                allocations.front().basis = AllocationBasis::SotdmaTimeout;
                allocations.front().reserved_through_minute = frame + timeout;
            }
        }
    }
    // Every token-level check has passed: stage now. The directory is staged
    // for every decoded token: the peer record is derived from the post-expiry
    // copy, so a peer that expired at `now` is re-learned as a fresh record
    // exactly as after an explicit synchronize.
    stage();
    if (!staged_directory) staged_directory.emplace(directory_);
    require(staged_directory->would_fit(facts.source_mmsi),
            "Class A knowledge: bounded peer directory capacity would be exceeded");

    PeerRecord record;
    bool existing_record = false;
    if (const auto* existing = staged_directory->find(facts.source_mmsi)) {
        record = *existing;
        existing_record = true;
    } else {
        record.mmsi = facts.source_mmsi;
        record.first_receipt_id = observation.receipt_id;
    }
    // Identity-level facts follow the physically latest decode; a late
    // disclosure of an older frame never regresses them, and a message that
    // does not carry a field (Message 20 position/state, an undecodable
    // communication state) never erases what an earlier decode established.
    if (!existing_record || received_index >= record.last_heard_index) {
        record.kind = kind;
        record.last_message = facts.kind;
        record.last_channel = observation.channel;
        record.last_heard_index = received_index;
        if (!deferred) {
            record.position_available = facts.longitude != ais::kLongitudeUnavailable &&
                                        facts.latitude != ais::kLatitudeUnavailable;
            record.longitude_ais = facts.longitude;
            record.latitude_ais = facts.latitude;
            record.position_in_geodesic_domain = false;
            if (record.position_available) {
                record.position = ais_position_to_nanodegrees(facts.longitude, facts.latitude);
                record.position_in_geodesic_domain = world::geodesy_detail::valid_point(record.position);
            }
            record.navigation_status = facts.navigation_status;
        }
        if (carries_sync) {
            record.sync_state = sync;
            record.received_station_count = station_count;
            record.utc_hour = utc_hour;
            record.utc_minute_field = utc_minute_field;
        }
    }
    record.last_disclosed_at = observation.disclosed_at;
    record.last_receipt_id = observation.receipt_id;
    record.receipts += 1;
    record.learned_generation = generation_;
    record.provenance = observation.provenance;
    record.reservations.erase(std::remove_if(record.reservations.begin(), record.reservations.end(),
        [&](const PeerAnnouncedReservation& reservation) {
            if (reservation_last_index(reservation) < earliest) return true;
            return std::any_of(allocations.begin(), allocations.end(), [&](const ExternalAllocation& value) {
                return same_reservation_lineage(reservation, value) && !reservation_physically_newer(reservation, value);
            });
        }), record.reservations.end());
    auto reservation_of = [](const ExternalAllocation& value) {
        return PeerAnnouncedReservation{value.channel, value.first_index, value.block_length, value.announced_minute,
                                        value.reserved_through_minute, value.basis, value.receipt_id, value.announced_index};
    };

    // Apply to the staged map and record: the map enforces horizon, lineage
    // recency and the bounded external store exactly, after the advance and
    // after supersession; a failure at any record discards the whole staging.
    for (const auto& value : allocations) {
        if (staged_map->record_external(value)) {
            acceptance.allocations.push_back(value);
            record.reservations.push_back(reservation_of(value));
        } else {
            acceptance.superseded.push_back(value);
        }
    }
    std::sort(record.reservations.begin(), record.reservations.end(),
        [](const PeerAnnouncedReservation& a, const PeerAnnouncedReservation& b) {
            return a.channel != b.channel ? a.channel < b.channel : a.first_index < b.first_index;
        });
    require(record.reservations.size() <= kMaxPeerReservations,
            "Class A knowledge: bounded per-peer reservation cache would be exceeded");
    staged_directory->upsert(record);
    acceptance.peer = facts.source_mmsi;
    acceptance.unresolved = std::move(unresolved);
    acceptance.status = deferred ? "decoded_base_message_deferred" : "decoded_peer_evidence_accepted";
    commit();
    return acceptance;
}

// The S3.1 commit installs staged containers by move assignment and returns
// the prepared result by move; both must be unable to throw.
static_assert(std::is_nothrow_move_assignable_v<LocalSlotMap>, "S3.1 commit needs a non-throwing LocalSlotMap move");
static_assert(std::is_nothrow_move_assignable_v<PeerDirectory>, "S3.1 commit needs a non-throwing PeerDirectory move");
static_assert(std::is_nothrow_move_assignable_v<std::vector<ReuseExclusion>>, "S3.1 commit needs a non-throwing exclusion move");
static_assert(std::is_nothrow_move_constructible_v<EvidenceAcceptance>, "S3.1 result must be returnable without throwing");

OwnReservation StationKnowledge::reserve_own(const OwnReservationRequest& request,
                                             const QualifiedOwnInputs& own_now,
                                             runtime::SimulationInstant now) {
    const auto now_index = synchronize(own_now, now);
    validate_channel(request.channel);
    OwnReservation reservation;
    reservation.channel = request.channel;
    reservation.first_index = request.first_index;
    reservation.block_length = request.block_length;
    reservation.kind = request.kind;
    reservation.timeout_frames = request.timeout_frames;
    reservation.keep = request.keep;
    reservation.power_generation = generation_;
    reservation.decided_at = now;
    reservation.decision_id = next_decision_id_;
    reservation.cause = request.cause;
    require(request.first_index > now_index, "Class A knowledge: own reservation must start after the decision slot");
    // One physical transmitter: reject opposite/adjacent own occupancy on the other channel (LCP-P05).
    const auto exclusions = own_adjacency_exclusions(own_blocks(map(other_channel(request.channel))));
    const auto retained = own_retained_frames(reservation);
    for (std::int64_t frame = 0; frame <= retained; ++frame) {
        for (std::uint8_t offset = 0; offset < reservation.block_length; ++offset) {
            const auto index = offset_index(reservation.first_index, frame * kSlotsPerFrame + offset);
            require(!std::binary_search(exclusions.begin(), exclusions.end(), index),
                    "Class A knowledge: one transmitter cannot occupy opposite or adjacent slots on both channels");
        }
    }
    map_for(request.channel).reserve_own(reservation);
    next_decision_id_ += 1;
    revision_ += 1;
    last_decision_at_ = now;
    return reservation;
}

bool StationKnowledge::release_own(Channel channel, std::int64_t first_index,
                                   const QualifiedOwnInputs& own_now, runtime::SimulationInstant now) {
    (void)synchronize(own_now, now);
    const bool released = map_for(channel).release_own(first_index);
    if (released) {
        revision_ += 1;
        last_decision_at_ = now;  // A no-op release decides nothing.
    }
    return released;
}

CandidateReport StationKnowledge::candidates(Channel channel, const SelectionInterval& interval,
                                             std::uint8_t block_length,
                                             std::optional<std::int64_t> current_own_slot_index,
                                             const QualifiedOwnInputs& own_now, runtime::SimulationInstant now) {
    const auto now_index = synchronize(own_now, now);
    CandidateContext context;
    context.selection_channel = channel;
    context.block_length = block_length;
    context.now_index = now_index;
    const auto& fields = own_now.fields();
    if (fields.longitude != ais::kLongitudeUnavailable && fields.latitude != ais::kLatitudeUnavailable)
        context.own_position = ais_position_to_nanodegrees(fields.longitude, fields.latitude);
    context.current_own_slot_index = current_own_slot_index;
    context.reuse_exclusions = reuse_exclusions_;
    context.knowledge_revision = revision_;
    context.own_inputs_fingerprint = own_inputs_fingerprint(own_now);
    auto report = build_candidates(map(channel), map(other_channel(channel)), directory_, context, interval);
    last_decision_at_ = now;
    return report;
}

DrawKey StationKnowledge::draw_key(ProtocolPurpose purpose, std::uint64_t episode, std::uint64_t draw_index) const {
    DrawKey key;
    key.scenario_seed = seed_.effective_seed();
    key.equipment_id = config_.identity().equipment_id.value;
    key.power_generation = generation_;
    key.purpose = purpose;
    key.episode = episode;
    key.draw_index = draw_index;
    return key;
}

void StationKnowledge::require_ledger_room() const {
    require(ledger_.size() < limits_.ledger_capacity,
            "Class A knowledge: draw ledger is full; drain_ledger() before further draws (no silent eviction)");
}

void StationKnowledge::check_key(ProtocolPurpose purpose, std::uint64_t episode, std::uint64_t draw_index) const {
    const auto& watermark = watermarks_[purpose_slot(purpose)];
    require(!watermark || std::make_pair(episode, draw_index) > *watermark,
            "Class A knowledge: draw key (episode, draw_index) must strictly increase per purpose within a power generation; replay rejected");
}

void StationKnowledge::commit_key(ProtocolPurpose purpose, std::uint64_t episode, std::uint64_t draw_index) {
    watermarks_[purpose_slot(purpose)] = std::make_pair(episode, draw_index);
}

void StationKnowledge::record(DrawLedgerEntry entry, runtime::SimulationInstant now) {
    require_ledger_room();
    ledger_.push_back(std::move(entry));
    ledger_issued_ += 1;
    last_decision_at_ = now;
}

CandidateChoice StationKnowledge::choose_candidate(const CandidateReport& report, std::uint64_t episode,
                                                   std::uint64_t draw_index, const QualifiedOwnInputs& own_now,
                                                   runtime::SimulationInstant now) {
    const auto now_index = synchronize(own_now, now);
    require(report.owner == config_.identity(), "Class A knowledge: candidate report belongs to another owner");
    require(report.now_index == now_index, "Class A knowledge: candidate report was built at another decision slot");
    require(report.knowledge_revision == revision_,
            "Class A knowledge: candidate report is stale (knowledge changed since it was built)");
    require(report.own_inputs_fingerprint == own_inputs_fingerprint(own_now),
            "Class A knowledge: candidate report was built from other own inputs (fix, position or clock sample)");
    require(report.interval.last_index > now_index, "Class A knowledge: candidate report interval is in the past");
    require_ledger_room();
    check_key(ProtocolPurpose::CandidateChoice, episode, draw_index);
    auto choice = ::aismixer::lab::classa::choose_candidate(report, draw_key(ProtocolPurpose::CandidateChoice, episode, draw_index));
    commit_key(ProtocolPurpose::CandidateChoice, episode, draw_index);
    if (!choice.candidate.reuse_peers.empty()) {
        for (const auto peer : choice.candidate.reuse_peers) {
            reuse_exclusions_.erase(std::remove_if(reuse_exclusions_.begin(), reuse_exclusions_.end(),
                [&](const ReuseExclusion& exclusion) { return exclusion.mmsi == peer; }),
                reuse_exclusions_.end());
            reuse_exclusions_.push_back({peer, offset_index(now_index, kReuseExclusionSlots)});
        }
        std::sort(reuse_exclusions_.begin(), reuse_exclusions_.end(),
                  [](const ReuseExclusion& a, const ReuseExclusion& b) { return a.mmsi < b.mmsi; });
        revision_ += 1;
    }
    record(choice.ledger, now);
    return choice;
}

DrawLedgerEntry StationKnowledge::draw_timeout(std::uint64_t episode, std::uint64_t draw_index,
                                               const QualifiedOwnInputs& own_now, runtime::SimulationInstant now) {
    (void)synchronize(own_now, now);
    require_ledger_room();
    check_key(ProtocolPurpose::ContinuousTimeout, episode, draw_index);
    auto entry = bounded_draw(draw_key(ProtocolPurpose::ContinuousTimeout, episode, draw_index), 3, 7, 0, "continuous_timeout");
    commit_key(ProtocolPurpose::ContinuousTimeout, episode, draw_index);
    record(entry, now);
    return entry;
}

DrawLedgerEntry StationKnowledge::draw_ratdma_probability(std::uint64_t episode, std::uint64_t draw_index,
                                                          const QualifiedOwnInputs& own_now,
                                                          runtime::SimulationInstant now) {
    (void)synchronize(own_now, now);
    require_ledger_room();
    check_key(ProtocolPurpose::GenericRatdmaProbability, episode, draw_index);
    auto entry = bounded_draw(draw_key(ProtocolPurpose::GenericRatdmaProbability, episode, draw_index), 0,
                              kRatdmaMaximumDraw, 0, "generic_ratdma_probability");
    commit_key(ProtocolPurpose::GenericRatdmaProbability, episode, draw_index);
    record(entry, now);
    return entry;
}

DrawLedgerEntry StationKnowledge::draw_entry_offset(std::int64_t minimum, std::int64_t maximum, std::uint64_t episode,
                                                    std::uint64_t draw_index, const QualifiedOwnInputs& own_now,
                                                    runtime::SimulationInstant now) {
    (void)synchronize(own_now, now);
    require(minimum >= 0 && maximum >= minimum, "Class A knowledge: entry offset bounds must be a nonnegative inclusive range");
    require_ledger_room();
    check_key(ProtocolPurpose::EntryNominalStartSlot, episode, draw_index);
    auto entry = bounded_draw(draw_key(ProtocolPurpose::EntryNominalStartSlot, episode, draw_index), minimum, maximum, 0,
                              "entry_offset_draw_only");
    commit_key(ProtocolPurpose::EntryNominalStartSlot, episode, draw_index);
    record(entry, now);
    return entry;
}

DrawLedgerEntry StationKnowledge::choose_reuse_peer(const std::vector<std::uint32_t>& peers_ascending,
                                                    std::uint64_t episode, std::uint64_t draw_index,
                                                    const QualifiedOwnInputs& own_now, runtime::SimulationInstant now) {
    (void)synchronize(own_now, now);
    require(!peers_ascending.empty() && std::is_sorted(peers_ascending.begin(), peers_ascending.end()) &&
            std::adjacent_find(peers_ascending.begin(), peers_ascending.end()) == peers_ascending.end(),
            "Class A knowledge: reuse peers must be a nonempty canonical ascending set");
    require_ledger_room();
    check_key(ProtocolPurpose::IntentionalReusePeer, episode, draw_index);
    std::string canonical;
    for (const auto peer : peers_ascending) canonical += std::to_string(peer) + ";";
    auto entry = bounded_draw(draw_key(ProtocolPurpose::IntentionalReusePeer, episode, draw_index), 0,
                              static_cast<std::int64_t>(peers_ascending.size()) - 1, fnv1a64(canonical),
                              "intentional_reuse_peer");
    commit_key(ProtocolPurpose::IntentionalReusePeer, episode, draw_index);
    entry.outcome = "intentional_reuse_peer:" + std::to_string(peers_ascending[static_cast<std::size_t>(entry.value)]);
    record(entry, now);
    return entry;
}

std::vector<DrawLedgerEntry> StationKnowledge::ledger() const {
    return std::vector<DrawLedgerEntry>(ledger_.begin(), ledger_.end());
}

std::vector<DrawLedgerEntry> StationKnowledge::drain_ledger() {
    std::vector<DrawLedgerEntry> drained(ledger_.begin(), ledger_.end());
    ledger_drained_ += drained.size();
    ledger_.clear();
    return drained;
}

KnowledgeSnapshot StationKnowledge::snapshot() const {
    KnowledgeSnapshot result;
    result.power_generation = generation_;
    result.equipment_on = on_;
    result.now_index = now_index_;
    result.knowledge_revision = revision_;
    result.last_decision_at = last_decision_at_;
    result.own_ais1 = ais1_.own_reservations();
    result.own_ais2 = ais2_.own_reservations();
    result.external_ais1 = ais1_.external_count();
    result.external_ais2 = ais2_.external_count();
    result.garble_ais1 = ais1_.garble_count();
    result.garble_ais2 = ais2_.garble_count();
    result.peers = directory_.snapshot();
    result.reuse_exclusions = reuse_exclusions_;
    for (std::size_t slot = 0; slot < watermarks_.size(); ++slot) {
        if (!watermarks_[slot]) continue;
        result.draw_watermarks.push_back({static_cast<ProtocolPurpose>(101 + slot), watermarks_[slot]->first,
                                          watermarks_[slot]->second});
    }
    result.ledger_entries = ledger_.size();
    result.ledger_issued = ledger_issued_;
    result.ledger_drained = ledger_drained_;
    result.last_receipt_id = last_receipt_id_;
    return result;
}

}  // namespace aismixer::lab::classa
