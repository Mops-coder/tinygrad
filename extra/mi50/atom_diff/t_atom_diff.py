#!/usr/bin/env python3
"""Differential test: Linux atom.c (compiled unchanged into ./harness) vs the fork's tinygrad/runtime/support/am/atom.py on random well-formed
ATOM command tables. Every opcode 1..126, every argument kind, IIO programs, switch, calltable, forward jumps. Undefined C behaviour is never
generated (shift counts >= 32, fb windows past the scratch buffer, the WS SHIFT/FB_WINDOW destinations that feed them).
Run: python3 extra/mi50/atom_diff/t_atom_diff.py <fork root> <n programs> [seed]   [env MI50_LINUX_AMD=<linux>/drivers/gpu/drm/amd]"""
import sys, random, struct, subprocess, os, tempfile, time
sys.path.insert(0, sys.argv[1])
from tinygrad.runtime.support.am import atom as A

IMG, CMD, DATA, FWI, IIO, DATAREG, CODE0 = 0x20010, 0x200, 0x300, 0x400, 0x500, 0x600, 0x1000
SCRATCH, NT = 1024, 6
A6 = [0, 1, 2, 3, 6, 7]   # REG PS WS FB PLL MC (destination kinds in opcode order)

def opinfo():
  t = {}
  for op, (f, a) in enumerate(A.OPCODE_TABLE):
    if f is not None: t[op] = (f.__name__, a)
  return t
OPS = opinfo()

class Gen:
  def __init__(self, rng): self.r = rng
  def imm(self, align, small=None):
    v = self.r.randrange(small) if small else self.r.getrandbits(32)
    return struct.pack("<I", v) if align == 0 else struct.pack("<H", v & 0xFFFF) if align in (1, 2, 3) else bytes([v & 0xFF])
  def operand(self, kind, align, ws, dst=False):
    r = self.r
    if kind == 0: return struct.pack("<H", r.choice([r.randrange(0x40), r.randrange(0x20000) & 0xFFFF, 0]))   # REG
    if kind == 1: return bytes([r.randrange(20)])                                                            # PS (some out of range)
    if kind == 2:                                                                                            # WS
      pool = list(range(max(ws, 1) + 2)) + [0x40, 0x41, 0x42, 0x47, 0x48] + ([] if dst else [0x43, 0x44, 0x45, 0x46])
      return bytes([r.choice(pool)])
    if kind == 3: return bytes([r.randrange(200)])                                                           # FB (window kept inside scratch)
    if kind == 4: return struct.pack("<H", r.randrange(0x10000))                                             # ID
    if kind == 5: return self.imm(align)                                                                     # IMM
    return bytes([r.randrange(256)])                                                                         # PLL / MC
  def attr(self, src_kinds=range(8)):
    r = self.r
    return (r.choice(list(src_kinds))) | (r.randrange(8) << 3) | (r.randrange(4) << 6)
  @staticmethod
  def dst_align(attr): return A.atom_dst_to_src[(attr >> 3) & 7][(attr >> 6) & 3]

  def instr(self, op, ws, tbl_idx):
    """Returns (bytes, list of (byte offset of a u16 jump target inside the instruction))."""
    r, (name, arg) = self.r, OPS[op]
    o = bytes([op])
    if name in ("op_move", "op_and", "op_or", "op_mul", "op_div", "op_add", "op_sub", "op_compare", "op_test", "op_xor", "op_mul32", "op_div32"):
      at = self.attr(); s = at & 7
      return o + bytes([at]) + self.operand(arg, 0, ws, dst=True) + self.operand(s, (at >> 3) & 7, ws), []
    if name == "op_mask":
      at = self.attr(); s = at & 7
      return o + bytes([at]) + self.operand(arg, 0, ws, dst=True) + self.imm((at >> 3) & 7) + self.operand(s, (at >> 3) & 7, ws), []
    if name in ("op_shift_left", "op_shift_right"):
      at = self.attr()
      return o + bytes([at]) + self.operand(arg, 0, ws, dst=True) + bytes([r.randrange(32)]), []
    if name in ("op_shl", "op_shr"):
      at = (5) | (r.randrange(8) << 3) | (r.randrange(4) << 6)   # src forced IMM < 32: a C shift by >= 32 is undefined
      return o + bytes([at]) + self.operand(arg, 0, ws, dst=True) + self.imm((at >> 3) & 7, small=32), []
    if name == "op_clear":
      return o + bytes([self.attr()]) + self.operand(arg, 0, ws, dst=True), []
    if name == "op_setport":
      return (o + struct.pack("<H", r.choice([0, 0, 1, 2, 3]))) if arg == 0 else (o + bytes([r.randrange(256)])), []
    if name == "op_setregblock": return o + struct.pack("<H", r.choice([0, r.randrange(0x100), r.randrange(0x10000)])), []
    if name == "op_setfbbase":
      at = 5 | (r.choice([0, 1, 4]) << 3)   # IMM, small: keeps every FB access inside the scratch buffer
      return o + bytes([at]) + self.imm((at >> 3) & 7, small=64), []
    if name == "op_switch":
      at = self.attr(); s = at & 7
      b = o + bytes([at]) + self.operand(s, (at >> 3) & 7, ws)
      tg = []
      for _ in range(r.randrange(4)):
        b += bytes([A.ATOM_CASE_MAGIC]) + self.imm((at >> 3) & 7, small=r.choice([4, 1 << 16])); tg.append(len(b)); b += b"\0\0"
      return b + struct.pack("<H", A.ATOM_CASE_END), tg
    if name == "op_jump": return o + b"\0\0", [1]
    if name == "op_delay": return o + bytes([r.randrange(3)]), []
    if name == "op_calltable": return o + bytes([r.choice([tbl_idx + 1 + r.randrange(3), 40])]), []   # higher index (finite) or absent
    if name == "op_setdatablock": return o + bytes([r.choice([0, 255, 4, 7, 9, 12, 30])]), []
    if name in ("op_postcard", "op_debug"): return o + bytes([r.randrange(256)]), []
    if name == "op_processds":
      n = r.randrange(6); return o + struct.pack("<H", n) + bytes(r.randrange(256) for _ in range(n)), []
    if name in ("op_nop", "op_beep", "op_unimplemented"): return o, []
    if name == "op_eot": return None, []
    raise AssertionError(name)

  def table(self, tbl_idx, n_instr):
    ws, ps = self.r.randrange(6), self.r.randrange(0, 40, 4)
    ins, offs, off = [], [], 6
    for _ in range(n_instr):
      op = self.r.choice([k for k in OPS if k != A.ATOM_OP_EOT])
      b, tg = self.instr(op, ws, tbl_idx)
      ins.append((b, tg)); offs.append(off); off += len(b)
    eot_off = off; off += 1
    code = b""
    for i, (b, tg) in enumerate(ins):
      b = bytearray(b)
      for p in tg:   # forward targets only: a later instruction start or the EOT -> every program terminates
        target = self.r.choice(offs[i + 1:] + [eot_off])
        b[p:p + 2] = struct.pack("<H", target)
      code += bytes(b)
    code += bytes([A.ATOM_OP_EOT])
    return struct.pack("<HBBBB", off, 1, 1, ws, ps) + code

  def image(self):
    r = self.r
    img = bytearray(r.getrandbits(8) for _ in range(IMG))
    img[0:2] = b"\x55\xaa"; img[0x30:0x3a] = b" 761295520"; img[0x48:0x4a] = struct.pack("<H", 0x100)
    img[0x104:0x108] = b"ATOM"; img[0x11e:0x120] = struct.pack("<H", CMD); img[0x120:0x122] = struct.pack("<H", DATA)
    img[CMD:CMD + 4 + 2 * 64] = bytes(4 + 2 * 64); img[DATA:DATA + 4 + 2 * 64] = bytes(4 + 2 * 64)
    for i in (4, 7, 9, 12, 30): img[DATA + 4 + 2 * i:DATA + 6 + 2 * i] = struct.pack("<H", DATAREG + 0x40 * i)
    img[DATA + 0xC:DATA + 0xE] = struct.pack("<H", FWI)
    img[FWI + 8:FWI + 16] = struct.pack("<II", 1 + r.randrange(100000), 1 + r.randrange(100000))
    img[DATA + 0x32:DATA + 0x34] = struct.pack("<H", IIO)
    p = IIO + 4
    for idx in (1, 2, 0x81, 0x82):
      prog = bytes([A.ATOM_IIO_START, idx])
      for _ in range(r.randrange(1, 6)):
        k = r.choice([0, 2, 3, 4, 5, 6, 7, 8])
        if k == 0: prog += bytes([0])
        elif k in (2, 3): prog += bytes([k]) + struct.pack("<H", r.randrange(0x10000))
        elif k in (4, 5): prog += bytes([k, r.randrange(1, 33), r.randrange(32)])
        else: prog += bytes([k, r.randrange(1, 33), r.randrange(32), r.randrange(32)])
      prog += bytes([A.ATOM_IIO_END, 0, 0])
      img[p:p + len(prog)] = prog; p += len(prog)
    img[p] = 0   # end of the IIO list (not ATOM_IIO_START)
    cp = CODE0
    for t in range(NT):
      tb = self.table(t, r.randrange(1, 40))
      img[cp:cp + len(tb)] = tb
      img[CMD + 4 + 2 * t:CMD + 6 + 2 * t] = struct.pack("<H", cp)
      cp += len(tb) + 16
    return bytes(img)

def run_py(img:bytes) -> list[str]:
  out, n = [], [0]
  def rd(reg):
    v = ((reg * 2654435761) ^ (n[0] * 40503) ^ 0x5bd1e995) & 0xFFFFFFFF; n[0] += 1
    out.append(f"R {reg:x} {v:x}"); return v
  def wr(reg, val): out.append(f"W {reg:x} {val:x}")
  ctx = A.AtomContext(A.CardInfo(rd, wr), img, SCRATCH)
  ps = [(0x11111111 * (i + 1)) & 0xFFFFFFFF for i in range(16)]
  r = ctx.execute_table(0, ps, 16)
  out.append(f"RET {r}")
  out += [f"PS {i} {v:x}" for i, v in enumerate(ps)]
  out += [f"FB {i} {v:x}" for i, v in enumerate(ctx.scratch) if v]
  out.append(f"DIVMUL {ctx.divmul[0]:x} {ctx.divmul[1]:x}")
  out.append(f"DB {ctx.data_block:x} RB {ctx.reg_block:x} FBB {ctx.fb_base:x} SHIFT {ctx.shift:x} ATTR {ctx.io_attr:x} IO {ctx.io_mode:x} "
             f"EQ {int(ctx.cs_equal)} AB {int(ctx.cs_above)}")
  return out

def build_harness() -> str:
  """Compile Linux atom.c UNCHANGED (copied, cmp-verified) with userspace stubs for the kernel headers; amdgpu.h is a stub next to the copy because
  atom.c's quoted #include would otherwise pick up the real one from its own directory."""
  import shutil, filecmp
  linux = os.environ.get("MI50_LINUX_AMD", "/private/tmp/claude-501/-Users-leonh-Documents-Ai-shit/bba39989-2505-4a2d-a9f5-db421196e2ba/"
                                            "scratchpad/repos/linux/drivers/gpu/drm/amd")
  here = os.path.dirname(os.path.abspath(__file__))
  out = tempfile.mkdtemp(prefix="atom_diff_")
  shutil.copy(os.path.join(linux, "amdgpu", "atom.c"), os.path.join(out, "atom.c"))
  assert filecmp.cmp(os.path.join(linux, "amdgpu", "atom.c"), os.path.join(out, "atom.c"), shallow=False), "atom.c copy differs"
  shutil.copy(os.path.join(here, "harness.c"), os.path.join(out, "harness.c"))
  open(os.path.join(out, "amdgpu.h"), "w").write('#pragma once\n#include "kstub.h"\n')
  subprocess.run(["clang", "-O1", "-w", f"-I{here}/stub", f"-I{linux}/include", f"-I{linux}/amdgpu", "-o", os.path.join(out, "harness"),
                  os.path.join(out, "harness.c")], check=True)
  return os.path.join(out, "harness")

def main():
  n, seed = int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 1
  A.time.sleep = lambda s: None   # the C stubs make udelay/msleep no-ops too
  import contextlib, io
  harness = build_harness()
  reads, writes, fails = 0, 0, 0
  with tempfile.TemporaryDirectory() as td:
    for i in range(n):
      g = Gen(random.Random(seed * 1000003 + i))
      img = g.image()
      f = os.path.join(td, "bios.bin"); open(f, "wb").write(img)
      c = subprocess.run([harness, f, str(SCRATCH)], capture_output=True, text=True, timeout=60).stdout.splitlines()
      with contextlib.redirect_stdout(io.StringIO()): p = run_py(img)
      reads += sum(1 for l in c if l[0] == "R"); writes += sum(1 for l in c if l[0] == "W")
      if c != p:
        fails += 1
        k = next(j for j in range(min(len(c), len(p)) + 1) if j >= len(c) or j >= len(p) or c[j] != p[j])
        print(f"MISMATCH program {i} (seed {seed}) at line {k}: C {c[k] if k < len(c) else '<end>'} | PY {p[k] if k < len(p) else '<end>'}")
        if fails >= 5: break
  print(f"{n} programs, {reads} register reads + {writes} writes compared, {fails} mismatches")
  sys.exit(1 if fails else 0)

if __name__ == "__main__": main()
