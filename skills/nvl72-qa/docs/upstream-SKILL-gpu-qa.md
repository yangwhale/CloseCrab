---
name: gpu-qa
description: >-
  GPU 节点质检（GB200/GB300 GKE 集群）。触发短语：
  "质检" / "跑质检" / "全面质检" / "启动质检" / "QA" / "run QA" / "hardware check" /
  "对 pool-XXXX 质检" / "跑 hw-check" / "跑 nccl" / "跑 cublas" / "跑 dcgm" /
  "跑 gemm" / "多节点 nccl" / "跨域 nccl" / "环境准备" / "setup env" /
  "生成质检报告" / "生成报告" / "cordon 故障节点"
---

# GPU QA Skill

对 GKE 集群上的 GB200/GB300 GPU 节点执行端到端质检：环境准备 → 测试 → 日志收集 → 分析 → 报告 → 故障处理。

所有脚本在 `qa/` 目录下，profile 配置在 `qa/profiles/` 下。

---

## 0. 前置条件

开始质检前必须确认以下条件就绪。**任何一项不满足都不要继续**，先帮用户解决。

### 基础设施

| 条件 | 检查方法 | 不满足时怎么做 |
|---|---|---|
| GKE 集群已创建且可达 | `kubectl --context=<ctx> cluster-info` | 找集群管理员或参考 GCP 文档创建集群 |
| GPU Node Pool 已部署且节点 Ready | `kubectl --context=<ctx> get nodes -l cloud.google.com/gke-accelerator=nvidia-<gpu_type>` | 创建 node pool（需 reservation + placement policy） |
| GPU 节点未被其他负载占满 | `kubectl --context=<ctx> get pods -A -o wide \| grep <node>` | 清理或等待现有负载完成 |

### 本机工具

| 工具 | 检查方法 | 安装方式 |
|---|---|---|
| `kubectl` | `kubectl version --client` | `gcloud components install kubectl` |
| `gcloud` | `gcloud version` | https://cloud.google.com/sdk/docs/install |
| `helm` | `helm version` | `curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \| bash` |
| `python3` | `python3 --version` | 系统包管理器 |
| `envsubst` | `envsubst --version` | `apt install gettext-base` / `brew install gettext` |

### 认证

| 条件 | 检查方法 | 不满足时怎么做 |
|---|---|---|
| kubeconfig context 已配置 | `kubectl config get-contexts \| grep <cluster>` | `gcloud container clusters get-credentials <cluster> --region <region> --project <project>` |
| gcloud named configuration 已创建 | `gcloud config configurations list \| grep <config>` | `gcloud config configurations create <name>` + `gcloud config set project <project>` + 认证 |
| gcloud 有 Cloud Logging 读权限 | `gcloud logging read "resource.type=k8s_container" --limit=1 --project=<project>` | 给 SA 加 `roles/logging.viewer` |

### Profile

| 条件 | 检查方法 | 不满足时怎么做 |
|---|---|---|
| Profile 文件存在 | `ls qa/profiles/<name>.sh` | 复制 `qa/profiles/profile-template.sh`，填写必填项 |
| 必填项已填写 | `source <profile> && echo $QA_KUBE_CONTEXT $QA_PROJECT` | 编辑 profile，填写 `QA_KUBE_CONTEXT`、`QA_GCLOUD_CONFIG`、`QA_PROJECT`、`QA_ZONE`、`QA_POOL_FALLBACK_PREFIX`、`QA_GPU_TYPE` |
| context 能连通 | `kubectl --context=$QA_KUBE_CONTEXT get nodes` | 检查 kubeconfig 或网络 |

### 前置条件速查命令

一行命令验证所有前置条件（替换 profile 路径）：

```bash
source qa/profiles/gb200-gke-taiji.sh && \
  echo "1. kubectl: $(kubectl version --client -o json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["clientVersion"]["gitVersion"])' 2>/dev/null || echo MISSING)" && \
  echo "2. helm: $(helm version --short 2>/dev/null || echo MISSING)" && \
  echo "3. python3: $(python3 --version 2>/dev/null || echo MISSING)" && \
  echo "4. envsubst: $(envsubst --version 2>/dev/null | head -1 || echo MISSING)" && \
  echo "5. context: $(kubectl --context=${QA_KUBE_CONTEXT} cluster-info 2>/dev/null | head -1 || echo UNREACHABLE)" && \
  echo "6. gcloud: $(gcloud --configuration=${QA_GCLOUD_CONFIG} config get project 2>/dev/null || echo MISSING)" && \
  echo "7. nodes: $(kubectl --context=${QA_KUBE_CONTEXT} get nodes -l cloud.google.com/gke-accelerator=nvidia-${QA_GPU_TYPE} --no-headers 2>/dev/null | wc -l) Ready"
```

---

## 快速参考：自然语言 → action 映射

### 组合 action（一句话触发完整流程）

| 用户说 | action | 包含的测试 |
|---|---|---|
| "质检 0003" / "对 pool-0003 质检" / "跑质检 0003" / "QA 0003" | `all 0003` | hw-check → dcgm → nccl → cublas（单节点） |
| "全面质检 0003" / "完整质检 0003" / "全量质检" | `all-full 0003` | 上述 4 项 + 多节点 RDMA + 多节点 MNNVL（域内全部健康节点） |

### 单项 action

| 用户说 | action | 说明 |
|---|---|---|
| "跑 hw-check 0003" / "硬件检查 0003" | `hw-check 0003` | 14 项硬件自检 |
| "跑 dcgm 0003" | `dcgm 0003` | DCGM r2 诊断（PCIe+显存+HBM） |
| "跑 nccl 0003" / "单机 nccl" | `nccl 0003` | 单机 NCCL 4 collective（NVLink） |
| "跑 cublas 0003" / "跑 gemm 0003" | `gemm 0003` | cuBLAS 6 精度 GEMM |
| "多节点 nccl 0003" / "域内 nccl" | `nccl-multi 0003 --mnnvl=on` | 域内多节点 MNNVL |
| "多节点 rdma 0003" / "域内 rdma" | `nccl-multi 0003 --mnnvl=off` | 域内多节点 RDMA（关闭 MNNVL） |
| "跨域 nccl 0003 0005" / "cross domain" | `nccl-cross 0003 0005` | 跨域 NCCL（2 个 sub-block） |

### 辅助操作

| 用户说 | 操作 | 说明 |
|---|---|---|
| "环境准备" / "setup env" | `setup-env.sh` | 安装 DRA/IMEX/MPI/JobSet（幂等） |
| "生成报告" / "生成质检报告" | `gen-report.sh` | 从日志生成 markdown 报告到 `qa/docs/` |
| "cordon 故障节点" | `cordon-faulty.sh` | 分析日志 → 列出故障 → 用户确认 → cordon |
| "收集日志" | `collect-logs-cloud.sh` | 从 Cloud Logging 拉取日志 |
| "分析日志 <目录>" | `analyze-logs.sh` | 日志分析 + 离群检测 |

### 指定单节点

任何 action 后加节点尾缀可限定单台：`"对 0003 的 04fk 跑 hw-check"` → `hw-check 0003 04fk`

### 默认 profile 推断

| 用户提到 | profile |
|---|---|
| gb300 / pool-0001~0012 / d0001~d0012 | `qa/profiles/gb300-gke-taiji.sh` |
| gb200 / pool-0014 | `qa/profiles/gb200-gke-taiji.sh` |
| 未指定 | 问"哪个集群？GB200 还是 GB300？" |

---

## 1. 确定 Profile

根据用户提到的集群/GPU 型号选择 profile：

| 关键词 | Profile |
|---|---|
| gb300 / pool-0001~0012 | `qa/profiles/gb300-gke-taiji.sh` |
| gb200 / pool-0014 | `qa/profiles/gb200-gke-taiji.sh` |

如果用户没指定，问一句"哪个集群？GB200 还是 GB300？"

Profile 已包含 `QA_KUBE_CONTEXT`，脚本会自动用正确的 kubectl context，无需手动切换。

---

## 2. 环境准备（首次或新集群）

```bash
bash qa/setup-env.sh <profile>
```

幂等：已装的组件跳过，缺的自动安装（DRA Driver / IMEX Channel DS / MPI Operator / JobSet）。

运行方式：直接 Bash 执行，不用后台。通常 1-2 分钟完成。

---

## 3. 执行质检

### 单节点全面质检

```bash
bash qa/run-checks.sh <profile> all <subblock>
```

- 后台运行（`nohup ... &`）+ Monitor 监控进度
- 4 项测试顺序执行：hw-check → dcgm r2 → nccl-single → cublas
- 预计 15-30 分钟
- 输出 manifest 文件到 `qa/logs/qa-manifest-*.txt`
- hw-check 完成后自动收集 nvidia-bug-report.log.gz 到 `qa/logs/qa-bug-reports-*/`
- 渲染后的 YAML 保存到 `qa/logs/rendered-*.yaml`

### 全面质检（单节点 + 多节点 RDMA + MNNVL）

```bash
bash qa/run-checks.sh <profile> all-full <subblock>
```

- 在 `all` 基础上追加：多节点 RDMA（mnnvl=off，测 CX-8 NIC）+ 多节点 MNNVL（mnnvl=on，测 NVSwitch）
- 域内所有健康节点参与多节点测试
- 需要 DRA RDMA 网络已配置
- 预计 30-45 分钟

### 跨域 NCCL

```bash
# 2 域 32N
bash qa/run-checks.sh <profile> nccl-cross <sub1> <sub2>

# 4 域 64N — 需要手动扩展模板或连续跑 2 次 2 域
```

### 单项测试

```bash
bash qa/run-checks.sh <profile> hw-check <subblock> [node-suffix]
bash qa/run-checks.sh <profile> dcgm <subblock>
bash qa/run-checks.sh <profile> nccl <subblock>
bash qa/run-checks.sh <profile> gemm <subblock>
bash qa/run-checks.sh <profile> nccl-multi <subblock> --mnnvl=on
```

### 监控

用 Monitor 工具跟踪进度日志：
```bash
tail -f /tmp/qa-<run>.log | grep -E --line-buffered "===|FAIL|ERROR|完成|Ready"
```

Monitor grep 必须包含 stuck 信号（FAIL/ERROR/timeout），不能只 grep 成功信号。

---

## 4. 日志收集

测试完成后，用 Cloud Logging 收集日志：

```bash
bash qa/collect-logs-cloud.sh <profile> --manifest <manifest-file>
```

- 从 manifest 文件批量收集所有测试日志
- 不依赖 pod/namespace 存活（可在 cleanup 后运行）
- 日志写入 `qa/logs/qa-<test>-<gpu>-<sub>-<timestamp>/` 目录
- 每节点一个 `.log` 文件

---

## 5. 分析

```bash
bash qa/analyze-logs.sh <log-dir> [type]
```

- type 自动检测：hw-check / nccl-single / nccl-cross / cublas-bench / dcgm
- 输出逐节点结果表 + 离群检测
- NCCL: 各 collective 16G busBW 统计
- cuBLAS: 各精度 TFLOPS 统计

---

## 6. 报告生成

```bash
bash qa/gen-report.sh <profile> <manifest-file> [output-file]
```

- 从 manifest 关联的日志目录自动生成 markdown 报告
- 结构：TL;DR → 集群概览 → 单节点质检 → 多节点 NCCL → 故障节点
- 默认输出到 `docs/qa-report-<gpu_type>-<date>.md`

---

## 7. 故障处理

```bash
# 列出故障节点（dry-run，不执行 cordon）
bash qa/cordon-faulty.sh <profile> <hw-check-logdir> [dcgm-logdir] --dry-run

# 实际 cordon
bash qa/cordon-faulty.sh <profile> <hw-check-logdir> [dcgm-logdir] --cordon
```

- 从 hw-check/dcgm 日志中识别故障节点
- 查询并打印完整 physicalHost（不截断）
- `--cordon` 执行前先展示列表，**让用户确认后再执行**

---

## 8. 端到端流程

当用户说"对 XXX 做全面质检"时，按以下顺序操作：

1. **确定 profile** — 根据 GPU 型号 / 集群
2. **环境准备** — `setup-env.sh`（首次或不确定时跑一次）
3. **执行质检** — `run-checks.sh all-full <subblock>`（后台 + Monitor）
4. **收集日志** — `collect-logs-cloud.sh --manifest <manifest>`
5. **分析** — `analyze-logs.sh` 对每个日志目录
6. **报告** — `gen-report.sh` 生成 markdown
7. **故障处理** — `cordon-faulty.sh --dry-run` 列出故障 → 用户确认 → `--cordon`
8. **记录** — 结果追加到 `docs/operations.md`

每一步完成后向用户汇报进展。故障 cordon 必须用户确认。

---

## 9. 注意事项

- **Monitor 必须覆盖 stuck 信号**：grep 必须含 FAIL/ERROR/timeout，不能只等成功
- **日志收集完再清理**：不要在日志收集前删除 namespace/pod
- **physicalHost 不截断**：故障报告中 physicalHost 必须完整写出
- **operations.md 必须记录**：每次 GCP 资源变更（cordon/uncordon/reset）立即追加
- **exit code**：`run-checks.sh` 测试失败时 exit 1，不再静默 exit 0
- **context 安全**：profile 绑定 kubectl context，不会操作错误集群
