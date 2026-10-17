#pragma once

#include "event.h"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <iosfwd>
#include <memory>
#include <string>
#include <vector>

namespace aismixer::lab::runtime {

struct SenderCounters {
    std::uint64_t records = 0;
    std::uint64_t bytes = 0;
    std::uint64_t errors = 0;
    std::uint64_t backpressure = 0;
};

// A future UDP adapter consumes this same typed, already-finalized record.
// Delivery must preserve sentence bytes/order; it must never invoke an encoder.
class OutputSink {
public:
    virtual ~OutputSink() = default;
    virtual void deliver(SimulationInstant time, const ReceiverNmea& record) = 0;
    virtual void flush() = 0;
};

// Exactly one receiver per raw stream: no prefixes, banners or origin mixing.
// Use binary mode on Windows translating streams. Counters measure accepted
// writes, not remote receipt or durable storage. Each complete logical record
// is flushed before deliver returns. An I/O error poisons the sink.
class StreamOutputSink final : public OutputSink {
public:
    StreamOutputSink(std::ostream& output, ReceiverId receiver);
    void deliver(SimulationInstant time, const ReceiverNmea& record) override;
    void flush() override;
    const SenderCounters& counters() const noexcept;
private:
    std::ostream& output_;
    ReceiverId receiver_;
    SenderCounters counters_;
    bool failed_ = false;
};

// Bounded streaming output to an exclusive same-directory temporary, followed
// by explicit no-replace publication after successful playback. Destruction
// removes owned temporary output best-effort; forced termination may leave it.
class FileOutputSink final : public OutputSink {
public:
    FileOutputSink(const std::filesystem::path& new_destination, ReceiverId receiver);
    ~FileOutputSink();
    FileOutputSink(const FileOutputSink&) = delete;
    FileOutputSink& operator=(const FileOutputSink&) = delete;
    void deliver(SimulationInstant time, const ReceiverNmea& record) override;
    void flush() override;
    void complete();
    const SenderCounters& counters() const noexcept;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

struct QueueLimits {
    std::size_t records_per_receiver = 64;
    std::size_t bytes_per_receiver = 64 * 1024;
};
struct QueueState {
    std::size_t records = 0;
    std::size_t bytes = 0;
    bool failed = false;
    SenderCounters counters;
};

// Independent queues, no hidden workers. Full queues explicitly signal
// backpressure; callers drain/wait before retrying. No record is discarded.
// A sink failure retains the front record and poisons only that receiver's
// queue, preventing unsafe automatic retry after a possibly partial write.
// Calls must be serialized by the owner; this is not a thread/UDP framework.
class ReceiverQueues {
public:
    explicit ReceiverQueues(const std::vector<ReceiverId>& receivers, QueueLimits limits = {});
    ~ReceiverQueues();
    ReceiverQueues(const ReceiverQueues&) = delete;
    ReceiverQueues& operator=(const ReceiverQueues&) = delete;
    [[nodiscard]] bool try_enqueue(SimulationInstant time, const ReceiverNmea& record);
    void enqueue_or_throw(SimulationInstant time, const ReceiverNmea& record);
    bool drain_one(const ReceiverId& receiver, OutputSink& sink);
    void flush(const ReceiverId& receiver, OutputSink& sink);
    QueueState state(const ReceiverId& receiver) const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

// Contract only: canonical dotted-decimal IPv4 literals and ports 1..65535.
// One receiver -> one endpoint, unique across receivers. No DNS/socket/send.
// IPv6/hostnames and intentional duplicate destinations need a future explicit
// policy; alternate textual IP spellings are rejected rather than aliased.
struct Endpoint { std::string address; std::uint32_t port = 0; };
class EndpointBindings {
public:
    explicit EndpointBindings(std::vector<ReceiverId> receivers);
    void bind(const ReceiverId& receiver, Endpoint endpoint);
    const Endpoint& endpoint(const ReceiverId& receiver) const;
private:
    std::vector<ReceiverId> receivers_;
    struct Binding { ReceiverId receiver; Endpoint endpoint; };
    std::vector<Binding> bindings_;
};

}  // namespace aismixer::lab::runtime
