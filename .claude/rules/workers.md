---
globs: closecrab/workers/*.py
---

# Worker 开发规则

## 架构理解
`ClaudeCodeWorker` 管理一个持久的 Claude Code CLI 子进程。通信方式：

```
Bot Process                Claude CLI Process
    │                            │
    ├── sock_in  ──────────►  stdin (fd)
    │                            │
    ◄── sock_out ◄──────────  stdout (fd)
    │                            │
    └── proc.stderr ◄────────  stderr
```

- **不是** stdin/stdout，是 **socketpair** — 两对独立的 fd
- Claude CLI 启动时通过 `--input-fd` 和 `--output-fd` 参数接收 fd 编号
- stderr 重定向到临时文件，用于调试

## 关键状态
- `self._lock` — asyncio.Lock，防止并发操作同一个 worker
- `self._interrupted` — 中断标志，通过 socketpair 发送 interrupt 消息
- `self._usage` — 累计 token 用量（input/output/cache/cost）
- `self._session_id` — Claude session ID，支持 resume

## stream-JSON 事件解析
Claude CLI 输出 line-delimited JSON，每行一个事件。关键事件类型：
- `assistant` — Claude 的回复文本
- `tool_use` / `tool_result` — 工具调用和结果
- `control_request` — 控制请求（ExitPlanMode、AskUserQuestion），需要传递给 Channel 层等待用户回复
- `usage` — token 用量统计
- `error` — 错误信息

## 修改注意事项
- 改 JSON 解析逻辑时，确保处理不完整的 JSON 行（网络延迟可能导致一行分多次到达）
- 新增事件类型时，同步更新 `BotCore` 的事件处理逻辑
- timeout 检测基于 `asyncio.wait_for`，不要用 signal.alarm
- Worker 生命周期由 BotCore 管理，不要在 Worker 内部自行 restart
