#!/usr/bin/env python3
"""Copy a small, content-locked reuse set. Never edit the source projects."""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(
            "upstream source changed; review adapter before proceeding: " + old[:100]
        )
    return text.replace(old, new, 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--security", type=Path, default=ROOT.parent / "代码")
    parser.add_argument(
        "--echo", type=Path, default=Path.home() / "Documents/echo-mate/Echo-Mate"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "build/vendor")
    args = parser.parse_args()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    records = []

    def take(path, target, transform=lambda text: text):
        original = path.read_bytes()
        text = transform(original.decode("utf-8").replace("\r\n", "\n"))
        output = destination / target
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        records.append(
            {
                "source": str(path.resolve()),
                "target": target,
                "source_sha256": hashlib.sha256(original).hexdigest(),
                "adapted_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
        )

    rv = args.security / "RV1106"
    rk = args.security / "RK3576/src/model"

    def header(text):
        text = text.replace(
            "#include <deque>", "#include <deque>\n#include <functional>"
        )
        text = replace_once(
            text,
            "    void setPerformanceLogCallback",
            "    using SegmentCallback = std::function<void(const std::string&, uint64_t, uint64_t)>;\n    void setSegmentCallback(SegmentCallback value) { segment_callback_ = value; }\n    void setPerformanceLogCallback",
        )
        return replace_once(
            text,
            "    std::string root_path_;",
            "    SegmentCallback segment_callback_;\n    uint64_t source_first_us_ = 0, source_last_us_ = 0;\n    std::string root_path_;",
        )

    def recording(text):
        text = text.replace(
            '#include "../../../3rdparty/minimp4/minimp4.h"',
            '#include "minimp4.h"\n#include <climits>\n#ifdef __APPLE__\n#define fdatasync fsync\n#endif',
        )
        text = replace_once(
            text,
            "    segment_start_ms_ = packet.wall_ms;",
            "    source_first_us_ = source_last_us_ = packet.pts;\n    segment_start_ms_ = packet.wall_ms;",
        )
        text = replace_once(
            text,
            "    last_pts_ = packet.pts;",
            "    source_last_us_ = packet.pts;\n    last_pts_ = packet.pts;",
        )
        text = replace_once(
            text,
            "packet.wall_ms - segment_start_ms_ >= segment_seconds_ * 1000",
            "packet.pts >= source_first_us_ && packet.pts - source_first_us_ >= segment_seconds_ * 1000000",
        )
        old = """        if (rename(temporary_path_.c_str(), final_path.c_str()) == 0)
            printf("[Recording] 已保存 %s (%llu bytes)\\n", final_path.c_str(),
                   (unsigned long long)segment_bytes_);
        else perror("[Recording] 录像重命名失败");"""
        new = """        if (rename(temporary_path_.c_str(), final_path.c_str()) == 0) {
            if (segment_callback_) segment_callback_(final_path, source_first_us_,
                source_last_us_ + uint64_t(last_duration_) * 1000 / 90);
        } else perror("[Recording] 录像重命名失败");"""
        text = replace_once(text, old, new)
        start = text.index("bool Recording::maintainStorage(")
        end = text.index("bool Recording::openSegment(", start)
        text = (
            text[:start]
            + """bool Recording::maintainStorage(uint64_t, size_t incoming) {
    struct statvfs info;
    if (statvfs(directory_.c_str(), &info) != 0) return false;
    const uint64_t free_bytes = uint64_t(info.f_bavail) * info.f_frsize;
    return free_bytes >= min_free_bytes_ + incoming;
}

"""
            + text[end:]
        )
        # Keep clock anchoring across .part recovery. The external session manifest
        # intentionally marks interrupted, unmanifested fragments as unavailable.
        return (
            text.replace("last_duration_(3000)", "last_duration_(3600)")
            .replace("last_duration_ = 3000", "last_duration_ = 3600")
            .replace("track.default_duration = 3000", "track.default_duration = 3600")
        )

    take(rv / "code/modules/video/recording.h", "recording/recording.h", header)
    take(rv / "code/modules/video/recording.cc", "recording/recording.cc", recording)
    take(
        rv / "code/modules/video/mp4_mux.c",
        "recording/mp4_mux.c",
        lambda s: s.replace("../../../3rdparty/minimp4/minimp4.h", "minimp4.h"),
    )
    take(rv / "3rdparty/minimp4/minimp4.h", "recording/minimp4.h")

    for filename in (
        "IVideoDecoder.h",
        "FFmpegDecoder.h",
        "FFmpegDecoder.cpp",
        "RecordingPlayer.h",
        "RecordingPlayer.cpp",
        "MppDecoder.h",
        "MppDecoder.cpp",
    ):

        def decoder(text, filename=filename):
            if filename == "RecordingPlayer.cpp":
                text = replace_once(
                    text,
                    '        if (readRet < 0) {\n            if (readRet == AVERROR_EOF)\n                finishedNaturally = true;\n            else\n                qWarning() << "RecordingPlayer: 读取失败" << avErrorText(readRet);\n            break;\n        }',
                    '        if (readRet < 0 && readRet != AVERROR_EOF) {\n            qWarning() << "RecordingPlayer: 读取失败" << avErrorText(readRet);\n            break;\n        }',
                )
                text = replace_once(
                    text,
                    "        if (pkt->stream_index != videoIndex) {",
                    "        if (readRet >= 0 && pkt->stream_index != videoIndex) {",
                )
                text = replace_once(
                    text,
                    "        if (decoder->sendPacket(pkt)) {",
                    "        if (decoder->sendPacket(readRet == AVERROR_EOF ? nullptr : pkt)) {",
                )
                text = replace_once(
                    text,
                    "        } else {\n            av_packet_unref(pkt);\n        }\n    }\n\n    av_packet_free(&pkt);",
                    "        } else {\n            av_packet_unref(pkt);\n        }\n        if (readRet == AVERROR_EOF) {\n            finishedNaturally = true;\n            break;\n        }\n    }\n\n    av_packet_free(&pkt);",
                )
                text = replace_once(
                    text,
                    '#include "RecordingPlayer.h"',
                    '#include "RecordingPlayer.h"\n#include <chrono>',
                )
                text = replace_once(
                    text,
                    "namespace {",
                    "namespace {\nqint64 playback_clock_ms() {\n    return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();\n}\n",
                )
                text = text.replace(
                    "QDateTime::currentMSecsSinceEpoch()", "playback_clock_ms()"
                )
                text = replace_once(
                    text,
                    "    m_seekMs = ms;\n    m_hasSeek = true;",
                    "    m_seekMs = ms;\n    m_hasSeek = true;\n    m_forceFrame = true;",
                )
                text = replace_once(
                    text,
                    "    bool firstFrameSeen = false;",
                    "    qint64 discardBeforeMs = -1;",
                )
                text = replace_once(
                    text,
                    "    m_durationMs = durationMs;",
                    "    m_firstPts = fmtCtx->streams[videoIndex]->start_time == AV_NOPTS_VALUE ? 0 : fmtCtx->streams[videoIndex]->start_time;\n    m_durationMs = durationMs;",
                )
                text = replace_once(
                    text,
                    "                needResync = true;\n                QMutexLocker locker(&m_mutex);",
                    "                needResync = true;\n                discardBeforeMs = seekTarget;\n                QMutexLocker locker(&m_mutex);",
                )
                text = replace_once(
                    text,
                    "                if (!firstFrameSeen) {\n                    m_firstPts = rawPts >= 0 ? rawPts : 0;\n                    firstFrameSeen = true;\n                }",
                    "",
                )
                text = replace_once(
                    text,
                    "                const qint64 nowMs = playback_clock_ms();",
                    "                if (mediaMs < discardBeforeMs) continue;\n                discardBeforeMs = -1;\n                const qint64 nowMs = playback_clock_ms();",
                )
                text = replace_once(
                    text,
                    "    qint64 durationMs = 0;\n    if (fmtCtx->duration > 0) {",
                    "    qint64 durationMs = 0;\n    if (fmtCtx->streams[videoIndex]->duration > 0) {\n        durationMs = av_rescale_q(fmtCtx->streams[videoIndex]->duration, fmtCtx->streams[videoIndex]->time_base, kMsTimeBase);\n    } else if (fmtCtx->duration > 0) {",
                )
                text = replace_once(
                    text,
                    "    } else if (fmtCtx->streams[videoIndex]->duration > 0) {\n        durationMs = av_rescale_q(fmtCtx->streams[videoIndex]->duration,\n                                  fmtCtx->streams[videoIndex]->time_base, kMsTimeBase);\n    }",
                    "    }",
                )
                text = replace_once(
                    text,
                    "        durationMs = fmtCtx->duration / (AV_TIME_BASE / 1000);",
                    "        durationMs = (fmtCtx->duration - (fmtCtx->start_time > 0 ? fmtCtx->start_time : 0)) / (AV_TIME_BASE / 1000);",
                )
            if filename == "MppDecoder.h":
                text = text.replace(
                    "    void cleanup() override;",
                    "    void cleanup() override;\n    qint64 lastFramePts() const override { return m_lastFramePts; }\n    bool usingRga() const { return m_useRGA; }",
                )
                text = text.replace(
                    "    bool m_initialized;",
                    "    qint64 m_lastFramePts = -1;\n    bool m_initialized;",
                )
            if filename == "MppDecoder.cpp":
                text = text.replace('#include "model.h"', '#include "FramePolicy.h"')
                text = text.replace(
                    "Model::s_currentGridCount", "FramePolicy::s_currentGridCount"
                )
                # Keep visible geometry separate from aligned storage strides.
                import re

                text = re.sub(
                    r"^\s*(?:m_outWidth|m_outHeight|newOutWidth|newOutHeight) = \(.*? \+ 15\) & ~15;\n",
                    "\n",
                    text,
                    flags=re.MULTILINE,
                )
                text = text.replace(
                    "m_rgbBufferSize = m_width * m_height * 3;",
                    "m_rgbBufferSize = ((m_width + 15) & ~15) * m_height * 3;",
                )
                text = text.replace(
                    "uint8_t* uvPlane = yPlane + yStride * m_height;",
                    "uint8_t* uvPlane = yPlane + yStride * mpp_frame_get_ver_stride(frame);",
                )
                text = text.replace(
                    "    // 释放帧\n    mpp_frame_deinit(&mppFrame);",
                    "    if (success) m_lastFramePts = mpp_frame_get_pts(mppFrame);\n    // 释放帧\n    mpp_frame_deinit(&mppFrame);",
                )
                # BUFFER_FULL means not accepted; callers must drain and retry.
                text = text.replace(
                    "    return true;\n}\n\nbool MppDecoder::receiveFrame",
                    "    return ret == MPP_OK;\n}\n\nbool MppDecoder::receiveFrame",
                )
                text = replace_once(
                    text,
                    "image = QImage(currentVirAddr, m_outWidth, m_outHeight, bytesPerLine, QImage::Format_RGB888);",
                    "image = QImage(currentVirAddr, m_outWidth, m_outHeight, bytesPerLine, QImage::Format_RGB888).copy();",
                )
            return text

        take(rk / filename, "decoder/" + filename, decoder)
    license_file = args.echo / "LICENSE"
    if license_file.exists():
        take(license_file, "licenses/Echo-Mate-LICENSE")
    lock = {
        "format": 1,
        "note": "Git objects in the source snapshots are incomplete; byte hashes are authoritative.",
        "sources": records,
    }
    (destination / "sources.lock.json").write_text(
        json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ROOT / "sources.lock.json").write_text(
        json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {"reused_files": len(records), "output": str(destination)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
