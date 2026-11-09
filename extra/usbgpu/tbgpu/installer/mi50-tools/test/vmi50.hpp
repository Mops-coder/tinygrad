// vmi50.hpp - a virtual AMD Instinct MI50 (Vega 20, PCI 1002:66a1) for testing the TinyGPU driver's MI50 bring-up offline.
// A C++ port of the reference's virtual card (scratchpad vcard/vmi50.py, the judge of tinygrad's MI50 driver): every block answers what the
// Linux code driving it waits for. Register addresses: the driver's mi50::reg (generated from the Linux headers + vega20_ip_offset.h), so
// include this after TinyGPUDriver.cpp; every other definition is Linux's own header (the test build puts Linux d6d3455's amdgpu headers on
// the include path). Firmware: the card cuts Linux's PSP payloads out of the raw linux-firmware files itself (vmi50.linux_fw's offsets),
// independently of the driver's generated mi50::fw.
// Power-on values measured on the real card 2026-10-13: RCC_CONFIG_MEMSIZE 0x7ff0, BIOS_SCRATCH_7 0, MP1 C2PMSG_90 0xffffffff (no PMFW),
// MP0 C2PMSG_35 0x80000000, C2PMSG_81 0, FB_LOCATION 0x8000..0x87ff. Time is simulated: every clock read advances 1 us.
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#include "atom-types.h"
#include "atomfirmware.h"
#include "smu11_driver_if.h"
#include "vega20_pptable.h"
#include "vega20_ppsmc.h"
#include "psp_gfx_if.h"

namespace vm {
using namespace mi50::reg;

static inline std::string slurp(const std::string &p) {
  std::ifstream f(p, std::ios::binary);
  if (!f) { fprintf(stderr, "vmi50: cannot read %s\n", p.c_str()); exit(2); }
  std::stringstream s; s << f.rdbuf(); return s.str();
}
static inline uint32_t u16(const std::string &b, size_t o) { return (uint32_t)(uint8_t)b[o] | (uint32_t)(uint8_t)b[o + 1] << 8; }
static inline uint32_t u32(const std::string &b, size_t o) { return u16(b, o) | u16(b, o + 2) << 16; }

// ---- firmware as Linux hands it to the PSP (vmi50.linux_fw): common header ucode_size @20 / array offset @24; psp v1.0 sos desc offset @36,
// size @40; gfx v1.0 jt_offset @36 / jt_size @40; rlc v2.1 restore lists @116/120, @132/136, @148/152 when sized; ta v1.0 ras @48/@52 ----
struct LinuxFw {
  std::string sysdrv, sos, asd, ras;
  std::vector<std::pair<uint32_t, std::string>> ip;
  uint32_t smc_version = 0;
};
static inline LinuxFw linux_fw(const std::string &dir) {
  auto rd = [&](const char *n) { return slurp(dir + "/" + n); };
  auto gen = [](const std::string &b) { return b.substr(u32(b, 24), u32(b, 20)); };
  LinuxFw f;
  std::string s = rd("vega20_sos.bin"); uint32_t a = u32(s, 24);
  f.sysdrv = s.substr(a, u32(s, 36)); f.sos = s.substr(a + u32(s, 36), u32(s, 40));
  f.ip.push_back({GFX_FW_TYPE_SDMA0, gen(rd("vega20_sdma.bin"))}); f.ip.push_back({GFX_FW_TYPE_SDMA1, gen(rd("vega20_sdma1.bin"))});
  f.ip.push_back({GFX_FW_TYPE_CP_CE, gen(rd("vega20_ce.bin"))}); f.ip.push_back({GFX_FW_TYPE_CP_PFP, gen(rd("vega20_pfp.bin"))});
  f.ip.push_back({GFX_FW_TYPE_CP_ME, gen(rd("vega20_me.bin"))});
  for (auto [n, jt] : {std::pair<const char *, uint32_t>{"vega20_mec.bin", GFX_FW_TYPE_CP_MEC_ME1}, {"vega20_mec2.bin", GFX_FW_TYPE_CP_MEC_ME2}}) {
    std::string b = rd(n); uint32_t o = u32(b, 24), sz = u32(b, 20), jo = u32(b, 36), js = u32(b, 40);
    f.ip.push_back({GFX_FW_TYPE_CP_MEC, b.substr(o, sz - js * 4)}); f.ip.push_back({jt, b.substr(o + jo * 4, js * 4)});
  }
  std::string r = rd("vega20_rlc.bin");
  if (u16(r, 10) >= 1) {
    const uint32_t L[3][3] = {{116, 120, GFX_FW_TYPE_RLC_RESTORE_LIST_SRM_CNTL}, {132, 136, GFX_FW_TYPE_RLC_RESTORE_LIST_GPM_MEM},
                              {148, 152, GFX_FW_TYPE_RLC_RESTORE_LIST_SRM_MEM}};
    for (auto &l : L) if (u32(r, l[0])) f.ip.push_back({l[2], r.substr(u32(r, l[1]), u32(r, l[0]))});
  }
  f.ip.push_back({GFX_FW_TYPE_RLC_G, gen(r)});
  std::string smc = rd("vega20_smc.bin");
  f.ip.push_back({GFX_FW_TYPE_SMU, gen(smc)});
  f.smc_version = u32(smc, 16);
  f.asd = gen(rd("vega20_asd.bin"));
  std::string t = rd("vega20_ta.bin");
  f.ras = t.substr(u32(t, 24) + u32(t, 48), u32(t, 52));
  return f;
}

// ---- synthetic VBIOS (vmi50.build_vbios; no MI50 dump exists, every number is a TEST value) ----
constexpr uint32_t HDR = 0x100, MDT = 0x200, FWI = 0x300, GFX = 0x400, VRU = 0x500, PPT = 0x600, SDI = 0x1000, UMC = 0x1180, VRI = 0x1200, CMT = 0x1400,
  ASIC = 0x1500, CLK = 0x1600, PN = 0x80;
constexpr uint32_t MARK = NBIO_BIOS_SCRATCH_14;   // the synthetic ASIC_Init copies PS[0] here
constexpr uint32_t CLK_MARK = NBIO_BIOS_SCRATCH_0;   // the synthetic getsmuclockinfo copies its input PS[0] (syspll | clk id << 8) here
// OD8_SETTING order (vega20_hwmgr.h:401-418) -> (min, max)
static const uint32_t OD_LIMITS[14][2] = {{800, 1000}, {1200, 2000}, {800, 1000}, {750, 1100}, {1000, 1500}, {750, 1100}, {1200, 2000}, {750, 1150},
  {700, 1200}, {1, 50}, {500, 3850}, {800, 3850}, {25, 105}, {80, 105}};
struct Umc { uint8_t frev, crev, c0; uint32_t c1; };
struct Vbios { std::vector<uint8_t> img; PPTable_t exp; };
static inline Vbios build_vbios(uint32_t fw_cap = 0, const Umc *umc = nullptr, const char *pn = "113-D1631700-111", uint32_t sclk10k = 172500,
                                uint32_t mclk10k = 100000) {
  Vbios v; v.img.assign(0x10000, 0);
  uint8_t *img = v.img.data();
  auto w16 = [&](uint32_t o, uint32_t x) { img[o] = x & 0xff; img[o + 1] = (x >> 8) & 0xff; };
  auto w32 = [&](uint32_t o, uint32_t x) { w16(o, x & 0xffff); w16(o + 2, x >> 16); };
  img[0] = 0x55; img[1] = 0xaa; img[2] = (uint8_t)(v.img.size() >> 9); memcpy(img + 0x30, " 761295520", 10); w16(0x48, HDR);
  memcpy(img + PN, pn, strlen(pn));
  struct atom_rom_header_v2_2 rh; memset(&rh, 0, sizeof(rh)); memcpy(rh.atom_bios_string, "ATOM", 4); rh.masterdatatable_offset = MDT;
  memcpy(img + HDR, &rh, sizeof(rh)); w16(HDR + 0x1E, CMT);
  struct atom_master_data_table_v2_1 mdt; memset(&mdt, 0, sizeof(mdt));
  mdt.table_header.structuresize = sizeof(mdt); mdt.table_header.format_revision = 2;
  auto &L = mdt.listOfdatatables;
  L.firmwareinfo = FWI; L.gfx_info = GFX; L.vram_usagebyfirmware = VRU; L.powerplayinfo = PPT; L.smc_dpm_info = SDI; L.vram_info = VRI;
  if (umc) L.umc_info = UMC;
  memcpy(img + MDT, &mdt, sizeof(mdt));
  struct atom_firmware_info_v3_2 f; memset(&f, 0, sizeof(f));
  f.table_header.structuresize = 72; f.table_header.format_revision = 3; f.table_header.content_revision = 2;
  f.bios_scratch_reg_startaddr = NBIO_BIOS_SCRATCH_0; f.bootup_sclk_in10khz = sclk10k; f.bootup_mclk_in10khz = mclk10k; f.bootup_vddc_mv = 950;
  f.firmware_capability = fw_cap;
  memcpy(img + FWI, &f, 72);
  struct atom_gfx_info_v2_4 g; memset(&g, 0, sizeof(g));
  g.table_header.structuresize = 72; g.table_header.format_revision = 2; g.table_header.content_revision = 4;
  g.max_shader_engines = 4; g.max_cu_per_sh = 16; g.max_sh_per_se = 1; g.max_backends_per_se = 4; g.max_texture_channel_caches = 16;
  g.gc_num_gprs = 256; g.gc_num_max_gs_thds = 32; g.gc_gs_table_depth = 32; g.gc_gsprim_buff_depth = 1792; g.gc_double_offchip_lds_buffer = 1;
  g.gc_wave_size = 64; g.gc_max_waves_per_simd = 10; g.gc_max_scratch_slots_per_cu = 32; g.gc_lds_size = 64;
  memcpy(img + GFX, &g, 72);
  struct vram_usagebyfirmware_v2_1 u; memset(&u, 0, sizeof(u));
  u.table_header.structuresize = 12; u.table_header.format_revision = 2; u.table_header.content_revision = 1;
  u.start_address_in_kb = (0x7ff0u << 10) - (8u << 10); u.used_by_firmware_in_kb = 8 << 10; u.used_by_driver_in_kb = 256;
  memcpy(img + VRU, &u, 12);
  ATOM_Vega20_POWERPLAYTABLE pp; memset(&pp, 0, sizeof(pp));
  pp.sHeader.structuresize = sizeof(pp); pp.sHeader.format_revision = 11;
  pp.ucThermalControllerType = 1; pp.ulPlatformCaps = 0x5; pp.usSoftwareShutdownTemp = 118;
  auto &od = pp.OverDrive8Table; od.ucODTableRevision = 1; od.ODFeatureCount = 10; od.ODSettingCount = 14;
  for (int i = 0; i < 10; i++) od.ODFeatureCapabilities[i] = 1;
  for (int i = 0; i < 14; i++) { od.ODSettingsMin[i] = OD_LIMITS[i][0]; od.ODSettingsMax[i] = OD_LIMITS[i][1]; }
  PPTable_t &t = pp.smcPPTable; t.Version = 3; t.TedgeLimit = 90; t.ThotspotLimit = 105; t.ThbmLimit = 95;
  t.PcieGenSpeed[0] = 0; t.PcieGenSpeed[1] = 3; t.PcieLaneCount[0] = 6; t.PcieLaneCount[1] = 6;
  t.MinVoltageGfx = 4 * 700; t.MaxVoltageGfx = 4 * 1200; t.FanMaximumRpm = 3850; t.FanPwmMin = 20; t.FanTargetTemperature = 85;
  memcpy(img + PPT, &pp, sizeof(pp));
  struct atom_smc_dpm_info_v4_4 d; memset(&d, 0, sizeof(d));
  d.table_header.structuresize = 308; d.table_header.format_revision = 4; d.table_header.content_revision = 4;
  d.maxvoltagestepgfx = 4; d.gfxmaxcurrent = 300; d.uclkspreadenabled = 1; d.fllgfxclkspreadfreq = 777;
  d.i2ccontrollers[0].enabled = 1; d.i2ccontrollers[0].slaveaddress = 0x20; d.i2ccontrollers[6].i2cspeed = 3;
  memcpy(img + SDI, &d, 308);
  if (umc) { w16(UMC, 64); img[UMC + 2] = umc->frev; img[UMC + 3] = umc->crev; img[UMC + 27] = umc->c0; w32(UMC + 52, umc->c1); }
  w16(VRI, 0x60); img[VRI + 2] = 2; img[VRI + 3] = 3; img[VRI + 20] = 1; w16(VRI + 24 + 20, 0x30); img[VRI + 24 + 23] = 0x60;   // vram_info v2.3, HBM2
  w16(CMT + 4, ASIC); w16(CMT + 4 + 2 * 41, CLK);   // command tables 0 = ASIC_Init, 41 = getsmuclockinfo
  std::vector<uint8_t> code = {60, 0x05, (uint8_t)(CLK_MARK & 0xff), (uint8_t)(CLK_MARK >> 8), 0xAD, 0xDE, 0, 0,     // COMPARE REG[CLK_MARK], 0xDEAD
                               68, 0x00, 0xF0,                                                                       // JUMP_EQUAL off the image
                               1, 0x01, (uint8_t)(MARK & 0xff), (uint8_t)(MARK >> 8), 0};                            // REG[MARK] = PS[0]
  const uint32_t s7 = NBIO_BIOS_SCRATCH_7;
  for (uint8_t b : {(uint8_t)13, (uint8_t)0x05, (uint8_t)(s7 & 0xff), (uint8_t)(s7 >> 8), (uint8_t)0x00, (uint8_t)0x02, (uint8_t)0, (uint8_t)0})
    code.push_back(b);                                                                                                // SCRATCH_7 |= 0x200
  for (uint8_t b : {(uint8_t)81, (uint8_t)77, (uint8_t)80, (uint8_t)3, (uint8_t)83}) code.push_back(b);          // DELAY_US 77, DELAY_MS 3, REPEAT (logs)
  code.push_back(91);
  w16(ASIC, (uint32_t)(6 + code.size())); img[ASIC + 2] = 1; img[ASIC + 3] = 1; memcpy(img + ASIC + 6, code.data(), code.size());
  std::vector<uint8_t> clk = {1, 0x01, (uint8_t)(CLK_MARK & 0xff), (uint8_t)(CLK_MARK >> 8), 0,                     // REG[CLK_MARK] = PS[0]
                              2, 0x05, 0x00, 0x00, 0xe1, 0xf5, 0x05, 91};                                          // PS[0] = 100 MHz in Hz
  w16(CLK, (uint32_t)(6 + clk.size())); img[CLK + 2] = 1; img[CLK + 3] = 1; memcpy(img + CLK + 6, clk.data(), clk.size());
  // what the driver uploads: smcPPTable + append_vbios_pptable, the table's own fan target (Leon 2026-10-17: no override; the boot cap is applied by the test)
  v.exp = t;
  v.exp.MaxVoltageStepGfx = 4; v.exp.GfxMaxCurrent = 300; v.exp.UclkSpreadEnabled = 0; v.exp.FllGfxclkSpreadFreq = 777;
  v.exp.I2cControllers[0].Enabled = 1; v.exp.I2cControllers[0].SlaveAddress = 0x20; v.exp.I2cControllers[6].I2cSpeed = 3;
  return v;
}

// ---- the card ----
constexpr uint32_t CFG_PCIE = 0x64, CFG_REBAR = 0x200;
struct SmuState { uint32_t allowed[2] = {}, drv[2] = {}, tools[2] = {}, ppt = 300; uint64_t enabled = 0; bool shutdown = false;
                  std::map<uint32_t, std::vector<uint8_t>> tables; };
struct PspEv { std::string kind; uint64_t a = 0, b = 0, c = 0, d = 0; bool ok = false; std::vector<uint8_t> flags; };

struct Card {
  // options (vmi50.VirtualMI50.__init__)
  std::vector<uint8_t> img; LinuxFw fw;
  uint32_t memsize_mb = 0x7ff0, fb0 = 0x8000, fb1 = 0x87ff; uint16_t cmd0 = 0x0006; int rebar_bars = 1; uint64_t reset_window_ns = 200000000ull;
  uint16_t lnkctl0 = 0x0002; int post_resets_bar = 0;   /* POST: 1 REBAR + BAR0, 2 BAR0 upper dword only, 3 REBAR only */ uint32_t boot_cfg = 0, rebar_sizes = 0x000FF000; uint64_t bar0_size = 32ull << 30;
  bool have_partner = false; uint32_t partner_cap = 0, partner_cap2 = 0;
  uint32_t fan_rpm = 0;             // what GetCurrentRpm reports (no fan on the MI50: 0)
  std::vector<uint32_t> thm_seq;    // CG_MULT_THERMAL_STATUS reads in order, the last one repeats (the thermal watchdog, Leon 2026-10-17)
  size_t thm_at = 0;
  uint32_t fail_smu_msg = 0;        // a message the SMU answers with PPSMC_Result_Failed (failure injection)
  uint32_t fail_ip_type = 0xffff;   // an IP firmware type the PSP refuses (failure injection)
  // variants and failure injection (t_mi50 K-W); a count of 0 = off, else the n-th (1-based) of that kind
  uint32_t mmio_size_dwords = (512u << 10) / 4;   // BAR5; the driver reaches registers past it through PCIE_INDEX2/DATA2 (SMN address reg * 4)
  uint64_t smu_refuse = 0;                        // features EnableAllSmuFeatures leaves off
  std::map<uint32_t, std::vector<uint32_t>> dpm_levels;   // per PPCLK: the levels the SMU reports instead of its own
  uint32_t hang_smu_msg = 0;                      // a message the SMU never answers
  int fail_alloc_nth = 0, fail_smu_nth = 0, hang_smu_nth = 0, fail_psp_nth = 0, hang_psp_nth = 0;
  int psp_stall = 0;                  // 2: the sOS never starts, 5: the bootloader not ready again after SYS_DRV, 3: the KM ring is never created, 4: the sOS starts but never reports ready
  bool sysdrv_starts_sos = false;     // the bootloader brings the sOS up already with SYS_DRV
  bool smu_no_start = false;          // the SMU firmware load does not start the PMFW
  bool shutdown_drops_psp = false;    // PrepareMp1ForShutdown leaves the PSP without its response flag
  uint32_t memsize_after_reset = 0;   // non-zero: RCC_CONFIG_MEMSIZE after a mode1 reset
  bool post_fails_after_reset = false;   // ASIC_Init after a mode1 reset runs off the image (the table's own test of BIOS_SCRATCH_0)
  bool bl_dead_after_reset = false;   // the PSP bootloader never reports ready after a mode1 reset
  uint32_t rom_glitch = 0;           // non-zero: the second pass over the ROM reads this as its first dword (a link glitch)
  uint32_t ras_init_status = 0;       // what the RAS TA reports in its shared memory
  int bar0_moves = 0, bar0_hi_moves = 0;   // BAR0 low / high dword reads differently on this many reads first
  uint64_t ns_per_call = 1000;        // the clock's step per now_ns() (sweeps use 1 ms so 10 s timeouts stay cheap)
  // observations
  std::vector<std::string> events, timeline, logs; std::vector<PspEv> psp_log; std::vector<std::pair<uint32_t, uint32_t>> smu_log;
  std::vector<uint32_t> clk_calls, us_delays, ms_sleeps; int allocs = 0, smu_count = 0, psp_count = 0, indirect = 0; long outstanding = 0; std::map<void *, size_t> live;
  struct CfgW { uint32_t off, val; unsigned size; }; std::vector<CfgW> cfg_writes; std::set<uint32_t> touched; size_t reset_cfg_index = 0;
  int mode1_count = 0, posted = 0; uint32_t last_mark = 0;
  // GFX (AM_GFX.init_hw): SH_MEM_CONFIG / SH_MEM_BASES banked per VMID by GRBM_GFX_CNTL (the reference selects a VMID, then writes them),
  // CP_STAT as the test sets it (0 = idle), and every register write in order
  uint32_t sh_mem_config[16] = {}, sh_mem_bases[16] = {}, grbm_vmid = 0, cp_stat = 0; std::vector<uint32_t> wr_order;
  // bug 5: the CU fuses of each shader array (SE-major; CC_GC_SHADER_ARRAY_CONFIG INACTIVE_CUS, read for the array GRBM_GFX_INDEX selects).
  // TEST pattern: CU 15 off in each of the 4 arrays, 64 - 4 = the MI50's 60; the real card's pattern is not known
  uint32_t inactive_cus[4] = {0x8000, 0x8000, 0x8000, 0x8000};
  bool grbm_selected = false;   // GRBM_GFX_INDEX written since power-on: a CU read before that relies on a power-on value this model does not know
  // state
  std::vector<uint8_t> cfg; std::unordered_map<uint32_t, uint32_t> regs, smn; std::map<uint64_t, std::vector<uint8_t>> vram;
  uint32_t rom_ptr = 0; int rom_pass = 0; bool psp_sysdrv = false, psp_sos = false, ring_set = false; uint64_t ring = 0; uint32_t ring_size = 0, psp_rptr = 0;
  bool smu_alive = false, smu_hung = false; SmuState smu; uint32_t boot_cfg_next = 0;
  uint64_t clock = 0, down_until = 0; bool pending_reset = false;

  Card(const std::vector<uint8_t> &image, const LinuxFw &f) : img(image), fw(f) {}
  void start() { power_on(true); }   // after the options are set

  // ---- time / memory / logging: what IODelay, IOSleep, clock_gettime_nsec_np, IOMallocZero / IOFree and the bring-up's os_log lines reach
  // through the DriverKit stand-ins (test/dk_card.hpp) ----
  uint64_t now_ns() { return clock += ns_per_call; }
  void delay_us(uint32_t us) { us_delays.push_back(us); clock += (uint64_t)us * 1000; }
  void sleep_ms(uint32_t ms) { ms_sleeps.push_back(ms); clock += (uint64_t)ms * 1000000; }
  void log(const char *m) { logs.push_back(m); }
  void *alloc(size_t n) {   // IOMallocZero: the driver must hand back the same size to IOFree
    if (++allocs == fail_alloc_nth) return nullptr;
    void *p = calloc(1, n ? n : 1); live[p] = n; outstanding = (long)live.size(); return p;
  }
  void release(void *p, size_t n) {
    auto it = live.find(p);
    if (it == live.end()) ev("release_unknown_pointer"); else { if (it->second != n) ev("release_size_mismatch"); live.erase(it); }
    outstanding = (long)live.size(); free(p);
  }
  // the PowerPlay table a test hands MI50Device (the driver hands it the V420's): pp_source when set, else the card image's own powerplayinfo
  // (ROM header pointer at 0x48, master data table at +0x20, entries at +4), structuresize long
  std::vector<uint8_t> pp_source; bool pp_none = false;   // pp_none: no table at all
  const uint8_t *powerplay_table(uint32_t *size) {
    if (pp_none) { *size = 0; return nullptr; }
    if (!pp_source.empty()) { *size = (uint32_t)pp_source.size(); return pp_source.data(); }
    auto r16 = [&](uint32_t o) { return o + 2 <= img.size() ? (uint32_t)img[o] | (uint32_t)img[o + 1] << 8 : 0u; };
    uint32_t hdr = r16(0x48), mdt = r16(hdr + 0x20), ppt = r16(mdt + 4 + (uint32_t)offsetof(struct atom_master_list_of_data_tables_v2_1, powerplayinfo));
    if (!ppt || ppt + 2 > img.size()) { *size = 0; return nullptr; }
    uint32_t n = r16(ppt);
    *size = ppt + n <= img.size() ? n : (uint32_t)img.size() - ppt;
    return img.data() + ppt;
  }
  uint32_t mmio_dwords() { return mmio_size_dwords; }

  void ev(const std::string &e) { events.push_back(e); }
  void power_on(bool cold) {
    cfg = cfg_defaults(); regs.clear(); smn.clear(); grbm_selected = false;
    regs[NBIO_RCC_CONFIG_MEMSIZE] = !cold && memsize_after_reset ? memsize_after_reset : memsize_mb;
    regs[MMHUB_MC_VM_FB_LOCATION_BASE] = fb0; regs[MMHUB_MC_VM_FB_LOCATION_TOP] = fb1;
    regs[MP0_MP0_SMN_C2PMSG_35] = !cold && bl_dead_after_reset ? 0 : 0x80000000;
    rom_ptr = 0; psp_sysdrv = psp_sos = ring_set = false; psp_rptr = 0;
    smu_alive = smu_hung = false; smu = SmuState();
    if (!cold) { ev("power_on_after_mode1"); reset_cfg_index = cfg_writes.size(); if (post_fails_after_reset) regs[CLK_MARK] = 0xDEAD; }
  }
  // ---- config space (vmi50._cfg_defaults / cfg_write / _cfg_dword) ----
  std::vector<uint8_t> cfg_defaults() {
    std::vector<uint8_t> c(4096, 0);
    auto w = [&](uint32_t o, uint32_t x) { memcpy(&c[o], &x, 4); };
    w(0x00, 0x66a11002); w(0x04, 0x0010u << 16 | cmd0); w(0x08, 0x03800000);
    w(0x10, 0xC); w(0x18, 0xC); w(0x24, 0x0); w(0x34, 0x50); w(0x3c, 0x100);
    w(0x50, 0x0003u << 16 | CFG_PCIE << 8 | 0x01);
    w(CFG_PCIE, 0x0002u << 16 | 0xA0 << 8 | 0x10);
    w(CFG_PCIE + 0x10, lnkctl0);
    w(CFG_PCIE + 0x08, 0x2810); w(CFG_PCIE + 0x30, 0x0003);
    w(0xA0, 0x05);
    w(0x100, 0x150u << 20 | 1u << 16 | 0x000b);
    w(0x150, (rebar_bars ? CFG_REBAR : 0x270u) << 20 | 2u << 16 | 0x0001);
    w(0x150 + 0x0C, 0x00462030); w(0x150 + 0x14, 0x00002000);
    if (rebar_bars) { w(CFG_REBAR, 0x270u << 20 | 1u << 16 | 0x0015); w(CFG_REBAR + 4, rebar_sizes); w(CFG_REBAR + 8, 15u << 8 | (uint32_t)rebar_bars << 5 | 0); }
    if (rebar_bars == 2) { w(CFG_REBAR + 0xC, 0x1FE0); w(CFG_REBAR + 0x10, 1u << 8 | 2); }
    w(0x270, 0x320u << 20 | 1u << 16 | 0x0019);
    w(0x320, 0x0018 | 1u << 16);
    return c;
  }
  uint32_t dw(uint32_t o) { uint32_t x; memcpy(&x, &cfg[o], 4); return x; }
  void put(uint32_t o, uint32_t x) { memcpy(&cfg[o], &x, 4); }
  uint64_t rebar_size(int bar) {
    for (int i = 0; i < rebar_bars; i++) { uint32_t ctrl = dw(CFG_REBAR + 8 + 8 * i); if ((int)(ctrl & 7) == bar) return 1ull << (20 + ((ctrl >> 8) & 0x3f)); }
    return bar == 0 ? bar0_size : (2ull << 20);
  }
  uint32_t bar_mask(uint32_t base, uint32_t v) {
    if (base == 0x10 || base == 0x14 || base == 0x18 || base == 0x1c) {
      uint64_t sz = rebar_size(base < 0x18 ? 0 : 2);
      if (base == 0x10 || base == 0x18) return (uint32_t)((v & ~0xFu & ~(uint32_t)((sz - 1) & 0xFFFFFFFF)) | 0xC);
      return (uint32_t)(v & ~(uint32_t)((sz - 1) >> 32));
    }
    return v & ~((512u << 10) - 1) & ~0xFu;
  }
  bool down() {
    if (pending_reset && clock >= down_until) { pending_reset = false; power_on(false); }
    return clock < down_until;
  }
  uint32_t cfg_rd(uint32_t off, unsigned size) {
    if (down()) { ev("cfg_read_while_down"); return size == 4 ? 0xffffffffu : (1u << (8 * size)) - 1; }
    uint32_t d = dw(off & ~3u), m = size == 4 ? 0xffffffffu : (1u << (8 * size)) - 1;
    if ((off & ~3u) == 0x10 && bar0_moves > 0) { d ^= (uint32_t)bar0_moves << 28; bar0_moves--; }   // the kext still moving BAR0: a new address each read
    if ((off & ~3u) == 0x14 && bar0_hi_moves > 0) { d ^= (uint32_t)bar0_hi_moves << 4; bar0_hi_moves--; }
    return (d >> (8 * (off & 3))) & m;
  }
  void cfg_wr(uint32_t off, uint32_t val, unsigned size) {
    if (down()) { ev("cfg_write_while_down"); return; }
    cfg_writes.push_back({off, val, size});
    uint32_t base = off & ~3u, sh = 8 * (off & 3), m = (size == 4 ? 0xffffffffu : ((1u << (8 * size)) - 1)) << sh, old = dw(base);
    cfg_dword(base, (old & ~m) | ((val << sh) & m), old);
  }
  void cfg_dword(uint32_t base, uint32_t nw, uint32_t old) {
    if (base == 0x158 || base == 0x15C || base == 0x164 || base == 0x168 || base == 0x324) return put(base, nw);
    if (base == 0x154 || base == 0x160) return put(base, old & ~nw);
    bool rebar_ctrl = false;
    for (int i = 0; i < rebar_bars; i++) if (base == CFG_REBAR + 8 + 8 * (uint32_t)i) rebar_ctrl = true;
    if (base == 0x00 || base == 0x08 || base == 0x20 || base == 0x34 || (base >= 0x100 && !rebar_ctrl)) { if (nw != old) ev("cfg_write_readonly"); return; }
    if (base == 0x04) return put(base, (old & 0xFFFF0000) | (nw & 0x0547));
    if (base == 0x10 || base == 0x14 || base == 0x18 || base == 0x1c || base == 0x24) return put(base, bar_mask(base, nw));
    if (base == 0x30) return put(base, (nw & ~(128u * 1024 - 1) & 0xFFFFF800) | (nw & 1));
    if (rebar_ctrl) {
      uint32_t sz = (nw >> 8) & 0x3f, cap = dw(base - 4);
      if (!((cap >> (4 + sz)) & 1)) { ev("rebar_unsupported_size"); return; }
      put(base, (old & ~0x3f00u) | (sz << 8));
      uint32_t b0 = (old & 7) == 0 ? 0x10 : 0x18;
      put(b0, bar_mask(b0, dw(b0))); put(b0 + 4, bar_mask(b0 + 4, dw(b0 + 4)));
      return;
    }
    if (base == 0x0c || base == 0x3c || base == CFG_PCIE + 8 || base == CFG_PCIE + 0x10 || base == CFG_PCIE + 0x28 || base == CFG_PCIE + 0x30)
      return put(base, (old & 0xFFFF0000) | (nw & 0xFFFF));
    ev("cfg_write_unmodelled"); put(base, nw);
  }
  uint64_t bar0() { return ((uint64_t)dw(0x14) << 32 | dw(0x10)) & ~0xFull; }

  // ---- VRAM (sparse 1 MiB pages) and the card's view of memory ----
  void vram_io(uint64_t pa, uint8_t *buf, size_t n, bool wr) {
    if (pa + n > ((uint64_t)memsize_mb << 20)) { ev("vram_access_outside"); return; }
    for (size_t i = 0; i < n;) {
      uint64_t pg = (pa + i) >> 20, o = (pa + i) & 0xFFFFF; size_t k = std::min<size_t>(n - i, (1u << 20) - o);
      auto it = vram.find(pg);
      if (wr) { if (it == vram.end()) it = vram.emplace(pg, std::vector<uint8_t>(1u << 20, 0)).first; memcpy(&it->second[o], buf + i, k); }
      else { if (it == vram.end()) memset(buf + i, 0, k); else memcpy(buf + i, &it->second[o], k); }
      i += k;
    }
  }
  uint64_t fb_base() { return (uint64_t)(regs[MMHUB_MC_VM_FB_LOCATION_BASE] & 0xFFFFFF) << 24; }
  std::vector<uint8_t> mc_read(uint64_t mc, size_t n) {
    std::vector<uint8_t> b(n, 0); uint64_t pa = mc - fb_base();
    if (mc < fb_base() || pa >= ((uint64_t)memsize_mb << 20)) { ev("mc_out_of_fb"); return b; }
    vram_io(pa, b.data(), n, false); return b;
  }
  void mc_write(uint64_t mc, const void *d, size_t n) {
    uint64_t pa = mc - fb_base();
    if (mc < fb_base() || pa >= ((uint64_t)memsize_mb << 20)) { ev("mc_out_of_fb"); return; }
    vram_io(pa, (uint8_t *)d, n, true);
  }
  bool mc_equal(uint64_t mc, const std::string &exp) { auto b = mc_read(mc, exp.size()); return memcmp(b.data(), exp.data(), exp.size()) == 0; }

  // ---- registers: BAR5 directly, every register also at SMN address reg * 4 through PCIE_INDEX2/DATA2 (amdgpu_device_indirect_rreg/wreg) ----
  uint32_t mmio_rd(uint32_t a) { if (a >= mmio_size_dwords) ev("mmio_past_bar5"); return reg_rd(a); }
  void mmio_wr(uint32_t a, uint32_t x) { if (a >= mmio_size_dwords) ev("mmio_past_bar5"); reg_wr(a, x); }
  uint32_t reg_rd(uint32_t a) {
    if (down()) { ev("mmio_read_while_down"); return 0xFFFFFFFF; }
    touched.insert(a);
    if (a == THM_CG_MULT_THERMAL_STATUS && !thm_seq.empty()) return thm_seq[thm_at < thm_seq.size() - 1 ? thm_at++ : thm_at];
    if (a == SMUIO_ROM_DATA) {
      uint32_t x = 0xffffffff; if (rom_ptr + 4 <= img.size()) memcpy(&x, &img[rom_ptr], 4);
      if (rom_glitch && rom_pass == 2 && rom_ptr == 0) x = rom_glitch;
      rom_ptr += 4; return x;
    }
    if (a == NBIO_PCIE_DATA2) return smn_rd(regs[NBIO_PCIE_INDEX2]);
    if (a == NBIO_MM_DATA) {
      uint32_t idx = regs[NBIO_MM_INDEX];
      if (!(idx & 0x80000000)) { ev("mm_index_register_space"); return 0; }
      uint32_t x; vram_io(((uint64_t)regs[NBIO_MM_INDEX_HI] << 31) | (idx & 0x7FFFFFFF), (uint8_t *)&x, 4, false); return x;
    }
    if (a == MP1_MP1_SMN_C2PMSG_66 || a == MP1_MP1_SMN_C2PMSG_82 || a == MP1_MP1_SMN_C2PMSG_90) return smu_alive ? regs[a] : 0xFFFFFFFF;
    if (a == GC_CP_STAT) return cp_stat;
    if (a == GC_CC_GC_SHADER_ARRAY_CONFIG) {   // one array's fuses: a broadcast index selects none
      if (!grbm_selected) ev("cu_config_read_unselected");
      uint32_t ix = regs[GC_GRBM_GFX_INDEX];
      if (ix & (GC_GRBM_GFX_INDEX__SE_BROADCAST_WRITES_MASK | GC_GRBM_GFX_INDEX__SH_BROADCAST_WRITES_MASK)) { ev("cu_config_read_broadcast"); return 0; }
      uint32_t se = (ix & GC_GRBM_GFX_INDEX__SE_INDEX_MASK) >> GC_GRBM_GFX_INDEX__SE_INDEX__SHIFT, sh = (ix & GC_GRBM_GFX_INDEX__SH_INDEX_MASK) >> GC_GRBM_GFX_INDEX__SH_INDEX__SHIFT;
      if (se >= 4 || sh != 0) { ev("cu_config_bad_index"); return 0xFFFFFFFF; }
      return inactive_cus[se] << GC_CC_GC_SHADER_ARRAY_CONFIG__INACTIVE_CUS__SHIFT;
    }
    auto it = regs.find(a); return it == regs.end() ? 0 : it->second;
  }
  void reg_wr(uint32_t a, uint32_t x) {
    if (down()) { ev("mmio_write_while_down"); return; }
    touched.insert(a);
    wr_order.push_back(a);
    if (a == GC_GRBM_GFX_INDEX) grbm_selected = true;
    if (a == GC_GRBM_GFX_CNTL) grbm_vmid = (x & GC_GRBM_GFX_CNTL__VMID_MASK) >> GC_GRBM_GFX_CNTL__VMID__SHIFT;
    if (a == GC_SH_MEM_CONFIG) sh_mem_config[grbm_vmid] = x;
    if (a == GC_SH_MEM_BASES) sh_mem_bases[grbm_vmid] = x;
    if (a == SMUIO_ROM_INDEX) { rom_ptr = x; rom_pass++; return; }
    if (a == NBIO_PCIE_DATA2) {
      uint32_t addr = regs[NBIO_PCIE_INDEX2];
      if (addr < (512u << 10)) { indirect++; reg_wr(addr / 4, x); } else smn[addr] = x;
      return;
    }
    if (a == CLK_MARK) clk_calls.push_back(x);
    if (a == NBIO_MM_DATA) {
      uint32_t idx = regs[NBIO_MM_INDEX];
      if (idx & 0x80000000) vram_io(((uint64_t)regs[NBIO_MM_INDEX_HI] << 31) | (idx & 0x7FFFFFFF), (uint8_t *)&x, 4, true);
      return;
    }
    if (a == NBIO_BIOS_SCRATCH_7) {
      if ((x & 0x200) && !(regs[a] & 0x200)) {
        posted++; last_mark = regs[MARK]; ev("posted");
        if (post_resets_bar == 1 || post_resets_bar == 3) { if (rebar_bars) put(CFG_REBAR + 8, (dw(CFG_REBAR + 8) & ~0x3f00u) | 15u << 8); }
        if (post_resets_bar == 1) put(0x10, 0xC);
        if (post_resets_bar == 1 || post_resets_bar == 2) put(0x14, 0);
      }
      regs[a] = x; return;
    }
    if (a == THM_THM_THERMAL_INT_ENA) { regs[a] = x; timeline.push_back("thm_int_ena=" + std::to_string(x)); return; }
    if (a == MP0_MP0_SMN_C2PMSG_35) return psp_bl(x);
    if (a == MP0_MP0_SMN_C2PMSG_64) return psp_ctrl(x);
    if (a == MP0_MP0_SMN_C2PMSG_67) return psp_wptr(x);
    if (a == MP1_MP1_SMN_C2PMSG_66) return smu_msg(x);
    if (a == MP1_MP1_SMN_C2PMSG_82 || a == MP1_MP1_SMN_C2PMSG_90) { if (smu_alive) regs[a] = x; return; }
    regs[a] = x;
  }
  uint32_t smn_rd(uint32_t addr) {
    if (addr == 0x3b10024) return smu_alive ? 1 : 0;
    if (addr < (512u << 10)) { indirect++; return reg_rd(addr / 4); }
    auto it = smn.find(addr); return it == smn.end() ? 0 : it->second;
  }

  // ---- PSP 11 (psp_v11_0.c) ----
  void psp_bl(uint32_t cmd) {
    regs[MP0_MP0_SMN_C2PMSG_35] = cmd;
    if (psp_sos) { ev("psp_bl_cmd_after_sos"); return; }
    const std::string *exp = cmd == 0x10000 ? &fw.sysdrv : cmd == 0x20000 ? &fw.sos : nullptr;
    if (!exp) { ev("psp_bl_unknown"); regs[MP0_MP0_SMN_C2PMSG_35] = 0x80000001; return; }
    bool ok = mc_equal((uint64_t)regs[MP0_MP0_SMN_C2PMSG_36] << 20, *exp);
    PspEv e; e.kind = "BL"; e.a = cmd; e.ok = ok; psp_log.push_back(e);
    if (!ok) { ev("psp_bl_bad_image"); regs[MP0_MP0_SMN_C2PMSG_35] = 0x80000001; return; }
    if (cmd == 0x10000) {
      psp_sysdrv = true; regs[MP0_MP0_SMN_C2PMSG_35] = psp_stall == 5 ? 0x10000 : 0x80000000;
      if (sysdrv_starts_sos) { psp_sos = true; regs[MP0_MP0_SMN_C2PMSG_81] = 0x1; regs[MP0_MP0_SMN_C2PMSG_64] = 0x80000000; }
      return;
    }
    if (!psp_sysdrv) ev("psp_sos_before_sysdrv");
    if (psp_stall == 2) return;
    psp_sos = true; regs[MP0_MP0_SMN_C2PMSG_81] = 0x1; regs[MP0_MP0_SMN_C2PMSG_64] = psp_stall == 4 ? 0 : 0x80000000;
  }
  void psp_ctrl(uint32_t x) {
    if (x == GFX_CTRL_CMD_ID_MODE1_RST) {
      PspEv e; e.kind = "MODE1_RST"; psp_log.push_back(e); timeline.push_back("MODE1_RST");
      if (!psp_sos) ev("mode1_without_sos");
      mode1_count++; down_until = clock + reset_window_ns; pending_reset = true; return;
    }
    if (!psp_sos) { ev("psp_ctrl_without_sos"); regs[MP0_MP0_SMN_C2PMSG_64] = x; return; }
    if (x == GFX_CTRL_CMD_ID_DESTROY_RINGS) { PspEv e; e.kind = "DESTROY_RINGS"; psp_log.push_back(e); ring_set = false; regs[MP0_MP0_SMN_C2PMSG_64] = 0x80000000; return; }
    if (x == 2u << 16) {
      if (psp_stall == 3) { regs[MP0_MP0_SMN_C2PMSG_64] = x; return; }
      ring = regs[MP0_MP0_SMN_C2PMSG_69] | (uint64_t)regs[MP0_MP0_SMN_C2PMSG_70] << 32; ring_size = regs[MP0_MP0_SMN_C2PMSG_71];
      psp_rptr = regs[MP0_MP0_SMN_C2PMSG_67]; ring_set = true;
      PspEv e; e.kind = "RING_CREATE"; e.a = ring; e.b = ring_size; psp_log.push_back(e);
      regs[MP0_MP0_SMN_C2PMSG_64] = 0x80000000; return;
    }
    ev("psp_ctrl_unknown"); regs[MP0_MP0_SMN_C2PMSG_64] = x;
  }
  void psp_wptr(uint32_t x) {
    regs[MP0_MP0_SMN_C2PMSG_67] = x;
    if (!ring_set) { ev("psp_wptr_without_ring"); return; }
    while (psp_rptr < x) {
      uint32_t off = (psp_rptr * 4) % ring_size;
      auto fb = mc_read(ring + off, sizeof(struct psp_gfx_rb_frame));
      struct psp_gfx_rb_frame fr; memcpy(&fr, fb.data(), sizeof(fr));
      psp_rptr += sizeof(fr) / 4;
      psp_cmd(fr);
    }
  }
  void psp_cmd(const struct psp_gfx_rb_frame &fr) {
    uint64_t cmd_mc = fr.cmd_buf_addr_lo | (uint64_t)fr.cmd_buf_addr_hi << 32, fence_mc = fr.fence_addr_lo | (uint64_t)fr.fence_addr_hi << 32;
    auto cb = mc_read(cmd_mc, sizeof(struct psp_gfx_cmd_resp));
    struct psp_gfx_cmd_resp cmd; memcpy(&cmd, cb.data(), sizeof(cmd));
    uint32_t status = 0; PspEv e;
    int n = ++psp_count;
    if (n == hang_psp_nth) {   // no response, no fence; a boot-config command keeps its sub-command and values for the log
      e.kind = "HANG"; e.a = cmd.cmd_id;
      if (cmd.cmd_id == GFX_CMD_ID_BOOT_CFG) { e.b = cmd.cmd.boot_cfg.sub_cmd; e.c = cmd.cmd.boot_cfg.boot_config; e.d = cmd.cmd.boot_cfg.boot_config_valid; }
      psp_log.push_back(e); return;
    }
    if (cmd.cmd_id == GFX_CMD_ID_LOAD_IP_FW) {
      auto &c = cmd.cmd.cmd_load_ip_fw;
      auto data = mc_read(c.fw_phy_addr_lo | (uint64_t)c.fw_phy_addr_hi << 32, c.fw_size);
      bool ok = false;
      for (auto &p : fw.ip) if (p.first == (uint32_t)c.fw_type && p.second.size() == c.fw_size && memcmp(p.second.data(), data.data(), c.fw_size) == 0) ok = true;
      e.kind = "LOAD_IP_FW"; e.a = c.fw_type; e.ok = ok; e.b = c.fw_size; psp_log.push_back(e);
      if (!ok || (uint32_t)c.fw_type == fail_ip_type) { status = 0xFFFF0001; ev("psp_bad_ip_fw"); }
      else if (c.fw_type == GFX_FW_TYPE_SMU && !smu_no_start) smu_start();
    } else if (cmd.cmd_id == GFX_CMD_ID_SETUP_TMR) {
      auto &c = cmd.cmd.cmd_setup_tmr;
      e.kind = "SETUP_TMR"; e.a = c.buf_phy_addr_lo | (uint64_t)c.buf_phy_addr_hi << 32; e.b = c.system_phy_addr_lo | (uint64_t)c.system_phy_addr_hi << 32;
      e.c = c.buf_size; e.d = c.bitfield.virt_phy_addr; psp_log.push_back(e);
    } else if (cmd.cmd_id == GFX_CMD_ID_LOAD_ASD || cmd.cmd_id == GFX_CMD_ID_LOAD_TA) {
      auto &c = cmd.cmd.cmd_load_ta;
      const std::string &exp = cmd.cmd_id == GFX_CMD_ID_LOAD_ASD ? fw.asd : fw.ras;
      bool ok = c.app_len == exp.size() && mc_equal(c.app_phy_addr_lo | (uint64_t)c.app_phy_addr_hi << 32, exp);
      uint64_t shared = c.cmd_buf_phy_addr_lo | (uint64_t)c.cmd_buf_phy_addr_hi << 32;
      e.kind = cmd.cmd_id == GFX_CMD_ID_LOAD_ASD ? "LOAD_ASD" : "LOAD_TA"; e.ok = ok; e.a = shared; e.b = c.cmd_buf_len;
      if (cmd.cmd_id == GFX_CMD_ID_LOAD_TA) e.flags = mc_read(shared + 16, 16);
      psp_log.push_back(e);
      if (!ok) { status = 0xFFFF0001; ev("psp_bad_ta"); }
      else if (cmd.cmd_id == GFX_CMD_ID_LOAD_TA) { uint32_t z = ras_init_status; mc_write(shared + 8, &z, 4); cmd.resp.session_id = 1; }
    } else if (cmd.cmd_id == GFX_CMD_ID_BOOT_CFG) {
      auto &c = cmd.cmd.boot_cfg;
      e.kind = "BOOT_CFG"; e.a = c.sub_cmd; e.b = c.boot_config; e.c = c.boot_config_valid; psp_log.push_back(e);
      if (c.sub_cmd == BOOTCFG_CMD_GET) cmd.resp.uresp.boot_cfg.boot_cfg = boot_cfg;
      else if (c.sub_cmd == BOOTCFG_CMD_SET) boot_cfg_next = c.boot_config;
    } else { e.kind = "UNEXPECTED"; e.a = cmd.cmd_id; psp_log.push_back(e); ev("psp_unexpected_cmd"); status = 0xFFFF0002; }
    if (n == fail_psp_nth) status = 0xFFFF0003;
    cmd.resp.status = status;
    mc_write(cmd_mc, &cmd, sizeof(cmd));
    mc_write(fence_mc, &fr.fence_value, 4);
  }

  // ---- SMU 11 mailbox (vega20_smumgr.c) ----
  void smu_start() { smu_alive = true; smu_hung = false; smu = SmuState(); regs[MP1_MP1_SMN_C2PMSG_90] = 1; regs[MP1_MP1_SMN_C2PMSG_82] = 0; regs[MP1_MP1_SMN_C2PMSG_66] = 0; }
  std::vector<uint32_t> levels(uint32_t clk) {
    auto ov = dpm_levels.find(clk);
    if (ov != dpm_levels.end()) return ov->second;
    auto it = smu.tables.find(TABLE_PPTABLE);
    if (it != smu.tables.end() && (clk == PPCLK_GFXCLK || clk == PPCLK_UCLK)) {
      PPTable_t pt; memcpy(&pt, it->second.data(), sizeof(pt));
      std::set<uint32_t> s;
      if (clk == PPCLK_GFXCLK) { for (auto x : pt.FreqTableGfx) if (x) s.insert(x); } else { for (auto x : pt.FreqTableUclk) if (x) s.insert(x); }
      if (!s.empty()) return std::vector<uint32_t>(s.begin(), s.end());
    }
    switch (clk) {
      case PPCLK_GFXCLK: return {800, 1200, 1500, 1725}; case PPCLK_UCLK: return {350, 800, 1000}; case PPCLK_SOCCLK: return {310, 600, 970};
      case PPCLK_FCLK: return {550, 1100}; case PPCLK_DCEFCLK: return {310, 600}; case PPCLK_DISPCLK: return {400}; case PPCLK_PIXCLK: return {400};
      case PPCLK_PHYCLK: return {300}; case PPCLK_ECLK: return {300}; case PPCLK_VCLK: return {300}; case PPCLK_DCLK: return {300};
    }
    return {};
  }
  void smu_msg(uint32_t msg) {
    if (!smu_alive) { ev("smu_msg_without_pmfw"); return; }
    if (smu_hung) { timeline.push_back("smu_ignored:" + std::to_string(msg)); return; }
    int n = ++smu_count;
    if (n == hang_smu_nth || msg == hang_smu_msg) { timeline.push_back("smu_hang:" + std::to_string(msg)); return; }   // never answered
    regs[MP1_MP1_SMN_C2PMSG_66] = msg;
    uint32_t p = regs[MP1_MP1_SMN_C2PMSG_82], arg = 0, res = PPSMC_Result_OK, clk = p >> 16;
    smu_log.push_back({msg, p}); timeline.push_back("smu:" + std::to_string(msg));
    auto known_clk = [&] { return clk < PPCLK_COUNT; };
    switch (msg) {
      case PPSMC_MSG_GetSmuVersion: arg = fw.smc_version; break;
      case PPSMC_MSG_ReadSerialNumTop32: arg = 0x1234; break;
      case PPSMC_MSG_ReadSerialNumBottom32: arg = 0x5678; break;
      case PPSMC_MSG_SetAllowedFeaturesMaskHigh: smu.allowed[1] = p; break;
      case PPSMC_MSG_SetAllowedFeaturesMaskLow: smu.allowed[0] = p; break;
      case PPSMC_MSG_EnableAllSmuFeatures:
        if (!smu.tables.count(TABLE_PPTABLE)) { ev("smu_enable_before_pptable"); res = PPSMC_Result_Failed; }
        else smu.enabled = (smu.allowed[0] | (uint64_t)smu.allowed[1] << 32) & ~smu_refuse;
        break;
      case PPSMC_MSG_DisableAllSmuFeatures: smu.enabled = 0; break;
      case PPSMC_MSG_PrepareMp1ForShutdown: smu.shutdown = true; if (shutdown_drops_psp) regs[MP0_MP0_SMN_C2PMSG_64] = 0; break;
      case PPSMC_MSG_DisableSmuFeaturesLow: smu.enabled &= ~(uint64_t)p; break;
      case PPSMC_MSG_DisableSmuFeaturesHigh: smu.enabled &= ~((uint64_t)p << 32); break;
      case PPSMC_MSG_GetEnabledSmuFeaturesLow: arg = (uint32_t)smu.enabled; break;
      case PPSMC_MSG_GetEnabledSmuFeaturesHigh: arg = (uint32_t)(smu.enabled >> 32); break;
      case PPSMC_MSG_SetDriverDramAddrHigh: smu.drv[1] = p; break;
      case PPSMC_MSG_SetDriverDramAddrLow: smu.drv[0] = p; break;
      case PPSMC_MSG_SetToolsDramAddrHigh: smu.tools[1] = p; break;
      case PPSMC_MSG_SetToolsDramAddrLow: smu.tools[0] = p; break;
      case PPSMC_MSG_GetDpmFreqByIndex: case PPSMC_MSG_GetDcModeMaxDpmFreq: case PPSMC_MSG_GetMaxDpmFreq:
      case PPSMC_MSG_SetSoftMinByFreq: case PPSMC_MSG_SetSoftMaxByFreq: {
        if (!known_clk()) { res = PPSMC_Result_Failed; ev("smu_bad_clock"); break; }
        auto lv = levels(clk);
        if (msg == PPSMC_MSG_GetDpmFreqByIndex) arg = (p & 0xffff) == 0xff ? (uint32_t)lv.size() : lv.at(p & 0xffff);
        else if (msg == PPSMC_MSG_GetDcModeMaxDpmFreq) arg = clk == PPCLK_UCLK || lv.empty() ? 0 : lv.back();
        else if (msg == PPSMC_MSG_GetMaxDpmFreq) arg = lv.empty() ? 0 : lv.back();
        break;
      }
      case PPSMC_MSG_GetAVFSVoltageByDpm: arg = 4 * (700 + (p & 0xffff) / 10); break;
      case PPSMC_MSG_GetPptLimit: arg = smu.ppt; break;
      case PPSMC_MSG_GetCurrentRpm: arg = fan_rpm; break;
      case PPSMC_MSG_SetPptLimit: smu.ppt = p; break;
      case PPSMC_MSG_TransferTableDram2Smu: case PPSMC_MSG_TransferTableSmu2Dram: {
        uint32_t tid = p & 0xffff; uint64_t mc = (uint64_t)smu.drv[1] << 32 | smu.drv[0];
        size_t sz = tid == TABLE_PPTABLE ? sizeof(PPTable_t) : tid == TABLE_WATERMARKS ? sizeof(Watermarks_t) : tid == TABLE_OVERDRIVE ? sizeof(OverDriveTable_t) :
                    tid == TABLE_SMU_METRICS ? sizeof(SmuMetrics_t) : tid == TABLE_ACTIVITY_MONITOR_COEFF ? sizeof(DpmActivityMonitorCoeffInt_t) : 0;
        if (!sz) { res = PPSMC_Result_Failed; ev("smu_bad_table"); break; }
        if (msg == PPSMC_MSG_TransferTableDram2Smu) {
          smu.tables[tid] = mc_read(mc, sz);
          if (tid == TABLE_PPTABLE) { PPTable_t pt; memcpy(&pt, smu.tables[tid].data(), sizeof(pt)); if (pt.SocketPowerLimitAc0) smu.ppt = pt.SocketPowerLimitAc0; }
        } else {
          std::vector<uint8_t> d(sz, 0);
          if (smu.tables.count(tid)) d = smu.tables[tid];
          else if (tid == TABLE_OVERDRIVE) { OverDriveTable_t o; memset(&o, 0, sizeof(o)); o.GfxclkFmin = 800; o.GfxclkFmax = 1725; o.UclkFmax = 1000; o.OverDrivePct = 0; o.FanMaximumRpm = 3850; memcpy(d.data(), &o, sz); }
          mc_write(mc, d.data(), sz);
        }
        break;
      }
      case PPSMC_MSG_NumOfDisplays: case PPSMC_MSG_SetMinDeepSleepDcefclk: case PPSMC_MSG_RunBtc: case PPSMC_MSG_RunAfllBtc:
      case PPSMC_MSG_OverridePcieParameters: case PPSMC_MSG_SetUclkFastSwitch: case PPSMC_MSG_SetFclkGfxClkRatio: case PPSMC_MSG_SetFanTemperatureTarget:
      case PPSMC_MSG_PrepareMp1ForUnload: case PPSMC_MSG_SetHardMinByFreq: case PPSMC_MSG_SetHardMaxByFreq: break;
      // BACO (vega20_baco.c): the chip's core off / on again, as NBIO BACO_CNTL's BACO_MODE shows it
      case PPSMC_MSG_EnterBaco: regs[NBIO_BACO_CNTL] |= NBIO_BACO_CNTL__BACO_MODE_MASK; ev("baco_in"); break;
      case PPSMC_MSG_ExitBaco: regs[NBIO_BACO_CNTL] &= ~NBIO_BACO_CNTL__BACO_MODE_MASK; ev("baco_out"); break;
      default: res = PPSMC_Result_UnknownCmd; ev("smu_unknown_msg");
    }
    if (msg == fail_smu_msg || n == fail_smu_nth) res = PPSMC_Result_Failed;
    regs[MP1_MP1_SMN_C2PMSG_82] = arg; regs[MP1_MP1_SMN_C2PMSG_90] = res;
  }
};

}  // namespace vm
