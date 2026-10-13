#include "scene_output.h"
#include "scene_validation.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <limits>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

namespace fs = std::filesystem;
using namespace aismixer::lab;
using namespace aismixer::lab::runner;

void require(bool condition, std::string_view message) {
    if (!condition) {
        throw std::runtime_error(std::string(message));
    }
}

template <typename Action>
void invalid(Action action, std::string_view field) {
    bool rejected = false;
    try {
        action();
    } catch (const std::invalid_argument& error) {
        rejected = true;
        require(std::string_view(error.what()).find(field) != std::string_view::npos,
                "invalid-input diagnostic omitted its field");
    }
    require(rejected, "invalid input was accepted");
}

template <typename Action>
void file_error(Action action) {
    bool rejected = false;
    try {
        action();
    } catch (const FileError& error) {
        rejected = true;
        require(std::string_view(error.what()).size() > 10,
                "file error lacked actionable context");
    }
    require(rejected, "unsafe or invalid output path was accepted");
}

void numbers() {
    require(parse_u64("18446744073709551615", "seed") == UINT64_MAX, "uint64 endpoint");
    require(parse_u32("4294967295", "actor_id") == UINT32_MAX, "uint32 endpoint");
    require(parse_i64("-9223372036854775808", "x_mm") == INT64_MIN, "int64 minimum");
    require(parse_i64("9223372036854775807", "x_mm") == INT64_MAX, "int64 maximum");
    require(parse_i32("-2147483648", "velocity") == INT32_MIN, "int32 minimum");
    require(parse_i32("2147483647", "velocity") == INT32_MAX, "int32 maximum");
    require(parse_u64("00042", "seed") == 42 && parse_i64("-0", "x_mm") == 0,
            "strict decimal leading zeros or signed zero");
    for (const std::string value : {"", "+1", " 1", "1 ", "1\n", "1.0", "NaN",
                                   "nan", "1e2", "0x10", "1_000", "--1", "-"}) {
        invalid([&] { (void)parse_u64(value, "seed"); }, "seed");
        invalid([&] { (void)parse_i64(value, "x_mm"); }, "x_mm");
    }
    invalid([] { (void)parse_u64("-1", "seed"); }, "seed");
    invalid([] { (void)parse_u64("18446744073709551616", "seed"); }, "seed");
    invalid([] { (void)parse_u32("4294967296", "actor_id"); }, "actor_id");
    invalid([] { (void)parse_i64("-9223372036854775809", "x_mm"); }, "x_mm");
    invalid([] { (void)parse_i64("9223372036854775808", "x_mm"); }, "x_mm");
    invalid([] { (void)parse_i32("-2147483649", "velocity"); }, "velocity");
    invalid([] { (void)parse_i32("2147483648", "velocity"); }, "velocity");
    std::cout << "PASS strict decimal integer widths and diagnostics\n";
}

Scene small_scene() {
    return Scene{"host-oracle", RunParameters{42, 0, 8, 2},
                 {{1, ActorCategory::ClassA, 1000, -2000, 250, -100, 2, 0}}};
}

void validation() {
    auto scene = small_scene();
    validate_scene(scene);
    scene.simulation = {UINT64_MAX, kMaxAbsoluteTick, 1, kMaxTickSeconds};
    scene.actors[0] = {UINT32_MAX, ActorCategory::ClassA, kMaxInitialPositionMm,
                      -kMaxInitialPositionMm, kMaxVelocityMmPerS, -kMaxVelocityMmPerS,
                      UINT32_MAX, UINT32_MAX - 1};
    validate_scene(scene);
    scene.actors.clear();
    scene.simulation.tick_count = 0;
    validate_scene(scene);
    scene.simulation = {0, 0, kMaxSceneTicks, 1};
    validate_scene(scene);

    const auto check = [](Scene candidate, std::string_view field) {
        invalid([&] { validate_scene(candidate); }, field);
    };
    scene = small_scene();
    scene.actors.push_back(scene.actors[0]);
    check(scene, "actor_id");
    scene = small_scene();
    scene.actors[0].category = static_cast<ActorCategory>(6);
    check(scene, "category");
    scene = small_scene();
    scene.actors[0].report_interval_ticks = 0;
    scene.simulation.tick_count = 0;
    check(scene, "report_interval_ticks");
    scene = small_scene();
    scene.actors[0].phase_ticks = scene.actors[0].report_interval_ticks;
    check(scene, "phase_ticks");
    for (const auto category : {ActorCategory::BaseStation, ActorCategory::AtoN}) {
        scene = small_scene();
        scene.actors[0].category = category;
        scene.actors[0].vy_mm_per_s = 0;
        check(scene, "vx_mm_per_s");
        scene.actors[0].vx_mm_per_s = 0;
        scene.actors[0].vy_mm_per_s = -1;
        check(scene, "vy_mm_per_s");
        scene.actors[0].vy_mm_per_s = 0;
        validate_scene(scene);
    }
    for (const auto position : {kMaxInitialPositionMm + 1, -kMaxInitialPositionMm - 1,
                               INT64_MIN, INT64_MAX}) {
        scene = small_scene();
        scene.actors[0].x_mm = position;
        check(scene, "x_mm");
        scene.actors[0].x_mm = 0;
        scene.actors[0].y_mm = position;
        check(scene, "y_mm");
    }
    for (const auto velocity : {kMaxVelocityMmPerS + 1, -kMaxVelocityMmPerS - 1,
                               INT32_MIN, INT32_MAX}) {
        scene = small_scene();
        scene.actors[0].vx_mm_per_s = velocity;
        check(scene, "vx_mm_per_s");
        scene.actors[0].vx_mm_per_s = 0;
        scene.actors[0].vy_mm_per_s = velocity;
        check(scene, "vy_mm_per_s");
    }
    scene = small_scene();
    for (const auto seconds : {0u, 61u, UINT32_MAX}) {
        scene.simulation.tick_seconds = seconds;
        check(scene, "tick_seconds");
    }
    scene = small_scene();
    for (const auto ticks : {kMaxSceneTicks + 1, UINT64_MAX}) {
        scene.simulation.tick_count = ticks;
        check(scene, "tick_count");
    }
    scene = small_scene();
    scene.simulation.start_tick = kMaxAbsoluteTick;
    scene.simulation.tick_count = 2;
    check(scene, "tick");
    scene.simulation.start_tick = UINT64_MAX;
    scene.simulation.tick_count = 0;
    scene.actors.clear();
    check(scene, "start_tick");
    scene = small_scene();
    scene.name = "   ";
    check(scene, "name");
    scene.name = std::string(201, 'x');
    check(scene, "name");
    scene.name = "bad\nname";
    check(scene, "name");

    scene = small_scene();
    scene.actors.clear();
    for (std::uint32_t id = 0; id < kMaxSceneActors; ++id) {
        scene.actors.push_back({id, ActorCategory::ClassA, 0, 0, 0, 0, 1, 0});
    }
    scene.simulation.tick_count = 100;
    validate_scene(scene); // Exactly one million candidates, host validation only.
    scene.simulation.tick_count = 101;
    check(scene, "tick_count");
    scene.simulation.tick_count = UINT64_MAX;
    check(scene, "tick_count");
    scene.simulation.tick_count = 0;
    scene.actors.push_back({10000, ActorCategory::ClassA, 0, 0, 0, 0, 1, 0});
    check(scene, "actors");
    std::cout << "PASS B2 semantic bounds, stationary classes, duplicates and empty requests\n";
}

std::vector<EventIntent> demo_records() {
    // Recorded B2 oracle from the validated CUDA 12.9 baseline; no GPU is called.
    return {
        {0,10,ActorCategory::ClassA,1000,-2000,1037580057u},
        {0,40,ActorCategory::AtoN,-9000,10000,2619888965u},
        {0,60,ActorCategory::Emergency,-100,200,1764872081u},
        {1,30,ActorCategory::BaseStation,7000,-8000,3726906015u},
        {1,50,ActorCategory::SAR,-600,1100,2482024895u},
        {1,60,ActorCategory::Emergency,100,100,819242961u},
        {2,10,ActorCategory::ClassA,2000,-2400,4007308748u},
        {2,20,ActorCategory::ClassB,-700,700,2823968003u},
        {2,60,ActorCategory::Emergency,300,0,261283093u},
        {3,50,ActorCategory::SAR,-1800,3100,1729192233u},
        {3,60,ActorCategory::Emergency,500,-100,3276829640u},
        {4,10,ActorCategory::ClassA,3000,-2800,2345979035u},
        {4,30,ActorCategory::BaseStation,7000,-8000,1770527517u},
        {4,40,ActorCategory::AtoN,-9000,10000,60766089u},
        {4,60,ActorCategory::Emergency,700,-200,1325498485u},
        {5,20,ActorCategory::ClassB,-1000,850,3035815649u},
        {5,50,ActorCategory::SAR,-3000,5100,98501157u},
        {5,60,ActorCategory::Emergency,900,-300,360867017u},
        {6,10,ActorCategory::ClassA,4000,-3200,3649219634u},
        {6,60,ActorCategory::Emergency,1100,-400,1627061349u},
        {7,30,ActorCategory::BaseStation,7000,-8000,4031106803u},
        {7,50,ActorCategory::SAR,-4200,7100,870806573u},
        {7,60,ActorCategory::Emergency,1300,-500,1596043612u},
    };
}

std::string csv_text(const std::vector<EventIntent>& events) {
    std::ostringstream output;
    write_csv(output, events);
    return output.str();
}

class GroupedNumbers final : public std::numpunct<char> {
protected:
    char do_thousands_sep() const override { return ','; }
    std::string do_grouping() const override { return "\3"; }
};

void output_helpers() {
    require(event_digest({}) == UINT64_C(0xcbf29ce484222325), "empty FNV offset basis");
    const std::vector<EventIntent> custom{{UINT64_C(0x0123456789abcdef), 0x89abcdefu,
        ActorCategory::Emergency, -INT64_C(1234567890123), INT64_C(9876543210), 0x76543210u}};
    // Independently computed with Python struct.pack('<QIIqqI', ...) and FNV-1a.
    require(event_digest(custom) == UINT64_C(0xe3edbce05c6f7ab8), "36-byte LE serialization");
    const auto events = demo_records();
    require(event_digest(events) == UINT64_C(0x7aad5e9c764012e3), "B2 golden fixture digest");
    require(csv_text({events.front()}) ==
        "absolute_tick,actor_id,category,x_mm,y_mm,random_u32\n"
        "0,10,0,1000,-2000,1037580057\n", "CSV header/row schema changed");
    std::ostringstream decorated;
    decorated.imbue(std::locale(std::locale::classic(), new GroupedNumbers));
    decorated << std::hex << std::showpos << std::setw(30) << std::setfill('!');
    write_csv(decorated, {events.front()});
    require(decorated.str() == csv_text({events.front()}),
            "canonical CSV depends on stream locale or formatting flags");
    std::ostringstream summary;
    print_summary(summary, small_scene(), events);
    const auto text = summary.str();
    require(text.find("7aad5e9c764012e3") != std::string::npos, "summary omitted digest");
    require(text.find("preview_count=10") != std::string::npos, "default preview is not ten events");
    require(std::count(text.begin(), text.end(), '\n') < 40,
            "default summary preview is unbounded");
    auto many = small_scene();
    many.actors.clear();
    for (std::uint32_t id = 0; id < 101; ++id) {
        many.actors.push_back({id, ActorCategory::ClassA, 0, 0, 0, 0, 1, 0});
    }
    std::ostringstream inspected;
    print_scene(inspected, many);
    require(inspected.str().find("actors_shown=100/101") != std::string::npos &&
                inspected.str().find("actor_id=100 ") == std::string::npos,
            "actor inspection is not bounded to one hundred actors");
    std::cout << "PASS independent 36-byte FNV oracle, B2 golden, CSV and bounded summary\n";
}

std::string read_file(const fs::path& path) {
    std::ifstream input(path, std::ios::binary);
    require(input.is_open(), "cannot read test output");
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

std::string normalize_lines(std::string value) {
    value.erase(std::remove(value.begin(), value.end(), '\r'), value.end());
    return value;
}

void files(const fs::path& base) {
    fs::create_directories(base);
    const auto token = std::chrono::steady_clock::now().time_since_epoch().count();
    fs::path owned;
    for (unsigned suffix = 0; ; ++suffix) {
        owned = base / ("host-" + std::to_string(token) + "-" + std::to_string(suffix));
        if (fs::create_directory(owned)) {
            break;
        }
    }
    const auto events = demo_records();
    const auto target = owned / fs::u8path(u8"нови-данни") / fs::u8path(u8"сцена.csv");
    const auto resolved = prepare_output_path(target);
    require(resolved.is_absolute(), "output path was not resolved");
    export_csv(resolved, events);
    require(normalize_lines(read_file(resolved)) == csv_text(events), "export differs from CSV stream");
    const auto original = read_file(resolved);
    file_error([&] { export_csv(resolved, {}); });
    require(read_file(resolved) == original, "overwrite refusal modified prior output");
    file_error([&] { (void)prepare_output_path(resolved); });
    file_error([&] { (void)prepare_output_path(owned); });
    file_error([&] { (void)prepare_output_path(fs::path{}); });
    const auto blocker = owned / "parent-is-file";
    { std::ofstream file(blocker, std::ios::binary); file << "preserve"; }
    file_error([&] { export_csv(blocker / "events.csv", events); });
    require(read_file(blocker) == "preserve", "file-error handling changed parent file");
    std::cout << "PASS CSV full write/close, exclusive export, Unicode paths and file errors\n"
              << "Artifacts retained: " << path_text(owned) << '\n';
}

} // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 2, "Usage: aismixer_lab_scene_host_tests WORK_DIR");
        numbers();
        validation();
        output_helpers();
        files(fs::u8path(argv[1]));
        std::cout << "Scene host tests: PASS (no YAML parser or CUDA runtime used)\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Scene host tests: FAIL: " << error.what() << '\n';
        return 1;
    }
}
