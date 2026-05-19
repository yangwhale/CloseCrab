# Cross-Bot Restart Protocol

> 评估者 bot SIGHUP 重启另一个 bot 的协议。Chris 在 evolution round 内永久授权这个动作。

## Why SIGHUP (not /restart, not SIGKILL)

| Method | Result | Caveat |
|---|---|---|
| `/restart` 飞书命令 | bot main.py 收到 → `sys.exit(42)` → run.sh restart | 需要 Chris 自己发，不适合 bot 间互调 |
| `SIGHUP` | main.py signal handler → `sys.exit(129)` → run.sh restart | ✅ 干净，保留 session_id，evolution 用这个 |
| `SIGTERM` | main.py signal handler → `sys.exit(143)` → run.sh **不重启** | 用于停止，不是重启 |
| `SIGKILL` | OS 立杀进程 | 丢未 flush 的 Firestore log + 子进程 zombie，**禁用** |

## Why nohup + sleep + setsid

直接 `kill -HUP $PID` 看起来够用，但会有一个陷阱：

```
bunny 的 ClaudeCodeWorker 在执行 bash tool
  └─ bash invokes restart-peer-bot.sh
       └─ kill -HUP xiaoai_PID  <-- 同步执行
       
问题: 如果 xiaoai 被 SIGHUP 后某种链路反作用到 bunny 的进程组（极少但发生过——
       比如 systemd-cgls 把 ssh forwarded session 也归一组），bunny 自己也被杀。
```

「nohup + setsid + bash -c 'sleep N && kill'」把 kill 命令彻底脱离调用者的进程组：

```bash
nohup setsid bash -c "sleep ${DELAY} && kill -HUP ${OLD_PID}" >/dev/null 2>&1 &
disown
```

- `nohup` → 忽略 HUP 信号
- `setsid` → 新 session，新进程组
- `bash -c "sleep N && ..."` → 加延迟，让调用者有时间发完报告 / 退出当前轮
- `>/dev/null 2>&1` → 不写当前终端，避免被 SIGPIPE 反伤
- `&` + `disown` → 当前 shell 不持有它

12s 是经验值（bunny 自己测过 work）：足够长，让调用 worker 把当前 turn 收尾；不太长，让 round 节奏不卡。重启 evaluator 自己时改 8s 也行。

## Verification Checklist

restart-peer-bot.sh 已经做了这些验证，但写下来供 manual debug：

1. **Old PID disappeared**
   ```bash
   pgrep -f "python3 -m closecrab.*--bot[= ]xiaoaitongxue"
   # 应该: 老 PID 没了 OR 已经是新 PID
   ```

2. **New PID appeared (different from old)**
   ```bash
   ps -p <new_PID> -o lstart=
   # 应该: 启动时间 = 刚才
   ```

3. **bot.log 有 startup 行**
   ```bash
   tail -20 ~/.claude/closecrab/<target>/bot.log
   # 应该看到: "Loading system_prompt" / "Phase E injecting" / "GBrain index ... chars"
   ```

4. **bot 响应 inbox / 飞书消息**
   ```bash
   python3 ~/CloseCrab/scripts/inbox-send.py <target> "ping after restart"
   # 30s 内应该收到回复
   ```

## Failure Modes

### A. 新 PID 没出现
- 原因：run.sh 也死了 / 退出码异常被 wrapper 当 fatal
- 排查：`ps aux | grep run.sh`，如果也没了，需要 ssh 上去手动启
  ```bash
  ssh <target_host>
  cd ~/CloseCrab && nohup ./run.sh <target> > /tmp/<target>.out 2>&1 &
  disown
  ```

### B. 新 PID 出现但 bot 不响应
- 原因：worker 初始化卡住（Firestore 401 / MCP server 启动失败 / GEMINI.md 写不进去）
- 排查：
  ```bash
  tail -100 ~/.claude/closecrab/<target>/bot.log
  ls -la ~/.claude/closecrab/<target>/  # 看 stderr_*.log
  ```

### C. SIGHUP 没生效（PID 还是老的）
- 原因：bot main.py 的 signal handler 被覆盖（新代码 bug）/ 进程被 ptrace
- 排查：`grep -r "SIGHUP" closecrab/main.py` 确认 handler 还在；用 `strace -p <PID>` 看是否在 syscall 卡死

### D. 调用 evaluator 自己挂了
- 原因：1% 概率的进程组链式反应，或者 evaluator worker 正在 timeout
- 救援：另一个 evaluator 拿过 ownership，重新发 round report；如果两个都挂了，Chris 介入

## Authorization Boundaries

Chris 原话（2026-05-19）：「你就互相 restart 呗，不要让我参与」

### 永久授权（不用问）
- 跨 bot SIGHUP restart (evolution round 内)
- patch peer bot 的 worker 源码 + push + 远程 pull
- dispatch evolution case 到 inbox
- 修对方的 SKILL.md / GBrain page

### 仍需 Chris 批
- 删 Firestore 数据 / 改 collection schema
- 改 channel tokens / API keys / 模型 endpoint
- restart 进程 > 3 次/小时 (loop 暴走保护)
- 跨机器迁移 bot
- Round 跨夜 + 还没收敛 (写报告封轮，找 Chris)

### Audit Log
所有跨 bot restart 写 `/tmp/cross-bot-restart.log`，每行：
```
<ISO ts UTC>: cross-bot SIGHUP fired to <target> PID <old> by <sender> (reason: <reason>)
<ISO ts UTC>: verified restart <old> → <new>
```

Round report 必须引用这个 log 的相关行。

## Examples

### bunny 重启 xiaoai（今晚已验证过 work 的范例）
```bash
# 当时手敲的命令:
nohup setsid bash -c "sleep 8 && kill -HUP 2650505" >/dev/null 2>&1 & disown
sleep 12
pgrep -f "python3 -m closecrab.*--bot xiaoaitongxue"
# → 输出 2740340 (新 PID, started 19:12)
grep "Phase E" ~/.claude/closecrab/xiaoaitongxue/bot.log | tail -1
# → "Phase E injecting 2342 chars GBrain index"
```

### 脚本封装后
```bash
bash ~/CloseCrab/skills/evolution/scripts/restart-peer-bot.sh xiaoaitongxue --reason "round-1-kilo-streaming-patch"
# 自动验证 PID + bot.log，退出码 0 = 成功
```
