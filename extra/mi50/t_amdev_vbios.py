#!/usr/bin/env python3
"""Offline check (no card) for the mi50 fork's amdev.py Vega 20 branches and runtime/support/am/vbios.py (design v2 §3 amdev.py rows; B3, B4, B7, B16).

(1) a synthetic ATOM image built from the ported header structs (NO VBIOS dump exists yet - every number in it is a SYNTHETIC test value, not an
    MI50 fact) -> VBIOS walker: signature/length/ATOM checks, master data table walk, firmwareinfo v3.2, gfx_info v2.4, vram_usagebyfirmware v2.1,
    powerplayinfo + OverDrive8 limits; (2) read_rom through fake ROM_INDEX/ROM_DATA registers at the design dwords 0x168e4/0x168e5 (auto-increment);
(3) gc_info v2.0 shaping (every field lands in the slot Linux pairs it with); (4) AMDev._static_vega20 + _build_regs on a fake PCI device:
    ip_ver / regs_offset == egpu/mi50/port/vega20_ip_table.py (+ the NBIF alias), register dwords == the design's numbers, aids, harvested,
    non-Vega gating (gfx11 fake binds no thm/smuio/athub/sdma tables, boot_version == AMDev.Version); (5) POST like Linux: ATOM ASIC_Init when
    BIOS_SCRATCH_7 bit 9 is clear, only its return value judged (amdgpu_device.c:4067-4080), the VBIOS scratch base used as given, reserved VRAM 0,
    the ATOM scratch size, the real __init__ order (nothing on the card before the ROM read and POST), the mode1-reset re-POST;
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
HDR, MDT, FWI, GFX, VRU, PPT, CMT, ASIC = 0x100, 0x200, 0x300, 0x400, 0x500, 0x600, 0xD00, 0xE00   # CMT/ASIC: command table master + ASIC_Init
MARK = 0x1000   # synthetic ASIC_Init copies PS[0] (default sclk) here so the test sees the parameter space atom_asic_init built
SYN = dict(se=4, cu_per_sh=16, sh_per_se=1, rb=4, tccs=16, gprs=256, gs_thds=32, gs_depth=32, gsprim=1792, dbl_lds=1, wave=64, waves_simd=10,
           scratch_slots=32, lds=64, sclk10k=85200, mclk10k=100000, fw_start_kb=(32768 << 10) - (8 << 10), fw_kb=8192, shutdown_temp=118)
def synthetic_image(scratch_base=0x4c, posted_fw_region=True, vru_version=(2, 1), gfx_crev=4, pptable_version=3, fw_start_kb=None,
                    asic_init="set_post", vru_flag=0x80000000) -> bytes:
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
    u.start_address_in_kb = (SYN["fw_start_kb"] if fw_start_kb is None else fw_start_kb) | vru_flag   # 0x2 << 30 = SRIOV msg-share flag, masked off by the reader (atombios.h:4298-4302)
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
  if asic_init is not None:   # SYNTHETIC ATOM bytecode (atom.c opcode table :1098-1227), not MI50 code
    img[HDR + 0x1E:HDR + 0x20] = CMT.to_bytes(2, "little")            # rom header masterhwfunction_offset = ATOM_ROM_CMD_PTR (atom.h:41)
    img[CMT + 4:CMT + 6] = ASIC.to_bytes(2, "little")                 # command table 0 = ATOM_CMD_INIT (atom.h:44)
    code = bytes([1, 0x01]) + MARK.to_bytes(2, "little") + bytes([0])   # MOVE_REG REG[MARK] <- PS[0] dword
    if asic_init == "set_post": code += bytes([13, 0x05]) + (0x53).to_bytes(2, "little") + (0x200).to_bytes(4, "little")   # OR_REG BIOS_SCRATCH_7 |= 0x200
    code += bytes([91])                                               # EOT
    img[ASIC:ASIC + 6 + len(code)] = (6 + len(code)).to_bytes(2, "little") + bytes([1, 1, 0, 0]) + code   # size, frev, crev, WS 0, PS 0
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
  """dword-indexed BAR5 (512 KiB = 0x20000 dwords, design §1.3); ROM_DATA auto-increments from the pointer ROM_INDEX sets."""
  ROM_INDEX, ROM_DATA = 0x168e4, 0x168e5
  def __init__(self, img:bytes, seed=None):
    self.img, self.ptr, self.regs, self.log = img, 0, dict(seed or {}), []
  def __len__(self): return 0x20000
  def __getitem__(self, k):
    if k == self.ROM_DATA: val, self.ptr = int.from_bytes(self.img[self.ptr:self.ptr+4].ljust(4, b"\0"), 'little'), self.ptr + 4; return val
    return self.regs.get(k, 0)
  def __setitem__(self, k, val):
    self.log.append((k, val))
    if k == self.ROM_INDEX: self.ptr = val
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
def seed(posted=True, memsize_mib=32768): return {0xde3: memsize_mib, 0x53: 0x200 if posted else 0}
def fake_dev(mmio) -> AMDev:
  """The hand-built AMDev mirrors __init__: is_vega20 from config dword 0."""
  dev = AMDev.__new__(AMDev)
  dev.pci_dev, dev.devfmt, dev.mmio, dev.vram = FakePCI(mmio=mmio), "fake", mmio, types.SimpleNamespace(nbytes=256 << 20)
  dev.is_vega20 = dev.pci_dev.read_config(0, 4) == VEGA20_PCI_ID
  return dev
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
dev.boot_version = AMDev.VEGA20_BOOT_VERSION
dev._vega20_vbios()
check(dev.vega20_posted and dev.vbios.img == img and dev.gc_info.gc_num_se == SYN["se"] and dev.powerplay["SoftwareShutdownTemp"] == SYN["shutdown_temp"],
      "_vega20_vbios: POST seen, ROM dumped through the register objects, gc_info and powerplay limits set")
check(dev.reserved_vram_size == 0, "reserved_vram_size 0 even with a vram_usagebyfirmware table (bare metal: amdgpu_ttm.c:1741-1772, amdgpu_atomfirmware.c:119-125)")
# failure paths
d2 = fake_dev(FakeMMIO(img, seed=seed(posted=False))); d2._run_discovery(); d2._build_regs()
d2._vega20_vbios()
w = d2.mmio.log
rom_i = next(i for i, (k, _) in enumerate(w) if k == FakeMMIO.ROM_INDEX); mark_i = next((i for i, (k, _) in enumerate(w) if k == MARK), None)
check(d2.vega20_posted and mark_i is not None and rom_i < mark_i, "unposted card: ROM read first, then ASIC_Init ran (amdgpu_device.c:4067-4080 order), card marked posted")
check((MARK, SYN["sclk10k"]) in w and d2.mmio.regs[0x53] & 0x200, "ASIC_Init got PS[0] = firmwareinfo bootup sclk (atom.c:1660) and set BIOS_SCRATCH_7 bit 9")
check(not any(k == MARK for k, _ in dev.mmio.log), "POSTed card: ASIC_Init never runs (amdgpu_device_need_post false)")
d2b = fake_dev(FakeMMIO(synthetic_image(asic_init="noop"), seed=seed(posted=False))); d2b._run_discovery(); d2b._build_regs()
d2b._vega20_vbios()
check(d2b.vega20_posted and any(k == MARK for k, _ in d2b.mmio.log) and not d2b.mmio.regs[0x53] & 0x200,
      "ASIC_Init returning 0 is success even if the POST bit stays clear: Linux judges only the return value (amdgpu_device.c:4075-4079)")
d2c = fake_dev(FakeMMIO(synthetic_image(asic_init=None), seed=seed(posted=False))); d2c._run_discovery(); d2c._build_regs()
raises(d2c._vega20_vbios, "gpu post error! amdgpu_atom_asic_init returned 1", "no ASIC_Init table -> atom_asic_init 1 -> post error (atom.c:1665-1666)")
# POST first (how the hardware works, Leon 2026-10-13): the REAL _boot order, every MMIO access logged in sequence
class OrderMMIO(FakeMMIO):
  def __init__(self, *a, **k): super().__init__(*a, **k); self.seq = []
  def __getitem__(self, k): self.seq.append(("R", k)); return super().__getitem__(k)
  def __setitem__(self, k, val): self.seq.append(("W", k)); return super().__setitem__(k, val)
class _StopBoot(Exception): pass
om = OrderMMIO(img, seed=seed(posted=False)); pci5 = FakePCI(mmio=om)
booted = []
def _stop(self, smi_dev=False): booted.append(self); raise _StopBoot()   # stop right after the VBIOS tables (amdev.py _boot: init_sw follows)
AMDev.init_sw, _orig_init_sw = _stop, AMDev.init_sw
try: AMDev(pci5)   # the REAL __init__: identity, ASPM, BAR maps, _boot
except _StopBoot: pass
finally: AMDev.init_sw = _orig_init_sw
d5 = booted[0]
sq = om.seq
first_rom = next(i for i, a in enumerate(sq) if a == ("W", FakeMMIO.ROM_INDEX))
post_end = max(i for i, a in enumerate(sq) if a == ("W", MARK))
pre = [a for a in sq[:first_rom]]
check(pre == [], f"nothing touches the card's registers before the ROM read (POST needs it): {pre[:4]}")
check(all(i > post_end for i, a in enumerate(sq) if a == ("R", 0xde3)) and ("R", 0xde3) in sq and ("R", 0x1665f) not in sq,
      "memsize (0xde3) is read only AFTER ASIC_Init; no thermal read during boot up to init_sw")
check([w for w in pci5.cfg_writes] == [], f"pre-POST config writes: only the ASPM link-control write when a PCIe cap exists (fake has none): {pci5.cfg_writes}")
between = {a for a in sq[first_rom:post_end + 1] if a[1] not in (FakeMMIO.ROM_INDEX, FakeMMIO.ROM_DATA, 0x53, MARK)}
check(not between, f"between the ROM read and the end of ASIC_Init only ROM_INDEX/ROM_DATA, the POST bit and ASIC_Init's own access: {sorted(between)[:4]}")
check(d5.vega20_posted and d5.vram_size == 32768 << 20, "the real _boot POSTed the card, then read memsize")
# mode1 reset = amdgpu_device_mode1_reset (amdgpu_device.c:4937-4999), then the full-reset re-POST (:5143-5146). No gfx/smu objects are installed:
# AMDev.__getattr__ raises on them, so any quiesce call (gfx.fini_hw / smu.set_clocks) fails this check.
check(d2.regBIOS_SCRATCH_3.addr[0] == 0x4f, "regBIOS_SCRATCH_3 -> dword 0x4f (scratch base 0x4c + 3, amdgpu_atombios.c:1613)")
ev = []
class CfgPCI:
  """256-byte config space: MI50 function, PCIe capability at 0x60 (version 2, endpoint), synthetic values."""
  def __init__(self):
    self.cfg = bytearray(256)
    for off, val, size in ((0, VEGA20_PCI_ID, 4), (0x04, 0x0006, 2), (0x10, 0xe000000c, 4), (0x18, 0xf000000c, 4), (0x24, 0xf8000000, 4),
                           (0x3c, 0x01ff, 2), (0x34, 0x60, 1), (0x60, 0x0010, 2), (0x62, 0x0002, 2), (0x68, 0x2810, 2), (0x70, 0x0040, 2),
                           (0x78, 0x1111, 2), (0x7c, 0x2222, 2), (0x88, 0x0400, 2), (0x90, 0x0003, 2), (0x98, 0x3333, 2)): self.put(off, val, size)
  def put(self, off, val, size): self.cfg[off:off + size] = val.to_bytes(size, "little")
  def read_config(self, off, size): return int.from_bytes(self.cfg[off:off + size], "little")
  def write_config(self, off, val, size): ev.append(("cfgw", off, val, size)); self.put(off, val, size)
class _P:
  def mode1_reset(self):
    ev.append(("psp.mode1_reset", d2.mmio.regs.get(0x4f, 0) & 0x20000000)); d2.mmio.regs[0x53] = 0   # the reset clears the POST bit ...
    for off, val, size in ((0x04, 0, 2), (0x10, 0x0000000c, 4), (0x70, 0x0043, 2), (0x88, 0, 2)): d2.pci_dev.put(off, val, size)   # ... and config
  def _wait_for_bootloader(self): ev.append("psp._wait_for_bootloader")
d2.pci_dev, d2.psp = CfgPCI(), _P()
d2.mmio.log.clear()
d2._vega20_reset()
lg = d2.mmio.log
mark_after = [i for i, (k, _) in enumerate(lg) if k == MARK]
check(("psp.mode1_reset", 0x20000000) in ev, "BIOS_SCRATCH_3 ATOM_S3_ASIC_GUI_ENGINE_HUNG set while the PSP mode1 reset runs (amdgpu_device.c:4942-4943)")
cfgw = [e[1:] for e in ev if e[0] == "cfgw"]
check(cfgw == [(0x68, 0x2810, 2), (0x70, 0x0040, 2), (0x88, 0x0400, 2), (0x90, 0x0003, 2), (0x10, 0xe000000c, 4), (0x04, 0x0006, 4)],
      f"PCI state restore: DEVCTL, LNKCTL, DEVCTL2, LNKCTL2 written back (endpoint: no SLTCTL/RTCTL/SLTCTL2), then only the changed header "
      f"dwords, BAR0 before COMMAND (pci.c pci_restore_state order): {[(hex(o), hex(v), n) for o, v, n in cfgw]}")
order = [e if isinstance(e, str) else e[0] for e in ev]
check(order.index("psp.mode1_reset") < order.index("cfgw") and max(i for i, e in enumerate(order) if e == "cfgw") < order.index("psp._wait_for_bootloader"),
      "order: PSP mode1 reset -> PCI state restore -> psp_v11_0_wait_for_bootloader (amdgpu_device.c:4960, :4972, :4973)")
check(not d2.mmio.regs[0x4f] & 0x20000000, "engine-hung flag cleared after the reset (amdgpu_device.c:4991-4992)")
check(len(mark_after) == 1 and d2.mmio.regs[0x53] & 0x200, "card re-POSTed by ATOM ASIC_Init after the reset (full reset posts unconditionally, amdgpu_device.c:5143-5146)")
check(not AMDev._pcie_reg_implemented(0x0001, 0x30) and AMDev._pcie_reg_implemented(0x0142, 0x18) and AMDev._pcie_reg_implemented(0x0042, 0x1c),
      "pcie_capability_reg_implemented: LNKCTL2 needs version > 1; SLTCTL on a downstream port with SLOT; RTCTL on a root port (access.c)")
# the VBIOS scratch base is used as given (amdgpu_atomfirmware_scratch_regs_init, amdgpu_atomfirmware.c:88-103): POST bit read at base + 7
d3 = fake_dev(FakeMMIO(synthetic_image(scratch_base=0x4d), seed={0xde3: 32768, 0x54: 0x200})); d3._run_discovery(); d3._build_regs(); d3._vega20_vbios()
check(d3.bios_scratch_reg_offset == 0x4d and not any(k == MARK for k, _ in d3.mmio.log), "scratch base 0x4d from the VBIOS: POST bit read at 0x54, set -> no ASIC_Init")
d5 = fake_dev(FakeMMIO(synthetic_image(vru_version=None), seed=seed())); d5._run_discovery(); d5._build_regs()
d5._vega20_vbios(); check(d5.reserved_vram_size == 0 and d5._vega20_atom_scratch_bytes() == 0, "no vram_usagebyfirmware -> reserved 0, ATOM scratch 0 (AtomContext default)")
check(dev._vega20_atom_scratch_bytes() == 0, "vram_usagebyfirmware v2.1 with the SRIOV flag (0x2 << 30) -> ATOM scratch 0 (amdgpu_atomfirmware.c:105-131)")
d5b = fake_dev(FakeMMIO(synthetic_image(vru_flag=0), seed=seed())); d5b._run_discovery(); d5b._build_regs(); d5b._vega20_vbios()
check(d5b._vega20_atom_scratch_bytes() == 256 << 10, "v2.1 without the SRIOV flag -> ATOM scratch = used_by_driver_in_kb (256 KiB)")
# non-Vega gating: a gfx11-shaped fake binds no Vega tables; boot_version is the stock constant
g11 = AMDev.__new__(AMDev); g11.devfmt, g11.is_vega20 = "fake-gfx11", False
g11.ip_ver = {am.GC_HWIP: (11,0,2), am.MP0_HWIP: (13,0,7), am.MP1_HWIP: (13,0,7), am.HDP_HWIP: (6,0,0), am.MMHUB_HWIP: (3,0,1), am.OSSSYS_HWIP: (6,0,0), am.NBIO_HWIP: (7,7,0), am.SDMA0_HWIP: (6,0,2)}
g11.regs_offset = __import__("collections").defaultdict(dict, {h: {0: (0,)*6} for h in g11.ip_ver}); g11.harvested = __import__("collections").defaultdict(set)
g11._build_regs()
check(not any(n in g11.__dict__ for n in ("mmROM_INDEX", "mmCG_MULT_THERMAL_STATUS", "mmRPB_ARB_CNTL", "mmSDMA_F32_CNTL")) and "regCP_MEC_RS64_CNTL" in g11.__dict__,
      "gfx11 fake: no thm/smuio/athub/sdma_4_2 tables bound (gate GC == (9,4,0) / SDMA in {4.2.0, 4.4.2, 4.4.4}); gc_11_0_0 bound")
check((AMDev.VEGA20_BOOT_VERSION if g11.is_vega20 else AMDev.Version) == 0xA0000008, "non-Vega boot_version == AMDev.Version 0xA0000008")
check(AMDev.VEGA20_BOOT_VERSION not in (0xA0000008, 0xA000000D), "Vega placeholder differs from stock 0xA0000008 and master 0xA000000D")

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
