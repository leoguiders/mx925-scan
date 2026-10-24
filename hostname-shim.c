/*
 * hostname-shim: override gethostname() via LD_PRELOAD.
 *
 * The SANE pixma backend sends "<user>  <hostname>" to the printer, which
 * shows it as the scan host on the operation panel. With host networking the
 * container cannot have its own hostname, so this shim returns the value of
 * $SCAN_HOSTNAME instead (falls back to the real hostname if unset).
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int gethostname(char *name, size_t len)
{
    const char *h = getenv("SCAN_HOSTNAME");
    if (h == NULL || *h == '\0') {
        int (*real)(char *, size_t) =
            (int (*)(char *, size_t))dlsym(RTLD_NEXT, "gethostname");
        if (real == NULL) {
            errno = ENOSYS;
            return -1;
        }
        return real(name, len);
    }
    if (len == 0) {
        errno = EINVAL;
        return -1;
    }
    size_t n = strlen(h);
    if (n >= len) {
        memcpy(name, h, len);   /* truncated, like glibc: not terminated */
        errno = ENAMETOOLONG;
        return -1;
    }
    memcpy(name, h, n + 1);
    return 0;
}
