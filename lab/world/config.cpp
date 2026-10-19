#include "config.h"

#include <yaml-cpp/yaml.h>
#include <yaml-cpp/eventhandler.h>
#include <yaml-cpp/parser.h>

#include <algorithm>
#include <charconv>
#include <fstream>
#include <initializer_list>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <type_traits>
#include <utility>

namespace aismixer::lab::world {
namespace {
constexpr std::size_t kMaxYamlBytes = 1024 * 1024;
constexpr std::size_t kMaxYamlDepth = 32, kMaxYamlNodes = 100000, kMaxScalarBytes = 4096;
using Mapping = std::map<std::string, YAML::Node>;

[[noreturn]] void error_at(const std::string& path, const YAML::Mark& mark,
                         std::string_view field, std::string_view reason) {
    std::string location = path;
    if (mark.line >= 0 && mark.column >= 0)
        location += ':' + std::to_string(mark.line + 1) + ':' + std::to_string(mark.column + 1);
    throw std::invalid_argument(location + ": " + std::string(field) + ": " + std::string(reason));
}
void validate_utf8(std::string_view value, const std::string& path) {
    for (std::size_t index = 0; index < value.size();) {
        const auto lead = static_cast<unsigned char>(value[index]);
        const std::size_t width = lead < 0x80 ? 1 : lead >= 0xc2 && lead <= 0xdf ? 2 :
            lead >= 0xe0 && lead <= 0xef ? 3 : lead >= 0xf0 && lead <= 0xf4 ? 4 : 0;
        bool valid = width != 0 && width <= value.size() - index && lead != 0;
        for (std::size_t offset = 1; valid && offset < width; ++offset) {
            const auto byte = static_cast<unsigned char>(value[index + offset]);
            valid = byte >= 0x80 && byte <= 0xbf;
        }
        if (valid && width >= 3) {
            const auto second = static_cast<unsigned char>(value[index + 1]);
            valid = !(lead == 0xe0 && second < 0xa0) && !(lead == 0xed && second > 0x9f) &&
                    !(lead == 0xf0 && second < 0x90) && !(lead == 0xf4 && second > 0x8f);
        }
        if (!valid) throw std::invalid_argument(path + ": invalid UTF-8/NUL at byte " + std::to_string(index + 1));
        index += width;
    }
}

// Reject graph expansion and excessive nesting while yaml-cpp is still
// producing parser events, before constructing a YAML::Node graph.
class Preflight final : public YAML::EventHandler {
public:
    explicit Preflight(std::string path) : path_(std::move(path)) {}
    void OnDocumentStart(const YAML::Mark& mark) override {
        if (++documents_ != 1) fail(mark, "exactly one YAML document required");
    }
    void OnDocumentEnd() override {}
    void OnNull(const YAML::Mark& mark, YAML::anchor_t) override { fail(mark, "null values unsupported"); }
    void OnAlias(const YAML::Mark& mark, YAML::anchor_t) override { fail(mark, "YAML aliases unsupported"); }
    void OnAnchor(const YAML::Mark& mark, const std::string&) override { fail(mark, "YAML anchors unsupported"); }
    void OnScalar(const YAML::Mark& mark, const std::string& tag, YAML::anchor_t anchor,
                  const std::string& value) override {
        inspect(mark, tag, anchor);
        if (value.size() > kMaxScalarBytes) fail(mark, "scalar exceeds 4096 bytes");
        // Escapes are decoded by yaml-cpp; validate their resulting bytes too.
        validate_utf8(value, path_);
    }
    void OnSequenceStart(const YAML::Mark& mark, const std::string& tag, YAML::anchor_t anchor,
                         YAML::EmitterStyle::value) override { begin(mark, tag, anchor); }
    void OnSequenceEnd() override { --depth_; }
    void OnMapStart(const YAML::Mark& mark, const std::string& tag, YAML::anchor_t anchor,
                    YAML::EmitterStyle::value) override { begin(mark, tag, anchor); }
    void OnMapEnd() override { --depth_; }
private:
    [[noreturn]] void fail(const YAML::Mark& mark, const char* reason) const { error_at(path_, mark, "YAML", reason); }
    void inspect(const YAML::Mark& mark, const std::string& tag, YAML::anchor_t anchor) {
        if (anchor != 0) fail(mark, "YAML anchors unsupported");
        if (!tag.empty() && tag != "?" && tag != "!") fail(mark, "explicit/custom YAML tags unsupported");
        if (++nodes_ > kMaxYamlNodes) fail(mark, "YAML node count exceeds 100000");
    }
    void begin(const YAML::Mark& mark, const std::string& tag, YAML::anchor_t anchor) {
        inspect(mark, tag, anchor);
        if (++depth_ > kMaxYamlDepth) fail(mark, "YAML nesting exceeds 32 collections");
    }
    std::string path_;
    std::size_t documents_ = 0, nodes_ = 0, depth_ = 0;
};

struct Source { std::filesystem::path path; YAML::Node root; std::uint64_t hash = 0; };
Source read_source(const std::filesystem::path& requested) {
    std::error_code filesystem_error;
    const auto path = std::filesystem::absolute(requested, filesystem_error).lexically_normal();
    if (requested.empty() || filesystem_error) throw std::invalid_argument("world YAML path is empty or cannot be resolved");
    const auto display = path.u8string();
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error(display + ": cannot open world YAML input");
    std::string bytes(kMaxYamlBytes + 1, '\0');
    input.read(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    const auto size = static_cast<std::size_t>(input.gcount());
    if (input.bad() || (input.fail() && !input.eof())) throw std::runtime_error(display + ": world YAML read failed");
    if (size > kMaxYamlBytes) throw std::invalid_argument(display + ": world YAML exceeds 1 MiB limit");
    bytes.resize(size); input.clear(); input.close();
    if (input.fail()) throw std::runtime_error(display + ": world YAML close failed");
    validate_utf8(bytes, display);
    std::string normalized;
    normalized.reserve(bytes.size());
    for (std::size_t index = 0; index < bytes.size(); ++index) {
        if (bytes[index] == '\r') {
            normalized.push_back('\n');
            if (index + 1 < bytes.size() && bytes[index + 1] == '\n') ++index;
        } else normalized.push_back(bytes[index]);
    }
    auto hash = UINT64_C(14695981039346656037);
    for (const unsigned char byte : normalized) { hash ^= byte; hash *= UINT64_C(1099511628211); }
    try {
        std::istringstream stream(normalized);
        YAML::Parser parser(stream); Preflight preflight(display);
        if (!parser.HandleNextDocument(preflight)) throw std::invalid_argument(display + ": exactly one YAML document required");
        if (parser.HandleNextDocument(preflight)) throw std::invalid_argument(display + ": exactly one YAML document required");
        return {path, YAML::Load(normalized), hash};
    } catch (const YAML::Exception& error) { error_at(display, error.mark, "YAML", error.msg); }
}

class Reader {
public:
    explicit Reader(const Source& source) : path_(source.path.u8string()) {}
    [[noreturn]] void fail(const YAML::Node& node, std::string_view field, std::string_view reason) const {
        error_at(path_, node.Mark(), field, reason);
    }
    Mapping mapping(const YAML::Node& node, const std::string& field,
                    std::initializer_list<std::string_view> required,
                    std::initializer_list<std::string_view> optional = {}) const {
        if (!node.IsMap()) fail(node, field, "expected mapping");
        Mapping result;
        for (const auto& entry : node) {
            if (!entry.first.IsScalar()) fail(entry.first, field, "scalar keys required");
            const auto key = entry.first.Scalar();
            if (std::find(required.begin(), required.end(), key) == required.end() &&
                std::find(optional.begin(), optional.end(), key) == optional.end()) fail(entry.first, field + '.' + key, "unknown field");
            if (!result.emplace(key, entry.second).second) fail(entry.first, field + '.' + key, "duplicate key");
        }
        for (const auto key : required) if (!result.count(std::string(key))) fail(node, field + '.' + std::string(key), "required field missing");
        return result;
    }
    std::string scalar(const YAML::Node& node, const std::string& field) const {
        if (!node.IsScalar()) fail(node, field, "expected scalar");
        return node.Scalar();
    }
    std::string human(const YAML::Node& node, const std::string& field, std::size_t maximum = 4096) const {
        const auto value = scalar(node, field);
        if (value.empty() || value.size() > maximum) fail(node, field, "invalid bounded text length");
        for (const unsigned char byte : value) if (byte < 32 || byte == 127) fail(node, field, "control characters unsupported");
        return value;
    }
    YAML::Node sequence(const YAML::Node& node, const std::string& field, std::size_t maximum) const {
        if (!node.IsSequence() || node.size() > maximum) fail(node, field, "expected bounded sequence (maximum " + std::to_string(maximum) + ')');
        return node;
    }
    template<class Integer> Integer integer(const YAML::Node& node, const std::string& field) const {
        const auto value = scalar(node, field);
        const std::size_t first = std::is_signed_v<Integer> && !value.empty() && value.front() == '-' ? 1 : 0;
        if (first == value.size()) fail(node, field, "expected ASCII base-10 integer");
        for (std::size_t index = first; index < value.size(); ++index)
            if (value[index] < '0' || value[index] > '9') fail(node, field, "expected ASCII base-10 integer without plus, fraction or exponent");
        Integer result{};
        const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
        if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size()) fail(node, field, "integer overflow");
        return result;
    }
    std::int64_t decimal(const YAML::Node& node, const std::string& field, std::uint64_t scale) const {
        const auto value = scalar(node, field);
        const bool negative = !value.empty() && value.front() == '-';
        std::size_t at = negative ? 1 : 0;
        const auto start = at;
        const auto limit = static_cast<std::uint64_t>(INT64_MAX) + (negative ? 1u : 0u);
        std::uint64_t whole = 0, fraction = 0;
        const auto digit = [&](char byte) { return byte >= '0' && byte <= '9'; };
        while (at < value.size() && digit(value[at])) {
            const auto number = static_cast<unsigned>(value[at++] - '0');
            if (whole > (limit - number) / 10) fail(node, field, "scaled decimal overflow");
            whole = whole * 10 + number;
        }
        if (at == start) fail(node, field, "decimal requires an ASCII integer part");
        std::uint64_t place = scale;
        if (at < value.size() && value[at] == '.') {
            ++at; const auto fraction_start = at;
            while (at < value.size() && digit(value[at])) {
                if (place == 1) fail(node, field, "decimal exceeds exact unit precision");
                place /= 10; fraction += static_cast<unsigned>(value[at++] - '0') * place;
            }
            if (at == fraction_start) fail(node, field, "decimal fraction requires digits");
        }
        if (at != value.size()) fail(node, field, "decimal forbids plus, whitespace, exponent and nonfinite values");
        if (whole > limit / scale || fraction > limit - whole * scale) fail(node, field, "scaled decimal overflow");
        const auto magnitude = whole * scale + fraction;
        if (negative && magnitude == UINT64_C(0x8000000000000000)) return INT64_MIN;
        const auto result = static_cast<std::int64_t>(magnitude);
        return negative ? -result : result;
    }
    void exact(const YAML::Node& node, const std::string& field, std::string_view expected) const {
        if (scalar(node, field) != expected) fail(node, field, "requires " + std::string(expected));
    }
    void version(const Mapping& map, const std::string& field, std::string_view kind) const {
        if (integer<std::uint32_t>(map.at("schema_version"), field + ".schema_version") != 1) fail(map.at("schema_version"), field, "only schema version 1 supported");
        exact(map.at("kind"), field + ".kind", kind);
        (void)human(map.at("profile_id"), field + ".profile_id", 64);
        (void)human(map.at("provenance"), field + ".provenance");
    }
    void coordinates(const Mapping& map, const std::string& field) const {
        exact(map.at("coordinate_units"), field + ".coordinate_units", "nanodegrees");
        exact(map.at("coordinate_order"), field + ".coordinate_order", "longitude_latitude");
        exact(map.at("datum"), field + ".datum", "WGS84");
    }
    Point point(const Mapping& map, const std::string& field) const {
        return {integer<std::int64_t>(map.at("longitude_ndeg"), field + ".longitude_ndeg"),
                integer<std::int64_t>(map.at("latitude_ndeg"), field + ".latitude_ndeg")};
    }
    Interval interval(const YAML::Node& node, const std::string& field, std::uint64_t scale = 0) const {
        const auto list = sequence(node, field, 2);
        if (list.size() != 2) fail(node, field, "interval requires exactly two bounds");
        Interval result{scale ? decimal(list[0], field + "[0]", scale) : integer<std::int64_t>(list[0], field + "[0]"),
                        scale ? decimal(list[1], field + "[1]", scale) : integer<std::int64_t>(list[1], field + "[1]")};
        if (result.minimum > result.maximum) fail(node, field, "reversed interval");
        return result;
    }
    Interval seconds(const YAML::Node& node, const std::string& field, bool zero = false) const {
        const auto result = interval(node, field);
        if (result.minimum < (zero ? 0 : 1)) fail(node, field, zero ? "seconds must be nonnegative" : "repeating seconds must be positive");
        try { (void)runtime::from_seconds(result.maximum); }
        catch (const std::overflow_error&) { fail(node, field, "seconds exceed simulation time width"); }
        return result;
    }
    PowerModel power(const YAML::Node& node, const std::string& field) const {
        const auto map = mapping(node, field, {"initial_delay_seconds", "online_duration_seconds", "offline_duration_seconds"});
        return {seconds(map.at("initial_delay_seconds"), field + ".initial_delay_seconds", true),
                seconds(map.at("online_duration_seconds"), field + ".online_duration_seconds"),
                seconds(map.at("offline_duration_seconds"), field + ".offline_duration_seconds")};
    }
    Selection selection(const YAML::Node& node, const std::string& field, bool subsequent = false) const {
        const auto map = mapping(node, field, {"mode"}, {"waypoint_id", "waypoint_ids"});
        const auto mode = scalar(map.at("mode"), field + ".mode");
        Selection result;
        if (mode == "fixed") {
            if (subsequent || !map.count("waypoint_id") || map.count("waypoint_ids")) fail(node, field, "fixed requires only waypoint_id and is forbidden for subsequent destinations");
            result.mode = SelectionMode::Fixed; result.waypoint_id = {integer<std::uint32_t>(map.at("waypoint_id"), field + ".waypoint_id")};
        } else if (mode == "all") {
            if (map.size() != 1) fail(node, field, "all mode forbids fixed/subset fields");
            result.mode = SelectionMode::All;
        } else if (mode == "subset") {
            if (!map.count("waypoint_ids") || map.count("waypoint_id")) fail(node, field, "subset requires only waypoint_ids");
            result.mode = SelectionMode::Subset;
            const auto ids = sequence(map.at("waypoint_ids"), field + ".waypoint_ids", kMaxWaypoints);
            if (ids.size() == 0) fail(node, field, "subset must be nonempty");
            for (std::size_t index = 0; index < ids.size(); ++index) result.waypoint_ids.push_back({integer<std::uint32_t>(ids[index], field + ".waypoint_ids[" + std::to_string(index) + ']')});
        } else fail(node, field, "unsupported selection mode");
        return result;
    }
    Vessel vessel(const YAML::Node& node, const std::string& field) const {
        const auto map = mapping(node, field, {"id", "equipment_id", "radio_id", "name", "route", "speed", "navigation",
            "restricted_speed_cap_knots", "power", "antenna", "reach"});
        Vessel result;
        result.id = {integer<std::uint32_t>(map.at("id"), field + ".id")};
        result.equipment_id = {integer<std::uint32_t>(map.at("equipment_id"), field + ".equipment_id")};
        result.radio_id = {integer<std::uint32_t>(map.at("radio_id"), field + ".radio_id")};
        result.name = human(map.at("name"), field + ".name", 128);
        const auto route = mapping(map.at("route"), field + ".route", {"initial", "first_destination", "subsequent_destination"});
        result.initial = selection(route.at("initial"), field + ".route.initial");
        result.first_destination = selection(route.at("first_destination"), field + ".route.first_destination");
        result.subsequent_destination = selection(route.at("subsequent_destination"), field + ".route.subsequent_destination", true);
        const auto speed = mapping(map.at("speed"), field + ".speed", {"knots", "hold_seconds"});
        result.speed_mkn = interval(speed.at("knots"), field + ".speed.knots", 1000);
        result.speed_hold_seconds = seconds(speed.at("hold_seconds"), field + ".speed.hold_seconds");
        result.restricted_speed_cap_mkn = decimal(map.at("restricted_speed_cap_knots"), field + ".restricted_speed_cap_knots", 1000);
        const auto navigation = sequence(map.at("navigation"), field + ".navigation", 3);
        result.navigation.clear();
        if (navigation.size() == 0) fail(map.at("navigation"), field + ".navigation", "at least one enabled navigation mode required");
        std::set<NavigationStatus> statuses;
        for (std::size_t index = 0; index < navigation.size(); ++index) {
            const auto prefix = field + ".navigation[" + std::to_string(index) + ']';
            const auto entry = mapping(navigation[index], prefix, {"status", "weight", "duration_seconds"});
            const auto mode = scalar(entry.at("status"), prefix + ".status");
            NavigationStatus status;
            if (mode == "UNDER_WAY_USING_ENGINE") status = NavigationStatus::UnderWayUsingEngine;
            else if (mode == "AT_ANCHOR") status = NavigationStatus::AtAnchor;
            else if (mode == "RESTRICTED_MANOEUVRABILITY") status = NavigationStatus::RestrictedManoeuvrability;
            else fail(entry.at("status"), prefix + ".status", "unsupported navigation mode");
            const auto weight = integer<std::uint32_t>(entry.at("weight"), prefix + ".weight");
            if (weight == 0 || !statuses.insert(status).second) fail(navigation[index], prefix, "weights must be positive and statuses unique");
            result.navigation.push_back({status, weight, seconds(entry.at("duration_seconds"), prefix + ".duration_seconds")});
        }
        result.power = power(map.at("power"), field + ".power");
        const auto antenna = mapping(map.at("antenna"), field + ".antenna", {"height_above_waterline_m", "waterline"});
        result.antenna_offset_mm = decimal(antenna.at("height_above_waterline_m"), field + ".antenna.height_above_waterline_m", 1000);
        const auto waterline = mapping(antenna.at("waterline"), field + ".antenna.waterline", {"height_datum", "provenance"}, {"elevation_m"});
        const auto datum = scalar(waterline.at("height_datum"), field + ".antenna.waterline.height_datum");
        result.waterline_provenance = human(waterline.at("provenance"), field + ".antenna.waterline.provenance");
        if (datum == "unresolved") {
            if (waterline.count("elevation_m")) fail(antenna.at("waterline"), field + ".antenna.waterline", "unresolved datum forbids an ellipsoidal elevation");
        } else if (datum == "WGS84_ellipsoid") {
            if (!waterline.count("elevation_m")) fail(antenna.at("waterline"), field + ".antenna.waterline", "explicit ellipsoid assumption requires elevation_m");
            result.waterline_ellipsoid_mm = decimal(waterline.at("elevation_m"), field + ".antenna.waterline.elevation_m", 1000);
        } else fail(waterline.at("height_datum"), field + ".antenna.waterline.height_datum", "requires unresolved or WGS84_ellipsoid; no geoid conversion is available");
        const auto reach = mapping(map.at("reach"), field + ".reach", {"radius_km", "hold_seconds"});
        result.reach_radius_mm = interval(reach.at("radius_km"), field + ".reach.radius_km", 1000000);
        result.reach_hold_seconds = seconds(reach.at("hold_seconds"), field + ".reach.hold_seconds");
        return result;
    }
    Region region(const YAML::Node& node, const std::string& field) const {
        const auto map = mapping(node, field, {"id", "name", "center", "radius_km", "count", "antenna_elevation_m", "height_datum", "provenance", "power"});
        Region result;
        result.id = {integer<std::uint32_t>(map.at("id"), field + ".id")}; result.name = human(map.at("name"), field + ".name", 128);
        const auto center = mapping(map.at("center"), field + ".center", {"longitude_ndeg", "latitude_ndeg"});
        result.center = point(center, field + ".center");
        result.radius_mm = decimal(map.at("radius_km"), field + ".radius_km", 1000000);
        result.count = interval(map.at("count"), field + ".count");
        result.elevation_mm = interval(map.at("antenna_elevation_m"), field + ".antenna_elevation_m", 1000);
        const auto datum = scalar(map.at("height_datum"), field + ".height_datum");
        if (datum == "AMSL") result.height_datum = HeightDatum::Amsl;
        else if (datum == "WGS84_ellipsoid") result.height_datum = HeightDatum::Wgs84Ellipsoid;
        else fail(map.at("height_datum"), field + ".height_datum", "requires AMSL or WGS84_ellipsoid; AGL conversion unsupported");
        result.provenance = human(map.at("provenance"), field + ".provenance");
        result.power = power(map.at("power"), field + ".power"); return result;
    }
private:
    std::string path_;
};

std::vector<Waypoint> catalogue(const Source& source) {
    Reader reader(source);
    const auto top = reader.mapping(source.root, "catalogue", {"schema_version", "kind", "profile_id", "provenance",
        "coordinate_units", "coordinate_order", "datum", "ring_closure", "waypoints"});
    reader.version(top, "catalogue", "world_waypoint_catalog"); reader.coordinates(top, "catalogue");
    reader.exact(top.at("ring_closure"), "catalogue.ring_closure", "explicit");
    const auto points = reader.sequence(top.at("waypoints"), "catalogue.waypoints", kMaxWaypoints + 1);
    if (points.size() < 3) reader.fail(top.at("waypoints"), "catalogue.waypoints", "requires at least two distinct waypoints plus explicit closure");
    std::vector<Waypoint> result;
    for (std::size_t index = 0; index < points.size(); ++index) {
        const auto field = "catalogue.waypoints[" + std::to_string(index) + ']';
        const auto point = reader.mapping(points[index], field, {"id", "name", "longitude_ndeg", "latitude_ndeg"});
        result.push_back({{reader.integer<std::uint32_t>(point.at("id"), field + ".id")},
            reader.human(point.at("name"), field + ".name", 128), reader.point(point, field)});
    }
    const auto& first = result.front(); const auto& last = result.back();
    if (first.id != last.id || first.name != last.name || first.location.longitude_ndeg != last.location.longitude_ndeg ||
        first.location.latitude_ndeg != last.location.latitude_ndeg)
        reader.fail(points[points.size() - 1], "catalogue.waypoints", "closing waypoint must exactly repeat first ID, name and coordinates");
    result.pop_back();
    return result;
}
std::vector<Region> regions(const Source& source) {
    Reader reader(source);
    const auto top = reader.mapping(source.root, "regions", {"schema_version", "kind", "profile_id", "provenance",
        "coordinate_units", "coordinate_order", "datum", "regions"});
    reader.version(top, "regions", "world_receiver_regions"); reader.coordinates(top, "regions");
    const auto entries = reader.sequence(top.at("regions"), "regions.regions", kMaxRegions);
    std::vector<Region> result;
    for (std::size_t index = 0; index < entries.size(); ++index)
        result.push_back(reader.region(entries[index], "regions.regions[" + std::to_string(index) + ']'));
    return result;
}
std::filesystem::path reference_path(const Source& scenario, Reader& reader, const YAML::Node& node, const std::string& field) {
    const auto name = reader.human(node, field);
    const auto path = std::filesystem::u8path(name);
    return path.is_absolute() ? path : scenario.path.parent_path() / path;
}
}  // namespace

Config load_world(const std::filesystem::path& path) {
    const auto scenario = read_source(path); Reader reader(scenario);
    const auto top = reader.mapping(scenario.root, "world", {"schema_version", "kind", "profile_id", "provenance",
        "duration_seconds", "seed", "waypoint_catalog", "receiver_regions", "vessels"});
    reader.version(top, "world", "world_scenario");
    Config config;
    config.profile_id = reader.human(top.at("profile_id"), "world.profile_id", 64);
    config.provenance = reader.human(top.at("provenance"), "world.provenance");
    const auto duration = reader.integer<std::int64_t>(top.at("duration_seconds"), "world.duration_seconds");
    if (duration < 1 || duration > kMaxScenarioSeconds) reader.fail(top.at("duration_seconds"), "world.duration_seconds", "requires 1..604800 whole seconds");
    config.duration = runtime::from_seconds(duration);
    const auto seed = reader.mapping(top.at("seed"), "world.seed", {"mode"}, {"value"});
    const auto mode = reader.scalar(seed.at("mode"), "world.seed.mode");
    if (mode == "fixed") {
        if (!seed.count("value")) reader.fail(top.at("seed"), "world.seed", "fixed seed requires value");
        config.effective_seed = reader.integer<std::uint64_t>(seed.at("value"), "world.seed.value");
    } else if (mode == "fresh") {
        if (seed.count("value")) reader.fail(top.at("seed"), "world.seed", "fresh seed forbids a configured value");
        config.fresh_seed = true; config.effective_seed = 0;  // Selected and recorded by the world session, never the parser.
    } else reader.fail(seed.at("mode"), "world.seed.mode", "requires fixed or fresh");
    const auto waypoint_source = read_source(reference_path(scenario, reader, top.at("waypoint_catalog"), "world.waypoint_catalog"));
    const auto region_source = read_source(reference_path(scenario, reader, top.at("receiver_regions"), "world.receiver_regions"));
    config.waypoints = catalogue(waypoint_source); config.regions = regions(region_source);
    const auto vessels = reader.sequence(top.at("vessels"), "world.vessels", kMaxVessels);
    for (std::size_t index = 0; index < vessels.size(); ++index)
        config.vessels.push_back(reader.vessel(vessels[index], "world.vessels[" + std::to_string(index) + ']'));
    config.scenario_hash = scenario.hash; config.waypoint_hash = waypoint_source.hash; config.regions_hash = region_source.hash;
    try { return canonical_config(std::move(config)); }
    catch (const std::invalid_argument& error) { reader.fail(scenario.root, "world validation", error.what()); }
    catch (const std::overflow_error& error) { reader.fail(scenario.root, "world validation", error.what()); }
}
}  // namespace aismixer::lab::world
