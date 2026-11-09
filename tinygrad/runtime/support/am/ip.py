import ctypes, time, contextlib, functools
from typing import Iterable, Literal
from tinygrad.helpers import to_mv, data64, lo32, hi32, DEBUG, wait_cond, pad_bytes, getbits
from tinygrad.runtime.autogen.am import am
from tinygrad.runtime.support.amd import import_soc
from tinygrad.runtime.support.am import vbios
from tinygrad.runtime.support.memory import AddrSpace

class AM_IP:
  def __init__(self, adev): self.adev = adev
  def init_sw(self): pass # Prepare sw/allocations for this IP
  def init_hw(self): pass # Initialize hw for this IP
  def fini_hw(self): pass # Finalize hw for this IP
  def set_clockgating_state(self): pass # Set clockgating state for this IP

class AM_SOC(AM_IP):
  def init_sw(self):
    self.module = import_soc(self.adev.ip_ver[am.GC_HWIP])
    self.ih_clients = am.enum_soc21_ih_clientid if (ih_soc21:=self.adev.ip_ver[am.GC_HWIP][0] >= 11) else am.enum_soc15_ih_clientid

    self.gfx_ih_clients = [am.SOC21_IH_CLIENTID_GRBM_CP, am.SOC21_IH_CLIENTID_GFX] \
      if ih_soc21 else [am.SOC15_IH_CLIENTID_GRBM_CP] + [getattr(am, f'SOC15_IH_CLIENTID_SE{i}SH') for i in range(4)]
    self.sdma_ih_clients = [] if ih_soc21 else [getattr(am, f'SOC15_IH_CLIENTID_SDMA{i}') for i in range(8)]

    def _ih_srcs(pref:str, hwip:int) -> dict[int, str]:
      return {getattr(am, k): k[off+9:] for k in dir(am) if k.startswith(f'{pref}_{self.adev.ip_ver[hwip][0]}') and (off:=k.find('__SRCID__')) != -1}

    gfx_srcs, sdma_srcs = _ih_srcs('GFX', am.GC_HWIP), _ih_srcs('SDMA0', am.SDMA0_HWIP)
    self.ih_srcs_names:dict[int, dict[int, str]] = {**{k: gfx_srcs for k in self.gfx_ih_clients}, **{k: sdma_srcs for k in self.sdma_ih_clients}}

  def init_hw(self):
    if self.adev.ip_ver[am.NBIO_HWIP] == (7,4,0): return self._init_hw_nbio_7_4_0()   # mi50 fork: Vega 20 (design v2 B8)
    if self.adev.ip_ver[am.NBIO_HWIP] in {(7,9,0), (7,9,1)}:
      # fence doorbells for harvested xccs (0xff & ~xcc_mask in the kernel); a fully-unharvested chip keeps the previous 0x0
      live_xccs = sum(1 << i for i in self.adev.regs_offset[am.GC_HWIP] if i not in self.adev.harvested[am.GC_HWIP] and i < 8)
      self.adev.regXCC_DOORBELL_FENCE.write(0xff & ~live_xccs)
      for aid in self.adev.aids[1:]:
        self.adev.indirect_wreg_pcie(self.adev.regXCC_DOORBELL_FENCE.addr[0], self.adev.regXCC_DOORBELL_FENCE.encode(shub_slv_mode=1), aid=aid)
      self.adev.regBIFC_GFX_INT_MONITOR_MASK.write(0x7ff)
      self.adev.regBIFC_DOORBELL_ACCESS_EN_PF.write(0xfffff)
    else: self.adev.regRCC_DEV0_EPF2_STRAP2.update(strap_no_soft_reset_dev0_f2=0x0)
    self.adev.regRCC_DEV0_EPF0_RCC_DOORBELL_APER_EN.write(0x1)

  def _init_hw_nbio_7_4_0(self):
    # mi50 fork: Linux soc15_common_hw_init for CHIP_VEGA20 (soc15.c:1248-1275) + soc15_common_late_init (soc15.c:1195-1208), nbio_v7_4.c.
    # Not ported: amdgpu_nbio_program_aspm (soc15.c:1253; AM clears endpoint ASPM in amdev.py _disable_aspm, design v2 B1/U-H5);
    # nbio init_registers (soc15.c:1255 -> nbio_v7_4.c:341-357) acts only on NBIO 7.4.4, a no-op for 7.4.0;
    # remap_hdp_registers (soc15.c:1260-1261 -> nbio_v7_4.c:101-107): the flush uses the un-remapped register, see AM_GMC.flush_hdp.
    # enable_doorbell_aperture(true): WREG32_FIELD15(RCC_DOORBELL_APER_EN, BIF_DOORBELL_APER_EN, 1) (soc15.c:1264 -> nbio_v7_4.c:208-212)
    self.adev.regRCC_DOORBELL_APER_EN.update(bif_doorbell_aper_en=1)
    # soc15_sdma_doorbell_range_init (soc15.c:1234-1245): per instance sdma_doorbell_range(i, true, sdma_engine[i] << 1, sdma_doorbell_range);
    # nbio_v7_4_sdma_doorbell_range (nbio_v7_4.c:138-177): reg = BIF_SDMA0_DOORBELL_RANGE + instance (instance < 2), read, set OFFSET/SIZE, write.
    # sdma_engine[0/1] = AMDGPU_VEGA20_DOORBELL_sDMA_ENGINE0/1 (0x100/0x10A), sdma_doorbell_range = 20 (vega20_reg_init.c:72-73, 98;
    # amdgpu_doorbell.h:137-138); num_instances = 2 for CHIP_VEGA20 (amdgpu_discovery.c:3203).
    sdma_insts = sorted(self.adev.regs_offset[am.SDMA0_HWIP])
    if sdma_insts != [0, 1]: raise RuntimeError(f"NBIO 7.4.0 SOC init expects SDMA instances [0, 1] (amdgpu_discovery.c:3203), got {sdma_insts}")
    for i in sdma_insts:
      self.adev.reg(f"regBIF_SDMA{i}_DOORBELL_RANGE").update(offset=getattr(am, f"AMDGPU_VEGA20_DOORBELL_sDMA_ENGINE{i}") << 1, size=20)
    # vega20_ih_irq_init: ih_doorbell_range(use_doorbell=true, doorbell_index) (vega20_ih.c:356-358) with doorbell_index = doorbell_index.ih << 1
    # (vega20_ih.c:588-589; ih = AMDGPU_VEGA20_DOORBELL_IH 0x178, vega20_reg_init.c:80); nbio_v7_4_ih_doorbell_range: read, OFFSET=index, SIZE=8,
    # write (nbio_v7_4.c:233-245). Programmed here with the other BIF ranges (design v2 B8); AM_IH leaves IH_DOORBELL_RPTR.enable=0, so inert.
    self.adev.regBIF_IH_DOORBELL_RANGE.update(offset=am.AMDGPU_VEGA20_DOORBELL_IH << 1, size=8)
    # soc15_common_late_init: enable_doorbell_selfring_aperture(true) (soc15.c:1205 -> nbio_v7_4.c:214-231): BASE_LOW/HIGH = doorbell.base =
    # pci_resource_start(pdev, 2) (amdgpu_doorbell_mgr.c:210) = the BAR2 bus address from config space (design v2 B8/U-H7: bar_info()[0] is a
    # host VA on the TinyGPU path); CNTL = EN=1 | MODE=1 | SIZE=0, written from 0 (nbio_v7_4.c:217-222, 230).
    from tinygrad.runtime.autogen import pci   # local import: keeps this fork change inside AM_SOC
    bar2_lo = self.adev.pci_dev.read_config(pci.PCI_BASE_ADDRESS_2, 4)
    if bar2_lo & pci.PCI_BASE_ADDRESS_SPACE != 0: raise RuntimeError(f"BAR2 is an IO BAR ({bar2_lo:#x}), expected the doorbell memory BAR")
    is_64bit = bar2_lo & pci.PCI_BASE_ADDRESS_MEM_TYPE_MASK == pci.PCI_BASE_ADDRESS_MEM_TYPE_64
    bar2_hi = self.adev.pci_dev.read_config(pci.PCI_BASE_ADDRESS_3, 4) if is_64bit else 0
    if (doorbell_base:=(bar2_hi << 32) | (bar2_lo & pci.PCI_BASE_ADDRESS_MEM_MASK & 0xffffffff)) == 0:
      raise RuntimeError(f"BAR2 has no assigned bus address (lo={bar2_lo:#x} hi={bar2_hi:#x}); the doorbell self-ring aperture needs one")
    self.adev.regDOORBELL_SELFRING_GPA_APER_BASE_LOW.write(lo32(doorbell_base))
    self.adev.regDOORBELL_SELFRING_GPA_APER_BASE_HIGH.write(hi32(doorbell_base))
    self.adev.regDOORBELL_SELFRING_GPA_APER_CNTL.write(doorbell_selfring_gpa_aper_en=1, doorbell_selfring_gpa_aper_mode=1,
                                                       doorbell_selfring_gpa_aper_size=0)

  def set_clockgating_state(self):
    if self.adev.ip_ver[am.HDP_HWIP] >= (5,2,1): self.adev.regHDP_MEM_POWER_CTRL.update(atomic_mem_power_ctrl_en=1, atomic_mem_power_ds_en=1)

  def doorbell_enable(self, port, awid=0, awaddr_31_28_value=0, offset=0, size=0, aid=0):
    # mi50 fork: no S2A_DOORBELL_ENTRY_* on NBIO 7.4 (nbio_7_4_offset.h; design v2 B8): the SDMA/IH doorbell routing is the BIF_*_DOORBELL_RANGE
    # programming of _init_hw_nbio_7_4_0 (soc15.c:1266-1272), so this is a no-op there.
    if self.adev.ip_ver[am.NBIO_HWIP] == (7,4,0): return
    reg = self.adev.reg(f"{'regGDC_S2A0_S2A' if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0) else 'regS2A'}_DOORBELL_ENTRY_{port}_CTRL")
    val = reg.encode(**{f"s2a_doorbell_port{port}_enable":1, f"s2a_doorbell_port{port}_awid":awid,  f"s2a_doorbell_port{port}_range_size":size,
      f"s2a_doorbell_port{port}_awaddr_31_28_value":awaddr_31_28_value, f"s2a_doorbell_port{port}_range_offset":offset})

    if self.adev.ip_ver[am.NBIO_HWIP] in {(7,9,0), (7,9,1)}: self.adev.indirect_wreg_pcie(reg.addr[0], val, aid=aid)
    else: reg.write(val)

class AM_GMC(AM_IP):
  def init_sw(self):
    self.vmhubs = len(self.adev.regs_offset[am.MMHUB_HWIP])

    xgmi_lfb_cntl = self.adev.regGCMC_VM_XGMI_LFB_CNTL.read_bitfields() if hasattr(self.adev, 'regGCMC_VM_XGMI_LFB_CNTL') else {}
    self.xgmi_phys_id, self.xgmi_max_region = xgmi_lfb_cntl.get('pf_lfb_region', 0), xgmi_lfb_cntl.get('pf_max_region', 0)
    self.xgmi_seg_sz = self.adev.regGCMC_VM_XGMI_LFB_SIZE.read_bitfields()['pf_lfb_size']<<24 if hasattr(self.adev, 'regGCMC_VM_XGMI_LFB_CNTL') else 0

    self.paddr_base = self.xgmi_phys_id * self.xgmi_seg_sz
    # mi50 fork (design v2 B6): Linux gmc_v9_0_vram_gtt_location: vram_base_offset = gfxhub get_mc_fb_offset() + xgmi node offset
    # (gmc_v9_0.c:1663-1668); gfxhub_v1_0_get_mc_fb_offset = (u64)RREG32_SOC15(GC, 0, mmMC_VM_FB_OFFSET) << 24 (gfxhub_v1_0.c:34-37); every
    # page-table PA goes through paddr2xgmi (amdev.py AMPageTableEntry.set_entry), so the fold-in here makes PTEs/PDEs and the TMR address
    # Linux-correct. Register = gc_9_0 mmMC_VM_FB_OFFSET (table name mmGCMC_VM_FB_OFFSET, GC seg0 + 0x96b = dword 0x296b).
    if self.adev.ip_ver[am.GC_HWIP] == (9,4,0): self.paddr_base += self.adev.regGCMC_VM_FB_OFFSET.read() << 24

    self.fb_base = (self.adev.regMMMC_VM_FB_LOCATION_BASE.read() & 0xFFFFFF) << 24
    self.fb_end = (self.adev.regMMMC_VM_FB_LOCATION_TOP.read() & 0xFFFFFF) << 24

    # Memory controller aperture
    self.mc_base = self.fb_base + self.paddr_base

    # VM aperture
    self.vm_base = self.adev.mm.va_base
    self.vm_end = min(self.vm_base + (1 << self.adev.mm.va_bits) - 1, 0x7fffffffffff)

    # mi50 fork (design v2 B6): Linux sets gmc.translate_further only for GC 9.4.2 in the shared gfx9 case (gmc_v9_0.c:1906-1923; 9.4.0 at :1908
    # keeps the default false), so Vega 20 runs the 4-level / block_size-0 layout and get_vm_pde adds no BFS/TF (gmc_v9_0.c:1027-1028).
    self.trans_futher = self.adev.ip_ver[am.GC_HWIP] < (10, 0, 0) and self.adev.ip_ver[am.GC_HWIP] != (9, 4, 0)

    # mi3xx has 48-bit, others have 44-bit address space
    self.address_space_mask = (1 << (48 if self.adev.ip_ver[am.GC_HWIP][:2] in {(9,4), (9,5)} else 44)) - 1

    self.memscratch_xgmi_paddr = self.adev.paddr2xgmi(self.adev.mm.palloc(0x1000, zero=False, boot=True))
    self.dummy_page_xgmi_paddr = self.adev.paddr2xgmi(self.adev.mm.palloc(0x1000, zero=False, boot=True))

    # MM hub is inited before any tlb flushes and is still valid during partial_boot, so set it to true
    self.hub_initted = {"MM": True, "GC": False}

    self.mm_insts = self.adev.aids if self.adev.ip_ver[am.NBIO_HWIP] in {(7,9,0), (7,9,1)} else list(range(self.vmhubs)) # dead mmhubs hang us

    self.pf_status_reg = lambda ip: f"reg{ip}VM_L2_PROTECTION_FAULT_STATUS{'_LO32' if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0) else ''}"

  def init_hw(self):
    # mi50 fork: golden + HDP init + flush before the hubs (design v2 B9/B10)
    if self.adev.ip_ver[am.GC_HWIP] == (9,4,0):
      self._init_hw_vega20_pre_hub()
      # review 2026-10-13 L6: gmc_v9_0_hw_init flushes vmid 0 on every hub BEFORE gart_enable (gmc_v9_0.c:2208-2212 -> :2217); AM's flush_tlb is
      # that ENG17 request/ack (+ the MMHUB semaphore, gmc_v9_0_use_invalidate_semaphore :734-746). Ported for the MM hub here, in Linux order.
      # The GFX hub's flush (:2211, same loop) is NOT issued: AM's flush_tlb returns while hub_initted["GC"] is False (ip.py :190) and the GC hub is
      # programmed later by AM_GFX (init_hub("GC")); AM flushes after every mapping (on_range_mapped) - skip listed, T9 decides.
      self.flush_tlb("MM", vmid=0)
    self.init_hub("MM", insts=self.mm_insts)

  def _golden(self, reg, and_mask:int, or_mask:int):
    # soc15_program_register_sequence (soc15.c:455-485): and_mask != 0xffffffff -> tmp = read; tmp &= ~and_mask; tmp |= or_mask & and_mask; write
    if and_mask == 0xffffffff: reg.write(or_mask)
    else: reg.write((reg.read() & ~and_mask) | (or_mask & and_mask))

  def _init_hw_vega20_pre_hub(self):
    # mi50 fork: the gmc_v9_0_hw_init steps that precede gart_enable (gmc_v9_0.c:2163-2217), in Linux order. Not ported: the VGA lockout
    # (:2183-2188, keyed on mode_info.num_crtc) and mmhub update_power_gating (:2190-2191, an SMU power-gating message under pg_flags).
    # 1. gmc_v9_0_init_golden_registers (:2079-2093): MMHUB_HWIP (9,4,0) -> golden_settings_mmhub_1_0_0 + golden_settings_athub_1_0_0 (:333-341)
    if (mmhub:=self.adev.ip_ver[am.MMHUB_HWIP]) != (9,4,0): raise RuntimeError(f"GC 9.4.0 expects MMHUB 9.4.0 (amdgpu_discovery.c:3207), got {mmhub}")
    self._golden(self.adev.regDAGB1_WRCLI2, 0x00000007, 0xfe5fe0fa)               # gmc_v9_0.c:334
    self._golden(self.adev.regMMEA1_DRAM_WR_CLI2GRP_MAP0, 0x00000030, 0x55555565)  # gmc_v9_0.c:335
    self._golden(self.adev.regRPB_ARB_CNTL, 0x0000ff00, 0x00000800)                # gmc_v9_0.c:339 (athub_1_0 table; design v2 §3 _build_regs row)
    self._golden(self.adev.regRPB_ARB_CNTL2, 0x00ff00ff, 0x00080008)               # gmc_v9_0.c:340
    # 2. hdp init_registers (:2193 -> hdp_v4_0_init_registers, hdp_v4_0.c:137-158): HDP 4.2.0 takes no (4,2,1)/(4,4,0) special case;
    #    FLUSH_INVALIDATE_CACHE=1 via WREG32_FIELD15 (:151), NONSURFACE_BASE = vram_start >> 8, _HI = vram_start >> 40 (:156-157);
    #    vram_start = fb location base + xgmi node offset (gmc_v9_0.c:1654-1658 -> amdgpu_gmc_vram_location mc->vram_start = base).
    if (hdp:=self.adev.ip_ver[am.HDP_HWIP]) != (4,2,0): raise RuntimeError(f"GC 9.4.0 expects HDP 4.2.0 (amdgpu_discovery.c:3210), got {hdp}")
    vram_start = self.fb_base + self.xgmi_phys_id * self.xgmi_seg_sz
    self.adev.regHDP_MISC_CNTL.update(flush_invalidate_cache=1)
    self.adev.regHDP_NONSURFACE_BASE.write(vram_start >> 8)
    self.adev.regHDP_NONSURFACE_BASE_HI.write(vram_start >> 40)
    # 3. "After HDP is initialized, flush HDP." (:2195-2196)
    self.flush_hdp()
    # 4. set_fault_enable_default on both hubs (:2203-2207) is done per hub at the top of init_hub (the GC hub is programmed later by AM_GFX).
    # 5. the vmid-0 flush before gart_enable (:2208-2212): MM hub flushed in init_hw right after this (review L6); GC hub skip listed there.

  def _set_fault_enable_default(self, ip:Literal["MM", "GC"], inst:int):
    # mi50 fork: gmc_v9_0_hw_init value = true unless amdgpu_vm_fault_stop == AMDGPU_VM_FAULT_STOP_ALWAYS (gmc_v9_0.c:2198-2201; module default 0
    # amdgpu_drv.c:173, ALWAYS = 2 amdgpu_vm.h:147). gfxhub_v1_0_set_fault_enable_default (gfxhub_v1_0.c:376-411) sets the 11 *_ENABLE_DEFAULT
    # fields = value and CRASH_ON_NO_RETRY_FAULT / CRASH_ON_RETRY_FAULT = !value; mmhub_v1_0_set_fault_enable_default (mmhub_v1_0.c:425-465) sets
    # the same 11 fields and touches the CRASH bits only when !value. Both are read-modify-write.
    en = {f'{x}_protection_fault_enable_default': 1 for x in ['range', 'pde0', 'pde1', 'pde2', 'translate_further', 'nack', 'dummy_page', 'valid',
                                                               'read', 'write', 'execute']}
    crash = {'crash_on_no_retry_fault': 0, 'crash_on_retry_fault': 0} if ip == "GC" else {}
    self.adev.reg(f"reg{ip}VM_L2_PROTECTION_FAULT_CNTL").update(**en, **crash, inst=inst)

  def flush_hdp(self):
    if self.adev.ip_ver[am.NBIO_HWIP] == (7,4,0):
      # mi50 fork (design v2 B9): amdgpu_hdp_generic_flush (amdgpu_hdp.c:51-60) writes 0 at (rmmio_remap.reg_offset + KFD_MMIO_REMAP_HDP_MEM_FLUSH_CNTL)
      # >> 2 then reads memsize. Review 2026-10-13 L5 (citation corrected): on x86 bare metal nbio_v7_4_set_reg_remap takes the REMAP branch
      # (nbio_v7_4.c:794-796: reg_offset = MMIO_REG_HOLE_OFFSET = 0x80000 - PAGE_SIZE) and nbio_v7_4_remap_hdp_registers (:101-107, called from
      # soc15 hw_init) points that hole at HDP_MEM_COHERENCY_FLUSH_CNTL, so the write lands on the same register this writes directly; the
      # else-branch (:797-801, SR-IOV / PAGE_SIZE > 4096) names that register outright: mmBIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL
      # (NBIO seg2 0xd20 + 0xf7). The remap programming itself is NOT ported (KFD_MMIO_REMAP_* constants live in include/uapi/linux/kfd_ioctl.h,
      # not in the amd tree here) - skip listed. memsize = RCC_CONFIG_MEMSIZE (nbio_v7_4.c:133-136, dword 0xde3).
      self.adev.regBIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL.write(0x0)
      self.adev.regRCC_CONFIG_MEMSIZE.read()
      return
    self.adev.wreg(self.adev.reg("regBIF_BX0_REMAP_HDP_MEM_FLUSH_CNTL").read() // 4, 0x0)
  def flush_tlb(self, ip:Literal["MM", "GC"], vmid, flush_type=0):
    self.flush_hdp()

    # Can't issue TLB invalidation if the hub isn't initialized.
    if not self.hub_initted[ip]: return

    for inst in (self.adev.gmc.mm_insts if ip == "MM" else range(self.adev.gfx.xccs)):
      if ip == "MM": wait_cond(lambda: self.adev.regMMVM_INVALIDATE_ENG17_SEM.read(inst=inst) & 0x1, value=1, msg="mm flush_tlb timeout")

      self.adev.reg(f"reg{ip}VM_INVALIDATE_ENG17_REQ").write(flush_type=flush_type, per_vmid_invalidate_req=(1 << vmid), invalidate_l2_ptes=1,
        invalidate_l2_pde0=1, invalidate_l2_pde1=1, invalidate_l2_pde2=1, invalidate_l1_ptes=1, clear_protection_fault_status_addr=0, inst=inst)

      wait_cond(lambda: self.adev.reg(f"reg{ip}VM_INVALIDATE_ENG17_ACK").read(inst=inst) & (1 << vmid), value=(1 << vmid), msg="flush_tlb timeout")

      if ip == "MM": self.adev.regMMVM_INVALIDATE_ENG17_SEM.write(0x0, inst=inst)
      if self.adev.ip_ver[am.GC_HWIP] >= (11,0,0) and ip == "MM":
        self.adev.regMMVM_L2_BANK_SELECT_RESERVED_CID2.update(reserved_cache_private_invalidation=1, inst=inst)

        # Read back the register to ensure the invalidation is complete
        self.adev.regMMVM_L2_BANK_SELECT_RESERVED_CID2.read(inst=inst)

  def enable_vm_addressing(self, page_table, ip:Literal["MM", "GC"], vmid, inst):
    self.adev.wreg_pair(f"reg{ip}VM_CONTEXT{vmid}_PAGE_TABLE_START_ADDR", "_LO32", "_HI32", self.vm_base >> 12, inst=inst)
    self.adev.wreg_pair(f"reg{ip}VM_CONTEXT{vmid}_PAGE_TABLE_END_ADDR", "_LO32", "_HI32", self.vm_end >> 12, inst=inst)
    self.adev.wreg_pair(f"reg{ip}VM_CONTEXT{vmid}_PAGE_TABLE_BASE_ADDR", "_LO32", "_HI32", self.adev.paddr2xgmi(page_table.paddr) | 1, inst=inst)

    fault_flags = {f'{x}_protection_fault_enable_interrupt':1 for x in ['pde0', 'dummy_page', 'range', 'valid', 'read', 'write', 'execute']}
    en_def_flags = {f'{x}_protection_fault_enable_default':1 for x in ['pde0', 'dummy_page', 'range', 'valid', 'read', 'write', 'execute']}
    self.adev.reg(f"reg{ip}VM_CONTEXT{vmid}_CNTL").write(0x1800000, **fault_flags, **en_def_flags, enable_context=1,
      page_table_depth=((2 if self.trans_futher else 3) - page_table.lv), page_table_block_size=9 if self.trans_futher else 0, inst=inst)

  def init_hub(self, ip:Literal["MM", "GC"], insts:Iterable[int]):
    vega20 = self.adev.ip_ver[am.GC_HWIP] == (9,4,0)   # mi50 fork: gfxhub_v1_0 / mmhub_v1_0 deltas (design v2 B10), exact-version gated
    # Init system apertures
    for inst in insts:
      if vega20: self._set_fault_enable_default(ip, inst)   # gmc_v9_0.c:2203-2207, before this hub's gart_enable (:2217)
      self.adev.reg(f"reg{ip}MC_VM_AGP_BASE").write(0, inst=inst)
      self.adev.reg(f"reg{ip}MC_VM_AGP_BOT").write(0xffffffffffff >> 24, inst=inst) # disable AGP
      self.adev.reg(f"reg{ip}MC_VM_AGP_TOP").write(0, inst=inst)

      self.adev.reg(f"reg{ip}MC_VM_SYSTEM_APERTURE_LOW_ADDR").write(self.fb_base >> 18, inst=inst)
      # mi50 fork (review 2026-10-13 L12): Linux writes max(fb_end, agp_end) >> 18 with fb_end = vram_start + mc_vram_size - 1 (gfxhub_v1_0.c:119-122;
      # amdgpu_gmc.c:221/:233), i.e. (FB_LOCATION_TOP << 24 | 0xffffff) >> 18 = AM's value | 0x3f (AGP disabled: agp_end 0). Other cards keep AM's.
      self.adev.reg(f"reg{ip}MC_VM_SYSTEM_APERTURE_HIGH_ADDR").write(((self.fb_end | 0xffffff) if vega20 else self.fb_end) >> 18, inst=inst)
      self.adev.wreg_pair(f"reg{ip}MC_VM_SYSTEM_APERTURE_DEFAULT_ADDR", "_LSB", "_MSB", self.memscratch_xgmi_paddr >> 12, inst=inst)
      self.adev.wreg_pair(f"reg{ip}VM_L2_PROTECTION_FAULT_DEFAULT_ADDR", "_LO32", "_HI32", self.dummy_page_xgmi_paddr >> 12, inst=inst)

      self.adev.reg(f"reg{ip}VM_L2_PROTECTION_FAULT_CNTL2").update(active_page_migration_pte_read_retry=1, inst=inst)

      # Init TLB and cache
      # mi50 fork: gfxhub_v1_0_init_tlb_regs / mmhub_v1_0_init_tlb_regs add ATC_EN=1 (gfxhub_v1_0.c:169, mmhub_v1_0.c:152; mask 0x2000 =
      # gc_9_0_sh_mask.h:8662 / mmhub_1_0_sh_mask.h:10076)
      self.adev.reg(f"reg{ip}MC_VM_MX_L1_TLB_CNTL").update(enable_l1_tlb=1, system_access_mode=3, enable_advanced_driver_model=1,
        system_aperture_unmapped_access=0, mtype=self.adev.soc.module.MTYPE_UC, **({'atc_en': 1} if vega20 else {}), inst=inst)

      # mi50 fork: gfxhub_v1_0_init_cache_regs / mmhub_v1_0_init_cache_regs (gfxhub_v1_0.c:179-188, mmhub_v1_0.c:165-174) set ENABLE_L2_CACHE,
      # ENABLE_L2_FRAGMENT_PROCESSING, L2_PDE0_CACHE_TAG_GENERATION_MODE=0, PDE_FAULT_CLASSIFICATION=0, CONTEXT1_IDENTITY_ACCESS_MODE=1,
      # IDENTITY_MODE_FRAGMENT_SIZE=0 and never ENABLE_DEFAULT_PAGE_OUT_TO_SYSTEM_MEMORY -> dropped on Vega 20 (design v2 B10)
      self.adev.reg(f"reg{ip}VM_L2_CNTL").update(enable_l2_cache=1, **({} if vega20 else {'enable_default_page_out_to_system_memory': 1}),
        l2_pde0_cache_tag_generation_mode=0, pde_fault_classification=0, context1_identity_access_mode=1, identity_mode_fragment_size=0,
        enable_l2_fragment_processing=int(self.adev.ip_ver[am.GC_HWIP] < (10,0,0)), inst=inst)
      self.adev.reg(f"reg{ip}VM_L2_CNTL2").update(invalidate_all_l1_tlbs=1, invalidate_l2_cache=1, inst=inst)
      # (Vega 20: mmVM_L2_CNTL3_DEFAULT 0x80100007 with BANK_SELECT=9, L2_CACHE_BIGK_FRAGMENT_SIZE=6 (gfxhub_v1_0.c:195-205, mmhub_v1_0.c:181-191,
      #  gc_9_0_default.h:595) = 0x80130009 = exactly this write with trans_futher False; checked offline in extra/mi50/ip_soc_gmc_ih_check.py)
      self.adev.reg(f"reg{ip}VM_L2_CNTL3").write(l2_cache_4k_associativity=1, l2_cache_bigk_associativity=1,
        bank_select=12 if self.trans_futher else 9, l2_cache_bigk_fragment_size=9 if self.trans_futher else 6, inst=inst)
      if vega20:
        # mmVM_L2_CNTL4_DEFAULT 0xc1 (gc_9_0_default.h:615, mmhub_1_0_default.h:673) with VMC_TAP_PDE/PTE_REQUEST_PHYSICAL = 0
        # (gfxhub_v1_0.c:207-215 non-connected_to_cpu branch, mmhub_v1_0.c:193-196), written whole
        cntl4 = self.adev.reg(f"reg{ip}VM_L2_CNTL4")
        cntl4.write(0xc1 & ~cntl4.fields_mask('vmc_tap_pde_request_physical', 'vmc_tap_pte_request_physical'), inst=inst)
      else: self.adev.reg(f"reg{ip}VM_L2_CNTL4").write(l2_cache_4k_partition_count=1, inst=inst)
      if self.adev.ip_ver[am.GC_HWIP] >= (10,0,0): self.adev.reg(f"reg{ip}VM_L2_CNTL5").write(walker_priority_client_id=0x1ff, inst=inst)

      self.enable_vm_addressing(self.adev.mm.root_page_table, ip, vmid=0, inst=inst)

      # Disable identity aperture
      self.adev.wreg_pair(f"reg{ip}VM_L2_CONTEXT1_IDENTITY_APERTURE_LOW_ADDR", "_LO32", "_HI32", 0xfffffffff, inst=inst)
      self.adev.wreg_pair(f"reg{ip}VM_L2_CONTEXT1_IDENTITY_APERTURE_HIGH_ADDR", "_LO32", "_HI32", 0x0, inst=inst)
      self.adev.wreg_pair(f"reg{ip}VM_L2_CONTEXT_IDENTITY_PHYSICAL_OFFSET", "_LO32", "_HI32", 0x0, inst=inst)

      for eng_i in range(18): self.adev.wreg_pair(f"reg{ip}VM_INVALIDATE_ENG{eng_i}_ADDR_RANGE", "_LO32", "_HI32", 0x1fffffffff, inst=inst)
    self.hub_initted[ip] = True

  @functools.cache  # pylint: disable=method-cache-max-size-none
  def get_pte_flags(self, pte_lv, is_table, frag, uncached, system, snooped, valid, extra=0):
    extra |= (am.AMDGPU_PTE_SYSTEM * system) | (am.AMDGPU_PTE_SNOOPED * snooped) | (am.AMDGPU_PTE_VALID * valid) | am.AMDGPU_PTE_FRAG(frag)
    if not is_table: extra |= (am.AMDGPU_PTE_WRITEABLE | am.AMDGPU_PTE_READABLE | am.AMDGPU_PTE_EXECUTABLE)
    if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0):
      extra |= am.AMDGPU_PTE_MTYPE_GFX12(0, self.adev.soc.module.MTYPE_UC if uncached else 0)
      extra |= (am.AMDGPU_PDE_PTE_GFX12 if not is_table and pte_lv != am.AMDGPU_VM_PTB else (am.AMDGPU_PTE_IS_PTE if not is_table else 0))
    elif self.adev.ip_ver[am.GC_HWIP] >= (10,0,0):
      extra |= am.AMDGPU_PTE_MTYPE_NV10(0, self.adev.soc.module.MTYPE_UC if uncached else 0)
      extra |= (am.AMDGPU_PDE_PTE if not is_table and pte_lv != am.AMDGPU_VM_PTB else 0)
    else:
      extra |= am.AMDGPU_PTE_MTYPE_VG10(0, self.adev.soc.module.MTYPE_UC if uncached else 0)
      if self.trans_futher:
        if is_table and pte_lv == am.AMDGPU_VM_PDB1: extra |= am.AMDGPU_PDE_BFS(0x9)
        if is_table and pte_lv == am.AMDGPU_VM_PDB0: extra |= am.AMDGPU_PTE_TF
        if not is_table and pte_lv not in {am.AMDGPU_VM_PTB, am.AMDGPU_VM_PDB0}: extra |= am.AMDGPU_PDE_PTE
      # mi50 fork (design v2 B10): gmc_v9_0_get_vm_pde returns before any BFS/TF when !translate_further (gmc_v9_0.c:1027-1028), so a huge page
      # at any PD level keeps its PDE_PTE flag (the gfx10 rule above) and PDB0 is not special
      elif not is_table and pte_lv != am.AMDGPU_VM_PTB: extra |= am.AMDGPU_PDE_PTE
    return extra
  def is_pte_huge_page(self, pte_lv, pte):
    if self.adev.ip_ver[am.GC_HWIP] < (10,0,0) and self.trans_futher:
      return (pte & am.AMDGPU_PDE_PTE) if pte_lv != am.AMDGPU_VM_PDB0 else not (pte & am.AMDGPU_PTE_TF)
    # (gfx9 without translate_further takes this PDE_PTE-only rule too)
    return pte & (am.AMDGPU_PDE_PTE_GFX12 if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0) else am.AMDGPU_PDE_PTE)

class AM_SMU(AM_IP):
  # mi50 fork: Vega 20 (MI50, gfx906) is MP1/MP0 11.0.2 (Linux amdgpu_discovery.c:3216-3217) on the legacy powerplay stack
  # (pm/powerplay/smumgr/vega20_smumgr.c + pm/powerplay/hwmgr/vega20_hwmgr.c; message numbers pm/powerplay/inc/vega20_ppsmc.h:37-124,
  # tables/structs pm/powerplay/inc/smu11_driver_if.h). Spec: "Vega20 Port Design v2" B13 (minimal init sequence), §6 (telemetry /
  # overdrive), §3 row `AM_SMU`. Every Vega 20 branch is keyed on the exact ip version (design rule R-LINUX); the gfx11 (RX 7600 XT,
  # MP1 13.0.x) and MI300 (MP0 13.0.6/12/15) paths are the unchanged upstream lines.
  class ResponseError(RuntimeError): pass  # Vega 20 mailbox answered with a PPSMC_Result_* other than OK (vega20_ppsmc.h:30-34)
  # PMSTATUSLOG buffer: `tools_size = 0x19000` vega20_smumgr.c:421, allocated :467-479, handed to the SMU by vega20_set_tools_address :375-394
  VEGA20_TOOLS_SIZE = 0x19000
  # vega20_od8_get_gfx_clock_base_voltage (vega20_hwmgr.c:1227-1245):
  #   GetAVFSVoltageByDpm param = (AVFS_CURVE << 24) | (OD8_HOTCURVE_TEMPERATURE << 16) | freq
  VEGA20_AVFS_CURVE, VEGA20_OD8_HOTCURVE_TEMPERATURE = 0, 85  # vega20_hwmgr.h:42-43
  VEGA20_VOLTAGE_SCALE = 4  # pm/powerplay/inc/hwmgr.h:43; OverDriveTable_t.GfxclkVolt* = mV * VOLTAGE_SCALE (vega20_hwmgr.c:1299-1300, :3105)

  def _vega20(self) -> bool: return self.adev.ip_ver[am.MP1_HWIP] == (11,0,2)
  def _vega20_only(self, what:str):
    if not self._vega20():
      raise RuntimeError(f"am {self.adev.devfmt}: AM_SMU.{what} is implemented for Vega 20 (MP1 11.0.2) only, "
                         f"this is MP1 {self.adev.ip_ver[am.MP1_HWIP]}")

  def init_sw(self):
    self.smu_mod = self.adev._ip_module("smu", am.MP1_HWIP)  # (11,0,2) -> autogen smu_11_0_2 (support/amd.py import_module: same major, <= target)
    if self._vega20(): return self._vega20_smu_init()
    self.driver_table_paddr = self.adev.mm.palloc(0x4000, zero=False, boot=True)

  def init_hw(self):
    if self._vega20():
      # Linux: amdgpu_pm_load_smu_firmware -> pp_dpm_load_fw -> vega20_start_smu right after the PSP loaded the SMC image (amd_powerplay.c:308-320),
      # then the powerplay IP hw_init -> hwmgr_hw_init (amd_powerplay.c:164-176, hwmgr.c:228-275). AM runs PSP.init_hw then SMU.init_hw: same order.
      self._vega20_start_smu()
      return self._vega20_hwmgr_hw_init()
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrHigh, hi32(self.adev.paddr2mc(self.driver_table_paddr)))
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrLow, lo32(self.adev.paddr2mc(self.driver_table_paddr)))
    self._send_msg(self.smu_mod.PPSMC_MSG_EnableAllSmuFeatures, 0)

  # ---- Vega 20 powerplay, ported from Linux pm/powerplay (smumgr/vega20_smumgr.c, hwmgr/vega20_hwmgr.c, vega20_processpptables.c,
  # vega20_thermal.c, vega20_powertune.c, hwmgr.c, hardwaremanager.c, ppatomfwctrl.c). Line numbers refer to those files. ----
  def _vega20_smu_init(self):
    """vega20_smu_init (vega20_smumgr.c:418-553): one PAGE_SIZE-aligned VRAM buffer per SMU table. smu_v11_0_i2c_control_init (:542) only registers a
    Linux i2c adapter object (smu_v11_0_i2c.c:727-753, no register access) and has no counterpart here."""
    m = self.smu_mod
    self._vega20_results = {v: k for k, v in vars(m).items() if k.startswith("PPSMC_Result_")}  # vega20_ppsmc.h:30-34 names for errors
    self.vega20_tables = {tid: (self.adev.mm.palloc(size, align=0x1000, zero=False, boot=True), size) for tid, size in (
      (m.TABLE_PPTABLE, ctypes.sizeof(m.PPTable_t)), (m.TABLE_WATERMARKS, ctypes.sizeof(m.Watermarks_t)),
      (m.TABLE_PMSTATUSLOG, self.VEGA20_TOOLS_SIZE), (m.TABLE_OVERDRIVE, ctypes.sizeof(m.OverDriveTable_t)),
      (m.TABLE_SMU_METRICS, ctypes.sizeof(m.SmuMetrics_t)), (m.TABLE_ACTIVITY_MONITOR_COEFF, ctypes.sizeof(m.DpmActivityMonitorCoeffInt_t)))}

  def _vega20_start_smu(self):
    """vega20_start_smu (vega20_smumgr.c:589-604): vega20_is_smc_ram_running (:49-62) = RREG32_PCIE(MP1_Public | smnMP1_FIRMWARE_FLAGS) bit
    INTERRUPTS_ENABLED (mp_11_0_sh_mask.h:266-268), then vega20_set_tools_address (:375-394) with the PMSTATUSLOG buffer when its address is set."""
    if not self.adev.indirect_rreg((0x03b00000 | 0x3010024) // 4) & 0x1: raise RuntimeError(f"am {self.adev.devfmt}: [Vega20StartSmu] SMC is not running!")
    if (tools_mc:=self.adev.paddr2mc(self.vega20_tables[self.smu_mod.TABLE_PMSTATUSLOG][0])):
      self._send_msg(self.smu_mod.PPSMC_MSG_SetToolsDramAddrHigh, hi32(tools_mc))
      self._send_msg(self.smu_mod.PPSMC_MSG_SetToolsDramAddrLow, lo32(tools_mc))

  def _vega20_hwmgr_hw_init(self):
    """hwmgr_hw_init (hwmgr.c:228-275), pm_en = amdgpu_dpm (-1, amdgpu_drv.c:163) && not_vf: pptable_init -> backend_init ->
    psm_init_power_state_table (vega20 has no get_num_of_pp_table_entries: returns 0, pp_psm.c:36-37) -> phm_setup_asic (vega20_setup_asic_task:
    low_sclk_interrupt_threshold = 0; the BACO workaround only runs in a BACO reset / runtime PM, vega20_hwmgr.c:499-519) ->
    phm_enable_dynamic_state_management (= vega20_enable_dpm_tasks, :4421; the in_suspend skip does not apply at init) ->
    phm_start_thermal_controller -> psm_set_performance_states (hwmgr->ps is NULL for vega20: returns 0, pp_psm.c:200-206)."""
    self._vega20_pptable_init()
    self._vega20_backend_init()
    self.vega20_low_sclk_interrupt_threshold = 0
    self._vega20_enable_dpm_tasks()
    self._vega20_start_thermal_controller()

  def _vega20_pptable_init(self):
    """vega20_pp_tables_initialize (vega20_processpptables.c:323-350): get_powerplay_table + check_powerplay_tables (vbios.VBIOS.powerplay),
    set_platform_caps (:93-115), init_powerplay_table_information (:238-321) incl. append_vbios_pptable (:126-196) and the fan target override (:198-206)."""
    pp = self.adev.vbios.powerplay()
    caps = self.vega20_caps = set()
    for bit, cap in ((0x1, "PowerPlaySupport"), (0x2, "BiosPowerSourceControl"), (0x4, "BACO"), (0x8, "BAMACO")):   # vega20_pptable.h:31-34
      if pp.ulPlatformCaps & bit: caps.add(cap)
    if pp.ucThermalControllerType != 0: caps.add("ThermalController")   # ATOM_VEGA20_PP_THERMALCONTROLLER_NONE 0 (vega20_pptable.h:28); :244-246
    caps.add("MicrocodeFanControl")                                     # :248
    info = self.vega20_pptable_info = {"od_feature_capabilities": [], "od_settings_max": [], "od_settings_min": []}
    od = pp.OverDrive8Table
    if od.ucODTableRevision == 1:   # :250-273
      nf, ns = min(od.ODFeatureCount, len(vbios.ATOM_VEGA20_ODFEATURE)), min(od.ODSettingCount, len(vbios.ATOM_VEGA20_ODSETTING))
      info["od_feature_capabilities"] = [od.ODFeatureCapabilities[i] & 0xff for i in range(nf)]          # copy_overdrive_feature_capabilities_array
      if any(info["od_feature_capabilities"]): caps.add("ACOverdriveSupport")
      info["od_settings_max"] = [od.ODSettingsMax[i] for i in range(ns)]                                  # phm_copy_overdrive_settings_limits_array
      info["od_settings_min"] = [od.ODSettingsMin[i] for i in range(ns)]
    for k in ("SmallPowerLimit1", "SmallPowerLimit2", "BoostPowerLimit", "ODTurboPowerLimit", "ODPowerSavePowerLimit", "SoftwareShutdownTemp"):
      info["us_" + k] = getattr(pp, "us" + k)   # :275-282
    self.vega20_tdp_od_limit = od.ODSettingsMax[vbios.ATOM_VEGA20_ODSETTING.index("POWERPERCENTAGE")]   # :284
    if self.vega20_tdp_od_limit: caps.add("PowerControl")                                               # :286-289
    pt = self.vega20_smc_pptable = self.smu_mod.PPTable_t.from_buffer_copy(bytes(pp.smcPPTable))       # :307-310 kmemdup
    d = self.adev.vbios.smc_dpm_info()                                                                   # append_vbios_pptable :126-196
    for dst, src in (("MaxVoltageStepGfx", "maxvoltagestepgfx"), ("MaxVoltageStepSoc", "maxvoltagestepsoc"), ("VddGfxVrMapping", "vddgfxvrmapping"),
        ("VddSocVrMapping", "vddsocvrmapping"), ("VddMem0VrMapping", "vddmem0vrmapping"), ("VddMem1VrMapping", "vddmem1vrmapping"),
        ("GfxUlvPhaseSheddingMask", "gfxulvphasesheddingmask"), ("SocUlvPhaseSheddingMask", "soculvphasesheddingmask"),
        ("ExternalSensorPresent", "externalsensorpresent"), ("GfxMaxCurrent", "gfxmaxcurrent"), ("GfxOffset", "gfxoffset"),
        ("Padding_TelemetryGfx", "padding_telemetrygfx"), ("SocMaxCurrent", "socmaxcurrent"), ("SocOffset", "socoffset"),
        ("Padding_TelemetrySoc", "padding_telemetrysoc"), ("Mem0MaxCurrent", "mem0maxcurrent"), ("Mem0Offset", "mem0offset"),
        ("Padding_TelemetryMem0", "padding_telemetrymem0"), ("Mem1MaxCurrent", "mem1maxcurrent"), ("Mem1Offset", "mem1offset"),
        ("Padding_TelemetryMem1", "padding_telemetrymem1"), ("AcDcGpio", "acdcgpio"), ("AcDcPolarity", "acdcpolarity"), ("VR0HotGpio", "vr0hotgpio"),
        ("VR0HotPolarity", "vr0hotpolarity"), ("VR1HotGpio", "vr1hotgpio"), ("VR1HotPolarity", "vr1hotpolarity"), ("Padding1", "padding1"),
        ("Padding2", "padding2"), ("LedPin0", "ledpin0"), ("LedPin1", "ledpin1"), ("LedPin2", "ledpin2"),
        ("PllGfxclkSpreadEnabled", "pllgfxclkspreadenabled"), ("PllGfxclkSpreadPercent", "pllgfxclkspreadpercent"),
        ("PllGfxclkSpreadFreq", "pllgfxclkspreadfreq"), ("UclkSpreadPercent", "uclkspreadpercent"), ("UclkSpreadFreq", "uclkspreadfreq"),
        ("FclkSpreadEnabled", "fclkspreadenabled"), ("FclkSpreadPercent", "fclkspreadpercent"), ("FclkSpreadFreq", "fclkspreadfreq"),
        ("FllGfxclkSpreadEnabled", "fllgfxclkspreadenabled"), ("FllGfxclkSpreadPercent", "fllgfxclkspreadpercent"),
        ("FllGfxclkSpreadFreq", "fllgfxclkspreadfreq")):
      setattr(pt, dst, getattr(d, src))
    pt.UclkSpreadEnabled = 0   # :172
    for i in range(self.smu_mod.I2C_CONTROLLER_NAME_COUNT):   # :183-196
      for dst, src in (("Enabled", "enabled"), ("SlaveAddress", "slaveaddress"), ("ControllerPort", "controllerport"),
                       ("ThermalThrottler", "thermalthrottler"), ("I2cProtocol", "i2cprotocol"), ("I2cSpeed", "i2cspeed")):
        setattr(pt.I2cControllers[i], dst, getattr(d.i2ccontrollers[i], src))
    pt.FanTargetTemperature = 105   # override_powerplay_table_fantargettemperature, VEGA20_FAN_TARGET_TEMPERATURE_OVERRIDE (:36, :198-206)

  # GNLD order (vega20_hwmgr.h:49-84) -> smu11_driver_if.h FEATURE_*_BIT (vega20_init_dpm_defaults :331-389; GNLD_DIDT -> FEATURE_GFX_EDC_BIT)
  VEGA20_GNLD = ("DPM_PREFETCHER", "DPM_GFXCLK", "DPM_UCLK", "DPM_SOCCLK", "DPM_UVD", "DPM_VCE", "ULV", "DPM_MP0CLK", "DPM_LINK", "DPM_DCEFCLK",
    "DS_GFXCLK", "DS_SOCCLK", "DS_LCLK", "PPT", "TDC", "THERMAL", "GFX_PER_CU_CG", "RM", "DS_DCEFCLK", "ACDC", "VR0HOT", "VR1HOT", "FW_CTF",
    "LED_DISPLAY", "FAN_CONTROL", "GFX_EDC", "GFXOFF", "CG", "DPM_FCLK", "DS_FCLK", "DS_MP1CLK", "DS_MP0CLK", "XGMI", "ECC")
  # amdgpu_pp_feature_mask default (amdgpu_drv.c:202); PP_*_MASK (amd_shared.h:216-235). Thunderbolt-attached = removable -> PCIe dynamic switching is
  # supported (amdgpu_device.c:1298-1309) so PP_PCIE_DPM_MASK stays (:2087-2088); KFD's default sched policy is HWS, so GFXOFF stays too (:2083-2084).
  VEGA20_PP_FEATURE_MASK = 0xfff7bfff

  def _vega20_feature(self, name:str) -> dict: return self.vega20_features[self.VEGA20_GNLD.index(name)]

  def _vega20_backend_init(self):
    """vega20_hwmgr_backend_init (vega20_hwmgr.c:433-497): vega20_set_default_registry_data (:62-174), vega20_set_features_platform_caps (:176-258;
    only PowerContainment matters for this path and it stays unset, :246-247), vega20_init_dpm_defaults (:331-410)."""
    m, fm = self.smu_mod, self.VEGA20_PP_FEATURE_MASK
    disallowed = 0xE0041C00                                                       # :93-102 (GFXCLK/SOCCLK/LCLK/DCEFCLK/FCLK/MP1CLK/MP0CLK DS)
    self.vega20_smu_version = self._send_msg(m.PPSMC_MSG_GetSmuVersion, 0, read_back_arg=True)   # :104
    if self.vega20_smu_version < 0x282100: disallowed |= m.FEATURE_ECC_MASK                    # :105-106
    for pp_mask, feat_mask in ((0x4, m.FEATURE_DPM_LINK_MASK), (0x1, m.FEATURE_DPM_GFXCLK_MASK), (0x1000, m.FEATURE_DPM_SOCCLK_MASK),
                               (0x2, m.FEATURE_DPM_UCLK_MASK), (0x2000, m.FEATURE_DPM_DCEFCLK_MASK), (0x100, m.FEATURE_ULV_MASK),
                               (0x8, m.FEATURE_DS_GFXCLK_MASK)):              # :108-127
      if not fm & pp_mask: disallowed |= feat_mask
    self.vega20_pcie_dpm_key_disabled = not (fm & 0x4)                           # :173
    self.vega20_fclk_gfxclk_ratio = 0                                            # :165
    self.vega20_caps.discard("PowerContainment")                                 # :246-247 "assume disabled"
    self.vega20_features = [{"id": (fid:=getattr(m, f"FEATURE_{n}_BIT")), "bitmap": 1 << fid, "allowed": not ((disallowed >> i) & 1),
                             "enabled": False, "supported": False} for i, n in enumerate(self.VEGA20_GNLD)]   # :339-396
    top32 = self._send_msg(m.PPSMC_MSG_ReadSerialNumTop32, 0, read_back_arg=True)          # :399-401
    bottom32 = self._send_msg(m.PPSMC_MSG_ReadSerialNumBottom32, 0, read_back_arg=True)    # :403-405
    self.vega20_unique_id = (bottom32 << 32) | top32                                       # :407

  def _vega20_enable_dpm_tasks(self):
    """vega20_enable_dpm_tasks (vega20_hwmgr.c:1693-1774), every step in order. A failing SMU message raises (Linux returns the error and
    hwmgr_hw_init fails, "powerplay hw init failed")."""
    m = self.smu_mod
    self._send_msg(m.PPSMC_MSG_NumOfDisplays, 0)                                           # :1697-1698
    lo = hi = 0                                                                            # vega20_set_allowed_featuresmask :925-956
    for f in self.vega20_features:
      if f["allowed"]:
        if f["id"] > 31: hi |= (f["bitmap"] >> 32) & 0xFFFFFFFF
        else: lo |= f["bitmap"] & 0xFFFFFFFF
    self._send_msg(m.PPSMC_MSG_SetAllowedFeaturesMaskHigh, hi)
    self._send_msg(m.PPSMC_MSG_SetAllowedFeaturesMaskLow, lo)
    self._vega20_init_smc_table()                                                          # :1705-1708
    self._send_msg(m.PPSMC_MSG_RunBtc, 0)                                                  # vega20_run_btc :958-961
    self._send_msg(m.PPSMC_MSG_RunAfllBtc, 0)                                              # vega20_run_btc_afll :963-966
    self._send_msg(m.PPSMC_MSG_EnableAllSmuFeatures, 0)                                    # vega20_enable_all_smu_features :968-1003
    enabled = self.get_features()
    for f in self.vega20_features: f["enabled"] = f["supported"] = bool(enabled & f["bitmap"])
    self._vega20_override_pcie_parameters()                                                # :1725-1728
    if self._vega20_feature("DPM_UCLK")["enabled"]: self._send_msg(m.PPSMC_MSG_SetUclkFastSwitch, 1)   # vega20_notify_smc_display_change :1005-1016
    self._send_msg(m.PPSMC_MSG_SetFclkGfxClkRatio, self.vega20_fclk_gfxclk_ratio)         # vega20_send_clock_ratio :1018-1026
    self.vega20_uvd_power_gated = self.vega20_vce_power_gated = True                       # vega20_init_powergate_state :1684-1691
    self._vega20_setup_default_dpm_tables()                                                # :1742-1745
    self._vega20_init_max_sustainable_clocks()                                             # :1747-1750
    if "PowerContainment" in self.vega20_caps: raise RuntimeError("vega20_power_control_set_level: PowerContainment is never set on Vega 20")  # :1752
    self._vega20_od8_initialize_default_settings()                                         # :1757-1760
    gfx, mem = self.vega20_dpm["gfx"], self.vega20_dpm["mem"]                              # vega20_populate_umdpstate_clocks :1568-1584
    lvl = (3, 2) if len(gfx) > 3 and len(mem) > 2 else (0, 0)                              # VEGA20_UMD_PSTATE_GFXCLK_LEVEL 3 / MCLK_LEVEL 2 (vega20_hwmgr.h:582-584)
    self.vega20_pstate = {"sclk": gfx[lvl[0]], "mclk": mem[lvl[1]], "sclk_peak": gfx[-1], "mclk_peak": mem[-1]}
    self.vega20_default_power_limit = self.vega20_power_limit = self._send_msg(m.PPSMC_MSG_GetPptLimit, m.POWER_SOURCE_AC << 16, read_back_arg=True)  # :1765-1771

  def _vega20_init_smc_table(self):
    """vega20_init_smc_table (vega20_hwmgr.c:790-831): VBIOS boot-up values (pp_atomfwctrl_get_vbios_bootup_values, ppatomfwctrl.c:513-541),
    SetMinDeepSleepDcefclk(dcef_clock / 100), the PPTable upload (TABLE_PPTABLE)."""
    fwi = self.adev.vbios.firmwareinfo()
    b = self.vega20_boot = {"vddc": fwi.bootup_vddc_mv, "vddci": fwi.bootup_vddci_mv, "mvddc": fwi.bootup_mvddc_mv, "gfx": fwi.bootup_sclk_in10khz,
      "mem": fwi.bootup_mclk_in10khz, "soc": 0, "dcef": 0, "e": 0, "v": 0, "d": 0, "f": 0, "cooling_id": fwi.coolingsolution_id}
    v32 = (fwi.table_header.format_revision, fwi.table_header.content_revision) == (3, 2)
    # v3.2: SMU11 clock ids (ppatomfwctrl.c:445-476, atomfirmware.h:3873-3895); v3.1: SMU9 ids, no FCLK (:478-511, atomfirmware.h:3856-3869)
    for key, (clk_id, pll) in ((("soc", (1, 0)), ("dcef", (5, 0)), ("e", (0, 0)), ("v", (4, 0)), ("d", (3, 0)), ("f", (0, 3))) if v32 else
                               (("soc", (1, 0)), ("dcef", (8, 0)), ("e", (7, 0)), ("v", (6, 0)), ("d", (5, 0)))):
      if (f:=self._vega20_atom_clock(clk_id, pll)) is not None: b[key] = f
    self._send_msg(self.smu_mod.PPSMC_MSG_SetMinDeepSleepDcefclk, (b["dcef"] // 100) & 0xFFFFFFFF)   # :818-821
    self.write_table(self.vega20_smc_pptable, self.smu_mod.TABLE_PPTABLE)                         # :823-829

  def _vega20_atom_clock(self, clk_id:int, syspll_id:int) -> int|None:
    """pp_atomfwctrl_get_clk_information_by_clkid (ppatomfwctrl.c:418-441): ATOM command table getsmuclockinfo (master command list index 41,
    offsetof/2 checked with clang) with atom_get_smu_clock_info_parameters_v3_1 {syspll_id, clk_id, command=GET_CLOCK_FREQ 0, dfsdid=0}
    (4 bytes, passed with params_size = sizeof = 4 as Linux does); output dword 0 = smu_clock_freq_hz, returned / 10000. A failing table -> None."""
    ps = [syspll_id | (clk_id << 8), 0, 0, 0]
    if self.adev.atom.execute_table(41, ps, 4): return None
    return (ps[0] & 0xFFFFFFFF) // 10000

  def _vega20_override_pcie_parameters(self):
    """vega20_override_pcie_parameters (vega20_hwmgr.c:840-923) with the platform half of adev->pm.pcie_gen_mask / pcie_mlw_mask
    (amdgpu_device_get_pcie_info, amdgpu_device.c:5911-6090; AMDev._vega20_pcie_platform_caps)."""
    m, pt = self.smu_mod, self.vega20_smc_pptable
    gen_mask, mlw_mask = self.adev._vega20_pcie_platform_caps()
    pcie_gen = next((g for g, bit in ((3, 0x80000), (2, 0x40000), (1, 0x20000), (0, 0x10000)) if gen_mask & bit), 0)   # :853-860, amd_pcie.h:27-30
    pcie_width = next((w for w, bit in ((6, 0x200000), (5, 0x100000), (4, 0x80000), (3, 0x40000), (2, 0x20000), (1, 0x10000)) if mlw_mask & bit), 0)  # :862-873
    for i in range(m.NUM_LINK_LEVELS):   # :878-896
      gen_arg, width_arg = min(pt.PcieGenSpeed[i], pcie_gen), min(pt.PcieLaneCount[i], pcie_width)
      if gen_arg != pt.PcieGenSpeed[i] or width_arg != pt.PcieLaneCount[i]:
        self._send_msg(m.PPSMC_MSG_OverridePcieParameters, (i << 16) | (gen_arg << 8) | width_arg)
      pt.PcieGenSpeed[i], pt.PcieLaneCount[i] = gen_arg, width_arg
    if self.vega20_pcie_dpm_key_disabled:   # :898-920
      for i in range(m.NUM_LINK_LEVELS):
        self._send_msg(m.PPSMC_MSG_OverridePcieParameters, (i << 16) | (pcie_gen << 8) | pcie_width)
        pt.PcieGenSpeed[i], pt.PcieLaneCount[i] = pcie_gen, pcie_width
      link = self._vega20_feature("DPM_LINK")
      self._send_msg(m.PPSMC_MSG_DisableSmuFeaturesLow, link["bitmap"] & 0xFFFFFFFF)       # vega20_enable_smc_features(false) vega20_smumgr.c:317-346
      self._send_msg(m.PPSMC_MSG_DisableSmuFeaturesHigh, (link["bitmap"] >> 32) & 0xFFFFFFFF)
      link["enabled"] = link["supported"] = False

  def _vega20_single_dpm_table(self, clk:int) -> list[int]:
    """vega20_setup_single_dpm_table (vega20_hwmgr.c:568-591): GetDpmFreqByIndex (clk << 16 | 0xFF) = level count, (clk << 16 | i) = level i."""
    n = self._send_msg(self.smu_mod.PPSMC_MSG_GetDpmFreqByIndex, (clk << 16) | 0xFF, read_back_arg=True)
    return [self._send_msg(self.smu_mod.PPSMC_MSG_GetDpmFreqByIndex, (clk << 16) | i, read_back_arg=True) for i in range(n)]

  def _vega20_setup_default_dpm_tables(self):
    """vega20_setup_default_dpm_tables (vega20_hwmgr.c:643-783): per clock the SMU's levels when its DPM feature is enabled, else one level = the
    VBIOS boot clock / 100 (pix/disp/phy: no level). Every soft/hard min/max starts at 0 / VG20_CLOCK_MAX_DEFAULT (vega20_init_dpm_state :528-534)."""
    m, b, f = self.smu_mod, self.vega20_boot, self._vega20_feature
    def one(feat, clk, boot): return self._vega20_single_dpm_table(clk) if f(feat)["enabled"] else [boot // 100]
    def disp(clk): return self._vega20_single_dpm_table(clk) if f("DPM_DCEFCLK")["enabled"] else []
    self.vega20_dpm = {"soc": one("DPM_SOCCLK", m.PPCLK_SOCCLK, b["soc"]), "gfx": one("DPM_GFXCLK", m.PPCLK_GFXCLK, b["gfx"]),
      "mem": one("DPM_UCLK", m.PPCLK_UCLK, b["mem"]), "eclk": one("DPM_VCE", m.PPCLK_ECLK, b["e"]), "vclk": one("DPM_UVD", m.PPCLK_VCLK, b["v"]),
      "dclk": one("DPM_UVD", m.PPCLK_DCLK, b["d"]), "dcef": one("DPM_DCEFCLK", m.PPCLK_DCEFCLK, b["dcef"]), "pixel": disp(m.PPCLK_PIXCLK),
      "display": disp(m.PPCLK_DISPCLK), "phy": disp(m.PPCLK_PHYCLK), "fclk": one("DPM_FCLK", m.PPCLK_FCLK, b["f"])}
    self.vega20_dpm_state = {k: {"soft_min": 0, "soft_max": 0xFFFF, "hard_min": 0, "hard_max": 0xFFFF} for k in self.vega20_dpm}
    self.vega20_golden_dpm = {k: list(v) for k, v in self.vega20_dpm.items()}   # :779-780

  def _vega20_init_max_sustainable_clocks(self):
    """vega20_init_max_sustainable_clocks (vega20_hwmgr.c:1612-1668) with vega20_get_max_sustainable_clock (:1586-1610): GetDcModeMaxDpmFreq, and
    GetMaxDpmFreq when the DC limit is 0."""
    m, b, f = self.smu_mod, self.vega20_boot, self._vega20_feature
    def get(clk):
      v = self._send_msg(m.PPSMC_MSG_GetDcModeMaxDpmFreq, clk << 16, read_back_arg=True)
      return v if v else self._send_msg(m.PPSMC_MSG_GetMaxDpmFreq, clk << 16, read_back_arg=True)
    c = self.vega20_max_sustainable = {"uclock": b["mem"] // 100, "soc_clock": b["soc"] // 100, "dcef_clock": b["dcef"] // 100,
                                        "display_clock": 0xFFFFFFFF, "phy_clock": 0xFFFFFFFF, "pixel_clock": 0xFFFFFFFF}
    if f("DPM_UCLK")["enabled"]: c["uclock"] = get(m.PPCLK_UCLK)
    if f("DPM_SOCCLK")["enabled"]: c["soc_clock"] = get(m.PPCLK_SOCCLK)
    if f("DPM_DCEFCLK")["enabled"]:
      for k, clk in (("dcef_clock", m.PPCLK_DCEFCLK), ("display_clock", m.PPCLK_DISPCLK), ("phy_clock", m.PPCLK_PHYCLK), ("pixel_clock", m.PPCLK_PIXCLK)):
        c[k] = get(clk)
    if c["soc_clock"] < c["uclock"]: c["uclock"] = c["soc_clock"]

  # OD8_SETTING_* (vega20_hwmgr.h:401-418) and OD8 capability bits (:389-398)
  OD8 = ("GFXCLK_FMIN", "GFXCLK_FMAX", "GFXCLK_FREQ1", "GFXCLK_VOLTAGE1", "GFXCLK_FREQ2", "GFXCLK_VOLTAGE2", "GFXCLK_FREQ3", "GFXCLK_VOLTAGE3",
         "UCLK_FMAX", "POWER_PERCENTAGE", "FAN_ACOUSTIC_LIMIT", "FAN_MIN_SPEED", "FAN_TARGET_TEMP", "OPERATING_TEMP_MAX", "AC_TIMING",
         "FAN_ZERO_RPM_CONTROL")
  OD8_CAP = {n: 1 << i for i, n in enumerate(("GFXCLK_LIMITS", "GFXCLK_CURVE", "UCLK_MAX", "POWER_LIMIT", "ACOUSTIC_LIMIT_SCLK", "FAN_SPEED_MIN",
                                               "TEMPERATURE_FAN", "TEMPERATURE_SYSTEM", "MEMORY_TIMING_TUNE", "FAN_ZERO_RPM_CONTROL"))}

  def _vega20_od8_initialize_default_settings(self):
    """vega20_od8_initialize_default_settings (vega20_hwmgr.c:1247-1395) with vega20_od8_set_feature_capabilities (:1044-1136) and
    vega20_od8_set_feature_id (:1138-1225). Linux copies the VBIOS OD8 limits in ATOM_VEGA20_ODSETTING order and indexes them with OD8_SETTING_*
    values; that indexing is kept verbatim. Entries past the VBIOS's ODSettingCount read as 0 (C reads past the kmalloc'ed array)."""
    m, pt, info, f, cap = self.smu_mod, self.vega20_smc_pptable, self.vega20_pptable_info, self._vega20_feature, self.OD8_CAP
    def at(arr, name): return arr[i] if (i:=self.OD8.index(name)) < len(arr) else 0
    fc, smax, smin = info["od_feature_capabilities"], info["od_settings_max"], info["od_settings_min"]
    def afeat(name): i = vbios.ATOM_VEGA20_ODFEATURE.index(name); return fc[i] if i < len(fc) else 0
    caps = 0
    if f("DPM_GFXCLK")["enabled"]:   # :1053-1072
      if afeat("GFXCLK_LIMITS") and at(smax, "GFXCLK_FMAX") > 0 and at(smin, "GFXCLK_FMIN") > 0 and at(smax, "GFXCLK_FMAX") >= at(smin, "GFXCLK_FMIN"):
        caps |= cap["GFXCLK_LIMITS"]
      if (afeat("GFXCLK_CURVE") and at(smin, "GFXCLK_VOLTAGE1") >= pt.MinVoltageGfx // self.VEGA20_VOLTAGE_SCALE and
          at(smax, "GFXCLK_VOLTAGE3") <= pt.MaxVoltageGfx // self.VEGA20_VOLTAGE_SCALE and at(smax, "GFXCLK_VOLTAGE3") >= at(smin, "GFXCLK_VOLTAGE1")):
        caps |= cap["GFXCLK_CURVE"]
    if f("DPM_UCLK")["enabled"]:   # :1074-1084
      i = self.OD8.index("UCLK_FMAX")
      while len(smin) <= i: smin.append(0)
      smin[i] = self.vega20_dpm["mem"][len(self.vega20_dpm["mem"]) - 2]
      if afeat("UCLK_MAX") and at(smin, "UCLK_FMAX") > 0 and at(smax, "UCLK_FMAX") > 0 and at(smax, "UCLK_FMAX") >= at(smin, "UCLK_FMAX"):
        caps |= cap["UCLK_MAX"]
    if (afeat("POWER_LIMIT") and 0 < at(smax, "POWER_PERCENTAGE") <= 100 and 0 < at(smin, "POWER_PERCENTAGE") <= 100):   # :1086-1091
      caps |= cap["POWER_LIMIT"]
    if f("FAN_CONTROL")["enabled"]:   # :1093-1109
      if (afeat("FAN_ACOUSTIC_LIMIT") and at(smin, "FAN_ACOUSTIC_LIMIT") > 0 and at(smax, "FAN_ACOUSTIC_LIMIT") > 0 and
          at(smax, "FAN_ACOUSTIC_LIMIT") >= at(smin, "FAN_ACOUSTIC_LIMIT")): caps |= cap["ACOUSTIC_LIMIT_SCLK"]
      if (afeat("FAN_SPEED_MIN") and at(smin, "FAN_MIN_SPEED") >= (pt.FanPwmMin * pt.FanMaximumRpm // 100) and at(smax, "FAN_MIN_SPEED") > 0 and
          at(smax, "FAN_MIN_SPEED") >= at(smin, "FAN_MIN_SPEED")): caps |= cap["FAN_SPEED_MIN"]
    if f("THERMAL")["enabled"]:   # :1111-1125
      if (afeat("TEMPERATURE_FAN") and at(smax, "FAN_TARGET_TEMP") > 0 and at(smin, "FAN_TARGET_TEMP") > 0 and
          at(smax, "FAN_TARGET_TEMP") >= at(smin, "FAN_TARGET_TEMP")): caps |= cap["TEMPERATURE_FAN"]
      if (afeat("TEMPERATURE_SYSTEM") and at(smax, "OPERATING_TEMP_MAX") > 0 and at(smin, "OPERATING_TEMP_MAX") > 0 and
          at(smax, "OPERATING_TEMP_MAX") >= at(smin, "OPERATING_TEMP_MAX")): caps |= cap["TEMPERATURE_SYSTEM"]
    if afeat("MEMORY_TIMING_TUNE"): caps |= cap["MEMORY_TIMING_TUNE"]                       # :1127-1128
    if afeat("FAN_ZERO_RPM_CONTROL") and pt.FanZeroRpmEnable: caps |= cap["FAN_ZERO_RPM_CONTROL"]   # :1130-1132
    self.vega20_od8_caps = caps
    feature_of = {"GFXCLK_FMIN": "GFXCLK_LIMITS", "GFXCLK_FMAX": "GFXCLK_LIMITS", **{f"GFXCLK_{k}{n}": "GFXCLK_CURVE" for k in ("FREQ", "VOLTAGE")
                  for n in (1, 2, 3)}, "UCLK_FMAX": "UCLK_MAX", "POWER_PERCENTAGE": "POWER_LIMIT", "FAN_ACOUSTIC_LIMIT": "ACOUSTIC_LIMIT_SCLK",
                  "FAN_MIN_SPEED": "FAN_SPEED_MIN", "FAN_TARGET_TEMP": "TEMPERATURE_FAN", "OPERATING_TEMP_MAX": "TEMPERATURE_SYSTEM"}   # :1138-1225
    s = self.vega20_od8 = {n: {"feature_id": (cap[feature_of[n]] if n in feature_of and caps & cap[feature_of[n]] else 0), "default": 0}
                           for n in self.OD8}
    od = self.read_table(m.OverDriveTable_t, m.TABLE_OVERDRIVE)                             # :1263-1267
    if caps & cap["GFXCLK_LIMITS"]: s["GFXCLK_FMIN"]["default"], s["GFXCLK_FMAX"]["default"] = od.GfxclkFmin, od.GfxclkFmax
    if caps & cap["GFXCLK_CURVE"]:   # :1279-1316
      od.GfxclkFreq1 = od.GfxclkFmin; s["GFXCLK_FREQ1"]["default"] = od.GfxclkFreq1
      od.GfxclkFreq3 = od.GfxclkFmax; s["GFXCLK_FREQ3"]["default"] = od.GfxclkFreq3
      od.GfxclkFreq2 = (od.GfxclkFreq1 + od.GfxclkFreq3) // 2; s["GFXCLK_FREQ2"]["default"] = od.GfxclkFreq2
      for n in (1, 2, 3):
        try: v = self._send_msg(m.PPSMC_MSG_GetAVFSVoltageByDpm, (self.VEGA20_AVFS_CURVE << 24) | (self.VEGA20_OD8_HOTCURVE_TEMPERATURE << 16) |
                                getattr(od, f"GfxclkFreq{n}"), read_back_arg=True) // self.VEGA20_VOLTAGE_SCALE   # :1227-1245
        except AM_SMU.ResponseError: v = 0                                                  # PP_ASSERT_WITH_CODE(..., default_value = 0)
        s[f"GFXCLK_VOLTAGE{n}"]["default"] = v
        setattr(od, f"GfxclkVolt{n}", v * self.VEGA20_VOLTAGE_SCALE)
    if caps & cap["UCLK_MAX"]: s["UCLK_FMAX"]["default"] = od.UclkFmax                     # :1333-1338
    if caps & cap["POWER_LIMIT"]: s["POWER_PERCENTAGE"]["default"] = od.OverDrivePct      # :1340-1345
    if caps & cap["ACOUSTIC_LIMIT_SCLK"]: s["FAN_ACOUSTIC_LIMIT"]["default"] = od.FanMaximumRpm   # :1347-1352
    if caps & cap["FAN_SPEED_MIN"]: s["FAN_MIN_SPEED"]["default"] = od.FanMinimumPwm * pt.FanMaximumRpm // 100   # :1354-1359
    if caps & cap["TEMPERATURE_FAN"]: s["FAN_TARGET_TEMP"]["default"] = od.FanTargetTemperature   # :1361-1366
    if caps & cap["TEMPERATURE_SYSTEM"]: s["OPERATING_TEMP_MAX"]["default"] = od.MaxOpTemp     # :1368-1373
    for i, n in enumerate(self.OD8):   # :1375-1388
      on = bool(s[n]["feature_id"])
      s[n].update(min=(smin[i] if on and i < len(smin) else 0), max=(smax[i] if on and i < len(smax) else 0), current=(s[n]["default"] if on else 0))
    self.write_table(od, m.TABLE_OVERDRIVE)                                                 # :1390-1393

  def _vega20_start_thermal_controller(self):
    """phm_start_thermal_controller (hardwaremanager.c:222-260) -> vega20_get_thermal_temperature_range (vega20_hwmgr.c:4267-4294, starts from
    SMU7ThermalWithDelayPolicy[0], pp_thermal.h:28-31) and, with PHM_PlatformCaps_ThermalController, vega20_start_thermal_controller
    (vega20_thermal.c:337-356): set_temperature_range (:241-278), enable_alert (:280-294), setup_fan_table (:322-335)."""
    pt, info = self.vega20_smc_pptable, self.vega20_pptable_info
    self.vega20_thermal_range = {"min": -273150, "max": pt.TedgeLimit * 1000, "edge_emergency_max": (pt.TedgeLimit + 5) * 1000,
      "hotspot_min": -273150, "hotspot_crit_max": pt.ThotspotLimit * 1000, "hotspot_emergency_max": (pt.ThotspotLimit + 5) * 1000,
      "mem_min": -273150, "mem_crit_max": pt.ThbmLimit * 1000, "mem_emergency_max": (pt.ThbmLimit + 5) * 1000,
      "sw_ctf_threshold": info["us_SoftwareShutdownTemp"] * 1000}   # CTF_OFFSET_* 5 (pp_thermal.h:38-40), PP_TEMPERATURE_UNITS_PER_CENTIGRADES 1000
    if "ThermalController" not in self.vega20_caps: return
    low, high = 0, 255   # VEGA20_THERMAL_MINIMUM/MAXIMUM_ALERT_TEMP (vega20_thermal.h:46-47)
    if low < int(self.vega20_thermal_range["min"] / 1000): low = int(self.vega20_thermal_range["min"] / 1000)   # C integer division truncates
    if high > info["us_SoftwareShutdownTemp"]: high = info["us_SoftwareShutdownTemp"]
    if low > high: raise RuntimeError(f"am {self.adev.devfmt}: vega20_thermal_set_temperature_range: low {low} > high {high} (-EINVAL)")
    reg = self.adev.regTHM_THERMAL_INT_CTRL
    val = reg.read() & ~reg.fields_mask("max_ih_credit", "therm_ih_hw_ena", "dig_therm_inth", "dig_therm_intl", "therm_trigger_mask",
                                        "therm_inth_mask", "therm_intl_mask")
    reg.write(val | reg.encode(max_ih_credit=5, therm_ih_hw_ena=1, dig_therm_inth=high, dig_therm_intl=low))
    self.adev.regTHM_THERMAL_INT_ENA.write(self.adev.regTHM_THERMAL_INT_ENA.encode(therm_inth_clr=1, therm_intl_clr=1, therm_trigger_clr=1))
    self._send_msg(self.smu_mod.PPSMC_MSG_SetFanTemperatureTarget, pt.FanTargetTemperature)

  def is_smu_alive(self):
    # GetSmuVersion is 0x2 in smu_11_0_2 as well (vega20_ppsmc.h:38). ResponseError is raised only under the (11,0,2) gate in _send_msg, so
    # suppressing it changes nothing for the other cards.
    with contextlib.suppress(TimeoutError, AM_SMU.ResponseError): self._send_msg(self.smu_mod.PPSMC_MSG_GetSmuVersion, 0, timeout=100)
    return self.adev.mmMP1_SMN_C2PMSG_90.read() != 0

  def mode1_reset(self):
    if self.adev.ip_ver[am.MP0_HWIP] == (11,0,2):
      # vega20_ppsmc.h:37-124 has no Mode1Reset / GfxDriverReset message (GfxDeviceDriverReset 0x3B is commented out, :89); Linux resets Vega 20
      # through PSP (psp_v11_0_mode1_reset, amdgpu/psp_v11_0.c:410-432) — design v2 §3 row AM_SMU "mode1_reset on MP0 (11,0,2) delegates to PSP".
      # Never send an SMU message here; the MP1 messages Linux has are PrepareMp1ForReset 0x59 / ForUnload 0x40 / ForShutdown 0x5A
      # (vega20_set_mp1_state, vega20_hwmgr.c:3165-3191), which prepare MP1, they do not reset.
      if (psp_reset:=getattr(self.adev.psp, "mode1_reset", None)) is None:
        raise RuntimeError(f"am {self.adev.devfmt}: Vega 20 (MP0 11.0.2) mode1 reset is AM_PSP.mode1_reset (psp_v11_0.c:410-432), "
                           "not an SMU message; "
                           "AM_PSP has no mode1_reset in this tree (PSP stage) — do not call AM_SMU.mode1_reset directly")
      if DEBUG >= 2: print(f"am {self.adev.devfmt}: mode1 reset delegated to PSP (MP0 11.0.2)")
      return psp_reset()
    if DEBUG >= 2: print(f"am {self.adev.devfmt}: mode1 reset")
    if self.adev.ip_ver[am.MP0_HWIP] >= (14,0,0) or self.adev.ip_ver[am.MP0_HWIP] in {(13,0,0), (13,0,7), (13,0,10)}:
      self._send_msg(__DEBUGSMC_MSG_Mode1Reset:=2, 0, debug=True)
    elif self.adev.ip_ver[am.MP0_HWIP] in {(13,0,6), (13,0,12), (13,0,15)}: self._send_msg(self.smu_mod.PPSMC_MSG_GfxDriverReset, 1)
    else: self._send_msg(self.smu_mod.PPSMC_MSG_Mode1Reset, 0)

    if not self.adev.is_hive(): time.sleep(0.5) # 500ms

  def read_table(self, table_t, arg):
    if self._vega20(): return self._vega20_read_table(table_t, arg)
    if self.adev.ip_ver[am.MP0_HWIP] in {(13,0,6),(13,0,12),(13,0,15)}: self._send_msg(self.smu_mod.PPSMC_MSG_GetMetricsTable, arg)
    else: self._send_msg(self.smu_mod.PPSMC_MSG_TransferTableSmu2Dram, arg)
    return table_t.from_buffer(bytearray(self.adev.vram.view(self.driver_table_paddr, ctypes.sizeof(table_t))[:]))

  # --- Vega 20 table transfer (vega20_smumgr.c:163-246): each table has its own buffer (vega20_smu_init) ---
  def _vega20_table(self, table_id:int) -> tuple[int, int]:
    # vega20_copy_table_from/to_smc guards (:171-176 / :216-221): id < TABLE_COUNT and a table that vega20_smu_init allocated (version/size != 0).
    # Bits 31:16 of the id carry a workload type for TABLE_ACTIVITY_MONITOR_COEFF (:275, :305); the buffer is the one of bits 15:0.
    if not (0 <= (table_id & 0xffff) < self.smu_mod.TABLE_COUNT) or (table_id & 0xffff) not in self.vega20_tables:
      raise ValueError(f"am {self.adev.devfmt}: Invalid SMU Table ID {table_id:#x}")
    return self.vega20_tables[table_id & 0xffff]

  def _vega20_read_table(self, table_t, table_id:int):
    # vega20_copy_table_from_smc (:163-201): SetDriverDramAddrHigh/Low = this table's buffer (:178-189), TransferTableSmu2Dram (:190-193),
    # amdgpu_hdp_invalidate (:195) = hdp_v4_0_invalidate_hdp (hdp_v4_0.c:39-54: HDP_READ_CACHE_INVALIDATE = 1, read back), memcpy (:197-198).
    paddr, size = self._vega20_table(table_id)
    mc = self.adev.paddr2mc(paddr)
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrHigh, hi32(mc))
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrLow, lo32(mc))
    self._send_msg(self.smu_mod.PPSMC_MSG_TransferTableSmu2Dram, table_id)
    self.adev.regHDP_READ_CACHE_INVALIDATE.write(1)
    self.adev.regHDP_READ_CACHE_INVALIDATE.read()
    return table_t.from_buffer(bytearray(self.adev.vram.view(paddr, size)[:ctypes.sizeof(table_t)]))

  def write_table(self, table, table_id:int):
    # vega20_copy_table_to_smc (:208-246): memcpy into this table's buffer (:223-224), amdgpu_hdp_flush (:226) = amdgpu_hdp_generic_flush
    # (amdgpu_hdp.c:51-60 = AM_GMC.flush_hdp incl. the get_memsize read), SetDriverDramAddrHigh/Low (:228-239), TransferTableDram2Smu (:240-243).
    self._vega20_only("write_table")
    paddr, size = self._vega20_table(table_id)
    self.adev.vram.view(paddr, size)[:] = bytes(table)[:size]
    self.adev.gmc.flush_hdp()
    mc = self.adev.paddr2mc(paddr)
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrHigh, hi32(mc))
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrLow, lo32(mc))
    self._send_msg(self.smu_mod.PPSMC_MSG_TransferTableDram2Smu, table_id)

  # --- Vega 20 telemetry / overdrive (design v2 §6) ---
  def read_metrics(self):
    self._vega20_only("read_metrics")
    # vega20_get_metrics_table vega20_hwmgr.c:2115-2141 (:2126-2129)
    return self.read_table(self.smu_mod.SmuMetrics_t, self.smu_mod.TABLE_SMU_METRICS)

  def read_od(self):
    self._vega20_only("read_od")
    # vega20_hwmgr.c:1264 (init), :1411 (od8_set_settings), :3123-3125 (restore default)
    return self.read_table(self.smu_mod.OverDriveTable_t, self.smu_mod.TABLE_OVERDRIVE)

  def write_od(self, od):
    # vega20_hwmgr.c:1392 (init), :1468 (od8_set_settings), :3132-3134 (PP_OD_COMMIT_DPM_TABLE). After the OD table write Linux sends NO further SMU
    # message: the commit path only re-reads the gfx/mem DPM tables with GetDpmFreqByIndex when GfxclkFmin/Fmax or UclkFmax changed
    # (:3139-3155 -> vega20_setup_gfxclk/memclk_dpm_table :593-642); no SetSoftMin/MaxByFreq, no feature message. Linux's edit model is
    # read-modify-write of the whole table (:1411-1468, :2951-3163) = read_od() -> edit fields -> write_od(). GfxclkVolt* are mV * VOLTAGE_SCALE
    # (:3105 `input_vol * VOLTAGE_SCALE`). The per-field limits (od_settings_min/max, :1376-1379) come from the VBIOS OverDrive8Table and are not
    # available here (design v2 §6) — this method does not range-check; the SMU's own acceptance is the only check (ResponseError on reject).
    self._vega20_only("write_od")
    if not isinstance(od, self.smu_mod.OverDriveTable_t): raise TypeError(f"write_od expects smu_11_0_2.OverDriveTable_t, got {type(od).__name__}")
    self.write_table(od, self.smu_mod.TABLE_OVERDRIVE)

  def get_features(self) -> int:
    # vega20_get_enabled_smc_features (vega20_smumgr.c:349-373): GetEnabledSmuFeaturesLow 0xC, GetEnabledSmuFeaturesHigh 0xD (Linux sends them
    # without a
    # parameter, :358-367; AM's mailbox always writes C2PMSG_82, here 0), result = low | high << 32 (:369-370; SMU_FEATURES_*_SHIFT/MASK
    # vega20_smumgr.h:45-48). "DPM running" = any bit of SMC_DPM_FEATURES 0x30F (vega20_hwmgr.h:91; vega20_is_dpm_running :606-616).
    self._vega20_only("get_features")
    lo = self._send_msg(self.smu_mod.PPSMC_MSG_GetEnabledSmuFeaturesLow, 0, read_back_arg=True)
    hi = self._send_msg(self.smu_mod.PPSMC_MSG_GetEnabledSmuFeaturesHigh, 0, read_back_arg=True)
    return lo | (hi << 32)

  def get_power_limit(self) -> int:
    # vega20_enable_dpm_tasks tail (vega20_hwmgr.c:1765-1771): GetPptLimit 0x53 with POWER_SOURCE_AC << 16, reply = the PPT limit (W).
    self._vega20_only("get_power_limit")
    return self._send_msg(self.smu_mod.PPSMC_MSG_GetPptLimit, self.smu_mod.POWER_SOURCE_AC << 16, read_back_arg=True)

  @functools.cache  # pylint: disable=method-cache-max-size-none
  def read_clocks(self, clk_list:tuple[int]) -> dict[int, list[int]]:
    return {clck: [self._send_msg(self.smu_mod.PPSMC_MSG_GetDpmFreqByIndex, (clck<<16)|i, read_back_arg=True)&0x7fffffff for i in range(cnt)]
      for clck in clk_list if (cnt:=self._send_msg(self.smu_mod.PPSMC_MSG_GetDpmFreqByIndex, (clck<<16)|0xff, read_back_arg=True)&0x7fffffff)}

  def set_clocks(self, level:int|None):
    if self._vega20(): return self._vega20_set_clocks(level)
    clks = tuple([self.smu_mod.PPCLK_UCLK, self.smu_mod.PPCLK_FCLK, self.smu_mod.PPCLK_SOCCLK])
    if self.adev.ip_ver[am.MP0_HWIP] not in {(13,0,6), (13,0,12), (13,0,15)}: clks += (self.smu_mod.PPCLK_GFXCLK,)

    if level is None:
      for clck in clks:
        with contextlib.suppress(TimeoutError): self._send_msg(self.smu_mod.PPSMC_MSG_SetSoftMinByFreq, clck << 16, timeout=20)
        if self.adev.ip_ver[am.GC_HWIP] >= (10,0,0): self._send_msg(self.smu_mod.PPSMC_MSG_SetSoftMaxByFreq, clck << 16 | 0xffff)
      return

    for clck, vals in self.read_clocks(clks).items():
      with contextlib.suppress(TimeoutError): self._send_msg(self.smu_mod.PPSMC_MSG_SetSoftMinByFreq, clck << 16 | (vals[level]), timeout=20)
      if self.adev.ip_ver[am.GC_HWIP] >= (10,0,0): self._send_msg(self.smu_mod.PPSMC_MSG_SetSoftMaxByFreq, clck << 16 | (vals[level]))

  def _vega20_set_clocks(self, level:int|None):
    """AM's set_clocks(level) on Vega 20 = Linux's DPM level forcing on the DPM tables cached at init (vega20_setup_default_dpm_tables):
    None = vega20_unforce_dpm_levels (vega20_hwmgr.c:2477-2530: soft min = lowest, soft max = highest level), -1 = vega20_force_dpm_highest
    (:2392-2432), 0 = vega20_force_dpm_lowest (:2434-2475), any other index = that single level (vega20_force_clock_level with a one-level mask).
    Levels are all enabled after setup_single_dpm_table, so find_lowest/highest = first/last (:1776-1818). Then vega20_upload_dpm_min_level and
    vega20_upload_dpm_max_level (:1820-2009) with FEATURE_DPM_GFXCLK|UCLK|SOCCLK: per clock only while its DPM feature is enabled."""
    m = self.smu_mod
    for key in ("gfx", "mem", "soc"):
      lv = self.vega20_dpm[key]
      lo, hi = (lv[0], lv[-1]) if level is None else (lv[level], lv[level])
      self.vega20_dpm_state[key]["soft_min"], self.vega20_dpm_state[key]["soft_max"] = lo, hi
    order = (("DPM_GFXCLK", m.PPCLK_GFXCLK, "gfx"), ("DPM_UCLK", m.PPCLK_UCLK, "mem"), ("DPM_SOCCLK", m.PPCLK_SOCCLK, "soc"))
    for feat, clk, key in order:   # upload_dpm_min_level order: GFXCLK, UCLK, (UVD, VCE not in the mask), SOCCLK
      if self._vega20_feature(feat)["enabled"]:
        self._send_msg(m.PPSMC_MSG_SetSoftMinByFreq, (clk << 16) | (self.vega20_dpm_state[key]["soft_min"] & 0xffff))
    for feat, clk, key in order:   # upload_dpm_max_level, same order
      if self._vega20_feature(feat)["enabled"]:
        self._send_msg(m.PPSMC_MSG_SetSoftMaxByFreq, (clk << 16) | (self.vega20_dpm_state[key]["soft_max"] & 0xffff))
    if DEBUG >= 2: print(f"am {self.adev.devfmt}: Vega 20 DPM soft min/max: { {k: self.vega20_dpm_state[k] for k in ('gfx', 'mem', 'soc')} }")

  def set_power_limit(self, watts:float):
    if self._vega20(): return self._vega20_set_power_limit(watts)   # mi50 fork: same one-line gate pattern as set_clocks above
    ppt_limit = max(int(round(watts)), 1)
    self._send_msg(self.smu_mod.PPSMC_MSG_SetPptLimit, ppt_limit)
    if DEBUG >= 2: print(f"am {self.adev.devfmt}: GPU power limit set to {ppt_limit}W")

  def _vega20_set_power_limit(self, watts:float):
    # vega20_set_power_limit (vega20_powertune.c:32-43): SetPptLimit only while the PPT feature is enabled, else nothing (return 0).
    ppt_limit = max(int(round(watts)), 1)
    if self._vega20_feature("PPT")["enabled"]:
      self._send_msg(self.smu_mod.PPSMC_MSG_SetPptLimit, ppt_limit)
      self.vega20_power_limit = ppt_limit
    if DEBUG >= 2: print(f"am {self.adev.devfmt}: GPU power limit {'set to ' + str(ppt_limit) + 'W' if self._vega20_feature('PPT')['enabled'] else 'not set: PPT feature off'}")

  def _aca_read_reg(self, bank_idx:int, reg_idx:int, ue=True) -> int:
    msg = self.smu_mod.PPSMC_MSG_McaBankDumpDW if ue else self.smu_mod.PPSMC_MSG_McaBankCeDumpDW
    return (self._send_msg(msg, (bank_idx << 16) | (reg_idx * 8 + 4), read_back_arg=True) << 32) | \
            self._send_msg(msg, (bank_idx << 16) | (reg_idx * 8), read_back_arg=True)

  def _aca_read_banks(self, ue=True) -> list[list[int]]:
    if not hasattr(self.smu_mod, 'PPSMC_MSG_QueryValidMcaCount'): return []
    count_msg = self.smu_mod.PPSMC_MSG_QueryValidMcaCount if ue else self.smu_mod.PPSMC_MSG_QueryValidMcaCeCount
    return [[self._aca_read_reg(idx, reg_idx, ue=ue) for reg_idx in range(16)] for idx in range(self._send_msg(count_msg, 0, read_back_arg=True))]

  def _smu_cmn_send_msg(self, msg:int, param=0, debug=False):
    (self.adev.mmMP1_SMN_C2PMSG_90 if not debug else self.adev.mmMP1_SMN_C2PMSG_54).write(0) # resp reg
    (self.adev.mmMP1_SMN_C2PMSG_82 if not debug else self.adev.mmMP1_SMN_C2PMSG_53).write(param)
    (self.adev.mmMP1_SMN_C2PMSG_66 if not debug else self.adev.mmMP1_SMN_C2PMSG_75).write(msg)

  def _send_msg(self, msg:int, param:int, read_back_arg=False, timeout=10000, debug=False): # default timeout is 10 seconds
    if self._vega20(): return self._vega20_send_msg(msg, param, read_back_arg=read_back_arg, timeout=timeout, debug=debug)
    self._smu_cmn_send_msg(msg, param, debug=debug)
    wait_cond((self.adev.mmMP1_SMN_C2PMSG_90 if not debug else self.adev.mmMP1_SMN_C2PMSG_54).read, value=1, timeout_ms=timeout,
      msg=f"SMU msg {msg:#x} timeout")
    return (self.adev.mmMP1_SMN_C2PMSG_82 if not debug else self.adev.mmMP1_SMN_C2PMSG_53).read() if read_back_arg else None

  def _vega20_send_msg(self, msg:int, param:int, read_back_arg=False, timeout=10000, debug=False):
    # vega20_send_msg_to_smc_with_parameter (vega20_smumgr.c:130-149): wait for the previous response (:136 = vega20_wait_for_response :70-81, i.e.
    # phm_wait_for_register_unequal(C2PMSG_90, 0) smu_helper.c:153-175 with usec_timeout = AMD_MAX_USEC_TIMEOUT = 1000 ms (hwmgr.c:85,
    # amd_shared.h:30),
    # its result discarded), C2PMSG_90 = 0 (:138), C2PMSG_82 = param (:140), C2PMSG_66 = msg (:142 -> :89-97), wait C2PMSG_90 != 0 (:144), require
    # PPSMC_Result_OK else error (:145-148; codes vega20_ppsmc.h:30-34), argument back from C2PMSG_82 (:151-156). The MP1 13.x "debug" mailbox
    # (C2PMSG_53/54/75, used by mode1_reset above) does not exist in vega20_smumgr.c.
    if debug: raise RuntimeError(f"am {self.adev.devfmt}: Vega 20 has no debug SMU mailbox (vega20_smumgr.c uses C2PMSG_66/82/90 only)")
    resp = self.adev.mmMP1_SMN_C2PMSG_90
    with contextlib.suppress(TimeoutError):
      wait_cond(lambda: resp.read() != 0, value=True, timeout_ms=1000, msg=f"SMU previous response pending before msg {msg:#x}")
    self._smu_cmn_send_msg(msg, param)
    wait_cond(lambda: resp.read() != 0, value=True, timeout_ms=timeout, msg=f"SMU msg {msg:#x} timeout")
    if (r:=resp.read()) != self.smu_mod.PPSMC_Result_OK:
      raise AM_SMU.ResponseError(f"am {self.adev.devfmt}: SMU msg {msg:#x} param {param:#x} -> "
                                 f"{self._vega20_results.get(r, 'unknown response')} ({r:#x})")
    return self.adev.mmMP1_SMN_C2PMSG_82.read() if read_back_arg else None

class AM_GFX(AM_IP):
  # mi50 fork (Vega20 Port Design v2 B14.4): the three gfx9 golden tables Linux applies for GC 9.4.0, in Linux order
  # (gfx_v9_0_init_golden_registers: golden_settings_gc_9_0 + golden_settings_gc_9_0_vg20, gfx_v9_0.c:1115-1122, then
  # golden_settings_gc_9_x_common :1154-1157). Entries are (register, and_mask, or_mask) exactly as SOC15_REG_GOLDEN_VALUE lists them;
  # applied with the soc15_program_register_sequence semantics (soc15.c:463-475): raw write when and_mask == 0xffffffff,
  # else (read & ~and_mask) | (or_mask & and_mask). The GB_ADDR_CONFIG entry is the (read & ~0xf3e777ff) | 0x22014042 of :2053-2055.
  GOLDEN_GC_9_0 = [                                                # gfx_v9_0.c:676-698
    ("regDB_DEBUG2", 0xf00fffff, 0x00000400),                      # :678
    ("regDB_DEBUG3", 0x80000000, 0x80000000),                      # :679
    ("regGB_GPU_ID", 0x0000000f, 0x00000000),                      # :680
    ("regPA_SC_BINNER_EVENT_CNTL_3", 0x00000003, 0x82400024),      # :681
    ("regPA_SC_ENHANCE", 0x3fffffff, 0x00000001),                  # :682
    ("regPA_SC_LINE_STIPPLE_STATE", 0x0000ff0f, 0x00000000),       # :683
    ("regSH_MEM_CONFIG", 0x00001000, 0x00001000),                  # :684
    ("regSPI_RESOURCE_RESERVE_CU_0", 0x0007ffff, 0x00000800),      # :685
    ("regSPI_RESOURCE_RESERVE_CU_1", 0x0007ffff, 0x00000800),      # :686
    ("regSPI_RESOURCE_RESERVE_EN_CU_0", 0x01ffffff, 0x00ffff87),   # :687
    ("regSPI_RESOURCE_RESERVE_EN_CU_1", 0x01ffffff, 0x00ffff8f),   # :688
    ("regSQC_CONFIG", 0x03000000, 0x020a2000),                     # :689
    ("regTA_CNTL_AUX", 0xfffffeef, 0x010b0000),                    # :690
    ("regTCP_CHAN_STEER_HI", 0xffffffff, 0x4a2c0e68),              # :691
    ("regTCP_CHAN_STEER_LO", 0xffffffff, 0xb5d3f197),              # :692
    ("regVGT_CACHE_INVALIDATION", 0x3fff3af3, 0x19200000),         # :693
    ("regVGT_GS_MAX_WAVE_ID", 0x00000fff, 0x000003ff),             # :694
    ("regCP_MEC1_F32_INT_DIS", 0x00000800, 0x00000800),            # :695
    ("regCP_MEC2_F32_INT_DIS", 0x00000800, 0x00000800),            # :696
    ("regCP_DEBUG", 0x00008000, 0x00008000)]                       # :697
  GOLDEN_GC_9_0_VG20 = [                                           # gfx_v9_0.c:722-735
    ("regCB_DCC_CONFIG", 0x0f000080, 0x04000080),                  # :724
    ("regCB_HW_CONTROL_2", 0x0f000000, 0x0a000000),                # :725
    ("regCB_HW_CONTROL_3", 0x30000000, 0x10000000),                # :726
    ("regGB_ADDR_CONFIG", 0xf3e777ff, 0x22014042),                 # :727
    ("regGB_ADDR_CONFIG_READ", 0xf3e777ff, 0x22014042),            # :728
    ("regDB_DEBUG2", 0x00003e00, 0x00000400),                      # :729
    ("regPA_SC_ENHANCE_1", 0xff840000, 0x04040000),                # :730
    ("regRMI_UTCL1_CNTL2", 0x00030000, 0x00030000),                # :731
    ("regSPI_CONFIG_CNTL_1", 0xffff010f, 0x01000107),              # :732
    ("regTA_CNTL_AUX", 0x000b0000, 0x000b0000),                    # :733
    ("regTD_CNTL", 0x01000000, 0x01000000)]                        # :734
  GOLDEN_GC_9_X_COMMON = [                                         # gfx_v9_0.c:815-820
    ("regCP_SD_CNTL", 0xffffffff, 0x000001ff),                     # :817
    ("regGRBM_CAM_INDEX", 0xffffffff, 0x00000000),                 # :818
    ("regGRBM_CAM_DATA", 0xffffffff, 0x2544c382)]                  # :819

  def init_sw(self):
    self.xccs = sum(1 for i in self.adev.regs_offset[am.GC_HWIP] if i not in self.adev.harvested[am.GC_HWIP])
    self.mqd_paddr = [self.adev.mm.palloc(0x1000 * self.xccs, zero=False, boot=True) for i in range(2)]
    self.mqd_mc = [self.adev.paddr2mc(mqd_paddr) for mqd_paddr in self.mqd_paddr]

    if self.adev.ip_ver[am.GC_HWIP] == (9,4,0):
      # mi50 fork (design v2 B14.6): the RLC clear-state buffer Linux builds in amdgpu_gfx_rlc_init_csb (amdgpu_rlc.c:128-148: get_csb_size dwords,
      # dws*4 bytes, PAGE_SIZE aligned) and fills in gfx_v9_0_init_csb (gfx_v9_0.c:2775). Boot memory like the MQDs; MC address like mqd_mc.
      self.csb_dws = self._csb_gfx9()
      self.csb_paddr = self.adev.mm.palloc((len(self.csb_dws) * 4 + 0xfff) & ~0xfff, boot=True)
      self.csb_mc = self.adev.paddr2mc(self.csb_paddr)
      # is_rlc_v2_1 = RLC header 2.1 (amdgpu_rlc.c:565-566). AMFirmware keeps no copy of the header, so re-read the cached, hash-checked file.
      hdr = self.adev.fw.load_fw("vega20_rlc.bin", am.struct_rlc_firmware_header_v2_0)[1].header
      self.rlc_v2_1 = (hdr.header_version_major, hdr.header_version_minor) == (2, 1)

  def init_hw(self):
    if self.adev.ip_ver[am.GC_HWIP] == (9,4,0): return self._init_hw_gc_9_4_0() # mi50 fork: Vega 20 takes the Linux gfx_v9_0 sequence (design v2 B14)

    # Wait for RLC autoload to complete
    wait_cond(lambda: self.adev.regCP_STAT.read() == 0 or self.adev.regRLC_RLCS_BOOTLOAD_STATUS.read_bitfields()['bootload_complete'] == 0,
              value=True, msg="RLC autoload timeout")

    self.adev.gmc.init_hub("GC", insts=range(self.xccs))
    if self.adev.partial_boot: return self.reset_mec()

    self._config_mec()

    # NOTE: Golden reg for gfx11. No values for this reg provided. The kernel just ors 0x20000000 to this reg.
    for xcc in range(self.xccs): self.adev.regTCP_CNTL.write(self.adev.regTCP_CNTL.read() | 0x20000000, inst=xcc)

    for xcc in range(self.xccs): self.adev.regRLC_CNTL.write(0x1, inst=xcc)

    for xcc in range(self.xccs): self.adev.regRLC_SRM_CNTL.update(srm_enable=1, auto_incr_addr=1, inst=xcc)

    for xcc in range(self.xccs): self.adev.regRLC_SPM_MC_CNTL.write(0xf, inst=xcc)

    if self.adev.ip_ver[am.NBIO_HWIP][:2] != (7,9):
      self.adev.soc.doorbell_enable(port=0, awid=0x3, awaddr_31_28_value=0x3)
      self.adev.soc.doorbell_enable(port=3, awid=0x6, awaddr_31_28_value=0x3)

    for xcc in range(self.xccs):
      if self.adev.ip_ver[am.GC_HWIP] in {(9,4,3), (9,5,0)}:
        self.adev.regGB_ADDR_CONFIG.write(0x2a114042, inst=xcc) # Golden value for mi300/mi350
        self.adev.regTCP_UTCL1_CNTL2.update(spare=1, inst=xcc)

      self.adev.regGRBM_CNTL.update(read_timeout=0xff, inst=xcc)
      for i in range(0, 16):
        self._grbm_select(vmid=i, inst=xcc)
        self.adev.regSH_MEM_CONFIG.write(**({'initial_inst_prefetch':3} if self.adev.ip_ver[am.GC_HWIP][0]>=10 else {'retry_disable':1}),
          **({'f8_mode':1} if self.adev.ip_ver[am.GC_HWIP][:2]==(9,4) else {}),
          address_mode=self.adev.soc.module.SH_MEM_ADDRESS_MODE_64, alignment_mode=self.adev.soc.module.SH_MEM_ALIGNMENT_MODE_UNALIGNED, inst=xcc)

        # Configure apertures:
        # LDS:         0x10000000'00000000 - 0x10000001'00000000 (4GB)
        # Scratch:     0x20000000'00000000 - 0x20000001'00000000 (4GB)
        self.adev.regSH_MEM_BASES.write(shared_base=0x1, private_base=0x2, inst=xcc)
      self._grbm_select(inst=xcc)

      # Configure MEC doorbell range
      self.adev.regCP_MEC_DOORBELL_RANGE_LOWER.write(0x100 * xcc, inst=xcc)
      self.adev.regCP_MEC_DOORBELL_RANGE_UPPER.write(0x100 * xcc + 0xf8, inst=xcc)

    self._enable_mec()

    # Set 1 partition
    if self.xccs > 1: self.adev.psp._spatial_partition_cmd(1)

  # ---- mi50 fork: Vega 20 (GC 9.4.0) bring-up, ported from Linux gfx_v9_0.c (design v2 B14, rule R-LINUX). Every value/order cites its line. ----
  def _init_hw_gc_9_4_0(self):
    # B14.1: gfx9 has no RLC autoload and regRLC_RLCS_BOOTLOAD_STATUS is absent from gc_9_0 (T4); RLC/CP firmware is PSP-loaded
    # (gfx_v9_0.c:3212-3217 and :3954-3965 skip the legacy microcode loads when load_type == PSP). Wait for CP_STAT == 0 alone.
    wait_cond(lambda: self.adev.regCP_STAT.read() == 0, value=True, msg="CP_STAT busy")
    self.adev.gmc.init_hub("GC", insts=range(self.xccs)) # B14.2
    if self.adev.partial_boot: return self.reset_mec()

    # gfx_v9_0_hw_init (gfx_v9_0.c:4027-4048): amdgpu_gfx_cleaner_shader_init :4032-4033 (skipped, B14.3), init_golden_registers :4035-4036,
    # constants_init :4038, init_tcp_config :4040 (returns unless 9.4.1/9.4.2, :4006-4008), rlc resume :4042, cp_resume :4046.
    self._golden_gfx9()
    self._constants_init_gfx9()
    self._rlc_resume_gfx9()
    self._cp_resume_gfx9()
    # ip.py's S2A doorbell_enable calls (nbio_7_9 excluded path) are not issued: nbio_7_4 has no S2A registers (design B14 "no S2A doorbell calls").
    # ip.py's TCP_CNTL |= 0x20000000 is a gfx11 golden value and is not applied (B14.4).

  def _golden_gfx9(self):
    for table in (self.GOLDEN_GC_9_0, self.GOLDEN_GC_9_0_VG20, self.GOLDEN_GC_9_X_COMMON):
      for name, and_mask, or_mask in table:
        reg = self.adev.reg(name)
        reg.write(or_mask if and_mask == 0xffffffff else (reg.read() & ~and_mask) | (or_mask & and_mask)) # soc15.c:467-475

  def _select_se_sh_gfx9(self, se:int, sh:int, instance:int=0xffffffff):
    # gfx_v9_0_select_se_sh (gfx_v9_0.c:2503-2524): 0xffffffff selects broadcast for that field, else the index.
    kw = {'instance_broadcast_writes':1} if instance == 0xffffffff else {'instance_index':instance}
    kw |= {'se_broadcast_writes':1} if se == 0xffffffff else {'se_index':se}
    kw |= {'sh_broadcast_writes':1} if sh == 0xffffffff else {'sh_index':sh}
    self.adev.regGRBM_GFX_INDEX.write(**kw)

  def _get_cu_info_gfx9(self):
    # gfx_v9_0_get_cu_info (gfx_v9_0.c:7892-7960). SE/SH/CU counts come from the synthesized gc_info v2 (design v2 B4/B17); no card value is guessed.
    gi = self.adev.gc_info
    try: self.gfx9_num_se, self.gfx9_num_sh, self.gfx9_num_cu = gi.gc_num_se, gi.gc_num_sh_per_se, gi.gc_num_cu_per_sh
    except AttributeError as e:
      raise RuntimeError("Vega 20 gfx needs gc_info fields gc_num_se/gc_num_sh_per_se/gc_num_cu_per_sh (design v2 B4)") from e
    if self.gfx9_num_se * self.gfx9_num_sh > 16: raise RuntimeError(f"gfx9 cu_info: {self.gfx9_num_se}*{self.gfx9_num_sh} > 16") # :7905-7907
    self.gfx9_cu_bitmap:dict[tuple[int,int], int] = {}
    for se in range(self.gfx9_num_se):
      for sh in range(self.gfx9_num_sh):
        self._select_se_sh_gfx9(se, sh)                                                                                 # :7919
        # :7920-7921 set_user_cu_inactive_bitmap(disable_masks[..]): the mask is 0 unless the amdgpu.disable_cu module parameter is given
        # (amdgpu_gfx.c:119-122, amdgpu_drv.c:184 default NULL), and :7868-7869 then returns without writing GC_USER_SHADER_ARRAY_CONFIG.
        data = self.adev.regCC_GC_SHADER_ARRAY_CONFIG.read() | self.adev.regGC_USER_SHADER_ARRAY_CONFIG.read()            # :7881-7882
        inactive = self.adev.regCC_GC_SHADER_ARRAY_CONFIG.decode(data)['inactive_cus']                                   # :7884-7885
        # :7887-7889, amdgpu_gfx.h:585-588
        self.gfx9_cu_bitmap[(se, sh)] = (~inactive) & ((1 << self.gfx9_num_cu) - 1)
    self._select_se_sh_gfx9(0xffffffff, 0xffffffff)                                                                     # :7952

  def _constants_init_gfx9(self):
    # gfx_v9_0_constants_init (gfx_v9_0.c:2665-2715)
    self.adev.regGRBM_CNTL.update(read_timeout=0xff)   # :2672
    # :2675 tiling_mode_table_init is a TODO no-op (:2498-2501); :2677-2678 setup_rb only with gfx rings (none here)
    self._get_cu_info_gfx9()                           # :2679
    self.adev.regDB_DEBUG2.read()                      # :2680 (stored in gfx.config; not used here)
    unaligned, mode64 = self.adev.soc.module.SH_MEM_ALIGNMENT_MODE_UNALIGNED, self.adev.soc.module.SH_MEM_ADDRESS_MODE_64
    # :2685-2707 vmid 0 .. num_ids-1; num_ids = first_kfd_vmid for GC < 10 (amdgpu_ids.c:652-655) = 8 for 9.4.0 (gmc_v9_0.c:2033-2039).
    # RETRY_DISABLE = gmc.noretry, default true for 9.4.0 (amdgpu_gmc.c:1004-1005,1019; amdgpu_noretry -1, amdgpu_drv.c:228).
    for i in range(8):
      self._grbm_select(vmid=i)                                                                                      # :2686
      self.adev.regSH_MEM_CONFIG.write(address_mode=mode64, alignment_mode=unaligned, retry_disable=1)             # :2689-2693, :2696-2700
      # :2694 writes SH_MEM_BASES 0 for vmid 0, which never runs user shaders in Linux. AM dispatches on vmid 0 (cp_hqd_vmid=0, setup_ring),
      # so design v2 B14.5 keeps AM's apertures (ip.py shared_base=0x1, private_base=0x2) for vmid 0 and takes Linux's values for 1..15.
      if i == 0: self.adev.regSH_MEM_BASES.write(shared_base=0x1, private_base=0x2)
      # :2701-2705; gmc_v9_0.c:1605,1608
      else: self.adev.regSH_MEM_BASES.write(private_base=0x1000000000000000 >> 48, shared_base=0x2000000000000000 >> 48)
    self._grbm_select()                                                                                              # :2708
    # init_compute_vmid (:2590-2626): vmid first_kfd_vmid(8)..15, DEFAULT_SH_MEM_BASES 0x6000 (:2589, :2602); SH_MEM_CONFIG without RETRY_DISABLE
    # (:2604-2606)
    for i in range(8, 16):
      self._grbm_select(vmid=i)                                                                                      # :2610
      self.adev.regSH_MEM_CONFIG.write(address_mode=mode64, alignment_mode=unaligned)                                # :2612
      self.adev.regSH_MEM_BASES.write(0x6000 | (0x6000 << 16))                                                       # :2613
    self._grbm_select()                                                                                              # :2615
    for i in range(8, 16): self._gds_zero_gfx9(i)                                                                    # :2620-2625
    for i in range(1, 16): self._gds_zero_gfx9(i)                                                                    # init_gds_vmid :2628-2644
    # :2714 init_sq_config: default branch for 9.4.0 (:2650-2662), nothing

  def _gds_zero_gfx9(self, vmid:int):
    # WREG32_SOC15_OFFSET(GDS_VMID0_BASE, 2*vmid) etc. (gfx_v9_0.c:2621-2624): the per-vmid registers exist by name in gc_9_0
    # (GDS_VMID{n}_BASE/SIZE at +2n, GWS/OA at +n).
    for name in (f"regGDS_VMID{vmid}_BASE", f"regGDS_VMID{vmid}_SIZE", f"regGDS_GWS_VMID{vmid}", f"regGDS_OA_VMID{vmid}"):
      self.adev.reg(name).write(0)

  def _gui_idle_int_gfx9(self, enable:bool):
    # gfx_v9_0_enable_gui_idle_interrupt (gfx_v9_0.c:2755-2771) on CP_INT_CNTL_RING0; GFX_IDLE_INT_ENABLE only with gfx rings (:2767-2768), none here.
    self.adev.regCP_INT_CNTL_RING0.update(cntx_busy_int_enable=int(enable), cntx_empty_int_enable=int(enable), cmp_busy_int_enable=int(enable))

  def _wait_rlc_serdes_gfx9(self):
    # gfx_v9_0_wait_for_rlc_serdes (gfx_v9_0.c:2717-2753); usec_timeout = 100000 us (amdgpu.h:274, amdgpu_device.c:3783)
    for se in range(self.gfx9_num_se):
      for sh in range(self.gfx9_num_sh):
        self._select_se_sh_gfx9(se, sh)                                                                                 # :2725
        try: wait_cond(lambda: self.adev.regRLC_SERDES_CU_MASTER_BUSY.read(), value=0, timeout_ms=100, msg="RLC serdes CU busy")  # :2726-2730
        except TimeoutError:
          self._select_se_sh_gfx9(0xffffffff, 0xffffffff)                                                               # :2732-2733
          if DEBUG >= 2: print(f"am {self.adev.devfmt}: Timeout wait for RLC serdes {se},{sh}")                        # :2735-2736
          return                                                                                                        # :2737
    self._select_se_sh_gfx9(0xffffffff, 0xffffffff)                                                                     # :2741
    # :2744-2747
    mask = self.adev.regRLC_SERDES_NONCU_MASTER_BUSY.fields_mask('se_master_busy', 'gc_master_busy', 'tc0_master_busy', 'tc1_master_busy')
    # :2748-2752
    try: wait_cond(lambda: self.adev.regRLC_SERDES_NONCU_MASTER_BUSY.read() & mask, value=0, timeout_ms=100, msg="RLC serdes non-CU busy")
    except TimeoutError:
      if DEBUG >= 2: print(f"am {self.adev.devfmt}: Timeout wait for RLC non-CU serdes")                               # Linux falls through silently

  def _csb_gfx9(self) -> list[int]:
    # gfx_v9_0_get_csb_buffer (gfx_v9_0.c:1652-1664) = amdgpu_gfx_csb_preamble_start + csb_data_parser + preamble_end (amdgpu_gfx.c:2847-2905)
    # over gfx9_cs_data (clearstate_gfx9.h via the generated clearstate_gfx9 module). get_csb_size (:1624-1650) returns 0 for a non-SECT_CONTEXT
    # section.
    from tinygrad.runtime.autogen.am import pm4_soc15 as pm4, clearstate_gfx9 as cs
    buf = [pm4.PACKET3(pm4.PACKET3_PREAMBLE_CNTL, 0), pm4.PACKET3_PREAMBLE_BEGIN_CLEAR_STATE,                          # amdgpu_gfx.c:2851-2852
           pm4.PACKET3(pm4.PACKET3_CONTEXT_CONTROL, 1), 0x80000000, 0x80000000]                                        # :2854-2856
    for sect_id, extents in cs.gfx9_cs_data:                                                                           # :2877-2887
      if sect_id != cs.SECT_CONTEXT: raise RuntimeError(f"clearstate_gfx9 section {sect_id} is not SECT_CONTEXT (gfx_v9_0.c:1637-1640)")
      for reg_count, reg_index, values in extents:
        buf += [pm4.PACKET3(pm4.PACKET3_SET_CONTEXT_REG, reg_count), reg_index - pm4.PACKET3_SET_CONTEXT_REG_START, *values] # :2880-2884
    buf += [pm4.PACKET3(pm4.PACKET3_PREAMBLE_CNTL, 0), pm4.PACKET3_PREAMBLE_END_CLEAR_STATE, pm4.PACKET3(pm4.PACKET3_CLEAR_STATE, 0), 0] # :2900-2904
    return buf

  def _init_lbpw_gfx9_4(self):
    # gfx_v9_4_init_lbpw (gfx_v9_0.c:1759-1806), run for IP_VERSION(9,4,0) at :3228-3229
    self.adev.regRLC_LB_THR_CONFIG_1.write(0x0000007F)                                                                 # :1764
    self.adev.regRLC_LB_THR_CONFIG_2.write(0x033388F8)                                                                 # :1765
    self.adev.regRLC_LB_THR_CONFIG_3.write(0x00000077)                                                                 # :1766
    self.adev.regRLC_LB_THR_CONFIG_4.write(0x10 | 0x27 << 8 | 0x02FA << 16)                                            # :1767
    self.adev.regRLC_LB_CNTR_INIT.write(0x00000000)                                                                    # :1770
    # :1773 (the comment says 0x500, the code writes 0x800)
    self.adev.regRLC_LB_CNTR_MAX.write(0x00000800)
    self._select_se_sh_gfx9(0xffffffff, 0xffffffff)                                                                    # :1777
    self.adev.regRLC_LB_INIT_CU_MASK.write(0xffffffff)                                                                 # :1778
    self.adev.regRLC_LB_PARAMS.write(fifo_samples=0x0003, pg_idle_samples=0x0010, pg_idle_sample_interval=0x033F)      # :1781-1784
    self.adev.regRLC_GPM_GENERAL_7.write((self.adev.regRLC_GPM_GENERAL_7.read() & 0x0000FFFF) | 0x00C00000)            # :1787-1790
    self.adev.regRLC_LB_CNTL.write(lb_cnt_spim_active=1, cu_mask_used_off_hyst=0x09, reserved=0x80000)                 # :1799-1802
    # gfx_v9_0_init_always_on_cu_mask (:1666-1708) at :1805: dGPU, not 9.2.1 -> always_on_cu_num 12 (:1679), pg_always_on_cu_num 2 (:1669)
    for se in range(self.gfx9_num_se):
      for sh in range(self.gfx9_num_sh):
        mask, cu_bitmap, counter = 1, 0, 0
        self._select_se_sh_gfx9(se, sh)                                                                                # :1687
        for _ in range(self.gfx9_num_cu):                                                                              # :1689
          if self.gfx9_cu_bitmap[(se, sh)] & mask:                                                                     # :1690
            if counter == 2: self.adev.regRLC_PG_ALWAYS_ON_CU_MASK.write(cu_bitmap)                                    # :1691-1692
            if counter < 12: cu_bitmap |= mask                                                                         # :1693-1694
            else: break                                                                                                # :1696
            counter += 1
          mask <<= 1
        self.adev.regRLC_LB_ALWAYS_ACTIVE_CU_MASK.write(cu_bitmap)                                                     # :1702
    self._select_se_sh_gfx9(0xffffffff, 0xffffffff)                                                                    # :1706

  def _rlc_resume_gfx9(self):
    import array
    # gfx_v9_0_rlc_resume (gfx_v9_0.c:3196-3244), non-SR-IOV path. rlc_stop (:3205 -> :3124-3129):
    self.adev.regRLC_CNTL.update(rlc_enable_f32=0)                                                                     # :3126
    self._gui_idle_int_gfx9(False)                                                                                     # :3127
    self._wait_rlc_serdes_gfx9()                                                                                       # :3128
    self.adev.regRLC_CGCG_CGLS_CTRL.write(0)                                                                           # :3208 disable CG
    # init_pg (:3210 -> :3096-3122): init_csb (:3098 -> :2773-2783)
    # :2775 get_csb_buffer into cs_ptr
    self.adev.vram.view(self.csb_paddr, len(self.csb_dws) * 4, fmt='I')[:] = array.array('I', self.csb_dws)
    self.adev.regRLC_CSIB_ADDR_HI.write(self.csb_mc >> 32)                                                             # :2777-2778
    self.adev.regRLC_CSIB_ADDR_LO.write(self.csb_mc & 0xfffffffc)                                                      # :2779-2780
    # :2781-2782, clear_state_size in dwords (amdgpu_rlc.c:134)
    self.adev.regRLC_CSIB_LENGTH.write(len(self.csb_dws))
    # :3104-3110: with a v2.1 RLC header enable_save_restore_machine (:2925-2928); the save/restore list init at :3105-3108 is 9.2.1/Raven2 only
    if self.rlc_v2_1: self.adev.regRLC_SRM_CNTL.update(srm_enable=1)                                                   # :2927
    # :3112-3121 skipped: pg_flags == 0 for 9.4.0 (soc15.c:1019). :3212-3217 skipped: RLC firmware is PSP-loaded.
    # :3228-3233 IP_VERSION(9,4,0): gfx_v9_4_init_lbpw, then enable_lbpw(amdgpu_lbpw > 0) with the default amdgpu_lbpw = -1 (amdgpu_drv.c:204) ->
    # false
    self._init_lbpw_gfx9_4()                                                                                           # :3229
    self.adev.regRLC_LB_CNTL.update(load_balance_enable=0)                                                             # :3233 -> :1808-1811
    # :3239 -> update_spm_vmid_internal :5208-5226
    self.adev.regRLC_SPM_MC_CNTL.update(rlc_spm_vmid=0xf)
    # rlc_start (:3241 -> :3139-3152)
    self.adev.regRLC_CNTL.update(rlc_enable_f32=1)                                                                     # :3145
    time.sleep(50e-6)                                                                                                  # :3146 udelay(50)
    self._gui_idle_int_gfx9(True)                                                                                      # :3149-3150 (not an APU)
    time.sleep(50e-6)                                                                                                  # :3151 udelay(50)

  def _cp_gfx_enable_gfx9(self, enable:bool):
    # gfx_v9_0_cp_gfx_enable (gfx_v9_0.c:3246-3264): all twelve CP_ME_CNTL fields set to !enable, then udelay(50)
    v = 0 if enable else 1
    self.adev.regCP_ME_CNTL.update(ce_invalidate_icache=v, pfp_invalidate_icache=v, me_invalidate_icache=v, ce_pipe0_reset=v, ce_pipe1_reset=v,
      pfp_pipe0_reset=v, pfp_pipe1_reset=v, me_pipe0_reset=v, me_pipe1_reset=v, me_halt=v, pfp_halt=v, ce_halt=v)      # :3250-3262
    time.sleep(50e-6)                                                                                                  # :3263

  def _cp_compute_enable_gfx9(self, enable:bool):
    # gfx_v9_0_cp_compute_enable (gfx_v9_0.c:3462-3480): CP_MEC_CNTL = 0, or the full disable mask (all ME1 pipes 0..3, ME2 pipes 0..1), then
    # udelay(50)
    if enable: self.adev.regCP_MEC_CNTL.write(0)                                                                       # :3465
    else: self.adev.regCP_MEC_CNTL.write(mec_invalidate_icache=1, mec_me1_pipe0_reset=1, mec_me1_pipe1_reset=1, mec_me1_pipe2_reset=1,
      mec_me1_pipe3_reset=1, mec_me2_pipe0_reset=1, mec_me2_pipe1_reset=1, mec_me1_halt=1, mec_me2_halt=1)              # :3467-3476
    time.sleep(50e-6)                                                                                                  # :3479

  def _cp_resume_gfx9(self):
    # gfx_v9_0_cp_resume (gfx_v9_0.c:3946-4000)
    self._gui_idle_int_gfx9(False)                                                                                     # :3951-3952
    # :3954-3965 skipped: CP firmware is PSP-loaded
    # :3967-3968 (Linux has a gfx ring on Vega 20; AM never brings it up, B14.7)
    self._cp_gfx_enable_gfx9(False)
    self._cp_compute_enable_gfx9(False)                                                                                # :3969
    # :3971 kiq_resume and :3975-3979 cp_gfx_resume are not used (no KIQ, no gfx ring: B14.7). kcq_resume (:3981 -> :3931-3944) =
    # cp_compute_enable(true)
    # then the MQDs, which the compute side programs through setup_ring; amdgpu_gfx_enable_kcq (:3943) is a KIQ MAP_QUEUES, AM loads HQDs directly
    # (B14.9).
    # B14.8: MEC doorbell range = the kiq_init_register values (:3759-3772): LOWER (kiq 0 * 2) << 2, UPPER (userqueue_end 0x8A * 2) << 2 = 0x450
    # (vega20_reg_init.c:60,70; amdgpu_doorbell.h:122,134); check_if_enlarge_doorbell_range is 9.3.0-only (:1409-1417). Doorbell 3 (MEC_RING0) lies
    # inside.
    self.adev.regCP_MEC_DOORBELL_RANGE_LOWER.write((am.AMDGPU_VEGA20_DOORBELL_KIQ * 2) << 2)                            # :3760-3761
    self.adev.regCP_MEC_DOORBELL_RANGE_UPPER.write((am.AMDGPU_VEGA20_DOORBELL_USERQUEUE_END * 2) << 2)                  # :3771-3772
    # :3935 -> :3465 CP_MEC_CNTL = 0 (+ AM's 50 ms, ip.py)
    self._enable_mec()
    self._gui_idle_int_gfx9(True)                                                                                      # :3997

  def fini_hw(self): self._dequeue_hqds()

  def reset_mec(self):
    self._dequeue_hqds()

    if self.adev.ip_ver[am.GC_HWIP] < (12,0,0): # gfx12+ uses mec_pipe0_reset
      for xcc in range(self.xccs): self.adev.regGRBM_SOFT_RESET.write(soft_reset_cp=1, soft_reset_cpc=1, inst=xcc)
      time.sleep(0.05)
      for xcc in range(self.xccs): self.adev.regGRBM_SOFT_RESET.write(0x0, inst=xcc)

    self._config_mec()
    self._enable_mec()

  def setup_ring(self, ring_addr:int, ring_size:int, rptr_addr:int, wptr_addr:int, eop_addr:int, eop_size:int, idx:int, aql:bool) -> int:
    pipe, queue, doorbell = idx // 4, idx % 4, am.AMDGPU_NAVI10_DOORBELL_MEC_RING0
    vega20 = self.adev.ip_ver[am.GC_HWIP] == (9,4,0) # mi50 fork, design v2 B14.9: Linux gfx_v9_0_mqd_init / kiq_init_register deltas

    for xcc in range(self.xccs if aql else 1):
      self._grbm_select(me=1, pipe=pipe, queue=queue, inst=xcc)

      struct_t = getattr(am, f"struct_v{self.adev.ip_ver[am.GC_HWIP][0]}{'_compute' if self.adev.ip_ver[am.GC_HWIP][0] >= 10 else ''}_mqd")
      mqd_struct = struct_t(header=0xC0310800, cp_mqd_base_addr_lo=lo32(self.mqd_mc[queue] + 0x1000*xcc),
        cp_mqd_base_addr_hi=hi32(self.mqd_mc[queue] + 0x1000*xcc), cp_hqd_pipe_priority=0x2, cp_hqd_queue_priority=0xf, cp_hqd_quantum=0x111,
        # gfx_v9_0.c:3660-3662: PRELOAD_SIZE 0x53, no PRELOAD_REQ. Review 2026-10-13 L8: design B14.9 takes these two deltas from gfx_v9_0_mqd_init
        # (kernel queues, registers written one by one :3686-3797) while AM's HQD load below is the KFD window copy (amdgpu_amdkfd_gfx_v9.c:236-242,
        # :293-294) whose MQD source for gfx906 sets PRELOAD_REQ|0x53, UNORD_DISPATCH=1, hq_status0 = 1<<14 (kfd_mqd_manager_v9.c:193-199, :211);
        # rptr_block_size=5 / priv_state=1 below are AM's stock (= the KFD values). Mixed sources as the design specifies; U-G3 / T9 decide.
        cp_hqd_persistent_state=self.adev.regCP_HQD_PERSISTENT_STATE.encode(preload_size=0x53) if vega20 else
                                self.adev.regCP_HQD_PERSISTENT_STATE.encode(preload_size=0x55, preload_req=1),
        cp_hqd_pq_base_lo=lo32(ring_addr>>8), cp_hqd_pq_base_hi=hi32(ring_addr>>8),
        cp_hqd_pq_rptr_report_addr_lo=lo32(rptr_addr), cp_hqd_pq_rptr_report_addr_hi=hi32(rptr_addr),
        cp_hqd_pq_wptr_poll_addr_lo=lo32(wptr_addr), cp_hqd_pq_wptr_poll_addr_hi=hi32(wptr_addr),
        cp_hqd_pq_doorbell_control=self.adev.regCP_HQD_PQ_DOORBELL_CONTROL.encode(doorbell_offset=doorbell*2, doorbell_en=1),
        cp_hqd_pq_control=self.adev.regCP_HQD_PQ_CONTROL.encode(rptr_block_size=5, unord_dispatch=0, queue_size=(ring_size//4).bit_length()-2,
          **({'queue_full_en':1, 'slot_based_wptr':2, 'no_update_rptr':xcc!=0 or self.xccs==1} if aql else {})),
        cp_hqd_ib_control=self.adev.regCP_HQD_IB_CONTROL.encode(min_ib_avail_size=0x3),
        **({} if vega20 else {'cp_hqd_hq_status0':0x20004000}), # not in gfx_v9_0_mqd_init (:3550-3684)
        cp_mqd_control=self.adev.regCP_MQD_CONTROL.encode(priv_state=1), cp_hqd_vmid=0, cp_hqd_aql_control=int(aql),
        cp_hqd_eop_base_addr_lo=lo32(eop_addr>>8), cp_hqd_eop_base_addr_hi=hi32(eop_addr>>8),
        cp_hqd_eop_control=self.adev.regCP_HQD_EOP_CONTROL.encode(eop_size=(eop_size//4).bit_length()-2),
        **({'compute_tg_chunk_size':1, 'compute_current_logic_xcc_id':xcc, 'cp_mqd_stride_size':0x1000} if aql and self.xccs > 1 else {}))
      for se in range(8 if self.adev.ip_ver[am.GC_HWIP][0] >= 10 else 4): setattr(mqd_struct, f'compute_static_thread_mgmt_se{se}', 0xffffffff)

      self.adev.vram.view(self.mqd_paddr[queue] + 0x1000*xcc, ctypes.sizeof(mqd_struct))[:] = memoryview(mqd_struct).cast('B')

      if vega20: self.adev.regCP_PQ_WPTR_POLL_CNTL.update(en=0, inst=xcc) # gfx_v9_0.c:3693 disable wptr polling before the HQD load
      mqd_st_mv = to_mv(ctypes.addressof(mqd_struct), ctypes.sizeof(mqd_struct)).cast('I')
      for i, reg in enumerate(range(self.adev.regCP_MQD_BASE_ADDR.addr[xcc], self.adev.regCP_HQD_PQ_WPTR_HI.addr[xcc] + 1)):
        self.adev.wreg(reg, mqd_st_mv[0x80 + i])
      self.adev.regCP_HQD_ACTIVE.write(0x1, inst=xcc)
      if vega20 and hasattr(self.adev, 'regCP_PQ_STATUS'): self.adev.regCP_PQ_STATUS.update(doorbell_enable=1, inst=xcc) # gfx_v9_0.c:3794-3795

      self.adev.gmc.flush_hdp()
      self._grbm_select(inst=xcc)
    return doorbell

  def set_clockgating_state(self):
    # mi50 fork, design v2 B16: no-op on 9.4.0 (gc_9_0 lacks the fgcg fields written below; the Linux clock-gating port is U-G4, after T12)
    if self.adev.ip_ver[am.GC_HWIP] == (9,4,0): return
    if hasattr(self.adev, 'regMM_ATC_L2_MISC_CG'): self.adev.regMM_ATC_L2_MISC_CG.write(enable=1, mem_ls_enable=1)

    for xcc in range(self.xccs):
      self.adev.regRLC_SAFE_MODE.write(message=1, cmd=1, inst=xcc)
      wait_cond(lambda: self.adev.regRLC_SAFE_MODE.read(inst=xcc) & 0x1, value=0, msg="RLC safe mode timeout")

      self.adev.regRLC_CGCG_CGLS_CTRL.update(cgcg_gfx_idle_threshold=0x36, cgcg_en=1, cgls_rep_compansat_delay=0xf, cgls_en=1, inst=xcc)

      self.adev.regCP_RB_WPTR_POLL_CNTL.update(poll_frequency=0x100, idle_poll_count=0x90, inst=xcc)
      self.adev.regCP_INT_CNTL.update(cntx_busy_int_enable=1, cntx_empty_int_enable=1, cmp_busy_int_enable=1, inst=xcc)
      if self.adev.ip_ver[am.GC_HWIP] >= (10,0,0):
        self.adev.regSDMA0_RLC_CGCG_CTRL.update(cgcg_int_enable=1, inst=xcc)
        self.adev.regSDMA1_RLC_CGCG_CTRL.update(cgcg_int_enable=1, inst=xcc)

      feats_gfx9 = {'gfxip_mgls_override':0, 'gfxip_rep_fgcg_override':0} if self.adev.ip_ver[am.GC_HWIP][0] == 9 else {}
      feats_gfx11 = {'perfmon_clock_state':1, 'gfxip_repeater_fgcg_override':0} if self.adev.ip_ver[am.GC_HWIP][0] >= 11 else {}
      self.adev.regRLC_CGTT_MGCG_OVERRIDE.update(**feats_gfx9, **feats_gfx11, gfxip_fgcg_override=0, grbm_cgtt_sclk_override=0,
        rlc_cgtt_sclk_override=0, gfxip_mgcg_override=0, gfxip_cgls_override=0, gfxip_cgcg_override=0, inst=xcc)

      self.adev.regRLC_SAFE_MODE.write(message=0, cmd=1, inst=xcc)

  def _grbm_select(self, me=0, pipe=0, queue=0, vmid=0, inst=0):
    self.adev.regGRBM_GFX_CNTL.write(meid=me, pipeid=pipe, vmid=vmid, queueid=queue, inst=inst)

  def _enable_mec(self):
    for xcc in range(self.xccs):
      if self.adev.ip_ver[am.GC_HWIP] >= (10,0,0): self.adev.regCP_MEC_RS64_CNTL.update(mec_pipe0_reset=0, mec_pipe0_active=1, mec_halt=0, inst=xcc)
      else: self.adev.regCP_MEC_CNTL.write(0x0, inst=xcc)
    time.sleep(0.05)  # Wait for MEC to be ready

  def _config_mec(self):
    def _config_helper(eng_name, cntl_reg, eng_reg, pipe_cnt, me=0, xcc=0):
      for pipe in range(pipe_cnt):
        self._grbm_select(me=me, pipe=pipe, inst=xcc)
        self.adev.wreg_pair(f"regCP_{eng_reg}_PRGRM_CNTR_START", "", "_HI", self.adev.fw.ucode_start[eng_name] >> 2, inst=xcc)
      self._grbm_select(inst=xcc)
      self.adev.reg(f"regCP_{cntl_reg}_CNTL").update(**{f"{eng_name.lower()}_pipe{pipe}_reset": 1 for pipe in range(pipe_cnt)}, inst=xcc)
      self.adev.reg(f"regCP_{cntl_reg}_CNTL").update(**{f"{eng_name.lower()}_pipe{pipe}_reset": 0 for pipe in range(pipe_cnt)}, inst=xcc)

    for xcc in range(self.adev.gfx.xccs):
      # mi50 fork (design v2 B14.7): Vega 20 uses the full Linux cp_compute_enable(false) mask (gfx_v9_0.c:3467-3476)
      if self.adev.ip_ver[am.GC_HWIP] == (9,4,0): self._cp_compute_enable_gfx9(False)
      elif self.adev.ip_ver[am.GC_HWIP] < (10,0,0):
        self.adev.regCP_MEC_CNTL.update(mec_invalidate_icache=1, mec_me1_pipe0_reset=1, mec_me2_pipe0_reset=1, mec_me1_halt=1,mec_me2_halt=1,inst=xcc)
      if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0):
        _config_helper(eng_name="PFP", cntl_reg="ME", eng_reg="PFP", pipe_cnt=1, xcc=xcc)
        _config_helper(eng_name="ME", cntl_reg="ME", eng_reg="ME", pipe_cnt=1, xcc=xcc)
      if self.adev.ip_ver[am.GC_HWIP] >= (10,0,0):
        _config_helper(eng_name="MEC", cntl_reg="MEC_RS64", eng_reg="MEC_RS64", pipe_cnt=1, me=1, xcc=xcc)

  def _dequeue_hqds(self):
    for q in range(2):
      for xcc in range(self.xccs):
        self._grbm_select(me=1, pipe=0, queue=q, inst=xcc)
        if self.adev.regCP_HQD_ACTIVE.read(inst=xcc) & 1:
          self.adev.regCP_HQD_DEQUEUE_REQUEST.write(0x2, inst=xcc) # 1 - DRAIN_PIPE; 2 - RESET_WAVES
          self.adev.regSPI_COMPUTE_QUEUE_RESET.write(0x1, inst=xcc)
          if not self.adev.is_err_state:
            try: wait_cond(lambda: self.adev.regCP_HQD_ACTIVE.read(inst=xcc) & 1, value=0, msg="HQD dequeue timeout")
            # kernel tolerates this too; a wedged wave can survive RESET_WAVES
            except TimeoutError:
              if DEBUG >= 2: print(f"am {self.adev.devfmt}: HQD dequeue timeout xcc{xcc} q{q}, continuing")
    self._grbm_select()

class AM_IH(AM_IP):
  def init_sw(self):
    self.ring_size = 256 << 10
    def _alloc_ring(size): return (self.adev.mm.palloc(size, zero=False, boot=True), self.adev.mm.palloc(0x1000, zero=False, boot=True))
    self.rings = [(*_alloc_ring(self.ring_size), "", 0), (*_alloc_ring(self.ring_size), "_RING1", 1)]
    self.ring_view = self.adev.vram.view(offset=self.rings[0][0], size=self.ring_size, fmt='I')

    # mi50 fork (design v2 B11): nbio_v7_4_ih_control writes INTERRUPT_CNTL2 = adev->dummy_page_addr >> 8 (nbio_v7_4.c:295); Linux's dummy page is a
    # DMA-mapped HOST page (amdgpu_gart.c:76-86 dma_map_page_attrs), so one host page is taken from the same alloc_sysmem AM_PSP uses (ip.py AM_PSP
    # init_sw, usb path). The view is kept on self so the mapping stays alive for the GPU's dummy reads.
    if self.adev.ip_ver[am.OSSSYS_HWIP] == (4,2,0):
      self.dummy_rd_view, paddrs = self.adev.pci_dev.alloc_sysmem(0x1000)
      self.dummy_rd_paddr = paddrs[0]
      # INTERRUPT_CNTL2.IH_DUMMY_RD_ADDR is 32 bits of (addr >> 8) (nbio_7_4 table field (0,31)): the page must be 4 KiB aligned and below 2^40
      if self.dummy_rd_paddr & 0xfff or (self.dummy_rd_paddr >> 8) >= (1 << 32):
        raise RuntimeError(f"IH dummy-read page paddr {self.dummy_rd_paddr:#x} does not fit INTERRUPT_CNTL2 (needs 4 KiB alignment and < 2^40)")

  def init_hw(self):
    osssys_4_2_0 = self.adev.ip_ver[am.OSSSYS_HWIP] == (4,2,0)   # mi50 fork: vega20_ih_irq_init is the model (vega20_ih.c:307-388, design v2 B11)
    if osssys_4_2_0:
      # vega20_ih_irq_init: rings off (:315, AM's full IH_RB_CNTL writes below carry rb_enable=0), then nbio ih_control (:319 ->
      # nbio_v7_4.c:290-304): INTERRUPT_CNTL2 = dummy_page_addr >> 8 (:295); INTERRUPT_CNTL read, IH_DUMMY_RD_OVERRIDE=0 ("dummy read disabled
      # with msi, enabled without msi"), IH_REQ_NONSNOOP_EN=0, write (:296-303). Programmed before any ring is enabled.
      self.adev.regINTERRUPT_CNTL2.write(self.dummy_rd_paddr >> 8)
      self.adev.regINTERRUPT_CNTL.update(ih_dummy_rd_override=0, ih_req_nonsnoop_en=0)

    for ring_vm, rwptr_vm, suf, ring_id in self.rings:
      self.adev.wreg_pair("regIH_RB_BASE", suf, f"_HI{suf}", self.adev.paddr2mc(ring_vm) >> 8)

      # mi50 fork: vega20_ih_enable_ring sets ring 0's RPTR_REARM = !!adev->irq.msi_enabled (vega20_ih.c:258-259); AM polls the ring with no MSI
      # (no irq_poller on APLRemotePCIDevice, design v2 B11) -> 0 on OSSSYS 4.2.0; other cards keep AM's 1
      ring0_flags = {'wptr_overflow_enable': 1, 'rptr_rearm': 0 if osssys_4_2_0 else 1}
      # review 2026-10-13 L7: vega20_ih_rb_cntl (vega20_ih.c:194-215) sets WPTR_OVERFLOW_ENABLE=1 (:202-203) and WPTR_WRITEBACK_ENABLE=1 (:208-209)
      # on EVERY ring through the IH_RB_CNTL field masks. Ring 0 has both fields in the osssys_4_2_0 table; the IH_RB_CNTL_RING1 table lacks
      # WPTR_WRITEBACK_ENABLE (bit 8), so ring 1 gets bit 8 raw (same bit, same mask Linux applies). Inert for AM (it polls IH_RB_WPTR by MMIO).
      v420 = ({'wptr_writeback_enable': 1} if ring_id == 0 else {'wptr_overflow_enable': 1}) if osssys_4_2_0 else {}
      self.adev.reg(f"regIH_RB_CNTL{suf}").write((1 << 8) if (osssys_4_2_0 and ring_id != 0) else 0, mc_space=4, wptr_overflow_clear=1,
        rb_size=((self.ring_size//4)-1).bit_length(), mc_snoop=1, mc_ro=0, mc_vmid=0, **v420, **(ring0_flags if ring_id == 0 else {'rb_full_drain_enable': 1}))

      if ring_id == 0: self.adev.wreg_pair("regIH_RB_WPTR_ADDR", "_LO", "_HI", self.adev.paddr2mc(rwptr_vm))

      self.adev.reg(f"regIH_RB_WPTR{suf}").write(0)
      self.adev.reg(f"regIH_RB_RPTR{suf}").write(0)

      # OSSSYS 4.2.0 deviation (review L7, listed): Linux enables the rptr doorbell with doorbell_index (vega20_ih_doorbell_rptr :221-227, use_doorbell
      # :588); AM rings no IH doorbell (rptr written by MMIO in drain), so ENABLE=0 = Linux's !use_doorbell form (:228-231) on every card.
      self.adev.reg(f"regIH_DOORBELL_RPTR{suf}").write(enable=0)

    if self.adev.ip_ver[am.OSSSYS_HWIP] != (4,4,2):
      self.adev.regIH_STORM_CLIENT_LIST_CNTL.update(client18_is_storm_client=1)
      self.adev.regIH_INT_FLOOD_CNTL.update(flood_cntl_enable=1)
      # mi50 fork: no IH_MSI_STORM_CTRL in osssys_4_2_0_offset.h (design v2 B11); the BIF_IH_DOORBELL_RANGE write lives in AM_SOC; the retry CAM
      # (vega20_ih.c:362-377 IH_DOORBELL_RETRY_CAM / IH_RETRY_INT_CAM_CNTL) is skipped: AM runs noretry (SH_MEM_CONFIG.retry_disable=1)
      if not osssys_4_2_0: self.adev.regIH_MSI_STORM_CTRL.update(delay=3)

    # toggle interrupts
    for _, rwptr_vm, suf, ring_id in self.rings:
      self.adev.reg(f"regIH_RB_CNTL{suf}").update(rb_enable=1, **({'enable_intr': 1} if ring_id == 0 else {}))

  def drain(self):
    _, _, suf, _ = self.rings[0]
    wptr = self.adev.reg(f"regIH_RB_WPTR{suf}").read_bitfields()
    self.adev.regIH_RB_RPTR.write(wptr['offset'] % (self.ring_size // 4))

    if wptr['rb_overflow']:
      self.adev.reg(f"regIH_RB_WPTR{suf}").update(rb_overflow=0)
      self.adev.reg(f"regIH_RB_CNTL{suf}").update(wptr_overflow_clear=1)
      self.adev.reg(f"regIH_RB_CNTL{suf}").update(wptr_overflow_clear=0)

  def interrupt_handler(self):
    _, _, suf, _ = self.rings[0]
    wptr = self.adev.reg(f"regIH_RB_WPTR{suf}").read_bitfields()
    rptr = self.adev.regIH_RB_RPTR.read()

    while rptr != wptr['offset']:
      entry = [self.ring_view[(rptr + i) % (self.ring_size // 4)] for i in range(8)]
      rptr = (rptr + 8) % (self.ring_size // 4)

      client, src, ring_id, vmid, vmid_type, pasid, node = \
        [getattr(am, f'SOC15_{n}_FROM_IH_ENTRY')(entry) for n in ['CLIENT_ID', 'SOURCE_ID', 'RING_ID', 'VMID', 'VMID_TYPE', 'PASID', 'NODEID']]
      ctx = [getattr(am, f'SOC15_CONTEXT_ID{i}_FROM_IH_ENTRY')(entry) for i in range(4)]

      src_name = self.adev.soc.ih_srcs_names.get(client, {}).get(src, '')
      if src_name in {"SDMA_TRAP", "CP_EOP_INTR"}: continue

      print(f"am {self.adev.devfmt}: IH ({rptr:#x}/{wptr['offset']:#x}) client={self.adev.soc.ih_clients.get(client)} src={src_name}({src}) "
            f"ring={ring_id} vmid={vmid}({vmid_type}) pasid={pasid} node={node} ctx=[{ctx[0]:#x}, {ctx[1]:#x}, {ctx[2]:#x}, {ctx[3]:#x}]")

      if src_name == "SQ_INTERRUPT_ID":
        enc_type = getbits(ctx[1], 6, 7) if (is_soc21:=self.adev.ip_ver[am.GC_HWIP][0] >= 11) else getbits(ctx[0], 26, 27)
        err_type = getbits(ctx[0], 21, 24) if is_soc21 else getbits((ctx[0] & 0xfff) | ((ctx[0]>>16) & 0xf000) | ((ctx[1]<<16) & 0xff0000), 20, 23)
        err_info = f" ({['EDC_FUE', 'ILLEGAL_INST', 'MEMVIOL', 'EDC_FED'][err_type]})" if enc_type == 2 else ""
        print(f"am {self.adev.devfmt}: sq_intr: {['auto', 'wave', 'error'][enc_type]}{err_info}")
        self.adev.is_err_state |= enc_type == 2
      elif src_name == "UTCL2_FAULT" or (self.adev.ip_ver[am.GC_HWIP][0] == 9 and client == am.SOC15_IH_CLIENTID_UTCL2):
        bf = self.adev.reg(self.adev.gmc.pf_status_reg('GC')).read_bitfields()
        va = (self.adev.reg('regGCVM_L2_PROTECTION_FAULT_ADDR_HI32').read()<<32) | self.adev.reg('regGCVM_L2_PROTECTION_FAULT_ADDR_LO32').read()
        print(f"am {self.adev.devfmt}: GCVM_L2_PROTECTION_FAULT_STATUS: {bf} {va<<12:#x}")
        self.adev.reg('regGCVM_L2_PROTECTION_FAULT_CNTL').update(clear_protection_fault_status_addr=1)
        self.adev.is_err_state = True
      else: self.adev.is_err_state = True

    self.drain()

    bif_intr = self.adev.regBIF_BX0_BIF_DOORBELL_INT_CNTL.read_bitfields()
    athub_err, cntlr_err = bif_intr['ras_athub_err_event_interrupt_status'], bif_intr['ras_cntlr_interrupt_status']
    if athub_err or cntlr_err:
      print(f"am {self.adev.devfmt}: fatal hardware error detected: {'RAS_ATHUB_ERR_EVENT ' if athub_err else ''}{'RAS_CNTLR' if cntlr_err else ''}")

      acas = self.adev.smu._aca_read_banks(ue=True) + self.adev.smu._aca_read_banks(ue=False)
      for regs in acas:
        acatyp = 'Uncorrectable' if (regs[1] >> 61) & 1 and (regs[1] >> 57) & 1 else 'Correctable'
        hwname = f'{self.adev.hwid_names.get((regs[5] >> 32) & 0xFFF, "")} ({(regs[5] >> 32) & 0xFFF:#03x})'
        print(f"am {self.adev.devfmt}: {acatyp} ACA: {hwname} mcatype={(regs[5] >> 48) & 0xFFFF:#06x} regs=[{', '.join(f'{r:#x}' for r in regs)}]")

      self.adev.regBIF_BX0_BIF_DOORBELL_INT_CNTL.write(ras_cntlr_interrupt_clear=cntlr_err, ras_athub_err_event_interrupt_clear=athub_err)
      self.adev.is_err_state = True

class AM_SDMA(AM_IP):
  # mi50 fork (Vega20 Port Design v2 B15): Linux sdma_v4_0.c golden tables for SDMA 4.2.0, in Linux order (sdma_v4_0_init_golden_registers
  # IP_VERSION(4,2,0): golden_settings_sdma0_4_2_init, golden_settings_sdma0_4_2, golden_settings_sdma1_4_2, sdma_v4_0.c:535-545), applied with the
  # soc15_program_register_sequence semantics (soc15.c:463-475). The fork's sdma_4_2 table normalizes SDMA0_*/SDMA1_* to SDMA_* with the instance
  # selecting the base, as sdma_v4_0_get_reg_offset does (:440-460); entries are (register, and_mask, or_mask) exactly as SOC15_REG_GOLDEN_VALUE
  # lists them.
  GOLDEN_SDMA0_4_2_INIT = [                                                # sdma_v4_0.c:197-199, instance 0
    ("regSDMA_RLC0_RB_WPTR_POLL_CNTL", 0xfffffff0, 0x00403000)]            # :198
  GOLDEN_SDMA0_4_2 = [                                                     # sdma_v4_0.c:201-230, instance 0
    ("regSDMA_CHICKEN_BITS", 0xfe931f07, 0x02831f07),                      # :203
    ("regSDMA_CLK_CTRL", 0xffffffff, 0x3f000100),                          # :204
    ("regSDMA_GB_ADDR_CONFIG", 0x0000773f, 0x00004002),                    # :205
    ("regSDMA_GB_ADDR_CONFIG_READ", 0x0000773f, 0x00004002),               # :206
    ("regSDMA_GFX_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),               # :207
    ("regSDMA_GFX_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),             # :208
    ("regSDMA_PAGE_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :209
    ("regSDMA_PAGE_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :210
    ("regSDMA_RD_BURST_CNTL", 0x0000000f, 0x00000003),                     # :211
    ("regSDMA_RLC0_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :212
    ("regSDMA_RLC0_RB_WPTR_POLL_CNTL", 0xfffffff0, 0x00403000),            # :213
    ("regSDMA_RLC1_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :214
    ("regSDMA_RLC1_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :215
    ("regSDMA_RLC2_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :216
    ("regSDMA_RLC2_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :217
    ("regSDMA_RLC3_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :218
    ("regSDMA_RLC3_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :219
    ("regSDMA_RLC4_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :220
    ("regSDMA_RLC4_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :221
    ("regSDMA_RLC5_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :222
    ("regSDMA_RLC5_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :223
    ("regSDMA_RLC6_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :224
    ("regSDMA_RLC6_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :225
    ("regSDMA_RLC7_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :226
    ("regSDMA_RLC7_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :227
    ("regSDMA_UTCL1_PAGE", 0x000003ff, 0x000003c0),                        # :228
    ("regSDMA_UTCL1_TIMEOUT", 0xffffffff, 0x00010001)]                     # :229
  GOLDEN_SDMA1_4_2 = [                                                     # sdma_v4_0.c:232-260, instance 1 (mmSDMA1_* in Linux)
    ("regSDMA_CHICKEN_BITS", 0xfe931f07, 0x02831f07),                      # :233
    ("regSDMA_CLK_CTRL", 0xffffffff, 0x3f000100),                          # :234
    ("regSDMA_GB_ADDR_CONFIG", 0x0000773f, 0x00004002),                    # :235
    ("regSDMA_GB_ADDR_CONFIG_READ", 0x0000773f, 0x00004002),               # :236
    ("regSDMA_GFX_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),               # :237
    ("regSDMA_GFX_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),             # :238
    ("regSDMA_PAGE_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :239
    ("regSDMA_PAGE_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :240
    ("regSDMA_RD_BURST_CNTL", 0x0000000f, 0x00000003),                     # :241
    ("regSDMA_RLC0_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :242
    ("regSDMA_RLC0_RB_WPTR_POLL_CNTL", 0xfffffff0, 0x00403000),            # :243
    ("regSDMA_RLC1_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :244
    ("regSDMA_RLC1_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :245
    ("regSDMA_RLC2_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :246
    ("regSDMA_RLC2_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :247
    ("regSDMA_RLC3_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :248
    ("regSDMA_RLC3_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :249
    ("regSDMA_RLC4_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :250
    ("regSDMA_RLC4_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :251
    ("regSDMA_RLC5_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :252
    ("regSDMA_RLC5_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :253
    ("regSDMA_RLC6_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :254
    ("regSDMA_RLC6_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :255
    ("regSDMA_RLC7_RB_RPTR_ADDR_LO", 0xfffffffd, 0x00000001),              # :256
    ("regSDMA_RLC7_RB_WPTR_POLL_CNTL", 0xfffffff7, 0x00403000),            # :257
    ("regSDMA_UTCL1_PAGE", 0x000003ff, 0x000003c0),                        # :258
    ("regSDMA_UTCL1_TIMEOUT", 0xffffffff, 0x00010001)]                     # :259

  def init_sw(self):
    self.sdma_reginst, self.sdma_name = [], "F32" if self.adev.ip_ver[am.SDMA0_HWIP] < (7,0,0) else "MCU"
    if self.adev.ip_ver[am.SDMA0_HWIP] == (4,2,0): self._init_sw_sdma_4_2_0() # mi50 fork, design v2 B15

  def _init_sw_sdma_4_2_0(self):
    # Vega 20 has two SDMA instances (amdgpu_discovery.c:3203); every per-instance loop below is bounded by the discovered count.
    self.sdma_insts = len(self.adev.regs_offset[am.SDMA0_HWIP])
    if self.sdma_insts != 2: raise RuntimeError(f"SDMA 4.2.0: expected 2 instances (amdgpu_discovery.c:3203), discovery gave {self.sdma_insts}")
    # has_page_queue on 4.2.0 = instance 0 fw_version >= 123 (sdma_v4_0.c:1736-1750, :1774-1775); fw_version is the SDMA header's ucode_version
    # (amdgpu_sdma.c:165). AMFirmware keeps no copy, so re-read the cached, hash-checked vega20_sdma.bin (= instance 0, amdev.py _init_vega20).
    self.sdma_fw_version = self.adev.fw.load_fw("vega20_sdma.bin", am.struct_sdma_firmware_header_v1_0)[1].header.ucode_version
    self.has_page_queue = self.sdma_fw_version >= 123
    if self.has_page_queue:
      # Linux page rings: amdgpu_ring_init(page, 1024 dw) (sdma_v4_0.c:1914); ring_size = roundup_pow_of_two(1024 * 4 * sched_hw_submission)
      # (amdgpu_ring.c:338) with sched_hw_submission 256 for instance[0].page (:247-248) and amdgpu_sched_hw_submission = 2 otherwise
      # (amdgpu_drv.c:178): 1 MiB and 8 KiB. rptr/wptr write-back: amdgpu_wb_get hands out 8-dword slots (amdgpu_wb.c:104); one boot page
      # per instance here, rptr at +0 and wptr at +0x20. Boot memory + MC addresses like AM_GFX's MQDs (system aperture = FB, ip.py init_hub).
      self.page_ring_size = {0: 0x100000, 1: 0x2000}
      self.page_ring_mc = [self.adev.paddr2mc(self.adev.mm.palloc(self.page_ring_size[i], boot=True)) for i in range(self.sdma_insts)]
      self.page_wb_mc = [self.adev.paddr2mc(self.adev.mm.palloc(0x1000, boot=True)) for i in range(self.sdma_insts)]

  def init_hw(self):
    if self.adev.ip_ver[am.SDMA0_HWIP] == (4,2,0): return self._init_hw_sdma_4_2_0() # mi50 fork, design v2 B15: the Linux sdma_v4_0 sequence

    for pipe_id in range(16 if self.adev.ip_ver[am.SDMA0_HWIP] < (5,0,0) else 1):
      pipe, inst = ("", pipe_id) if self.adev.ip_ver[am.SDMA0_HWIP] < (5,0,0) else (str(pipe_id), 0)

      if self.adev.ip_ver[am.SDMA0_HWIP] >= (6,0,0):
        self.adev.reg(f"regSDMA{pipe}_WATCHDOG_CNTL").update(queue_hang_count=100, inst=inst) # 10s, 100ms per unit
        self.adev.reg(f"regSDMA{pipe}_UTCL1_CNTL").update(resp_mode=3, redo_delay=9, inst=inst)

        # rd=noa, wr=bypass
        self.adev.reg(f"regSDMA{pipe}_UTCL1_PAGE").update(rd_l2_policy=2, wr_l2_policy=3, **({'llc_noalloc':1} if self.sdma_name == "F32" else {}),
                                                          inst=inst)
        self.adev.reg(f"regSDMA{pipe}_{self.sdma_name}_CNTL").update(halt=0, **{f"{'th1_' if self.sdma_name == 'F32' else ''}reset":0}, inst=inst)

      self.adev.reg(f"regSDMA{pipe}_CNTL").update(trap_enable=1,
        **({'utc_l1_enable':1} if self.adev.ip_ver[am.SDMA0_HWIP] <= (5,2,0) else {}), inst=inst)

    if self.adev.ip_ver[am.NBIO_HWIP] in {(7,9,0), (7,9,1)}:
      for aid_id in self.adev.aids:
        for dev_inst, (port, awid, offset, awaddr) in enumerate([(1, 0xe, 0xe, 0x1), (2, 0x8, 0x8, 0x2), (5, 0x9, 0x9, 0x8), (6, 0xa, 0xa, 0x9)]):
          entry = dev_inst + 1 + 4 * aid_id
          self.adev.reg(f"regDOORBELL0_CTRL_ENTRY_{entry}").write(**{f"bif_doorbell{entry}_range_size_entry": 20,
            f"bif_doorbell{entry}_range_offset_entry": (am.AMDGPU_NAVI10_DOORBELL_sDMA_ENGINE0 + (entry - 1) * 0xA) * 2})
          self.adev.soc.doorbell_enable(port=port, awid=awid, awaddr_31_28_value=awaddr, offset=offset, size=4, aid=aid_id)
    else: self.adev.soc.doorbell_enable(port=2, awid=0xe, awaddr_31_28_value=0x3, offset=am.AMDGPU_NAVI10_DOORBELL_sDMA_ENGINE0*2, size=4)

  # ---- mi50 fork: Vega 20 (SDMA 4.2.0 x2) bring-up, ported from Linux sdma_v4_0.c (design v2 B15, rule R-LINUX). Every value/order cites its
  # line. ----
  def _init_hw_sdma_4_2_0(self):
    from tinygrad.runtime.autogen import pci
    # sdma_v4_0_hw_init (sdma_v4_0.c:1960-1977): init_golden_registers (:1968-1969), then sdma_v4_0_start (:1971 -> :1381-1451), non-SR-IOV,
    # PSP-loaded (:1391-1395 skipped)
    for inst, table in ((0, self.GOLDEN_SDMA0_4_2_INIT), (0, self.GOLDEN_SDMA0_4_2), (1, self.GOLDEN_SDMA1_4_2)):   # :535-545
      for name, and_mask, or_mask in table:
        reg = self.adev.reg(name)
        reg.write(or_mask if and_mask == 0xffffffff else (reg.read(inst=inst) & ~and_mask) | (or_mask & and_mask), inst=inst) # soc15.c:467-475
    # sdma_v4_0_enable(true) (:1398 -> :1043-1060): F32_CNTL.HALT = 0 per instance (:1055-1059)
    for i in range(self.sdma_insts): self.adev.regSDMA_F32_CNTL.update(halt=0, inst=i)
    # sdma_v4_0_ctx_switch_enable(true) (:1400 -> :980-1033). amdgpu_sdma_phase_quantum defaults to 32 (amdgpu_drv.c:183): 32 fits
    # PHASE0_QUANTUM.VALUE,
    # so value 32, unit 0 (:985-1007). Per instance: read CNTL, set AUTO_CTXSW_ENABLE (:1010-1012), write PHASE0/1/2_QUANTUM (:1013-1017), write
    # CNTL (:1018),
    # UTCL1_TIMEOUT = 0x00800080 (:1030). :1025-1028 (PUB_DUMMY_REG2) is 4.2.2-only.
    phase_quantum = self.adev.regSDMA_PHASE0_QUANTUM.encode(value=32, unit=0)
    for i in range(self.sdma_insts):
      sdma_cntl = self.adev.regSDMA_CNTL
      cntl = (sdma_cntl.read(inst=i) & ~sdma_cntl.fields_mask('auto_ctxsw_enable')) | sdma_cntl.encode(auto_ctxsw_enable=1)
      for name in ("regSDMA_PHASE0_QUANTUM", "regSDMA_PHASE1_QUANTUM", "regSDMA_PHASE2_QUANTUM"): self.adev.reg(name).write(phase_quantum, inst=i)
      self.adev.regSDMA_CNTL.write(cntl, inst=i)
      self.adev.regSDMA_UTCL1_TIMEOUT.write(0x00800080, inst=i)
    # :1404-1423 per instance
    for i in range(self.sdma_insts):
      self.adev.regSDMA_SEM_WAIT_FAIL_TIMER_CNTL.write(0, inst=i)                                                   # :1407
      # :1408 gfx_resume: AM programs the gfx ring in setup_ring when the compute side hands it a ring (design B15); :1409-1410 page_resume when
      # has_page_queue:
      # Linux page ring doorbell = (sdma_engine[i] + 1) << 1 dwords on 4.2.0 (:1898-1903; AMDGPU_VEGA20_DOORBELL_sDMA_ENGINE0/1 = 0x100/0x10A,
      # amdgpu_doorbell.h:137-138)
      if self.has_page_queue:
        self._resume_ring_sdma_4_2_0("regSDMA_PAGE", i, self.page_ring_mc[i], self.page_ring_size[i], self.page_wb_mc[i], self.page_wb_mc[i] + 0x20,
                                     (am.AMDGPU_VEGA20_DOORBELL_sDMA_ENGINE0 + i * 0xA + 1) << 1)
      # :1413-1415 UTC_L1_ENABLE (+ trap_enable as ip.py does)
      self.adev.regSDMA_CNTL.update(utc_l1_enable=1, trap_enable=1, inst=i)
      self.adev.regSDMA_F32_CNTL.update(halt=0, inst=i)                                                            # :1419-1421 unhalt engine
    # :1429 rlc_resume -> init_pg (:1298-1313): pg_flags == 0 on Vega 20 (soc15.c:1019), nothing. :1434-1448 ring tests are T10.
    # late_init (:1789-1799) -> setup_ulv (:580-608): IP 4.2.0 and PCI device 0x66a1 (:593-595) -> ULV_CNTL.HYSTERESIS = 0 per instance (:601-607)
    if self.adev.pci_dev.read_config(pci.PCI_DEVICE_ID, 2) == 0x66a1:
      for i in range(self.sdma_insts): self.adev.regSDMA_ULV_CNTL.update(hysteresis=0, inst=i)
    # ip.py's S2A doorbell_enable is not issued: nbio_7_4 has no S2A registers; BIF_SDMA0/1_DOORBELL_RANGE is AM_SOC's (design B8).

  def _resume_ring_sdma_4_2_0(self, reg:str, inst:int, ring_addr:int, ring_size:int, rptr_addr:int, wptr_addr:int, doorbell_dw:int):
    # sdma_v4_0_gfx_resume (sdma_v4_0.c:1088-1162) and sdma_v4_0_page_resume (:1173-1248): the same register sequence on the GFX_* / PAGE_* set.
    def r(s): return self.adev.reg(f"{reg}_{s}")
    rb = r("RB_CNTL")
    # :1096-1097 sdma_v4_0_rb_cntl (:1065-1077), order_base_2(ring_size/4)
    rb_cntl = (rb.read(inst=inst) & ~rb.fields_mask('rb_size')) | rb.encode(rb_size=(ring_size//4).bit_length()-1)
    rb.write(rb_cntl, inst=inst)                                                                                   # :1098
    for s in ("RB_RPTR", "RB_RPTR_HI", "RB_WPTR", "RB_WPTR_HI"): r(s).write(0, inst=inst)                          # :1101-1104
    r("RB_RPTR_ADDR_HI").write(hi32(rptr_addr), inst=inst)                                                         # :1107-1108
    r("RB_RPTR_ADDR_LO").write(lo32(rptr_addr) & 0xFFFFFFFC, inst=inst)                                            # :1109-1110
    rb_cntl |= rb.encode(rptr_writeback_enable=1)                                                                  # :1112-1113
    r("RB_BASE").write(lo32(ring_addr >> 8), inst=inst)                                                            # :1115
    r("RB_BASE_HI").write(ring_addr >> 40, inst=inst)                                                              # :1116
    r("MINOR_PTR_UPDATE").write(1, inst=inst)                                                                      # :1121
    # :1123, :1126-1127, :1131 (use_doorbell, :1857/:1886)
    r("DOORBELL").update(enable=1, inst=inst)
    r("DOORBELL_OFFSET").update(offset=doorbell_dw, inst=inst)                                                     # :1124, :1128-1130, :1132
    # :1134 ring_set_wptr with use_doorbell (:702-717 / :767-772): *wptr_cpu_addr = wptr << 2 and WDOORBELL64(doorbell_index, wptr << 2), wptr == 0.
    # WDOORBELL64 writes at a dword index into the doorbell aperture (amdgpu_doorbell_mgr.c:103-110); AM's doorbell64 view is 64-bit wide -> index
    # // 2. Review 2026-10-13 L10: the 8-byte shadow write (:713 / :771) is NOT issued here - wptr_addr is a GPU VA of the caller's slot (the page
    # queues: AM's zeroed boot page; the gfx ring: ops_amd.py:1096 gart alloc at :944) that AM_SDMA cannot address, and AM's stock path for every
    # other card (setup_ring below) writes no shadow either: the caller's slot must read 0. Listed.
    self.adev.doorbell64[doorbell_dw // 2] = 0
    r("MINOR_PTR_UPDATE").write(0, inst=inst)                                                                      # :1137
    r("RB_WPTR_POLL_ADDR_LO").write(lo32(wptr_addr), inst=inst)                                                    # :1141-1142
    r("RB_WPTR_POLL_ADDR_HI").write(hi32(wptr_addr), inst=inst)                                                    # :1143-1144
    r("RB_WPTR_POLL_CNTL").update(f32_poll_enable=0, inst=inst)                                                    # :1145-1149 (1 only under SR-IOV)
    rb.write(rb_cntl | rb.encode(rb_enable=1), inst=inst)                                                          # :1152-1153
    r("IB_CNTL").update(ib_enable=1, inst=inst)                                                                    # :1155-1161
    # fini_hw stops it (Linux gfx/page_stop :955-970)
    self.sdma_reginst.append((reg, inst))

  def fini_hw(self):
    for reg, inst in self.sdma_reginst:
      self.adev.reg(f"{reg}_RB_CNTL").update(rb_enable=0, inst=inst)
      self.adev.reg(f"{reg}_IB_CNTL").update(ib_enable=0, inst=inst)
      self.adev.reg(f"{reg}_DOORBELL").update(enable=0, inst=inst)
      self.adev.reg(f"{reg}_DOORBELL_OFFSET").update(offset=0, inst=inst)

    if self.adev.ip_ver[am.SDMA0_HWIP] >= (6,0,0):
      self.adev.regGRBM_SOFT_RESET.write(soft_reset_sdma0=1)
      time.sleep(0.01)
      self.adev.regGRBM_SOFT_RESET.write(0x0)

  def setup_ring(self, ring_addr:int, ring_size:int, rptr_addr:int, wptr_addr:int, idx:int) -> int:
    if self.adev.ip_ver[am.SDMA0_HWIP] >= (5,0,0) and idx > 0: raise RuntimeError(f"am {self.adev.devfmt}: sdma queue {idx} is not available")

    if self.adev.ip_ver[am.SDMA0_HWIP] == (4,2,0):
      # mi50 fork, design v2 B15: one gfx ring per engine (sdma_v4_0.c:1854-1863), instance = engine; doorbell 0x100 + 0xA*engine ==
      # AMDGPU_VEGA20_DOORBELL_sDMA_ENGINE0/1 (amdgpu_doorbell.h:137-138); Linux ring->doorbell_index = sdma_engine[i] << 1 dwords (:1863).
      if idx >= self.sdma_insts: raise RuntimeError(f"am {self.adev.devfmt}: sdma queue {idx} is not available ({self.sdma_insts} engines)")
      doorbell = am.AMDGPU_NAVI10_DOORBELL_sDMA_ENGINE0 + idx * 0xA
      self._resume_ring_sdma_4_2_0("regSDMA_GFX", idx, ring_addr, ring_size, rptr_addr, wptr_addr, doorbell * 2)
      self.adev.regSDMA_F32_CNTL.update(halt=0, inst=idx) # :1419-1421 (design B15: F32 HALT=0 after the ring programming)
      return doorbell

    pipe, queue = idx // 4, idx % 4
    reg, inst = ("regSDMA_GFX", pipe+queue*4) if self.adev.ip_ver[am.SDMA0_HWIP][:2] == (4,4) else (f"regSDMA{pipe}_QUEUE{queue}", 0)
    doorbell = am.AMDGPU_NAVI10_DOORBELL_sDMA_ENGINE0 + (pipe+queue*4) * 0xA
    self.sdma_reginst.append((reg, inst))

    self.adev.reg(f"{reg}_MINOR_PTR_UPDATE").write(0x1, inst=inst)
    self.adev.wreg_pair(f"{reg}_RB_RPTR", "", "_HI", 0, inst=inst)
    self.adev.wreg_pair(f"{reg}_RB_WPTR", "", "_HI", 0, inst=inst)
    self.adev.wreg_pair(f"{reg}_RB_BASE", "", "_HI", ring_addr >> 8, inst=inst)
    self.adev.wreg_pair(f"{reg}_RB_RPTR_ADDR", "_LO", "_HI", rptr_addr, inst=inst)
    self.adev.wreg_pair(f"{reg}_RB_WPTR_POLL_ADDR", "_LO", "_HI", wptr_addr, inst=inst)
    self.adev.reg(f"{reg}_DOORBELL_OFFSET").update(offset=doorbell * 2, inst=inst)
    self.adev.reg(f"{reg}_DOORBELL").update(enable=1, inst=inst)
    self.adev.reg(f"{reg}_MINOR_PTR_UPDATE").write(0x0, inst=inst)
    self.adev.reg(f"{reg}_RB_CNTL").write(**({f'{self.sdma_name.lower()}_wptr_poll_enable':1} if self.adev.ip_ver[am.SDMA0_HWIP][:2]!=(4,4) else {}),
      rb_vmid=0, rptr_writeback_enable=1, rptr_writeback_timer=4, rb_enable=1, rb_priv=1, rb_size=(ring_size//4).bit_length()-1, inst=inst)
    self.adev.reg(f"{reg}_IB_CNTL").update(ib_enable=1, inst=inst)
    return doorbell

class AM_PSP(AM_IP):
  # mi50 fork: Vega 20 = MP0 (11,0,2) = Linux psp_v11_0. Every Vega 20 difference below is gated on `self.vega20` (exact tuple, design v2
  # R-LINUX / B12), so MP0 13.x (gfx11, MI300) and 14.x (gfx12) execute the same statements with the same values as before this fork.
  # Linux sources: L/amdgpu/psp_v11_0.c (bootloader, ring, mode1 reset), L/amdgpu/amdgpu_psp.c (psp_hw_start / psp_load_fw order),
  # L/amdgpu/psp_gfx_if.h (command ids, masks, structs), L/amdgpu/amdgpu_psp.h (mailbox masks, PSP_TMR_SIZE, bootloader cmds).
  PSP_TMR_SIZE_VEGA20 = 0x400000 # amdgpu_psp.h:40 `PSP_TMR_SIZE(adev)`: 0x400000 for every asic but CHIP_ALDEBARAN; not in autogen am.py
  # amdgpu_psp.h:57-58 MBOX_TOS_READY_FLAG = GFX_FLAG_RESPONSE, MBOX_TOS_READY_MASK = GFX_CMD_RESPONSE_MASK | GFX_CMD_STATUS_MASK
  # (psp_gfx_if.h:29 STATUS 0x0000FFFF, :32 RESPONSE 0x80000000, :84 FLAG_RESPONSE 0x80000000) -> mask 0x8000FFFF, flag 0x80000000
  MBOX_TOS_READY_MASK, MBOX_TOS_READY_FLAG = am.GFX_CMD_RESPONSE_MASK | am.GFX_CMD_STATUS_MASK, am.GFX_FLAG_RESPONSE

  def init_sw(self):
    self.vega20 = self.adev.ip_ver[am.MP0_HWIP] == (11,0,2)
    # vega20: the mp_11_0_0 table spells these mmMP0_SMN_C2PMSG_* (mp_11_0_offset.h:34-126); AMDev.__getattr__ maps reg* -> mm* (amdev.py)
    self.reg_pref = "regMP0_SMN_C2PMSG" if self.adev.ip_ver[am.MP0_HWIP] < (14,0,0) else "regMPASP_SMN_C2PMSG"

    if self.adev.devfmt.startswith("usb:"):
      self.msg1_view, paddrs = self.adev.pci_dev.alloc_sysmem(512 << 10)
      self.msg1_addr = self.adev.mm.alloc_vaddr(size=self.msg1_view.nbytes, align=am.PSP_1_MEG)
      self.adev.mm.map_range(self.msg1_addr, self.msg1_view.nbytes, [(paddrs[0], self.msg1_view.nbytes)], AddrSpace.SYS, uncached=True, boot=True)
    else:
      self.msg1_paddr = self.adev.mm.palloc(am.PSP_1_MEG, align=am.PSP_1_MEG, zero=False, boot=True)
      self.msg1_addr, self.msg1_view = self.adev.paddr2mc(self.msg1_paddr), self.adev.vram.view(self.msg1_paddr, am.PSP_1_MEG, 'B')

    self.cmd_paddr = self.adev.mm.palloc(am.PSP_CMD_BUFFER_SIZE, zero=False, boot=True)
    self.fence_paddr = self.adev.mm.palloc(am.PSP_FENCE_BUFFER_SIZE, zero=True, boot=True)

    self.ring_size = 0x10000
    self.ring_paddr = self.adev.mm.palloc(self.ring_size, zero=False, boot=True)

    self.max_tmr_size, self.tmr_size = 0x1300000, 0
    # vega20: psp_tmr_init starts from tmr_size = PSP_TMR_SIZE and only LOAD_TOC overrides it (amdgpu_psp.c:887-910); a v1.0 sos header
    # has no TOC (amdev.py AMFirmware._init_vega20), so AM's "0 until LOAD_TOC" would send SETUP_TMR with buf_size 0 (_tmr_load_cmd).
    if self.vega20: self.tmr_size = self.PSP_TMR_SIZE_VEGA20
    self.boot_time_tmr = self.adev.ip_ver[am.MP0_HWIP] in {(13,0,6), (13,0,14), (14,0,2), (14,0,3)}
    self.autoload_tmr = self.adev.ip_ver[am.MP0_HWIP] not in {(13,0,6), (13,0,14)}
    self.tmr_paddr = self.adev.mm.palloc(self.max_tmr_size, align=am.PSP_TMR_ALIGNMENT, zero=False, boot=True) if not self.boot_time_tmr else 0

  def init_hw(self):
    if self.vega20: return self._init_hw_vega20()
    spl_key = am.PSP_FW_TYPE_PSP_SPL if self.adev.ip_ver[am.MP0_HWIP] >= (14,0,0) else am.PSP_FW_TYPE_PSP_KDB
    sos_components = [(am.PSP_FW_TYPE_PSP_KDB, am.PSP_BL__LOAD_KEY_DATABASE), (spl_key, am.PSP_BL__LOAD_TOS_SPL_TABLE),
      (am.PSP_FW_TYPE_PSP_SYS_DRV, am.PSP_BL__LOAD_SYSDRV), (am.PSP_FW_TYPE_PSP_SOC_DRV, am.PSP_BL__LOAD_SOCDRV),
      (am.PSP_FW_TYPE_PSP_INTF_DRV, am.PSP_BL__LOAD_INTFDRV), (am.PSP_FW_TYPE_PSP_DBG_DRV, am.PSP_BL__LOAD_DBGDRV),
      (am.PSP_FW_TYPE_PSP_RAS_DRV, am.PSP_BL__LOAD_RASDRV), (am.PSP_FW_TYPE_PSP_SOS, am.PSP_BL__LOAD_SOSDRV)]

    if not self.is_sos_alive():
      for fw, compid in sos_components: self._bootloader_load_component(fw, compid)
      wait_cond(self.is_sos_alive, value=True, msg="sOS failed to start")

    self._ring_create()
    if am.PSP_FW_TYPE_PSP_TOC in self.adev.fw.sos_fw: self._tmr_init()

    # SMU fw should be loaded before TMR.
    if hasattr(self.adev.fw, 'smu_psp_desc'): self._load_ip_fw_cmd(*self.adev.fw.smu_psp_desc)
    if not self.boot_time_tmr or not self.autoload_tmr: self._tmr_load_cmd()

    for psp_desc in self.adev.fw.descs: self._load_ip_fw_cmd(*psp_desc)

    if self.adev.ip_ver[am.GC_HWIP] >= (11,0,0): self._rlc_autoload_cmd()
    else: self._load_ip_fw_cmd([am.GFX_FW_TYPE_REG_LIST], self.adev.fw.sos_fw[am.PSP_FW_TYPE_PSP_RL])

  def _init_hw_vega20(self):
    # Linux psp_load_fw (amdgpu_psp.c:3595-3631): psp_hw_start -> psp_load_non_psp_fw -> psp_asd_initialize -> psp_rl_load (design v2 §4.4).
    # psp_hw_start (amdgpu_psp.c:2952-3051) runs the bootloader stages kdb/spl/sysdrv/soc/intf/dbg/ras/ipkeymgr/spdm/sos, each only when
    # the descriptor is valid AND the psp_funcs entry exists; psp_v11_0 has kdb/spl/sysdrv/sos only (psp_v11_0.c:673-689) and a v1.0 sos
    # header carries neither kdb nor spl (amdev.py AMFirmware._init_vega20) -> SYS_DRV (PSP_BL__LOAD_SYSDRV 0x10000) then SOS
    # (PSP_BL__LOAD_SOSDRV 0x20000) (amdgpu_psp.h:96-97). Both are mandatory: psp_v11_0_init_microcode fails without the sos file
    # (psp_v11_0.c:99-103) and is_psp_fw_valid = size_bytes != 0 (amdgpu_psp.c:4711-4714; checked at :2961, :2979, :3042).
    for fw, compid in [(am.PSP_FW_TYPE_PSP_SYS_DRV, am.PSP_BL__LOAD_SYSDRV), (am.PSP_FW_TYPE_PSP_SOS, am.PSP_BL__LOAD_SOSDRV)]:
      # each psp_v11_0 stage returns early once the sOS sign-of-life register C2PMSG_81 is non-zero (psp_v11_0.c:214-215, 262-263)
      if self.is_sos_alive(): break
      assert len(self.adev.fw.sos_fw.get(fw, b'')) > 0, f"vega20_sos.bin: no {am.enum_psp_fw_type[fw]} image"
      self._bootloader_load_component(fw, compid) # C2PMSG_36 = msg1 >> 20, C2PMSG_35 = cmd, wait bootloader (psp_v11_0.c:217-234)
      # psp_v11_0_bootloader_load_sos: "there might be handshake issue with hardware which needs delay" mdelay(20) (psp_v11_0.c:279-280)
      if compid == am.PSP_BL__LOAD_SOSDRV: time.sleep(0.02)
    # psp_v11_0.c:281-285: wait for C2PMSG_81 to change from its post-delay value (PSP_WAITREG_CHANGED, amdgpu_psp.c:609-625); the stage
    # only runs while C2PMSG_81 == 0, so "changed" is "!= 0" = is_sos_alive (psp_v11_0.c:193-200). (If the loop broke on alive, one read.)
    wait_cond(self.is_sos_alive, value=True, msg="sOS failed to start")

    self._ring_create() # psp_ring_create(KM) right after the bootloader, before the TMR (amdgpu_psp.c:3052-3056)
    # psp_update_fw_reservation: `default: return 0` for MP0 other than 14.0.2/14.0.3 (amdgpu_psp.c:1065-1089) -> no step here.
    # psp_tmr_init (amdgpu_psp.c:887-929, gated by !boot_time_tmr || autoload at :3069-3071): LOAD_TOC only with a TOC; none in v1.0.
    if am.PSP_FW_TYPE_PSP_TOC in self.adev.fw.sos_fw: self._tmr_init()
    # psp_hw_start loads the SMU before the TMR only when pmfw_centralized_cstate_management (amdgpu_psp.c:3078-3087), which is set for
    # MP0 11.0.0/4/5/7/9/11/12/13 and 13.0.0/2/7 only (amdgpu_psp.c:117-128) - not 11.0.2. The SMC image is instead the last fw.descs
    # entry (ucode enum order, amdgpu_psp.c:3515-3593; amdev.py _init_vega20), so smu_psp_desc must not exist for this card.
    assert not hasattr(self.adev.fw, 'smu_psp_desc'), "vega20: SMC goes through fw.descs after RLC_G, never through smu_psp_desc"
    # psp_tmr_load -> SETUP_TMR with virt_phy_addr=1, buf_size = tmr size, system addr = vram pa (amdgpu_psp.c:3089-3093, 826-849, 941-961)
    self._tmr_load_cmd()
    # psp_load_non_psp_fw (amdgpu_psp.c:3515-3593): every valid ucode in enum order (SDMA0, SDMA1, MEC1, MEC1_JT, RLC lists, RLC_G, SMC),
    # one GFX_CMD_ID_LOAD_IP_FW each (psp_prep_load_ip_fw_cmd_buf amdgpu_psp.c:3374-3390); fw.descs holds exactly that order.
    for psp_desc in self.adev.fw.descs: self._load_ip_fw_cmd(*psp_desc)
    # psp_asd_initialize (amdgpu_psp.c:3621-3625 call; :1681-1706): mandatory for 11.0.2 (psp_init_asd_microcode error is fatal,
    # psp_v11_0.c:99-106); "PSP load asd failed!" aborts psp_load_fw. Design v2 U-P2 asks whether compute-only needs it; R-LINUX keeps it.
    assert hasattr(self.adev.fw, 'asd_fw') and len(self.adev.fw.asd_fw) > 0, "vega20: vega20_asd.bin image missing (psp_v11_0.c:99-106)"
    self._load_asd_cmd(self.adev.fw.asd_fw)
    # psp_rl_load (amdgpu_psp.c:3627-3631 call; :1177-1184): `if (!is_psp_fw_valid(psp->rl)) return 0` - only a v1.3 header has one.
    if am.PSP_FW_TYPE_PSP_RL in self.adev.fw.sos_fw: self._load_ip_fw_cmd([am.GFX_FW_TYPE_REG_LIST], self.adev.fw.sos_fw[am.PSP_FW_TYPE_PSP_RL])

  def is_sos_alive(self): return self.adev.reg(f"{self.reg_pref}_81").read() != 0x0

  def _wait_for_bootloader(self):
    # vega20: psp_v11_0_wait_for_bootloader = (C2PMSG_35 & 0x8000FFFF) == 0x80000000 (psp_v11_0.c:179-188); others keep AM's bit-31 test
    mask = 0x8000FFFF if self.vega20 else 0x80000000
    wait_cond(lambda: self.adev.reg(f"{self.reg_pref}_35").read() & mask, value=0x80000000, msg="BL not ready")

  def _prep_msg1(self, data:memoryview):
    assert len(data) <= self.msg1_view.nbytes, f"msg1 buffer is too small {len(data):#x} > {self.msg1_view.nbytes:#x}"
    padded_data = pad_bytes(bytes(data) + b'\x00' * 4, 16) # HACK: apple's memcpy requires 16-bytes alignment
    self.msg1_view[:len(padded_data)] = padded_data
    self.adev.gmc.flush_hdp()

  def _bootloader_load_component(self, fw:int, compid:int):
    if fw not in self.adev.fw.sos_fw: return 0

    self._wait_for_bootloader()

    if DEBUG >= 2: print(f"am {self.adev.devfmt}: loading sos component: {am.enum_psp_fw_type.get(fw)}")

    self._prep_msg1(self.adev.fw.sos_fw[fw])
    self.adev.reg(f"{self.reg_pref}_36").write(self.msg1_addr >> 20)
    self.adev.reg(f"{self.reg_pref}_35").write(compid)

    return self._wait_for_bootloader() if compid != am.PSP_BL__LOAD_SOSDRV else 0

  def _tmr_init(self):
    # Load TOC and calculate TMR size
    self._prep_msg1(fwm:=self.adev.fw.sos_fw[am.PSP_FW_TYPE_PSP_TOC])
    self.tmr_size = self._load_toc_cmd(len(fwm)).resp.tmr_size
    assert self.tmr_size <= self.max_tmr_size

  def _ring_create(self):
    # If the ring is already created, destroy it
    if self.adev.reg(f"{self.reg_pref}_71").read() != 0:
      self.adev.reg(f"{self.reg_pref}_64").write(am.GFX_CTRL_CMD_ID_DESTROY_RINGS)

      # There might be handshake issue with hardware which needs delay
      time.sleep(0.02)

    # Wait until the sOS is ready
    # vega20: psp_v11_0_ring_create waits C2PMSG_64 & MBOX_TOS_READY_MASK == MBOX_TOS_READY_FLAG (psp_v11_0.c:356-363); others keep bit 31
    mask = self.MBOX_TOS_READY_MASK if self.vega20 else 0x80000000
    wait_cond(lambda: self.adev.reg(f"{self.reg_pref}_64").read() & mask, value=0x80000000, msg="sOS not ready")

    self.adev.wreg_pair(self.reg_pref, "_69", "_70", self.adev.paddr2mc(self.ring_paddr))
    self.adev.reg(f"{self.reg_pref}_71").write(self.ring_size)
    self.adev.reg(f"{self.reg_pref}_64").write(am.PSP_RING_TYPE__KM << 16)

    # There might be handshake issue with hardware which needs delay
    time.sleep(0.02)

    wait_cond(lambda: self.adev.reg(f"{self.reg_pref}_64").read() & 0x8000FFFF, value=0x80000000, msg="sOS ring not created")

  def _ring_submit(self, cmd:am.struct_psp_gfx_cmd_resp) -> am.struct_psp_gfx_cmd_resp:
    prev_wptr = self.adev.reg(f"{self.reg_pref}_67").read() # psp_v11_0_ring_get_wptr (psp_v11_0.c:590-601)
    # vega20: psp_ring_cmd_submit places the frame at wptr % ring_size_dw and advances wptr modulo ring_size_dw (amdgpu_psp.c:3917-3967)
    if self.vega20: prev_wptr %= self.ring_size // 4
    msg = am.struct_psp_gfx_rb_frame(fence_value=prev_wptr + 1,
      cmd_buf_addr_lo=lo32(self.adev.paddr2mc(self.cmd_paddr)), cmd_buf_addr_hi=hi32(self.adev.paddr2mc(self.cmd_paddr)),
      fence_addr_lo=lo32(self.adev.paddr2mc(self.fence_paddr)), fence_addr_hi=hi32(self.adev.paddr2mc(self.fence_paddr)))

    self.adev.vram.view(self.cmd_paddr, ctypes.sizeof(cmd))[:] = memoryview(cmd).cast('B')
    self.adev.vram.view(self.ring_paddr + prev_wptr * 4, ctypes.sizeof(msg))[:] = memoryview(msg).cast('B')

    # Move the wptr
    new_wptr = prev_wptr + ctypes.sizeof(am.struct_psp_gfx_rb_frame) // 4
    self.adev.reg(f"{self.reg_pref}_67").write(new_wptr % (self.ring_size // 4) if self.vega20 else new_wptr) # amdgpu_psp.c:3963-3964

    wait_cond(lambda: self.adev.vram.view(self.fence_paddr, 4, 'I')[0], value=msg.fence_value, msg="sOS ring not responding")

    resp = type(cmd).from_buffer(bytearray(self.adev.vram.view(self.cmd_paddr, ctypes.sizeof(cmd))[:]))
    # vega20 / design v2 U-P3: Linux only warns on a non-zero status ("Some version of PSP FW doesn't write 0 to that field",
    # amdgpu_psp.c:772-790) - log every status with the command (and fw type for LOAD_IP_FW) so T6b records it; AM's raise stays.
    if self.vega20 and DEBUG >= 2:
      fwt = f" {am.enum_psp_gfx_fw_type.get(resp.cmd.cmd_load_ip_fw.fw_type)}" if resp.cmd_id == am.GFX_CMD_ID_LOAD_IP_FW else ""
      print(f"am {self.adev.devfmt}: psp {am.enum_psp_gfx_cmd_id.get(resp.cmd_id, resp.cmd_id)}{fwt} resp.status={resp.resp.status:#x}")
    if resp.resp.status != 0: raise RuntimeError(f"PSP command failed {resp.cmd_id} {resp.resp.status}")

    return resp

  def _load_ip_fw_cmd(self, fw_types:list[int], fw_bytes:memoryview):
    self._prep_msg1(fw_bytes)
    for fw_type in fw_types:
      if DEBUG >= 2: print(f"am {self.adev.devfmt}: loading fw: {am.enum_psp_gfx_fw_type.get(fw_type)}")
      cmd = am.struct_psp_gfx_cmd_resp(cmd_id=am.GFX_CMD_ID_LOAD_IP_FW)
      cmd.cmd.cmd_load_ip_fw.fw_phy_addr_hi, cmd.cmd.cmd_load_ip_fw.fw_phy_addr_lo = data64(self.msg1_addr)
      cmd.cmd.cmd_load_ip_fw.fw_size = len(fw_bytes)
      cmd.cmd.cmd_load_ip_fw.fw_type = fw_type
      self._ring_submit(cmd)

  def _tmr_load_cmd(self) -> am.struct_psp_gfx_cmd_resp:
    tmr_paddr = self.adev.paddr2xgmi(self.tmr_paddr) if self.tmr_paddr else 0

    cmd = am.struct_psp_gfx_cmd_resp(cmd_id=am.GFX_CMD_ID_SETUP_TMR)
    cmd.cmd.cmd_setup_tmr.buf_phy_addr_hi, cmd.cmd.cmd_setup_tmr.buf_phy_addr_lo = data64(self.adev.paddr2mc(self.tmr_paddr) if self.tmr_paddr else 0)
    cmd.cmd.cmd_setup_tmr.system_phy_addr_hi, cmd.cmd.cmd_setup_tmr.system_phy_addr_lo = data64(tmr_paddr)
    cmd.cmd.cmd_setup_tmr.bitfield.virt_phy_addr = 1
    cmd.cmd.cmd_setup_tmr.buf_size = self.tmr_size if self.tmr_paddr else 0
    return self._ring_submit(cmd)

  def _load_toc_cmd(self, toc_size:int) -> am.struct_psp_gfx_cmd_resp:
    cmd = am.struct_psp_gfx_cmd_resp(cmd_id=am.GFX_CMD_ID_LOAD_TOC)
    cmd.cmd.cmd_load_toc.toc_phy_addr_hi, cmd.cmd.cmd_load_toc.toc_phy_addr_lo = data64(self.msg1_addr)
    cmd.cmd.cmd_load_toc.toc_size = toc_size
    return self._ring_submit(cmd)

  def _spatial_partition_cmd(self, mode):
    cmd = am.struct_psp_gfx_cmd_resp(cmd_id=am.GFX_CMD_ID_SRIOV_SPATIAL_PART)
    cmd.cmd.cmd_spatial_part.mode = mode
    return self._ring_submit(cmd)

  def _rlc_autoload_cmd(self): return self._ring_submit(am.struct_psp_gfx_cmd_resp(cmd_id=am.GFX_CMD_ID_AUTOLOAD_RLC))

  def _load_asd_cmd(self, asd_bytes:memoryview) -> am.struct_psp_gfx_cmd_resp:
    # psp_asd_initialize (amdgpu_psp.c:1681-1706): shared_mc_addr = 0, shared_mem_size = PSP_ASD_SHARED_MEM_SIZE = 0 (amdgpu_psp.h:69-70),
    # ta_load_type = GFX_CMD_ID_LOAD_ASD (psp_gfx_if.h:92). psp_ta_load (amdgpu_psp.c:1836-1858) copies the image to fw_pri (= msg1 here)
    # and psp_prep_ta_load_cmd_buf (amdgpu_psp.c:1777-1791) fills struct psp_gfx_cmd_load_ta (psp_gfx_if.h:131-144): app_phy_addr =
    # fw_pri_mc_addr (4 KB aligned; msg1 is 1 MiB aligned), app_len = size_bytes, cmd_buf_phy_addr = 0, cmd_buf_len = 0.
    self._prep_msg1(asd_bytes)
    if DEBUG >= 2: print(f"am {self.adev.devfmt}: loading asd: {len(asd_bytes):#x} bytes")
    cmd = am.struct_psp_gfx_cmd_resp(cmd_id=am.GFX_CMD_ID_LOAD_ASD)
    cmd.cmd.cmd_load_ta.app_phy_addr_hi, cmd.cmd.cmd_load_ta.app_phy_addr_lo = data64(self.msg1_addr)
    cmd.cmd.cmd_load_ta.app_len = len(asd_bytes)
    cmd.cmd.cmd_load_ta.cmd_buf_phy_addr_hi, cmd.cmd.cmd_load_ta.cmd_buf_phy_addr_lo = data64(0)
    cmd.cmd.cmd_load_ta.cmd_buf_len = am.PSP_ASD_SHARED_MEM_SIZE
    return self._ring_submit(cmd)

  def mode1_reset(self):
    # psp_v11_0_mode1_reset (psp_v11_0.c:410-432): wait C2PMSG_64 & MBOX_TOS_READY_MASK == MBOX_TOS_READY_FLAG, else -EINVAL
    # "psp is not working correctly before mode1 reset!"; write GFX_CTRL_CMD_ID_MODE1_RST (0x70000, psp_gfx_if.h:48) to C2PMSG_64;
    # msleep(500). Linux's post-reset gate (PCI state restore, bootloader wait, RCC_CONFIG_MEMSIZE != 0xffffffff, re-POST) lives in
    # amdgpu_device_mode1_reset and is the caller's (design v2 B7, amdev.py reset branch), not this method's.
    assert self.vega20, "AM_PSP.mode1_reset is the psp_v11_0 mailbox reset, ported for MP0 (11,0,2) only; other MP0 use AM_SMU.mode1_reset"
    if DEBUG >= 2: print(f"am {self.adev.devfmt}: psp mode1 reset")
    wait_cond(lambda: self.adev.reg(f"{self.reg_pref}_64").read() & self.MBOX_TOS_READY_MASK, value=self.MBOX_TOS_READY_FLAG,
              msg="psp is not working correctly before mode1 reset")
    self.adev.reg(f"{self.reg_pref}_64").write(am.GFX_CTRL_CMD_ID_MODE1_RST)
    time.sleep(0.5)
