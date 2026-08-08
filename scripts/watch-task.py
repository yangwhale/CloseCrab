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

用法:
  watch-task.py create --name t80 --interval 120 --notify-bot jarvis \\
      --prompt "读 /tmp/t80.log 尾部，判断训练进度。跑完或失败时用 DONE。"
  watch-task.py list
  watch-task.py tick            # cron-daemon 调，不用手动跑
  watch-task.py stop <name>
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
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

PROBE_MODEL = "claude-haiku-4-5-20251001"
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


# ── 探针 ────────────────────────────────────────────────────────────────────

def run_probe(prompt: str, prev: str, skips: int) -> tuple[str, str]:
    """跑一次 headless sub-agent。返回 (verdict, body)。

    用 haiku：这条路每 1-2 分钟就走一次，20 分钟的训练要走十几次，
    用主力模型是纯浪费——判断「日志有没有新东西」不需要那个档次的推理。
    """
    full = prompt + _CONTRACT.format(prev=prev or "（还没报过）", skips=skips)
    try:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BETAS"}
        out = subprocess.run(
            ["claude", "-p", full, "--model", PROBE_MODEL, "--dangerously-skip-permissions"],
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
    """inbox 通道：触发主进程一次完整 turn，让它接着往下做。"""
    db().collection("messages").add({
        "from": f"watch:{task['name']}",
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
        "notify_bot": args.notify_bot or os.environ.get("BOT_NAME", "jarvis"),
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
        verdict, body = run_probe(task["prompt"], task.get("last_report", ""), skips)
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

    c = sub.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--prompt", required=True, help="给探针 agent 的指令，讲清楚看哪里、怎么算跑完")
    c.add_argument("--interval", type=int, default=120, help="秒，默认 120")
    c.add_argument("--notify-bot", help="DONE 时把 inbox 交接给谁，默认 $BOT_NAME")
    c.add_argument("--stall-after", type=int, default=DEFAULT_STALL_AFTER,
                   help=f"连续多少轮无进展算疑似卡住，默认 {DEFAULT_STALL_AFTER}")
    c.add_argument("--max-age", type=int, default=6 * 3600, help="秒，超时自动收，默认 6 小时")
    c.set_defaults(fn=cmd_create)

    l = sub.add_parser("list")
    l.add_argument("--all", action="store_true")
    l.set_defaults(fn=cmd_list)

    s_ = sub.add_parser("stop")
    s_.add_argument("name")
    s_.set_defaults(fn=cmd_stop)

    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    t.set_defaults(fn=cmd_tick)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
