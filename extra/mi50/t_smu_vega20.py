#!/usr/bin/env python3
"""Offline check (no card) of AM_SMU's Vega 20 (MP1 11.0.2) branch, design v2 B13 / §6 / §3 row AM_SMU.

A fake AMDev with a scripted SMU mailbox logs every (msg, param) pair. Checks:
(1) init_hw = GetSmuVersion, SetToolsDramAddrHigh/Low (0x19000 boot allocation), SetDriverDramAddrHigh/Low and NOTHING else (no EnableAllSmuFeatures);
(2) set_clocks(0/None/-1) sends SetSoftMinByFreq AND SetSoftMaxByFreq for GFXCLK/UCLK/SOCCLK whose DPM feature is enabled, min block then max block;
(3) set_power_limit -> SetPptLimit, refused when FEATURE_PPT is off; get_power_limit -> GetPptLimit(POWER_SOURCE_AC << 16);
(4) read_table(6)/read_metrics: SetDriverDramAddrHigh/Low re-sent, TransferTableSmu2Dram, HDP_READ_CACHE_INVALIDATE write 1 + readback, struct decoded;
(5) read_od / write_od / od_defaults (GetAVFSVoltageByDpm with (AVFS_CURVE<<24)|(85<<16)|freq, /4*4), write_table = memcpy, flush_hdp, RCC_CONFIG_MEMSIZE read,
    addr, TransferTableDram2Smu, and NO message after the OD write; (6) get_features = 0xC/0xD -> low | high << 32; (7) mode1_reset raises without
    AM_PSP.mode1_reset and delegates with it, never an SMU message; (8) 0xFC/0xFD/0xFE/0xFF -> ResponseError naming the vega20_ppsmc.h code;
(9) enable_dpm_full replays vega20_enable_dpm_tasks' messages in order and validates its required inputs; (10) every message number the class
    sends == the value parsed from vega20_ppsmc.h; (11) sizeof(SmuMetrics_t)/sizeof(OverDriveTable_t) from smu_11_0_2 == a clang compile of
    smu11_driver_if.h (== the Tables-stage numbers 96 / 32); (12) the register names the branch asks AMDev for resolve in the Vega 20 tables;
(13) existing cards: the pre-change AM_SMU (git mi50:ip.py) and the new one produce byte-identical register traces for gfx11 / MI300 / MI350 / MP1 14.x.
Run: PYTHONPATH=<fork> python3 extra/mi50/t_smu_vega20.py   [env MI50_LINUX_AMD=<linux>/drivers/gpu/drm/amd, default below]
"""
import os, re, sys, ctypes, pathlib, subprocess, tempfile, types
from tinygrad.runtime.autogen.am import am, smu_11_0_2 as smu
from tinygrad.runtime.support.amd import import_module
import tinygrad.runtime.support.am.ip as ipmod
from tinygrad.helpers import hi32, lo32

LINUX = pathlib.Path(os.getenv("MI50_LINUX_AMD", "/private/tmp/claude-501/-Users-leonh-Documents-Ai-shit/bba39989-2505-4a2d-a9f5-db421196e2ba/"
                                                 "scratchpad/repos/linux/drivers/gpu/drm/amd"))
FORK = pathlib.Path(__file__).resolve().parents[2]
fails: list[str] = []
def check(cond, msg):
  print(("ok   " if cond else "FAIL ") + msg)
  if not cond: fails.append(msg)
def raises(exc, fn, needle=""):
  try: fn()
  except exc as e: return needle in str(e)
  except Exception as e: print("     unexpected", type(e).__name__, e)  # noqa: BLE001
  return False

# --- fake device: registers + scripted mailbox ------------------------------------------------------------------------------------------
MC_BASE = 1 << 32  # paddr2mc adds this so SetXxxDramAddrHigh carries a non-zero high word
class FakeReg:
  def __init__(self, adev, name): self.adev, self.name, self.val = adev, name, 0
  def read(self): self.adev.trace.append(("r", self.name, self.val)); return self.val
  def write(self, v=None, **kw):
    if v is None: v = sum(kw.values())
    self.adev.trace.append(("w", self.name, v)); self.val = v
    if self.name in ("mmMP1_SMN_C2PMSG_66", "mmMP1_SMN_C2PMSG_75"): self.adev.mailbox(v, debug=self.name == "mmMP1_SMN_C2PMSG_75")

class FakeMM:
  def __init__(self, adev): self.adev, self.next, self.allocs = adev, 0x1000, []
  def palloc(self, size, align=0x1000, zero=True, boot=False):
    p = self.next; self.next += (size + 0xfff) & ~0xfff; self.allocs.append((p, size, boot)); return p

class FakeVram:
  def __init__(self, adev): self.buf = bytearray(1 << 20)
  def view(self, paddr, size): return memoryview(self.buf)[paddr:paddr+size]

class FakeAdev:
  REGS = ["mmMP1_SMN_C2PMSG_53", "mmMP1_SMN_C2PMSG_54", "mmMP1_SMN_C2PMSG_75", "mmMP1_SMN_C2PMSG_66", "mmMP1_SMN_C2PMSG_82", "mmMP1_SMN_C2PMSG_90",
          "regHDP_READ_CACHE_INVALIDATE", "regRCC_CONFIG_MEMSIZE"]
  def __init__(self, ip_ver, psp_reset=False):
    self.ip_ver, self.devfmt, self.trace, self.msgs = ip_ver, "fake", [], []
    for r in self.REGS: setattr(self, r, FakeReg(self, r))
    self.mm, self.vram, self.is_booting = FakeMM(self), FakeVram(self), True
    self.gmc = types.SimpleNamespace(flush_hdp=lambda: self.trace.append(("flush_hdp", "", 0)))
    self.psp = types.SimpleNamespace(mode1_reset=lambda: self.trace.append(("psp_mode1_reset", "", 0))) if psp_reset else types.SimpleNamespace()
    self.fail: dict[int, int] = {}       # msg -> PPSMC_Result code to answer with
    self.features = 0                    # 64-bit enabled feature mask the fake SMU reports
    self.levels = {smu.PPCLK_GFXCLK: [500, 1000, 1700], smu.PPCLK_UCLK: [350, 800, 1000], smu.PPCLK_SOCCLK: [300, 600, 1000]}  # fake MHz
    self.smu_tables: dict[int, bytes] = {}
    self.pending = []                    # msgs whose response is withheld (timeout path)
    self.smu_version = 0x283c00          # fake; the number is the MI50 SMC header's version (Vega20 Firmware Facts, vega20_smc.bin row)
  def paddr2mc(self, p): return p + MC_BASE
  def is_hive(self): return False
  def _ip_module(self, prefix, hwip): return import_module(prefix, self.ip_ver[hwip])
  def mailbox(self, msg, debug=False):
    param = (self.mmMP1_SMN_C2PMSG_53 if debug else self.mmMP1_SMN_C2PMSG_82).val
    self.msgs.append((msg, param))
    if msg in self.pending: return
    mod = self.smu_obj.smu_mod  # resolve the number through the card's own message module (13.x numbers differ from Vega 20's)
    name = "" if debug else {v: k for k, v in vars(mod).items() if k.startswith("PPSMC_MSG_")}.get(msg, "")
    resp, arg = self.fail.get(msg, 1), 0
    if name == "PPSMC_MSG_GetSmuVersion": arg = self.smu_version
    elif name == "PPSMC_MSG_GetEnabledSmuFeaturesLow": arg = self.features & 0xffffffff
    elif name == "PPSMC_MSG_GetEnabledSmuFeaturesHigh": arg = self.features >> 32
    elif name == "PPSMC_MSG_GetDpmFreqByIndex":
      clk, idx = param >> 16, param & 0xffff; arg = len(self.levels[clk]) if idx == 0xff else self.levels[clk][idx]
    elif name == "PPSMC_MSG_GetAVFSVoltageByDpm": arg = (param & 0xffff) * 2 + 3  # fake: not a multiple of 4, so the /4*4 truncation is visible
    elif name == "PPSMC_MSG_GetPptLimit": arg = 999  # fake watts
    elif name == "PPSMC_MSG_TransferTableSmu2Dram":
      tid, paddr = param & 0xffff, self.smu_obj.driver_table_paddr
      if tid == smu.TABLE_SMU_METRICS: t = smu.SmuMetrics_t(); t.TemperatureHotspot, t.TemperatureHBM, t.CurrSocketPower = 61, 55, 123
      elif tid == smu.TABLE_OVERDRIVE: t = smu.OverDriveTable_t(); t.GfxclkFmin, t.GfxclkFmax, t.UclkFmax, t.OverDrivePct = 800, 1700, 1000, 0
      else: t = (ctypes.c_uint8 * 0x4000)()
      self.vram.view(paddr, ctypes.sizeof(t))[:] = bytes(t)
    elif name == "PPSMC_MSG_TransferTableDram2Smu":
      self.smu_tables[param & 0xffff] = bytes(self.vram.view(self.smu_obj.driver_table_paddr, 0x4000))
    (self.mmMP1_SMN_C2PMSG_53 if debug else self.mmMP1_SMN_C2PMSG_82).val = arg
    (self.mmMP1_SMN_C2PMSG_54 if debug else self.mmMP1_SMN_C2PMSG_90).val = resp

VEGA20 = {am.MP0_HWIP: (11,0,2), am.MP1_HWIP: (11,0,2), am.GC_HWIP: (9,4,0), am.HDP_HWIP: (4,2,0), am.NBIO_HWIP: (7,4,0)}
def mk(cls=ipmod.AM_SMU, psp_reset=False, features=None):
  adev = FakeAdev(dict(VEGA20), psp_reset=psp_reset); s = cls(adev); adev.smu_obj = s; s.init_sw(); adev.is_booting = False
  adev.features = (smu.FEATURE_DPM_GFXCLK_MASK | smu.FEATURE_DPM_UCLK_MASK | smu.FEATURE_DPM_SOCCLK_MASK | smu.FEATURE_PPT_MASK) if features is None else features
  adev.msgs.clear(); adev.trace.clear(); return adev, s
M = {k: v for k, v in vars(smu).items() if k.startswith("PPSMC_MSG_")}
def names(msgs): return [(next(k[10:] for k, v in M.items() if v == m), p) for m, p in msgs]

# (1) init_sw / init_hw
adev, s = mk()
check([a for a in adev.mm.allocs] == [(0x1000, 0x4000, True), (0x5000, 0x19000, True)], f"init_sw: driver table 0x4000 + tools table 0x19000, both boot allocations: {adev.mm.allocs}")
s.init_hw()
tools_mc, drv_mc = adev.paddr2mc(s.tools_table_paddr), adev.paddr2mc(s.driver_table_paddr)
exp = [("GetSmuVersion", 0), ("SetToolsDramAddrHigh", hi32(tools_mc)), ("SetToolsDramAddrLow", lo32(tools_mc)),
       ("SetDriverDramAddrHigh", hi32(drv_mc)), ("SetDriverDramAddrLow", lo32(drv_mc))]
check(names(adev.msgs) == exp, f"init_hw sequence (design B13 1-3, nothing else): {names(adev.msgs)}")
check(all(m != smu.PPSMC_MSG_EnableAllSmuFeatures for m, _ in adev.msgs), "init_hw sends no EnableAllSmuFeatures 0x6 (design B13(4), U-S1)")
check(hi32(tools_mc) == 1 and hi32(drv_mc) == 1, "high words carry the mc base (hi32/lo32 split exercised)")
adev2, s2 = mk(); adev2.pending = [smu.PPSMC_MSG_GetSmuVersion]
check(raises(RuntimeError, s2.init_hw, "SMC is not running"), "init_hw refuses when GetSmuVersion gets no answer (vega20_start_smu :593-596)")
check(adev2.msgs == [(smu.PPSMC_MSG_GetSmuVersion, 0)], "  ... and sent nothing after the liveness check")

# (2) set_clocks
adev, s = mk(); got = s.set_clocks(0)
q = lambda c: [("GetDpmFreqByIndex", (c << 16) | 0xff)] + [("GetDpmFreqByIndex", (c << 16) | i) for i in range(3)]
exp = [("GetEnabledSmuFeaturesLow", 0), ("GetEnabledSmuFeaturesHigh", 0)] + q(smu.PPCLK_GFXCLK) + q(smu.PPCLK_UCLK) + q(smu.PPCLK_SOCCLK) + \
      [("SetSoftMinByFreq", (smu.PPCLK_GFXCLK << 16) | 500), ("SetSoftMinByFreq", (smu.PPCLK_UCLK << 16) | 350), ("SetSoftMinByFreq", (smu.PPCLK_SOCCLK << 16) | 300),
       ("SetSoftMaxByFreq", (smu.PPCLK_GFXCLK << 16) | 500), ("SetSoftMaxByFreq", (smu.PPCLK_UCLK << 16) | 350), ("SetSoftMaxByFreq", (smu.PPCLK_SOCCLK << 16) | 300)]
check(names(adev.msgs) == exp, f"set_clocks(0): features, levels, then 0x26 x3 then 0x27 x3 (vega20_hwmgr.c:2417-2426): {names(adev.msgs)[-6:]}")
check(got == {smu.PPCLK_GFXCLK: (500, 500), smu.PPCLK_UCLK: (350, 350), smu.PPCLK_SOCCLK: (300, 300)}, f"set_clocks(0) returns what was sent: {got}")
adev, s = mk(); got = s.set_clocks(None)
check(got == {smu.PPCLK_GFXCLK: (500, 1700), smu.PPCLK_UCLK: (350, 1000), smu.PPCLK_SOCCLK: (300, 1000)}, f"set_clocks(None) = lowest min / highest max (unforce :2477-2532): {got}")
adev, s = mk(); got = s.set_clocks(-1)
check(got[smu.PPCLK_GFXCLK] == (1700, 1700) and names(adev.msgs)[-1] == ("SetSoftMaxByFreq", (smu.PPCLK_SOCCLK << 16) | 1000), f"set_clocks(-1) = highest level: {got}")
adev, s = mk(features=smu.FEATURE_DPM_GFXCLK_MASK); got = s.set_clocks(0)
check(set(got) == {smu.PPCLK_GFXCLK} and sum(m in (smu.PPSMC_MSG_SetSoftMinByFreq, smu.PPSMC_MSG_SetSoftMaxByFreq) for m, _ in adev.msgs) == 2,
      "set_clocks only touches clocks whose DPM feature is enabled (:1827-1828 etc.)")
adev, s = mk(features=0); got = s.set_clocks(0)
check(got == {} and names(adev.msgs) == [("GetEnabledSmuFeaturesLow", 0), ("GetEnabledSmuFeaturesHigh", 0)], "set_clocks with no DPM feature enabled sends no clock message")
adev, s = mk(); check(raises(ValueError, lambda: s.set_clocks(3), "out of range"), "set_clocks(3) with 3 levels -> ValueError")

# (3) power limit
adev, s = mk(); s.set_power_limit(150.4)
check(names(adev.msgs) == [("GetEnabledSmuFeaturesLow", 0), ("GetEnabledSmuFeaturesHigh", 0), ("SetPptLimit", 150)], f"set_power_limit(150.4): PPT check then SetPptLimit 150: {names(adev.msgs)}")
adev, s = mk(features=smu.FEATURE_DPM_GFXCLK_MASK)
check(raises(RuntimeError, lambda: s.set_power_limit(150), "FEATURE_PPT") and smu.PPSMC_MSG_SetPptLimit not in [m for m, _ in adev.msgs],
      "set_power_limit refuses (no SetPptLimit sent) while FEATURE_PPT is off (vega20_powertune.c:37-42)")
adev, s = mk(); v = s.get_power_limit()
check(v == 999 and names(adev.msgs) == [("GetPptLimit", smu.POWER_SOURCE_AC << 16)], f"get_power_limit = GetPptLimit(POWER_SOURCE_AC<<16) reply: {names(adev.msgs)} -> {v}")

# (4) read_table / read_metrics
adev, s = mk(); t = s.read_table(smu.SmuMetrics_t, smu.TABLE_SMU_METRICS)
check(names(adev.msgs) == [("SetDriverDramAddrHigh", hi32(drv_mc)), ("SetDriverDramAddrLow", lo32(drv_mc)), ("TransferTableSmu2Dram", 6)],
      f"read_table(6): addr re-sent then TransferTableSmu2Dram 6 (vega20_smumgr.c:178-193): {names(adev.msgs)}")
hdp = [x for x in adev.trace if x[1] == "regHDP_READ_CACHE_INVALIDATE"]
check(hdp == [("w", "regHDP_READ_CACHE_INVALIDATE", 1), ("r", "regHDP_READ_CACHE_INVALIDATE", 1)], f"HDP_READ_CACHE_INVALIDATE write 1 + readback after the transfer (hdp_v4_0.c:47-49): {hdp}")
check(adev.trace.index(hdp[0]) > max(i for i, x in enumerate(adev.trace) if x[1] == "mmMP1_SMN_C2PMSG_66"), "  ... and it comes after the last mailbox write")
check((t.TemperatureHotspot, t.TemperatureHBM, t.CurrSocketPower) == (61, 55, 123), f"SmuMetrics_t decoded from the driver table: hotspot {t.TemperatureHotspot} HBM {t.TemperatureHBM} W {t.CurrSocketPower}")
adev, s = mk(); t2 = s.read_metrics(); check(names(adev.msgs)[-1] == ("TransferTableSmu2Dram", 6) and t2.TemperatureHotspot == 61, "read_metrics() == read_table(SmuMetrics_t, 6)")
check(raises(ValueError, lambda: s.read_table(smu.SmuMetrics_t, smu.TABLE_COUNT), "TABLE_COUNT"), "table id >= TABLE_COUNT -> ValueError (:171-172)")
check(raises(ValueError, lambda: s.read_table(ctypes.c_uint8 * 0x4001, 6), "driver table"), "struct > 0x4000 -> ValueError")
adev, s = mk(); s.read_table(ctypes.c_uint8 * 44, smu.TABLE_ACTIVITY_MONITOR_COEFF | (2 << 16))
check(adev.msgs[-1] == (smu.PPSMC_MSG_TransferTableSmu2Dram, 8 | (2 << 16)), "workload type in bits 31:16 passes the id check (:275/:305)")

# (5) overdrive
adev, s = mk(); od = s.read_od()
check(names(adev.msgs)[-1] == ("TransferTableSmu2Dram", 9) and (od.GfxclkFmin, od.GfxclkFmax) == (800, 1700), f"read_od: table 9, Fmin {od.GfxclkFmin} Fmax {od.GfxclkFmax}")
adev.msgs.clear(); adev.trace.clear(); od.GfxclkVolt3 = 4 * 1000; s.write_od(od)
check(names(adev.msgs) == [("SetDriverDramAddrHigh", hi32(drv_mc)), ("SetDriverDramAddrLow", lo32(drv_mc)), ("TransferTableDram2Smu", 9)],
      f"write_od: addr re-sent then TransferTableDram2Smu 9 and NO message after it (vega20_smumgr.c:228-243; vega20_hwmgr.c:3131-3156): {names(adev.msgs)}")
ops = [x[0] if x[0] == "flush_hdp" else x[1] for x in adev.trace]
check(ops.index("flush_hdp") < ops.index("regRCC_CONFIG_MEMSIZE") < ops.index("mmMP1_SMN_C2PMSG_66"), f"write_table: flush_hdp, RCC_CONFIG_MEMSIZE read, then the mailbox (amdgpu_hdp.c:55-60): {ops[:4]}")
check(adev.smu_tables[9][:32] == bytes(od) and smu.OverDriveTable_t.from_buffer_copy(adev.smu_tables[9]).GfxclkVolt3 == 4000, "the SMU received the edited OD table bytes")
check(raises(TypeError, lambda: s.write_od(smu.SmuMetrics_t())), "write_od rejects a non-OverDriveTable_t")
adev, s = mk(); d = s.od_defaults()
exp = [("SetDriverDramAddrHigh", hi32(drv_mc)), ("SetDriverDramAddrLow", lo32(drv_mc)), ("TransferTableSmu2Dram", 9)] + \
      [("GetAVFSVoltageByDpm", (0 << 24) | (85 << 16) | f) for f in (800, 1250, 1700)]
check(names(adev.msgs) == exp, f"od_defaults: read OD, then GetAVFSVoltageByDpm for Fmin, (Fmin+Fmax)/2, Fmax with (AVFS_CURVE<<24)|(85<<16)|freq (:1227-1316): {names(adev.msgs)[3:]}")
check((d.GfxclkFreq1, d.GfxclkFreq2, d.GfxclkFreq3) == (800, 1250, 1700), f"od_defaults freqs {d.GfxclkFreq1, d.GfxclkFreq2, d.GfxclkFreq3}")
check((d.GfxclkVolt1, d.GfxclkVolt2, d.GfxclkVolt3) == tuple(((f * 2 + 3) // 4) * 4 for f in (800, 1250, 1700)), f"od_defaults volts = reply // VOLTAGE_SCALE * VOLTAGE_SCALE: {d.GfxclkVolt1, d.GfxclkVolt2, d.GfxclkVolt3}")
check(smu.PPSMC_MSG_TransferTableDram2Smu not in [m for m, _ in adev.msgs], "od_defaults does not write the table (write_od(od_defaults()) is Linux :1392)")

# (6) features
adev, s = mk(features=(1 << 40) | 0x30F); f = s.get_features()
check(f == (1 << 40) | 0x30F and names(adev.msgs) == [("GetEnabledSmuFeaturesLow", 0), ("GetEnabledSmuFeaturesHigh", 0)], f"get_features = low | high << 32 (vega20_smumgr.c:369-370): {f:#x}")

# (7) mode1_reset
adev, s = mk(); check(raises(RuntimeError, s.mode1_reset, "AM_PSP.mode1_reset") and adev.msgs == [], "mode1_reset on MP0 11.0.2 without AM_PSP.mode1_reset raises, sends nothing")
adev, s = mk(psp_reset=True); s.mode1_reset()
check(adev.msgs == [] and adev.trace == [("psp_mode1_reset", "", 0)], "mode1_reset on MP0 11.0.2 delegates to AM_PSP.mode1_reset, no SMU message")

# (8) responses
for code, name in ((0xFC, "CmdRejectedBusy"), (0xFD, "CmdRejectedPrereq"), (0xFE, "UnknownCmd"), (0xFF, "Failed")):
  adev, s = mk(); adev.fail[smu.PPSMC_MSG_SetPptLimit] = code
  check(raises(ipmod.AM_SMU.ResponseError, lambda: s._send_msg(smu.PPSMC_MSG_SetPptLimit, 150), f"PPSMC_Result_{name} ({code:#x})"), f"response {code:#x} -> ResponseError naming PPSMC_Result_{name}")
adev, s = mk(); check(raises(RuntimeError, lambda: s._send_msg(2, 0, debug=True), "debug"), "debug mailbox refused on Vega 20")
adev, s = mk(); adev.fail[smu.PPSMC_MSG_GetSmuVersion] = 0xFE
check(s.is_smu_alive() is True, "is_smu_alive stays True on an error response (C2PMSG_90 != 0), ResponseError suppressed")
adev, s = mk(); adev.pending = [smu.PPSMC_MSG_GetSmuVersion]; check(s.is_smu_alive() is False, "is_smu_alive False when C2PMSG_90 stays 0")
w = [x for x in adev.trace if x[0] == "w" and x[1] in ("mmMP1_SMN_C2PMSG_90", "mmMP1_SMN_C2PMSG_82", "mmMP1_SMN_C2PMSG_66")]
check(w == [("w", "mmMP1_SMN_C2PMSG_90", 0), ("w", "mmMP1_SMN_C2PMSG_82", 0), ("w", "mmMP1_SMN_C2PMSG_66", 2)], f"mailbox write order 90=0, 82=param, 66=msg (vega20_smumgr.c:138-142): {w}")

# (9) enable_dpm_full
adev, s = mk(); pp = bytes(ctypes.sizeof(smu.PPTable_t)); r = s.enable_dpm_full(pp, (1 << 33) | 0xF, 600, [(1, 2, 4)], fclk_gfxclk_ratio=0)
exp = [("NumOfDisplays", 0), ("SetAllowedFeaturesMaskHigh", 2), ("SetAllowedFeaturesMaskLow", 0xF), ("SetMinDeepSleepDcefclk", 600),
       ("SetDriverDramAddrHigh", hi32(drv_mc)), ("SetDriverDramAddrLow", lo32(drv_mc)), ("TransferTableDram2Smu", 0), ("RunBtc", 0), ("RunAfllBtc", 0),
       ("EnableAllSmuFeatures", 0), ("GetEnabledSmuFeaturesLow", 0), ("GetEnabledSmuFeaturesHigh", 0), ("OverridePcieParameters", (1 << 16) | (2 << 8) | 4),
       ("SetUclkFastSwitch", 1), ("SetFclkGfxClkRatio", 0), ("GetPptLimit", 0)]
check(names(adev.msgs) == exp, f"enable_dpm_full replays vega20_enable_dpm_tasks (:1693-1774) in order: {names(adev.msgs)}")
check(r == (adev.features, 999) and adev.smu_tables[0][:len(pp)] == pp, f"enable_dpm_full returns (features, ppt limit) = {r} and uploaded the pptable")
adev, s = mk(features=smu.FEATURE_DPM_GFXCLK_MASK); s.enable_dpm_full(pp, 1, 600, [])
check(("SetUclkFastSwitch", 1) not in names(adev.msgs) and ("OverridePcieParameters", 0) not in names(adev.msgs), "no SetUclkFastSwitch without UCLK DPM (:1009), no pcie override when none given")
adev, s = mk()
check(raises(ValueError, lambda: s.enable_dpm_full(pp[:-1], 1, 600, []), "sizeof(PPTable_t)"), "enable_dpm_full: pptable length must be sizeof(PPTable_t)")
check(raises(ValueError, lambda: s.enable_dpm_full(pp, 1, 0, []), "dcef"), "enable_dpm_full: dcef clock required")
check(raises(ValueError, lambda: s.enable_dpm_full(pp, 1, 600, [(0, 4, 1)]), "gen 0..3"), "enable_dpm_full: pcie gen range")
check(raises(TypeError, lambda: s.enable_dpm_full(pp, 1, 600)), "enable_dpm_full: pcie_overrides has no default")
check(adev.msgs == [], "  ... invalid inputs send nothing")

# (10) message numbers against vega20_ppsmc.h
hdr = LINUX / "pm/powerplay/inc/vega20_ppsmc.h"
if not hdr.is_file(): check(False, f"{hdr} not found (set MI50_LINUX_AMD); message numbers NOT verified against the header")
else:
  text = hdr.read_text()
  hmsgs = {m.group(1): int(m.group(2), 16) for m in re.finditer(r"^#define PPSMC_MSG_(\w+)\s+(0x[0-9A-Fa-f]+)", text, re.M)}
  hres = {m.group(1): int(m.group(2), 16) for m in re.finditer(r"^#define PPSMC_Result_(\w+)\s+(0x[0-9A-Fa-f]+)", text, re.M)}
  src = (FORK / "tinygrad/runtime/support/am/ip.py").read_text(); a, b = src.index("class AM_SMU("), src.index("class AM_GFX(")
  used = set(re.findall(r"PPSMC_MSG_(\w+)", src[a:b]))
  vega_used = {"GetSmuVersion", "SetToolsDramAddrHigh", "SetToolsDramAddrLow", "SetDriverDramAddrHigh", "SetDriverDramAddrLow", "TransferTableSmu2Dram",
               "TransferTableDram2Smu", "GetAVFSVoltageByDpm", "GetEnabledSmuFeaturesLow", "GetEnabledSmuFeaturesHigh", "GetPptLimit", "GetDpmFreqByIndex",
               "SetSoftMinByFreq", "SetSoftMaxByFreq", "SetPptLimit", "NumOfDisplays", "SetAllowedFeaturesMaskHigh", "SetAllowedFeaturesMaskLow",
               "SetMinDeepSleepDcefclk", "RunBtc", "RunAfllBtc", "EnableAllSmuFeatures", "OverridePcieParameters", "SetUclkFastSwitch", "SetFclkGfxClkRatio",
               "OverDriveSetPercentage"}
  check(vega_used <= used, f"every Vega 20 message name appears in the class: missing {vega_used - used}")
  bad = {n: (getattr(smu, 'PPSMC_MSG_' + n, None), hmsgs.get(n)) for n in vega_used if getattr(smu, 'PPSMC_MSG_' + n, None) != hmsgs.get(n)}
  check(not bad, f"all {len(vega_used)} Vega 20 message numbers in smu_11_0_2 == vega20_ppsmc.h: {bad or 'identical'}")
  check({k: getattr(smu, 'PPSMC_Result_' + k) for k in hres} == hres and hres == {"OK": 1, "Failed": 0xFF, "UnknownCmd": 0xFE, "CmdRejectedPrereq": 0xFD, "CmdRejectedBusy": 0xFC}, f"PPSMC_Result_* == header (:30-34): {hres}")
  for n, v in {"SetToolsDramAddrHigh": 0x12, "SetToolsDramAddrLow": 0x13, "SetDriverDramAddrHigh": 0x10, "SetDriverDramAddrLow": 0x11, "TransferTableSmu2Dram": 0x14,
               "TransferTableDram2Smu": 0x15, "SetSoftMinByFreq": 0x26, "SetSoftMaxByFreq": 0x27, "SetPptLimit": 0xF, "GetPptLimit": 0x53, "GetAVFSVoltageByDpm": 0x5F,
               "GetEnabledSmuFeaturesLow": 0xC, "GetEnabledSmuFeaturesHigh": 0xD, "GetSmuVersion": 0x2}.items():
    check(hmsgs[n] == v, f"header PPSMC_MSG_{n} == {v:#x} (task statement)")
  check(smu.TABLE_SMU_METRICS == 6 and smu.TABLE_OVERDRIVE == 9 and smu.POWER_SOURCE_AC == 0, "TABLE_SMU_METRICS 6, TABLE_OVERDRIVE 9, POWER_SOURCE_AC 0")

# (11) sizeof against clang
inc = LINUX / "pm/powerplay/inc"
check(ctypes.sizeof(smu.SmuMetrics_t) == smu.SmuMetrics_t.SIZE == 96 and ctypes.sizeof(smu.OverDriveTable_t) == smu.OverDriveTable_t.SIZE == 32,
      f"module sizeof SmuMetrics_t {ctypes.sizeof(smu.SmuMetrics_t)} / OverDriveTable_t {ctypes.sizeof(smu.OverDriveTable_t)} == Tables-stage clang numbers 96 / 32 (commit d4697a019)")
if not (inc / "smu11_driver_if.h").is_file(): check(False, "C compile skipped: smu11_driver_if.h not found (set MI50_LINUX_AMD)")
else:
  csrc = ('#include <stdint.h>\n#include <stdio.h>\n#define bool _Bool\n#include "vega20_ppsmc.h"\n#include "smu11_driver_if.h"\n'
          'int main(void){printf("%zu %zu %zu\\n", sizeof(SmuMetrics_t), sizeof(OverDriveTable_t), sizeof(PPTable_t)); return 0;}\n')
  with tempfile.TemporaryDirectory() as td:
    pathlib.Path(td, "t.c").write_text(csrc)
    r = subprocess.run(["clang", "-std=c11", f"-I{inc}", f"{td}/t.c", "-o", f"{td}/t"], capture_output=True, text=True)
    out = subprocess.run([f"{td}/t"], capture_output=True, text=True).stdout.split() if r.returncode == 0 else []
  check(r.returncode == 0 and [int(x) for x in out] == [ctypes.sizeof(smu.SmuMetrics_t), ctypes.sizeof(smu.OverDriveTable_t), ctypes.sizeof(smu.PPTable_t)],
        f"clang sizeof(SmuMetrics_t, OverDriveTable_t, PPTable_t) = {out} == module {[ctypes.sizeof(getattr(smu, n)) for n in ('SmuMetrics_t', 'OverDriveTable_t', 'PPTable_t')]}")

# (12) register names the branch asks AMDev for (reg->mm fallback, amdev.py:303-308)
hdp_t, nbio_t, mp_t = (import_module(n, v, submod="regs") for n, v in (("hdp", (4,2,0)), ("nbio", (7,4,0)), ("mp", (11,0,0))))
check("mmHDP_READ_CACHE_INVALIDATE" in hdp_t, "hdp (4,2,0) table has mmHDP_READ_CACHE_INVALIDATE (regHDP_READ_CACHE_INVALIDATE resolves)")
check("mmRCC_CONFIG_MEMSIZE" in nbio_t and "mmBIF_BX0_REMAP_HDP_MEM_FLUSH_CNTL" in nbio_t, "nbio (7,4,0) table has mmRCC_CONFIG_MEMSIZE and mmBIF_BX0_REMAP_HDP_MEM_FLUSH_CNTL (gmc.flush_hdp)")
check(all(f"mmMP1_SMN_C2PMSG_{n}" in mp_t for n in (66, 82, 90)), "mp (11,0,0) table has mmMP1_SMN_C2PMSG_66/82/90")
check(import_module("smu", (11,0,2)) is smu, "import_module('smu', (11,0,2)) -> smu_11_0_2")

# (13) existing cards: old AM_SMU (git mi50) vs new, identical register traces
old_src = subprocess.run(["git", "-C", str(FORK), "show", "mi50:tinygrad/runtime/support/am/ip.py"], capture_output=True, text=True, check=True).stdout
old = types.ModuleType("ip_old"); old.__file__ = "ip_old"; exec(compile(old_src, "ip_old", "exec"), old.__dict__)
import time as _time
_sleep = _time.sleep; _time.sleep = lambda s: None
CARDS = {"gfx11 (RX 7600 XT class)": {am.MP0_HWIP: (13,0,7), am.MP1_HWIP: (13,0,7), am.GC_HWIP: (11,0,2)},
         "MI300 13.0.6": {am.MP0_HWIP: (13,0,6), am.MP1_HWIP: (13,0,6), am.GC_HWIP: (9,4,3)},
         "MI350 13.0.12": {am.MP0_HWIP: (13,0,12), am.MP1_HWIP: (13,0,12), am.GC_HWIP: (9,5,0)},
         "MP1 14.0.2": {am.MP0_HWIP: (14,0,2), am.MP1_HWIP: (14,0,2), am.GC_HWIP: (12,0,0)}}
def drive(cls, ipv):
  adev = FakeAdev(dict(ipv)); s = cls(adev); adev.smu_obj = s; s.init_sw(); adev.is_booting = False
  m = s.smu_mod; adev.levels = {getattr(m, k): [300 + i * 100 for i in range(3)] for k in ("PPCLK_GFXCLK", "PPCLK_UCLK", "PPCLK_SOCCLK", "PPCLK_FCLK") if hasattr(m, k)}
  s.init_hw(); s.is_smu_alive(); s.mode1_reset(); s.read_table(ctypes.c_uint8 * 64, 1); s.set_clocks(0); s.set_clocks(None); s.set_clocks(-1); s.set_power_limit(100)
  s._send_msg(5, 7, read_back_arg=True)
  return adev.trace, adev.mm.allocs
for name, ipv in CARDS.items():
  try: ta, tb = drive(old.AM_SMU, ipv), drive(ipmod.AM_SMU, ipv)
  except Exception as e: check(False, f"{name}: drive failed: {type(e).__name__}: {e}"); continue  # noqa: BLE001
  check(ta == tb and len(ta[0]) > 20, f"{name}: old vs new AM_SMU register traces identical ({len(ta[0])} register ops, allocations {ta[1]})")
_time.sleep = _sleep
# the non-Vega lines of the class are the upstream lines: every statement line of the old class body still exists verbatim in the new one, except the
# one deliberate edit (is_smu_alive also suppresses AM_SMU.ResponseError, which only the (11,0,2) gate raises)
new_src = (FORK / "tinygrad/runtime/support/am/ip.py").read_text(); new_cls = new_src[new_src.index("class AM_SMU("):new_src.index("class AM_GFX(")]
old_cls = old_src[old_src.index("class AM_SMU("):old_src.index("class AM_GFX(")]
allowed = {"    with contextlib.suppress(TimeoutError): self._send_msg(self.smu_mod.PPSMC_MSG_GetSmuVersion, 0, timeout=100)"}
missing = [l for l in old_cls.splitlines() if l.strip() and not l.strip().startswith("#") and l not in new_cls and l not in allowed]
check(not missing, f"every upstream statement line of AM_SMU is still present verbatim (1 listed exception): missing {missing}")
check(all(l in old_cls for l in allowed) and "with contextlib.suppress(TimeoutError, AM_SMU.ResponseError): self._send_msg(self.smu_mod.PPSMC_MSG_GetSmuVersion, 0, timeout=100)" in new_cls,
      "the one edited upstream line is the is_smu_alive suppress list")

print()
print(f"ALL CHECKS PASSED: {len(fails)} failures" if not fails else f"{len(fails)} FAILURES:\n" + "\n".join(fails))
sys.exit(1 if fails else 0)
