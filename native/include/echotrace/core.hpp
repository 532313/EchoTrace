#pragma once
#include <cstdint>
#include <deque>
#include <filesystem>
#include <functional>
#include <json/json.h>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace echotrace {
uint64_t monotonic_us();
std::string boot_id();
std::string json(const Json::Value &value);
Json::Value parse_json(const std::string &value);
void atomic_json(const std::filesystem::path &path, const Json::Value &value);
void sync_closed_file(const std::filesystem::path &path);
std::string sha256_file(const std::filesystem::path &path);
std::vector<uint8_t> decode_base64(const std::string &text, size_t maximum);

struct Packet {
  uint16_t kind = 1;
  uint32_t sequence = 0;
  uint64_t source_us = 0;
  uint32_t samples = 640;
  std::vector<uint8_t> payload;
  std::vector<uint8_t> encode() const;
  static Packet decode(const std::vector<uint8_t> &bytes);
};

class Journal {
public:
  Journal(std::filesystem::path root, std::string device, std::string boot);
  Json::Value append(const std::string &session, const std::string &kind,
                     uint64_t stamp, const std::string &note = "",
                     Json::Value payload = Json::objectValue);
  void persist(const Json::Value &message);
  std::vector<Json::Value> replay() const;
  std::vector<Json::Value> read_from(uint64_t &cursor, size_t limit = 8) const;
  void visit(const std::function<void(const Json::Value &)> &fn) const;
  const std::string device_id, boot;

private:
  std::filesystem::path root_;
  mutable std::mutex mutex_;
  uint64_t sequence_ = 0;
};

class WaveWriter {
public:
  void open(const std::filesystem::path &path);
  void write(const int16_t *samples, size_t count);
  void close(bool durable = true);
  uint64_t samples() const { return samples_; }
  ~WaveWriter();

private:
  std::filesystem::path path_;
  int fd_ = -1;
  uint64_t samples_ = 0;
};

template <class T> class BoundedQueue {
public:
  explicit BoundedQueue(size_t limit) : limit_(limit) {}
  bool push(T value) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (items_.size() >= limit_)
      return false;
    items_.push_back(std::move(value));
    return true;
  }
  std::optional<T> pop() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (items_.empty())
      return {};
    T value = std::move(items_.front());
    items_.pop_front();
    return value;
  }

private:
  size_t limit_;
  std::deque<T> items_;
  std::mutex mutex_;
};
} // namespace echotrace
