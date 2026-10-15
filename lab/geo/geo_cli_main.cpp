#include "geo_api.h"
#include "geo_ais.h"
#include "geo_config.h"
#include "geo_output.h"
#include "nmea0183.h"
#include "scene_validation.h"
#include "scene_yaml.h"

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#endif

namespace {
using namespace aismixer::lab;
namespace g = aismixer::lab::geo;
using Clock = std::chrono::steady_clock;
double elapsed(Clock::time_point start) {
    return std::chrono::duration<double, std::milli>(Clock::now()-start).count();
}
std::uint64_t scenario_digest(std::vector<Actor> actors, std::uint64_t start,
                              std::uint64_t ticks, std::uint32_t seconds) {
    std::sort(actors.begin(), actors.end(), [](const Actor& a, const Actor& b) {
        return a.actor_id < b.actor_id;
    });
    std::uint64_t hash = UINT64_C(14695981039346656037);
    const auto word = [&](std::uint64_t value, unsigned bytes) {
        for (unsigned i=0; i<bytes; ++i) {
            hash ^= value & 255; hash *= UINT64_C(1099511628211); value >>= 8;
        }
    };
    word(start,8); word(ticks,8); word(seconds,4); word(actors.size(),8);
    for (const auto& a : actors) {
        word(a.actor_id,4); word(static_cast<std::uint32_t>(a.category),4);
        word(static_cast<std::uint64_t>(a.x_mm),8); word(static_cast<std::uint64_t>(a.y_mm),8);
        word(static_cast<std::uint32_t>(a.vx_mm_per_s),4);
        word(static_cast<std::uint32_t>(a.vy_mm_per_s),4);
        word(a.report_interval_ticks,4); word(a.phase_ticks,4);
    }
    return hash;
}
void hash_line(const char* key, std::uint64_t hash) {
    std::cerr << key << '=' << std::hex << std::setfill('0') << std::setw(16)
              << hash << std::dec << std::setfill(' ') << '\n';
}
void metadata(const g::Session& session, const g::Domain& domain,
              const std::vector<Actor>& actors, const std::vector<g::Anchor>& anchors,
              g::Seed seed, std::uint64_t start, std::uint64_t ticks,
              std::uint32_t seconds, const g::Result& result) {
    const auto& info = session.device_info();
    std::cerr << "seed_mode=" << (seed.mode == g::SeedMode::Fresh ? "fresh" : "fixed")
              << "\nseed=" << seed.value << "\nprofile=" << domain.profile_id
              << "\nrng=cuRAND_Philox4_32_10\nrng_domain_version=" << g::kRngDomainVersion
              << "\nprojection=wgs84_tangent_linear_v1\ncoordinate_units=nanodegrees"
              << "\nbuild_toolchain=" << AISMIXER_LAB_GEO_BUILD_INFO
              << "\nqualification_reference=Windows_MSVC_CUDA12.9_sm61"
              << "\ngpu=" << info.name << "\ncompute_capability=" << info.major << '.' << info.minor
              << "\nruntime_version=" << info.runtime_version
              << "\ndriver_version=" << info.driver_version
              << "\npopulation=" << actors.size() << "\nstart_tick=" << start
              << "\ntick_count=" << ticks << "\ntick_seconds=" << seconds
              << "\nsimulation_duration_seconds=" << ticks * seconds << '\n';
    for (unsigned category=0; category<=5; ++category) {
        std::cerr << "category_" << category << "_count="
                  << std::count_if(actors.begin(),actors.end(),[&](const Actor& a) {
                         return static_cast<unsigned>(a.category)==category;
                     }) << '\n';
    }
    hash_line("profile_fnv1a64", g::domain_digest(domain));
    hash_line("overlay_fnv1a64", g::anchor_digest(anchors));
    hash_line("scenario_fnv1a64", scenario_digest(actors,start,ticks,seconds));
    hash_line("result_fnv1a64", g::event_digest(result.events));
}
void metrics(const char* name, const g::Session& session, const g::Result& result) {
    const auto& s = session.setup_metrics(); const auto& m = result.metrics;
    std::cerr << std::fixed << std::setprecision(4) << "case=" << name
              << " candidates=" << m.candidates << " scheduled=" << m.scheduled
              << " accepted=" << m.accepted << " rejected=" << m.rejected
              << " unscheduled=" << m.unscheduled
              << " setup_wall_ms=" << s.setup_wall_ms << " h2d_ms=" << s.h2d_ms
              << " placement_ms=" << s.placement_ms << " motion_ms=" << m.motion_ms
              << " setup_d2h_ms=" << s.d2h_ms
              << " predicate_ms=" << m.predicate_ms << " compaction_ms=" << m.compaction_ms
              << " d2h_ms=" << m.d2h_ms << " steady_wall_ms=" << m.wall_ms
              << " total_wall_ms=" << s.setup_wall_ms + m.wall_ms
              << " h2d_bytes=" << s.h2d_bytes
              << " d2h_record_bytes=" << m.d2h_record_bytes
              << " d2h_diagnostic_bytes=" << s.d2h_diagnostic_bytes + m.d2h_diagnostic_bytes
              << " uncompacted_record_bytes=" << m.uncompacted_record_bytes
              << " device_bytes=" << m.device_bytes
              << " candidate_per_second=" << (m.wall_ms > 0 ? m.candidates*1000.0/m.wall_ms : 0)
              << '\n';
}
struct Arguments {
    bool benchmark=false, demo=false, help=false, fresh=false, seed_set=false;
    std::uint64_t seed=42;
    unsigned block=256;
    std::string domain, scene, overlay, receivers, nmea;
};
Arguments parse(const std::vector<std::string>& arguments) {
    Arguments a;
    std::vector<std::string> seen;
    for (std::size_t i=1; i<arguments.size(); ++i) {
        const std::string& key=arguments[i];
        if (std::find(seen.begin(),seen.end(),key)!=seen.end())
            throw std::invalid_argument("duplicate option: "+key);
        seen.push_back(key);
        const auto value=[&]() -> std::string {
            if (++i>=arguments.size() || arguments[i].empty() || arguments[i].rfind("--",0)==0)
                throw std::invalid_argument("missing or empty value: "+key);
            return arguments[i];
        };
        if (key=="--help") a.help=true;
        else if (key=="--benchmark") a.benchmark=true;
        else if (key=="--demo") a.demo=true;
        else if (key=="--domain") a.domain=value();
        else if (key=="--scene") a.scene=value();
        else if (key=="--overlay") a.overlay=value();
        else if (key=="--receivers") a.receivers=value();
        else if (key=="--nmea") a.nmea=value();
        else if (key=="--fresh-seed") a.fresh=true;
        else if (key=="--seed") { a.seed=runner::parse_u64(value(),"seed"); a.seed_set=true; }
        else if (key=="--block") a.block=runner::parse_u32(value(),"block");
        else throw std::invalid_argument("unknown option: "+key);
    }
    if (a.help) {
        if (seen.size()!=1) throw std::invalid_argument("--help must be used alone");
        return a;
    }
    if (unsigned(a.benchmark)+unsigned(a.demo)+unsigned(!a.scene.empty())!=1)
        throw std::invalid_argument("choose exactly --demo, --benchmark, or --scene PATH");
    if (a.domain.empty()) throw std::invalid_argument("--domain PATH is required");
    if (a.fresh && a.seed_set) throw std::invalid_argument("--seed and --fresh-seed are exclusive");
    if (a.scene.empty()!=a.overlay.empty()) throw std::invalid_argument("--scene requires --overlay and vice versa");
    if (a.benchmark && !a.nmea.empty()) throw std::invalid_argument("benchmark has no NMEA file output");
    if (a.block!=64 && a.block!=128 && a.block!=256) throw std::invalid_argument("--block must be 64, 128, or 256");
    if (!a.nmea.empty()) a.nmea=g::prepare_nmea_output(std::filesystem::u8path(a.nmea)).u8string();
    return a;
}
void emit(const std::vector<g::OfflineTransmission>& transmissions,
          const std::string& path) {
    if (path.empty()) {
#ifdef _WIN32
        if (_setmode(_fileno(stdout),_O_BINARY)==-1) throw std::runtime_error("cannot set stdout binary mode");
#endif
        for (const auto& tx : transmissions) ais::write_sentences(std::cout,tx.sentences);
        std::cout.flush();
        if (!std::cout) throw std::runtime_error("cannot flush controlled NMEA stdout");
    } else {
        g::export_nmea(std::filesystem::u8path(path),transmissions);
    }
}
void run(const Arguments& args) {
    const auto domain=g::load_domain(std::filesystem::u8path(args.domain));
    if (!args.receivers.empty()) {
        const auto receivers=g::load_receivers(std::filesystem::u8path(args.receivers));
        std::cerr << "passive_receiver_count=" << receivers.size() << " (catalog_only_no_RF)\n";
    }
    const auto seed=g::select_seed(args.fresh ? g::SeedMode::Fresh : g::SeedMode::Fixed,args.seed);
    g::Options options; options.block_size=args.block;
    if (args.benchmark) {
        for (const auto count : {std::size_t(100),std::size_t(1000),std::size_t(10000)}) {
            const std::uint64_t ticks=count==100 ? 10 : 100;
            std::vector<Actor> actors;
            for (std::size_t i=0; i<count; ++i) {
                const auto interval=std::uint32_t(i%2 ? 4 : 2);
                actors.push_back({static_cast<std::uint32_t>(i+1),
                    i%2 ? ActorCategory::ClassB : ActorCategory::ClassA,
                    12345,-67890,100,-50,interval,static_cast<std::uint32_t>(i%interval)});
            }
            options.anchor_mode=g::AnchorMode::CudaPlacement; options.candidate_capacity=count*ticks;
            g::Session session(domain,actors,{},seed,options);
            // Warm kernels once, then report median of three equal steady runs.
            session.run(0,1,1);
            std::vector<g::Result> samples;
            for (int repeat=0; repeat<3; ++repeat) samples.push_back(session.run(0,ticks,1));
            const auto digest=g::event_digest(samples.front().events);
            for (const auto& s : samples) if (g::event_digest(s.events)!=digest)
                throw std::runtime_error("benchmark repeated result differs");
            std::sort(samples.begin(),samples.end(),[](const g::Result& a,const g::Result& b) {
                return a.metrics.wall_ms < b.metrics.wall_ms;
            });
            const auto& result=samples[1];
            metadata(session,domain,actors,{},seed,0,ticks,1,result);
            std::cerr << "class_a_count=" << count/2 << "\nclass_b_count=" << count/2
                      << "\nmeasurement=median_of_3_after_1_warmup\n";
            metrics(count==100 ? "small" : count==1000 ? "medium" : "million",session,result);
            const auto ids=g::synthetic_identities(actors);
            const auto sample_size=std::min<std::size_t>(1000,result.events.size());
            std::vector<g::GeoEvent> subset(result.events.begin(),result.events.begin()+sample_size);
            const auto encoding_start=Clock::now();
            const auto encoded=g::encode_offline(subset,ids);
            std::cerr << "cpu_ais_encoding_sample_events=" << encoded.size()
                      << " cpu_ais_encoding_ms=" << elapsed(encoding_start) << '\n';
        }
        return;
    }
    std::vector<Actor> actors;
    std::vector<g::Anchor> anchors;
    std::uint64_t start=0,ticks=4; std::uint32_t seconds=1;
    auto effective_seed=seed;
    if (!args.scene.empty()) {
        const auto scene=runner::load_scene(std::filesystem::u8path(args.scene));
        actors=scene.actors; start=scene.simulation.start_tick;
        ticks=scene.simulation.tick_count; seconds=scene.simulation.tick_seconds;
        if (!args.seed_set && !args.fresh) effective_seed=g::select_seed(g::SeedMode::Fixed,scene.simulation.seed);
        anchors=g::load_overlay(std::filesystem::u8path(args.overlay));
        options.anchor_mode=g::AnchorMode::Explicit;
    } else {
        actors={{902,ActorCategory::ClassB,12345,-67890,-50,25,1,0},
                {701,ActorCategory::ClassA,-999,888,250,-100,1,0}};
        options.anchor_mode=g::AnchorMode::CudaPlacement;
    }
    options.candidate_capacity=std::max<std::size_t>(1,actors.size()*static_cast<std::size_t>(ticks));
    g::Session session(domain,actors,anchors,effective_seed,options);
    const auto result=session.run(start,ticks,seconds);
    metadata(session,domain,actors,anchors,effective_seed,start,ticks,seconds,result);
    std::cerr << "block_size=" << options.block_size << "\nanchor_mode="
              << (options.anchor_mode==g::AnchorMode::Explicit ? "explicit" : "cuda_placement") << '\n';
    metrics("demo",session,result);
    if (args.demo || !args.nmea.empty()) {
        const auto identities=g::synthetic_identities(actors);
        const auto encoding_start=Clock::now();
        const auto transmissions=g::encode_offline(result.events,identities);
        std::cerr << "cpu_ais_encoding_ms=" << elapsed(encoding_start) << '\n';
        g::write_event_metadata(std::cerr,result.events,identities);
        emit(transmissions,args.nmea);
    } else {
        std::cout << "absolute_tick,simulation_seconds,actor_id,category,x_mm,y_mm,longitude_ndeg,latitude_ndeg,random_u32\n";
        for (const auto& e : result.events) std::cout << e.absolute_tick << ',' << e.simulation_seconds
            << ',' << e.actor_id << ',' << static_cast<std::uint32_t>(e.category) << ',' << e.x_mm
            << ',' << e.y_mm << ',' << e.location.longitude_ndeg << ',' << e.location.latitude_ndeg
            << ',' << e.random_u32 << '\n';
        std::cout.flush();
        if (!std::cout) throw std::runtime_error("cannot flush complete geographic CSV stdout");
    }
}
int guarded_run(const std::vector<std::string>& arguments) {
    try {
        const auto args=parse(arguments);
        if (args.help) {
            std::cout << "AISMixer B3.1 offline CUDA geography (no playback/RF/network)\n"
                << "--demo | --benchmark | --scene PATH --overlay PATH\n"
                << "--domain PATH [--seed UINT64 | --fresh-seed] [--block 64|128|256]\n"
                << "[--receivers PATH] [--nmea PATH]\n"
                << "Demo: stdout binary AIVDM; scene: CSV unless --nmea. Diagnostics/replay metadata: stderr.\n"
                << "Benchmarks: 1000/100000/1000000 candidates, median of 3 after warmup.\n"
                << "MMSIs/radio states are synthetic offline test data, not live allocation/TDMA.\n";
            std::cout.flush();
            if (!std::cout) throw std::runtime_error("cannot flush geographic help stdout");
        } else run(args);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "B3.1 error: " << error.what() << '\n'; return 1;
    }
}

#ifdef _WIN32
std::string utf8_argument(const wchar_t* argument) {
    const int bytes=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,argument,-1,
                                       nullptr,0,nullptr,nullptr);
    if (bytes==0) throw std::invalid_argument("command-line argument is not valid Unicode");
    std::string result(static_cast<std::size_t>(bytes),'\0');
    if (WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,argument,-1,result.data(),bytes,
                            nullptr,nullptr)!=bytes)
        throw std::invalid_argument("cannot convert command-line argument to UTF-8");
    result.pop_back();
    return result;
}

void configure_terminal_utf8() {
    DWORD mode=0;
    if (GetConsoleMode(GetStdHandle(STD_OUTPUT_HANDLE),&mode) ||
        GetConsoleMode(GetStdHandle(STD_ERROR_HANDLE),&mode)) SetConsoleOutputCP(CP_UTF8);
}
#endif
}  // namespace

#ifdef _WIN32
int wmain(int argc,wchar_t** argv) {
    configure_terminal_utf8();
    try {
        std::vector<std::string> arguments;
        arguments.reserve(static_cast<std::size_t>(argc));
        for (int index=0; index<argc; ++index) arguments.push_back(utf8_argument(argv[index]));
        return guarded_run(arguments);
    } catch (const std::exception& error) {
        std::cerr << "B3.1 command-line error: " << error.what() << '\n';
        return 1;
    }
}
#else
int main(int argc,char** argv) { return guarded_run(std::vector<std::string>(argv,argv+argc)); }
#endif
