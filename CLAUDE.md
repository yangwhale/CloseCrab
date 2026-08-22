# CloseCrab — Claude Code Bot Framework

## ⛔ 第一原则：不确定就去查，绝不编

**任何事实性断言 —— 数字、年份、出处、架构关系、API 行为 —— 写下之前先问一句：
这是我查过的，还是我推出来的？**

- 推出来的**必须写清推导链**（例：「单 device 1153 TFLOP/s = 官方每芯片 2307 ÷ 2」）
- 查不到就明说「这个我不确定」「官方没有公开」——**比给一个像样的答案强**
- **不怕慢。** 查证渠道都很便宜：读 config / 源码 → 内部术语库（wiki MCP）→
  `jina-ai` 搜原文 → `glinux-tools` 内部搜索。几十秒的事
- **最危险的一类是「听起来像常识的架构关系」。** 老约束在新架构上往往已被解除 ——
  `hidden = 头数 × 每头维度` 在标准 MHA 里成立，在 MLA 上就不成立。不要顺手套用
- 数字自检靠**外部锚点**：算出的总参数能对上官方口径，就验证了整套公式；
  **对不上任何锚点的孤立数字要格外小心**

> **为什么排第一**：编出来的东西看起来最合理，所以最难被自己发现 ——
> 它总是长得像一条正确的知识。查一次几十秒，编一次赔上整份材料的可信度。
> （2026-08-22 立规，触发事件见 memory `feedback_verify-never-fabricate`）


<!--
======================================================================
DISPATCHER PATTERN (functional-area-resolver, 借鉴 gbrain skill)
======================================================================
本文件是 CloseCrab 项目根 instructions，只放跨 area 的架构概览、约束、
命令速查、配置体系。详细 per-area 规则按需 lazy-load，改对应 area 代码前先读：

  Channel 开发     → .claude/rules/channels.md   (三平台一致性 + control_request)
  Worker 开发      → .claude/rules/workers.md    (4 种 worker IPC/ACP/MCP/retry)
  部署/scripts     → .claude/rules/deploy.md     (deploy.sh/run.sh/scripts)
  Skills 系统      → .claude/rules/skills.md     (SKILL.md 格式 + 命名)
  Voice 部署       → docs/voice-deploy-quickstart.md (LiveKit 一键装 + 凭据)

rules/ 是各 area 单源真理，本文件不重复其内容。
实测改进（gbrain A/B eval, Opus/Sonnet/Haiku）：+13~17pp，文件体积 ~50%
======================================================================
-->

## 项目概述
CloseCrab 将 Claude Code CLI 包装为多平台 AI Bot（Discord/飞书/钉钉）。每个 bot 是独立进程，通过 Unix socketpair 与 Claude CLI 通信，Firestore 存配置和日志。支持 5 种 worker（claude/gemini/openclaw/kilo/dsh），由 Firestore `bots/{name}.worker_type` 切换。

```
用户消息 → Channel Adapter → UnifiedMessage → BotCore → Worker ⇄ CLI (Claude/Gemini/...)
             (STT if voice)        ↕                        ↕
                              Firestore                Skills / MCP
```

入口 `closecrab/main.py`（`build_system_prompt()` 构造 system prompt：channel style → safety → bot 身份 → 语音指令 → Inbox → Team 角色）；核心 `closecrab/core/bot.py`（BotCore 路由 + per-user worker）。模块级细节见对应 rules/ 文件。

## 常用命令

```bash
# 启动 / 部署（详见 rules/deploy.md）
./run.sh <bot_name>                          # 带自动重启的 wrapper
./deploy.sh [--cc-only|--bot|--npm|--voice]  # 完整 / 分步安装

# 配置管理
python3 scripts/config-manage.py list|show <bot>|set-channel <bot> <ch>|set-worker-type <bot> <w>

# Bot 间消息 / 增强能力
python3 scripts/inbox-send.py <target_bot> "<message>"
python3 scripts/subagent-parallel.py --inline '{"tasks":[{"label":"A","prompt":"..."}]}'
python3 scripts/cron-tool.py add --target <bot> --in 10m|--cron "0 9 * * MON-FRI" --message "..."
python3 scripts/cron-tool.py list|remove <id>|tick      # cron-daemon 由第一个 bot 的 run.sh 单例拉起，30s tick
python3 scripts/session-status.py <bot> [--days N]

# 运维 / 健康检查
scripts/boot-autostart.sh [--check]          # 开机自启（三台机器的 @reboot 都调它）；幂等，随时可手动跑验证
scripts/dispatch-bot.sh deploy|recall|move|check
scripts/sync-memory.sh --push|--pull
scripts/send-to-discord.sh --channel <id> "<msg>"
scripts/closecrab-smoke-test.sh <bot> [--json] [--actions]
scripts/dsh-setup.sh [--check]               # 建 dsh worker 用的 cordis profile（幂等）
                                             # 部署指南（含 LiteLLM 网关要求）见 docs/dsh-worker-deploy.md

# 可观测性 / 备份
python3 scripts/memory-audit.py --action-only         # 记忆体检（周一 cron）
python3 scripts/prompt-audit.py --bot <bot>           # cold-start token 账 + 环比
                                                      # 启动时 main.py 也会跑，但带 --no-save 不写趋势
scripts/firestore-backup-cron.sh                      # 周度：GCS export + 私有仓库脱敏快照
~/CloseCrab/scripts/install-wiki-mcp.sh [--check]     # 装 Wiki MCP（幂等，见 docs/wiki-deploy.md）
```

## 退出码约定（run.sh 行为）
| 码 | 含义 | 重启 |
|----|------|------|
| `42` | `/restart` 命令 | 立即重启 |
| `0` | **异常**（bot 正常运行时不该自己退出） | 重启，并 touch `.dirty_restart` 计入失败 |
| `130` / `137` / `143` | SIGINT / SIGKILL / SIGTERM | 不重启 |
| `1` | 配置错误 | 不重启 |
| 其他非零 | 崩溃 | 重启（连续 >10 次停止） |

`0` 那行反直觉但是故意的，见 `run.sh:120-125`。

## 配置体系
- **Bootstrap**: `.env`（deploy.sh 生成，正常只有 `FIRESTORE_PROJECT` + `FIRESTORE_DATABASE`；
  本机另有一行手工加的 `export STT_AB_DEBUG=1`，是个例外——它会在下次 deploy 时丢失）
- **运行时配置**: Firestore `bots/{name}`（见下方 Firestore 表）
- **全局常量**: Firestore `config/global`（cc_pages_url、gcs_bucket）
- **CC 环境**: `~/.claude/settings.json`（env / permissions / plugins）；**MCP**: `~/.claude.json`
  - MCP 改动**要重启 bot 才生效** —— CLI 只在启动时读一次
  - **雇主内部工具一律不走 MCP**（2026-08-14 起）。相关的 4 个 MCP 已从
    `~/.claude.json` 移除 —— 它们的工具 schema 常驻 system prompt，实测约
    **10.4K token** 冷启动开销，而且那条链路本身有未定位的故障。
    改用私有 skill 直连后端，按需加载、零常驻开销。
    **配置与用法只在私有 skill 目录里，本仓库不记录。**
  - 目前 `~/.claude.json` 里只剩 `jina-ai`（远程 HTTP）、`wiki`（本地）、`serena`（本地 LSP）
  - `serena` 必须带 `--project <repo>`，否则每次首调都报 `No active project`，
    agent 吃一次错就退回 grep，整个 session 不再用它（2026-08-10 实测 30 天 0 调用）
  - 一台机器的 `~/.claude.json` 是**所有 bot 共用**的，改了要把这台上的 bot 都重启
  - **浏览器操作走 `browser-cli` skill，不走 MCP**。`chrome-devtools-mcp`
    已于 2026-08-10 从 Claude 侧移除 —— 近 7 天 48 次调用里属于它独有能力
    （Lighthouse / heap snapshot / performance trace）的是 **0 次**，
    其余全是 `browser-cli` 能做且便宜 40 倍的操作。真要用那三样时加回：
    ```jsonc
    // ~/.claude.json → mcpServers
    "chrome-devtools-mcp": {
      "command": "npx",
      "args": ["-y", "chrome-devtools-mcp@latest", "--browserUrl", "http://127.0.0.1:9222"]
    }
    ```
    加完**必须重启 bot**。Gemini CLI 那边（`~/.gemini/settings.json`）2026-08-14
    也一并砍了 —— 本机不用 Gemini CLI，而它那份是走 mcp-proxy 的 SSE 版，
    随 proxy 下线已成死链。现在那里只剩 `jina-ai` + `wiki`
  - 那套 SSE 聚合代理（`mcp-proxy`）已于 2026-08-14 整体退役，相关端口转发一并拆除。
    **拆解过程与拓扑细节记在私有 skill 与 memory 里，本仓库不展开。**
    这里只留一条通用教训：**反向隧道常常一条连接同时扛多个端口**，
    其中可能包括你赖以登录那台机器的端口 —— 只想关掉其中一个转发时，
    改配置后重启，别直接 kill 整条隧道，并且给改动配一个失败自动回滚的自检
- **OpenClaw**: `~/.openclaw/openclaw.json`（deploy.sh 从 `config/openclaw.json` 模板生成）
- **GBrain**: **2026-08-10 宣布归档停用，但直到 2026-08-14 才真正停干净** ——
  当初只摘了 `~/.claude.json`，**漏了另外两个 MCP 配置源**，所以它一直在跑：
  每个 agent 会话一个 launcher，实测 cc-tw 上同时有 7 个、占 382 MB，
  当初记着要省的 8.5K 冷启动 token 一分没省到。
  > **教训：这台机器的 MCP 有三个配置源，砍 MCP 要三个都查**
  > 1. `~/.claude.json` — Claude CLI 用户级
  > 2. `~/.mcp.json` — **Claude CLI 项目级**（bot 的 cwd 是 `$HOME`，所以它生效）。最容易漏
  > 3. `~/.openclaw/openclaw.json` — OpenClaw 自己那份
  >
  > 判据别看配置，直接数进程：`ps -ef | grep <你以为已经停掉的东西>`。
  三处均已清理（备份 `*.bak-gbrain-20260814`）。另外 `bots/*.gbrain_index.enabled`
  08-10 起就是 `false`，但 `prompt-audit.py` 一直写死 5000 不看开关，
  连续四天虚报 5K —— 08-14 一并修了。清理后 jarvis 冷启动 65.5K → **46.2K**。
  > ⚠️ **`bun ... gbrain serve --http --port 3131` 那个进程是故意留着的，不要杀。**
  > 它是 GBrain 服务本体（~484 MB），Chris 08-14 明确要求保留。而且它底下是 PGLite ——
  > **跑得好 ≠ 能重启**，大量未 checkpoint 的 WAL 会让它再也起不来
  > （见 memory `feedback_pglite-wal-timebomb`）。真要停，先备份 + 确认能从 markdown 重建。
  数据和服务原样保留，恢复方法与停用理由见 `~/.gbrain/ARCHIVED-README.md`。
  一句话理由：它装的是 MEMORY.md 的副本（同步脚本全量灌 memory 目录），
  而那批内容的索引本来就自动注入，要哪条直接 Read 更快 —— 近 7 天 query 0 次。
  省 8.5K cold start。架构文档仍在 docs/gbrain-integration.md
- **Secrets**: 绝不硬编码 / 不进 git — Firestore 存 tokens，GKE 用 K8s Secret 挂载

## Firestore 数据结构
| Collection | 用途 |
|-----------|------|
| `bots/{name}` | Bot 配置（channel tokens、model、allowed users、team、inbox、email、worker_type、livekit） |
| `bots/{name}/logs/{id}` | 对话日志（timestamp、status、duration_seconds、usage、worker_type、user、source、session_id）。**完整回复在 `assistant`（截 10K），没有 `reply` 字段**；`steps` 是过程轨迹，**每条截 500 字符、最多 200 条**，所以结论要从 `assistant` 读不是从 `steps` 读 |
| `messages` | Bot 间收件箱（from、to、instruction、status、result） |
| `registry` | Bot 运行时状态（hostname、accelerator、last_seen） |
| `config/global` | 全局常量（cc_pages_url、gcs_bucket） |
| `config/secrets` | deploy.sh 拉取的部署期 secrets |
| `config/zello` | Zello 账号与频道配置（`zello_voice_sidecar.py`） |
| `config/watch_sweep` / `config/inbox_sweep` | 两个清扫器的游标 |
| `scheduled_jobs` | cron-tool 任务（job_id、target、fire_at、cron、message、status） |
| `watch_tasks` | watch-task.py 长跑盯梢任务（name、interval、prompt、model、host） |

## Firestore 备份（三层，2026-08-10 起）
| 层 | 频率 | 防什么 |
|---|---|---|
| 托管 backup schedule | 每日，留 7 天 | 库整个没了 |
| PITR | 连续 7 天 | 数据被脚本改坏 —— 能回到出事前一分钟，托管备份只能回到昨天快照 |
| `firestore-backup-cron.sh` | 每周一 04:30 | 离线归档（GCS export）+ 配置可 diff（私有仓库脱敏 JSON） |

两个 database 都已开**删除保护**。JSON 快照**不含凭据不能用于还原** ——
要还原用 GCS export 或托管 backup。

## Bot Team 系统
Leader（协调派活）/ Teammate（执行汇报）两种角色，配置存 `bots/{name}.team`。`build_system_prompt()` 按角色动态注入协调规则（运行时 system prompt 已含完整规则，此处仅备忘）。Leader 在 team 频道 @mention 派活，也可走 Firestore Inbox 异步通信。

## CC Wiki v2
Wiki 路径 `~/my-wiki-v2/`，在线地址由 `WIKI_URL` 配置。**主动识别知识价值**：用户分享有长期价值的文章/分析时，问"要录入 Wiki 吗？"；每 10 次 ingest 或超一周提醒跑 `/wiki lint`。具体 ingest/query/lint 操作在 wiki skill 的 SKILL.md。

## 编码规范
- **Python**: 全异步（asyncio）；日志用 `logging.getLogger("closecrab.{module}")` 不用 print；错误 log + graceful degradation，不要 silent `except:`。
- **Channel 开发** → 必读 `.claude/rules/channels.md`（三平台同步、`_format_interactive_prompt`、ExitPlanMode/AskUserQuestion）
- **Worker 开发** → 必读 `.claude/rules/workers.md`（4 种 worker 的 IPC/ACP/MCP/事件/retry）
- **Skills 系统** → 必读 `.claude/rules/skills.md`；新建 skill **必须**用 `skill-creator`，不要手动建文件

## 重要约束
- **不要修改 `.env`** — deploy.sh 生成，手动改会被覆盖
- **不要直接 kill bot 进程** — 用 `/stop` 或 SIGTERM（触发 graceful shutdown 清理子进程）
- **deploy.sh 修改后** — 至少在一台 VM 上测 `./deploy.sh --cc-only` 通过
- **Firestore schema 变更** — 考虑已部署 bot 的向后兼容
- 退出码约定、不 commit secrets 见上文对应章节

## 部署拓扑
每 bot 独立机器（GCE VM / GKE Pod / gLinux），`git clone` + `deploy.sh`。升级：`git pull` → 重启进程（kill run.sh PID 或 `/restart`）。GKE Pod 必须挂载 SA key 访问 Firestore（Workload Identity 对 Firestore 不生效）。

## Troubleshooting
- **Bot 不响应 / 重复进程**: `ps aux | grep "run.sh\|closecrab"` 确认只有一组，多余的 kill；日志看 `~/.claude/closecrab/{name}/bot.log`
- **Claude CLI 卡住**: 查 `~/.claude/closecrab/{name}/` 下 stderr 文件的 API 错误
- **npm 版本冲突**: `which claude && ls -la $(which claude)` 确认 symlink 指向对的 npm prefix
- **Firestore 403**: `GOOGLE_APPLICATION_CREDENTIALS` 指向有效 SA key，且 SA 有 `roles/datastore.user`
- **健康检查**: `scripts/closecrab-smoke-test.sh <bot> --json --actions`
