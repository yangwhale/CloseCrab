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
import math
import re
import time
from datetime import datetime

from closecrab.utils.session_search import SessionIndex

log = logging.getLogger("closecrab.session_recall")


def _score(row: dict, now_ts: int) -> float:
    """log(len) × recency_decay × info_density. Half-life ≈ 14 days.

    Three factors:
      * length — log10 clamped to [20, 2000] chars; longer = more substance
      * recency — 0.5 ** (age_days / 14); 2-week half-life
      * info_density — LLM-rated 0..1 (NULL → neutral 0.5; floor at 0.3 so
        a misjudged-low row doesn't completely vanish from recall)

    The density floor matters because the haiku judge is probabilistic; a
    real fact accidentally scored 0.1 should still be recallable if its
    length + recency are strong.
    """
    text = row.get("text") or ""
    length = max(20, min(len(text), 2000))
    ts = row.get("ts") or 0
    age_days = max(0.0, (now_ts - ts) / 86400.0)
    density = row.get("info_density")
    if density is None:
        density_factor = 0.5
    else:
        try:
            density_factor = max(0.3, min(1.0, float(density)))
        except (TypeError, ValueError):
            density_factor = 0.5
    return math.log10(length) * (0.5 ** (age_days / 14.0)) * density_factor


# Channel adapters prefix user text with provenance markers like
# "[from: 飞书私聊]\n" before BotCore sees it. Strip these before keyword
# extraction so words like "飞书" don't dominate recall.
_PREFIX_PATTERNS = [
    re.compile(r"^\s*\[channel:[^\]]+\]\s*\n?", re.IGNORECASE),
    re.compile(r"^\s*\[from:[^\]]+\]\s*\n?", re.IGNORECASE),
    re.compile(r"^\s*\[Teammate [^\]]+\]\s*\n?"),
    re.compile(r"^\s*\[System:[^\]]+\]\s*\n?"),
    re.compile(r"^\s*\[相关历史召回[^\]]*\][\s\S]*?\n---\n\n?"),
]

# Noise patterns scrubbed from the message body before tokenization. Pasted
# URLs / file paths / UUIDs / long hashes / opaque IDs become garbage
# keywords otherwise (e.g. "kubernetes/nodepool", "ee5e57e67g..." leak into
# recall). Order matters: long-id last so it doesn't eat file extensions.
_NOISE_PATTERNS = [
    re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE),       # URLs
    re.compile(r"\[Attached file:[^\]]*\]", re.IGNORECASE),    # attach markers
    re.compile(r"!?\[[^\]]*\]\([^)]*\)"),                      # md links/images
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{8,}\b"),        # UUIDs
    re.compile(r"\b[0-9a-fA-F]{12,}\b"),                       # long hex hashes
    re.compile(                                                # filenames
        r"\b[\w\-./]+\.(?:png|jpe?g|gif|webp|pdf|pb|json|log|txt|sh|py|md)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b[A-Za-z0-9][A-Za-z0-9_\-]{19,}\b"),         # ≥20 char IDs
]


def _clean_noise(text: str) -> str:
    out = text
    for pat in _NOISE_PATTERNS:
        out = pat.sub(" ", out)
    return out

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

# Colloquial fillers that jieba's POS tagger sometimes mislabels as n/v and
# would otherwise leak as high-frequency noise keywords. These are real words
# (so they survive POS filtering) but carry no retrieval signal in this repo's
# conversational queries.
_EXTRA_STOP = {
    "什么", "怎么", "怎样", "这样", "那样", "这个", "那个", "这些", "那些", "东西",
    "时候", "知道", "觉得", "可以", "应该", "需要", "可能", "就是", "还是", "或者",
    "现在", "以后", "以前", "之前", "之后", "到底", "其实", "然后", "但是", "因为",
    "如果", "所以", "这么", "那么", "一下", "一点", "一些", "有点", "比较", "非常",
    "我们", "你们", "他们", "咱们", "自己", "别人", "大家", "事情", "问题",
    "看看", "试试", "搞搞", "弄弄", "帮忙", "一直", "已经", "曾经", "刚刚",
    "好多", "好几", "几百", "没什么", "毛病",
    "牛逼", "厉害", "继续", "接着", "开干", "干吧", "好的", "行吧",
    "比如说", "加进去", "保持一致",
    # A-不-A / 有没有 型疑问填充（jieba 整词切，bigram 停用词拦不住）
    "有没有", "是不是", "能不能", "可不可以", "要不要", "行不行",
    "好不好", "对不对", "是否", "能否", "可否",
}

# CJK basic block U+4E00..U+9FFF — captures Chinese characters used in
# practice. Doesn't cover CJK Extension A/B (rare) or Japanese kana.
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_\-]{1,}|[0-9]{2,}|[一-鿿]+")
_HAS_LATIN = re.compile(r"[a-zA-Z]")
_HAS_CJK = re.compile(r"[一-鿿]")

# POS tags kept from jieba word segmentation: noun-family + verb-family +
# English + proper-noun + abbreviation/idiom + locative. Everything else
# (pronouns, particles, conjunctions, adverbs, etc.) is dropped.
_KEEP_POS = {
    "n", "nr", "ns", "nt", "nz", "nl", "ng", "nrt", "nrfg",  # nouns / proper
    "v", "vn", "vd", "vg",                                    # verbs
    "eng",                                                    # English
    "j", "l", "i",                                            # abbrev/idiom
    "s",                                                      # locative
}

# Lazy, guarded jieba loader. jieba is an optional dependency: if it's not
# installed the picker falls back to the mechanical bigram extractor so recall
# still works (just lower quality). Loaded once per process; the first call
# pays a ~0.5s dict-load cost, cached thereafter.
_jieba_pseg = None       # the posseg module, or None if unavailable
_jieba_tried = False


def _get_jieba_pseg():
    global _jieba_pseg, _jieba_tried
    if _jieba_tried:
        return _jieba_pseg
    _jieba_tried = True
    try:
        import jieba
        import jieba.posseg as pseg
        jieba.setLogLevel(logging.WARNING)
        _jieba_pseg = pseg
    except Exception as e:  # ImportError or any init failure → graceful fallback
        log.debug("jieba unavailable, recall falls back to bigram picker: %s", e)
        _jieba_pseg = None
    return _jieba_pseg


def _pick_keywords(text: str, max_keywords: int = 6) -> list[str]:
    """jieba POS-tagging keyword picker with graceful bigram fallback.

    Strategy (when jieba available):
      * Segment + POS-tag the noise-cleaned text.
      * Latin/digit runs → kept unconditionally (technical terms: VLLM, GKE).
      * CJK words → kept only if POS ∈ _KEEP_POS (drops pronouns/particles).
      * Drop tokens < 2 chars, stopwords, and _EXTRA_STOP fillers.
      * Sort by (is_latin, length, freq) all DESC — technical terms first,
        longer/more-specific words before short ones, frequent before rare.

    Falls back to :func:`_extract_keywords` (mechanical bigrams) if jieba is
    not installed, so recall degrades gracefully rather than breaking.
    """
    if not text:
        return []
    pseg = _get_jieba_pseg()
    if pseg is None:
        return _extract_keywords(text, max_keywords=max_keywords)

    words = list(pseg.cut(text))
    freq: dict[str, int] = {}
    for w in words:
        freq[w.word] = freq.get(w.word, 0) + 1
    cand: dict[str, int] = {}
    for w in words:
        tok = w.word.strip()
        if not tok or len(tok) < 2:
            continue
        tl = tok.lower()
        if tl in _STOP or tok in _STOP or tok in _EXTRA_STOP:
            continue
        is_latin = bool(_HAS_LATIN.search(tok)) and not _HAS_CJK.search(tok)
        if is_latin:
            cand[tok] = freq[tok]
        elif w.flag in _KEEP_POS and _HAS_CJK.search(tok):
            cand[tok] = freq[tok]

    def keyf(t: str):
        lat = 1 if _HAS_LATIN.search(t) and not _HAS_CJK.search(t) else 0
        return (lat, len(t), cand[t])

    return sorted(cand, key=keyf, reverse=True)[:max_keywords]


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
    exclude_ids: set[int] | None = None,
) -> str:
    """Search the bot's local FTS5 index and return a formatted context block.

    Returns ``""`` if no keywords could be extracted, no hits, or any
    failure path — the caller treats empty string as "don't inject".

    The block format is human-readable Markdown-ish and labeled as a
    background context block so the LLM understands these are past
    conversations, not current input.

    Cross-turn dedup: ``exclude_ids`` is a caller-owned set of row ids that
    have already been injected earlier in the same conversation. Rows in this
    set are filtered out of candidates, and the ids of the rows finally chosen
    are added back into it (mutated in place). This guarantees a given history
    row is injected at most once per conversation — later turns surface only
    content not yet seen by the model. The caller (BotCore) resets the set when
    the session ends/switches so a new conversation recalls from scratch.
    """
    try:
        stripped = _clean_noise(_strip_channel_prefix(query or ""))
        keywords = _pick_keywords(stripped, max_keywords=6)
        if not keywords:
            return ""

        idx = SessionIndex(bot_name)
        # Track how many distinct keywords each row matched. Relevance signal:
        # a row hit by 3 of the query's keywords is far more on-topic than one
        # hit by a single common word.
        match_cnt: dict[int, int] = {}
        row_by_id: dict[int, dict] = {}
        # Pull 4x candidates so the score-based picker has room to filter.
        per_kw_limit = limit * 4
        for kw in keywords:
            try:
                rows = idx.search(
                    kw, days=days, user_id=user_id or None, limit=per_kw_limit,
                )
            except Exception as e:
                log.debug("search(%r, user_id=%r) failed: %s", kw, user_id, e)
                rows = []
            # Backfilled rows have user_id="" (predate the user_id field).
            # If the user-filtered hit count is thin, append unfiltered
            # results so the user can still benefit from historical context.
            if user_id and len(rows) < 2:
                try:
                    extra = idx.search(kw, days=days, limit=per_kw_limit)
                    rows.extend(extra)
                except Exception:
                    pass
            seen_this_kw: set[int] = set()
            for r in rows:
                rid = r.get("id")
                # Cross-turn dedup: never reconsider a row already injected
                # earlier in this conversation.
                if exclude_ids is not None and rid in exclude_ids:
                    continue
                if rid in seen_this_kw:   # one keyword counts once per row
                    continue
                seen_this_kw.add(rid)
                match_cnt[rid] = match_cnt.get(rid, 0) + 1
                row_by_id[rid] = r

        if not row_by_id:
            return ""

        # Rank by relevance-weighted score: match_count² × (log(len) ×
        # recency × density). The match_count² term makes keyword relevance
        # dominate — a row matching 3 keywords beats a longer/newer row that
        # only matched 1. Without this, one long recent message would hijack
        # the top slot of every unrelated query (the deepest pre-fix defect).
        now_ts = int(time.time())
        scored = [
            ((match_cnt[rid] ** 2) * _score(r, now_ts), r)
            for rid, r in row_by_id.items()
        ]
        scored.sort(key=lambda x: -x[0])
        unique = [r for _, r in scored[:limit]]
        # Final display order: newest-first, so the LLM reads chronologically.
        unique.sort(key=lambda r: -(r.get("ts") or 0))

        if not unique:
            return ""

        # Format + char-budget truncation. Only rows that actually fit the
        # budget count as "injected" — a row truncated out here was never
        # shown to the model, so it must stay eligible for a later turn.
        header = f"[相关历史召回（基于关键词: {' / '.join(keywords)}）]"
        lines = [header]
        total = len(header)
        for r in unique:
            line = _fmt_row(r)
            if total + len(line) + 1 > max_total_chars:
                break
            lines.append(line)
            total += len(line) + 1
            if exclude_ids is not None:
                rid = r.get("id")
                if rid is not None:
                    exclude_ids.add(rid)
        if len(lines) == 1:  # only header, no rows fit
            return ""
        return "\n".join(lines)
    except Exception as e:
        log.debug("recall_history failed silently: %s", e)
        return ""


__all__ = ["recall_history"]
