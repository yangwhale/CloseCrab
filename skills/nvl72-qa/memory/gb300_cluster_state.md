---
name: gb300-cluster-state
description: GB300 GKE 集群当前状态：11 pool / 188 节点 / 6 cordoned，质检完成 2026-07-17
metadata: 
  node_type: memory
  type: project
  originSessionId: 1d46131b-e454-47aa-9615-b08009477d3c
---

## GKE 集群状态（2026-07-17）

集群: `gb300-gke-test` / GCP project `tencent-gcp-taiji-poc`

### Node Pool 分布

| Pool | Domain | 总节点 | 可调度 | Cordoned | 故障 |
|---|---|---|---|---|---|
| gb300-pool-0001 | d0001 | 18 | 18 | 0 | — |
| gb300-pool-0002 | d0002 | 18 | 16 | 2 | lcg3 GPU=3, 3r0c GPU0 内存错误 |
| gb300-pool-0003 | d0003 | 16 | 16 | 0 | — |
| gb300-pool-0004 | d0004 | 18 | 18 | 0 | — |
| gb300-pool-0005 | d0005 | 17 | 17 | 0 | — |
| gb300-pool-0006 | d0006 | 18 | 16 | 2 | 33qv GPU=3, qx2s RDMA 1 port down |
| gb300-pool-0008 | d0008 | 16 | 16 | 0 | — |
| gb300-pool-0009 | d0009 | 16 | 15 | 1 | 36wz GPU=3 |
| gb300-pool-0010 | d0010 | 17 | 17 | 0 | — |
| gb300-pool-0011 | d0011 | 17 | 17 | 0 | — |
| gb300-pool-0012 | d0012 | 18 | 17 | 1 | 5kw9 RDMA 1 port down |
| **合计** | | **189** | **183** | **6** | |

d0007 已创建（2026-07-17 质检通过）。nx8n (d0001) DRA kubelet-plugin 已就绪（quota 修复后）。

### Cordoned 节点

| 节点 | Domain | 故障 | physicalHost |
|---|---|---|---|
| lcg3 | d0002 | GPU=3 | `/f597b3d23d968584b8660cdbb324b5ab/e9e26a9c9da388db2f8a62e0ce5b1f3e/b2c34e05a5b13478164a1b15c2aaea8c` |
| 3r0c | d0002 | GPU0 内存错误 row remap pending | `/f597b3d23d968584b8660cdbb324b5ab/e9e26a9c9da388db2f8a62e0ce5b1f3e/03cea4e24a724bc0efe125b0d3572519` |
| 33qv | d0006 | GPU=3 | `/f597b3d23d968584b8660cdbb324b5ab/ee18edff617d7dfd650f1baa1eb6e73a/0b4c96338a55388b4c0b36a58172c3f5` |
| qx2s | d0006 | RDMA 1 port down | `/f597b3d23d968584b8660cdbb324b5ab/ee18edff617d7dfd650f1baa1eb6e73a/74a862afdc5a4a9ccfb55e930c089817` |
| 36wz | d0009 | GPU=3 | `/f597b3d23d968584b8660cdbb324b5ab/a5dd4eb244e534941b43e1e4255c04ba/d6290eeb0c628ae825a74b817e66f3d5` |
| 5kw9 | d0012 | RDMA 1 port down | `/f597b3d23d968584b8660cdbb324b5ab/fafad31160cb4a080f6f7c41f8ac151d/8cccabead3491b104b584fed744464ee` |

### 质检结论

全量质检 2026-07-16~17 完成，详见 `docs/gke-qa-report-v2.md`。

- 189 节点 × 4 项（hw-check/DCGM r2/NCCL/cuBLAS）全部测试，日志完整
- 200 健康 / 6 故障（3 GPU 缺失 + 1 GPU 内存错误 + 2 RDMA port down）
- 跨域 NCCL 6 对全部跑通（MNNVL=2, all_reduce 802-811 GB/s, 比 GB200 高 6-7%）
- 新增组件: JobSet v0.12.0 + cert-manager v1.17.2 + imex-channel-init DaemonSet
- DRA pod quota: 500（原 150，修复后 206 kubelet-plugin 全部 Ready）
- NCCL NVLink busBW: 全部 domain 均值 688~690 GB/s，无离群
- cuBLAS FP4: 全部 domain 均值 7890~7940 TFLOPS，无离群
- RDMA 跨节点测试（nccl-multi）待执行

**Why:** 跟踪集群可用容量和故障节点分布，影响训练任务调度。
**How to apply:** 部署训练任务前确认目标 domain 的可调度节点数和故障情况。
