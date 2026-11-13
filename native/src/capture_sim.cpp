#include "echotrace/capture.hpp"
#include <chrono>
#include <fstream>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

namespace fs = std::filesystem;
namespace echotrace {
namespace {
void run_ffmpeg(const std::vector<std::string> &args) {
  pid_t pid = fork();
  if (pid < 0)
    throw std::runtime_error("cannot fork simulation encoder");
  if (pid == 0) {
    std::vector<char *> argv;
    for (const auto &s : args)
      argv.push_back(const_cast<char *>(s.c_str()));
    argv.push_back(nullptr);
    execvp(argv[0], argv.data());
    _exit(127);
  }
  int status = 0;
  while (waitpid(pid, &status, 0) < 0) {
    if (errno != EINTR)
      throw std::runtime_error("waitpid failed");
  }
  if (!WIFEXITED(status) || WEXITSTATUS(status))
    throw std::runtime_error("simulation needs ffmpeg with libx264");
}
class SimulatedCapture final : public Capture {
public:
  SimulatedCapture(CaptureConfig config, MediaCallback media,
                   EventCallback event)
      : config_(std::move(config)), media_(std::move(media)),
        event_(std::move(event)) {}
  ~SimulatedCapture() { stop(); }
  void start(const std::string &session) override {
    stop();
    preview_stamp_ = 0;
    directory_ = config_.root / "sessions" / session;
    fs::create_directories(directory_);
    {
      std::lock_guard<std::mutex> lock(mutex_);
      error_.clear();
    }
    flushed_ = false;
    paused_ = false;
    running_ = true;
    thread_ = std::thread([this] {
      try {
        uint64_t begin = monotonic_us();
        while (running_) {
          std::this_thread::sleep_for(std::chrono::milliseconds(20));
          if (paused_) {
            if (!flushed_)
              flush(begin, monotonic_us());
            flushed_ = true;
            begin = monotonic_us();
            continue;
          }
          if (flushed_) {
            begin = monotonic_us();
            flushed_ = false;
          }
          const auto now = monotonic_us();
          if (!config_.preview_path.empty() && now - preview_stamp_.load() > 500000) {
            std::vector<uint8_t> pixels(160 * 90 * 2);
            for (size_t at = 0; at < pixels.size(); at += 2) {
              uint16_t color = uint16_t((((at / 2) % 160) / 20) << 12);
              pixels[at] = uint8_t(color);
              pixels[at + 1] = uint8_t(color >> 8);
            }
            write_preview(config_.preview_path, pixels);
            preview_stamp_ = now;
          }
          if (now - begin >= 10000000) {
            flush(begin, now);
            begin = monotonic_us();
          }
        }
        if (!paused_)
          flush(begin, monotonic_us());
      } catch (const std::exception &e) {
        {
          std::lock_guard<std::mutex> lock(mutex_);
          error_ = e.what();
        }
        running_ = false;
      }
    });
  }
  void pause(bool value) override { paused_ = value; }
  uint64_t preview_time_us() const override { return preview_stamp_.load(); }
  void stop() override {
    running_ = false;
    if (thread_.joinable())
      thread_.join();
  }
  std::string error() const override {
    std::lock_guard<std::mutex> lock(mutex_);
    return error_;
  }

private:
  void flush(uint64_t begin, uint64_t end) {
    const auto duration = double(end - begin) / 1000000;
    if (duration < .12)
      return;
    if (fs::space(directory_).available < config_.reserve_bytes)
      throw std::runtime_error("storage reserve reached");
    const auto video =
        directory_ / ("simulated-video-" + std::to_string(begin) + ".mp4");
    run_ffmpeg({"ffmpeg",       "-nostdin",
                "-hide_banner", "-loglevel",
                "error",        "-y",
                "-f",           "lavfi",
                "-i",           "testsrc2=size=640x360:rate=25",
                "-t",           std::to_string(duration),
                "-c:v",         "libx264",
                "-pix_fmt",     "yuv420p",
                "-g",           "25",
                "-movflags",    "+frag_keyframe+empty_moov",
                video.string()});
    const auto audio =
        directory_ / ("simulated-audio-" + std::to_string(begin) + ".part");
    WaveWriter wav;
    wav.open(audio);
    std::vector<int16_t> silence(640, 0);
    const uint64_t frames = uint64_t(duration * 16000) / 640;
    for (uint64_t i = 0; i < frames; ++i)
      wav.write(silence.data(), silence.size());
    wav.close();
    auto final = audio;
    final.replace_extension(".wav");
    media_(video, "video", begin, end, false);
    media_(final, "audio", begin, begin + frames * 40000, false);
    Json::Value payload;
    payload["synthetic"] = true;
    event_("scene", begin + 40000, "模拟相机片段，不能用于真实操作评测。",
           payload);
  }
  CaptureConfig config_;
  std::atomic<uint64_t> preview_stamp_{0};
  MediaCallback media_;
  EventCallback event_;
  std::thread thread_;
  std::atomic<bool> running_{false}, paused_{false};
  bool flushed_ = false;
  fs::path directory_;
  std::string error_;
  mutable std::mutex mutex_;
};
} // namespace
#ifdef ECHO_RV1106
std::unique_ptr<Capture> make_rv_capture(const CaptureConfig &, MediaCallback,
                                         EventCallback);
#endif
std::unique_ptr<Capture> make_capture(const CaptureConfig &config,
                                      MediaCallback media, EventCallback event,
                                      bool simulated) {
  if (simulated)
    return std::make_unique<SimulatedCapture>(config, std::move(media),
                                              std::move(event));
#ifdef ECHO_RV1106
  return make_rv_capture(config, std::move(media), std::move(event));
#else
  throw std::runtime_error("this binary has no hardware backend; explicitly "
                           "use --simulated for synthetic data");
#endif
}
} // namespace echotrace
