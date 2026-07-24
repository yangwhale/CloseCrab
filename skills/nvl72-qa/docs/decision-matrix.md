# 3 条路径决策矩阵（recreate pool-0013~0017 用哪个 image）

**决策前提**：`scripts/gke-create-nodepool.sh` 已改成默认 pin `GKE_NODE_VERSION=1.36.0-gke.4447000`（旧 image，已验证 CUDA 可用），且加了 preflight。

**GKE 现状**（2026-07-24 确认）:
```
cluster gb300-gke-test 现在 RAPID channel
master version: 1.36.0-gke.4681000

RAPID channel validVersions:
  1.36.2-gke.1498000  1.36.2-gke.1346000
  1.36.0-gke.4681000  ← default，broken
  1.35.6-gke.1258000  1.35.6-gke.1250000
  1.34.9-gke.1322000  1.34.9-gke.1287000
  1.33.13-gke.1109000 1.33.13-gke.1101000

REGULAR channel validVersions (含 1.36.0-gke.4447000):
  1.36.0-gke.4447000  ← 已知 CUDA 可用
  1.36.0-gke.3712000
  1.35.6-gke.1127000  1.35.6-gke.1049000
  1.34.9-gke.1131000  1.34.9-gke.1065000
  1.33.13-gke.1011000 1.33.12-gke.1270000
```

**问题**：`1.36.0-gke.4447000` 已从 RAPID 里 deprecate 掉，脚本 preflight 现在会 abort。必须选一条路。

---

## 路径 A：cluster 切 REGULAR channel + pin 4447000（推荐）

### 操作
```bash
# 1. 切 channel（cluster-wide 一次性变更）
gcloud --configuration=taiji-poc container clusters update gb300-gke-test \
  --location=us-central1 --project=tencent-gcp-taiji-poc \
  --release-channel=regular
# op 需要 ~5–15 min（master upgrade）

# 2. 等 op DONE 后，跑脚本（默认 GKE_NODE_VERSION=1.36.0-gke.4447000 就 valid 了）
bash scripts/gke-create-nodepool.sh 0013 0014:15 0015 0016 0017
```

### 优点
- 已在 pool-0002 / 0003 / 0005 / 0007 上验证 CUDA 完全可用
- 是已知稳定路径，风险最低
- 后续 auto-upgrade 也走 REGULAR 更保守版本

### 缺点
- cluster-wide 变更：现存所有 pool 的 nominal auto-upgrade target 会改
  - 现存 pool 的**实际节点不受影响**（除非 rolling recreate），只是 nominal version 挂上 REGULAR default
  - 已知副作用：不会自动破坏什么，但如果之后有 auto-upgrade 触发 recreate，会滚到 REGULAR 版本
- RAPID → REGULAR 意味着新 GB300 feature（例如更新的 accelerator profile / DRA driver）到达延迟
- 需要跟对方确认 RAPID 是不是有意选的（可能之前 POC 选 RAPID 是为了早拿 GB300 支持）

### 判断标准
- 如果对方 "只关心 pool-0013~0017 尽快交付质检"，且没有 hard requirement 在 RAPID → 选 A
- 如果对方明确 "cluster 必须留 RAPID" → 走 B

---

## 路径 B：保留 RAPID，pin 到 RAPID 里的老 minor

### 候选版本（RAPID 里 valid，都需要 create 1 pool 跑 probe 验证 CUDA 是否可用）

| 版本 | 距 4681000 | 未知点 |
|---|---|---|
| `1.36.0-gke.4681000` | 0 | ✗ 已知 broken |
| `1.35.6-gke.1258000` | 老一 minor | 是否有相同 regression 未验证；1.35 早于 GB300 GA 有可能没适配好 |
| `1.35.6-gke.1250000` | 同上 | 同上 |
| `1.34.9-gke.1322000` | 老两 minor | 更老 kernel/driver，可能 GB300 driver 支持不完整 |
| `1.33.13-gke.1109000` | 老三 minor | 大概率不能识别 GB300 |

### 操作
```bash
# 1. 决定试哪个（推荐 1.35.6-gke.1258000）
export GKE_NODE_VERSION=1.35.6-gke.1258000

# 2. 先建 1 个 pool 验证
bash scripts/gke-create-nodepool.sh 0013

# 3. 等 pool ready，起 probe pod verify CUDA
kubectl apply -f probe/probe-newold.yaml
# ...改 probe-newold.yaml 里 probe-new 的 nodeName 为一台新 pool-0013 节点
kubectl -n cuda-probe exec probe-new -- python3 /tmp/cuprobe.py

# 4a. 如果 cuDevicePrimaryCtxRetain 全 0 → CUDA 可用 → 继续 create 剩下 4 pool
bash scripts/gke-create-nodepool.sh 0014:15 0015 0016 0017
# 4b. 如果仍 -> 1 → 换下一个版本重试
```

### 优点
- 不动 cluster channel，其他 pool / 其他人的 workload 完全不受影响
- 只影响这 5 个 pool

### 缺点
- 未验证：这些老版本可能有别的 regression（GB300 支持不完整、driver 不 stable 等）
- 每换一个版本要重跑 create + probe，时间成本高
- 老 minor 会更快 deprecate，未来还得再换

### 判断标准
- 如果对方明确 "cluster 不能切 REGULAR" → 只能走 B
- 建议先跑 `1.35.6-gke.1258000` probe，通就用它；不通就升级到路径 A

---

## 路径 C：保留 broken 现状，等 GKE / NVIDIA fix

### 操作
```bash
# 不动任何 pool，pool-0013~0017 continue broken
# 定期查 rapid channel default 是否更新到 4681000 之后的新 build
gcloud --configuration=taiji-poc container get-server-config \
  --location=us-central1 --format='value(channels.defaultVersion)' \
  --flatten='channels[]' --filter='channels.channel:RAPID'
```

### 优点
- 零操作
- 后面 GKE 出 fix 之后 auto-upgrade 会滚过来（如果 pool auto-upgrade on），或者手动 upgrade

### 缺点
- 5 pool × 72 GPU = **360 张 GB300 GPU 完全无法用**（只有 hw-check 能验证节点物理层）
- 不知道 GKE 什么时候出 fix，可能几天，也可能几周
- 用户 / 客户等交付时间受阻

### 判断标准
- 如果对方已知 GKE 会在 X 天内出新 image，且 X 短，可以接受这个等待 → 选 C
- 如果对方需要"下周就要用 pool-0013~0017 跑训练/评估" → **不能选 C**

---

## 我的建议

**如果你只能 default 一个选择：走 A。**

理由：
1. A 是唯一"已知稳定 + 一次性搞定"的方案
2. cluster 切 channel 是可逆操作（后面 GKE fix 出了可以再切回 RAPID）
3. B 的验证成本 = 一次 create + probe（30–45 min），如果最终发现要走 A 就浪费了这次 create + delete pool 时间
4. C 是"不做决定"，对客户交付不负责

**决策 checklist**（问对方）：
- [ ] cluster 现在的 RAPID channel 是有意选的吗？切 REGULAR 有 blocker 吗？
- [ ] pool-0013~0017 交付的死线是什么？
- [ ] 有没有其他 pool 上跑的 workload 会被 auto-upgrade 影响？
- [ ] 已经跟 GKE / NVIDIA 报过 case 吗？有没有 ETA？

对齐后再动手。
