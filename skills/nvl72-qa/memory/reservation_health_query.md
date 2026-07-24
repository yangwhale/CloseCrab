---
name: reservation-health-query
description: Reservation sub-block degrade 和 GCE 创建失败的查询方法，区分三种节点损耗
metadata: 
  node_type: memory
  type: reference
  originSessionId: 1d46131b-e454-47aa-9615-b08009477d3c
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

**Why:** 报告中区分三种损耗需要不同的 API 查询，degrade 和 GCE 失败的处理方式不同（degrade 等 GCP 修/换，GCE 失败可能重试 node pool resize 解决）。
**How to apply:** 更新质检报告或排查节点不足时，按上述方法查询最新数据。
