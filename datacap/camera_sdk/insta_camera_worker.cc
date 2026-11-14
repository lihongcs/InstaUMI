#include <camera/camera.h>
#include <camera/device_discovery.h>
#include <camera/photography_settings.h>

#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <ctime>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

namespace {

constexpr const char* kPrefix = "DATACAP_JSON ";

int64_t UnixTimeMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

int64_t SteadyTimeNs() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

std::string EscapeJson(const std::string& value) {
    std::ostringstream out;
    for (const unsigned char c : value) {
        switch (c) {
            case '\\': out << "\\\\"; break;
            case '"': out << "\\\""; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (c < 0x20) {
                    out << "?";
                } else {
                    out << c;
                }
        }
    }
    return out.str();
}

void Emit(const std::string& action, bool ok, const std::string& message,
          const std::string& extra_json = "") {
    std::cout << kPrefix << "{\"protocol\":\"insta-worker-v1\",\"action\":\""
              << EscapeJson(action) << "\",\"ok\":" << (ok ? "true" : "false")
              << ",\"message\":\"" << EscapeJson(message) << "\"";
    if (!extra_json.empty()) {
        std::cout << "," << extra_json;
    }
    std::cout << "}" << std::endl;
}

ins_camera::VideoResolution ParseResolution(const std::string& value) {
    if (value == "8k30") return ins_camera::VideoResolution::RES_8KP30;
    if (value == "5.7k30") return ins_camera::VideoResolution::RES_57KP30;
    if (value == "5.7k60") return ins_camera::VideoResolution::RES_57KP60;
    if (value == "4k30") return ins_camera::VideoResolution::RES_4KP30;
    if (value == "4k60") return ins_camera::VideoResolution::RES_4KP60;
    throw std::runtime_error("unsupported resolution: " + value);
}

std::string StringArrayJson(const std::vector<std::string>& values) {
    std::ostringstream out;
    out << "[";
    for (size_t index = 0; index < values.size(); ++index) {
        if (index != 0) out << ",";
        out << "\"" << EscapeJson(values[index]) << "\"";
    }
    out << "]";
    return out.str();
}

}  // namespace

int main(int argc, char* argv[]) {
    std::string serial;
    std::string resolution_name = "5.7k60";
    int bitrate_mbps = 120;
    for (int index = 1; index < argc; ++index) {
        const std::string arg = argv[index];
        if (arg == "--serial" && index + 1 < argc) {
            serial = argv[++index];
        } else if (arg == "--resolution" && index + 1 < argc) {
            resolution_name = argv[++index];
        } else if (arg == "--bitrate-mbps" && index + 1 < argc) {
            bitrate_mbps = std::atoi(argv[++index]);
        } else if (arg == "--help") {
            std::cout << "Usage: insta_camera_worker [--serial SERIAL] "
                         "[--resolution 8k30|5.7k60|5.7k30|4k60|4k30] "
                         "[--bitrate-mbps N]\n";
            return 0;
        }
    }

    ins_camera::VideoResolution resolution;
    try {
        resolution = ParseResolution(resolution_name);
    } catch (const std::exception& exc) {
        Emit("ready", false, exc.what());
        return 2;
    }

    ins_camera::SetLogLevel(ins_camera::LogLevel::ERR);
    ins_camera::DeviceDiscovery discovery;
    auto devices = discovery.GetAvailableDevices();
    if (devices.empty()) {
        Emit("ready", false, "no Insta360 camera found over USB or Wi-Fi");
        return 3;
    }

    size_t selected = 0;
    if (!serial.empty()) {
        bool found = false;
        for (size_t index = 0; index < devices.size(); ++index) {
            if (devices[index].serial_number == serial) {
                selected = index;
                found = true;
                break;
            }
        }
        if (!found) {
            discovery.FreeDeviceDescriptors(devices);
            Emit("ready", false, "requested camera serial was not found",
                 "\"requested_serial\":\"" + EscapeJson(serial) + "\"");
            return 4;
        }
    }

    const std::string camera_serial = devices[selected].serial_number;
    const std::string camera_name = devices[selected].camera_name;
    const std::string firmware = devices[selected].fw_version;
    auto camera = std::make_shared<ins_camera::Camera>(devices[selected].info);
    if (!camera->Open()) {
        discovery.FreeDeviceDescriptors(devices);
        Emit("ready", false, "failed to open Insta360 camera");
        return 5;
    }
    discovery.FreeDeviceDescriptors(devices);

    const std::time_t now = std::time(nullptr);
    const bool clock_synced = camera->SyncLocalTimeToCamera(
        static_cast<uint64_t>(now), static_cast<uint32_t>(0));
    Emit(
        "ready", true, "Insta360 camera opened",
        "\"serial\":\"" + EscapeJson(camera_serial) + "\","
        "\"camera_name\":\"" + EscapeJson(camera_name) + "\","
        "\"firmware\":\"" + EscapeJson(firmware) + "\","
        "\"resolution\":\"" + EscapeJson(resolution_name) + "\","
        "\"bitrate_mbps\":" + std::to_string(bitrate_mbps) + ","
        "\"clock_synced\":" + (clock_synced ? std::string("true") : std::string("false")) + ","
        "\"camera_media_time_ms\":" + std::to_string(camera->GetCameraMediaTime()) + ","
        "\"host_unix_time_ms\":" + std::to_string(UnixTimeMs()));

    bool recording = false;
    std::string command;
    while (std::getline(std::cin, command)) {
        if (command == "START") {
            if (recording || camera->CaptureCurrentStatus()) {
                recording = true;
                Emit("start", true, "camera was already recording",
                     "\"host_unix_time_ms\":" + std::to_string(UnixTimeMs()) + ","
                     "\"camera_media_time_ms\":" + std::to_string(camera->GetCameraMediaTime()));
                continue;
            }
            const int64_t request_unix_ms = UnixTimeMs();
            const int64_t request_steady_ns = SteadyTimeNs();
            bool ok = camera->SetVideoSubMode(ins_camera::SubVideoMode::VIDEO_NORMAL);
            ins_camera::RecordParams params;
            params.resolution = resolution;
            params.bitrate = bitrate_mbps * 1024 * 1024;
            ok = ok && camera->SetVideoCaptureParams(
                           params, ins_camera::CameraFunctionMode::FUNCTION_MODE_NORMAL_VIDEO);
            ok = ok && camera->StartRecording();
            recording = ok;
            Emit(
                "start", ok, ok ? "Insta360 recording started" : "failed to start Insta360 recording",
                "\"request_unix_time_ms\":" + std::to_string(request_unix_ms) + ","
                "\"complete_unix_time_ms\":" + std::to_string(UnixTimeMs()) + ","
                "\"request_steady_time_ns\":" + std::to_string(request_steady_ns) + ","
                "\"complete_steady_time_ns\":" + std::to_string(SteadyTimeNs()) + ","
                "\"camera_media_time_ms\":" + std::to_string(camera->GetCameraMediaTime()));
        } else if (command == "STOP") {
            if (!recording && !camera->CaptureCurrentStatus()) {
                Emit("stop", true, "camera was already stopped",
                     "\"host_unix_time_ms\":" + std::to_string(UnixTimeMs()));
                continue;
            }
            const int64_t request_unix_ms = UnixTimeMs();
            const int64_t request_steady_ns = SteadyTimeNs();
            const auto urls = camera->StopRecording();
            const bool ok = !urls.Empty();
            if (ok) recording = false;
            Emit(
                "stop", ok, ok ? "Insta360 recording stopped" : "failed to stop Insta360 recording",
                "\"request_unix_time_ms\":" + std::to_string(request_unix_ms) + ","
                "\"complete_unix_time_ms\":" + std::to_string(UnixTimeMs()) + ","
                "\"request_steady_time_ns\":" + std::to_string(request_steady_ns) + ","
                "\"complete_steady_time_ns\":" + std::to_string(SteadyTimeNs()) + ","
                "\"camera_media_time_ms\":" + std::to_string(camera->GetCameraMediaTime()) + ","
                "\"origin_urls\":" + StringArrayJson(urls.OriginUrls()) + ","
                "\"lrv_urls\":" + StringArrayJson(urls.LRVUrls()));
        } else if (command == "STATUS") {
            recording = camera->CaptureCurrentStatus();
            Emit("status", true, "camera status",
                 "\"connected\":" + std::string(camera->IsConnected() ? "true" : "false") + ","
                 "\"recording\":" + std::string(recording ? "true" : "false") + ","
                 "\"camera_media_time_ms\":" + std::to_string(camera->GetCameraMediaTime()));
        } else if (command == "FILES") {
            const auto files = camera->GetCameraFilesList();
            Emit("files", true, "camera file list", "\"files\":" + StringArrayJson(files));
        } else if (command == "QUIT") {
            if (recording || camera->CaptureCurrentStatus()) {
                camera->StopRecording();
            }
            Emit("quit", true, "CameraSDK worker closing");
            break;
        } else if (!command.empty()) {
            Emit("unknown", false, "unknown command: " + command);
        }
    }

    camera->Close();
    return 0;
}
