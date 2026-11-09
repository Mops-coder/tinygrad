#include "TinyGPUDriverUserClient.h"
#include "TinyGPUDriver.h"
#include <DriverKit/DriverKit.h>
#include <DriverKit/OSSharedPtr.h>
#include <PCIDriverKit/PCIDriverKit.h>

struct TinyGPUDriverUserClient_IVars
{
	OSSharedPtr<TinyGPUDriver> provider = nullptr;

	TinyGPUCreateDMAResp *dmas = nullptr;
	size_t dmaCount = 0;
	size_t dmaCap = 0;
	int access = 0;   // mi50: 0 not checked yet, 1 the client may drive the card, -1 refused
	bool quiesce = false;   // mi50: QuiesceOnStop received

	int ensureDMACap(size_t need)
	{
		// not thread-safe
		if (need <= dmaCap) return 0;

		size_t newCap = dmaCap ? dmaCap * 2 : 16;
		while (newCap < need) newCap *= 2;

		auto *newArr = IONewZero(TinyGPUCreateDMAResp, newCap);
		if (!newArr) return -kIOReturnNoMemory;

		if (dmas && dmaCount) {
			memcpy(newArr, dmas, dmaCount * sizeof(TinyGPUCreateDMAResp));
		}

		IOSafeDeleteNULL(dmas, TinyGPUCreateDMAResp, dmaCap);
		dmas = newArr;
		dmaCap = newCap;
		return 0;
	}
};

bool TinyGPUDriverUserClient::init()
{
	auto ok = super::init();
	if (!ok) return false;

	ivars = IONewZero(TinyGPUDriverUserClient_IVars, 1);
	if (!ivars) return false;
	return true;
}

void TinyGPUDriverUserClient::free()
{
	if (ivars) {
		IOSafeDeleteNULL(ivars, TinyGPUDriverUserClient_IVars, 1);
	}
	super::free();
}

kern_return_t TinyGPUDriverUserClient::Start_Impl(IOService* in_provider)
{
	kern_return_t err = kIOReturnSuccess;
	if (!in_provider) {
		os_log(OS_LOG_DEFAULT, "tinygpu: provider is null");
		err = kIOReturnBadArgument;
		goto error;
	}

	err = Start(in_provider, SUPERDISPATCH);
	if (err) {
		os_log(OS_LOG_DEFAULT, "tinygpu: failed to start super (%d)", err);
		goto error;
	}

	// mi50: a provider that is not the TinyGPU driver used to start with no provider, and every RPC dereferenced it
	ivars->provider = OSSharedPtr(OSDynamicCast(TinyGPUDriver, in_provider), OSRetain);
	if (!ivars->provider.get()) {
		err = kIOReturnBadArgument;
		goto error;
	}
	return 0;

error:
	ivars->provider.reset();
	return err;
}

kern_return_t TinyGPUDriverUserClient::Stop_Impl(IOService* in_provider)
{
	// release all DMA allocations for this client
	if (ivars) {
		// mi50: QuiesceOnStop: the card stops its DMA before the mappings below are completed, however the session ended (the server's
		// cleanup, a killed or crashed server); a card that reads 0xffff is gone and is left alone
		uint32_t cmd = 0;
		if (ivars->quiesce && ivars->provider.get() && !ivars->provider->CfgRead(kIOPCIConfigurationOffsetCommand, 2, &cmd) && cmd != 0xffff &&
		    (cmd & kIOPCICommandBusMaster))
			ivars->provider->CfgWrite(kIOPCIConfigurationOffsetCommand, 2, cmd & ~(uint32_t)kIOPCICommandBusMaster);
		for (size_t i = 0; i < ivars->dmaCount; i++) {
			auto &d = ivars->dmas[i];
			if (d.dmaCmd) {
				d.dmaCmd->CompleteDMA(kIODMACommandCompleteDMANoOptions);
				d.dmaCmd->release();
				d.dmaCmd = nullptr;
			}
		}
		ivars->dmaCount = 0;
		IOSafeDeleteNULL(ivars->dmas, TinyGPUCreateDMAResp, ivars->dmaCap);
		ivars->dmas = nullptr;
		if (ivars->provider.get()) ivars->provider->MI50SessionEnd();   // mi50 (F-02): after the quiesce and the DMA mappings: the session ended
		ivars->provider.reset();
	}

	return Stop(in_provider, SUPERDISPATCH);
}

// mi50: only TinyGPU itself may drive a card. Its app carries the DriverKit user-client entitlement naming this driver
// (macOS.entitlements: com.apple.developer.driverkit.userclient-access = [org.tinygrad.tinygpu.driver2]); the NoSIP build of this
// driver is signed with allow-any-userclient-access, so without this check any local process could map the BARs and DMA through it.
static bool ClientMayAccess(TinyGPUDriverUserClient* uc, TinyGPUDriverUserClient_IVars* iv)
{
	if (iv->access == 0) {
		OSDictionary* ents = nullptr;
		bool ok = false;
		if (uc->CopyClientEntitlements(&ents) == kIOReturnSuccess && ents) {
			OSArray* arr = OSDynamicCast(OSArray, ents->getObject("com.apple.developer.driverkit.userclient-access"));
			for (uint32_t i = 0; arr && i < arr->getCount(); i++) {
				OSString* s = OSDynamicCast(OSString, arr->getObject(i));
				if (s && s->isEqualTo("org.tinygrad.tinygpu.driver2")) ok = true;
			}
			ents->release();
		}
		iv->access = ok ? 1 : -1;
	}
	return iv->access > 0;
}

kern_return_t TinyGPUDriverUserClient::ExternalMethod(uint64_t selector, IOUserClientMethodArguments* args, const IOUserClientMethodDispatch* in_dispatch, OSObject* in_target, void* in_reference)
{
	kern_return_t err = 0;
	if (!ClientMayAccess(this, ivars)) return kIOReturnNotPermitted;
	// mi50: no provider after Stop (or a failed Start): refused like CopyClientMemoryForType, not dereferenced
	if (!ivars->provider.get()) return kIOReturnNotAttached;
	// mi50: a MI50 whose bring-up failed serves RESET, which brings it up again (review finding 15; the reason is logged), and the MI50
	// selector, which answers every operation with that reason (run_op) - the TinyGPU server reads it to retry or to refuse the session (bug 1)
	if (selector != TinyGPURPC::Reset && selector != TinyGPURPC::MI50 && ivars->provider->MI50Refused()) return kIOReturnNotReady;

	os_log(OS_LOG_DEFAULT, "tinygpu: rpc (%llu) in:%d, out:%d", selector, args->scalarInputCount, args->scalarOutputCount);

	if (selector == TinyGPURPC::ReadCfg) {
		if (args->scalarInputCount != 2 or args->scalarOutputCount < 1) return kIOReturnBadArgument;

		uint32_t off = uint32_t(args->scalarInput[0]);
		uint32_t size = uint32_t(args->scalarInput[1]);

		uint32_t val = 0;
		err = ivars->provider->CfgRead(off, size, &val);
		os_log(OS_LOG_DEFAULT, "tinygpu: read cfg off:%x sz:%d, val:%x", off, size, val);

		if (!err) {
			args->scalarOutput[0] = val;
			args->scalarOutputCount = 1;
		}
		return err;
	} else if (selector == TinyGPURPC::WriteCfg) {
		if (args->scalarInputCount != 3) return kIOReturnBadArgument;

		uint32_t off = uint32_t(args->scalarInput[0]);
		uint32_t size = uint32_t(args->scalarInput[1]);
		uint32_t val = uint32_t(args->scalarInput[2]);

		os_log(OS_LOG_DEFAULT, "tinygpu: wr cfg off:%x sz:%d, val:%x", off, size, val);
		return ivars->provider->CfgWrite(off, size, val);
	} else if (selector == TinyGPURPC::Reset) {
		os_log(OS_LOG_DEFAULT, "tinygpu: reset");
		return ivars->provider->ResetDevice();
	} else if (selector == TinyGPURPC::PrepareDMA) {
		// both input and output buffers must be >= 4097 bytes for IOMemoryDescriptor
		if (!args->structureInputDescriptor || !args->structureOutputDescriptor) {
			os_log(OS_LOG_DEFAULT, "tinygpu: PrepareDMA requires buffers >= 4097 bytes");
			return kIOReturnBadArgument;
		}
		if (ivars->ensureDMACap(ivars->dmaCount + 1)) return kIOReturnNoMemory;

		uint64_t size = 0;
		args->structureInputDescriptor->GetLength(&size);

		IODMACommand* dmaCmd = nullptr;
		IOAddressSegment segments[32];
		uint32_t segCount = 32;
		err = ivars->provider->SetupDMA(args->structureInputDescriptor, size, &dmaCmd, segments, &segCount);
		if (err) return err;

		// write physical addresses to output: [addr0, len0, addr1, len1, ..., 0, 0]
		IOMemoryMap* outMap = nullptr;
		err = args->structureOutputDescriptor->CreateMapping(0, 0, 0, 0, 0, &outMap);
		if (err || !outMap) { os_log(OS_LOG_DEFAULT, "tinygpu: output map failed err=%d", err); dmaCmd->CompleteDMA(kIODMACommandCompleteDMANoOptions); dmaCmd->release(); return err ?: kIOReturnError; }

		uint64_t* out = (uint64_t*)outMap->GetAddress();
		for (uint32_t i = 0; i < segCount; i++) { out[i * 2] = segments[i].address; out[i * 2 + 1] = segments[i].length; }
		out[segCount * 2] = 0; out[segCount * 2 + 1] = 0;
		outMap->release();

		os_log(OS_LOG_DEFAULT, "tinygpu: PrepareDMA size=%llu segs=%u", size, segCount);
		ivars->dmas[ivars->dmaCount++] = {nullptr, dmaCmd, segCount ? segments[0].address : 0};
		return kIOReturnSuccess;
	} else if (selector == TinyGPURPC::ReleaseDMA) {
		// mi50: free one host-memory mapping while the client keeps running (a long-running model used to run out of mappings)
		if (args->scalarInputCount != 1) return kIOReturnBadArgument;
		uint64_t iova = args->scalarInput[0];
		for (size_t i = 0; i < ivars->dmaCount; i++) {
			auto &d = ivars->dmas[i];
			if (!d.dmaCmd || d.iova != iova) continue;
			d.dmaCmd->CompleteDMA(kIODMACommandCompleteDMANoOptions);
			d.dmaCmd->release();
			// sharedBuf (CreateDMA path) was handed to the caller by CopyClientMemoryForType; like Stop, never released here
			ivars->dmas[i] = ivars->dmas[--ivars->dmaCount];   // order of the list does not matter
			ivars->dmas[ivars->dmaCount] = {};
			return kIOReturnSuccess;
		}
		return kIOReturnNotFound;
	} else if (selector == TinyGPURPC::QuiesceOnStop) {
		// mi50: sent by the server for a client that named its card (that client sets bus master again at every open, full and partial boot)
		if (args->scalarInputCount != 0) return kIOReturnBadArgument;
		ivars->quiesce = true;
		return kIOReturnSuccess;
	} else if (selector == TinyGPURPC::MI50) {
		// mi50: one MI50 tuning operation; the reply is an OSData the OS releases (IOUserClient.iig IOUserClientMethodArguments.structureOutput),
		// which must not be set when the caller passed an output descriptor
		if (!args->structureInput || args->structureOutputDescriptor) return kIOReturnBadArgument;
		OSData* out = nullptr;
		err = ivars->provider->MI50Call(args->structureInput, &out);
		if (err) return err;
		if (args->structureOutputMaximumSize != kIOUserClientVariableStructureSize && out->getLength() > args->structureOutputMaximumSize) {
			out->release();
			return kIOReturnNoSpace;
		}
		args->structureOutput = out;
		return kIOReturnSuccess;
	}

	return kIOReturnUnsupported;
}

kern_return_t IMPL(TinyGPUDriverUserClient, CopyClientMemoryForType)
{
	if (!memory) return kIOReturnBadArgument;
	if (!ivars->provider.get()) return kIOReturnNotAttached;
	if (!ClientMayAccess(this, ivars)) return kIOReturnNotPermitted;
	if (ivars->provider->MI50Refused()) return kIOReturnNotReady;   // mi50: a MI50 whose bring-up failed: nothing is mapped (finding 15)

	// bar handling, type is bar num
	if (type < 6) {
		uint32_t bar = (uint32_t)type;
		return ivars->provider->MapBar(bar, memory);
	}

	// dma handling, type is size
	if (ivars->ensureDMACap(ivars->dmaCount + 1)) {
		os_log(OS_LOG_DEFAULT, "tinygpu: cannot grow dma array");
		return kIOReturnNoMemory;
	}

	TinyGPUCreateDMAResp buf{};
	kern_return_t err = ivars->provider->CreateDMA(type, &buf);
	if (err) return err;

	ivars->dmas[ivars->dmaCount++] = buf;
	*memory = buf.sharedBuf;
	return 0;
}
