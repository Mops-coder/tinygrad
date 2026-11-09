#!/usr/bin/env python3
"""Offline check (no card) of the Vega 20 powerplay bring-up in AM_SMU against Linux's sequence (vega20_start_smu + hwmgr_hw_init).
A hand-built AMDev with the real register tables on a fake BAR5 whose SMU mailbox is scripted, and a SYNTHETIC VBIOS (powerplay, smc_dpm_info v4.4,
firmwareinfo v3.2, an ATOM getsmuclockinfo script). Every number in the image is a test value, not an MI50 fact.
Run: PYTHONPATH=<fork> python3 extra/mi50/t_smu_vega20.py"""
import ctypes, types
from tinygrad.runtime.autogen.am import am, smu_11_0_2 as smu
from tinygrad.runtime.support.am import vbios as v, atom
from tinygrad.runtime.support.am.amdev import AMDev, VEGA20_PCI_ID
import tinygrad.runtime.support.am.ip as ipmod

fails: list[str] = []
def check(cond, msg):
  print(("ok   " if cond else "FAIL ") + msg)
  if not cond: fails.append(msg)

# ---------------- synthetic VBIOS ----------------
HDR, MDT, FWI, PPT, SDI, CMT, CLK = 0x100, 0x200, 0x300, 0x600, 0xD00, 0xE80, 0xF80
# OD8 limits keyed by the OD8_SETTING index Linux reads them with (vega20_hwmgr.h:401-418); vega20_processpptables.c:284-291 copies the
# VBIOS arrays verbatim in ATOM_VEGA20_ODSETTING order, so slot i is read as OD8 setting i. Test values chosen so every OD8 capability is set.
OD_LIMITS = {"GFXCLK_FMIN": (800, 1000), "GFXCLK_FMAX": (1200, 2000), "GFXCLK_FREQ1": (800, 1000), "GFXCLK_VOLTAGE1": (750, 1100),
             "GFXCLK_FREQ2": (1000, 1500), "GFXCLK_VOLTAGE2": (750, 1100), "GFXCLK_FREQ3": (1200, 2000), "GFXCLK_VOLTAGE3": (750, 1150),
             "UCLK_FMAX": (700, 1200), "POWER_PERCENTAGE": (1, 50), "FAN_ACOUSTIC_LIMIT": (500, 3850), "FAN_MIN_SPEED": (800, 3850),
             "FAN_TARGET_TEMP": (25, 105), "OPERATING_TEMP_MAX": (80, 105)}
def image(controller=1, vmin1=750) -> tuple[bytes, smu.PPTable_t]:
  img = bytearray(4096)
  img[0:2] = b"\x55\xaa"; img[2] = len(img) >> 9; img[0x30:0x3a] = v.AMD_VBIOS_SIGNATURE; img[0x48:0x4a] = HDR.to_bytes(2, "little")
  rh = v.atom_rom_header_v2_2(); rh.atom_bios_string[:] = b"ATOM"; rh.masterdatatable_offset = MDT
  img[HDR:HDR + ctypes.sizeof(rh)] = bytes(rh); img[HDR + 0x1E:HDR + 0x20] = CMT.to_bytes(2, "little")
  mdt = v.atom_master_data_table_v2_1(); mdt.table_header.structuresize = ctypes.sizeof(mdt)
  mdt.listOfdatatables.firmwareinfo, mdt.listOfdatatables.powerplayinfo, mdt.listOfdatatables.smc_dpm_info = FWI, PPT, SDI
  img[MDT:MDT + ctypes.sizeof(mdt)] = bytes(mdt)
  f = v.atom_firmware_info_v3_2(); f.table_header.structuresize, f.table_header.format_revision, f.table_header.content_revision = 72, 3, 2
  f.bios_scratch_reg_startaddr, f.bootup_sclk_in10khz, f.bootup_mclk_in10khz, f.bootup_vddc_mv = 0x4c, 85200, 100000, 950
  img[FWI:FWI + 72] = bytes(f)
  pp = v.ATOM_Vega20_POWERPLAYTABLE(); pp.sHeader.structuresize, pp.sHeader.format_revision = ctypes.sizeof(pp), 11
  pp.ucThermalControllerType, pp.ulPlatformCaps, pp.usSoftwareShutdownTemp = controller, 0x5, 118
  od = pp.OverDrive8Table; od.ucODTableRevision, od.ODFeatureCount, od.ODSettingCount = 1, 10, 14
  for i in range(10): od.ODFeatureCapabilities[i] = 1
  for name, (lo_, hi_) in OD_LIMITS.items(): i = ipmod.AM_SMU.OD8.index(name); od.ODSettingsMin[i], od.ODSettingsMax[i] = lo_, hi_
  od.ODSettingsMin[ipmod.AM_SMU.OD8.index("GFXCLK_VOLTAGE1")] = vmin1
  t = pp.smcPPTable; t.Version = 3; t.TedgeLimit, t.ThotspotLimit, t.ThbmLimit = 90, 105, 95
  t.PcieGenSpeed[0], t.PcieGenSpeed[1], t.PcieLaneCount[0], t.PcieLaneCount[1] = 0, 3, 6, 6
  t.MinVoltageGfx, t.MaxVoltageGfx, t.FanMaximumRpm, t.FanPwmMin, t.FanTargetTemperature = 4 * 700, 4 * 1200, 3850, 20, 85
  img[PPT:PPT + ctypes.sizeof(pp)] = bytes(pp)
  d = v.atom_smc_dpm_info_v4_4(); d.table_header.structuresize, d.table_header.format_revision, d.table_header.content_revision = 308, 4, 4
  d.maxvoltagestepgfx, d.gfxmaxcurrent, d.uclkspreadenabled, d.fllgfxclkspreadfreq = 4, 300, 1, 777
  d.i2ccontrollers[0].enabled, d.i2ccontrollers[0].slaveaddress, d.i2ccontrollers[6].i2cspeed = 1, 0x20, 3
  img[SDI:SDI + 308] = bytes(d)
  img[CMT + 4 + 2 * 41:CMT + 6 + 2 * 41] = CLK.to_bytes(2, "little")   # getsmuclockinfo: PS[0] = 100 MHz in Hz, then EOT
  code = bytes([2, 0x05, 0x00]) + (100_000_000).to_bytes(4, "little") + bytes([91])
  img[CLK:CLK + 6 + len(code)] = (6 + len(code)).to_bytes(2, "little") + bytes([1, 1, 0, 0]) + code
  exp = smu.PPTable_t.from_buffer_copy(bytes(t))   # what Linux uploads: smcPPTable + append_vbios_pptable + fan target override
  exp.MaxVoltageStepGfx, exp.GfxMaxCurrent, exp.UclkSpreadEnabled, exp.FllGfxclkSpreadFreq, exp.FanTargetTemperature = 4, 300, 0, 777, 105
  exp.I2cControllers[0].Enabled, exp.I2cControllers[0].SlaveAddress, exp.I2cControllers[6].I2cSpeed = 1, 0x20, 3
  return bytes(img), exp

# ---------------- fake card ----------------
M = {v_: k[10:] for k, v_ in vars(smu).items() if k.startswith("PPSMC_MSG_")}
LEVELS = {smu.PPCLK_GFXCLK: [800, 1200, 1500, 1725], smu.PPCLK_UCLK: [350, 800, 1000], smu.PPCLK_SOCCLK: [310, 600, 970],
          smu.PPCLK_FCLK: [550, 1100], smu.PPCLK_DCEFCLK: [310, 600], smu.PPCLK_DISPCLK: [400], smu.PPCLK_PIXCLK: [400], smu.PPCLK_PHYCLK: [300],
          smu.PPCLK_ECLK: [300], smu.PPCLK_VCLK: [300], smu.PPCLK_DCLK: [300]}
class FakeMMIO:
  def __init__(self, dev, fw_flags=1): self.dev, self.regs, self.msgs, self.allowed, self.fw_flags, self.addr, self.tables = dev, {}, [], [0, 0], fw_flags, [0, 0], {}
  def __len__(self): return 0x20000
  def __getitem__(self, k):
    if k == self.dev.regBIF_BX0_PCIE_DATA2.addr[0]: return self.fw_flags if self.regs.get(self.dev.regBIF_BX0_PCIE_INDEX2.addr[0]) == 0x3b10024 else 0
    return self.regs.get(k, 0)
  def __setitem__(self, k, val):
    self.regs[k] = val
    if k != self.dev.mmMP1_SMN_C2PMSG_66.addr[0]: return
    p, name, arg = self.regs.get(self.dev.mmMP1_SMN_C2PMSG_82.addr[0], 0), M[val], 0
    self.msgs.append((name, p))
    if name == "GetSmuVersion": arg = 0x283c00
    elif name in ("ReadSerialNumTop32", "ReadSerialNumBottom32"): arg = 0x1234 if name.endswith("Top32") else 0x5678
    elif name == "SetAllowedFeaturesMaskHigh": self.allowed[1] = p
    elif name == "SetAllowedFeaturesMaskLow": self.allowed[0] = p
    elif name == "GetEnabledSmuFeaturesLow": arg = self.allowed[0]
    elif name == "GetEnabledSmuFeaturesHigh": arg = self.allowed[1]
    elif name == "SetDriverDramAddrHigh": self.addr[1] = p
    elif name == "SetDriverDramAddrLow": self.addr[0] = p
    elif name == "GetDpmFreqByIndex": lv = LEVELS[p >> 16]; arg = len(lv) if (p & 0xffff) == 0xff else lv[p & 0xffff]
    elif name == "GetDcModeMaxDpmFreq": arg = 0 if (p >> 16) == smu.PPCLK_UCLK else LEVELS[p >> 16][-1]
    elif name == "GetMaxDpmFreq": arg = LEVELS[p >> 16][-1]
    elif name == "GetAVFSVoltageByDpm": arg = 4 * (700 + (p & 0xffff) // 10)
    elif name == "GetPptLimit": arg = 300
    elif name in ("TransferTableSmu2Dram", "TransferTableDram2Smu"):
      paddr = ((self.addr[1] << 32) | self.addr[0]) - MC_BASE
      if name == "TransferTableDram2Smu": self.tables[p & 0xffff] = bytes(self.dev.vram.view(paddr, 0x2000))
      elif (p & 0xffff) == smu.TABLE_OVERDRIVE:
        o = smu.OverDriveTable_t(); o.GfxclkFmin, o.GfxclkFmax, o.UclkFmax, o.OverDrivePct, o.FanMaximumRpm = 800, 1725, 1000, 0, 3850
        self.dev.vram.view(paddr, ctypes.sizeof(o))[:] = bytes(o)
    self.regs[self.dev.mmMP1_SMN_C2PMSG_82.addr[0]] = arg
    self.regs[self.dev.mmMP1_SMN_C2PMSG_90.addr[0]] = smu.PPSMC_Result_OK

MC_BASE = 1 << 32
class FakeVram:
  def __init__(self): self.buf = bytearray(1 << 21)
  def view(self, paddr, size, fmt=None): return memoryview(self.buf)[paddr:paddr + size]
class FakeMM:
  def __init__(self): self.next = 0x1000
  def palloc(self, size, align=0x1000, zero=True, boot=False):
    p = self.next; self.next += (size + 0xfff) & ~0xfff; return p

def mk(fw_flags=1, controller=1, vmin1=750):
  img, exp = image(controller, vmin1)
  dev = AMDev.__new__(AMDev); dev.devfmt, dev.is_vega20 = "fake", True
  dev._static_vega20(); dev._build_regs()
  dev.mmio, dev.vram, dev.mm = FakeMMIO(dev, fw_flags), FakeVram(), FakeMM()
  dev.paddr2mc = lambda p: p + MC_BASE
  dev.gmc = types.SimpleNamespace(flush_hdp=lambda: None)
  dev.vbios, dev.bios_scratch_reg_offset = v.VBIOS(img), 0x4c
  dev.atom = atom.AtomContext(atom.CardInfo(dev.rreg, dev.wreg), img, 0)
  dev._vega20_pcie_platform_caps = lambda: (0x70000, 0x70000)   # platform Gen3 x4 (AMDev._pcie_caps_masks of a Gen3 x4 partner)
  dev.is_booting = True; s = ipmod.AM_SMU(dev); s.init_sw(); dev.is_booting = False
  return dev, s, exp

# ---------------- (1) the full Vega 20 SMU init ----------------
dev, s, exp = mk()
thm_ctrl, thm_ena = dev.regTHM_THERMAL_INT_CTRL.addr[0], dev.regTHM_THERMAL_INT_ENA.addr[0]
dev.mmio.regs[thm_ctrl] = 0x0FAB0000   # pre-set the four mask bits + temp_threshold 0xAB (bits 16-23) so the read-modify-write is visible
s.init_hw()
msgs = dev.mmio.msgs; names = [n for n, _ in msgs]
tools = s.vega20_tables[smu.TABLE_PMSTATUSLOG][0] + MC_BASE
check(msgs[:2] == [("SetToolsDramAddrHigh", tools >> 32), ("SetToolsDramAddrLow", tools & 0xffffffff)],
      "vega20_start_smu: SMC running (MP1_FIRMWARE_FLAGS bit 0), SetToolsDramAddrHigh/Low = PMSTATUSLOG buffer")
check(names[2:5] == ["GetSmuVersion", "ReadSerialNumTop32", "ReadSerialNumBottom32"], f"backend_init: GetSmuVersion, ReadSerialNum Top/Bottom: {names[2:5]}")
check(s.vega20_unique_id == (0x5678 << 32) | 0x1234, "unique_id = bottom32 << 32 | top32 (vega20_hwmgr.c:407)")
GN = ipmod.AM_SMU.VEGA20_GNLD
lo = hi = 0
for i, n in enumerate(GN):
  if not (0xE0041C00 >> i) & 1:
    b = getattr(smu, f"FEATURE_{n}_BIT"); hi, lo = (hi | (1 << (b - 32)), lo) if b > 31 else (hi, lo | (1 << b))
check(msgs[5:8] == [("NumOfDisplays", 0), ("SetAllowedFeaturesMaskHigh", hi), ("SetAllowedFeaturesMaskLow", lo)],
      f"enable_dpm_tasks: NumOfDisplays 0, allowed mask High {hi:#x} / Low {lo:#x} (0xE0041C00 disallowed by GNLD index): {msgs[5:8]}")
check(msgs[8] == ("SetMinDeepSleepDcefclk", 1_0000 // 100), f"SetMinDeepSleepDcefclk = getsmuclockinfo DCEFCLK (10 kHz units) / 100: {msgs[8]}")
check(s.vega20_boot["soc"] == s.vega20_boot["dcef"] == s.vega20_boot["f"] == 100_000_000 // 10000, "boot SOC/DCEF/FCLK from the ATOM getsmuclockinfo table (Hz / 10000)")
check(dev.mmio.tables.get(smu.TABLE_PPTABLE, b"")[:ctypes.sizeof(smu.PPTable_t)] == bytes(exp),
      "PPTable upload = VBIOS smcPPTable + smc_dpm_info v4.4 fields + UclkSpreadEnabled 0 + FanTargetTemperature 105")
order = ["TransferTableDram2Smu", "RunBtc", "RunAfllBtc", "EnableAllSmuFeatures", "GetEnabledSmuFeaturesLow", "GetEnabledSmuFeaturesHigh",
         "OverridePcieParameters", "SetUclkFastSwitch", "SetFclkGfxClkRatio", "GetDpmFreqByIndex", "GetDcModeMaxDpmFreq", "TransferTableSmu2Dram",
         "GetAVFSVoltageByDpm", "GetPptLimit", "SetFanTemperatureTarget"]
first = [names.index(n) for n in order]
check(first == sorted(first), f"vega20_enable_dpm_tasks / start_thermal_controller order: {[names[i] for i in sorted(first)]}")
check([p for n, p in msgs if n == "OverridePcieParameters"] == [0x00003, 0x10203],
      "OverridePcieParameters: level 0 width 6->3, level 1 gen 3->2 width 6->3 (platform Gen3 x4), Linux (i<<16)|(gen<<8)|width")
check(("SetUclkFastSwitch", 1) in msgs and ("SetFclkGfxClkRatio", 0) in msgs, "SetUclkFastSwitch 1 (UCLK DPM on), SetFclkGfxClkRatio 0")
check(s.vega20_dpm["gfx"] == LEVELS[smu.PPCLK_GFXCLK] and s.vega20_dpm["mem"] == LEVELS[smu.PPCLK_UCLK], "default DPM tables read from the SMU")
check(s.vega20_max_sustainable["uclock"] == min(LEVELS[smu.PPCLK_UCLK][-1], LEVELS[smu.PPCLK_SOCCLK][-1]), "UCLK DC max 0 -> GetMaxDpmFreq, then min(soc, uclk)")
check(("GetPptLimit", 0) in msgs and s.vega20_default_power_limit == 300, "GetPptLimit(POWER_SOURCE_AC << 16) -> default power limit")
check(s.vega20_od8_caps == 0x1ff and s.vega20_tdp_od_limit == 50,
      f"OD8 capabilities: all but FAN_ZERO_RPM_CONTROL (FanZeroRpmEnable 0) = 0x1ff, TDP OD limit = POWERPERCENTAGE max: {s.vega20_od8_caps:#x}")
od_up = smu.OverDriveTable_t.from_buffer_copy(dev.mmio.tables[smu.TABLE_OVERDRIVE][:ctypes.sizeof(smu.OverDriveTable_t)])
check((od_up.GfxclkFreq1, od_up.GfxclkFreq3, od_up.GfxclkFreq2) == (800, 1725, (800 + 1725) // 2) and od_up.GfxclkVolt1 == 4 * (700 + 800 // 10),
      "OD8 defaults: curve Freq1/3 = Fmin/Fmax, Freq2 = mean, Volt = GetAVFSVoltageByDpm // 4 * 4, written back")
ctrl = dev.mmio.regs[thm_ctrl]; f = dev.regTHM_THERMAL_INT_CTRL.decode(ctrl)
check((f["max_ih_credit"], f["therm_ih_hw_ena"], f["dig_therm_inth"], f["dig_therm_intl"], f["therm_inth_mask"], f["therm_intl_mask"],
       f["therm_trigger_mask"], f["temp_threshold"]) == (5, 1, 118, 0, 0, 0, 0, 0xAB) and f["therm_prochot_mask"] == 1,
      f"THM_THERMAL_INT_CTRL: credit 5, IH on, high = min(255, shutdown 118), low 0, alert masks cleared, rest kept: {f}")
check(dev.mmio.regs[thm_ena] == dev.regTHM_THERMAL_INT_ENA.encode(therm_inth_clr=1, therm_intl_clr=1, therm_trigger_clr=1), "THM_THERMAL_INT_ENA = the three CLR bits")
check(msgs[-1] == ("SetFanTemperatureTarget", 105), f"last message SetFanTemperatureTarget 105 (the overridden fan target): {msgs[-1]}")

# ---------------- (2) power limit / clocks after init ----------------
dev.mmio.msgs.clear(); s.set_power_limit(250)
check(dev.mmio.msgs == [("SetPptLimit", 250)], "set_power_limit(250): PPT enabled -> SetPptLimit 250 (vega20_set_power_limit)")
s._vega20_feature("PPT")["enabled"] = False; dev.mmio.msgs.clear(); s.set_power_limit(250)
check(dev.mmio.msgs == [], "PPT feature off -> no message, no error (vega20_powertune.c:37-42)")
dev.mmio.msgs.clear(); s.set_clocks(None)
lo_ = [(smu.PPCLK_GFXCLK << 16) | 800, (smu.PPCLK_UCLK << 16) | 350, (smu.PPCLK_SOCCLK << 16) | 310]
hi_ = [(smu.PPCLK_GFXCLK << 16) | 1725, (smu.PPCLK_UCLK << 16) | 1000, (smu.PPCLK_SOCCLK << 16) | 970]
check(dev.mmio.msgs == [("SetSoftMinByFreq", x) for x in lo_] + [("SetSoftMaxByFreq", x) for x in hi_], "set_clocks(None) = vega20_unforce_dpm_levels")
dev.mmio.msgs.clear(); s.set_clocks(0)
check([p for _, p in dev.mmio.msgs] == lo_ + lo_, "set_clocks(0) = vega20_force_dpm_lowest (min and max = lowest level)")

# ---------------- (3) failure paths ----------------
dev2, s2, _ = mk(fw_flags=0)
try: s2.init_hw(); ok = False
except RuntimeError as e: ok = "SMC is not running" in str(e)
check(ok and dev2.mmio.msgs == [], "MP1_FIRMWARE_FLAGS interrupts bit clear -> [Vega20StartSmu] SMC is not running, no message sent")
dev3, s3, _ = mk(controller=0); s3.init_hw()
check("SetFanTemperatureTarget" not in [n for n, _ in dev3.mmio.msgs] and dev3.mmio.regs.get(dev3.regTHM_THERMAL_INT_ENA.addr[0], 0) == 0,
      "no thermal controller in the pptable -> phm_start_thermal_controller does nothing")

dev4, s4, _ = mk(vmin1=4); s4.init_hw()
check(not s4.vega20_od8_caps & s4.OD8_CAP["GFXCLK_CURVE"] and "GetAVFSVoltageByDpm" not in [n for n, _ in dev4.mmio.msgs],
      "OD voltage1 min 4 < MinVoltageGfx/4 (700) -> no GFXCLK_CURVE capability, no AVFS voltage query (vega20_hwmgr.c:1062-1071)")

print(f"\n{len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
