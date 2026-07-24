---
name: gb300-nccl-benchmark-status
description: GB300 NCCL/cuBLAS benchmark 进展、JobSet 方案、3+ 节点 RDMA 问题根因
metadata:
  type: project
  originSessionId: 84c18ffc-fe2a-4786-a7c5-abc764542166
---

## 已验证的 NCCL 测试（16G busBW, GB/s）

| 拓扑 | GPU | all_reduce | all_gather | reduce_scatter | alltoall | 方式 | 日期 |
|---|---|---|---|---|---|---|---|
| 单机 NVLink P2P | 4 | 687 | 620 | 631 | 606 | MPIJob | 07-13 |
| 同域双机 MNNVL | 8 | 842 | 684 | 693 | 676 | JobSet | 07-14 |
| 跨域双机 RDMA | 8 | 330 | 193 | 193 | 43 | JobSet | 07-14 |
| 跨域 4 节点 RDMA | 16 | 653 | 384 | 385 | 47 | MPIJob | 07-13 |

07-13 的 4 节点数据在 DRA helm 重装前获取。07-14 DRA 重装后 3+ 节点 NCCL 全部失败。

## JobSet 方案（07-14 验证通过）

MPIJob launcher 有单点调度问题，大规模测试改用 JobSet（参考 GCP 官方 example）：
- `enableDNSHostnames: true` 自动 DNS 解析
- 所有 pod 平等，index 0 当 head node 跑 mpirun
- `--mca btl self,tcp --mca btl_tcp_if_include eth0` 限制 MPI TCP 只走 pod 网络
- `--mca plm_rsh_agent 'ssh -p 222'` 用 GIB daemon 的 sshd
- JobSet name ≤ 13 字符（FQDN 64 字符上限）
- 同域测试需要 per-test ComputeDomain（用完即删）
- 跨域测试不需要 CD
- 需要 warmup run 避免首个 collective crash

YAML 文件：
- `yamls/nccl-jobset-2node-same-domain.yaml`（name=nccl-2sd）
- `yamls/nccl-jobset-2node-cross-domain.yaml`（name=nccl-2cd）
- `yamls/nccl-jobset-4node-cross-domain.yaml`（name=nccl-4cd）

## RDMA NIC claim 必须 8 个

`rdma-nics-mpi-sd` 只 claim 4 NIC（gpu*rdma0），DRANET 只给 claim 的 NIC 配 IP。`UCX_NET_DEVICES` 列了全部 8 个 → 访问无 IP 的 NIC crash。改用 `rdma-nics-all-8`（YAML: `yamls/rdma-nics-all-8.yaml`）。

## NVLS 对 all_reduce 的影响

| | NVLS=0 | NVLS=1 |
|---|---|---|
| all_reduce | 703 | **842** (+20%) |
| 其他 collective | 不变 | 不变 |

同域 MNNVL 测试必须 NVLS=1 才能拿到最优 all_reduce。跨域测试 NVLS=0。

## 未解决：3+ 节点 NCCL RDMA 失败（07-14）

**现象**：DRA helm 重装（`helm uninstall` + `helm install` v0.4.1）后，3 节点及以上 NCCL 测试全部失败。2 节点稳定通过。

**错误**：`transport/net.cc -> 6`（ncclInternalError），然后 UDS proxy connection refused（级联故障）。

**已排除**：
- d0010 节点特定问题 → 全 d0009 也失败
- DRA claims 问题 → 无 DRA claims（privileged）也失败
- RDMA NIC 数量（4 vs 8）→ 都失败
- GIB 镜像版本 → diagnostic 和 non-diagnostic 同版本 plugin
- GID table changed → grep 0 次
- JobSet vs MPIJob → 都失败
- DRANET 状态 → 重启后也失败

**可能根因**：DRA helm 重装改变了 kubelet 对 GPU/RDMA 设备的资源管理方式（ResourceSlice 注册），影响了 privileged 容器中 NCCL GIB net plugin 的 RDMA QP 建链。07-13 的 4 节点成功是在 DRA rolling restart 前、helm 重装前。

**How to apply**：3+ 节点 NCCL 需提 GCP support ticket 或等 DRA driver 更新。当前可用的基线数据以 2 节点为准。

## 其他踩坑

- DRA 重启必须 DRANET + kubelet-plugin 一起，只重启一半会导致 RDMA prepare 异常
- ComputeDomain 必须 per-test 创建用完销毁，持久 CD 占节点标签阻塞新 CD
- /etc/hosts 被 guest-agent 覆盖导致 calico CrashLoopBackOff → setup-worker.sh ALWAYS 段已修复
- hzchen 节点已 cordon（避免干扰 NCCL 测试调度）

## 日志位置

```
logs/nccl-jobset-2node-same.log    # JobSet 同域 2 节点 MNNVL (NVLS=1)
logs/nccl-jobset-2node-cross.log   # JobSet 跨域 2 节点 RDMA
logs/nccl-final-2node-cross.log    # MPIJob 跨域 2 节点 (warmup=50 iters=100)
logs/nccl-final-4node-cross.log    # MPIJob 跨域 4 节点 (07-13, DRA 重装前)
logs/nccl-batch-single-node-full.log  # 44 台单机全量质检
logs/cublas-batch-full.log         # 44 台 cuBLAS GEMM
```
