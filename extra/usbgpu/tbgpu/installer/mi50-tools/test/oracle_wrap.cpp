// oracle_wrap.cpp - the Linux ATOM interpreter (drivers/gpu/drm/amd/amdgpu/atom.c, compiled unmodified with the test shim) behind a small API
// for the differential test of the TinyGPU driver's port (the ATOM section of TinyGPUDriver.cpp). Its own translation unit: Linux's atom.h macros (ATOM_*) would
// collide with the port's names.
#include "oracle_wrap.h"
extern "C" {
#include "atom.h"
int amdgpu_atom_execute_table(struct atom_context *ctx, int index, uint32_t *params, int params_size);
int amdgpu_atom_asic_init(struct atom_context *ctx);
struct atom_context *amdgpu_atom_parse(struct card_info *card, void *bios, uint32_t bios_size);
void amdgpu_atom_destroy(struct atom_context *ctx);
unsigned long shim_jiffies = 0;
void shim_udelay(unsigned int us) { oracle_io.delay(0, us); }
void shim_msleep(unsigned int ms) { oracle_io.delay(1, ms); }
}

OracleIo oracle_io;

static uint32_t rd(struct card_info *, uint32_t r) { return oracle_io.rd(r); }
static void wr(struct card_info *, u32 r, uint32_t v) { oracle_io.wr(r, v); }
static uint32_t zero_rd(struct card_info *, uint32_t) { return 0; }   // amdgpu's mc/pll callbacks read 0 (amdgpu_atombios.c:1738-1784)
static void nop_wr(struct card_info *, u32, uint32_t) {}

struct Oracle { struct card_info card; struct atom_context *ctx; };

void *oracle_parse(const uint8_t *bios, uint32_t size, uint32_t scratch_bytes, uint32_t scratch_slack_dwords) {
  Oracle *o = (Oracle *)calloc(1, sizeof(Oracle));
  o->card.reg_read = rd; o->card.reg_write = wr; o->card.mc_read = zero_rd; o->card.mc_write = nop_wr; o->card.pll_read = zero_rd;
  o->card.pll_write = nop_wr;
  o->ctx = amdgpu_atom_parse(&o->card, (void *)bios, size);
  if (!o->ctx) { free(o); return nullptr; }
  o->ctx->scratch = (uint32_t *)calloc(scratch_bytes / 4 + scratch_slack_dwords, 4);   // amdgpu_atomfirmware_allocate_fb_scratch (kzalloc)
  o->ctx->scratch_size_bytes = (int)scratch_bytes;
  return o;
}
int oracle_execute(void *h, int index, uint32_t *params, int params_size) { return amdgpu_atom_execute_table(((Oracle *)h)->ctx, index, params, params_size); }
int oracle_asic_init(void *h) { return amdgpu_atom_asic_init(((Oracle *)h)->ctx); }
const uint32_t *oracle_scratch(void *h) { return ((Oracle *)h)->ctx->scratch; }
void oracle_destroy(void *h) {
  Oracle *o = (Oracle *)h;
  free(o->ctx->scratch); o->ctx->scratch = nullptr;
  amdgpu_atom_destroy(o->ctx);
  free(o);
}
