#include "cuda_engine.h"
#include "rng.h"

#include <cuda_runtime.h>
#include <algorithm>
#include <chrono>
#include <limits>
#include <stdexcept>
#include <utility>

namespace aismixer::lab::world {
namespace {
using Clock = std::chrono::steady_clock;
using Status = geodesy_detail::Status;
void checked(cudaError_t error, const char* operation) {
    if (error != cudaSuccess) throw std::runtime_error(std::string(operation)+": "+
        cudaGetErrorName(error)+" ("+cudaGetErrorString(error)+")");
}
struct Event {
    cudaEvent_t value = nullptr;
    Event() { checked(cudaEventCreate(&value),"cudaEventCreate(W1)"); }
    ~Event() { if (value) cudaEventDestroy(value); }
    Event(const Event&) = delete; Event& operator=(const Event&) = delete;
};
struct Timer {
    Event beginning, ending;
    void start() { checked(cudaEventRecord(beginning.value),"cudaEventRecord(W1 start)"); }
    void stop() { checked(cudaEventRecord(ending.value),"cudaEventRecord(W1 stop)"); }
    double elapsed() {
        checked(cudaEventSynchronize(ending.value),"cudaEventSynchronize(W1)");
        float elapsed_ms = 0;
        checked(cudaEventElapsedTime(&elapsed_ms,beginning.value,ending.value),"cudaEventElapsedTime(W1)");
        return elapsed_ms;
    }
};
template<class T> struct Buffer {
    T* pointer = nullptr;
    ~Buffer() { if (pointer) cudaFree(pointer); }
    void allocate(std::size_t count) {
        checked(cudaMalloc(reinterpret_cast<void**>(&pointer),count*sizeof(T)),"cudaMalloc(W1 persistent buffer)");
    }
    Buffer() = default; Buffer(const Buffer&) = delete; Buffer& operator=(const Buffer&) = delete;
};
struct DeviceRegion { RegionSpec spec; std::uint32_t first, end; };
struct Errors { unsigned count, first; };
static_assert(sizeof(DeviceRegion) == 64 && sizeof(Errors) == 8);
__device__ void record_error(Status status, unsigned index, std::uint32_t* statuses, Errors* errors) {
    statuses[index] = static_cast<std::uint32_t>(status);
    if (status != Status::Ok) { atomicAdd(&errors->count,1U); atomicMin(&errors->first,index); }
}
__global__ void evaluate_positions(const PositionQuery* queries, unsigned count,
                                  PositionResult* results, std::uint32_t* statuses, Errors* errors) {
    const unsigned index = blockIdx.x*blockDim.x+threadIdx.x;
    if (index >= count) return;
    const auto query = queries[index];
    GeoDirect direct;
    const auto status = geodesy_detail::direct(query.origin,query.initial_bearing_ndeg,query.distance_m,direct);
    if (status == Status::Ok) results[index] = {direct.location,direct.final_bearing_ndeg};
    record_error(status,index,statuses,errors);
}
__global__ void sample_candidates(const DeviceRegion* regions, unsigned region_count,
                                 unsigned count, std::uint64_t seed, RegionSample* results,
                                 std::uint32_t* statuses, Errors* errors) {
    const unsigned index = blockIdx.x*blockDim.x+threadIdx.x;
    if (index >= count) return;
    unsigned low = 0, high = region_count;
    while (low < high) {
        const unsigned middle = low+(high-low)/2;
        if (index >= regions[middle].end) low = middle+1; else high = middle;
    }
    const auto region = regions[low];
    const auto ordinal = index-region.first+1;
    const auto entity = static_cast<std::uint64_t>(region.spec.region_id);
    const double radial = rng_uniform(seed,entity,Purpose::RegionRadial,ordinal);
    const double bearing = 360.0*rng_uniform(seed,entity,Purpose::RegionBearing,ordinal);
    const double distance = static_cast<double>(region.spec.radius_mm)/1000.0*::sqrt(radial);
    GeoDirect direct;
    auto status = geodesy_detail::direct(region.spec.center,
        geodesy_detail::bearing_ndeg(bearing),distance,direct);
    std::uint64_t elevation_offset = 0;
    const auto width = static_cast<std::uint64_t>(region.spec.max_elevation_mm)-
                       static_cast<std::uint64_t>(region.spec.min_elevation_mm);
    if (status == Status::Ok && !rng_bounded(seed,entity,Purpose::RegionElevation,ordinal,0,width,elevation_offset))
        status = Status::RandomExhausted;
    if (status == Status::Ok) results[index] = {region.spec.region_id,ordinal,direct.location,
        region.spec.min_elevation_mm+static_cast<std::int64_t>(elevation_offset)};
    record_error(status,index,statuses,errors);
}
DeviceInfo select_device(unsigned block_size) {
    int count = 0;
    checked(cudaGetDeviceCount(&count),"cudaGetDeviceCount(W1)");
    if (count == 0) throw std::runtime_error("No CUDA device is available for W1 world evaluation");
    checked(cudaSetDevice(0),"cudaSetDevice(0) for W1");
    cudaDeviceProp properties{};
    checked(cudaGetDeviceProperties(&properties,0),"cudaGetDeviceProperties(W1)");
    if (properties.major < 6 || (properties.major == 6 && properties.minor < 1) ||
        properties.maxThreadsPerBlock < static_cast<int>(block_size))
        throw std::runtime_error("W1 requires a CUDA device capable of sm_61 and the selected block size");
    int runtime = 0, driver = 0;
    checked(cudaRuntimeGetVersion(&runtime),"cudaRuntimeGetVersion(W1)");
    checked(cudaDriverGetVersion(&driver),"cudaDriverGetVersion(W1)");
    return {properties.name,properties.major,properties.minor,runtime,driver};
}
void validate_query(const PositionQuery& query) {
    validate_point(query.origin);
    if (query.initial_bearing_ndeg < 0 || query.initial_bearing_ndeg >= 360*kDegree)
        throw std::invalid_argument("W1 position query bearing must be in [0,360) degrees");
    if (!geodesy_detail::finite(query.distance_m) || query.distance_m < 0 || query.distance_m > kGeodesicMaxDistanceM)
        throw std::invalid_argument("W1 position query distance must be finite in [0,2500000] metres");
}
}  // namespace

struct Session::Impl {
    Options options;
    DeviceInfo info;
    Metrics accumulated;
    Buffer<PositionQuery> queries;
    Buffer<PositionResult> positions;
    Buffer<DeviceRegion> regions;
    Buffer<RegionSample> samples;
    Buffer<std::uint32_t> statuses;
    Buffer<Errors> errors;
    std::unique_ptr<Timer> upload, position, sampling, diagnostics, download;
    explicit Impl(Options requested) : options(requested) {
        const auto beginning = Clock::now();
        if (options.capacity == 0 || options.capacity > kMaxCudaBatch ||
            (options.block_size != 64 && options.block_size != 256))
            throw std::invalid_argument("W1 CUDA capacity must be in 1..65536 and block size 64 or 256");
        info = select_device(options.block_size);
        const auto bytes = options.capacity*(sizeof(PositionQuery)+sizeof(PositionResult)+sizeof(DeviceRegion)+
                                             sizeof(RegionSample)+sizeof(std::uint32_t))+sizeof(Errors);
        std::size_t available = 0, total = 0;
        checked(cudaMemGetInfo(&available,&total),"cudaMemGetInfo(W1 allocation budget)");
        if (bytes > available) throw std::runtime_error("W1 persistent device buffers require "+
            std::to_string(bytes)+" bytes; free device bytes="+std::to_string(available));
        queries.allocate(options.capacity); positions.allocate(options.capacity); regions.allocate(options.capacity);
        samples.allocate(options.capacity); statuses.allocate(options.capacity); errors.allocate(1);
        upload=std::make_unique<Timer>(); position=std::make_unique<Timer>(); sampling=std::make_unique<Timer>();
        diagnostics=std::make_unique<Timer>(); download=std::make_unique<Timer>();
        accumulated.device_bytes = bytes;
        accumulated.setup_wall_ms = std::chrono::duration<double,std::milli>(Clock::now()-beginning).count();
    }
    void begin() {
        checked(cudaSetDevice(0),"cudaSetDevice(0) for W1 session");
        const Errors zero{0,std::numeric_limits<unsigned>::max()};
        checked(cudaMemcpy(errors.pointer,&zero,sizeof(zero),cudaMemcpyHostToDevice),"cudaMemcpy(W1 error reset H2D)");
        accumulated.h2d_bytes += sizeof(zero);
    }
    void validate_device_result() {
        diagnostics->start();
        Errors result{};
        checked(cudaMemcpy(&result,errors.pointer,sizeof(result),cudaMemcpyDeviceToHost),"cudaMemcpy(W1 small diagnostics D2H)");
        diagnostics->stop();
        accumulated.d2h_ms += diagnostics->elapsed();
        accumulated.diagnostic_bytes += sizeof(result);
        if (result.count) {
            if (result.count > options.capacity || result.first >= options.capacity)
                throw std::runtime_error("W1 CUDA diagnostic counter/index is inconsistent with capacity");
            std::uint32_t code = 0;
            checked(cudaMemcpy(&code,statuses.pointer+result.first,sizeof(code),cudaMemcpyDeviceToHost),"cudaMemcpy(W1 first failure D2H)");
            accumulated.diagnostic_bytes += sizeof(code);
            throw std::runtime_error("W1 CUDA geodesic/sample failure at row "+std::to_string(result.first)+
                ", failed rows="+std::to_string(result.count)+": "+geodesy_detail::status_name(static_cast<Status>(code)));
        }
    }
};

Session::Session(Options options) : impl_(std::make_unique<Impl>(options)) {}
Session::~Session() = default;
Session::Session(Session&&) noexcept = default;
Session& Session::operator=(Session&&) noexcept = default;
std::vector<PositionResult> Session::evaluate(const std::vector<PositionQuery>& input) {
    auto& state = *impl_;
    const auto beginning = Clock::now();
    if (input.size() > state.options.capacity) throw std::invalid_argument("W1 position batch exceeds persistent capacity");
    for (const auto& query : input) validate_query(query);
    checked(cudaSetDevice(0),"cudaSetDevice(0) for W1 evaluate");
    if (input.empty()) return {};
    std::vector<PositionResult> result(input.size());
    state.upload->start(); state.begin();
    checked(cudaMemcpy(state.queries.pointer,input.data(),input.size()*sizeof(PositionQuery),cudaMemcpyHostToDevice),
            "cudaMemcpy(W1 immutable position queries H2D)");
    state.upload->stop();
    const auto count = static_cast<unsigned>(input.size());
    const auto blocks = (count+state.options.block_size-1)/state.options.block_size;
    state.position->start();
    evaluate_positions<<<blocks,state.options.block_size>>>(state.queries.pointer,count,state.positions.pointer,state.statuses.pointer,state.errors.pointer);
    checked(cudaGetLastError(),"evaluate_positions(W1) launch"); state.position->stop();
    state.validate_device_result();
    state.download->start();
    checked(cudaMemcpy(result.data(),state.positions.pointer,result.size()*sizeof(PositionResult),cudaMemcpyDeviceToHost),
            "cudaMemcpy(W1 world positions D2H)");
    state.download->stop();
    state.accumulated.h2d_ms += state.upload->elapsed(); state.accumulated.position_ms += state.position->elapsed();
    state.accumulated.d2h_ms += state.download->elapsed();
    state.accumulated.h2d_bytes += input.size()*sizeof(PositionQuery);
    state.accumulated.d2h_bytes += result.size()*sizeof(PositionResult);
    state.accumulated.position_queries += input.size(); ++state.accumulated.launches;
    state.accumulated.peak_batch = std::max(state.accumulated.peak_batch,input.size());
    state.accumulated.wall_ms += std::chrono::duration<double,std::milli>(Clock::now()-beginning).count();
    return result;
}
std::vector<RegionSample> Session::sample_regions(std::uint64_t seed, const std::vector<RegionSpec>& input) {
    auto& state = *impl_;
    const auto beginning = Clock::now();
    if (input.size() > state.options.capacity) throw std::invalid_argument("W1 region catalog exceeds persistent capacity");
    auto sorted = input;
    std::sort(sorted.begin(),sorted.end(),[](const auto& a,const auto& b) { return a.region_id < b.region_id; });
    std::vector<DeviceRegion> upload; upload.reserve(sorted.size());
    std::size_t total = 0;
    std::uint32_t previous = 0;
    for (const auto& region : sorted) {
        validate_point(region.center);
        const auto width = static_cast<std::uint64_t>(region.max_elevation_mm)-static_cast<std::uint64_t>(region.min_elevation_mm);
        if (region.region_id == 0 || region.region_id == previous || region.count == 0 || region.radius_mm < 0 ||
            region.radius_mm > static_cast<std::int64_t>(kGeodesicMaxDistanceM*1000) ||
            region.min_elevation_mm > region.max_elevation_mm || width > static_cast<std::uint64_t>(INT64_MAX) ||
            region.count > state.options.capacity-total)
            throw std::invalid_argument("W1 region identity/count/radius/elevation interval/capacity is invalid");
        const auto first = total; total += region.count;
        upload.push_back({region,static_cast<std::uint32_t>(first),static_cast<std::uint32_t>(total)});
        previous = region.region_id;
    }
    checked(cudaSetDevice(0),"cudaSetDevice(0) for W1 sampling");
    if (total == 0) return {};
    std::vector<RegionSample> result(total);
    state.upload->start(); state.begin();
    checked(cudaMemcpy(state.regions.pointer,upload.data(),upload.size()*sizeof(DeviceRegion),cudaMemcpyHostToDevice),
            "cudaMemcpy(W1 resident region descriptors H2D)");
    state.upload->stop();
    const auto count = static_cast<unsigned>(total);
    const auto blocks = (count+state.options.block_size-1)/state.options.block_size;
    state.sampling->start();
    sample_candidates<<<blocks,state.options.block_size>>>(state.regions.pointer,static_cast<unsigned>(upload.size()),count,seed,
        state.samples.pointer,state.statuses.pointer,state.errors.pointer);
    checked(cudaGetLastError(),"sample_candidates(W1) launch"); state.sampling->stop();
    state.validate_device_result();
    state.download->start();
    checked(cudaMemcpy(result.data(),state.samples.pointer,result.size()*sizeof(RegionSample),cudaMemcpyDeviceToHost),
            "cudaMemcpy(W1 fixed candidate receiver positions D2H)");
    state.download->stop();
    state.accumulated.h2d_ms += state.upload->elapsed(); state.accumulated.sampling_ms += state.sampling->elapsed();
    state.accumulated.d2h_ms += state.download->elapsed();
    state.accumulated.h2d_bytes += upload.size()*sizeof(DeviceRegion);
    state.accumulated.d2h_bytes += result.size()*sizeof(RegionSample);
    state.accumulated.sampled_receivers += total; ++state.accumulated.launches;
    state.accumulated.peak_batch = std::max(state.accumulated.peak_batch,total);
    state.accumulated.wall_ms += std::chrono::duration<double,std::milli>(Clock::now()-beginning).count();
    return result;
}
const Metrics& Session::metrics() const { return impl_->accumulated; }
const DeviceInfo& Session::device_info() const { return impl_->info; }
}  // namespace aismixer::lab::world
