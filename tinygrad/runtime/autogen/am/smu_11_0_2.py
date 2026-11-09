# mypy: disable-error-code="empty-body"
from __future__ import annotations
import ctypes
from typing import Literal, TypeAlias
from tinygrad.runtime.support.c import _IO, _IOW, _IOR, _IOWR
from tinygrad.runtime.support import c
PPSMC_Result: TypeAlias = ctypes.c_uint32
PPSMC_Msg: TypeAlias = ctypes.c_uint32
I2cControllerPort_e: dict[int, str] = {(I2C_CONTROLLER_PORT_0:=0): 'I2C_CONTROLLER_PORT_0', (I2C_CONTROLLER_PORT_1:=1): 'I2C_CONTROLLER_PORT_1'}
I2cControllerName_e: dict[int, str] = {(I2C_CONTROLLER_NAME_VR_GFX:=0): 'I2C_CONTROLLER_NAME_VR_GFX', (I2C_CONTROLLER_NAME_VR_SOC:=1): 'I2C_CONTROLLER_NAME_VR_SOC', (I2C_CONTROLLER_NAME_VR_VDDCI:=2): 'I2C_CONTROLLER_NAME_VR_VDDCI', (I2C_CONTROLLER_NAME_VR_HBM:=3): 'I2C_CONTROLLER_NAME_VR_HBM', (I2C_CONTROLLER_NAME_LIQUID_0:=4): 'I2C_CONTROLLER_NAME_LIQUID_0', (I2C_CONTROLLER_NAME_LIQUID_1:=5): 'I2C_CONTROLLER_NAME_LIQUID_1', (I2C_CONTROLLER_NAME_PLX:=6): 'I2C_CONTROLLER_NAME_PLX', (I2C_CONTROLLER_NAME_COUNT:=7): 'I2C_CONTROLLER_NAME_COUNT'}
I2cControllerThrottler_e: dict[int, str] = {(I2C_CONTROLLER_THROTTLER_TYPE_NONE:=0): 'I2C_CONTROLLER_THROTTLER_TYPE_NONE', (I2C_CONTROLLER_THROTTLER_VR_GFX:=1): 'I2C_CONTROLLER_THROTTLER_VR_GFX', (I2C_CONTROLLER_THROTTLER_VR_SOC:=2): 'I2C_CONTROLLER_THROTTLER_VR_SOC', (I2C_CONTROLLER_THROTTLER_VR_VDDCI:=3): 'I2C_CONTROLLER_THROTTLER_VR_VDDCI', (I2C_CONTROLLER_THROTTLER_VR_HBM:=4): 'I2C_CONTROLLER_THROTTLER_VR_HBM', (I2C_CONTROLLER_THROTTLER_LIQUID_0:=5): 'I2C_CONTROLLER_THROTTLER_LIQUID_0', (I2C_CONTROLLER_THROTTLER_LIQUID_1:=6): 'I2C_CONTROLLER_THROTTLER_LIQUID_1', (I2C_CONTROLLER_THROTTLER_PLX:=7): 'I2C_CONTROLLER_THROTTLER_PLX'}
I2cControllerProtocol_e: dict[int, str] = {(I2C_CONTROLLER_PROTOCOL_VR_XPDE132G5:=0): 'I2C_CONTROLLER_PROTOCOL_VR_XPDE132G5', (I2C_CONTROLLER_PROTOCOL_VR_IR35217:=1): 'I2C_CONTROLLER_PROTOCOL_VR_IR35217', (I2C_CONTROLLER_PROTOCOL_TMP_TMP102A:=2): 'I2C_CONTROLLER_PROTOCOL_TMP_TMP102A', (I2C_CONTROLLER_PROTOCOL_SPARE_0:=3): 'I2C_CONTROLLER_PROTOCOL_SPARE_0', (I2C_CONTROLLER_PROTOCOL_SPARE_1:=4): 'I2C_CONTROLLER_PROTOCOL_SPARE_1', (I2C_CONTROLLER_PROTOCOL_SPARE_2:=5): 'I2C_CONTROLLER_PROTOCOL_SPARE_2'}
I2cControllerSpeed_e: dict[int, str] = {(I2C_CONTROLLER_SPEED_SLOW:=0): 'I2C_CONTROLLER_SPEED_SLOW', (I2C_CONTROLLER_SPEED_FAST:=1): 'I2C_CONTROLLER_SPEED_FAST'}
@c.record
class I2cControllerConfig_t(c.Struct):
  SIZE = 28
  Enabled: int
  SlaveAddress: int
  ControllerPort: int
  ControllerName: int
  ThermalThrottler: int
  I2cProtocol: int
  I2cSpeed: int
uint32_t: TypeAlias = ctypes.c_uint32
I2cControllerConfig_t.register_fields([('Enabled', uint32_t, 0), ('SlaveAddress', uint32_t, 4), ('ControllerPort', uint32_t, 8), ('ControllerName', uint32_t, 12), ('ThermalThrottler', uint32_t, 16), ('I2cProtocol', uint32_t, 20), ('I2cSpeed', uint32_t, 24)])
@c.record
class QuadraticInt_t(c.Struct):
  SIZE = 12
  a: int
  b: int
  c: int
QuadraticInt_t.register_fields([('a', uint32_t, 0), ('b', uint32_t, 4), ('c', uint32_t, 8)])
@c.record
class LinearInt_t(c.Struct):
  SIZE = 8
  m: int
  b: int
LinearInt_t.register_fields([('m', uint32_t, 0), ('b', uint32_t, 4)])
@c.record
class DroopInt_t(c.Struct):
  SIZE = 12
  a: int
  b: int
  c: int
DroopInt_t.register_fields([('a', uint32_t, 0), ('b', uint32_t, 4), ('c', uint32_t, 8)])
PPCLK_e: dict[int, str] = {(PPCLK_GFXCLK:=0): 'PPCLK_GFXCLK', (PPCLK_VCLK:=1): 'PPCLK_VCLK', (PPCLK_DCLK:=2): 'PPCLK_DCLK', (PPCLK_ECLK:=3): 'PPCLK_ECLK', (PPCLK_SOCCLK:=4): 'PPCLK_SOCCLK', (PPCLK_UCLK:=5): 'PPCLK_UCLK', (PPCLK_DCEFCLK:=6): 'PPCLK_DCEFCLK', (PPCLK_DISPCLK:=7): 'PPCLK_DISPCLK', (PPCLK_PIXCLK:=8): 'PPCLK_PIXCLK', (PPCLK_PHYCLK:=9): 'PPCLK_PHYCLK', (PPCLK_FCLK:=10): 'PPCLK_FCLK', (PPCLK_COUNT:=11): 'PPCLK_COUNT'}
POWER_SOURCE_e: dict[int, str] = {(POWER_SOURCE_AC:=0): 'POWER_SOURCE_AC', (POWER_SOURCE_DC:=1): 'POWER_SOURCE_DC', (POWER_SOURCE_COUNT:=2): 'POWER_SOURCE_COUNT'}
VOLTAGE_MODE_e: dict[int, str] = {(VOLTAGE_MODE_AVFS:=0): 'VOLTAGE_MODE_AVFS', (VOLTAGE_MODE_AVFS_SS:=1): 'VOLTAGE_MODE_AVFS_SS', (VOLTAGE_MODE_SS:=2): 'VOLTAGE_MODE_SS', (VOLTAGE_MODE_COUNT:=3): 'VOLTAGE_MODE_COUNT'}
AVFS_VOLTAGE_TYPE_e: dict[int, str] = {(AVFS_VOLTAGE_GFX:=0): 'AVFS_VOLTAGE_GFX', (AVFS_VOLTAGE_SOC:=1): 'AVFS_VOLTAGE_SOC', (AVFS_VOLTAGE_COUNT:=2): 'AVFS_VOLTAGE_COUNT'}
@c.record
class DpmDescriptor_t(c.Struct):
  SIZE = 24
  VoltageMode: int
  SnapToDiscrete: int
  NumDiscreteLevels: int
  padding: int
  ConversionToAvfsClk: LinearInt_t
  SsCurve: QuadraticInt_t
uint8_t: TypeAlias = ctypes.c_ubyte
DpmDescriptor_t.register_fields([('VoltageMode', uint8_t, 0), ('SnapToDiscrete', uint8_t, 1), ('NumDiscreteLevels', uint8_t, 2), ('padding', uint8_t, 3), ('ConversionToAvfsClk', LinearInt_t, 4), ('SsCurve', QuadraticInt_t, 12)])
@c.record
class PPTable_t(c.Struct):
  SIZE = 1256
  Version: int
  FeaturesToRun: c.Array[ctypes.c_uint32, Literal[2]]
  SocketPowerLimitAc0: int
  SocketPowerLimitAc0Tau: int
  SocketPowerLimitAc1: int
  SocketPowerLimitAc1Tau: int
  SocketPowerLimitAc2: int
  SocketPowerLimitAc2Tau: int
  SocketPowerLimitAc3: int
  SocketPowerLimitAc3Tau: int
  SocketPowerLimitDc: int
  SocketPowerLimitDcTau: int
  TdcLimitSoc: int
  TdcLimitSocTau: int
  TdcLimitGfx: int
  TdcLimitGfxTau: int
  TedgeLimit: int
  ThotspotLimit: int
  ThbmLimit: int
  Tvr_gfxLimit: int
  Tvr_memLimit: int
  Tliquid1Limit: int
  Tliquid2Limit: int
  TplxLimit: int
  FitLimit: int
  PpmPowerLimit: int
  PpmTemperatureThreshold: int
  MemoryOnPackage: int
  padding8_limits: int
  Tvr_SocLimit: int
  UlvVoltageOffsetSoc: int
  UlvVoltageOffsetGfx: int
  UlvSmnclkDid: int
  UlvMp1clkDid: int
  UlvGfxclkBypass: int
  Padding234: int
  MinVoltageGfx: int
  MinVoltageSoc: int
  MaxVoltageGfx: int
  MaxVoltageSoc: int
  LoadLineResistanceGfx: int
  LoadLineResistanceSoc: int
  DpmDescriptor: c.Array[DpmDescriptor_t, Literal[11]]
  FreqTableGfx: c.Array[ctypes.c_uint16, Literal[16]]
  FreqTableVclk: c.Array[ctypes.c_uint16, Literal[8]]
  FreqTableDclk: c.Array[ctypes.c_uint16, Literal[8]]
  FreqTableEclk: c.Array[ctypes.c_uint16, Literal[8]]
  FreqTableSocclk: c.Array[ctypes.c_uint16, Literal[8]]
  FreqTableUclk: c.Array[ctypes.c_uint16, Literal[4]]
  FreqTableFclk: c.Array[ctypes.c_uint16, Literal[8]]
  FreqTableDcefclk: c.Array[ctypes.c_uint16, Literal[8]]
  FreqTableDispclk: c.Array[ctypes.c_uint16, Literal[8]]
  FreqTablePixclk: c.Array[ctypes.c_uint16, Literal[8]]
  FreqTablePhyclk: c.Array[ctypes.c_uint16, Literal[8]]
  DcModeMaxFreq: c.Array[ctypes.c_uint16, Literal[11]]
  Padding8_Clks: int
  Mp0clkFreq: c.Array[ctypes.c_uint16, Literal[2]]
  Mp0DpmVoltage: c.Array[ctypes.c_uint16, Literal[2]]
  GfxclkFidle: int
  GfxclkSlewRate: int
  CksEnableFreq: int
  Padding789: int
  CksVoltageOffset: QuadraticInt_t
  Padding567: c.Array[ctypes.c_ubyte, Literal[4]]
  GfxclkDsMaxFreq: int
  GfxclkSource: int
  Padding456: int
  LowestUclkReservedForUlv: int
  Padding8_Uclk: c.Array[ctypes.c_ubyte, Literal[3]]
  PcieGenSpeed: c.Array[ctypes.c_ubyte, Literal[2]]
  PcieLaneCount: c.Array[ctypes.c_ubyte, Literal[2]]
  LclkFreq: c.Array[ctypes.c_uint16, Literal[2]]
  EnableTdpm: int
  TdpmHighHystTemperature: int
  TdpmLowHystTemperature: int
  GfxclkFreqHighTempLimit: int
  FanStopTemp: int
  FanStartTemp: int
  FanGainEdge: int
  FanGainHotspot: int
  FanGainLiquid: int
  FanGainVrGfx: int
  FanGainVrSoc: int
  FanGainPlx: int
  FanGainHbm: int
  FanPwmMin: int
  FanAcousticLimitRpm: int
  FanThrottlingRpm: int
  FanMaximumRpm: int
  FanTargetTemperature: int
  FanTargetGfxclk: int
  FanZeroRpmEnable: int
  FanTachEdgePerRev: int
  FuzzyFan_ErrorSetDelta: int
  FuzzyFan_ErrorRateSetDelta: int
  FuzzyFan_PwmSetDelta: int
  FuzzyFan_Reserved: int
  OverrideAvfsGb: c.Array[ctypes.c_ubyte, Literal[2]]
  Padding8_Avfs: c.Array[ctypes.c_ubyte, Literal[2]]
  qAvfsGb: c.Array[QuadraticInt_t, Literal[2]]
  dBtcGbGfxCksOn: DroopInt_t
  dBtcGbGfxCksOff: DroopInt_t
  dBtcGbGfxAfll: DroopInt_t
  dBtcGbSoc: DroopInt_t
  qAgingGb: c.Array[LinearInt_t, Literal[2]]
  qStaticVoltageOffset: c.Array[QuadraticInt_t, Literal[2]]
  DcTol: c.Array[ctypes.c_uint16, Literal[2]]
  DcBtcEnabled: c.Array[ctypes.c_ubyte, Literal[2]]
  Padding8_GfxBtc: c.Array[ctypes.c_ubyte, Literal[2]]
  DcBtcMin: c.Array[ctypes.c_int16, Literal[2]]
  DcBtcMax: c.Array[ctypes.c_uint16, Literal[2]]
  XgmiLinkSpeed: c.Array[ctypes.c_ubyte, Literal[2]]
  XgmiLinkWidth: c.Array[ctypes.c_ubyte, Literal[2]]
  XgmiFclkFreq: c.Array[ctypes.c_uint16, Literal[2]]
  XgmiUclkFreq: c.Array[ctypes.c_uint16, Literal[2]]
  XgmiSocclkFreq: c.Array[ctypes.c_uint16, Literal[2]]
  XgmiSocVoltage: c.Array[ctypes.c_uint16, Literal[2]]
  DebugOverrides: int
  ReservedEquation0: QuadraticInt_t
  ReservedEquation1: QuadraticInt_t
  ReservedEquation2: QuadraticInt_t
  ReservedEquation3: QuadraticInt_t
  MinVoltageUlvGfx: int
  MinVoltageUlvSoc: int
  MGpuFanBoostLimitRpm: int
  padding16_Fan: int
  FanGainVrMem0: int
  FanGainVrMem1: int
  DcBtcGb: c.Array[ctypes.c_uint16, Literal[2]]
  Reserved: c.Array[ctypes.c_uint32, Literal[11]]
  Padding32: c.Array[ctypes.c_uint32, Literal[3]]
  MaxVoltageStepGfx: int
  MaxVoltageStepSoc: int
  VddGfxVrMapping: int
  VddSocVrMapping: int
  VddMem0VrMapping: int
  VddMem1VrMapping: int
  GfxUlvPhaseSheddingMask: int
  SocUlvPhaseSheddingMask: int
  ExternalSensorPresent: int
  Padding8_V: int
  GfxMaxCurrent: int
  GfxOffset: int
  Padding_TelemetryGfx: int
  SocMaxCurrent: int
  SocOffset: int
  Padding_TelemetrySoc: int
  Mem0MaxCurrent: int
  Mem0Offset: int
  Padding_TelemetryMem0: int
  Mem1MaxCurrent: int
  Mem1Offset: int
  Padding_TelemetryMem1: int
  AcDcGpio: int
  AcDcPolarity: int
  VR0HotGpio: int
  VR0HotPolarity: int
  VR1HotGpio: int
  VR1HotPolarity: int
  Padding1: int
  Padding2: int
  LedPin0: int
  LedPin1: int
  LedPin2: int
  padding8_4: int
  PllGfxclkSpreadEnabled: int
  PllGfxclkSpreadPercent: int
  PllGfxclkSpreadFreq: int
  UclkSpreadEnabled: int
  UclkSpreadPercent: int
  UclkSpreadFreq: int
  FclkSpreadEnabled: int
  FclkSpreadPercent: int
  FclkSpreadFreq: int
  FllGfxclkSpreadEnabled: int
  FllGfxclkSpreadPercent: int
  FllGfxclkSpreadFreq: int
  I2cControllers: c.Array[I2cControllerConfig_t, Literal[7]]
  BoardReserved: c.Array[ctypes.c_uint32, Literal[10]]
  MmHubPadding: c.Array[ctypes.c_uint32, Literal[8]]
uint16_t: TypeAlias = ctypes.c_uint16
int16_t: TypeAlias = ctypes.c_int16
int8_t: TypeAlias = ctypes.c_byte
PPTable_t.register_fields([('Version', uint32_t, 0), ('FeaturesToRun', c.Array[uint32_t, Literal[2]], 4), ('SocketPowerLimitAc0', uint16_t, 12), ('SocketPowerLimitAc0Tau', uint16_t, 14), ('SocketPowerLimitAc1', uint16_t, 16), ('SocketPowerLimitAc1Tau', uint16_t, 18), ('SocketPowerLimitAc2', uint16_t, 20), ('SocketPowerLimitAc2Tau', uint16_t, 22), ('SocketPowerLimitAc3', uint16_t, 24), ('SocketPowerLimitAc3Tau', uint16_t, 26), ('SocketPowerLimitDc', uint16_t, 28), ('SocketPowerLimitDcTau', uint16_t, 30), ('TdcLimitSoc', uint16_t, 32), ('TdcLimitSocTau', uint16_t, 34), ('TdcLimitGfx', uint16_t, 36), ('TdcLimitGfxTau', uint16_t, 38), ('TedgeLimit', uint16_t, 40), ('ThotspotLimit', uint16_t, 42), ('ThbmLimit', uint16_t, 44), ('Tvr_gfxLimit', uint16_t, 46), ('Tvr_memLimit', uint16_t, 48), ('Tliquid1Limit', uint16_t, 50), ('Tliquid2Limit', uint16_t, 52), ('TplxLimit', uint16_t, 54), ('FitLimit', uint32_t, 56), ('PpmPowerLimit', uint16_t, 60), ('PpmTemperatureThreshold', uint16_t, 62), ('MemoryOnPackage', uint8_t, 64), ('padding8_limits', uint8_t, 65), ('Tvr_SocLimit', uint16_t, 66), ('UlvVoltageOffsetSoc', uint16_t, 68), ('UlvVoltageOffsetGfx', uint16_t, 70), ('UlvSmnclkDid', uint8_t, 72), ('UlvMp1clkDid', uint8_t, 73), ('UlvGfxclkBypass', uint8_t, 74), ('Padding234', uint8_t, 75), ('MinVoltageGfx', uint16_t, 76), ('MinVoltageSoc', uint16_t, 78), ('MaxVoltageGfx', uint16_t, 80), ('MaxVoltageSoc', uint16_t, 82), ('LoadLineResistanceGfx', uint16_t, 84), ('LoadLineResistanceSoc', uint16_t, 86), ('DpmDescriptor', c.Array[DpmDescriptor_t, Literal[11]], 88), ('FreqTableGfx', c.Array[uint16_t, Literal[16]], 352), ('FreqTableVclk', c.Array[uint16_t, Literal[8]], 384), ('FreqTableDclk', c.Array[uint16_t, Literal[8]], 400), ('FreqTableEclk', c.Array[uint16_t, Literal[8]], 416), ('FreqTableSocclk', c.Array[uint16_t, Literal[8]], 432), ('FreqTableUclk', c.Array[uint16_t, Literal[4]], 448), ('FreqTableFclk', c.Array[uint16_t, Literal[8]], 456), ('FreqTableDcefclk', c.Array[uint16_t, Literal[8]], 472), ('FreqTableDispclk', c.Array[uint16_t, Literal[8]], 488), ('FreqTablePixclk', c.Array[uint16_t, Literal[8]], 504), ('FreqTablePhyclk', c.Array[uint16_t, Literal[8]], 520), ('DcModeMaxFreq', c.Array[uint16_t, Literal[11]], 536), ('Padding8_Clks', uint16_t, 558), ('Mp0clkFreq', c.Array[uint16_t, Literal[2]], 560), ('Mp0DpmVoltage', c.Array[uint16_t, Literal[2]], 564), ('GfxclkFidle', uint16_t, 568), ('GfxclkSlewRate', uint16_t, 570), ('CksEnableFreq', uint16_t, 572), ('Padding789', uint16_t, 574), ('CksVoltageOffset', QuadraticInt_t, 576), ('Padding567', c.Array[uint8_t, Literal[4]], 588), ('GfxclkDsMaxFreq', uint16_t, 592), ('GfxclkSource', uint8_t, 594), ('Padding456', uint8_t, 595), ('LowestUclkReservedForUlv', uint8_t, 596), ('Padding8_Uclk', c.Array[uint8_t, Literal[3]], 597), ('PcieGenSpeed', c.Array[uint8_t, Literal[2]], 600), ('PcieLaneCount', c.Array[uint8_t, Literal[2]], 602), ('LclkFreq', c.Array[uint16_t, Literal[2]], 604), ('EnableTdpm', uint16_t, 608), ('TdpmHighHystTemperature', uint16_t, 610), ('TdpmLowHystTemperature', uint16_t, 612), ('GfxclkFreqHighTempLimit', uint16_t, 614), ('FanStopTemp', uint16_t, 616), ('FanStartTemp', uint16_t, 618), ('FanGainEdge', uint16_t, 620), ('FanGainHotspot', uint16_t, 622), ('FanGainLiquid', uint16_t, 624), ('FanGainVrGfx', uint16_t, 626), ('FanGainVrSoc', uint16_t, 628), ('FanGainPlx', uint16_t, 630), ('FanGainHbm', uint16_t, 632), ('FanPwmMin', uint16_t, 634), ('FanAcousticLimitRpm', uint16_t, 636), ('FanThrottlingRpm', uint16_t, 638), ('FanMaximumRpm', uint16_t, 640), ('FanTargetTemperature', uint16_t, 642), ('FanTargetGfxclk', uint16_t, 644), ('FanZeroRpmEnable', uint8_t, 646), ('FanTachEdgePerRev', uint8_t, 647), ('FuzzyFan_ErrorSetDelta', int16_t, 648), ('FuzzyFan_ErrorRateSetDelta', int16_t, 650), ('FuzzyFan_PwmSetDelta', int16_t, 652), ('FuzzyFan_Reserved', uint16_t, 654), ('OverrideAvfsGb', c.Array[uint8_t, Literal[2]], 656), ('Padding8_Avfs', c.Array[uint8_t, Literal[2]], 658), ('qAvfsGb', c.Array[QuadraticInt_t, Literal[2]], 660), ('dBtcGbGfxCksOn', DroopInt_t, 684), ('dBtcGbGfxCksOff', DroopInt_t, 696), ('dBtcGbGfxAfll', DroopInt_t, 708), ('dBtcGbSoc', DroopInt_t, 720), ('qAgingGb', c.Array[LinearInt_t, Literal[2]], 732), ('qStaticVoltageOffset', c.Array[QuadraticInt_t, Literal[2]], 748), ('DcTol', c.Array[uint16_t, Literal[2]], 772), ('DcBtcEnabled', c.Array[uint8_t, Literal[2]], 776), ('Padding8_GfxBtc', c.Array[uint8_t, Literal[2]], 778), ('DcBtcMin', c.Array[int16_t, Literal[2]], 780), ('DcBtcMax', c.Array[uint16_t, Literal[2]], 784), ('XgmiLinkSpeed', c.Array[uint8_t, Literal[2]], 788), ('XgmiLinkWidth', c.Array[uint8_t, Literal[2]], 790), ('XgmiFclkFreq', c.Array[uint16_t, Literal[2]], 792), ('XgmiUclkFreq', c.Array[uint16_t, Literal[2]], 796), ('XgmiSocclkFreq', c.Array[uint16_t, Literal[2]], 800), ('XgmiSocVoltage', c.Array[uint16_t, Literal[2]], 804), ('DebugOverrides', uint32_t, 808), ('ReservedEquation0', QuadraticInt_t, 812), ('ReservedEquation1', QuadraticInt_t, 824), ('ReservedEquation2', QuadraticInt_t, 836), ('ReservedEquation3', QuadraticInt_t, 848), ('MinVoltageUlvGfx', uint16_t, 860), ('MinVoltageUlvSoc', uint16_t, 862), ('MGpuFanBoostLimitRpm', uint16_t, 864), ('padding16_Fan', uint16_t, 866), ('FanGainVrMem0', uint16_t, 868), ('FanGainVrMem1', uint16_t, 870), ('DcBtcGb', c.Array[uint16_t, Literal[2]], 872), ('Reserved', c.Array[uint32_t, Literal[11]], 876), ('Padding32', c.Array[uint32_t, Literal[3]], 920), ('MaxVoltageStepGfx', uint16_t, 932), ('MaxVoltageStepSoc', uint16_t, 934), ('VddGfxVrMapping', uint8_t, 936), ('VddSocVrMapping', uint8_t, 937), ('VddMem0VrMapping', uint8_t, 938), ('VddMem1VrMapping', uint8_t, 939), ('GfxUlvPhaseSheddingMask', uint8_t, 940), ('SocUlvPhaseSheddingMask', uint8_t, 941), ('ExternalSensorPresent', uint8_t, 942), ('Padding8_V', uint8_t, 943), ('GfxMaxCurrent', uint16_t, 944), ('GfxOffset', int8_t, 946), ('Padding_TelemetryGfx', uint8_t, 947), ('SocMaxCurrent', uint16_t, 948), ('SocOffset', int8_t, 950), ('Padding_TelemetrySoc', uint8_t, 951), ('Mem0MaxCurrent', uint16_t, 952), ('Mem0Offset', int8_t, 954), ('Padding_TelemetryMem0', uint8_t, 955), ('Mem1MaxCurrent', uint16_t, 956), ('Mem1Offset', int8_t, 958), ('Padding_TelemetryMem1', uint8_t, 959), ('AcDcGpio', uint8_t, 960), ('AcDcPolarity', uint8_t, 961), ('VR0HotGpio', uint8_t, 962), ('VR0HotPolarity', uint8_t, 963), ('VR1HotGpio', uint8_t, 964), ('VR1HotPolarity', uint8_t, 965), ('Padding1', uint8_t, 966), ('Padding2', uint8_t, 967), ('LedPin0', uint8_t, 968), ('LedPin1', uint8_t, 969), ('LedPin2', uint8_t, 970), ('padding8_4', uint8_t, 971), ('PllGfxclkSpreadEnabled', uint8_t, 972), ('PllGfxclkSpreadPercent', uint8_t, 973), ('PllGfxclkSpreadFreq', uint16_t, 974), ('UclkSpreadEnabled', uint8_t, 976), ('UclkSpreadPercent', uint8_t, 977), ('UclkSpreadFreq', uint16_t, 978), ('FclkSpreadEnabled', uint8_t, 980), ('FclkSpreadPercent', uint8_t, 981), ('FclkSpreadFreq', uint16_t, 982), ('FllGfxclkSpreadEnabled', uint8_t, 984), ('FllGfxclkSpreadPercent', uint8_t, 985), ('FllGfxclkSpreadFreq', uint16_t, 986), ('I2cControllers', c.Array[I2cControllerConfig_t, Literal[7]], 988), ('BoardReserved', c.Array[uint32_t, Literal[10]], 1184), ('MmHubPadding', c.Array[uint32_t, Literal[8]], 1224)])
@c.record
class DriverSmuConfig_t(c.Struct):
  SIZE = 44
  GfxclkAverageLpfTau: int
  SocclkAverageLpfTau: int
  UclkAverageLpfTau: int
  GfxActivityLpfTau: int
  UclkActivityLpfTau: int
  SocketPowerLpfTau: int
  MmHubPadding: c.Array[ctypes.c_uint32, Literal[8]]
DriverSmuConfig_t.register_fields([('GfxclkAverageLpfTau', uint16_t, 0), ('SocclkAverageLpfTau', uint16_t, 2), ('UclkAverageLpfTau', uint16_t, 4), ('GfxActivityLpfTau', uint16_t, 6), ('UclkActivityLpfTau', uint16_t, 8), ('SocketPowerLpfTau', uint16_t, 10), ('MmHubPadding', c.Array[uint32_t, Literal[8]], 12)])
@c.record
class OverDriveTable_t(c.Struct):
  SIZE = 32
  GfxclkFmin: int
  GfxclkFmax: int
  GfxclkFreq1: int
  GfxclkVolt1: int
  GfxclkFreq2: int
  GfxclkVolt2: int
  GfxclkFreq3: int
  GfxclkVolt3: int
  UclkFmax: int
  OverDrivePct: int
  FanMaximumRpm: int
  FanMinimumPwm: int
  FanTargetTemperature: int
  MaxOpTemp: int
  FanZeroRpmEnable: int
  Padding: int
OverDriveTable_t.register_fields([('GfxclkFmin', uint16_t, 0), ('GfxclkFmax', uint16_t, 2), ('GfxclkFreq1', uint16_t, 4), ('GfxclkVolt1', uint16_t, 6), ('GfxclkFreq2', uint16_t, 8), ('GfxclkVolt2', uint16_t, 10), ('GfxclkFreq3', uint16_t, 12), ('GfxclkVolt3', uint16_t, 14), ('UclkFmax', uint16_t, 16), ('OverDrivePct', int16_t, 18), ('FanMaximumRpm', uint16_t, 20), ('FanMinimumPwm', uint16_t, 22), ('FanTargetTemperature', uint16_t, 24), ('MaxOpTemp', uint16_t, 26), ('FanZeroRpmEnable', uint16_t, 28), ('Padding', uint16_t, 30)])
@c.record
class SmuMetrics_t(c.Struct):
  SIZE = 96
  CurrClock: c.Array[ctypes.c_uint16, Literal[11]]
  AverageGfxclkFrequency: int
  AverageSocclkFrequency: int
  AverageUclkFrequency: int
  AverageGfxActivity: int
  AverageUclkActivity: int
  CurrSocVoltageOffset: int
  CurrGfxVoltageOffset: int
  CurrMemVidOffset: int
  Padding8: int
  CurrSocketPower: int
  TemperatureEdge: int
  TemperatureHotspot: int
  TemperatureHBM: int
  TemperatureVrGfx: int
  TemperatureVrSoc: int
  TemperatureVrMem0: int
  TemperatureVrMem1: int
  TemperatureLiquid: int
  TemperaturePlx: int
  ThrottlerStatus: int
  LinkDpmLevel: int
  AverageSocketPower: int
  Padding: int
  MmHubPadding: c.Array[ctypes.c_uint32, Literal[7]]
SmuMetrics_t.register_fields([('CurrClock', c.Array[uint16_t, Literal[11]], 0), ('AverageGfxclkFrequency', uint16_t, 22), ('AverageSocclkFrequency', uint16_t, 24), ('AverageUclkFrequency', uint16_t, 26), ('AverageGfxActivity', uint16_t, 28), ('AverageUclkActivity', uint16_t, 30), ('CurrSocVoltageOffset', uint8_t, 32), ('CurrGfxVoltageOffset', uint8_t, 33), ('CurrMemVidOffset', uint8_t, 34), ('Padding8', uint8_t, 35), ('CurrSocketPower', uint16_t, 36), ('TemperatureEdge', uint16_t, 38), ('TemperatureHotspot', uint16_t, 40), ('TemperatureHBM', uint16_t, 42), ('TemperatureVrGfx', uint16_t, 44), ('TemperatureVrSoc', uint16_t, 46), ('TemperatureVrMem0', uint16_t, 48), ('TemperatureVrMem1', uint16_t, 50), ('TemperatureLiquid', uint16_t, 52), ('TemperaturePlx', uint16_t, 54), ('ThrottlerStatus', uint32_t, 56), ('LinkDpmLevel', uint8_t, 60), ('AverageSocketPower', uint16_t, 62), ('Padding', uint8_t, 64), ('MmHubPadding', c.Array[uint32_t, Literal[7]], 68)])
@c.record
class WatermarkRowGeneric_t(c.Struct):
  SIZE = 12
  MinClock: int
  MaxClock: int
  MinUclk: int
  MaxUclk: int
  WmSetting: int
  Padding: c.Array[ctypes.c_ubyte, Literal[3]]
WatermarkRowGeneric_t.register_fields([('MinClock', uint16_t, 0), ('MaxClock', uint16_t, 2), ('MinUclk', uint16_t, 4), ('MaxUclk', uint16_t, 6), ('WmSetting', uint8_t, 8), ('Padding', c.Array[uint8_t, Literal[3]], 9)])
WM_CLOCK_e: dict[int, str] = {(WM_SOCCLK:=0): 'WM_SOCCLK', (WM_DCEFCLK:=1): 'WM_DCEFCLK', (WM_COUNT_PP:=2): 'WM_COUNT_PP'}
@c.record
class Watermarks_t(c.Struct):
  SIZE = 124
  WatermarkRow: c.Array[c.Array[WatermarkRowGeneric_t, Literal[4]], Literal[2]]
  MmHubPadding: c.Array[ctypes.c_uint32, Literal[7]]
Watermarks_t.register_fields([('WatermarkRow', c.Array[c.Array[WatermarkRowGeneric_t, Literal[4]], Literal[2]], 0), ('MmHubPadding', c.Array[uint32_t, Literal[7]], 96)])
@c.record
class AvfsDebugTable_t(c.Struct):
  SIZE = 576
  avgPsmCount: c.Array[ctypes.c_uint16, Literal[45]]
  minPsmCount: c.Array[ctypes.c_uint16, Literal[45]]
  avgPsmVoltage: c.Array[ctypes.c_float, Literal[45]]
  minPsmVoltage: c.Array[ctypes.c_float, Literal[45]]
  avgScsPsmCount: int
  minScsPsmCount: int
  avgScsPsmVoltage: float
  minScsPsmVoltage: float
  MmHubPadding: c.Array[ctypes.c_uint32, Literal[6]]
AvfsDebugTable_t.register_fields([('avgPsmCount', c.Array[uint16_t, Literal[45]], 0), ('minPsmCount', c.Array[uint16_t, Literal[45]], 90), ('avgPsmVoltage', c.Array[ctypes.c_float, Literal[45]], 180), ('minPsmVoltage', c.Array[ctypes.c_float, Literal[45]], 360), ('avgScsPsmCount', uint16_t, 540), ('minScsPsmCount', uint16_t, 542), ('avgScsPsmVoltage', ctypes.c_float, 544), ('minScsPsmVoltage', ctypes.c_float, 548), ('MmHubPadding', c.Array[uint32_t, Literal[6]], 552)])
@c.record
class AvfsFuseOverride_t(c.Struct):
  SIZE = 232
  AvfsVersion: int
  AvfsEn: c.Array[ctypes.c_ubyte, Literal[2]]
  OverrideVFT: c.Array[ctypes.c_ubyte, Literal[2]]
  OverrideAvfsGb: c.Array[ctypes.c_ubyte, Literal[2]]
  OverrideTemperatures: c.Array[ctypes.c_ubyte, Literal[2]]
  OverrideVInversion: c.Array[ctypes.c_ubyte, Literal[2]]
  OverrideP2V: c.Array[ctypes.c_ubyte, Literal[2]]
  OverrideP2VCharzFreq: c.Array[ctypes.c_ubyte, Literal[2]]
  VFT0_m1: c.Array[ctypes.c_int32, Literal[2]]
  VFT0_m2: c.Array[ctypes.c_int32, Literal[2]]
  VFT0_b: c.Array[ctypes.c_int32, Literal[2]]
  VFT1_m1: c.Array[ctypes.c_int32, Literal[2]]
  VFT1_m2: c.Array[ctypes.c_int32, Literal[2]]
  VFT1_b: c.Array[ctypes.c_int32, Literal[2]]
  VFT2_m1: c.Array[ctypes.c_int32, Literal[2]]
  VFT2_m2: c.Array[ctypes.c_int32, Literal[2]]
  VFT2_b: c.Array[ctypes.c_int32, Literal[2]]
  AvfsGb0_m1: c.Array[ctypes.c_int32, Literal[2]]
  AvfsGb0_m2: c.Array[ctypes.c_int32, Literal[2]]
  AvfsGb0_b: c.Array[ctypes.c_int32, Literal[2]]
  AcBtcGb_m1: c.Array[ctypes.c_int32, Literal[2]]
  AcBtcGb_m2: c.Array[ctypes.c_int32, Literal[2]]
  AcBtcGb_b: c.Array[ctypes.c_int32, Literal[2]]
  AvfsTempCold: c.Array[ctypes.c_uint32, Literal[2]]
  AvfsTempMid: c.Array[ctypes.c_uint32, Literal[2]]
  AvfsTempHot: c.Array[ctypes.c_uint32, Literal[2]]
  VInversion: c.Array[ctypes.c_uint32, Literal[2]]
  P2V_m1: c.Array[ctypes.c_int32, Literal[2]]
  P2V_m2: c.Array[ctypes.c_int32, Literal[2]]
  P2V_b: c.Array[ctypes.c_int32, Literal[2]]
  P2VCharzFreq: c.Array[ctypes.c_uint32, Literal[2]]
  EnabledAvfsModules: int
  MmHubPadding: c.Array[ctypes.c_uint32, Literal[7]]
int32_t: TypeAlias = ctypes.c_int32
AvfsFuseOverride_t.register_fields([('AvfsVersion', uint8_t, 0), ('AvfsEn', c.Array[uint8_t, Literal[2]], 1), ('OverrideVFT', c.Array[uint8_t, Literal[2]], 3), ('OverrideAvfsGb', c.Array[uint8_t, Literal[2]], 5), ('OverrideTemperatures', c.Array[uint8_t, Literal[2]], 7), ('OverrideVInversion', c.Array[uint8_t, Literal[2]], 9), ('OverrideP2V', c.Array[uint8_t, Literal[2]], 11), ('OverrideP2VCharzFreq', c.Array[uint8_t, Literal[2]], 13), ('VFT0_m1', c.Array[int32_t, Literal[2]], 16), ('VFT0_m2', c.Array[int32_t, Literal[2]], 24), ('VFT0_b', c.Array[int32_t, Literal[2]], 32), ('VFT1_m1', c.Array[int32_t, Literal[2]], 40), ('VFT1_m2', c.Array[int32_t, Literal[2]], 48), ('VFT1_b', c.Array[int32_t, Literal[2]], 56), ('VFT2_m1', c.Array[int32_t, Literal[2]], 64), ('VFT2_m2', c.Array[int32_t, Literal[2]], 72), ('VFT2_b', c.Array[int32_t, Literal[2]], 80), ('AvfsGb0_m1', c.Array[int32_t, Literal[2]], 88), ('AvfsGb0_m2', c.Array[int32_t, Literal[2]], 96), ('AvfsGb0_b', c.Array[int32_t, Literal[2]], 104), ('AcBtcGb_m1', c.Array[int32_t, Literal[2]], 112), ('AcBtcGb_m2', c.Array[int32_t, Literal[2]], 120), ('AcBtcGb_b', c.Array[int32_t, Literal[2]], 128), ('AvfsTempCold', c.Array[uint32_t, Literal[2]], 136), ('AvfsTempMid', c.Array[uint32_t, Literal[2]], 144), ('AvfsTempHot', c.Array[uint32_t, Literal[2]], 152), ('VInversion', c.Array[uint32_t, Literal[2]], 160), ('P2V_m1', c.Array[int32_t, Literal[2]], 168), ('P2V_m2', c.Array[int32_t, Literal[2]], 176), ('P2V_b', c.Array[int32_t, Literal[2]], 184), ('P2VCharzFreq', c.Array[uint32_t, Literal[2]], 192), ('EnabledAvfsModules', uint32_t, 200), ('MmHubPadding', c.Array[uint32_t, Literal[7]], 204)])
@c.record
class DpmActivityMonitorCoeffInt_t(c.Struct):
  SIZE = 128
  Gfx_ActiveHystLimit: int
  Gfx_IdleHystLimit: int
  Gfx_FPS: int
  Gfx_MinActiveFreqType: int
  Gfx_BoosterFreqType: int
  Gfx_UseRlcBusy: int
  Gfx_MinActiveFreq: int
  Gfx_BoosterFreq: int
  Gfx_PD_Data_time_constant: int
  Gfx_PD_Data_limit_a: int
  Gfx_PD_Data_limit_b: int
  Gfx_PD_Data_limit_c: int
  Gfx_PD_Data_error_coeff: int
  Gfx_PD_Data_error_rate_coeff: int
  Soc_ActiveHystLimit: int
  Soc_IdleHystLimit: int
  Soc_FPS: int
  Soc_MinActiveFreqType: int
  Soc_BoosterFreqType: int
  Soc_UseRlcBusy: int
  Soc_MinActiveFreq: int
  Soc_BoosterFreq: int
  Soc_PD_Data_time_constant: int
  Soc_PD_Data_limit_a: int
  Soc_PD_Data_limit_b: int
  Soc_PD_Data_limit_c: int
  Soc_PD_Data_error_coeff: int
  Soc_PD_Data_error_rate_coeff: int
  Mem_ActiveHystLimit: int
  Mem_IdleHystLimit: int
  Mem_FPS: int
  Mem_MinActiveFreqType: int
  Mem_BoosterFreqType: int
  Mem_UseRlcBusy: int
  Mem_MinActiveFreq: int
  Mem_BoosterFreq: int
  Mem_PD_Data_time_constant: int
  Mem_PD_Data_limit_a: int
  Mem_PD_Data_limit_b: int
  Mem_PD_Data_limit_c: int
  Mem_PD_Data_error_coeff: int
  Mem_PD_Data_error_rate_coeff: int
  Fclk_ActiveHystLimit: int
  Fclk_IdleHystLimit: int
  Fclk_FPS: int
  Fclk_MinActiveFreqType: int
  Fclk_BoosterFreqType: int
  Fclk_UseRlcBusy: int
  Fclk_MinActiveFreq: int
  Fclk_BoosterFreq: int
  Fclk_PD_Data_time_constant: int
  Fclk_PD_Data_limit_a: int
  Fclk_PD_Data_limit_b: int
  Fclk_PD_Data_limit_c: int
  Fclk_PD_Data_error_coeff: int
  Fclk_PD_Data_error_rate_coeff: int
DpmActivityMonitorCoeffInt_t.register_fields([('Gfx_ActiveHystLimit', uint8_t, 0), ('Gfx_IdleHystLimit', uint8_t, 1), ('Gfx_FPS', uint8_t, 2), ('Gfx_MinActiveFreqType', uint8_t, 3), ('Gfx_BoosterFreqType', uint8_t, 4), ('Gfx_UseRlcBusy', uint8_t, 5), ('Gfx_MinActiveFreq', uint16_t, 6), ('Gfx_BoosterFreq', uint16_t, 8), ('Gfx_PD_Data_time_constant', uint16_t, 10), ('Gfx_PD_Data_limit_a', uint32_t, 12), ('Gfx_PD_Data_limit_b', uint32_t, 16), ('Gfx_PD_Data_limit_c', uint32_t, 20), ('Gfx_PD_Data_error_coeff', uint32_t, 24), ('Gfx_PD_Data_error_rate_coeff', uint32_t, 28), ('Soc_ActiveHystLimit', uint8_t, 32), ('Soc_IdleHystLimit', uint8_t, 33), ('Soc_FPS', uint8_t, 34), ('Soc_MinActiveFreqType', uint8_t, 35), ('Soc_BoosterFreqType', uint8_t, 36), ('Soc_UseRlcBusy', uint8_t, 37), ('Soc_MinActiveFreq', uint16_t, 38), ('Soc_BoosterFreq', uint16_t, 40), ('Soc_PD_Data_time_constant', uint16_t, 42), ('Soc_PD_Data_limit_a', uint32_t, 44), ('Soc_PD_Data_limit_b', uint32_t, 48), ('Soc_PD_Data_limit_c', uint32_t, 52), ('Soc_PD_Data_error_coeff', uint32_t, 56), ('Soc_PD_Data_error_rate_coeff', uint32_t, 60), ('Mem_ActiveHystLimit', uint8_t, 64), ('Mem_IdleHystLimit', uint8_t, 65), ('Mem_FPS', uint8_t, 66), ('Mem_MinActiveFreqType', uint8_t, 67), ('Mem_BoosterFreqType', uint8_t, 68), ('Mem_UseRlcBusy', uint8_t, 69), ('Mem_MinActiveFreq', uint16_t, 70), ('Mem_BoosterFreq', uint16_t, 72), ('Mem_PD_Data_time_constant', uint16_t, 74), ('Mem_PD_Data_limit_a', uint32_t, 76), ('Mem_PD_Data_limit_b', uint32_t, 80), ('Mem_PD_Data_limit_c', uint32_t, 84), ('Mem_PD_Data_error_coeff', uint32_t, 88), ('Mem_PD_Data_error_rate_coeff', uint32_t, 92), ('Fclk_ActiveHystLimit', uint8_t, 96), ('Fclk_IdleHystLimit', uint8_t, 97), ('Fclk_FPS', uint8_t, 98), ('Fclk_MinActiveFreqType', uint8_t, 99), ('Fclk_BoosterFreqType', uint8_t, 100), ('Fclk_UseRlcBusy', uint8_t, 101), ('Fclk_MinActiveFreq', uint16_t, 102), ('Fclk_BoosterFreq', uint16_t, 104), ('Fclk_PD_Data_time_constant', uint16_t, 106), ('Fclk_PD_Data_limit_a', uint32_t, 108), ('Fclk_PD_Data_limit_b', uint32_t, 112), ('Fclk_PD_Data_limit_c', uint32_t, 116), ('Fclk_PD_Data_error_coeff', uint32_t, 120), ('Fclk_PD_Data_error_rate_coeff', uint32_t, 124)])
@c.record
class struct_smu_hw_power_state(c.Struct):
  SIZE = 4
  magic: int
struct_smu_hw_power_state.register_fields([('magic', ctypes.c_uint32, 0)])
class struct_smu_power_state(c.Struct): pass
enum_smu_state_ui_label: dict[int, str] = {(SMU_STATE_UI_LABEL_NONE:=0): 'SMU_STATE_UI_LABEL_NONE', (SMU_STATE_UI_LABEL_BATTERY:=1): 'SMU_STATE_UI_LABEL_BATTERY', (SMU_STATE_UI_TABEL_MIDDLE_LOW:=2): 'SMU_STATE_UI_TABEL_MIDDLE_LOW', (SMU_STATE_UI_LABEL_BALLANCED:=3): 'SMU_STATE_UI_LABEL_BALLANCED', (SMU_STATE_UI_LABEL_MIDDLE_HIGHT:=4): 'SMU_STATE_UI_LABEL_MIDDLE_HIGHT', (SMU_STATE_UI_LABEL_PERFORMANCE:=5): 'SMU_STATE_UI_LABEL_PERFORMANCE', (SMU_STATE_UI_LABEL_BACO:=6): 'SMU_STATE_UI_LABEL_BACO'}
enum_smu_state_classification_flag: dict[int, str] = {(SMU_STATE_CLASSIFICATION_FLAG_BOOT:=1): 'SMU_STATE_CLASSIFICATION_FLAG_BOOT', (SMU_STATE_CLASSIFICATION_FLAG_THERMAL:=2): 'SMU_STATE_CLASSIFICATION_FLAG_THERMAL', (SMU_STATE_CLASSIFICATIN_FLAG_LIMITED_POWER_SOURCE:=4): 'SMU_STATE_CLASSIFICATIN_FLAG_LIMITED_POWER_SOURCE', (SMU_STATE_CLASSIFICATION_FLAG_RESET:=8): 'SMU_STATE_CLASSIFICATION_FLAG_RESET', (SMU_STATE_CLASSIFICATION_FLAG_FORCED:=16): 'SMU_STATE_CLASSIFICATION_FLAG_FORCED', (SMU_STATE_CLASSIFICATION_FLAG_USER_3D_PERFORMANCE:=32): 'SMU_STATE_CLASSIFICATION_FLAG_USER_3D_PERFORMANCE', (SMU_STATE_CLASSIFICATION_FLAG_USER_2D_PERFORMANCE:=64): 'SMU_STATE_CLASSIFICATION_FLAG_USER_2D_PERFORMANCE', (SMU_STATE_CLASSIFICATION_FLAG_3D_PERFORMANCE:=128): 'SMU_STATE_CLASSIFICATION_FLAG_3D_PERFORMANCE', (SMU_STATE_CLASSIFICATION_FLAG_AC_OVERDIRVER_TEMPLATE:=256): 'SMU_STATE_CLASSIFICATION_FLAG_AC_OVERDIRVER_TEMPLATE', (SMU_STATE_CLASSIFICATION_FLAG_UVD:=512): 'SMU_STATE_CLASSIFICATION_FLAG_UVD', (SMU_STATE_CLASSIFICATION_FLAG_3D_PERFORMANCE_LOW:=1024): 'SMU_STATE_CLASSIFICATION_FLAG_3D_PERFORMANCE_LOW', (SMU_STATE_CLASSIFICATION_FLAG_ACPI:=2048): 'SMU_STATE_CLASSIFICATION_FLAG_ACPI', (SMU_STATE_CLASSIFICATION_FLAG_HD2:=4096): 'SMU_STATE_CLASSIFICATION_FLAG_HD2', (SMU_STATE_CLASSIFICATION_FLAG_UVD_HD:=8192): 'SMU_STATE_CLASSIFICATION_FLAG_UVD_HD', (SMU_STATE_CLASSIFICATION_FLAG_UVD_SD:=16384): 'SMU_STATE_CLASSIFICATION_FLAG_UVD_SD', (SMU_STATE_CLASSIFICATION_FLAG_USER_DC_PERFORMANCE:=32768): 'SMU_STATE_CLASSIFICATION_FLAG_USER_DC_PERFORMANCE', (SMU_STATE_CLASSIFICATION_FLAG_DC_OVERDIRVER_TEMPLATE:=65536): 'SMU_STATE_CLASSIFICATION_FLAG_DC_OVERDIRVER_TEMPLATE', (SMU_STATE_CLASSIFICATION_FLAG_BACO:=131072): 'SMU_STATE_CLASSIFICATION_FLAG_BACO', (SMU_STATE_CLASSIFICATIN_FLAG_LIMITED_POWER_SOURCE2:=262144): 'SMU_STATE_CLASSIFICATIN_FLAG_LIMITED_POWER_SOURCE2', (SMU_STATE_CLASSIFICATION_FLAG_ULV:=524288): 'SMU_STATE_CLASSIFICATION_FLAG_ULV', (SMU_STATE_CLASSIFICATION_FLAG_UVD_MVC:=1048576): 'SMU_STATE_CLASSIFICATION_FLAG_UVD_MVC'}
@c.record
class struct_smu_state_classification_block(c.Struct):
  SIZE = 16
  ui_label: int
  flags: int
  bios_index: int
  temporary_state: bool
  to_be_deleted: bool
struct_smu_state_classification_block.register_fields([('ui_label', ctypes.c_uint32, 0), ('flags', ctypes.c_uint32, 4), ('bios_index', ctypes.c_int32, 8), ('temporary_state', ctypes.c_bool, 12), ('to_be_deleted', ctypes.c_bool, 13)])
@c.record
class struct_smu_state_pcie_block(c.Struct):
  SIZE = 4
  lanes: int
struct_smu_state_pcie_block.register_fields([('lanes', ctypes.c_uint32, 0)])
enum_smu_refreshrate_source: dict[int, str] = {(SMU_REFRESHRATE_SOURCE_EDID:=0): 'SMU_REFRESHRATE_SOURCE_EDID', (SMU_REFRESHRATE_SOURCE_EXPLICIT:=1): 'SMU_REFRESHRATE_SOURCE_EXPLICIT'}
@c.record
class struct_smu_state_display_block(c.Struct):
  SIZE = 20
  disable_frame_modulation: bool
  limit_refreshrate: bool
  refreshrate_source: int
  explicit_refreshrate: int
  edid_refreshrate_index: int
  enable_vari_bright: bool
struct_smu_state_display_block.register_fields([('disable_frame_modulation', ctypes.c_bool, 0), ('limit_refreshrate', ctypes.c_bool, 1), ('refreshrate_source', ctypes.c_uint32, 4), ('explicit_refreshrate', ctypes.c_int32, 8), ('edid_refreshrate_index', ctypes.c_int32, 12), ('enable_vari_bright', ctypes.c_bool, 16)])
@c.record
class struct_smu_state_memory_block(c.Struct):
  SIZE = 5
  dll_off: bool
  m3arb: int
  unused: c.Array[ctypes.c_ubyte, Literal[3]]
struct_smu_state_memory_block.register_fields([('dll_off', ctypes.c_bool, 0), ('m3arb', uint8_t, 1), ('unused', c.Array[uint8_t, Literal[3]], 2)])
@c.record
class struct_smu_state_software_algorithm_block(c.Struct):
  SIZE = 2
  disable_load_balancing: bool
  enable_sleep_for_timestamps: bool
struct_smu_state_software_algorithm_block.register_fields([('disable_load_balancing', ctypes.c_bool, 0), ('enable_sleep_for_timestamps', ctypes.c_bool, 1)])
@c.record
class struct_smu_temperature_range(c.Struct):
  SIZE = 44
  min: int
  max: int
  edge_emergency_max: int
  hotspot_min: int
  hotspot_crit_max: int
  hotspot_emergency_max: int
  mem_min: int
  mem_crit_max: int
  mem_emergency_max: int
  software_shutdown_temp: int
  software_shutdown_temp_offset: int
struct_smu_temperature_range.register_fields([('min', ctypes.c_int32, 0), ('max', ctypes.c_int32, 4), ('edge_emergency_max', ctypes.c_int32, 8), ('hotspot_min', ctypes.c_int32, 12), ('hotspot_crit_max', ctypes.c_int32, 16), ('hotspot_emergency_max', ctypes.c_int32, 20), ('mem_min', ctypes.c_int32, 24), ('mem_crit_max', ctypes.c_int32, 28), ('mem_emergency_max', ctypes.c_int32, 32), ('software_shutdown_temp', ctypes.c_int32, 36), ('software_shutdown_temp_offset', ctypes.c_int32, 40)])
@c.record
class struct_smu_state_validation_block(c.Struct):
  SIZE = 3
  single_display_only: bool
  disallow_on_dc: bool
  supported_power_levels: int
struct_smu_state_validation_block.register_fields([('single_display_only', ctypes.c_bool, 0), ('disallow_on_dc', ctypes.c_bool, 1), ('supported_power_levels', uint8_t, 2)])
@c.record
class struct_smu_uvd_clocks(c.Struct):
  SIZE = 8
  vclk: int
  dclk: int
struct_smu_uvd_clocks.register_fields([('vclk', uint32_t, 0), ('dclk', uint32_t, 4)])
enum_smu_power_src_type: dict[int, str] = {(SMU_POWER_SOURCE_AC:=0): 'SMU_POWER_SOURCE_AC', (SMU_POWER_SOURCE_DC:=1): 'SMU_POWER_SOURCE_DC', (SMU_POWER_SOURCE_COUNT:=2): 'SMU_POWER_SOURCE_COUNT'}
enum_smu_ppt_limit_type: dict[int, str] = {(SMU_DEFAULT_PPT_LIMIT:=0): 'SMU_DEFAULT_PPT_LIMIT', (SMU_FAST_PPT_LIMIT:=1): 'SMU_FAST_PPT_LIMIT'}
enum_smu_ppt_limit_level: dict[int, str] = {(SMU_PPT_LIMIT_MIN:=-1): 'SMU_PPT_LIMIT_MIN', (SMU_PPT_LIMIT_CURRENT:=0): 'SMU_PPT_LIMIT_CURRENT', (SMU_PPT_LIMIT_DEFAULT:=1): 'SMU_PPT_LIMIT_DEFAULT', (SMU_PPT_LIMIT_MAX:=2): 'SMU_PPT_LIMIT_MAX'}
enum_smu_memory_pool_size: dict[int, str] = {(SMU_MEMORY_POOL_SIZE_ZERO:=0): 'SMU_MEMORY_POOL_SIZE_ZERO', (SMU_MEMORY_POOL_SIZE_256_MB:=268435456): 'SMU_MEMORY_POOL_SIZE_256_MB', (SMU_MEMORY_POOL_SIZE_512_MB:=536870912): 'SMU_MEMORY_POOL_SIZE_512_MB', (SMU_MEMORY_POOL_SIZE_1_GB:=1073741824): 'SMU_MEMORY_POOL_SIZE_1_GB', (SMU_MEMORY_POOL_SIZE_2_GB:=2147483648): 'SMU_MEMORY_POOL_SIZE_2_GB'}
enum_smu_clk_type: dict[int, str] = {(SMU_GFXCLK:=0): 'SMU_GFXCLK', (SMU_VCLK:=1): 'SMU_VCLK', (SMU_DCLK:=2): 'SMU_DCLK', (SMU_VCLK1:=3): 'SMU_VCLK1', (SMU_DCLK1:=4): 'SMU_DCLK1', (SMU_ECLK:=5): 'SMU_ECLK', (SMU_SOCCLK:=6): 'SMU_SOCCLK', (SMU_UCLK:=7): 'SMU_UCLK', (SMU_DCEFCLK:=8): 'SMU_DCEFCLK', (SMU_DISPCLK:=9): 'SMU_DISPCLK', (SMU_PIXCLK:=10): 'SMU_PIXCLK', (SMU_PHYCLK:=11): 'SMU_PHYCLK', (SMU_FCLK:=12): 'SMU_FCLK', (SMU_SCLK:=13): 'SMU_SCLK', (SMU_MCLK:=14): 'SMU_MCLK', (SMU_PCIE:=15): 'SMU_PCIE', (SMU_LCLK:=16): 'SMU_LCLK', (SMU_OD_CCLK:=17): 'SMU_OD_CCLK', (SMU_OD_SCLK:=18): 'SMU_OD_SCLK', (SMU_OD_MCLK:=19): 'SMU_OD_MCLK', (SMU_OD_VDDC_CURVE:=20): 'SMU_OD_VDDC_CURVE', (SMU_OD_RANGE:=21): 'SMU_OD_RANGE', (SMU_OD_VDDGFX_OFFSET:=22): 'SMU_OD_VDDGFX_OFFSET', (SMU_OD_FAN_CURVE:=23): 'SMU_OD_FAN_CURVE', (SMU_OD_ACOUSTIC_LIMIT:=24): 'SMU_OD_ACOUSTIC_LIMIT', (SMU_OD_ACOUSTIC_TARGET:=25): 'SMU_OD_ACOUSTIC_TARGET', (SMU_OD_FAN_TARGET_TEMPERATURE:=26): 'SMU_OD_FAN_TARGET_TEMPERATURE', (SMU_OD_FAN_MINIMUM_PWM:=27): 'SMU_OD_FAN_MINIMUM_PWM', (SMU_CLK_COUNT:=28): 'SMU_CLK_COUNT'}
@c.record
class struct_smu_user_dpm_profile(c.Struct):
  SIZE = 140
  fan_mode: int
  power_limit: int
  fan_speed_pwm: int
  fan_speed_rpm: int
  flags: int
  user_od: int
  clk_mask: c.Array[ctypes.c_uint32, Literal[28]]
  clk_dependency: int
struct_smu_user_dpm_profile.register_fields([('fan_mode', uint32_t, 0), ('power_limit', uint32_t, 4), ('fan_speed_pwm', uint32_t, 8), ('fan_speed_rpm', uint32_t, 12), ('flags', uint32_t, 16), ('user_od', uint32_t, 20), ('clk_mask', c.Array[uint32_t, Literal[28]], 24), ('clk_dependency', uint32_t, 136)])
@c.record
class struct_smu_table(c.Struct):
  SIZE = 48
  size: int
  align: int
  domain: int
  mc_address: int
  cpu_addr: ctypes.c_void_p
  bo: c.POINTER[struct_amdgpu_bo]
  version: int
uint64_t: TypeAlias = ctypes.c_uint64
class struct_amdgpu_bo(c.Struct): pass
struct_smu_table.register_fields([('size', uint64_t, 0), ('align', uint32_t, 8), ('domain', uint8_t, 12), ('mc_address', uint64_t, 16), ('cpu_addr', ctypes.c_void_p, 24), ('bo', c.POINTER[struct_amdgpu_bo], 32), ('version', uint32_t, 40)])
enum_smu_perf_level_designation: dict[int, str] = {(PERF_LEVEL_ACTIVITY:=0): 'PERF_LEVEL_ACTIVITY', (PERF_LEVEL_POWER_CONTAINMENT:=1): 'PERF_LEVEL_POWER_CONTAINMENT'}
@c.record
class struct_smu_performance_level(c.Struct):
  SIZE = 24
  core_clock: int
  memory_clock: int
  vddc: int
  vddci: int
  non_local_mem_freq: int
  non_local_mem_width: int
struct_smu_performance_level.register_fields([('core_clock', uint32_t, 0), ('memory_clock', uint32_t, 4), ('vddc', uint32_t, 8), ('vddci', uint32_t, 12), ('non_local_mem_freq', uint32_t, 16), ('non_local_mem_width', uint32_t, 20)])
@c.record
class struct_smu_clock_info(c.Struct):
  SIZE = 24
  min_mem_clk: int
  max_mem_clk: int
  min_eng_clk: int
  max_eng_clk: int
  min_bus_bandwidth: int
  max_bus_bandwidth: int
struct_smu_clock_info.register_fields([('min_mem_clk', uint32_t, 0), ('max_mem_clk', uint32_t, 4), ('min_eng_clk', uint32_t, 8), ('max_eng_clk', uint32_t, 12), ('min_bus_bandwidth', uint32_t, 16), ('max_bus_bandwidth', uint32_t, 20)])
@c.record
class struct_smu_bios_boot_up_values(c.Struct):
  SIZE = 68
  revision: int
  gfxclk: int
  uclk: int
  socclk: int
  dcefclk: int
  eclk: int
  vclk: int
  dclk: int
  vddc: int
  vddci: int
  mvddc: int
  vdd_gfx: int
  cooling_id: int
  pp_table_id: int
  format_revision: int
  content_revision: int
  fclk: int
  lclk: int
  firmware_caps: int
struct_smu_bios_boot_up_values.register_fields([('revision', uint32_t, 0), ('gfxclk', uint32_t, 4), ('uclk', uint32_t, 8), ('socclk', uint32_t, 12), ('dcefclk', uint32_t, 16), ('eclk', uint32_t, 20), ('vclk', uint32_t, 24), ('dclk', uint32_t, 28), ('vddc', uint16_t, 32), ('vddci', uint16_t, 34), ('mvddc', uint16_t, 36), ('vdd_gfx', uint16_t, 38), ('cooling_id', uint8_t, 40), ('pp_table_id', uint32_t, 44), ('format_revision', uint32_t, 48), ('content_revision', uint32_t, 52), ('fclk', uint32_t, 56), ('lclk', uint32_t, 60), ('firmware_caps', uint32_t, 64)])
enum_smu_table_id: dict[int, str] = {(SMU_TABLE_PPTABLE:=0): 'SMU_TABLE_PPTABLE', (SMU_TABLE_WATERMARKS:=1): 'SMU_TABLE_WATERMARKS', (SMU_TABLE_CUSTOM_DPM:=2): 'SMU_TABLE_CUSTOM_DPM', (SMU_TABLE_DPMCLOCKS:=3): 'SMU_TABLE_DPMCLOCKS', (SMU_TABLE_AVFS:=4): 'SMU_TABLE_AVFS', (SMU_TABLE_AVFS_PSM_DEBUG:=5): 'SMU_TABLE_AVFS_PSM_DEBUG', (SMU_TABLE_AVFS_FUSE_OVERRIDE:=6): 'SMU_TABLE_AVFS_FUSE_OVERRIDE', (SMU_TABLE_PMSTATUSLOG:=7): 'SMU_TABLE_PMSTATUSLOG', (SMU_TABLE_SMU_METRICS:=8): 'SMU_TABLE_SMU_METRICS', (SMU_TABLE_DRIVER_SMU_CONFIG:=9): 'SMU_TABLE_DRIVER_SMU_CONFIG', (SMU_TABLE_ACTIVITY_MONITOR_COEFF:=10): 'SMU_TABLE_ACTIVITY_MONITOR_COEFF', (SMU_TABLE_OVERDRIVE:=11): 'SMU_TABLE_OVERDRIVE', (SMU_TABLE_I2C_COMMANDS:=12): 'SMU_TABLE_I2C_COMMANDS', (SMU_TABLE_PACE:=13): 'SMU_TABLE_PACE', (SMU_TABLE_ECCINFO:=14): 'SMU_TABLE_ECCINFO', (SMU_TABLE_COMBO_PPTABLE:=15): 'SMU_TABLE_COMBO_PPTABLE', (SMU_TABLE_WIFIBAND:=16): 'SMU_TABLE_WIFIBAND', (SMU_TABLE_COUNT:=17): 'SMU_TABLE_COUNT'}
PPSMC_Result_OK = 0x1
PPSMC_Result_Failed = 0xFF
PPSMC_Result_UnknownCmd = 0xFE
PPSMC_Result_CmdRejectedPrereq = 0xFD
PPSMC_Result_CmdRejectedBusy = 0xFC
PPSMC_MSG_TestMessage = 0x1
PPSMC_MSG_GetSmuVersion = 0x2
PPSMC_MSG_GetDriverIfVersion = 0x3
PPSMC_MSG_SetAllowedFeaturesMaskLow = 0x4
PPSMC_MSG_SetAllowedFeaturesMaskHigh = 0x5
PPSMC_MSG_EnableAllSmuFeatures = 0x6
PPSMC_MSG_DisableAllSmuFeatures = 0x7
PPSMC_MSG_EnableSmuFeaturesLow = 0x8
PPSMC_MSG_EnableSmuFeaturesHigh = 0x9
PPSMC_MSG_DisableSmuFeaturesLow = 0xA
PPSMC_MSG_DisableSmuFeaturesHigh = 0xB
PPSMC_MSG_GetEnabledSmuFeaturesLow = 0xC
PPSMC_MSG_GetEnabledSmuFeaturesHigh = 0xD
PPSMC_MSG_SetWorkloadMask = 0xE
PPSMC_MSG_SetPptLimit = 0xF
PPSMC_MSG_SetDriverDramAddrHigh = 0x10
PPSMC_MSG_SetDriverDramAddrLow = 0x11
PPSMC_MSG_SetToolsDramAddrHigh = 0x12
PPSMC_MSG_SetToolsDramAddrLow = 0x13
PPSMC_MSG_TransferTableSmu2Dram = 0x14
PPSMC_MSG_TransferTableDram2Smu = 0x15
PPSMC_MSG_UseDefaultPPTable = 0x16
PPSMC_MSG_UseBackupPPTable = 0x17
PPSMC_MSG_RunBtc = 0x18
PPSMC_MSG_RequestI2CBus = 0x19
PPSMC_MSG_ReleaseI2CBus = 0x1A
PPSMC_MSG_SetFloorSocVoltage = 0x21
PPSMC_MSG_SoftReset = 0x22
PPSMC_MSG_StartBacoMonitor = 0x23
PPSMC_MSG_CancelBacoMonitor = 0x24
PPSMC_MSG_EnterBaco = 0x25
PPSMC_MSG_SetSoftMinByFreq = 0x26
PPSMC_MSG_SetSoftMaxByFreq = 0x27
PPSMC_MSG_SetHardMinByFreq = 0x28
PPSMC_MSG_SetHardMaxByFreq = 0x29
PPSMC_MSG_GetMinDpmFreq = 0x2A
PPSMC_MSG_GetMaxDpmFreq = 0x2B
PPSMC_MSG_GetDpmFreqByIndex = 0x2C
PPSMC_MSG_GetDpmClockFreq = 0x2D
PPSMC_MSG_GetSsVoltageByDpm = 0x2E
PPSMC_MSG_SetMemoryChannelConfig = 0x2F
PPSMC_MSG_SetGeminiMode = 0x30
PPSMC_MSG_SetGeminiApertureHigh = 0x31
PPSMC_MSG_SetGeminiApertureLow = 0x32
PPSMC_MSG_SetMinLinkDpmByIndex = 0x33
PPSMC_MSG_OverridePcieParameters = 0x34
PPSMC_MSG_OverDriveSetPercentage = 0x35
PPSMC_MSG_SetMinDeepSleepDcefclk = 0x36
PPSMC_MSG_ReenableAcDcInterrupt = 0x37
PPSMC_MSG_NotifyPowerSource = 0x38
PPSMC_MSG_SetUclkFastSwitch = 0x39
PPSMC_MSG_SetUclkDownHyst = 0x3A
PPSMC_MSG_GetCurrentRpm = 0x3C
PPSMC_MSG_SetVideoFps = 0x3D
PPSMC_MSG_SetTjMax = 0x3E
PPSMC_MSG_SetFanTemperatureTarget = 0x3F
PPSMC_MSG_PrepareMp1ForUnload = 0x40
PPSMC_MSG_DramLogSetDramAddrHigh = 0x41
PPSMC_MSG_DramLogSetDramAddrLow = 0x42
PPSMC_MSG_DramLogSetDramSize = 0x43
PPSMC_MSG_SetFanMaxRpm = 0x44
PPSMC_MSG_SetFanMinPwm = 0x45
PPSMC_MSG_ConfigureGfxDidt = 0x46
PPSMC_MSG_NumOfDisplays = 0x47
PPSMC_MSG_RemoveMargins = 0x48
PPSMC_MSG_ReadSerialNumTop32 = 0x49
PPSMC_MSG_ReadSerialNumBottom32 = 0x4A
PPSMC_MSG_SetSystemVirtualDramAddrHigh = 0x4B
PPSMC_MSG_SetSystemVirtualDramAddrLow = 0x4C
PPSMC_MSG_WaflTest = 0x4D
PPSMC_MSG_SetFclkGfxClkRatio = 0x4E
PPSMC_MSG_AllowGfxOff = 0x51
PPSMC_MSG_DisallowGfxOff = 0x52
PPSMC_MSG_GetPptLimit = 0x53
PPSMC_MSG_GetDcModeMaxDpmFreq = 0x54
PPSMC_MSG_GetDebugData = 0x55
PPSMC_MSG_SetXgmiMode = 0x56
PPSMC_MSG_RunAfllBtc = 0x57
PPSMC_MSG_ExitBaco = 0x58
PPSMC_MSG_PrepareMp1ForReset = 0x59
PPSMC_MSG_PrepareMp1ForShutdown = 0x5A
PPSMC_MSG_SetMGpuFanBoostLimitRpm = 0x5D
PPSMC_MSG_GetAVFSVoltageByDpm = 0x5F
PPSMC_MSG_BacoWorkAroundFlushVDCI = 0x60
PPSMC_MSG_DFCstateControl = 0x63
PPSMC_Message_Count = 0x64
PPTABLE_V20_SMU_VERSION = 3
NUM_GFXCLK_DPM_LEVELS = 16
NUM_VCLK_DPM_LEVELS = 8
NUM_DCLK_DPM_LEVELS = 8
NUM_ECLK_DPM_LEVELS = 8
NUM_MP0CLK_DPM_LEVELS = 2
NUM_SOCCLK_DPM_LEVELS = 8
NUM_UCLK_DPM_LEVELS = 4
NUM_FCLK_DPM_LEVELS = 8
NUM_DCEFCLK_DPM_LEVELS = 8
NUM_DISPCLK_DPM_LEVELS = 8
NUM_PIXCLK_DPM_LEVELS = 8
NUM_PHYCLK_DPM_LEVELS = 8
NUM_LINK_LEVELS = 2
NUM_XGMI_LEVELS = 2
MAX_GFXCLK_DPM_LEVEL = (NUM_GFXCLK_DPM_LEVELS  - 1)
MAX_VCLK_DPM_LEVEL = (NUM_VCLK_DPM_LEVELS    - 1)
MAX_DCLK_DPM_LEVEL = (NUM_DCLK_DPM_LEVELS    - 1)
MAX_ECLK_DPM_LEVEL = (NUM_ECLK_DPM_LEVELS    - 1)
MAX_MP0CLK_DPM_LEVEL = (NUM_MP0CLK_DPM_LEVELS  - 1)
MAX_SOCCLK_DPM_LEVEL = (NUM_SOCCLK_DPM_LEVELS  - 1)
MAX_UCLK_DPM_LEVEL = (NUM_UCLK_DPM_LEVELS    - 1)
MAX_FCLK_DPM_LEVEL = (NUM_FCLK_DPM_LEVELS    - 1)
MAX_DCEFCLK_DPM_LEVEL = (NUM_DCEFCLK_DPM_LEVELS - 1)
MAX_DISPCLK_DPM_LEVEL = (NUM_DISPCLK_DPM_LEVELS - 1)
MAX_PIXCLK_DPM_LEVEL = (NUM_PIXCLK_DPM_LEVELS  - 1)
MAX_PHYCLK_DPM_LEVEL = (NUM_PHYCLK_DPM_LEVELS  - 1)
MAX_LINK_LEVEL = (NUM_LINK_LEVELS        - 1)
MAX_XGMI_LEVEL = (NUM_XGMI_LEVELS        - 1)
PPSMC_GeminiModeNone = 0
PPSMC_GeminiModeMaster = 1
PPSMC_GeminiModeSlave = 2
FEATURE_DPM_PREFETCHER_BIT = 0
FEATURE_DPM_GFXCLK_BIT = 1
FEATURE_DPM_UCLK_BIT = 2
FEATURE_DPM_SOCCLK_BIT = 3
FEATURE_DPM_UVD_BIT = 4
FEATURE_DPM_VCE_BIT = 5
FEATURE_ULV_BIT = 6
FEATURE_DPM_MP0CLK_BIT = 7
FEATURE_DPM_LINK_BIT = 8
FEATURE_DPM_DCEFCLK_BIT = 9
FEATURE_DS_GFXCLK_BIT = 10
FEATURE_DS_SOCCLK_BIT = 11
FEATURE_DS_LCLK_BIT = 12
FEATURE_PPT_BIT = 13
FEATURE_TDC_BIT = 14
FEATURE_THERMAL_BIT = 15
FEATURE_GFX_PER_CU_CG_BIT = 16
FEATURE_RM_BIT = 17
FEATURE_DS_DCEFCLK_BIT = 18
FEATURE_ACDC_BIT = 19
FEATURE_VR0HOT_BIT = 20
FEATURE_VR1HOT_BIT = 21
FEATURE_FW_CTF_BIT = 22
FEATURE_LED_DISPLAY_BIT = 23
FEATURE_FAN_CONTROL_BIT = 24
FEATURE_GFX_EDC_BIT = 25
FEATURE_GFXOFF_BIT = 26
FEATURE_CG_BIT = 27
FEATURE_DPM_FCLK_BIT = 28
FEATURE_DS_FCLK_BIT = 29
FEATURE_DS_MP1CLK_BIT = 30
FEATURE_DS_MP0CLK_BIT = 31
FEATURE_XGMI_BIT = 32
FEATURE_ECC_BIT = 33
FEATURE_SPARE_34_BIT = 34
FEATURE_SPARE_35_BIT = 35
FEATURE_SPARE_36_BIT = 36
FEATURE_SPARE_37_BIT = 37
FEATURE_SPARE_38_BIT = 38
FEATURE_SPARE_39_BIT = 39
FEATURE_SPARE_40_BIT = 40
FEATURE_SPARE_41_BIT = 41
FEATURE_SPARE_42_BIT = 42
FEATURE_SPARE_43_BIT = 43
FEATURE_SPARE_44_BIT = 44
FEATURE_SPARE_45_BIT = 45
FEATURE_SPARE_46_BIT = 46
FEATURE_SPARE_47_BIT = 47
FEATURE_SPARE_48_BIT = 48
FEATURE_SPARE_49_BIT = 49
FEATURE_SPARE_50_BIT = 50
FEATURE_SPARE_51_BIT = 51
FEATURE_SPARE_52_BIT = 52
FEATURE_SPARE_53_BIT = 53
FEATURE_SPARE_54_BIT = 54
FEATURE_SPARE_55_BIT = 55
FEATURE_SPARE_56_BIT = 56
FEATURE_SPARE_57_BIT = 57
FEATURE_SPARE_58_BIT = 58
FEATURE_SPARE_59_BIT = 59
FEATURE_SPARE_60_BIT = 60
FEATURE_SPARE_61_BIT = 61
FEATURE_SPARE_62_BIT = 62
FEATURE_SPARE_63_BIT = 63
NUM_FEATURES = 64
FEATURE_DPM_PREFETCHER_MASK = (1 << FEATURE_DPM_PREFETCHER_BIT     )
FEATURE_DPM_GFXCLK_MASK = (1 << FEATURE_DPM_GFXCLK_BIT         )
FEATURE_DPM_UCLK_MASK = (1 << FEATURE_DPM_UCLK_BIT           )
FEATURE_DPM_SOCCLK_MASK = (1 << FEATURE_DPM_SOCCLK_BIT         )
FEATURE_DPM_UVD_MASK = (1 << FEATURE_DPM_UVD_BIT            )
FEATURE_DPM_VCE_MASK = (1 << FEATURE_DPM_VCE_BIT            )
FEATURE_ULV_MASK = (1 << FEATURE_ULV_BIT                )
FEATURE_DPM_MP0CLK_MASK = (1 << FEATURE_DPM_MP0CLK_BIT         )
FEATURE_DPM_LINK_MASK = (1 << FEATURE_DPM_LINK_BIT           )
FEATURE_DPM_DCEFCLK_MASK = (1 << FEATURE_DPM_DCEFCLK_BIT        )
FEATURE_DS_GFXCLK_MASK = (1 << FEATURE_DS_GFXCLK_BIT          )
FEATURE_DS_SOCCLK_MASK = (1 << FEATURE_DS_SOCCLK_BIT          )
FEATURE_DS_LCLK_MASK = (1 << FEATURE_DS_LCLK_BIT            )
FEATURE_PPT_MASK = (1 << FEATURE_PPT_BIT                )
FEATURE_TDC_MASK = (1 << FEATURE_TDC_BIT                )
FEATURE_THERMAL_MASK = (1 << FEATURE_THERMAL_BIT            )
FEATURE_GFX_PER_CU_CG_MASK = (1 << FEATURE_GFX_PER_CU_CG_BIT      )
FEATURE_RM_MASK = (1 << FEATURE_RM_BIT                 )
FEATURE_DS_DCEFCLK_MASK = (1 << FEATURE_DS_DCEFCLK_BIT         )
FEATURE_ACDC_MASK = (1 << FEATURE_ACDC_BIT               )
FEATURE_VR0HOT_MASK = (1 << FEATURE_VR0HOT_BIT             )
FEATURE_VR1HOT_MASK = (1 << FEATURE_VR1HOT_BIT             )
FEATURE_FW_CTF_MASK = (1 << FEATURE_FW_CTF_BIT             )
FEATURE_LED_DISPLAY_MASK = (1 << FEATURE_LED_DISPLAY_BIT        )
FEATURE_FAN_CONTROL_MASK = (1 << FEATURE_FAN_CONTROL_BIT        )
FEATURE_GFX_EDC_MASK = (1 << FEATURE_GFX_EDC_BIT            )
FEATURE_GFXOFF_MASK = (1 << FEATURE_GFXOFF_BIT             )
FEATURE_CG_MASK = (1 << FEATURE_CG_BIT                 )
FEATURE_DPM_FCLK_MASK = (1 << FEATURE_DPM_FCLK_BIT           )
FEATURE_DS_FCLK_MASK = (1 << FEATURE_DS_FCLK_BIT            )
FEATURE_DS_MP1CLK_MASK = (1 << FEATURE_DS_MP1CLK_BIT          )
FEATURE_DS_MP0CLK_MASK = (1 << FEATURE_DS_MP0CLK_BIT          )
FEATURE_XGMI_MASK = (1 << FEATURE_XGMI_BIT               )
FEATURE_ECC_MASK = (1 << FEATURE_ECC_BIT                )
DPM_OVERRIDE_DISABLE_SOCCLK_PID = 0x00000001
DPM_OVERRIDE_DISABLE_UCLK_PID = 0x00000002
DPM_OVERRIDE_ENABLE_VOLT_LINK_UVD_SOCCLK = 0x00000004
DPM_OVERRIDE_ENABLE_VOLT_LINK_UVD_UCLK = 0x00000008
DPM_OVERRIDE_ENABLE_FREQ_LINK_VCLK_SOCCLK = 0x00000010
DPM_OVERRIDE_ENABLE_FREQ_LINK_VCLK_UCLK = 0x00000020
DPM_OVERRIDE_ENABLE_FREQ_LINK_DCLK_SOCCLK = 0x00000040
DPM_OVERRIDE_ENABLE_FREQ_LINK_DCLK_UCLK = 0x00000080
DPM_OVERRIDE_ENABLE_VOLT_LINK_VCE_SOCCLK = 0x00000100
DPM_OVERRIDE_ENABLE_VOLT_LINK_VCE_UCLK = 0x00000200
DPM_OVERRIDE_ENABLE_FREQ_LINK_ECLK_SOCCLK = 0x00000400
DPM_OVERRIDE_ENABLE_FREQ_LINK_ECLK_UCLK = 0x00000800
DPM_OVERRIDE_ENABLE_FREQ_LINK_GFXCLK_SOCCLK = 0x00001000
DPM_OVERRIDE_ENABLE_FREQ_LINK_GFXCLK_UCLK = 0x00002000
DPM_OVERRIDE_ENABLE_GFXOFF_GFXCLK_SWITCH = 0x00004000
DPM_OVERRIDE_ENABLE_GFXOFF_SOCCLK_SWITCH = 0x00008000
DPM_OVERRIDE_ENABLE_GFXOFF_UCLK_SWITCH = 0x00010000
DPM_OVERRIDE_ENABLE_GFXOFF_FCLK_SWITCH = 0x00020000
I2C_CONTROLLER_ENABLED = 1
I2C_CONTROLLER_DISABLED = 0
VR_MAPPING_VR_SELECT_MASK = 0x01
VR_MAPPING_VR_SELECT_SHIFT = 0x00
VR_MAPPING_PLANE_SELECT_MASK = 0x02
VR_MAPPING_PLANE_SELECT_SHIFT = 0x01
PSI_SEL_VR0_PLANE0_PSI0 = 0x01
PSI_SEL_VR0_PLANE0_PSI1 = 0x02
PSI_SEL_VR0_PLANE1_PSI0 = 0x04
PSI_SEL_VR0_PLANE1_PSI1 = 0x08
PSI_SEL_VR1_PLANE0_PSI0 = 0x10
PSI_SEL_VR1_PLANE0_PSI1 = 0x20
PSI_SEL_VR1_PLANE1_PSI0 = 0x40
PSI_SEL_VR1_PLANE1_PSI1 = 0x80
THROTTLER_STATUS_PADDING_BIT = 0
THROTTLER_STATUS_TEMP_EDGE_BIT = 1
THROTTLER_STATUS_TEMP_HOTSPOT_BIT = 2
THROTTLER_STATUS_TEMP_HBM_BIT = 3
THROTTLER_STATUS_TEMP_VR_GFX_BIT = 4
THROTTLER_STATUS_TEMP_VR_SOC_BIT = 5
THROTTLER_STATUS_TEMP_VR_MEM0_BIT = 6
THROTTLER_STATUS_TEMP_VR_MEM1_BIT = 7
THROTTLER_STATUS_TEMP_LIQUID_BIT = 8
THROTTLER_STATUS_TEMP_PLX_BIT = 9
THROTTLER_STATUS_TEMP_SKIN_BIT = 10
THROTTLER_STATUS_TDC_GFX_BIT = 11
THROTTLER_STATUS_TDC_SOC_BIT = 12
THROTTLER_STATUS_PPT_BIT = 13
THROTTLER_STATUS_FIT_BIT = 14
THROTTLER_STATUS_PPM_BIT = 15
TABLE_TRANSFER_OK = 0x0
TABLE_TRANSFER_FAILED = 0xFF
WORKLOAD_DEFAULT_BIT = 0
WORKLOAD_PPLIB_FULL_SCREEN_3D_BIT = 1
WORKLOAD_PPLIB_POWER_SAVING_BIT = 2
WORKLOAD_PPLIB_VIDEO_BIT = 3
WORKLOAD_PPLIB_VR_BIT = 4
WORKLOAD_PPLIB_COMPUTE_BIT = 5
WORKLOAD_PPLIB_CUSTOM_BIT = 6
WORKLOAD_PPLIB_COUNT = 7
XGMI_STATE_D0 = 1
XGMI_STATE_D3 = 0
NUM_WM_RANGES = 4
TABLE_PPTABLE = 0
TABLE_WATERMARKS = 1
TABLE_AVFS = 2
TABLE_AVFS_PSM_DEBUG = 3
TABLE_AVFS_FUSE_OVERRIDE = 4
TABLE_PMSTATUSLOG = 5
TABLE_SMU_METRICS = 6
TABLE_DRIVER_SMU_CONFIG = 7
TABLE_ACTIVITY_MONITOR_COEFF = 8
TABLE_OVERDRIVE = 9
TABLE_COUNT = 10
UCLK_SWITCH_SLOW = 0
UCLK_SWITCH_FAST = 1
SQ_Enable_MASK = 0x1
SQ_IR_MASK = 0x2
SQ_PCC_MASK = 0x4
SQ_EDC_MASK = 0x8
TCP_Enable_MASK = 0x100
TCP_IR_MASK = 0x200
TCP_PCC_MASK = 0x400
TCP_EDC_MASK = 0x800
TD_Enable_MASK = 0x10000
TD_IR_MASK = 0x20000
TD_PCC_MASK = 0x40000
TD_EDC_MASK = 0x80000
DB_Enable_MASK = 0x1000000
DB_IR_MASK = 0x2000000
DB_PCC_MASK = 0x4000000
DB_EDC_MASK = 0x8000000
SQ_Enable_SHIFT = 0
SQ_IR_SHIFT = 1
SQ_PCC_SHIFT = 2
SQ_EDC_SHIFT = 3
TCP_Enable_SHIFT = 8
TCP_IR_SHIFT = 9
TCP_PCC_SHIFT = 10
TCP_EDC_SHIFT = 11
TD_Enable_SHIFT = 16
TD_IR_SHIFT = 17
TD_PCC_SHIFT = 18
TD_EDC_SHIFT = 19
DB_Enable_SHIFT = 24
DB_IR_SHIFT = 25
DB_PCC_SHIFT = 26
DB_EDC_SHIFT = 27
REMOVE_FMAX_MARGIN_BIT = 0x0
REMOVE_DCTOL_MARGIN_BIT = 0x1
REMOVE_PLATFORM_MARGIN_BIT = 0x2
SMU_THERMAL_MINIMUM_ALERT_TEMP = 0
SMU_THERMAL_MAXIMUM_ALERT_TEMP = 255
SMU_TEMPERATURE_UNITS_PER_CENTIGRADES = 1000
SMU_FW_NAME_LEN = 0x24
SMU_DPM_USER_PROFILE_RESTORE = (1 << 0)
SMU_CUSTOM_FAN_SPEED_RPM = (1 << 1)
SMU_CUSTOM_FAN_SPEED_PWM = (1 << 2)
SMU_THROTTLER_PPT0_BIT = 0
SMU_THROTTLER_PPT1_BIT = 1
SMU_THROTTLER_PPT2_BIT = 2
SMU_THROTTLER_PPT3_BIT = 3
SMU_THROTTLER_SPL_BIT = 4
SMU_THROTTLER_FPPT_BIT = 5
SMU_THROTTLER_SPPT_BIT = 6
SMU_THROTTLER_SPPT_APU_BIT = 7
SMU_THROTTLER_TDC_GFX_BIT = 16
SMU_THROTTLER_TDC_SOC_BIT = 17
SMU_THROTTLER_TDC_MEM_BIT = 18
SMU_THROTTLER_TDC_VDD_BIT = 19
SMU_THROTTLER_TDC_CVIP_BIT = 20
SMU_THROTTLER_EDC_CPU_BIT = 21
SMU_THROTTLER_EDC_GFX_BIT = 22
SMU_THROTTLER_APCC_BIT = 23
SMU_THROTTLER_TEMP_GPU_BIT = 32
SMU_THROTTLER_TEMP_CORE_BIT = 33
SMU_THROTTLER_TEMP_MEM_BIT = 34
SMU_THROTTLER_TEMP_EDGE_BIT = 35
SMU_THROTTLER_TEMP_HOTSPOT_BIT = 36
SMU_THROTTLER_TEMP_SOC_BIT = 37
SMU_THROTTLER_TEMP_VR_GFX_BIT = 38
SMU_THROTTLER_TEMP_VR_SOC_BIT = 39
SMU_THROTTLER_TEMP_VR_MEM0_BIT = 40
SMU_THROTTLER_TEMP_VR_MEM1_BIT = 41
SMU_THROTTLER_TEMP_LIQUID0_BIT = 42
SMU_THROTTLER_TEMP_LIQUID1_BIT = 43
SMU_THROTTLER_VRHOT0_BIT = 44
SMU_THROTTLER_VRHOT1_BIT = 45
SMU_THROTTLER_PROCHOT_CPU_BIT = 46
SMU_THROTTLER_PROCHOT_GFX_BIT = 47
SMU_THROTTLER_PPM_BIT = 56
SMU_THROTTLER_FIT_BIT = 57