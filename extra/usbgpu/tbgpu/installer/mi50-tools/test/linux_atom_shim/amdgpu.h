// test shim for the oracle: the kernel services atom.c uses; delays go to the test's trace
#pragma once
#include "linux/types.h"
#include "atom-types.h"
#include "atombios.h"
static inline void mutex_init(struct mutex *m) { (void)m; }
static inline void mutex_lock(struct mutex *m) { (void)m; }
static inline void mutex_unlock(struct mutex *m) { (void)m; }
#define KERN_DEBUG ""
#define printk(...) do { } while (0)
#define pr_info(...) do { } while (0)
#define DRM_ERROR(...) do { } while (0)
#define drm_info(...) do { } while (0)
extern unsigned long shim_jiffies;
#define jiffies (shim_jiffies)
#define time_after(a, b) ((long)((b) - (a)) < 0)
static inline unsigned int jiffies_to_msecs(unsigned long j) { return (unsigned int)j; }
void shim_udelay(unsigned int us);
void shim_msleep(unsigned int ms);
#define udelay(x) shim_udelay(x)
#define mdelay(x) shim_msleep(x)
#define msleep(x) shim_msleep(x)
#define le16_to_cpu(x) (x)
#define le32_to_cpu(x) (x)
#define cpu_to_le32(x) (x)
#define lower_32_bits(n) ((u32)((n) & 0xffffffff))
#define upper_32_bits(n) ((u32)(((u64)(n)) >> 32))
#define do_div(n, base) ({ u32 __rem = (u32)((n) % (base)); (n) /= (base); __rem; })
#define OPTIMIZER_HIDE_VAR(x) do { } while (0)
#define min(a, b) ((a) < (b) ? (a) : (b))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
static inline size_t strscpy(void *d, const void *s, size_t n) {
  char *dd = (char *)d; const char *ss = (const char *)s; size_t i = 0;
  if (!n) return 0;
  for (; i + 1 < n && ss[i]; i++) dd[i] = ss[i];
  dd[i] = 0; return i;
}
static inline int scnprintf(char *buf, size_t size, const char *fmt, ...) {
  va_list ap; va_start(ap, fmt); int r = vsnprintf(buf, size, fmt, ap); va_end(ap);
  if (r < 0) return 0; if (size == 0) return 0; return (size_t)r >= size ? (int)(size - 1) : r;
}
