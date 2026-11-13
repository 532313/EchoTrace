#pragma once
#include "core.hpp"
#include <atomic>
#include <functional>
#include <memory>

namespace echotrace {
using MediaCallback =
    std::function<void(const std::filesystem::path &, const std::string &,
                       uint64_t, uint64_t, bool)>;
using EventCallback = std::function<void(const std::string &, uint64_t,
                                         const std::string &, Json::Value)>;
struct CaptureConfig {
  std::filesystem::path root;
  std::filesystem::path preview_path;
  unsigned capture_channels = 2, microphone_channel = 0;
  unsigned capture_latency_us = 500000;
  std::string alsa = "default";
  std::string iq = "/etc/iqfiles";
  uint64_t reserve_bytes = 256ULL * 1024 * 1024;
};
class Capture {
public:
  virtual ~Capture() = default;
  virtual void start(const std::string &session) = 0;
  virtual void pause(bool paused) = 0;
  virtual void stop() = 0;
  virtual std::string error() const = 0;
  virtual void play(const std::filesystem::path &, double) {
    throw std::runtime_error("playback backend unavailable");
  }
  virtual void stop_playback() {}
  virtual bool playback_active() const { return false; }
  virtual uint64_t preview_time_us() const { return 0; }
};
std::unique_ptr<Capture> make_capture(const CaptureConfig &, MediaCallback,
                                      EventCallback, bool simulated);
} // namespace echotrace
