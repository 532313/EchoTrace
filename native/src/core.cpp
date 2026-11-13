#include "echotrace/core.hpp"
#include "picosha2.h"
#include <algorithm>
#include <array>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <random>
#include <sstream>
#include <stdexcept>
#include <unistd.h>

namespace fs = std::filesystem;
namespace echotrace {
namespace {
void write_all(int fd, const void *data, size_t length) {
  auto *p = static_cast<const char *>(data);
  while (length) {
    const auto n = ::write(fd, p, length);
    if (n < 0 && errno == EINTR)
      continue;
    if (n <= 0)
      throw std::runtime_error("write failed");
    p += n;
    length -= size_t(n);
  }
}
void put_be(std::vector<uint8_t> &data, uint64_t value, unsigned count) {
  for (unsigned i = count; i > 0; --i)
    data.push_back(uint8_t(value >> ((i - 1) * 8)));
}
uint64_t get_be(const std::vector<uint8_t> &bytes, size_t at, unsigned count) {
  uint64_t v = 0;
  for (unsigned i = 0; i < count; ++i)
    v = (v << 8) | bytes.at(at + i);
  return v;
}
void le32(uint8_t *p, uint32_t v) {
  for (int i = 0; i < 4; ++i)
    p[i] = uint8_t(v >> (8 * i));
}
void le16(uint8_t *p, uint16_t v) {
  p[0] = uint8_t(v);
  p[1] = uint8_t(v >> 8);
}
} // namespace
uint64_t monotonic_us() {
  timespec stamp{};
  if (clock_gettime(CLOCK_MONOTONIC, &stamp))
    throw std::runtime_error("monotonic clock unavailable");
  return uint64_t(stamp.tv_sec) * 1000000 + stamp.tv_nsec / 1000;
}
void write_preview(const fs::path &path, const std::vector<uint8_t> &pixels) {
  if (pixels.size() != 160 * 90 * 2)
    throw std::runtime_error("invalid RGB565 preview dimensions");
  auto part = path;
  part += ".part";
  std::ofstream output(part, std::ios::binary | std::ios::trunc);
  output.write(reinterpret_cast<const char *>(pixels.data()), pixels.size());
  output.close();
  if (!output)
    throw std::runtime_error("preview unavailable");
  fs::rename(part, path);
}
std::string boot_id() {
  std::ifstream source("/proc/sys/kernel/random/boot_id");
  std::string result;
  if (source >> result)
    return result;
  std::random_device random;
  std::ostringstream out;
  out << std::hex << random() << random() << random() << random();
  return out.str();
}
std::string json(const Json::Value &value) {
  Json::StreamWriterBuilder builder;
  builder["indentation"] = "";
  return Json::writeString(builder, value);
}
Json::Value parse_json(const std::string &text) {
  Json::CharReaderBuilder builder;
  builder["collectComments"] = false;
  builder["failIfExtra"] = true;
  std::unique_ptr<Json::CharReader> reader(builder.newCharReader());
  Json::Value result;
  std::string errors;
  if (!reader->parse(text.data(), text.data() + text.size(), &result, &errors))
    throw std::runtime_error("invalid JSON: " + errors);
  return result;
}
void atomic_json(const fs::path &path, const Json::Value &value) {
  fs::create_directories(path.parent_path());
  auto temporary = path;
  temporary += ".part";
  const auto text = json(value) + "\n";
  int fd = ::open(temporary.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
  if (fd < 0)
    throw std::runtime_error("cannot open state file");
  try {
    write_all(fd, text.data(), text.size());
    if (fsync(fd))
      throw std::runtime_error("fsync state failed");
    ::close(fd);
    fd = -1;
    fs::rename(temporary, path);
    int dir = ::open(path.parent_path().c_str(), O_RDONLY);
    if (dir >= 0) {
      fsync(dir);
      ::close(dir);
    }
  } catch (...) {
    if (fd >= 0)
      ::close(fd);
    throw;
  }
}
std::string sha256_file(const fs::path &path) {
  std::ifstream stream(path, std::ios::binary);
  if (!stream)
    throw std::runtime_error("cannot hash media");
  picosha2::hash256_one_by_one hash;
  std::array<char, 65536> buffer{};
  while (stream) {
    stream.read(buffer.data(), buffer.size());
    hash.process(buffer.data(), buffer.data() + stream.gcount());
  }
  if (!stream.eof())
    throw std::runtime_error("media read failed");
  hash.finish();
  return picosha2::get_hash_hex_string(hash);
}
void sync_closed_file(const fs::path &path) {
  const int file = ::open(path.c_str(), O_RDONLY);
  if (file < 0)
    throw std::runtime_error("cannot open closed media for sync");
  const int result = fsync(file);
  ::close(file);
  if (result)
    throw std::runtime_error("closed media sync failed");
  const auto parent = path.has_parent_path() ? path.parent_path() : fs::path(".");
  const int directory = ::open(parent.c_str(), O_RDONLY);
  if (directory < 0)
    throw std::runtime_error("cannot open media directory for sync");
  const int committed = fsync(directory);
  ::close(directory);
  if (committed)
    throw std::runtime_error("media directory sync failed");
}

std::vector<uint8_t> decode_base64(const std::string &text, size_t maximum) {
  static const std::string alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  if (text.empty() || text.size() % 4 || text.size() > (maximum + 2) / 3 * 4)
    throw std::runtime_error("invalid thumbnail base64 size");
  size_t padding = 0;
  if (text.back() == '=')
    ++padding;
  if (text.size() > 1 && text[text.size() - 2] == '=')
    ++padding;
  const size_t expected = text.size() / 4 * 3 - padding;
  if (expected > maximum)
    throw std::runtime_error("thumbnail exceeds its bound");
  std::vector<uint8_t> output;
  output.reserve(expected);
  uint32_t value = 0;
  int bits = 0;
  for (size_t i = 0; i < text.size() - padding; ++i) {
    auto found = alphabet.find(text[i]);
    if (found == std::string::npos)
      throw std::runtime_error("invalid thumbnail base64 character");
    value = (value << 6) | uint32_t(found);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      output.push_back(uint8_t(value >> bits));
    }
  }
  if (output.size() != expected || (bits && (value & ((1u << bits) - 1))))
    throw std::runtime_error("noncanonical thumbnail base64");
  return output;
}

std::vector<uint8_t> Packet::encode() const {
  if ((kind != 1 && kind != 2) || payload.empty() || payload.size() > 65536)
    throw std::invalid_argument("invalid packet");
  if (kind == 1 && samples != 640)
    throw std::invalid_argument("audio frame must contain 640 samples");
  std::vector<uint8_t> bytes;
  bytes.reserve(24 + payload.size());
  put_be(bytes, 3, 2);
  put_be(bytes, kind, 2);
  put_be(bytes, payload.size(), 4);
  put_be(bytes, sequence, 4);
  put_be(bytes, source_us, 8);
  put_be(bytes, samples, 4);
  bytes.insert(bytes.end(), payload.begin(), payload.end());
  return bytes;
}
Packet Packet::decode(const std::vector<uint8_t> &bytes) {
  if (bytes.size() < 24 || get_be(bytes, 0, 2) != 3)
    throw std::invalid_argument("bad protocol header");
  Packet p;
  p.kind = uint16_t(get_be(bytes, 2, 2));
  p.sequence = uint32_t(get_be(bytes, 8, 4));
  p.source_us = get_be(bytes, 12, 8);
  p.samples = uint32_t(get_be(bytes, 20, 4));
  const auto n = get_be(bytes, 4, 4);
  if ((p.kind != 1 && p.kind != 2) || !n || n > 65536 ||
      bytes.size() != 24 + n || (p.kind == 1 && p.samples != 640))
    throw std::invalid_argument("bad packet length or type");
  p.payload.assign(bytes.begin() + 24, bytes.end());
  return p;
}

Journal::Journal(fs::path root, std::string device, std::string boot_value)
    : device_id(std::move(device)), boot(std::move(boot_value)),
      root_(std::move(root)) {
  fs::create_directories(root_);
  const auto path = root_ / "observations.jsonl";
  if (fs::exists(path)) {
    const auto length = fs::file_size(path);
    uint64_t offset = 0;
    std::string line;
    std::ifstream stream(path, std::ios::binary);
    while (std::getline(stream, line)) {
      try {
        if (!line.empty())
          parse_json(line);
      } catch (...) {
        if (!stream.eof())
          throw std::runtime_error(
              "event journal contains corruption before its tail");
        stream.close();
        fs::resize_file(path, offset);
        break;
      }
      offset += line.size() + 1;
      if (offset > length) {
        stream.close();
        int fd = ::open(path.c_str(), O_WRONLY | O_APPEND);
        if (fd < 0)
          throw std::runtime_error("journal recovery failed");
        write_all(fd, "\n", 1);
        fsync(fd);
        ::close(fd);
        break;
      }
    }
  }
  visit([this](const Json::Value &item) {
    if (item["type"] == "event" && item["event"]["boot_id"].asString() == boot)
      sequence_ = std::max(sequence_, item["event"]["seq"].asUInt64() + 1);
  });
}
void Journal::persist(const Json::Value &value) {
  const auto data = json(value) + "\n";
  std::lock_guard<std::mutex> lock(mutex_);
  int fd = ::open((root_ / "observations.jsonl").c_str(),
                  O_CREAT | O_WRONLY | O_APPEND, 0600);
  if (fd < 0)
    throw std::runtime_error("cannot append event journal");
  try {
    write_all(fd, data.data(), data.size());
    if (fsync(fd))
      throw std::runtime_error("journal sync failed");
    ::close(fd);
  } catch (...) {
    ::close(fd);
    throw;
  }
}
Json::Value Journal::append(const std::string &session, const std::string &kind,
                            uint64_t stamp, const std::string &note,
                            Json::Value payload) {
  Json::Value event;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    event["seq"] = Json::UInt64(sequence_++);
  }
  event["id"] = boot + "-" + std::to_string(event["seq"].asUInt64());
  event["session_id"] = session;
  event["device_id"] = device_id;
  event["boot_id"] = boot;
  event["source_time_us"] = Json::UInt64(stamp);
  event["kind"] = kind;
  event["note"] = note;
  event["payload"] = std::move(payload);
  Json::Value message;
  message["type"] = "event";
  message["event"] = event;
  persist(message);
  return message;
}
std::vector<Json::Value> Journal::read_from(uint64_t &cursor,
                                            size_t limit) const {
  std::lock_guard<std::mutex> lock(mutex_);
  std::ifstream stream(root_ / "observations.jsonl", std::ios::binary);
  stream.seekg(std::streamoff(cursor));
  std::vector<Json::Value> result;
  std::string line;
  while (result.size() < limit && std::getline(stream, line)) {
    if (stream.eof())
      break; // An incomplete last line is retried after the writer commits it.
    cursor += line.size() + 1;
    if (!line.empty())
      result.push_back(parse_json(line));
  }
  return result;
}
void Journal::visit(const std::function<void(const Json::Value &)> &fn) const {
  uint64_t cursor = 0;
  while (true) {
    auto batch = read_from(cursor);
    if (batch.empty())
      break;
    for (const auto &item : batch)
      fn(item);
  }
}
std::vector<Json::Value> Journal::replay() const {
  std::vector<Json::Value> result;
  visit([&](const Json::Value &item) { result.push_back(item); });
  return result;
}

void WaveWriter::open(const fs::path &path) {
  close();
  path_ = path;
  samples_ = 0;
  fd_ = ::open(path.c_str(), O_CREAT | O_WRONLY | O_TRUNC, 0600);
  if (fd_ < 0)
    throw std::runtime_error("cannot open audio segment");
  std::array<uint8_t, 44> header{};
  write_all(fd_, header.data(), header.size());
}
void WaveWriter::write(const int16_t *samples, size_t count) {
  if (fd_ < 0)
    throw std::runtime_error("audio segment is not open");
  if (samples_ + count > 16000 * 20)
    throw std::runtime_error("audio segment exceeds 20 seconds");
  std::vector<uint8_t> data(count * 2);
  for (size_t i = 0; i < count; ++i)
    le16(data.data() + 2 * i, uint16_t(samples[i]));
  write_all(fd_, data.data(), data.size());
  samples_ += count;
}
void WaveWriter::close(bool durable) {
  if (fd_ < 0)
    return;
  std::array<uint8_t, 44> h{};
  std::memcpy(h.data(), "RIFF", 4);
  le32(h.data() + 4, 36 + uint32_t(samples_ * 2));
  std::memcpy(h.data() + 8, "WAVEfmt ", 8);
  le32(h.data() + 16, 16);
  le16(h.data() + 20, 1);
  le16(h.data() + 22, 1);
  le32(h.data() + 24, 16000);
  le32(h.data() + 28, 32000);
  le16(h.data() + 32, 2);
  le16(h.data() + 34, 16);
  std::memcpy(h.data() + 36, "data", 4);
  le32(h.data() + 40, uint32_t(samples_ * 2));
  if (lseek(fd_, 0, SEEK_SET) < 0) {
    ::close(fd_);
    fd_ = -1;
    throw std::runtime_error("cannot finalize WAV");
  }
  try {
    write_all(fd_, h.data(), h.size());
  } catch (...) {
    ::close(fd_);
    fd_ = -1;
    throw;
  }
  ::close(fd_);
  fd_ = -1;
  if (path_.extension() == ".part") {
    auto final = path_;
    final.replace_extension(".wav");
    fs::rename(path_, final);
    path_ = final;
  }
  if (durable)
    sync_closed_file(path_);
}
WaveWriter::~WaveWriter() {
  try {
    close();
  } catch (...) {
  }
}
} // namespace echotrace
