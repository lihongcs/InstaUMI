#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#if __has_include(<ins_stitcher.h>)
#include <ins_stitcher.h>
#elif __has_include(<ins_video_stitcher.h>)
#include <ins_video_stitcher.h>
#elif __has_include(<ins_video_stitcher.hpp>)
#include <ins_video_stitcher.hpp>
#elif __has_include(<video_stitcher.h>)
#include <video_stitcher.h>
#elif __has_include(<ins_media_sdk.h>)
#include <ins_media_sdk.h>
#else
#error "Cannot find Insta360 MediaSDK video stitcher header. Check INSTA360_MEDIASDK_ROOT/include."
#endif

namespace fs = std::filesystem;

struct Options {
    std::vector<std::string> inputs;
    std::string output_dir;
    std::string model_dir;
    std::string log_dir;
    std::set<uint64_t> frames;
    int width = 3840;
    int height = 1920;
    bool flowstate = false;
    bool direction_lock = false;
    bool stitch_fusion = false;
    bool cuda = true;
    int timeout_sec = 0;
    std::string stitch_type = "optflow";
};

static void usage(const char* argv0) {
    std::cerr
        << "Usage:\n"
        << "  " << argv0
        << " --input chapter.insv --output-dir DIR --frames '0,3,10-20[:2]' [options]\n\n"
        << "The frame indices are zero-based within the ordered MediaSDK input timeline.\n"
        << "This tool asks MediaSDK for lossless PNG files and never creates an intermediate video.\n\n"
        << "Options:\n"
        << "  --input PATH            INSV input; repeat to preserve the requested order.\n"
        << "  --inputs PATHS          Comma/semicolon/space separated INSV paths.\n"
        << "  --output-dir DIR        Empty directory where MediaSDK writes PNG files.\n"
        << "  --frame N               Export one frame; may be repeated.\n"
        << "  --frames SPEC           List/ranges, e.g. 0,3,10-20,30-40:2. Ranges are inclusive.\n"
        << "  --frames-file PATH      Read frame specifications from a text file (# comments allowed).\n"
        << "  --width N               Panorama width, default 3840.\n"
        << "  --height N              Panorama height, default 1920.\n"
        << "  --flowstate on|off      Stabilization, default off.\n"
        << "  --direction-lock on|off Direction lock, default off.\n"
        << "  --stitch-type NAME      template|dynamic|optflow|aiflow; default optflow.\n"
        << "  --stitch-fusion on|off  Chromatic calibration, default off.\n"
        << "  --cuda on|off           MediaSDK CUDA acceleration, default on.\n"
        << "  --timeout-sec N         Cancel after N seconds; zero means no timeout.\n"
        << "  --model-dir PATH        MediaSDK model directory.\n"
        << "  --log-dir PATH          MediaSDK log directory.\n";
}

static bool parse_bool(const std::string& value) {
    if (value == "1" || value == "true" || value == "on" || value == "yes") return true;
    if (value == "0" || value == "false" || value == "off" || value == "no") return false;
    throw std::runtime_error("invalid boolean value: " + value);
}

static uint64_t parse_uint64(const std::string& text, const std::string& context) {
    if (text.empty() || text.front() == '-') {
        throw std::runtime_error("invalid non-negative integer for " + context + ": " + text);
    }
    std::size_t consumed = 0;
    const auto value = std::stoull(text, &consumed, 10);
    if (consumed != text.size()) {
        throw std::runtime_error("invalid non-negative integer for " + context + ": " + text);
    }
    return value;
}

static std::vector<std::string> split_tokens(const std::string& text) {
    std::vector<std::string> out;
    std::string current;
    for (char ch : text) {
        if (ch == ',' || ch == ';' || std::isspace(static_cast<unsigned char>(ch))) {
            if (!current.empty()) {
                out.push_back(current);
                current.clear();
            }
        } else {
            current.push_back(ch);
        }
    }
    if (!current.empty()) out.push_back(current);
    return out;
}

static void add_frame_token(const std::string& raw_token, std::set<uint64_t>& frames) {
    if (raw_token.empty()) return;
    std::string token = raw_token;
    uint64_t step = 1;
    const auto colon = token.find(':');
    if (colon != std::string::npos) {
        if (token.find(':', colon + 1) != std::string::npos) {
            throw std::runtime_error("invalid frame range: " + raw_token);
        }
        step = parse_uint64(token.substr(colon + 1), "range step");
        if (step == 0) throw std::runtime_error("frame range step must be positive: " + raw_token);
        token.resize(colon);
    }
    const auto dash = token.find('-');
    if (dash == std::string::npos) {
        if (colon != std::string::npos) {
            throw std::runtime_error("a step requires an inclusive START-END range: " + raw_token);
        }
        frames.insert(parse_uint64(token, "frame"));
        return;
    }
    if (token.find('-', dash + 1) != std::string::npos) {
        throw std::runtime_error("invalid frame range: " + raw_token);
    }
    const uint64_t first = parse_uint64(token.substr(0, dash), "range start");
    const uint64_t last = parse_uint64(token.substr(dash + 1), "range end");
    if (last < first) throw std::runtime_error("descending frame range is not allowed: " + raw_token);
    for (uint64_t value = first;;) {
        frames.insert(value);
        if (last - value < step) break;
        value += step;
    }
}

static void add_frame_spec(const std::string& spec, std::set<uint64_t>& frames) {
    for (const auto& token : split_tokens(spec)) add_frame_token(token, frames);
}

static void add_frame_file(const fs::path& path, std::set<uint64_t>& frames) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open frames file: " + path.string());
    std::string line;
    while (std::getline(input, line)) {
        const auto comment = line.find('#');
        if (comment != std::string::npos) line.resize(comment);
        add_frame_spec(line, frames);
    }
}

static Options parse_args(int argc, char** argv) {
    Options opt;
    for (int i = 1; i < argc; ++i) {
        const std::string key = argv[i];
        auto need_value = [&](const std::string& name) -> std::string {
            if (i + 1 >= argc) throw std::runtime_error("missing value for " + name);
            return argv[++i];
        };
        if (key == "--input" || key == "-i") {
            opt.inputs.push_back(need_value(key));
        } else if (key == "--inputs") {
            const auto values = split_tokens(need_value(key));
            opt.inputs.insert(opt.inputs.end(), values.begin(), values.end());
        } else if (key == "--output-dir" || key == "-o") {
            opt.output_dir = need_value(key);
        } else if (key == "--frame") {
            add_frame_token(need_value(key), opt.frames);
        } else if (key == "--frames") {
            add_frame_spec(need_value(key), opt.frames);
        } else if (key == "--frames-file") {
            add_frame_file(need_value(key), opt.frames);
        } else if (key == "--width") {
            opt.width = std::stoi(need_value(key));
        } else if (key == "--height") {
            opt.height = std::stoi(need_value(key));
        } else if (key == "--flowstate") {
            opt.flowstate = parse_bool(need_value(key));
        } else if (key == "--direction-lock") {
            opt.direction_lock = parse_bool(need_value(key));
        } else if (key == "--stitch-fusion") {
            opt.stitch_fusion = parse_bool(need_value(key));
        } else if (key == "--cuda") {
            opt.cuda = parse_bool(need_value(key));
        } else if (key == "--stitch-type") {
            opt.stitch_type = need_value(key);
        } else if (key == "--timeout-sec") {
            opt.timeout_sec = std::stoi(need_value(key));
        } else if (key == "--model-dir") {
            opt.model_dir = need_value(key);
        } else if (key == "--log-dir") {
            opt.log_dir = need_value(key);
        } else if (key == "--help" || key == "-h") {
            usage(argv[0]);
            std::exit(0);
        } else {
            throw std::runtime_error("unknown argument: " + key);
        }
    }
    if (opt.inputs.empty()) throw std::runtime_error("at least one --input is required");
    if (opt.output_dir.empty()) throw std::runtime_error("--output-dir is required");
    if (opt.frames.empty()) throw std::runtime_error("at least one --frame/--frames is required");
    if (opt.width <= 0 || opt.height <= 0 || opt.width != opt.height * 2) {
        throw std::runtime_error("output size must be positive and 2:1, e.g. 3840x1920");
    }
    if (opt.timeout_sec < 0) throw std::runtime_error("--timeout-sec must be non-negative");
    if (opt.direction_lock && !opt.flowstate) {
        throw std::runtime_error("--direction-lock on requires --flowstate on");
    }
    for (const auto& input : opt.inputs) {
        if (!fs::is_regular_file(input)) throw std::runtime_error("input does not exist: " + input);
    }
    const fs::path output_dir(opt.output_dir);
    if (fs::exists(output_dir) && !fs::is_directory(output_dir)) {
        throw std::runtime_error("output path is not a directory: " + opt.output_dir);
    }
    fs::create_directories(output_dir);
    if (fs::directory_iterator(output_dir) != fs::directory_iterator()) {
        throw std::runtime_error("output directory must be empty: " + opt.output_dir);
    }
    return opt;
}

static ins::STITCH_TYPE parse_stitch_type(const std::string& value) {
    if (value == "template") return ins::STITCH_TYPE::TEMPLATE;
    if (value == "dynamic" || value == "dynamicstitch") return ins::STITCH_TYPE::DYNAMICSTITCH;
    if (value == "optflow" || value == "opticalflow") return ins::STITCH_TYPE::OPTFLOW;
    if (value == "aiflow" || value == "ai") return ins::STITCH_TYPE::AIFLOW;
    throw std::runtime_error("invalid stitch type: " + value);
}

static bool is_png_path(const fs::path& path) {
    std::string extension = path.extension().string();
    std::transform(extension.begin(), extension.end(), extension.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return extension == ".png";
}

int main(int argc, char** argv) {
    try {
        const Options opt = parse_args(argc, argv);
        const std::vector<uint64_t> frames(opt.frames.begin(), opt.frames.end());

        ins::InitEnv();
        ins::SetLogLevel(ins::InsLogLevel::ERR);
        if (!opt.model_dir.empty()) ins::SetModelFileRootDir(opt.model_dir);
        if (!opt.log_dir.empty()) {
            fs::create_directories(opt.log_dir);
            ins::SetLogPath(opt.log_dir);
        }

        auto stitcher = std::make_shared<ins::VideoStitcher>();
        stitcher->SetInputPath(opt.inputs);
        stitcher->SetExportFrameSequence(frames);
        stitcher->SetImageSequenceInfo(opt.output_dir, ins::IMAGE_TYPE::PNG);
        stitcher->SetOutputSize(opt.width, opt.height);
        stitcher->SetStitchType(parse_stitch_type(opt.stitch_type));
        stitcher->EnableCuda(opt.cuda);
        stitcher->EnableFlowState(opt.flowstate);
        stitcher->EnableDirectionLock(opt.direction_lock);
        stitcher->EnableStitchFusion(opt.stitch_fusion);

        std::mutex mutex;
        std::condition_variable condition;
        bool finished = false;
        bool failed = false;
        std::string error_message;
        std::atomic<int> last_progress{-1};

        auto mark_failed = [&](const std::string& message) {
            {
                std::lock_guard<std::mutex> lock(mutex);
                failed = true;
                error_message = message;
            }
            condition.notify_all();
        };

        stitcher->SetStitchProgressCallback([&](int progress, int error) {
            if (progress != last_progress.exchange(progress)) {
                std::cout << "\r[stitch-png] progress=" << progress << "% error=" << error << std::flush;
            }
            if (error != 0) {
                mark_failed("MediaSDK progress callback error=" + std::to_string(error));
                return;
            }
            if (progress >= 100) {
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    finished = true;
                }
                condition.notify_all();
            }
        });
        stitcher->SetStitchStateCallback([&](int error, const char* info) {
            if (error != 0) {
                mark_failed(info ? info : ("MediaSDK state callback error=" + std::to_string(error)));
            }
        });

        std::cout << "[stitch-png] input_count=" << opt.inputs.size()
                  << " requested_frames=" << frames.size()
                  << " first=" << frames.front()
                  << " last=" << frames.back()
                  << " output_dir=" << opt.output_dir
                  << " size=" << opt.width << "x" << opt.height
                  << " stitch_type=" << opt.stitch_type
                  << " cuda=" << (opt.cuda ? "on" : "off")
                  << std::endl;

        stitcher->StartStitch();
        std::unique_lock<std::mutex> lock(mutex);
        if (opt.timeout_sec > 0) {
            const bool signalled = condition.wait_for(
                lock,
                std::chrono::seconds(opt.timeout_sec),
                [&] { return finished || failed; });
            if (!signalled) {
                lock.unlock();
                stitcher->CancelStitch();
                throw std::runtime_error("MediaSDK PNG export timed out after " +
                                         std::to_string(opt.timeout_sec) + " seconds");
            }
        } else {
            condition.wait(lock, [&] { return finished || failed; });
        }
        const bool did_fail = failed;
        const std::string failure = error_message;
        lock.unlock();
        std::cout << std::endl;

        if (did_fail) {
            std::cerr << "[error] PNG stitch failed: " << failure << std::endl;
            return 2;
        }

        // Destroy the stitcher before inspecting the directory so all SDK file
        // handles and PNG encoder buffers have been closed.
        stitcher.reset();
        std::size_t png_count = 0;
        for (const auto& entry : fs::directory_iterator(opt.output_dir)) {
            if (entry.is_regular_file() && is_png_path(entry.path())) ++png_count;
        }
        if (png_count != frames.size()) {
            std::cerr << "[error] MediaSDK PNG count mismatch: got=" << png_count
                      << " expected=" << frames.size() << std::endl;
            return 3;
        }
        std::cout << "[done] MediaSDK wrote " << png_count << " PNG frames to " << opt.output_dir
                  << std::endl;
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "[error] " << error.what() << std::endl;
        usage(argv[0]);
        return 1;
    }
}
