#include "EvidencePlayback.h"
#include <QCoreApplication>
#include <QDateTime>
#include <QElapsedTimer>
#include <QFile>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QThread>
#include <iostream>
#include <stdexcept>

void require(bool condition, const char *message) {
  if (!condition) throw std::runtime_error(message);
}
void until(std::function<bool()> predicate, int timeoutMs = 6000) {
  QElapsedTimer elapsed;
  elapsed.start();
  while (!predicate()) {
    QCoreApplication::processEvents(QEventLoop::AllEvents, 20);
    if (elapsed.elapsed() > timeoutMs) throw std::runtime_error("playback condition timed out");
    QThread::msleep(5);
  }
}
void settle(int milliseconds) {
  QElapsedTimer elapsed;
  elapsed.start();
  until([&] { return elapsed.elapsed() >= milliseconds; }, milliseconds + 1000);
}

int main(int argc, char **argv) {
  QGuiApplication application(argc, argv);
  if (argc != 3) return 2;
  try {
    QFile file(argv[1]);
    require(file.open(QIODevice::ReadOnly), "fixture plan could not be opened");
    const auto fixtures = QJsonDocument::fromJson(file.readAll()).object();
    QString segment, state, error;
    QImage image;
    int frames = 0;
    int firstSegmentFrames = -1;
    QJsonArray seen;
    EvidencePlayback playback(argv[2], "playback-test-token");
    QObject::connect(&playback, &EvidencePlayback::segmentChanged, &application, [&](QString id) { segment = id; seen.append(id); if (id == "video-2" && firstSegmentFrames < 0) firstSegmentFrames = frames; });
    QObject::connect(&playback, &EvidencePlayback::frameReady, &application, [&](QImage frame) { if (!frame.isNull()) { image = frame; ++frames; } });
    QObject::connect(&playback, &EvidencePlayback::stateChanged, &application, [&](QString value) { state = value; });
    QObject::connect(&playback, &EvidencePlayback::problem, &application, [&](QString value) { error = value; });
    playback.setPlan(fixtures["continuous"].toObject());
    playback.setAudioEnabled(true);
    playback.play();
    until([&] { return segment == "video-2"; });
    require(firstSegmentFrames == 50, "delayed B-frames were dropped at the segment boundary");
    require(error.isEmpty(), "continuous playback failed");
    playback.pause();
    settle(100);
    const auto paused = playback.position();
    settle(220);
    require(qAbs(playback.position() - paused) <= 80, "paused video clock continued advancing");
    const auto beforeSeek = frames;
    playback.seek(4500);
    until([&] { return segment == "video-3" && frames > beforeSeek; });
    require(!playback.isPlaying(), "paused cross-segment seek started playback");
    require(qAbs(playback.position() - 4500) <= 80, "seek did not preserve the source time");
    const auto blue = image.pixelColor(image.width()/2, image.height()/2);
    require(blue.blue() > 180 && blue.red() < 60, "seek displayed a stale frame from another segment");
    playback.play();
    until([&] { return state == "ended"; });
    require(playback.position() == 6000, "evidence end did not clamp the source clock");
    const auto beforeReverse = frames;
    playback.seek(250);
    until([&] { return segment == "video-1" && frames > beforeReverse; });
    require(qAbs(playback.position() - 250) <= 80, "reverse seek was shown from the earlier keyframe");
    require(image.pixelColor(image.width()/2, image.height()/2).red() > 180, "reverse seek decoded the wrong segment");

    error.clear();
    playback.setPlan(fixtures["gap"].toObject());
    playback.play();
    until([&] { return !error.isEmpty(); });
    require(error.contains("中断"), "recording gap was not reported");
    require(!playback.isPlaying() && segment == "video-2", "recording gap was skipped silently");
    require(playback.position() == 4000, "recording gap compressed the source timeline");
    const auto beforeGapSeek = frames;
    playback.seek(5000);
    until([&] { return segment == "video-3" && frames > beforeGapSeek; });
    require(qAbs(playback.position() - 5000) <= 80, "explicit seek after a gap failed");

    error.clear();
    playback.setPlan(fixtures["uncached"].toObject());
    playback.play();
    until([&] { return !error.isEmpty(); });
    require(error.contains("尚未缓存"), "uncached interval was not reported");
    require(!playback.isPlaying() && segment != "video-3", "uncached segment was skipped silently");

    error.clear();
    playback.setPlan(fixtures["continuous"].toObject());
    playback.play();
    playback.seek(4500);
    until([&] { return segment == "video-3" && playback.position() >= 4500; });
    playback.pause();
    settle(400);
    require(segment == "video-3", "late download replaced a newer seek");
    playback.stop();
    require(error.isEmpty(), "rapid seek failed");
    error.clear();
    playback.setPlan(fixtures["shifted"].toObject());
    const auto beforeShifted = frames;
    playback.seek(500);
    until([&] { return frames > beforeShifted; });
    require(qAbs(playback.position() - 500) <= 80, "nonzero file PTS changed the source seek position");
    require(error.isEmpty(), "nonzero file PTS was incorrectly treated as clip duration");
    error.clear();
    playback.setPlan(fixtures["bad_clock"].toObject());
    playback.play();
    until([&] { return !error.isEmpty(); });
    require(error.contains("时长"), "inconsistent media clock was played as source evidence");
    require(!playback.isPlaying(), "inconsistent source duration did not stop playback");
    playback.stop();
    error.clear();
    playback.setPlan(fixtures["audio_only"].toObject());
    playback.play();
    until([&] { return segment == "audio-2"; });
    playback.pause();
    playback.seek(4500);
    require(!playback.isPlaying() && playback.position() == 4500, "audio-only paused seek failed");
    playback.play();
    until([&] { return state == "ended"; });
    require(playback.position() == 6000 && error.isEmpty(), "audio-only playback failed");
    playback.stop();
    const QJsonObject report{{"synthetic_media", true}, {"continuous_transition", true},
      {"paused_seek", true}, {"reverse_seek", true}, {"gap_stops", true},
      {"uncached_stops", true}, {"stale_download_ignored", true},
      {"nonzero_pts_seek", true}, {"invalid_duration_rejected", true},
      {"b_frames_drained", true}, {"first_segment_frames", firstSegmentFrames},
      {"audio_only_playback", true},
      {"decoded_frames", frames}, {"segments", seen}};
    std::cout << QJsonDocument(report).toJson(QJsonDocument::Compact).constData() << std::endl;
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << std::endl;
    return 1;
  }
}
