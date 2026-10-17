#pragma once

#include "event.h"

#include <functional>
#include <limits>
#include <memory>

namespace aismixer::lab::runtime {

class CausalProducerBarrier {
public:
    virtual ~CausalProducerBarrier() = default;
    virtual void validate_advance(SimulationInstant watermark) const = 0;
    virtual void validate_finish() const = 0;
    virtual void committed(SimulationInstant) const {}
};

// This is a causal infrastructure queue, not a TDMA scheduler. The callback may
// enqueue genuinely generated later work; it must not recursively advance.
class Scheduler {
public:
    using Consumer = std::function<void(const Event&)>;
    using PhaseConsumer = std::function<void(const std::vector<Event>&)>;
    explicit Scheduler(std::size_t pending_capacity = 4096,
        SimulationInstant initial_watermark = {std::numeric_limits<std::int64_t>::min()},
        ValidationProfile profile = ValidationProfile::LegacyV1);
    ~Scheduler();
    Scheduler(Scheduler&&) noexcept;
    Scheduler& operator=(Scheduler&&) noexcept;
    Scheduler(const Scheduler&) = delete;
    Scheduler& operator=(const Scheduler&) = delete;
    void enqueue(Event event);
    // Attach stable producers before the first advance. Their bounded state
    // must prove all relevant results were materialized before commit.
    void attach_producer_barrier(std::shared_ptr<const CausalProducerBarrier> barrier);
    // Half open: on success all times < watermark are final, time == watermark
    // is still open. No future enqueue may alter the committed prefix.
    void advance_watermark(SimulationInstant watermark, const Consumer& consume);
    // Dynamic RF/station adapters use coherent same-time/same-phase groups.
    // In particular every simultaneous TX start is delivered in one group.
    // Generated work must have a strictly later time/phase than that group.
    // External causal producers must complete before declaring a watermark.
    void advance_watermark_batches(SimulationInstant watermark, const PhaseConsumer& consume);
    // Drains even INT64_MAX and seals; no artificial last-time + 1 sentinel.
    void finish(const Consumer& consume);
    void finish_batches(const PhaseConsumer& consume);
    SimulationInstant watermark() const noexcept;
    std::size_t pending_size() const noexcept;
    std::optional<SimulationInstant> next_pending_time() const noexcept;
    // Release-bound query for a trusted producer's known enqueued ID. This is
    // not an arbitrary historical-ID membership index (legacy gaps are legal).
    bool has_dispatched(const EventId& event_id) const noexcept;
    // Immutable snapshot lookup for host/RF binding. A pending accepted event
    // is not yet a causal publication; local routing still needs dispatch proof.
    std::optional<Event> accepted_event(const EventId& event_id) const;
    bool finished() const noexcept;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace aismixer::lab::runtime
