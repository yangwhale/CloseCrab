#!/usr/bin/env python3
"""Wiki v2 Query — 倒排索引 + LRU 缓存，支持 frontmatter 匹配。

用法:
  python3 query.py "TPU v7 性能" --top-k 5
  python3 query.py "knowledge compounding" --type concept
  python3 query.py "karpathy" --tag llm-wiki
"""

import argparse
import json
import math
import re
import sys
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Optional

import jieba
jieba.setLogLevel(jieba.logging.WARNING)  # 静默加载日志

sys.path.insert(0, str(Path(__file__).parent))
from wiki_utils import WIKI_CONTENT, all_pages, parse_frontmatter, page_url

# ── 同义词表 ──────────────────────────────────────────────────

_SYNONYMS_FILE = Path(__file__).parent / "synonyms.json"
_synonym_map: dict[str, list[str]] = {}  # term → [synonyms]


def _load_synonyms():
    """加载同义词表，构建双向映射。"""
    global _synonym_map
    if _synonym_map:
        return
    if not _SYNONYMS_FILE.exists():
        return
    data = json.loads(_SYNONYMS_FILE.read_text(encoding="utf-8"))
    # 构建双向映射：key → values, 每个 value → key + 其他 values
    for key, values in data.items():
        all_terms = [key.lower()] + [v.lower() for v in values]
        for term in all_terms:
            existing = _synonym_map.get(term, [])
            for other in all_terms:
                if other != term and other not in existing:
                    existing.append(other)
            _synonym_map[term] = existing


_load_synonyms()


# ── 摘要预生成 ────────────────────────────────────────────────

_SUMMARY_CLEAN_RE = re.compile(r"\[.*?\]\(.*?\)|!\[.*?\]\(.*?\)|#{1,6}\s*|^\s*[-*]\s*|"
                                r"\*\*|__|`|>\s*|\[\[.*?\|?(.*?)\]\]", re.MULTILINE)


def _generate_summary(body: str, meta: dict) -> str:
    """从正文生成 2-3 句摘要。优先用 description，否则取正文前几段。"""
    desc = meta.get("description", "")
    if desc and len(desc) > 20:
        return str(desc)

    # 清理 markdown 标记
    clean = _SUMMARY_CLEAN_RE.sub(r"\1", body)
    # 取前 300 字符的正文
    lines = [l.strip() for l in clean.split("\n") if l.strip() and len(l.strip()) > 10]
    if not lines:
        return ""

    summary_parts = []
    total = 0
    for line in lines:
        if total + len(line) > 300:
            break
        summary_parts.append(line)
        total += len(line)

    return " ".join(summary_parts)[:300]


# ── 倒排索引 ──────────────────────────────────────────────────

class WikiIndex:
    """预构建倒排索引，避免每次查询全量扫描。"""

    def __init__(self):
        self._pages: dict[str, dict] = {}          # slug → {meta, body, path, ...}
        self._title_idx: dict[str, set] = defaultdict(set)   # term → {slugs}
        self._tag_idx: dict[str, set] = defaultdict(set)     # term → {slugs}
        self._desc_idx: dict[str, set] = defaultdict(set)    # term → {slugs}
        self._body_idx: dict[str, set] = defaultdict(set)    # term → {slugs}
        self._body_counts: dict[str, dict] = defaultdict(dict)  # slug → {term: count}
        self._body_lengths: dict[str, int] = {}     # slug → body token count
        self._avg_body_len: float = 0.0             # 平均 body 长度（BM25 用）
        self._built_at: float = 0
        self._build_time_ms: float = 0

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def build_time_ms(self) -> float:
        return self._build_time_ms

    def build(self):
        """全量构建索引。"""
        t0 = time.perf_counter()
        self._pages.clear()
        self._title_idx.clear()
        self._tag_idx.clear()
        self._desc_idx.clear()
        self._body_idx.clear()
        self._body_counts.clear()
        self._body_lengths.clear()

        for filepath in all_pages():
            slug = filepath.stem
            meta, body = parse_frontmatter(filepath)
            if not meta:
                continue

            self._pages[slug] = {
                "meta": meta,
                "body": body,
                "path": filepath,
                "summary": _generate_summary(body, meta),
            }

            # 索引 title
            title = str(meta.get("title", "")).lower()
            for term in _tokenize(title):
                self._title_idx[term].add(slug)

            # 索引 tags
            tags = [str(t).lower() for t in (meta.get("tags") or [])]
            for tag in tags:
                for term in _tokenize(tag):
                    self._tag_idx[term].add(slug)

            # 索引 description
            desc = str(meta.get("description", "")).lower()
            for term in _tokenize(desc):
                self._desc_idx[term].add(slug)

            # 索引 body（统计词频）
            body_lower = body.lower()
            body_terms = _tokenize(body_lower)
            self._body_lengths[slug] = len(body_terms)
            term_counts: dict[str, int] = defaultdict(int)
            for term in body_terms:
                term_counts[term] += 1
            for term, count in term_counts.items():
                self._body_idx[term].add(slug)
                self._body_counts[slug][term] = count

        # BM25 平均文档长度
        if self._body_lengths:
            self._avg_body_len = sum(self._body_lengths.values()) / len(self._body_lengths)

        self._built_at = time.perf_counter()
        self._build_time_ms = (self._built_at - t0) * 1000

    def get_page(self, slug: str) -> Optional[dict]:
        return self._pages.get(slug)

    def candidate_slugs(self, terms: list[str]) -> set[str]:
        """返回至少匹配一个 term 的所有 slug。"""
        candidates = set()
        for term in terms:
            candidates |= self._title_idx.get(term, set())
            candidates |= self._tag_idx.get(term, set())
            candidates |= self._desc_idx.get(term, set())
            candidates |= self._body_idx.get(term, set())
        return candidates

    def term_in_title(self, slug: str, term: str) -> bool:
        return slug in self._title_idx.get(term, set())

    def term_in_tags(self, slug: str, term: str) -> bool:
        return slug in self._tag_idx.get(term, set())

    def term_in_desc(self, slug: str, term: str) -> bool:
        return slug in self._desc_idx.get(term, set())

    def body_term_count(self, slug: str, term: str) -> int:
        return self._body_counts.get(slug, {}).get(term, 0)

    def body_df(self, term: str) -> int:
        """文档频率：包含 term 的文档数。"""
        return len(self._body_idx.get(term, set()))

    def body_length(self, slug: str) -> int:
        return self._body_lengths.get(slug, 0)

    def page_degree(self, slug: str) -> int:
        """页面在 wikilink 图中的连接度（入边+出边）。"""
        # 需要从 MCP server 的 adj 获取，这里用 body 中 wikilinks 数量近似
        page = self._pages.get(slug)
        if not page:
            return 0
        from wiki_utils import extract_wikilinks
        return len(extract_wikilinks(page["body"]))

    def co_occurring_tags(self, tag: str, min_cooccurrence: int = 3) -> list[str]:
        """找与给定 tag 经常共现的其他 tags。"""
        # 找含有该 tag 的所有页面
        matching_slugs = self._tag_idx.get(tag, set())
        if not matching_slugs:
            return []

        # 统计共现 tags
        co_counts: dict[str, int] = defaultdict(int)
        for slug in matching_slugs:
            page = self._pages.get(slug)
            if not page:
                continue
            page_tags = [str(t).lower() for t in (page["meta"].get("tags") or [])]
            for pt in page_tags:
                if pt != tag:
                    co_counts[pt] += 1

        # 按共现频率排序，过滤低频
        result = [(t, c) for t, c in co_counts.items() if c >= min_cooccurrence]
        result.sort(key=lambda x: x[1], reverse=True)
        return [t for t, c in result]


# ── 全局索引实例 ──────────────────────────────────────────────

_index: Optional[WikiIndex] = None
_INDEX_TTL = 60  # 秒


def get_index() -> WikiIndex:
    """获取或构建索引（60s TTL 自动刷新）。"""
    global _index
    now = time.perf_counter()
    if _index is None or (now - _index._built_at) > _INDEX_TTL:
        _index = WikiIndex()
        _index.build()
        _query_cache.cache_clear()  # 索引更新后清空查询缓存
    return _index


# ── 分词 ──────────────────────────────────────────────────────

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_SPLIT_RE = re.compile(r"[\s,;:!?()（）【】「」《》·•—–]+")


def _tokenize(text: str) -> list[str]:
    """混合分词：CJK 文本用 jieba，其余用空格分割。"""
    if not text or not text.strip():
        return []

    # 检测是否包含 CJK 字符
    if _CJK_RE.search(text):
        # jieba 分词（精确模式）
        tokens = jieba.lcut(text)
        result = []
        for t in tokens:
            t = t.strip().lower()
            if len(t) >= 2 or _CJK_RE.match(t):  # CJK 单字也保留
                result.append(t)
        return result
    else:
        # 纯英文/数字：按分隔符分割
        terms = _SPLIT_RE.split(text.strip())
        return [t.lower() for t in terms if len(t) >= 2]


# ── 模糊匹配 + 同义词扩展 ─────────────────────────────────────

def _levenshtein(s1: str, s2: str) -> int:
    """计算编辑距离。"""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


def _expand_terms(idx: WikiIndex, terms: list[str]) -> list[str]:
    """扩展查询词：同义词 + tag 关联 + 模糊匹配。返回扩展后的词列表（含原词）。"""
    expanded = list(terms)
    all_index_terms = None  # lazy load

    for term in terms:
        t = term.lower()

        # 1. 同义词扩展
        syns = _synonym_map.get(t, [])
        for syn in syns:
            if syn not in expanded:
                expanded.append(syn)

        # 2. Tag 关联扩展：如果 term 匹配某个 tag，找共现的 tags
        co_tags = idx.co_occurring_tags(t, min_cooccurrence=3)
        for co_tag in co_tags[:3]:  # 最多扩展 3 个共现 tag
            if co_tag not in expanded:
                expanded.append(co_tag)

        # 3. 模糊匹配（仅当无精确匹配时 + 英文词 + 长度≥4）
        if len(t) >= 4 and not _CJK_RE.search(t):
            has_exact = bool(idx.candidate_slugs([t]))
            if not has_exact:
                if all_index_terms is None:
                    all_index_terms = set()
                    all_index_terms.update(idx._title_idx.keys())
                    all_index_terms.update(idx._tag_idx.keys())
                    all_index_terms.update(idx._body_idx.keys())

                max_dist = 1 if len(t) < 6 else 2
                for idx_term in all_index_terms:
                    if abs(len(idx_term) - len(t)) > max_dist:
                        continue
                    if _levenshtein(t, idx_term) <= max_dist:
                        if idx_term not in expanded:
                            expanded.append(idx_term)

    return expanded


# ── Snippet 提取 ──────────────────────────────────────────────

def _extract_snippets(body: str, terms: list[str], max_snippets: int = 3,
                      window: int = 80) -> list[str]:
    """提取多个不重叠的匹配片段，高亮匹配 term。"""
    body_lower = body.lower()
    # 收集所有匹配位置
    positions = []
    for term in terms:
        t = term.lower()
        start = 0
        while True:
            idx = body_lower.find(t, start)
            if idx < 0:
                break
            positions.append((idx, len(t), term))
            start = idx + 1

    if not positions:
        return []

    # 按位置排序，去重选不重叠的片段
    positions.sort(key=lambda x: x[0])
    snippets = []
    used_ranges = []

    for pos, tlen, term in positions:
        if len(snippets) >= max_snippets:
            break
        snip_start = max(0, pos - window)
        snip_end = min(len(body), pos + tlen + window)

        # 检查是否与已有片段重叠
        overlaps = False
        for us, ue in used_ranges:
            if snip_start < ue and snip_end > us:
                overlaps = True
                break
        if overlaps:
            continue

        used_ranges.append((snip_start, snip_end))
        snippet = body[snip_start:snip_end].replace("\n", " ").strip()

        # 高亮匹配 term（用 **bold**）
        for t in terms:
            pattern = re.compile(re.escape(t), re.IGNORECASE)
            snippet = pattern.sub(f"**{t}**", snippet)

        snippets.append(f"...{snippet}...")

    return snippets


# ── BM25 评分 ─────────────────────────────────────────────────

# BM25 参数
_BM25_K1 = 1.5
_BM25_B = 0.75

# Field 权重乘数
_W_TITLE = 10.0
_W_TAGS = 5.0
_W_DESC = 3.0
_W_BODY = 1.0


def _bm25_term_score(tf: int, df: int, dl: int, avgdl: float, N: int) -> float:
    """单个 term 的 BM25 分数。"""
    if df == 0 or N == 0:
        return 0.0
    idf = math.log((N - df + 0.5) / (df + 0.5) + 1.0)
    tf_norm = (tf * (_BM25_K1 + 1)) / (tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / max(avgdl, 1)))
    return idf * tf_norm


def score_page(idx: WikiIndex, slug: str, query_terms: list[str],
               type_filter: str = None, tag_filter: str = None) -> tuple[float, dict]:
    """对单个页面计算 BM25 相关性分数。"""
    page = idx.get_page(slug)
    if not page:
        return 0.0, {}

    meta = page["meta"]

    # 类型过滤
    if type_filter and meta.get("type") != type_filter:
        return 0.0, {}

    # 标签过滤
    if tag_filter:
        page_tags = [str(t).lower() for t in (meta.get("tags") or [])]
        if tag_filter.lower() not in page_tags:
            return 0.0, {}

    N = idx.page_count
    avgdl = idx._avg_body_len
    dl = idx.body_length(slug)

    score = 0.0
    matched_terms = []

    for term in query_terms:
        t = term.lower()
        term_score = 0.0

        # Title: 匹配即给高分
        if idx.term_in_title(slug, t):
            term_score += _W_TITLE
            matched_terms.append(term)
            # Title 完全匹配 bonus：entity/concept 页面的 title 包含全部 query terms → 额外 +15
            # (在外层循环后处理)

        # Tags: 同理
        if idx.term_in_tags(slug, t):
            term_score += _W_TAGS
            if term not in matched_terms:
                matched_terms.append(term)

        # Description: 同理
        if idx.term_in_desc(slug, t):
            term_score += _W_DESC
            if term not in matched_terms:
                matched_terms.append(term)

        # Body: BM25 评分
        body_tf = idx.body_term_count(slug, t)
        if body_tf > 0:
            body_df = idx.body_df(t)
            bm25 = _bm25_term_score(body_tf, body_df, dl, avgdl, N)
            term_score += bm25 * _W_BODY
            if term not in matched_terms:
                matched_terms.append(term)

        score += term_score

    if score == 0:
        return 0.0, {}

    # Entity/Concept 权威匹配 bonus
    title_lower = str(meta.get("title", "")).lower()
    page_type = meta.get("type", "")
    if page_type in ("entity", "concept"):
        # 如果所有 query terms 都在 title 中 → 这是"权威定义"页
        all_in_title = all(t.lower() in title_lower for t in query_terms if len(t) >= 2)
        if all_in_title:
            score += 25.0

    # 时间衰减：新页面加权，deprecated 降权
    date_str = meta.get("date")
    if date_str:
        try:
            from datetime import datetime, date as date_type
            if isinstance(date_str, date_type):
                page_date = date_str
            else:
                page_date = datetime.strptime(str(date_str), "%Y-%m-%d").date()
            days_ago = (datetime.now().date() - page_date).days
            # 半衰期 180 天（约 6 个月）
            freshness = 0.5 ** (days_ago / 180)
            # 混合：70% 原始分 + 30% 时间加权
            score = score * (0.7 + 0.3 * freshness)
        except (ValueError, TypeError):
            pass

    # deprecated 页面大幅降权
    if meta.get("deprecated"):
        score *= 0.1

    # 图谱连接度加权：hub 页面轻微加权（log scale，避免过大影响）
    degree = idx.page_degree(slug)
    if degree > 0:
        hub_bonus = 1.0 + 0.05 * math.log(1 + degree)  # e.g. 10 links → +12%, 50 links → +20%
        score *= hub_bonus

    # 提取多个匹配 snippet（最多 3 个不重叠片段）
    body = page["body"]
    snippets = _extract_snippets(body, query_terms, max_snippets=3, window=80)

    filepath = page["path"]
    info = {
        "path": str(filepath),
        "title": meta.get("title", filepath.stem),
        "type": meta.get("type", "unknown"),
        "tags": meta.get("tags", []),
        "url": page_url(filepath),
        "score": round(score, 2),
        "matched_terms": matched_terms,
        "summary": page.get("summary", ""),
        "context": snippets[0] if snippets else "",
        "snippets": snippets,
    }
    return score, info


# ── 查询（带 LRU 缓存）────────────────────────────────────────

# ── 查询意图分类 ──────────────────────────────────────────────

def _classify_intent(query_str: str, idx: WikiIndex) -> str:
    """识别查询意图：exact_slug | exact_title | search。"""
    q = query_str.strip()

    # 1. 精确 slug 查找（如 "tpu-v7", "karpathy-llm-wiki-20260407"）
    if re.match(r"^[a-z0-9][a-z0-9\-]*$", q) and idx.get_page(q):
        return "exact_slug"

    # 2. 精确标题匹配
    q_lower = q.lower()
    for slug, page in idx._pages.items():
        if page["meta"].get("title", "").lower() == q_lower:
            return "exact_title"

    return "search"


@lru_cache(maxsize=128)
def _query_cache(query_key: str, top_k: int, type_filter: str,
                 tag_filter: str) -> tuple:
    """缓存层 — 将 list 转为 tuple 以支持 lru_cache。"""
    idx = get_index()

    # 意图分类：精确查找 shortcut
    intent = _classify_intent(query_key, idx)

    if intent == "exact_slug":
        page = idx.get_page(query_key.strip())
        if page:
            filepath = page["path"]
            meta = page["meta"]
            info = {
                "path": str(filepath),
                "title": meta.get("title", filepath.stem),
                "type": meta.get("type", "unknown"),
                "tags": meta.get("tags", []),
                "url": page_url(filepath),
                "score": 100.0,
                "matched_terms": [query_key.strip()],
                "context": "",
                "snippets": [],
                "intent": "exact_slug",
            }
            return (info,)

    if intent == "exact_title":
        q_lower = query_key.strip().lower()
        for slug, page in idx._pages.items():
            if page["meta"].get("title", "").lower() == q_lower:
                filepath = page["path"]
                meta = page["meta"]
                info = {
                    "path": str(filepath),
                    "title": meta.get("title", filepath.stem),
                    "type": meta.get("type", "unknown"),
                    "tags": meta.get("tags", []),
                    "url": page_url(filepath),
                    "score": 100.0,
                    "matched_terms": [query_key.strip()],
                    "context": "",
                    "snippets": [],
                    "intent": "exact_title",
                }
                return (info,)

    # 普通搜索
    terms = _tokenize(query_key)
    if not terms:
        return ()

    # 同义词 + 模糊匹配扩展
    expanded = _expand_terms(idx, terms)

    # 用倒排索引快速找候选，避免全扫描
    candidates = idx.candidate_slugs(expanded)

    results = []
    for slug in candidates:
        score, info = score_page(idx, slug, expanded, type_filter, tag_filter)
        if score > 0:
            results.append(info)

    results.sort(key=lambda x: x["score"], reverse=True)
    return tuple(results[:top_k])


def query(query_str: str, top_k: int = 5, type_filter: str = None,
          tag_filter: str = None) -> list[dict]:
    """搜索 Wiki 页面，返回排序后的结果列表。"""
    if not query_str or not query_str.strip():
        return []
    # 限制输入长度，防止超长查询
    query_str = query_str.strip()[:500]
    top_k = max(1, min(top_k, 50))
    result = _query_cache(query_str, top_k, type_filter or "", tag_filter or "")
    return list(result)


def main():
    parser = argparse.ArgumentParser(description="Wiki v2 Query")
    parser.add_argument("query", help="搜索关键词")
    parser.add_argument("--top-k", type=int, default=5, help="返回结果数（默认 5）")
    parser.add_argument("--type", default=None, help="按类型过滤（source/entity/concept/analysis）")
    parser.add_argument("--tag", default=None, help="按标签过滤")
    parser.add_argument("--format", default="text", choices=["text", "json"],
                        help="输出格式")

    args = parser.parse_args()
    results = query(args.query, args.top_k, args.type, args.tag)

    if not results:
        print("未找到匹配页面。")
        return

    if args.format == "json":
        import json
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for i, r in enumerate(results, 1):
            tags_str = ", ".join(r["tags"][:5]) if r["tags"] else ""
            print(f"\n{i}. [{r['type']}] {r['title']}  (score: {r['score']})")
            print(f"   路径: {r['path']}")
            print(f"   URL: {r['url']}")
            if tags_str:
                print(f"   标签: {tags_str}")
            if r["context"]:
                print(f"   上下文: ...{r['context']}...")


if __name__ == "__main__":
    main()
