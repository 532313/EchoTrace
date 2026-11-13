#include "echotrace/core.hpp"
#include "echotrace/screen_frame.h"
#include <cassert>
#include <cstring>
#include <sys/socket.h>
#include <unistd.h>

int main() {
  echotrace::Packet packet;
  packet.kind = 3; packet.samples = 0; packet.sequence = 7; packet.source_us = 123456789;
  packet.payload.resize(16 + ET_SCREEN_PIXELS);
  for (size_t i = 0; i < packet.payload.size(); ++i) packet.payload[i] = uint8_t(i);
  auto bytes = packet.encode();
  et_screen_frame frame;
  assert(et_screen_parse(bytes.data(), bytes.size(), &frame));
  assert(frame.sequence == 7 && frame.source_us == packet.source_us);
  assert(et_screen_matches(&frame, "000102030405060708090a0b0c0d0e0f"));
  assert(!et_screen_matches(&frame, "000102030405060708090a0b0c0d0e00"));
  assert(!et_screen_parse(bytes.data(), bytes.size() - 1, &frame));
  auto damaged = bytes; damaged[23] = 1;
  assert(!et_screen_parse(damaged.data(), damaged.size(), &frame));
  auto decoded = echotrace::Packet::decode(bytes);
  assert(decoded.payload == packet.payload && decoded.source_us == packet.source_us);
  char directory[] = "/tmp/echotrace-screen-XXXXXX";
  assert(mkdtemp(directory));
  std::string path = std::string(directory) + "/frames";
  int receiver = et_screen_receiver(path.c_str()); assert(receiver >= 0);
  assert(et_screen_receiver(path.c_str()) < 0); // A live display cannot be replaced.
  int sender = et_screen_sender(); assert(sender >= 0);
  assert(et_screen_send(sender, path.c_str(), bytes.data(), bytes.size()));
  std::vector<uint8_t> received(bytes.size());
  assert(recv(receiver, received.data(), received.size(), 0) == ssize_t(bytes.size()));
  assert(received == bytes);
  close(receiver); // Abnormal display exit leaves a stale path, which is recoverable.
  receiver = et_screen_receiver(path.c_str()); assert(receiver >= 0);
  close(receiver); close(sender); unlink(path.c_str()); rmdir(directory);
}
