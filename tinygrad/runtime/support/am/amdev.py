from __future__ import annotations
import ctypes, collections, dataclasses, functools, hashlib, array, struct
from tinygrad.helpers import mv_address, getenv, DEBUG, lo32, hi32, fetch_fw, to_mv
from tinygrad.runtime.autogen import pci
from tinygrad.runtime.autogen.am import am, fw
from tinygrad.runtime.support.amd import AMDReg, import_module, import_asic_regs
from tinygrad.runtime.support.memory import TLSFAllocator, MemoryManager, AddrSpace
from tinygrad.runtime.support.system import PCIDevice
from tinygrad.runtime.support.am.ip import AM_IP, AM_SOC, AM_GMC, AM_IH, AM_PSP, AM_SMU, AM_GFX, AM_SDMA
from tinygrad.runtime.support.am import vbios, atom

AM_DEBUG = getenv("AM_DEBUG", 0)
VEGA20_PCI_ID = 0x66a11002 # mi50: PCI 1002:66a1, AMD Instinct MI50 (Vega 20)

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

    # mi50: the TinyGPU driver loads the MI50's firmware; tinygrad only takes the card with a partial boot, which loads none
    if adev.ip_ver[am.MP0_HWIP] == (11,0,2): return

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
    self._disable_aspm()
    self.vram, self.doorbell64, self.mmio = self.pci_dev.map_bar(0), self.pci_dev.map_bar(2, fmt='Q'), self.pci_dev.map_bar(5, fmt='I')

    self._run_discovery()
    self._build_regs()
    # mi50 (review finding 12): the MI50's power cap is the OverDrive table's OverDrivePct, applied by the TinyGPU driver from the overdrive file;
    # AM_POWER_LIMIT's SetPptLimit would be a second power setting beside it
    if getattr(self, "is_vega20", False) and getenv("AM_POWER_LIMIT", 0.0) > 0:
      raise RuntimeError("AM_POWER_LIMIT is not used on the MI50: its power cap is OverDrivePct in the overdrive file, applied by the TinyGPU driver")

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

    self.partial_boot = (self.reg("regSCRATCH_REG7").read() == AMDev.Version) and (getenv("AM_RESET", 0) != 1)
    if self.partial_boot and (self.reg("regSCRATCH_REG6").read() != 0 or self.reg(self.gmc.pf_status_reg("GC")).read() != 0):
      if DEBUG >= 2: print(f"am {self.devfmt}: Malformed state. Issuing a full reset.")
      self.partial_boot = False

    # mi50 (review finding 6, Leon 2026-10-16 "In-place reboot, re-read"): the TinyGPU driver boots the MI50 (the V420 table, the overdrive file as
    # MI50BarFix published it at this open's BAR0 map above). A card that is not in its hand-over state goes to the driver's RESET, which boots it in
    # place; tinygrad then takes it with its partial boot, and never boots it itself
    if not self.partial_boot and getattr(self, "is_vega20", False):
      if DEBUG >= 2: print(f"am {self.devfmt}: MI50 not handed over. The TinyGPU driver reboots it (RESET).")
      self.pci_dev.reset()
      self.partial_boot = self.reg("regSCRATCH_REG7").read() == AMDev.Version and self.reg("regSCRATCH_REG6").read() == 0 and \
                          self.reg(self.gmc.pf_status_reg("GC")).read() == 0
      if not self.partial_boot: raise RuntimeError(f"am {self.devfmt}: the TinyGPU driver's reboot did not hand the MI50 over (its log says why)")

    # aqua (gc 9.5.0): full boot over live state can kill the fabric (power cycle recovers); partial boot+reset_mec is the deepest safe reset
    if self.ip_ver[am.GC_HWIP] == (9,5,0) and self.reg("regSCRATCH_REG7").read() == AMDev.Version: self.partial_boot = True

    # Init hw for IP blocks where it is needed
    if not self.partial_boot:
      if self.psp.is_sos_alive() and self.smu.is_smu_alive():
        self.pci_dev.write_config_flush(pci.PCI_COMMAND, self.pci_dev.read_config(pci.PCI_COMMAND, 2) & ~pci.PCI_COMMAND_MASTER, 2)
        if self.is_hive():
          if reset_mode: return # in reset mode, do not raise
          raise RuntimeError("Malformed state. Use extra/amdpci/hive_reset.py to reset the hive")
        self.smu.mode1_reset()
      self.pci_dev.write_config_flush(pci.PCI_COMMAND, self.pci_dev.read_config(pci.PCI_COMMAND, 2) | pci.PCI_COMMAND_MASTER, 2)
      self.init_hw(self.soc, self.gmc, self.ih, self.psp, self.smu)
    else:
      # mi50: the TinyGPU server clears bus master when a session ends (a killed model's queues must stop DMA before its host memory is
      # released), so a partial boot enables it again as the full boot above does; idempotent when it is still set
      self.pci_dev.write_config_flush(pci.PCI_COMMAND, self.pci_dev.read_config(pci.PCI_COMMAND, 2) | pci.PCI_COMMAND_MASTER, 2)

    # Booting done
    self.is_booting = False

    # Re-initialize main blocks
    self.init_hw(self.gfx, self.sdma)

    if (max_power:=getenv("AM_POWER_LIMIT", 0.0)) > 0:
      self.smu.set_power_limit(max_power)
      self.smu.set_clocks(level=None)
    else: self.smu.set_clocks(level=-1) # last level, max perf.
    for ip in [self.soc, self.gfx]: ip.set_clockgating_state()
    self.reg("regSCRATCH_REG7").write(AMDev.Version)
    self.reg("regSCRATCH_REG6").write(1) # set initialized state.
    if DEBUG >= 2: print(f"am {self.devfmt}: boot done")

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

  def reg(self, reg:str) -> AMRegister: return self.__dict__[reg]

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
    if self.ip_ver[am.NBIO_HWIP] == (7,4,0): # mi50: amdgpu_device_indirect_rreg through PCIE_INDEX2/DATA2
      self.regBIF_BX0_PCIE_INDEX2.write(reg * 4)
      self.regBIF_BX0_PCIE_INDEX2.read()
      return self.regBIF_BX0_PCIE_DATA2.read()
    self.reg("regBIF_BX_PF0_RSMU_INDEX").write(reg * 4)
    return self.reg("regBIF_BX_PF0_RSMU_DATA").read()

  def indirect_wreg(self, reg:int, val:int):
    if self.ip_ver[am.NBIO_HWIP] == (7,4,0): # mi50: amdgpu_device_indirect_wreg through PCIE_INDEX2/DATA2
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
    if self.pci_dev.read_config(0, 4) == VEGA20_PCI_ID: return self._vega20_discovery()
    # NOTE: Fixed register to query memory size without known ip bases to find the discovery table.
    #       The table is located at the end of VRAM - 64KB and is 10KB in size.
    mmRCC_CONFIG_MEMSIZE = 0xde3
    self.vram_size = self.rreg(mmRCC_CONFIG_MEMSIZE) << 20
    self.large_bar = self.vram.nbytes >= self.vram_size
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

  def _vega20_discovery(self):
    # mi50: Vega 20 has no discovery table: AMD's fixed CHIP_VEGA20 tables, then POST before any other card access, then memsize and gc_info
    self._static_vega20()
    self._build_regs()
    self._vega20_post()
    self.vram_size = self.rreg(0xde3) << 20 # mmRCC_CONFIG_MEMSIZE, as below
    if self.vram_size in (0, 0xffffffff << 20): raise RuntimeError("MI50: RCC_CONFIG_MEMSIZE unreadable after POST")   # review R4-05
    self.large_bar = self.vram.nbytes >= self.vram_size
    self._vega20_vbios()
    self.timestamp_mhz = self.vbios.core_refclk_10khz() / 100   # the CP's timestamp clock (soc15_get_xclk): ops_amd's AMDSignal divider

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

  def _vega20_post(self):
    """The VBIOS before anything else (amdgpu_device_init order: amdgpu_get_bios -> amdgpu_atombios_init = amdgpu_atom_parse +
    amdgpu_atomfirmware_scratch_regs_init + amdgpu_atomfirmware_allocate_fb_scratch, amdgpu_atombios.c:1909-1946), then the POST bit. Card
    accesses: the ROM read (ROM_INDEX/ROM_DATA) and the POST bit. The TinyGPU driver POSTs the MI50: an unPOSTed card goes to its RESET."""
    self.vbios = vbios.VBIOS(vbios.read_rom(self.rreg, self.wreg, self.regROM_INDEX.addr[0], self.regROM_DATA.addr[0]))
    # amdgpu_atomfirmware_scratch_regs_init (amdgpu_atomfirmware.c:88-103): bios_scratch_reg_offset = firmware_info.bios_scratch_reg_startaddr
    self.bios_scratch_reg_offset = self.vbios.firmwareinfo().bios_scratch_reg_startaddr
    self.atom = atom.AtomContext(atom.CardInfo(self.rreg, self.wreg), self.vbios.img, self._vega20_atom_scratch_bytes())
    # the card ROM's tables are checked here, before any other card access: gfx_info (read later as gc_info), powerplay (the card's own; the
    # SMU runs the driver's boot table, ip.py _vega20_live_state) and smc_dpm_info (appended to the SMC table, ip.py _vega20_pptable_init)
    self.vbios.gfx_info(), self.vbios.powerplay(), self.vbios.smc_dpm_info()
    if not self.vega20_is_posted():
      self.pci_dev.reset()
      if not self.vega20_is_posted(): raise RuntimeError(f"am {self.devfmt}: the MI50 is not POSTed and the TinyGPU driver's RESET did not POST it (its log says why)")
    self.vega20_posted = True

  def _vega20_vbios(self):
    """After POST: the VBIOS data tables Linux reads (gfx_info -> gc config, powerplayinfo -> the SMC PPTable) and the VRAM reservation (none)."""
    if not getattr(self, 'vega20_posted', False): self._vega20_post()   # hand-built AMDevs (offline checks) call this directly
    fwi = self.vbios.firmwareinfo()
    self.gc_info = self.vbios.gc_info_v2_0()
    # bug 5 (Leon 2026-10-16): the ROM's gfx_info lists the die's 16 CUs per array; 4 are cut in hardware. The CUs per array are the driver's
    # count over the ROM's arrays, so ops_amd's cu_cnt (_compute_props) and everything it sizes reads the MI50's 60
    cus, arrays = self._vega20_driver_cu_info()
    if arrays != self.gc_info.gc_num_se * self.gc_info.gc_num_sh_per_se or not 0 < cus <= arrays * self.gc_info.gc_num_cu_per_sh or cus % arrays:
      raise RuntimeError(f"am {self.devfmt}: the driver's {cus} active CUs in {arrays} arrays do not fit gfx_info's "
                         f"{self.gc_info.gc_num_se}x{self.gc_info.gc_num_sh_per_se} arrays of {self.gc_info.gc_num_cu_per_sh} CUs evenly")
    self.gc_info.gc_num_cu_per_sh = cus // arrays
    # Linux reserves nothing at the top of VRAM for CHIP_VEGA20 on bare metal: amdgpu_ttm_init_fw_resv_region (amdgpu_ttm.c:1741-1772) returns at
    # :1745 (reserve_tmr is set only when the discovery binary is read from VRAM, amdgpu_discovery.c:407-410; CHIP_VEGA20 has none) and
    # vram_usagebyfirmware is acted on only under SR-IOV (amdgpu_atomfirmware.c:119-125, :151-157).
    self.reserved_vram_size = 0
    if DEBUG >= 2: print(f"am {self.devfmt}: MI50 VBIOS ok, gc_info se={self.gc_info.gc_num_se} cu/sh={self.gc_info.gc_num_cu_per_sh} "
                         f"reserved_vram={self.reserved_vram_size >> 20} MiB bootup sclk={fwi.bootup_sclk_in10khz / 100} MHz")

  def _vega20_atom_scratch_bytes(self) -> int:
    """amdgpu_atomfirmware_allocate_fb_scratch (L/amdgpu/amdgpu_atomfirmware.c:173-200): v2_1 asks for used_by_driver_in_kb unless start_address
    carries ATOM_VRAM_BLOCK_SRIOV_MSG_SHARE_RESERVATION (0x2 << ATOM_VRAM_OPERATION_FLAGS_SHIFT 30, atombios.h:4298-4302; :105-131), v2_2 always 0
    on bare metal (:134-171); 0 -> 20 KiB (AtomContext). Host memory, never VRAM."""
    vu = self.vbios.vram_usagebyfirmware()
    if isinstance(vu, vbios.vram_usagebyfirmware_v2_1):
      return 0 if (vu.start_address_in_kb & vbios.ATOM_VRAM_OPERATION_FLAGS_MASK) == (0x2 << 30) else vu.used_by_driver_in_kb << 10
    return 0

  def vega20_is_posted(self) -> bool:
    """POST state = amdgpu_atombios_scratch_need_asic_init (L/amdgpu/amdgpu_atombios.c:1634-1642): RREG32(bios_scratch_reg_offset + 7) &
    ATOM_S7_ASIC_INIT_COMPLETE_MASK (0x200, L/include/atombios.h:7075); scratch 7 = mmBIOS_SCRATCH_7 0x3f BASE_IDX 1 (nbio_7_4_offset.h:2494-2495) ->
    dword 0x53."""
    return bool(self.rreg(self.bios_scratch_reg_offset + 7) & vbios.ATOM_S7_ASIC_INIT_COMPLETE_MASK)

  def _vega20_driver_pptable(self) -> bytes:
    """mi50 (bug 2, Leon 2026-10-16): the PowerPlay table the SMU runs - the TinyGPU driver's boot table (the V420's with Leon's settings) -
    from the driver's MI50 operation 20 (TinyGPUDriver.cpp OP_BOOT_PPTABLE) over this card's TinyGPU connection (server.c CMD_MI50 = 128):
    up to 1024 bytes per call from the byte offset in a0, the table's size in the reply's raw field. Wire format mi50::Mi50Req '<II4q32s',
    mi50::Mi50Resp '<iI504s32s128q'."""
    pd = self.pci_dev
    if not hasattr(pd, "sock"): raise RuntimeError(f"am {self.devfmt}: the MI50's PowerPlay table comes from the TinyGPU driver")
    req, resp = struct.Struct('<II4q32s'), struct.Struct('<iI504s32s128q')
    out, size = b"", None
    while size is None or len(out) < size:
      _, _, r, _ = pd._rpc(pd.sock, pd.dev_id, 128, req.size, payload=req.pack(20, 0, len(out), 0, 0, 0, b""), readout_size=resp.size)
      status, _, err, raw, *v = resp.unpack(r)
      if status:
        why = err.split(b"\0")[0].decode()
        raise RuntimeError(f"am {self.devfmt}: the TinyGPU driver gave no PowerPlay table: {why}")
      size = int.from_bytes(raw[:4], "little")
      out += struct.pack('<128q', *v)[:min(1024, size - len(out))]
    return out

  def _vega20_driver_cu_info(self) -> tuple[int, int]:
    """mi50 (bug 5, Leon 2026-10-16: "do 60CU", the count written everywhere for the card): (active CUs, shader arrays) as the TinyGPU driver
    counted them from the CU fuse registers at its bring-up (TinyGPUDriver.cpp read_cu_info, Linux gfx_v9_0_get_cu_info), from the driver's MI50
    operation 21 (OP_CU_INFO: v0 = active CUs, v1 = shader arrays) over the same connection and wire format as _vega20_driver_pptable."""
    pd = self.pci_dev
    if not hasattr(pd, "sock"): raise RuntimeError(f"am {self.devfmt}: the MI50's CU count comes from the TinyGPU driver")
    req, resp = struct.Struct('<II4q32s'), struct.Struct('<iI504s32s128q')
    _, _, r, _ = pd._rpc(pd.sock, pd.dev_id, 128, req.size, payload=req.pack(21, 0, 0, 0, 0, 0, b""), readout_size=resp.size)
    status, _, err, _, *v = resp.unpack(r)
    if status:
      why = err.split(b"\0")[0].decode()
      raise RuntimeError(f"am {self.devfmt}: the TinyGPU driver gave no CU count: {why}")
    return v[0], v[1]

  @functools.cached_property
  def hwid_names(self) -> dict[int, str]: return {v:k.removesuffix('_HWID') for k,v in vars(am).items() if k.endswith('_HWID') and isinstance(v, int)}

  def _ip_module(self, prefix:str, hwip): return import_module(prefix, self.ip_ver[hwip])

  def _build_regs(self):
    mods = [("mp", am.MP0_HWIP), ("hdp", am.HDP_HWIP), ("gc", am.GC_HWIP), ("mmhub", am.MMHUB_HWIP), ("osssys", am.OSSSYS_HWIP),
      ("nbio" if self.ip_ver[am.GC_HWIP] < (12,0,0) else "nbif", am.NBIO_HWIP)]
    if self.ip_ver[am.SDMA0_HWIP] in {(4,4,2), (4,4,4), (4,2,0)}: mods += [("sdma", am.SDMA0_HWIP)]
    if self.ip_ver.get(am.SMUIO_HWIP) == (11,0,2): mods += [("smuio", am.SMUIO_HWIP)] # mi50: ROM_INDEX/ROM_DATA for the VBIOS
    if self.ip_ver.get(am.THM_HWIP) == (11,0,2): mods += [("thm", am.THM_HWIP)] # mi50: thermal interrupt setup (powerplay port)

    for prefix, hwip in mods:
      self.__dict__.update(import_asic_regs(prefix, self.ip_ver[hwip], cls=functools.partial(AMRegister, adev=self, bases=self.regs_offset[hwip])))
    # mi50: only the stock 'mm' names come from this MP1 import; mp_11_0_0 now also carries the MI50's regMP0_* PSP aliases, which must keep the MP0
    # bases the per-IP loop above gave them (on the 7600 XT they would otherwise be rebound onto MP1)
    self.__dict__.update({k: v for k, v in import_asic_regs('mp', (11, 0, 0), cls=functools.partial(AMRegister, adev=self,
                          bases=self.regs_offset[am.MP1_HWIP])).items() if k.startswith('mm')})

    # Live AIDs like the kernel: 4 SDMAs per AID; the AID lives iff its group's alive-mask is 0xf/0x3/0xc.
    # Dead AIDs must never be touched via the indirect window: writes poison the whole fabric.
    live_sdma = {k for k in self.regs_offset[am.SDMA0_HWIP] if k not in self.harvested[am.SDMA0_HWIP]}
    max_aid = max((k >> 2 for k in self.regs_offset[am.SDMA0_HWIP]), default=0)
    self.aids = [0] + [aid for aid in range(1, max_aid + 1) if sum(1 << (i & 3) for i in live_sdma if i >> 2 == aid) in {0xf, 0x3, 0xc}]
