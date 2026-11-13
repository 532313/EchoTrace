#ifndef ECHOTRACE_MEDIA_LEASE_H
#define ECHOTRACE_MEDIA_LEASE_H

#ifdef __cplusplus
extern "C" {
#endif

/* Both the capture daemon and DeskBot's camera/audio apps use this lock. */
#define ECHOTRACE_MEDIA_LEASE_PATH "/run/echotrace-media.lock"

/* Returns a held descriptor, or -1 with errno set. An empty path uses the
 * production lock. Call release after closing the last camera/audio owner. */
int echotrace_media_lease_acquire(const char *path);
void echotrace_media_lease_release(int descriptor);

#ifdef __cplusplus
}
#endif

#endif
