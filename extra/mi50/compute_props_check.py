#!/usr/bin/env python3
"""B17 (design v2 §2 B17 / §3 ops_amd rows): offline check of the compute-side host path for Vega 20 (MI50, gfx906). Needs no card.

Exercises PCIIface._compute_props (tinygrad/runtime/ops_amd.py) on a FAKE dev_impl whose gc_info is a struct_gc_info_v2_0 (autogen/am/am.py:2581,
:2601 - the shape the amdev side synthesizes from the VBIOS gfx_info v2.4). The six gc_info values are NOT known offline: they come from the VBIOS
at T2b, so they are REQUIRED command-line inputs (no defaults). What is checked is the computation, not the numbers:
(1) GC (9,4,0) -> gfx_target_version 90006 (kfd_device.c:330-332), target (9,0,6), arch "gfx906", AMDDevice's assert accepts it;
(2) props fields are exactly the v2 names (gc_num_se, gc_num_sh_per_se, gc_num_cu_per_sh, gc_max_scratch_slots_per_cu, gc_max_waves_per_simd,
    gc_lds_size) with xccs = 1 (amdgpu_discovery.c:3196,3206 CHIP_VEGA20 `adev->gfx.xcc_mask = 1`), and the AMDDevice.__init__ derivations
    (ops_amd.py cu_cnt/se_cnt/waves_per_cu/wave_cnt lines) are printed from them;
(3) a v1-shaped gc_info on GC (9,4,0) is refused (the fork's RuntimeError);
(4) regression: GC (9,4,3) v2 -> 90402 and GC (11,0,0) v1 -> 110000 exactly as before (the map only gained the 90400 key);
(5) the register gates: gc_9_0 has mmCOMPUTE_PGM_RSRC1/2 and NO COMPUTE_PGM_RSRC3 (gc_9_0_offset.h:2214-2217), gc_9_4_3 and gc_11_0_0 have RSRC3
    (so the (9,0,6) gate in AMDComputeQueue.exec is the only behaviour change); nbio (7,4,0) resolves regBIF_BX_PF0_GPU_HDP_FLUSH_REQ/DONE through
    AMDIP (memory_barrier unchanged) at the dword the fork README verified (0xe26/0xe27 = seg2 0xD20 + 262/263);
(6) 0x66a1 is in the PCIIface device tuple and "1002:66a1" sorts FIRST among every id in that tuple under System.pci_scan_bus's sorted().
Run: PYTHONPATH=<fork> python3 extra/mi50/compute_props_check.py --gc_num_se N --gc_num_sh_per_se N --gc_num_cu_per_sh N \
       --gc_max_scratch_slots_per_cu N --gc_max_waves_per_simd N --gc_lds_size N   (all six from the T2b VBIOS gfx_info dump)
"""
import argparse, types, sys
from tinygrad.runtime.autogen.am import am, vega_offsets
from tinygrad.runtime.support.amd import AMDIP, import_module
from tinygrad.runtime.ops_amd import PCIIface

V2_FIELDS = ("gc_num_se", "gc_num_sh_per_se", "gc_num_cu_per_sh", "gc_max_scratch_slots_per_cu", "gc_max_waves_per_simd", "gc_lds_size")
ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
for f in V2_FIELDS: ap.add_argument(f"--{f}", type=int, required=True, help="from the VBIOS gfx_info (T2b); no default on purpose")
args = ap.parse_args()
for f in V2_FIELDS: assert getattr(args, f) > 0, f"{f} must be > 0 (got {getattr(args, f)})"

fails: list[str] = []
def check(cond, msg):
  print(("ok   " if cond else "FAIL ") + msg)
  if not cond: fails.append(msg)

def gc_info_v2(**vals):
  gi = am.struct_gc_info_v2_0(); gi.header.version_major = 2; gi.header.version_minor = 0
  for k, v in vals.items(): setattr(gi, k, v)
  return gi
def gc_info_v1(num_se, wgp0, wgp1, sa_per_se, scratch, waves, lds):
  gi = am.struct_gc_info_v1_0(); gi.header.version_major = 1
  gi.gc_num_se, gi.gc_num_wgp0_per_sa, gi.gc_num_wgp1_per_sa, gi.gc_num_sa_per_se = num_se, wgp0, wgp1, sa_per_se
  gi.gc_max_scratch_slots_per_cu, gi.gc_max_waves_per_simd, gi.gc_lds_size = scratch, waves, lds
  return gi
def fake_iface(gc_ver, gc_info, xccs):
  # only what PCIIface._compute_props reads: dev_impl.ip_ver[GC_HWIP], dev_impl.gc_info, dev_impl.gfx.xccs
  return types.SimpleNamespace(dev_impl=types.SimpleNamespace(ip_ver={am.GC_HWIP: gc_ver}, gc_info=gc_info, gfx=types.SimpleNamespace(xccs=xccs)))
def props_for(gc_ver, gc_info, xccs=1):
  f = fake_iface(gc_ver, gc_info, xccs); PCIIface._compute_props(f); return f.props
def decode_target(props):  # the two AMDDevice.__init__ lines that turn gfx_target_version into target/arch
  trgt = props['gfx_target_version']; target = (trgt // 10000, (trgt // 100) % 100, trgt % 100)
  return target, "gfx%d%x%x" % target

# (1)+(2) Vega 20: GC (9,4,0), xcc_mask = 1 (amdgpu_discovery.c:3206, CHIP_VEGA20 case at :3196), v2 gc_info from the CLI inputs
vals = {f: getattr(args, f) for f in V2_FIELDS}
p = props_for((9,4,0), gc_info_v2(**vals), xccs=1)
target, arch = decode_target(p)
check(p['gfx_target_version'] == 90006, f"GC (9,4,0) -> gfx_target_version {p['gfx_target_version']} (kfd_device.c:332 says 90006)")
check(target == (9,0,6) and arch == "gfx906", f"target {target} arch {arch}")
check((target in ((9,0,6),(9,4,2),(9,5,0))) or target[0] in (11, 12), "AMDDevice assert expression accepts (9,0,6)")
array_count = vals['gc_num_sh_per_se'] * vals['gc_num_se'] * 1
expect = {'cu_per_simd_array': vals['gc_num_cu_per_sh'], 'simd_count': 2 * vals['gc_num_cu_per_sh'] * array_count, 'simd_per_cu': 2,
          'array_count': array_count, 'max_slots_scratch_cu': vals['gc_max_scratch_slots_per_cu'], 'max_waves_per_simd': vals['gc_max_waves_per_simd'],
          'simd_arrays_per_engine': vals['gc_num_sh_per_se'], 'lds_size_in_kb': vals['gc_lds_size'], 'num_xcc': 1, 'gfx_target_version': 90006}
check(p == expect, f"props from the v2 fields == {expect}")
# AMDDevice.__init__ derivations (ops_amd.py: xccs/se_cnt/cu_cnt/waves_per_cu/wave_cnt lines; wave_cnt = KFD get_num_waves gfxv<100100, kfd_queue.c)
xccs = p['num_xcc']; se_cnt = p['array_count'] // p['simd_arrays_per_engine'] // xccs; cu_cnt = p['simd_count'] // p['simd_per_cu'] // xccs
waves_per_cu = p['max_waves_per_simd'] * p['simd_per_cu']; wave_cnt = min(cu_cnt * 40, se_cnt * xccs * 512)
print(f"     AMDDevice would derive: se_cnt={se_cnt} cu_cnt={cu_cnt} waves_per_cu={waves_per_cu} wave_cnt={wave_cnt} "
      f"(inputs: {vals}; these are exercise values unless they came from the T2b dump)")
check(cu_cnt == vals['gc_num_cu_per_sh'] * vals['gc_num_sh_per_se'] * vals['gc_num_se'] and se_cnt == vals['gc_num_se'],
      "cu_cnt = cu_per_sh*sh_per_se*num_se and se_cnt = num_se round-trip through the props")

# (3) v1-shaped gc_info on (9,4,0) is refused
try: props_for((9,4,0), gc_info_v1(4, 1, 1, 1, 32, 10, 64)); check(False, "v1 gc_info on GC (9,4,0) must raise")
except RuntimeError as e: check("gc_info v2" in str(e), f"v1 gc_info on GC (9,4,0) refused: {e}")

# (4) other cards: byte-for-byte the same map result as before (only the 90400 key was added)
p943 = props_for((9,4,3), gc_info_v2(**vals), xccs=8); check(p943['gfx_target_version'] == 90402, f"GC (9,4,3) -> {p943['gfx_target_version']} (unchanged 90402)")
p110 = props_for((11,0,0), gc_info_v1(2, 2, 2, 2, 32, 16, 64)); check(p110['gfx_target_version'] == 110000, f"GC (11,0,0) v1 -> {p110['gfx_target_version']} (unchanged)")
check(decode_target(p110)[1] == "gfx1100" and decode_target(p943)[1] == "gfx942", "gfx1100 / gfx942 arch strings unchanged")
check(props_for((11,0,2), gc_info_v1(2, 2, 2, 2, 32, 16, 64))['gfx_target_version'] == 110002, "GC (11,0,2) (RX 7600 XT family id) -> 110002 unchanged")

# (5) register gates
gc90 = import_module('gc', (9,4,0), submod='regs'); gc943 = import_module('gc', (9,4,3), submod='regs'); gc110 = import_module('gc', (11,0,0), submod='regs')
check('mmCOMPUTE_PGM_RSRC1' in gc90 and 'mmCOMPUTE_PGM_RSRC2' in gc90, "gc (9,4,0) table has mmCOMPUTE_PGM_RSRC1/2")
check(not any(k.endswith('COMPUTE_PGM_RSRC3') for k in gc90), "gc (9,4,0) table has NO COMPUTE_PGM_RSRC3 -> exec() skips the write only for (9,0,6)")
check('regCOMPUTE_PGM_RSRC3' in gc943 and 'regCOMPUTE_PGM_RSRC3' in gc110, "gc (9,4,3) and (11,0,0) keep regCOMPUTE_PGM_RSRC3 (write unchanged)")
nbio = AMDIP('nbio', (7,4,0), bases={i: tuple(getattr(vega_offsets, f'NBIO_BASE__INST{i}_SEG{s}', 0) for s in range(9)) for i in range(6)})
req, done = nbio.regBIF_BX_PF0_GPU_HDP_FLUSH_REQ, nbio.regBIF_BX_PF0_GPU_HDP_FLUSH_DONE
check((req.addr[0], done.addr[0]) == (0xe26, 0xe27), f"nbio (7,4,0) memory_barrier regs resolve: REQ {req.addr[0]:#x} DONE {done.addr[0]:#x}")

# (6) device tuple + sort order (System.pci_scan_bus returns sorted() "vendor:device" strings)
import inspect
src = inspect.getsource(PCIIface.__init__)
ids = [int(x, 16) for x in __import__('re').findall(r'0x[0-9a-f]{4}', src.split('devices=')[1].split(')),')[0])]
check(0x66a1 in ids, f"PCIIface device tuple contains 0x66a1: {[hex(i) for i in ids]}")
order = sorted(f"1002:{d:x}" for d in ids)
check(order[0] == "1002:66a1", f"sorted() order puts the MI50 first: {order[0]} ... (so with another AMD card present it is AMD:0)")

print(f"\n{len(fails)} failure(s)" + (": " + "; ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
