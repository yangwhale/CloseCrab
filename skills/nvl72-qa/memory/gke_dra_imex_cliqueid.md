---
name: gke-dra-imex-cliqueid
description: GKE DRA v0.4.1 升级踩坑：ComputeDomainClique CRD 缺失 + 部分节点 IMEX channels 未初始化导致 kubelet-plugin crash
metadata: 
  node_type: memory
  type: project
  originSessionId: bd18a3c2-5c82-45a6-becf-760d64047093
---

## DRA GPU Driver v25.8.0 → v0.4.1 升级踩坑（2026-07-15）

### 问题 1: ComputeDomainClique CRD 缺失

Helm upgrade 不自动安装新 CRD。v0.4.1 新增 `ComputeDomainClique` CRD，控制器启动后持续报 `the server could not find the requested resource (get computedomaincliques.resource.nvidia.com)`，导致 CD 控制器不工作（不 patch channel template、不创建 CD daemon DaemonSet）。

**修法**：手动 `kubectl apply -f` CRD 目录（`deployments/helm/dra-driver-nvidia-gpu/crds/`）。

### 问题 2: 部分节点缺少 /dev/nvidia-caps-imex-channels/

8 个节点（pool-0002 全部 6 个 + pool-0001 的 vcxp/blvd）kubelet-plugin 启动时报：
```
Error: error creating driver: error getting cliqueID: unexpected number of unique CliqueIDs found on devices
```

根因：节点上 `/dev/nvidia-caps-imex-channels/` 目录不存在。4 颗 GPU 各报不同 numeric cliqueID（但 clique UUID 相同）。v25.8.0 不检查 cliqueID 一致性所以正常，v0.4.1 加了严格校验。

工作正常的节点有 `/dev/nvidia-caps-imex-channels/channel0`（char 240,0），由 NVIDIA 驱动创建（不是内核模块也不是 IMEX daemon）。

**处理**：删除了 pool-0002（6 节点）。pool-0001 的 vcxp/blvd 本来就 SchedulingDisabled。

**Why:** v0.4.1 的 compute-domains 容器对 cliqueID 有严格一致性校验，IMEX channels device 未初始化的节点无法通过。这是 v25.8.0 → v0.4.1 的 breaking change。

**How to apply:** 升级 DRA driver 后检查所有节点 kubelet-plugin Ready 状态。不 Ready 的节点检查 `/dev/nvidia-caps-imex-channels/` 是否存在。新建的 node pool 可能需要一次 workload 触发（或 v25.8.0 先跑一次）来初始化 IMEX channels。

### 问题 3: channel template domainID 注入

v0.4.1 + CRD 安装后，CD 控制器会自动 patch channel ResourceClaimTemplate（注入 domainID + labels + config block）。但 ResourceClaimTemplate spec 不可变 — 如果 pod 已经从未 patch 的 template 创建了 claim，必须删 StatefulSet + channel template 重建。

### 问题 4: bash GROUPS 变量

`GROUPS` 是 bash 内置只读变量（当前用户的 group ID 列表），赋值被静默忽略。脚本中不要用 `GROUPS` 做变量名。
