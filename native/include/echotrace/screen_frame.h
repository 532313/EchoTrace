#ifndef ECHOTRACE_SCREEN_FRAME_H
#define ECHOTRACE_SCREEN_FRAME_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

/* Protocol v3 kind 3: existing 24-byte network header, 16-byte stream UUID,
 * followed by one RGB565LE image. No frame fragments or unbounded queue. */
#define ET_SCREEN_WIDTH 240
#define ET_SCREEN_HEIGHT 136
#define ET_SCREEN_PIXELS (ET_SCREEN_WIDTH * ET_SCREEN_HEIGHT * 2)
#define ET_SCREEN_WIRE_BYTES (24 + 16 + ET_SCREEN_PIXELS)
typedef struct {
    uint32_t sequence;
    uint64_t source_us;
    const uint8_t *stream;
    const uint8_t *pixels;
} et_screen_frame;
int et_screen_parse(const void *data, size_t size, et_screen_frame *frame);
int et_screen_matches(const et_screen_frame *frame, const char *stream_hex);
/* Sockets are nonblocking and CLOEXEC. Each datagram is a complete frame. */
int et_screen_sender(void);
int et_screen_receiver(const char *path);
int et_screen_send(int fd, const char *path, const void *data, size_t size);

#ifdef __cplusplus
}
#endif
#endif
