#!/usr/bin/env python3
"""wiki-query.py — Query the Wiki knowledge base using BM25 + graph augmentation.

Usage:
  python3 wiki-query.py "TPU v7 和 B200 谁更适合 MoE？"
  python3 wiki-query.py "MFU 怎么算" --top-k 3
  python3 wiki-query.py "FSDP vs TP" --save
  python3 wiki-query.py "并行策略" --format json
"""
import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WIKI_REPO

DATA_DIR = WIKI_REPO / "wiki-data"
WIKI_URL = os.environ.get("CC_PAGES_URL_PREFIX", "") + "/wiki"


# ── Tokenizer (no external dependencies) ──

def tokenize(text):
    """Simple mixed Chinese/English tokenizer.

    Extracts: English words, Chinese character bigrams, numbers.
    """
    text = text.lower()
    tokens = []

    # English words and numbers
    tokens.extend(re.findall(r'[a-z][a-z0-9_-]*[a-z0-9]|[a-z]', text))

    # Chinese characters → bigrams for better matching
    chinese = re.findall(r'[\u4e00-\u9fff]+', text)
    for segment in chinese:
        # Unigrams
        tokens.extend(list(segment))
        # Bigrams
        for i in range(len(segment) - 1):
            tokens.append(segment[i:i+2])

    return tokens


# ── BM25 Engine ──

class BM25:
    """Okapi BM25 scoring, no external dependencies."""

    def __init__(self, corpus, k1=1.5, b=0.75):
        """
        Args:
            corpus: list of (doc_id, tokens) tuples
        """
        self.k1 = k1
        self.b = b
        self.corpus = corpus
        self.doc_count = len(corpus)
        self.avgdl = sum(len(toks) for _, toks in corpus) / max(self.doc_count, 1)

        # Document frequency
        self.df = Counter()
        self.doc_tokens = {}
        self.doc_tf = {}

        for doc_id, tokens in corpus:
            self.doc_tokens[doc_id] = tokens
            tf = Counter(tokens)
            self.doc_tf[doc_id] = tf
            for term in set(tokens):
                self.df[term] += 1

    def _idf(self, term):
        df = self.df.get(term, 0)
        return math.log((self.doc_count - df + 0.5) / (df + 0.5) + 1)

    def score(self, query_tokens, doc_id):
        tf = self.doc_tf.get(doc_id, {})
        dl = len(self.doc_tokens.get(doc_id, []))
        score = 0.0
        for term in query_tokens:
            f = tf.get(term, 0)
            if f == 0:
                continue
            idf = self._idf(term)
            num = f * (self.k1 + 1)
            den = f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            score += idf * num / den
        return score

    def search(self, query_tokens, top_k=10):
        """Return top-k (doc_id, score) pairs."""
        scores = []
        for doc_id, _ in self.corpus:
            s = self.score(query_tokens, doc_id)
            if s > 0:
                scores.append((doc_id, s))
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]


# ── Query Engine ──

def load_search_index():
    """Load search-chunks.json."""
    path = DATA_DIR / "search-chunks.json"
    if not path.exists():
        print(f"Error: Search index not found at {path}", file=sys.stderr)
        print("Run: python3 build-search-index.py", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def load_graph():
    """Load graph.json for augmentation."""
    path = DATA_DIR / "graph.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def get_graph_neighbors(graph, slug, depth=1):
    """Get N-hop neighbors from graph."""
    if not graph:
        return set()

    node_ids = {n["id"] for n in graph.get("nodes", [])}
    adj = defaultdict(set)
    for link in graph.get("links", []):
        src = link["source"] if isinstance(link["source"], str) else (link["source"].get("id", "") if isinstance(link["source"], dict) else "")
        tgt = link["target"] if isinstance(link["target"], str) else (link["target"].get("id", "") if isinstance(link["target"], dict) else "")
        if src in node_ids and tgt in node_ids:
            adj[src].add(tgt)
            adj[tgt].add(src)

    visited = {slug}
    frontier = {slug}
    for _ in range(depth):
        next_frontier = set()
        for node in frontier:
            for neighbor in adj.get(node, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.add(neighbor)
        frontier = next_frontier

    visited.discard(slug)
    return visited


def query(question, top_k=5, save=False):
    """Execute a query against the wiki search index."""
    index = load_search_index()
    graph = load_graph()
    chunks = index.get("chunks", [])

    if not chunks:
        return {"query": question, "results": [], "message": "Empty search index"}

    # Build BM25 corpus from chunks
    corpus = []
    chunk_map = {}
    for chunk in chunks:
        chunk_id = chunk["id"]
        # Include title, tags, and text in tokenization for better matching
        combined = f"{chunk['page_title']} {' '.join(chunk['tags'])} {chunk['text']}"
        tokens = tokenize(combined)
        corpus.append((chunk_id, tokens))
        chunk_map[chunk_id] = chunk

    bm25 = BM25(corpus)
    query_tokens = tokenize(question)

    # BM25 search at chunk level
    chunk_results = bm25.search(query_tokens, top_k=top_k * 3)

    # Aggregate scores by page
    page_scores = defaultdict(float)
    page_chunks = defaultdict(list)
    page_info = {}

    for chunk_id, score in chunk_results:
        chunk = chunk_map[chunk_id]
        page_id = chunk["page_id"]
        page_scores[page_id] += score
        page_chunks[page_id].append({
            "text": chunk["text"][:300],  # Truncate for output
            "score": round(score, 4),
        })
        if page_id not in page_info:
            page_info[page_id] = {
                "title": chunk["page_title"],
                "type": chunk["page_type"],
                "path": chunk["path"],
                "tags": chunk["tags"],
            }

    # Graph augmentation: boost pages that are neighbors of top results
    top_pages = sorted(page_scores.items(), key=lambda x: -x[1])[:3]
    neighbor_boost = set()
    for page_id, _ in top_pages:
        neighbors = get_graph_neighbors(graph, page_id, depth=1)
        neighbor_boost.update(neighbors)

    for page_id in neighbor_boost:
        if page_id not in page_scores:
            # Check if this page exists in chunks
            for chunk in chunks:
                if chunk["page_id"] == page_id:
                    # Add with small base score
                    page_scores[page_id] = 0.1
                    if page_id not in page_info:
                        page_info[page_id] = {
                            "title": chunk["page_title"],
                            "type": chunk["page_type"],
                            "path": chunk["path"],
                            "tags": chunk["tags"],
                        }
                    break

    # Sort and format results
    sorted_pages = sorted(page_scores.items(), key=lambda x: -x[1])[:top_k]

    results = []
    for page_id, score in sorted_pages:
        info = page_info.get(page_id, {})
        result = {
            "page_id": page_id,
            "title": info.get("title", page_id),
            "type": info.get("type", ""),
            "path": info.get("path", ""),
            "url": f"{WIKI_URL}/{info.get('path', '')}",
            "tags": info.get("tags", []),
            "relevance_score": round(score, 4),
            "matched_chunks": page_chunks.get(page_id, [])[:3],
        }

        # Add graph neighbors as related pages
        neighbors = get_graph_neighbors(graph, page_id, depth=1)
        result["related_pages"] = sorted(neighbors)[:5]

        results.append(result)

    output = {
        "query": question,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result_count": len(results),
        "results": results,
    }

    # Save query to log if --save
    if save:
        save_query_log(output)
        output["saved"] = True

    return output


def save_query_log(query_result):
    """Append query result to query-log.json."""
    log_path = DATA_DIR / "query-log.json"

    if log_path.exists():
        log = json.loads(log_path.read_text(encoding="utf-8"))
    else:
        log = {"queries": []}

    log["queries"].append({
        "timestamp": query_result["timestamp"],
        "question": query_result["query"],
        "result_count": query_result["result_count"],
        "pages_consulted": [r["page_id"] for r in query_result["results"]],
    })

    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def format_text(output):
    """Format query results as human-readable text."""
    lines = [f"Query: {output['query']}", f"Found: {output['result_count']} relevant pages", ""]

    for i, r in enumerate(output["results"], 1):
        lines.append(f"{i}. [{r['type']}] {r['title']} (score: {r['relevance_score']})")
        lines.append(f"   URL: {r['url']}")
        if r["tags"]:
            lines.append(f"   Tags: {', '.join(r['tags'])}")
        if r.get("related_pages"):
            lines.append(f"   Related: {', '.join(r['related_pages'][:3])}")
        if r.get("matched_chunks"):
            preview = r["matched_chunks"][0]["text"][:150].replace("\n", " ")
            lines.append(f"   Preview: {preview}...")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Query the CC Wiki knowledge base")
    parser.add_argument("question", help="Question to search for")
    parser.add_argument("--top-k", type=int, default=5, help="Number of results (default: 5)")
    parser.add_argument("--save", action="store_true", help="Save query to query-log.json")
    parser.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    args = parser.parse_args()

    result = query(args.question, top_k=args.top_k, save=args.save)

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_text(result))


if __name__ == "__main__":
    main()
