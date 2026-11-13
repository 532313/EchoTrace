#include "recording.h"
#include <cassert>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <thread>
#include <vector>
int main(int argc, char **argv) {
  if (argc != 3) {
    std::cerr << "usage: echotrace-mux-test input-with-aud.h264 output-root\n";
    return 2;
  }
  setenv("IRC_RECORD_ROOT", argv[2], 1);
  setenv("IRC_RECORD_MIN_FREE_MB", "0", 1);
  setenv("IRC_RECORD_SEGMENT_SECONDS", "2", 1);
  std::ifstream stream(argv[1], std::ios::binary);
  std::vector<unsigned char> data((std::istreambuf_iterator<char>(stream)), {});
  if (data.empty())
    return 2;
  std::vector<size_t> boundaries;
  for (size_t i = 0; i + 5 < data.size(); ++i) {
    size_t prefix = 0;
    if (data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1)
      prefix = 3;
    else if (data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 0 &&
             data[i + 3] == 1)
      prefix = 4;
    if (prefix && (data[i + prefix] & 31) == 9) {
      boundaries.push_back(i);
      i += prefix;
    }
  }
  boundaries.push_back(data.size());
  if (boundaries.size() < 3)
    return 3;
  Recording recorder(640, 360);
  int segments = 0;
  recorder.setSegmentCallback(
      [&](const std::string &path, uint64_t begin, uint64_t end) {
        assert(end > begin);
        std::cout << path << " " << begin << " " << end << "\n";
        ++segments;
      });
  if (!recorder.start())
    return 4;
  for (size_t i = 0; i + 1 < boundaries.size(); ++i) {
    recorder.enqueue(data.data() + boundaries[i],
                     boundaries[i + 1] - boundaries[i], 1000000 + i * 40000,
                     false);
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  recorder.stop();
  return segments ? 0 : 5;
}
