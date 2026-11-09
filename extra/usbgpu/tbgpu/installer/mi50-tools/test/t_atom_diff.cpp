// t_atom_diff.cpp - differential test of the TinyGPU driver's ATOM interpreter (the ATOM section of TinyGPUDriver.cpp) against Linux's own (atom.c, unmodified):
// thousands of random, well-formed ATOM BIOS images run on both; every register read and write, delay, return code, parameter and FB scratch
// dword must be identical.
// Programs always terminate: jumps and switch cases go forward to instruction boundaries, CALLTABLE only to later tables. They stay away from
// what C leaves undefined (shift counts >= 32, IIO masks of width 0) and from the port's documented, fail-closed deviations (reads past the
// image, the FB dword just past the scratch buffer, the total-time deadline).
// Directed cases cover what random programs never produce: refused images, absent tables, IIO indexing edge cases, a bad switch case,
// WS_SHIFT / WS_FB_WINDOW writes (on both); and the port's fail-closed contract (port only, Linux would read past the image): every kind of
// operand cut by the image end, the 20 s loop and deadline aborts. The cut test checks that contract on random programs cut at random bytes.
// atom.c must be compiled with -ftrivial-auto-var-init=zero: a REG read through an undefined IIO read method leaves Linux's `saved` unset
// (see the interpreter's header comment in TinyGPUDriver.cpp), and the flag pins it to the port's (and atom.py's) 0 without changing a line of atom.c.
// usage: t_atom_diff [programs] [seed]
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>
#include "../../TinyGPUDriverExtension/TinyGPUDriver.cpp"   // the interpreter is the driver's own (its ATOM section), compiled unchanged
#include "dk_card.hpp"                                      // the DriverKit stand-ins the rest of the driver needs to link
#include "oracle_wrap.h"

struct Rng {
  uint64_t s;
  uint32_t next() { s ^= s << 13; s ^= s >> 7; s ^= s << 17; return (uint32_t)s; }
  uint32_t below(uint32_t n) { return n ? next() % n : 0; }
  bool pct(uint32_t p) { return below(100) < p; }
};

constexpr uint32_t IMG = 0x40000, HDR = 0x100, MDT = 0x200, CMT = 0x400, IIOB = 0x7000, DATA0 = 0x8000, TBL0 = 0x1000, TBL_STRIDE = 0x800,
  NTBL = 8, SCRATCH = 0x5000;

// ---- one random image ----
struct Ins { std::vector<uint8_t> b; std::vector<std::pair<size_t, int>> patches; };   // (byte offset in b of a u16 target, target instruction)
struct Gen {
  Rng &r; int cur_tbl; int nins;
  explicit Gen(Rng &rng, int t, int n) : r(rng), cur_tbl(t), nins(n) {}
  void u8(Ins &i, uint32_t v) { i.b.push_back((uint8_t)v); }
  void u16(Ins &i, uint32_t v) { u8(i, v & 0xff); u8(i, (v >> 8) & 0xff); }
  void u32(Ins &i, uint32_t v) { u16(i, v & 0xffff); u16(i, v >> 16); }
  uint32_t reg() { static const uint32_t R[] = {0, 1, 2, 3, 6, 0x10, 0x11, 0x20, 0x40, 0x53, 0x5a, 0x100, 0x1234}; return r.pct(80) ? R[r.below(13)] : r.below(0x400); }
  uint32_t ws_idx(bool dst) {
    if (r.pct(30)) { uint32_t w = 0x40 + r.below(9); if (dst && (w == 0x43 || w == 0x46)) w = 0x40; return w; }   // never WS_SHIFT or WS_FB_WINDOW as a destination (C shift UB / the FB wrap, see the interpreter's header comment)
    return r.below(10);
  }
  void dst(Ins &i, uint32_t arg) {
    switch (arg) { case 0: u16(i, reg()); break; case 1: u8(i, r.below(20)); break; case 2: u8(i, ws_idx(true)); break; case 3: u8(i, r.below(64)); break;
                   default: u8(i, r.below(8)); }
  }
  void src(Ins &i, uint32_t attr) {
    uint32_t arg = attr & 7, al = (attr >> 3) & 7;
    switch (arg) {
      case 0: u16(i, reg()); break; case 1: u8(i, r.below(20)); break; case 2: u8(i, ws_idx(false)); break; case 3: u8(i, r.below(64)); break;
      case 4: u16(i, r.below(0x7000)); break;
      case 5: if (al == 0) u32(i, r.next()); else if (al <= 3) u16(i, r.next()); else u8(i, r.next()); break;
      default: u8(i, r.below(8));
    }
  }
  uint32_t attr_any() { return r.next() & 0xff; }
  int near(int idx) { int left = std::max(1, nins - idx); return idx + 1 + (int)(r.pct(80) ? r.below(std::min(2, left)) : r.below(left)); }   // mostly the next instruction or the one after
  Ins make(int idx) {
    Ins i;
    uint32_t op;
    int pick = r.below(30);
    auto group = [&](uint32_t first) { uint32_t a = r.below(6); op = first + a; return a; };   // A6 order: REG PS WS FB PLL MC
    static const uint32_t A6[6] = {0, 1, 2, 3, 6, 7};
    auto dst_src = [&](uint32_t first) { uint32_t a = A6[group(first)]; u8(i, op); uint32_t at = attr_any(); u8(i, at); dst(i, a); src(i, at); };
    switch (pick) {
      case 0: case 1: case 2: dst_src(1); break;                        // move
      case 3: dst_src(7); break; case 4: dst_src(13); break;            // and, or
      case 5: case 6: { uint32_t a = A6[group(r.pct(50) ? 19 : 25)]; u8(i, op); u8(i, attr_any()); dst(i, a); u8(i, r.below(32)); break; }   // shift_left/right
      case 7: dst_src(31); break; case 8: dst_src(37); break;           // mul, div
      case 9: dst_src(43); break; case 10: dst_src(49); break;          // add, sub
      case 11: { u8(i, 55 + r.below(3)); if (i.b[0] == 55) u16(i, r.below(6)); else u8(i, r.below(256)); break; }   // setport ATI / PCI / SYSIO
      case 12: u8(i, 58); u16(i, r.below(0x200)); break;                // setregblock
      case 13: { u8(i, 59); u8(i, 0x05); u32(i, 4 * r.below(0x1440) + 2); break; }   // setfbbase: IMM dword 4k+2 below 0x5100 (no uint32 wrap, never == the scratch size; see the interpreter's header comment)
      case 14: dst_src(60); break; case 15: dst_src(74); break;         // compare, test
      case 16: {                                                        // switch: forward cases
        u8(i, 66); uint32_t at = attr_any(); u8(i, at); src(i, at);
        uint32_t al = (at >> 3) & 7; int n = r.below(4);
        for (int k = 0; k < n; k++) {
          u8(i, 0x63);
          if (al == 0) u32(i, r.pct(30) ? r.below(4) : r.next()); else if (al <= 3) u16(i, r.below(4)); else u8(i, r.below(4));
          i.patches.push_back({i.b.size(), near(idx)}); u16(i, 0);
        }
        u16(i, 0x5A5A); break;
      }
      case 17: case 18: { u8(i, 67 + r.below(7)); i.patches.push_back({i.b.size(), near(idx)}); u16(i, 0); break; }   // jump
      case 19: u8(i, 80 + r.below(2)); u8(i, r.below(20)); break;      // delay ms / us
      case 20: { u8(i, 82); u8(i, r.pct(70) ? (cur_tbl + 1 + r.below(NTBL)) : NTBL + r.below(256 - NTBL)); break; }   // calltable: later tables or absent ones (no recursion)
      case 21: dst_src(92); break;                                       // mask: attr, dst, mask, src
      case 22: { uint32_t a = A6[group(84)]; u8(i, op); u8(i, attr_any()); dst(i, a); break; }          // clear
      case 23: { static const uint32_t S[] = {90, 98, 99, 83, 100, 101, 121}; u8(i, S[r.below(7)]); if (i.b[0] == 98 || i.b[0] == 121) u8(i, r.below(256)); break; }
      case 24: { u8(i, 102); uint32_t v = r.below(4); u8(i, v == 0 ? 0 : v == 1 ? 255 : 1 + r.below(60)); break; }   // setdatablock
      case 25: dst_src(103); break;                                      // xor
      case 26: { uint32_t a = A6[group(r.pct(50) ? 109 : 115)]; u8(i, op); uint32_t at = 0x25 | (r.below(4) << 6); u8(i, at); dst(i, a); u8(i, r.below(32)); break; }   // shl / shr
      case 27: { u8(i, 122); uint32_t n = r.below(8); u16(i, n); for (uint32_t k = 0; k < n; k++) u8(i, r.next()); break; }   // processds
      case 28: { op = 123 + r.below(2); u8(i, op); uint32_t at = attr_any(); u8(i, at); dst(i, op == 123 ? 1 : 2); src(i, at); break; }   // mul32 PS/WS
      default: { op = 125 + r.below(2); u8(i, op); uint32_t at = attr_any(); u8(i, at); dst(i, op == 125 ? 1 : 2); src(i, at); break; }   // div32
    }
    // mask carries an extra direct operand between dst and src: rebuild it properly
    if (i.b[0] >= 92 && i.b[0] <= 97) {
      Ins m; uint32_t a = A6[i.b[0] - 92]; u8(m, i.b[0]); uint32_t at = attr_any(); u8(m, at); dst(m, a);
      uint32_t al = (at >> 3) & 7;
      if (al == 0) u32(m, r.next()); else if (al <= 3) u16(m, r.next()); else u8(m, r.next());
      src(m, at); return m;
    }
    return i;
  }
};

static std::vector<uint8_t> build(Rng &r, std::vector<uint32_t> *t0_off = nullptr) {   // t0_off: table 0's instruction offsets + its end byte
  std::vector<uint8_t> img(IMG, 0);
  auto w16 = [&](uint32_t o, uint32_t v) { img[o] = v & 0xff; img[o + 1] = (v >> 8) & 0xff; };
  img[0] = 0x55; img[1] = 0xAA; img[2] = 0x80; memcpy(&img[0x30], " 761295520", 10); w16(0x48, HDR);
  memcpy(&img[HDR + 4], "ATOM", 4); w16(HDR + 0x1E, CMT); w16(HDR + 0x20, MDT);
  memcpy(&img[0x80], "113-D1631700-111", 16);   // part number (Linux atom_get_vbios_pn reads through NULL without one)
  for (uint32_t o = DATA0; o < 0x10000; o++) img[o] = (uint8_t)r.next();          // data the ID operands and data blocks point at
  for (uint32_t k = 0; k < 80; k++) w16(MDT + 4 + 2 * k, DATA0 + r.below(0x7F00));
  w16(MDT + 0x32, IIOB - 4);                                                        // ATOM_DATA_IIO_PTR -> IIO methods at IIOB
  // IIO methods 0..5 (well-formed: widths 1..32, shifts 0..31)
  uint32_t p = IIOB;
  static const uint32_t IIO_IDX[] = {0, 1, 2, 3, 4, 5, 0x80, 0x81, 0x82, 0x83, 0x84, 0x85};   // reads use iio[io_mode & 0x7F], writes iio[io_mode & 0xFF]
  for (uint32_t m : IIO_IDX) {
    if (r.pct(25)) continue;
    img[p++] = 1; img[p++] = (uint8_t)m;
    int n = 1 + r.below(8);
    for (int k = 0; k < n; k++) {
      uint32_t op = r.below(9);
      if (op == 1) op = 0;   // START is not an op inside a method
      img[p++] = (uint8_t)op;
      if (op == 2 || op == 3) { w16(p, r.below(0x60)); p += 2; }
      else if (op == 4 || op == 5) { img[p++] = (uint8_t)(1 + r.below(32)); img[p++] = (uint8_t)r.below(32); }
      else if (op >= 6) { img[p++] = (uint8_t)(1 + r.below(32)); img[p++] = (uint8_t)r.below(32); img[p++] = (uint8_t)r.below(32); }
    }
    img[p++] = 9; img[p++] = 0; img[p++] = 0;   // IIO_END + 2 bytes
  }
  img[p] = 0;
  // command tables
  for (uint32_t t = 0; t < NTBL; t++) {
    if (t > 0 && r.pct(15)) continue;
    uint32_t base = TBL0 + t * TBL_STRIDE;
    w16(CMT + 4 + 2 * t, base);
    int nins = 3 + r.below(40);
    Gen g(r, (int)t, nins);
    std::vector<Ins> ins;
    for (int k = 0; k < nins; k++) ins.push_back(g.make(k));
    std::vector<uint32_t> off(nins + 1);
    uint32_t pos = 6;
    for (int k = 0; k < nins; k++) { off[k] = pos; pos += (uint32_t)ins[k].b.size(); }
    off[nins] = pos;   // the EOT
    if (t == 0 && t0_off) *t0_off = off;
    if (pos + 1 > TBL_STRIDE) { fprintf(stderr, "generator: table %u is %u bytes, over %u\n", t, pos + 1, TBL_STRIDE); exit(2); }
    for (int k = 0; k < nins; k++)
      for (auto &pt : ins[k].patches) { uint32_t tgt = off[std::min(pt.second, nins)]; ins[k].b[pt.first] = tgt & 0xff; ins[k].b[pt.first + 1] = tgt >> 8; }
    uint32_t q = base + 6;
    for (int k = 0; k < nins; k++) { memcpy(&img[q], ins[k].b.data(), ins[k].b.size()); q += (uint32_t)ins[k].b.size(); }
    img[q++] = r.pct(10) ? (uint8_t)(r.pct(50) ? 0 : 127 + r.below(129)) : 91;   // EOT, or an opcode 0 / >= ATOM_OP_CNT that ends the table
    w16(base, q - base); img[base + 2] = 1; img[base + 3] = 1; img[base + 4] = (uint8_t)r.below(7); img[base + 5] = (uint8_t)(r.below(2) * 4);   // PS shift 0/1: nested windows never pass the 16-dword parameter array (see the interpreter's header comment)
  }
  return img;
}

// ---- the two runs ----
struct Ev { char k; uint32_t a, b; bool operator==(const Ev &o) const { return k == o.k && a == o.a && b == o.b; } };
struct Run { std::vector<Ev> tr; std::map<uint32_t, uint32_t> regs; };
static Run *cur;
static uint32_t reg_init(uint32_t i) { return (i * 2654435761u) ^ 0x5a5a5a5au; }
static uint32_t c_rd(uint32_t i) { auto it = cur->regs.find(i); uint32_t v = it == cur->regs.end() ? reg_init(i) : it->second; cur->tr.push_back({'R', i, v}); return v; }
static void c_wr(uint32_t i, uint32_t v) { cur->regs[i] = v; cur->tr.push_back({'W', i, v}); }
static void c_delay(int ms, unsigned n) { cur->tr.push_back({ms ? 'm' : 'u', n, 0}); }
static uint64_t fake_ns = 0;
static uint32_t p_rd(void *, uint32_t i) { return c_rd(i); }
static void p_wr(void *, uint32_t i, uint32_t v) { c_wr(i, v); }
static void p_dus(void *, uint32_t us) { c_delay(0, us); }
static void p_dms(void *, uint32_t ms) { c_delay(1, ms); }
static uint64_t p_now(void *) { return fake_ns += 1000; }
static void p_log(void *, const char *) {}

// ---- directed cases ----
// A 64 KiB image (build() keeps everything below 64 KiB) whose table 0 is `code`: at TBL0, or ending exactly at the image end (`at_end`).
static std::vector<uint8_t> image(std::initializer_list<uint8_t> code, bool at_end = false) {
  Rng r{3}; std::vector<uint8_t> img = build(r); img.resize(0x10000);
  uint32_t n = (uint32_t)code.size(), base = at_end ? 0x10000 - 6 - n : TBL0;
  std::copy(code.begin(), code.end(), img.begin() + base + 6);
  img[base] = (6 + n) & 0xff; img[base + 1] = (6 + n) >> 8; img[base + 2] = img[base + 3] = 1; img[base + 4] = 0; img[base + 5] = 0;   // no WS, PS shift 0
  img[CMT + 4] = base & 0xff; img[CMT + 5] = base >> 8;
  return img;
}
static void w16(std::vector<uint8_t> &img, uint32_t o, uint32_t v) { img[o] = v & 0xff; img[o + 1] = (v >> 8) & 0xff; }
static void methods(std::vector<uint8_t> &img, std::initializer_list<uint8_t> m) { std::copy(m.begin(), m.end(), img.begin() + IIOB); img[IIOB + m.size()] = 0; }
static uint32_t fwi(const std::vector<uint8_t> &img) { return img[MDT + 0xC] | img[MDT + 0xD] << 8; }

struct Both { int ra = 0, rb = 0; bool lx = false; Run a, b; uint32_t pa[16], pb[16]; std::vector<uint32_t> sa, sb; };
// img[0..size) on the port and, when `linux`, on Linux: table `index`, or amdgpu_atom_asic_init when index < 0. A refused image reads
// 1000 + the port's rc (Linux: 1000 - 22). Parameters start as 0x12345600 + i, so a store shows.
static Both both(atom_ctx *g, const std::vector<uint8_t> &img, int index, bool linux, uint32_t size = 0x10000) {
  Both r;
  for (int i = 0; i < 16; i++) r.pa[i] = r.pb[i] = 0x12345600u + i;
  r.sa.assign(SCRATCH / 4 + 4, 0);
  atom_card card = {nullptr, p_rd, p_wr, p_dus, p_dms, p_now, p_log};
  cur = &r.a; int pr = atom_parse(g, &card, img.data(), size, r.sa.data(), SCRATCH);
  r.ra = pr ? 1000 + pr : index < 0 ? atom_asic_init(g) : atom_execute_table(g, (uint32_t)index, r.pa, 16);
  if (!linux) return r;
  r.lx = true; cur = &r.b; void *o = oracle_parse(img.data(), size, SCRATCH, 4);
  r.rb = o ? (index < 0 ? oracle_asic_init(o) : oracle_execute(o, index, r.pb, 16)) : 1000 - 22;
  if (o) { r.sb.assign(oracle_scratch(o), oracle_scratch(o) + SCRATCH / 4 + 4); oracle_destroy(o); }
  return r;
}
static bool same(const Both &r) {
  return r.ra == r.rb && r.a.tr == r.b.tr && !memcmp(r.pa, r.pb, sizeof(r.pa)) && (r.sb.empty() || !memcmp(r.sa.data(), r.sb.data(), SCRATCH));
}
static int check(bool ok, const char *what, const Both &r) {
  if (r.lx) printf("%s %s: port %d, linux %d, %zu events\n", ok ? "ok  " : "FAIL", what, r.ra, r.rb, r.a.tr.size());
  else printf("%s %s: port %d, %zu events\n", ok ? "ok  " : "FAIL", what, r.ra, r.a.tr.size());
  return ok ? 0 : 1;
}
static std::string logs;
static void cap_log(void *, const char *m) { logs += m; logs += '\n'; }
static uint64_t calls;
static uint64_t loop_clock(void *) { return ++calls >= 5 ? 21000000000ull + calls : calls * 1000; }   // the 2nd jump comes 21 s after the 1st
static uint64_t late_clock(void *) { return ++calls == 1 ? 1000 : 1000 + 20000000001ull; }              // 20 s pass before the first opcode

static int directed(atom_ctx *g) {
  int f = 0;
  const uint32_t P0 = 0x12345600u;
  // where Linux is defined: identical on both
  { std::vector<uint8_t> a = image({91}), b = a, c = a;
    a[0] = 0; b[0x31] = 'X'; c[HDR + 4] = 'X';
    Both ra = both(g, a, 0, true), rb = both(g, b, 0, true), rc = both(g, c, 0, true);
    f += check(same(ra) && ra.ra == 978, "parse refuses an image without 0xAA55", ra);
    f += check(same(rb) && rb.ra == 978, "parse refuses a wrong ATI magic", rb);
    f += check(same(rc) && rc.ra == 978, "parse refuses a ROM table without \"ATOM\"", rc);
    for (int i = 1; i < 4; i++) { std::vector<uint8_t> d = image({91}); d[HDR + 4 + i] = 'X'; Both r = both(g, d, 0, true); f += check(same(r) && r.ra == 978, "  ... or with one of T, O, M wrong", r); } }
  { std::vector<uint8_t> img = image({3, 0x05, 0, 0, 0, 0, 0,  45, 0x05, 0, 1, 0, 0, 0,  62, 0x05, 0, 3, 0, 0, 0,  69, 13, 0,  2, 0x02, 0, 0,  91});
    img[TBL0 + 4] = 1;   // one WS dword. WS[0] = 0; do WS[0] += 1 while WS[0] < 3 (JUMP_BELOW back to the ADD); PS[0] = WS[0]
    Both r = both(g, img, 0, true);
    f += check(same(r) && r.ra == 0 && r.pa[0] == 3, "backward loop: the same jump target taken twice within 20 s", r); }
  { Both r = both(g, image({91}), 9, true);
    f += check(same(r) && r.ra == ATOM_EINVAL && r.a.tr.empty(), "absent table -> -EINVAL", r); }
  { std::vector<uint8_t> img = image({91}); uint32_t h = fwi(img); memset(&img[h + 8], 0, 4);
    Both r = both(g, img, -1, true);
    f += check(same(r) && r.ra == 1 && r.a.tr.empty(), "asic_init with default sclk 0 -> 1", r); }
  { std::vector<uint8_t> img = image({91}); uint32_t h = fwi(img); img[h + 8] = 1; memset(&img[h + 12], 0, 4);
    Both r = both(g, img, -1, true);
    f += check(same(r) && r.ra == 1 && r.a.tr.empty(), "asic_init with default mclk 0 -> 1", r); }
  { std::vector<uint8_t> img = image({91}); uint32_t h = fwi(img); img[h + 8] = img[h + 12] = 1; w16(img, CMT + 4, 0);
    Both r = both(g, img, -1, true);
    f += check(same(r) && r.ra == 1 && r.a.tr.empty(), "asic_init without ASIC_Init -> 1", r); }
  { std::vector<uint8_t> img = image({55, 2, 0, 2, 0x00, 0, 0, 0,  55, 4, 0, 2, 0x00, 1, 0, 0,  91});   // PS[0] = REG via method 2, PS[1] via 4
    methods(img, {1, 2, 2, 0x10, 0, 9, 0, 0,  1, 3, 10,  1, 4, 2, 0x11, 0, 9, 0, 0});                    // method 3 holds IIO opcode 10
    Both r = both(g, img, 0, true);
    f += check(same(r) && r.ra == 0 && r.a.tr.size() == 1 && r.pa[0] == reg_init(0x10) && r.pa[1] == 0,
               "IIO indexing stops at an unknown opcode (method 4 never indexed)", r); }
  { std::vector<uint8_t> img = image({55, 5, 0, 2, 0x00, 0, 0, 0, 91});
    const uint8_t m[] = {1, 5, 2, 0x10, 0x00};   // START 5: READ 0x10, no END before the image ends
    memcpy(&img[0x10000 - sizeof(m)], m, sizeof(m)); w16(img, MDT + 0x32, 0x10000 - sizeof(m) - 4);
    Both r = both(g, img, 0, true);
    f += check(same(r) && r.ra == 0 && r.a.tr.empty() && r.pa[0] == 0, "IIO method without END is not indexed", r); }
  { std::vector<uint8_t> img = image({55, 6, 0, 2, 0x00, 0, 0, 0, 91});
    methods(img, {1, 6, 2, 0x10, 0, 1, 0, 3, 0x11, 0, 9, 0, 0});   // READ 0x10, then START inside the method (unknown at run time), WRITE
    Both r = both(g, img, 0, true);
    f += check(same(r) && r.ra == 0 && r.a.tr.size() == 1 && r.a.tr[0].k == 'R' && r.pa[0] == 0, "IIO opcode START inside a method -> 0", r); }
  { Both r = both(g, image({66, 0x25, 7, 2, 0x25, 0, 0x77, 91}), 0, true);   // SWITCH 7 on a byte that is no case: the byte runs as MOVE PS[0].b0
    f += check(same(r) && r.ra == 0 && r.pa[0] == ((P0 & ~0xFFu) | 0x77), "switch: bad case, execution goes on at that byte", r); }
  { Both r = both(g, image({3, 0x05, 0x43, 5, 0, 0, 0,  2, 0x02, 0, 0x44,  2, 0x02, 1, 0x45,  2, 0x02, 2, 0x43,  91}), 0, true);
    f += check(same(r) && r.pa[0] == 0x20 && r.pa[1] == ~0x20u && r.pa[2] == 5, "WS_SHIFT = 5 -> OR_MASK 0x20, AND_MASK ~0x20", r); }
  { Both r = both(g, image({3, 0x05, 0x46, 0x00, 0x01, 0, 0,  4, 0x05, 1, 0xCD, 0xAB, 0, 0,  2, 0x03, 0, 1,  2, 0x02, 1, 0x46,  91}), 0, true);
    f += check(same(r) && r.sa[0x41] == 0xABCD && r.pa[0] == 0xABCD && r.pa[1] == 0x100, "WS_FB_WINDOW = 0x100 moves FB[1] to dword 0x41", r); }
  // the port's fail-closed contract (Linux would read past the image): a read past the end -> ATOM_EFAULT, nothing after that read happens
  { Both r = both(g, image({}, true), 0, false);
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty(), "opcode fetch past the image", r); }
  { Both r = both(g, image({2, 0x05, 0x00, 0x11, 0x22}, true), 0, false);
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty() && r.pa[0] == P0, "MOVE PS[0] = IMM32 cut: PS[0] not stored", r); }
  { Both r = both(g, image({1, 0x05, 0x10, 0x00, 0x11}, true), 0, false);
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty(), "MOVE REG[0x10] = IMM32 cut: no register write", r); }
  { Both r = both(g, image({2, 0x00, 0x00, 0x10}, true), 0, false);
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty() && r.pa[0] == P0, "REG source index cut (MM): no register read", r); }
  { std::vector<uint8_t> img = image({55, 7, 0, 2, 0x00, 0x00, 0x10}, true); methods(img, {1, 7, 3, 0x20, 0, 9, 0, 0});
    Both r = both(g, img, 0, false);
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty(), "REG source index cut (IIO): the method does not run", r); }
  { Both r = both(g, image({102, 255, 2, 0x04, 0x00, 0xF0, 0xFF, 91}), 0, false);   // data block = this table, ID[0xFFF0] past the end
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty() && r.pa[0] == P0, "ID source past the image: PS[0] not stored", r); }
  { Both r = both(g, image({4, 0x05, 1, 0x55, 0x55, 0x55, 0x55,  102, 255, 4, 0x04, 1, 0xF0, 0xFF, 91}), 0, false);
    f += check(r.ra == ATOM_EFAULT && r.sa[1] == 0x55555555u, "ID source past the image: FB[1] not stored", r); }
  { std::vector<uint8_t> img = image({55, 7, 0, 102, 255, 1, 0x04, 0x10, 0x00, 0xF0, 0xFF, 91}); methods(img, {1, 0x87, 3, 0x20, 0, 9, 0, 0});
    Both r = both(g, img, 0, false);
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty(), "ID source past the image: the IIO write method does not run", r); }
  { Both r = both(g, image({80}, true), 0, false);
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty(), "DELAY count cut: no delay", r); }
  { Both r = both(g, image({82}, true), 0, false);
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty(), "CALL_TABLE index cut: no table runs", r); }
  { Both r = both(g, image({66, 0x25, 7, 0x63, 0x01}, true), 0, false);
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty(), "SWITCH case list cut", r); }
  { std::vector<uint8_t> img = image({91}); w16(img, HDR + 0x1E, 0xFFFE);
    Both r = both(g, img, 0, false);
    f += check(r.ra == ATOM_EFAULT, "command table entry past the image", r); }
  { std::vector<uint8_t> img = image({91}); w16(img, CMT + 4, 0xFFFE);
    Both r = both(g, img, 0, false);
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty(), "table header past the image", r); }
  { std::vector<uint8_t> img = image({91}); w16(img, HDR + 0x20, 0xFFF0);
    Both r = both(g, img, 0, false);
    f += check(r.ra == 1000 + ATOM_EFAULT, "parse: master data table past the image", r); }
  { std::vector<uint8_t> img = image({91}); w16(img, MDT + 0xC, 0xFFFE);
    Both r = both(g, img, -1, false);
    f += check(r.ra == ATOM_EFAULT && r.a.tr.empty(), "asic_init: firmware info past the image", r); }
  { Both r = both(g, image({59, 0x05, 0xFC, 0x4F, 0, 0,  4, 0x05, 1, 0x11, 0x11, 0x11, 0x11,  2, 0x03, 0, 1,  91}), 0, false);
    bool clean = true; for (uint32_t v : r.sa) clean = clean && v == 0;   // FB base 0x4FFC: FB[1] is the dword just past the 0x5000 bytes
    f += check(r.ra == 0 && clean && r.pa[0] == 0, "FB dword just past the scratch: neither stored nor read", r); }
  for (int k = 0; k < 3; k++) {   // the clock and the log: loop detection, the total deadline, no log callback
    std::vector<uint8_t> img = k == 0 ? image({67, 6, 0}) : k == 1 ? image({91}) : image({83, 91});   // JUMP to itself / EOT / REPEAT logs
    uint32_t ps[16] = {}, sc[SCRATCH / 4] = {};
    atom_card card = {nullptr, p_rd, p_wr, p_dus, p_dms, k == 0 ? loop_clock : k == 1 ? late_clock : p_now, k == 2 ? nullptr : cap_log};
    Run a; cur = &a; logs.clear(); calls = 0;
    int r = atom_parse(g, &card, img.data(), 0x10000, sc, SCRATCH) ? 999 : atom_execute_table(g, 0, ps, 16);
    bool ok = k == 0 ? r == ATOM_EINVAL && logs.find("stuck in loop") != std::string::npos && logs.find("in total") == std::string::npos
            : k == 1 ? r == ATOM_EINVAL && logs.find("in total") != std::string::npos
            : r == 0;
    static const char *what[3] = {"a jump to the same target 21 s later aborts", "20 s in total abort the table", "no log callback"};
    printf("%s %s: port %d, %zu events\n", ok ? "ok  " : "FAIL", what[k], r, a.tr.size());
    f += !ok;
  }
  return f;
}

// The fail-closed contract on random programs: table 0 moved to the end of a 64 KiB image that ends inside its instruction k. Compared with
// a reference that ends at instruction k (EOT, or opcode 0, written there) and with the uncut image: the cut run's register and delay events
// extend the reference's and are a prefix of the uncut run's (instruction k may read operands before the one cut off, never anything after
// it), its parameters and scratch are the reference's (a store is an instruction's last step), and where the reference ends it returns
// ATOM_EFAULT. When the two end markers give different results the reference read that byte as data: the case is skipped, and counted.
static int cut_test(atom_ctx *g, int N, uint64_t seed) {
  Rng rng{seed}; int fails = 0, faulted = 0, skipped = 0;
  for (int n = 0; n < N; n++) {
    std::vector<uint32_t> off;
    std::vector<uint8_t> img = build(rng, &off); img.resize(0x10000);
    uint32_t L = img[TBL0] | img[TBL0 + 1] << 8, base = 0x10000 - L;
    memmove(&img[base], &img[TBL0], L); w16(img, CMT + 4, base);
    uint32_t c = 6 + rng.below(L - 6);
    size_t k = 0; while (k + 1 < off.size() && off[k + 1] <= c) k++;
    uint32_t size = base + c, rsize = std::max(size, base + off[k] + 1);
    auto run = [&](uint32_t sz, int mark) { std::vector<uint8_t> im = img; if (mark >= 0) im[base + off[k]] = (uint8_t)mark; return both(g, im, n & 1 ? -1 : 0, false, sz); };
    Both cut = run(size, -1), ra = run(rsize, ATOM_OP_EOT), rb = run(rsize, 0), full = run(0x10000, -1);
    auto stores = [](const Both &x, const Both &y) { return !memcmp(x.pa, y.pa, sizeof(x.pa)) && x.sa == y.sa; };
    auto prefix = [](const std::vector<Ev> &p, const std::vector<Ev> &w) { return p.size() <= w.size() && std::equal(p.begin(), p.end(), w.begin()); };
    if (ra.ra != rb.ra || ra.a.tr != rb.a.tr || !stores(ra, rb)) { skipped++; continue; }
    if (cut.ra == ATOM_EFAULT) faulted++;
    bool ok = stores(cut, ra) && prefix(ra.a.tr, cut.a.tr) && prefix(cut.a.tr, full.a.tr) &&
              (cut.ra == ATOM_EFAULT ? ra.ra == 0 || ra.ra == ATOM_EFAULT : cut.ra == ra.ra && cut.a.tr == ra.a.tr);
    if (!ok) {
      if (++fails <= 5) printf("CUT FAIL program %d: cut at table byte %u (instruction %zu at %u), rc %d vs reference %d, events %zu vs %zu\n", n, c, k,
                               off[k], cut.ra, ra.ra, cut.a.tr.size(), ra.a.tr.size());
    }
  }
  printf("%s cut test: %d programs, %d returned ATOM_EFAULT, %d skipped (end marker read as data), %d failures\n", fails ? "FAIL" : "ok  ", N, faulted,
         skipped, fails);
  return fails;
}

int main(int argc, char **argv) {
  int N = argc > 1 ? atoi(argv[1]) : 5000;
  Rng rng{argc > 2 ? strtoull(argv[2], nullptr, 0) : 0x9e3779b97f4a7c15ull};
  oracle_io = {c_rd, c_wr, c_delay};
  int fails = 0, ran = 0; long events = 0; std::map<int, int> rc_hist;
  atom_ctx *g = (atom_ctx *)calloc(1, sizeof(atom_ctx));
  {   // directed: table 0 calls itself with PS shift 0 -> nesting reaches ATOM_EXECUTE_MAX_DEPTH (-ELOOP), every level above aborts (-EINVAL)
    Rng r0{1};
    std::vector<uint8_t> img = build(r0);
    const uint8_t code[] = {82, 0, 91};
    memcpy(&img[TBL0 + 6], code, sizeof(code));
    img[TBL0] = 6 + sizeof(code); img[TBL0 + 1] = 0; img[TBL0 + 4] = 0; img[TBL0 + 5] = 0;
    uint32_t pa_[16] = {}, pb_[16] = {};
    Run a, b;
    std::vector<uint32_t> sa(SCRATCH / 4 + 4, 0);
    atom_card card = {nullptr, p_rd, p_wr, p_dus, p_dms, p_now, p_log};
    cur = &a; int ra = atom_parse(g, &card, img.data(), IMG, sa.data(), SCRATCH) ? 999 : atom_execute_table(g, 0, pa_, 16);
    cur = &b; void *o = oracle_parse(img.data(), IMG, SCRATCH, 4); int rb = o ? oracle_execute(o, 0, pb_, 16) : 998;
    bool ok = ra == rb && ra == -22 && a.tr == b.tr;
    printf("%s recursion: port %d, linux %d (ATOM_EXECUTE_MAX_DEPTH -> -ELOOP inside, -EINVAL at the top)\n", ok ? "ok  " : "FAIL", ra, rb);
    if (!ok) fails++;
    if (o) oracle_destroy(o);
  }
  {   // directed: an indexed IIO method whose END sits in the image's last bytes -> execution stops at "runs past BIOS image", reads as 0
    Rng r0{2};
    std::vector<uint8_t> img = build(r0);
    const uint8_t method[] = {1, 7, 2, 0x10, 0x00, 9};   // IIO_START 7: READ reg 0x10; END at the last byte
    memcpy(&img[IMG - sizeof(method)], method, sizeof(method));
    img[MDT + 0x32] = (uint8_t)((IMG - sizeof(method) - 4) & 0xff); img[MDT + 0x33] = (uint8_t)((IMG - sizeof(method) - 4) >> 8);
    // ATOM_DATA_IIO_PTR is a u16: the method must lie below 64 KiB + 4 -> use a 64 KiB image for this case
    std::vector<uint8_t> small(img.begin(), img.begin() + 0x10000);
    memcpy(&small[0x10000 - sizeof(method)], method, sizeof(method));
    uint32_t iio = 0x10000 - sizeof(method) - 4;
    small[MDT + 0x32] = iio & 0xff; small[MDT + 0x33] = iio >> 8;
    const uint8_t code[] = {55, 7, 0, 2, 0x00, 0, 0x10, 0x00, 91};   // SETPORT ATI 7; MOVE PS[0] = REG[0x10] (through IIO method 7); EOT
    memcpy(&small[TBL0 + 6], code, sizeof(code));
    small[TBL0] = 6 + sizeof(code); small[TBL0 + 1] = 0; small[TBL0 + 4] = 0; small[TBL0 + 5] = 0;
    uint32_t pa_[16] = {1, 2}, pb_[16] = {1, 2};
    Run a, b;
    std::vector<uint32_t> sa(SCRATCH / 4 + 4, 0);
    atom_card card = {nullptr, p_rd, p_wr, p_dus, p_dms, p_now, p_log};
    cur = &a; int ra = atom_parse(g, &card, small.data(), 0x10000, sa.data(), SCRATCH) ? 999 : atom_execute_table(g, 0, pa_, 16);
    cur = &b; void *o = oracle_parse(small.data(), 0x10000, SCRATCH, 4); int rb = o ? oracle_execute(o, 0, pb_, 16) : 998;
    bool ok = ra == rb && ra == 0 && a.tr == b.tr && pa_[0] == pb_[0] && pa_[0] == 0 && a.tr.size() == 1;
    printf("%s IIO method running into the image end: port %d / PS[0] %u, linux %d / PS[0] %u, %zu register events\n", ok ? "ok  " : "FAIL", ra, pa_[0], rb,
           pb_[0], a.tr.size());
    if (!ok) fails++;
    if (o) oracle_destroy(o);
  }
  int df = directed(g);
  for (int n = 0; n < N; n++) {
    std::vector<uint8_t> img = build(rng);
    uint32_t params0[16]; for (auto &x : params0) x = rng.pct(50) ? rng.below(8) : rng.next();
    // the port
    Run a; cur = &a;
    std::vector<uint32_t> scratch_a(SCRATCH / 4 + 4, 0);
    atom_card card = {nullptr, p_rd, p_wr, p_dus, p_dms, p_now, p_log};
    int pa = atom_parse(g, &card, img.data(), IMG, scratch_a.data(), SCRATCH);
    uint32_t pa_params[16]; memcpy(pa_params, params0, sizeof(pa_params));
    bool asic = n & 1;   // odd programs: amdgpu_atom_asic_init (firmwareinfo boot clocks into PS[0..1], table 0)
    int ra = pa ? 1000 + pa : asic ? atom_asic_init(g) : atom_execute_table(g, 0, pa_params, 16);
    // Linux
    Run b; cur = &b;
    void *o = oracle_parse(img.data(), IMG, SCRATCH, 4);
    uint32_t pb_params[16]; memcpy(pb_params, params0, sizeof(pb_params));
    int rb = o ? (asic ? oracle_asic_init(o) : oracle_execute(o, 0, pb_params, 16)) : 1000 - 22;
    bool same = ra == rb && a.tr == b.tr && memcmp(pa_params, pb_params, sizeof(pa_params)) == 0 &&
                (!o || memcmp(scratch_a.data(), oracle_scratch(o), SCRATCH) == 0);
    if (!same) {
      fails++;
      if (fails <= 5) {
        size_t i = 0; while (i < a.tr.size() && i < b.tr.size() && a.tr[i] == b.tr[i]) i++;
        printf("MISMATCH program %d: rc port %d linux %d, trace %zu/%zu events, first difference at %zu", n, ra, rb, a.tr.size(), b.tr.size(), i);
        if (i < a.tr.size()) printf(" port %c %x %x", a.tr[i].k, a.tr[i].a, a.tr[i].b);
        if (i < b.tr.size()) printf(" linux %c %x %x", b.tr[i].k, b.tr[i].a, b.tr[i].b);
        printf("%s%s\n", memcmp(pa_params, pb_params, sizeof(pa_params)) ? " params differ" : "",
               o && memcmp(scratch_a.data(), oracle_scratch(o), SCRATCH) ? " scratch differs" : "");
      }
    }
    if (o) oracle_destroy(o);
    ran++; events += (long)a.tr.size(); rc_hist[ra]++;
  }
  int cf = cut_test(g, N, rng.s);
  free(g);
  printf("%d directed cases failed; %d programs, %ld register/delay events compared, %d mismatches; return codes:", df, ran, events, fails);
  for (auto &h : rc_hist) printf(" %d x%d", h.first, h.second);
  printf("\n");
  return fails || df || cf ? 1 : 0;
}
