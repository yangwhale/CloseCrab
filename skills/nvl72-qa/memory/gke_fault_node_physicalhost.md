---
name: gke-fault-node-physicalhost
description: 故障节点必须记录 physicalHost 路径（/block/subblock/host），用于重建 pool 后追踪同一台物理机
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1d46131b-e454-47aa-9615-b08009477d3c
---

确定故障的 GKE 节点，必须在质检报告中记录 physicalHost 信息。

**Why:** pool 重建后节点名变了，但 physicalHost 的 host ID 不变。如果同一台物理机反复出故障，需要这个信息来追踪和报修。

**How to apply:**
- 获取方式: `gcloud compute instances describe <node-name> --zone=us-central1-b --project=tencent-gcp-taiji-poc --format="value(resourceStatus.physicalHost)"`
- 也可以从 node labels 拿: `cloud.google.com/gce-topology-block`, `cloud.google.com/gce-topology-subblock`, `cloud.google.com/gce-topology-host`
- physicalHost 路径格式: `/block/subblock/host`
- 在 `docs/gke-qa-report.md` 的 cordon 节点表旁附上 physicalHost 表
- cordon 操作时同步获取并记录，不要事后补
- **block/subblock/host 的 hash 必须写完整，禁止用省略号截断**

[[gke-kubectl-auth]]
