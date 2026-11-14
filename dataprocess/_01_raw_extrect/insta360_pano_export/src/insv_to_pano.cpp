#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
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
    std::string output;
    std::string model_dir;
    std::string log_dir;
    int width = 3840;
    int height = 1920;
    int64_t bitrate = 67LL * 1000LL * 1000LL;
    bool flowstate = false;
    bool direction_lock = false;
    bool stitch_fusion = false;
    bool cuda = true;
    std::string stitch_type = "optflow";
};

static void usage(const char* argv0) {
    std::cerr
        << "Usage:\n"
        << "  " << argv0 << " --input in.insv --output out.mp4 [options]\n\n"
        << "Options:\n"
        << "  --input PATH            One INSV input. Can be repeated for dual-file material.\n"
        << "  --inputs PATHS          Space/comma separated INSV list.\n"
        << "  --output PATH           Output equirectangular mp4.\n"
        << "  --width N               Output width, default 3840.\n"
        << "  --height N              Output height, default 1920.\n"
        << "  --bitrate RATE          Output bitrate, e.g. 67M or 67000000.\n"
        << "  --flowstate on|off      Stabilization. Default off to match InstaUMI SLAM export.\n"
        << "  --direction-lock on|off Direction lock. Default off.\n"
        << "  --stitch-type NAME      template|dynamic|optflow|aiflow. Default optflow.\n"
        << "  --stitch-fusion on|off  Chromatic calibration. Default off.\n"
        << "  --cuda on|off           CUDA stitching acceleration. Default on.\n"
        << "  --model-dir PATH        MediaSDK models directory, e.g. SDK_ROOT/models.\n"
        << "  --log-dir PATH          SDK log directory.\n";
}

static bool parse_bool(const std::string& value) {
    if (value == "1" || value == "true" || value == "on" || value == "yes") return true;
    if (value == "0" || value == "false" || value == "off" || value == "no") return false;
    throw std::runtime_error("invalid bool value: " + value);
}

static int64_t parse_bitrate(const std::string& value) {
    if (value.empty()) throw std::runtime_error("empty bitrate");
    char suffix = value.back();
    double scale = 1.0;
    std::string number = value;
    if (suffix == 'k' || suffix == 'K') {
        scale = 1000.0;
        number.pop_back();
    } else if (suffix == 'm' || suffix == 'M') {
        scale = 1000.0 * 1000.0;
        number.pop_back();
    }
    return static_cast<int64_t>(std::stod(number) * scale);
}

static std::vector<std::string> split_inputs(const std::string& text) {
    std::vector<std::string> out;
    std::string cur;
    for (char ch : text) {
        if (ch == ',' || ch == ';' || ch == ' ') {
            if (!cur.empty()) {
                out.push_back(cur);
                cur.clear();
            }
        } else {
            cur.push_back(ch);
        }
    }
    if (!cur.empty()) out.push_back(cur);
    return out;
}

static Options parse_args(int argc, char** argv) {
    Options opt;
    for (int i = 1; i < argc; ++i) {
        std::string key = argv[i];
        auto need_value = [&](const std::string& name) -> std::string {
            if (i + 1 >= argc) throw std::runtime_error("missing value for " + name);
            return argv[++i];
        };

        if (key == "--input" || key == "-i") {
            opt.inputs.push_back(need_value(key));
        } else if (key == "--inputs") {
            auto values = split_inputs(need_value(key));
            opt.inputs.insert(opt.inputs.end(), values.begin(), values.end());
        } else if (key == "--output" || key == "-o") {
            opt.output = need_value(key);
        } else if (key == "--width") {
            opt.width = std::stoi(need_value(key));
        } else if (key == "--height") {
            opt.height = std::stoi(need_value(key));
        } else if (key == "--bitrate") {
            opt.bitrate = parse_bitrate(need_value(key));
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
    if (opt.output.empty()) throw std::runtime_error("--output is required");
    if (opt.width <= 0 || opt.height <= 0 || opt.width != opt.height * 2) {
        throw std::runtime_error("output size must be positive and 2:1, e.g. 3840x1920");
    }
    for (const auto& input : opt.inputs) {
        if (!fs::exists(input)) throw std::runtime_error("input does not exist: " + input);
    }
    const auto output_parent = fs::path(opt.output).parent_path();
    if (!output_parent.empty()) {
        fs::create_directories(output_parent);
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

int main(int argc, char** argv) {
    try {
        Options opt = parse_args(argc, argv);

        ins::InitEnv();
        ins::SetLogLevel(ins::InsLogLevel::ERR);
        if (!opt.model_dir.empty()) {
            ins::SetModelFileRootDir(opt.model_dir);
        }
        if (!opt.log_dir.empty()) {
            fs::create_directories(opt.log_dir);
            ins::SetLogPath(opt.log_dir);
        }

        auto stitcher = std::make_shared<ins::VideoStitcher>();
        stitcher->SetInputPath(opt.inputs);
        stitcher->SetOutputPath(opt.output);
        stitcher->SetOutputSize(opt.width, opt.height);
        stitcher->EnableH265Encoder();
        stitcher->SetOutputBitRate(opt.bitrate);
        stitcher->SetSoftwareCodecUsage(false, false);
        stitcher->SetImageProcessingAccelType(ins::ImageProcessingAccel::kAuto);
        stitcher->EnableCuda(opt.cuda);
        stitcher->EnableFlowState(opt.flowstate);
        stitcher->EnableDirectionLock(opt.direction_lock);
        stitcher->SetStitchType(parse_stitch_type(opt.stitch_type));
        stitcher->EnableStitchFusion(opt.stitch_fusion);

        std::mutex mutex;
        std::condition_variable cv;
        std::atomic<bool> finished{false};
        std::atomic<bool> failed{false};
        std::string error_message;
        int last_progress = -1;

        stitcher->SetStitchProgressCallback([&](int progress, int error) {
            if (progress != last_progress) {
                std::cout << "\r[stitch] progress=" << progress << "% error=" << error << std::flush;
                last_progress = progress;
            }
            if (progress >= 100) {
                finished = true;
                cv.notify_one();
            }
        });

        stitcher->SetStitchStateCallback([&](int error, const char* errinfo) {
            if (error != 0) {
                failed = true;
                error_message = errinfo ? errinfo : "unknown MediaSDK error";
                cv.notify_one();
            }
        });

        std::cout << "[stitch] input_count=" << opt.inputs.size()
                  << " output=" << opt.output
                  << " size=" << opt.width << "x" << opt.height
                  << " bitrate=" << opt.bitrate
                  << " codec=h265"
                  << " cuda=" << (opt.cuda ? "on" : "off")
                  << " flowstate=" << (opt.flowstate ? "on" : "off")
                  << " stitch_type=" << opt.stitch_type
                  << std::endl;

        stitcher->StartStitch();
        {
            std::unique_lock<std::mutex> lock(mutex);
            cv.wait(lock, [&] { return finished.load() || failed.load(); });
        }
        std::cout << std::endl;

        if (failed) {
            std::cerr << "[error] stitch failed: " << error_message << std::endl;
            return 2;
        }
        if (!fs::exists(opt.output) || fs::file_size(opt.output) == 0) {
            std::cerr << "[error] output was not created: " << opt.output << std::endl;
            return 3;
        }
        std::cout << "[done] " << opt.output << std::endl;
        return 0;
    } catch (const std::exception& exc) {
        std::cerr << "[error] " << exc.what() << std::endl;
        usage(argv[0]);
        return 1;
    }
}
