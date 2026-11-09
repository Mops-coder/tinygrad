#!/bin/zsh
# MI50 support in the TinyGPU driver: check that TinyGPUDriver.cpp's generated section is what mi50_gen makes from the primary sources, then
# compile the driver's own files against the DriverKit stand-ins and run the offline tests.
#   build_and_test.sh <linux drivers/gpu/drm/amd> <tinygrad autogen/am/regs.py> <vega20 firmware dir> <V420.rom> <MI50.rom> <build dir> <MI50BarFix.cpp>
#                     [recorded OD table]
# V420.rom: the Radeon Pro V420 32 GB VBIOS (its PowerPlay table is the driver's boot table); MI50.rom: the MI50's own (113-D1631711-100);
# MI50BarFix.cpp: the BAR0 kext's source (its overdrive-file check is tested here); recorded OD table: the one the driver's mi50::od section
# was generated from (mi50_gen's last argument), when one was
set -euo pipefail
LINUX=$1 REGS=$2 FW=$3 ROM=$4 MI50ROM=$5 OUT=$6 KEXT=$7
OD_REC=(${@:8})
HERE=${0:A:h}
DRV=$HERE/../TinyGPUDriverExtension/TinyGPUDriver.cpp
mkdir -p $OUT
CXX=(clang++ -std=gnu++17 -Wall -Wextra -Werror)
LH=(-I $LINUX/include -I $LINUX/pm/powerplay/inc -I $LINUX/pm/powerplay/hwmgr -I $LINUX/amdgpu)   # Linux d6d3455's amdgpu headers, unmodified
SAN=(-g -O1 -fsanitize=address,undefined -fno-sanitize-recover=all)
TEST=($SAN -Wno-unused-parameter -I $HERE/test/fake_driverkit $LH)
# the generated section: mi50_gen on a copy of the driver must leave it byte for byte as it is
$CXX -O1 $LH -o $OUT/mi50_gen $HERE/mi50_gen.cpp
cp $DRV $OUT/TinyGPUDriver.cpp
$OUT/mi50_gen $LINUX $REGS $FW $ROM $OUT/TinyGPUDriver.cpp $OD_REC
cmp $DRV $OUT/TinyGPUDriver.cpp && echo "TinyGPUDriver.cpp: the generated section is mi50_gen's output, byte for byte"
# the built-in OverDrive table path: a copy of the driver generated with a TEST table (GfxclkFmin 900, GfxclkFmax 1700, UclkFmax 950,
# FanMaximumRpm 3000, the rest 0; u16 little endian), run through t_dext below; a table of the wrong size is refused
mkdir -p $OUT/od_default
printf '\x84\x03\xa4\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xb6\x03\x00\x00\xb8\x0b\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00' > $OUT/od_default/od_test.bin
cp $DRV $OUT/od_default/TinyGPUDriver.cpp
$OUT/mi50_gen $LINUX $REGS $FW $ROM $OUT/od_default/TinyGPUDriver.cpp $OUT/od_default/od_test.bin
head -c 31 $OUT/od_default/od_test.bin > $OUT/od_default/od_short.bin
if $OUT/mi50_gen $LINUX $REGS $FW $ROM $OUT/od_default/TinyGPUDriver.cpp $OUT/od_default/od_short.bin 2> $OUT/od_default/short.log; then
  echo "FAIL mi50_gen took a 31-byte OverDrive table"; exit 1
fi
grep -q "31 bytes, not an OverDriveTable_t (32)" $OUT/od_default/short.log && echo "ok   mi50_gen refuses a 31-byte OverDrive table"
# the bring-up (MI50Device) on the virtual MI50
$CXX $TEST -o $OUT/t_mi50 $HERE/test/t_mi50.cpp
$OUT/t_mi50 $FW $ROM $MI50ROM
# the driver's MI50 power-up path (StartMI50, the link partner, the overdrive file, TinyGPUDriver::Start on the card)
$CXX $TEST -o $OUT/t_dext $HERE/test/t_dext.cpp -lz
$OUT/t_dext $FW $ROM
# the same on the copy with the built-in TEST table (mi50::od::HAVE_DEFAULT true)
$CXX $TEST -DMI50_DRIVER_CPP="\"$OUT/od_default/TinyGPUDriver.cpp\"" -o $OUT/t_dext_od $HERE/test/t_dext.cpp -lz
$OUT/t_dext_od $FW $ROM
# egpu_vega20.py moved into the driver: the tuning (controls, limits, readouts, perf, OverDrive) on the virtual MI50
$CXX $TEST -o $OUT/t_vega20 $HERE/test/t_vega20.cpp -lz
$OUT/t_vega20 $FW
# the fan control switch hardcoded on (Leon 2026-10-17: once the fan is fitted): the bring-up, the driver's power-up path and the tuning on a
# copy of the driver whose only difference is "#define MI50_FAN_CONTROL 1" (review R6-10: no build flag sets the switch)
mkdir -p $OUT/fan_on
sed 's/^#define MI50_FAN_CONTROL 0$/#define MI50_FAN_CONTROL 1/' $DRV > $OUT/fan_on/TinyGPUDriver.cpp
[[ $(diff $DRV $OUT/fan_on/TinyGPUDriver.cpp | grep -c '^[<>]') -eq 2 ]] || { echo "FAIL the fan-on copy differs from the driver in more than its switch line"; exit 1; }
echo "ok   fan_on/TinyGPUDriver.cpp = the driver with only #define MI50_FAN_CONTROL 1"
FANCPP=(-DMI50_DRIVER_CPP="\"$OUT/fan_on/TinyGPUDriver.cpp\"")
$CXX $TEST $FANCPP -o $OUT/t_mi50_fan $HERE/test/t_mi50.cpp
$OUT/t_mi50_fan $FW $ROM $MI50ROM
$CXX $TEST $FANCPP -o $OUT/t_dext_fan $HERE/test/t_dext.cpp -lz
$OUT/t_dext_fan $FW $ROM
$CXX $TEST $FANCPP -o $OUT/t_vega20_fan $HERE/test/t_vega20.cpp -lz
$OUT/t_vega20_fan $FW
# MI50BarFix.kext's overdrive-file frame check, cut from the kext's source between its BEGIN / END od_file_ok markers
sed -n '/^\/\/ ---- BEGIN od_file_ok/,/^\/\/ ---- END od_file_ok/p' $KEXT > $OUT/od_file_ok.inc
[[ $(grep -c 'od_file_ok' $OUT/od_file_ok.inc) -ge 3 ]] || { echo "$KEXT: no od_file_ok block"; exit 1; }
$CXX $SAN -I $OUT -o $OUT/t_kext_od $HERE/test/t_kext_od.cpp
$OUT/t_kext_od
# the driver's vendor paths (user client, DMA, config, reset, Stop) and the MI50 hook without a card
$CXX $TEST -o $OUT/t_driver $HERE/test/t_driver.cpp
$OUT/t_driver
# the driver's ATOM interpreter against Linux's own atom.c, compiled unmodified. A copy sits in $OUT so its #include "amdgpu.h" finds the
# test shim instead of the real header next to it; -ftrivial-auto-var-init=zero: see test/t_atom_diff.cpp.
LINC=(-I $HERE/test/linux_atom_shim -I $LINUX/include -I $LINUX/amdgpu)
cp $LINUX/amdgpu/atom.c $OUT/atom.c
clang -std=gnu11 $SAN -ftrivial-auto-var-init=zero -w $LINC -c $OUT/atom.c -o $OUT/linux_atom.o
clang++ -std=gnu++17 $SAN -w $LINC -c $HERE/test/oracle_wrap.cpp -o $OUT/oracle_wrap.o
$CXX $TEST -o $OUT/t_atom_diff $HERE/test/t_atom_diff.cpp $OUT/oracle_wrap.o $OUT/linux_atom.o
$OUT/t_atom_diff 20000
