#!/usr/bin/env python3
"""再深入一层：试 cuCtxCreate (老 API) + 查 device attributes 差异候选."""
import ctypes
libcuda = ctypes.CDLL("libcuda.so.1")

def call(name, *args, quiet=False):
    fn = getattr(libcuda, name)
    r = fn(*args)
    if not quiet:
        print(f"  {name} -> {r}")
    return r

call("cuInit", 0)
dev = ctypes.c_int()
call("cuDeviceGet", ctypes.byref(dev), 0)

# 关键 attribute 探测（回避 primary context）
attrs = {
    75: "COMPUTE_CAPABILITY_MAJOR",
    76: "COMPUTE_CAPABILITY_MINOR",
    82: "UNIFIED_ADDRESSING",
    95: "CONCURRENT_MANAGED_ACCESS",
    99: "COMPUTE_PREEMPTION_SUPPORTED",
    116: "MEMPOOL_SUPPORTED_HANDLE_TYPES",
    121: "GPU_DIRECT_RDMA_SUPPORTED",
    138: "MEMORY_POOLS_SUPPORTED",
    130: "HANDLE_TYPE_POSIX_FILE_DESCRIPTOR_SUPPORTED",
}
for k, name in attrs.items():
    v = ctypes.c_int()
    r = libcuda.cuDeviceGetAttribute(ctypes.byref(v), k, dev)
    print(f"  attr[{k}={name}] = {v.value}  (rc={r})")

# 老式 cuCtxCreate (不走 primary ctx)
print("\n== cuCtxCreate (老式 non-primary) ==")
ctx = ctypes.c_void_p()
r = libcuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev)  # v2 是标准 API
print(f"  cuCtxCreate_v2(flags=0) -> {r}  ctx=0x{ctx.value or 0:x}")
if r == 0:
    print("  ✓ 老式 cuCtxCreate 可以，说明 PRIMARY ctx 特有 bug")
    libcuda.cuCtxDestroy_v2(ctx)
else:
    print("  ✗ 老式 API 也挂，driver 层完全没法建 context")

# 也尝试 flag=CU_CTX_SCHED_AUTO
print("\n== cuCtxCreate flag=SCHED_AUTO ==")
ctx = ctypes.c_void_p()
r = libcuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev)
print(f"  -> {r}")
if r == 0:
    libcuda.cuCtxDestroy_v2(ctx)

# 尝试 cuDevicePrimaryCtxSetFlags
print("\n== cuDevicePrimaryCtxSetFlags(SCHED_AUTO=0) then Retain ==")
r = libcuda.cuDevicePrimaryCtxSetFlags(dev, 0)
print(f"  cuDevicePrimaryCtxSetFlags -> {r}")
ctx2 = ctypes.c_void_p()
r = libcuda.cuDevicePrimaryCtxRetain(ctypes.byref(ctx2), dev)
print(f"  cuDevicePrimaryCtxRetain after SetFlags -> {r}")
