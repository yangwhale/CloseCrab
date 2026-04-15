#!/usr/bin/env python3
"""wiki-mcp-server.py — MCP server exposing Wiki knowledge tools.

Provides multi-Bot access to the CC Wiki via Claude Code MCP protocol.
Runs as stdio server, spawned per-bot via ~/.claude.json config.

Tools exposed:
  - wiki_query: BM25 + graph-augmented search
  - wiki_page: Read a page's plain text
  - wiki_graph_neighbors: N-hop neighbors
  - wiki_graph_path: BFS shortest path
  - wiki_status: Statistics
  - wiki_search: Keyword search (simple grep)
  - wiki_list: List pages by type/tag
"""
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict, deque

from mcp.server.fastmcp import FastMCP

# Add scripts dir to path for wiki_utils
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wiki_utils import WIKI_REPO, TextExtractor

DATA_DIR = WIKI_REPO / "wiki-data"
WIKI_DIR = WIKI_REPO / "wiki"
WIKI_URL = os.environ.get("CC_PAGES_URL_PREFIX", "") + "/wiki"

# ── Data loading (cached in memory) ──

_cache = {}


def get_graph():
    if "graph" not in _cache:
        path = DATA_DIR / "graph.json"
        if path.exists():
            _cache["graph"] = json.loads(path.read_text(encoding="utf-8"))
        else:
            _cache["graph"] = {"nodes": [], "links": [], "meta": {}}
    return _cache["graph"]


def get_search_index():
    if "search" not in _cache:
        path = DATA_DIR / "search-chunks.json"
        if path.exists():
            _cache["search"] = json.loads(path.read_text(encoding="utf-8"))
        else:
            _cache["search"] = {"chunks": []}
    return _cache["search"]


def get_node_map():
    if "node_map" not in _cache:
        _cache["node_map"] = {n["id"]: n for n in get_graph().get("nodes", [])}
    return _cache["node_map"]


def get_adjacency():
    if "adj" not in _cache:
        adj = defaultdict(set)
        for link in get_graph().get("links", []):
            src = link["source"] if isinstance(link["source"], str) else link["source"].get("id", "")
            tgt = link["target"] if isinstance(link["target"], str) else link["target"].get("id", "")
            adj[src].add(tgt)
            adj[tgt].add(src)
        _cache["adj"] = adj
    return _cache["adj"]


# ── BM25 (inline, same as wiki-query.py) ──


def tokenize(text):
    text = text.lower()
    tokens = re.findall(r'[a-z][a-z0-9_-]*[a-z0-9]|[a-z]', text)
    chinese = re.findall(r'[\u4e00-\u9fff]+', text)
    for segment in chinese:
        tokens.extend(list(segment))
        for i in range(len(segment) - 1):
            tokens.append(segment[i:i + 2])
    return tokens


class BM25:
    def __init__(self, corpus, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.corpus = corpus
        self.doc_count = len(corpus)
        self.avgdl = sum(len(toks) for _, toks in corpus) / max(self.doc_count, 1)
        self.df = Counter()
        self.doc_tf = {}
        self.doc_len = {}
        for doc_id, tokens in corpus:
            tf = Counter(tokens)
            self.doc_tf[doc_id] = tf
            self.doc_len[doc_id] = len(tokens)
            for term in set(tokens):
                self.df[term] += 1

    def score(self, query_tokens, doc_id):
        tf = self.doc_tf.get(doc_id, {})
        dl = self.doc_len.get(doc_id, 0)
        s = 0.0
        for term in query_tokens:
            f = tf.get(term, 0)
            if f == 0:
                continue
            idf = math.log((self.doc_count - self.df.get(term, 0) + 0.5) / (self.df.get(term, 0) + 0.5) + 1)
            s += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s

    def search(self, query_tokens, top_k=10):
        scores = [(doc_id, self.score(query_tokens, doc_id)) for doc_id, _ in self.corpus]
        scores = [(d, s) for d, s in scores if s > 0]
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]


# ── MCP Server ──

mcp = FastMCP("CC Wiki", instructions="Personal knowledge wiki with 160+ pages on AI/ML infrastructure, TPU/GPU training, and related topics.")


@mcp.tool()
def wiki_query(question: str, top_k: int = 5) -> str:
    """Search the wiki using BM25 + knowledge graph augmentation.

    Returns relevant pages with matched text snippets.
    Use this for any knowledge question about AI infrastructure, TPU, GPU, training, etc.
    """
    index = get_search_index()
    chunks = index.get("chunks", [])

    if not chunks:
        return json.dumps({"error": "Search index empty. Run build-search-index.py"})

    corpus = []
    chunk_map = {}
    for chunk in chunks:
        cid = chunk["id"]
        combined = f"{chunk['page_title']} {' '.join(chunk['tags'])} {chunk['text']}"
        corpus.append((cid, tokenize(combined)))
        chunk_map[cid] = chunk

    bm25 = BM25(corpus)
    query_tokens = tokenize(question)
    chunk_results = bm25.search(query_tokens, top_k=top_k * 3)

    # Aggregate by page
    page_scores = defaultdict(float)
    page_chunks = defaultdict(list)
    page_info = {}
    for cid, score in chunk_results:
        c = chunk_map.get(cid)
        if not c:
            continue
        pid = c["page_id"]
        page_scores[pid] += score
        page_chunks[pid].append(c["text"][:300])
        if pid not in page_info:
            page_info[pid] = {"title": c["page_title"], "type": c["page_type"],
                              "path": c["path"], "tags": c["tags"]}

    # Graph augmentation
    adj = get_adjacency()
    top_pages = sorted(page_scores.items(), key=lambda x: -x[1])[:3]
    for pid, _ in top_pages:
        for nb in adj.get(pid, set()):
            if nb not in page_scores:
                page_scores[nb] = 0.1
                nm = get_node_map()
                if nb in nm:
                    n = nm[nb]
                    page_info[nb] = {"title": n["title"], "type": n["type"],
                                     "path": n["path"], "tags": n.get("tags", [])}

    results = []
    for pid, score in sorted(page_scores.items(), key=lambda x: -x[1])[:top_k]:
        info = page_info.get(pid, {})
        results.append({
            "page_id": pid,
            "title": info.get("title", pid),
            "type": info.get("type", ""),
            "url": f"{WIKI_URL}/{info.get('path', '')}",
            "tags": info.get("tags", []),
            "score": round(score, 3),
            "snippets": page_chunks.get(pid, [])[:2],
        })

    return json.dumps({"query": question, "results": results}, ensure_ascii=False)


@mcp.tool()
def wiki_page(slug: str) -> str:
    """Read a wiki page's plain text content by its slug (filename without .html).

    Example slugs: tpu-v7, fsdp, knowledge-compounding
    """
    nm = get_node_map()
    if slug not in nm:
        return f"Page '{slug}' not found. Use wiki_list to see available pages."

    node = nm[slug]
    path = WIKI_DIR / node["path"]
    if not path.exists():
        return f"File not found: {node['path']}"

    content = path.read_text(encoding="utf-8")
    extractor = TextExtractor()
    extractor.feed(content)
    text = extractor.get_text()

    meta = f"Title: {node['title']}\nType: {node['type']}\nTags: {', '.join(node.get('tags', []))}\n"
    meta += f"URL: {WIKI_URL}/{node['path']}\n\n"

    return meta + text


@mcp.tool()
def wiki_graph_neighbors(slug: str, depth: int = 1) -> str:
    """Get N-hop neighbors of a wiki page in the knowledge graph.

    Returns connected pages up to the specified depth.
    """
    nm = get_node_map()
    if slug not in nm:
        return f"Node '{slug}' not found"

    adj = get_adjacency()
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
        layers[d] = list(next_frontier)
        frontier = next_frontier

    result = {"center": nm[slug]["title"], "depth": depth, "layers": {}}
    for d, nids in layers.items():
        result["layers"][str(d)] = [
            {"id": nid, "title": nm[nid]["title"], "type": nm[nid]["type"]}
            for nid in sorted(nids) if nid in nm
        ]

    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def wiki_graph_path(source: str, target: str) -> str:
    """Find the shortest path between two wiki pages in the knowledge graph.

    Uses BFS. Returns the path as a list of nodes.
    """
    nm = get_node_map()
    if source not in nm:
        return f"Source '{source}' not found"
    if target not in nm:
        return f"Target '{target}' not found"

    adj = get_adjacency()
    queue = deque([(source, [source])])
    visited = {source}

    while queue:
        current, path = queue.popleft()
        if current == target:
            return json.dumps({
                "path": [{"id": nid, "title": nm[nid]["title"], "type": nm[nid]["type"]}
                          for nid in path],
                "hops": len(path) - 1,
            }, ensure_ascii=False)
        for nb in adj.get(current, set()):
            if nb not in visited:
                visited.add(nb)
                queue.append((nb, path + [nb]))

    return json.dumps({"error": f"No path between '{source}' and '{target}'"})


@mcp.tool()
def wiki_status() -> str:
    """Get wiki statistics: page counts, link counts, health score, recent activity."""
    graph = get_graph()
    nodes = graph.get("nodes", [])
    links = graph.get("links", [])

    type_counts = defaultdict(int)
    for n in nodes:
        type_counts[n["type"]] += 1

    # Health score (simplified)
    inbound = defaultdict(int)
    for link in links:
        inbound[link["target"]] += 1
    orphans = sum(1 for n in nodes if inbound[n["id"]] == 0)

    score = max(0, min(100, round(100 - orphans * 0.5 + (2 if len(type_counts) >= 4 else 0))))

    result = {
        "pages": len(nodes),
        "links": len(links),
        "health_score": score,
        "types": dict(type_counts),
        "orphans": orphans,
        "url": f"{WIKI_URL}/index.html",
        "health_url": f"{WIKI_URL}/health.html",
    }

    # Recent log
    log_path = DATA_DIR / "log.json"
    if log_path.exists():
        log = json.loads(log_path.read_text(encoding="utf-8"))
        entries = log.get("entries", []) if isinstance(log, dict) else log
        result["recent"] = entries[-5:]
        result["total_operations"] = len(entries)

    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def wiki_search(keyword: str) -> str:
    """Simple keyword search across wiki page titles and tags.

    Faster than wiki_query for simple lookups. Returns matching pages.
    """
    nm = get_node_map()
    kw = keyword.lower()

    matches = []
    for nid, n in nm.items():
        title_match = kw in n["title"].lower()
        tag_match = any(kw in t.lower() for t in n.get("tags", []))
        id_match = kw in nid.lower()
        if title_match or tag_match or id_match:
            matches.append({
                "id": nid,
                "title": n["title"],
                "type": n["type"],
                "tags": n.get("tags", []),
                "url": f"{WIKI_URL}/{n['path']}",
                "match": "title" if title_match else ("tag" if tag_match else "id"),
            })

    matches.sort(key=lambda x: (0 if x["match"] == "title" else 1, x["title"]))
    return json.dumps({"keyword": keyword, "count": len(matches),
                        "results": matches[:20]}, ensure_ascii=False)


@mcp.tool()
def wiki_list(type: str = "", tag: str = "") -> str:
    """List wiki pages, optionally filtered by type and/or tag.

    Types: source, entity, concept, analysis
    """
    nm = get_node_map()
    pages = []

    for nid, n in nm.items():
        if type and n["type"] != type:
            continue
        if tag and tag not in [t.lower() for t in n.get("tags", [])]:
            continue
        pages.append({
            "id": nid,
            "title": n["title"],
            "type": n["type"],
            "tags": n.get("tags", []),
            "url": f"{WIKI_URL}/{n['path']}",
        })

    pages.sort(key=lambda x: x["title"])
    return json.dumps({"filter": {"type": type or "all", "tag": tag or "all"},
                        "count": len(pages), "pages": pages}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
