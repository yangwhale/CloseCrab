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

- `grep` > `read + 正则`（grep 有 ripgrep 加速）
- `glob` > `bash find`
- 内置工具 > MCP（MCP 多一次 IPC）
- 本地能算 > 联网查（不要拿 `webfetch` / `search_web` 查本地事实）
- 能用 `cat` 别用 `read`，能用 1 次 bash 别拆多次

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

---

## 通用工具脚本（worker-agnostic）

以下 4 个脚本任何 worker 都能用 `bash` 调用：

```bash
# 真并行 N 个 LLM sub-agent（每个独立推理 + bash + read 工具）
python3 ~/CloseCrab/scripts/subagent-parallel.py --inline '{"tasks":[{"label":"A","prompt":"..."}]}'

# 定时提醒 / cron（精度 30s，daemon 自动跑）
BOT_NAME=$BOT_NAME python3 ~/CloseCrab/scripts/cron-tool.py add \
  --target $BOT_NAME --in 10m --message "..."
# 也支持 --at <ISO UTC> 或 --cron "0 9 * * MON-FRI"
BOT_NAME=$BOT_NAME python3 ~/CloseCrab/scripts/cron-tool.py list
BOT_NAME=$BOT_NAME python3 ~/CloseCrab/scripts/cron-tool.py remove <job_id>

# 自查状态（model / cost / token / 历史 turns）
python3 ~/CloseCrab/scripts/session-status.py $BOT_NAME [--days N]

# 图片生成（Gemini 3 Pro Image）
~/CloseCrab/skills/imagen-generator/scripts/imagen-generate.sh "prompt" --aspect 16:9

# 语音生成（Gemini 3.1 Flash TTS，15 个声音 + 情绪标签）
OGG=$(~/CloseCrab/skills/tts-generator/scripts/tts-generate.py "[casually] hello")
echo "<voice-file>$OGG</voice-file>"   # 飞书 channel 会自动上传为语音消息
```

用户说"用什么模型 / 今天花了多少" 走 session-status；说"10 分钟后提醒我" 走 cron；说"画一张图" 走 imagen；说"读出来 / /tts" 走 tts。
