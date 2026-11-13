#include "echotrace/core.hpp"
#include <cassert>
#include <fstream>
#include <iostream>
#include <unistd.h>
using namespace echotrace;
int main() {
  Packet packet;
  packet.sequence = 0x01020304;
  packet.source_us = 0x0102030405060708ULL;
  packet.payload = {0xf8, 0xff, 0xfe};
  const auto data = packet.encode();
  assert(data.size() == 27 && data[0] == 0 && data[1] == 3 && data[8] == 1 &&
         data[12] == 1 && data[19] == 8);
  const auto decoded = Packet::decode(data);
  assert(decoded.source_us == packet.source_us &&
         decoded.sequence == packet.sequence);
  auto malformed = data;
  malformed.push_back(0);
  bool rejected = false;
  try {
    Packet::decode(malformed);
  } catch (...) {
    rejected = true;
  }
  assert(rejected);
  auto root = std::filesystem::temp_directory_path() /
              ("echotrace-test-" + std::to_string(getpid()));
  std::filesystem::create_directories(root);
  {
    Journal journal(root, "unit-device", "unit-boot");
    auto first = journal.append("session-a", "mark", 100, "先保存接线");
    assert(first["event"]["seq"].asUInt64() == 0);
  }
  {
    Journal recovered(root, "unit-device", "unit-boot");
    auto second =
        recovered.append("session-a", "voice", 200, "因为接触不良更换排线");
    assert(second["event"]["seq"].asUInt64() == 1);
    assert(recovered.replay().size() == 2);
  }
  {
    std::ofstream file(root / "observations.jsonl", std::ios::app);
    file << "{\"type\":";
  }
  {
    Journal recovered(root, "unit-device", "unit-boot");
    recovered.append("session-a", "mark", 300, "断电尾部恢复");
    uint64_t cursor = 0;
    size_t count = 0;
    while (true) {
      auto batch = recovered.read_from(cursor, 1);
      if (batch.empty())
        break;
      assert(batch.size() == 1);
      ++count;
    }
    assert(count == 3);
  }
  {
    std::ofstream file(root / "hash.txt", std::ios::binary);
    file << "abc";
  }
  assert(sha256_file(root / "hash.txt") ==
         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  assert(decode_base64("AAH/", 3) == std::vector<uint8_t>({0, 1, 255}));
  assert(decode_base64("YQ==", 1) == std::vector<uint8_t>({'a'}));
  bool bad_base64 = false;
  try {
    decode_base64("YQ=/", 3);
  } catch (...) {
    bad_base64 = true;
  }
  assert(bad_base64);
  {
    WaveWriter writer;
    writer.open(root / "audio.part");
    int16_t samples[640]{};
    writer.write(samples, 640);
    writer.close();
  }
  assert(std::filesystem::file_size(root / "audio.wav") == 1324);
  {
    WaveWriter writer;
    writer.open(root / "deferred.part");
    int16_t samples[640]{};
    writer.write(samples, 640);
    writer.close(false);
    assert(std::filesystem::exists(root / "deferred.wav"));
    assert(!std::filesystem::exists(root / "deferred.part"));
    sync_closed_file(root / "deferred.wav");
    assert(std::filesystem::file_size(root / "deferred.wav") == 1324);
    bool rejected = false;
    try {
      sync_closed_file(root / "missing.wav");
    } catch (const std::runtime_error &) {
      rejected = true;
    }
    assert(rejected);
  }
  {
    const auto previous = std::filesystem::current_path();
    std::filesystem::current_path(root);
    WaveWriter writer;
    writer.open("relative.part");
    int16_t sample = 42;
    writer.write(&sample, 1);
    writer.close();
    assert(std::filesystem::file_size("relative.wav") == 46);
    std::filesystem::current_path(previous);
  }
  BoundedQueue<int> queue(1);
  assert(queue.push(1));
  assert(!queue.push(2));
  assert(queue.pop().value() == 1);
  assert(!queue.pop());
  std::filesystem::remove_all(root);
  std::cout
      << "protocol, durable replay, WAV and queue checks passed\n";
}
