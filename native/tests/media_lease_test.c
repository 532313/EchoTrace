#include "echotrace/media_lease.h"

#include <assert.h>
#include <errno.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static void child_expect(const char *path, int inherited, int expect_busy) {
  pid_t child = fork();
  assert(child >= 0);
  if (child == 0) {
    if (inherited >= 0)
      close(inherited);
    int descriptor = echotrace_media_lease_acquire(path);
    int ok = expect_busy ? descriptor < 0 &&
                               (errno == EWOULDBLOCK || errno == EAGAIN)
                         : descriptor >= 0;
    if (descriptor >= 0)
      echotrace_media_lease_release(descriptor);
    _exit(ok ? 0 : 1);
  }
  int status = 0;
  assert(waitpid(child, &status, 0) == child);
  assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
}

int main(void) {
  char path[] = "/tmp/echotrace-media-lease-XXXXXX";
  int temporary = mkstemp(path);
  assert(temporary >= 0);
  close(temporary);
  int descriptor = echotrace_media_lease_acquire(path);
  assert(descriptor >= 0);
  child_expect(path, descriptor, 1);
  echotrace_media_lease_release(descriptor);
  child_expect(path, -1, 0);
  unlink(path);
  return 0;
}
