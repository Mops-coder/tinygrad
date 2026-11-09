#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <dispatch/dispatch.h>
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOMessage.h>
#include <IOKit/pwr_mgt/IOPMLib.h>
#include <mach/mach.h>
#include <pthread.h>

// Protocol

enum {
  CMD_PROBE = 0,          // probe devices, returns count
  CMD_MAP_BAR = 1,        // map PCI BAR, returns size
  CMD_MAP_SYSMEM_FD = 2,  // alloc DMA memory, returns fd via SCM_RIGHTS
  CMD_CFG_READ = 3,       // read PCI config space
  CMD_CFG_WRITE = 4,      // write PCI config space
  CMD_RESET = 5,          // reset device
  CMD_MMIO_READ = 6,      // bulk read from BAR
  CMD_MMIO_WRITE = 7,     // bulk write to BAR
  CMD_MAP_SYSMEM = 8,     // map system memory
  CMD_SYSMEM_READ = 9,    // bulk read from system memory
  CMD_SYSMEM_WRITE = 10,  // bulk write to system memory
  CMD_RESIZE_BAR = 11,    // resize bar (noop)
  CMD_UNMAP_SYSMEM = 15,  // mi50: free one MAP_SYSMEM_FD slot (arg0 = slot); number as tinygrad's RemoteCmd.UNMAP_SYSMEM
  CMD_MI50 = 128,         // mi50: one MI50 tuning operation (Leon 2026-10-16: 128, clear of upstream's sequential 0-15): arg0 = 72, payload =
                          // the dext's mi50::Mi50Req; reply = mi50::Mi50Resp (1568 bytes), forwarded to the dext's MI50 selector unchanged
  RESP_OK = 0, RESP_ERR = 1,
};

typedef struct { uint8_t cmd; uint32_t dev_id, bar; uint64_t arg0, arg1, arg2; } __attribute__((packed)) request_t;
typedef struct { uint8_t status; uint64_t resp0, resp1; } __attribute__((packed)) response_t;

// Constants and state

#define BULK_BUF_SIZE (64 << 20)
#define MAX_BARS 6
#define MAX_SYSMEM 1024   // mi50: slots are freed and reused now (UNMAP_SYSMEM); the cap bounds live host buffers, not allocations

static uint8_t g_bulk_buf[BULK_BUF_SIZE];
static io_connect_t g_conn = IO_OBJECT_NULL;
static uint32_t g_pci_id = 0;                      // mi50: vendor<<16|device of the opened tinygpu service
static io_connect_t g_bar0_conn = IO_OBJECT_NULL;  // mi50: MI50BarFix.kext user client (BAR0)
static int g_client_active = 0;
static int g_card_lock = -1;                       // mi50: flock of /tmp/tinygpu.<vvvvdddd>.lock while this server holds the card
static io_registry_entry_t g_pci_nub = IO_OBJECT_NULL;  // mi50: the IOPCIDevice of the opened card (its MI50BarFix is a child of it)
static const char *g_open_err = NULL;              // mi50: why open_tinygpu refused
static int g_named = 0;                            // mi50: the client named its card (this tree's tinygrad, which sets bus master at every open)
static int g_dext_quiesces = 0;                    // mi50: the dext accepted QuiesceOnStop: it clears bus master itself when the session ends
// mi50 (bug 4): the open session as the system-sleep callback (another thread) sees it, under g_pm_mu: its socket (-1 = none) and whether
// sleep shut that socket down; g_asleep (sleep gap, Leon 2026-10-16): from the sleep notice to the wake's kIOMessageSystemHasPoweredOn
static pthread_mutex_t g_pm_mu = PTHREAD_MUTEX_INITIALIZER;
static int g_session_fd = -1, g_session_cut = 0, g_asleep = 0;
static io_connect_t g_pm = IO_OBJECT_NULL;         // mi50 (bug 4): IORegisterForSystemPower's root-domain connection

static struct { mach_vm_address_t addr; mach_vm_size_t size; } g_bars[MAX_BARS];
// mi50: one entry per live host buffer; the shm name and descriptor are gone once the client holds its own descriptor
static struct { mach_vm_address_t addr; mach_vm_size_t size; uint64_t iova; int used; } g_sysmem[MAX_SYSMEM];

// Utilities

static int recvall(int fd, void *buf, size_t len) {   // mi50: 0 = all len bytes read, -1 = the client went away first
  for (size_t off = 0; off < len; ) {
    ssize_t r = recv(fd, (uint8_t*)buf + off, len - off, 0);
    if (r <= 0) return -1;
    off += r;
  }
  return 0;
}
static int sendall(int fd, const void *buf, size_t len) {   // mi50: 0 = all len bytes sent, -1 = the client went away first
  for (size_t off = 0; off < len; ) {
    ssize_t r = send(fd, (const uint8_t*)buf + off, len - off, 0);
    if (r <= 0) return -1;
    off += r;
  }
  return 0;
}

// MMIO requires 32-bit aligned volatile accesses
static void mmio_copy(void *dst, void *src, size_t len) {
  volatile uint32_t *d = dst, *s = src;
  for (size_t i = 0; i < len / 4; i++) d[i] = s[i];
  for (size_t i = len & ~3; i < len; i++) ((volatile uint8_t*)dst)[i] = ((volatile uint8_t*)src)[i];
}

static int send_response(int fd, response_t *resp, int send_fd) {
  char cmsgbuf[CMSG_SPACE(sizeof(int))];
  struct iovec iov = {resp, sizeof(*resp)};
  struct msghdr msg = {.msg_iov = &iov, .msg_iovlen = 1};

  if (send_fd >= 0) {
    msg.msg_control = cmsgbuf;
    msg.msg_controllen = sizeof(cmsgbuf);
    struct cmsghdr *cmsg = CMSG_FIRSTHDR(&msg);
    *cmsg = (struct cmsghdr){.cmsg_level = SOL_SOCKET, .cmsg_type = SCM_RIGHTS, .cmsg_len = CMSG_LEN(sizeof(int))};
    memcpy(CMSG_DATA(cmsg), &send_fd, sizeof(int));
  }
  return sendmsg(fd, &msg, 0) > 0 ? 0 : -1;
}

static void send_error(int fd, const char *msg) {
  response_t resp = {.status = RESP_ERR, .resp0 = strlen(msg)};
  send_response(fd, &resp, -1);
  send(fd, msg, strlen(msg), 0);
}

// Driver interface

static void on_disconnect(void *refcon, io_service_t svc, uint32_t msg, void *arg) {
  if (msg == kIOMessageServiceIsTerminated) _exit(0);
}

static int session_cut(void) { pthread_mutex_lock(&g_pm_mu); int c = g_session_cut; pthread_mutex_unlock(&g_pm_mu); return c; }
static int session_open(void) { pthread_mutex_lock(&g_pm_mu); int o = g_session_fd >= 0; pthread_mutex_unlock(&g_pm_mu); return o; }
static int asleep(void) { pthread_mutex_lock(&g_pm_mu); int a = g_asleep; pthread_mutex_unlock(&g_pm_mu); return a; }
// review fix 2 (Leon 2026-10-17): MI50BarFix.kext marks the card asleep from the system's sleep notice to the wake (MI50BarFix.cpp
// kSleepProperty, a kernel registration that predates every server), so a server started after its own sleep notice still holds the session;
// a card without the kext has no mark
static int card_asleep(void) {
  if (g_pci_nub == IO_OBJECT_NULL) return 0;
  CFTypeRef v = IORegistryEntryCreateCFProperty(g_pci_nub, CFSTR("MI50BarFix Sleep"), kCFAllocatorDefault, 0);
  int a = v && CFGetTypeID(v) == CFBooleanGetTypeID() && CFBooleanGetValue((CFBooleanRef)v);
  if (v) CFRelease(v);
  return a;
}

// mi50 (bug 4, Leon 2026-10-16): no session runs across system sleep. kIOMessageSystemWillSleep is delivered before any hardware is powered
// off and cannot be aborted; the kernel waits 30 s at most for its acknowledgement (IOPMLib.h IORegisterForSystemPower). The open session's
// socket is shut down - handle_client ends the session after the current request and cleanup() releases the card (DMA stopped, mappings
// released) - and the sleep is allowed once run_server has seen the session end. kIOMessageCanSystemSleep is allowed at once; the other
// messages take no acknowledgement (IOPMLib.h). Sleep gap (Leon 2026-10-16): the sleep notice also marks the server asleep until the wake's
// kIOMessageSystemHasPoweredOn - a session that starts after this acknowledgement but before the card powers down gets no notice of its own,
// so handle_client holds every new session while the mark is set (the sleep cannot be aborted after this notice, IOPMLib.h)
static void on_power(void *refcon, io_service_t svc, uint32_t msg, void *arg) {
  if (msg == kIOMessageCanSystemSleep) { IOAllowPowerChange(g_pm, (intptr_t)arg); return; }
  if (msg == kIOMessageSystemHasPoweredOn) { pthread_mutex_lock(&g_pm_mu); g_asleep = 0; pthread_mutex_unlock(&g_pm_mu); return; }
  if (msg != kIOMessageSystemWillSleep) return;
  pthread_mutex_lock(&g_pm_mu);
  g_asleep = 1;
  int open = g_session_fd >= 0;
  if (open) { g_session_cut = 1; shutdown(g_session_fd, SHUT_RDWR); }
  pthread_mutex_unlock(&g_pm_mu);
  if (open) printf("system sleep: ending the session\n");
  for (int i = 0; open && i < 3000; i++) { usleep(10000); open = session_open(); }   // 30 s at most, the kernel's own limit
  IOAllowPowerChange(g_pm, (intptr_t)arg);
}

// mi50: read a 32-bit little-endian OSData property (vendor-id / device-id) of a registry entry; 0 if absent
static uint32_t read_u32_prop(io_registry_entry_t ent, const char *key) {
  uint32_t val = 0;
  CFStringRef k = CFStringCreateWithCString(NULL, key, kCFStringEncodingUTF8);
  CFTypeRef prop = IORegistryEntryCreateCFProperty(ent, k, kCFAllocatorDefault, 0);
  CFRelease(k);
  if (prop) {
    if (CFGetTypeID(prop) == CFDataGetTypeID() && CFDataGetLength(prop) >= 4) CFDataGetBytes(prop, CFRangeMake(0, 4), (UInt8*)&val);
    CFRelease(prop);
  }
  return val;
}

// mi50: the PCI identity (vendor<<16|device) of the IOPCIDevice that provides a tinygpu service
static uint32_t service_pci_id(io_service_t svc) {
  io_registry_entry_t parent = IO_OBJECT_NULL;
  if (IORegistryEntryGetParentEntry(svc, kIOServicePlane, &parent) != KERN_SUCCESS) return 0;
  uint32_t id = (read_u32_prop(parent, "vendor-id") << 16) | (read_u32_prop(parent, "device-id") & 0xffff);
  IOObjectRelease(parent);
  return id;
}

// mi50: a lock file in a shared directory (/tmp): never through a symlink, and only a regular file of this user (another account could
// plant either one); -1 with errno set when refused
static int open_lock(const char *path, mode_t mode) {
  int fd = open(path, O_RDONLY | O_CREAT | O_CLOEXEC | O_NOFOLLOW, mode);
  struct stat st;
  if (fd >= 0 && (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) || st.st_uid != getuid())) {
    fprintf(stderr, "lock file %s is not a regular file of this user: refused\n", path);
    close(fd); errno = EPERM; return -1;
  }
  return fd;
}

// mi50: open the tinygpu service of the card the client named by dev_id (vendor<<16|device); unknown id -> first service
static io_connect_t open_tinygpu(uint32_t dev_id) {
  static io_object_t notif;
  static IONotificationPortRef port;
  g_open_err = "Driver not available. Check: System Report > PCI for GPU, System Settings > Privacy & Security.";
  io_iterator_t it = IO_OBJECT_NULL;
  if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceNameMatching("tinygpu"), &it) != KERN_SUCCESS) return IO_OBJECT_NULL;
  io_service_t svc = IO_OBJECT_NULL, first = IO_OBJECT_NULL, cand;
  int n_mi50 = 0;
  while ((cand = IOIteratorNext(it))) {
    uint32_t id = service_pci_id(cand);
    // mi50: a stock client (dev_id 0) gets stock TinyGPU's first card - the first service - leaving out the MI50, the one card stock tinygrad
    // cannot drive (its card list, ops_amd.py:898, has no 66a1); with no MI50 attached this is stock's choice exactly (review finding 20)
    if (id == 0x100266a1) n_mi50++;
    if (!first && id != 0x100266a1) { first = cand; IOObjectRetain(first); }
    if (dev_id && id == dev_id && !svc) { svc = cand; IOObjectRetain(svc); }
    IOObjectRelease(cand);
  }
  IOObjectRelease(it);
  if (!dev_id && !first && n_mi50) {
    fprintf(stderr, "dev_id 0 with only the MI50 attached: refused\n");
    g_open_err = "Stock tinygrad has no MI50 support: open the MI50 by its PCI ID (Device[\"1002:66a1\"]) with the tinygrad that has it.";
    return IO_OBJECT_NULL;
  }
  // mi50: a client that names a card (dev_id != 0) gets that card or nothing; only the stock client (dev_id 0, bus "usb4") gets the first card
  if (!svc && dev_id) {
    if (first) IOObjectRelease(first);
    fprintf(stderr, "dev_id 0x%08x not attached\n", dev_id);
    g_open_err = "The named card is not attached.";
    return IO_OBJECT_NULL;
  }
  if (!svc) { svc = first; first = IO_OBJECT_NULL; }
  if (first) IOObjectRelease(first);
  if (!svc) return IO_OBJECT_NULL;
  uint32_t pci_id = service_pci_id(svc);
  printf("opening tinygpu service for PCI %04x:%04x\n", pci_id >> 16, pci_id & 0xffff);

  // mi50: one TinyGPU server per card, whatever socket path or TMPDIR its client came from (the dext hands out any number of user clients)
  char lp[48];
  snprintf(lp, sizeof(lp), "/tmp/tinygpu.%08x.lock", pci_id);
  int lk = open_lock(lp, 0644);
  if (lk < 0 || flock(lk, LOCK_EX | LOCK_NB) < 0) {
    if (lk >= 0) close(lk);
    IOObjectRelease(svc);
    g_open_err = "The card is held by another TinyGPU server.";
    return IO_OBJECT_NULL;
  }

  // mi50: the termination notification follows the card actually opened
  if (!port) {
    port = IONotificationPortCreate(kIOMainPortDefault);
    IONotificationPortSetDispatchQueue(port, dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_HIGH, 0));
  }
  if (notif) { IOObjectRelease(notif); notif = IO_OBJECT_NULL; }
  IOServiceAddInterestNotification(port, svc, kIOGeneralInterest, on_disconnect, NULL, &notif);

  g_pci_id = pci_id;
  if (IORegistryEntryGetParentEntry(svc, kIOServicePlane, &g_pci_nub) != KERN_SUCCESS) g_pci_nub = IO_OBJECT_NULL;
  io_connect_t conn;
  kern_return_t kr = IOServiceOpen(svc, mach_task_self(), 0, &conn);
  IOObjectRelease(svc);
  if (kr != KERN_SUCCESS) { close(lk); return IO_OBJECT_NULL; }
  g_card_lock = lk;
  g_named = dev_id != 0;
  return conn;
}

static int dext_rpc(uint32_t sel, uint64_t *in, uint32_t in_cnt, uint64_t *out_val) {
  uint64_t out[2];
  uint32_t out_cnt = 2;
  if (IOConnectCallMethod(g_conn, sel, in, in_cnt, NULL, 0, out, &out_cnt, NULL, NULL) != KERN_SUCCESS) return -1;
  if (out_val) *out_val = out[0];
  return 0;
}

// mi50 (bug 1): is the MI50 up? The MI50 selector's OP_SMU_VERSION (17) reads a value the driver keeps, no card access; while the driver's
// bring-up has failed the driver answers every MI50 operation with the failure's reason (TinyGPUDriver.cpp run_op, mi50::Mi50Resp: status
// at 0, err at 8, 504 bytes). 1 = up, 0 = down (why = the driver's reason), 2 = powered down (bug 4: status 3, why = the driver's text),
// -1 = the driver did not answer
static int mi50_ready(char *why, size_t n) {
  enum { MI50_REQ = 72, MI50_RESP = 1568, MI50_SEL = 6, OP_SMU_VERSION = 17 };
  uint8_t q[MI50_REQ] = {0}, r[MI50_RESP];
  uint32_t op = OP_SMU_VERSION;
  memcpy(q, &op, sizeof(op));
  size_t sz = sizeof(r);
  if (IOConnectCallStructMethod(g_conn, MI50_SEL, q, sizeof(q), r, &sz) != KERN_SUCCESS || sz != sizeof(r)) return -1;
  int32_t status;
  memcpy(&status, r, sizeof(status));
  if (!status) return 1;
  snprintf(why, n, "%.*s", 504, (const char *)r + 8);
  return status == 3 ? 2 : 0;
}

// mi50 (bug 1): a driver call refused because the MI50's bring-up failed - or, bug 4, while it is powered down - answers with the driver's
// reason instead of a bare failure. 1 = e holds "MI50 not initialised: <reason>" / "MI50 powered down: <reason>"; 0 = not the MI50, or its
// driver reports it up (the call failed for another reason)
static int mi50_refusal(char *e, size_t n) {
  char why[512] = "";
  int up = g_pci_id == 0x100266a1 ? mi50_ready(why, sizeof(why)) : 1;
  if (up != 0 && up != 2) return 0;
  snprintf(e, n, "MI50 %s: %s", up == 2 ? "powered down" : "not initialised", why);
  return 1;
}

static int map_bar(uint32_t bar, response_t *resp) {
  if (bar >= MAX_BARS) return -1;
  // mi50: macOS leaves the MI50's BAR0 unassigned; MI50BarFix.kext places it and maps it (memory type 0), the dext the rest
  io_connect_t conn = g_conn;
  if (bar == 0 && g_pci_id == 0x100266a1) {
    if (g_bar0_conn == IO_OBJECT_NULL) {   // mi50: the MI50BarFix attached to this card's IOPCIDevice, not the first one by class
      io_iterator_t it = IO_OBJECT_NULL;
      io_service_t svc = IO_OBJECT_NULL, child;
      if (!g_pci_nub || IORegistryEntryGetChildIterator(g_pci_nub, kIOServicePlane, &it) != KERN_SUCCESS) return -1;
      while ((child = IOIteratorNext(it))) {
        if (!svc && IOObjectConformsTo(child, "MI50BarFix")) svc = child; else IOObjectRelease(child);
      }
      IOObjectRelease(it);
      if (!svc) return -1;
      kern_return_t kr = IOServiceOpen(svc, mach_task_self(), 0, &g_bar0_conn);
      IOObjectRelease(svc);
      if (kr != KERN_SUCCESS) { g_bar0_conn = IO_OBJECT_NULL; return -1; }
    }
    conn = g_bar0_conn;
  }
  if (!g_bars[bar].addr && IOConnectMapMemory64(conn, bar, mach_task_self(), &g_bars[bar].addr, &g_bars[bar].size, kIOMapAnywhere)) return -1;
  resp->resp0 = g_bars[bar].addr;
  resp->resp1 = g_bars[bar].size;
  return 0;
}

static int map_sysmem_fd(uint64_t size, int contiguous, response_t *resp, int *out_fd, char shm_name[32]) {
  int idx = -1;
  for (int i = 0; i < MAX_SYSMEM && idx < 0; i++) if (!g_sysmem[i].used) idx = i;   // mi50: reuse freed slots
  if (idx < 0) return -1;
  int fd = -1;
  void *ptr = MAP_FAILED;

  // page-align, min 16KB for IOMemoryDescriptor
  size_t alloc_sz = (size + 0xfff) & ~0xfff;
  if (alloc_sz < 0x4000) alloc_sz = 0x4000;

  snprintf(shm_name, 32, "/tinygpu_%d_%d", (int)getpid(), idx);   // mi50: one server per card: names must not collide
  shm_unlink(shm_name);
  if ((fd = shm_open(shm_name, O_CREAT | O_EXCL | O_RDWR, 0600)) < 0) goto fail;
  if (ftruncate(fd, alloc_sz) < 0) goto fail;
  if ((ptr = mmap(NULL, alloc_sz, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0)) == MAP_FAILED) goto fail;

  // PrepareDMA writes physical addresses to output buffer, copy to shared mem
  uint8_t paddr_buf[8192] = {0};
  size_t out_sz = sizeof(paddr_buf);
  if (IOConnectCallStructMethod(g_conn, 3, ptr, alloc_sz, paddr_buf, &out_sz) != KERN_SUCCESS) goto fail;
  memcpy(ptr, paddr_buf, out_sz);

  uint64_t iova;   // first DMA address = the dext's key for ReleaseDMA (memcpy: paddr_buf is a byte array, no aligned uint64_t read)
  memcpy(&iova, paddr_buf, sizeof(iova));
  g_sysmem[idx] = (typeof(g_sysmem[idx])){.addr = (mach_vm_address_t)ptr, .size = alloc_sz, .iova = iova, .used = 1};

  *resp = (response_t){.resp0 = alloc_sz, .resp1 = idx};
  *out_fd = fd;
  return 0;

fail:
  if (ptr != MAP_FAILED) munmap(ptr, alloc_sz);
  if (fd >= 0) close(fd);
  shm_unlink(shm_name);
  return -1;
}

// mi50: the card stops mapping the buffer (dext ReleaseDMA, by its first DMA address), then the server's view goes and the slot is free
static int unmap_sysmem(uint64_t idx) {
  if (idx >= MAX_SYSMEM || !g_sysmem[idx].used) return -1;
  uint64_t in[1] = {g_sysmem[idx].iova};
  if (dext_rpc(4, in, 1, NULL)) return -1;   // TinyGPURPC::ReleaseDMA; a refusal keeps the buffer mapped and the slot used
  munmap((void*)g_sysmem[idx].addr, g_sysmem[idx].size);
  g_sysmem[idx].used = 0;
  return 0;
}

static int validate_bar(uint32_t bar, uint64_t off, uint64_t sz) {   // mi50: full 32-bit index, overflow-free bounds
  return (bar < MAX_BARS && g_bars[bar].addr && sz <= BULK_BUF_SIZE && sz <= g_bars[bar].size && off <= g_bars[bar].size - sz) ? 0 : -1;
}

static void cleanup(void) {
  // mi50: a session of a client that named its card ends with bus master cleared, before the host memory below is unmapped and the dext
  // releases its DMA mappings: a killed model's queues stop DMA first (IOPCIFamily's quiesce does the same). That client sets bus master
  // again at every open (full and partial boot); a stock client (dev_id 0) does not on a partial boot, so its sessions are left as they were.
  // Done here only for a dext that refused QuiesceOnStop (an older build); otherwise the dext does it when the connection below closes
  if (g_conn != IO_OBJECT_NULL && g_named && !g_dext_quiesces) {
    uint64_t rd[2] = {4, 2}, cmd = 0;   // PCI_COMMAND, 2 bytes
    if (!dext_rpc(0, rd, 2, &cmd) && cmd != 0xffff && (cmd & 0x4)) { uint64_t wr[3] = {4, 2, cmd & ~0x4ull}; dext_rpc(1, wr, 3, NULL); }
  }
  g_named = 0; g_dext_quiesces = 0;
  for (int i = 0; i < MAX_BARS; i++)
    if (g_bars[i].addr) { IOConnectUnmapMemory64(i == 0 && g_bar0_conn ? g_bar0_conn : g_conn, i, mach_task_self(), g_bars[i].addr); g_bars[i].addr = 0; }
  if (g_bar0_conn != IO_OBJECT_NULL) { IOServiceClose(g_bar0_conn); g_bar0_conn = IO_OBJECT_NULL; }

  for (int i = 0; i < MAX_SYSMEM; i++)   // the dext releases every DMA mapping of the client when its user client closes (below)
    if (g_sysmem[i].used) { munmap((void*)g_sysmem[i].addr, g_sysmem[i].size); g_sysmem[i].used = 0; }

  if (g_conn != IO_OBJECT_NULL) { IOServiceClose(g_conn); g_conn = IO_OBJECT_NULL; }
  if (g_pci_nub != IO_OBJECT_NULL) { IOObjectRelease(g_pci_nub); g_pci_nub = IO_OBJECT_NULL; }
  if (g_card_lock >= 0) { close(g_card_lock); g_card_lock = -1; }   // mi50: the card is free for the next server
}

static void handle_client(int fd) {
  int bufsize = BULK_BUF_SIZE;
  setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &bufsize, sizeof(bufsize));
  setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bufsize, sizeof(bufsize));
  printf("client connected\n");

  // mi50: the device is chosen by the first request's dev_id (vendor<<16|device); stock opened the first service here
  request_t req;
  if (recv(fd, &req, sizeof(req), MSG_WAITALL) != sizeof(req)) return;
  g_conn = open_tinygpu(req.dev_id);
  if (g_conn == IO_OBJECT_NULL) {
    fprintf(stderr, "failed to connect to tinygpu driver\n");
    send_error(fd, g_open_err);
    cleanup();   // mi50: a refused open still releases what it took (lock, registry references)
    return;
  }
  // mi50 (sleep gap, Leon 2026-10-16): a session that starts between the sleep notice and the wake waits for the wake, every card, polled every
  // 100 ms within the 60 s it shares with the MI50's powered-down wait below; still asleep then -> refused. The MI50 also by its card's mark
  // (review fix 2), for a server started after its own sleep notice
  int waited = 0;
  for (; (asleep() || card_asleep()) && waited < 600 && !session_cut(); waited++) usleep(100000);
  if (asleep() || card_asleep()) {
    fprintf(stderr, "system sleep in progress: session refused\n");
    send_error(fd, "System sleep in progress: the card is not usable until the Mac has woken.");
    cleanup();
    return;
  }
  // mi50 (bug 1): a session on the MI50 starts on a card the driver brought up. After a failed bring-up the driver refuses every call but
  // RESET, which tinygrad only sends after reading config space and mapping BARs: here the server sends that one RESET (the driver's
  // bring-up again, POST and power management first) and, if the card is still not up, refuses the session with the driver's reason
  if (g_named && g_pci_id == 0x100266a1) {
    char why[512] = "";
    int up = mi50_ready(why, sizeof(why));
    // bug 4 (Leon 2026-10-16): powered down - the wake's bring-up is waited for, polled every 100 ms, 60 s at most, unless sleep ends this session
    for (int i = waited; up == 2 && i < 600 && !session_cut(); i++) { usleep(100000); up = mi50_ready(why, sizeof(why)); }
    if (up == 0) { dext_rpc(2, NULL, 0, NULL); up = mi50_ready(why, sizeof(why)); }   // TinyGPURPC::Reset; its answer is the check after it
    if (up != 1) {
      char e[600];
      snprintf(e, sizeof(e), "MI50 %s: %s", up == 2 ? "powered down" : "not initialised",
               up < 0 ? "the TinyGPU driver did not answer the MI50 check" : why);
      fprintf(stderr, "%s\n", e);
      send_error(fd, e);
      cleanup();
      return;
    }
  }
  // mi50: a named session ends with the card's DMA stopped before its mappings are released; the dext does it in its user-client Stop
  // (TinyGPURPC::QuiesceOnStop), which also covers a killed or crashed server. A dext without the selector refuses it: cleanup() does it
  g_dext_quiesces = g_named && !dext_rpc(5, NULL, 0, NULL);

  response_t resp;
  int have_req = 1;
  while (have_req || recv(fd, &req, sizeof(req), MSG_WAITALL) == sizeof(req)) {
    have_req = 0;
    resp = (response_t){0};
    int drv = 0;    // mi50 (bug 1): the reply comes from a driver call - refused on the MI50, it carries the driver's reason (mi50_refusal)
    char e[600];

    switch (req.cmd) {
    case CMD_MAP_BAR:
      resp.status = map_bar(req.bar, &resp) ? 1 : 0;
      drv = !(req.bar == 0 && g_pci_id == 0x100266a1);   // the MI50's BAR0 comes from MI50BarFix.kext, not the driver
      break;

    case CMD_MAP_SYSMEM_FD: {
      int shm_fd = -1;
      char shm_name[32] = {0};
      resp.status = map_sysmem_fd(req.arg0, (int)req.arg1, &resp, &shm_fd, shm_name) ? 1 : 0;
      if (resp.status && mi50_refusal(e, sizeof(e))) send_error(fd, e); else send_response(fd, &resp, shm_fd);
      // mi50: the client now holds its own descriptor; the server keeps only its mapping, so no descriptor and no global name lingers
      if (shm_fd >= 0) { close(shm_fd); shm_unlink(shm_name); }
      continue;
    }

    case CMD_UNMAP_SYSMEM:
      resp.status = unmap_sysmem(req.arg0) ? 1 : 0;
      drv = 1;
      break;

    case CMD_MI50: {   // mi50: the payload is read first (the stream stays in step), then handed to TinyGPURPC::MI50 (6) for the MI50 only
      enum { MI50_REQ = 72, MI50_RESP = 1568, MI50_SEL = 6 };   // TinyGPUDriver.cpp mi50::Mi50Req / Mi50Resp, TinyGPUDriverUserClient.iig
      if (req.arg0 != MI50_REQ) { fprintf(stderr, "bad MI50 request length %llu\n", (unsigned long long)req.arg0); goto done; }
      if (recvall(fd, g_bulk_buf, MI50_REQ)) goto done;
      if (g_pci_id != 0x100266a1) { send_error(fd, "not a MI50: no MI50 tuning on this card"); continue; }
      size_t out_sz = MI50_RESP;
      kern_return_t kr = IOConnectCallStructMethod(g_conn, MI50_SEL, g_bulk_buf, MI50_REQ, g_bulk_buf + MI50_REQ, &out_sz);
      if (kr != KERN_SUCCESS || out_sz != MI50_RESP) {
        char e[96]; snprintf(e, sizeof(e), "MI50 call failed (0x%08x, %zu bytes)", kr, out_sz);
        send_error(fd, e); continue;
      }
      send_response(fd, &resp, -1);
      if (sendall(fd, g_bulk_buf + MI50_REQ, MI50_RESP)) goto done;   // a short reply would leave the client's stream out of step
      continue;
    }

    case CMD_CFG_READ: {
      uint64_t in[2] = {req.arg0, req.arg1}, val = 0;  // resp is packed: no pointer into it
      resp.status = dext_rpc(0, in, 2, &val) ? 1 : 0;
      resp.resp0 = val;
      drv = 1;
      break;
    }

    case CMD_CFG_WRITE: {
      uint64_t in[3] = {req.arg0, req.arg1, req.arg2};
      resp.status = dext_rpc(1, in, 3, NULL) ? 1 : 0;
      drv = 1;
      break;
    }

    case CMD_RESIZE_BAR:
      break;

    case CMD_RESET:
      resp.status = dext_rpc(2, NULL, 0, NULL) ? 1 : 0;
      drv = 1;
      break;

    case CMD_MMIO_READ:
      if (validate_bar(req.bar, req.arg0, req.arg1)) { resp.status = 1; break; }
      mmio_copy(g_bulk_buf, (void*)(g_bars[req.bar].addr + req.arg0), req.arg1);
      resp.resp0 = req.arg1;
      send_response(fd, &resp, -1);
      send(fd, g_bulk_buf, req.arg1, 0);
      continue;

    case CMD_MMIO_WRITE:   // mi50: checked before its payload is read; a bad request or a short payload ends the session (the stream is lost)
      if (validate_bar(req.bar, req.arg0, req.arg1)) { fprintf(stderr, "bad MMIO_WRITE bar %u off 0x%llx size 0x%llx\n", req.bar,
                                                                  (unsigned long long)req.arg0, (unsigned long long)req.arg1); goto done; }
      if (recvall(fd, g_bulk_buf, req.arg1)) goto done;
      mmio_copy((void*)(g_bars[req.bar].addr + req.arg0), g_bulk_buf, req.arg1);
      continue;

    default:   // mi50: an unknown command may carry a payload of unknown length (PROBE, SYSMEM_WRITE, a newer client's LOAD_PROG); reading on
               // would take its bytes as requests (config / BAR writes to the card) - the session ends instead
      fprintf(stderr, "unknown command %u: session ended\n", req.cmd);
      goto done;
    }
    if (resp.status && drv && mi50_refusal(e, sizeof(e))) send_error(fd, e); else send_response(fd, &resp, -1);
  }

done:
  printf("client disconnected\n");
  cleanup();
}

int run_server(const char *sock_path) {
  signal(SIGPIPE, SIG_IGN);   // mi50: a client that died mid-reply ends its session (EPIPE), not the server without cleanup

  // mi50: one live server per socket path: a second one (spawned when the first one's backlog was full) must not unlink and steal it
  char lock_path[sizeof(((struct sockaddr_un*)0)->sun_path) + 8];
  snprintf(lock_path, sizeof(lock_path), "%s.lock", sock_path);
  int path_lock = open_lock(lock_path, 0600);
  if (path_lock < 0 || flock(path_lock, LOCK_EX | LOCK_NB) < 0) { fprintf(stderr, "a server already runs on %s\n", sock_path); return 1; }

  int server_fd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (server_fd < 0) { perror("socket"); return 1; }

  umask(077);   // mi50: the socket (and every file this server creates) is this user's alone; getpeereid below checks each client
  struct sockaddr_un addr = {.sun_family = AF_UNIX};
  strncpy(addr.sun_path, sock_path, sizeof(addr.sun_path) - 1);
  unlink(sock_path);

  if (bind(server_fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) { perror("bind"); close(server_fd); return 1; }
  if (listen(server_fd, 1) < 0) { perror("listen"); close(server_fd); return 1; }
  printf("listening on %s\n", sock_path);

  IONotificationPortRef pm_port = NULL;   // mi50 (bug 4): system sleep ends the open session (on_power)
  io_object_t pm_notifier = IO_OBJECT_NULL;
  g_pm = IORegisterForSystemPower(NULL, &pm_port, on_power, &pm_notifier);
  if (g_pm) IONotificationPortSetDispatchQueue(pm_port, dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_HIGH, 0));
  else fprintf(stderr, "no system sleep notifications (IORegisterForSystemPower failed): a session is not ended before sleep\n");

  while (1) {
    int client_fd = accept(server_fd, NULL, NULL);
    if (client_fd < 0) { if (errno == EINTR) continue; perror("accept"); break; }
    uid_t peer_uid; gid_t peer_gid;   // mi50: only this user's processes drive the card
    if (getpeereid(client_fd, &peer_uid, &peer_gid) != 0 || peer_uid != getuid()) { fprintf(stderr, "rejected: client of another user\n"); close(client_fd); continue; }
    if (g_client_active) { printf("rejected: client already connected\n"); close(client_fd); continue; }
    g_client_active = 1;
    pthread_mutex_lock(&g_pm_mu); g_session_fd = client_fd; g_session_cut = 0; pthread_mutex_unlock(&g_pm_mu);
    handle_client(client_fd);
    pthread_mutex_lock(&g_pm_mu); g_session_fd = -1; pthread_mutex_unlock(&g_pm_mu);   // cleanup() has run: on_power may allow the sleep
    g_client_active = 0;
    close(client_fd);
  }

  close(server_fd);
  unlink(sock_path);
  cleanup();
  return 0;
}
