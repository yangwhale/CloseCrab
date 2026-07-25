---
name: nvl72-qa
description: NVIDIA NVL72 (GB200/GB300) GPU 集群验收测试。Profile 驱动的端到端质检：硬件自检、DCGM 诊断、NCCL 带宽、cuBLAS GEMM、单域/跨域多节点 NCCL、日志分析、报告生成、故障节点处理。含验收 baseline 阈值与判定标准。当用户说"验收测试"、"GPU QA"、"质检"、"跑一下 hw-check"、"NCCL benchmark"、"节点健康检查"、"GPU 集群测试"、"NVL72 测试"、"GB300 QA"、"GB200 QA"等关键词时触发。
---

# NVIDIA NVL72 验收测试 (GPU QA Toolkit)

Profile 驱动的 GPU 节点质检套件，支持 GB200/GB300 GKE 集群。覆盖环境准备、测试执行、日志收集、分析、报告生成、故障 cordon 全流程。

**基线版本**：2026-07-25 handoff（5 pool × 358 GPU 全部质检 PASS 的那一版）

## 端到端流程

```
环境准备 → 执行质检 → 收集日志 → 分析 → 生成报告 → 故障处理
setup-env → run-checks → collect-logs-cloud → analyze-logs → gen-report → cordon-faulty
```

## 前置条件速查

```bash
cd ~/.claude/skills/nvl72-qa
source qa/profiles/gb300-gke-taiji.sh && \
  echo "kubectl: $(kubectl version --client -o json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["clientVersion"]["gitVersion"])' 2>/dev/null || echo MISSING)" && \
  echo "helm: $(helm version --short 2>/dev/null || echo MISSING)" && \
  echo "envsubst: $(envsubst --version 2>/dev/null | head -1 || echo MISSING)" && \
  echo "context: $(kubectl --context=${QA_KUBE_CONTEXT} cluster-info 2>/dev/null | head -1 || echo UNREACHABLE)" && \
  echo "gcloud: $(gcloud --configuration=${QA_GCLOUD_CONFIG} config get project 2>/dev/null || echo MISSING)"
```

**首次跑前必须 `mkdir -p logs`**，否则 run-checks 写 manifest 会失败（实测踩过）。

## 自然语言 → action 映射

| 用户说 | 命令 |
|---|---|
| "质检 0013" / "QA 0013" | `all 0013`（hw + dcgm + nccl + cublas 单节点） |
| "全面质检 0013" / "全量质检" | `all-full 0013`（上述 + 多节点 RDMA + MNNVL） |
| "硬件检查" | `hw-check 0013 [node]` |
| "跑 dcgm" | `dcgm 0013` |
| "单机 nccl" | `nccl 0013` |
| "跑 gemm / cublas" | `gemm 0013` |
| "域内多节点 nccl" | `nccl-multi 0013 --mnnvl=on` |
| "域内 rdma" | `nccl-multi 0013 --mnnvl=off` |
| "跨域 nccl" | `nccl-cross 0013 0015` |

## 快速使用

```bash
cd ~/.claude/skills/nvl72-qa
P=qa/profiles/gb300-gke-taiji.sh

bash qa/setup-env.sh $P                                    # 1. 环境准备（幂等）
bash qa/run-checks.sh $P all 0013                          # 2. 执行质检
bash qa/collect-logs-cloud.sh $P --manifest logs/qa-manifest-gb300-0013-*.txt   # 3. 拉日志
bash qa/analyze-logs.sh logs/qa-hw-check-gb300-0013-*/     # 4. 分析
bash qa/gen-report.sh $P logs/qa-manifest-gb300-0013-*.txt # 5. 报告 → qa/docs/ + index.md
bash qa/cordon-faulty.sh $P logs/qa-hw-check-*/ --dry-run  # 6. 故障处理（先 dry-run）
```

**不要并行多个 subblock 跑 all-full** —— DRA / GPU device plugin 层撞车会导致 CUDA busy 假报。逐 pool 顺序跑。

## ⭐ 验收标准（PASS 判定）

### 绝对下限阈值（来自 299N/267N fleet 统计，p5 打八折）

profile 里已配置，analyze-logs / gen-report 自动应用：

| 指标 | 下限 | GB300 实测基线 |
|---|---|---|
| NCCL all_reduce busBW @16GB | **650 GB/s** | ~688 |
| NCCL all_gather / reduce_scatter | 630 GB/s | ~670 |
| NCCL alltoall | 640 GB/s | ~682 |
| cuBLAS FP4 | 7500 TFLOPS | ~7892 |
| cuBLAS FP8 | 3300 | — |
| cuBLAS FP16 | 1600 | — |
| cuBLAS BF16 | 1700 | — |
| cuBLAS TF32 | 800 | — |
| cuBLAS FP32 | 70 | — |
| 多节点 MNNVL=ON | — | 900+ GB/s（实测 933） |
| 多节点 MNNVL=OFF (RDMA) | — | ~370-380 GB/s |

离群检测：相对偏离 median > 5%（NCCL）/ 3%（cuBLAS），**或**低于绝对下限 → 标记 outlier。
**用 median 不用 mean** —— 多台同时坏时 mean 会被拉低导致漏检（踩过）。

### 报告 verdict 四态

`gen-report.sh` 自动判定，区分"没跑"和"跑了没过"：

| Verdict | 含义 |
|---|---|
| `PASS` | 全部执行且全部通过 |
| `PASS (incomplete)` | 已执行的都过，但有项目 NOT_RUN / INCOMPLETE，需补测 |
| `FAIL` | 有故障节点，需 cordon |
| `FAIL (incomplete)` | 既有故障又有未执行 |

一个 pool 完整验收 = 4 项测试 × N 节点全部 PASS（18 节点 = 72 项）+ 多节点 NCCL（MNNVL on/off）通过。

### 已验收样本参考

`reports/` 目录有 9 份真实报告（2026-07-25 pool-0013~0017 全 PASS），可作为格式和数值参照。

## 质检项目

- **hw-check**: 14 项硬件自检（GPU 可见性、NVLink topo、Fabric clique、RDMA NIC、ECC、时钟、固件）— 读 sysfs，不需 CUDA
- **dcgm**: DCGM r2（PCIe/显存/HBM，需 DCGM 4.6.0+ for Blackwell）
- **nccl**: 单机 4-GPU NVLink，4 collective @16GB
- **gemm**: cuBLAS 6 精度（FP4/FP8/FP16/BF16/TF32/FP32）
- **nccl-multi**: 单域多节点（JobSet + ComputeDomain）
- **nccl-cross**: 跨域（2 ComputeDomain, MNNVL=2）

## GKE 集群/Pool 操作

脚本在 `scripts/`：`gke-env.sh`（含 node version pin + expected COS）、`gke-create-cluster.sh`、`gke-create-nodepool.sh`（已加 pin 4447000 + preflight + `--verify-only`）、`gke-post-install.sh`。

**铁律**：任何 GKE pool/cluster 变更先 `ls scripts/gke-*` 找现成脚本，不要 `gcloud describe --format=yaml` 手写命令（漏 hugepages 等字段会引发连锁失败）。create/update 后立刻 `kubectl get node -o jsonpath='{.status.capacity}'` 核对 hugepages-2Mi / nvidia.com/gpu。

## ⚠️ 关键已知问题

**GKE node image 1.36.0-gke.4681000（COS 224.80）CUDA 全挂**
- nvidia.ko Jun 27 build → `cuDevicePrimaryCtxRetain` / `cuCtxCreate_v2` 返回 `CUDA_ERROR_INVALID_VALUE`
- 表象：`nvidia-smi` 正常、hw-check PASS，但 DCGM / NCCL / cuBLAS / 任何训练负载全挂
- 已知好版本：**1.36.0-gke.4447000（COS 224.49，nvidia.ko Jun 18 build）**
- 2026-07-25 已完成单变量隔离，**唯一差异就是 nvidia.ko 二进制本身，不要再重做排查**（`memory/gke_4681000_nvidia_ko_regression.md`）
- 集群已设维护例外 `freeze-node-upgrades-ko-regression`，2026-07-25 → **2026-10-23** 到期前需重评估
- probe 工具在 `probe/`（cuprobe.py / hostdiff.sh），备用复现

## 踩坑清单

1. **wrapper `DONE` marker ≠ test pass** —— 永远看 detail log，别信 manifest
2. **删 pod / ns 前先拉日志** —— Cloud Logging 有 ~60s flush delay
3. **每次 GCP 操作立即记 operations.md** —— 交付文档依据
4. **不要并行多 subblock all-full** —— DRA 撞车 CUDA busy 假报
5. **pool NODE_VERSION nominal ≠ 节点实际 kubelet** —— 看 node 的 kubeletVersion
6. **MIG regional instance template 才决定 COS 镜像**（不是 pool NODE_VERSION），核对用 `--verify-only`
7. **pod 1/1 Running 不代表有负载** —— entrypoint 有 `sleep infinity` 兜底（`memory/fake_running_zombie_workload.md`）
8. **NVLink fabric split 4 cliques 可 reset 恢复** —— 节点级 fabric manager 初始化异常，不用急着 report-host-as-faulty
9. **hugepages 漏配连锁反应** —— 漏 `--num-2m-hugepages=4096` → asapd-lite pending → ipvlan 缺失 → DRA 不全 → CUDA busy
10. **DRAM correctable ECC >1000 是 WARN 不是 FAIL** —— 软错误，可 cordon 观察或转 NVIDIA support

## 目录

```
nvl72-qa/
├── qa/          测试脚本 + profiles + templates
├── scripts/     GKE 集群/pool 基础设施脚本
├── docs/        HANDOFF / 集群状态 / CUDA 根因 / pool-0014 决策 / ops log
├── reports/     ⭐ 9 份已验收质检报告样本（含 index.md）
├── probe/       CUDA busy 复现工具
└── memory/      21 个经验记忆
```
