# mi50 fork (Leon 2026-10-13, "of course port it"): the ATOM BIOS bytecode interpreter, a line-by-line port of Linux
# drivers/gpu/drm/amd/amdgpu/atom.c (+ atom.h, include/atom-bits.h; tree d6d3455). An unPOSTed Vega 20 (nothing runs the VBIOS on a Mac over
# Thunderbolt) is initialised exactly as amdgpu does it: amdgpu_device_need_post -> amdgpu_device_asic_init -> amdgpu_atom_asic_init
# (amdgpu_device.c:4068-4080, :958-990; GC 9.4.0 takes the amdgpu_atom_asic_init branch at :982).
# Every value is unsigned 32 bit as in C (uint32_t); citations are atom.c line numbers unless stated.
# Deliberate deviations (fail closed, never different behaviour on a well-formed BIOS): a read past the image raises IndexError (C reads out of
# bounds); FB scratch indices are bounded by the Python list as well as by Linux's check (:302, :528 allow fb_base + idx*4 == size, one dword past
# the kzalloc'ed buffer); variable shifts use the count mod 32 like the arm64/x86 shift instructions the C compiles to (C leaves >= 32 undefined);
# a top-level table (execute_table, nested calls included) running longer than ATOM_CMD_TIMEOUT_SEC in total is aborted like Linux's
# stuck loop (:769-783), whose same-target test misses loops alternating two taken jumps and never times op_switch. 20 s is Linux's own
# bound for a stuck interpreter; the MI50's cold POST (BIOS_SCRATCH_7 bit 9 clear) measured 0.00-0.01 s on the card, 2026-10-14.
# Not ported (informational only, never read by the interpreter): atom_get_vbios_name/pn/date/version/build (:1361-1561), the DEBUG tracing.
import time
from typing import Callable

M32 = 0xFFFFFFFF
# atom.h
ATOM_BIOS_MAGIC, ATOM_ATI_MAGIC_PTR, ATOM_ATI_MAGIC, ATOM_ROM_TABLE_PTR = 0xAA55, 0x30, b" 761295520", 0x48
ATOM_ROM_MAGIC, ATOM_ROM_MAGIC_PTR, ATOM_ROM_CMD_PTR, ATOM_ROM_DATA_PTR = b"ATOM", 4, 0x1E, 0x20
ATOM_CMD_INIT = 0
ATOM_DATA_FWI_PTR, ATOM_DATA_IIO_PTR = 0xC, 0x32
ATOM_FWI_DEFSCLK_PTR, ATOM_FWI_DEFMCLK_PTR = 8, 0xC
ATOM_CT_SIZE_PTR, ATOM_CT_WS_PTR, ATOM_CT_PS_PTR, ATOM_CT_PS_MASK, ATOM_CT_CODE_PTR = 0, 4, 5, 0x7F, 6
ATOM_OP_CNT, ATOM_OP_EOT = 127, 91
ATOM_CASE_MAGIC, ATOM_CASE_END = 0x63, 0x5A5A
ATOM_ARG_REG, ATOM_ARG_PS, ATOM_ARG_WS, ATOM_ARG_FB, ATOM_ARG_ID, ATOM_ARG_IMM, ATOM_ARG_PLL, ATOM_ARG_MC = range(8)
(ATOM_SRC_DWORD, ATOM_SRC_WORD0, ATOM_SRC_WORD8, ATOM_SRC_WORD16, ATOM_SRC_BYTE0, ATOM_SRC_BYTE8, ATOM_SRC_BYTE16, ATOM_SRC_BYTE24) = range(8)
(ATOM_WS_QUOTIENT, ATOM_WS_REMAINDER, ATOM_WS_DATAPTR, ATOM_WS_SHIFT, ATOM_WS_OR_MASK, ATOM_WS_AND_MASK, ATOM_WS_FB_WINDOW, ATOM_WS_ATTRIBUTES,
 ATOM_WS_REGPTR) = range(0x40, 0x49)
(ATOM_IIO_NOP, ATOM_IIO_START, ATOM_IIO_READ, ATOM_IIO_WRITE, ATOM_IIO_CLEAR, ATOM_IIO_SET, ATOM_IIO_MOVE_INDEX, ATOM_IIO_MOVE_ATTR,
 ATOM_IIO_MOVE_DATA, ATOM_IIO_END) = range(10)
ATOM_IO_MM, ATOM_IO_PCI, ATOM_IO_SYSIO, ATOM_IO_IIO = 0, 1, 2, 0x80
# atom.c:43-61
ATOM_COND_ABOVE, ATOM_COND_ABOVEOREQUAL, ATOM_COND_ALWAYS, ATOM_COND_BELOW, ATOM_COND_BELOWOREQUAL, ATOM_COND_EQUAL, ATOM_COND_NOTEQUAL = range(7)
ATOM_PORT_ATI, ATOM_PORT_PCI, ATOM_PORT_SYSIO = 0, 1, 2
ATOM_UNIT_MICROSEC, ATOM_UNIT_MILLISEC = 0, 1
ATOM_CMD_TIMEOUT_SEC = 20
ATOM_EXECUTE_MAX_DEPTH = 32
# atom.c:82-99
atom_arg_mask = [0xFFFFFFFF, 0xFFFF, 0xFFFF00, 0xFFFF0000, 0xFF, 0xFF00, 0xFF0000, 0xFF000000]
atom_arg_shift = [0, 0, 8, 16, 0, 8, 16, 24]
atom_dst_to_src = [[0, 0, 0, 0], [1, 2, 3, 0], [1, 2, 3, 0], [1, 2, 3, 0], [4, 5, 6, 7], [4, 5, 6, 7], [4, 5, 6, 7], [4, 5, 6, 7]]
atom_def_dst = [0, 0, 1, 2, 0, 1, 2, 3]
atom_iio_len = [1, 2, 3, 3, 3, 3, 4, 4, 4, 3]   # :1328

def _shl(v:int, s:int) -> int: return (v << (s & 31)) & M32
def _shr(v:int, s:int) -> int: return (v & M32) >> (s & 31)

class AtomError(RuntimeError): pass

class CardInfo:
  """struct card_info (atom.h:111-121). amdgpu fills it in amdgpu_atombios_init (amdgpu_atombios.c:1909-1924): reg_read/reg_write = RREG32/WREG32
  (cail_reg_read/write :1794-1816), mc_* and pll_* are no-ops that read 0 (:1738-1784)."""
  def __init__(self, reg_read:Callable[[int], int], reg_write:Callable[[int, int], None]):
    self.reg_read, self.reg_write = reg_read, reg_write
  def mc_read(self, reg:int) -> int: return 0
  def mc_write(self, reg:int, val:int): pass
  def pll_read(self, reg:int) -> int: return 0
  def pll_write(self, reg:int, val:int): pass

class AtomContext:
  """struct atom_context (atom.h:123-150) + amdgpu_atom_parse (:1585-1651)."""
  def __init__(self, card:CardInfo, bios:bytes, scratch_size_bytes:int):
    self.card, self.bios, self.bios_size = card, bytes(bios), len(bios)
    if self.cu16(0) != ATOM_BIOS_MAGIC: raise AtomError("Invalid BIOS magic")                                          # :1600-1604
    if self.bios[ATOM_ATI_MAGIC_PTR:ATOM_ATI_MAGIC_PTR + len(ATOM_ATI_MAGIC)] != ATOM_ATI_MAGIC: raise AtomError("Invalid ATI magic")  # :1605-1611
    base = self.cu16(ATOM_ROM_TABLE_PTR)                                                                                  # :1613
    if self.bios[base + ATOM_ROM_MAGIC_PTR:base + ATOM_ROM_MAGIC_PTR + 4] != ATOM_ROM_MAGIC: raise AtomError("Invalid ATOM magic")  # :1614-1620
    self.cmd_table, self.data_table = self.cu16(base + ATOM_ROM_CMD_PTR), self.cu16(base + ATOM_ROM_DATA_PTR)          # :1622-1623
    self.iio = [0] * 256
    self._index_iio(self.cu16(self.data_table + ATOM_DATA_IIO_PTR) + 4)                                                   # :1624
    # amdgpu_atomfirmware_allocate_fb_scratch (amdgpu_atomfirmware.c:204-207): host memory, kzalloc'ed, 20 KiB when the table asks for 0
    self.scratch_size_bytes = scratch_size_bytes or 20 * 1024
    self.scratch = [0] * (self.scratch_size_bytes // 4)
    self.data_block = self.reg_block = self.fb_base = self.io_attr = self.shift = 0
    self.divmul = [0, 0]
    self.cs_equal = self.cs_above = False
    self.io_mode = ATOM_IO_MM
    self.execute_depth = 0
    self.deadline = 0.0   # set by execute_table; _execute_locked entered any other way aborts at once

  # atom-bits.h
  def cu8(self, p:int) -> int: return self.bios[p]
  def cu16(self, p:int) -> int: return self.bios[p] | (self.bios[p + 1] << 8)
  def cu32(self, p:int) -> int: return self.cu16(p) | (self.cu16(p + 2) << 16)

  def _index_iio(self, base:int):
    """atom_index_iio (:1330-1359)."""
    while base + 1 < self.bios_size and self.cu8(base) == ATOM_IIO_START:
      index, start = self.cu8(base + 1), base + 2
      base += 2
      while base < self.bios_size and self.cu8(base) != ATOM_IIO_END:
        if (op:=self.cu8(base)) >= len(atom_iio_len): return   # :1346-1347 unknown opcode: the stream cannot be resynced
        base += atom_iio_len[op]
      if base >= self.bios_size: return
      self.iio[index] = start   # :1352 only well-formed methods are indexed
      base += 3

  def iio_execute(self, base:int, index:int, data:int) -> int:
    """atom_iio_execute (:113-188)."""
    temp, start = 0xCDCDCDCD, base
    def m(n): return _shr(M32, 32 - n)   # (0xFFFFFFFF >> (32 - n)), count mod 32 like every variable shift here (n == 0 -> 0xFFFFFFFF)
    while base + 3 < self.bios_size:
      op = self.cu8(base)
      if op == ATOM_IIO_NOP: base += 1
      elif op == ATOM_IIO_READ: temp = self.card.reg_read(self.cu16(base + 1)) & M32; base += 3
      elif op == ATOM_IIO_WRITE: self.card.reg_write(self.cu16(base + 1), temp); base += 3
      elif op == ATOM_IIO_CLEAR: temp &= ~_shl(m(self.cu8(base + 1)), self.cu8(base + 2)) & M32; base += 3
      elif op == ATOM_IIO_SET: temp |= _shl(m(self.cu8(base + 1)), self.cu8(base + 2)); base += 3
      elif op in (ATOM_IIO_MOVE_INDEX, ATOM_IIO_MOVE_DATA, ATOM_IIO_MOVE_ATTR):
        src = {ATOM_IIO_MOVE_INDEX: index, ATOM_IIO_MOVE_DATA: data, ATOM_IIO_MOVE_ATTR: self.io_attr}[op]
        n, sh, dsh = self.cu8(base + 1), self.cu8(base + 2), self.cu8(base + 3)
        temp &= ~_shl(m(n), dsh) & M32
        temp |= _shl(_shr(src, sh) & m(n), dsh)
        base += 4
      elif op == ATOM_IIO_END: return temp
      else: print("ATOM: Unknown IIO opcode"); return 0
    print(f"ATOM: IIO method starting at offset {start} runs past BIOS image")
    return 0

  def execute_table(self, index:int, params:list[int], params_size:int|None=None) -> int:
    """amdgpu_atom_execute_table (:1307-1326): resets data block, reg block, fb window, io mode and divmul, then runs the table. params is the
    caller's uint32 array and is modified in place (PS writes), as in C."""
    self.data_block = self.reg_block = self.fb_base = 0
    self.io_mode = ATOM_IO_MM
    self.divmul = [0, 0]
    self.deadline = time.monotonic() + ATOM_CMD_TIMEOUT_SEC
    return self._execute_locked(index, params, 0, len(params) if params_size is None else params_size)

  def _execute_locked(self, index:int, params:list[int], ps_base:int, ps_size:int) -> int:
    """amdgpu_atom_execute_table_locked (:1229-1305). Returns 0 or a negative errno like C (-EINVAL 22, -ELOOP 40)."""
    base = self.cu16(self.cmd_table + 4 + 2 * index)
    if not base: return -22
    if self.execute_depth >= ATOM_EXECUTE_MAX_DEPTH:
      print(f"ATOM: atombios command table nesting exceeded limit ({ATOM_EXECUTE_MAX_DEPTH})"); return -40
    self.execute_depth += 1
    try:
      ws = self.cu8(base + ATOM_CT_WS_PTR)
      ps = self.cu8(base + ATOM_CT_PS_PTR) & ATOM_CT_PS_MASK
      e = _Exec(self, params, ps_base, ps_size, ps // 4, base, ws)
      e.ptr = base + ATOM_CT_CODE_PTR
      while True:
        op = self.cu8(e.ptr); e.ptr += 1
        if not e.abort and time.monotonic() > self.deadline:
          print(f"ATOM: atombios table {index} ran for more than {ATOM_CMD_TIMEOUT_SEC}secs in total, aborting"); e.abort = True
        if e.abort:
          print(f"ATOM: atombios stuck executing {base:04X} (len {self.cu16(base + ATOM_CT_SIZE_PTR)}, WS {ws}, PS {ps}) @ 0x{e.ptr - 1:04X}")
          return -22
        if 0 < op < ATOM_OP_CNT:
          fn, arg = OPCODE_TABLE[op]
          fn(e, arg)
        else: break      # :1287-1289 op 0 or >= ATOM_OP_CNT ends the table without an error
        if op == ATOM_OP_EOT: break
      return 0
    finally: self.execute_depth -= 1

  def asic_init(self) -> int:
    """amdgpu_atom_asic_init (:1653-1675): ps[0] = default sclk, ps[1] = default mclk from the firmware info table (10 kHz units); 1 when either is 0
    or the ASIC_Init table is absent; else the result of command table 0 with a 16-dword parameter space."""
    hwi = self.cu16(self.data_table + ATOM_DATA_FWI_PTR)
    ps = [0] * 16
    ps[0], ps[1] = self.cu32(hwi + ATOM_FWI_DEFSCLK_PTR), self.cu32(hwi + ATOM_FWI_DEFMCLK_PTR)
    if not ps[0] or not ps[1]: return 1
    if not self.cu16(self.cmd_table + 4 + 2 * ATOM_CMD_INIT): return 1
    return self.execute_table(ATOM_CMD_INIT, ps, 16)

class _Exec:
  """atom_exec_context (:65-74). ps is a window [ps_base, ps_base + ps_size) of the caller's array (C passes a pointer, :646)."""
  def __init__(self, ctx:AtomContext, params:list[int], ps_base:int, ps_size:int, ps_shift:int, start:int, ws:int):
    self.ctx, self.params, self.ps_base, self.ps_size, self.ps_shift, self.start = ctx, params, ps_base, ps_size, ps_shift, start
    self.ws, self.ws_size = [0] * ws, ws
    self.abort, self.last_jump, self.last_jump_time, self.ptr = False, 0, 0.0, 0

  def u8(self) -> int: v = self.ctx.cu8(self.ptr); self.ptr += 1; return v
  def u16(self) -> int: v = self.ctx.cu16(self.ptr); self.ptr += 2; return v
  def u32(self) -> int: v = self.ctx.cu32(self.ptr); self.ptr += 4; return v

  # ---- argument fetch (:190-379), skip (:381-419), direct (:421-445), dst (:447-461), store (:463-605)
  def get_src_int(self, attr:int) -> tuple[int, int]:
    """Returns (masked and shifted value, saved raw value). IMM returns its raw value as both (:308-333 returns before `saved`)."""
    g, arg, align, val = self.ctx, attr & 7, (attr >> 3) & 7, 0xCDCDCDCD
    if arg == ATOM_ARG_REG:
      idx = (self.u16() + g.reg_block) & M32
      if g.io_mode == ATOM_IO_MM: val = g.card.reg_read(idx) & M32
      elif g.io_mode == ATOM_IO_PCI: print("ATOM: PCI registers are not implemented"); return 0, 0
      elif g.io_mode == ATOM_IO_SYSIO: print("ATOM: SYSIO registers are not implemented"); return 0, 0
      else:
        if not g.io_mode & 0x80: print("ATOM: Bad IO mode"); return 0, 0
        if not g.iio[g.io_mode & 0x7F]: print(f"ATOM: Undefined indirect IO read method {g.io_mode & 0x7F}"); return 0, 0
        val = g.iio_execute(g.iio[g.io_mode & 0x7F], idx, 0)
    elif arg == ATOM_ARG_PS:
      idx = self.u8()
      if idx < self.ps_size: val = self.params[self.ps_base + idx] & M32
      else: print(f"ATOM: PS index out of range: {idx} > {self.ps_size}")
    elif arg == ATOM_ARG_WS:
      idx = self.u8()
      if idx == ATOM_WS_QUOTIENT: val = g.divmul[0]
      elif idx == ATOM_WS_REMAINDER: val = g.divmul[1]
      elif idx == ATOM_WS_DATAPTR: val = g.data_block
      elif idx == ATOM_WS_SHIFT: val = g.shift
      elif idx == ATOM_WS_OR_MASK: val = _shl(1, g.shift)
      elif idx == ATOM_WS_AND_MASK: val = ~_shl(1, g.shift) & M32
      elif idx == ATOM_WS_FB_WINDOW: val = g.fb_base
      elif idx == ATOM_WS_ATTRIBUTES: val = g.io_attr
      elif idx == ATOM_WS_REGPTR: val = g.reg_block
      elif idx < self.ws_size: val = self.ws[idx]
      else: print(f"ATOM: WS index out of range: {idx} > {self.ws_size}")
    elif arg == ATOM_ARG_ID:
      val = g.cu32(self.u16() + g.data_block)
    elif arg == ATOM_ARG_FB:
      idx = self.u8()
      di = g.fb_base // 4 + idx
      if g.fb_base + idx * 4 > g.scratch_size_bytes or di >= len(g.scratch):
        print(f"ATOM: fb read beyond scratch region: {g.fb_base + idx * 4} vs. {g.scratch_size_bytes}"); val = 0
      else: val = g.scratch[di]
    elif arg == ATOM_ARG_IMM:
      if align == ATOM_SRC_DWORD: val = self.u32()
      elif align in (ATOM_SRC_WORD0, ATOM_SRC_WORD8, ATOM_SRC_WORD16): val = self.u16()
      else: val = self.u8()
      return val, val
    elif arg == ATOM_ARG_PLL: val = g.card.pll_read(self.u8()) & M32
    elif arg == ATOM_ARG_MC: val = g.card.mc_read(self.u8()) & M32
    saved = val
    return (val & atom_arg_mask[align]) >> atom_arg_shift[align], saved

  def get_src(self, attr:int) -> int: return self.get_src_int(attr)[0]

  def skip_src_int(self, attr:int):
    align, arg = (attr >> 3) & 7, attr & 7
    if arg in (ATOM_ARG_REG, ATOM_ARG_ID): self.ptr += 2
    elif arg in (ATOM_ARG_PLL, ATOM_ARG_MC, ATOM_ARG_PS, ATOM_ARG_WS, ATOM_ARG_FB): self.ptr += 1
    elif arg == ATOM_ARG_IMM: self.ptr += 4 if align == ATOM_SRC_DWORD else 2 if align in (ATOM_SRC_WORD0, ATOM_SRC_WORD8, ATOM_SRC_WORD16) else 1

  def get_src_direct(self, align:int) -> int:
    if align == ATOM_SRC_DWORD: return self.u32()
    if align in (ATOM_SRC_WORD0, ATOM_SRC_WORD8, ATOM_SRC_WORD16): return self.u16()
    return self.u8()

  @staticmethod
  def _dst_attr(arg:int, attr:int) -> int: return arg | atom_dst_to_src[(attr >> 3) & 7][(attr >> 6) & 3] << 3
  def get_dst(self, arg:int, attr:int) -> tuple[int, int]: return self.get_src_int(self._dst_attr(arg, attr))
  def skip_dst(self, arg:int, attr:int): self.skip_src_int(self._dst_attr(arg, attr))

  def put_dst(self, arg:int, attr:int, dptr:int, val:int, saved:int):
    g, align = self.ctx, atom_dst_to_src[(attr >> 3) & 7][(attr >> 6) & 3]
    val = (_shl(val, atom_arg_shift[align]) & atom_arg_mask[align]) | (saved & ~atom_arg_mask[align] & M32)
    if arg == ATOM_ARG_REG:
      idx = (g.cu16(dptr) + g.reg_block) & M32
      if g.io_mode == ATOM_IO_MM: g.card.reg_write(idx, _shl(val, 2) if idx == 0 else val)   # :479-483 index 0 = MM_INDEX takes a byte address
      elif g.io_mode == ATOM_IO_PCI: print("ATOM: PCI registers are not implemented")
      elif g.io_mode == ATOM_IO_SYSIO: print("ATOM: SYSIO registers are not implemented")
      else:
        if not g.io_mode & 0x80: print("ATOM: Bad IO mode"); return
        # :497-503 the write method is iio[io_mode & 0xFF] (read uses & 0x7F, :233-241): Linux's indexing, kept verbatim
        if not g.iio[g.io_mode & 0xFF]: print(f"ATOM: Undefined indirect IO write method {g.io_mode & 0x7F}"); return
        g.iio_execute(g.iio[g.io_mode & 0xFF], idx, val)
    elif arg == ATOM_ARG_PS:
      idx = g.cu8(dptr)
      if idx >= self.ps_size: print(f"ATOM: PS index out of range: {idx} > {self.ps_size}"); return
      self.params[self.ps_base + idx] = val
    elif arg == ATOM_ARG_WS:
      idx = g.cu8(dptr)
      if idx == ATOM_WS_QUOTIENT: g.divmul[0] = val
      elif idx == ATOM_WS_REMAINDER: g.divmul[1] = val
      elif idx == ATOM_WS_DATAPTR: g.data_block = val & 0xFFFF       # uint16_t data_block (atom.h:140)
      elif idx == ATOM_WS_SHIFT: g.shift = val & 0xFF                # uint8_t shift (atom.h:136)
      elif idx in (ATOM_WS_OR_MASK, ATOM_WS_AND_MASK): pass
      elif idx == ATOM_WS_FB_WINDOW: g.fb_base = val
      elif idx == ATOM_WS_ATTRIBUTES: g.io_attr = val & 0xFFFF       # uint16_t io_attr (atom.h:143)
      elif idx == ATOM_WS_REGPTR: g.reg_block = val & 0xFFFF         # uint16_t reg_block (atom.h:144)
      elif idx >= self.ws_size: print(f"ATOM: WS index out of range: {idx} > {self.ws_size}")
      else: self.ws[idx] = val
    elif arg == ATOM_ARG_FB:
      idx = g.cu8(dptr)
      di = g.fb_base // 4 + idx
      if g.fb_base + idx * 4 > g.scratch_size_bytes or di >= len(g.scratch):
        print(f"ATOM: fb write beyond scratch region: {g.fb_base + idx * 4} vs. {g.scratch_size_bytes}")
      else: g.scratch[di] = val
    elif arg == ATOM_ARG_PLL: g.card.pll_write(g.cu8(dptr), val)
    elif arg == ATOM_ARG_MC: g.card.mc_write(g.cu8(dptr), val)

  # ---- opcodes (:607-1096)
  def _rmw(self, arg:int, f:Callable[[int, int], int]):
    """The shared shape of add/and/or/sub/xor (:607-631, :849-859, :1016-1027, :1067-1078): attr, dst (with saved), src, put."""
    attr = self.u8(); dptr = self.ptr
    dst, saved = self.get_dst(arg, attr)
    src = self.get_src(attr)
    self.put_dst(arg, attr, dptr, f(dst, src) & M32, saved)
  def op_add(self, arg): self._rmw(arg, lambda d, s: d + s)
  def op_and(self, arg): self._rmw(arg, lambda d, s: d & s)
  def op_or(self, arg): self._rmw(arg, lambda d, s: d | s)
  def op_sub(self, arg): self._rmw(arg, lambda d, s: d - s)
  def op_xor(self, arg): self._rmw(arg, lambda d, s: d ^ s)

  def op_beep(self, arg): print("ATOM BIOS beeped!")                                              # :635-638
  def op_calltable(self, arg):                                                                    # :640-654
    idx, g = self.u8(), self.ctx
    r = 0
    if g.cu16(g.cmd_table + 4 + 2 * idx):
      r = g._execute_locked(idx, self.params, self.ps_base + self.ps_shift, self.ps_size - self.ps_shift)
    if r: self.abort = True
  def op_clear(self, arg):                                                                        # :656-666
    attr = self.u8(); dptr = self.ptr
    attr &= 0x38
    attr |= atom_def_dst[attr >> 3] << 6
    _, saved = self.get_dst(arg, attr)
    self.put_dst(arg, attr, dptr, 0, saved)
  def op_compare(self, arg):                                                                      # :668-680
    attr = self.u8()
    dst, _ = self.get_dst(arg, attr)
    src = self.get_src(attr)
    self.ctx.cs_equal, self.ctx.cs_above = dst == src, dst > src
  def op_delay(self, arg):                                                                        # :682-692 udelay / msleep
    count = self.u8()
    time.sleep(count / 1e6 if arg == ATOM_UNIT_MICROSEC else count / 1e3)
  def op_div(self, arg):                                                                          # :694-709
    attr = self.u8()
    dst, _ = self.get_dst(arg, attr)
    src = self.get_src(attr)
    self.ctx.divmul = [dst // src, dst % src] if src else [0, 0]
  def op_div32(self, arg):                                                                        # :711-730
    attr = self.u8()
    dst, _ = self.get_dst(arg, attr)
    src = self.get_src(attr)
    if src:
      q = (dst | (self.ctx.divmul[1] << 32)) // src
      self.ctx.divmul = [q & M32, (q >> 32) & M32]
    else: self.ctx.divmul = [0, 0]
  def op_eot(self, arg): pass                                                                     # :732-735
  def op_jump(self, arg):                                                                         # :737-789
    g, target = self.ctx, self.u16()
    execute = {ATOM_COND_ABOVE: g.cs_above, ATOM_COND_ABOVEOREQUAL: g.cs_above or g.cs_equal, ATOM_COND_ALWAYS: True,
               ATOM_COND_BELOW: not (g.cs_above or g.cs_equal), ATOM_COND_BELOWOREQUAL: not g.cs_above, ATOM_COND_EQUAL: g.cs_equal,
               ATOM_COND_NOTEQUAL: not g.cs_equal}[arg]
    if execute:
      now = time.monotonic()
      if self.last_jump == self.start + target:   # :769-783 the same backward target for more than 20 s = stuck loop -> abort
        if now - self.last_jump_time > ATOM_CMD_TIMEOUT_SEC:
          print(f"ATOM: atombios stuck in loop for more than {ATOM_CMD_TIMEOUT_SEC}secs aborting"); self.abort = True
      else: self.last_jump, self.last_jump_time = self.start + target, now
      self.ptr = self.start + target
  def op_mask(self, arg):                                                                         # :791-806
    attr = self.u8(); dptr = self.ptr
    dst, saved = self.get_dst(arg, attr)
    mask = self.get_src_direct((attr >> 3) & 7)
    src = self.get_src(attr)
    self.put_dst(arg, attr, dptr, ((dst & mask) | src) & M32, saved)
  def op_move(self, arg):                                                                         # :808-823
    attr = self.u8(); dptr = self.ptr
    if (attr >> 3) & 7 != ATOM_SRC_DWORD: _, saved = self.get_dst(arg, attr)
    else: self.skip_dst(arg, attr); saved = 0xCDCDCDCD
    src = self.get_src(attr)
    self.put_dst(arg, attr, dptr, src, saved)
  def op_mul(self, arg):                                                                          # :825-834
    attr = self.u8()
    dst, _ = self.get_dst(arg, attr)
    src = self.get_src(attr)
    self.ctx.divmul[0] = (dst * src) & M32
  def op_mul32(self, arg):                                                                        # :836-847
    attr = self.u8()
    dst, _ = self.get_dst(arg, attr)
    src = self.get_src(attr)
    p = dst * src
    self.ctx.divmul = [p & M32, (p >> 32) & M32]
  def op_nop(self, arg): pass
  def op_postcard(self, arg): self.u8()                                                           # :861-866
  def op_unimplemented(self, arg): print("ATOM: unimplemented!")                                  # repeat/restorereg/savereg :868-881, no ptr advance
  def op_setdatablock(self, arg):                                                                 # :890-902
    idx, g = self.u8(), self.ctx
    g.data_block = 0 if not idx else self.start if idx == 255 else g.cu16(g.data_table + 4 + 2 * idx)
  def op_setfbbase(self, arg):                                                                    # :904-909
    attr = self.u8()
    self.ctx.fb_base = self.get_src(attr)
  def op_setport(self, arg):                                                                      # :911-937
    g = self.ctx
    if arg == ATOM_PORT_ATI:
      port = g.cu16(self.ptr)
      g.io_mode = ATOM_IO_MM if not port else ATOM_IO_IIO | port
      self.ptr += 2
    elif arg == ATOM_PORT_PCI: g.io_mode = ATOM_IO_PCI; self.ptr += 1
    elif arg == ATOM_PORT_SYSIO: g.io_mode = ATOM_IO_SYSIO; self.ptr += 1
  def op_setregblock(self, arg): self.ctx.reg_block = self.u16()                                  # :939-943
  def _shift_direct(self, arg:int, left:bool):                                                    # shift_left :945-959, shift_right :961-975
    attr = self.u8(); dptr = self.ptr
    attr &= 0x38
    attr |= atom_def_dst[attr >> 3] << 6
    dst, saved = self.get_dst(arg, attr)
    shift = self.get_src_direct(ATOM_SRC_BYTE0)
    self.put_dst(arg, attr, dptr, _shl(dst, shift) if left else _shr(dst, shift), saved)
  def op_shift_left(self, arg): self._shift_direct(arg, True)
  def op_shift_right(self, arg): self._shift_direct(arg, False)
  def _shift_full(self, arg:int, left:bool):                                                      # shl :977-995, shr :997-1014
    attr = self.u8(); dptr = self.ptr
    dst_align = atom_dst_to_src[(attr >> 3) & 7][(attr >> 6) & 3]
    _, saved = self.get_dst(arg, attr)
    dst = saved                       # "op needs to full dst value"
    shift = self.get_src(attr) & 0xFF   # uint8_t shift
    dst = _shl(dst, shift) if left else _shr(dst, shift)
    dst = (dst & atom_arg_mask[dst_align]) >> atom_arg_shift[dst_align]
    self.put_dst(arg, attr, dptr, dst, saved)
  def op_shl(self, arg): self._shift_full(arg, True)
  def op_shr(self, arg): self._shift_full(arg, False)
  def op_switch(self, arg):                                                                       # :1029-1054
    g, attr = self.ctx, self.u8()
    src = self.get_src(attr)
    while g.cu16(self.ptr) != ATOM_CASE_END:
      if g.cu8(self.ptr) == ATOM_CASE_MAGIC:
        self.ptr += 1
        val = self.get_src((attr & 0x38) | ATOM_ARG_IMM)
        target = g.cu16(self.ptr)
        if val == src:
          self.ptr = self.start + target
          return
        self.ptr += 2
      else:
        print("ATOM: Bad case"); return
    self.ptr += 2
  def op_test(self, arg):                                                                         # :1056-1065
    attr = self.u8()
    dst, _ = self.get_dst(arg, attr)
    src = self.get_src(attr)
    self.ctx.cs_equal = (dst & src) == 0
  def op_debug(self, arg): self.u8()                                                              # :1080-1084
  def op_processds(self, arg):                                                                    # :1086-1091
    val = self.ctx.cu16(self.ptr)
    self.ptr += val + 2

def _build_opcode_table() -> list:
  """opcode_table[ATOM_OP_CNT] (:1093-1227), same order: index 0 is NULL."""
  E, A6 = _Exec, [ATOM_ARG_REG, ATOM_ARG_PS, ATOM_ARG_WS, ATOM_ARG_FB, ATOM_ARG_PLL, ATOM_ARG_MC]
  t:list = [(None, 0)]
  for f in (E.op_move, E.op_and, E.op_or, E.op_shift_left, E.op_shift_right, E.op_mul, E.op_div, E.op_add, E.op_sub): t += [(f, a) for a in A6]
  t += [(E.op_setport, ATOM_PORT_ATI), (E.op_setport, ATOM_PORT_PCI), (E.op_setport, ATOM_PORT_SYSIO), (E.op_setregblock, 0), (E.op_setfbbase, 0)]
  t += [(E.op_compare, a) for a in A6]
  t += [(E.op_switch, 0)]
  t += [(E.op_jump, c) for c in (ATOM_COND_ALWAYS, ATOM_COND_EQUAL, ATOM_COND_BELOW, ATOM_COND_ABOVE, ATOM_COND_BELOWOREQUAL,
                                 ATOM_COND_ABOVEOREQUAL, ATOM_COND_NOTEQUAL)]
  t += [(E.op_test, a) for a in A6]
  t += [(E.op_delay, ATOM_UNIT_MILLISEC), (E.op_delay, ATOM_UNIT_MICROSEC), (E.op_calltable, 0), (E.op_unimplemented, 0)]
  t += [(E.op_clear, a) for a in A6]
  t += [(E.op_nop, 0), (E.op_eot, 0)]
  t += [(E.op_mask, a) for a in A6]
  t += [(E.op_postcard, 0), (E.op_beep, 0), (E.op_unimplemented, 0), (E.op_unimplemented, 0), (E.op_setdatablock, 0)]
  for f in (E.op_xor, E.op_shl, E.op_shr): t += [(f, a) for a in A6]
  t += [(E.op_debug, 0), (E.op_processds, 0), (E.op_mul32, ATOM_ARG_PS), (E.op_mul32, ATOM_ARG_WS), (E.op_div32, ATOM_ARG_PS),
        (E.op_div32, ATOM_ARG_WS)]
  assert len(t) == ATOM_OP_CNT, f"opcode table has {len(t)} entries, atom.h ATOM_OP_CNT = {ATOM_OP_CNT}"
  assert t[ATOM_OP_EOT][0] is E.op_eot, "ATOM_OP_EOT (91) must be atom_op_eot"
  return t

OPCODE_TABLE = _build_opcode_table()
