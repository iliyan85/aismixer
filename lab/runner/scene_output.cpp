#include "scene_output.h"
#include "scene_validation.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <iomanip>
#include <ostream>
#include <streambuf>
#include <string>
#include <system_error>

#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace aismixer::lab::runner {
namespace {

void write_record(std::ostream& output, const EventIntent& event) {
    // The maximum decimal widths of all six integer fields fit in 128 bytes.
    // to_chars and write bypass locale grouping, hex/showpos and stream width.
    std::array<char, 128> row{};
    char* cursor = row.data();
    const auto append = [&cursor, &row](auto value) {
        const auto converted = std::to_chars(cursor, row.data() + row.size(), value);
        if (converted.ec != std::errc{}) {
            throw std::logic_error("CSV record exceeded its fixed buffer");
        }
        cursor = converted.ptr;
    };
    append(event.absolute_tick);
    *cursor++ = ',';
    append(event.actor_id);
    *cursor++ = ',';
    append(static_cast<std::uint32_t>(event.category));
    *cursor++ = ',';
    append(event.x_mm);
    *cursor++ = ',';
    append(event.y_mm);
    *cursor++ = ',';
    append(event.random_u32);
    *cursor++ = '\n';
    output.write(row.data(), static_cast<std::streamsize>(cursor - row.data()));
}

void write_header(std::ostream& output) {
    constexpr char header[] = "absolute_tick,actor_id,category,x_mm,y_mm,random_u32\n";
    output.write(header, sizeof(header) - 1);
}

void scene_details(std::ostream& output, const Scene& scene) {
    std::array<std::size_t, 6> counts{};
    for (const auto& actor : scene.actors) {
        const auto category = static_cast<std::size_t>(actor.category);
        if (category >= counts.size()) {
            throw std::invalid_argument("Cannot format an invalid actor category");
        }
        ++counts[category];
    }
    output << "scene=" << scene.name << '\n'
        << "actor_count=" << scene.actors.size() << " category_counts:";
    for (std::size_t i = 0; i < counts.size(); ++i) {
        output << ' ' << category_name(static_cast<ActorCategory>(i)) << '=' << counts[i];
    }
    const auto& run = scene.simulation;
    output << "\nseed=" << run.seed << " start_tick=" << run.start_tick
        << " tick_count=" << run.tick_count << " tick_seconds=" << run.tick_seconds << '\n';
}

// A single buffered file owns its handle from exclusive creation through close.
// It never reopens a temporary pathname after creation.
class TemporaryCsv {
public:
    explicit TemporaryCsv(const std::filesystem::path& target) {
        for (unsigned attempt = 0; attempt < 32; ++attempt) {
#ifdef _WIN32
            temporary_ = target.parent_path() / (target.filename().native()
                + L".aismixer-tmp-" + std::to_wstring(GetCurrentProcessId())
                + L"-" + std::to_wstring(attempt));
            handle_ = CreateFileW(temporary_.c_str(), GENERIC_WRITE, 0, nullptr,
                                  CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
            if (handle_ != INVALID_HANDLE_VALUE) {
                return;
            }
            const auto error = GetLastError();
            if (error != ERROR_FILE_EXISTS && error != ERROR_ALREADY_EXISTS) {
                throw FileError("Cannot create temporary CSV beside " + path_text(target)
                    + ": " + std::system_category().message(static_cast<int>(error)));
            }
#else
            temporary_ = target.parent_path() / (target.filename().string()
                + ".aismixer-tmp-" + std::to_string(getpid()) + "-" + std::to_string(attempt));
            handle_ = ::open(temporary_.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
            if (handle_ >= 0) {
                return;
            }
            if (errno != EEXIST) {
                throw FileError("Cannot create temporary CSV beside " + path_text(target)
                    + ": " + std::generic_category().message(errno));
            }
#endif
        }
        throw FileError("Cannot create an exclusive temporary CSV beside " + path_text(target)
            + ": all temporary names are occupied");
    }

    TemporaryCsv(const TemporaryCsv&) = delete;
    TemporaryCsv& operator=(const TemporaryCsv&) = delete;

    ~TemporaryCsv() noexcept {
#ifdef _WIN32
        if (handle_ != INVALID_HANDLE_VALUE) {
            CloseHandle(handle_);
        }
#else
        if (handle_ >= 0) {
            ::close(handle_);
        }
#endif
        if (!temporary_.empty()) {
            std::error_code ignored;
            std::filesystem::remove(temporary_, ignored);
        }
    }

    bool write(const char* bytes, std::size_t count) {
        while (count != 0) {
#ifdef _WIN32
            DWORD written = 0;
            const BOOL success = WriteFile(handle_, bytes, static_cast<DWORD>(count), &written, nullptr);
            if (!success || written == 0) {
                last_error_ = static_cast<int>(success ? ERROR_WRITE_FAULT : GetLastError());
                return false;
            }
#else
            const auto written = ::write(handle_, bytes, count);
            if (written < 0 && errno == EINTR) {
                continue;
            }
            if (written <= 0) {
                last_error_ = written == 0 ? EIO : errno;
                return false;
            }
#endif
            bytes += written;
            count -= written;
        }
        return true;
    }

    std::string error_text() const {
#ifdef _WIN32
        return std::system_category().message(last_error_);
#else
        return std::generic_category().message(last_error_);
#endif
    }

    void close_and_publish(const std::filesystem::path& target) {
#ifdef _WIN32
        if (!FlushFileBuffers(handle_)) {
            throw FileError("Cannot flush CSV for " + path_text(target) + ": "
                + std::system_category().message(static_cast<int>(GetLastError())));
        }
        if (!CloseHandle(handle_)) {
            throw FileError("Cannot close CSV for " + path_text(target) + ": "
                + std::system_category().message(static_cast<int>(GetLastError())));
        }
        handle_ = INVALID_HANDLE_VALUE;
        // Deliberately omit MOVEFILE_REPLACE_EXISTING: a late destination race
        // fails rather than overwriting another file.
        if (!MoveFileExW(temporary_.c_str(), target.c_str(), MOVEFILE_WRITE_THROUGH)) {
            throw FileError("Cannot publish CSV without overwriting " + path_text(target)
                + ": " + std::system_category().message(static_cast<int>(GetLastError())));
        }
        temporary_.clear();
#else
        if (::fsync(handle_) != 0) {
            throw FileError("Cannot flush CSV for " + path_text(target) + ": "
                + std::generic_category().message(errno));
        }
        const auto handle = handle_;
        handle_ = -1;
        if (::close(handle) != 0) {
            throw FileError("Cannot close CSV for " + path_text(target) + ": "
                + std::generic_category().message(errno));
        }
        // link() atomically refuses an existing target on the same filesystem.
        if (::link(temporary_.c_str(), target.c_str()) != 0) {
            throw FileError("Cannot publish CSV without overwriting " + path_text(target)
                + ": " + std::generic_category().message(errno));
        }
        // The destructor removes only our temporary link, leaving the target.
#endif
    }

private:
    std::filesystem::path temporary_;
    int last_error_ = 0;
#ifdef _WIN32
    HANDLE handle_ = INVALID_HANDLE_VALUE;
#else
    int handle_ = -1;
#endif
};

class CsvBuffer : public std::streambuf {
public:
    explicit CsvBuffer(TemporaryCsv& file) : file_(file) {
        setp(buffer_.data(), buffer_.data() + buffer_.size());
    }

protected:
    int sync() override { return flush() ? 0 : -1; }

    int_type overflow(int_type character) override {
        if (!flush()) {
            return traits_type::eof();
        }
        if (!traits_type::eq_int_type(character, traits_type::eof())) {
            *pptr() = traits_type::to_char_type(character);
            pbump(1);
        }
        return traits_type::not_eof(character);
    }

private:
    bool flush() {
        const auto count = static_cast<std::size_t>(pptr() - pbase());
        if (!file_.write(pbase(), count)) {
            return false;
        }
        setp(buffer_.data(), buffer_.data() + buffer_.size());
        return true;
    }

    TemporaryCsv& file_;
    std::array<char, 8192> buffer_{};
};

}  // namespace

std::uint64_t event_digest(const std::vector<EventIntent>& events) {
    std::uint64_t hash = UINT64_C(14695981039346656037);
    const auto append = [&hash](std::uint64_t word, unsigned width) {
        for (unsigned i = 0; i < width; ++i) {
            hash ^= word & UINT64_C(0xff);
            hash *= UINT64_C(1099511628211);
            word >>= 8;
        }
    };
    for (const auto& event : events) {
        append(event.absolute_tick, 8);
        append(event.actor_id, 4);
        append(static_cast<std::uint32_t>(event.category), 4);
        append(static_cast<std::uint64_t>(event.x_mm), 8);
        append(static_cast<std::uint64_t>(event.y_mm), 8);
        append(event.random_u32, 4);
    }
    return hash;
}

void write_csv(std::ostream& output, const std::vector<EventIntent>& events) {
    write_header(output);
    for (const auto& event : events) {
        write_record(output, event);
    }
}

void print_scene(std::ostream& output, const Scene& scene, std::size_t actor_preview_limit) {
    scene_details(output, scene);
    const auto shown = std::min(actor_preview_limit, scene.actors.size());
    output << "actors_shown=" << shown << '/' << scene.actors.size() << '\n';
    for (std::size_t i = 0; i < shown; ++i) {
        const auto& actor = scene.actors[i];
        output << "actor_id=" << actor.actor_id << " category=" << category_name(actor.category)
            << " position_mm=(" << actor.x_mm << ',' << actor.y_mm << ") velocity_mm_per_s=("
            << actor.vx_mm_per_s << ',' << actor.vy_mm_per_s << ") interval_ticks="
            << actor.report_interval_ticks << " phase_ticks=" << actor.phase_ticks << '\n';
    }
    if (shown < scene.actors.size()) {
        output << "Additional actors remain in the YAML file.\n";
    }
}

void print_summary(std::ostream& output, const Scene& scene,
                   const std::vector<EventIntent>& events, std::size_t preview_limit) {
    scene_details(output, scene);
    output << "event_count=" << events.size() << "\ndigest_fnv1a64="
        << std::hex << std::setfill('0') << std::setw(16) << event_digest(events)
        << std::dec << std::setfill(' ') << '\n';
    const auto shown = std::min(preview_limit, events.size());
    output << "preview_count=" << shown << '\n';
    if (shown != 0) {
        write_header(output);
        for (std::size_t i = 0; i < shown; ++i) {
            write_record(output, events[i]);
        }
    }
}

std::filesystem::path prepare_output_path(const std::filesystem::path& path) {
    if (path.empty()) {
        throw FileError("Output path must not be empty");
    }
    std::error_code error;
    const auto target = std::filesystem::absolute(path, error).lexically_normal();
    if (error) {
        throw FileError("Cannot resolve output path " + path_text(path) + ": " + error.message());
    }
    if (std::filesystem::exists(target, error)) {
        throw FileError("Refusing to overwrite existing output: " + path_text(target));
    }
    if (error) {
        throw FileError("Cannot inspect output path " + path_text(target) + ": " + error.message());
    }
    std::filesystem::create_directories(target.parent_path(), error);
    if (error) {
        throw FileError("Cannot create output directory " + path_text(target.parent_path())
            + ": " + error.message());
    }
    return target;
}

void export_csv(const std::filesystem::path& path, const std::vector<EventIntent>& events) {
    const auto target = prepare_output_path(path);
    TemporaryCsv file(target);
    CsvBuffer buffer(file);
    std::ostream output(&buffer);
    write_csv(output, events);
    output.flush();
    if (!output) {
        throw FileError("Cannot write complete CSV for " + path_text(target) + ": " + file.error_text());
    }
    file.close_and_publish(target);
}

}  // namespace aismixer::lab::runner
