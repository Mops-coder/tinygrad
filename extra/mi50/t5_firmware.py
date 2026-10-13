# T5 firmware parse (design v2 §7.1 T5, no card): recreated 2026-10-13 from /tmp/amfw_test.py (egpu/mi50/port/patches/README.md item 8) verbatim
# below the sys.path line. Run: PYTHONPATH=<fork> python3 extra/mi50/t5_firmware.py  (fetch_fw reads tinygrad's download cache / linux-firmware).
import sys; sys.path.insert(0, ".")
from tinygrad.runtime.support.am import amdev
from tinygrad.runtime.autogen.am import am
class FakeDev:
  ip_ver = {am.MP0_HWIP: (11,0,2), am.MP1_HWIP: (11,0,2), am.GC_HWIP: (9,4,0), am.SDMA0_HWIP: (4,2,0)}
  devfmt = "test"
fw = amdev.AMFirmware(FakeDev())
names = {v: k for k, v in vars(am).items() if (k.startswith("GFX_FW_TYPE_") or k.startswith("PSP_FW_TYPE_")) and isinstance(v, int)}
print("sos_fw:", {names[k]: len(v) for k, v in fw.sos_fw.items()})
print("descs (in PSP load order):"); [print(f"  {[names.get(t, t) for t in types]} {len(blob)} B") for types, blob in fw.descs]
print("smu_version", hex(fw.smu_version), "| asd", len(fw.asd_fw), "B | smu_psp_desc set:", hasattr(fw, "smu_psp_desc"))
assert len(fw.sos_fw[am.PSP_FW_TYPE_PSP_SYS_DRV]) == 112528 and len(fw.sos_fw[am.PSP_FW_TYPE_PSP_SOS]) == 62208
assert [t[0][0] for t in fw.descs][-1] == am.GFX_FW_TYPE_SMU and [t[0][0] for t in fw.descs][0] == am.GFX_FW_TYPE_SDMA0
# review 2026-10-13 L2: the exact Linux enum order (amdgpu_ucode.h:480-531) = design v2 §4 "PSP load order emitted by the fork"
WANT = [am.GFX_FW_TYPE_SDMA0, am.GFX_FW_TYPE_SDMA1, am.GFX_FW_TYPE_CP_CE, am.GFX_FW_TYPE_CP_PFP, am.GFX_FW_TYPE_CP_ME, am.GFX_FW_TYPE_CP_MEC, am.GFX_FW_TYPE_CP_MEC_ME1,
        am.GFX_FW_TYPE_CP_MEC, am.GFX_FW_TYPE_CP_MEC_ME2, am.GFX_FW_TYPE_RLC_RESTORE_LIST_SRM_CNTL, am.GFX_FW_TYPE_RLC_RESTORE_LIST_GPM_MEM,
        am.GFX_FW_TYPE_RLC_RESTORE_LIST_SRM_MEM, am.GFX_FW_TYPE_RLC_G, am.GFX_FW_TYPE_SMU]
assert [t[0][0] for t in fw.descs] == WANT, [names.get(t[0][0]) for t in fw.descs]
sizes = {names[t[0]] + ("_2" if i == 7 else ""): len(b) for i, (t, b) in enumerate(fw.descs)}
assert sizes["GFX_FW_TYPE_CP_CE"] == 9088 and sizes["GFX_FW_TYPE_CP_PFP"] == 21376 and sizes["GFX_FW_TYPE_CP_ME"] == 17280, sizes   # Firmware Facts :19
assert len(fw.descs[7][1]) == len(fw.descs[5][1]) == 267072 and len(fw.descs[8][1]) == 896, "mec2 code/JT sizes == mec (identical images, Firmware Facts :17)"
assert all(len(b) <= (1 << 20) for _, b in fw.descs), "every blob <= 1 MiB (PSP_1_MEG)"
print("PSP load order == Linux enum order (14 descriptors incl. CE/PFP/ME/MEC2)")
print("vega20 firmware self-check ok")
