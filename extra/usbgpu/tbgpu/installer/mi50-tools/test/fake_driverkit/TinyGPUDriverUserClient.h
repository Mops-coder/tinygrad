// stands in for the iig-generated header of TinyGPUDriverUserClient.iig
#pragma once
#include <DriverKit/IOLib.h>
enum TinyGPURPC { ReadCfg, WriteCfg, Reset, PrepareDMA, ReleaseDMA, QuiesceOnStop, MI50 };
struct TinyGPUDriverUserClient_IVars;
class TinyGPUDriverUserClient : public IOUserClient { typedef IOUserClient super; public:
  TinyGPUDriverUserClient_IVars* ivars = nullptr;
  bool init() override; void free() override;
  kern_return_t Start_Impl(IOService* p); kern_return_t Stop_Impl(IOService* p);
  kern_return_t ExternalMethod(uint64_t sel, IOUserClientMethodArguments* a, const IOUserClientMethodDispatch* d, OSObject* t, void* r);
  kern_return_t CopyClientMemoryForType_Impl(uint64_t type, uint64_t* options, IOMemoryDescriptor** memory); };
