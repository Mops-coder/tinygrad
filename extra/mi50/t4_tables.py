#!/usr/bin/env python3
"""T4 (design v2 §7.1): offline check of the generated Vega 20 (MI50, gfx906) modules. Needs no card.

Checks: (1) import_module lands on the right table for every (prefix, ip_ver) the existing cards and the Vega 20 use;
(2) every register/field name the design (Vega20 Port Design v2 §2/§3/§6) and the Linux CHIP_VEGA20 code use resolves in its table,
and the names the design says are ABSENT are absent; (3) the design's dword addresses fall out of table offset + vega_offsets base;
(4) smu_11_0_2: PPSMC message numbers / PPCLK / TABLE ids against vega20_ppsmc.h + smu11_driver_if.h, and sizeof/offsetof against a
C compile of those headers (clang); (5) clearstate_gfx9 entry count / first / last entries against clearstate_gfx9.h.
Run: PYTHONPATH=<fork> python3 extra/mi50/t4_tables.py  [env MI50_LINUX_AMD=<linux>/drivers/gpu/drm/amd for the C compile, default below]
"""
import os, re, sys, subprocess, tempfile, pathlib, ctypes
from tinygrad.runtime.autogen.am import regs, smu_11_0_2 as smu, clearstate_gfx9 as cs, vega_offsets as vo
from tinygrad.runtime.support.amd import import_module

LINUX = pathlib.Path(os.getenv("MI50_LINUX_AMD", "/private/tmp/claude-501/-Users-leonh-Documents-Ai-shit/bba39989-2505-4a2d-a9f5-db421196e2ba/"
                                                 "scratchpad/repos/linux/drivers/gpu/drm/amd"))
fails: list[str] = []
def check(cond, msg):
  print(("ok   " if cond else "FAIL ") + msg)
  if not cond: fails.append(msg)
def table_name(mod): return next(c for c in regs.__all__ if getattr(regs, c) is mod)
def lookup(tab, name):  # the AMDIP/AMDev rule: exact name, else reg* -> mm* (support/amd.py AMDIP.__getattr__, am/amdev.py:303)
  return tab.get(name) or tab.get(name.replace("reg", "mm", 1))

# (1) lookups: the existing cards (gfx11 RX 7600 XT = 11.0.2, MI300 = 9.4.3 ...) must land where they did before this fork; Vega 20 on its tables
expect = {("gc",(11,0,2)):"gc_11_0_0", ("gc",(11,0,0)):"gc_11_0_0", ("gc",(11,0,3)):"gc_11_0_3", ("gc",(11,5,0)):"gc_11_5_0", ("gc",(12,0,0)):"gc_12_0_0",
  ("gc",(9,4,3)):"gc_9_4_3", ("gc",(9,5,0)):"gc_9_4_3", ("gc",(9,4,0)):"gc_9_0",
  ("nbio",(7,9,0)):"nbio_7_9_0", ("nbio",(7,11,0)):"nbio_7_11_0", ("nbio",(7,7,0)):"nbio_7_7_0", ("nbio",(4,3,0)):"nbio_4_3_0", ("nbio",(7,2,0)):"nbio_7_2_0",
  ("nbio",(7,4,0)):"nbio_7_4", ("nbif",(6,3,1)):"nbif_6_3_1", ("sdma",(4,4,2)):"sdma_4_4_2", ("sdma",(4,4,4)):"sdma_4_4_2", ("sdma",(4,2,0)):"sdma_4_2",
  ("mp",(13,0,7)):"mp_13_0_0", ("mp",(13,0,0)):"mp_13_0_0", ("mp",(13,0,6)):"mp_13_0_0", ("mp",(14,0,2)):"mp_14_0_2", ("mp",(11,0,2)):"mp_11_0_0",
  ("hdp",(6,0,0)):"hdp_6_0_0", ("hdp",(4,4,2)):"hdp_4_4_2", ("hdp",(7,0,0)):"hdp_7_0_0", ("hdp",(4,2,0)):"hdp_4_0",
  ("osssys",(6,0,0)):"osssys_6_0_0", ("osssys",(4,4,2)):"osssys_4_4_2", ("osssys",(6,1,0)):"osssys_6_1_0", ("osssys",(7,0,0)):"osssys_7_0_0",
  ("osssys",(4,2,0)):"osssys_4_2_0", ("mmhub",(1,8,0)):"mmhub_1_8_0", ("mmhub",(3,0,0)):"mmhub_3_0_0", ("mmhub",(3,0,1)):"mmhub_3_0_1",
  ("mmhub",(3,0,2)):"mmhub_3_0_2", ("mmhub",(3,3,0)):"mmhub_3_3_0", ("mmhub",(4,1,0)):"mmhub_4_1_0", ("mmhub",(9,4,0)):"mmhub_1_0",
  ("athub",(9,4,0)):"athub_1_0", ("thm",(11,0,2)):"thm_11_0_2", ("smuio",(11,0,2)):"smuio_11_0_0"}
for (p, v), want in expect.items(): check(table_name(import_module(p, v, submod="regs")) == want, f"import_module({p}, {v}) -> {want}")
for v, want in {(13,0,7):"smu_13_0_0", (13,0,0):"smu_13_0_0", (13,0,6):"smu_13_0_6", (13,0,12):"smu_13_0_12", (14,0,2):"smu_14_0_2", (11,0,2):"smu_11_0_2"}.items():
  check(import_module("smu", v).__name__.endswith(want), f"import_module(smu, {v}) -> {want}")

# (2) names. gc: golden tables gfx_v9_0.c:676-697/722-734/815-819, init_compute_vmid :2613-2624, get_cu_active_bitmap :7881-7882, select_se_sh :2523,
# rlc stop/start/resume :3124-3244, init_csb :2777-2781, gfx_v9_4_init_lbpw :1763-1802, wait_for_rlc_serdes :2727/2749, init_always_on_cu_mask :1690/1700,
# cp_gfx_enable :3248, cp_compute_enable :3465, mqd_init/kiq_init_register :3550-3798, soft_reset :4205-4241, kiq_reset_hw_queue :1058; design B6 (MC_VM_FB_OFFSET),
# B10 (MC_VM_MX_L1_TLB_CNTL.atc_en, VM_L2_CNTL*, VM_CONTEXT0_CNTL), B14 (CP_STAT, SCRATCH_REG6/7, GB_ADDR_CONFIG, SH_MEM_*, TCP_CNTL, CP_INT_CNTL(_RING0)).
# Vega-era headers are mm-prefixed; the driver asks with reg* (ip.py) -> both spellings are tried, like AMDIP.__getattr__.
names = {
 "gc_9_0": """DB_DEBUG2 DB_DEBUG3 GB_GPU_ID PA_SC_BINNER_EVENT_CNTL_3 PA_SC_ENHANCE PA_SC_LINE_STIPPLE_STATE SH_MEM_CONFIG SPI_RESOURCE_RESERVE_CU_0
   SPI_RESOURCE_RESERVE_CU_1 SPI_RESOURCE_RESERVE_EN_CU_0 SPI_RESOURCE_RESERVE_EN_CU_1 SQC_CONFIG TA_CNTL_AUX TCP_CHAN_STEER_HI TCP_CHAN_STEER_LO
   VGT_CACHE_INVALIDATION VGT_GS_MAX_WAVE_ID CP_MEC1_F32_INT_DIS CP_MEC2_F32_INT_DIS CP_DEBUG CB_DCC_CONFIG CB_HW_CONTROL_2 CB_HW_CONTROL_3 GB_ADDR_CONFIG
   GB_ADDR_CONFIG_READ PA_SC_ENHANCE_1 RMI_UTCL1_CNTL2 SPI_CONFIG_CNTL_1 TD_CNTL CP_SD_CNTL GRBM_CAM_INDEX GRBM_CAM_DATA
   SH_MEM_BASES GDS_VMID0_BASE GDS_VMID0_SIZE GDS_GWS_VMID0 GDS_OA_VMID0 CC_GC_SHADER_ARRAY_CONFIG GC_USER_SHADER_ARRAY_CONFIG GRBM_GFX_INDEX GRBM_CNTL
   RLC_CNTL RLC_CGCG_CGLS_CTRL RLC_CSIB_ADDR_HI RLC_CSIB_ADDR_LO RLC_CSIB_LENGTH RLC_SRM_CNTL RLC_SPM_MC_CNTL RLC_SAFE_MODE RLC_JUMP_TABLE_RESTORE
   RLC_LB_THR_CONFIG_1 RLC_LB_THR_CONFIG_2 RLC_LB_THR_CONFIG_3 RLC_LB_THR_CONFIG_4 RLC_LB_CNTR_INIT RLC_LB_CNTR_MAX RLC_LB_INIT_CU_MASK RLC_LB_PARAMS RLC_LB_CNTL
   RLC_GPM_GENERAL_7 RLC_GPM_GENERAL_6 RLC_GPM_GENERAL_12 RLC_GPM_TIMER_INT_3 RLC_SERDES_CU_MASTER_BUSY RLC_SERDES_NONCU_MASTER_BUSY RLC_SERDES_WR_CU_MASTER_MASK
   RLC_SERDES_WR_NONCU_MASTER_MASK RLC_SERDES_WR_CTRL RLC_PG_ALWAYS_ON_CU_MASK RLC_LB_ALWAYS_ACTIVE_CU_MASK RLC_CP_SCHEDULERS RLC_CGTT_MGCG_OVERRIDE
   CP_INT_CNTL_RING0 CP_INT_CNTL CP_ME_CNTL CP_MEC_CNTL CP_MEC_DOORBELL_RANGE_LOWER CP_MEC_DOORBELL_RANGE_UPPER CP_PQ_WPTR_POLL_CNTL CP_PQ_STATUS CP_RB_WPTR_POLL_CNTL
   CP_MQD_BASE_ADDR CP_MQD_BASE_ADDR_HI CP_MQD_CONTROL CP_HQD_ACTIVE CP_HQD_VMID CP_HQD_PERSISTENT_STATE CP_HQD_PQ_BASE CP_HQD_PQ_BASE_HI CP_HQD_PQ_CONTROL
   CP_HQD_PQ_DOORBELL_CONTROL CP_HQD_PQ_RPTR CP_HQD_PQ_RPTR_REPORT_ADDR CP_HQD_PQ_RPTR_REPORT_ADDR_HI CP_HQD_PQ_WPTR_LO CP_HQD_PQ_WPTR_HI CP_HQD_PQ_WPTR_POLL_ADDR
   CP_HQD_PQ_WPTR_POLL_ADDR_HI CP_HQD_EOP_BASE_ADDR CP_HQD_EOP_BASE_ADDR_HI CP_HQD_EOP_CONTROL CP_HQD_IB_CONTROL CP_HQD_QUANTUM CP_HQD_DEQUEUE_REQUEST CP_HQD_HQ_STATUS0
   CP_STAT GRBM_SOFT_RESET GRBM_STATUS GRBM_STATUS2 SPI_COMPUTE_QUEUE_RESET SCRATCH_REG6 SCRATCH_REG7 TCP_CNTL
   GCMC_VM_FB_OFFSET GCMC_VM_MX_L1_TLB_CNTL GCVM_L2_CNTL GCVM_L2_CNTL2 GCVM_L2_CNTL3 GCVM_L2_CNTL4 GCVM_CONTEXT0_CNTL GCVM_L2_PROTECTION_FAULT_CNTL
   GCVM_L2_PROTECTION_FAULT_STATUS GCVM_INVALIDATE_ENG17_REQ GCVM_INVALIDATE_ENG17_ACK GCMC_VM_FB_LOCATION_BASE GCMC_VM_FB_LOCATION_TOP""",
 # gmc_v9_0.c:333-336 golden_settings_mmhub_1_0_0; mmhub_v1_0.c gart_enable/set_fault_enable_default; ip.py:62-63 FB_LOCATION
 "mmhub_1_0": "DAGB1_WRCLI2 MMEA1_DRAM_WR_CLI2GRP_MAP0 MMMC_VM_FB_LOCATION_BASE MMMC_VM_FB_LOCATION_TOP MMMC_VM_MX_L1_TLB_CNTL MMVM_L2_CNTL MMVM_L2_CNTL3 MMVM_L2_CNTL4 "
               "MMVM_CONTEXT0_CNTL MMVM_L2_PROTECTION_FAULT_CNTL MMVM_L2_PROTECTION_FAULT_STATUS MMVM_INVALIDATE_ENG17_REQ MMVM_INVALIDATE_ENG17_ACK",
 # (mmhub_1_0 names its clock-gating register mmATC_L2_MISC_CG, mmhub_1_0_offset.h:1277 - not MM_ATC_L2_MISC_CG; ip.py:355 hasattr-guards it; clock gating is U-G4, not bring-up)
 "athub_1_0": "RPB_ARB_CNTL RPB_ARB_CNTL2",                                    # gmc_v9_0.c:338-341 golden_settings_athub_1_0_0
 # nbio_v7_4.c (design B8/B9/B11/B4): doorbell apertures, HDP flush, ih_control, memsize, BIOS scratch; + the gfx11-named aliases ops_amd/ip.py use (§5.4)
 "nbio_7_4": "RCC_DOORBELL_APER_EN BIF_SDMA0_DOORBELL_RANGE BIF_SDMA1_DOORBELL_RANGE BIF_IH_DOORBELL_RANGE DOORBELL_SELFRING_GPA_APER_BASE_LOW "
             "DOORBELL_SELFRING_GPA_APER_BASE_HIGH DOORBELL_SELFRING_GPA_APER_CNTL REMAP_HDP_MEM_FLUSH_CNTL GPU_HDP_FLUSH_REQ GPU_HDP_FLUSH_DONE INTERRUPT_CNTL "
             "INTERRUPT_CNTL2 RCC_CONFIG_MEMSIZE BIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL PCIE_INDEX2 PCIE_DATA2 BIOS_SCRATCH_0 BIOS_SCRATCH_7 BIF_FB_EN "
             "regBIF_BX_PF0_GPU_HDP_FLUSH_REQ regBIF_BX_PF0_GPU_HDP_FLUSH_DONE regBIF_BX0_REMAP_HDP_MEM_FLUSH_CNTL regBIF_BX0_PCIE_INDEX2 regBIF_BX0_PCIE_DATA2 "
             "regRCC_DEV0_EPF0_RCC_DOORBELL_APER_EN regBIF_BX0_BIF_DOORBELL_INT_CNTL",
 "hdp_4_0": "HDP_MISC_CNTL HDP_NONSURFACE_BASE HDP_NONSURFACE_BASE_HI HDP_READ_CACHE_INVALIDATE HDP_MEM_POWER_LS",       # hdp_v4_0.c:83-158 (design B9)
 # vega20_ih.c:244-388 (design B11); retry-CAM registers listed as skipped but present; IH_MSI_STORM_CTRL absent (checked below)
 "osssys_4_2_0": "IH_RB_CNTL IH_RB_BASE IH_RB_BASE_HI IH_RB_RPTR IH_RB_WPTR IH_RB_WPTR_ADDR_LO IH_RB_WPTR_ADDR_HI IH_DOORBELL_RPTR IH_STORM_CLIENT_LIST_CNTL "
                 "IH_INT_FLOOD_CNTL IH_DOORBELL_RETRY_CAM IH_RETRY_INT_CAM_CNTL IH_RB_CNTL_RING1 IH_RB_CNTL_RING2 IH_CHICKEN IH_CLK_CTRL",
 # sdma_v4_0.c:201-261 golden, :980 ctx_switch_enable, :1043 enable, :1088 gfx_resume, :1381 start, :594-606 ULV (0x66a1; late_init :1789), :2030 is_idle (design B15, T10)
 "sdma_4_2": "SDMA_CHICKEN_BITS SDMA_CLK_CTRL SDMA_GB_ADDR_CONFIG SDMA_GB_ADDR_CONFIG_READ SDMA_GFX_RB_RPTR_ADDR_LO SDMA_GFX_RB_WPTR_POLL_CNTL SDMA_PAGE_RB_RPTR_ADDR_LO "
             "SDMA_PAGE_RB_WPTR_POLL_CNTL SDMA_RD_BURST_CNTL SDMA_UTCL1_PAGE SDMA_UTCL1_TIMEOUT SDMA_RLC0_RB_RPTR_ADDR_LO SDMA_RLC0_RB_WPTR_POLL_CNTL "
             "SDMA_F32_CNTL SDMA_CNTL SDMA_SEM_WAIT_FAIL_TIMER_CNTL SDMA_ULV_CNTL SDMA_STATUS_REG SDMA_GFX_RB_CNTL SDMA_GFX_RB_RPTR SDMA_GFX_RB_WPTR SDMA_GFX_RB_RPTR_ADDR_HI "
             "SDMA_GFX_RB_BASE SDMA_GFX_RB_BASE_HI SDMA_GFX_MINOR_PTR_UPDATE SDMA_GFX_DOORBELL SDMA_GFX_DOORBELL_OFFSET SDMA_GFX_RB_WPTR_POLL_ADDR_LO "
             "SDMA_GFX_RB_WPTR_POLL_ADDR_HI SDMA_GFX_IB_CNTL SDMA_GFX_RB_WPTR_POLL_CNTL SDMA_PAGE_RB_CNTL SDMA_POWER_CNTL",
 # psp_v11_0.c:168-389 mailboxes 35/36/81/64/69/70/71/67; vega20_smumgr.c:70-156 66/82/90 (design B12/B13)
 "mp_11_0_0": "regMP0_SMN_C2PMSG_35 regMP0_SMN_C2PMSG_36 regMP0_SMN_C2PMSG_64 regMP0_SMN_C2PMSG_67 regMP0_SMN_C2PMSG_69 regMP0_SMN_C2PMSG_70 regMP0_SMN_C2PMSG_71 "
              "regMP0_SMN_C2PMSG_81 regMP1_SMN_C2PMSG_66 regMP1_SMN_C2PMSG_82 regMP1_SMN_C2PMSG_90",
 "thm_11_0_2": "CG_MULT_THERMAL_STATUS CG_THERMAL_STATUS CG_FDO_CTRL0 CG_FDO_CTRL1 CG_FDO_CTRL2 CG_TACH_CTRL CG_TACH_STATUS THM_THERMAL_INT_ENA THM_THERMAL_INT_CTRL",  # §6
 "smuio_11_0_0": "ROM_INDEX ROM_DATA SMUSVI0_TEL_PLANE0",                                                              # B4 ROM dump, §6 VDDGFX
}
for t, ns in names.items():
  tab = getattr(regs, t); missing = [n for n in ns.split() if lookup(tab, n if n.startswith("reg") else "mm" + n) is None]
  check(not missing, f"{t}: {len(ns.split())} names resolve" + (f"; MISSING {missing}" if missing else ""))
# fields the design relies on
fields = [("gc_9_0","mmGCMC_VM_MX_L1_TLB_CNTL","atc_en"), ("gc_9_0","mmCP_PQ_STATUS","doorbell_enable"), ("gc_9_0","mmCP_PQ_WPTR_POLL_CNTL","en"),
  ("gc_9_0","mmGCVM_L2_CNTL4","vmc_tap_pde_request_physical"), ("gc_9_0","mmGCVM_L2_CNTL4","vmc_tap_pte_request_physical"),
  ("gc_9_0","mmSH_MEM_BASES","private_base"), ("gc_9_0","mmSH_MEM_BASES","shared_base"), ("gc_9_0","mmRLC_CGTT_MGCG_OVERRIDE","gfxip_mgls_override"),
  ("gc_9_0","mmCP_HQD_PERSISTENT_STATE","preload_size"), ("gc_9_0","mmCC_GC_SHADER_ARRAY_CONFIG","inactive_cus"),
  ("mmhub_1_0","mmMMVM_L2_CNTL4","vmc_tap_pde_request_physical"), ("mmhub_1_0","mmMMVM_L2_CNTL4","vmc_tap_pte_request_physical"),
  ("osssys_4_2_0","mmIH_STORM_CLIENT_LIST_CNTL","client18_is_storm_client"), ("osssys_4_2_0","mmIH_INT_FLOOD_CNTL","flood_cntl_enable"),
  ("osssys_4_2_0","mmIH_RB_CNTL","rptr_rearm"), ("sdma_4_2","mmSDMA_GFX_RB_WPTR_POLL_CNTL","f32_poll_enable"), ("sdma_4_2","mmSDMA_ULV_CNTL","hysteresis"),
  ("sdma_4_2","mmSDMA_STATUS_REG","idle"), ("sdma_4_2","mmSDMA_CNTL","auto_ctxsw_enable"), ("sdma_4_2","mmSDMA_CNTL","utc_l1_enable"), ("sdma_4_2","mmSDMA_F32_CNTL","halt"),
  ("thm_11_0_2","mmCG_MULT_THERMAL_STATUS","ctf_temp"), ("thm_11_0_2","mmCG_MULT_THERMAL_STATUS","asic_max_temp"), ("thm_11_0_2","mmCG_THERMAL_STATUS","fdo_pwm_duty"),
  ("thm_11_0_2","mmCG_FDO_CTRL1","fmax_duty100"), ("thm_11_0_2","mmCG_FDO_CTRL0","fdo_static_duty"), ("thm_11_0_2","mmCG_FDO_CTRL2","fdo_pwm_mode"), ("thm_11_0_2","mmCG_FDO_CTRL2","tmin"),
  ("smuio_11_0_0","mmSMUSVI0_TEL_PLANE0","svi0_plane0_vddcor"), ("nbio_7_4","mmRCC_DOORBELL_APER_EN","bif_doorbell_aper_en"),
  ("nbio_7_4","mmBIF_SDMA0_DOORBELL_RANGE","offset"), ("nbio_7_4","mmBIF_SDMA0_DOORBELL_RANGE","size"), ("nbio_7_4","mmINTERRUPT_CNTL","ih_dummy_rd_override"),
  ("nbio_7_4","mmINTERRUPT_CNTL","ih_req_nonsnoop_en"), ("hdp_4_0","mmHDP_MISC_CNTL","flush_invalidate_cache")]
for t, r, f in fields:
  e = getattr(regs, t).get(r); check(e is not None and f in e[2], f"{t}.{r}.{f} = {e[2].get(f) if e else None}")
# names the design states are absent on Vega 20 (B14.1, B17, B11, B6, B14.5, B16); if one appears the design's gates need re-reading
absent = [("gc_9_0","mmRLC_RLCS_BOOTLOAD_STATUS"), ("gc_9_0","mmCOMPUTE_PGM_RSRC3"), ("gc_9_0","mmGCMC_VM_XGMI_LFB_CNTL"), ("osssys_4_2_0","mmIH_MSI_STORM_CTRL")]
for t, r in absent: check(r not in getattr(regs, t), f"{t}: {r} absent (as the design states)")
check("f8_mode" not in regs.gc_9_0["mmSH_MEM_CONFIG"][2], "gc_9_0 SH_MEM_CONFIG has no f8_mode field (design B14.5)")
check(not {"gfxip_rep_fgcg_override", "gfxip_fgcg_override"} & set(regs.gc_9_0["mmRLC_CGTT_MGCG_OVERRIDE"][2]), "gc_9_0 RLC_CGTT_MGCG_OVERRIDE lacks the fgcg fields (design B16)")

# (3) dword addresses the design computes (B2/B4/B6/§1.7/§6): table offset + the vega_offsets base of its BASE_IDX segment
def dword(t, r, bases): off, seg, _ = getattr(regs, t)[r]; return bases[seg] + off
nbio_b = (vo.NBIO_BASE__INST0_SEG0, vo.NBIO_BASE__INST0_SEG1, vo.NBIO_BASE__INST0_SEG2)
for t, r, bases, want in [("thm_11_0_2","mmCG_MULT_THERMAL_STATUS",(vo.THM_BASE__INST0_SEG0,),0x1665f), ("smuio_11_0_0","mmROM_INDEX",(vo.SMUIO_BASE__INST0_SEG0,),0x168e4),
    ("smuio_11_0_0","mmROM_DATA",(vo.SMUIO_BASE__INST0_SEG0,),0x168e5), ("smuio_11_0_0","mmSMUSVI0_TEL_PLANE0",(vo.SMUIO_BASE__INST0_SEG0,),0x16804),
    ("gc_9_0","mmGCMC_VM_FB_OFFSET",(vo.GC_BASE__INST0_SEG0,),0x296b), ("nbio_7_4","mmRCC_CONFIG_MEMSIZE",nbio_b,0xde3), ("nbio_7_4","mmBIOS_SCRATCH_0",nbio_b,0x4c),
    ("nbio_7_4","mmBIOS_SCRATCH_7",nbio_b,0x53), ("nbio_7_4","mmGPU_HDP_FLUSH_REQ",nbio_b,0xe26), ("sdma_4_2","mmSDMA_STATUS_REG",(vo.SDMA0_BASE__INST0_SEG0,),0x1285)]:
  check((d:=dword(t, r, bases)) == want, f"{t}.{r} dword {d:#x} (design {want:#x})")
l2c4 = regs.gc_9_0["mmGCVM_L2_CNTL4"][2]; tap = sum(((1 << (hi-lo+1)) - 1) << lo for lo, hi in (l2c4["vmc_tap_pde_request_physical"], l2c4["vmc_tap_pte_request_physical"]))
print(f"info VM_L2_CNTL4 value for B10 = mmVM_L2_CNTL4_DEFAULT 0xc1 (mmhub_1_0_default.h:673) & ~tap_fields {tap:#x} = {0xc1 & ~tap:#x}")
print(f"info CG_MULT_THERMAL_STATUS fields {regs.thm_11_0_2['mmCG_MULT_THERMAL_STATUS'][2]}; CG_THERMAL_STATUS {regs.thm_11_0_2['mmCG_THERMAL_STATUS'][2]}")

# (4) smu_11_0_2: vega20_ppsmc.h:37-124 message numbers, smu11_driver_if.h:316-329 PPCLK_e, :834-844 TABLE_*; then the C compile
msgs = dict(GetSmuVersion=0x2, SetDriverDramAddrHigh=0x10, SetDriverDramAddrLow=0x11, TransferTableSmu2Dram=0x14, TransferTableDram2Smu=0x15, GetDpmFreqByIndex=0x2C,
  SetSoftMinByFreq=0x26, SetSoftMaxByFreq=0x27, SetPptLimit=0xF, GetPptLimit=0x53, GetEnabledSmuFeaturesLow=0xC, GetEnabledSmuFeaturesHigh=0xD, SetToolsDramAddrHigh=0x12,
  SetToolsDramAddrLow=0x13, GetAVFSVoltageByDpm=0x5F, OverDriveSetPercentage=0x35, GetCurrentRpm=0x3C, GetDpmClockFreq=0x2D, EnableAllSmuFeatures=0x6, DisableSmuFeaturesLow=0xA,
  EnableSmuFeaturesLow=0x8, SoftReset=0x22, PrepareMp1ForReset=0x59, NumOfDisplays=0x47, SetAllowedFeaturesMaskLow=0x4, SetAllowedFeaturesMaskHigh=0x5, RunBtc=0x18, RunAfllBtc=0x57,
  OverridePcieParameters=0x34, SetFanTemperatureTarget=0x3F, GetMinDpmFreq=0x2A, GetMaxDpmFreq=0x2B, GetDcModeMaxDpmFreq=0x54)
for m, v in msgs.items(): check(getattr(smu, f"PPSMC_MSG_{m}", None) == v, f"smu_11_0_2 PPSMC_MSG_{m} == {v:#x}")
check(all(getattr(smu, f"PPSMC_Result_{k}", None) == v for k, v in dict(OK=0x1, Failed=0xFF, UnknownCmd=0xFE, CmdRejectedPrereq=0xFD, CmdRejectedBusy=0xFC).items()), "smu_11_0_2 PPSMC_Result_* (vega20_ppsmc.h:30-34)")
check(not hasattr(smu, "PPSMC_MSG_Mode1Reset") and not hasattr(smu, "PPSMC_MSG_GetMetricsTable"), "smu_11_0_2 has no Mode1Reset / GetMetricsTable (design §1.6, §6)")
check([getattr(smu, f"PPCLK_{c}") for c in "GFXCLK VCLK DCLK ECLK SOCCLK UCLK DCEFCLK DISPCLK PIXCLK PHYCLK FCLK COUNT".split()] == list(range(12)), "smu_11_0_2 PPCLK_e order GFXCLK..FCLK, COUNT=11")
check([getattr(smu, f"TABLE_{c}") for c in "PPTABLE WATERMARKS AVFS AVFS_PSM_DEBUG AVFS_FUSE_OVERRIDE PMSTATUSLOG SMU_METRICS DRIVER_SMU_CONFIG ACTIVITY_MONITOR_COEFF OVERDRIVE COUNT".split()] == list(range(11)), "smu_11_0_2 TABLE_* ids 0..9, COUNT=10")
for s in ["SmuMetrics_t", "OverDriveTable_t", "PPTable_t", "Watermarks_t", "DpmActivityMonitorCoeffInt_t", "DriverSmuConfig_t", "AvfsFuseOverride_t"]:
  print(f"info sizeof {s}: ctypes {ctypes.sizeof(getattr(smu, s))}, SIZE {getattr(smu, s).SIZE}")
check(ctypes.sizeof(smu.PPTable_t) <= 0x4000, f"PPTable_t ({ctypes.sizeof(smu.PPTable_t)} B) fits AM's 0x4000-byte driver table (ip.py:181)")
C_SRC = r'''
#include <stdint.h>
#include <stdio.h>
#include <stddef.h>
#define bool _Bool
#include "vega20_ppsmc.h"
#include "smu11_driver_if.h"
#define S(t) printf("sizeof %s %zu\n", #t, sizeof(t))
#define O(t,f) printf("offsetof %s %s %zu\n", #t, #f, offsetof(t, f))
#define D(m) printf("define %s %u\n", #m, (unsigned)(m))
int main(void) {
  S(SmuMetrics_t); S(OverDriveTable_t); S(PPTable_t); S(Watermarks_t); S(DpmActivityMonitorCoeffInt_t); S(DriverSmuConfig_t); S(AvfsFuseOverride_t);
  O(SmuMetrics_t, CurrClock); O(SmuMetrics_t, AverageGfxclkFrequency); O(SmuMetrics_t, CurrSocketPower); O(SmuMetrics_t, TemperatureEdge); O(SmuMetrics_t, TemperatureHotspot);
  O(SmuMetrics_t, TemperatureHBM); O(SmuMetrics_t, ThrottlerStatus); O(SmuMetrics_t, LinkDpmLevel); O(SmuMetrics_t, AverageSocketPower); O(SmuMetrics_t, MmHubPadding);
  O(OverDriveTable_t, GfxclkFmin); O(OverDriveTable_t, UclkFmax); O(OverDriveTable_t, OverDrivePct); O(OverDriveTable_t, FanZeroRpmEnable); O(OverDriveTable_t, Padding);
  O(PPTable_t, Version); O(PPTable_t, FeaturesToRun); O(PPTable_t, SocketPowerLimitAc0); O(PPTable_t, TedgeLimit); O(PPTable_t, ThotspotLimit); O(PPTable_t, ThbmLimit);
  O(PPTable_t, FanStopTemp); O(PPTable_t, FanStartTemp); O(PPTable_t, FanPwmMin); O(PPTable_t, MmHubPadding);
  D(PPSMC_MSG_GetSmuVersion); D(PPSMC_MSG_SetDriverDramAddrHigh); D(PPSMC_MSG_TransferTableSmu2Dram); D(PPSMC_MSG_GetDpmFreqByIndex); D(PPSMC_MSG_SetSoftMaxByFreq);
  D(PPSMC_MSG_SetPptLimit); D(PPSMC_MSG_GetPptLimit); D(PPSMC_MSG_SetToolsDramAddrLow); D(PPSMC_MSG_PrepareMp1ForReset); D(PPSMC_Message_Count); D(PPCLK_FCLK); D(PPCLK_COUNT);
  D(TABLE_SMU_METRICS); D(TABLE_OVERDRIVE); D(TABLE_COUNT); D(FEATURE_PPT_BIT); D(FEATURE_FAN_CONTROL_BIT); D(FEATURE_DPM_GFXCLK_BIT); D(FEATURE_DPM_UCLK_BIT); D(PPTABLE_V20_SMU_VERSION);
  return 0; }
'''
inc = LINUX / "pm/powerplay/inc"
if not (inc / "smu11_driver_if.h").is_file(): check(False, f"C compile: {inc}/smu11_driver_if.h not found (set MI50_LINUX_AMD); sizeof NOT verified")
else:
  with tempfile.TemporaryDirectory() as td:
    (pathlib.Path(td) / "t.c").write_text(C_SRC)
    r = subprocess.run(["clang", "-std=c11", f"-I{inc}", f"{td}/t.c", "-o", f"{td}/t"], capture_output=True, text=True)
    check(r.returncode == 0, "C compile of vega20_ppsmc.h + smu11_driver_if.h" + ("" if r.returncode == 0 else f": {r.stderr[:400]}"))
    out = subprocess.run([f"{td}/t"], capture_output=True, text=True, check=True).stdout if r.returncode == 0 else ""
  n = 0
  for line in out.splitlines():
    kind, *a = line.split(); n += 1
    if kind == "sizeof": check(ctypes.sizeof(getattr(smu, a[0])) == int(a[1]) == getattr(smu, a[0]).SIZE, f"C sizeof({a[0]}) = {a[1]} == ctypes.sizeof == SIZE")
    elif kind == "offsetof":
      off = next(f[2] for f in getattr(smu, a[0])._real_fields_ if f[0] == a[1]); check(off == int(a[2]), f"C offsetof({a[0]}, {a[1]}) = {a[2]} == module {off}")
    elif kind == "define": check(getattr(smu, a[0], None) == int(a[1]), f"C {a[0]} = {a[1]} == module {getattr(smu, a[0], None)}")
  expected = len(re.findall(r"\b[SOD]\((?!t\b|m\b)", C_SRC))  # one output line per S()/O()/D() use in main()
  check(n == expected, f"C check produced {n} lines (expected {expected})")

# (5) clearstate_gfx9: clearstate_gfx9.h:919-928 (8 extents), :930-933 (one SECT_CONTEXT section); values :25 DB_RENDER_CONTROL=0, :38 PA_SC_SCREEN_SCISSOR_BR=0x40004000,
# :917 CB_COLOR7_DCC_BASE_EXT=0; clearstate_defs.h:26-31 enum; csb dword count per gfx_v9_0.c:1624-1651 get_csb_size
check((cs.SECT_NONE, cs.SECT_CONTEXT, cs.SECT_CLEAR, cs.SECT_CTRLCONST) == (0, 1, 2, 3), "clearstate section ids")
check(len(cs.gfx9_cs_data) == 1 and cs.gfx9_cs_data[0][0] == cs.SECT_CONTEXT, "gfx9_cs_data = one SECT_CONTEXT section")
ext = cs.gfx9_cs_data[0][1]
check([(c, i) for c, i, _ in ext] == [(212,0xa000),(282,0xa0d6),(4,0xa1f5),(157,0xa200),(2,0xa2a0),(1,0xa2a3),(66,0xa2a5),(155,0xa2f5)], f"8 extents (reg_count, reg_index): {[(c, hex(i)) for c, i, _ in ext]}")
check(all(len(v) == c for c, _, v in ext), "every extent holds reg_count values")
check(ext[0][2][0] == 0 and ext[0][2][13] == 0x40004000 and ext[-1][2][-1] == 0, f"first value {ext[0][2][0]:#x}, PA_SC_SCREEN_SCISSOR_BR {ext[0][2][13]:#x}, last value {ext[-1][2][-1]:#x}")
print(f"info csb size per gfx_v9_0_get_csb_size = 2+3+sum(2+reg_count)+2+2 = {2+3+sum(2+c for c,_,_ in ext)+2+2} dwords; total clear-state regs {sum(c for c,_,_ in ext)}")
hdr = LINUX / "amdgpu/clearstate_gfx9.h"
if hdr.is_file():  # independent re-parse of the local header, compared value for value
  t = hdr.read_text(); arrays = {m.group(1): [int(v, 0) for v in re.sub(r'//[^\n]*', '', m.group(2)).split(',') if v.strip()] for m in re.finditer(r'unsigned int (\w+)\[\] = \{(.*?)\};', t, re.S)}
  lin = [(int(c), int(i, 0), tuple(arrays[a])) for a, i, c in re.findall(r'\{(gfx9_\w+), (0x[0-9a-f]+), (\d+) \}', t)]
  check(lin == ext, f"module data == independent parse of {hdr.name} ({len(lin)} extents, {sum(len(v) for *_, v in lin)} values)")
else: print(f"info {hdr} not present: header re-parse skipped (structural checks above still ran)")

print(f"\n{'ALL CHECKS PASSED' if not fails else f'{len(fails)} FAILED'}: {len(fails)} failures")
sys.exit(1 if fails else 0)
