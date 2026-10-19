#pragma once

// Independent numerical test oracle. It integrates the geodesic differential
// equations on the WGS84 ellipsoid; it shares no Vincenty inverse/direct
// equations, series coefficients or production helpers with the world engine.
// This is bounded test computation, never a production CPU fallback.

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <stdexcept>

namespace aismixer::lab::test_reference::world_geodesic {
inline constexpr double kPi = 3.141592653589793238462643383279502884;
inline constexpr double kA = 6378137.0;
inline constexpr double kF = 1.0 / 298.257223563;
inline constexpr double kE2 = kF * (2.0 - kF);
struct Result { double longitude_deg, latitude_deg, course_deg; };
using State = std::array<double, 3>;  // Latitude, longitude, azimuth (radians).
inline State derivative(const State& state) {
    const auto sine = std::sin(state[0]);
    const auto denominator = 1.0 - kE2 * sine * sine;
    const auto prime_vertical = kA / std::sqrt(denominator);
    const auto meridian = kA * (1.0 - kE2) / (denominator * std::sqrt(denominator));
    return {{std::cos(state[2]) / meridian,
             std::sin(state[2]) / (prime_vertical * std::cos(state[0])),
             std::sin(state[2]) * std::tan(state[0]) / prime_vertical}};
}
inline State translated(const State& state, const State& slope, double step) {
    return {{state[0] + step * slope[0], state[1] + step * slope[1], state[2] + step * slope[2]}};
}
inline Result direct(double longitude_deg, double latitude_deg, double course_deg,
                     double distance_m, double maximum_step_m = 100.0) {
    if (!std::isfinite(distance_m) || distance_m < 0 || distance_m > 3000000 ||
        !std::isfinite(maximum_step_m) || maximum_step_m < 1 || maximum_step_m > 1000 ||
        !std::isfinite(latitude_deg) || std::abs(latitude_deg) > 80 ||
        !std::isfinite(longitude_deg) || !std::isfinite(course_deg))
        throw std::invalid_argument("bounded independent geodesic oracle input invalid");
    const auto count = std::max(std::size_t{1}, static_cast<std::size_t>(std::ceil(distance_m / maximum_step_m)));
    const auto step = distance_m / static_cast<double>(count);
    State state{{latitude_deg * kPi / 180, longitude_deg * kPi / 180, course_deg * kPi / 180}};
    for (std::size_t i = 0; i < count; ++i) {
        const auto first = derivative(state);
        const auto second = derivative(translated(state, first, step / 2));
        const auto third = derivative(translated(state, second, step / 2));
        const auto fourth = derivative(translated(state, third, step));
        for (std::size_t component = 0; component < state.size(); ++component)
            state[component] += step * (first[component] + 2 * second[component] +
                                       2 * third[component] + fourth[component]) / 6;
    }
    auto longitude = std::remainder(state[1] * 180 / kPi, 360.0);
    auto course = std::fmod(state[2] * 180 / kPi, 360.0);
    if (course < 0) course += 360;
    return {longitude, state[0] * 180 / kPi, course};
}
inline double endpoint_error_m(double longitude_deg, double latitude_deg,
                               const Result& expected) {
    const auto latitude = expected.latitude_deg * kPi / 180;
    const auto sine = std::sin(latitude), denominator = 1 - kE2 * sine * sine;
    const auto north_radius = kA * (1 - kE2) / (denominator * std::sqrt(denominator));
    const auto east_radius = kA * std::cos(latitude) / std::sqrt(denominator);
    const auto north = (latitude_deg - expected.latitude_deg) * kPi / 180 * north_radius;
    const auto east = std::remainder(longitude_deg - expected.longitude_deg, 360.0) * kPi / 180 * east_radius;
    return std::hypot(north, east);
}
// Independent composite Simpson integration of meridional curvature. Endpoints
// on a shared meridian give exact-direction fixtures without an inverse solver.
inline double meridian_distance_m(double latitude_start_deg, double latitude_end_deg) {
    constexpr std::size_t count = 16384;
    auto first = latitude_start_deg * kPi / 180, last = latitude_end_deg * kPi / 180;
    if (first > last) std::swap(first, last);
    const auto curvature = [](double latitude) {
        const auto sine = std::sin(latitude), den = 1 - kE2 * sine * sine;
        return kA * (1 - kE2) / (den * std::sqrt(den));
    };
    const auto step = (last - first) / static_cast<double>(count);
    auto total = curvature(first) + curvature(last);
    for (std::size_t i = 1; i < count; ++i) total += (i % 2 == 0 ? 2 : 4) * curvature(first + static_cast<double>(i) * step);
    return total * step / 3;
}
}  // namespace aismixer::lab::test_reference::world_geodesic
