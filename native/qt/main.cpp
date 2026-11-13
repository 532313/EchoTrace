#include "EvidencePlayback.h"
#include <QApplication>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMainWindow>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QPushButton>
#include <QSlider>
#include <QSplitter>
#include <QStandardPaths>
#include <QStatusBar>
#include <QTabWidget>
#include <QTextEdit>
#include <QTimer>
#include <QUrl>
#include <QVBoxLayout>
#include <functional>

class Workbench : public QMainWindow {
public:
  explicit Workbench(QString endpoint, QString token)
      : endpoint_(endpoint), token_(token), playback_(endpoint, token, this) {
    setWindowTitle("EchoTrace · 过程记忆工作台");
    resize(1280, 800);
    auto *central = new QWidget;
    auto *layout = new QVBoxLayout(central);
    auto *header = new QHBoxLayout;
    auto *brand = new QLabel(
        "<b style='font-size:24px'>EchoTrace</b>　过程与依据，一起找回");
    header->addWidget(brand);
    header->addStretch();
    status_ = new QLabel("正在连接记忆服务");
    header->addWidget(status_);
    layout->addLayout(header);
    auto *splitter = new QSplitter;
    layout->addWidget(splitter, 1);
    auto *sidebar = new QWidget;
    auto *side = new QVBoxLayout(sidebar);
    side->addWidget(new QLabel("项目"));
    projects_ = new QListWidget;
    side->addWidget(projects_);
    auto *projectName = new QLineEdit;
    projectName->setPlaceholderText("新项目名称");
    side->addWidget(projectName);
    auto *create = new QPushButton("创建项目");
    side->addWidget(create);
    side->addWidget(new QLabel("会话"));
    sessions_ = new QListWidget;
    side->addWidget(sessions_);
    auto *start = new QPushButton("开始记忆");
    side->addWidget(start);
    splitter->addWidget(sidebar);
    auto *middle = new QWidget;
    auto *mid = new QVBoxLayout(middle);
    auto *searchBar = new QHBoxLayout;
    query_ = new QLineEdit;
    query_->setPlaceholderText("为什么更换排线？上次做到哪一步？");
    auto *search = new QPushButton("查找证据");
    searchBar->addWidget(query_, 1);
    searchBar->addWidget(search);
    mid->addLayout(searchBar);
    answer_ = new QLabel("选择项目和会话，或输入当时说过的话。");
    answer_->setWordWrap(true);
    mid->addWidget(answer_);
    tabs_ = new QTabWidget;
    timeline_ = new QListWidget;
    results_ = new QListWidget;
    tabs_->addTab(timeline_, "过程时间线");
    tabs_->addTab(results_, "检索结果");
    mid->addWidget(tabs_, 1);
    auto *controls = new QHBoxLayout;
    auto *mark = new QPushButton("记下这一步");
    auto *pause = new QPushButton("暂停 / 继续");
    auto *stop = new QPushButton("结束记忆");
    controls->addWidget(mark);
    controls->addWidget(pause);
    controls->addWidget(stop);
    mid->addLayout(controls);
    splitter->addWidget(middle);
    auto *evidence = new QWidget;
    auto *ev = new QVBoxLayout(evidence);
    video_ = new QLabel("原始证据");
    video_->setMinimumSize(400, 250);
    video_->setAlignment(Qt::AlignCenter);
    video_->setStyleSheet("background:#142b28;color:#ccdcd7;border-radius:8px");
    ev->addWidget(video_, 1);
    source_ = new QLabel;
    source_->setWordWrap(true);
    ev->addWidget(source_);
    slider_ = new QSlider(Qt::Horizontal);
    ev->addWidget(slider_);
    playbackStatus_ = new QLabel("按来源时间回放证据");
    ev->addWidget(playbackStatus_);
    auto *playbar = new QHBoxLayout;
    auto *play = new QPushButton("播放 / 暂停");
    auto *fetch = new QPushButton("获取原始片段");
    auto *sound = new QPushButton("Echo 联动声音");
    sound->setCheckable(true);
    playbar->addWidget(play);
    playbar->addWidget(fetch);
    playbar->addWidget(sound);
    ev->addLayout(playbar);
    note_ = new QTextEdit;
    note_->setPlaceholderText("用户笔记；语音转写和自动描述会分别显示");
    ev->addWidget(note_);
    auto *savebar = new QHBoxLayout;
    auto *save = new QPushButton("保存笔记");
    auto *exportBtn = new QPushButton("导出证据");
    auto *summary = new QPushButton("会话摘要");
    savebar->addWidget(save);
    savebar->addWidget(exportBtn);
    savebar->addWidget(summary);
    ev->addLayout(savebar);
    splitter->addWidget(evidence);
    splitter->setSizes({210, 420, 650});
    setCentralWidget(central);
    setStyleSheet("QMainWindow,QWidget{background:#f5f7f3;color:#203f37;font:"
                  "14px 'Noto Sans CJK SC';} "
                  "QListWidget,QLineEdit,QTextEdit{background:white;border:1px "
                  "solid #d7e2da;border-radius:6px;padding:8px;} "
                  "QPushButton{background:#dfebe1;border:1px solid "
                  "#c3d8c8;border-radius:6px;padding:9px;} "
                  "QPushButton:hover{background:#c4dccb;} "
                  "QListWidget::item{padding:10px;} "
                  "QListWidget::item:selected{background:#d9eadd;color:#163d2e;"
                  "} QTabWidget::pane{border:0;}");
    connect(projects_, &QListWidget::currentRowChanged, this, [this](int row) {
      if (row < 0)
        return;
      clearEvidence();
      project_ = projectItems_[row].toObject()["id"].toString();
      loadSessions();
    });
    connect(sessions_, &QListWidget::currentRowChanged, this, [this](int row) {
      if (row < 0)
        return;
      clearEvidence();
      session_ = sessionItems_[row].toObject();
      loadTimeline();
    });
    connect(timeline_, &QListWidget::currentRowChanged, this, [this](int row) {
      if (row >= 0)
        select(anchorItems_[row].toObject()["id"].toString());
    });
    connect(results_, &QListWidget::currentRowChanged, this, [this](int row) {
      if (row >= 0)
        select(hits_[row].toObject()["anchor"].toObject()["id"].toString());
    });
    connect(create, &QPushButton::clicked, this, [this, projectName] {
      api("POST", "/api/projects", {{"name", projectName->text()}},
          [this, projectName](auto) {
            projectName->clear();
            loadProjects();
          });
    });
    connect(search, &QPushButton::clicked, this, [this] { searchQuery(); });
    connect(query_, &QLineEdit::returnPressed, this, [this] { searchQuery(); });
    connect(start, &QPushButton::clicked, this, [this] {
      if (project_.isEmpty() || devices_.isEmpty()) {
        statusBar()->showMessage("请选择项目，并连接记录终端", 5000);
        return;
      }
      auto d = devices_[0].toObject();
      api("POST", "/api/sessions",
          {{"project_id", project_},
           {"device_id", d["device_id"]},
           {"boot_id", d["boot_id"]},
           {"source_time_us", d["source_time_us"]},
           {"title",
            QDateTime::currentDateTime().toString("MM-dd 工作台记录")}},
          [this](auto) { loadSessions(); });
    });
    connect(mark, &QPushButton::clicked, this, [this] {
      api("POST", "/api/sessions/" + sid() + "/mark",
          {{"note", query_->text()}}, [this](auto) {
            QTimer::singleShot(400, this, [this] { loadTimeline(); });
          });
    });
    connect(pause, &QPushButton::clicked, this, [this] {
      state(session_["state"] == "recording" ? "paused" : "recording");
    });
    connect(stop, &QPushButton::clicked, this, [this] { state("stopped"); });
    connect(save, &QPushButton::clicked, this, [this] {
      if (anchor_.isEmpty())
        return;
      api("PATCH", "/api/anchors/" + anchor_["id"].toString() + "/note",
          {{"note", note_->toPlainText()}, {"revision", anchor_["revision"]}},
          [this](auto obj) {
            anchor_ = obj;
            loadTimeline();
          });
    });
    connect(summary, &QPushButton::clicked, this, [this] {
      api("POST", "/api/sessions/" + sid() + "/summary", {},
          [this](auto obj) { answer_->setText(obj["summary"].toString()); });
    });
    connect(fetch, &QPushButton::clicked, this, [this] {
      for (const auto &value : media_) {
        auto m = value.toObject();
        if (!m["available"].toBool())
          api("POST", "/api/media/" + m["id"].toString() + "/fetch", {},
              [](auto) {});
      }
      QTimer::singleShot(2200, this, [this] {
        if (!anchor_.isEmpty())
          select(anchor_["id"].toString());
      });
    });
    connect(play, &QPushButton::clicked, this, [this] {
      if (playback_.isPlaying()) playback_.pause();
      else playback_.play();
    });
    connect(sound, &QPushButton::toggled, this, [this](bool enabled) {
      playback_.setAudioEnabled(enabled);
      if (enabled) playback_.play();
    });
    connect(&playback_, &EvidencePlayback::audioEnabledChanged, this, [sound](bool enabled) {
      sound->blockSignals(true);
      sound->setChecked(enabled);
      sound->blockSignals(false);
    });
    connect(&playback_, &EvidencePlayback::stateChanged, this, [this](const QString &state) {
      if (state == "audio") video_->setText("正在回放原始声音");
    });
    connect(slider_, &QSlider::sliderReleased, this, [this] {
      playback_.seek(slider_->value());
    });
    connect(exportBtn, &QPushButton::clicked, this, [this] {
      if (anchor_.isEmpty())
        return;
      api("POST", "/api/export", {{"anchor_ids", QJsonArray{anchor_["id"]}}},
          [this](auto obj) {
            download(obj["url"].toString(), [this](QByteArray bytes) {
              auto path =
                  QStandardPaths::writableLocation(
                      QStandardPaths::DocumentsLocation) +
                  "/EchoTrace-" +
                  QDateTime::currentDateTime().toString("yyyyMMdd-hhmmss") +
                  ".zip";
              QFile f(path);
              if (f.open(QIODevice::WriteOnly) &&
                  f.write(bytes) == bytes.size())
                statusBar()->showMessage("已导出：" + path, 10000);
            });
          });
    });
    connect(&playback_, &EvidencePlayback::frameReady, this, [this](QImage img) {
      if (img.isNull()) {
        video_->clear();
        video_->setText(playbackPlan_["video"].toArray().isEmpty() ? "仅有声音证据" : "该位置无可回放画面");
        return;
      }
      video_->setPixmap(QPixmap::fromImage(img).scaled(
          video_->size(), Qt::KeepAspectRatio, Qt::SmoothTransformation));
    });
    connect(&playback_, &EvidencePlayback::rangeChanged, this,
            [this](qint64 duration) {
              slider_->setRange(0, int(duration));
            });
    connect(&playback_, &EvidencePlayback::positionChanged, this,
            [this](qint64 ms) {
              if (!slider_->isSliderDown())
                slider_->setValue(int(ms));
              playbackStatus_->setText(QString("证据回放 %1 / %2 秒")
                  .arg(ms / 1000.0, 0, 'f', 1).arg(playback_.duration() / 1000.0, 0, 'f', 1));
            });
    connect(&playback_, &EvidencePlayback::problem, this,
            [this, sound](const QString &error) {
              statusBar()->showMessage(error, 10000);
              playbackStatus_->setText(error);
              sound->blockSignals(true);
              sound->setChecked(playback_.audioEnabled());
              sound->blockSignals(false);
            });
    auto *timer = new QTimer(this);
    connect(timer, &QTimer::timeout, this, [this] { refreshStatus(); });
    timer->start(3000);
    refreshStatus();
    loadProjects();
  }
  ~Workbench() override {
    playback_.stop();
  }

private:
  using Done = std::function<void(QJsonObject)>;
  void api(QString method, QString route, QJsonObject data, Done done) {
    request(method, route, QJsonDocument(data).toJson(QJsonDocument::Compact),
            [this, done](QByteArray bytes) {
              auto doc = QJsonDocument::fromJson(bytes);
              if (doc.isArray()) {
                QJsonObject wrapper;
                wrapper["items"] = doc.array();
                done(wrapper);
              } else
                done(doc.object());
            });
  }
  void download(QString route, std::function<void(QByteArray)> done) {
    request("GET", route, {}, done);
  }
  void request(QString method, QString route, QByteArray body,
               std::function<void(QByteArray)> done) {
    QNetworkRequest req(QUrl(endpoint_ + route));
    req.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
    if (!token_.isEmpty())
      req.setRawHeader("Authorization", ("Bearer " + token_).toUtf8());
    auto *reply = net_.sendCustomRequest(req, method.toUtf8(),
                                         method == "GET" ? QByteArray() : body);
    connect(reply, &QNetworkReply::finished, this, [this, reply, done] {
      auto body = reply->readAll();
      if (reply->error() != QNetworkReply::NoError)
        statusBar()->showMessage(QString::fromUtf8(body).left(500), 8000);
      else
        done(body);
      reply->deleteLater();
    });
  }
  void refreshStatus() {
    api("GET", "/api/status", {}, [this](auto obj) {
      devices_ = obj["devices"].toArray();
      status_->setText(
          devices_.isEmpty()
              ? "记录终端未连接 · 可回看历史"
              : "记录终端已连接 · " +
                    stateLabel(devices_[0].toObject()["capture_state"].toString()));
    });
  }
  void loadProjects() {
    api("GET", "/api/projects", {}, [this](auto obj) {
      projectItems_ = obj["items"].toArray();
      projects_->clear();
      for (auto p : projectItems_)
        projects_->addItem(p.toObject()["name"].toString());
      if (projects_->count())
        projects_->setCurrentRow(0);
    });
  }
  void loadSessions() {
    api("GET", "/api/sessions?project_id=" + project_, {}, [this](auto obj) {
      sessionItems_ = obj["items"].toArray();
      sessions_->clear();
      for (auto p : sessionItems_) {
        auto s = p.toObject();
        sessions_->addItem(s["title"].toString() + "\n" +
                           stateLabel(s["state"].toString()));
      }
      if (sessions_->count())
        sessions_->setCurrentRow(0);
    });
  }
  void loadTimeline() {
    if (sid().isEmpty())
      return;
    api("GET", "/api/sessions/" + sid() + "/timeline", {}, [this](auto obj) {
      const auto selected = timeline_->currentRow();
      const auto oldId = selected >= 0 && selected < anchorItems_.size()
                             ? anchorItems_[selected].toObject()["id"].toString()
                             : QString();
      session_ = obj["session"].toObject();
      anchorItems_ = obj["anchors"].toArray();
      timeline_->clear();
      for (auto v : anchorItems_) {
        auto a = v.toObject();
        auto seconds = int((a["source_time_us"].toDouble() -
                            session_["started_us"].toDouble()) /
                           1e6);
        auto text = a["note"].toString();
        if (text.isEmpty())
          text = a["asr_text"].toString();
        if (text.isEmpty())
          text = a["caption"].toString();
        timeline_->addItem(
            QString("%1:%2 · %3\n%4")
                .arg(seconds / 60, 2, 10, QChar('0'))
                .arg(seconds % 60, 2, 10, QChar('0'))
                .arg(kindLabel(a["kind"].toString()),
                     text.isEmpty() ? "操作标记，等待处理" : text));
      }
      int selectedRow = 0;
      for (int index = 0; index < anchorItems_.size(); ++index)
        if (anchorItems_[index].toObject()["id"].toString() == oldId)
          selectedRow = index;
      if (timeline_->count())
        timeline_->setCurrentRow(selectedRow);
    });
  }
  QString stateLabel(const QString &value) const {
    if (value == "recording") return "记录中";
    if (value == "paused") return "已暂停";
    if (value == "stopped") return "已结束";
    if (value == "interrupted") return "连接中断";
    return value;
  }
  QString kindLabel(const QString &value) const {
    if (value == "mark" || value == "pedal") return "步骤标记";
    if (value == "voice") return "口述";
    if (value == "tray") return "历史标记";
    if (value == "scene") return "画面变化";
    return value;
  }
  QString sid() const { return session_["id"].toString(); }
  void state(QString s) {
    if (sid().isEmpty())
      return;
    double now = session_["last_us"].toDouble();
    for (auto v : devices_)
      if (v.toObject()["device_id"] == session_["device_id"])
        now = v.toObject()["source_time_us"].toDouble();
    api("PATCH", "/api/sessions/" + sid() + "/state",
        {{"state", s}, {"source_time_us", now}}, [this](auto obj) {
          session_ = obj;
          loadTimeline();
        });
  }
  void searchQuery() {
    QJsonObject q{{"text", query_->text()}};
    if (!project_.isEmpty())
      q["project_id"] = project_;
    api("POST", "/api/query", q, [this](auto obj) {
      answer_->setText(obj["answer"].toString());
      hits_ = obj["hits"].toArray();
      results_->clear();
      for (auto h : hits_) {
        auto a = h.toObject()["anchor"].toObject();
        results_->addItem(a["title"].toString() + "\n" +
                          (a["note"].toString().isEmpty()
                               ? a["asr_text"].toString()
                               : a["note"].toString()));
      }
      tabs_->setCurrentWidget(results_);
      if (results_->count())
        results_->setCurrentRow(0);
    });
  }
  void clearEvidence() {
    ++selection_;
    playbackPlan_ = {};
    playback_.setPlan(playbackPlan_);
    anchor_ = {};
    media_ = {};
    note_->clear();
    source_->clear();
    video_->clear();
    video_->setText("选择步骤，查看原始证据");
    slider_->setRange(0, 0);
  }
  void select(QString id) {
    const auto selection = ++selection_;
    if (anchor_["id"].toString() != id) playback_.stop();
    api("GET", "/api/anchors/" + id, {}, [this, selection](auto obj) {
      if (selection != selection_) return;
      anchor_ = obj["anchor"].toObject();
      media_ = obj["media"].toArray();
      const auto plan = obj["playback"].toObject();
      if (plan != playbackPlan_) {
        playbackPlan_ = plan;
        playback_.setPlan(plan);
      }
      note_->setPlainText(anchor_["note"].toString());
      source_->setText("证据 ID：" + anchor_["id"].toString() + "\n口述转写：" +
                       anchor_["asr_text"].toString() + "\n自动描述：" +
                       anchor_["caption"].toString());
      if (plan["video"].toArray().isEmpty()) {
        video_->setText("仅有声音证据，可用回放按钮收听");
        return;
      }
      api("POST", "/api/anchors/" + anchor_["id"].toString() + "/frame", {},
          [this, selection](auto obj) {
            if (selection != selection_) return;
            download(obj["url"].toString(), [this, selection](auto bytes) {
              if (selection != selection_ || playback_.isPlaying()) return;
              QImage img = QImage::fromData(bytes);
              if (!img.isNull())
                video_->setPixmap(QPixmap::fromImage(img).scaled(
                    video_->size(), Qt::KeepAspectRatio,
                    Qt::SmoothTransformation));
            });
          });
    });
  }
  QString endpoint_, token_, project_;
  QNetworkAccessManager net_;
  EvidencePlayback playback_;
  QJsonObject playbackPlan_;
  quint64 selection_ = 0;
  QJsonArray projectItems_, sessionItems_, anchorItems_, hits_, media_,
      devices_;
  QJsonObject session_, anchor_;
  QListWidget *projects_, *sessions_, *timeline_, *results_;
  QTabWidget *tabs_;
  QLabel *status_, *answer_, *video_, *source_, *playbackStatus_;
  QLineEdit *query_;
  QTextEdit *note_;
  QSlider *slider_;
};
int main(int argc, char **argv) {
  QApplication app(argc, argv);
  QCoreApplication::setApplicationName("EchoTrace");
  QString endpoint = "http://127.0.0.1:18081";
  QString token = qEnvironmentVariable("ECHOTRACE_TOKEN");
  for (int i = 1; i < argc; ++i)
    if (QString(argv[i]) == "--hub" && i + 1 < argc)
      endpoint = argv[++i];
  Workbench ui(endpoint, token);
  ui.show();
  if (qEnvironmentVariableIsSet("ECHOTRACE_SCREENSHOT"))
    QTimer::singleShot(2500, &ui, [&ui] {
      ui.grab().save(qEnvironmentVariable("ECHOTRACE_SCREENSHOT"));
      qApp->quit();
    });
  return app.exec();
}
