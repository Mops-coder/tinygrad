// stands in for the iig-generated header of TinyGPUDriver.iig (same members; the iig output adds the IPC dispatch only)
#pragma once
#include <DriverKit/IOLib.h>
struct TinyGPUCreateDMAResp { IOBufferMemoryDescriptor* sharedBuf; IODMACommand* dmaCmd; uint64_t iova; };
struct TinyGPUDriver_IVars;
class TinyGPUDriver : public IOService { typedef IOService super; public:
  TinyGPUDriver_IVars* ivars = nullptr;
  bool init() override; void free() override;
  kern_return_t Start_Impl(IOService* p); kern_return_t Stop_Impl(IOService* p);
  kern_return_t NewUserClient_Impl(uint32_t t, IOUserClient** out);
  kern_return_t MapBar(uint32_t bar, IOMemoryDescriptor** memory);
  kern_return_t CreateDMA(size_t size, TinyGPUCreateDMAResp* dmaDesc);
  kern_return_t SetupDMA(IOMemoryDescriptor* memory, uint64_t size, IODMACommand** outCmd, IOAddressSegment* segments, uint32_t* segCount);
  kern_return_t CfgRead(uint32_t off, uint32_t size, uint32_t* val); kern_return_t CfgWrite(uint32_t off, uint32_t size, uint32_t val);
  kern_return_t ResetDevice(); IOPCIDevice* GetPCI();
  kern_return_t SetPowerState_Impl(uint32_t powerFlags); kern_return_t MI50Call(OSData* in, OSData** out); bool MI50Refused();
  void MI50SessionEnd(); };
