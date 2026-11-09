// oracle_wrap.h - Linux's ATOM interpreter as a test oracle (see oracle_wrap.cpp)
#pragma once
#include <stdint.h>

struct OracleIo {   // the card the oracle drives: set by the test before each run
  uint32_t (*rd)(uint32_t reg);
  void (*wr)(uint32_t reg, uint32_t val);
  void (*delay)(int unit_ms, unsigned count);   // 0 = udelay(us), 1 = mdelay/msleep(ms)
};
extern OracleIo oracle_io;

void *oracle_parse(const uint8_t *bios, uint32_t size, uint32_t scratch_bytes, uint32_t scratch_slack_dwords);   // nullptr: refused
int oracle_execute(void *h, int index, uint32_t *params, int params_size);
int oracle_asic_init(void *h);
const uint32_t *oracle_scratch(void *h);
void oracle_destroy(void *h);
