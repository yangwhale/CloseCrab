# Cluster / Pool 当前状态快照（2026-07-25 10:30 UTC）

> **重要**：本文件是快照，实际操作前应重新跑一遍下面的命令确认。集群变动频繁。

## 1. Cluster

```
name              master version        node count  channel  status
gb300-gke-test    1.36.0-gke.4681000    145         RAPID    RUNNING
```

## 2. 维护例外（挡 node auto-upgrade）

```
freeze-node-upgrades-ko-regression:
  endTime: 2026-10-23T00:00:00Z
  maintenanceExclusionOptions: {'scope': 'NO_MINOR_OR_NODE_UPGRADES'}
  startTime: 2026-07-25T01:35:00Z
```

## 3. Node Pools（全部）

```
NAME             NODE_VERSION        STATUS   INITIAL_NODE_COUNT  AUTO_UPGRADE
default-pool     1.36.0-gke.4681000  RUNNING  3                   True
gb300-pool-0002  1.36.0-gke.4681000  RUNNING  18                  True
gb300-pool-0006  1.36.0-gke.4681000  RUNNING  18                  True
gb300-pool-0009  1.36.0-gke.4681000  RUNNING  18                  True
gb300-pool-0013  1.36.0-gke.4447000  RUNNING  18
gb300-pool-0015  1.36.0-gke.4447000  RUNNING  18
gb300-pool-0016  1.36.0-gke.4447000  RUNNING  18
gb300-pool-0017  1.36.0-gke.4447000  RUNNING  18
gb300-pool-0014  1.36.0-gke.4447000  ERROR    18
```

**注意 NODE_VERSION 是 nominal 值**，跟节点实际 kubelet 可能不一致。老 pool 的 NODE_VERSION 显示 4681000（跟随 auto-upgrade target），但实际节点还是 create 时的 kubelet（见 §4）。老 pool 的 AUTO_UPGRADE=True 是历史遗留，脚本还没 `--no-enable-autoupgrade` flag 时建的；新 pool（0013~0017）AUTO_UPGRADE 列为空即 False。

## 4. 5 个目标 pool 的实际节点 kubelet 版本

判断规则：`kubelet 4447000` = 好 image（COS 224.49, nvidia.ko Jun 18）；`4681000` = 坏 image（COS 224.80, nvidia.ko Jun 27）。

```
  pool-0013: 18 节点, kubelet=v1.36.0-gke.4447000
  pool-0014: 16 节点, kubelet=v1.36.0-gke.4447000
  pool-0015: 18 节点, kubelet=v1.36.0-gke.4447000
  pool-0016: 18 节点, kubelet=v1.36.0-gke.4447000
  pool-0017: 18 节点, kubelet=v1.36.0-gke.4447000
```

**5 pool 全部好 image**。pool-0014 是 ERROR 状态但已建出的 16 台节点也是好 image。

## 5. Sub-block d0013~d0017 健康

```
  sub      degrade   healthy  count    inUse
  d0013    0         18       18       18
  d0014    2         16       18       16
  d0015    0         18       18       18
  d0016    0         18       18       18
  d0017    0         18       18       18
```

解读：
- d0013 / d0015 / d0016 / d0017: healthy=18, degrade=0 → 可以建满 18 台
- **d0014: healthy=16, degrade=2** → 永远只能建 16 台（长期硬件降级，趋势见 `memory/reservation_health_query.md`）

## 6. 老 pool 的 MIG COS 状态（不属你的交付，但要留意）

`bash scripts/gke-create-nodepool.sh 0002 0006 0009 --verify-only` 输出：

```
❌ gb300-pool-0002: 期望含 '19506-224-49'，实际 'gke-1360-gke4681000-cos-gb300-bm-129-19506-224-80-c-nvda'
   (template=gke-gb300-gke-test-gb300-pool-0002-8d846b43)
❌ gb300-pool-0006: 期望含 '19506-224-49'，实际 'gke-1360-gke4681000-cos-gb300-bm-129-19506-224-80-c-nvda'
   (template=gke-gb300-gke-test-gb300-pool-0006-563e4893)
✓  gb300-pool-0009: gke-1360-gke4447000-cos-gb300-bm-129-19506-224-49-c-nvda
```

解读：
- **pool-0009**: ✓ 好 image，完全空闲，可以借用（如果 chris 想 sanity check）
- **pool-0002 / pool-0006**: MIG 指向坏 COS 224.80。当前跑着的节点是老的好 image（还没被 auto-upgrade 触发重建），但任何硬件故障导致 MIG 补建都会出坏节点（`lcg3`、`33qv` 就是这么变坏的）。已跟 yangwhale / infer 团队沟通即可

## 重新抓取本快照的命令

```bash
# pool 状态
gcloud --configuration=taiji-poc container node-pools list \
  --cluster=gb300-gke-test --location=us-central1 --project=tencent-gcp-taiji-poc \
  --format='table(name,version,status,initialNodeCount,management.autoUpgrade)'

# sub-block 健康
for SB in 0013 0014 0015 0016 0017; do
  gcloud --configuration=taiji-poc compute reservations sub-blocks describe \
    nvidia-gb300-dxkhoz4ypk4mh \
    --block-name=nvidia-gb300-dxkhoz4ypk4mh-block-0001 \
    --sub-block-name=nvidia-gb300-dxkhoz4ypk4mh-block-0001-subblock-${SB} \
    --zone=us-central1-b --project=tencent-gcp-taiji \
    --format='value(resource.healthInfo.degradedHostCount,resource.healthInfo.healthyHostCount,resource.count,resource.inUseCount)'
done

# MIG COS 核对
bash scripts/gke-create-nodepool.sh 0013 0014 0015 0016 0017 --verify-only

# 节点实际 kubelet
for P in 0013 0014 0015 0016 0017; do
  kubectl get nodes -l cloud.google.com/gke-nodepool=gb300-pool-$P \
    -o custom-columns=NAME:.metadata.name,KUBELET:.status.nodeInfo.kubeletVersion \
    --no-headers 2>/dev/null | sort -u -k2
done

# 维护例外
gcloud --configuration=taiji-poc container clusters describe gb300-gke-test \
  --location=us-central1 --project=tencent-gcp-taiji-poc \
  --format='value(maintenancePolicy)' | grep -A2 freeze
```
