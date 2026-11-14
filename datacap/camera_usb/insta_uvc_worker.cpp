#include <linux/fs.h>
#include <linux/videodev2.h>

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavcodec/version.h>
#include <libavutil/avutil.h>
#include <libavutil/error.h>
#include <libavutil/frame.h>
#include <libavutil/opt.h>
#include <libavutil/version.h>
#include <libswscale/swscale.h>
#include <libswscale/version.h>
}

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <exception>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace fs = std::filesystem;

namespace {

constexpr const char *kProtocolPrefix = "DATACAP_UVC_JSON ";
constexpr const char *kProtocolName = "instadatacap-uvc-worker-v1";
constexpr const char *kCaptureSchema = "instadatacap.usb_webcam.capture.v1";

struct CaptureProfile {
  const char *view_mode;
  uint32_t width;
  uint32_t height;
  int preview_width;
  int preview_height;
  const char *semantic;
  const char *projection;
  const char *layout;
  const char *camera_processing;
};

struct CameraIdentity {
  std::string model;
  bool flowstate_supported_in_webcam = false;
  std::string flowstate_status;
};

constexpr CaptureProfile kPanoramaProfile{
    "panorama",
    2880,
    1440,
    960,
    480,
    "in_camera_2to1_panorama",
    "equirectangular_2to1",
    "stitched_2to1",
    "in_camera_stitch_and_stabilization_baked_into_pixels",
};

constexpr CaptureProfile kFlatProfile{
    "flat",
    1920,
    1080,
    960,
    540,
    "in_camera_native_flat",
    "native_flat_profile",
    "dual_fisheye_vertical_stack",
    "in_camera_native_usb_profile_baked_into_pixels_no_host_projection",
};

constexpr uint32_t kFps = 30;
constexpr uint32_t kBufferCount = 4;
constexpr int kPreviewFps = 10;
constexpr auto kPreviewPeriod = std::chrono::milliseconds(1000 / kPreviewFps);
constexpr int64_t kPreviewPeriodNs = 1000000000LL / kPreviewFps;
constexpr double kPreviewReadyTimeoutSec = 5.0;
constexpr int kStableIntervals = 8;
constexpr double kWarmupTimeoutSec = 10.0;
constexpr double kMaxDriverAgeMs = 75.0;
constexpr double kStartupMaxSourceIntervalMs = (1000.0 / kFps) * 1.8;
constexpr double kPostReadyHardMaxSourceIntervalMs = 500.0;
constexpr size_t kRollingFpsWindowFrames = 300;
constexpr double kRollingFpsMinimum = 27.0;
constexpr double kRollingFpsMaximum = 33.0;
constexpr double kFrameStallTimeoutSec = 2.0;
constexpr size_t kWriterQueueCapacity = 8;
constexpr size_t kMaxMjpegPayloadBytes = 16U * 1024U * 1024U;
constexpr size_t kMaxStdinBufferBytes = 64U * 1024U;

std::atomic<bool> g_stop{false};
std::atomic<int> g_signal_number{0};
std::atomic<bool> g_abort_command{false};
std::mutex g_stdout_mutex;
std::string g_protocol_role;

[[noreturn]] void Fail(const std::string &message) {
  throw std::runtime_error(message);
}

CaptureProfile CaptureProfileForViewMode(const std::string &view_mode) {
  if (view_mode == kPanoramaProfile.view_mode)
    return kPanoramaProfile;
  if (view_mode == kFlatProfile.view_mode)
    return kFlatProfile;
  Fail("--view-mode must be 'panorama' or 'flat'");
}

void SignalHandler(int signal_number) {
  g_signal_number.store(signal_number, std::memory_order_relaxed);
  g_stop.store(true, std::memory_order_relaxed);
}

int Xioctl(int fd, unsigned long request, void *argument) {
  int result = 0;
  do {
    result = ioctl(fd, request, argument);
  } while (result < 0 && errno == EINTR);
  return result;
}

int64_t MonotonicNs() {
  timespec value{};
  if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) {
    Fail(std::string("clock_gettime(CLOCK_MONOTONIC) failed: ") +
         std::strerror(errno));
  }
  return static_cast<int64_t>(value.tv_sec) * 1000000000LL + value.tv_nsec;
}

int64_t MonotonicRawNs() {
  timespec value{};
  if (clock_gettime(CLOCK_MONOTONIC_RAW, &value) != 0) {
    Fail(std::string("clock_gettime(CLOCK_MONOTONIC_RAW) failed: ") +
         std::strerror(errno));
  }
  return static_cast<int64_t>(value.tv_sec) * 1000000000LL + value.tv_nsec;
}

struct MonotonicRawMapping {
  int64_t monotonic_before_ns = 0;
  int64_t raw_ns = 0;
  int64_t monotonic_after_ns = 0;
  int64_t raw_minus_monotonic_ns = 0;
  int64_t error_bound_ns = 0;
};

MonotonicRawMapping SampleMonotonicRawMapping() {
  // Bracket the RAW sample in CLOCK_MONOTONIC. The RAW-to-MONOTONIC offset is
  // estimated at the MONOTONIC interval midpoint; half the bracket width
  // (rounded up) is a conservative sampling-time error bound.
  MonotonicRawMapping result;
  result.monotonic_before_ns = MonotonicNs();
  result.raw_ns = MonotonicRawNs();
  result.monotonic_after_ns = MonotonicNs();
  if (result.monotonic_after_ns < result.monotonic_before_ns)
    Fail("CLOCK_MONOTONIC moved backwards while sampling RAW mapping");
  const int64_t bracket_ns =
      result.monotonic_after_ns - result.monotonic_before_ns;
  const int64_t midpoint_ns =
      result.monotonic_before_ns + bracket_ns / 2;
  result.raw_minus_monotonic_ns = result.raw_ns - midpoint_ns;
  result.error_bound_ns = (bracket_ns + 1) / 2;
  return result;
}

int64_t UnixTimeNs() {
  timespec value{};
  if (clock_gettime(CLOCK_REALTIME, &value) != 0) {
    Fail(std::string("clock_gettime(CLOCK_REALTIME) failed: ") +
         std::strerror(errno));
  }
  return static_cast<int64_t>(value.tv_sec) * 1000000000LL + value.tv_nsec;
}

int64_t TimevalNs(const timeval &value) {
  return static_cast<int64_t>(value.tv_sec) * 1000000000LL +
         static_cast<int64_t>(value.tv_usec) * 1000LL;
}

double ElapsedMs(int64_t start_ns, int64_t end_ns) {
  return static_cast<double>(end_ns - start_ns) / 1.0e6;
}

std::string JsonEscape(const std::string &value) {
  std::ostringstream output;
  output << '"';
  for (const unsigned char character : value) {
    switch (character) {
    case '"':
      output << "\\\"";
      break;
    case '\\':
      output << "\\\\";
      break;
    case '\b':
      output << "\\b";
      break;
    case '\f':
      output << "\\f";
      break;
    case '\n':
      output << "\\n";
      break;
    case '\r':
      output << "\\r";
      break;
    case '\t':
      output << "\\t";
      break;
    default:
      if (character < 0x20) {
        output << "\\u" << std::hex << std::setw(4) << std::setfill('0')
               << static_cast<int>(character) << std::dec;
      } else {
        output << static_cast<char>(character);
      }
    }
  }
  output << '"';
  return output.str();
}

std::string CaptureProfileJson(const CaptureProfile &profile,
                               bool include_preview_geometry = true) {
  std::ostringstream output;
  output << "\"view_mode\":" << JsonEscape(profile.view_mode)
         << ",\"width\":" << profile.width
         << ",\"height\":" << profile.height
         << ",\"projection\":" << JsonEscape(profile.projection)
         << ",\"layout\":" << JsonEscape(profile.layout)
         << ",\"semantic\":" << JsonEscape(profile.semantic);
  if (include_preview_geometry) {
    output << ",\"preview_width\":" << profile.preview_width
           << ",\"preview_height\":" << profile.preview_height;
  }
  return output.str();
}

CameraIdentity CameraIdentityForCard(const std::string &card_name,
                                     const CaptureProfile &profile) {
  if (card_name.find("Insta360 X5") != std::string::npos) {
    return CameraIdentity{
        "Insta360 X5",
        true,
        profile.view_mode == std::string(kPanoramaProfile.view_mode)
            ? "in_camera_processing_baked_into_uvc_pixels_not_controllable_or_verified_via_uvc"
            : "not_verified_for_native_flat_uvc_profile",
    };
  }
  if (card_name.find("Insta360 X3") != std::string::npos) {
    if (profile.view_mode == std::string(kPanoramaProfile.view_mode)) {
      Fail("Insta360 X3 Webcam does not expose the required MJPG "
           "2880x1440 stitched panorama profile; select --view-mode flat "
           "for its native 1920x1080 dual-fisheye stream");
    }
    return CameraIdentity{
        "Insta360 X3",
        false,
        "unsupported_in_x3_webcam",
    };
  }
  Fail("capture node is not an explicitly supported Insta360 X3/X5 Webcam: " +
       card_name);
}

std::string CameraIdentityJson(const CameraIdentity &identity) {
  std::ostringstream output;
  output << "\"camera_model\":" << JsonEscape(identity.model)
         << ",\"flowstate_supported_in_webcam\":"
         << (identity.flowstate_supported_in_webcam ? "true" : "false")
         << ",\"flowstate_verified\":false"
         << ",\"flowstate_status\":"
         << JsonEscape(identity.flowstate_status);
  return output.str();
}

void Emit(const std::string &action, bool ok, const std::string &message,
          const std::string &extra_json = {}) {
  std::lock_guard<std::mutex> lock(g_stdout_mutex);
  std::cout << kProtocolPrefix << "{\"protocol\":" << JsonEscape(kProtocolName)
            << ",\"role\":" << JsonEscape(g_protocol_role)
            << ",\"action\":" << JsonEscape(action)
            << ",\"ok\":" << (ok ? "true" : "false")
            << ",\"message\":" << JsonEscape(message);
  if (!extra_json.empty()) {
    std::cout << ',' << extra_json;
  }
  std::cout << "}" << std::endl;
}

std::string FourccText(uint32_t code) {
  char value[5] = {
      static_cast<char>(code & 0xff),
      static_cast<char>((code >> 8) & 0xff),
      static_cast<char>((code >> 16) & 0xff),
      static_cast<char>((code >> 24) & 0xff),
      '\0',
  };
  return value;
}

void WriteAll(int fd, const uint8_t *data, size_t size) {
  size_t offset = 0;
  while (offset < size) {
    const ssize_t written = write(fd, data + offset, size - offset);
    if (written < 0 && errno == EINTR)
      continue;
    if (written <= 0) {
      Fail(std::string("write failed: ") + std::strerror(errno));
    }
    offset += static_cast<size_t>(written);
  }
}

void WriteAll(int fd, const std::string &value) {
  WriteAll(fd, reinterpret_cast<const uint8_t *>(value.data()), value.size());
}

void CloseFd(int *fd) {
  if (*fd < 0)
    return;
  // POSIX leaves the descriptor state unspecified after close() returns
  // EINTR. Retrying can accidentally close a descriptor reused by another
  // thread, so relinquish ownership first and issue close exactly once.
  const int owned_fd = *fd;
  *fd = -1;
  (void)close(owned_fd);
}

void FsyncFd(int fd, const std::string &label) {
  int result = 0;
  do {
    result = fsync(fd);
  } while (result < 0 && errno == EINTR);
  if (result != 0) {
    Fail("fsync failed for " + label + ": " + std::strerror(errno));
  }
}

void FsyncDirectory(const fs::path &directory) {
  const int fd = open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (fd < 0) {
    Fail("cannot open output directory for fsync: " + directory.string() +
         ": " + std::strerror(errno));
  }
  int result = 0;
  do {
    result = fsync(fd);
  } while (result < 0 && errno == EINTR);
  const int saved_errno = errno;
  int owned_fd = fd;
  CloseFd(&owned_fd);
  if (result != 0) {
    Fail("directory fsync failed for " + directory.string() + ": " +
         std::strerror(saved_errno));
  }
}

int OpenExclusive(const fs::path &path) {
  const int fd = open(
      path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0644);
  if (fd < 0) {
    Fail("refusing to overwrite output " + path.string() + ": " +
         std::strerror(errno));
  }
  return fd;
}

int RenameNoReplaceRaw(const fs::path &source, const fs::path &destination) {
#ifdef SYS_renameat2
  return static_cast<int>(syscall(SYS_renameat2, AT_FDCWD, source.c_str(),
                                  AT_FDCWD, destination.c_str(),
                                  RENAME_NOREPLACE));
#else
  errno = ENOSYS;
  return -1;
#endif
}

void RenameNoReplace(const fs::path &source, const fs::path &destination,
                     const std::string &label) {
  if (RenameNoReplaceRaw(source, destination) != 0) {
    Fail(label + ": " + std::strerror(errno));
  }
}

bool IsStrictlyWithin(const fs::path &root, const fs::path &candidate) {
  const fs::path relative = candidate.lexically_relative(root);
  if (relative.empty() || relative == ".")
    return false;
  for (const auto &component : relative) {
    if (component == "..")
      return false;
  }
  return !relative.is_absolute();
}

void EnsureDirectoryWithoutSymlink(const fs::path &directory) {
  std::error_code error;
  fs::file_status status = fs::symlink_status(directory, error);
  if (error && error != std::errc::no_such_file_or_directory) {
    Fail("cannot inspect output directory component " + directory.string() +
         ": " + error.message());
  }
  if (fs::exists(status)) {
    if (fs::is_symlink(status))
      Fail("refusing symlink output directory component: " +
           directory.string());
    if (!fs::is_directory(status))
      Fail("output path component is not a directory: " + directory.string());
    return;
  }
  error.clear();
  if (!fs::create_directory(directory, error) || error) {
    Fail("cannot create output directory component " + directory.string() +
         ": " + error.message());
  }
  status = fs::symlink_status(directory, error);
  if (error || fs::is_symlink(status) || !fs::is_directory(status)) {
    Fail("new output directory component failed no-symlink validation: " +
         directory.string());
  }
}

class ScopedFdFlags {
public:
  ScopedFdFlags(int fd, int original_flags)
      : fd_(fd), original_flags_(original_flags) {}
  ~ScopedFdFlags() {
    if (original_flags_ >= 0)
      (void)fcntl(fd_, F_SETFL, original_flags_);
  }

  ScopedFdFlags(const ScopedFdFlags &) = delete;
  ScopedFdFlags &operator=(const ScopedFdFlags &) = delete;

private:
  int fd_;
  int original_flags_;
};

std::string Trim(const std::string &input) {
  const auto first = input.find_first_not_of(" \t\r\n");
  if (first == std::string::npos)
    return {};
  const auto last = input.find_last_not_of(" \t\r\n");
  return input.substr(first, last - first + 1);
}

int HexDigit(char value) {
  if (value >= '0' && value <= '9')
    return value - '0';
  if (value >= 'a' && value <= 'f')
    return value - 'a' + 10;
  if (value >= 'A' && value <= 'F')
    return value - 'A' + 10;
  return -1;
}

uint32_t DecodeHex4(const std::string &text, size_t offset) {
  if (offset + 4 > text.size())
    Fail("truncated JSON unicode escape");
  uint32_t result = 0;
  for (size_t index = 0; index < 4; ++index) {
    const int digit = HexDigit(text[offset + index]);
    if (digit < 0)
      Fail("invalid JSON unicode escape");
    result = result * 16 + static_cast<uint32_t>(digit);
  }
  return result;
}

void AppendUtf8(std::string *output, uint32_t codepoint) {
  if (codepoint <= 0x7f) {
    output->push_back(static_cast<char>(codepoint));
  } else if (codepoint <= 0x7ff) {
    output->push_back(static_cast<char>(0xc0 | (codepoint >> 6)));
    output->push_back(static_cast<char>(0x80 | (codepoint & 0x3f)));
  } else if (codepoint <= 0xffff) {
    output->push_back(static_cast<char>(0xe0 | (codepoint >> 12)));
    output->push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3f)));
    output->push_back(static_cast<char>(0x80 | (codepoint & 0x3f)));
  } else if (codepoint <= 0x10ffff) {
    output->push_back(static_cast<char>(0xf0 | (codepoint >> 18)));
    output->push_back(static_cast<char>(0x80 | ((codepoint >> 12) & 0x3f)));
    output->push_back(static_cast<char>(0x80 | ((codepoint >> 6) & 0x3f)));
    output->push_back(static_cast<char>(0x80 | (codepoint & 0x3f)));
  } else {
    Fail("JSON unicode codepoint is out of range");
  }
}

std::string DecodeJsonString(const std::string &encoded) {
  const std::string text = Trim(encoded);
  if (text.size() < 2 || text.front() != '"' || text.back() != '"') {
    Fail("START argument must be exactly one JSON string");
  }
  std::string output;
  for (size_t index = 1; index + 1 < text.size(); ++index) {
    const unsigned char character = static_cast<unsigned char>(text[index]);
    if (character < 0x20)
      Fail("unescaped control character in JSON string");
    if (character != '\\') {
      output.push_back(static_cast<char>(character));
      continue;
    }
    if (++index + 1 >= text.size())
      Fail("truncated JSON string escape");
    const char escaped = text[index];
    switch (escaped) {
    case '"':
      output.push_back('"');
      break;
    case '\\':
      output.push_back('\\');
      break;
    case '/':
      output.push_back('/');
      break;
    case 'b':
      output.push_back('\b');
      break;
    case 'f':
      output.push_back('\f');
      break;
    case 'n':
      output.push_back('\n');
      break;
    case 'r':
      output.push_back('\r');
      break;
    case 't':
      output.push_back('\t');
      break;
    case 'u': {
      uint32_t codepoint = DecodeHex4(text, index + 1);
      index += 4;
      if (codepoint >= 0xd800 && codepoint <= 0xdbff) {
        if (index + 6 >= text.size() || text[index + 1] != '\\' ||
            text[index + 2] != 'u') {
          Fail("JSON high surrogate is missing its low surrogate");
        }
        const uint32_t low = DecodeHex4(text, index + 3);
        if (low < 0xdc00 || low > 0xdfff)
          Fail("invalid JSON low surrogate");
        codepoint = 0x10000 + ((codepoint - 0xd800) << 10) + (low - 0xdc00);
        index += 6;
      } else if (codepoint >= 0xdc00 && codepoint <= 0xdfff) {
        Fail("unexpected JSON low surrogate");
      }
      AppendUtf8(&output, codepoint);
      break;
    }
    default:
      Fail("unsupported JSON string escape");
    }
  }
  if (output.empty())
    Fail("session directory must not be empty");
  if (output.find('\0') != std::string::npos)
    Fail("session directory contains NUL");
  return output;
}

std::string AvError(int code) {
  char buffer[AV_ERROR_MAX_STRING_SIZE]{};
  av_strerror(code, buffer, sizeof(buffer));
  return buffer;
}

enum class FrameWaitTimeout {
  kNone,
  kStartupWarmup,
  kReadyStall,
};

enum class SourceIntervalClass {
  kNominal,
  kAllowedLongJitter,
  kFatal,
};

SourceIntervalClass ClassifyPostReadySourceInterval(double interval_ms) {
  if (!std::isfinite(interval_ms) || interval_ms <= 0.0 ||
      interval_ms > kPostReadyHardMaxSourceIntervalMs) {
    return SourceIntervalClass::kFatal;
  }
  return interval_ms > kStartupMaxSourceIntervalMs
             ? SourceIntervalClass::kAllowedLongJitter
             : SourceIntervalClass::kNominal;
}

double RollingFps(const std::deque<int64_t> &timestamps_ns) {
  if (timestamps_ns.size() != kRollingFpsWindowFrames ||
      timestamps_ns.back() <= timestamps_ns.front()) {
    return 0.0;
  }
  return static_cast<double>(timestamps_ns.size() - 1) * 1.0e9 /
         static_cast<double>(timestamps_ns.back() - timestamps_ns.front());
}

bool RollingFpsIsHealthy(double fps) {
  return std::isfinite(fps) && fps >= kRollingFpsMinimum &&
         fps <= kRollingFpsMaximum;
}

bool RollingFpsViolationIsFatal(bool recording_active, double fps) {
  return recording_active && fps > 0.0 && !RollingFpsIsHealthy(fps);
}

FrameWaitTimeout EvaluateFrameWaitTimeout(bool source_ready, int64_t now_ns,
                                          int64_t last_arrival_ns,
                                          int64_t warmup_deadline_ns) {
  if (!source_ready) {
    return now_ns > warmup_deadline_ns ? FrameWaitTimeout::kStartupWarmup
                                      : FrameWaitTimeout::kNone;
  }
  const int64_t ready_stall_timeout_ns =
      static_cast<int64_t>(kFrameStallTimeoutSec * 1.0e9);
  return now_ns - last_arrival_ns > ready_stall_timeout_ns
             ? FrameWaitTimeout::kReadyStall
             : FrameWaitTimeout::kNone;
}

void RunOfflineStartupTimingSelfTest() {
  constexpr int64_t start_ns = 1000000000LL;
  const int64_t warmup_deadline_ns =
      start_ns + static_cast<int64_t>(kWarmupTimeoutSec * 1.0e9);
  const int64_t stall_ns =
      static_cast<int64_t>(kFrameStallTimeoutSec * 1.0e9);
  if (EvaluateFrameWaitTimeout(false, start_ns + stall_ns + 1, start_ns,
                               warmup_deadline_ns) !=
          FrameWaitTimeout::kNone ||
      EvaluateFrameWaitTimeout(false, warmup_deadline_ns + 1, start_ns,
                               warmup_deadline_ns) !=
          FrameWaitTimeout::kStartupWarmup ||
      EvaluateFrameWaitTimeout(true, start_ns + stall_ns, start_ns,
                               warmup_deadline_ns) !=
          FrameWaitTimeout::kNone ||
      EvaluateFrameWaitTimeout(true, start_ns + stall_ns + 1, start_ns,
                               warmup_deadline_ns) !=
          FrameWaitTimeout::kReadyStall ||
      ClassifyPostReadySourceInterval(102.27) !=
          SourceIntervalClass::kAllowedLongJitter ||
      ClassifyPostReadySourceInterval(kPostReadyHardMaxSourceIntervalMs) !=
          SourceIntervalClass::kAllowedLongJitter ||
      ClassifyPostReadySourceInterval(kPostReadyHardMaxSourceIntervalMs +
                                      0.001) !=
          SourceIntervalClass::kFatal ||
      ClassifyPostReadySourceInterval(33.333) !=
          SourceIntervalClass::kNominal ||
      !RollingFpsIsHealthy(29.97) || !RollingFpsIsHealthy(27.0) ||
      !RollingFpsIsHealthy(33.0) || RollingFpsIsHealthy(26.999) ||
      RollingFpsIsHealthy(33.001) ||
      RollingFpsViolationIsFatal(false, 26.999) ||
      RollingFpsViolationIsFatal(false, 33.001) ||
      RollingFpsViolationIsFatal(true, 0.0) ||
      RollingFpsViolationIsFatal(true, 27.0) ||
      RollingFpsViolationIsFatal(true, 29.97) ||
      RollingFpsViolationIsFatal(true, 33.0) ||
      !RollingFpsViolationIsFatal(true, 26.999) ||
      !RollingFpsViolationIsFatal(true, 33.001)) {
    Fail("offline UVC startup/stall timing self-test failed");
  }
  std::cout << "OFFLINE_STARTUP_TIMING_SELF_TEST_OK warmup_timeout_sec="
            << kWarmupTimeoutSec
            << " ready_stall_timeout_sec=" << kFrameStallTimeoutSec
            << " startup_interval_max_ms=" << kStartupMaxSourceIntervalMs
            << " post_ready_hard_interval_max_ms="
            << kPostReadyHardMaxSourceIntervalMs
            << " rolling_window_frames=" << kRollingFpsWindowFrames
            << " rolling_fps_range=" << kRollingFpsMinimum << '-'
            << kRollingFpsMaximum << '\n';
}

void RunOfflineProfileSelfTest() {
  const auto check = [](const CaptureProfile &profile,
                        const char *expected_view_mode,
                        uint32_t expected_width, uint32_t expected_height,
                        int expected_preview_width,
                        int expected_preview_height,
                        const char *expected_semantic,
                        const char *expected_projection,
                        const char *expected_layout) {
    if (std::string(profile.view_mode) != expected_view_mode ||
        profile.width != expected_width || profile.height != expected_height ||
        profile.preview_width != expected_preview_width ||
        profile.preview_height != expected_preview_height ||
        std::string(profile.semantic) != expected_semantic ||
        std::string(profile.projection) != expected_projection ||
        std::string(profile.layout) != expected_layout) {
      Fail("offline UVC native-profile contract self-test failed for " +
           std::string(expected_view_mode));
    }
    const std::string json = CaptureProfileJson(profile);
    for (const std::string required :
         {"\"view_mode\"", "\"width\"", "\"height\"",
          "\"projection\"", "\"layout\"", "\"semantic\"",
          "\"preview_width\"", "\"preview_height\""}) {
      if (json.find(required) == std::string::npos)
        Fail("offline UVC profile JSON is missing " + required);
    }
  };
  check(kPanoramaProfile, "panorama", 2880, 1440, 960, 480,
        "in_camera_2to1_panorama", "equirectangular_2to1", "stitched_2to1");
  check(kFlatProfile, "flat", 1920, 1080, 960, 540,
        "in_camera_native_flat", "native_flat_profile",
        "dual_fisheye_vertical_stack");
  const CameraIdentity x3 =
      CameraIdentityForCard("Insta360 X3: Insta360 X3", kFlatProfile);
  if (x3.model != "Insta360 X3" || x3.flowstate_supported_in_webcam ||
      x3.flowstate_status != "unsupported_in_x3_webcam") {
    Fail("offline X3 Webcam capability contract self-test failed");
  }
  const CameraIdentity x5 =
      CameraIdentityForCard("Insta360 X5: Insta360 X5", kPanoramaProfile);
  if (x5.model != "Insta360 X5" || !x5.flowstate_supported_in_webcam) {
    Fail("offline X5 Webcam capability contract self-test failed");
  }
  std::cout << "OFFLINE_PROFILE_SELF_TEST_OK panorama=2880x1440/960x480 "
               "flat=1920x1080/960x540 x3_webcam_flowstate=unsupported "
               "host_projection=none\n";
}

unsigned LibraryVersionMajor(unsigned version) { return version >> 16U; }

std::string FfmpegVersionSummary() {
  std::ostringstream output;
  output << "headers(avcodec=" << LIBAVCODEC_VERSION_MAJOR
         << ",avutil=" << LIBAVUTIL_VERSION_MAJOR
         << ",swscale=" << LIBSWSCALE_VERSION_MAJOR << ") runtime(avcodec="
         << LibraryVersionMajor(avcodec_version())
         << ",avutil=" << LibraryVersionMajor(avutil_version())
         << ",swscale=" << LibraryVersionMajor(swscale_version()) << ')';
  return output.str();
}

void VerifyFfmpegAbi() {
  const unsigned runtime_avcodec = LibraryVersionMajor(avcodec_version());
  const unsigned runtime_avutil = LibraryVersionMajor(avutil_version());
  const unsigned runtime_swscale = LibraryVersionMajor(swscale_version());
  if (runtime_avcodec != LIBAVCODEC_VERSION_MAJOR ||
      runtime_avutil != LIBAVUTIL_VERSION_MAJOR ||
      runtime_swscale != LIBSWSCALE_VERSION_MAJOR) {
    Fail("FFmpeg header/runtime ABI mismatch: " + FfmpegVersionSummary() +
         "; rebuild with matching pkg-config headers and shared libraries");
  }
}

struct Options {
  fs::path device;
  std::string role;
  fs::path preview_path;
  CaptureProfile profile = kPanoramaProfile;
};

void PrintUsage(const char *argv0) {
  std::cout
      << "Usage: " << argv0
      << " --device PATH --role ROLE --preview-path PATH "
         "[--view-mode panorama|flat]\n\n"
      << "Strict Insta360 X3/X5 USB Webcam worker. Native source profiles:\n"
      << "panorama (X5 only, default): MJPG 2880x1440 at 30 fps, "
         "in-camera stitched 2:1\n"
      << "flat (X3/X5): MJPG 1920x1080 at 30 fps, native dual-fisheye "
         "vertical stack\n"
      << "Neither profile is reprojected by the worker. The worker never "
         "discovers devices.\n\n"
      << "offline diagnostics:\n"
      << "  --ffmpeg-abi-check\n"
      << "  --offline-startup-timing-self-test\n"
      << "  --offline-profile-self-test\n"
      << "  --offline-preview-self-test JPEG [--view-mode panorama|flat]\n\n"
      << "stdin commands:\n"
      << "  START\\t<JSON encoded session directory>\n"
      << "  STOP\n"
      << "  ABORT\n"
      << "  STATUS\n"
      << "  QUIT\n\n"
      << "stdout runtime protocol: DATACAP_UVC_JSON followed by one JSON "
         "object.\n"
      << "All diagnostic logs are written to stderr.\n";
}

Options ParseOptions(int argc, char **argv) {
  Options options;
  for (int index = 1; index < argc; ++index) {
    const std::string argument = argv[index];
    auto require_value = [&]() -> const char * {
      if (++index >= argc)
        Fail("missing value for " + argument);
      return argv[index];
    };
    if (argument == "--device") {
      options.device = require_value();
    } else if (argument == "--role") {
      options.role = require_value();
    } else if (argument == "--preview-path") {
      options.preview_path = require_value();
    } else if (argument == "--view-mode") {
      options.profile = CaptureProfileForViewMode(require_value());
    } else if (argument == "--help" || argument == "-h") {
      PrintUsage(argv[0]);
      std::exit(0);
    } else {
      Fail("unknown argument: " + argument);
    }
  }
  if (options.device.empty())
    Fail("--device is required");
  if (!options.device.is_absolute())
    Fail("--device must be an explicit absolute path");
  if (options.role.empty())
    Fail("--role is required");
  if (!std::all_of(options.role.begin(), options.role.end(),
                   [](unsigned char value) {
                     return std::isalnum(value) || value == '_' || value == '-';
                   })) {
    Fail("--role may contain only letters, digits, underscore, and hyphen");
  }
  if (options.preview_path.empty())
    Fail("--preview-path is required");
  options.preview_path = fs::absolute(options.preview_path).lexically_normal();
  return options;
}

enum class RecordingLifecycle {
  kIdle,
  kStarting,
  kRecording,
  kFinalizing,
  kComplete,
  kFailed,
  kAborted,
};

const char *RecordingLifecycleText(RecordingLifecycle lifecycle) {
  switch (lifecycle) {
  case RecordingLifecycle::kIdle:
    return "idle";
  case RecordingLifecycle::kStarting:
    return "starting";
  case RecordingLifecycle::kRecording:
    return "recording";
  case RecordingLifecycle::kFinalizing:
    return "finalizing";
  case RecordingLifecycle::kComplete:
    return "complete";
  case RecordingLifecycle::kFailed:
    return "failed";
  case RecordingLifecycle::kAborted:
    return "aborted";
  }
  return "unknown";
}

struct RecordingSnapshot {
  bool recording = false;
  std::string lifecycle = "idle";
  fs::path session_dir;
  fs::path output_dir;
  fs::path video_path;
  fs::path frames_path;
  fs::path manifest_path;
  uint64_t frames = 0;
  uint64_t bytes = 0;
  uint32_t first_sequence = 0;
  uint32_t last_sequence = 0;
  int64_t first_soe_ns = 0;
  int64_t last_soe_ns = 0;
  int64_t first_soe_raw_ns = 0;
  int64_t last_soe_raw_ns = 0;
  int64_t min_monotonic_to_raw_offset_ns = 0;
  int64_t max_monotonic_to_raw_offset_ns = 0;
  int64_t max_monotonic_to_raw_error_bound_ns = 0;
  int64_t start_unix_ns = 0;
  int64_t start_monotonic_ns = 0;
  uint64_t source_interval_samples = 0;
  double max_source_interval_ms = 0.0;
  uint64_t allowed_long_jitter_intervals = 0;
  uint64_t rolling_fps_samples = 0;
  double min_rolling_source_fps = 0.0;
  double max_rolling_source_fps = 0.0;
  double latest_rolling_source_fps = 0.0;
  uint64_t writer_enqueued = 0;
  uint64_t writer_written = 0;
  size_t writer_queue_capacity = 0;
  size_t writer_queue_depth = 0;
  size_t writer_queue_high_water = 0;
  uint32_t v4l2_buffer_count = 0;
  std::string writer_error;
  std::string failure;
};

struct FinalizeResult {
  bool ok = false;
  std::string message;
  RecordingSnapshot snapshot;
};

struct WriterSlot {
  std::vector<uint8_t> payload;
  size_t payload_size = 0;
  uint64_t frame_index = 0;
  uint64_t file_offset = 0;
  uint32_t sequence = 0;
  int64_t soe_ns = 0;
  int64_t soe_raw_ns = 0;
  int64_t dequeue_ns = 0;
  int64_t monotonic_to_raw_offset_ns = 0;
  int64_t monotonic_to_raw_error_bound_ns = 0;
  int64_t enqueue_ns = 0;
  double source_interval_ms = 0.0;
  double rolling_source_fps = 0.0;
};

class Recorder {
public:
  Recorder(std::string role, fs::path device, CaptureProfile profile)
      : role_(std::move(role)), device_(std::move(device)), profile_(profile),
        negotiated_width_(profile.width), negotiated_height_(profile.height) {}

  ~Recorder() { Abort("worker destroyed before clean STOP"); }

  void SetCameraIdentity(const std::string &card_name,
                         const CameraIdentity &identity) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (lifecycle_ == RecordingLifecycle::kStarting ||
        lifecycle_ == RecordingLifecycle::kRecording ||
        lifecycle_ == RecordingLifecycle::kFinalizing) {
      Fail("cannot change UVC camera identity while recording");
    }
    card_name_ = card_name;
    camera_identity_ = identity;
  }

  void SetNegotiated(uint32_t fourcc, uint32_t width, uint32_t height,
                     double fps, size_t max_payload_bytes,
                     uint32_t v4l2_buffer_count) {
    if (max_payload_bytes == 0 || max_payload_bytes > kMaxMjpegPayloadBytes) {
      Fail("unsafe negotiated MJPEG sizeimage for bounded writer queue: " +
           std::to_string(max_payload_bytes));
    }
    std::vector<WriterSlot> slots(kWriterQueueCapacity);
    for (auto &slot : slots)
      slot.payload.resize(max_payload_bytes);

    std::lock_guard<std::mutex> lock(mutex_);
    if (lifecycle_ == RecordingLifecycle::kStarting ||
        lifecycle_ == RecordingLifecycle::kRecording ||
        lifecycle_ == RecordingLifecycle::kFinalizing) {
      Fail("cannot change negotiated UVC format while recording");
    }
    negotiated_fourcc_ = fourcc;
    negotiated_width_ = width;
    negotiated_height_ = height;
    negotiated_fps_ = fps;
    negotiated_buffer_count_ = v4l2_buffer_count;
    writer_payload_capacity_ = max_payload_bytes;
    writer_slots_ = std::move(slots);
  }

  RecordingSnapshot Start(const fs::path &requested_session_dir) {
    std::lock_guard<std::mutex> operation_lock(control_mutex_);
    struct StartWork {
      fs::path session_dir;
      fs::path output_dir;
      fs::path video_partial;
      fs::path frames_partial;
      fs::path manifest_partial;
      fs::path video_final;
      fs::path frames_final;
      fs::path manifest_final;
      int video_fd = -1;
      int frames_fd = -1;
      bool video_created = false;
      bool frames_created = false;
      int64_t start_unix_ns = 0;
      int64_t start_monotonic_ns = 0;
    } work;

    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (lifecycle_ == RecordingLifecycle::kStarting)
        Fail("recording startup is already in progress");
      if (lifecycle_ == RecordingLifecycle::kRecording)
        Fail("recording is already active");
      if (lifecycle_ == RecordingLifecycle::kFinalizing)
        Fail("recording finalization is still in progress");
      if (lifecycle_ == RecordingLifecycle::kAborted ||
          !writer_error_.empty()) {
        Fail("writer is failed/aborted; restart the UVC worker before START");
      }
      if (writer_thread_.joinable())
        Fail("previous writer thread was not joined before START");
      ResetStateLocked();
      abort_requested_external_.store(false, std::memory_order_release);
      lifecycle_ = RecordingLifecycle::kStarting;
      session_dir_ = requested_session_dir;
    }

    auto cleanup_work = [&]() {
      CloseFd(&work.video_fd);
      CloseFd(&work.frames_fd);
      std::error_code ignored;
      if (work.video_created)
        fs::remove(work.video_partial, ignored);
      ignored.clear();
      if (work.frames_created)
        fs::remove(work.frames_partial, ignored);
    };

    try {
      // Filesystem validation and descriptor creation can block. They run
      // outside mutex_ while Append() observes kStarting and immediately
      // returns, so the capture thread is free to QBUF every source packet.
      if (!requested_session_dir.is_absolute())
        Fail("START session directory must be absolute");
      if (!fs::is_directory(requested_session_dir)) {
        Fail("session directory does not exist: " +
             requested_session_dir.string());
      }
      work.session_dir = fs::canonical(requested_session_dir);
      if (work.session_dir == work.session_dir.root_path())
        Fail("refusing to use the filesystem root as a capture session");
      const fs::path uvc_dir = work.session_dir / "uvc";
      EnsureDirectoryWithoutSymlink(uvc_dir);
      const fs::path requested_output_dir = uvc_dir / role_;
      EnsureDirectoryWithoutSymlink(requested_output_dir);
      work.output_dir = fs::canonical(requested_output_dir);
      if (!IsStrictlyWithin(work.session_dir, work.output_dir) ||
          work.output_dir != requested_output_dir.lexically_normal()) {
        Fail("refusing UVC output directory outside canonical session: " +
             work.output_dir.string());
      }

      work.video_partial = work.output_dir / "video.mjpeg.partial";
      work.frames_partial = work.output_dir / "frames.jsonl.partial";
      work.manifest_partial = work.output_dir / "manifest.json.partial";
      work.video_final = work.output_dir / "video.mjpeg";
      work.frames_final = work.output_dir / "frames.jsonl";
      work.manifest_final = work.output_dir / "manifest.json";
      for (const auto &path :
           {work.video_partial, work.frames_partial, work.manifest_partial,
            work.video_final, work.frames_final, work.manifest_final}) {
        if (fs::exists(path)) {
          Fail("refusing to overwrite existing capture artifact: " +
               path.string());
        }
      }

      work.video_fd = OpenExclusive(work.video_partial);
      work.video_created = true;
      work.frames_fd = OpenExclusive(work.frames_partial);
      work.frames_created = true;
      work.start_unix_ns = UnixTimeNs();
      work.start_monotonic_ns = MonotonicNs();

      std::lock_guard<std::mutex> lock(mutex_);
      if (lifecycle_ != RecordingLifecycle::kStarting ||
          g_stop.load(std::memory_order_relaxed) ||
          abort_requested_external_.load(std::memory_order_acquire)) {
        Fail("recording startup was cancelled before commit");
      }
      session_dir_ = work.session_dir;
      output_dir_ = work.output_dir;
      video_partial_ = work.video_partial;
      frames_partial_ = work.frames_partial;
      manifest_partial_ = work.manifest_partial;
      video_final_ = work.video_final;
      frames_final_ = work.frames_final;
      manifest_final_ = work.manifest_final;
      start_unix_ns_ = work.start_unix_ns;
      start_monotonic_ns_ = work.start_monotonic_ns;
      video_fd_ = work.video_fd;
      frames_fd_ = work.frames_fd;
      work.video_fd = -1;
      work.frames_fd = -1;
      video_artifact_path_ = video_partial_;
      frames_artifact_path_ = frames_partial_;
      manifest_artifact_path_.clear();
      if (writer_slots_.size() != kWriterQueueCapacity ||
          writer_payload_capacity_ == 0) {
        Fail("bounded writer queue was not configured before START");
      }
      writer_running_ = true;
      writer_thread_ = std::thread(&Recorder::WriterLoop, this);
      lifecycle_ = RecordingLifecycle::kRecording;
      work.video_created = false;
      work.frames_created = false;
      return SnapshotLocked();
    } catch (const std::exception &error) {
      cleanup_work();
      bool join_started_writer = false;
      {
        std::lock_guard<std::mutex> lock(mutex_);
        if (lifecycle_ == RecordingLifecycle::kStarting) {
          ResetStateLocked();
          lifecycle_ = RecordingLifecycle::kFailed;
          failure_ = "recording startup failed: " + std::string(error.what());
        } else if (lifecycle_ == RecordingLifecycle::kRecording) {
          lifecycle_ = RecordingLifecycle::kAborted;
          failure_ =
              "recording startup response failed: " + std::string(error.what());
          writer_abort_requested_ = true;
          writer_ready_.notify_all();
          join_started_writer = true;
        }
        if (!work.session_dir.empty())
          session_dir_ = work.session_dir;
        if (!work.output_dir.empty())
          output_dir_ = work.output_dir;
      }
      if (join_started_writer && writer_thread_.joinable())
        writer_thread_.join();
      int owned_video_fd = -1;
      int owned_frames_fd = -1;
      {
        std::lock_guard<std::mutex> lock(mutex_);
        if (join_started_writer) {
          owned_video_fd = video_fd_;
          owned_frames_fd = frames_fd_;
          video_fd_ = -1;
          frames_fd_ = -1;
        }
      }
      CloseFd(&owned_video_fd);
      CloseFd(&owned_frames_fd);
      if (join_started_writer) {
        std::error_code ignored;
        fs::remove(work.video_partial, ignored);
        ignored.clear();
        fs::remove(work.frames_partial, ignored);
      }
      {
        std::lock_guard<std::mutex> lock(mutex_);
        video_artifact_path_ =
            (work.video_created || join_started_writer)
                ? ExistingArtifactPath(work.video_partial, work.video_final)
                : fs::path{};
        frames_artifact_path_ =
            (work.frames_created || join_started_writer)
                ? ExistingArtifactPath(work.frames_partial, work.frames_final)
                : fs::path{};
        manifest_artifact_path_.clear();
      }
      throw;
    }
  }

  void Append(uint32_t sequence, int64_t soe_ns, int64_t soe_raw_ns,
              int64_t dequeue_ns, int64_t monotonic_to_raw_offset_ns,
              int64_t monotonic_to_raw_error_bound_ns,
              double source_interval_ms, double rolling_source_fps,
              const uint8_t *payload,
              size_t payload_size) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (lifecycle_ != RecordingLifecycle::kRecording)
      return;
    // A frame may already have been dequeued when START acquires the
    // recorder lock. SOE is in the same CLOCK_MONOTONIC domain, so never
    // admit a pre-START exposure merely because its write raced START.
    if (soe_ns < start_monotonic_ns_)
      return;
    if (RollingFpsViolationIsFatal(true, rolling_source_fps)) {
      Fail("recording rolling 300-frame SOE rate violated [27, 33] fps: " +
           std::to_string(rolling_source_fps));
    }
    if (!writer_error_.empty())
      Fail("bounded writer failed: " + writer_error_);
    if (payload == nullptr || payload_size == 0 || video_fd_ < 0 ||
        frames_fd_ < 0 || !writer_running_) {
      FailWriterAdmissionLocked(
          "invalid bounded-writer state while enqueuing an MJPEG packet");
    }
    if (payload_size > writer_payload_capacity_) {
      FailWriterAdmissionLocked(
          "MJPEG payload exceeds preallocated writer slot: " +
          std::to_string(payload_size) + " > " +
          std::to_string(writer_payload_capacity_));
    }
    if (writer_occupied_ >= writer_slots_.size()) {
      FailWriterAdmissionLocked("bounded writer queue is full at capacity " +
                                std::to_string(writer_slots_.size()) +
                                "; no recording packet was silently dropped");
    }

    WriterSlot &slot = writer_slots_[writer_tail_];
    std::memcpy(slot.payload.data(), payload, payload_size);
    slot.payload_size = payload_size;
    slot.frame_index = writer_enqueued_;
    slot.file_offset = enqueued_payload_bytes_;
    slot.sequence = sequence;
    slot.soe_ns = soe_ns;
    slot.soe_raw_ns = soe_raw_ns;
    slot.dequeue_ns = dequeue_ns;
    slot.monotonic_to_raw_offset_ns = monotonic_to_raw_offset_ns;
    slot.monotonic_to_raw_error_bound_ns =
        monotonic_to_raw_error_bound_ns;
    slot.enqueue_ns = MonotonicNs();
    slot.source_interval_ms = source_interval_ms;
    slot.rolling_source_fps = rolling_source_fps;
    writer_tail_ = (writer_tail_ + 1) % writer_slots_.size();
    ++writer_queued_;
    ++writer_occupied_;
    ++writer_enqueued_;
    enqueued_payload_bytes_ += payload_size;
    writer_queue_high_water_ =
        std::max(writer_queue_high_water_, writer_occupied_);
    writer_ready_.notify_one();
  }

  FinalizeResult Stop() {
    std::lock_guard<std::mutex> operation_lock(control_mutex_);
    struct FinalizeWork {
      int video_fd = -1;
      int frames_fd = -1;
      fs::path output_dir;
      fs::path video_partial;
      fs::path frames_partial;
      fs::path manifest_partial;
      fs::path video_final;
      fs::path frames_final;
      fs::path manifest_final;
      std::string manifest;
      uint64_t frames = 0;
    } work;
    int64_t stop_unix_ns = 0;
    int64_t stop_monotonic_ns = 0;

    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (lifecycle_ == RecordingLifecycle::kStarting) {
        return {false, "recording startup is still in progress",
                SnapshotLocked()};
      }
      if (lifecycle_ == RecordingLifecycle::kFinalizing) {
        return {false, "recording finalization is already in progress",
                SnapshotLocked()};
      }
      if (lifecycle_ != RecordingLifecycle::kRecording) {
        const bool clean = lifecycle_ == RecordingLifecycle::kIdle ||
                           lifecycle_ == RecordingLifecycle::kComplete;
        return {clean, "recording was already stopped", SnapshotLocked()};
      }

      stop_unix_ns = UnixTimeNs();
      stop_monotonic_ns = MonotonicNs();
      // Close admission immediately, then ask the single writer to drain every
      // packet already accepted by the bounded queue. Append() now returns
      // immediately and the capture loop remains free to QBUF mmap buffers.
      lifecycle_ = RecordingLifecycle::kFinalizing;
      writer_drain_requested_ = true;
      writer_ready_.notify_all();
    }

    if (writer_thread_.joinable())
      writer_thread_.join();

    bool writer_failed = false;
    RecordingSnapshot writer_failure_snapshot;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      writer_failed = !writer_error_.empty() ||
                      lifecycle_ == RecordingLifecycle::kAborted ||
                      writer_queued_ != 0 || writer_occupied_ != 0 ||
                      writer_written_ != writer_enqueued_;
      if (writer_failed) {
        if (failure_.empty())
          failure_ = "bounded writer did not drain all accepted packets";
        lifecycle_ = RecordingLifecycle::kFailed;
      }

      work.video_fd = video_fd_;
      work.frames_fd = frames_fd_;
      work.output_dir = output_dir_;
      work.video_partial = video_partial_;
      work.frames_partial = frames_partial_;
      work.manifest_partial = manifest_partial_;
      work.video_final = video_final_;
      work.frames_final = frames_final_;
      work.manifest_final = manifest_final_;
      work.frames = frames_;
      video_fd_ = -1;
      frames_fd_ = -1;

      if (!writer_failed) {
        work.manifest = BuildManifestLocked(stop_unix_ns, stop_monotonic_ns);
      } else {
        video_artifact_path_ = video_partial_;
        frames_artifact_path_ = frames_partial_;
        manifest_artifact_path_.clear();
        writer_failure_snapshot = SnapshotLocked();
      }
    }

    if (writer_failed) {
      CloseFd(&work.video_fd);
      CloseFd(&work.frames_fd);
      return {false,
              "UVC recording writer failed: " + writer_failure_snapshot.failure,
              writer_failure_snapshot};
    }

    bool video_promoted = false;
    bool frames_promoted = false;
    bool manifest_promoted = false;
    try {
      FailIfFinalizationCancelled();
      if (work.frames == 0)
        Fail("refusing to finalize a UVC recording with zero frames");
      FsyncFd(work.video_fd, work.video_partial.string());
      FsyncFd(work.frames_fd, work.frames_partial.string());
      CloseFd(&work.video_fd);
      CloseFd(&work.frames_fd);
      FailIfFinalizationCancelled();

      int manifest_fd = OpenExclusive(work.manifest_partial);
      try {
        WriteAll(manifest_fd, work.manifest);
        FsyncFd(manifest_fd, work.manifest_partial.string());
        CloseFd(&manifest_fd);
      } catch (...) {
        CloseFd(&manifest_fd);
        throw;
      }
      FailIfFinalizationCancelled();

      RenameNoReplace(work.video_partial, work.video_final,
                      "cannot promote video.mjpeg.partial without overwrite");
      video_promoted = true;
      if (RenameNoReplaceRaw(work.frames_partial, work.frames_final) != 0) {
        const int saved_errno = errno;
        if (RenameNoReplaceRaw(work.video_final, work.video_partial) == 0)
          video_promoted = false;
        Fail("cannot promote frames.jsonl.partial without overwrite: " +
             std::string(std::strerror(saved_errno)));
      }
      frames_promoted = true;
      // Make both data artifacts durable before publishing manifest.json.
      // Consumers treat the final manifest name as the sole completion marker.
      FsyncDirectory(work.output_dir);
      FailIfFinalizationCancelled();
      if (RenameNoReplaceRaw(work.manifest_partial, work.manifest_final) != 0) {
        const int saved_errno = errno;
        if (RenameNoReplaceRaw(work.frames_final, work.frames_partial) == 0)
          frames_promoted = false;
        if (RenameNoReplaceRaw(work.video_final, work.video_partial) == 0)
          video_promoted = false;
        Fail("cannot promote manifest.json.partial without overwrite: " +
             std::string(std::strerror(saved_errno)));
      }
      manifest_promoted = true;
      // Do not acknowledge STOP until the completion marker itself is durable.
      FsyncDirectory(work.output_dir);
      FailIfFinalizationCancelled();

      RecordingSnapshot completed_snapshot;
      bool cancelled_at_commit = false;
      {
        std::lock_guard<std::mutex> lock(mutex_);
        // This is the lifecycle commit point. Check the lock-free abort signal
        // while holding the state mutex, but do not throw until after releasing
        // it (the catch path must acquire mutex_ to roll final names back).
        cancelled_at_commit =
            abort_requested_external_.load(std::memory_order_acquire) ||
            g_signal_number.load(std::memory_order_relaxed) != 0;
        if (!cancelled_at_commit) {
          lifecycle_ = RecordingLifecycle::kComplete;
          failure_.clear();
          video_artifact_path_ = work.video_final;
          frames_artifact_path_ = work.frames_final;
          manifest_artifact_path_ = work.manifest_final;
          completed_snapshot = SnapshotLocked();
        }
      }
      if (cancelled_at_commit)
        Fail("recording finalization cancelled at lifecycle commit");
      return {true, "UVC recording finalized", completed_snapshot};
    } catch (const std::exception &error) {
      CloseFd(&work.video_fd);
      CloseFd(&work.frames_fd);
      // A STOP failure must not leave artifacts carrying final names.
      // Roll back every promotion that occurred before the failure;
      // the .partial names are the crash/failure evidence consumed by
      // the supervisor and are never mistaken for a complete capture.
      if (manifest_promoted) {
        if (RenameNoReplaceRaw(work.manifest_final, work.manifest_partial) !=
            0) {
          // manifest.json is the sole completion marker and was created by our
          // successful RENAME_NOREPLACE above. If rollback cannot restore the
          // partial name, remove that marker rather than expose a failed STOP
          // as complete.
          (void)unlink(work.manifest_final.c_str());
        }
      }
      if (frames_promoted) {
        RenameNoReplaceRaw(work.frames_final, work.frames_partial);
      }
      if (video_promoted) {
        RenameNoReplaceRaw(work.video_final, work.video_partial);
      }
      try {
        FsyncDirectory(work.output_dir);
      } catch (...) {
        // Preserve and report the original finalization failure. Actual paths
        // below are resolved from the filesystem even if rollback durability
        // could not be confirmed.
      }

      std::lock_guard<std::mutex> lock(mutex_);
      lifecycle_ = RecordingLifecycle::kFailed;
      failure_ = error.what();
      video_artifact_path_ =
          ExistingArtifactPath(work.video_partial, work.video_final);
      frames_artifact_path_ =
          ExistingArtifactPath(work.frames_partial, work.frames_final);
      manifest_artifact_path_ =
          ExistingArtifactPath(work.manifest_partial, work.manifest_final);
      return {false, "UVC recording finalization failed: " + failure_,
              SnapshotLocked()};
    }
  }

  RecordingSnapshot Abort(const std::string &reason) {
    // Publish cancellation before waiting for control_mutex_. If STOP is
    // concurrently fsyncing, it observes this flag and rolls every final name
    // back to .partial before releasing the operation lock.
    abort_requested_external_.store(true, std::memory_order_release);
    std::lock_guard<std::mutex> operation_lock(control_mutex_);
    int video_fd = -1;
    int frames_fd = -1;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (lifecycle_ == RecordingLifecycle::kIdle ||
          lifecycle_ == RecordingLifecycle::kComplete) {
        return SnapshotLocked();
      }
      lifecycle_ = RecordingLifecycle::kAborted;
      if (writer_error_.empty())
        failure_ = reason;
      writer_abort_requested_ = true;
      writer_drain_requested_ = false;
      writer_ready_.notify_all();
    }

    // Never hold the recorder state mutex while joining: the writer needs it
    // to finish its current packet, discard queued slots, and exit.
    if (writer_thread_.joinable())
      writer_thread_.join();

    std::lock_guard<std::mutex> lock(mutex_);
    writer_queued_ = 0;
    writer_occupied_ = 0;
    writer_running_ = false;
    video_fd = video_fd_;
    frames_fd = frames_fd_;
    video_fd_ = -1;
    frames_fd_ = -1;
    CloseFd(&video_fd);
    CloseFd(&frames_fd);
    video_artifact_path_ = ExistingArtifactPath(video_partial_, video_final_);
    frames_artifact_path_ =
        ExistingArtifactPath(frames_partial_, frames_final_);
    manifest_artifact_path_ =
        ExistingArtifactPath(manifest_partial_, manifest_final_);
    return SnapshotLocked();
  }

  RecordingSnapshot Snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return SnapshotLocked();
  }

private:
  [[noreturn]] void FailWriterAdmissionLocked(const std::string &detail) {
    writer_error_ = detail;
    failure_ = "bounded writer admission failed: " + detail;
    lifecycle_ = RecordingLifecycle::kAborted;
    Fail(detail);
  }

  void FailIfFinalizationCancelled() const {
    if (abort_requested_external_.load(std::memory_order_acquire) ||
        g_signal_number.load(std::memory_order_relaxed) != 0) {
      Fail("recording finalization cancelled by abort or termination signal");
    }
  }

  void WriterLoop() noexcept {
    std::string fatal_error;
    try {
      while (true) {
        size_t slot_index = 0;
        {
          std::unique_lock<std::mutex> lock(mutex_);
          writer_ready_.wait(lock, [&]() {
            return writer_abort_requested_ || writer_drain_requested_ ||
                   writer_queued_ > 0;
          });
          if (writer_abort_requested_) {
            writer_queued_ = 0;
            writer_occupied_ = 0;
            break;
          }
          if (writer_queued_ == 0 && writer_drain_requested_)
            break;
          if (writer_queued_ == 0)
            continue;
          slot_index = writer_head_;
          writer_head_ = (writer_head_ + 1) % writer_slots_.size();
          --writer_queued_;
        }

        WriterSlot &slot = writer_slots_[slot_index];
        const int64_t record_monotonic_ns = MonotonicNs();
        WriteAll(video_fd_, slot.payload.data(), slot.payload_size);
        std::ostringstream row;
        row << "{\"frame_index\":" << slot.frame_index
            << ",\"sequence\":" << slot.sequence
            << ",\"soe_monotonic_ns\":" << slot.soe_ns
            << ",\"soe_monotonic_raw_ns\":" << slot.soe_raw_ns
            << ",\"monotonic_to_raw_offset_ns\":"
            << slot.monotonic_to_raw_offset_ns
            << ",\"monotonic_to_raw_error_bound_ns\":"
            << slot.monotonic_to_raw_error_bound_ns
            << ",\"dequeue_monotonic_ns\":" << slot.dequeue_ns
            << ",\"enqueue_monotonic_ns\":" << slot.enqueue_ns
            << ",\"record_monotonic_ns\":" << record_monotonic_ns
            << ",\"source_interval_ms\":" << std::fixed << std::setprecision(3)
            << slot.source_interval_ms << ",\"rolling_source_fps_300\":";
        if (slot.rolling_source_fps > 0.0)
          row << slot.rolling_source_fps;
        else
          row << "null";
        row << ",\"payload_bytes\":" << slot.payload_size
            << ",\"file_offset_bytes\":" << slot.file_offset << "}\n";
        WriteAll(frames_fd_, row.str());

        {
          std::lock_guard<std::mutex> lock(mutex_);
          if (frames_ == 0) {
            first_sequence_ = slot.sequence;
            first_soe_ns_ = slot.soe_ns;
            first_soe_raw_ns_ = slot.soe_raw_ns;
            min_monotonic_to_raw_offset_ns_ =
                slot.monotonic_to_raw_offset_ns;
            max_monotonic_to_raw_offset_ns_ =
                slot.monotonic_to_raw_offset_ns;
          }
          last_sequence_ = slot.sequence;
          last_soe_ns_ = slot.soe_ns;
          last_soe_raw_ns_ = slot.soe_raw_ns;
          min_monotonic_to_raw_offset_ns_ =
              std::min(min_monotonic_to_raw_offset_ns_,
                       slot.monotonic_to_raw_offset_ns);
          max_monotonic_to_raw_offset_ns_ =
              std::max(max_monotonic_to_raw_offset_ns_,
                       slot.monotonic_to_raw_offset_ns);
          max_monotonic_to_raw_error_bound_ns_ =
              std::max(max_monotonic_to_raw_error_bound_ns_,
                       slot.monotonic_to_raw_error_bound_ns);
          ++frames_;
          bytes_ += slot.payload_size;
          ++source_interval_samples_;
          max_source_interval_ms_ =
              std::max(max_source_interval_ms_, slot.source_interval_ms);
          if (slot.source_interval_ms > kStartupMaxSourceIntervalMs)
            ++allowed_long_jitter_intervals_;
          if (slot.rolling_source_fps > 0.0) {
            if (rolling_fps_samples_ == 0) {
              min_rolling_source_fps_ = slot.rolling_source_fps;
              max_rolling_source_fps_ = slot.rolling_source_fps;
            } else {
              min_rolling_source_fps_ =
                  std::min(min_rolling_source_fps_, slot.rolling_source_fps);
              max_rolling_source_fps_ =
                  std::max(max_rolling_source_fps_, slot.rolling_source_fps);
            }
            latest_rolling_source_fps_ = slot.rolling_source_fps;
            ++rolling_fps_samples_;
          }
          ++writer_written_;
          if (writer_occupied_ == 0)
            Fail("bounded writer occupied-count underflow");
          --writer_occupied_;
          writer_done_.notify_all();
        }
      }
    } catch (const std::exception &error) {
      fatal_error = error.what();
    } catch (...) {
      fatal_error = "unknown bounded writer failure";
    }

    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (!fatal_error.empty()) {
        writer_error_ = fatal_error;
        failure_ = "bounded writer failed: " + fatal_error;
        writer_abort_requested_ = true;
        writer_queued_ = 0;
        writer_occupied_ = 0;
        if (lifecycle_ == RecordingLifecycle::kRecording ||
            lifecycle_ == RecordingLifecycle::kFinalizing) {
          lifecycle_ = RecordingLifecycle::kAborted;
        }
        video_artifact_path_ = video_partial_;
        frames_artifact_path_ = frames_partial_;
        manifest_artifact_path_.clear();
      }
      writer_running_ = false;
      writer_done_.notify_all();
    }

    if (!fatal_error.empty()) {
      Emit("error", false, "bounded recording writer failed",
           "\"fatal\":true,\"detail\":" + JsonEscape(fatal_error));
      g_stop.store(true, std::memory_order_relaxed);
    }
  }

  static fs::path ExistingArtifactPath(const fs::path &partial,
                                       const fs::path &final) {
    std::error_code error;
    if (!partial.empty() && fs::exists(partial, error))
      return partial;
    error.clear();
    if (!final.empty() && fs::exists(final, error))
      return final;
    return {};
  }

  void ResetStateLocked() {
    CloseFd(&video_fd_);
    CloseFd(&frames_fd_);
    lifecycle_ = RecordingLifecycle::kIdle;
    session_dir_.clear();
    output_dir_.clear();
    video_partial_.clear();
    frames_partial_.clear();
    manifest_partial_.clear();
    video_final_.clear();
    frames_final_.clear();
    manifest_final_.clear();
    video_artifact_path_.clear();
    frames_artifact_path_.clear();
    manifest_artifact_path_.clear();
    frames_ = 0;
    bytes_ = 0;
    first_sequence_ = 0;
    last_sequence_ = 0;
    first_soe_ns_ = 0;
    last_soe_ns_ = 0;
    first_soe_raw_ns_ = 0;
    last_soe_raw_ns_ = 0;
    min_monotonic_to_raw_offset_ns_ = 0;
    max_monotonic_to_raw_offset_ns_ = 0;
    max_monotonic_to_raw_error_bound_ns_ = 0;
    start_unix_ns_ = 0;
    start_monotonic_ns_ = 0;
    source_interval_samples_ = 0;
    max_source_interval_ms_ = 0.0;
    allowed_long_jitter_intervals_ = 0;
    rolling_fps_samples_ = 0;
    min_rolling_source_fps_ = 0.0;
    max_rolling_source_fps_ = 0.0;
    latest_rolling_source_fps_ = 0.0;
    writer_head_ = 0;
    writer_tail_ = 0;
    writer_queued_ = 0;
    writer_occupied_ = 0;
    writer_queue_high_water_ = 0;
    writer_enqueued_ = 0;
    writer_written_ = 0;
    enqueued_payload_bytes_ = 0;
    writer_drain_requested_ = false;
    writer_abort_requested_ = false;
    writer_running_ = false;
    writer_error_.clear();
    failure_.clear();
  }

  RecordingSnapshot SnapshotLocked() const {
    RecordingSnapshot result;
    result.recording = lifecycle_ == RecordingLifecycle::kRecording;
    result.lifecycle = RecordingLifecycleText(lifecycle_);
    result.session_dir = session_dir_;
    result.output_dir = output_dir_;
    result.video_path = video_artifact_path_;
    result.frames_path = frames_artifact_path_;
    result.manifest_path = manifest_artifact_path_;
    result.frames = frames_;
    result.bytes = bytes_;
    result.first_sequence = first_sequence_;
    result.last_sequence = last_sequence_;
    result.first_soe_ns = first_soe_ns_;
    result.last_soe_ns = last_soe_ns_;
    result.first_soe_raw_ns = first_soe_raw_ns_;
    result.last_soe_raw_ns = last_soe_raw_ns_;
    result.min_monotonic_to_raw_offset_ns =
        min_monotonic_to_raw_offset_ns_;
    result.max_monotonic_to_raw_offset_ns =
        max_monotonic_to_raw_offset_ns_;
    result.max_monotonic_to_raw_error_bound_ns =
        max_monotonic_to_raw_error_bound_ns_;
    result.start_unix_ns = start_unix_ns_;
    result.start_monotonic_ns = start_monotonic_ns_;
    result.source_interval_samples = source_interval_samples_;
    result.max_source_interval_ms = max_source_interval_ms_;
    result.allowed_long_jitter_intervals = allowed_long_jitter_intervals_;
    result.rolling_fps_samples = rolling_fps_samples_;
    result.min_rolling_source_fps = min_rolling_source_fps_;
    result.max_rolling_source_fps = max_rolling_source_fps_;
    result.latest_rolling_source_fps = latest_rolling_source_fps_;
    result.writer_enqueued = writer_enqueued_;
    result.writer_written = writer_written_;
    result.writer_queue_capacity = writer_slots_.size();
    result.writer_queue_depth = writer_occupied_;
    result.writer_queue_high_water = writer_queue_high_water_;
    result.v4l2_buffer_count = negotiated_buffer_count_;
    result.writer_error = writer_error_;
    result.failure = failure_;
    return result;
  }

  std::string BuildManifestLocked(int64_t stop_unix_ns,
                                  int64_t stop_monotonic_ns) const {
    std::ostringstream manifest;
    manifest
        << "{\n"
        << "  \"schema\": " << JsonEscape(kCaptureSchema) << ",\n"
        << "  \"role\": " << JsonEscape(role_) << ",\n"
        << "  \"view_mode\": " << JsonEscape(profile_.view_mode) << ",\n"
        << "  \"width\": " << negotiated_width_ << ",\n"
        << "  \"height\": " << negotiated_height_ << ",\n"
        << "  \"projection\": " << JsonEscape(profile_.projection) << ",\n"
        << "  \"layout\": " << JsonEscape(profile_.layout) << ",\n"
        << "  \"source\": {\n"
        << "    \"device\": " << JsonEscape(device_.string()) << ",\n"
        << "    \"card\": " << JsonEscape(card_name_) << ",\n"
        << "    \"camera_model\": "
        << JsonEscape(camera_identity_.model) << ",\n"
        << "    \"flowstate_supported_in_webcam\": "
        << (camera_identity_.flowstate_supported_in_webcam ? "true" : "false")
        << ",\n"
        << "    \"flowstate_verified\": false,\n"
        << "    \"flowstate_status\": "
        << JsonEscape(camera_identity_.flowstate_status) << ",\n"
        << "    \"view_mode\": " << JsonEscape(profile_.view_mode) << ",\n"
        << "    \"negotiated_fourcc\": "
        << JsonEscape(FourccText(negotiated_fourcc_)) << ",\n"
        << "    \"negotiated_width\": " << negotiated_width_ << ",\n"
        << "    \"negotiated_height\": " << negotiated_height_ << ",\n"
        << "    \"width\": " << negotiated_width_ << ",\n"
        << "    \"height\": " << negotiated_height_ << ",\n"
        << "    \"negotiated_fps\": " << std::fixed << std::setprecision(6)
        << negotiated_fps_ << ",\n"
        << "    \"v4l2_mmap_buffer_count\": " << negotiated_buffer_count_
        << ",\n"
        << "    \"semantic\": " << JsonEscape(profile_.semantic) << ",\n"
        << "    \"projection\": " << JsonEscape(profile_.projection)
        << ",\n"
        << "    \"layout\": " << JsonEscape(profile_.layout) << ",\n"
        << "    \"camera_processing\": "
        << JsonEscape(profile_.camera_processing) << ",\n"
        << "    \"host_geometric_postprocessing\": false,\n"
        << "    \"standalone_jfif_payload\": false\n"
        << "  },\n"
        << "  \"timestamp_semantics\": {\n"
        << "    \"source\": \"V4L2 monotonic start-of-exposure flag\",\n"
        << "    \"soe_monotonic_ns_clock\": \"CLOCK_MONOTONIC\",\n"
        << "    \"soe_monotonic_raw_ns_clock\": "
           "\"CLOCK_MONOTONIC_RAW estimated per DQBUF\",\n"
        << "    \"monotonic_to_raw_mapping\": {\n"
        << "      \"sampling\": \"CLOCK_MONOTONIC before, "
           "CLOCK_MONOTONIC_RAW, CLOCK_MONOTONIC after\",\n"
        << "      \"offset_definition\": \"raw sample minus midpoint of "
           "the bracketing monotonic samples\",\n"
        << "      \"error_bound_definition\": \"half the monotonic "
           "bracket width rounded up\",\n"
        << "      \"per_frame_fields\": [\"soe_monotonic_raw_ns\", "
           "\"monotonic_to_raw_offset_ns\", "
           "\"monotonic_to_raw_error_bound_ns\"]\n"
        << "    },\n"
        << "    \"dequeue_monotonic_ns\": \"first CLOCK_MONOTONIC sample "
           "immediately after successful VIDIOC_DQBUF\",\n"
        << "    \"enqueue_monotonic_ns\": \"capture-thread admission into "
           "the bounded writer queue\",\n"
        << "    \"record_monotonic_ns\": \"writer-thread slot pickup "
           "before write calls; not disk write completion or durability\",\n"
        << "    \"hidden_sensor_to_driver_timestamp_ms\": \"unknown\"\n"
        << "  },\n"
        << "  \"files\": {\n"
        << "    \"video\": \"video.mjpeg\",\n"
        << "    \"frames\": \"frames.jsonl\",\n"
        << "    \"sole_completion_marker\": \"manifest.json\"\n"
        << "  },\n"
        << "  \"recording\": {\n"
        << "    \"start_unix_time_ns\": " << start_unix_ns_ << ",\n"
        << "    \"start_monotonic_time_ns\": " << start_monotonic_ns_ << ",\n"
        << "    \"stop_unix_time_ns\": " << stop_unix_ns << ",\n"
        << "    \"stop_monotonic_time_ns\": " << stop_monotonic_ns << ",\n"
        << "    \"frame_count\": " << frames_ << ",\n"
        << "    \"payload_bytes\": " << bytes_ << ",\n"
        << "    \"first_sequence\": " << first_sequence_ << ",\n"
        << "    \"last_sequence\": " << last_sequence_ << ",\n"
        << "    \"first_soe_monotonic_ns\": " << first_soe_ns_ << ",\n"
        << "    \"last_soe_monotonic_ns\": " << last_soe_ns_ << ",\n"
        << "    \"first_soe_monotonic_raw_ns\": " << first_soe_raw_ns_
        << ",\n"
        << "    \"last_soe_monotonic_raw_ns\": " << last_soe_raw_ns_
        << ",\n"
        << "    \"min_monotonic_to_raw_offset_ns\": "
        << min_monotonic_to_raw_offset_ns_ << ",\n"
        << "    \"max_monotonic_to_raw_offset_ns\": "
        << max_monotonic_to_raw_offset_ns_ << ",\n"
        << "    \"max_monotonic_to_raw_error_bound_ns\": "
        << max_monotonic_to_raw_error_bound_ns_ << ",\n"
        << "    \"source_interval_samples\": " << source_interval_samples_
        << ",\n"
        << "    \"max_source_interval_ms\": " << std::fixed
        << std::setprecision(3) << max_source_interval_ms_ << ",\n"
        << "    \"startup_stable_max_source_interval_ms\": "
        << kStartupMaxSourceIntervalMs << ",\n"
        << "    \"post_ready_hard_max_source_interval_ms\": "
        << kPostReadyHardMaxSourceIntervalMs << ",\n"
        << "    \"max_allowed_source_interval_ms\": "
        << kPostReadyHardMaxSourceIntervalMs << ",\n"
        << "    \"allowed_long_jitter_intervals\": "
        << allowed_long_jitter_intervals_ << ",\n"
        << "    \"rolling_fps_window_frames\": " << kRollingFpsWindowFrames
        << ",\n"
        << "    \"rolling_fps_minimum\": " << kRollingFpsMinimum << ",\n"
        << "    \"rolling_fps_maximum\": " << kRollingFpsMaximum << ",\n"
        << "    \"rolling_fps_300_samples\": " << rolling_fps_samples_
        << ",\n"
        << "    \"min_rolling_source_fps_300\": " << min_rolling_source_fps_
        << ",\n"
        << "    \"max_rolling_source_fps_300\": " << max_rolling_source_fps_
        << ",\n"
        << "    \"latest_rolling_source_fps_300\": "
        << latest_rolling_source_fps_ << ",\n"
        << "    \"interval_policy\": \"startup requires eight consecutive "
           "intervals at or below the startup threshold; post-ready long "
           "SOE jitter is counted and accepted only up to the hard threshold\",\n"
        << "    \"sequence_gaps\": 0,\n"
        << "    \"packet_storage\": "
           "\"verbatim_concatenated_v4l2_mjpeg_payloads\"\n"
        << "  },\n"
        << "  \"bounded_writer\": {\n"
        << "    \"capacity_frames\": " << writer_slots_.size() << ",\n"
        << "    \"slot_payload_capacity_bytes\": " << writer_payload_capacity_
        << ",\n"
        << "    \"queue_high_water_frames\": " << writer_queue_high_water_
        << ",\n"
        << "    \"enqueued_frames\": " << writer_enqueued_ << ",\n"
        << "    \"written_frames\": " << writer_written_ << ",\n"
        << "    \"queue_full_is_fatal\": true,\n"
        << "    \"write_error\": null\n"
        << "  },\n"
        << "  \"preview\": {\n"
        << "    \"participates_in_recording\": false,\n"
        << "    \"queue_semantics\": \"independent_latest_only\",\n"
        << "    \"target_fps\": " << kPreviewFps << ",\n"
        << "    \"width\": " << profile_.preview_width << ",\n"
        << "    \"height\": " << profile_.preview_height << ",\n"
        << "    \"view_mode\": " << JsonEscape(profile_.view_mode) << ",\n"
        << "    \"projection\": " << JsonEscape(profile_.projection) << ",\n"
        << "    \"layout\": " << JsonEscape(profile_.layout) << ",\n"
        << "    \"recording_note\": \"preview decode, resize, JPEG encode, "
           "overwrite, or failure cannot alter recorded MJPEG packets\"\n"
        << "  }\n"
        << "}\n";
    return manifest.str();
  }

  const std::string role_;
  const fs::path device_;
  const CaptureProfile profile_;
  std::string card_name_;
  CameraIdentity camera_identity_{"unknown", false, "not_verified"};
  mutable std::mutex control_mutex_;
  mutable std::mutex mutex_;
  std::atomic<bool> abort_requested_external_{false};
  std::condition_variable writer_ready_;
  std::condition_variable writer_done_;
  RecordingLifecycle lifecycle_ = RecordingLifecycle::kIdle;
  int video_fd_ = -1;
  int frames_fd_ = -1;
  fs::path session_dir_;
  fs::path output_dir_;
  fs::path video_partial_;
  fs::path frames_partial_;
  fs::path manifest_partial_;
  fs::path video_final_;
  fs::path frames_final_;
  fs::path manifest_final_;
  fs::path video_artifact_path_;
  fs::path frames_artifact_path_;
  fs::path manifest_artifact_path_;
  uint64_t frames_ = 0;
  uint64_t bytes_ = 0;
  uint32_t first_sequence_ = 0;
  uint32_t last_sequence_ = 0;
  int64_t first_soe_ns_ = 0;
  int64_t last_soe_ns_ = 0;
  int64_t first_soe_raw_ns_ = 0;
  int64_t last_soe_raw_ns_ = 0;
  int64_t min_monotonic_to_raw_offset_ns_ = 0;
  int64_t max_monotonic_to_raw_offset_ns_ = 0;
  int64_t max_monotonic_to_raw_error_bound_ns_ = 0;
  int64_t start_unix_ns_ = 0;
  int64_t start_monotonic_ns_ = 0;
  uint64_t source_interval_samples_ = 0;
  double max_source_interval_ms_ = 0.0;
  uint64_t allowed_long_jitter_intervals_ = 0;
  uint64_t rolling_fps_samples_ = 0;
  double min_rolling_source_fps_ = 0.0;
  double max_rolling_source_fps_ = 0.0;
  double latest_rolling_source_fps_ = 0.0;
  std::vector<WriterSlot> writer_slots_;
  size_t writer_payload_capacity_ = 0;
  size_t writer_head_ = 0;
  size_t writer_tail_ = 0;
  size_t writer_queued_ = 0;
  size_t writer_occupied_ = 0;
  size_t writer_queue_high_water_ = 0;
  uint64_t writer_enqueued_ = 0;
  uint64_t writer_written_ = 0;
  uint64_t enqueued_payload_bytes_ = 0;
  bool writer_drain_requested_ = false;
  bool writer_abort_requested_ = false;
  bool writer_running_ = false;
  std::string writer_error_;
  std::thread writer_thread_;
  std::string failure_;
  uint32_t negotiated_fourcc_ = V4L2_PIX_FMT_MJPEG;
  uint32_t negotiated_width_;
  uint32_t negotiated_height_;
  double negotiated_fps_ = static_cast<double>(kFps);
  uint32_t negotiated_buffer_count_ = 0;
};

struct PreviewPacket {
  uint32_t sequence = 0;
  int64_t soe_ns = 0;
  size_t payload_size = 0;
  std::vector<uint8_t> bytes;
};

struct PreviewSnapshot {
  uint64_t submitted = 0;
  uint64_t published = 0;
  uint64_t overwritten = 0;
  uint64_t gate_skipped = 0;
  uint64_t errors = 0;
  uint32_t latest_sequence = 0;
  int64_t latest_soe_ns = 0;
  std::string last_error;
};

class JpegPreviewCodec {
public:
  explicit JpegPreviewCodec(CaptureProfile profile) : profile_(profile) {
    VerifyFfmpegAbi();
    av_log_set_level(AV_LOG_ERROR);
    const AVCodec *decoder = avcodec_find_decoder(AV_CODEC_ID_MJPEG);
    const AVCodec *encoder = avcodec_find_encoder(AV_CODEC_ID_MJPEG);
    if (decoder == nullptr || encoder == nullptr)
      Fail("FFmpeg MJPEG codecs are unavailable");
    decoder_ = avcodec_alloc_context3(decoder);
    encoder_ = avcodec_alloc_context3(encoder);
    decoded_ = av_frame_alloc();
    scaled_ = av_frame_alloc();
    input_packet_ = av_packet_alloc();
    output_packet_ = av_packet_alloc();
    if (!decoder_ || !encoder_ || !decoded_ || !scaled_ || !input_packet_ ||
        !output_packet_) {
      Fail("FFmpeg preview allocation failed");
    }
    decoder_->thread_count = 1;
    decoder_->flags |= AV_CODEC_FLAG_LOW_DELAY;
    decoder_->flags2 |= AV_CODEC_FLAG2_FAST;
    // MJPEG carries its input pixel format in every JPEG frame.  Leave the
    // decoder format unset so libavcodec can select it from the bitstream.
    decoder_->pix_fmt = AV_PIX_FMT_NONE;
    int result = avcodec_open2(decoder_, decoder, nullptr);
    if (result < 0)
      Fail("cannot open FFmpeg MJPEG decoder: " + AvError(result));

    encoder_->width = profile_.preview_width;
    encoder_->height = profile_.preview_height;
    encoder_->thread_count = 1;
    encoder_->time_base = AVRational{1, kPreviewFps};
    encoder_->framerate = AVRational{kPreviewFps, 1};
    encoder_->pix_fmt = AV_PIX_FMT_YUVJ420P;
    if (encoder_->pix_fmt == AV_PIX_FMT_NONE)
      Fail("FFmpeg preview encoder pixel format was not initialized");
    encoder_->color_range = AVCOL_RANGE_JPEG;
    encoder_->flags |= AV_CODEC_FLAG_QSCALE;
    encoder_->global_quality = FF_QP2LAMBDA * 5;
    result = avcodec_open2(encoder_, encoder, nullptr);
    if (result < 0)
      Fail("cannot open FFmpeg MJPEG encoder: " + AvError(result));

    scaled_->format = encoder_->pix_fmt;
    scaled_->width = encoder_->width;
    scaled_->height = encoder_->height;
    scaled_->color_range = AVCOL_RANGE_JPEG;
    result = av_frame_get_buffer(scaled_, 32);
    if (result < 0)
      Fail("cannot allocate preview frame: " + AvError(result));
  }

  ~JpegPreviewCodec() {
    sws_freeContext(scaler_);
    av_packet_free(&output_packet_);
    av_packet_free(&input_packet_);
    av_frame_free(&scaled_);
    av_frame_free(&decoded_);
    avcodec_free_context(&encoder_);
    avcodec_free_context(&decoder_);
  }

  std::vector<uint8_t> Convert(const PreviewPacket &input) {
    av_packet_unref(input_packet_);
    if (input.payload_size == 0 ||
        input.bytes.size() <
            input.payload_size + AV_INPUT_BUFFER_PADDING_SIZE) {
      Fail("preview MJPEG packet lacks FFmpeg input padding");
    }
    input_packet_->data = const_cast<uint8_t *>(input.bytes.data());
    input_packet_->size = static_cast<int>(input.payload_size);
    int result = avcodec_send_packet(decoder_, input_packet_);
    if (result < 0)
      Fail("preview decoder rejected MJPEG packet: " + AvError(result));
    av_frame_unref(decoded_);
    result = avcodec_receive_frame(decoder_, decoded_);
    if (result < 0)
      Fail("preview decoder produced no frame: " + AvError(result));
    if (decoded_->width != static_cast<int>(profile_.width) ||
        decoded_->height != static_cast<int>(profile_.height)) {
      Fail("decoded UVC geometry changed to " +
           std::to_string(decoded_->width) + "x" +
           std::to_string(decoded_->height));
    }
    if (decoded_->format == AV_PIX_FMT_NONE)
      Fail("preview MJPEG decoder returned no pixel format");

    scaler_ =
        sws_getCachedContext(scaler_, decoded_->width, decoded_->height,
                             static_cast<AVPixelFormat>(decoded_->format),
                             profile_.preview_width, profile_.preview_height,
                             encoder_->pix_fmt,
                             SWS_BILINEAR, nullptr, nullptr, nullptr);
    if (scaler_ == nullptr)
      Fail("cannot configure FFmpeg preview scaler");
    result = av_frame_make_writable(scaled_);
    if (result < 0)
      Fail("preview output frame is not writable: " + AvError(result));
    result = sws_scale(scaler_, decoded_->data, decoded_->linesize, 0,
                       decoded_->height, scaled_->data, scaled_->linesize);
    if (result != profile_.preview_height)
      Fail("FFmpeg preview scaler returned incomplete output");
    scaled_->pts = next_pts_++;
    scaled_->quality = encoder_->global_quality;

    result = avcodec_send_frame(encoder_, scaled_);
    if (result < 0)
      Fail("preview encoder rejected frame: " + AvError(result));
    av_packet_unref(output_packet_);
    result = avcodec_receive_packet(encoder_, output_packet_);
    if (result < 0)
      Fail("preview encoder produced no JPEG: " + AvError(result));
    if (output_packet_->size < 4 || output_packet_->data[0] != 0xff ||
        output_packet_->data[1] != 0xd8 ||
        output_packet_->data[output_packet_->size - 2] != 0xff ||
        output_packet_->data[output_packet_->size - 1] != 0xd9) {
      Fail("FFmpeg preview output is not a standalone JPEG with SOI/EOI");
    }
    std::vector<uint8_t> output(output_packet_->data,
                                output_packet_->data + output_packet_->size);
    av_frame_unref(decoded_);
    av_packet_unref(output_packet_);
    return output;
  }

  void ResetDecoder() { avcodec_flush_buffers(decoder_); }

private:
  const CaptureProfile profile_;
  AVCodecContext *decoder_ = nullptr;
  AVCodecContext *encoder_ = nullptr;
  AVFrame *decoded_ = nullptr;
  AVFrame *scaled_ = nullptr;
  AVPacket *input_packet_ = nullptr;
  AVPacket *output_packet_ = nullptr;
  SwsContext *scaler_ = nullptr;
  int64_t next_pts_ = 0;
};

void RunOfflinePreviewSelfTest(const fs::path &input_path,
                               const CaptureProfile &profile) {
  const int fd = open(input_path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0) {
    Fail("cannot open offline preview self-test input " + input_path.string() +
         ": " + std::strerror(errno));
  }
  int owned_fd = fd;
  try {
    struct stat metadata {};
    if (fstat(owned_fd, &metadata) != 0)
      Fail("cannot stat offline preview self-test input: " +
           std::string(std::strerror(errno)));
    if (!S_ISREG(metadata.st_mode) || metadata.st_size < 4 ||
        static_cast<uint64_t>(metadata.st_size) > kMaxMjpegPayloadBytes) {
      Fail("offline preview self-test input must be a regular JPEG no larger "
           "than 16 MiB");
    }
    PreviewPacket packet;
    packet.payload_size = static_cast<size_t>(metadata.st_size);
    packet.bytes.resize(packet.payload_size + AV_INPUT_BUFFER_PADDING_SIZE, 0);
    size_t offset = 0;
    while (offset < packet.payload_size) {
      const ssize_t count =
          read(owned_fd, packet.bytes.data() + offset, packet.payload_size - offset);
      if (count < 0 && errno == EINTR)
        continue;
      if (count <= 0)
        Fail("short read in offline preview self-test input");
      offset += static_cast<size_t>(count);
    }
    CloseFd(&owned_fd);
    JpegPreviewCodec codec(profile);
    const std::vector<uint8_t> output = codec.Convert(packet);
    std::cout << "OFFLINE_PREVIEW_SELF_TEST_OK input_bytes="
              << packet.payload_size << " output_bytes=" << output.size()
              << " view_mode=" << profile.view_mode
              << " input_geometry=" << profile.width << 'x' << profile.height
              << " output_geometry=" << profile.preview_width << 'x'
              << profile.preview_height
              << ' ' << FfmpegVersionSummary() << '\n';
  } catch (...) {
    CloseFd(&owned_fd);
    throw;
  }
}

class PreviewPublisher {
public:
  PreviewPublisher(fs::path preview_path, CaptureProfile profile)
      : preview_path_(std::move(preview_path)), profile_(profile),
        temporary_path_(preview_path_.string() + ".tmp." +
                        std::to_string(getpid())) {}

  ~PreviewPublisher() { Stop(); }

  void Start() {
    fs::create_directories(preview_path_.parent_path());
    thread_ = std::thread(&PreviewPublisher::Run, this);
  }

  void Submit(uint32_t sequence, int64_t soe_ns, const uint8_t *payload,
              size_t size) noexcept {
    try {
      // The capture thread sees native 30 Hz packets. Admit at most one packet
      // per 100 ms to the copying path; the other frames perform only an
      // atomic load/branch and return without allocation, memcpy, or preview
      // mutex acquisition.
      const int64_t now_ns = MonotonicNs();
      int64_t next_ns = next_copy_ns_.load(std::memory_order_relaxed);
      while (true) {
        if (now_ns < next_ns) {
          gate_skipped_.fetch_add(1, std::memory_order_relaxed);
          return;
        }
        if (next_copy_ns_.compare_exchange_weak(
                next_ns, now_ns + kPreviewPeriodNs, std::memory_order_relaxed,
                std::memory_order_relaxed)) {
          break;
        }
      }
      {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopping_)
          return;
      }
      PreviewPacket packet;
      packet.sequence = sequence;
      packet.soe_ns = soe_ns;
      packet.payload_size = size;
      packet.bytes.resize(size + AV_INPUT_BUFFER_PADDING_SIZE, 0);
      std::memcpy(packet.bytes.data(), payload, size);
      {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopping_)
          return;
        ++snapshot_.submitted;
        if (latest_.has_value())
          ++snapshot_.overwritten;
        latest_ = std::move(packet);
      }
      ready_.notify_one();
    } catch (const std::exception &error) {
      std::lock_guard<std::mutex> lock(mutex_);
      ++snapshot_.errors;
      snapshot_.last_error =
          "preview submit failed: " + std::string(error.what());
    } catch (...) {
      std::lock_guard<std::mutex> lock(mutex_);
      ++snapshot_.errors;
      snapshot_.last_error = "preview submit failed with an unknown error";
    }
  }

  void Stop() {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      stopping_ = true;
    }
    ready_.notify_all();
    if (thread_.joinable())
      thread_.join();
    std::error_code ignored;
    fs::remove(temporary_path_, ignored);
  }

  PreviewSnapshot Snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    PreviewSnapshot result = snapshot_;
    result.gate_skipped = gate_skipped_.load(std::memory_order_relaxed);
    return result;
  }

  bool WaitFirstPublished(double timeout_sec) {
    std::unique_lock<std::mutex> lock(mutex_);
    ready_.wait_for(lock, std::chrono::duration<double>(timeout_sec),
                    [&]() { return snapshot_.published > 0 || stopping_; });
    return snapshot_.published > 0;
  }

private:
  void AtomicReplace(const std::vector<uint8_t> &jpeg) {
    int fd = open(temporary_path_.c_str(),
                  O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0644);
    if (fd < 0)
      Fail("cannot open preview temporary file: " +
           std::string(std::strerror(errno)));
    try {
      WriteAll(fd, jpeg.data(), jpeg.size());
      CloseFd(&fd);
    } catch (...) {
      CloseFd(&fd);
      throw;
    }
    if (::rename(temporary_path_.c_str(), preview_path_.c_str()) != 0) {
      Fail("cannot atomically publish preview JPEG: " +
           std::string(std::strerror(errno)));
    }
  }

  void Run() {
    try {
      JpegPreviewCodec codec(profile_);
      auto next_publish = std::chrono::steady_clock::now();
      while (true) {
        PreviewPacket packet;
        {
          std::unique_lock<std::mutex> lock(mutex_);
          ready_.wait(lock, [&]() { return stopping_ || latest_.has_value(); });
          if (stopping_)
            break;
          // Preview is deliberately capped at 10 Hz. During this wait the
          // capture thread keeps replacing latest_ rather than building a
          // queue, so preview work can neither consume 30-Hz dual-camera CPU
          // nor apply backpressure to verbatim packet recording.
          while (!stopping_ &&
                 std::chrono::steady_clock::now() < next_publish) {
            ready_.wait_until(lock, next_publish, [&]() { return stopping_; });
          }
          if (stopping_)
            break;
          if (!latest_.has_value())
            continue;
          packet = std::move(*latest_);
          latest_.reset();
        }
        try {
          const auto jpeg = codec.Convert(packet);
          AtomicReplace(jpeg);
          uint64_t published = 0;
          {
            std::lock_guard<std::mutex> lock(mutex_);
            ++snapshot_.published;
            snapshot_.latest_sequence = packet.sequence;
            snapshot_.latest_soe_ns = packet.soe_ns;
            snapshot_.last_error.clear();
            published = snapshot_.published;
          }
          ready_.notify_all();
          if (published == 1 || published % kPreviewFps == 0) {
            Emit("frame", true, "preview JPEG published",
                 "\"sequence\":" + std::to_string(packet.sequence) +
                     ",\"soe_monotonic_ns\":" + std::to_string(packet.soe_ns) +
                     ",\"preview_path\":" + JsonEscape(preview_path_.string()) +
                     "," + CaptureProfileJson(profile_));
          }
        } catch (const std::exception &error) {
          codec.ResetDecoder();
          uint64_t errors = 0;
          {
            std::lock_guard<std::mutex> lock(mutex_);
            ++snapshot_.errors;
            snapshot_.last_error = error.what();
            errors = snapshot_.errors;
          }
          if (errors == 1 || errors % 30 == 0) {
            Emit("error", false, "preview frame was rejected",
                 "\"fatal\":false,\"sequence\":" +
                     std::to_string(packet.sequence) +
                     ",\"detail\":" + JsonEscape(error.what()));
          }
        }
        next_publish = std::chrono::steady_clock::now() + kPreviewPeriod;
      }
    } catch (const std::exception &error) {
      {
        std::lock_guard<std::mutex> lock(mutex_);
        ++snapshot_.errors;
        snapshot_.last_error = error.what();
        stopping_ = true;
        latest_.reset();
      }
      ready_.notify_all();
      Emit("error", false, "preview thread stopped",
           "\"fatal\":false,\"detail\":" + JsonEscape(error.what()));
    }
  }

  const fs::path preview_path_;
  const CaptureProfile profile_;
  const fs::path temporary_path_;
  mutable std::mutex mutex_;
  std::condition_variable ready_;
  std::optional<PreviewPacket> latest_;
  PreviewSnapshot snapshot_;
  std::atomic<int64_t> next_copy_ns_{0};
  std::atomic<uint64_t> gate_skipped_{0};
  bool stopping_ = false;
  std::thread thread_;
};

struct RuntimeSnapshot {
  bool source_ready = false;
  bool capture_done = false;
  uint64_t startup_discarded = 0;
  uint64_t source_frames = 0;
  uint32_t latest_sequence = 0;
  int64_t first_soe_ns = 0;
  int64_t latest_soe_ns = 0;
  int64_t latest_soe_raw_ns = 0;
  int64_t latest_dequeue_ns = 0;
  int64_t latest_monotonic_to_raw_offset_ns = 0;
  int64_t latest_monotonic_to_raw_error_bound_ns = 0;
  int64_t max_monotonic_to_raw_error_bound_ns = 0;
  uint64_t source_interval_samples = 0;
  double max_source_interval_ms = 0.0;
  uint64_t allowed_long_jitter_intervals = 0;
  uint64_t rolling_fps_samples = 0;
  double min_rolling_source_fps = 0.0;
  double max_rolling_source_fps = 0.0;
  double latest_rolling_source_fps = 0.0;
  std::string fatal_error;
};

class RuntimeState {
public:
  void MarkReady() {
    std::lock_guard<std::mutex> lock(mutex_);
    source_ready_ = true;
    changed_.notify_all();
  }

  void AddStartupDiscard() {
    std::lock_guard<std::mutex> lock(mutex_);
    ++startup_discarded_;
  }

  void AddFrame(uint32_t sequence, int64_t soe_ns, int64_t soe_raw_ns,
                int64_t dequeue_ns, int64_t monotonic_to_raw_offset_ns,
                int64_t monotonic_to_raw_error_bound_ns,
                double source_interval_ms, double rolling_source_fps) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (source_frames_ == 0)
      first_soe_ns_ = soe_ns;
    ++source_frames_;
    latest_sequence_ = sequence;
    latest_soe_ns_ = soe_ns;
    latest_soe_raw_ns_ = soe_raw_ns;
    latest_dequeue_ns_ = dequeue_ns;
    latest_monotonic_to_raw_offset_ns_ = monotonic_to_raw_offset_ns;
    latest_monotonic_to_raw_error_bound_ns_ =
        monotonic_to_raw_error_bound_ns;
    max_monotonic_to_raw_error_bound_ns_ =
        std::max(max_monotonic_to_raw_error_bound_ns_,
                 monotonic_to_raw_error_bound_ns);
    ++source_interval_samples_;
    max_source_interval_ms_ =
        std::max(max_source_interval_ms_, source_interval_ms);
    if (source_interval_ms > kStartupMaxSourceIntervalMs)
      ++allowed_long_jitter_intervals_;
    if (rolling_source_fps > 0.0) {
      if (rolling_fps_samples_ == 0) {
        min_rolling_source_fps_ = rolling_source_fps;
        max_rolling_source_fps_ = rolling_source_fps;
      } else {
        min_rolling_source_fps_ =
            std::min(min_rolling_source_fps_, rolling_source_fps);
        max_rolling_source_fps_ =
            std::max(max_rolling_source_fps_, rolling_source_fps);
      }
      latest_rolling_source_fps_ = rolling_source_fps;
      ++rolling_fps_samples_;
    }
  }

  bool SetFatal(const std::string &error) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!fatal_error_.empty())
      return false;
    fatal_error_ = error;
    changed_.notify_all();
    return true;
  }

  void MarkDone() {
    std::lock_guard<std::mutex> lock(mutex_);
    capture_done_ = true;
    changed_.notify_all();
  }

  bool WaitReady(double timeout_sec) {
    std::unique_lock<std::mutex> lock(mutex_);
    changed_.wait_for(lock, std::chrono::duration<double>(timeout_sec), [&]() {
      return source_ready_ || !fatal_error_.empty() || capture_done_;
    });
    return source_ready_ && fatal_error_.empty();
  }

  RuntimeSnapshot Snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    RuntimeSnapshot result;
    result.source_ready = source_ready_;
    result.capture_done = capture_done_;
    result.startup_discarded = startup_discarded_;
    result.source_frames = source_frames_;
    result.latest_sequence = latest_sequence_;
    result.first_soe_ns = first_soe_ns_;
    result.latest_soe_ns = latest_soe_ns_;
    result.latest_soe_raw_ns = latest_soe_raw_ns_;
    result.latest_dequeue_ns = latest_dequeue_ns_;
    result.latest_monotonic_to_raw_offset_ns =
        latest_monotonic_to_raw_offset_ns_;
    result.latest_monotonic_to_raw_error_bound_ns =
        latest_monotonic_to_raw_error_bound_ns_;
    result.max_monotonic_to_raw_error_bound_ns =
        max_monotonic_to_raw_error_bound_ns_;
    result.source_interval_samples = source_interval_samples_;
    result.max_source_interval_ms = max_source_interval_ms_;
    result.allowed_long_jitter_intervals = allowed_long_jitter_intervals_;
    result.rolling_fps_samples = rolling_fps_samples_;
    result.min_rolling_source_fps = min_rolling_source_fps_;
    result.max_rolling_source_fps = max_rolling_source_fps_;
    result.latest_rolling_source_fps = latest_rolling_source_fps_;
    result.fatal_error = fatal_error_;
    return result;
  }

private:
  mutable std::mutex mutex_;
  std::condition_variable changed_;
  bool source_ready_ = false;
  bool capture_done_ = false;
  uint64_t startup_discarded_ = 0;
  uint64_t source_frames_ = 0;
  uint32_t latest_sequence_ = 0;
  int64_t first_soe_ns_ = 0;
  int64_t latest_soe_ns_ = 0;
  int64_t latest_soe_raw_ns_ = 0;
  int64_t latest_dequeue_ns_ = 0;
  int64_t latest_monotonic_to_raw_offset_ns_ = 0;
  int64_t latest_monotonic_to_raw_error_bound_ns_ = 0;
  int64_t max_monotonic_to_raw_error_bound_ns_ = 0;
  uint64_t source_interval_samples_ = 0;
  double max_source_interval_ms_ = 0.0;
  uint64_t allowed_long_jitter_intervals_ = 0;
  uint64_t rolling_fps_samples_ = 0;
  double min_rolling_source_fps_ = 0.0;
  double max_rolling_source_fps_ = 0.0;
  double latest_rolling_source_fps_ = 0.0;
  std::string fatal_error_;
};

struct Mapping {
  void *address = MAP_FAILED;
  size_t length = 0;
};

class V4l2Capture {
public:
  V4l2Capture(const Options &options, Recorder *recorder,
              PreviewPublisher *preview, RuntimeState *state,
              std::atomic<bool> *protocol_ready)
      : options_(options), recorder_(recorder), preview_(preview),
        state_(state), protocol_ready_(protocol_ready) {}

  ~V4l2Capture() {
    g_stop.store(true, std::memory_order_relaxed);
    Join();
    Close();
  }

  void Open() {
    fd_ = open(options_.device.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (fd_ < 0) {
      Fail("cannot open explicit UVC device " + options_.device.string() +
           ": " + std::strerror(errno));
    }

    v4l2_capability capability{};
    if (Xioctl(fd_, VIDIOC_QUERYCAP, &capability) < 0) {
      Fail(std::string("VIDIOC_QUERYCAP failed: ") + std::strerror(errno));
    }
    const uint32_t capabilities =
        (capability.capabilities & V4L2_CAP_DEVICE_CAPS)
            ? capability.device_caps
            : capability.capabilities;
    if (!(capabilities & V4L2_CAP_VIDEO_CAPTURE) ||
        !(capabilities & V4L2_CAP_STREAMING)) {
      Fail("device is not a V4L2 streaming video-capture node");
    }
    card_name_ = reinterpret_cast<const char *>(capability.card);
    camera_identity_ = CameraIdentityForCard(card_name_, options_.profile);
    recorder_->SetCameraIdentity(card_name_, camera_identity_);

    v4l2_format format{};
    format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    format.fmt.pix.width = options_.profile.width;
    format.fmt.pix.height = options_.profile.height;
    format.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
    format.fmt.pix.field = V4L2_FIELD_NONE;
    if (Xioctl(fd_, VIDIOC_S_FMT, &format) < 0) {
      Fail(std::string("VIDIOC_S_FMT failed: ") + std::strerror(errno));
    }
    if (format.fmt.pix.width != options_.profile.width ||
        format.fmt.pix.height != options_.profile.height ||
        format.fmt.pix.pixelformat != V4L2_PIX_FMT_MJPEG) {
      Fail("driver substituted required " +
           std::string(options_.profile.view_mode) + " MJPG " +
           std::to_string(options_.profile.width) + "x" +
           std::to_string(options_.profile.height) + " with " +
           FourccText(format.fmt.pix.pixelformat) + " " +
           std::to_string(format.fmt.pix.width) + "x" +
           std::to_string(format.fmt.pix.height));
    }

    v4l2_streamparm parameters{};
    parameters.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    parameters.parm.capture.timeperframe.numerator = 1;
    parameters.parm.capture.timeperframe.denominator = kFps;
    if (Xioctl(fd_, VIDIOC_S_PARM, &parameters) < 0) {
      Fail(std::string("VIDIOC_S_PARM failed: ") + std::strerror(errno));
    }
    const auto interval = parameters.parm.capture.timeperframe;
    if (interval.numerator == 0 || interval.denominator == 0 ||
        std::abs(static_cast<double>(interval.denominator) /
                     interval.numerator -
                 kFps) > 0.01) {
      Fail("driver did not accept exact 30 fps");
    }
    v4l2_requestbuffers request{};
    request.count = kBufferCount;
    request.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    request.memory = V4L2_MEMORY_MMAP;
    if (Xioctl(fd_, VIDIOC_REQBUFS, &request) < 0) {
      Fail(std::string("VIDIOC_REQBUFS failed: ") + std::strerror(errno));
    }
    if (request.count < 2 || request.count > kBufferCount) {
      Fail("driver returned unsafe mmap buffer count " +
           std::to_string(request.count));
    }
    actual_buffer_count_ = request.count;
    recorder_->SetNegotiated(format.fmt.pix.pixelformat, format.fmt.pix.width,
                             format.fmt.pix.height,
                             static_cast<double>(interval.denominator) /
                                 static_cast<double>(interval.numerator),
                             format.fmt.pix.sizeimage, request.count);
    mappings_.resize(request.count);
    for (uint32_t index = 0; index < request.count; ++index) {
      v4l2_buffer buffer{};
      buffer.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
      buffer.memory = V4L2_MEMORY_MMAP;
      buffer.index = index;
      if (Xioctl(fd_, VIDIOC_QUERYBUF, &buffer) < 0) {
        Fail(std::string("VIDIOC_QUERYBUF failed: ") + std::strerror(errno));
      }
      mappings_[index].length = buffer.length;
      mappings_[index].address = mmap(nullptr, buffer.length, PROT_READ,
                                      MAP_SHARED, fd_, buffer.m.offset);
      if (mappings_[index].address == MAP_FAILED) {
        Fail(std::string("mmap failed: ") + std::strerror(errno));
      }
      if (Xioctl(fd_, VIDIOC_QBUF, &buffer) < 0) {
        Fail(std::string("initial VIDIOC_QBUF failed: ") +
             std::strerror(errno));
      }
    }

    v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (Xioctl(fd_, VIDIOC_STREAMON, &type) < 0) {
      Fail(std::string("VIDIOC_STREAMON failed: ") + std::strerror(errno));
    }
    streaming_ = true;
    std::cerr << "[insta-uvc] opened explicit device=" << options_.device
              << " role=" << options_.role << " card=" << card_name_
              << " camera_model=" << camera_identity_.model
              << " view_mode=" << options_.profile.view_mode
              << " format=MJPG geometry=" << options_.profile.width << 'x'
              << options_.profile.height << " fps=30 projection="
              << options_.profile.projection << " layout="
              << options_.profile.layout << " buffers="
              << request.count << "\n";
  }

  void StartThread() { thread_ = std::thread(&V4l2Capture::Run, this); }

  void Join() {
    if (thread_.joinable())
      thread_.join();
  }

  void Close() {
    if (streaming_ && fd_ >= 0) {
      v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
      Xioctl(fd_, VIDIOC_STREAMOFF, &type);
      streaming_ = false;
    }
    for (auto &mapping : mappings_) {
      if (mapping.address != MAP_FAILED)
        munmap(mapping.address, mapping.length);
      mapping.address = MAP_FAILED;
    }
    mappings_.clear();
    CloseFd(&fd_);
  }

  const std::string &CardName() const { return card_name_; }
  const CameraIdentity &Identity() const { return camera_identity_; }
  uint32_t BufferCount() const { return actual_buffer_count_; }

private:
  void Requeue(v4l2_buffer *buffer) {
    if (Xioctl(fd_, VIDIOC_QBUF, buffer) < 0) {
      Fail(std::string("VIDIOC_QBUF failed: ") + std::strerror(errno));
    }
  }

  void Run() {
    try {
      const double nominal_ms = 1000.0 / kFps;
      const double stable_min_ms = nominal_ms * 0.45;
      const double stable_max_ms = kStartupMaxSourceIntervalMs;
      const int64_t warmup_deadline =
          MonotonicNs() + static_cast<int64_t>(kWarmupTimeoutSec * 1.0e9);
      int stable_run = 0;
      bool admitted = false;
      int64_t previous_source_ns = 0;
      int64_t previous_dequeue_ns = 0;
      int64_t last_arrival_ns = MonotonicNs();
      bool have_sequence = false;
      uint32_t previous_sequence = 0;
      std::deque<int64_t> rolling_source_timestamps;
      bool preview_cadence_warning_active = false;

      while (!g_stop.load(std::memory_order_relaxed)) {
        pollfd descriptor{fd_, POLLIN | POLLPRI, 0};
        const int polled = poll(&descriptor, 1, 250);
        if (polled < 0 && errno == EINTR)
          continue;
        if (polled < 0)
          Fail(std::string("UVC poll failed: ") + std::strerror(errno));
        if (polled == 0) {
          const FrameWaitTimeout timeout = EvaluateFrameWaitTimeout(
              admitted, MonotonicNs(), last_arrival_ns, warmup_deadline);
          if (timeout == FrameWaitTimeout::kStartupWarmup) {
            Fail("UVC startup did not reach a stable native frame cadence "
                 "within 10 seconds");
          }
          if (timeout == FrameWaitTimeout::kReadyStall) {
            Fail("UVC ready stream stalled for more than 2 seconds");
          }
          continue;
        }
        if (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) {
          Fail("UVC device reported poll error flags " +
               std::to_string(descriptor.revents));
        }

        v4l2_buffer buffer{};
        buffer.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buffer.memory = V4L2_MEMORY_MMAP;
        if (Xioctl(fd_, VIDIOC_DQBUF, &buffer) < 0) {
          if (errno == EAGAIN)
            continue;
          Fail(std::string("VIDIOC_DQBUF failed: ") + std::strerror(errno));
        }
        bool requeued = false;
        try {
          const MonotonicRawMapping raw_mapping =
              SampleMonotonicRawMapping();
          // Preserve dequeue_monotonic_ns as the earliest host timestamp
          // immediately following DQBUF. The two later clock reads exist only
          // to map this frame's SOE into CLOCK_MONOTONIC_RAW.
          const int64_t dequeue_ns = raw_mapping.monotonic_before_ns;
          last_arrival_ns = dequeue_ns;
          if (buffer.flags & V4L2_BUF_FLAG_ERROR) {
            Fail("UVC driver marked buffer sequence " +
                 std::to_string(buffer.sequence) + " with V4L2_BUF_FLAG_ERROR");
          }
          const int64_t source_ns = TimevalNs(buffer.timestamp);
          if (source_ns <= 0 ||
              (previous_source_ns > 0 && source_ns <= previous_source_ns)) {
            Fail("UVC SOE timestamp is not strictly positive and increasing");
          }
          const int64_t source_raw_ns =
              source_ns + raw_mapping.raw_minus_monotonic_ns;
          if (!(buffer.flags & V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC) ||
              (buffer.flags & V4L2_BUF_FLAG_TSTAMP_SRC_MASK) !=
                  V4L2_BUF_FLAG_TSTAMP_SRC_SOE) {
            Fail(
                "UVC buffer lacks monotonic start-of-exposure timestamp flags");
          }
          if (buffer.index >= mappings_.size() || buffer.bytesused < 4 ||
              buffer.bytesused > mappings_[buffer.index].length) {
            Fail("invalid UVC MJPEG buffer bounds");
          }
          const double source_interval_ms =
              previous_source_ns ? ElapsedMs(previous_source_ns, source_ns)
                                 : std::numeric_limits<double>::quiet_NaN();
          const double dequeue_interval_ms =
              previous_dequeue_ns ? ElapsedMs(previous_dequeue_ns, dequeue_ns)
                                  : std::numeric_limits<double>::quiet_NaN();
          const double driver_age_ms = ElapsedMs(source_ns, dequeue_ns);
          previous_source_ns = source_ns;
          previous_dequeue_ns = dequeue_ns;

          bool publish = admitted;
          if (!admitted) {
            state_->AddStartupDiscard();
            const bool stable = std::isfinite(source_interval_ms) &&
                                source_interval_ms >= stable_min_ms &&
                                source_interval_ms <= stable_max_ms &&
                                std::isfinite(dequeue_interval_ms) &&
                                dequeue_interval_ms >= stable_min_ms &&
                                dequeue_interval_ms <= stable_max_ms &&
                                driver_age_ms >= 0.0 &&
                                driver_age_ms <= kMaxDriverAgeMs;
            stable_run = stable ? stable_run + 1 : 0;
            if (stable_run >= kStableIntervals) {
              admitted = true;
              publish = true;
              state_->MarkReady();
              std::cerr << "[insta-uvc] startup backlog drained role="
                        << options_.role << " sequence=" << buffer.sequence
                        << " driver_age_ms=" << driver_age_ms << "\n";
            } else if (dequeue_ns > warmup_deadline) {
              Fail("UVC startup backlog did not reach fresh native cadence");
            }
          }

          if (publish) {
            if (source_ns <= 0 || driver_age_ms < 0.0 ||
                driver_age_ms > kMaxDriverAgeMs) {
              Fail("UVC frame violated driver SOE age contract: " +
                   std::to_string(driver_age_ms) + " ms");
            }
            const SourceIntervalClass interval_class =
                ClassifyPostReadySourceInterval(source_interval_ms);
            if (interval_class == SourceIntervalClass::kFatal) {
              Fail("UVC SOE source interval violated post-ready hard range: " +
                   std::to_string(source_interval_ms) + " ms not in (0, " +
                   std::to_string(kPostReadyHardMaxSourceIntervalMs) + "] ms");
            }
            if (have_sequence &&
                buffer.sequence !=
                    static_cast<uint32_t>(previous_sequence + 1U)) {
              Fail("UVC source sequence gap: previous=" +
                   std::to_string(previous_sequence) +
                   " current=" + std::to_string(buffer.sequence));
            }
            have_sequence = true;
            previous_sequence = buffer.sequence;
            rolling_source_timestamps.push_back(source_ns);
            if (rolling_source_timestamps.size() > kRollingFpsWindowFrames)
              rolling_source_timestamps.pop_front();
            const double rolling_source_fps =
                RollingFps(rolling_source_timestamps);
            if (rolling_source_fps > 0.0 &&
                !RollingFpsIsHealthy(rolling_source_fps)) {
              if (!preview_cadence_warning_active) {
                std::cerr
                    << "[insta-uvc] preview cadence warning role="
                    << options_.role << " rolling_fps_300="
                    << rolling_source_fps
                    << " outside [27, 33]; preview continues and recording "
                       "remains fail-closed\n";
                preview_cadence_warning_active = true;
              }
            } else if (preview_cadence_warning_active) {
              std::cerr << "[insta-uvc] preview cadence recovered role="
                        << options_.role << " rolling_fps_300="
                        << rolling_source_fps << '\n';
              preview_cadence_warning_active = false;
            }
            const auto *payload =
                static_cast<const uint8_t *>(mappings_[buffer.index].address);
            recorder_->Append(
                buffer.sequence, source_ns, source_raw_ns, dequeue_ns,
                raw_mapping.raw_minus_monotonic_ns,
                raw_mapping.error_bound_ns, source_interval_ms,
                rolling_source_fps, payload, buffer.bytesused);
            if (protocol_ready_->load(std::memory_order_acquire)) {
              preview_->Submit(buffer.sequence, source_ns, payload,
                               buffer.bytesused);
            }
            state_->AddFrame(buffer.sequence, source_ns, source_raw_ns,
                             dequeue_ns,
                             raw_mapping.raw_minus_monotonic_ns,
                             raw_mapping.error_bound_ns, source_interval_ms,
                             rolling_source_fps);
          }
          Requeue(&buffer);
          requeued = true;
        } catch (...) {
          if (!requeued) {
            Xioctl(fd_, VIDIOC_QBUF, &buffer);
          }
          throw;
        }
      }
    } catch (const std::exception &error) {
      recorder_->Abort("capture failed closed: " + std::string(error.what()));
      if (state_->SetFatal(error.what())) {
        Emit("error", false, "UVC capture failed closed",
             "\"fatal\":true,\"detail\":" + JsonEscape(error.what()));
      }
      g_stop.store(true, std::memory_order_relaxed);
    }
    state_->MarkDone();
  }

  const Options &options_;
  Recorder *recorder_;
  PreviewPublisher *preview_;
  RuntimeState *state_;
  std::atomic<bool> *protocol_ready_;
  int fd_ = -1;
  bool streaming_ = false;
  std::vector<Mapping> mappings_;
  std::string card_name_;
  CameraIdentity camera_identity_{"unknown", false, "not_verified"};
  uint32_t actual_buffer_count_ = 0;
  std::thread thread_;
};

std::string RecordingJson(const RecordingSnapshot &recording,
                          const CaptureProfile &profile) {
  std::ostringstream output;
  output << "\"recording\":" << (recording.recording ? "true" : "false")
         << ",\"recording_lifecycle\":" << JsonEscape(recording.lifecycle)
         << ",\"artifact_paths_are_actual\":true"
         << ',' << CaptureProfileJson(profile)
         << ",\"recorded_frames\":" << recording.frames
         << ",\"recorded_payload_bytes\":" << recording.bytes
         << ",\"recorded_source_interval_samples\":"
         << recording.source_interval_samples
         << ",\"recorded_max_source_interval_ms\":" << std::fixed
         << std::setprecision(3) << recording.max_source_interval_ms
         << ",\"recorded_allowed_long_jitter_intervals\":"
         << recording.allowed_long_jitter_intervals
         << ",\"recorded_rolling_fps_300_samples\":"
         << recording.rolling_fps_samples
         << ",\"recorded_min_rolling_source_fps_300\":"
         << recording.min_rolling_source_fps
         << ",\"recorded_max_rolling_source_fps_300\":"
         << recording.max_rolling_source_fps
         << ",\"recorded_latest_rolling_source_fps_300\":"
         << recording.latest_rolling_source_fps
         << ",\"writer_enqueued\":" << recording.writer_enqueued
         << ",\"writer_written\":" << recording.writer_written
         << ",\"writer_queue_capacity\":" << recording.writer_queue_capacity
         << ",\"writer_queue_depth\":" << recording.writer_queue_depth
         << ",\"writer_queue_high_water\":" << recording.writer_queue_high_water
         << ",\"v4l2_mmap_buffer_count\":" << recording.v4l2_buffer_count;
  if (!recording.output_dir.empty()) {
    output << ",\"output_dir\":" << JsonEscape(recording.output_dir.string());
  }
  if (!recording.video_path.empty()) {
    output << ",\"video_path\":" << JsonEscape(recording.video_path.string());
  }
  if (!recording.frames_path.empty()) {
    output << ",\"frames_path\":" << JsonEscape(recording.frames_path.string());
  }
  if (!recording.manifest_path.empty()) {
    output << ",\"manifest_path\":"
           << JsonEscape(recording.manifest_path.string());
  }
  if (!recording.failure.empty()) {
    output << ",\"recording_failure\":" << JsonEscape(recording.failure);
  }
  if (!recording.writer_error.empty()) {
    output << ",\"writer_error\":" << JsonEscape(recording.writer_error);
  }
  if (recording.frames > 0) {
    output << ",\"first_sequence\":" << recording.first_sequence
           << ",\"last_sequence\":" << recording.last_sequence
           << ",\"first_soe_monotonic_ns\":" << recording.first_soe_ns
           << ",\"last_soe_monotonic_ns\":" << recording.last_soe_ns
           << ",\"first_soe_monotonic_raw_ns\":"
           << recording.first_soe_raw_ns
           << ",\"last_soe_monotonic_raw_ns\":"
           << recording.last_soe_raw_ns
           << ",\"recorded_min_monotonic_to_raw_offset_ns\":"
           << recording.min_monotonic_to_raw_offset_ns
           << ",\"recorded_max_monotonic_to_raw_offset_ns\":"
           << recording.max_monotonic_to_raw_offset_ns
           << ",\"recorded_max_monotonic_to_raw_error_bound_ns\":"
           << recording.max_monotonic_to_raw_error_bound_ns;
  }
  return output.str();
}

std::string StatusJson(const Options &options, const RuntimeSnapshot &runtime,
                       const RecordingSnapshot &recording,
                       const PreviewSnapshot &preview) {
  double source_fps = 0.0;
  if (runtime.source_frames >= 2 &&
      runtime.latest_soe_ns > runtime.first_soe_ns) {
    source_fps =
        static_cast<double>(runtime.source_frames - 1) * 1.0e9 /
        static_cast<double>(runtime.latest_soe_ns - runtime.first_soe_ns);
  }
  const double latest_age_ms =
      runtime.latest_soe_ns > 0
          ? ElapsedMs(runtime.latest_soe_ns, MonotonicNs())
          : -1.0;
  std::ostringstream output;
  output.setf(std::ios::fixed);
  output << std::setprecision(3) << "\"connected\":"
         << (runtime.source_ready && runtime.fatal_error.empty() ? "true"
                                                                 : "false")
         << ",\"source_ready\":" << (runtime.source_ready ? "true" : "false")
         << ",\"capture_done\":" << (runtime.capture_done ? "true" : "false")
         << ",\"device\":" << JsonEscape(options.device.string())
         << ",\"pixel_format\":\"MJPG\"," << CaptureProfileJson(options.profile)
         << ",\"fps\":" << kFps
         << ",\"source_frames\":" << runtime.source_frames
         << ",\"startup_discarded\":" << runtime.startup_discarded
         << ",\"latest_sequence\":" << runtime.latest_sequence
         << ",\"latest_soe_monotonic_ns\":" << runtime.latest_soe_ns
         << ",\"latest_soe_monotonic_raw_ns\":"
         << runtime.latest_soe_raw_ns
         << ",\"latest_monotonic_to_raw_offset_ns\":"
         << runtime.latest_monotonic_to_raw_offset_ns
         << ",\"latest_monotonic_to_raw_error_bound_ns\":"
         << runtime.latest_monotonic_to_raw_error_bound_ns
         << ",\"max_monotonic_to_raw_error_bound_ns\":"
         << runtime.max_monotonic_to_raw_error_bound_ns
         << ",\"source_fps_observed\":" << source_fps
         << ",\"latest_source_age_ms\":" << latest_age_ms << ','
         << "\"source_interval_samples\":" << runtime.source_interval_samples
         << ",\"max_source_interval_ms\":" << runtime.max_source_interval_ms
         << ",\"allowed_long_jitter_intervals\":"
         << runtime.allowed_long_jitter_intervals
         << ",\"rolling_fps_window_frames\":" << kRollingFpsWindowFrames
         << ",\"rolling_fps_minimum\":" << kRollingFpsMinimum
         << ",\"rolling_fps_maximum\":" << kRollingFpsMaximum
         << ",\"rolling_fps_300_samples\":" << runtime.rolling_fps_samples
         << ",\"min_rolling_source_fps_300\":"
         << runtime.min_rolling_source_fps
         << ",\"max_rolling_source_fps_300\":"
         << runtime.max_rolling_source_fps
         << ",\"latest_rolling_source_fps_300\":"
         << runtime.latest_rolling_source_fps
         << ",\"startup_stable_max_source_interval_ms\":"
         << kStartupMaxSourceIntervalMs
         << ",\"post_ready_hard_max_source_interval_ms\":"
         << kPostReadyHardMaxSourceIntervalMs
         << ",\"max_allowed_source_interval_ms\":"
         << kPostReadyHardMaxSourceIntervalMs
         << ',' << RecordingJson(recording, options.profile)
         << ",\"preview_path\":" << JsonEscape(options.preview_path.string())
         << ",\"preview_target_fps\":" << kPreviewFps
         << ",\"preview_submitted\":" << preview.submitted
         << ",\"preview_published\":" << preview.published
         << ",\"preview_overwritten\":" << preview.overwritten
         << ",\"preview_gate_skipped\":" << preview.gate_skipped
         << ",\"preview_errors\":" << preview.errors;
  if (!preview.last_error.empty()) {
    output << ",\"preview_last_error\":" << JsonEscape(preview.last_error);
  }
  if (!runtime.fatal_error.empty()) {
    output << ",\"fatal_error\":" << JsonEscape(runtime.fatal_error);
  }
  return output.str();
}

bool HandleCommand(const std::string &raw_command, const Options &options,
                   Recorder *recorder, RuntimeState *runtime,
                   PreviewPublisher *preview) {
  const std::string command = Trim(raw_command);
  if (command.empty())
    return true;
  try {
    if (command.rfind("START\t", 0) == 0) {
      const fs::path session_dir = DecodeJsonString(command.substr(6));
      const auto state = runtime->Snapshot();
      if (!state.source_ready || !state.fatal_error.empty()) {
        Fail("UVC source is not ready for recording");
      }
      const RecordingSnapshot started = recorder->Start(session_dir);
      Emit("start", true, "UVC recording started",
           RecordingJson(started, options.profile) +
               ",\"session_dir\":" + JsonEscape(started.session_dir.string()));
      return true;
    }
    if (command == "STOP") {
      const FinalizeResult result = recorder->Stop();
      Emit("stop", result.ok, result.message,
           RecordingJson(result.snapshot, options.profile));
      if (!result.ok) {
        runtime->SetFatal(result.message);
        g_stop.store(true, std::memory_order_relaxed);
        return false;
      }
      return true;
    }
    if (command == "ABORT") {
      g_abort_command.store(true, std::memory_order_relaxed);
      const RecordingSnapshot snapshot =
          recorder->Abort("recording aborted by protocol command");
      Emit("abort", true, "UVC recording aborted; partial artifacts retained",
           RecordingJson(snapshot, options.profile));
      g_stop.store(true, std::memory_order_relaxed);
      return false;
    }
    if (command == "STATUS") {
      Emit("status", true, "UVC worker status",
           StatusJson(options, runtime->Snapshot(), recorder->Snapshot(),
                      preview->Snapshot()));
      return true;
    }
    if (command == "QUIT") {
      const FinalizeResult result = recorder->Stop();
      if (!result.ok) {
        Emit("quit", false, result.message,
             RecordingJson(result.snapshot, options.profile));
        runtime->SetFatal(result.message);
      } else {
        Emit("quit", true, "UVC worker closing",
             RecordingJson(result.snapshot, options.profile));
      }
      g_stop.store(true, std::memory_order_relaxed);
      return false;
    }
    Emit("error", false, "unknown UVC worker command",
         "\"fatal\":false,\"command\":" + JsonEscape(command));
  } catch (const std::exception &error) {
    const std::string action =
        command.rfind("START\t", 0) == 0 ? "start" : "error";
    Emit(action, false, error.what(), "\"fatal\":false");
  }
  return true;
}

void CommandLoop(const Options &options, Recorder *recorder,
                 RuntimeState *runtime, PreviewPublisher *preview) {
  const int original_flags = fcntl(STDIN_FILENO, F_GETFL, 0);
  ScopedFdFlags restore_stdin_flags(STDIN_FILENO, original_flags);
  if (original_flags >= 0)
    fcntl(STDIN_FILENO, F_SETFL, original_flags | O_NONBLOCK);
  std::string pending;
  bool stdin_closed = false;
  while (!g_stop.load(std::memory_order_relaxed) && !stdin_closed) {
    pollfd descriptor{STDIN_FILENO, POLLIN | POLLHUP, 0};
    const int result = poll(&descriptor, 1, 250);
    if (result < 0 && errno == EINTR)
      continue;
    if (result < 0)
      Fail(std::string("stdin poll failed: ") + std::strerror(errno));
    if (result == 0)
      continue;
    if (descriptor.revents & POLLNVAL)
      Fail("stdin became invalid");
    if (descriptor.revents & (POLLIN | POLLHUP)) {
      char buffer[4096];
      while (true) {
        const ssize_t count = read(STDIN_FILENO, buffer, sizeof(buffer));
        if (count > 0) {
          if (pending.size() + static_cast<size_t>(count) >
              kMaxStdinBufferBytes) {
            Fail("stdin command buffer exceeded 64 KiB without being drained");
          }
          pending.append(buffer, static_cast<size_t>(count));
          size_t newline = 0;
          while ((newline = pending.find('\n')) != std::string::npos) {
            std::string line = pending.substr(0, newline);
            pending.erase(0, newline + 1);
            if (!line.empty() && line.back() == '\r')
              line.pop_back();
            if (!HandleCommand(line, options, recorder, runtime, preview))
              return;
          }
          continue;
        }
        if (count == 0) {
          stdin_closed = true;
          break;
        }
        if (errno == EINTR)
          continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK)
          break;
        Fail(std::string("stdin read failed: ") + std::strerror(errno));
      }
    }
  }
  if (!pending.empty() && !g_stop.load(std::memory_order_relaxed)) {
    HandleCommand(pending, options, recorder, runtime, preview);
  }
  if (!g_stop.load(std::memory_order_relaxed)) {
    const FinalizeResult result = recorder->Stop();
    Emit("quit", result.ok,
         result.ok ? "stdin closed; UVC worker closing" : result.message,
         RecordingJson(result.snapshot, options.profile));
    if (!result.ok)
      runtime->SetFatal(result.message);
    g_stop.store(true, std::memory_order_relaxed);
  }
}

} // namespace

int main(int argc, char **argv) {
  bool ready_response_emitted = false;
  CaptureProfile requested_profile = kPanoramaProfile;
  try {
    if (argc == 2 && std::string(argv[1]) == "--ffmpeg-abi-check") {
      VerifyFfmpegAbi();
      std::cout << "FFMPEG_ABI_OK " << FfmpegVersionSummary() << '\n';
      return 0;
    }
    if (argc == 2 &&
        std::string(argv[1]) == "--offline-startup-timing-self-test") {
      RunOfflineStartupTimingSelfTest();
      return 0;
    }
    if (argc == 2 &&
        std::string(argv[1]) == "--offline-profile-self-test") {
      RunOfflineProfileSelfTest();
      return 0;
    }
    if (argc >= 2 &&
        std::string(argv[1]) == "--offline-preview-self-test") {
      if (argc == 5 && std::string(argv[3]) == "--view-mode") {
        requested_profile = CaptureProfileForViewMode(argv[4]);
      } else if (argc != 3) {
        Fail("offline preview self-test usage: --offline-preview-self-test "
             "JPEG [--view-mode panorama|flat]");
      }
      VerifyFfmpegAbi();
      RunOfflinePreviewSelfTest(fs::path(argv[2]), requested_profile);
      return 0;
    }
    const Options options = ParseOptions(argc, argv);
    requested_profile = options.profile;
    g_protocol_role = options.role;
    // Check before starting the preview thread or opening the explicitly
    // configured V4L2 node.  A mismatched FFmpeg context layout is undefined
    // behaviour and must fail without touching a camera.
    VerifyFfmpegAbi();
    signal(SIGINT, SignalHandler);
    signal(SIGTERM, SignalHandler);
    signal(SIGPIPE, SIG_IGN);

    Recorder recorder(options.role, options.device, options.profile);
    PreviewPublisher preview(options.preview_path, options.profile);
    RuntimeState runtime;
    std::atomic<bool> protocol_ready{false};
    V4l2Capture capture(options, &recorder, &preview, &runtime,
                        &protocol_ready);

    preview.Start();
    capture.Open();
    capture.StartThread();
    if (!runtime.WaitReady(kWarmupTimeoutSec + 2.0)) {
      const auto state = runtime.Snapshot();
      const std::string detail =
          state.fatal_error.empty() ? "timed out waiting for a fresh UVC source"
                                    : state.fatal_error;
      runtime.SetFatal(detail);
      // The adapter is synchronously waiting for the lifecycle action
      // "ready".  A generic error event is informational and would leave it
      // waiting for the whole hardware timeout, which also delays application
      // shutdown.  Always complete that pending action with an explicit
      // negative response.
      ready_response_emitted = true;
      Emit("ready", false, "UVC worker failed before ready",
           "\"ready\":false,\"connected\":false,\"fatal\":true,\"detail\":" +
               JsonEscape(detail) + "," +
               CaptureProfileJson(options.profile));
      g_stop.store(true, std::memory_order_relaxed);
    } else {
      protocol_ready.store(true, std::memory_order_release);
      if (!preview.WaitFirstPublished(kPreviewReadyTimeoutSec)) {
        const PreviewSnapshot state = preview.Snapshot();
        const std::string detail =
            state.last_error.empty()
                ? "timed out waiting for first atomically published preview "
                  "JPEG"
                : "preview failed before ready: " + state.last_error;
        runtime.SetFatal(detail);
        ready_response_emitted = true;
        Emit("ready", false, "UVC preview failed before ready",
             "\"ready\":false,\"connected\":false,\"fatal\":true,\"detail\":" +
                 JsonEscape(detail) + "," +
                 CaptureProfileJson(options.profile));
        g_stop.store(true, std::memory_order_relaxed);
      } else {
        const auto state = runtime.Snapshot();
        ready_response_emitted = true;
        Emit("ready", true,
             capture.Identity().model +
                 " UVC source and first preview JPEG are ready",
             "\"ready\":true,\"connected\":true,\"device\":" +
                 JsonEscape(options.device.string()) +
                 ",\"card\":" + JsonEscape(capture.CardName()) +
                 "," + CameraIdentityJson(capture.Identity()) +
                 ",\"pixel_format\":\"MJPG\"," +
                 CaptureProfileJson(options.profile) +
                 ",\"fps\":" + std::to_string(kFps) +
                 ",\"v4l2_mmap_buffer_count\":" +
                 std::to_string(capture.BufferCount()) +
                 ",\"startup_discarded\":" +
                 std::to_string(state.startup_discarded) +
                 ",\"preview_path\":" +
                 JsonEscape(options.preview_path.string()) +
                 ",\"preview_target_fps\":" + std::to_string(kPreviewFps));
        CommandLoop(options, &recorder, &runtime, &preview);
      }
    }

    const int terminating_signal =
        g_signal_number.load(std::memory_order_relaxed);
    if (terminating_signal != 0) {
      recorder.Abort("worker terminated by signal " +
                     std::to_string(terminating_signal));
      runtime.SetFatal("worker terminated by signal " +
                       std::to_string(terminating_signal));
    } else if (!g_abort_command.load(std::memory_order_relaxed)) {
      const RecordingSnapshot recording = recorder.Snapshot();
      if (recording.lifecycle == "starting" || recording.recording ||
          recording.lifecycle == "finalizing") {
        recorder.Abort("worker stopped without a clean STOP");
      }
      const RecordingSnapshot final_recording = recorder.Snapshot();
      if (!final_recording.writer_error.empty() &&
          runtime.Snapshot().fatal_error.empty()) {
        runtime.SetFatal("bounded recording writer failed: " +
                         final_recording.writer_error);
      }
    }
    g_stop.store(true, std::memory_order_relaxed);
    capture.Join();
    capture.Close();
    preview.Stop();
    return runtime.Snapshot().fatal_error.empty() ? 0 : 2;
  } catch (const std::exception &error) {
    if (!g_protocol_role.empty() && !ready_response_emitted) {
      ready_response_emitted = true;
      Emit("ready", false, "UVC worker failed before ready",
           "\"ready\":false,\"connected\":false,\"fatal\":true,\"detail\":" +
               JsonEscape(error.what()) + "," +
               CaptureProfileJson(requested_profile));
    } else {
      Emit("error", false, "fatal UVC worker error",
           "\"fatal\":true,\"detail\":" + JsonEscape(error.what()) + "," +
               CaptureProfileJson(requested_profile));
    }
    std::cerr << "[insta-uvc] fatal: " << error.what() << '\n';
    return 2;
  }
}
