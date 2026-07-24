---
name: qa-toolkit-design
description: "质检 skill /gpu-qa — 用户提到\"质检\"时触发此 skill，覆盖环境准备→测试→日志→分析→报告→cordon 全流程"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1d46131b-e454-47aa-9615-b08009477d3c
---

## 质检统一入口：/gpu-qa skill

用户提到"质检"、"hw-check"、"NCCL 测试"、"cuBLAS"、"DCGM"、"检查节点"、"跨域"、"环境准备"、"cordon 故障"等质检相关操作时，**触发 `/gpu-qa` skill**。

skill 定义: `.claude/skills/gpu-qa/SKILL.md`
详细文档: `qa/README.md`

### 端到端流程（6 步）

```
1. setup-env.sh     → 检测+安装 DRA/IMEX/MPI/JobSet（幂等）
2. run-checks.sh    → 执行质检（输出 manifest）
3. collect-logs-cloud.sh → Cloud Logging 日志收集
4. analyze-logs.sh  → 日志分析
5. gen-report.sh    → 自动生成 markdown 报告
6. cordon-faulty.sh → 故障 cordon（需用户确认）
```

### Profile

| 集群 | Profile | kubectl context |
|---|---|---|
| GB300 GKE | `qa/profiles/gb300-gke-taiji.sh` | `gke_tencent-gcp-taiji-poc_us-central1_gb300-gke-test` |
| GB200 GKE | `qa/profiles/gb200-gke-taiji.sh` | `gke_tencent-gcp-taiji-poc_us-east1_gb200-gke-test` |

Profile 已绑定 `QA_KUBE_CONTEXT`，脚本自动带 `--context`，无需手动切换。新环境复制 `profiles/profile-template.sh` 填写。

### 快速参考

```bash
PROFILE=qa/profiles/gb200-gke-taiji.sh

# 环境准备（首次）
bash qa/setup-env.sh $PROFILE

# 单节点全量（hw-check + dcgm + nccl-single + cublas）
bash qa/run-checks.sh $PROFILE all <subblock>

# 全面质检（上述 + 单域多节点 NCCL）
bash qa/run-checks.sh $PROFILE all-full <subblock>

# 跨域 NCCL
bash qa/run-checks.sh $PROFILE nccl-cross <sub1> <sub2>

# 日志收集
bash qa/collect-logs-cloud.sh $PROFILE --manifest logs/qa-manifest-*.txt

# 报告生成
bash qa/gen-report.sh $PROFILE logs/qa-manifest-*.txt

# 故障处理（先 dry-run，确认后 --cordon）
bash qa/cordon-faulty.sh $PROFILE logs/qa-hw-check-*/ --dry-run
bash qa/cordon-faulty.sh $PROFILE logs/qa-hw-check-*/ --cordon
```

### 文件结构

```
qa/
├── setup-env.sh                    # 环境准备（DRA/IMEX/MPI/JobSet）
├── run-checks.sh                   # 测试编排（ktl context 包装 + exit code）
├── collect-logs-cloud.sh           # Cloud Logging 日志收集
├── analyze-logs.sh                 # 日志分析
├── gen-report.sh                   # 报告自动生成
├── cordon-faulty.sh                # 故障 cordon + physicalHost
├── profiles/
│   ├── profile-template.sh         # 新环境模板
│   ├── gb300-gke-taiji.sh          # GB300 生产
│   └── gb200-gke-taiji.sh          # GB200 生产
├── templates/                      # YAML 模板（hw-check/dcgm/nccl/cublas/multi/cross）
├── README.md                       # 完整文档（新手可读）
└── .claude/skills/gpu-qa/SKILL.md  # Skill 定义
```

### 关键设计

- **kubectl context 安全**: 所有 kubectl 调用通过 `ktl()` 包装，自动带 `--context=$QA_KUBE_CONTEXT`，启动时校验连通性
- **exit code 传播**: `run_test()` 追踪 `wait_completion` 返回值，`FAIL_COUNT` 计数，`all`/`all-full` 失败时 exit 1
- **manifest TIMEOUT 标记**: wait_completion 超时的条目标记 `|TIMEOUT`，日志收集可跳过
- **测试与日志分离**: run-checks 只跑测试 + cleanup，Cloud Logging 异步收集
- **preflight 修复**: 用 process substitution 替代 pipe，gcloud 批量限流正常工作
- **DCGM 镜像参数化**: `${QA_DCGM_IMAGE}` 从 profile 读，不再硬编码在模板里
- **报告自动生成**: gen-report.sh 从 manifest + 日志目录提取数据，python3 解析 NCCL/cuBLAS
- **cordon 安全**: cordon-faulty.sh 先 dry-run 列出故障 + physicalHost，需用户确认才执行

### 踩坑速查

- IMEX channel: GKE COS 不自动创建，setup-env.sh 自动部署 DS
- DRA pod quota: GKE 需 ResourceQuota 允许 system-node-critical，setup-env.sh 自动创建
- MPI LD_LIBRARY_PATH: 必须包含 `/usr/local/nvidia/lib64`
- cuBLAS 下载: pod 内从 GitHub 下载 binary，需能访问 raw.githubusercontent.com
- DCGM 版本: 需 4.6.0+，3.x 不支持 Blackwell
- NCCL size 匹配: 用 `1717986` 前缀（按 GPU 数对齐，不固定 17179869184）
- Cloud Logging 延迟: pod 删后等 2-3 分钟再收集
- 跨域 namespace: `qa-cd-<sub1>-<sub2>`（64 字符 FQDN 限制）
- physicalHost 不截断: 故障报告必须完整写出 [[gke-fault-node-physicalhost]]
- 删除前确认日志: [[feedback-verify-before-delete]]

**Why:** 从手动 6 步编排升级为 skill 驱动的自动化流程，其他人/agent 可一句话触发端到端质检。
**How to apply:** 看到质检相关请求，触发 `/gpu-qa` skill，按 skill 中的 8 步流程执行。
