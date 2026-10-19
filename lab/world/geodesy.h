#pragma once

#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <string>

#ifdef __CUDACC__
#define W1_GEO_HD __host__ __device__
#else
#define W1_GEO_HD
#endif

namespace aismixer::lab::world {
inline constexpr std::int64_t kDegree = 1000000000;
inline constexpr std::int64_t kGeodesicMaxLatitude = 80 * kDegree;
inline constexpr double kGeodesicMaxDistanceM = 2500000.0;
inline constexpr const char* kGeodesicVersion = "wgs84-vincenty-bounded-v1";
struct Point { std::int64_t longitude_ndeg = 0, latitude_ndeg = 0; };
W1_GEO_HD constexpr bool operator==(Point a, Point b) noexcept {
    return a.longitude_ndeg == b.longitude_ndeg && a.latitude_ndeg == b.latitude_ndeg;
}
W1_GEO_HD constexpr bool operator!=(Point a, Point b) noexcept { return !(a == b); }
struct GeoInverse {
    std::int64_t distance_um = 0, initial_bearing_ndeg = 0, final_bearing_ndeg = 0;
    bool course_valid = false;
};
struct GeoDirect { Point location; std::int64_t final_bearing_ndeg = 0; };

// WGS84 a/f: https://earth-info.nga.mil/?action=wgs84&dir=wgs84
// Full nested equations (not the reduced-accuracy variants): T. Vincenty,
// Survey Review 23(176), 1975, https://www.ngs.noaa.gov/PUBS_LIB/inverse.pdf
// The inverse has no general antipodal guarantee. This implementation fails
// closed on nonconvergence and limits successful solves to <=2500km, serialized
// endpoint latitudes +/-80 degrees. It supplies no shoreline/datum conversion.
namespace geodesy_detail {
inline constexpr double pi = 3.141592653589793238462643383279502884;
inline constexpr double radians = pi / 180.0;
inline constexpr double a = 6378137.0;
inline constexpr double f = 1.0 / 298.257223563;
inline constexpr double b = a * (1.0 - f);
inline constexpr unsigned max_iterations = 128;
inline constexpr double iteration_tolerance = 1e-13;
enum class Status : std::uint32_t { Ok = 0, InvalidPoint = 1, InvalidBearing = 2,
    InvalidDistance = 3, NonConvergence = 4, NonFinite = 5, RandomExhausted = 6 };
struct RawInverse { double distance_m = 0, initial_bearing_deg = 0, final_bearing_deg = 0; bool course_valid = false; };
W1_GEO_HD inline bool finite(double value) {
#ifdef __CUDA_ARCH__
    return isfinite(value);
#else
    return std::isfinite(value);
#endif
}
W1_GEO_HD constexpr bool valid_point(Point point) {
    return point.longitude_ndeg >= -180*kDegree && point.longitude_ndeg <= 180*kDegree &&
           point.latitude_ndeg >= -kGeodesicMaxLatitude && point.latitude_ndeg <= kGeodesicMaxLatitude;
}
W1_GEO_HD inline double longitude_radians(double value) {
    double result = ::fmod(value + pi, 2*pi);
    if (result < 0) result += 2*pi;
    return result - pi;
}
W1_GEO_HD inline double bearing_degrees(double value) {
    double result = ::fmod(value / radians, 360.0);
    if (result < 0) result += 360.0;
    return result;
}
// Inputs are already bounded far inside int64. Nearest, exact ties away from
// zero; no host rounding-mode dependence, saturation or cast of NaN/infinity.
W1_GEO_HD inline std::int64_t nearest_integer(double value) {
    return static_cast<std::int64_t>(value < 0 ? ::ceil(value - 0.5) : ::floor(value + 0.5));
}
W1_GEO_HD inline std::int64_t bearing_ndeg(double degrees) {
    auto result = nearest_integer(degrees * static_cast<double>(kDegree));
    if (result == 360*kDegree) result = 0;
    return result;
}
W1_GEO_HD inline void coefficients(double cosine_squared_alpha, double& A, double& B) {
    const double u2 = cosine_squared_alpha * (a*a - b*b) / (b*b);
    A = 1 + u2/16384 * (4096 + u2*(-768 + u2*(320 - 175*u2)));
    B = u2/1024 * (256 + u2*(-128 + u2*(74 - 47*u2)));
}
W1_GEO_HD inline double delta_sigma(double B, double sine_sigma, double cosine_sigma, double cosine_twice_middle) {
    const double c2 = cosine_twice_middle*cosine_twice_middle;
    return B*sine_sigma*(cosine_twice_middle + B/4*(cosine_sigma*(-1 + 2*c2) -
        B/6*cosine_twice_middle*(-3 + 4*sine_sigma*sine_sigma)*(-3 + 4*c2)));
}
W1_GEO_HD inline Status inverse(Point origin, Point target, RawInverse& result) {
    if (!valid_point(origin) || !valid_point(target)) return Status::InvalidPoint;
    const double phi1 = static_cast<double>(origin.latitude_ndeg)/kDegree*radians;
    const double phi2 = static_cast<double>(target.latitude_ndeg)/kDegree*radians;
    const double L = longitude_radians(static_cast<double>(target.longitude_ndeg-origin.longitude_ndeg)/kDegree*radians);
    const double U1 = ::atan((1-f)*::tan(phi1)), U2 = ::atan((1-f)*::tan(phi2));
    const double s1 = ::sin(U1), c1 = ::cos(U1), s2 = ::sin(U2), c2 = ::cos(U2);
    double lambda = L, sine_sigma = 0, cosine_sigma = 0, sigma = 0;
    double sine_alpha = 0, cosine_squared_alpha = 0, cosine_twice_middle = 0;
    bool converged = false;
    for (unsigned iteration = 0; iteration < max_iterations; ++iteration) {
        const double sl = ::sin(lambda), cl = ::cos(lambda);
        const double x = c2*sl, y = c1*s2 - s1*c2*cl;
        sine_sigma = ::sqrt(x*x+y*y);
        cosine_sigma = s1*s2+c1*c2*cl;
        if (sine_sigma < 1e-30) {
            if (cosine_sigma > 0) { result = {}; return Status::Ok; }
            return Status::NonConvergence;
        }
        sigma = ::atan2(sine_sigma, cosine_sigma);
        sine_alpha = c1*c2*sl/sine_sigma;
        cosine_squared_alpha = 1-sine_alpha*sine_alpha;
        if (cosine_squared_alpha < 0) cosine_squared_alpha = 0;
        cosine_twice_middle = cosine_squared_alpha > 1e-24 ?
            cosine_sigma-2*s1*s2/cosine_squared_alpha : 0;
        const double C = f/16*cosine_squared_alpha*(4+f*(4-3*cosine_squared_alpha));
        const double next = L+(1-C)*f*sine_alpha*(sigma+C*sine_sigma*(cosine_twice_middle+
            C*cosine_sigma*(-1+2*cosine_twice_middle*cosine_twice_middle)));
        if (!finite(next)) return Status::NonFinite;
        if (::fabs(next-lambda) <= iteration_tolerance) { lambda = next; converged = true; break; }
        lambda = next;
    }
    if (!converged) return Status::NonConvergence;
    double A = 0, B = 0; coefficients(cosine_squared_alpha,A,B);
    result.distance_m = b*A*(sigma-delta_sigma(B,sine_sigma,cosine_sigma,cosine_twice_middle));
    if (!finite(result.distance_m)) return Status::NonFinite;
    if (result.distance_m < 0 || result.distance_m > kGeodesicMaxDistanceM) return Status::InvalidDistance;
    result.initial_bearing_deg = bearing_degrees(::atan2(c2*::sin(lambda), c1*s2-s1*c2*::cos(lambda)));
    result.final_bearing_deg = bearing_degrees(::atan2(c1*::sin(lambda), -s1*c2+c1*s2*::cos(lambda)));
    result.course_valid = true;
    return Status::Ok;
}
W1_GEO_HD inline Status direct(Point origin, std::int64_t initial_bearing, double distance_m, GeoDirect& result) {
    if (!valid_point(origin)) return Status::InvalidPoint;
    if (initial_bearing < 0 || initial_bearing >= 360*kDegree) return Status::InvalidBearing;
    if (!finite(distance_m) || distance_m < 0 || distance_m > kGeodesicMaxDistanceM) return Status::InvalidDistance;
    if (distance_m == 0) { result = {origin,initial_bearing}; return Status::Ok; }
    const double alpha1 = static_cast<double>(initial_bearing)/kDegree*radians;
    const double phi1 = static_cast<double>(origin.latitude_ndeg)/kDegree*radians;
    const double U1 = ::atan((1-f)*::tan(phi1));
    const double s1 = ::sin(U1), c1 = ::cos(U1), sa1 = ::sin(alpha1), ca1 = ::cos(alpha1);
    const double sigma1 = ::atan2(::tan(U1),ca1);
    const double sine_alpha = c1*sa1, cosine_squared_alpha = 1-sine_alpha*sine_alpha;
    double A = 0, B = 0; coefficients(cosine_squared_alpha,A,B);
    const double base_sigma = distance_m/(b*A);
    double sigma = base_sigma;
    bool converged = false;
    for (unsigned iteration = 0; iteration < max_iterations; ++iteration) {
        const double next = base_sigma+delta_sigma(B,::sin(sigma),::cos(sigma),::cos(2*sigma1+sigma));
        if (!finite(next)) return Status::NonFinite;
        if (::fabs(next-sigma) <= iteration_tolerance) { sigma = next; converged = true; break; }
        sigma = next;
    }
    if (!converged) return Status::NonConvergence;
    const double ss = ::sin(sigma), cs = ::cos(sigma), cm = ::cos(2*sigma1+sigma);
    const double temporary = s1*ss-c1*cs*ca1;
    const double phi2 = ::atan2(s1*cs+c1*ss*ca1,(1-f)*::sqrt(sine_alpha*sine_alpha+temporary*temporary));
    const double lambda = ::atan2(ss*sa1,c1*cs-s1*ss*ca1);
    const double C = f/16*cosine_squared_alpha*(4+f*(4-3*cosine_squared_alpha));
    const double L = lambda-(1-C)*f*sine_alpha*(sigma+C*ss*(cm+C*cs*(-1+2*cm*cm)));
    const double lon2 = longitude_radians(static_cast<double>(origin.longitude_ndeg)/kDegree*radians+L);
    const double latitude_ndeg = phi2/radians*kDegree, longitude_ndeg = lon2/radians*kDegree;
    if (!finite(latitude_ndeg) || !finite(longitude_ndeg)) return Status::NonFinite;
    result.location = {nearest_integer(longitude_ndeg),nearest_integer(latitude_ndeg)};
    if (result.location.longitude_ndeg == 180*kDegree) result.location.longitude_ndeg = -180*kDegree;
    if (!valid_point(result.location)) return Status::InvalidPoint;
    result.final_bearing_ndeg = bearing_ndeg(bearing_degrees(::atan2(sine_alpha,-temporary)));
    return Status::Ok;
}
inline const char* status_name(Status status) {
    switch (status) {
    case Status::Ok: return "success";
    case Status::InvalidPoint: return "point outside WGS84 +/-80 degree latitude qualification";
    case Status::InvalidBearing: return "bearing outside [0,360) degrees";
    case Status::InvalidDistance: return "distance must be finite in [0,2500000] metres";
    case Status::NonConvergence: return "geodesic failed to converge; near-antipodal solutions unsupported";
    case Status::NonFinite: return "nonfinite geodesic computation";
    case Status::RandomExhausted: return "unbiased random rejection budget exhausted";
    }
    return "unknown geodesic status";
}
inline void require_success(Status status) {
    if (status != Status::Ok) throw std::invalid_argument(std::string("W1 WGS84: ")+status_name(status));
}
}  // namespace geodesy_detail

inline void validate_point(Point point) {
    if (!geodesy_detail::valid_point(point)) geodesy_detail::require_success(geodesy_detail::Status::InvalidPoint);
}
inline GeoInverse geodesic_inverse(Point origin, Point target) {
    geodesy_detail::RawInverse raw;
    geodesy_detail::require_success(geodesy_detail::inverse(origin,target,raw));
    return {geodesy_detail::nearest_integer(raw.distance_m*1000000.0),
        geodesy_detail::bearing_ndeg(raw.initial_bearing_deg),
        geodesy_detail::bearing_ndeg(raw.final_bearing_deg),raw.course_valid};
}
inline GeoDirect geodesic_direct(Point origin, std::int64_t bearing, double distance_m) {
    GeoDirect result;
    geodesy_detail::require_success(geodesy_detail::direct(origin,bearing,distance_m,result));
    return result;
}
}  // namespace aismixer::lab::world

#undef W1_GEO_HD
