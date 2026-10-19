#include "config.h"
#include "world.h"
#include "rng.h"
#include <algorithm>
#include <charconv>
#include <chrono>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#define NOMINMAX
#include <Windows.h>
#include <psapi.h>
#include <fcntl.h>
#include <io.h>

namespace {
namespace w=aismixer::lab::world;
namespace r=aismixer::lab::runtime;
using Clock=std::chrono::steady_clock;
constexpr const char* kBanner="WORLD STATE / LIFECYCLE ONLY -- NO AIS TDMA OR RF RECEPTION SIMULATED";
std::string quoted(const std::string& value) {
    std::string result="\"";
    for(unsigned char c:value) {
        if(c=='"'||c=='\\'){result+='\\';result+=static_cast<char>(c);}
        else if(c<32){const char* hex="0123456789abcdef";result+="\\u00";result+=hex[c>>4];result+=hex[c&15];}
        else result+=static_cast<char>(c);
    }
    return result+'"';
}
std::string hex64(std::uint64_t value){std::ostringstream out;out<<std::hex<<std::setfill('0')<<std::setw(16)<<value;return out.str();}
std::uint64_t number(const std::string& value,std::uint64_t low,std::uint64_t high,const std::string& name) {
    std::uint64_t result=0; const auto parsed=std::from_chars(value.data(),value.data()+value.size(),result);
    if(value.empty() || (value.size()>1&&value.front()=='0') || parsed.ec!=std::errc{} || parsed.ptr!=value.data()+value.size() || result<low || result>high)
        throw std::invalid_argument(name+" requires a canonical decimal integer in range");
    return result;
}
struct Arguments {
    bool help=false,validate=false,verbose=false;
    std::filesystem::path world;
    std::optional<std::filesystem::path> output;
    std::optional<std::string> seed;
    std::optional<std::int64_t> duration;
    std::int64_t step=60,chunk=300;
    std::size_t batch=1024;
    unsigned block=256;
};
Arguments arguments(const std::vector<std::string>& args) {
    Arguments out; std::set<std::string> seen;
    for(std::size_t i=1;i<args.size();++i) {
        const auto key=args[i]; if(!seen.insert(key).second)throw std::invalid_argument("duplicate W1 option: "+key);
        const auto value=[&](){if(++i>=args.size()||args[i].empty()||args[i].rfind("--",0)==0)throw std::invalid_argument("missing W1 option value: "+key);return args[i];};
        if(key=="--help")out.help=true;
        else if(key=="--validate")out.validate=true;
        else if(key=="--verbose")out.verbose=true;
        else if(key=="--world")out.world=std::filesystem::u8path(value());
        else if(key=="--output")out.output=std::filesystem::u8path(value());
        else if(key=="--seed")out.seed=value();
        else if(key=="--duration-seconds")out.duration=static_cast<std::int64_t>(number(value(),1,w::kMaxScenarioSeconds,key));
        else if(key=="--step-seconds")out.step=static_cast<std::int64_t>(number(value(),1,w::kMaxScenarioSeconds,key));
        else if(key=="--chunk-seconds")out.chunk=static_cast<std::int64_t>(number(value(),1,w::kMaxScenarioSeconds,key));
        else if(key=="--batch-size")out.batch=static_cast<std::size_t>(number(value(),1,w::kMaxCudaBatch,key));
        else if(key=="--block-size"){out.block=static_cast<unsigned>(number(value(),64,256,key));if(out.block!=64&&out.block!=256)throw std::invalid_argument("W1 block-size must be64 or256");}
        else throw std::invalid_argument("unknown W1 option: "+key);
    }
    if(out.help){if(seen.size()!=1)throw std::invalid_argument("--help must be used alone");return out;}
    if(out.world.empty())throw std::invalid_argument("W1 requires --world PATH");
    if(out.validate&&(out.output||out.verbose||seen.count("--batch-size")||seen.count("--block-size")||seen.count("--chunk-seconds")||seen.count("--step-seconds")))
        throw std::invalid_argument("--validate accepts only --world and optional --seed/--duration-seconds");
    return out;
}
class Output {
public:
    explicit Output(const std::optional<std::filesystem::path>& destination) {
        if(!destination)return;
        target_=std::filesystem::absolute(*destination).lexically_normal();
        if(target_.filename().empty()||!std::filesystem::is_directory(target_.parent_path()))throw std::invalid_argument("W1 output requires an existing parent directory");
        if(std::filesystem::exists(std::filesystem::symlink_status(target_)))throw std::invalid_argument("W1 output destination already exists");
        for(unsigned attempt=0;attempt<64;++attempt){
            temporary_=target_.parent_path()/(target_.filename().native()+L".w1-tmp-"+std::to_wstring(GetCurrentProcessId())+L"-"+std::to_wstring(attempt));
            handle_=CreateFileW(temporary_.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
            if(handle_!=INVALID_HANDLE_VALUE)return;
            if(GetLastError()!=ERROR_FILE_EXISTS&&GetLastError()!=ERROR_ALREADY_EXISTS)throw std::runtime_error("W1 cannot create exclusive temporary output");
        }
        temporary_.clear();throw std::runtime_error("W1 output temporary namespace exhausted");
    }
    ~Output(){if(handle_!=INVALID_HANDLE_VALUE)CloseHandle(handle_);if(!temporary_.empty()){std::error_code ignored;std::filesystem::remove(temporary_,ignored);}}
    void write(const std::string& text) {
        if(handle_==INVALID_HANDLE_VALUE){std::cout.write(text.data(),static_cast<std::streamsize>(text.size()));if(!std::cout)throw std::runtime_error("W1 stdout write failed");return;}
        std::size_t position=0;
        while(position<text.size()) {DWORD written=0;const auto count=static_cast<DWORD>(std::min<std::size_t>(text.size()-position,1048576));
            if(!WriteFile(handle_,text.data()+position,count,&written,nullptr)||written==0)throw std::runtime_error("W1 trace file write failed");position+=written;}
    }
    void finish(){
        if(handle_==INVALID_HANDLE_VALUE){std::cout.flush();if(!std::cout)throw std::runtime_error("W1 stdout flush failed");return;}
        if(!FlushFileBuffers(handle_))throw std::runtime_error("W1 trace flush failed");
        if(!CloseHandle(handle_))throw std::runtime_error("W1 trace close failed");handle_=INVALID_HANDLE_VALUE;
        if(!MoveFileExW(temporary_.c_str(),target_.c_str(),MOVEFILE_WRITE_THROUGH))throw std::runtime_error("W1 trace no-replace publication failed");
        temporary_.clear();
    }
private:
    std::filesystem::path target_,temporary_;HANDLE handle_=INVALID_HANDLE_VALUE;
};
class Trace {
public:
    Trace(Output& output,w::Session& cuda,std::size_t batch):output_(output),cuda_(cuda),batch_(batch){}
    void line(const std::string& value){
        const auto bytes=value+'\n';output_.write(bytes);
        for(unsigned char c:bytes){digest_^=c;digest_*=UINT64_C(1099511628211);}++records_;
    }
    void enqueue(std::string row){pending_.push_back({std::move(row),{},false,false});bound();}
    void transition(const w::Transition& event){
        std::string kind=w::transition_name(event.kind);
        if(event.kind==w::TransitionKind::InitialOff)kind=event.entity_id<=UINT32_MAX?"initial_network_entry_preparation":"initial_receiver_power_delay";
        std::ostringstream row;row<<"{\"type\":\"transition\",\"time_units\":"<<event.time.units<<",\"kind\":"<<quoted(kind)
            <<",\"entity_id\":"<<event.entity_id<<",\"episode\":"<<event.episode<<",\"value\":"<<event.value
            <<",\"protocol_initialization_required\":"<<(event.requires_protocol_initialization?"true":"false")
            <<",\"abort_if_overlapping_burst\":"<<(event.requires_abort_of_overlapping_burst?"true":"false")<<'}';enqueue(row.str());
    }
    void vessel(r::SimulationInstant time,const w::VesselState& view){
        std::ostringstream before,after;
        before<<"{\"type\":\"vessel\",\"time_units\":"<<time.units<<",\"vessel_id\":"<<view.id.value
            <<",\"equipment_id\":"<<view.equipment_id.value<<",\"radio_id\":"<<view.radio_id.value<<',';
        after<<",\"nominal_speed_mkn\":"<<view.nominal_speed_mkn<<",\"sog_mkn\":"<<view.speed_mkn
            <<",\"navigation_status\":"<<static_cast<unsigned>(view.navigation_status)<<",\"equipment_on\":"<<(view.equipment_on?"true":"false")
            <<",\"tx_enabled\":"<<(view.equipment_on?"true":"false")<<",\"rx_enabled\":"<<(view.equipment_on?"true":"false")
            <<",\"power_generation\":"<<view.power_generation<<",\"leg_from\":"<<view.leg_from.value<<",\"destination\":"<<view.destination.value
            <<",\"leg_index\":"<<view.leg_index<<",\"leg_length_mm\":"<<view.leg_length_mm<<",\"leg_distance_mm\":"<<view.leg_distance_mm
            <<",\"distance_remainder\":"<<view.distance_remainder<<",\"distance_denominator\":"<<w::kDistanceDenominator
            <<",\"total_distance_mm\":"<<view.total_distance_mm<<",\"total_distance_remainder\":"<<view.total_distance_remainder
            <<",\"antenna_offset_mm\":"<<view.antenna_offset_mm<<",\"antenna_ellipsoid_mm\":";
        if(view.antenna_ellipsoid_mm)after<<*view.antenna_ellipsoid_mm;else after<<"null";
        after<<",\"effective_reach_radius_mm\":"<<view.effective_reach_radius_mm<<",\"environment_revision\":"<<view.environment_revision
            <<",\"route_qualification\":\"unqualified illustrative geodesic; no shoreline mask\"}";
        pending_.push_back({before.str(),after.str(),true,view.course_valid});queries_.push_back(view.position_query);bound();
    }
    void receiver(r::SimulationInstant time,const w::PassiveState& view){
        std::ostringstream row;row<<"{\"type\":\"receiver\",\"time_units\":"<<time.units<<",\"region_id\":"<<view.region_id.value
            <<",\"ordinal\":"<<view.ordinal<<",\"equipment_key\":"<<w::passive_equipment_key(view.region_id,view.ordinal)
            <<",\"longitude_ndeg\":"<<view.location.longitude_ndeg<<",\"latitude_ndeg\":"<<view.location.latitude_ndeg
            <<",\"antenna_elevation_mm\":"<<view.elevation_mm<<",\"height_datum\":"<<quoted(view.height_datum==w::HeightDatum::Amsl?"AMSL":"WGS84_ellipsoid")
            <<",\"rx_enabled\":"<<(view.equipment_on?"true":"false")<<",\"tx_enabled\":false,\"power_generation\":"<<view.power_generation
            <<",\"site_qualification\":\"unqualified synthetic candidate\",\"provenance\":"<<quoted(view.provenance)<<'}';enqueue(row.str());
    }
    void flush(){
        const auto values=queries_.empty()?std::vector<w::PositionResult>{}:cuda_.evaluate(queries_);std::size_t index=0;
        for(const auto& row:pending_){
            if(!row.position){line(row.before);continue;}
            const auto& value=values.at(index++);std::ostringstream point;
            point<<"\"longitude_ndeg\":"<<value.location.longitude_ndeg<<",\"latitude_ndeg\":"<<value.location.latitude_ndeg<<",\"cog_ndeg\":";
            if(row.course_valid)point<<value.final_bearing_ndeg;else point<<"null";
            line(row.before+point.str()+row.after);
        }
        peak_rows_=std::max(peak_rows_,pending_.size());pending_.clear();queries_.clear();
    }
    void complete(){flush();output_.write("{\"type\":\"complete\",\"status\":\"COMPLETE_WORLD_ONLY\",\"records\":"+std::to_string(records_)+",\"logical_fnv1a64\":\""+hex64(digest_)+"\"}\n");output_.finish();}
    std::uint64_t digest()const{return digest_;}std::uint64_t records()const{return records_;}std::size_t peak_rows()const{return peak_rows_;}
private:
    struct Row{std::string before,after;bool position,course_valid;};
    void bound(){if(queries_.size()>=batch_||pending_.size()>=8192)flush();}
    Output& output_;w::Session& cuda_;std::size_t batch_,peak_rows_=0;
    std::vector<Row> pending_;std::vector<w::PositionQuery> queries_;
    std::uint64_t digest_=UINT64_C(14695981039346656037),records_=0;
};
int run(const std::vector<std::string>& args){
    const auto options=arguments(args);
    if(options.help){std::cout<<"AISMixer Lab W1 -- "<<kBanner<<"\n--world PATH [--validate] [--seed UINT64|fresh] [--duration-seconds N]\n"
        <<"  [--step-seconds N] [--chunk-seconds N] [--batch-size 1..65536] [--block-size 64|256]\n  [--output NEW_NDJSON] [--verbose]\n"
        <<"--validate is CUDA-free. Production world evaluation requires CUDA; no fallback.\n";std::cout.flush();if(!std::cout)throw std::runtime_error("W1 stdout help failed");return 0;}
    std::cerr<<kBanner<<'\n';auto config=w::load_world(options.world);
    if(options.seed){config.fresh_seed=*options.seed=="fresh";if(!config.fresh_seed)config.effective_seed=number(*options.seed,0,UINT64_MAX,"--seed");}
    if(options.duration)config.duration=r::from_seconds(*options.duration);
    config=w::resolve_seed(std::move(config));
    if(options.validate){std::cout<<"{\"status\":\"VALID_WORLD_CONFIGURATION\",\"effective_seed\":"<<config.effective_seed<<",\"vessels\":"<<config.vessels.size()<<",\"regions\":"<<config.regions.size()<<"}\n";std::cout.flush();if(!std::cout)throw std::runtime_error("W1 validation stdout failed");return 0;}
    const auto started=Clock::now();w::Session cuda({w::kMaxCudaBatch,options.block});
    auto samples=cuda.sample_regions(config.effective_seed,w::region_specs(config));w::Timeline timeline(config,std::move(samples));
    const auto step=r::from_seconds(options.step).units,chunk=r::from_seconds(options.chunk).units;
    const auto frames=static_cast<std::uint64_t>(config.duration.units/step)+2;
    const auto population=timeline.metrics().vessels+timeline.metrics().receivers;
    if(frames>UINT64_C(5000000)/(population?population:1))throw std::invalid_argument("W1 output sampling exceeds five million snapshot rows; increase step-seconds");
    Output output(options.output);Trace trace(output,cuda,options.batch);
    std::ostringstream manifest;manifest<<"{\"type\":\"manifest\",\"trace_schema\":1,\"world_schema\":1,\"rng_version\":"<<w::kRngVersion
        <<",\"geodesic_version\":"<<quoted(w::kGeodesicVersion)<<",\"model_version\":1,\"effective_seed\":"<<config.effective_seed
        <<",\"duration_units\":"<<config.duration.units<<",\"step_units\":"<<step<<",\"time_units_per_second\":"<<r::kUnitsPerSecond
        <<",\"scenario_fnv1a64\":"<<quoted(hex64(config.scenario_hash))<<",\"waypoint_fnv1a64\":"<<quoted(hex64(config.waypoint_hash))
        <<",\"regions_fnv1a64\":"<<quoted(hex64(config.regions_hash))<<",\"profile_id\":"<<quoted(config.profile_id)
        <<",\"provenance\":"<<quoted(config.provenance)<<",\"scope\":"<<quoted(kBanner)<<'}';trace.line(manifest.str());
    const auto sink=[&](const w::Transition& event){trace.transition(event);};
    r::SimulationInstant sample{0},current{0};
    for(;;){
        while(current<sample){const auto distance=sample.units-current.units;current=r::add_units(current,std::min(chunk,distance));timeline.advance(current,sink);}
        if(sample.units==0)timeline.advance(sample,sink);
        const auto frame=timeline.prepare();for(const auto& vessel:frame.vessels)trace.vessel(sample,vessel);for(const auto& receiver:frame.receivers)trace.receiver(sample,receiver);
        if(sample==config.duration)break;sample={std::min(config.duration.units,sample.units+step)};
    }
    trace.complete();const auto& metric=cuda.metrics();const auto& host=timeline.metrics();const auto& device=cuda.device_info();
    PROCESS_MEMORY_COUNTERS_EX memory{};memory.cb=sizeof(memory);
    const bool memory_available=K32GetProcessMemoryInfo(GetCurrentProcess(),reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&memory),sizeof(memory))!=0;
    std::cerr<<"status=COMPLETE_WORLD_ONLY seed="<<config.effective_seed<<" digest="<<hex64(trace.digest())<<" records="<<trace.records()
        <<"\ngpu="<<device.name<<" sm="<<device.major<<'.'<<device.minor<<" runtime="<<device.runtime_version<<" driver="<<device.driver_version
        <<" build="<<AISMIXER_LAB_WORLD_BUILD_INFO<<"\nsimulation_units="<<config.duration.units<<" vessels="<<host.vessels<<" receivers="<<host.receivers
        <<" transitions="<<host.transitions<<" integration_intervals="<<host.integration_intervals<<" owned_host_state_bytes="<<host.owned_state_bytes
        <<" peak_notices="<<host.peak_pending_notices<<" peak_trace_rows="<<trace.peak_rows()<<"\nposition_queries="<<metric.position_queries
        <<" sampled_receivers="<<metric.sampled_receivers<<" launches="<<metric.launches<<" peak_batch="<<metric.peak_batch
        <<" device_owned_bytes="<<metric.device_bytes<<" H2D_bytes="<<metric.h2d_bytes<<" D2H_bytes="<<metric.d2h_bytes<<" diagnostic_bytes="<<metric.diagnostic_bytes
        <<std::fixed<<std::setprecision(4)<<"\nsetup_ms="<<metric.setup_wall_ms<<" h2d_ms="<<metric.h2d_ms<<" position_kernel_ms="<<metric.position_ms
        <<" sampling_kernel_ms="<<metric.sampling_ms<<" d2h_ms="<<metric.d2h_ms<<" cuda_calls_wall_ms="<<metric.wall_ms
        <<" total_wall_ms="<<std::chrono::duration<double,std::milli>(Clock::now()-started).count()<<'\n';
    if(memory_available)std::cerr<<"process_peak_working_set_bytes="<<memory.PeakWorkingSetSize<<" process_peak_pagefile_bytes="<<memory.PeakPagefileUsage<<'\n';
    else std::cerr<<"process_peak_memory=UNAVAILABLE\n";
    if(options.verbose)std::cerr<<"trace_format=version1_NDJSON; receiver candidates unqualified; no RF/TDMA/NMEA generated\n";
    return 0;
}
std::string utf8(const wchar_t* value){
    const auto count=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,value,-1,nullptr,0,nullptr,nullptr);
    if(count<=0)throw std::invalid_argument("W1 invalid UTF-16 argument");std::string text(static_cast<std::size_t>(count),'\0');
    if(WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,value,-1,text.data(),count,nullptr,nullptr)!=count)throw std::runtime_error("W1 UTF-8 argument conversion failed");text.pop_back();return text;
}
}  // namespace
int wmain(int argc,wchar_t** argv){
    try{if(_setmode(_fileno(stdout),_O_BINARY)==-1)throw std::runtime_error("W1 cannot set binary stdout");std::vector<std::string> args;for(int i=0;i<argc;++i)args.push_back(utf8(argv[i]));return run(args);}
    catch(const std::exception& error){std::cerr<<"W1 error: "<<error.what()<<'\n';return 1;}
}
