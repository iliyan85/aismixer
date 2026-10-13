#include "scene_yaml.h"
#include "scene_validation.h"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <cerrno>
#include <fstream>
#include <initializer_list>
#include <map>
#include <string_view>
#include <system_error>
#include <type_traits>
#include <unordered_map>
#include <utility>

namespace aismixer::lab::runner {
namespace {

constexpr std::size_t kMaxYamlBytes = 1024 * 1024;

std::string location(const std::string& path, const YAML::Mark& mark) {
    std::string result = path;
    if (mark.line >= 0 && mark.column >= 0) {
        result += ":" + std::to_string(mark.line + 1) + ":" +
                  std::to_string(mark.column + 1);
    }
    return result;
}

[[noreturn]] void schema_error(const std::string& path, const YAML::Mark& mark,
                               std::string_view field, std::string_view explanation) {
    throw std::invalid_argument(location(path, mark) + ": " + std::string(field) +
                                ": " + std::string(explanation));
}

// Reject invalid encodings, overlong forms, surrogate encodings, and code
// points beyond U+10FFFF before the YAML parser sees the bounded input.
void validate_utf8(const std::string& content, const std::string& path) {
    std::size_t index = 0;
    while (index < content.size()) {
        const auto first = static_cast<unsigned char>(content[index]);
        std::size_t width = 0;
        if (first <= 0x7f) {
            width = 1;
        } else if (first >= 0xc2 && first <= 0xdf) {
            width = 2;
        } else if (first >= 0xe0 && first <= 0xef) {
            width = 3;
        } else if (first >= 0xf0 && first <= 0xf4) {
            width = 4;
        }
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
        if (!valid) {
            throw std::invalid_argument(path + ": UTF-8: invalid encoding at byte " +
                                        std::to_string(index + 1));
        }
        index += width;
    }
}

std::string read_yaml(const std::filesystem::path& path, const std::string& display_path) {
    errno = 0;
    // C++17's filesystem-path overload preserves Unicode filenames on Windows.
    std::ifstream input(path, std::ios::binary);
    if (!input.is_open()) {
        const int open_error = errno;
        throw FileError(display_path + ": cannot open YAML input" +
                        (open_error != 0 ? ": " +
                            std::error_code(open_error, std::generic_category()).message() : ""));
    }
    std::string content(kMaxYamlBytes + 1, '\0');
    input.read(content.data(), static_cast<std::streamsize>(content.size()));
    const auto bytes_read = static_cast<std::size_t>(input.gcount());
    if (input.bad() || (input.fail() && !input.eof())) {
        throw FileError(display_path + ": failed while reading YAML input");
    }
    if (bytes_read > kMaxYamlBytes) {
        throw std::invalid_argument(display_path + ": YAML input exceeds the 1 MiB size limit");
    }
    content.resize(bytes_read);
    input.clear();  // A bounded read ending at EOF normally sets failbit.
    input.close();
    if (input.fail()) {
        throw FileError(display_path + ": failed while closing YAML input");
    }
    validate_utf8(content, display_path);
    return content;
}

class SchemaReader {
public:
    explicit SchemaReader(std::string path) : path_(std::move(path)) {}

    Scene read(const YAML::Node& root) {
        const auto top = mapping(root, "document", {"schema_version", "name", "simulation", "actors"});
        const std::uint32_t version = number<std::uint32_t>(top.at("schema_version"), "schema_version");
        if (version != 1) {
            fail(top.at("schema_version"), "schema_version", "only schema_version 1 is supported");
        }
        Scene scene{};
        scene.name = scalar(top.at("name"), "name");
        const auto simulation = mapping(top.at("simulation"), "simulation",
                                        {"seed", "start_tick", "tick_count", "tick_seconds"});
        scene.simulation = RunParameters{
            number<std::uint64_t>(simulation.at("seed"), "simulation.seed"),
            number<std::uint64_t>(simulation.at("start_tick"), "simulation.start_tick"),
            number<std::uint64_t>(simulation.at("tick_count"), "simulation.tick_count"),
            number<std::uint32_t>(simulation.at("tick_seconds"), "simulation.tick_seconds")};
        const YAML::Node actors = top.at("actors");
        inspect(actors, "actors");
        if (!actors.IsSequence()) {
            fail(actors, "actors", "expected a sequence");
        }
        if (actors.size() > kMaxSceneActors) {
            fail(actors, "actors", "actor count exceeds " + std::to_string(kMaxSceneActors));
        }
        scene.actors.reserve(actors.size());
        for (std::size_t index = 0; index < actors.size(); ++index) {
            const std::string field = "actors[" + std::to_string(index) + "]";
            const auto fields = mapping(actors[index], field,
                {"actor_id", "category", "x_mm", "y_mm", "vx_mm_per_s", "vy_mm_per_s",
                 "report_interval_ticks", "phase_ticks"});
            const std::string category = scalar(fields.at("category"), field + ".category");
            ActorCategory category_value{};
            if (category == "ClassA") category_value = ActorCategory::ClassA;
            else if (category == "ClassB") category_value = ActorCategory::ClassB;
            else if (category == "BaseStation") category_value = ActorCategory::BaseStation;
            else if (category == "AtoN") category_value = ActorCategory::AtoN;
            else if (category == "SAR") category_value = ActorCategory::SAR;
            else if (category == "Emergency") category_value = ActorCategory::Emergency;
            else fail(fields.at("category"), field + ".category",
                      "unknown category; accepted names: ClassA, ClassB, BaseStation, AtoN, SAR, Emergency");
            scene.actors.push_back(Actor{
                number<std::uint32_t>(fields.at("actor_id"), field + ".actor_id"), category_value,
                number<std::int64_t>(fields.at("x_mm"), field + ".x_mm"),
                number<std::int64_t>(fields.at("y_mm"), field + ".y_mm"),
                number<std::int32_t>(fields.at("vx_mm_per_s"), field + ".vx_mm_per_s"),
                number<std::int32_t>(fields.at("vy_mm_per_s"), field + ".vy_mm_per_s"),
                number<std::uint32_t>(fields.at("report_interval_ticks"), field + ".report_interval_ticks"),
                number<std::uint32_t>(fields.at("phase_ticks"), field + ".phase_ticks")});
        }
        try {
            validate_scene(scene);
        } catch (const std::invalid_argument& error) {
            const std::string message = error.what();
            const std::size_t separator = message.find(": ");
            const std::string field = message.substr(0, separator);
            const auto mark = field_marks_.find(field);
            schema_error(path_, mark != field_marks_.end() ? mark->second : root.Mark(), field,
                         separator == std::string::npos ? message : message.substr(separator + 2));
        }
        return scene;
    }

private:
    using Mapping = std::map<std::string, YAML::Node>;

    [[noreturn]] void fail(const YAML::Node& node, std::string_view field,
                           std::string_view explanation) const {
        schema_error(path_, node.Mark(), field, explanation);
    }

    void inspect(const YAML::Node& node, const std::string& field, bool record_field = true) {
        if (!node.IsDefined()) {
            schema_error(path_, YAML::Mark::null_mark(), field, "required node is missing");
        }
        const std::string tag = node.Tag();
        if (tag != "?" && tag != "!") {
            fail(node, field, "explicit/custom YAML tags are not supported");
        }
        const YAML::Mark mark = node.Mark();
        if (record_field) {
            field_marks_[field] = mark;
        }
        // yaml-cpp aliases share node identity and the anchor's source mark.
        // Index by source position, checking identities only in that small
        // bucket; this avoids a quadratic scan and rejects cycles without
        // recursively traversing untrusted YAML graphs.
        if (mark.pos >= 0) {
            auto& at_position = visited_[mark.pos];
            for (const YAML::Node& prior : at_position) {
                if (prior.is(node)) {
                    fail(node, field, "YAML aliases are not supported");
                }
            }
            at_position.push_back(node);
        }
    }

    Mapping mapping(const YAML::Node& node, const std::string& field,
                    std::initializer_list<std::string_view> required) {
        inspect(node, field);
        if (!node.IsMap()) {
            fail(node, field, "expected a mapping");
        }
        Mapping result;
        for (const auto& entry : node) {
            const std::string key_field = entry.first.IsScalar()
                ? field + "." + entry.first.Scalar() : field + " key";
            inspect(entry.first, key_field, false);
            if (!entry.first.IsScalar()) {
                fail(entry.first, field, "mapping keys must be scalar field names");
            }
            const std::string key = entry.first.Scalar();
            if (std::find(required.begin(), required.end(), key) == required.end()) {
                fail(entry.first, field + "." + key, "unknown field");
            }
            if (!result.emplace(key, entry.second).second) {
                fail(entry.first, field + "." + key, "duplicate mapping key");
            }
        }
        for (const auto key : required) {
            if (result.find(std::string(key)) == result.end()) {
                fail(node, field + "." + std::string(key), "required field is missing");
            }
        }
        return result;
    }

    std::string scalar(const YAML::Node& node, const std::string& field) {
        inspect(node, field);
        if (!node.IsScalar()) {
            fail(node, field, "expected a scalar value");
        }
        return node.Scalar();
    }

    template <typename Integer>
    Integer number(const YAML::Node& node, const std::string& field) {
        const std::string value = scalar(node, field);
        try {
            if constexpr (std::is_same_v<Integer, std::uint64_t>) {
                return parse_u64(value, field);
            } else if constexpr (std::is_same_v<Integer, std::uint32_t>) {
                return parse_u32(value, field);
            } else if constexpr (std::is_same_v<Integer, std::int64_t>) {
                return parse_i64(value, field);
            } else {
                return parse_i32(value, field);
            }
        } catch (const std::invalid_argument& error) {
            const std::string message = error.what();
            const std::size_t separator = message.find(": ");
            fail(node, field, separator == std::string::npos ? message : message.substr(separator + 2));
        }
    }

    std::string path_;
    std::map<std::string, YAML::Mark> field_marks_;
    std::unordered_map<int, std::vector<YAML::Node>> visited_;
};

}  // namespace

Scene load_scene(const std::filesystem::path& path) {
    std::error_code path_error;
    const auto resolved = std::filesystem::absolute(path, path_error).lexically_normal();
    if (path_error) {
        throw FileError(path_text(path) + ": cannot resolve input path: " + path_error.message());
    }
    const std::string display_path = path_text(resolved);
    const std::string content = read_yaml(resolved, display_path);
    try {
        const std::vector<YAML::Node> documents = YAML::LoadAll(content);
        if (documents.size() != 1) {
            schema_error(display_path, documents.size() > 1 ? documents[1].Mark() : YAML::Mark::null_mark(),
                         "document", "expected exactly one YAML document");
        }
        return SchemaReader(display_path).read(documents.front());
    } catch (const YAML::Exception& error) {
        schema_error(display_path, error.mark, "YAML", error.msg);
    }
}

}  // namespace aismixer::lab::runner
