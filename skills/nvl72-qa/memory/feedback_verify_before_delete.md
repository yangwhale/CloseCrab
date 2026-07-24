---
name: feedback-verify-before-delete
description: 删除 namespace/pod/DS 前必须先确认日志已完整收集，不要先删再发现日志没了
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1d46131b-e454-47aa-9615-b08009477d3c
---

删除 k8s 资源（namespace、pod、DaemonSet）前，**先确认日志已完整收集**。

**Why:** 多次在日志未收齐时就删了 namespace 或 force-delete pods，导致数据丢失无法补救。QA 测试跑了十几分钟的结果因为提前删除而白费。

**How to apply:**
- 手动清理前：先跑 `find logs/qa-*-<domain>-* -name "*.log" -size +100c | wc -l` 核对数量
- 脚本里：`collect_logs` 完成后再调 `cleanup_ds`，且 `collect_logs` 返回 0/0 时必须报错不能静默过
- 重跑/重启批次前：先审计当前已收集的日志，确认哪些已经完整不需要重跑
- 删 namespace 前：确认该 namespace 下的 pod 日志都已落盘

相关：[[qa-toolkit-design]]
