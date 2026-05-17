# Kilo Worker 升级记录：跟 OpenClaw 看齐

> 2026-05-17 | Author: tiemu (OpenClaw worker) | Status: 已落地 + 已验证

## 背景

CloseCrab 当前有四种 worker：`claude_code` / `gemini_acp` / `openclaw_acp` / `kilo`。
其中 OpenClaw runtime 在工具编排、并行执行、sub-agent、定时任务、自省、多模态等方面有
原生支持；Kilo 因为基于上游预编译二进制（`@kilocode/cli`），服务端能力受限，需要
worker 层和 prompt 层补救。

这份文档记录 2026-05-17 由 tiemu（OpenClaw worker）对 xiaoaitongxue（kilo worker）做
对抗测试 + 修复的 6 轮迭代，目的是让 Kilo worker 接近 OpenClaw 的能力面。

---

## 实测发现的 Kilo 真实短板

按严重程度排：

### 1. `task` (subagent) 工具完全串行调度

同一回复内并发发出 3 个 `task` tool_use，服务端**强制排队启动**，
start 时间戳跨 **6.96 秒**（每个 ~3.3s 间隔）。原因是每个 subagent 需要起独立
LLM session + LOAS 凭据，Kilo 服务端把它当重资源串行调度。

→ 见 [**修法 1: subagent-parallel.py**](#修法-1-subagent-parallelpy) 和 [**规则 #11**](#prompt-12-条新规则)。

### 2. Bash tool_use 并发被节流（~50% 并发度）

同一回复内并发发出 5 个 `bash` tool_use（每个 sleep 3），实测 start 跨 2.5 秒，
总耗时 5.55s（理想真并行应 3.0s）。比 task 工具好，但仍远非真并行。

→ 见 [**规则 #2**](#prompt-12-条新规则)：教 worker 用 `bash -c '() & () & wait'` 在
**一次** tool_use 内做 OS 级真并行，绕开服务端节流。

### 3. 多文件场景默认拆 N 次 `write`

写 3 个文件 → 5 次 tool_use（mkdir + 3× write + bash 计算）。每次 tool_use 都有
IPC + LLM 推理往返开销，N 倍浪费。

→ 见 [**规则 #1**](#prompt-12-条新规则)：批处理优先，用 1 条 bash + heredoc/echo 合并。

### 4. Inbox 回执硬切 2000 字符

`feishu.py:1906` 写死 `result[:2000]`，但下游 `firestore_inbox.mark_done` 实际能存
10000。多 MCP 调研类长报告的「调用统计」/「结论」章节会被切掉。

→ 修：`feishu.py:1906` 改为 8000 字符（commit `e476992`）。

### 5. 没有真并行多 LLM sub-agent

OpenClaw 有 `sessions_spawn`，可真并行起 N 个独立 LLM 推理 agent。Kilo 的 task 完全串行
（短板 1），bash + Python 是 OS 级并行但每个 task 是一段固定脚本、没有 LLM 推理能力。

→ 修：[**subagent-parallel.py**](#修法-1-subagent-parallelpy)。

### 6. 没有 cron / 定时任务

OpenClaw 有 `cron` 工具，Kilo worker 没有等价能力。

→ 修：[**cron-tool.py + cron-daemon.py**](#修法-2-cron-工具--daemon)。

### 7. 没有 session 自省

OpenClaw 有 `session_status`。Kilo worker 答不出"我用什么模型 / 今天花了多少钱"。

→ 修：[**session-status.py**](#修法-3-session-statuspy)。

### 8. 早就存在但 worker 不知道：imagen / tts 脚本

`skills/imagen-generator/` 和 `skills/tts-generator/` 早就提供了图片 + 语音生成，但
Kilo worker 的 prompt 没说过，所以她不主动用。

→ 修：在 prompt 加 [「多模态生成」](#prompt-12-条新规则) 章节。

### 9. cron-daemon 没接 launcher

第一版 cron-daemon 是手工 `setsid` 跑的，机器重启或误杀就废了。

→ 修：`launcher.sh` 加 `_ensure_cron_daemon` idempotent 启动逻辑（commit `e430b0b`）。

---

## 修法 1: subagent-parallel.py

**位置**：`scripts/subagent-parallel.py` (214 行)

**核心**：用 `AsyncAnthropicVertex` SDK 在 `asyncio.gather` 里跑 N 个真并行 LLM 推理 agent，每个 agent 自带 bash + read 工具的 tool loop。

**调用**：

```bash
python3 ~/CloseCrab/scripts/subagent-parallel.py --inline '{
  "tasks": [
    {"label": "A", "prompt": "调研 X 文件..."},
    {"label": "B", "prompt": "调研 Y 文件..."}
  ]
}'
```

返回 JSON：每个 agent 的 `text` / `tool_uses` / `elapsed_ms` / `start_ns` / `end_ns` / `error` + `total_elapsed_ms` + `parallelism`。

**约束**：最多 8 个 task 并发，每个 task 最多 8 轮 tool。

**实测对比**（3 个独立文件调研任务）：

| 方案 | total_elapsed | start 跨度 | tool_use 数 | 真并行 |
|---|---|---|---|---|
| Kilo `task` × 3 | 23.0 s | 6960 ms | 3 | ❌ |
| 1 条 bash + Python | 4.0 s | n/a | 1 | ✅ 但无 LLM 推理 |
| **subagent-parallel × 3** | **5.8 s** | **4 ms** | **1** | ✅ + 每 agent LLM 推理 |

**选型决策**：
- 任务是纯 shell / 固定脚本 → **1 条 bash + Python**（最快）
- 每个任务需要 LLM 推理 / 多轮工具调用 → **subagent-parallel.py**
- 需要 isolation 的长任务 → 才用 Kilo `task`

---

## 修法 2: cron 工具 + daemon

**位置**：
- `scripts/cron-tool.py` (297 行) — CRUD CLI: add / list / remove / tick
- `scripts/cron-daemon.py` (84 行) — 30s tick 守护进程，host 单例
- `scripts/launcher.sh` — `_ensure_cron_daemon()` 自动拉起 (commit `e430b0b`)

**调用**：

```bash
BOT_NAME=$BOT_NAME python3 ~/CloseCrab/scripts/cron-tool.py add \
  --target $BOT_NAME --in 10m --message "..."
  # 也支持 --at <ISO UTC> 或 --cron "0 9 * * MON-FRI"
```

**数据模型**：Firestore `scheduled_jobs/{job_id}` 集合
```
{
  job_id, kind: "oneshot"|"recurring",
  cron, fire_at, target, sender, message,
  status: "scheduled"|"done"|"cancelled"|"error",
  created_at, last_fired_at, fire_count
}
```

**daemon 行为**：每 30s 调 `tick`：
1. 删 > 7 天的 done/cancelled/error 任务（GC sweep）
2. 找 fire_at ≤ now 且 status=scheduled 的，写 target bot 的 inbox（前缀 `[⏰ 定时提醒]`）
3. recurring 任务 next_fire_at = next_cron_fire(expr)，oneshot 任务 status=done

**Cron 表达式解析**：优先用 `croniter` 库，没装的话 fallback 到内置 `_basic_cron`，支持 `* / N / a-b / a-b/n / 列表 / DOW 别名 MON-FRI`。

**精度**：30s tick → 实际触发延迟 0-30s。不适合秒级精确调度，适合"人感"提醒。

**E2E 验证**：60s 后 xiaoaitongxue 准时收到 `[⏰ 定时提醒] cron 工具 E2E 测试`，回 `cron 到达 ✓`。

---

## 修法 3: session-status.py

**位置**：`scripts/session-status.py` (172 行)

**数据来源**（Firestore 三表 join）：
- `bots/{name}` — worker_type, model, active_channel, team role, description
- `registry/{name}` — status (online/offline), hostname, last_seen
- `bots/{name}/logs/*` — 每 turn 的 usage（input/output/cache tokens, cost_usd, duration_seconds, status, steps）

**调用**：

```bash
python3 ~/CloseCrab/scripts/session-status.py [bot_name] [--days N] [--json]
```

**输出示例**：

```
## 📊 xiaoaitongxue session_status

**Identity**:
  • worker: `kilo` · model: `google-vertex-anthropic/claude-opus-4-7@default`
  • channel: `feishu` · team: `teammate`

**Runtime**:
  • status: **online** · host: `chrisya-cc-tw...`
  • last_seen: 2026-05-17 02:52 UTC

**Last 1d usage** (62 turns, 0 errors):
  • tokens in/out: 143 / 20.1k
  • cache read/write: 5.28M / 1.65M
  • cost: **$17.99** · total dur: 450.8s
  • channels: feishu=62

**Last 5 turns**:
  • 2026-05-17 02:55:01 UTC [feishu/done] steps=2 dur=7.8s in=6 out=16 cost=$1.22
  ...
```

**触发场景**：用户问"用什么模型 / 今天花了多少 / 上几轮做了什么"，prompt 教 worker 直接走这个脚本。

---

## Prompt: 12 条新规则

全部加到 `closecrab/prompts/kilo-memory-guide.md` 的「关键行为准则（最高优先级）」章节顶部，共 12 条，按使用频率排序：

| # | 规则 | 解决的问题 |
|---|---|---|
| 1 | 批处理优先（少调用比多调用好） | 多文件场景拆 N 次 write |
| 2 | 真正并行用 shell `& wait`（重要） | 服务端 bash 并发节流 |
| 3 | 独立只读查询仍在一次回复内并发 | 浪费往返 |
| 4 | 报数自律 | 自我校正机制 |
| 5 | 工具选择优先级（grep > read+regex 等） | 工具选错 |
| 6 | 时效字段必须实查 | 凭记忆答错 |
| 7 | Memory 调用纪律（先 grep MEMORY.md 再答） | 凭记忆瞎编 |
| 8 | 错误重试 / 弱结果再查 | 第一次失败就放弃 |
| 9 | 多步任务强制 todo | 漏步骤 / 重复劳动 |
| 10 | 需要真并行 LLM 推理：用 subagent-parallel.py | task 完全串行 |
| 11 | `task` (subagent) 工具 — 严格串行，慎用 | 错用 task 做并发批处理 |
| 12 | 多模态生成（imagen / tts） | 不知道现有工具 |
| 13 | 自我状态查询（session-status） | 不知道自己状态 |
| 14 | 定时提醒 / cron 能力 | 没有定时能力 |

---

## 修复前后对比（最终一图）

| 指标 | 修复前（Kilo 默认） | 修复后 | 改进 |
|---|---|---|---|
| 多文件创建 tool_use | 5 | 1 | -80% |
| 5 并发任务 start 跨度 | 2553 ms | 0.4 ms | -99.98% |
| 5 并发任务总耗时 | 5.55 s | 3.0 s | -46% |
| 3 文件调研（task 串行 → bash+Py） | 23 s | 4 s | 5.75× |
| 3 并发 LLM sub-agent | 不支持 | 5.8 s, 4ms 跨度 | 新能力 |
| Inbox 长结果截断 | 2000 字 | 8000 字 | 4× |
| 定时提醒 | 不支持 | 60s 提醒 E2E ✓ | 新能力 |
| 自省（cost / model / usage） | 不支持 | session-status ✓ | 新能力 |
| 多模态生成 | 不主动用 | imagen + tts 知道用 | 新能力 |

---

## 已落地的 commits

```
f5e97ec  kilo: 第六轮 — 修 3 个真 bug + 写文档
e430b0b  kilo: 第五轮 — cron 自启 + session_status 工具
1286279  kilo: 第四轮 — 教她用已有的 imagen / tts 脚本
aa12d1d  kilo: 第三轮调教 — 6 条新 prompt 规则 + 2 个真新装备
e476992  feishu: inbox 回执上限 2000 → 8000 字符
622de25  kilo: 加 task (subagent) 工具使用准则
a82871f  kilo: 加工具调用效率规则（批处理 + bash 真并行）
```

---

## 设计权衡 & 留坑

### 为什么不把 subagent-parallel 包成 MCP server？

抽象层增加但价值不大：bash 调用已经够清晰，且 Kilo MCP 调用本身有调度开销
（约 1-2s），不如直接 bash 起 Python。

### 为什么 cron 精度只到 30s？

避免 daemon 频繁打 Firestore（成本 + quota）。要更高精度可以缩短 INTERVAL，
但提醒类场景 30s 完全够用。

### 为什么不修 Kilo 服务端的串行化？

Kilo CLI 是预编译二进制（`@kilocode/cli`），无 TS/JS 源码可改。所有补救都在
worker / prompt / 外部脚本层。

### 留坑 1：Kilo 上报的 cost_usd 偶尔不合理

实测有 turn 只 6 输入 / 16 输出 token 但 cost_usd=$1.22，怀疑 cache_creation token
的计费规则没分摊清楚。但这是上游 binary 的事，无法在 worker 层修。

### 留坑 2：MEMORY.md 写入路径仍依赖 bwrap-passthrough

xiaoaitongxue 写 MEMORY.md / memory/ 需要 bwrap 允许，目前是通过
`bwrap-passthrough` 全局解除（见 MEMORY.md feedback 项）。如果将来 sandbox 收紧
需要重新评估。

### 留坑 3：subagent-parallel 暂只支持 bash + read 两个工具

够覆盖 80% 场景（调研、计算、文件分析）。如果未来需要让 subagent 用 grep / glob /
web_fetch / MCP，可以扩展 TOOLS 列表，但要小心 token 消耗膨胀。

---

## 相关文档

- [`kilo-worker-design.md`](kilo-worker-design.md) — KiloWorker 初始设计
- [`kilo-worker-optimization.md`](kilo-worker-optimization.md) — 第一轮优化（permission 配置化 + 指令文件加载）
- [`openclaw-worker-design.md`](openclaw-worker-design.md) — OpenClaw worker 设计（这次升级的对标对象）
