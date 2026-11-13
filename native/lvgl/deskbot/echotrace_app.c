#include "echotrace_app.h"
#include "ui.h"
#include "echotrace/screen_frame.h"
#include <errno.h>
#include <json-c/json.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

LV_FONT_DECLARE(echotrace_font);
#define THUMB_BYTES (160 * 90 * 2)
#define MAX_PROJECTS 32
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_t worker;
static int running, worker_started, queued, failed_send, rendering;
static char command[4096], notice[320];
static json_object *shared_status, *status, *anchor_model;
static lv_obj_t *screen, *status_label, *notice_label, *tabs, *project_select;
static lv_obj_t *start_button, *mark_button, *pause_button, *stop_button;
static lv_obj_t *result_label, *project_label, *dialog, *editor;
static lv_obj_t *live_image, *elapsed_label, *record_project_label, *preview_hint;
static lv_image_dsc_t live_descriptor;
static uint8_t live_data[128 * 72 * 2];
static uint64_t live_stamp;

static char compare_anchor[100], compare_caption[600];
static uint8_t compare_original[THUMB_BYTES], compare_second[THUMB_BYTES];
static uint8_t compare_small[2][128 * 72 * 2];
static lv_image_dsc_t compare_descriptors[2];
static void confirm_delete(void);
static void export_click(lv_event_t *event);
static void compare_click(lv_event_t *event);


static int edit_mode;
static lv_obj_t *voice_button, *dialog_voice, *query_list, *question_label, *speak_button, *edit_hint;
static char spoken_answer[2400];
static void render_query(json_object *data);
static void voice_click(lv_event_t *event);

static lv_timer_t *timer;
static char selected_project[100], pending_id[100], pending_action[64];
static char edit_project[100], edit_anchor[100], old_choices[4096], last_query[100];
static uint64_t edit_revision;
static int edit_append;
static uint64_t pending_since;
static char entry_target[160];
static lv_obj_t *player_view, *player_image, *player_rate;
static lv_timer_t *video_timer;
static int video_fd = -1, video_dirty;
static char video_socket[108], video_stream[33], display_report[1024];
static uint8_t video_packet[ET_SCREEN_WIRE_BYTES], video_pixels[ET_SCREEN_PIXELS];
static lv_image_dsc_t video_descriptor;
static uint32_t video_sequence;
static uint64_t player_controls_us;
static uint64_t video_received, video_rendered, video_discarded, video_epoch, video_window, video_window_frames;
static void close_player(lv_event_t *event);
static void open_player(void);

static void panel(lv_obj_t *item) {
    lv_obj_set_style_bg_color(item, lv_color_hex(0xffffff), 0);
    lv_obj_set_style_bg_opa(item, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(item, lv_color_hex(0x20313f), 0);
    lv_obj_set_style_border_width(item, 0, 0);
    lv_obj_set_style_radius(item, 8, 0);
    lv_obj_set_style_shadow_width(item, 0, 0);
}
static void set_text(lv_obj_t *item, const char *text) {
    if (item && strcmp(lv_label_get_text(item), text)) lv_label_set_text(item, text);
}
static void destructive(lv_obj_t *item) {
    lv_obj_set_style_bg_color(item, lv_color_hex(0xb53d4b), 0);
    lv_obj_set_style_bg_color(item, lv_color_hex(0x902c38), LV_STATE_PRESSED);
}

static void open_at_startup(lv_timer_t *once) {
    lv_timer_delete(once);
    lv_lib_pm_OpenPage(&page_manager, NULL, "EchoTracePage");
}
void echotrace_app_startup(void) {
    const char *target = getenv("ECHOTRACE_OPEN_APP");
    if (target && (!strcmp(target, "record") || !strcmp(target, "project") ||
                   !strcmp(target, "history") || !strcmp(target, "ask") ||
                   (!strncmp(target, "anchor:", 7) && strlen(target) < sizeof(entry_target))))
        lv_timer_create(open_at_startup, 200, NULL);
}

static void copy(char *out, size_t size, const char *text) {
    snprintf(out, size, "%s", text ? text : "");
}
static uint64_t now_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}
static json_object *field(json_object *object, const char *key) {
    json_object *value = NULL;
    if (object) json_object_object_get_ex(object, key, &value);
    return value;
}
static const char *str(json_object *object, const char *key) {
    json_object *value = field(object, key);
    return value && json_object_is_type(value, json_type_string)
               ? json_object_get_string(value) : "";
}
static int boolean(json_object *object, const char *key) {
    return json_object_get_boolean(field(object, key));
}
static uint64_t number(json_object *object, const char *key) {
    return json_object_get_uint64(field(object, key));
}
static void message(const char *text) {
    pthread_mutex_lock(&mutex);
    copy(notice, sizeof(notice), text);
    pthread_mutex_unlock(&mutex);
}
static void font_tree(lv_obj_t *object) {
    lv_obj_set_style_text_font(object, &echotrace_font, LV_PART_MAIN);
    for (uint32_t i = 0; i < lv_obj_get_child_count(object); ++i)
        font_tree(lv_obj_get_child(object, i));
}
static json_object *exchange(json_object *request, char *error, size_t size) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) { copy(error, size, "无法创建控制连接"); return NULL; }
    struct timeval timeout = {.tv_sec = 1};
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    const char *path = getenv("ECHOTRACE_SOCKET");
    if (!path || !*path) path = access("/run/echotrace-sd.sock", F_OK) == 0
                                     ? "/run/echotrace-sd.sock"
                                     : "/run/echotrace/control.sock";
    if (strlen(path) >= sizeof(address.sun_path)) {
        copy(error, size, "控制路径过长"); close(fd); return NULL;
    }
    strcpy(address.sun_path, path);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        copy(error, size, "采集服务未连接"); close(fd); return NULL;
    }
    const char *body = json_object_to_json_string_ext(request, JSON_C_TO_STRING_PLAIN);
    size_t at = 0, length = strlen(body);
    while (at < length) {
        ssize_t n = send(fd, body + at, length - at, MSG_NOSIGNAL);
        if (n <= 0) { copy(error, size, "命令发送失败"); close(fd); return NULL; }
        at += (size_t)n;
    }
    if (send(fd, "\n", 1, MSG_NOSIGNAL) != 1) {
        copy(error, size, "命令发送失败"); close(fd); return NULL;
    }
    char response[65536];
    at = 0;
    while (at + 1 < sizeof(response)) {
        ssize_t n = recv(fd, response + at, sizeof(response) - at - 1, 0);
        if (n <= 0) break;
        at += (size_t)n;
        if (memchr(response, '\n', at)) break;
    }
    close(fd);
    response[at] = 0;
    if (!at || !memchr(response, '\n', at)) {
        copy(error, size, "采集服务没有完整回复"); return NULL;
    }
    json_object *reply = json_tokener_parse(response);
    if (!reply || !json_object_is_type(reply, json_type_object)) {
        if (reply) json_object_put(reply);
        copy(error, size, "采集服务回复格式错误"); return NULL;
    }
    return reply;
}
static void *worker_main(void *unused) {
    (void)unused;
    for (;;) {
        char next[4096] = "";
        pthread_mutex_lock(&mutex);
        int active = running;
        if (queued) { copy(next, sizeof(next), command); queued = 0; }
        pthread_mutex_unlock(&mutex);
        if (!active && !*next) break;
        if (*next) {
            char error[320] = "";
            json_object *request = json_tokener_parse(next);
            json_object *reply = request ? exchange(request, error, sizeof(error)) : NULL;
            if (request) json_object_put(request);
            if (reply) {
                if (*str(reply, "error")) copy(error, sizeof(error), str(reply, "error"));
                else if (boolean(reply, "queued_offline"))
                    copy(error, sizeof(error), "已在本机执行，联网后同步");
                json_object_put(reply);
            }
            if (*error) {
                pthread_mutex_lock(&mutex);
                copy(notice, sizeof(notice), error);
                failed_send = 1;
                pthread_mutex_unlock(&mutex);
            }
        }
        if (!active) break;
        char error[320] = "";
        json_object *request = json_object_new_object();
        json_object_object_add(request, "op", json_object_new_string("status"));
        char report[1024];
        pthread_mutex_lock(&mutex); copy(report, sizeof(report), display_report); pthread_mutex_unlock(&mutex);
        if (*report) {
            json_object *metrics = json_tokener_parse(report);
            if (metrics) json_object_object_add(request, "display", metrics);
        }
        json_object *reply = exchange(request, error, sizeof(error));
        json_object_put(request);
        pthread_mutex_lock(&mutex);
        if (shared_status) json_object_put(shared_status);
        shared_status = reply;
        if (!reply) copy(notice, sizeof(notice), error);
        pthread_mutex_unlock(&mutex);
        usleep(180000);
    }
    return NULL;
}
static int enqueue(json_object *request) {
    const char *text = json_object_to_json_string_ext(request, JSON_C_TO_STRING_PLAIN);
    pthread_mutex_lock(&mutex);
    int ok = (!queued || !strcmp(str(request, "op"), "screen.stop")) && strlen(text) < sizeof(command);
    if (ok) { copy(command, sizeof(command), text); queued = 1; notice[0] = 0; }
    else copy(notice, sizeof(notice), "上一条操作仍在等待");
    pthread_mutex_unlock(&mutex);
    return ok;
}
static json_object *object(void) { return json_object_new_object(); }
static void putstr(json_object *o, const char *key, const char *value) {
    json_object_object_add(o, key, json_object_new_string(value ? value : ""));
}
static void operation(const char *op) {
    json_object *request = object();
    putstr(request, "op", op);
    if (*selected_project) putstr(request, "project_id", selected_project);
    enqueue(request);
    json_object_put(request);
}
static int call(const char *action, json_object *args) {
    if (*pending_id) { message("正在处理，请稍候"); return 0; }
    json_object *request = object();
    char id[100];
    snprintf(id, sizeof(id), "screen-%ld-%llu", (long)getpid(),
             (unsigned long long)now_us());
    putstr(request, "op", "app.call");
    putstr(request, "id", id);
    putstr(request, "action", action);
    json_object_object_add(request, "arguments", args ? json_object_get(args) : object());
    int ok = enqueue(request);
    if (ok) {
        copy(pending_id, sizeof(pending_id), id);
        copy(pending_action, sizeof(pending_action), action);
        pending_since = now_us();
        message("正在处理…");
    }
    json_object_put(request);
    return ok;
}
static lv_obj_t *label(lv_obj_t *parent, const char *text, int width) {
    lv_obj_t *item = lv_label_create(parent);
    lv_obj_set_width(item, width);
    lv_label_set_long_mode(item, LV_LABEL_LONG_WRAP);
    lv_label_set_text(item, text);
    lv_obj_set_style_text_color(item, lv_color_hex(0x20313f), 0);
    return item;
}
static lv_obj_t *button(lv_obj_t *parent, const char *text, int width,
                        lv_event_cb_t callback, intptr_t action) {
    lv_obj_t *item = lv_button_create(parent);
    lv_obj_set_size(item, width, 32);
    panel(item);
    lv_obj_set_style_radius(item, 7, 0);
    lv_obj_set_style_bg_color(item, lv_color_hex(0x127d73), 0);
    lv_obj_set_style_bg_color(item, lv_color_hex(0x096359), LV_STATE_PRESSED);
    lv_obj_set_style_text_color(item, lv_color_hex(0xffffff), 0);
    lv_obj_set_style_bg_color(item, lv_color_hex(0xe5eaee), LV_STATE_DISABLED);
    lv_obj_set_style_text_color(item, lv_color_hex(0x82919b), LV_STATE_DISABLED);
    lv_obj_set_style_pad_all(item, 3, 0);
    lv_obj_t *caption = lv_label_create(item);
    lv_label_set_text(caption, text);
    lv_obj_center(caption);
    if (callback) lv_obj_add_event_cb(item, callback, LV_EVENT_CLICKED, (void *)action);
    return item;
}
static lv_obj_t *row(lv_obj_t *parent, int height) {
    lv_obj_t *item = lv_obj_create(parent);
    lv_obj_set_size(item, LV_PCT(100), height);
    lv_obj_set_style_pad_all(item, 0, 0);
    lv_obj_set_style_border_width(item, 0, 0);
    lv_obj_set_style_bg_opa(item, LV_OPA_TRANSP, 0);
    lv_obj_set_flex_flow(item, LV_FLEX_FLOW_ROW);
    lv_obj_set_style_pad_column(item, 5, 0);
    lv_obj_remove_flag(item, LV_OBJ_FLAG_SCROLLABLE);
    return item;
}
static void enabled(lv_obj_t *item, int yes) {
    if (yes) lv_obj_remove_state(item, LV_STATE_DISABLED);
    else lv_obj_add_state(item, LV_STATE_DISABLED);
}
static const char *selected_name(void) {
    json_object *projects = field(status, "projects");
    for (size_t i = 0; projects && i < json_object_array_length(projects); ++i) {
        json_object *project = json_object_array_get_idx(projects, i);
        if (!strcmp(str(project, "id"), selected_project)) return str(project, "name");
    }
    return "工作台记录";
}
static void select_project(const char *id) {
    copy(selected_project, sizeof(selected_project), id);
    json_object *request = object();
    putstr(request, "op", "project.select");
    putstr(request, "project_id", id);
    enqueue(request);
    json_object_put(request);
}
static void project_changed(lv_event_t *event) {
    if (rendering) return;
    unsigned index = lv_dropdown_get_selected(lv_event_get_target(event));
    json_object *projects = field(status, "projects");
    if (projects && index < json_object_array_length(projects))
        select_project(str(json_object_array_get_idx(projects, index), "id"));
}
static void close_dialog(lv_event_t *event) {
    (void)event;
    if (dialog && *pending_id && !strcmp(pending_action, "voice")) {
        operation("voice.cancel"); pending_id[0] = 0;
    }
    if (dialog) { lv_obj_delete(dialog); dialog = NULL; editor = dialog_voice = edit_hint = NULL; }
}
static void save_project(lv_event_t *event) {
    (void)event;
    json_object *args = object();
    const char *action;
    if (edit_mode == 1) {
        putstr(args, "text", lv_textarea_get_text(editor));
        if (*selected_project) putstr(args, "project_id", selected_project);
        action = "query";
    } else if (edit_mode == 2) {
        putstr(args, "anchor_id", edit_anchor);
        putstr(args, "note", lv_textarea_get_text(editor));
        json_object_object_add(args, "revision", json_object_new_int64(edit_revision));
        json_object_object_add(args, "append", json_object_new_boolean(edit_append));
        action = "anchor.note";
    } else {
        putstr(args, "name", lv_textarea_get_text(editor));
        if (*edit_project) putstr(args, "project_id", edit_project);
        action = *edit_project ? "project.rename" : "project.create";
    }
    if (call(action, args)) {
        if (edit_mode == 1) close_dialog(NULL);
        else if (edit_hint) lv_label_set_text(edit_hint, "正在保存…");
    }
    json_object_put(args);
}
static void project_editor(lv_event_t *event) {
    if (dialog) return;
    int kind = (int)(intptr_t)lv_event_get_user_data(event);
    int rename = kind == 1;
    edit_mode = kind == 2 ? 1 : kind == 3 ? 2 : 0;
    if (edit_mode == 2 && !anchor_model) { message("请先打开一个步骤"); return; }
    if (edit_mode == 2) {
        copy(edit_anchor, sizeof(edit_anchor), str(anchor_model, "id"));
        edit_revision = number(anchor_model, "revision");
        edit_append = boolean(anchor_model, "note_is_long");
    }
    if (rename && !*selected_project) { message("请先选择项目"); return; }
    copy(edit_project, sizeof(edit_project), rename ? selected_project : "");
    dialog = lv_obj_create(screen);
    panel(dialog);
    lv_obj_add_flag(dialog, LV_OBJ_FLAG_FLOATING);
    lv_obj_set_size(dialog, LV_PCT(100), LV_PCT(100));
    lv_obj_set_pos(dialog, 0, 0);
    lv_obj_set_style_pad_all(dialog, 4, 0);
    lv_obj_remove_flag(dialog, LV_OBJ_FLAG_SCROLLABLE);
    label(dialog, edit_mode == 2 ? "修改笔记" : edit_mode == 1 ? "输入问题" : rename ? "修改名称" : "新建项目", 108);
    dialog_voice = button(dialog, "语音", 62, voice_click, 0);
    lv_obj_set_pos(dialog_voice, 116, 0);
    lv_obj_t *cancel = button(dialog, "取消", 57, close_dialog, 0);
    lv_obj_set_pos(cancel, 186, 0);
    lv_obj_t *save = button(dialog, "保存", 57, save_project, 0);
    lv_obj_set_pos(save, 249, 0);
    editor = lv_textarea_create(dialog);
    lv_obj_set_pos(editor, 0, 38);
    lv_obj_set_size(editor, 306, 38);
    lv_textarea_set_one_line(editor, edit_mode != 2);
    lv_textarea_set_max_length(editor, edit_mode == 2 ? 600 : edit_mode == 1 ? 300 : 120);
    char suggested[100];
    snprintf(suggested, sizeof(suggested), "工作记录 %zu",
             (field(status, "projects") ? json_object_array_length(field(status, "projects")) : 0) + 1);
    lv_textarea_set_text(editor, edit_mode == 2 ? (boolean(anchor_model, "note_is_long") ? "" : str(anchor_model, "note")) : edit_mode == 1 ? "上次做到哪一步" : rename ? selected_name() : suggested);
    lv_obj_t *keyboard = lv_keyboard_create(dialog);
    lv_obj_set_pos(keyboard, 0, 84);
    lv_obj_set_size(keyboard, 306, 122);
    lv_keyboard_set_textarea(keyboard, editor);
    edit_hint = label(dialog, edit_mode == 2 && boolean(anchor_model, "note_is_long") ? "长笔记以追加方式保存，原文保留" : "可点语音输入中文，确认后保存", 302);
    lv_obj_set_pos(edit_hint, 0, 211);
    lv_label_set_long_mode(edit_hint, LV_LABEL_LONG_DOT);
    font_tree(dialog);
}
static void voice_click(lv_event_t *event) {
    (void)event;
    if (boolean(status, "listening")) { operation("voice.stop"); message("正在识别…"); return; }
    if (*pending_id) { message("正在处理，请稍候"); return; }
    if (strcmp(str(status, "state"), "stopped")) {
        message("录制中请直接说“查一下”加问题；命名请先结束录制"); return;
    }
    json_object *request = object(), *args = object();
    char id[100]; snprintf(id, sizeof(id), "voice-%ld-%llu", (long)getpid(), (unsigned long long)now_us());
    putstr(request, "op", "voice.start"); putstr(request, "id", id);
    putstr(args, "mode", dialog && edit_mode == 0 ? "project.name" : dialog && edit_mode == 2 ? "note" : "query");
    if (*selected_project) putstr(args, "project_id", selected_project);
    json_object_object_add(request, "arguments", args);
    if (enqueue(request)) {
        copy(pending_id, sizeof(pending_id), id); copy(pending_action, sizeof(pending_action), "voice");
        pending_since = now_us(); message("正在听，请说话后点结束，最长 12 秒");
    }
    json_object_put(request);
}
static void operation_click(lv_event_t *event) {
    intptr_t which = (intptr_t)lv_event_get_user_data(event);
    const char *state = str(status, "state");
    switch (which) {
    case 0: operation("session.start"); break;
    case 1: operation("mark"); message("已提交步骤标记"); break;
    case 2: operation(!strcmp(state, "recording") ? "session.pause" : "session.resume"); break;
    case 3: operation("session.stop"); break;
    case 4: operation("evidence.play"); break;
    }
}
static lv_obj_t *history_page, *detail, *detail_body, *detail_image, *detail_media_label;
static char history_session[100], opened_anchor[100], history_title[1024];
static int history_offset, history_more, detail_pending;
static uint64_t detail_retry;
static lv_obj_t *player_slider, *player_text, *player_pause;
static uint64_t player_frame;
static uint8_t detail_pixels[THUMB_BYTES];
static lv_image_dsc_t detail_descriptor;


static void delete_confirmed(lv_event_t *event) {
    (void)event;
    json_object *args = object(); putstr(args, "session_id", history_session);
    if (call("session.delete", args) && edit_hint) {
        lv_label_set_text(edit_hint, "正在删除，返回不撤销已确认的操作");
        lv_obj_t *choices = lv_obj_get_parent(lv_event_get_target(event));
        lv_label_set_text(lv_obj_get_child(lv_obj_get_child(choices, 0), 0), "返回");
    }
    json_object_put(args);
}
static void confirm_delete(void) {
    if (dialog || !*history_session) return;
    dialog = lv_obj_create(screen);
    panel(dialog);
    lv_obj_add_flag(dialog, LV_OBJ_FLAG_FLOATING);
    lv_obj_set_pos(dialog, 0, 0); lv_obj_set_size(dialog, LV_PCT(100), LV_PCT(100));
    lv_obj_set_style_pad_all(dialog, 8, 0);
    lv_obj_set_flex_flow(dialog, LV_FLEX_FLOW_COLUMN);
    label(dialog, "删除这次记录？", 292);
    label(dialog, *history_title ? history_title : history_session, 292);
    label(dialog, "会删除本次会话和源端原片。已导出的 ZIP 保留。", 292);
    lv_obj_t *choices = row(dialog, 34);
    button(choices, "取消", 137, close_dialog, 0);
    destructive(button(choices, "确认删除", 137, delete_confirmed, 0));
    edit_hint = label(dialog, "", 292);
    editor = dialog_voice = NULL;
    font_tree(dialog);
}

static void request_history(int reset) {
    if (reset) { history_session[0] = 0; history_offset = 0; }
    json_object *args = object();
    if (*history_session) putstr(args, "session_id", history_session);
    else if (*selected_project) putstr(args, "project_id", selected_project);
    json_object_object_add(args, "offset", json_object_new_int(history_offset));
    call(*history_session ? "history.anchors" : "history.sessions", args);
    json_object_put(args);
}
static void free_button_data(lv_event_t *event) {
    free(lv_event_get_user_data(event));
}
static void history_item(lv_event_t *event) {
    const char *id = lv_event_get_user_data(event);
    if (*pending_id) { message("请等待当前操作完成"); return; }
    if (*history_session) {
        copy(opened_anchor, sizeof(opened_anchor), id);
        json_object *args = object(); putstr(args, "anchor_id", id);
        call("anchor.open", args); json_object_put(args);
    } else {
        copy(history_session, sizeof(history_session), id);
        history_offset = 0; request_history(0);
    }
}
static void history_control(lv_event_t *event) {
    intptr_t op = (intptr_t)lv_event_get_user_data(event);
    if (*pending_id) { message("正在处理，请稍候"); return; }
    if (op == 5) { confirm_delete(); return; }
    if (op == 0) request_history(1);
    else if (op == 1 || op == 2) {
        if (op == 1 && history_offset >= 8) history_offset -= 8;
        else if (op == 2 && history_more) history_offset += 8;
        else return;
        request_history(0);
    } else {
        json_object *args = object(); putstr(args, "session_id", history_session);
        call(op == 3 ? "session.summary" : "session.continue", args);
        json_object_put(args);
    }
}
static void close_detail(lv_event_t *event) {
    (void)event;
    close_player(NULL);
    if (detail && *opened_anchor &&
        !strcmp(str(field(status, "player"), "anchor_id"), opened_anchor)) operation("screen.stop");
    if (detail) { lv_obj_delete(detail); detail = detail_body = detail_image = NULL; }
    player_slider = player_text = player_pause = NULL; player_frame = 0;
    if (anchor_model) { json_object_put(anchor_model); anchor_model = NULL; }
    opened_anchor[0] = 0; detail_pending = 0;
    if (*pending_id && (!strcmp(pending_action, "anchor.open") || !strncmp(pending_action, "player.", 7))) {
        if (!strncmp(pending_action, "player.", 7)) operation("screen.stop");
        pending_id[0] = 0;
    }
}
static void reader(const char *title) {
    close_detail(NULL);
    detail = lv_obj_create(screen);
    panel(detail);
    lv_obj_add_flag(detail, LV_OBJ_FLAG_FLOATING);
    lv_obj_set_pos(detail, 0, 0);
    lv_obj_set_size(detail, LV_PCT(100), LV_PCT(100));
    lv_obj_set_style_pad_all(detail, 3, 0);
    lv_obj_remove_flag(detail, LV_OBJ_FLAG_SCROLLABLE);
    button(detail, "返回", 55, close_detail, 0);
    lv_obj_t *heading = label(detail, title, 242);
    lv_obj_set_pos(heading, 62, 4);
    lv_label_set_long_mode(heading, LV_LABEL_LONG_DOT);
    detail_body = lv_obj_create(detail);
    panel(detail_body);
    lv_obj_set_pos(detail_body, 0, 38);
    lv_obj_set_size(detail_body, 307, 190);
    lv_obj_set_style_pad_all(detail_body, 3, 0);
    lv_obj_set_flex_flow(detail_body, LV_FLEX_FLOW_COLUMN);
}
static void detail_refresh(lv_event_t *event) {
    (void)event;
    json_object *args = object(); putstr(args, "anchor_id", opened_anchor);
    call("anchor.open", args); json_object_put(args);
}
static void display_detail_frame(const char *path) {
    FILE *file = fopen(path, "rb");
    size_t got = file ? fread(detail_pixels, 1, sizeof(detail_pixels), file) : 0;
    if (file) fclose(file);
    if (got == sizeof(detail_pixels)) {
        lv_image_cache_drop(&detail_descriptor);
        lv_image_set_src(detail_image, &detail_descriptor);
        lv_obj_remove_flag(detail_image, LV_OBJ_FLAG_HIDDEN);
    }
}
static void update_detail_image(json_object *data) {
    display_detail_frame(str(data, "thumbnail_path"));
}
static void player_control(lv_event_t *event) {
    intptr_t op = (intptr_t)lv_event_get_user_data(event);
    if (op == 2) { operation("screen.stop"); return; }
    if (strcmp(str(status, "state"), "stopped") || boolean(status, "voice_running")) {
        message("请先结束录制或语音输入"); return;
    }
    if (video_fd < 0) { message("画面通道未就绪，请重启记忆页"); return; }
    json_object *args = object(); putstr(args, "anchor_id", opened_anchor);
    const char *state = str(field(status, "player"), "state");
    const char *action = op == 0 ? "player.start" :
        !strcmp(state, "playing") || !strcmp(state, "loading") ? "player.pause" : "player.resume";
    if (call(action, args) && op == 0) open_player();
    json_object_put(args);
}
static void player_seek(lv_event_t *event) {
    (void)event;
    json_object *current = field(status, "player");
    if (strcmp(str(current, "anchor_id"), opened_anchor) || !number(current, "duration_ms")) return;
    int value = lv_slider_get_value(player_slider), maximum = (int)number(current, "duration_ms");
    if (value >= maximum) value = maximum - 1;
    json_object *args = object();
    json_object_object_add(args, "offset_ms", json_object_new_int(value));
    call("player.seek", args); json_object_put(args);
}

static void close_player(lv_event_t *event) {
    (void)event;
    if (!player_view) return;
    operation("screen.stop");
    lv_obj_delete(player_view);
    player_view = player_image = player_rate = NULL;
    player_slider = player_text = player_pause = NULL;
    video_dirty = 0;
    if (!strncmp(pending_action, "player.", 7)) pending_id[0] = 0;
}
static void open_player(void) {
    if (player_view) return;
    player_view = lv_obj_create(screen); panel(player_view);
    lv_obj_add_flag(player_view, LV_OBJ_FLAG_FLOATING);
    lv_obj_remove_flag(player_view, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_size(player_view, 320, 240); lv_obj_set_pos(player_view, -4, -4);
    lv_obj_set_style_radius(player_view, 0, 0);
    lv_obj_set_style_pad_all(player_view, 0, 0);
    lv_obj_set_style_bg_color(player_view, lv_color_hex(0x101c2b), 0);
    lv_obj_t *back = button(player_view, "返回", 54, close_player, 0);
    lv_obj_set_pos(back, 4, 1); lv_obj_set_height(back, 28);
    player_text = label(player_view, "正在准备原始片段…", 251);
    lv_obj_set_pos(player_text, 63, 5);
    lv_obj_set_style_text_color(player_text, lv_color_hex(0xf3f7fb), 0);
    lv_label_set_long_mode(player_text, LV_LABEL_LONG_DOT);
    memset(video_pixels, 0, sizeof(video_pixels));
    memset(&video_descriptor, 0, sizeof(video_descriptor));
    video_descriptor.header.magic = LV_IMAGE_HEADER_MAGIC;
    video_descriptor.header.cf = LV_COLOR_FORMAT_RGB565;
    video_descriptor.header.w = ET_SCREEN_WIDTH; video_descriptor.header.h = ET_SCREEN_HEIGHT;
    video_descriptor.header.stride = ET_SCREEN_WIDTH * 2;
    video_descriptor.data = video_pixels; video_descriptor.data_size = sizeof(video_pixels);
    player_image = lv_image_create(player_view);
    lv_obj_set_pos(player_image, (320 - ET_SCREEN_WIDTH) / 2, 32);
    lv_image_set_src(player_image, &video_descriptor);
    player_slider = lv_slider_create(player_view);
    lv_obj_set_pos(player_slider, 13, 176); lv_obj_set_size(player_slider, 294, 9);
    lv_slider_set_range(player_slider, 0, 1);
    lv_obj_set_style_bg_color(player_slider, lv_color_hex(0x314557), LV_PART_MAIN);
    lv_obj_set_style_bg_color(player_slider, lv_color_hex(0x20c4b2), LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(player_slider, lv_color_hex(0xe5fff9), LV_PART_KNOB);
    lv_obj_add_event_cb(player_slider, player_seek, LV_EVENT_RELEASED, NULL);
    lv_obj_t *controls = row(player_view, 31);
    lv_obj_set_pos(controls, 5, 192); lv_obj_set_width(controls, 310);
    button(controls, "重播", 88, player_control, 0);
    player_pause = button(controls, "暂停", 112, player_control, 1);
    destructive(button(controls, "停止", 98, player_control, 2));
    player_rate = label(player_view, "-- fps", 302);
    lv_obj_set_pos(player_rate, 9, 223);
    lv_obj_set_style_text_color(player_rate, lv_color_hex(0x9bb3c4), 0);
    lv_label_set_long_mode(player_rate, LV_LABEL_LONG_DOT);
    font_tree(player_view);
    lv_obj_set_style_text_font(player_rate, &lv_font_montserrat_14, 0);
    video_stream[0] = 0; video_dirty = 0;
    player_controls_us = 0;
}
static void video_flushed(lv_event_t *event) {
    (void)event;
    if (player_view && video_dirty) {
        ++video_rendered; ++video_window_frames; video_dirty = 0;
    }
}
static void draw_video(lv_timer_t *unused) {
    (void)unused;
    if (video_fd < 0) return;
    json_object *player = field(status, "player");
    int updated = 0;
    for (int i = 0; i < 8; ++i) {
        ssize_t got = recv(video_fd, video_packet, sizeof(video_packet), MSG_DONTWAIT | MSG_TRUNC);
        if (got < 0) break;
        et_screen_frame frame;
        if (!player_view || !player_image || !et_screen_parse(video_packet, (size_t)got, &frame) ||
            strcmp(str(player, "anchor_id"), opened_anchor) ||
            !et_screen_matches(&frame, str(player, "stream_id"))) continue;
        const char *stream = str(player, "stream_id");
        if (strcmp(video_stream, stream)) {
            copy(video_stream, sizeof(video_stream), stream);
            video_received = video_rendered = video_discarded = video_window_frames = 0;
            video_epoch = video_window = now_us(); video_sequence = frame.sequence;
        } else if (video_received && frame.sequence <= video_sequence) continue;
        video_sequence = frame.sequence; ++video_received;
        if (updated || video_dirty) ++video_discarded;
        memcpy(video_pixels, frame.pixels, sizeof(video_pixels)); updated = 1;
    }
    if (updated) {
        video_dirty = 1;
        lv_image_cache_drop(&video_descriptor);
        lv_obj_invalidate(player_image);
    }
    uint64_t now = now_us();
    if (*video_stream && now - video_window >= 1000000) {
        unsigned fps_milli = (unsigned)(video_window_frames * 1000000000ULL / (now - video_window));
        char report[1024];
        snprintf(report, sizeof(report), "{\"stream_id\":\"%s\",\"frames_received\":%llu,\"frames_rendered\":%llu,"
                 "\"frames_discarded\":%llu,\"fps_milli\":%u,\"elapsed_us\":%llu}", video_stream,
                 (unsigned long long)video_received, (unsigned long long)video_rendered,
                 (unsigned long long)video_discarded, fps_milli, (unsigned long long)(now - video_epoch));
        pthread_mutex_lock(&mutex); copy(display_report, sizeof(display_report), report); pthread_mutex_unlock(&mutex);
        if (player_rate) {
            char text[100]; snprintf(text, sizeof(text), "%u.%u fps",
                                    fps_milli / 1000, (fps_milli % 1000) / 100);
            set_text(player_rate, text);
        }
        video_window_frames = 0; video_window = now;
    }
}

static void export_click(lv_event_t *event) {
    (void)event;
    json_object *args = object(); putstr(args, "anchor_id", opened_anchor);
    call("export", args); json_object_put(args);
}
static void compare_click(lv_event_t *event) {
    (void)event;
    if (!anchor_model) return;
    uint8_t *pixels = *compare_anchor ? compare_second : compare_original;
    FILE *file = fopen(str(anchor_model, "thumbnail_path"), "rb");
    size_t count = file ? fread(pixels, 1, THUMB_BYTES, file) : 0;
    if (file) fclose(file);
    if (count != THUMB_BYTES) { message("请先取得这一步的原始画面"); return; }
    if (!*compare_anchor) {
        copy(compare_anchor, sizeof(compare_anchor), opened_anchor);
        snprintf(compare_caption, sizeof(compare_caption), "画面 A · %s · %02llu:%02llu",
                 str(anchor_model, "title"),
                 (unsigned long long)(number(anchor_model, "seconds") / 60),
                 (unsigned long long)(number(anchor_model, "seconds") % 60));
        message("已设为画面 A；再打开另一步点对照");
        return;
    }
    if (!strcmp(compare_anchor, opened_anchor)) {
        compare_anchor[0] = 0; message("已清除画面 A"); return;
    }
    char caption[600];
    snprintf(caption, sizeof(caption), "画面 B · %s · %02llu:%02llu",
             str(anchor_model, "title"),
             (unsigned long long)(number(anchor_model, "seconds") / 60),
             (unsigned long long)(number(anchor_model, "seconds") % 60));
    reader("两步原始画面对照");
    lv_obj_t *images = row(detail_body, 75);
    for (unsigned i = 0; i < 2; ++i) {
        const uint8_t *source = i ? compare_second : compare_original;
        for (unsigned y = 0; y < 72; ++y) for (unsigned x = 0; x < 128; ++x) {
            size_t from = ((y * 90 / 72) * 160 + x * 160 / 128) * 2;
            size_t to = (y * 128 + x) * 2;
            compare_small[i][to] = source[from]; compare_small[i][to + 1] = source[from + 1];
        }
        lv_image_dsc_t *d = &compare_descriptors[i];
        memset(d, 0, sizeof(*d)); d->header.magic = LV_IMAGE_HEADER_MAGIC;
        d->header.cf = LV_COLOR_FORMAT_RGB565;
        d->header.w = 128; d->header.h = 72; d->header.stride = 256;
        d->data = compare_small[i]; d->data_size = sizeof(compare_small[i]);
        lv_image_cache_drop(d);
        lv_obj_t *image = lv_image_create(images); lv_image_set_src(image, d);
    }
    label(detail_body, compare_caption, 285); label(detail_body, caption, 285);
    label(detail_body, "左 A，右 B。说明与原始画面分别保留。", 285);
    font_tree(detail);
}

static void detail_tool(lv_event_t *event) {
    intptr_t op = (intptr_t)lv_event_get_user_data(event);
    close_dialog(NULL);
    if (op == 0) detail_refresh(NULL);
    else if (op == 1) compare_click(NULL);
    else export_click(NULL);
}
static void detail_more(lv_event_t *event) {
    (void)event;
    if (dialog || *pending_id) { message("请等待当前操作完成"); return; }
    dialog = lv_obj_create(screen); panel(dialog);
    lv_obj_add_flag(dialog, LV_OBJ_FLAG_FLOATING);
    lv_obj_set_pos(dialog, 0, 0); lv_obj_set_size(dialog, 320, 240);
    lv_obj_set_style_pad_all(dialog, 12, 0);
    lv_obj_set_style_pad_row(dialog, 9, 0);
    lv_obj_set_flex_flow(dialog, LV_FLEX_FLOW_COLUMN);
    label(dialog, "步骤工具", 290);
    button(dialog, "刷新详情和原图", 290, detail_tool, 0);
    button(dialog, *compare_anchor ? "与画面 A 对照" : "设为对照画面 A", 290, detail_tool, 1);
    button(dialog, "导出证据到 SD 卡", 290, detail_tool, 2);
    button(dialog, "返回详情", 290, close_dialog, 0);
    editor = dialog_voice = edit_hint = NULL;
    font_tree(dialog);
}

static void render_anchor(json_object *data) {
    if (detail && anchor_model && !strcmp(opened_anchor, str(data, "id")) &&
        number(anchor_model, "revision") == number(data, "revision") &&
        !strcmp(str(anchor_model, "note"), str(data, "note")) &&
        !strcmp(str(anchor_model, "asr_text"), str(data, "asr_text")) &&
        !strcmp(str(anchor_model, "caption"), str(data, "caption"))) {
        update_detail_image(data);
        lv_label_set_text(detail_media_label, str(data, "media_message"));
        json_object_put(anchor_model); anchor_model = json_object_get(data);
        detail_pending = boolean(data, "pending"); detail_retry = now_us();
        return;
    }
    char anchor_id[100]; copy(anchor_id, sizeof(anchor_id), str(data, "id"));
    char title[100];
    uint64_t seconds = number(data, "seconds");
    snprintf(title, sizeof(title), "步骤 %02llu:%02llu",
             (unsigned long long)(seconds / 60), (unsigned long long)(seconds % 60));
    reader(title);
    lv_obj_set_height(detail_body, 150);
    lv_obj_set_flex_align(detail_body, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START);
    copy(opened_anchor, sizeof(opened_anchor), anchor_id);
    anchor_model = json_object_get(data);
    detail_image = lv_image_create(detail_body);
    lv_obj_add_flag(detail_image, LV_OBJ_FLAG_HIDDEN);
    memset(&detail_descriptor, 0, sizeof(detail_descriptor));
    detail_descriptor.header.magic = LV_IMAGE_HEADER_MAGIC;
    detail_descriptor.header.cf = LV_COLOR_FORMAT_RGB565;
    detail_descriptor.header.w = 160; detail_descriptor.header.h = 90;
    detail_descriptor.header.stride = 320;
    detail_descriptor.data_size = sizeof(detail_pixels);
    detail_descriptor.data = detail_pixels;
    update_detail_image(data);
    lv_obj_t *play_controls = row(detail, 33);
    lv_obj_set_pos(play_controls, 0, 195); lv_obj_set_width(play_controls, 307);
    button(play_controls, "回放", 98, player_control, 0);
    button(play_controls, "改笔记", 98, project_editor, 3);
    button(play_controls, "更多", 98, detail_more, 0);
    detail_media_label = label(detail_body, str(data, "media_message"), 285);
    if (*str(data, "note")) {
        label(detail_body, "你的笔记", 285); label(detail_body, str(data, "note"), 285);
    }
    if (*str(data, "asr_text")) {
        label(detail_body, "当时的口述", 285); label(detail_body, str(data, "asr_text"), 285);
    }
    if (*str(data, "caption")) {
        label(detail_body, "自动描述 · 以原图为准", 285); label(detail_body, str(data, "caption"), 285);
    }
    if (!*str(data, "note") && !*str(data, "asr_text") && !*str(data, "caption"))
        label(detail_body, "本步骤没有文字说明，可查看原始画面。", 285);
    detail_pending = boolean(data, "pending");
    detail_retry = now_us();
    font_tree(detail);
}
static void render_history(json_object *data) {
    lv_obj_clean(history_page);
    lv_obj_t *controls = row(history_page, 32);
    button(controls, *history_session ? "列表" : "刷新记录", *history_session ? 70 : 140, history_control, 0);
    if (*history_session) {
        button(controls, "进度", 70, history_control, 3);
        button(controls, "继续", 70, history_control, 4);
        button(controls, "删除", 70, history_control, 5);
        copy(history_title, sizeof(history_title), str(field(data, "session"), "title"));
    }
    lv_obj_t *list = lv_obj_create(history_page);
    panel(list);
    lv_obj_set_width(list, LV_PCT(100));
    lv_obj_set_flex_grow(list, 1);
    lv_obj_set_style_pad_all(list, 1, 0);
    lv_obj_set_style_pad_row(list, 3, 0);
    lv_obj_set_flex_flow(list, LV_FLEX_FLOW_COLUMN);
    json_object *items = field(data, "items"), *session = field(data, "session");
    for (size_t i = 0; items && i < json_object_array_length(items); ++i) {
        json_object *item = json_object_array_get_idx(items, i);
        char text[1100];
        if (*history_session) {
            uint64_t started = number(session, "started_us");
            uint64_t source = number(item, "source_time_us");
            uint64_t seconds = source >= started ? (source - started) / 1000000 : 0;
            const char *description = *str(item, "note") ? str(item, "note") :
                 *str(item, "asr_text") ? str(item, "asr_text") :
                 *str(item, "caption") ? str(item, "caption") :
                 !strcmp(str(item, "kind"), "voice") ? "口述正在识别" : "操作标记";
            snprintf(text, sizeof(text), "%02llu:%02llu  %s",
                (unsigned long long)(seconds / 60), (unsigned long long)(seconds % 60), description);
        } else {
            uint64_t end = number(item, "ended_us");
            if (!end) end = number(item, "last_us");
            uint64_t started = number(item, "started_us");
            uint64_t seconds = end >= started ? (end - started) / 1000000 : 0;
            snprintf(text, sizeof(text), "%s\n%llu 分 %llu 秒 · %llu 个步骤",
                *str(item, "title") ? str(item, "title") : str(item, "project_name"),
                (unsigned long long)(seconds / 60), (unsigned long long)(seconds % 60),
                (unsigned long long)number(item, "steps"));
        }
        char *id = strdup(str(item, "id"));
        lv_obj_t *item_button = button(list, text, 285, NULL, 0);
        lv_obj_set_style_bg_color(item_button, lv_color_hex(0xe9f4f1), 0);
        lv_obj_set_style_text_color(item_button, lv_color_hex(0x184b45), 0);
        lv_obj_set_style_text_color(item_button, lv_color_hex(0xffffff), LV_STATE_PRESSED);
        lv_obj_set_height(item_button, 54);
        lv_obj_t *caption = lv_obj_get_child(item_button, 0);
        lv_obj_set_width(caption, 270);
        lv_label_set_long_mode(caption, LV_LABEL_LONG_DOT);
        lv_obj_add_event_cb(item_button, history_item, LV_EVENT_CLICKED, id);
        lv_obj_add_event_cb(item_button, free_button_data, LV_EVENT_DELETE, id);
    }
    if (!items || !json_object_array_length(items))
        label(list, *history_session ? "还没有步骤。新记录可稍后刷新。" : "这个项目还没有记录。", 285);
    history_more = boolean(data, "more");
    history_offset = (int)number(data, "offset");
    controls = row(history_page, 29);
    lv_obj_t *previous = button(controls, "上一页", 95, history_control, 1);
    lv_obj_t *next = button(controls, "下一页", 95, history_control, 2);
    enabled(previous, history_offset > 0); enabled(next, history_more);
    char page[50]; snprintf(page, sizeof(page), "第 %d 页", history_offset / 8 + 1);
    label(controls, page, 93);
    font_tree(history_page);
}
static void tab_changed(lv_event_t *event) {
    (void)event;
    if (lv_tabview_get_tab_active(tabs) == 2) request_history(1);
}


static void query_result_click(lv_event_t *event) {
    if (*pending_id) { message("正在处理，请稍候"); return; }
    const char *id = lv_event_get_user_data(event);
    copy(opened_anchor, sizeof(opened_anchor), id);
    json_object *args = object(); putstr(args, "anchor_id", id);
    call("anchor.open", args); json_object_put(args);
}
static void speak_click(lv_event_t *event) {
    (void)event;
    json_object *args = object(); putstr(args, "text", spoken_answer);
    call("speak", args); json_object_put(args);
}
static void render_query(json_object *data) {
    const char *text = str(data, "transcript");
    lv_label_set_text(question_label, *text ? text : "查询结果");
    lv_label_set_text(result_label, str(data, "answer"));
    copy(spoken_answer, sizeof(spoken_answer), str(data, "answer"));
    enabled(speak_button, *spoken_answer != 0);
    lv_obj_clean(query_list);
    json_object *hits = field(data, "hits");
    for (size_t i = 0; hits && i < json_object_array_length(hits); ++i) {
        json_object *hit = json_object_array_get_idx(hits, i);
        char caption[1000];
        uint64_t seconds = number(hit, "seconds");
        snprintf(caption, sizeof(caption), "%zu. %02llu:%02llu  %s", i + 1,
                 (unsigned long long)(seconds / 60), (unsigned long long)(seconds % 60),
                 str(hit, "label"));
        lv_obj_t *item = button(query_list, caption, 272, NULL, 0);
        lv_obj_set_height(item, 55);
        lv_obj_t *text_label = lv_obj_get_child(item, 0);
        lv_obj_set_width(text_label, 260);
        lv_label_set_long_mode(text_label, LV_LABEL_LONG_DOT);
        char *id = strdup(str(hit, "id"));
        lv_obj_add_event_cb(item, query_result_click, LV_EVENT_CLICKED, id);
        lv_obj_add_event_cb(item, free_button_data, LV_EVENT_DELETE, id);
    }
    font_tree(query_list);
}

static void consume_reply(json_object *reply) {
    if (!*pending_id || strcmp(str(reply, "id"), pending_id)) return;
    pending_id[0] = 0;
    if (!boolean(reply, "ok")) {
        message(str(reply, "error"));
        if (player_view && player_text) set_text(player_text, str(reply, "error"));
        if (dialog && edit_hint) lv_label_set_text(edit_hint, str(reply, "error"));
        return;
    }
    json_object *data = field(reply, "data");
    if (!strcmp(pending_action, "project.create") || !strcmp(pending_action, "project.rename")) {
        close_dialog(NULL);
        select_project(str(data, "id"));
        message("项目已保存，可直接开始记忆");
        lv_tabview_set_active(tabs, 0, LV_ANIM_OFF);
    } else if (!strcmp(pending_action, "voice") && !strcmp(str(data, "mode"), "project.name")) {
        if (dialog && editor && edit_mode == 0) lv_textarea_set_text(editor, str(data, "transcript"));
        message("名称已识别，请确认后保存");
    } else if (!strcmp(pending_action, "voice") && !strcmp(str(data, "mode"), "note")) {
        if (dialog && editor && edit_mode == 2) {
            if (*lv_textarea_get_text(editor)) lv_textarea_add_text(editor, "\n");
            lv_textarea_add_text(editor, str(data, "transcript"));
            if (edit_hint) lv_label_set_text(edit_hint, "已识别，请确认后保存");
        }
        message("口述已识别，确认后保存");
    } else if (!strcmp(pending_action, "anchor.note")) {
        close_dialog(NULL);
        json_object *args = object(); putstr(args, "anchor_id", str(data, "id"));
        call("anchor.open", args); json_object_put(args); message("笔记已保存");
    } else if (!strcmp(pending_action, "session.delete")) {
        close_dialog(NULL); close_detail(NULL); request_history(1);
        message(!strcmp(str(data, "source_deletion"), "done") ? "会话和原片已删除" : "会话已删除，原片将在重连后清理");
    } else if (!strcmp(pending_action, "export")) {
        char saved[320]; snprintf(saved, sizeof(saved), "已导出到 SD：%s", str(data, "path"));
        reader("导出完成"); label(detail_body, saved, 285); font_tree(detail); message("证据包已保存到 SD 卡");
    } else if (!strcmp(pending_action, "query") || !strcmp(pending_action, "voice")) {
        if (dialog && edit_mode == 1) close_dialog(NULL);
        render_query(data); lv_tabview_set_active(tabs, 3, LV_ANIM_OFF); message("");
    } else if (!strncmp(pending_action, "player.", 7)) {
        message("");
    } else if (!strcmp(pending_action, "speak")) {
        message("正在朗读回答");
    } else if (!strcmp(pending_action, "history.sessions") || !strcmp(pending_action, "history.anchors")) {
        render_history(data); message("");
    } else if (!strcmp(pending_action, "anchor.open")) {
        render_anchor(data); message("");
    } else if (!strcmp(pending_action, "session.summary")) {
        reader("上次做到哪里"); label(detail_body, str(data, "summary"), 285); font_tree(detail); message("");
    } else if (!strcmp(pending_action, "session.continue")) {
        select_project(str(data, "project_id"));
        lv_tabview_set_active(tabs, 0, LV_ANIM_OFF); message("已开始后续记录");
    } else message("已完成");
}
static void refresh(lv_timer_t *unused) {
    (void)unused;
    char hint[320];
    pthread_mutex_lock(&mutex);
    json_object *next = shared_status ? json_object_get(shared_status) : NULL;
    if (status) json_object_put(status);
    copy(hint, sizeof(hint), notice);
    if (failed_send) { pending_id[0] = 0; failed_send = 0; }
    pthread_mutex_unlock(&mutex);
    status = next;
    if (*pending_id && now_us() - pending_since > (!strcmp(pending_action, "export") ? 180000000ULL : !strcmp(pending_action, "voice") ? 90000000ULL : 20000000ULL)) {
        pending_id[0] = 0; message("后台未及时回复，请检查连接后重试");
    }
    if (status) consume_reply(field(status, "app_reply"));
    if (*entry_target && boolean(status, "connected") && !*pending_id) {
        if (!strcmp(entry_target, "project")) lv_tabview_set_active(tabs, 1, LV_ANIM_OFF);
        else if (!strcmp(entry_target, "history")) {
            lv_tabview_set_active(tabs, 2, LV_ANIM_OFF); request_history(1);
        } else if (!strcmp(entry_target, "ask")) lv_tabview_set_active(tabs, 3, LV_ANIM_OFF);
        else if (!strncmp(entry_target, "anchor:", 7)) {
            json_object *args = object(); putstr(args, "anchor_id", entry_target + 7);
            call("anchor.open", args); json_object_put(args);
        }
        entry_target[0] = 0;
    }
    if (detail && detail_pending && !*pending_id && now_us() - detail_retry > 3000000) {
        detail_retry = now_us();
        detail_refresh(NULL);
    }
    json_object *player = field(status, "player");
    if (player_view && player_text && !strcmp(str(player, "anchor_id"), opened_anchor)) {
        uint64_t position = number(player, "position_ms"), duration = number(player, "duration_ms");
        char playback[220];
        snprintf(playback, sizeof(playback), "%02llu:%02llu / %02llu:%02llu  %s",
                 (unsigned long long)(position / 60000), (unsigned long long)(position / 1000 % 60),
                 (unsigned long long)(duration / 60000), (unsigned long long)(duration / 1000 % 60),
                 str(player, "message"));
        set_text(player_text, playback);
        if (!lv_obj_has_state(player_slider, LV_STATE_PRESSED) &&
            (now_us() - player_controls_us >= 500000 || strcmp(str(player, "state"), "playing"))) {
            lv_slider_set_range(player_slider, 0, duration ? (int)duration : 1);
            lv_slider_set_value(player_slider, (int)position, LV_ANIM_OFF);
            player_controls_us = now_us();
        }
        set_text(lv_obj_get_child(player_pause, 0), !strcmp(str(player, "state"), "paused") ? "继续" : "暂停");
    }
    /* The opaque player covers the workbench. Updating hidden widgets causes
     * full-height dirty ranges in fbtft, wasting the SPI refresh budget. */
    if (player_view) return;
    const char *state = str(status, "state");
    char line[200];
    snprintf(line, sizeof(line), "%s%s · %s", boolean(status, "synthetic") ? "模拟 · " : "",
             !status ? "服务未连接" : !strcmp(state, "recording") ? "记录中" :
             !strcmp(state, "paused") ? "已暂停" : "已停止",
             boolean(status, "connected") ? "在线" : "离线");
    set_text(status_label, line);
    lv_obj_set_style_text_color(status_label, lv_color_hex(!strcmp(state, "recording") ? 0xb53d4b :
        boolean(status, "connected") ? 0x127d73 : 0x8c672e), 0);
    if (!*selected_project) copy(selected_project, sizeof(selected_project), str(status, "selected_project_id"));
    json_object *session = field(status, "session");
    int active = !strcmp(state, "recording") || !strcmp(state, "paused");
    if (active) copy(selected_project, sizeof(selected_project), str(session, "project_id"));
    json_object *projects = field(status, "projects");
    char choices[4096] = "";
    unsigned count = projects ? (unsigned)json_object_array_length(projects) : 0, selected = 0;
    if (count > MAX_PROJECTS) count = MAX_PROJECTS;
    for (unsigned i = 0; i < count; ++i) {
        json_object *p = json_object_array_get_idx(projects, i);
        if (!strcmp(str(p, "id"), selected_project)) selected = i;
        if (i) strncat(choices, "\n", sizeof(choices) - strlen(choices) - 1);
        char short_name[100];
        copy(short_name, sizeof(short_name), str(p, "name"));
        /* JSON names are valid UTF-8. Shorten only on a complete code point. */
        size_t n = strlen(short_name);
        if (n > 60) { n = 60; while (n && ((unsigned char)short_name[n] & 0xc0) == 0x80) --n; short_name[n] = 0; }
        strncat(choices, short_name, sizeof(choices) - strlen(choices) - 1);
    }
    if (!count) copy(choices, sizeof(choices), "默认工作记录 · 可直接开始");
    rendering = 1;
    if (strcmp(old_choices, choices)) {
        lv_dropdown_set_options(project_select, choices);
        copy(old_choices, sizeof(old_choices), choices);
    }
    lv_dropdown_set_selected(project_select, selected);
    enabled(project_select, !active);
    rendering = 0;
    if (!*selected_project && count) copy(selected_project, sizeof(selected_project), str(json_object_array_get_idx(projects, selected), "id"));
    set_text(project_label, selected_name());
    set_text(record_project_label, selected_name());
    uint64_t started = number(session, "started_us"), current = number(status, "source_time_us");
    uint64_t end = active ? current : number(session, "ended_us");
    uint64_t seconds = started && end >= started ? (end - started) / 1000000 : 0;
    snprintf(line, sizeof(line), "%02llu:%02llu:%02llu",
             (unsigned long long)(seconds / 3600), (unsigned long long)(seconds / 60 % 60),
             (unsigned long long)(seconds % 60));
    set_text(elapsed_label, line);
    set_text(preview_hint, !active ? "开始后持续收音" : !strcmp(state, "paused") ? "已暂停采集" : "正在保存声画");
    uint64_t stamp = number(status, "preview_time_us");
    if (active && stamp && stamp != live_stamp) {
        uint8_t pixels[THUMB_BYTES];
        FILE *file = fopen(str(status, "preview_path"), "rb");
        size_t got = file ? fread(pixels, 1, sizeof(pixels), file) : 0;
        if (file) fclose(file);
        if (got == sizeof(pixels)) {
            for (unsigned y = 0; y < 72; ++y) for (unsigned x = 0; x < 128; ++x) {
                size_t from = ((y * 90 / 72) * 160 + x * 160 / 128) * 2;
                size_t to = (y * 128 + x) * 2;
                live_data[to] = pixels[from]; live_data[to + 1] = pixels[from + 1];
            }
            lv_image_cache_drop(&live_descriptor);
            lv_image_set_src(live_image, &live_descriptor);
            lv_obj_remove_flag(live_image, LV_OBJ_FLAG_HIDDEN);
            live_stamp = stamp;
        }
    } else if (!active) {
        lv_obj_add_flag(live_image, LV_OBJ_FLAG_HIDDEN);
        live_stamp = 0;
    }
    set_text(lv_obj_get_child(pause_button, 0), !strcmp(state, "paused") ? "继续记忆" : "暂停记忆");
    enabled(start_button, !active);
    enabled(mark_button, !strcmp(state, "recording"));
    enabled(pause_button, active);
    enabled(stop_button, active);
    int listening = boolean(status, "listening");
    enabled(voice_button, listening || (!active && boolean(status, "connected")));
    if (voice_button) set_text(lv_obj_get_child(voice_button, 0), listening ? "结束说话" : "语音提问");
    if (dialog_voice) set_text(lv_obj_get_child(dialog_voice, 0), listening ? "结束" : "语音");
    if (listening) set_text(status_label, "正在听 · 点击结束说话");
    enabled(start_button, !active && !boolean(status, "voice_running") && !boolean(status, "playback_active"));
    if (*pending_id && !strcmp(pending_action, "export")) {
        json_object *progress = field(status, "export_progress");
        int percent = progress ? json_object_get_int(progress) : -1;
        if (percent >= 0) snprintf(hint, sizeof(hint), "正在导出到 SD · %d%%", percent);
        else copy(hint, sizeof(hint), "正在准备原片与证据包…");
    }
    const char *error = str(status, "error");
    set_text(notice_label, *error ? error : hint);
    json_object *query = field(status, "query");
    char query_time[100];
    snprintf(query_time, sizeof(query_time), "%llu", (unsigned long long)number(query, "received_us"));
    if (number(query, "received_us") && strcmp(query_time, last_query)) {
        copy(last_query, sizeof(last_query), query_time);
        render_query(query);
        lv_tabview_set_active(tabs, 3, LV_ANIM_OFF);
    }
}
static void back_click(lv_event_t *event) {
    (void)event;
    lv_lib_pm_OpenPrePage(&page_manager);
}
void ui_EchoTracePage_init(void) {
    copy(entry_target, sizeof(entry_target), getenv("ECHOTRACE_OPEN_APP"));
    selected_project[0] = pending_id[0] = old_choices[0] = last_query[0] = 0;
    dialog = editor = NULL;
    queued = failed_send = rendering = 0;
    notice[0] = 0;
    player_view = player_image = player_rate = NULL;
    video_stream[0] = display_report[0] = 0;
    screen = lv_obj_create(NULL);
    panel(screen); lv_obj_set_style_radius(screen, 0, 0);
    lv_obj_set_style_bg_color(screen, lv_color_hex(0xf4f7fa), 0);
    lv_obj_set_style_pad_all(screen, 4, 0);
    lv_obj_set_style_pad_row(screen, 3, 0);
    lv_obj_set_flex_flow(screen, LV_FLEX_FLOW_COLUMN);
    lv_obj_t *header = row(screen, 28);
    lv_obj_set_height(button(header, "返回", 52, back_click, 0), 27);
    status_label = label(header, "正在连接…", 245);
    lv_obj_set_style_text_color(status_label, lv_color_hex(0x183f32), 0);
    tabs = lv_tabview_create(screen);
    panel(tabs); lv_obj_set_style_bg_opa(tabs, LV_OPA_TRANSP, 0);
    lv_obj_set_style_pad_all(tabs, 0, 0);
    lv_obj_set_width(tabs, LV_PCT(100));
    lv_obj_set_flex_grow(tabs, 1);
    lv_tabview_set_tab_bar_size(tabs, 27);
    lv_obj_t *record = lv_tabview_add_tab(tabs, "记录");
    lv_obj_t *project = lv_tabview_add_tab(tabs, "项目");
    history_page = lv_tabview_add_tab(tabs, "历史");
    lv_obj_t *memory = lv_tabview_add_tab(tabs, "提问");
    lv_obj_t *pages[] = {record, project, history_page, memory};
    for (int i = 0; i < 4; ++i) {
        panel(pages[i]);
        lv_obj_set_style_pad_all(pages[i], 2, 0);
        lv_obj_set_style_pad_row(pages[i], 3, 0);
        lv_obj_set_flex_flow(pages[i], LV_FLEX_FLOW_COLUMN);
    }
    lv_obj_t *bar = lv_tabview_get_tab_bar(tabs);
    panel(bar); lv_obj_set_style_radius(bar, 7, 0);
    lv_obj_set_style_bg_color(bar, lv_color_hex(0xe6edf2), 0);
    for (uint32_t i = 0; i < lv_obj_get_child_count(bar); ++i) {
        lv_obj_t *tab = lv_obj_get_child(bar, i);
        lv_obj_set_style_radius(tab, 6, 0);
        lv_obj_set_style_bg_opa(tab, LV_OPA_TRANSP, 0);
        lv_obj_set_style_bg_color(tab, lv_color_hex(0x127d73), LV_STATE_CHECKED);
        lv_obj_set_style_bg_opa(tab, LV_OPA_COVER, LV_STATE_CHECKED);
        lv_obj_set_style_text_color(tab, lv_color_hex(0x4c6272), 0);
        lv_obj_set_style_text_color(tab, lv_color_hex(0xffffff), LV_STATE_CHECKED);
        lv_obj_set_style_border_width(tab, 0, LV_STATE_CHECKED);
    }
    project_select = lv_dropdown_create(project);
    panel(project_select);
    lv_obj_set_style_bg_color(project_select, lv_color_hex(0xe9f4f1), 0);
    lv_obj_set_size(project_select, LV_PCT(100), 32);
    lv_dropdown_set_options(project_select, "默认工作记录 · 可直接开始");
    lv_obj_add_event_cb(project_select, project_changed, LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_t *live = row(record, 74);
    lv_obj_t *preview = lv_obj_create(live); panel(preview);
    lv_obj_set_size(preview, 128, 72); lv_obj_set_style_pad_all(preview, 0, 0);
    lv_obj_remove_flag(preview, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(preview, lv_color_hex(0x182d3e), 0);
    lv_obj_t *placeholder = label(preview, "工作台预览", 118);
    lv_obj_set_style_text_color(placeholder, lv_color_hex(0xb1c8d6), 0);
    lv_obj_set_style_text_align(placeholder, LV_TEXT_ALIGN_CENTER, 0); lv_obj_center(placeholder);
    live_image = lv_image_create(preview); lv_obj_set_pos(live_image, 0, 0);
    lv_obj_add_flag(live_image, LV_OBJ_FLAG_HIDDEN);
    memset(&live_descriptor, 0, sizeof(live_descriptor));
    live_descriptor.header.magic = LV_IMAGE_HEADER_MAGIC;
    live_descriptor.header.cf = LV_COLOR_FORMAT_RGB565;
    live_descriptor.header.w = 128; live_descriptor.header.h = 72;
    live_descriptor.header.stride = 256;
    live_descriptor.data_size = sizeof(live_data);
    live_descriptor.data = live_data;
    lv_obj_t *info = lv_obj_create(live);
    panel(info); lv_obj_remove_flag(info, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_size(info, 164, 74);
    lv_obj_set_style_pad_all(info, 1, 0);
    lv_obj_set_style_border_width(info, 0, 0);
    lv_obj_set_flex_flow(info, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(info, 2, 0);
    record_project_label = label(info, "工作台记录", 158);
    lv_label_set_long_mode(record_project_label, LV_LABEL_LONG_DOT);
    elapsed_label = label(info, "会话 00:00:00", 158);
    preview_hint = label(info, "开始后显示预览", 158);
    /* Project + clock already fill this 72px card at the Chinese font height. */
    lv_obj_add_flag(preview_hint, LV_OBJ_FLAG_HIDDEN);
    live_stamp = 0;
    lv_obj_t *actions = row(record, 32);
    start_button = button(actions, "开始记忆", 145, operation_click, 0);
    mark_button = button(actions, "记这一步", 145, operation_click, 1);
    actions = row(record, 32);
    pause_button = button(actions, "暂停/继续", 145, operation_click, 2);
    stop_button = button(actions, "结束记忆", 145, operation_click, 3);
    destructive(stop_button);
    project_label = label(project, "工作台记录", 300);
    lv_label_set_long_mode(project_label, LV_LABEL_LONG_DOT);
    actions = row(project, 34);
    button(actions, "新建项目", 145, project_editor, 0);
    button(actions, "修改名称", 145, project_editor, 1);
    button(project, "用此项目开始记忆", 295, operation_click, 0);
    label(project, "语音可输入中文，保存后即可开始。", 300);
    button(history_page, "查看项目历史", 295, history_control, 0);
    label(history_page, "记录结束后，可在这里查看步骤并接着工作。", 295);
    lv_obj_add_event_cb(tabs, tab_changed, LV_EVENT_VALUE_CHANGED, NULL);
    history_session[0] = opened_anchor[0] = compare_anchor[0] = 0;
    history_offset = history_more = detail_pending = 0;
    detail = detail_body = detail_image = NULL;
    lv_obj_t *query_controls = row(memory, 32);
    voice_button = button(query_controls, "语音提问", 145, voice_click, 0);
    button(query_controls, "输入问题", 145, project_editor, 2);
    lv_obj_t *results = lv_obj_create(memory);
    panel(results);
    lv_obj_set_width(results, LV_PCT(100)); lv_obj_set_flex_grow(results, 1);
    lv_obj_set_style_pad_all(results, 3, 0);
    lv_obj_set_flex_flow(results, LV_FLEX_FLOW_COLUMN);
    question_label = label(results, "未录制时也可语音提问。", 285);
    result_label = label(results, "回答和候选证据在这里显示。", 285);
    speak_button = button(results, "朗读回答", 280, speak_click, 0);
    enabled(speak_button, 0);
    query_list = lv_obj_create(results);
    panel(query_list);
    lv_obj_set_size(query_list, 285, LV_SIZE_CONTENT);
    lv_obj_set_style_pad_all(query_list, 1, 0);
    lv_obj_set_flex_flow(query_list, LV_FLEX_FLOW_COLUMN);
    spoken_answer[0] = 0; dialog_voice = NULL;
    notice_label = label(screen, "", 307);
    lv_obj_set_height(notice_label, 19);
    lv_label_set_long_mode(notice_label, LV_LABEL_LONG_DOT);
    font_tree(screen);
    lv_obj_set_style_text_font(elapsed_label, &lv_font_montserrat_20, 0);
    set_text(elapsed_label, "00:00:00");
    const char *control = getenv("ECHOTRACE_SOCKET");
    if (!control || !*control) control = access("/run/echotrace-sd.sock", F_OK) == 0 ?
                                           "/run/echotrace-sd.sock" : "/run/echotrace/control.sock";
    if (snprintf(video_socket, sizeof(video_socket), "%s.frames", control) < (int)sizeof(video_socket))
        video_fd = et_screen_receiver(video_socket);
    if (video_fd < 0) message("画面通道未就绪，请重启记忆页面");
    lv_display_t *display = lv_display_get_default();
    lv_timer_set_period(lv_display_get_refr_timer(display), 16);
    lv_display_add_event_cb(display, video_flushed, LV_EVENT_REFR_READY, NULL);
    video_timer = lv_timer_create(draw_video, 8, NULL);
    lv_scr_load_anim(screen, LV_SCR_LOAD_ANIM_MOVE_RIGHT, 100, 0, true);
    running = 1;
    worker_started = pthread_create(&worker, NULL, worker_main, NULL) == 0;
    if (!worker_started) { running = 0; message("无法启动状态线程"); }
    timer = lv_timer_create(refresh, 150, NULL);
}
void ui_EchoTracePage_deinit(void) {
    if (timer) { lv_timer_delete(timer); timer = NULL; }
    if (video_timer) { lv_timer_delete(video_timer); video_timer = NULL; }
    lv_display_t *display = lv_display_get_default();
    lv_display_remove_event_cb_with_user_data(display, video_flushed, NULL);
    lv_timer_set_period(lv_display_get_refr_timer(display), LV_DEF_REFR_PERIOD);
    if (video_fd >= 0) { close(video_fd); unlink(video_socket); video_fd = -1; }
    close_player(NULL);
    const char *play_state = str(field(status, "player"), "state");
    if (!strcmp(str(status, "state"), "stopped") &&
        (!strcmp(play_state, "playing") || !strcmp(play_state, "loading"))) operation("screen.stop");
    int cancel_voice = boolean(status, "voice_running") ||
                       (*pending_id && !strcmp(pending_action, "voice"));
    pthread_mutex_lock(&mutex);
    running = 0;
    pthread_mutex_unlock(&mutex);
    if (worker_started) { pthread_join(worker, NULL); worker_started = 0; }
    if (cancel_voice) {
        json_object *cancel = object(); putstr(cancel, "op", "voice.cancel");
        char error[100] = ""; json_object *reply = exchange(cancel, error, sizeof(error));
        if (reply) json_object_put(reply); json_object_put(cancel);
    }
    pthread_mutex_lock(&mutex);
    if (shared_status) { json_object_put(shared_status); shared_status = NULL; }
    pthread_mutex_unlock(&mutex);
    if (status) { json_object_put(status); status = NULL; }
    if (anchor_model) { json_object_put(anchor_model); anchor_model = NULL; }
    detail = detail_body = detail_image = NULL;
    dialog = editor = dialog_voice = edit_hint = NULL;
}
