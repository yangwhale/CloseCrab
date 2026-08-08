# Task Scheduler — 时间驱动的任务系统

> 设计：2026-08-08
> 状态：**PRD 草案，待打磨。未开始实现。**
> 作者：Chris × bunny 协同设计
> 调研输入：OpenClaw（本机源码）、Claude Code（官方文档）、Goose、OpenHarness、Crush、OpenCode

---

## 1. 问题

### 1.1 目标能力

给 bot 一个**任务清单**，但它不只是清单——它同时是**日历 + cron**。随着时间流逝：

1. 心跳按固定节奏推进（当前 cron-daemon 是 30s）
2. 每次心跳去清单里找「这一刻该做什么」
3. 没有 → 直接过去
4. 有 → 取出任务，**拉起一个后台 agent 去执行**
5. 执行进展**输出到聊天窗口**
6. 完成 → 任务从清单移除
7. 周期任务 → 继续排下一次，**直到条件满足才停**

两条典型用户故事：

> **US-1（间隔轮询）**：「每 30 分钟看一次资源池，有空闲机器就告诉我，然后这个任务就结束。」
>
> **US-2（挂钟定时）**：「每天早上 8 点帮我看 12306 有没有票。」

### 1.2 硬约束

**执行必须是 agent 推理，不能是 shell 脚本。** 脚本表达能力太差——「有没有空闲机器」这种判断需要看队列、看物理节点、理解上下文，不是一条 `grep` 能解决的。

---

## 2. 现状盘点

CloseCrab 已经有约 70% 的地基，**不需要推倒重建**。

### 2.1 现有链路

```
cron-daemon.py          每 30s tick（INTERVAL = 30，cron-daemon.py:15）
      ↓ subprocess
cron-tool.py tick       扫 Firestore scheduled_jobs，找 fire_at 到期的
      ↓ 写一条
messages 集合（inbox）
      ↓ on_snapshot 实时推送
目标 bot                触发一次完整 LLM turn  ← 不是脚本
      ↓
回复落到 bot 的聊天窗口
```

### 2.2 已具备

| 能力 | 现状 | 位置 |
|---|---|---|
| 心跳 tick | 30s，比需求的 1min 更细 | `scripts/cron-daemon.py:15` |
| 一次性延时 | `--in 10m` | `scripts/cron-tool.py` `parse_in` |
| 绝对时刻 | `--at <ISO>` | `cron-tool.py:129` |
| 挂钟周期 | `--cron "0 9 * * MON-FRI"` | `cron-tool.py` `next_cron_fire` |
| 周期自动续排 | 触发后重算 `fire_at` | `cmd_tick` 里 `kind == "recurring"` 分支 |
| **执行为 agent 推理** | 经 inbox 触发完整 turn | `closecrab/utils/firestore_inbox.py` |
| 结果回流聊天 | turn 的 reply 走 channel | 现成 |
| 过期清理 | done/cancelled/error 存 7 天 | `cmd_tick` sweep 段 |
| 排期原则文档 | STAGGER / QUIET HOURS / THIN PROMPTS | `cron-tool.py` docstring |

### 2.3 缺口（本设计要补的）

**G1 — 周期任务无法自终止。**
派发的指令是 `f"[⏰ 定时提醒] {x['message']}"`，**`job_id` 没进正文**（只在 `task_id` 字段写成 `cron-{job_id}`）。agent 查到票之后不知道该删哪条 job，只能靠人手 `cron-tool.py remove`。US-1 直接卡在这。

**G2 — 存的是「定时消息」不是「任务对象」。**
`scheduled_jobs` 的语义是「到点发这句话」。没有目标、没有状态机、没有进度累积、没有执行历史。用户心智里的「任务清单」需要的是有生命周期的对象。

**G3 — 定时 turn 与用户对话抢锁。**
`BotCore._user_task_locks`（`closecrab/core/bot.py:99`）把同一 `user_key` 的 turn 串行化。定时任务触发的 turn 和用户正在进行的对话争同一把锁。虽有超时强制驱逐兜底（`_acquire_user_task_lock`，`bot.py:116`），但用户打字时后台任务到点会互相卡。

**G4 — cron 表达式按 UTC 求值。**
`cron-tool.py:140` 的 `croniter(expr, after)` 中 `after` 是 UTC，结果也 `.astimezone(timezone.utc)`。CLI help 明写 `cron expr "M H DOM MON DOW" UTC`。

**后果**：US-2 写成 `--cron "0 8 * * *"` 会在**香港时间下午 4 点**触发。这是个静默错误，不会报任何异常。考虑到项目所有时间约定都是 HKT，这是必须修的坑。

**G5 — 无成本控制。**
每次触发 = 一次完整 LLM turn。「每 3 分钟查一次」= 一天 480 次。详见 §7。

---

## 3. 业界调研

### 3.1 横向对比

| | 跨 session 持久队列 | 定时/周期 | 心跳 | 到期执行方式 | 终止条件 | 结果回推 |
|---|---|---|---|---|---|---|
| **Claude Code** | 部分（session 级） | 有 | 1s | 注入 prompt 到回合间 | **self-paced stop** | 会话内 |
| **OpenClaw** | 部分 | 有（3 kind） | 事件驱动 armTimer | **agentTurn + isolated session** | **削权自删** | announce/webhook |
| **Goose** | **有**（全局 job 库） | 有 | tokio-cron | 起完整 Recipe session | 只能手删/暂停 | 无 |
| **OpenHarness** | **有**（JSON registry） | 有 | 30s | shell **或** agent_turn | 只有 enabled 开关 | **有（飞书 DM）** |
| **Crush** | 无（PR #3466 待合） | 无（PR 待合） | PR: 1min | PR: 注入 user turn | PR: 一次性/cron | 无 |
| **OpenCode** | 无 | **无** | 无 | 靠外部 launchd/systemd | — | 无 |

### 3.2 已成共识的三条

1. **调度必须是 agent 可自管的工具**，不是纯 CLI。Claude Code `CronCreate`、Goose `agents/schedule_tool.rs`、OpenClaw cron tool 都把调度能力开放给模型自己调用。
2. **到期必须拉起 agent 推理，不是跑脚本。** 四家独立收敛到同一答案：Claude Code 注入 prompt、Goose 起 Recipe session、OpenClaw `agentTurn`、OpenHarness `payload.kind = "agent_turn"` → `ohmo --print` 子进程。这印证了 §1.2 的硬约束不是个人偏好，是这类系统的必然形态。
3. **tick 普遍是朴素轮询**（1s ~ 60s）。只有 OpenClaw 用事件驱动 `armTimer`（对齐最早的 `nextRunAtMs`，`Math.max(nextAt - now, 0)`）。

### 3.3 尚无共识的两块 —— 正是本设计的核心

**终止条件。** 主流只有「手动删 / 暂停 / 兜底过期」。像样的只有两家：

- **Claude Code** `ScheduleWakeup`：self-paced 模式下模型自己决定下次间隔（1min–1h），完成后调 `stop:true` 自行终止。
- **OpenClaw** `selfRemoveOnlyJobId`（`dist/plugin-sdk/src/agents/tools/cron-tool.d.ts`）：给 isolated run 注入一个**只能删自己这条 job** 的 cron 工具。

注意 OpenClaw **没有** `maxRuns` / `until` 声明式字段（grep 整个 dist 零命中）。这是刻意的：**终止条件是语义的（"查到票"）而非计数的**，声明式字段表达不了。

**结果回推。** 几乎空白。只有 OpenHarness 因自带 channel 层做了 `notify: {type: feishu_dm}`。

### 3.4 为什么业界没做好

不是需求少（OpenCode 挂着至少 4 个 FR：#11232、#36676、#39514）。三个真实阻力：

1. CLI agent 进程生命周期短——关终端就没了，逼你要 daemon
2. 「睡着的 agent 被唤醒」要解决 context 和权限从哪来
3. **外部 cron + `agent-cli --print` 确实能覆盖 80%**，框架方缺动力

**CloseCrab 恰好绕开了前两个**：bot 是常驻进程，channel 层现成，Firestore 已是共享状态。这是我们能把这件事做完整的结构性优势。

### 3.5 值得直接借鉴的设计

**B1 — 定义与运行时状态物理分离**（OpenClaw）

```
~/.openclaw/cron/jobs.json          定义（可 git track）
~/.openclaw/cron/jobs-state.json    nextRunAtMs / consecutiveErrors / lastStatus
~/.openclaw/cron/runs/<jobId>.jsonl 每条 job 的执行历史
```

手改定义文件后，系统比对 schedule 字段的 identity 判断是否需要重排 pending 的 `nextRunAtMs`——**纯改格式或键序不会误触发**。

**B2 — 能力收窄的自终止授权**（OpenClaw `selfRemoveOnlyJobId`）

比 `maxRuns` 灵活，又不用把整个 cron 管理权开放给一个后台 job。配套的 introspection 也自过滤（只能看自己那条 job 和它的历史）。

**B3 — isolated session**（OpenClaw `--session isolated` → `cron:<jobId>`）

定时任务开专属全新 session。顺手解掉 G3 的锁竞争——**不是同一个 session，压根不争锁**。

**B4 — 整点错峰**（OpenClaw 默认随机 stagger ≤5min）

与我们 `cron-tool.py` docstring 里的 STAGGER 原则不谋而合，但我们只是文档约定，靠模型自觉；OpenClaw 是代码强制。

---

## 4. 概念模型：Task 与 Job 分离

现有 `scheduled_jobs` 把两个概念揉在一起了。拆开：

```
Task  = 目标 + 状态 + 进度累积 + 执行历史      ← 用户心智里的「任务清单」
Job   = 触发时机（何时把 Task 唤醒一次）        ← 现有 scheduled_jobs 的职责
关系  = 一个 Task 挂 0..1 个 Job
```

**为什么必须分开**：

- 有些 Task 没有 Job（纯待办，用户想起来了手动推进）
- 一个 Task 会被同一个 Job 唤醒**很多次**，每次产生进度，但 Task 只有一个
- Task 的终止（目标达成）和 Job 的终止（不再排下一次）是两件事，虽然通常一起发生

**Task 状态机**：

```
pending ──► active ──► done
              │
              ├──► blocked   （等外部条件，Job 暂停）
              └──► failed    （连续错误超阈值，见 §7.3）
```

---

## 5. 时间模型

### 5.1 核心结论：物化层统一，规则层不统一

调度器在 tick 那一刻只问一个问题：`next_fire_at <= now?`。所以**物化成绝对 UTC 时间戳是没得选的**。

但只存这个就不知道下一次排到哪。所以分两层（即 §3.5 B1）：

```
规则层（声明式，怎么算下一次）   ← 三种 kind，不可互相化约
物化层（一个绝对 UTC 时间戳）    ← 调度器只看这个
```

### 5.2 三种 schedule kind

| kind | 语义 | 对应用户故事 |
|---|---|---|
| `at` | 一次性绝对时刻 | 「明天 8 点查一次」 |
| `cron` | 挂钟周期，**必带时区** | US-2「每工作日早 8 点」 |
| `every` | 间隔周期，**带锚点选择** | US-1「每 30 分钟看资源池」 |

**加一个越权通道**：agent 可在执行中直接改写自己这条 Job 的 `next_fire_at`。对应 Claude Code 的 `ScheduleWakeup` ——下次什么时候来由模型判断。这是退避（§7）和「我判断 10 分钟后再来」的落脚点。

### 5.3 为什么 `every` 和 `cron` 不能互相化约

**① 漂移语义相反**

| | 锚点 | 任务跑了 10 分钟后 | 机器停了 2 小时 |
|---|---|---|---|
| `cron` | 挂钟槽位 | 下次仍在 8:00 整 | 错过的槽位**跳过** |
| `every 30m` | 上一次 | 下次是 T+40 | 需要决定补不补 |

US-1 要的是**不重叠**：上一次查完了再等 30 分钟。cron 表达不了——它到点就发，不管上一次跑完没有。

**② 日历语义 cron 独有**：「每个工作日」「每月 1 号」「跳过周末」，interval 算不出来。还有 DST（香港没有，但跨时区部署会中招）。

**③ 只有 `every` 能做退避**：查 3 次没票就把间隔从 3 分钟拉到 10 分钟——cron 表达式改不了，interval 改个数字就行。这是 §7 成本控制的前提。

### 5.4 为什么不照抄 Google Calendar

Calendar 存 `RRULE` + `DTSTART` + `TZID` 然后物化实例，这个**两层思路是对的**，我们采纳。

但 **RRULE 是纯挂钟的**，没有「从现在起每 30 分钟」的概念——人的日历不需要。照抄会让 US-1 无处安放。

**结论**：借鉴 Calendar 的两层结构，但规则层必须比 RRULE 多一种 `every`。

### 5.5 三个必须拍板的时间细节

**① 时区**
`cron` kind 必须存 `tz` 字段，默认 `Asia/Hong_Kong`。`every` 和 `at` 不需要（`at` 存 UTC 绝对值，输入时按 HKT 解析）。

→ 这同时是 G4 的修复。**已有数据的迁移必须逐条人工做，绝不能批量加默认值。**

2026-08-08 实查 `scheduled_jobs`，共 4 条，2 条 `cancelled`，2 条在跑：

| cron | message 里声称的时间 | 实际 | 结论 |
|---|---|---|---|
| `13 0 * * 1` | 「周一 8:13 HKT」 | 00:13 UTC = 08:13 HKT | ✓ 正确 |
| `7 0 * * 1` | 「周一晨扫」 | 00:07 UTC = 08:07 HKT | ✓ 正确 |

**当前没有跑错的任务** —— 但这是因为**作者当初手工换算过**（想要 8 点，写 0 点 UTC）。

这恰恰坐实了 G4 的危害：表达式写 `13 0`、说明文字写「8:13 HKT」，**两者对不上，只有作者知道为什么**。后续任何人（包括 LLM 自己）去改这条 job，看到「8:13 HKT」就会写成 `13 8`，直接偏 8 小时。这是典型的靠人肉补偿维持正确的脆弱状态。

**迁移动作**：加 `tz: Asia/Hong_Kong` 的**同时**必须把表达式改回直觉写法（`13 0` → `13 8`，`7 0` → `7 8`）。只加字段不改表达式，会把这两条任务推到 HKT 凌晨 0 点。两步必须原子完成。

**② `every` 的锚点**
从**上次触发**算，还是从**上次完成**算？

- 轮询类建议锚在**完成**——避免上一轮没跑完就叠下一轮
- 严格节拍类锚在**触发**

建议做成字段 `anchor: "fire" | "complete"`，默认 `complete`，而不是全局约定。

**③ 补跑策略**
机器停 2 小时，30 分钟的任务错过 4 次。

- `every`：**合并成一次**。补 4 次没有意义。
- `cron`：看陈旧度。迟到超过 `max_lateness`（建议默认 1 小时）就跳过，并在执行历史里留一条 `skipped_stale` 记录。

**绝不要一次性把 4 次全放出来**——这会瞬间打爆 4 个 LLM turn。

---

## 6. 执行模型

### 6.1 isolated session

定时任务触发的 turn 走独立 session（沿用 OpenClaw 的命名习惯：`sched:<task_id>`），不进主对话。

**收益**：
- 解 G3：不与用户对话争 `_user_task_locks`
- 不把定时任务的噪音灌进主对话上下文

**代价（必须写清楚）**：
- 独立 session **没有主对话的上下文**。任务描述必须自包含。
- 每次触发都是 cold start，token 成本更高（见 §7）
- 需要决定：同一个 Task 的多次触发是**复用同一个 session**（有连续性，能看到上次查的结果，但 context 会累积膨胀），还是**每次全新**（干净，但每次从零）。
  → **待拍板**。倾向：`every` 类复用（需要「上次查到什么」的连续性），`cron` 类每次全新。

### 6.2 削权自终止

派发时：

1. 把 `task_id` / `job_id` **拼进 instruction 正文**（修 G1）
2. 给这次 run 注入一个**只能操作自己这条 job** 的能力

终止条件用自然语言表达在任务描述里：

> 「每 30 分钟查一次资源池。**查到空闲机器就把结果发给我，然后终止本任务。**没查到就什么都不发，等下一轮。」

注意最后半句——**没查到就什么都不发**。否则用户会被 48 条「还没有」刷屏。这属于任务描述的最佳实践，应该写进 §9 的 prompt 模板。

### 6.3 不采用的方案

- **`maxRuns` / `until` 声明式终止**：表达不了「查到票」这种语义条件。可作为**兜底**保留（防跑飞），但不作为主要终止手段。
- **事件驱动 armTimer**：OpenClaw 的做法更省，但我们 30s 轮询已在跑且够用，换过去的收益不抵改造成本和新引入的 bug 面。**明确不做。**

---

## 7. 成本控制

> **2026-08-08 决策：本期不做。** Chris 拍板「成本先不考虑，先把功能做完美」。
>
> 本节保留为背景记录 + 未来实施依据。**但有一条边界要划清**：
>
> - **不做**的是 §7.2 方案 A「廉价探针 + 升级」—— 那是第二条执行路径，属于架构级改动。
> - **仍然要做**的是 §5.2 的「agent 可改写自己 `next_fire_at`」越权通道 —— 它本来就是自终止机制的一部分，**零额外成本**，而且是将来加退避（方案 B）的唯一前提。砍掉它等于把成本优化的门焊死。
>
> 换句话说：**不建第二条路，但把岔路口留着。**
>
> §7.3 的连续错误熔断**照做** —— 它防的是失控重试，属于正确性而非成本优化。

**以下为原始分析，两个调研 agent 都没提到这一点。**

### 7.1 问题量级

每次触发 = 一次完整 LLM turn。cold start 约 89K token（见 `feedback_cold-start`）。

「每 3 分钟查一次机器」= 一天 **480 次** turn，而其中 **99% 只是查一下发现没有然后退出**。

这不是理论担忧——US-1 正是这个形态。

### 7.2 两个候选方案

**方案 A — 廉价探针 + 升级**
tick 到期时先跑一个 haiku 级的轻量判断（或者干脆一条确定性检查），有变化才拉起完整 agent。

- 优点：成本降一到两个数量级
- 缺点：引入两段式复杂度；「什么算有变化」本身可能就需要推理，那探针就白搭

**方案 B — 指数退避**
查 N 次没结果就自动拉长间隔：3min → 10min → 30min，有结果则重置。

- 优点：实现简单，纯改 `next_fire_at`，§5.2 的越权通道天然支持
- 缺点：降低时效性——真出票了可能晚 30 分钟才发现

**倾向 A，但需要 Chris 拍板**，因为它直接影响架构（要不要引入第二个执行路径）。

也可以两者叠加：探针降单次成本，退避降频次。

### 7.3 连续错误熔断

参照 OpenClaw 的 `consecutiveErrors`：连续失败 N 次（建议 5）自动把 Task 置为 `failed` 并通知，不要无限重试烧钱。

---

## 8. 结果回流

现有链路已经能用：inbox turn 的 reply 走 channel 落到聊天窗口。需要补的是**分档**（借鉴 OpenClaw 的 `announce` / `webhook` / `none`）：

| 档位 | 行为 | 适用 |
|---|---|---|
| `always` | 每次触发都汇报 | 调试期 |
| `on_change` | 只有产生新进展才发 | **默认**，US-1 用这个 |
| `on_done` | 只在任务终止时发一次 | 长周期后台任务 |
| `none` | 不发，只写执行历史 | 纯采集类 |

**注意**：agent 在 run 内本来就可以自己发消息。要防止「agent 自己发了 + 系统 fallback 又发一次」的重复——OpenClaw 的做法是有 route 时 runner 跳过 fallback announce。我们需要同样的去重。

---

## 9. 数据结构（草案）

> 以下 schema 是**讨论用草案**，字段名和取值待定。

### 9.1 `tasks` 集合（新增）

```
tasks/{task_id}
  task_id        str
  owner_bot      str          执行者
  created_by     str          创建者（用户或 bot）
  goal           str          自然语言目标，必须自包含（见 §6.1 代价）
  state          str          pending | active | blocked | done | failed
  progress       [ {ts, summary} ]    进度累积
  job_id         str | null   挂载的 Job
  notify         str          always | on_change | on_done | none
  session_mode   str          reuse | fresh        （见 §6.1 待拍板）
  created_at     ts
  updated_at     ts
```

### 9.2 `scheduled_jobs` 集合（扩展现有，不新建）

```
scheduled_jobs/{job_id}
  # ── 规则层（定义，人可读可改） ──
  kind           str          at | cron | every
  cron           str | null   kind=cron 时的表达式
  tz             str          kind=cron 时必填，默认 Asia/Hong_Kong   ← 修 G4
  interval_sec   int | null   kind=every 时的间隔
  anchor         str          fire | complete，默认 complete          ← §5.5②
  max_lateness_sec int        默认 3600，超时跳过                      ← §5.5③
  max_runs       int | null   兜底防跑飞，非主要终止手段                ← §6.3

  # ── 物化层（运行时状态，机器写） ──
  next_fire_at   ts           调度器唯一读的字段
  last_fired_at  ts
  last_status    str
  fire_count     int
  consecutive_errors int                                              ← §7.3

  # ── 关联 ──
  task_id        str          反向指回 Task
  target         str          目标 bot
  status         str          scheduled | paused | done | cancelled | error
```

**为什么规则层和物化层放同一个 doc 而不是像 OpenClaw 那样两个文件**：我们本来就在 Firestore，没有「配置文件进 git」的诉求，拆两个 collection 只会增加一次读。但**字段分组在文档里要标清楚**，避免有人手改 `next_fire_at` 却不改规则。

### 9.3 执行历史

建议 `tasks/{task_id}/runs/{run_id}` 子集合，或复用现有 `bots/{name}/logs`。**待定** —— 前者查询方便，后者不引入新结构。

---

## 10. 与现有代码的关系

| 文件 | 改动 |
|---|---|
| `scripts/cron-tool.py` | 加 `kind=every`、`tz`、`anchor`、`max_lateness`；`tick` 里 job_id 拼进 instruction（修 G1）；cron 求值改用 tz（修 G4） |
| `scripts/cron-daemon.py` | 基本不动。30s tick 保留 |
| `closecrab/core/bot.py` | inbox 消息按 `sched:` 前缀走 isolated session，绕开 `_user_task_locks`（修 G3） |
| `closecrab/utils/firestore_inbox.py` | 透传新的 task 字段 |
| 新增 `scripts/task-tool.py` | Task 的 CRUD，给 agent 在 bash 里调 |
| `closecrab/main.py` | system prompt 里加 task 工具的用法说明 |

**明确不动**：worker 层、channel 层。这个 feature 不应该下沉到 worker。

---

## 11. 待拍板问题

每条都给出**推荐答案 + 理由**，Chris 确认或反驳即可，不必从零思考。

### 已定

**Q1 — 成本方案** ✅ **2026-08-08 定：本期不做。** 边界见 §7 开头的决策框。

**Q5 — 现存 cron job 的时区迁移** ✅ **已查清，不构成阻塞。** 只有 2 条在跑，当前语义正确；迁移时表达式与 `tz` 字段原子同改，用 T401 验证等价性。

### 待确认（附推荐答案）

**Q2 — session 复用策略**（§6.1）

> **推荐：按 kind 区分。`every` 复用同一 session，`cron` 每次全新。**
>
> 理由：轮询类天然需要「上次查到什么」的连续性——第 3 轮的 agent 知道前两轮都是 BUSY，才可能做出「情况没变」的判断，也才谈得上退避。定点类（每天 8 点查票）各次之间通常独立，全新 session 更干净、上下文更小。
>
> **必须配的护栏**：复用会让 context 无限累积。要设上限（建议 20 轮或 200K token，取先到者）触发强制新开，并把旧 session 的摘要带进新 session。否则一个跑一个月的轮询任务会把 context 撑爆。
>
> 反对意见欢迎：如果觉得护栏复杂度不值，退化成「一律每次全新 + 把上次结果写进 Task.progress 让 agent 读」也成立，只是多一次读。

**Q3 — `every` 默认锚点**（§5.5②）

> **推荐：默认 `complete`。**
>
> 理由：轮询是 `every` 的主要用例，**不重叠**比严格节拍重要得多。一个查询跑 40 秒、间隔 60 秒的任务，用 `fire` 锚点迟早会因为某次变慢而叠起来。想要严格节拍的场景显式写 `anchor: fire` 即可。
>
> 「默认值应该服务于主流用例，而不是理论上更纯粹的那个」。

**Q4 — 执行历史存哪**（§9.3）

> **推荐：新建子集合 `tasks/{task_id}/runs/{run_id}`。**
>
> 理由：`bots/{name}/logs` 是**按 bot 切分的对话日志**，要回答「这个任务历次跑得怎么样」得全表扫加过滤。子集合天然按任务聚合，一次查询搞定，而且 Task 删除时能级联清理。
>
> 代价：多一个集合层级，以及 Firestore 子集合不会随父文档自动删除，需要显式清理。可接受。

**Q6 — Task 能不能挂多个 Job**

> **推荐：保持 0..1，不做多 Job。**
>
> 理由：目前想不出真实场景。「工作日早 8 点 + 每 30 分钟」这种双触发确实无法用单个 cron 表达，但也可以建两个 Task 指向同一个目标描述。为一个假想需求引入一对多关系，会让终止语义变复杂（删哪个 job 算任务结束？）。
>
> YAGNI。真遇到了再改，改动是加性的、不破坏现有数据。

**Q7 — 时区支持范围**（写 §14.1-B 测试用例时发现的）

> **推荐：只支持 `Asia/Hong_Kong` + `UTC`，传其他值在创建时直接报错。**
>
> 理由：任意 IANA 时区就得处理 DST 的两个恶心 case（春季不存在的挂钟时刻、秋季重复出现的挂钟时刻，见 T121/T122）。为一个当前不存在的跨时区需求承担这个复杂度不值。
>
> **关键是要报错而不是静默接受**。将来有人传 `America/New_York`，宁可他立刻看到「不支持」，也不要得到一个悄悄算错 1 小时的调度。
>
> 若采纳，T121-T123 降级为单条「传入带 DST 的 tz 必须显式报错」的测试。

---

## 12. 分阶段实施建议

> 每阶段独立可用，不必一次做完。

**P0 — 修坑（不引入新概念）**
- G4 时区修复 + 现存 job 迁移
- G1 job_id 进 instruction，agent 可自终止
- 这两条做完，US-1 和 US-2 **今天的架构就能跑**，只是没有 Task 对象

**P1 — Task 对象**
- `tasks` 集合 + `task-tool.py`
- 进度累积、状态机、notify 分档

**P2 — 执行隔离**
- isolated session（修 G3）
- 削权自删

**P3 — `every` kind + 锚点/补跑语义**
- 严格说 P0 之后就能用 cron 凑合表达「每 30 分钟」（`*/30 * * * *`），但那是挂钟对齐的、没有 anchor 语义、也无法退避。P3 补上真正的 `every`。
- 连续错误熔断（§7.3）在这一阶段一并做——它属于正确性，不是成本优化。

**P-later — 成本控制**
- 本期不做（§7 决策框）。将来要做时，前提条件（`next_fire_at` 越权通道）已在 P2 就位，加退避是纯增量改动。

---

## 13. 明确不做

- **事件驱动 armTimer**（§6.3）——收益不抵改造成本
- **声明式 `until` / 复杂条件表达式**——终止条件交给语义判断，不做 DSL
- **任务依赖图 / DAG**——本设计只做单任务。多任务编排是另一个 feature
- **把调度下沉到 worker 层**——保持在 BotCore 之上
- **给非 Firestore 后端做抽象**——YAGNI

---

## 14. 测试方案

### 14.0 两个为可测性而必须做的设计决定

**写测试方案时倒逼出来的两条实现约束，必须在 P0 就落地，否则后面补不回来。**

**T-0-A：所有时间计算函数接受注入式 `now`。**

```python
def next_fire(job: dict, now: datetime) -> datetime | None: ...
```

绝不在计算函数内部调 `datetime.now()`。否则 §14.1 的 20 多个时间用例只能靠 `sleep` 跑，一轮几十分钟，且不可能测 DST 和跨年。当前 `cron-tool.py` 的 `NOW = lambda: datetime.now(timezone.utc)` 是全局的，`next_cron_fire(expr, after)` 已经接受 `after` 参数——**这条基本已满足**，实现时保持住即可。

**T-0-B：`tick` 支持 `--dry-run`。**

输出「本次会触发哪些 job、各自算出的下一次是什么」但不写 inbox、不改状态。§14.2 的派发层测试和生产环境的排障都依赖它。

**测试隔离**：所有测试用 `scheduled_jobs` 里带 `test_run_id` 字段的文档，`tick` 正常路径必须过滤掉带此字段的条目。**绝不允许测试污染生产 job 集合**——现在生产里只有 2 条 weekly job，误触发会给 jarvis 发垃圾。

---

### 14.1 L1 — 时间计算（纯函数 / 无 LLM / 无网络 / 毫秒级）

**这是全系统风险最高的部分**，也是唯一能穷举的部分。全部用 freeze 的 `now` 驱动。

#### A. `cron` kind + 时区

| ID | cron | tz | now | 期望 next_fire_at (UTC) | 考点 |
|---|---|---|---|---|---|
| T101 | `0 8 * * *` | Asia/Hong_Kong | 2026-08-08 05:00Z | 2026-08-09 **00:00Z** | 基础：HKT 8 点 = UTC 0 点，**修 G4 的核心断言** |
| T102 | `0 8 * * *` | Asia/Hong_Kong | 2026-08-08 23:00Z | 2026-08-09 00:00Z | 跨 UTC 日界 |
| T103 | `0 8 * * *` | UTC | 2026-08-08 05:00Z | 2026-08-08 08:00Z | 显式 UTC 不受默认值影响 |
| T104 | `13 8 * * 1` | Asia/Hong_Kong | 2026-08-08 05:00Z (周六) | 2026-08-10 00:13Z (周一) | 周几语义 + 迁移后的真实 job |
| T105 | `0 8 * * MON-FRI` | Asia/Hong_Kong | 2026-08-07 05:00Z (周五) | 2026-08-10 00:00Z | 跳过周末 |
| T106 | `0 8 1 * *` | Asia/Hong_Kong | 2026-08-08 05:00Z | 2026-09-01 00:00Z | 月初 |
| T107 | `0 8 * * *` | Asia/Hong_Kong | 2026-12-31 20:00Z | 2027-01-01 00:00Z | 跨年 |
| T108 | `0 8 29 2 *` | Asia/Hong_Kong | 2026-03-01 00:00Z | 2028-02-29 00:00Z | 闰日（跨 2 年） |

#### B. DST（香港没有，但跨时区部署会中招）

| ID | cron | tz | 场景 | 期望 | 考点 |
|---|---|---|---|---|---|
| T121 | `30 2 * * *` | America/New_York | 春季跳时那天 2:30 不存在 | **不能崩**，按库语义顺延或跳过，行为需固化断言 | 不存在的挂钟时刻 |
| T122 | `30 1 * * *` | America/New_York | 秋季回拨那天 1:30 出现两次 | **只触发一次** | 重复的挂钟时刻 |
| T123 | `0 8 * * *` | Europe/London | 跨 BST 切换前后各一天 | 两天的 UTC 时刻相差 1 小时 | UTC 偏移随日期变 |

> 若最终决定「只支持 Asia/Hong_Kong 和 UTC」，T121-T123 可降级为「传入带 DST 的 tz 必须显式报错」的单条测试。**这是个待拍板项**——见 §11 新增第 7 条。

#### C. `every` kind + 锚点

| ID | interval | anchor | 场景 | 期望 | 考点 |
|---|---|---|---|---|---|
| T141 | 1800s | fire | 上次 fire 10:00，任务跑 10 分钟 | 10:30 | fire 锚点不受执行耗时影响 |
| T142 | 1800s | complete | 上次 fire 10:00，10:10 完成 | 10:40 | complete 锚点顺延 |
| T143 | 1800s | complete | 任务跑了 40 分钟（超过间隔） | 完成时刻 +30min，**不叠加** | 长任务不重叠 |
| T144 | 1800s | fire | 任务跑了 40 分钟（超过间隔） | 下一次已过期 → 立即触发一次，**不补两次** | fire 锚点的溢出处理 |
| T145 | 180s | complete | 首次创建，无 last_fire | now + 180s | 冷启动锚点 |

#### D. 补跑 / 陈旧（§5.5③）

| ID | kind | 场景 | 期望 | 考点 |
|---|---|---|---|---|
| T161 | every 1800s | 停机 2 小时，错过 4 次 | **只触发 1 次**，`fire_count` +1 | 合并，不放 4 个 turn |
| T162 | cron `0 8 * * *`, max_lateness=3600 | 停机到 8:30 才起 | 触发（迟到 30min < 1h） | 窗口内补 |
| T163 | cron `0 8 * * *`, max_lateness=3600 | 停机到 10:00 才起 | **跳过**，历史留 `skipped_stale` | 超窗跳过 |
| T164 | cron | 停机 3 天，错过 3 次日任务 | 最多触发 1 次（最近的且在窗内） | 绝不放 3 个 |

#### E. `at` kind 与非法输入

| ID | 输入 | 期望 |
|---|---|---|
| T181 | `--in 10m` | now + 600s |
| T182 | `--in 90` (无单位) | 明确报错或按秒，**行为要固化** |
| T183 | `--at 2020-01-01T00:00:00Z`（过去） | 创建时报错，不接受 |
| T184 | cron `0 25 * * *`（非法小时） | 创建时报错，不落库 |
| T185 | cron `*/7 * * * *` | 支持步长（现存 cancelled job 用过这个写法） |
| T186 | tz `Mars/Olympus` | 创建时报错 |

---

### 14.2 L2 — 派发层（假 bot / 不跑真 LLM / 秒级）

用 `--dry-run` + 一个 stub inbox，断言写出的 message 文档内容。

| ID | 场景 | 期望 | 对应缺口 |
|---|---|---|---|
| T201 | 任意 job 触发 | instruction 正文里**含 `job_id` 和 `task_id`** | **G1** |
| T202 | isolated 模式触发 | session key 形如 `sched:<task_id>`，不等于用户的 user_key | **G3** |
| T203 | 用户对话进行中，job 到期 | 两者**不争同一把** `_user_task_locks` | **G3** |
| T204 | notify=`on_change`，agent 无新进展 | **不发消息**，只写执行历史 | §8 |
| T205 | notify=`on_change`，agent 有新进展 | 发一条 | §8 |
| T206 | notify=`on_done` | 中间轮次静默，终止那次发一条 | §8 |
| T207 | agent 自己在 run 内发了消息 | 系统 fallback announce **跳过**，用户只收到 1 条 | §8 去重 |
| T208 | 连续失败 5 次 | Task → `failed`，job 停排，发一条告警 | §7.3 |
| T209 | 失败 4 次后成功 | `consecutive_errors` 归零 | §7.3 |
| T210 | 带 `test_run_id` 的 job | 生产 tick **不触发它** | §14.0 隔离 |

#### 削权自终止（含必需的负例）

| ID | 场景 | 期望 | 说明 |
|---|---|---|---|
| T221 | agent 删自己这条 job | 成功，job 状态 → `done` | 正例 |
| T222 | **agent 尝试删别人的 job** | **必须失败**，且留审计记录 | **负例，不可省** |
| T223 | agent 尝试列出所有 job | 只能看到自己那条 | introspection 自过滤 |
| T224 | agent 改自己的 `next_fire_at` | 成功（§5.2 越权通道） | 退避的基础 |
| T225 | agent 改别人的 `next_fire_at` | **必须失败** | 负例 |

> **为什么负例不可省**：参考 `feedback_fast-path-patch-checklist` —— 只测正例的 mock 测试会让权限绕过静默通过。T222/T225 是这套授权机制唯一的安全断言。

---

### 14.3 L3 — 端到端（真 bot / 真 LLM / 分钟级）

用**专用测试 bot** + 廉价模型 + 压缩到分钟级的间隔。每条用例都要能在 10 分钟内跑完。

**T301 — US-1 完整链路（最重要的一条）**

```
1. 建 Task：「每 60 秒查一次 /tmp/pool-test.txt，里面出现 FREE 就把内容发给我并终止本任务；
            没有就什么都不发。」
2. 前 2 轮：文件内容为 BUSY
   断言：触发了 2 次；用户侧收到 0 条消息（notify=on_change 生效）
3. 第 3 轮前：写入 FREE
   断言：agent 发了 1 条含 FREE 的消息
   断言：job 状态 → done，不再排下一次
   断言：Task 状态 → done
4. 再等 120 秒
   断言：没有新的触发
```

这条同时验证 G1（自终止）、§8（notify 分档）、§6.2（削权），是整个 feature 的验收核心。

**T302 — US-2 定点触发**：建一条 2 分钟后的 `at` job，断言在正确的挂钟时刻触发（用 HKT 表述，验证 G4 端到端）。

**T303 — daemon 重启幂等**：job 到期瞬间 kill 掉 cron-daemon 再拉起，断言**只触发一次**，不重复也不丢。

**T304 — 长任务不重叠**：`every 60s` + 一个要跑 150 秒的任务，断言同一时刻不会有两个 run 并存。

**T305 — 锁隔离实测**：定时任务触发的同时，用户在主对话发一条消息，断言两者都在合理时间内完成，没有一个卡住等另一个。

---

### 14.4 回归 — 迁移正确性的黄金判据

**T401（必须过，否则不许上线）**：

对现存 2 条生产 job，取迁移**前**的定义（`13 0 * * 1` / `7 0 * * 1`，无 tz，UTC 求值）和迁移**后**的定义（`13 8 * * 1` / `7 8 * * 1`，tz=Asia/Hong_Kong），用同一批 freeze 的 `now`（建议覆盖一整年、每周一个采样点）分别算 `next_fire_at`。

**两边结果必须逐一相等。**

这是判断「迁移是否等价」唯一可靠的方式——比人肉核对表达式强得多，也能自动发现 DST 之类的隐藏差异。

**T402**：迁移脚本必须幂等（跑两遍结果相同）。

**T403**：迁移后 `cron-tool.py list` 输出的时间用 HKT 展示，且与 message 文本里写的时间一致（消除 §5.5① 那种「表达式和说明对不上」的状态）。

---

### 14.5 成本验证（§7）— 随 P-later 一起延后

> 本期不实施（见 §7 决策框）。用例先留着，将来做退避/探针时直接用。
>
> **例外**：T505（24 小时 turn 数与 token 统计报表）建议**提前做**。它不依赖任何成本优化机制，只是把「现在到底烧了多少」变得可见。没有这个数，将来讨论要不要做优化就只能拍脑袋。

| ID | 场景 | 期望 |
|---|---|---|
| T501 | 退避方案：连续 3 次空轮 | 间隔从 3min 拉到 10min，再 3 次拉到 30min |
| T502 | 退避方案：空轮后出现结果 | 间隔**重置回初始值** |
| T503 | 探针方案：探针判定无变化 | **不拉起完整 agent turn**，只记一次探针调用 |
| T504 | 探针方案：探针判定有变化 | 升级为完整 turn |
| T505 | 24 小时干跑统计 | 实际 turn 数 ≤ 预算；输出每条 job 的 turn 数与 token 消耗 |

T505 建议做成一个可重复运行的报表，而不是一次性测试——**成本是会随任务增多而漂移的**，需要持续可观测。

---

### 14.6 明确不测

- **LLM 判断「有没有票 / 有没有机器」的准确率** —— 这是 prompt 质量问题，不是调度器的责任。调度器只保证「按时把 agent 叫起来」。
- **真实的 DST 切换** —— 用 freeze time 模拟（T121-T123），不等半年。
- **Firestore 本身的可用性** —— 假定它是可靠的；连续错误熔断（T208）已覆盖它不可靠时的行为。
- **多 bot 并发抢同一条 job** —— 当前设计里每条 job 只有一个 `target`，不存在竞争。若将来支持 job 池化，再补。

---

## 附录 A：调研证据索引

| 结论 | 来源 |
|---|---|
| OpenClaw cron 三种 kind | `dist/plugin-sdk/src/gateway/protocol/schema/cron.d.ts` |
| OpenClaw armTimer 事件驱动 | `dist/server-cron-CKQu4czX.js` |
| OpenClaw 削权自删 | `dist/plugin-sdk/src/agents/tools/cron-tool.d.ts` `selfRemoveOnlyJobId` |
| OpenClaw 状态分离 | `~/.openclaw/cron/{jobs.json, jobs-state.json, runs/}` |
| OpenClaw 文档 | `~/.npm-global/lib/node_modules/openclaw/docs/automation/cron-jobs.md` |
| Claude Code scheduled tasks | https://code.claude.com/docs/en/scheduled-tasks |
| Goose scheduler | `crates/goose/src/scheduler.rs`（1736 行，tokio_cron_scheduler） |
| Goose agent 自管工具 | `crates/goose/src/agents/schedule_tool.rs` |
| OpenHarness cron | HKUDS/OpenHarness `services/cron.py` + `services/cron_scheduler.py` |
| Crush 调度 PR | charmbracelet/crush PR #3466（2026-07-31，未合） |
| OpenCode 缺失 | FR #11232 / #36676 / #39514，maintainer 未表态 |

> **证据强度提示**：OpenClaw 的结论基于本机安装的构建版（`~/.npm-global/lib/node_modules/openclaw`，2026-05-20 安装），上游可能已变化；`maxRuns` 缺失是 dist grep 的阴性结论，权重低于源码仓库确认。
