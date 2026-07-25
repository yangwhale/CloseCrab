---
name: fake-running-zombie-workload
description: 判断 pool/节点是否闲置时 pod 的 Running 状态不可信，必须交叉验证进程表+GPU利用率+日志时间戳
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 80c6268e-7a1b-4b81-994f-5797b6e32485
  modified: 2026-07-25T07:02:03.217Z
---

盘点闲置资源时，**`kubectl get pod` 的 `1/1 Running` 不能作为「有负载」的判据**。容器 entrypoint 普遍有 `sleep infinity` / `bash -c "...; sleep"` 兜底，主进程死了 pod 仍显示 Running、`restarts=0`、DRA ResourceClaim 保持 `allocated,reserved`。

**Why:** 2026-07-25 释放 pool 时，pool-0012 的 4 个 `vllm-v4pro-dp2ep8-*` pod 全部 `1/1 Running`、43h、0 restart、DRA claim 正常，据此判定「infer2 的活服务，不能删」。实际该 PD 分离服务在 42 小时前（07-23 09:31:40）就已失效，空占 12 颗 GPU 的 claim + 552 GB 显存。差一点因为看错状态而保留了整个 pool。

**How to apply:** 三个维度交叉验证，任一异常即为假活：

1. **容器内进程表** — `kubectl exec <pod> -c <container> -- ps -eo pid,etime,stat,comm --sort=-etime`
   只剩 `bash`(PID 1) + `sleep` = 主进程已退出。
2. **GPU 实际占用** — `kubectl exec <pod> -- nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader`
   显存 0 MiB = 模型未加载；显存满但 util 恒 0% = 加载了但无流量。
3. **日志末条时间戳** — `kubectl logs <pod> --timestamps | tail -1`
   停在数十小时前 = 死了；只有 `/health` `/metrics` 这类 k8s probe 和 Prometheus 抓取 = 零业务流量。

对 PD 分离 / 多组件服务，逐个组件查：本例 decode 侧进程退出后 router 全部请求 `Connection refused`，但 prefill 侧进程仍活着、模型仍在显存里，只看 prefill 会误判成服务正常。

另一种形态：**pod 卡 Terminating 但 kubelet 没走完清理**（`deletionTimestamp` 已过 `grace` 十几小时、无 finalizer），同样占着节点。用 `kubectl delete pod --all --force --grace-period=0` 清。

删之前先按 [[log-pull-before-delete-gate]] 存档日志，且 gate 要做**内容级校验** —— 只看文件大小会把 `Unable to connect to the server`（API 超时）和 `BadRequest: container is waiting to start` 当成有效日志。

相关：[[gb300-cluster-state]]
