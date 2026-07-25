---
name: gb300-cluster-state
description: GB300 GKE 集群状态：2026-07-25 释放 7 个 sub-block 后仅剩 3 pool / 57 节点，含故障节点与坏 COS 分布
metadata: 
  node_type: memory
  type: project
  originSessionId: 1d46131b-e454-47aa-9615-b08009477d3c
  modified: 2026-07-25T07:01:14.421Z
---

## GKE 集群状态（2026-07-25 04:16，大规模释放后）

集群 `gb300-gke-test` / GCP project `tencent-gcp-taiji-poc`

**2026-07-25 一次性释放 7 个 sub-block（126 节点 / 504 GPU）**：d0001 / d0003 / d0004 / d0005 / d0007 / d0010 / d0012。集群从 10 个 GB300 pool / 183 节点 / 720 GPU 缩到：

| Pool | 节点 | 占用 | cordon | MIG COS | team |
|---|---|---|---|---|---|
| `gb300-pool-0002` | 18 | 4 pod / 4 节点（`nrl-0`~`3` + `nrl-cd`） | 1（`lcg3`） | ❌ 224.80 | yangwhale |
| `gb300-pool-0006` | 18 | 17 pod / 10 节点（dspark + sglang/vllm + 5 CD） | 2（`33qv` `qx2s`） | ❌ 224.80 | infer |
| `gb300-pool-0009` | 18 | **0（完全空闲）** | 1（`36wz`） | ✓ 224.49 | 无（label 已移除） |
| `default-pool` | 3 (e2) | `dynamo-etcd` / `dynamo-nats` | 0 | — | — |

合计 **57 节点 / 213 GPU**（54 个 GB300 节点满配应 216，缺 3 颗见下）。

### 故障节点（全部 cordon）

| 节点全名 | Sub-block | 故障 | GCE 状态 | 上报 |
|---|---|---|---|---|
| `gke-gb300-gke-test-gb300-pool-0002-c2cb19f4-lcg3` | d0002 | GPU 3/4，reset/reboot 无效 | REPAIRING | 已报 `UNRECOVERABLE_GPU_ERROR` |
| `gke-gb300-gke-test-gb300-pool-0006-0a916ca1-33qv` | d0006 | GPU 3/4，Xid 143 FSP boot fail | REPAIRING | 已报 `UNRECOVERABLE_GPU_ERROR` |
| `gke-gb300-gke-test-gb300-pool-0009-070612ce-36wz` | d0009 | GPU 3/4，硬件死 | REPAIRING | 已报 `UNRECOVERABLE_GPU_ERROR` |
| `gke-gb300-gke-test-gb300-pool-0006-0a916ca1-qx2s` | d0006 | `mlx5_3` phys=Disabled（NIC 硬件死） | RUNNING，GPU 4 完好 | **未报**，需用 `PERFORMANCE` + NIC 描述 |

三台 REPAIRING 的 physicalHost 与 `docs/gke-qa-report-v2.md` §5 记录一致 → 仍在原物理机上修，**尚未换 host**。完整 physicalHost 值见该报告，记录规则见 [[gke-fault-node-physicalhost]]。

`qx2s` 可修手段已穷尽：OS reboot（07-23）无效、GCE REPAIRING 软 reset（physicalHost 未变）无效，2026-07-25 复测 `mlx5_3` 仍 `DOWN/Disabled/40 Gb/sec (4X QDR)`，其余 7 口 `ACTIVE/LinkUp/400 Gb/sec (4X NDR)`。对照 `5kw9`（d0012）同症状经 REPAIRING 后 8/8 全恢复 → **`phys=Disabled` 分软件 transient 与硬件死两种，REPAIRING 有效与否取决于故障性质**。

### ⚠ 坏 COS 占比因释放而恶化

释放优先清掉的恰好都是好镜像 pool，坏 COS 占比从 **5/10 升到 2/3**：

- ✓ `19506-224-49`（nvidia.ko Jun 18，好）：**仅 pool-0009**
- ❌ `19506-224-80`（nvidia.ko Jun 27 回归）：pool-0002、pool-0006

含义：pool-0002 / pool-0006 任一节点走 GCE 修复被 MIG 补建，出来就是 CUDA context 全废的节点（`lcg3`、`33qv` 即实证，两台现跑 4681000）。**现在只有 pool-0009 能安全重建节点。** 背景见 [[gke-4681000-nvidia-ko-regression]]。

核对命令：`bash scripts/gke-create-nodepool.sh --verify-only <sub-blocks>` —— 注意**不传参数时只查默认的 0011/0012**，必须显式传 sub-block 列表。

维护例外 `freeze-node-upgrades-ko-regression` 冻结 node auto-upgrade 至 **2026-10-23**，到期前需有处理方案。

### Reservation 结构（CLAUDE.md 记的已过时）

`nvidia-gb300-dxkhoz4ypk4mh` / block-0001 实际是 **306 VM / 17 个 sub-block**（d0001~d0017），不是 CLAUDE.md 写的 216 VM / 12 sub-block。健康分布与 d0014 长期降级见 [[reservation-health-query]]。

### 历史质检

全量质检 2026-07-16~17 完成（189 节点 × 4 项），跨域 NCCL 6 对全跑通（MNNVL=2, all_reduce 802-811 GB/s，比 GB200 高 6-7%），详见 `docs/gke-qa-report-v2.md`。07-24 补采 18N 同域 baseline（RDMA 365.7-366.4 / MNNVL 912.7-920.4 GB/s），但其唯一数据源 d0004+d0005 已随本次释放消失，**该组数据不可复现**。

**Why:** 集群容量与故障分布直接决定训练任务能否调度、pool 能否重建。
**How to apply:** 部署前先确认目标 pool 的可调度节点数与 cordon 情况；**任何涉及节点重建的操作先核对 MIG COS 版本**。本文件是快照，集群变动频繁（本 session 内 yangwhale 就自行清空了 pool-0001 全部 18 节点负载并删掉 `yw-a`），实际操作前用 `kubectl get nodes` + `gcloud container node-pools list` 复核。
