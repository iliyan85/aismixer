#include "geo_output.h"
#include "ais_message.h"
#include "nmea0183.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {
namespace geo = aismixer::lab::geo;
namespace ais = aismixer::lab::ais;
namespace fs = std::filesystem;

void require(bool condition, std::string_view reason) {
    if (!condition) throw std::runtime_error("Geo output test: " + std::string(reason));
}

class Scratch {
public:
    explicit Scratch(const fs::path& base) {
        fs::create_directories(base);
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        for (unsigned attempt = 0; attempt < 100; ++attempt) {
            directory_ = base / ("output-" + std::to_string(stamp) + "-" + std::to_string(attempt));
            if (fs::create_directory(directory_)) return;
        }
        throw std::runtime_error("Cannot create exclusive output-test directory");
    }
    ~Scratch() {
        std::error_code ignored;
        for (const auto& file : files_) fs::remove(file, ignored);
        fs::remove(directory_, ignored);
    }
    fs::path path(const std::string& name) {
        const auto file = directory_ / fs::u8path(name);
        files_.push_back(file);
        return file;
    }
    void no_temporaries() const {
        for (const auto& entry : fs::directory_iterator(directory_)) {
            require(entry.path().filename().u8string().find(".aismixer-tmp-") == std::string::npos,
                    "owned temporary survived success or handled failure");
        }
    }
private:
    fs::path directory_;
    std::vector<fs::path> files_;
};

std::string read(const fs::path& path) {
    std::ifstream input(path, std::ios::binary);
    require(static_cast<bool>(input), "cannot open test output");
    std::ostringstream content;
    content << input.rdbuf();
    require(!input.bad(), "cannot read test output");
    return content.str();
}

void write(const fs::path& path, const std::string& content) {
    std::ofstream output(path, std::ios::binary);
    output.write(content.data(), static_cast<std::streamsize>(content.size()));
    output.close();
    require(static_cast<bool>(output), "cannot create competitor fixture");
}

template <typename Function>
void expect_failure(Function function, std::string_view reason) {
    try { function(); }
    catch (const std::exception&) { return; }
    throw std::runtime_error("Expected controlled failure: " + std::string(reason));
}

std::vector<geo::OfflineTransmission> transmissions() {
    ais::Type1 one(ais::Mmsi("100000001"), ais::RadioState19(0));
    one.longitude = -1;
    one.latitude = 2;
    ais::Type18 eighteen(ais::Mmsi("100000002"), ais::RadioState20(0));
    eighteen.longitude = 3;
    eighteen.latitude = -4;
    return {{7, 0, 0, 7, ais::encode_aivdm(one, 'A')},
            {9, 0, 0, 9, ais::encode_aivdm(eighteen, 'A')}};
}

void publication_tests(Scratch& scratch) {
    const auto records = transmissions();
    std::string expected;
    for (const auto& record : records) for (const auto& line : record.sentences) expected += line;
    require(expected.size() == 98 && expected.find("\r\n") != std::string::npos,
            "fixture must contain two canonical binary AIS sentences");

    const auto success = scratch.path(u8"успешен-изход.nmea");
    geo::export_nmea(success, records);
    require(read(success) == expected, "Unicode publication changed exact CRLF bytes");
    scratch.no_temporaries();

    const auto existing = scratch.path("existing.nmea");
    write(existing, "preserve existing bytes");
    expect_failure([&] { (void)geo::prepare_nmea_output(existing); }, "existing preflight");
    expect_failure([&] { geo::export_nmea(existing, records); }, "existing publication");
    require(read(existing) == "preserve existing bytes", "existing file was overwritten");
    scratch.no_temporaries();

    const auto competitor = scratch.path(u8"конкурентен-изход.nmea");
    (void)geo::prepare_nmea_output(competitor);
    bool callback_ran = false;
    geo::testing::OutputHooks race;
    race.before_publish = [&](const fs::path& temporary, const fs::path& destination) {
        callback_ran = true;
        require(read(temporary) == expected, "publication attempted before complete flush/close");
        require(!fs::exists(destination), "race fixture destination already exists");
        write(destination, "late competing destination");
    };
    expect_failure([&] { geo::testing::export_nmea_with_hooks(competitor, records, race); },
                   "destination created after preflight");
    require(callback_ran && read(competitor) == "late competing destination",
            "publication overwrote the late competing file");
    scratch.no_temporaries();

    const std::vector<geo::testing::OutputFailure> faults = {
        geo::testing::OutputFailure::Write, geo::testing::OutputFailure::Flush,
        geo::testing::OutputFailure::Close, geo::testing::OutputFailure::Publish};
    for (std::size_t index = 0; index < faults.size(); ++index) {
        const auto target = scratch.path("fault-" + std::to_string(index) + ".nmea");
        geo::testing::OutputHooks hook;
        hook.failure = faults[index];
        hook.fail_write_after_bytes = 13;  // Safe partial temporary write, not disk exhaustion.
        expect_failure([&] { geo::testing::export_nmea_with_hooks(target, records, hook); },
                       "injected write/flush/close/publication error");
        require(!fs::exists(target), "failed output published a partial destination");
        scratch.no_temporaries();
    }

    const auto invalid_target = scratch.path("invalid-sentence.nmea");
    auto invalid = records;
    invalid.back().sentences.front() = "not an AIS sentence\r\n";
    expect_failure([&] { geo::export_nmea(invalid_target, invalid); }, "late sentence validation");
    require(!fs::exists(invalid_target), "invalid later sentence published earlier output");
    scratch.no_temporaries();

    const auto callback_target = scratch.path("callback-failure.nmea");
    geo::testing::OutputHooks failed_callback;
    failed_callback.before_publish = [](const fs::path&, const fs::path&) {
        throw std::runtime_error("controlled prepublication callback error");
    };
    expect_failure([&] { geo::testing::export_nmea_with_hooks(callback_target, records, failed_callback); },
                   "injected prepublication failure");
    require(!fs::exists(callback_target), "failed callback published a destination");
    scratch.no_temporaries();
    expect_failure([&] { (void)geo::prepare_nmea_output(fs::path{}); }, "empty destination");
}

void metadata_tests() {
    constexpr std::size_t population = 1000;
    std::vector<aismixer::lab::Actor> reversed(population);
    for (std::size_t index = 0; index < population; ++index) {
        reversed[index].actor_id = static_cast<std::uint32_t>((population - index - 1) * 2 + 7);
    }
    const auto identities = geo::synthetic_identities(reversed);
    std::vector<geo::GeoEvent> events;
    std::ostringstream expected;
    for (std::uint64_t tick = 0; tick < 4; ++tick) {
        for (std::size_t index = 0; index < population; ++index) {
            const auto actor = static_cast<std::uint32_t>(index * 2 + 7);
            const auto category = index % 2 ? aismixer::lab::ActorCategory::ClassB
                                            : aismixer::lab::ActorCategory::ClassA;
            const auto longitude = -static_cast<std::int64_t>(index + 1);
            const auto latitude = static_cast<std::int64_t>(index + 2);
            events.push_back({tick, tick * 2, actor, category, 0, 0, {longitude, latitude}, 0});
            expected << "event=" << tick << ',' << tick * 2 << ',' << actor << ','
                     << static_cast<std::uint32_t>(category) << ',' << longitude << ','
                     << latitude << ',' << 100000001 + index << '\n';
        }
    }
    std::ostringstream actual;
    geo::write_event_metadata(actual, events, identities);
    require(actual.str() == expected.str(), "many-actor identity metadata bytes/order changed");

    auto missing = identities;
    missing.pop_back();
    expect_failure([&] { std::ostringstream output; geo::write_event_metadata(output, events, missing); },
                   "missing identity lookup");
    auto unsorted = identities;
    std::swap(unsorted.front(), unsorted.back());
    expect_failure([&] { std::ostringstream output; geo::write_event_metadata(output, events, unsorted); },
                   "unsorted identity catalog");
    auto duplicate = identities;
    duplicate.back() = duplicate.front();
    expect_failure([&] { std::ostringstream output; geo::write_event_metadata(output, events, duplicate); },
                   "duplicate identity catalog");
    std::ostringstream broken;
    broken.setstate(std::ios::badbit);
    expect_failure([&] { geo::write_event_metadata(broken, events, identities); }, "metadata stream failure");
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2) { std::cerr << "Usage: geo_output_tests <work-directory>\n"; return 2; }
    try {
        Scratch scratch(fs::u8path(argv[1]));
        publication_tests(scratch);
        metadata_tests();
        std::cout << "PASS atomic Unicode NMEA publication, late competitor preservation, fault cleanup, "
                     "4000 canonical metadata rows/1000 reversed actor identities; host only\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
