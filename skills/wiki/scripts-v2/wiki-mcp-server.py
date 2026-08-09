#!/usr/bin/env python3
"""Wiki v2 MCP Server — 基于 Markdown 的知识 Wiki MCP tools。

直接读取 Markdown 文件 + [[wikilinks]]，不依赖 graph.json 或 search-chunks.json。

Tools: wiki_query, wiki_page, wiki_graph_neighbors, wiki_graph_path,
       wiki_status, wiki_search, wiki_list
"""

import json
import sys
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# 复用 v2 脚本
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wiki_utils import (WIKI_CONTENT, WIKI_URL, all_pages, parse_frontmatter,
                        extract_wikilinks, find_page_by_slug, page_url)
from query import query as do_query
from status import get_status

# ── 内存缓存：页面元数据 + wikilink 邻接表 ──
# 缓存 TTL 60 秒：MCP server 长期运行，新增/编辑页面后需要刷新

_cache = {}
_CACHE_TTL = 60  # 秒
_file_mtimes: dict[str, float] = {}  # path → last mtime


def _build_cache(force=False):
    """构建缓存。支持增量更新：只重建 mtime 变化的页面。"""
    now = time.time()

    # 首次构建或 force
    if force or "pages" not in _cache:
        pages_meta = {}
        adj = defaultdict(set)
        mtimes = {}

        for p in all_pages():
            slug = p.stem
            meta, body = parse_frontmatter(p)
            pages_meta[slug] = {
                "title": meta.get("title", slug),
                "type": meta.get("type", "unknown"),
                "tags": [str(t) for t in (meta.get("tags") or [])],
                "path": str(p.relative_to(WIKI_CONTENT)),
                "url": page_url(p),
            }
            for link_slug in extract_wikilinks(body):
                adj[slug].add(link_slug)
                adj[link_slug].add(slug)
            mtimes[str(p)] = p.stat().st_mtime

        _cache["pages"] = pages_meta
        _cache["adj"] = adj
        _cache["_ts"] = now
        _file_mtimes.clear()
        _file_mtimes.update(mtimes)
        return

    # TTL 内不检查
    if now - _cache.get("_ts", 0) < _CACHE_TTL:
        return

    # 增量更新：检查 mtime 变化
    changed = False
    current_files = set()
    for p in all_pages():
        path_str = str(p)
        current_files.add(path_str)
        old_mtime = _file_mtimes.get(path_str, 0)
        new_mtime = p.stat().st_mtime

        if new_mtime != old_mtime:
            # 文件变化，重新解析
            slug = p.stem
            meta, body = parse_frontmatter(p)
            _cache["pages"][slug] = {
                "title": meta.get("title", slug),
                "type": meta.get("type", "unknown"),
                "tags": [str(t) for t in (meta.get("tags") or [])],
                "path": str(p.relative_to(WIKI_CONTENT)),
                "url": page_url(p),
            }
            # 重建该页的邻接关系
            old_neighbors = set()
            for nb_set in _cache["adj"].values():
                if slug in nb_set:
                    old_neighbors.add(slug)
            # 清除旧的出边
            if slug in _cache["adj"]:
                for nb in list(_cache["adj"][slug]):
                    _cache["adj"][nb].discard(slug)
                _cache["adj"][slug].clear()
            # 添加新的
            for link_slug in extract_wikilinks(body):
                _cache["adj"][slug].add(link_slug)
                _cache["adj"][link_slug].add(slug)

            _file_mtimes[path_str] = new_mtime
            changed = True

    # 检查删除的文件
    deleted = set(_file_mtimes.keys()) - current_files
    for path_str in deleted:
        slug = Path(path_str).stem
        _cache["pages"].pop(slug, None)
        if slug in _cache["adj"]:
            for nb in _cache["adj"][slug]:
                _cache["adj"][nb].discard(slug)
            del _cache["adj"][slug]
        del _file_mtimes[path_str]
        changed = True

    _cache["_ts"] = now


def get_pages():
    _build_cache()
    return _cache["pages"]


def get_adj():
    _build_cache()
    return _cache["adj"]


# ── MCP Server ──

mcp = FastMCP(
    "CC Wiki v2",
    instructions="Personal knowledge wiki (Quartz/Markdown) with 180+ pages on AI/ML infrastructure, TPU/GPU training, and related topics.",
)

import logging
_logger = logging.getLogger("wiki-mcp")


def _safe_tool(func):
    """Decorator: catch exceptions and return error JSON instead of crashing."""
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            _logger.error(f"Tool {func.__name__} error: {e}", exc_info=True)
            return json.dumps({"error": f"{func.__name__} failed: {str(e)}"})
    return wrapper


@mcp.tool()
@_safe_tool
def wiki_query(question: str, top_k: int = 5) -> str:
    """Search the wiki for relevant pages.

    Returns matched pages with scores, snippets, URLs, descriptions, and related pages.
    Use for any knowledge question about AI infrastructure, TPU, GPU, training, etc.
    """
    if not question or not question.strip():
        return json.dumps({"error": "Empty query", "results": []})
    question = question.strip()[:500]  # 限制查询长度
    top_k = max(1, min(top_k, 20))  # 限制结果数

    t0 = time.time()
    results = do_query(question, top_k=top_k)
    query_time_ms = round((time.time() - t0) * 1000, 1)

    if not results:
        return json.dumps({"query": question, "results": [], "query_time_ms": query_time_ms,
                           "message": "No matching pages found."})

    # 增强每个结果：加 description + related_pages
    pages = get_pages()
    adj = get_adj()
    for r in results:
        slug = Path(r["path"]).stem
        page_info = pages.get(slug, {})

        # 加 description
        if slug in pages:
            meta_path = find_page_by_slug(slug)
            if meta_path:
                meta, _ = parse_frontmatter(meta_path)
                r["description"] = str(meta.get("description", ""))

        # 加 related_pages（1-hop wikilink 邻居，最多 5 个）
        neighbors = adj.get(slug, set())
        related = []
        for nb in sorted(neighbors):
            if nb in pages and len(related) < 5:
                related.append({"slug": nb, "title": pages[nb]["title"], "type": pages[nb]["type"]})
        r["related_pages"] = related

    return json.dumps({"query": question, "results": results, "query_time_ms": query_time_ms},
                      ensure_ascii=False)


@mcp.tool()
@_safe_tool
def wiki_page(slug: str) -> str:
    """Read a wiki page's full content by slug.

    Example slugs: tpu-v7, karpathy-llm-wiki-20260407, knowledge-compounding
    """
    path = find_page_by_slug(slug)
    if not path:
        return f"Page '{slug}' not found. Use wiki_search to find pages."

    meta, body = parse_frontmatter(path)
    header = f"Title: {meta.get('title', slug)}\n"
    header += f"Type: {meta.get('type', 'unknown')}\n"
    header += f"Tags: {', '.join(str(t) for t in (meta.get('tags') or []))}\n"
    header += f"URL: {page_url(path)}\n\n"

    return header + body


@mcp.tool()
@_safe_tool
def wiki_graph_neighbors(slug: str, depth: int = 1) -> str:
    """Get N-hop neighbors of a wiki page in the knowledge graph.

    Graph is built from [[wikilinks]] in Markdown files.
    """
    pages = get_pages()
    if slug not in pages:
        return f"Node '{slug}' not found"

    adj = get_adj()
    visited = {slug}
    frontier = {slug}
    layers = {}

    for d in range(1, depth + 1):
        next_frontier = set()
        for node in frontier:
            for nb in adj.get(node, set()):
                if nb not in visited:
                    visited.add(nb)
                    next_frontier.add(nb)
        layers[d] = sorted(next_frontier)
        frontier = next_frontier

    result = {"center": pages[slug]["title"], "slug": slug, "depth": depth, "layers": {}}
    for d, slugs in layers.items():
        result["layers"][str(d)] = [
            {"slug": s, "title": pages[s]["title"], "type": pages[s]["type"]}
            for s in slugs if s in pages
        ]

    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
@_safe_tool
def wiki_graph_path(source: str, target: str) -> str:
    """Find shortest path between two wiki pages via wikilinks.

    Uses BFS on the [[wikilink]] graph.
    """
    pages = get_pages()
    if source not in pages:
        return f"Source '{source}' not found"
    if target not in pages:
        return f"Target '{target}' not found"

    adj = get_adj()
    queue = deque([(source, [source])])
    visited = {source}

    while queue:
        current, path = queue.popleft()
        if current == target:
            return json.dumps({
                "path": [{"slug": s, "title": pages[s]["title"], "type": pages[s]["type"]}
                         for s in path if s in pages],
                "hops": len(path) - 1,
            }, ensure_ascii=False)
        for nb in adj.get(current, set()):
            if nb not in visited:
                visited.add(nb)
                queue.append((nb, path + [nb]))

    return json.dumps({"error": f"No path between '{source}' and '{target}'"})


@mcp.tool()
@_safe_tool
def wiki_status() -> str:
    """Get wiki statistics: page counts by type, top tags, recent changes, last build time."""
    return json.dumps(get_status(), ensure_ascii=False)


@mcp.tool()
@_safe_tool
def wiki_search(keyword: str) -> str:
    """Quick keyword search across page titles, slugs, and tags.

    Supports structured queries:
    - Simple: "tpu" — searches title, slug, tags
    - Field-specific: "type:source tag:tpu title:v7" — filter by fields
    - Regex: "/v[67]/" — regex match on title
    - Combined: "type:entity /karp/"
    """
    pages = get_pages()

    # 解析结构化查询
    type_filter = None
    tag_filters = []
    title_pattern = None
    plain_keywords = []

    parts = keyword.split()
    for part in parts:
        if part.startswith("type:"):
            type_filter = part[5:]
        elif part.startswith("tag:"):
            tag_filters.append(part[4:].lower())
        elif part.startswith("title:"):
            plain_keywords.append(part[6:].lower())
        elif part.startswith("/") and part.endswith("/") and len(part) > 2:
            try:
                title_pattern = re.compile(part[1:-1], re.IGNORECASE)
            except re.error:
                plain_keywords.append(part.lower())
        else:
            plain_keywords.append(part.lower())

    matches = []
    for slug, info in pages.items():
        # type 过滤
        if type_filter and info["type"] != type_filter:
            continue

        # tag 过滤
        page_tags_lower = [t.lower() for t in info["tags"]]
        if tag_filters and not all(tf in page_tags_lower for tf in tag_filters):
            continue

        # regex 匹配
        if title_pattern and not title_pattern.search(info["title"]):
            continue

        # 关键词匹配
        if plain_keywords:
            title_lower = info["title"].lower()
            slug_lower = slug.lower()
            tags_str = " ".join(page_tags_lower)
            matched = False
            match_type = "slug"
            for kw in plain_keywords:
                if kw in title_lower:
                    matched = True
                    match_type = "title"
                elif kw in tags_str:
                    matched = True
                    match_type = "tag"
                elif kw in slug_lower:
                    matched = True
            if not matched:
                continue
        else:
            match_type = "filter"

        matches.append({**info, "slug": slug, "match": match_type})

    matches.sort(key=lambda x: (0 if x["match"] == "title" else 1, x["title"]))
    return json.dumps({"keyword": keyword, "count": len(matches),
                       "results": matches[:20]}, ensure_ascii=False)


@mcp.tool()
@_safe_tool
def wiki_list(type: str = "", tag: str = "") -> str:
    """List wiki pages, optionally filtered by type and/or tag.

    Types: source, entity, concept, analysis
    """
    pages = get_pages()
    result = []

    for slug, info in pages.items():
        if type and info["type"] != type:
            continue
        if tag and tag.lower() not in [t.lower() for t in info["tags"]]:
            continue
        result.append({**info, "slug": slug})

    result.sort(key=lambda x: x["title"])
    return json.dumps({"filter": {"type": type or "all", "tag": tag or "all"},
                       "count": len(result), "pages": result}, ensure_ascii=False)


@mcp.tool()
@_safe_tool
def wiki_ask(question: str) -> str:
    """Answer a question using wiki knowledge (extractive RAG).

    Searches top-3 relevant pages and returns the most relevant paragraphs with sources.
    Use this for direct questions like "What is TPU v7 HBM capacity?" or "How to shard a model on TPU?"
    """
    t0 = time.time()
    results = do_query(question, top_k=3)
    if not results:
        return json.dumps({"question": question, "answer": "No relevant pages found.",
                           "sources": []})

    # 从 top-3 页面提取最相关段落
    from query import _tokenize, get_index
    terms = _tokenize(question)
    idx = get_index()
    paragraphs = []

    for r in results:
        slug = Path(r["path"]).stem
        page = idx.get_page(slug)
        if not page:
            continue

        body = page["body"]
        # 按段落分割（空行分隔）
        paras = re.split(r"\n\s*\n", body)
        for para in paras:
            para = para.strip()
            if len(para) < 30:  # 跳过太短的段落
                continue
            # 计算段落与查询的匹配度
            para_lower = para.lower()
            match_count = sum(1 for t in terms if t.lower() in para_lower)
            if match_count > 0:
                paragraphs.append({
                    "text": para[:500],  # 限制段落长度
                    "match_count": match_count,
                    "match_ratio": round(match_count / max(len(terms), 1), 2),
                    "source_title": r["title"],
                    "source_slug": slug,
                    "source_url": r["url"],
                })

    # 按匹配度排序，取 top-5 段落
    paragraphs.sort(key=lambda x: x["match_count"], reverse=True)
    top_paras = paragraphs[:5]

    # 去掉内部排序字段
    for p in top_paras:
        del p["match_count"]
        del p["match_ratio"]

    query_time_ms = round((time.time() - t0) * 1000, 1)
    return json.dumps({
        "question": question,
        "paragraphs": top_paras,
        "sources": [{"title": r["title"], "slug": Path(r["path"]).stem, "url": r["url"]}
                    for r in results],
        "query_time_ms": query_time_ms,
    }, ensure_ascii=False)


@mcp.tool()
@_safe_tool
def wiki_related(slug: str, top_k: int = 5) -> str:
    """Find pages related to a given page (by graph links + tag overlap + title similarity).

    Use to discover related knowledge: "what else should I read after this page?"
    """
    pages = get_pages()
    if slug not in pages:
        return f"Page '{slug}' not found. Use wiki_search to find pages."

    center = pages[slug]
    adj = get_adj()
    center_tags = set(t.lower() for t in center["tags"])

    candidates = []
    for other_slug, other_info in pages.items():
        if other_slug == slug:
            continue
        score = 0.0

        # 1. Wikilink 邻居：直接连接得 10 分
        if other_slug in adj.get(slug, set()):
            score += 10.0

        # 2. Tag 交集：每个共同 tag 得 3 分
        other_tags = set(t.lower() for t in other_info["tags"])
        common_tags = center_tags & other_tags
        score += len(common_tags) * 3.0

        # 3. 同类型得 1 分
        if other_info["type"] == center["type"]:
            score += 1.0

        if score > 0:
            candidates.append({
                "slug": other_slug,
                "title": other_info["title"],
                "type": other_info["type"],
                "tags": other_info["tags"],
                "url": other_info["url"],
                "relevance_score": round(score, 1),
                "shared_tags": sorted(common_tags),
                "is_linked": other_slug in adj.get(slug, set()),
            })

    candidates.sort(key=lambda x: x["relevance_score"], reverse=True)
    return json.dumps({
        "center": {"slug": slug, "title": center["title"]},
        "related": candidates[:top_k],
    }, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
