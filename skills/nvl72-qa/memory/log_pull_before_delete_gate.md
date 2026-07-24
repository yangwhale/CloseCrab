---
name: log-pull-before-delete-gate
description: 拉训练 log 到本地后必须做严格 gate（每 pod log 都拉到 + rank-last iter 完整），验证通过才 kubectl delete
metadata:
  type: feedback
---

benchmark workflow 的 cleanup 必须以 log 拉齐+完整校验作为 gate，any 一个 pod pull failed 都要 abort delete。参见 [[feedback-verify-before-delete]]。

**Why:** 2026-07-18 70b-64g 第 1 次跑完后 `kubectl logs` 循环遇到 GKE API server timeout，只 pod-0 抢到；随后盲 delete sts 导致 pod-15 (rank-last，含全 iter 数据) 永久丢失，被迫 re-run 30 min。用户明确要求"所有日志要完整拉下来（检查是否完整）"。

**How to apply:**
- log-pull 循环加 3-retry + 单 pod check（size > 0 + expected patterns）
- 循环结束后做**总 gate**：所有 pod file 存在 + rank-last iter count == train_iters + 所有 pod 都有 done marker
- gate 不通过 → **echo abort，绝对不 kubectl delete**；让用户手动介入
- Sleep infinity 让 pod 一直 alive，给 log-pull 无限重试窗口
