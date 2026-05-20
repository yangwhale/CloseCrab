# GBrain 记忆库集成指南

> GBrain 是 CloseCrab 的**可选**持久化记忆库，提供跨 session、跨 bot、可搜索的知识图谱。CloseCrab 自身不强制 GBrain — 不装也能正常跑，只是失去 "salience-aware recall" 这一能力层。

## 这是什么

[GBrain](https://github.com/garrytan/gbrain) 是 Garry Tan (Y Combinator) 开源的 memory bank 服务（MIT license），提供：

- 基于 PGLite (默认) 或 Postgres + pgvector 的语义检索
- 结构化页面（slug + frontmatter + 链接图谱）
- OAuth 2.1 认证的 HTTP MCP server
- 与 Claude Code / Cursor / Codex 等 AI agent 即插即用

CloseCrab 的 ClaudeCodeWorker / OpenClawWorker 通过 stdio MCP launcher 把每个 bot 接入它自己专属的 GBrain 实例，实现 "持久记忆 + 多 bot 隔离"。

## 架构：每 bot 独立服务

按 [GBrain Topology 3 (Split-engine per-worktree)](https://github.com/garrytan/gbrain/blob/main/docs/architecture/topologies.md) 部署，每个 bot 拥有独立的：

| 维度 | 隔离方式 |
|---|---|
| **数据库** | 独立 `GBRAIN_HOME` 目录，各自一个 `brain.pglite` |
| **端口** | 每 bot 绑独立 port（避免冲突） |
| **OAuth 凭据** | 每 bot 一份 `cc-tw-{bot}-creds.json`（600 perm） |
| **进程** | 每 bot 单独的 `gbrain serve --http` 长进程 |

参考端口分配（自行调整）：

| Bot | GBRAIN_HOME | Port |
|---|---|---|
| jarvis | `~/.gbrain-jarvis` | 3131 |
| bunny | `~/.gbrain-bunny` | 3132 |
| xiaoai | `~/.gbrain-xiaoai` | 3133 |
| tiemu | `~/.gbrain-tiemu` | 3134 |
| hulk | `~/.gbrain-hulk` | 3135 |

> 单 bot 部署可以直接用默认的 `~/.gbrain` 和端口 3131，不需要设 `GBRAIN_HOME`。

## 前置依赖

| 依赖 | 用途 | 安装 |
|---|---|---|
| **Bun** | GBrain runtime | `curl -fsSL https://bun.sh/install \| bash` |
| **Gemini API key** | 1536-dim embedding | [aistudio.google.com](https://aistudio.google.com/apikey) |

环境变量（写到 `~/.zshenv` 或 `~/.bashrc`）：

```bash
# CRITICAL: 是 GOOGLE_GENERATIVE_AI_API_KEY，不是 GEMINI_API_KEY
export GOOGLE_GENERATIVE_AI_API_KEY="your-gemini-key-here"
```

## 安装步骤（每个 bot 重复一次）

下面以 bot 名 `mybot`、port `3132` 为例。

### 1. 装 GBrain CLI

```bash
bun install -g github:garrytan/gbrain
gbrain --version   # 验证 ≥ v0.34
```

> postinstall 失败时走 fallback：`git clone https://github.com/garrytan/gbrain.git ~/gbrain && cd ~/gbrain && bun install && bun link`

### 2. 初始化独立 brain

```bash
export GBRAIN_HOME=~/.gbrain-mybot
gbrain init --pglite

# 验证
gbrain doctor --json
gbrain models doctor
```

`init` 会在 `$GBRAIN_HOME/config.json` 写入引擎类型 + embedding 模型配置。

### 3. 注册 OAuth client

```bash
export GBRAIN_HOME=~/.gbrain-mybot

# 启服务（先短时启动一次让 register-client 能写库；后续步骤 4 会重启）
gbrain serve --http --port 3132 --bind 127.0.0.1 &
SERVE_PID=$!
sleep 3

# 通过 DCR 注册（CLI 直接写 PGLite 会被 serve 持锁 silent fail，必须走 HTTP）
curl -s -X POST http://127.0.0.1:3132/register \
    -H 'Content-Type: application/json' \
    -d '{"client_name":"cc-tw-mybot","grant_types":["client_credentials"],"scope":"read write admin"}' \
  | tee ~/.gbrain/cc-tw-mybot-creds.json

chmod 600 ~/.gbrain/cc-tw-mybot-creds.json

kill -TERM $SERVE_PID   # SIGTERM (不要 SIGKILL，否则未 flush 的 embeddings 会丢)
wait $SERVE_PID 2>/dev/null
```

凭据文件应类似：

```json
{
  "client_id": "gbrain_cl_<32hex>",
  "client_secret": "gbrain_cs_<32hex>",
  "scope": "read write",
  "issuer": "http://localhost:3132"
}
```

> ⚠️ **不要 commit 凭据文件**：CloseCrab 项目根的 `.gitignore` 已经排除 `~/.gbrain/`，但请确保不要手动把它复制进 repo。

### 4. 启长进程

```bash
nohup env GBRAIN_HOME=~/.gbrain-mybot \
    gbrain serve --http --port 3132 --bind 127.0.0.1 \
    > ~/.gbrain-mybot/serve.log 2>&1 &

# 验证（应返回 access_token JSON）
CREDS=~/.gbrain/cc-tw-mybot-creds.json
curl -s -X POST http://127.0.0.1:3132/token \
    -d "grant_type=client_credentials" \
    -d "client_id=$(jq -r .client_id $CREDS)" \
    -d "client_secret=$(jq -r .client_secret $CREDS)"
```

生产环境建议用 systemd unit 而不是 nohup，例：

```ini
# /etc/systemd/system/gbrain-mybot.service
[Unit]
Description=GBrain for mybot
After=network.target

[Service]
Type=simple
User=chrisya
Environment="GBRAIN_HOME=/home/chrisya/.gbrain-mybot"
Environment="GOOGLE_GENERATIVE_AI_API_KEY=your-key-here"
ExecStart=/home/chrisya/.bun/bin/gbrain serve --http --port 3132 --bind 127.0.0.1
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=default.target
```

### 5. 接入 CloseCrab

CloseCrab 通过 `scripts/mcp-proxy-launcher.py`（stdio → HTTP MCP 桥接）让 Claude Code 看到 GBrain。CloseCrab 启动时会自动尝试加载 `~/.gbrain/cc-tw-{bot_name}-creds.json`，找到就接入，找不到就静默跳过（bot 仍然正常运行）。

确认凭据文件命名匹配你的 bot：

```bash
ls -la ~/.gbrain/cc-tw-mybot-creds.json   # 必须是 600 perm
```

重启 bot：

```bash
python3 scripts/config-manage.py show mybot   # 确认配置存在
# 然后通过飞书/Discord 发 /restart，或直接 SIGTERM bot.py
```

bot 启动日志应该出现 `[GBrain] connected to http://127.0.0.1:3132`（取决于 worker_type）。

## 验证集成

通过 bot 发消息测试：

- 让 bot 把一条事实记下来（写入触发 `put_page`）
- 重启 bot，再问刚才记的内容（读取触发 `get_page` / `query`）

或直接调用 GBrain CLI：

```bash
export GBRAIN_HOME=~/.gbrain-mybot
gbrain query "test query"       # 语义搜索
gbrain stats                    # 查 page/chunk 计数
```

## Troubleshooting

| 症状 | 原因 | 解法 |
|---|---|---|
| `gbrain: command not found` | bun bin 不在 PATH | `export PATH=$HOME/.bun/bin:$PATH` |
| Bot 启动后 GBrain MCP 工具不出现 | 凭据文件名/perm 错 | 确认 `cc-tw-{bot}-creds.json` 存在且 600，client_id/client_secret 不空 |
| `register-client` 成功但 `auth list-clients` 空 | PGLite 持锁导致 CLI 写库 silent fail | 必须走 `/register` HTTP DCR，不要用 `gbrain auth register-client`（详见 [feedback_gbrain-pglite-cli-write-silent-fail](https://github.com/yangwhale/CloseCrab/blob/main/.claude/memory/feedback_gbrain-pglite-cli-write-silent-fail.md)） |
| Claude Code 看到 401 触发 OAuth helper 工具 | Claude Code 直连 HTTP MCP 会覆盖静态 Authorization header | 必须用 `mcp-proxy-launcher.py` stdio 封装（CloseCrab 默认行为，详见 [feedback_gbrain-claude-stdio-proxy](https://github.com/yangwhale/CloseCrab/blob/main/.claude/memory/feedback_gbrain-claude-stdio-proxy.md)） |
| Bot 重启后 GBrain 数据丢失 | `kill -9` 强杀，PGLite WAL 未 checkpoint | 永远用 SIGTERM；抢救走 WAL replay（详见 [feedback_pglite-sigkill-loses-embeddings](https://github.com/yangwhale/CloseCrab/blob/main/.claude/memory/feedback_pglite-sigkill-loses-embeddings.md)） |
| Embedding 全部失败 | env var 错叫 `GEMINI_API_KEY` | 必须是 `GOOGLE_GENERATIVE_AI_API_KEY`（详见 [feedback_gbrain-embedding-setup](https://github.com/yangwhale/CloseCrab/blob/main/.claude/memory/feedback_gbrain-embedding-setup.md)） |
| Bot session 启动后没有 GBrain tools | brain serve 还没起就启了 launcher | 启 bot 前确认 `curl localhost:3132/.well-known/oauth-authorization-server` 200（CloseCrab 已加 `wait_for_brain_serve` retry） |
| 端口被占 | 多 bot 同 port | 重新分配 port，更新对应 systemd unit + bot 重启 |

## 进阶

- **共享 Postgres 模式**: 多 bot 共享 1 个 Supabase 实例（节省资源但失去隔离），参考 [GBrain 官方 Postgres 文档](https://github.com/garrytan/gbrain/blob/main/docs/INSTALL.md#postgres-engine)
- **Thin-client 模式**: 只装 MCP 不存数据 `gbrain init --mcp-only`
- **管理后台**: HTTP server 自带 `/admin` dashboard（admin scope 可访问）
- **数据迁移**: `gbrain export` / `gbrain import` 跨实例搬数据

## 相关链接

- **GBrain 源码**: <https://github.com/garrytan/gbrain>
- **GBrain INSTALL.md**: <https://github.com/garrytan/gbrain/blob/main/docs/INSTALL.md>
- **GBrain Topologies**: <https://github.com/garrytan/gbrain/blob/main/docs/architecture/topologies.md>
- **CloseCrab GBrain client 实现**: `closecrab/utils/gbrain_index.py`
- **CloseCrab GBrain monitor**: `scripts/gbrain-usage-monitor.py`
- **CloseCrab MCP launcher**: `scripts/mcp-proxy-launcher.py`
