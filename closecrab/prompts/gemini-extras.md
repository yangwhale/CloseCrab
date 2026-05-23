## MCP 工具速查 (Gemini 工具名)

_以下 MCP 工具列表用 Gemini CLI 风格的工具名 (run_shell_command, read_file 等)._
_其他 worker 通过自己的 MCP 配置自动加载相同 MCP server, 直接调工具名即可._

### MCP 工具（已自动加载，直接调用即可）

你有以下 MCP 服务可用。**不需要查找配置文件或手动安装**——它们在会话创建时已自动注入，直接调用工具名即可。

**Google 内部工具（coding MCP）**：
- `internal_search` — **Google 内部搜索**（Moma/OneGraph）。用户说"搜内部"、"moma search"、"找文档"、"搜人"时用这个
- `search_for_files_codesearch` — **代码搜索**（Codesearch）。搜索 Google3 monorepo 代码
- `search_changelists` — 搜索 CL
- `get_critique_comments` / `get_critique_analysis` — 读取 CL 的 review comments 和分析
- `create_changelist` / `update_changelist` — 创建和更新 CL
- `create_piper_workspace` / `list_piper_workspaces` — Piper workspace 管理
- `fetch_resource` — 抓取内部网页内容（go/ links、.google.com 等）
- `read_sponge_test_logs` / `read_sponge_test_failure_logs` — 读取 Sponge 测试日志

**Google Workspace（google-workspace MCP）**：
- `read_document` — 读取 Google Doc（传 doc_id）
- `read_drive_file` — 读取 Google Drive 文件
- `get_calendar_events` — 查看日历
- `get_spreadsheets` / `get_sheet_content` — 读取 Google Sheets
- `create_document` / `update_document` — 创建/更新 Google Doc

**Bug 追踪（bugged MCP）**：
- `bugged_search` — 搜索 Buganizer bug
- `bugged_show` — 查看 bug 详情
- `bugged_create` / `bugged_edit` / `bugged_comment` — 创建/编辑/评论 bug

**Wiki 知识库（wiki MCP）**：
- `wiki_query` — 语义搜索 Wiki 页面（AI/ML 基础设施、TPU/GPU 等）
- `wiki_ask` — 用 RAG 回答问题
- `wiki_page` — 读取特定 Wiki 页面
- `wiki_search` — 关键词搜索

**XProf（c2xprof MCP）**：
- `c2xprof_upload` — 上传 XPlane profiler 文件到 XProf 可视化

**Chrome 浏览器（chrome-devtools-mcp）**：
- `navigate_page` / `take_snapshot` / `click` 等 — 浏览器自动化
- **这是兜底工具**，有专用 MCP 时优先用专用的

**搜索引擎（jina-ai MCP）**：
- `search_web` — 外部网页搜索
- `read_webpage` — 抓取外部网页内容

**工具选择优先级**：
1. 搜内部信息 → `internal_search`（coding MCP），不要用 Chrome 浏览器
2. 搜代码 → `search_for_files_codesearch`（coding MCP）
3. 读 Google Doc → `read_document`（google-workspace MCP）
4. 查 bug → `bugged_search`（bugged MCP）
5. 查技术规格 → `wiki_query`（wiki MCP）
6. 搜外部信息 → `search_web`（jina-ai MCP）或 `google_web_search`（内置）
7. Chrome 浏览器 → 只在以上都不适用时兜底使用


---

## Chrome MCP 上下文管控（重要）

Chrome DevTools MCP 的 `take_snapshot`、`list_network_requests`、`take_memory_snapshot` 等工具会返回**巨量数据**（单次 10 万~50 万字符），几轮调用就能撑满 1M token 上下文。

### 必须遵守的规则

1. **优先用 sub-agent 处理浏览器任务**：涉及网页浏览、DOM 分析、页面调试等需要多次 Chrome MCP 调用的任务，委托给 sub-agent 执行。Sub-agent 的中间工具调用不会污染你的主上下文
2. **Sub-agent 只返回结论**：指示 sub-agent 返回精炼的结果摘要（关键数据、找到的元素、操作结果），不要返回原始 DOM/snapshot
3. **避免 full page snapshot**：除非必须获取完整页面结构，否则优先用：
   - `take_screenshot` — 视觉确认，比 snapshot 小 10 倍
   - `evaluate_script` — 精准提取特定元素/数据
   - `click` / `fill` 等操作工具 — 直接交互，不需要先拿整个页面
4. **不要连续多次 snapshot**：如果一次 snapshot 已经拿到了页面结构，后续操作基于已知 uid 直接交互，不要每步都重新 snapshot
5. **Network/Memory 工具慎用**：`list_network_requests`、`take_memory_snapshot` 输出极大，只在明确需要调试网络/内存问题时使用，且用 filter 参数缩小范围

### 典型的好做法

```
❌ 差：自己调用 take_snapshot → 分析 → 再 snapshot → 再分析（3 轮 = 60 万 tokens）
✅ 好：委托 sub-agent "打开 X 页面，找到 Y 按钮的 uid，点击后确认结果"，sub-agent 返回 "uid=abc123，点击成功，页面跳转到 Z"
```

### 为什么这很重要
你的上下文窗口是 1M tokens，**没有自动压缩**。Chrome MCP snapshot 每次 10-20 万字符，3-5 轮调用就会触发上下文溢出，导致整个对话丢失。Sub-agent 的工具输出不计入你的主上下文，是最有效的隔离手段。
