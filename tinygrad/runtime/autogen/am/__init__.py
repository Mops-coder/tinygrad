import pathlib, hashlib, re, itertools
from tinygrad.runtime.autogen import load, root

__all__ = ["am", "pm4_soc15", "pm4_nv", "sdma_4_0_0", "sdma_5_0_0", "sdma_6_0_0", "smu_13_0_0", "smu_13_0_6", "smu_13_0_12", "smu_14_0_2",
           "fw", "navi_offsets", "vega_offsets", "regs", "soc_9", "soc_11", "soc_12", "pmc",
           "smu_11_0_2", "clearstate_gfx9"]  # mi50 fork: Vega 20 SMU (powerplay vega20_ppsmc.h + smu11_driver_if.h), RLC clear-state (clearstate_gfx9.h)

am_src="https://github.com/ROCm/ROCK-Kernel-Driver/archive/33970e1351f5e511029602454979f3de7e22260f.tar.gz"
rocm_src="https://github.com/ROCm/rocm-systems/archive/cccc350dc620e61ae2554978b62ab3532dc10bd9.tar.gz"
AMD, AMDINC = "{}/drivers/gpu/drm/amd", "{}/drivers/gpu/drm/amd/include"
inc, kern_rules = ["-include", "stdint.h"], [(r'le32_to_cpu', ''),]
fw_src="https://gitlab.com/kernel-firmware/linux-firmware/-/archive/0a6871b19abf5d6e024b5d208b101ae53e7fa0de/0a6871b19abf5d6e024b5d208b101ae53e7fa0de.tar.gz"
pmc_src="https://raw.githubusercontent.com/ROCm/rocm-systems/cccc350dc620e61ae2554978b62ab3532dc10bd9/projects/rocprofiler-compute/src/rocprof_compute_soc/profile_configs/counter_defs.yaml"

reg_files = {
  "gc": [(9,4,3), (11,0,0), (11,0,3), (11,5,0), (12,0,0)],
  "mmhub": [(1,8,0), (3,0,0), (3,0,1), (3,0,2), (3,3,0), (4,1,0)],
  "nbio": [(4,3,0), (7,2,0), (7,7,0), (7,9,0), (7,11,0)], "nbif": [(6,3,1)],
  "mp": [(11,0,0), (13,0,0), (14,0,2)], "hdp": [(4,4,2), (6,0,0), (7,0,0)],
  "osssys": [(4,4,2), (6,0,0), (6,1,0), (7,0,0)], "sdma": [(4,4,2)],
  # mi50 fork (Vega 20 / gfx906, Leon 2026-10-11): the headers Linux uses for CHIP_VEGA20 (gfx_v9_0.c, mmhub_v1_0.c, nbio_v7_4.c,
  # hdp_v4_0.c, vega20_ih.c, sdma_v4_0.c, vega20_thermal.c, athub_v1_0.c, smuio_v11_0.c). import_module picks the newest listed
  # version <= the card's, so gc_9_0 serves GC 9.4.0, hdp_4_0 serves 4.2.0, osssys_4_2_0 serves 4.2.0 (the header vega20_ih.c:30-31
  # includes; design v2 §5.2), nbio_7_4 serves 7.4.0 (nbio_7_2_0 would have been picked silently otherwise), smuio_11_0_0 serves 11.0.2
  # (smuio_v11_0.c:25-26); mmhub 9.4.0 -> mmhub_1_0 and athub 9.4.0 -> athub_1_0 (athub_v1_0.c:26-27) need the overrides in
  # support/amd.py import_module.
  "thm": [(11,0,2)],
  "athub": [(1,0)], "smuio": [(11,0,0)],
}
reg_files["gc"] += [(9,0)]; reg_files["mmhub"] += [(1,0)]; reg_files["nbio"] += [(7,4)]; reg_files["hdp"] += [(4,0)]; reg_files["osssys"] += [(4,2,0)]
reg_files["sdma"] += [(4,2)]
# mi50 fork: Vega 20's SDMA header lives at asic_reg/sdma0/sdma0_4_2 (names mmSDMA0_*); it is generated as module "sdma_4_2" with the
# names normalized to SDMA_* so the driver's regSDMA_* lookups work and import_module("sdma", (4,2,0)) picks it (and (4,4,2) still gets sdma_4_4_2)
reg_dirs = {("sdma", (4,2)): "sdma0/sdma0_4_2"}
reg_renames = {"sdma0_4_2": "sdma_4_2"}

reg_patterns = {
  "gc": ["GCVM", "GCMC_VM", "CP_(HQD|MQD|MEC|ME_CNTL|PERFMON|RB_WPTR_POLL_CNTL|INT_CNTL|STAT|PFP_PRGRM|ME_PRGRM|COHER_START)", "COMPUTE_",
         "(SQ|GL2C|TCC)_PERFCOUNTER", "SQ_THREAD_TRACE", "SPI_(CONFIG_CNTL|COMPUTE_QUEUE_RESET)", "GRBM", "SH_MEM", "RLC", "TCP", "GB_ADDR_CONFIG",
         "SDMA[01]_(WATCHDOG_CNTL|UTCL1_(CNTL|PAGE)|MCU_CNTL|F32_CNTL|CNTL|QUEUE0_|RLC_CGCG_CTRL)", "SCRATCH_REG[67]"],
  "mmhub": ["MMVM", "MMMC_VM", "MM_ATC_L2_MISC_CG"],
  "nbio": (nbio:=["BIF_BX_PF[01]_GPU_HDP_FLUSH", "BIF_BX_PF0_RSMU", "BIF_BX0_(REMAP_HDP_MEM_FLUSH_CNTL|BIF_DOORBELL_INT_CNTL|PCIE_INDEX2|PCIE_DATA2)",
                  "BIFC_(DOORBELL_ACCESS_EN_PF|GFX_INT_MONITOR_MASK)", "XCC_DOORBELL_FENCE", "DOORBELL0_CTRL_ENTRY", "GDC_S2A0_S2A_DOORBELL_ENTRY",
                  "S2A_DOORBELL_ENTRY", "RCC_DEV0_EPF0_RCC_DOORBELL_APER_EN", "RCC_DEV0_EPF2_STRAP2"]),
  "nbif": nbio,
  "mp": ["MP([01]|ASP)_SMN_C2PMSG"], "hdp": ["HDP_MEM_POWER_CTRL"], "oss": ["IH_"], "sdma": ["SDMA_"],  # mi50 fork: whole SDMA block (was SDMA_GFX, SDMA_CNTL)
  # mi50 fork: Vega 20 SDMA registers live in their own sdma0_4_2 header (both instances share the map; SDMA1 = SDMA0 + base
  # from the ip-offset table), THM is read directly for temperature/fan (vega20_thermal.c) when no SMU is running
  "thm": ["CG_(MULT_THERMAL_STATUS|THERMAL_(INT|STATUS|RANGE|CTRL)|FDO_CTRL|TACH)", "THM_TMON", "THM_THERMAL_INT"],  # thm_11_0_2_offset.h:44-47
  # mi50 fork: gmc_v9_0.c:338-341 golden_settings_athub_1_0_0 (RPB_ARB_CNTL, RPB_ARB_CNTL2; athub_1_0_offset.h:398-401)
  "athub": ["RPB_ARB_CNTL"],
  # mi50 fork: smuio_v11_0.c:28-36 ROM_INDEX/ROM_DATA (VBIOS dump, smuio_11_0_0_offset.h:230-233); SMUSVI0_TEL_PLANE0 = VDDGFX telemetry
  # (smuio_11_0_0_offset.h:28-29, sh_mask :30 VDDCOR_MASK; read by vega20_hwmgr.c:2281-2283, vid -> mV in smu_helper.c:43)
  "smuio": ["ROM_(INDEX|DATA)", "SMUSVI0_TEL_PLANE0"],
}
# mi50 fork: Vega-20-only allowlist additions, keyed by TABLE name so that no other card's table changes (the shared patterns above
# apply to every version of a family). Every name = a register Linux writes on CHIP_VEGA20 (design v2 §3 row autogen, §5.2):
reg_patterns_extra = {
  "gc_9_0": [
    # gfx_v9_0.c:676-697 golden_settings_gc_9_0, :722-734 golden_settings_gc_9_0_vg20, :815-819 golden_settings_gc_9_x_common
    # (TCP_CHAN_STEER_*, GRBM_CAM_*, CP_MEC[12]_F32_INT_DIS, SH_MEM_CONFIG, GB_ADDR_CONFIG*, SPI_CONFIG_CNTL_1 already match "TCP"/"GRBM"/
    # "CP_(…MEC…)"/"SH_MEM"/"GB_ADDR_CONFIG"/"SPI_(CONFIG_CNTL…)" above)
    "DB_DEBUG", "GB_GPU_ID", "PA_SC_", "SPI_", "SQC_CONFIG", "TA_CNTL_AUX", "VGT_", "CP_DEBUG", "CB_", "RMI_UTCL1_CNTL2", "TD_CNTL", "CP_SD_CNTL",
    "GDS_",                               # gfx_v9_0.c:2621-2624 init_compute_vmid: GDS_VMID0_BASE/SIZE, GDS_GWS_VMID0, GDS_OA_VMID0
    "CP_PQ_(STATUS|WPTR_POLL_CNTL)",      # gfx_v9_0.c:3693 CP_PQ_WPTR_POLL_CNTL.EN=0, :3795 CP_PQ_STATUS.DOORBELL_ENABLE=1 (kiq_init_register)
    "CC_GC_SHADER_ARRAY_CONFIG", "GC_USER_SHADER_ARRAY_CONFIG",  # gfx_v9_0.c:7881-7882 get_cu_active_bitmap
  ],
  "mmhub_1_0": ["DAGB1_WRCLI2", "MMEA1_DRAM_WR_CLI2GRP_MAP0"],  # gmc_v9_0.c:333-336 golden_settings_mmhub_1_0_0 (mmhub_1_0_offset.h:376-377, 838-839)
  "nbio_7_4": ["BIOS_SCRATCH_[0-7]"],  # amdgpu_atombios.c:1634-1642 POST bit = BIOS_SCRATCH_7 bit 9 (nbio_7_4_offset.h:2480-2495); T2 reads 0x4c..0x53
}
# mi50 fork: the nbio/hdp allowlists above are written for the gfx11/MI300 header naming (BIF_BX_PF0_*, HDP_MEM_POWER_CTRL); the
# Vega 20 headers (nbio_7_4, hdp_4_0) name the same functions differently. Names = what Linux nbio_v7_4.c / hdp_v4_0.c use.
reg_patterns["nbio"] = reg_patterns["nbio"] + ["BIF_(FB_EN|DOORBELL_INT_CNTL|IH_DOORBELL_RANGE|SDMA[01]_DOORBELL_RANGE|MMSCH[01]_DOORBELL_RANGE|INTR_CNTL)",
  "DOORBELL_SELFRING_GPA_APER", "GPU_HDP_FLUSH_(REQ|DONE)", "INTERRUPT_CNTL", "PCIE_(INDEX|DATA)2", "RCC_CONFIG_MEMSIZE",
  "RCC_DEV0_EPF0_(STRAP0|RCC_DOORBELL_APER_EN)", "RCC_DOORBELL_APER_EN", "REMAP_HDP_(MEM|REG)_FLUSH_CNTL", "BIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL",
  "HDP_MEM_COHERENCY_FLUSH_CNTL", "BACO_CNTL"]
reg_patterns["nbif"] = reg_patterns["nbio"]
reg_patterns["hdp"] = reg_patterns["hdp"] + ["HDP_(EDC_CNT|MEM_POWER_LS|NONSURFACE_BASE|READ_CACHE_INVALIDATE|HOST_PATH_CNTL|MISC_CNTL|MMHUB_CNTL|MEM_COHERENCY_FLUSH_CNTL)"]

soc_patterns = ["SQ_TT", "VGT_EVENT_TYPE", "CS", "MTYPE", "SH"]

def __getattr__(nm):
  match nm:
    case "am": return load("am/am", [root/f"extra/amdpci/headers/{s}.h" for s in ["v11_structs", "v12_structs", "amdgpu_vm",
      "discovery", "amdgpu_ucode", "psp_gfx_if", "amdgpu_psp", "amdgpu_irq", "amdgpu_doorbell"]] + [f"{AMD}/amdkfd/soc15_int.h"] + \
      [f"{AMDINC}/ivsrcid/{s}.h" for s in [f"gfx/irqsrcs_gfx_{x}_0" for x in ('9','11_0','12_0')] + [f"sdma0/irqsrcs_sdma0_{x}_0" for x in (4,5)]] + \
      [f"{AMDINC}/{s}.h" for s in ["v9_structs", "soc15_ih_clientid"]], args=inc, srcs=am_src, rules=kern_rules)
    case "pm4_soc15": return load("am/pm4_soc15", [f"{AMD}/amdkfd/kfd_pm4_headers_ai.h", f"{AMD}/amdgpu/soc15d.h"], srcs=am_src)
    case "pm4_nv": return load("am/pm4_nv", [f"{AMD}/amdkfd/kfd_pm4_headers_ai.h", f"{AMD}/amdgpu/nvd.h"], srcs=am_src)
    case "sdma_4_0_0": return load("am/sdma_4_0_0", [root/"extra/hip_gpu_driver/sdma_registers.h", f"{AMD}/amdgpu/vega10_sdma_pkt_open.h"],
                                   args=["-I/opt/rocm/include", "-x", "c++"], srcs=am_src)
    case "sdma_5_0_0": return load("am/sdma_5_0_0", [root/"extra/hip_gpu_driver/sdma_registers.h", f"{AMD}/amdgpu/navi10_sdma_pkt_open.h"],
                                   args=["-I/opt/rocm/include", "-x", "c++"], srcs=am_src)
    case "sdma_6_0_0": return load("am/sdma_6_0_0", [root/"extra/hip_gpu_driver/sdma_registers.h", f"{AMD}/amdgpu/sdma_v6_0_0_pkt_open.h"],
                                   args=["-I/opt/rocm/include", "-x", "c++"], srcs=am_src)
    case "smu_13_0_0": return load("am/smu_13_0_0", [f"{AMD}/pm/swsmu/inc/pmfw_if/{s}.h" for s in ["smu_v13_0_0_ppsmc","smu13_driver_if_v13_0_0"]]
                                    +[root/"extra/amdpci/headers/amdgpu_smu.h"], args=inc, srcs=am_src)
    case "smu_13_0_6": return load("am/smu_13_0_6", [f"{AMD}/pm/swsmu/inc/pmfw_if/{s}.h" for s in ["smu_v13_0_6_ppsmc","smu_v13_0_6_pmfw", \
      "smu13_driver_if_v13_0_6"]] +[root/"extra/amdpci/headers/amdgpu_smu.h"], args=inc, srcs=am_src)
    case "smu_13_0_12": return load("am/smu_13_0_12", [f"{AMD}/pm/swsmu/inc/pmfw_if/{s}.h" for s in ["smu_v13_0_12_ppsmc","smu_v13_0_12_pmfw",
      "smu13_driver_if_v13_0_6"]] +[root/"extra/amdpci/headers/amdgpu_smu.h"], args=inc, srcs=am_src)
    case "smu_14_0_2": return load("am/smu_14_0_2", [f"{AMD}/pm/swsmu/inc/pmfw_if/{s}.h" for s in ["smu_v14_0_0_pmfw", "smu_v14_0_2_ppsmc",
                                    "smu14_driver_if_v14_0"]]+[root/"extra/amdpci/headers/amdgpu_smu.h"], args=inc, srcs=am_src)
    # mi50 fork: Vega 20 (MP1 11.0.2) is driven by the legacy powerplay stack, not swsmu: messages = vega20_ppsmc.h (vega20_smumgr.c),
    # tables/structs = smu11_driver_if.h (vega20_hwmgr.c); same amdgpu_smu.h prolog as the swsmu modules (it defines bool).
    case "smu_11_0_2": return load("am/smu_11_0_2", [f"{AMD}/pm/powerplay/inc/{s}.h" for s in ["vega20_ppsmc", "smu11_driver_if"]]
                                    +[root/"extra/amdpci/headers/amdgpu_smu.h"], args=inc, srcs=am_src)
    # mi50 fork: RLC clear-state buffer data for gfx9 (gfx_v9_0.c:1652 get_csb_buffer -> amdgpu_gfx.c amdgpu_gfx_csb_data_parser walks
    # cs_data[].section[] = {extent, reg_index, reg_count}; clearstate_defs.h:33-42). Header-only: no clang, the arrays are parsed as text.
    case "clearstate_gfx9":
      def gencs(_, files, **kwargs):
        defs, cs = [pathlib.Path(f).read_text() for f in files]
        enum_body = re.search(r'enum section_id\s*\{([^}]*)\}', defs).group(1)
        assert '=' not in enum_body, "section_id enum has explicit values; parser assumes declaration order"  # clearstate_defs.h:26-31 has none
        ids = {nm: i for i, nm in enumerate(n for n in (x.strip().rstrip(',').strip() for x in enum_body.split('\n')) if n)}  # SECT_NONE=0, SECT_CONTEXT=1, ...
        arrays = {m.group(1): [int(v, 0) for v in re.sub(r'//[^\n]*', '', m.group(2)).replace('\n', ' ').split(',') if v.strip()]
                  for m in re.finditer(r'static const unsigned int (\w+)\[\] = \{(.*?)\};', cs, re.S)}
        exts = {m.group(1): [(int(c), int(i, 0), tuple(arrays[a])) for a, i, c in re.findall(r'\{\s*(\w+),\s*(0x[0-9a-fA-F]+),\s*(\d+)\s*\}', m.group(2))]
                for m in re.finditer(r'static const struct cs_extent_def (\w+)\[\] = \{(.*?)\};', cs, re.S)}
        for e in exts.values():
          for cnt, _, vals in e: assert len(vals) == cnt, (cnt, len(vals))
        data = [(ids[sid], exts[sec]) for sec, sid in re.findall(r'\{\s*(\w+),\s*(SECT_\w+)\s*\}', re.search(r'gfx9_cs_data\[\] = \{(.*?)\};', cs, re.S).group(1))
                if sec != '0']
        out = ["# sections of (reg_count, reg_index, reg_values) per clearstate_defs.h cs_extent_def {extent, reg_index, reg_count}; the terminating",
               "# {0,0,0} / {0, SECT_NONE} entries are dropped (Linux loops until extent/section == NULL)"]
        out += [f"{k} = {v}" for k, v in ids.items()]
        out += ["gfx9_cs_data = ["] + [f"  ({sid}, [" + ", ".join(f"({c}, {i:#x}, {v!r})" for c, i, v in e) + "]),"
                                       for sid, e in data] + ["]"]
        return "\n".join(out)
      return load("am/clearstate_gfx9", [f"{AMD}/amdgpu/clearstate_defs.h", f"{AMD}/amdgpu/clearstate_gfx9.h"], srcs=am_src, gen=gencs)
    # firmware hashes
    case "fw":
      def genfw(name, files, **kwargs): return "\n".join(["hashes = {"] + [f"  {p.name!r}: {hashlib.sha256(p.read_bytes()).hexdigest()!r},"
                                                                           for f in files if (p:=pathlib.Path(f)).is_file()] + ["}"])
      return load("am/fw", ["{}/amdgpu/psp_*_sos.bin", "{}/amdgpu/smu_*.bin", "{}/amdgpu/sdma_*.bin"] +
                           [f"{{}}/amdgpu/gc_*_{x}.bin" for x in ["pfp", "me", "mec", "imu", "rlc"]], srcs=fw_src, gen=genfw)
    case "navi_offsets": return load("am/navi_offsets", [f"{AMD}/include/sienna_cichlid_ip_offset.h"], srcs=am_src)
    case "vega_offsets": return load("am/vega_offsets", [f"{AMD}/include/vega20_ip_offset.h"], srcs=am_src)
    case "regs":
      def genreg(_, files, **kwargs):
        out = ["__all__ = " + repr([reg_renames.get(file.split('/')[-1], file.split('/')[-1]) for file in files])]
        for file, nm in [(file.replace("mp_11_0_0", "mp_11_0"), reg_renames.get(file.split('/')[-1], file.split('/')[-1])) for file in files]:
          pats = reg_patterns[prefix := {"osssys": "oss"}.get(x:=nm.split("_", 1)[0], x)] + reg_patterns_extra.get(nm, [])
          vega_sdma = file.endswith("sdma0_4_2")

          def split_name(name): return name[:(pos:=next((i for i,c in enumerate(name) if c.isupper()), len(name)))], name[pos:]
          # handle CDNA's different register names
          def normalize(reg):
            # mi50 fork: Vega 20 SDMA registers come from the sdma0_4_2 header as mmSDMA0_*; the driver names them regSDMA_* and
            # addresses SDMA1 as instance 1 with the same offsets + its own base (Linux sdma_v4_0.c sdma_v4_0_get_reg_offset)
            if vega_sdma: return reg.replace("SDMA0_", "SDMA_", 1)
            return s[0] + prefix.upper()[:2] + s[1] if prefix in ("gc", "mmhub") and (s:=split_name(reg))[1].startswith(("VM_", "MC_VM_")) else reg
          def extract(lines, pat): return ((normalize(m.group(1)), int(m.group(2), 0)) for l in lines if (m:=re.match(pat, l)))

          offset, sh_mask = pathlib.Path(f"{file}_offset.h").read_text().splitlines(), pathlib.Path(f"{file}_sh_mask.h").read_text().splitlines()
          defs = {k:v for k,v in extract(offset, r'#define\s+((?:mm|reg)\S+)\s+(0x[\da-fA-F]+|\d+)') if any(re.match("(mm|reg)"+p, k) for p in pats)}
          fields = {reg: {name.split('__')[1].lower(): ((mask & -mask).bit_length() - 1, mask.bit_length() - 1) for name, mask in fs}
                    for reg, fs in itertools.groupby(extract(sh_mask, r'#define\s+(\S+)_MASK\s+(0x[\da-fA-F]+|\d+)'), lambda x: x[0].split('__')[0])}

          regs = {reg: (off, defs[f"{reg}_BASE_IDX"], fields.get(split_name(reg)[1], {})) for reg,off in defs.items() if f"{reg}_BASE_IDX" in defs}
          # mi50 fork: the driver (ops_amd.py memory_barrier/signal_doorbell via AMDIP('nbio'), am/ip.py AM_SOC/AM_GMC) addresses the NBIO
          # registers by their gfx11/MI300 header names; NBIO 7.4 names the same registers without the BIF_BX_PF0_/BIF_BX0_/RCC_DEV0_EPF0_
          # prefixes (Linux nbio_v7_4.c uses GPU_HDP_FLUSH_REQ/DONE, REMAP_HDP_MEM_FLUSH_CNTL, PCIE_INDEX2/DATA2, RCC_DOORBELL_APER_EN).
          # Alias entries so both spellings resolve to the one register (same offset/segment/fields).
          if nm == "nbio_7_4":
            for new_name, src in [("mmBIF_BX_PF0_GPU_HDP_FLUSH_REQ", "mmGPU_HDP_FLUSH_REQ"), ("mmBIF_BX_PF0_GPU_HDP_FLUSH_DONE", "mmGPU_HDP_FLUSH_DONE"),
                                  ("mmBIF_BX0_REMAP_HDP_MEM_FLUSH_CNTL", "mmREMAP_HDP_MEM_FLUSH_CNTL"), ("mmBIF_BX0_PCIE_INDEX2", "mmPCIE_INDEX2"),
                                  ("mmBIF_BX0_PCIE_DATA2", "mmPCIE_DATA2"), ("mmRCC_DEV0_EPF0_RCC_DOORBELL_APER_EN", "mmRCC_DOORBELL_APER_EN"),
                                  ("mmBIF_BX0_BIF_DOORBELL_INT_CNTL", "mmBIF_DOORBELL_INT_CNTL")]:
              if src in regs: regs[new_name] = regs[src]
          print(f"defined {len(regs)} registers for {nm}")
          out.extend([f"{nm} = {{"] + [f"  {k!r}: {v!r}," for k,v in regs.items()] + ["}"])
        return "\n".join(out)
      return load("am/regs", [AMDINC + "/asic_reg/" + reg_dirs.get((pre, ver), {"osssys":"oss"}.get(pre, pre) + f"/{pre}_{'_'.join(map(str, ver))}")
                              for pre in reg_files for ver in sorted(reg_files[pre])], srcs=am_src, gen=genreg)
    case "soc_9" | "soc_11" | "soc_12":
      return load(f"am/{nm}", ["{}/projects/aqlprofile/linux/" + {9: "vega10", 11: "soc21", 12: "soc24"}[int(nm.split('_')[1])] + "_enum.h"],
                  srcs=rocm_src, patterns=soc_patterns, macros=False)
    case "pmc":
      def genpmc(_, files, **kwargs):
        from yaml import safe_load # type: ignore
        with open(files[0], "r") as f: data = safe_load(f)
        out = ["counters = {"]
        for counter in [c for c in data['rocprofiler-sdk']['counters'] if any('block' in d for d in c['definitions'])]:
          out.extend([f"  {counter['name']!r}: {{",
                      *[f"    {a!r}: ({d['block']!r}, {d['event']})," for d in counter['definitions'] for a in d['architectures']], "  },"])
        return "\n".join(out + ["}"])
      return load("am/pmc", ["{}/counter_defs.yaml"], srcs=pmc_src, gen=genpmc)
    case _: raise AttributeError(f"no such autogen: {nm}")
