#include "config.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace {
using namespace aismixer::lab::world;
namespace fs = std::filesystem;
std::size_t rejected = 0, cases = 0;
void require(bool condition, std::string_view reason) { if (!condition) throw std::runtime_error(std::string(reason)); }
std::string replace(std::string source, const std::string& before, const std::string& after) {
    const auto at = source.find(before); require(at != std::string::npos, "test replacement pattern absent");
    source.replace(at, before.size(), after); return source;
}
std::string read(const fs::path& path) {
    std::ifstream stream(path, std::ios::binary); require(static_cast<bool>(stream), "test read failed");
    return std::string(std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>());
}
void write(const fs::path& path, const std::string& value) {
    std::ofstream stream(path, std::ios::binary | std::ios::trunc); require(static_cast<bool>(stream), "test open failed");
    stream.write(value.data(), static_cast<std::streamsize>(value.size())); stream.flush();
    require(static_cast<bool>(stream), "test write failed"); stream.close(); require(!stream.fail(), "test close failed");
}
std::string crlf(const std::string& source) {
    std::string result; for (char byte : source) { if (byte == '\n') result += '\r'; result += byte; } return result;
}
std::uint64_t hash(std::string_view source) {
    auto result = UINT64_C(14695981039346656037);
    for (const unsigned char byte : source) { result ^= byte; result *= UINT64_C(1099511628211); }
    return result;
}
class Scratch {
public:
    explicit Scratch(const fs::path& work) : parent_(fs::absolute(work).lexically_normal()) {
        fs::create_directories(parent_);
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        for (unsigned attempt = 0; attempt < 100; ++attempt) {
            path_ = parent_ / ("world-config-" + std::to_string(stamp) + '-' + std::to_string(attempt));
            std::error_code error;
            if (fs::create_directory(path_, error)) return;
            require(!error || error == std::errc::file_exists, "cannot create test scratch");
        }
        throw std::runtime_error("test scratch exhausted");
    }
    ~Scratch() {
        if (path_.parent_path() == parent_ && path_.filename().string().rfind("world-config-", 0) == 0) {
            std::error_code ignored; fs::remove_all(path_, ignored);
        }
    }
    fs::path operator/(const fs::path& part) const { return path_ / part; }
private:
    fs::path parent_, path_;
};

const std::string scenario = R"YAML(schema_version: 1
kind: world_scenario
profile_id: world_config_test_v1
provenance: WORLD STATE ONLY - NO AIS TDMA OR RF; exact-decimal configuration test
duration_seconds: 600
seed: {mode: fixed, value: 42}
waypoint_catalog: waypoints.yaml
receiver_regions: regions.yaml
vessels:
  - id: 101
    equipment_id: 201
    radio_id: 301
    name: Configuration vessel
    route:
      initial: {mode: fixed, waypoint_id: 1}
      first_destination: {mode: fixed, waypoint_id: 2}
      subsequent_destination: {mode: subset, waypoint_ids: [1, 2, 3]}
    speed: {knots: [10.001, 20.999], hold_seconds: [100, 200]}
    navigation:
      - {status: UNDER_WAY_USING_ENGINE, weight: 6, duration_seconds: [100, 200]}
      - {status: AT_ANCHOR, weight: 2, duration_seconds: [100, 200]}
      - {status: RESTRICTED_MANOEUVRABILITY, weight: 2, duration_seconds: [100, 200]}
    restricted_speed_cap_knots: 4.125
    power:
      initial_delay_seconds: [0, 0]
      online_duration_seconds: [2, 2]
      offline_duration_seconds: [1, 1]
    antenna:
      height_above_waterline_m: 18.125
      waterline:
        height_datum: unresolved
        provenance: No geoid or sea-surface ellipsoid data supplied
    reach: {radius_km: [0.000001, 60.123456], hold_seconds: [100, 200]}
)YAML";
const std::string waypoints = R"YAML(schema_version: 1
kind: world_waypoint_catalog
profile_id: world_config_waypoints_v1
provenance: Unqualified synthetic catalogue copied from representative settlement points
coordinate_units: nanodegrees
coordinate_order: longitude_latitude
datum: WGS84
ring_closure: explicit
waypoints:
  - {id: 1, name: Varna, longitude_ndeg: 27910000000, latitude_ndeg: 43210000000}
  - {id: 2, name: Balchik, longitude_ndeg: 28170000000, latitude_ndeg: 43410000000}
  - {id: 3, name: Burgas, longitude_ndeg: 27470000000, latitude_ndeg: 42500000000}
  - {id: 1, name: Varna, longitude_ndeg: 27910000000, latitude_ndeg: 43210000000}
)YAML";
const std::string region_source = R"YAML(schema_version: 1
kind: world_receiver_regions
profile_id: world_config_regions_v1
provenance: Unqualified synthetic candidates, not real receiver installations
coordinate_units: nanodegrees
coordinate_order: longitude_latitude
datum: WGS84
regions:
  - id: 11
    name: Configuration region
    center: {longitude_ndeg: 27910000000, latitude_ndeg: 43210000000}
    radius_km: 20.000001
    count: [1, 3]
    antenna_elevation_m: [10.001, 300.125]
    height_datum: AMSL
    provenance: Unqualified synthetic candidate; no geoid conversion supplied
    power:
      initial_delay_seconds: [0, 10]
      online_duration_seconds: [100, 1000]
      offline_duration_seconds: [10, 300]
)YAML";

Config load_case(const Scratch& scratch, const std::string& world = scenario,
                 const std::string& points = waypoints, const std::string& regions = region_source) {
    const auto directory = scratch / std::to_string(++cases); require(fs::create_directory(directory), "test case directory exists");
    write(directory / "world.yaml", world); write(directory / "waypoints.yaml", points); write(directory / "regions.yaml", regions);
    return load_world(directory / "world.yaml");
}
void reject_case(const Scratch& scratch, const std::string& world = scenario,
                 const std::string& points = waypoints, const std::string& regions = region_source,
                 std::string_view diagnostic = {}) {
    bool caught = false;
    try { (void)load_case(scratch, world, points, regions); }
    catch (const std::invalid_argument& error) {
        require(diagnostic.empty() || std::string_view(error.what()).find(diagnostic) != std::string_view::npos,
                std::string("wrong validation boundary: ") + error.what());
        caught = true;
    }
    require(caught, "invalid world configuration accepted"); ++rejected;
}
void positive_contracts(const Scratch& scratch, const fs::path& profiles) {
    const auto config = load_case(scratch);
    require(config.schema_version == 1 && config.duration.units == 600 * aismixer::lab::runtime::kUnitsPerSecond &&
            !config.fresh_seed && config.effective_seed == 42, "world metadata/time/seed parse");
    require(config.waypoints.size() == 3 && config.waypoints.front().id.value == 1 &&
            config.waypoints.front().name == "Varna", "explicit closing vertex was sampled as a distinct waypoint");
    const auto& vessel = config.vessels.at(0);
    require(vessel.id.value == 101 && vessel.equipment_id.value == 201 && vessel.radio_id.value == 301 &&
            vessel.speed_mkn.minimum == 10001 && vessel.speed_mkn.maximum == 20999 &&
            vessel.restricted_speed_cap_mkn == 4125 && vessel.antenna_offset_mm == 18125 &&
            vessel.reach_radius_mm.minimum == 1 && vessel.reach_radius_mm.maximum == 60123456 &&
            vessel.power.initial_delay_seconds.minimum == 0 && vessel.power.online_duration_seconds.maximum == 2,
            "exact speed/mount/reach units or common equipment power parse");
    require(!vessel.waterline_ellipsoid_mm && config.regions.at(0).height_datum == HeightDatum::Amsl &&
            config.regions.at(0).radius_mm == 20000001 && config.regions.at(0).elevation_mm.minimum == 10001 &&
            config.regions.at(0).elevation_mm.maximum == 300125, "source height datum was converted or units rounded");
    require(config.scenario_hash == hash(scenario) && config.waypoint_hash == hash(waypoints) &&
            config.regions_hash == hash(region_source), "independent LF-source FNV fingerprints");
    const auto windows = load_case(scratch, crlf(scenario), crlf(waypoints), crlf(region_source));
    require(windows.scenario_hash == config.scenario_hash && windows.waypoint_hash == config.waypoint_hash &&
            windows.regions_hash == config.regions_hash, "CRLF normalization changed source fingerprints");
    const auto fresh = load_case(scratch, replace(scenario, "mode: fixed, value: 42", "mode: fresh"));
    require(fresh.fresh_seed && fresh.effective_seed == 0, "parser sampled or invented a fresh seed");
    const auto maximum_seed = load_case(scratch, replace(scenario, "value: 42", "value: 18446744073709551615"));
    require(maximum_seed.effective_seed == UINT64_MAX, "unsigned64 seed precision lost");
    auto all = replace(scenario, "initial: {mode: fixed, waypoint_id: 1}", "initial: {mode: all}");
    all = replace(all, "first_destination: {mode: fixed, waypoint_id: 2}", "first_destination: {mode: subset, waypoint_ids: [1, 2]}");
    all = replace(all, "subsequent_destination: {mode: subset, waypoint_ids: [1, 2, 3]}", "subsequent_destination: {mode: all}");
    const auto selected = load_case(scratch, all);
    require(selected.vessels.front().initial.mode == SelectionMode::All &&
            selected.vessels.front().first_destination.mode == SelectionMode::Subset &&
            selected.vessels.front().subsequent_destination.mode == SelectionMode::All, "operator all/subset selections inaccessible");
    const auto subset = load_case(scratch, replace(scenario, "initial: {mode: fixed, waypoint_id: 1}",
        "initial: {mode: subset, waypoint_ids: [1, 3]}"));
    require(subset.vessels.front().initial.waypoint_ids.size() == 2, "initial subset parse");
    const auto filtered_all = load_case(scratch, replace(scenario, "initial: {mode: fixed, waypoint_id: 1}",
        "initial: {mode: all}"));
    const auto& all_route = filtered_all.vessels.front();
    require(all_route.initial.mode == SelectionMode::All && all_route.initial.waypoint_id.value == 0 &&
            all_route.initial.waypoint_ids.empty() && all_route.first_destination.mode == SelectionMode::Fixed &&
            all_route.first_destination.waypoint_id.value == 2 && all_route.first_destination.waypoint_ids.empty(),
            "all initial plus fixed first destination rejected or configured selector rewritten");
    const auto filtered_subset = load_case(scratch, replace(scenario, "initial: {mode: fixed, waypoint_id: 1}",
        "initial: {mode: subset, waypoint_ids: [3, 2, 1]}"));
    const auto& subset_route = filtered_subset.vessels.front();
    require(subset_route.initial.mode == SelectionMode::Subset && subset_route.initial.waypoint_id.value == 0 &&
            subset_route.initial.waypoint_ids == std::vector<WaypointId>{{3}, {2}, {1}} &&
            subset_route.first_destination.mode == SelectionMode::Fixed &&
            subset_route.first_destination.waypoint_id.value == 2 && subset_route.first_destination.waypoint_ids.empty(),
            "overlapping initial subset rejected or configured pool/fixed destination rewritten");
    auto explicit_height = replace(scenario, "height_datum: unresolved", "height_datum: WGS84_ellipsoid\n        elevation_m: -0.001");
    const auto resolved = load_case(scratch, explicit_height, waypoints,
        replace(region_source, "height_datum: AMSL", "height_datum: WGS84_ellipsoid"));
    require(resolved.vessels.front().waterline_ellipsoid_mm == -1 &&
            resolved.regions.front().height_datum == HeightDatum::Wgs84Ellipsoid, "explicit ellipsoid assumption lost signed millimetres");
    const auto quoted = load_case(scratch, replace(scenario, "knots: [10.001, 20.999]", "knots: ['10.001', '20.999']"));
    require(quoted.vessels.front().speed_mkn.minimum == 10001, "numeric quote style changed exact units");
    const auto zero_reach = load_case(scratch, replace(scenario, "radius_km: [0.000001, 60.123456]", "radius_km: [0, 0]"),
        waypoints, replace(region_source, "radius_km: 20.000001", "radius_km: 0"));
    require(zero_reach.vessels.front().reach_radius_mm.maximum == 0 && zero_reach.regions.front().radius_mm == 0,
            "nonnegative zero reach/footprint forbidden or reinterpreted as reception");
    const auto pair = load_case(scratch, replace(scenario, "waypoint_ids: [1, 2, 3]", "waypoint_ids: [1, 2]"),
        replace(waypoints, "  - {id: 3, name: Burgas, longitude_ndeg: 27470000000, latitude_ndeg: 42500000000}\n", ""));
    require(pair.waypoints.size() == 2, "waypoint catalogue was incorrectly treated as a polygon mask");
    const auto largest_region = load_case(scratch, scenario, waypoints, replace(region_source, "count: [1, 3]", "count: [4095, 4095]"));
    require(largest_region.regions.front().count.maximum == 4095, "combined radio capacity rejected its exact boundary");

    const auto unicode = scratch / fs::u8path(u8"\u043A\u043E\u043D\u0444\u0438\u0433 \u6D77"); require(fs::create_directory(unicode), "Unicode scratch exists");
    const auto reference = u8"\u043A\u0430\u0442\u0430\u043B\u043E\u0433 \u6D77.yaml";
    write(unicode / fs::u8path(reference), waypoints); write(unicode / "regions.yaml", region_source);
    write(unicode / "world.yaml", replace(scenario, "waypoint_catalog: waypoints.yaml", std::string("waypoint_catalog: '") + reference + '\''));
    require(load_world(unicode / "world.yaml").waypoints.size() == 3, "Unicode relative file references failed");

    const auto demo = load_world(profiles / "black_sea_world_v1.yaml");
    require(demo.waypoints.size() == 16 && demo.vessels.size() == 2 && demo.regions.size() == 2 &&
            demo.duration.units == 18000 * aismixer::lab::runtime::kUnitsPerSecond &&
            demo.waypoints.at(4).name == "Varna" && demo.waypoints.back().name == "Mangalia" &&
            demo.regions.at(0).height_datum == HeightDatum::Amsl && !demo.vessels.front().waterline_ellipsoid_mm,
            "dedicated demo altered catalogue identities, datum or world-only scenario");
}

void yaml_boundaries(const Scratch& scratch) {
    reject_case(scratch, scenario + "schema_version: 1\n", waypoints, region_source, "duplicate");
    reject_case(scratch, scenario + "unknown_option: 1\n", waypoints, region_source, "unknown");
    reject_case(scratch, replace(scenario, "duration_seconds: 600\n", ""), waypoints, region_source, "missing");
    reject_case(scratch, replace(scenario, "schema_version: 1", "schema_version: 2"), waypoints, region_source, "version");
    reject_case(scratch, replace(scenario, "kind: world_scenario", "kind: legacy_scene"), waypoints, region_source, "kind");
    reject_case(scratch, scenario + "---\n" + scenario, waypoints, region_source, "one YAML document");
    reject_case(scratch, replace(scenario, "profile_id: world_config_test_v1", "profile_id: &unused world_config_test_v1"), waypoints, region_source, "anchors");
    reject_case(scratch, replace(scenario, "profile_id: world_config_test_v1", "profile_id: &alias world_config_test_v1\nprovenance: *alias"), waypoints, region_source, "anchors");
    reject_case(scratch, replace(scenario, "value: 42", "value: !!int 42"), waypoints, region_source, "tags");
    reject_case(scratch, replace(scenario, "profile_id: world_config_test_v1", "profile_id: !custom profile"), waypoints, region_source, "tags");
    reject_case(scratch, replace(scenario, "seed: {mode: fixed, value: 42}", "seed: null"), waypoints, region_source, "null");
    reject_case(scratch, std::string(33, '[') + '0' + std::string(33, ']'), waypoints, region_source, "nesting");
    std::string nodes = "["; for (unsigned index = 0; index < 100001; ++index) nodes += "0,"; nodes += "0]";
    reject_case(scratch, nodes, waypoints, region_source, "node count");
    reject_case(scratch, std::string(1024 * 1024 + 1, ' '), waypoints, region_source, "1 MiB");
    reject_case(scratch, replace(scenario, "profile_id: world_config_test_v1", "profile_id: " + std::string(4097, 'a')), waypoints, region_source, "scalar");
    reject_case(scratch, scenario + '\xE9', waypoints, region_source, "UTF-8");
    reject_case(scratch, scenario + '\0', waypoints, region_source, "NUL");
    reject_case(scratch, replace(scenario, "profile_id: world_config_test_v1", "profile_id: \"invalid\\uD800\""));
    reject_case(scratch, replace(scenario, "speed: {knots:", "speed: {unexpected: 1, knots:"), waypoints, region_source, "unknown");
    reject_case(scratch, replace(scenario, "speed: {knots:", "speed: {hold_seconds: [1, 2], knots:"), waypoints, region_source, "duplicate");
}
void numeric_and_semantic_boundaries(const Scratch& scratch) {
    for (const auto& value : {"1e1", ".nan", ".inf", "+10", "0x10", "' 10'", "10.0000", "10.", ".5", "9223372036854776.000"})
        reject_case(scratch, replace(scenario, "knots: [10.001, 20.999]", std::string("knots: [") + value + ", 20.999]"));
    reject_case(scratch, replace(scenario, "knots: [10.001, 20.999]", "knots: [-1, 20.999]"));
    reject_case(scratch, replace(scenario, "knots: [10.001, 20.999]", "knots: [21, 20.999]"), waypoints, region_source, "reversed");
    reject_case(scratch, replace(scenario, "height_above_waterline_m: 18.125", "height_above_waterline_m: 18.1250"), waypoints, region_source, "precision");
    reject_case(scratch, replace(scenario, "radius_km: [0.000001, 60.123456]", "radius_km: [-1, 60.123456]"));
    reject_case(scratch, replace(scenario, "radius_km: [0.000001, 60.123456]", "radius_km: [0.0000001, 60.123456]"), waypoints, region_source, "precision");
    reject_case(scratch, replace(scenario, "duration_seconds: 600", "duration_seconds: -1"));
    reject_case(scratch, replace(scenario, "duration_seconds: 600", "duration_seconds: 0"));
    reject_case(scratch, replace(scenario, "duration_seconds: 600", "duration_seconds: 604801"));
    reject_case(scratch, replace(scenario, "initial_delay_seconds: [0, 0]", "initial_delay_seconds: [-1, 0]"));
    reject_case(scratch, replace(scenario, "online_duration_seconds: [2, 2]", "online_duration_seconds: [0, 2]"));
    reject_case(scratch, replace(scenario, "offline_duration_seconds: [1, 1]", "offline_duration_seconds: [0, 1]"));
    reject_case(scratch, replace(scenario, "hold_seconds: [100, 200]", "hold_seconds: [0, 200]"));
    reject_case(scratch, replace(scenario, "hold_seconds: [100, 200]", "hold_seconds: [100, 9223372036854775807]"), waypoints, region_source, "simulation time width");
    reject_case(scratch, replace(scenario, "online_duration_seconds: [2, 2]", "online_duration_seconds: [1.0, 2]"), waypoints, region_source, "integer");
    reject_case(scratch, replace(scenario, "online_duration_seconds: [2, 2]", "online_duration_seconds: [1, 2, 3]"));
    reject_case(scratch, replace(scenario, "value: 42", "value: 18446744073709551616"), waypoints, region_source, "overflow");
    reject_case(scratch, replace(scenario, "seed: {mode: fixed, value: 42}", "seed: {mode: fixed}"));
    reject_case(scratch, replace(scenario, "mode: fixed, value: 42", "mode: fresh, value: 42"));
    reject_case(scratch, replace(scenario, "id: 101", "id: -1"), waypoints, region_source, "integer");
    reject_case(scratch, replace(scenario, "id: 101", "id: 0"));
    reject_case(scratch, replace(scenario, "radio_id: 301", "radio_id: 4294967296"), waypoints, region_source, "overflow");
    reject_case(scratch, replace(scenario, "AT_ANCHOR", "NOT_UNDER_COMMAND"), waypoints, region_source, "navigation");
    reject_case(scratch, replace(scenario, "AT_ANCHOR", "UNDER_WAY_USING_ENGINE"), waypoints, region_source, "unique");
    reject_case(scratch, replace(scenario, "weight: 6", "weight: 0"), waypoints, region_source, "positive");
    reject_case(scratch, replace(scenario, "online_duration_seconds", "tx_online_duration_seconds"), waypoints, region_source, "unknown");
    reject_case(scratch, replace(scenario, "height_datum: unresolved", "height_datum: AMSL"), waypoints, region_source, "geoid");
    reject_case(scratch, replace(scenario, "height_datum: unresolved", "height_datum: WGS84_ellipsoid"), waypoints, region_source, "requires elevation");
    reject_case(scratch, replace(scenario, "height_datum: unresolved", "height_datum: unresolved\n        elevation_m: 0"), waypoints, region_source, "forbids");
    reject_case(scratch, scenario, waypoints, replace(region_source, "height_datum: AMSL", "height_datum: AGL"), "AGL");
    reject_case(scratch, scenario, waypoints, replace(region_source, "count: [1, 3]", "count: [0, 1]"));
    reject_case(scratch, scenario, waypoints, replace(region_source, "count: [1, 3]", "count: [1, 4097]"));
    reject_case(scratch, scenario, waypoints, replace(region_source, "count: [1, 3]", "count: [4096, 4096]"), "aggregate");
    const auto region_start = region_source.find("  - id: 11\n");
    require(region_start != std::string::npos, "region aggregate fixture pattern");
    auto second_region = replace(region_source.substr(region_start), "id: 11", "id: 12");
    second_region = replace(second_region, "name: Configuration region", "name: Second configuration region");
    const auto combined = replace(region_source, "count: [1, 3]", "count: [2048, 2048]") +
        replace(second_region, "count: [1, 3]", "count: [2048, 2048]");
    reject_case(scratch, scenario, waypoints, combined, "aggregate");
    reject_case(scratch, scenario, waypoints, replace(region_source, "radius_km: 20.000001", "radius_km: -1"));
    reject_case(scratch, scenario, waypoints, replace(region_source, "longitude_ndeg: 27910000000", "longitude_ndeg: 180000000001"));
    reject_case(scratch, scenario, waypoints, replace(region_source, "latitude_ndeg: 43210000000", "latitude_ndeg: 85000000000"));
}
void selections_catalogues_and_sizes(const Scratch& scratch) {
    reject_case(scratch, replace(scenario, "first_destination: {mode: fixed, waypoint_id: 2}", "first_destination: {mode: fixed, waypoint_id: 1}"));
    reject_case(scratch, replace(scenario, "initial: {mode: fixed, waypoint_id: 1}", "initial: {mode: subset, waypoint_ids: [2]}"));
    reject_case(scratch, replace(scenario, "initial: {mode: fixed, waypoint_id: 1}", "initial: {mode: subset, waypoint_ids: [2, 99, 1]}"));
    reject_case(scratch, replace(scenario, "initial: {mode: fixed, waypoint_id: 1}", "initial: {mode: subset, waypoint_ids: [2, 2, 3]}"));
    auto filtered = replace(scenario, "initial: {mode: fixed, waypoint_id: 1}", "initial: {mode: all}");
    // Keep waypoint 3 out of the later cycle so its rejected leg is an eligible initial -> fixed pair.
    filtered = replace(filtered, "subsequent_destination: {mode: subset, waypoint_ids: [1, 2, 3]}",
        "subsequent_destination: {mode: subset, waypoint_ids: [1, 2]}");
    const auto too_short = replace(waypoints,
        "id: 3, name: Burgas, longitude_ndeg: 27470000000, latitude_ndeg: 42500000000",
        "id: 3, name: Burgas, longitude_ndeg: 28170000001, latitude_ndeg: 43410000000");
    reject_case(scratch, filtered, too_short, region_source, "shorter than1m");
    const auto too_long = replace(waypoints,
        "id: 3, name: Burgas, longitude_ndeg: 27470000000, latitude_ndeg: 42500000000",
        "id: 3, name: Burgas, longitude_ndeg: 0, latitude_ndeg: 43410000000");
    reject_case(scratch, filtered, too_long, region_source, "exceeds qualified2000km");
    reject_case(scratch, replace(scenario, "first_destination: {mode: fixed, waypoint_id: 2}", "first_destination: {mode: subset, waypoint_ids: [1]}"));
    reject_case(scratch, replace(scenario, "subsequent_destination: {mode: subset, waypoint_ids: [1, 2, 3]}", "subsequent_destination: {mode: subset, waypoint_ids: [2]}"));
    reject_case(scratch, replace(scenario, "subsequent_destination: {mode: subset, waypoint_ids: [1, 2, 3]}", "subsequent_destination: {mode: fixed, waypoint_id: 1}"), waypoints, region_source, "forbidden");
    reject_case(scratch, replace(scenario, "waypoint_ids: [1, 2, 3]", "waypoint_ids: []"), waypoints, region_source, "nonempty");
    reject_case(scratch, replace(scenario, "waypoint_ids: [1, 2, 3]", "waypoint_ids: [1, 1]"));
    reject_case(scratch, replace(scenario, "waypoint_ids: [1, 2, 3]", "waypoint_ids: [1, 99]"));
    reject_case(scratch, replace(scenario, "initial: {mode: fixed, waypoint_id: 1}", "initial: {mode: all, waypoint_id: 1}"), waypoints, region_source, "forbids");
    reject_case(scratch, scenario, replace(waypoints, "coordinate_order: longitude_latitude", "coordinate_order: latitude_longitude"), region_source, "coordinate_order");
    auto ambiguous = waypoints;
    const auto closing = ambiguous.rfind("name: Varna"); require(closing != std::string::npos, "closing test pattern");
    ambiguous.replace(closing, std::string("name: Varna").size(), "name: Different");
    reject_case(scratch, scenario, ambiguous, region_source, "closing");
    reject_case(scratch, scenario, replace(waypoints, "id: 2, name: Balchik", "id: 1, name: Balchik"));
    reject_case(scratch, scenario, replace(waypoints, "id: 2, name: Balchik", "id: 2, name: Varna"));
    auto coincident = replace(waypoints, "longitude_ndeg: 28170000000", "longitude_ndeg: 27910000000");
    coincident = replace(coincident, "latitude_ndeg: 43410000000", "latitude_ndeg: 43210000000");
    reject_case(scratch, scenario, coincident);
    reject_case(scratch, scenario, replace(waypoints, "latitude_ndeg: 43410000000", "latitude_ndeg: 85000000000"));
    auto too_many_vessels = scenario.substr(0, scenario.find("vessels:\n")) + "vessels:\n";
    for (std::size_t index = 0; index <= kMaxVessels; ++index) too_many_vessels += "  - {}\n";
    reject_case(scratch, too_many_vessels, waypoints, region_source, "bounded sequence");
    auto too_many_points = waypoints.substr(0, waypoints.find("waypoints:\n")) + "waypoints:\n";
    for (std::size_t index = 0; index <= kMaxWaypoints + 1; ++index) too_many_points += "  - {}\n";
    reject_case(scratch, scenario, too_many_points, region_source, "bounded sequence");
    auto too_many_regions = region_source.substr(0, region_source.find("regions:\n")) + "regions:\n";
    for (std::size_t index = 0; index <= kMaxRegions; ++index) too_many_regions += "  - {}\n";
    reject_case(scratch, scenario, waypoints, too_many_regions, "bounded sequence");
}
}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 3, "usage: world_config_tests PROFILE_DIRECTORY WORK_DIRECTORY");
        const Scratch scratch(fs::u8path(argv[2]));
        positive_contracts(scratch, fs::u8path(argv[1])); yaml_boundaries(scratch);
        numeric_and_semantic_boundaries(scratch); selections_catalogues_and_sizes(scratch);
        std::cout << "world configuration: exact units, selections, source datums/closure, Unicode/hash reproducibility and "
                  << rejected << " strict invalid cases passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "world config test failed: " << error.what() << '\n'; return 1;
    }
}
