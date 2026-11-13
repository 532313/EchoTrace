#include "echotrace/inputs.hpp"
#include <cmath>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <linux/input.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <unistd.h>

namespace echotrace {
LinuxInputs::LinuxInputs(CaptureConfig config, EventCallback event,
                         std::function<void(bool)> pause)
    : config_(std::move(config)), event_(std::move(event)),
      pause_(std::move(pause)), thread_([this] { run(); }) {}
LinuxInputs::~LinuxInputs() { stop(); }
void LinuxInputs::stop() {
  running_ = false;
  if (thread_.joinable())
    thread_.join();
}
void LinuxInputs::run() {
  int keys = -1, tray = -1, pause = -1;
  TrayDetector detector;
  uint64_t last_sample = 0, last_fault = 0, last_key = 0;
  bool fault = false;
  double offset = 0, counts_per_gram = 0;
  auto open_input = [](const std::string &path) {
    if (path.empty())
      return -1;
    const int fd = open(path.c_str(), O_RDONLY | O_NONBLOCK);
    if (fd >= 0) {
      int clock = CLOCK_MONOTONIC;
      ioctl(fd, EVIOCSCLOCKID, &clock);
    }
    return fd;
  };
  keys = open_input(config_.input_device);
  pause = open_input(config_.pause_device);
  if (!config_.iio_device.empty()) {
    std::ifstream stream(config_.calibration);
    std::string text((std::istreambuf_iterator<char>(stream)), {});
    try {
      auto calibration = parse_json(text);
      offset = calibration["offset_counts"].asDouble();
      counts_per_gram = calibration["counts_per_gram"].asDouble();
      if (!std::isfinite(counts_per_gram) || std::abs(counts_per_gram) < 1e-9)
        throw std::runtime_error("invalid counts_per_gram");
      tray = open(config_.iio_device.c_str(), O_RDONLY | O_NONBLOCK);
    } catch (const std::exception &e) {
      event_("sensor_fault", monotonic_us(),
             std::string("托盘未校准：") + e.what(), Json::objectValue);
    }
  }
  while (running_) {
    pollfd descriptors[3] = {
        {keys, POLLIN, 0}, {tray, POLLIN, 0}, {pause, POLLIN, 0}};
    poll(descriptors, 3, 200);
    const auto now = monotonic_us();
    for (int slot : {0, 2})
      if (descriptors[slot].revents & POLLIN) {
        input_event value{};
        while (read(descriptors[slot].fd, &value, sizeof(value)) ==
               sizeof(value)) {
          if (slot == 0 && value.type == EV_KEY && value.code == KEY_F13 &&
              value.value == 1 && now - last_key > 200000) {
            last_key = now;
            const auto stamp =
                uint64_t(value.time.tv_sec) * 1000000 + value.time.tv_usec;
            event_("pedal", stamp, "", Json::objectValue);
          }
          if (slot == 2 && value.type == EV_SW &&
              value.code == SW_MUTE_DEVICE) {
            const bool paused = value.value != 0;
            pause_(paused);
            Json::Value state;
            state["state"] = paused ? "paused" : "recording";
            event_("recording_state", now,
                   paused ? "用户暂停记录" : "用户继续记录", state);
          }
        }
      }
    if (tray >= 0 && (descriptors[1].revents & POLLIN)) {
      alignas(8) unsigned char buffer[16]{};
      const auto n = read(tray, buffer, sizeof(buffer));
      if (n == 16) {
        uint32_t raw = 0;
        int64_t stamp_ns = 0;
        std::memcpy(&raw, buffer, 4);
        std::memcpy(&stamp_ns, buffer + 8, 8);
        if (raw > 0xffffff || stamp_ns <= 0 ||
            uint64_t(stamp_ns / 1000) > now + 1000000) {
          detector.fault();
          fault = true;
          continue;
        }
        last_sample = now;
        if (fault) {
          detector.fault();
          fault = false;
        }
        if (auto change =
                detector.sample((double(raw) - offset) / counts_per_gram,
                                uint64_t(stamp_ns / 1000))) {
          Json::Value payload;
          payload["weight_g"] = change->weight_g;
          payload["delta_g"] = change->delta_g;
          payload["threshold_g"] = change->threshold_g;
          event_("tray", change->source_us, "托盘稳定重量发生变化", payload);
        }
      } else if (n > 0) {
        detector.fault();
        fault = true;
      }
    }
    if (tray >= 0 && (!last_sample || now - last_sample > 2000000) &&
        now - last_fault > 10000000) {
      last_fault = now;
      detector.fault();
      fault = true;
      event_("sensor_fault", now, "托盘采样超时；本段不生成重量事件",
             Json::objectValue);
    }
  }
  for (int fd : {keys, tray, pause})
    if (fd >= 0)
      close(fd);
}
} // namespace echotrace
