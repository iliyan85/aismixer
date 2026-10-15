#include "geo_config.h"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <charconv>
#include <fstream>
#include <initializer_list>
#include <limits>
#include <map>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
#include <utility>

namespace aismixer::lab::geo {
namespace {

constexpr std::size_t kMaxYamlBytes = 1024 * 1024;

void validate_utf8(std::string_view content, const std::string& path) {
    std::size_t index = 0;
    while (index < content.size()) {
        const auto first = static_cast<unsigned char>(content[index]);
        const std::size_t width = first <= 0x7f ? 1 :
            first >= 0xc2 && first <= 0xdf ? 2 :
            first >= 0xe0 && first <= 0xef ? 3 :
            first >= 0xf0 && first <= 0xf4 ? 4 : 0;
        bool valid = width != 0 && width <= content.size() - index;
        for (std::size_t offset = 1; valid && offset < width; ++offset) {
            const auto byte = static_cast<unsigned char>(content[index + offset]);
            valid = byte >= 0x80 && byte <= 0xbf;
        }
        if (valid && width >= 3) {
            const auto second = static_cast<unsigned char>(content[index + 1]);
            valid = !(first == 0xe0 && second < 0xa0) &&
                    !(first == 0xed && second > 0x9f) &&
                    !(first == 0xf0 && second < 0x90) &&
                    !(first == 0xf4 && second > 0x8f);
        }
        if (!valid) throw std::invalid_argument(path + ": invalid UTF-8 at byte " + std::to_string(index + 1));
        index += width;
    }
}

std::string read_file(const std::filesystem::path& path) {
    const auto display = path.u8string();
    std::ifstream input(path, std::ios::binary);
    if (!input.is_open()) throw std::runtime_error(display + ": cannot open geospatial YAML input");
    std::string content(kMaxYamlBytes + 1, '\0');
    input.read(content.data(), static_cast<std::streamsize>(content.size()));
    const auto count = static_cast<std::size_t>(input.gcount());
    if (input.bad() || (input.fail() && !input.eof())) {
        throw std::runtime_error(display + ": geospatial YAML read failed");
    }
    if (count > kMaxYamlBytes) throw std::invalid_argument(display + ": YAML exceeds 1 MiB limit");
    content.resize(count);
    input.clear();
    input.close();
    if (input.fail()) throw std::runtime_error(display + ": geospatial YAML close failed");
    validate_utf8(content, display);
    return content;
}

class Reader {
public:
    explicit Reader(std::string path) : path_(std::move(path)) {}

    Domain domain(const YAML::Node& root) {
        const auto top = mapping(root, "domain", {"schema_version", "profile_id", "provenance",
            "coordinate_units", "coordinate_order", "datum", "ring_closure", "boundary", "ring"});
        metadata(top, "domain");
        exact(top.at("ring_closure"), "domain.ring_closure", "explicit");
        exact(top.at("boundary"), "domain.boundary", "inclusive");
        Domain result;
        result.schema_version = 1;
        result.profile_id = scalar(top.at("profile_id"), "domain.profile_id");
        result.provenance = scalar(top.at("provenance"), "domain.provenance");
        const auto ring = sequence(top.at("ring"), "domain.ring", kMaxPolygonVertices + 1);
        result.ring.reserve(ring.size());
        for (std::size_t index = 0; index < ring.size(); ++index) {
            const auto field = "domain.ring[" + std::to_string(index) + "]";
            const auto entry = mapping(ring[index], field, {"longitude_ndeg", "latitude_ndeg"}, {"label"});
            if (entry.find("label") != entry.end()) {
                const auto label = scalar(entry.at("label"), field + ".label");
                if (label.empty() || label.size() > 128) fail(entry.at("label"), field + ".label", "requires 1..128 UTF-8 bytes");
                for (const unsigned char byte : label) {
                    if (byte < 32 || byte == 127) fail(entry.at("label"), field + ".label", "control characters unsupported");
                }
            }
            result.ring.push_back(point(entry, field));
        }
        validate_at(root, [&] { validate_domain(result); });
        return result;
    }

    std::vector<Anchor> overlay(const YAML::Node& root) {
        const auto top = mapping(root, "overlay", {"schema_version", "coordinate_units", "coordinate_order",
            "datum", "projection", "anchor_reference", "anchors"});
        metadata(top, "overlay");
        exact(top.at("projection"), "overlay.projection", "wgs84_tangent_linear_v1");
        exact(top.at("anchor_reference"), "overlay.anchor_reference", "actor_tick_zero");
        const auto anchors = sequence(top.at("anchors"), "overlay.anchors", kMaxSceneActors);
        std::vector<Anchor> result;
        result.reserve(anchors.size());
        std::vector<Actor> identities;
        identities.reserve(anchors.size());
        for (std::size_t index = 0; index < anchors.size(); ++index) {
            const auto field = "overlay.anchors[" + std::to_string(index) + "]";
            const auto entry = mapping(anchors[index], field, {"actor_id", "longitude_ndeg", "latitude_ndeg"});
            const auto actor_id = number<std::uint32_t>(entry.at("actor_id"), field + ".actor_id");
            result.push_back({actor_id, point(entry, field)});
            Actor identity{};
            identity.actor_id = actor_id;
            identities.push_back(identity);
        }
        // Actor association is validated again against the real scene at the
        // Session boundary. Here only coordinate/identity safety is checked.
        validate_at(root, [&] { validate_anchors(identities, result); });
        return result;
    }

    std::vector<Receiver> receivers(const YAML::Node& root) {
        const auto top = mapping(root, "catalog", {"schema_version", "coordinate_units", "coordinate_order",
            "datum", "height_datum", "receivers"});
        metadata(top, "catalog");
        exact(top.at("height_datum"), "catalog.height_datum", "WGS84_ellipsoid");
        const auto entries = sequence(top.at("receivers"), "catalog.receivers", kMaxReceivers);
        std::vector<Receiver> result;
        result.reserve(entries.size());
        for (std::size_t index = 0; index < entries.size(); ++index) {
            const auto field = "catalog.receivers[" + std::to_string(index) + "]";
            const auto entry = mapping(entries[index], field,
                {"receiver_id", "longitude_ndeg", "latitude_ndeg", "antenna_height_mm"});
            result.push_back({number<std::uint32_t>(entry.at("receiver_id"), field + ".receiver_id"),
                point(entry, field), number<std::int64_t>(entry.at("antenna_height_mm"), field + ".antenna_height_mm"),
                "WGS84_ellipsoid"});
        }
        validate_at(root, [&] { validate_receivers(result); });
        return result;
    }

private:
    using Mapping = std::map<std::string, YAML::Node>;

    [[noreturn]] void fail(const YAML::Node& node, std::string_view field, std::string_view reason) const {
        const auto mark = node.Mark();
        std::string location = path_;
        if (mark.line >= 0 && mark.column >= 0) {
            location += ":" + std::to_string(mark.line + 1) + ":" + std::to_string(mark.column + 1);
        }
        throw std::invalid_argument(location + ": " + std::string(field) + ": " + std::string(reason));
    }

    void inspect(const YAML::Node& node, const std::string& field) {
        if (!node.IsDefined()) fail(node, field, "missing node");
        const std::string tag = node.Tag();
        if (tag != "?" && tag != "!") fail(node, field, "explicit/custom YAML tags unsupported");
        const auto mark = node.Mark();
        if (mark.pos >= 0) {
            auto& entries = visited_[mark.pos];
            for (const auto& prior : entries) {
                if (prior.is(node)) fail(node, field, "YAML aliases unsupported");
            }
            entries.push_back(node);
        }
    }

    Mapping mapping(const YAML::Node& node, const std::string& field,
                    std::initializer_list<std::string_view> required,
                    std::initializer_list<std::string_view> optional = {}) {
        inspect(node, field);
        if (!node.IsMap()) fail(node, field, "expected mapping");
        Mapping result;
        for (const auto& entry : node) {
            inspect(entry.first, field + " key");
            if (!entry.first.IsScalar()) fail(entry.first, field, "scalar field names required");
            const auto key = entry.first.Scalar();
            if (std::find(required.begin(), required.end(), key) == required.end() &&
                std::find(optional.begin(), optional.end(), key) == optional.end()) {
                fail(entry.first, field + "." + key, "unknown field");
            }
            if (!result.emplace(key, entry.second).second) fail(entry.first, field + "." + key, "duplicate key");
        }
        for (const auto key : required) {
            if (result.find(std::string(key)) == result.end()) fail(node, field + "." + std::string(key), "required field missing");
        }
        return result;
    }

    std::string scalar(const YAML::Node& node, const std::string& field) {
        inspect(node, field);
        if (!node.IsScalar()) fail(node, field, "expected scalar");
        return node.Scalar();
    }

    YAML::Node sequence(const YAML::Node& node, const std::string& field, std::size_t maximum) {
        inspect(node, field);
        if (!node.IsSequence()) fail(node, field, "expected sequence");
        if (node.size() > maximum) fail(node, field, "sequence exceeds resource bound " + std::to_string(maximum));
        return node;
    }

    template <typename Integer>
    Integer number(const YAML::Node& node, const std::string& field) {
        const auto value = scalar(node, field);
        const std::size_t first = std::numeric_limits<Integer>::is_signed && !value.empty() && value[0] == '-' ? 1 : 0;
        if (value.size() == first) fail(node, field, "expected ASCII base-10 integer");
        for (std::size_t index = first; index < value.size(); ++index) {
            if (value[index] < '0' || value[index] > '9') fail(node, field, "expected ASCII base-10 integer without plus/whitespace/fraction");
        }
        Integer result{};
        const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result, 10);
        if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size()) {
            fail(node, field, "integer exceeds its required signed/unsigned width");
        }
        return result;
    }

    void exact(const YAML::Node& node, const std::string& field, std::string_view expected) {
        if (scalar(node, field) != expected) fail(node, field, "requires " + std::string(expected));
    }

    void metadata(const Mapping& top, const std::string& prefix) {
        if (number<std::uint32_t>(top.at("schema_version"), prefix + ".schema_version") != 1) {
            fail(top.at("schema_version"), prefix + ".schema_version", "only version 1 supported");
        }
        exact(top.at("coordinate_units"), prefix + ".coordinate_units", "nanodegrees");
        exact(top.at("coordinate_order"), prefix + ".coordinate_order", "longitude_latitude");
        exact(top.at("datum"), prefix + ".datum", "WGS84");
    }

    Point point(const Mapping& entry, const std::string& field) {
        return {number<std::int64_t>(entry.at("longitude_ndeg"), field + ".longitude_ndeg"),
                number<std::int64_t>(entry.at("latitude_ndeg"), field + ".latitude_ndeg")};
    }

    template <typename Function>
    void validate_at(const YAML::Node& root, Function function) {
        try { function(); }
        catch (const std::invalid_argument& error) { fail(root, "host validation", error.what()); }
    }

    std::string path_;
    std::unordered_map<int, std::vector<YAML::Node>> visited_;
};

template <typename Function>
auto load(const std::filesystem::path& path, Function function) {
    std::error_code error;
    const auto absolute = std::filesystem::absolute(path, error).lexically_normal();
    if (error) throw std::runtime_error(path.u8string() + ": cannot resolve YAML path: " + error.message());
    const auto content = read_file(absolute);
    try {
        const auto documents = YAML::LoadAll(content);
        if (documents.size() != 1) throw std::invalid_argument(absolute.u8string() + ": exactly one YAML document required");
        Reader reader(absolute.u8string());
        return function(reader, documents.front());
    } catch (const YAML::Exception& yaml_error) {
        throw std::invalid_argument(absolute.u8string() + ": YAML:" + std::to_string(yaml_error.mark.line + 1) +
            ":" + std::to_string(yaml_error.mark.column + 1) + ": " + yaml_error.msg);
    }
}

}  // namespace

Domain load_domain(const std::filesystem::path& path) {
    return load(path, [](Reader& reader, const YAML::Node& root) { return reader.domain(root); });
}

std::vector<Anchor> load_overlay(const std::filesystem::path& path) {
    return load(path, [](Reader& reader, const YAML::Node& root) { return reader.overlay(root); });
}

std::vector<Receiver> load_receivers(const std::filesystem::path& path) {
    return load(path, [](Reader& reader, const YAML::Node& root) { return reader.receivers(root); });
}

}  // namespace aismixer::lab::geo
