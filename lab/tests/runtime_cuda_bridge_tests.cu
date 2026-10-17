// SYNTHETIC INFRASTRUCTURE TEST, NOT RF/AIS RADIO SIMULATION.
// This executable is test-only; no selection API is linked into the runtime.
#include "rf_contract.h"
#include <cuda_runtime.h>
#include <cub/device/device_select.cuh>
#include <algorithm>
#include <array>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace r = aismixer::lab::runtime;
namespace {
struct Candidate { r::CompactRfObservation observation; std::uint32_t synthetic_keep; };
static_assert(std::is_trivial_v<Candidate>);
void check(cudaError_t result, const char* operation) {
    if (result != cudaSuccess) throw std::runtime_error(std::string(operation)+": "+cudaGetErrorString(result));
}
template<class T> struct Buffer {
    T* data=nullptr;
    explicit Buffer(std::size_t count) { check(cudaMalloc(reinterpret_cast<void**>(&data),count*sizeof(T)),"test-only cudaMalloc"); }
    ~Buffer() { if(data) cudaFree(data); }
    Buffer(const Buffer&)=delete;
    Buffer& operator=(const Buffer&)=delete;
};
__global__ void synthetic_flags(const Candidate* input, r::CompactRfObservation* values,
                                unsigned char* flags, unsigned count) {
    const auto index=blockIdx.x*blockDim.x+threadIdx.x;
    if(index<count) {
        // Copies an explicitly supplied fixture predicate; no RF judgement.
        values[index]=input[index].observation;
        flags[index]=input[index].synthetic_keep ? 1 : 0;
    }
}
r::Event event(const r::CompactRfObservation& row) {
    r::Event e;
    e.time={row.availability_units}; e.kind=r::EventKind::RfObservation;
    e.source_id="rf:synthetic-fixture";
    e.subject_id="receiver:"+std::to_string(row.receiver.value);
    e.event_id={e.source_id,row.source_local_sequence};
    e.payload=r::RfObservation{{"tx:"+std::to_string(row.transmission.value)},
        {e.subject_id},e.time,false,std::nullopt,"SYNTHETIC TRANSFER TUPLE; NOT RF OBSERVATION"};
    r::validate_event(e);
    return e;
}
std::vector<r::CompactRfObservation> transfer(const std::vector<Candidate>& input,unsigned block) {
    const auto count=static_cast<unsigned>(input.size());
    Buffer<Candidate> source(count);
    Buffer<r::CompactRfObservation> values(count),selected(count);
    Buffer<unsigned char> flags(count);
    Buffer<int> selected_count(1);
    std::size_t scratch_bytes=0;
    check(cub::DeviceSelect::Flagged(nullptr,scratch_bytes,values.data,flags.data,selected.data,
                                    selected_count.data,static_cast<int>(count)),"synthetic CUB query");
    Buffer<unsigned char> scratch(scratch_bytes);
    check(cudaMemcpy(source.data,input.data(),input.size()*sizeof(Candidate),cudaMemcpyHostToDevice),"synthetic H2D");
    synthetic_flags<<<(count+block-1)/block,block>>>(source.data,values.data,flags.data,count);
    check(cudaGetLastError(),"synthetic device predicate launch");
    check(cub::DeviceSelect::Flagged(scratch.data,scratch_bytes,values.data,flags.data,selected.data,
                                    selected_count.data,static_cast<int>(count)),"synthetic stable select");
    int accepted=0;
    check(cudaMemcpy(&accepted,selected_count.data,sizeof(accepted),cudaMemcpyDeviceToHost),"synthetic count D2H");
    if(accepted<0 || accepted>static_cast<int>(count)) throw std::runtime_error("synthetic selection count invalid");
    std::vector<r::CompactRfObservation> compact(static_cast<std::size_t>(accepted));
    if(accepted) check(cudaMemcpy(compact.data(),selected.data,compact.size()*sizeof(compact[0]),cudaMemcpyDeviceToHost),"compact synthetic D2H");
    // CUB must preserve input order before the host's canonical chronology sort.
    auto expected=input.begin();
    for(const auto& row:compact) {
        expected=std::find_if(expected,input.end(),[](const Candidate& c){return c.synthetic_keep!=0;});
        if(expected==input.end() || row.source_local_sequence!=expected->observation.source_local_sequence ||
           row.transmission.value!=expected->observation.transmission.value ||
           row.receiver.value!=expected->observation.receiver.value || row.reception.value!=0 ||
           row.transmission_start_units!=expected->observation.transmission_start_units ||
           row.transmission_end_units!=expected->observation.transmission_end_units ||
           row.availability_units!=expected->observation.availability_units ||
           row.channel!=expected->observation.channel || row.schema_version!=1 ||
           row.disposition!=r::DecodeDisposition::Undecoded || row.reserved[0]!=0 || row.reserved[1]!=0)
            throw std::runtime_error("stable fixture selection changed identities");
        ++expected;
    }
    std::sort(compact.begin(),compact.end(),[](const auto& a,const auto& b){return r::CanonicalLess{}(event(a),event(b));});
    std::cout << "synthetic_bridge block=" << block << " H2D=" << input.size()*sizeof(Candidate)
              << " D2H_records=" << compact.size()*sizeof(compact[0]) << " D2H_count=" << sizeof(accepted)
              << " candidates=" << count << " selected=" << accepted << '\n';
    return compact;
}
}
int main() {
    try {
        int count=0; check(cudaGetDeviceCount(&count),"cudaGetDeviceCount");
        if(!count) throw std::runtime_error("CUDA bridge test requires an actual compatible GPU; no CPU fallback");
        check(cudaSetDevice(0),"cudaSetDevice"); cudaDeviceProp p{};
        check(cudaGetDeviceProperties(&p,0),"cudaGetDeviceProperties");
        cudaFuncAttributes attributes{}; check(cudaFuncGetAttributes(&attributes,synthetic_flags),"synthetic kernel architecture");
        if(attributes.maxThreadsPerBlock<256) throw std::runtime_error("incompatible synthetic kernel block capability");
        std::vector<Candidate> fixture;
        for(std::uint64_t identity=1; identity<=513; ++identity) {
            // Stable fixture semantic IDs survive all input permutations.
            const auto sequence=identity*7;
            r::CompactRfObservation row{{100000+identity},{700+(identity%3)},{0},
                sequence,static_cast<std::int64_t>(sequence*1000-500),
                static_cast<std::int64_t>(sequence*1000-100),static_cast<std::int64_t>(sequence*1000),
                1,static_cast<std::uint8_t>(identity%2),r::DecodeDisposition::Undecoded,{0,0}};
            fixture.push_back({row,static_cast<std::uint32_t>(identity%3!=0)});
        }
        std::reverse(fixture.begin(),fixture.end());
        const auto first=transfer(fixture,64);
        std::rotate(fixture.begin(),fixture.begin()+137,fixture.end());
        const auto second=transfer(fixture,256);
        if(first.size()!=342 || second.size()!=first.size()) throw std::runtime_error("independent synthetic accepted count mismatch");
        for(std::size_t i=0;i<first.size();++i) {
            const auto& a=first[i]; const auto& b=second[i];
            if(a.transmission.value!=b.transmission.value || a.receiver.value!=b.receiver.value ||
               a.reception.value!=0 || b.reception.value!=0 || a.availability_units!=b.availability_units ||
               a.transmission_start_units!=b.transmission_start_units ||
               a.transmission_end_units!=b.transmission_end_units ||
               a.source_local_sequence!=b.source_local_sequence || a.channel!=b.channel ||
               a.schema_version!=1 || a.disposition!=r::DecodeDisposition::Undecoded)
                throw std::runtime_error("launch geometry/input order changed canonical synthetic tuples");
        }
        std::cout << "PASS SYNTHETIC INFRASTRUCTURE TEST, NOT RF: actual " << p.name << " sm_" << p.major << p.minor
                  << "; real H2D/device/CUB/compact D2H, canonical host sorting, blocks64/256\n";
        return 0;
    } catch(const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
