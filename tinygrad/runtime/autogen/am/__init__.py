import pathlib, hashlib, re, itertools
from tinygrad.runtime.autogen import load, root

__all__ = ["am", "pm4_soc15", "pm4_nv", "sdma_4_0_0", "sdma_5_0_0", "sdma_6_0_0", "smu_13_0_0", "smu_13_0_6", "smu_13_0_12", "smu_14_0_2", "smu_11_0_2",
           "fw", "navi_offsets", "vega_offsets", "regs", "soc_9", "soc_11", "soc_12", "pmc"]

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
  "osssys": [(4,4,2), (6,0,0), (6,1,0), (7,0,0)], "sdma": [(4,4,2)]
}
# mi50: Vega 20 tables, written under TinyGPU's register names (mm -> reg, SDMA0_ -> SDMA_)
reg_files["gc"] += [(9,0)]; reg_files["mmhub"] += [(1,0)]; reg_files["nbio"] += [(7,4)]; reg_files["hdp"] += [(4,0)]
reg_files["osssys"] += [(4,2,0)]; reg_files["sdma"] += [(4,2)]; reg_files["smuio"] = [(11,0,0)]; reg_files["thm"] = [(11,0,2)]
VEGA20_TABLES = {"gc_9_0", "mmhub_1_0", "nbio_7_4", "hdp_4_0", "osssys_4_2_0", "sdma_4_2", "smuio_11_0_0", "thm_11_0_2"}
# mi50: NBIO 7.4 registers TinyGPU uses under another name, and the Vega 20 registers TinyGPU has no name for
VEGA20_RENAMES = {"RCC_DOORBELL_APER_EN": "RCC_DEV0_EPF0_RCC_DOORBELL_APER_EN", "BIF_DOORBELL_INT_CNTL": "BIF_BX0_BIF_DOORBELL_INT_CNTL",
  "REMAP_HDP_MEM_FLUSH_CNTL": "BIF_BX0_REMAP_HDP_MEM_FLUSH_CNTL", "GPU_HDP_FLUSH_REQ": "BIF_BX_PF0_GPU_HDP_FLUSH_REQ",
  "GPU_HDP_FLUSH_DONE": "BIF_BX_PF0_GPU_HDP_FLUSH_DONE", "PCIE_INDEX2": "BIF_BX0_PCIE_INDEX2", "PCIE_DATA2": "BIF_BX0_PCIE_DATA2"}
VEGA20_PATTERNS = {"nbio_7_4": ["BIF_SDMA[01]_DOORBELL_RANGE", "REMAP_HDP_REG_FLUSH_CNTL", "BIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL"],
  "hdp_4_0": ["HDP_READ_CACHE_INVALIDATE"],  # powerplay port: table reads invalidate the HDP read cache (vega20_smumgr.c)
  "sdma_4_2": ["SDMA_F32_CNTL"]}  # sdma_v4_0_start un-halts each engine (F32_CNTL.HALT = 0, sdma_v4_0.c:1397-1398, :1418-1421)

reg_patterns = {
  "gc": ["GCVM", "GCMC_VM", "CP_(HQD|MQD|MEC|ME_CNTL|PERFMON|RB_WPTR_POLL_CNTL|INT_CNTL|STAT|PFP_PRGRM|ME_PRGRM|COHER_START)", "COMPUTE_",
         "(SQ|GL2C|TCC)_PERFCOUNTER", "SQ_THREAD_TRACE", "SPI_(CONFIG_CNTL|COMPUTE_QUEUE_RESET)", "GRBM", "SH_MEM", "RLC", "TCP", "GB_ADDR_CONFIG",
         "SDMA[01]_(WATCHDOG_CNTL|UTCL1_(CNTL|PAGE)|MCU_CNTL|F32_CNTL|CNTL|QUEUE0_|RLC_CGCG_CTRL)", "SCRATCH_REG[67]"],
  "mmhub": ["MMVM", "MMMC_VM", "MM_ATC_L2_MISC_CG"],
  "nbio": (nbio:=["BIF_BX_PF[01]_GPU_HDP_FLUSH", "BIF_BX_PF0_RSMU", "BIF_BX0_(REMAP_HDP_MEM_FLUSH_CNTL|BIF_DOORBELL_INT_CNTL|PCIE_INDEX2|PCIE_DATA2)",
                  "BIFC_(DOORBELL_ACCESS_EN_PF|GFX_INT_MONITOR_MASK)", "XCC_DOORBELL_FENCE", "DOORBELL0_CTRL_ENTRY", "GDC_S2A0_S2A_DOORBELL_ENTRY",
                  "S2A_DOORBELL_ENTRY", "RCC_DEV0_EPF0_RCC_DOORBELL_APER_EN", "RCC_DEV0_EPF2_STRAP2"]),
  "nbif": nbio,
  "mp": ["MP([01]|ASP)_SMN_C2PMSG"], "hdp": ["HDP_MEM_POWER_CTRL"], "oss": ["IH_"], "sdma": ["SDMA_GFX", "SDMA_CNTL"],
  "smuio": ["ROM_(INDEX|DATA)"], "thm": ["THM_THERMAL_INT_(CTRL|ENA)"]
}

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
    # mi50: Vega 20 SMU 11 messages and tables (powerplay vega20_ppsmc.h + smu11_driver_if.h)
    case "smu_11_0_2": return load("am/smu_11_0_2", [f"{AMD}/pm/powerplay/inc/{s}.h" for s in ["vega20_ppsmc", "smu11_driver_if"]]
                                    +[root/"extra/amdpci/headers/amdgpu_smu.h"], args=inc, srcs=am_src)
    # firmware hashes
    case "fw":
      def genfw(name, files, **kwargs): return "\n".join(["hashes = {"] + [f"  {p.name!r}: {hashlib.sha256(p.read_bytes()).hexdigest()!r},"
                                                                           for f in files if (p:=pathlib.Path(f)).is_file()] + ["}"])
      return load("am/fw", ["{}/amdgpu/psp_*_sos.bin", "{}/amdgpu/smu_*.bin", "{}/amdgpu/sdma_*.bin"] +
                           [f"{{}}/amdgpu/gc_*_{x}.bin" for x in ["pfp", "me", "mec", "imu", "rlc"]] +
                           [f"{{}}/amdgpu/vega20_{x}.bin" for x in ["sos", "asd", "ta", "sdma", "sdma1", "ce", "pfp", "me", "mec", "mec2", "rlc", "smc"]],
                  srcs=fw_src, gen=genfw)
    case "navi_offsets": return load("am/navi_offsets", [f"{AMD}/include/sienna_cichlid_ip_offset.h"], srcs=am_src)
    case "vega_offsets": return load("am/vega_offsets", [f"{AMD}/include/vega20_ip_offset.h"], srcs=am_src)
    case "regs":
      def genreg(_, files, **kwargs):
        def modname(file): return file.split('/')[-1].replace("sdma0_4_2", "sdma_4_2")
        out = ["__all__ = " + repr([modname(file) for file in files])]
        for file, nm in [(file.replace("mp_11_0_0", "mp_11_0"), modname(file)) for file in files]:
          pats = reg_patterns[prefix := {"osssys": "oss"}.get(x:=nm.split("_", 1)[0], x)] + VEGA20_PATTERNS.get(nm, [])

          def split_name(name): return name[:(pos:=next((i for i,c in enumerate(name) if c.isupper()), len(name)))], name[pos:]
          # handle CDNA's different register names
          def normalize(reg):
            return s[0] + prefix.upper()[:2] + s[1] if prefix in ("gc", "mmhub") and (s:=split_name(reg))[1].startswith(("VM_", "MC_VM_")) else reg
          def rename(reg):
            if nm not in VEGA20_TABLES: return reg
            pre, base = ("reg", reg[2:]) if reg.startswith("mm") else ("", reg)
            sfx = "_BASE_IDX" if base.endswith("_BASE_IDX") else ""
            base = base.removesuffix(sfx)
            if nm == "nbio_7_4": base = (lambda h, sep, t: VEGA20_RENAMES.get(h, h) + sep + t)(*base.partition("__"))   # fields: REG__FIELD
            if nm == "sdma_4_2" and base.startswith("SDMA0_"): base = "SDMA_" + base[6:]
            return pre + base + sfx
          def extract(lines, pat): return ((rename(normalize(m.group(1))), int(m.group(2), 0)) for l in lines if (m:=re.match(pat, l)))

          offset, sh_mask = pathlib.Path(f"{file}_offset.h").read_text().splitlines(), pathlib.Path(f"{file}_sh_mask.h").read_text().splitlines()
          defs = {k:v for k,v in extract(offset, r'#define\s+((?:mm|reg)\S+)\s+(0x[\da-fA-F]+|\d+)') if any(re.match("(mm|reg)"+p, k) for p in pats)}
          fields = {reg: {name.split('__')[1].lower(): ((mask & -mask).bit_length() - 1, mask.bit_length() - 1) for name, mask in fs}
                    for reg, fs in itertools.groupby(extract(sh_mask, r'#define\s+(\S+)_MASK\s+(0x[\da-fA-F]+|\d+)'), lambda x: x[0].split('__')[0])}

          regs = {reg: (off, defs[f"{reg}_BASE_IDX"], fields.get(split_name(reg)[1], {})) for reg,off in defs.items() if f"{reg}_BASE_IDX" in defs}
          # mi50: Vega 20's PSP mailbox lives in this shared MP 11 table under mm names; TinyGPU's PSP code asks for reg names
          if nm == "mp_11_0_0": regs.update({"reg" + k[2:]: v for k, v in list(regs.items()) if k.startswith("mmMP0_SMN_C2PMSG")})
          print(f"defined {len(regs)} registers for {nm}")
          out.extend([f"{nm} = {{"] + [f"  {k!r}: {v!r}," for k,v in regs.items()] + ["}"])
        return "\n".join(out)
      return load("am/regs", [AMDINC + "/asic_reg/" + ("sdma0/sdma0_4_2" if (pre, ver) == ("sdma", (4,2)) else
                              {"osssys":"oss"}.get(pre, pre) + f"/{pre}_{'_'.join(map(str, ver))}") for pre in reg_files for ver in sorted(reg_files[pre])],
                  srcs=am_src, gen=genreg)
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
