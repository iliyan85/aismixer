#include "cuda_engine.h"
#include "world.h"
#include "philox_reference.h"
#include "world_geodesic_reference.h"
#include <algorithm>
#include <cmath>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string_view>

namespace w = aismixer::lab::world;
namespace independent = aismixer::lab::test_reference;
namespace ref = independent::world_geodesic;
namespace {
void require(bool value,const char* reason) { if(!value)throw std::runtime_error(reason); }
template<class F> void invalid(F action,std::string_view diagnostic={}) {
    try { action(); } catch(const std::invalid_argument& error) {
        require(std::string_view(error.what()).find(diagnostic)!=std::string_view::npos,"unexpected W1 CUDA host-validation branch");return;
    }
    throw std::runtime_error("invalid W1 CUDA contract accepted");
}
bool same(const w::PositionResult& a,const w::PositionResult& b) {
    return a.location==b.location && a.final_bearing_ndeg==b.final_bearing_ndeg;
}
bool same(const w::RegionSample& a,const w::RegionSample& b) {
    return a.region_id==b.region_id && a.ordinal==b.ordinal && a.location==b.location && a.elevation_mm==b.elevation_mm;
}
template<class T> void equal(const std::vector<T>& a,const std::vector<T>& b,const char* reason) {
    require(a.size()==b.size(),reason);
    for(std::size_t i=0;i<a.size();++i) require(same(a[i],b[i]),reason);
}
std::uint32_t word(std::uint64_t seed,std::uint64_t entity,std::uint32_t purpose,
                   std::uint64_t episode,std::uint32_t draw=0) {
    const auto mixed=seed^entity;
    const independent::PhiloxBlock counter{{static_cast<std::uint32_t>(episode),static_cast<std::uint32_t>(episode>>32),
        draw/4,(1U<<24)|purpose}};
    const independent::PhiloxKey key{{static_cast<std::uint32_t>(mixed),static_cast<std::uint32_t>(mixed>>32)}};
    return independent::philox4x32_10(counter,key)[draw%4];
}
std::int64_t elevation(std::uint64_t seed,const w::RegionSpec& region,std::uint32_t ordinal) {
    const auto width=static_cast<std::uint64_t>(region.max_elevation_mm)-static_cast<std::uint64_t>(region.min_elevation_mm);
    const auto span=width+1, threshold=(std::uint64_t{0}-span)%span;
    for(std::uint32_t proposal=0;proposal<32;++proposal) {
        const auto value=(static_cast<std::uint64_t>(word(seed,region.region_id,14,ordinal,proposal*2))<<32)|
            word(seed,region.region_id,14,ordinal,proposal*2+1);
        if(value>=threshold)return region.min_elevation_mm+static_cast<std::int64_t>(value%span);
    }
    throw std::runtime_error("independent W1 elevation rejection budget exhausted");
}
void fnv(std::uint64_t& digest,std::uint64_t value,unsigned bytes) {
    for(unsigned i=0;i<bytes;++i) {digest^=static_cast<std::uint8_t>(value>>(8*i));digest*=UINT64_C(1099511628211);}
}
w::Config wrapper_config() {
    w::Config config;
    config.profile_id="world.public-session.gpu";
    config.provenance="SYNTHETIC WORLD API TEST; NO AIS TDMA OR RF RECEPTION";
    config.duration=aismixer::lab::runtime::from_seconds(100);
    config.effective_seed=42;
    config.waypoints={{{1},"catalogue.A",{27910000000LL,43210000000LL}},
                      {{2},"catalogue.B",{28960000000LL,41010000000LL}}};
    for(std::uint32_t index=0;index<2;++index) {
        w::Vessel vessel;
        vessel.id={index+1};vessel.equipment_id={index+11};vessel.radio_id={index+21};
        vessel.name=index==0?"vessel.A":"vessel.B";
        vessel.initial={w::SelectionMode::Fixed,{index==0?1U:2U},{}};
        vessel.first_destination={w::SelectionMode::Fixed,{index==0?2U:1U},{}};
        vessel.subsequent_destination.mode=w::SelectionMode::All;
        vessel.speed_mkn={10000+static_cast<std::int64_t>(index)*10000,10000+static_cast<std::int64_t>(index)*10000};
        vessel.speed_hold_seconds={4,4};
        vessel.navigation={{w::NavigationStatus::UnderWayUsingEngine,1,{40,40}}};
        vessel.power=index==0?w::PowerModel{{0,0},{10,10},{5,5}}:w::PowerModel{{5,5},{20,20},{5,5}};
        vessel.reach_radius_mm={10000000,20000000};vessel.reach_hold_seconds={3,7};
        vessel.waterline_ellipsoid_mm=0;
        vessel.waterline_provenance="explicit synthetic ellipsoidal waterline assumption";
        config.vessels.push_back(vessel);
    }
    return config;
}
void equal_world(const w::Snapshot& a,const w::Snapshot& b) {
    require(a.time==b.time && a.vessels.size()==b.vessels.size() && a.receivers.size()==b.receivers.size(),
            "public WorldSession seed replay changed snapshot shape/time");
    for(std::size_t i=0;i<a.vessels.size();++i) {
        const auto& x=a.vessels[i];const auto& y=b.vessels[i];
        require(x.location==y.location && x.course_ndeg==y.course_ndeg && x.state.id==y.state.id &&
            x.state.equipment_id==y.state.equipment_id && x.state.radio_id==y.state.radio_id &&
            x.state.leg_from==y.state.leg_from && x.state.destination==y.state.destination &&
            x.state.leg_index==y.state.leg_index && x.state.leg_length_mm==y.state.leg_length_mm &&
            x.state.leg_distance_mm==y.state.leg_distance_mm && x.state.distance_remainder==y.state.distance_remainder &&
            x.state.total_distance_mm==y.state.total_distance_mm && x.state.total_distance_remainder==y.state.total_distance_remainder &&
            x.state.nominal_speed_mkn==y.state.nominal_speed_mkn && x.state.speed_mkn==y.state.speed_mkn &&
            x.state.navigation_status==y.state.navigation_status && x.state.course_valid==y.state.course_valid &&
            x.state.equipment_on==y.state.equipment_on && x.state.power_generation==y.state.power_generation &&
            x.state.environment_revision==y.state.environment_revision && x.state.effective_reach_radius_mm==y.state.effective_reach_radius_mm &&
            x.state.antenna_offset_mm==y.state.antenna_offset_mm && x.state.antenna_ellipsoid_mm==y.state.antenna_ellipsoid_mm,
            "public WorldSession actual fresh-seed replay changed physical/equipment state");
    }
}
void public_world_session() {
    namespace runtime=aismixer::lab::runtime;
    auto config=wrapper_config();
    w::WorldSession world(config,{1,64});
    bool initialization=false,abort_requirement=false;
    const auto notices=[&](const w::Transition& notice) {
        if(notice.kind==w::TransitionKind::EquipmentOn) {
            require(notice.requires_protocol_initialization,"coupled Class A ON lost fresh-protocol requirement");
            initialization=true;
        }
        if(notice.kind==w::TransitionKind::EquipmentOff)abort_requirement|=notice.requires_abort_of_overlapping_burst;
    };
    world.advance({0},notices);const auto zero=world.snapshot();
    require(zero.time.units==0 && zero.vessels[0].state.equipment_on && !zero.vessels[1].state.equipment_on,
            "public WorldSession initial coupled power boundaries failed");
    const auto zero_owners=w::receiver_ownership(zero);
    const auto& a_radio=zero_owners->receiver({w::radio_name(zero.vessels[0].state.radio_id)});
    const auto& b_radio=zero_owners->receiver({w::radio_name(zero.vessels[1].state.radio_id)});
    require(a_radio.role==runtime::ReceiverRole::TransceiverRadio && a_radio.enabled &&
            a_radio.owner_station_id==w::station_name(zero.vessels[0].state.id) && !b_radio.enabled &&
            b_radio.owner_station_id==w::station_name(zero.vessels[1].state.id),
            "public WorldSession R0.1 owner/role map disagrees with coupled power");
    world.advance(runtime::from_seconds(5),notices);const auto five=world.snapshot();
    require(five.time==runtime::from_seconds(5) && five.vessels[0].state.equipment_on && five.vessels[1].state.equipment_on,
            "public WorldSession independent receiver activation failed");
    world.advance(runtime::from_seconds(10),notices);const auto ten=world.snapshot();
    const auto ten_owners=w::receiver_ownership(ten);
    require(!ten.vessels[0].state.equipment_on && ten.vessels[0].location!=zero.vessels[0].location &&
            !ten_owners->receiver({w::radio_name(ten.vessels[0].state.radio_id)}).enabled &&
            !w::inspect_burst(config,11,runtime::from_seconds(10),runtime::from_seconds(11)).may_start,
            "OFF did not simultaneously disable TX/RX while physical vessel kept moving");
    invalid([&]{world.advance(runtime::from_seconds(9));},"regressed");
    world.advance(runtime::from_seconds(15),notices);const auto fifteen=world.snapshot();
    require(fifteen.time==runtime::from_seconds(15) && fifteen.vessels[0].state.equipment_on &&
            fifteen.vessels[0].location!=ten.vessels[0].location &&
            fifteen.vessels[0].state.power_generation>zero.vessels[0].state.power_generation &&
            initialization && abort_requirement,"reactivated public WorldSession used cached/offline navigation state");
    require(world.cuda().metrics().peak_batch==1 && world.cuda().metrics().position_queries==8,
            "WorldSession.snapshot did not stream two vessels through capacity-one CUDA buffers");
    auto fresh=wrapper_config();fresh.fresh_seed=true;
    for(auto& vessel:fresh.vessels) {
        vessel.initial={w::SelectionMode::All,{},{}};vessel.first_destination={w::SelectionMode::All,{},{}};
        vessel.speed_mkn={10000,20000};vessel.speed_hold_seconds={2,5};
        vessel.power={{0,3},{5,9},{2,4}};
        vessel.navigation={{w::NavigationStatus::UnderWayUsingEngine,2,{4,8}},
            {w::NavigationStatus::AtAnchor,1,{4,8}},{w::NavigationStatus::RestrictedManoeuvrability,1,{4,8}}};
    }
    w::WorldSession fresh_world(fresh,{1,64});
    auto fixed=fresh_world.timeline().config();
    const auto effective_seed=fixed.effective_seed;
    require(!fixed.fresh_seed,"fresh seed request was not consumed before exposing the effective configuration");
    w::WorldSession replay(fixed,{1,256});
    require(replay.timeline().config().effective_seed==effective_seed,"fixed replay ignored recorded actual seed");
    fresh_world.advance(runtime::from_seconds(10));fresh_world.advance(runtime::from_seconds(31));
    fresh_world.advance(runtime::from_seconds(75));replay.advance(runtime::from_seconds(75));
    equal_world(fresh_world.snapshot(),replay.snapshot());
}
}
int main() {
    try {
        w::Session shape64({65536,64}), shape256({65536,256});
        const auto inverse=w::geodesic_inverse({27910000000LL,43210000000LL},{28960000000LL,41010000000LL});
        std::vector<w::PositionQuery> queries{{{27910000000LL,43210000000LL},inverse.initial_bearing_ndeg,185200.0},
            {{0,0},90000000000LL,2000000.0},{{179000000000LL,10000000000LL},90000000000LL,900000.0}};
        for(std::uint32_t i=0;i<8192;++i)queries.push_back({{27910000000LL,43210000000LL},
            static_cast<std::int64_t>(i%360)*w::kDegree+12345,1234.5+static_cast<double>(i%1000)*123.456});
        const auto first=shape64.evaluate(queries), second=shape256.evaluate(queries);
        equal(first,second,"64/256 CUDA launch shapes changed canonical positions/courses");
        std::vector<w::PositionResult> partitioned;
        for(std::size_t offset=0;offset<queries.size();) {
            const auto count=std::min(std::size_t{73},queries.size()-offset);
            const auto result=shape64.evaluate({queries.begin()+static_cast<std::ptrdiff_t>(offset),
                queries.begin()+static_cast<std::ptrdiff_t>(offset+count)});
            partitioned.insert(partitioned.end(),result.begin(),result.end());offset+=count;
        }
        equal(first,partitioned,"GPU query batch partitions changed canonical positions");
        auto reversed=queries;std::reverse(reversed.begin(),reversed.end());
        auto reordered=shape256.evaluate(reversed);std::reverse(reordered.begin(),reordered.end());
        equal(first,reordered,"unrelated query order changed world positions");
        double maximum_reference_error=0;
        for(std::size_t i=0;i<queries.size();++i) {
            const auto host=w::geodesic_direct(queries[i].origin,queries[i].initial_bearing_ndeg,queries[i].distance_m);
            const auto local=ref::Result{host.location.longitude_ndeg/1e9,host.location.latitude_ndeg/1e9,host.final_bearing_ndeg/1e9};
            require(ref::endpoint_error_m(first[i].location.longitude_ndeg/1e9,first[i].location.latitude_ndeg/1e9,local)<0.002,
                    "host/device WGS84 disagreement exceeds 2mm");
            if(i<3 || i%511==0) {
                const auto expected=ref::direct(queries[i].origin.longitude_ndeg/1e9,queries[i].origin.latitude_ndeg/1e9,
                    queries[i].initial_bearing_ndeg/1e9,queries[i].distance_m,100);
                const auto error=ref::endpoint_error_m(first[i].location.longitude_ndeg/1e9,first[i].location.latitude_ndeg/1e9,expected);
                require(error<0.002,"production CUDA WGS84 disagrees with independent ODE reference");
                maximum_reference_error=std::max(maximum_reference_error,error);
            }
        }
        const std::vector<w::RegionSpec> regions{{1001,{27910000000LL,43210000000LL},20000000,10000,300000,4097},
            {2002,{28170000000LL,43410000000LL},10000000,10000,200000,2049}};
        const auto candidates=shape64.sample_regions(42,regions);
        auto region_order=regions;std::reverse(region_order.begin(),region_order.end());
        equal(candidates,shape256.sample_regions(42,region_order),"region order/64vs256 shape changed fixed candidate sites");
        const auto isolated=shape256.sample_regions(42,{regions.front()});
        equal(isolated,{candidates.begin(),candidates.begin()+static_cast<std::ptrdiff_t>(isolated.size())},
              "adding unrelated region changed existing region samples");
        auto prefix_region=regions.front();prefix_region.count=17;
        const auto prefix=shape64.sample_regions(42,{prefix_region});
        equal(prefix,{isolated.begin(),isolated.begin()+static_cast<std::ptrdiff_t>(prefix.size())},
              "adding unrelated receiver ordinals changed existing samples");
        double normalized_radius_squared=0;
        std::size_t radial_cases=0;
        for(std::size_t i=0;i<candidates.size();++i) {
            const auto& sample=candidates[i];const auto& region=sample.region_id==1001?regions[0]:regions[1];
            require(sample.ordinal>=1 && sample.ordinal<=region.count,"receiver ordinal is not positive/stable");
            require(sample.elevation_mm==elevation(42,region,sample.ordinal),"device elevation differs from independent unbiased Philox oracle");
            const auto distance=w::geodesic_inverse(region.center,sample.location).distance_um/1e6;
            require(distance<=region.radius_mm/1000.0+0.002,"sample escaped geodesic horizontal footprint");
            if(sample.region_id==1001) {
                const auto ratio=distance/(region.radius_mm/1000.0);normalized_radius_squared+=ratio*ratio;++radial_cases;
            }
            if(i%317==0) {
                const auto radial=(static_cast<double>(word(42,region.region_id,12,sample.ordinal))+0.5)/4294967296.0;
                const auto bearing=(static_cast<double>(word(42,region.region_id,13,sample.ordinal))+0.5)/4294967296.0*360;
                const auto expected=ref::direct(region.center.longitude_ndeg/1e9,region.center.latitude_ndeg/1e9,
                    std::round(bearing*1e9)/1e9,region.radius_mm/1000.0*std::sqrt(radial));
                require(ref::endpoint_error_m(sample.location.longitude_ndeg/1e9,sample.location.latitude_ndeg/1e9,expected)<0.002,
                        "CUDA area sampler differs from independent semantic Philox + WGS84 ODE reference");
            }
        }
        const auto area_mean=normalized_radius_squared/static_cast<double>(radial_cases);
        require(area_mean>0.45 && area_mean<0.55,"fixed-seed area samples have an incorrect squared-radius distribution");
        const auto center=shape64.sample_regions(42,{{3003,regions.front().center,0,12345,12345,3}});
        for(const auto& sample:center)require(sample.location==regions.front().center && sample.elevation_mm==12345,
                                             "zero-radius/fixed-elevation region changed its center");
        w::Session small({8,64});
        invalid([&]{(void)small.evaluate(std::vector<w::PositionQuery>(9));},"capacity");
        invalid([&]{(void)small.evaluate({{{0,0},-1,1}});},"bearing");
        invalid([&]{(void)small.evaluate({{{0,0},0,std::numeric_limits<double>::quiet_NaN()}});},"distance");
        invalid([&]{(void)small.sample_regions(42,{{1,{0,0},1,0,1,9}});},"invalid");
        invalid([&]{(void)small.sample_regions(42,{{1,{0,0},1,0,1,1},{1,{0,0},1,0,1,1}});},"invalid");
        invalid([&]{(void)small.sample_regions(42,{{1,{0,0},-1,0,1,1}});},"invalid");
        invalid([&]{(void)small.sample_regions(42,{{1,{0,0},1,2,1,1}});},"invalid");
        bool rejected_on_device=false;
        try{(void)small.evaluate({{{0,80000000000LL},0,20000}});}catch(const std::runtime_error& error){
            require(std::string_view(error.what()).find("W1 CUDA geodesic/sample failure")!=std::string_view::npos,"unexpected CUDA error accepted as endpoint rejection");
            rejected_on_device=true;
        }
        require(rejected_on_device,"CUDA accepted an endpoint beyond qualified latitude range");
        require(small.evaluate({{{0,0},0,0}}).front().location==w::Point{0,0},"CUDA session did not recover after rejected endpoint");
        const auto before=shape64.metrics().launches;
        require(shape64.evaluate({}).empty() && shape64.sample_regions(42,{}).empty() && shape64.metrics().launches==before,
                "empty CUDA batch generated a fictitious launch");
        std::uint64_t digest=UINT64_C(14695981039346656037);
        for(const auto& p:first){fnv(digest,static_cast<std::uint64_t>(p.location.longitude_ndeg),8);fnv(digest,static_cast<std::uint64_t>(p.location.latitude_ndeg),8);fnv(digest,static_cast<std::uint64_t>(p.final_bearing_ndeg),8);}
        for(const auto& p:candidates){fnv(digest,p.region_id,4);fnv(digest,p.ordinal,4);fnv(digest,static_cast<std::uint64_t>(p.location.longitude_ndeg),8);fnv(digest,static_cast<std::uint64_t>(p.location.latitude_ndeg),8);fnv(digest,static_cast<std::uint64_t>(p.elevation_mm),8);}
        const auto& metrics=shape64.metrics();const auto& info=shape64.device_info();
        require(metrics.device_bytes==65536*(32+24+64+32+4)+8 && metrics.launches>1 &&
                metrics.position_queries>=queries.size()*2 && metrics.sampled_receivers>=candidates.size(),"CUDA metrics/bounded device allocation accounting failed");
        public_world_session();
        std::cout<<std::setprecision(12)<<"PASS W1 production CUDA "<<info.name<<" sm_"<<info.major<<info.minor
                 <<" queries="<<queries.size()<<" receivers="<<candidates.size()<<" canonical_digest="<<std::hex<<digest<<std::dec
                 <<" reference_error_m="<<maximum_reference_error<<" mean_squared_radius="<<area_mean
                 <<" owned_device_bytes="<<metrics.device_bytes<<" H2D_bytes="<<metrics.h2d_bytes
                 <<" D2H_record_bytes="<<metrics.d2h_bytes<<" diagnostic_bytes="<<metrics.diagnostic_bytes
                 <<" launches="<<metrics.launches<<" position_ms="<<metrics.position_ms<<" sampling_ms="<<metrics.sampling_ms<<'\n';
        return 0;
    }catch(const std::exception& error){
        const std::string_view message=error.what();
        if(message=="No CUDA device is available for W1 world evaluation" ||
            message.find("cudaGetDeviceCount(W1): cudaErrorNoDevice")!=std::string_view::npos){
            std::cout<<"SKIP W1 GPU gate: explicit CUDA no-device outcome\n";return 77;
        }
        std::cerr<<error.what()<<'\n';return 1;
    }
}
