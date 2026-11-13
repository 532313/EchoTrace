#include "echotrace_app.h"
#include "ui.h"
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
static json_object *shared_status, *status;
static lv_obj_t *screen, *status_label, *notice_label, *tabs, *project_select[2];
static lv_obj_t *start_button, *mark_button, *pause_button, *stop_button;
static lv_obj_t *result_label, *thumb_image, *project_label, *dialog, *editor;
static lv_timer_t *timer;
static char selected_project[100], pending_id[100], pending_action[64];
static char edit_project[100], old_choices[4096], last_query[100];
static uint64_t pending_since;
static uint8_t thumb_data[THUMB_BYTES];
static lv_image_dsc_t thumb_descriptor;

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
        if (!active) break;
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
        char error[320] = "";
        json_object *request = json_object_new_object();
        json_object_object_add(request, "op", json_object_new_string("status"));
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
    int ok = !queued && strlen(text) < sizeof(command);
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
    return item;
}
static lv_obj_t *button(lv_obj_t *parent, const char *text, int width,
                        lv_event_cb_t callback, intptr_t action) {
    lv_obj_t *item = lv_button_create(parent);
    lv_obj_set_size(item, width, 32);
    lv_obj_set_style_bg_color(item, lv_color_hex(0x27634e), 0);
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
    if (dialog) { lv_obj_delete(dialog); dialog = NULL; editor = NULL; }
}
static void save_project(lv_event_t *event) {
    (void)event;
    json_object *args = object();
    putstr(args, "name", lv_textarea_get_text(editor));
    if (*edit_project) putstr(args, "project_id", edit_project);
    if (call(*edit_project ? "project.rename" : "project.create", args))
        close_dialog(NULL);
    json_object_put(args);
}
static void project_editor(lv_event_t *event) {
    if (dialog) return;
    int rename = (int)(intptr_t)lv_event_get_user_data(event);
    if (rename && !*selected_project) { message("请先选择项目"); return; }
    copy(edit_project, sizeof(edit_project), rename ? selected_project : "");
    dialog = lv_obj_create(screen);
    lv_obj_add_flag(dialog, LV_OBJ_FLAG_FLOATING);
    lv_obj_set_size(dialog, LV_PCT(100), LV_PCT(100));
    lv_obj_set_pos(dialog, 0, 0);
    lv_obj_set_style_pad_all(dialog, 4, 0);
    lv_obj_remove_flag(dialog, LV_OBJ_FLAG_SCROLLABLE);
    label(dialog, rename ? "修改名称" : "新建项目", 115);
    lv_obj_t *cancel = button(dialog, "取消", 57, close_dialog, 0);
    lv_obj_set_pos(cancel, 186, 0);
    lv_obj_t *save = button(dialog, "保存", 57, save_project, 0);
    lv_obj_set_pos(save, 249, 0);
    editor = lv_textarea_create(dialog);
    lv_obj_set_pos(editor, 0, 38);
    lv_obj_set_size(editor, 306, 38);
    lv_textarea_set_one_line(editor, true);
    lv_textarea_set_max_length(editor, 120);
    char suggested[100];
    snprintf(suggested, sizeof(suggested), "工作记录 %zu",
             (field(status, "projects") ? json_object_array_length(field(status, "projects")) : 0) + 1);
    lv_textarea_set_text(editor, rename ? selected_name() : suggested);
    lv_obj_t *keyboard = lv_keyboard_create(dialog);
    lv_obj_set_pos(keyboard, 0, 84);
    lv_obj_set_size(keyboard, 306, 139);
    lv_keyboard_set_textarea(keyboard, editor);
    font_tree(dialog);
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
static void show_thumbnail(const char *path) {
    FILE *file = *path ? fopen(path, "rb") : NULL;
    size_t count = file ? fread(thumb_data, 1, sizeof(thumb_data), file) : 0;
    if (file) fclose(file);
    if (count == sizeof(thumb_data)) {
        lv_image_cache_drop(&thumb_descriptor);
        lv_image_set_src(thumb_image, &thumb_descriptor);
        lv_obj_remove_flag(thumb_image, LV_OBJ_FLAG_HIDDEN);
    }
}
static void consume_reply(json_object *reply) {
    if (!*pending_id || strcmp(str(reply, "id"), pending_id)) return;
    pending_id[0] = 0;
    if (!boolean(reply, "ok")) { message(str(reply, "error")); return; }
    json_object *data = field(reply, "data");
    if (!strcmp(pending_action, "project.create") || !strcmp(pending_action, "project.rename")) {
        select_project(str(data, "id"));
        message("项目已保存，可直接开始记忆");
        lv_tabview_set_active(tabs, 0, LV_ANIM_OFF);
    } else message("已完成");
}
static void refresh(lv_timer_t *unused) {
    (void)unused;
    char hint[320];
    pthread_mutex_lock(&mutex);
    json_object *next = shared_status ? json_object_get(shared_status) : NULL;
    copy(hint, sizeof(hint), notice);
    if (failed_send) { pending_id[0] = 0; failed_send = 0; }
    pthread_mutex_unlock(&mutex);
    if (status) json_object_put(status);
    status = next;
    if (*pending_id && now_us() - pending_since > 20000000) {
        pending_id[0] = 0; message("后台未及时回复，请检查连接后重试");
    }
    if (status) consume_reply(field(status, "app_reply"));
    const char *state = str(status, "state");
    char line[200];
    snprintf(line, sizeof(line), "%s · %s",
             !status ? "服务未连接" : !strcmp(state, "recording") ? "记录中" :
             !strcmp(state, "paused") ? "已暂停" : "已停止",
             boolean(status, "connected") ? "在线" : "离线");
    lv_label_set_text(status_label, line);
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
        for (int i = 0; i < 2; ++i) lv_dropdown_set_options(project_select[i], choices);
        copy(old_choices, sizeof(old_choices), choices);
    }
    for (int i = 0; i < 2; ++i) {
        lv_dropdown_set_selected(project_select[i], selected);
        enabled(project_select[i], !active);
    }
    rendering = 0;
    if (!*selected_project && count) copy(selected_project, sizeof(selected_project), str(json_object_array_get_idx(projects, selected), "id"));
    lv_label_set_text(project_label, selected_name());
    enabled(start_button, !active);
    enabled(mark_button, !strcmp(state, "recording"));
    enabled(pause_button, active);
    enabled(stop_button, active);
    const char *error = str(status, "error");
    lv_label_set_text(notice_label, *error ? error : hint);
    json_object *query = field(status, "query");
    char query_time[100];
    snprintf(query_time, sizeof(query_time), "%llu", (unsigned long long)number(query, "received_us"));
    if (number(query, "received_us") && strcmp(query_time, last_query)) {
        copy(last_query, sizeof(last_query), query_time);
        lv_label_set_text(result_label, str(query, "answer"));
        show_thumbnail(str(query, "thumbnail_path"));
    }
}
static void back_click(lv_event_t *event) {
    (void)event;
    lv_lib_pm_OpenPrePage(&page_manager);
}
void ui_EchoTracePage_init(void) {
    selected_project[0] = pending_id[0] = old_choices[0] = last_query[0] = 0;
    dialog = editor = NULL;
    queued = failed_send = rendering = 0;
    notice[0] = 0;
    screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(screen, lv_color_hex(0xf3f6ef), 0);
    lv_obj_set_style_pad_all(screen, 4, 0);
    lv_obj_set_style_pad_row(screen, 3, 0);
    lv_obj_set_flex_flow(screen, LV_FLEX_FLOW_COLUMN);
    lv_obj_t *header = row(screen, 28);
    lv_obj_set_height(button(header, "返回", 52, back_click, 0), 27);
    status_label = label(header, "正在连接…", 245);
    lv_obj_set_style_text_color(status_label, lv_color_hex(0x183f32), 0);
    tabs = lv_tabview_create(screen);
    lv_obj_set_width(tabs, LV_PCT(100));
    lv_obj_set_flex_grow(tabs, 1);
    lv_tabview_set_tab_bar_size(tabs, 27);
    lv_obj_t *record = lv_tabview_add_tab(tabs, "记录");
    lv_obj_t *project = lv_tabview_add_tab(tabs, "项目");
    lv_obj_t *memory = lv_tabview_add_tab(tabs, "回看");
    lv_obj_t *pages[] = {record, project, memory};
    for (int i = 0; i < 3; ++i) {
        lv_obj_set_style_pad_all(pages[i], 2, 0);
        lv_obj_set_style_pad_row(pages[i], 3, 0);
        lv_obj_set_flex_flow(pages[i], LV_FLEX_FLOW_COLUMN);
    }
    for (int i = 0; i < 2; ++i) {
        project_select[i] = lv_dropdown_create(i ? project : record);
        lv_obj_set_size(project_select[i], LV_PCT(100), 32);
        lv_dropdown_set_options(project_select[i], "默认工作记录 · 可直接开始");
        lv_obj_add_event_cb(project_select[i], project_changed, LV_EVENT_VALUE_CHANGED, NULL);
    }
    lv_obj_t *actions = row(record, 34);
    start_button = button(actions, "开始记忆", 145, operation_click, 0);
    mark_button = button(actions, "记这一步", 145, operation_click, 1);
    actions = row(record, 34);
    pause_button = button(actions, "暂停/继续", 145, operation_click, 2);
    stop_button = button(actions, "结束记忆", 145, operation_click, 3);
    label(record, "返回首页继续记录；口述自动保存。", 300);
    project_label = label(project, "工作台记录", 300);
    actions = row(project, 34);
    button(actions, "新建项目", 145, project_editor, 0);
    button(actions, "修改名称", 145, project_editor, 1);
    button(project, "用此项目开始记忆", 295, operation_click, 0);
    label(project, "可直接保存默认中文名称，或输入字母数字。", 300);
    thumb_image = lv_image_create(memory);
    lv_obj_add_flag(thumb_image, LV_OBJ_FLAG_HIDDEN);
    memset(&thumb_descriptor, 0, sizeof(thumb_descriptor));
    thumb_descriptor.header.magic = LV_IMAGE_HEADER_MAGIC;
    thumb_descriptor.header.cf = LV_COLOR_FORMAT_RGB565;
    thumb_descriptor.header.w = 160; thumb_descriptor.header.h = 90;
    thumb_descriptor.header.stride = 320;
    thumb_descriptor.data_size = sizeof(thumb_data);
    thumb_descriptor.data = thumb_data;
    result_label = label(memory, "录制中说“查一下”查询，回答在此显示。", 300);
    button(memory, "播放证据声音", 220, operation_click, 4);
    notice_label = label(screen, "", 307);
    lv_obj_set_height(notice_label, 19);
    lv_label_set_long_mode(notice_label, LV_LABEL_LONG_DOT);
    font_tree(screen);
    lv_scr_load_anim(screen, LV_SCR_LOAD_ANIM_MOVE_RIGHT, 100, 0, true);
    running = 1;
    worker_started = pthread_create(&worker, NULL, worker_main, NULL) == 0;
    if (!worker_started) { running = 0; message("无法启动状态线程"); }
    timer = lv_timer_create(refresh, 150, NULL);
}
void ui_EchoTracePage_deinit(void) {
    if (timer) { lv_timer_delete(timer); timer = NULL; }
    pthread_mutex_lock(&mutex);
    running = 0;
    pthread_mutex_unlock(&mutex);
    if (worker_started) { pthread_join(worker, NULL); worker_started = 0; }
    pthread_mutex_lock(&mutex);
    if (shared_status) { json_object_put(shared_status); shared_status = NULL; }
    pthread_mutex_unlock(&mutex);
    if (status) { json_object_put(status); status = NULL; }
    dialog = editor = NULL;
}
