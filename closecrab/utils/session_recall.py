"""S1 Background Review — auto-recall relevant history from the per-bot
FTS5 SessionIndex and format as a context block for prompt injection.

Design: zero extra LLM calls. The bot already has FTS5 mirrored writes
(see ``session_search.SessionIndex``); recall is just keyword extraction +
N single-term searches + dedup + format. Per-turn latency budget < 100ms.

The output of :func:`recall_history` is intended to be prepended to the
user's message before being sent to the worker, NOT to be persisted back
to the index or to the firestore log doc (that would create a recursive
pollution loop — past recall blocks would be re-indexed and surface in
future recalls). Caller must keep ``msg.content`` and the augmented
``content`` separate. See ``bot.py:_handle_message_locked``.

Silent on any failure: callers receive ``""`` and degrade to S0 behavior.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from closecrab.utils.session_search import SessionIndex

log = logging.getLogger("closecrab.session_recall")


# Channel adapters prefix user text with provenance markers like
# "[from: 飞书私聊]\n" before BotCore sees it. Strip these before keyword
# extraction so words like "飞书" don't dominate recall.
_PREFIX_PATTERNS = [
    re.compile(r"^\s*\[from:[^\]]+\]\s*\n?", re.IGNORECASE),
    re.compile(r"^\s*\[Teammate [^\]]+\]\s*\n?"),
    re.compile(r"^\s*\[System:[^\]]+\]\s*\n?"),
    re.compile(r"^\s*\[相关历史召回[^\]]*\][\s\S]*?\n---\n\n?"),
]

# Stopwords filtered out of keyword extraction. Tuned for the kinds of
# things users actually say to bots in this repo, not corpus-wide.
_STOP_CN = {
    "的", "了", "吗", "呢", "啊", "嗯", "呀", "哦", "哈", "吧",
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "咱们",
    "这", "那", "这个", "那个", "这些", "那些", "这里", "那里",
    "什么", "怎么", "为啥", "为什么", "怎样", "怎么样",
    "上次", "之前", "刚才", "昨天", "今天", "明天", "现在", "最近",
    "可以", "应该", "需要", "必须", "想要", "知道",
    "一下", "一个", "一些", "一直", "一定", "一起",
    "就是", "还是", "或者", "但是", "因为", "所以", "如果",
    "已经", "正在", "还没", "没有", "还有",
    "比较", "非常", "特别", "稍微", "有点", "好像",
    "怎么办", "怎么搞", "搞定", "弄好", "做好",
    "对吧", "对吗", "好吗", "行吗", "对不对", "是不是",
    "你能", "能不能", "可不可以", "帮我", "帮忙",
    "看看", "看下", "查查", "查一下", "试试", "试一下",
    "好的", "好啊", "嗯嗯", "对的", "明白", "了解", "收到", "懂了",
    "继续", "动手", "执行", "下一步", "完成", "搞定",
    # 高频对话填充 2-grams (出自实际 bug case "我想想，有没有写过关于...")
    "想想", "看看", "试试", "找找", "查查",
    "有没", "没有", "有写", "写过", "做过", "干过",
    "关于", "这个", "那个", "其他", "另外",
    "可能", "或许", "也许", "大概", "差不多",
    "我想", "你想", "想要", "要不", "要不要",
    "之类", "等等", "什么样",
}
_STOP_EN = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can",
    "i", "you", "he", "she", "it", "we", "they", "me", "us", "them",
    "my", "your", "his", "her", "its", "our", "their",
    "this", "that", "these", "those",
    "what", "how", "why", "when", "where", "who", "which",
    "and", "or", "but", "if", "then", "else", "so", "as",
    "of", "in", "on", "at", "to", "for", "with", "by", "from",
    "no", "yes", "ok", "okay", "yeah", "yep", "nope",
    "just", "very", "really", "still", "yet", "now", "also",
    "thanks", "thx", "pls", "please",
    "lol", "btw", "imo", "tbh",
}
_STOP = _STOP_CN | _STOP_EN


# CJK basic block U+4E00..U+9FFF — captures Chinese characters used in
# practice. Doesn't cover CJK Extension A/B (rare) or Japanese kana.
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_\-]{1,}|[0-9]{2,}|[一-鿿]+")


def _strip_channel_prefix(text: str) -> str:
    """Strip channel/team/system markers added by adapters before recall.

    Repeatedly applies all patterns until none match (in case of stacked
    prefixes like ``[from: 飞书]\\n[Teammate ...]``).
    """
    out = text
    changed = True
    safety = 8  # avoid pathological loops
    while changed and safety > 0:
        changed = False
        safety -= 1
        for pat in _PREFIX_PATTERNS:
            new = pat.sub("", out, count=1)
            if new != out:
                out = new
                changed = True
    return out.strip()


def _extract_keywords(text: str, max_keywords: int = 5) -> list[str]:
    """Extract up to ``max_keywords`` content-bearing tokens from ``text``.

    Two-tier priority — Latin tokens always outrank CJK bigrams. This is
    deliberate: in this codebase's typical queries ("有没有写过关于
    VLLM 和 sglang 的报告"), the user names the technical concept in
    Latin (VLLM, sglang, OpenClaw, GKE) and surrounds it with Chinese
    conversational filler. Without this priority the greedy left-to-right
    walk would fill the keyword budget with the filler bigrams before
    ever reaching the Latin tokens, and recall would surface noise.

    - Tier 1: Latin alphanumeric runs >= 2 chars, sorted by length DESC
      (so "OpenClaw" outranks "GKE" when both compete for a slot).
    - Tier 2: CJK 2-grams from each greedy CJK run, document order.
      Backfills only if Tier 1 didn't saturate the budget.

    Stopword filter applies to both tiers. 2-grams match how the FTS5
    trigram tokenizer indexes content, so they're guaranteed-matchable.
    """
    if not text:
        return []
    latin_tokens: list[str] = []
    cjk_bigrams: list[str] = []
    seen: set[str] = set()

    for match in _TOKEN_RE.finditer(text):
        tok = match.group(0)
        if "一" <= tok[0] <= "鿿":
            # CJK run → explode into 2-grams; collect into Tier 2.
            if len(tok) < 2:
                continue
            for i in range(len(tok) - 1):
                bigram = tok[i:i + 2]
                if bigram in _STOP or bigram in seen:
                    continue
                seen.add(bigram)
                cjk_bigrams.append(bigram)
        else:
            # Latin/digit run → collect into Tier 1.
            t = tok.lower()
            if len(tok) < 2 or t in _STOP or t in seen:
                continue
            seen.add(t)
            latin_tokens.append(tok)

    # Within Tier 1: longer first (more specific), tiebreak by lowercased
    # spelling for deterministic ordering across runs.
    latin_tokens.sort(key=lambda s: (-len(s), s.lower()))

    result = latin_tokens[:max_keywords]
    if len(result) < max_keywords:
        for bg in cjk_bigrams:
            if bg in result:
                continue
            result.append(bg)
            if len(result) >= max_keywords:
                break
    return result


def _fmt_row(r: dict, max_chars: int = 200) -> str:
    ts = r.get("ts") or 0
    role = r.get("role") or "?"
    text = (r.get("text") or "").replace("\n", " ").strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    when = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "??-?? ??:??"
    return f"- {when} [{role}] {text}"


def recall_history(
    bot_name: str,
    user_id: str | None,
    query: str,
    *,
    limit: int = 5,
    days: int = 60,
    max_total_chars: int = 1200,
) -> str:
    """Search the bot's local FTS5 index and return a formatted context block.

    Returns ``""`` if no keywords could be extracted, no hits, or any
    failure path — the caller treats empty string as "don't inject".

    The block format is human-readable Markdown-ish and labeled as a
    background context block so the LLM understands these are past
    conversations, not current input.
    """
    try:
        stripped = _strip_channel_prefix(query or "")
        keywords = _extract_keywords(stripped, max_keywords=5)
        if not keywords:
            return ""

        idx = SessionIndex(bot_name)
        all_hits: list[dict] = []
        for kw in keywords:
            try:
                rows = idx.search(
                    kw, days=days, user_id=user_id or None, limit=limit,
                )
            except Exception as e:
                log.debug("search(%r, user_id=%r) failed: %s", kw, user_id, e)
                rows = []
            # Backfilled rows have user_id="" (predate the user_id field).
            # If the user-filtered hit count is thin, append unfiltered
            # results so the user can still benefit from historical context.
            if user_id and len(rows) < 2:
                try:
                    extra = idx.search(kw, days=days, limit=limit)
                    rows.extend(extra)
                except Exception:
                    pass
            all_hits.extend(rows)

        if not all_hits:
            return ""

        # Dedup by row id; sort newest-first; cap at ``limit`` rows.
        seen_ids: set[int] = set()
        unique: list[dict] = []
        for r in sorted(all_hits, key=lambda x: -(x.get("ts") or 0)):
            rid = r.get("id")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            unique.append(r)
            if len(unique) >= limit:
                break

        if not unique:
            return ""

        # Format + char-budget truncation.
        header = f"[相关历史召回（基于关键词: {' / '.join(keywords)}）]"
        lines = [header]
        total = len(header)
        for r in unique:
            line = _fmt_row(r)
            if total + len(line) + 1 > max_total_chars:
                break
            lines.append(line)
            total += len(line) + 1
        if len(lines) == 1:  # only header, no rows fit
            return ""
        return "\n".join(lines)
    except Exception as e:
        log.debug("recall_history failed silently: %s", e)
        return ""


__all__ = ["recall_history"]
