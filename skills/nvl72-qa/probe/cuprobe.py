#!/usr/bin/env python3
"""层次化 CUDA driver API probe: cuInit / DeviceGet / PrimaryCtxRetain / PrimaryCtxGetState."""
import ctypes, sys
libcuda = ctypes.CDLL("libcuda.so.1")

def call(name, *args):
    fn = getattr(libcuda, name)
    r = fn(*args)
    print(f"  {name}({', '.join(str(a) for a in args)}) -> {r}")
    return r

print("== cuInit ==")
call("cuInit", 0)

print("== cuDeviceGetCount ==")
n = ctypes.c_int()
call("cuDeviceGetCount", ctypes.byref(n))
print("  count =", n.value)

for i in range(n.value):
    print(f"== GPU {i} ==")
    dev = ctypes.c_int()
    call("cuDeviceGet", ctypes.byref(dev), i)
    name = ctypes.create_string_buffer(128)
    call("cuDeviceGetName", name, 128, dev)
    print("  name =", name.value.decode())

    # PrimaryCtx state before retain
    flags = ctypes.c_uint()
    active = ctypes.c_int()
    call("cuDevicePrimaryCtxGetState", dev, ctypes.byref(flags), ctypes.byref(active))
    print(f"  before-retain: flags=0x{flags.value:x} active={active.value}")

    # Retain
    ctx = ctypes.c_void_p()
    r = call("cuDevicePrimaryCtxRetain", ctypes.byref(ctx), dev)
    if r == 0:
        print(f"  retained ctx=0x{ctx.value:x}")
        # release
        call("cuDevicePrimaryCtxRelease", dev)

print("DONE")
