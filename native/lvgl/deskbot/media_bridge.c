#include "echotrace_app.h"
#include "echotrace/media_lease.h"

#include <pthread.h>
#include <string.h>

static pthread_mutex_t owner_mutex = PTHREAD_MUTEX_INITIALIZER;
static int owner_descriptor = -1;
static const char *owner_page = NULL;

int echotrace_legacy_media_begin(const char *page_name) {
  if (!page_name || (strcmp(page_name, "ChatBotPage") != 0 &&
                     strcmp(page_name, "YOLOPage") != 0))
    return 0;
  pthread_mutex_lock(&owner_mutex);
  if (owner_descriptor >= 0) {
    pthread_mutex_unlock(&owner_mutex);
    return 0;
  }
  int descriptor = echotrace_media_lease_acquire(NULL);
  if (descriptor >= 0) {
    owner_descriptor = descriptor;
    owner_page = strcmp(page_name, "ChatBotPage") == 0 ? "ChatBotPage"
                                                    : "YOLOPage";
  }
  pthread_mutex_unlock(&owner_mutex);
  return descriptor >= 0;
}

void echotrace_legacy_media_end(const char *page_name) {
  pthread_mutex_lock(&owner_mutex);
  if (owner_descriptor >= 0 && owner_page && page_name &&
      strcmp(owner_page, page_name) == 0) {
    echotrace_media_lease_release(owner_descriptor);
    owner_descriptor = -1;
    owner_page = NULL;
  }
  pthread_mutex_unlock(&owner_mutex);
}
