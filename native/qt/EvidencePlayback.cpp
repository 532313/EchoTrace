#include "EvidencePlayback.h"
#include <QCryptographicHash>
#include <QEventLoop>
#include <QFile>
#include <QJsonDocument>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QSaveFile>
#include <QTimer>
#include <algorithm>

struct EvidencePlayback::Download {
  QSaveFile file;
  QCryptographicHash digest{QCryptographicHash::Sha256};
  QList<Cached> waiting;
  QNetworkReply *reply = nullptr;
  qint64 received = 0;
  QString error;
  explicit Download(QString path) : file(path) {}
};

EvidencePlayback::EvidencePlayback(QString endpoint, QString token, QObject *parent)
    : QObject(parent), endpoint_(endpoint), token_(token) {
  connect(&audioClock_, &QTimer::timeout, this, [this] {
    if (!audioOnly_ || !wanted_ || !audioElapsed_.isValid()) return;
    positionUs_ = qMin(endUs_, audioOriginUs_ + audioElapsed_.elapsed() * 1000);
    emit positionChanged(position());
    if (positionUs_ >= qMin(endUs_, qint64(current_["end_us"].toDouble()))) finishSegment();
  });
}

EvidencePlayback::~EvidencePlayback() {
  stop();
  if (audioBusy_ || !appliedAudio_.isEmpty()) {
    QEventLoop loop;
    QTimer poll;
    connect(&poll, &QTimer::timeout, &loop, [&] {
      if (!audioBusy_ && appliedAudio_.isEmpty()) loop.quit();
    });
    poll.start(20);
    QTimer::singleShot(1500, &loop, &QEventLoop::quit);
    loop.exec();
  }
  for (auto job : downloads_.values()) job->reply->abort();
}

QUrl EvidencePlayback::url(const QString &path) const {
  return QUrl(endpoint_ + "/").resolved(QUrl(path));
}

QJsonObject EvidencePlayback::at(const QJsonArray &clips, qint64 stamp) const {
  QJsonObject found;
  for (auto value : clips) {
    auto clip = value.toObject();
    if (clip["start_us"].toDouble() <= stamp && stamp < clip["end_us"].toDouble())
      found = clip;
  }
  return found;
}

void EvidencePlayback::haltPlayer() {
  ++generation_;
  audioClock_.stop();
  audioElapsed_.invalidate();
  audioOnly_ = false;
  for (auto connection : connections_) disconnect(connection);
  connections_.clear();
  player_.stopPlayback();
  player_.wait();
  loading_ = false;
}

void EvidencePlayback::stop() {
  wanted_ = false;
  haltPlayer();
  ++audioEpoch_;
  pumpAudio();
  emit stateChanged("paused");
}

void EvidencePlayback::setPlan(const QJsonObject &plan) {
  stop();
  for (auto job : downloads_.values()) job->reply->abort();
  for (auto path : cached_) QFile::remove(path);
  cached_.clear();
  videos_ = plan["video"].toArray();
  audios_ = plan["audio"].toArray();
  beginUs_ = qint64(plan["begin_us"].toDouble());
  endUs_ = qint64(plan["end_us"].toDouble());
  positionUs_ = qint64(plan["anchor_us"].toDouble());
  toleranceUs_ = qBound<qint64>(0, qint64(plan["continuity_tolerance_us"].toDouble()), 50000);
  current_ = {};
  emit rangeChanged(duration());
  emit positionChanged(position());
}

void EvidencePlayback::play() {
  if (endUs_ <= beginUs_) return;
  wanted_ = true;
  if (videos_.isEmpty()) {
    if (!audioEnabled_) { audioEnabled_ = true; emit audioEnabledChanged(true); }
    if (positionUs_ >= endUs_) positionUs_ = beginUs_;
    loadAt(positionUs_);
    return;
  }
  if (loading_) return;
  if (player_.isRunning()) {
    player_.resumePlayback();
    ++audioEpoch_;
    pumpAudio();
    emit stateChanged("playing");
    return;
  }
  if (positionUs_ >= endUs_) positionUs_ = beginUs_;
  loadAt(positionUs_);
}

void EvidencePlayback::pause() {
  wanted_ = false;
  audioClock_.stop();
  audioElapsed_.invalidate();
  player_.pausePlayback();
  ++audioEpoch_;
  pumpAudio();
  emit stateChanged("paused");
}

void EvidencePlayback::seek(qint64 relativeMs) {
  if (endUs_ <= beginUs_) return;
  const auto stamp = beginUs_ + qBound<qint64>(0, relativeMs * 1000, endUs_ - beginUs_ - 1);
  loadAt(stamp);
}

void EvidencePlayback::fail(const QString &message) {
  pause();
  loading_ = false;
  emit problem(message);
}

void EvidencePlayback::cache(const QJsonObject &clip, Cached done) {
  const auto id = clip["id"].toString();
  if (cached_.contains(id)) { done(cached_.value(id), {}); return; }
  if (downloads_.contains(id)) { downloads_.value(id)->waiting.append(done); return; }
  const auto size = qint64(clip["size_bytes"].toDouble());
  if (!clip["available"].toBool() || size <= 0 || size > 128 * 1024 * 1024) {
    done({}, "原始片段尚未缓存，请先获取原件"); return;
  }
  const auto name = QCryptographicHash::hash(id.toUtf8(), QCryptographicHash::Sha256).toHex();
  auto job = std::make_shared<Download>(directory_.filePath(name + ".mp4"));
  job->waiting.append(done);
  if (!job->file.open(QIODevice::WriteOnly)) { done({}, "无法写入回放缓存"); return; }
  QNetworkRequest request(url(clip["url"].toString()));
  if (!token_.isEmpty()) request.setRawHeader("Authorization", ("Bearer " + token_).toUtf8());
  job->reply = network_.get(request);
  QTimer::singleShot(15000, job->reply, [reply = job->reply] { if (!reply->isFinished()) reply->abort(); });
  job->reply->setReadBufferSize(65536);
  downloads_.insert(id, job);
  auto consume = [job, size] {
    const auto bytes = job->reply->readAll();
    job->received += bytes.size();
    if (job->received > size || job->file.write(bytes) != bytes.size()) {
      job->error = "回放缓存长度或写入失败";
      job->reply->abort();
    } else job->digest.addData(bytes);
  };
  connect(job->reply, &QNetworkReply::readyRead, this, consume);
  connect(job->reply, &QNetworkReply::finished, this, [this, job, id, clip, size, consume] {
    consume();
    auto error = job->error;
    if (error.isEmpty() && job->reply->error() != QNetworkReply::NoError)
      error = job->reply->errorString();
    if (error.isEmpty() && (job->received != size || job->digest.result().toHex() != clip["sha256"].toString().toLatin1()))
      error = "原始片段校验失败，已拒绝播放";
    QString path;
    if (error.isEmpty() && job->file.commit()) {
      path = job->file.fileName();
      cached_.insert(id, path);
    } else if (error.isEmpty()) error = "无法提交回放缓存";
    if (downloads_.value(id) == job) downloads_.remove(id);
    job->reply->deleteLater();
    for (auto waiting : job->waiting) waiting(path, error);
  });
}

void EvidencePlayback::loadAt(qint64 stamp) {
  haltPlayer();
  ++audioEpoch_;
  pumpAudio();
  positionUs_ = stamp;
  const bool audioOnly = videos_.isEmpty();
  current_ = at(audioOnly ? audios_ : videos_, stamp);
  emit positionChanged(position());
  if (current_.isEmpty()) { emit frameReady(QImage()); fail("该时间没有对应记录，可跳转到后续有记录的位置"); return; }
  if (!current_["available"].toBool()) { emit frameReady(QImage()); fail("该片段尚未缓存，请先获取原件"); return; }
  if (audioOnly) {
    audioOnly_ = true;
    emit frameReady(QImage());
    emit segmentChanged(current_["id"].toString());
    emit stateChanged(wanted_ ? "audio" : "paused");
    pumpAudio();
    return;
  }
  const auto generation = generation_;
  const auto clip = current_;
  loading_ = true;
  emit stateChanged("loading");
  cache(clip, [this, generation, clip, stamp](QString path, QString error) {
    if (generation != generation_) return;
    if (!error.isEmpty()) { fail(error); return; }
    loading_ = false;
    connections_.append(connect(&player_, &RecordingPlayer::opened, this,
      [this, generation, clip](qint64 durationMs, int, int) {
        if (generation != generation_) return;
        if (durationMs <= 0 || qAbs(durationMs * 1000 - qint64(clip["end_us"].toDouble() - clip["start_us"].toDouble())) > 200000) {
          haltPlayer();
          emit frameReady(QImage());
          fail("视频时长与来源时间不一致，已暂停回放");
        }
      }));
    connections_.append(connect(&player_, &RecordingPlayer::frameReady, this,
      [this, generation](const QImage &image) { if (generation == generation_) emit frameReady(image); }));
    connections_.append(connect(&player_, &RecordingPlayer::positionChanged, this,
      [this, generation, clip](qint64 ms) {
        if (generation != generation_) return;
        positionUs_ = qMin(endUs_, qint64(clip["start_us"].toDouble()) + ms * 1000);
        emit positionChanged(position());
        if (positionUs_ >= endUs_) { pause(); emit stateChanged("ended"); }
        else pumpAudio();
      }));
    connections_.append(connect(&player_, &RecordingPlayer::openFailed, this,
      [this, generation](const QString &message) { if (generation == generation_) fail(message); }));
    connections_.append(connect(&player_, &RecordingPlayer::playbackFinished, this,
      [this, generation] { if (generation == generation_ && wanted_) finishSegment(); }));
    player_.setSource(path);
    if (!wanted_) player_.pausePlayback();
    player_.requestSeek(qMax<qint64>(0, (stamp - qint64(clip["start_us"].toDouble())) / 1000));
    player_.start();
    emit segmentChanged(clip["id"].toString());
    emit stateChanged(wanted_ ? "playing" : "paused");
    bool found = false;
    for (auto value : videos_) {
      auto next = value.toObject();
      if (found && next["available"].toBool()) { cache(next, [](QString, QString) {}); break; }
      if (next["id"] == clip["id"]) found = true;
    }
  });
}

void EvidencePlayback::finishSegment() {
  const auto end = qint64(current_["end_us"].toDouble());
  positionUs_ = qMin(end, endUs_);
  emit positionChanged(position());
  if (positionUs_ >= endUs_) { pause(); emit stateChanged("ended"); return; }
  QJsonObject next;
  for (auto value : audioOnly_ ? audios_ : videos_) {
    auto clip = value.toObject();
    if (clip["id"] != current_["id"] && clip["start_us"].toDouble() >= end - toleranceUs_) {
      next = clip; break;
    }
  }
  if (next.isEmpty() || next["start_us"].toDouble() > end + toleranceUs_) {
    fail("记录在此中断，可用进度条选择后续片段"); return;
  }
  loadAt(qMax(beginUs_, qint64(next["start_us"].toDouble())));
}

void EvidencePlayback::setAudioEnabled(bool enabled) {
  audioEnabled_ = enabled;
  emit audioEnabledChanged(enabled);
  if (!enabled && audioOnly_) { pause(); return; }
  ++audioEpoch_;
  pumpAudio();
}

void EvidencePlayback::pumpAudio() {
  if (audioBusy_) return;
  auto clip = audioEnabled_ && wanted_ && !loading_ && (audioOnly_ || player_.isRunning())
                  ? at(audios_, positionUs_) : QJsonObject();
  if (!clip["available"].toBool()) clip = {};
  const auto key = clip.isEmpty() ? QString() : clip["id"].toString() + ":" + QString::number(audioEpoch_);
  if (key == appliedAudio_) return;
  const auto route = clip.isEmpty() ? "/api/playback/stop" : "/api/playback";
  QJsonObject body;
  if (!clip.isEmpty()) {
    body["media_id"] = clip["id"];
    body["offset_seconds"] = qMax(0.0, (positionUs_ - clip["start_us"].toDouble()) / 1e6);
  }
  QNetworkRequest request(url(route));
  request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
  if (!token_.isEmpty()) request.setRawHeader("Authorization", ("Bearer " + token_).toUtf8());
  audioBusy_ = true;
  auto reply = network_.post(request, QJsonDocument(body).toJson(QJsonDocument::Compact));
  QTimer::singleShot(8000, reply, [reply] { if (!reply->isFinished()) reply->abort(); });
  connect(reply, &QNetworkReply::finished, this, [this, reply, key] {
    audioBusy_ = false;
    if (reply->error() == QNetworkReply::NoError) {
      appliedAudio_ = key;
      const auto current = at(audios_, positionUs_);
      if (audioOnly_ && wanted_ && !key.isEmpty() && key == current["id"].toString() + ":" + QString::number(audioEpoch_)) {
        audioOriginUs_ = positionUs_;
        audioElapsed_.start();
        audioClock_.start(20);
      }
    }
    else {
      appliedAudio_ = key.isEmpty() ? QString() : QString("unknown");
      audioEnabled_ = false;
      emit audioEnabledChanged(false);
      if (audioOnly_) pause();
      emit problem("Echo 声音不可用，视频仍可继续回放");
    }
    reply->deleteLater();
    pumpAudio();
  });
}
