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

#define MAX_PROJECTS 32
#define THUMB_BYTES (160 * 90 * 2)

typedef struct {
    int available, connected, playback_active, project_count;
    char state[24], error[256], message[256], answer[768], thumbnail[256];
    char project_ids[MAX_PROJECTS][80], project_names[MAX_PROJECTS][96];
    uint64_t marker_us, query_us;
} trace_snapshot_t;

static pthread_mutex_t trace_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_t trace_worker;
static int trace_running, trace_worker_started, command_pending;
static char command_name[32], command_project[80];
static trace_snapshot_t shared, shown;
static lv_obj_t *status_label, *project_select, *result_label, *tabs, *thumb_image;
static lv_timer_t *refresh_timer;
static char old_choices[MAX_PROJECTS * 100];
static uint64_t displayed_query_us;
static uint8_t thumb_data[THUMB_BYTES];
static lv_image_dsc_t thumb_descriptor;

static void copy_text(char *out, size_t size, const char *in) {
    snprintf(out, size, "%s", in ? in : "");
}

static void project_option(char *out, size_t size, int index, const char *name) {
    int prefix = snprintf(out, size, "%d. ", index + 1);
    if (prefix < 0 || (size_t)prefix >= size)
        return;
    size_t used = (size_t)prefix, offset = 0, name_length = strlen(name);
    int glyphs = 0;
    while (offset < name_length && glyphs < 14) {
        unsigned char lead = (unsigned char)name[offset];
        size_t span = lead < 0x80 ? 1 : lead >= 0xf0 ? 4 :
                      lead >= 0xe0 ? 3 : lead >= 0xc0 ? 2 : 0;
        if (!span || span > name_length - offset || used + span + 4 > size)
            break;
        int valid = 1;
        for (size_t j = 1; j < span; ++j) {
            if (((unsigned char)name[offset + j] & 0xc0) != 0x80) {
                valid = 0;
                break;
            }
        }
        if (!valid)
            break;
        memcpy(out + used, name + offset, span);
        used += span;
        offset += span;
        ++glyphs;
    }
    if (offset < name_length && used + 4 <= size) {
        memcpy(out + used, "…", 3);
        used += 3;
    }
    out[used] = '\0';
}

static uint64_t now_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

static const char *string_field(json_object *object, const char *key) {
    json_object *value = NULL;
    if (!object || !json_object_object_get_ex(object, key, &value) ||
        !json_object_is_type(value, json_type_string))
        return "";
    return json_object_get_string(value);
}

static uint64_t uint_field(json_object *object, const char *key) {
    json_object *value = NULL;
    if (!object || !json_object_object_get_ex(object, key, &value))
        return 0;
    return json_object_get_uint64(value);
}

static int bool_field(json_object *object, const char *key) {
    json_object *value = NULL;
    if (!object || !json_object_object_get_ex(object, key, &value))
        return 0;
    return json_object_get_boolean(value);
}

static const char *control_path(void) {
    const char *path = getenv("ECHOTRACE_SOCKET");
    if (path && *path)
        return path;
    if (access("/run/echotrace-sd.sock", F_OK) == 0)
        return "/run/echotrace-sd.sock";
    return "/run/echotrace/control.sock";
}

static json_object *exchange(json_object *request, char *error, size_t error_size) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        copy_text(error, error_size, "无法创建控制连接");
        return NULL;
    }
    struct timeval timeout = {.tv_sec = 1, .tv_usec = 0};
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    const char *path = control_path();
    if (strlen(path) >= sizeof(address.sun_path)) {
        copy_text(error, error_size, "控制路径过长");
        close(fd);
        return NULL;
    }
    strcpy(address.sun_path, path);
    if (connect(fd, (const struct sockaddr *)&address, sizeof(address)) < 0) {
        copy_text(error, error_size, "EchoTrace 采集服务未连接");
        close(fd);
        return NULL;
    }
    const char *body = json_object_to_json_string_ext(request, JSON_C_TO_STRING_PLAIN);
    size_t sent = 0, length = strlen(body);
    while (sent < length) {
        ssize_t n = send(fd, body + sent, length - sent, MSG_NOSIGNAL);
        if (n <= 0) {
            copy_text(error, error_size, "控制命令发送失败");
            close(fd);
            return NULL;
        }
        sent += (size_t)n;
    }
    if (send(fd, "\n", 1, MSG_NOSIGNAL) != 1) {
        copy_text(error, error_size, "控制命令发送失败");
        close(fd);
        return NULL;
    }
    char response[65536];
    size_t used = 0;
    while (used + 1 < sizeof(response)) {
        ssize_t n = recv(fd, response + used, sizeof(response) - used - 1, 0);
        if (n <= 0)
            break;
        used += (size_t)n;
        if (memchr(response, '\n', used))
            break;
    }
    close(fd);
    response[used] = '\0';
    if (!used || !memchr(response, '\n', used)) {
        copy_text(error, error_size, "采集服务无完整回复");
        return NULL;
    }
    json_object *reply = json_tokener_parse(response);
    if (!reply || !json_object_is_type(reply, json_type_object)) {
        if (reply)
            json_object_put(reply);
        copy_text(error, error_size, "采集服务回复格式错误");
        return NULL;
    }
    return reply;
}

static void read_status(trace_snapshot_t *next, json_object *reply) {
    json_object *array = NULL, *query = NULL;
    next->available = 1;
    next->connected = bool_field(reply, "connected");
    next->playback_active = bool_field(reply, "playback_active");
    copy_text(next->state, sizeof(next->state), string_field(reply, "state"));
    copy_text(next->error, sizeof(next->error), string_field(reply, "error"));
    next->marker_us = uint_field(reply, "last_marker_us");
    if (json_object_object_get_ex(reply, "projects", &array) &&
        json_object_is_type(array, json_type_array)) {
        next->project_count = json_object_array_length(array);
        if (next->project_count > MAX_PROJECTS)
            next->project_count = MAX_PROJECTS;
        for (int i = 0; i < next->project_count; ++i) {
            json_object *item = json_object_array_get_idx(array, i);
            copy_text(next->project_ids[i], sizeof(next->project_ids[i]),
                      string_field(item, "id"));
            copy_text(next->project_names[i], sizeof(next->project_names[i]),
                      string_field(item, "name"));
        }
    }
    if (json_object_object_get_ex(reply, "query", &query) &&
        json_object_is_type(query, json_type_object)) {
        next->query_us = uint_field(query, "received_us");
        copy_text(next->answer, sizeof(next->answer), string_field(query, "answer"));
        copy_text(next->thumbnail, sizeof(next->thumbnail),
                  string_field(query, "thumbnail_path"));
    }
}

static void *worker_main(void *unused) {
    (void)unused;
    for (;;) {
        char op[sizeof(command_name)] = "";
        char selected[sizeof(command_project)] = "";
        pthread_mutex_lock(&trace_mutex);
        int running = trace_running;
        if (command_pending) {
            copy_text(op, sizeof(op), command_name);
            copy_text(selected, sizeof(selected), command_project);
            command_pending = 0;
        }
        pthread_mutex_unlock(&trace_mutex);
        if (!running)
            break;
        if (*op) {
            json_object *request = json_object_new_object();
            json_object_object_add(request, "op", json_object_new_string(op));
            if (*selected)
                json_object_object_add(request, "project_id",
                                       json_object_new_string(selected));
            char error[256] = "";
            json_object *reply = exchange(request, error, sizeof(error));
            json_object_put(request);
            if (reply) {
                const char *failure = string_field(reply, "error");
                if (*failure)
                    copy_text(error, sizeof(error), failure);
                else if (bool_field(reply, "queued_offline"))
                    copy_text(error, sizeof(error), "已在本机排队，稍后同步");
                json_object_put(reply);
            }
            pthread_mutex_lock(&trace_mutex);
            copy_text(shared.message, sizeof(shared.message), error);
            pthread_mutex_unlock(&trace_mutex);
        }
        json_object *request = json_object_new_object();
        json_object_object_add(request, "op", json_object_new_string("status"));
        char error[256] = "";
        json_object *reply = exchange(request, error, sizeof(error));
        json_object_put(request);
        pthread_mutex_lock(&trace_mutex);
        if (reply) {
            trace_snapshot_t next = {0};
            copy_text(next.message, sizeof(next.message), shared.message);
            read_status(&next, reply);
            shared = next;
        } else {
            shared.available = 0;
            copy_text(shared.error, sizeof(shared.error), error);
        }
        pthread_mutex_unlock(&trace_mutex);
        if (reply)
            json_object_put(reply);
        usleep(250000);
    }
    return NULL;
}

static void set_label(lv_obj_t *label, const char *value) {
    if (strcmp(lv_label_get_text(label), value) != 0)
        lv_label_set_text(label, value);
}

static void apply_font_tree(lv_obj_t *object) {
    lv_obj_set_style_text_font(object, &echotrace_font, LV_PART_MAIN);
    uint32_t children = lv_obj_get_child_count(object);
    for (uint32_t i = 0; i < children; ++i)
        apply_font_tree(lv_obj_get_child(object, i));
}

static void show_thumbnail(const char *path) {
    FILE *file = *path ? fopen(path, "rb") : NULL;
    size_t read_bytes = file ? fread(thumb_data, 1, sizeof(thumb_data), file) : 0;
    if (file)
        fclose(file);
    if (read_bytes == sizeof(thumb_data)) {
        lv_image_cache_drop(&thumb_descriptor);
        lv_image_set_src(thumb_image, &thumb_descriptor);
        lv_obj_remove_flag(thumb_image, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(thumb_image, LV_OBJ_FLAG_HIDDEN);
    }
}

static void refresh(lv_timer_t *timer) {
    (void)timer;
    pthread_mutex_lock(&trace_mutex);
    shown = shared;
    pthread_mutex_unlock(&trace_mutex);
    char line[192];
    if (!shown.available)
        copy_text(line, sizeof(line), "EchoTrace · 采集服务未连接");
    else
        snprintf(line, sizeof(line), "EchoTrace · %s · %s",
                 strcmp(shown.state, "recording") == 0 ? "记录中" :
                 strcmp(shown.state, "paused") == 0 ? "已暂停" : "已停止",
                 shown.connected ? "在线" : "离线");
    set_label(status_label, line);
    lv_obj_set_style_text_color(status_label,
        lv_color_hex(strcmp(shown.state, "recording") == 0 ? 0xff7770 : 0xf3f6ef), 0);

    char choices[sizeof(old_choices)] = "";
    if (shown.project_count == 0)
        copy_text(choices, sizeof(choices), "先在工作台创建项目");
    for (int i = 0; i < shown.project_count; ++i) {
        size_t left = sizeof(choices) - strlen(choices) - 1;
        if (i && left)
            strncat(choices, "\n", left);
        left = sizeof(choices) - strlen(choices) - 1;
        if (left) {
            char option[80];
            project_option(option, sizeof(option), i, shown.project_names[i]);
            strncat(choices, option, left);
        }
    }
    if (strcmp(choices, old_choices) != 0) {
        unsigned selected = lv_dropdown_get_selected(project_select);
        lv_dropdown_set_options(project_select, choices);
        if (selected < (unsigned)shown.project_count)
            lv_dropdown_set_selected(project_select, selected);
        copy_text(old_choices, sizeof(old_choices), choices);
    }
    const char *message = shown.error[0] ? shown.error : shown.message;
    if (message[0])
        set_label(result_label, message);
    else if (shown.query_us && shown.query_us != displayed_query_us) {
        displayed_query_us = shown.query_us;
        set_label(result_label, shown.answer);
        show_thumbnail(shown.thumbnail);
        lv_tabview_set_active(tabs, 1, LV_ANIM_OFF);
    } else if (shown.marker_us && now_us() - shown.marker_us < 2000000)
        set_label(result_label, "已记下这一步");
}

static void queue_operation(const char *op) {
    pthread_mutex_lock(&trace_mutex);
    if (command_pending) {
        copy_text(shared.message, sizeof(shared.message), "上一条操作仍在等待");
    } else if (strcmp(op, "session.start") == 0 &&
               shown.project_count == 0) {
        copy_text(shared.message, sizeof(shared.message), "请先在浏览器或 Qt 新建项目");
    } else {
        copy_text(command_name, sizeof(command_name), op);
        unsigned selected = lv_dropdown_get_selected(project_select);
        const char *id = selected < (unsigned)shown.project_count
                             ? shown.project_ids[selected] : "";
        copy_text(command_project, sizeof(command_project), id);
        command_pending = 1;
        shared.message[0] = '\0';
    }
    pthread_mutex_unlock(&trace_mutex);
}

static void operation_click(lv_event_t *event) {
    const char *op = lv_event_get_user_data(event);
    if (strcmp(op, "toggle") == 0)
        op = strcmp(shown.state, "recording") == 0 ? "session.pause" :
             "session.resume";
    queue_operation(op);
}

static void back_click(lv_event_t *event) {
    (void)event;
    lv_lib_pm_OpenPrePage(&page_manager);
}

static void make_button(lv_obj_t *parent, const char *text, const char *op,
                        int width) {
    lv_obj_t *button = lv_button_create(parent);
    lv_obj_set_size(button, width, 37);
    lv_obj_set_style_bg_color(button, lv_color_hex(0x27634e), 0);
    lv_obj_t *label = lv_label_create(button);
    lv_label_set_text(label, text);
    lv_obj_center(label);
    lv_obj_add_event_cb(button, operation_click, LV_EVENT_CLICKED, (void *)op);
}

void ui_EchoTracePage_init(void) {
    memset(&shared, 0, sizeof(shared));
    memset(&shown, 0, sizeof(shown));
    old_choices[0] = '\0';
    displayed_query_us = 0;
    command_pending = 0;
    lv_obj_t *screen = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(screen, lv_color_hex(0xf3f6ef), 0);
    lv_obj_set_style_text_font(screen, &echotrace_font, 0);
    lv_obj_set_flex_flow(screen, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_all(screen, 5, 0);
    lv_obj_set_style_pad_row(screen, 4, 0);
    lv_obj_t *header = lv_obj_create(screen);
    lv_obj_set_size(header, LV_PCT(100), 29);
    lv_obj_set_style_pad_all(header, 0, 0);
    lv_obj_set_flex_flow(header, LV_FLEX_FLOW_ROW);
    lv_obj_t *back = lv_button_create(header);
    lv_obj_set_size(back, 50, 26);
    lv_obj_t *back_text = lv_label_create(back);
    lv_label_set_text(back_text, "返回");
    lv_obj_center(back_text);
    lv_obj_add_event_cb(back, back_click, LV_EVENT_CLICKED, NULL);
    status_label = lv_label_create(header);
    lv_label_set_text(status_label, "EchoTrace · 正在连接");
    lv_obj_set_width(status_label, 250);
    tabs = lv_tabview_create(screen);
    lv_obj_set_width(tabs, LV_PCT(100));
    lv_obj_set_flex_grow(tabs, 1);
    lv_tabview_set_tab_bar_size(tabs, 25);
    lv_obj_t *record = lv_tabview_add_tab(tabs, "记录");
    lv_obj_t *memory = lv_tabview_add_tab(tabs, "回看");
    for (int i = 0; i < 2; ++i) {
        lv_obj_t *page = i ? memory : record;
        lv_obj_set_flex_flow(page, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_style_pad_all(page, 2, 0);
        lv_obj_set_style_pad_row(page, 4, 0);
    }
    project_select = lv_dropdown_create(record);
    lv_obj_set_size(project_select, LV_PCT(100), 34);
    lv_dropdown_set_options(project_select, "先在工作台创建项目");
    for (int i = 0; i < 2; ++i) {
        lv_obj_t *row = lv_obj_create(record);
        lv_obj_set_size(row, LV_PCT(100), 43);
        lv_obj_set_style_pad_all(row, 2, 0);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        if (i == 0) {
            make_button(row, "开始记忆", "session.start", 116);
            make_button(row, "记这一步", "mark", 116);
        } else {
            make_button(row, "暂停/继续", "toggle", 116);
            make_button(row, "结束记忆", "session.stop", 116);
        }
    }
    lv_obj_t *hint = lv_label_create(record);
    lv_label_set_text(hint, "返回首页继续录制；说“查一下”查询。");
    lv_obj_set_width(hint, LV_PCT(100));
    thumb_image = lv_image_create(memory);
    lv_obj_add_flag(thumb_image, LV_OBJ_FLAG_HIDDEN);
    memset(&thumb_descriptor, 0, sizeof(thumb_descriptor));
    thumb_descriptor.header.magic = LV_IMAGE_HEADER_MAGIC;
    thumb_descriptor.header.cf = LV_COLOR_FORMAT_RGB565;
    thumb_descriptor.header.w = 160;
    thumb_descriptor.header.h = 90;
    thumb_descriptor.header.stride = 320;
    thumb_descriptor.data_size = sizeof(thumb_data);
    thumb_descriptor.data = thumb_data;
    result_label = lv_label_create(memory);
    lv_obj_set_width(result_label, LV_PCT(100));
    lv_label_set_long_mode(result_label, LV_LABEL_LONG_WRAP);
    lv_label_set_text(result_label, "查询结果在这里显示；详细回放请用浏览器或 Qt。");
    make_button(memory, "播放证据声音", "evidence.play", 188);
    apply_font_tree(screen);
    lv_obj_set_style_text_color(status_label, lv_color_hex(0xf3f6ef), 0);
    lv_scr_load_anim(screen, LV_SCR_LOAD_ANIM_MOVE_RIGHT, 100, 0, true);
    trace_running = 1;
    trace_worker_started = pthread_create(&trace_worker, NULL, worker_main, NULL) == 0;
    if (!trace_worker_started) {
        trace_running = 0;
        lv_label_set_text(result_label, "无法启动 EchoTrace 状态线程");
    }
    refresh_timer = lv_timer_create(refresh, 150, NULL);
}

void ui_EchoTracePage_deinit(void) {
    if (refresh_timer) {
        lv_timer_delete(refresh_timer);
        refresh_timer = NULL;
    }
    pthread_mutex_lock(&trace_mutex);
    trace_running = 0;
    pthread_mutex_unlock(&trace_mutex);
    if (trace_worker_started) {
        pthread_join(trace_worker, NULL);
        trace_worker_started = 0;
    }
}
