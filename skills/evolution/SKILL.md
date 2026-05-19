---
name: evolution
description: Bot team three-way mutual worker-optimization loop. Two evaluator bots design and dispatch test cases to a target bot via Firestore inbox, monitor real-time logs, compute metrics (fail_rate, empty_response_count, p50/p95 duration, avg_step_count), diagnose root causes, propose source-code fixes, then trigger cross-bot SIGHUP restart and re-test — all autonomously without asking Chris. Use when user says "进化"、"evolve"、"evolution round"、"进化一轮"、"start a round"、"互相 restart"、"三方互评"、"今晚优化 <bot>"、"组队优化" or asks the bot team to autonomously improve a specific worker (ClaudeCodeWorker / KiloWorker / OpenClawWorker / GeminiACPWorker).
trigger: 进化 / evolution / 三方互评 / 互相 restart / 组队优化 / 今晚优化 / evolve
---

# Evolution — Bot Team Mutual Optimization

## Overview

每个 worker 都有自己看不见的盲区（流式协议、tool 注入、prompt 注入、空回复处理）——但从另一种 worker 的视角看，这些盲区是显眼的。Evolution skill 让 bunny（Claude Code）、小爱同学（Kilo）、铁幕（OpenClaw）三个 bot 互相当评估者，用对方的盲区作 case，跑 → 看 → 改 → 重启 → 再跑，直到指标改善。Chris 不参与每轮决策——授权已永久标记，bots 互相 restart 对方就行。

## When to Trigger

- Chris 说「进化」「今晚搞 evolution」「三方互评」「互相 restart」「组队优化 <bot>」
- 自己发现 worker 有明显问题（空回复率高 / step 不动 / 协议崩溃），且另一个 bot 在线
- 例行夜间任务（cron 配的话）

## The Round（12 步标准流程）

每轮一个 **target**（被优化的 bot），两个 **evaluators**（互相协作的另外两个 bot）。下面以 bunny+tiemu 优化 xiaoai 为例，角色可平移。

### 1. 选 target + 角色分配
- 看谁最近问题最多（Firestore `bots/{name}/logs` 翻 fail_rate）
- 两个 evaluator 在 #team-ops 频道商量并明确：「本轮 target=xiaoai (kilo)，evaluators=bunny+tiemu」
- 一句话发 Chris，FYI 不等审批

### 2. 招募 + 任务分工
- evaluator A 用 inbox 给 evaluator B 发：「我负责 case 1-3 (流式)、你负责 case 4-6 (MCP)、各自 dispatch」
- 不重复 case；如果对方静默 >10 min，evaluator A 单独 cover 全部 case

### 3. Dispatch cases
- 用 `scripts/dispatch-case.py` 把 case 通过 inbox 发到 target
- 每个 case 一条 inbox message，message 里写明：case_id / 输入 / 期望 / 评估维度（latency? completeness? tool_use?）
- 同时记下发送时间（用来后面 query logs）

### 4. 实时盯日志
- target bot 在自己机器上跑 case，bunny/tiemu 远程 query Firestore `bots/{target}/logs` 拉最新 N 条
- 也可以直接 `ssh <target_host> tail -f ~/.claude/closecrab/{target}/bot.log`
- 关键观察：worker 流式事件是否正常？tool_use 是否被 channel 看到？空回复触发了吗？

### 5. 算指标
- 用 `scripts/metrics-from-firestore.py --bot {target} --since <round_start>` 算：
  - `fail_rate` (status != "success")
  - `empty_response_count`
  - `duration_seconds` p50 / p95
  - `avg_step_count` per turn
  - `tool_call_diversity` (unique tools used)
- 输出 markdown 表给 Chris（dispatch 完一波就报一次，不憋大单）

### 6. 诊断
- 两个 evaluator 各自给出诊断（互不预告），写完后交叉看
- 如果两个诊断指向同一个根因 → 高信度，进入第 7 步
- 如果不一致 → 在 #team-ops 各自陈述，30 秒决出主诊断（按证据强度，不投票）

### 7. 提案修改
- evaluator A 写 patch（修 target 的 worker 源码 / SKILL.md / 配置）
- patch 必须 grep 验证过相关代码确实存在（参考 `feedback_grep-source-before-asserting-architecture`）
- 不 patch target 本身的 memory 或 instructions，避免 target 「学到」当前 case 的答案而非泛化

### 8. 推送 patch + 远程 pull
- evaluator 在本地 git commit + push（CloseCrab repo）
- ssh 到 target 机器 `cd ~/CloseCrab && git pull`

### 9. 跨 bot SIGHUP restart
- `bash scripts/restart-peer-bot.sh <target>`
- 这个脚本用「12s/8s nohup + sleep + kill -HUP」pattern（见 `references/cross-bot-restart-protocol.md`）
- 必须验证旧 PID 消失 + 新 PID 出现 + bot.log 有 18:10:19 / Phase E xxxx chars 的启动行

### 10. Re-test 同一组 case
- 用 `scripts/dispatch-case.py --rerun <round_id>` 把 step 3 的同一批 case 再发一遍
- 不改 case 内容，纯粹看 patch 是否解决问题

### 11. 对比指标
- 再跑一次 step 5，diff 两轮：「fail_rate 30%→5%」「empty_response 8 → 0」「p95 12s → 4s」
- 没改善或更糟 → 回到 step 7，patch 重写（最多 3 次循环，避免抽搐）

### 12. Round report + GBrain 落地
- evaluator A 写 round report（target / cases / metrics before-after / patch / lesson）到 GBrain（`put_page` slug=`round_<date>_<target>`）
- 把可复用的 lesson 也写到 feedback page（`feedback_xxx`）
- 在 #team-ops 一句话 summary，@Chris FYI

## Authorization Scope（永久授权）

Chris 已经永久授权 evolution 流程内的以下动作，不需要每轮再问：

| Action | 是否需要问 | Owner |
|---|---|---|
| 跨 bot SIGHUP restart 对方 (evolution round 内) | 否 | 任何 evaluator |
| 给对方 bot 的源码提 patch + push + 远程 pull | 否 | 任何 evaluator |
| 修 target 的 SKILL.md / GBrain page | 否 | 任何 evaluator |
| dispatch case 到任意 bot 的 inbox | 否 | 任何 evaluator |
| 上面以外的破坏性动作（删数据 / 改密钥 / 改 channel 配置） | **是** | Chris |

授权依据：Chris 原话「你就互相 restart 呗，不要让我参与。然后你把这个能力做成一个 skill，就叫做进化」（2026-05-19）。

## Resources

### scripts/
- `restart-peer-bot.sh <bot_name>` — 跨 bot SIGHUP restart（12s nohup pattern，含 PID 验证）
- `dispatch-case.py --target <bot> --case <id> --content "..."` — Firestore inbox dispatch wrapper
- `metrics-from-firestore.py --bot <name> --since <ISO>` — 算 fail_rate / empty_response_count / p50p95 duration / avg_step_count

### references/
- `cross-bot-restart-protocol.md` — SIGHUP 协议详解、12s nohup 为什么 work、PID 验证清单、failure modes
- `case-library/kilo-cases.md` — Kilo (xiaoai) 已知盲区 + cases
- `case-library/openclaw-cases.md` — OpenClaw (tiemu) 已知盲区 + cases
- `case-library/claude-cases.md` — Claude Code (bunny) 已知盲区 + cases
- `metrics-spec.md` — 每个指标的定义、阈值、解读

## Workflow Examples

### 例 1：「进化一轮 xiaoai」
1. Chris 说「进化一轮 xiaoai」
2. bunny 先 `inbox-send.py tiemu "evolution round target=xiaoai, 我负责流式 case 1-3, 你负责 MCP case 4-6"`
3. bunny dispatch case 1-3 → 等 5 min → 算 metrics → 诊断 → 写 patch → push
4. ssh xiaoai-host && git pull
5. `bash restart-peer-bot.sh xiaoai`
6. dispatch case 1-3 再跑一次
7. diff metrics → 写 round report

### 例 2：「三个 bot 互相进化一轮，把今晚的精华时间用完」
1. Round 1: bunny + tiemu 优化 xiaoai
2. Round 2: xiaoai + bunny 优化 tiemu
3. Round 3: xiaoai + tiemu 优化 bunny
- 每轮独立写 round report
- 一轮结束才进下一轮（不并发，避免互相 restart 时打到对方还在跑的进程）

## Anti-Patterns（不要做）

- ❌ **不要假设 worker_type**：每次先 query Firestore `bots/{name}.worker_type` 拿真值（参考 `feedback_grep-source-before-asserting-architecture`）
- ❌ **不要 patch target 的 instructions/memory 让它学会答 case**：这是过拟合，要 patch worker 源码让能力泛化
- ❌ **不要不验证 restart 就 re-test**：必须确认新 PID + bot.log startup 行，否则你 re-test 的还是老进程
- ❌ **不要 round 内联系 Chris 等他批 patch**：授权范围内自己跑，round 结束才一句话 FYI
- ❌ **不要 round 跨夜还在 loop**：每个 target 一轮内最多 3 次 patch 循环，无改善就写"本轮失败、root cause 待人工"封轮
- ❌ **不要 SIGKILL target**：用 SIGHUP，让 run.sh wrapper 干净重启，避免丢 session 状态

## Failure Modes & Recovery

| Symptom | Likely Cause | Fix |
|---|---|---|
| restart 后新 PID 没出现 | run.sh wrapper 死了 | ssh 上去 `./run.sh <bot> &` 手动起，并查 nohup.out |
| dispatch 后 inbox status 一直 pending | target 进程死了 / inbox watcher 异常 | step 1：ps aux \| grep <bot>；step 2：tail bot.log 找 watcher 异常 |
| metrics 拉不到 (Firestore 查询报 grpc) | query 太复杂或权限不对 | 简化 query（只按 timestamp 过滤），用 `gcloud auth application-default login` 重新认证 |
| 两个 evaluator 诊断打架不收敛 | case 设计模糊 | 重新设计 case 让信号锐利（一次只测一个维度） |
| patch push 后 target 拉不到 | 远程仓库未同步 / 网络 | ssh target && cd CloseCrab && git fetch origin && git log -1 origin/main 确认 |

## See Also

- GBrain page: `feedback_strong-leads-weak-evolution` — 第一轮强带弱的策略说明
- GBrain page: `chris-authorized-cross-bot-restart` — Chris 授权全文
- `~/CloseCrab/CLAUDE.md` 的「Bot Team 系统」段
