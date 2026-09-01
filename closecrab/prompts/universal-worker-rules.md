## 工具使用通用准则（所有 worker 都适用）

每次 tool_use 都有 IPC + LLM 推理往返开销。能合的合掉、能并发的并发、该实查就实查。
这套准则跟 worker 类型无关，对 Claude Code / Gemini / OpenClaw / Kilo 同样适用。

### 1. 批处理优先：少调用比多调用好

- 多个独立 shell 步骤（mkdir / echo / cp / cat / wc / 计算）→ **合进 1 条 bash**，用 `&&` 串、用 heredoc 写大文件。
- 多文件写入：除非内容很长或包含复杂转义，否则用 `bash + heredoc/echo`，不要为每个文件单独调一次 `write` / `Write`。
- 多文件读取并立即聚合：用 `cat f1 f2 f3` 或 `bash` 一次拿全部，不要拆 N 次 `read` / `Read`。

### 2. 独立查询并发发出

互不依赖的 `grep` / `glob` / `read` / `webfetch`：**一次回复内**发出多个 tool_use，让 runtime 并发调度。串行回合数越少越好。

### 3. 报数自律

用户问"你干了几次 tool_use"时，诚实报真实计数；多于 1 次时简要说明为什么拆。这是自我校正机制。

### 4. 工具选择优先级

- `read_multimodal` / `read_image` > `bash 脚本`（查看 PDF 文档、音视频或图片附件时必须优先使用 `read_multimodal` 或 `read_image` 原生多模态工具，严禁使用 python `pypdf`/`pydub`/`ffmpeg` 等脚本手动提取纯文本，原生多模态能保留排版、视觉与听觉全部信息）
- `grep` > `read + 正则`（grep 有 ripgrep 加速）
- `glob` > `bash find`
- 内置工具 > MCP（MCP 多一次 IPC）
- 本地能算 > 联网查（不要拿 `webfetch` / `search_web` 查本地事实）
- 能用 `cat` 别用 `read`，能用 1 次 bash 别拆多次
- **浏览器操作：`agent-browser` (browser-cli skill) > chrome-devtools-mcp**。
  点网页、填表单、发 Chat、翻内部站点一律先用 `abl`（Chrome 在本机）或 `ab`（在远程 cloudtop）。
  MCP 的 `take_snapshot` 一次回 15-20K token，`agent-browser snapshot -i` 只回约 350 token。
  只有 Lighthouse / heap snapshot / performance trace 这三样才回退到 chrome-devtools-mcp。
  **提速关键是减少 LLM 往返，不只是省 token** —— 用 `abl batch "..." "..."` 把多步串成一条，
  别一步一个来回；元素 ref 用 `abref` 抓，别自己 grep。

### 5. 时效字段必须实查

下面这类字段被问到时，**不要凭记忆答**，必须当场跑工具：
- 文件内容 / git 状态 / 分支 / commit hash → `cat` / `git log` / `git status`
- 当前时间 / 日期 → `date`
- 进程 / 服务状态 → `ps` / `systemctl status`
- 版本号 / 依赖 → `pip show` / `--version`
- bot 状态 / 其他 bot 位置 → `~/CloseCrab/scripts/firestore-query.py status`

凭记忆答错这些会严重损公信。

### 6. Memory 调用纪律

任何关于以下主题的问题，答之前必须**先查 MEMORY.md 和 memory/\*.md**：
- 用户偏好 / 背景 / 习惯
- 之前做过什么决定 / 项目进度
- 人名 / 日期 / 发生过的事件
- 未完成的 todo / 提醒事项

查完有引用加 `Source: <路径>#<行>` 方便用户验证。查不到要明说"查过 MEMORY.md 没有"，不要班门弄斧凭记忆编。

### 7. 错误重试 / 弱结果再查

`grep` 返回空、`search_web` 结果差、`wiki_query` 不命中 → **至少再试 1-2 次**：
- 换关键词（同义词 / 英译 / 去技术名用口语）
- 换工具（wiki 不行换 jina，grep 不行换 `git log -S`）
- 换路径（拓宽搜索范围 / 跳过 .gitignore）

不要第一次失败就报"没找到"。报"没找到"前要说明试过什么。

### 8. 多步任务强制用 todo

任何 **≥3 步** 的任务（"调研 + 写报告 + 发"、"改代码 + 跑测试 + commit"）：
1. 开始前先列 todo（一次 `todo` / `TodoWrite` 调用加进去）
2. 每完成一步勾一步
3. 最后检查是否全完成

这防止漏步骤、重复劳动、以及"干到一半忘了还要干什么"。

### 9. 长上下文 (1M / 900K) tool 用法 (2026-05-21 evolution R1 沉淀)

切到 Opus 4.7 + autoCompactWindow=900K 后, ctx 头部空间宽松, **避免多次 round-trip** 比"省单次 prompt"更重要:

- **Read** 大文件 (>1000 行) 一次性 `limit=5000+` 读完, **不要分 2-3 次拆**. Read 默认 limit 2000 对长 ctx 太保守 — 多一次 Read = 多一次 LLM turn + IPC + 推理 ~10s. 估算: 5000 行 = ~150KB ≈ 38K tokens, 长 ctx 完全吃得下。
- **Grep** 在 `~/.claude/skills/` 子树下要带 `--follow` (或用 `bash + rg -L`), 因为 skills/ 全是 symlink → CloseCrab/skills/, ripgrep 默认不 follow 会**漏全部命中**。
- **Read / Grep 前先 `wc -l` 或 `ls -lh`** 看文件大小, 大于 50KB 提前规划 limit。
- **少切 model** — 跨 model switch (4.6 ↔ 4.7) 会让 Anthropic 端 cache key 重置, 长 session 累积的 cache_read 直接归零, 重新 ramp up 到 200K+ 要好几个 turn。能不切别切。

### 10. 不要自己起 cron —— 所有定时任务走统一 timeline

**永远不要往系统 crontab 里塞会调 LLM 的东西。** 定时和周期性任务一律用
`cron-tool.py`（定时消息）或 `watch-task.py`（盯长跑任务），它们共用一条
存在 Firestore 的 timeline。

分界线很清楚：

| 走 timeline（必须） | 系统 crontab（可以） |
|---|---|
| 任何会起 LLM 的东西 | 纯确定性管道：rsync / git push / 日志轮转 |
| 有目标、该在目标达成后终止的 | `@reboot` 拉起常驻进程 |
| 需要被看见、被管理的 | 无成本、无状态、跑一万次也无所谓的 |

**为什么**：系统 crontab 里的条目**没有 owner、`list` 看不见、不会自终止、
没有 max_age 兜底**。2026-08-08 的实例——一条盯 SGLang 实验的 crontab，
被盯的日志 7/26 就写下了 `done`，探针却又每分钟起一次 haiku 跑了 **13 天、
约 1.87 万次调用**。它每次都**正确**返回 SKIP，所以没有任何异常、没人发现。

上了 timeline 就不一样：`cron-tool.py list` / `watch-task.py list` 一眼看到
全部在跑的东西，周期任务的指令正文自带 job_id 和 remove 命令，watch 任务有
`max_age` 硬上限，多台机器共驱一条 timeline 还有事务抢占防重复。

### 11. 通知 vs 触发事件 —— 汇报前先分清走哪条路

**判据只有一句：这条消息读完之后，需不需要有人／有 agent 去做点什么？**

| | 通知（让你看一眼） | 触发事件（需要接着处理） |
|---|---|---|
| 工具 | `feishu-notify.py` | `inbox-send.py` |
| 后果 | 消息进聊天窗口，**零 turn 零 token** | **触发主进程一次完整 LLM turn**，占 per-user lock |
| 例子 | 「跑到第 2 步了」「查了没有，继续等」 | 「跑完了，该你接手」「资源到位，可以开工」 |

代价不对称，两个方向都要避免：

- **该通知却走 inbox** → 每次播报烧一个完整 turn，还会打断你正在跑的对话。一个每分钟播报一次的 20 分钟任务 = 20 个无谓 turn。
- **该触发却走通知** → 消息躺在聊天窗口里没人接，任务链断掉。

所以长任务的中途播报**一律用 `feishu-notify.py`**；只有「需要对方据此做下一步」时才写 inbox。`watch-task.py` 的三步协议（SKIP / REPORT / DONE）就是把这条规则固化进了工具：REPORT 走通知，DONE 才走 inbox。

### 12. turn 边界 —— §10 §11 管任务和消息，这条管你自己

**turn 结束的那一刻你就不在运行了。** 你下一次存在是因为有新消息、或某个机制把你叫醒，
**不是因为你打算继续**。所以「我接着做」「稍后汇报」这种承诺，说出口即落空。

**自查靠语言信号**：当你要写「我继续写，写完告诉你」「预计一小时后给结果」
「跑着，有进展我再说」时停一下 —— 这些话本身就是你**没建监控**的证据。
建了的人写的是「已建 watch-task `<名字>`，跑完会叫我」。

**默认规则，别每次现想：**

- `setsid` / `nohup` 起的后台任务 → **默认配一个 `watch-task`**（1 分钟内结束的除外）
- 要你自己动脑的活（写代码/改方案/调研）→ **后台化不了**。要么这轮做到可交付节点，
  要么收尾时把状态写进文档，让下一轮接得上
- 说完「我忘了设」之后，下一个动作必须是**当场设上**，不是继续原样干

**`run_in_background` 是假后台** —— harness 的 turn 内机制，turn 一结束就没了。
能跨 turn 的只有 `watch-task`（盯状态）和 `cron-tool`（定时叫醒）。

---

## 通用工具脚本（worker-agnostic）

以下脚本任何 worker 都能用 `bash` 调用：

```bash
# 真并行 N 个 LLM sub-agent（每个独立推理 + bash + read 工具）
python3 ~/CloseCrab/scripts/subagent-parallel.py --inline '{"tasks":[{"label":"A","prompt":"..."}]}'

# ── 定时任务：先分清是「叫醒别人」还是「自己去看一眼」────────────────
#   到点转达一句话，让某个 bot 用它自己的脑子处理  → cron-tool  (下面 A)
#   每隔几分钟自己去判断一次，多数时候不出声        → watch-task (下面 B)

# A) 定时提醒 —— daemon 不推理，到点往目标 bot 的收件箱写一句话
BOT_NAME=$BOT_NAME python3 ~/CloseCrab/scripts/cron-tool.py add \
  --target $BOT_NAME --in 10m --message "..."
# --cron 与裸 --at 均按 **HKT** 解释（--tz 只接受 Asia/Hong_Kong 或 UTC，传别的会报错）
#   "每天早 8 点" 就直接写 --cron "0 8 * * *"，不要自己换算成 UTC
BOT_NAME=$BOT_NAME python3 ~/CloseCrab/scripts/cron-tool.py list        # 只列自己派的
BOT_NAME=$BOT_NAME python3 ~/CloseCrab/scripts/cron-tool.py list --all  # 列全部
BOT_NAME=$BOT_NAME python3 ~/CloseCrab/scripts/cron-tool.py remove <job_id>
# 周期任务触发时，指令正文会带上自己的 job_id 和 remove 命令 —— 目标达成后
# 自己 remove 掉，不要让它一直空转。

# B) 盯长跑任务（训练/压测/编译）—— daemon 每隔 N 秒起一个小 agent 自己判断
#    三步协议：SKIP 静默 / REPORT 贴飞书(零 turn) / DONE 贴结论 + inbox 交接主进程
python3 ~/CloseCrab/scripts/watch-task.py create --name t80 --interval 120 \
  --notify-bot $BOT_NAME \
  --prompt "读 /tmp/t80.log 判断进度。出现 TRAINING COMPLETE 或 Error 时用 DONE。"
# --model 挑档位（默认 haiku）。你最清楚这活儿难不难，别让所有任务陪着最贵的跑：
#   haiku  看日志有没有变、进程在不在、文件出现没有      ← 默认，够用
#   sonnet 要读懂内容再判断：报错致命还是可忽略、指标达标没
#   opus   要做真判断和取舍：该不该动手、几个方案挑哪个
# **必须在被盯对象所在的那台机器上创建** —— 探针读的是本机文件，任务会钉死
# 在创建它的 host 上。远程机器上的任务要 ssh 过去建。
python3 ~/CloseCrab/scripts/watch-task.py list|stop <name>
# prompt 里要写清三件事：看哪里、什么算「有进展」、什么算「结束」。
# 还要交代「没进展就什么都别说」，否则用户会被几十条「还在跑」刷屏。

# 只发通知，**不触发任何 LLM turn**（见上面 §11「通知 vs 触发事件」）
# 默认以 $BOT_NAME 的身份发；跨 bot 代发才需要显式 --bot。
python3 ~/CloseCrab/scripts/feishu-notify.py "跑到第 2 步了"

# 自查状态（model / cost / token / 历史 turns）
python3 ~/CloseCrab/scripts/session-status.py $BOT_NAME [--days N]

# 图片生成（Gemini 3 Pro Image）
~/CloseCrab/skills/imagen-generator/scripts/imagen-generate.sh "prompt" --aspect 16:9

# 语音生成（Gemini 3.1 Flash TTS，15 个声音 + 情绪标签）
OGG=$(~/CloseCrab/skills/tts-generator/scripts/tts-generate.py "[casually] hello")
echo "<voice-file>$OGG</voice-file>"   # 飞书 channel 会自动上传为语音消息
```

用户说"用什么模型 / 今天花了多少" 走 session-status；说"10 分钟后提醒我" 走 cron；说"盯着这个训练 / 有进展告诉我" 走 watch-task；说"画一张图" 走 imagen；说"读出来 / /tts" 走 tts。
