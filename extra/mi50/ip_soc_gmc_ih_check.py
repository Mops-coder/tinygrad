#!/usr/bin/env python3
"""Offline check (no card) for the mi50 fork's AM_SOC / AM_GMC / AM_IH Vega 20 branches (Vega20 Port Design v2 B6, B8, B9, B10, B11).

(1) Instantiates the three classes against a fake adev (dict-backed rreg/wreg that records every read and write, ip_ver = Vega 20 from
    amdgpu_discovery.c:3196-3225, register tables = the fork's generated regs.py bound to the vega20_ip_offset.h bases) and prints the ordered
    register access log of init_sw + init_hw (+ the GC hub init AM_GFX triggers, + the PTE/PDE flag table), with the values the design names
    asserted (dword 0xde3 / 0x296b / 0xe17, doorbell ranges 0x200/0x214/0x2f0, VM_L2_CNTL3/4 vs the Linux defaults, RPTR_REARM=0, no MSI write...).
(2) Proves the non-Vega paths are unchanged: the same fake, with gfx11- / MI300- / gfx12-shaped ip versions, run through THIS worktree's ip.py and
    through the unmodified mi50 branch's ip.py (git show mi50:...), must produce byte-identical logs (reads, writes and raised errors included).
Run: PYTHONPATH=<worktree> python3 extra/mi50/ip_soc_gmc_ih_check.py   [env MI50_FORK_GIT=<path of the git checkout holding branch mi50>]
"""
import collections, functools, importlib.util, itertools, os, pathlib, subprocess, sys, tempfile, types
from tinygrad.runtime.autogen.am import am, vega_offsets as vo
from tinygrad.runtime.support.amd import import_asic_regs
from tinygrad.runtime.support.am.amdev import AMDev, AMRegister
import tinygrad.runtime.support.am.ip as ip_new

fails: list[str] = []
def check(cond, msg):
  print(("ok   " if cond else "FAIL ") + msg)
  if not cond: fails.append(msg)

# --- ip versions -------------------------------------------------------------------------------------------------------------------------
# Vega 20: amdgpu_discovery.c:3196-3225 (CHIP_VEGA20); NBIF = NBIO per vega20_reg_init.c:38,49 (ops_amd asks AMDIP('nbio', ip_versions[NBIF_HWIP]))
VEGA20 = {am.GC_HWIP: (9,4,0), am.MMHUB_HWIP: (9,4,0), am.ATHUB_HWIP: (9,4,0), am.OSSSYS_HWIP: (4,2,0), am.HDP_HWIP: (4,2,0), am.SDMA0_HWIP: (4,2,0),
          am.NBIO_HWIP: (7,4,0), am.NBIF_HWIP: (7,4,0), am.MP0_HWIP: (11,0,2), am.MP1_HWIP: (11,0,2), am.THM_HWIP: (11,0,2), am.SMUIO_HWIP: (11,0,2)}
# Vega 20 bases: autogen vega_offsets (vega20_ip_offset.h), the table Linux vega20_reg_base_init uses (vega20_reg_init.c:29-56)
def vega_bases(name): return {0: tuple(getattr(vo, f"{name}_BASE__INST0_SEG{s}", 0) for s in range(6))}
VEGA20_BASES = {am.GC_HWIP: vega_bases("GC"), am.MMHUB_HWIP: vega_bases("MMHUB"), am.ATHUB_HWIP: vega_bases("ATHUB"), am.OSSSYS_HWIP: vega_bases("OSSSYS"),
  am.HDP_HWIP: vega_bases("HDP"), am.NBIO_HWIP: vega_bases("NBIO"), am.MP0_HWIP: vega_bases("MP0"), am.MP1_HWIP: vega_bases("MP1"),
  am.SDMA0_HWIP: {0: vega_bases("SDMA0")[0], 1: vega_bases("SDMA1")[0]}}   # design v2 B3: SDMA1 = instance 1 of SDMA0_HWIP
# Non-Vega bases: test/mockgpu/am/amgpu.py:17-26 IP_BASES (any consistent base set works for a before/after log diff)
def _pad(t, n=10): return t + (0,) * (n - len(t))
MOCK_BASES = {am.GC_HWIP: {0: _pad((0x1260, 0xA000, 0x1C000, 0x2402C00))}, am.SDMA0_HWIP: {0: _pad((0x1260, 0xA000, 0x1C000, 0x2402C00))},
  am.MMHUB_HWIP: {0: _pad((0x1A000, 0x2408800))}, am.NBIO_HWIP: {0: _pad((0x0, 0x14, 0xD20, 0x10400, 0x241B000, 0x4040000))},
  am.MP0_HWIP: {0: _pad((0x16000, 0xDC0000, 0xE00000, 0xE40000, 0x243FC00))}, am.MP1_HWIP: {0: _pad((0x16000, 0xDC0000, 0xE00000, 0xE40000, 0x243FC00))},
  am.HDP_HWIP: {0: _pad((0xF20, 0x240A400))}, am.OSSSYS_HWIP: {0: _pad((0x10A0, 0x240A000))}}

class FakeMM:
  def __init__(self):
    self.next, self.va_base, self.va_bits = 0x100000, 0x200000000000, 48
    self.root_page_table = types.SimpleNamespace(paddr=self.palloc(0x1000), lv=am.AMDGPU_VM_PDB2)   # amdev.py init_sw: first_lv=AMDGPU_VM_PDB2
  def palloc(self, size, align=0x1000, zero=True, boot=False):
    self.next = (self.next + align - 1) // align * align
    paddr, self.next = self.next, self.next + size
    return paddr
class FakePCIDev:
  """config space: BAR2 (0x18/0x1c) = a 64-bit prefetchable memory BAR at bus address 0x1_9000_0000 (low dword carries the type bits 0xc)"""
  cfg = {0x18: 0x9000000c, 0x1c: 0x1}
  def read_config(self, off, size): return self.cfg.get(off, 0)
  def alloc_sysmem(self, size, vaddr=0, contiguous=False): return (object(), [0x7f0000000 + i * 0x1000 for i in range(size // 0x1000)])
class FakeVram:
  def view(self, *a, **k): return None

class FakeDev(AMDev):
  """AMDev with the hardware replaced: dict-backed registers + access log. reg()/__getattr__/wreg_pair/paddr2* are the real AMDev methods."""
  def __init__(self, ip_ver, bases, presets, extra_mods=()):
    self.ip_ver, self.regs_offset, self.devfmt, self.partial_boot = dict(ip_ver), bases, "fake", False
    self.harvested, self.aids, self.mm, self.vram, self.pci_dev = collections.defaultdict(set), [0], FakeMM(), FakeVram(), FakePCIDev()
    self.mmio = [0] * 0x20000   # BAR5 = 512 KiB = 0x20000 dwords (design v2 §1.3): rreg/wreg below never go indirect for addr < len(mmio)
    self.store, self.log, self.names = {}, [], collections.defaultdict(list)
    # amdev.py _build_regs: mp/hdp/gc/mmhub/osssys/nbio(+sdma for 4.4.x) by ip version, mp (11,0,0) for MP1; + the design v2 §3 extras (athub)
    mods = [("mp", am.MP0_HWIP), ("hdp", am.HDP_HWIP), ("gc", am.GC_HWIP), ("mmhub", am.MMHUB_HWIP), ("osssys", am.OSSSYS_HWIP),
            ("nbio" if self.ip_ver[am.GC_HWIP] < (12,0,0) else "nbif", am.NBIO_HWIP)] + list(extra_mods)
    for prefix, hwip in mods:
      self.__dict__.update(import_asic_regs(prefix, self.ip_ver[hwip], cls=functools.partial(AMRegister, adev=self, bases=self.regs_offset[hwip])))
    self.__dict__.update(import_asic_regs('mp', (11, 0, 0), cls=functools.partial(AMRegister, adev=self, bases=self.regs_offset[am.MP1_HWIP])))
    for k, v in self.__dict__.items():
      if isinstance(v, AMRegister):
        for a in v.addr.values(): self.names[a].append(k)
    for name, val in presets.items(): self.store[self.reg(name).addr[0]] = val
  def _nm(self, addr): return "|".join(self.names.get(addr, ["?"]))
  def rreg(self, reg):
    val = self.store.get(reg, 0)
    self.log.append(f"R {self._nm(reg)}@{reg:#07x} -> {val:#x}")
    return val
  def wreg(self, reg, val):
    self.store[reg] = val
    self.log.append(f"W {self._nm(reg)}@{reg:#07x} <- {val:#x}")

def run(mod:types.ModuleType, ip_ver, bases, presets, extra_mods=(), gc_hub=True):
  """init_sw + init_hw of SOC, GMC, IH (amdev.py init_sw/init_hw order), then the GC hub init AM_GFX.init_hw triggers, then the PTE flag table"""
  dev = FakeDev(ip_ver, bases, presets, extra_mods)
  out: list[str] = []
  try:
    dev.soc, dev.gmc, dev.ih = mod.AM_SOC(dev), mod.AM_GMC(dev), mod.AM_IH(dev)
    for blk in (dev.soc, dev.gmc, dev.ih):
      dev.log.append(f"-- {blk.__class__.__name__}.init_sw"); blk.init_sw()
    for blk in (dev.soc, dev.gmc, dev.ih):
      dev.log.append(f"-- {blk.__class__.__name__}.init_hw"); blk.init_hw()
    if gc_hub: dev.log.append("-- AM_GMC.init_hub('GC') (called by AM_GFX.init_hw)"); dev.gmc.init_hub("GC", insts=[0])
    dev.log.append("-- AM_SOC.doorbell_enable(port=0, awid=3, awaddr_31_28_value=3) (the AM_GFX.init_hw call)"); dev.soc.doorbell_enable(port=0, awid=0x3, awaddr_31_28_value=0x3)
    dev.log.append("-- AM_GMC.flush_hdp()"); dev.gmc.flush_hdp()
    for lv in (am.AMDGPU_VM_PDB2, am.AMDGPU_VM_PDB1, am.AMDGPU_VM_PDB0, am.AMDGPU_VM_PTB):
      for is_table in (True, False):
        f = dev.gmc.get_pte_flags(lv, is_table, 0, False, False, False, True)
        out.append(f"pte lv={am.enum_amdgpu_vm_level[lv]} table={int(is_table)} flags={f:#x} huge={int(bool(dev.gmc.is_pte_huge_page(lv, f)))}")
  except Exception as e: out.append(f"EXC {type(e).__name__}: {e}")
  return dev, dev.log + out

# --- (1) Vega 20 -------------------------------------------------------------------------------------------------------------------------
# presets: MMHUB FB location 0..32 GiB (T2 measures the real ones), FB_OFFSET 0 on a single card (design v2 B6/T2), memsize 32768 MiB, a non-zero
# INTERRUPT_CNTL / L1_TLB_CNTL / PROTECTION_FAULT_CNTL so the read-modify-write semantics are visible in the log
PRESETS = {"regMMMC_VM_FB_LOCATION_BASE": 0x0, "regMMMC_VM_FB_LOCATION_TOP": 0x7ff, "regGCMC_VM_FB_OFFSET": 0x0, "regRCC_CONFIG_MEMSIZE": 0x8000,
           "regINTERRUPT_CNTL": 0x10, "regMMMC_VM_MX_L1_TLB_CNTL": 0x80, "regGCMC_VM_MX_L1_TLB_CNTL": 0x80, "regMMVM_L2_PROTECTION_FAULT_CNTL": 0xc0000000,
           "regGCVM_L2_PROTECTION_FAULT_CNTL": 0xc0000000, "regDAGB1_WRCLI2": 0x12345678, "regRPB_ARB_CNTL": 0xffffffff,
           "regMMVM_INVALIDATE_ENG17_SEM": 0x1, "regMMVM_INVALIDATE_ENG17_ACK": 0x1}   # the pre-gart vmid-0 MM flush (review L6) acquires/acks at once
VEGA_EXTRA = (("athub", am.ATHUB_HWIP),)   # design v2 §3 _build_regs row: athub bound when GC == (9,4,0) (the amdev.py owner's change)
dev, log = run(ip_new, VEGA20, VEGA20_BASES, PRESETS, VEGA_EXTRA)
print("=== Vega 20 (MI50) ordered register access log: AM_SOC, AM_GMC, AM_IH init_hw, then the GC hub + PTE flags ===")
for l in log: print(l)
print()
writes = [l for l in log if l.startswith("W ")]
def wrote(name, val=None):
  rs = [l for l in writes if l.split("@")[0][2:].split("|").count(name)]
  return rs if val is None else [l for l in rs if l.endswith(f"<- {val:#x}")]
def idx(op, name): return next(i for i, l in enumerate(log) if l.startswith(op + " ") and name in l.split("@")[0][2:].split("|"))
check(not any(l.startswith("EXC") for l in log), "Vega 20 run raised nothing")
# B8 SOC (nbio_v7_4.c:208-212, 138-177, 233-245, 214-231; vega20_reg_init.c:72-73,80,98)
check(dev.regRCC_DOORBELL_APER_EN.addr[0] == 0xde0 and wrote("mmRCC_DOORBELL_APER_EN", 0x1), "RCC_DOORBELL_APER_EN (dword 0xde0) <- 1")
check(wrote("mmBIF_SDMA0_DOORBELL_RANGE", (0x200 << 2) | (20 << 16)), "BIF_SDMA0_DOORBELL_RANGE OFFSET=0x200 SIZE=20")
check(wrote("mmBIF_SDMA1_DOORBELL_RANGE", (0x214 << 2) | (20 << 16)), "BIF_SDMA1_DOORBELL_RANGE OFFSET=0x214 SIZE=20")
check(wrote("mmBIF_IH_DOORBELL_RANGE", (0x2f0 << 2) | (8 << 16)), "BIF_IH_DOORBELL_RANGE OFFSET=0x2f0 (=0x178<<1) SIZE=8")
check(wrote("mmDOORBELL_SELFRING_GPA_APER_BASE_LOW", 0x90000000) and wrote("mmDOORBELL_SELFRING_GPA_APER_BASE_HIGH", 0x1),
      "DOORBELL_SELFRING_GPA_APER_BASE = BAR2 bus address from config 0x18/0x1c with the type bits masked (0x1_9000_0000)")
check(wrote("mmDOORBELL_SELFRING_GPA_APER_CNTL", 0x3), "DOORBELL_SELFRING_GPA_APER_CNTL = EN|MODE(1)|SIZE(0) = 0x3")
check(not any("S2A_DOORBELL" in l for l in writes), "doorbell_enable() is a no-op on NBIO 7.4.0 (no S2A write)")
# B9 HDP / flush (hdp_v4_0.c:151-157; nbio_v7_4.c:792-803 + amdgpu_hdp.c:51-60)
check(wrote("mmHDP_MISC_CNTL", 0x1) and wrote("mmHDP_NONSURFACE_BASE", 0x0) and wrote("mmHDP_NONSURFACE_BASE_HI", 0x0), "HDP init: MISC_CNTL.FLUSH_INVALIDATE_CACHE=1, NONSURFACE_BASE/HI = vram_start(0)>>8/>>40")
check(dev.regBIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL.addr[0] == 0xe17 and wrote("mmBIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL", 0x0),
      "flush_hdp: write 0 to BIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL (dword 0xe17)")
check(dev.regRCC_CONFIG_MEMSIZE.addr[0] == 0xde3 and any("R mmRCC_CONFIG_MEMSIZE@0x00de3" in l for l in log), "flush_hdp: then reads RCC_CONFIG_MEMSIZE (dword 0xde3)")
i_flush = idx("W", "mmBIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL")
check(log[i_flush + 1].startswith("R mmRCC_CONFIG_MEMSIZE@0x00de3"), "flush_hdp order: write 0 then read memsize")
# B6 (gfxhub_v1_0.c:34-37, gmc_v9_0.c:1664-1668, 1906-1923)
check(dev.regGCMC_VM_FB_OFFSET.addr[0] == 0x296b and any("R mmGCMC_VM_FB_OFFSET@0x0296b" in l for l in log), "init_sw reads GC MC_VM_FB_OFFSET (dword 0x296b)")
check(dev.gmc.trans_futher is False and dev.gmc.paddr_base == 0 and dev.gmc.mc_base == 0, "trans_futher False; paddr_base = FB_OFFSET(0)<<24 = 0")
dev2, _ = run(ip_new, VEGA20, VEGA20_BASES, {**PRESETS, "regGCMC_VM_FB_OFFSET": 0x10}, VEGA_EXTRA)
check(dev2.gmc.paddr_base == 0x10 << 24 and dev2.paddr2xgmi(0x1000) == (0x10 << 24) + 0x1000, "a non-zero FB_OFFSET (0x10) folds into paddr_base (0x10000000) and paddr2xgmi")
check(dev.gmc.address_space_mask == (1 << 48) - 1, "address_space_mask stays 48-bit (gmc_v9_0.c:1982)")
# B10 (gmc_v9_0.c:333-341 + soc15.c:455-485; gfxhub_v1_0.c / mmhub_v1_0.c)
check(wrote("mmDAGB1_WRCLI2", (0x12345678 & ~0x7) | (0xfe5fe0fa & 0x7)), "golden DAGB1_WRCLI2: (read & ~0x7) | (0xfe5fe0fa & 0x7)")
check(wrote("mmMMEA1_DRAM_WR_CLI2GRP_MAP0", 0x55555565 & 0x30), "golden MMEA1_DRAM_WR_CLI2GRP_MAP0: (0 & ~0x30) | (0x55555565 & 0x30)")
check(wrote("mmRPB_ARB_CNTL", (0xffffffff & ~0xff00) | (0x800 & 0xff00)), "golden RPB_ARB_CNTL: (read & ~0xff00) | (0x800 & 0xff00)")
check(wrote("mmRPB_ARB_CNTL2", 0x00080008 & 0x00ff00ff), "golden RPB_ARB_CNTL2: (0 & ~0xff00ff) | (0x80008 & 0xff00ff)")
i_gold, i_hdp, i_fl, i_mmhub = (idx("W", k) for k in ("mmDAGB1_WRCLI2", "mmHDP_MISC_CNTL", "mmBIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL",
                                                      "mmMMVM_L2_PROTECTION_FAULT_CNTL"))
check(i_gold < i_hdp < i_fl < i_mmhub, "GMC order: golden -> HDP init -> flush -> MM hub (set_fault_enable_default first) = gmc_v9_0.c:2181-2217")
en11 = sum(1 << b for b in range(2, 13))   # the 11 *_PROTECTION_FAULT_ENABLE_DEFAULT fields = bits 2..12 of VM_L2_PROTECTION_FAULT_CNTL
check(wrote("mmMMVM_L2_PROTECTION_FAULT_CNTL", 0xc0000000 | en11), "MM hub set_fault_enable_default(true): 11 enable_default bits set, CRASH bits untouched (mmhub_v1_0.c:432-464)")
check(wrote("mmGCVM_L2_PROTECTION_FAULT_CNTL", en11), "GC hub set_fault_enable_default(true): 11 enable_default bits set, CRASH_ON_*_FAULT = !value = 0 (gfxhub_v1_0.c:381-410)")
mtype_uc = dev.soc.module.MTYPE_UC
r1 = dev.regMMMC_VM_MX_L1_TLB_CNTL
l1_fields = dict(enable_l1_tlb=1, system_access_mode=3, enable_advanced_driver_model=1, system_aperture_unmapped_access=0, mtype=mtype_uc, atc_en=1)
l1 = (0x80 & ~r1.fields_mask(*l1_fields)) | r1.encode(**l1_fields)   # RMW: the preset's bit 7 (an eco_bits bit) survives, like Linux's REG_SET_FIELD chain
check(wrote("mmMMMC_VM_MX_L1_TLB_CNTL", l1) and wrote("mmGCMC_VM_MX_L1_TLB_CNTL", l1) and l1 & 0x2000, f"MC_VM_MX_L1_TLB_CNTL on both hubs carries ATC_EN (0x2000): {l1:#x}")
for hub in ("MM", "GC"):
  v = int([l for l in writes if f"W mm{hub}VM_L2_CNTL@" in l][0].split("<- ")[1], 16)
  check(v & (1 << 11) == 0 and v & 0x3 == 0x3, f"{hub} VM_L2_CNTL: ENABLE_DEFAULT_PAGE_OUT_TO_SYSTEM_MEMORY (bit 11) clear, L2 cache + fragment processing on: {v:#x}")
  v3 = int([l for l in writes if f"W mm{hub}VM_L2_CNTL3@" in l][0].split("<- ")[1], 16)
  r3 = dev.reg(f"reg{hub}VM_L2_CNTL3")
  linux3 = (0x80100007 & ~r3.fields_mask('bank_select', 'l2_cache_bigk_fragment_size')) | r3.encode(bank_select=9, l2_cache_bigk_fragment_size=6)
  check(v3 == linux3 == 0x80130009, f"{hub} VM_L2_CNTL3 == mmVM_L2_CNTL3_DEFAULT(0x80100007) with BANK_SELECT=9, BIGK_FRAGMENT_SIZE=6: {v3:#x}")
  v4 = int([l for l in writes if f"W mm{hub}VM_L2_CNTL4@" in l][0].split("<- ")[1], 16)
  check(v4 == (0xc1 & ~0xc0) == 0x1, f"{hub} VM_L2_CNTL4 == mmVM_L2_CNTL4_DEFAULT(0xc1) minus VMC_TAP_PDE/PTE_REQUEST_PHYSICAL: {v4:#x}")
  vc = int([l for l in writes if f"W mm{hub}VM_CONTEXT0_CNTL@" in l][0].split("<- ")[1], 16)
  d = dev.reg(f"reg{hub}VM_CONTEXT0_CNTL").decode(vc)
  check(d['page_table_depth'] == 3 and d['page_table_block_size'] == 0 and d['enable_context'] == 1, f"{hub} VM_CONTEXT0_CNTL depth=3 block_size=0 (translate_further false)")
pte = {l.split()[1:3][0] + " " + l.split()[2]: l for l in log if l.startswith("pte ")}
check(all("huge=1" in l for k, l in pte.items() if "table=0" in k and "PTB" not in k) and not any(int(l.split("flags=")[1].split()[0], 16) & (am.AMDGPU_PTE_TF | (0x1f << 59)) for l in pte.values()),
      "PTE flags: no PDE_BFS / PTE_TF anywhere; PDE_PTE marks a huge page at PDB2/PDB1/PDB0 (gmc_v9_0.c:1027-1028)")
# B11 (vega20_ih.c:244-284, 307-388; nbio_v7_4.c:290-304)
check(wrote("mmINTERRUPT_CNTL2", 0x7f0000000 >> 8), "INTERRUPT_CNTL2 = host dummy page paddr >> 8")
check(wrote("mmINTERRUPT_CNTL", 0x10 & ~0x9), "INTERRUPT_CNTL: IH_DUMMY_RD_OVERRIDE=0, IH_REQ_NONSNOOP_EN=0, other bits kept (RMW)")
i_ic, i_rb = idx("W", "mmINTERRUPT_CNTL"), idx("W", "mmIH_RB_CNTL")
check(i_ic < i_rb, "ih_control before any IH ring programming/enable")
rb0 = [l for l in writes if "W mmIH_RB_CNTL@" in l]
check(all(dev.regIH_RB_CNTL.decode(int(l.split("<- ")[1], 16))['rptr_rearm'] == 0 for l in rb0), "IH_RB_CNTL ring 0: RPTR_REARM = 0 (no MSI)")
check(not any("MSI_STORM" in l for l in log) and not any("RETRY_CAM" in l or "RETRY_INT_CAM" in l for l in writes), "no IH_MSI_STORM_CTRL access, no retry-CAM writes")
check(wrote("mmIH_STORM_CLIENT_LIST_CNTL") and wrote("mmIH_INT_FLOOD_CNTL"), "IH_STORM_CLIENT_LIST_CNTL / IH_INT_FLOOD_CNTL still written (fields exist in osssys_4_2_0)")
last_rb = [l for l in writes if "W mmIH_RB_CNTL@" in l][-1]
check(dev.regIH_RB_CNTL.decode(int(last_rb.split("<- ")[1], 16))['rb_enable'] == 1, "ring 0 enabled last")
# review 2026-10-13 L7: vega20_ih_rb_cntl (vega20_ih.c:202-203, :208-209) on every ring
rb0v = dev.regIH_RB_CNTL.decode(int(rb0[0].split("<- ")[1], 16)); rb1 = [l for l in writes if "W mmIH_RB_CNTL_RING1@" in l]; rb1v = int(rb1[0].split("<- ")[1], 16)
check(rb0v['wptr_writeback_enable'] == 1 and rb0v['wptr_overflow_enable'] == 1, "IH_RB_CNTL ring 0: WPTR_WRITEBACK_ENABLE=1, WPTR_OVERFLOW_ENABLE=1")
check((rb1v >> 8) & 1 == 1 and dev.regIH_RB_CNTL_RING1.decode(rb1v)['wptr_overflow_enable'] == 1 and dev.regIH_RB_CNTL_RING1.decode(rb1v)['rb_full_drain_enable'] == 1,
      f"IH_RB_CNTL_RING1: bit 8 (WPTR_WRITEBACK_ENABLE, absent from the RING1 table) raw, WPTR_OVERFLOW_ENABLE=1, RB_FULL_DRAIN_ENABLE=1: {rb1v:#x}")
check(wrote("mmIH_DOORBELL_RPTR", 0) and wrote("mmIH_DOORBELL_RPTR_RING1", 0), "IH_DOORBELL_RPTR ENABLE=0 on both rings (Linux !use_doorbell form; AM rings no IH doorbell - listed)")
# review L6: vmid-0 MM-hub ENG17 flush BEFORE the MM hub's gart_enable (gmc_v9_0.c:2208-2212 -> :2217), after the HDP init/flush (:2193-2196)
i_req, i_agp = idx("W", "mmMMVM_INVALIDATE_ENG17_REQ"), idx("W", "mmMMMC_VM_AGP_BASE")
check(i_flush < i_req < i_agp and dev.regMMVM_INVALIDATE_ENG17_REQ.decode(int(log[i_req].split("<- ")[1], 16))['per_vmid_invalidate_req'] == 1,
      "MM hub: ENG17 vmid-0 flush after the HDP flush and before init_hub's first aperture write (Linux order)")
check(not any("W mmGCVM_INVALIDATE_ENG17_REQ@" in l for l in writes[:idx("W", "mmGCMC_VM_AGP_BASE")]), "GC hub: no pre-gart flush (AM's hub_initted guard; skip listed)")
# review L12: SYSTEM_APERTURE_HIGH_ADDR = Linux fb_end >> 18 = ((TOP << 24) | 0xffffff) >> 18 on both hubs
hi_want = ((0x7ff << 24) | 0xffffff) >> 18
check(wrote("mmMMMC_VM_SYSTEM_APERTURE_HIGH_ADDR", hi_want) and wrote("mmGCMC_VM_SYSTEM_APERTURE_HIGH_ADDR", hi_want), f"MC_VM_SYSTEM_APERTURE_HIGH_ADDR = {hi_want:#x} on MM and GC (gfxhub_v1_0.c:119-122, amdgpu_gmc.c:221)")
print()

# --- (2) non-Vega paths byte-identical to the mi50 branch ---------------------------------------------------------------------------------
fork = pathlib.Path(os.getenv("MI50_FORK_GIT", pathlib.Path(__file__).resolve().parents[2]))
# integration 2026-10-13: the "unmodified" baseline is the pre-integration mi50 commit d4697a019 (every implementer branched from it); after the
# merge `mi50` itself carries the Vega 20 branches, so comparing against it would prove nothing. Override with env MI50_BASELINE_REF.
base_src = subprocess.run(["git", "-C", str(fork), "show", os.environ.get("MI50_BASELINE_REF", "d4697a019") + ":tinygrad/runtime/support/am/ip.py"], check=True, capture_output=True, text=True).stdout
with tempfile.TemporaryDirectory() as td:
  p = pathlib.Path(td) / "ip_mi50.py"; p.write_text(base_src)
  spec = importlib.util.spec_from_file_location("ip_mi50", p); ip_old = importlib.util.module_from_spec(spec); spec.loader.exec_module(ip_old)
check(ip_old.AM_SOC is not ip_new.AM_SOC and base_src != pathlib.Path(ip_new.__file__).read_text(), "baseline = unmodified mi50 ip.py (differs from the worktree's)")
# gfx11-shaped: GC 11.0.2 = the RX 7600 XT (extra/mi50/t4_tables.py:25; eGPU Control Center Plan.md:24); its NBIO/HDP/OSSSYS/MMHUB versions are not
# recorded in any tree here, so every non-Vega version the fork's reg_files carries (autogen/am/__init__.py:16-20) is swept
GFX11 = {"gc": [(11,0,2)], "nbio": [(4,3,0), (7,2,0), (7,7,0), (7,11,0)], "hdp": [(6,0,0), (7,0,0), (4,4,2)], "osssys": [(6,0,0), (6,1,0), (7,0,0), (4,4,2)],
         "mmhub": [(3,0,0), (3,0,1), (3,0,2), (3,3,0), (4,1,0)], "mp": [(13,0,7)]}
MI300 = {am.GC_HWIP: (9,4,3), am.NBIO_HWIP: (7,9,0), am.NBIF_HWIP: (7,9,0), am.HDP_HWIP: (4,4,2), am.OSSSYS_HWIP: (4,4,2), am.MMHUB_HWIP: (1,8,0),
         am.MP0_HWIP: (13,0,6), am.MP1_HWIP: (13,0,6), am.SDMA0_HWIP: (4,4,2)}      # the versions the existing gates name (ip.py, amdev.py _build_regs)
GFX12 = {am.GC_HWIP: (12,0,0), am.SDMA0_HWIP: (7,0,0), am.MMHUB_HWIP: (4,1,0), am.NBIO_HWIP: (6,3,1), am.NBIF_HWIP: (6,3,1), am.MP0_HWIP: (14,0,2),
         am.MP1_HWIP: (14,0,2), am.HDP_HWIP: (7,0,0), am.OSSSYS_HWIP: (7,0,0)}       # test/mockgpu/am/amgpu.py:11-14
sets = [("MI300", MI300), ("gfx12 (mockgpu)", GFX12)]
for gc, nb, hd, os_, mh, mp in itertools.product(*GFX11.values()):
  sets.append((f"gfx11 gc{gc} nbio{nb} hdp{hd} osssys{os_} mmhub{mh} mp{mp}", {am.GC_HWIP: gc, am.NBIO_HWIP: nb, am.NBIF_HWIP: nb, am.HDP_HWIP: hd,
               am.OSSSYS_HWIP: os_, am.MMHUB_HWIP: mh, am.MP0_HWIP: mp, am.MP1_HWIP: mp, am.SDMA0_HWIP: (6,0,0)}))
NV_PRESETS = {"regMMMC_VM_FB_LOCATION_BASE": 0x0, "regMMMC_VM_FB_LOCATION_TOP": 0x7ff}
same, differ, raised, clean = 0, [], [], []
for name, iv in sets:
  _, log_old = run(ip_old, iv, MOCK_BASES, NV_PRESETS)
  _, log_new = run(ip_new, iv, MOCK_BASES, NV_PRESETS)
  (raised if any(l.startswith("EXC") for l in log_old) else clean).append(name)
  if log_old == log_new: same += 1
  else:
    differ.append(name)
    for a, b in zip(log_old, log_new):
      if a != b: print(f"  DIFF in {name}:\n    mi50: {a}\n    new : {b}"); break
check(not differ, f"{same}/{len(sets)} non-Vega version sets give byte-identical access logs on mi50 and this worktree")
check("MI300" in clean and "gfx12 (mockgpu)" in clean, f"MI300 and gfx12 sets complete without raising; {len(clean) - 2} gfx11 sets complete, {len(raised)} raise "
      f"identically on both (a version whose table lacks a register AM names, e.g. {raised[0].split(' ', 1)[1] if raised else '-'}: "
      f"{next(l for l in run(ip_old, dict(sets[[n for n, _ in sets].index(raised[0])][1]), MOCK_BASES, NV_PRESETS)[1] if l.startswith('EXC')) if raised else '-'})")
print("gfx11 sets completing on both:", ", ".join(n.removeprefix("gfx11 ") for n in clean if n.startswith("gfx11")))
print("\nnon-Vega sample (MI300) log head:"); print("\n".join(run(ip_new, MI300, MOCK_BASES, NV_PRESETS)[1][:12]))

print("\nALL OK" if not fails else f"\n{len(fails)} FAIL(S):\n" + "\n".join(fails))
sys.exit(1 if fails else 0)
