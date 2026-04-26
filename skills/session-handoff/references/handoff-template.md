# Handoff Prompt Template — 8 Sections

The output is a single markdown block. Paste it directly to a fresh Claude Code session
of the same bot, and the bot can resume work without losing context.

## Why these 8 sections

A new session has zero memory. To resume effectively, it needs:

| § | Section | Purpose |
|---|---|---|
| 1 | 角色 + 任务一句话 | Bot identity + the one-sentence goal |
| 2 | 当前最新状态 | What's done (✅) / blocked (❌) / next (⏳), with concrete numbers |
| 3 | 必读文档链接 | The 1-3 most recent CC Pages — these contain the deepest context |
| 4 | 关键文件位置（已 patched）| Avoid re-doing work that's already on disk |
| 5 | 绝对不要再踩的坑 | Pitfalls extracted from user corrections + actual debugging |
| 6 | Node pool / 资源现状 | What infrastructure exists and what's contested |
| 7 | 团队 + 沟通规则 | Who else is involved, how to coordinate, response cadence |
| 8 | 第一步具体行动清单 | Concrete kubectl/git/curl commands to start with |

## Format rules

- Use 中文 (matches user's working language)
- Use ✅ ❌ ⏳ 🚨 emojis sparingly — only for status markers
- Concrete numbers: HBM GB, layer counts, commit hashes, timings
- File paths must be absolute
- For each "don't do X" pitfall, also write the WHY (the user almost always told us)
- Keep total length 60-100 lines; if longer, push detail into a linked HTML brief

## Template skeleton

````markdown
你是 {BOT_NAME}，跑在 {HOSTNAME} 上，
负责 {ONE-SENTENCE PROJECT IDENTITY}.

【任务】{ONE-SENTENCE OUTCOME — what "done" looks like}

【立刻读这 N 份文档接续上下文 — 必须读完整，不要跳过】
1. {URL}  ({1-line summary of what's in it})
2. ...

【最近一次成功的状态 ({DATE TIME})】
✅ {milestone 1 with concrete numbers}
✅ {milestone 2}
⏳ 下一步：{specific next step with ETA}

【关键文件位置（已 patched，不要重写）】
- {path}: {one-line purpose}
- ...
- git: branch {BRANCH} @ {COMMIT} on {REMOTE}

【绝对不要再踩这些坑】
❌ {pitfall 1} ({why — usually a past failure})
❌ {pitfall 2}
✅ {positive pattern that works} (env var / flag / config that must be set)

【Node Pool / 资源现状】
- {pool name}: {topology, who's on it, status}
- 不够就 {fallback action}

【团队 + 沟通】
- Leader: {name} ({channel})
- 同组: {names with one-line roles}
- 资源协调用 {Bitable Inbox / Discord / Firestore Inbox}
- ⚠️ 长任务 必须 {N}min 一汇报，沉默 = 用户假设最坏

【第一步该做啥】
1. 读完上面文档
2. {specific shell/git command}
3. {specific verification step}
...
N. 验证 {success criterion} = 成功

收到任务先用一句话 ack，然后开始读文档。
````

## Synthesis hints (for Claude generating the prompt)

When reading the extract output, look for:

1. **Task identity** (§1): Last 5 user messages usually crystallize what the bot is doing.
   The first user message often has the original ask but it's likely outdated.

2. **Latest state** (§2): Recent CC Pages HTML files (sorted by mtime) typically have
   the most current "Loading weights took X s, layer N PASS" data. Trust the HTML
   over the jsonl — the bot may have written status after the last conversation turn.

3. **Required reads** (§3): Pick 1-3 CC Pages, prefer:
   - The most recent (mtime sorted)
   - Pages with "rewrite-plan", "summary", "stage", "status" in the name
   - Skip pages from earlier than the most recent task pivot

4. **File locations** (§4): The extract gives you /workspace/, /lustre/, /tmp/claude/
   paths. Pick paths actually mentioned recently — early-session paths may be stale.

5. **Pitfalls** (§5): Mine these from:
   - User messages that start with "❌" / "不要" / "千万别" / "为什么..." (corrections)
   - "Errors / Crash Points" section of the extract
   - Patches mentioned in CC Pages (each patch = a pitfall avoided)

6. **Resource state** (§6): Check the latest user message for node pool names. Read
   the most recent CC Page's "Node Pool" or "Infrastructure" section if any.

7. **Team** (§7): If user mentioned other bots (jarvis/hulk/tianmaojingling/etc.),
   include the coordination protocol. Default cadence: 5 min reports for long tasks.

8. **First step** (§8): Be specific — give actual `git log -1`, `kubectl get`,
   `curl ...` commands. The new session should be able to run them verbatim.

## Length budget

Target ~80 lines. If a single pitfall or state milestone is bigger than 3 lines,
move detail into a CC Pages HTML brief and link to it from §3 instead.
