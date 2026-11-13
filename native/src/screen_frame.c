#include "echotrace/screen_frame.h"
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

static uint64_t network_number(const uint8_t *bytes, size_t size) {
    uint64_t result = 0;
    for (size_t i = 0; i < size; ++i) result = (result << 8) | bytes[i];
    return result;
}
int et_screen_parse(const void *data, size_t size, et_screen_frame *frame) {
    if (!data || !frame || size != ET_SCREEN_WIRE_BYTES) return 0;
    const uint8_t *p = data;
    if (network_number(p, 2) != 3 || network_number(p + 2, 2) != 3 ||
        network_number(p + 4, 4) != 16 + ET_SCREEN_PIXELS ||
        network_number(p + 20, 4) != 0) return 0;
    frame->sequence = (uint32_t)network_number(p + 8, 4);
    frame->source_us = network_number(p + 12, 8);
    frame->stream = p + 24; frame->pixels = p + 40;
    return 1;
}
static int hex_digit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}
int et_screen_matches(const et_screen_frame *frame, const char *hex) {
    if (!frame || !hex || strlen(hex) != 32) return 0;
    for (size_t i = 0; i < 16; ++i) {
        int high = hex_digit(hex[i * 2]), low = hex_digit(hex[i * 2 + 1]);
        if (high < 0 || low < 0 || frame->stream[i] != (high * 16 + low)) return 0;
    }
    return 1;
}
static int open_socket(void) {
    int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (fd < 0) return -1;
    if (fcntl(fd, F_SETFL, O_NONBLOCK) || fcntl(fd, F_SETFD, FD_CLOEXEC)) {
        close(fd); return -1;
    }
    int size = 4 * ET_SCREEN_WIRE_BYTES;
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &size, sizeof(size));
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &size, sizeof(size));
    return fd;
}
int et_screen_sender(void) { return open_socket(); }
static int address(struct sockaddr_un *a, const char *path) {
    if (!path || !*path || strlen(path) >= sizeof(a->sun_path)) { errno = ENAMETOOLONG; return 0; }
    memset(a, 0, sizeof(*a)); a->sun_family = AF_UNIX; strcpy(a->sun_path, path);
    return 1;
}
int et_screen_receiver(const char *path) {
    struct sockaddr_un a;
    if (!address(&a, path)) return -1;
    int fd = open_socket();
    if (fd < 0) return -1;
    /* The display owns this path; retain a live receiver rather than stealing it. */
    struct stat info;
    if (lstat(path, &info) == 0) {
        if (!S_ISSOCK(info.st_mode) || info.st_uid != getuid()) {
            close(fd); errno = EADDRINUSE; return -1;
        }
        if (connect(fd, (struct sockaddr *)&a, sizeof(a)) == 0 || errno != ECONNREFUSED) {
            close(fd); errno = EADDRINUSE; return -1;
        }
        close(fd); fd = open_socket();
        if (fd < 0) return -1;
        struct stat current;
        if (lstat(path, &current) || current.st_ino != info.st_ino || current.st_dev != info.st_dev || unlink(path)) {
            close(fd); return -1;
        }
    }
    if (bind(fd, (struct sockaddr *)&a, sizeof(a)) < 0) { close(fd); return -1; }
    chmod(path, 0600);
    return fd;
}
int et_screen_send(int fd, const char *path, const void *data, size_t size) {
    struct sockaddr_un a;
    if (!address(&a, path) || fd < 0 || size != ET_SCREEN_WIRE_BYTES) return 0;
    return sendto(fd, data, size, MSG_DONTWAIT, (struct sockaddr *)&a, sizeof(a)) == (ssize_t)size;
}
