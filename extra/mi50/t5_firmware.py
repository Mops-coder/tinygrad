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
print("vega20 firmware self-check ok")
