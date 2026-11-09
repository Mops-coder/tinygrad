// dk_card.hpp - what the DriverKit stand-ins (fake_driverkit/DriverKit/IOLib.h) declare, defined over the virtual MI50. With a card attached
// (CARD) the IOPCIDevice's configuration and BAR5 accesses, IODelay / IOSleep / the uptime clock, IOMallocZero / IOFree and the bring-up's own
// os_log lines go to the card, and DriverKit's contract is checked on the way (BAR5 accesses through BAR5's memory index, dword aligned,
// inside the BAR; the uptime clock; IOFree with the IOMallocZero size); without a card a device is its configuration array and time is a
// counter. CopyProviderProperties returns CHAIN, each dictionary cut down to the requested keys. Include after TinyGPUDriver.cpp.
#pragma once
#include "vmi50.hpp"

static vm::Card *CARD;                              // the card behind every IOPCIDevice, its time and memory (none: nullptr)
static std::vector<std::string> BAD, KEYS;          // DriverKit contract violations; the keys CopyProviderProperties was asked for
static int ACCESSES;                                // IOPCIDevice register / configuration accesses with the card attached
static struct { kern_return_t err = 0; uint8_t idx = 3; uint64_t size = 512u << 10; } BAR5;   // GetBARInfo(5)'s answer with the card (TEST values)
static kern_return_t CHAIN_ERR; static bool CHAIN_NULL, CHAIN_ERR_ARRAY; static std::vector<OSObject *> CHAIN;   // the provider chain, this service first;
// CHAIN_ERR: CopyProviderProperties fails with it (and still hands out an array when CHAIN_ERR_ARRAY)
static uint64_t CLOCK_NS;                           // time without a card
static int BARINFO5_CALLS, BARINFO5_OPENS; static uint16_t BARINFO5_CMD;   // GetBARInfo(5): calls; the device's opens and command register then

void IODelay(uint64_t us) { if (CARD) CARD->delay_us((uint32_t)us); else CLOCK_NS += us * 1000; }
void IOSleep(uint64_t ms) { if (CARD) CARD->sleep_ms((uint32_t)ms); else CLOCK_NS += ms * 1000000; }
void *IOMallocZero(size_t n) { return CARD ? CARD->alloc(n) : calloc(1, n ? n : 1); }
void IOFree(void *p, size_t n) { if (CARD) CARD->release(p, n); else free(p); }
inline void (*g_tick)() = nullptr;   // called at every clock read: a test's event at a set point inside a wait (a power-off, a Stop)
uint64_t fake_uptime_ns(clockid_t c) { if (c != CLOCK_UPTIME_RAW) BAD.push_back("clock " + std::to_string(c)); if (g_tick) g_tick();
  return CARD ? CARD->now_ns() : (CLOCK_NS += 1000); }
void fake_os_log(void *, const char *fmt, ...) {
  std::string f = fmt; for (size_t i; (i = f.find("{public}")) != std::string::npos;) f.erase(i, 8);
  char b[1024]; va_list ap; va_start(ap, fmt); vsnprintf(b, sizeof(b), f.c_str(), ap); va_end(ap);
  g_logs.push_back(b);
  // the bring-up's own messages (MI50Device::log_msg) are also the card's log, as the reference driver's prints are the virtual card's
  if (CARD && !strcmp(fmt, "tinygpu: mi50: %{public}s")) { va_start(ap, fmt); CARD->log(va_arg(ap, const char *)); va_end(ap); }
}
// MI50BarFix.kext's state on the card (MI50BarFix.cpp kStateProperty, read by the driver's wait_kext) as a script over the clock: {from_ns,
// state}, the last entry at or before now applies; "" = no property; an empty script = the kext publishes nothing. Queries for it are answered
// from here, not from CHAIN (so CHAIN_ERR / CHAIN_NULL keep meaning the driver's other queries), and counted in STATE_QUERIES, not in KEYS.
// STATE_ERR: CopyProviderProperties fails for them; a script entry "!err": it fails from then on (review fix 7). Default: the kext reported
// "placed" before the driver started.
static std::vector<std::pair<uint64_t, std::string>> KEXT_STATE = {{0, "placed"}};
static int STATE_QUERIES; static kern_return_t STATE_ERR; static bool STATE_NO_CARD, STATE_NOT_STRING;
static int FIRST_CMDW_QUERIES = -1;   // STATE_QUERIES when the card's command register was first written (-1: not written)
kern_return_t IOService::CopyProviderProperties(OSArray *keys, OSArray **props) {
  bool state_query = false;
  for (uint32_t i = 0; keys && i < keys->getCount(); i++) { OSString *s = OSDynamicCast(OSString, keys->getObject(i)); state_query |= s && s->s == "MI50BarFix State"; }
  if (state_query) {
    STATE_QUERIES++;
    if (STATE_ERR) return STATE_ERR;
    uint64_t now = CARD ? CARD->now_ns() : CLOCK_NS;
    std::string cur; for (auto &e : KEXT_STATE) if (e.first <= now) cur = e.second;
    if (cur == "!err") return kIOReturnNoMemory;
    OSDictionary *d = OSDictionary::withCapacity(4);
    uint32_t ven = 0x1002, dev = STATE_NO_CARD ? 0x14a1 : 0x66a1, cc = 0x038000;
    auto put = [&](const char *k, OSObject *o) { d->setObject(k, o); o->release(); };
    put("vendor-id", OSData::withBytes(&ven, 4)); put("device-id", OSData::withBytes(&dev, 4)); put("class-code", OSData::withBytes(&cc, 4));
    if (!cur.empty()) {   // a failed factory call (fake_os_fail_at) leaves the property out
      OSObject *v = STATE_NOT_STRING ? (OSObject *)OSData::withBytes(cur.data(), (uint32_t)cur.size()) : OSString::withCString(cur.c_str());
      if (v) put("MI50BarFix State", v);
    }
    OSArray *a = OSArray::withCapacity(1);
    if (!a) { d->release(); return kIOReturnNoMemory; }
    a->setObject(d); d->release();
    *props = a;
    return 0;
  }
  for (uint32_t i = 0; keys && i < keys->getCount(); i++) { OSString *s = OSDynamicCast(OSString, keys->getObject(i)); KEYS.push_back(s ? s->s : "?"); }
  if (CHAIN_ERR) { if (CHAIN_ERR_ARRAY) *props = OSArray::withCapacity(1); return CHAIN_ERR; }
  if (CHAIN_NULL) { *props = nullptr; return 0; }
  OSArray *a = OSArray::withCapacity((uint32_t)CHAIN.size());
  for (OSObject *o : CHAIN) {   // each dictionary cut down to the requested keys
    OSDictionary *d = OSDynamicCast(OSDictionary, o);
    if (!d) { a->setObject(o); continue; }
    OSDictionary *f = OSDictionary::withCapacity(5);
    for (auto &k : KEYS) if (OSObject *v = d->getObject(k.c_str())) f->setObject(k.c_str(), v);
    a->setObject(f); f->release();
  }
  *props = a;
  return 0;
}

// ---- IOPCIDevice ----
static bool bar5_ok(uint8_t i, uint64_t off) {
  ACCESSES++;
  bool ok = i == BAR5.idx && off % 4 == 0 && off + 4 <= BAR5.size;
  if (!ok) BAD.push_back("BAR5 access idx " + std::to_string(i) + " offset " + std::to_string(off));
  return ok;
}
kern_return_t IOPCIDevice::GetBARInfo(uint8_t bar, uint8_t *idx, uint64_t *size, uint8_t *type) {
  if (bar == 5) {   // the device's state when the driver asks for its register BAR (read directly: no access counted)
    BARINFO5_CALLS++; BARINFO5_OPENS = opens;
    if (CARD) BARINFO5_CMD = (uint16_t)(CARD->cfg[4] | CARD->cfg[5] << 8); else memcpy(&BARINFO5_CMD, cfg + 4, 2);
  }
  if (g_fail & F_BARINFO) return kIOReturnError;
  if (CARD) {
    if (bar != 5) BAD.push_back("GetBARInfo(" + std::to_string(bar) + ")");
    if (BAR5.err) return BAR5.err;
    *idx = BAR5.idx; if (size) *size = BAR5.size; if (type) *type = 0;
    return 0;
  }
  *idx = bar; if (size) *size = 1 << 20; if (type) *type = 0;
  return bar < 6 ? 0 : kIOReturnBadArgument;
}
void IOPCIDevice::MemoryRead32(uint8_t i, uint64_t off, uint32_t *v) { if (!CARD) *v = 0xffffffff; else if (bar5_ok(i, off)) *v = CARD->mmio_rd((uint32_t)(off / 4)); }
void IOPCIDevice::MemoryWrite32(uint8_t i, uint64_t off, uint32_t v) { if (CARD && bar5_ok(i, off)) CARD->mmio_wr((uint32_t)(off / 4), v); }
void IOPCIDevice::ConfigurationRead8(uint64_t o, uint8_t *v) { if (CARD) { ACCESSES++; *v = (uint8_t)CARD->cfg_rd((uint32_t)o, 1); } else *v = cfg[o]; }
void IOPCIDevice::ConfigurationRead16(uint64_t o, uint16_t *v) { if (CARD) { ACCESSES++; *v = (uint16_t)CARD->cfg_rd((uint32_t)o, 2); } else memcpy(v, cfg + o, 2); }
void IOPCIDevice::ConfigurationRead32(uint64_t o, uint32_t *v) { if (CARD) { ACCESSES++; *v = CARD->cfg_rd((uint32_t)o, 4); } else memcpy(v, cfg + o, 4); }
void IOPCIDevice::ConfigurationWrite8(uint64_t o, uint8_t v) { if (CARD) { ACCESSES++; CARD->cfg_wr((uint32_t)o, v, 1); } else cfg[o] = v; }
void IOPCIDevice::ConfigurationWrite16(uint64_t o, uint16_t v) {
  if (o == 4 && FIRST_CMDW_QUERIES < 0) FIRST_CMDW_QUERIES = STATE_QUERIES;
  if (CARD) { ACCESSES++; CARD->cfg_wr((uint32_t)o, v, 2); return; }
  if (o == 4) g_cmdw_completes = g_completes;   // the command register written: how many DMA completions came before it
  memcpy(cfg + o, &v, 2);
}
void IOPCIDevice::ConfigurationWrite32(uint64_t o, uint32_t v) { if (CARD) { ACCESSES++; CARD->cfg_wr((uint32_t)o, v, 4); } else memcpy(cfg + o, &v, 4); }

// ---- provider-chain entries: a PCI device nub (vendor-id / device-id / class-code as 4-byte data, link caps as numbers) or a non-PCI one
// (a driver, a bridge driver). IDs other than the card's and caps are TEST values. ----
[[maybe_unused]] static OSDictionary *pci_entry(uint32_t ven, uint32_t dev, int64_t cap = -1, int64_t cap2 = -1, uint32_t ven_len = 4, bool cls = true) {
  OSDictionary *d = OSDictionary::withCapacity(5);
  auto put = [&](const char *k, OSObject *o) { d->setObject(k, o); o->release(); };
  uint32_t cc = 0x060400;
  put("vendor-id", OSData::withBytes(&ven, ven_len)); put("device-id", OSData::withBytes(&dev, 4));
  if (cls) put("class-code", OSData::withBytes(&cc, 4));
  if (cap >= 0) put("IOPCIExpressLinkCapabilities", OSNumber::withNumber((uint64_t)cap, 32));
  if (cap2 >= 0) put("IOPCIExpressLinkCapabilities2", OSNumber::withNumber((uint64_t)cap2, 32));
  return d;
}
[[maybe_unused]] static OSDictionary *other_entry() { OSDictionary *d = OSDictionary::withCapacity(1); OSNumber *n = OSNumber::withNumber(1, 32); d->setObject("IOProbeScore", n); n->release(); return d; }
[[maybe_unused]] static void set_chain(std::vector<OSObject *> v) { for (OSObject *o : CHAIN) o->release(); CHAIN = v; }
