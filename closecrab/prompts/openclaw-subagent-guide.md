## sessions_spawn 子任务分发规则（OpenClaw 专用）

`sessions_spawn` 是 OpenClaw 用来起子 agent 的工具。它有两种 `runtime`：

- **`runtime: "subagent"`**（默认，**推荐**）：同步执行，子 agent 在主进程内运行，结果**直接 inline 返回**到工具结果里。主 agent 拿到结果后正常综合回复用户。
- **`runtime: "acp"`**：异步执行，子 agent 起独立 ACP session，工具**立即返回 `{status: "accepted"}`**，不带结果。

### 关键规则

1. **简单/快速子任务（< 1 分钟）→ 用 `runtime: "subagent"`**
   - 例：并行查 3 个目录的文件数、读 3 个配置文件、调 3 次同一个查询
   - 工具直接返回完整结果，你拿到后综合就行

2. **真长任务（> 5 分钟，需要后台跑）→ `runtime: "acp"` 配合 `streamTo: "parent"` + `sessions_yield`**
   - **必须**显式传 `streamTo: "parent"`，否则子 agent 输出**全部丢失**，用户看不到结果
   - **必须**在 spawn 所有子任务之后，调用 `sessions_yield` 工具让出当前 turn —— OpenClaw 会等子任务结果到达，再以新消息形式把结果回流给你
   - 不要用 `runtime: "acp"` 不带 `streamTo: "parent"` — 那是后台模式，CloseCrab 当前不支持后台结果回送
   - 流程模板：
     1. 调 `sessions_spawn(runtime:"acp", streamTo:"parent", task:"...")` 起子任务 1
     2. 重复起其他子任务
     3. 调 `sessions_yield(message:"等待 N 个子任务完成")` 让出 turn
     4. 子任务完成后，CloseCrab 会自动 ping 你续杯，你将收到一条包含子任务结果的系统消息
     5. 综合所有子任务结果后回复用户

3. **不要靠 `sessions_spawn` 起子任务后立即 end_turn**
   - 子任务的结果必须出现在你的最终回复里，否则用户得到空回复
   - 异步模式（`streamTo: "parent"`）务必跟 `sessions_yield` 一起用，让 CloseCrab 知道你在等
   - 同步模式（`runtime: "subagent"`，默认）可以直接 end_turn，结果已经 inline

### 错误模式（不要踩）

- ❌ 调 `sessions_spawn` 不指定 `runtime`，然后立即 end_turn → 用户拿到空回复
- ❌ 调 `sessions_spawn(runtime: "acp")` 不带 `streamTo: "parent"` → 子任务结果丢失
- ❌ 把每个微任务都拆成 `sessions_spawn` → 浪费 token，简单查询直接用 `read_file`/`run_shell_command` 就行

### 选择 sub-agent 的时机

- **应该用 sub-agent**：
  - 并行执行 3+ 个独立子任务（Chrome MCP 多轮调用、大量代码搜索）
  - 子任务需要独立上下文（避免污染主对话）
- **不应该用 sub-agent**：
  - 简单的单步操作（直接调对应工具）
  - 已经有专门 MCP 工具的场景（用专门的）
