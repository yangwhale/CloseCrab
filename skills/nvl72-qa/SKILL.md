---
name: nvl72-qa
description: NVIDIA NVL72 (GB200/GB300) GPU 集群验收测试。Profile 驱动的端到端质检：硬件自检、DCGM 诊断、NCCL 带宽、cuBLAS GEMM、单域/跨域多节点 NCCL、日志分析、报告生成、故障节点处理。当用户说"验收测试"、"GPU QA"、"质检"、"跑一下 hw-check"、"NCCL benchmark"、"节点健康检查"、"GPU 集群测试"、"NVL72 测试"、"GB300 QA"、"GB200 QA"等关键词时触发。
---

# NVIDIA NVL72 验收测试 (GPU QA Toolkit)

Profile 驱动的 GPU 节点质检套件，支持 GB200/GB300 GKE 集群。覆盖环境准备、测试执行、日志收集、分析、报告生成、故障 cordon 全流程。

## 端到端流程

```
环境准备 → 执行质检 → 收集日志 → 分析 → 生成报告 → 故障处理
setup-env → run-checks → collect-logs-cloud → analyze-logs → gen-report → cordon-faulty
```

## 工具位置

所有脚本在 `~/.claude/skills/nvl72-qa/qa/` 目录下。

## 快速使用

### 1. 环境准备（首次）

```bash
bash ~/.claude/skills/nvl72-qa/qa/setup-env.sh ~/.claude/skills/nvl72-qa/qa/profiles/gb300-gke-taiji.sh
```

### 2. 执行质检

```bash
# 单节点全量（hw + dcgm + nccl + cublas），约 15-30 分钟
bash ~/.claude/skills/nvl72-qa/qa/run-checks.sh <profile> all <subblock>

# 含多节点 NCCL，约 30-45 分钟
bash ~/.claude/skills/nvl72-qa/qa/run-checks.sh <profile> all-full <subblock>

# 单项测试
bash ~/.claude/skills/nvl72-qa/qa/run-checks.sh <profile> hw-check <subblock> [node]
bash ~/.claude/skills/nvl72-qa/qa/run-checks.sh <profile> dcgm <subblock>
bash ~/.claude/skills/nvl72-qa/qa/run-checks.sh <profile> nccl <subblock>
bash ~/.claude/skills/nvl72-qa/qa/run-checks.sh <profile> gemm <subblock>
bash ~/.claude/skills/nvl72-qa/qa/run-checks.sh <profile> nccl-multi <subblock> --mnnvl=on|off
bash ~/.claude/skills/nvl72-qa/qa/run-checks.sh <profile> nccl-cross <sub1> <sub2>
```

### 3. 收集日志 + 分析 + 报告

```bash
bash ~/.claude/skills/nvl72-qa/qa/collect-logs-cloud.sh <profile> --manifest logs/qa-manifest-*.txt
bash ~/.claude/skills/nvl72-qa/qa/analyze-logs.sh logs/qa-hw-check-*/
bash ~/.claude/skills/nvl72-qa/qa/gen-report.sh <profile> logs/qa-manifest-*.txt
```

### 4. 故障处理

```bash
bash ~/.claude/skills/nvl72-qa/qa/cordon-faulty.sh <profile> logs/qa-hw-check-*/ --dry-run
bash ~/.claude/skills/nvl72-qa/qa/cordon-faulty.sh <profile> logs/qa-hw-check-*/ --cordon
```

## 可用 Profile

| 集群 | Profile 文件 |
|---|---|
| GB300 GKE 太极 POC | `qa/profiles/gb300-gke-taiji.sh` |
| GB200 GKE 太极 POC | `qa/profiles/gb200-gke-taiji.sh` |
| GB200 GKE Playground | `qa/profiles/gb200-gke-playground.sh` |

新环境：复制 `qa/profiles/profile-template.sh`，填 6 个必填项即可。

## 质检项目

- **hw-check**: 13 项硬件自检（GPU 可见性、NVLink topo、Fabric clique、RDMA NIC、ECC、时钟、固件）
- **dcgm**: DCGM r2 诊断（PCIe/显存/HBM，需 DCGM 4.6.0+ for Blackwell）
- **nccl**: 单机 4-GPU NVLink（all_reduce/all_gather/reduce_scatter/alltoall @ 16GB）
- **gemm**: cuBLAS 6 精度 GEMM（FP4/FP8/FP16/BF16/TF32/FP32）
- **nccl-multi**: 单域多节点（JobSet + ComputeDomain）
- **nccl-cross**: 跨域 NCCL（2 ComputeDomain, MNNVL=2）

## GKE 集群操作脚本

GKE 集群/pool 操作脚本在 `~/.claude/skills/nvl72-qa/scripts/` 目录下：
- `gke-env.sh` — 环境变量（含 node version pin）
- `gke-create-cluster.sh` — 创建 GKE 集群
- `gke-create-nodepool.sh` — 创建 node pool（含 hugepages、auto-repair off、image pin）
- `gke-post-install.sh` — 集群后置安装
- `gke-run-checks.sh` / `gke-analyze-logs.sh` — 质检快捷入口

**铁律**：任何 GKE pool/cluster 操作先 `ls scripts/gke-*` 找现成脚本，不要 `gcloud describe --format=yaml` 手写命令（容易漏 hugepages 等关键配置）。

## 关键经验教训

详见 `memory/` 目录和 `docs/` 目录：

- **hugepages 漏配连锁反应**: 漏 `--num-2m-hugepages=4096` → asapd-lite pending → ipvlan 缺失 → DRA 不全 → CUDA busy（见 `docs/operations-excerpt.md`）
- **GKE node image CUDA regression**: kubelet 4681000 image 的 nvidia.ko Jun 27 build 有 `cuDevicePrimaryCtxRetain` regression，需 pin 到 4447000（见 `docs/cuda-regression-rootcause.md`）
- **NVLink fabric split 可 reset 恢复**: 4 cliques + PCIe fallback 是节点级 fabric manager 初始化异常，GCE reset 可修
- **NCCL outlier 检测用 median + absolute floor**: mean 在多台同时坏时会漏检，改 median + 400 GB/s 地板
- **wrapper PASS ≠ 测试真 PASS**: run-checks wrapper 只判 pod exit，不判测试内容，必须看原始 log
