#include "geodesy.h"
#include "world_geodesic_reference.h"
#include <algorithm>
#include <cmath>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string_view>

namespace w = aismixer::lab::world;
namespace ref = aismixer::lab::test_reference::world_geodesic;
namespace {
void require(bool value,const char* reason) { if(!value)throw std::runtime_error(reason); }
template<class F> void invalid(F action,std::string_view diagnostic={}) {
    try { action(); } catch(const std::invalid_argument& error) {
        require(std::string_view(error.what()).find(diagnostic)!=std::string_view::npos,"unexpected WGS84 rejection branch");
        return;
    }
    throw std::runtime_error("invalid WGS84 solve accepted");
}
std::int64_t ndeg(double value) { return static_cast<std::int64_t>(std::round(value*1e9)); }
double angle_error(double a,double b) { return std::abs(std::remainder(a-b,360.0)); }
}
int main() {
    try {
        constexpr double tolerance_m = 0.002;
        const w::Point origin{27910000000LL,43210000000LL}, target{28960000000LL,41010000000LL};
        const auto inverse=w::geodesic_inverse(origin,target);
        require(inverse.course_valid && std::abs(inverse.distance_um/1e6-259332.189502357)<tolerance_m,
                "Black Sea-scale inverse disagrees with independently solved ellipsoid ODE");
        require(angle_error(inverse.initial_bearing_ndeg/1e9,160.081995740407)<1e-7,
                "Black Sea-scale initial course differs from independent ODE shooting solution");
        const auto five_hours=w::geodesic_direct(origin,inverse.initial_bearing_ndeg,185200.0);
        require(ref::endpoint_error_m(five_hours.location.longitude_ndeg/1e9,five_hours.location.latitude_ndeg/1e9,
                    {28.667187719559,41.639944356435,160.592865015522})<tolerance_m,
                "five-hour 20kn voyage endpoint differs from independent ODE pin");

        double maximum_error=0, maximum_refinement=0, maximum_inverse_error=0;
        std::size_t cases=0;
        for(const auto start: {w::Point{0,0},origin,w::Point{179000000000LL,10000000000LL},
                              w::Point{-179000000000LL,-37000000000LL},w::Point{10000000000LL,70000000000LL}}) {
            for(const double bearing: {0.0,90.0,160.081995740,270.0}) {
                for(const double distance: {0.0001,1.0,20000.0,185200.0,900000.0,2000000.0}) {
                    const auto expected=ref::direct(start.longitude_ndeg/1e9,start.latitude_ndeg/1e9,bearing,distance,100);
                    if(std::abs(expected.latitude_deg)>80) continue;
                    const auto refined=ref::direct(start.longitude_ndeg/1e9,start.latitude_ndeg/1e9,bearing,distance,50);
                    const auto refinement=ref::endpoint_error_m(expected.longitude_deg,expected.latitude_deg,refined);
                    require(refinement<0.00005,"independent ODE step refinement did not converge");
                    maximum_refinement=std::max(maximum_refinement,refinement);
                    const auto actual=w::geodesic_direct(start,ndeg(bearing),distance);
                    const auto error=ref::endpoint_error_m(actual.location.longitude_ndeg/1e9,actual.location.latitude_ndeg/1e9,refined);
                    require(error<tolerance_m,"bounded WGS84 direct exceeds 2mm independent reference tolerance");
                    require(angle_error(actual.final_bearing_ndeg/1e9,refined.course_deg)<1e-7,
                            "direct final forward course differs from independent ODE");
                    maximum_error=std::max(maximum_error,error);
                    const auto back=w::geodesic_inverse(start,actual.location);
                    const auto inverse_error=std::abs(back.distance_um/1e6-distance);
                    require(inverse_error<tolerance_m,"inverse distance disagrees with independent direct fixture");
                    if(distance>=1000) require(angle_error(back.initial_bearing_ndeg/1e9,bearing)<0.00001,
                                              "inverse initial course disagrees with independent fixture");
                    maximum_inverse_error=std::max(maximum_inverse_error,inverse_error); ++cases;
                }
            }
        }
        const auto equator=w::geodesic_inverse({0,0},{1000000000,0});
        require(std::abs(equator.distance_um/1e6-ref::kA*ref::kPi/180)<tolerance_m &&
                equator.initial_bearing_ndeg==90000000000LL,"exact equatorial fixture failed");
        const auto meridian=w::geodesic_inverse({0,0},{0,10000000000LL});
        require(std::abs(meridian.distance_um/1e6-ref::meridian_distance_m(0,10))<tolerance_m &&
                meridian.initial_bearing_ndeg==0,"independent meridian integral fixture failed");
        const auto coincident=w::geodesic_inverse(origin,origin);
        require(coincident.distance_um==0 && !coincident.course_valid,"coincident inverse invented course/distance");
        require(w::geodesic_direct(origin,123000000000LL,0).location==origin,"zero distance changed immutable origin");
        require(w::geodesic_inverse({0,0},{1,0}).distance_um>0,"short leg collapsed to coincident");
        invalid([&]{(void)w::geodesic_inverse({0,0},{179999900000LL,100000});},"converge");
        invalid([&]{(void)w::geodesic_inverse({0,0},{60000000000LL,0});},"distance");
        invalid([&]{(void)w::geodesic_direct({0,90000000000LL},0,1);},"point");
        invalid([&]{(void)w::geodesic_direct({181000000000LL,0},0,1);},"point");
        invalid([&]{(void)w::geodesic_direct({0,80000000000LL},0,20000);},"point");
        invalid([&]{(void)w::geodesic_direct(origin,-1,1);},"bearing");
        invalid([&]{(void)w::geodesic_direct(origin,360000000000LL,1);},"bearing");
        invalid([&]{(void)w::geodesic_direct(origin,0,-1);},"distance");
        invalid([&]{(void)w::geodesic_direct(origin,0,2500000.001);},"distance");
        invalid([&]{(void)w::geodesic_direct(origin,0,std::numeric_limits<double>::infinity());},"distance");
        invalid([&]{(void)w::geodesic_direct(origin,0,std::numeric_limits<double>::quiet_NaN());},"distance");
        std::cout<<std::setprecision(12)<<"PASS W1 bounded WGS84 independent ODE cases="<<cases
                 <<" max_direct_error_m="<<maximum_error<<" max_inverse_error_m="<<maximum_inverse_error
                 <<" max_reference_refinement_m="<<maximum_refinement<<" tolerance_m="<<tolerance_m<<'\n';
        return 0;
    } catch(const std::exception& error) { std::cerr<<error.what()<<'\n';return 1; }
}
