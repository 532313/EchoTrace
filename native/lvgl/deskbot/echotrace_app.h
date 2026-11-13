#ifndef ECHOTRACE_DESKBOT_APP_H
#define ECHOTRACE_DESKBOT_APP_H

#ifdef __cplusplus
extern "C" {
#endif

void ui_EchoTracePage_init(void);
void ui_EchoTracePage_deinit(void);
/* Optional launch destination; unset ECHOTRACE_OPEN_APP keeps the Home page. */
void echotrace_app_startup(void);

/* The existing ChatBot and YOLO pages hold this lease until their media
 * worker is fully stopped. EchoTrace's daemon takes the same lease. */
int echotrace_legacy_media_begin(const char *page_name);
void echotrace_legacy_media_end(const char *page_name);

#ifdef __cplusplus
}
#endif

#endif
