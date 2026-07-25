---
name: reservation-health-query
description: Reservation sub-block degrade 和 GCE 创建失败的查询方法，区分三种节点损耗
metadata: 
  node_type: memory
  type: reference
  originSessionId: 1d46131b-e454-47aa-9615-b08009477d3c
  modified: 2026-07-25T07:01:39.710Z
---

## Reservation 健康数据查询

### 数据来源

**Degrade（GCP 硬件降级）:**
```bash
gcloud --configuration=taiji-poc compute reservations sub-blocks describe \
  nvidia-gb300-dxkhoz4ypk4mh \
  --block-name=nvidia-gb300-dxkhoz4ypk4mh-block-0001 \
  --sub-block-name=nvidia-gb300-dxkhoz4ypk4mh-block-0001-subblock-<NNNN> \
  --zone=us-central1-b --project=tencent-gcp-taiji \
  --format='value(resource.healthInfo.degradedHostCount,resource.healthInfo.healthyHostCount,resource.count,resource.inUseCount)'
```

**GCE 创建失败:** `healthyHostCount - inUseCount`（host 健康但 VM 没创建成功）

**可调度计算公式:** `可调度 = 配额(18) − degrade − GCE创建失败 − 质检故障`

### 三种节点损耗

| 类型 | 数据来源 | 含义 |
|---|---|---|
| Degrade | `degradedHostCount` from reservation sub-block API | GCP 标记硬件降级，无法创建 VM |
| GCE 创建失败 | `healthyHostCount − inUseCount` | host 健康但 node pool 创建时 GCE 报错 |
| 质检故障 | `kubectl get nodes` cordoned 数 | VM 创建成功但 GPU/RDMA 有问题 |

### 批量查询

```bash
# sub-block 列表（count + inUseCount）
gcloud --configuration=taiji-poc compute reservations sub-blocks list \
  nvidia-gb300-dxkhoz4ypk4mh \
  --block-name=nvidia-gb300-dxkhoz4ypk4mh-block-0001 \
  --zone=us-central1-b --project=tencent-gcp-taiji \
  --format='table(name,count,inUseCount)'

# 单个 sub-block 详情（含 degradedHostCount）
gcloud ... sub-blocks describe ... --format='json' | python3 -c "
import json,sys; d=json.load(sys.stdin)['resource']
print(f'count={d[\"count\"]} inUse={d[\"inUseCount\"]} degrade={d[\"healthInfo\"][\"degradedHostCount\"]} healthy={d[\"healthInfo\"][\"healthyHostCount\"]}')"
```

### 2026-07-17 数据快照

| Domain | 配额 | Degrade | GCE 失败 | 质检故障 | 可调度 |
|---|---|---|---|---|---|
| d0001 | 18 | 0 | 0 | 0 | 18 |
| d0002 | 18 | 0 | 0 | 2 | 16 |
| d0003 | 18 | 0 | 2 | 0 | 16 |
| d0004 | 18 | 0 | 0 | 0 | 18 |
| d0005 | 18 | 0 | 1 | 0 | 17 |
| d0006 | 18 | 0 | 0 | 2 | 16 |
| d0007 | 18 | 0 | 1 | 0 | 17 |
| d0008 | 18 | 1 | 1 | 0 | 16 |
| d0009 | 18 | 2 | 0 | 1 | **15** |
| d0010 | 18 | 1 | 0 | 0 | 17 |
| d0011 | 18 | 1 | 0 | 0 | 17 |
| d0012 | 18 | 0 | 0 | 1 | 17 |

## ⚠ 释放 node pool 后 degradedHostCount 会暂时飙升（清理窗口，非真降级）

**GB300 节点删除后会进入 1-2 小时的清理流程，表现就是先 degrade、再逐步恢复。** 期间查 reservation 会看到该 sub-block `healthStatus=DEGRADED`、`degradedHostCount` 接近满值。这是预期行为，不是硬件真降级，不需要处理。

2026-07-25 实测（释放 5 个 pool 后）：

| 时点 | d0004 | d0005 | d0007 | d0010 | d0012 |
|---|---|---|---|---|---|
| 删除前 | 0 | 0 | 0 | 0 | 0 |
| 删后 ~30 min | 17 | 14 | 15 | 15 | 0（刚删 4 min，未进入） |
| 再 +2 min | 15 ↓ | 12 ↓ | 14 ↓ | 10 ↓ | 0 |

`degradedHostCount` 回落、`healthyHostCount` 同步上升 → host 逐台走完检查即恢复。

**Why:** 释放后立即读数会误判成"机器全降级、拿不回来了"，进而错误升级到 GCP support。
**How to apply:** 释放 pool 后 **1-2 小时内的 reservation 读数直接忽略**，等清理流程走完再看。判断真实降级只看**稳态**值 —— 长期稳定在 degraded=1 的（如 d0002/d0006/d0008/d0009/d0011）才是真的。

## block-0001 实际是 17 个 sub-block / 306 VM

不是 CLAUDE.md 写的 12 sub-block / 216 VM。批量查询要覆盖 **d0001~d0017**。

## d0014 长期硬件降级（真实 degrade 的典型）

`subblock-0014` 是全 reservation 唯一一个非释放导致的持续 `healthStatus=DEGRADED`，且**在恶化**：

| 日期 | degraded | healthy | 来源 |
|---|---|---|---|
| 2026-07-15 | 0 | 18 | operations.md `## 2026-07-15 DSv3 256 GPU GKE 部署` |
| 2026-07-23 | 2 | 16 | `## 2026-07-23 部署 subblock 0013-0017 到 GKE node pool` |
| 2026-07-24 07:22 | **3** | 15 | `## 2026-07-24 07:22 全部后台任务完成快照` |
| 2026-07-25 04:20 | **3** | 15 | 实测，`inUse=0`、`status=READY` |

**已造成的实际损失**：pool-0014 两次都建不起来 —— 07-23 请求 18 台（物理上限 16），拿到 12 STAGING 后 GCE stockout 拒绝，最终 ERROR 并 rollback，VM 全部 deprovision 归 0；07-24 重建时 degraded 已涨到 3，pool 状态 ERROR / 15 台。原话记录："GKE 一直 async retry 想拿到 18，但 GCE 侧没 spare 可给"。这 3 台从 07-23 等 spare replacement 至今未换。

**建 pool 前务必先查目标 sub-block 的 `healthyHostCount`，用它做 `--num-nodes`**，否则 GKE 会无限 retry 直到 ERROR + rollback，白等 30+ 分钟且 VM 全清。

### 2026-07-25 04:20 全量快照（17 sub-block）

| 类型 | Sub-block |
|---|---|
| 持续 DEGRADED（真实） | **d0014（3 台，≥2 天，单调恶化）** |
| 稳态 degraded=1 | d0002 / d0006 / d0008 / d0009 / d0010 / d0011 / d0012 |
| degraded=0 | d0004 / d0005 / d0007 / d0013 / d0015 / d0016 / d0017 |
| 释放后回落中（临时，忽略） | d0001（3）、d0003（4） |
| inUse>0（仍被占用） | d0002=18、d0006=18、d0009=18、d0011=17、d0008=2、d0013=1 |

d0008（自建 k8s worker `gb300-central-b0001-d0008-w1`）与 d0011（`harry-gb300-central-nvl72-policy-0011`，他人资源）的占用不属 GKE 集群。

**Why:** 报告中区分三种损耗需要不同的 API 查询，degrade 和 GCE 失败的处理方式不同（degrade 等 GCP 修/换，GCE 失败可能重试 node pool resize 解决）。
**How to apply:** 更新质检报告或排查节点不足时，按上述方法查询最新数据。
