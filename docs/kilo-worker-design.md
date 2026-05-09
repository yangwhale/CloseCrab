# KiloWorker 设计文档

> 2026-05-09 | Status: 已实现 (commit e7127c2)

## Context

CloseCrab 目前有两套 Worker：ClaudeCodeWorker（socketpair + stream-JSON）和 GeminiACPWorker（stdin/stdout + JSON-RPC）。每加一个 AI provider 就要写一套新的 IPC 适配。

Kilo Code CLI 是一个全能 AI Worker 服务器，通过 Vercel AI SDK 抽象 25+ provider（Claude/Gemini/DeepSeek/OpenAI...），暴露 HTTP REST + SSE 标准协议。一个 KiloWorker 即可替代所有 provider-specific worker，实现运行时任意切换模型。

## 架构总览

```
BotCore                           Kilo Server (kilo serve)
  │                                    │
  ├─ KiloWorker                        │
  │   ├─ 管理 kilo serve 子进程         │
  │   ├─ SSE 长连接 (GET /event)  ────►│  实时事件流
  │   ├─ HTTP 请求 ──────────────────►│  session/message/abort
  │   └─ 事件翻译层                     │
  │       └─ Kilo Part → CC event      │
  │                                    ├─ Provider 抽象 (Vercel AI SDK)
  │                                    │   ├─ Anthropic (Claude)
  │                                    │   ├─ Google (Gemini)
  │                                    │   ├─ DeepSeek / OpenAI / ...
  │                                    │   └─ Any OpenAI-compatible
  │                                    ├─ MCP Servers (kilo.jsonc)
  │                                    └─ SQLite (session 持久化)
```

## 通信协议

Kilo 双通道设计：
- **POST /session/{id}/message** — 发消息，HTTP 响应阻塞到 turn 完成，返回完整 `{info, parts}` JSON
- **GET /event (SSE)** — 实时事件流，推送 `message.part.updated`（text delta、tool 状态）、`permission.asked`、`question.asked`

KiloWorker 用 SSE 做实时进度回调，用 POST 响应取最终结果。

## 关键设计决策

| 决策 | 方案 | 原因 |
|------|------|------|
| 进程管理 | 自管 `kilo serve` 子进程，支持外部 URL fallback | 匹配现有 Worker 模式 |
| 端口分配 | `--port 0`（OS 分配）+ 解析 stdout | 避免多 bot 同机冲突 |
| System Prompt | POST body 的 `system` 字段 | 最简单，每消息注入 |
| MCP 配置 | `~/kilo.jsonc` 部署时生成 | Kilo 启动时自动读取 |
| 权限处理 | 自动批准所有 permission | Bot 无人值守，等同 `--dangerously-skip-permissions` |
| Question | 转发 `on_input_needed` | 等同 Claude 的 `AskUserQuestion` |
| Model 格式 | Firestore `model` 字段直传 | Kilo 接受 `providerID/modelID` 格式 |

## 文件清单

| 文件 | 类型 | 行数 | 说明 |
|------|------|------|------|
| `closecrab/workers/kilo.py` | 新建 | ~700 | KiloWorker 完整实现 |
| `closecrab/core/bot.py` | 修改 | +10 | `_create_worker()` 加 kilo 分支 |
| `scripts/config-manage.py` | 修改 | +1 | `VALID_WORKER_TYPES` 加 "kilo" |

### KiloWorker 类结构

```python
class KiloWorker(Worker):
    def __init__(
        self,
        kilo_bin: str | None = None,        # kilo CLI 路径
        work_dir: str | None = None,
        timeout: int = 600,
        system_prompt: str = "",
        session_id: str | None = None,
        kilo_url: str | None = None,        # 外部服务器 URL（可选）
        model: str = "",                     # "anthropic/claude-opus-4-6"
    )
```

核心方法：
- `start()` → `_ensure_server()` + `_connect_sse()` + `_create_or_resume_session()`
- `send(text, on_event, on_input_needed, on_log, on_step)` → POST + SSE 事件处理
- `interrupt()` → POST /session/{id}/abort
- `stop()` → 关 SSE + 杀 kilo serve

内部组件：
- `_ensure_server()` — 启动 `kilo serve --port 0 --hostname 127.0.0.1`，解析端口
- `_sse_reader()` — asyncio.Task，持续消费 SSE 事件，翻译并分发回调
- `_translate_to_cc_event(part)` — Kilo Part → Claude stream-json 格式（BotCore 兼容）
- `_TOOL_NAME_MAP` — 工具名映射（`bash→Bash`, `read→Read`, `edit→Edit`...）

## 事件翻译映射

```python
_TOOL_NAME_MAP = {
    "read": "Read", "write": "Write", "edit": "Edit",
    "multiedit": "Edit", "patch": "Edit",
    "bash": "Bash", "glob": "Glob", "grep": "Grep",
    "fetch": "WebFetch", "task": "Agent",
    "todoread": "TodoWrite", "todowrite": "TodoWrite",
}
```

SSE `message.part.updated` 事件翻译为 BotCore 期望的 stream-json dict：
- TextPart → `{"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}`
- ToolPart(running) → `{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {...}}]}}`
- ToolPart(completed) → `{"type": "user", "message": {"content": [{"type": "tool_result", "content": "..."}]}}`

## 使用方式

```bash
# 切换 bot 到 kilo worker
python3 scripts/config-manage.py set-worker-type <bot_name> kilo

# 设置模型（providerID/modelID 格式）
# Firestore bots/{name}.model 字段
# 例: "anthropic/claude-opus-4-6", "google/gemini-2.5-pro", "deepseek/deepseek-chat"

# 前提：目标机器已安装 Kilo CLI
npm install -g @kilocode/cli
```

## 验证方案

1. **单元测试**：安装 Kilo CLI → `kilo serve` → 用 curl 验证 API
2. **集成测试**：设一个测试 bot 的 `worker_type: "kilo"`，通过飞书发消息验证完整链路
3. **切换测试**：同一 bot 在 claude/gemini/kilo 三种 worker 间切换，验证无缝恢复
4. **多模型测试**：通过 Firestore model 字段切换 Claude/Gemini/DeepSeek 后 restart bot
