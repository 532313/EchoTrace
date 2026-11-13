#include "echotrace/media_lease.h"

#include <errno.h>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

int echotrace_media_lease_acquire(const char *path) {
  if (!path || !*path)
    path = ECHOTRACE_MEDIA_LEASE_PATH;
  int descriptor = open(path, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
  if (descriptor < 0)
    return -1;
  if (flock(descriptor, LOCK_EX | LOCK_NB) == 0)
    return descriptor;
  int saved = errno;
  close(descriptor);
  errno = saved;
  return -1;
}

void echotrace_media_lease_release(int descriptor) {
  if (descriptor >= 0) {
    flock(descriptor, LOCK_UN);
    close(descriptor);
  }
}
