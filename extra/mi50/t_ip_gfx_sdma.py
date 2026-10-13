#!/usr/bin/env python3
"""Offline check for the mi50 fork's AM_GFX / AM_SDMA Vega 20 branches (Vega20 Port Design v2 B14/B15). Needs no card.

A fake AMDev records every register write (and the udelays, doorbell and VRAM writes) while AM_GFX/AM_SDMA run against the REAL register
tables (gc_9_0 / sdma_4_2 for Vega 20, gc_11_0_0 / gc_9_4_3 + sdma_4_4_2 + nbio for the other cards). Checks:
  (1) Vega 20 init_hw + setup_ring write log follows the Linux order: golden (gfx_v9_0.c:1115-1157) -> constants_init (:2665-2715) ->
      rlc_resume (:3196-3244) -> cp_resume (:3946-4000); SDMA golden (sdma_v4_0.c:535-545) -> enable -> ctx_switch -> SEM/PAGE/UTC_L1/HALT -> ULV.
  (2) exact values: GB_ADDR_CONFIG golden, SH_MEM_BASES per vmid, GDS zeroing, LB registers, CP_ME_CNTL / CP_MEC_CNTL masks, MEC doorbell range,
      CSB (size per gfx_v9_0_get_csb_size), MQD deltas, SDMA phase quantum, page-queue doorbells, ULV gate, page-queue gate (fw 122 vs 145).
  (3) gfx11 (RX 7600 XT, GC 11.0.2) and MI300 (GC 9.4.3, SDMA 4.4.2, NBIO 7.9.0) write logs are IDENTICAL between this ip.py and the
      unmodified mi50 branch's ip.py (git show mi50:tinygrad/runtime/support/am/ip.py), for init_hw / setup_ring / set_clockgating_state /
      reset_mec / fini_hw.
Run: PYTHONPATH=<fork> python3 extra/mi50/t_ip_gfx_sdma.py   (env MI50_FORK_GIT=<path of the git checkout> to locate the mi50 baseline; default = this tree)
"""
import os, sys, time, types, functools, subprocess, pathlib, collections
from tinygrad.runtime.autogen.am import am, vega_offsets as vo, clearstate_gfx9 as cs
from tinygrad.runtime.support.amd import import_asic_regs
from tinygrad.runtime.support.am.amdev import AMRegister
import tinygrad.runtime.support.am.ip as ip_new

fails: list[str] = []
def check(cond, msg):
  print(("ok   " if cond else "FAIL ") + msg)
  if not cond: fails.append(msg)

class Obj:
  def __init__(self, **kw): self.__dict__.update(kw)

class FakeDev:
  """Enough of AMDev for AM_GFX / AM_SDMA: real register tables + a write log. Reads return the last written value, else 0."""
  def __init__(self, ip_ver, tables, regs_offset, gc_info=None, fw_versions=None, pci_device=0x66a1, harvested=None):
    self.ip_ver, self.regs_offset, self.devfmt, self.partial_boot, self.is_err_state = ip_ver, regs_offset, "fake", False, False
    self.harvested = collections.defaultdict(set, harvested or {})
    for prefix, ver, hwip in tables:
      self.__dict__.update(import_asic_regs(prefix, ver, cls=functools.partial(AMRegister, adev=self, bases=self.regs_offset[hwip])))
    self.log: list[tuple] = []; self.vals: dict[int, int] = {}
    self.mc_base, self._pa = 0x8000000000, 0x1000   # fake FB at a high MC address so hi32 paths are exercised
    self.vram_buf = memoryview(bytearray(4 << 20))
    self.vram = Obj(view=lambda off, size, fmt='B': self.vram_buf[off:off+size].cast(fmt))
    self.mm = Obj(palloc=self._palloc)
    self.doorbell64 = DoorbellLog(self)
    fwv = fw_versions or {}
    self.fw = Obj(ucode_start={'MEC': 0x10000, 'PFP': 0x20000, 'ME': 0x30000}, load_fw=lambda name, *h: (None, self._fw_hdr(name, fwv)))
    self.gmc = Obj(init_hub=lambda ip, insts: self.log.append(("init_hub", ip, tuple(insts))), flush_hdp=lambda: self.log.append(("flush_hdp",)))
    self.soc = Obj(module=Obj(SH_MEM_ADDRESS_MODE_64=0, SH_MEM_ALIGNMENT_MODE_UNALIGNED=3),
                   doorbell_enable=lambda **kw: self.log.append(("doorbell_enable", tuple(sorted(kw.items())))))
    self.psp = Obj(_spatial_partition_cmd=lambda m: self.log.append(("spatial_partition", m)))
    self.pci_dev = Obj(read_config=lambda off, sz: {0x2: pci_device}[off])
    self.gc_info = gc_info
    self.aids = [0] + [aid for aid in range(1, 4) if any(i >> 2 == aid for i in self.regs_offset.get(am.SDMA0_HWIP, {}))]
  def _fw_hdr(self, name, fwv):
    if name == "vega20_sdma.bin": return Obj(header=Obj(ucode_version=fwv.get("sdma", 145)))
    if name == "vega20_rlc.bin": return Obj(header=Obj(header_version_major=2, header_version_minor=fwv.get("rlc_minor", 1)))
    raise RuntimeError(name)
  def _palloc(self, size, align=0x1000, zero=True, boot=False):
    p, self._pa = self._pa, (self._pa + size + 0xfff) & ~0xfff
    self.log.append(("palloc", size, boot)); return p
  def paddr2mc(self, p): return self.mc_base + p
  def reg(self, name): return getattr(self, name)
  def __getattr__(self, name):   # the fork's AMDev.__getattr__ (amdev.py): reg* -> mm* fallback
    if name.startswith('reg') and (alt:='mm' + name[3:]) in self.__dict__: return self.__dict__[alt]
    raise AttributeError(f"{type(self).__name__} has no attribute {name}")
  selfclear: dict[int, int] = {}   # bits the hardware clears on its own (RLC_SAFE_MODE.CMD): modelled so the old/new comparison runs can finish
  def rreg(self, addr): return self.vals.get(addr, 0) & ~self.selfclear.get(addr, 0)
  clears_on_write: dict[int, int] = {}   # DEQUEUE_REQUEST address -> CP_HQD_ACTIVE address it clears (the HQD dequeues), so the old/new fini/reset_mec runs can finish
  def wreg(self, addr, val):
    self.log.append(("w", addr, val)); self.vals[addr] = val
    if addr in self.clears_on_write: self.vals[self.clears_on_write[addr]] = 0
  def wreg_pair(self, base, lo, hi, val, inst=0):
    self.reg(f"{base}{lo}").write(val & 0xffffffff, inst=inst); self.reg(f"{base}{hi}").write(val >> 32, inst=inst)
  def name_of(self, addr):
    for k, v in self.__dict__.items():
      if isinstance(v, AMRegister):
        for inst, a in v.addr.items():
          if a == addr: return f"{k}[{inst}]"
    return f"{addr:#x}"

class DoorbellLog:
  def __init__(self, dev): self.dev = dev
  def __setitem__(self, idx, val): self.dev.log.append(("doorbell", idx, val))

def log_sleep(log):
  def sleep(s): log.append(("sleep", s))
  return sleep

def run(mod, dev, what):
  """Run the scenario `what` with module `mod`'s AM_GFX/AM_SDMA on `dev`; returns the write log (register writes resolved to names)."""
  real_sleep, time.sleep = time.sleep, log_sleep(dev.log)
  try:
    gfx, sdma = mod.AM_GFX(dev), mod.AM_SDMA(dev)
    dev.gfx, dev.sdma = gfx, sdma
    gfx.init_sw(); sdma.init_sw()
    if "init_hw" in what: gfx.init_hw(); sdma.init_hw()
    if "setup_ring" in what:
      gfx.setup_ring(ring_addr=0x200000000000, ring_size=0x100000, rptr_addr=0x200001000000, wptr_addr=0x200001000100, eop_addr=0x200002000000, eop_size=0x1000, idx=0, aql=False)
      sdma.setup_ring(ring_addr=0x200003000000, ring_size=0x10000, rptr_addr=0x200001000200, wptr_addr=0x200001000300, idx=0)
    if "clockgating" in what: gfx.set_clockgating_state()
    if "reset_mec" in what: gfx.reset_mec()
    if "fini" in what: sdma.fini_hw(); gfx.fini_hw()
  finally: time.sleep = real_sleep
  return [(e[0], dev.name_of(e[1]), e[2]) if e[0] == "w" else e for e in dev.log]

# ---------------------------------------------------------------- Vega 20 ----------------------------------------------------------------
GC_V20 = {0: (vo.GC_BASE__INST0_SEG0, vo.GC_BASE__INST0_SEG1, vo.GC_BASE__INST0_SEG2, vo.GC_BASE__INST0_SEG3, vo.GC_BASE__INST0_SEG4)}
SDMA_V20 = {0: (vo.SDMA0_BASE__INST0_SEG0,), 1: (vo.SDMA1_BASE__INST0_SEG0,)}
def vega(gc_info=None, **kw):
  ver = {am.GC_HWIP: (9,4,0), am.SDMA0_HWIP: (4,2,0), am.NBIO_HWIP: (7,4,0), am.MP0_HWIP: (11,0,2), am.MMHUB_HWIP: (9,4,0)}
  # HARNESS values for loop coverage only (SE/SH/CU of the MI50 come from T2b's VBIOS gfx_info, design B4) - not a card fact
  return FakeDev(ver, [("gc", (9,4,0), am.GC_HWIP), ("sdma", (4,2,0), am.SDMA0_HWIP)], {am.GC_HWIP: GC_V20, am.SDMA0_HWIP: SDMA_V20},
                 gc_info=gc_info or Obj(gc_num_se=4, gc_num_sh_per_se=1, gc_num_cu_per_sh=16), **kw)

dev = vega()
log = run(ip_new, dev, {"init_hw", "setup_ring"})
writes = [(n, v) for k, n, v in [e if e[0] == "w" else ("x", None, None) for e in log] if k == "w"]
names = [n for n, _ in writes]
def first(name, after=0):
  for i in range(after, len(names)):
    if names[i].startswith(name): return i
  raise AssertionError(f"{name} not written after {after}")
def all_idx(name): return [i for i, n in enumerate(names) if n.startswith(name)]
def val_at(i): return writes[i][1]

print(f"info Vega 20 log: {len(log)} entries, {len(writes)} register writes")
pallocs = [e for e in log if e[0] == "palloc"]
check(pallocs == [("palloc", 0x1000, True)] * 2 + [("palloc", (csb_bytes:=(len(dev.gfx.csb_dws) * 4 + 0xfff) & ~0xfff), True), ("palloc", 0x100000, True), ("palloc", 0x2000, True), ("palloc", 0x1000, True), ("palloc", 0x1000, True)],
      f"init_sw pallocs: 2 MQDs, csb {csb_bytes:#x} B, page rings 1 MiB (instance 0, amdgpu_ring.c:247-248) and 8 KiB (instance 1), 2 wb pages, all boot memory: {pallocs}")
check(any(e == ("init_hub", "GC", (0,)) for e in log), "init_hub('GC') issued")
# (1) golden tables first, in Linux order; GB_ADDR_CONFIG = (read & ~0xf3e777ff) | 0x22014042 with read == 0 -> 0x22014042
i_db2 = first("mmDB_DEBUG2"); i_gb = first("mmGB_ADDR_CONFIG["); i_gbr = first("mmGB_ADDR_CONFIG_READ"); i_cam = first("mmGRBM_CAM_DATA")
check(i_db2 == 0, "first write is golden_settings_gc_9_0[0] DB_DEBUG2 (gfx_v9_0.c:678)")
check(val_at(i_db2) == 0x00000400 and val_at(i_gb) == 0x22014042 and val_at(i_gbr) == 0x22014042, f"golden values: DB_DEBUG2 {val_at(i_db2):#x} (0x400), GB_ADDR_CONFIG {val_at(i_gb):#x} (:727 -> 0x22014042)")
check(val_at(first("mmTCP_CHAN_STEER_LO")) == 0xb5d3f197 and val_at(i_cam) == 0x2544c382, "raw writes for and_mask 0xffffffff entries (TCP_CHAN_STEER_LO :692, GRBM_CAM_DATA :819)")
n_golden = len(ip_new.AM_GFX.GOLDEN_GC_9_0) + len(ip_new.AM_GFX.GOLDEN_GC_9_0_VG20) + len(ip_new.AM_GFX.GOLDEN_GC_9_X_COMMON)
check(n_golden == 20 + 11 + 3 and i_cam == n_golden - 1, f"{n_golden} golden writes = 20 + 11 + 3 entries (gfx_v9_0.c:676-698, 722-735, 815-820), last is GRBM_CAM_DATA")
check(not any(n.startswith("mmTCP_CNTL") for n in names), "no TCP_CNTL |= 0x20000000 (gfx11 golden) on Vega 20 (B14.4)")
# constants_init
i_grbm = first("mmGRBM_CNTL["); check(i_grbm == n_golden and val_at(i_grbm) == 0xff, "GRBM_CNTL.READ_TIMEOUT=0xff right after golden (:2672)")
gi = all_idx("mmGRBM_GFX_INDEX")
check(len(gi) >= 2 and val_at(gi[0]) == dev.mmGRBM_GFX_INDEX.encode(instance_broadcast_writes=1, se_index=0, sh_index=0), "get_cu_info: select_se_sh(0,0,0xffffffff) (:7919, :2503-2524)")
check(not any(n.startswith("mmGC_USER_SHADER_ARRAY_CONFIG") for n in names), "get_cu_info writes no GC_USER_SHADER_ARRAY_CONFIG without amdgpu.disable_cu (:7868-7869)")
bases = [(i, val_at(i)) for i in all_idx("mmSH_MEM_BASES")]
cfgs = [(i, val_at(i)) for i in all_idx("mmSH_MEM_CONFIG")]
cntl = [(i, dev.mmGRBM_GFX_CNTL.decode(val_at(i))['vmid']) for i in all_idx("mmGRBM_GFX_CNTL")]
def vmid_of(i): return max((v for j, v in cntl if j < i), default=None)
per_vmid = {vmid_of(i): v for i, v in bases}
check(per_vmid[0] == dev.mmSH_MEM_BASES.encode(shared_base=1, private_base=2), f"vmid 0 SH_MEM_BASES keeps AM's apertures {per_vmid[0]:#x} (design B14.5)")
check(all(per_vmid[v] == (0x1000 | 0x2000 << 16) for v in range(1, 8)), f"vmid 1..7 SH_MEM_BASES private 0x1000 / shared 0x2000 (:2701-2705, gmc_v9_0.c:1605/1608): {per_vmid[1]:#x}")
check(all(per_vmid[v] == (0x6000 | 0x6000 << 16) for v in range(8, 16)), f"vmid 8..15 SH_MEM_BASES DEFAULT_SH_MEM_BASES (:2589, :2602, :2613): {per_vmid[8]:#x}")
per_vmid_cfg = {vmid_of(i): v for i, v in cfgs}
check(all(per_vmid_cfg[v] == dev.mmSH_MEM_CONFIG.encode(alignment_mode=3, retry_disable=1) for v in range(8)) and all(per_vmid_cfg[v] == dev.mmSH_MEM_CONFIG.encode(alignment_mode=3) for v in range(8, 16)),
      "SH_MEM_CONFIG: vmid 0..7 UNALIGNED|RETRY_DISABLE (:2689-2692, noretry default), vmid 8..15 UNALIGNED only (:2604-2606)")
gds = [n for n in names if n.startswith("mmGDS_")]
exp_gds = [f"mm{r}[0]" for i in range(8, 16) for r in (f"GDS_VMID{i}_BASE", f"GDS_VMID{i}_SIZE", f"GDS_GWS_VMID{i}", f"GDS_OA_VMID{i}")] + \
          [f"mm{r}[0]" for i in range(1, 16) for r in (f"GDS_VMID{i}_BASE", f"GDS_VMID{i}_SIZE", f"GDS_GWS_VMID{i}", f"GDS_OA_VMID{i}")]
check(gds == exp_gds and all(val_at(i) == 0 for i in all_idx("mmGDS_")), f"GDS zeroing: vmid 8..15 (:2620-2625) then 1..15 (:2638-2643), {len(gds)} writes of 0")
# rlc_resume order
i_rlc_off = first("mmRLC_CNTL["); i_cg = first("mmRLC_CGCG_CGLS_CTRL"); i_csib_hi = first("mmRLC_CSIB_ADDR_HI"); i_csib_lo = first("mmRLC_CSIB_ADDR_LO"); i_csib_len = first("mmRLC_CSIB_LENGTH")
i_srm = first("mmRLC_SRM_CNTL"); i_thr1 = first("mmRLC_LB_THR_CONFIG_1"); i_lbcntl = first("mmRLC_LB_CNTL["); i_spm = first("mmRLC_SPM_MC_CNTL"); i_rlc_on = first("mmRLC_CNTL[", i_rlc_off + 1)
check(val_at(i_rlc_off) == 0 and i_rlc_off > max(all_idx("mmGDS_")), "rlc_stop: RLC_CNTL.RLC_ENABLE_F32=0 (:3126) after constants_init")
i_gui_off = first("mmCP_INT_CNTL_RING0", i_rlc_off)
check(i_rlc_off < i_gui_off < i_cg < i_csib_hi < i_csib_lo < i_csib_len < i_srm < i_thr1 < i_lbcntl < i_spm < i_rlc_on,
      "rlc_resume order: RLC off (:3126) -> gui-idle off on CP_INT_CNTL_RING0 (:3127, :2762-2770) -> CGCG_CGLS_CTRL=0 (:3208) -> CSIB HI/LO/LENGTH (:2777-2782) -> SRM_ENABLE (:2927) -> lbpw (:1764..) -> RLC_LB_CNTL -> SPM_MC_CNTL (:3239) -> RLC on (:3145)")
check(val_at(i_gui_off) == 0 and not any(n.startswith("mmCP_INT_CNTL[") for n in names), "gui-idle bits go through CP_INT_CNTL_RING0, never CP_INT_CNTL (:2762)")
csb_dws = 2 + 3 + sum(2 + c for c, _, _ in cs.gfx9_cs_data[0][1]) + 2 + 2
check(val_at(i_csib_len) == csb_dws == len(dev.gfx.csb_dws), f"RLC_CSIB_LENGTH = {val_at(i_csib_len)} dwords = gfx_v9_0_get_csb_size (:1624-1650)")
check(val_at(i_csib_hi) == dev.gfx.csb_mc >> 32 and val_at(i_csib_lo) == dev.gfx.csb_mc & 0xfffffffc, "RLC_CSIB_ADDR_HI/LO = csb MC address (:2777-2780)")
csb = dev.vram.view(dev.gfx.csb_paddr, csb_dws * 4, fmt='I').tolist()
check(csb[0] == 0xc0004a00 and csb[1] == 2 << 28 and csb[2] == 0xc0012800 and csb[3] == csb[4] == 0x80000000, f"csb preamble {[hex(x) for x in csb[:5]]} (amdgpu_gfx.c:2851-2856)")
check(csb[5] == (3 << 30) | (0x69 << 8) | (212 << 16) and csb[6] == 0 and csb[-4] == 0xc0004a00 and csb[-3] == 3 << 28 and csb[-2] == 0xc0001200 and csb[-1] == 0, "csb first SET_CONTEXT_REG(212 @ 0xa000-0xa000) and end preamble (:2880-2881, :2900-2904)")
check(val_at(i_srm) == 1, "RLC_SRM_CNTL.SRM_ENABLE=1 with the v2.1 RLC header (:3104-3110)")
lb = {n.split('[')[0]: val_at(i) for i, n in enumerate(names) if n.startswith("mmRLC_LB") or n.startswith("mmRLC_GPM_GENERAL_7")}
check(lb["mmRLC_LB_THR_CONFIG_1"] == 0x7F and lb["mmRLC_LB_THR_CONFIG_2"] == 0x033388F8 and lb["mmRLC_LB_THR_CONFIG_3"] == 0x77 and lb["mmRLC_LB_THR_CONFIG_4"] == (0x10 | 0x27 << 8 | 0x02FA << 16)
      and lb["mmRLC_LB_CNTR_INIT"] == 0 and lb["mmRLC_LB_CNTR_MAX"] == 0x800 and lb["mmRLC_LB_INIT_CU_MASK"] == 0xffffffff and lb["mmRLC_LB_PARAMS"] == 0x033F1006
      and lb["mmRLC_GPM_GENERAL_7"] == 0x00C00000, f"gfx_v9_4_init_lbpw values (:1764-1790): LB_PARAMS {lb['mmRLC_LB_PARAMS']:#x}")
lbc = [val_at(i) for i in all_idx("mmRLC_LB_CNTL[")]
check(lbc == [0x80000094, 0x80000094], f"RLC_LB_CNTL {[hex(x) for x in lbc]}: SPIM_ACTIVE|HYST 9|RESERVED 0x80000 (:1799-1802), then LOAD_BALANCE_ENABLE=0 (:3233, amdgpu_lbpw=-1)")
ao = [val_at(i) for i in all_idx("mmRLC_LB_ALWAYS_ACTIVE_CU_MASK")]; pg = [val_at(i) for i in all_idx("mmRLC_PG_ALWAYS_ON_CU_MASK")]
check(ao == [0xfff] * 4 and pg == [0x3] * 4, f"always-on CU masks with 16 active CUs/SH: LB_ALWAYS_ACTIVE {[hex(x) for x in ao]} (12 CUs, :1679), PG_ALWAYS_ON {[hex(x) for x in pg]} (2 CUs, :1669)")
check(val_at(i_spm) == 0xf and val_at(i_rlc_on) == 1, "RLC_SPM_MC_CNTL.VMID=0xf (:5219-5220) and RLC_ENABLE_F32=1 (:3145)")
sl = [e for e in log if e[0] == "sleep"]
check(sl[:2] == [("sleep", 50e-6), ("sleep", 50e-6)], f"rlc_start udelay(50) x2 (:3146, :3151): {sl[:2]}")
# cp_resume
i_me = first("mmCP_ME_CNTL"); i_mec_off = first("mmCP_MEC_CNTL"); i_low = first("mmCP_MEC_DOORBELL_RANGE_LOWER"); i_up = first("mmCP_MEC_DOORBELL_RANGE_UPPER"); i_mec_on = first("mmCP_MEC_CNTL", i_mec_off + 1)
check(i_rlc_on < i_me < i_mec_off < i_low < i_up < i_mec_on, "cp_resume order: gui-idle off (:3951) -> CP_ME_CNTL halt (:3967) -> CP_MEC_CNTL disable (:3969) -> doorbell range (:3760-3772) -> CP_MEC_CNTL=0 (:3935/:3465)")
check(val_at(i_me) == dev.mmCP_ME_CNTL.encode(**{f: 1 for f in "ce_invalidate_icache pfp_invalidate_icache me_invalidate_icache ce_pipe0_reset ce_pipe1_reset pfp_pipe0_reset pfp_pipe1_reset me_pipe0_reset me_pipe1_reset me_halt pfp_halt ce_halt".split()}),
      f"CP_ME_CNTL = all 12 fields (:3250-3261) = {val_at(i_me):#x}")
check(val_at(i_mec_off) == dev.mmCP_MEC_CNTL.encode(mec_invalidate_icache=1, mec_me1_pipe0_reset=1, mec_me1_pipe1_reset=1, mec_me1_pipe2_reset=1, mec_me1_pipe3_reset=1, mec_me2_pipe0_reset=1, mec_me2_pipe1_reset=1, mec_me1_halt=1, mec_me2_halt=1) and val_at(i_mec_on) == 0,
      f"CP_MEC_CNTL full disable mask (:3467-3476) = {val_at(i_mec_off):#x}, then 0 (:3465)")
check(val_at(i_low) == 0 and val_at(i_up) == 0x450, f"MEC doorbell range {val_at(i_low):#x} / {val_at(i_up):#x} (kiq 0, userqueue_end 0x8A: :3760-3772)")
check(not any(e[0] == "doorbell_enable" for e in log) and not any(e[0] == "spatial_partition" for e in log), "no S2A doorbell_enable / spatial partition calls on Vega 20")
# setup_ring (gfx): CP_PQ_WPTR_POLL_CNTL.EN=0 before the MQD window, MQD deltas, CP_PQ_STATUS after HQD_ACTIVE
i_poll = first("mmCP_PQ_WPTR_POLL_CNTL"); i_mqd0 = first("mmCP_MQD_BASE_ADDR["); i_act = first("mmCP_HQD_ACTIVE", i_mqd0 + 56); i_pqs = first("mmCP_PQ_STATUS")
check(i_mec_on < i_poll < i_mqd0 < i_act < i_pqs and val_at(i_poll) == 0 and val_at(i_pqs) == dev.mmCP_PQ_STATUS.encode(doorbell_enable=1), "setup_ring: WPTR_POLL_CNTL.EN=0 (:3693) -> MQD window -> HQD_ACTIVE=1 -> CP_PQ_STATUS.DOORBELL_ENABLE=1 (:3795)")
hq0 = dev.mmCP_HQD_HQ_STATUS0.addr[0] - dev.mmCP_MQD_BASE_ADDR.addr[0]   # dword 0xa0 - 0x80 = 0x20 of the window (v9_mqd cp_hqd_hq_status0 at byte 640)
print(f"info window names at +0x20: {names[i_mqd0 + hq0]} (alias of CP_HQD_HQ_STATUS0 at the same address)")
check(val_at(first("mmCP_HQD_PERSISTENT_STATE")) == dev.mmCP_HQD_PERSISTENT_STATE.encode(preload_size=0x53) and hq0 == 0x20 and val_at(i_mqd0 + hq0) == 0, "MQD: PERSISTENT_STATE preload 0x53 no preload_req (:3661), HQ_STATUS0 (window +0x20) not set")
check(i_act - i_mqd0 == 56 and names[i_act - 1].startswith("mmCP_HQD_PQ_WPTR_HI"), "MQD window = 56 registers CP_MQD_BASE_ADDR..CP_HQD_PQ_WPTR_HI (v9_mqd dwords 0x80..0xb7)")
# SDMA
i_s0 = next(i for i, n in enumerate(names) if n.startswith("mmSDMA_")); i_s_last_golden = first("mmSDMA_UTCL1_TIMEOUT[1]")
print(f"info first SDMA writes: {names[i_s0:i_s0+3]} values {[hex(val_at(i)) for i in range(i_s0, i_s0+3)]}; i_pqs {i_pqs} i_s0 {i_s0}; expected name {dev.name_of(dev.mmSDMA_RLC0_RB_WPTR_POLL_CNTL.addr[0])}")
check(i_mec_on < i_s0 < i_poll and writes[i_s0][0] == dev.name_of(dev.mmSDMA_RLC0_RB_WPTR_POLL_CNTL.addr[0]) and val_at(i_s0) == 0x00403000, "SDMA init_hw runs after GFX init_hw and before the ring setups; its first write is golden_settings_sdma0_4_2_init RLC0_RB_WPTR_POLL_CNTL = 0x00403000 (sdma_v4_0.c:198)")
sg = names[i_s0:i_s_last_golden + 1]
check(len(sg) == 1 + 27 + 27 and sg[1] == "mmSDMA_CHICKEN_BITS[0]" and sg[28] == "mmSDMA_CHICKEN_BITS[1]" and sg[-1] == "mmSDMA_UTCL1_TIMEOUT[1]", f"{len(sg)} SDMA golden writes = 1 (:197-199) + 27 (:201-230, inst 0) + 27 (:232-260, inst 1)")
check(val_at(first("mmSDMA_UTCL1_TIMEOUT[0]")) == 0x00010001 and val_at(first("mmSDMA_CLK_CTRL[0]")) == 0x3f000100, "SDMA golden raw values UTCL1_TIMEOUT 0x00010001 (:229), CLK_CTRL 0x3f000100 (:204)")
i_h0 = first("mmSDMA_F32_CNTL[0]", i_s_last_golden); i_h1 = first("mmSDMA_F32_CNTL[1]", i_s_last_golden)
i_p0 = first("mmSDMA_PHASE0_QUANTUM[0]"); i_c0 = first("mmSDMA_CNTL[0]", i_p0); i_t0 = first("mmSDMA_UTCL1_TIMEOUT[0]", i_p0)
check(i_s_last_golden < i_h0 < i_h1 < i_p0 < first("mmSDMA_PHASE1_QUANTUM[0]") < first("mmSDMA_PHASE2_QUANTUM[0]") < i_c0 < i_t0, "enable: HALT=0 both instances (:1055-1059) -> ctx_switch: PHASE0/1/2 (:1013-1017) -> CNTL (:1018) -> UTCL1_TIMEOUT (:1030)")
check(val_at(i_h0) == 0 and val_at(i_p0) == 32 << 8 and val_at(i_c0) == dev.mmSDMA_CNTL.encode(auto_ctxsw_enable=1) and val_at(i_t0) == 0x00800080, f"values: HALT 0, PHASE quantum {val_at(i_p0):#x} (32 << VALUE shift, amdgpu_drv.c:183), CNTL AUTO_CTXSW {val_at(i_c0):#x}, UTCL1_TIMEOUT 0x00800080")
i_sem0 = first("mmSDMA_SEM_WAIT_FAIL_TIMER_CNTL[0]"); i_pg0 = first("mmSDMA_PAGE_RB_CNTL[0]", i_sem0); i_u0 = first("mmSDMA_CNTL[0]", i_pg0); i_hh0 = first("mmSDMA_F32_CNTL[0]", i_u0)
i_sem1 = first("mmSDMA_SEM_WAIT_FAIL_TIMER_CNTL[1]"); i_ulv0 = first("mmSDMA_ULV_CNTL[0]")
check(i_t0 < i_sem0 < i_pg0 < i_u0 < i_hh0 < i_sem1 < i_ulv0, "per instance: SEM_WAIT_FAIL_TIMER_CNTL=0 (:1407) -> page_resume (:1409-1410) -> CNTL.UTC_L1_ENABLE (:1413-1415) -> HALT=0 (:1419-1421); then ULV (:1789 -> :601-607)")
check(val_at(i_sem0) == 0 and val_at(i_u0) == dev.mmSDMA_CNTL.encode(auto_ctxsw_enable=1, utc_l1_enable=1, trap_enable=1) and val_at(i_ulv0) == 0, "values: SEM 0, CNTL keeps AUTO_CTXSW and adds UTC_L1|TRAP, ULV_CNTL.HYSTERESIS 0")
page_order = [n.split('[')[0][len("mmSDMA_PAGE_"):] for n in names[i_pg0:i_u0]]
exp_ring = ["RB_CNTL", "RB_RPTR", "RB_RPTR_HI", "RB_WPTR", "RB_WPTR_HI", "RB_RPTR_ADDR_HI", "RB_RPTR_ADDR_LO", "RB_BASE", "RB_BASE_HI", "MINOR_PTR_UPDATE", "DOORBELL", "DOORBELL_OFFSET",
            "MINOR_PTR_UPDATE", "RB_WPTR_POLL_ADDR_LO", "RB_WPTR_POLL_ADDR_HI", "RB_WPTR_POLL_CNTL", "RB_CNTL", "IB_CNTL"]
check(page_order == exp_ring, f"page_resume register order (:1181-1247): {page_order}")
pv = {n.split('[')[0][len('mmSDMA_PAGE_'):]: val_at(i) for i in range(i_pg0, i_u0) for n in [names[i]]}
check(dev.mmSDMA_PAGE_RB_CNTL.decode(pv["RB_CNTL"]) | {} == dev.mmSDMA_PAGE_RB_CNTL.decode(dev.mmSDMA_PAGE_RB_CNTL.encode(rb_size=18, rptr_writeback_enable=1, rb_enable=1)), f"page0 RB_CNTL final {pv['RB_CNTL']:#x}: rb_size 18 (1 MiB / 4 = 2^18), RPTR_WRITEBACK, RB_ENABLE; rb_vmid/rb_priv untouched (:1065-1070, :1112, :1152)")
check(pv["DOORBELL_OFFSET"] == dev.mmSDMA_PAGE_DOORBELL_OFFSET.encode(offset=(0x100 + 1) << 1) and pv["RB_BASE"] == (dev.sdma.page_ring_mc[0] >> 8) & 0xffffffff and pv["RB_BASE_HI"] == dev.sdma.page_ring_mc[0] >> 40 and pv["RB_RPTR_ADDR_LO"] == (dev.sdma.page_wb_mc[0] & 0xfffffffc) and pv["RB_WPTR_POLL_ADDR_LO"] == (dev.sdma.page_wb_mc[0] + 0x20) & 0xffffffff,
      f"page0 doorbell dword (0x100+1)<<1 = {(0x101)<<1:#x} (:1902-1903), RB_BASE >>8/>>40 (:1115-1116), rptr wb +0, wptr wb +0x20")
check(dev.mmSDMA_PAGE_RB_WPTR_POLL_CNTL.decode(pv["RB_WPTR_POLL_CNTL"])["f32_poll_enable"] == 0 and pv["RB_WPTR_POLL_CNTL"] == 0x00403000 and pv["IB_CNTL"] == 1, f"page0 WPTR_POLL_CNTL {pv['RB_WPTR_POLL_CNTL']:#x}: F32_POLL_ENABLE 0 (:1234) on top of the golden 0x00403000 (:210, RMW); IB_ENABLE 1 (:1242)")
dbw = [e for e in log if e[0] == "doorbell"]
check(dbw == [("doorbell", 0x101, 0), ("doorbell", 0x10b, 0), ("doorbell", 0x100, 0)], f"doorbell writes of 0 (set_wptr :1134/:1220): page0 0x101, page1 0x10b, gfx0 0x100 -> {dbw}")
i_g0 = first("mmSDMA_GFX_RB_CNTL[0]", i_ulv0)
check(i_ulv0 < i_poll < i_pqs < i_g0, "ring setups follow init_hw: GFX HQD (WPTR_POLL..PQ_STATUS) then the SDMA gfx ring")
gfx_order = [n.split('[')[0][len("mmSDMA_GFX_"):] for n in names[i_g0:-1]]
check(gfx_order == exp_ring and names[-1] == "mmSDMA_F32_CNTL[0]" and val_at(len(names) - 1) == 0, f"setup_ring: gfx_resume order (:1096-1161) then F32 HALT=0 (:1419-1421): {gfx_order[-3:]} + {names[-1]}")
gv = {n.split('[')[0][len('mmSDMA_GFX_'):]: val_at(i) for i in range(i_g0, len(names) - 1) for n in [names[i]]}
check(gv["RB_CNTL"] == dev.mmSDMA_GFX_RB_CNTL.encode(rb_size=14, rptr_writeback_enable=1, rb_enable=1) and gv["DOORBELL_OFFSET"] == dev.mmSDMA_GFX_DOORBELL_OFFSET.encode(offset=0x200) and gv["RB_BASE"] == (0x200003000000 >> 8) & 0xffffffff and gv["RB_BASE_HI"] == 0x200003000000 >> 40,
      f"gfx ring RB_CNTL {gv['RB_CNTL']:#x} (64 KiB -> rb_size 14), doorbell offset 0x200 (0x100 << 1, :1863), RB_BASE/HI")
check(dev.sdma.sdma_reginst == [("regSDMA_PAGE", 0), ("regSDMA_PAGE", 1), ("regSDMA_GFX", 0)], f"sdma_reginst for fini_hw: {dev.sdma.sdma_reginst}")
check(not any(n.startswith("mmSDMA") and "QUEUE" in n for n in names) and not any(n.startswith("mmRLC_RLCS") for n in names), "no gfx11-style SDMA{n}_QUEUE names, no RLC_RLCS_BOOTLOAD_STATUS on Vega 20")

# gates: page queue off with fw 122; ULV skipped for a non-0x66a1 device; a different SE/SH/CU shape; non-2.1 RLC header -> no SRM write
d2 = vega(fw_versions={"sdma": 122}, pci_device=0x66af, gc_info=Obj(gc_num_se=1, gc_num_sh_per_se=1, gc_num_cu_per_sh=4))
l2 = run(ip_new, d2, {"init_hw"}); n2 = [e[1] for e in l2 if e[0] == "w"]
check(not d2.sdma.has_page_queue and not any(n.startswith(("mmSDMA_PAGE_RB_CNTL", "mmSDMA_PAGE_RB_BASE", "mmSDMA_PAGE_DOORBELL", "mmSDMA_PAGE_IB_CNTL")) for n in n2) and not any("ULV" in n for n in n2),
      "fw_version 122 -> no page_resume writes (:1746-1747; the two PAGE_* golden entries :209-210 still apply); device 0x66af -> no ULV write (:594-595)")
check([e for e in l2 if e[0] == "palloc"] == [("palloc", 0x1000, True)] * 2 + [("palloc", (len(d2.gfx.csb_dws) * 4 + 0xfff) & ~0xfff, True)], "no page ring allocation without the page queue (only MQDs + csb)")
check([d2.vals[d2.mmRLC_LB_ALWAYS_ACTIVE_CU_MASK.addr[0]], d2.vals[d2.mmRLC_PG_ALWAYS_ON_CU_MASK.addr[0]]] == [0xf, 0x3], "1 SE x 1 SH x 4 CU: always-active 0xf, PG always-on 0x3 (loop follows gc_info, :1689-1702)")
d3 = vega(fw_versions={"rlc_minor": 0}); l3 = run(ip_new, d3, {"init_hw"})
check(not any(e[0] == "w" and e[1].startswith("mmRLC_SRM_CNTL") for e in l3), "RLC header 2.0 -> no RLC_SRM_CNTL write (:3104)")
d4 = FakeDev({am.GC_HWIP: (9,4,0), am.SDMA0_HWIP: (4,2,0), am.NBIO_HWIP: (7,4,0)}, [("gc", (9,4,0), am.GC_HWIP), ("sdma", (4,2,0), am.SDMA0_HWIP)], {am.GC_HWIP: GC_V20, am.SDMA0_HWIP: {0: SDMA_V20[0]}})
try: ip_new.AM_SDMA(d4).init_sw(); check(False, "SDMA init_sw must refuse != 2 instances")
except RuntimeError as e: check("expected 2 instances" in str(e), f"SDMA init_sw refuses 1 discovered instance: {e}")
d5 = vega(gc_info=Obj())
try: ip_new.AM_GFX(d5).init_sw(); d5.gfx = ip_new.AM_GFX(d5); d5.gfx.init_sw(); d5.gfx.init_hw(); check(False, "gfx must refuse a gc_info without SE/SH/CU")
except RuntimeError as e: check("gc_num_se" in str(e), f"gfx init_hw refuses missing gc_info fields: {e}")
# partial boot path: reset_mec -> _config_mec uses the full Linux mask on Vega 20
d6 = vega(); d6.partial_boot = True; l6 = run(ip_new, d6, {"init_hw"}); w6 = [(e[1], e[2]) for e in l6 if e[0] == "w"]
check(w6[0][0].startswith("mmGRBM_GFX_CNTL") and any(n.startswith("mmGRBM_SOFT_RESET") for n, _ in w6) and any(n.startswith("mmCP_MEC_CNTL") and v == dev.mmCP_MEC_CNTL.encode(mec_invalidate_icache=1, mec_me1_pipe0_reset=1, mec_me1_pipe1_reset=1, mec_me1_pipe2_reset=1, mec_me1_pipe3_reset=1, mec_me2_pipe0_reset=1, mec_me2_pipe1_reset=1, mec_me1_halt=1, mec_me2_halt=1) for n, v in w6)
      and not any(n.startswith("mmDB_DEBUG2") for n, _ in w6), "partial boot: reset_mec with the full CP_MEC_CNTL disable mask, no golden/RLC re-run (ip.py reset_mec)")

# ---------------------------------------------------------------- regression: gfx11 and MI300 vs the unmodified mi50 ip.py ----------------------------------------------------------------
fork = pathlib.Path(os.getenv("MI50_FORK_GIT", pathlib.Path(__file__).resolve().parents[2]))
# integration 2026-10-13: the "unmodified" baseline is the pre-integration mi50 commit d4697a019 (every implementer branched from it); after the
# merge `mi50` itself carries the Vega 20 branches, so comparing against it would prove nothing. Override with env MI50_BASELINE_REF.
src = subprocess.run(["git", "-C", str(fork), "show", os.environ.get("MI50_BASELINE_REF", "d4697a019") + ":tinygrad/runtime/support/am/ip.py"], capture_output=True, text=True, check=True).stdout
ip_old = types.ModuleType("ip_mi50_baseline"); ip_old.__file__ = "mi50:ip.py"; exec(compile(src, "mi50:ip.py", "exec"), ip_old.__dict__)
def seg(n, base): return tuple(base + 0x100000 * i for i in range(n))
def selfclear_safe_mode(d):
  d.selfclear = {a: d.regRLC_SAFE_MODE.fields_mask('cmd') for a in d.regRLC_SAFE_MODE.addr.values()}
  d.clears_on_write = {d.regCP_HQD_DEQUEUE_REQUEST.addr[i]: d.regCP_HQD_ACTIVE.addr[i] for i in d.regCP_HQD_ACTIVE.addr}; return d
def gfx11():
  ver = {am.GC_HWIP: (11,0,2), am.SDMA0_HWIP: (6,0,2), am.NBIO_HWIP: (7,7,0), am.MP0_HWIP: (13,0,7)}
  return selfclear_safe_mode(FakeDev(ver, [("gc", (11,0,2), am.GC_HWIP), ("nbio", (7,7,0), am.NBIO_HWIP)], {am.GC_HWIP: {0: seg(16, 0x2000)}, am.SDMA0_HWIP: {0: seg(16, 0x1000)}, am.NBIO_HWIP: {0: seg(16, 0x0)}}))
def mi300():
  ver = {am.GC_HWIP: (9,4,3), am.SDMA0_HWIP: (4,4,2), am.NBIO_HWIP: (7,9,0), am.MP0_HWIP: (13,0,6)}
  return selfclear_safe_mode(FakeDev(ver, [("gc", (9,4,3), am.GC_HWIP), ("sdma", (4,4,2), am.SDMA0_HWIP), ("nbio", (7,9,0), am.NBIO_HWIP)],
                 {am.GC_HWIP: {0: seg(16, 0x2000), 1: seg(16, 0x3000)}, am.SDMA0_HWIP: {i: seg(16, 0x4000 + 0x100 * i) for i in range(16)}, am.NBIO_HWIP: {0: seg(16, 0x0)}}))
for name, mk in (("gfx11 (GC 11.0.2, SDMA 6.0.2)", gfx11), ("MI300 (GC 9.4.3, SDMA 4.4.2, NBIO 7.9.0, 2 XCC, 16 SDMA)", mi300)):
  logs = {}
  for tag, mod in (("new", ip_new), ("mi50", ip_old)):
    d = mk(); logs[tag] = run(mod, d, {"init_hw", "setup_ring", "clockgating", "reset_mec", "fini"})
  same = logs["new"] == logs["mi50"]
  check(same and len(logs["new"]) > 100, f"{name}: write log identical to the unmodified mi50 ip.py ({len(logs['new'])} entries)")
  if not same:
    for i, (a, b) in enumerate(zip(logs["new"], logs["mi50"])):
      if a != b: print(f"   first difference at {i}: new {a} vs mi50 {b}"); break
check(all(n in ip_old.__dict__ for n in ("AM_GFX", "AM_SDMA")) and "mi50 fork" not in src.split("class AM_GFX")[1].split("class AM_IH")[0], "baseline module is the unmodified mi50 AM_GFX")

print(f"\n{'ALL CHECKS PASSED' if not fails else f'{len(fails)} FAILED'}: {len(fails)} failures")
sys.exit(1 if fails else 0)
