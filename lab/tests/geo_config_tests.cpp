#include "geo_config.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

namespace geo = aismixer::lab::geo;

void require(bool condition, std::string_view reason) {
    if (!condition) throw std::runtime_error("Geo configuration test: " + std::string(reason));
}

class Scratch {
public:
    explicit Scratch(const std::filesystem::path& base) {
        std::filesystem::create_directories(base);
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        for (unsigned attempt = 0; attempt < 100; ++attempt) {
            const auto candidate = base / ("geo-config-" + std::to_string(stamp) + "-" + std::to_string(attempt));
            if (std::filesystem::create_directory(candidate)) {
                directory_ = candidate;
                return;
            }
        }
        throw std::runtime_error("Cannot create exclusive external test directory");
    }
    ~Scratch() {
        std::error_code ignored;
        for (const auto& file : files_) std::filesystem::remove(file, ignored);
        std::filesystem::remove(directory_, ignored);
    }
    std::filesystem::path write(const std::string& content) {
        const auto file = directory_ / ("case-" + std::to_string(files_.size()) + ".yaml");
        files_.push_back(file);
        std::ofstream output(file, std::ios::binary);
        output.write(content.data(), static_cast<std::streamsize>(content.size()));
        output.close();
        require(static_cast<bool>(output), "temporary YAML write failed");
        return file;
    }
private:
    std::filesystem::path directory_;
    std::vector<std::filesystem::path> files_;
};

std::string replace_once(std::string value, const std::string& old_value, const std::string& new_value) {
    const auto position = value.find(old_value);
    require(position != std::string::npos, "invalid-case substitution has no matching text");
    value.replace(position, old_value.size(), new_value);
    return value;
}

constexpr const char* domain_yaml =
    "schema_version: 1\nprofile_id: test_square_v1\nprovenance: synthetic test fixture\n"
    "coordinate_units: nanodegrees\ncoordinate_order: longitude_latitude\ndatum: WGS84\n"
    "ring_closure: explicit\nboundary: inclusive\nring:\n"
    "  - {longitude_ndeg: 0, latitude_ndeg: 0}\n"
    "  - {longitude_ndeg: 1000000000, latitude_ndeg: 0}\n"
    "  - {longitude_ndeg: 1000000000, latitude_ndeg: 1000000000}\n"
    "  - {longitude_ndeg: 0, latitude_ndeg: 1000000000}\n"
    "  - {longitude_ndeg: 0, latitude_ndeg: 0}\n";

constexpr const char* overlay_yaml =
    "schema_version: 1\ncoordinate_units: nanodegrees\ncoordinate_order: longitude_latitude\n"
    "datum: WGS84\nprojection: wgs84_tangent_linear_v1\nanchor_reference: actor_tick_zero\n"
    "anchors:\n  - {actor_id: 0, longitude_ndeg: 0, latitude_ndeg: 0}\n"
    "  - {actor_id: 99, longitude_ndeg: -100, latitude_ndeg: 100}\n";

constexpr const char* receiver_yaml =
    "schema_version: 1\ncoordinate_units: nanodegrees\ncoordinate_order: longitude_latitude\n"
    "datum: WGS84\nheight_datum: WGS84_ellipsoid\nreceivers:\n"
    "  - {receiver_id: 0, longitude_ndeg: 0, latitude_ndeg: 0, antenna_height_mm: -500}\n"
    "  - {receiver_id: 99, longitude_ndeg: 100, latitude_ndeg: -100, antenna_height_mm: 25000}\n";

template <typename Function>
void expect_invalid(const std::string& label, Function function, unsigned& count) {
    try { function(); }
    catch (const std::invalid_argument&) { ++count; return; }
    throw std::runtime_error("Invalid configuration accepted: " + label);
}

void tests(const std::filesystem::path& profiles, Scratch& scratch) {
    const auto domain = geo::load_domain(profiles / "black_sea_demo.yaml");
    require(domain.ring.size() == 17, "Black Sea landmark sequence/closure changed");
    require(domain.profile_id == "black_sea_illustrative_wikidata_v1" &&
            domain.provenance.find("Wikidata") != std::string::npos &&
            domain.provenance.find("2026-10-14") != std::string::npos,
            "coarse profile lost its source/version/data-quality declaration");
    const auto overlay = geo::load_overlay(profiles / "scene_demo_overlay.yaml");
    require(overlay.size() == 6, "demo overlay must match all six B2 actor identities");
    std::vector<aismixer::lab::Actor> actors(6);
    for (std::size_t index = 0; index < actors.size(); ++index) {
        actors[index].actor_id = static_cast<std::uint32_t>((index + 1) * 10);
    }
    geo::validate_anchors(actors, overlay);
    const auto receivers = geo::load_receivers(profiles / "receivers_demo.yaml");
    require(receivers.size() == 3, "synthetic passive receiver catalog changed");

    require(geo::load_domain(scratch.write(domain_yaml)).ring.size() == 5, "valid square did not parse");
    require(geo::load_domain(scratch.write(std::string("\xef\xbb\xbf") + domain_yaml)).ring.size() == 5,
            "valid UTF-8 BOM did not parse");
    require(geo::load_overlay(scratch.write(overlay_yaml)).size() == 2, "valid signed anchors did not parse");
    require(geo::load_receivers(scratch.write(receiver_yaml)).front().antenna_height_mm == -500,
            "negative ellipsoidal height was not retained");
    require(geo::load_overlay(scratch.write(replace_once(overlay_yaml,
        "anchors:\n  - {actor_id: 0, longitude_ndeg: 0, latitude_ndeg: 0}\n"
        "  - {actor_id: 99, longitude_ndeg: -100, latitude_ndeg: 100}\n", "anchors: []\n"))).empty(),
        "empty overlay must be supported");
    require(geo::load_receivers(scratch.write(replace_once(receiver_yaml,
        "receivers:\n  - {receiver_id: 0, longitude_ndeg: 0, latitude_ndeg: 0, antenna_height_mm: -500}\n"
        "  - {receiver_id: 99, longitude_ndeg: 100, latitude_ndeg: -100, antenna_height_mm: 25000}\n",
        "receivers: []\n"))).empty(), "empty receiver catalog must be supported");

    unsigned invalid_count = 0;
    const auto bad_domain = [&](const std::string& name, const std::string& content) {
        expect_invalid(name, [&] { (void)geo::load_domain(scratch.write(content)); }, invalid_count);
    };
    bad_domain("duplicate key", std::string(domain_yaml) + "schema_version: 1\n");
    bad_domain("unknown field", std::string(domain_yaml) + "include: external.yaml\n");
    bad_domain("missing required metadata", replace_once(domain_yaml, "datum: WGS84\n", ""));
    bad_domain("schema version", replace_once(domain_yaml, "schema_version: 1", "schema_version: 2"));
    bad_domain("axis order", replace_once(domain_yaml, "longitude_latitude", "latitude_longitude"));
    bad_domain("coordinate units", replace_once(domain_yaml, "nanodegrees", "degrees"));
    bad_domain("datum", replace_once(domain_yaml, "datum: WGS84", "datum: NAD83"));
    bad_domain("closure policy", replace_once(domain_yaml, "ring_closure: explicit", "ring_closure: implicit"));
    bad_domain("boundary policy", replace_once(domain_yaml, "boundary: inclusive", "boundary: exclusive"));
    bad_domain("explicit YAML tag", replace_once(domain_yaml, "datum: WGS84", "datum: !!str WGS84"));
    bad_domain("scalar alias", replace_once(replace_once(domain_yaml,
        "profile_id: test_square_v1", "profile_id: &id test_square_v1"),
        "provenance: synthetic test fixture", "provenance: *id"));
    bad_domain("ring alias", replace_once(replace_once(domain_yaml,
        "ring:\n  - {longitude_ndeg: 0, latitude_ndeg: 0}",
        "ring:\n  - &origin {longitude_ndeg: 0, latitude_ndeg: 0}"),
        "  - {longitude_ndeg: 0, latitude_ndeg: 0}\n", "  - *origin\n"));
    bad_domain("multiple documents", std::string(domain_yaml) + "---\n" + domain_yaml);
    bad_domain("fractional coordinate", replace_once(domain_yaml, "longitude_ndeg: 1000000000", "longitude_ndeg: 1.5"));
    bad_domain("plus sign", replace_once(domain_yaml, "longitude_ndeg: 1000000000", "longitude_ndeg: +1000000000"));
    bad_domain("integer overflow", replace_once(domain_yaml, "longitude_ndeg: 1000000000", "longitude_ndeg: 18446744073709551616"));
    bad_domain("invalid geographic coordinate", replace_once(domain_yaml, "longitude_ndeg: 1000000000", "longitude_ndeg: 181000000000"));
    bad_domain("unsupported latitude", replace_once(domain_yaml, "latitude_ndeg: 1000000000", "latitude_ndeg: 71000000000"));
    bad_domain("unclosed ring", replace_once(domain_yaml, "  - {longitude_ndeg: 0, latitude_ndeg: 0}\n", ""));
    bad_domain("duplicate vertex", replace_once(domain_yaml,
        "  - {longitude_ndeg: 1000000000, latitude_ndeg: 1000000000}",
        "  - {longitude_ndeg: 1000000000, latitude_ndeg: 0}"));
    bad_domain("bow tie", replace_once(domain_yaml,
        "  - {longitude_ndeg: 1000000000, latitude_ndeg: 0}\n"
        "  - {longitude_ndeg: 1000000000, latitude_ndeg: 1000000000}\n"
        "  - {longitude_ndeg: 0, latitude_ndeg: 1000000000}\n",
        "  - {longitude_ndeg: 1000000000, latitude_ndeg: 1000000000}\n"
        "  - {longitude_ndeg: 1000000000, latitude_ndeg: 0}\n"
        "  - {longitude_ndeg: 0, latitude_ndeg: 1000000000}\n"));
    bad_domain("bad UTF-8", std::string(domain_yaml) + "# invalid \xE9\n");
    bad_domain("oversized file", std::string(1024 * 1024 + 1, ' '));
    bad_domain("oversized profile ID", replace_once(domain_yaml, "test_square_v1", std::string(129, 'a')));
    bad_domain("empty provenance", replace_once(domain_yaml, "synthetic test fixture", "\"\""));
    std::string excessive_ring = std::string(domain_yaml).substr(0, std::string(domain_yaml).find("ring:\n")) + "ring:\n";
    for (std::size_t index = 0; index < geo::kMaxPolygonVertices + 2; ++index) {
        excessive_ring += "  - {longitude_ndeg: 0, latitude_ndeg: 0}\n";
    }
    bad_domain("vertex resource bound", excessive_ring);

    const auto bad_overlay = [&](const std::string& name, const std::string& content) {
        expect_invalid(name, [&] { (void)geo::load_overlay(scratch.write(content)); }, invalid_count);
    };
    bad_overlay("duplicate anchor", replace_once(overlay_yaml, "actor_id: 99", "actor_id: 0"));
    bad_overlay("negative actor identity", replace_once(overlay_yaml, "actor_id: 99", "actor_id: -1"));
    bad_overlay("unknown projection", replace_once(overlay_yaml, "wgs84_tangent_linear_v1", "global_equirectangular"));
    bad_overlay("wrong reference epoch", replace_once(overlay_yaml, "actor_tick_zero", "batch_start"));
    bad_overlay("actor ID overflow", replace_once(overlay_yaml, "actor_id: 99", "actor_id: 4294967296"));
    bad_overlay("axis ambiguous coordinate", replace_once(overlay_yaml, "longitude_ndeg: -100", "x_mm: -100"));

    const auto bad_receivers = [&](const std::string& name, const std::string& content) {
        expect_invalid(name, [&] { (void)geo::load_receivers(scratch.write(content)); }, invalid_count);
    };
    bad_receivers("duplicate receiver", replace_once(receiver_yaml, "receiver_id: 99", "receiver_id: 0"));
    bad_receivers("unknown height datum", replace_once(receiver_yaml, "WGS84_ellipsoid", "mean_sea_level"));
    bad_receivers("unbounded height", replace_once(receiver_yaml, "antenna_height_mm: 25000", "antenna_height_mm: 10000001"));
    bad_receivers("implicit RF field", replace_once(receiver_yaml, "antenna_height_mm: 25000", "antenna_height_mm: 25000, frequency: 162000000"));
    expect_invalid("real scene association mismatch", [&] {
        auto wrong = overlay;
        wrong.front().actor_id = 77;
        geo::validate_anchors(actors, wrong);
    }, invalid_count);

    std::cout << "PASS geospatial YAML profiles, named axes, UTF-8, strict metadata, identities; "
              << invalid_count << " invalid cases rejected; no CUDA initialized\n";
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "Usage: geo_config_tests <profiles-directory> <external-work-directory>\n";
        return 2;
    }
    try {
        Scratch scratch(std::filesystem::u8path(argv[2]));
        tests(std::filesystem::u8path(argv[1]), scratch);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
