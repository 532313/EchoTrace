#include "MppDecoder.h"
#include <QCoreApplication>
#include <QImage>
#include <chrono>
#include <cmath>
#include <iostream>
#include <memory>
#include <thread>
extern "C" {
#include <libavcodec/bsf.h>
#include <libavformat/avformat.h>
}
int main(int argc, char **argv) {
  QCoreApplication application(argc, argv);
  AVFormatContext *format = nullptr;
  AVBSFContext *filter = nullptr;
  AVPacket *packet = av_packet_alloc();
  AVPacket *annex = av_packet_alloc();
  try {
    QString input, output;
    double seconds = -1;
    for (int i = 1; i + 1 < argc; ++i) {
      QString arg = argv[i];
      if (arg == "--input")
        input = argv[++i];
      else if (arg == "--output")
        output = argv[++i];
      else if (arg == "--seconds")
        seconds = QString(argv[++i]).toDouble();
      else
        throw std::runtime_error("unknown argument");
    }
    if (input.isEmpty() || output.isEmpty() || !std::isfinite(seconds) ||
        seconds < 0)
      throw std::runtime_error(
          "expected input, output and source-relative seconds");
    if (avformat_open_input(&format, input.toUtf8().constData(), nullptr,
                            nullptr) < 0 ||
        avformat_find_stream_info(format, nullptr) < 0)
      throw std::runtime_error("cannot open source media");
    int stream =
        av_find_best_stream(format, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0);
    if (stream < 0 ||
        format->streams[stream]->codecpar->codec_id != AV_CODEC_ID_H264)
      throw std::runtime_error("hardware extractor supports H.264 evidence");
    auto *vs = format->streams[stream];
    const auto start = vs->start_time == AV_NOPTS_VALUE ? 0 : vs->start_time;
    const auto target = start + av_rescale_q(int64_t(seconds * 1e6),
                                             AV_TIME_BASE_Q, vs->time_base);
    auto *bsf = av_bsf_get_by_name("h264_mp4toannexb");
    if (!bsf || av_bsf_alloc(bsf, &filter) < 0 ||
        avcodec_parameters_copy(filter->par_in, vs->codecpar) < 0)
      throw std::runtime_error("Annex B conversion unavailable");
    filter->time_base_in = vs->time_base;
    if (av_bsf_init(filter) < 0 ||
        av_seek_frame(format, stream, target, AVSEEK_FLAG_BACKWARD) < 0)
      throw std::runtime_error("source seek failed");
    MppDecoder decoder;
    if (!decoder.init(vs->codecpar))
      throw std::runtime_error("MPP init failed");
    QImage chosen;
    int64_t chosen_pts = -1;
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    auto drain = [&] {
      QImage frame;
      for (int i = 0; i < 32 && decoder.receiveFrame(frame); ++i) {
        auto pts = decoder.lastFramePts();
        if (pts >= target) {
          chosen = frame.copy();
          chosen_pts = pts;
          return true;
        }
      }
      return false;
    };
    while (chosen.isNull() && std::chrono::steady_clock::now() < deadline &&
           av_read_frame(format, packet) >= 0) {
      if (packet->stream_index != stream) {
        av_packet_unref(packet);
        continue;
      }
      if (av_bsf_send_packet(filter, packet) < 0)
        throw std::runtime_error("bitstream conversion failed");
      while (av_bsf_receive_packet(filter, annex) >= 0) {
        while (!decoder.sendPacket(annex)) {
          if (drain() || std::chrono::steady_clock::now() >= deadline)
            break;
          std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
        for (int i = 0; i < 3 && chosen.isNull(); ++i) {
          if (drain())
            break;
          std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
        av_packet_unref(annex);
        if (!chosen.isNull())
          break;
      }
      av_packet_unref(packet);
    }
    for (int i = 0; i < 100 && chosen.isNull() &&
                    std::chrono::steady_clock::now() < deadline;
         ++i) {
      drain();
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    if (chosen.isNull())
      throw std::runtime_error("no timestamp-matched hardware frame");
    if (!chosen.save(output, "JPEG", 94))
      throw std::runtime_error("JPEG write failed");
    std::cout << "{\"backend\":\""
              << (decoder.usingRga() ? "mpp-rga" : "mpp-cpu")
              << "\",\"relative_time_us\":"
              << av_rescale_q(chosen_pts - start, vs->time_base, AV_TIME_BASE_Q)
              << ",\"width\":" << chosen.width()
              << ",\"height\":" << chosen.height() << "}" << std::endl;
    av_packet_free(&packet);
    av_packet_free(&annex);
    av_bsf_free(&filter);
    avformat_close_input(&format);
    return 0;
  } catch (const std::exception &e) {
    av_packet_free(&packet);
    av_packet_free(&annex);
    av_bsf_free(&filter);
    if (format)
      avformat_close_input(&format);
    std::cerr << "echotrace-frame: " << e.what() << "\n";
    return 1;
  }
}
