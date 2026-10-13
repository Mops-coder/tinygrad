#!/usr/bin/env python3
"""T6b offline (design v2 §7.1 T6b / §2 B12): drive AM_PSP.init_hw / mode1_reset against a scripted PSP mailbox. Needs no card.

Scripted hardware: C2PMSG_35 answers "bootloader ready" (bit 31, status bits scriptable), C2PMSG_81 is 0 until the SOS stage is written,
C2PMSG_64 answers "sOS ready"/"response" (bit 31), C2PMSG_67 is the ring wptr and every wptr write is served like the sOS would: the frame at
(wptr % ring_dw) is read, the command buffer behind it is answered with resp.status (scriptable) and the fence is written.
Checks: (1) Vega 20 (MP0 11.0.2) cold boot: exact command sequence == Linux psp_load_fw order (design v2 §4.4); (2) sOS-already-alive boot:
no bootloader stages, rest identical; (3) wptr wraps at ring_size/4 (amdgpu_psp.c:3963-3964); (4) a non-zero resp.status is logged and
raises; (5) mode1_reset writes GFX_CTRL_CMD_ID_MODE1_RST after the TOS-ready wait; (6) MP0 (13,0,6) / (13,0,7) / (14,0,2): the full MMIO
trace and every byte of every command/frame are identical between this tree's AM_PSP and the unmodified mi50 branch (git show mi50:...).
Run: PYTHONPATH=<worktree> python3 extra/mi50/t6b_psp_sequence.py   [env MI50_FW_DIR=<dir with vega20_*.bin>, default: the pinned scratchpad copy;
     absent -> synthetic blobs with the same descriptor order as AMFirmware._init_vega20]
"""
import os, sys, dataclasses, functools, importlib.util, pathlib, subprocess, tempfile, types
os.environ.setdefault("DEBUG", "2") # AM_PSP logs stages / statuses at DEBUG >= 2
from tinygrad.runtime.autogen.am import am
from tinygrad.runtime.support.amd import AMDReg, import_asic_regs
from tinygrad.runtime.support.am import ip as ip_new

HERE = pathlib.Path(__file__).resolve()
TREE = HERE.parents[2]
FW_DIR = pathlib.Path(os.getenv("MI50_FW_DIR", "/private/tmp/claude-501/-Users-leonh-Documents-Ai-shit/bba39989-2505-4a2d-a9f5-db421196e2ba/"
                                              "scratchpad/fw-vega20-pinned"))
MC_BASE, MP_BASE = 0x8000_0000_0000, 0x16000 # fake mc window; MP0_BASE seg0 == MP1_BASE seg0 == 0x16000 (vega20_ip_offset.h:85, 91)
fails: list[str] = []
def check(cond, msg):
  print(("ok   " if cond else "FAIL ") + msg)
  if not cond: fails.append(msg)

@dataclasses.dataclass
class FakeReg(AMDReg):   # same shape as am/amdev.py AMRegister
  adev: object
  def read(self, inst=0): return self.adev.rreg(self.addr[inst])
  def write(self, _am_val:int=0, inst=0, **kwargs): self.adev.wreg(self.addr[inst], _am_val | self.encode(**kwargs))

class FakeDev:
  """Everything AM_PSP touches on adev: ip_ver, devfmt, mm.palloc, paddr2mc/paddr2xgmi, vram.view, reg(), wreg_pair, gmc.flush_hdp, fw."""
  def __init__(self, mp0, gc, fw, sos_alive=False, wptr0=0, bl_status=0, status_for:dict|None=None, arena=0x1800000):
    self.ip_ver, self.devfmt, self.fw = {am.MP0_HWIP: mp0, am.GC_HWIP: gc}, "fake", fw
    self.arena, self.top = bytearray(arena), 0
    self.vram = types.SimpleNamespace(view=lambda paddr, size, fmt='B': memoryview(self.arena)[paddr:paddr+size].cast(fmt))
    self.mm = types.SimpleNamespace(palloc=self._palloc)
    self.gmc = types.SimpleNamespace(flush_hdp=lambda: self.trace.append(("hdp",)))
    # the real tables (regs.py) through the real lookup rule; both MP0 bases 0x16000 like vega20_ip_offset.h (irrelevant for 13.x/14.x)
    self.__dict__.update(import_asic_regs("mp", mp0, cls=functools.partial(FakeReg, adev=self, bases={0: (MP_BASE,)+(0,)*5})))
    self.idx = {r.addr[0]: int(n.rsplit("_", 1)[1]) for n, r in self.__dict__.items() if isinstance(r, FakeReg) and "C2PMSG_" in n}
    self.sol, self.wptr, self.bl_status, self.status_for = (0x80b67 if sos_alive else 0), wptr0, bl_status, status_for or {}
    self.c2p: dict[int, int] = {} # plain scratch for 36/69/70/71
    self.events, self.trace, self.snaps = [], [], []
  def _palloc(self, size, align=0x1000, zero=False, boot=True):
    addr = (self.top + align - 1) // align * align
    self.top = addr + size
    assert self.top <= len(self.arena), f"fake arena too small: {self.top:#x}"
    return addr
  def paddr2mc(self, p): return MC_BASE + p
  def paddr2xgmi(self, p): return p
  def reg(self, name): return getattr(self, name)
  def __getattr__(self, name):  # the fork's AMDev.__getattr__ reg* -> mm* fallback (am/amdev.py)
    if name.startswith("reg") and (alt:="mm" + name[3:]) in self.__dict__: return self.__dict__[alt]
    raise AttributeError(f"{type(self).__name__} has no attribute {name}")
  def wreg_pair(self, base, lo, hi, val, inst=0):
    self.reg(f"{base}{lo}").write(val & 0xffffffff, inst=inst)
    self.reg(f"{base}{hi}").write(val >> 32, inst=inst)
  def rreg(self, addr):
    i = self.idx[addr]
    val = {35: 0x80000000 | self.bl_status, 81: self.sol, 64: 0x80000000, 67: self.wptr}.get(i, self.c2p.get(i, 0))
    self.trace.append(("r", addr, val))
    return val
  def wreg(self, addr, val):
    i = self.idx[addr]
    self.trace.append(("w", addr, val))
    if i == 35:
      self.events.append(("BL", am.enum_psp_bootloader_cmd.get(val, hex(val))))
      if val == am.PSP_BL__LOAD_SOSDRV: self.sol = 0x80b67
    elif i == 64:
      if val == am.GFX_CTRL_CMD_ID_DESTROY_RINGS:
        self.events.append(("DESTROY_RINGS",))
        self.c2p[71] = 0
      elif val == am.GFX_CTRL_CMD_ID_MODE1_RST: self.events.append(("MODE1_RST",))
      elif val >> 16 == am.PSP_RING_TYPE__KM: self.events.append(("RING_CREATE", self.c2p[69] | (self.c2p[70] << 32), self.c2p[71]))
      else: self.events.append(("C2PMSG_64", hex(val)))
    elif i == 67: self._serve_frame(val)
    else: self.c2p[i] = val
  def _serve_frame(self, new_wptr):
    ring_dw = self.c2p[71] // 4
    frame_paddr = (self.c2p[69] | (self.c2p[70] << 32)) - MC_BASE + (self.wptr % ring_dw) * 4
    fr = am.struct_psp_gfx_rb_frame.from_buffer(self.arena, frame_paddr)
    cmd_paddr = (fr.cmd_buf_addr_lo | (fr.cmd_buf_addr_hi << 32)) - MC_BASE
    cmd = am.struct_psp_gfx_cmd_resp.from_buffer(self.arena, cmd_paddr)
    name = am.enum_psp_gfx_cmd_id.get(cmd.cmd_id, hex(cmd.cmd_id))
    if cmd.cmd_id == am.GFX_CMD_ID_LOAD_IP_FW:
      ev = ("CMD", name, am.enum_psp_gfx_fw_type.get(cmd.cmd.cmd_load_ip_fw.fw_type), cmd.cmd.cmd_load_ip_fw.fw_size)
    elif cmd.cmd_id == am.GFX_CMD_ID_SETUP_TMR: ev = ("CMD", name, cmd.cmd.cmd_setup_tmr.buf_size, cmd.cmd.cmd_setup_tmr.bitfield.virt_phy_addr)
    elif cmd.cmd_id == am.GFX_CMD_ID_LOAD_ASD: ev = ("CMD", name, cmd.cmd.cmd_load_ta.app_len, cmd.cmd.cmd_load_ta.cmd_buf_len)
    elif cmd.cmd_id == am.GFX_CMD_ID_LOAD_TOC: ev = ("CMD", name, cmd.cmd.cmd_load_toc.toc_size)
    else: ev = ("CMD", name)
    self.events.append(ev + (("wptr", self.wptr, new_wptr),))
    self.snaps.append((bytes(self.arena[frame_paddr:frame_paddr+64]), bytes(self.arena[cmd_paddr:cmd_paddr+1024])))
    cmd.resp.status = self.status_for.get(cmd.cmd_id, 0)
    if cmd.cmd_id == am.GFX_CMD_ID_LOAD_TOC: cmd.resp.tmr_size = 0x400000
    fence_paddr = (fr.fence_addr_lo | (fr.fence_addr_hi << 32)) - MC_BASE
    memoryview(self.arena)[fence_paddr:fence_paddr+4].cast('I')[0] = fr.fence_value
    self.wptr = new_wptr

def mv(b): return memoryview(bytearray(b))
def vega20_fw():
  """The fork's AMFirmware._init_vega20 over the pinned files (offline: load_fw reads the dir instead of fetch_fw), else synthetic blobs."""
  if FW_DIR.is_dir() and (FW_DIR / "vega20_sos.bin").exists():
    from tinygrad.runtime.support.am.amdev import AMFirmware
    from tinygrad.helpers import mv_address
    class OfflineFW(AMFirmware):
      def load_fw(self, fname, *headers, versioned_header=None):
        blob = mv((FW_DIR / fname).read_bytes())
        return tuple([blob] + [h.from_address(mv_address(blob)) for h in headers])
    fw = OfflineFW(types.SimpleNamespace(ip_ver={am.MP0_HWIP: (11,0,2)}, devfmt="fake"))
    print(f"firmware: real vega20_*.bin from {FW_DIR}")
    return fw
  print("firmware: synthetic (MI50_FW_DIR not found)")
  fw = types.SimpleNamespace(sos_fw={am.PSP_FW_TYPE_PSP_SYS_DRV: mv(b"\x11"*112528), am.PSP_FW_TYPE_PSP_SOS: mv(b"\x22"*62208)}, ucode_start={},
    descs=[([t], mv(bytes([t])*4096)) for t in [am.GFX_FW_TYPE_SDMA0, am.GFX_FW_TYPE_SDMA1, am.GFX_FW_TYPE_CP_MEC, am.GFX_FW_TYPE_CP_MEC_ME1,
      am.GFX_FW_TYPE_RLC_G, am.GFX_FW_TYPE_SMU]], asd_fw=mv(b"\x33"*205056))
  return fw

# Linux psp_load_non_psp_fw walks ucode[] in enum order (amdgpu_ucode.h:480-531) mapped by amdgpu_psp_get_fw_type (amdgpu_psp.c:3105-3209)
LINUX_IP_FW_ORDER = ["GFX_FW_TYPE_SDMA0", "GFX_FW_TYPE_SDMA1", "GFX_FW_TYPE_CP_CE", "GFX_FW_TYPE_CP_PFP", "GFX_FW_TYPE_CP_ME", "GFX_FW_TYPE_CP_MEC",
  "GFX_FW_TYPE_CP_MEC_ME1", "GFX_FW_TYPE_CP_MEC", "GFX_FW_TYPE_CP_MEC_ME2", "GFX_FW_TYPE_RLC_RESTORE_LIST_SRM_CNTL",
  "GFX_FW_TYPE_RLC_RESTORE_LIST_GPM_MEM", "GFX_FW_TYPE_RLC_RESTORE_LIST_SRM_MEM", "GFX_FW_TYPE_RLC_G", "GFX_FW_TYPE_SMU"]
def is_subseq(seq, order):
  it = iter(order)
  return all(any(x == y for y in it) for x in seq)
def summary(events): return [e[:-1] if e[0] == "CMD" else e for e in events]

def run_vega(ip_mod, **kw):
  dev = FakeDev((11,0,2), (9,4,0), vega20_fw(), **kw)
  psp = ip_mod.AM_PSP(dev)
  psp.init_sw()
  psp.init_hw()
  return dev, psp

# (1) cold boot (C2PMSG_81 == 0 as found): the design §4.4 / B12 sequence
print("--- (1) Vega 20 cold boot")
dev, psp = run_vega(ip_new)
ev = summary(dev.events)
for e in ev: print("   ", e)
check(ev[:2] == [("BL", "PSP_BL__LOAD_SYSDRV"), ("BL", "PSP_BL__LOAD_SOSDRV")], "bootloader stages = SYS_DRV 0x10000 then SOS 0x20000, nothing else")
check(ev[2][0] == "RING_CREATE" and ev[2][2] == 0x10000, "KM ring create right after the bootloader (amdgpu_psp.c:3052-3056)")
check(ev[3] == ("CMD", "GFX_CMD_ID_SETUP_TMR", 0x400000, 1), "SETUP_TMR buf_size = PSP_TMR_SIZE 0x400000, virt_phy_addr=1, before any LOAD_IP_FW")
ipfw = [e for e in ev if e[0] == "CMD" and e[1] == "GFX_CMD_ID_LOAD_IP_FW"]
types_ = [e[2] for e in ipfw]
check(ev[4:4+len(ipfw)] == ipfw, "LOAD_IP_FW block is contiguous after SETUP_TMR")
check(is_subseq(types_, LINUX_IP_FW_ORDER), f"LOAD_IP_FW fw types in Linux ucode enum order: {types_}")
check(types_[-1] == "GFX_FW_TYPE_SMU" and types_[-2] == "GFX_FW_TYPE_RLC_G", "SMC is last, after RLC_G (not pre-TMR)")
check(all(t in types_ for t in ["GFX_FW_TYPE_SDMA0", "GFX_FW_TYPE_SDMA1", "GFX_FW_TYPE_CP_MEC", "GFX_FW_TYPE_CP_MEC_ME1", "GFX_FW_TYPE_RLC_G",
                                "GFX_FW_TYPE_SMU"]),
      "SDMA0, SDMA1, MEC, MEC_ME1, RLC_G, SMU all loaded")
check(all(e[3] <= am.PSP_1_MEG for e in ipfw), "every LOAD_IP_FW fw_size <= PSP_1_MEG (psp_copy_fw, amdgpu_psp.c:4657-4675)")
asd_len = len(dev.fw.asd_fw)
check(ev[4+len(ipfw)] == ("CMD", "GFX_CMD_ID_LOAD_ASD", asd_len, 0), f"LOAD_ASD after the IP firmware: app_len={asd_len:#x}, cmd_buf_len=0")
check(len(ev) == 5 + len(ipfw), "nothing after LOAD_ASD: no LOAD_TOC, no REG_LIST, no AUTOLOAD_RLC, no DESTROY_RINGS")
check(not any(e[1] in ("GFX_CMD_ID_LOAD_TOC", "GFX_CMD_ID_AUTOLOAD_RLC") for e in ev if e[0] == "CMD") and "GFX_FW_TYPE_REG_LIST" not in types_,
      "no LOAD_TOC / AUTOLOAD_RLC / REG_LIST for a v1.0 sos header")
msg1 = dev.c2p[36]
check(msg1 == psp.msg1_addr >> 20 and psp.msg1_addr % am.PSP_1_MEG == 0, f"C2PMSG_36 = msg1 >> 20 ({msg1:#x}), msg1 1 MiB aligned")
check(psp.tmr_size == 0x400000 and psp.tmr_paddr % am.PSP_TMR_ALIGNMENT == 0, "tmr_size 0x400000 without TOC; TMR 1 MiB aligned (PSP_TMR_ALIGNMENT)")
wptrs = [e[-1] for e in dev.events if e[0] == "CMD"]
check(all(b - a == 16 for _, a, b in wptrs), "wptr advances 16 dwords (sizeof psp_gfx_rb_frame / 4) per command")

# (2) sOS already alive as found (T2 shows C2PMSG_81 != 0): no bootloader stages, rest identical
print("--- (2) Vega 20 with sOS alive")
dev2, _ = run_vega(ip_new, sos_alive=True)
ev2 = summary(dev2.events)
check(not any(e[0] == "BL" for e in ev2), "no bootloader stage written when C2PMSG_81 != 0 (psp_v11_0.c:214, 262)")
check(ev2 == ev[2:], "ring create / TMR / IP fw / ASD identical to the cold boot")

# (3) wptr wrap: start the ring wptr one frame before the end (ring_size/4 - 16)
print("--- (3) wptr modulo")
dev3, psp3 = run_vega(ip_new, wptr0=0x10000 // 4 - 16)
w = [e[-1] for e in dev3.events if e[0] == "CMD"]
check(w[0] == ("wptr", 0x3ff0, 0) and w[1] == ("wptr", 0, 16), f"first frame at dword 0x3ff0 then wptr wraps to 0 (amdgpu_psp.c:3963-3964): {w[:2]}")
check(all(b < 0x4000 for _, _, b in w), "wptr never reaches ring_size/4")

# (4) non-zero status: logged (DEBUG>=2 line above) and raised (U-P3)
print("--- (4) non-zero resp.status")
try:
  run_vega(ip_new, status_for={am.GFX_CMD_ID_LOAD_ASD: 0x1234})
  check(False, "non-zero status must raise")
except RuntimeError as e: check("0x1234" in str(e) or "4660" in str(e), f"RuntimeError carries the status: {e}")

# (5) mode1 reset
print("--- (5) mode1_reset")
dev5, psp5 = run_vega(ip_new)
n = len(dev5.events)
psp5.mode1_reset()
reads64 = [t for t in dev5.trace if t[0] == "r" and dev5.idx[t[1]] == 64]
check(dev5.events[n:] == [("MODE1_RST",)] and dev5.trace[-1] == ("w", psp5.adev.mmMP0_SMN_C2PMSG_64.addr[0], 0x70000) and reads64,
      "mode1_reset: TOS-ready read on C2PMSG_64, then C2PMSG_64 <- GFX_CTRL_CMD_ID_MODE1_RST 0x70000 (psp_v11_0.c:410-432)")
check(psp5.MBOX_TOS_READY_MASK == 0x8000FFFF and psp5.MBOX_TOS_READY_FLAG == 0x80000000,
      "MBOX_TOS_READY_MASK/FLAG = 0x8000FFFF / 0x80000000 (amdgpu_psp.h:57-58)")
try:
  p = ip_new.AM_PSP(FakeDev((13,0,6), (9,4,3), types.SimpleNamespace(sos_fw={}, descs=[])))
  p.init_sw()
  p.mode1_reset()
  check(False, "mode1_reset must refuse non-Vega 20")
except AssertionError as e: check(True, f"mode1_reset refuses MP0 (13,0,6): {str(e)[:60]}")

# (6) other cards: this tree vs the unmodified mi50 branch, full MMIO trace + command bytes
print("--- (6) MP0 13.0.6 / 13.0.7 / 14.0.2 unchanged vs mi50")
# integration 2026-10-13: the "unmodified" baseline is the pre-integration mi50 commit d4697a019 (every implementer branched from it); after the
# merge `mi50` itself carries the Vega 20 branches, so comparing against it would prove nothing. Override with env MI50_BASELINE_REF.
old_src = subprocess.run(["git", "-C", str(TREE), "show", os.environ.get("MI50_BASELINE_REF", "d4697a019") + ":tinygrad/runtime/support/am/ip.py"], capture_output=True, text=True,
                         check=True).stdout
with tempfile.TemporaryDirectory() as td:
  pathlib.Path(td, "ip_mi50.py").write_text(old_src)
  spec = importlib.util.spec_from_file_location("ip_mi50", pathlib.Path(td, "ip_mi50.py"))
  ip_old = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(ip_old)
def other_fw(gc):
  fw = types.SimpleNamespace(sos_fw={k: mv(bytes([k])*8192) for k in [am.PSP_FW_TYPE_PSP_KDB, am.PSP_FW_TYPE_PSP_SPL, am.PSP_FW_TYPE_PSP_SYS_DRV,
    am.PSP_FW_TYPE_PSP_SOS, am.PSP_FW_TYPE_PSP_TOC, am.PSP_FW_TYPE_PSP_RL]}, ucode_start={},
    descs=[([am.GFX_FW_TYPE_SDMA0, am.GFX_FW_TYPE_SDMA1], mv(b"\x09"*4096)), ([am.GFX_FW_TYPE_CP_MEC], mv(b"\x04"*4096)),
           ([am.GFX_FW_TYPE_RLC_G], mv(b"\x08"*4096))])
  if gc >= (11,0,0): fw.smu_psp_desc = ([am.GFX_FW_TYPE_SMU], mv(b"\x12"*4096))
  return fw
for mp0, gc in [((13,0,6), (9,4,3)), ((13,0,7), (11,0,0)), ((14,0,2), (12,0,0))]:
  for sos_alive in (False, True):
    runs = []
    for mod in (ip_old, ip_new):
      d = FakeDev(mp0, gc, other_fw(gc), sos_alive=sos_alive)
      p = mod.AM_PSP(d)
      p.init_sw()
      p.init_hw()
      runs.append((d.trace, d.snaps, summary(d.events), (p.tmr_size, p.tmr_paddr, p.boot_time_tmr, p.autoload_tmr)))
    same = runs[0] == runs[1]
    check(same, f"MP0 {mp0} GC {gc} sos_alive={sos_alive}: {len(runs[1][0])} MMIO ops, {len(runs[1][1])} commands - "
                "trace/bytes/state identical to mi50")
    if not same:
      for a, b in zip(runs[0][2], runs[1][2]):
        if a != b: print("    differs:", a, "!=", b)
    if not sos_alive: print("    sequence:", [e[1:] if e[0] == "CMD" else e for e in runs[1][2]])

print(f"\n{len(fails)} FAIL" if fails else "\nall checks passed (offline; no card)")
sys.exit(1 if fails else 0)
