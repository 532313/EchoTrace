#include "echotrace/core.hpp"
#include "lvgl.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstring>
#include <fstream>
#include <sys/socket.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>
using namespace echotrace;
namespace {
volatile sig_atomic_t stopping = 0;
void stop(int) { stopping = 1; }
lv_obj_t *status_label, *project, *query, *result, *tabs, *thumb_image;
std::vector<uint8_t> thumb_bytes(28800);
lv_image_dsc_t thumb_descriptor{};
std::mutex snapshot_mutex;
std::vector<std::string> project_ids;
Json::Value snapshot;
BoundedQueue<Json::Value> commands(16);
std::string socket_path = "/run/echotrace/control.sock";
Json::Value exchange(Json::Value request) {
  int fd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (fd < 0)
    throw std::runtime_error("socket unavailable");
  timeval timeout{1, 0};
  setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
  sockaddr_un addr{};
  addr.sun_family = AF_UNIX;
  std::strncpy(addr.sun_path, socket_path.c_str(), sizeof(addr.sun_path) - 1);
  if (connect(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr))) {
    close(fd);
    throw std::runtime_error("capture service unavailable");
  }
  auto body = json(request) + "\n";
  size_t offset = 0;
  while (offset < body.size()) {
    auto n = send(fd, body.data() + offset, body.size() - offset, 0);
    if (n <= 0) {
      close(fd);
      throw std::runtime_error("control send failed");
    }
    offset += size_t(n);
  }
  std::string answer;
  char buffer[1024];
  while (answer.size() < 65536 && answer.find('\n') == std::string::npos) {
    auto n = recv(fd, buffer, sizeof(buffer), 0);
    if (n <= 0)
      break;
    answer.append(buffer, size_t(n));
  }
  close(fd);
  return parse_json(answer);
}
void send_command(const char *operation) {
  Json::Value c;
  c["op"] = operation;
  const auto selected = lv_dropdown_get_selected(project);
  if (selected < project_ids.size())
    c["project_id"] = project_ids[selected];
  if (std::string(operation) == "query") {
    auto text = std::string(lv_textarea_get_text(query));
    if (text.empty()) {
      lv_label_set_text(
          result, "记录中说：查一下，再提出问题。\n也可在工作台输入查询。");
      lv_tabview_set_active(tabs, 1, LV_ANIM_OFF);
      return;
    }
    c["text"] = text;
  }
  if (!commands.push(c))
    lv_label_set_text(result, "命令队列已满，请稍后重试");
}
void clicked(lv_event_t *event) {
  auto op = static_cast<const char *>(lv_event_get_user_data(event));
  if (std::string(op) == "toggle") {
    std::lock_guard<std::mutex> lock(snapshot_mutex);
    op = snapshot["state"] == "recording" ? "session.pause" : "session.resume";
  }
  send_command(op);
}
void button(lv_obj_t *parent, const char *label, const char *operation, int width = 98) {
  auto b = lv_button_create(parent);
  lv_obj_set_style_bg_color(b, lv_color_hex(0x27634e), 0);
  lv_obj_set_size(b, width, 40);
  auto l = lv_label_create(b);
  lv_label_set_text(l, label);
  lv_obj_center(l);
  lv_obj_add_event_cb(b, clicked, LV_EVENT_CLICKED,
                      const_cast<char *>(operation));
}
} // namespace
int main(int argc, char **argv) {
  std::string fb = "/dev/fb0", input = "/dev/input/event0";
  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if (i + 1 < argc && arg == "--socket")
      socket_path = argv[++i];
    else if (i + 1 < argc && arg == "--fb")
      fb = argv[++i];
    else if (i + 1 < argc && arg == "--input")
      input = argv[++i];
    else
      return 2;
  }
  signal(SIGINT, stop);
  signal(SIGTERM, stop);
  signal(SIGPIPE, SIG_IGN);
  lv_init();
  lv_tick_set_cb([]() -> uint32_t { return uint32_t(monotonic_us() / 1000); });
  auto display = lv_linux_fbdev_create();
  lv_linux_fbdev_set_file(display, fb.c_str());
  lv_evdev_create(LV_INDEV_TYPE_POINTER, input.c_str());
  auto screen = lv_screen_active();
  lv_obj_set_style_bg_color(screen, lv_color_hex(0xf3f6ef), 0);
  lv_obj_set_flex_flow(screen, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_all(screen, 8, 0);
  lv_obj_set_style_pad_row(screen, 6, 0);
  status_label = lv_label_create(screen);
  lv_label_set_text(status_label, "EchoTrace · 正在连接采集服务");
  lv_obj_set_width(status_label, LV_PCT(100));
  tabs = lv_tabview_create(screen);
  lv_obj_set_width(tabs, LV_PCT(100));
  lv_obj_set_flex_grow(tabs, 1);
  lv_tabview_set_tab_bar_size(tabs, 28);
  auto record_page = lv_tabview_add_tab(tabs, "记录");
  auto memory_page = lv_tabview_add_tab(tabs, "回看");
  for (auto page : {record_page, memory_page}) {
    lv_obj_set_flex_flow(page, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_all(page, 4, 0);
    lv_obj_set_style_pad_row(page, 6, 0);
  }
  project = lv_dropdown_create(record_page);
  lv_obj_set_width(project, LV_PCT(100));
  lv_obj_set_height(project, 40);
  lv_dropdown_set_options(project, "工作台记录");
  for (int i = 0; i < 2; ++i) {
    auto row = lv_obj_create(record_page);
    lv_obj_set_size(row, LV_PCT(100), 46);
    lv_obj_set_style_pad_all(row, 3, 0);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    if (!i) {
      button(row, "开始记忆", "session.start");
      button(row, "记这一步", "mark");
    } else {
      button(row, "暂停/继续", "toggle");
      button(row, "结束记忆", "session.stop");
    }
  }
  auto hint = lv_label_create(record_page);
  lv_obj_set_width(hint, LV_PCT(100));
  lv_label_set_text(hint, "按脚踏标记步骤；记录中说“查一下”可语音回看。");
  thumb_image = lv_image_create(memory_page);
  lv_obj_add_flag(thumb_image, LV_OBJ_FLAG_HIDDEN);
  thumb_descriptor.header.magic = LV_IMAGE_HEADER_MAGIC;
  thumb_descriptor.header.cf = LV_COLOR_FORMAT_RGB565;
  thumb_descriptor.header.w = 160;
  thumb_descriptor.header.h = 90;
  thumb_descriptor.header.stride = 320;
  thumb_descriptor.data_size = 28800;
  thumb_descriptor.data = thumb_bytes.data();
  result = lv_label_create(memory_page);
  lv_obj_set_width(result, LV_PCT(100));
  lv_label_set_long_mode(result, LV_LABEL_LONG_WRAP);
  lv_label_set_text(result,
                    "记录中说“查一下”，再提出问题。\n回看结果会附上原始画面。");
  query = lv_textarea_create(memory_page);
  lv_obj_set_size(query, LV_PCT(100), 36);
  lv_textarea_set_one_line(query, true);
  lv_textarea_set_placeholder_text(query, "也可使用工作台文字查询");
  button(memory_page, "回看记录", "query");
  button(memory_page, "播放证据声音", "evidence.play", 180);
  std::thread worker([] {
    while (!stopping) {
      try {
        if (auto c = commands.pop()) {
          auto reply = exchange(*c);
          if (reply.isMember("error"))
            throw std::runtime_error(reply["error"].asString());
        }
        Json::Value request;
        request["op"] = "status";
        auto value = exchange(request);
        {
          std::lock_guard<std::mutex> lock(snapshot_mutex);
          snapshot = std::move(value);
        }
      } catch (const std::exception &e) {
        std::lock_guard<std::mutex> lock(snapshot_mutex);
        snapshot["error"] = e.what();
        snapshot["connected"] = false;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  });
  std::string old_projects;
  uint64_t last = 0, last_query = 0;
  while (!stopping) {
    if (monotonic_us() - last > 100000) {
      Json::Value value;
      {
        std::lock_guard<std::mutex> lock(snapshot_mutex);
        value = snapshot;
      }
      std::string state = value["state"].asString();
      const bool marked = value["last_marker_us"].asUInt64() &&
                          monotonic_us() - value["last_marker_us"].asUInt64() < 2000000;
      std::string text = "EchoTrace · " +
                         (state == "recording" ? std::string("● 记录中")
                          : state == "paused"  ? "Ⅱ 已暂停"
                                               : "已停止") +
                         (marked ? " · 已记下" :
                          value["connected"].asBool() ? " · 在线" : " · 离线") +
                         (value["playback_active"].asBool() ? " · 播报" : "");
      const auto error = value["error"].asString();
      const bool fault = !error.empty() && state != "recording";
      if (fault) {
        if (error.find("存储") != std::string::npos ||
            error.find("storage reserve") != std::string::npos)
          text = state == "paused" ? "存储不足，录制已停止" : "存储不足，无法开始录制";
        else
          text = "状态异常，请查看回看页";
      }
      lv_label_set_text(status_label, text.c_str());
      lv_obj_set_style_text_color(status_label,
          lv_color_hex(fault || state == "recording" ? 0xb83225 :
                       state == "paused" ? 0x9a6100 : 0x25332e), 0);
      std::string choices;
      std::vector<std::string> new_ids;
      for (auto p : value["projects"]) {
        new_ids.push_back(p["id"].asString());
        if (!choices.empty())
          choices += '\n';
        choices += p["name"].asString();
      }
      if (!choices.empty() && (choices != old_projects || new_ids != project_ids)) {
        const auto index = lv_dropdown_get_selected(project);
        const std::string old_id =
            index < project_ids.size() ? project_ids[index] : "";
        lv_dropdown_set_options(project, choices.c_str());
        project_ids = std::move(new_ids);
        auto found = std::find(project_ids.begin(), project_ids.end(), old_id);
        if (found != project_ids.end())
          lv_dropdown_set_selected(project,
                                   unsigned(found - project_ids.begin()));
        old_projects = choices;
      }
      const bool active = state == "recording" || state == "paused";
      if (active) {
        const auto current = value["session"]["project_id"].asString();
        auto found = std::find(project_ids.begin(), project_ids.end(), current);
        if (found != project_ids.end())
          lv_dropdown_set_selected(project, unsigned(found - project_ids.begin()));
        lv_obj_add_state(project, LV_STATE_DISABLED);
      } else
        lv_obj_remove_state(project, LV_STATE_DISABLED);
      if (!value["error"].asString().empty())
        lv_label_set_text(result, value["error"].asCString());
      else if (value["last_marker_us"].asUInt64() &&
               monotonic_us() - value["last_marker_us"].asUInt64() < 2000000)
        lv_label_set_text(result, "已记下这一步");
      else if (value["query"].isMember("answer") &&
               value["query"]["received_us"].asUInt64() != last_query) {
        last_query = value["query"]["received_us"].asUInt64();
        lv_label_set_text(result, value["query"]["answer"].asCString());
        std::ifstream image(value["query"].get("thumbnail_path", "").asString(),
                            std::ios::binary);
        image.read(reinterpret_cast<char *>(thumb_bytes.data()),
                   thumb_bytes.size());
        if (image.gcount() == 28800) {
          lv_image_cache_drop(&thumb_descriptor);
          lv_image_set_src(thumb_image, &thumb_descriptor);
          lv_obj_remove_flag(thumb_image, LV_OBJ_FLAG_HIDDEN);
        } else
          lv_obj_add_flag(thumb_image, LV_OBJ_FLAG_HIDDEN);
        lv_tabview_set_active(tabs, 1, LV_ANIM_OFF);
        lv_obj_scroll_to_y(lv_tabview_get_content(tabs), 0, LV_ANIM_OFF);
      }
      last = monotonic_us();
    }
    auto wait = lv_timer_handler();
    usleep(std::min<uint32_t>(wait, 20) * 1000);
  }
  worker.join();
  lv_deinit();
  return 0;
}
