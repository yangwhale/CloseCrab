---
name: vscode-reference
description: Study Claude Code official source (CLI npm package) as golden reference for CloseCrab. Check updates, reverse-engineer patterns, distill improvements.
user_invocable: true
---

# Claude Code Official Source — Golden Reference

Claude Code CLI 和 VS Code extension 共享**同一套 JS 代码**（`@anthropic-ai/claude-code` npm 包）。这是 CloseCrab 的 golden source。CloseCrab 的 IPC、stream 处理、控制流应尽可能对齐官方实现。

## Architecture

```
                    Claude API (云端模型)
                          ↑
               Claude Code Core (JS 核心库)
               @anthropic-ai/claude-code
                          ↑
          ┌───────────────┼───────────────┐
          │               │               │
     Terminal UI      VS Code UI     socketpair IPC
     (同进程,无IPC)   (同代码+UI层)  (--input-fd/--output-fd)
     你敲 claude      VS Code 插件          ↑
                                      CloseCrab Bot
                                    (Discord/飞书 UI)
```

三者都是同一个 JS 核心库的不同前端。Terminal 是同进程直接调用，VS Code 和 CloseCrab 是子进程模式通过 socketpair 通信。

## Code Locations

### 1. CLI npm 包 (Golden Source — 总是最新)
```bash
# 实际文件位置（跟随 nvm 路径）
GOLDEN_JS=$(dirname $(readlink -f $(which claude)))/cli.js

# 或直接
~/.nvm/versions/node/v20.20.0/lib/node_modules/@anthropic-ai/claude-code/cli.js
```
- 安装/更新: `npm install -g @anthropic-ai/claude-code`（或 `deploy.sh --npm`）
- 查版本: `claude --version`
- **不需要 VS Code。CLI 就是最新代码。**

### 2. VS Code Extension (可能过时，仅供参考)
```bash
ls -d ~/.vscode/extensions/anthropic.claude-code-* | sort -V | tail -1
```
- 需要在 VS Code 界面手动点 Update，版本可能落后 CLI

### 3. GitHub / npm
- Repo: `github.com/anthropics/claude-code`
- npm: `npmjs.com/package/@anthropic-ai/claude-code`
- Releases: `gh release list -R anthropics/claude-code --limit 10`

## Workflow: Check for Updates

### 1. Version Comparison
```bash
# 当前安装的 CLI 版本
claude --version

# npm 上最新版本
npm info @anthropic-ai/claude-code version

# 最近 5 个 release
gh release list -R anthropics/claude-code --limit 5
```

### 2. Changelog Review
```bash
# 查看特定 release 的更新内容
gh release view -R anthropics/claude-code <TAG>

# 最近 10 个版本号
npm info @anthropic-ai/claude-code versions --json | python3 -c "import json,sys; v=json.load(sys.stdin); print('\n'.join(v[-10:]))"
```

### 3. Diff Key Patterns
用 golden source 对比 CloseCrab 实现：

```bash
GOLDEN_JS=$(dirname $(readlink -f $(which claude)))/cli.js
```

| Area | grep pattern | CloseCrab File |
|------|-------------|----------------|
| IPC / socketpair | `input-fd\|output-fd` | `workers/claude_code.py` |
| Stream JSON | `readMessages\|parseJSON` | `workers/claude_code.py` |
| control_request | `control_request\|control_response` | `_reader_loop()` |
| Result handling | `"result"\|session_id` | `send()` |
| Compaction | `compact\|compaction\|contextTokenThreshold` | N/A (CLI-level) |
| Background tasks | `run_in_background\|task_notification` | `_handle_background_event()` |
| Process lifecycle | `spawn\|kill\|terminate\|SIGTERM` | `start()` / `stop()` |
| keep_alive | `keep_alive` | `_reader_loop()` |

## Reverse Engineering Minified JS

cli.js 是 minified 的（变量名被 mangle），但字符串字面量保留原文。

### Search Techniques
```bash
GOLDEN_JS=$(dirname $(readlink -f $(which claude)))/cli.js

# 搜索特定模式（字符串字面量不会被 mangle）
grep -oP '.{0,200}control_request.{0,200}' "$GOLDEN_JS" | head -5
grep -oP '.{0,200}keep_alive.{0,200}' "$GOLDEN_JS" | head -5

# 扩大上下文理解逻辑
grep -oP '.{0,500}PATTERN.{0,500}' "$GOLDEN_JS" | head -3
```

### Known Patterns (from reverse engineering)
- **Stream reader**: Async iterable class, line-delimited JSON from socket
- **Event routing**: `readMessages()` routes by event type
- **Control handler**: Immediate at reader level, never enqueued
- **Result consumer**: First `result` event = end of turn, no `system:init` check
- **Compaction**: Default `contextTokenThreshold` = 100K (JZ7=1e5), configurable via `autoCompactWindow`

## CloseCrab Alignment Checklist

- [ ] **control_request** handled at reader level (not enqueued)
- [ ] **keep_alive** silently consumed at reader level
- [ ] **control_cancel_request** logged and consumed at reader level
- [ ] **Result handling** does NOT depend on `system:init`
- [ ] **Graceful stop** uses SIGTERM before SIGKILL
- [ ] **Background events** only handle `task_notification` and `result`
- [ ] **No polling/draining** — continuous reader loop with asyncio.Queue

## Key Lessons Learned

1. **不检查 `system:init`**。Claude CLI 可能合并 background task 和 user message 到一轮，只发一个 `result`。检查 `system:init` 会导致 stall。

2. **Control messages 永不入队**。Reader level 立即处理，因为 CLI BLOCKS 等 `control_response`。入队 = 死锁。

3. **不需要 buffer polling**。连续 async reader loop，事件路由到 queue（active）或 background handler（idle）。

4. **Line-delimited JSON**。每行一个完整 JSON。注意不完整行需要 buffer 到换行符。
