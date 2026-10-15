#include "geo_output.h"

#include "ais_message.h"
#include "nmea0183.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <functional>
#include <stdexcept>
#include <streambuf>
#include <string>
#include <system_error>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace aismixer::lab::geo {
namespace {

enum class Failure { None, Write, Flush, Close, Publish };
struct Hooks {
    Failure failure = Failure::None;
    std::size_t fail_write_after_bytes = 13;
    std::function<void(const std::filesystem::path&, const std::filesystem::path&)> before_publish;
};

std::string error_text(int error) {
#ifdef _WIN32
    return std::system_category().message(error);
#else
    return std::generic_category().message(error);
#endif
}

class TemporaryNmea {
public:
    TemporaryNmea(const std::filesystem::path& target, const Hooks& hooks) : hooks_(hooks) {
        for (unsigned attempt = 0; attempt < 32; ++attempt) {
#ifdef _WIN32
            temporary_ = target.parent_path() / (target.filename().native() + L".aismixer-tmp-" +
                std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(attempt));
            handle_ = CreateFileW(temporary_.c_str(), GENERIC_WRITE, 0, nullptr,
                                  CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
            if (handle_ != INVALID_HANDLE_VALUE) return;
            const auto error = GetLastError();
            if (error != ERROR_FILE_EXISTS && error != ERROR_ALREADY_EXISTS) {
                throw std::runtime_error("Cannot create exclusive NMEA temporary beside " + target.u8string() +
                                         ": " + error_text(static_cast<int>(error)));
            }
#else
            temporary_ = target.parent_path() / (target.filename().string() + ".aismixer-tmp-" +
                std::to_string(getpid()) + "-" + std::to_string(attempt));
            handle_ = ::open(temporary_.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
            if (handle_ >= 0) return;
            if (errno != EEXIST) {
                throw std::runtime_error("Cannot create exclusive NMEA temporary beside " + target.u8string() +
                                         ": " + error_text(errno));
            }
#endif
        }
        throw std::runtime_error("Cannot create NMEA temporary: all bounded exclusive names are occupied");
    }
    TemporaryNmea(const TemporaryNmea&) = delete;
    TemporaryNmea& operator=(const TemporaryNmea&) = delete;
    ~TemporaryNmea() noexcept {
#ifdef _WIN32
        if (handle_ != INVALID_HANDLE_VALUE) CloseHandle(handle_);
#else
        if (handle_ >= 0) ::close(handle_);
#endif
        if (!temporary_.empty()) {
            std::error_code ignored;
            std::filesystem::remove(temporary_, ignored);
        }
    }

    bool write(const char* bytes, std::size_t count) {
        while (count != 0) {
            std::size_t requested = count;
            if (hooks_.failure == Failure::Write) {
                if (bytes_written_ >= hooks_.fail_write_after_bytes) {
                    last_error_ = "injected temporary write failure";
                    return false;
                }
                requested = std::min(requested, hooks_.fail_write_after_bytes - bytes_written_);
            }
#ifdef _WIN32
            DWORD written = 0;
            const auto chunk = static_cast<DWORD>(std::min<std::size_t>(requested, MAXDWORD));
            const BOOL success = WriteFile(handle_, bytes, chunk, &written, nullptr);
            if (!success || written == 0) {
                last_error_ = error_text(static_cast<int>(success ? ERROR_WRITE_FAULT : GetLastError()));
                return false;
            }
#else
            const auto written = ::write(handle_, bytes, requested);
            if (written < 0 && errno == EINTR) continue;
            if (written <= 0) {
                last_error_ = error_text(written == 0 ? EIO : errno);
                return false;
            }
#endif
            bytes += written;
            count -= static_cast<std::size_t>(written);
            bytes_written_ += static_cast<std::size_t>(written);
        }
        return true;
    }

    const std::string& last_error() const { return last_error_; }

    void close_and_publish(const std::filesystem::path& target) {
        if (hooks_.failure == Failure::Flush) throw std::runtime_error("Injected NMEA temporary flush failure");
#ifdef _WIN32
        if (!FlushFileBuffers(handle_)) {
            throw std::runtime_error("Cannot flush NMEA temporary: " + error_text(static_cast<int>(GetLastError())));
        }
        if (!CloseHandle(handle_)) {
            throw std::runtime_error("Cannot close NMEA temporary: " + error_text(static_cast<int>(GetLastError())));
        }
        handle_ = INVALID_HANDLE_VALUE;
#else
        if (::fsync(handle_) != 0) throw std::runtime_error("Cannot flush NMEA temporary: " + error_text(errno));
        const auto closing = handle_;
        handle_ = -1;
        if (::close(closing) != 0) throw std::runtime_error("Cannot close NMEA temporary: " + error_text(errno));
#endif
        if (hooks_.failure == Failure::Close) throw std::runtime_error("Injected NMEA temporary close failure");
        if (hooks_.before_publish) hooks_.before_publish(temporary_, target);
        if (hooks_.failure == Failure::Publish) throw std::runtime_error("Injected NMEA publication failure");
#ifdef _WIN32
        // Same-directory rename; deliberately no MOVEFILE_REPLACE_EXISTING or
        // MOVEFILE_COPY_ALLOWED. An intervening destination cannot be clobbered.
        if (!MoveFileExW(temporary_.c_str(), target.c_str(), MOVEFILE_WRITE_THROUGH)) {
            throw std::runtime_error("Cannot publish NMEA without overwriting " + target.u8string() +
                                     ": " + error_text(static_cast<int>(GetLastError())));
        }
        temporary_.clear();
#else
        if (::link(temporary_.c_str(), target.c_str()) != 0) {
            throw std::runtime_error("Cannot publish NMEA without overwriting " + target.u8string() +
                                     ": " + error_text(errno));
        }
        // Destruction removes only our temporary hard link, not the destination.
#endif
    }

private:
    const Hooks& hooks_;
    std::filesystem::path temporary_;
    std::string last_error_;
    std::size_t bytes_written_ = 0;
#ifdef _WIN32
    HANDLE handle_ = INVALID_HANDLE_VALUE;
#else
    int handle_ = -1;
#endif
};

class NmeaBuffer : public std::streambuf {
public:
    explicit NmeaBuffer(TemporaryNmea& file) : file_(file) { reset(); }
protected:
    int sync() override { return flush() ? 0 : -1; }
    int_type overflow(int_type character) override {
        if (!flush()) return traits_type::eof();
        if (!traits_type::eq_int_type(character, traits_type::eof())) {
            *pptr() = traits_type::to_char_type(character);
            pbump(1);
        }
        return traits_type::not_eof(character);
    }
private:
    void reset() { setp(buffer_.data(), buffer_.data() + buffer_.size()); }
    bool flush() {
        if (!file_.write(pbase(), static_cast<std::size_t>(pptr() - pbase()))) return false;
        reset();
        return true;
    }
    TemporaryNmea& file_;
    std::array<char, 8192> buffer_{};
};

void export_impl(const std::filesystem::path& path,
                 const std::vector<OfflineTransmission>& transmissions, const Hooks& hooks) {
    const auto target = prepare_nmea_output(path);
    TemporaryNmea file(target, hooks);
    NmeaBuffer buffer(file);
    std::ostream output(&buffer);
    for (const auto& transmission : transmissions) ais::write_sentences(output, transmission.sentences);
    output.flush();
    if (!output) throw std::runtime_error("Cannot write/flush complete NMEA temporary: " + file.last_error());
    file.close_and_publish(target);
}

}  // namespace

std::filesystem::path prepare_nmea_output(const std::filesystem::path& path) {
    if (path.empty() || path.filename().empty()) throw std::invalid_argument("NMEA destination must name a nonempty file");
    std::error_code error;
    const auto target = std::filesystem::absolute(path, error).lexically_normal();
    if (error) throw std::runtime_error("Cannot resolve NMEA destination: " + error.message());
    const auto status = std::filesystem::symlink_status(target, error);
    if (error && error != std::errc::no_such_file_or_directory) {
        throw std::runtime_error("Cannot inspect NMEA destination: " + error.message());
    }
    if (std::filesystem::exists(status)) throw std::invalid_argument("NMEA destination already exists: " + target.u8string());
    error.clear();
    if (!std::filesystem::is_directory(target.parent_path(), error) || error) {
        throw std::runtime_error("NMEA destination directory does not exist or is inaccessible: " + target.parent_path().u8string());
    }
    return target;
}

void export_nmea(const std::filesystem::path& path,
                 const std::vector<OfflineTransmission>& transmissions) {
    export_impl(path, transmissions, Hooks{});
}

void write_event_metadata(std::ostream& output, const std::vector<GeoEvent>& events,
                          const std::vector<Identity>& identities) {
    if (identities.size() > kMaxSceneActors || events.size() > kMaxSceneCandidates) {
        throw std::invalid_argument("event metadata exceeds bounded population/events");
    }
    for (std::size_t index = 0; index < identities.size(); ++index) {
        if (index != 0 && identities[index - 1].actor_id >= identities[index].actor_id) {
            throw std::invalid_argument("event metadata requires unique ascending actor identities");
        }
        (void)ais::Mmsi(identities[index].mmsi);
    }
    for (const auto& event : events) {
        const auto identity = std::lower_bound(identities.begin(), identities.end(), event.actor_id,
            [](const Identity& current, std::uint32_t id) { return current.actor_id < id; });
        if (identity == identities.end() || identity->actor_id != event.actor_id) {
            throw std::invalid_argument("event metadata: missing actor identity " + std::to_string(event.actor_id));
        }
        std::array<char, 160> row{};
        char* cursor = row.data();
        constexpr std::string_view prefix = "event=";
        cursor = std::copy(prefix.begin(), prefix.end(), cursor);
        const auto number = [&](auto value) {
            const auto converted = std::to_chars(cursor, row.data() + row.size(), value);
            if (converted.ec != std::errc{}) throw std::logic_error("event metadata exceeded bounded row buffer");
            cursor = converted.ptr;
            *cursor++ = ',';
        };
        number(event.absolute_tick);
        number(event.simulation_seconds);
        number(event.actor_id);
        number(static_cast<std::uint32_t>(event.category));
        number(event.location.longitude_ndeg);
        number(event.location.latitude_ndeg);
        cursor = std::copy(identity->mmsi.begin(), identity->mmsi.end(), cursor);
        *cursor++ = '\n';
        output.write(row.data(), cursor - row.data());
        if (!output) throw std::runtime_error("Cannot write complete event identity metadata");
    }
}

#ifdef AISMIXER_LAB_GEO_OUTPUT_TESTING
namespace testing {
void export_nmea_with_hooks(const std::filesystem::path& path,
                            const std::vector<OfflineTransmission>& transmissions,
                            const OutputHooks& requested) {
    Hooks hooks;
    switch (requested.failure) {
        case OutputFailure::None: break;
        case OutputFailure::Write: hooks.failure = Failure::Write; break;
        case OutputFailure::Flush: hooks.failure = Failure::Flush; break;
        case OutputFailure::Close: hooks.failure = Failure::Close; break;
        case OutputFailure::Publish: hooks.failure = Failure::Publish; break;
    }
    hooks.fail_write_after_bytes = requested.fail_write_after_bytes;
    hooks.before_publish = requested.before_publish;
    export_impl(path, transmissions, hooks);
}
}  // namespace testing
#endif

}  // namespace aismixer::lab::geo
