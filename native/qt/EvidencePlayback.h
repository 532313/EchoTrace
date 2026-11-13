#pragma once
#include "RecordingPlayer.h"
#include <QHash>
#include <QElapsedTimer>
#include <QJsonArray>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QObject>
#include <QTemporaryDir>
#include <QTimer>
#include <functional>
#include <memory>

class EvidencePlayback : public QObject {
  Q_OBJECT
public:
  EvidencePlayback(QString endpoint, QString token, QObject *parent = nullptr);
  ~EvidencePlayback() override;
  void setPlan(const QJsonObject &plan);
  void play();
  void pause();
  void stop();
  void seek(qint64 relativeMs);
  void setAudioEnabled(bool enabled);
  bool isPlaying() const { return wanted_; }
  bool audioEnabled() const { return audioEnabled_; }
  qint64 position() const { return (positionUs_ - beginUs_) / 1000; }
  qint64 duration() const { return (endUs_ - beginUs_) / 1000; }
signals:
  void frameReady(const QImage &image);
  void positionChanged(qint64 relativeMs);
  void rangeChanged(qint64 durationMs);
  void segmentChanged(const QString &id);
  void stateChanged(const QString &state);
  void problem(const QString &message);
  void audioEnabledChanged(bool enabled);
private:
  struct Download;
  using Cached = std::function<void(QString, QString)>;
  QJsonObject at(const QJsonArray &clips, qint64 stamp) const;
  void loadAt(qint64 stamp);
  void cache(const QJsonObject &clip, Cached done);
  void finishSegment();
  void haltPlayer();
  void fail(const QString &message);
  void pumpAudio();
  QUrl url(const QString &path) const;
  QString endpoint_, token_;
  QNetworkAccessManager network_;
  QTemporaryDir directory_;
  RecordingPlayer player_;
  QTimer audioClock_;
  QElapsedTimer audioElapsed_;
  qint64 audioOriginUs_ = 0;
  QVector<QMetaObject::Connection> connections_;
  QHash<QString, QString> cached_;
  QHash<QString, std::shared_ptr<Download>> downloads_;
  QJsonArray videos_, audios_;
  QJsonObject current_;
  quint64 generation_ = 0, audioEpoch_ = 0;
  qint64 beginUs_ = 0, endUs_ = 0, positionUs_ = 0, toleranceUs_ = 50000;
  bool wanted_ = false, loading_ = false, audioEnabled_ = false, audioBusy_ = false;
  bool audioOnly_ = false;
  QString appliedAudio_;
};
