#include "engine_api.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

namespace {

using aismixer::lab::Sample;

struct Options {
    std::uint64_t seed = 42;
    std::uint32_t count = 1025;
    bool seed_set = false;
    bool count_set = false;
    bool probe = false;
    bool self_test = false;
    bool help = false;
};

void print_help() {
    std::cout
        << "Usage: aismixer_lab_gpu_smoke [--probe] [--seed UINT64] [--count COUNT]\n"
        << "       aismixer_lab_gpu_smoke [--probe] --self-test\n"
        << "       aismixer_lab_gpu_smoke --help\n\n"
        << "  --probe      Report CUDA device 0, API versions and memory.\n"
        << "               Probe alone does not generate samples.\n"
        << "  --seed       Decimal unsigned 64-bit seed (default: 42).\n"
        << "  --count      Sample count, 0.." << aismixer::lab::kMaxBatchSize
        << " (default: 1025).\n"
        << "  --self-test  Run deterministic generation and bounds checks on the GPU.\n"
        << "               Cannot be combined with --seed or --count.\n"
        << "  --help       Show this help without accessing CUDA.\n\n"
        << "With no options, generate the default batch on the GPU.\n"
        << "Digest: FNV-1a-64 over each ordered index and random value,\n"
        << "each encoded as four little-endian bytes (no struct padding).\n";
}

std::uint64_t parse_unsigned(std::string_view value, std::string_view option) {
    std::uint64_t result = 0;
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
    if (value.empty() || parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size()) {
        throw std::invalid_argument(std::string(option) + " requires a decimal unsigned 64-bit integer");
    }
    return result;
}

Options parse_options(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string_view argument(argv[i]);
        if (argument == "--help" || argument == "--probe" || argument == "--self-test") {
            bool& flag = argument == "--help" ? options.help
                : argument == "--probe" ? options.probe : options.self_test;
            if (flag) {
                throw std::invalid_argument("duplicate option: " + std::string(argument));
            }
            flag = true;
        } else if (argument == "--seed" || argument == "--count") {
            bool& flag = argument == "--seed" ? options.seed_set : options.count_set;
            if (flag) {
                throw std::invalid_argument("duplicate option: " + std::string(argument));
            }
            flag = true;
            if (++i == argc) {
                throw std::invalid_argument("missing value for " + std::string(argument));
            }
            const auto value = parse_unsigned(argv[i], argument);
            if (argument == "--seed") {
                options.seed = value;
            } else {
                if (value > aismixer::lab::kMaxBatchSize) {
                    throw std::invalid_argument("--count must be between 0 and "
                        + std::to_string(aismixer::lab::kMaxBatchSize));
                }
                options.count = static_cast<std::uint32_t>(value);
            }
        } else {
            throw std::invalid_argument("unknown option: " + std::string(argument));
        }
    }
    if (options.self_test && (options.seed_set || options.count_set)) {
        throw std::invalid_argument("--self-test cannot be combined with --seed or --count");
    }
    return options;
}

// FNV-1a-64: unsigned arithmetic wraps modulo 2^64. Serialize fields, never
// object memory, so the digest does not depend on host endianness or padding.
std::uint64_t digest(const std::vector<Sample>& samples) {
    std::uint64_t hash = UINT64_C(14695981039346656037);
    for (const auto& sample : samples) {
        for (const auto word : {sample.index, sample.random_value}) {
            for (unsigned shift = 0; shift < 32; shift += 8) {
                hash ^= (word >> shift) & UINT32_C(0xff);
                hash *= UINT64_C(1099511628211);
            }
        }
    }
    return hash;
}

void print_digest(const std::vector<Sample>& samples) {
    std::cout << "digest_fnv1a64=" << std::hex << std::setfill('0')
        << std::setw(16) << digest(samples) << std::dec << std::setfill(' ') << '\n';
}

std::string api_version(int version) {
    return std::to_string(version / 1000) + "." + std::to_string((version % 1000) / 10)
        + " (" + std::to_string(version) + ")";
}

void print_probe() {
    const auto device = aismixer::lab::probe_device();
    std::cout << "device=0\n"
        << "name=" << device.name << '\n'
        << "compute_capability=" << device.compute_major << '.' << device.compute_minor << '\n'
        << "cuda_runtime_api=" << api_version(device.runtime_version) << '\n'
        << "cuda_driver_api=" << api_version(device.driver_version) << '\n'
        << "gpu_memory_total_bytes=" << device.total_memory_bytes << '\n'
        << "gpu_memory_free_bytes=" << device.free_memory_bytes << '\n';
}

void require(bool condition, std::string_view message) {
    if (!condition) {
        throw std::runtime_error("self-test validation failed: " + std::string(message));
    }
}

void validate_order(const std::vector<Sample>& samples, std::uint32_t expected_count) {
    require(samples.size() == expected_count, "batch size differs from requested count");
    for (std::size_t i = 0; i < samples.size(); ++i) {
        require(samples[i].index == i, "sample indices are not in requested order");
    }
}

bool same_sample(const Sample& left, const Sample& right) {
    return left.index == right.index && left.random_value == right.random_value;
}

bool same_batch(const std::vector<Sample>& left, const std::vector<Sample>& right) {
    return left.size() == right.size()
        && std::equal(left.begin(), left.end(), right.begin(), same_sample);
}

bool values_differ(const std::vector<Sample>& left, const std::vector<Sample>& right) {
    require(left.size() == right.size(), "seed comparison batch sizes differ");
    for (std::size_t i = 0; i < left.size(); ++i) {
        if (left[i].random_value != right[i].random_value) {
            return true;
        }
    }
    return false;
}

void self_test() {
    // Probe also verifies driver/device availability and compiled-kernel support.
    print_probe();
    constexpr std::uint64_t seed = 42;
    const auto reference = aismixer::lab::generate_batch(seed, 1025);
    validate_order(reference, 1025);
    const std::array<std::uint32_t, 5> counts{0, 1, 257, 1025, aismixer::lab::kMaxBatchSize};
    for (const auto count : counts) {
        const auto first = aismixer::lab::generate_batch(seed, count);
        const auto second = aismixer::lab::generate_batch(seed, count);
        validate_order(first, count);
        validate_order(second, count);
        require(same_batch(first, second), "repeated seed/count changed samples");
        const auto prefix_size = std::min(first.size(), reference.size());
        require(std::equal(first.begin(), first.begin() + prefix_size,
            reference.begin(), same_sample), "per-index samples changed with batch size");
        std::cout << "PASS seed=" << seed << " count=" << count
            << " repeat/order/prefix ";
        print_digest(first);
    }

    const auto other_seed = aismixer::lab::generate_batch(seed + 1, 1025);
    validate_order(other_seed, 1025);
    require(values_differ(reference, other_seed), "different seed did not change random values");
    std::cout << "PASS different seed changes values\n";

    // Exercise both uint64 endpoints and prove the upper seed bits are used.
    const std::array<std::uint64_t, 3> extra_seeds{
        0, std::numeric_limits<std::uint64_t>::max(), seed + (UINT64_C(1) << 32)};
    for (const auto extra_seed : extra_seeds) {
        const auto first = aismixer::lab::generate_batch(extra_seed, 1025);
        const auto second = aismixer::lab::generate_batch(extra_seed, 1025);
        validate_order(first, 1025);
        validate_order(second, 1025);
        require(same_batch(first, second), "64-bit seed repeat changed samples");
        require(values_differ(reference, first), "64-bit seed did not change random values");
        std::cout << "PASS seed=" << extra_seed << " count=1025 repeat/order ";
        print_digest(first);
    }

    bool rejected = false;
    try {
        (void)aismixer::lab::generate_batch(seed, aismixer::lab::kMaxBatchSize + 1);
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    require(rejected, "engine accepted a batch above its maximum");
    std::cout << "PASS oversized batch rejected\nGPU self-test: PASS\n";
}

void generate(const Options& options) {
    const auto samples = aismixer::lab::generate_batch(options.seed, options.count);
    validate_order(samples, options.count);
    std::cout << "seed=" << options.seed << " count=" << options.count << '\n';
    const auto preview_count = std::min<std::size_t>(samples.size(), 8);
    std::cout << "preview_count=" << preview_count << '\n';
    for (std::size_t i = 0; i < preview_count; ++i) {
        std::cout << "index=" << samples[i].index << " random_value=" << samples[i].random_value << '\n';
    }
    print_digest(samples);
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const auto options = parse_options(argc, argv);
        if (options.help) {
            print_help();
            return 0;
        }
        if (options.self_test) {
            self_test();
            return 0;
        }
        if (options.probe) {
            print_probe();
            if (!options.seed_set && !options.count_set) {
                return 0;
            }
        }
        generate(options);
        return 0;
    } catch (const std::invalid_argument& error) {
        std::cerr << "Invalid input: " << error.what() << "\nUse --help for usage.\n";
        return 2;
    } catch (const std::exception& error) {
        std::cerr << "GPU smoke failed: " << error.what() << '\n';
        return 1;
    }
}
