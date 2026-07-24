---
name: gke-dsv3-training-lessons
description: GKE GB300 DSv3 256GPU 训练全流程：1618 TFLOPs 达成（98.2% NVIDIA 参考值），含完整 env/CD/launcher/踩坑
metadata: 
  node_type: memory
  type: project
  originSessionId: bd18a3c2-5c82-45a6-becf-760d64047093
---

## GKE GB300 DSv3 256 GPU 训练经验（2026-07-15~17）

### 最终成果

**1618 MODEL TFLOP/s/GPU**（NVIDIA 参考值 1648 = 98.2%），full_iteration CUDA graph，GBS=4096，10 步稳定运行。

### 1. 完整 perf env（最关键的发现）

`run_script.py` torchrun 直跑**不会**自动设 perf env（那是 nemo-run launcher 侧 `perf_plugins.py` 的活）。必须手动 export 全部变量。

**full_iteration graph 必需（历史一直 OOM/crash 的根因）**：

| env | 值 | 作用 |
|-----|-----|------|
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:True,graph_capture_record_stream_reuse:True` | **必须两个都有**。只有 expandable_segments 还是 OOM |
| `TORCH_NCCL_AVOID_RECORD_STREAMS` | `0` | full graph 下必须 0（默认 1），否则 `StreamCaptureUnjoined` |

**HybridEP NVL domain**：

| env | 值 |
|-----|-----|
| `NVLINK_DOMAIN_SIZE` | `72` |
| `USE_MNNVL` | `1` |
| `NUM_OF_HYBRID_EP_RANKS_PER_NVLINK_DOMAIN` | `32`（== EP size，不是 8） |
| `NUM_OF_TOKENS_PER_CHUNK_COMBINE_API` | `128` |

**其他必需**：`NVTE_ALLOW_NONDETERMINISTIC_ALGO=0`, `NVTE_NORM_FWD_USE_CUDNN=1`, `NVTE_NORM_BWD_USE_CUDNN=1`, `CUDA_DEVICE_MAX_CONNECTIONS=32`, `NVTE_FWD/BWD_LAYERNORM_SM_MARGIN=20`, `NVTE_CUTEDSL_FUSED_GROUPED_MLP=1`, `CUDNNFE_CLUSTER_OVERLAP_MARGIN=8`

**NCCL**：`NCCL_CONF_FILE=/usr/local/gib/configs/nccl.a4xmax.conf`, `NCCL_NVLS_ENABLE=0`, `NCCL_GRAPH_REGISTER=0`, `NCCL_IB_SPLIT_DATA_ON_QPS=1`, `NCCL_CTA_POLICY=1`

完整脚本：`scripts/run-dsv3-gke.sh`

### 2. 绝不覆盖 recipe 参数

- **不加** `model.cuda_graph_impl=...` / `model.cuda_graph_scope=...` — 会绕过 `moe_paged_stash` 等 full_iteration 依赖
- **不加** `model.recompute_modules=...` — recipe 默认无 recompute（显存够用）
- **用 `-cv v2`** 选 GBS=4096 config variant
- 让 Bridge native recipe 全权控制

### 3. ComputeDomain 配置

- 每个 NVLink domain 一个 CD，`numNodes: 0`
- **channel template 由 CD 控制器自动创建**（不要手动建，会冲突）
- `numNodes=0` 有死锁风险：CD 不预分配节点 → pod 调度不上 → 需配合 pool nodeSelector
- 需要 `ComputeDomainClique` CRD（v0.4.1 新增，Helm upgrade 不自动装）

### 4. 节点调度

**pool nodeSelector + podAffinity 双保证**：
- `cloud.google.com/gke-nodepool: <pool>` — 每 pool = 1 subblock = 1 NVLink domain
- `podAffinity topologyKey: cloud.google.com/gce-topology-subblock` — 同组同域
- `podAntiAffinity topologyKey: kubernetes.io/hostname` — 每节点 1 pod
- 每个 pool 必须有 **16+ 可调度 + plugin-ready 节点**，否则 pod 调度不满

**当前 team=gdde pools**: 0001(17), 0004(18), 0006(16), 0009(16)
**弃用**: pool-0002（3r0c GPU0 坏 + lcg3 3GPU，可用 <16）

### 5. Launcher 架构

**head pod TCP trigger**（解决 kubectl exec timeout）：
1. 每个 pod 跑 `pod-listener.py`（TCP :18888），等指令
2. 训练脚本通过 ConfigMap 挂载到 `/opt/run-script/`
3. 启动：`kubectl exec dsv3-a-0 -- python3 /opt/run-script/head-trigger.py run 10`
4. head pod 通过集群内网发指令到 64 pod，64/64 一次成功

**不要用** kubectl exec 逐 pod 启动 — GKE public endpoint 不稳定，64 个 exec 大量 timeout

### 6. DRA GPU Driver

- v0.4.1（从 v25.8.0 升级）
- 需手动装 `ComputeDomainClique` CRD
- `imex-channel-init` DaemonSet 必须在所有 GPU 节点运行
- 部分节点（缺 IMEX channels）kubelet-plugin crash `unexpected number of unique CliqueIDs`

### 7. AR Pull Secret

详见 [[ar-secret-refresher-pattern]]
- CronJob 每 45 分钟刷新，用 `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE`
- `google/cloud-sdk:latest`（不是 slim，slim 没 kubectl）

### 8. 踩坑汇总

| 问题 | 根因 | 解法 |
|------|------|------|
| full_iteration OOM | 缺 `graph_capture_record_stream_reuse:True` | 完整 perf env |
| StreamCaptureUnjoined | 缺 `TORCH_NCCL_AVOID_RECORD_STREAMS=0` | 同上 |
| TE graph override crash | 覆盖 cuda_graph_impl 破坏 paged_stash | 不覆盖 recipe |
| CD 跨域 | 无 podAffinity → CD 节点跨多个 subblock | pool nodeSelector + podAffinity |
| CD 死锁 | numNodes=0 不预分配 | pool nodeSelector 保证节点够 |
| NCCL rendezvous hang | 部分 pod 没启动 torchrun | head pod trigger 保证 64/64 |
| kubectl exec timeout | GKE public endpoint 不稳定 | head pod 集群内网通信 |
| NCCL_CONF_FILE 路径错 | `/usr/local/gib/scripts/` 不存在 | 改 `/usr/local/gib/configs/nccl.a4xmax.conf` |
| bash GROUPS 变量 | bash 内置只读变量 | 改名 STS_GROUPS |
| AR token 过期 | `authorized_user` 凭证不兼容 `activate-service-account` | `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` |

### 9. 文件清单

| 文件 | 用途 |
|------|------|
| `yamls/gke-dsv3-256gpu.yaml` | 4-CD StatefulSet pool（pod-listener + ConfigMap） |
| `scripts/run-dsv3-gke.sh` | Pod 内训练脚本（完整 perf env + native recipe） |
| `scripts/pod-listener.py` | Pod TCP listener（:18888，等 head 指令） |
| `scripts/head-trigger.py` | Head pod 触发脚本（run/kill/status） |
| `scripts/launch-dsv3-gke.sh` | 本地 launcher（调用 head-trigger） |

### 10. 参考

- [yangwhale DSv3 671B GKE guide](https://github.com/yangwhale/gpu-tpu-pedia/blob/main/gpu/a4x-max/07-megatron-training/07e-gb300-deepseekv3-671b-gke/README.md) — 首次跑通的参考文档
- [NVIDIA Performance Summary](https://docs.nvidia.com/nemo/megatron-bridge/latest/performance-summary.html) — 官方 1648 TFLOPs 参考值
