#pragma once
#include "capture.hpp"
#include <thread>
namespace echotrace {
class LinuxInputs {
public:
  LinuxInputs(CaptureConfig config, EventCallback event,
              std::function<void(bool)> pause);
  ~LinuxInputs();
  void stop();

private:
  void run();
  CaptureConfig config_;
  EventCallback event_;
  std::function<void(bool)> pause_;
  std::atomic<bool> running_{true};
  std::thread thread_;
};
} // namespace echotrace
