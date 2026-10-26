#include "access_engine.h"

#include <algorithm>
#include <limits>
#include <stdexcept>
#include <type_traits>
#include <utility>

namespace aismixer::lab::classa {
namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::invalid_argument(message);
}
std::size_t channel_number(Channel channel) {
    validate_channel(channel);
    return channel == Channel::Ais1 ? 0u : 1u;
}
bool same_config(const OwnStationConfig& a, const OwnStationConfig& b) {
    return a.identity() == b.identity() && a.world_revision() == b.world_revision() &&
        a.mmsi().digits() == b.mmsi().digits() && a.model_id() == b.model_id() &&
        a.schema_version() == b.schema_version() && a.profile_id() == b.profile_id() &&
        a.excluded_functions() == b.excluded_functions() &&
        a.clock().source_id == b.clock().source_id && a.clock().quality == b.clock().quality &&
        a.clock().provenance == b.clock().provenance &&
        a.clock().anchor_revision == b.clock().anchor_revision &&
        a.clock().anchor.posix_seconds == b.clock().anchor.posix_seconds &&
        a.clock().anchor.subsecond_units == b.clock().anchor.subsecond_units &&
        a.input_policy().version == b.input_policy().version &&
        a.input_policy().epoch_policy == b.input_policy().epoch_policy &&
        a.input_policy().epfs_source_id == b.input_policy().epfs_source_id &&
        a.input_policy().epfs_category == b.input_policy().epfs_category &&
        a.input_policy().provenance == b.input_policy().provenance &&
        a.input_policy().max_navigation_age_units == b.input_policy().max_navigation_age_units &&
        a.input_policy().max_clock_age_units == b.input_policy().max_clock_age_units &&
        a.origin().category == b.origin().category &&
        a.origin().identity_validity == b.origin().identity_validity &&
        a.origin().scenario_id == b.origin().scenario_id &&
        a.origin().provenance == b.origin().provenance &&
        a.manufacturer().vendor_id() == b.manufacturer().vendor_id() &&
        a.manufacturer().model_code() == b.manufacturer().model_code() &&
        a.manufacturer().serial_number() == b.manufacturer().serial_number() &&
        ais::encode(a.static_voyage()).bits == ais::encode(b.static_voyage()).bits;
}
runtime::SimulationInstant slot_start(std::int64_t index, const runtime::UtcAnchor& anchor) {
    const auto absolute = slot_from_index(index);
    const auto instant = runtime::compose_utc({absolute.utc_minute, absolute.slot, 0}, anchor);
    const auto check = runtime::decompose_utc(instant, anchor);
    require(check.units_into_slot == 0 && absolute_index(slot_from_utc(check)) == index,
            "Class A access: slot start does not roundtrip through own UTC anchor");
    return instant;
}
std::uint64_t next_identifier(std::uint64_t& next) {
    if (next == std::numeric_limits<std::uint64_t>::max())
        throw std::overflow_error("Class A access: local identifier overflow");
    return next++;
}
std::optional<std::int64_t> steady_rate(const OwnNavigationFields& fields) {
    if ((fields.navigation_status != 0 && fields.navigation_status != 1 && fields.navigation_status != 3) ||
        fields.sog_tenths_knots < 0 || fields.sog_tenths_knots > 1022)
        return std::nullopt;  // Defensive: present S2 qualification already excludes these fields.
    if (fields.navigation_status == 1 && fields.sog_tenths_knots <= 30) return 0;
    if (fields.navigation_status == 1) return 6;
    if (fields.sog_tenths_knots <= 140) return 6;  // LCP-P06: exactly 14 kn is the lower band.
    return fields.sog_tenths_knots <= 230 ? 10 : 30;
}
ais::EncodedPayload payload_from(const OwnStationConfig& config, const QualifiedOwnInputs& own,
                                const ais::ItdmaState19& state) {
    ais::Type3 message(config.mmsi(), state);
    const auto& fields = own.fields();
    message.navigation_status = fields.navigation_status;
    message.rot_wire = fields.rot_wire;
    message.sog_tenths_knots = fields.sog_tenths_knots;
    message.position_accuracy = fields.position_accuracy;
    message.longitude = fields.longitude;
    message.latitude = fields.latitude;
    message.cog_tenths_degrees = fields.cog_tenths_degrees;
    message.true_heading_degrees = fields.true_heading_degrees;
    message.utc_second = fields.utc_second;
    message.raim = fields.raim;
    return ais::encode(message);
}
}  // namespace

const char* access_state_name(AccessState state) {
    switch (state) {
    case AccessState::Off: return "off";
    case AccessState::Observe60Seconds: return "observe_60_seconds";
    case AccessState::EntrySelect: return "entry_select";
    case AccessState::EntryWait: return "entry_wait";
    case AccessState::FirstFrame: return "first_frame";
    case AccessState::Continuous: return "continuous_marker";
    case AccessState::Blocked: return "blocked";
    }
    throw std::invalid_argument("Class A access: unknown access state");
}
const char* channel_phase_name(ChannelPhase phase) {
    switch (phase) {
    case ChannelPhase::Idle: return "idle";
    case ChannelPhase::WaitingFirst: return "waiting_first";
    case ChannelPhase::FirstFrameChain: return "first_frame_chain";
    case ChannelPhase::ContinuousSotdma: return "continuous_sotdma_marker";
    }
    throw std::invalid_argument("Class A access: unknown channel phase");
}
const char* intent_status_name(IntentStatus status) {
    switch (status) {
    case IntentStatus::Proposed: return "proposed";
    case IntentStatus::Due: return "due";
    case IntentStatus::Withdrawn: return "withdrawn";
    case IntentStatus::Missed: return "missed";
    case IntentStatus::Blocked: return "blocked";
    }
    throw std::invalid_argument("Class A access: unknown intent status");
}
const char* intent_message_name(IntentMessage message) {
    switch (message) {
    case IntentMessage::Type3Entry: return "type3_entry";
    case IntentMessage::Type3FirstFrame: return "type3_first_frame";
    case IntentMessage::Type3Terminal: return "type3_terminal";
    }
    throw std::invalid_argument("Class A access: unknown intent message");
}
const char* blocked_reason_name(BlockedReason reason) {
    switch (reason) {
    case BlockedReason::None: return "none";
    case BlockedReason::NoUtc: return "blocked_no_utc";
    case BlockedReason::OwnInputsUnavailable: return "blocked_own_inputs_unavailable";
    case BlockedReason::LowRatePolicyAbsent: return "blocked_low_rate_policy_absent";
    case BlockedReason::UnsupportedRate: return "blocked_unsupported_rate";
    case BlockedReason::NoLegalCandidates: return "blocked_no_legal_candidates";
    case BlockedReason::MissedDeadline: return "missed_exact_nts_deadline";
    case BlockedReason::ReservationConflict: return "blocked_reservation_conflict";
    case BlockedReason::ReservationFailure: return "blocked_reservation_failure";
    case BlockedReason::HistoryCapacity: return "blocked_history_capacity";
    case BlockedReason::RateChangeNotImplemented: return "blocked_rate_change_not_implemented";
    case BlockedReason::LedgerOrDrawFailure: return "blocked_ledger_or_draw_failure";
    }
    throw std::invalid_argument("Class A access: unknown blocked reason");
}

struct ClassAAccessEngine::Impl {
    struct Data {
        AccessState state = AccessState::Off;
        BlockedReason blocked = BlockedReason::None;
        std::uint64_t generation = 0;
        bool initialized = false, on = false;
        std::optional<runtime::SimulationInstant> observation_deadline, last_off_at;
        std::array<ChannelPlan, 2> plans;
        std::array<std::optional<PlannedSlot>, 2> pending;
        std::array<std::vector<std::uint64_t>, 2> first_ledgers;
        std::vector<TxIntent> history;
        std::vector<PlannedSlot> proposals;
        std::vector<std::pair<Channel, std::int64_t>> reservations;
        std::int64_t rr = 0, ni = 0;
        std::uint32_t attempts = 0;
        Data() { plans[0].channel = Channel::Ais1; plans[1].channel = Channel::Ais2; }
    } data;
    static_assert(std::is_nothrow_move_assignable_v<Data>, "access engine state installation must not throw");
    static_assert(std::is_nothrow_move_constructible_v<EngineStep>, "access result must move without throwing");
    OwnStationConfig config;
    StationInputGate& gate;
    StationKnowledge& knowledge;
    AccessPolicy policy;
    std::optional<runtime::SimulationInstant> latest_time, last_step_at, last_phase_two_at;
    std::array<std::uint64_t, kProtocolPurposeCount> draw_episodes{};
    std::uint64_t next_proposal = 1, next_decision = 1;

    Impl(const OwnStationConfig& own_config, StationInputGate& own_gate,
         StationKnowledge& own_knowledge, AccessPolicy selected)
        : config(own_config), gate(own_gate), knowledge(own_knowledge), policy(std::move(selected)) {
        require(policy.version == kAccessEngineVersion && policy.first_frame_policy_id == kFirstFramePolicyId,
                "Class A access: unsupported engine/first-frame policy version");
        require(policy.entry_retry_limit >= 1 && policy.entry_retry_limit <= 256,
                "Class A access: entry retry bound must be 1..256 (CLASSA-ENTRY-RETRY-v1)");
        require(policy.history_capacity >= 1 && policy.history_capacity <= 65536,
                "Class A access: history capacity must be 1..65536 records");
        require(policy.rate_source == RateSource::OwnSteadyNavigation || policy.rate_source == RateSource::FixedFixture,
                "Class A access: unsupported rate source");
        if (policy.rate_source == RateSource::FixedFixture) {
            require(!policy.rate_provenance.empty() && policy.rate_provenance.size() <= 256 &&
                    policy.rate_provenance.find_first_not_of(' ') != std::string::npos,
                    "Class A access: fixed fixture rate requires explicit bounded provenance");
            for (const unsigned char value : policy.rate_provenance)
                require(value >= 32 && value <= 126, "Class A access: rate provenance must be printable ASCII");
        }
        require(same_config(config, gate.config()) && same_config(config, knowledge.config()),
                "Class A access: configuration/gate/knowledge belong to different stations or scenarios");
        require(gate.equipment_on() == knowledge.equipment_on() &&
                gate.power_generation() == knowledge.power_generation(),
                "Class A access: gate and knowledge lifecycle disagree");
    }

    void check_time(runtime::SimulationInstant now) const {
        require(!latest_time || now >= *latest_time, "Class A access: reversed local time");
    }
    void check_external_power() const {
        require(gate.equipment_on() == data.on && knowledge.equipment_on() == data.on &&
                gate.power_generation() == data.generation && knowledge.power_generation() == data.generation,
                "Class A access: dispatch gate then knowledge then engine power notices before decisions");
    }
    void check_own(const QualifiedOwnInputs& own, runtime::SimulationInstant now) {
        check_time(now);
        require(data.initialized && data.on, "Class A access: own engine is OFF/uninitialized");
        check_external_power();
        require(own.qualified_at() == now && own.navigation().identity() == config.identity() &&
                own.clock().identity() == config.identity() &&
                own.navigation().power_generation() == data.generation &&
                own.clock().power_generation() == data.generation,
                "Class A access: foreign/stale/non-current own qualification");
        require(own.input_eligible() && !own.ready_for_tx() && !knowledge.ready_for_tx(),
                "Class A access: input eligibility is not physical transmit readiness");
        (void)gate.revalidate(own, now);
        (void)knowledge.synchronize(own, now); // Also proves knowledge's private actual gate binding.
        latest_time = now;
    }
    bool room(const Data& staged, std::size_t additional) const noexcept {
        const auto used = staged.history.size() + staged.proposals.size();
        return used <= policy.history_capacity && additional <= policy.history_capacity - used;
    }
    void cancel_pending(Data& staged, EngineStep& result, const char* reason,
                        IntentStatus status = IntentStatus::Withdrawn) {
        for (std::size_t channel = 0; channel < 2; ++channel) {
            if (!staged.pending[channel]) continue;
            auto proposal = *staged.pending[channel];
            proposal.status = status;
            proposal.reason = reason;
            result.withdrawn.push_back(proposal);
            result.timers.push_back({TimerChange::Kind::NtsDue, proposal.channel, proposal.slot_start, true});
            for (auto& historical : staged.proposals)
                if (historical.proposal_id == proposal.proposal_id) historical = proposal;
            staged.pending[channel].reset();
            staged.plans[channel].next_due_index.reset();
        }
    }
    void block(Data& staged, EngineStep& result, BlockedReason reason) {
        cancel_pending(staged, result, blocked_reason_name(reason),
                       reason == BlockedReason::MissedDeadline ? IntentStatus::Missed : IntentStatus::Withdrawn);
        if (staged.observation_deadline) {
            result.timers.push_back({TimerChange::Kind::ObservationComplete, std::nullopt,
                                     *staged.observation_deadline, true});
            staged.observation_deadline.reset();
        }
        staged.state = AccessState::Blocked;
        staged.blocked = reason;
        result.blocked.push_back(reason);
        result.decisions.emplace_back(blocked_reason_name(reason));
    }
    std::uint64_t episode(ProtocolPurpose purpose) {
        const auto position = static_cast<std::size_t>(static_cast<unsigned>(purpose) - 101u);
        require(position < draw_episodes.size(), "Class A access: unknown draw purpose");
        if (draw_episodes[position] > kMaxEpisodeKey)
            throw std::overflow_error("Class A access: semantic draw episode exhausted");
        // Burn the key before the fallible S3 call; never replay it after a
        // failed operation or a compensated reservation. First key is episode0.
        return draw_episodes[position]++;
    }
    std::pair<DrawLedgerEntry, std::uint64_t> entry_draw(const QualifiedOwnInputs& own,
                                                       runtime::SimulationInstant now, std::int64_t ni) {
        auto entry = knowledge.draw_entry_offset(1, ni, episode(ProtocolPurpose::EntryNominalStartSlot), 0, own, now);
        return {std::move(entry), knowledge.ledger_issued()};
    }
    std::pair<CandidateChoice, std::uint64_t> candidate_draw(const CandidateReport& report,
        const QualifiedOwnInputs& own, runtime::SimulationInstant now) {
        auto choice = knowledge.choose_candidate(report, episode(ProtocolPurpose::CandidateChoice), 0, own, now);
        return {std::move(choice), knowledge.ledger_issued()};
    }
    std::pair<DrawLedgerEntry, std::uint64_t> timeout_draw(const QualifiedOwnInputs& own,
                                                         runtime::SimulationInstant now) {
        auto entry = knowledge.draw_timeout(episode(ProtocolPurpose::ContinuousTimeout), 0, own, now);
        return {std::move(entry), knowledge.ledger_issued()};
    }
    std::int64_t reporting_rate(const QualifiedOwnInputs& own, BlockedReason& reason) const {
        if (policy.rate_source == RateSource::OwnSteadyNavigation) {
            const auto rate = steady_rate(own.fields());
            if (!rate) { reason = BlockedReason::UnsupportedRate; return 0; }
            if (*rate == 0) reason = BlockedReason::LowRatePolicyAbsent;
            return *rate;
        }
        const auto numerator = policy.fixed_rate_numerator;
        const auto denominator = policy.fixed_rate_denominator;
        if (numerator <= 0 || denominator <= 0) { reason = BlockedReason::UnsupportedRate; return 0; }
        // Avoid multiplication by two when an arbitrary caller supplies MAX.
        if (numerator / denominator < 2) { reason = BlockedReason::LowRatePolicyAbsent; return 0; }
        if (numerator % denominator != 0) { reason = BlockedReason::UnsupportedRate; return 0; }
        const auto rate = numerator / denominator;
        if (rate != 6 && rate != 10 && rate != 18 && rate != 30) {
            reason = BlockedReason::UnsupportedRate;
            return 0;
        }
        return rate;
    }
    PlannedSlot proposal(Channel channel, std::int64_t index, std::int64_t nss,
                         std::uint32_t ordinal, std::vector<std::uint64_t> ledgers,
                         const QualifiedOwnInputs& own, const char* reason) {
        PlannedSlot planned;
        planned.owner = config.identity();
        planned.power_generation = data.generation;
        planned.channel = channel;
        planned.first_slot_index = index;
        planned.slot_start = slot_start(index, config.clock().anchor);
        require(planned.slot_start > own.qualified_at(), "Class A access: a proposal must be strictly future");
        planned.proposal_id = next_identifier(next_proposal);
        planned.ordinal = ordinal;
        planned.nss_index = nss;
        planned.knowledge_revision = knowledge.knowledge_revision();
        planned.inputs_fingerprint = own_inputs_fingerprint(own);
        planned.ledger_sequences = std::move(ledgers);
        planned.reason = reason;
        return planned;
    }
    void publish_proposal(Data& staged, EngineStep& result, std::size_t channel, PlannedSlot planned) {
        staged.pending[channel] = planned;
        staged.plans[channel].next_due_index = planned.first_slot_index;
        staged.proposals.push_back(planned);
        result.proposals.push_back(planned);
        result.timers.push_back({TimerChange::Kind::NtsDue, planned.channel, planned.slot_start, false});
    }
    OwnReservation reserve(Channel channel, std::int64_t index, std::int64_t drawn_timeout,
                           const QualifiedOwnInputs& own, runtime::SimulationInstant now) {
        OwnReservationRequest request;
        request.channel = channel;
        request.first_index = index;
        request.kind = OwnReservationKind::Sotdma;
        request.timeout_frames = retained_frames_from_timeout_draw(drawn_timeout);
        request.cause = "CLASSA-FIRST-FRAME-v1:local_intention_not_air_grant";
        return knowledge.reserve_own(request, own, now);
    }
    // S3 reservation operations intentionally retain their revision and draw
    // effects. Compensation removes only this attempt's new own blocks. It is
    // not a cross-object noexcept transaction: S3 revalidation can itself throw
    // (e.g. allocation failure), which is propagated with an explicit diagnostic.
    void compensate(const std::vector<std::pair<Channel, std::int64_t>>& added,
                    const QualifiedOwnInputs& own, runtime::SimulationInstant now) {
        for (auto iterator = added.rbegin(); iterator != added.rend(); ++iterator) {
            if (!knowledge.release_own(iterator->first, iterator->second, own, now))
                throw std::runtime_error("Class A access: reservation compensation lost its own block");
        }
    }
    void enter(Data& staged, EngineStep& result, const QualifiedOwnInputs& own,
               runtime::SimulationInstant now) {
        if (!room(staged, 2)) { block(staged, result, BlockedReason::HistoryCapacity); return; }
        const auto current = absolute_index(slot_from_utc(own.current_slot()));
        staged.state = AccessState::EntrySelect;
        for (std::uint32_t attempt = 0; attempt < policy.entry_retry_limit; ++attempt) {
            staged.attempts = attempt + 1;
            const auto entry = entry_draw(own, now, staged.ni);
            const auto nss_a = offset_index(current, entry.first.value);
            const std::array<std::int64_t, 2> nominal{nss_a, offset_index(nss_a, staged.ni)};
            std::array<std::optional<PlannedSlot>, 2> chosen;
            std::array<std::uint64_t, 2> reservation_ids{};
            std::vector<std::pair<Channel, std::int64_t>> added;
            added.reserve(2);
            try {
                bool exhausted = false;
                for (std::size_t channel = 0; channel < 2; ++channel) {
                    const auto selected_channel = channel == 0 ? Channel::Ais1 : Channel::Ais2;
                    const auto report = knowledge.candidates(selected_channel,
                        selection_interval(nominal[channel], staged.ni), 1, std::nullopt, own, now);
                    if (report.candidates.empty()) { exhausted = true; break; }
                    const auto choice = candidate_draw(report, own, now);
                    const auto timeout = timeout_draw(own, now);
                    // Stage the actual map's validation before the first commit
                    // on this channel; S3 remains authority for opposite-channel
                    // retained-frame checks and performs them on reserve_own.
                    auto map_check = knowledge.map(selected_channel);
                    OwnReservation checked;
                    checked.channel = selected_channel;
                    checked.first_index = choice.first.candidate.first_index;
                    checked.kind = OwnReservationKind::Sotdma;
                    checked.timeout_frames = retained_frames_from_timeout_draw(timeout.first.value);
                    checked.power_generation = data.generation;
                    map_check.reserve_own(checked);
                    auto planned = proposal(selected_channel, checked.first_index, nominal[channel], 0,
                        {entry.second, choice.second, timeout.second}, own, "entry_slot_prepared_not_transmitted");
                    const auto reservation = reserve(selected_channel, checked.first_index, timeout.first.value, own, now);
                    added.emplace_back(selected_channel, checked.first_index);
                    reservation_ids[channel] = reservation.decision_id;
                    chosen[channel] = std::move(planned);
                }
                if (exhausted) {
                    compensate(added, own, now);
                    added.clear();
                    result.decisions.push_back("entry_retry_no_legal_candidate:" + std::to_string(attempt + 1));
                    continue;
                }
                for (std::size_t channel = 0; channel < 2; ++channel) {
                    staged.plans[channel].nss_index = nominal[channel];
                    staged.plans[channel].first_nts_index = chosen[channel]->first_slot_index;
                    staged.plans[channel].first_reservation_decision_id = reservation_ids[channel];
                    staged.plans[channel].phase = ChannelPhase::WaitingFirst;
                    staged.first_ledgers[channel] = chosen[channel]->ledger_sequences;
                    publish_proposal(staged, result, channel, *chosen[channel]);
                }
                staged.reservations.insert(staged.reservations.end(), added.begin(), added.end());
                staged.observation_deadline.reset();
                staged.state = AccessState::EntryWait;
                result.decisions.emplace_back("dual_channel_entry_prepared:local_intentions_only");
                return;
            } catch (...) {
                compensate(added, own, now);
                throw;
            }
        }
        block(staged, result, BlockedReason::NoLegalCandidates);
    }
    void due(Data& staged, EngineStep& result, std::size_t channel,
             const QualifiedOwnInputs& own, runtime::SimulationInstant now) {
        const auto pending = *staged.pending[channel];
        auto& plan = staged.plans[channel];
        const auto count = static_cast<std::uint32_t>(staged.rr / 2);
        const bool terminal = pending.ordinal == count;
        if (!room(staged, terminal ? 1 : 2)) { block(staged, result, BlockedReason::HistoryCapacity); return; }
        const auto* retained = knowledge.map(pending.channel).own_covering(pending.first_slot_index);
        require(retained && retained->power_generation == data.generation,
                "Class A access: due slot no longer has its local own reservation");
        if (terminal)
            require(retained->decision_id == plan.first_reservation_decision_id,
                    "Class A access: terminal must be the original first slot's already-kept repeat");
        const auto exclusions = own_adjacency_exclusions(own_blocks(knowledge.map(other_channel(pending.channel))));
        require(!std::binary_search(exclusions.begin(), exclusions.end(), pending.first_slot_index),
                "Class A access: shared transmitter has an opposite or adjacent own reservation");

        std::optional<PlannedSlot> successor;
        std::vector<std::pair<Channel, std::int64_t>> added;
        added.reserve(1);
        std::vector<std::uint64_t> ledgers = pending.ledger_sequences;
        try {
            std::int64_t increment = 0;
            if (!terminal) {
                std::int64_t next = 0;
                if (pending.ordinal + 1 == count) {
                    next = offset_index(*plan.first_nts_index, kSlotsPerFrame);
                    const auto* first = knowledge.map(pending.channel).own_covering(next);
                    require(first && first->decision_id == plan.first_reservation_decision_id &&
                            first->power_generation == data.generation,
                            "Class A access: terminal repeat is not retained by the original first reservation");
                    successor = proposal(pending.channel, next, *plan.nss_index, pending.ordinal + 1,
                                         staged.first_ledgers[channel], own, "already_kept_terminal_repeat");
                } else {
                    const auto ordinal = static_cast<std::int64_t>(pending.ordinal + 1);
                    const auto ns = offset_index(*plan.nss_index, ordinal * (2 * staged.ni));
                    const auto report = knowledge.candidates(pending.channel, selection_interval(ns, staged.ni),
                                                              1, std::nullopt, own, now);
                    if (report.candidates.empty()) { block(staged, result, BlockedReason::NoLegalCandidates); return; }
                    const auto choice = candidate_draw(report, own, now);
                    const auto timeout = timeout_draw(own, now);
                    next = choice.first.candidate.first_index;
                    successor = proposal(pending.channel, next, *plan.nss_index, pending.ordinal + 1,
                                         {choice.second, timeout.second}, own, "same_channel_successor_prepared");
                    // Complete all payload validation and staging below before
                    // publication; compensation removes a failed new successor.
                    (void)reserve(pending.channel, next, timeout.first.value, own, now);
                    added.emplace_back(pending.channel, next);
                }
                increment = runtime::difference_units({next}, {pending.first_slot_index});
                require(increment > 0 && increment <= 16383,
                        "Class A access: actual next-slot difference is not a positive ITDMA increment");
            }
            const auto state = ais::ItdmaState19::from_effective(own.sync_state(), increment, 1, true);
            require(!terminal || state.no_further_allocation(), "Class A access: terminal is not true effective zero");
            TxIntent intent;
            intent.owner = config.identity();
            intent.power_generation = data.generation;
            intent.channel = pending.channel;
            intent.first_slot_index = pending.first_slot_index;
            intent.slot_start = pending.slot_start;
            intent.ordinal = pending.ordinal;
            intent.nss_index = *plan.nss_index;
            intent.message = terminal ? IntentMessage::Type3Terminal
                           : pending.ordinal == 0 ? IntentMessage::Type3Entry : IntentMessage::Type3FirstFrame;
            if (successor) {
                intent.next_slot_index = successor->first_slot_index;
                ledgers.insert(ledgers.end(), successor->ledger_sequences.begin(), successor->ledger_sequences.end());
            }
            std::sort(ledgers.begin(), ledgers.end());
            ledgers.erase(std::unique(ledgers.begin(), ledgers.end()), ledgers.end());
            intent.communication_state_19 = state.raw().value();
            intent.proposal_id = pending.proposal_id;
            intent.decision_id = next_identifier(next_decision);
            intent.knowledge_revision = knowledge.knowledge_revision();
            intent.inputs_fingerprint = own_inputs_fingerprint(own);
            intent.fields = own.fields();
            intent.provenance = own.provenance();
            intent.ledger_sequences = std::move(ledgers);
            intent.payload = payload_from(config, own, state);
            intent.reason = terminal ? "due_terminal_zero_not_transmitted" : "due_type3_not_transmitted";
            staged.history.push_back(intent);
            result.intents.push_back(std::move(intent));
            for (auto& historical : staged.proposals) {
                if (historical.proposal_id == pending.proposal_id) {
                    historical.status = IntentStatus::Due;
                    historical.reason = "due_local_intent_not_transmitted";
                }
            }
            staged.pending[channel].reset();
            plan.next_due_index.reset();
            plan.transmissions_done += 1;
            plan.phase = terminal ? ChannelPhase::ContinuousSotdma : ChannelPhase::FirstFrameChain;
            if (successor) publish_proposal(staged, result, channel, *successor);
            staged.reservations.insert(staged.reservations.end(), added.begin(), added.end());
            staged.state = staged.plans[0].phase == ChannelPhase::ContinuousSotdma &&
                           staged.plans[1].phase == ChannelPhase::ContinuousSotdma
                               ? AccessState::Continuous : AccessState::FirstFrame;
            result.decisions.emplace_back(terminal ? "per_channel_terminal_zero_prepared" : "first_frame_due_intent_prepared");
        } catch (...) {
            compensate(added, own, now);
            throw;
        }
    }
};

ClassAAccessEngine::ClassAAccessEngine(const OwnStationConfig& config, StationInputGate& gate,
                                     StationKnowledge& knowledge, AccessPolicy policy)
    : impl_(std::make_unique<Impl>(config, gate, knowledge, std::move(policy))) {}
ClassAAccessEngine::~ClassAAccessEngine() = default;

EngineStep ClassAAccessEngine::apply_power(const OwnPowerNotice& notice) {
    auto& self = *impl_;
    require(notice.own_identity == self.config.identity(), "Class A access: foreign own power notice");
    self.check_time(notice.time);
    // The host seam rejects phase inversion as the future scheduler will.
    // It cannot undo an earlier incorrect external gate/knowledge dispatch.
    require((!self.last_step_at || notice.time > *self.last_step_at) &&
            (!self.last_phase_two_at || notice.time > *self.last_phase_two_at),
            "Class A access: phase-0 power notice delivered after phase-2/3 work at this instant");
    const bool on = notice.kind == OwnPowerKind::On;
    require(self.gate.equipment_on() == on && self.knowledge.equipment_on() == on &&
            self.gate.power_generation() == notice.power_generation &&
            self.knowledge.power_generation() == notice.power_generation,
            "Class A access: gate and knowledge must receive this power notice before engine");
    switch (notice.kind) {
    case OwnPowerKind::InitialOff:
        require(!self.data.initialized && notice.power_generation == 0,
                "Class A access: InitialOff is generation-zero bootstrap only");
        break;
    case OwnPowerKind::On:
        require(!self.data.on && notice.power_generation > self.data.generation &&
                notice.power_generation <= kMaxPowerGenerationKey,
                "Class A access: duplicate or stale/out-of-domain ON epoch");
        require(!self.data.last_off_at || notice.time != *self.data.last_off_at,
                "Class A access: OFF dominates ON at the same instant");
        break;
    case OwnPowerKind::Off:
        require(notice.power_generation >= self.data.generation, "Class A access: stale OFF generation");
        break;
    default: throw std::invalid_argument("Class A access: unknown power notice");
    }
    Impl::Data staged = self.data;
    EngineStep result;
    self.cancel_pending(staged, result, "cancelled_by_off");
    if (staged.observation_deadline)
        result.timers.push_back({TimerChange::Kind::ObservationComplete, std::nullopt,
                                 *staged.observation_deadline, true});
    staged.plans = {};
    staged.plans[0].channel = Channel::Ais1;
    staged.plans[1].channel = Channel::Ais2;
    staged.first_ledgers = {};
    staged.reservations.clear();
    staged.observation_deadline.reset();
    staged.generation = notice.power_generation;
    staged.initialized = true;
    staged.on = on;
    staged.rr = staged.ni = 0;
    staged.attempts = 0;
    staged.blocked = BlockedReason::None;
    staged.state = on ? AccessState::Observe60Seconds : AccessState::Off;
    if (on) {
        staged.observation_deadline = runtime::add_units(notice.time, runtime::kUnitsPerFrame);
        result.timers.push_back({TimerChange::Kind::ObservationComplete, std::nullopt,
                                 *staged.observation_deadline, false});
    } else if (notice.kind == OwnPowerKind::Off) staged.last_off_at = notice.time;
    self.data = std::move(staged);
    self.latest_time = notice.time;
    self.last_step_at.reset();
    self.last_phase_two_at.reset();
    self.draw_episodes = {};
    result.state = self.data.state;
    return result;
}

EvidenceAcceptance ClassAAccessEngine::deliver_evidence(const OwnedRadioEvidence& evidence,
    const QualifiedOwnInputs& own_now, runtime::SimulationInstant now) {
    auto& self = *impl_;
    require(!self.last_step_at || now > *self.last_step_at,
            "Class A access: phase-2 evidence delivered after this instant's phase-3 step");
    self.check_own(own_now, now);  // Explicit phase-group synchronize; S3.1 owns token transaction.
    // Own validation has entered phase 2 even if the following token is rejected.
    self.last_phase_two_at = now;
    return self.knowledge.accept_evidence(evidence, own_now, now);
}

EngineStep ClassAAccessEngine::step(const QualifiedOwnInputs& own_now, runtime::SimulationInstant now) {
    auto& self = *impl_;
    self.check_own(own_now, now); // Both actual owning gates verified BEFORE any draw/reservation.
    EngineStep result;
    result.state = self.data.state;
    if (self.last_step_at && now == *self.last_step_at) return result;
    if (self.data.state == AccessState::Blocked || self.data.state == AccessState::Continuous) {
        self.last_step_at = now;
        return result;
    }
    Impl::Data staged = self.data;
    try {
    BlockedReason rate_failure = BlockedReason::None;
    const auto rate = self.reporting_rate(own_now, rate_failure);
    if (rate_failure != BlockedReason::None) {
        self.block(staged, result, rate_failure);
    } else if (staged.rr != 0 && self.policy.rate_source == RateSource::OwnSteadyNavigation && rate != staged.rr) {
        self.block(staged, result, BlockedReason::RateChangeNotImplemented);
    } else if (staged.state == AccessState::Observe60Seconds) {
        if (now >= *staged.observation_deadline) {
            staged.rr = rate;
            staged.ni = kSlotsPerFrame / rate;
            self.enter(staged, result, own_now, now);
        }
    } else {
        for (const auto& pending : staged.pending) {
            if (pending && now > pending->slot_start) {
                self.block(staged, result, BlockedReason::MissedDeadline);
                break;
            }
        }
        if (staged.state != AccessState::Blocked) {
            std::optional<std::size_t> due_channel;
            for (std::size_t channel = 0; channel < 2; ++channel) {
                if (!staged.pending[channel] || staged.pending[channel]->slot_start != now) continue;
                require(!due_channel, "Class A access: two channels due at one shared transmitter instant");
                due_channel = channel;
            }
            if (due_channel) self.due(staged, result, *due_channel, own_now, now);
        }
    }
    } catch (const std::overflow_error&) {
        // Exact arithmetic failures remain explicit overflow errors. Any new
        // reservation has already been compensated by enter/due.
        throw;
    } catch (const std::invalid_argument& error) {
        const std::string diagnostic = error.what();
        self.block(staged, result, diagnostic.find("ledger") != std::string::npos ||
                   diagnostic.find("draw") != std::string::npos
                       ? BlockedReason::LedgerOrDrawFailure : BlockedReason::ReservationFailure);
        result.decisions.push_back("operation_rejected:" + diagnostic);
    } catch (const std::runtime_error& error) {
        const std::string diagnostic = error.what();
        if (diagnostic.find("compensation") != std::string::npos) throw;
        self.block(staged, result, BlockedReason::LedgerOrDrawFailure);
        result.decisions.push_back("operation_rejected:" + diagnostic);
    }
    self.data = std::move(staged);
    self.last_step_at = now;
    result.state = self.data.state;
    return result;
}

EngineStep ClassAAccessEngine::block_inputs(runtime::SimulationInstant now, BlockedReason reason) {
    require(reason == BlockedReason::NoUtc || reason == BlockedReason::OwnInputsUnavailable,
            "Class A access: missing-input path requires explicit no-UTC/own-inputs reason");
    auto& self = *impl_;
    self.check_time(now);
    self.check_external_power();
    EngineStep result;
    if (self.data.state != AccessState::Off && self.data.state != AccessState::Blocked) {
        Impl::Data staged = self.data;
        self.block(staged, result, reason);
        self.data = std::move(staged);
    }
    self.latest_time = now;
    self.last_step_at = now;
    result.state = self.data.state;
    return result;
}

AccessState ClassAAccessEngine::state() const noexcept { return impl_->data.state; }
const ChannelPlan& ClassAAccessEngine::channel_plan(Channel channel) const {
    return impl_->data.plans[channel_number(channel)];
}
EngineSnapshot ClassAAccessEngine::snapshot() const {
    const auto& self = *impl_;
    EngineSnapshot result;
    result.owner = self.config.identity();
    result.power_generation = self.data.generation;
    result.state = self.data.state;
    result.blocked_reason = self.data.blocked;
    result.observation_deadline = self.data.observation_deadline;
    result.channels = self.data.plans;
    for (const auto& pending : self.data.pending) if (pending) result.pending.push_back(*pending);
    result.reports_per_minute = self.data.rr;
    result.nominal_increment = self.data.ni;
    result.entry_attempts = self.data.attempts;
    result.next_draw_episode = self.draw_episodes;
    result.due_history_records = self.data.history.size();
    result.proposal_history_records = self.data.proposals.size();
    return result;
}
const std::vector<TxIntent>& ClassAAccessEngine::history() const noexcept { return impl_->data.history; }
const std::vector<PlannedSlot>& ClassAAccessEngine::proposal_history() const noexcept { return impl_->data.proposals; }
std::vector<TxIntent> ClassAAccessEngine::drain_history() {
    std::vector<TxIntent> exported;
    exported.swap(impl_->data.history);
    return exported;
}
std::vector<PlannedSlot> ClassAAccessEngine::drain_proposal_history() {
    require(!impl_->data.pending[0] && !impl_->data.pending[1],
            "Class A access: export proposal history only after pending proposals ended");
    std::vector<PlannedSlot> exported;
    exported.swap(impl_->data.proposals);
    return exported;
}

}  // namespace aismixer::lab::classa
