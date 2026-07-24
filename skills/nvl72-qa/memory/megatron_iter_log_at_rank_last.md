---
name: megatron-iter-log-at-rank-last
description: Megatron print_rank_last 默认，多机训练的 iter timing log 在 pod-<N-1>（world_rank=world_size-1），不是 pod-0
metadata:
  type: project
---

Megatron-LM `print_rank_last` 默认将 iter progress 打到 world_rank == world_size-1 的进程。GKE 上 pod ordinal → node_rank → global_rank：pod-K 上的 local_rank j 是 global_rank = K*4 + j（每 pod 4 GPU）。所以 rank last 落在 **pod-`<replicas-1>`** 上。

**Why:** 多机 log 完整性判断如果沿用单机"看 pod-0"的直觉会漏 iter timing —— pod-0 只有 model init/memory/done marker，iter=0 是正常。首次 llama2-7b-8g benchmark 就因为脚本判 "每 pod 都要 20 iter" 报了误 WARN。

**How to apply:**
- 抓 megatron 训练性能数据（iter time / TFLOPs / MFU）时，**只需 pod-`<replicas-1>` log**
- log-completeness gate 只对 rank-last 校验 iter count == train_iters
- 其他 pod log 只需 `training done at` marker 存在（sleep infinity 已确保 pod 走到最后）
- 单机 config（replicas=1）rank-last = pod-0 = rank 0，直觉一致；容易只在多机时踩坑
