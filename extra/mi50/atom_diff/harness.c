/* Differential harness: Linux atom.c compiled unchanged (included below) driven by a deterministic fake register file. */
#include "atom.c"   /* copied unchanged from the Linux tree by t_atom_diff.py */
static uint32_t nreads = 0;
static uint32_t fake_read(struct card_info *i, uint32_t reg) {
  uint32_t v = (reg * 2654435761u) ^ (nreads * 40503u) ^ 0x5bd1e995u; nreads++;
  printf("R %x %x\n", reg, v); return v;
}
static void fake_write(struct card_info *i, uint32_t reg, uint32_t val) { printf("W %x %x\n", reg, val); }
static uint32_t z_read(struct card_info *i, uint32_t reg) { return 0; }
static void z_write(struct card_info *i, uint32_t reg, uint32_t val) {}
int main(int argc, char **argv) {
  FILE *f = fopen(argv[1], "rb"); fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
  unsigned char *bios = malloc(n); fread(bios, 1, n, f); fclose(f);
  int scratch = atoi(argv[2]);
  struct card_info card = { .reg_read = fake_read, .reg_write = fake_write, .mc_read = z_read, .mc_write = z_write, .pll_read = z_read, .pll_write = z_write };
  struct atom_context *ctx = calloc(1, sizeof(*ctx));
  ctx->card = &card; ctx->bios = bios; ctx->bios_size = n;
  /* amdgpu_atom_parse without the informational string readers */
  int base = CU16(ATOM_ROM_TABLE_PTR);
  ctx->cmd_table = CU16(base + ATOM_ROM_CMD_PTR); ctx->data_table = CU16(base + ATOM_ROM_DATA_PTR);
  atom_index_iio(ctx, CU16(ctx->data_table + ATOM_DATA_IIO_PTR) + 4);
  ctx->scratch = calloc(1, scratch + 4); ctx->scratch_size_bytes = scratch;  /* +4: Linux's bound check admits one dword past the end */
  uint32_t ps[16]; for (int i = 0; i < 16; i++) ps[i] = 0x11111111u * (i + 1);
  int r = amdgpu_atom_execute_table(ctx, 0, ps, 16);
  printf("RET %d\n", r);
  for (int i = 0; i < 16; i++) printf("PS %d %x\n", i, ps[i]);
  for (int i = 0; i < scratch / 4; i++) if (ctx->scratch[i]) printf("FB %d %x\n", i, ctx->scratch[i]);
  printf("DIVMUL %x %x\nDB %x RB %x FBB %x SHIFT %x ATTR %x IO %x EQ %d AB %d\n", ctx->divmul[0], ctx->divmul[1], ctx->data_block, ctx->reg_block,
         ctx->fb_base, ctx->shift, ctx->io_attr, ctx->io_mode, ctx->cs_equal, ctx->cs_above);
  return 0;
}
