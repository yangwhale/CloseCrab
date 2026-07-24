---
name: feedback-no-unverified-attribution
description: 报告 / 交付文档禁止未实测验证的性能归因；差距只列事实数值，不猜测原因
metadata:
  type: feedback
---

benchmark 报告里写"~X% gap 来自 GPU 锁频 / vboost / driver 差异 / xxx"这种归因，如果没实测 baseline 对比过就是猜测，用户明确禁止。

**Why:** 2026-07-19 dsv3 报告写"1618 vs 官方 1648 的 ~2% gap 来自官方额外开了 GPU 锁频 + vboost"，用户指正："vboost，锁频这种猜测不要出现在报告中"。gap 是事实，归因是猜测。给客户/内部看的报告出现未验证归因会误导决策。

**How to apply:**
- 报告里差距/性能落后只写**事实数据**（e.g. "1618 vs 1648，达成率 98.2%"），不加"来自 X"/"因为 Y"这种归因
- 若确实想标注可能原因，必须**先实测验证**（例如实际启用 vboost 再跑一次比较）后写入
- 相关判断可以放到 ops.md / memory 里作为 hypothesis 待验证，不进正式报告
- 覆盖范围：docs/ 下所有 .md（training guide / benchmark report / handoff doc），不限于某个具体文档
