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
import subprocess
import sys
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
            ["claude", "-p", full, "--model", model, "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT, cwd="/tmp", env=env,
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        return "SKIP", ""
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

def notify_chat(name: str, text: str, bot: str | None = None) -> None:
    """chat 通道：直发飞书，**不产生任何 turn**。"""
    cmd = [sys.executable, str(_ROOT / "scripts" / "feishu-notify.py"),
           f"🤖 [{name}] {datetime.now(cron_tool.ZoneInfo('Asia/Hong_Kong')).strftime('%H:%M')}\n{text}"]
    if bot:
        cmd += ["--bot", bot]
    subprocess.run(cmd, capture_output=True, timeout=60)


def notify_inbox(task: dict, text: str) -> None:
    """inbox 通道：触发主进程一次完整 turn，让它接着往下做。

    `from` 必须是**真实存在的 bot 名**。第一版写成 `watch:<name>`，结果收件方
    回执时把它当收件人，那个 bot 不存在，回执就永久卡在 pending —— 每跑一个
    watch 任务漏一条。watch 的身份信息放在 instruction 正文里已经够了。
    """
    db().collection("messages").add({
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
    })


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
    db().collection(COLL).document(args.name).set(doc)
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


def cmd_tick(args):
    d = db()
    cutoff = NOW()
    acted = []
    for s in d.collection(COLL).where("status", "==", "active").stream():
        x = s.to_dict() or {}
        name = x.get("name")
        if not name:
            continue
        nf = x.get("next_fire_at")
        if not nf or nf > cutoff:
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

        ref = d.collection(COLL).document(name)

        # 兜底：跑太久了也要收，免得任务泄漏成永久后台负担
        age = (cutoff - task["created_at"]).total_seconds()
        if task.get("max_age_sec") and age > task["max_age_sec"]:
            notify_chat(name, f"⏱ 已盯了 {int(age/60)} 分钟仍未结束，自动停止本 watch。", task.get("notify_bot"))
            ref.update({"status": "expired"})
            acted.append({"name": name, "verdict": "EXPIRED"})
            continue

        skips = task.get("consecutive_skips") or 0
        # 老任务库里没有 model 字段，按默认档跑，不需要数据迁移
        verdict, body = run_probe(task["prompt"], task.get("last_report", ""), skips,
                                  task.get("model") or DEFAULT_MODEL)
        upd = {"next_fire_at": NOW() + timedelta(seconds=task.get("interval_sec", 120))}

        if verdict == "DONE":
            notify_chat(name, body, task.get("notify_bot"))
            notify_inbox(task, body)          # ← 交接主进程，本脚本存在的理由
            upd.update({"status": "done", "last_report": body})
        elif verdict == "REPORT":
            notify_chat(name, body, task.get("notify_bot"))
            upd.update({"last_report": body, "consecutive_skips": 0, "stall_notified": False})
        else:
            skips += 1
            upd["consecutive_skips"] = skips
            # 停滞检测：「还在跑」和「卡死了」在 SKIP 层面长得一样，
            # 只能靠连续多少轮没动静来区分。只播一次，不刷屏。
            if skips >= (task.get("stall_after") or DEFAULT_STALL_AFTER) and not task.get("stall_notified"):
                mins = skips * task.get("interval_sec", 120) / 60
                notify_chat(name, f"⚠️ 已连续 {skips} 轮（约 {mins:.0f} 分钟）无实质进展，可能卡住了。",
                            task.get("notify_bot"))
                upd["stall_notified"] = True

        ref.update(upd)
        acted.append({"name": name, "verdict": verdict})

    out = {"acted": acted, "count": len(acted)}
    if args.dry_run:
        out["dry_run"] = True
    print(json.dumps(out, ensure_ascii=False, default=str))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", aliases=["add"])
    c.add_argument("--name", required=True, type=_task_name)
    c.add_argument("--prompt", required=True, help="给探针 agent 的指令，讲清楚看哪里、怎么算跑完")
    c.add_argument("--interval", type=int, default=120, help="秒，默认 120")
    c.add_argument("--model", type=_model_tier, default=DEFAULT_MODEL,
                   help=f"探针档位 {'|'.join(MODEL_TIERS)}，默认 {DEFAULT_MODEL}。"
                        "看日志有没有变用 haiku；要读懂内容再判断用 sonnet；"
                        "要做真取舍用 opus")
    c.add_argument("--notify-bot", help="DONE 时把 inbox 交接给谁，默认 $BOT_NAME")
    c.add_argument("--stall-after", type=int, default=DEFAULT_STALL_AFTER,
                   help=f"连续多少轮无进展算疑似卡住，默认 {DEFAULT_STALL_AFTER}")
    c.add_argument("--max-age", type=int, default=6 * 3600, help="秒，超时自动收，默认 6 小时")
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
