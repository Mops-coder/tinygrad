#pragma once
#include "types.h"
#define GFP_KERNEL 0
static inline void *kzalloc(size_t n, int f) { (void)f; return calloc(1, n ? n : 1); }
static inline void *kcalloc(size_t n, size_t s, int f) { (void)f; return calloc(n ? n : 1, s ? s : 1); }
static inline void kfree(const void *p) { free((void *)p); }
#define kzalloc_obj(T) ((T *)calloc(1, sizeof(T)))
