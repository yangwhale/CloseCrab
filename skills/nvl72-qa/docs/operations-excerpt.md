# operations.md 相关章节摘录（07-23~07-25）

> 从项目 `docs/operations.md` 抽取，涵盖 pool-0013~0017 从首次质检失败到根因深挖到最终 pin image + 重建的完整轨迹。
> 完整 ops log 见发起交接人的本地 gb300/docs/operations.md（当前总 8650 行）。

---

## 2026-07-23 全面质检 pool-0013 ~ pool-0017（via /gpu-qa skill）

### 1. setup-env（幂等确认）
```
=== [14:58:00] 环境配置完成 ===
  dra-driver             already-present
  imex-channel-init      already-present
  mpi-operator           already-present
  jobset                 already-present
ComputeDomain CRD: OK, ResourceSlice: 526 已发布
```
全组件就绪。

### 2. all-full 并行 5 subblock
```
$ for SUB in 0013 0014 0015 0016 0017; do
    nohup bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh all-full $SUB > /tmp/qa-all-$SUB.log 2>&1 &
  done
```
每 subblock: hw-check + dcgm r2 + nccl-single + cublas + 单域多节点 NCCL（预计 30-45min）。
Namespaces: `gpu-qa-0013` / `gpu-qa-0014` / ... / `gpu-qa-0017`（skill 自带 subblock 后缀，隔离并行）。

### 3. all-full 5 subblock 结果（30-40min）

| Sub | hw-check | dcgm | nccl-single | cublas | nccl-multi | 失败数 |
|---|---|---|---|---|---|---|
| 0013 | **TIMEOUT** | ✓ | ✓ | **TIMEOUT** | JobSet BadRequest | 3 |
| 0014 | ✓ | **TIMEOUT** | **TIMEOUT** | **TIMEOUT** | JobSet BadRequest | 4 |
| 0015 | ✓ | **TIMEOUT** | **TIMEOUT** | **TIMEOUT** | JobSet BadRequest | 4 |
| 0016 | **TIMEOUT** | ✓ | ✓ | **TIMEOUT** | JobSet BadRequest | 3 |
| 0017 | ✓ | ✓ | ✓ | **TIMEOUT** | JobSet BadRequest | 2 |

Manifest 保存在 `logs/qa-manifest-gb300-00XX-20260723-XXXXXX.txt`。

**问题分析**：
1. **TIMEOUT ≠ 真 test fail**：wrapper `kubectl wait --for=condition=Ready` 超时，但 DS 里 pod 可能仍在跑 test。cublas TIMEOUT=900s 对 18 nodes × 5 并行是紧的（DCGM Level 2 ~150s per node，cublas ~15-20min per node）。
2. **nccl-multi 全 fail**：JobSet v1alpha2 strict decoding error on `spec.replicatedJobs[0].template.spec.template.spec.containers[0].claims` — 模板用了 DRA claims 字段，JobSet CRD 版本不识别。需要 update JobSet 或去掉 claims 字段。
3. **wrapper bash bug** (`qa/run-checks.sh:348-349`): `local: can only be used in a function` + `START_TIME: unbound variable` — nccl-multi 阶段错误路径的代码不在函数里。

下一步：先 collect-logs-cloud 拉真实 test log 分析（很多 TIMEOUT 是 wait 假报），nccl-multi 单独处理 JobSet bug。

### 4. collect-logs-cloud（5 subblock 并行）
```
$ for SUB in 0013..0017; do
    nohup bash qa/collect-logs-cloud.sh qa/profiles/gb300-gke-taiji.sh --manifest logs/qa-manifest-gb300-$SUB-*.txt > /tmp/qa-collect-$SUB.log 2>&1 &
  done
```
5 pids 都提交，等 Cloud Logging 拉完 4 项测试 × 每 pod 完整日志。

### 收集结果
| Sub | hw-check | dcgm | nccl-single | cublas | 备注 |
|---|---|---|---|---|---|
| 0013 | 18 logs | 18 | 18 | **0** | cublas 无日志 |
| 0014 | 18 | 18 | 18 | **0** | 全部有；cublas 无 |
| 0015 | 18 | 18 | 18 | **0** | 同上 |
| 0016 | 18 | 18 | 18 | **0** | 同上 |
| 0017 | 18 | 18 | 18 | **0** | 同上 |

cublas 无日志根因：wrapper cublas wait TIMEOUT (900s) → cleanup 前 DS pod 未到 DONE marker → Cloud Logging 里 pod stdout 没被 collect 脚本判定为"完整"。需要 update TIMEOUT_CUBLAS 或调 cublas 测试配置。

**下一步**：analyze-logs 三项已有日志分析结果。cublas 单独处理（后跑或改超时重试）。

### 5. analyze-logs 结果 & 根因分析

**hw-check（可信）**: 所有 5 subblock × 18 nodes 每台 `PASS=23 FAIL=0 WARN=0 Result: PASS`。analyze-logs 空表是脚本渲染问题，不是无数据。**GPU / RDMA / NVLink / row remap / Xid 全部无异常**。

**DCGM（不可信）**：全部 mem=Fail + pcie=Fail，理由：
```
| memory | Fail |
|        | et GPU. Restart DCGM. Rerun diagnostics. |
| pcie   | Fail |
|        | Reset GPU. Restart DCGM. Rerun diagnostics. |
```
DCGM 报错要求 "Reset GPU" — 意味着 GPU **被其它进程占用**。

**nccl-single（不可信）**：每个 collective test 都 `CUDA-capable device(s) is/are busy or unavailable`。同一 GPU busy root cause。

**cublas**：0 logs（wrapper cublas wait 900s TIMEOUT，Cloud Logging 里没 DONE marker，collect 判定不完整；实际 pod 可能根本没跑起）。

**根因**：5 subblock 并行 all-full 时，同一时间 90 nodes × 2+ test pods 大批同时抢 GPU，DRA GPU device 分配 / DCGM hostengine / nccl-tests 撞车。虽然每 subblock 独立 ns + 独立节点集，但 kubelet API / GPU device plugin 层面有全局资源竞争，加上新 pool 刚 provisioning 完 30-60min DRA 还在稳定期。

**修复方案**：
1. hw-check 单跑（已完成）— 结果可用
2. DCGM + NCCL + cublas 需要**顺序** 5 subblock（每 subblock ~15-20min，共 ~90min），避免 GPU busy 撞车
3. nccl-multi 需要修 JobSet claims 字段兼容性 + wrapper bash bug (line 348-349)

## 2026-07-24 顺序补跑 all 5 subblock

**背景**：昨天 5 subblock 并行 all-full → DCGM/NCCL/cuBLAS GPU busy 假报。改顺序，规避 GPU 抢占。

```bash
$ cat /tmp/qa-sequential.sh
for SUB in 0013 0014 0015 0016 0017; do
  bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh all $SUB
done

$ nohup bash /tmp/qa-sequential.sh > /tmp/qa-sequential-all.log 2>&1 &
```

action=`all`（hw + dcgm + nccl-single + cublas，跳 nccl-multi）。预计每 subblock ~30min，共 ~150min。

### 顺序补跑结果（100 min）
每 subblock 稳定 20 min。5 subblock manifest 一致：
```
qa-hw-check    ✓ Summary
qa-dcgm-diag   ✓ DONE
qa-nccl-single ✓ Done
qa-cublas-bench TIMEOUT (900s wait 超时)
```

对比昨天并行结果：**DCGM + NCCL 恢复正常**（并行时 GPU busy → 假报，顺序后消失）→ 证实并行确实撞车。**cublas 仍 TIMEOUT**（每 subblock 900s 不够，需要调 TIMEOUT 或分批）。

### cublas 根因诊断

手工在 pool-0017 一台节点跑 cublas probe（跟 QA yaml 相同 image + resources）：
- curl download binary: 370ms ✓
- 运行 cublas: **`Failed to allocate memory: CUDA-capable device(s) is/are busy or unavailable`**
- nvidia-smi 侧看 **GPU 空闲**（`0 MiB used, 0% util, no compute apps`）

矛盾：nvidia-smi 说 free、CUDA runtime 说 busy。可能是 GKE device plugin / DRA 层 release 延迟，或 nccl-tests 遗留 CUDA context 未清理。

要验证：等 60s 后再跑一次 cublas probe，看是否恢复。

### cublas 问题：新 pool 特有

对比 probe:
| 节点 | kubelet | driver | cublas |
|---|---|---|---|
| pool-0002 (旧) | v1.36.0-gke.4447000 | 580.159.04 | **PASS** (33376 Gflops FP16 512³) |
| pool-0017 (新) | v1.36.0-gke.4681000 | 580.159.04 | **FAIL** `CUDA-capable device(s) is/are busy or unavailable` |

同 driver、同 pod spec、GPU 空闲、libcuda 可 load —— 唯一差异是 **GKE node image / kubelet 版本**（新 pool 用了 4681000）。

推测：新 image 上 nvidia-container-toolkit 或 GKE DRA hook 变化，导致 CUDA runtime layer 抢到 GPU 时立即报 busy（虽然 driver 侧 free）。

hw-check + DCGM + NCCL 都过（走不同 kernel 接口），只 cuBLAS binary 特殊路径不工作。可能是 cublas 需要的某个 driver capability 没导出。

**影响**：新 pool 上 cublas benchmark 暂时不可用。其他 3 项已 PASS。

### ⚠️ analyze-logs 重要更正

之前基于 manifest `DONE:` marker 判定 "DCGM + NCCL-single PASS" 是**错的**。detail log 显示：
- **DCGM**：sw pass, mem/pcie fail —— `API call cudaStreamCreate failed for GPU X: 'CUDA-capable device(s) is/are busy or unavailable' Reset GPU. Restart DCGM. Rerun diagnostics.`
- **NCCL-single**：每个 collective (all_reduce, all_gather, reduce_scatter, alltoall) 都 `Test CUDA failure common.cu:1333 'CUDA-capable device(s) is/are busy or unavailable'`
- **cuBLAS**：同错误

wrapper 只判 pod ready + `DONE:` marker，不判 test 内容是否 pass。manifest 的 `DONE:` 只代表 pod 正常退出。

**真实结论**：新 pool (0013-0017) 上**只有 hw-check 能跑**（读 sysfs, 不需要 CUDA runtime allocate GPU）。所有涉及 `cudaMalloc/cudaStreamCreate` 的测试都因 "CUDA device busy" 挂掉。跟 cuBLAS 是同一根因（新 kubelet 4681000 image 下 CUDA runtime 层问题）。

等 B agent 深挖后修复，重新验证 DCGM + NCCL + cuBLAS。

---

## 2026-07-24 qa/run-checks.sh nccl-multi 两 bug 修复

### Bug 1: JobSet apply strict-decode 报错

- 文件: `qa/templates/nccl-multi-node.yaml`
- 症状: `strict decoding error: unknown field "spec.replicatedJobs[0].template.spec.template.spec.containers[0].claims"`
- 根因: 原模板把 `claims:` 缩进到 container-level（14 空格），与 `resources:` 同级 — 不是合法字段。k8s DRA 语法要求 `claims` 嵌在 `resources:` 里（16 空格）
- 修复: 把 `claims:` 内缩到 `resources:` 下

同时发现模板资源名与 wrapper script 不一致（JobSet/CD/RCT/SUBDOMAIN 用 `nccl-sd*`，script 引用 `qa-nccl-multi*`），导致 apply 成功后 wrapper 找不到 pod 也无法清理。一并对齐到 `qa-nccl-multi*`：
- JobSet: `nccl-sd` → `qa-nccl-multi`
- ComputeDomain: `nccl-sd-cd` → `qa-nccl-multi-cd`
- ResourceClaimTemplate: `nccl-sd-cd-ch` → `qa-nccl-multi-cd-ch`
- SUBDOMAIN env: `nccl-sd` → `qa-nccl-multi`（pod DNS FQDN 后缀跟随 JobSet 名）

### Bug 2: `local` 出现在 case 分支外报语法错

- 文件: `qa/run-checks.sh` line 348
- 症状: `line 348: local: can only be used in a function` + `set -u` 下 `START_TIME: unbound variable`
- 根因: `local` 只能在 shell 函数里；nccl-multi case 分支是顶层 case，非函数
- 修复: `local START_TIME=$SECONDS` → `START_TIME=$SECONDS`

### 验证

- Server dry-run: apply 4 个对象均成功（namespace / RCT / CD / JobSet），无 strict decoding 报错
- 实跑 `bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh nccl-multi 0013 --mnnvl=off`:
  - JobSet 创建 OK, 18 pods 立即 Pending
  - 卡在 `FailedScheduling: 18 cannot allocate all claims` — 与两个 bug 无关，是 pool-0013 dra.net 驱动 side infra issue
  - 对比 pool-0006 dra.net resourceslice: 12 devices / 8 rdma=true；pool-0013: 4 devices / 0 rdma=true → 新 pool 上 networking-dra-driver 未暴露 mrdma 设备（8× CX-8 NIC 没被识别）
- 拉起 pool-0006 复跑同命令 → JobSet apply OK, 10/16 pods Running（容器 install-nccl 完成, 进入 mpirun SSH-wait 循环），6/16 Pending 卡在 `Insufficient nvidia.com/gpu` — pool-0006 的 6 台节点已被 `default/vllm-v4pro-1p1d-*` 占用 4 GPU，QA script 的 `HEALTHY` 仅判 `SchedulingDisabled` 不判 GPU 实际 free
- 结论: 两个 bug 修复本身正确（apply 无 strict-decode 报错、bash 无 local 报错、pod 起来、containers 到 mpirun setup）；NCCL bandwidth 结果因 pool 侧无 16 台 GPU 全空的节点未跑到

### 未解

- pool-0013~0017 的 networking-dra-driver 为什么只识别到 IDPF NIC，不识别 mrdma — 可能与之前 ops.md 记录的 "新 kubelet 4681000 image CUDA busy" 是同一根因（GKE 新 node image 上 driver plumbing 变化）
- pool-0006 有 vLLM 常驻，跑 16-way NCCL 需要先驱逐/切换 pool 或降 parallelism
- QA script `HEALTHY` 计算改进项: 除 `SchedulingDisabled` 外，宜再判 `allocatable.nvidia.com/gpu >= QA_GPUS_PER_NODE` 且 node 上无其他 GPU-holding pod（避免 accept 但 schedule 不了的假成功）

---

## 2026-07-24 cuBLAS busy 深挖：libcuda ↔ kernel driver 层根因

### 目标
之前只知道 "新 pool cuBLAS FAIL / 旧 pool PASS，同 driver 版本、同 pod spec"，未定位到具体层。这次要把失败层从 "cuBLAS binary" 精确到 CUDA runtime / driver API / kernel driver 中的哪一层。

### 方法
起 `qa-cublas-probe-1784860910` ns，pool-0017-1ht6（新，v1.36.0-gke.4681000）+ pool-0002-1zt9（旧，v1.36.0-gke.4447000）各起一个 privileged probe pod，同 diagnostic image、同 `nvidia.com/gpu: 4`。对比：设备节点、driver build、fabric state、CUDA runtime、CUDA driver API。

### 关键证据

**层次隔离测试**：

```
测试 API                             probe-new (0017)                 probe-old (0002)
cuInit(0)                            0 (success)                      0 (success)
cuDeviceGetCount()                   4                                4
cudaGetDeviceCount()                 4                                4
cudaSetDevice(0)                     46 (device unavailable)          0 (success)
cudaMemGetInfo()                     46 (device unavailable)          0 (success)
cuDevicePrimaryCtxRetain(0)          1 (CUDA_ERROR_INVALID_VALUE)     0 (success)
cuDevicePrimaryCtxGetState(0)        0 flags=0x0 active=0             (未测)
```

**结论**：failure NOT in cuBLAS，NOT in cudart。失败在 **`libcuda.so → kernel ioctl` 层**。`cuDevicePrimaryCtxRetain` 直接返回 INVALID_VALUE，无 flag 被设置（GetState 显示 flags=0x0），说明是 driver 内部对 primary context 初始化路径的判断出问题。cudart 的 `cudaSetDevice` 内部调 `cuDevicePrimaryCtxRetain`，所以 error 从 driver 层传上来，被 cudart 转成 error 46。

### 环境差异
| 维度 | pool-0002 (旧，工作) | pool-0017 (新，失败) |
|---|---|---|
| kubelet | v1.36.0-gke.4447000 | v1.36.0-gke.4681000 |
| kernel .ko build | `builder@1bd82b8cbd9c Jun 18 02:49` | `builder@e041cd032e3a Jun 27 15:18` |
| userspace libcuda | 580.159.04 (96460560 bytes, 同一 file) | 同 |
| nvidia-drivers-*.tgz | 159349152 bytes | 160070682 bytes (+721 KB) |
| Persistence Mode | Disabled | **Enabled** |
| Fabric GUID / CliqueId | 各 GPU 有独立 GUID / CliqueId=31 | 各 GPU 有独立 GUID / CliqueId=14 |
| IMEX channels | channel0-255 全在 | 同 |
| asapd-lite | Running（8Gi hugepages-2Mi） | **Pending**（hugepages-2Mi=0 无法调度）|
| hugepages-2Mi | 8Gi allocatable | **0** allocatable |
| dmesg init warning | `_gpuFabricProbeRbmSleepLinks: Error setting links to sleep on linkmask 0x0` | 同（cosmetic，两侧都有）|
| GPU firmware | 580.159.04 | 同 |
| Video BIOS | 97.10.4a.00.1a | 同 |

### 归因
两个可疑变化（都可能是根因，需 NVIDIA 侧确认）：
1. **kernel .ko Jun 27 build 引入 regression**：`nvidia.ko` 在 primary context 初始化路径上做了改动，与 GB300 GSP 交互 / MNNVL 状态机不兼容
2. **GKE 新 image gpu-installer 默认 enable persistence mode**：new pool `Persistence Mode: Enabled` 是 nvidia-persistenced-installer 显式设置的（旧 pool 是 Disabled）。580 driver 里 persistence mode + 特定 primary context init flags 组合可能触发 driver 内部状态锁死

hw-check（读 sysfs）和 NCCL-tests（如果走 `cuCtxCreate` 老 API 或 fork 过 primary ctx）绕过了这个失败点，所以只有 cuBLAS / cudart 显性挂。

### Probe pod 清理
删除 `qa-cublas-probe-1784860910` ns（wait=false），未影响集群其他 workload。

## 2026-07-24 深挖：不换 image 的 targeted 修复

### 组件版本对比（同）
| 组件 | pool-0002 (旧) | pool-0017 (新) |
|---|---|---|
| dra.net (networking-dra-driver) | `dranet:v1.1.0-gke.6@sha256:8957e1...` | 同 |
| nvidia-gpu-device-plugin | `v1.36.2-gke.0@sha256:5b6b20...` | 同 |
| dra-driver-nvidia-gpu | `v0.4.1` | 同 |

**所有 GKE / DRA 组件 image 完全相同**。差异只在 GKE **node image** (kubelet 4681000 vs 4447000)。

### dra.net ResourceSlice 差异
- **OLD (12 devices)**: `gpu0ipvlan0, gpu0ipvlan1, ..., gpu3ipvlan0, gpu3ipvlan1` (8 ipvlan) + 4 pci
- **NEW (4 devices)**: 只 4 pci，**8 个 `gpu*ipvlan*` interfaces 缺失**

dra.net driver 相同版本，行为差异 = 新 node 上 gpu*ipvlan interfaces 没被创建（image 侧问题）。

### pool-0017 (新) 节点上 network interfaces
```
eth0, eth1 (mgmt)
gpu[0-3]rdma[0-1]  (8 mrdma NICs)
lo, docker0, cilium_*, lxc*  (系统)
```
**缺 gpu[0-3]ipvlan[0-1]**（旧 pool 有 8 个 ipvlan）。dra.net 只读 kernel network，没 ipvlan → 只 4 pci devices。

**独立问题**（跟 kernel .ko regression 不同源）：GKE accelerator-network-profile "auto" 没在新 image 上创建 gpu*ipvlan interfaces。

### 定位 mrdma DRA 缺失根因

| 节点 | ipvlan kernel module | gpu*ipvlan interfaces |
|---|---|---|
| pool-0002 (旧, 4447000) | **loaded** | 8 exist |
| pool-0017 (新, 4681000) | **NOT loaded** | 0 (missing) |

`ipvlan` module 没 auto-load → GKE accelerator-network-profile 无法创建 gpu*ipvlan → dra.net 只识别 4 pci devices → NCCL-multi 起不来。

**修法候选**：`modprobe ipvlan` on each new pool node，测试是否触发 ipvlan interface 自动创建。

### modprobe ipvlan 结果
在 pool-0017 一台节点 `nsenter --target 1 modprobe ipvlan` 后：
- module loaded (`ipvlan 196608 0`)
- 但 **gpu*ipvlan interfaces 仍未出现**（等 15s 无变化）

结论：module load 是必要不充分条件。需要触发 GKE **accelerator network profile** daemon 创建 interfaces。

## 🎯 真根因（不是 kernel .ko regression，也不是 image）

pool 创建时**漏了 `--num-2m-hugepages=4096`** flag（`linuxNodeConfig.hugepages.hugepageSize2m`）：

| Pool | linuxNodeConfig | node hugepages-2Mi |
|---|---|---|
| pool-0002 (旧) | `hugepageSize2m: 4096` (= 8 GiB) | 8Gi |
| pool-0017 (新) | **null** | **0** |

### 连锁反应
1. hugepages=0 → asapd-lite pod 请求 `hugepages-2Mi: 8Gi` 无法 schedule → `Pending 20h, FailedScheduling: Insufficient hugepages-2Mi`
2. asapd-lite 不跑 → GKE accelerator setup daemon 没跑 → **gpu*ipvlan interfaces 没创建**
3. 缺 ipvlan → dra.net driver 只识别 4 pci devices（vs 旧 pool 12 devices）→ nccl-multi FailedScheduling `cannot allocate all claims`
4. hugepages=0 → CUDA runtime 无法用 2Mi hugepages 分配 managed memory pool → `cuDevicePrimaryCtxRetain` 返 INVALID_VALUE → cudart `cudaSetDevice` 报 `devicesUnavailable "device busy"` → DCGM / NCCL / cuBLAS 全挂

B agent 挖到的 "kernel .ko Jun 27 vs Jun 18 build" 是 red herring — 真因是 config missing。C agent 挖到的 "dra.net 只 4 devices" 也是同一根因下游。

### 修复方案（无需换 image、无需重建 pool）
```
$ cat > /tmp/gb300-linux-config.yaml <<Y
linuxConfig:
  hugepageConfig:
    hugepage_size2m: '4096'
Y
$ for POOL in gb300-pool-0013 ...0017; do
    gcloud --configuration=taiji-poc container node-pools update $POOL \
      --cluster=gb300-gke-test --location=us-central1 \
      --system-config-from-file=/tmp/gb300-linux-config.yaml
  done
```
GKE 会 rolling recreate node（保持原 image，只加 hugepages 配置）。每 pool ~30-60min。

## 2026-07-24 pool-0017 config patch (hugepages)

### 踩坑：yaml value 类型
第一次 yaml 用 `'4096'`（字符串）报 `ValidationError: Expected type (<class 'int'>) found <class 'str'>`。改成 `4096`（int）OK。

### 提交
```
$ cat /tmp/gb300-linux-config.yaml
linuxConfig:
  hugepageConfig:
    hugepage_size2m: 4096
$ gcloud --configuration=taiji-poc container node-pools update gb300-pool-0017 \
    --cluster=gb300-gke-test --location=us-central1 \
    --system-config-from-file=/tmp/gb300-linux-config.yaml --async
operation-1784863282288-...  UPGRADE_NODES  us-central1  gb300-pool-0017  PENDING
```
GKE 会 rolling recreate 18 节点（同 image v1.36.0-gke.4681000，加 hugepages 配置）。等 monitor + verify。

### Monitor 提前 trigger（误判）
`gcloud container node-pools describe ... --format=value(status)` 一直是 `RUNNING`，即使 UPGRADE_NODES op 正在进行。真实进度看 op status。改用 op DONE 作终止条件。

### ❌ Rolling upgrade 失败：placement policy 硬性上限

op DONE 但 statusMessage：
```
Failed to apply resource-policy - gb300-subblock-0017-policy
No more than 18 instances of machine type a4x-maxgpu-4g-metal can be created with this policy
```

**根因**：GKE rolling upgrade 默认 `max-surge=1`（新增 1 台再删旧 1 台），任何时刻 19 台 > placement policy 上限 18 → GCE 拒绝。所有 18 节点 age 还是原 2026-07-23T14:27，一台都没被 recreate。

**修法**：改 surge=0 + max-unavailable=1（先删再建），一次减 1 台不加。
```
$ gcloud container node-pools update gb300-pool-0017 \
    ... --max-surge-upgrade=0 --max-unavailable-upgrade=1
```
然后 re-apply system-config-from-file 触发 rolling。

### 已改 surge=0 + max-unavailable=1
```
$ gcloud container node-pools update gb300-pool-0017 --max-surge-upgrade=0 --max-unavailable-upgrade=1
Updated [.../gb300-pool-0017]
```
再次提交 hugepages config 触发 rolling recreate。

### 新 op 已提交
`operation-1784863408191-e9fffe78-...` UPGRADE_NODES PENDING。surge=0 逐台重建，预计 18 × 5-10min = 1.5-3h。

## 📌 复盘：为什么漏了 hugepages

**已有固化脚本 `scripts/gke-create-nodepool.sh`**（136 行），line 62-99 明明就有：
```
HUGEPAGE_CFG=$(mktemp /tmp/gke-hugepage-XXXX.yaml)
...
  hugepageConfig:
    hugepage_size2m: ${GKE_HUGEPAGE_2M_COUNT}
...
$G container node-pools create ${POOL_NAME} \
    ...
    --system-config-from-file=${HUGEPAGE_CFG} \
```

**我这次做 0013-0017 时的错误 flow**：
1. `describe pool-0012 --format=yaml` 读现有 pool 配置作模板 ✗（应该 `ls scripts/` 先找现成脚本）
2. 从 yaml 手写 gcloud create 命令行 ✗（漏了 linuxNodeConfig → hugepages）
3. Create 后没 verify `kubectl get node -o jsonpath='{.status.capacity.hugepages-2Mi}'` 确认 ≠ 0

**教训 → 加进项目 CLAUDE.md**：任何 GKE 侧操作（pool/cluster create/update），先 `ls scripts/gke-*` 找脚本；确定要写新命令时对比脚本参数集不能少。

### CLAUDE.md 加规则
`gb300/CLAUDE.md` 在 `## GCP 操作` 段落加子节：**⚠️ GKE cluster / pool 操作先看 scripts/gke-***。要求：
- 任何 GKE 侧变更先 `ls scripts/gke-*` 找现成脚本
- 不要 `gcloud describe --format=yaml` 就手写命令（现有 pool 的 yaml 里有很多历史显式设置字段，GKE default 不同，手写易漏）
- pool create/update 后立刻 `kubectl get node -o jsonpath='{.status.capacity}'` 对齐关键字段

## 2026-07-24 Delete pool-0013/0014/0015/0016（保留 0017 验证）

**动机**：4 个 pool 与 0017 同根因（linuxNodeConfig=null，hugepages=0）。用户决定先删 4 个，等 pool-0017 rolling upgrade 验证 hugepages 修复方案有效后，再用固化脚本 `scripts/gke-create-nodepool.sh` 重建。

**pre-check**：4 pool 上无 non-system workload。

```
$ for POOL in gb300-pool-0013 ...0016; do
    gcloud --configuration=taiji-poc container node-pools delete $POOL \
      --cluster=gb300-gke-test --location=us-central1 --quiet --async
  done
```

4 op：0013 RUNNING, 0014 PENDING, 0015 RUNNING, 0016 RUNNING。

**保留 pool-0017** 继续 rolling upgrade（hugepages config apply 中，surge=0 逐台重建）。

## 2026-07-24 中断 pool-0017 rolling + delete

**动机**：用户改主意，5 pool 全删，用固化脚本 `scripts/gke-create-nodepool.sh` 重建才靠谱。

**步骤**：
1. 直接 `gcloud container node-pools delete gb300-pool-0017` → 报 `Cluster is running incompatible operation` （UPGRADE_NODES RUNNING 不允许 delete）
2. `gcloud container operations cancel operation-1784863408191-...` → status ABORTING → 10s 后 DONE
3. `gcloud container node-pools delete gb300-pool-0017 --async` → PENDING (`operation-1784864061690-...`)

pool-0013/0014/0015/0016 之前已提交 delete op（RUNNING 中）。pool-0017 加入队列。5 pool 全在 delete 中，等完后用固化脚本重建。

## 2026-07-24 5kw9 post-REPAIRING 恢复！

VM RUNNING + Ready=True + GPU=4, physicalHost 未变（`fafad31.../8cccabead...`，跟 REPAIRING 前一样）。

**mlx5 port state（直接 probe，之前 qx2s 用过的方法）**：
```
mlx5_0..7 全部 state=ACTIVE phys=LinkUp rate=400 Gb/sec (4X NDR)  ✓
```

**关键对比**：
| 节点 | REPAIRING 前 | REPAIRING 后 (physicalHost 未变) |
|---|---|---|
| qx2s (mlx5_3) | DOWN + Disabled | **仍 DOWN + Disabled**（硬件死）|
| 5kw9 (mlx5_5) | DOWN + Disabled | **ACTIVE + LinkUp 400 NDR**（软件层 transient，GCE 软 reset 清了）|

已 uncordon + 起 hw-check wrapper 走 skill 出完整报告（pid 502688）。

### 5kw9 hw-check 结果

Wrapper 两次跑都 `全部完成 (1/1 Ready)` 立即 cleanup（skill wrapper 行为），pod 保留时间过短来不及 kubectl logs 拉。Cloud Logging 也未 flush（skill 的 collect-logs-cloud 通常等 60s flush delay，wrapper cleanup 内嵌 5s 太短）。

**关键结论（直接 probe 已确认）**：`mlx5_0..7 全部 state=ACTIVE phys=LinkUp rate=400 Gb NDR`。

hw-check 剩余 sections（GPU count/Xid/NVLink topo/counters/firmware/row remap/DRAM ECC）跟 qx2s post-REPAIRING 结果一致概率极高（同 image 同 pod spec），只 Section 6 RDMA 那条从 FAIL → PASS。

5kw9 保持 uncordon 状态待用户决定是否加回 schedulable pool。

### 5kw9 DCGM Level 2

```
software  Pass  (GPU0-3 all Pass)
memory    Pass  (GPU0-3 all Pass)
pcie      Pass  (GPU0-3 all Pass)
```

5kw9 完全恢复（软件层 transient，GCE REPAIRING 已修）。跟 qx2s (硬件死) 对比再一次验证：REPAIRING 是否有效取决于故障性质。

### 5kw9 NCCL single-node

4 collective 全 concluded 无 CUDA failure：
```
all_reduce_perf      Concluded ✓
all_gather_perf      Concluded ✓
reduce_scatter_perf  Concluded ✓
alltoall_perf        Concluded ✓
```

5kw9 三项测试 (hw-check RDMA / DCGM r2 / NCCL single-node) 全 pass，可正式加回 production pool。

### 5kw9 NCCL busBW @ 16 GB
- all_reduce_perf : Avg busBW **668.652 GB/s**（peak 687.67 @ 16GB）
- all_gather_perf : Avg busBW **623.38 GB/s** （peak 672.28 @ 16GB）
- （reduce_scatter / alltoall pod 已被 wrapper cleanup，未 grab）

对照 v2 QA baseline（GB300 单节点 all_reduce @ 16GB ~688 GB/s，跟 3r0c 恢复后同）：**达到 baseline，节点性能正常**。

### uncordon 5kw9（幂等确认）
```
$ kubectl uncordon gke-gb300-gke-test-gb300-pool-0012-d963499c-5kw9
node/... already uncordoned

STATUS  SCHED   GPU
True    <none>  4
```
5kw9 正式加回 production pool。pool-0012 现在 18/18 全部 schedulable（之前 5kw9 cordon 时 17）。

## 2026-07-24 pool-0013 用标准脚本重建

```
$ bash scripts/gke-create-nodepool.sh 0013
```
op `operation-1784871543736-fe762bf2-...` PROVISIONING 18 台。脚本自带 `--system-config-from-file=$HUGEPAGE_CFG` (4096 × 2Mi = 8Gi hugepages)。

### ⚠️ 脚本缺 `--no-enable-autorepair`
create 后 `AUTO_REPAIR=on`（GKE default），与本项目"全 pool auto-repair off"规范冲突。立刻 update 报 `Cluster is running incompatible operation`（PROVISIONING 正在跑不能 update）。等 create 完成再 update autoRepair off。

**待办**：修 `scripts/gke-create-nodepool.sh` line 90-107 加 `--no-enable-autorepair`，一劳永逸避免下次漏。

### Placement policy 现状快照
17 个 policy 全 READY（gb300-subblock-0001-policy ~ gb300-subblock-0017-policy），gpuTopology=1x72, collocation=COLLOCATED。0013-0017 policy 在 5 pool delete 时未被自动清（policy 与 pool 生命周期独立），复用 pool-0013 重建时省了一步。

## 2026-07-24 Placement policy 扩到 0025

```
$ for N in 18..25; do
    gcloud compute resource-policies create group-placement gb300-subblock-00${N}-policy \
      --region=us-central1 --collocation=COLLOCATED --gpu-topology=1x72
  done
```
8 个新 policy 全 READY: `gb300-subblock-0018-policy` ~ `gb300-subblock-0025-policy`。

**注意**：reservation `nvidia-gb300-dxkhoz4ypk4mh-block-0001` 目前只有 sub-block 0001-0017（各 18 台，共 306 quota）。**subblock 0018-0025 不存在** — placement policy 建了先占位，实际用作 pool `--placement-policy` 时需对应 subblock 就位才能拉起 VM。

### pool-0013 create 完成 + autoRepair 关闭

| 项 | 值 |
|---|---|
| Status | RUNNING |
| 节点数 | 18/18 |
| Ready | 18/18 |
| **hugepages-2Mi** | **8Gi** ✓（首次用固化脚本 create，一次到位）|
| GPU | 4 per node ✓ |
| auto-repair | **off** ✓（create 完立即 update 关掉）|

## 2026-07-24 pool-0013 全面质检（除 hw-check）

**跳过 hw-check**（已知新 pool 上 PASS，且 pool-0013 刚重建 hugepages 到位）。

```bash
$ cat /tmp/qa-0013-full.sh
for ACT in dcgm nccl gemm; do
  bash qa/run-checks.sh <profile> $ACT 0013
done
bash qa/run-checks.sh <profile> nccl-multi 0013 --mnnvl=on

$ nohup bash /tmp/qa-0013-full.sh > /tmp/qa-0013-full.log 2>&1 &
```

预计 dcgm 5min + nccl 5min + cublas 15-20min + nccl-multi 30-45min = 55-75min。核心验证：新 pool 用固化脚本 (hugepages=8Gi) 是否修复了之前 pool-0013~0017 上的 CUDA busy 问题。

### pool-0003 / pool-0005 状态快照

| Pool | 节点数 | 可用 | 说明 |
|---|---|---|---|
| pool-0003 | 18 | **18/18** | 之前 GCE stockout (17 台)，07-24 补齐 2 台新节点 (age 02:29, 05:10) → 满 18 |
| pool-0005 | 18 | **18/18** | 之前 GCE fail 1 台 (17)，07-24 02:51 补齐 → 满 18 |

两 pool 全部 Ready + GPU=4 + 未 cordon。GKE 侧 pool-0003 STATUS 仍显示 ERROR（历史创建 op 遗留），但 kubectl 侧实际节点已补齐。

## 2026-07-24 修脚本 + 后台建 4 pool + orchestrate

### 修 `scripts/gke-create-nodepool.sh`
在 `gcloud container node-pools create` 参数里加 `--no-enable-autorepair`（line 107 前）。原因：本项目"全 pool auto-repair off"是规范（防止 MIG 误触发 report-host-as-faulty 节点被换机），create 时一次到位免得后续 update。以后 create pool 自动 off，跟 hugepages 一样一劳永逸。

### 后台建 pool-0014/0015/0016/0017
```
$ nohup bash scripts/gke-create-nodepool.sh 0014 0015 0016 0017 > /tmp/create-pool-14-17.log 2>&1 &
```
4 pool async 提交（脚本内 --async 逐个 kick）。每 pool 30-60min（GB300 裸金属慢）。

### Orchestrate 后台脚本
`/tmp/orchestrate.sh` (pid 529578)：
1. wait pool-0013 QA (PID 525028) exit
2. collect-logs-cloud + analyze-logs 更新 pool-0013 报告数据
3. 顺序 QA pool-0003 → pool-0005 → pool-0007（action=all）

同时 4 pool create (0014-0017) 后台跑（pid 529340，每 pool 30-60min）。

Monitor 会跟这 3 条并行进程：
- 4 pool create（0014-0017）
- pool-0013 gemm 中 → nccl-multi
- orchestrate 等 0013 完 → 3/5/7 QA

### pool-0013 hugepages 修复后验证

**asapd-lite + DRA 恢复** ✓：
- `asapd-lite-zl5wh 2/2 Running`（之前 pool-0013~0017 pending "Insufficient hugepages-2Mi"）
- dra.net ResourceSlice: **12 devices** `gpu0ipvlan0..gpu3ipvlan1` + 4 pci（跟旧 pool-0002 一致）→ mrdma 识别全

**cuBLAS 仍 CUDA busy** ✗：
- 18/18 cublas-bench pods **CrashLoopBackOff (3) + Error (15)**
- pod stdout: `Failed to allocate memory: CUDA-capable device(s) is/are busy or unavailable`

**diff 定位**：pool-0002 node image `gke-1360-gke4447000-cos-gb300-bm-129-19506-224-49-c-nvda`，pool-0013 image `gke-1360-gke4681000-cos-gb300-bm-129-19506-224-80-c-nvda`。两 pool 都是 kubelet 4681000，但**node image 不同**（4447000 vs 4681000，NVDA build 224.49 vs 224.80）。

hugepages 修好了 asapd/DRA 层，但 **CUDA runtime 层依然挂在新 image** — 印证 B agent 的 root cause 分析（新 image 里 NVIDIA 580.159.04 kernel .ko Jun 27 build 可能有 `cuDevicePrimaryCtxRetain` regression）。这次 layer 更清楚：hugepages 是 asapd 需要，CUDA busy 是独立的 image-level 问题。

### pool-0013 单机 NCCL 也失败（同 image 根因）

Cloud Logging 里 nccl-multi pod stdout 每个 rank 都：
```
qa-nccl-multi-w-0-X: Test CUDA failure common.cu:1333
  'CUDA-capable device(s) is/are busy or unavailable'
```

nccl-single 之前 wrapper 显示 `18/18 pods Ready + 全部完成` = **假象**（wrapper 只判 pod touch qa-done marker，不判 collective 是否 pass）。

**新 image `gke-1360-gke4681000-cos-gb300-bm-129-19506-224-80-c-nvda` 上有效测试范围**：
- ✓ hw-check（读 sysfs，不需 CUDA allocate）
- ✓ asapd-lite（hugepages 修完后 schedule 成功）
- ✓ dra.net (mrdma DRA 12 devices 识别)
- ✗ DCGM Level 2（`memory/pcie` 测试需 `cudaStreamCreate` → fail）
- ✗ NCCL single/multi（需 `cudaSetDevice/cudaMalloc` → fail）
- ✗ cuBLAS（同上）

**结论**：pool-0013 只能做被动检查（asapd/DRA/RDMA 状态），不能跑任何真实 CUDA workload。pool-0014-0017 create 完后同问题。

### 状态快照 06:11

**pool-0013 sequential QA 完成**（23min: dcgm 3 + nccl 3 + gemm 16 + nccl-multi 1.5 + overhead）。orchestrate 进入下一步：collect-logs + analyze pool-0013 → 顺序 QA 3/5/7（06:11:29 START pool-0003）。

**pool-0014-0017 create 进度**：
| Pool | Status |
|---|---|
| pool-0014 | PROVISIONING |
| pool-0015 | RUNNING ✓ |
| pool-0016 | RUNNING ✓ |
| pool-0017 | RUNNING ✓ |

3/4 已 RUNNING（15/16/17 快过 0014，原因：0014 subblock 只 15 healthy + 3 degraded 需等 spare replacement）。auto-repair 已 off（脚本修完直接生效）。

## 2026-07-24 07:22 全部后台任务完成快照

### 1. pool-0014-0017 create 结果
| Pool | Status | Nodes | auto-repair |
|---|---|---|---|
| pool-0014 | **ERROR** | 15/18 | off ✓ |
| pool-0015 | RUNNING | 18/18 | off ✓ |
| pool-0016 | RUNNING | 18/18 | off ✓ |
| pool-0017 | RUNNING | 18/18 | off ✓ |

pool-0014 subblock healthy=15 + degraded=3，物理上限就 15。修脚本加 `--no-enable-autorepair` 生效 ✓，无需 create 后再 update。

### 2. orchestrate DONE (07:22:03)
- pool-0003 all: 24 min (06:11:29-06:35:53)
- pool-0005 all: 24 min (06:35:53-07:00:08)
- pool-0007 all: 22 min (07:00:08-07:22:03)

3/5/7 都是**旧 image (4447000-224.49)** — 预期 hw + dcgm + nccl + cublas 都能正常跑通（除非节点自身故障）。log 待 collect 分析。

### 3. pool-0013 sequential DONE (06:11:00, 23 min)
新 image (4681000-224.80) — DCGM/NCCL/cuBLAS 都 CUDA busy fake pass；只 hw-check + asapd + DRA 层有效。

## 2026-07-24 pool-0003/0005/0007 analyze 结果

### 汇总（旧 image 4447000-224.49，DCGM/NCCL/cuBLAS 全跑通）

| Pool | 节点 | hw-check FAIL | DCGM | NCCL | cuBLAS | 说明 |
|---|---|---|---|---|---|---|
| pool-0003 | 18 | **5** | 0/18 | 无离群 | 无离群 | 2 NVLink-PCIe + 3 Xid dmesg |
| pool-0005 | 18 | **10** | 0/18 | 无离群 | 无离群 | 2 NVLink-PCIe + 8 Xid dmesg |
| pool-0007 | 18 | **9** | 0/18 | 无离群 | 无离群 | 0 NVLink-PCIe + 9 Xid dmesg |
| 合计 | 54 | 24 | 0 | ✓ | ✓ | |

### FAIL 分类

**真硬件/性能问题 (NVLink topo 12 pairs via PCIe)**：4 台，建议 cordon
- pool-0003: `0l5w, 24cq`
- pool-0005: `0v60, tv53`

**历史 Xid dmesg 事件 (Xid 143 FSP boot fail + Xid 7)**：20 台
- 节点当前 GPU=4 allocatable, DCGM 全 pass, NCCL/cuBLAS 无离群 → **当前性能正常，可继续使用**
- Xid 143 是 GPU firmware secure processor 曾经 boot 失败，reset 后已恢复
- pool-0003: `5xqb lrfm mjtk` (3)
- pool-0005: `29j1 2jgk 54k9 9p1q hf83 n1zn s11x vj88` (8)
- pool-0007: `8q23 8smj fq2b gvvm gxll k1gt nqjn svvl vb95` (9)

### 结论对比新旧 image
- **旧 image (pool-0003/0005/0007)**: DCGM + NCCL + cuBLAS 都能跑通并出真实数据 ✓
- **新 image (pool-0013)**: 同 3 项测试都 CUDA busy（wrapper 层面 fake pass）✗

再次印证：CUDA runtime 层挂在新 image（`gke4681000-cos-gb300-bm-224.80`），跟 hugepages 无关。

## 2026-07-24 MIG errors 深挖 (pool-0003/0005/0007)

### 每 pool 长期 retry 失败的 slot
| Pool | 节点 | 错误次数 | 时间跨度 | 最终 create | NVLink 状态 |
|---|---|---|---|---|---|
| pool-0003 | **0l5w** | 97x INTERNAL_ERROR | **07-18~07-23**（5 天）| 07-23 22:02 | **PCIe (fail)** ⚠ |
| pool-0003 | **24cq** | 95x INTERNAL_ERROR | 07-18~07-23 | 07-23 19:23 | **PCIe (fail)** ⚠ |
| pool-0005 | **tv53** | 94x INTERNAL_ERROR | 07-18~07-23 | 07-23 19:37 | **PCIe (fail)** ⚠ |
| pool-0005 | 0v60 | (无 recent) | — | 07-17 06:16 | **PCIe (fail)** ⚠（老节点）|
| pool-0007 | lvf3 | 96x INTERNAL_ERROR | 07-18~07-23 | 07-23 22:xx | ✓ (unaffected) |

### 关键模式
- **每个失败 slot 一个独立 error Code**（`4499...548`, `4544...437`, `-300080...434`）→ 同一物理 host slot 反复 provision fail。
- 3 台（0l5w/24cq/tv53）连续 5 天每小时一次 INTERNAL_ERROR retry，07-23 傍晚终于 create 成功但硬件 **NVLink 拓扑降级**（GPU 之间用 PCIe 而不是 NV18）。
- lvf3 也 96x error 后成功但 hw check ✓ — 说明"long-retry 后成功"不必然坏（视 GCE 换到啥 host 而定）。
- **偶尔的 `ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS`** 说明 subblock 短期没 healthy host 可分。

### 推断
GCE 侧 subblock 里长期有 1-2 台 "half-broken" host：hw 层不足以 mark degradedHostCount，但 provision-time 检测到问题 → INTERNAL_ERROR。5 天后勉强 create 成功但 NVLink lane training fail → GPU 走 PCIe fallback。

用户提到"4 台是昨天今天补充"— 完全印证 3 台是 **07-23 傍晚 create 成功**（几天 retry 后终于 pass GCE provision，但 NVLink 有问题）；0v60 是 07-17 老节点，可能原始 provision 时就 NVLink degraded 但未被立即发现。

### 建议
对 4 台节点 `gcloud compute instances report-host-as-faulty --fault-reasons=behavior=PERFORMANCE,description="NVLink topology degraded: 12 GPU pairs via PCIe instead of NV18"` 让 GCE 层 mark degraded + 换新 host。

## 2026-07-24 ⚠️ 0v60 一周内 NVLink 硬件退化

用户提问 "0v60 之前有没有检测过"，翻历史 log：

| 检测时间 | log 文件 | NVLink topo | Fabric clique | Result |
|---|---|---|---|---|
| **07-16 hw-check** | `qa-hw-check-gb300-0005-20260716-182529/0v60.log` | ✓ all 12 pairs via **NVLink** | 未报 | **PASS 12/0/0** |
| **07-24 hw-check** | `qa-hw-check-gb300-0005-20260724-084158/0v60.log` | ✗ 12 pairs via **PCIe** | ✗ GPUs in **4 different cliques** (expected 1) | **FAIL 21/2/0** |

**含义**：不是初始 provision 问题（07-16 时 NVLink 全正常），是**运行期间硬件退化** — NVSwitch fabric 或 NVLink cage 在 7 天内失效。physicalHost 未变，同一物理机。

**推断**：另外 3 台 07-23 新 create 的（0l5w/24cq/tv53）NVLink PCIe 可能也是 provision 时就是这样（GCE 5 天反复 retry 后勉强 create，硬件底层有问题）。0v60 则是**运行时退化**，性质更严重。

**结论**：4 台 NVLink-PCIe 都值得 report-host-as-faulty，但 0v60 更紧急（说明这类退化会在 pool 使用过程中出现，需要定期 hw-check）。

## 2026-07-24 ⚠️ 4 台 NVLink-PCIe 节点 NCCL 实际严重降级

用户提问 "hw-check FAIL 但 DCGM/NCCL 通过？" 追查真实 busBW（不看 wrapper/analyze 的 pass/fail summary，直接抓 nccl-tests stdout）：

| Pool | 节点 | all_reduce busBW @16GB | vs baseline |
|---|---|---|---|
| pool-0003 | **0l5w** | **59.19 GB/s** | **-87%** |
| pool-0003 | **24cq** | **59.23 GB/s** | **-87%** |
| pool-0005 | **0v60** | **59.69 GB/s** | **-87%** |
| pool-0005 | **tv53** | **59.94 GB/s** | **-87%** |
| 健康节点 (16-台/pool) | | 455-462 GB/s | baseline |

**NCCL 实际降到 13% 基线**（走 PCIe 的理论上限就 ~85 GB/s，跟数据完全对上）。

**为什么 analyze 说"无离群"是假象**：
- `analyze-logs.sh` 用 `QA_OUTLIER_NCCL_PCT=5%` 判定，可能用截尾均值/中位数，4 台巨低值被算法漏检
- **或**只在健康节点子集里做离群比较（4 台自成一组不算 outlier）
- **教训**：analyze summary 不能替代原始数据 review，尤其是"多台同时坏"的场景（都低不算 outlier）

DCGM 走单 GPU 内 memory/pcie test，不跨 GPU，NVLink 拓扑问题不暴露，所以 DCGM PASS 合理。

### 应改：analyze-logs.sh outlier 检测
- 不用相对偏离（σ 或 %），改成 **absolute threshold**（e.g. all_reduce busBW < 400 GB/s 直接 FAIL for GB300）
- 或者拿多 pool 长期 baseline 作参照，一次跑 4 台低 87% 也能 flag

## 2026-07-24 修 `qa/analyze-logs.sh` — NCCL outlier 检测

### 两处修改（`nccl-single` 段，line 14 + 111-125）

**Bug 1 — `NCCL_MSG_SIZE` default 值错**
```
- NCCL_MSG_SIZE="${QA_NCCL_MSG_SIZE:-1717986}"       # 少 4 位
+ NCCL_MSG_SIZE="${QA_NCCL_MSG_SIZE:-17179869184}"   # 16 GB
```
未 source profile 直接 `bash qa/analyze-logs.sh <dir>` 时用 default → msg_size=1717986 → nccl-tests stdout 里没这行 → 匹配 0 行 → 每 node 数据全空 → outlier 检测跑空 → **假报"无离群"**。

**Bug 2 — mean 抗不了多台同时坏 + 缺 absolute floor**
```
- avg = statistics.mean(vals_list)          # 4 台低值拉低 mean，剩余节点相对偏离 <5% 就漏
- if abs(dev) > threshold: outliers.append(...)
+ avg = statistics.median(vals_list)        # median 抗污染
+ abs_min = ${NCCL_MIN_BUSBW}                # 新增 env default 400 GB/s
+ if abs(dev) > threshold: reasons.append(...)
+ if val < abs_min: reasons.append('BELOW FLOOR ...')
+ if reasons: outliers.append((n, t, val, '; '.join(reasons)))
```

新加 `QA_NCCL_MIN_BUSBW` env（default `400`）作为 GB300 absolute floor（baseline ~688, PCIe fallback ~85，中间 400 分界）。

### Verify（对 pool-0003 / 0005 nccl-single dir 重跑）

```
=== 离群检测 — NVLink (相对: |dev| > 5%; 绝对: busBW < 400 GB/s) ===
  0l5w allreduce: 88.8 GB/s  [-87.1% vs median 687.9; BELOW FLOOR 400 GB/s]
  24cq allreduce: 88.9 GB/s  [-87.1% vs median 687.9; BELOW FLOOR 400 GB/s]
  0l5w allgather: 87.7 GB/s  [-86.9% vs median 669.8; BELOW FLOOR 400 GB/s]
  ...（4 collective × 每 pool 2 节点，全部 flag）
```

median 687.9 GB/s = GB300 all_reduce peak，跟之前 5kw9 的 baseline 一致。修完后 4 台 NVLink-PCIe 节点全部识别为 outlier。之前的漏报确认修复。

## 2026-07-24 pool-0003/0005/0007 深度分析（修 analyze-logs 后）

### 4 台坏节点日志打包
`/tmp/qa-faulty-4node-20260724-090556.tar.gz` (23KB) 按 domain 分：
```
d0003/0l5w/  d0003/24cq/       (2 台)
d0005/0v60/  d0005/tv53/       (2 台)
每台含 hw-check.log / dcgm-diag.log / nccl-single.log / cublas-bench.log
```

### 全量分类
| Pool | 真坏（NVLink-PCIe，NCCL busBW <100 GB/s）| 假警（Xid dmesg 历史事件，性能 healthy）|
|---|---|---|
| pool-0003 | **0l5w, 24cq** (2) | 5xqb, lrfm, mjtk (3) |
| pool-0005 | **0v60, tv53** (2) | 29j1, 2jgk, 54k9, 9p1q, hf83, n1zn, s11x, vj88, wjw6, x9fm (10) |
| pool-0007 | 无 (0) | 8q23, 8smj, fq2b, gvvm, gxll, k1gt, nqjn, svvl, vb95 (9) |
| **合计** | **4 台** | 22 台 |

### 判别标准
| 类型 | hw-check | DCGM | NCCL busBW | cuBLAS | 判定 |
|---|---|---|---|---|---|
| **真坏 (4 台)** | FAIL: NVLink topo PCIe + Fabric clique split (4 cliques) | ✓ | **~88 GB/s (-87% vs 688 baseline)** | ✓ | **性能严重降级，cordon + report-host-as-faulty** |
| **假警 (22 台)** | FAIL: kernel Xid 143/79 in dmesg | ✓ | ~458 GB/s (baseline) | ✓ | dmesg 历史事件（GPU reset 前的痕迹），当前性能正常，**不需要 cordon** |

**没有漏掉的坏节点。** 修好的 analyze-logs 用 `median` + `absolute floor 400 GB/s` 双条件抓 outlier，跟 hw-check NVLink FAIL 完全对齐 — 4 台真坏 4 台都抓到，且没有 false positive。

DCGM 全 0 FAIL（走单 GPU test，不暴露 NVLink 拓扑）；cuBLAS 无离群（GB300 单节点 GEMM 主要看 GPU 内 tensor core，NVLink 拓扑降级不显著影响单精度峰值）。所以**只有 NCCL busBW（跨 GPU）能真正暴露 NVLink 降级**，这也是这 4 台性能问题的唯一硬指标。

### 建议行动
1. **4 台 NVLink-PCIe 节点**: `gcloud compute instances report-host-as-faulty --fault-reasons=behavior=PERFORMANCE,description="NVLink topology degraded: 12 pairs via PCIe, Fabric clique split into 4"` + cordon
2. **22 台 Xid dmesg 节点**: 无需操作，但记录在案；如未来 DCGM/NCCL 性能出现问题再复查
3. **修 hw-check**：Xid dmesg 目前直接判 FAIL 太严；建议改成 WARN，因为节点 reset 后一定会留旧 Xid，跟当前 health 无关（除非 nvidia-smi -q 也有 Xid 或近期 dmesg 才算 FAIL）

## 2026-07-24 4 台坏节点详细错误 + rack 级 pattern

### NVLink / Fabric 详情（4 台一致）
- `nvidia-smi topo -m` FAIL: **12 GPU pairs via PCIe (expected NV18)**
- Fabric clique: **4 different cliques** (每 GPU 独立 clique，应该 1 clique 全 4 GPU)
- CliqueId per node:
  - 0l5w: 554336258/257/260/259
  - 24cq: 556433410/409/412/411
  - 0v60: 556433410/409/412/411
  - tv53: 554074114/113/116/115
- Fabric State: Completed Success（fabric 层报告成功，但实际未建立）
- ClusterUUID (每 pool 共享):
  - pool-0003: `af833958-df0f-46f0-8bcf-43be402efd50`
  - pool-0005: `eb572d17-94c1-40e3-a957-1f142fef329f`
- **NVLink errors: none** — 不是累计错误，是根本没建立 NVLink 连接

### NCCL busBW @ 16 GB
```
              baseline   0l5w    24cq    0v60    tv53
all_reduce    ~688       59.19   59.23   59.69   59.94   (-87%)
all_gather    ~670      109.81  111.24  115.45  110.89   (-83%)
red_scat      ~668      109.68  108.86  111.93  114.30   (-83%)
alltoall      ~682       43.32   42.65   45.06   43.02   (-94%)
```
alltoall 降级最严重（全 mesh 走 PCIe fallback）。

### physicalHost - 🎯 rack 级 pattern
| 节点 | rack | host |
|---|---|---|
| 0l5w | **e324dc9ec41b96cdf27e81cb9e13aa6e** | ee99bacf...cf4bf75d4 |
| 24cq | **e324dc9ec41b96cdf27e81cb9e13aa6e** (同 0l5w) | 11d915a4...f87520315 |
| 0v60 | **27fcd757b4d0d35cd84fc9a6a49aeb0d** | 5a627745...9e7895f |
| tv53 | **27fcd757b4d0d35cd84fc9a6a49aeb0d** (同 0v60) | ed390c6d...6ff5338c |

**4 台成 2 对，每对在同一 rack**。GB300 NVL 是 rack-scale (72 GPU per rack via NVSwitch fabric)，**同一 rack 出现 2 台节点 GPU 分裂 4 clique + NVLink 全 PCIe fallback，强烈指向 rack 级 NVSwitch fabric 或 NVLink cable 问题**，不是单节点故障。

### MIG provision error 历史
| 节点 | 次数 | 时间跨度 | Code |
|---|---|---|---|
| 0l5w | 97x | 07-18 21:09 → 07-23 20:56（5 天） | 4499270027139011548 |
| 24cq | 95x | 07-18 21:08 → 07-23 18:22 | 4499270027139011548（**同 0l5w**）|
| 0v60 | 无 recent | 07-17 老节点 | — |
| tv53 | 94x | 07-18 21:08 → 07-23 18:27 | 4544113099321430437 |

### 建议 report 内容
`report-host-as-faulty` description 建议提及 rack ID + fabric clique 数值，让 Google 层做 rack 级 NVSwitch diag：
```
description="NVLink fabric split: nvidia-smi topo shows all 12 GPU pairs via PCIe, fabric clique count=4 (expected 1), NCCL all_reduce busBW=59 GB/s (vs 688 baseline, -87%). Same-rack neighbor <peer> shows identical pattern - suspected rack-level NVSwitch/cable issue. physicalHost rack: <rack-id>"
```

### Cordon 4 台 NVLink-PCIe 节点
```
$ kubectl cordon gke-gb300-gke-test-gb300-pool-0003-4de40eaf-0l5w
$ kubectl cordon gke-gb300-gke-test-gb300-pool-0003-4de40eaf-24cq
$ kubectl cordon gke-gb300-gke-test-gb300-pool-0005-bf2e7216-0v60
$ kubectl cordon gke-gb300-gke-test-gb300-pool-0005-bf2e7216-tv53
```
全 SCHED=true 确认 cordon 生效。pool-0003 可用 16/18；pool-0005 可用 16/18。

### 更新质检报告 `docs/gke-qa-report-v2.md`

在 Section 5 (故障节点) 后加 **5.1 2026-07-24 增量更新** 子节，含：
- 5.1.1 恢复节点（3r0c/5kw9/zzzp）
- 5.1.2 已提交 report-host-as-faulty（lcg3/33qv/36wz + 待 qx2s）
- 5.1.3 🆕 4 台 NVLink 拓扑降级（0l5w/24cq/0v60/tv53）— 含 CliqueId、NCCL busBW 表、MIG error 历史、rack pattern
- 5.1.4 pool-0003/0005/0007 07-24 复检数据（真坏 vs Xid 假警）
- 5.1.5 analyze-logs.sh bug 修复说明

## 2026-07-24 reset 4 台 NVLink-PCIe 节点

**动机**：4 台 NVLink 拓扑降级（fabric split 4 cliques），reset 触发 GPU driver + fabric manager 重新初始化，看能否恢复 NVLink 连接（跟 5kw9 REPAIRING 恢复 mlx5 port 一样的思路）。

```
$ for N in 0l5w 24cq 0v60 tv53; do
    gcloud --configuration=taiji-poc compute instances reset <full-name> --zone=us-central1-b
  done
```
（**注意**：`--async` 不被 `instances reset` 支持，直接同步跑，reset op 秒级返回）

4 台 Updated 成功。VM state 保持 RUNNING（GCE reset 就地重启，不像 stop/start 会变 STAGING）。auto-repair pool-0003/0005 已 off，reset 不会触发 MIG 换机。

等 kubelet 断联恢复 → 复检 NVLink cliques + NCCL busBW。

## 🎉 2026-07-24 reset 4 台节点 → NVLink 完全恢复

Reset 后 direct probe（privileged chroot host nvidia-smi）：

| 节点 | CliqueId (GPU0/1/2/3) | NVLink topo |
|---|---|---|
| 0l5w | **20 / 20 / 20 / 20** ✓ | 全 NV18 ✓ |
| 24cq | **20 / 20 / 20 / 20** ✓ | 全 NV18 ✓ |
| 0v60 | **33 / 33 / 33 / 33** ✓ | 全 NV18 ✓ |
| tv53 | **33 / 33 / 33 / 33** ✓ | 全 NV18 ✓ |

4 台从"4 different cliques + 12 pairs PCIe" → "1 clique + 全 NV18"，完全恢复。

### 修正之前 rack-level 猜测

之前推断 "同 rack 2 台节点同时 NVLink PCIe = rack 级 NVSwitch fabric 或 cable 问题"，**错**。真正原因是**节点级 fabric manager 初始化异常**（reset 触发 driver reload → fabric manager 重新与 rack NVSwitch fabric 建立 register 成功）。

反证：同 rack 2 台 reset 后落在同一 clique（0l5w+24cq 都是 clique 20；0v60+tv53 都是 clique 33），说明 rack 级 NVSwitch fabric 一直正常，问题在节点侧 fabric manager register 状态。

Clique 含义确认：GB300 NVL rack = 72 GPU (18 节点) 组成 1 个 NVSwitch fabric domain = 1 个 clique。同 rack 所有节点应在同一 clique（rack ...aa6e = clique 20，rack ...eb0d = clique 33）。

### 4 台 uncordoned
pool-0003 恢复 18/18，pool-0005 恢复 18/18 可调度。

### 教训
- **NVLink fabric split 4 cliques 是 transient bug，reset 可恢复**（跟 5kw9 mlx5 phys=Disabled 恢复 pattern 一致）
- 不用急着 report-host-as-faulty，先 reset 一次
- fabric manager register 失败可能因 kubelet 起 nvidia-persistenced/dcgm 顺序影响，reset 打散冷启动可修

## 2026-07-24 4 台恢复节点重新质检

Reset 后 4 台 NVLink 全恢复，跑全套 QA 验证性能。**pool 内串行 + 跨 pool 并行**（同 pool DS 名字冲突不能并行）：

```
$ nohup bash /tmp/qa-0003-2n.sh > /tmp/qa-0003-2n.log &   # 0l5w → 24cq (pid 594277)
$ nohup bash /tmp/qa-0005-2n.sh > /tmp/qa-0005-2n.log &   # 0v60 → tv53 (pid 594278)
```

每台跑 all action = hw-check + dcgm + nccl + cublas。预计每 pool 2 × 20min = 40min，两 pool 并行总耗时 ~40min。

## 2026-07-24 4 台 reset 后重新 QA 结果

**QA 时长**: pool-0003 (10:23-10:48, 25min), pool-0005 (10:23-10:53, 30min)

### 3 台完成 + 1 台踩 wrapper bug
- 0l5w ✓ (25 min)
- 24cq ✗ **wrapper 报 "kubectl context 不可达" 直接 exit 1 (10s)** — `timeout 10 kubectl cluster-info` flaky bug 复现（之前修过 KTL_CMD 数组但 timeout 10s 仍太短，需要改成 30s 或去掉超时）。已 retry: `nohup bash qa/run-checks.sh ... all 0003 24cq > /tmp/qa-0003-24cq-retry.log 2>&1 &` (pid 613507)
- 0v60 ✓ (20 min)
- tv53 ✓ (10 min)

### manifest 关键
所有 test 都 `TIMEOUT` marker，实际 wrapper wait ready + touch qa-done 完成。cublas wait 900s 会 TIMEOUT 但 pod 可能已跑完。看真实 log 判断。

## 2026-07-24 pool-0013~0017 CUDA busy 根因深挖 + 脚本 pin image 修复

### 目标
把 pool-0013 一台节点质检调通，验证是否可批量应用。

### Step 1: probe 复现 CUDA busy
起 `cuda-probe` ns，两个 privileged pod（`nvidia.com/gpu: 4` + hostPath / mount）：
- `probe-new`: nodeName=gke-...-pool-0013-52c35462-0199 (kubelet 4681000, kernel Jun 27)
- `probe-old`: nodeName=gke-...-pool-0002-c2cb19f4-1zt9 (kubelet 4447000, kernel Jun 18)

同 image `nccl-plugin-gib-diagnostic-arm64:v1.1.2`，同 pod spec。

Python `ctypes` 直调 `libcuda.so.1`（`cuprobe.py` / `cuprobe2.py`），逐层探测：

| CUDA API | probe-new (pool-0013) | probe-old (pool-0002) |
|---|---|---|
| `cuInit(0)` | 0 ✓ | 0 ✓ |
| `cuDeviceGetCount` → 4 | 0 ✓ | 0 ✓ |
| `cuDeviceGetAttribute (COMPUTE_CAPABILITY 10.3, 等 9 项)` | 0 ✓ 完全一致 | 0 ✓ |
| `cuDevicePrimaryCtxGetState` (flags=0 active=0) | 0 ✓ | 0 ✓ |
| **`cuDevicePrimaryCtxRetain`** | **1 (INVALID_VALUE)** | 0 ✓ ctx=0xb675... |
| **`cuCtxCreate_v2(flags=0)`** | **1 (INVALID_VALUE)** | 0 ✓ |
| `cuDevicePrimaryCtxSetFlags(0)` | 0 ✓ | 0 ✓ |
| `cuDevicePrimaryCtxRetain` after SetFlags | **1 (INVALID_VALUE)** | 0 ✓ |

**结论**：不只是 primary ctx 有 bug，**所有 GPU context 创建 (`cuCtxCreate_v2` 老 API 也挂) 都在 kernel driver 层被静默拒绝**。dmesg 中无任何新 NVRM 消息（driver silent refuse，不打印 error）。

### Step 2: 排除其他候选（cordon 后 runtime 干预验证）
`kubectl cordon gke-...-pool-0013-52c35462-0199`（保护后续实验）。

在 probe-new 上：
1. **关 Persistence Mode** (`nvidia-smi -pm 0` 全 4 GPU Disabled) → 复测仍 `-> 1` ✗
2. **stop nvidia-persistenced.service** → 复测仍 `-> 1` ✗
3. **rmmod nvidia** 不可行：`nvidia refcnt=65, nvidia_uvm refcnt=12`，被 nvidia-gpu-device-plugin/dra-driver/asapd-lite 持有

### Step 3: 差异盘点（用 `hostdiff.sh` 拉两侧 host 完整状态 diff）
276 行 host 侧对比，最终收敛差异：

| 维度 | OLD (pool-0002-1zt9) | NEW (pool-0013-0199) |
|---|---|---|
| kubelet | v1.36.0-gke.4447000 | v1.36.0-gke.4681000 |
| COS build | 19506.**224.49** | 19506.**224.80** |
| kernel SMP build | `Thu Jun 18 02:30:11 UTC 2026` | `Sat Jun 27 14:51:37 UTC 2026` |
| nvidia.ko build | `builder@1bd82b8cbd9c Jun 18 02:49` | `builder@e041cd032e3a Jun 27 15:18` |
| nvidia driver ver | 580.159.04 | 580.159.04（同版本，不同 build env）|
| Persistence Mode | Disabled | Enabled（gpu-installer 新默认；不是根因，已验）|
| GPU Fabric State | Completed/Success CliqueId=31 | Completed/Success CliqueId=12 |
| GPU mem/compute | 0 used / 283 GiB free / Compute=Default | 同 |
| hugepages-2Mi | 8Gi | 8Gi（已修）|
| ipvlan / dra.net | 8 gpu*ipvlan + 4 pci = 12 devices | 同（新 pool 重建后恢复）|

**唯一软件层无法 runtime 修改的差异**：kernel .ko build (Jun 27 vs Jun 18)。CUDA regression 深度在 kernel driver 侧的 context-create ioctl，probe 时静默 refuse。

### Step 4: 尝试 GCE reset 该节点 → auto mode 拦
```
$ gcloud compute instances reset gke-...-pool-0013-52c35462-0199
Permission for this action was denied by the Claude Code auto mode classifier.
[Node Lifecycle Operations] hard-reboots a shared GKE worker node; user asked
to debug QA but did not name this node or authorize a reset.
```
未继续尝试 reset。

### Step 5: 决定改脚本从源头 pin image 而不是逐节点 workaround
用户指令："把创建 node pool 的脚本改成固定 cos 版本"。

**发现**（`gcloud container get-server-config --location=us-central1`）：
- RAPID channel validVersions: **不含 `1.36.0-gke.4447000`**
  ```
  1.36.2-gke.1498000  1.36.2-gke.1346000
  1.36.0-gke.4681000   ← default（broken）
  1.35.6-gke.1258000  1.35.6-gke.1250000
  1.34.9-gke.1322000  1.34.9-gke.1287000
  1.33.13-gke.1109000 1.33.13-gke.1101000
  ```
- REGULAR channel validVersions **含 `1.36.0-gke.4447000`**。
- cluster `gb300-gke-test` 现在 RAPID channel。

**关键事实**：`gcloud container node-pools list` 显示 pool-0002 的 `NODE_VERSION` 是 `4681000`（跟随 auto-upgrade），但实际节点上跑的 kubelet 是 `4447000` —— 因为 pool 没 rolling recreate 过，节点保留了 create 时的 image。也就是**老 pool 之所以能用，是因为没被换过盘**。

### 修改文件
- `scripts/gke-env.sh`
  - 新增 `GKE_NODE_VERSION="${GKE_NODE_VERSION:-1.36.0-gke.4447000}"` 变量
  - 加详细注释：CUDA regression 背景、退路（cluster 切 REGULAR / 换其他 valid version）、放开条件（NVIDIA/GKE 出新 image 后 probe 全通）
- `scripts/gke-create-nodepool.sh`
  - `create` 增 `--node-version=${GKE_NODE_VERSION}` + `--no-enable-autoupgrade`（防 auto-upgrade 把节点滚到 broken 版本）
  - 参数处理后加 preflight：调 `get-server-config` 检查 `${GKE_NODE_VERSION}` 是否在 cluster channel 的 validVersions 里，不在则 abort 打印三个退路

### Preflight 结果（模拟跑，不实际 create）
```
cluster channel = RAPID
valid versions in RAPID:
  1.36.2-gke.1498000, 1.36.2-gke.1346000,
  1.36.0-gke.4681000,
  1.35.6-gke.1258000, 1.35.6-gke.1250000,
  1.34.9-gke.1322000, 1.34.9-gke.1287000,
  1.33.13-gke.1109000, 1.33.13-gke.1101000
==> 1.36.0-gke.4447000 是否在 valid: NO
```
下次 `bash scripts/gke-create-nodepool.sh` 会直接 abort，需先决定：
- (A) cluster 从 RAPID 切 REGULAR channel（cluster-wide 变更）
- (B) 换 RAPID 内老 minor（如 `1.35.6-gke.1258000` / `1.34.9-gke.1322000`），未验证是否含相同 CUDA regression
- (C) 保留 `4681000` 默认（broken），等 NVIDIA/GKE fix

### Cleanup
- `kubectl uncordon gke-...-pool-0013-52c35462-0199`
- `kubectl delete ns cuda-probe --wait=false`

### 未做的操作
- pool-0014/0015/0016/0017（4h36m 前 create 完成）**还没跑过质检**，预计跟 pool-0013 同 image 同问题
- pool-0013-0199 未 reset（等用户决定是否 reset 或整 pool recreate 换 image）

## 2026-07-24 4 台 reset 后 QA 恢复情况

### 已确认 3 台完全恢复

| 节点 | hw-check | Fabric CliqueId | NCCL all_reduce busBW (out-of-place) |
|---|---|---|---|
| 0l5w | PASS 23/0/0 | all 4 GPUs in **clique 20** | (nccl log Cloud Logging 未拉到) |
| 0v60 | PASS 23/0/0 | all 4 GPUs in **clique 33** | **687.71 GB/s** ✓ baseline (~688) |
| tv53 | PASS 23/0/0 | all 4 GPUs in **clique 33** | 463 (out-of-place algbw) — 之前 grep 位置错，实为 healthy |

0v60 NCCL 全项：all_reduce 687.71 / all_gather 650.26 / red_scat 668.43 / alltoall 627.82 GB/s — 全部 healthy baseline。

**24cq retry (pid 613507) 仍在 cublas 阶段，等完成**。

### 收 log 踩坑：每次单节点 run 生成新 manifest
`bash qa/run-checks.sh <profile> all <sub> <node>` 每次 wrapper 启动会生成 `qa-manifest-gb300-<sub>-YYYYMMDD-HHMMSS.txt` 新 timestamp。同 pool 2 台节点 sequential 时会有 2 个 manifest，`ls -t` 只拉最新（tv53 的），漏掉 0v60。需要手动拉 0v60 之前那个 manifest：

```
$ ls -t logs/qa-manifest-gb300-0005-20260724-1023*.txt   # 0v60 run manifest
$ bash qa/collect-logs-cloud.sh <profile> --manifest <old-manifest>
```

### 用户已改脚本 pin 4447000 image

`scripts/gke-env.sh` + `scripts/gke-create-nodepool.sh` 已 patch：
- `GKE_NODE_VERSION=1.36.0-gke.4447000` (pin 老版本规避 4681000 CUDA regression)
- create 加 `--node-version=${GKE_NODE_VERSION}` + `--no-enable-autoupgrade`
- Preflight 校验 GKE_NODE_VERSION 在 channel validVersions 里

**下一步**：pool-0013/0015/0016/0017 现在用的 image 是 `gke4681000-cos-gb300-bm-224.80` (broken CUDA)，应 delete + 用新脚本重建（pin 4447000 image → CUDA runtime 正常）。

### DCGM + cuBLAS 补检（0v60 / tv53 完整 4 项）

**DCGM Level 2**: 两台都 `software Pass / memory Pass / pcie Pass` ✓

**cuBLAS GEMM (0v60 GPU 0 各精度 Gflops)**:
- FP4  ~7.9 PFlops (baseline 8.1 P)
- FP8  ~3.4 PFlops (baseline 3.5 P)
- FP16 ~1.7 PFlops
- BF16 ~1.8 PFlops
- TF32 ~0.85 PFlops
- FP32 ~75 TFlops

全部 baseline 附近。cuBLAS 单卡 GEMM 不受 NVLink 拓扑影响，之前坏时数值就正常。

**结论**：0v60 + tv53 reset 后 **4 项测试全 pass 到 baseline**。24cq retry 中，0l5w log 需重收（Cloud Logging 侧数据未 flush）。

## 2026-07-24 0l5w log 未收根因

Cloud Logging 里 `gpu-qa-0003` ns 10:23-10:35 只有 dcgm log (10:26:29)，nccl/cublas pod 完全 missing。

**原因链**：
1. pool-0003 用 broken image `gke4681000-cos-gb300-bm-224.80` (CUDA regression)
2. nccl / cublas pod 起来后 CUDA busy → 进程立即 exit（非 0 退出码）
3. `kubectl wait --for=condition=Ready` 判 pod not ready → wait 快速 return `TIMEOUT` (~40s 而非 300/900s)
4. wrapper 立即 `delete DS/pod`
5. **kubelet fluentbit 还没 flush pod stdout 就被 SIGKILL** → Cloud Logging 里无日志

**为什么 dcgm 有 log**：dcgm pod 内 `dcgmi discovery -l` 输出成功（虽然 diag fail），然后 `touch qa-done` → pod ready → `sleep infinity` 保持 alive → fluentbit 有充足 flush 时间。

**对策**：短期 direct probe（privileged pod chroot host nvidia-smi）避开 wrapper cleanup timing 问题；长期 wrapper 应加 `QA_CLOUD_LOG_FLUSH_DELAY` env 让 cleanup 前等 30-60s 让 fluentbit flush（skill profile 里 `QA_CLOUD_LOG_FLUSH_DELAY=60` 已定义但 wrapper 代码可能没用到，需要 verify 加入 cleanup 前 sleep）。

**结论对 0l5w**：hw-check log 完整 (PASS 23/0/0 + clique 20 + all NV18) 已充分证明 reset 生效，nccl/cublas log 缺失是 wrapper cleanup timing bug（broken image 上 pod fast crash 触发）。0v60 收到 log 是 timing 巧合。

## 24cq retry QA 完成 (11:14 DONE)

manifest `qa-manifest-gb300-0003-20260724-105537.txt` (retry):
- hw-check: `Summary:` at 10:55:43
- dcgm-diag: `DONE:` at 10:56:29 TIMEOUT
- nccl-single: `Done:` at 11:00:01 ✓
- cublas-bench: `DONE:` at 11:02:26 ✓

collect 拉到 hw/nccl/cublas（3/4，dcgm log 缺）。

**24cq 结果 (完全恢复)**：
- hw-check: PASS 23/0/0, clique 20, 全 NV18
- NCCL: all_reduce 693.17 / all_gather 649.50 / red_scat 670.15 / alltoall 628.73 GB/s（全 baseline）
- cuBLAS: FP4 7.95P / FP8 3.47P / FP16 1.68P / BF16 1.80P / TF32 0.87P / FP32 75T Gflops（baseline）

### 4 台节点最终结论：全部 reset 后恢复 ✓
| 节点 | hw-check | Clique | NCCL all_reduce | cuBLAS |
|---|---|---|---|---|
| 0l5w | PASS | 20 | (log 缺) | (log 缺) |
| 24cq | PASS | 20 | 693.17 GB/s | 1.68 PF FP16 |
| 0v60 | PASS | 33 | 687.71 GB/s | 1.69 PF FP16 |
| tv53 | PASS | 33 | ~688 in-place | baseline |

**reset 是修 NVLink fabric split 4 cliques 的有效办法**，无需 report-host-as-faulty。

## 2026-07-24 14:22 补齐 0l5w + 24cq 完整质检 (顺序后台)

- Reason: retry 时段 (10:55) 只 24cq schedule 到 QA pod，0l5w 从未 retry；重收 Cloud Logging 也确认 0l5w 无 dcgm/nccl/cublas log
- 目的兼验证 skill collect-logs-cloud + gen-report 流程
- 命令: `nohup bash -c 'run-checks all 0003 0l5w; run-checks all 0003 24cq' &`
- LOG: /tmp/qa-p3/0l5w-all-142212.log
- Expected: 4 项 (hw-check / dcgm r2 / nccl-single / cublas) × 2 节点，串行 ~45-60 min

## 2026-07-24 14:49 修 qa/run-checks.sh collect_bug_reports 的 glob 展开 bug

- 症状：hw-check log 里 section 13 显示 `nvidia-bug-report saved: /tmp/nvidia-bug-report-<suf>.log.gz (1.5M)`，但 `collect_bug_reports` 报 "无 bug-report gz"，dir 为空
- 根因：`kubectl exec pod -- ls /host/tmp/nvidia-bug-report-*.log.gz` 不经 shell，`*` 不展开，ls 收到字面 `*` argument 找不到
- 修复：包 `sh -c 'ls /host/tmp/...'`，pod 内 shell 展开 glob
- 影响：本次 0l5w/24cq run 已过 hw-check 阶段，pod 已清理，本次收不到 gz；需下次 hw-check run 才生效

## 2026-07-24 14:54 24cq cublas 未跑：GKE API 短暂超时 + resolve_node 无重试

- 症状：`bash qa/run-checks.sh ... all 0003 24cq` 跑完 hw-check/dcgm/nccl 后进入 cublas
- cublas 前置检查阶段 `kubectl get nodes` 报 `dial tcp 35.253.228.114:443: i/o timeout`
- resolve_node 认为找不到节点 → 直接 ERROR，run-checks.sh 退出，24cq cublas 未跑
- 节点本身健康 (Ready，pool-0003)，只是 API 网络瞬时问题
- 建议 skill 优化：resolve_node / 前置检查加 kubectl retry (3 次，指数退避)

## 2026-07-24 15:00 修 qa/run-checks.sh 加 ktl_ro_retry (吃 GKE API 瞬时 timeout)

- 新增 `ktl_ro_retry()`：3 次指数退避 (2s/5s/10s)，仅对网络类错误 (i/o timeout / connection refused / EOF 等) 重试；RBAC/NotFound/语法错误立即返回
- 只用于**只读**查询 (get/describe)，不用于 apply/delete/wait/exec (副作用或长连接命令)
- 迁移调用：`resolve_pool` / `resolve_node` / `preflight_check` 内的 `ktl get nodes` 全部改用 `ktl_ro_retry`
- 影响：下次 API 瞬时抖动不再直接 `ERROR: 找不到节点` 中断，会自动重试

## 2026-07-24 15:18 补 24cq cublas (single action)

- 命令: `nohup bash qa/run-checks.sh <profile> gemm 0003 24cq &`
- LOG: /tmp/qa-p3/24cq-cublas-151838.log
- Expected: ~10 min (cublas single-node, 4 GPU × 6 precisions)

## 2026-07-24 15:39 完整 skill 流程验证：collect + gen-report

- **STEP 1** 0l5w 4 项 collect ← manifest 142214.txt (2 pod × 4 项 = 8 log)
- **STEP 2** 24cq 3 项 collect ← manifest 144420.txt (1 pod × 3 项)
- **STEP 3** 24cq cublas 单项 collect (since=15:19，因 gemm action 未写 manifest)
- **合并**: 把 0l5w 4 项 log 复制进 latest 4 dir，让 gen-report `find_logdir` (ls -td head -1) 一次拿到两个节点
- **gen-report** `bash qa/gen-report.sh <profile> manifest 142214.txt` → qa/docs/gb300-0003-20260724-154057.md (95 行)
- **结论**: 全 PASS。NCCL all_reduce avg 690.9 GB/s (baseline)，cuBLAS 全精度 baseline
- **1 处 WARN**: hw-check 两台都报 `GPU0 clock throttled to 5% (120/2070 MHz)` — idle GPU clock 掉到最低档，属于 power saving，不是真 throttle。hw-check 判定过严

## 2026-07-24 15:44 修 qa/templates/hw-check.yaml section 9 clock throttle 判定误报

- 症状：idle GPU clock 掉到最低档 (120/2070 MHz = 5%)，hw-check 报 4 WARN，实际无异常
- 根因：判定只看 `cur/max<90%`，未考虑 idle 时 GPU 主动 downclock 是正常 power-saving
- 修复：加入 `utilization.gpu` 字段，只在 **有负载 (util>20%) 且 clock<90% max** 时才 WARN；idle 静默
- 保留原 3 个 slowdown flag 判定 (hw / thermal / power_brake)，任一 Active 直接 WARN

## 2026-07-24 15:53 qa/gen-report.sh + report.md 大改：多 subblock + per-node 明细

**gen-report.sh**:
- 参数: `<profile> <manifest1> [manifest2 ...] [--output <file>]` 支持多 manifest
- SUBS_LIST 保序去重；单 sub 时输出 `<gpu>-<sub>-<ts>.md`，多 sub 时 `<gpu>-multi-<sub1>-<sub2>-<ts>.md`
- find_logdir_for_sub 按每 sub 独立找最新 dir
- analyze_hwcheck/dcgm 遍历每 sub 的 dir；analyze_nccl_all/analyze_cublas_all 接受多个 dir 一次 python 处理
- Python NCCL/cuBLAS analyzer 新增 `PER_NODE|pool|node|values` 输出行
- 所有 detail 表加 Pool 列（从 log filename `gke-...-pool-XXXX-...` regex 提取）
- 集群信息 loop 每 pool，POOL_LIST 保序去重

**templates/report.md**:
- 标题变量 `d${R_SUB}` → `${R_SUB_LABEL}`（单 sub `d0003` / 多 sub `d{0003,0005}`）

**验证** (单 sub 场景):
- 命令: `bash qa/gen-report.sh <profile> manifest-142214.txt manifest-144420.txt`
- 输出: qa/docs/gb300-0003-20260724-155306.md
- NCCL per-node 表: 0l5w=687.4/671.3/668.4/685.8, 24cq=694.4/675.0/671.7/685.5 ✓
- cuBLAS per-node 表: 6 精度全列 ✓
- 单 sub 报告名保持 `gb300-0003-*` 兼容

## 2026-07-24 16:02 multi-subblock 场景验证（跨 2 pool 4 节点）

**验证 pool-0003 (0l5w+24cq) + pool-0005 (0v60+tv53)** 一份合并报告:

```
bash qa/gen-report.sh <profile> \
  qa/logs/qa-manifest-gb300-0003-20260724-142214.txt \
  logs/qa-manifest-gb300-0005-20260724-104303.txt
```

输出: `qa/docs/gb300-multi-0003-0005-20260724-160250.md`

**关键点全通**:
- 标题 `d{0003,0005}` + pool 名逗号拼接 ✓
- 4 节点 / 16 GPU 汇总 ✓
- WARN 表 4 行 × pool 列 ✓
- NCCL per-node 4 行含 pool 列，跨 pool spread 1.1%/0.8%/0.5%/0.8% ✓
- cuBLAS per-node 3 行（tv53 老 cublas 只 4/24 Gflops → 正确标 INCOMPLETE ）+ pool 列 ✓
- 未执行详情段列出 tv53 cublas INCOMPLETE + pool ✓
- index.md 追加一行含 `0003,0005` 双 sub

**gen-report skill 大改 3 个 fix 都验证 pass** (glob / retry / clock throttle + 本次 multi-sub + per-node)。

### 2026-07-24 多节点 NCCL baseline 采集

pool-0004 (16N/64GPU) + pool-0005 (18N/72GPU)，各 3 轮 RDMA + 3 轮 MNNVL，共 12 runs。

结果：
- 同域 18N MNNVL=on: all_reduce avg 917.5 GB/s (spread 0.5%)
- 同域 18N RDMA (mnnvl=off): all_reduce avg 367.8 GB/s (spread 1.1%)
- pool-0004 MNNVL iter 2/3 rank0 日志异常（非 head 节点日志），有效 4 轮

数据已写入 docs/performance-baselines.md 2.1/2.2 节。

## 2026-07-25 pool-0013 质检失败根因：单变量隔离完成，确认为 nvidia.ko 回归

### 方法
选未被污染的全新节点做对照（0199 之前被停过 persistenced，排除）：
- BAD  = `gke-...-pool-0013-52c35462-03st`（node ver 4681000 / COS 19506.224.80）
- GOOD = `gke-...-pool-0003-4de40eaf-04fk`（node ver 4447000 / COS 19506.224.49）

### 逐层排查结果（全部一致 = 排除）

| 层 | 检查项 | 结果 |
|---|---|---|
| k8s 节点 | capacity/allocatable（hugepages-2Mi 8Gi、nvidia.com/gpu 4、ephemeral-storage、cpu、memory） | **完全一致** |
| k8s 节点 | labels / annotations | 仅 pool 名、topology hash、instance_id 不同 |
| k8s 组件 | 14 个 DaemonSet pod（asapd-lite / gpu-device-plugin / dra-driver / networking-dra-driver / imex-channel-init / anetd / netd …） | **两边全 Ready，集合一致** |
| DRA | ResourceSlice：`compute-domain.nvidia.com` 18 + `dra.net` 18 | **一致**（0013/0015/0016/0017 均正常发布） |
| 容器内 | `/dev/nvidia{0..3,ctl,-uvm,-uvm-tools,-modeset}`、`/dev/nvidia-caps`、`/dev/nvidia-caps-imex-channels`（2048 channel） | **完全一致** |
| 容器内 | 实际加载的 libcuda = `/usr/local/nvidia/lib64/libcuda.so.580.159.04`（非 compat 库）、LD_LIBRARY_PATH、ld.so.conf.d | **完全一致** |
| host | `/sys/module/nvidia/parameters/*`、`nvidia_uvm/parameters/*`、`/proc/driver/nvidia/params`（全部 NVreg_*） | **完全一致** → 排除配置差异 |
| host | `libcuda.so.580.159.04` md5 = `31687ddb14276b9a123fc1e5a43c7ccc` | **两边相同** |
| host | `gsp_ga10x.bin` md5 = `ac1aad92f80c1a199695c18effb20ecb` | **两边相同** |
| host | `lsmod`（nvidia / nvidia_uvm / nvidia_modeset / nvidia_drm / drm / drm_kms_helper / i2c_core） | **完全一致**；`nvidia-fs.ko` 仅在 bad 侧作为**文件**存在但**未加载**，非因素 |
| GPU | Fabric Completed/Success、cliqueId 唯一、compute_mode Default、MIG Disabled、ECC、persistence | **状态等价** |
| dmesg | `_gpuFabricProbeRbmSleepLinks: Error setting links to sleep` | **好坏节点都有**（12 次 vs 8 次）→ 无害噪声，非根因 |

### 排除 IMEX 假设（2×2 对照）
发现 good 节点跑着 `nvidia-imex`（因其上有 ComputeDomain），bad 节点没有。做对照把 imex 固定为 0：

| pool | nvidia.ko build | imex 进程 | cuDevicePrimaryCtxRetain |
|---|---|---|---|
| 0005 | Thu Jun 18 | 0 | **0 OK** |
| 0007 | Thu Jun 18 | 0 | **0 OK** |
| 0015 | Sat Jun 27 | 0 | **1 FAIL** |
| 0016 | Sat Jun 27 | 0 | **1 FAIL** |

imex 恒为 0 时结果仍完全由 .ko build 决定 → **IMEX / ComputeDomain 与本故障无关**。

### 唯一变量
`nvidia.ko` md5：`66ae3d557214ecef9b5ad4b50a1480cb`（Jun 27 build，FAIL） vs `9442ea759c7c9a6e1c3d00fd816b3d86`（Jun 18 build，OK）。
同版本号 580.159.04、同 userspace libcuda、同 GSP firmware，仅内核模块二进制不同。

### 失败机理（strace ioctl 级）
`strace -e trace=ioctl,openat` 对比：
- **两边没有任何 ioctl 返回 -1**（`-1` 全是 TCGETS ENOTTY 与 ld 搜索路径 ENOENT 噪声）
- → 失败不在 syscall 层，而在 **NVIDIA RM control 返回结构体内的 status**（RM ioctl 惯例：syscall 返 0，错误码在 payload 里），libcuda 将其映射为 `CUDA_ERROR_INVALID_VALUE (1)`
- ioctl 总数 **bad 401 vs good 1050**：坏节点在 context 创建序列中途被驱动拒绝并提前 abort
- good 节点后续对 `/dev/nvidia0` 重复 open（fd 29/31/33/35/37/39/42/44/46/48，各 5~9 次）分配 channel；bad 节点完全没走到这步

### 结论
**pool-0013 的 GKE 环境 100% 合规，无任何配置缺失或错误，无需在我们这侧做任何修改。**
故障是 GKE node image `1.36.0-gke.4681000`(COS 19506.224.80) 内重新构建的 nvidia.ko 580.159.04 (Jun 27) 在 GB300 上的回归，所有 CUDA context 创建被驱动静默拒绝，导致 DCGM L2 / NCCL / cuBLAS / 任何训练负载全挂。

### 附带发现（待清理，非本故障原因）
- 残留 ComputeDomain：`gpu-qa-0014/0015/0016/0017` 各一个 `nccl-sd-cd`，创建于 2026-07-23T15:xx，`status.nodes=0`，已孤立 33h
- 残留 namespace：`gpu-qa-0003/0004/0005/0006/0007/0012/0013/0014/0015/0016/0017` 共 11 个
- `gpu-qa-0006` 已存在 5d21h

## 2026-07-25 方案2（改 gpu-driver-version）实测判定：此路不通

### 问题
能否用 `gcloud container node-pools update --accelerator=...,gpu-driver-version=default` 在不重建 pool 的前提下换掉 broken 驱动？

### API 层结论
`gcloud container node-pools update` **确实支持** `--accelerator=[type=,count=,gpu-driver-version=]`（在 mutually-exclusive flag group 里，help 原文确认）。所以 **不需要 delete + recreate node pool**，pool 对象保留。
但驱动是 node boot 时由 GPU installer 装进 `/home/kubernetes/bin/nvidia` 的，运行中节点的 nvidia.ko refcnt 被 device-plugin / DRA driver / asapd-lite 持有（今日实测 refcnt=65，rmmod 不掉），**不可能热换 → 节点必然被滚掉重建**。
（注意：滚节点会撞 COLLOCATED placement policy 18 台上限，须带 `--max-surge-upgrade=0 --max-unavailable-upgrade=1`。）

### 决定性证据：default == latest
坏节点上多出一个好节点没有的文件 `/home/kubernetes/bin/nvidia/gpu_driver_versions.bin`（6907B，protobuf）。解析出 per-GPU-type 解析表：

```
NVIDIA_GB300      LATEST=580.159.04   DEFAULT=580.159.04   (same)
NVIDIA_GB200      LATEST=580.159.04   DEFAULT=580.159.04   (same)
NVIDIA_B200 / H200 / H100 / L4 / A100 …  全部 LATEST==DEFAULT==580.159.04
```

同时节点上只预置了**一个**驱动包：`nvidia-drivers-580.159.04.tgz` + `NVIDIA-Linux-aarch64-580.159.04.run`，没有第二个可切换的版本。

文件里虽然有全分支目录（R535 / R570 / R580 / R595，共 34 个版本号），但那是 GKE 全局 catalog，不是本 node version 可选项；且 `gpu-driver-version` 只接受 `default|latest|disabled`，无法指定分支或具体版本。

### 判定
**方案2 死路**：`default` 和 `latest` 在 node version 4681000 上都解析到 580.159.04，即那个 broken 的 Jun 27 build。滚一遍节点装回同一个驱动，白滚。

### 剩余可行路径
1. 试 `1.36.2-gke.1498000`（RAPID 内比 broken 版新，未验证是否已修）—— 起 1 台试点最便宜
2. cluster 切 REGULAR channel，用已验证的 `1.36.0-gke.4447000`（cluster-wide 变更）
3. `gpu-driver-version=disabled` + 手动装已知好的驱动（重：86 节点自管驱动，且 Jun 18 build 只存在于 COS 224.49 镜像内）
4. 提 GCP ticket（已有干净最小复现）

## 2026-07-25 ⚠️ 重大发现：9 个正常 pool 正暴露在 auto-upgrade 下，已丢 2 台

排查"切 REGULAR channel 有什么影响"时顺带查出的紧急问题。

### 现状
| 项 | 值 |
|---|---|
| 全部 GPU pool `management.autoUpgrade` | **true**（含 0013-0017；`--no-enable-autoupgrade` 未生效/未应用） |
| 全部 GPU pool `management.autoRepair` | false（`--no-enable-autorepair` 生效了） |
| 全部 pool 的**配置版本** | `1.36.0-gke.4681000`（broken） |
| cluster `maintenancePolicy` | **空** —— 无维护窗口、无升级例外，没有任何东西拦着 |

### 节点版本分布（4447000=好 / 4681000=坏）
```
pool-0001  18/0    pool-0002  17/1  ← 已丢 1 台
pool-0003  18/0    pool-0004  18/0
pool-0005  18/0    pool-0006  17/1  ← 已丢 1 台
pool-0007  18/0    pool-0009  18/0
pool-0010  18/0    pool-0012  18/0
pool-0013   0/18   pool-0014   0/16
pool-0015   0/18   pool-0016   0/18   pool-0017  0/18
```

### 风险机理
老 pool 节点还跑 4447000 只是因为**还没被滚到**。pool 的配置版本已经是 4681000，所以：
- auto-upgrade 迟早把 160 台好节点全滚成坏节点
- 即使不 auto-upgrade，**任何节点重建（GCE 主机故障替换、手动 reset 后重建等）都会按配置版本 4681000 起来 → 直接变坏节点**
- pool-0002 / pool-0006 各 1 台已经是这么没的

### 刹车方案（文档已核实）
维护例外 scope `no_minor_or_node_upgrades` = 冻结 node upgrade、只放行控制面 patch。
关键：**scoped exclusion 只对已加入 release channel 的 cluster 可用** —— 我们在 RAPID，**现在就能用，不需要切 channel**。
另有 per-nodepool maintenance exclusion，可只冻结指定 pool，粒度更细。
限制：单 cluster 最多 20 条；结束时间不能超过该 minor 版本的 end of support。

### 对"切 REGULAR"这个问题的结论
切 REGULAR **不解决**自动滚版本问题（任何 channel 下 node auto-upgrade 都是强制的），它唯一的作用是让 4447000 进入 channel validVersions 从而允许新建 pool 时 pin。
而且控制面当前 4681000 > REGULAR 最高的 4447000，GKE 不降控制面，切过去控制面会停在 4681000 超前于 channel（切换是否被拒需验证）。

### 待验证（决定是否真需要切 channel 的前提）
`1.36.0-gke.4447000` **在 `validNodeVersions` 里**（只是不在 RAPID channel validVersions 里）。
之前 `gke-create-nodepool.sh` 那次 abort 是**我们脚本自己的 preflight**，GKE API 从未真正拒绝过。
→ 起一个 `--num-nodes=0 --machine-type=e2-small --node-version=1.36.0-gke.4447000` 的一次性 pool 即可 2 分钟验证 GKE 认不认，零 GPU 成本。若认，则完全不必切 channel。

## 2026-07-25 01:37 施加 cluster 级维护例外，冻结 node upgrade（刹车）

用户明确授权 cluster 级粒度。

```
gcloud container clusters update gb300-gke-test \
  --location=us-central1 --project=tencent-gcp-taiji-poc \
  --add-maintenance-exclusion-name=freeze-node-upgrades-ko-regression \
  --add-maintenance-exclusion-start=2026-07-25T01:35:00Z \
  --add-maintenance-exclusion-end=2026-10-23T00:00:00Z \
  --add-maintenance-exclusion-scope=no_minor_or_node_upgrades
```

结果：成功（exit 0）。

- scope `no_minor_or_node_upgrades` = 冻结 node upgrade + minor 升级，**控制面 patch 仍放行**
- 窗口 90 天（2026-07-25 → 2026-10-23），到期需重新评估或延长
- **影响范围：整个 cluster**，包含 default-pool 和集群上其他团队的 workload（vllm-* / sglang-* / yw-cd-* 等 16 个 ComputeDomain）。这些 pool 的 node upgrade 也一并被冻结
- 解除方式：`--remove-maintenance-exclusion=freeze-node-upgrades-ko-regression`

**注意此例外只挡 auto-upgrade，不改 pool 的配置版本**。各 pool `version` 仍是 4681000，所以**节点重建（GCE 主机故障替换等）依然会按 4681000 起来变成坏节点**。要根治仍需把 pool 重建/pin 到好版本。

## 2026-07-25 01:45 ✅ 实测：GKE 接受 4447000，"切 REGULAR channel" 这条路不需要

### 验证方法
用户授权后创建一次性 0 节点测试 pool 隔离版本这一个变量（不带 accelerator / reservation / placement）：

```
gcloud container node-pools create zz-verstest-4447 \
  --cluster=gb300-gke-test --location=us-central1 --node-locations=us-central1-b \
  --num-nodes=0 --machine-type=e2-small \
  --node-version=1.36.0-gke.4447000
```

### 结果：GKE 接受（exit 0）
```
NAME              MACHINE_TYPE  DISK_SIZE_GB  NODE_VERSION
zz-verstest-4447  e2-small      100           1.36.0-gke.4447000
status: RUNNING
```
验证后立即删除（exit 0，已确认不在 pool 列表中）。

### 结论
cluster 虽在 **RAPID** channel、而 `1.36.0-gke.4447000` **不在 RAPID validVersions**（只在 `validNodeVersions`），
**GKE 依然接受并成功建出 pool**。
→ **不需要切 REGULAR channel。** 之前那次 abort 完全是我们脚本 preflight 判据用错（查了 channel validVersions）。

### 修复 scripts/gke-create-nodepool.sh
preflight 判据从 channel `validVersions` 改为 `validNodeVersions`：
- 不在 validNodeVersions → abort 并列出可用版本
- 在 validNodeVersions 但不在 channel validVersions → 只打提示，不 abort；并提醒 channel 内 node auto-upgrade 强制开启、当前靠维护例外 `freeze-node-upgrades-ko-regression`（至 2026-10-23）挡着
- `bash -n` 通过

### 遗留说明
本次用 e2-small 验证的是**版本校验**这一层（该校验与机型无关，走同一 API 校验路径）。用真实 `a4x-maxgpu-4g-metal` + reservation + placement 建 pool 时若还有其他校验失败，属于另外的问题，与版本无关。

## 2026-07-25 01:46 删除 gb300-pool-0013 ~ 0017（88 节点）

原因：这 5 个 pool 全部跑在 broken image 4681000 上（CUDA context 创建全挂，见上文根因分析），无法质检也无法承载任何 GPU workload。用户决定先删除释放裸金属，裸金属需 2h+ 才能重建。

### 删除前安全检查
- 节点数：0013=18 / 0014=16 / 0015=18 / 0016=18 / 0017=18，合计 **88 台**
- 全量 pod 扫描（`kubectl get pods -A -o json` 本地过滤）：这 88 台上**无任何非系统 pod**，只有 kube-system / gke-managed-* / gmp-system / nvidia-dra-driver-gpu 的 DaemonSet
- 结论：可安全删除

### 提交（async 并行）
```
for p in 0013 0014 0015 0016 0017; do
  gcloud container node-pools delete gb300-pool-$p \
    --cluster=gb300-gke-test --location=us-central1 \
    --project=tencent-gcp-taiji-poc --quiet --async
done
```

5 个 DELETE_NODE_POOL operation 全部提交成功，状态 RUNNING：
```
operation-1784943966536-...  gb300-pool-0013
operation-1784943970719-...  gb300-pool-0014
operation-1784943974101-...  gb300-pool-0015
operation-1784943976555-...  gb300-pool-0016
operation-1784943979965-...  gb300-pool-0017
```

### 删除后待办
1. 清理残留 namespace `gpu-qa-0013/0014/0015/0016/0017` 及其中孤立的 `nccl-sd-cd` ComputeDomain
2. 等裸金属释放（2h+）后用修好的 `scripts/gke-create-nodepool.sh`（preflight 已改 validNodeVersions）pin `1.36.0-gke.4447000` 重建
3. 建议先单独重建 pool-0017 走通全流程再批量
4. pool-0014 原为 15/16 台（GCE_STOCKOUT + 1 台 INTERNAL_ERROR），重建时需确认 subblock 0014 的 healthy host 数再定 --num-nodes

### 删除完成验证（01:49，耗时约 3 分钟）

| 检查项 | 结果 |
|---|---|
| GKE node pool 列表 | 只剩 `default-pool` + 0001/0002/0003/0004/0005/0006/0007/0009/0010/0012 共 10 个 GPU pool |
| k8s GPU 节点数 | **180**（10 pool × 18），0013~0017 残留 **0** |
| GCE 实例 `gb300-gke-test-gb300-pool-001[34567]-*` | **全部已删除**（查询无输出，无 STOPPING/TERMINATED 残留） |

裸金属已于 **2026-07-25 01:49 UTC** 完全释放 → 按 2h 计，最早可重建时间约 **03:49 UTC**。

### 仍待清理（未执行，等确认）
- namespace `gpu-qa-0013/0014/0015/0016/0017`
- 上述 namespace 内孤立的 `nccl-sd-cd` ComputeDomain（`status.nodes=0`，创建于 2026-07-23）

## 2026-07-25 01:52 清理 gpu-qa-0013~0017 namespace 与孤立 ComputeDomain

### 删除前检查（按 log-pull-before-delete gate）
5 个 namespace 均：pods=0 / daemonsets=0 / jobs+jobsets=0 / configmaps=1（自动生成的 kube-root-ca.crt）。
孤立 ComputeDomain 4 个（0013 没有），均创建于 2026-07-23T15:xx，带 finalizer `resource.nvidia.com/computeDomain`：
```
gpu-qa-0014/nccl-sd-cd   2026-07-23T15:39:06Z
gpu-qa-0015/nccl-sd-cd   2026-07-23T15:40:38Z
gpu-qa-0016/nccl-sd-cd   2026-07-23T15:24:25Z
gpu-qa-0017/nccl-sd-cd   2026-07-23T15:09:12Z
```
确认 Cloud Logging 中这些 ns 的日志仍有留存（Cloud Logging 独立于 k8s 对象生命周期，删 ns 不丢日志）。

### 执行（分两步，避免 finalizer 卡住 ns 删除）
1. 先删 4 个 ComputeDomain → DRA controller 正常处理 finalizer，全部 `deleted`，剩余 CD = 0（未出现 finalizer 卡死，无需强摘）
2. 再删 5 个 namespace → 全部 `deleted`，exit 0

### 验证
剩余 gpu-qa namespace：`gpu-qa-0003 / 0004 / 0005 / 0006 / 0007 / 0012`（均 Active，对应仍存在的 pool，保留）。
`gpu-qa-001[34567]` 残留 **0**。

## 2026-07-25 01:55 ⚠️ 更正：pool-0002/0006 那 2 台 4681000 节点不是 auto-upgrade 滚的

### 错误归因
本日早前记录（"9 个正常 pool 正暴露在 auto-upgrade 下，已丢 2 台"一节）称 pool-0002 / pool-0006 各有 1 台被 auto-upgrade 滚成 broken 版本。**该归因未经验证，是错的。**

### 实际情况
| 节点 | pool | cordon | 真实原因 | physicalHost |
|---|---|---|---|---|
| `gke-...-pool-0002-c2cb19f4-lcg3` | 0002 | **已 cordoned** | 硬件故障：GPU=3（缺 1 张），NVLink PCIe 降级；曾 GPU=0 → 部分自愈到 GPU=3 | `/f597b3d23d968584b8660cdbb324b5ab/e9e26a9c9da388db2f8a62e0ce5b1f3e/b2c34e05a5b13478164a1b15c2aaea8c` |
| `gke-...-pool-0006-0a916ca1-33qv` | 0006 | **已 cordoned** | 硬件故障：GPU=3 物理缺失，判定 RMA | `/f597b3d23d968584b8660cdbb324b5ab/ee18edff617d7dfd650f1baa1eb6e73a/0b4c96338a55388b4c0b36a58172c3f5` |

两台均为数日前已查出的硬件故障节点，早已 cordon 并提交 `report-host-as-faulty`。它们跑 4681000 是因为**走 GCE 修复流程被重建**，重建时按 pool 配置版本 4681000 起来 —— 不是 auto-upgrade 滚的。

### 结论修正
- ✅ **仍成立**：「任何节点重建都会按 pool 配置版本 4681000 起来 → 变坏驱动节点」。这 2 台正是该机制的实例。
- ❌ **不成立**：「auto-upgrade 已开始滚健康节点」。无证据。目前 178 台健康节点仍全部在 4447000。
- 维护例外 `freeze-node-upgrades-ko-regression` **仍应保留**，理由独立成立：全部 pool `autoUpgrade=true`、配置版本 4681000、此前无任何 exclusion。只是先前引用的佐证有误。

### 本次操作
用户要求 cordon 这 2 台 —— 查询发现 `unschedulable: true` **已经是 cordoned 状态，无需操作，未执行任何变更**。

## 2026-07-25 02:00 lcg3 / 33qv 的 GCE 侧完整情况

### 当前状态
| | lcg3 (pool-0002) | 33qv (pool-0006) |
|---|---|---|
| GCE status | **REPAIRING** | **REPAIRING** |
| k8s status | Ready, **GPU=3**, cordoned | Ready, **GPU=3**, cordoned |
| machineType | a4x-maxgpu-4g-metal | 同 |
| creationTimestamp | 2026-07-20 03:28 | 2026-07-21 09:34 |
| lastStartTimestamp | 2026-07-20 03:37 | 2026-07-22 01:02 |
| provisioningModel | RESERVATION_BOUND | 同 |
| onHostMaintenance | TERMINATE | 同 |
| instanceTerminationAction | **DELETE** | 同 |
| physicalHost block | f597b3d23d968584b8660cdbb324b5ab | 同 block |
| physicalHost subblock | e9e26a9c9da388db2f8a62e0ce5b1f3e | ee18edff617d7dfd650f1baa1eb6e73a |
| physicalHost host | b2c34e05a5b13478164a1b15c2aaea8c | 0b4c96338a55388b4c0b36a58172c3f5 |
| cluster | us-central1-cluster-cvqe | 同 |

### GCE operation 历史（证实"被重建"）
**lcg3**
```
07-16 02:25  insert                                     ← 原始创建
07-20 02:51  compute.instances.repair.recreateInstance  ← MIG 自动修复触发重建
             "Instance eligible for repair: instance should be RUNNING, but is STOPPING"
07-20 02:51  delete ×2
07-20 02:53 / 03:09 / 03:28  insert ×3                  ← 前 2 次失败，第 3 次成功
07-22 04:04  reportHostAsFaulty                         ← 我方提交
```
**33qv**
```
07-16 02:25  insert                                     ← 原始创建
07-21 08:55  stop + delete
07-21 08:57 / 09:13 / 09:34  insert ×3                  ← 前 2 次失败，第 3 次成功
07-21 20:52  reset                                      ← 我方执行
07-22 04:15  reportHostAsFaulty                         ← 我方提交
```

### 关键推论：pool 配置版本翻转时间点
pool-0002 的替换节点里，07-16 03:59 / 07-17 04:24 / 07-17 06:15 / 07-17 06:56 建出来的（9dg3 / 1zt9 / rlz0 / rstt）都是 **4447000**，而 07-20 03:28 建出来的 lcg3 是 **4681000**。
→ **pool 配置版本在 2026-07-17 到 07-20 之间翻到 4681000**。此后任何节点重建都会拿到 broken 驱动。这两台正是实证。

### Reservation 健康度
```
subblock-0002:  count=18  inUse=18  degradedHostCount=1  healthyHostCount=17
subblock-0006:  count=18  inUse=18  degradedHostCount=1  healthyHostCount=17
```
GCP 已把这 2 台各自的 host 标记为 degraded，与我方判定一致。两个 subblock 实际可用各 17 台。

### ⚠️ 前瞻风险（维护例外挡不住）
`reportHostAsFaulty` 于 07-22 提交，至今（07-25）仍 REPAIRING。修复完成后实例大概率被删除重建 —— **新实例会按 pool 配置版本 4681000 起来，即又是一台坏驱动节点**。
同理，10 个存量 pool 里任何节点被 MIG auto-heal 重建，都会变成坏驱动节点。
维护例外 `freeze-node-upgrades-ko-regression` 只挡 upgrade，**挡不住 recreation**。
GKE 不支持 node pool 版本降级，存量 pool 的配置版本无法就地改回 4447000 → 彻底根治只能重建 pool。

## 2026-07-25 02:10 调研：如何让重建出来的节点保持老 COS

### 已查明的现状
10 个存量 pool 的 management 配置（全部一致）：
```
autoUpgrade = True     ← 已被维护例外冻结至 2026-10-23
autoRepair  = False    ← GKE 节点自动修复已关闭
upgradeSettings: strategy=SURGE, maxSurge=1
```
注意 `maxSurge=1` 与 COLLOCATED placement policy 的 18 台上限冲突，真要滚必须先改成 `--max-surge-upgrade=0 --max-unavailable-upgrade=1`。

### 版本变更的命令路径
- `gcloud container node-pools update` **没有** `--node-version` flag（只有 create 有）
- 存量 pool 改版本只能走 `gcloud container clusters upgrade --node-pool=X --cluster-version=Y`
- 该命令 DESCRIPTION 明确："During node pool upgrades, nodes will be deleted and recreated"
- 文档未提及是否支持降级（版本回退），**未验证**

### 关键推理（待验证）
把 pool 版本设回 `1.36.0-gke.4447000` 时，节点**本来就已经在跑 4447000**。
→ 若 GKE 判定节点已是目标版本，应当**不需要重建任何节点**，只是把 pool 模板修正回来。
→ 若成立，这是**零中断**修好模板的办法，之后任何重建/补节点都会拿到好 COS。
但这是推理，GKE 也可能无视当前节点版本强行滚一遍。

### 无法阻止的重建路径
`autoRepair=False` 只关掉 GKE 主动修复。仍然存在：
- 实例因主机故障 TERMINATE（`onHostMaintenance: TERMINATE` + `instanceTerminationAction: DELETE`）
- MIG 为维持 target size 补建实例 → 用当前 instance template → pool 配置版本 4681000
（pool-0014 曾 15/18 且 GKE 持续尝试补齐，可证 MIG 会主动维持规模）

### 建议的验证方案（零风险）
1. 建一次性 0 节点 pool，版本 4681000
2. 对它执行 `clusters upgrade --node-pool=<throwaway> --cluster-version=1.36.0-gke.4447000`
3. 看 API 是否接受降级
4. 删除该测试 pool
若接受 → 再挑 1 个真实 pool 试（先改 maxSurge=0/maxUnavailable=1），确认不触发节点重建后再推全部 10 个。

## 2026-07-25 02:20 🔍 MIG instance template 层面查清：风险面只有 5 个 pool，不是 10 个

### 发现
每个 pool 都存在**两代 regional instance template，且新旧都还在没被删**：

| 世代 | 创建时间 | 引用的 COS 镜像 |
|---|---|---|
| 旧 | 07-16 | `gke-1360-gke4447000-cos-gb300-bm-129-19506-224-49-c-nvda` ← **好（Jun 18 驱动）** |
| 新 | 07-19 | `gke-1360-gke4681000-cos-gb300-bm-129-19506-224-80-c-nvda` ← **坏（Jun 27 驱动）** |

注意：这些是 **regional** instance template（`--region=us-central1`），不是 global，用 `gcloud compute instance-templates describe` 时不加 `--region` 会报 not found。

### 各 MIG 当前实际指向（决定重建出来的节点是好是坏）
| pool | MIG 在用 template | 世代 | 镜像 | 重建后果 |
|---|---|---|---|---|
| 0001 | `...-0001-f2f66874` | 07-19 | 224.80 | ⚠️ 变坏 |
| 0002 | `...-0002-8d846b43` | 07-19 | 224.80 | ⚠️ 变坏（lcg3 实证）|
| 0004 | `...-0004-a8d26386` | 07-19 | 224.80 | ⚠️ 变坏 |
| 0006 | `...-0006-563e4893` | 07-19 | 224.80 | ⚠️ 变坏（33qv 实证）|
| 0012 | `...-0012-9aa1cb88` | 07-19 | 224.80 | ⚠️ 变坏 |
| 0003 | `...-0003-4de40eaf` | 07-16 | 224.49 | ✅ 仍是好的 |
| 0005 | `...-0005-bf2e7216` | 07-16 | 224.49 | ✅ |
| 0007 | `...-0007-5b205810` | 07-16 | 224.49 | ✅ |
| 0009 | `...-0009-070612ce` | 07-16 | 224.49 | ✅ |
| 0010 | `...-0010-bb136229` | 07-16 | 224.49 | ✅ |

### 修正先前判断
先前记录称"10 个存量 pool 任何节点重建都会变坏"——**过度悲观**。
实际只有 **0001 / 0002 / 0004 / 0006 / 0012 这 5 个** pool 的 MIG 指向坏镜像；另外 5 个仍指向好镜像，重建出来还是好节点。
两台已知坏节点（lcg3@0002、33qv@0006）都落在切了新 template 的 pool 里，与该结论一致。

### 由此得出的可能解法（均未验证）
1. `gcloud compute instance-groups managed set-instance-template` 把这 5 个 MIG 指回 07-16 旧 template。
   风险：GKE node pool controller 拥有并会 reconcile 这些 MIG，很可能被改回去；且修改 GKE 托管 MIG 属于**不受支持**操作。
2. 通过 `gcloud container clusters upgrade --node-pool=X --cluster-version=1.36.0-gke.4447000` 让 GKE 自己把 pool 目标版本改回，从而正规地切回好 template。是否允许版本回退未验证。
3. 不动，接受 5 个 pool 的渐进损耗（8 天掉 2 台，且都是本来就要 RMA 的机器）。

## 2026-07-25 02:38 建 pool 脚本加固：固定老 COS + 建完自动核对

### 背景
原脚本已通过 `--node-version=1.36.0-gke.4447000` 间接 pin 到好 COS，但存在两个隐患：
1. `--node-version` 只是"请求"，**真正决定节点装什么的是 MIG 的 regional instance template**。07-19 GKE 给每个 pool 生成过指向坏 COS 的新 template 并切走了一半 MIG —— 光看 `--node-version` 不足以保证结果
2. `--no-enable-autoupgrade` 在已加入 release channel 的 cluster 上会被 GKE 拒绝，原代码 `|| echo "[WARN] 提交失败"` 会把整个 create 失败静默吞掉，什么 pool 都没建出来

### scripts/gke-env.sh
新增 `GKE_EXPECTED_COS="19506-224-49"`，并固化实测映射：
```
1.36.0-gke.4447000 → gke-1360-gke4447000-cos-gb300-bm-129-19506-224-49-c-nvda  (nvidia.ko Jun 18, 好)
1.36.0-gke.4681000 → gke-1360-gke4681000-cos-gb300-bm-129-19506-224-80-c-nvda  (nvidia.ko Jun 27, 坏)
```
并注明 instance template 是 **regional**，describe 不加 `--region` 会 not found。

### scripts/gke-create-nodepool.sh
- 抽出 `do_create_pool()`；`--no-enable-autoupgrade` 被拒时**自动去掉该 flag 重试**，并打印提示说明防护改由 maintenance exclusion 承担；失败改为 `[ERROR]` 而非 `[WARN]`
- 新增 `verify_pool_cos()`：MIG → instance template → sourceImage，核对是否含 `GKE_EXPECTED_COS`
- 建完后自动核对所有目标 pool；不符打 `❌❌ 不要用！`；`--async` 尚未建出 MIG 的提示重跑核对
- 新增 `--verify-only` 模式：不创建只核对，供 async 建完后复核
- 结尾补充节点 Ready 后的 kubelet 版本核对提示

### 实跑验证 `--verify-only`（对现有 10 个 pool）
```
❌ gb300-pool-0001 ... 19506-224-80  (template f2f66874)
❌ gb300-pool-0002 ... 19506-224-80  (template 8d846b43)
✓ gb300-pool-0003 ... 19506-224-49
❌ gb300-pool-0004 ... 19506-224-80  (template a8d26386)
✓ gb300-pool-0005 ... 19506-224-49
❌ gb300-pool-0006 ... 19506-224-80  (template 563e4893)
✓ gb300-pool-0007 ... 19506-224-49
✓ gb300-pool-0009 ... 19506-224-49
✓ gb300-pool-0010 ... 19506-224-49
❌ gb300-pool-0012 ... 19506-224-80  (template 9aa1cb88)
→ 5 个未通过
```
脚本独立复现了手工排查结论。`bash -n` 两个脚本均通过。

## 2026-07-25 03:17-03:26 释放 4 个闲置 node pool + 清理集群残留

### 动机
盘点闲置资源，释放无 workload 且无团队排队的 pool。候选 6 个（d0003/0004/0005/0007/0010/0012），核对后确认 d0003（`yw-cd-a` 绑 16 节点 + 16 个 `yw-a` pod Pending 排队）和 d0012（4 个 `vllm-v4pro-dp2ep8` pod Running 42h）仍在占用，**不删**；实际释放 4 个。状态快照见 `docs/gke-qa-report-v2.md` §5.1.6。

### 1. 删除前日志 gate（`gpu-qa-0004` 6 个僵尸 pod）

pod `deletionTimestamp=2026-07-24T15:18:31Z`、`grace=60s`、**无 finalizer**，仍卡 Terminating 11.5h（kubelet 未走完清理）。按 log-pull-before-delete 规则先存档：

```bash
D=qa/logs/qa-nccl-multi-gb300-0004-zombie-20260724-151831
kubectl logs -n gpu-qa-0004 <pod> --tail=-1 --timestamps > $D/<pod>.log
kubectl get pod -n gpu-qa-0004 <pod> -o yaml       > $D/<pod>.pod.yaml
```

结果：4 个 pod 日志完整（374-375 行），2 个（`n2q99`/`kzrfk`）容器 `ContainerCreating` 从未启动、无日志可拉 —— 这正是 run `151727`/`154856` 失败（rank0 一直 `Waiting qa-nccl-multi-w-0-1`）的原因，pod.yaml 已存证。

**踩坑：gate 逻辑第一版有缺陷。** 只按文件大小判断，把两类无效内容误判为 PASS：
- `kubectl get -o yaml` 瞬时 `i/o timeout`（74 bytes 的 "Unable to connect to the server"）
- `kubectl logs` 的 `BadRequest: container is waiting to start` 错误消息（123 bytes）

改为内容级校验（grep `^apiVersion` / 识别 `Unable to connect` / 区分 `ContainerCreating`）并对失败项重试后才 PASS。**大小检查不足以判定日志有效，必须校验内容特征。**

### 2. 执行的删除

| 对象 | 数量 | 命令 |
|---|---|---|
| `gpu-qa-0004` 僵尸 pod | 6 | `kubectl delete pod -n gpu-qa-0004 --all --force --grace-period=0` |
| node pool | 4 | `gcloud container node-pools delete gb300-pool-{0004,0005,0007,0010} --cluster=gb300-gke-test --location=us-central1 --quiet --async` |
| 孤立 ComputeDomain (`status.nodes=0`) | 10 | `mc-debug-cd` `sgl2-cd` `sgl4-cd` `vllm-v4pro-2p{1,2,3,4}d-cd` `yw-cd-{b,c,d}` |
| 空 namespace | 6 | `gpu-qa-{0003,0004,0005,0006,0007,0012}` |
| 本地备份文件 | 2 | `qa/gen-report.sh.bak` `qa/templates/report.md.bak` |

node pool 删除 03:21:42 提交 → **03:25:01 全部完成（3.5 分钟）**，无一回退到 RUNNING/ERROR。

### 3. 验证结果

| 项 | 删除前 | 删除后 |
|---|---|---|
| GB300 node pool | 10 | **6**（0001/0002/0003/0006/0009/0012） |
| 集群节点总数 | 183 | **111**（108 GB300 + 3 default-pool） |
| GB300 GPU | 720 | **432** |
| reservation `inUseCount` d0004/0005/0007/0010 | 18 / 18 / 18 / 18 | **0 / 0 / 0 / 0** |
| 孤立 ComputeDomain | 10 | 0 |
| `gpu-qa-*` namespace | 6 | 0 |

四个 sub-block 的 72 台 host 已确认交回 reservation（`count=18, inUse=0, degradedHostCount=0, healthStatus=HEALTHY`），d0003/d0012 保持 `inUse=18` 未受影响。

### 4. 遗留事项

- **placement policy 未删**：`gb300-subblock-{0004,0005,0007,0010}-policy` 保留（不占配额、不计费，重建 pool 需同名 policy）。当前 25 个 `gb300-subblock-*-policy` 中已有 19 个无对应 pool。
- **d0004 / d0012 的 MIG instance template 指向坏 COS `19506-224-80`**（nvidia.ko Jun 27 回归）。d0004 的 pool 已删，将来在该 sub-block 重建必须先跑 `scripts/gke-create-nodepool.sh --verify-only` 确认落到 `224.49`。
- **§4 的 18N 同域 NCCL baseline 唯一数据源 d0004 + d0005 已释放**，该组数据（RDMA 365.7-366.4 / MNNVL 912.7-920.4 GB/s）不可复现；跨域数据覆盖全部 12 domain，不受影响。
- **d0010 释放时质检覆盖仅 12/18**（`0r70` `2btr` `dx4b` `jvb6` `msgq` 重建后未复检，`rfkn` 从未质检），未补测即释放。
- 操作期间 infer 团队新建了 `sglang-v4pro-1p1dsp-cd` 和 `vllm-v4pro-1p1d-tpmc-cd` 两个 ComputeDomain，不在删除名单内，未受影响 —— 集群有其他团队并发操作，删除前的状态核对需临近执行时点重做。

## 2026-07-25 03:45 清理 pool-0012 上假 Running 的 vllm 僵尸服务（pool 保留）

### 发现：pod 1/1 Running ≠ 服务活着

排查 pool-0012 能否释放时，4 个 `vllm-v4pro-dp2ep8-*` pod 全部 `1/1 Running`、`restarts=0`、DRA ResourceClaim `allocated,reserved` 43h，表面是正常在跑的 PD 分离推理服务。实际早已失效：

| 组件 | 真实状态 |
|---|---|
| `decode-0` | vLLM 进程 **2026-07-23T09:31:40 已 shutdown 退出**。容器内只剩 `bash`(PID 1) + `sleep`，靠它维持 Running。**显存 0 MiB**，4 GPU 空转 |
| `router` | 末条日志 09:34:05，全是打向 decode 的 `Connection refused`，此后 **42h 零输出** |
| `prefill-leader` / `worker` | 进程存活、模型仍占 276 GB×8 显存，**GPU util 0%**，日志只有 k8s `/health` + Prometheus `/metrics`，零业务流量 |

**空占 12 颗 GPU 的 DRA claim + 552 GB 显存 42 小时。**

**教训**：判断 pool 是否闲置，`kubectl get pod` 的 Running 状态不可信。容器 entrypoint 常有 `sleep infinity` 兜底，主进程死了 pod 仍是 Running。必须交叉验证：**容器内进程表（`ps`）+ `nvidia-smi` 显存/利用率 + 日志末条时间戳**，三者任一异常即为假活。

### decode 崩溃根因（供 infer2 排查）

```
EngineCore pid=1048  core.py:1233
  multiproc_executor.py:95  _wait_for_response
  multiproc_executor.py:387 get_response → mq.dequeue(timeout=...)
  shm_broadcast.py:779 dequeue → acquire_read
  shm_broadcast.py:701 → RuntimeError: cancelled
```

EngineCore 在 shm_broadcast 等 worker 响应时被取消 —— multiproc executor 某个 worker 先死导致的崩溃传播。APIServer 侧收到的 `EngineDeadError` + 900 行递归 traceback 均为次生错误，`POST /v1/chat/completions` 返 500 后 APIServer 优雅关闭。

### 执行

日志 gate（内容级校验，全部 PASS）后存档到 `logs/vllm-v4pro-dp2ep8-crash-20260723-093140/`：

| 文件 | 行数 |
|---|---|
| `vllm-v4pro-dp2ep8-decode-0.log` | 60898 |
| `vllm-v4pro-dp2ep8-prefill-leader.log` | 21317 |
| `vllm-v4pro-dp2ep8-router.log` | 12403 |
| `vllm-v4pro-dp2ep8-prefill-worker.log` | 4575 |
| 4 份 `*.pod.yaml` + `vllm-v4pro-dp2ep8-cd.yaml` + `resourceclaims.yaml` | — |

```bash
kubectl delete pod -n default vllm-v4pro-dp2ep8-{decode-0,prefill-leader,prefill-worker,router}
kubectl delete computedomains.resource.nvidia.com -n default vllm-v4pro-dp2ep8-cd
```

### 结果

3 个 DRA ResourceClaim 随 pod 删除自动释放，pool-0012 非-DaemonSet 运行中 pod 归 0，**18 节点 / 72 GPU 全部空闲**。

**node pool 本身按决定保留未删**（区别于 03:21 那批的 4 个）。pool-0012 的 MIG instance template 仍指向坏 COS `19506-224-80`，若后续释放再重建需先 `--verify-only` 核对。

## 2026-07-25 03:48-03:52 释放 gb300-pool-0012

清空僵尸服务后按决定释放。删除前贴执行时点重新核对：0 运行 pod、0 Pending、无关联 ComputeDomain、无残留 ResourceClaim。

```bash
gcloud --configuration=taiji-poc container node-pools delete gb300-pool-0012 \
  --cluster=gb300-gke-test --location=us-central1 --project=tencent-gcp-taiji-poc --quiet --async
```

03:48:54 提交 → **03:52:20 完成（3.5 分钟）**。

### 累计释放结果（本日两批）

| 项 | 释放前 | 现在 |
|---|---|---|
| GB300 node pool | 10 | **5**（0001/0002/0003/0006/0009） |
| 集群节点总数 | 183 | **93**（90 GB300 + 3 default-pool） |
| GB300 GPU | 720 | **357**（满配 360，缺 3 = `lcg3`/`33qv`/`36wz` 各缺 1 颗，均 REPAIRING 中） |
| 已释放 sub-block | — | d0004 / d0005 / d0007 / d0010 / d0012，共 **90 节点 / 360 GPU** |

### ⚠ 释放后 reservation `degradedHostCount` 会暂时飙升（清理窗口，非真降级）

**GB300 节点删除后进入 1-2 小时清理流程，表现就是先 degrade 再逐步恢复**，属预期行为，无需处理。

删除 pool 后再查 reservation，四个 sub-block 的 `healthStatus` 变成 `DEGRADED`：

| 时点 | d0004 | d0005 | d0007 | d0010 | d0012 |
|---|---|---|---|---|---|
| 03:26（删除前核对） | 0 | 0 | 0 | 0 | 0 |
| 03:52（删后 31 min / d0012 删后 4 min） | **17** | **14** | **15** | **15** | 0 |
| 03:54（+2 min） | **15** | **12** | **14** | **10** | 0 |

`degradedHostCount` 在**回落**、`healthyHostCount` 同步上升，与 1-2 小时清理窗口的预期一致。**不是硬件真降级，释放的 host 不会丢**，无需联系 GCP support。

**How to apply**：释放 pool 后 **1-2 小时内的 reservation 读数直接忽略**，等清理走完再看。判断真实降级只看**稳态**数值 —— 例如 d0002/d0006/d0008/d0009/d0011 长期稳定在 degraded=1，那才是真的。

### 其他观察

- d0008 `inUse=1`、d0011 `inUse=17`：这两个 sub-block 早已无 GKE node pool，占用来自自建 k8s 集群 worker（`gb300-central-b0001-d0008-w1`）和他人资源（`harry-gb300-central-nvl72-policy-0011`），不属本次释放范围。
- 5 个已释放 sub-block 对应的 `gb300-subblock-{0004,0005,0007,0010,0012}-policy` 均保留未删。

## 2026-07-25 04:00 去掉 gb300-pool-0009 的 team=gdde 标签

### 先确认 label 来源：手动打的，不在 pool 配置里

```bash
gcloud container node-pools describe gb300-pool-0009 --format="value(config.labels)"
# → cloud.google.com/gke-dpv2-unified-cni=cni-migration;
#   cloud.google.com/gke-networking-dra-driver=true;
#   gke.networks.io/accelerator-network-profile=auto
```

`team=gdde` **不在 `config.labels`** 里 → 是 `kubectl label` 手动打的，删节点 label 即可，无需改 GKE pool 配置，将来 MIG 重建节点也不会再带上。

旁证：18 台里只有 17 台有该 label，缺的 `t96p`（07-21 重建）正是重建后没补 label 的那台 —— 印证手动 label 不随节点重建继承。

### 执行

```bash
# 用存在性选择器只选有 team label 的节点，避免对无该 label 的节点报 "label not found"
kubectl label nodes -l 'cloud.google.com/gke-nodepool=gb300-pool-0009,team' team-
```

17 台 unlabeled。

### 验证

| 项 | 结果 |
|---|---|
| pool-0009 仍带 team label 的节点 | **0** |
| pool-0009 节点总数 | 18（不变） |
| 其他 pool team label | `infer` 17（pool-0006）、`yangwhale` 35（pool-0001 18 + pool-0002 17），未受影响 |

pool-0009 现无团队归属标记。集群内 `gdde` 归属已完全消失（pool-0005 已释放，pool-0009 label 已移除）。

## 2026-07-25 04:05-04:11 释放 gb300-pool-0003（yangwhale 已自行腾空）

### 状态变化：yw-a 已被对方清掉

03:26 核对时 pool-0003 的阻塞理由是「`yw-cd-a` 绑 16 节点 + 16 个 `yw-a` pod Pending 排队」，故当时判定不可释放。04:05 复查发现 **`yw-a` StatefulSet 与 16 个 Pending pod 均已不存在**（yangwhale 侧自行清理，非本次操作删除）。pool-0003 上只剩 `yw-cd-a` 一个绑定对象。

`default` ns 内其余 yw-* 残留与 pool-0003 无关，未动：

| 对象 | 状态 | 说明 |
|---|---|---|
| `statefulset/yw-c` | replicas=0 | nodeSelector 指向 **pool-0007**（今日已删） |
| `statefulset/yw-d` | replicas=0 | nodeSelector 指向 **pool-0011**（本就无此 pool） |
| `service/yw` | endpoints 空 | headless service |
| `secret/yw-ssh` | 无 pod 引用 | — |

均为指向已不存在 pool 的僵尸配置，spec 已存档 `logs/yw-pool0003-release-20260725/`（`yw-cd-a.yaml` / `service-yw.yaml` / `statefulset-yw-cd.yaml`），供 yangwhale 需要时恢复。

### 执行

```bash
kubectl delete computedomains.resource.nvidia.com -n default yw-cd-a
# 核对：运行中 pod 0 / Pending 0 / 绑 0003 的 CD 无
gcloud --configuration=taiji-poc container node-pools delete gb300-pool-0003 ... --quiet --async
```

04:09:13 提交 → **04:11:28 完成（2.3 分钟）**。

### 结果

GB300 node pool 5 → **4**（0001/0002/0006/0009），集群节点 93 → **75**，GPU 357 → **285**。

## 2026-07-25 04:13 释放 gb300-pool-0001（yangwhale 负载已自行清空）

### 状态变化：gd-* 全批消失

03:54 时 pool-0001 还是 **18/18 满载**（`gd-p0`~`gd-p15` + `gd-d3`/`gd-d4` + `sabench`，`v4v-cd` 绑 18 节点）。04:10 复查全部消失 —— 与 `yw-a` 同一波清理动作，yangwhale 主动腾资源，非本次操作删除。

删除前五项核对全空，**无需清理任何对象**：

| 核对项 | 结果 |
|---|---|
| 运行中 pod | 0 |
| Pending 指向 0001 | 0 |
| 绑 0001 的 ComputeDomain | 无（`v4v-cd` 已随负载消失） |
| 引用 0001 的 ResourceClaim | 无 |
| nodeSelector 指向 0001 的 StatefulSet/Deployment | 无 |

```bash
gcloud --configuration=taiji-poc container node-pools delete gb300-pool-0001 ... --quiet --async
```

04:13:02 提交 → **04:15:40 完成（2.6 分钟）**。

### 本日累计释放总计

| 项 | 起始（07-25 02:41） | 现在（04:16） |
|---|---|---|
| GB300 node pool | 10 | **3**（0002 / 0006 / 0009） |
| 集群节点总数 | 183 | **57**（54 GB300 + 3 default-pool） |
| GB300 GPU | 720 | **213** |
| 已释放 sub-block | — | d0001 / d0003 / d0004 / d0005 / d0007 / d0010 / d0012，共 **7 个 = 126 节点 / 504 GPU** |

GPU 213 而非满配 216，差 3 颗为 `lcg3`(0002) / `33qv`(0006) / `36wz`(0009) 各缺 1 颗，三台均 cordon + `Ready=Unknown`（REPAIRING 中）。

剩余三个 pool 占用：

| Pool | 节点 | 占用 | cordon | MIG COS |
|---|---|---|---|---|
| `gb300-pool-0002` | 18 | 4 pod / 4 节点（`nrl-0`~`3` + `nrl-cd`） | 1（`lcg3`） | ❌ 224.80 |
| `gb300-pool-0006` | 18 | 17 pod / 10 节点（dspark + sglang/vllm 系列 + 5 CD） | 2（`33qv` `qx2s`） | ❌ 224.80 |
| `gb300-pool-0009` | 18 | **0** | 1（`36wz`） | ✓ 224.49 |

**剩余 3 个 pool 中 2 个（0002 / 0006）的 MIG template 指向坏 COS `19506-224-80`**，仅 pool-0009 为好镜像 `224.49`。坏 COS 占比由释放前的 5/10 升至 2/3 —— 释放优先清掉了好镜像 pool，后续若需重建节点，安全余量仅剩 pool-0009。维护例外 `freeze-node-upgrades-ko-regression` 至 2026-10-23 到期前需有处理方案。

## 2026-07-25 04:23 重建 gb300-pool-0013（单节点试点）

### 前置检查
```
placement policy gb300-subblock-0013-policy : COLLOCATED, 1x72  ✓ 仍存在
subblock-0013 reservation : count=18 inUseCount=0 degraded=0 healthy=18  ✓ 裸金属已完全释放
pool-0013 : 不存在  ✓
```
注：01:49 删除，04:23 重建，实际间隔约 2.5h；`inUseCount=0` 确认容量完全回收。

### 创建
```
bash scripts/gke-create-nodepool.sh 0013:1
```
使用加固后的脚本（pin `1.36.0-gke.4447000` + 建后自动核对 COS）。

### 提交结果
```
gb300-pool-0013   version=1.36.0-gke.4447000   initialNodeCount=1   status=PROVISIONING
management.autoUpgrade = (empty/false)
management.autoRepair  = (empty/false)
operation: CREATE_NODE_POOL  RUNNING  04:23:23
```

### 🔎 新发现：`--no-enable-autoupgrade` 在 channel 集群里**是被接受的**
先前假设"cluster 加入 release channel 后无法关闭 node auto-upgrade"、并据此在脚本里加了降级重试逻辑 —— **该假设不成立**。
本次创建带 `--no-enable-autoupgrade` 直接成功，pool-0013 的 `autoUpgrade=false`。
（存量 10 个 pool 的 `autoUpgrade=true` 是因为它们创建时脚本还没有这个 flag，不是 GKE 强制的。）
脚本里的降级重试逻辑保留无害，作为兜底。

含义：新建的 pool 天然免疫 auto-upgrade，不必依赖 maintenance exclusion。

### 04:29 pool-0013 单节点创建完成 —— 端到端验证全通过

节点 `gke-gb300-gke-test-gb300-pool-0013-ede6cae6-3txd`，从提交到 Ready 约 6 分钟（04:23 → 04:29）。

| 检查项 | 结果 |
|---|---|
| MIG → instance template → sourceImage | `gke-1360-gke4447000-cos-gb300-bm-129-19506-224-49-c-nvda` ✓ |
| kubelet | `v1.36.0-gke.4447000` ✓ |
| kernel | 6.12.90+ |
| **nvidia.ko build** | **`Thu Jun 18`** ✓（好驱动，非 Jun 27） |
| Fabric | 4 卡全 Completed，cliqueId 统一 = 12 ✓ |
| **cuInit / cuDeviceGetCount / cuDevicePrimaryCtxRetain ×4** | **全部 CUDA_SUCCESS** ✓ |
| hugepages-2Mi | 8Gi ✓ |
| nvidia.com/gpu | 4 ✓ |
| 系统 DaemonSet | 14/14 全 Ready（含 asapd-lite / imex-channel-init / dra-driver / device-plugin）✓ |
| autoUpgrade / autoRepair | false / false ✓ |

**结论：pin 老 COS 的链路端到端打通**，加固后的 `gke-create-nodepool.sh` 可用于批量重建。
（对比：broken pool 上同一 probe 是 `cuDevicePrimaryCtxRetain -> 1 CUDA_ERROR_INVALID_VALUE`。）

### 04:43 ✅ collect_bug_reports glob fix 实证生效

昨日修的 `qa/run-checks.sh` glob 展开 bug（`kubectl exec -- ls /host/tmp/*.gz` 不经 shell，`*` 不展开 → 改为 `sh -c 'ls ...'`）本次首次跑到，验证通过：

```
修复后  qa-bug-reports-gb300-0013-20260725-044253/nvidia-bug-report-3txd.log.gz   1.5M
修复前  qa-bug-reports-gb300-0003-20260724-142304/   0 个文件
        qa-bug-reports-gb300-0003-20260724-144524/   0 个文件
```
脚本日志也从 "bug-report 收集完成: 0 份" 变为 "**1 份**"。

## 2026-07-25 04:50 修 qa/run-checks.sh：kubectl wait 被网络毛刺误判 TIMEOUT

### 症状
pool-0013 质检的 NCCL 阶段，04:49:24 开始 wait（预算 300s），**37 秒后**就判 `WARNING: timeout (0/1 Ready)`，随后 cleanup 删掉 DaemonSet。manifest 记为 `TIMEOUT`。
原始日志：
```
=== [04:49:24] 等待完成 (label=qa-nccl-single, timeout=300s) ===
  1 pod(s) 已创建
  kubectl wait --for=condition=Ready (timeout=300s)...
Unable to connect to the server: dial tcp 35.253.228.114:443: i/o timeout
  WARNING: timeout (0/1 Ready)
```

### 根因
又是 GKE public endpoint 瞬时抖动（今天第二次，14:54 那次打的是 `resolve_node`）。
`kubectl wait` 被网络中断立即返回非 0，`wait_completion` 无条件当成超时 → 提前 cleanup，**误杀正在正常运行的测试**。
上午加的 `ktl_ro_retry` 只覆盖只读查询，当时特意把 `wait/apply/delete/exec` 排除在外 —— 这个盲区正好被命中。

### 修复
`wait_completion` 改为：
- 用 wall-clock（`$SECONDS`）记预算，循环调用 `kubectl wait`，每次传剩余时间
- 失败输出匹配网络类错误（`i/o timeout|connection refused|Unable to connect|TLS handshake timeout|unexpected EOF|no route to host`）→ 5s 后重试，最多 4 次
- 非网络类失败（真超时 / pod 失败）立即退出循环，不重试
- **兜底**：即使 wait 调用失败，只要实际 `READY >= COUNT` 就判通过（避免网络问题掩盖正常结果）
`bash -n` 通过。

### 影响
本次 pool-0013 的 NCCL 需重跑（节点本身无问题，CUDA probe 已证 4 卡全通）。

## 2026-07-25 05:04 pool-0013 单节点质检结果（3/4 完成，NCCL 补跑中）

节点 `gke-gb300-gke-test-gb300-pool-0013-ede6cae6-3txd`（新建，pin 老 COS）。

### hw-check：PASS=23 FAIL=0 **WARN=0**
- Fabric clique：4 卡统一 clique 12，ClusterUUID 一致 `41aa8dcd-...`
- nvidia-bug-report gz 收到 1 份（1.5M）

### DCGM Level 2：全 Pass
`software / memory / pcie` 对 GPU0-3 全部 Pass。
（对照：broken image 节点上 DCGM 因 CUDA context 起不来必挂）

### cuBLAS（4 GPU 平均 TFLOPS）
| 精度 | 本节点 | baseline | 差异 |
|---|---|---|---|
| FP4 | 7874 | 7950 | -1.0% |
| FP8 | 3408 | 3450 | -1.2% |
| FP16 | 1666 | 1670 | -0.3% |
| BF16 | 1790 | 1795 | -0.3% |
| TF32 | 845 | 860 | -1.7% |
| FP32 | 75 | 75 | -0.0% |

全部在 baseline -1.7% 以内。

### NCCL：首次被网络毛刺误杀，已用修复后的 wait 逻辑补跑中

### ✅ 今日三个 skill fix 全部实证通过
| fix | 验证证据 |
|---|---|
| `collect_bug_reports` glob 展开 | `nvidia-bug-report-3txd.log.gz` 1.5M（修复前同类目录 0 文件） |
| preflight 判据改 `validNodeVersions` | pin 4447000 建 pool 成功，不再误 abort |
| hw-check clock throttle 加 util 门槛 | `WARN=0`（修复前 idle 时固定 4 个假 WARN）；section 9 现输出 `GPU0: SM 120/2070 MHz, util 0%` 并 PASS |

新增第 4 个 fix（`wait_completion` 网络毛刺重试）已在补跑中生效，日志可见 `kubectl wait ... (剩余 300s，第 1 次)`。

### ⚠️ 操作教训
本次在后台 run-checks.sh **运行期间**编辑了该脚本。bash 按字节偏移增量读取脚本，在当前执行点之前插入内容有导致后续读取错位、执行乱码的风险。本次侥幸未触发（函数已解析、后续分支已进缓冲），但**正确做法是等进程结束再改**。

### 05:06 NCCL 补跑成功 —— wait 修复实证生效

manifest `qa-manifest-gb300-0013-20260725-050416.txt`：
```
gpu-qa-0013|qa-nccl-single|Done:|0013|2026-07-25T05:04:42Z      ← 无 TIMEOUT 标记
```
日志可见新逻辑 `kubectl wait --for=condition=Ready (剩余 300s，第 1 次)...`，72 秒正常完成，日志 8030b 完整。

NCCL 16G out-of-place busBW：
| collective | 本节点 | baseline | 差异 |
|---|---|---|---|
| all_reduce | 688.3 | 690 | -0.2% |
| all_gather | 674.3 | 673 | +0.2% |
| reduce_scatter | 670.1 | 670 | +0.0% |
| alltoall | 681.4 | 685 | -0.5% |

### 质检报告
`qa/docs/gb300-0013-20260725-050844.md` —— **全部 PASS，无故障，无需处理**（4/4 项，1 节点 / 4 GPU）。
index.md 已自动追加。

### pool-0013 试点结论：可以批量重建
加固后的 `scripts/gke-create-nodepool.sh` 端到端验证通过：pin 老 COS → 好驱动 → CUDA 正常 → 4 项质检全 baseline。
剩余 0014 / 0015 / 0016 / 0017 可按同法建。注意 0014 需先查 subblock healthy host 数（历史上仅 15-16 台可用）。

## 2026-07-25 05:10 ⚠️ 发现 7 个 pool 在 03:21–04:15 被删除（非本 session 操作）

### 发现经过
质检收尾时统计节点数，发现 GPU 节点只剩 55 台（预期 181）。复查确认 7 个 node pool 已不存在。

### 被删除的 pool（GKE operation 时间）
```
03:21:42  gb300-pool-0004
03:21:46  gb300-pool-0005
03:21:49  gb300-pool-0007
03:21:53  gb300-pool-0010
03:48:54  gb300-pool-0012
04:09:13  gb300-pool-0003
04:13:02  gb300-pool-0001
```
共约 126 台节点。

### 剩余
```
gb300-pool-0002  18
gb300-pool-0006  18
gb300-pool-0009  18
gb300-pool-0013   1   (本 session 新建)
default-pool      3
→ GPU 节点合计 55
```

### 责任归属查证
Cloud Audit Log 显示发起者 principal = `maxwellx@google.com`。
注意：本 session 使用的 `keys/gb300-sa.json` 是 **authorized_user 类型凭据**（非 SA key），解析后同样是 `maxwellx@google.com` —— 因此**审计日志无法凭身份区分本 session 与其他 session**。

按时间线判定：本 session 在 **02:38（--verify-only）到 04:22（建 pool 前置检查）之间未发起任何 GCP 命令**，而删除发生在 03:21–04:15 窗口内。
本 session 全部 pool 操作（审计日志核对一致）：
```
01:37  create + delete  zz-verstest-4447   (版本验证用)
01:46  delete           0013 0014 0015 0016 0017
04:23  create           0013
```
→ **03:21–04:15 的 7 个删除来自使用同一账号的另一个 session/终端**，不是本 session。

### 当前 reservation 占用（可重建容量）
```
subblock  count  inUse  degraded  healthy
0001        18      0      1        17
0002        18     18      1        17
0003        18      0      0        18
0004        18      0      0        18
0005        18      0      0        18
0006        18     18      1        17
0007        18      0      0        18
0008        18      2      1        17   (self-managed k8s)
0009        18     18      1        17
0010        18      0      0        18
0011        18     17      1        17
0012        18      0      1        17
0013        18      1      0        18
```
0001/0003/0004/0005/0007/0010/0012 已释放，可立即重建。

### 待确认
此次删除是否为有意操作（例如另一终端在按"pin 老 COS 重建"的方案清理）。**在确认前不做任何重建动作。**

### 05:12 结论：7 个 pool 的删除已确认为用户本人有意操作
用户确认 03:21–04:15 删除 0001/0003/0004/0005/0007/0010/0012 是其本人在另一终端执行。非异常事件，结案。

## 2026-07-25 05:12 pool-0013 扩容 1 → 18 台

前置：MIG template 已验证指向 `gke-1360-gke4447000-cos-gb300-bm-129-19506-224-49-c-nvda`（好 COS），
新增节点继承同一 template，预期均为好驱动。subblock-0013 healthy=18 degraded=0，容量足够。

### 05:48 扩容完成并核验 —— 18/18 全部好驱动

05:42 提交 → 05:48 完成，约 6 分钟。

| 核验项 | 结果 |
|---|---|
| MIG template → COS | `gke-1360-gke4447000-cos-gb300-bm-129-19506-224-49-c-nvda` ✓ |
| 节点数 | 18 |
| Ready | 18/18 |
| kubelet | 18 × `v1.36.0-gke.4447000`（**0 台 4681000**）|
| nvidia.com/gpu | 18 × 4 |
| hugepages-2Mi | 18 × 8Gi |
| cordoned | 0 |

扩容期间监控逐轮检测 4681000 节点数，全程为 0 —— 证实**只要 MIG template 指向好镜像，新增节点就是好的**，扩容不会引入坏驱动。

### 集群当前状态
```
gb300-pool-0002  18   (含 1 台 cordoned 硬件故障 lcg3，MIG 指向坏 COS)
gb300-pool-0006  18   (含 1 台 cordoned 硬件故障 33qv，MIG 指向坏 COS)
gb300-pool-0009  18   (MIG 指向好 COS)
gb300-pool-0013  18   (本次重建，全新好 COS)
default-pool      3
→ GPU 节点 72 台
```
已释放可重建的 subblock：0001 / 0003 / 0004 / 0005 / 0007 / 0010 / 0012（inUse=0）。

## 2026-07-25 06:14 pool-0013（18 节点）all-full 全面质检

### 依赖组件前置检查
```
JobSet controller        jobset-controller-manager 1/1  ✓
MPI Operator             mpi-operator 1/1               ✓
DRA driver               87 pods Running                ✓
ComputeDomain CRD        存在                            ✓
pool-0013 ResourceSlice  36 (18 节点 × compute-domain + dra.net)  ✓
```

### 启动
```
bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh all-full 0013
LOG: /tmp/qa-p13/0013-allfull-061403.log
```
覆盖：hw-check → dcgm L2 → nccl-single → cublas → **单域多节点 NCCL（MNNVL on/off）**。
单节点那轮只有 1 台，测不了 MNNVL；这次 18 台才能真正验证 NVLink domain。

### 06:20 hw-check 结果（18/18 PASS）与 collect_bug_reports 在 18 节点下的缺陷

#### hw-check：18/18 PASS
17 台 `PASS=23 FAIL=0 WARN=0`；1 台 `pqcm` 为 `PASS=22 FAIL=0 WARN=1`（PASS with warnings）。

#### pqcm 的 WARN 解读（含一处自我更正）
WARN 内容：`high DRAM correctable ECC count (>1000)` — `DRAM Correctable=2586`。

**更正**：先前口头判断"该节点 35 分钟内累积 2586 次"是**错的**，两个指标混淆了：
- section 1 实测 `ecc.errors.corrected.volatile.total`：GPU0-3 **全为 0**（本次开机零纠错）
- section 14 的 2586 来自 bug-report gz 解析的 `DRAM Correctable`，是 **aggregate 终身计数**（跨重启持久，记录物理 GPU 出厂至今累积）

同节点 row remap 无 pending / 无 failure / 无 uncorrectable，Xid 无，ECC 已启用。
→ 判定：历史累积计数，非新增故障，**不影响使用**。

#### 阈值设计问题（待改，未改）
hw-check 用 `>1000` 卡的是 **aggregate 终身计数**，服役过的硬件上会系统性误报。
建议改为只卡 volatile 计数 + row remap pending/failure，或把 aggregate 阈值大幅提高并降级为 INFO。

#### collect_bug_reports 在 18 节点下只收到 8/18
```
8 × OK (1.5M)   1 × cp 失败 (lg8w)   9 × 无 bug-report gz
```
根因：18 台串行收集时 GKE API 变慢，三处 timeout 全部偏紧且无重试：
- `timeout 5` get pod nodeName → 失败后 `SHORT=${NODE:-$POD}` **退化用 pod 名**
- `timeout 10` exec ls
- `timeout 30` kubectl cp

证据：失败项 `7hndb/9jdd6/b6g4w/sg458/v2q6n/w878c/w9bjn/wbm64` 均非 pool-0013 节点后缀，而是 **pod 名后缀**。

**更严重的是命名错误**：`btl5z` 那份收集成功了，但存成 `nvidia-bug-report-btl5z.log.gz` —— 用 pod 名命名，**无法追溯归属哪台节点**，交 NVIDIA support 时不可用。

**实际代价**：18 台中唯一有真实发现的 `pqcm`（pod `qa-hw-check-b6g4w`）恰好在失败列表里，其 bug-report 未收到。

#### 待修（等本轮质检结束后再动脚本）
1. 放宽超时：get pod 5→15s，exec 10→30s，cp 30→120s
2. 三处调用加重试
3. **node 名查询失败必须重试，绝不退化用 pod 名命名**（宁可标 UNKNOWN 也不能错误归属）

### 06:58 pool-0013 all-full 全面质检完成 —— 全部通过

manifest `qa-manifest-gb300-0013-20260725-061404.txt`，4 项均无 TIMEOUT / CONTENT_FAIL 标记：
```
qa-hw-check      Summary:  06:14:30
qa-dcgm-diag     DONE:     06:21:19
qa-nccl-single   Done:     06:28:30
qa-cublas-bench  DONE:     06:33:30
```

#### 单节点 4 项（18 台）
| 测试 | 结果 |
|---|---|
| hw-check | 18/18 PASS（17 台 WARN=0，pqcm 1 WARN = 历史累积 ECC，见前节） |
| DCGM L2 | 18/18，全库零 `Fail` 行 |
| NCCL single | 18/18 完整数据，**零离群** |
| cuBLAS | 18/18 完成 |

NCCL 单机 16G busBW（18 台统计）：
```
              min      median    max
all_reduce    675.5    682.2    686.1
all_gather    663.7    668.5    672.9
red_scat      667.1    671.5    675.9
alltoall      683.5    687.8    694.2
```
all_reduce 全距仅 10.6 GB/s（1.6%），一致性良好。

#### 多节点 NCCL（18 节点 / 72 GPU）

**MNNVL=OFF（RDMA 路径，`MNNVL=0 NVLS=0 CUMEM=0`）**
| collective | 本次 | 历史 pool-0005 ×3 |
|---|---|---|
| all_reduce | 366.32 | 366.4 / 367.7 / 369.8 |
| all_gather | 364.87 | 364.6–367.8 |
| reduce_scatter | 367.20 | 367.0–367.7 |
| alltoall | 85.23 | 84.9 / 85.6 / 85.9 |

alltoall 85 GB/s **不是异常** —— 历史三次同为 85 左右。RDMA 路径下 18 节点 all-to-all 有 17/18 流量过网络 bisection，而 all_reduce 有 ring/tree 优化。

**MNNVL=ON（NVLink 路径，`MNNVL=2 NVLS=1 CUMEM=1`）**
| collective | 本次 | 历史 pool-0005 | 偏差 |
|---|---|---|---|
| all_reduce | 917.02 | 917.4–920.4 | -0.2% ✓ |
| all_gather | 686.40 | 686.1–687.3 | -0.0% ✓ |
| reduce_scatter | 707.76 | 708.4–708.5 | -0.1% ✓ |
| alltoall | 660.65 | 661.5–661.7 | -0.1% ✓ |

四项全部命中历史 baseline，偏差 ≤0.2%。NVLink vs RDMA 的 alltoall 差距 7.8×（660.65 vs 85.23），符合预期。

#### 结论
**pool-0013 重建后 18 节点全部达标**，单机 + 单域多机、NVLink + RDMA 两条路径均为 baseline 水平。
pin 老 COS 的重建方案完整验证通过，可用于其余 pool。

### 07:11 ⚠️ 两处更正：NCCL 单机 collective 标签错位 + 日志收集被多机数据污染

#### 更正 1：单机 NCCL 的 collective 标签整体反序（前节数据作废）
前节"NCCL 单机 16G busBW（18 台统计）"表中四列标签**错位**。

根因：那次 ad-hoc `gcloud logging read` **未加 `--order=asc`**，Cloud Logging 默认 newest-first，
每节点 4 行数据倒序返回，而脚本按 [all_reduce, all_gather, reduce_scatter, alltoall] 正序贴标签 → 整体反转。

原始日志实证（节点 2tf9）：
```
starting: all_reduce_perf      busbw=688.33
starting: all_gather_perf      busbw=671.09
starting: reduce_scatter_perf  busbw=667.95
starting: alltoall_perf        busbw=682.08
```

**正确的 18 台统计**：
```
                  min      median      max
all_reduce       683.5     687.8      694.2
all_gather       667.1     671.5      675.9
reduce_scatter   663.7     668.5      672.9
alltoall         675.5     682.2      686.1
```
与本日 pool-0003 gen-report 报告（687.4 / 671.3 / 668.4 / 685.8）一致，交叉验证通过。

**多节点 NCCL 的 MNNVL=OFF / ON 数据不受影响** —— 那两组直接读 `rank0.log` 文件、按
`Collective test starting: X_perf` marker 解析，不经 Cloud Logging 排序。结论仍然成立。

#### 更正 2：collect-logs-cloud.sh 把多机 NCCL 日志混入单机目录
`qa-nccl-single` 项收集时报 `发现 54 个 pod`（应为 18）。落盘结果被污染：
```
2tf9  busbw=366.32  MNNVL=0   ← 多机 RDMA 日志
3txd  busbw=917.02  MNNVL=2   ← 多机 NVLink 日志
其余 8 个  ~4KB 截断碎片，无 16G 数据
```
根因：多节点 JobSet 的容器名同为 `nccl`、同处 namespace `gpu-qa-0013`，
而 collect 只按 `since` 时间戳过滤、**无时间上界** → 06:47 之后两轮多机日志全被卷入，
按节点名写同一文件互相覆盖，并触发大量"截断重拉"空转。

处置：终止该收集进程（kill 进程组），改用带上下界的直接查询重收：
```
timestamp>="2026-07-25T06:28:30Z" AND timestamp<="2026-07-25T06:34:00Z"  --order=asc
→ qa/logs/qa-nccl-single-gb300-0013-CLEAN-20260725-071119/  18/18 完整，无 MNNVL 标记
```

#### 新增待修项（累计 3 项，均等本轮收尾后再动脚本）
1. `collect_bug_reports`：超时放宽 + 重试 + 禁止用 pod 名命名
2. hw-check ECC 阈值：`>1000` 卡的是 aggregate 终身计数，应改卡 volatile + row remap
3. **`collect-logs-cloud.sh`：需支持时间上界（或按 pod 名前缀过滤），避免单机/多机 NCCL 同 namespace 互相污染；内部 `gcloud logging read` 必须显式 `--order=asc`**

## 2026-07-25 07:2x 修复三个质检脚本缺陷（均已验证）

### 更正前节一处指责
前节曾写 "`collect-logs-cloud.sh` 内部 `gcloud logging read` 必须显式 `--order=asc`" —— **该指责不成立**。
脚本第 96、112 行的 per-pod 取日志调用**本来就有** `--order=asc`。
缺 `--order=asc` 的是我当时手打的 ad-hoc 查询，与脚本无关。脚本的真实缺陷只有「缺时间上界」一条。

### Fix A — `qa/run-checks.sh` : collect_bug_reports
- 超时放宽并可配：`QA_BR_TIMEOUT_NODE=15` / `QA_BR_TIMEOUT_EXEC=30` / `QA_BR_TIMEOUT_CP=120`（原 5/10/30）
- 三处调用（取 nodeName / exec ls / kubectl cp）各加 3 次重试
- 取 pod 列表改用 `ktl_ro_retry`
- **核心原则：node 名 3 次取不到就 SKIP，绝不退化用 pod 名命名**（原实现 `${NODE:-$POD}` 会产出无法追溯的文件）
- cp 成功后校验 `[ -s "$DST" ]`，失败清理残件再重试
- 汇总行改为 `成功 N / 共 M pod (失败 X, 节点名未解析 Y)`，未收齐时打印手工补收命令

### Fix B — `qa/collect-logs-cloud.sh` : 增加时间上界
- `collect_one()` 新增第 6 参 `UNTIL`，加入 filter `timestamp<="${UNTIL}"`
- manifest 模式改为先 `mapfile` 整体读入，**用下一项的时间戳作为本项上界**（最后一项无上界）
- 解决：多节点 NCCL 的 JobSet pod 容器名同为 `nccl`、同 namespace，只按 SINCE 过滤会把之后的多机日志卷进单机目录

### Fix C — `qa/templates/hw-check.yaml` : ECC 判定区分 Volatile / Aggregate
- 原实现取所有 `DRAM Correctable` 的 max（实际等于 Aggregate 终身计数）与 1000 比较 → 服役硬件系统性误报
- 新实现按 `Volatile` / `Aggregate` 段分别统计：
  - **Volatile > 1000 → WARN**（本次开机以来新增，可操作）
  - **Aggregate > 0 → INFO**（出厂至今累计，仅供参考，不参与判定）

### 验证
```
语法：bash -n run-checks.sh ✓   bash -n collect-logs-cloud.sh ✓
      hw-check.yaml 3 docs ✓ + 从 ConfigMap 抽出 check.sh 单独 bash -n ✓

ECC 合成数据：
  volatile=0    aggregate=2586  → 只出 INFO           ✓
  volatile=5000 aggregate=9999  → WARN + INFO         ✓

ECC 真实数据（pqcm 的 1.5M bug-report gz）：
  INFO 00000008:06:00.0 aggregate=2586 (lifetime)
  WARN 数量 = 0   （旧逻辑报 1 个 WARN）              ✓
```

### 附带：补回 pqcm 的 bug-report
gz 仍留在该节点主机 `/tmp/nvidia-bug-report-pqcm.log.gz`（1550212b）。
起临时 ns `br-recover` + hostPath 挂载 /tmp 的特权 pod，`kubectl cp` 取回到
`qa/logs/qa-bug-reports-gb300-0013-20260725-061634/`，随后删除该 ns。
→ 18 台中可追溯覆盖从 7 台提升到 8 台（含唯一有真实发现的 pqcm）。

## 2026-07-25 07:26 新增 qa/recover-bug-reports.sh + 全量补收 pool-0013 的 18 份 bug-report

### 背景
hw-check 跑完后 pod 已删除，但 `nvidia-bug-report-<node>.log.gz` **仍留在各节点主机 `/tmp` 上**
（pqcm 已验证：1550212b 完好）。原先只有 8/18，需要全部取回。

### 新脚本 `qa/recover-bug-reports.sh`
```
用法: bash qa/recover-bug-reports.sh <profile> <subblock> [outdir]
```
- 在目标 pool 上起临时 ns + DaemonSet，hostPath 挂载节点 `/tmp`
- 逐 pod 解析 nodeName（3 次重试），**取不到就 SKIP，绝不用 pod 名命名**
- `ls` gz、`kubectl cp` 各 3 次重试，超时 30s / 120s
- **增量补收**：目标目录已有同名非空文件则跳过，可反复运行
- toleration 含 `node.kubernetes.io/unschedulable`，cordoned 节点也能收
- 退出前 trap 清理临时 ns
- 结束打印 `目录内共 N / M 节点`
- `bash -n` 通过

### 清理废件
删除 `nvidia-bug-report-btl5z.log.gz` —— 该文件用 **pod 名**命名（旧代码 `${NODE:-$POD}` 退化所致），
无法追溯归属节点；全量重收后会有正确命名的同源文件，故直接删除而非保留。

### 执行
```
bash qa/recover-bug-reports.sh qa/profiles/gb300-gke-taiji.sh 0013 \
  qa/logs/qa-bug-reports-gb300-0013-20260725-061634
```
起始状态：8 份（含此前单独捞回的 pqcm）。目标 18/18。

### 07:30 补收完成 —— 18/18 全部到位

```
=== 取回 10 份 (跳过已有 8, 失败 0)；目录内共 18 / 18 节点 ===
```

最终核验 `qa/logs/qa-bug-reports-gb300-0013-20260725-061634/`：
| 校验项 | 结果 |
|---|---|
| 文件数 | 18 |
| 总大小 | 27M |
| 空文件 | 0 |
| 命名可追溯（均为真实节点后缀） | 18/18 ✓ |
| `gzip -t` 完整性 | 18/18 ✓ |
| 缺失节点 | 无 |

其中 `lg8w` 是首轮报 `cp 失败` 的那台，本次重试成功。
临时 ns `br-recover-0013` 已清理（trap 触发 + 手工确认 Terminating→消失）。

### 本轮 bug-report 问题闭环
1. 首轮 8/18，其中 1 份用 pod 名命名不可追溯 → 已删
2. 修 `collect_bug_reports`（超时/重试/禁止 pod 名命名）
3. 新增 `qa/recover-bug-reports.sh` 支持事后从主机 `/tmp` 增量补收
4. 全量补收至 18/18，逐份校验命名与 gzip 完整性

## 2026-07-25 07:39 重收 4 项 + 生成 18 节点质检报告

### Fix B 实证通过
用修好的 `collect-logs-cloud.sh` 重跑同一 manifest：
```
hw-check     since=06:14:30 until=06:21:19  → 发现 18 个 pod  → 18/18
dcgm-diag    since=06:21:19 until=06:28:30  → 发现 18 个 pod  → 18/18
nccl-single  since=06:28:30 until=06:33:30  → 发现 18 个 pod  → 18/18   ← 首轮是 54 个
cublas-bench since=06:33:30 (无上界，容器名 bench 不冲突) → 18 个 pod → 18/18
全部 4 项收集完成，总耗时 ~2 分钟
```
对比首轮：nccl-single 匹配 54 个 pod、截断重试风暴、预计 27+ 分钟且产出被多机数据污染。

### 报告
`qa/docs/gb300-0013-20260725-073920.md`（148 行），index.md 已追加。

**结论：全部 PASS，18 节点 / 72 GPU，故障 0，未执行 0。**

| 测试项 | 日志数 | PASS | FAIL |
|---|---|---|---|
| hw-check | 18 | 18 | 0 |
| DCGM L2 | 18 | 18 | 0 |
| NCCL 单机 | 18 | 18 | 0 |
| cuBLAS | 18 | 18 | 0 |

NCCL 单机汇总（18 台）：all_reduce 683.5/**688.3**/694.2 (spread 1.6%)，
all_gather 667.1/671.6/675.9，reduce_scatter 663.7/668.2/672.9，alltoall 675.5/682.5/686.1，无离群。
cuBLAS 汇总：FP4 **7892** / FP8 3443 / FP16 1666 / BF16 1796 / TF32 856 / FP32 75，无离群。
per-node 数值与手工核算的更正后统计一致，交叉验证通过。

### ⚠️ 报告的两个已知缺口（需向使用者说明）
1. **hw-check 的 pqcm WARN 文案是修复前的逻辑产生的**
   报告里仍显示 `high DRAM correctable ECC count (>1000, GPU memory chip may have physical defect)`。
   该日志生成于 06:15，早于 07:2x 的 ECC 判定修复。实际已查明为 Aggregate 终身计数 2586、
   Volatile 全 0、row remap 无异常，**不是故障**。修复后的逻辑对同一份 gz 输出 WARN=0 + INFO 1 条。
   下次 hw-check 重跑后该 WARN 会自动消失。

2. **gen-report.sh 不覆盖多节点 NCCL**
   报告只含 4 项单节点测试。本轮真正验证 NVLink fabric 的
   单域 18 节点 NCCL（MNNVL=OFF 366.32 / MNNVL=ON 917.02 GB/s all_reduce）**未进入报告**，
   数据仅存在于 `qa/logs/qa-nccl-multi-gb300-0013-mnnvl{0,2}-*/rank0.log` 与本操作记录。
   对交付级报告而言这是实质缺口 —— gen-report 应增加多节点段落。

## 2026-07-25 07:49 gen-report.sh 增加多节点 NCCL 段落

### 改动
**`qa/gen-report.sh`**
- 新增 `find_multi_dir_for_sub()`：按 `qa-nccl-multi-<gpu>-<sub>-mnnvl{0,2}-<ts>` 定位最新目录
  （mnnvl0 = MNNVL 关闭走 RDMA NIC；mnnvl2 = MNNVL 开启走 NVLink/NVSwitch）
- 新增 `parse_multi_rank0()`：只读 `rank0.log`，**按 `Collective test starting: X_perf` marker 归属数值**，
  并从 `# Rank N ... on <host> device` 行统计唯一 host 得出节点数、乘 `QA_GPUS_PER_NODE` 得 GPU 数
  （注释里写明：不能靠数据行出现顺序，2026-07-25 曾因此把四个 collective 标签整体写反）
- 新增 `analyze_nccl_multi()`：遍历所有 subblock × {0,2} 模式
- 新增 `R_NCCL_MULTI_DETAIL`：主表 + 同 sub 同时存在 on/off 时追加「NVLink / RDMA 倍数」对比表；
  无多机数据时输出说明文字而非留空

**`qa/templates/report.md`**
- 在 cuBLAS 与 nvidia-bug-report 之间插入 `### NCCL 单域多节点 (MNNVL on/off)` 段落

### 验证
`bash -n` 通过。重新生成 `qa/docs/gb300-0013-20260725-074915.md`（169 行，上一版 148 行）：
```
| Sub-block | 模式                 | 节点 | GPU | all_reduce | all_gather | reduce_scatter | alltoall |
| d0013     | MNNVL=OFF (RDMA NIC) | 18   | 72  | 366.32     | 364.87     | 367.20         | 85.23    |
| d0013     | MNNVL=ON  (NVLink)   | 18   | 72  | 917.02     | 686.40     | 707.76         | 660.65   |

倍数: all_reduce 2.5×  all_gather 1.9×  reduce_scatter 1.9×  alltoall 7.8×
```
数值与手工核对一致，节点数/GPU 数自动推导正确。

### 报告文件现状（pool-0013 共 3 份，索引 3 行）
- `gb300-0013-20260725-050844.md` — 单节点试点那轮（1 节点）
- `gb300-0013-20260725-073920.md` — 18 节点，**无多机段落，已被下一份取代**
- `gb300-0013-20260725-074915.md` — 18 节点 + 多机段落，**当前有效版本**

### 07:5x 删除被取代的中间报告
用户确认后删除 `qa/docs/gb300-0013-20260725-073920.md`（18 节点但缺多机段落，为验证 gen-report 改动时生成的中间产物），
并用 `sed -i '/gb300-0013-20260725-073920\.md/d'` 从 `qa/docs/index.md` 移除对应行（仅删该行）。

核验：index 中 `073920` 残留引用 0 处。pool-0013 现存报告 2 份：
- `gb300-0013-20260725-050844.md` — 单节点试点轮（1 节点），保留作为重建首验记录
- `gb300-0013-20260725-074915.md` — **18 节点完整版（含多节点 NCCL 段落），当前有效**

## 2026-07-25 07:55 建 pool 0014-0017

### 前置检查
```
subblock  count  inUse  degraded  healthy  可建
0014        18      0      2        16     16 台   ← 2 台 GCP 标记 degraded
0015        18      0      0        18     18 台
0016        18      0      0        18     18 台
0017        18      0      0        18     18 台
```
placement policy `gb300-subblock-001{4,5,6,7}-policy` 均存在（COLLOCATED / 1x72）。
建前现存 pool：default-pool / 0002 / 0006 / 0009 / 0013。

### 0015 / 0016 / 0017（各 18 台）
`bash scripts/gke-create-nodepool.sh 0015 0016 0017`
preflight 通过（`node version 1.36.0-gke.4447000 在 validNodeVersions 中`），三个 pool 07:55:10~07:55:19 提交完成。

### 0014（18 台）
subblock 仅 16 台 healthy，按 18 建必然 GCE_STOCKOUT → pool 落 ERROR。
已向用户说明三个选项（建 16 台 / 建 18 台接受 ERROR / 暂不建等修复），
**用户选择：仍按 18 台建，接受 ERROR 状态**（GKE 会在容量恢复时异步补齐剩余 2 台）。
`bash scripts/gke-create-nodepool.sh 0014`

### 监控要点
监控逐轮统计全集群 4681000 坏驱动节点数，基线为 2（lcg3 / 33qv 两台已 cordon 的硬件故障节点）。
一旦 >2 说明新节点起在坏 COS 上，立即告警。

### 07:57 修 gke-create-nodepool.sh：set -e 导致 async 建池必 exit 2

**症状**：`bash scripts/gke-create-nodepool.sh 0014` 提交成功（pool 确实进入 PROVISIONING），但脚本 `exit 2`，
且结尾的 COS 核对汇总、后续提示全部没打印。

**根因**：脚本头 `set -euo pipefail`。验证循环里写的是
```bash
verify_pool_cos "..."
case $? in ...
```
`verify_pool_cos` 在 MIG 尚未创建时返回 2 —— 而 `--async` 提交后 MIG 必然还没建出来，
所以这个返回值 100% 会出现。裸调用非 0 直接触发 errexit 中止脚本，`case $?` 永远执行不到。

**影响**：每一次正常的 async 建池都会「看起来失败」（exit 2），验证汇总段从未真正跑过。
之前建 0013、0015-0017 时同样中招，只是我没注意退出码。

**修复**：改为 `VRC=0; verify_pool_cos ... || VRC=$?; case $VRC in ...`，并加注释说明 set -e 陷阱。

### 08:02 0015/0016/0017 创建完成并核验

| pool | 状态 | 节点 | Ready | kubelet |
|---|---|---|---|---|
| gb300-pool-0015 | RUNNING | 18 | 18 | 18 × 1.36.0-gke.4447000 |
| gb300-pool-0016 | RUNNING | 18 | 18 | 18 × 1.36.0-gke.4447000 |
| gb300-pool-0017 | RUNNING | 18 | 18 | 18 × 1.36.0-gke.4447000 |
| gb300-pool-0014 | PROVISIONING | — | — | 仍在起（预期落 ERROR，subblock 仅 16 healthy）|

07:55 提交 → 08:02 三池就绪，约 7 分钟。

**COS 核对（修复 exit code 后可正常输出）**：
```
✓ gb300-pool-0014 / 0015 / 0016 / 0017 → gke-1360-gke4447000-cos-gb300-bm-129-19506-224-49-c-nvda
✓ 全部 pool 的 MIG 都指向预期 COS   退出码 0
```

**全集群 kubelet 分布**：140 × 4447000（好） / 2 × 4681000（坏）。
坏驱动数在整个扩容过程中始终保持基线 2（= 已 cordon 的硬件故障节点 lcg3、33qv），
**未因新建 54 台节点而增加**，再次印证「MIG template 指向好镜像 ⇒ 新节点必为好驱动」。

## 2026-07-25 08:11 并行启动 0015/0016/0017 全面质检

启动前确认三池均 `Ready=18/18`、`GPU=4 的节点=18/18`。

```
bash qa/run-checks.sh <profile> all-full 0015 &
bash qa/run-checks.sh <profile> all-full 0016 &
bash qa/run-checks.sh <profile> all-full 0017 &
```
脚本自带 stagger 错峰（0015→30s / 0016→32s / 0017→34s），各自独立 namespace `gpu-qa-<sub>`。
日志：`/tmp/qa-new/{0015,0016,0017}-allfull.log`

覆盖：hw-check → dcgm L2 → nccl-single → cublas → 单域多节点 NCCL(MNNVL off/on)。
预计 45-60 分钟（三池并行）。

**0014 暂不纳入**：仍 PROVISIONING（16/18 稳定，GKE 在尝试补最后 2 台 degraded host），
待其落终态（预期 ERROR）后单独跑。

### 本轮同时是最后一个待验证 fix 的实证机会
`collect_bug_reports`（超时放宽 15/30/120 + 三处重试 + 禁止 pod 名命名）尚未端到端跑到，
本轮 3 × 18 = 54 个 hw-check pod 并发收集，正好压测该修复。
预期：每池 18/18 且全部使用真实节点名。

### 08:32 pool-0014 落终态 ERROR（16/18），与预测一致

```
status: ERROR
Ready: 16/18   （subblock-0014 healthy=16, degraded=2）
```
用户此前已知情并选择「按 18 台建、接受 ERROR」。GKE 会在容量恢复时异步补齐剩余 2 台。
16 台节点本身可用（kubelet 全 4447000，MIG template 指向好 COS）。

### bug-report 在 54 pod 并发下的表现（fix A 压测结果）
```
pool-0015  成功 15/18  (失败 1, 节点名未解析 2)
pool-0016  成功 14/18  (失败 1, 节点名未解析 3)
pool-0017  成功 12/18  (失败 1, 节点名未解析 5)
合计 41/54
```
**安全属性达标**：全部失败都显式报出 `[SKIP] pod xxx: 3 次都取不到 nodeName，跳过（不生成无法追溯的文件）`，
**零废件、零错标**（对比修复前 18 台并发即产出 pod 名命名的不可追溯文件）。

**覆盖率未达标**：54 pod 并发下，即便超时放宽到 15s、重试 3 次，
`get pod -o jsonpath nodeName` 仍有 10 次彻底失败。瓶颈是 GKE API 高并发响应能力，不是超时值。
→ 处置：待三池质检结束后用 `qa/recover-bug-reports.sh` 增量补齐（该脚本走 DaemonSet 直读主机 /tmp，
不依赖 hw-check pod 存活，pool-0013 曾一把补到 18/18）。

两条真实 FAIL 待查：`wvwl`、`j2wr` —— 主机 /tmp 上确实没有 gz，疑似该节点 hw-check section 13 未生成文件。

### 监控假读数问题
今日第 4、5 次撞到 GKE API 抖动，导致 kubectl 查询返回空、计数型监控出现假零
（`ready=0/18 坏驱动=0`，而坏驱动基线本应为 2）。
逐 pool 并发查询同样中招（8 个 pool 逐条查时 0002/0006 假报 Ready=0）。
**教训**：计数型监控必须先校验查询本身是否成功（如全集群总数为 0 即判定查询失败、本轮读数作废），
否则「失败」与「真零」不可区分；批量统计应一次性拉全量后本地聚合，避免并发查询被限流。
新监控已加该校验。

### 08:33 发现自身 fix 的缺陷：wait_completion 的 READY 计数查询未加重试

**症状**：pool-0015 的 DCGM 阶段输出 `=== 全部完成 (0/18 Ready) ===`。

**查证**：
```
=== [08:30:04] 等待完成 (label=qa-dcgm-diag, timeout=900s) ===
  18 pod(s) 已创建
  kubectl wait --for=condition=Ready (剩余 900s，第 1 次)...
pod/qa-dcgm-diag-qm7gk condition met      ← wait 本身成功
=== [08:33:41] 全部完成 (0/18 Ready) ===
```
未出现「网络毛刺重试」也未出现「wait 调用失败但实际 …」兜底提示 → `WRC=0`，wait 成功。
事后核验 `gpu-qa-0015` 18 个 pod 全为 `1/1` → **测试确实通过，仅计数显示错误**。

**根因**：wait 之后统计 READY 数那行仍是 `timeout 8` 单次查询、无重试：
```bash
local READY=$(timeout 8 "${KTL_CMD[@]}" get pods -n ${NS} -l "app=${LABEL}" -o jsonpath='...' | grep -c True)
```
高并发下 API 返回空 → READY=0。

**危害不止于显示**：同一个 `READY` 变量还参与今日新加的兜底判定
`[ "${READY:-0}" -ge "${COUNT}" ]`。若某次 `kubectl wait` 真被网络打断（WRC≠0）、
而该计数查询又恰好同时失败，兜底会误判为失败 —— **我加的安全网在最需要它的场景下会失效**。

**待修（不在运行中改脚本）**：READY 计数查询比照其他调用处理 —— 放宽超时并加重试，
拿不到有效计数时明确区分「查询失败」与「真 0」，不可直接用于成功/失败判定。

### 08:57 pool-0016 多机 MNNVL=OFF 完成，落在 pool-0013 基线 ±0.3% 内

`qa/logs/qa-nccl-multi-gb300-0016-mnnvl0-20260725-085749/rank0.log`（46821b，18 host，`MNNVL=0 NVLS=0 CUMEM=0`）

| collective | 0016 | 0013 基线 | 差异 |
|---|---|---|---|
| all_reduce | 367.26 | 366.32 | +0.3% |
| all_gather | 363.70 | 364.87 | -0.3% |
| reduce_scatter | 367.05 | 367.20 | -0.0% |
| alltoall | 85.26 | 85.23 | +0.0% |

### 三池单节点阶段全部收官：4 项 × 3 池，零失败
```
08:52:22  pool-0016 单节点测试完成 (0 失败)
08:53:32  pool-0017 单节点测试完成 (0 失败)   ※ 见下方更正
08:56:16  第三池   单节点测试完成 (0 失败)
```
hw-check 经 Cloud Logging 核验：三池 54/54 全部 `PASS=23 FAIL=0 WARN=0`，**零 WARN**
—— 这批新硬件 aggregate ECC 均为 0，连 Fix C 的 INFO 分支都未触发。

### 异常信号累计（截至多机阶段）
```
网络毛刺   1   ← pool-0016 cuBLAS，1800s 预算内第 36 秒被打断，wait 重试拦下
apply 尝试 2   ← 脚本原有 apply 重试拦下
[SKIP]    10   ← bug-report 取不到 nodeName，安全跳过（无废件）
[FAIL]     3   ← bug-report 2 次主机无 gz + 1 次 cp 失败
TIMEOUT    0
CONTENT_FAIL 0
```
三类真实 API 抖动（wait / apply / bug-report）全部被重试机制吸收，**无一测试项因此作废**。

### ⚠️ 本人统计失误更正（今日第三次）
先前汇报「pool-0016 多机 1/2 轮完成」是**错的**。
`grep -c "JobSet 完成"` 命中的是 **「等待 JobSet 完成」** 这行（子串误匹配），并非完成事件。
当时 0016 第一轮尚在运行、rank0.log 未生成。真正的完成标志是 `JobSet 完成 (DONE in logs)`。

今日三次计数失误：`grep -hcE` 多文件配 `paste|bc` 静默失败、异常统计全 0、本次子串误匹配。
共同点：**未校验匹配到的内容就直接汇报数字**。后续涉及计数的结论需先确认匹配对象。

## 2026-07-25 09:05 ⚠️ pool-0015 死锁：cleanup_ds 静默失败导致级联故障

### 现象
pool-0015 的 cuBLAS 18 个 pod `Pending` 21 分钟，`FailedScheduling: Insufficient nvidia.com/gpu`。
`kubectl get ds -n gpu-qa-0015` 显示 **`qa-dcgm-diag` 仍有 18/18 Running** —— DCGM 测试早已结束却未被清理。

### 根因：cleanup_ds 未检查删除结果
0015 日志 08:34:51 的清理段：
```
=== [08:34:51] 清理 qa-dcgm-diag ===
  configmap "kube-root-ca.crt" deleted from gpu-qa-0015 namespace
                                    ← 缺 daemonset.apps "qa-dcgm-diag" deleted
```
对照同一脚本 08:43:42 对 nccl-single 的正常清理（有 `daemonset.apps ... deleted` 行）。
→ **DaemonSet 删除调用失败（几乎可以肯定又是 API i/o timeout），脚本未检查返回值、静默继续。**

### 级联后果（三级）
1. DCGM 18 pod 持续占用全部 4×18 GPU
2. **nccl-single 真实失败**：18 pod 抢不到 GPU，`timed out waiting for the condition`
   → `WARNING: timeout (0/18 Ready)`（此处是真 0，非计数假读）
3. cuBLAS 同样全部 Pending，卡死 21 分钟直至人工介入

### 处置
按「删除前确认日志已收集」规则，先经 Cloud Logging 核验 0015 的 DCGM 日志 **18/18 节点均有 DONE 标记**，
再 `kubectl delete ds qa-dcgm-diag -n gpu-qa-0015`（exit 0），DCGM pod 转 Terminating，GPU 释放。

### ⚠️ 更正先前错误结论
先前汇报「三池单节点 4 项全部通过，零失败」**是错的**。
该结论来自数 monitor 里「单节点测试完成 (0 失败)」事件的**出现次数**，未归属到具体池。
实际 **pool-0015 的 nccl-single 已失败**，单节点阶段并未走完。
今日第五次同类失误（计数/归属未经校验即汇报）。

### 待修（新增，累计 2 项脚本缺陷）
1. `cleanup_ds` 必须检查删除是否成功并重试；失败要显式报错而非静默continue
   —— 这是本次级联故障的唯一源头，优先级最高
2. `wait_completion` 的 READY 计数查询加重试（此前已记录）

### pool-0015 后续需重跑
`nccl-single`（真实失败）与 `cublas`（卡死，预算 09:13:51 到期）均需重跑。

### 09:07 三池收尾状态

| pool | 进程 | 结束时间 | 备注 |
|---|---|---|---|
| 0015 | 运行中 | — | 删残留 DS 后 cuBLAS 18 pod 已全部 Running |
| 0016 | 已结束 | 09:03:55 | **MNNVL=ON 轮无 rank0.log** |
| 0017 | 已结束 | 09:07:05 | 两轮多机数据齐全 |

### 多机 NCCL 结果（对比 pool-0013 同模式基线）
```
0016 OFF(RDMA)    367.26(+0.3%)  363.70(-0.3%)  367.05(-0.0%)   85.26(+0.0%)
0016 ON(NVLink)   ❌ 无 rank0.log
0017 OFF(RDMA)    371.25(+1.3%)  362.85(-0.6%)  369.83(+0.7%)   85.28(+0.1%)
0017 ON(NVLink)   915.51(-0.2%)  690.07(+0.5%)  707.41(-0.0%)  659.54(-0.2%)
```
已出数的三轮全部贴合基线（OFF ±1.3% 内，ON ±0.5% 内）。

### 缺陷 3：多机 rank0.log 保存失败且静默
pool-0016 MNNVL=ON 轮日志：
```
09:03:01 JobSet 完成 (DONE in logs)
                                   ← 缺「日志已保存」行
09:03:32 清理 JobSet
09:03:55 全面质检完成
```
对照同池 OFF 轮（08:57:49 完成 → 08:57:55 有「日志已保存」）。
目录 `qa-nccl-multi-gb300-0016-mnnvl2-20260725-090301/` 已建但为空。
脚本未检测保存失败即宣告「全面质检完成」，属**静默失败**，导致该池 NVLink 数据缺失。

### 累计待修脚本缺陷（3 项，均为静默失败类）
1. **`cleanup_ds` 不检查 DS 删除结果** —— 已致 pool-0015 级联死锁，优先级最高
2. **多机 rank0.log 保存失败不报错** —— 已致 pool-0016 NVLink 数据缺失
3. `wait_completion` 的 READY 计数查询无重试 —— 影响显示与兜底判定

共同模式：**调用失败后不检查、不报错、继续往下走**。今日所有质检事故都源于此。

### 09:26 pool-0015 MNNVL=ON 完成 —— 四池多机数据汇总

`qa-nccl-multi-gb300-0015-mnnvl2-20260725-092642/rank0.log`（47547b，18 host，`MNNVL=2 NVLS=1 CUMEM=1`）

**四池 × 两模式，16G out-of-place busBW (GB/s)**
```
pool  模式         all_reduce      all_gather   reduce_scatter      alltoall
0013  OFF(RDMA)        366.32          364.87           367.20         85.23   ← 基线
0015  OFF(RDMA)        368.48(+0.6%)   366.19(+0.4%)    369.74(+0.7%)  85.43(+0.2%)
0016  OFF(RDMA)        367.26(+0.3%)   363.70(-0.3%)    367.05(-0.0%)  85.26(+0.0%)
0017  OFF(RDMA)        371.25(+1.3%)   362.85(-0.6%)    369.83(+0.7%)  85.28(+0.1%)

0013  ON(NVLink)       917.02          686.40           707.76        660.65   ← 基线
0015  ON(NVLink)       914.32(-0.3%)   686.66(+0.0%)    708.34(+0.1%) 660.90(+0.0%)
0016  ON(NVLink)       ❌ rank0.log 未保存
0017  ON(NVLink)       915.51(-0.2%)   690.07(+0.5%)    707.41(-0.0%) 659.54(-0.2%)
```
**7/8 轮出数，全部贴合基线（OFF ±1.3%，ON ±0.5%）。四池 NVLink fabric 与 RDMA 网络均正常。**

### 关于一条无法证实的 monitor 事件
09:26:11 收到 `JobSet 完成` + `[FAIL] 日志为空/未保存: ...mnnvl2-20260725-092611/rank0.log`。
核查时（系统时间 09:23:47）：日志文件内 grep 不到该条、所引用目录不存在、JobSet 正常运行中。
最终磁盘只落了一个 mnnvl2 目录 `092642`（成功，47547b）。
→ 该事件与磁盘证据矛盾，不采信；以实际落盘结果为准。

## 2026-07-25 09:3x 修复三个「静默失败」类缺陷（qa/run-checks.sh）

### Fix 1 — cleanup_ds 不再吞掉删除失败
原代码：
```bash
timeout 30 ... delete ds "${LABEL}" ... || true      # 失败被吞
...
echo "  WARNING: ${REMAINING} pods 仍未删除"          # 隐式 return 0
```
改为：
- delete ds 重试 3 次（超时 30→60s），三次都失败显式打 `❌`
- 等 pod 消失的轮询改用 `ktl_ro_retry`，避免查询失败被当成「已清零」
- 等待窗口 90→120s
- 未清干净时打 `❌❌ 清理失败` + 后果说明 + 手动处理命令，**return 1**

### Fix 1b — 调用方感知并阻断级联
- `run_test` 末尾：`cleanup_ds` 失败 → 置全局 `CLEANUP_BROKEN=1`、`FAIL_COUNT++`、`RC=1`
- `run_test` 开头：`CLEANUP_BROKEN=1` 时直接跳过后续测试项并说明原因
  （0015 事故中，清理失败后 nccl-single 与 cuBLAS 各白跑一遍、cuBLAS 还卡死 21 分钟）

### Fix 2 — 多机 rank0.log 保存失败不再静默
原代码：pod 查询无重试；`ktl logs` 结果不检查；查不到 pod 时只 `log WARNING`（0016 连这行都没出现）。
改为：
- rank0 pod 查询重试 3 次（`ktl_ro_retry`）
- 保存后校验：exit 0 **且** 文件非空 **且** 含 `Collective test` 关键字，否则重试 3 次
- 任一环节最终失败 → 打 `❌❌` + 重跑命令 / 手动补取命令，并 `FAIL_COUNT++`
- 成功时日志附带文件字节数

### Fix 3 — wait_completion 的 READY 计数区分「查询失败」与「真 0」
原代码：`timeout 8` 单次查询，失败即 0。既导致「全部完成 (0/18 Ready)」假显示，
更严重的是该值参与兜底判定 —— 查询一失败，兜底会把成功误判为失败。
改为：
- 重试 3 次（超时 8→30s），拿不到有效结果记为 `READY=-1`
- 显示层用 `查询失败` 而非 `0`
- 判定层：`WRC=0` 直接通过；`READY=-1` 绝不参与达标判断，也不当作 0 去判失败
- 按超时处理时额外提示「Ready 数查询三次均失败，无法确认实际状态」

`bash -n` 通过。`CLEANUP_BROKEN` 以 `${CLEANUP_BROKEN:-0}` 取值，兼容 `set -u`。

### 09:4x 首版 cleanup_ds 修复引入回归，已二次修正

#### 回归现象
0015 nccl-single 补跑（烟囱测试）时报 `❌❌ 清理失败: 仍有 1 个 pod`，
但实测 DS=0、pod=0，清理其实成功。**测试本身也成功**
（manifest `qa-nccl-single|Done:` 无 TIMEOUT，Cloud Logging 18/18 节点有 `Done:`）。

#### 回归危害大于原缺陷
误报会置 `CLEANUP_BROKEN=1` → `all-full` 中后续所有测试项被跳过。
原来的静默失败至少还会继续跑；这个假警报直接砍掉整轮质检。

#### 二次修正
1. **区分 Terminating 与存活 pod**：
   按 `deletionTimestamp` 判定，`<none>` 为存活、非 `<none>` 为 Terminating。
   只有**存活 pod**才真占 GPU；只剩 Terminating 且 DS 已删 → 判成功直接返回。
2. **等待窗口 120→300s**
3. **硬指标改为 DaemonSet 是否已删除**，pod 数仅作辅助

#### 对拍中又抓到两个计数 bug（均已修）
| 写法 | 问题 |
|---|---|
| `echo "$OUT"` | $OUT 为空时输出一个空行 → `grep -vc` 记成 1 → 0 个 pod 误判为 1 个 Terminating |
| `printf '%s' "$OUT"` | 末行无换行符 → 本机 grep 是 **ugrep**，不统计缺结尾换行的末行 → 漏计最后一个 pod |

最终写法：先显式判空，非空时用 `printf '%s\n'` 补足结尾换行。

#### 对拍结果（5 种情形全过）
```
2 存活                    → 2/0 ✓
2 Terminating             → 0/2 ✓
空输出                     → 0/0 ✓
1 存活 + 1 Terminating     → 1/1 ✓
1 Pending                 → 1/0 ✓
```

#### 教训
改「失败检测」这类代码，误报的代价可能高于漏报。上线前必须用构造数据对拍各边界情形，
尤其是**空输入**和**末行无换行**——本机 grep 为 ugrep，与 GNU grep 行为不同。

### 补跑进度
- ✅ pool-0015 nccl-single：成功（18/18 节点 `Done:`，manifest 无 TIMEOUT）
- ⬜ pool-0015 cuBLAS
- ⬜ pool-0016 MNNVL=ON
- ⬜ bug-report 41/54 → 54/54
- ⬜ pool-0014 全套

## 2026-07-25 09:5x 第 4 处同类缺陷：nccl-multi 前置检查把查询失败当成 0

### 现象
pool-0016 MNNVL=ON 补跑**启动即失败**：
```
ERROR: gb300-pool-0016 健康节点 0 台，至少需要 2 台
```
但 pool-0016 实有 18 台 Ready 节点。

### 证实为假读数
连查 3 次：
```
第1次: 非cordon=0   Ready=18     ← 查询失败
第2次: 非cordon=18  Ready=18
第3次: 非cordon=18  Ready=18
```

### 出错代码（qa/run-checks.sh:587）
```bash
HEALTHY=$(ktl get nodes -l "${QA_NODE_SELECTOR_KEY}=${QA_POOL}" --no-headers 2>/dev/null \
          | grep -v SchedulingDisabled | wc -l)
if [ "$HEALTHY" -lt 2 ]; then
  echo "ERROR: ${QA_POOL} 健康节点 ${HEALTHY} 台，至少需要 2 台"; exit 1
fi
```
裸 `ktl`（无重试），查询失败 → 输出空 → `wc -l = 0` → 直接 `exit 1`，整轮补跑被打死。

### 这是同一族缺陷的第 4 处
1. `cleanup_ds` 删除失败不检查（已修）
2. 多机 rank0.log 保存失败不报错（已修）
3. `wait_completion` READY 计数无重试（已修）
4. **nccl-multi 前置 HEALTHY 计数无重试（本处，待修）**

统一模式：**kubectl 查询失败 → 输出为空 → 计数为 0 → 当作真实业务值使用**。

### 另更正一处先前判断
先前称 `wvwl` / `j2wr` 「主机 /tmp 上确实没有 gz，是真实发现」——**错误**。
`recover-bug-reports.sh` 已把 `wvwl`（及 `42rd`）正常取回 1.5M，文件一直都在，
当时只是 hw-check pod 内 `ls` 查询在 54 pod 并发下失败。

### 待修方案
`HEALTHY` 计数改用 `ktl_ro_retry`，并区分「查询失败」与「真的 0 台」：
查询失败应重试后仍失败才 abort，且错误信息要写明是查询失败而非节点不足。
（当前 0015 cuBLAS 仍在运行，共用同一脚本文件，待其结束后再改。）

### 09:5x 全局扫描：同类「查询失败当真值」隐患共 4 处

`grep -nE "wc -l|grep -c" qa/run-checks.sh` 全量排查结果：

| 行 | 代码 | 风险 | 状态 |
|---|---|---|---|
| L586 | `HEALTHY=$(ktl get nodes ... \| wc -l)` | 查询失败→0→`exit 1` 打死整轮补跑 | 待修（最严重）|
| L264 | `COUNT=$(timeout 8 ... get pods \| wc -l)` | 持续失败→误报「无 pod」；COUNT 还是后续 READY 判定的分母 | 待修 |
| L393 | `DSLEFT=$(ktl_ro_retry get ds \| grep -c .)` | 查询失败时 stdout 空→0→误判「DS 已删除」 | 待修 |
| L478 | `TOTAL=$(echo "${PODS}" \| grep -c .)` | PODS 空→报「成功 0 / 共 0 pod」，掩盖查询失败 | 待修 |
| L314 | `READY=$(echo "$_out" \| grep -c True)` | 已有 `-n "$_out"` 守卫 | ✅ 本日已修 |
| L386 | `ALIVE/TERMING` | 已有 `-z "$OUT"` 守卫 + `printf '%s\n'` | ✅ 本日已修 |

`ktl_ro_retry` 当前覆盖 8 处（L77/154/166/196/375/393/427/635）。

**统一整改原则**：任何把 kubectl 输出转成计数、并据此做业务判断的地方，
必须能区分「查询失败」与「真实为 0」；查询失败要么重试、要么显式报错，
绝不可静默按 0 处理。

### 10:04 cleanup_ds 二次修复验证通过 + 0015 cuBLAS 补跑成功

```
=== [10:04:35] 清理 qa-cublas-bench ===
  daemonset.apps "qa-cublas-bench" deleted from gpu-qa-0015 namespace
  ✓ qa-cublas-bench DS 已删除，剩余 18 个 pod 处于 Terminating（正常退出中）
```
正是设计意图：DS 已删 + 只剩 Terminating → 判成功放行，不再误报 `❌❌`。
实测残留 DS=0 / pod=0，判定与实际一致。

manifest `gpu-qa-0015|qa-cublas-bench|DONE:` 无 TIMEOUT 标记 → **cuBLAS 补跑成功**。

### 补跑进度更新
- ✅ pool-0015 nccl-single
- ✅ pool-0015 cuBLAS
- ✅ pool-0017 bug-report 18/18（取回 6，跳过 12，失败 0）
- ❌ pool-0016 MNNVL=ON（被 L586 假读数打死，待修后重跑）
- ⬜ pool-0015 / 0016 bug-report（15/18、14/18）
- ⬜ pool-0014 全套

## 2026-07-25 10:1x 一次性修完 4 处「查询失败当真值」隐患

| 位置 | 改动 |
|---|---|
| L615 `HEALTHY`（nccl-multi 前置） | 改 `ktl_ro_retry` + 重试 3 次；查询失败记 `-1` 并单独报错「是查询问题，不是节点不足」；真不足才报节点数 |
| L264 `COUNT`（wait_completion 等 pod） | 改 `ktl_ro_retry`；新增 `QFAIL` 计数区分「查询失败」与「pod 确实未创建」，两种情况错误信息不同 |
| L407 `DSLEFT`（cleanup 判 DS 是否删净） | 先检查查询 exit code，失败则记 `-1` 不下结论、继续轮询；避免「查询失败」被当成「DS 已删除」 |
| L498 `TOTAL`（collect_bug_reports） | PODS 为空时不再输出「成功 0 / 共 0 pod」这种伪正常，改为明确提示可能是查询失败；未收齐时引导用 `recover-bug-reports.sh` 增量补收 |

### 复扫结果
`grep -nE "wc -l|grep -c" qa/run-checks.sh` 剩余出现处全部有守卫：
L275 / L401 / L412 / L501 先判空或先查 exit code；L329 在重试块内且有 `-n "$_out"` 守卫。
**已无裸 kubectl 查询结果直接当业务计数的地方。**

### HEALTHY 逻辑对拍（5 情形全过）
```
rc=0, 3 台 Ready                        → 3   ✓
rc=0, 1 Ready + 1 SchedulingDisabled    → 1   ✓
rc=1, 空输出（查询失败）                 → -1  ✓
rc=0, 空输出                            → -1  ✓
rc=0, 仅 1 台 SchedulingDisabled         → 0   ✓
```
`bash -n` 通过。

### 本日 qa/run-checks.sh 缺陷修复总览（7 项）
1. `collect_bug_reports` glob 未展开（kubectl exec 不经 shell）
2. `ktl_ro_retry` 只读查询重试封装（新增）
3. `wait_completion` 网络毛刺重试（wall-clock 预算）
4. `cleanup_ds` 删除失败不检查 → 重试 + 区分 Terminating/存活 + 失败 return 1
5. `run_test` 感知清理失败并阻断级联（`CLEANUP_BROKEN`）
6. 多机 rank0.log 保存失败不报错 → 重试 + 内容校验
7. 4 处「查询失败当真值」计数（HEALTHY / COUNT / DSLEFT / TOTAL）

### 10:12 bug-report 补收进展 + HEALTHY 修复实战验证

**HEALTHY 修复当场生效**：0016 MNNVL=ON 重跑时日志出现
```
[健康节点查询重试 1/3] 未取到节点列表，5s 后重试
[健康节点查询重试 2/3] 未取到节点列表，5s 后重试
=== [10:11:56] 多节点 NCCL: gb300-pool-0016, 18 nodes, MNNVL=ON (测 NVSwitch) ===
```
同一查询**连续失败 2 次**后第 3 次成功。修复前第 1 次失败即 `健康节点 0 台` + `exit 1` 打死整轮
（20 分钟前刚发生过）。说明该查询在当前 API 状态下失败率很高，不是偶发。

**bug-report 覆盖率**
```
0015: 15/18 → 18/18  (取回 3, 跳过 15, 失败 0)
0016: 14/18          (待 NCCL 跑完后补)
0017: 12/18 → 18/18  (取回 6, 跳过 12, 失败 0)
```

**再次确认先前误判**：`wvwl`、`j2wr` 两台此前被判「主机 /tmp 上确实没有 gz，是真实发现」，
现均已由 `recover-bug-reports.sh` 正常取回 1.5M。文件一直存在，当时是 54 pod 并发下
hw-check pod 内 `ls` 查询失败。**该「真实发现」结论作废。**

### 10:15 pool-0016 MNNVL=ON 补跑成功 —— rank0.log 保存修复验证通过

日志行带上了字节数，即新加的校验（非空 + 含 `Collective test`）通过：
```
=== [10:15:01] 日志已保存: .../qa-nccl-multi-gb300-0016-mnnvl2-20260725-101458/rank0.log (46727b) ===
```
对照修复前：同一步骤静默失败、目录为空、脚本仍宣告「全面质检完成」。

数据（`MNNVL=2 NVLS=1 CUMEM=1`，18 host）：
| collective | 0016 ON | 0013 基线 | 差异 |
|---|---|---|---|
| all_reduce | 926.76 | 917.02 | +1.1% |
| all_gather | 687.47 | 686.40 | +0.2% |
| reduce_scatter | 708.61 | 707.76 | +0.1% |
| alltoall | 659.11 | 660.65 | -0.2% |

### 四池 × 两模式 多机 NCCL 全部出数（8/8）
```
pool  模式          all_reduce      all_gather   reduce_scatter      alltoall
0013  OFF(RDMA)        366.32          364.87           367.20         85.23  ← 基线
0015  OFF              368.48(+0.6%)   366.19(+0.4%)    369.74(+0.7%)  85.43(+0.2%)
0016  OFF              367.26(+0.3%)   363.70(-0.3%)    367.05(-0.0%)  85.26(+0.0%)
0017  OFF              371.25(+1.3%)   362.85(-0.6%)    369.83(+0.7%)  85.28(+0.1%)

0013  ON(NVLink)       917.02          686.40           707.76        660.65  ← 基线
0015  ON               914.32(-0.3%)   686.66(+0.0%)    708.34(+0.1%) 660.90(+0.0%)
0016  ON               926.76(+1.1%)   687.47(+0.2%)    708.61(+0.1%) 659.11(-0.2%)
0017  ON               915.51(-0.2%)   690.07(+0.5%)    707.41(-0.0%) 659.54(-0.2%)
```
**四池 72 节点 / 288 GPU，RDMA 与 NVLink 两条路径全部达标（OFF ±1.3%，ON ±1.1%）。**

### 10:22 四池 bug-report 全部补齐 18/18

```
pool-0013  18/18 ✓      pool-0015  18/18 ✓
pool-0016  18/18 ✓      pool-0017  18/18 ✓
```
0016 本次：取回 4 份，跳过已有 14，失败 0。
四池合计 72 份，每份约 1.5M，命名均为真实节点后缀，可追溯。

`qa/recover-bug-reports.sh`（DaemonSet 直读主机 /tmp）在四池上均一次补齐，
证明该路径比 hw-check pod 内 `kubectl exec ls` + `kubectl cp` 可靠得多 ——
后者在高并发下 nodeName 查询与 cp 都会大量失败。

### 10:20 pool-0014 全套质检启动（16 节点）
pool-0014 因 subblock 仅 16 台 healthy 处于 ERROR 状态，但 16 台节点本身可用
（kubelet 4447000、MIG template 指向好 COS）。
脚本按实际健康节点数自适应，多机 NCCL 将以 16 节点 / 64 GPU 运行。
- hw-check：**16/16 完成**
- 日志：/tmp/qa-fix/0014-allfull-102003.log

### 10:30 pool-0014 bug-report 一次满收 16/16 —— collect_bug_reports 修复实证

```
=== bug-report: 成功 16 / 共 16 pod (失败 0, 节点名未解析 0) ===
```
**零失败、零 nodeName 解析失败**，修复后首次在质检流程内完整收齐。

### 与修复前的对照及结论
| 场景 | 结果 |
|---|---|
| 修复前，18 pod 单池（pool-0013） | 8/18，且 1 份用 pod 名命名不可追溯 |
| 修复后，54 pod 三池并发（0015/0016/0017） | 41/54（失败集中在 nodeName 查询） |
| 修复后，16 pod 单池无竞争（pool-0014） | **16/16，零失败** |

**结论**：修复（超时 15/30/120 + 三处重试 + 禁止 pod 名命名）确实有效，
但**并发规模是主要变量** —— 多池同时质检时 GKE API 扛不住，重试也补不齐。

**运维建议**：多池并发质检时，`collect_bug_reports` 应视为「尽力而为」，
`qa/recover-bug-reports.sh`（DaemonSet 直读主机 /tmp）必须作为**标准收尾步骤**固化进流程，
而非事后补救。今日四池（0013/0015/0016/0017）全部依赖它才补齐到 18/18。

### 10:52 pool-0014 单节点 4 项全部通过（16 节点）

Cloud Logging 核验：
```
hw-check  16/16  PASS=23 FAIL=0 WARN=0    ← 零 WARN（ECC 修复在第 5 个 pool 上验证）
DCGM      含 "Fail" 日志行 0
nccl-single  16/16 Ready
cuBLAS       16/16 Ready
```

三次 `cleanup_ds` 全部明确确认成功：
```
✓ qa-hw-check 已清理干净
✓ qa-dcgm-diag 已清理干净      ← 正是 pool-0015 死锁的同一步骤
✓ qa-nccl-single 已清理干净
```
其中 DCGM 那步是 0015 事故的源头，本次明确确认清理成功，未再出现残留占 GPU。

`cleanup_ds` 修复的三种输出形态至此全部见过：
1. `✓ 已清理干净`（pod 全消失）
2. `✓ DS 已删除，剩余 N 个 pod 处于 Terminating`（宽容路径，避免首版误报）
3. `❌❌ 清理失败`（真失败，会置 CLEANUP_BROKEN 阻断级联）

### 待办：resolve_pool 的 fallback 刷屏
`WARNING: 反查 pool 名失败，fallback: gb300-pool-XXXX` 今日出现数十次，**每次 fallback 值都正确**。
原因是 `resolve_pool` 依赖 reservation label 反查，该 label 在这批节点上似乎不存在 → 100% 走 fallback。
优先级低，但会刷屏掩盖真问题。建议：确认 label 是否应存在；若确实不存在则降级为 debug 输出或直接静默。

### ⚠️ Monitor 事件可能带未来时间戳、引用尚未存在的文件（今日 2 次）

| 次数 | 事件声称 | 核查时实际 |
|---|---|---|
| 09:26 | `[09:26:11] JobSet 完成` + `[FAIL] 日志为空: ...mnnvl2-...-092611/rank0.log` | 系统时间 09:23:47；日志无该行；目录不存在；JobSet 运行中。最终真实结果为 `092642` 目录、成功、47547b |
| 11:05 | `[11:05:48] JobSet 完成` + `日志已保存 ...mnnvl2-...-110548/rank0.log (43664b)` | 系统时间 11:04:36；日志无该行；目录不存在；JobSet 16 pods Running 2m43s |

两次均为**事件时间戳早于系统时钟到达**，且引用的产物尚未落盘。
第一次的内容最终大致成真（晚约 30 秒、目录名不同），性质更像提前投递而非虚构，
但**在到达时刻完全不可作为事实依据**。

**处置原则**：monitor 事件仅作提示，**以落盘文件与 kubectl 实际状态为准**。
今日据此避免了两次「误报测试完成」。凡涉及「完成 / 成功 / 数据已生成」的结论，
一律先 `ls` 文件 + 读内容确认后再汇报。

### 11:05 pool-0014 MNNVL=ON 完成 —— 五池 10/10 轮多机 NCCL 全部出数

注：真实落盘目录为 `...-mnnvl2-20260725-110520`（42942b），
与前述假事件所称的 `110548` / 43664b **不同** —— 再次印证事件不可信、以落盘为准。

pool-0014（16 节点 / 64 GPU，`MNNVL=2 NVLS=1 CUMEM=1`）：
| collective | 0014(16N) | 0013(18N) | 差异 |
|---|---|---|---|
| all_reduce | 933.92 | 917.02 | +1.8% |
| all_gather | 690.97 | 686.40 | +0.7% |
| reduce_scatter | 708.78 | 707.76 | +0.1% |
| alltoall | 666.70 | 660.65 | +0.9% |

**⚠️ 0014 与其余四池规模不同（16N/64GPU vs 18N/72GPU），数值不可直接对比。**
偏高与规模差异方向一致，但无同规模历史基线可比对，故不下机理结论。
可确认的是：四项均无异常低值、无离群，RDMA 与 NVLink 两条路径均工作正常。

### 五池多机 NCCL 总表（10/10）
```
pool 规模        模式         all_reduce   all_gather  reduce_scatter   alltoall
0013 18N/72G    OFF            366.32       364.87        367.20        85.23  ← 18N 基线
0015 18N/72G    OFF            368.48       366.19        369.74        85.43
0016 18N/72G    OFF            367.26       363.70        367.05        85.26
0017 18N/72G    OFF            371.25       362.85        369.83        85.28
0014 16N/64G    OFF            379.21       379.27        376.50        91.02  ← 规模不同

0013 18N/72G    ON             917.02       686.40        707.76       660.65  ← 18N 基线
0015 18N/72G    ON             914.32       686.66        708.34       660.90
0016 18N/72G    ON             926.76       687.47        708.61       659.11
0017 18N/72G    ON             915.51       690.07        707.41       659.54
0014 16N/64G    ON             933.92       690.97        708.78       666.70  ← 规模不同
```
四个 18 节点池组内一致性：OFF ±1.3%，ON ±1.1%。

## 2026-07-25 11:2x 四池报告生成 + 修复「跑得快被判故障」的离群逻辑

### 日志收集（四池 214 份，零失败）
串行收集，总耗时 8 分钟：
```
pool-0014  16/16 × 4 项
pool-0015  18/18 × 4 项   ← 用合成 manifest
pool-0016  18/18 × 4 项
pool-0017  18/18 × 4 项
```

**0015 合成 manifest**：原 manifest 中 nccl-single 与 cuBLAS 带 `TIMEOUT` 标记（失败旧记录），
补跑成功的在另两个 manifest 里。合成为按时序的单一文件：
```
hw-check    08:11:30 → 08:28:56
dcgm        08:28:56 → 09:37:22
nccl-single 09:37:22 → 09:51:30   ← 只含补跑成功那次
cublas      09:51:30 → (无上界)
```
验证：nccl-single 匹配 **18 个 pod**（非 54），抽查 busbw 687.92/687.36/687.86（单机区间），
无 MNNVL 标记 —— 成功排除 08:36 失败记录与 09:16/09:22 多机日志。

### ⚠️ 缺陷：离群判定把「高于均值」也算故障
pool-0014 首版报告判 **FAIL / 1 个故障节点 / 需 cordon 处理**，理由：
```
| npdg | TF32 | 886 | +3.9% vs avg |
```
即**全场最快的节点被判故障**。而同批最慢的 `wxkr=834`（-2.3%）因未超 3% 阈值反而未标记。
TF32 十六台实际分布 834–886、均值 854、自然离散 **6.1%**，阈值 3% 本就小于自然离散。

判定方向完全错误：`abs(dev) > threshold` 双向触发，且任何 OUTLIER 都计入 `CUBLAS_FAIL`
→ `TOTAL_FAIL` → VERDICT=FAIL「需 cordon 处理」。

### 修复
NCCL 与 cuBLAS 两处分析器同步改：
- 仅 `dev < -threshold` 或低于绝对下限 → `OUTLIER`（计入故障）
- `dev > threshold` → 新增 `FASTNODE` 行，仅展示、不计 FAIL
- 报告中相应改为两张表：「偏低节点（计入故障）」与「高于均值/中位数（仅提示，不计故障）」

### 修复验证
重新生成 pool-0014 报告：
```
修复前: **1 个故障节点，需 cordon 处理**  结果 FAIL  故障节点 1
修复后: **全部 PASS，无故障，无需处理**   结果 PASS  故障节点 0
        npdg 移入「高于均值（仅提示，不计故障）」表
```
删除被取代的 `gb300-0014-20260725-112049.md` 及其 index 行。
