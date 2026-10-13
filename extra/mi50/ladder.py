#!/usr/bin/env python3
"""extra/mi50/ladder.py - common harness for the Vega 20 (MI50) test ladder, design v2 §7 (Vega20 Port Design v2.md:296-330).

What lives here (design §7.0 rules 1-8; every number below is cited to the line it comes from):
  * required inputs: env AM_MI50_TEMP_C (hotspot abort threshold, Leon's ruling after T0/T2, rule 4) and AM_MI50_PPT_W (SetPptLimit cap,
    Leon's ruling after T3, §8 U-S6). No defaults: absent -> the rung refuses to run. T-1/T0/T1/T2/T2b need the threshold only.
  * connect(): APLRemotePCIDevice("AM", "1002:66a1") -> dev_id 0x100266a1 (system.py:385-390 derives vendor<<16|device from "vvvv:dddd";
    system.py:67 formats the bus string f"{v:x}:{d:x}"; PCIIfaceBase passes dn[:2]="AM" as devpref, system.py:88/259). Requires env
    TINYGPU_FORK=1 and neutralises APLRemotePCIDevice.ensure_app in-process (system.py:421-428 would pkill/ditto over the forked app).
  * the _rpc lock (rule 2, §3 "system.py RemotePCIDevice._rpc" row): one RLock around every request/response so the watchdog thread can
    share the single-client socket (server.c:268-278 rejects a second client; system.py:394 flock). Installed instance-level here because
    the fork at HEAD d4697a019 has no lock in system.py (grep threading -> none); if the fork adds dev._rpc_lock, that one is used.
  * Watchdog thread (rule 2): reads dword 0x1665f every 1 s, logs CTF_TEMP (bits 9..17) and ASIC_MAX_TEMP (bits 0..8)
    (thm_11_0_2_sh_mask.h:27-30; vega20_thermal.c:218-232 masks CTF_TEMP & 0x1ff), treats 0 and 511 as INVALID (rule 4), prints ABORT at/over
    the threshold and ends the rung through the quiesce + power-off path (rule 1: never left without a managed state).
  * dead-man (rule 3): no completed read for 10 s -> "THERMAL UNKNOWN - POWER OFF", one quiesce attempt (bounded, it may hang on the same
    socket), os._exit(3) so the socket closes and server.c:273-276 handle_client returns.
  * exception policy (rule 6): Harness.run wraps the rung; any exception -> AMDev._emergency_quiesce (the amdev implementer's hook, §3
    amdev.py:160-163 row) or the rung-registered quiesce for pre-AM rungs -> power-off prompt. T6d proves it.
  * LadderAMDev: AMDev subclass that halts AMDev.__init__ right after a named block's init_hw (design §7.1 T6b "halted after
    init_hw(soc, gmc, ih, psp)"): the halt is a LadderHalt (BaseException) raised from init_hw; the subclass's _emergency_quiesce
    override makes the amdev implementer's except-hook a no-op for an intentional halt and counts real calls (T6d).
  * Report: every read printed as READ, every rule as PASS/FAIL, as-found values as AS-FOUND; the summary never says "clean" (rule 8).

Provided by: this file (ladder implementer). Consumed by egpu/mi50/port/ladder/t*.py. Run offline proof: python3 extra/mi50/ladder_selfcheck.py
"""
from __future__ import annotations
import os, sys, time, threading, json, pathlib, traceback, functools

sys.stdout.reconfigure(line_buffering=True)   # os._exit skips buffered output; every line must already be out

VENDOR_DEVICE = 0x66a11002   # config dword 0 of the MI50 (design §7.1 T1; amdev.py _run_discovery gate in §3)
PCIBUS = "1002:66a1"         # system.py:67 f"{v:x}:{d:x}" -> RemotePCIDevice.__init__ system.py:388-390 dev_id = 0x1002<<16 | 0x66a1
DEVPREF = "AM"               # system.py:88 cl(device[:2], pcibus) with device = "AMDDevice"[:-6] = "AMD" (system.py:259) -> lock am_1002:66a1.lock
THM_STATUS_DWORD = 0x1665f   # THM base 0x16600 (vega_offsets.py THM_BASE__INST0_SEG0) + mmCG_MULT_THERMAL_STATUS 0x5f (thm_11_0_2_offset.h:26-27); design §1.7
WATCHDOG_PERIOD_S = 1.0      # design §7.0 rule 2
DEADMAN_S = 10.0             # design §7.0 rule 3
QUIESCE_BOUND_S = DEADMAN_S  # the one quiesce attempt before os._exit may hang on the same socket (rule 3); bounded so the exit always follows
POWER_OFF_PROMPT = "POWER OFF: Leon powers the enclosure off now (design §7.0 rule 1: no automated power-off exists, server.c:19-30)"

def decode_temp(raw:int) -> tuple[int, int, bool]:
  """CG_MULT_THERMAL_STATUS -> (CTF_TEMP, ASIC_MAX_TEMP, valid). thm_11_0_2_sh_mask.h:27-30: ASIC_MAX_TEMP mask 0x1ff shift 0, CTF_TEMP mask
  0x3fe00 shift 9. Valid = both 9-bit fields in 1..510 (design §7.0 rule 4: 0 or 511 is INVALID, not cool)."""
  ctf, mx = (raw >> 9) & 0x1ff, raw & 0x1ff
  return ctf, mx, (1 <= ctf <= 510) and (1 <= mx <= 510)

def _env_int(name:str, what:str, lo:int, hi:int) -> int:
  v = os.environ.get(name)
  if v is None or v.strip() == "": raise SystemExit(f"REQUIRED input missing: env {name} = {what}. No default exists (design §8 U-S6: Leon rules it).")
  try: n = int(v, 0)
  except ValueError: raise SystemExit(f"env {name}={v!r} is not an integer ({what})")
  if not lo <= n <= hi: raise SystemExit(f"env {name}={n} outside {lo}..{hi} ({what})")
  return n

def env_threshold() -> int: return _env_int("AM_MI50_TEMP_C", "hotspot abort threshold in C (design §7.0 rule 4)", 1, 510)  # 9-bit field, 0/511 invalid
def env_cap() -> int: return _env_int("AM_MI50_PPT_W", "SetPptLimit cap in W (design B16/T3; PPSMC_MSG_SetPptLimit 0xF)", 1, 0xffff)

class Report:
  """One report per rung (design §7.0 rule 8). PASS/FAIL per rule, READ per value, AS-FOUND for inputs with no pass value."""
  def __init__(self, rung:str):
    self.rung, self.passes, self.fails, self.asfound, self.values = rung, 0, [], 0, {}
    self.out = pathlib.Path(os.environ.get("AM_MI50_LADDER_OUT", "ladder_out"))
  def read(self, name:str, value, cite:str="") -> None:
    self.values[name] = value
    print(f"READ  {name} = {value:#x}" if isinstance(value, int) and not isinstance(value, bool) else f"READ  {name} = {value!r}", f"[{cite}]" if cite else "")
  def check(self, cond:bool, msg:str) -> bool:
    print(("PASS  " if cond else "FAIL  ") + msg)
    if cond: self.passes += 1
    else: self.fails.append(msg)
    return bool(cond)
  def record(self, msg:str) -> None:
    self.asfound += 1; print("AS-FOUND " + msg)
  def finding(self, msg:str) -> None: self.fails.append(msg); print("FINDING " + msg)
  def summary(self, extra:str="") -> int:
    self.out.mkdir(parents=True, exist_ok=True)
    (self.out / f"{self.rung}.json").write_text(json.dumps(self.values, indent=1, default=str))
    print(f"RUNG {self.rung}: {self.passes} pass, {len(self.fails)} fail, {self.asfound} as-found{(' | ' + extra) if extra else ''}; "
          f"adversarial review not exhausted (values -> {self.out / (self.rung + '.json')})")
    for f in self.fails: print(f"  FAIL: {f}")
    return 0 if not self.fails else 1

def install_rpc_lock(dev) -> threading.RLock:
  """Rule 2 / §3 critic gap 25: serialise every request+response on the shared socket. _rpc is a staticmethod called as self._rpc(sock, ...)
  (system.py:374-383, 398, 404-416); _bulk_write sends without _rpc (system.py:400-401). Both get an instance-level locked wrapper. If the fork
  already ships dev._rpc_lock (design §3 row), it is reused and nothing is wrapped."""
  if (lock:=getattr(dev, "_rpc_lock", None)) is not None: return lock
  # integration 2026-10-13: the fork now ships the lock as RemotePCIDevice._sock_lock (compute-host report: instance-method _rpc, _bulk_read,
  # _bulk_write and alloc_sysmem already run under it, system.py:379-419), so nothing is wrapped; the alias keeps `dev._rpc_lock` for the rungs.
  if (lock:=getattr(dev, "_sock_lock", None)) is not None: dev._rpc_lock = lock; return lock
  lock = dev._rpc_lock = threading.RLock()
  orig_rpc, orig_bulk_write = type(dev)._rpc, dev._bulk_write
  def rpc(*a, **k):
    with lock: return orig_rpc(*a, **k)
  def bulk_write(*a, **k):
    with lock: return orig_bulk_write(*a, **k)
  dev._rpc, dev._bulk_write = rpc, bulk_write
  return lock

def connect_mi50():
  """APLRemotePCIDevice for the MI50. system.py:431-441: connects to APL_REMOTE_SOCK (default temp("tinygpu.sock")) and spawns
  `TinyGPU server <sock>` itself when nothing listens (design §7.1 T-1 pipeline)."""
  if os.environ.get("TINYGPU_FORK") != "1":
    raise SystemExit("REQUIRED: env TINYGPU_FORK=1 (design §3 system.py:417-425 row: never pkill/ditto over the forked, ad-hoc-signed TinyGPU.app)")
  from tinygrad.runtime.support.system import APLRemotePCIDevice
  if not os.path.exists(APLRemotePCIDevice.APP_PATH): raise SystemExit(f"{APLRemotePCIDevice.APP_PATH} missing: the forked TinyGPU.app is not installed (T-1)")
  # fork HEAD d4697a019 system.py:421-428 still downloads + `pkill -f TinyGPU` + ditto when the pinned zip is absent; neutralised in-process here.
  APLRemotePCIDevice.ensure_app = classmethod(lambda cls: None)
  dev = APLRemotePCIDevice(DEVPREF, PCIBUS)
  install_rpc_lock(dev)
  return dev

class Watchdog(threading.Thread):
  """Design §7.0 rules 2-4. Reads THM_STATUS_DWORD every WATCHDOG_PERIOD_S through dev.map_bar(5) (one MMIO_READ RPC under the lock, system.py:313-320,
  398). A second daemon thread is the dead-man: the sampling thread itself is the one stuck inside a hung read (server.c:201-247 blocks)."""
  def __init__(self, dev, threshold:int, on_abort, metrics=None):
    super().__init__(name="mi50-watchdog", daemon=True)
    self.dev, self.threshold, self.on_abort, self.metrics = dev, threshold, on_abort, metrics
    self.mmio = dev.map_bar(5, fmt='I')   # MAP_BAR is idempotent in the server (server.c:122: maps once, returns the same size)
    self.samples:list[tuple[float, int, int, bool]] = []
    self.last_ok, self.t0, self.stop_evt, self.deadman_fired = time.monotonic(), time.monotonic(), threading.Event(), False
  def read_raw(self) -> int: return self.mmio[THM_STATUS_DWORD]
  def run(self):
    threading.Thread(target=self._deadman, name="mi50-deadman", daemon=True).start()
    while not self.stop_evt.is_set():
      try: raw = self.read_raw()
      except BaseException as e:   # review 2026-10-13 S10: a failed read is as unknown as a hung one (rule 3) - abort now, not after the dead-man
        self.on_abort("deadman", f"THERMAL UNKNOWN - POWER OFF (thermal read raised {e!r}; design §7.0 rule 3)"); return
      now = time.monotonic()
      ctf, mx, valid = decode_temp(raw)
      if valid: self.last_ok = now   # review S6: an INVALID (0/511) sample is not a reading (rule 4); 10 s of them reach the dead-man (rule 3)
      self.samples.append((now - self.t0, ctf, mx, valid))
      line = f"WD +{now - self.t0:7.1f}s CTF_TEMP={ctf:3d} C ASIC_MAX_TEMP={mx:3d} C raw={raw:#010x}" + ("" if valid else " INVALID(0/511 = not a reading)")
      if self.metrics is not None:
        try: line += " " + self.metrics()
        except BaseException as e:   # §7.0 rule 5: the first SMU timeout in a rung ends the rung
          print(line); self.on_abort("metrics", f"ABORT: watchdog metrics read failed ({e!r}); design §7.0 rule 5 ends the rung"); return
      print(line)
      if valid and ctf >= self.threshold:
        self.on_abort("thermal", f"ABORT: hotspot CTF_TEMP {ctf} C >= threshold {self.threshold} C (AM_MI50_TEMP_C); design §7.0 rules 1/4"); return
      self.stop_evt.wait(WATCHDOG_PERIOD_S)
  def _deadman(self):
    while not self.stop_evt.is_set():
      self.stop_evt.wait(0.5)
      if not self.stop_evt.is_set() and time.monotonic() - self.last_ok > DEADMAN_S:
        self.deadman_fired = True
        self.on_abort("deadman", f"THERMAL UNKNOWN - POWER OFF (no valid (1..510) thermal read for {DEADMAN_S:.0f} s; design §7.0 rules 3/4)"); return
  def stop(self): self.stop_evt.set()

class LadderHalt(BaseException):
  """Intentional stop of AMDev.__init__ after a named block (design §7.1 T6b/T7/T8). BaseException so nothing in AM swallows it."""

def make_ladder_amdev(stop_after:str|None, inject_after:str|None=None, on_init_sw=None, on_instance=None):
  """AMDev subclass: amdev.py:236 calls init_hw(soc, gmc, ih, psp, smu) and :242 init_hw(gfx, sdma). The override runs blocks in order and raises
  LadderHalt right after `stop_after`'s init_hw. If the first init_hw call does not contain the stop block (partial boot, amdev.py:219-225 skipped
  the full-boot call), it halts BEFORE running anything and records what was skipped. inject_after raises a RuntimeError after that block (T6d).
  _emergency_quiesce (the amdev implementer's hook, §3 amdev.py:160-163 row) is a no-op for the intentional halt and counted otherwise.
  on_init_sw(adev) runs right after init_sw (amdev.py:217), i.e. after the IP objects exist and before any init_hw: observers hook here.
  on_instance(adev) runs on the first line of __init__ (review 2026-10-13 S1): the harness sees the AMDev while AMDev.__init__ is still running,
  so an abort fired mid-boot reaches _emergency_quiesce instead of "no AMDev". The SMU message lock is installed right after init_sw (S5: the
  IP objects exist, no init_hw has run) so a quiesce from the abort thread waits for the in-flight mailbox message instead of interleaving.
  stop_after=None halts at the first init_hw call before running any block (T6a needs the sw-built AMDev only)."""
  from tinygrad.runtime.support.am.amdev import AMDev
  class LadderAMDev(AMDev):
    instance = None
    def __init__(self, pci_dev, **kw):
      type(self).instance = self
      self._ladder_halted, self._ladder_skipped, self._quiesce_calls = False, None, 0
      if on_instance is not None: on_instance(self)
      super().__init__(pci_dev, **kw)
    def init_sw(self, smi_dev=False):
      super().init_sw(smi_dev=smi_dev)
      install_smu_lock(self)
      if on_init_sw is not None: on_init_sw(self)
    def init_hw(self, *blocks):
      stop = getattr(self, stop_after) if stop_after is not None else None
      if stop not in blocks:
        self._ladder_halted, self._ladder_skipped = True, [b.__class__.__name__ for b in blocks]
        if stop_after is None: self._ladder_skipped = None   # intended: nothing of init_hw runs
        raise LadderHalt(f"init_hw({', '.join(b.__class__.__name__ for b in blocks)}) reached before {stop_after}.init_hw: halted without running it")
      for ip in blocks:
        ip.init_hw()
        print(f"am {self.devfmt}: {ip.__class__.__name__} initialized (ladder)")   # mirrors amdev.py:274-277 at DEBUG>=2
        if inject_after is not None and ip is getattr(self, inject_after): raise RuntimeError(f"T6d injected RuntimeError after {inject_after}.init_hw")
        if ip is stop:
          self._ladder_halted = True
          raise LadderHalt(f"halted after {stop_after}.init_hw")
    def _emergency_quiesce(self):
      if self._ladder_halted: return   # an intentional halt is not an emergency
      self._quiesce_calls += 1
      sup = getattr(super(), "_emergency_quiesce", None)
      if sup is None: print("QUIESCE: AMDev._emergency_quiesce is NOT provided by the fork (design §3 amdev.py:160-163 row) - nothing sent"); return
      return sup()
  return LadderAMDev

class Harness:
  def __init__(self, rung:str, needs_cap:bool, needs_dev:bool=True):
    self.rung, self.threshold, self.cap = rung, env_threshold(), (env_cap() if needs_cap else None)
    if needs_dev and os.environ.get("TINYGPU_FORK") != "1":
      raise SystemExit("REQUIRED: env TINYGPU_FORK=1 (design §3 system.py:417-425 row: never pkill/ditto over the forked, ad-hoc-signed TinyGPU.app)")
    if self.cap is not None:
      os.environ["AM_POWER_LIMIT"] = str(self.cap)   # the existing AM cap env (amdev.py:244) so a stock clock block gets Leon's number, never max perf
    self.dev = self.adev = self.wd = self.quiesce_fn = None
    self.quiesce_ran, self._aborted, self._abort_lock = False, False, threading.Lock()
    self.report = Report(rung)
    print(f"RUNG {rung} start: threshold AM_MI50_TEMP_C={self.threshold} C" + (f", cap AM_MI50_PPT_W={self.cap} W" if self.cap is not None else ""))
  # --- device / watchdog
  def connect(self):
    return self.attach(connect_mi50())
  def attach(self, dev):
    """Start the watchdog on an existing device (T12: the device AMDDevice created; hook_apl_device wires it)."""
    self.dev = dev; install_rpc_lock(dev)
    self.wd = Watchdog(dev, self.threshold, self.abort); self.wd.start()
    return dev
  def hook_apl_device(self):
    """T12: AMDDevice builds its own APLRemotePCIDevice inside PCIIfaceBase.__init__ (system.py:259) before AMDev boots (:261); wrap __init__ so
    the lock + watchdog are live before the boot starts."""
    from tinygrad.runtime.support.system import APLRemotePCIDevice
    from tinygrad.runtime.support.am.amdev import AMDev
    if os.environ.get("TINYGPU_FORK") != "1": raise SystemExit("REQUIRED: env TINYGPU_FORK=1")
    APLRemotePCIDevice.ensure_app = classmethod(lambda cls: None)
    orig = APLRemotePCIDevice.__init__
    def init(dev_self, devpref, pcibus):
      orig(dev_self, devpref, pcibus)
      if pcibus == PCIBUS and self.dev is None: self.attach(dev_self)
    APLRemotePCIDevice.__init__ = init
    # review 2026-10-13 S1/S5 for T12: the stock AMDev is published to the harness on its first line and gets the SMU message lock right after
    # init_sw, exactly like LadderAMDev - an abort during the AMDDevice boot reaches _emergency_quiesce and never races the mailbox.
    orig_am_init, orig_am_init_sw = AMDev.__init__, AMDev.init_sw
    def am_init(adev_self, pci_dev, **kw):
      if self.adev is None: self.adev = adev_self
      orig_am_init(adev_self, pci_dev, **kw)
    def am_init_sw(adev_self, smi_dev=False):
      orig_am_init_sw(adev_self, smi_dev=smi_dev)
      if adev_self is self.adev: install_smu_lock(adev_self)
    AMDev.__init__, AMDev.init_sw = am_init, am_init_sw
  # --- AM boot stages
  def boot_to(self, stop_after:str|None, inject_after:str|None=None, on_init_sw=None):
    """Run AMDev.__init__ on self.dev up to and including <stop_after>.init_hw (soc|gmc|ih|psp|smu; None = before any init_hw), then set
    is_booting=False exactly as amdev.py:239 does after the full-boot block, so the rung can allocate non-boot memory. Returns the AMDev.
    Everything __init__ does before the halt (B1 ASPM, B3/B4 static table + POST + VBIOS, B6 init_sw, B7 reset decision) is the amdev
    implementer's code, run unchanged."""
    cls = make_ladder_amdev(stop_after, inject_after, on_init_sw, on_instance=lambda a: setattr(self, "adev", a))
    try: cls(self.dev)
    except LadderHalt as e: print(f"BOOT halted: {e}")
    finally: self.adev = cls.instance
    if self.adev is None: raise RuntimeError("AMDev.__init__ returned no instance (it finished without reaching the stop block?)")
    self.report.check(hasattr(self.adev, "_emergency_quiesce") and type(self.adev).__mro__[1].__dict__.get("_emergency_quiesce") is not None,
                      "fork provides AMDev._emergency_quiesce (design §3 amdev.py:160-163 row)")
    if self.adev._ladder_skipped is not None:
      self.report.check(False, f"full-boot init_hw call never happened: partial boot (SCRATCH_REG7 == adev.boot_version = "
                        f"AMDev.VEGA20_BOOT_VERSION, amdev.py, ruled by Leon 2026-10-13) skipped it; "
                        f"halted before init_hw({', '.join(self.adev._ladder_skipped)}); rerun with AM_RESET=1")
    self.adev.is_booting = False   # amdev.py:239 "Booting done"
    install_smu_lock(self.adev)
    return self.adev
  # --- quiesce / abort / exception policy
  def quiesce_once(self):
    if self.quiesce_ran: return
    self.quiesce_ran = True
    adev = self.adev
    if adev is not None and hasattr(adev, "_emergency_quiesce"):
      if getattr(adev, "_quiesce_calls", 0) > 0: print(f"QUIESCE: AMDev._emergency_quiesce already ran {adev._quiesce_calls}x inside AMDev (fork hook)"); return
      adev._ladder_halted = False   # a real emergency now, whatever the boot state
      print("QUIESCE: calling AMDev._emergency_quiesce (amdev implementer's hook)")
      try: adev._emergency_quiesce(); print("QUIESCE: returned")
      except BaseException as e: print(f"QUIESCE: raised {e!r}")
      return
    if self.quiesce_fn is None: print("QUIESCE: no AMDev and no rung quiesce registered - nothing to send (pre-AM rung)"); return
    print(f"QUIESCE: running rung quiesce {getattr(self.quiesce_fn, '__name__', self.quiesce_fn)}")
    try: self.quiesce_fn(); print("QUIESCE: returned")
    except BaseException as e: print(f"QUIESCE: raised {e!r}")
  def abort(self, kind:str, msg:str):
    """Watchdog-thread entry (thermal / deadman / metrics). Quiesce once (bounded), power-off prompt, hard exit: 3 = dead-man (design §7.0 rule 3)."""
    with self._abort_lock:
      if self._aborted: return
      self._aborted = True
    print(msg)
    th = threading.Thread(target=self.quiesce_once, daemon=True); th.start(); th.join(QUIESCE_BOUND_S)
    if th.is_alive(): print(f"QUIESCE: still not returned after {QUIESCE_BOUND_S:.0f} s (hung socket?) - exiting anyway so the socket closes (rule 3)")
    print(POWER_OFF_PROMPT)
    try: self.report.summary(extra=f"ABORTED ({kind})")
    except BaseException: pass
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(3 if kind == "deadman" else 2)
  def run(self, fn, post=None) -> int:
    """Exception policy (design §7.0 rule 6): any exception -> quiesce -> power-off prompt. Returns the process exit code.
    post(h, exc) runs before the summary with the exception (or None): T6d asserts on the quiesce/watchdog state there."""
    rc, exc = 1, None
    try:
      fn(self)
    except SystemExit:
      if self.dev is None: raise   # pre-connect refusal (REQUIRED inputs etc.): nothing to quiesce
      exc = sys.exc_info()[1]; print(f"EXCEPTION in rung {self.rung}: {exc!r} (SystemExit with a live device, review 2026-10-13 S9)")
      self.quiesce_once(); self.report.fails.append(f"rung raised SystemExit: {exc}"); print(POWER_OFF_PROMPT)
    except BaseException as e:
      exc = e
      print(f"EXCEPTION in rung {self.rung}: {e!r}"); traceback.print_exc()
      self.quiesce_once()
      self.report.fails.append(f"rung raised {type(e).__name__}: {e}")
      print(POWER_OFF_PROMPT)
    finally:
      if post is not None:
        try: post(self, exc)
        except BaseException as e2: self.report.fails.append(f"post-check raised {e2!r}"); traceback.print_exc()
      rc = 0 if not self.report.fails else 1
      if self.wd is not None:
        self.wd.stop()
        if time.monotonic() - self.wd.last_ok > DEADMAN_S:   # the rung ended while the thermal read was hung or invalid: the state is unknown (rule 3)
          print(f"THERMAL UNKNOWN - POWER OFF (last valid thermal read {time.monotonic() - self.wd.last_ok:.0f} s ago; design §7.0 rules 3/4)")
          print(POWER_OFF_PROMPT); rc = 3
      self.report.summary(extra=f"watchdog samples={len(self.wd.samples) if self.wd else 0}")
    return rc

def install_smu_lock(adev) -> threading.RLock:
  """The SMU mailbox is a multi-RPC sequence (ip.py:240-249: write 90, 82, 66, poll 90, read 82). The rpc lock serialises single RPCs only, so the
  watchdog's metrics read (T7+) and the rung's messages are serialised here, at message level. Lock order is always smu_lock -> rpc lock."""
  if (lock:=getattr(adev, "_ladder_smu_lock", None)) is not None: return lock
  lock = adev._ladder_smu_lock = threading.RLock()
  orig = adev.smu._send_msg
  def send_msg(*a, **k):
    with lock: return orig(*a, **k)
  adev.smu._send_msg = send_msg
  return lock

# --- register addresses without an AMDev (T0-T3, T6c): generated table offset + the static Vega 20 base of its BASE_IDX segment (design B3/B5)
def vega20_bases() -> dict[str, tuple[int, ...]]:
  """Segment bases from the fork's vega_offsets (the design's source for the static table, B3; ops_amd.py:1024-1028 reads them the same way)."""
  from tinygrad.runtime.autogen.am import vega_offsets as vo
  return {ip: tuple(getattr(vo, f"{ip}_BASE__INST0_SEG{i}", 0) for i in range(6)) for ip in ["GC", "MMHUB", "NBIO", "MP0", "MP1", "THM", "SMUIO", "SDMA0", "SDMA1", "HDP", "OSSSYS", "ATHUB"]}

def reg_dword(table:str, name:str, bases:tuple[int, ...]) -> int:
  """Dword address of generated-table register <name> (mm* key) in <table> given the segment bases."""
  from tinygrad.runtime.autogen.am import regs
  off, seg, _ = getattr(regs, table)[name]
  return bases[seg] + off

class SMURejected(RuntimeError): pass

class RawSMU:
  """SMU mailbox over BAR5 with no AMDev (design §7.1 T3 "no PSP, no boot"). Protocol = vega20_smumgr.c:132-156 / AM ip.py:240-249: write
  C2PMSG_90=0, C2PMSG_82=param, C2PMSG_66=msg, wait 90 != 0, require PPSMC_Result_OK (1), read arg from 82 (design §6). Registers from the
  mp_11_0_0 table (mmMP1_SMN_C2PMSG_66/82/90 = 0x282/0x292/0x29a) + MP1 base 0x16000 (vega_offsets MP1_BASE__INST0_SEG0)."""
  def __init__(self, mmio):
    from tinygrad.runtime.autogen.am import smu_11_0_2 as smu
    self.mmio, self.smu = mmio, smu
    b = vega20_bases()["MP1"]
    self.r66, self.r82, self.r90 = (reg_dword("mp_11_0_0", f"mmMP1_SMN_C2PMSG_{n}", b) for n in (66, 82, 90))
    self.names = {v: k for k, v in vars(smu).items() if k.startswith("PPSMC_Result_")}
    self.msgnames = {v: k[10:] for k, v in vars(smu).items() if k.startswith("PPSMC_MSG_")}
  def send(self, msg:int, param:int=0, timeout_ms:int=10000) -> int:
    """timeout_ms default = AM _send_msg default 10 s (ip.py:245). First timeout or rejection ends the rung (design §7.0 rule 5, T3 row)."""
    self.mmio[self.r90] = 0; self.mmio[self.r82] = param; self.mmio[self.r66] = msg
    t0 = time.monotonic(); resp = 0
    while (resp:=self.mmio[self.r90]) == 0 and (time.monotonic() - t0) * 1000 < timeout_ms: pass
    arg = self.mmio[self.r82]; dt = (time.monotonic() - t0) * 1000
    print(f"SMU   {self.msgnames.get(msg, msg)}({msg:#x}, param={param:#x}) -> resp={resp:#x} {self.names.get(resp, '?')} arg={arg:#x} ({dt:.1f} ms)")
    if resp == 0: raise TimeoutError(f"SMU msg {msg:#x} no response in {timeout_ms} ms (design §7.0 rule 5: first timeout ends the rung)")
    if resp != self.smu.PPSMC_Result_OK: raise SMURejected(f"SMU msg {msg:#x} rejected: {resp:#x} {self.names.get(resp, '?')} (T3 rule: ends the rung)")
    return arg
  def is_alive(self) -> bool:
    """ip.py:188-190: GetSmuVersion with a 100 ms timeout, then C2PMSG_90 != 0."""
    try: self.send(self.smu.PPSMC_MSG_GetSmuVersion, 0, timeout_ms=100)
    except (TimeoutError, SMURejected) as e: print(f"SMU   is_alive: {e}")
    return self.mmio[self.r90] != 0

def cap_step(send, cap_w:int, features_low:int, report:Report):
  """Design §7.1 T3 cap step: SetPptLimit <cap> (vega20_powertune.c, PPSMC_MSG_SetPptLimit 0xF), then SetSoftMaxByFreq GFXCLK (0x27, clk<<16|freq)
  to the lowest GetDpmFreqByIndex level (0x2C, clk<<16|0xFF = count, |i = level; AM ip.py:208-209 encoding, &0x7fffffff) - only if features & 0x30F
  shows GFXCLK DPM (FEATURE_DPM_GFXCLK_BIT = 1, smu11_driver_if.h; SMC_DPM_FEATURES 0x30F vega20_hwmgr.h:91). Returns the requested GFXCLK min or None."""
  from tinygrad.runtime.autogen.am import smu_11_0_2 as smu
  send(smu.PPSMC_MSG_SetPptLimit, cap_w); report.read("SetPptLimit.sent_W", cap_w, "AM_MI50_PPT_W")
  dpm = bool(features_low & (1 << smu.FEATURE_DPM_GFXCLK_BIT))
  report.read("features_low.DPM_GFXCLK", dpm, "bit 1 of GetEnabledSmuFeaturesLow; vega20_hwmgr.h:91 SMC_DPM_FEATURES 0x30F")
  if not dpm:
    report.record("GFXCLK DPM not running: SetSoftMaxByFreq skipped (design T3: only if features & 0x30F shows GFXCLK DPM)"); return None
  cnt = send(smu.PPSMC_MSG_GetDpmFreqByIndex, (smu.PPCLK_GFXCLK << 16) | 0xff) & 0x7fffffff
  report.read("GFXCLK.dpm_level_count", cnt, "GetDpmFreqByIndex clk<<16|0xFF; ip.py:208-209")
  levels = [send(smu.PPSMC_MSG_GetDpmFreqByIndex, (smu.PPCLK_GFXCLK << 16) | i) & 0x7fffffff for i in range(cnt)]
  report.read("GFXCLK.dpm_levels_MHz", levels, "GetDpmFreqByIndex clk<<16|i")
  if not levels: report.check(False, "GetDpmFreqByIndex returned 0 GFXCLK levels although DPM_GFXCLK is enabled"); return None
  send(smu.PPSMC_MSG_SetSoftMaxByFreq, (smu.PPCLK_GFXCLK << 16) | levels[0]); report.read("SetSoftMaxByFreq.GFXCLK_MHz", levels[0], "lowest level")
  return levels[0]

def watch_cap(h:"Harness", send, want_gfx_mhz:int|None, minutes:int=5):
  """Design §7.1 T3 pass rule: within 5 min the hotspot reading is non-increasing over the last 60 samples (least-squares slope <= 0 C/min) and
  GetDpmClockFreq GFXCLK (0x2D, clk<<16; vega20_hwmgr.c:2178) == the requested min. The watchdog is the sampler (1 Hz)."""
  from tinygrad.runtime.autogen.am import smu_11_0_2 as smu
  print(f"WATCH {minutes} min under the cap (watchdog samples at 1 Hz)")
  t_end = time.monotonic() + minutes * 60
  while time.monotonic() < t_end: time.sleep(1)
  last = [s for s in h.wd.samples if s[3]][-60:]
  h.report.check(len(last) == 60, f"{len(last)} valid samples in the last-60 window")
  if len(last) >= 2:
    xs = [s[0] / 60 for s in last]; ys = [s[1] for s in last]; mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sum((x - mx) ** 2 for x in xs) or 1)
    h.report.read("hotspot.slope_C_per_min_last60", round(slope, 4), "least squares over the last 60 valid CTF_TEMP samples")
    h.report.check(slope <= 0, f"hotspot non-increasing over the last 60 samples (slope {slope:+.3f} C/min <= 0)")
  if want_gfx_mhz is not None:
    cur = send(smu.PPSMC_MSG_GetDpmClockFreq, smu.PPCLK_GFXCLK << 16)
    h.report.read("GetDpmClockFreq.GFXCLK_MHz", cur, "0x2D clk<<16")
    h.report.check(cur == want_gfx_mhz, f"GetDpmClockFreq GFXCLK {cur} == requested min {want_gfx_mhz}")

def psp_observer(adev, log:list):
  """T6b/U-P3: record every PSP ring command and its resp.status (ip.py:662-678 _ring_submit) and every bootloader stage (ip.py:623-634)."""
  from tinygrad.runtime.autogen.am import am
  cmdnames = {v: k[11:] for k, v in vars(am).items() if k.startswith("GFX_CMD_ID_") and isinstance(v, int)}
  orig_submit, orig_bl = adev.psp._ring_submit, adev.psp._bootloader_load_component
  def ring_submit(cmd):
    name = cmdnames.get(cmd.cmd_id, f"cmd_id {cmd.cmd_id:#x}")
    extra = f" fw_type={am.enum_psp_gfx_fw_type.get(cmd.cmd.cmd_load_ip_fw.fw_type, cmd.cmd.cmd_load_ip_fw.fw_type)}" if cmd.cmd_id == am.GFX_CMD_ID_LOAD_IP_FW else ""
    try: resp = orig_submit(cmd)
    except RuntimeError as e:   # ip.py:677 raises on resp.status != 0 with the status in the message
      log.append((name, extra.strip(), f"raised: {e}")); print(f"PSP   {name}{extra} -> {e}"); raise
    status = resp.resp.status; log.append((name, extra.strip(), status)); print(f"PSP   {name}{extra} -> resp.status={status}")
    return resp
  def bl_load(fw, compid):
    before = adev.reg(f"{adev.psp.reg_pref}_35").read()
    r = orig_bl(fw, compid)
    after = adev.reg(f"{adev.psp.reg_pref}_35").read()
    print(f"PSP   bootloader stage {am.enum_psp_fw_type.get(fw, fw)} cmd={compid:#x}: C2PMSG_35 before={before:#010x} after={after:#010x}"
          f" ready={(after & 0x8000FFFF) == 0x80000000} (psp_v11_0.c:179-188 mask 0x8000FFFF == 0x80000000)")
    log.append((f"BL {am.enum_psp_fw_type.get(fw, fw)}", f"cmd={compid:#x}", f"35={after:#x}"))
    return r
  adev.psp._ring_submit, adev.psp._bootloader_load_component = ring_submit, bl_load

def boot_sw_only(h:"Harness"):
  """T6a only: the AMDev with its software state built and NO init_hw and NO reset decision - amdev.py:216-218 + 229-233 + 245-246 run verbatim
  (pci_dev, _disable_aspm, map BAR0/2/5, _run_discovery -> the amdev implementer's _static_vega20, _build_regs, _vega20_vbios, init_sw). The reset
  branch (amdev.py:257-265) is deliberately not reached: T6a issues AM_PSP.mode1_reset itself ("alone")."""
  from tinygrad.runtime.support.am.amdev import AMDev, VEGA20_PCI_ID
  adev = AMDev.__new__(AMDev)
  h.adev = adev   # review 2026-10-13 S8: published before anything runs, so Harness.run / abort reach _emergency_quiesce (it copes with partial state)
  adev.pci_dev, adev.devfmt = h.dev, h.dev.pcibus                                                                       # amdev.py __init__
  adev.is_vega20 = h.dev.read_config(0, 4) == VEGA20_PCI_ID                                                              # amdev.py __init__ (one CFG_READ)
  if adev.is_vega20: adev._vega20_required_inputs()                                                                      # amdev.py __init__ (review TI4)
  adev._disable_aspm()                                                                                                   # amdev.py __init__
  adev.vram, adev.doorbell64, adev.mmio = h.dev.map_bar(0), h.dev.map_bar(2, fmt='Q'), h.dev.map_bar(5, fmt='I')         # amdev.py __init__
  adev._run_discovery(); adev._build_regs()                                                                              # amdev.py _boot
  # integration 2026-10-13: the amdev branch's _boot (amdev.py:232-233) derives boot_version and, on the Vega 20, runs _vega20_vbios() (required
  # inputs, POST gate, ROM dump: reads only) which sets reserved_vram_size and gc_info; init_sw needs both (amdev report, amdev.py:481-482).
  adev.boot_version = AMDev.VEGA20_BOOT_VERSION if adev.is_vega20 else AMDev.Version      # amdev.py:232
  if adev.is_vega20: adev._vega20_vbios()                                                                                # amdev.py:233
  adev.is_booting = True; adev.init_sw(smi_dev=False)                                                                    # amdev.py:245-246
  install_smu_lock(adev)
  return adev

def power_off_prompt(): print(POWER_OFF_PROMPT)

if __name__ == "__main__":
  print(__doc__)
