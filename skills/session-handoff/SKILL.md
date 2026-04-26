---
name: session-handoff
description: Generate a handoff prompt that lets a fresh Claude Code session resume a complex task from a crashed/restarted CloseCrab bot session. Reads the old session jsonl plus the bot's recently-written CC Pages docs, extracts task identity / latest state / file locations / pitfalls / team coordination signals, and synthesizes a concise markdown prompt to paste into the new session. Use when a CloseCrab bot's session crashed (API error, context overflow, OOM-killed), or when the user says "wrap up this context for a new session", "give me a handoff prompt for {bot}", "{bot} 的 session 挂了写个交接", or "总结一下让新 session 接着干".
---

# Session Handoff

Generate a handoff prompt so a fresh Claude Code session of a CloseCrab bot can resume a long-running task without re-asking the user for context.

## When this triggers

- A bot's session crashed (API Error, context too long, OOM-killed bot.py)
- User wants to restart a bot mid-task and preserve state
- User says variations of: "give me a prompt for {bot}", "{bot} session 挂了，写交接", "总结一下让新 session 接着干"

## Workflow

```
1. Run extract_session.py {bot}        →  structured markdown extract
2. Read top 1-2 most recent CC Pages   →  ground truth latest state
3. Synthesize 8-section handoff prompt →  markdown ready to paste
4. Output the prompt in chat           →  optionally save HTML brief
```

### Step 1: Extract session signals

```bash
python3 ${SKILL_DIR}/scripts/extract_session.py {bot_name}
```

Or with explicit jsonl:

```bash
python3 ${SKILL_DIR}/scripts/extract_session.py --jsonl /path/to/session.jsonl
```

The script emits markdown sections:
- **User Messages Timeline** — every `[from: ...]` user msg in order. Mine task evolution from this.
- **Last N Records** — final tool calls / assistant turns. Confirms where it crashed.
- **Documents — cc.higcp.com Pages Mentioned** — URLs the bot already wrote/referenced.
- **Recent Local cc-pages files** — mtime-sorted local HTML files. ⚠️ The newest one is often newer than the jsonl — it has the freshest state.
- **GitHub URLs / Files Worked On / GCS Paths** — code locations, including `/lustre/`, `/workspace/`, `/tmp/claude/` paths.
- **Git Signals** — branches and commit hashes mentioned.
- **Version Iterations** — `v1` ... `vN` sequence (telling for "what version is current").
- **Errors / Crash Points** — API errors, OOMs, tracebacks.

### Step 2: Read the freshest CC Pages

Identify the 1-2 most recent HTML files from the "Recent Local cc-pages files" section. These are usually the bot's own status/plan documents written after the most recent debugging round.

```bash
# Strip HTML tags for plain-text reading
python3 -c "
import re, html
raw = open('/usr/local/google/home/chrisya/gcs-mount/cc-pages/pages/{file.html}').read()
t = re.sub(r'<script.*?</script>', '', raw, flags=re.DOTALL)
t = re.sub(r'<style.*?</style>', '', t, flags=re.DOTALL)
t = re.sub(r'<[^>]+>', ' ', t)
print(html.unescape(t)[:8000])
"
```

Why this matters: the jsonl captures conversation, but the bot often writes the *final* state ("✅ commit d905b434 pushed", "40L sanity PASS", "next: extend to 61L") into an HTML doc *after* the last conversation turn. Trust the HTML.

### Step 3: Synthesize the 8-section prompt

Read [references/handoff-template.md](references/handoff-template.md) for:
- The 8 required sections
- The format skeleton
- Synthesis hints for picking content from the extract

The 8 sections (中文 by default — matches user's working language):

1. 角色 + 任务一句话
2. 当前最新状态（带 ✅❌⏳ + 具体数字）
3. 必读文档链接（按优先级）
4. 关键文件位置（已 patched，不要重写）
5. 绝对不要再踩的坑
6. Node pool / 资源现状
7. 团队 + 沟通规则
8. 第一步具体行动清单（精确到 kubectl / git / curl 命令）

Target length: ~80 lines. If detail overflows, link to a HTML brief.

### Step 4: Output to user

Print the prompt as a fenced markdown block in chat, ready to copy-paste.

## Key principles

**The extract is mechanical, the synthesis is judgmental.** The script gives you raw signals. Picking which user message captures the "real task", which version is current, which pitfalls actually matter — these all require reading and judgment.

**Trust HTML > jsonl for state.** The bot writes its final status to CC Pages after the last conversation turn. Use the HTML for §2.

**Mine pitfalls from user corrections.** Search user messages for "不要", "千万别", "为什么", "❌", "你这个怎么..." — these are pitfall-shaped statements where the user told the bot to stop doing something.

**Concrete > abstract.** §8 first-step list should have actual commands the bot can run, not "check the status".

**Don't write HTML unless asked.** Default output is the markdown prompt only. Generate a HTML brief on cc.higcp.com only if the user explicitly asks for one or if the prompt would otherwise exceed 100 lines.

## What this skill does NOT do

- Does not SSH to remote machines (assumes everything is on local gLinux)
- Does not modify `sessions.json` (read-only)
- Does not kill the old session process
- Does not auto-restart the bot (user does that via `/restart` in 飞书)
