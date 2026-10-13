#!/usr/bin/env python3
"""Design v2 §3 "_rpc lock" / §7.0 rule 2: offline concurrency check of RemotePCIDevice's per-device socket lock. Needs no card, no server.

A FakeSock stands in for the TinyGPU server socket. It replies like the server (17-byte '<BQQ' status/resp0/resp1, then the readout for
MMIO_READ, an SCM_RIGHTS-style recvmsg for MAP_SYSMEM_FD, a paddr blob after MAP_SYSMEM; nothing for MMIO_WRITE) and sleeps between request
and reply so a second thread has every chance to cut in. It detects two things a missing lock produces: (a) a request sent while another
thread's exchange is still open (interleaved bytes on the wire), (b) a reply consumed by a thread that did not send the request. Every reply
carries a tag derived from its request, so a cross-delivered reply is also caught by the callers' value checks.
Threads: "main" does _bulk_read / _bulk_write / read_config / write_config / _rpc(has_fd=True) / alloc_sysmem (Linux-path two-step);
"watchdog" reads config dword 0 and MMIO dword 0x1665f (the §1.7 THM temperature dword) in a loop, like the §7.0 watchdog thread.
Then the negative control: the same run with the lock replaced by a no-op MUST be caught (proves the check fails when the lock is absent).
Also: APLRemotePCIDevice.ensure_app with TINYGPU_FORK=1 returns (APP_PATH present) / raises (absent) without calling pkill/ditto/fetch, and without
the flag the upstream path is still taken (its first action, `system("pkill -f TinyGPU")`, is intercepted - nothing is executed).
Run: PYTHONPATH=<fork> python3 extra/mi50/rpc_lock_check.py
"""
import contextlib, os, socket, struct, sys, tempfile, threading, time, random
import tinygrad.runtime.support.system as sysmod
from tinygrad.runtime.support.system import RemotePCIDevice, RemoteCmd, APLRemotePCIDevice, System
from tinygrad.helpers import getenv

fails: list[str] = []
def check(cond, msg):
  print(("ok   " if cond else "FAIL ") + msg)
  if not cond: fails.append(msg)

HDR = struct.Struct('<BIIQQQ')   # client request header (system.py _rpc_nolock / _bulk_write)
class Interleave(AssertionError): pass
class FakeSock:
  """One request at a time, like server.c's blocking handle_client loop; raises Interleave on a wire-level race."""
  def __init__(self, latency=0.0005):
    self.owner: int|None = None; self.pending = b''; self.pending_fd: int|None = None; self.latency = latency; self.n = 0
    self.violations: list[str] = []
  def getpeername(self): return ("fake", 0)
  def setsockopt(self, *a): pass
  def _violate(self, msg):
    self.violations.append(msg); raise Interleave(msg)
  def sendall(self, data: bytes):
    me = threading.get_ident()
    if self.owner is not None and self.owner != me: self._violate(f"request from thread {me} while thread {self.owner}'s exchange is open")
    if self.pending: self._violate("request sent before the previous reply was fully consumed")
    cmd, dev_id, bar, a0, a1, a2 = HDR.unpack(data[:HDR.size]); payload = data[HDR.size:]; self.n += 1
    tag = (a0 * 2654435761 + cmd * 97 + bar) & 0xffffffffffff
    if cmd == RemoteCmd.MMIO_WRITE:   # bulk write: header + data, no reply (client does not read one)
      assert len(payload) == a1, "bulk write payload length != header"; return
    if cmd in (RemoteCmd.CFG_READ,): reply = struct.pack('<BQQ', 0, tag, 0)
    elif cmd == RemoteCmd.CFG_WRITE: reply = struct.pack('<BQQ', 0, 0, 0)
    elif cmd == RemoteCmd.MMIO_READ: reply = struct.pack('<BQQ', 0, 0, 0) + bytes((tag + i) & 0xff for i in range(a1))   # readout a1 bytes
    elif cmd == RemoteCmd.MAP_SYSMEM_FD: reply = struct.pack('<BQQ', 0, 0x1000, 0); self.pending_fd = tag & 0x7fffffff  # fd via ancillary data
    elif cmd == RemoteCmd.MAP_SYSMEM: reply = struct.pack('<BQQ', 0, 16, 0) + struct.pack('<QQ', tag, tag ^ 0xffff)         # two paddrs
    else: raise AssertionError(f"unexpected cmd {cmd}")
    self.owner, self.pending = me, reply
    time.sleep(self.latency * random.random())   # the server's turnaround: this is the window a second thread would cut into
  def _take(self, n):
    me = threading.get_ident()
    if self.owner != me: self._violate(f"thread {me} reads a reply owned by thread {self.owner}")
    time.sleep(self.latency * random.random())
    out, self.pending = self.pending[:n], self.pending[n:]
    if not self.pending: self.owner = None
    return out
  def recv(self, n): return self._take(n)
  def recvmsg(self, n, anc_len):
    msg = self._take(n); fd, self.pending_fd = self.pending_fd, None
    return msg, [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack('<i', fd))], 0, None

def tag_of(cmd, bar, a0): return (a0 * 2654435761 + cmd * 97 + bar) & 0xffffffffffff

def make_dev(sock):
  System.flock_acquire = lambda name: -1   # System is the _System instance; no lock-file side effect, the real __init__ (dev_id + RLock) still runs
  return RemotePCIDevice("AM", "1002:66a1", sock=sock)

def run(dev, iters, stop_on_error):
  errors: list[str] = []
  def guard(fn):
    def w():
      try: fn()
      except Exception as e: errors.append(f"{threading.current_thread().name}: {type(e).__name__}: {e}")
    return w
  def main():
    for i in range(iters):
      if stop_on_error and errors: return
      off = (i * 4) & 0xffff
      data = dev._bulk_read(RemoteCmd.MMIO_READ, 5, off, 16)
      if data != bytes((tag_of(RemoteCmd.MMIO_READ, 5, off) + k) & 0xff for k in range(16)): errors.append(f"main: MMIO_READ {off:#x} got foreign data")
      dev._bulk_write(RemoteCmd.MMIO_WRITE, 5, off, b'\x11' * 8)
      if (v := dev.read_config(off & 0xfc, 4)) != tag_of(RemoteCmd.CFG_READ, 0, off & 0xfc): errors.append(f"main: CFG_READ {off:#x} got foreign value {v:#x}")
      dev.write_config(0x4, 0x6, 2)
      _, _, _, fd = dev._rpc(dev.sock, dev.dev_id, RemoteCmd.MAP_SYSMEM_FD, 0x1000, 0, has_fd=True)   # the has_fd (recvmsg) path
      if fd != (tag_of(RemoteCmd.MAP_SYSMEM_FD, 0, 0x1000) & 0x7fffffff): errors.append(f"main: has_fd reply mismatch {fd}")
      _, paddrs = dev.alloc_sysmem(0x2000)   # Linux-path two-step exchange (_rpc + _recvall) under the same lock
      if paddrs != [tag_of(RemoteCmd.MAP_SYSMEM, 0, 0x2000), tag_of(RemoteCmd.MAP_SYSMEM, 0, 0x2000) ^ 0xffff]: errors.append("main: alloc_sysmem paddrs foreign")
  def watchdog():
    for i in range(iters * 2):
      if stop_on_error and errors: return
      if (v := dev.read_config(0, 4)) != tag_of(RemoteCmd.CFG_READ, 0, 0): errors.append(f"watchdog: CFG_READ 0 got foreign value {v:#x}")
      d = dev._bulk_read(RemoteCmd.MMIO_READ, 5, 0x1665f * 4, 4)
      if d != bytes((tag_of(RemoteCmd.MMIO_READ, 5, 0x1665f * 4) + k) & 0xff for k in range(4)): errors.append("watchdog: THM dword got foreign data")
  ts = [threading.Thread(target=guard(main), name="main"), threading.Thread(target=guard(watchdog), name="watchdog")]
  for t in ts: t.start()
  for t in ts: t.join()
  return errors

# --- positive: with the per-device RLock, no wire-level race and every reply reaches its requester
sock = FakeSock(); dev = make_dev(sock)
check(isinstance(dev._sock_lock, type(threading.RLock())), "RemotePCIDevice.__init__ created a per-device threading.RLock")
check(dev.dev_id == 0x100266a1, f"dev_id from '1002:66a1' = {dev.dev_id:#x} (fork's vendor<<16|device rule)")
errs = run(dev, iters=300, stop_on_error=False)
check(not errs and not sock.violations, f"locked run: {sock.n} exchanges over 2 threads, {len(errs)} caller errors, {len(sock.violations)} wire violations")
for e in errs[:5]: print("     ", e)
# watchdog dead-man hook (§7.0 rule 3): the lock supports a timed acquire so a watchdog can notice a transport stalled behind a hung read
with dev._sock_lock: check(dev._sock_lock.acquire(timeout=0.05) is True, "RLock re-enters for the owning thread (alloc_sysmem's nested _rpc)") ; dev._sock_lock.release()
holder = threading.Thread(target=lambda: (dev._sock_lock.acquire(), time.sleep(0.2), dev._sock_lock.release())); holder.start(); time.sleep(0.02)
check(dev._sock_lock.acquire(timeout=0.05) is False, "acquire(timeout=) returns False while another thread holds the lock (dead-man rule can use it)")
holder.join()

# --- negative control: no lock -> the fake must catch the race (otherwise this check proves nothing)
sock2 = FakeSock(); dev2 = make_dev(sock2); dev2._sock_lock = contextlib.nullcontext()
errs2 = run(dev2, iters=300, stop_on_error=True)
check(bool(sock2.violations) or bool(errs2), f"unlocked run is caught: {len(sock2.violations)} wire violation(s), {len(errs2)} caller error(s)"
      + (f"; first: {(sock2.violations or errs2)[0]}" if (sock2.violations or errs2) else ""))

# --- ensure_app guard
class Sentinel(Exception): pass
calls: list[str] = []
def fake_system(cmd): calls.append(cmd); raise Sentinel(cmd)
sysmod.system, sysmod.fetch = fake_system, lambda *a, **k: (_ for _ in ()).throw(Sentinel("fetch"))
orig_path = APLRemotePCIDevice.APP_PATH
with tempfile.TemporaryDirectory() as td:
  present, absent = os.path.join(td, "TinyGPU"), os.path.join(td, "missing")
  open(present, "w").close()
  os.environ["TINYGPU_FORK"] = "1"; getenv.cache_clear()
  APLRemotePCIDevice.APP_PATH = present; APLRemotePCIDevice.ensure_app()
  check(calls == [], "TINYGPU_FORK=1 + APP_PATH present: ensure_app returned without pkill/ditto/fetch")
  APLRemotePCIDevice.APP_PATH = absent
  try: APLRemotePCIDevice.ensure_app(); check(False, "TINYGPU_FORK=1 + APP_PATH absent must raise")
  except RuntimeError as e: check("TINYGPU_FORK=1" in str(e) and calls == [], f"TINYGPU_FORK=1 + APP_PATH absent: {e}")
  os.environ.pop("TINYGPU_FORK"); getenv.cache_clear()
  APLRemotePCIDevice.APP_PATH = absent
  try: APLRemotePCIDevice.ensure_app(); check(False, "upstream path must reach the intercepted pkill")
  except Sentinel: check(calls == ["pkill -f TinyGPU"], f"without the flag the upstream sequence is unchanged (first action intercepted: {calls})")
APLRemotePCIDevice.APP_PATH = orig_path

print(f"\n{len(fails)} failure(s)" + (": " + "; ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
