#include "echotrace/capture.hpp"
#include "echotrace/inputs.hpp"
#include "recording.h"
#include <algorithm>
#include <alsa/asoundlib.h>
#include <array>
#include <chrono>
#include <cmath>
#include <cstring>
#include <fstream>
#include <iostream>
#include <thread>
extern "C" {
#include "rk_mpi_mb.h"
#include "rk_mpi_sys.h"
#include "rk_mpi_venc.h"
#include "rk_mpi_vi.h"
#include "rtsp_demo.h"
#include "sample_comm_isp.h"
}

namespace fs = std::filesystem;
namespace echotrace {
namespace {
class RockchipCapture final : public Capture {
public:
  RockchipCapture(CaptureConfig config, MediaCallback media,
                  EventCallback event)
      : config_(std::move(config)), media_(std::move(media)),
        event_(std::move(event)) {}
  ~RockchipCapture() { stop(); }
  void start(const std::string &session) override {
    stop();
    directory_ = config_.root / "sessions" / session;
    fs::create_directories(directory_ / "video");
    if (fs::space(directory_).available < config_.reserve_bytes)
      throw std::runtime_error("storage reserve reached");
    {
      std::lock_guard<std::mutex> lock(error_mutex_);
      error_.clear();
    }
    paused_ = false;
    native_offset_ = 0;
    clock_set_ = false;
    try {
      // The owner service is launched instead of rkipc by the board overlay.
      if (SAMPLE_COMM_ISP_Init(0, RK_AIQ_WORKING_MODE_NORMAL, RK_FALSE,
                               config_.iq.c_str()))
        throw std::runtime_error("ISP init failed");
      isp_ = true;
      if (SAMPLE_COMM_ISP_Run(0))
        throw std::runtime_error("ISP run failed");
      if (RK_MPI_SYS_Init() != RK_SUCCESS)
        throw std::runtime_error("MPI init failed");
      system_ = true;
      VI_DEV_ATTR_S device{};
      const int configured = RK_MPI_VI_GetDevAttr(0, &device);
      if (configured == RK_ERR_VI_NOT_CONFIG &&
          RK_MPI_VI_SetDevAttr(0, &device) != RK_SUCCESS)
        throw std::runtime_error("VI device configuration failed");
      if (RK_MPI_VI_GetDevIsEnable(0) != RK_SUCCESS) {
        if (RK_MPI_VI_EnableDev(0) != RK_SUCCESS)
          throw std::runtime_error("VI enable failed");
        VI_DEV_BIND_PIPE_S pipes{};
        pipes.u32Num = 1;
        pipes.PipeId[0] = 0;
        if (RK_MPI_VI_SetDevBindPipe(0, &pipes) != RK_SUCCESS)
          throw std::runtime_error("VI pipe bind failed");
      }
      vi_device_ = true;
      setup_vi(0, 1920, 1080);
      vi_main_ = true;
      VENC_CHN_ATTR_S attr{};
      attr.stVencAttr.enType = RK_VIDEO_ID_AVC;
      attr.stVencAttr.enPixelFormat = RK_FMT_YUV420SP;
      attr.stVencAttr.u32Profile = 100;
      attr.stVencAttr.u32PicWidth = 1920;
      attr.stVencAttr.u32PicHeight = 1080;
      attr.stVencAttr.u32VirWidth = 1920;
      attr.stVencAttr.u32VirHeight = 1080;
      attr.stVencAttr.u32StreamBufCnt = 2;
      attr.stVencAttr.u32BufSize = 1920 * 1080 * 3 / 2;
      attr.stRcAttr.enRcMode = VENC_RC_MODE_H264CBR;
      auto &rc = attr.stRcAttr.stH264Cbr;
      rc.u32BitRate = 8192;
      rc.u32Gop = 25;
      rc.u32SrcFrameRateNum = 25;
      rc.u32SrcFrameRateDen = 1;
      rc.fr32DstFrameRateNum = 25;
      rc.fr32DstFrameRateDen = 1;
      if (RK_MPI_VENC_CreateChn(0, &attr) != RK_SUCCESS)
        throw std::runtime_error("NV12 VENC create failed");
      venc_ = true;
      VENC_RECV_PIC_PARAM_S receive{};
      receive.s32RecvPicNum = -1;
      if (RK_MPI_VENC_StartRecvFrame(0, &receive) != RK_SUCCESS)
        throw std::runtime_error("VENC start failed");
      MPP_CHN_S input{RK_ID_VI, 0, 0}, output{RK_ID_VENC, 0, 0};
      if (RK_MPI_SYS_Bind(&input, &output) != RK_SUCCESS)
        throw std::runtime_error("VI to VENC bind failed");
      bound_ = true;
      const auto video_root = (directory_ / "video").string();
      setenv("IRC_RECORD_ROOT", video_root.c_str(), 1);
      setenv("IRC_RECORD_SEGMENT_SECONDS", "10", 1);
      setenv("IRC_RECORD_SYNC_SECONDS", "1", 1);
      const auto reserve =
          std::to_string(config_.reserve_bytes / (1024 * 1024));
      setenv("IRC_RECORD_MIN_FREE_MB", reserve.c_str(), 1);
      recording_ = std::make_unique<Recording>(1920, 1080);
      recording_->setSegmentCallback(
          [this](const std::string &path, uint64_t begin, uint64_t end) {
            guard([&] {
              media_(path, "video", begin, end,
                     video_discontinuity_.exchange(false));
            });
          });
      if (!recording_->start())
        throw std::runtime_error("recording start failed");
      rtsp_ = create_rtsp_demo(554);
      if (rtsp_) {
        rtsp_session_ = rtsp_new_session(rtsp_, "/live/0");
        if (rtsp_session_) {
          rtsp_set_video(rtsp_session_, RTSP_CODEC_ID_VIDEO_H264, nullptr, 0);
          rtsp_sync_video_ts(rtsp_session_, rtsp_get_reltime(),
                             rtsp_get_ntptime());
        }
      }
      int ret = snd_pcm_open(&pcm_, config_.alsa.c_str(),
                             SND_PCM_STREAM_CAPTURE, SND_PCM_NONBLOCK);
      if (ret < 0)
        throw std::runtime_error(std::string("ALSA open: ") +
                                 snd_strerror(ret));
      if ((ret = snd_pcm_set_params(
               pcm_, SND_PCM_FORMAT_S16_LE, SND_PCM_ACCESS_RW_INTERLEAVED,
               config_.capture_channels, 16000, 1, config_.capture_latency_us)) < 0)
        throw std::runtime_error(std::string("ALSA params: ") +
                                 snd_strerror(ret));
      snd_pcm_sw_params_t *sw;
      snd_pcm_sw_params_alloca(&sw);
      snd_pcm_sw_params_current(pcm_, sw);
      snd_pcm_sw_params_set_tstamp_mode(pcm_, sw, SND_PCM_TSTAMP_ENABLE);
      snd_pcm_sw_params_set_tstamp_type(pcm_, sw,
                                        SND_PCM_TSTAMP_TYPE_MONOTONIC);
      if (snd_pcm_sw_params(pcm_, sw) < 0)
        throw std::runtime_error(
            "ALSA monotonic timestamp configuration failed");
      snd_pcm_start(pcm_);
      running_ = true;
      video_thread_ = std::thread([this] { guard([this] { video_loop(); }); });
      audio_thread_ = std::thread([this] { guard([this] { audio_loop(); }); });
      try {
        setup_vi(1, 320, 180);
        vi_sub_ = true;
        scene_thread_ =
            std::thread([this] { guard([this] { scene_loop(); }); });
      } catch (const std::exception &e) {
        std::cerr << "scene channel unavailable: " << e.what()
                  << "; periodic source anchors remain enabled\n";
      }
      inputs_ = std::make_unique<LinuxInputs>(
          config_, event_, [this](bool value) { pause(value); });
    } catch (...) {
      stop();
      throw;
    }
  }
  void pause(bool value) override {
    const bool previous = paused_.exchange(value);
    if (previous && !value)
      video_discontinuity_ = true;
  }
  void stop() override {
    running_ = false;
    inputs_.reset();
    stop_playback();
    for (auto *thread : {&video_thread_, &audio_thread_, &scene_thread_})
      if (thread->joinable())
        thread->join();
    if (recording_) {
      recording_->stop();
      recording_.reset();
    }
    if (pcm_) {
      snd_pcm_close(pcm_);
      pcm_ = nullptr;
    }
    if (bound_) {
      MPP_CHN_S input{RK_ID_VI, 0, 0}, output{RK_ID_VENC, 0, 0};
      RK_MPI_SYS_UnBind(&input, &output);
      bound_ = false;
    }
    if (venc_) {
      RK_MPI_VENC_StopRecvFrame(0);
      RK_MPI_VENC_DestroyChn(0);
      venc_ = false;
    }
    if (vi_sub_) {
      RK_MPI_VI_DisableChn(0, 1);
      vi_sub_ = false;
    }
    if (vi_main_) {
      RK_MPI_VI_DisableChn(0, 0);
      vi_main_ = false;
    }
    if (vi_device_) {
      RK_MPI_VI_DisableDev(0);
      vi_device_ = false;
    }
    if (rtsp_) {
      rtsp_del_demo(rtsp_);
      rtsp_ = nullptr;
      rtsp_session_ = nullptr;
    }
    if (system_) {
      RK_MPI_SYS_Exit();
      system_ = false;
    }
    if (isp_) {
      SAMPLE_COMM_ISP_Stop(0);
      isp_ = false;
    }
  }
  std::string error() const override {
    std::lock_guard<std::mutex> lock(error_mutex_);
    return error_;
  }
  void play(const fs::path &path, double offset) override {
    stop_playback();
    if (offset < 0 || !std::isfinite(offset))
      throw std::runtime_error("invalid playback offset");
    playing_ = true;
    playback_ = std::thread([this, path, offset] {
      snd_pcm_t *output = nullptr;
      try {
        std::ifstream source(path, std::ios::binary);
        char header[44];
        source.read(header, 44);
        if (source.gcount() != 44 || std::memcmp(header, "RIFF", 4) ||
            std::memcmp(header + 8, "WAVE", 4))
          throw std::runtime_error("playback requires an owned PCM WAV");
        source.seekg(44 + std::streamoff(offset * 16000) * 2);
        if (snd_pcm_open(&output,
                         (config_.alsa.rfind("hw:", 0) == 0
                              ? "plug" + config_.alsa
                              : config_.alsa)
                             .c_str(),
                         SND_PCM_STREAM_PLAYBACK, 0) < 0)
          throw std::runtime_error("ALSA playback open failed");
        if (snd_pcm_set_params(output, SND_PCM_FORMAT_S16_LE,
                               SND_PCM_ACCESS_RW_INTERLEAVED, 1, 16000, 1,
                               80000) < 0)
          throw std::runtime_error("ALSA playback params failed");
        Json::Value data;
        data["active"] = true;
        event_("playback", monotonic_us(), "开始回放，暂停建立语音笔记", data);
        std::array<int16_t, 640> buffer{};
        while (playing_ && source) {
          source.read(reinterpret_cast<char *>(buffer.data()), sizeof(buffer));
          snd_pcm_sframes_t frames = source.gcount() / 2;
          size_t at = 0;
          while (playing_ && frames > 0) {
            auto written = snd_pcm_writei(output, buffer.data() + at, frames);
            if (written < 0) {
              if (snd_pcm_recover(output, int(written), 1) < 0)
                throw std::runtime_error("ALSA playback recovery failed");
              continue;
            }
            at += size_t(written);
            frames -= written;
          }
        }
        if (playing_)
          snd_pcm_drain(output);
      } catch (const std::exception &e) {
        std::cerr << e.what() << "\n";
      }
      if (output)
        snd_pcm_close(output);
      playing_ = false;
      Json::Value data;
      data["active"] = false;
      event_("playback", monotonic_us(), "回放结束", data);
    });
  }
  bool playback_active() const override { return playing_.load(); }
  void stop_playback() override {
    playing_ = false;
    if (playback_.joinable())
      playback_.join();
  }

private:
  void setup_vi(int channel, int width, int height) {
    VI_CHN_ATTR_S attr{};
    attr.stIspOpt.u32BufCount = 2;
    attr.stIspOpt.enMemoryType = VI_V4L2_MEMORY_TYPE_DMABUF;
    attr.stSize.u32Width = width;
    attr.stSize.u32Height = height;
    attr.enPixelFormat = RK_FMT_YUV420SP;
    attr.enCompressMode = COMPRESS_MODE_NONE;
    attr.u32Depth = channel ? 1 : 0;
    attr.stFrameRate.s32SrcFrameRate = -1;
    attr.stFrameRate.s32DstFrameRate = -1;
    if (RK_MPI_VI_SetChnAttr(0, channel, &attr) != RK_SUCCESS ||
        RK_MPI_VI_EnableChn(0, channel) != RK_SUCCESS)
      throw std::runtime_error("VI channel setup failed");
  }
  template <class F> void guard(F function) {
    try {
      function();
    } catch (const std::exception &e) {
      std::lock_guard<std::mutex> lock(error_mutex_);
      error_ = e.what();
    }
  }
  uint64_t video_clock(uint64_t native) {
    const auto now = monotonic_us();
    std::lock_guard<std::mutex> lock(clock_mutex_);
    if (!clock_set_) {
      const int64_t difference = int64_t(now) - int64_t(native);
      native_offset_ = std::abs(difference) > 5000000 ? difference : 0;
      clock_set_ = true;
      Json::Value info;
      info["native_offset_us"] = Json::Int64(native_offset_);
      info["alignment_requires_hardware_validation"] = true;
      event_("gap", now, "视频时间源初始化，精度需声画同步实验验证", info);
    }
    return uint64_t(std::max<int64_t>(0, int64_t(native) + native_offset_));
  }
  void video_loop() {
    uint64_t last_anchor = 0, last_space = 0, last_frame = monotonic_us();
    bool previously_paused = false;
    while (running_) {
      VENC_PACK_S pack{};
      VENC_STREAM_S stream{};
      stream.pstPack = &pack;
      if (RK_MPI_VENC_GetStream(0, &stream, 200) != RK_SUCCESS) {
        if (monotonic_us() - last_frame > 3000000)
          throw std::runtime_error("camera stream timeout");
        continue;
      }
      last_frame = monotonic_us();
      const auto stamp = video_clock(pack.u64PTS);
      auto *bytes =
          static_cast<unsigned char *>(RK_MPI_MB_Handle2VirAddr(pack.pMbBlk));
      if (paused_) {
        if (!previously_paused)
          recording_->stop();
        previously_paused = true;
      } else {
        if (previously_paused) {
          if (!recording_->start())
            throw std::runtime_error("recording resume failed");
          previously_paused = false;
        }
        if (bytes && pack.u32Len) {
          recording_->enqueue(bytes, pack.u32Len, stamp, false);
          if (rtsp_ && rtsp_session_) {
            rtsp_tx_video(rtsp_session_, bytes, pack.u32Len, stamp);
            rtsp_do_event(rtsp_);
          }
        }
        if (stamp - last_anchor >= 30000000) {
          event_("scene", stamp, "", Json::objectValue);
          last_anchor = stamp;
        }
      }
      RK_MPI_VENC_ReleaseStream(0, &stream);
      if (stamp - last_space > 1000000) {
        last_space = stamp;
        if (fs::space(directory_).available < config_.reserve_bytes)
          throw std::runtime_error(
              "storage reserve reached; stopping recording");
      }
    }
  }
  void audio_loop() {
    WaveWriter wav;
    uint64_t begin = 0, last_source_end = 0, last_sample = monotonic_us();
    bool had_xrun = false, had_playback = false;
    fs::path part;
    std::array<int16_t, 640> samples{};
    std::vector<int16_t> interleaved(640 * config_.capture_channels);
    if (config_.capture_channels < 1 || config_.capture_channels > 4 ||
        config_.microphone_channel >= config_.capture_channels)
      throw std::runtime_error("invalid hardware audio channel selection");
    snd_pcm_status_t *status;
    snd_pcm_status_alloca(&status);
    auto finish = [&] {
      if (!begin || !wav.samples())
        return;
      const auto end = begin + wav.samples() * 1000000 / 16000;
      // The owner commits data + directory before publishing its manifest.
      // Keep this capture thread available to drain ALSA during slow fsync.
      const auto closing = monotonic_us();
      wav.close(false);
      const auto elapsed = monotonic_us() - closing;
      if (elapsed > 100000)
        std::cerr << "audio close latency_us=" << elapsed << "\n";
      auto path = part;
      path.replace_extension(".wav");
      media_(path, "audio", begin, end, had_xrun || had_playback);
      last_source_end = end;
      begin = 0;
      had_xrun = false;
      had_playback = false;
    };
    while (running_) {
      auto count = snd_pcm_readi(pcm_, interleaved.data(), samples.size());
      if (count == -EAGAIN) {
        if (monotonic_us() - last_sample > 3000000)
          throw std::runtime_error("audio capture timeout");
        snd_pcm_wait(pcm_, 100);
        continue;
      }
      if (count == -EINTR)
        continue;
      if (count < 0) {
        finish();
        had_xrun = true;
        Json::Value failure;
        failure["alsa_error"] = int(count);
        failure["last_success_us"] = Json::UInt64(last_sample);
        event_("gap", monotonic_us(), "音频 XRUN，开启新的时间连续片段",
               failure);
        if (snd_pcm_recover(pcm_, int(count), 1) < 0)
          throw std::runtime_error("ALSA XRUN recovery failed");
        snd_pcm_start(pcm_);
        continue;
      }
      if (!count)
        continue;
      last_sample = monotonic_us();
      if (snd_pcm_status(pcm_, status) < 0)
        throw std::runtime_error("ALSA status failed");
      snd_htimestamp_t stamp{};
      snd_pcm_status_get_htstamp(status, &stamp);
      const auto delay =
          std::max<snd_pcm_sframes_t>(0, snd_pcm_status_get_delay(status));
      const uint64_t native =
          uint64_t(stamp.tv_sec) * 1000000 + stamp.tv_nsec / 1000;
      const uint64_t latency =
          (uint64_t(delay) + uint64_t(count)) * 1000000 / 16000;
      const uint64_t first =
          native > latency ? native - latency : monotonic_us();
      if (paused_) {
        finish();
        continue;
      }
      if (!begin) {
        begin = std::max(first, last_source_end);
        part = directory_ / ("audio-" + std::to_string(begin) + ".part");
        wav.open(part);
      }
      if (playing_)
        had_playback = true;
      for (size_t i = 0; i < size_t(count); ++i)
        samples[i] = interleaved[i * config_.capture_channels +
                                 config_.microphone_channel];
      wav.write(samples.data(), size_t(count));
      if (wav.samples() >= 160000)
        finish();
    }
    finish();
  }
  void scene_loop() {
    std::vector<unsigned char> previous;
    double old_mean = 0;
    bool changed = false;
    uint64_t stable_since = 0, last_event = 0, best_stamp = 0;
    double best_sharpness = 0;
    while (running_) {
      std::this_thread::sleep_for(std::chrono::milliseconds(500));
      if (paused_)
        continue;
      VIDEO_FRAME_INFO_S frame{};
      if (RK_MPI_VI_GetChnFrame(0, 1, &frame, 200) != RK_SUCCESS)
        continue;
      const auto stamp = video_clock(frame.stVFrame.u64PTS);
      auto *y = static_cast<unsigned char *>(
          RK_MPI_MB_Handle2VirAddr(frame.stVFrame.pMbBlk));
      const int stride = std::max(320, int(frame.stVFrame.u32VirWidth));
      std::vector<unsigned char> image(320 * 180);
      if (y)
        for (int row = 0; row < 180; ++row)
          std::memcpy(image.data() + row * 320, y + row * stride, 320);
      RK_MPI_VI_ReleaseChnFrame(0, 1, &frame);
      if (!y)
        continue;
      double mean = 0;
      for (auto p : image)
        mean += p;
      mean /= image.size();
      if (previous.empty()) {
        previous = std::move(image);
        old_mean = mean;
        continue;
      }
      double difference = 0, sharpness = 0;
      for (size_t i = 1; i < image.size(); i += 2) {
        difference += std::abs((double(image[i]) - mean) -
                               (double(previous[i]) - old_mean)) > 20;
        sharpness += std::abs(int(image[i]) - int(image[i - 1]));
      }
      difference /= image.size() / 2;
      previous = std::move(image);
      old_mean = mean;
      if (difference > .06) {
        changed = true;
        stable_since = 0;
        best_sharpness = 0;
      } else if (changed) {
        if (!stable_since)
          stable_since = stamp;
        if (sharpness > best_sharpness) {
          best_sharpness = sharpness;
          best_stamp = stamp;
        }
        if (stamp - stable_since >= 1000000 && stamp - last_event >= 10000000) {
          event_("scene", best_stamp, "", Json::objectValue);
          last_event = stamp;
          changed = false;
        }
      }
    }
  }
  CaptureConfig config_;
  MediaCallback media_;
  EventCallback event_;
  fs::path directory_;
  std::unique_ptr<Recording> recording_;
  std::unique_ptr<LinuxInputs> inputs_;
  snd_pcm_t *pcm_ = nullptr;
  std::atomic<bool> running_{false}, paused_{false}, playing_{false},
      video_discontinuity_{true};
  std::thread video_thread_, audio_thread_, scene_thread_, playback_;
  mutable std::mutex error_mutex_, clock_mutex_;
  std::string error_;
  int64_t native_offset_ = 0;
  bool clock_set_ = false;
  bool isp_ = false, system_ = false, vi_device_ = false, vi_main_ = false,
       vi_sub_ = false, venc_ = false, bound_ = false;
  rtsp_demo_handle rtsp_ = nullptr;
  rtsp_session_handle rtsp_session_ = nullptr;
};
} // namespace
std::unique_ptr<Capture> make_rv_capture(const CaptureConfig &config,
                                         MediaCallback media,
                                         EventCallback event) {
  return std::make_unique<RockchipCapture>(config, std::move(media),
                                           std::move(event));
}
} // namespace echotrace
