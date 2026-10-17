#include "output.h"

#include <algorithm>
#include <cerrno>
#include <deque>
#include <limits>
#include <map>
#include <ostream>
#include <stdexcept>
#include <system_error>
#include <utility>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace aismixer::lab::runtime {
namespace {

std::uint64_t byte_count(const ReceiverNmea& record) {
    validate_receiver_nmea(record);
    std::uint64_t bytes = 0;
    for (const auto& sentence : record.sentences) bytes += sentence.size();
    return bytes;
}

void check_counter_capacity(const SenderCounters& counters, std::uint64_t bytes) {
    if (counters.records == std::numeric_limits<std::uint64_t>::max() ||
        bytes > std::numeric_limits<std::uint64_t>::max() - counters.bytes) {
        throw std::overflow_error("runtime sender counters exceed uint64 capacity");
    }
}

void check_receiver(const ReceiverId& expected, const ReceiverNmea& record) {
    if (!(expected == record.receiver_id)) {
        throw std::invalid_argument("raw output sink refuses receiver mixing");
    }
}

void validate_receivers(const std::vector<ReceiverId>& receivers) {
    if (receivers.empty() || receivers.size() > kMaxEventSources) {
        throw std::invalid_argument("output requires 1..1024 explicitly named receivers");
    }
    std::vector<std::string> names;
    for (const auto& receiver : receivers) {
        validate_identity(receiver.value, "output receiver");
        names.push_back(receiver.value);
    }
    std::sort(names.begin(), names.end());
    if (std::adjacent_find(names.begin(), names.end()) != names.end()) {
        throw std::invalid_argument("output receiver catalog contains duplicate identities");
    }
}

std::filesystem::path destination_path(const std::filesystem::path& requested) {
    if (requested.empty() || requested.filename().empty()) {
        throw std::invalid_argument("runtime output requires a nonempty new filename");
    }
    const auto target = std::filesystem::absolute(requested).lexically_normal();
    if (!std::filesystem::is_directory(target.parent_path())) {
        throw std::invalid_argument("runtime output parent directory does not exist: " + target.u8string());
    }
    if (std::filesystem::exists(std::filesystem::symlink_status(target))) {
        throw std::invalid_argument("runtime output destination already exists: " + target.u8string());
    }
    return target;
}

std::string native_error(unsigned error) {
    // Numeric OS errors remain ASCII/UTF-8 independently of the system locale.
    return "OS error " + std::to_string(error);
}

void validate_endpoint(const Endpoint& endpoint) {
    if (endpoint.port == 0 || endpoint.port > 65535 || endpoint.address.empty() ||
        endpoint.address.size() > 15) {
        throw std::invalid_argument("endpoint requires canonical IPv4 and port 1..65535");
    }
    std::size_t start = 0;
    for (unsigned part = 0; part < 4; ++part) {
        const auto end = endpoint.address.find('.', start);
        if ((part == 3) != (end == std::string::npos)) {
            throw std::invalid_argument("endpoint IPv4 requires four decimal components");
        }
        const auto count = (end == std::string::npos ? endpoint.address.size() : end) - start;
        if (count == 0 || count > 3 || (count > 1 && endpoint.address[start] == '0')) {
            throw std::invalid_argument("endpoint IPv4 components must use canonical decimal spelling");
        }
        unsigned value = 0;
        for (std::size_t index = 0; index < count; ++index) {
            const char digit = endpoint.address[start + index];
            if (digit < '0' || digit > '9') throw std::invalid_argument("endpoint IPv4 contains a nondecimal character");
            value = value * 10 + static_cast<unsigned>(digit - '0');
        }
        if (value > 255) throw std::invalid_argument("endpoint IPv4 component exceeds 255");
        start += count + 1;
    }
}

}  // namespace

StreamOutputSink::StreamOutputSink(std::ostream& output, ReceiverId receiver)
    : output_(output), receiver_(std::move(receiver)) {
    validate_identity(receiver_.value, "stream receiver");
}

void StreamOutputSink::deliver(SimulationInstant, const ReceiverNmea& record) {
    if (failed_) throw std::runtime_error("runtime stream sink is poisoned after an I/O failure");
    check_receiver(receiver_, record);
    const auto bytes = byte_count(record);
    check_counter_capacity(counters_, bytes);
    try {
        for (const auto& sentence : record.sentences) {
            output_.write(sentence.data(), static_cast<std::streamsize>(sentence.size()));
            if (!output_) throw std::runtime_error("runtime NMEA stdout/stream write failed");
        }
        // A complete logical message must become observable at its scheduled
        // delivery, before playback sleeps through a later lifecycle gap.
        output_.flush();
        if (!output_) throw std::runtime_error("runtime NMEA stdout/stream record flush failed");
    } catch (...) {
        failed_ = true;
        ++counters_.errors;
        throw;
    }
    ++counters_.records;
    counters_.bytes += bytes;
}

void StreamOutputSink::flush() {
    if (failed_) throw std::runtime_error("runtime stream sink is poisoned after an I/O failure");
    try {
        output_.flush();
        if (!output_) throw std::runtime_error("runtime NMEA stdout/stream flush failed");
    } catch (...) {
        failed_ = true;
        ++counters_.errors;
        throw;
    }
}
const SenderCounters& StreamOutputSink::counters() const noexcept { return counters_; }

struct FileOutputSink::Impl {
    Impl(const std::filesystem::path& requested, ReceiverId selected)
        : target(destination_path(requested)), receiver(std::move(selected)) {
        validate_identity(receiver.value, "file receiver");
        for (unsigned attempt = 0; attempt < 64; ++attempt) {
#ifdef _WIN32
            temporary = target.parent_path() / (target.filename().native() + L".r0-tmp-" +
                std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(attempt));
            handle = CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                                 FILE_ATTRIBUTE_NORMAL, nullptr);
            if (handle != INVALID_HANDLE_VALUE) return;
            const auto error = GetLastError();
            if (error != ERROR_FILE_EXISTS && error != ERROR_ALREADY_EXISTS) {
                throw std::runtime_error("cannot create exclusive runtime temporary: " + native_error(error));
            }
#else
            temporary = target.parent_path() / (target.filename().string() + ".r0-tmp-" +
                std::to_string(getpid()) + "-" + std::to_string(attempt));
            handle = ::open(temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
            if (handle >= 0) return;
            if (errno != EEXIST) throw std::runtime_error("cannot create exclusive runtime temporary: " + native_error(errno));
#endif
        }
        throw std::runtime_error("all bounded runtime temporary names are occupied");
    }
    ~Impl() noexcept {
#ifdef _WIN32
        if (handle != INVALID_HANDLE_VALUE) CloseHandle(handle);
#else
        if (handle >= 0) ::close(handle);
#endif
        if (!temporary.empty()) {
            std::error_code ignored;
            std::filesystem::remove(temporary, ignored);
        }
    }
    void ensure_open() const {
        if (failed || completed) throw std::runtime_error("runtime file sink is failed or already published");
    }
    void write(const std::string& sentence) {
        const char* data = sentence.data();
        std::size_t remaining = sentence.size();
        while (remaining) {
#ifdef _WIN32
            DWORD written = 0;
            if (!WriteFile(handle, data, static_cast<DWORD>(remaining), &written, nullptr) || written == 0) {
                throw std::runtime_error("runtime NMEA temporary write failed: " + native_error(GetLastError()));
            }
#else
            const auto written = ::write(handle, data, remaining);
            if (written < 0 && errno == EINTR) continue;
            if (written <= 0) throw std::runtime_error("runtime NMEA temporary write failed: " + native_error(errno));
#endif
            data += written;
            remaining -= static_cast<std::size_t>(written);
        }
    }
    void flush() {
        ensure_open();
#ifdef _WIN32
        if (!FlushFileBuffers(handle)) throw std::runtime_error("runtime NMEA temporary flush failed: " + native_error(GetLastError()));
#else
        if (::fsync(handle) != 0) throw std::runtime_error("runtime NMEA temporary flush failed: " + native_error(errno));
#endif
    }
    std::filesystem::path target, temporary;
    ReceiverId receiver;
    SenderCounters counters;
    bool failed = false, completed = false;
#ifdef _WIN32
    HANDLE handle = INVALID_HANDLE_VALUE;
#else
    int handle = -1;
#endif
};

FileOutputSink::FileOutputSink(const std::filesystem::path& destination, ReceiverId receiver)
    : impl_(std::make_unique<Impl>(destination, std::move(receiver))) {}
FileOutputSink::~FileOutputSink() = default;
void FileOutputSink::deliver(SimulationInstant, const ReceiverNmea& record) {
    impl_->ensure_open();
    try {
        check_receiver(impl_->receiver, record);
        const auto bytes = byte_count(record);
        check_counter_capacity(impl_->counters, bytes);
        for (const auto& sentence : record.sentences) impl_->write(sentence);
        ++impl_->counters.records;
        impl_->counters.bytes += bytes;
    } catch (...) {
        impl_->failed = true;
        ++impl_->counters.errors;
        throw;
    }
}
void FileOutputSink::flush() {
    try { impl_->flush(); }
    catch (...) { impl_->failed = true; ++impl_->counters.errors; throw; }
}
void FileOutputSink::complete() {
    impl_->ensure_open();
    try {
        impl_->flush();
#ifdef _WIN32
        if (!CloseHandle(impl_->handle)) throw std::runtime_error("runtime NMEA close failed: " + native_error(GetLastError()));
        impl_->handle = INVALID_HANDLE_VALUE;
        if (!MoveFileExW(impl_->temporary.c_str(), impl_->target.c_str(), MOVEFILE_WRITE_THROUGH)) {
            throw std::runtime_error("runtime NMEA no-replace publication failed: " + native_error(GetLastError()));
        }
        impl_->temporary.clear();
#else
        const auto closing = impl_->handle;
        impl_->handle = -1;
        if (::close(closing) != 0) throw std::runtime_error("runtime NMEA close failed: " + native_error(errno));
        if (::link(impl_->temporary.c_str(), impl_->target.c_str()) != 0) {
            throw std::runtime_error("runtime NMEA no-replace publication failed: " + native_error(errno));
        }
        std::error_code ignored;
        std::filesystem::remove(impl_->temporary, ignored);
        impl_->temporary.clear();
#endif
        impl_->completed = true;
    } catch (...) {
        impl_->failed = true;
        ++impl_->counters.errors;
        throw;
    }
}
const SenderCounters& FileOutputSink::counters() const noexcept { return impl_->counters; }

struct ReceiverQueues::Impl {
    struct Pending { SimulationInstant time; ReceiverNmea record; std::size_t bytes; };
    struct Queue {
        std::deque<Pending> pending;
        std::size_t bytes = 0;
        bool failed = false;
        std::optional<SimulationInstant> last_time;
        SenderCounters counters;
    };
    Impl(const std::vector<ReceiverId>& receivers, QueueLimits requested) : limits(requested) {
        validate_receivers(receivers);
        if (limits.records_per_receiver == 0 || limits.records_per_receiver > 4096 ||
            limits.bytes_per_receiver == 0 || limits.bytes_per_receiver > 4 * 1024 * 1024 ||
            limits.records_per_receiver > 65536 / receivers.size() ||
            limits.bytes_per_receiver > (64 * 1024 * 1024) / receivers.size()) {
            throw std::invalid_argument("receiver queue limits exceed bounded aggregate record/byte capacity");
        }
        for (const auto& receiver : receivers) queues.emplace(receiver.value, Queue{});
    }
    Queue& get(const ReceiverId& receiver) {
        auto found = queues.find(receiver.value);
        if (found == queues.end()) throw std::invalid_argument("unknown receiver queue: " + receiver.value);
        return found->second;
    }
    const Queue& get(const ReceiverId& receiver) const {
        const auto found = queues.find(receiver.value);
        if (found == queues.end()) throw std::invalid_argument("unknown receiver queue: " + receiver.value);
        return found->second;
    }
    QueueLimits limits;
    std::map<std::string, Queue> queues;
};

ReceiverQueues::ReceiverQueues(const std::vector<ReceiverId>& receivers, QueueLimits limits)
    : impl_(std::make_unique<Impl>(receivers, limits)) {}
ReceiverQueues::~ReceiverQueues() = default;
bool ReceiverQueues::try_enqueue(SimulationInstant time, const ReceiverNmea& record) {
    const auto bytes = static_cast<std::size_t>(byte_count(record));
    auto& queue = impl_->get(record.receiver_id);
    if (queue.failed) throw std::runtime_error("receiver queue is poisoned after sink failure");
    if (queue.last_time && time < *queue.last_time) throw std::invalid_argument("receiver queue would reorder simulation time");
    if (bytes > impl_->limits.bytes_per_receiver) throw std::invalid_argument("one output record exceeds receiver byte capacity");
    if (queue.pending.size() == impl_->limits.records_per_receiver ||
        bytes > impl_->limits.bytes_per_receiver - queue.bytes) {
        if (queue.counters.backpressure == std::numeric_limits<std::uint64_t>::max()) {
            throw std::overflow_error("receiver backpressure counter overflow");
        }
        ++queue.counters.backpressure;
        return false;
    }
    queue.pending.push_back({time, record, bytes});
    queue.bytes += bytes;
    queue.last_time = time;
    return true;
}
void ReceiverQueues::enqueue_or_throw(SimulationInstant time, const ReceiverNmea& record) {
    if (!try_enqueue(time, record)) throw std::runtime_error("receiver queue full: explicit backpressure; drain/wait before retry");
}
bool ReceiverQueues::drain_one(const ReceiverId& receiver, OutputSink& sink) {
    auto& queue = impl_->get(receiver);
    if (queue.failed) throw std::runtime_error("receiver queue is poisoned after sink failure");
    if (queue.pending.empty()) return false;
    const auto& front = queue.pending.front();
    check_counter_capacity(queue.counters, front.bytes);
    try { sink.deliver(front.time, front.record); }
    catch (...) { queue.failed = true; ++queue.counters.errors; throw; }
    ++queue.counters.records;
    queue.counters.bytes += front.bytes;
    queue.bytes -= front.bytes;
    queue.pending.pop_front();
    return true;
}
void ReceiverQueues::flush(const ReceiverId& receiver, OutputSink& sink) {
    auto& queue = impl_->get(receiver);
    if (queue.failed) throw std::runtime_error("receiver queue is poisoned after sink failure");
    try { sink.flush(); }
    catch (...) { queue.failed = true; ++queue.counters.errors; throw; }
}
QueueState ReceiverQueues::state(const ReceiverId& receiver) const {
    const auto& queue = impl_->get(receiver);
    return {queue.pending.size(), queue.bytes, queue.failed, queue.counters};
}

EndpointBindings::EndpointBindings(std::vector<ReceiverId> receivers) : receivers_(std::move(receivers)) {
    validate_receivers(receivers_);
}
void EndpointBindings::bind(const ReceiverId& receiver, Endpoint endpoint) {
    validate_identity(receiver.value, "endpoint receiver");
    if (std::none_of(receivers_.begin(), receivers_.end(), [&](const ReceiverId& id) { return id == receiver; })) {
        throw std::invalid_argument("endpoint binding names an unknown receiver");
    }
    validate_endpoint(endpoint);
    for (const auto& binding : bindings_) {
        if (binding.receiver == receiver) throw std::invalid_argument("receiver endpoint is already bound");
        if (binding.endpoint.address == endpoint.address && binding.endpoint.port == endpoint.port) {
            throw std::invalid_argument("endpoint is already assigned to another receiver");
        }
    }
    bindings_.push_back({receiver, std::move(endpoint)});
}
const Endpoint& EndpointBindings::endpoint(const ReceiverId& receiver) const {
    const auto found = std::find_if(bindings_.begin(), bindings_.end(), [&](const Binding& binding) {
        return binding.receiver == receiver;
    });
    if (found == bindings_.end()) throw std::invalid_argument("receiver has no endpoint binding");
    return found->endpoint;
}

}  // namespace aismixer::lab::runtime
