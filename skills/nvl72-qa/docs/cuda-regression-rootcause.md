# CUDA regression 根因分析（GKE node image 4681000 nvidia.ko）

**结论层次**：`libcuda ↔ NVIDIA kernel driver` 的 context-create ioctl，driver silent refuse
**单变量已隔离**：唯一差异是 `nvidia.ko` 二进制本身（Jun 27 build vs Jun 18 build），其他所有维度已验证一致
**修复策略**：绕开（pin node version 到 `1.36.0-gke.4447000`）+ 冻结 auto-upgrade（cluster 维护例外 `freeze-node-upgrades-ko-regression` 至 2026-10-23）
**别再重排查**：见文末 §"已排除的退路"

---

## 现象

GKE node image `1.36.0-gke.4681000`（COS `19506.224.80`）上的 nvidia.ko（build `Sat Jun 27`）导致 GB300 上**所有 GPU context 创建失败**：
- `cuDevicePrimaryCtxRetain` / `cuCtxCreate_v2` → `CUDA_ERROR_INVALID_VALUE (1)`
- `cuInit` / `cuDeviceGetCount` / `cuDeviceGetUuid` / `nvidia-smi` 全部正常（表面看节点是健康的）
- DCGM Level 2 / NCCL-tests / cuBLAS / 训练脚本 全部挂
- dmesg 无任何新 NVRM message（driver silent refuse）

已知**正常**：`1.36.0-gke.4447000`（COS `19506.224.49`，nvidia.ko build `Thu Jun 18`）

## 层次隔离验证（Python ctypes 直调 libcuda）

用 `probe/cuprobe.py` + `probe/cuprobe2.py`，起 privileged probe pod（`probe/probe-newold.yaml`），两台节点同 image、同 pod spec、`nvidia.com/gpu: 4`：

| CUDA API | probe-new (broken) | probe-old (工作) |
|---|---|---|
| `cuInit(0)` | 0 ✓ | 0 ✓ |
| `cuDeviceGetCount()` = 4 | 0 ✓ | 0 ✓ |
| `cuDeviceGet(dev, 0)` | 0 ✓ | 0 ✓ |
| `cuDeviceGetName` (返 "NVIDIA GB300") | 0 ✓ | 0 ✓ |
| `cuDeviceGetAttribute` × 9 项 | 0 ✓ 完全一致 | 0 ✓ |
| `cuDevicePrimaryCtxGetState` (flags=0, active=0) | 0 ✓ | 0 ✓ |
| `cuDevicePrimaryCtxSetFlags(0)` | 0 ✓ | 0 ✓ |
| **`cuDevicePrimaryCtxRetain`** | **1 (INVALID_VALUE)** | 0 ✓ |
| **`cuCtxCreate_v2(flags=0)`** | **1 (INVALID_VALUE)** | 0 ✓ |

`cuCtxCreate_v2` 老 API 也挂 → 不是 primary ctx 特有，**所有 GPU context 创建都在 driver 层被 refuse**。

## 单变量隔离（2026-07-25 完成，别重做）

跨 pool-0013/pool-0015/pool-0016（坏）与 pool-0005/pool-0007（好，当时还没释放）对照，全部一致、可排除的项：

- node capacity/allocatable（含 hugepages-2Mi = 8Gi）
- 全部 DaemonSet
- DRA ResourceSlice
- 容器内 `/dev/nvidia*` 与 2048 个 IMEX channel
- 实际加载的 libcuda 路径
- **libcuda.so.580.159.04 md5 完全相同**
- **gsp_ga10x.bin md5 完全相同**
- 全部 `NVreg_*` 模块参数
- `lsmod` 已加载模块集合
- GPU fabric / ECC / compute mode

**唯一差异**：`nvidia.ko` 二进制本身
- 好：`580.159.04 Release Build builder@1bd82b8cbd9c Thu Jun 18 02:49`，md5 `9442ea759c7c9a6e1c3d00fd816b3d86`
- 坏：`580.159.04 Release Build builder@e041cd032e3a Sat Jun 27 15:18`，md5 `66ae3d557214ecef9b5ad4b50a1480cb`

同版本号（580.159.04），不同 build environment。

### strace 观察

- 两边**没有任何 ioctl 返回 -1**（driver 层不返回 syscall error）
- 失败在 NVIDIA RM control 返回结构体内部的 `status` 字段
- ioctl 计数：bad 401 / good 1050 —— 坏节点在 context 创建流程中途被 driver abort

### 容易误判的陷阱（都已排除）

1. **`NVRM: _gpuFabricProbeRbmSleepLinks: Error setting links to sleep on linkmask 0x0`** —— 好坏节点都有，是无害噪声，**不是根因**
2. **`nvidia-imex` 运行状态** —— 好节点上常跑着 imex（因为有 ComputeDomain），坏节点没有，但 2×2 对照（pool-0005/0007 imex=0 仍 OK，pool-0015/0016 imex=0 FAIL）已证明 **IMEX/ComputeDomain 与此无关**

## 已排除的退路（2026-07-25 实测，别再试）

1. **`gpu-driver-version=default` 而不是 `latest`** —— 无效。坏节点上 `/home/kubernetes/bin/nvidia/gpu_driver_versions.bin`（protobuf）明确写着 `NVIDIA_GB300: LATEST=580.159.04, DEFAULT=580.159.04`，两者同一个 broken build，节点上也只预置了这一个驱动包。改完滚一遍白滚。
2. **在坏节点上 rmmod + modprobe nvidia** —— 无效，同一个 broken .ko 重载。且 rmmod 需要先赶走所有 GPU workload（device-plugin / DRA / asapd-lite / dcgm-exporter），破坏性大。
3. **停 nvidia-persistenced / 关 Persistence Mode** —— 无效，独立 daemon，跟 primary ctx 无关。
4. **手工替换 nvidia.ko（从好节点拷）** —— 未验证，理论上 vermagic 会 mismatch（kernel SMP build 时间也不同）。且 rmmod 前置条件同 #2。
5. **cluster 切 REGULAR channel + pin 4447000** —— 之前 handoff 里写过这条路，其实**不需要**。GKE 判据是 `validNodeVersions`，不是 channel 的 `validVersions`。4447000 虽不在 RAPID validVersions 但在 validNodeVersions 里，GKE 接受。已在 2026-07-25 用 0 节点测试 pool 实测。

## 修复策略（当前采用）

1. **建 pool 时 pin `--node-version=1.36.0-gke.4447000`**（脚本已 hardcode 默认值到 `GKE_NODE_VERSION`）
2. **建 pool 时加 `--no-enable-autoupgrade`**（若 GKE 拒绝会 fallback，`scripts/gke-create-nodepool.sh` 的 `do_create_pool` 函数已实现）
3. **cluster 加维护例外 `freeze-node-upgrades-ko-regression`**
   - `scope=NO_MINOR_OR_NODE_UPGRADES`
   - `2026-07-25 → 2026-10-23`
   - 挡 auto-upgrade 但**不改 pool 配置版本**
   - 到期前需要重新评估
4. **建 pool 后必须 `--verify-only` 核对 MIG 实际 COS 镜像**
   - `--node-version` 只是"请求"，真正决定节点装什么的是 MIG 的 regional instance template
   - 2026-07-19 GKE 曾给每个 pool 生成新 template 指向坏 COS，一半 MIG 被切过去 —— 必须核对

真正挡不住 broken 节点重新出现的路径：**主机故障 → 实例 TERMINATE/DELETE → MIG 为维持 target size 补建 → 用当前 template**。这就是老 pool（pool-0002 的 `lcg3`、pool-0006 的 `33qv`）变坏的原因（它们是硬件故障节点，MIG 补建用了坏 template）。

想根治只有：等 NVIDIA/GKE 出新 image（nvidia.ko build 修复），或者把老 pool 的 MIG template 手工指回好 image 后重建全部节点。

## 你要不要自己复现

**不要重复排查**。根因已定案，单变量隔离已完成。

如果要 verify 现状（比如新 pool 建完想确认是好节点）：
```bash
# 在 handoff 包目录里，改 probe-newold.yaml 里 probe-new 的 nodeName 到目标节点
kubectl apply -f probe/probe-newold.yaml
kubectl -n cuda-probe exec probe-new -- python3 /tmp/cuprobe.py | grep cuDevicePrimaryCtxRetain
# 期望：全部 4 GPU return 0（cuDevicePrimaryCtxRetain -> 0）
# 如果 return 1，说明节点跑的还是坏 image
kubectl delete ns cuda-probe --wait=false
```

或者更简单，查节点 kubelet 版本 = 4447000 = 好，= 4681000 = 坏。
