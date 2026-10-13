from __future__ import annotations
import ctypes, collections, dataclasses, functools, hashlib, array, os, time
from tinygrad.helpers import mv_address, getenv, DEBUG, lo32, hi32, fetch_fw, to_mv, wait_cond
from tinygrad.runtime.autogen import pci
from tinygrad.runtime.autogen.am import am, fw
from tinygrad.runtime.support.amd import AMDReg, import_module, import_asic_regs
from tinygrad.runtime.support.memory import TLSFAllocator, MemoryManager, AddrSpace
from tinygrad.runtime.support.system import PCIDevice
from tinygrad.runtime.support.am.ip import AM_IP, AM_SOC, AM_GMC, AM_IH, AM_PSP, AM_SMU, AM_GFX, AM_SDMA
from tinygrad.runtime.support.am import vbios

AM_DEBUG = getenv("AM_DEBUG", 0)

# mi50 fork: config dword 0 of the MI50 = device 0x66a1 << 16 | vendor 0x1002 (design v2 §1.1, §3 "_run_discovery" row)
VEGA20_PCI_ID = 0x66a11002

def _required_env(name:str, what:str, lo:int, hi:int) -> int:
  """Values that are Leon's to rule (design v2 §8 U-S6: 'none exist in the trees'): no default, a clear error when absent. Environment is the
  one mechanism because AMDev is constructed by PCIIfaceBase/system.py (not editable here); constructor arguments would be unreachable.
  Parse rule = extra/mi50/ladder.py _env_int (review 2026-10-13 TI4/S13: one rule for the ladder and the driver): int(v, 0), bounded lo..hi."""
  if (v:=os.environ.get(name)) is None or v.strip() == "":
    raise RuntimeError(f"MI50 (Vega 20): environment variable {name} = {what} is REQUIRED and has no default - the value is Leon's to rule "
                       "(design v2 §3/§8 U-S6)")
  try: n = int(v, 0)
  except ValueError: raise RuntimeError(f"MI50 (Vega 20): {name}={v!r} is not an integer ({what})") from None
  if not lo <= n <= hi: raise RuntimeError(f"MI50 (Vega 20): {name}={n} outside {lo}..{hi} ({what})")
  return n

@dataclasses.dataclass
class AMRegister(AMDReg):
  adev:AMDev

  def read(self, inst=0): return self.adev.rreg(self.addr[inst])
  def read_bitfields(self, inst=0) -> dict[str, int]: return self.decode(self.read(inst=inst))

  def write(self, _am_val:int=0, inst=0, **kwargs): self.adev.wreg(self.addr[inst], _am_val | self.encode(**kwargs))

  def update(self, inst=0, **kwargs): self.write(self.read(inst=inst) & ~self.fields_mask(*kwargs.keys()), inst=inst, **kwargs)

class AMFirmware:
  def __init__(self, adev):
    self.adev = adev
    def fmt_ver(hwip): return '_'.join(map(str, adev.ip_ver[hwip]))

    # mi50 fork: Vega 20 ships under linux-firmware's legacy names (amdgpu_ucode_legacy_naming: "vega20") with v1.0 headers
    if adev.ip_ver[am.MP0_HWIP] == (11,0,2): return self._init_vega20()

    # Load SOS firmware
    self.sos_fw = {}

    blob, sos_hdr = self.load_fw(f"psp_{fmt_ver(am.MP0_HWIP)}_sos.bin", versioned_header='struct_psp_firmware_header')
    fw_bin = sos_hdr.psp_fw_bin

    for fw_i in range(sos_hdr.psp_fw_bin_count):
      fw_bin_desc = am.struct_psp_fw_bin_desc.from_address(ctypes.addressof(fw_bin) + fw_i * ctypes.sizeof(am.struct_psp_fw_bin_desc))
      ucode_start_offset = fw_bin_desc.offset_bytes + sos_hdr.header.ucode_array_offset_bytes
      self.sos_fw[fw_bin_desc.fw_type] = blob[ucode_start_offset:ucode_start_offset+fw_bin_desc.size_bytes]

    # Load other fw
    self.ucode_start: dict[str, int] = {}
    self.descs: list[tuple[list[int], memoryview]] = []

    # SMU firmware
    if adev.ip_ver[am.MP1_HWIP] != (13,0,12):
      blob, hdr = self.load_fw(f"smu_{fmt_ver(am.MP1_HWIP)}.bin", versioned_header="struct_smc_firmware_header")
      if self.adev.ip_ver[am.GC_HWIP] >= (11,0,0):
        self.smu_psp_desc = self.desc(blob, hdr.v1_0.header.ucode_array_offset_bytes, hdr.v1_0.header.ucode_size_bytes, am.GFX_FW_TYPE_SMU)
      else:
        p2stables = (am.struct_smc_soft_pptable_entry * hdr.pptable_count).from_buffer(blob[hdr.pptable_entry_offset:])
        for p2stable in p2stables:
          if p2stable.id == (__P2S_TABLE_ID_X:=0x50325358):
            self.descs += [self.desc(blob, p2stable.ppt_offset_bytes, p2stable.ppt_size_bytes, am.GFX_FW_TYPE_P2S_TABLE)]

    # SDMA firmware
    blob, hdr = self.load_fw(f"sdma_{fmt_ver(am.SDMA0_HWIP)}.bin", versioned_header="struct_sdma_firmware_header")
    if hdr.header.header_version_major == 1:
      self.descs += [self.desc(blob, hdr.header.ucode_array_offset_bytes, hdr.header.ucode_size_bytes, am.GFX_FW_TYPE_SDMA0,
                               am.GFX_FW_TYPE_SDMA1, am.GFX_FW_TYPE_SDMA2, am.GFX_FW_TYPE_SDMA3)]
    elif hdr.header.header_version_major == 2:
      self.descs += [self.desc(blob, hdr.ctl_ucode_offset, hdr.ctl_ucode_size_bytes, am.GFX_FW_TYPE_SDMA_UCODE_TH1)]
      self.descs += [self.desc(blob, hdr.header.ucode_array_offset_bytes, hdr.ctx_ucode_size_bytes, am.GFX_FW_TYPE_SDMA_UCODE_TH0)]
    else: self.descs += [self.desc(blob, hdr.header.ucode_array_offset_bytes, hdr.ucode_size_bytes, am.GFX_FW_TYPE_SDMA_UCODE_TH0)]

    # PFP, ME, MEC firmware
    for (fw_name, fw_cnt) in ([('PFP', 1), ('ME', 1)] if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0) else []) + [('MEC', 1)]:
      blob, hdr = self.load_fw(f"gc_{fmt_ver(am.GC_HWIP)}_{fw_name.lower()}.bin", versioned_header="struct_gfx_firmware_header")

      ucode_off = hdr.header.ucode_array_offset_bytes
      if hdr.header.header_version_major == 1:
        # Code
        self.descs += [self.desc(blob, ucode_off, hdr.header.ucode_size_bytes - hdr.jt_size * 4, getattr(am, f'GFX_FW_TYPE_CP_{fw_name}'))]
        # JT
        self.descs += [self.desc(blob, ucode_off + hdr.jt_offset * 4, hdr.jt_size * 4, getattr(am, f'GFX_FW_TYPE_CP_{fw_name}_ME1'))]
      else:
        # Code
        self.descs += [self.desc(blob, ucode_off, hdr.ucode_size_bytes, getattr(am, f'GFX_FW_TYPE_RS64_{fw_name}'))]
        # Stack
        stack_fws = [getattr(am, f'GFX_FW_TYPE_RS64_{fw_name}_P{fwnum}_STACK') for fwnum in range(fw_cnt)]
        self.descs += [self.desc(blob, hdr.data_offset_bytes, hdr.data_size_bytes, *stack_fws)]
        self.ucode_start[fw_name] = hdr.ucode_start_addr_lo | (hdr.ucode_start_addr_hi << 32)

    # IMU firmware
    if self.adev.ip_ver[am.GC_HWIP] >= (11,0,0):
      blob, hdr = self.load_fw(f"gc_{fmt_ver(am.GC_HWIP)}_imu.bin", am.struct_imu_firmware_header_v1_0)
      imu_i_off, imu_i_sz, imu_d_sz = hdr.header.ucode_array_offset_bytes, hdr.imu_iram_ucode_size_bytes, hdr.imu_dram_ucode_size_bytes
      self.descs += [self.desc(blob, imu_i_off, imu_i_sz, am.GFX_FW_TYPE_IMU_I), self.desc(blob, imu_i_off+imu_i_sz, imu_d_sz, am.GFX_FW_TYPE_IMU_D)]

    # RLC firmware
    blob, hdr0, hdr1, hdr2, hdr3 = self.load_fw(f"gc_{fmt_ver(am.GC_HWIP)}_rlc.bin", am.struct_rlc_firmware_header_v2_0,
      am.struct_rlc_firmware_header_v2_1, am.struct_rlc_firmware_header_v2_2, am.struct_rlc_firmware_header_v2_3)

    if hdr0.header.header_version_minor == 1:
      for mem,fmem in [('LIST_SRM_CNTL', 'list_cntl'), ('LIST_GPM_MEM', 'list_gpm'), ('LIST_SRM_MEM', 'list_srm')]:
        off, sz = getattr(hdr1, f'save_restore_{fmem}_offset_bytes'), getattr(hdr1, f'save_restore_{fmem}_size_bytes')
        self.descs += [self.desc(blob, off, sz, getattr(am, f'GFX_FW_TYPE_RLC_RESTORE_{mem}'))]

    if hdr0.header.header_version_minor >= 2:
      for mem,fmem in [('IRAM', 'iram'), ('DRAM_BOOT', 'dram')]:
        off, sz = getattr(hdr2, f'rlc_{fmem}_ucode_offset_bytes'), getattr(hdr2, f'rlc_{fmem}_ucode_size_bytes')
        self.descs += [self.desc(blob, off, sz, getattr(am, f'GFX_FW_TYPE_RLC_{mem}'))]

    if hdr0.header.header_version_minor == 3:
      for mem in ['P', 'V']:
        off, sz = getattr(hdr3, f'rlc{mem.lower()}_ucode_offset_bytes'), getattr(hdr3, f'rlc{mem.lower()}_ucode_size_bytes')
        self.descs += [self.desc(blob, off, sz, getattr(am, f'GFX_FW_TYPE_RLC_{mem}'))]

    self.descs += [self.desc(blob, hdr0.header.ucode_array_offset_bytes, hdr0.header.ucode_size_bytes, am.GFX_FW_TYPE_RLC_G)]

  def _init_vega20(self):
    """Vega 20 / MI50 firmware set, measured 2026-10-13 (egpu/mi50/port/Vega20 Firmware Facts.md):
    vega20_sos.bin is a psp v1.0 header = sys driver + sOS back to back (Linux psp_init_sos_microcode: sys = ucode[0:sos.offset_bytes],
    sos = ucode[sos.offset_bytes:+sos.size_bytes]); no KDB/TOC/SPL/RL. The PSP hands the non-PSP firmware over in Linux's ucode enum
    order (amdgpu_psp.c psp_load_non_psp_fw): SDMA0, SDMA1, MEC1 code, MEC1 JT, RLC restore lists (v2.1, when sized), RLC_G, then
    SMC (amdgpu_cgs.c registers vega20_smc.bin as AMDGPU_UCODE_ID_SMC -> GFX_FW_TYPE_SMU) - so SMC goes LAST in descs here and
    smu_psp_desc (which AM_PSP loads before the TMR) is deliberately not set. ASD is kept for a LOAD_ASD command.
    Review 2026-10-13 (L2): CE/PFP/ME and MEC2 are loaded like Linux does for CHIP_VEGA20 - gfx_v9_0_early_init gives GC 9.4.0 num_gfx_rings =
    GFX9_NUM_GFX_RINGS (gfx_v9_0.c:4862-4866, only 9.4.1/9.4.2 get 0) so gfx_v9_0_init_microcode requests pfp/me/ce (:1607-1611 -> :1455-1480,
    whole ucode_size_bytes each, amdgpu_gfx.c:1489-1552), and gfx_v9_0_load_mec2_fw_bin_support is true for 9.4.0 (:1539-1547) so mec2 is
    requested and registered as CP_MEC2 + CP_MEC2_JT (:1568-1579) = GFX_FW_TYPE_CP_MEC + GFX_FW_TYPE_CP_MEC_ME2 (amdgpu_psp.c:3157-3160).
    Enum order amdgpu_ucode.h:480-531: SDMA0, SDMA1, CP_CE, CP_PFP, CP_ME, CP_MEC1, CP_MEC1_JT, CP_MEC2, CP_MEC2_JT, RLC lists, RLC_G, SMC
    (= design v2 §4 "PSP load order emitted by the fork"). vega20_mec2.bin is byte-identical to vega20_mec.bin (fw.py hashes; Firmware Facts)."""
    self.sos_fw, self.ucode_start, self.descs = {}, {}, []
    blob, hdr = self.load_fw("vega20_sos.bin", am.struct_psp_firmware_header_v1_0)
    assert (hdr.header.header_version_major, hdr.header.header_version_minor) == (1, 0), "vega20_sos.bin: expected a v1.0 psp header"
    base = hdr.header.ucode_array_offset_bytes
    self.sos_fw[am.PSP_FW_TYPE_PSP_SYS_DRV] = blob[base:base + hdr.sos.offset_bytes]
    self.sos_fw[am.PSP_FW_TYPE_PSP_SOS] = blob[base + hdr.sos.offset_bytes:base + hdr.sos.offset_bytes + hdr.sos.size_bytes]

    for fname, fw_type in [("vega20_sdma.bin", am.GFX_FW_TYPE_SDMA0), ("vega20_sdma1.bin", am.GFX_FW_TYPE_SDMA1)]:
      blob, hdr = self.load_fw(fname, am.struct_sdma_firmware_header_v1_0)
      self.descs += [self.desc(blob, hdr.header.ucode_array_offset_bytes, hdr.header.ucode_size_bytes, fw_type)]

    # CP_CE 3, CP_PFP 2, CP_ME 1: whole ucode_size_bytes at ucode_array_offset_bytes (amdgpu_gfx.c:1489-1498, :1516-1525, :1543-1552)
    for fname, fw_type in [("vega20_ce.bin", am.GFX_FW_TYPE_CP_CE), ("vega20_pfp.bin", am.GFX_FW_TYPE_CP_PFP), ("vega20_me.bin", am.GFX_FW_TYPE_CP_ME)]:
      blob, hdr = self.load_fw(fname, am.struct_gfx_firmware_header_v1_0)
      self.descs += [self.desc(blob, hdr.header.ucode_array_offset_bytes, hdr.header.ucode_size_bytes, fw_type)]

    # MEC1 then MEC2, each = code (ucode_size - jt_size*4) + JT (jt_size*4 at jt_offset*4), amdgpu_ucode.c (CP_MEC1/MEC1_JT and CP_MEC2/MEC2_JT)
    for fname, jt_type in [("vega20_mec.bin", am.GFX_FW_TYPE_CP_MEC_ME1), ("vega20_mec2.bin", am.GFX_FW_TYPE_CP_MEC_ME2)]:
      blob, hdr = self.load_fw(fname, am.struct_gfx_firmware_header_v1_0)
      ucode_off = hdr.header.ucode_array_offset_bytes
      self.descs += [self.desc(blob, ucode_off, hdr.header.ucode_size_bytes - hdr.jt_size * 4, am.GFX_FW_TYPE_CP_MEC)]
      self.descs += [self.desc(blob, ucode_off + hdr.jt_offset * 4, hdr.jt_size * 4, jt_type)]

    blob, hdr0, hdr1 = self.load_fw("vega20_rlc.bin", am.struct_rlc_firmware_header_v2_0, am.struct_rlc_firmware_header_v2_1)
    if hdr0.header.header_version_minor >= 1:
      for mem, fmem in [('LIST_SRM_CNTL', 'list_cntl'), ('LIST_GPM_MEM', 'list_gpm'), ('LIST_SRM_MEM', 'list_srm')]:
        off, sz = getattr(hdr1, f'save_restore_{fmem}_offset_bytes'), getattr(hdr1, f'save_restore_{fmem}_size_bytes')
        if sz: self.descs += [self.desc(blob, off, sz, getattr(am, f'GFX_FW_TYPE_RLC_RESTORE_{mem}'))]   # Linux amdgpu_rlc.c: only when sized
    self.descs += [self.desc(blob, hdr0.header.ucode_array_offset_bytes, hdr0.header.ucode_size_bytes, am.GFX_FW_TYPE_RLC_G)]

    blob, hdr = self.load_fw("vega20_smc.bin", am.struct_smc_firmware_header_v1_0)
    self.smu_version = hdr.header.ucode_version
    self.descs += [self.desc(blob, hdr.header.ucode_array_offset_bytes, hdr.header.ucode_size_bytes, am.GFX_FW_TYPE_SMU)]

    blob, hdr = self.load_fw("vega20_asd.bin", am.struct_psp_firmware_header_v1_0)
    self.asd_fw = blob[hdr.header.ucode_array_offset_bytes:hdr.header.ucode_array_offset_bytes + hdr.header.ucode_size_bytes]

  def load_fw(self, fname:str, *headers, versioned_header:str|None=None):
    blob = memoryview(bytearray(fetch_fw("amdgpu", fname, fw.hashes[fname])))
    if AM_DEBUG >= 1: print(f"am {self.adev.devfmt}: loading firmware {fname}: {hashlib.sha256(blob).hexdigest()}")
    if versioned_header:
      chdr = am.struct_common_firmware_header.from_address(mv_address(blob))
      headers += (getattr(am, versioned_header + f"_v{chdr.header_version_major}_{chdr.header_version_minor}"),)
    return tuple([blob] + [hdr.from_address(mv_address(blob)) for hdr in headers])

  def desc(self, blob:memoryview, offset:int, size:int, *types:int) -> tuple[list[int], memoryview]: return (list(types), blob[offset:offset+size])

class AMPageTableEntry:
  def __init__(self, adev, paddr, lv): self.adev, self.paddr, self.lv, self.entries = adev, paddr, lv, adev.vram.view(paddr, 0x1000, fmt='Q')

  def set_entry(self, entry_id:int, paddr:int, table=False, uncached=False, aspace=AddrSpace.PHYS, snooped=False, frag=0, valid=True):
    is_sys = aspace is AddrSpace.SYS
    if aspace is AddrSpace.PHYS: paddr = self.adev.paddr2xgmi(paddr)
    assert paddr & self.adev.gmc.address_space_mask == paddr, f"Invalid physical address {paddr:#x}"
    self.entries[entry_id] = self.adev.gmc.get_pte_flags(self.lv, table, frag, uncached, is_sys, snooped, valid) | (paddr & 0x0000FFFFFFFFF000)

  def entry(self, entry_id:int) -> int: return self.entries[entry_id]
  def valid(self, entry_id:int) -> bool: return (self.entries[entry_id] & am.AMDGPU_PTE_VALID) != 0
  def address(self, entry_id:int) -> int:
    assert self.entries[entry_id] & am.AMDGPU_PTE_SYSTEM == 0, "should not be system address"
    return self.adev.xgmi2paddr(self.entries[entry_id] & 0x0000FFFFFFFFF000)
  def is_page(self, entry_id:int) -> bool: return self.lv == am.AMDGPU_VM_PTB or self.adev.gmc.is_pte_huge_page(self.lv, self.entries[entry_id])
  def supports_huge_page(self, paddr:int): return self.lv >= am.AMDGPU_VM_PDB2

class AMMemoryManager(MemoryManager):
  va_allocator = TLSFAllocator((1 << 44), base=0x200000000000) # global for all devices.

  def on_range_mapped(self):
    # Invalidate TLB after mappings.
    self.dev.gmc.flush_tlb(ip='GC', vmid=0)
    self.dev.gmc.flush_tlb(ip='MM', vmid=0)

class AMDev:
  Version = 0xA0000008
  # mi50 fork: PLACEHOLDER, NOT A RULING - the value is Leon's (design v2 §3 row 1 "value: Leon"). Design B7 only requires it to differ from stock
  # AM's 0xA0000008 (above) and master's 0xA000000D (tinygrad-master runtime/support/am/amdev.py:145) so a stock-AM SCRATCH_REG7 never passes as a
  # partial boot on the MI50. Used only when self.is_vega20 (self.boot_version); every other card keeps AMDev.Version byte for byte.
  VEGA20_BOOT_VERSION_PLACEHOLDER_NOT_RULED_BY_LEON = 0x0066A100
  assert VEGA20_BOOT_VERSION_PLACEHOLDER_NOT_RULED_BY_LEON not in (Version, 0xA000000D)

  def _disable_aspm(self):
    # L1 across retimers makes reads oscillate to 0xffffffff; power on defaults it enabled. Clearing the GPU endpoint
    # alone suffices: L1 only engages when both ends of the link enable it.
    cap, seen = self.pci_dev.read_config(0x34, 1) & 0xfc, set() # bound the walk: a dead link can return 0xff pointers forever
    while cap and cap not in seen and self.pci_dev.read_config(cap, 1) != 0x10:
      seen.add(cap)
      cap = self.pci_dev.read_config(cap + 1, 1) & 0xfc
    if cap and cap not in seen: self.pci_dev.write_config_flush(cap + 0x10, self.pci_dev.read_config(cap + 0x10, 2) & ~3, 2) # PCIe cap lnkctl

  def __init__(self, pci_dev:PCIDevice, reset_mode=False):
    self.pci_dev, self.devfmt = pci_dev, pci_dev.pcibus
    # mi50 fork (design v2 §3 "_run_discovery" row; review 2026-10-13 TI4/S13): config dword 0 is the only gate that exists before discovery. This
    # is the ONE CFG_READ _run_discovery made before (moved here, not added: other cards see the same single inert read), so the two REQUIRED
    # inputs are refused before the first config write (_disable_aspm) and before any MMIO - a missing input must never end in
    # _emergency_quiesce's SMU messages.
    self.is_vega20 = self.pci_dev.read_config(0, 4) == VEGA20_PCI_ID
    if self.is_vega20: self._vega20_required_inputs()
    self._disable_aspm()
    self.vram, self.doorbell64, self.mmio = self.pci_dev.map_bar(0), self.pci_dev.map_bar(2, fmt='Q'), self.pci_dev.map_bar(5, fmt='I')

    # mi50 fork (design v2 §3 "__init__" row, critic gap 27): fini() is unreachable when __init__ raises and PCIIfaceBase only holds the object
    # after dev_impl_t returns (system.py:261), so the Vega 20 quiesces itself before the exception propagates. Other cards: no-op (gated inside).
    try: self._boot(reset_mode)
    except BaseException:
      self._emergency_quiesce()
      raise

  def _vega20_required_inputs(self):
    # the two values that are Leon's to rule (design v2 §3 rows / §8 U-S6); same parse + bounds as extra/mi50/ladder.py env_cap / env_threshold
    self.mi50_ppt_w = _required_env("AM_MI50_PPT_W", "SetPptLimit cap in W (design v2 B16/T3; PPSMC_MSG_SetPptLimit 0xF)", 1, 0xffff)
    self.mi50_temp_c = _required_env("AM_MI50_TEMP_C", "hotspot abort threshold in C (design v2 §7.0 rule 4: 9-bit CTF_TEMP, 0/511 invalid)", 1, 510)

  def _boot(self, reset_mode:bool):
    self._run_discovery()
    self._build_regs()
    # mi50 fork: the partial-boot marker is per card; non-Vega cards keep AMDev.Version byte for byte
    self.boot_version = AMDev.VEGA20_BOOT_VERSION_PLACEHOLDER_NOT_RULED_BY_LEON if self.is_vega20 else AMDev.Version
    if self.is_vega20: self._vega20_vbios()   # B4: required inputs, POST gate, ROM dump -> gc_info / reserved_vram_size / powerplay limits

    # AM boot Process:
    # The GPU being passed can be in one of several states: 1. Not initialized. 2. Initialized by amdgpu. 3. Initialized by AM.
    # The 1st and 2nd states require a full GPU setup since their states are unknown. The 2nd state also requires a mode1 reset to
    # reinitialize all components.
    #
    # The 3rd state can be set up partially to optimize boot time. In this case, only the GFX and SDMA IPs need to be initialized.
    # To enable this, AM uses a separate boot memory that is guaranteed not to be overwritten. This physical memory is utilized for
    # all blocks that are initialized only during the initial AM boot.
    # To determine if the GPU is in the third state, AM uses regSCRATCH_REG7 as a flag.
    # To determine if the previous AM session finalized correctly, AM uses regSCRATCH_REG6 as a flag.
    self.is_booting = True # During boot only boot memory can be allocated. This flag is to validate this.
    self.init_sw(smi_dev=False)

    self.partial_boot = (self.reg("regSCRATCH_REG7").read() == self.boot_version) and (getenv("AM_RESET", 0) != 1)
    if self.partial_boot and (self.reg("regSCRATCH_REG6").read() != 0 or self.reg(self.gmc.pf_status_reg("GC")).read() != 0):
      if DEBUG >= 2: print(f"am {self.devfmt}: Malformed state. Issuing a full reset.")
      self.partial_boot = False

    # aqua (gc 9.5.0): full boot over live state can kill the fabric (power cycle recovers); partial boot+reset_mec is the deepest safe reset
    if self.ip_ver[am.GC_HWIP] == (9,5,0) and self.reg("regSCRATCH_REG7").read() == self.boot_version: self.partial_boot = True

    # Init hw for IP blocks where it is needed
    if not self.partial_boot:
      if self.psp.is_sos_alive() and self.smu.is_smu_alive():
        self.pci_dev.write_config_flush(pci.PCI_COMMAND, self.pci_dev.read_config(pci.PCI_COMMAND, 2) & ~pci.PCI_COMMAND_MASTER, 2)
        if self.is_hive():
          if reset_mode: return # in reset mode, do not raise
          raise RuntimeError("Malformed state. Use extra/amdpci/hive_reset.py to reset the hive")
        # mi50 fork (design v2 B7): MP0 11.0.2 has no PPSMC Mode1Reset (vega20_ppsmc.h:37-124); the reset is the PSP mailbox one, quiesced first
        if self.ip_ver[am.MP0_HWIP] == (11,0,2): self._vega20_reset()
        else: self.smu.mode1_reset()
      self.pci_dev.write_config_flush(pci.PCI_COMMAND, self.pci_dev.read_config(pci.PCI_COMMAND, 2) | pci.PCI_COMMAND_MASTER, 2)
      self.init_hw(self.soc, self.gmc, self.ih, self.psp, self.smu)

    # Booting done
    self.is_booting = False

    # mi50 fork (review 2026-10-13 S4; design v2 §7.0 rule 7, §7.1 T7 -> T8): on GC (9,4,0) the thermal re-check, the cap and the lowest DPM level
    # come BEFORE gfx/sdma are brought up - full boot: right after smu.init_hw; partial boot: before the gfx/sdma re-init - so the post-reset
    # "firmware defaults" window never contains an engine init. Design §3 row "amdev.py:203-206 clock policy" keeps the stock (after-gfx)
    # position and contradicts §7.1's T7 -> T8 order; the safety rule is followed here, the row is Leon's to reconcile.
    if self.ip_ver[am.GC_HWIP] == (9,4,0): self._vega20_clock_policy()

    # Re-initialize main blocks
    self.init_hw(self.gfx, self.sdma)

    if self.ip_ver[am.GC_HWIP] == (9,4,0): pass   # done above (before gfx/sdma)
    elif (max_power:=getenv("AM_POWER_LIMIT", 0.0)) > 0:
      self.smu.set_power_limit(max_power)
      self.smu.set_clocks(level=None)
    else: self.smu.set_clocks(level=-1) # last level, max perf.
    # mi50 fork (design v2 B16/§3): AM_GFX.set_clockgating_state writes RLC_CGTT_MGCG_OVERRIDE fields absent from gc_9_0 -> skipped on (9,4,0)
    for ip in [self.soc] + ([] if self.ip_ver[am.GC_HWIP] == (9,4,0) else [self.gfx]): ip.set_clockgating_state()
    self.reg("regSCRATCH_REG7").write(self.boot_version)
    self.reg("regSCRATCH_REG6").write(1) # set initialized state.
    if DEBUG >= 2: print(f"am {self.devfmt}: boot done")

  def _vega20_clock_policy(self):
    """Design v2 B16 (critic gaps 14/15/30): level=-1 "max perf" is refused for the MI50; the card always gets the ruled cap and the lowest DPM
    level, re-sent on every boot including partial ones (§8 U-S2). AM_MI50_PPT_W is the only cap mechanism for this card.
    Review 2026-10-13 L1/TI3: the cap is sent only while FEATURE_PPT is enabled, exactly vega20_set_power_limit (vega20_powertune.c:37-42: Linux
    silently returns 0 otherwise). The fork never sends EnableAllSmuFeatures at boot (ip.py _vega20_init_hw (4), §8 U-S1), so the feature state
    is whatever the VBIOS/SMC boot left - unmeasured until T3/T7; a boot must not raise on it. The state is PRINTED unconditionally so the boot
    log records "uncapped"; AM_SMU.set_power_limit keeps its raise for explicit callers (raise vs. Linux's silent return there is Leon's ruling).
    One GetEnabledSmuFeaturesLow/High pair (get_features) feeds the cap gate and the clock gate (no duplicate messages)."""
    if (p:=getenv("AM_POWER_LIMIT", 0.0)) > 0 and p != self.mi50_ppt_w:
      raise RuntimeError(f"MI50: AM_POWER_LIMIT={p} conflicts with AM_MI50_PPT_W={self.mi50_ppt_w}; the MI50 cap is AM_MI50_PPT_W only")
    self.vega20_check_thermal()
    feats = self.smu.get_features()
    if feats & self.smu.smu_mod.FEATURE_PPT_MASK: self.smu._vega20_set_power_limit(self.mi50_ppt_w, features=feats)
    else: print(f"am {self.devfmt}: MI50 cap NOT applied - FEATURE_PPT (bit {self.smu.smu_mod.FEATURE_PPT_BIT}) off, GetEnabledSmuFeatures={feats:#x}; "
                "vega20_set_power_limit sends nothing then (vega20_powertune.c:37-42): card UNCAPPED (design v2 §8 U-S1)")
    # review S14: _vega20_set_clocks returns {} when no DPM feature is enabled (vega20_hwmgr.c:1827/:1928 skip) - record it, the rungs read levels
    if not self.smu._vega20_set_clocks(0, features=feats) and DEBUG >= 1:
      print(f"am {self.devfmt}: MI50: no DPM feature enabled (GetEnabledSmuFeatures={feats:#x}) - clocks not pinned, PPT cap only (vega20_hwmgr.c:1827/1928)")

  def init_sw(self, smi_dev=False):
    self.smi_dev, self.is_err_state = smi_dev, False

    # Memory manager & firmware
    self.mm = AMMemoryManager(self, self.vram_size - self.reserved_vram_size, boot_size=(32 << 20), pt_t=AMPageTableEntry, va_shifts=[12, 21, 30, 39],
      va_bits=48, first_lv=am.AMDGPU_VM_PDB2, va_base=AMMemoryManager.va_allocator.base, reserve_ptable=not self.large_bar,
      palloc_ranges=[(1 << (i + 12), (2 << 20) if i >= 9 else 0x1000) for i in range(9 * (3 - am.AMDGPU_VM_PDB2), -1, -1)])
    self.fw = AMFirmware(self)

    # Initialize IP blocks
    self.soc:AM_SOC = AM_SOC(self)
    self.gmc:AM_GMC = AM_GMC(self)
    self.ih:AM_IH = AM_IH(self)
    self.psp:AM_PSP = AM_PSP(self)
    self.smu:AM_SMU = AM_SMU(self)
    self.gfx:AM_GFX = AM_GFX(self)
    self.sdma:AM_SDMA = AM_SDMA(self)

    # Init sw for all IP blocks
    for ip in [self.soc, self.gmc, self.ih, self.psp, self.smu, self.gfx, self.sdma]: ip.init_sw()

  def init_hw(self, *blocks:AM_IP):
    for ip in blocks:
      ip.init_hw()
      if DEBUG >= 2: print(f"am {self.devfmt}: {ip.__class__.__name__} initialized")

  def fini(self):
    if DEBUG >= 2: print(f"am {self.devfmt}: Finalizing")
    for ip in [self.sdma, self.gfx]: ip.fini_hw()
    self.smu.set_clocks(level=0)
    self.ih.interrupt_handler()
    self.reg("regSCRATCH_REG6").write(self.is_err_state) # set finalized state.

  def recover(self, force=False) -> bool:
    if not force and not self.is_err_state: return False
    if DEBUG >= 3: print(f"am {self.devfmt}: Start recovery")
    self.ih.interrupt_handler()
    self.gfx.reset_mec()
    self.is_err_state = False
    if DEBUG >= 3: print(f"am {self.devfmt}: Recovery complete")
    return True

  # a hive has multiple XGMI regions; single-node parts (like MI350P) may still program LFB_SIZE with region 0 only
  def is_hive(self) -> bool: return self.gmc.xgmi_seg_sz > 0 and self.gmc.xgmi_max_region > 0

  def paddr2mc(self, paddr:int) -> int: return self.gmc.mc_base + paddr
  def paddr2xgmi(self, paddr:int) -> int: return self.gmc.paddr_base + paddr
  def xgmi2paddr(self, xgmi_paddr:int) -> int: return xgmi_paddr - self.gmc.paddr_base

  def reg(self, reg:str) -> AMRegister: return getattr(self, reg)
  def __getattr__(self, name:str):
    # mi50 fork: Vega-era asic_reg headers (gc_9_0, mmhub_1_0, nbio_7_4, hdp_4_0, osssys_4_0, sdma0_4_2, mp_11_0, thm_11_0_2) name
    # registers mm*, the driver code says reg*; same fallback AMDIP.__getattr__ has in support/amd.py. Only reached when normal
    # attribute lookup fails, so gfx11/gfx12/MI300 (reg* names) are untouched.
    if name.startswith('reg') and (alt:='mm' + name[3:]) in self.__dict__: return self.__dict__[alt]
    raise AttributeError(f"{type(self).__name__} has no attribute {name}")

  def rreg(self, reg:int) -> int:
    val = self.indirect_rreg(reg) if reg >= len(self.mmio) else self.mmio[reg]
    if AM_DEBUG >= 4 and getattr(self, '_prev_rreg', None) != (reg, val): print(f"am {self.devfmt}: Reading register {reg:#x} with value {val:#x}")
    self._prev_rreg = (reg, val)
    return val

  def wreg(self, reg:int, val:int):
    if AM_DEBUG >= 4: print(f"am {self.devfmt}: Writing register {reg:#x} with value {val:#x}")
    if reg >= len(self.mmio): self.indirect_wreg(reg, val)
    else: self.mmio[reg] = val

  def wreg_pair(self, reg_base:str, lo_suffix:str, hi_suffix:str, val:int, inst:int=0):
    self.reg(f"{reg_base}{lo_suffix}").write(lo32(val), inst=inst)
    self.reg(f"{reg_base}{hi_suffix}").write(hi32(val), inst=inst)

  def indirect_rreg(self, reg:int) -> int:
    if self.ip_ver[am.NBIO_HWIP] == (7,4,0):
      # mi50 fork (review 2026-10-13 TI8): nbio_7_4 has no RSMU_INDEX/DATA (grep nbio_7_4_offset.h: none). Linux amdgpu_device_rreg routes a register
      # past rmmio_size to amdgpu_reg_pcie_rd32 (amdgpu_reg_access.c:371-381) = amdgpu_device_indirect_rreg (:590-610): PCIE_INDEX2 = reg*4, read
      # it back, read PCIE_DATA2 (nbio_v7_4.c:316-324 offsets; aliases mmBIF_BX0_PCIE_INDEX2/DATA2 in the nbio_7_4 table, patches README item 6).
      self.regBIF_BX0_PCIE_INDEX2.write(reg * 4)
      self.regBIF_BX0_PCIE_INDEX2.read()
      return self.regBIF_BX0_PCIE_DATA2.read()
    self.reg("regBIF_BX_PF0_RSMU_INDEX").write(reg * 4)
    return self.reg("regBIF_BX_PF0_RSMU_DATA").read()

  def indirect_wreg(self, reg:int, val:int):
    if self.ip_ver[am.NBIO_HWIP] == (7,4,0):
      # amdgpu_device_wreg (amdgpu_reg_access.c:500-511) -> amdgpu_device_indirect_wreg (:758-777): write index, read it back, write data, read it back
      self.regBIF_BX0_PCIE_INDEX2.write(reg * 4)
      self.regBIF_BX0_PCIE_INDEX2.read()
      self.regBIF_BX0_PCIE_DATA2.write(val)
      self.regBIF_BX0_PCIE_DATA2.read()
      return
    self.reg("regBIF_BX_PF0_RSMU_INDEX").write(reg * 4)
    self.reg("regBIF_BX_PF0_RSMU_DATA").write(val)

  def indirect_wreg_pcie(self, reg:int, val:int, aid:int=0):
    reg_addr = reg * 4 + ((((aid & 0b11) << 32) | (1 << 34)) if aid > 0 else 0)
    self.reg("regBIF_BX0_PCIE_INDEX2").write(lo32(reg_addr))
    if hi32(reg_addr) > 0: self.reg("regBIF_BX0_PCIE_INDEX2_HI").write(hi32(reg_addr) & 0xff)
    self.reg("regBIF_BX0_PCIE_DATA2").write(val)
    if hi32(reg_addr) > 0: self.reg("regBIF_BX0_PCIE_INDEX2_HI").write(0)

  def _read_vram(self, addr, size) -> bytes:
    assert addr % 4 == 0 and size % 4 == 0, f"Invalid address {addr:#x} or size {size:#x}"
    res = []
    for caddr in range(addr, addr + size, 4):
      self.wreg(0x06, caddr >> 31)
      self.wreg(0x00, (caddr & 0x7FFFFFFF) | 0x80000000)
      res.append(self.rreg(0x01))
    return bytes(array.array('I', res))

  def _run_discovery(self):
    # NOTE: Fixed register to query memory size without known ip bases to find the discovery table.
    #       The table is located at the end of VRAM - 64KB and is 10KB in size.
    mmRCC_CONFIG_MEMSIZE = 0xde3
    self.vram_size = self.rreg(mmRCC_CONFIG_MEMSIZE) << 20
    self.large_bar = self.vram.nbytes >= self.vram_size
    # mi50 fork (design v2 B3): Vega 20 needs no discovery binary (L/amdgpu/amdgpu_discovery.c:3196-3200 "not fatal ... only for sysfs");
    # is_vega20 = config dword 0 == VEGA20_PCI_ID, read once in __init__ (review TI4). Hand-built AMDevs (ladder boot_sw_only, offline checks) set it.
    if self.is_vega20: return self._static_vega20()
    tmr_offset, tmr_size = self.vram_size - (64 << 10), (10 << 10)

    disc_tbl = self.vram.view(tmr_offset, tmr_size)[:] if self.large_bar else self._read_vram(tmr_offset, tmr_size)
    self.bhdr = am.struct_binary_header.from_buffer(bytearray(disc_tbl))
    ihdr = am.struct_ip_discovery_header.from_address(ctypes.addressof(self.bhdr) + self.bhdr.table_list[am.IP_DISCOVERY].offset)
    assert self.bhdr.binary_signature == am.BINARY_SIGNATURE and ihdr.signature == am.DISCOVERY_TABLE_SIGNATURE, "discovery signatures mismatch"

    self.regs_offset:dict[int, dict[int, tuple]] = collections.defaultdict(dict)
    self.ip_ver:dict[int, tuple[int, int, int]] = {}

    for num_die in range(ihdr.num_dies):
      dhdr = am.struct_die_header.from_address(ctypes.addressof(self.bhdr) + ihdr.die_info[num_die].die_offset)

      ip_offset = ctypes.addressof(self.bhdr) + ctypes.sizeof(dhdr) + ihdr.die_info[num_die].die_offset
      for _ in range(dhdr.num_ips):
        ip = am.struct_ip_v4.from_address(ip_offset)
        ba = ((ctypes.c_uint64 if ihdr.base_addr_64_bit else ctypes.c_uint32) * ip.num_base_address).from_address(ip_offset + 8)
        for hw_ip in range(1, am.MAX_HWIP):
          if hw_ip in am.hw_id_map and am.hw_id_map[hw_ip] == ip.hw_id:
            self.regs_offset[hw_ip][ip.instance_number] = tuple(list(ba))
            self.ip_ver[hw_ip] = (ip.major, ip.minor, ip.revision)

        ip_offset += 8 + (8 if ihdr.base_addr_64_bit else 4) * ip.num_base_address

    # HARV(EST) table: harvested instances must be excluded (like amdgpu_discovery_harvest_ip)
    # layout: u32 signature, u16 version, u16 size, then 32 entries of {hw_id:u16, inst:u8, rsv:u8}
    self.harvested:dict[int, set[int]] = collections.defaultdict(set)
    if (harv_off:=self.bhdr.table_list[am.HARVEST_INFO].offset) != 0 and \
       (blob:=to_mv(ctypes.addressof(self.bhdr) + harv_off, 8 + 32*4).cast('I'))[0] == am.HARVEST_TABLE_SIGNATURE:
      inv_hw_id = {hw_id: hw_ip for hw_ip, hw_id in am.hw_id_map.items()}
      for ent in blob[2:]:
        if (ip_:=inv_hw_id.get(ent & 0xffff)) is not None: self.harvested[ip_].add((ent >> 16) & 0xff)

    gc_info = am.struct_gc_info_v1_0.from_address(gc_addr:=ctypes.addressof(self.bhdr) + self.bhdr.table_list[am.GC].offset)
    self.gc_info = getattr(am, f"struct_gc_info_v{gc_info.header.version_major}_{gc_info.header.version_minor}").from_address(gc_addr)
    self.reserved_vram_size = (384 << 20) if self.ip_ver[am.GC_HWIP][:2] in {(9,4), (9,5)} else (64 << 20)

  # ---- mi50 fork: Vega 20 (MI50) branches. Every gate is an exact ip version / device id (design v2 rule R-LINUX). ----
  def _static_vega20(self):
    """Design v2 B3: the CHIP_VEGA20 tables Linux hard-codes instead of a discovery walk. ip_ver = L/amdgpu/amdgpu_discovery.c:3207-3224 (17
    entries, SDMA1/UVD/VCE/DCI included) + NBIF (7,4,0) because ops_amd builds AMDIP('nbio', ip_versions[NBIF_HWIP]) (T/runtime/ops_amd.py:1027-1028).
    regs_offset = the 20 blocks of vega20_reg_base_init (L/amdgpu/vega20_reg_init.c:34-53, NBIF = NBIO bases :49) from L/include/vega20_ip_offset.h as
    shipped in autogen/am/vega_offsets.py (INST0 only: every INST1 base in that header is 0 except UVD seg1, kept as instance 1 like Linux).
    SDMA: regs_offset[SDMA0_HWIP] = {0: SDMA0 bases, 1: SDMA1 bases} because _build_regs iterates its keys and AMRegister.addr[inst] indexes bases by
    instance (amdev.py AMRegister / support/amd.py:8) - what Linux sdma_v4_0_get_reg_offset does with the sdma0 header for both engines."""
    from tinygrad.runtime.autogen.am import vega_offsets as vo
    self.is_vega20 = True
    self.ip_ver = {am.MMHUB_HWIP: (9,4,0), am.ATHUB_HWIP: (9,4,0), am.OSSSYS_HWIP: (4,2,0), am.HDP_HWIP: (4,2,0), am.SDMA0_HWIP: (4,2,0),
      am.SDMA1_HWIP: (4,2,0), am.DF_HWIP: (3,6,0), am.NBIO_HWIP: (7,4,0), am.UMC_HWIP: (6,1,1), am.MP0_HWIP: (11,0,2), am.MP1_HWIP: (11,0,2),
      am.THM_HWIP: (11,0,2), am.SMUIO_HWIP: (11,0,2), am.GC_HWIP: (9,4,0), am.UVD_HWIP: (7,2,0), am.VCE_HWIP: (4,1,0), am.DCI_HWIP: (12,1,0),
      am.NBIF_HWIP: (7,4,0)}
    def bases(blk:str, inst:int=0) -> tuple[int, ...]: return tuple(getattr(vo, f"{blk}_BASE__INST{inst}_SEG{s}") for s in range(6))
    self.regs_offset = collections.defaultdict(dict, {hwip: {0: bases(blk)} for hwip, blk in [(am.GC_HWIP, "GC"), (am.HDP_HWIP, "HDP"),
      (am.MMHUB_HWIP, "MMHUB"), (am.ATHUB_HWIP, "ATHUB"), (am.NBIO_HWIP, "NBIO"), (am.MP0_HWIP, "MP0"), (am.MP1_HWIP, "MP1"), (am.VCE_HWIP, "VCE"),
      (am.DF_HWIP, "DF"), (am.DCE_HWIP, "DCE"), (am.OSSSYS_HWIP, "OSSSYS"), (am.SDMA1_HWIP, "SDMA1"), (am.SMUIO_HWIP, "SMUIO"),
      (am.NBIF_HWIP, "NBIO"), (am.THM_HWIP, "THM"), (am.CLK_HWIP, "CLK"), (am.UMC_HWIP, "UMC"), (am.RSMU_HWIP, "RSMU")]})
    self.regs_offset[am.UVD_HWIP] = {0: bases("UVD", 0), 1: bases("UVD", 1)}
    self.regs_offset[am.SDMA0_HWIP] = {0: bases("SDMA0"), 1: bases("SDMA1")}
    self.harvested = collections.defaultdict(set)   # what discovery builds (above); a plain {} raises KeyError in _build_regs and ip.py:253
    self.aids = [0]   # _build_regs recomputes the same from the SDMA instance keys {0,1} (max_aid 0); set explicitly per design B3
    # gc_info and reserved_vram_size come from the VBIOS in _vega20_vbios() (needs the register tables, i.e. after _build_regs)

  def _vega20_vbios(self):
    """Design v2 B4: POST gate, ROM dump, data tables (the required inputs are read in __init__, before any config write). Runs right after
    _build_regs, before init_sw (no allocations needed)."""
    self.vega20_check_thermal()   # first thing read: the card's current hotspot vs the ruled threshold
    if not self.vega20_is_posted():
      raise RuntimeError("MI50: card not POSTed (BIOS_SCRATCH_7 bit 9 clear, amdgpu_atombios.c:1634-1642). Linux would run the ATOM interpreter "
                         "(amdgpu_device.c:4068-4080); the fork does not port it (design v2 B4, §8 U-V1) - nothing boots before")
    self.vega20_posted = True
    self.vbios = vbios.VBIOS(vbios.read_rom(self.rreg, self.wreg, self.regROM_INDEX.addr[0], self.regROM_DATA.addr[0]))
    fwi = self.vbios.firmwareinfo()
    if fwi.bios_scratch_reg_startaddr != self.regBIOS_SCRATCH_0.addr[0]:   # amdgpu_atomfirmware.c:100-101 vs nbio_7_4_offset.h:2480-2481 (dword 0x4c)
      raise RuntimeError(f"MI50: VBIOS bios_scratch_reg_startaddr {fwi.bios_scratch_reg_startaddr:#x} != nbio_7_4 mmBIOS_SCRATCH_0 "
                         f"{self.regBIOS_SCRATCH_0.addr[0]:#x}")
    self.gc_info = self.vbios.gc_info_v2_0()
    self.powerplay = self.vbios.powerplay_limits()   # §6 limits + the pptable blob; Linux vega20 hwmgr refuses to init without a valid table
    # reserved_vram_size (design B3/B4, §8 U-M1 "the fork asserts no overlap and moves boot_size/reserved_vram_size accordingly"): AM reserves the
    # top of VRAM (mm size = vram_size - reserved_vram_size, init_sw); the top reservation covering the VBIOS firmware region is vram_size - start.
    # Review 2026-10-13 L4 (citation corrected): Linux acts on vram_usagebyfirmware only under SR-IOV - v2_1 amdgpu_atomfirmware.c:119-125 is the
    # ATOM_VRAM_BLOCK_SRIOV_MSG_SHARE_RESERVATION branch, v2_2 :151-157 is amdgpu_sriov_vf; bare metal reads only drv_size (:129). Its bare-metal
    # top reservation, amdgpu_ttm_init_fw_resv_region (amdgpu_ttm.c:1741-1772), returns at :1745 because reserve_tmr is set only when the discovery
    # binary is read from VRAM (amdgpu_discovery.c:407-410) and CHIP_VEGA20 reads it from the file (:570-571, :721-727): Linux reserves NOTHING at
    # the top for Vega 20. The reservation + overlap refusal below are the design's U-M1 step (kept; dropping it is Leon's ruling).
    if (region:=self.vbios.fw_vram_region()) is not None:
      start, size = region
      if start < (32 << 20) or start + size > self.vram_size:
        raise RuntimeError(f"MI50: VBIOS firmware VRAM region [{start:#x}, {start+size:#x}) overlaps AM's boot region [0, 32 MiB) or exceeds VRAM "
                           f"{self.vram_size:#x}: moving boot_size is Leon's decision (design v2 §8 U-M1)")
      self.reserved_vram_size = self.vram_size - start
    else: self.reserved_vram_size = _required_env("AM_MI50_RESERVED_VRAM_MB", "top-of-VRAM reservation in MiB (no vram_usagebyfirmware region)",
                                                  0, self.vram_size >> 20) << 20   # no table / zero size: explicit value required
    if DEBUG >= 2: print(f"am {self.devfmt}: MI50 VBIOS ok, gc_info se={self.gc_info.gc_num_se} cu/sh={self.gc_info.gc_num_cu_per_sh} "
                         f"reserved_vram={self.reserved_vram_size >> 20} MiB bootup sclk={fwi.bootup_sclk_in10khz / 100} MHz")

  def vega20_is_posted(self) -> bool:
    """POST state = amdgpu_atombios_scratch_need_asic_init (L/amdgpu/amdgpu_atombios.c:1634-1642): RREG32(bios_scratch_reg_offset + 7) &
    ATOM_S7_ASIC_INIT_COMPLETE_MASK (0x200, L/include/atombios.h:7075); scratch 7 = mmBIOS_SCRATCH_7 0x3f BASE_IDX 1 (nbio_7_4_offset.h:2494-2495) ->
    dword 0x53. Rule (design v2 B4): bit clear -> the boot stops (no ATOM interpreter in the fork); re-read after every reset (B7), because Linux
    re-POSTs after a full reset (amdgpu_device.c:5142-5146)."""
    return bool(self.regBIOS_SCRATCH_7.read() & vbios.ATOM_S7_ASIC_INIT_COMPLETE_MASK)

  def vega20_hotspot_c(self) -> int:
    """Hotspot °C with no SMU: THM CG_MULT_THERMAL_STATUS.CTF_TEMP bits 9..17 (vega20_thermal_get_temperature,
    L/pm/powerplay/hwmgr/vega20_thermal.c:218-232; thm_11_0_2_offset.h:26-27 0x5f BASE_IDX 0 -> dword 0x1665f; mask thm_11_0_2_sh_mask.h:28,30).
    0 and 511 are invalid readings (design v2 §7.0 rule 4)."""
    if (t:=self.regCG_MULT_THERMAL_STATUS.read_bitfields()['ctf_temp']) in (0, 0x1ff):
      raise RuntimeError(f"MI50: hotspot reading {t} is invalid (9-bit CTF_TEMP at 0 or 511 is not a temperature, design v2 §7.0 rule 4)")
    return t

  def vega20_check_thermal(self):
    if (t:=self.vega20_hotspot_c()) >= self.mi50_temp_c: raise RuntimeError(f"MI50: hotspot {t} °C >= threshold AM_MI50_TEMP_C={self.mi50_temp_c} °C")

  def _vega20_halt_mec(self):
    # gfx_v9_0_cp_compute_enable(false): raw write of the full disable mask (L/amdgpu/gfx_v9_0.c:3467-3476) then udelay(50) (:3479)
    self.regCP_MEC_CNTL.write(mec_invalidate_icache=1, mec_me1_pipe0_reset=1, mec_me1_pipe1_reset=1, mec_me1_pipe2_reset=1, mec_me1_pipe3_reset=1,
                              mec_me2_pipe0_reset=1, mec_me2_pipe1_reset=1, mec_me1_halt=1, mec_me2_halt=1)
    time.sleep(50e-6)

  def _vega20_halt_sdma(self):
    # sdma_v4_0_enable(false): read-modify-write F32_CNTL.HALT = 1 per instance (L/amdgpu/sdma_v4_0.c:1055-1059)
    for inst in self.regs_offset[am.SDMA0_HWIP]: self.regSDMA_F32_CNTL.update(halt=1, inst=inst)

  def _vega20_reset(self):
    """Design v2 B7: quiesce (tinygrad-master amdev.py:198-204 back-port: HQDs off, clocks down, engines halted, 100 ms), PSP mode1 reset
    (AM_PSP.mode1_reset = psp_v11_0_mode1_reset, L/amdgpu/psp_v11_0.c:410-432), then the post-reset gates: config space first (master ip.py:220-223:
    config reads fail fast on a wedged GPU, an MMIO read would hang - and here stall the whole TinyGPU client, design §1.3), RCC_CONFIG_MEMSIZE !=
    0xffffffff within usec_timeout (L/amdgpu/amdgpu_device.c:4978-4985; AMDGPU_MAX_USEC_TIMEOUT 100 ms, amdgpu.h:274), then the POST bit again
    (B4).
    Review 2026-10-13 L3: amdgpu_device_mode1_reset caches the PCI config space before the reset (amdgpu_device.c:4947-4950 ->
    amdgpu_device_cache_pci_state :6475-6484 pci_save_state) and restores it after (:4972 amdgpu_device_load_pci_state -> pci_restore_state), before
    the bootloader wait (:4973) and the memsize poll (:4978-4985). drivers/pci is not in the Linux tree checked out here, so the exact dword set and
    write order of pci_save/restore_state are UNVERIFIED (listed): the type-0 header dwords named by the review are snapshotted and only the ones
    the reset changed are written back, descending offsets. :4942-4943/:4991-4992 amdgpu_atombios_scratch_regs_engine_hung (BIOS_SCRATCH_3 bit
    ATOM_S3_ASIC_GUI_ENGINE_HUNG, amdgpu_atombios.c:1609-1618) is a flag for the ATOM interpreter the fork does not port (§8 U-V1): skipped, listed."""
    self.gfx.fini_hw()
    self.smu.set_clocks(level=0)
    self._vega20_halt_mec()
    self._vega20_halt_sdma()
    time.sleep(0.1)
    cfg_offs = [0x04, 0x0c, *range(0x10, 0x28, 4), 0x30, 0x3c]   # command/status, cacheline/latency/header/BIST, BAR0..5, expansion ROM, int line/pin
    cfg_saved = {off: self.pci_dev.read_config(off, 4) for off in cfg_offs}
    self.psp.mode1_reset()
    wait_cond(self.pci_dev.read_config, 0, 2, value=0x1002, timeout_ms=2000,
              msg=f"am {self.devfmt}: MI50 did not return from PSP mode1 reset (config space dead)")
    for off in reversed(cfg_offs):
      if (now:=self.pci_dev.read_config(off, 4)) != cfg_saved[off]:
        if DEBUG >= 1: print(f"am {self.devfmt}: MI50 config dword {off:#04x} changed by the reset {now:#010x} -> restoring {cfg_saved[off]:#010x}")
        self.pci_dev.write_config(off, cfg_saved[off], 4)
    wait_cond(lambda: self.regRCC_CONFIG_MEMSIZE.read() != 0xffffffff, value=True, timeout_ms=100,
              msg=f"am {self.devfmt}: MI50 RCC_CONFIG_MEMSIZE stuck at 0xffffffff after mode1 reset")
    if not self.vega20_is_posted():
      raise RuntimeError("MI50: POST bit clear after PSP mode1 reset; Linux re-POSTs here (amdgpu_device.c:5142-5146), the fork cannot "
                         "(design v2 §8 U-V1)")

  def _emergency_quiesce(self):
    """Design v2 §3 (critic gap 27) / §7.0 rule 6: runs when __init__ raises (and from the ladder's abort path), Vega 20 only. Nothing here raises.
    Review 2026-10-13 S7: the engine halts are gated on the register tables existing and a LIVE POST read (one MMIO read, vega20_is_posted), not on
    the vega20_posted flag - a card left hot by a crashed session (HQDs active) or refused at the thermal check must still get its engines halted;
    Linux never touches GC before POST (amdgpu_device.c:4068-4080), hence the live read.
    Review S2/S3 (§7.0 rules 1 and 5): when the mailbox answers (AM_SMU.is_smu_alive logic; smu.init_sw may not have run), the ruled cap goes
    FIRST as one message (the only lever rule 1 names for a passive card), then Linux's force-lowest = _vega20_set_clocks(0) (vega20_force_dpm_lowest
    vega20_hwmgr.c:2417-2429: SetSoftMinByFreq THEN SetSoftMaxByFreq per DPM clock whose feature is enabled, :1827/:1928). The first mailbox
    TimeoutError ends the quiesce (rule 5: no further messages); a ResponseError on the cap still lets the clocks run (own try each)."""
    if not getattr(self, 'is_vega20', False): return
    if DEBUG >= 1: print(f"am {getattr(self, 'devfmt', '?')}: MI50 emergency quiesce")
    if 'mmCP_MEC_CNTL' in self.__dict__ and 'mmSDMA_F32_CNTL' in self.__dict__ and 'mmBIOS_SCRATCH_7' in self.__dict__:
      try: posted = self.vega20_is_posted()
      except Exception as e:
        posted = False
        if DEBUG >= 1: print(f"am {self.devfmt}: quiesce POST read failed, engines not touched: {e!r}")
      if posted:
        for step in (self._vega20_halt_mec, self._vega20_halt_sdma):
          try: step()
          except Exception as e:
            if DEBUG >= 1: print(f"am {self.devfmt}: quiesce {step.__name__} failed: {e!r}")
    try:
      smu_mod, smu = import_module("smu", self.ip_ver[am.MP1_HWIP]), getattr(self, 'smu', None) or AM_SMU(self)
      if not hasattr(smu, 'smu_mod'):  # integration 2026-10-13: AM_SMU.init_sw never ran; ip.py AM_SMU._vega20_send_msg needs both fields
        smu.smu_mod, smu._vega20_results = smu_mod, {v: k for k, v in vars(smu_mod).items() if k.startswith("PPSMC_Result_")}
      if not smu.is_smu_alive(): return  # GetSmuVersion (100 ms) then C2PMSG_90 != 0; ip.py is_smu_alive also suppresses the Vega ResponseError
      try:
        if (cap:=getattr(self, 'mi50_ppt_w', None)) is not None: smu._send_msg(smu_mod.PPSMC_MSG_SetPptLimit, max(int(round(cap)), 1))  # 0xF, cap first
      except TimeoutError as e:
        if DEBUG >= 1: print(f"am {self.devfmt}: quiesce SetPptLimit timed out - no further messages (§7.0 rule 5): {e!r}")
        return
      except Exception as e:
        if DEBUG >= 1: print(f"am {self.devfmt}: quiesce SetPptLimit failed: {e!r}")
      try: smu._vega20_set_clocks(0)   # min then max per enabled DPM clock; stops at the first failure like vega20_force_dpm_lowest's PP_ASSERT returns
      except Exception as e:
        if DEBUG >= 1: print(f"am {self.devfmt}: quiesce force-lowest clocks ended: {e!r}")
    except Exception as e:
      if DEBUG >= 1: print(f"am {getattr(self, 'devfmt', '?')}: quiesce SMU step failed: {e!r}")

  @functools.cached_property
  def hwid_names(self) -> dict[int, str]: return {v:k.removesuffix('_HWID') for k,v in vars(am).items() if k.endswith('_HWID') and isinstance(v, int)}

  def _ip_module(self, prefix:str, hwip): return import_module(prefix, self.ip_ver[hwip])

  def _build_regs(self):
    mods = [("mp", am.MP0_HWIP), ("hdp", am.HDP_HWIP), ("gc", am.GC_HWIP), ("mmhub", am.MMHUB_HWIP), ("osssys", am.OSSSYS_HWIP),
      ("nbio" if self.ip_ver[am.GC_HWIP] < (12,0,0) else "nbif", am.NBIO_HWIP)]
    if self.ip_ver[am.SDMA0_HWIP] in {(4,2,0), (4,4,2), (4,4,4)}: mods += [("sdma", am.SDMA0_HWIP)]   # mi50 fork: + Vega 20 sdma_4_2 (design v2 §3)
    # mi50 fork (design v2 §3 "_build_regs" row, critic gap 13): THM (hotspot), SMUIO (ROM_INDEX/DATA), ATHUB (RPB_ARB_CNTL golden) tables, 9.4.0 only
    if self.ip_ver[am.GC_HWIP] == (9,4,0): mods += [("thm", am.THM_HWIP), ("smuio", am.SMUIO_HWIP), ("athub", am.ATHUB_HWIP)]

    for prefix, hwip in mods:
      self.__dict__.update(import_asic_regs(prefix, self.ip_ver[hwip], cls=functools.partial(AMRegister, adev=self, bases=self.regs_offset[hwip])))
    self.__dict__.update(import_asic_regs('mp', (11, 0, 0), cls=functools.partial(AMRegister, adev=self, bases=self.regs_offset[am.MP1_HWIP])))

    # Live AIDs like the kernel: 4 SDMAs per AID; the AID lives iff its group's alive-mask is 0xf/0x3/0xc.
    # Dead AIDs must never be touched via the indirect window: writes poison the whole fabric.
    live_sdma = {k for k in self.regs_offset[am.SDMA0_HWIP] if k not in self.harvested[am.SDMA0_HWIP]}
    max_aid = max((k >> 2 for k in self.regs_offset[am.SDMA0_HWIP]), default=0)
    self.aids = [0] + [aid for aid in range(1, max_aid + 1) if sum(1 << (i & 3) for i in live_sdma if i >> 2 == aid) in {0xf, 0x3, 0xc}]
