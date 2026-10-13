#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#else
#include <unistd.h>
#endif

#include "scene_output.h"
#include "scene_validation.h"
#include "scene_yaml.h"

#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

namespace {

namespace fs = std::filesystem;
namespace runner = aismixer::lab::runner;
using aismixer::lab::EventIntent;
using runner::FileError;
using runner::RunParameters;
using runner::Scene;

class GpuError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

enum class Format { Summary, Csv };

struct Options {
    bool help = false;
    bool interactive = false;
    bool validate = false;
    bool format_set = false;
    Format format = Format::Summary;
    std::optional<fs::path> scene_path;
    std::optional<fs::path> output_path;
    std::optional<std::uint64_t> seed;
    std::optional<std::uint64_t> start_tick;
    std::optional<std::uint64_t> tick_count;
    std::optional<std::uint32_t> tick_seconds;
};

void print_help() {
    std::cout
        << "AISMixer Lab - Native Scene Runner\n"
        << "Usage: aismixer_lab_scene --interactive [--scene PATH] [run overrides]\n"
        << "       aismixer_lab_scene --scene PATH [run overrides] [--validate]\n"
        << "       aismixer_lab_scene --scene PATH [run overrides] [--format summary|csv]\n"
        << "                          [--output PATH]\n"
        << "       aismixer_lab_scene --help\n\n"
        << "  --scene PATH         Load a UTF-8 schema_version=1 YAML scene.\n"
        << "  --seed UINT64        Override the explicit seed for this invocation.\n"
        << "  --start-tick UINT64  Override the first absolute tick.\n"
        << "  --tick-count UINT64  Override the number of ticks (0..3600).\n"
        << "  --tick-seconds UINT32  Override seconds per tick (1..60).\n"
        << "  --validate           Check schema and scene bounds on the CPU, without CUDA.\n"
        << "  --format summary|csv  Default summary previews at most ten events.\n"
        << "                       CSV stdout contains only the canonical header/rows;\n"
        << "                       diagnostics and digest are written to stderr.\n"
        << "  --output PATH        Export complete CSV, refusing an existing destination.\n"
        << "  --interactive        Load, inspect, edit, validate, run and export repeatedly.\n"
        << "  --help               Show this help without reading YAML or initializing CUDA.\n\n"
        << "Numbers are strict decimal integers; overrides never rewrite YAML.\n"
        << "Duplicate options are invalid. Help cannot be combined with other options.\n"
        << "Interactive mode accepts a startup scene and overrides, but not --validate,\n"
        << "--format or --output. Validate cannot be combined with CSV or output.\n"
        << "Without arguments, terminal stdin starts interactive mode; redirected stdin\n"
        << "requires explicit --interactive. All user paths are relative to the current\n"
        << "working directory. Interactive startup searches lab/scenes/demo.yaml, then\n"
        << "scenes/demo.yaml; subsequent loads adopt each file's simulation defaults.\n\n"
        << "Exit codes: 0 success/help/clean interactive exit, 2 syntax or scene validation,\n"
        << "3 file or stream I/O, 4 GPU failure. Event intents are not AIS frames or RF\n"
        << "transmissions; intervals are explicit scenario choices, not AIS standards.\n";
}

Options parse_options(const std::vector<std::string>& arguments) {
    Options options;
    std::set<std::string_view> seen;
    for (std::size_t i = 1; i < arguments.size(); ++i) {
        const std::string_view argument(arguments[i]);
        if (argument != "--help" && argument != "--interactive" && argument != "--validate"
            && argument != "--scene" && argument != "--output" && argument != "--format"
            && argument != "--seed" && argument != "--start-tick"
            && argument != "--tick-count" && argument != "--tick-seconds") {
            throw std::invalid_argument("unknown option: " + std::string(argument));
        }
        if (!seen.insert(argument).second) {
            throw std::invalid_argument("duplicate option: " + std::string(argument));
        }
        if (argument == "--help") {
            options.help = true;
        } else if (argument == "--interactive") {
            options.interactive = true;
        } else if (argument == "--validate") {
            options.validate = true;
        } else {
            if (++i == arguments.size() || arguments[i].empty()
                || std::string_view(arguments[i]).substr(0, 2) == "--") {
                throw std::invalid_argument("missing value for " + std::string(argument));
            }
            const std::string_view value(arguments[i]);
            if (argument == "--scene") {
                options.scene_path = fs::u8path(value.begin(), value.end());
            } else if (argument == "--output") {
                options.output_path = fs::u8path(value.begin(), value.end());
            } else if (argument == "--format") {
                options.format_set = true;
                if (value == "summary") {
                    options.format = Format::Summary;
                } else if (value == "csv") {
                    options.format = Format::Csv;
                } else {
                    throw std::invalid_argument("--format must be summary or csv");
                }
            } else if (argument == "--seed") {
                options.seed = runner::parse_u64(value, argument);
            } else if (argument == "--start-tick") {
                options.start_tick = runner::parse_u64(value, argument);
            } else if (argument == "--tick-count") {
                options.tick_count = runner::parse_u64(value, argument);
            } else {
                options.tick_seconds = runner::parse_u32(value, argument);
            }
        }
    }
    if (options.help && seen.size() != 1) {
        throw std::invalid_argument("--help cannot be combined with other options");
    }
    if (options.interactive && (options.validate || options.format_set || options.output_path)) {
        throw std::invalid_argument("--interactive cannot be combined with --validate, --format or --output");
    }
    if (options.validate && (options.output_path || options.format == Format::Csv)) {
        throw std::invalid_argument("--validate cannot be combined with --output or --format csv");
    }
    // Reject intrinsically invalid numeric overrides before opening a scene.
    if (options.start_tick && *options.start_tick > aismixer::lab::kMaxAbsoluteTick) {
        throw std::invalid_argument("--start-tick must be at most 1000000000");
    }
    if (options.tick_count && *options.tick_count > aismixer::lab::kMaxSceneTicks) {
        throw std::invalid_argument("--tick-count must be between 0 and 3600");
    }
    if (options.tick_seconds && (*options.tick_seconds == 0
        || *options.tick_seconds > aismixer::lab::kMaxTickSeconds)) {
        throw std::invalid_argument("--tick-seconds must be between 1 and 60");
    }
    if (options.start_tick && options.tick_count && *options.tick_count != 0
        && *options.tick_count - 1 > aismixer::lab::kMaxAbsoluteTick - *options.start_tick) {
        throw std::invalid_argument("--start-tick/--tick-count last included tick exceeds 1000000000");
    }
    return options;
}

void apply_overrides(Scene& scene, const Options& options) {
    if (options.seed) scene.simulation.seed = *options.seed;
    if (options.start_tick) scene.simulation.start_tick = *options.start_tick;
    if (options.tick_count) scene.simulation.tick_count = *options.tick_count;
    if (options.tick_seconds) scene.simulation.tick_seconds = *options.tick_seconds;
    runner::validate_scene(scene);
}

bool input_is_terminal() {
#ifdef _WIN32
    DWORD mode = 0;
    return GetConsoleMode(GetStdHandle(STD_INPUT_HANDLE), &mode) != 0;
#else
    return isatty(STDIN_FILENO) != 0;
#endif
}

void configure_terminal_utf8() {
#ifdef _WIN32
    DWORD mode = 0;
    if (GetConsoleMode(GetStdHandle(STD_INPUT_HANDLE), &mode)) {
        SetConsoleCP(CP_UTF8);
    }
    if (GetConsoleMode(GetStdHandle(STD_OUTPUT_HANDLE), &mode)
        || GetConsoleMode(GetStdHandle(STD_ERROR_HANDLE), &mode)) {
        SetConsoleOutputCP(CP_UTF8);
    }
#endif
}

fs::path resolved_scene_path(const fs::path& path) {
    std::error_code error;
    const auto resolved = fs::absolute(path, error);
    if (error) {
        throw FileError("Cannot resolve scene path '" + runner::path_text(path) + "': " + error.message());
    }
    return resolved.lexically_normal();
}

void flush_output(std::ostream& output, std::string_view destination) {
    output.flush();
    if (!output) {
        throw FileError("Failed to write " + std::string(destination));
    }
}

std::vector<EventIntent> generate(const Scene& scene) {
    runner::validate_scene(scene);
    try {
        const auto& p = scene.simulation;
        return aismixer::lab::generate_event_intents(scene.actors, p.seed, p.start_tick,
                                                    p.tick_count, p.tick_seconds);
    } catch (const std::invalid_argument&) {
        throw;
    } catch (const std::exception& error) {
        throw GpuError(error.what());
    }
}

void print_validation(const Scene& scene) {
    runner::print_scene(std::cout, scene, 0);
    std::cout << "Validation passed (host-only): schema, actor IDs/categories, schedules,\n"
        << "stationary classes, positions/velocities and bounded tick/candidate range.\n"
        << "CUDA was not initialized; GPU runtime health was not checked.\n";
    flush_output(std::cout, "validation summary to stdout");
}

int batch(const Options& options) {
    if (!options.scene_path) {
        throw std::invalid_argument("--scene PATH is required; use --interactive for a terminal session");
    }
    const auto source = resolved_scene_path(*options.scene_path);
    auto scene = runner::load_scene(source);
    apply_overrides(scene, options);
    if (options.validate) {
        std::cout << "scene_file=" << runner::path_text(source) << '\n';
        print_validation(scene);
        return 0;
    }
    std::optional<fs::path> output;
    if (options.output_path) {
        output = runner::prepare_output_path(*options.output_path);
    }
    const auto events = generate(scene);
    if (output) {
        runner::export_csv(*output, events);
    }
    if (options.format == Format::Csv) {
#ifdef _WIN32
        // Keep redirected stdout byte-identical to the binary CSV export: CRT
        // text mode would otherwise translate each LF to CRLF on Windows.
        if (_setmode(_fileno(stdout), _O_BINARY) == -1) {
            throw FileError("Cannot set stdout to binary mode for canonical CSV");
        }
#endif
        runner::write_csv(std::cout, events);
        flush_output(std::cout, "CSV to stdout");
        std::cerr << "scene_file=" << runner::path_text(source) << '\n';
        runner::print_summary(std::cerr, scene, events, 0);
        if (output) {
            std::cerr << "CSV export complete: " << runner::path_text(*output) << '\n';
        }
        flush_output(std::cerr, "CSV diagnostics to stderr");
    } else {
        std::cout << "scene_file=" << runner::path_text(source) << '\n';
        runner::print_summary(std::cout, scene, events);
        if (output) {
            std::cout << "CSV export complete: " << runner::path_text(*output) << '\n';
        }
        flush_output(std::cout, "run summary to stdout");
    }
    return 0;
}

// EOF is a normal interactive exit, including when explicit --interactive uses
// redirected input. Other input failures are reported as stream I/O errors.
bool prompt(std::string_view text, std::string& answer) {
    std::cout << text;
    flush_output(std::cout, "interactive prompt to stdout");
    if (std::getline(std::cin, answer)) {
        return true;
    }
    if (!std::cin.eof()) {
        throw FileError("Failed to read interactive input");
    }
    return false;
}

template <typename T, typename Parser>
bool edit_parameter(Scene& scene, std::string_view field, T RunParameters::*member, Parser parse) {
    for (unsigned attempt = 0; attempt < 3; ++attempt) {
        const std::string text = std::string(field) + " ["
            + std::to_string(scene.simulation.*member) + "] (Enter keeps): ";
        std::string answer;
        if (!prompt(text, answer)) return false;
        if (answer.empty()) return true;
        try {
            const T value = parse(answer, field);
            auto tentative = scene;
            tentative.simulation.*member = value;
            runner::validate_scene(tentative);
            scene.simulation.*member = value;
            std::cout << field << '=' << value << '\n';
            return true;
        } catch (const std::invalid_argument& error) {
            std::cerr << "Invalid " << field << ": " << error.what() << '\n';
        }
    }
    std::cout << "Three invalid attempts; previous " << field << " kept.\n";
    return true;
}

bool edit_parameters(Scene& scene) {
    return edit_parameter(scene, "seed", &RunParameters::seed, runner::parse_u64)
        && edit_parameter(scene, "start_tick", &RunParameters::start_tick, runner::parse_u64)
        && edit_parameter(scene, "tick_count", &RunParameters::tick_count, runner::parse_u64)
        && edit_parameter(scene, "tick_seconds", &RunParameters::tick_seconds, runner::parse_u32);
}

void print_menu(const std::optional<Scene>& scene, const std::optional<fs::path>& source) {
    std::cout << "\nAISMixer Lab - Scene Runner\n";
    if (scene) {
        const auto& p = scene->simulation;
        std::cout << "Current scene: " << scene->name << " (" << scene->actors.size() << " actors)\n"
            << "File: " << runner::path_text(*source) << '\n'
            << "Seed: " << p.seed << " | start: " << p.start_tick << " | ticks: " << p.tick_count
            << " | seconds/tick: " << p.tick_seconds << '\n';
    } else {
        std::cout << "Current scene: none; choose 1 to load a YAML scenario.\n";
    }
    std::cout << "1  Load YAML scenario\n"
        << "2  View scene and actor summary (first 100 actors)\n"
        << "3  Edit seed / start tick / tick count / tick seconds\n"
        << "4  Validate scenario (no GPU)\n"
        << "5  Run GPU scene (summary + first 10 events)\n"
        << "6  Run and export complete CSV to chosen path\n"
        << "7  Help\n"
        << "0  Exit\n";
}

std::optional<fs::path> discover_demo() {
    for (const auto& candidate : {fs::path("lab/scenes/demo.yaml"), fs::path("scenes/demo.yaml")}) {
        std::error_code error;
        if (fs::is_regular_file(candidate, error)) {
            return resolved_scene_path(candidate);
        }
        if (error && error != std::errc::no_such_file_or_directory) {
            throw FileError("Cannot inspect default scene '" + runner::path_text(candidate)
                + "': " + error.message());
        }
    }
    return std::nullopt;
}

void print_interactive_error(const std::exception& error) {
    std::cerr << "Action failed: " << error.what() << "\nCurrent scene/settings are retained.\n";
}

int interactive(const Options& options) {
    std::optional<Scene> current;
    std::optional<fs::path> source;
    if (options.scene_path) {
        // Explicit startup errors are command-line failures, with nonzero status.
        source = resolved_scene_path(*options.scene_path);
        current = runner::load_scene(*source);
    } else {
        // Automatic discovery may fail without preventing a manual menu load.
        try {
            source = discover_demo();
            if (source) current = runner::load_scene(*source);
        } catch (const FileError& error) {
            print_interactive_error(error);
            source.reset();
        } catch (const std::invalid_argument& error) {
            print_interactive_error(error);
            source.reset();
        }
    }
    if (current) {
        apply_overrides(*current, options);
        std::cout << "Loaded startup scene: " << runner::path_text(*source) << '\n';
    } else {
        if (options.seed || options.start_tick || options.tick_count || options.tick_seconds) {
            throw std::invalid_argument("startup run overrides require a usable scene; provide --scene PATH");
        }
        std::cout << "No usable default demo was found. Choose 1 and enter a YAML scene path.\n";
    }
    for (;;) {
        print_menu(current, source);
        std::string choice;
        if (!prompt("Choice: ", choice) || choice == "0") return 0;
        if (choice.empty()) continue;
        try {
            if (choice == "1") {
                std::string path;
                if (!prompt("YAML path (Enter cancels): ", path)) return 0;
                if (path.empty()) continue;
                const auto next_source = resolved_scene_path(fs::u8path(path));
                auto next = runner::load_scene(next_source);
                // Commit only after a complete successful load and validation.
                current = std::move(next);
                source = next_source;
                std::cout << "Loaded scene: " << runner::path_text(*source)
                    << " (simulation defaults adopted)\n";
            } else if (choice == "7") {
                print_help();
            } else if (choice == "2" || choice == "3" || choice == "4"
                       || choice == "5" || choice == "6") {
                if (!current) {
                    std::cout << "Load a scene with option 1 first.\n";
                    continue;
                }
                if (choice == "2") {
                    runner::print_scene(std::cout, *current);
                    if (current->actors.size() > 100) {
                        std::string answer;
                        if (!prompt("Show all actors? [y/N]: ", answer)) return 0;
                        if (answer == "y" || answer == "Y") {
                            runner::print_scene(std::cout, *current, current->actors.size());
                        }
                    }
                } else if (choice == "3") {
                    if (!edit_parameters(*current)) return 0;
                } else if (choice == "4") {
                    runner::validate_scene(*current);
                    print_validation(*current);
                } else {
                    std::optional<fs::path> output;
                    if (choice == "6") {
                        std::string path;
                        if (!prompt("New CSV output path (Enter cancels): ", path)) return 0;
                        if (path.empty()) continue;
                        // Refuse an existing destination before any CUDA access.
                        output = runner::prepare_output_path(fs::u8path(path));
                    }
                    const auto events = generate(*current);
                    if (output) runner::export_csv(*output, events);
                    runner::print_summary(std::cout, *current, events);
                    if (output) {
                        std::cout << "CSV export complete: " << runner::path_text(*output) << '\n';
                    }
                }
            } else {
                std::cout << "Invalid menu choice; enter a number from 0 through 7.\n";
            }
            flush_output(std::cout, "interactive output to stdout");
        } catch (const FileError& error) {
            print_interactive_error(error);
        } catch (const fs::filesystem_error& error) {
            print_interactive_error(error);
        } catch (const std::invalid_argument& error) {
            print_interactive_error(error);
        } catch (const GpuError& error) {
            std::cerr << "GPU run failed: " << error.what() << "\nCurrent scene/settings are retained.\n";
        }
    }
}

int run(const std::vector<std::string>& arguments) {
    const auto options = parse_options(arguments);
    if (options.help) {
        print_help();
        flush_output(std::cout, "help to stdout");
        return 0;
    }
    if (options.interactive || (arguments.size() == 1 && input_is_terminal())) {
        return interactive(options);
    }
    if (arguments.size() == 1) {
        throw std::invalid_argument("no arguments with noninteractive stdin; use --scene PATH or --interactive");
    }
    return batch(options);
}

int guarded_run(const std::vector<std::string>& arguments) {
    try {
        return run(arguments);
    } catch (const FileError& error) {
        std::cerr << "File/stream error: " << error.what() << '\n';
        return 3;
    } catch (const fs::filesystem_error& error) {
        std::cerr << "File error: " << error.what() << '\n';
        return 3;
    } catch (const std::invalid_argument& error) {
        std::cerr << "Invalid input: " << error.what() << "\nUse --help for usage.\n";
        return 2;
    } catch (const GpuError& error) {
        std::cerr << "GPU run failed: " << error.what() << '\n';
        return 4;
    } catch (const std::exception& error) {
        std::cerr << "Runner failed: " << error.what() << '\n';
        return 3;
    }
}

#ifdef _WIN32
std::string utf8_argument(const wchar_t* argument) {
    const int bytes = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, argument, -1,
                                         nullptr, 0, nullptr, nullptr);
    if (bytes == 0) throw std::invalid_argument("command-line argument is not valid Unicode");
    std::string result(static_cast<std::size_t>(bytes), '\0');
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, argument, -1, result.data(), bytes,
                            nullptr, nullptr) != bytes) {
        throw std::invalid_argument("cannot convert command-line argument to UTF-8");
    }
    result.pop_back();
    return result;
}
#endif

}  // namespace

#ifdef _WIN32
int wmain(int argc, wchar_t** argv) {
    configure_terminal_utf8();
    try {
        std::vector<std::string> arguments;
        arguments.reserve(static_cast<std::size_t>(argc));
        for (int i = 0; i < argc; ++i) arguments.push_back(utf8_argument(argv[i]));
        return guarded_run(arguments);
    } catch (const std::exception& error) {
        std::cerr << "Invalid command line: " << error.what() << '\n';
        return 2;
    }
}
#else
int main(int argc, char** argv) {
    configure_terminal_utf8();
    return guarded_run(std::vector<std::string>(argv, argv + argc));
}
#endif
