---
name: ops-log-discipline
description: 每次执行 GCP 操作后必须追加记录到 docs/operations.md
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 84c18ffc-fe2a-4786-a7c5-abc764542166
---

执行任何 GCP 基础设施操作（gcloud create/delete/update、VM 创建、网络变更、权限变更等）后，必须立即将操作记录追加到 `docs/operations.md`。

**Why:** 这个文件是最终交付文档的依据，漏记会导致交付文档不完整。用户明确指出"你如何保证做到这点"，说明口头承诺不够。

**How to apply:** 每次 gcloud 命令执行完，在回复用户结果的同时，用 Edit 工具追加到 `docs/operations.md` 对应章节。格式参照已有条目：日期、命令、结果、踩坑。不要攒批，每次操作立即记录。
