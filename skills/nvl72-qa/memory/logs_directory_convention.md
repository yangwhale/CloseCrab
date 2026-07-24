---
name: logs-directory-convention
description: GB300 项目日志文件放 logs/ 目录，不放 docs/
metadata: 
  node_type: memory
  type: feedback
  originSessionId: a41bd9c5-a072-420b-86fe-98ea5ee5653e
---

日志文件（hw-check 输出、benchmark 结果等运行产出）放 `logs/` 目录，不放 `docs/`。

**Why:** 用户明确纠正过，docs/ 放文档，logs/ 放运行日志。

**How to apply:** 脚本输出、检查报告、benchmark 日志等保存到 `~/code/tencent/gb300/logs/`，命名带日期（如 `hw-check-20260713.log`）。
