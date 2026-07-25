---
name: gke-4681000-nvidia-ko-regression
description: GKE node image 1.36.0-gke.4681000 的 nvidia.ko (Jun 27 build) 在 GB300 上 CUDA context 创建全挂；已单变量隔离确认，环境侧无需修改
metadata: 
  node_type: memory
  type: project
  originSessionId: 1afc4ebe-b7cf-4541-8ddb-16d13c168ac1
  modified: 2026-07-25T05:22:28.574Z
---

GKE node image `1.36.0-gke.4681000`（COS `19506.224.80`）里重新构建的 `nvidia.ko` 580.159.04（build `Sat Jun 27`，md5 `66ae3d557214ecef9b5ad4b50a1480cb`）在 GB300 上导致 **所有 CUDA context 创建失败** —— `cuDevicePrimaryCtxRetain` / `cuCtxCreate_v2` 返回 `CUDA_ERROR_INVALID_VALUE (1)`。`cuInit` / `cuDeviceGetCount` / `cuDeviceGetUuid` / `nvidia-smi` 全部正常，所以表面看节点是健康的，但 DCGM Level 2 / NCCL-tests / cuBLAS / 任何训练负载全挂。

已知正常：`1.36.0-gke.4447000`（COS `19506.224.49`，nvidia.ko build `Thu Jun 18`，md5 `9442ea759c7c9a6e1c3d00fd816b3d86`）。

**2026-07-25 完成单变量隔离（不要再重做这套排查）。** 已确认两边完全一致、可排除的项：node capacity/allocatable（含 hugepages-2Mi 8Gi）、全部 DaemonSet、DRA ResourceSlice、容器内 `/dev/nvidia*` 与 2048 个 IMEX channel、实际加载的 libcuda 路径、**libcuda.so.580.159.04 与 gsp_ga10x.bin 的 md5 完全相同**、全部 `NVreg_*` 模块参数、`lsmod` 已加载模块集合、GPU fabric/ECC/compute mode。唯一差异就是 `nvidia.ko` 二进制本身。

两个容易误判的陷阱：
- `NVRM: _gpuFabricProbeRbmSleepLinks: Error setting links to sleep` 在**好坏节点都有**，是无害噪声，不是根因。
- 好节点上常跑着 `nvidia-imex`（因为有 ComputeDomain），坏节点没有 —— 但 2×2 对照（pool-0005/0007 imex=0 仍 OK，pool-0015/0016 imex=0 FAIL）已证明 **IMEX/ComputeDomain 与此无关**。

strace 显示两边**没有任何 ioctl 返回 -1**，失败在 NVIDIA RM control 返回结构体内部的 status；ioctl 计数 bad 401 / good 1050，坏节点在 context 创建中途被驱动 abort。

**已排除的退路（2026-07-25 实测，别再试）**：改 `gpu-driver-version` 从 `latest` 到 `default` 没用。`gcloud container node-pools update --accelerator=...` 虽然支持原地改（不用删重建 pool，但节点会被滚重建），可坏节点上的 `/home/kubernetes/bin/nvidia/gpu_driver_versions.bin`（protobuf）明确写着 `NVIDIA_GB300: LATEST=580.159.04, DEFAULT=580.159.04` —— 两者同一个 broken build，节点上也只预置了这一个驱动包。滚一遍白滚。

**建 pool 直接 pin 4447000 就行，不用切 channel**（2026-07-25 用 0 节点测试 pool 实测）：cluster 在 RAPID、`1.36.0-gke.4447000` 不在 RAPID validVersions 但在 `validNodeVersions` 里，**GKE 照样接受并建出 pool**。判据是 `validNodeVersions`，不是 channel 的 `validVersions` —— `scripts/gke-create-nodepool.sh` 的 preflight 原先判错导致误 abort，已修。

**判断一个 pool 会不会建出坏节点，要看 MIG 的 regional instance template 指向哪个镜像，不是看 pool 的 `version` 字段，也不是看现有节点跑什么。** 2026-07-19 GKE 给每个 pool 都生成了指向坏 COS 的新 template（旧的没删），但只有一半 MIG 被切过去 —— 所以同一时刻有的 pool 重建出好节点、有的出坏节点。核对命令已固化进 `scripts/gke-create-nodepool.sh --verify-only`。

**存量老 pool 的 `management.autoUpgrade=true`，但这不是 GKE 强制的** —— 2026-07-25 建 pool-0013 时带 `--no-enable-autoupgrade` 被正常接受，新 pool 是 `autoUpgrade=false`。老 pool 是 true 只因为创建时脚本还没这个 flag。（早前"channel 内关不掉 auto-upgrade"的判断是错的。）
另外 `autoRepair` 全部已是 false。真正挡不住的重建路径是：主机故障 → 实例 TERMINATE/DELETE → MIG 为维持 target size 补建 → 用当前 template。pool-0002 的 `lcg3`、pool-0006 的 `33qv` 就是这么变坏的（它们是硬件故障节点，不是被 auto-upgrade 滚的，别混淆）。已加 cluster 级维护例外 `freeze-node-upgrades-ko-regression`（scope `no_minor_or_node_upgrades`，2026-07-25 → **2026-10-23 到期**）冻结 node upgrade，控制面 patch 仍放行；它挡 auto-upgrade 但**不改 pool 配置版本**，根治仍需重建 pool 到好版本。解除：`--remove-maintenance-exclusion=freeze-node-upgrades-ko-regression`。

滚节点时注意：COLLOCATED placement policy 卡 18 台上限，必须带 `--max-surge-upgrade=0 --max-unavailable-upgrade=1`。

相关：[[gb300-cluster-state]]、[[qa-toolkit-design]]、[[gke-dra-imex-cliqueid]]
