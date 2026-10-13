#pragma once
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include "linux/types.h"
#define printk(...) fprintf(stderr, __VA_ARGS__)
#define pr_info(...) fprintf(stderr, __VA_ARGS__)
#define KERN_DEBUG ""
#define DRM_ERROR(...) fprintf(stderr, __VA_ARGS__)
#define DRM_DEBUG(...) do {} while (0)
#define drm_info(dev, ...) fprintf(stderr, __VA_ARGS__)
#define GFP_KERNEL 0
#define kzalloc(n, f) calloc(1, (n))
#define kcalloc(n, s, f) calloc((n), (s))
#define kzalloc_obj(t) calloc(1, sizeof(t))
#define kfree(p) free(p)
#define cpu_to_le32(x) (x)
#define le32_to_cpu(x) (x)
#define le16_to_cpu(x) (x)
static inline u32 get_unaligned_le32(const void *p) { u32 v; memcpy(&v, p, 4); return v; }
#define str_yes_no(b) ((b) ? "yes" : "no")
struct mutex { int x; };
#define mutex_lock(m) do {} while (0)
#define mutex_unlock(m) do {} while (0)
#define udelay(x) do {} while (0)
#define mdelay(x) do {} while (0)
#define msleep(x) do {} while (0)
#define drm_can_sleep() 1
static unsigned long jiffies = 0;
#define time_after(a, b) ((long)((b) - (a)) < 0)
#define jiffies_to_msecs(j) ((unsigned)(j))
#define do_div(n, base) ({ uint32_t __rem = (uint32_t)((n) % (base)); (n) /= (base); __rem; })
#define lower_32_bits(n) ((u32)((n) & 0xffffffff))
#define upper_32_bits(n) ((u32)((n) >> 32))
#define min(a, b) ((a) < (b) ? (a) : (b))
#define strscpy(d, s, n) strlcpy((char*)(d), (s), (n))
#define OPTIMIZER_HIDE_VAR(x) do {} while (0)
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define scnprintf snprintf
