# GPU 节点质检工具

Profile 驱动的 GPU 节点质检套件，支持 GB200/GB300 GKE 集群。覆盖环境准备、测试执行、日志收集、分析、报告生成、故障 cordon 全流程。

## 端到端流程

```
┌─────────────┐    ┌──────────────┐    ┌───────────────┐    ┌────────────┐    ┌──────────────┐    ┌──────────────┐
│ 1. 环境准备  │ →  │ 2. 执行质检   │ →  │ 3. 收集日志    │ →  │ 4. 分析     │ →  │ 5. 生成报告   │ →  │ 6. 故障处理   │
│ setup-env.sh │    │ run-checks.sh│    │collect-logs-  │    │analyze-    │    │ gen-report.sh │    │cordon-       │
│              │    │              │    │cloud.sh       │    │logs.sh     │    │               │    │faulty.sh     │
└─────────────┘    └──────────────┘    └───────────────┘    └────────────┘    └──────────────┘    └──────────────┘
```

## 快速开始（5 分钟上手）

### 前提条件

- `kubectl` 已安装，且目标集群的 kubeconfig context 已配置
- `gcloud` 已安装，且有目标 GCP project 的 named configuration
- `helm` 已安装（环境准备步骤需要）
- `python3` 已安装（日志分析和报告生成需要）
- `envsubst` 已安装（来自 GNU gettext，YAML 模板渲染需要）

### 1. 选择 Profile

| 集群 | Profile | kubectl context |
|---|---|---|
| GB300 GKE (us-central1) | `qa/profiles/gb300-gke-taiji.sh` | `gke_tencent-gcp-taiji-poc_us-central1_gb300-gke-test` |
| GB200 GKE (us-east1) | `qa/profiles/gb200-gke-taiji.sh` | `gke_tencent-gcp-taiji-poc_us-east1_gb200-gke-test` |

Profile 已绑定 `QA_KUBE_CONTEXT`，**无需手动 `kubectl config use-context`**。

### 2. 环境准备（首次使用）

检测并安装 DRA Driver / IMEX Channel DS / MPI Operator / JobSet：

```bash
bash qa/setup-env.sh qa/profiles/gb200-gke-taiji.sh
```

幂等：已装的跳过，缺的自动安装。通常 1-2 分钟。

### 3. 执行质检

```bash
# 单节点全量（hw-check + dcgm + nccl-single + cublas），约 15-30 分钟
bash qa/run-checks.sh qa/profiles/gb200-gke-taiji.sh all 0014

# 全面质检（上述 + 单域多节点 NCCL），约 30-45 分钟
bash qa/run-checks.sh qa/profiles/gb200-gke-taiji.sh all-full 0014
```

`0014` 是 sub-block 编号，对应 node pool `gb200-pool-0014`。

### 4. 收集日志

```bash
# 等 Cloud Logging 摄取（~2 分钟），然后从 manifest 批量收集
bash qa/collect-logs-cloud.sh qa/profiles/gb200-gke-taiji.sh \
  --manifest logs/qa-manifest-gb200-0014-*.txt
```

### 5. 生成报告

```bash
bash qa/gen-report.sh qa/profiles/gb200-gke-taiji.sh \
  logs/qa-manifest-gb200-0014-*.txt
```

报告输出到 `docs/qa-report-gb200-YYYYMMDD.md`。

### 6. 故障处理

```bash
# 列出故障节点（dry-run，不执行 cordon）
bash qa/cordon-faulty.sh qa/profiles/gb200-gke-taiji.sh \
  logs/qa-hw-check-gb200-0014-*/ --dry-run

# 确认后实际 cordon
bash qa/cordon-faulty.sh qa/profiles/gb200-gke-taiji.sh \
  logs/qa-hw-check-gb200-0014-*/ --cordon
```

## 文件结构

```
qa/
├── setup-env.sh                    # 环境准备（检测+安装 k8s 组件）
├── run-checks.sh                   # 测试编排（输出 manifest）
├── collect-logs-cloud.sh           # Cloud Logging 日志收集
├── analyze-logs.sh                 # 日志分析（逐节点结果 + 离群检测）
├── gen-report.sh                   # 报告自动生成
├── cordon-faulty.sh                # 故障节点 cordon + physicalHost
├── profiles/
│   ├── profile-template.sh         # 新环境 profile 模板（复制后填写）
│   ├── gb300-gke-taiji.sh          # GB300 GKE 生产 profile
│   ├── gb200-gke-taiji.sh          # GB200 GKE 生产 profile
│   └── gb200-gke-playground.sh     # GB200 playground 模板（待填 TODO）
├── templates/                      # YAML 模板（${QA_*} 变量由 envsubst 替换）
│   ├── hw-check.yaml               # 硬件自检 DaemonSet (13 项)
│   ├── dcgm-diag.yaml              # DCGM r2 DaemonSet
│   ├── nccl-single-node.yaml       # 单机 NCCL DaemonSet
│   ├── cublas-bench.yaml           # cuBLAS GEMM DaemonSet
│   ├── nccl-multi-node.yaml        # 单域多节点 NCCL JobSet
│   └── nccl-cross-domain.yaml      # 跨域 NCCL JobSet (2 ComputeDomain)
└── README.md                       # 本文件
```

## 所有 Action

| Action | 用法 | 说明 |
|---|---|---|
| `hw-check` | `run-checks.sh <profile> hw-check <sub> [node]` | 13 项硬件自检 |
| `dcgm` | `run-checks.sh <profile> dcgm <sub>` | DCGM r2 (PCIe/显存/HBM) |
| `nccl` | `run-checks.sh <profile> nccl <sub> [node]` | 单机 4-GPU NCCL |
| `gemm` | `run-checks.sh <profile> gemm <sub> [node]` | cuBLAS 6 精度 GEMM |
| `nccl-multi` | `run-checks.sh <profile> nccl-multi <sub> --mnnvl=on\|off` | 单域多节点 NCCL |
| `nccl-cross` | `run-checks.sh <profile> nccl-cross <sub1> <sub2>` | 跨域 NCCL (MNNVL=2) |
| `all` | `run-checks.sh <profile> all <sub> [node]` | 全部单节点测试 |
| `all-full` | `run-checks.sh <profile> all-full <sub>` | 单节点 + 单域多节点 NCCL |
| `all-with-multi` | `run-checks.sh <profile> all-with-multi <sub>` | 单节点 + MNNVL on+off |
| `clean` | `run-checks.sh <profile> clean [sub]` | 清理 namespace |
| `logs` | `run-checks.sh <profile> logs <sub>` | 查看当前 pod 状态 |

## 新环境接入

### 1. 创建 Profile

```bash
cp qa/profiles/profile-template.sh qa/profiles/my-cluster.sh
```

编辑 `my-cluster.sh`，填写 6 个必填项：

```bash
export QA_GPU_TYPE=gb300                    # gb200 | gb300
export QA_KUBE_CONTEXT="gke_PROJECT_REGION_CLUSTER"  # kubectl context 名
export QA_GCLOUD_CONFIG="my-config"         # gcloud named configuration
export QA_PROJECT="my-gcp-project"          # GCP project ID
export QA_ZONE="us-central1-b"              # GCE zone
export QA_POOL_FALLBACK_PREFIX="gb300-pool" # node pool 名前缀
```

其他变量有合理默认值，按需调整。完整变量列表见 `profile-template.sh` 中的注释。

### 2. 配置 kubectl context

```bash
# GKE 集群
gcloud container clusters get-credentials <CLUSTER> \
  --region <REGION> --project <PROJECT>

# 确认 context 名
kubectl config get-contexts
```

将 context 名填入 profile 的 `QA_KUBE_CONTEXT`。

### 3. 配置 gcloud named configuration

```bash
gcloud config configurations create my-config
gcloud config set project my-gcp-project
gcloud config set compute/zone us-central1-b
gcloud auth activate-service-account --key-file=keys/sa.json
```

将 configuration 名填入 profile 的 `QA_GCLOUD_CONFIG`。

### 4. 运行环境准备

```bash
bash qa/setup-env.sh qa/profiles/my-cluster.sh
```

### 5. 开跑

```bash
bash qa/run-checks.sh qa/profiles/my-cluster.sh all <subblock>
```

## 质检项目详情

### hw-check（13 项）

| # | 检查项 | PASS 条件 |
|---|---|---|
| 1a | GPU 可见性 (nvidia-smi) | 数量 = 期望值（默认 4） |
| 1b | GPU 可见性 (CUDA) | CUDA 设备数 = nvidia-smi 数（捕获 GPUrequiresreset） |
| 2 | GPU 健康 | 温度 < 阈值，UECC = 0，无 pending retired pages |
| 3 | GPU 显存 | 总量 >= 阈值（默认 100 GB） |
| 4 | NVLink topo | GPU 间全部 NV18（无 PCIe fallback） |
| 5 | Fabric clique | 全部 GPU 在同一 clique，State=Completed |
| 6 | NVLink errors | 0 replay/recovery/crc/ecc errors |
| 7 | GPU Topology | 输出 topo 矩阵（信息项，不判 PASS/FAIL） |
| 8 | RDMA NIC | 全部端口 Active（GB300: 8 口，GB200: 4 口） |
| 9 | GPU 时钟 | SM clock >= 90% max，无 HW slowdown |
| 10 | ECC 模式 | 全部 GPU ECC Enabled |
| 11 | RDMA error counters | 无 port_rcv_errors / xmit_discards / symbol_error 等 |
| 12 | RDMA 固件 | 节点内固件版本一致 |

### DCGM r2

DCGM 4.6.0+ 诊断（3.x 不支持 Blackwell），测 PCIe 带宽/replays、显存完整性、HBM 带宽。

### NCCL 单机

4-GPU NVLink 测试，4 个 collective（all_reduce / all_gather / reduce_scatter / alltoall），16G message busBW。

### cuBLAS GEMM

6 精度 × 4 GPU：FP4 / FP8 / FP16 / BF16 / TF32 / FP32。benchmark binary 运行时从 GitHub 下载。

### NCCL 多节点

- **单域**: 同 pool 全部健康节点，JobSet + ComputeDomain，mpirun 协调
- **跨域**: 2 个 pool，2 个 ComputeDomain，MNNVL=2（域内 NVSwitch + 跨域 RDMA）

## 并行质检

每个 domain 使用独立 namespace `gpu-qa-${SUBBLOCK}`，可安全并行：

```bash
for D in 0001 0002 0003; do
  bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh all $D &
done
wait
```

Profile 中 `QA_PREFLIGHT_STAGGER_S` 控制启动间隔（默认 2 秒 × subblock 号），避免并发打 API。

## 日志分析

```bash
# 自动检测类型
bash qa/analyze-logs.sh logs/qa-hw-check-gb200-0014-*/
bash qa/analyze-logs.sh logs/qa-nccl-single-gb200-0014-*/
bash qa/analyze-logs.sh logs/qa-cublas-bench-gb200-0014-*/
```

输出逐节点 PASS/FAIL 表 + 关键指标统计（busBW / TFLOPS）+ 离群检测。

## Exit Code

| 脚本 | 0 | 1 | 2 |
|---|---|---|---|
| `run-checks.sh` | 全部测试 PASS | 有测试 FAIL 或 TIMEOUT | — |
| `collect-logs-cloud.sh` | 日志收集完整 | 有不完整日志 | — |
| `gen-report.sh` | 报告生成成功 | 参数错误或生成失败 | — |
| `cordon-faulty.sh` | 无故障节点 | 有故障节点 | 参数错误 |
| `setup-env.sh` | 全部组件就绪 | 有组件安装失败 | — |

## 环境准备详情

`setup-env.sh` 检测并安装以下组件（版本可在 profile 中覆盖）：

| 组件 | 默认版本 | Profile 变量 | 安装方式 |
|---|---|---|---|
| DRA Driver | v0.4.1 | `QA_DRA_VERSION` | Helm chart |
| IMEX Channel Init DS | — | — | kubectl apply (inline YAML) |
| MPI Operator | v0.8.2 | `QA_MPI_VERSION` | kubectl apply (GitHub release) |
| JobSet | v0.12.0 | `QA_JOBSET_VERSION` | kubectl apply (GitHub release) |

额外操作：
- DRA Driver 安装后自动创建 ResourceQuota（pods: 500），解决 GKE `system-node-critical` 优先级限制
- 安装后验证 ComputeDomain CRD 和 ResourceSlice 发布

## 踩坑

### GKE IMEX Channel

GKE COS 上 NVIDIA driver 不创建 `/dev/nvidia-caps-imex-channels/` 设备节点。`setup-env.sh` 自动部署 `imex-channel-init` DaemonSet 解决。

### DRA Pod Quota

GKE 不允许在无 ResourceQuota 的 namespace 创建 `system-node-critical` 优先级 pod。`setup-env.sh` 自动创建 quota。

### MPI LD_LIBRARY_PATH

多节点 NCCL 的 mpirun `-x LD_LIBRARY_PATH` 必须包含 `/usr/local/nvidia/lib64`，否则 worker 节点报 CUDA driver version insufficient。

### cuBLAS 二进制下载

benchmark binary 在 pod 内从 GitHub 下载。GKE 节点需能访问 `raw.githubusercontent.com`。

### DCGM 版本

DCGM 3.x 不支持 Blackwell GPU，需 4.6.0+。镜像通过 profile 变量 `QA_DCGM_IMAGE` 配置。

### NCCL Message Size

alltoall/all_gather/reduce_scatter 的 16G 实际 size 不是精确 `17179869184`（element count 按 GPU 数对齐）。分析脚本用前缀匹配。

### 跨域 ComputeDomain

`numNodes: 0` 让 DRA 自动分配。Namespace 命名 `qa-cd-<sub1>-<sub2>`（≤64 字符 FQDN 限制）。
