# CUDA regression 根因分析（pool-0013~0017 无法跑 CUDA workload）

**诊断时间**：2026-07-24（张量企鹅调研）
**结论层次**：`libcuda ↔ NVIDIA kernel driver` 的 context-create ioctl，driver silent refuse

---

## 现象

pool-0013~0017 上任何需要创建 GPU CUDA context 的 workload 都会挂：
- DCGM Level 2（`cudaStreamCreate` fail）
- NCCL-tests（`cudaSetDevice` 报 `CUDA-capable device(s) is/are busy or unavailable`）
- cuBLAS benchmark（同上，pod 直接 CrashLoopBackOff）
- 训练脚本（Megatron / DeepSeek 等）

但**读 sysfs / 不真正建 context 的操作是 OK 的**：
- `hw-check` 全部通过（nvidia-smi -q, /sys/class/infiniband/*, /proc/driver/nvidia/*）
- `asapd-lite`（RDMA config daemon）Running
- `dra.net`（12 devices 全部识别）
- `nvidia-fabricmanager`（Fabric State=Completed, Status=Success）

## 层次隔离验证（Python ctypes 直调 libcuda）

用 `probe/cuprobe.py` + `probe/cuprobe2.py`，起 privileged probe pod（`probe/probe-newold.yaml`），两台节点同 image、同 pod spec、`nvidia.com/gpu: 4`：
- `probe-new`: pool-0013 节点，kubelet 4681000, kernel Jun 27
- `probe-old`: pool-0002 节点，kubelet 4447000, kernel Jun 18

| CUDA API | probe-new (broken) | probe-old (工作) |
|---|---|---|
| `cuInit(0)` | 0 ✓ | 0 ✓ |
| `cuDeviceGetCount()` = 4 | 0 ✓ | 0 ✓ |
| `cuDeviceGet(dev, 0)` | 0 ✓ | 0 ✓ |
| `cuDeviceGetName` (返 "NVIDIA GB300") | 0 ✓ | 0 ✓ |
| `cuDeviceGetAttribute` × 9 项（COMPUTE_CAPABILITY=10.3 等） | 0 ✓ 完全一致 | 0 ✓ |
| `cuDevicePrimaryCtxGetState` (flags=0, active=0) | 0 ✓ | 0 ✓ |
| `cuDevicePrimaryCtxSetFlags(0)` | 0 ✓ | 0 ✓ |
| **`cuDevicePrimaryCtxRetain`** | **1 (INVALID_VALUE)** | 0 ✓ ctx=0xb675... |
| **`cuCtxCreate_v2(flags=0)`** | **1 (INVALID_VALUE)** | 0 ✓ |

**关键**：`cuCtxCreate_v2` 老 API 也挂 → 不是 primary ctx 特有的问题，**所有 GPU context 创建都在 driver 层被 refuse**。dmesg 中没有任何新 NVRM message（driver silent refuse，不打 log）。

## 已排除的其他候选（跑过 runtime 干预验证）

在 probe-new 上 cordon 后逐项试：

| 候选 | 干预方法 | 结果 |
|---|---|---|
| Persistence Mode Enabled | `nvidia-smi -pm 0` 全 4 GPU Disabled | 仍 `-> 1` ✗ |
| nvidia-persistenced daemon | `systemctl stop nvidia-persistenced` | 仍 `-> 1` ✗ |
| GPU busy / stale context | 检查 `nvidia-smi --query-gpu=memory.used` = 0 MiB | 排除，GPU 完全空闲 |
| hugepages 缺失 | `kubectl get node -o jsonpath='{.status.capacity.hugepages-2Mi}'` = 8Gi | 排除，已修 |
| Fabric State 未 Ready | `nvidia-smi -q` 显示 State=Completed, Status=Success | 排除 |
| ipam.service 挂 | 看着 activating 但每 20s finish successfully，是 timer 正常行为 | 排除 |
| ipvlan / dra.net 缺 device | pool-0013 12 devices（跟 pool-0002 一样） | 排除 |

## 唯一软件层无法 runtime 修改的差异

| 维度 | pool-0002-1zt9 (OLD, 工作) | pool-0013-0199 (NEW, 挂) |
|---|---|---|
| GKE kubelet | v1.36.0-gke.**4447000** | v1.36.0-gke.**4681000** |
| COS build | 19506.**224.49** | 19506.**224.80** |
| kernel SMP build | Thu Jun 18 02:30:11 UTC 2026 | **Sat Jun 27 14:51:37 UTC 2026** |
| nvidia.ko build | `builder@1bd82b8cbd9c Jun 18 02:49` | **`builder@e041cd032e3a Jun 27 15:18`** |
| nvidia driver ver | 580.159.04 | 580.159.04（同版本号，不同 build environment） |

nvidia 的 open kernel module 会跟当前 kernel 一起在 GKE image 打包时重新编译，所以 kernel 换了 driver 二进制也换。**Jun 27 那次 build 引入了 GPU context-create 的 regression**。

## 前面的错误归因（历史包袱，读 ops log 时会看到）

- **error 1**：一开始以为是"5 subblock 并行 all-full 撞 GPU"—— 后来顺序跑发现 DCGM/NCCL wrapper "pass" 但 detail log 全 CUDA busy，wrapper 假 pass
- **error 2**：怀疑 "kernel .ko Jun 27 build regression" —— 后来查到 hugepages 缺失（linuxNodeConfig=null）能解释 asapd/DRA/nccl-multi 层，被归为 red herring
- **error 3**：重建 pool-0013 hugepages 修好后又发现 cuBLAS 仍全 CrashLoop，重新回到 "kernel .ko regression"

**真相是两层根因叠加**：
1. **hugepages 缺失**（原始 create 漏了 `--system-config-from-file`，已用固化脚本 `scripts/gke-create-nodepool.sh` 修）→ 影响 asapd-lite / DRA / gpu*ipvlan / nccl-multi
2. **kernel .ko Jun 27 regression**（GKE 4681000 image / NVIDIA 580.159.04 open kernel module 特定 build）→ 影响所有 GPU context 创建 → DCGM Level 2 / NCCL-tests / cuBLAS / 训练

hugepages 已修，剩下的第 2 层只能换 image 版本或等 NVIDIA/GKE 出 fix。

## 老 pool 为什么能用（不要被误导）

`gcloud container node-pools list` 显示 pool-0002 的 `NODE_VERSION` 是 `4681000`（跟随 auto-upgrade），**但实际节点上跑的 kubelet 是 `4447000`**（老 image）—— 因为 pool 没被 rolling recreate 过，节点保留了初次 create 时的 image。

判断某个 node 有没有这个 regression：
```bash
kubectl get node <NAME> -o jsonpath='{.status.nodeInfo.kubeletVersion}{"\n"}'
# 4447000 = 老 image，CUDA 可用
# 4681000 = 新 image，CUDA broken
```

## 你要不要自己复现？

不需要。如果要 verify：
```bash
# 在 handoff 包目录里
kubectl apply -f probe/probe-newold.yaml
# 等 pod Ready 后
kubectl cp probe/cuprobe.py cuda-probe/probe-new:/tmp/cuprobe.py
kubectl -n cuda-probe exec probe-new -- python3 /tmp/cuprobe.py | grep cuDevicePrimaryCtxRetain
# 期望看到全部 4 GPU return 1 (INVALID_VALUE)
kubectl -n cuda-probe exec probe-old -- python3 /tmp/cuprobe.py | grep cuDevicePrimaryCtxRetain
# 期望看到全部 4 GPU return 0
# 清理
kubectl delete ns cuda-probe --wait=false
```
