# pool-0014 交付决策

## 现状（2026-07-25 10:30）

```
gb300-pool-0014  STATUS=ERROR  initialNodeCount=18  实际 16 节点 RUNNING
statusMessage:
  Not all instances running in IGM after 35m9s. Expected 18, running 16.
    [INTERNAL_ERROR]: Instance 'hp0q' creation failed.
    [GCE_STOCKOUT]: 2 nodes cannot be created due to lack of capacity.

Sub-block d0014:
  degradedHostCount=2  healthyHostCount=16  count=18  inUseCount=16
```

**关键事实**：`d0014` sub-block 长期硬件降级（2 台 host down），物理上限就是 16 台。

## 已交付的 pool-0014 质检报告

**16 台节点已跑完全套质检并 PASS**（见 `qa/docs/gb300-0014-20260725-112347.md`）：
- 32/32 测试项 PASS（4 项 × 16 节点：hw-check + DCGM + NCCL + cuBLAS）
- NCCL all_reduce avg 688.9 GB/s（跟其他 pool 一致）
- MNNVL=ON 933.92 GB/s（NVLink fabric 正常）
- MNNVL=OFF 379.21 GB/s（8× CX-8 RDMA 正常）

**pool-0014 现在是可交付状态**，只是节点数比原计划少 2 台。ERROR status 是 GKE 抱怨"没拿到 18 台"，不代表节点本身有问题。

## 三个选项

### 选项 A：接受 16 台，直接交付（推荐默认）

**动作**：
- 不做任何操作，把 pool-0014 16 台按现状交付
- 交付邮件里注明"pool-0014 因 d0014 sub-block 硬件降级只有 16 台可用（GCP 侧未修）"

**优点**：
- 零风险，最快交付
- 已通过质检 PASS
- 客户如果只是要"跑训练"，16 台 = 64 GPU 也是完整 domain

**缺点**：
- pool 状态显示 ERROR，业务方查 GKE console 会看到红字（可能会问）
- 少了 8 GPU 容量
- pool status ERROR 状态下 GKE 是否允许后续 `--node-count` update 或 auto scale，未验证

**判断标准**：如果对方（客户 / 业务方）没有明确"必须 18 台"的硬要求，选 A。

### 选项 B：delete + recreate 用 healthy 上限，让 STATUS 变成 RUNNING

**动作**：
```bash
# 1. 记录当前 pool-0014 的 16 台节点 physicalHost（事后追踪）
kubectl get nodes -l cloud.google.com/gke-nodepool=gb300-pool-0014 \
  -o custom-columns=NAME:.metadata.name,PHOST:.metadata.labels.'cloud\.google\.com/gce-topology-host' \
  > /tmp/pool-0014-before.txt

# 2. Delete pool
gcloud --configuration=taiji-poc container node-pools delete gb300-pool-0014 \
  --cluster=gb300-gke-test --location=us-central1 --project=tencent-gcp-taiji-poc --quiet

# 3. 等 sub-block 清理窗口过（1-2 小时，degradedHostCount 会短暂飙升，是预期）
#    等它回落到稳态：
until R=$(gcloud --configuration=taiji-poc compute reservations sub-blocks describe \
     nvidia-gb300-dxkhoz4ypk4mh --block-name=nvidia-gb300-dxkhoz4ypk4mh-block-0001 \
     --sub-block-name=nvidia-gb300-dxkhoz4ypk4mh-block-0001-subblock-0014 \
     --zone=us-central1-b --project=tencent-gcp-taiji \
     --format='value(resource.healthInfo.degradedHostCount,resource.healthInfo.healthyHostCount)' 2>/dev/null) && \
     H=$(echo $R | awk '{print $2}') && [[ $H -ge 16 ]]; do
  echo "[$(date +%H:%M:%S)] waiting for cleanup: $R"; sleep 60
done
echo "healthy 恢复到 >=16, 可 recreate"

# 4. Recreate 用 healthy 上限
bash scripts/gke-create-nodepool.sh 0014:16   # 或者 :<healthy 数>

# 5. 建完后核对 MIG COS 是好 image
bash scripts/gke-create-nodepool.sh 0014 --verify-only
# 期望：✓ gb300-pool-0014: <含 '19506-224-49' 的 image name>

# 6. 重跑 pool-0014 质检（新节点可能不同 physicalHost，需要重跑）
bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh all 0014
bash qa/collect-logs-cloud.sh qa/profiles/gb300-gke-taiji.sh --manifest logs/qa-manifest-gb300-0014-*.txt
bash qa/gen-report.sh qa/profiles/gb300-gke-taiji.sh logs/qa-manifest-gb300-0014-*.txt
```

**优点**：
- pool STATUS=RUNNING 干净
- 现有 pool-0014 报告失效（是不同物理节点），新报告更有说服力
- 老 pool 的 16 台释放后进入 GCP 的重新分配池，理论上能拿到更好的 host

**缺点**：
- 白白 delete 一批已 PASS 的节点（沉没成本）
- 重跑质检要另外 ~30 min
- Recreate 时 sub-block healthy 可能还是 16（不会突然拿到 18）
- 有小概率 GCE stockout 更严重（比如只拿到 14 台）
- 总耗时 3-4 小时（清理窗口 + create + 质检）

**判断标准**：只有当"pool STATUS=RUNNING"是硬指标时选 B。如果 STATUS ERROR 但 16 台能用可以接受，选 A。

### 选项 C：等 GCP 修 d0014 硬件后拿满 18 台

**动作**：
- 走 GCP support case，请求修 d0014 的 2 台 degraded host
- 等硬件恢复后（不确定多久，可能几天到几周）执行选项 B 步骤
- 期间 pool-0014 保持 ERROR 状态 16 台，可选择先按 A 交付

**背景**：d0014 从 07-15 的 0 degrade 一路走到 07-24 的 3 degrade，07-25 稳态 2 degrade —— **是趋势性恶化**，不是一天两天能修好的。历史见 `memory/reservation_health_query.md`。

**优点**：
- 最终能拿到 18 台
- 借机让 GCP 修真硬件问题

**缺点**：
- 交付时间不确定
- 需要 GCP support 配合，涉及外部依赖
- 期间 pool-0014 一直 ERROR

**判断标准**：如果对方明确要求"必须 18 台且能等"，选 C（同时按 A 先交付 16 台 unblock）。

---

## 我的建议：默认 A，除非有特殊要求

理由：
1. 16 台已 PASS，交付内容质量没问题
2. GKE 的 pool ERROR 状态不影响节点使用，只是元数据显示
3. 选 B 是白干一遍，选 C 依赖外部时间线
4. 客户/业务方如果没提"必须 18 台"，就没必要主动做减法（吃亏）

**跟对方 sync 时的说法**：
> "d0014 sub-block 有 2 台硬件降级（GCP 侧长期问题），pool-0014 只能拿到 16 台。16 台已通过全套质检 PASS（NCCL 688.9 GB/s，MNNVL 933 GB/s，跟其他 pool 一致）。是接受 16 台交付，还是要等 GCP 修硬件？后者需要 GCP support case + 未定时间线。"

对方选完你再动手。
