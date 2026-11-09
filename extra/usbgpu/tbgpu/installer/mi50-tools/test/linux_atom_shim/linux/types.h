// test shim: just enough of the Linux kernel headers to compile drivers/gpu/drm/amd/amdgpu/atom.c, unmodified (as C), in user space as the
// test oracle for the driver's ATOM interpreter
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
typedef uint8_t u8; typedef uint16_t u16; typedef uint32_t u32; typedef uint64_t u64; typedef int8_t s8; typedef int16_t s16; typedef int32_t s32;
struct drm_device { int unused; };
struct mutex { int unused; };
