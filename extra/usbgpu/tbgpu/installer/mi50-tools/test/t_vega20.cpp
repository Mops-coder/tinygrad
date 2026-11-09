// t_vega20.cpp - egpu_vega20.py moved into the TinyGPU driver (Leon 2026-10-16): MI50Device's tuning - controls, read_limits, read_metrics,
// read_overdrive, set_perf_control and OverDrive - on the virtual MI50, booted on the V420 table as StartMI50 boots it: stock values up to
// Leon's ceilings, edited in the OverDrive table. Every edit is checked where it lands: the SMU's OverDrive table, its messages, the error
// text. TinyGPUDriver.cpp is compiled unchanged against the stand-ins.
// Build/run: see mi50-tools/build_and_test.sh.
#include <algorithm>
#include <functional>
#include <memory>
#include <zlib.h>   // crc32: the oracle for the driver's CRC-32 (link with -lz)
#ifndef MI50_DRIVER_CPP   // a driver copy chosen at build time (mutation checks)
#define MI50_DRIVER_CPP "../../TinyGPUDriverExtension/TinyGPUDriver.cpp"
#endif
#include MI50_DRIVER_CPP
#include "dk_card.hpp"

using namespace vm;
using mi50::MI50Device;
using OD = MI50Device::OverDrive;

// MI50Device on a virtual card, as StartMI50 builds it (t_mi50's DevHw / Dev): the card behind the IOPCIDevice, BAR5, the PowerPlay table the
// card names, a provider chain with the card
struct DevHw {
  Card *card; IOService svc; IOPCIDevice pci; const uint8_t *pp = nullptr; uint32_t pp_n = 0;
  explicit DevHw(Card &c) : card(&c) { pp = c.powerplay_table(&pp_n); bind(); }
  void bind() { CARD = card; BAR5.err = 0; BAR5.idx = 3; BAR5.size = (uint64_t)card->mmio_size_dwords * 4; set_chain({other_entry(), pci_entry(0x1002, 0x66a1)}); }
};
struct Dev : DevHw, MI50Device {
  explicit Dev(Card &c) : DevHw(c), MI50Device(&svc, &pci, BAR5.idx, BAR5.size, pp, pp_n) {}
  ~Dev() { bind(); }
};
static int nfail = 0, nok = 0;
static void check(bool c, const std::string &m) { printf("%s %s\n", c ? "ok  " : "FAIL", m.c_str()); if (c) nok++; else nfail++; }
static LinuxFw FW;
static const int64_t N = OD::NONE;
static const uint32_t OD_T = (uint32_t)TABLE_OVERDRIVE;

// a card as IOPCIFamily + MI50BarFix leave it (t_dext card): REBAR 256 MiB, BAR0 placed, BAR2, BAR5; v420: the driver's boot table
static std::unique_ptr<Card> card(bool v420, uint64_t refuse = 0) {
  std::unique_ptr<Card> c(new Card(build_vbios().img, FW));
  if (v420) c->pp_source.assign(mi50::pp::V420_PPTABLE, mi50::pp::V420_PPTABLE + sizeof(mi50::pp::V420_PPTABLE));
  c->smu_refuse = refuse;
  c->start();
  c->cfg_wr(CFG_REBAR + 8, 8u << 8, 4);
  c->cfg_wr(0x10, 0x50000000, 4); c->cfg_wr(0x14, 0x10, 4); c->cfg_wr(0x18, 0x40000000, 4); c->cfg_wr(0x1c, 0, 4); c->cfg_wr(0x24, 0x40200000, 4);
  c->cfg_writes.clear(); c->events.clear();
  return c;
}
static OverDriveTable_t smu_od(Card &c) { OverDriveTable_t t; memset(&t, 0, sizeof(t)); auto &v = c.smu.tables[OD_T]; if (v.size() == 32) memcpy(&t, v.data(), 32); return t; }
static bool od_eq(Card &c, const OverDriveTable_t &w) { OverDriveTable_t t = smu_od(c); return memcmp(&t, &w, 32) == 0; }
static std::vector<std::pair<uint32_t, uint32_t>> since(Card &c, size_t n) { return {c.smu_log.begin() + (long)n, c.smu_log.end()}; }
static std::vector<std::pair<uint32_t, uint32_t>> dpm_reads(Card &c, uint32_t clk) {
  std::vector<std::pair<uint32_t, uint32_t>> e = {{PPSMC_MSG_GetDpmFreqByIndex, clk << 16 | 0xFF}};
  auto lv = c.levels(clk);
  for (uint32_t i = 0; i < lv.size(); i++) e.push_back({PPSMC_MSG_GetDpmFreqByIndex, clk << 16 | i});
  return e;
}
// the messages of one OverDrive table upload (vega20_copy_table_to_smc)
static std::vector<std::pair<uint32_t, uint32_t>> od_upload(MI50Device &) {
  return {{PPSMC_MSG_SetDriverDramAddrHigh, 0}, {PPSMC_MSG_SetDriverDramAddrLow, 0}, {PPSMC_MSG_TransferTableDram2Smu, OD_T}};
}
// the address params are the buffer's MC address: compared by message only for those two
static bool same_msgs(const std::vector<std::pair<uint32_t, uint32_t>> &got, const std::vector<std::pair<uint32_t, uint32_t>> &want) {
  if (got.size() != want.size()) return false;
  for (size_t i = 0; i < got.size(); i++) {
    if (got[i].first != want[i].first) return false;
    bool addr = want[i].first == PPSMC_MSG_SetDriverDramAddrHigh || want[i].first == PPSMC_MSG_SetDriverDramAddrLow;
    if (!addr && got[i].second != want[i].second) return false;
  }
  return true;
}
static std::vector<std::pair<uint32_t, uint32_t>> cat(std::vector<std::pair<uint32_t, uint32_t>> a, const std::vector<std::pair<uint32_t, uint32_t>> &b) {
  a.insert(a.end(), b.begin(), b.end()); return a;
}
// the messages of one DPM level forcing on the card's current levels (MI50Device::set_clocks: soft min GFXCLK / UCLK / SOCCLK, then soft max)
static std::vector<std::pair<uint32_t, uint32_t>> forced(Card &c, MI50Device::Perf p) {
  std::vector<std::pair<uint32_t, uint32_t>> e;
  const uint32_t clk[3] = {PPCLK_GFXCLK, PPCLK_UCLK, PPCLK_SOCCLK};
  for (int hi = 0; hi < 2; hi++)
    for (uint32_t k : clk) {
      auto lv = c.levels(k);
      uint32_t f = p == MI50Device::PERF_HIGH ? lv.back() : p == MI50Device::PERF_LOW ? lv.front() : hi ? lv.back() : lv.front();
      e.push_back({hi ? (uint32_t)PPSMC_MSG_SetSoftMaxByFreq : (uint32_t)PPSMC_MSG_SetSoftMinByFreq, k << 16 | f});
    }
  return e;
}
static size_t count_msg(const Card &c, uint32_t msg) { size_t n = 0; for (auto &m : c.smu_log) n += m.first == msg; return n; }
static bool err_is(MI50Device &d, const std::string &s) { return s == d.error(); }
static std::string E(MI50Device &d) { return std::string(" [") + d.error() + "]"; }

int main(int argc, char **argv) {
  if (argc != 2) { fprintf(stderr, "usage: t_vega20 <vega20 firmware dir>\n"); return 2; }
  FW = linux_fw(argv[1]);

  // ================= C: the driver's CRC-32 (the overdrive file's checksum) =================
  { uint8_t b[64]; for (int i = 0; i < 64; i++) b[i] = (uint8_t)(i * 37 + 11);
    check(mi50::crc32_ieee((const uint8_t *)"123456789", 9) == 0xCBF43926u && mi50::crc32_ieee(b, 42) == (uint32_t)crc32(0L, b, 42) &&
          mi50::crc32_ieee(b, 0) == 0, "C1: CRC-32 check value 0xCBF43926, equal to zlib's crc32 on 42 bytes, 0 for none"); }

  // ================= R: the V420 table, fan control off (Leon: "fan off for now") =================
  std::unique_ptr<Card> c = card(true, 1ull << FEATURE_FAN_CONTROL_BIT);
  Dev d(*c);
  uint32_t ctl = 0;
  check(!d.controls(&ctl) && err_is(d, "the card is not initialised (init() has not succeeded)"), "R0: before init() -> refused" + E(d));
  { OD early(d); MI50Device::OdView ev;
    check(!early.ok() && !early.boot_od(&ev) && err_is(d, "the card is not initialised (init() has not succeeded)"),
          "R0b: an OverDrive opened before init() -> not ok, boot_od refused instead of zeros" + E(d)); }
  check(d.init() == MI50Device::FULL_BOOT && err_is(d, ""), "R1: booted on the V420 table, no stale reason from R0 left in error()" + E(d));
  check(d.controls(&ctl) && ctl == (MI50Device::CTL_PERF | MI50Device::CTL_GFXCLK_MIN | MI50Device::CTL_GFXCLK_MAX | MI50Device::CTL_UCLK |
                                    MI50Device::CTL_VOLTAGE | MI50Device::CTL_PPT),
        "R2: controls = perf, GPU minimum (Leon 2026-10-17), GPU maximum, memory maximum, voltage, power cap");
  MI50Device::Limits l;
  bool ok = d.read_limits(&l);
  check(ok && l.gfxclk_mhz[0] == 925 && l.gfxclk_mhz[1] == 1900 && l.gfxclk_max_mhz[0] == 1800 && l.gfxclk_max_mhz[1] == 2000 && l.uclk_mhz[0] == 1000 &&
        l.uclk_mhz[1] == 1200 && l.voltage_offset_mv[0] == 738 - 1125 && l.voltage_offset_mv[1] == 0 && l.ppt_pct[0] == 0 && l.ppt_pct[1] == 68 &&
        l.power_limit_ac_w == 178 && l.power_w_floor && d.od8_caps() == 0,
        "R3: limits = GPU minimum 925..1900 (the maximum's ceiling - 100 MHz, Leon 2026-10-17), maximum 1800..2000, memory 1000..1200, voltage -387..0 mV (738..1125), power 0..+68 % of 178 W (299.04 W: within "
        "300 W under any rounding; +69 % = 300.82 W), no OD8 capability with FAN_CONTROL off");
  // readouts
  { SmuMetrics_t x; memset(&x, 0, sizeof(x));
    x.CurrClock[PPCLK_GFXCLK] = 1700; x.CurrClock[PPCLK_SOCCLK] = 970; x.CurrClock[PPCLK_UCLK] = 1000; x.CurrClock[PPCLK_VCLK] = 300; x.CurrClock[PPCLK_DCLK] = 301;
    x.AverageGfxclkFrequency = 1650; x.AverageSocclkFrequency = 960; x.AverageUclkFrequency = 990; x.AverageGfxActivity = 87; x.AverageUclkActivity = 45;
    x.CurrSocketPower = 171; x.AverageSocketPower = 99; x.TemperatureEdge = 61; x.TemperatureHotspot = 74; x.TemperatureHBM = 66; x.TemperatureVrGfx = 58;
    x.TemperatureVrSoc = 52; x.TemperatureVrMem0 = 50; x.ThrottlerStatus = 0x00012345;
    c->smu.tables[TABLE_SMU_METRICS].assign((uint8_t *)&x, (uint8_t *)&x + sizeof(x));
    c->regs[SMUIO_SMUSVI0_TEL_PLANE0] = 0x60u << 16 | 0x12; c->smn[0x11140288] = 6u << 4; c->smn[0x11140290] = 3u << 14;
    c->regs[THM_CG_MULT_THERMAL_STATUS] = 0xFFFC0000u | 77u << 9 | 0x1ABu;   // CTF_TEMP 77 between set bits above and ASIC_MAX_TEMP below
    size_t n0 = c->smu_log.size();
    MI50Device::Metrics m; memset(&m, 0xAB, sizeof(m));
    ok = d.read_metrics(&m);
    check(ok && m.clk_gfx == 1700 && m.clk_soc == 970 && m.clk_mem == 1000 && m.clk_vclk == 300 && m.clk_dclk == 301 && m.avg_gfx == 1650 && m.avg_soc == 960 &&
          m.avg_mem == 990 && m.vddgfx_mv == 950 && m.load_gfx == 87 && m.load_mem == 45 && m.power_w == 171 && m.t_edge == 61 && m.t_hotspot == 74 &&
          m.t_mem == 66 && m.t_vr_gfx == 58 && m.t_vr_soc == 52 && m.t_vr_mem0 == 50 && m.fan_rpm == 0 && m.pcie_speed_x10 == 160 && m.pcie_lanes == 16 &&
          m.throttler_status == 0x12345 && m.t_hotspot_reg == 77,
          "R4: metrics = the SMU's metrics table, power = CurrSocketPower (not AverageSocketPower, Leon 2026-10-17), VDDGFX 950 mV (vid 0x60), fan 0 RPM "
          "(no fan fitted), PCIe 16 GT/s x16, the THM register's hotspot = CTF_TEMP 77 alone");
    check(same_msgs(since(*c, n0), {{PPSMC_MSG_SetDriverDramAddrHigh, 0}, {PPSMC_MSG_SetDriverDramAddrLow, 0}, {PPSMC_MSG_TransferTableSmu2Dram, TABLE_SMU_METRICS},
                                    {PPSMC_MSG_GetCurrentRpm, 0}}), "R5: a sample = TransferTableSmu2Dram(SMU_METRICS) + GetCurrentRpm, nothing written");
    c->regs[SMUIO_SMUSVI0_TEL_PLANE0] = 0xFFu << 16; c->smn[0x11140288] = 7u << 4; c->smn[0x11140290] = 0;
    ok = d.read_metrics(&m);
    check(ok && m.vddgfx_mv == (uint16_t)((6200 - 255 * 25) / 4) && m.pcie_lanes == 0 && m.pcie_speed_x10 == 25,
          "R6: vid 0xFF -> convert_to_vddc's uint16 wrap (" + std::to_string(m.vddgfx_mv) + "), width code 7 -> 0 lanes, speed code 0 -> 2.5 GT/s"); }
  // perf: the DPM level forcing (GFXCLK, UCLK, SOCCLK: soft min all, then soft max all)
  { auto force = [&](MI50Device::Perf p) { size_t n0 = c->smu_log.size(); bool r = d.set_perf_control(p); return std::make_pair(r, since(*c, n0)); };
    auto mm = [](uint32_t g0, uint32_t u0, uint32_t s0, uint32_t g1, uint32_t u1, uint32_t s1) {
      return std::vector<std::pair<uint32_t, uint32_t>>{{PPSMC_MSG_SetSoftMinByFreq, PPCLK_GFXCLK << 16 | g0}, {PPSMC_MSG_SetSoftMinByFreq, PPCLK_UCLK << 16 | u0},
        {PPSMC_MSG_SetSoftMinByFreq, PPCLK_SOCCLK << 16 | s0}, {PPSMC_MSG_SetSoftMaxByFreq, PPCLK_GFXCLK << 16 | g1}, {PPSMC_MSG_SetSoftMaxByFreq, PPCLK_UCLK << 16 | u1},
        {PPSMC_MSG_SetSoftMaxByFreq, PPCLK_SOCCLK << 16 | s1}}; };
    auto hi = force(MI50Device::PERF_HIGH), lo = force(MI50Device::PERF_LOW), au = force(MI50Device::PERF_AUTO);
    check(hi.first && hi.second == mm(1800, 1000, 970, 1800, 1000, 970) && lo.first && lo.second == mm(925, 350, 310, 925, 350, 310) && au.first &&
          au.second == mm(925, 350, 310, 1800, 1000, 970), "R7: perf high / low / auto = top / lowest level forced, auto = lowest..top (V420 GFX 925..1800, HBM 350..1000)"); }
  // the OverDrive
  OD od(d);
  MI50Device::OdView v;
  od.boot_od(&v);
  const OverDriveTable_t boot = smu_od(*c);
  check(od.ok() && od.v0_mv() == 1125 && v.gfxclk_mhz[0] == l.gfxclk_mhz[0] && v.gfxclk_mhz[1] == l.gfxclk_max_mhz[0] && v.uclk_mhz == l.uclk_mhz[0] &&
        !v.has_voltage_offset && v.ppt_pct == 0 && boot.GfxclkVolt3 == 0,
        "R8 (APP-03): opened; boot view's clocks = the stock levels read_limits starts its ranges at (" + std::to_string(v.gfxclk_mhz[0]) + ".." +
        std::to_string(v.gfxclk_mhz[1]) + " MHz, memory " + std::to_string(v.uclk_mhz) + "; the virtual SMU's boot table says " +
        std::to_string(boot.GfxclkFmin) + ".." + std::to_string(boot.GfxclkFmax) + " / " + std::to_string(boot.UclkFmax) + "), no cap in its curve, "
        "voltage knob reference 1125 mV");
  size_t n0 = c->smu_log.size();
  ok = od.gfxclk(N, 1900) && od.uclk(N, 1100) && od.voltage_offset(-100) && od.ppt_pct(20);
  check(ok && c->smu_log.size() == n0, "R9: edits accepted, nothing sent before commit" + E(d));
  ok = od.commit();
  OverDriveTable_t t = smu_od(*c), w = boot;
  w.GfxclkFmax = 1900; w.UclkFmax = 1100; w.GfxclkFreq1 = boot.GfxclkFmin; w.GfxclkFreq3 = 1900; w.GfxclkFreq2 = (uint16_t)((boot.GfxclkFmin + 1900) / 2);
  w.GfxclkVolt1 = w.GfxclkVolt2 = w.GfxclkVolt3 = 4 * 1025; w.OverDrivePct = 20;
  check(ok && memcmp(&t, &w, 32) == 0 && d.power_limit_w() == 178 && c->smu.ppt == 178 && count_msg(*c, PPSMC_MSG_SetPptLimit) == 0 &&
        d.perf() == MI50Device::PERF_AUTO &&
        same_msgs(since(*c, n0), cat(cat(cat(od_upload(d), dpm_reads(*c, PPCLK_GFXCLK)), dpm_reads(*c, PPCLK_UCLK)), forced(*c, MI50Device::PERF_AUTO))),
        "R10: commit -> the OverDrive table: GPU max 1900, memory max 1100, flat curve 1025 mV at GPU min / mid / max, OverDrivePct +20 %, the rest as "
        "booted; the GFX / memory levels re-read, then the requested level (auto) forced on them (F-01); no SetPptLimit (the power cap is the table's)" + E(d));
  ok = d.read_overdrive(&v);
  check(ok && v.gfxclk_mhz[0] == boot.GfxclkFmin && boot.GfxclkFmin != 925 && v.gfxclk_mhz[1] == 1800 && v.uclk_mhz == 1000 && v.ppt_pct == 20 &&
        v.has_voltage_offset && v.voltage_offset_mv[0] == -100 && v.voltage_offset_mv[1] == -100 && v.voltage_offset_mv[2] == -100 &&
        v.curve[0][1] == 1025 && v.curve[2][0] == 1900,
        "R11: read_overdrive = the SMU's read-back: the GPU minimum = the table's GfxclkFmin (" + std::to_string(boot.GfxclkFmin) + ", as OD_SCLK "
        "prints it; the virtual SMU's lowest GFX level is 925), the maximums its DPM levels (1800 / 1000), +20 %, curve cap 1025 mV = -100 mV");
  // ================= P: partial boot: a new device on the up card reads it =================
  { Dev d2(*c);
    check(d2.init() == MI50Device::ALREADY_UP, "P1: the same card again -> already up" + E(d2));
    size_t p0 = c->smu_log.size();
    uint32_t ctl2 = 0;
    ok = d2.controls(&ctl2);
    auto msgs = since(*c, p0);
    check(ok && ctl2 == ctl && msgs.empty() && od_eq(*c, w), "P2: a tuning call after a partial boot sends nothing, the tune runs on" + E(d2));
    MI50Device::Limits l2;
    check(d2.read_limits(&l2) && l2.gfxclk_max_mhz[1] == 2000 && l2.ppt_pct[1] == 68 && l2.power_limit_ac_w == 178, "P3: the same limits");
    // edits start from the running tune (Leon 2026-10-16): after the restart one edit keeps the rest of it
    OD o2(d2);
    ok = o2.ok() && o2.uclk(N, 1150) && o2.commit();
    OverDriveTable_t w2 = w; w2.UclkFmax = 1150;
    check(ok && od_eq(*c, w2), "P4: after a driver restart an edit keeps the running tune (GPU max 1900, flat curve 1025 mV, +20 %); memory max 1150" + E(d2));
    // Reset on a bring-up that read the card with nothing handed over to it (no boot of this driver instance recorded the table): no boot
    // table is known -> refused (Leon 2026-10-16), nothing sent, the tune left running (D-02: the refusal's text names that case)
    const char *nob = "Reset is not available: no boot of this driver instance recorded the boot OverDrive table (mi50::od)";
    size_t r0 = c->smu_log.size();
    bool r1 = o2.reset();
    MI50Device::OdView pv;
    bool r3 = o2.boot_od(&pv);
    check(!r1 && !r3 && err_is(d2, nob) && c->smu_log.size() == r0 && od_eq(*c, w2),
          "P5: after a restart with no table recorded, Reset and the boot view are refused, nothing written, the tune runs on" + E(d2));
    // with the profile's saved boot table given, Reset works after a restart too
    OD o3(d2, &w, sizeof(w));
    check(o3.ok() && o3.reset() && od_eq(*c, w), "P6: after a restart with a saved boot table given -> Reset uploads it" + E(d2));
    // A-03 / F-01: a bring-up that only read the card adopts what the one before recorded (mi50::bring_up): the boot table and the level
    { mi50::OverDriveTable_t rec; bool have = d.boot_od(&rec);   // the driver's own copy of the Linux struct
      d2.adopt(have ? &rec : nullptr, MI50Device::PERF_HIGH);
      OD o4(d2);
      MI50Device::OdView bv;
      bool br = o4.ok() && o4.boot_od(&bv);
      size_t q0 = c->smu_log.size();
      bool rr = o4.reset();
      check(have && memcmp(&rec, &boot, 32) == 0 && br && rr && od_eq(*c, boot) && d2.perf() == MI50Device::PERF_HIGH &&
            same_msgs(since(*c, q0), cat(cat(cat(od_upload(d2), dpm_reads(*c, PPCLK_GFXCLK)), dpm_reads(*c, PPCLK_UCLK)), forced(*c, MI50Device::PERF_HIGH))),
            "P7 (A-03, F-01): adopt() on a read-only bring-up -> the recorded boot table is known, Reset uploads it, the adopted level (high) is forced "
            "after it" + E(d2));
      c->smu.tables[OD_T].assign((uint8_t *)&w, (uint8_t *)&w + 32); }   // the table P6 left, put back
  }
  // the OD-table route's checks and texts
  struct X { const char *tag; std::function<bool()> f; const char *err; };
  X xs[] = {
    {"GPU minimum below the stock lowest level", [&] { return od.gfxclk(924, N); }, "gfxclk min 924 outside the card's range 925..1900"},
    {"GPU minimum above the maximum's ceiling - 100", [&] { return od.gfxclk(1901, N); }, "gfxclk min 1901 outside the card's range 925..1900"},
    {"GPU minimum above the running maximum - 100", [&] { return od.gfxclk(1801, N); }, "gfxclk min 1801 above the GPU maximum 1900 - 100 MHz"},
    {"GPU minimum above the requested maximum - 100", [&] { return od.gfxclk(1750, 1800); }, "gfxclk min 1750 above the GPU maximum 1800 - 100 MHz"},
    {"GPU maximum above 2000", [&] { return od.gfxclk(N, 2001); }, "gfxclk max 2001 outside the card's range 1800..2000"},
    {"GPU maximum below the stock top level", [&] { return od.gfxclk(N, 1799); }, "gfxclk max 1799 outside the card's range 1800..2000"},
    {"memory minimum", [&] { return od.uclk(500, N); }, "Vega 20 sets the memory clock maximum only"},
    {"memory above 1200", [&] { return od.uclk(N, 1201); }, "uclk max 1201 outside the card's range 1000..1200"},
    {"memory below the stock top level", [&] { return od.uclk(N, 999); }, "uclk max 999 outside the card's range 1000..1200"},
    {"voltage above the stock maximum", [&] { return od.voltage_offset(1); }, "max core voltage 1126 outside the card's range 738..1125"},
    {"voltage below MinVoltageGfx", [&] { return od.voltage_offset(-388); }, "max core voltage 737 outside the card's range 738..1125"},
    {"power +69 % (300.82 W, above 300 W unless the SMU rounds down)", [&] { return od.ppt_pct(69); }, "power limit (%) 69 outside the card's range 0..68"},
    {"power below the stock limit", [&] { return od.ppt_pct(-1); }, "power limit (%) -1 outside the card's range 0..68"},
    {"fan while FAN_CONTROL is off", [&] { return od.fan(OD::FAN_MAX_RPM, 3000); }, "fan control is off: FAN_CONTROL is hardcoded off until the fan is fitted (MI50_FAN_CONTROL)"},
    {"a fan field that is not a native fan control", [&] { return od.fan(2, 0); }, "not available on this card: the V420's fan controls are FanMaximumRpm and FanMinimumPwm"}};
  for (auto &x : xs) { n0 = c->smu_log.size(); bool r = x.f(); check(!r && err_is(d, x.err) && c->smu_log.size() == n0, std::string("R12: ") + x.tag + E(d)); }
  ok = od.gfxclk(925, 2000) && od.uclk(N, 1200) && od.voltage_offset(-387) && od.ppt_pct(68) && od.commit();
  t = smu_od(*c);
  check(ok && t.GfxclkFmin == 925 && t.GfxclkFreq1 == 925 && t.GfxclkFmax == 2000 && t.UclkFmax == 1200 && t.GfxclkVolt1 == 4 * 738 &&
        t.GfxclkVolt3 == 4 * 738 && t.GfxclkFreq3 == 2000 && t.OverDrivePct == 68,
        "R13: the bounds: GPU minimum 925 written (the table had " + std::to_string(boot.GfxclkFmin) + "), GPU 2000, memory 1200, 738 mV, +68 %" + E(d));
  ok = od.voltage_offset(0) && od.commit();
  t = smu_od(*c);
  check(ok && t.GfxclkVolt1 == 4 * 1125 && t.GfxclkVolt2 == 4 * 1125 && t.GfxclkVolt3 == 4 * 1125 && t.GfxclkFreq1 == 925 && t.GfxclkFreq3 == 2000 &&
        t.GfxclkFmax == 2000, "R14: knob at 0 -> the flat cap at 1125 mV + 0 on all three points (Leon 2026-10-16: 1125 mV + knob)" + E(d));
  // the GPU minimum (Leon 2026-10-17): at its top (the maximum 2000 - 100) written at the commit with the GFX levels re-read and read back as
  // the table's; then a maximum under it + 100 refused, nothing sent, the table as it ran
  n0 = c->smu_log.size();
  ok = od.gfxclk(1900, N) && od.commit();
  t = smu_od(*c);
  const auto sent = since(*c, n0);   // the commit's messages, before read_overdrive's table read
  MI50Device::OdView mv;
  bool rd = d.read_overdrive(&mv);
  check(ok && rd && t.GfxclkFmin == 1900 && t.GfxclkFreq1 == 1900 && t.GfxclkFmax == 2000 && mv.gfxclk_mhz[0] == 1900 &&
        same_msgs(sent, cat(cat(od_upload(d), dpm_reads(*c, PPCLK_GFXCLK)), forced(*c, d.perf()))),
        "R14b: GPU minimum 1900 under maximum 2000 -> the table (GfxclkFmin, the curve's first point), the GFX levels re-read, the level forced; "
        "read back 1900" + E(d));
  n0 = c->smu_log.size();
  ok = od.gfxclk(N, 1999);
  check(!ok && err_is(d, "gfxclk min 1900 above the GPU maximum 1999 - 100 MHz") && c->smu_log.size() == n0 && od.commit() && smu_od(*c).GfxclkFmax == 2000,
        "R14c: a maximum 1999 under the running minimum 1900 + 100 -> refused, nothing sent; the next commit uploads the table as it ran" + E(d));
  check(od.gfxclk(925, N) && od.commit() && smu_od(*c).GfxclkFmin == 925, "R14d: the GPU minimum back to 925 for the cases below" + E(d));
  // a refused upload: edits dropped, the object back at the last accepted table, the SMU message named
  OverDriveTable_t before = smu_od(*c);
  c->fail_smu_msg = PPSMC_MSG_TransferTableDram2Smu;
  ok = od.gfxclk(N, 1950) && od.commit();
  char want[400];
  snprintf(want, sizeof(want), "firmware rejected the overdrive table (SMU msg 0x%x param 0x%x -> response 0x%x); edits dropped; going back to the previous "
           "table failed too: SMU msg 0x%x param 0x%x -> response 0x%x", PPSMC_MSG_TransferTableDram2Smu, OD_T, PPSMC_Result_Failed,
           PPSMC_MSG_TransferTableDram2Smu, OD_T, PPSMC_Result_Failed);
  check(!ok && err_is(d, want) && od_eq(*c, before), "R15: every TransferTableDram2Smu refused -> edits dropped, the way back refused too, both named" + E(d));
  c->fail_smu_msg = 0;
  ok = od.commit();
  check(ok && od_eq(*c, before), "R16: the next commit uploads the last accepted table (GPU max 2000, not 1950)" + E(d));
  // a step after the upload refused -> the card goes back to what it ran (egpu_vega20.OverDrive.commit's contract): the last accepted table
  // uploaded again, both DPM tables re-read (review 2026-10-16: the reference left the new table running on its OverDrive path)
  c->fail_smu_nth = c->smu_count + 4;   // upload High, Low, Dram2Smu, here GetDpmFreqByIndex(GFX, 0xFF)
  n0 = c->smu_log.size();
  ok = od.gfxclk(N, 1950) && od.commit();
  c->fail_smu_nth = 0;
  snprintf(want, sizeof(want), "firmware rejected the overdrive table (SMU msg 0x%x param 0x%x -> response 0x%x); edits dropped", PPSMC_MSG_GetDpmFreqByIndex,
           (uint32_t)PPCLK_GFXCLK << 16 | 0xFF, PPSMC_Result_Failed);
  auto after = since(*c, n0);
  auto want_msgs = cat(cat(od_upload(d), {{PPSMC_MSG_GetDpmFreqByIndex, (uint32_t)PPCLK_GFXCLK << 16 | 0xFF}}),
                       cat(cat(cat(od_upload(d), dpm_reads(*c, PPCLK_GFXCLK)), dpm_reads(*c, PPCLK_UCLK)), forced(*c, d.perf())));
  check(!ok && err_is(d, want) && od_eq(*c, before) && d.perf() == MI50Device::PERF_AUTO && same_msgs(after, want_msgs),
        "R16b: the GFX level read refused after the 1950 MHz table was uploaded -> the 2000 MHz table uploaded again, GFX and memory levels re-read, "
        "the requested level forced again on them (F-01)" + E(d));
  // a level read refused half way, and the way back never answered -> the GFX DPM table is the previous one, not half of the new one
  c->dpm_levels[PPCLK_GFXCLK] = {500, 600, 700};
  c->ns_per_call = 1000000;
  c->fail_smu_nth = c->smu_count + 6;   // upload High, Low, Dram2Smu, GetDpmFreqByIndex(GFX, 0xFF), (GFX, 0), here (GFX, 1)
  c->hang_smu_nth = c->smu_count + 7;   // the way back's first message
  ok = od.gfxclk(N, 1960) && od.commit();
  std::string e1 = d.error();
  c->fail_smu_nth = c->hang_smu_nth = 0; c->dpm_levels.erase(PPCLK_GFXCLK);
  n0 = c->smu_log.size();
  bool low = d.set_perf_control(MI50Device::PERF_LOW);   // its first message waits out the unanswered one's response (1 s)
  c->ns_per_call = 1000;
  auto lowmsgs = since(*c, n0);
  check(!ok && e1.find("going back to the previous table failed too: SMU msg") != std::string::npos && low && !lowmsgs.empty() &&
        lowmsgs[0] == std::make_pair((uint32_t)PPSMC_MSG_SetSoftMinByFreq, (uint32_t)(PPCLK_GFXCLK << 16 | 925)),
        "R16c: a GFX level read refused after level 0 was read -> the DPM table kept whole (perf low forces 925, not the half-read 500) [" + e1 + "]");
  ok = od.commit();
  check(ok && od_eq(*c, before) && d.set_perf_control(MI50Device::PERF_AUTO), "R16d: the next commit uploads the last accepted table again" + E(d));
  // a cap read back from the SMU is written back unchanged: 2950 (737.5 mV, the loaded table's MinVoltageGfx) stays 2950
  { OverDriveTable_t q = smu_od(*c); q.GfxclkFreq1 = q.GfxclkFmin; q.GfxclkFreq3 = q.GfxclkFmax; q.GfxclkFreq2 = (uint16_t)((q.GfxclkFmin + q.GfxclkFmax) / 2);
    q.GfxclkVolt1 = q.GfxclkVolt2 = q.GfxclkVolt3 = 2950;
    c->smu.tables[OD_T].assign((uint8_t *)&q, (uint8_t *)&q + 32);
    OD od3(d);
    ok = od3.ok() && od3.gfxclk(N, 1990) && od3.commit();
    OverDriveTable_t g = smu_od(*c);
    check(ok && g.GfxclkVolt1 == 2950 && g.GfxclkVolt2 == 2950 && g.GfxclkVolt3 == 2950 && g.GfxclkFmax == 1990 && g.GfxclkFreq3 == 1990,
          "R16e: a 2950 cap (737.5 mV) read back is written back as 2950, not truncated to 737 mV (2948, below MinVoltageGfx)" + E(d));
    c->smu.tables[OD_T].assign((uint8_t *)&before, (uint8_t *)&before + 32); }
  // the saved boot table (the profile's od_boot_table) is the object's boot table
  { OverDriveTable_t saved = boot; saved.FanMaximumRpm = 2222; saved.MaxOpTemp = 77;
    OD od4(d, &saved, sizeof(saved));
    MI50Device::OdView v4;
    ok = od4.ok() && od4.boot_od(&v4) && v4.max_op_temp == 77 && od4.reset();
    OverDriveTable_t g = smu_od(*c);
    check(ok && memcmp(&g, &saved, 32) == 0, "R16f: a saved boot table given -> Reset uploads it byte for byte" + E(d));
    OverDriveTable_t tuned = before; tuned.FanMaximumRpm = 2500;   // the SMU runs a tune whose fan maximum differs from the boot table's
    c->smu.tables[OD_T].assign((uint8_t *)&tuned, (uint8_t *)&tuned + 32);
    OD od5(d, &saved, sizeof(saved) - 1);
    ok = od5.ok() && od5.reset();
    check(ok && od_eq(*c, boot), "R16g: a saved table of the wrong size is not used; Reset goes to the SMU's table as this driver's boot left it, byte "
          "for byte (fan max " + std::to_string(smu_od(*c).FanMaximumRpm) + " RPM), not to what runs (2500)" + E(d)); }
  // Reset (Leon 2026-10-16: "Reset goes to the boot table")
  n0 = c->smu_log.size();
  ok = od.reset();
  t = smu_od(*c);
  check(ok && memcmp(&t, &boot, 32) == 0 && d.read_overdrive(&v) && v.ppt_pct == 0 && !v.has_voltage_offset,
        "R17: reset -> the boot table byte for byte: its clocks, its curve (no cap), OverDrivePct 0" + E(d));

  // ================= FN: the fan control (Leon 2026-10-17): the switch (this build: MI50_FAN_CONTROL), the V420's fan target, the native fan
  // fields in the fitted fan's 450..4300 RPM =================
  { std::unique_ptr<Card> cn = card(true);   // nothing refused: FAN_CONTROL runs exactly when the switch offers it
    Dev dn(*cn);
    check(dn.init() == MI50Device::FULL_BOOT, "FN0: booted on the V420 table" + E(dn));
    PPTable_t up; memcpy(&up, cn->smu.tables[TABLE_PPTABLE].data(), sizeof(up));
    const bool bit = up.FeaturesToRun[0] >> FEATURE_FAN_CONTROL_BIT & 1, allowed = cn->smu.allowed[0] >> FEATURE_FAN_CONTROL_BIT & 1,
               runs = cn->smu.enabled >> FEATURE_FAN_CONTROL_BIT & 1;
    const bool tgt = std::find(cn->smu_log.begin(), cn->smu_log.end(), std::make_pair((uint32_t)PPSMC_MSG_SetFanTemperatureTarget, 95u)) != cn->smu_log.end();
    check(bit == (bool)MI50_FAN_CONTROL && allowed == (bool)MI50_FAN_CONTROL && runs == (bool)MI50_FAN_CONTROL && up.FanTargetTemperature == 95 && tgt,
          std::string("FN1: switch ") + (MI50_FAN_CONTROL ? "on" : "off") + " -> FEATURE_FAN_CONTROL in the uploaded table, the allowed mask and running "
          "alike; the V420's fan target 95 C uploaded and sent, no override");
    uint32_t cl = 0; MI50Device::Limits ln;
    bool k = dn.controls(&cl) && dn.read_limits(&ln);
    const int64_t rng[2][2] = {{450, 4300}, {10, 86}};
    check(k && (bool)(cl & MI50Device::CTL_FAN) == (bool)MI50_FAN_CONTROL && memcmp(ln.fan, rng, sizeof(rng)) == 0,
          "FN2: CTL_FAN exactly while FAN_CONTROL runs; FanMaximumRpm 450..4300 RPM (the V420's 400..4950 inside the fan's), FanMinimumPwm 10..86 % "
          "(of FanMaximumRpm 4950: 495..4257 RPM)" + E(dn));
    OD on(dn);
    size_t q0 = cn->smu_log.size();
#if MI50_FAN_CONTROL
    const OverDriveTable_t gb = smu_od(*cn);
    k = on.ok() && on.fan(OD::FAN_MAX_RPM, 3000) && on.fan(OD::FAN_MIN_PWM, 20) && cn->smu_log.size() == q0 && on.commit();
    OverDriveTable_t g = smu_od(*cn), gw = gb; MI50Device::OdView vn;
    gw.FanMaximumRpm = 3000; gw.FanMinimumPwm = 20;
    check(k && memcmp(&g, &gw, 32) == 0 && dn.read_overdrive(&vn) && vn.fan_max_rpm == 3000 && vn.fan_min_pwm == 20,
          "FN3: the native fan fields -> the OverDrive table at the commit (3000 RPM, 20 %), every other field as it ran, read_overdrive reports them" + E(dn));
    X fx[] = {
      {"fan maximum 449 RPM", [&] { return on.fan(OD::FAN_MAX_RPM, 449); }, "FanMaximumRpm 449 outside the card's range 450..4300"},
      {"fan maximum 4301 RPM", [&] { return on.fan(OD::FAN_MAX_RPM, 4301); }, "FanMaximumRpm 4301 outside the card's range 450..4300"},
      {"PWM minimum 9 % = 445.5 RPM", [&] { return on.fan(OD::FAN_MIN_PWM, 9); }, "FanMinimumPwm 9 outside the card's range 10..86"},
      {"PWM minimum 87 % = 4306.5 RPM", [&] { return on.fan(OD::FAN_MIN_PWM, 87); }, "FanMinimumPwm 87 outside the card's range 10..86"},
      {"the retired fan target field", [&] { return on.fan(2, 80); }, "not available on this card: the V420's fan controls are FanMaximumRpm and FanMinimumPwm"},
      {"the retired zero RPM field", [&] { return on.fan(3, 1); }, "not available on this card: the V420's fan controls are FanMaximumRpm and FanMinimumPwm"}};
    for (auto &x : fx) { size_t n1 = cn->smu_log.size(); bool r = x.f(); check(!r && err_is(dn, x.err) && cn->smu_log.size() == n1, std::string("FN4: ") + x.tag + E(dn)); }
#else
    bool any = false;
    for (int w = 0; w < 2; w++) { any |= on.fan(w, 1); any |= !err_is(dn, "fan control is off: FAN_CONTROL is hardcoded off until the fan is fitted (MI50_FAN_CONTROL)"); }
    check(on.ok() && !any && cn->smu_log.size() == q0, "FN3: switch off -> both native fan fields refused with the reason, nothing sent" + E(dn));
#endif
  }


  // ================= F: F-01 (the requested DPM level forced again after every OverDrive commit) and F-02 (an SMU failure kept for the session's end)
  { std::unique_ptr<Card> cf = card(true, 1ull << FEATURE_FAN_CONTROL_BIT);
    Dev df(*cf);
    check(df.init() == MI50Device::FULL_BOOT && df.perf() == MI50Device::PERF_AUTO && !df.smu_failed(), "F0: booted; requested level auto, no SMU failure");
    OD of(df);
    check(of.ok() && df.set_perf_control(MI50Device::PERF_HIGH) && df.perf() == MI50Device::PERF_HIGH, "F1a: perf high requested" + E(df));
    auto lv = cf->levels(PPCLK_GFXCLK); lv.push_back(1950);   // the SMU moves its top GFX level to the new maximum (Linux re-reads it, the driver too)
    cf->dpm_levels[PPCLK_GFXCLK] = lv;
    size_t f0 = cf->smu_log.size();
    bool ok1 = of.gfxclk(N, 1950) && of.commit();
    check(ok1 && same_msgs(since(*cf, f0), cat(cat(od_upload(df), dpm_reads(*cf, PPCLK_GFXCLK)), forced(*cf, MI50Device::PERF_HIGH))) &&
          since(*cf, f0).back() == std::make_pair((uint32_t)PPSMC_MSG_SetSoftMaxByFreq, (uint32_t)(PPCLK_SOCCLK << 16 | cf->levels(PPCLK_SOCCLK).back())) &&
          forced(*cf, MI50Device::PERF_HIGH)[0].second == (uint32_t)(PPCLK_GFXCLK << 16 | 1950),
          "F1: perf high, GPU max raised to 1950 -> the table, the GFX levels re-read, then soft min / max forced to the new top level 1950 (F-01)" + E(df));
    check(df.set_perf_control(MI50Device::PERF_AUTO), "F2a: perf auto" + E(df));
    f0 = cf->smu_log.size();
    bool ok2 = of.ppt_pct(5) && of.commit();
    check(ok2 && same_msgs(since(*cf, f0), cat(od_upload(df), forced(*cf, MI50Device::PERF_AUTO))),
          "F2: auto, a power-only edit -> the table, then lowest..top forced (Linux readjusts after every commit; no level re-read without a clock edit)" + E(df));
    OverDriveTable_t runs = smu_od(*cf);
    cf->fail_smu_nth = cf->smu_count + 3 + (int)dpm_reads(*cf, PPCLK_UCLK).size() + 3 + 1;   // upload, the memory levels, 3 soft min, here the 1st soft max
    f0 = cf->smu_log.size();
    bool ok3 = of.uclk(N, 1100) && of.commit();
    cf->fail_smu_nth = 0;
    char w3[300];
    snprintf(w3, sizeof(w3), "firmware rejected the overdrive table (SMU msg 0x%x param 0x%x -> response 0x%x); edits dropped", PPSMC_MSG_SetSoftMaxByFreq,
             (uint32_t)(PPCLK_GFXCLK << 16 | cf->levels(PPCLK_GFXCLK).back()), PPSMC_Result_Failed);
    auto f3 = forced(*cf, MI50Device::PERF_AUTO);
    auto want3 = cat(cat(cat(od_upload(df), dpm_reads(*cf, PPCLK_UCLK)), {f3[0], f3[1], f3[2], f3[3]}),
                     cat(cat(cat(od_upload(df), dpm_reads(*cf, PPCLK_GFXCLK)), dpm_reads(*cf, PPCLK_UCLK)), f3));
    check(!ok3 && err_is(df, w3) && od_eq(*cf, runs) && same_msgs(since(*cf, f0), want3),
          "F3: the forcing refused after the new table was uploaded -> the table that ran uploaded again, both levels re-read, the level forced again" + E(df));
    check(df.smu_failed(), "F4a (F-02): the refused SMU message is kept (smu_failed)");
    cf->regs[GC_SCRATCH_REG6] = 0;   // tinygrad's fini: REG6 = is_err_state (False)
    df.mark_smu_failure();
    check(cf->regs[GC_SCRATCH_REG6] == 1, "F4 (F-02): mark_smu_failure -> SCRATCH_REG6 = 1 again after the session's fini wrote 0");
    // R2-03: a driver that stops (or a power-off since the operation began) ends the operation at once, with no register access reaching the card
    bool stop = false; uint32_t gen = 0;
    df.watch(&stop, &gen);
    MI50Device::Metrics m1 = {};
    bool before = df.read_metrics(&m1);
    stop = true;
    size_t s0 = cf->smu_log.size(); uint64_t clk0 = cf->clock;
    MI50Device::Metrics m2 = {};
    bool after = df.read_metrics(&m2);
    std::string why = df.error();
    check(before && !after && cf->smu_log.size() == s0 && cf->clock - clk0 < 1000000000ull && why.find("powered down or its driver stops") != std::string::npos,
          "W1 (R2-03): the driver stops -> metrics refused at once (model clock +" + std::to_string((cf->clock - clk0) / 1000000) + " ms), no SMU message sent: " + why);
    stop = false; gen = 1;   // a power-off since the last arm
    df.arm();
    MI50Device::Metrics m3 = {};
    bool rearmed = df.read_metrics(&m3);
    check(rearmed, "W2 (R2-03): arm() after the power-off's count -> served again" + E(df));
    df.watch(nullptr, nullptr);
  }

  printf("\n%d ok, %d FAIL\n", nok, nfail);
  return nfail ? 1 : 0;
}
