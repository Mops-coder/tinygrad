#!/usr/bin/env python3
"""Offline check (no card) for the mi50 fork's amdev.py Vega 20 branches and runtime/support/am/vbios.py (design v2 §3 amdev.py rows; B3, B4, B7, B16).

(1) a synthetic ATOM image built from the ported header structs (NO VBIOS dump exists yet - every number in it is a SYNTHETIC test value, not an
    MI50 fact) -> VBIOS walker: signature/length/ATOM checks, master data table walk, firmwareinfo v3.2, gfx_info v2.4, vram_usagebyfirmware v2.1,
    powerplayinfo + OverDrive8 limits; (2) read_rom through fake ROM_INDEX/ROM_DATA registers at the design dwords 0x168e4/0x168e5 (auto-increment);
(3) gc_info v2.0 shaping (every field lands in the slot Linux pairs it with); (4) AMDev._static_vega20 + _build_regs on a fake PCI device:
    ip_ver / regs_offset == egpu/mi50/port/vega20_ip_table.py (+ the NBIF alias), register dwords == the design's numbers, aids, harvested,
    non-Vega gating (gfx11 fake binds no thm/smuio/athub/sdma tables, boot_version == AMDev.Version); (5) _vega20_vbios: POST gate, scratch-base
    check, reserved_vram_size from the table / required env / overlap refusal, required inputs, invalid thermal reading, threshold;
(6) _emergency_quiesce: MEC mask + SDMA halts + SMU SetSoftMaxByFreq/SetPptLimit through a fake mailbox, and the __init__ wrapper (injected error);
(7) clang sizeof/offsetof of every ported struct against the Linux headers.
Run: PYTHONPATH=<fork> python3 extra/mi50/t_amdev_vbios.py   [env MI50_LINUX_AMD=<linux>/drivers/gpu/drm/amd, MI50_IP_TABLE=<vega20_ip_table.py>]
"""
import os, sys, ctypes, pathlib, subprocess, tempfile, importlib.util, types
from tinygrad.runtime.autogen.am import am, smu_11_0_2 as smu
from tinygrad.runtime.support.am import vbios as v
from tinygrad.runtime.support.am.amdev import AMDev, VEGA20_PCI_ID

LINUX = pathlib.Path(os.getenv("MI50_LINUX_AMD", "/private/tmp/claude-501/-Users-leonh-Documents-Ai-shit/bba39989-2505-4a2d-a9f5-db421196e2ba/"
                                                 "scratchpad/repos/linux/drivers/gpu/drm/amd"))
IP_TABLE = pathlib.Path(os.getenv("MI50_IP_TABLE", "/Users/leonh/Documents/Ai shit/egpu/mi50/port/vega20_ip_table.py"))
fails: list[str] = []
def check(cond, msg):
  print(("ok   " if cond else "FAIL ") + msg)
  if not cond: fails.append(msg)
def raises(fn, needle:str, msg:str):
  try: fn()
  except Exception as e: check(needle in str(e), f"{msg}: raised {type(e).__name__}: {str(e)[:110]}")
  else: check(False, f"{msg}: did not raise")

# ---------------- (1) synthetic image ----------------
HDR, MDT, FWI, GFX, VRU, PPT = 0x100, 0x200, 0x300, 0x400, 0x500, 0x600
SYN = dict(se=4, cu_per_sh=16, sh_per_se=1, rb=4, tccs=16, gprs=256, gs_thds=32, gs_depth=32, gsprim=1792, dbl_lds=1, wave=64, waves_simd=10,
           scratch_slots=32, lds=64, sclk10k=85200, mclk10k=100000, fw_start_kb=(32768 << 10) - (8 << 10), fw_kb=8192, shutdown_temp=118)
def synthetic_image(scratch_base=0x4c, posted_fw_region=True, vru_version=(2, 1), gfx_crev=4, pptable_version=3, fw_start_kb=None) -> bytes:
  img = bytearray(4096)
  img[0:2] = b"\x55\xaa"; img[2] = len(img) >> 9                                               # amdgpu_bios.c:45-46
  img[0x30:0x3a] = v.AMD_VBIOS_SIGNATURE                                                       # :41-42
  img[0x48:0x4a] = HDR.to_bytes(2, 'little')                                                   # atom.h:35
  rh = v.atom_rom_header_v2_2(); rh.atom_bios_string[:] = b"ATOM"; rh.masterdatatable_offset = MDT
  img[HDR:HDR+ctypes.sizeof(rh)] = bytes(rh)
  mdt = v.atom_master_data_table_v2_1(); mdt.table_header.structuresize = ctypes.sizeof(mdt); mdt.table_header.format_revision = 2
  mdt.listOfdatatables.firmwareinfo, mdt.listOfdatatables.gfx_info, mdt.listOfdatatables.powerplayinfo = FWI, GFX, PPT
  if vru_version: mdt.listOfdatatables.vram_usagebyfirmware = VRU
  img[MDT:MDT+ctypes.sizeof(mdt)] = bytes(mdt)
  fwi = v.atom_firmware_info_v3_2(); fwi.table_header.structuresize, fwi.table_header.format_revision, fwi.table_header.content_revision = 72, 3, 2
  fwi.bios_scratch_reg_startaddr, fwi.bootup_sclk_in10khz, fwi.bootup_mclk_in10khz, fwi.bootup_vddc_mv = scratch_base, SYN["sclk10k"], SYN["mclk10k"], 950
  img[FWI:FWI+72] = bytes(fwi)
  g = v.atom_gfx_info_v2_4(); g.table_header.structuresize, g.table_header.format_revision, g.table_header.content_revision = 72, 2, gfx_crev
  g.max_shader_engines, g.max_cu_per_sh, g.max_sh_per_se, g.max_backends_per_se, g.max_texture_channel_caches = SYN["se"], SYN["cu_per_sh"], SYN["sh_per_se"], SYN["rb"], SYN["tccs"]
  g.gc_num_gprs, g.gc_num_max_gs_thds, g.gc_gs_table_depth, g.gc_gsprim_buff_depth, g.gc_double_offchip_lds_buffer = SYN["gprs"], SYN["gs_thds"], SYN["gs_depth"], SYN["gsprim"], SYN["dbl_lds"]
  g.gc_wave_size, g.gc_max_waves_per_simd, g.gc_max_scratch_slots_per_cu, g.gc_lds_size, g.gc_parameter_cache_depth = SYN["wave"], SYN["waves_simd"], SYN["scratch_slots"], SYN["lds"], 1023
  img[GFX:GFX+72] = bytes(g)
  if vru_version == (2, 1):
    u = v.vram_usagebyfirmware_v2_1(); u.table_header.structuresize, u.table_header.format_revision, u.table_header.content_revision = 12, 2, 1
    u.start_address_in_kb = (SYN["fw_start_kb"] if fw_start_kb is None else fw_start_kb) | 0x80000000   # a flag bit, masked off by the reader (atombios.h:4298)
    u.used_by_firmware_in_kb, u.used_by_driver_in_kb = (SYN["fw_kb"] if posted_fw_region else 0), 256
    img[VRU:VRU+12] = bytes(u)
  elif vru_version:
    u2 = v.vram_usagebyfirmware_v2_2(); u2.table_header.structuresize, u2.table_header.format_revision, u2.table_header.content_revision = 48, 2, 2
    u2.fw_region_start_address_in_kb, u2.used_by_firmware_in_kb = SYN["fw_start_kb"], SYN["fw_kb"]
    img[VRU:VRU+48] = bytes(u2)
  pp = v.ATOM_Vega20_POWERPLAYTABLE(); pp.sHeader.structuresize, pp.sHeader.format_revision = ctypes.sizeof(pp), 11
  pp.usSmallPowerLimit1, pp.usSmallPowerLimit2, pp.usBoostPowerLimit, pp.usODTurboPowerLimit, pp.usODPowerSavePowerLimit = 300, 250, 320, 330, 200
  pp.usSoftwareShutdownTemp = SYN["shutdown_temp"]
  od = pp.OverDrive8Table; od.ucODTableRevision, od.ODFeatureCount, od.ODSettingCount = 1, 10, 14
  for i in range(14): od.ODSettingsMin[i], od.ODSettingsMax[i] = 1000 + i, 2000 + i
  for i in range(10): od.ODFeatureCapabilities[i] = i & 1
  pp.smcPPTable.Version = pptable_version
  img[PPT:PPT+ctypes.sizeof(pp)] = bytes(pp)
  return bytes(img)

img = synthetic_image()
bios = v.VBIOS(img)
check(bios.data_table == MDT and bios.rom_header.masterdatatable_offset == MDT, "walker: data_table = CU16(hdr+0x20) = rom_header.masterdatatable_offset")
check(bios.data_header("firmwareinfo") == (FWI, 72, 3, 2), f"walker: firmwareinfo entry {bios.data_header('firmwareinfo')}")
check(bios.data_header("gfx_info") == (GFX, 72, 2, 4) and bios.data_header("powerplayinfo")[0] == PPT, "walker: gfx_info / powerplayinfo entries")
check(bios.data_header("vram_info") is None and bios.data_header("umc_info") is None, "walker: absent tables -> None (atom.c:1691-1692)")
check((v.master_index("firmwareinfo"), v.master_index("vram_usagebyfirmware"), v.master_index("gfx_info"), v.master_index("powerplayinfo")) == (4, 11, 14, 15),
      "walker: master indices firmwareinfo 4, vram_usagebyfirmware 11, gfx_info 14, powerplayinfo 15 (atomfirmware.h:395,402,405,406)")
fwi = bios.firmwareinfo()
check(isinstance(fwi, v.atom_firmware_info_v3_2) and fwi.bios_scratch_reg_startaddr == 0x4c and fwi.bootup_sclk_in10khz == SYN["sclk10k"], "firmwareinfo v3.2 fields")
check(bios.fw_vram_region() == (SYN["fw_start_kb"] << 10, SYN["fw_kb"] << 10), f"vram_usagebyfirmware v2.1 region (flag bits masked): {bios.fw_vram_region()}")
check(v.VBIOS(synthetic_image(vru_version=(2, 2))).fw_vram_region() == (SYN["fw_start_kb"] << 10, SYN["fw_kb"] << 10), "vram_usagebyfirmware v2.2 region")
check(v.VBIOS(synthetic_image(vru_version=None)).fw_vram_region() is None and v.VBIOS(synthetic_image(posted_fw_region=False)).fw_vram_region() is None,
      "vram_usagebyfirmware absent / zero size -> None")
lim = bios.powerplay_limits()
check(lim["SoftwareShutdownTemp"] == SYN["shutdown_temp"] and lim["SmallPowerLimit1"] == 300 and lim["tdp_od_limit"] == 2009, f"powerplay limits {dict((k, lim[k]) for k in lim if k != 'smc_pptable')}")
check(lim["od8"]["settings"]["GFXCLKFMIN"] == (1001, 2001) and lim["od8"]["settings"]["OPERATINGTEMPMAX"] == (1013, 2013) and len(lim["od8"]["settings"]) == 14,
      "OverDrive8 settings min/max in enum order (vega20_pptable.h:57-73), clamped to 14")
check(lim["od8"]["features"]["GFXCLK_CURVE"] == 1 and lim["od8"]["features"]["GFXCLK_LIMITS"] == 0 and len(lim["smc_pptable"]) == 1256, "OverDrive8 feature caps; smc_pptable blob = 1256 B")
raises(lambda: v.VBIOS(synthetic_image(pptable_version=2)).powerplay(), "Unmatch PPTable version", "powerplay: smcPPTable.Version != 3 refused (vega20_processpptables.c:82-88)")
raises(lambda: v.VBIOS(synthetic_image(gfx_crev=7)).gfx_info(), "only v2.4", "gfx_info v2.7 refused with its version")
raises(lambda: v.VBIOS(b"\x00" * 4096), "0x55 0xaa", "image without 55 AA refused")
raises(lambda: v.VBIOS(img[:0x4a] + b"\x00" * 100), "past the image", "ATOM header pointer past the image refused")
bad = bytearray(img); bad[HDR+4:HDR+8] = b"XXXX"; raises(lambda: v.VBIOS(bytes(bad)), "ATOM/MOTA", "no ATOM/MOTA magic refused")

# ---------------- (2) read_rom through fake registers ----------------
class FakeMMIO:
  """dword-indexed BAR5 (512 KiB = 0x20000 dwords, design §1.3); ROM_DATA auto-increments from the pointer ROM_INDEX sets; SMU mailbox optional."""
  ROM_INDEX, ROM_DATA, C2PMSG_66, C2PMSG_82, C2PMSG_90 = 0x168e4, 0x168e5, 0x16000 + 642, 0x16000 + 658, 0x16000 + 666
  FEATURES_LOW = (1 << smu.FEATURE_DPM_GFXCLK_BIT) | (1 << smu.FEATURE_DPM_UCLK_BIT) | (1 << smu.FEATURE_DPM_SOCCLK_BIT) | (1 << smu.FEATURE_PPT_BIT)
  def __init__(self, img:bytes, smu_alive=False, seed=None):
    self.img, self.ptr, self.smu_alive, self.regs, self.log = img, 0, smu_alive, dict(seed or {}), []
  def __len__(self): return 0x20000
  def __getitem__(self, k):
    if k == self.ROM_DATA: val, self.ptr = int.from_bytes(self.img[self.ptr:self.ptr+4].ljust(4, b"\0"), 'little'), self.ptr + 4; return val
    return self.regs.get(k, 0)
  def __setitem__(self, k, val):
    self.log.append((k, val))
    if k == self.ROM_INDEX: self.ptr = val
    elif k == self.C2PMSG_66 and self.smu_alive:   # fake SMU: GetDpmFreqByIndex count 8 / level freq 800 (synthetic), every message answered OK
      param = self.regs.get(self.C2PMSG_82, 0)
      if val == smu.PPSMC_MSG_GetDpmFreqByIndex: self.regs[self.C2PMSG_82] = 8 if (param & 0xff) == 0xff else 800
      elif val == smu.PPSMC_MSG_GetEnabledSmuFeaturesLow: self.regs[self.C2PMSG_82] = self.FEATURES_LOW   # synthetic feature state
      else: self.regs[self.C2PMSG_82] = 0
      self.regs[self.C2PMSG_90] = smu.PPSMC_Result_OK
    else: self.regs[k] = val
mm = FakeMMIO(img)
dump = v.read_rom(lambda r: mm[r], lambda r, val: mm.__setitem__(r, val), FakeMMIO.ROM_INDEX, FakeMMIO.ROM_DATA)
check(dump == img and mm.log[0] == (FakeMMIO.ROM_INDEX, 0) and mm.log.count((FakeMMIO.ROM_INDEX, 0)) == 2, "read_rom: index write 0, header read, full image read == image")
raises(lambda: v.read_rom(lambda r: 0xffffffff, lambda r, val: None, 1, 2), "no AMD ROM", "read_rom: 0xffffffff bus refused")

# ---------------- (3) gc_info shaping ----------------
gc = bios.gc_info_v2_0()
check(gc.header.version_major == 2 and gc.header.version_minor == 0 and gc.header.table_id == am.GC and gc.header.size == 80, "gc_info header v2.0, table GC, size 80")
pairs = {"gc_num_se": SYN["se"], "gc_num_cu_per_sh": SYN["cu_per_sh"], "gc_num_sh_per_se": SYN["sh_per_se"], "gc_num_rb_per_se": SYN["rb"], "gc_num_tccs": SYN["tccs"],
  "gc_num_gprs": SYN["gprs"], "gc_num_max_gs_thds": SYN["gs_thds"], "gc_gs_table_depth": SYN["gs_depth"], "gc_gsprim_buff_depth": SYN["gsprim"],
  "gc_double_offchip_lds_buffer": SYN["dbl_lds"], "gc_wave_size": SYN["wave"], "gc_max_waves_per_simd": SYN["waves_simd"],
  "gc_max_scratch_slots_per_cu": SYN["scratch_slots"], "gc_lds_size": SYN["lds"], "gc_parameter_cache_depth": 0, "gc_num_sc_per_se": 0, "gc_num_packer_per_sc": 0}
for k, val in pairs.items(): check(getattr(gc, k) == val, f"gc_info.{k} == {val}")
check(bytes(gc)[12:16] == SYN["se"].to_bytes(4, 'little'), "gc_info bytes: gc_num_se at offset 12 (am.py struct_gc_info_v2_0 layout)")

# ---------------- (4)/(5) AMDev on a fake PCI device ----------------
class FakePCI:
  def __init__(self, dev_id=VEGA20_PCI_ID, mmio=None): self.pcibus, self.dev_id, self.mmio_obj, self.cfg_writes = "fake", dev_id, mmio, []
  def read_config(self, off, size): return {(0, 4): self.dev_id, (0, 2): self.dev_id & 0xffff, (4, 2): 0x7}.get((off, size), 0)
  def write_config_flush(self, off, val, size): self.cfg_writes.append((off, val, size))
  def map_bar(self, bar, fmt='B'): return self.mmio_obj if bar == 5 else types.SimpleNamespace(nbytes=256 << 20)
def seed(temp=60, maxtemp=70, posted=True, memsize_mib=32768): return {0xde3: memsize_mib, 0x53: 0x200 if posted else 0, 0x1665f: (temp << 9) | maxtemp}
def fake_dev(mmio, inputs=True) -> AMDev:
  """The hand-built AMDev mirrors __init__ (review 2026-10-13 TI4): is_vega20 from config dword 0, then the two REQUIRED inputs."""
  dev = AMDev.__new__(AMDev)
  dev.pci_dev, dev.devfmt, dev.mmio, dev.vram = FakePCI(mmio=mmio), "fake", mmio, types.SimpleNamespace(nbytes=256 << 20)
  dev.is_vega20 = dev.pci_dev.read_config(0, 4) == VEGA20_PCI_ID
  if dev.is_vega20 and inputs: dev._vega20_required_inputs()
  return dev
os.environ.update({"AM_MI50_PPT_W": "150", "AM_MI50_TEMP_C": "90"}); os.environ.pop("AM_MI50_RESERVED_VRAM_MB", None)
mm = FakeMMIO(img, seed=seed()); dev = fake_dev(mm)
dev._run_discovery(); dev._build_regs()
check(dev.is_vega20 and dev.vram_size == 32768 << 20 and not dev.large_bar, "fake MI50: _run_discovery took the static branch; vram 32 GiB, small BAR")
spec = importlib.util.spec_from_file_location("vega20_ip_table", IP_TABLE); tbl = importlib.util.module_from_spec(spec); spec.loader.exec_module(tbl)
exp_ipver = {getattr(am, k): val for k, val in tbl.IP_VER.items()} | {am.NBIF_HWIP: (7, 4, 0)}
exp_regs = {getattr(am, k): val for k, val in tbl.REGS_OFFSET.items()} | {am.NBIF_HWIP: tbl.REGS_OFFSET["NBIO_HWIP"]}
check(dev.ip_ver == exp_ipver, f"ip_ver == {IP_TABLE.name} IP_VER (17 entries, amdgpu_discovery.c:3207-3224) + NBIF (7,4,0)")
check(dict(dev.regs_offset) == exp_regs, f"regs_offset == {IP_TABLE.name} REGS_OFFSET (19 blocks + SDMA0 inst 1) + NBIF = NBIO bases")
check(dev.regs_offset[am.SDMA0_HWIP] == {0: (0x1260, 0, 0, 0, 0, 0), 1: (0x1860, 0, 0, 0, 0, 0)}, "regs_offset[SDMA0_HWIP] = {0: SDMA0, 1: SDMA1} bases")
check(dev.aids == [0] and isinstance(dev.harvested, __import__("collections").defaultdict) and dev.harvested[am.GC_HWIP] == set(), "aids [0]; harvested defaultdict(set)")
dw = {"regBIOS_SCRATCH_7": 0x53, "regBIOS_SCRATCH_0": 0x4c, "regROM_INDEX": 0x168e4, "regROM_DATA": 0x168e5, "regCG_MULT_THERMAL_STATUS": 0x1665f,
      "regRCC_CONFIG_MEMSIZE": 0xde3, "regCP_MEC_CNTL": 0x208d, "regCP_ME_CNTL": 0x2000 + 438, "regSCRATCH_REG7": 0xa000 + 0x2047, "regSCRATCH_REG6": 0xa000 + 0x2046,
      "regCP_STAT": 0x2000 + 0x1a0, "mmMP1_SMN_C2PMSG_90": 0x16000 + 666, "mmMP0_SMN_C2PMSG_64": 0x16000 + 128, "regRPB_ARB_CNTL": 0xc20 + dev.regRPB_ARB_CNTL.offset}
for name, addr in dw.items(): check(dev.reg(name).addr[0] == addr, f"{name} -> dword {addr:#x}")
check(dev.regSDMA_F32_CNTL.addr == {0: 0x1260 + 0x2a, 1: 0x1860 + 0x2a}, "regSDMA_F32_CNTL per instance (sdma0_4_2_offset.h:98-99)")
check(dev.regSDMA_F32_CNTL.fields_mask('halt') == 1 and dev.regCP_MEC_CNTL.encode(mec_invalidate_icache=1, mec_me1_pipe0_reset=1, mec_me1_pipe1_reset=1,
      mec_me1_pipe2_reset=1, mec_me1_pipe3_reset=1, mec_me2_pipe0_reset=1, mec_me2_pipe1_reset=1, mec_me1_halt=1, mec_me2_halt=1) == 0x503f0010,
      "SDMA HALT mask 1 (sdma0_4_2_sh_mask.h:586); MEC disable mask 0x503f0010 (gfx_v9_0.c:3467-3476 bits 4,16-21,28,30)")
dev.boot_version = AMDev.VEGA20_BOOT_VERSION_PLACEHOLDER_NOT_RULED_BY_LEON
dev._vega20_vbios()
check(dev.vega20_posted and dev.vbios.img == img and dev.gc_info.gc_num_se == SYN["se"] and dev.powerplay["SoftwareShutdownTemp"] == SYN["shutdown_temp"],
      "_vega20_vbios: POST seen, ROM dumped through the register objects, gc_info and powerplay limits set")
check(dev.reserved_vram_size == (32768 << 20) - (SYN["fw_start_kb"] << 10) == 8 << 20, f"reserved_vram_size = vram_size - fw region start = {dev.reserved_vram_size >> 20} MiB")
check(dev.mi50_ppt_w == 150.0 and dev.mi50_temp_c == 90 and dev.vega20_hotspot_c() == 60, "required inputs read; hotspot 60 °C from CTF_TEMP bits 9..17")
# failure paths
d2 = fake_dev(FakeMMIO(img, seed=seed(posted=False))); d2._run_discovery(); d2._build_regs()
raises(d2._vega20_vbios, "not POSTed", "POST bit clear -> boot stops before the ROM read"); check(not getattr(d2, 'vega20_posted', False), "unposted card never marked posted")
d3 = fake_dev(FakeMMIO(synthetic_image(scratch_base=0x4d), seed=seed())); d3._run_discovery(); d3._build_regs()
raises(d3._vega20_vbios, "bios_scratch_reg_startaddr", "VBIOS scratch base != nbio_7_4 BIOS_SCRATCH_0 refused")
d4 = fake_dev(FakeMMIO(synthetic_image(fw_start_kb=16 << 10), seed=seed())); d4._run_discovery(); d4._build_regs()
raises(d4._vega20_vbios, "overlaps AM's boot region", "fw region inside the 32 MiB boot region refused (U-M1)")
d5 = fake_dev(FakeMMIO(synthetic_image(vru_version=None), seed=seed())); d5._run_discovery(); d5._build_regs()
raises(d5._vega20_vbios, "AM_MI50_RESERVED_VRAM_MB", "no vram_usagebyfirmware -> AM_MI50_RESERVED_VRAM_MB required")
os.environ["AM_MI50_RESERVED_VRAM_MB"] = "64"; d5._vega20_vbios(); check(d5.reserved_vram_size == 64 << 20, "explicit AM_MI50_RESERVED_VRAM_MB honoured"); del os.environ["AM_MI50_RESERVED_VRAM_MB"]
d6 = fake_dev(FakeMMIO(img, seed=seed(temp=0))); d6._run_discovery(); d6._build_regs(); raises(d6._vega20_vbios, "invalid", "CTF_TEMP 0 is an invalid reading (§7.0 rule 4)")
d7 = fake_dev(FakeMMIO(img, seed=seed(temp=95))); d7._run_discovery(); d7._build_regs(); raises(d7._vega20_vbios, ">= threshold", "hotspot 95 >= AM_MI50_TEMP_C 90 refused")
for var in ("AM_MI50_PPT_W", "AM_MI50_TEMP_C"):
  saved = os.environ.pop(var); d8 = fake_dev(FakeMMIO(img, seed=seed()), inputs=False)
  raises(d8._vega20_required_inputs, f"{var} = ", f"missing {var} is a clear error (REQUIRED)"); os.environ[var] = saved
# review 2026-10-13 S13/TI4: one parse rule (int(v, 0), bounded) shared with extra/mi50/ladder.py _env_int
for var, val, needle in (("AM_MI50_TEMP_C", "600", "outside 1..510"), ("AM_MI50_TEMP_C", "0", "outside 1..510"), ("AM_MI50_PPT_W", "150.5", "not an integer"),
                         ("AM_MI50_PPT_W", "0", "outside 1..65535")):
  saved = os.environ[var]; os.environ[var] = val; d9 = fake_dev(FakeMMIO(img, seed=seed()), inputs=False)
  raises(d9._vega20_required_inputs, needle, f"{var}={val} refused"); os.environ[var] = saved
os.environ["AM_MI50_PPT_W"] = "0x96"; d9 = fake_dev(FakeMMIO(img, seed=seed())); check(d9.mi50_ppt_w == 150, "AM_MI50_PPT_W=0x96 parsed as 150 (int(v, 0), the ladder's rule)")
os.environ["AM_MI50_PPT_W"] = "150"
saved = os.environ.pop("AM_MI50_TEMP_C"); mm9 = FakeMMIO(img, seed=seed()); pdev9 = FakePCI(mmio=mm9)
raises(lambda: AMDev(pdev9), "AM_MI50_TEMP_C", "__init__ with a missing input raises")
check(pdev9.cfg_writes == [] and mm9.log == [], "...before any config WRITE (ASPM) and before any MMIO access: nothing touched (review TI4)")
os.environ["AM_MI50_TEMP_C"] = saved
# non-Vega gating: a gfx11-shaped fake binds no Vega tables; boot_version is the stock constant
g11 = AMDev.__new__(AMDev); g11.devfmt, g11.is_vega20 = "fake-gfx11", False
g11.ip_ver = {am.GC_HWIP: (11,0,2), am.MP0_HWIP: (13,0,7), am.MP1_HWIP: (13,0,7), am.HDP_HWIP: (6,0,0), am.MMHUB_HWIP: (3,0,1), am.OSSSYS_HWIP: (6,0,0), am.NBIO_HWIP: (7,7,0), am.SDMA0_HWIP: (6,0,2)}
g11.regs_offset = __import__("collections").defaultdict(dict, {h: {0: (0,)*6} for h in g11.ip_ver}); g11.harvested = __import__("collections").defaultdict(set)
g11._build_regs()
check(not any(n in g11.__dict__ for n in ("mmROM_INDEX", "mmCG_MULT_THERMAL_STATUS", "mmRPB_ARB_CNTL", "mmSDMA_F32_CNTL")) and "regCP_MEC_RS64_CNTL" in g11.__dict__,
      "gfx11 fake: no thm/smuio/athub/sdma_4_2 tables bound (gate GC == (9,4,0) / SDMA in {4.2.0, 4.4.2, 4.4.4}); gc_11_0_0 bound")
check((AMDev.VEGA20_BOOT_VERSION_PLACEHOLDER_NOT_RULED_BY_LEON if g11.is_vega20 else AMDev.Version) == 0xA0000008, "non-Vega boot_version == AMDev.Version 0xA0000008")
check(AMDev.VEGA20_BOOT_VERSION_PLACEHOLDER_NOT_RULED_BY_LEON not in (0xA0000008, 0xA000000D), "Vega placeholder differs from stock 0xA0000008 and master 0xA000000D")
q = fake_dev(FakeMMIO(img)); q.is_vega20 = False; q._emergency_quiesce(); check(q.mmio.log == [], "_emergency_quiesce is a no-op when not Vega 20")

# ---------------- (6) emergency quiesce + __init__ wrapper ----------------
mm.log.clear(); dev._emergency_quiesce()
check((0x208d, 0x503f0010) in mm.log and (0x128a, 1) in mm.log and (0x188a, 1) in mm.log, "quiesce (SMU silent): MEC disable mask written, both SDMA F32_CNTL.HALT=1")
check(not any(r == FakeMMIO.C2PMSG_66 and val in (smu.PPSMC_MSG_SetSoftMaxByFreq, smu.PPSMC_MSG_SetPptLimit) for r, val in mm.log), "quiesce (SMU silent): no cap messages sent")
mm.smu_alive = True; mm.log.clear(); dev._emergency_quiesce()
msgs = [(val, mm.log[i-1][1]) for i, (r, val) in enumerate(mm.log) if r == FakeMMIO.C2PMSG_66]   # (msg, param written just before to C2PMSG_82)
softmin = [p for m, p in msgs if m == smu.PPSMC_MSG_SetSoftMinByFreq]; softmax = [p for m, p in msgs if m == smu.PPSMC_MSG_SetSoftMaxByFreq]
want_clks = (smu.PPCLK_GFXCLK, smu.PPCLK_UCLK, smu.PPCLK_SOCCLK)   # _vega20_set_clocks order; FCLK is not in vega20_force_dpm_lowest's masks
check(softmin == [(c << 16) | 800 for c in want_clks] and softmax == softmin and (smu.PPCLK_GFXCLK, smu.PPCLK_UCLK, smu.PPCLK_SOCCLK) == (0, 5, 4),
      f"quiesce (SMU alive, review S3): SetSoftMinByFreq THEN SetSoftMaxByFreq to the lowest level for GFXCLK 0, UCLK 5, SOCCLK 4 (vega20_hwmgr.c:2417-2429), no FCLK: min {[hex(x) for x in softmin]} max {[hex(x) for x in softmax]}")
i_min, i_max = msgs.index((smu.PPSMC_MSG_SetSoftMinByFreq, softmin[0])), msgs.index((smu.PPSMC_MSG_SetSoftMaxByFreq, softmax[0]))
check(msgs[0][0] == smu.PPSMC_MSG_GetSmuVersion and msgs[1] == (smu.PPSMC_MSG_SetPptLimit, 150) and i_min < i_max,
      "quiesce (SMU alive, review S2): GetSmuVersion, then SetPptLimit 150 W as the FIRST lever, mins before maxes")
# review S2 (§7.0 rule 5): a mailbox timeout on the cap ends the quiesce; a rejection does not
class StubSMU:
  def __init__(self, fail): self.fail, self.sent, self.smu_mod = fail, [], smu
  def is_smu_alive(self): return True
  def _send_msg(self, msg, param, **k):
    self.sent.append(msg)
    if isinstance(self.fail, Exception): raise self.fail
  def _vega20_set_clocks(self, level, features=None): self.sent.append("force_lowest")
from tinygrad.runtime.support.am.ip import AM_SMU
for fail, want, label in ((TimeoutError("fake mailbox timeout"), [smu.PPSMC_MSG_SetPptLimit], "TimeoutError on SetPptLimit -> no further messages (rule 5)"),
                          (AM_SMU.ResponseError("fake reject"), [smu.PPSMC_MSG_SetPptLimit, "force_lowest"], "ResponseError on SetPptLimit -> clocks still forced lowest")):
  dev.smu = StubSMU(fail); dev._emergency_quiesce(); check(dev.smu.sent == want, f"quiesce: {label}: {dev.smu.sent}")
del dev.smu
# review S7: the engine halts follow a LIVE POST read, not the vega20_posted flag
dv = fake_dev(FakeMMIO(img, seed=seed(posted=True))); dv._run_discovery(); dv._build_regs()   # no _vega20_vbios -> flag never set
dv.mmio.log.clear(); dv._emergency_quiesce()
check((0x208d, 0x503f0010) in dv.mmio.log and (0x128a, 1) in dv.mmio.log, "quiesce halts MEC/SDMA on a POSTed card even when _vega20_vbios never ran (live POST read, review S7)")
dv2 = fake_dev(FakeMMIO(img, seed=seed(posted=False))); dv2._run_discovery(); dv2._build_regs(); dv2.mmio.log.clear(); dv2._emergency_quiesce()
check((0x208d, 0x503f0010) not in dv2.mmio.log and (0x128a, 1) not in dv2.mmio.log, "quiesce leaves the engines alone when the live POST read says unposted")
# review L1/TI3/S4/S14: the boot clock policy gates the cap on FEATURE_PPT (vega20_powertune.c:37-42) and records the state; one get_features pair
import io, contextlib
class PolicySMU:
  def __init__(self, feats): self.feats, self.calls, self.smu_mod = feats, [], smu
  def get_features(self): self.calls.append("get_features"); return self.feats
  def _vega20_set_power_limit(self, w, features=None): self.calls.append(("cap", w, features))
  def _vega20_set_clocks(self, level, features=None): self.calls.append(("clocks", level, features)); return {} if not (self.feats & 0x30f) else {0: (800, 800)}
dp = fake_dev(FakeMMIO(img, seed=seed())); dp._run_discovery(); dp._build_regs(); dp.ip_ver = dict(dp.ip_ver)
dp.smu = PolicySMU(FakeMMIO.FEATURES_LOW); dp._vega20_clock_policy()
check(dp.smu.calls == ["get_features", ("cap", 150, FakeMMIO.FEATURES_LOW), ("clocks", 0, FakeMMIO.FEATURES_LOW)], f"clock policy with PPT on: one get_features, cap 150 W, level 0, features passed through: {dp.smu.calls}")
dp.smu = PolicySMU(0); buf = io.StringIO()
with contextlib.redirect_stdout(buf): dp._vega20_clock_policy()
check(dp.smu.calls == ["get_features", ("clocks", 0, 0)] and "MI50 cap NOT applied" in buf.getvalue() and "UNCAPPED" in buf.getvalue(),
      f"clock policy with FEATURE_PPT off: no SetPptLimit, state printed (vega20_powertune.c:37-42 silent form), level 0 still sent: {dp.smu.calls}")
# the __init__ wrapper: inject an error after the VBIOS step (design T6d), expect the quiesce ran and the error propagated
mm2 = FakeMMIO(img, smu_alive=True, seed=seed()); pdev = FakePCI(mmio=mm2)
def boom(*a, **k): raise RuntimeError("injected after _vega20_vbios")
AMDev.init_sw, orig = boom, AMDev.init_sw
try:
  try: AMDev(pdev)
  except RuntimeError as e: check("injected" in str(e), f"__init__ re-raised the injected error: {e}")
  else: check(False, "__init__ did not raise")
finally: AMDev.init_sw = orig
check((0x208d, 0x503f0010) in mm2.log and (0x128a, 1) in mm2.log and any(r == FakeMMIO.C2PMSG_66 and val == smu.PPSMC_MSG_SetPptLimit for r, val in mm2.log),
      "__init__ wrapper: _emergency_quiesce halted MEC/SDMA and capped the SMU before re-raising")

# ---------------- (7) clang sizeof/offsetof of the ported structs ----------------
C_SRC = r'''
#include <stdint.h>
#include <stdio.h>
#include <stddef.h>
#define bool _Bool
#include "atomfirmware.h"
#include "atom-types.h"
#include "smu11_driver_if.h"
#include "vega20_pptable.h"
#define S(t) printf("sizeof %s %zu\n", #t, sizeof(struct t))
#define ST(t) printf("sizeof %s %zu\n", #t, sizeof(t))
#define O(t,f) printf("offsetof %s %s %zu\n", #t, #f, offsetof(struct t, f))
#define OT(t,f) printf("offsetof %s %s %zu\n", #t, #f, offsetof(t, f))
int main(void) {
  S(atom_common_table_header); S(atom_rom_header_v2_2); S(atom_master_list_of_data_tables_v2_1); S(atom_master_data_table_v2_1);
  S(atom_firmware_info_v3_1); S(atom_firmware_info_v3_2); S(atom_gfx_info_v2_4); S(vram_usagebyfirmware_v2_1); S(vram_usagebyfirmware_v2_2);
  ST(ATOM_VEGA20_OVERDRIVE8_RECORD); ST(ATOM_VEGA20_POWER_SAVING_CLOCK_RECORD); ST(ATOM_Vega20_POWERPLAYTABLE);
  O(atom_rom_header_v2_2, masterdatatable_offset); O(atom_master_list_of_data_tables_v2_1, firmwareinfo); O(atom_master_list_of_data_tables_v2_1, vram_usagebyfirmware);
  O(atom_master_list_of_data_tables_v2_1, gfx_info); O(atom_master_list_of_data_tables_v2_1, powerplayinfo); O(atom_firmware_info_v3_1, bios_scratch_reg_startaddr);
  O(atom_firmware_info_v3_2, bios_scratch_reg_startaddr); O(atom_firmware_info_v3_2, bootup_sclk_in10khz); O(atom_gfx_info_v2_4, max_shader_engines);
  O(atom_gfx_info_v2_4, gc_num_gprs); O(atom_gfx_info_v2_4, gc_lds_size); O(atom_gfx_info_v2_4, gc_max_scratch_slots_per_cu); O(vram_usagebyfirmware_v2_1, used_by_firmware_in_kb);
  O(vram_usagebyfirmware_v2_2, fw_region_start_address_in_kb); OT(ATOM_Vega20_POWERPLAYTABLE, usSoftwareShutdownTemp); OT(ATOM_Vega20_POWERPLAYTABLE, OverDrive8Table);
  OT(ATOM_Vega20_POWERPLAYTABLE, smcPPTable); OT(ATOM_VEGA20_OVERDRIVE8_RECORD, ODSettingsMax); OT(ATOM_VEGA20_OVERDRIVE8_RECORD, ODSettingsMin);
  printf("define ATOM_S7_ASIC_INIT_COMPLETE_MASK %u\n", 0x200u); printf("define PPTABLE_V20_SMU_VERSION %u\n", (unsigned)PPTABLE_V20_SMU_VERSION);
  printf("define ATOM_VEGA20_TABLE_REVISION_VEGA20 %u\n", (unsigned)ATOM_VEGA20_TABLE_REVISION_VEGA20); printf("define ATOM_VEGA20_ODSETTING_COUNT %u\n", (unsigned)ATOM_VEGA20_ODSETTING_COUNT);
  printf("define ATOM_VEGA20_ODFEATURE_COUNT %u\n", (unsigned)ATOM_VEGA20_ODFEATURE_COUNT);
  return 0; }
'''
incs = [LINUX / "include", LINUX / "pm/powerplay/inc", LINUX / "pm/powerplay/hwmgr"]
if not all((d / f).is_file() for d, f in [(incs[0], "atomfirmware.h"), (incs[1], "smu11_driver_if.h"), (incs[2], "vega20_pptable.h")]):
  check(False, f"C compile: headers not found under {LINUX} (set MI50_LINUX_AMD); sizeof NOT verified")
else:
  with tempfile.TemporaryDirectory() as td:
    (pathlib.Path(td) / "t.c").write_text(C_SRC)
    r = subprocess.run(["clang", "-std=c11", *[f"-I{d}" for d in incs], f"{td}/t.c", "-o", f"{td}/t"], capture_output=True, text=True)
    check(r.returncode == 0, "C compile of atomfirmware.h + smu11_driver_if.h + vega20_pptable.h" + ("" if r.returncode == 0 else f": {r.stderr[:400]}"))
    out = subprocess.run([f"{td}/t"], capture_output=True, text=True, check=True).stdout if r.returncode == 0 else ""
  pyconst = {"ATOM_S7_ASIC_INIT_COMPLETE_MASK": v.ATOM_S7_ASIC_INIT_COMPLETE_MASK, "PPTABLE_V20_SMU_VERSION": v.PPTABLE_V20_SMU_VERSION,
             "ATOM_VEGA20_TABLE_REVISION_VEGA20": v.ATOM_VEGA20_TABLE_REVISION_VEGA20, "ATOM_VEGA20_ODSETTING_COUNT": len(v.ATOM_VEGA20_ODSETTING),
             "ATOM_VEGA20_ODFEATURE_COUNT": len(v.ATOM_VEGA20_ODFEATURE)}
  for line in out.splitlines():
    kind, *a = line.split()
    if kind == "sizeof": check(ctypes.sizeof(getattr(v, a[0])) == int(a[1]), f"C sizeof({a[0]}) = {a[1]} == ctypes")
    elif kind == "offsetof": check(getattr(getattr(v, a[0]), a[1]).offset == int(a[2]), f"C offsetof({a[0]}, {a[1]}) = {a[2]} == ctypes")
    elif kind == "define": check(pyconst[a[0]] == int(a[1]), f"C {a[0]} = {a[1]} == vbios.py")

print(f"\n{len(fails)} failure(s)" + (": " + "; ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
