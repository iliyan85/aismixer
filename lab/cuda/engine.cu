#include "engine_api.h"

#include <cuda_runtime.h>
#include <curand_kernel.h>

#include <cstdio>
#include <stdexcept>
#include <string>

namespace aismixer::lab {
namespace {

constexpr unsigned int kThreadsPerBlock = 256;

void check_cuda(cudaError_t status, const char* operation) {
    if (status != cudaSuccess) {
        throw std::runtime_error(std::string(operation) + ": " +
                                 cudaGetErrorName(status) + " (" +
                                 cudaGetErrorString(status) + ")");
    }
}

__global__ void generate_samples(std::uint64_t seed, std::uint32_t count,
                                 Sample* samples) {
    const std::uint32_t index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= count) {
        return;
    }

    // Each sample has its own subsequence: execution order cannot affect it.
    curandStatePhilox4_32_10_t state;
    curand_init(static_cast<unsigned long long>(seed),
                static_cast<unsigned long long>(index), 0, &state);
    samples[index] = Sample{index, curand(&state)};
}

cudaDeviceProp select_compatible_device() {
    int device_count = 0;
    check_cuda(cudaGetDeviceCount(&device_count), "cudaGetDeviceCount");
    if (device_count == 0) {
        throw std::runtime_error("No CUDA device is available");
    }
    check_cuda(cudaSetDevice(0), "cudaSetDevice(0)");

    cudaDeviceProp properties{};
    check_cuda(cudaGetDeviceProperties(&properties, 0),
               "cudaGetDeviceProperties(0)");

    // Loading the function detects missing/incompatible kernel images even for
    // --probe and empty batches, without pretending a kernel has executed.
    cudaFuncAttributes attributes{};
    check_cuda(cudaFuncGetAttributes(&attributes, generate_samples),
               "cudaFuncGetAttributes(generate_samples): verify compiled GPU architecture");
    if (attributes.maxThreadsPerBlock < static_cast<int>(kThreadsPerBlock) ||
        properties.maxThreadsPerBlock < static_cast<int>(kThreadsPerBlock)) {
        throw std::runtime_error("CUDA device 0 cannot run a 256-thread generation block");
    }
    return properties;
}

class DeviceSamples {
public:
    explicit DeviceSamples(std::size_t bytes) {
        check_cuda(cudaMalloc(reinterpret_cast<void**>(&data_), bytes),
                   "cudaMalloc(sample buffer)");
    }

    DeviceSamples(const DeviceSamples&) = delete;
    DeviceSamples& operator=(const DeviceSamples&) = delete;

    ~DeviceSamples() noexcept {
        if (data_ != nullptr) {
            // Preserve the original exception, but make a cleanup error visible.
            const cudaError_t status = cudaFree(data_);
            if (status != cudaSuccess) {
                std::fprintf(stderr, "CUDA cleanup failed: cudaFree(sample buffer): %s (%s)\n",
                             cudaGetErrorName(status), cudaGetErrorString(status));
            }
        }
    }

    Sample* data() const noexcept { return data_; }

    void release() {
        Sample* allocation = data_;
        data_ = nullptr;
        check_cuda(cudaFree(allocation), "cudaFree(sample buffer)");
    }

private:
    Sample* data_ = nullptr;
};

}  // namespace

DeviceInfo probe_device() {
    const cudaDeviceProp properties = select_compatible_device();
    int runtime_version = 0;
    int driver_version = 0;
    check_cuda(cudaRuntimeGetVersion(&runtime_version), "cudaRuntimeGetVersion");
    check_cuda(cudaDriverGetVersion(&driver_version), "cudaDriverGetVersion");

    std::size_t free_bytes = 0;
    std::size_t total_bytes = 0;
    check_cuda(cudaMemGetInfo(&free_bytes, &total_bytes), "cudaMemGetInfo");
    return DeviceInfo{properties.name, properties.major, properties.minor,
                      runtime_version, driver_version,
                      static_cast<std::uint64_t>(total_bytes),
                      static_cast<std::uint64_t>(free_bytes)};
}

std::vector<Sample> generate_batch(std::uint64_t seed, std::uint32_t count) {
    if (count > kMaxBatchSize) {
        throw std::invalid_argument("Sample count exceeds maximum batch size " +
                                    std::to_string(kMaxBatchSize));
    }
    select_compatible_device();
    if (count == 0) {
        return {};
    }

    std::vector<Sample> samples(count);
    const std::size_t bytes = samples.size() * sizeof(Sample);
    DeviceSamples device_samples(bytes);
    const unsigned int blocks = (count + kThreadsPerBlock - 1) / kThreadsPerBlock;
    generate_samples<<<blocks, kThreadsPerBlock>>>(seed, count, device_samples.data());
    check_cuda(cudaGetLastError(), "generate_samples launch");
    check_cuda(cudaDeviceSynchronize(), "generate_samples synchronization");
    check_cuda(cudaMemcpy(samples.data(), device_samples.data(), bytes,
                          cudaMemcpyDeviceToHost),
               "cudaMemcpy(sample buffer to host)");
    device_samples.release();
    return samples;
}

}  // namespace aismixer::lab
