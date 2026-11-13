#include "echotrace/capture.hpp"
#include "echotrace/media_lease.h"
#include <cerrno>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>
#include <websocketpp/client.hpp>
#include <websocketpp/config/asio_no_tls_client.hpp>

using namespace echotrace;
namespace fs = std::filesystem;
using Client = websocketpp::client<websocketpp::config::asio_client>;
namespace {
volatile sig_atomic_t quitting = 0;
void signal_stop(int) { quitting = 1; }
bool component(const std::string &text) {
  return !text.empty() && text.size() < 128 &&
         text.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRST"
                                "UVWXYZ0123456789-_") == std::string::npos;
}
Json::Value load_json(const fs::path &path) {
  std::ifstream stream(path);
  if (!stream)
    return Json::Value(Json::objectValue);
  std::string text((std::istreambuf_iterator<char>(stream)), {});
  return parse_json(text);
}

class Edge {
  struct ClosedMedia {
    fs::path path;
    std::string kind, session;
    uint64_t begin, end;
    bool discontinuity;
  };
public:
  Edge(fs::path root, std::string hub, std::string token, bool simulated,
       Json::Value config)
      : root_(fs::weakly_canonical(root)), hub_(std::move(hub)),
        token_(std::move(token)), simulated_(simulated),
        config_(std::move(config)),
        journal_(
            root_ / "journal",
            config_.get("device_id", simulated ? "simulator-edge" : "echo-mate")
                .asString(),
            boot_id()) {
    fs::create_directories(root_);
    retired_ = load_json(root_ / "retired.json");
    seen_commands_ = load_json(root_ / "commands.json");
    const auto previous = load_json(root_ / "state.json");
    if (previous.isMember("session") &&
        !previous["session"]["id"].asString().empty() &&
        previous["state"] != "stopped") {
      Json::Value reconcile;
      reconcile["type"] = "session.reconcile";
      reconcile["session_id"] = previous["session"]["id"];
      reconcile["state"] = "stopped";
      reconcile["reason"] = "source_restart";
      uint64_t last =
          previous.get("source_time_us", Json::UInt64(0)).asUInt64();
      journal_.visit([&](const Json::Value &message) {
        if (message["type"] == "media.register" &&
            message["media"]["session_id"] == reconcile["session_id"])
          last = std::max(last, message["media"]["end_us"].asUInt64());
        if (message["type"] == "event" &&
            message["event"]["session_id"] == reconcile["session_id"])
          last = std::max(last, message["event"]["source_time_us"].asUInt64());
      });
      reconcile["source_time_us"] = Json::UInt64(last);
      journal_.persist(reconcile);
    }
    CaptureConfig capture_config;
    capture_config.root = root_;
    capture_config.alsa = config_.get("alsa", "hw:0,0").asString();
    capture_config.capture_channels =
        config_.get("capture_channels", 2).asUInt();
    capture_config.microphone_channel =
        config_.get("microphone_channel", 0).asUInt();
    capture_config.capture_latency_us =
        config_.get("capture_latency_us", 500000).asUInt();
    if (capture_config.capture_latency_us < 80000 ||
        capture_config.capture_latency_us > 1000000)
      throw std::runtime_error("capture_latency_us must be 80000..1000000");
    capture_config.iq = config_.get("iq_directory", "/etc/iqfiles").asString();
    capture_config.reserve_bytes =
        config_.get("reserve_mb", 256).asUInt64() * 1024 * 1024;
    capture_config.iio_device = config_.get("iio_device", "").asString();
    capture_config.input_device = config_.get("input_device", "").asString();
    capture_config.pause_device = config_.get("pause_device", "").asString();
    capture_config.calibration = config_.get("calibration_file", "").asString();
    capture_ = make_capture(
        capture_config,
        [this](const fs::path &path, const std::string &kind, uint64_t begin,
               uint64_t end,
               bool gap) { media_closed(path, kind, begin, end, gap); },
        [this](const std::string &kind, uint64_t stamp, const std::string &note,
               Json::Value data) {
          observation(kind, stamp, note, std::move(data));
        },
        simulated_);
    client_.clear_access_channels(websocketpp::log::alevel::all);
    client_.clear_error_channels(websocketpp::log::elevel::all);
    client_.init_asio();
    client_.start_perpetual();
    client_.set_open_handler([this](websocketpp::connection_hdl hdl) {
      std::lock_guard<std::mutex> lock(connection_mutex_);
      if (!same_connection(handle_, hdl))
        return;
      Json::Value hello;
      hello["type"] = "hello";
      hello["version"] = 3;
      hello["device_id"] = journal_.device_id;
      hello["boot_id"] = journal_.boot;
      hello["source_time_us"] = Json::UInt64(monotonic_us());
      hello["capture_state"] = state();
      hello["session_id"] = snapshot()["session"]["id"];
      hello["capabilities"].append(simulated_ ? "synthetic_capture"
                                              : "rv1106_capture");
      hello["capabilities"].append("durable_replay");
      hello["capabilities"].append("media_upload");
      hello["capabilities"].append("media_chunk_ack");
      websocketpp::lib::error_code ec;
      client_.send(hdl, json(hello), websocketpp::frame::opcode::text, ec);
      if (ec) {
        connecting_ = false;
        return;
      }
      // Wait for the application hello before replay or upload traffic.
    });
    client_.set_close_handler([this](websocketpp::connection_hdl hdl) {
      std::lock_guard<std::mutex> lock(connection_mutex_);
      if (!same_connection(handle_, hdl))
        return;
      connected_ = false;
      connecting_ = false;
      websocketpp::lib::error_code ec;
      auto connection = client_.get_con_from_hdl(hdl, ec);
      if (!ec)
        std::cerr << "hub connection closed: "
                  << connection->get_remote_close_code() << " "
                  << connection->get_remote_close_reason() << "\n";
    });
    client_.set_fail_handler([this](websocketpp::connection_hdl hdl) {
      std::lock_guard<std::mutex> lock(connection_mutex_);
      if (!same_connection(handle_, hdl))
        return;
      connected_ = false;
      connecting_ = false;
      websocketpp::lib::error_code ec;
      auto connection = client_.get_con_from_hdl(hdl, ec);
      if (!ec)
        std::cerr << "hub connection failed: " << connection->get_ec().message()
                  << "\n";
    });
    client_.set_message_handler([this](websocketpp::connection_hdl hdl,
                                       Client::message_ptr message) {
      if (!current_connection(hdl))
        return;
      try {
        if (message->get_opcode() == websocketpp::frame::opcode::binary) {
          const auto &payload = message->get_payload();
          auto packet = Packet::decode(
              std::vector<uint8_t>(payload.begin(), payload.end()));
          std::lock_guard<std::mutex> lock(download_mutex_);
          if (packet.kind != 2 || !download_.is_open() ||
              packet.sequence != download_sequence_ ||
              download_bytes_ + packet.payload.size() > download_size_)
            throw std::runtime_error("invalid feedback audio transfer");
          download_.write(reinterpret_cast<const char *>(packet.payload.data()),
                          packet.payload.size());
          if (!download_)
            throw std::runtime_error("feedback audio write failed");
          download_bytes_ += packet.payload.size();
          ++download_sequence_;
          return;
        }
        if (message->get_opcode() != websocketpp::frame::opcode::text)
          return;
        auto value = parse_json(message->get_payload());
        if (value["type"] == "command") {
          if (!commands_.push(value))
            throw std::runtime_error("command queue full");
        } else if (value["type"] == "hello" ||
                   value["type"] == "projects.changed") {
          {
            std::lock_guard<std::mutex> lock(state_mutex_);
            projects_ = value["projects"];
          }
          if (value["type"] == "hello" && value.get("accepted", false).asBool()) {
            chunk_ack_ = value.get("media_chunk_ack", false).asBool();
            replay_pending_ = true;
            std::lock_guard<std::mutex> lock(connection_mutex_);
            if (same_connection(handle_, hdl)) {
              connected_ = true;
              connecting_ = false;
            }
          }
        } else if (value["type"] == "media.chunk.ack") {
          upload_acks_.push(value);
        } else if (value["type"] == "query.result") {
          auto result = value["result"];
          Json::Value brief;
          brief["answer"] = result["answer"];
          brief["status"] = result["status"];
          brief["received_us"] = Json::UInt64(monotonic_us());
          if (result["hits"].size()) {
            const auto &hit = result["hits"][0];
            const auto stamp = hit["anchor"]["source_time_us"].asUInt64();
            for (const auto &m : hit["evidence"]["media"]) {
              if (m["kind"] == "audio" && m["start_us"].asUInt64() <= stamp &&
                  stamp < m["end_us"].asUInt64()) {
                brief["audio_media_id"] = m["id"];
                brief["audio_offset"] =
                    double(stamp - m["start_us"].asUInt64()) / 1000000;
                break;
              }
            }
          }
          const auto &thumb = result["thumbnail"];
          if (thumb.isObject() && thumb["width"].asInt() == 160 &&
              thumb["height"].asInt() == 90 && thumb["format"] == "rgb565le") {
            auto bytes = decode_base64(thumb["data"].asString(), 28800);
            if (bytes.size() != 28800)
              throw std::runtime_error("wrong thumbnail dimensions");
            fs::create_directories(root_ / "thumbnails");
            const auto part = root_ / "thumbnails/query.part";
            const auto final = root_ / "thumbnails/query.rgb565";
            std::ofstream file(part, std::ios::binary);
            file.write(reinterpret_cast<char *>(bytes.data()), bytes.size());
            file.close();
            if (!file)
              throw std::runtime_error("thumbnail write failed");
            fs::rename(part, final);
            brief["thumbnail_path"] = final.string();
          }
          std::lock_guard<std::mutex> lock(state_mutex_);
          last_query_ = std::move(brief);
        } else if (value["type"] == "error") {
          std::cerr << "hub: " << value["error"].asString() << "\n";
        }
      } catch (const std::exception &e) {
        std::cerr << e.what() << "\n";
      }
    });
  }
  ~Edge() {
    capture_->stop();
    release_media_lease();
    connected_ = false;
    websocketpp::lib::error_code ec;
    client_.close(connection_handle(), websocketpp::close::status::going_away,
                  "shutdown", ec);
    client_.stop_perpetual();
    client_.stop();
    if (network_.joinable())
      network_.join();
  }
  Json::Value snapshot() const {
    std::lock_guard<std::mutex> lock(state_mutex_);
    Json::Value value;
    value["device_id"] = journal_.device_id;
    value["boot_id"] = journal_.boot;
    value["source_time_us"] = Json::UInt64(monotonic_us());
    value["state"] = state_;
    value["session"] = session_;
    value["connected"] = connected_.load();
    value["synthetic"] = simulated_;
    value["projects"] = projects_;
    value["query"] = last_query_;
    value["last_marker_us"] = Json::UInt64(last_marker_us_);
    value["playback_active"] = capture_->playback_active();
    value["error"] = capture_error_.empty() ? last_error_ : capture_error_;
    return value;
  }
  std::string state() const {
    std::lock_guard<std::mutex> lock(state_mutex_);
    return state_;
  }
  Json::Value local(const Json::Value &request) {
    Json::Value result;
    const auto op = request.get("op", "status").asString();
    if (op == "status")
      return snapshot();
    if (op == "evidence.play") {
      Json::Value command;
      {
        std::lock_guard<std::mutex> lock(state_mutex_);
        if (last_query_["audio_media_id"].asString().empty())
          throw std::runtime_error("没有可回放的原始声音证据");
        command["arguments"]["media_id"] = last_query_["audio_media_id"];
        command["arguments"]["offset_seconds"] = last_query_["audio_offset"];
      }
      command["id"] = "local-play-" + std::to_string(monotonic_us());
      command["op"] = "playback.start";
      if (!commands_.push(command))
        throw std::runtime_error("command queue full");
      result["queued"] = true;
      return result;
    }
    if (!connected_) {
      if (op == "mark" || op == "session.pause" || op == "session.resume" ||
          op == "session.stop") {
        Json::Value command;
        command["id"] = "local-" + std::to_string(monotonic_us());
        command["op"] = op;
        command["arguments"] = request;
        command["arguments"]["session_id"] = session_id();
        if (!commands_.push(command))
          throw std::runtime_error("command queue full");
        result["queued_offline"] = true;
        return result;
      }
      throw std::runtime_error(
          "hub disconnected; existing recording continues locally");
    }
    if (op == "query") {
      if (request["text"].asString().empty())
        throw std::runtime_error(
            "记录中说“查一下”后提出问题，或在工作台输入问题");
      Json::Value message;
      message["type"] = "query";
      message["id"] = std::to_string(monotonic_us());
      message["query"]["text"] = request["text"];
      const auto snap = snapshot();
      message["query"]["project_id"] = request.isMember("project_id")
                                           ? request["project_id"]
                                           : snap["session"]["project_id"];
      outgoing_.push(message);
      result["ok"] = true;
      return result;
    }
    Json::Value message;
    message["type"] = "intent";
    message["op"] = op;
    message["arguments"] = request;
    outgoing_.push(message);
    result["ok"] = true;
    return result;
  }
  void run() {
    network_ = std::thread([this] { client_.run(); });
    uint64_t retry = 0, heartbeat = 0;
    while (!quitting) {
      const auto now = monotonic_us();
      {
        std::lock_guard<std::mutex> lock(connection_mutex_);
        if (!connected_ && !connecting_ && now >= retry) {
          connecting_ = true;
          websocketpp::lib::error_code ec;
          auto connection = client_.get_connection(hub_, ec);
          if (!ec) {
            handle_ = connection->get_handle();
            if (!token_.empty())
              connection->append_header("Authorization", "Bearer " + token_);
            client_.connect(connection);
          } else
            connecting_ = false;
          retry = now + 3000000;
        }
      }
      if (auto command = commands_.pop())
        execute(*command);
      commit_closed_media();
      if (connected_) {
        if (replay_pending_.exchange(false)) {
          replay_cursor_ = 0;
          replay_complete_ = false;
        }
        // Send a bounded batch per tick, leaving time for marking/control.
        if (!replay_complete_) {
          auto batch = journal_.read_from(replay_cursor_);
          replay_complete_ = batch.empty();
          for (const auto &m : batch) {
            const auto sid = m["type"] == "event"
                                 ? m["event"]["session_id"].asString()
                                 : (m["type"] == "media.register"
                                        ? m["media"]["session_id"].asString()
                                        : m["session_id"].asString());
            if (!retired_.isMember(sid))
              send(m);
          }
        }
        if (auto message = outgoing_.pop())
          send(*message);
        upload_tick();
      }
      if (now - heartbeat >= 3000000) {
        auto value = snapshot();
        value["type"] = "heartbeat";
        value["capture_state"] = value["state"];
        value["session_id"] = value["session"]["id"];
        send(value);
        persist_state();
        heartbeat = now;
      }
      auto error = capture_->error();
      if (error.empty())
        error = media_error_;
      if (!error.empty() && state() == "recording") {
        observation("gap", now, "采集故障：" + error, Json::objectValue);
        capture_->stop();
        drain_closed_media();
        capture_faulted_ = true;
        {
          std::lock_guard<std::mutex> lock(state_mutex_);
          state_ = "paused";
          capture_error_ = error.find("storage reserve") != std::string::npos
              ? "存储低于预留值，录制已停止。请结束会话，清理后重新开始。"
              : "采集已停止：" + error;
        }
        Json::Value reconcile;
        reconcile["type"] = "session.reconcile";
        reconcile["session_id"] = session_id();
        reconcile["state"] = "paused";
        reconcile["source_time_us"] = Json::UInt64(monotonic_us());
        journal_.persist(reconcile);
        outgoing_.push(reconcile);
        persist_state();
      }
      if (media_lease_fd_ >= 0 && state() == "stopped" &&
          !capture_->playback_active())
        release_media_lease();
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    capture_->stop();
    release_media_lease();
    drain_closed_media();
    if (!session_id().empty()) {
      Json::Value reconcile;
      reconcile["type"] = "session.reconcile";
      reconcile["session_id"] = session_id();
      reconcile["state"] = "stopped";
      reconcile["source_time_us"] = Json::UInt64(monotonic_us());
      journal_.persist(reconcile);
      if (connected_)
        send(reconcile);
    }
    {
      std::lock_guard<std::mutex> lock(state_mutex_);
      state_ = "stopped";
    }
    persist_state();
  }

private:
  std::string session_id() const {
    std::lock_guard<std::mutex> lock(state_mutex_);
    return session_["id"].asString();
  }
  void persist_state() {
    // Audio/video close callbacks and the heartbeat share this atomic file.
    // Serialize snapshot + commit so a writer cannot truncate another's .part.
    std::lock_guard<std::mutex> lock(persist_mutex_);
    atomic_json(root_ / "state.json", snapshot());
  }
  bool send(const Json::Value &value) {
    if (!connected_)
      return false;
    websocketpp::lib::error_code ec;
    std::lock_guard<std::mutex> lock(connection_mutex_);
    client_.send(handle_, json(value), websocketpp::frame::opcode::text, ec);
    if (ec) {
      connected_ = false;
      return false;
    }
    return true;
  }
  void observation(const std::string &kind, uint64_t stamp,
                   const std::string &note, Json::Value payload) {
    const auto sid = session_id();
    if (sid.empty())
      return;
    if (kind == "recording_state") {
      Json::Value reconcile;
      reconcile["type"] = "session.reconcile";
      reconcile["session_id"] = sid;
      reconcile["state"] = payload["state"];
      reconcile["source_time_us"] = Json::UInt64(stamp);
      {
        std::lock_guard<std::mutex> lock(state_mutex_);
        state_ = payload["state"].asString();
      }
      journal_.persist(reconcile);
      outgoing_.push(reconcile);
      persist_state();
      auto gap = journal_.append(sid, "gap", stamp, note, payload);
      outgoing_.push(gap);
      return;
    }
    if (state() != "recording" && kind != "gap" && kind != "sensor_fault")
      return;
    if (kind == "pedal") {
      std::lock_guard<std::mutex> lock(state_mutex_);
      last_marker_us_ = stamp;
    }
    payload["synthetic"] = simulated_;
    auto message = journal_.append(sid, kind, stamp, note, std::move(payload));
    if (!outgoing_.push(std::move(message)))
      replay_pending_ = true;
  }
  void media_closed(const fs::path &path, const std::string &kind,
                    uint64_t begin, uint64_t end, bool discontinuity) {
    const auto sid = session_id();
    if (sid.empty() || end <= begin)
      return;
    // Capture threads only hand off a closed file. Hashing and journal fsync
    // must not stall ALSA draining at each ten-second boundary.
    if (!closed_media_.push({path, kind, sid, begin, end, discontinuity}))
      throw std::runtime_error("closed media index queue full");
  }
  bool commit_closed_media() {
    auto item = closed_media_.pop();
    if (!item)
      return false;
    try {
      register_closed_media(*item);
    } catch (const std::exception &error) {
      media_error_ = std::string("media index: ") + error.what();
      std::cerr << media_error_ << "\n";
    }
    return true;
  }
  void drain_closed_media() {
    while (commit_closed_media()) {}
  }
  void register_closed_media(const ClosedMedia &closed) {
    const auto &path = closed.path;
    const auto &kind = closed.kind;
    const auto &sid = closed.session;
    const auto begin = closed.begin, end = closed.end;
    if (kind == "audio")
      sync_closed_file(path);
    Json::Value item;
    item["id"] = journal_.boot + "-" + kind + "-" + std::to_string(begin);
    item["session_id"] = sid;
    item["device_id"] = journal_.device_id;
    item["boot_id"] = journal_.boot;
    item["start_us"] = Json::UInt64(begin);
    item["end_us"] = Json::UInt64(end);
    item["kind"] = kind;
    item["size_bytes"] = Json::UInt64(fs::file_size(path));
    item["sha256"] = sha256_file(path);
    item["mime"] = kind == "video" ? "video/mp4" : "audio/wav";
    item["discontinuity"] = closed.discontinuity;
    Json::Value meta;
    meta["path"] = fs::absolute(path).string();
    meta["media"] = item;
    atomic_json(root_ / "manifests" / (item["id"].asString() + ".json"), meta);

    Json::Value message;
    message["type"] = "media.register";
    message["media"] = item;
    journal_.persist(message);
    if (!outgoing_.push(message))
      replay_pending_ = true;
    if (kind == "audio") {
      Json::Value payload;
      payload["media_id"] = item["id"];
      payload["synthetic"] = simulated_;
      auto note = journal_.append(sid, "voice", begin, "", payload);
      if (!outgoing_.push(note))
        replay_pending_ = true;
    }
    persist_state();
  }
  void execute(const Json::Value &command) {
    Json::Value reply;
    reply["type"] = "ack";
    reply["id"] = command["id"];
    reply["ok"] = true;
    const auto id = command["id"].asString();
    if (seen_commands_.isMember(id)) {
      send(seen_commands_[id]);
      return;
    }
    const auto op = command["op"].asString();
    // A saturated background upload queue is retried by the hub. It must not
    // replace a capture or user-command error on the small screen.
    const bool visible_error = op != "media.request";
    try {
      if (visible_error) {
        std::lock_guard<std::mutex> lock(state_mutex_);
        last_error_.clear();
      }
      const auto &args = command["arguments"];
      if (op == "session.start") {
        auto session = args["session"];
        if (!component(session["id"].asString()) ||
            session["boot_id"].asString() != journal_.boot)
          throw std::runtime_error("invalid session source clock");
        if (state() == "recording" || state() == "paused")
          throw std::runtime_error("a session is already active");
        acquire_media_lease();
        {
          std::lock_guard<std::mutex> lock(state_mutex_);
          session_ = session;
          state_ = "recording";
        }
        try {
          capture_->start(session["id"].asString());
          capture_faulted_ = false;
          media_error_.clear();
          {
            std::lock_guard<std::mutex> lock(state_mutex_);
            capture_error_.clear();
          }
        } catch (...) {
          release_media_lease();
          std::lock_guard<std::mutex> lock(state_mutex_);
          state_ = "stopped";
          capture_error_.clear();
          throw;
        }
        persist_state();
      } else if (op == "session.pause" || op == "session.resume" ||
                 op == "session.stop") {
        if (args["session_id"].asString() != session_id())
          throw std::runtime_error("session identity mismatch");
        if (op == "session.stop") {
          capture_->stop();
          release_media_lease();
          drain_closed_media();
          std::lock_guard<std::mutex> lock(state_mutex_);
          state_ = "stopped";
          capture_error_.clear();
        } else {
          if (op == "session.resume" && capture_faulted_)
            throw std::runtime_error("capture fault: stop this session and "
                                     "start a new one before recording");
          const bool pause = op == "session.pause";
          capture_->pause(pause);
          std::lock_guard<std::mutex> lock(state_mutex_);
          state_ = pause ? "paused" : "recording";
        }
        persist_state();
        Json::Value reconcile;
        reconcile["type"] = "session.reconcile";
        reconcile["session_id"] = session_id();
        reconcile["state"] = state();
        reconcile["source_time_us"] = Json::UInt64(monotonic_us());
        journal_.persist(reconcile);
        outgoing_.push(reconcile);
      } else if (op == "mark") {
        if (args["session_id"].asString() != session_id() ||
            state() != "recording")
          throw std::runtime_error("recording is not active");
        observation("pedal", monotonic_us(), args.get("note", "").asString(),
                    Json::objectValue);
      } else if (op == "media.request") {
        const auto media_id = args["media_id"].asString();
        media_path(media_id);
        if (!requested_uploads_.count(media_id)) {
          if (!upload_requests_.push(media_id))
            throw std::runtime_error("media request queue full");
          requested_uploads_.insert(media_id);
        }
      } else if (op == "playback.cache.begin") {
        std::lock_guard<std::mutex> lock(download_mutex_);
        const auto name = args["id"].asString();
        const auto size = args["size_bytes"].asUInt64();
        if (!component(name) || !size || size > 2 * 1024 * 1024)
          throw std::runtime_error("invalid feedback audio metadata");
        if (download_.is_open())
          download_.close();
        fs::create_directories(root_ / "playback");
        for (const auto &entry : fs::directory_iterator(root_ / "playback"))
          fs::remove(entry.path());
        download_path_ = root_ / "playback" / (name + ".part");
        download_id_ = name;
        download_size_ = size;
        download_bytes_ = 0;
        download_sequence_ = 0;
        download_hash_ = args["sha256"].asString();
        download_.clear();
        download_.open(download_path_, std::ios::binary | std::ios::trunc);
        if (!download_)
          throw std::runtime_error("feedback cache unavailable");
      } else if (op == "playback.cache.end") {
        std::lock_guard<std::mutex> lock(download_mutex_);
        if (args["id"].asString() != download_id_ || !download_.is_open() ||
            download_bytes_ != download_size_)
          throw std::runtime_error("incomplete feedback audio");
        download_.flush();
        download_.close();
        if (sha256_file(download_path_) != download_hash_)
          throw std::runtime_error("feedback audio checksum mismatch");
        auto final = download_path_;
        final.replace_extension(".wav");
        fs::rename(download_path_, final);
        acquire_media_lease();
        capture_->play(final, 0);
      } else if (op == "playback.start") {
        auto path = media_path(args["media_id"].asString());
        acquire_media_lease();
        capture_->play(path, args.get("offset_seconds", 0.0).asDouble());
      } else if (op == "playback.stop") {
        capture_->stop_playback();
        if (state() == "stopped")
          release_media_lease();
      }
      else if (op == "session.delete") {
        const auto sid = args["session_id"].asString();
        if (!component(sid) || sid == session_id() && state() != "stopped")
          throw std::runtime_error("stop the session before deleting");
        retired_[sid] = true;
        atomic_json(root_ / "retired.json", retired_);
        fs::remove_all(root_ / "sessions" / sid);
        for (const auto &entry : fs::directory_iterator(root_ / "manifests")) {
          if (entry.path().extension() != ".json")
            continue;
          auto meta = load_json(entry.path());
          if (meta["media"]["session_id"].asString() == sid) {
            if (upload_id_ == meta["media"]["id"].asString()) {
              upload_.close();
              upload_.clear();
            }
            fs::remove(entry.path());
          }
        }
      } else
        throw std::runtime_error("unknown command");
    } catch (const std::exception &e) {
      reply["ok"] = false;
      reply["error"] = e.what();
      if (visible_error) {
        std::lock_guard<std::mutex> lock(state_mutex_);
        last_error_ = e.what();
      }
    }
    reply["source_time_us"] = Json::UInt64(monotonic_us());
    seen_commands_[id] = reply;
    if (seen_commands_.size() > 128)
      seen_commands_.clear();
    atomic_json(root_ / "commands.json", seen_commands_);
    send(reply);
  }
  void acquire_media_lease() {
    if (simulated_ || media_lease_fd_ >= 0)
      return;
    media_lease_fd_ = echotrace_media_lease_acquire(nullptr);
    if (media_lease_fd_ >= 0)
      return;
    if (errno == EWOULDBLOCK || errno == EAGAIN)
      throw std::runtime_error(
          "Echo-Mate 聊天或 AI 相机正在使用声画设备，请先退出该应用");
    throw std::runtime_error(std::string("media lease: ") +
                             std::strerror(errno));
  }
  void release_media_lease() {
    echotrace_media_lease_release(media_lease_fd_);
    media_lease_fd_ = -1;
  }
  fs::path media_path(const std::string &id) {
    if (!component(id))
      throw std::runtime_error("invalid media id");
    auto meta = load_json(root_ / "manifests" / (id + ".json"));
    if (!meta.isMember("path"))
      throw std::runtime_error("source media unavailable");
    const auto path = fs::weakly_canonical(meta["path"].asString());
    const auto relative = path.lexically_relative(root_);
    if (relative.empty() || *relative.begin() == ".." ||
        !fs::is_regular_file(path))
      throw std::runtime_error("invalid source media path");
    return path;
  }
  websocketpp::connection_hdl connection_handle() {
    std::lock_guard<std::mutex> lock(connection_mutex_);
    return handle_;
  }
  static bool same_connection(websocketpp::connection_hdl a,
                              websocketpp::connection_hdl b) {
    return !a.owner_before(b) && !b.owner_before(a);
  }
  bool current_connection(websocketpp::connection_hdl hdl) {
    std::lock_guard<std::mutex> lock(connection_mutex_);
    return same_connection(handle_, hdl);
  }
  void upload_tick() {
    const auto hdl = connection_handle();
    websocketpp::lib::error_code ec;
    auto connection = client_.get_con_from_hdl(hdl, ec);
    if (ec || connection->get_state() != websocketpp::session::state::open)
      return;
    // An upload belongs to one WebSocket. Restart its framing only when the
    // actual connection changes, never because an old close callback fires.
    if (upload_.is_open() && !same_connection(upload_handle_, hdl)) {
      upload_.close();
      upload_.clear();
      if (!upload_requests_.push(upload_id_))
        requested_uploads_.erase(upload_id_);
      upload_waiting_ = false;
    }
    if (!upload_.is_open())
      upload_waiting_ = false;
    while (auto ack = upload_acks_.pop()) {
      if ((*ack)["media_id"].asString() == upload_id_ &&
          (*ack)["sequence"].asUInt() + 1 == upload_sequence_)
        upload_waiting_ = false;
    }
    if (upload_waiting_)
      return;
    if (!upload_.is_open()) {
      auto next = upload_requests_.pop();
      if (!next)
        return;
      upload_id_ = *next;
      fs::path path;
      try {
        path = media_path(upload_id_);
      } catch (const std::exception &e) {
        std::cerr << e.what() << "\n";
        requested_uploads_.erase(upload_id_);
        return;
      }
      upload_.open(path, std::ios::binary);
      if (!upload_) {
        requested_uploads_.erase(upload_id_);
        return;
      }
      upload_handle_ = hdl;
      upload_sequence_ = 0;
      Json::Value begin;
      begin["type"] = "media.begin";
      begin["media_id"] = upload_id_;
      client_.send(hdl, json(begin), websocketpp::frame::opcode::text, ec);
      if (ec)
        return;
    }
    if (connection->get_buffered_amount() > 512 * 1024)
      return;
    // End-to-end flow control keeps video bytes out of the command/heartbeat
    // backlog, including when SSH tunnels hide a large intermediate buffer.
    std::vector<uint8_t> data(chunk_ack_ ? 16384 : 65536);
    upload_.read(reinterpret_cast<char *>(data.data()), data.size());
    data.resize(size_t(upload_.gcount()));
    if (!data.empty()) {
      Packet packet;
      packet.kind = 2;
      packet.samples = 0;
      packet.sequence = upload_sequence_++;
      packet.payload = std::move(data);
      const auto bytes = packet.encode();
      client_.send(hdl, bytes.data(), bytes.size(),
                   websocketpp::frame::opcode::binary, ec);
      if (ec) {
        connected_ = false;
        return;
      }
      upload_waiting_ = chunk_ack_;
    }
    if (upload_.eof() && !upload_waiting_) {
      Json::Value end;
      end["type"] = "media.end";
      end["media_id"] = upload_id_;
      client_.send(hdl, json(end), websocketpp::frame::opcode::text, ec);
      upload_.close();
      upload_.clear();
      requested_uploads_.erase(upload_id_);
    }
  }
  fs::path root_;
  std::string hub_, token_;
  bool simulated_;
  int media_lease_fd_ = -1;
  bool capture_faulted_ = false;
  Json::Value config_;
  Journal journal_;
  std::unique_ptr<Capture> capture_;
  mutable std::mutex state_mutex_, media_mutex_, connection_mutex_, persist_mutex_,
      download_mutex_;
  std::string state_ = "stopped";
  Json::Value session_, projects_, last_query_, retired_, seen_commands_;
  uint64_t last_marker_us_ = 0;
  std::string last_error_;
  std::string capture_error_;
  std::string media_error_;
  Client client_;
  websocketpp::connection_hdl handle_, upload_handle_;
  std::thread network_;
  std::atomic<bool> connected_{false}, connecting_{false}, replay_pending_{false},
      chunk_ack_{false};
  BoundedQueue<Json::Value> commands_{64}, outgoing_{128};
  BoundedQueue<Json::Value> upload_acks_{64};
  BoundedQueue<std::string> upload_requests_{32};
  BoundedQueue<ClosedMedia> closed_media_{16};
  std::set<std::string> requested_uploads_;
  uint64_t replay_cursor_ = 0;
  bool replay_complete_ = true;

  std::ifstream upload_;
  std::string upload_id_;
  uint32_t upload_sequence_ = 0;
  bool upload_waiting_ = false;
  std::ofstream download_;
  fs::path download_path_;
  std::string download_id_, download_hash_;
  uint64_t download_size_ = 0, download_bytes_ = 0;
  uint32_t download_sequence_ = 0;
};

class ControlSocket {
public:
  ControlSocket(const fs::path &path, Edge &edge) : path_(path) {
    if (path.string().size() >= sizeof(sockaddr_un::sun_path))
      throw std::runtime_error("control socket path too long");
    fs::create_directories(path.parent_path());
    lock_ = open((path.string() + ".lock").c_str(), O_CREAT | O_RDWR, 0600);
    if (lock_ < 0 || flock(lock_, LOCK_EX | LOCK_NB))
      throw std::runtime_error(
          "another capture daemon owns this control socket");
    struct stat info{};
    if (lstat(path.c_str(), &info) == 0) {
      if (!S_ISSOCK(info.st_mode) || info.st_uid != getuid())
        throw std::runtime_error("refuse to replace foreign control path");
      unlink(path.c_str());
    }
    fd_ = socket(AF_UNIX, SOCK_STREAM, 0);
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    std::strncpy(address.sun_path, path.c_str(), sizeof(address.sun_path) - 1);
    if (bind(fd_, reinterpret_cast<sockaddr *>(&address), sizeof(address)) ||
        listen(fd_, 4))
      throw std::runtime_error("control socket bind failed");
    chmod(path.c_str(), 0600);
    thread_ = std::thread([this, &edge] {
      while (running_) {
        int peer = accept(fd_, nullptr, nullptr);
        if (peer < 0) {
          if (!running_)
            break;
          continue;
        }
        timeval timeout{1, 0};
        setsockopt(peer, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        std::string message;
        char buffer[1024];
        while (message.size() < 8192 &&
               message.find('\n') == std::string::npos) {
          const auto n = recv(peer, buffer, sizeof(buffer), 0);
          if (n <= 0)
            break;
          message.append(buffer, size_t(n));
        }
        Json::Value reply;
        try {
          reply = edge.local(parse_json(message));
        } catch (const std::exception &e) {
          reply["error"] = e.what();
        }
        const auto text = json(reply) + "\n";
        size_t at = 0;
        while (at < text.size()) {
          const auto n = ::send(peer, text.data() + at, text.size() - at, 0);
          if (n <= 0)
            break;
          at += size_t(n);
        }
        close(peer);
      }
    });
  }
  ~ControlSocket() {
    running_ = false;
    shutdown(fd_, SHUT_RDWR);
    close(fd_);
    if (thread_.joinable())
      thread_.join();
    unlink(path_.c_str());
    if (lock_ >= 0)
      close(lock_);
  }

private:
  fs::path path_;
  int fd_ = -1, lock_ = -1;
  std::atomic<bool> running_{true};
  std::thread thread_;
};
} // namespace
int main(int argc, char **argv) {
  try {
    fs::path data = "runtime/edge", config_path,
             socket_path = "/tmp/echotrace-edge.sock";
    std::string hub = "ws://127.0.0.1:18081/ws/device";
    bool simulated = false;
    for (int i = 1; i < argc; ++i) {
      const std::string arg = argv[i];
      if (arg == "--simulated")
        simulated = true;
      else if (arg == "--data" && i + 1 < argc)
        data = argv[++i];
      else if (arg == "--hub" && i + 1 < argc)
        hub = argv[++i];
      else if (arg == "--config" && i + 1 < argc)
        config_path = argv[++i];
      else if (arg == "--socket" && i + 1 < argc)
        socket_path = argv[++i];
      else
        throw std::runtime_error(
            "usage: echotrace-edge --data DIR --hub ws://HOST:18081/ws/device "
            "[--config FILE] [--socket PATH] [--simulated]");
    }
    signal(SIGINT, signal_stop);
    signal(SIGTERM, signal_stop);
    signal(SIGPIPE, SIG_IGN);
    auto config = config_path.empty() ? Json::Value(Json::objectValue)
                                      : load_json(config_path);
    std::string token =
        getenv("ECHOTRACE_TOKEN") ? getenv("ECHOTRACE_TOKEN") : "";
    if (config.isMember("token_file")) {
      std::ifstream file(config["token_file"].asString());
      std::getline(file, token);
    }
    Edge edge(fs::absolute(data), hub, token, simulated, config);
    ControlSocket control(socket_path, edge);
    std::cout << "EchoTrace capture daemon; synthetic=" << simulated
              << " control=" << socket_path << "\n";
    edge.run();
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "EchoTrace: " << e.what() << "\n";
    return 1;
  }
}
