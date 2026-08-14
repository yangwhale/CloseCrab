---
name: glinux-tools
description: "Google 内部工具直连（替代 mcp-proxy 的 coding / bugged / workspace / c2xprof 四个 MCP）。通过 ssh 到 gLinux 直接驱动后端，零冷启动 token。当需要 Moma 内部搜索、Code Search、Buganizer 增删改查、Critique/CL、Sponge 测试日志、Google Docs/Sheets/Drive/Calendar 读写、XPlane 转 XProf 时触发。关键词：搜内部资料、go/ 链接、查 bug、buganizer、code search、查 CL、看 sponge、读 Google Doc、改文档、日历、xprof 上传。"
---

# gLinux 内部工具直连

**这套 skill 取代了原来 mcp-proxy 上的 4 个 MCP 服务**（coding / bugged / google-workspace / c2xprof）。

## 为什么不用 MCP 了

| | mcp-proxy | 本 skill |
|---|---|---|
| 冷启动 token | **39 个工具 schema 常驻 system prompt** | **0**（按需加载） |
| 链路 | bot → SSE → 反向隧道 → mcp-proxy → 后端 | bot → ssh → 后端 |
| 可靠性 | **`coding` 走 proxy 时 Gaia mint 必失败**（根因未定位，见 memory `feedback_mcp-proxy-gaia-mint-fails`） | 实测稳定 |
| bugged 延迟 | ~15 s | **~1 s**（走 CLI 直通） |

## 两种调用方式，优先用第一种

### 1. CLI 直通（快，~1 s）—— 能用就用这个

gLinux 上这些 corp CLI 可以直接 ssh 调：

```bash
ssh -T glinux bugged show 503386547
ssh -T glinux bugged search 'assignee:me status:open'
ssh -T glinux cs 'file:hybrid_sim lang:python'      # Code Search CLI
ssh -T glinux fileutil ls /bigstore/<bucket>/
ssh -T glinux g4 <args>        # Piper
ssh -T glinux blaze <args>
```

封装好的：`scripts/bugged.sh show|search|edit|create|comment <args>`

### 2. gcall.py（通用，~10-15 s）—— CLI 没有的走它

```bash
scripts/gcall.py <server> <tool> '<json-args>'
scripts/gcall.py <server> --list          # 列该 server 的全部工具与参数
```

慢是因为每次现开一个 par。**低频调用可接受，高频的请优先找 CLI。**

> [!warning] 并发别超过 5
> 每次调用起一条 ssh。并发太多会撞上 gLinux `sshd-user` 的 MaxStartups，
> 多余的连接被直接拒掉，报的却是 **「后端没响应 initialize」** ——
> 跟凭据过期的症状一模一样。2026-08-14 一口气开 13 个，两个假失败，
> 串行重跑全过。**报这个错先降并发重试，再去怀疑 `gcert`。**

## 工具清单

### `coding`（18 个）

| 工具 | 必填参数 | 用途 |
|---|---|---|
| `internal_search` | `query` | **Moma 内部搜索** —— 找 go/ 链接、设计文档、内部工具 |
| `search_for_files_codesearch` | `query` | Code Search（也可用 `cs` CLI，更快）。支持 `file:` `lang:` `content:` `function:` `class:` |
| `fetch_resource` | `url` | 抓内部网页（g3doc、Drive 等），可传 `css_selector` |
| `search_changelists` | `query` | 搜 CL |
| `get_critique_comments` | `cl_number` | 读 Critique 评论 |
| `get_critique_analysis` | `cl_number` | 读 Critique 静态分析结果 |
| ~~`get_current_workspace`~~ | — | **用不了**，见下方注 |
| `list_piper_workspaces` | — | 列 workspace |
| `create_piper_workspace` | `workspace_name` | 建 workspace |
| `get_workspace_for_cl` | `cl_number` | CL 对应的 workspace |
| `create_changelist` | `workspace_name`, `commit_message` | 建 CL |
| `update_changelist` | `workspace_name`, `cl_number` | 改 CL |
| `update_changelist_reviewer` | `cl_number` | 加/删 reviewer |
| `read_sponge_test_logs` | `invocation_id` | 读 Sponge 测试日志，可 `content_regex` 过滤 |
| `read_sponge_test_failure_logs` | `invocation_id` | 只读失败日志 |
| `list_sponge_artifacts` | `invocation_id` | 列 artifact |
| `read_sponge_artifact` | `uri` | 读单个 artifact |
| `create_gpaste` | `title`, `content` | 建 gPaste |

> `get_current_workspace` 靠**当前工作目录**跑 `vcstool source-root` 反推 workspace，
> 而 ssh 进去落在 `$HOME`（不是 Piper client 根），必然抛 `PiperError`。
> **改用 `list_piper_workspaces`**（能列出全部 11 个，正常）。

### Buganizer —— **只有 `bugged.sh` 一种叫法**

```bash
scripts/bugged.sh show 503386547
scripts/bugged.sh search 'assignee:me status:open'
scripts/bugged.sh edit|create|comment <args>
```

原来还有一套 `bugged_*` MCP 工具（走 `gcall.py bugged`），**2026-08-14 已删除**。
它底下也只是 shell 出去调同一个 `bugged` 命令，白绕一层协议、慢十几倍。
同一件事留两条路只会让人选错，所以 `gcall.py` 的 server 列表里没有 `bugged`。

### `workspace`（12 个）

| 工具 | 必填参数 |
|---|---|
| `read_document` | `doc_id` |
| `update_document` | `doc_id`, `markdown_text` |
| `create_document` | `title`, `markdown_text` |
| `replace_paragraph` | `doc_id`, `old_text`, `new_text` |
| `read_drive_file` | `file_name` |
| `copy_drive_file` | `file_id`, `new_name` |
| `list_drive_files` | `owner`, `start_date`, `end_date` |
| `list_drive_folder` | `folder_id` |
| `get_spreadsheets` | `query` |
| `list_worksheets` | `spreadsheet_id` |
| `get_sheet_content` | `spreadsheet_id`, `worksheet_id` |
| `get_calendar_events` | —（可传 `week_of` `username`）|

### `c2xprof`（1 个）

`c2xprof_upload` — 必填 `gcs_path`，**必须同时传 `project`**（gLinux 上没有 gcloud 默认项目）。
⚠️ 1.8 GB 级 xplane 不要走这里（客户端超时），直接 ssh 跑 `c2xprof.par --gcs_path=...`，约 8 分钟。

## 常用例子

```bash
# 找内部工具叫什么、有没有 go link
scripts/gcall.py coding internal_search '{"query":"TPU performance simulator","max_num_results":3}'

# 读一篇 g3doc
scripts/gcall.py coding fetch_resource '{"url":"https://g3doc.corp.google.com/..."}'

# 查 bug（用 CLI，快）
scripts/bugged.sh show 503386547

# 读 Google Doc
scripts/gcall.py workspace read_document '{"doc_id":"1IYg3aTwRe8cBGABAo5i35I-kb_OYyQnFb2iELwbr0pk"}'

# 看这周日历
scripts/gcall.py workspace get_calendar_events '{}'
```

## 不归这个 skill 管的：浏览器

需要**真的开一个浏览器**（点按钮、填表单、看渲染后的页面、抓需要登录态的内页）——
用 **`browser-cli` skill**，不要往这里塞。

两者链路方向相反，别搞混：

| | 本 skill | `browser-cli` |
|---|---|---|
| 方向 | cc-tw → ssh → gLinux 上的 par/CLI | cc-tw → ssh → gLinux 上 Chrome 的 CDP :9222 |
| 交互性 | 一次性请求-响应 | 有会话、有页面状态 |
| 失败模式 | 凭据（`gcert`） | Chrome 没起 / CDP 端口不通 |

选择很简单：**要的是数据 → 本 skill；要的是操作 → `browser-cli`**。
比如「读一篇 g3doc 的正文」用 `fetch_resource`（快得多）；
「这个内部 dashboard 得点两下才出数」才上 `browser-cli`。
`ab --where` 看当前目标机，`ab --use bj|hk` 切换。

## 前置条件与排障

**唯一的前置条件是 gLinux 上有有效凭据。** gLinux 一重启，`/var/run` 是 tmpfs、LOAS2 凭据文件必然被清空。

判据（一条命令）：

```bash
ssh glinux 'ls /google/bin/releases/codemind-mcp-servers/ | head -3'
```

- 报 **`Required key not available`** → 让 Chris 在 gLinux 上跑 **`gcert`**（要点安全密钥，远程做不到）。详见 memory `feedback_internal-mcp-needs-loas2`。
- 能列出文件但调用仍失败 → 看 `gcall.py` 的 stderr；后端不响应 initialize 会明确提示。

**不要**去修 mcp-proxy —— 那条路的失败根因排查了 13 项因素仍未定位，见 memory `feedback_mcp-proxy-gaia-mint-fails`。
