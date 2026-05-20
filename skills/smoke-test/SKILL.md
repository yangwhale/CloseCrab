---
name: smoke-test
description: CloseCrab bot 部署/重启后健康检查。当用户说"体检"、"smoke test"、"health check"、"检查 bot"、"bot 还好吗"、"刚部署完确认下"或部署/重启后想验证状态时触发。覆盖进程、Firestore、worker secrets、日志活性、近期错误，并扫描 `~/.closecrab/smoke-tests.d/` 下用户自定义 drop-in 检查。
---

# CloseCrab Bot Smoke Test

部署/重启 CloseCrab bot 后的一站式健康检查。基于 GBrain `smoke-test` 设计模式移植 — 但 **v1 是 detect-only**（不做 auto-fix，因为 production bot 上自动修复风险太高）。

**v2（Round 3 沉淀, 2026-05-20）**: 加入 evolution loop 学到的两项 anti-pattern 检查:
- `binary_alignment` — 比对 bot 进程 lstart vs git HEAD commit time，落后即 FAIL，并打印 SIGHUP 重启命令。防 Round 3 anti-pattern 1（stale binary 跑 fast-path test 测出假 PASS）。
- `fast_path_callbacks` — 静态扫 `closecrab/channels/*.py`，所有 `_make_input_callback` 必须有 `is_inbox` 参数。防 Round 2/3 anti-pattern（inbox 派活走 user-facing 5min × N timeout）。

这两个 check 由 `scripts/check-binary-alignment.py` + bash 内嵌 grep 实现，无新依赖。

## 触发条件

- 用户说："体检 bunny"、"检查 bot 状态"、"smoke test"、"刚重启完确认下"
- 任何 bot deploy / restart 后的自我验证
- Inbox 收到 `/health` 命令（agent 自动回 JSON）
- 调度脚本 `dispatch-bot.sh` 之后

## 用法

```bash
# 检查单个 bot（带颜色，人读）
~/CloseCrab/scripts/closecrab-smoke-test.sh bunny

# 检查本机所有跑着的 bot
~/CloseCrab/scripts/closecrab-smoke-test.sh --all

# 只看总结行（适合脚本调用）
~/CloseCrab/scripts/closecrab-smoke-test.sh bunny --quiet

# JSON 输出（适合 agent 自动消费 / inbox 回复）
~/CloseCrab/scripts/closecrab-smoke-test.sh bunny --json

# JSON + 修复建议（skillpack-check 模式，给 agent 看）
~/CloseCrab/scripts/closecrab-smoke-test.sh bunny --json --actions
```

**Exit code = failed check 数**（`0` = 全过）。任何 caller 都可以拿 exit code 直接决策。

### JSON Schema（`--json` 输出）

```json
{
  "status": "ok | warn | fail",     // 总状态：fail>0 = fail, skip>0 = warn, all pass = ok
  "pass": 8,
  "fail": 1,
  "skip": 3,
  "bots": ["bunny"],
  "results": [
    {"name": "bot_process", "status": "pass", "detail": "pid=12345 rss=265MB"},
    ...
  ],
  "actions": [                       // 仅 --actions 时填充
    {
      "check": "bot_process",
      "cmd":   "cd ~/CloseCrab && nohup ./run.sh bunny > /tmp/bunny.run.log 2>&1 &",
      "reason": "Bot process not running; relaunch via run.sh supervisor."
    }
  ]
}
```

### Agent 用法（受 GBrain skillpack-check 启发）

当 agent / inbox / 其他 bot 想问 "这个 bot 还好吗"，跑：

```bash
~/CloseCrab/scripts/closecrab-smoke-test.sh <bot> --json --actions
```

然后根据返回的 `status` + `actions[]` 决策：
- `status: "ok"` → 啥都不做
- `status: "warn"` → 把 `results[]` 里 `skip` 的项目当 review 候选展示给人
- `status: "fail"` → 把 `actions[]` 里的 `cmd` paste 出来让 oncall 决定执行（**不要自动跑**）

## 输出语义

| 符号 | 状态 | 含义 |
|------|------|------|
| `✓` | pass | 检查通过 |
| `✗` | fail | 真问题，需要处理（计入 exit code） |
| `⊘` | skip | 检查不适用（如 secret 没设但可能用 ADC）— **不算 fail** |

## 内置检查

| Check | 说明 |
|-------|------|
| `bot_process` | `pgrep` 找 `python3 -m closecrab --bot {name}` 进程 |
| `run_sh_wrapper` | 是否有 `run.sh` supervisor（手动启动是 skip 不是 fail） |
| `firestore_sa_key` | `GOOGLE_APPLICATION_CREDENTIALS` 文件可读且 JSON 合法（未设 → skip，假设走 ADC） |
| `firestore_reachable` | `bots/{name}` doc 存在（用 8s timeout 防卡） |
| `worker_type` | 显示当前 worker（claude/gemini/openclaw/kilo） |
| `claude_settings` | `~/.claude/settings.json` 解析 OK |
| `worker_secret_*` | 按 worker_type 检查 — Claude 看 Vertex/API key；Gemini 看 API key；OpenClaw 看 Gateway `:18789`；Kilo 看 `kilo` 二进制 |
| `bot_log_recent` | `bot.log` mtime — <1h pass；<24h skip；>24h fail |
| `recent_errors` | 末尾 200 行的 ERROR/CRITICAL/Traceback 计数 |
| `drop_ins` | 扫描自定义检查 |

## Drop-in 自定义检查

放在 `~/.closecrab/smoke-tests.d/` 下（**所有 bot** 通用）或 `~/.closecrab/smoke-tests.d/{bot}/`（**单 bot** 专属）的 `*.sh` 文件会自动被发现执行。

合约：脚本第一行 stdout 必须以 `OK ` / `FAIL ` / `SKIP ` 开头。`BOT` 环境变量已注入。

**示例**：`~/.closecrab/smoke-tests.d/bunny/discord-token.sh`：

```bash
#!/usr/bin/env bash
set -u
token=$(timeout 5 python3 -c "
from google.cloud import firestore
db = firestore.Client(project='chris-pgp-host', database='closecrab')
d = db.collection('bots').document('$BOT').get().to_dict() or {}
print((d.get('discord') or {}).get('token','')[:10])
" 2>/dev/null)
if [ -n "$token" ]; then echo "OK discord token present (${token}...)"; else echo "FAIL no discord token"; exit 1; fi
```

## 设计原则（从 GBrain 移植 + CloseCrab 适配）

1. **Detect-only, no auto-fix** — 跑着的 production bot 自动重启会丢用户上下文，先 v1 detect → 让人/agent 决策
2. **Timeout everything** — 所有外部调用都 `timeout 5-8s`，不卡死
3. **Skip 不是 fail** — 可选能力没配（如 SA key、drop-ins 目录空）只 skip
4. **JSON-first for agents** — `--json` 输出让 inbox 消息 / cron / 其他 bot 能机器消费
5. **Drop-in 扩展** — 不改核心脚本就能为某 bot 加专属检查

## 后续可加（v2 候选）

- 按 worker_type 拉最近一次 firestore log 看 `status` / `error` 字段
- ✁ `--fix` flag 启用 careful auto-fix（如重启 cron-daemon、re-mount gcsfuse）— 需要 case-by-case 评估
- Cross-bot `/health` 聚合面板（Jarvis 一次查所有 teammate）

## 来源

移植自 [gbrain skills/smoke-test/SKILL.md](https://github.com/garrytan/gbrain/blob/main/skills/smoke-test/SKILL.md)。
GBrain 评估报告：https://cc.higcp.com/assets/gbrain-evaluation-20260519-101510.html
