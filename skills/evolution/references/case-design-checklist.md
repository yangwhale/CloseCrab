# Case 设计与执行核对清单（Round 3 教训）

> Round 3 实测教训：fast-path live verify 一连漏了**两个** silent bug — stale binary 和 cross-layer keyword 不 round-trip。第一轮误报 PASS 是 evaluator 没看四元组的副产物。下面 3 个 anti-pattern 提炼为 case 必填 checklist，下一轮起任何 fast-path / control-request / IPC 类 case 都得过这关。

## Anti-pattern 1 — Stale binary（不验证 bot 加载了目标 commit）

### 症状
- live test 看下游行为是 ✅（plan 通过 / 工具继续运行）
- 但 source-of-truth 显示**根本没走 fast-path**（gap = 数秒 + answer 是用户点 card 出来的）
- 误判根因：bot 跑的是 pre-patch 旧 binary，新代码没加载

### 必检步骤（每个 case 前置）
```bash
# 1. bot 进程启动时间
ps -eo pid,lstart,cmd | grep <bot_name> | grep -v grep

# 2. 目标 commit 时间
git log --format='%ad %h %s' -10 -- closecrab/<modified_file>

# 3. 对比：bot lstart > commit time 才算加载
# 如果 bot lstart < commit time → 必须先 restart bot
```

### Restart 模板（self-SIGHUP, daemon-friendly）
```bash
# 让 bot 自己 90s 后 SIGHUP 自己（run.sh wrapper 会自动重启）
nohup setsid bash -c 'sleep 90 && kill -HUP <bot_pid>' </dev/null >/dev/null 2>&1 &
disown
```

### 防御
- case 模板第一行就是 **"binary alignment check: ps lstart > git log commit time"**
- evaluator 验收时**必须**报 bot PID + lstart + HEAD commit + commit time 四项

---

## Anti-pattern 2 — 只看下游行为（不取 source-of-truth 四元组）

### 症状
- "Claude 工具继续运行" 被当作 PASS 证据
- 真实路径可能是 user 手点 feishu card / 5 分钟超时返回 "继续" / 任何意外通路
- 4-tuple 不全 → 假 PASS

### 四元组（fast-path live test 必填）
| 字段 | 取值方法 | 通过门槛 |
|---|---|---|
| control_request_time | `grep "Control request for <Tool>" bot.log` | — |
| control_response_time | `grep "Sent control_response for <Tool>: answer=" bot.log` | — |
| gap_ms | response_time - request_time | **< 100ms** 才是真 fast-path |
| exact_return_string | log 里 answer= 后面的精确字符串 | 必须等于 fast-path 设计返回值 |
| behavior | Claude 下一步动作（allow / deny / 错乱） | allow |

### 实战取证命令
```bash
grep -nE "Control request for (ExitPlanMode|AskUserQuestion)|Sent control_response for" \
  ~/.claude/closecrab/<bot>/bot.log | tail -10
```

### 防御
- case 验收报告**必须**列出四元组表格（不省略 gap_ms / exact_return_string）
- gap_ms ≥ 100ms 自动标 ❌ FAIL，不接受 "可能是 logging 抖动" 的辩解
- exact_return_string ≠ 设计返回值自动标 ❌ FAIL

---

## Anti-pattern 3 — Fast-path return 跨层 contract 不 round-trip

### 症状
- channel 层 fast-path 返回 "approved"（设计上让 worker 当作"批准"）
- 但 worker 层 `_build_control_response` 的 keyword set 没有 "approved"
- 行为 = deny → plan 被拒 → 看似 fast-path 触发但没生效

### Round 3 实例
```python
# closecrab/workers/claude_code.py:453 (修复前)
_approve_keywords = {"可以了", "开干", "好的", "批准", "开始吧", "ok", "OK", "yes", "go"}
# ← channel 返回 "approved" 但 set 里没有！
```

修复（commit f197e97）：加 `"approved"` 进 set。

### 必检步骤（patch fast-path return 前）
```bash
# 1. 列出所有 worker 的 control_response / 答案解析逻辑
grep -rn "_approve_keywords\|_build_control_response\|control_response" \
  closecrab/workers/

# 2. 对 fast-path 计划返回的每个字符串
#    grep ALL worker 看是否被识别
for word in "approved" "继续" "ok"; do
  echo "=== $word ==="
  grep -rn "\"$word\"\|'$word'" closecrab/workers/
done

# 3. 多 worker 时确保每个 worker 都识别（claude_code / kilo / gemini_acp / openclaw_acp）
```

### 防御
- fast-path patch PR **必须**附带 cross-worker grep 矩阵（4 worker × N 返回字符串）
- mock test **必须**包含 round-trip 测试：用 worker 的 `_build_control_response`（或等价函数）验证每个 fast-path 返回值的实际 behavior
- 至少 1 个 **negative round-trip test**（e.g. "nope-not-approved" → deny）防御 approval bypass

---

## Case 设计模板（自查 7 问）

新设计任何 fast-path / control-request / IPC 类 case 前，过一遍：

1. ✅ **目标 commit** 是什么？什么时间 push？
2. ✅ **目标 bot** 的当前 PID 和 lstart？lstart > commit time?
3. ✅ 期望走哪条 code path？（fast-path / user-facing / 其他）
4. ✅ 四元组期望值是什么？（request_time 范围 / response_time 范围 / gap_ms 上界 / exact_return_string / behavior）
5. ✅ 涉及的 cross-layer contract？fast-path 返回值在所有 downstream consumer 都被识别吗？
6. ✅ 有 mock round-trip test 覆盖吗？（包含 negative test）
7. ✅ 取证命令是什么？（log grep / 文件 read / Firestore query）

7 问全过才能 dispatch。少一个就**先补再 dispatch**，不要"先跑跑看"。

---

## 关联

- 触发 Round: `round_2026-05-20_discord-dingtalk-fastpath`
- Fast-path pattern 主文档: `evolution/references/control-request-fastpath.md`
- Cross-bot restart 协议: `evolution/references/cross-bot-restart-protocol.md`
- Silent failure 检测（同根问题不同切面）: `evolution/references/silent-failure-detection.md`
- Mock test template（含 round-trip 范例）: `evolution/references/mock-test-template/`
- 相关 GBrain memory:
  - `feedback_test-pass-claim-needs-source-of-truth-verification`
  - `feedback_three-source-cross-verify-bot-attribute`
  - `feedback_summary-is-secondhand-narrative`
