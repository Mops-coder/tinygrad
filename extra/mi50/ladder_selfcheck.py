#!/usr/bin/env python3
"""Offline proof of the ladder harness (no card): a fake RemotePCIDevice drives extra/mi50/ladder.py through five scenarios, each in its own
subprocess because the dead-man and the abort paths end with os._exit.
  hang       - the thermal read blocks after 3 samples  -> "THERMAL UNKNOWN - POWER OFF", one quiesce attempt, exit 3 (design §7.0 rule 3)
  exception  - the rung raises RuntimeError            -> quiesce ran, power-off prompt, watchdog kept sampling, exit 1 (rule 6)
  thermal    - CTF_TEMP reaches the threshold          -> ABORT line, quiesce, power-off prompt, exit 2 (rules 1/4)
  invalid    - raw readings 0 and 0x3ffff (511/511)    -> logged INVALID, never counted as over threshold, exit 0 (rule 4)
  invalid_forever - every reading 0x3ffff            -> no valid sample for 10 s reaches the dead-man: THERMAL UNKNOWN, exit 3 (review S6, rules 3/4)
  raise      - the thermal read raises after 2 samples -> THERMAL UNKNOWN at once, quiesce, exit 3 (review S10, rule 3)
  contention - a thread hammers the rpc lock for 12 s  -> no dead-man, >= 10 watchdog samples, exit 0 (the unfair-RLock ceiling, measured)
  wrongcard  - config dword 0 is the RX 7600 XT       -> attach refuses with WRONG CARD before any watchdog read, exit 1 (identity gate)
Run: PYTHONPATH=<fork> python3 extra/mi50/ladder_selfcheck.py
"""
import os, sys, time, threading, subprocess

def encode(ctf:int, mx:int) -> int: return (ctf << 9) | mx   # thm_11_0_2_sh_mask.h:27-30

class FakeMMIO:
  def __init__(self, dev): self.dev = dev
  def __getitem__(self, dword): return self.dev._rpc(None, self.dev.dev_id, 6, dword * 4, 4, bar=5, readout_size=4)   # RemoteCmd.MMIO_READ = 6 (system.py:310)

class FakeDev:
  """Mimics the RemotePCIDevice surface the harness touches: _rpc staticmethod on the class, _bulk_write, map_bar (system.py:374-416)."""
  reads = 0
  mode = "normal"
  threshold = 0
  cfg0 = 0x66a11002   # config dword 0 the fake card answers (the identity gate reads it first)
  def __init__(self): self.dev_id, self.pcibus, self.sock = 0x100266a1, "fake:1002:66a1", None
  @staticmethod
  def _rpc(sock, dev_id, cmd, *args, bar=0, readout_size=0, payload=b'', has_fd=False):
    FakeDev.reads += 1
    if cmd != 6: return (0, 0, None, None)
    n = FakeDev.reads
    if FakeDev.mode == "hang" and n > 3: time.sleep(10_000)        # the blocking server loop (server.c:201-247) never answers
    if FakeDev.mode == "thermal": return encode(min(40 + n * 5, 510), 35)
    if FakeDev.mode == "invalid": return [0, 0x3ffff, encode(45, 40)][n % 3]
    if FakeDev.mode == "invalid_forever": return 0x3ffff
    if FakeDev.mode == "raise" and n > 2: raise RuntimeError("selfcheck: RPC failed (server status != 0)")
    return encode(45, 40)
  def _bulk_write(self, cmd, idx, offset, data): pass
  def read_config(self, offset, size): return FakeDev.cfg0 if offset == 0 else 0
  def map_bar(self, bar, off=0, addr=0, size=None, fmt='B'): return FakeMMIO(self)

def scenario(mode:str):
  os.environ["AM_MI50_TEMP_C"] = "60"
  from extra.mi50 import ladder
  FakeDev.mode = mode
  if mode == "wrongcard": FakeDev.cfg0 = 0x74801002   # the RX 7600 XT (PCI 1002:7480)
  h = ladder.Harness(f"selfcheck-{mode}", needs_cap=False, needs_dev=False)   # a fake device is attached below
  calls = []
  h.quiesce_fn = lambda: calls.append(1) or print("FAKE QUIESCE ran")
  h.attach(FakeDev()); h.start_watchdog()   # the fake card counts as POSTed: the watchdog starts after POST (ladder.py start_watchdog)
  def rung(h):
    if mode == "exception":
      time.sleep(2.5); raise RuntimeError("selfcheck injected exception")
    if mode == "contention":
      stop = time.monotonic() + 12
      def hammer():
        while time.monotonic() < stop: h.dev._rpc(None, 0, 3, 0, 4)   # CFG_READ spam under the same lock
      ts = [threading.Thread(target=hammer, daemon=True) for _ in range(2)]
      for t in ts: t.start()
      for t in ts: t.join()
      h.report.check(not h.wd.deadman_fired, "dead-man did not fire under 12 s of rpc-lock contention")
      h.report.check(len(h.wd.samples) >= 10, f"watchdog completed {len(h.wd.samples)} samples in 12 s (>= 10)")
      return
    time.sleep(30 if mode in ("hang", "invalid_forever", "raise") else 6.5)   # hang/thermal/...: the dead-man / ABORT must end the process before this returns
    if mode == "invalid":
      inv = [s for s in h.wd.samples if not s[3]]
      h.report.check(len(inv) >= 3, f"{len(inv)} INVALID samples logged (raw 0 / 0x3ffff)")
      h.report.check(all(s[1] in (0, 511) for s in inv), "INVALID samples decode to 0 or 511 and were not treated as over threshold")
  sys.exit(h.run(rung))

def main():
  # integration 2026-10-13: the fork's RemotePCIDevice ships the lock as _sock_lock with an instance-method _rpc (system.py:379-400, compute-host);
  # install_rpc_lock must reuse it, alias _rpc_lock for the rungs and wrap nothing (wrapping type(dev)._rpc would drop `self`)
  from extra.mi50 import ladder
  d = FakeDev(); d._sock_lock = threading.RLock(); orig = d._rpc
  assert ladder.install_rpc_lock(d) is d._sock_lock and d._rpc_lock is d._sock_lock and d._rpc is orig, "install_rpc_lock must reuse _sock_lock"
  print("ok   install_rpc_lock reuses the fork's _sock_lock (aliased as _rpc_lock) and leaves _rpc unwrapped")
  env = dict(os.environ, PYTHONPATH=os.environ.get("PYTHONPATH", "."))
  want = {"hang": (3, ["THERMAL UNKNOWN - POWER OFF", "QUIESCE: running rung quiesce", "FAKE QUIESCE ran", "POWER OFF: Leon"]),
          "exception": (1, ["EXCEPTION in rung", "FAKE QUIESCE ran", "POWER OFF: Leon", "RUNG selfcheck-exception: "]),
          "thermal": (2, ["ABORT: hotspot CTF_TEMP", "FAKE QUIESCE ran", "POWER OFF: Leon"]),
          "invalid": (0, ["INVALID(0/511", "INVALID samples logged (raw 0 / 0x3ffff)", "PASS  INVALID samples decode"]),
          "contention": (0, ["PASS  dead-man did not fire", "PASS  watchdog completed"]),
          "invalid_forever": (3, ["INVALID(0/511", "THERMAL UNKNOWN - POWER OFF (no valid (1..510) thermal read", "FAKE QUIESCE ran", "POWER OFF: Leon"]),
          "raise": (3, ["THERMAL UNKNOWN - POWER OFF (thermal read raised", "FAKE QUIESCE ran", "POWER OFF: Leon"]),
          "wrongcard": (1, ["WRONG CARD: config dword 0 = 0x74801002"])}
  ok = True
  for mode, (rc, needles) in want.items():
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, __file__, "--mode", mode], capture_output=True, text=True, env=env, timeout=120)
    out = p.stdout + p.stderr
    dt = time.monotonic() - t0
    missing = [n for n in needles if n not in out]
    wd = sum(1 for l in out.splitlines() if l.startswith("WD "))
    good = p.returncode == rc and not missing
    if mode == "exception": good = good and wd >= 2          # the watchdog kept sampling during the rung
    if mode == "hang": good = good and 10 <= dt <= 40          # dead-man at ~10 s after the last good read, quiesce bounded
    if mode == "invalid_forever": good = good and 10 <= dt <= 14 and wd >= 9   # reads complete (and are logged) but none is valid: dead-man at ~10 s
    if mode == "raise": good = good and dt <= 5                 # the raising read aborts immediately, not after the 10 s dead-man
    if mode == "wrongcard": good = good and wd == 0   # refused before the watchdog sampled anything (no WD line in the subprocess output)
    ok &= good
    print(f"{'ok  ' if good else 'FAIL'} {mode:10s} exit={p.returncode} (want {rc}) {dt:5.1f}s WD-samples={wd} missing={missing}")
    if not good: print(out)
  print("ladder_selfcheck: ALL SCENARIOS BEHAVED" if ok else "ladder_selfcheck: FAILURES ABOVE")
  sys.exit(0 if ok else 1)

if __name__ == "__main__":
  if len(sys.argv) == 3 and sys.argv[1] == "--mode": scenario(sys.argv[2])
  else: main()
