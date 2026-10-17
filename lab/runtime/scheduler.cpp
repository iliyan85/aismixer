#include "scheduler.h"

#include <map>
#include <set>
#include <stdexcept>
#include <utility>

namespace aismixer::lab::runtime {
struct Scheduler::Impl {
    struct Source { std::uint64_t sequence; SimulationInstant time; std::uint8_t phase; std::uint64_t dispatched = 0; };
    explicit Impl(ValidationProfile selected) : validator(selected) {}
    std::size_t capacity;
    SimulationInstant watermark;
    std::multiset<Event, CanonicalLess> pending;
    std::map<std::string, Source> sources;
    EventValidator validator;
    std::vector<std::shared_ptr<const CausalProducerBarrier>> barriers;
    std::optional<Event> last;
    std::optional<std::pair<SimulationInstant, std::uint8_t>> dispatching;
    bool advancing = false;
    bool sealed = false;
    bool failed = false;
    bool barriers_locked = false;

    void drain(const std::optional<SimulationInstant>& target, const PhaseConsumer& consume) {
        if (sealed) throw std::invalid_argument("scheduler is terminally sealed");
        if (advancing) throw std::invalid_argument("scheduler advance cannot be recursive");
        if (!consume) throw std::invalid_argument("scheduler requires an event consumer");
        if (target && *target < watermark) throw std::invalid_argument("commit watermark cannot regress");
        for (const auto& barrier : barriers) {
            if (target) barrier->validate_advance(*target); else barrier->validate_finish();
        }
        barriers_locked = true;
        advancing = true;
        try {
            while (!pending.empty() && (!target || pending.begin()->time < *target)) {
                const auto time = pending.begin()->time;
                const auto phase = causal_phase(pending.begin()->kind);
                std::vector<Event> group;
                while (!pending.empty() && pending.begin()->time == time && causal_phase(pending.begin()->kind) == phase) {
                    group.push_back(*pending.begin());
                    pending.erase(pending.begin());
                }
                // Validate the entire phase before exposing any of its records.
                // A failure is terminal: some earlier phases may be committed.
                for (const auto& event : group) { validator.append(event); last = event; }
                for (const auto& event : group) sources.at(event.source_id).dispatched = event.event_id.sequence;
                dispatching = std::make_pair(time, phase);
                consume(group);
                dispatching.reset();
            }
            for (const auto& barrier : barriers) {
                if (target) barrier->validate_advance(*target); else barrier->validate_finish();
            }
            if (target) {
                watermark = *target;
                for (const auto& barrier : barriers) barrier->committed(watermark);
            }
            else { validator.finish(); sealed = true; }
            advancing = false;
        } catch (...) {
            sealed = true; failed = true; advancing = false; dispatching.reset();
            throw;
        }
    }
};
Scheduler::Scheduler(std::size_t capacity, SimulationInstant initial_watermark, ValidationProfile profile) : impl_(std::make_unique<Impl>(profile)) {
    if (capacity == 0 || capacity > 65536) throw std::invalid_argument("pending event capacity must be 1..65536");
    impl_->capacity = capacity; impl_->watermark = initial_watermark;
}
Scheduler::~Scheduler() = default;
Scheduler::Scheduler(Scheduler&&) noexcept = default;
Scheduler& Scheduler::operator=(Scheduler&&) noexcept = default;
void Scheduler::enqueue(Event event) {
    auto& state = *impl_;
    if (state.sealed) throw std::invalid_argument("scheduler is terminally sealed");
    validate_event(event);
    if (event.schema_version != static_cast<std::uint32_t>(state.validator.profile()))
        throw std::invalid_argument("scheduler event schema does not match validation profile");
    if (event.time < state.watermark) throw std::invalid_argument("event is before committed half-open watermark");
    if (state.last && !CanonicalLess{}(*state.last, event)) throw std::invalid_argument("event would change already-dispatched canonical prefix");
    if (state.dispatching && (event.time < state.dispatching->first ||
        (event.time == state.dispatching->first && causal_phase(event.kind) <= state.dispatching->second)))
        throw std::invalid_argument("dynamic event must follow complete dispatched time/phase group");
    if (state.pending.size() == state.capacity) throw std::invalid_argument("bounded pending event capacity exceeded");
    const auto source = state.sources.find(event.source_id);
    if (source == state.sources.end()) {
        if (state.sources.size() == kMaxEventSources) throw std::invalid_argument("scheduler source catalog capacity exceeded");
    } else if (event.event_id.sequence <= source->second.sequence || event.time < source->second.time ||
        (event.time == source->second.time && causal_phase(event.kind) < source->second.phase))
        throw std::invalid_argument("source-local enqueue sequence, time or causal phase regresses");
    state.pending.insert(event);
    const auto dispatched = source == state.sources.end() ? std::uint64_t{0} : source->second.dispatched;
    state.sources[event.source_id] = {event.event_id.sequence, event.time, causal_phase(event.kind), dispatched};
}
void Scheduler::attach_producer_barrier(std::shared_ptr<const CausalProducerBarrier> barrier) {
    auto& state = *impl_;
    if (state.sealed || state.barriers_locked) throw std::invalid_argument("causal producer barriers must attach before first advance");
    if (!barrier || state.barriers.size() == kMaxEventSources) throw std::invalid_argument("causal producer barrier is null or exceeds bounded capacity");
    for (const auto& existing : state.barriers) if (existing == barrier) throw std::invalid_argument("duplicate causal producer barrier attachment");
    barrier->committed(state.watermark); state.barriers.push_back(std::move(barrier));
}
void Scheduler::advance_watermark(SimulationInstant target, const Consumer& consume) {
    if (!consume) throw std::invalid_argument("scheduler requires an event consumer");
    advance_watermark_batches(target, [&](const std::vector<Event>& group) { for (const auto& event : group) consume(event); });
}
void Scheduler::advance_watermark_batches(SimulationInstant target, const PhaseConsumer& consume) {
    impl_->drain(target, consume);
}
void Scheduler::finish(const Consumer& consume) {
    if (!consume) throw std::invalid_argument("scheduler requires an event consumer");
    finish_batches([&](const std::vector<Event>& group) { for (const auto& event : group) consume(event); });
}
void Scheduler::finish_batches(const PhaseConsumer& consume) { impl_->drain(std::nullopt, consume); }
SimulationInstant Scheduler::watermark() const noexcept { return impl_->watermark; }
std::size_t Scheduler::pending_size() const noexcept { return impl_->pending.size(); }
std::optional<SimulationInstant> Scheduler::next_pending_time() const noexcept {
    return impl_->pending.empty() ? std::nullopt : std::optional<SimulationInstant>{impl_->pending.begin()->time};
}
bool Scheduler::has_dispatched(const EventId& event_id) const noexcept {
    const auto found = impl_->sources.find(event_id.source_id);
    return !impl_->failed && event_id.sequence != 0 && found != impl_->sources.end() && event_id.sequence <= found->second.dispatched;
}
std::optional<Event> Scheduler::accepted_event(const EventId& event_id) const {
    for (const auto& event : impl_->pending) if (event.event_id == event_id) return event;
    return impl_->validator.retained_event(event_id);
}
bool Scheduler::finished() const noexcept { return impl_->sealed; }

}  // namespace aismixer::lab::runtime
