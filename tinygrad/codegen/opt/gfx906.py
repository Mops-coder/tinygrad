from dataclasses import replace
from tinygrad.codegen.opt.postrange import Scheduler
from tinygrad.dtype import AddrSpace, dtypes
from tinygrad.helpers import EMULATED_DTYPES, GFX906_DOT2, MI50_PREFETCH, MI50_PREFETCH_TAG
from tinygrad.uop.ops import AxisType, KernelInfo, Ops, UOp

_FLOATS = (dtypes.half, dtypes.float, dtypes.double)

def _true(x:UOp) -> bool:
  value = x.ssimplify()
  return isinstance(value, bool) and value

def _same_index(a:UOp, b:UOp) -> bool:
  value = (a-b).ssimplify()
  return isinstance(value, int) and value == 0

def _strip_float_casts(x:UOp) -> UOp|None:
  while x.op is Ops.CAST:
    if x.dtype not in _FLOATS or x.src[0].dtype not in _FLOATS: return None
    x = x.src[0]
  return x if x.dtype in _FLOATS else None

def _input(x:UOp) -> UOp|None:
  x = _strip_float_casts(x)
  if x is None or x.op is not Ops.INDEX or len(x.src) != 2: return None
  p = x.src[0]
  if p.op is not Ops.PARAM or p.addrspace is not AddrSpace.GLOBAL: return None
  if p.arg.volatile or not _true(x.src[1].get_valid()): return None
  return x

def _geometry(k:Scheduler, end:UOp, output:UOp, a:UOp, b:UOp, rk:UOp):
  ai, bi, ci = (x.src[1].get_idx() for x in (a, b, output))
  aranges, branges = set(ai.ranges), set(bi.ranges)
  mranges, nranges = aranges-{rk}, branges-{rk}
  if len(mranges) != 1 or len(nranges) != 1: return None
  rm, rn = next(iter(mranges)), next(iter(nranges))
  if rm is rn or aranges != {rm, rk} or branges != {rn, rk}: return None
  if set(ci.ranges) != {rm, rn}: return None
  if any(r.op is not Ops.RANGE or r.src[0].op is not Ops.CONST for r in (rm, rn, rk)): return None
  if rm.arg[-1] is not AxisType.GLOBAL or rn.arg[-1] is not AxisType.GLOBAL: return None
  if set(k.rngs) != {rm, rn, rk} or set(end.src[1:]) != {rm, rn}: return None
  M, N, K = (int(r.vmax)+1 for r in (rm, rn, rk))
  if not (_same_index(ai, rm*K+rk) and _same_index(bi, rk*N+rn) and _same_index(ci, rm*N+rn)): return None
  if a.src[0].shape != (M*K,) or b.src[0].shape != (K*N,) or output.src[0].shape != (M*N,): return None
  return M, N, K

def _tile(M:int, N:int, K:int, itemsize:int, shared_max:int, padding:int=1) -> int|None:
  # Conservative policy, not benchmark-derived: limit padding and avoid tiny grids.
  for B in ((32,) if itemsize == 8 else (64, 32)):
    if M < B or N < B or K < 16: continue
    gm, gn, gk = (M+B-1)//B, (N+B-1)//B, (K+15)//16
    if gm*gn < 64 or 4*M*N*K < 3*(gm*B)*(gn*B)*(gk*16): continue
    if 16*(2*B+padding)*itemsize <= shared_max: return B
  return None

def _dot2_product(product:UOp, a:UOp, b:UOp) -> bool:
  if a.dtype != dtypes.half or b.dtype != dtypes.half: return False
  if product.op is Ops.CAST and product.dtype == dtypes.float:
    mul = product.src[0]
    return mul.op is Ops.MUL and mul.dtype == dtypes.half and mul.src in ((a,b), (b,a))
  if product.op is Ops.MUL and product.dtype == dtypes.float:
    return product.src in ((a.cast(dtypes.float), b.cast(dtypes.float)),
                           (b.cast(dtypes.float), a.cast(dtypes.float)))
  return False

def try_gfx906_gemm(k:Scheduler) -> Scheduler|None:
  if k.ren.target.arch.split(":", 1)[0] != "gfx906": return None
  if not k.ren.has_local or not k.ren.has_shared or k.dont_use_locals or k.applied_opts: return None
  ast = k.ast
  topo = list(ast.toposort())
  if any(x.op in (Ops.BUFFER, Ops.STAGE, Ops.WMMA, Ops.SPECIAL) for x in topo): return None
  if sum(x.op is Ops.REDUCE for x in topo) != 1: return None
  if len(ast.src) != 1 or ast.src[0].op is not Ops.END: return None
  end = ast.src[0]
  store = end.src[0]
  if store.op is not Ops.STORE or len(store.src) != 2: return None
  output, original_value = store.src
  if output.op is not Ops.INDEX or len(output.src) != 2: return None
  c = output.src[0]
  if c.op is not Ops.PARAM or c.addrspace is not AddrSpace.GLOBAL or c.arg.volatile: return None
  if output.dtype not in _FLOATS or output.dtype != original_value.dtype: return None
  if not _true(output.src[1].get_valid()): return None

  red = _strip_float_casts(original_value)
  if red is None or red.op is not Ops.REDUCE or red.arg != (Ops.ADD, 0) or len(red.src) != 2: return None
  rk = red.src[1]
  if rk.op is not Ops.RANGE or rk.arg[-1] is not AxisType.REDUCE: return None
  original_product = red.src[0]
  mul = _strip_float_casts(original_product)
  if mul is None or mul.op is not Ops.MUL: return None
  left, right = (_input(x) for x in mul.src)
  if left is None or right is None or left.dtype != right.dtype: return None
  if (left.dtype, red.dtype) not in ((dtypes.half, dtypes.float),
                                    (dtypes.float, dtypes.float),
                                    (dtypes.double, dtypes.double)): return None
  if c in (left.src[0], right.src[0]): return None

  for a, b in ((left, right), (right, left)):
    shape = _geometry(k, end, output, a, b, rk)
    if shape is not None: break
  else:
    return None
  M, N, K = shape
  dot2 = (bool(GFX906_DOT2) and red.dtype == dtypes.float
          and dtypes.half in k.ren.supported_dtypes()
          and dtypes.half not in EMULATED_DTYPES.tolist(dtypes)
          and _dot2_product(original_product, a, b))
  # DOT2 stages adjacent K halves into one LDS word. Spread A's transpose across banks.
  padding = 4 if dot2 else 1
  if (B := _tile(M, N, K, a.dtype.itemsize, k.ren.shared_max, padding)) is None: return None
  T, AS = B//16, B+padding
  # Increase depth only for complete DOT2 tiles; keep the existing tail path.
  BK = 32 if dot2 and K%32 == 0 and 32*(AS+B)*a.dtype.itemsize <= k.ren.shared_max else 16

  tiled = k.copy()
  def rng(size:int, kind:AxisType) -> UOp:
    return UOp.range(size, next(tiled.opt_range), kind)
  gm, gn = rng((M+B-1)//B, AxisType.GLOBAL), rng((N+B-1)//B, AxisType.GLOBAL)
  tx, ty = rng(16, AxisType.LOCAL), rng(16, AxisType.LOCAL)
  local_a = UOp.placeholder((BK*AS,), a.dtype, 0, AddrSpace.LOCAL)
  local_b = UOp.placeholder((BK*B,), b.dtype, 1, AddrSpace.LOCAL)

  # In dot2 mode, the two K values occupy adjacent half elements in LDS.
  def address(kr:UOp, pos:UOp, stride:int) -> UOp:
    return 2*((kr//2)*stride+pos)+kr%2 if dot2 else kr*stride+pos

  # Packed pointer views require the MI50 HIP profile; other renderers retain the existing path.
  from tinygrad.renderer.cstyle import HIPRenderer
  if (getattr(k.ren, "mi50_tuning", False) and MI50_PREFETCH and dot2 and isinstance(k.ren, HIPRenderer)
      and BK == 32 and K > BK and M%B == 0 and N%B == 0):
    nt = K//BK
    kb_main = rng(nt-1, AxisType.LOOP)
    ki_main, ki_last = rng(BK//2, AxisType.LOOP), rng(BK//2, AxisType.LOOP)
    # No REDUCE nodes in this branch: all three REG allocations are explicit.
    acc = UOp.placeholder((T*T,), dtypes.float, 0, AddrSpace.REG)
    pref_a = UOp.placeholder((T,), dtypes.uint, 1, AddrSpace.REG)
    pref_b = UOp.placeholder((T,), dtypes.uint, 2, AddrSpace.REG)
    a_words = a.src[0].bitcast(dtypes.uint).rtag(MI50_PREFETCH_TAG)
    b_words = b.src[0].bitcast(dtypes.uint).rtag(MI50_PREFETCH_TAG)
    q = B//32
    # A: adjacent K halves. B: adjacent N halves, scattered to the existing paired-K LDS layout.
    coords = [(2*ty+tx%2+(j//2)*32, tx//2+(j%2)*8,
               ty+(j//q)*16, 2*tx+(j%q)*32) for j in range(T)]

    def fetch(tile, *deps):
      ap, bp = a_words.after(*deps), b_words.after(*deps)
      writes = []
      for j, (ar, ak, br, bc) in enumerate(coords):
        av = ap.index((gm*B+ar)*(K//2)+tile*(BK//2)+ak).load()
        bv = bp.index((tile*BK+br)*(N//2)+gn*(B//2)+bc//2).load()
        writes.extend((pref_a.index(j).store(av), pref_b.index(j).store(bv)))
      return UOp.group(*writes)

    def stage(pa:UOp, pb:UOp, *deps):
      # Distinguish main and tail stores for memory_coalescing; both name the same LDS buffers.
      da, db = local_a.after(*deps), local_b.after(*deps)
      writes = []
      for j, (ar, ak, br, bc) in enumerate(coords):
        aw, bw = pa.index(j).load(), pb.index(j).load()
        # Bit extraction, not FP16 arithmetic or conversion through float.
        alo, ahi = aw.cast(dtypes.ushort).bitcast(dtypes.half), (aw >> 16).cast(dtypes.ushort).bitcast(dtypes.half)
        blo, bhi = bw.cast(dtypes.ushort).bitcast(dtypes.half), (bw >> 16).cast(dtypes.ushort).bitcast(dtypes.half)
        ai, bi = address(2*ak, ar, AS), address(br, bc, B)
        writes.extend((
          da.index(ai).store(alo), da.index(ai+1).store(ahi),
          db.index(bi).store(blo), db.index(bi+2).store(bhi),
        ))
      return UOp.group(*writes).barrier()

    def accumulate(state:UOp, la:UOp, lb:UOp, kr:UOp):
      updates = []
      for mi in range(T):
        for ni in range(T):
          ai, bi = 2*(kr*AS+ty+mi*16), 2*(kr*B+tx+ni*16)
          dst = state.index(mi*T+ni)
          dot = UOp(Ops.CUSTOM, dtype=dtypes.float, arg="gfx906_dot2", src=(
            la.index(ai).load(), la.index(ai+1).load(),
            lb.index(bi).load(), lb.index(bi+1).load(), dst.load()))
          updates.append(dst.store(dot))
      return UOp.group(*updates).end(kr)

    prime = fetch(0)
    init = UOp.group(*(acc.index(i).store(UOp.const(0.0, dtypes.float)) for i in range(T*T)))
    copied = stage(pref_a.after(prime, kb_main), pref_b.after(prime, kb_main))
    # Every next tile exists. Keep these stores reachable and before current-tile arithmetic.
    prefetched = fetch(kb_main+1, copied)
    ready_a, ready_b = local_a.after(copied, prefetched), local_b.after(copied, prefetched)
    computed = accumulate(acc.after(init, kb_main, ki_main), ready_a, ready_b, ki_main)
    # Explicit WAR barrier also covers K=64, where the main loop folds to one iteration.
    main = computed.barrier().end(kb_main)

    # Drain the final prefetched tile without another fetch, predicate, or zero staging.
    last_copy = stage(pref_a.after(main), pref_b.after(main), main)
    last = accumulate(acc.after(main, ki_last), local_a.after(last_copy), local_b.after(last_copy), ki_last)
    final = acc.after(last)
    stores = []
    for mi in range(T):
      for ni in range(T):
        value = original_value.substitute({red:final.index(mi*T+ni).load()})
        row, col = gm*B+ty+mi*16, gn*B+tx+ni*16
        stores.append(c.index(row*N+col).store(value))
    info = replace(ast.arg or KernelInfo(), useful_ops=2*M*N*K, estimates=None)
    tiled.ast = ast.replace(src=(UOp.group(*stores).end(gm, gn, tx, ty),), arg=info)
    return tiled

  kb, ki = rng((K+BK-1)//BK, AxisType.REDUCE), rng(BK//2 if dot2 else BK, AxisType.REDUCE)
  cp = rng(T*(BK//16), AxisType.LOOP)

  ck, ct = (cp//T)*16, (cp%T)*16
  arow, ak = gm*B+ty+ct, kb*BK+ck+tx
  bk, bcol = kb*BK+ck+ty, gn*B+tx+ct
  copied = UOp.group(
    local_a.index(address(ck+tx, ty+ct, AS)).store(
      a.src[0].index((arow*K+ak).valid((arow<M) & (ak<K)))),
    local_b.index(address(ck+ty, tx+ct, B)).store(
      b.src[0].index((bk*N+bcol).valid((bk<K) & (bcol<N)))),
  ).end(cp)
  ready_a, ready_b = local_a.after(copied), local_b.after(copied)

  def output_store(value:UOp, mi, ni):
    row, col = gm*B+ty+mi*16, gn*B+tx+ni*16
    return c.index((row*N+col).valid((row<M) & (col<N))).store(value)

  if dot2:
    stores = []
    for mi in range(T):
      for ni in range(T):
        ai, bi = 2*(ki*AS+ty+mi*16), 2*(ki*B+tx+ni*16)
        # CUSTOM needs explicit loads; keep each accumulator scalar.
        dot = UOp(Ops.CUSTOM, dtype=dtypes.float, arg="gfx906_dot2", src=(
          ready_a.index(ai).load(), ready_a.index(ai+1).load(),
          ready_b.index(bi).load(), ready_b.index(bi+1).load(),
          UOp.const(0.0, dtypes.float)))
        value = original_value.substitute({red:red.replace(src=(dot, kb, ki))})
        stores.append(output_store(value, mi, ni))
    body = UOp.group(*stores).end(gm, gn, tx, ty)
  else:
    um, un = rng(T, AxisType.UPCAST), rng(T, AxisType.UPCAST)
    product = original_product.substitute({
      a:ready_a.index(ki*AS+ty+um*16),
      b:ready_b.index(ki*B+tx+un*16)})
    value = original_value.substitute({red:red.replace(src=(product, kb, ki))})
    body = output_store(value, um, un).end(gm, gn, tx, ty, um, un)

  info = replace(ast.arg or KernelInfo(), useful_ops=2*M*N*K, estimates=None)
  tiled.ast = ast.replace(src=(body,), arg=info)
  return tiled
