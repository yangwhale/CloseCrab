#!/usr/bin/env python3
"""翻其他 bot 的对话日志 —— 「jarvis 现在在干啥？」的标准答案。

所有 bot 共用一个 service account，`bots/{name}/logs` 因此是**互相可读**的。
这是 fleet 里唯一能看到「别的 bot 正在跟用户聊什么」的地方：inbox 只看得到
显式派给你的活，registry 只有心跳，真正的上下文全在 logs 里。

典型场景：用户把一条消息发错了窗口（本该给 jarvis 的接着发给了 bunny），
与其猜，不如去看看这十分钟内哪个 bot 在聊这个话题。

用法：
    peek-bot-logs.py                      # 所有 bot，最近 6 小时，每条一行
    peek-bot-logs.py jarvis --hours 2
    peek-bot-logs.py all --grep 出题       # 关键词过滤（user + assistant 都搜）
    peek-bot-logs.py jarvis --limit 3 --full   # 打完整正文

字段说明（写死在这里省得每次去翻 schema）：
    user       用户这一轮说的话，**带 channel / 时间 / 来源前缀**
    assistant  bot 的完整回复，Firestore 侧截断在 10K。**没有 `prompt` 字段，
               也没有 `reply` 字段** —— 实测 7 个 bot × 20 条共 140 个文档，
               键只有 user/assistant/steps/timestamp/status/session_id/source/
               usage/worker_type/duration_seconds
    steps      过程轨迹，每条截 500 字符、最多 200 条 ⇒ 要结论看 assistant，
               不要看 steps
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from closecrab.constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE  # noqa: E402
from google.cloud import firestore  # noqa: E402

HKT = timezone(timedelta(hours=8))


def flat(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bot", nargs="?", default="all", help="bot 名，或 all（默认）")
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--grep", default=None, help="关键词，user+assistant 里任一命中即保留")
    ap.add_argument("--limit", type=int, default=40, help="最多打几条（按时间倒序取）")
    ap.add_argument("--full", action="store_true", help="打完整 user / assistant，不截断")
    args = ap.parse_args()

    db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    if args.bot == "all":
        names = sorted(d.id for d in db.collection("bots").stream())
    else:
        names = [args.bot]

    rows = []
    for name in names:
        q = (db.collection("bots").document(name).collection("logs")
             .where("timestamp", ">=", cutoff)
             .order_by("timestamp", direction=firestore.Query.DESCENDING)
             .limit(200))
        for doc in q.stream():
            d = doc.to_dict()
            user, asst = d.get("user", ""), d.get("assistant", "")
            if args.grep and args.grep not in (user + asst):
                continue
            rows.append((d.get("timestamp"), name, user, asst, d.get("source", "")))

    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[: args.limit]

    if not rows:
        # 空结果**不等于**「没在聊」—— 也可能是时间窗太窄或关键词没对上。
        # 把条件回显出来，免得下游把它当成确定性的「没有」。
        print(f"（{args.hours} 小时内没有匹配记录"
              f"{'，关键词 ' + args.grep if args.grep else ''}）")
        return 0

    for ts, name, user, asst, source in reversed(rows):  # 旧 → 新，读起来顺
        t = ts.astimezone(HKT).strftime("%m-%d %H:%M")
        if args.full:
            print(f"\n{'=' * 70}\n[{t} HKT] {name} ({source})\n"
                  f"--- 用户 ---\n{user}\n--- 回复 ---\n{asst}")
        else:
            print(f"[{t}] {name:16s} 用户: {flat(user, 70)}")
            print(f"{'':28s} 回复: {flat(asst, 70)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
