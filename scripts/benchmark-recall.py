#!/usr/bin/env python3
"""Benchmark: 量化 session recall 注入对回答质量的影响。

对每条 query 跑 2 次（recall ON / OFF），每次起新 session 隔离上下文。
输出 JSON 到 /tmp/benchmark-recall-{ts}.json 供人工评分。

用法:
    python3 scripts/benchmark-recall.py --bot tianmaojingling --user <user_id>
    python3 scripts/benchmark-recall.py --bot tianmaojingling --user <user_id> --queries 1,2,3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from closecrab.workers.claude_code import ClaudeCodeWorker
from closecrab.utils.session_recall import recall_history
from closecrab.utils.config_store import load_bot_config_from_firestore as load_bot_config
from closecrab.main import build_system_prompt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("benchmark-recall")

# 15 条测试 query — A 强依赖历史, B 中等依赖, C 完全不依赖
QUERIES = [
    # A 档：强依赖历史
    ("A1", "强依赖", "不是，这个东西不应该每一次对话都去recall吧？这个不应该是启动一个新的session的时候recall一次就好了吗？"),
    ("A2", "强依赖", "那个PTT不是已经有了吗？现在我们有一个叫Hold to Talk的按钮，只不过是没有实现成这个我们想要的样子。"),
    ("A3", "强依赖", "就什么也没干，就光聊了几轮的天，你就能有700多K的上下文，这涨的速度也太快了，你拆开一下。"),
    ("A4", "强依赖", "你这个改动对原有的这个飞书Channel的交互没有任何影响，对吧？"),
    ("A5", "强依赖", "仔细讲一下，你把什么东西给撤了？"),
    # B 档：中等依赖
    ("B1", "中等依赖", "你给我讲讲 Deepseek v4 的模型架构。"),
    ("B2", "中等依赖", "帮我研究一下 Tailscale 的工作原理。"),
    ("B3", "中等依赖", "那两台服务器都没有公网IP，全都是通过NAT访问外网的，它们俩之间怎么建立的联系？用的是啥协议？"),
    ("B4", "中等依赖", "Hold to Talk 这种控制 STT 的开始与结束的方式和解决方案。"),
    ("B5", "中等依赖", "那本身 OPAAS 就能做 opening，为啥还要额外再调个模型？"),
    # C 档：完全不依赖
    ("C1", "无依赖", "你帮我把 worker 切换成 kilo。"),
    ("C2", "无依赖", "今天日期是几号？"),
    ("C3", "无依赖", "什么是 prompt cache？"),
    ("C4", "无依赖", "帮我写一个 Python 的快速排序。"),
    ("C5", "无依赖", "Vertex AI 跟 OpenAI API 调用方式有什么区别？"),
]


async def run_one(
    query: str,
    *,
    bot_name: str,
    user_id: str,
    system_prompt: str,
    claude_bin: str,
    work_dir: str,
    model: str,
    with_recall: bool,
) -> dict:
    """跑一条 query，返回 {reply, recall_chars, duration, usage}。"""
    # Build content (with or without recall)
    if with_recall:
        recall_block = recall_history(
            bot_name=bot_name,
            user_id=user_id,
            query=query,
            limit=5,
            days=60,
        )
        recall_chars = len(recall_block)
        content = (recall_block + "\n\n---\n\n" + query) if recall_block else query
    else:
        recall_block = ""
        recall_chars = 0
        content = query

    # Fresh worker per run — no session resume, full isolation
    worker = ClaudeCodeWorker(
        claude_bin=claude_bin,
        work_dir=work_dir,
        timeout=180,
        system_prompt=system_prompt,
        session_id=None,
        model=model or None,
    )
    t0 = time.time()
    try:
        await worker.start()
        reply = await worker.send(content)
    except Exception as e:
        reply = f"<ERROR: {type(e).__name__}: {e}>"
    duration = time.time() - t0
    usage = dict(getattr(worker, "_usage", {}))
    try:
        await worker.stop()
    except Exception:
        pass
    return {
        "reply": reply,
        "recall_chars": recall_chars,
        "recall_block": recall_block,
        "content_chars": len(content),
        "duration_seconds": round(duration, 1),
        "usage": usage,
    }


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bot", default="tianmaojingling")
    p.add_argument("--user", default="ou_chrisya_placeholder",
                   help="user_id used for recall scoping (uses chrisya's open_id by default)")
    p.add_argument("--queries", default="",
                   help="comma-separated query IDs (A1,B2,...) — empty = all 15")
    p.add_argument("--out", default=None, help="output JSON path")
    args = p.parse_args()

    cfg = load_bot_config(args.bot)
    if not cfg:
        log.error(f"bot {args.bot} config not found in Firestore")
        sys.exit(1)

    channel_type = cfg.get("channel", "feishu")
    livekit_enabled = bool((cfg.get("livekit") or {}).get("enabled"))
    worker_type = cfg.get("worker_type", "claude")
    if worker_type != "claude":
        log.warning(f"bot uses worker_type={worker_type}; harness only supports claude. Forcing claude.")

    system_prompt = build_system_prompt(
        args.bot,
        team=cfg.get("team"),
        channel_type=channel_type,
        livekit_enabled=livekit_enabled,
        worker_type="claude",
        model=cfg.get("model", ""),
    )

    selected_ids = set(args.queries.split(",")) if args.queries.strip() else None
    queries = [q for q in QUERIES if not selected_ids or q[0] in selected_ids]
    log.info(f"running {len(queries)} queries × 2 runs each ({len(queries)*2} total LLM calls)")

    out_path = Path(args.out) if args.out else Path(f"/tmp/benchmark-recall-{int(time.time())}.json")
    results = []
    for qid, category, qtext in queries:
        log.info(f"=== [{qid} / {category}] {qtext[:60]}...")
        # OFF first (so any state leak biases AGAINST recall, not for)
        log.info(f"  [{qid}] run OFF (no recall)...")
        off = await run_one(
            qtext,
            bot_name=args.bot,
            user_id=args.user,
            system_prompt=system_prompt,
            claude_bin=cfg["claude_bin"],
            work_dir=os.path.expanduser(cfg["work_dir"]),
            model=cfg.get("model", ""),
            with_recall=False,
        )
        log.info(f"    OFF done: {off['duration_seconds']}s, reply_chars={len(off['reply'])}")

        log.info(f"  [{qid}] run ON (with recall)...")
        on = await run_one(
            qtext,
            bot_name=args.bot,
            user_id=args.user,
            system_prompt=system_prompt,
            claude_bin=cfg["claude_bin"],
            work_dir=os.path.expanduser(cfg["work_dir"]),
            model=cfg.get("model", ""),
            with_recall=True,
        )
        log.info(f"    ON  done: {on['duration_seconds']}s, reply_chars={len(on['reply'])}, "
                 f"recall_chars={on['recall_chars']}")

        results.append({
            "qid": qid,
            "category": category,
            "query": qtext,
            "off": off,
            "on": on,
        })
        # Write incrementally so partial progress is preserved on crash
        with out_path.open("w", encoding="utf-8") as f:
            json.dump({"timestamp": int(time.time()), "bot": args.bot, "results": results},
                      f, ensure_ascii=False, indent=2)
        log.info(f"  saved progress: {len(results)}/{len(queries)} → {out_path}")

    log.info(f"DONE. Results: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
