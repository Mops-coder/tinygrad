// mi50_gen.cpp - host build tool: generates the TinyGPU driver's MI50 tables from their primary sources and writes them into
// TinyGPUDriver.cpp, between its "BEGIN mi50_gen output" and "END mi50_gen output" lines (everything between them is replaced):
//   mi50::reg  every register the MI50 bring-up touches: dword address = vega20_ip_offset.h base[seg] + the Linux offset
//              (amdgpu SOC15_REG_OFFSET), field shift/mask from the Linux sh_mask header. Each address and field is also checked
//              against tinygrad's autogen table (the reference driver's own numbers); any difference stops the build. Also GC_GOLDEN: the
//              reference's AM_GFX.VEGA20_GOLDEN, read from its ip.py (runtime/support/am/ip.py beside the given regs.py's autogen/am),
//              each register's offset and base index checked against Linux's gc_9_0_offset.h.
//   mi50::fw   the twelve linux-firmware Vega 20 images (SHA-256 checked against tinygrad's pinned hashes, autogen/am/fw.py),
//              cut into the payloads the PSP is handed, in the reference driver's load order (amdev.py AMFirmware._init_vega20).
//   mi50::pp   the Radeon Pro V420 32 GB VBIOS's PowerPlay table (powerplayinfo), byte for byte (V420.rom SHA-256 checked, table
//              checked as vega20_processpptables.c check_powerplay_tables does): the table the MI50 boots on (Leon 2026-10-16).
//   mi50::od   the built-in OverDrive table (OverDriveTable_t, 32 bytes): the SMU's own table recorded once on the card after the V420 boot,
//              given as the optional last argument. Without it HAVE_DEFAULT is false and the driver keeps the table the SMU reports at boot.
// Usage: mi50_gen <linux drivers/gpu/drm/amd> <tinygrad autogen/am/regs.py> <firmware dir> <V420.rom> <TinyGPUDriver.cpp> [recorded OD table]
// Build with the Linux headers on the include path: -I <linux>/include -I <linux>/pm/powerplay/inc -I <linux>/pm/powerplay/hwmgr
#include <CommonCrypto/CommonDigest.h>
#include <algorithm>
#include <cstddef>
#include <cstring>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <map>
#include <regex>
#include <sstream>
#include <string>
#include <vector>
#include "atom-types.h"
#include "atomfirmware.h"
#include "smu11_driver_if.h"
#include "vega20_pptable.h"

static std::string slurp(const std::string &p) {
  std::ifstream f(p, std::ios::binary);
  if (!f) { fprintf(stderr, "mi50_gen: cannot read %s\n", p.c_str()); exit(1); }
  std::stringstream s; s << f.rdbuf(); return s.str();
}
[[noreturn]] static void die(const std::string &m) { fprintf(stderr, "mi50_gen: %s\n", m.c_str()); exit(1); }

// #define NAME VALUE (hex or decimal, optional U/L suffix)
static std::map<std::string, uint64_t> defines(const std::string &path) {
  std::map<std::string, uint64_t> out;
  static const std::regex re(R"(^#define\s+(\w+)\s+(0x[0-9a-fA-F]+|\d+)[uUlL]*\s*(?://.*|/\*.*)?$)");
  std::istringstream in(slurp(path)); std::string line; std::smatch m;
  while (std::getline(in, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (std::regex_match(line, m, re)) out[m[1]] = std::stoull(m[2], nullptr, 0);
  }
  return out;
}

// tinygrad autogen/am/regs.py: module = { 'regNAME': (offset, segment, {'field': (lo, hi), ...}), ... }
struct TgReg { uint32_t off = 0, seg = 0; std::map<std::string, std::pair<int, int>> fields; };
static std::map<std::string, TgReg> tg_module(const std::string &text, const std::string &mod) {
  size_t at = text.find("\n" + mod + " = {\n");
  if (at == std::string::npos) die("regs.py: no module " + mod);
  size_t end = text.find("\n}\n", at + 1);
  std::istringstream in(text.substr(at, end - at)); std::string line;
  static const std::regex reg(R"(^\s+'(\w+)': \((\d+), (\d+), \{(.*)\}\),?$)"), fld(R"('(\w+)': \((\d+), (\d+)\))");
  std::map<std::string, TgReg> out; std::smatch m;
  while (std::getline(in, line)) {
    if (!std::regex_match(line, m, reg)) continue;
    TgReg r; r.off = std::stoul(m[2]); r.seg = std::stoul(m[3]);
    std::string fs = m[4];
    for (std::sregex_iterator it(fs.begin(), fs.end(), fld), e; it != e; ++it) r.fields[(*it)[1]] = {std::stoi((*it)[2]), std::stoi((*it)[3])};
    out[m[1]] = r;
  }
  return out;
}

static std::string lower(std::string s) { for (auto &c : s) c = (char)tolower((unsigned char)c); return s; }

struct Block { const char *name, *hdr, *base, *tgmod, *tgprefix; };
// block -> (Linux header Vega 20's driver uses, vega20_ip_offset.h base, tinygrad module, tinygrad name prefix). The tinygrad module is the one
// import_module picks for the Vega 20 IP versions (support/amd.py:31-38, amdev.py _static_vega20/_build_regs); MP1's 'mm' names come from
// mp_11_0_0 on the MP1 bases (amdev.py _build_regs). Linux's MP1 offsets in mp_11_0_offset.h equal mp_9_0_offset.h's (vega20_smumgr.c).
static const Block BLOCKS[] = {
  {"NBIO", "nbio/nbio_7_4", "NBIO", "nbio_7_4", "reg"},       {"GC", "gc/gc_9_0", "GC", "gc_9_0", "reg"},
  {"MMHUB", "mmhub/mmhub_1_0", "MMHUB", "mmhub_1_0", "regMM"}, {"OSSSYS", "oss/osssys_4_2_0", "OSSSYS", "osssys_4_2_0", "reg"},
  {"HDP", "hdp/hdp_4_0", "HDP", "hdp_4_0", "reg"},             {"MP0", "mp/mp_11_0", "MP0", "mp_11_0_0", "reg"},
  {"MP1", "mp/mp_11_0", "MP1", "mp_11_0_0", "mm"},             {"SMUIO", "smuio/smuio_11_0_0", "SMUIO", "smuio_11_0_0", "reg"},
  {"THM", "thm/thm_11_0_2", "THM", "thm_11_0_2", "reg"},
};

// register: block, Linux name (without mm), tinygrad name ("" = the reference uses a raw dword index, checked in raw), fields
struct RegSpec { const char *blk, *linux_name, *tg_name; uint32_t raw; std::vector<std::string> fields; };

int main(int argc, char **argv) {
  if (argc != 6 && argc != 7)
    die("usage: mi50_gen <linux drivers/gpu/drm/amd> <tinygrad autogen/am/regs.py> <firmware dir> <V420.rom> <TinyGPUDriver.cpp> [recorded OD table]");
  std::string L = argv[1], regs_py = slurp(argv[2]), fwdir = argv[3], v420_path = argv[4], target = argv[5];
  auto ipoff = defines(L + "/include/vega20_ip_offset.h");

  std::vector<RegSpec> specs = {
    // NBIO 7.4: VRAM window (amdgpu_device_mm_access), PCIE_INDEX2/DATA2 (amdgpu_device_indirect_rreg/wreg), HDP remap and doorbells
    // (nbio_v7_4.c), memory size, VBIOS scratch (amdgpu_atomfirmware_scratch_regs_init reads bios_scratch_reg_startaddr from the VBIOS)
    {"NBIO", "MM_INDEX", "", 0x0, {}}, {"NBIO", "MM_DATA", "", 0x1, {}}, {"NBIO", "MM_INDEX_HI", "", 0x6, {}},
    {"NBIO", "PCIE_INDEX2", "regBIF_BX0_PCIE_INDEX2", 0, {}}, {"NBIO", "PCIE_DATA2", "regBIF_BX0_PCIE_DATA2", 0, {}},
    {"NBIO", "BIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL", "regBIF_BX_DEV0_EPF0_VF0_HDP_MEM_COHERENCY_FLUSH_CNTL", 0, {}},
    {"NBIO", "REMAP_HDP_MEM_FLUSH_CNTL", "regBIF_BX0_REMAP_HDP_MEM_FLUSH_CNTL", 0, {}},
    {"NBIO", "REMAP_HDP_REG_FLUSH_CNTL", "regREMAP_HDP_REG_FLUSH_CNTL", 0, {}},
    {"NBIO", "RCC_DOORBELL_APER_EN", "regRCC_DEV0_EPF0_RCC_DOORBELL_APER_EN", 0, {}},
    {"NBIO", "BIF_SDMA0_DOORBELL_RANGE", "regBIF_SDMA0_DOORBELL_RANGE", 0, {"OFFSET", "SIZE"}},
    {"NBIO", "BIF_SDMA1_DOORBELL_RANGE", "regBIF_SDMA1_DOORBELL_RANGE", 0, {"OFFSET", "SIZE"}},
    {"NBIO", "RCC_CONFIG_MEMSIZE", "", 0xde3, {}},
    {"NBIO", "BIOS_SCRATCH_0", "", 0, {}}, {"NBIO", "BIOS_SCRATCH_4", "", 0, {}}, {"NBIO", "BIOS_SCRATCH_6", "", 0, {}},   // 6: vega20_baco.c clean_baco_tbl
    {"NBIO", "BIOS_SCRATCH_7", "", 0, {}},
    {"NBIO", "BIOS_SCRATCH_14", "", 0, {}},
    // GC 9.0: the AM boot flags and the GC hub fault status (amdev.py partial-boot test)
    {"GC", "SCRATCH_REG6", "regSCRATCH_REG6", 0, {}}, {"GC", "SCRATCH_REG7", "regSCRATCH_REG7", 0, {}},
    {"GC", "VM_L2_PROTECTION_FAULT_STATUS", "regGCVM_L2_PROTECTION_FAULT_STATUS", 0, {}},
    // GC 9.0: AM_GFX.init_hw's full boot for GC 9.4.0 (ip.py:849-905: CP idle, MEC config/enable, TCP, RLC, GRBM, SH_MEM per VMID, MEC doorbells)
    {"GC", "CP_STAT", nullptr, 0, {}},
    {"GC", "CP_MEC_CNTL", nullptr, 0, {"MEC_INVALIDATE_ICACHE", "MEC_ME1_PIPE0_RESET", "MEC_ME2_PIPE0_RESET", "MEC_ME1_HALT", "MEC_ME2_HALT"}},
    {"GC", "TCP_CNTL", nullptr, 0, {}}, {"GC", "RLC_CNTL", nullptr, 0, {}},
    {"GC", "RLC_SRM_CNTL", nullptr, 0, {"SRM_ENABLE", "AUTO_INCR_ADDR"}}, {"GC", "RLC_SPM_MC_CNTL", nullptr, 0, {}},
    {"GC", "GRBM_CNTL", nullptr, 0, {"READ_TIMEOUT"}},
    {"GC", "GRBM_GFX_CNTL", nullptr, 0, {"PIPEID", "MEID", "VMID", "QUEUEID"}},
    {"GC", "SH_MEM_CONFIG", nullptr, 0, {"ADDRESS_MODE", "ALIGNMENT_MODE", "RETRY_DISABLE"}},
    {"GC", "SH_MEM_BASES", nullptr, 0, {"PRIVATE_BASE", "SHARED_BASE"}},
    {"GC", "CP_MEC_DOORBELL_RANGE_LOWER", nullptr, 0, {}}, {"GC", "CP_MEC_DOORBELL_RANGE_UPPER", nullptr, 0, {}},
    // GC 9.0: the active CU count (bug 5, Leon 2026-10-16: the MI50's 60 CUs), as Linux gfx_v9_0_get_cu_info reads it: GRBM_GFX_INDEX selects a
    // shader engine / array (gfx_v9_0_select_se_sh), the two SHADER_ARRAY_CONFIGs mark its inactive CUs (gfx_v9_0_get_cu_active_bitmap). The
    // reference has no CU readout and its regs.py no SHADER_ARRAY_CONFIG: those two are Linux's alone
    {"GC", "GRBM_GFX_INDEX", nullptr, 0, {"INSTANCE_INDEX", "SH_INDEX", "SE_INDEX", "SH_BROADCAST_WRITES", "INSTANCE_BROADCAST_WRITES", "SE_BROADCAST_WRITES"}},
    {"GC", "CC_GC_SHADER_ARRAY_CONFIG", "", 0, {"INACTIVE_CUS"}}, {"GC", "GC_USER_SHADER_ARRAY_CONFIG", "", 0, {"INACTIVE_CUS"}},
    // MMHUB 1.0: AM_GMC.init_sw / init_hub("MM")
    {"MMHUB", "MC_VM_FB_LOCATION_BASE", nullptr, 0, {}}, {"MMHUB", "MC_VM_FB_LOCATION_TOP", nullptr, 0, {}},
    {"MMHUB", "MC_VM_AGP_BASE", nullptr, 0, {}}, {"MMHUB", "MC_VM_AGP_BOT", nullptr, 0, {}}, {"MMHUB", "MC_VM_AGP_TOP", nullptr, 0, {}},
    {"MMHUB", "MC_VM_SYSTEM_APERTURE_LOW_ADDR", nullptr, 0, {}}, {"MMHUB", "MC_VM_SYSTEM_APERTURE_HIGH_ADDR", nullptr, 0, {}},
    {"MMHUB", "MC_VM_SYSTEM_APERTURE_DEFAULT_ADDR_LSB", nullptr, 0, {}}, {"MMHUB", "MC_VM_SYSTEM_APERTURE_DEFAULT_ADDR_MSB", nullptr, 0, {}},
    {"MMHUB", "VM_L2_PROTECTION_FAULT_DEFAULT_ADDR_LO32", nullptr, 0, {}}, {"MMHUB", "VM_L2_PROTECTION_FAULT_DEFAULT_ADDR_HI32", nullptr, 0, {}},
    {"MMHUB", "VM_L2_PROTECTION_FAULT_CNTL2", nullptr, 0, {"ACTIVE_PAGE_MIGRATION_PTE_READ_RETRY"}},
    {"MMHUB", "MC_VM_MX_L1_TLB_CNTL", nullptr, 0,
     {"ENABLE_L1_TLB", "SYSTEM_ACCESS_MODE", "ENABLE_ADVANCED_DRIVER_MODEL", "SYSTEM_APERTURE_UNMAPPED_ACCESS", "MTYPE"}},
    {"MMHUB", "VM_L2_CNTL", nullptr, 0, {"ENABLE_L2_CACHE", "ENABLE_DEFAULT_PAGE_OUT_TO_SYSTEM_MEMORY", "L2_PDE0_CACHE_TAG_GENERATION_MODE",
     "PDE_FAULT_CLASSIFICATION", "CONTEXT1_IDENTITY_ACCESS_MODE", "IDENTITY_MODE_FRAGMENT_SIZE", "ENABLE_L2_FRAGMENT_PROCESSING"}},
    {"MMHUB", "VM_L2_CNTL2", nullptr, 0, {"INVALIDATE_ALL_L1_TLBS", "INVALIDATE_L2_CACHE"}},
    {"MMHUB", "VM_L2_CNTL3", nullptr, 0, {"L2_CACHE_4K_ASSOCIATIVITY", "L2_CACHE_BIGK_ASSOCIATIVITY", "BANK_SELECT", "L2_CACHE_BIGK_FRAGMENT_SIZE"}},
    {"MMHUB", "VM_L2_CNTL4", nullptr, 0, {"L2_CACHE_4K_PARTITION_COUNT"}},
    {"MMHUB", "VM_CONTEXT0_PAGE_TABLE_START_ADDR_LO32", nullptr, 0, {}}, {"MMHUB", "VM_CONTEXT0_PAGE_TABLE_START_ADDR_HI32", nullptr, 0, {}},
    {"MMHUB", "VM_CONTEXT0_PAGE_TABLE_END_ADDR_LO32", nullptr, 0, {}}, {"MMHUB", "VM_CONTEXT0_PAGE_TABLE_END_ADDR_HI32", nullptr, 0, {}},
    {"MMHUB", "VM_CONTEXT0_PAGE_TABLE_BASE_ADDR_LO32", nullptr, 0, {}}, {"MMHUB", "VM_CONTEXT0_PAGE_TABLE_BASE_ADDR_HI32", nullptr, 0, {}},
    {"MMHUB", "VM_CONTEXT0_CNTL", nullptr, 0, {"ENABLE_CONTEXT", "PAGE_TABLE_DEPTH", "PAGE_TABLE_BLOCK_SIZE",
     "PDE0_PROTECTION_FAULT_ENABLE_INTERRUPT", "DUMMY_PAGE_PROTECTION_FAULT_ENABLE_INTERRUPT", "RANGE_PROTECTION_FAULT_ENABLE_INTERRUPT",
     "VALID_PROTECTION_FAULT_ENABLE_INTERRUPT", "READ_PROTECTION_FAULT_ENABLE_INTERRUPT", "WRITE_PROTECTION_FAULT_ENABLE_INTERRUPT",
     "EXECUTE_PROTECTION_FAULT_ENABLE_INTERRUPT", "PDE0_PROTECTION_FAULT_ENABLE_DEFAULT", "DUMMY_PAGE_PROTECTION_FAULT_ENABLE_DEFAULT",
     "RANGE_PROTECTION_FAULT_ENABLE_DEFAULT", "VALID_PROTECTION_FAULT_ENABLE_DEFAULT", "READ_PROTECTION_FAULT_ENABLE_DEFAULT",
     "WRITE_PROTECTION_FAULT_ENABLE_DEFAULT", "EXECUTE_PROTECTION_FAULT_ENABLE_DEFAULT"}},
    {"MMHUB", "VM_L2_CONTEXT1_IDENTITY_APERTURE_LOW_ADDR_LO32", nullptr, 0, {}}, {"MMHUB", "VM_L2_CONTEXT1_IDENTITY_APERTURE_LOW_ADDR_HI32", nullptr, 0, {}},
    {"MMHUB", "VM_L2_CONTEXT1_IDENTITY_APERTURE_HIGH_ADDR_LO32", nullptr, 0, {}}, {"MMHUB", "VM_L2_CONTEXT1_IDENTITY_APERTURE_HIGH_ADDR_HI32", nullptr, 0, {}},
    {"MMHUB", "VM_L2_CONTEXT_IDENTITY_PHYSICAL_OFFSET_LO32", nullptr, 0, {}}, {"MMHUB", "VM_L2_CONTEXT_IDENTITY_PHYSICAL_OFFSET_HI32", nullptr, 0, {}},
    // OSSSYS 4.2.0: AM_IH.init_hw
    {"OSSSYS", "IH_RB_BASE", nullptr, 0, {}}, {"OSSSYS", "IH_RB_BASE_HI", nullptr, 0, {}},
    {"OSSSYS", "IH_RB_BASE_RING1", nullptr, 0, {}}, {"OSSSYS", "IH_RB_BASE_HI_RING1", nullptr, 0, {}},
    {"OSSSYS", "IH_RB_CNTL", nullptr, 0, {"MC_SPACE", "WPTR_OVERFLOW_CLEAR", "RB_SIZE", "MC_SNOOP", "MC_RO", "MC_VMID", "WPTR_OVERFLOW_ENABLE",
     "RPTR_REARM", "RB_ENABLE", "ENABLE_INTR"}},
    {"OSSSYS", "IH_RB_CNTL_RING1", nullptr, 0, {"MC_SPACE", "WPTR_OVERFLOW_CLEAR", "RB_SIZE", "MC_SNOOP", "MC_RO", "MC_VMID",
     "RB_FULL_DRAIN_ENABLE", "RB_ENABLE"}},
    {"OSSSYS", "IH_RB_WPTR_ADDR_LO", nullptr, 0, {}}, {"OSSSYS", "IH_RB_WPTR_ADDR_HI", nullptr, 0, {}},
    {"OSSSYS", "IH_RB_WPTR", nullptr, 0, {}}, {"OSSSYS", "IH_RB_WPTR_RING1", nullptr, 0, {}},
    {"OSSSYS", "IH_RB_RPTR", nullptr, 0, {}}, {"OSSSYS", "IH_RB_RPTR_RING1", nullptr, 0, {}},
    {"OSSSYS", "IH_DOORBELL_RPTR", nullptr, 0, {"ENABLE"}}, {"OSSSYS", "IH_DOORBELL_RPTR_RING1", nullptr, 0, {"ENABLE"}},
    {"OSSSYS", "IH_STORM_CLIENT_LIST_CNTL", nullptr, 0, {"CLIENT18_IS_STORM_CLIENT"}},
    {"OSSSYS", "IH_INT_FLOOD_CNTL", nullptr, 0, {"FLOOD_CNTL_ENABLE"}},
    // HDP 4.0 (hdp_v4_0_invalidate_hdp)
    {"HDP", "HDP_READ_CACHE_INVALIDATE", nullptr, 0, {}},
    // MP0 11.0: the PSP mailbox (psp_v11_0.c); MP1: the SMU mailbox (vega20_smumgr.c)
    {"MP0", "MP0_SMN_C2PMSG_35", nullptr, 0, {}}, {"MP0", "MP0_SMN_C2PMSG_36", nullptr, 0, {}}, {"MP0", "MP0_SMN_C2PMSG_64", nullptr, 0, {}},
    {"MP0", "MP0_SMN_C2PMSG_67", nullptr, 0, {}}, {"MP0", "MP0_SMN_C2PMSG_69", nullptr, 0, {}}, {"MP0", "MP0_SMN_C2PMSG_70", nullptr, 0, {}},
    {"MP0", "MP0_SMN_C2PMSG_71", nullptr, 0, {}}, {"MP0", "MP0_SMN_C2PMSG_81", nullptr, 0, {}},
    {"MP1", "MP1_SMN_C2PMSG_66", nullptr, 0, {}}, {"MP1", "MP1_SMN_C2PMSG_82", nullptr, 0, {}}, {"MP1", "MP1_SMN_C2PMSG_90", nullptr, 0, {}},
    // SMUIO 11.0.0: VBIOS ROM window (amdgpu_soc15_read_bios_from_rom), the VDDGFX telemetry (vega20_hwmgr.c read_sensor :2280-2285; the
    // reference egpu_vega20.py reads SMUIO base + 0x4 raw); THM 11.0.2: thermal alert and the hotspot temperature (vega20_thermal.c)
    {"SMUIO", "ROM_INDEX", nullptr, 0, {}}, {"SMUIO", "ROM_DATA", nullptr, 0, {}},
    {"SMUIO", "SMUSVI0_TEL_PLANE0", "", 0x16804, {"SVI0_PLANE0_VDDCOR"}},
    {"THM", "THM_THERMAL_INT_CTRL", nullptr, 0, {"MAX_IH_CREDIT", "THERM_IH_HW_ENA", "DIG_THERM_INTH", "DIG_THERM_INTL", "THERM_TRIGGER_MASK",
     "THERM_INTH_MASK", "THERM_INTL_MASK"}},
    {"THM", "THM_THERMAL_INT_ENA", nullptr, 0, {"THERM_INTH_CLR", "THERM_INTL_CLR", "THERM_TRIGGER_CLR"}},
    {"THM", "CG_MULT_THERMAL_STATUS", "", 0, {"CTF_TEMP"}},   // vega20_thermal_get_temperature (vega20_thermal.c:218-232); not in regs.py
    // the thermal cutoff (Leon 2026-10-17): vega20_baco.c BACO entry and its support check; not in regs.py
    {"THM", "THM_BACO_CNTL", "", 0, {}}, {"NBIO", "BACO_CNTL", "", 0, {"BACO_MODE"}}, {"NBIO", "RCC_BIF_STRAP0", "", 0, {"STRAP_PX_CAPABLE"}},
  };
  static std::vector<std::string> eng;   // the specs keep c_str() pointers into it
  for (int i = 0; i < 18; i++) for (const char *s : {"_ADDR_RANGE_LO32", "_ADDR_RANGE_HI32"}) eng.push_back("VM_INVALIDATE_ENG" + std::to_string(i) + s);
  for (auto &n : eng) specs.push_back({"MMHUB", n.c_str(), nullptr, 0, {}});
  // GC 9.0: AM_GMC.init_hub("GC") (ip.py:130-165) is init_hub("MM") on the GC block: every MMHUB hub register above, by the same Linux name
  // in gc_9_0, tinygrad's "regGC" + that name (the FB location is read from MMHUB only, AM_GMC.init_sw)
  static std::vector<std::string> gc_tg;   // tinygrad names, kept alive for the specs' c_str() pointers
  std::vector<RegSpec> gc_hub;
  for (const auto &s : specs)
    if (std::string(s.blk) == "MMHUB" && std::string(s.linux_name).rfind("MC_VM_FB_LOCATION_", 0) != 0) gc_hub.push_back(s);
  gc_tg.reserve(gc_hub.size());
  for (auto &s : gc_hub) { gc_tg.push_back(std::string("regGC") + s.linux_name); s.blk = "GC"; s.tg_name = gc_tg.back().c_str(); specs.push_back(s); }

  std::ostringstream h;
  h << "// ---- mi50::reg: Vega 20 register dword addresses = vega20_ip_offset.h <BLOCK>_BASE__INST0_SEG<idx> + the Linux offset (amdgpu\n"
       "// SOC15_REG_OFFSET), fields from the Linux sh_mask headers (drivers/gpu/drm/amd/include/asic_reg, Linux d6d3455). Every address and\n"
       "// field was checked against tinygrad's autogen/am/regs.py at generation time. The map is complete for the registers it names (each\n"
       "// with the fields the reference uses); the bring-up reads a subset of the fields, so unused constants are expected here, and only here.\n"
       "#pragma clang diagnostic push\n#pragma clang diagnostic ignored \"-Wunused-const-variable\"\nnamespace mi50 { namespace reg {\n";
  size_t nregs = 0, nfields = 0;
  std::ostringstream table;
  for (const auto &s : specs) {
    const Block *b = nullptr;
    for (const auto &x : BLOCKS) if (std::string(x.name) == s.blk) b = &x;
    if (!b) die(std::string("unknown block ") + s.blk);
    auto off = defines(L + "/include/asic_reg/" + b->hdr + "_offset.h");
    auto sh = defines(L + "/include/asic_reg/" + b->hdr + "_sh_mask.h");
    std::string ln = s.linux_name, key = "mm" + ln;
    if (!off.count(key)) die("Linux " + std::string(b->hdr) + "_offset.h has no " + key);
    uint64_t o = off[key], seg = off.count(key + "_BASE_IDX") ? off[key + "_BASE_IDX"] : 0;
    std::string bk = std::string(b->base) + "_BASE__INST0_SEG" + std::to_string(seg);
    if (!ipoff.count(bk)) die("vega20_ip_offset.h has no " + bk);
    uint64_t addr = ipoff[bk] + o;
    // cross-check against the reference driver's table
    std::string tgn = s.tg_name ? s.tg_name : (std::string(b->tgprefix) + ln);
    TgReg tr;
    if (s.tg_name && !*s.tg_name) {
      if (s.raw && s.raw != addr) die(ln + ": Linux address " + std::to_string(addr) + " != the reference's raw index " + std::to_string(s.raw));
    } else {
      auto mod = tg_module(regs_py, b->tgmod);
      if (!mod.count(tgn)) die("regs.py " + std::string(b->tgmod) + " has no " + tgn);
      tr = mod[tgn];
      if (tr.off != o || tr.seg != seg) die(tgn + ": regs.py (" + std::to_string(tr.off) + ", " + std::to_string(tr.seg) + ") != Linux (" +
                                            std::to_string(o) + ", " + std::to_string(seg) + ")");
    }
    char line[256];
    snprintf(line, sizeof line, "constexpr uint32_t %s_%s = 0x%llx;   // %s mm%s 0x%llx + %s 0x%llx\n", b->name, ln.c_str(),
             (unsigned long long)addr, b->hdr, ln.c_str(), (unsigned long long)o, bk.c_str(), (unsigned long long)ipoff[bk]);
    h << line; nregs++;
    table << "  {\"" << b->name << "." << ln << "\", " << b->name << "_" << ln << "},\n";
    for (const auto &f : s.fields) {
      std::string sk = ln + "__" + f + "__SHIFT", mk = ln + "__" + f + "_MASK";
      if (!sh.count(sk) || !sh.count(mk)) die("Linux " + std::string(b->hdr) + "_sh_mask.h has no " + sk + " / " + mk);
      uint64_t shift = sh[sk], mask = sh[mk];
      if (!tr.fields.empty() || (s.tg_name == nullptr || *s.tg_name)) {
        auto it = tr.fields.find(lower(f));
        if (it == tr.fields.end()) die(tgn + ": regs.py has no field " + lower(f));
        uint64_t lo = it->second.first, hi = it->second.second, tmask = (((1ull << (hi - lo + 1)) - 1) << lo) & 0xFFFFFFFFull;
        if (lo != shift || tmask != mask) die(tgn + "." + f + ": regs.py bits " + std::to_string(lo) + ".." + std::to_string(hi) + " != Linux");
      }
      snprintf(line, sizeof line, "constexpr uint32_t %s_%s__%s__SHIFT = %llu, %s_%s__%s_MASK = 0x%llxu;\n", b->name, ln.c_str(), f.c_str(),
               (unsigned long long)shift, b->name, ln.c_str(), f.c_str(), (unsigned long long)mask);
      h << line; nfields++;
    }
  }
  // GC 9.4.0's golden registers: AM_GFX.VEGA20_GOLDEN in the reference's ip.py (runtime/support/am/ip.py, the same tinygrad tree as regs.py),
  // (name, offset, base index, and_mask, or_mask) in its order; each name's offset and base index checked against Linux gc_9_0_offset.h
  {
    const std::string rp = argv[2], suffix = "/autogen/am/regs.py";
    if (rp.size() < suffix.size() || rp.compare(rp.size() - suffix.size(), suffix.size(), suffix)) die("regs.py path does not end in " + suffix);
    const std::string ip_py = slurp(rp.substr(0, rp.size() - suffix.size()) + "/support/am/ip.py");
    size_t at = ip_py.find("VEGA20_GOLDEN = ["), end = at == std::string::npos ? at : ip_py.find("]\n", at);
    if (at == std::string::npos || end == std::string::npos) die("ip.py: no VEGA20_GOLDEN list");
    const std::string list = ip_py.substr(at, end - at);
    static const std::regex ent(R"(\('(\w+)', (0x[0-9a-fA-F]+), (\d), (0x[0-9a-fA-F]+), (0x[0-9a-fA-F]+)\))");
    auto off = defines(L + "/include/asic_reg/gc/gc_9_0_offset.h");
    std::ostringstream g;
    size_t n = 0;
    for (std::sregex_iterator it(list.begin(), list.end(), ent), e; it != e; ++it, ++n) {
      std::string name = (*it)[1];
      uint64_t o = std::stoull((*it)[2], nullptr, 0), base = std::stoull((*it)[3]), am = std::stoull((*it)[4], nullptr, 0), om = std::stoull((*it)[5], nullptr, 0);
      std::string key = "mm" + name;
      if (!off.count(key)) die("golden " + name + ": Linux gc_9_0_offset.h has no " + key);
      uint64_t lseg = off.count(key + "_BASE_IDX") ? off[key + "_BASE_IDX"] : 0;
      if (off[key] != o || lseg != base) die("golden " + name + ": ip.py (" + std::to_string(o) + ", " + std::to_string(base) + ") != Linux (" +
                                             std::to_string(off[key]) + ", " + std::to_string(lseg) + ")");
      std::string bk = "GC_BASE__INST0_SEG" + std::to_string(base);
      if (!ipoff.count(bk)) die("vega20_ip_offset.h has no " + bk);
      char line[200];
      snprintf(line, sizeof line, "  {0x%llx, 0x%08llxu, 0x%08llxu},   // %s 0x%llx + %s\n", (unsigned long long)(ipoff[bk] + o),
               (unsigned long long)am, (unsigned long long)om, name.c_str(), (unsigned long long)o, bk.c_str());
      g << line;
    }
    if (!n || std::count(list.begin(), list.end(), '(') != (long)n) die("ip.py: VEGA20_GOLDEN has entries this parser did not read");
    h << "\n// GC 9.4.0 golden registers: AM_GFX.VEGA20_GOLDEN (ip.py), in its order: {dword address, and_mask, or_mask}; applied as\n"
         "// soc15_program_register_sequence: and_mask all ones writes or_mask, else (value & ~and_mask) | (or_mask & and_mask)\n"
         "struct GcGolden { uint32_t addr, and_mask, or_mask; };\nstatic constexpr GcGolden GC_GOLDEN[" << n << "] = {\n" << g.str() << "};\n";
    fprintf(stderr, "mi50_gen: GC golden registers: %zu, all at Linux's offsets\n", n);
  }
  h << "\n#ifdef MI50_REG_TABLE   // every register above, for tests that check a driver touches only named registers\n"
       "struct Named { const char *name; uint32_t addr; };\nstatic const Named ALL[] = {\n" << table.str() << "};\n#endif\n";
  h << "\n} }  // namespace mi50::reg\n#pragma clang diagnostic pop\n";
  fprintf(stderr, "mi50_gen: mi50::reg: %zu registers, %zu fields, all equal to tinygrad's regs.py\n", nregs, nfields);

  // ---------------- firmware ----------------
  // tinygrad autogen/am/fw.py hashes (the reference driver refuses any other file)
  const std::map<std::string, std::string> pinned = {
    {"vega20_sos.bin", "0b162ed3731537b410f3bb48fa89a0111752644abcd72e5c8ff6112c83b4a193"},
    {"vega20_asd.bin", "80ed634a9cec99e3520a6ac6fcb652b940bf81ca19f6f9aabccd91e1c8710482"},
    {"vega20_ta.bin", "aea10970b0530e2e11f84a5c0444315c78b109106a02a41b4eeaea3ac96d56dc"},
    {"vega20_sdma.bin", "1457d02c5c7bc0d69b7aa456867cd99d73789441b34ff2c6c982a87d9d600875"},
    {"vega20_sdma1.bin", "d61b687e9f3bcd930b236f2922ff352de3209c99e026403be6c9a59f2add1b90"},
    {"vega20_ce.bin", "aa168fc7d3d99c323fd445674eb0227bb5fa0072d95e3e95e9e16e3b3ba55c0f"},
    {"vega20_pfp.bin", "37bfd1994b757ff3cef7edb81d0ab969831f689d0cfcf3e9f2f63d7bf148f197"},
    {"vega20_me.bin", "a5889a7bb174437d11125565132de58e00a5ddf0b84104df96ed3930e5fd1c3c"},
    {"vega20_mec.bin", "7275323c00200905f6c1efaadb7d18d5e9f76f0a3edfa06cc86fc8b2bb2e904e"},
    {"vega20_mec2.bin", "7275323c00200905f6c1efaadb7d18d5e9f76f0a3edfa06cc86fc8b2bb2e904e"},
    {"vega20_rlc.bin", "786416def28a7a2f2db4abd5cecae5d4694fe1368f011ce2b26e100f9f008a4a"},
    {"vega20_smc.bin", "ba35e00f1971b63044a0d6c7795f1db82ab4a9925747d73e77b4de9657d51dfc"},
  };
  std::map<std::string, std::string> blob;
  for (const auto &p : pinned) {
    std::string b = slurp(fwdir + "/" + p.first);
    unsigned char d[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(b.data(), (CC_LONG)b.size(), d);
    char hex[65]; for (int i = 0; i < 32; i++) snprintf(hex + 2 * i, 3, "%02x", d[i]);
    if (p.second != hex) die(p.first + ": sha256 " + hex + " != pinned " + p.second);
    blob[p.first] = b;
  }
  auto u16 = [](const std::string &b, size_t o) { if (o + 2 > b.size()) die("firmware header read past the file"); return (uint32_t)(uint8_t)b[o] | (uint32_t)(uint8_t)b[o + 1] << 8; };
  auto u32 = [&](const std::string &b, size_t o) { return u16(b, o) | u16(b, o + 2) << 16; };
  auto cut = [](const std::string &b, uint64_t o, uint64_t n) { if (o + n > b.size()) die("firmware payload past the file"); return b.substr(o, n); };
  // amdgpu_ucode.h: common_firmware_header {size_bytes @0, header_size_bytes @4, header_version_major @8 / minor @10, ip @12/14,
  // ucode_version @16, ucode_size_bytes @20, ucode_array_offset_bytes @24, crc32 @28} (32 bytes)
  auto gen = [&](const std::string &b) { return cut(b, u32(b, 24), u32(b, 20)); };
  struct Fw { std::string name; uint32_t type; std::string data; };
  std::vector<Fw> ip;
  // psp_firmware_header_v1_0 = common + psp_fw_legacy_bin_desc sos {fw_version @32, offset_bytes @36, size_bytes @40}; the reference asserts v1.0
  const std::string &s = blob["vega20_sos.bin"];
  if (u16(s, 8) != 1 || u16(s, 10) != 0) die("vega20_sos.bin: expected a v1.0 psp header");
  uint32_t base = u32(s, 24);
  std::string sysdrv = cut(s, base, u32(s, 36)), sos = cut(s, base + u32(s, 36), u32(s, 40));
  // psp_gfx_if.h enum psp_gfx_fw_type
  std::map<std::string, uint64_t> en;   // enum psp_gfx_fw_type members (not #defines)
  {
    static const std::regex re(R"(^\s*(GFX_FW_TYPE_\w+)\s*=\s*(\d+)\s*,)");
    std::istringstream in(slurp(L + "/amdgpu/psp_gfx_if.h")); std::string line; std::smatch m;
    while (std::getline(in, line)) if (std::regex_search(line, m, re)) en[m[1]] = std::stoull(m[2]);
  }
  auto T = [&](const char *n) { if (!en.count(n)) die(std::string("psp_gfx_if.h has no ") + n); return (uint32_t)en[n]; };
  ip.push_back({"vega20_sdma.bin", T("GFX_FW_TYPE_SDMA0"), gen(blob["vega20_sdma.bin"])});
  ip.push_back({"vega20_sdma1.bin", T("GFX_FW_TYPE_SDMA1"), gen(blob["vega20_sdma1.bin"])});
  ip.push_back({"vega20_ce.bin", T("GFX_FW_TYPE_CP_CE"), gen(blob["vega20_ce.bin"])});
  ip.push_back({"vega20_pfp.bin", T("GFX_FW_TYPE_CP_PFP"), gen(blob["vega20_pfp.bin"])});
  ip.push_back({"vega20_me.bin", T("GFX_FW_TYPE_CP_ME"), gen(blob["vega20_me.bin"])});
  // gfx_firmware_header_v1_0 = common + ucode_feature_version @32, jt_offset @36, jt_size @40: code = ucode_size - jt_size*4, JT at jt_offset*4
  for (auto [n, jt] : {std::pair<const char *, const char *>{"vega20_mec.bin", "GFX_FW_TYPE_CP_MEC_ME1"}, {"vega20_mec2.bin", "GFX_FW_TYPE_CP_MEC_ME2"}}) {
    const std::string &b = blob[n];
    uint32_t off = u32(b, 24), sz = u32(b, 20), jo = u32(b, 36), js = u32(b, 40);
    if (js * 4 > sz) die(std::string(n) + ": jt larger than the ucode");
    ip.push_back({n, T("GFX_FW_TYPE_CP_MEC"), cut(b, off, sz - js * 4)});
    ip.push_back({n, T(jt), cut(b, off + jo * 4, js * 4)});
  }
  // rlc_firmware_header_v2_1: save_restore_list_{cntl,gpm,srm}_size_bytes @116/@132/@148, _offset_bytes @120/@136/@152; only when sized
  const std::string &r = blob["vega20_rlc.bin"];
  if (u16(r, 10) >= 1) {
    for (auto [so, oo, t] : {std::tuple<int, int, const char *>{116, 120, "GFX_FW_TYPE_RLC_RESTORE_LIST_SRM_CNTL"},
                             {132, 136, "GFX_FW_TYPE_RLC_RESTORE_LIST_GPM_MEM"}, {148, 152, "GFX_FW_TYPE_RLC_RESTORE_LIST_SRM_MEM"}})
      if (u32(r, so)) ip.push_back({"vega20_rlc.bin", T(t), cut(r, u32(r, oo), u32(r, so))});
  }
  ip.push_back({"vega20_rlc.bin", T("GFX_FW_TYPE_RLC_G"), gen(r)});
  ip.push_back({"vega20_smc.bin", T("GFX_FW_TYPE_SMU"), gen(blob["vega20_smc.bin"])});
  std::string asd = gen(blob["vega20_asd.bin"]);
  // ta_firmware_header_v1_0 = common + xgmi desc @32 + ras desc {fw_version @44, offset_bytes @48, size_bytes @52}
  const std::string &ta = blob["vega20_ta.bin"];
  std::string ras = cut(ta, (uint64_t)u32(ta, 24) + u32(ta, 48), u32(ta, 52));

  std::ostringstream f;
  f << "// ---- mi50::fw: linux-firmware amdgpu/vega20_*.bin (redistributable under AMD's linux-firmware license), SHA-256 equal to tinygrad's\n"
       "// autogen/am/fw.py, cut into the payloads the PSP is handed, in load order (amdev.py AMFirmware._init_vega20 = Linux\n"
       "// psp_load_non_psp_fw ucode enum order).\nnamespace mi50 { namespace fw {\n";
  int idx = 0;
  auto emit = [&](const std::string &name, const std::string &d) {
    f << "static const uint8_t " << name << "[" << d.size() << "] __attribute__((aligned(16))) = {";
    for (size_t i = 0; i < d.size(); i++) { if (i % 32 == 0) f << "\n"; f << (unsigned)(uint8_t)d[i] << ","; }
    f << "\n};\n";
    // MI50Device::psp_prep_msg1 copies an image + 4 zero bytes, padded to 16, into the 1 MiB msg1 buffer
    f << "static_assert(sizeof(" << name << ") % 4 == 0 && sizeof(" << name << ") + 4 <= (1u << 20), \"" << name
      << ": psp_prep_msg1 needs a whole number of dwords that fits msg1 with 4 zero bytes\");\n";
  };
  emit("SYS_DRV", sysdrv); emit("SOS", sos); emit("ASD", asd); emit("RAS_TA", ras);
  for (auto &x : ip) emit("IP_" + std::to_string(idx++), x.data);
  f << "struct IpFw { uint32_t type; const uint8_t *data; uint32_t size; const char *file; };\n";
  f << "static const IpFw IP_FW[" << ip.size() << "] = {\n";
  idx = 0;
  for (auto &x : ip) { f << "  {" << x.type << ", IP_" << idx << ", " << x.data.size() << ", \"" << x.name << "\"},\n"; idx++; }
  f << "};\nconstexpr uint32_t IP_FW_COUNT = " << ip.size() << ";\n\n} }  // namespace mi50::fw\n";
  fprintf(stderr, "mi50_gen: mi50::fw: sys_drv %zu, sos %zu, asd %zu, ras %zu, %zu IP images\n", sysdrv.size(), sos.size(), asd.size(), ras.size(),
          ip.size());

  // mi50::pp: the V420's powerplayinfo table, found the way atom.c / amdgpu_atombios find a data table (ROM header pointer at 0x48,
  // masterdatatable_offset, the master list entry) and checked as check_powerplay_tables (vega20_processpptables.c:45-91) checks it
  const char *V420_SHA = "c737534ca7fb85beb62b10cceb8e3c169096b430c9f33903b2eaad2c86592ff7";   // https://katastrophos.net/downloads/InstinctMI50/V420.rom
  const std::string v = slurp(v420_path);
  {
    unsigned char d[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(v.data(), (CC_LONG)v.size(), d);
    char hex[65]; for (int i = 0; i < 32; i++) snprintf(hex + 2 * i, 3, "%02x", d[i]);
    if (std::string(V420_SHA) != hex) die(v420_path + ": sha256 " + hex + " != pinned " + V420_SHA);
  }
  auto r16 = [&](size_t o) { if (o + 2 > v.size()) die("V420.rom: read past the image"); return (uint32_t)(uint8_t)v[o] | (uint32_t)(uint8_t)v[o + 1] << 8; };
  if (v.size() < 0x200 || (uint8_t)v[0] != 0x55 || (uint8_t)v[1] != 0xaa) die("V420.rom: no 55 AA ROM signature");
  uint32_t rom_hdr = r16(0x48);
  if (rom_hdr + 8 > v.size() || v.compare(rom_hdr + 4, 4, "ATOM")) die("V420.rom: no ATOM signature in the ROM header");
  uint32_t mdt = r16(rom_hdr + offsetof(struct atom_rom_header_v2_2, masterdatatable_offset));
  uint32_t ppt = r16(mdt + offsetof(struct atom_master_data_table_v2_1, listOfdatatables) + offsetof(struct atom_master_list_of_data_tables_v2_1, powerplayinfo));
  if (!ppt) die("V420.rom: no powerplayinfo table");
  uint32_t ppsize = r16(ppt);   // atom_common_table_header.structuresize
  if (ppt + ppsize > v.size()) die("V420.rom: powerplayinfo past the image");
  if (ppsize < sizeof(ATOM_Vega20_POWERPLAYTABLE)) die("V420.rom: Invalid PowerPlay Table (" + std::to_string(ppsize) + " bytes)");
  ATOM_Vega20_POWERPLAYTABLE pp; memcpy(&pp, v.data() + ppt, sizeof(pp));
  if (pp.sHeader.format_revision < ATOM_VEGA20_TABLE_REVISION_VEGA20) die("V420.rom: Unsupported PPTable format revision");
  if (pp.smcPPTable.Version != PPTABLE_V20_SMU_VERSION) die("V420.rom: Unmatch PPTable version");
  std::ostringstream p;
  p << "// ---- mi50::pp: the PowerPlay table (powerplayinfo, ATOM_Vega20_POWERPLAYTABLE, " << ppsize << " bytes at 0x" << std::hex << ppt << std::dec
    << ") of the Radeon Pro V420 32 GB VBIOS\n// 113-D1640200-043 (V420.rom, https://katastrophos.net/downloads/InstinctMI50/V420.rom, SHA-256 " << V420_SHA
    << "),\n// byte for byte: the table the MI50 boots on in place of its own (Leon 2026-10-16). MI50Device::build_boot_pptable applies Leon's\n"
       "// settings on it: the card's own thermal limits, fan control off.\nnamespace mi50 { namespace pp {\n"
    << "static constexpr uint8_t V420_PPTABLE[" << ppsize << "] __attribute__((aligned(4))) = {";
  for (uint32_t i = 0; i < ppsize; i++) { if (i % 32 == 0) p << "\n"; p << (unsigned)(uint8_t)v[ppt + i] << ","; }
  p << "\n};\n\n} }  // namespace mi50::pp\n";
  unsigned top = 0; for (auto f : pp.smcPPTable.FreqTableGfx) if (f > top) top = f;
  fprintf(stderr, "mi50_gen: mi50::pp: V420 powerplayinfo %u bytes (rev %u.%u), %u W, GFXCLK top %u MHz\n", ppsize, (unsigned)(uint8_t)v[ppt + 2],
          (unsigned)(uint8_t)v[ppt + 3], (unsigned)pp.smcPPTable.SocketPowerLimitAc0, top);

  // mi50::od: the recorded OverDrive table, byte for byte, or none yet
  std::ostringstream od;
  od << "// ---- mi50::od: the built-in OverDrive table (OverDriveTable_t), written to the SMU after the boot's OverDrive setup when no valid\n"
        "// overdrive file is supplied: the SMU's own table, recorded once on the card after the V420 boot.\n// ";
  std::string rec;
  if (argc == 7) {
    rec = slurp(argv[6]);
    if (rec.size() != sizeof(OverDriveTable_t)) die(std::string(argv[6]) + ": " + std::to_string(rec.size()) + " bytes, not an OverDriveTable_t (" +
                                                    std::to_string(sizeof(OverDriveTable_t)) + ")");
    unsigned char d[CC_SHA256_DIGEST_LENGTH];   // named by its content, not by where the file sits: the output is the same from any path
    CC_SHA256(rec.data(), (CC_LONG)rec.size(), d);
    char hex[65]; for (int i = 0; i < 32; i++) snprintf(hex + 2 * i, 3, "%02x", d[i]);
    od << "Recorded: SHA-256 " << hex << ".\n";
  } else {
    od << "None recorded yet: HAVE_DEFAULT is false and the driver keeps the table the SMU reports at boot.\n";
  }
  od << "namespace mi50 { namespace od {\nconstexpr bool HAVE_DEFAULT = " << (rec.empty() ? "false" : "true") << ";\n"
     << "static constexpr uint8_t DEFAULT[" << sizeof(OverDriveTable_t) << "] __attribute__((aligned(4))) = {";
  for (size_t i = 0; i < rec.size(); i++) od << (unsigned)(uint8_t)rec[i] << ",";
  od << "};\n\n} }  // namespace mi50::od\n";
  fprintf(stderr, "mi50_gen: mi50::od: %s\n", rec.empty() ? "no recorded table (HAVE_DEFAULT false)" : "recorded table, 32 bytes");

  // into the driver: the lines between the two markers are replaced, everything else in the file is kept byte for byte
  static const std::string BEGIN = "// ==== BEGIN mi50_gen output: GENERATED by mi50-tools/mi50_gen.cpp from the primary sources; do not edit, rerun mi50_gen ====\n",
                           END = "// ==== END mi50_gen output ====\n";
  std::string src = slurp(target);
  size_t b = src.find(BEGIN), e = src.find(END);
  if (b == std::string::npos || e == std::string::npos || e < b || src.find(BEGIN, b + 1) != std::string::npos || src.find(END, e + 1) != std::string::npos)
    die(target + ": needs exactly one BEGIN and one END mi50_gen output line, in that order");
  std::string out = src.substr(0, b + BEGIN.size()) + h.str() + "\n" + f.str() + "\n" + p.str() + "\n" + od.str() + src.substr(e);
  std::ofstream o(target, std::ios::binary | std::ios::trunc);
  if (!(o << out) || !(o.flush())) die("cannot write " + target);
  fprintf(stderr, "mi50_gen: wrote the generated section of %s (%zu bytes)\n", target.c_str(),
          h.str().size() + f.str().size() + p.str().size() + od.str().size() + 3);
  return 0;
}
