#!/usr/bin/env python3
"""watch-task.py — 盯着一个长跑任务，有进展就播报，跑完了交接给主进程。

典型场景：起一个要跑 20 分钟的 TPU/GPU 训练，然后
  - 每 1-5 分钟看一眼，**有实质进展**才往飞书贴一条（不触发任何 LLM turn）
  - 判断**跑完了**，除了贴结论，还要推一条 inbox 让主进程接手跑下一步

跟兄弟脚本的分工（三者都不惊动主进程，判断力递增）：
  feishu-notify.py     直发飞书，零判断
  log-watch-notify.sh  纯 grep 计数差，只能贴原始日志
  agent-watch.sh       headless sub-agent 自行判断有无进展 → 但**只有 chat 出口**
  本脚本               在 agent-watch 之上补齐 agent-watch 缺的那一半：
                       inbox 交接、自终止、停滞检测、状态入 Firestore

为什么需要 inbox 出口（这是本脚本存在的理由）：
  「跑完了」跟「跑到第二步了」是两类事。前者需要有人接着做（跑下一个实验、
  收结果、改配置再跑），后者只是让人看一眼。只有前者值得触发一次主进程
  的完整 turn。把两者都走 chat，任务链就断在这里，得靠人盯着；都走 inbox，
  20 分钟的训练要烧 20 个完整 turn 还会不停打断正在进行的对话。
  详见 docs/task-scheduler-design.md §8.1。

三步协议（喂给 sub-agent 的输出契约）：
  SKIP    还在跑，没什么可说的        → 静默退出
  REPORT  有实质进展                  → 飞书贴一条，更新游标
  DONE    跑完了/失败了，该主进程接手  → 飞书贴结论 + inbox 交接 + 本任务终止

模型分档（--model，默认 haiku）：写任务的人最清楚这活儿难不难。
  haiku   看日志有没有变、进程还在不在、文件出现没有   ← 默认，这条路是高频的
  sonnet  要读懂内容再判断：报错致命还是可忽略、指标达标没
  opus    要做真判断和取舍：该不该动手、几个方案挑哪个

用法:
  watch-task.py create --name t80 --interval 120 --notify-bot jarvis \\
      --prompt "读 /tmp/t80.log 尾部，判断训练进度。跑完或失败时用 DONE。"
  watch-task.py create --name pool --interval 300 --model sonnet --prompt "..."
  watch-task.py list
  watch-task.py tick            # cron-daemon 调，不用手动跑
  watch-task.py stop <name>
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# cron-tool.py 带连字符，正常 import 不了；复用它的 Firestore/时间/抢占逻辑，
# 避免两套调度器各写一份而慢慢漂移。
_spec = importlib.util.spec_from_file_location("cron_tool", _ROOT / "scripts" / "cron-tool.py")
cron_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cron_tool)

from google.cloud import firestore  # noqa: E402

COLL = "watch_tasks"
NOW = cron_tool.NOW
_hkt = cron_tool._hkt

# 模型分档（docs/task-scheduler-design.md §6.5）。写任务的 bot 最清楚这活儿
# 难不难 —— daemon 手上只有一句 prompt，从字面分不出「看日志有没有 done」和
# 「判断能不能开训」的难度差。
#
# 取值直接用 CLI 的裸别名，不维护 {档位 → model ID} 映射表：那张表会随模型
# 换代过期，裸别名由 CLI 负责解析到当代模型。
MODEL_TIERS = ("haiku", "sonnet", "opus")
DEFAULT_MODEL = "haiku"
PROBE_TIMEOUT = 240
# 连续这么多轮没有实质进展就播报一次「疑似卡住」。只播一次，不重复刷。
DEFAULT_STALL_AFTER = 15
# 终态任务保留多久。R4 审计发现这张表**一个 GC 都没有** —— done / cancelled /
# expired 会一直躺着。今天表是空的所以看不出来，但这正是 messages 集合当初
# 涨到 3857 条的走法：没人回收的东西不会报错，只会慢慢变多。
SWEEP_TERMINAL_DAYS = 14
_SWEEP_MIN_INTERVAL = 3600   # 别每次 tick 都全扫

_CONTRACT = """

【上次播报内容】
{prev}

【已连续 {skips} 轮没有实质进展】

【输出规则 — 第一行必须是 SKIP / REPORT / DONE 之一】
SKIP    跟上次相比没有实质进展（没有新数字、新阶段、新故障）。只输出 SKIP 四个字母，别的都不要。
REPORT  有实质进展。第一行写 REPORT，之后 2-4 句中文播报，要有判断不要只念日志，带上关键数字。
DONE    任务已结束（成功或失败）。第一行写 DONE，之后 2-4 句中文结论：结果是什么、
        关键数字、以及**接下来该做什么**——这段会被送去触发主进程，它要靠这段决定下一步。

不要 markdown，不要标题，不要客套。
"""


def db():
    return cron_tool.db()


def this_host() -> str:
    return socket.gethostname()


def _is_mine(task: dict) -> bool:
    """这条任务该不该由本机跑。

    watch 任务跟 cron 提醒有个本质区别：cron 到期只是**往 Firestore 写一条
    inbox**，目标 bot 在哪台机器都自己收得到，所以哪台 daemon 干结果一样，
    共享那张表反而是冗余优势（挂一台不影响排期）。

    watch 到期是**在本机起 agent 跑 shell、读本机文件**。三台 daemon 抢同一
    条任务时，赢的那台可能根本没有那个日志文件 —— 探针会对着不存在的路径
    永远返回 SKIP，看起来一切正常，实际那个任务再也不会被真正盯到。抢占是
    原子的、不会重复执行，但**执行在了错的机器上**，这不是锁能解决的问题。

    所以按 host 钉死。没有 host 字段的老任务视为不限机器（保持旧行为）。
    """
    h = task.get("host")
    return (not h) or h == this_host()


def _task_name(v: str) -> str:
    """任务名直接当 Firestore 文档 ID 用，所以得守它的规矩。

    不校验的话，`--name a/b` 或 `--name __x__` 会在 set() 那一刻甩出一整页
    gRPC 堆栈（"Resource id ... is invalid because it is reserved"），
    看不出是自己名字起错了。R2 审计时是被这个真绊了一下才发现的。
    """
    v = v.strip()
    if not v:
        raise argparse.ArgumentTypeError("--name 不能为空")
    if "/" in v:
        raise argparse.ArgumentTypeError(f"--name 不能含 '/'（要当文档 ID 用）: {v!r}")
    if v in (".", ".."):
        raise argparse.ArgumentTypeError(f"--name 不能是 {v!r}")
    if v.startswith("__") and v.endswith("__"):
        raise argparse.ArgumentTypeError(
            f"--name 不能是 __xxx__ 形式（Firestore 保留 ID）: {v!r}")
    if len(v.encode()) > 1500:
        raise argparse.ArgumentTypeError("--name 超过 1500 字节")
    return v


def _interval(v: str) -> int:
    """间隔必须为正。

    `--interval 0` 或负数会让抢占算出一个**不在未来**的 next_fire_at，于是每个
    tick 都判定到期 —— 每 30 秒烧一个探针，一直烧到 max_age（默认 6 小时 = 720 次）。
    参数长得完全正常，日志里也只是「一直在跑」，没人看得出是输入错了。
    下限取 30 秒：daemon 的 tick 就是 30 秒，比它更密没有意义。
    """
    n = int(v)
    if n < 30:
        raise argparse.ArgumentTypeError(
            f"--interval 必须 ≥30 秒（daemon tick 就是 30 秒），收到 {n}")
    return n


def _model_tier(v: str) -> str:
    """档位校验。非法值必须报错，不能静默退回默认 —— 否则你以为在跑 opus，
    实际跑的是 haiku，而结论看起来一样是一段中文，根本发现不了。"""
    v = v.strip().lower()
    if v in MODEL_TIERS:
        return v
    if v == "none":
        raise argparse.ArgumentTypeError(
            "none 档不是 watch 任务 —— 每轮直投 inbox 就是一条 cron job，"
            "请改用 `cron-tool.py add`（见 design §6.5）")
    raise argparse.ArgumentTypeError(
        f"不支持的档位 {v!r}，只能是 {' / '.join(MODEL_TIERS)}")


# ── 探针 ────────────────────────────────────────────────────────────────────

def claude_bin() -> str | None:
    """探针要的 `claude` 可执行文件，解析成绝对路径。

    两个坑叠在一起，合成一个**静默永久失败**：
    1. daemon 的 PATH 是 `/usr/local/bin:/usr/bin:/bin:/usr/games` —— 交互 shell
       里找得到的东西，daemon 里不一定找得到。
    2. 不是每台机器都装了 Claude Code CLI。跑 kilo / openclaw worker 的机器
       （hulk 所在的 gLinux 就是）压根没有这个命令。
    结果：任务建得下去、看着成功，然后每个周期抛一次 FileNotFoundError。
    而 ERROR 分支既不涨 consecutive_skips 也就不会触发停滞播报 —— 用户什么都
    收不到，任务空转到 max_age 才消失。所以要在**创建时**就拦下来。
    """
    p = shutil.which("claude")
    if p:
        return p
    for c in (Path.home() / ".npm-global/bin/claude",
              Path.home() / ".local/bin/claude",
              Path.home() / ".claude/local/claude"):
        if c.exists() and os.access(c, os.X_OK):
            return str(c)
    return None


def run_probe(prompt: str, prev: str, skips: int, model: str = DEFAULT_MODEL) -> tuple[str, str]:
    """跑一次 headless sub-agent。返回 (verdict, body)。

    默认 haiku：这条路每 1-2 分钟就走一次，20 分钟的训练要走十几次，多数轮次
    的结论就是「没变化」。要判断力的任务由写方显式抬档（--model sonnet/opus），
    而不是所有人陪着最贵的跑。
    """
    full = prompt + _CONTRACT.format(prev=prev or "（还没报过）", skips=skips)
    try:
        # 必须剥掉 ANTHROPIC_BETAS：本机设了 context-1m-2025-08-07 解锁 1M context，
        # 但 **haiku 不支持这个 beta**，带着它调直接 400。坑在于 opus/sonnet 带着
        # 都正常，只有最便宜、也就是默认的那一档会挂。
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BETAS"}
        out = subprocess.run(
            [claude_bin() or "claude", "-p", full, "--model", model,
             "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT, cwd="/tmp", env=env,
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        # 跟「没进展」区分开。两者都不该播报，但一个是任务在正常跑，另一个是
        # 探针自己就没跑完 —— 全都记成 SKIP 的话，一个每轮都超时的探针会一路
        # 静默到 max_age，看起来跟「任务还在跑」一模一样。
        return "TIMEOUT", ""
    if not out:
        return "SKIP", ""
    head, _, rest = out.partition("\n")
    head = head.strip().upper()
    for v in ("DONE", "REPORT", "SKIP"):
        if head.startswith(v):
            # 第一行可能是 "REPORT" 也可能是 "REPORT: xxx"，两种都收
            inline = head[len(v):].lstrip(":： ").strip()
            body = "\n".join(x for x in [inline, rest.strip()] if x)
            return v, body
    # 模型没守契约：当成有话要说，宁可多播报一条也别把进展吞掉
    return "REPORT", out


# ── 出口 ────────────────────────────────────────────────────────────────────

# REPORT 是每隔几分钟一条的状态更新，念全文会很吵。只取第一句。
_BRIEF_MAX = 50
_FULL_MAX = 300


def _brief(text: str) -> str:
    """取第一个自然停顿作为简短播报。REPORT 用。

    分三级退让：句末标点 → 逗号/顿号 → 硬截。硬截是最后手段，因为它会把
    「228 TFLOP/s」砍成「228 TF」，念出来像卡带。
    """
    t = " ".join(text.split())
    if len(t) <= _BRIEF_MAX:
        return t
    for seps in (("。", "！", "？", ". ", "! ", "? "), ("，", "；", "、", ", ", "; ")):
        best = -1
        for sep in seps:
            i = t.find(sep)
            if 0 < i <= _BRIEF_MAX and i > best:
                best = i
        if best > 0:
            return t[:best] + "。"
    return t[:_BRIEF_MAX] + "…"


def notify_voice(name: str, text: str, bot: str, brief: bool) -> None:
    """推进 bot 的 Discord 语音频道直播流。**零 turn**，跟 notify_chat 一样是旁路。

    走 sidecar 的 unix socket，跟正常 turn 的播报同一条队列同一个频道 —— 用户挂在
    那个频道上就能听见，不用去翻 DM。sidecar 没起 / 没连语音频道 → 连不上，静默跳过。

    best-effort：语音挂了不许影响 chat 播报和 inbox 交接。
    """
    body = _brief(text) if brief else " ".join(text.split())[:_FULL_MAX]
    if not body:
        return
    # REPORT 是轻量状态更新，语气放松；DONE 是结论，让它平实一点
    spoken = f"[casually] {body}" if brief else f"[thinking] {body}"
    # 带 fid 的按结论入队，排队久了也不丢。REPORT 不带 —— 状态更新过期就该丢。
    req = {"text": spoken}
    if not brief:
        req["fid"] = f"watch{int(time.time())}"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(f"/tmp/closecrab-voice-{bot}.sock")
            s.sendall((json.dumps(req) + "\n").encode())
            s.recv(256)
    except Exception as e:
        print(json.dumps({"warn": f"notify_voice {name}: {e}"}), file=sys.stderr)


def notify_chat(name: str, text: str, bot: str | None = None) -> None:
    """chat 通道：直发飞书，**不产生任何 turn**。

    best-effort：飞书挂了不该有能力打断主链路。R3 审计发现，这里一抛异常，
    后面的 inbox 交接就不走了，整轮 tick 也跟着崩 —— 一个旁路播报通道把
    真正要紧的那件事拖下了水。
    """
    cmd = [sys.executable, str(_ROOT / "scripts" / "feishu-notify.py"),
           f"🤖 [{name}] {datetime.now(cron_tool.ZoneInfo('Asia/Hong_Kong')).strftime('%H:%M')}\n{text}"]
    if bot:
        cmd += ["--bot", bot]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception as e:
        print(json.dumps({"warn": f"notify_chat failed for {name}: {e}"}), file=sys.stderr)


def _inbox_payload(task: dict, text: str) -> dict:
    """inbox 通道的消息体：触发主进程一次完整 turn，让它接着往下做。

    `from` 必须是**真实存在的 bot 名**。第一版写成 `watch:<name>`，结果收件方
    回执时把它当收件人，那个 bot 不存在，回执就永久卡在 pending —— 每跑一个
    watch 任务漏一条。watch 的身份信息放在 instruction 正文里已经够了。
    """
    return {
        "from": task.get("sender") or task.get("notify_bot") or "cron",
        "to": task["notify_bot"],
        "instruction": (
            f"[✅ 后台任务完成 · {task['name']}]\n{text}\n\n"
            f"（本条由 watch-task 在判定任务结束时推送，需要你接手下一步；"
            f"该 watch 任务已自行终止，不必再删）"
        ),
        "task_id": f"watch-{task['name']}",
        "status": "pending",
        "result": "",
        "created_at": NOW(),
    }


def notify_inbox(task: dict, text: str) -> None:
    """非事务版本，留给外部调用方；收尾路径走 _finalize_done。"""
    db().collection("messages").add(_inbox_payload(task, text))


def _finalize_done(d, name: str, task: dict, body: str) -> bool:
    """收尾：把 status 翻成 done 和写 inbox 放进同一个事务。

    返回 True 表示这一方赢得了收尾权（inbox 已写）；False 表示别人已经收过尾，
    什么都不该再做。两个写都在 Firestore，所以原子性是免费的 —— 没有理由让
    「交接了但没标记」或「标记了但没交接」这两种半截状态存在。
    """
    ref = d.collection(COLL).document(name)
    msg_ref = d.collection("messages").document()
    tx = d.transaction()

    @firestore.transactional
    def _run(t):
        snap = ref.get(transaction=t)
        if not snap.exists or (snap.to_dict() or {}).get("status") != "active":
            return False
        t.set(msg_ref, _inbox_payload(task, body))
        t.update(ref, {
            "status": "done",
            "last_report": body,
            "done_at": NOW(),
        })
        return True

    return _run(tx)


# ── 命令 ────────────────────────────────────────────────────────────────────

def cmd_create(args):
    now = NOW()
    doc = {
        "name": args.name,
        "prompt": args.prompt,
        "interval_sec": args.interval,
        "model": args.model,
        # 钉在创建它的这台机器上：探针读的是本机文件，换台机器跑就是错的
        "host": this_host(),
        "notify_bot": args.notify_bot or os.environ.get("BOT_NAME", "jarvis"),
        "sender": os.environ.get("BOT_NAME") or args.notify_bot or "jarvis",
        "status": "active",
        # anchor=complete：下一次从**跑完**算起，不是从触发算起。探针本身
        # 要十几秒，用 fire 锚点会让间隔越漂越前，长任务上尤其明显。
        "next_fire_at": now + timedelta(seconds=args.interval),
        "created_at": now,
        "last_report": "",
        "fire_count": 0,
        "consecutive_skips": 0,
        "stall_after": args.stall_after,
        "stall_notified": False,
        "max_age_sec": args.max_age,
    }
    if args.test_run_id:
        doc["test_run_id"] = args.test_run_id
    if not claude_bin():
        print(json.dumps({
            "error": f"本机 ({this_host().split('.')[0]}) 找不到 claude 可执行文件，"
                     f"watch 任务的探针跑不了 —— 拒绝创建",
            "why": "watch 任务钉死在创建它的机器上，探针要在本机跑 `claude -p`。"
                   "跑 kilo / openclaw worker 的机器通常没装这个 CLI。",
            "hint": "去装了 Claude Code CLI 再建，或者改到装了的机器上建。",
        }, ensure_ascii=False))
        sys.exit(2)
    ref = db().collection(COLL).document(args.name)
    # 名字直接当文档 ID，所以重名是覆盖而不是新建。原来这里是裸 set()：
    # 第二个任务顶掉第一个，第一个从此再没有人盯，而且两边都不报错 ——
    # 提交的人以为在跑，被顶掉的那个悄无声息。
    prev = ref.get()
    if prev.exists and (prev.to_dict() or {}).get("status") == "active" and not args.force:
        old = prev.to_dict() or {}
        print(json.dumps({
            "error": f"watch 任务 {args.name!r} 已存在且在运行中，拒绝覆盖",
            "existing": {
                "host": old.get("host"), "fire_count": old.get("fire_count"),
                "next_fire_hkt": _hkt(old.get("next_fire_at")),
                "prompt": (old.get("prompt") or "")[:80],
            },
            "hint": f"换个名字，或先 `watch-task.py stop {args.name}`，或加 --force 覆盖",
        }, ensure_ascii=False))
        sys.exit(2)
    ref.set(doc)
    print(json.dumps({
        "name": args.name, "interval_sec": args.interval,
        "model": args.model, "host": doc["host"],
        "first_fire_hkt": _hkt(doc["next_fire_at"]),
        "notify_bot": doc["notify_bot"],
    }, ensure_ascii=False))


def cmd_list(args):
    out = []
    for s in db().collection(COLL).stream():
        x = s.to_dict() or {}
        if not args.all and x.get("status") != "active":
            continue
        out.append({
            "name": x.get("name"), "status": x.get("status"),
            "interval_sec": x.get("interval_sec"),
            "model": x.get("model") or DEFAULT_MODEL,
            "host": x.get("host") or "(不限)",
            "next_fire_hkt": _hkt(x.get("next_fire_at")),
            "fire_count": x.get("fire_count"),
            "consecutive_skips": x.get("consecutive_skips"),
            "last_report": (x.get("last_report") or "")[:60],
        })
    print(json.dumps({"tasks": out, "count": len(out)}, ensure_ascii=False))


def cmd_stop(args):
    # 停任务不需要探针，任何机器都得能停 —— 尤其是那台装不了 claude 的。
    ref = db().collection(COLL).document(args.name)
    if not ref.get().exists:
        print(json.dumps({"error": f"watch task {args.name} not found"}))
        sys.exit(1)
    ref.update({"status": "cancelled"})
    print(json.dumps({"name": args.name, "status": "cancelled"}))


def _claim(d, name: str, cutoff: datetime):
    """原子抢占，跟 cron-tool 同一套理由：daemon 跑在三台机器上。

    这里比 cron job 更要紧——探针要跑十几秒，两台同时起会真的重复烧钱。
    """
    ref = d.collection(COLL).document(name)
    tx = d.transaction()

    @firestore.transactional
    def _run(t):
        snap = ref.get(transaction=t)
        if not snap.exists:
            return None
        x = snap.to_dict() or {}
        if x.get("status") != "active":
            return None
        nf = x.get("next_fire_at")
        if not nf or nf > cutoff:
            return None
        # 先把 next_fire_at 推远，占住这一轮；真正的 anchor=complete
        # 在探针跑完后再写一次准确值。
        t.update(ref, {
            "next_fire_at": cutoff + timedelta(seconds=x.get("interval_sec", 120)),
            "fire_count": (x.get("fire_count") or 0) + 1,
        })
        return x

    return _run(tx)


def _expire_if_stale(d, name: str, task: dict, cutoff) -> bool:
    """超龄就收，**任何一台机器都能收**。返回 True 表示这一方收的。

    过期判断是纯时间判断，不需要读本机文件，所以不该锁在 host 后面。以前它在
    `_run_one` 里，而 `_run_one` 只有本机任务才走得到 —— 于是任务被钉死在一台
    永久下线的机器上时：没人 claim（不是自己的）、没人过期（跑不到那行）、
    sweep 也不收（sweep 只管终态）。任务永远 active 躺在 list 里，看着像有人在盯，
    实际什么都没在盯。
    """
    ts = task.get("created_at")
    ma = task.get("max_age_sec")
    if not ma or ts is None or (cutoff - ts).total_seconds() <= ma:
        return False
    ref = d.collection(COLL).document(name)
    tx = d.transaction()

    @firestore.transactional
    def _run(t):
        snap = ref.get(transaction=t)
        if not snap.exists or (snap.to_dict() or {}).get("status") != "active":
            return False
        t.update(ref, {"status": "expired", "expired_at": NOW()})
        return True

    if not _run(tx):
        return False
    mins = int((cutoff - ts).total_seconds() / 60)
    notify_chat(name, f"⏱ 已盯了 {mins} 分钟仍未结束，自动停止本 watch。",
                task.get("notify_bot"))
    return True


def _sweep(d, now) -> int:
    """删掉过期的终态任务。跟 cron-tool 的 sweep 同一个理由和同一个节奏。"""
    meta = d.collection("config").document("watch_sweep")
    snap = meta.get()
    last = (snap.to_dict() or {}).get("last_swept_at") if snap.exists else None
    if last and (now - last).total_seconds() < _SWEEP_MIN_INTERVAL:
        return 0
    before = now - timedelta(days=SWEEP_TERMINAL_DAYS)
    # 先收集再删：边 stream 边 delete 会让 Firestore 的分页游标错位漏扫，
    # messages 那次 sweep 就是这么把统计数字跑飞的。
    doomed = []
    for s_ in d.collection(COLL).stream():
        x = s_.to_dict() or {}
        if x.get("status") not in ("done", "cancelled", "expired"):
            continue
        ts = x.get("created_at")
        # 没时间戳的终态任务永远算不出年龄，也就永远扫不掉 —— 直接收
        if ts is None or ts < before:
            doomed.append(s_.reference)
    for ref in doomed:
        try:
            ref.delete()
        except Exception as e:
            print(json.dumps({"warn": f"sweep {ref.id}: {e}"}), file=sys.stderr)
    meta.set({"last_swept_at": now}, merge=True)
    return len(doomed)


def cmd_tick(args):
    d = db()
    cutoff = NOW()
    acted = []
    swept = 0
    if not args.dry_run:
        try:
            swept = _sweep(d, cutoff)
        except Exception as e:
            print(json.dumps({"warn": f"sweep failed: {e}"}), file=sys.stderr)
    for s in d.collection(COLL).where("status", "==", "active").stream():
        x = s.to_dict() or {}
        name = x.get("name")
        if not name:
            continue
        # 测试夹具不许在真实循环里跑。跟 cron-tool 同一条规矩、同一个字段名。
        # 之前这里是空的，于是 R5 的端到端夹具真的把一条 inbox 交接推给了活的
        # bot，烧掉一个完整 turn —— 那条「训练完成、建议收集权重」的消息里
        # 说的是一个我自己刚写又刚删的假日志。--dry-run 仍然展示它们：
        # dry-run 什么都不写，看夹具正是它的用途。
        if x.get("test_run_id") and not args.dry_run:
            continue

        nf = x.get("next_fire_at")
        if not nf or nf > cutoff:
            continue

        # 超龄先收，且放在 host 判定之前 —— 否则钉在下线机器上的任务永远没人收。
        if args.dry_run:
            ts, ma = x.get("created_at"), x.get("max_age_sec")
            if ma and ts is not None and (cutoff - ts).total_seconds() > ma:
                acted.append({"name": name, "would_expire": True})
                continue
        elif _expire_if_stale(d, name, x, cutoff):
            acted.append({"name": name, "verdict": "EXPIRED"})
            continue

        # **必须在抢占之前判**：先 claim 再发现不是自己的，next_fire_at 已经被
        # 推到下一个周期了，真正该跑的那台机器这一轮就被饿死 —— 而且是静默的。
        if not _is_mine(x):
            continue

        if args.dry_run:
            acted.append({"name": name, "would_probe": True, "due_hkt": _hkt(nf)})
            continue

        task = _claim(d, name, cutoff)
        if task is None:
            continue

        try:
            acted.append(_run_one(d, name, task, cutoff))
        except Exception as e:
            # 一条任务出错不能拖垮整轮。R3 审计发现：探针崩了 / 飞书挂了 /
            # Firestore 写失败，异常都会一路冒出 cmd_tick，**后面排队的任务
            # 这一轮全都不跑**。一条持续坏掉的任务足以永久饿死其它所有任务，
            # 而且从外面看只是「怎么没动静」。
            print(json.dumps({"error": f"{name}: {e}"}, ensure_ascii=False), file=sys.stderr)
            acted.append({"name": name, "verdict": "ERROR", "error": str(e)[:200]})

    out = {"acted": acted, "count": len(acted)}
    if swept:
        out["swept"] = swept
    if args.dry_run:
        out["dry_run"] = True
    print(json.dumps(out, ensure_ascii=False, default=str))


def _run_one(d, name: str, task: dict, cutoff) -> dict:
    """跑一条已经抢到手的任务。抽出来是为了让上面那个 try 的边界清清楚楚。"""
    ref = d.collection(COLL).document(name)

    # 超龄兜底已经上移到 cmd_tick（在 host 判定之前），这里不再重复判。
    skips = task.get("consecutive_skips") or 0
    # 老任务库里没有 model 字段，按默认档跑，不需要数据迁移
    verdict, body = run_probe(task["prompt"], task.get("last_report", ""), skips,
                              task.get("model") or DEFAULT_MODEL)
    upd = {"next_fire_at": NOW() + timedelta(seconds=task.get("interval_sec", 120))}

    if verdict == "DONE":
        # inbox 交接和「标记 done」必须一起成功或一起失败，否则两种坏结局二选一：
        # 先发 inbox 再标记，标记失败 → 下一轮重判 DONE，同一条结论再交接一次
        # （每轮烧主进程一个完整 turn）；先标记再发 inbox，inbox 失败 → 任务链
        # 静默断掉。两者写的都是 Firestore，放进一个事务就都不用选。
        # 只有把 status 从 active 翻成 done 的那一方才写 inbox。
        if _finalize_done(d, name, task, body):
            notify_chat(name, body, task.get("notify_bot"))
            notify_voice(name, body, task.get("notify_bot") or "jarvis", brief=False)
        return {"name": name, "verdict": verdict}
    elif verdict == "REPORT":
        notify_chat(name, body, task.get("notify_bot"))
        notify_voice(name, body, task.get("notify_bot") or "jarvis", brief=True)
        upd.update({"last_report": body, "consecutive_skips": 0, "stall_notified": False})
    elif verdict == "TIMEOUT":
        # 探针没跑完。不播报（跟 SKIP 一样安静），但单独计数：连续超时说明
        # prompt 太重或者机器扛不住，跟「任务还在跑」是两回事。
        touts = (task.get("consecutive_timeouts") or 0) + 1
        upd["consecutive_timeouts"] = touts
        skips += 1
        upd["consecutive_skips"] = skips
        if touts == 3:
            notify_chat(name,
                        f"⚠️ 探针连续 {touts} 轮超时（每轮上限 {PROBE_TIMEOUT}s），"
                        f"这不代表任务卡住，是判断本身没跑完 —— prompt 可能太重。",
                        task.get("notify_bot"))
    else:
        skips += 1
        upd["consecutive_skips"] = skips
        upd["consecutive_timeouts"] = 0
        # 停滞检测：「还在跑」和「卡死了」在 SKIP 层面长得一样，
        # 只能靠连续多少轮没动静来区分。只播一次，不刷屏。
        if skips >= (task.get("stall_after") or DEFAULT_STALL_AFTER) and not task.get("stall_notified"):
            mins = skips * task.get("interval_sec", 120) / 60
            notify_chat(name, f"⚠️ 已连续 {skips} 轮（约 {mins:.0f} 分钟）无实质进展，可能卡住了。",
                        task.get("notify_bot"))
            upd["stall_notified"] = True

    ref.update(upd)
    return {"name": name, "verdict": verdict}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", aliases=["add"])
    c.add_argument("--name", required=True, type=_task_name)
    c.add_argument("--prompt", required=True, help="给探针 agent 的指令，讲清楚看哪里、怎么算跑完")
    c.add_argument("--interval", type=_interval, default=120, help="秒，默认 120，最小 30")
    c.add_argument("--model", type=_model_tier, default=DEFAULT_MODEL,
                   help=f"探针档位 {'|'.join(MODEL_TIERS)}，默认 {DEFAULT_MODEL}。"
                        "看日志有没有变用 haiku；要读懂内容再判断用 sonnet；"
                        "要做真取舍用 opus")
    c.add_argument("--notify-bot", help="DONE 时把 inbox 交接给谁，默认 $BOT_NAME")
    c.add_argument("--stall-after", type=int, default=DEFAULT_STALL_AFTER,
                   help=f"连续多少轮无进展算疑似卡住，默认 {DEFAULT_STALL_AFTER}")
    c.add_argument("--max-age", type=int, default=6 * 3600, help="秒，超时自动收，默认 6 小时")
    c.add_argument("--test-run-id", help="打上就只在 --dry-run 里出现，绝不真跑（测试夹具专用）")
    c.add_argument("--force", action="store_true",
                   help="同名任务已在运行时覆盖它（默认拒绝，避免顶掉别人的任务）")
    c.set_defaults(fn=cmd_create)

    l = sub.add_parser("list")
    l.add_argument("--all", action="store_true")
    l.set_defaults(fn=cmd_list)

    s_ = sub.add_parser("stop", aliases=["remove", "rm"])
    s_.add_argument("name")
    s_.set_defaults(fn=cmd_stop)

    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    t.set_defaults(fn=cmd_tick)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
