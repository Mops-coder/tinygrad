import ctypes, time, contextlib, functools
from typing import Iterable, Literal
from tinygrad.helpers import to_mv, data64, lo32, hi32, DEBUG, wait_cond, pad_bytes, getbits
from tinygrad.runtime.autogen.am import am
from tinygrad.runtime.support.amd import import_soc
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
  def set_clockgating_state(self):
    if self.adev.ip_ver[am.HDP_HWIP] >= (5,2,1): self.adev.regHDP_MEM_POWER_CTRL.update(atomic_mem_power_ctrl_en=1, atomic_mem_power_ds_en=1)

  def doorbell_enable(self, port, awid=0, awaddr_31_28_value=0, offset=0, size=0, aid=0):
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

    self.fb_base = (self.adev.regMMMC_VM_FB_LOCATION_BASE.read() & 0xFFFFFF) << 24
    self.fb_end = (self.adev.regMMMC_VM_FB_LOCATION_TOP.read() & 0xFFFFFF) << 24

    # Memory controller aperture
    self.mc_base = self.fb_base + self.paddr_base

    # VM aperture
    self.vm_base = self.adev.mm.va_base
    self.vm_end = min(self.vm_base + (1 << self.adev.mm.va_bits) - 1, 0x7fffffffffff)

    self.trans_futher = self.adev.ip_ver[am.GC_HWIP] < (10, 0, 0)

    # mi3xx has 48-bit, others have 44-bit address space
    self.address_space_mask = (1 << (48 if self.adev.ip_ver[am.GC_HWIP][:2] in {(9,4), (9,5)} else 44)) - 1

    self.memscratch_xgmi_paddr = self.adev.paddr2xgmi(self.adev.mm.palloc(0x1000, zero=False, boot=True))
    self.dummy_page_xgmi_paddr = self.adev.paddr2xgmi(self.adev.mm.palloc(0x1000, zero=False, boot=True))

    # MM hub is inited before any tlb flushes and is still valid during partial_boot, so set it to true
    self.hub_initted = {"MM": True, "GC": False}

    self.mm_insts = self.adev.aids if self.adev.ip_ver[am.NBIO_HWIP] in {(7,9,0), (7,9,1)} else list(range(self.vmhubs)) # dead mmhubs hang us

    self.pf_status_reg = lambda ip: f"reg{ip}VM_L2_PROTECTION_FAULT_STATUS{'_LO32' if self.adev.ip_ver[am.GC_HWIP] >= (12,0,0) else ''}"

  def init_hw(self): self.init_hub("MM", insts=self.mm_insts)

  def flush_hdp(self): self.adev.wreg(self.adev.reg("regBIF_BX0_REMAP_HDP_MEM_FLUSH_CNTL").read() // 4, 0x0)
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
    # Init system apertures
    for inst in insts:
      self.adev.reg(f"reg{ip}MC_VM_AGP_BASE").write(0, inst=inst)
      self.adev.reg(f"reg{ip}MC_VM_AGP_BOT").write(0xffffffffffff >> 24, inst=inst) # disable AGP
      self.adev.reg(f"reg{ip}MC_VM_AGP_TOP").write(0, inst=inst)

      self.adev.reg(f"reg{ip}MC_VM_SYSTEM_APERTURE_LOW_ADDR").write(self.fb_base >> 18, inst=inst)
      self.adev.reg(f"reg{ip}MC_VM_SYSTEM_APERTURE_HIGH_ADDR").write(self.fb_end >> 18, inst=inst)
      self.adev.wreg_pair(f"reg{ip}MC_VM_SYSTEM_APERTURE_DEFAULT_ADDR", "_LSB", "_MSB", self.memscratch_xgmi_paddr >> 12, inst=inst)
      self.adev.wreg_pair(f"reg{ip}VM_L2_PROTECTION_FAULT_DEFAULT_ADDR", "_LO32", "_HI32", self.dummy_page_xgmi_paddr >> 12, inst=inst)

      self.adev.reg(f"reg{ip}VM_L2_PROTECTION_FAULT_CNTL2").update(active_page_migration_pte_read_retry=1, inst=inst)

      # Init TLB and cache
      self.adev.reg(f"reg{ip}MC_VM_MX_L1_TLB_CNTL").update(enable_l1_tlb=1, system_access_mode=3, enable_advanced_driver_model=1,
        system_aperture_unmapped_access=0, mtype=self.adev.soc.module.MTYPE_UC, inst=inst)

      self.adev.reg(f"reg{ip}VM_L2_CNTL").update(enable_l2_cache=1, enable_default_page_out_to_system_memory=1,
        l2_pde0_cache_tag_generation_mode=0, pde_fault_classification=0, context1_identity_access_mode=1, identity_mode_fragment_size=0,
        enable_l2_fragment_processing=int(self.adev.ip_ver[am.GC_HWIP] < (10,0,0)), inst=inst)
      self.adev.reg(f"reg{ip}VM_L2_CNTL2").update(invalidate_all_l1_tlbs=1, invalidate_l2_cache=1, inst=inst)
      self.adev.reg(f"reg{ip}VM_L2_CNTL3").write(l2_cache_4k_associativity=1, l2_cache_bigk_associativity=1,
        bank_select=12 if self.trans_futher else 9, l2_cache_bigk_fragment_size=9 if self.trans_futher else 6, inst=inst)
      self.adev.reg(f"reg{ip}VM_L2_CNTL4").write(l2_cache_4k_partition_count=1, inst=inst)
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
      if is_table and pte_lv == am.AMDGPU_VM_PDB1: extra |= am.AMDGPU_PDE_BFS(0x9)
      if is_table and pte_lv == am.AMDGPU_VM_PDB0: extra |= am.AMDGPU_PTE_TF
      if not is_table and pte_lv not in {am.AMDGPU_VM_PTB, am.AMDGPU_VM_PDB0}: extra |= am.AMDGPU_PDE_PTE
    return extra
  def is_pte_huge_page(self, pte_lv, pte):
    if self.adev.ip_ver[am.GC_HWIP] < (10,0,0): return (pte & am.AMDGPU_PDE_PTE) if pte_lv != am.AMDGPU_VM_PDB0 else not (pte & am.AMDGPU_PTE_TF)
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
  VEGA20_TABLE_MAX = 0x4000    # AM's single driver table (init_sw below); every Vega 20 table transfer goes through it (design v2 §3 row AM_SMU)
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
    self.driver_table_paddr = self.adev.mm.palloc(0x4000, zero=False, boot=True)
    if self._vega20():
      self.tools_table_paddr = self.adev.mm.palloc(self.VEGA20_TOOLS_SIZE, zero=False, boot=True)  # design B13 step (2), vega20_smumgr.c:467-479
      self._vega20_results = {v: k for k, v in vars(self.smu_mod).items() if k.startswith("PPSMC_Result_")}  # vega20_ppsmc.h:30-34 names for errors

  def init_hw(self):
    if self._vega20(): return self._vega20_init_hw()
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrHigh, hi32(self.adev.paddr2mc(self.driver_table_paddr)))
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrLow, lo32(self.adev.paddr2mc(self.driver_table_paddr)))
    self._send_msg(self.smu_mod.PPSMC_MSG_EnableAllSmuFeatures, 0)

  def _vega20_init_hw(self):
    # Design v2 B13 "Vega 20 init_hw" (1)-(4) = Linux vega20_start_smu (vega20_smumgr.c:589-604) and nothing more:
    #  (1) SMC running (:593-596; Linux reads MP1_FIRMWARE_FLAGS :49-62, AM's is_smu_alive = GetSmuVersion 0x2 then C2PMSG_90 != 0, design B13(1));
    #  (2) vega20_set_tools_address (:598 -> :375-394): SetToolsDramAddrHigh 0x12 / SetToolsDramAddrLow 0x13 with the 0x19000-byte PMSTATUSLOG buffer;
    #  (3) SetDriverDramAddrHigh 0x10 / SetDriverDramAddrLow 0x11 with AM's driver table (Linux form: vega20_set_pptable_driver_address :396-416;
    #      the address is re-sent before every transfer anyway, see read_table/write_table);
    #  (4) NO EnableAllSmuFeatures 0x6 here: Linux sends it only inside vega20_enable_dpm_tasks after SetAllowedFeaturesMask + the pptable upload
    #      (vega20_hwmgr.c:1700-1723) = enable_dpm_full below, which is NOT called at boot (design step 12 decision, §8 U-S1; the no-DPM path
    #      hwmgr_hw_init returns before any message when pm_en is false, hwmgr.c:232-236).
    if not self.is_smu_alive():
      raise RuntimeError(f"am {self.adev.devfmt}: Vega 20 SMC is not running (no answer to GetSmuVersion 0x2); "
                         "vega20_start_smu refuses too (vega20_smumgr.c:593-596)")
    tools_mc = self.adev.paddr2mc(self.tools_table_paddr)
    self._send_msg(self.smu_mod.PPSMC_MSG_SetToolsDramAddrHigh, hi32(tools_mc))  # vega20_smumgr.c:382-385
    self._send_msg(self.smu_mod.PPSMC_MSG_SetToolsDramAddrLow, lo32(tools_mc))   # vega20_smumgr.c:387-390
    self._vega20_set_driver_dram_addr()

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

  # --- Vega 20 table transfer (vega20_smumgr.c:163-246) ---
  def _vega20_set_driver_dram_addr(self):
    mc = self.adev.paddr2mc(self.driver_table_paddr)
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrHigh, hi32(mc))  # vega20_smumgr.c:178-183 (read) / :228-233 (write)
    self._send_msg(self.smu_mod.PPSMC_MSG_SetDriverDramAddrLow, lo32(mc))   # vega20_smumgr.c:184-189 (read) / :234-239 (write)

  def _vega20_check_table(self, table_t, table_id:int) -> int:
    # vega20_copy_table_from_smc / _to_smc guards (vega20_smumgr.c:171-176 / :216-221): table id < TABLE_COUNT; the id sits in bits 15:0, bits 31:16
    # carry a workload type for TABLE_ACTIVITY_MONITOR_COEFF (:275, :305). AM has one driver table, so the struct must fit it.
    if not (0 <= (table_id & 0xffff) < self.smu_mod.TABLE_COUNT):
      raise ValueError(f"am {self.adev.devfmt}: SMU table id {table_id:#x} not < TABLE_COUNT {self.smu_mod.TABLE_COUNT} (smu11_driver_if.h:834-844)")
    if (sz:=ctypes.sizeof(table_t)) > self.VEGA20_TABLE_MAX:
      raise ValueError(f"am {self.adev.devfmt}: {table_t.__name__} ({sz} B) exceeds the {self.VEGA20_TABLE_MAX:#x}-byte driver table")
    return sz

  def _vega20_read_table(self, table_t, table_id:int):
    # vega20_copy_table_from_smc (vega20_smumgr.c:163-201): SetDriverDramAddrHigh/Low with the table address (:178-189), TransferTableSmu2Dram 0x14
    # with the id (:190-193), amdgpu_hdp_invalidate (:195) = hdp_v4_0_invalidate_hdp (amdgpu/hdp_v4_0.c:39-54; HDP is 4.2.0 on Vega 20,
    # amdgpu_discovery.c:3210, so not the 4.4.x early return :42-45): write HDP_READ_CACHE_INVALIDATE = 1 and read it back (:48-49);
    # then memcpy (:197-198).
    sz = self._vega20_check_table(table_t, table_id)
    self._vega20_set_driver_dram_addr()
    self._send_msg(self.smu_mod.PPSMC_MSG_TransferTableSmu2Dram, table_id)
    self.adev.regHDP_READ_CACHE_INVALIDATE.write(1)  # hdp_4_0 table, resolved through AMDev's reg->mm fallback (amdev.py:303-308)
    self.adev.regHDP_READ_CACHE_INVALIDATE.read()
    return table_t.from_buffer(bytearray(self.adev.vram.view(self.driver_table_paddr, sz)[:]))

  def write_table(self, table, table_id:int):
    # vega20_copy_table_to_smc (vega20_smumgr.c:208-246): memcpy into the table buffer (:223-224), amdgpu_hdp_flush (:226) = amdgpu_hdp_generic_flush
    # (hdp_v4_0.c:172 -> amdgpu/amdgpu_hdp.c:51-60: write REMAP_HDP_MEM_FLUSH_CNTL = 0 (:55-58) = AM_GMC.flush_hdp (ip.py:89), then nbio get_memsize
    # (:59-60) = nbio_v7_4_get_memsize = read RCC_CONFIG_MEMSIZE (amdgpu/nbio_v7_4.c:133-136)), SetDriverDramAddrHigh/Low (:228-239),
    # TransferTableDram2Smu 0x15 with the id (:240-243).
    self._vega20_only("write_table")
    sz = self._vega20_check_table(type(table), table_id)
    self.adev.vram.view(self.driver_table_paddr, sz)[:] = bytes(table)
    self.adev.gmc.flush_hdp()
    self.adev.regRCC_CONFIG_MEMSIZE.read()  # nbio_7_4 table (reg->mm fallback)
    self._vega20_set_driver_dram_addr()
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

  def od_defaults(self):
    # vega20_od8_initialize_default_settings, curve part (vega20_hwmgr.c:1281-1316): read the OD table (:1264), GfxclkFreq1 = GfxclkFmin (:1282),
    # GfxclkFreq3 = GfxclkFmax (:1286), GfxclkFreq2 = (Freq1 + Freq3) / 2 (:1290), GfxclkVolt_i = vega20_od8_get_gfx_clock_base_voltage(Freq_i)
    # (:1294-1316 -> :1227-1245: GetAVFSVoltageByDpm 0x5F, reply / VOLTAGE_SCALE (:1242), stored back * VOLTAGE_SCALE (:1299-1300, :1307-1308,
    # :1315-1316)). Linux runs the curve part only when the VBIOS OD8 capabilities include OD8_GFXCLK_CURVE (:1281; caps from the pptable,
    # :1047-1139) and writes the table back at :1392; that capability is unknown here (design v2 §6), so this returns the table and does not
    # write it — `write_od(od_defaults())` is Linux's :1392.
    self._vega20_only("od_defaults")
    od = self.read_od()
    od.GfxclkFreq1, od.GfxclkFreq3 = od.GfxclkFmin, od.GfxclkFmax
    od.GfxclkFreq2 = (od.GfxclkFreq1 + od.GfxclkFreq3) // 2
    for i, freq in ((1, od.GfxclkFreq1), (2, od.GfxclkFreq2), (3, od.GfxclkFreq3)):
      v = self._send_msg(self.smu_mod.PPSMC_MSG_GetAVFSVoltageByDpm,
                         (self.VEGA20_AVFS_CURVE << 24) | (self.VEGA20_OD8_HOTCURVE_TEMPERATURE << 16) | freq, read_back_arg=True)  # :1234-1237
      setattr(od, f"GfxclkVolt{i}", (v // self.VEGA20_VOLTAGE_SCALE) * self.VEGA20_VOLTAGE_SCALE)  # :1242 then :1299-1300 / :1307-1308 / :1315-1316
    return od

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

  def _vega20_read_clocks(self, clks) -> dict[int, list[int]]:
    # vega20_setup_single_dpm_table (vega20_hwmgr.c:568-591): GetDpmFreqByIndex 0x2C with (clk << 16 | 0xFF) = number of levels (:536-550), with
    # (clk << 16 | i) = level i in MHz (:552-566). Linux takes the reply as-is (no bit-31 mask, unlike AM's read_clocks) and re-reads after an OD
    # commit (:3139-3155), so this is not cached.
    out: dict[int, list[int]] = {}
    for clk in clks:
      n = self._send_msg(self.smu_mod.PPSMC_MSG_GetDpmFreqByIndex, (clk << 16) | 0xff, read_back_arg=True)
      out[clk] = [self._send_msg(self.smu_mod.PPSMC_MSG_GetDpmFreqByIndex, (clk << 16) | i, read_back_arg=True) for i in range(n)]
    return out

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

  def _vega20_set_clocks(self, level:int|None) -> dict[int, tuple[int, int]]:
    # vega20_force_dpm_highest / _lowest (vega20_hwmgr.c:2392-2475): soft_min = soft_max = the chosen level's MHz for GFXCLK, UCLK, SOCCLK
    # (:2399-2415), uploaded by vega20_upload_dpm_min_level (SetSoftMinByFreq 0x26, param (PPCLK << 16) | (freq & 0xffff), :1820-1919) and THEN
    # vega20_upload_dpm_max_level (SetSoftMaxByFreq 0x27, same param, :1921-2009) with FEATURE_DPM_GFXCLK|UCLK|SOCCLK (:2417-2426); a clock is
    # sent only when its DPM feature is enabled (:1827-1828, :1838-1839, :1882-1883 / :1928-1929, :1940-1941). level None = vega20_unforce_dpm_levels
    # (:2477-2532): soft_min = lowest, soft_max = highest level. GC 9.4.0 therefore gets BOTH messages (design v2 §3 row AM_SMU; the upstream
    # `GC >= (10,0,0)` gate above was never for Vega). Returns {PPCLK: (min MHz, max MHz)} actually sent; empty when no DPM feature is enabled
    # (then Linux has no clock lever either, vega20_hwmgr.c:1827/:1928 skip).
    features = self.get_features()
    m = self.smu_mod
    enabled = [clk for clk, mask in ((m.PPCLK_GFXCLK, m.FEATURE_DPM_GFXCLK_MASK), (m.PPCLK_UCLK, m.FEATURE_DPM_UCLK_MASK),
                                     (m.PPCLK_SOCCLK, m.FEATURE_DPM_SOCCLK_MASK)) if features & mask]
    levels = self._vega20_read_clocks(enabled)
    want: dict[int, tuple[int, int]] = {}
    for clk, vals in levels.items():
      if not vals: raise RuntimeError(f"am {self.adev.devfmt}: SMU reports 0 DPM levels for PPCLK {clk} (GetDpmFreqByIndex 0x2C | 0xff)")
      if level is not None and not (-len(vals) <= level < len(vals)):
        raise ValueError(f"am {self.adev.devfmt}: level {level} out of range for PPCLK {clk} with {len(vals)} levels")
      want[clk] = (vals[0], vals[-1]) if level is None else (vals[level], vals[level])
    for clk, (lo, _) in want.items(): self._send_msg(self.smu_mod.PPSMC_MSG_SetSoftMinByFreq, (clk << 16) | (lo & 0xffff))  # :2417-2422
    for clk, (_, hi) in want.items(): self._send_msg(self.smu_mod.PPSMC_MSG_SetSoftMaxByFreq, (clk << 16) | (hi & 0xffff))  # :2424-2429
    if DEBUG >= 2: print(f"am {self.adev.devfmt}: Vega 20 soft min/max set: {want}")
    return want

  def set_power_limit(self, watts:float):
    ppt_limit = max(int(round(watts)), 1)
    if self._vega20() and not (self.get_features() & self.smu_mod.FEATURE_PPT_MASK):
      # vega20_set_power_limit sends SetPptLimit only while FEATURE_PPT is enabled and otherwise silently returns 0 (vega20_powertune.c:32-43);
      # a silent no-op would leave the card uncapped, so the fork raises instead (design v2 §6 "SetPptLimit (if the PPT feature is on)").
      raise RuntimeError(f"am {self.adev.devfmt}: SetPptLimit needs FEATURE_PPT (bit {self.smu_mod.FEATURE_PPT_BIT}) enabled; "
                         "it is not (vega20_powertune.c:37-42)")
    self._send_msg(self.smu_mod.PPSMC_MSG_SetPptLimit, ppt_limit)
    if DEBUG >= 2: print(f"am {self.adev.devfmt}: GPU power limit set to {ppt_limit}W")

  def enable_dpm_full(self, pptable:bytes, allowed_features:int, dcef_boot_clk_mhz:int, pcie_overrides:list[tuple[int, int, int]],
                      fclk_gfxclk_ratio:int=0, od_percent:int|None=None) -> tuple[int, int]:
    # NOT called at boot (design v2 step 12 decision, §8 U-S1). Replays Linux vega20_enable_dpm_tasks (vega20_hwmgr.c:1693-1774) message by message;
    # every value Linux takes from the VBIOS / registry is a required input here (no defaults invented):
    #   pptable          = sizeof(PPTable_t) bytes, the VBIOS powerplay table Linux memcpy's at :823 (design B4; §8 U-S1)
    #   allowed_features = 64-bit mask for SetAllowedFeaturesMaskHigh/Low (Linux builds it from registry data + pptable, :933-941)
    #   dcef_boot_clk_mhz= VBIOS bootup DCEF clock; Linux sends dcef_clock / 100 (10 kHz units -> MHz, :818-821)
    #   pcie_overrides   = [(link level, gen 0..3, width 1..7)] for OverridePcieParameters (:872-891; Linux derives them from the host link
    #                      masks :850-870); [] = none
    #   fclk_gfxclk_ratio= registry default 0 (:168) for SetFclkGfxClkRatio (:1023-1026)
    #   od_percent       = OverDriveSetPercentage (vega20_powertune.c:51-72), sent only when given (Linux: only with
    #                      PHM_PlatformCaps_PowerContainment, :63)
    # Steps that only read (setup_default_dpm_tables :1743, init_max_sustainable_clocks :1748) are not replayed; od8 defaults (:1758) are the
    # caller's `write_od(od_defaults())` because the OD8 capabilities come from the VBIOS (:1047-1139).
    # Returns (enabled feature mask, default PPT limit).
    self._vega20_only("enable_dpm_full")
    if len(pptable) != (sz:=ctypes.sizeof(self.smu_mod.PPTable_t)):
      raise ValueError(f"pptable must be sizeof(PPTable_t) = {sz} bytes, got {len(pptable)}")
    if not (0 <= allowed_features < (1 << 64)): raise ValueError("allowed_features must be a 64-bit mask")
    if dcef_boot_clk_mhz <= 0: raise ValueError("dcef_boot_clk_mhz must be the VBIOS bootup DCEF clock in MHz (> 0)")
    for lvl, gen, width in pcie_overrides:
      if not (0 <= lvl <= 0xffff and 0 <= gen <= 3 and 1 <= width <= 7):
        raise ValueError(f"pcie override {(lvl, gen, width)}: level, gen 0..3, width 1..7 (vega20_hwmgr.c:872-874)")
    m = self.smu_mod
    self._send_msg(m.PPSMC_MSG_NumOfDisplays, 0)                                                   # :1697-1698
    self._send_msg(m.PPSMC_MSG_SetAllowedFeaturesMaskHigh, hi32(allowed_features))                # :943-947 (High first)
    self._send_msg(m.PPSMC_MSG_SetAllowedFeaturesMaskLow, lo32(allowed_features))                 # :949-953
    self._send_msg(m.PPSMC_MSG_SetMinDeepSleepDcefclk, dcef_boot_clk_mhz)                         # :818-821
    self.write_table(m.PPTable_t.from_buffer_copy(pptable), m.TABLE_PPTABLE)                      # :823-829
    self._send_msg(m.PPSMC_MSG_RunBtc, 0)                                                         # :958-961 (no parameter in Linux)
    self._send_msg(m.PPSMC_MSG_RunAfllBtc, 0)                                                     # :963-966 (no parameter in Linux)
    self._send_msg(m.PPSMC_MSG_EnableAllSmuFeatures, 0)                                           # :977-981 (no parameter in Linux)
    features = self.get_features()                                                                # :983-986
    for lvl, gen, width in pcie_overrides: self._send_msg(m.PPSMC_MSG_OverridePcieParameters, (lvl << 16) | (gen << 8) | width)  # :884-887
    if features & m.FEATURE_DPM_UCLK_MASK: self._send_msg(m.PPSMC_MSG_SetUclkFastSwitch, 1)      # :1009-1013
    self._send_msg(m.PPSMC_MSG_SetFclkGfxClkRatio, fclk_gfxclk_ratio)                             # :1023-1026
    if od_percent is not None: self._send_msg(m.PPSMC_MSG_OverDriveSetPercentage, od_percent & 0xffffffff)  # vega20_powertune.c:54-56
    return features, self.get_power_limit()                                                       # :1765-1771

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
  def init_sw(self):
    self.xccs = sum(1 for i in self.adev.regs_offset[am.GC_HWIP] if i not in self.adev.harvested[am.GC_HWIP])
    self.mqd_paddr = [self.adev.mm.palloc(0x1000 * self.xccs, zero=False, boot=True) for i in range(2)]
    self.mqd_mc = [self.adev.paddr2mc(mqd_paddr) for mqd_paddr in self.mqd_paddr]

  def init_hw(self):
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

    for xcc in range(self.xccs if aql else 1):
      self._grbm_select(me=1, pipe=pipe, queue=queue, inst=xcc)

      struct_t = getattr(am, f"struct_v{self.adev.ip_ver[am.GC_HWIP][0]}{'_compute' if self.adev.ip_ver[am.GC_HWIP][0] >= 10 else ''}_mqd")
      mqd_struct = struct_t(header=0xC0310800, cp_mqd_base_addr_lo=lo32(self.mqd_mc[queue] + 0x1000*xcc),
        cp_mqd_base_addr_hi=hi32(self.mqd_mc[queue] + 0x1000*xcc), cp_hqd_pipe_priority=0x2, cp_hqd_queue_priority=0xf, cp_hqd_quantum=0x111,
        cp_hqd_persistent_state=self.adev.regCP_HQD_PERSISTENT_STATE.encode(preload_size=0x55, preload_req=1),
        cp_hqd_pq_base_lo=lo32(ring_addr>>8), cp_hqd_pq_base_hi=hi32(ring_addr>>8),
        cp_hqd_pq_rptr_report_addr_lo=lo32(rptr_addr), cp_hqd_pq_rptr_report_addr_hi=hi32(rptr_addr),
        cp_hqd_pq_wptr_poll_addr_lo=lo32(wptr_addr), cp_hqd_pq_wptr_poll_addr_hi=hi32(wptr_addr),
        cp_hqd_pq_doorbell_control=self.adev.regCP_HQD_PQ_DOORBELL_CONTROL.encode(doorbell_offset=doorbell*2, doorbell_en=1),
        cp_hqd_pq_control=self.adev.regCP_HQD_PQ_CONTROL.encode(rptr_block_size=5, unord_dispatch=0, queue_size=(ring_size//4).bit_length()-2,
          **({'queue_full_en':1, 'slot_based_wptr':2, 'no_update_rptr':xcc!=0 or self.xccs==1} if aql else {})),
        cp_hqd_ib_control=self.adev.regCP_HQD_IB_CONTROL.encode(min_ib_avail_size=0x3), cp_hqd_hq_status0=0x20004000,
        cp_mqd_control=self.adev.regCP_MQD_CONTROL.encode(priv_state=1), cp_hqd_vmid=0, cp_hqd_aql_control=int(aql),
        cp_hqd_eop_base_addr_lo=lo32(eop_addr>>8), cp_hqd_eop_base_addr_hi=hi32(eop_addr>>8),
        cp_hqd_eop_control=self.adev.regCP_HQD_EOP_CONTROL.encode(eop_size=(eop_size//4).bit_length()-2),
        **({'compute_tg_chunk_size':1, 'compute_current_logic_xcc_id':xcc, 'cp_mqd_stride_size':0x1000} if aql and self.xccs > 1 else {}))
      for se in range(8 if self.adev.ip_ver[am.GC_HWIP][0] >= 10 else 4): setattr(mqd_struct, f'compute_static_thread_mgmt_se{se}', 0xffffffff)

      self.adev.vram.view(self.mqd_paddr[queue] + 0x1000*xcc, ctypes.sizeof(mqd_struct))[:] = memoryview(mqd_struct).cast('B')

      mqd_st_mv = to_mv(ctypes.addressof(mqd_struct), ctypes.sizeof(mqd_struct)).cast('I')
      for i, reg in enumerate(range(self.adev.regCP_MQD_BASE_ADDR.addr[xcc], self.adev.regCP_HQD_PQ_WPTR_HI.addr[xcc] + 1)):
        self.adev.wreg(reg, mqd_st_mv[0x80 + i])
      self.adev.regCP_HQD_ACTIVE.write(0x1, inst=xcc)

      self.adev.gmc.flush_hdp()
      self._grbm_select(inst=xcc)
    return doorbell

  def set_clockgating_state(self):
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
      if self.adev.ip_ver[am.GC_HWIP] < (10,0,0):
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

  def init_hw(self):
    for ring_vm, rwptr_vm, suf, ring_id in self.rings:
      self.adev.wreg_pair("regIH_RB_BASE", suf, f"_HI{suf}", self.adev.paddr2mc(ring_vm) >> 8)

      self.adev.reg(f"regIH_RB_CNTL{suf}").write(mc_space=4, wptr_overflow_clear=1, rb_size=((self.ring_size//4)-1).bit_length(),
        mc_snoop=1, mc_ro=0, mc_vmid=0, **({'wptr_overflow_enable': 1, 'rptr_rearm': 1} if ring_id == 0 else {'rb_full_drain_enable': 1}))

      if ring_id == 0: self.adev.wreg_pair("regIH_RB_WPTR_ADDR", "_LO", "_HI", self.adev.paddr2mc(rwptr_vm))

      self.adev.reg(f"regIH_RB_WPTR{suf}").write(0)
      self.adev.reg(f"regIH_RB_RPTR{suf}").write(0)

      self.adev.reg(f"regIH_DOORBELL_RPTR{suf}").write(enable=0)

    if self.adev.ip_ver[am.OSSSYS_HWIP] != (4,4,2):
      self.adev.regIH_STORM_CLIENT_LIST_CNTL.update(client18_is_storm_client=1)
      self.adev.regIH_INT_FLOOD_CNTL.update(flood_cntl_enable=1)
      self.adev.regIH_MSI_STORM_CTRL.update(delay=3)

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
  def init_sw(self): self.sdma_reginst, self.sdma_name = [], "F32" if self.adev.ip_ver[am.SDMA0_HWIP] < (7,0,0) else "MCU"
  def init_hw(self):
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
  def init_sw(self):
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
    self.boot_time_tmr = self.adev.ip_ver[am.MP0_HWIP] in {(13,0,6), (13,0,14), (14,0,2), (14,0,3)}
    self.autoload_tmr = self.adev.ip_ver[am.MP0_HWIP] not in {(13,0,6), (13,0,14)}
    self.tmr_paddr = self.adev.mm.palloc(self.max_tmr_size, align=am.PSP_TMR_ALIGNMENT, zero=False, boot=True) if not self.boot_time_tmr else 0

  def init_hw(self):
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

  def is_sos_alive(self): return self.adev.reg(f"{self.reg_pref}_81").read() != 0x0

  def _wait_for_bootloader(self): wait_cond(lambda: self.adev.reg(f"{self.reg_pref}_35").read() & 0x80000000, value=0x80000000, msg="BL not ready")

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
    wait_cond(lambda: self.adev.reg(f"{self.reg_pref}_64").read() & 0x80000000, value=0x80000000, msg="sOS not ready")

    self.adev.wreg_pair(self.reg_pref, "_69", "_70", self.adev.paddr2mc(self.ring_paddr))
    self.adev.reg(f"{self.reg_pref}_71").write(self.ring_size)
    self.adev.reg(f"{self.reg_pref}_64").write(am.PSP_RING_TYPE__KM << 16)

    # There might be handshake issue with hardware which needs delay
    time.sleep(0.02)

    wait_cond(lambda: self.adev.reg(f"{self.reg_pref}_64").read() & 0x8000FFFF, value=0x80000000, msg="sOS ring not created")

  def _ring_submit(self, cmd:am.struct_psp_gfx_cmd_resp) -> am.struct_psp_gfx_cmd_resp:
    msg = am.struct_psp_gfx_rb_frame(fence_value=(prev_wptr:=self.adev.reg(f"{self.reg_pref}_67").read()) + 1,
      cmd_buf_addr_lo=lo32(self.adev.paddr2mc(self.cmd_paddr)), cmd_buf_addr_hi=hi32(self.adev.paddr2mc(self.cmd_paddr)),
      fence_addr_lo=lo32(self.adev.paddr2mc(self.fence_paddr)), fence_addr_hi=hi32(self.adev.paddr2mc(self.fence_paddr)))

    self.adev.vram.view(self.cmd_paddr, ctypes.sizeof(cmd))[:] = memoryview(cmd).cast('B')
    self.adev.vram.view(self.ring_paddr + prev_wptr * 4, ctypes.sizeof(msg))[:] = memoryview(msg).cast('B')

    # Move the wptr
    self.adev.reg(f"{self.reg_pref}_67").write(prev_wptr + ctypes.sizeof(am.struct_psp_gfx_rb_frame) // 4)

    wait_cond(lambda: self.adev.vram.view(self.fence_paddr, 4, 'I')[0], value=msg.fence_value, msg="sOS ring not responding")

    resp = type(cmd).from_buffer(bytearray(self.adev.vram.view(self.cmd_paddr, ctypes.sizeof(cmd))[:]))
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
